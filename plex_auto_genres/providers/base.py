"""Provider protocol and the shared HTTP plumbing."""

from __future__ import annotations

import abc
import asyncio
import logging
import random
from dataclasses import dataclass, field
from typing import Any, TypeVar

import httpx

from .. import __version__
from ..errors import ProviderAuthError, ProviderError, ProviderNotFound, ProviderRateLimited
from ..models import Candidate, ExternalId, MediaType, ProviderResult
from ..ratelimit import CompositeLimiter

log = logging.getLogger(__name__)

USER_AGENT = f"plex-auto-genres/{__version__}"

#: Pause after a 429 that came with no Retry-After header. Providers that
#: police a rolling minute keep refusing for the rest of it, so the pause has
#: to be worth the wait; :meth:`CompositeLimiter.penalise` grows it from here
#: while the refusals continue.
DEFAULT_COOLDOWN_S = 5.0
#: Once a cooldown reaches this, retrying *this* title is a waste: the pause
#: is already in force for every request, so the title is handed back as
#: unresolved and the next run picks it up. Sitting through three of these
#: per title is what turned one bad minute into a library of failures.
RETRY_CEILING_S = 20.0


@dataclass(slots=True)
class LookupRequest:
    """Everything a provider needs to resolve one Plex item."""

    title: str
    year: int | None
    media_type: MediaType
    use_keywords: bool = False
    #: External ids read straight off the Plex item's GUIDs.
    external_ids: list[ExternalId] = field(default_factory=list)
    #: A manual binding, which overrides both GUIDs and title search: the id
    #: the user pinned, plus every id derivable from it. A binding names an id
    #: *scheme*, not a provider, so each provider takes whichever one it can
    #: consume -- an AniDB pin reaches Jikan as the MAL id it maps to.
    pinned: list[ExternalId] = field(default_factory=list)

    def id_for(self, *schemes: str) -> ExternalId | None:
        """The first external id matching any of ``schemes``, in that order."""
        return _first(self.external_ids, schemes)

    def pinned_for(self, *schemes: str) -> ExternalId | None:
        """The pinned id this provider can consume, if the binding reaches it."""
        return _first(self.pinned, schemes)


def _first(ids: list[ExternalId], schemes: tuple[str, ...]) -> ExternalId | None:
    """The first id matching any of ``schemes``, in the order given."""
    for scheme in schemes:
        match = next((e for e in ids if e.scheme == scheme), None)
        if match is not None:
            return match
    return None


class HttpTransport:
    """Rate-limited HTTP with retry-on-transient, shared by all providers."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        limiter: CompositeLimiter,
        *,
        max_attempts: int = 3,
        name: str = "provider",
    ) -> None:
        self._client = client
        self._limiter = limiter
        self._max_attempts = max_attempts
        self._name = name
        self.request_count = 0

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Perform a rate-limited request, retrying transient failures.

        Every non-2xx outcome surfaces as a :class:`ProviderError` subclass,
        never a raw ``httpx`` exception, so the pipeline's provider fallback
        and its per-item failure handling see one error family.
        """
        last_exc: Exception | None = None
        refused = False
        for attempt in range(1, self._max_attempts + 1):
            await self._limiter.acquire()
            try:
                self.request_count += 1
                response = await self._client.request(method, url, **kwargs)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = ProviderError(f"{self._name}: network error: {exc}")
                await self._sleep_backoff(attempt)
                continue

            if response.status_code == 429:
                asked = _parse_retry_after(response)
                paused = await self._limiter.penalise(
                    DEFAULT_COOLDOWN_S if asked is None else asked, escalate=not refused
                )
                refused = True
                last_exc = ProviderRateLimited(f"{self._name}: rate limited", retry_after=paused)
                log.debug("%s: 429, holding every request for %.1fs", self._name, paused)
                if paused >= RETRY_CEILING_S:
                    break
                continue

            if response.status_code == 404:
                # An answer, and proof the source is up.
                self._limiter.note_success()
                raise ProviderNotFound(f"{self._name}: no record at {url}")

            if 500 <= response.status_code < 600:
                last_exc = ProviderError(f"{self._name}: HTTP {response.status_code}")
                await self._sleep_backoff(attempt)
                continue

            if response.status_code in (401, 403):
                self._limiter.note_success()
                raise ProviderAuthError(
                    f"{self._name}: HTTP {response.status_code} -- check the API key"
                )
            # v1 never inspected status codes at all: a 429 body became a
            # KeyError that aborted the whole run.
            if response.status_code >= 400:
                raise ProviderError(f"{self._name}: HTTP {response.status_code} for {url}")
            self._limiter.note_success()
            return response

        raise last_exc or ProviderError(f"{self._name}: exhausted retries for {url}")

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        response = await self.request("GET", url, **kwargs)
        return response.json()

    @staticmethod
    async def _sleep_backoff(attempt: int) -> None:
        """Exponential backoff with jitter, and a floor under it.

        The jitter keeps parallel workers from retrying in lockstep. The floor
        matters just as much: jitter measured from zero lets a worker hit a
        gateway that is already timing out again before it has had a moment.
        """
        step = 2.0 ** (attempt - 1)
        low = min(0.5 * step, 8.0)
        high = min(2.0 * step, 30.0)
        # Not security-relevant: a timing jitter, so the PRNG is the right tool.
        await asyncio.sleep(random.uniform(low, high))  # nosec B311


def _parse_retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


class Provider(abc.ABC):
    """A metadata source.

    Implementations resolve an item either directly by external id -- which is
    exact and skips a search round trip -- or by title as a fallback.
    """

    #: Stable name used in config, the cache and CLI output.
    name: str = "provider"
    #: Plex GUID schemes this provider can consume without a search.
    guid_schemes: tuple[str, ...] = ()
    #: Library types this provider can serve.
    supports: tuple[MediaType, ...] = ()

    def __init__(self, transport: HttpTransport) -> None:
        self.transport = transport

    @property
    def request_count(self) -> int:
        return self.transport.request_count

    def handles(self, media_type: MediaType) -> bool:
        return media_type in self.supports

    @abc.abstractmethod
    async def fetch_by_id(self, external_id: ExternalId, request: LookupRequest) -> ProviderResult:
        """Resolve an exact provider id. Raises :class:`ProviderNotFound` if absent."""

    @abc.abstractmethod
    async def search(self, request: LookupRequest) -> ProviderResult:
        """Resolve by title. Raises :class:`ProviderNotFound` when nothing matches."""

    @abc.abstractmethod
    async def search_candidates(self, request: LookupRequest, limit: int = 8) -> list[Candidate]:
        """The ranked alternatives a human could pick from. Empty when none."""

    async def resolve(self, request: LookupRequest) -> ProviderResult:
        """Preferred path first: pinned binding, then GUID, then title search.

        Reading the id off the Plex GUID is the single biggest accuracy win
        over v1, which always searched by title and blindly took result [0].
        """
        pin = request.pinned_for(*self.guid_schemes)
        if pin is not None:
            result = await self.fetch_by_id(pin, request)
            result.matched_by = "binding"
            return result

        direct = request.id_for(*self.guid_schemes)
        if direct is not None:
            try:
                result = await self.fetch_by_id(direct, request)
            except ProviderNotFound:
                log.debug("%s: guid %s missing upstream, falling back to search",
                          self.name, direct)
            else:
                result.matched_by = "guid"
                return result

        result = await self.search(request)
        result.matched_by = "search"
        return result


T = TypeVar("T")


def score_of(value: object, *, scale: float = 1.0) -> float | None:
    """A provider's numeric score on the 0-10 scale, or ``None``.

    Zero and non-numbers mean "unrated"; writing 0 to Plex would be a real
    zero-star rating, not the absence of one.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return float(value) if scale == 1.0 else round(float(value) / scale, 1)


def rank_candidates(
    candidates: list[tuple[str, int | None, T]],
    title: str,
    year: int | None,
) -> list[T]:
    """Order candidates by title equality, then year proximity, best first.

    v1 took ``results[0]`` unconditionally. Using the year Plex already knows
    removes most of the mismatches that produced nonsense genres. The full
    ranking is what a human sees when picking a binding by hand.
    """
    target = _normalise(title)

    def score(entry: tuple[str, int | None, T]) -> tuple[int, int]:
        cand_title, cand_year, _ = entry
        name_score = 0 if _normalise(cand_title) == target else 1
        if year is None or cand_year is None:
            year_score = 1
        else:
            year_score = abs(cand_year - year)
            # More than a couple of years apart is almost certainly a different work.
            year_score = year_score if year_score <= 2 else 50 + year_score
        return name_score, year_score

    return [entry[2] for entry in sorted(candidates, key=score)]


def pick_best(
    candidates: list[tuple[str, int | None, T]],
    title: str,
    year: int | None,
) -> T | None:
    """The single closest candidate, or ``None``."""
    ranked = rank_candidates(candidates, title, year)
    return ranked[0] if ranked else None


def short(text: str | None, limit: int = 240) -> str | None:
    """Trim a synopsis to a card-sized excerpt at a word boundary."""
    if not text:
        return None
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut + "…"


def _normalise(value: str) -> str:
    return "".join(ch for ch in value.casefold() if ch.isalnum())
