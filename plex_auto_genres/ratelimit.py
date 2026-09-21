"""Async rate limiting.

v1 slept a flat 4 seconds before every Jikan call -- twice per anime, so 8
seconds of guaranteed dead time per title regardless of how fast the API
actually answered. This replaces that with token buckets that model the
provider's real published limits, which lets requests run concurrently right up
to the allowance and no further.
"""

from __future__ import annotations

import asyncio
import logging
import time
import weakref
from dataclasses import dataclass

log = logging.getLogger(__name__)


class TokenBucket:
    """A single leaky-bucket limiter.

    ``rate`` tokens are added per ``period`` seconds, up to ``burst`` in stock.
    :meth:`acquire` waits until a token is free, then takes it.
    """

    __slots__ = ("_burst", "_lock", "_period", "_rate", "_tokens", "_updated")

    def __init__(self, rate: float, period: float = 1.0, burst: float | None = None) -> None:
        if rate <= 0 or period <= 0:
            raise ValueError("rate and period must be positive")
        self._rate = rate
        self._period = period
        self._burst = burst if burst is not None else rate
        self._tokens = float(self._burst)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    @property
    def period(self) -> float:
        return self._period

    def retune(self, rate: float) -> None:
        """Adopt a rate the provider itself advertises.

        A published limit is a promise about a normal day; the header on every
        response is what the service is serving *now*. AniList, for one, has
        been answering at a third of its documented rate for a long while.
        """
        if rate <= 0 or rate == self._rate:
            return
        self._refill()
        log.info("rate limit retuned: %g -> %g per %gs", self._rate, rate, self._period)
        self._rate = rate
        self._burst = _burst_for(rate, self._period)
        self._tokens = min(self._tokens, self._burst)

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._updated
        if elapsed > 0:
            self._tokens = min(self._burst, self._tokens + elapsed * (self._rate / self._period))
            self._updated = now

    async def acquire(self) -> None:
        """Wait until a token is available, then consume it."""
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                deficit = 1.0 - self._tokens
                wait = deficit * (self._period / self._rate)
            # Sleep outside the lock so other tasks can still refill-check.
            await asyncio.sleep(max(wait, 0.01))


#: A 429 landing within this long of the last cooldown counts as the same
#: refusal continuing, and doubles the next pause.
PENALTY_MEMORY_S = 30.0
#: Ceiling on that doubling.
PENALTY_ESCALATION_MAX = 8.0
#: However long the escalation -- or the provider's own Retry-After -- works
#: out to, requests are never held for longer than this at a stretch. A
#: provider still refusing afterwards simply earns another cooldown.
MAX_PENALTY_S = 60.0


class CompositeLimiter:
    """Several buckets that must all allow a request (e.g. 3/s *and* 60/min)."""

    __slots__ = ("_buckets", "_penalty_lock", "_penalty_until", "_streak", "_streak_until")

    def __init__(self, *buckets: TokenBucket) -> None:
        self._buckets = buckets
        self._penalty_until = 0.0
        self._penalty_lock = asyncio.Lock()
        self._streak = 0
        self._streak_until = 0.0

    async def acquire(self) -> None:
        """Wait until every bucket allows a request."""
        # Honour any server-requested cooldown before spending tokens.
        while True:
            async with self._penalty_lock:
                remaining = self._penalty_until - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(remaining, 5.0))
        for bucket in self._buckets:
            await bucket.acquire()

    async def penalise(self, seconds: float, *, escalate: bool = True) -> float:
        """Back off after HTTP 429, harder each time it keeps happening.

        Returns the pause actually applied. A provider policing a rolling
        window goes on refusing for the rest of that window, so repeating one
        short pause only burns the item's attempts; each refusal that lands in
        the shadow of the previous one therefore doubles the wait.

        ``escalate=False`` marks a retry of the refusal already counted: one
        title retrying three times is one incident, and letting it climb the
        ladder by itself would price a single hiccup like a sustained outage.
        """
        async with self._penalty_lock:
            now = time.monotonic()
            if escalate or not self._streak:
                self._streak = self._streak + 1 if now < self._streak_until else 1
            factor = min(2.0 ** (self._streak - 1), PENALTY_ESCALATION_MAX)
            delay = max(seconds, 0.0) * factor
            self._penalty_until = min(max(self._penalty_until, now + delay), now + MAX_PENALTY_S)
            self._streak_until = self._penalty_until + PENALTY_MEMORY_S
            return self._penalty_until - now

    def note_success(self) -> None:
        """A request got through: unwind one step of the escalation."""
        if self._streak:
            self._streak -= 1

    def observe_limit(self, allowance: float, window: float = 60.0) -> None:
        """Take the provider at its word about its own allowance per window."""
        for bucket in self._buckets:
            if bucket.period == window:
                bucket.retune(allowance)


#: How much of a long window may be spent in one go. Handing out a whole
#: minute's allowance as a single burst is what trips a provider's own
#: rolling-window limiter: we deliver the entire minute in the first few
#: seconds, it sees the spike, and everything after that comes back 429.
BURST_SECONDS = 5.0


def _burst_for(rate: float, period: float) -> float:
    """Stock for a window: the full rate per second, five seconds' worth above."""
    if period <= 1.0:
        return rate
    return max(1.0, min(rate, rate / period * BURST_SECONDS))


@dataclass(frozen=True, slots=True)
class LimitSpec:
    """Published limits for a provider, as (requests, per_seconds) pairs."""

    windows: tuple[tuple[float, float], ...]

    def build(self) -> CompositeLimiter:
        return CompositeLimiter(
            *(TokenBucket(rate=n, period=p, burst=_burst_for(n, p)) for n, p in self.windows)
        )


#: One limiter per provider per event loop. A provider's quota is per
#: *process*, not per pool: a running job and the binding picker's searches
#: draw on the same allowance, and a 429 cooldown applies to both.
_SHARED: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, CompositeLimiter]]" = (
    weakref.WeakKeyDictionary()
)


def shared_limiter(name: str, spec: LimitSpec) -> CompositeLimiter:
    """The process-wide limiter for ``name`` on the running loop (fresh if none)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return spec.build()
    per_loop = _SHARED.setdefault(loop, {})
    limiter = per_loop.get(name)
    if limiter is None:
        limiter = per_loop[name] = spec.build()
    return limiter


#: https://docs.api.jikan.moe/#section/Information/Rate-Limiting -- 3/s, 60/min,
#: and no rate-limit header on the wire, so these figures are all there is.
#: Jikan's own note is worth remembering when it refuses anyway: "It's still
#: possible to get rate limited from MyAnimeList.net instead."
JIKAN_LIMITS = LimitSpec(((3, 1.0), (60, 60.0)))
#: https://docs.anilist.co/guide/rate-limiting -- 90/min on paper, but the API
#: has been "in a degraded state" and serving 30 for a long time, which is what
#: its X-RateLimit-Limit header reports. Start at what it actually serves; the
#: transport reads that header and retunes if the real figure differs, so a
#: restored AniList speeds back up without waiting for a release.
ANILIST_LIMITS = LimitSpec(((30, 60.0),))
#: https://developer.themoviedb.org/docs/rate-limiting -- the old 40-per-10s
#: cap was retired in 2019 and what remains is "somewhere in the 40 requests
#: per second range", with no header to read and an instruction to respect the
#: 429, which the transport does.
TMDB_LIMITS = LimitSpec(((40, 1.0),))
