"""Configuration, migration and the per-library fingerprint (bug #6)."""

from __future__ import annotations

import json

import pytest

from plex_auto_genres.config import AppConfig, GenreRules, load_config, migrate_v1
from plex_auto_genres.errors import ConfigError
from plex_auto_genres.models import MediaType, fold

V1 = {
    "general_settings": {"genres": {
        "standard-tv": {"ignore": [], "replace": {"sci-fi": "science fiction"},
                        "sortedPrefix": "", "sortedCollections": ["action", "sci-fi"]},
        "anime": {"ignore": ["kids"], "replace": {"Shoujo Ai": "shoujo"},
                  "sortedPrefix": "*", "sortedCollections": ["action"]},
    }},
    "automation_settings": {"run": [
        {"library": "Shows A", "type": "standard-tv", "setPosters": False,
         "sortCollections": False, "rateAnime": False, "createRatingCollections": False,
         "useKeywords": True, "useGenres": True, "clearGenres": True},
        {"library": "Shows B", "type": "standard-tv", "setPosters": False,
         "sortCollections": False, "rateAnime": False, "createRatingCollections": False,
         "useKeywords": False, "useGenres": False, "clearGenres": False},
    ]},
}


def build(raw: dict) -> AppConfig:
    return AppConfig.model_validate({**migrate_v1(raw), "plex": {}, "providers": {}})


def test_v1_config_migrates():
    config = build(V1)
    assert config.version == 2
    assert [r.library for r in config.libraries] == ["Shows A", "Shows B"]
    assert config.defaults[MediaType.STANDARD_TV].replace == {"sci-fi": "science fiction"}


def test_two_libraries_of_one_type_get_distinct_fingerprints():
    """Bug #6: v1 keyed the cache on media type, so these shared -- and
    poisoned -- a single progress file."""
    config = build(V1)
    a, b = config.find("Shows A"), config.find("Shows B")
    assert config.fingerprint(a) != config.fingerprint(b)


def test_fingerprint_changes_when_a_setting_changes():
    config = build(V1)
    run = config.find("Shows A")
    before = config.fingerprint(run)
    flipped = run.model_copy(update={"use_keywords": False})
    assert config.fingerprint(flipped) != before


def test_fingerprint_is_stable_across_calls():
    config = build(V1)
    run = config.find("Shows A")
    assert config.fingerprint(run) == config.fingerprint(run)


def test_replace_keys_are_lowercased():
    """v1 raised a bare KeyError when a replace key was not already lowercase."""
    config = build(V1)
    rules = config.defaults[MediaType.ANIME]
    assert "shoujo ai" in rules.replace
    assert rules.apply(["Shoujo Ai"]) == ["shoujo"]


def test_per_library_overrides_layer_over_type_defaults():
    config = AppConfig.model_validate({
        "version": 2,
        "defaults": {"standard-tv": {"ignore": ["Reality"], "replace": {"sci-fi": "science fiction"}}},
        "libraries": [{
            "library": "Kids TV", "type": "standard-tv", "useGenres": True,
            "overrides": {"ignore": ["Horror"], "replace": {"animation": "Cartoon"}},
        }],
    })
    rules = config.rules_for(config.find("Kids TV"))
    assert set(rules.ignore) == {"Reality", "Horror"}
    assert rules.apply(["Sci-Fi", "Animation", "Horror", "Reality"]) == [
        "science fiction", "Cartoon"
    ]


def test_genre_rules_cap_the_list():
    rules = GenreRules(maxGenres=2)
    assert rules.apply(["a", "b", "c", "d"]) == ["a", "b"]


def test_genre_rules_drop_a_replacement_that_maps_onto_an_ignored_name():
    rules = GenreRules(ignore=["Cartoon"], replace={"animation": "Cartoon"})
    assert rules.apply(["Animation", "Drama"]) == ["Drama"]


def test_duplicate_libraries_are_rejected():
    with pytest.raises(Exception):
        AppConfig.model_validate({"version": 2, "libraries": [
            {"library": "X", "type": "anime"}, {"library": "x", "type": "anime"},
        ]})


def test_clear_genres_without_use_genres_is_rejected_in_v2():
    """v1 accepted this and silently did nothing (see writer.py's docstring)."""
    with pytest.raises(Exception, match="clearGenres requires useGenres"):
        AppConfig.model_validate({"version": 2, "libraries": [
            {"library": "X", "type": "anime", "useGenres": False, "clearGenres": True},
        ]})


def test_migration_drops_the_impossible_v1_combination():
    raw = {"automation_settings": {"run": [
        {"library": "X", "type": "anime", "useGenres": False, "clearGenres": True,
         "useKeywords": True, "setPosters": False, "sortCollections": False,
         "rateAnime": False, "createRatingCollections": False},
    ]}}
    config = build(raw)
    run = config.find("X")
    assert run.clear_genres is False   # was a no-op in v1
    assert run.use_keywords is False   # a migrated anime library reads MAL alone


def test_keywords_need_a_source_that_has_them():
    """The rule follows the sources, not the library type.

    TMDB has keywords and AniList has community tags; MyAnimeList has neither,
    its themes and demographics being part of the genres it returns already.
    """
    with pytest.raises(Exception, match="useKeywords needs a source with keywords"):
        AppConfig.model_validate({"version": 2, "libraries": [
            {"library": "X", "type": "anime", "useKeywords": True},
        ]})

    for chain in (["jikan", "anilist"], ["jikan", "tmdb"]):
        config = AppConfig.model_validate({"version": 2, "libraries": [
            {"library": "X", "type": "anime", "useKeywords": True, "providers": chain},
        ]})
        assert config.find("X").use_keywords is True


def test_the_keyword_sources_listed_in_config_are_the_ones_that_have_them():
    """config.py cannot import the providers, so a test keeps the two in step."""
    from plex_auto_genres.config import KEYWORD_PROVIDERS
    from plex_auto_genres.providers import _CLASSES

    assert set(KEYWORD_PROVIDERS) == {n for n, c in _CLASSES.items() if c.has_keywords}


def test_default_providers_per_type():
    config = build(V1)
    assert config.find("Shows A").resolved_providers == ("tmdb",)
    anime = AppConfig.model_validate({"version": 2, "libraries": [
        {"library": "A", "type": "anime"}]})
    assert anime.find("A").resolved_providers == ("jikan",)


def test_missing_config_file_gives_an_actionable_error(tmp_path):
    with pytest.raises(ConfigError, match="No configuration file"):
        load_config(tmp_path / "nope.json")


def test_malformed_json_is_reported_clearly(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not json")
    with pytest.raises(ConfigError, match="not valid JSON"):
        load_config(path)


def test_unknown_field_is_rejected_rather_than_ignored(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"version": 2, "libraries": [
        {"library": "X", "type": "anime", "typo_field": True}]}))
    with pytest.raises(ConfigError, match="typo_field"):
        load_config(path, use_env=False)


def test_json_schema_is_generated_for_web_forms():
    from plex_auto_genres.config import config_json_schema

    schema = config_json_schema()
    assert "libraries" in schema["properties"]
    # Descriptions are what a generated form shows as help text.
    defs = schema["$defs"]["LibraryRun"]["properties"]
    assert defs["useGenres"]["description"]


def test_comment_keys_are_stripped(tmp_path):
    """The shipped example documents itself with '//' keys; strict validation
    would otherwise reject a straight copy of it."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "//": "a note", "version": 2,
        "libraries": [{"//why": "explanation", "library": "X", "type": "anime"}],
    }))
    config = load_config(path, use_env=False)
    assert config.find("X") is not None


def test_the_shipped_example_is_valid():
    """Guards against the example and the models drifting apart."""
    from pathlib import Path

    config = load_config(Path(__file__).parent.parent / "config" / "config.json.example",
                         use_env=False)
    assert [r.library for r in config.libraries] == ["Anime Shows", "TV Shows", "Movies"]
    movies = config.find("Movies")
    assert config.rules_for(movies).max_genres == 8


def test_taxonomy_check_reports_each_stale_name_once():
    from plex_auto_genres.taxonomy import check_names

    live = ["Action", "Racing", "Suspense"]
    stale = check_names(["cars", "Cars", "action", "thriller"], live)
    assert stale == [("cars", "Racing"), ("thriller", "Suspense")]


def test_blank_library_names_are_rejected():
    for bad in ("", "   "):
        with pytest.raises(Exception, match="blank|at least 1"):
            AppConfig.model_validate({"version": 2, "libraries": [{"library": bad, "type": "anime"}]})


def test_library_names_are_stripped():
    config = AppConfig.model_validate({"version": 2, "libraries": [{"library": "  Animes ", "type": "anime"}]})
    assert config.libraries[0].library == "Animes"


def test_migration_skips_malformed_v1_entries():
    """v1 ran one subprocess per entry, so a bad entry only failed itself."""
    raw = {"automation_settings": {"run": [
        {"library": "A", "type": "anime"},
        {"library": "Doc", "type": "some other type"},   # the v1 example ships one of these
        {"type": "anime"},
    ]}}
    assert [r["library"] for r in migrate_v1(raw)["libraries"]] == ["A"]


def test_missing_config_is_fine_when_told_so(tmp_path, monkeypatch):
    monkeypatch.setenv("PLEX_BASE_URL", "http://plex:32400")
    monkeypatch.setenv("PLEX_TOKEN", "t")
    config = load_config(tmp_path / "absent.json", missing_ok=True)
    assert config.libraries == [] and config.plex.base_url == "http://plex:32400"


def test_an_anime_library_may_end_its_chain_with_tmdb():
    """TMDB is never a default for anime, but a library can opt into it."""
    config = AppConfig.model_validate({
        "version": 2,
        "libraries": [
            {"library": "Animes", "type": "anime", "providers": ["jikan", "anilist", "tmdb"]},
            {"library": "Plain", "type": "anime"},
        ],
    })
    assert config.find("Animes").resolved_providers == ("jikan", "anilist", "tmdb")
    assert config.find("Plain").resolved_providers == ("jikan",), "still no TMDB by default"


def test_spelling_variants_collapse_into_one_genre():
    """Two sources spell one idea differently and Plex grows two collections.

    Comparisons run on letters and digits alone, so punctuation, spacing, case
    and accents stop splitting a genre in two.
    """
    rules = GenreRules(ignore=["ecchi"], replace={"comédie": "Comedy"})

    assert rules.apply(["Boys Love", "Boys' Love", "boys-love"]) == ["Boys Love"]
    assert rules.apply(["Comédie", "Comedy", "COMEDIE"]) == ["Comedy"]
    assert rules.apply(["Sci-Fi", "Sci Fi"]) == ["Sci-Fi"], "first spelling seen wins"
    assert rules.apply(["Ecchi", "ecchi!"]) == [], "an ignore rule matches variants too"


def test_a_rename_reaches_a_genre_whatever_its_spelling():
    rules = GenreRules(replace={"sci fi": "Science Fiction"})
    assert rules.apply(["Sci-Fi"]) == ["Science Fiction"]


def test_the_default_provider_mode_leaves_the_fingerprint_alone():
    """Adding a field every library carries would invalidate every cache.

    Only a mode that is not the old behaviour joins the hash, so upgrading does
    not send a whole install back through its libraries for nothing.
    """
    def fingerprint(**library) -> str:
        base = {"library": "A", "type": "anime", "useGenres": True}
        base.update(library)
        config = AppConfig.model_validate({"version": 2, "libraries": [base]})
        return config.fingerprint(config.libraries[0])

    assert fingerprint() == fingerprint(providerMode="fallback")
    assert fingerprint(providers=["jikan", "anilist"], providerMode="merge") != fingerprint(
        providers=["jikan", "anilist"]
    ), "merging writes a different genre list, so its cache is not the same"


def test_folding_keeps_every_writing_system():
    """An ASCII-only fold dropped whole libraries: TMDB answers in the
    language it is asked in, and those names are all a library has."""
    assert fold("日常") == "日常"
    assert fold("Романтика") == "романтика"
    assert fold("Θρίλερ") == "θριλερ"
    assert fold("드라마") == "드라마"

    rules = GenreRules()
    assert rules.apply(["アクション", "ドラマ"]) == ["アクション", "ドラマ"]
    assert GenreRules(replace={"コメディ": "Comedy"}).apply(["コメディ"]) == ["Comedy"]


def test_two_rename_rules_that_mean_the_same_thing_are_refused():
    """Folded keys made them one entry, and the loser vanished in silence."""
    with pytest.raises(Exception, match="the same rule"):
        GenreRules(replace={"sci-fi": "Science Fiction", "Sci Fi": "SciFi"})


def test_the_tmdb_language_is_part_of_the_fingerprint():
    """It decides the very strings written to Plex, so changing it re-tags."""
    def fingerprint(language: str) -> str:
        config = AppConfig.model_validate({
            "version": 2,
            "libraries": [{"library": "A", "type": "standard-movie", "useGenres": True}],
            "providers": {"tmdb_language": language},
        })
        return config.fingerprint(config.libraries[0])

    assert fingerprint("en-US") != fingerprint("fr-FR")
    assert fingerprint("en-US") == AppConfig.model_validate({
        "version": 2,
        "libraries": [{"library": "A", "type": "standard-movie", "useGenres": True}],
    }).fingerprint(AppConfig.model_validate({
        "version": 2,
        "libraries": [{"library": "A", "type": "standard-movie", "useGenres": True}],
    }).libraries[0]), "the default must not disturb an existing install"


def test_doctor_warns_when_the_keyword_source_is_never_reached(tmp_path, monkeypatch):
    """Falling back stops at the first source that answers, so keywords behind
    one that has none are a toggle that reports itself as on and does nothing."""
    from plex_auto_genres.doctor import run_doctor
    from plex_auto_genres.store import Store

    monkeypatch.setenv("PLEX_BASE_URL", "http://plex:32400")
    monkeypatch.setenv("PLEX_TOKEN", "t")
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"version": 2, "libraries": [
        {"library": "Behind", "type": "anime", "useGenres": True, "useKeywords": True,
         "providers": ["jikan", "anilist"]},
        {"library": "First", "type": "anime", "useGenres": True, "useKeywords": True,
         "providers": ["anilist", "jikan"]},
        {"library": "Merged", "type": "anime", "useGenres": True, "useKeywords": True,
         "providers": ["jikan", "anilist"], "providerMode": "merge"},
    ]}))

    warned = {c.id for c in run_doctor(path, Store(tmp_path / "s.db"), check_taxonomy=False).checks
              if c.id.startswith("keywords-behind")}
    assert warned == {"keywords-behind:Behind"}


def test_the_anilist_tag_threshold_is_part_of_the_fingerprint():
    """It decides which tags are written, so moving it has to re-tag."""
    def fingerprint(rank: int) -> str:
        config = AppConfig.model_validate({
            "version": 2,
            "libraries": [{"library": "A", "type": "anime", "useGenres": True,
                           "useKeywords": True, "providers": ["anilist"]}],
            "providers": {"anilist_tag_rank": rank},
        })
        return config.fingerprint(config.libraries[0])

    assert fingerprint(70) != fingerprint(20)
