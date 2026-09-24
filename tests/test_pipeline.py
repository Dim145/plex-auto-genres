"""End-to-end pipeline behaviour against a fake Plex server and mocked HTTP."""

from __future__ import annotations

import httpx
import respx

from plex_auto_genres.config import AppConfig
from plex_auto_genres.pipeline import GIVE_UP_AFTER, Pipeline, rating_bucket
from plex_auto_genres import providers as providers_module
from plex_auto_genres.providers import UNHEALTHY_AFTER
from plex_auto_genres.providers.anidb_map import MAPPING_URL
from plex_auto_genres.providers.base import HttpTransport
from plex_auto_genres.ratelimit import LimitSpec
from plex_auto_genres.models import ExternalId
from plex_auto_genres.store import Store

from .conftest import FakePlexItem, FakeTag


class FakeSection:
    def __init__(self, key, items, collections=()):
        self.key = key
        self.title = "Animes"
        self._items = items
        self._collections = list(collections)
        self.totalSize = len(items)

    def all(self):
        return self._items

    def collections(self):
        return self._collections


class FakeLibrary:
    def __init__(self, section):
        self._section = section

    def section(self, name):
        return self._section

    def sections(self):
        return [self._section]


class FakeServer:
    def __init__(self, items, collections=()):
        self._section = FakeSection(1, items, collections)
        self.library = FakeLibrary(self._section)

    def fetchItems(self, ekey, params=None, container_size=None):
        return self._section.all()

    def fetchItem(self, rating_key):
        return next(i for i in self._section.all() if i.ratingKey == int(rating_key))


def make_config(concurrency: int = 4, tmdb_key: str | None = None, **library_kwargs) -> AppConfig:
    base = {"library": "Animes", "type": "anime", "useGenres": True}
    base.update(library_kwargs)
    return AppConfig.model_validate({
        "version": 2,
        "defaults": {"anime": {"ignore": ["Kids"], "replace": {"sci-fi": "science fiction"}}},
        "libraries": [base],
        "providers": {"concurrency": concurrency, "tmdb_api_key": tmdb_key},
    })


def unmetered(monkeypatch, *names: str) -> None:
    """Take a provider's published pacing out of a test that is not about it."""
    for name in names:
        monkeypatch.setitem(providers_module._LIMITS, name, LimitSpec(((1000, 1.0),)))


async def _no_backoff(_attempt: int) -> None:
    """Skip the real 5xx wait; this test is about which source gets asked."""


def anilist_ok(genres=("Action", "Adventure")) -> httpx.Response:
    return httpx.Response(200, json={"data": {"Media": {
        "id": 1, "idMal": 1, "title": {"romaji": "Anime"}, "genres": list(genres),
        "tags": [], "averageScore": 80, "startDate": {"year": 1998},
        "siteUrl": "https://anilist.co/anime/1",
    }}})


def jikan_ok(mal_id=1, genres=("Action", "Kids", "Sci-Fi"), score=8.0):
    return httpx.Response(200, json={"data": {
        "mal_id": mal_id, "title": "Anime", "score": score,
        "genres": [{"name": g} for g in genres],
    }})


@respx.mock
async def test_happy_path_writes_filtered_genres_in_one_request(store: Store):
    respx.get(url__regex=r"https://api\.jikan\.moe/v4/anime/\d+").mock(
        return_value=jikan_ok()
    )
    handle = FakePlexItem(1, "Cowboy Bebop", 1998, genres=("Old",))
    handle.guids = [type("G", (), {"id": "mal://1"})()]
    server = FakeServer([handle])

    config = make_config(clearGenres=True)
    report = await Pipeline(config, store, server).tag_library(config.libraries[0])

    assert report.written == 1 and report.failed == 0
    assert handle.last_tags == ["Action", "science fiction"]   # Kids ignored, Sci-Fi renamed
    assert report.plex_requests == 1, "one write per item regardless of genre count"


@respx.mock
async def test_second_run_is_a_no_op_thanks_to_the_cache(store: Store):
    route = respx.get(url__regex=r"https://api\.jikan\.moe/v4/anime/\d+").mock(
        return_value=jikan_ok()
    )
    handle = FakePlexItem(1, "Cowboy Bebop", 1998)
    handle.guids = [type("G", (), {"id": "mal://1"})()]
    server = FakeServer([handle])
    config = make_config()

    first = await Pipeline(config, store, server).tag_library(config.libraries[0])
    second = await Pipeline(config, store, server).tag_library(config.libraries[0])

    assert first.written == 1
    assert second.skipped == 1 and second.written == 0
    assert route.call_count == 1, "the provider is not called again"


@respx.mock
async def test_changing_settings_forces_a_reprocess(store: Store):
    respx.get(url__regex=r"https://api\.jikan\.moe/v4/anime/\d+").mock(
        return_value=jikan_ok()
    )
    handle = FakePlexItem(1, "Cowboy Bebop", 1998)
    handle.guids = [type("G", (), {"id": "mal://1"})()]
    server = FakeServer([handle])

    await Pipeline(make_config(), store, server).tag_library(make_config().libraries[0])

    changed = make_config(clearGenres=True)      # different fingerprint
    report = await Pipeline(changed, store, server).tag_library(changed.libraries[0])
    assert report.skipped == 0


@respx.mock
async def test_dry_run_writes_nothing_and_caches_nothing(store: Store):
    respx.get(url__regex=r"https://api\.jikan\.moe/v4/anime/\d+").mock(
        return_value=jikan_ok()
    )
    handle = FakePlexItem(1, "Cowboy Bebop", 1998)
    handle.guids = [type("G", (), {"id": "mal://1"})()]
    server = FakeServer([handle])
    config = make_config()

    report = await Pipeline(config, store, server, dry_run=True).tag_library(config.libraries[0])

    assert report.written == 1          # reports the intent
    assert handle.edits == []           # but changed nothing
    assert store.get_state("Animes", "mal://1") is None


@respx.mock
async def test_a_failing_item_does_not_stop_the_others(store: Store):
    """v1's bare `except Exception` around the whole loop aborted the run."""
    respx.get("https://api.jikan.moe/v4/anime/1").mock(return_value=httpx.Response(404))
    respx.get("https://api.jikan.moe/v4/anime/2").mock(return_value=jikan_ok(2))

    good = FakePlexItem(2, "Good", 2000)
    good.guids = [type("G", (), {"id": "mal://2"})()]
    bad = FakePlexItem(1, "Bad", 1999)
    bad.guids = [type("G", (), {"id": "mal://1"})()]
    server = FakeServer([bad, good])

    config = make_config()
    report = await Pipeline(config, store, server).tag_library(config.libraries[0])

    assert report.written == 1 and report.failed == 1
    assert good.edits, "the healthy item was still processed"
    assert store.get_state("Animes", "mal://1").status == "failed"


@respx.mock
async def test_a_manual_binding_overrides_the_guid(store: Store):
    respx.get("https://api.jikan.moe/v4/anime/19").mock(
        return_value=jikan_ok(19, genres=("Psychological",))
    )
    wrong = respx.get("https://api.jikan.moe/v4/anime/1")

    handle = FakePlexItem(1, "Monster", 2004)
    handle.guids = [type("G", (), {"id": "mal://1"})()]   # the wrong auto-match
    server = FakeServer([handle])

    store.set_binding("Animes", "mal://1", "mal", "19")
    config = make_config()
    report = await Pipeline(config, store, server).tag_library(config.libraries[0])

    assert report.written == 1
    assert handle.last_tags == ["Psychological"]
    assert not wrong.called


@respx.mock
async def test_undo_restores_the_previous_tags(store: Store):
    from plex_auto_genres.plexsvc.writer import undo_run

    respx.get(url__regex=r"https://api\.jikan\.moe/v4/anime/\d+").mock(
        return_value=jikan_ok()
    )
    handle = FakePlexItem(1, "Cowboy Bebop", 1998, genres=("Original", "Tags"))
    handle.guids = [type("G", (), {"id": "mal://1"})()]
    server = FakeServer([handle])

    config = make_config(clearGenres=True)
    report = await Pipeline(config, store, server).tag_library(config.libraries[0])
    assert handle.last_tags == ["Action", "science fiction"]

    restored, skipped = undo_run(server, store, report.run_id)
    assert (restored, skipped) == (1, 0)
    assert handle.last_tags == ["Original", "Tags"]


@respx.mock
async def test_all_genres_filtered_out_counts_as_a_failure(store: Store):
    respx.get(url__regex=r"https://api\.jikan\.moe/v4/anime/\d+").mock(
        return_value=jikan_ok(genres=("Kids",))    # the only genre is ignored
    )
    handle = FakePlexItem(1, "Kids Show", 2001)
    handle.guids = [type("G", (), {"id": "mal://1"})()]
    server = FakeServer([handle])

    config = make_config()
    report = await Pipeline(config, store, server).tag_library(config.libraries[0])
    assert report.failed == 1
    # The source answered: say the rules dropped its names, not that it had none.
    assert "your ignore and replace rules dropped every one" in report.failures[0][1]


@respx.mock
async def test_falls_back_to_search_when_there_is_no_guid(store: Store):
    search = respx.get("https://api.jikan.moe/v4/anime").mock(
        return_value=httpx.Response(200, json={"data": [{
            "mal_id": 7, "title": "Cowboy Bebop", "year": 1998,
            "genres": [{"name": "Action"}],
        }]})
    )
    handle = FakePlexItem(1, "Cowboy Bebop", 1998)
    handle.guids = []
    server = FakeServer([handle])

    config = make_config()
    report = await Pipeline(config, store, server).tag_library(config.libraries[0])
    assert report.written == 1
    assert search.called


def test_rating_buckets():
    assert rating_bucket(9.4) == "5 Star Rating"
    assert rating_bucket(0.5) == "1 Star Rating"
    assert rating_bucket(0) is None
    assert rating_bucket(None) is None
    assert rating_bucket("nonsense") is None


# -- review regressions: run rows always close, caches are reused ----------------


import json  # noqa: E402

import pytest  # noqa: E402

from plex_auto_genres.errors import PlexConnectionError  # noqa: E402
from plex_auto_genres.plexsvc.writer import undo_run  # noqa: E402


class FakeCollection:
    def __init__(self, rating_key, title, title_sort=""):
        self.ratingKey, self.title, self.titleSort = rating_key, title, title_sort
        self.edits: list[dict] = []

    def edit(self, **kwargs):
        self.edits.append(kwargs)
        self.titleSort = kwargs.get("titleSort.value", self.titleSort)
        return self


class ServerWithCollections(FakeServer):
    def fetchItem(self, rating_key):
        for collection in self._section.collections():
            if collection.ratingKey == int(rating_key):
                return collection
        return super().fetchItem(rating_key)


def guid_item(rating_key=1, title="Cowboy Bebop", year=1998, genres=("Old",)) -> FakePlexItem:
    handle = FakePlexItem(rating_key, title, year, genres=genres)
    handle.guids = [type("G", (), {"id": f"mal://{rating_key}"})()]
    return handle


@respx.mock
async def test_a_plex_refusal_during_ratings_closes_the_run_row(store: Store):
    respx.get(url__regex=r"https://api\.jikan\.moe/v4/anime/\d+").mock(return_value=jikan_ok())
    handle = guid_item()

    def refuse(rating=None):
        raise RuntimeError("Plex said no")

    handle.rate = refuse
    config = make_config(rateAnime=True)
    run = config.libraries[0]

    report = await Pipeline(config, store, FakeServer([handle])).rate_library(run)

    assert report.failed == 1 and report.written == 0
    assert "Plex said no" in report.failures[0][1]
    assert store.get_run(report.run_id)["finished_at"] is not None


async def test_a_plex_failure_before_the_item_loop_still_closes_the_row(store: Store):
    from plexapi.exceptions import NotFound

    class GoneLibrary:
        def section(self, name):
            raise NotFound("gone")

        def sections(self):
            return []

    server = FakeServer([])
    server.library = GoneLibrary()
    config = make_config()

    with pytest.raises(PlexConnectionError):
        await Pipeline(config, store, server).tag_library(config.libraries[0])

    row = store.recent_runs(1, "Animes")[0]
    assert row["finished_at"] is not None
    assert "No Plex library" in json.loads(row["report"])["error"]


async def test_sort_without_a_prefix_reports_instead_of_raising(store: Store):
    server = ServerWithCollections([], collections=[FakeCollection(1, "Action")])
    config = make_config(sortCollections=True)   # the anime defaults set no sortedPrefix

    report = await Pipeline(config, store, server).sort(config.libraries[0])

    assert report.error and "sortedPrefix" in report.error
    assert store.get_run(report.run_id)["finished_at"] is not None


async def test_missing_poster_directory_is_reported_not_raised(store: Store, tmp_path):
    config = make_config(setPosters=True)
    report = await Pipeline(config, store, FakeServer([])).set_posters(
        config.libraries[0], str(tmp_path / "nope")
    )
    assert report.error and "not found" in report.error
    assert store.get_run(report.run_id)["finished_at"] is not None


async def test_sort_matches_prefixed_collections_and_is_undoable(store: Store):
    action = FakeCollection(101, "PAG-Action")
    server = ServerWithCollections([], collections=[action])
    config = AppConfig.model_validate({
        "version": 2,
        "defaults": {"anime": {"sortedPrefix": "*", "sortedCollections": ["action"]}},
        "libraries": [{"library": "Animes", "type": "anime", "sortCollections": True}],
        "plex": {"collection_prefix": "PAG-"},
    })

    report = await Pipeline(config, store, server).sort(config.libraries[0])
    assert (report.written, report.skipped) == (1, 0)
    assert action.titleSort == "*PAG-Action"

    assert undo_run(server, store, report.run_id) == (1, 0)
    assert action.titleSort == "" and action.edits[-1]["titleSort.locked"] == 0


@respx.mock
async def test_v1_progress_rows_are_adopted_under_the_guid_key(store: Store):
    handle = guid_item()
    config = make_config()
    run = config.libraries[0]
    # Exactly what import_legacy_logs writes: keyed by "Title (Year)".
    store.record_success(
        "Animes", "Cowboy Bebop (1998)", fingerprint=config.fingerprint(run),
        title="Cowboy Bebop", year=1998, rating_key=None, genres=[], provider=None,
        provider_id=None,
    )

    report = await Pipeline(config, store, FakeServer([handle])).tag_library(run)

    assert (report.skipped, report.written) == (1, 0)   # no provider route was needed
    assert store.get_state("Animes", "mal://1") is not None
    assert store.get_state("Animes", "Cowboy Bebop (1998)") is None


@respx.mock
async def test_ratings_come_from_the_cache_after_a_tag_run(store: Store):
    route = respx.get(url__regex=r"https://api\.jikan\.moe/v4/anime/\d+").mock(
        return_value=jikan_ok(score=8.0)
    )
    handle = guid_item()
    config = make_config(rateAnime=True)
    run = config.libraries[0]
    pipeline = Pipeline(config, store, FakeServer([handle]))

    await pipeline.tag_library(run)
    assert route.call_count == 1 and handle.ratings == [8.0]

    handle.userRating = 8.0                       # what Plex now reports
    report = await pipeline.rate_library(run)
    assert route.call_count == 1, "the cached score was used; no provider call"
    assert report.unchanged == 1 and handle.ratings == [8.0], "an equal rating is not re-sent"


@respx.mock
async def test_rating_collections_fall_back_to_the_cached_provider_score(store: Store):
    respx.get(url__regex=r"https://api\.jikan\.moe/v4/anime/\d+").mock(
        return_value=jikan_ok(score=8.0)
    )
    handle = guid_item()                          # no Plex rating on the item
    config = make_config(createRatingCollections=True)
    run = config.libraries[0]
    pipeline = Pipeline(config, store, FakeServer([handle]))

    await pipeline.tag_library(run)
    report = await pipeline.rating_collections(run)

    assert report.written == 1 and handle.last_tags == ["4 Star Rating"]


@respx.mock
async def test_a_provider_auth_failure_falls_through_to_the_next_provider(store: Store):
    respx.post("https://graphql.anilist.co").mock(return_value=httpx.Response(403))
    respx.get(url__regex=r"https://api\.jikan\.moe/v4/anime/\d+").mock(return_value=jikan_ok())
    handle = guid_item()
    config = make_config(providers=["anilist", "jikan"])

    report = await Pipeline(config, store, FakeServer([handle])).tag_library(config.libraries[0])

    assert report.written == 1 and report.failed == 0
    assert store.get_state("Animes", "mal://1").source == "guid"


@respx.mock
async def test_an_anime_library_can_fall_back_to_tmdb(store: Store):
    """The last resort: nothing on MAL, so TMDB's TV entry answers instead."""
    respx.get(url__regex=r"https://api\.jikan\.moe/v4/anime/\d+").mock(
        return_value=httpx.Response(404)
    )
    respx.get(url__regex=r"https://api\.jikan\.moe/v4/anime\b").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    respx.get("https://api.themoviedb.org/3/search/tv").mock(
        return_value=httpx.Response(200, json={
            "results": [{"id": 7, "name": "Cowboy Bebop", "first_air_date": "1998-04-03"}]
        })
    )
    respx.get("https://api.themoviedb.org/3/tv/7").mock(
        return_value=httpx.Response(200, json={
            "id": 7, "name": "Cowboy Bebop",
            "genres": [{"name": "Animation"}, {"name": "Sci-Fi & Fantasy"}],
        })
    )
    handle = guid_item(title="Cowboy Bebop", year=1998)
    config = AppConfig.model_validate({
        "version": 2,
        "defaults": {"anime": {"ignore": ["Kids"]}},
        "libraries": [{"library": "Animes", "type": "anime", "useGenres": True,
                       "clearGenres": True, "providers": ["jikan", "tmdb"]}],
        "providers": {"tmdb_api_key": "k"},
    })

    report = await Pipeline(config, store, FakeServer([handle])).tag_library(config.libraries[0])

    assert (report.written, report.failed) == (1, 0)
    assert handle.last_tags == ["Animation", "Sci-Fi", "Fantasy"]
    assert store.get_state("Animes", "mal://1").provider == "tmdb"


# -- when a source stops answering ----------------------------------------


@respx.mock
async def test_a_rate_limited_title_is_deferred_not_cached_as_a_failure(store: Store):
    """The v2.0 bug behind a big anime library coming back with 268 failures.

    Every source refusing to answer used to be raised as "not found", which
    cached a failure row and hid the title behind an hour of backoff. It is a
    missing answer, not an answer, so nothing is written down.
    """
    respx.get(url__regex=r"https://api\.jikan\.moe/v4/anime/\d+").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "0"})
    )
    config = make_config()
    server = FakeServer([guid_item()])

    report = await Pipeline(config, store, server).tag_library(config.libraries[0])

    assert (report.deferred, report.failed, report.written) == (1, 0, 0)
    assert store.get_state("Animes", "mal://1") is None, "nothing cached"

    # ... so the next run picks it straight back up instead of skipping it.
    again = await Pipeline(config, store, server).tag_library(config.libraries[0])
    assert again.deferred == 1 and again.skipped == 0


@respx.mock
async def test_a_run_gives_up_once_nothing_is_answering(store: Store, monkeypatch):
    """Better to stop and say so than to grind a whole library against a dead API."""
    unmetered(monkeypatch, "jikan")
    respx.get(url__regex=r"https://api\.jikan\.moe/v4/anime/\d+").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "0"})
    )
    items = [guid_item(i, f"Title {i}") for i in range(1, GIVE_UP_AFTER + 21)]
    config = make_config(concurrency=1)

    report = await Pipeline(config, store, FakeServer(items)).tag_library(config.libraries[0])

    assert report.error and "next run" in report.error
    assert report.deferred >= GIVE_UP_AFTER
    assert not store.states_for_library("Animes"), "and left no trace to skip next time"
    # Five titles pay three attempts each to establish that the source is
    # down. After that it is left alone: no amount of library is worth more
    # requests to an API that is refusing every one of them.
    assert report.provider_requests == UNHEALTHY_AFTER * 3


@respx.mock
async def test_a_source_that_stops_answering_stands_down_for_the_fallback(
    store: Store, monkeypatch
):
    """Once Jikan is clearly down, the rest of the library goes straight to AniList."""
    unmetered(monkeypatch, "jikan", "anilist")
    monkeypatch.setattr(HttpTransport, "_sleep_backoff", staticmethod(_no_backoff))
    jikan = respx.get(url__regex=r"https://api\.jikan\.moe/v4/anime/\d+").mock(
        return_value=httpx.Response(504)
    )
    respx.post("https://graphql.anilist.co").mock(return_value=anilist_ok())

    items = [guid_item(i, f"Title {i}") for i in range(1, 11)]
    config = make_config(providers=["jikan", "anilist"], concurrency=1)

    report = await Pipeline(config, store, FakeServer(items)).tag_library(config.libraries[0])

    assert report.written == 10 and report.deferred == 0
    # Five titles pay for the discovery, at three attempts each; the other
    # five skip Jikan entirely instead of spending 3 more calls apiece.
    assert jikan.call_count == UNHEALTHY_AFTER * 3


@respx.mock
async def test_a_pinned_binding_is_tried_before_the_configured_order(store: Store):
    """A binding names an id scheme, so the provider that answers it goes first.

    Sorting by provider *name* against a scheme left the configured order
    untouched, so a pinned TMDB id was reached only if MyAnimeList happened to
    miss first.
    """
    jikan = respx.get(url__regex=r"https://api\.jikan\.moe/v4/anime\b")
    respx.get("https://api.themoviedb.org/3/tv/7").mock(
        return_value=httpx.Response(200, json={
            "id": 7, "name": "Anime", "genres": [{"name": "Animation"}],
        })
    )
    handle = guid_item()
    store.set_binding("Animes", "mal://1", "tmdb", "7")
    config = make_config(tmdb_key="k", providers=["jikan", "tmdb"], clearGenres=True)

    report = await Pipeline(config, store, FakeServer([handle])).tag_library(config.libraries[0])

    assert report.written == 1 and handle.last_tags == ["Animation"]
    assert not jikan.called, "the pinned source answered, so nothing else was asked"


@respx.mock
async def test_a_pinned_binding_survives_its_source_standing_down(store: Store, monkeypatch):
    """Standing a source down must not quietly override a hand-picked match."""
    unmetered(monkeypatch, "jikan", "anilist")
    monkeypatch.setattr(HttpTransport, "_sleep_backoff", staticmethod(_no_backoff))
    pinned = respx.get("https://api.jikan.moe/v4/anime/777").mock(return_value=jikan_ok(777))
    respx.get(url__regex=r"https://api\.jikan\.moe/v4/anime/\d+").mock(
        return_value=httpx.Response(504)
    )
    respx.post("https://graphql.anilist.co").mock(return_value=anilist_ok())

    items = [guid_item(i, f"Title {i}") for i in range(1, 11)]
    store.set_binding("Animes", "mal://10", "mal", "777")   # the last one to be handled
    config = make_config(providers=["jikan", "anilist"], concurrency=1)

    report = await Pipeline(config, store, FakeServer(items)).tag_library(config.libraries[0])

    assert report.written == 10
    assert pinned.called, "the pinned source is still asked when its turn comes"


@respx.mock
async def test_a_source_standing_down_does_not_turn_real_misses_into_deferrals(
    store: Store, monkeypatch
):
    """A verdict from any source settles the title, even while another is down.

    Treating "somebody was silent" as enough to defer meant that, for the whole
    of a stand-down, a title AniList genuinely had no record of was never
    cached -- and counted toward giving up on the run.
    """
    unmetered(monkeypatch, "jikan", "anilist")
    monkeypatch.setattr(HttpTransport, "_sleep_backoff", staticmethod(_no_backoff))
    respx.get(url__regex=r"https://api\.jikan\.moe/v4/anime/\d+").mock(
        return_value=httpx.Response(504)
    )
    respx.post("https://graphql.anilist.co").mock(return_value=httpx.Response(404))

    items = [guid_item(i, f"Title {i}") for i in range(1, 11)]
    config = make_config(providers=["jikan", "anilist"], concurrency=1)

    report = await Pipeline(config, store, FakeServer(items)).tag_library(config.libraries[0])

    assert (report.failed, report.deferred) == (10, 0)
    assert report.error is None, "a library of genuine misses is not an outage"
    assert store.get_state("Animes", "mal://10").status == "failed"


@respx.mock
async def test_a_half_healthy_library_is_not_given_up_on(store: Store, monkeypatch):
    """Deferrals return at once while writes queue for Plex, so completion
    order bunches them together. Counting a run of them abandoned a library
    that was resolving half its titles perfectly well."""
    unmetered(monkeypatch, "jikan")
    monkeypatch.setattr(HttpTransport, "_sleep_backoff", staticmethod(_no_backoff))
    respx.get(url__regex=r"https://api\.jikan\.moe/v4/anime/\d*[13579]$").mock(
        return_value=httpx.Response(504)
    )
    respx.get(url__regex=r"https://api\.jikan\.moe/v4/anime/\d+").mock(return_value=jikan_ok())

    items = [guid_item(i, f"Title {i}") for i in range(1, 61)]
    config = make_config()

    report = await Pipeline(config, store, FakeServer(items)).tag_library(config.libraries[0])

    assert report.error is None, "half the library was resolving fine"
    assert report.written > 0 and report.written + report.deferred == len(items)


# -- bindings whose id scheme no provider speaks natively -------------------


@respx.mock
async def test_an_anidb_binding_reaches_jikan_as_the_mal_id_it_maps_to(store: Store):
    """The picker offers AniDB ids, and they were stored and then ignored.

    Nothing claims the ``anidb`` scheme, so the pin never applied: the item
    fell through to its Plex GUID or a title search, with no error anywhere.
    """
    respx.get(MAPPING_URL).mock(
        return_value=httpx.Response(200, json=[{"anidb_id": 4521, "mal_id": 19}])
    )
    pinned = respx.get("https://api.jikan.moe/v4/anime/19").mock(
        return_value=jikan_ok(19, genres=("Psychological",))
    )
    from_the_guid = respx.get("https://api.jikan.moe/v4/anime/1")

    handle = guid_item()                      # Plex says mal://1; the user says otherwise
    store.set_binding("Animes", "mal://1", "anidb", "4521")
    config = make_config(clearGenres=True)

    report = await Pipeline(config, store, FakeServer([handle])).tag_library(config.libraries[0])

    assert report.written == 1 and handle.last_tags == ["Psychological"]
    assert pinned.called and not from_the_guid.called
    assert store.get_state("Animes", "mal://1").source == "binding"


@respx.mock
async def test_an_imdb_binding_is_honoured_through_tmdb(store: Store):
    """Film libraries are offered IMDb ids, which only TMDB can resolve."""
    respx.get("https://api.themoviedb.org/3/find/tt0133093").mock(
        return_value=httpx.Response(200, json={"movie_results": [{"id": 603}]})
    )
    respx.get("https://api.themoviedb.org/3/movie/603").mock(
        return_value=httpx.Response(200, json={
            "id": 603, "title": "Film", "genres": [{"name": "Action"}]})
    )
    search = respx.get("https://api.themoviedb.org/3/search/movie")

    handle = FakePlexItem(1, "Film", 1999, genres=("Old",))
    store.set_binding("Films", "Film (1999)", "imdb", "tt0133093")
    config = AppConfig.model_validate({
        "version": 2,
        "libraries": [{"library": "Films", "type": "standard-movie",
                       "useGenres": True, "clearGenres": True}],
        "providers": {"tmdb_api_key": "k"},
    })

    report = await Pipeline(config, store, FakeServer([handle])).tag_library(config.libraries[0])

    assert report.written == 1 and handle.last_tags == ["Action"]
    assert not search.called, "the pinned id settled it, no title search"
    assert store.get_state("Films", "Film (1999)").source == "binding"


@respx.mock
async def test_an_anime_library_can_take_tmdb_keywords_from_its_fallback(store: Store):
    """Keywords became reachable for anime the moment TMDB joined the chain.

    They are TMDB's own concept, so they apply to exactly the titles TMDB
    answered for; the anime sources above it are untouched.
    """
    respx.get(url__regex=r"https://api\.jikan\.moe/v4/anime/\d+").mock(
        return_value=httpx.Response(404)
    )
    respx.get(url__regex=r"https://api\.jikan\.moe/v4/anime\b").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    respx.get("https://api.themoviedb.org/3/search/tv").mock(
        return_value=httpx.Response(200, json={
            "results": [{"id": 7, "name": "Anime", "first_air_date": "1998-04-03"}]})
    )
    respx.get("https://api.themoviedb.org/3/tv/7").mock(
        return_value=httpx.Response(200, json={
            "id": 7, "name": "Anime",
            "genres": [{"name": "Animation"}],
            "keywords": {"results": [{"name": "space western"}, {"name": "bounty hunter"}]},
        })
    )
    handle = guid_item(title="Anime", year=1998)
    config = AppConfig.model_validate({
        "version": 2,
        "libraries": [{"library": "Animes", "type": "anime", "useGenres": True,
                       "clearGenres": True, "useKeywords": True,
                       "providers": ["jikan", "tmdb"]}],
        "providers": {"tmdb_api_key": "k"},
    })

    report = await Pipeline(config, store, FakeServer([handle])).tag_library(config.libraries[0])

    assert report.written == 1
    assert handle.last_tags == ["space western", "bounty hunter"], "keywords, not genres"


# -- merging several sources instead of falling back -----------------------


@respx.mock
async def test_merge_mode_pools_what_every_source_returned(store: Store):
    """Fallback keeps the first answer; merge asks everyone and pools them."""
    respx.get(url__regex=r"https://api\.jikan\.moe/v4/anime/\d+").mock(
        return_value=jikan_ok(1, genres=("Action",))
    )
    respx.post("https://graphql.anilist.co").mock(return_value=anilist_ok(("Drama",)))

    handle = guid_item()
    config = make_config(providers=["jikan", "anilist"], providerMode="merge", clearGenres=True)

    report = await Pipeline(config, store, FakeServer([handle])).tag_library(config.libraries[0])

    assert report.written == 1
    assert handle.last_tags == ["Action", "Drama"]
    assert store.get_state("Animes", "mal://1").provider == "jikan+anilist"


@respx.mock
async def test_merge_mode_still_answers_when_one_source_is_silent(store: Store):
    respx.get(url__regex=r"https://api\.jikan\.moe/v4/anime/\d+").mock(
        return_value=httpx.Response(404)
    )
    respx.get(url__regex=r"https://api\.jikan\.moe/v4/anime\b").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    respx.post("https://graphql.anilist.co").mock(return_value=anilist_ok(("Drama",)))

    handle = guid_item()
    config = make_config(providers=["jikan", "anilist"], providerMode="merge", clearGenres=True)

    report = await Pipeline(config, store, FakeServer([handle])).tag_library(config.libraries[0])

    assert (report.written, report.deferred, report.failed) == (1, 0, 0)
    assert handle.last_tags == ["Drama"]


@respx.mock
async def test_merge_mode_leaves_a_pinned_title_to_the_source_that_was_pinned(store: Store):
    """Otherwise the sources that cannot honour the pin search by title and
    merge back the very match the binding was created to override."""
    respx.get("https://api.jikan.moe/v4/anime/19").mock(
        return_value=jikan_ok(19, genres=("Psychological",))
    )
    tmdb_search = respx.get("https://api.themoviedb.org/3/search/tv")

    handle = guid_item()
    store.set_binding("Animes", "mal://1", "mal", "19")
    config = make_config(tmdb_key="k", providers=["jikan", "tmdb"],
                         providerMode="merge", clearGenres=True)

    report = await Pipeline(config, store, FakeServer([handle])).tag_library(config.libraries[0])

    assert report.written == 1 and handle.last_tags == ["Psychological"]
    assert not tmdb_search.called, "TMDB cannot read a MAL id, so it is not asked"


@respx.mock
async def test_two_pinned_ids_let_a_merge_use_both_catalogues(store: Store):
    """The case one pin per item could not express: a series that exists on
    AniList and on TMDB, merged from the exact record on each."""
    respx.post("https://graphql.anilist.co").mock(return_value=anilist_ok(("Drama",)))
    respx.get("https://api.themoviedb.org/3/tv/7").mock(
        return_value=httpx.Response(200, json={
            "id": 7, "name": "Anime", "genres": [{"name": "Animation"}]})
    )
    searches = respx.get("https://api.themoviedb.org/3/search/tv")

    handle = guid_item()
    store.set_binding("Animes", "mal://1", "anilist", "21")
    store.set_binding("Animes", "mal://1", "tmdb", "7")
    config = make_config(tmdb_key="k", providers=["anilist", "tmdb"],
                         providerMode="merge", clearGenres=True)

    report = await Pipeline(config, store, FakeServer([handle])).tag_library(config.libraries[0])

    assert report.written == 1 and handle.last_tags == ["Drama", "Animation"]
    assert not searches.called, "each source was asked by the id it was handed"
    assert store.get_state("Animes", "mal://1").provider == "anilist+tmdb"


@respx.mock
async def test_a_pin_is_never_overridden_by_another_source_guessing(store: Store, monkeypatch):
    """The protection existed only in merge mode, leaving the default one
    free to write back the very match the binding was created to override."""
    unmetered(monkeypatch, "jikan", "anilist")
    monkeypatch.setattr(HttpTransport, "_sleep_backoff", staticmethod(_no_backoff))
    respx.get("https://api.jikan.moe/v4/anime/19").mock(return_value=httpx.Response(504))
    anilist = respx.post("https://graphql.anilist.co").mock(return_value=anilist_ok(("Wrong",)))

    handle = guid_item()
    store.set_binding("Animes", "mal://1", "mal", "19")
    config = make_config(providers=["jikan", "anilist"], concurrency=1)

    report = await Pipeline(config, store, FakeServer([handle])).tag_library(config.libraries[0])

    # AniList reads MAL ids too, so it is asked -- by id, never by title.
    assert anilist.called
    body = anilist.calls[0].request.content.decode()
    assert '"malId": 19' in body or '"malId":19' in body
    assert report.written + report.deferred == 1


@respx.mock
async def test_a_pin_no_source_can_read_says_so(store: Store):
    """A chain edited after the fact leaves the pin stranded; resolving the
    title by search anyway would be the bug the pin exists to prevent."""
    respx.get(url__regex=r"https://api\.jikan\.moe/v4/anime\b").mock(return_value=jikan_ok())

    handle = guid_item()
    store.set_binding("Animes", "mal://1", "tmdb", "7")   # tmdb is not in the chain
    config = make_config(providers=["jikan"])

    report = await Pipeline(config, store, FakeServer([handle])).tag_library(config.libraries[0])

    assert report.failed == 1 and report.written == 0
    assert "tmdb://7" in report.failures[0][1] and "jikan" in report.failures[0][1]


@respx.mock
async def test_a_source_with_nothing_to_say_does_not_stop_the_chain(store: Store):
    """A result carrying no names must not end the chain before the sources
    below it are asked."""
    respx.post("https://graphql.anilist.co").mock(
        return_value=httpx.Response(200, json={"data": {"Media": {
            "id": 1, "idMal": 1, "title": {"romaji": "Anime"}, "genres": [],
            "tags": [{"name": "Faint", "rank": 10, "isGeneralSpoiler": False}],
            "averageScore": 80, "startDate": {"year": 1998},
            "siteUrl": "https://anilist.co/anime/1",
        }}})
    )
    respx.get("https://api.themoviedb.org/3/search/tv").mock(
        return_value=httpx.Response(200, json={"results": [{"id": 7, "name": "Anime"}]})
    )
    respx.get("https://api.themoviedb.org/3/tv/7").mock(
        return_value=httpx.Response(200, json={
            "id": 7, "name": "Anime", "genres": [{"name": "Animation"}],
            "keywords": {"results": [{"name": "space western"}]}})
    )
    handle = guid_item()
    config = make_config(tmdb_key="k", providers=["anilist", "tmdb"],
                         useKeywords=True, clearGenres=True)

    report = await Pipeline(config, store, FakeServer([handle])).tag_library(config.libraries[0])

    assert report.written == 1 and handle.last_tags == ["space western"]


@respx.mock
async def test_a_merge_missing_a_source_is_deferred_not_cached(store: Store, monkeypatch):
    """Caching it would freeze the title on a subset of its sources for good."""
    unmetered(monkeypatch, "jikan", "anilist")
    monkeypatch.setattr(HttpTransport, "_sleep_backoff", staticmethod(_no_backoff))
    respx.get(url__regex=r"https://api\.jikan\.moe/v4/anime/\d+").mock(return_value=jikan_ok())
    respx.post("https://graphql.anilist.co").mock(return_value=httpx.Response(504))

    handle = guid_item()
    config = make_config(providers=["jikan", "anilist"], providerMode="merge")

    report = await Pipeline(config, store, FakeServer([handle])).tag_library(config.libraries[0])

    assert (report.deferred, report.written) == (1, 0)
    assert store.get_state("Animes", "mal://1") is None, "nothing cached, so it completes later"


def test_a_merged_result_takes_its_rating_from_the_source_that_names_it():
    """Identity and score must come from one record: a rating beside another
    catalogue's id sends anyone auditing it to the wrong entry."""
    from plex_auto_genres.models import ProviderResult
    from plex_auto_genres.pipeline import merge_results

    first = ProviderResult(provider="jikan", provider_id="1", title="A",
                           genres=["Action"], score=None)
    second = ProviderResult(provider="tmdb", provider_id="99", title="A",
                            genres=["Animation"], score=8.4)

    merged = merge_results([first, second])
    assert merged.provider_id == "1" and merged.score is None
    assert merged.genres == ["Action", "Animation"]


# -- genres decided by hand ------------------------------------------------


@respx.mock
async def test_manual_additions_and_removals_layer_over_the_sources(store: Store):
    respx.get(url__regex=r"https://api\.jikan\.moe/v4/anime/\d+").mock(
        return_value=jikan_ok(1, genres=("Action", "Drama"))
    )
    handle = guid_item()
    store.set_manual("Animes", "mal://1", added=["Space Opera"], removed=["Drama"],
                     locked=False)
    config = make_config(clearGenres=True)

    report = await Pipeline(config, store, FakeServer([handle])).tag_library(config.libraries[0])

    assert report.written == 1
    assert handle.last_tags == ["Action", "Space Opera"]
    # The sources were still asked: only what the person decided is fixed.
    assert store.get_state("Animes", "mal://1").provider == "jikan"


@respx.mock
async def test_a_manual_removal_takes_off_a_tag_already_in_plex(store: Store):
    """Without clearGenres the writer merges into what Plex holds, so a removal
    has to reach into that too, or it would never take anything off."""
    respx.get(url__regex=r"https://api\.jikan\.moe/v4/anime/\d+").mock(
        return_value=jikan_ok(1, genres=("Action",))
    )
    handle = guid_item(genres=("Drama", "Old"))
    store.set_manual("Animes", "mal://1", added=[], removed=["Old"], locked=False)
    config = make_config()                           # merge, not replace

    await Pipeline(config, store, FakeServer([handle])).tag_library(config.libraries[0])

    assert handle.last_tags == ["Drama", "Action"]


@respx.mock
async def test_a_locked_item_is_never_asked_about(store: Store):
    """Locked means decided: no request goes out, and the list is exact."""
    jikan = respx.get(url__regex=r"https://api\.jikan\.moe/v4/anime\b")
    handle = guid_item(genres=("Whatever", "Was", "There"))
    store.set_manual("Animes", "mal://1", added=["Mecha", "Drama"], removed=[], locked=True)
    config = make_config()                           # merge would keep the old tags

    report = await Pipeline(config, store, FakeServer([handle])).tag_library(config.libraries[0])

    assert not jikan.called
    assert report.written == 1 and handle.last_tags == ["Mecha", "Drama"]
    state = store.get_state("Animes", "mal://1")
    assert (state.provider, state.source) == ("manual", "manual")


@respx.mock
async def test_a_lock_never_clears_a_libraries_collections(store: Store):
    """Collections also hold the ones people build by hand; a lock replacing
    the whole field would have wiped them along with the genre ones."""
    jikan = respx.get(url__regex=r"https://api\.jikan\.moe/v4/anime\b")
    handle = guid_item(genres=())
    handle.collections = [FakeTag("My Favourites")]
    store.set_manual("Animes", "mal://1", added=["Mecha"], removed=[], locked=True)
    config = make_config(useGenres=False)

    await Pipeline(config, store, FakeServer([handle])).tag_library(config.libraries[0])

    assert not jikan.called
    assert handle.last_tags == ["My Favourites", "Mecha"]


@respx.mock
async def test_a_name_copied_from_plex_is_matched_without_its_prefix(store: Store):
    """The console offers the item's collections as Plex spells them, prefix
    and all; the writer adds the prefix itself, so it has to come off first."""
    respx.get(url__regex=r"https://api\.jikan\.moe/v4/anime/\d+").mock(
        return_value=jikan_ok(1, genres=("Drama", "Action"))
    )
    handle = guid_item(genres=())
    handle.collections = [FakeTag("PAG-Action"), FakeTag("My Favourites")]
    store.set_manual("Animes", "mal://1", added=["PAG-Mecha"], removed=["PAG-Action"],
                     locked=False)
    config = AppConfig.model_validate({
        "version": 2,
        "libraries": [{"library": "Animes", "type": "anime", "useGenres": False}],
        "plex": {"collection_prefix": "PAG-"},
    })

    await Pipeline(config, store, FakeServer([handle])).tag_library(config.libraries[0])

    assert handle.last_tags == ["My Favourites", "PAG-Drama", "PAG-Mecha"]


@respx.mock
async def test_an_empty_lock_means_no_tags_at_all(store: Store):
    handle = guid_item(genres=("Wrong", "Also Wrong"))
    store.set_manual("Animes", "mal://1", added=[], removed=[], locked=True)
    config = make_config()

    report = await Pipeline(config, store, FakeServer([handle])).tag_library(config.libraries[0])

    assert report.written == 1 and handle.last_tags == []


@respx.mock
async def test_manual_additions_are_not_cut_by_the_genre_cap(store: Store):
    """maxGenres shapes what the sources return; an explicit decision is not that."""
    respx.get(url__regex=r"https://api\.jikan\.moe/v4/anime/\d+").mock(
        return_value=jikan_ok(1, genres=("Action", "Drama", "Comedy"))
    )
    handle = guid_item()
    store.set_manual("Animes", "mal://1", added=["Space Opera"], removed=[], locked=False)
    config = make_config(clearGenres=True, overrides={"maxGenres": 1})

    await Pipeline(config, store, FakeServer([handle])).tag_library(config.libraries[0])

    assert handle.last_tags == ["Action", "Space Opera"]


# -- decisions by hand: what the review found ------------------------------


def _no_source_knows_it() -> None:
    respx.get(url__regex=r"https://api\.jikan\.moe/v4/anime/\d+").mock(
        return_value=httpx.Response(404)
    )
    respx.get(url__regex=r"https://api\.jikan\.moe/v4/anime\b").mock(
        return_value=httpx.Response(200, json={"data": []})
    )


def _prefixed(use_genres: bool) -> AppConfig:
    return AppConfig.model_validate({
        "version": 2,
        "libraries": [{"library": "Animes", "type": "anime", "useGenres": use_genres}],
        "plex": {"collection_prefix": "PAG-"},
    })


@respx.mock
async def test_a_refusal_that_leaves_nothing_still_takes_the_tag_off(store: Store):
    """Raising "nothing left" before the write meant the refused tag stayed
    on the item for good, and the item sat in failure backoff."""
    respx.get(url__regex=r"https://api\.jikan\.moe/v4/anime/\d+").mock(
        return_value=jikan_ok(1, genres=("Drama",))
    )
    handle = guid_item(genres=("Drama", "Old"))
    store.set_manual("Animes", "mal://1", added=[], removed=["Drama"], locked=False)
    config = make_config()                           # merge, not replace

    report = await Pipeline(config, store, FakeServer([handle])).tag_library(config.libraries[0])

    assert (report.written, report.failed) == (1, 0)
    assert handle.last_tags == ["Old"]
    assert store.get_state("Animes", "mal://1").status == "ok"


@respx.mock
async def test_a_refusal_that_leaves_nothing_clears_a_replacing_library(store: Store):
    respx.get(url__regex=r"https://api\.jikan\.moe/v4/anime/\d+").mock(
        return_value=jikan_ok(1, genres=("Drama",))
    )
    handle = guid_item(genres=("Drama",))
    store.set_manual("Animes", "mal://1", added=[], removed=["Drama"], locked=False)
    config = make_config(clearGenres=True)

    report = await Pipeline(config, store, FakeServer([handle])).tag_library(config.libraries[0])

    assert report.written == 1 and handle.last_tags == []


@respx.mock
async def test_rules_that_drop_everything_still_fail_where_a_refusal_would_wipe(store: Store):
    """With clearGenres, writing the empty list the rules left would wipe the
    item over a refusal that had nothing to do with it."""
    respx.get(url__regex=r"https://api\.jikan\.moe/v4/anime/\d+").mock(
        return_value=jikan_ok(1, genres=("Kids",))    # ignored by the defaults
    )
    handle = guid_item(genres=("Drama",))
    store.set_manual("Animes", "mal://1", added=[], removed=["Horror"], locked=False)
    config = make_config(clearGenres=True)

    report = await Pipeline(config, store, FakeServer([handle])).tag_library(config.libraries[0])

    assert report.failed == 1 and handle.edits == []


@respx.mock
async def test_additions_stand_when_no_source_knows_the_title(store: Store):
    """Always write meant "whatever the sources say" -- and was dropped exactly
    when they had nothing, because the lookup failed first."""
    _no_source_knows_it()
    handle = guid_item(genres=("Old",))
    store.set_manual("Animes", "mal://1", added=["Mecha"], removed=[], locked=False)
    config = make_config(clearGenres=True)

    report = await Pipeline(config, store, FakeServer([handle])).tag_library(config.libraries[0])

    assert (report.written, report.failed) == (1, 0)
    assert handle.last_tags == ["Old", "Mecha"], "merged: no answer to replace the tags with"
    state = store.get_state("Animes", "mal://1")
    assert (state.status, state.source) == ("ok", "manual")


@respx.mock
async def test_refused_names_do_not_use_up_a_capped_slot(store: Store):
    respx.get(url__regex=r"https://api\.jikan\.moe/v4/anime/\d+").mock(
        return_value=jikan_ok(1, genres=("Action", "Drama", "Comedy"))
    )
    handle = guid_item()
    store.set_manual("Animes", "mal://1", added=[], removed=["Action"], locked=False)
    config = make_config(clearGenres=True, overrides={"maxGenres": 2})

    await Pipeline(config, store, FakeServer([handle])).tag_library(config.libraries[0])

    assert handle.last_tags == ["Drama", "Comedy"]


@respx.mock
async def test_a_decision_saved_while_a_run_is_under_way_is_not_lost(store: Store):
    """The run that was going records the item under the settings it started
    with; the decision in the fingerprint is what makes that row stale."""
    respx.get(url__regex=r"https://api\.jikan\.moe/v4/anime/\d+").mock(
        return_value=jikan_ok(1, genres=("Action", "Drama"))
    )
    handle = guid_item()
    config = make_config(clearGenres=True)
    run = config.libraries[0]
    store.set_manual("Animes", "mal://1", added=["Mecha"], removed=[], locked=True)
    # What a run that started before the save writes a moment later.
    store.record_success("Animes", "mal://1", fingerprint=config.fingerprint(run),
                         title="Cowboy Bebop", year=1998, rating_key=1,
                         genres=["Action", "Drama"], provider="jikan", provider_id="1")

    report = await Pipeline(config, store, FakeServer([handle])).tag_library(run)

    assert report.skipped == 0 and handle.last_tags == ["Mecha"]


@respx.mock
async def test_a_decision_handed_back_during_a_run_goes_back_to_the_sources(store: Store):
    jikan = respx.get(url__regex=r"https://api\.jikan\.moe/v4/anime/\d+").mock(
        return_value=jikan_ok(1, genres=("Action", "Drama"))
    )
    handle = guid_item()
    config = make_config(clearGenres=True)
    run = config.libraries[0]
    lock = store.set_manual("Animes", "mal://1", added=["Mecha"], removed=[], locked=True)
    store.record_success("Animes", "mal://1", fingerprint=lock.stamp(config.fingerprint(run)),
                         title="Cowboy Bebop", year=1998, rating_key=1, genres=["Mecha"],
                         provider="manual", provider_id="", source="manual")
    store.delete_manual("Animes", "mal://1")

    await Pipeline(config, store, FakeServer([handle])).tag_library(run)

    assert jikan.called and handle.last_tags == ["Action", "Drama"]


@respx.mock
async def test_a_decision_made_before_a_v1_librarys_first_run_applies(store: Store):
    """The imported "Title (Year)" row was promoted onto the item's key and
    counted as done, and a decision the CLI filed under that key was lost."""
    respx.get(url__regex=r"https://api\.jikan\.moe/v4/anime/\d+").mock(return_value=jikan_ok())
    handle = guid_item()                             # "Cowboy Bebop (1998)", mal://1
    config = make_config(clearGenres=True)
    run = config.libraries[0]
    store.record_success("Animes", "Cowboy Bebop (1998)", fingerprint=config.fingerprint(run),
                         title="Cowboy Bebop", year=1998, rating_key=1, genres=["Old"],
                         provider="jikan", provider_id="1")
    store.set_manual("Animes", "Cowboy Bebop (1998)", added=["Mecha"], removed=[], locked=True)

    await Pipeline(config, store, FakeServer([handle])).tag_library(run)

    assert handle.last_tags == ["Mecha"]
    assert list(store.manual_for_library("Animes")) == ["mal://1"]


@respx.mock
async def test_a_note_edit_does_not_send_the_item_back_to_the_sources(store: Store):
    jikan = respx.get(url__regex=r"https://api\.jikan\.moe/v4/anime/\d+").mock(
        return_value=jikan_ok(1, genres=("Action",))
    )
    handle = guid_item()
    store.set_manual("Animes", "mal://1", added=["Mecha"], removed=[], locked=False, note="a")
    config = make_config()
    run = config.libraries[0]
    await Pipeline(config, store, FakeServer([handle])).tag_library(run)
    calls = jikan.call_count

    store.set_manual("Animes", "mal://1", added=["Mecha"], removed=[], locked=False, note="b")
    report = await Pipeline(config, store, FakeServer([handle])).tag_library(run)

    assert report.skipped == 1 and jikan.call_count == calls


@respx.mock
async def test_a_lock_keeps_the_score_its_source_gave(store: Store):
    """The stand-in answer had no score, so every ratings run went back to the
    sources for a locked item, and rating collections skipped it."""
    jikan = respx.get(url__regex=r"https://api\.jikan\.moe/v4/anime/\d+").mock(
        return_value=jikan_ok(1, score=8.0)
    )
    handle = guid_item()
    config = make_config(createRatingCollections=True)
    run = config.libraries[0]
    pipeline = Pipeline(config, store, FakeServer([handle]))
    await pipeline.tag_library(run)
    store.set_manual("Animes", "mal://1", added=["Mecha"], removed=[], locked=True)
    await pipeline.tag_library(run)

    state = store.get_state("Animes", "mal://1")
    assert (state.provider, state.score, state.source) == ("jikan", 8.0, "manual")
    before = jikan.call_count
    await pipeline.rate_library(run)
    assert jikan.call_count == before, "the ratings pass reads the score it kept"
    assert (await pipeline.rating_collections(run)).written == 1


@respx.mock
async def test_rating_collections_leave_a_refused_collection_off(store: Store):
    respx.get(url__regex=r"https://api\.jikan\.moe/v4/anime/\d+").mock(
        return_value=jikan_ok(1, genres=("Action",), score=8.0)
    )
    handle = guid_item(genres=())
    config = make_config(useGenres=False, createRatingCollections=True)
    run = config.libraries[0]
    pipeline = Pipeline(config, store, FakeServer([handle]))
    await pipeline.tag_library(run)
    store.set_manual("Animes", "mal://1", added=[], removed=["4 Star Rating"], locked=False)

    report = await pipeline.rating_collections(run)

    assert report.written == 0 and "4 Star Rating" not in handle.last_tags


def test_items_decided_by_hand_prove_nothing_about_the_sources():
    from plex_auto_genres.models import RunReport

    report = RunReport(run_id="r", library="Animes", action="genres", dry_run=False)
    report.deferred, report.written = GIVE_UP_AFTER, 1
    assert Pipeline._nothing_is_answering(report, None, by_hand=1) is True
    assert Pipeline._nothing_is_answering(report, None) is False


@respx.mock
async def test_a_lock_keeps_the_spelling_each_name_has_on_the_item(store: Store):
    """Seeded from the item, a lock re-prefixed Plex's own "Drama" into
    "PAG-Drama"; a name already on the item keeps its spelling now."""
    handle = guid_item(genres=("PAG-Action", "Drama"))
    store.set_manual("Animes", "mal://1", added=["PAG-Action", "Drama", "Mecha"], removed=[],
                     locked=True)
    config = _prefixed(use_genres=True)

    await Pipeline(config, store, FakeServer([handle])).tag_library(config.libraries[0])

    assert handle.last_tags == ["PAG-Action", "Drama", "PAG-Mecha"]


@respx.mock
async def test_a_collections_lock_seeded_from_the_item_duplicates_nothing(store: Store):
    handle = guid_item(genres=())
    handle.collections = [FakeTag("PAG-Action"), FakeTag("My Favourites"),
                          FakeTag("4 Star Rating")]
    store.set_manual("Animes", "mal://1", added=["PAG-Action", "My Favourites", "4 Star Rating"],
                     removed=[], locked=True)
    config = _prefixed(use_genres=False)

    report = await Pipeline(config, store, FakeServer([handle])).tag_library(config.libraries[0])

    assert report.unchanged == 1 and handle.edits == []


@respx.mock
async def test_a_refusal_reaches_a_tag_the_app_did_not_write(store: Store):
    """With a prefix set, only "PAG-Kids" was ever matched: Plex's own "Kids"
    stayed, though the console showed it struck through."""
    respx.get(url__regex=r"https://api\.jikan\.moe/v4/anime/\d+").mock(
        return_value=jikan_ok(1, genres=("Action",))
    )
    handle = guid_item(genres=("Kids", "PAG-Action"))
    store.set_manual("Animes", "mal://1", added=[], removed=["Kids"], locked=False)
    config = _prefixed(use_genres=True)

    await Pipeline(config, store, FakeServer([handle])).tag_library(config.libraries[0])

    assert handle.last_tags == ["PAG-Action"]


@respx.mock
async def test_a_collections_lock_can_still_refuse(store: Store):
    """A collections lock never clears, so its refusals are how one comes off."""
    handle = guid_item(genres=())
    handle.collections = [FakeTag("PAG-Action"), FakeTag("PAG-Kids")]
    store.set_manual("Animes", "mal://1", added=["Mecha"], removed=["PAG-Kids"], locked=True)
    config = _prefixed(use_genres=False)

    await Pipeline(config, store, FakeServer([handle])).tag_library(config.libraries[0])

    assert handle.last_tags == ["PAG-Action", "PAG-Mecha"]


@respx.mock
async def test_a_pin_filed_with_a_v1_row_moves_with_it_and_applies(store: Store):
    """A pin filed under a v1 key follows the row onto the GUID key when the
    row is adopted, and is used in that same run."""
    pinned = respx.get("https://api.jikan.moe/v4/anime/19").mock(
        return_value=jikan_ok(19, genres=("Psychological",))
    )
    handle = guid_item()                             # "Cowboy Bebop (1998)", mal://1
    config = make_config(clearGenres=True)
    store.record_success("Animes", "Cowboy Bebop (1998)", fingerprint=config.fingerprint(
        config.libraries[0]), title="Cowboy Bebop", year=1998, rating_key=None,
        genres=["Old"], provider=None, provider_id=None)          # as v1's import leaves it
    store.set_binding("Animes", "Cowboy Bebop (1998)", "mal", "19")

    await Pipeline(config, store, FakeServer([handle])).tag_library(config.libraries[0])

    assert pinned.called and handle.last_tags == ["Psychological"]
    assert [str(pin) for pin in store.get_bindings("Animes", "mal://1")] == ["mal://19"]



@respx.mock
async def test_a_pin_saved_while_a_run_is_under_way_is_applied(store: Store):
    """Binding deleted the cached row, and the run that was going wrote it back
    a moment later under the old fingerprint: every later run skipped the item."""
    pinned = respx.get("https://api.jikan.moe/v4/anime/19").mock(
        return_value=jikan_ok(19, genres=("Psychological",))
    )
    handle = guid_item()
    config = make_config(clearGenres=True)
    run = config.libraries[0]
    store.set_binding("Animes", "mal://1", "mal", "19")
    store.record_success("Animes", "mal://1", fingerprint=config.fingerprint(run),
                         title="Cowboy Bebop", year=1998, rating_key=1, genres=["Action"],
                         provider="jikan", provider_id="1")

    report = await Pipeline(config, store, FakeServer([handle])).tag_library(run)

    assert report.skipped == 0 and pinned.called and handle.last_tags == ["Psychological"]


@respx.mock
async def test_a_pin_removed_during_a_run_goes_back_to_the_guid(store: Store):
    from plex_auto_genres.models import stamp_pins

    guid = respx.get("https://api.jikan.moe/v4/anime/1").mock(
        return_value=jikan_ok(1, genres=("Action",))
    )
    handle = guid_item()
    config = make_config(clearGenres=True)
    run = config.libraries[0]
    store.set_binding("Animes", "mal://1", "mal", "19")
    store.record_success("Animes", "mal://1", fingerprint=stamp_pins(
        config.fingerprint(run), ["mal://19"]), title="Cowboy Bebop", year=1998, rating_key=1,
        genres=["Psychological"], provider="jikan", provider_id="19", source="binding")
    store.delete_binding("Animes", "mal://1")

    await Pipeline(config, store, FakeServer([handle])).tag_library(run)

    assert guid.called and handle.last_tags == ["Action"]


@respx.mock
async def test_an_items_own_key_is_never_taken_for_v1_data(store: Store):
    """A GUID-less item is keyed by "Title (Year)", the same string a matched
    item of that name has as its identifier: its pin must stay its own."""
    respx.get("https://api.jikan.moe/v4/anime/1").mock(return_value=jikan_ok(1, genres=("Drama",)))
    respx.get("https://api.jikan.moe/v4/anime/19").mock(
        return_value=jikan_ok(19, genres=("Psychological",))
    )
    matched = guid_item(rating_key=1, title="Monster", year=2004)
    loose = FakePlexItem(2, "Monster", 2004)                   # no GUID: keyed by its name
    loose.guids = []
    config = make_config(clearGenres=True)
    store.record_success("Animes", "Monster (2004)", fingerprint="old", title="Monster",
                         year=2004, rating_key=2, genres=[], provider=None, provider_id=None)
    store.set_binding("Animes", "Monster (2004)", "mal", "19")

    await Pipeline(config, store, FakeServer([matched, loose])).tag_library(config.libraries[0])

    assert matched.last_tags == ["Drama"] and loose.last_tags == ["Psychological"]
    assert store.get_bindings("Animes", "mal://1") == []
    assert store.get_bindings("Animes", "Monster (2004)") == [ExternalId("mal", "19")]


@respx.mock
async def test_a_name_two_items_share_adopts_nothing(store: Store):
    respx.get(url__regex=r"https://api\.jikan\.moe/v4/anime/\d+").mock(return_value=jikan_ok())
    one, two = guid_item(1, "Monster", 2004), guid_item(2, "Monster", 2004)
    config = make_config()
    store.record_success("Animes", "Monster (2004)", fingerprint=config.fingerprint(
        config.libraries[0]), title="Monster", year=2004, rating_key=None, genres=["Old"],
        provider=None, provider_id=None)

    report = await Pipeline(config, store, FakeServer([one, two])).tag_library(config.libraries[0])

    assert report.written == 2, "neither item is the v1 row's, so both are resolved"
    assert store.get_state("Animes", "Monster (2004)") is not None, "and the row stays put"


@respx.mock
async def test_a_dry_run_adopts_nothing(store: Store):
    respx.get(url__regex=r"https://api\.jikan\.moe/v4/anime/\d+").mock(return_value=jikan_ok())
    handle = guid_item()
    config = make_config()
    store.record_success("Animes", "Cowboy Bebop (1998)", fingerprint="stale", title="Cowboy Bebop",
                         year=1998, rating_key=None, genres=["Old"], provider=None,
                         provider_id=None)

    await Pipeline(config, store, FakeServer([handle]), dry_run=True).tag_library(
        config.libraries[0])

    assert store.get_state("Animes", "Cowboy Bebop (1998)") is not None
    assert store.get_state("Animes", "mal://1") is None


@respx.mock
async def test_ratings_do_not_reuse_a_score_from_before_the_pin(store: Store):
    """A pin changes which record the score is from."""
    respx.get("https://api.jikan.moe/v4/anime/1").mock(return_value=jikan_ok(1, score=8.0))
    pinned = respx.get("https://api.jikan.moe/v4/anime/19").mock(
        return_value=jikan_ok(19, score=3.0)
    )
    handle = guid_item()
    config = make_config(rateAnime=True)
    run = config.libraries[0]
    pipeline = Pipeline(config, store, FakeServer([handle]))
    await pipeline.tag_library(run)
    store.set_binding("Animes", "mal://1", "mal", "19")

    await pipeline.rate_library(run)

    assert pinned.called and handle.ratings[-1] == 3.0


@respx.mock
async def test_a_row_another_plex_item_left_is_not_adopted(store: Store):
    """A GUID-less item that Plex no longer has left its row, and its pin,
    under a name a new matched item now shares: they are not this one's."""
    respx.get("https://api.jikan.moe/v4/anime/5").mock(return_value=jikan_ok(5, genres=("Drama",)))
    matched = guid_item(rating_key=5, title="Monster", year=2004)       # mal://5
    config = make_config(clearGenres=True)
    store.record_success("Animes", "Monster (2004)", fingerprint=config.fingerprint(
        config.libraries[0]), title="Monster", year=2004, rating_key=2, genres=["Old"],
        provider="jikan", provider_id="19")
    store.set_binding("Animes", "Monster (2004)", "mal", "19")

    await Pipeline(config, store, FakeServer([matched])).tag_library(config.libraries[0])

    assert matched.last_tags == ["Drama"], "resolved by its own GUID"
    assert store.get_bindings("Animes", "mal://5") == []
    assert store.get_bindings("Animes", "Monster (2004)") == [ExternalId("mal", "19")]
