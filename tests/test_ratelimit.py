"""Token bucket behaviour."""

from __future__ import annotations

import asyncio
import time

import pytest

from plex_auto_genres.ratelimit import (
    ANILIST_LIMITS,
    JIKAN_LIMITS,
    MAX_PENALTY_S,
    PENALTY_ESCALATION_MAX,
    CompositeLimiter,
    LimitSpec,
    TokenBucket,
    _burst_for,
)


async def test_burst_is_allowed_immediately():
    bucket = TokenBucket(rate=5, period=1.0)
    start = time.monotonic()
    await asyncio.gather(*(bucket.acquire() for _ in range(5)))
    assert time.monotonic() - start < 0.2


async def test_requests_beyond_the_burst_are_paced():
    bucket = TokenBucket(rate=10, period=1.0, burst=2)
    start = time.monotonic()
    for _ in range(4):
        await bucket.acquire()
    # 2 free, then 2 more at 10/s -> at least ~0.2s.
    assert time.monotonic() - start >= 0.15


async def test_composite_requires_every_window():
    limiter = CompositeLimiter(TokenBucket(100, 1.0), TokenBucket(2, 1.0, burst=2))
    start = time.monotonic()
    for _ in range(3):
        await limiter.acquire()
    assert time.monotonic() - start >= 0.3


async def test_penalty_delays_the_next_acquire():
    limiter = LimitSpec(((100, 1.0),)).build()
    await limiter.penalise(0.3)
    start = time.monotonic()
    await limiter.acquire()
    assert time.monotonic() - start >= 0.25


def test_invalid_rates_are_rejected():
    with pytest.raises(ValueError):
        TokenBucket(rate=0)
    with pytest.raises(ValueError):
        TokenBucket(rate=1, period=0)


async def test_concurrency_is_actually_concurrent():
    """Ten calls at 10/s should take ~1s, not 10x the per-call latency."""
    limiter = LimitSpec(((20, 1.0),)).build()

    async def call():
        await limiter.acquire()
        await asyncio.sleep(0.1)   # simulated network latency

    start = time.monotonic()
    await asyncio.gather(*(call() for _ in range(10)))
    elapsed = time.monotonic() - start
    assert elapsed < 0.5, f"expected overlap, took {elapsed:.2f}s"


async def test_a_long_window_is_not_handed_out_as_one_burst():
    """Jikan's 60/min must not arrive as 60 requests in the first few seconds.

    That spike is what a provider's own rolling-window limiter sees, and what
    came back as a library full of "rate limited" failures.
    """
    minute = LimitSpec(((60, 60.0),)).build()
    await asyncio.gather(*(minute.acquire() for _ in range(5)))   # the whole stock
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(minute.acquire(), timeout=0.3)
    assert JIKAN_LIMITS.windows == ((3, 1.0), (60, 60.0))


def test_burst_for_scales_with_the_window():
    assert _burst_for(40, 1.0) == 40        # a per-second limit is the burst
    assert _burst_for(60, 60.0) == 5.0      # five seconds' worth of a minute
    assert _burst_for(90, 60.0) == 7.5
    assert _burst_for(2, 60.0) == 1.0       # never less than one request


async def test_repeated_refusals_escalate_the_pause():
    limiter = LimitSpec(((100, 1.0),)).build()
    first = await limiter.penalise(1.0)
    second = await limiter.penalise(1.0)
    third = await limiter.penalise(1.0)
    assert [first, second, third] == pytest.approx([1.0, 2.0, 4.0], abs=0.05)


async def test_escalation_is_capped():
    limiter = LimitSpec(((100, 1.0),)).build()
    pauses = [await limiter.penalise(1.0) for _ in range(8)]
    assert max(pauses) == pytest.approx(PENALTY_ESCALATION_MAX, abs=0.05)


async def test_a_request_getting_through_unwinds_the_escalation():
    limiter = LimitSpec(((100, 1.0),)).build()
    await limiter.penalise(1.0)
    await limiter.penalise(1.0)          # streak 2
    limiter.note_success()               # back down to 1
    assert await limiter.penalise(1.0) == pytest.approx(2.0, abs=0.05)


async def test_a_quiet_spell_resets_the_escalation():
    limiter = LimitSpec(((100, 1.0),)).build()
    assert await limiter.penalise(2.0) == pytest.approx(2.0, abs=0.05)
    # Far enough past the cooldown's shadow that this counts as a new incident.
    limiter._streak_until = time.monotonic() - 1.0       # noqa: SLF001
    limiter._penalty_until = time.monotonic() - 1.0      # noqa: SLF001
    assert await limiter.penalise(2.0) == pytest.approx(2.0, abs=0.05)


async def test_retrying_one_refusal_does_not_climb_the_ladder():
    """Three attempts on one title are one incident, not three."""
    limiter = LimitSpec(((100, 1.0),)).build()
    first = await limiter.penalise(1.0)
    retry = await limiter.penalise(1.0, escalate=False)
    assert [first, retry] == pytest.approx([1.0, 1.0], abs=0.05)
    assert await limiter.penalise(1.0) == pytest.approx(2.0, abs=0.05), "a new one still climbs"


async def test_a_cooldown_is_never_longer_than_the_ceiling():
    limiter = LimitSpec(((100, 1.0),)).build()
    assert await limiter.penalise(3600.0) == pytest.approx(MAX_PENALTY_S, abs=0.05)


async def test_a_provider_that_advertises_its_limit_is_taken_at_its_word():
    """AniList publishes 90 a minute and has been serving 30 for years; the
    header on every response is the only figure that is actually current."""
    limiter = LimitSpec(((90, 60.0),)).build()
    limiter.observe_limit(30.0)

    # Five seconds' worth of the new rate, not the old one.
    await asyncio.gather(*(limiter.acquire() for _ in range(2)))
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(limiter.acquire(), timeout=0.3)


async def test_an_advertised_limit_can_relax_the_pacing_again():
    limiter = LimitSpec(((30, 60.0),)).build()
    limiter.observe_limit(90.0)
    await asyncio.gather(*(limiter.acquire() for _ in range(7)))   # 5s of 90/min


async def test_a_nonsense_advertised_limit_is_ignored():
    limiter = LimitSpec(((30, 60.0),)).build()
    for value in (0.0, -5.0):
        limiter.observe_limit(value)
    await asyncio.gather(*(limiter.acquire() for _ in range(2)))
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(limiter.acquire(), timeout=0.3)


def test_anilist_is_paced_at_what_it_serves_today():
    assert ANILIST_LIMITS.windows == ((30, 60.0),)
