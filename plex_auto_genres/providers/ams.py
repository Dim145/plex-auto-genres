"""arr-metadata-server, through the metadata provider it serves Plex.

arr-metadata-server keeps a catalogue of its own, merged from TMDB, TheTVDB and
the arr stack's services, in which a correction locked by hand outranks every
source. Its Plex provider (``/plex``) is the surface asked here because it
answers by the ids Plex items carry: a film by its TMDB or IMDb id, a series by
its TheTVDB or TMDB id, an item its own Plex agent matched by the key that agent
gave it, and anything else by title and year. The genres it answers with are
the catalogue's, locks and all -- where the TMDB relay the server also runs
keeps only the locked genres TMDB has a name for.

That provider lets callers in by address (``AMS_PLEX_AUTH=allowlist``, its
default) -- this app's goes on the server's list of allowed addresses, beside
Plex's -- or, set to ``apikey``, by a key issued there. Plex itself carries its
key in the address it is given, ``…/plex/<key>`` (arr-metadata-server 0.8.1);
this app takes a key from there or from ``ARR_METADATA_API_KEY``, and sends it
in a header, so that no address it quotes holds it.

Asking for a work by its key fetches it into the catalogue and keeps it there,
as when Plex asks; so a title search only goes that far for a work whose title
and year match, never for the nearest guess.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, NamedTuple
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import httpx

from ..errors import ConfigError, ProviderAuthError, ProviderError, ProviderNotFound
from ..models import Candidate, ExternalId, MediaType, ProviderResult
from .base import LookupRequest, Provider, rank_candidates, score_of, short

#: The scheme of the GUID arr-metadata-server's Plex agent gives an item. What
#: follows is the provider's own key for the work: ``…://movie/movie-tmdb-603``.
AGENT_SCHEME = "tv.plex.agents.custom.arrmetadataserver"

#: A key as the provider writes them: ``movie-tmdb-603``, ``show-tvdb-81189``,
#: ``movie-id-…`` for a work made by hand there, with no id anywhere else.
WORK_KEY = re.compile(r"^(movie|show)-[a-z]+-[A-Za-z0-9-]{1,64}$")
_NUMBER = re.compile(r"^[1-9][0-9]{0,11}$")
_IMDB = re.compile(r"^tt[0-9]{1,12}$")

#: The provider's types: 1 a film, 2 a series.
_FILM, _SERIES = 1, 2

#: The ids each kind is asked for by, best first: a film by TMDB's, then
#: IMDb's; a series by TheTVDB's -- which settles its numbering there -- then
#: TMDB's. The provider has no key for a series by its IMDb id.
_SCHEMES = {"movie": ("ams", "tmdb", "imdb"), "show": ("ams", "tvdb", "tmdb")}

#: Where a work's page is, by the ids the provider lists for it.
_PAGES = {
    "tmdb": "https://www.themoviedb.org/{segment}/{value}",
    "tvdb": "https://thetvdb.com/dereferrer/series/{value}",
    "imdb": "https://www.imdb.com/title/{value}/",
}


def refusal(status: int | None, code: str | None, *, key_from: str | None) -> str:
    """Why the server refused this app, and what lets it in.

    Its Plex provider lets callers in by address (``AMS_PLEX_AUTH=allowlist``,
    the default) or by a key issued there (``apikey``). A 401 is a key missing,
    or one it does not know; a 403 an address it does not allow, or a key
    switched off or run out -- told apart only by whether a key was given.
    ``key_from`` names the setting the key came from, when one did.
    """
    if code is None:
        # The server names every refusal, {"error": "unauthorized", …}: one
        # without a name came from whatever stands in front of it.
        return (
            f"The address in ARR_METADATA_URL answered HTTP {status}, and not as "
            "arr-metadata-server refuses: whatever stands in front of it -- a reverse "
            "proxy, the user and password it asks for -- refused this app. Check the "
            "address, and the user and password written into it."
        )
    if code == "owner_inactive" and key_from:
        return (
            f"arr-metadata-server refused the key this app gives it, from {key_from}: the "
            "account it belongs to there is not active. Issue this app a key of its own, "
            f"one that only reads, under Keys & network, and put it in {key_from}."
        )
    if status == 401:
        if key_from:
            return (
                "arr-metadata-server does not know the key this app gives it, from "
                f"{key_from}: copy it again, or issue another that only reads under Keys & "
                "network, and put it there."
            )
        return (
            "arr-metadata-server asks this app for a key (AMS_PLEX_AUTH=apikey): issue it "
            "one that only reads under Keys & network, and set it as ARR_METADATA_API_KEY."
        )
    if key_from:
        return (
            f"arr-metadata-server refused this app. The key it gives, from {key_from}, may "
            "be switched off or have run out -- issue another under Keys & network -- or "
            "the server lets callers in by address (AMS_PLEX_AUTH=allowlist), reading no "
            "key, and this app's is not allowed: allow it under Keys & network > Allowed "
            "addresses, beside Plex's."
        )
    return (
        "arr-metadata-server refused this app's address. Its Plex provider lets callers "
        "in by address: allow this app's under Keys & network > Allowed addresses, beside "
        "Plex's -- or have it ask for keys (AMS_PLEX_AUTH=apikey), and give this app one "
        "that only reads in ARR_METADATA_API_KEY."
    )


class AmsProvider(Provider):
    """Genres from an arr-metadata-server catalogue, corrections included."""

    name = "ams"
    #: Its own key first: an item its agent matched names the work exactly.
    guid_schemes = ("ams", "tmdb", "tvdb", "imdb")
    supports = (MediaType.ANIME, MediaType.STANDARD_TV, MediaType.STANDARD_MOVIE)

    def __init__(self, transport, base_url: str, api_key: str | None = None) -> None:
        super().__init__(transport)
        address = split_address(base_url)
        key = issued_key(address, api_key)
        self._server, self._auth = address.server, address.auth
        self._provider = f"{address.server}/plex"
        self._headers = key_headers(key.value)
        self._key_from = key.source

    @classmethod
    def schemes_for(cls, media_type: MediaType) -> tuple[str, ...]:
        if media_type.is_anime:
            # A section of anime holds films as often as series: an item is
            # asked for by the ids of what Plex says it is.
            return ("ams", "tvdb", "tmdb", "imdb")
        return _SCHEMES["movie" if media_type.is_movie else "show"]

    def resolution_order(self, request: LookupRequest) -> tuple[str, ...]:
        # What the item is, when Plex said: an anime library is as often a
        # section of films, and a film's ids asked for as a series' would
        # fetch whatever series bears those numbers into the catalogue.
        return _SCHEMES[_kind(request)]

    async def fetch_by_id(self, external_id: ExternalId, request: LookupRequest) -> ProviderResult:
        key = work_key(external_id, _kind(request))
        return self._to_result(await self._work(key), request)

    async def search(self, request: LookupRequest) -> ProviderResult:
        kind = _kind(request)
        fields: dict[str, Any] = {"title": request.title}
        if request.year:
            fields["year"] = request.year
        ids = [e for e in request.external_ids if e.scheme in _MATCHED_BY[kind]]
        if ids:
            fields["guids"] = [str(e) for e in ids]
        for entry in await self._matches(fields, kind):
            key = str(entry.get("ratingKey") or "")
            if not (WORK_KEY.match(key) and key.startswith(f"{kind}-")):
                continue
            if not (_shares_an_id(entry, ids) or _same_work(entry, request)):
                # The server's best title match is its search's first hit of a
                # fitting year; asking for it by key would fetch it into the
                # catalogue for good. Only the same title earns that.
                break
            # Asked for by its key, the work is the catalogue's -- corrections
            # and all -- rather than the summary a search draws from the
            # sources, and it is kept there from then on, as when Plex asks.
            return self._to_result(await self._work(key), request)
        raise ProviderNotFound(f"ams: no {_NOUN[kind]} named {request.title!r}")

    async def search_candidates(self, request: LookupRequest, limit: int = 8) -> list[Candidate]:
        kind = _kind(request)
        entries = [
            entry for entry in await self._matches({"title": request.title, "manual": True}, kind)
            if WORK_KEY.match(str(entry.get("ratingKey") or ""))
        ]
        ranked = rank_candidates(
            [(str(e.get("title") or ""), _year_of(e), e) for e in entries],
            request.title, request.year,
        )
        return [
            Candidate(
                provider=self.name,
                provider_id=str(entry["ratingKey"]),
                title=str(entry.get("title") or ""),
                year=_year_of(entry),
                url=_page(entry, kind),
                image=entry.get("thumb") or None,
                synopsis=short(entry.get("summary")),
                score=_score(entry),
                genres=_genres(entry),
            )
            for entry in ranked[:limit]
        ]

    # -- the provider's routes ------------------------------------------------

    async def _work(self, key: str) -> dict:
        found = _entries(await self._call("GET", f"/library/metadata/{quote(key, safe='')}"))
        if not found:
            raise ProviderNotFound(f"ams: no work at {key}")
        return found[0]

    async def _matches(self, fields: dict[str, Any], kind: str) -> list[dict]:
        # Adult works are found only as far as the server's own policy for
        # this caller allows, as with the TMDB source's include_adult.
        body = {"type": _FILM if kind == "movie" else _SERIES, "includeAdult": True, **fields}
        return _entries(await self._call("POST", "/library/metadata/matches", json=body))

    async def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self.transport.request(
                method, f"{self._provider}{path}", headers=self._headers, auth=self._auth,
                # A redirected search would come back a GET, which the
                # provider does not answer; say where it points instead.
                follow_redirects=False, **kwargs,
            )
        except ProviderAuthError as exc:
            raise ProviderAuthError(
                refusal(exc.status, exc.code, key_from=self._key_from),
                status=exc.status, code=exc.code,
            ) from exc
        if response.is_redirect:
            raise ProviderError(
                f"ams: {self._server} redirects to {_location(response)}; set "
                "ARR_METADATA_URL to the address it redirects to."
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderError(
                f"ams: {method} {path} did not answer JSON; is ARR_METADATA_URL the "
                "server's address?"
            ) from exc

    def _to_result(self, entry: dict, request: LookupRequest) -> ProviderResult:
        return ProviderResult(
            provider=self.name,
            provider_id=str(entry.get("ratingKey") or ""),
            title=str(entry.get("title") or request.title),
            genres=_genres(entry),
            score=_score(entry),
            url=_page(entry, _kind(request)),
        )


#: The ids a match is given, by kind: films are not matched by TheTVDB's.
_MATCHED_BY = {"movie": ("tmdb", "imdb"), "show": ("tvdb", "tmdb", "imdb")}
_NOUN = {"movie": "film", "show": "series"}


def _kind(request: LookupRequest) -> str:
    """Film or series: what Plex says the item is, else what its library holds."""
    return "movie" if request.is_film else "show"


#: What every key the server issues begins with, whatever its case: what tells
#: a key at the end of an address from a path the server is reached under.
KEY_PREFIX = "ams_"


class Address(NamedTuple):
    """Where the server is, with no credential in it, and the credentials apart."""

    server: str
    auth: httpx.BasicAuth | None = None
    #: The key the address carried: ``…/plex/<key>``, the address the server's
    #: Plex panel gives (arr-metadata-server 0.8.1).
    key: str | None = None


def split_address(raw: str) -> Address:
    """The server's address without credentials, and the credentials apart.

    A user and password written into ``ARR_METADATA_URL`` -- for a proxy in
    front of the server -- travel as basic auth, and a key at the end of it,
    ``…/plex/<key>``, in a header: neither reaches a message, a log line or a
    title's recorded error, which all quote the address.

    Whatever cannot be used is refused here, as a :class:`ConfigError`: a
    host or a port a parser chokes on would otherwise escape mid-run, or from
    the doctor, as an error of another kind.
    """
    address = (raw or "").strip().rstrip("/")
    if not address.startswith(("http://", "https://")):
        raise ConfigError(
            "ARR_METADATA_URL must be the server's address, starting with http:// "
            "or https://, e.g. http://arr-metadata-server:8080"
        )
    try:
        parts = urlsplit(address)
    except ValueError:
        # Neither quoted nor chained: what the parser says can carry the
        # password written into the address.
        raise ConfigError(_UNUSABLE) from None
    userinfo, sep, host = parts.netloc.rpartition("@")
    if not sep and "@" in address:
        raise ConfigError(
            "ARR_METADATA_URL has an @ outside a user and password it can read: "
            "percent-encode the password."
        )
    try:
        named, _ = parts.hostname, parts.port
    except ValueError:
        raise ConfigError(_UNUSABLE) from None
    path, key = parts.path, None
    head, keyed, segment = path.rpartition("/plex/")
    if keyed and "/" not in segment and unquote(segment)[:len(KEY_PREFIX)].lower() == KEY_PREFIX:
        path, key = head, unquote(segment)
    server = urlunsplit(parts._replace(netloc=host, path=path))
    try:
        httpx.URL(server)
    except (ValueError, httpx.InvalidURL):
        named = None
    # A query or a fragment would end up before the provider's own path.
    if not named or any(ch.isspace() for ch in named) or parts.query or parts.fragment:
        raise ConfigError(_UNUSABLE)
    if not sep:
        return Address(server, None, key)
    user, _, password = userinfo.partition(":")
    return Address(server, httpx.BasicAuth(unquote(user), unquote(password)), key)


_UNUSABLE = (
    "ARR_METADATA_URL is not an address this app can use: check its host name and "
    "port, e.g. http://arr-metadata-server:8080"
)


class Issued(NamedTuple):
    """The key this app gives the server, and the setting it came from."""

    value: str | None = None
    source: str | None = None


def issued_key(address: Address, api_key: str | None) -> Issued:
    """The key this app gives the server: ``ARR_METADATA_API_KEY``, or its address's."""
    if address.key and api_key and address.key != api_key:
        raise ConfigError(
            "ARR_METADATA_URL carries one key and ARR_METADATA_API_KEY another: keep one "
            "of them."
        )
    if api_key:
        return Issued(api_key, "ARR_METADATA_API_KEY")
    if address.key:
        return Issued(address.key, "ARR_METADATA_URL")
    return Issued()


def key_headers(api_key: str | None) -> dict[str, str]:
    """The header carrying a key issued by the server, when there is one.

    The server takes it there as it takes it from Plex's address. A key no
    header can carry is refused here, without quoting it: the HTTP library's
    own complaint quotes it whole, and would carry it into every title's
    recorded error and the doctor's report.
    """
    if not api_key:
        return {}
    if not all(ch.isascii() and ch.isprintable() and not ch.isspace() for ch in api_key):
        raise ConfigError(
            "The key this app gives arr-metadata-server -- ARR_METADATA_API_KEY, or the "
            "one at the end of ARR_METADATA_URL -- holds a space, an accent or a control "
            "character, which no key it issues does: copy it again from its Keys & network "
            "page."
        )
    return {"X-Api-Key": api_key}


def work_key(external_id: ExternalId, kind: str) -> str:
    """The provider's key for a work, from an id Plex or a binding gave."""
    scheme, value = external_id.scheme, external_id.value.strip()
    if scheme == "ams" and WORK_KEY.match(value):
        if not value.startswith(f"{kind}-"):
            raise ProviderNotFound(f"ams: {value} is not a {_NOUN[kind]}")
        return value
    if scheme == "tmdb" and _NUMBER.match(value):
        return f"{kind}-tmdb-{value}"
    if scheme == "tvdb" and _NUMBER.match(value) and kind == "show":
        return f"show-tvdb-{value}"
    if scheme == "imdb" and _IMDB.match(value) and kind == "movie":
        return f"movie-imdb-{value}"
    raise ProviderNotFound(f"ams: a {_NOUN[kind]} is not looked up by {external_id}")


def agent_guid(raw: str) -> ExternalId | None:
    """The id in a GUID arr-metadata-server's Plex agent gave an item, if it is one."""
    scheme, sep, rest = raw.partition("://")
    if not sep or scheme.strip().lower() != AGENT_SCHEME:
        return None
    key = rest.split("?", 1)[0].rsplit("/", 1)[-1].strip()
    return ExternalId("ams", key) if WORK_KEY.match(key) else None


def _entries(payload: Any) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    container = payload.get("MediaContainer")
    metadata = container.get("Metadata") if isinstance(container, dict) else None
    return [entry for entry in metadata or [] if isinstance(entry, dict)]


def _genres(entry: dict) -> list[str]:
    """The catalogue's genres as it names them.

    Not split on " & ": the server already splits the compound genres TMDB
    uses, and keeps whole a name somebody locked, "Sword & Sorcery" say.
    """
    out: list[str] = []
    seen: set[str] = set()
    for tag in entry.get("Genre") or []:
        name = str(tag.get("tag") or "").strip() if isinstance(tag, dict) else ""
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            out.append(name)
    return out


def _ids_of(entry: dict) -> dict[str, str]:
    ids: dict[str, str] = {}
    for guid in entry.get("Guid") or []:
        parsed = ExternalId.parse(str(guid.get("id") or "")) if isinstance(guid, dict) else None
        if parsed is not None:
            ids.setdefault(parsed.scheme, parsed.value)
    return ids


def _shares_an_id(entry: dict, ids: list[ExternalId]) -> bool:
    held = _ids_of(entry)
    return any(held.get(e.scheme) == e.value for e in ids)


def _same_work(entry: dict, request: LookupRequest) -> bool:
    """The same title -- in either form the server gives -- from a year either side.

    Both years are needed: without the item's, a home video named like a film
    would take the film's genres, and keep the film in the catalogue.
    """
    wanted = _folded(request.title)
    named = {_folded(str(entry.get(field) or "")) for field in ("title", "originalTitle")}
    year = _year_of(entry)
    return (
        bool(wanted) and wanted in named
        and request.year is not None and year is not None and abs(year - request.year) <= 1
    )


def _folded(text: str) -> str:
    """A title as the server compares them: no case, no accents, letters and digits alone."""
    return "".join(ch for ch in unicodedata.normalize("NFKD", text.casefold()) if ch.isalnum())


def _score(entry: dict) -> float | None:
    """TMDB's audience rating where the work has one, else IMDb's."""
    ratings = [r for r in entry.get("Rating") or [] if isinstance(r, dict)]
    for source in ("themoviedb://", "imdb://"):
        for rating in ratings:
            if str(rating.get("image") or "").startswith(source):
                return score_of(rating.get("value"))
    return None


def _page(entry: dict, kind: str) -> str | None:
    """The work's page on the site of the id the provider lists first for it."""
    ids = _ids_of(entry)
    for scheme in _MATCHED_BY[kind]:
        if scheme in ids:
            segment = "movie" if kind == "movie" else "tv"
            return _PAGES[scheme].format(segment=segment, value=quote(ids[scheme], safe=""))
    return None


def _year_of(entry: dict) -> int | None:
    year = entry.get("year")
    return year if isinstance(year, int) and not isinstance(year, bool) else None


def _location(response: httpx.Response) -> str:
    """Where a redirect points, without any credentials written into it."""
    target = response.headers.get("location") or "?"
    parts = urlsplit(target)
    return urlunsplit(parts._replace(netloc=parts.netloc.rpartition("@")[2]))


def manifest_problem(base_url: str, api_key: str | None, *, timeout: float = 5.0) -> str | None:
    """Why the provider at ``base_url`` cannot be used from here, or None.

    Asks the manifest -- what Plex reads first when the provider is added -- so
    an address that is refused, a provider switched off and a server that is
    not one are told apart before a run finds out title by title.
    """
    try:
        address = split_address(base_url)
        key = issued_key(address, api_key)
        headers = key_headers(key.value)
    except ConfigError as exc:
        return str(exc)
    server = address.server
    url = f"{server}/plex"
    try:
        response = httpx.get(url, timeout=timeout, auth=address.auth, headers=headers)
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        return f"{url} cannot be reached: {exc}"
    if response.is_redirect:
        return (
            f"{url} redirects to {_location(response)}. Set ARR_METADATA_URL to the "
            "address it redirects to: a redirected search would not reach the provider."
        )
    if response.status_code in (401, 403):
        return refusal(response.status_code, _code_of(response), key_from=key.source)
    if response.status_code == 421:
        return (
            f"The server does not answer to the name in {server}: it lists the names "
            "it answers to in AMS_ALLOWED_HOSTS. Add this one there, or use one it lists."
        )
    if response.status_code == 503 and _code_of(response) == "surface_disabled":
        return (
            "Its Plex provider is switched off: turn it on under Keys & network "
            "(the setting api.plex)."
        )
    if response.status_code != 200:
        return f"{url} answered HTTP {response.status_code}."
    try:
        identifier = response.json()["MediaProvider"]["identifier"]
    except (ValueError, KeyError, TypeError):
        identifier = None
    if identifier != AGENT_SCHEME:
        return f"{url} does not answer as arr-metadata-server's Plex provider."
    return None


def _code_of(response: httpx.Response) -> str | None:
    """The code the server gives a refusal: ``{"error": "surface_disabled", …}``."""
    try:
        body = response.json()
    except ValueError:
        return None
    return body.get("error") if isinstance(body, dict) else None
