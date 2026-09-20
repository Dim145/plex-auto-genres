"""Provider registry and construction."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

from ..config import ProviderSettings
from ..errors import ConfigError, ProviderAuthError
from ..models import MediaType
from ..ratelimit import ANILIST_LIMITS, JIKAN_LIMITS, TMDB_LIMITS, shared_limiter
from .anidb_map import AniDbMapper
from .anilist import AniListProvider
from .base import HttpTransport, LookupRequest, Provider, USER_AGENT
from .jikan import JikanProvider
from .tmdb import TmdbProvider

log = logging.getLogger(__name__)

#: Consecutive unreachable answers before a provider is stood down for a
#: while. A public API having a bad minute should not cost every remaining
#: title its full retry budget before the next source gets a turn.
UNHEALTHY_AFTER = 5
#: First stand-down, doubling with each repeat up to :data:`MAX_COOLDOWN_S`.
COOLDOWN_S = 30.0
MAX_COOLDOWN_S = 300.0

__all__ = [
    "GUID_SCHEMES",
    "AniDbMapper",
    "AniListProvider",
    "JikanProvider",
    "LookupRequest",
    "Provider",
    "ProviderPool",
    "TmdbProvider",
    "build_providers",
]

_LIMITS = {"jikan": JIKAN_LIMITS, "anilist": ANILIST_LIMITS, "tmdb": TMDB_LIMITS}
_CLASSES: dict[str, type[Provider]] = {
    "jikan": JikanProvider, "anilist": AniListProvider, "tmdb": TmdbProvider,
}

#: Which Plex GUID schemes each provider resolves without a search. Class
#: attributes, so no credentials are needed to consult this.
GUID_SCHEMES: dict[str, tuple[str, ...]] = {
    "jikan": JikanProvider.guid_schemes,
    "anilist": AniListProvider.guid_schemes,
    "tmdb": TmdbProvider.guid_schemes,
}


@dataclass(slots=True)
class _Health:
    """One provider's recent record, for the run it belongs to."""

    strikes: int = 0
    stand_downs: int = 0
    until: float = 0.0


class ProviderPool:
    """Owns the HTTP client and the provider instances for one run.

    It also keeps each provider's recent record. A source that has stopped
    answering is stood down for a moment so the rest of the library goes
    straight to the next one instead of paying its retries title by title.
    """

    def __init__(self, providers: list[Provider], client: httpx.AsyncClient) -> None:
        self.providers = providers
        self._client = client
        self._health = {p.name: _Health() for p in providers}
        #: Answers of any kind, counted the moment they arrive. The run asks
        #: this before giving up, because a written item lands in the report
        #: much later than the answer that produced it.
        self.answers = 0

    def usable(self, providers: list[Provider]) -> list[Provider]:
        """Those of ``providers`` not currently standing down."""
        now = time.monotonic()
        return [p for p in providers if self._health[p.name].until <= now]

    def note_reachable(self, provider: Provider) -> None:
        """The provider answered -- whatever the answer was."""
        self.answers += 1
        health = self._health[provider.name]
        health.strikes = 0
        health.until = 0.0

    def note_unreachable(self, provider: Provider, reason: str) -> bool:
        """Record a transport failure. True when it stands the provider down."""
        health = self._health[provider.name]
        if health.until > time.monotonic():
            # Already standing down: the requests that were in flight when it
            # started are the same incident, not evidence of a longer outage.
            return False
        health.strikes += 1
        if health.strikes < UNHEALTHY_AFTER:
            return False
        health.strikes = 0
        health.stand_downs += 1
        pause = min(COOLDOWN_S * 2 ** (health.stand_downs - 1), MAX_COOLDOWN_S)
        health.until = time.monotonic() + pause
        log.warning(
            "%s is not answering (%s); leaving it alone for %.0fs", provider.name, reason, pause
        )
        return True

    @property
    def request_count(self) -> int:
        return sum(p.request_count for p in self.providers)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "ProviderPool":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


def build_providers(
    names: tuple[str, ...],
    media_type: MediaType,
    settings: ProviderSettings,
) -> ProviderPool:
    """Instantiate the requested providers, sharing one connection pool.

    Everything that can be refused is checked before the client exists, so a
    bad request (unknown provider, wrong type, missing TMDB key) never leaves
    an unclosed pool behind.
    """
    if not names:
        raise ConfigError(f"No providers configured for a {media_type.value} library.")
    for name in names:
        cls = _CLASSES.get(name)
        if cls is None:
            raise ConfigError(f"Unknown provider {name!r}. Known: {', '.join(sorted(_LIMITS))}.")
        if media_type not in cls.supports:
            raise ConfigError(
                f"Provider {name!r} cannot serve a {media_type.value} library. "
                f"It supports: {', '.join(t.value for t in cls.supports)}."
            )
        if name == "tmdb" and not settings.tmdb_api_key:
            raise ProviderAuthError(
                "TMDB_API_KEY is not set. It is required for standard-tv and "
                "standard-movie libraries, and for an anime library that falls "
                "back to TMDB."
            )

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(20.0, connect=10.0),
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
        limits=httpx.Limits(max_connections=settings.concurrency * 2),
    )
    built: list[Provider] = []
    for name in names:
        transport = HttpTransport(
            client, shared_limiter(name, _LIMITS[name]),
            max_attempts=settings.max_attempts, name=name,
        )
        if name == "tmdb":
            built.append(
                TmdbProvider(transport, settings.tmdb_api_key or "", settings.tmdb_language)
            )
        else:
            built.append(_CLASSES[name](transport))
    return ProviderPool(built, client)
