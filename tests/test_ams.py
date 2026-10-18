"""arr-metadata-server as a source: its Plex provider, asked by the ids Plex items carry."""

from __future__ import annotations

import base64
import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from plex_auto_genres import cli
from plex_auto_genres.config import ProviderSettings, load_config, shown_address
from plex_auto_genres.doctor import run_doctor
from plex_auto_genres.errors import (
    ConfigError,
    ProviderAuthError,
    ProviderError,
    ProviderNotFound,
)
from plex_auto_genres.models import ExternalId, MediaType
from plex_auto_genres.plexsvc.client import parse_guid
from plex_auto_genres.providers import bindable_schemes, build_providers
from plex_auto_genres.providers.ams import AmsProvider, manifest_problem, split_address
from plex_auto_genres.providers.base import HttpTransport, LookupRequest
from plex_auto_genres.ratelimit import LimitSpec
from plex_auto_genres.server import create_app
from plex_auto_genres.server.auth import AuthSettings
from plex_auto_genres.store import Store

SERVER = "http://ams.test:8080"
PROVIDER = f"{SERVER}/plex"

MOVIE = MediaType.STANDARD_MOVIE
SHOW = MediaType.STANDARD_TV
ANIME = MediaType.ANIME


def transport() -> HttpTransport:
    limiter = LimitSpec(((1000, 1.0),)).build()
    # Following redirects, as the client every run shares does.
    return HttpTransport(
        httpx.AsyncClient(follow_redirects=True), limiter, max_attempts=2, name="ams"
    )


def ams(api_key: str | None = None, address: str = SERVER) -> AmsProvider:
    return AmsProvider(transport(), address, api_key)


def answer(*entries: dict) -> httpx.Response:
    return httpx.Response(200, json={"MediaContainer": {
        "offset": 0, "totalSize": len(entries), "size": len(entries),
        "identifier": "tv.plex.agents.custom.arrmetadataserver", "Metadata": list(entries),
    }})


def film(key: str = "movie-tmdb-603", **extra) -> dict:
    return {
        "ratingKey": key, "type": "movie", "title": "The Matrix", "year": 1999,
        "Genre": [{"tag": "Action"}, {"tag": "Science Fiction"}],
        "Guid": [{"id": "imdb://tt0133093"}, {"id": "tmdb://603"}],
        "Rating": [{"image": "imdb://image.rating", "type": "audience", "value": 8.7},
                   {"image": "themoviedb://image.rating", "type": "audience", "value": 8.2}],
        **extra,
    }


def series(key: str = "show-tvdb-81189", **extra) -> dict:
    return {
        "ratingKey": key, "type": "show", "title": "Breaking Bad", "year": 2008,
        "Genre": [{"tag": "Drama"}, {"tag": "Crime"}],
        "Guid": [{"id": "tvdb://81189"}, {"id": "tmdb://1396"}],
        **extra,
    }


# -- by the ids Plex gives ------------------------------------------------------


@respx.mock
async def test_a_film_is_asked_for_by_its_tmdb_id_and_answers_the_catalogues_genres():
    route = respx.get(f"{PROVIDER}/library/metadata/movie-tmdb-603").mock(
        return_value=answer(film())
    )

    result = await ams("ams_key").resolve(
        LookupRequest("The Matrix", 1999, MOVIE, external_ids=[ExternalId("tmdb", "603")])
    )

    assert result.genres == ["Action", "Science Fiction"]
    assert result.provider == "ams"
    assert result.provider_id == "movie-tmdb-603"
    assert result.matched_by == "guid"
    assert result.score == 8.2, "TMDB's audience rating, as the TMDB source would give"
    assert result.url == "https://www.themoviedb.org/movie/603"
    assert route.calls.last.request.headers["X-Api-Key"] == "ams_key"


@respx.mock
async def test_a_film_prefers_tmdb_to_imdb_and_reaches_imdb_alone():
    tmdb = respx.get(f"{PROVIDER}/library/metadata/movie-tmdb-603").mock(
        return_value=answer(film())
    )
    imdb = respx.get(f"{PROVIDER}/library/metadata/movie-imdb-tt0133093").mock(
        return_value=answer(film("movie-imdb-tt0133093"))
    )
    both = [ExternalId("imdb", "tt0133093"), ExternalId("tmdb", "603")]

    await ams().resolve(LookupRequest("The Matrix", 1999, MOVIE, external_ids=both))
    assert tmdb.called and not imdb.called

    await ams().resolve(LookupRequest("The Matrix", 1999, MOVIE, external_ids=both[:1]))
    assert imdb.called


@respx.mock
async def test_a_series_prefers_tvdb_the_id_the_catalogue_numbers_it_by():
    tvdb = respx.get(f"{PROVIDER}/library/metadata/show-tvdb-81189").mock(
        return_value=answer(series())
    )
    tmdb = respx.get(f"{PROVIDER}/library/metadata/show-tmdb-1396")

    result = await ams().resolve(LookupRequest(
        "Breaking Bad", 2008, SHOW,
        external_ids=[ExternalId("tmdb", "1396"), ExternalId("tvdb", "81189")],
    ))

    assert tvdb.called and not tmdb.called
    assert result.genres == ["Drama", "Crime"]
    assert result.url == "https://thetvdb.com/dereferrer/series/81189"


@respx.mock
async def test_a_film_in_an_anime_library_is_asked_for_as_a_film():
    # An anime library is as often a section of films. Asked for as a series,
    # a film's TMDB id names some other series -- which the server would
    # fetch into its catalogue for good.
    as_film = respx.get(f"{PROVIDER}/library/metadata/movie-tmdb-129").mock(
        return_value=answer(film("movie-tmdb-129", title="Spirited Away", year=2001))
    )
    as_series = respx.get(f"{PROVIDER}/library/metadata/show-tmdb-129")

    result = await ams().resolve(LookupRequest(
        "Spirited Away", 2001, ANIME, plex_type="movie",
        external_ids=[ExternalId("tvdb", "4521"), ExternalId("tmdb", "129")],
    ))

    assert as_film.called and not as_series.called
    assert result.provider_id == "movie-tmdb-129"


@respx.mock
async def test_a_series_is_not_looked_up_by_its_imdb_id():
    # The provider has no key for one, and its match by that id reaches only
    # what the catalogue already holds: the title search carries the id instead.
    matches = respx.post(f"{PROVIDER}/library/metadata/matches").mock(return_value=answer(series()))
    respx.get(f"{PROVIDER}/library/metadata/show-tvdb-81189").mock(return_value=answer(series()))

    result = await ams().resolve(LookupRequest(
        "Breaking Bad", 2008, SHOW, external_ids=[ExternalId("imdb", "tt0903747")],
    ))

    assert json.loads(matches.calls.last.request.content)["guids"] == ["imdb://tt0903747"]
    assert result.matched_by == "search"
    with pytest.raises(ProviderNotFound):
        await ams().fetch_by_id(ExternalId("imdb", "tt0903747"), LookupRequest("x", None, SHOW))


@respx.mock
async def test_the_servers_own_agent_key_wins_over_every_other_id():
    own = respx.get(f"{PROVIDER}/library/metadata/movie-id-0b5f").mock(
        return_value=answer(film("movie-id-0b5f", Guid=[]))
    )
    respx.get(f"{PROVIDER}/library/metadata/movie-tmdb-603")

    result = await ams().resolve(LookupRequest(
        "Made by hand", None, MOVIE,
        external_ids=[ExternalId("tmdb", "603"), ExternalId("ams", "movie-id-0b5f")],
    ))

    assert own.called
    assert result.provider_id == "movie-id-0b5f"
    assert result.url is None, "a work made by hand has no page elsewhere"


@respx.mock
async def test_a_pinned_key_is_read_and_says_it_came_from_the_binding():
    respx.get(f"{PROVIDER}/library/metadata/show-tvdb-81189").mock(return_value=answer(series()))

    result = await ams().resolve(LookupRequest(
        "Some Show", None, SHOW, pinned=[ExternalId("ams", "show-tvdb-81189")],
    ))

    assert result.matched_by == "binding"
    assert result.provider_id == "show-tvdb-81189"


async def test_a_key_of_the_other_kind_is_not_taken():
    with pytest.raises(ProviderNotFound, match="not a film"):
        await ams().fetch_by_id(ExternalId("ams", "show-tvdb-81189"),
                                LookupRequest("x", None, MOVIE))


# -- by title -------------------------------------------------------------------


@respx.mock
async def test_a_title_is_matched_then_the_work_is_read_by_its_key():
    matches = respx.post(f"{PROVIDER}/library/metadata/matches").mock(
        # A match drawn from the sources' search carries little.
        return_value=answer({"ratingKey": "show-tvdb-81189", "title": "Breaking Bad", "year": 2008})
    )
    work = respx.get(f"{PROVIDER}/library/metadata/show-tvdb-81189").mock(
        return_value=answer(series())
    )

    result = await ams().resolve(LookupRequest("Breaking Bad", 2008, ANIME))

    sent = json.loads(matches.calls.last.request.content)
    assert sent == {"type": 2, "includeAdult": True, "title": "Breaking Bad", "year": 2008}
    assert matches.calls.last.request.headers["Content-Type"] == "application/json"
    assert work.called
    assert result.genres == ["Drama", "Crime"]
    assert result.matched_by == "search"


@respx.mock
@pytest.mark.parametrize(("title", "year"), [
    ("Another Show", 2008), ("Breaking Bad", 2004),
    # No year to tell them apart: a home video named like a series is not it.
    ("Breaking Bad", None),
])
async def test_a_loose_title_match_is_never_fetched_into_the_catalogue(title, year):
    # Asking for a key fetches the work into the server's catalogue for good;
    # the nearest guess for a home video must not end up there.
    respx.post(f"{PROVIDER}/library/metadata/matches").mock(
        return_value=answer({"ratingKey": "show-tvdb-81189", "title": "Breaking Bad", "year": 2008})
    )
    work = respx.get(f"{PROVIDER}/library/metadata/show-tvdb-81189")

    with pytest.raises(ProviderNotFound):
        await ams().search(LookupRequest(title, year, SHOW))
    assert not work.called


@respx.mock
@pytest.mark.parametrize(("asked", "held"), [("Amelie", "Amélie"), ("AMÉLIE", "Amelie")])
async def test_a_title_is_the_same_whatever_its_accents(asked, held):
    # The server folds case and accents when it matches a title; a match it
    # made so must not be turned away here.
    respx.post(f"{PROVIDER}/library/metadata/matches").mock(
        return_value=answer(film("movie-tmdb-194", title=held, year=2001))
    )
    work = respx.get(f"{PROVIDER}/library/metadata/movie-tmdb-194").mock(
        return_value=answer(film("movie-tmdb-194", title=held, year=2001))
    )

    result = await ams().search(LookupRequest(asked, 2001, MOVIE))

    assert work.called and result.provider_id == "movie-tmdb-194"


@respx.mock
async def test_a_match_on_one_of_the_items_ids_is_taken_whatever_its_title():
    respx.post(f"{PROVIDER}/library/metadata/matches").mock(
        return_value=answer(film(title="Matrix, The (localised)"))
    )
    respx.get(f"{PROVIDER}/library/metadata/movie-tmdb-603").mock(return_value=answer(film()))

    result = await ams().search(LookupRequest(
        "La Matrice", 1999, MOVIE, external_ids=[ExternalId("imdb", "tt0133093")],
    ))

    assert result.provider_id == "movie-tmdb-603"


@respx.mock
async def test_a_title_search_hands_on_the_ids_a_match_can_use():
    matches = respx.post(f"{PROVIDER}/library/metadata/matches").mock(return_value=answer(film()))
    respx.get(f"{PROVIDER}/library/metadata/movie-tmdb-603").mock(return_value=answer(film()))

    await ams().search(LookupRequest(
        "The Matrix", 1999, MOVIE,
        external_ids=[ExternalId("tvdb", "169"), ExternalId("imdb", "tt0133093")],
    ))

    # Films are not matched by TheTVDB's ids, so that one is not sent.
    assert json.loads(matches.calls.last.request.content)["guids"] == ["imdb://tt0133093"]


@respx.mock
async def test_nothing_matched_is_not_found():
    respx.post(f"{PROVIDER}/library/metadata/matches").mock(return_value=answer())
    with pytest.raises(ProviderNotFound):
        await ams().search(LookupRequest("Nothing", None, MOVIE))


@respx.mock
async def test_candidates_are_every_likely_match_ranked_for_a_person_to_pick():
    route = respx.post(f"{PROVIDER}/library/metadata/matches").mock(return_value=answer(
        film("movie-tmdb-604", title="The Matrix Reloaded", year=2003),
        film(thumb="http://ams.test:8080/media/poster.jpg", summary="A hacker learns the truth."),
        {"ratingKey": "not a key", "title": "Broken"},
    ))

    found = await ams().search_candidates(LookupRequest("The Matrix", 1999, MOVIE))

    assert json.loads(route.calls.last.request.content)["manual"] is True
    assert [c.provider_id for c in found] == ["movie-tmdb-603", "movie-tmdb-604"]
    best = found[0]
    assert best.provider == "ams"
    assert best.image == "http://ams.test:8080/media/poster.jpg"
    assert best.synopsis == "A hacker learns the truth."
    assert best.url == "https://www.themoviedb.org/movie/603"
    assert best.genres == ["Action", "Science Fiction"]


# -- what the server answers ----------------------------------------------------


@respx.mock
async def test_the_catalogues_genres_are_kept_as_it_names_them():
    # It splits TMDB's compound genres itself, and keeps whole a name somebody
    # locked: the corrections are what this source is for.
    respx.get(f"{PROVIDER}/library/metadata/show-tvdb-1").mock(return_value=answer(series(
        "show-tvdb-1",
        Genre=[{"tag": "Sword & Sorcery"}, {"tag": "Action"}, {"tag": "action"}, {"tag": ""}],
    )))
    result = await ams().fetch_by_id(ExternalId("tvdb", "1"), LookupRequest("x", None, SHOW))
    assert result.genres == ["Sword & Sorcery", "Action"]


@respx.mock
@pytest.mark.parametrize(("status", "code", "key", "said"), [
    # By address, the server's default: this app's is not on its list.
    (403, "forbidden", None, ["refused this app's address", "Allowed addresses",
                              "ARR_METADATA_API_KEY"]),
    # Asked for a key, and given none, or one it does not know.
    (401, "unauthorized", None, ["asks this app for a key", "ARR_METADATA_API_KEY"]),
    (401, "unauthorized", "ams_k3y", ["does not know the key", "from ARR_METADATA_API_KEY"]),
    # A key switched off or run out -- or an address list that reads no key.
    (403, "forbidden", "ams_k3y", ["switched off or have run out", "Allowed addresses"]),
    (403, "owner_inactive", "ams_k3y", ["account it belongs to", "not active"]),
    # Not the server's: something in front of it, a proxy and its password.
    (401, None, None, ["not as arr-metadata-server refuses", "user and password"]),
    (403, None, "ams_k3y", ["not as arr-metadata-server refuses"]),
])
async def test_a_refusal_says_what_lets_this_app_in(status, code, key, said):
    respx.get(f"{PROVIDER}/library/metadata/movie-tmdb-603").mock(return_value=(
        httpx.Response(status, json={"error": code, "message": "no"}) if code
        else httpx.Response(status, text="<html>Unauthorized</html>")
    ))
    with pytest.raises(ProviderAuthError) as refused:
        await ams(api_key=key).fetch_by_id(
            ExternalId("tmdb", "603"), LookupRequest("x", None, MOVIE)
        )
    assert all(words in str(refused.value) for words in said), str(refused.value)
    assert (refused.value.status, refused.value.code) == (status, code)
    assert "ams_k3y" not in str(refused.value)


@respx.mock
async def test_a_key_in_the_address_travels_in_a_header_and_is_never_quoted():
    # The address the server's Plex panel gives carries a key (0.8.1). Kept in
    # the path, it would be in every address this app quotes.
    route = respx.get(f"{PROVIDER}/library/metadata/movie-tmdb-603").mock(
        return_value=httpx.Response(404)
    )
    provider = ams(address=f"{SERVER}/plex/ams_k3y")

    with pytest.raises(ProviderNotFound) as missing:
        await provider.fetch_by_id(ExternalId("tmdb", "603"), LookupRequest("x", None, MOVIE))

    sent = route.calls.last.request
    assert sent.headers["X-Api-Key"] == "ams_k3y" and "ams_k3y" not in str(sent.url)
    assert "ams_k3y" not in str(missing.value)


@respx.mock
async def test_a_refused_key_is_named_by_the_setting_it_came_from():
    # Told to set ARR_METADATA_API_KEY for a key the address carries, the
    # person was then refused for giving two keys.
    respx.get(f"{PROVIDER}/library/metadata/movie-tmdb-603").mock(
        return_value=httpx.Response(401, json={"error": "unauthorized", "message": "no"})
    )
    with pytest.raises(ProviderAuthError) as refused:
        await ams(address=f"{SERVER}/plex/ams_k3y").fetch_by_id(
            ExternalId("tmdb", "603"), LookupRequest("x", None, MOVIE)
        )
    assert "from ARR_METADATA_URL" in str(refused.value)
    assert "ARR_METADATA_API_KEY" not in str(refused.value)


@pytest.mark.parametrize(("given", "server", "key"), [
    ("http://ams.test:8080/plex/ams_k3y", "http://ams.test:8080", "ams_k3y"),
    ("http://ams.test/sub/plex/ams_k3y/", "http://ams.test/sub", "ams_k3y"),
    ("http://u:p@ams.test/plex/ams_k%2B3y", "http://ams.test", "ams_k+3y"),
    ("http://ams.test:8080", "http://ams.test:8080", None),
    ("http://ams.test/sub", "http://ams.test/sub", None),
    ("http://ams.test/plex/AMS_K3y", "http://ams.test", "AMS_K3y"),
    # Every key the server issues begins with ams_: a path it is reached
    # under, behind a proxy, is not one.
    ("http://nas.test/plex/ams", "http://nas.test/plex/ams", None),
    ("http://nas.test/apps/plex/plex/ams_k3y", "http://nas.test/apps/plex", "ams_k3y"),
])
def test_the_key_an_address_carries_is_taken_out_of_it(given, server, key):
    address = split_address(given)
    assert (address.server, address.key) == (server, key)


def test_two_keys_for_one_server_are_refused_and_one_twice_is_not():
    with pytest.raises(ConfigError, match="ARR_METADATA_API_KEY another") as refused:
        build_providers(("ams",), MOVIE, ProviderSettings(
            ams_url=f"{SERVER}/plex/ams_one", ams_api_key="ams_two",
        ))
    assert "ams_one" not in str(refused.value) and "ams_two" not in str(refused.value)
    build_providers(("ams",), MOVIE, ProviderSettings(
        ams_url=f"{SERVER}/plex/ams_one", ams_api_key="ams_one",
    ))


@respx.mock
async def test_an_address_that_is_not_the_server_says_so():
    respx.get(f"{PROVIDER}/library/metadata/movie-tmdb-603").mock(
        return_value=httpx.Response(200, text="<html>a login page</html>")
    )
    with pytest.raises(ProviderError, match="ARR_METADATA_URL"):
        await ams().fetch_by_id(ExternalId("tmdb", "603"), LookupRequest("x", None, MOVIE))


@respx.mock
async def test_a_redirect_is_named_rather_than_followed():
    # A redirected search comes back a GET, which the provider does not answer.
    respx.post(f"{PROVIDER}/library/metadata/matches").mock(return_value=httpx.Response(
        301, headers={"Location": "https://u:p@ams.test/plex/library/metadata/matches"}
    ))
    respx.get("https://ams.test/plex/library/metadata/matches").mock(return_value=answer())
    with pytest.raises(ProviderError, match=r"redirects to https://ams\.test/plex") as moved:
        await ams().search(LookupRequest("The Matrix", 1999, MOVIE))
    assert "u:p" not in str(moved.value)


# -- credentials in the address -------------------------------------------------


@respx.mock
async def test_credentials_in_the_address_travel_as_basic_auth_and_are_never_quoted():
    route = respx.get(f"{PROVIDER}/library/metadata/movie-tmdb-603").mock(
        return_value=httpx.Response(404)
    )
    provider = ams(address="http://someone:hun%40ter2@ams.test:8080")

    with pytest.raises(ProviderNotFound) as missing:
        await provider.fetch_by_id(ExternalId("tmdb", "603"), LookupRequest("x", None, MOVIE))

    sent = route.calls.last.request
    assert sent.url.userinfo == b""
    expected = base64.b64encode(b"someone:hun@ter2").decode()
    assert sent.headers["Authorization"] == f"Basic {expected}"
    assert "hun" not in str(missing.value) and "someone" not in str(missing.value)


@respx.mock
def test_the_doctor_never_quotes_the_credentials_either():
    respx.get(PROVIDER).mock(return_value=httpx.Response(502))
    problem = manifest_problem("http://someone:hunter2@ams.test:8080", None) or ""
    assert "answered HTTP 502" in problem and "hunter2" not in problem


# -- configuration --------------------------------------------------------------


@pytest.mark.parametrize("given", [
    "http://ams.test:8080", "http://ams.test:8080/", " http://ams.test:8080/plex ",
    "http://ams.test:8080/plex/",
])
def test_the_servers_address_is_taken_as_given_or_as_its_providers(given):
    assert ProviderSettings(ams_url=given).ams_url == "http://ams.test:8080"


@pytest.mark.parametrize("given", [
    "ams.test:8080", "http://u:pa/ss@ams.test", "http://ams.test:port", "http://[::1",
    "http://ams.test:99999", "http://:8080", "http://ams\x01.test:8080", "http://ams test:8080",
    "http://ams.test:8080?x=1", "http://ams.test:8080#top",
])
def test_an_unusable_address_is_refused_where_it_is_used_not_at_load(given, tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"version": 2, "libraries": [
        {"library": "Films", "type": "standard-movie"},
    ]}))
    monkeypatch.setenv("ARR_METADATA_URL", given)

    config = load_config(path)  # a library not reading ams loads all the same

    with pytest.raises(ConfigError, match="ARR_METADATA_URL"):
        build_providers(("ams",), MOVIE, config.providers)


def test_the_address_and_key_come_from_the_environment(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"version": 2, "libraries": [
        {"library": "Films", "type": "standard-movie", "providers": ["ams", "tmdb"]},
    ]}))
    monkeypatch.setenv("ARR_METADATA_URL", "https://ams.test/plex")
    monkeypatch.setenv("ARR_METADATA_API_KEY", "ams_key")

    config = load_config(path)

    assert config.providers.ams_url == "https://ams.test"
    assert config.providers.ams_api_key == "ams_key"
    assert config.libraries[0].resolved_providers == ("ams", "tmdb")


@pytest.mark.parametrize("given", ["http://ams.test:port", "http://[::1", "http://:8080"])
def test_the_doctor_says_why_an_unusable_address_cannot_be_asked(given):
    # Not an error of another kind escaping from the check that explains it.
    assert "ARR_METADATA_URL" in (manifest_problem(given, None) or "")


def test_an_address_the_parser_chokes_on_is_refused_without_quoting_it():
    # Python's own message names the address whole, password included.
    with pytest.raises(ConfigError) as refused:
        build_providers(("ams",), MOVIE, ProviderSettings(ams_url="http://user:p\u00e4\uff20ss@ams.test"))
    assert "user" not in str(refused.value) and refused.value.__cause__ is None
    assert refused.value.__suppress_context__


def test_a_key_no_header_can_carry_is_refused_without_quoting_it():
    # The HTTP library's own complaint quotes a header value whole, into
    # every title's recorded error.
    with pytest.raises(ConfigError, match="ARR_METADATA_API_KEY") as refused:
        build_providers(("ams",), MOVIE, ProviderSettings(ams_url=SERVER, ams_api_key="ams key"))
    assert "ams key" not in str(refused.value)
    assert "ARR_METADATA_API_KEY" in (manifest_problem(SERVER, "ams\tkey") or "")


def test_the_blank_around_a_key_is_dropped(tmp_path, monkeypatch):
    assert ProviderSettings(ams_api_key="  ams_key\n").ams_api_key == "ams_key"
    assert ProviderSettings(ams_api_key="  ").ams_api_key is None
    monkeypatch.setenv("ARR_METADATA_API_KEY", "ams\x01key")
    check = _doctor(tmp_path, monkeypatch, SERVER)
    assert (check.level, check.title) == ("error", "ARR_METADATA_API_KEY cannot be used")


def test_a_chain_with_ams_needs_its_address():
    with pytest.raises(ConfigError, match="ARR_METADATA_URL"):
        build_providers(("ams",), MOVIE, ProviderSettings())


@respx.mock
async def test_the_source_is_built_with_the_key_it_is_given():
    route = respx.get(f"{PROVIDER}/library/metadata/movie-tmdb-603").mock(
        return_value=answer(film())
    )
    pool = build_providers(("ams", "jikan"), ANIME,
                           ProviderSettings(ams_url=SERVER, ams_api_key="ams_key"))
    async with pool:
        assert [p.name for p in pool.providers] == ["ams", "jikan"]
        await pool.providers[0].fetch_by_id(
            ExternalId("tmdb", "603"), LookupRequest("x", None, ANIME, plex_type="movie")
        )
    assert route.calls.last.request.headers["X-Api-Key"] == "ams_key"


def test_the_pins_a_library_takes_follow_the_order_they_are_tried_in():
    assert bindable_schemes(MOVIE, ["ams"]) == ["ams", "tmdb", "imdb"]
    assert bindable_schemes(SHOW, ["ams"]) == ["ams", "tvdb", "tmdb"]
    assert "ams" in bindable_schemes(ANIME, ["jikan", "ams"])
    # An anime library is as often a section of films: an IMDb id is pinned
    # on one, a TheTVDB id on a series.
    assert {"tvdb", "imdb"} <= set(bindable_schemes(ANIME, ["ams"]))
    assert "anidb" not in bindable_schemes(ANIME, ["ams"]), "it reads no MAL or AniList id"


def test_an_address_is_shown_without_its_credentials():
    assert shown_address("http://user:secret@ams.test:8080/x") == "http://ams.test:8080/x"
    assert shown_address("http://[::1]:8080") == "http://[::1]:8080"
    assert shown_address("http://u:p@[::1]:8080") == "http://[::1]:8080"
    assert shown_address("http://u:p@ams.test:80800") == "http://ams.test:80800", "no port check"
    assert shown_address("http://u:pa/ss@ams.test:8080") == "http://ams.test:8080"
    assert shown_address("http://u:pa#ss@ams.test") == "http://ams.test"
    assert shown_address(None) is None
    # Nor the key the address the server's Plex panel gives ends with.
    assert shown_address("http://u:p@ams.test:8080/plex/ams_k3y") == "http://ams.test:8080/plex/***"
    assert shown_address("http://ams.test/plex/ams_k3y/") == "http://ams.test/plex/***/"
    assert shown_address("http://ams.test/plex/library/x") == "http://ams.test/plex/library/x"
    assert shown_address("http://nas.test/apps/plex/plex/ams_k3y") == "http://nas.test/apps/plex/plex/***"
    assert shown_address("http://nas.test/plex/ams") == "http://nas.test/plex/ams", "not a key"
    assert shown_address("http://ams.test/plex?x=1&APIKEY=ams_k3y") == "http://ams.test/plex?x=1&APIKEY=***"
    assert shown_address("http://ams.test/plex?api_key=ams_k3y#a") == "http://ams.test/plex?api_key=***#a"


# -- pins from the console and the command line ---------------------------------


def _ams_library(tmp_path, monkeypatch):
    monkeypatch.setenv("PLEX_BASE_URL", "http://plex:32400")
    monkeypatch.setenv("PLEX_TOKEN", "t")
    monkeypatch.setenv("ARR_METADATA_URL", SERVER)
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"version": 2, "libraries": [
        {"library": "Films", "type": "standard-movie", "providers": ["ams"]},
    ]}))
    return path


def test_the_console_pins_an_ams_key(tmp_path, monkeypatch):
    path = _ams_library(tmp_path, monkeypatch)
    app = create_app(path, tmp_path / "state.db", static_dir=tmp_path / "nope",
                     auth=AuthSettings(password=None))
    with TestClient(app) as client:
        made = client.post("/api/v1/bindings", json={
            "library": "Films", "media_key": "tmdb://603",
            "provider": "ams", "provider_id": "movie-tmdb-603",
        })
    assert made.status_code == 201, made.text
    assert made.json()["provider"] == "ams"


@respx.mock
def test_the_console_searches_a_film_in_an_anime_library_among_films(tmp_path, monkeypatch):
    # A series' key pinned on a film is refused on every run.
    path = _ams_library(tmp_path, monkeypatch)
    matches = respx.post(f"{PROVIDER}/library/metadata/matches").mock(return_value=answer(film()))
    app = create_app(path, tmp_path / "state.db", static_dir=tmp_path / "nope",
                     auth=AuthSettings(password=None))
    with TestClient(app) as client:
        found = client.get("/api/v1/search", params={
            "q": "The Matrix", "type": "anime", "provider": "ams", "kind": "movie",
        })
    assert found.status_code == 200, found.text
    assert [c["provider_id"] for c in found.json()] == ["movie-tmdb-603"]
    assert json.loads(matches.calls.last.request.content)["type"] == 1


def test_the_command_line_pins_an_ams_key(tmp_path, monkeypatch, capsys):
    path = _ams_library(tmp_path, monkeypatch)
    db = tmp_path / "state.db"

    assert cli.main(["--config", str(path), "--db", str(db),
                     "bind", "Films", "tmdb://603", "ams", "movie-tmdb-603"]) == 0

    with Store(db) as store:
        assert [(r["provider"], r["provider_id"]) for r in store.list_bindings("Films")] \
            == [("ams", "movie-tmdb-603")]
    capsys.readouterr()


# -- the GUIDs its Plex agent gives ---------------------------------------------


@pytest.mark.parametrize(("raw", "key"), [
    ("tv.plex.agents.custom.arrmetadataserver://movie/movie-tmdb-603", "movie-tmdb-603"),
    ("tv.plex.agents.custom.arrmetadataserver://show/show-tvdb-81189?lang=fr", "show-tvdb-81189"),
    ("TV.PLEX.AGENTS.CUSTOM.ARRMETADATASERVER://movie/movie-id-0b5f", "movie-id-0b5f"),
])
def test_the_agents_guid_names_the_work_by_its_key(raw, key):
    assert parse_guid(raw) == ExternalId("ams", key)


@pytest.mark.parametrize("raw", [
    "tv.plex.agents.custom.arrmetadataserver://season/season-tvdb-81189-s1",
    "tv.plex.agents.custom.arrmetadataserver://movie/",
    "tv.plex.agents.custom.someoneelse://movie/movie-tmdb-603",
    # Typed by hand, an ams:// key is no item's key: items are keyed by their
    # TMDB, TheTVDB… ids, never by this one.
    "ams://movie-tmdb-603",
])
def test_other_guids_are_not_taken(raw):
    assert parse_guid(raw) is None


# -- the doctor -----------------------------------------------------------------


def _doctor(tmp_path, monkeypatch, url: str | None, *, online: bool = False):
    if url is None:
        monkeypatch.delenv("ARR_METADATA_URL", raising=False)
    else:
        monkeypatch.setenv("ARR_METADATA_URL", url)
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"version": 2, "libraries": [
        {"library": "Films", "type": "standard-movie", "providers": ["ams"]},
    ]}))
    with Store(tmp_path / "state.db") as store:
        report = run_doctor(str(path), store, check_taxonomy=online)
    return next(c for c in report.checks if c.id == "ams")


def test_the_doctor_shows_an_address_without_the_key_it_carries(tmp_path, monkeypatch):
    check = _doctor(tmp_path, monkeypatch, f"{SERVER}/plex/ams_k3y")
    assert check.level == "ok" and "/plex/***" in check.detail and "ams_k3y" not in check.detail
    monkeypatch.setenv("ARR_METADATA_API_KEY", "ams_other")
    check = _doctor(tmp_path, monkeypatch, f"{SERVER}/plex/ams_k3y")
    assert (check.level, check.title) == ("error", "ARR_METADATA_API_KEY cannot be used")


def test_the_doctor_wants_the_address_when_a_library_reads_from_ams(tmp_path, monkeypatch):
    check = _doctor(tmp_path, monkeypatch, None)
    assert check.level == "error" and "ARR_METADATA_URL" in check.title


def test_the_doctor_says_an_address_cannot_be_used(tmp_path, monkeypatch):
    check = _doctor(tmp_path, monkeypatch, "ams.test:8080")
    assert check.level == "error" and "http://" in (check.detail or "")


def test_offline_the_doctor_only_checks_that_there_is_an_address(tmp_path, monkeypatch):
    check = _doctor(tmp_path, monkeypatch, "http://u:p@ams.test:8080")
    assert check.level == "ok"
    assert (check.detail or "").startswith("http://ams.test:8080 ")


@respx.mock
def test_online_the_doctor_asks_the_providers_manifest(tmp_path, monkeypatch):
    respx.get(PROVIDER).mock(return_value=httpx.Response(
        200, json={"MediaProvider": {"identifier": "tv.plex.agents.custom.arrmetadataserver"}}
    ))
    assert _doctor(tmp_path, monkeypatch, SERVER, online=True).level == "ok"


@respx.mock
@pytest.mark.parametrize(("response", "said"), [
    (httpx.Response(403, json={"error": "forbidden", "message": "no"}), "Allowed addresses"),
    (httpx.Response(401, json={"error": "unauthorized"}), "asks this app for a key"),
    (httpx.Response(401, text="Unauthorized"), "not as arr-metadata-server refuses"),
    (httpx.Response(503, json={"error": "surface_disabled", "message": "off"}), "switched off"),
    (httpx.Response(503, text="bad gateway"), "answered HTTP 503"),
    (httpx.Response(421, text="unknown host"), "AMS_ALLOWED_HOSTS"),
    (httpx.Response(302, headers={"Location": "https://ams.test/plex"}), "redirects to https://ams.test/plex"),
    (httpx.Response(404), "HTTP 404"),
])
def test_the_manifest_tells_apart_why_the_server_cannot_be_asked(response, said):
    respx.get(PROVIDER).mock(return_value=response)
    assert said in (manifest_problem(SERVER, None) or "")


@respx.mock
def test_the_manifest_is_asked_with_the_key_the_address_carries():
    route = respx.get(PROVIDER).mock(
        return_value=httpx.Response(401, json={"error": "unauthorized"})
    )
    said = manifest_problem(f"{SERVER}/plex/ams_k3y", None) or ""
    assert route.calls.last.request.headers["X-Api-Key"] == "ams_k3y"
    assert "does not know the key" in said and "ams_k3y" not in said


@respx.mock
def test_a_server_that_is_not_arr_metadata_server_is_named():
    respx.get(PROVIDER).mock(return_value=httpx.Response(200, json={"MediaProvider": {
        "identifier": "tv.plex.agents.custom.someoneelse"}}))
    assert "does not answer as arr-metadata-server" in (manifest_problem(SERVER, None) or "")


@respx.mock
def test_a_server_that_cannot_be_reached_is_named():
    respx.get(PROVIDER).mock(side_effect=httpx.ConnectError("refused"))
    assert "cannot be reached" in (manifest_problem(SERVER, None) or "")


def test_a_library_not_reading_ams_is_not_checked_for_it(tmp_path, monkeypatch):
    monkeypatch.setenv("ARR_METADATA_URL", "not an address")
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"version": 2, "libraries": [
        {"library": "Films", "type": "standard-movie"},
    ]}))
    with Store(tmp_path / "state.db") as store:
        report = run_doctor(str(path), store, check_taxonomy=False)
    assert all(c.id != "ams" for c in report.checks)
