"""Domain types shared by the providers, the Plex layer and the pipeline.

These are deliberately plain dataclasses rather than pydantic models: they are
internal wire types, not user-facing configuration, and they are created in hot
loops. The pydantic models live in :mod:`plex_auto_genres.config`.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from enum import Enum


class MediaType(str, Enum):
    """The three library flavours the tool knows how to process."""

    ANIME = "anime"
    STANDARD_TV = "standard-tv"
    STANDARD_MOVIE = "standard-movie"

    @property
    def is_movie(self) -> bool:
        return self is MediaType.STANDARD_MOVIE

    @property
    def is_anime(self) -> bool:
        return self is MediaType.ANIME


class TagField(str, Enum):
    """Which Plex tag field a run writes into."""

    GENRE = "genre"
    COLLECTION = "collection"


def fold(name: str) -> str:
    """A comparison key for a genre or tag: what two spellings share.

    Case, accents, punctuation and spacing all go, so "Rock 'n' Roll" and
    "Rock n Roll" stop becoming two collections in Plex, and a rename rule
    written as "sci-fi" matches "Sci Fi" as well. Two words that genuinely
    differ -- "Comedy" and "Comedie" -- still need a rename rule to meet.

    Letters are whatever the writing system calls letters: an earlier cut kept
    ASCII alphanumerics alone, which folded every Japanese, Cyrillic, Greek or
    Korean genre to the empty string and dropped it -- so a library reading
    TMDB in one of those languages lost every genre it was given.
    """
    decomposed = unicodedata.normalize("NFKD", name.casefold())
    kept = "".join(c for c in decomposed if c.isalnum() and not unicodedata.combining(c))
    # Recompose: NFKD splits Hangul into jamo, and a key nobody can read is a
    # key nobody can debug. Latin letters lost their accents above and stay put.
    return unicodedata.normalize("NFKC", kept)


#: Provider id schemes we can read straight out of a Plex GUID, in the order we
#: prefer them. ``mal`` and ``anilist`` are exact anime matches; ``anidb`` needs
#: the offline mapping table; the rest are for standard libraries.
KNOWN_GUID_SCHEMES = ("mal", "anilist", "anidb", "tmdb", "tvdb", "imdb")


@dataclass(frozen=True, slots=True)
class ExternalId:
    """An id from a metadata provider, e.g. ``tmdb://1234``."""

    scheme: str
    value: str

    def __str__(self) -> str:
        return f"{self.scheme}://{self.value}"

    @classmethod
    def parse(cls, raw: str) -> "ExternalId | None":
        """Parse a Plex GUID string. Returns ``None`` for unknown shapes."""
        if "://" not in raw:
            return None
        scheme, _, value = raw.partition("://")
        scheme = scheme.strip().lower()
        # Plex appends things like '?lang=en' and HAMA uses 'anidb://1234/5'.
        value = value.split("?", 1)[0].split("/", 1)[0].strip()
        if not scheme or not value:
            return None
        return cls(scheme, value)


@dataclass(slots=True)
class MediaItem:
    """A single Plex library entry, flattened into what the pipeline needs.

    ``plexapi`` objects are lazily-reloading proxies; snapshotting the fields we
    care about up front keeps the hot loop free of surprise HTTP round trips.
    """

    rating_key: int
    title: str
    year: int | None
    guids: list[ExternalId] = field(default_factory=list)
    current_genres: list[str] = field(default_factory=list)
    current_collections: list[str] = field(default_factory=list)
    #: Fields Plex reports as locked against agent refreshes (``genre``, ...),
    #: so an undo can put the lock back the way it was.
    locked_fields: set[str] = field(default_factory=set)
    #: Plex poster path, e.g. ``/library/metadata/123/thumb/456``.
    thumb: str | None = None
    #: The live plexapi object, kept so the writer can edit it.
    handle: object | None = None

    @property
    def identifier(self) -> str:
        """Stable, human-readable key. Matches the v1 progress-file format."""
        return f"{self.title} ({self.year})" if self.year else self.title

    def find_id(self, scheme: str) -> ExternalId | None:
        return next((g for g in self.guids if g.scheme == scheme), None)

    def current_tags(self, field_: TagField) -> list[str]:
        if field_ is TagField.GENRE:
            return self.current_genres
        return self.current_collections


@dataclass(frozen=True, slots=True)
class ManualTags:
    """What a person decided about one item's genres (or collections).

    Three instructions, in the vocabulary Kometa uses for the same job:

    * ``added`` -- always written, whatever the sources say, and even when
      they have nothing for the title;
    * ``removed`` -- never written, and taken off the item if it is there;
    * ``locked`` -- the sources are no longer asked about this item. For
      genres the list is then exactly ``added``; collections are never
      cleared, since they also hold the ones people make, so a locked
      collection item gets ``added`` and loses ``removed`` and nothing else.

    Names are kept as the person gave them, collection prefix included when
    they picked one from Plex: the writer works out the spelling to write.
    Deleting the override hands the item back to the sources.
    """

    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    locked: bool = False
    note: str | None = None
    updated_at: float = 0.0
    #: The item's name when this was decided: a list of overrides can then
    #: say more than ``tmdb://1234``, and the CLI can find it again by name.
    title: str | None = None

    @property
    def decides(self) -> bool:
        """Whether this says anything at all; an empty one is no override."""
        return bool(self.added or self.removed or self.locked)

    def stamp(self, fingerprint: str) -> str:
        """The cache fingerprint of an item carrying this decision.

        The decision is part of what produced a cached result, so a run that
        applied an older one -- or none -- must not count as done. Folding it
        into the fingerprint, rather than deleting the cached row on every
        save, is what keeps a decision saved while a run is under way from
        being lost when that run records the item a moment later. The note
        and the title are left out: editing them changes nothing written.
        """
        if not self.decides:
            return fingerprint
        payload = json.dumps([self.added, self.removed, self.locked], ensure_ascii=False)
        return f"{fingerprint}+{hashlib.sha256(payload.encode()).hexdigest()[:12]}"

    def as_dict(self) -> dict:
        """Plain JSON types, for the API and ``manuals --json``."""
        out = asdict(self)
        out["added"], out["removed"] = list(self.added), list(self.removed)
        return out


#: The longest tag name a decision accepts.
MAX_NAME_LENGTH = 120


def unique_names(names: Iterable[str]) -> list[str]:
    """First spelling of each folded name, in order; blanks dropped."""
    out: list[str] = []
    seen: set[str] = set()
    for name in names:
        key = fold(name)
        if key and key not in seen:
            seen.add(key)
            out.append(name)
    return out


def clean_names(names: Iterable[str]) -> list[str]:
    """Names typed by a person, ready to store: stripped, deduplicated.

    Refuses rather than drops a name the rest of the app would lose: one
    with no letter or digit folds to nothing, and the genre rules discard
    those, so a lock made of one would quietly become an empty lock.
    """
    cleaned = [n.strip() for n in names if n and n.strip()]
    for name in cleaned:
        if len(name) > MAX_NAME_LENGTH:
            raise ValueError(f"{name[:40]!r}... is longer than {MAX_NAME_LENGTH} characters")
        if not fold(name):
            raise ValueError(f"{name!r} has no letter or digit to match a tag by")
    return unique_names(cleaned)


def bare_name(name: str, prefix: str) -> str:
    """``name`` without the collection prefix the app writes, if it has it."""
    if prefix and name.startswith(prefix) and name != prefix:
        return name[len(prefix):]
    return name


def check_decision(added: Iterable[str], removed: Iterable[str], prefix: str = "") -> None:
    """Refuse a name that is both always and never written.

    Compared the way the writer matches tags, prefix or not: "PAG-Action"
    added and "Action" refused name one tag, and one of them would have to
    lose without a word.
    """
    refused = {fold(bare_name(n, prefix)) for n in removed}
    clash = next((n for n in added if fold(bare_name(n, prefix)) in refused), None)
    if clash is not None:
        raise ValueError(f"{clash!r} is both added and removed; keep one")


@dataclass(slots=True)
class ProviderResult:
    """Normalised metadata returned by any provider."""

    provider: str
    provider_id: str
    title: str
    genres: list[str] = field(default_factory=list)
    #: 0-10 scale, matching what Plex's ``rate()`` expects.
    score: float | None = None
    url: str | None = None
    #: How the record was found: ``binding`` | ``guid`` | ``search``. Set by
    #: :meth:`Provider.resolve`; stored with the cache entry so the UI reports
    #: what actually happened rather than guessing from the GUIDs.
    matched_by: str = "search"


@dataclass(slots=True)
class Candidate:
    """One possible match a provider offers for a title, for a human to pick."""

    provider: str
    provider_id: str
    title: str
    year: int | None = None
    url: str | None = None
    image: str | None = None
    synopsis: str | None = None
    score: float | None = None
    genres: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "provider": self.provider,
            "provider_id": self.provider_id,
            "title": self.title,
            "year": self.year,
            "url": self.url,
            "image": self.image,
            "synopsis": self.synopsis,
            "score": self.score,
            "genres": list(self.genres),
        }


@dataclass(slots=True)
class ItemOutcome:
    """What happened to one media item during a run."""

    item: MediaItem
    status: str  # "written" | "unchanged" | "skipped" | "deferred" | "failed"
    genres: list[str] = field(default_factory=list)
    provider: str | None = None
    provider_id: str | None = None
    error: str | None = None
    retryable: bool = True
    #: Written from a decision alone, with no source answering for it.
    by_hand: bool = False


@dataclass(slots=True)
class RunReport:
    """Aggregate result of processing one library."""

    run_id: str
    library: str
    action: str
    dry_run: bool = False
    written: int = 0
    unchanged: int = 0
    skipped: int = 0
    failed: int = 0
    #: Titles no source could answer for. Not failures: nothing is known about
    #: them yet, nothing was cached, and the next run retries them at once.
    deferred: int = 0
    plex_requests: int = 0
    provider_requests: int = 0
    duration_s: float = 0.0
    failures: list[tuple[str, str]] = field(default_factory=list)
    #: True when the run was cancelled before every item was processed.
    cancelled: bool = False
    #: Set when the action as a whole could not proceed (Plex unreachable,
    #: a missing sortedPrefix, ...). Item-level failures go in ``failures``.
    error: str | None = None

    @property
    def total(self) -> int:
        return self.written + self.unchanged + self.skipped + self.failed + self.deferred

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "library": self.library,
            "action": self.action,
            "dry_run": self.dry_run,
            "written": self.written,
            "unchanged": self.unchanged,
            "skipped": self.skipped,
            "failed": self.failed,
            "deferred": self.deferred,
            "total": self.total,
            "plex_requests": self.plex_requests,
            "provider_requests": self.provider_requests,
            "duration_s": round(self.duration_s, 2),
            "failures": self.failures[:50],
            "cancelled": self.cancelled,
            "error": self.error,
        }
