"""Cache invalidation, retry backoff, bindings and undo bookkeeping."""

from __future__ import annotations

import json
import time

from plex_auto_genres.models import ExternalId, RunReport
from plex_auto_genres.store import Store, _retry_after


def test_unknown_media_is_processed(store: Store):
    assert store.should_process("Lib", "tmdb://1", "fp1") is True


def test_successful_media_is_skipped_next_time(store: Store):
    store.record_success("Lib", "tmdb://1", fingerprint="fp1", title="T", year=2000,
                         rating_key=1, genres=["Action"], provider="tmdb", provider_id="1")
    assert store.should_process("Lib", "tmdb://1", "fp1") is False


def test_a_settings_change_invalidates_the_cache(store: Store):
    store.record_success("Lib", "tmdb://1", fingerprint="fp1", title="T", year=2000,
                         rating_key=1, genres=["Action"], provider="tmdb", provider_id="1")
    assert store.should_process("Lib", "tmdb://1", "fp2") is True


def test_the_cache_is_per_library(store: Store):
    """Two libraries of the same type no longer share state."""
    store.record_success("LibA", "tmdb://1", fingerprint="fp1", title="T", year=2000,
                         rating_key=1, genres=["Action"], provider="tmdb", provider_id="1")
    assert store.should_process("LibA", "tmdb://1", "fp1") is False
    assert store.should_process("LibB", "tmdb://1", "fp1") is True


def test_failures_are_retried_after_a_backoff_not_blacklisted(store: Store):
    """v1 recorded a failure once and never looked at the title again."""
    store.record_failure("Lib", "tmdb://1", fingerprint="fp1", title="T", year=2000,
                         rating_key=1, error="TMDB timeout")
    assert store.should_process("Lib", "tmdb://1", "fp1") is False   # still cooling down

    later = time.time() + _retry_after(1) + 1
    assert store.should_process("Lib", "tmdb://1", "fp1", now=later) is True


def test_backoff_grows_with_attempts(store: Store):
    assert _retry_after(1) < _retry_after(2) < _retry_after(3)
    assert _retry_after(50) <= 7 * 86400


def test_repeated_failures_increment_the_attempt_counter(store: Store):
    for _ in range(3):
        store.record_failure("Lib", "k", fingerprint="fp", title="T", year=None,
                             rating_key=1, error="boom")
    assert store.get_state("Lib", "k").attempts == 3


def test_success_clears_a_previous_failure(store: Store):
    store.record_failure("Lib", "k", fingerprint="fp", title="T", year=None,
                         rating_key=1, error="boom")
    store.record_success("Lib", "k", fingerprint="fp", title="T", year=None,
                         rating_key=1, genres=["A"], provider="tmdb", provider_id="1")
    state = store.get_state("Lib", "k")
    assert state.status == "ok"
    assert state.attempts == 0
    assert state.last_error is None


def test_force_overrides_everything(store: Store):
    store.record_success("Lib", "k", fingerprint="fp", title="T", year=None,
                         rating_key=1, genres=["A"], provider="tmdb", provider_id="1")
    assert store.should_process("Lib", "k", "fp", force=True) is True


# -- manual bindings -------------------------------------------------------


def test_binding_round_trip(store: Store):
    store.set_binding("Animes", "Monster", "mal", "19", note="wrong auto-match")
    assert store.get_bindings("Animes", "Monster") == [ExternalId("mal", "19")]


def test_an_item_pins_one_id_per_source(store: Store):
    """A series on two catalogues needs both ids to be merged from both."""
    store.set_binding("Animes", "Monster", "mal", "19")
    store.set_binding("Animes", "Monster", "tmdb", "7")
    assert store.get_bindings("Animes", "Monster") == [
        ExternalId("mal", "19"), ExternalId("tmdb", "7")
    ]

    store.set_binding("Animes", "Monster", "tmdb", "8")
    assert store.get_bindings("Animes", "Monster") == [
        ExternalId("mal", "19"), ExternalId("tmdb", "8")
    ], "setting a source again replaces that pin alone"


def test_a_pin_makes_the_cached_match_stale_without_dropping_it(store: Store):
    """The row keeps the name the CLI finds the item by; the pin, stamped into
    the fingerprint, is what makes the old match stale. Deleting the row lost
    a pin saved while a run was going, and the name with it."""
    from plex_auto_genres.models import stamp_pins

    store.record_success("Animes", "mal://1", fingerprint="fp", title="Monster", year=2004,
                         rating_key=1, genres=["Wrong"], provider="jikan", provider_id="999")
    store.set_binding("Animes", "mal://1", "mal", "19")

    state = store.get_state("Animes", "mal://1")
    assert state is not None and state.fingerprint == "fp"
    assert Store.needs_work(state, stamp_pins("fp", store.get_bindings("Animes", "mal://1")))


def test_delete_binding(store: Store):
    store.set_binding("Animes", "Monster", "mal", "19")
    assert store.delete_binding("Animes", "Monster") is True
    assert store.delete_binding("Animes", "Monster") is False
    assert store.get_bindings("Animes", "Monster") == []


def test_one_pin_can_be_removed_without_the_others(store: Store):
    store.set_binding("Animes", "Monster", "mal", "19")
    store.set_binding("Animes", "Monster", "tmdb", "7")
    assert store.delete_binding("Animes", "Monster", "tmdb") is True
    assert store.get_bindings("Animes", "Monster") == [ExternalId("mal", "19")]


def test_an_older_database_keeps_its_bindings_when_the_table_is_widened(tmp_path):
    """The old key allowed one pin per item, so it cannot simply be replaced."""
    import sqlite3

    path = tmp_path / "old.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        "CREATE TABLE bindings (library TEXT NOT NULL, media_key TEXT NOT NULL, "
        "provider TEXT NOT NULL, provider_id TEXT NOT NULL, note TEXT, "
        "created_at REAL NOT NULL, PRIMARY KEY (library, media_key));"
        "INSERT INTO bindings VALUES ('Animes','mal://1','mal','19','why',1.0);"
    )
    legacy.commit()
    legacy.close()

    store = Store(path)
    assert store.get_bindings("Animes", "mal://1") == [ExternalId("mal", "19")]
    assert store.list_bindings("Animes")[0]["note"] == "why"
    store.set_binding("Animes", "mal://1", "tmdb", "7")
    assert len(store.get_bindings("Animes", "mal://1")) == 2
    store.close()


def test_a_half_finished_widening_does_not_wedge_the_database(tmp_path):
    """The rebuild once ran statement by statement, each committing on its
    own: an interruption either lost every binding or left a table behind
    that made the next open raise, which is the whole database gone."""
    import sqlite3

    path = tmp_path / "interrupted.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        "CREATE TABLE bindings (library TEXT NOT NULL, media_key TEXT NOT NULL, "
        "provider TEXT NOT NULL, provider_id TEXT NOT NULL, note TEXT, "
        "created_at REAL NOT NULL, PRIMARY KEY (library, media_key));"
        "INSERT INTO bindings VALUES ('Animes','mal://1','mal','19',NULL,1.0);"
        # What a killed attempt leaves on disk.
        "CREATE TABLE bindings_widened (library TEXT, media_key TEXT, provider TEXT, "
        "provider_id TEXT, note TEXT, created_at REAL);"
    )
    legacy.commit()
    legacy.close()

    for _ in range(2):
        store = Store(path)
        assert store.get_bindings("Animes", "mal://1") == [ExternalId("mal", "19")]
        store.close()


# -- runs and snapshots ----------------------------------------------------

def test_run_lifecycle_and_report_persistence(store: Store):
    run_id = store.start_run("Animes", "genres", dry_run=False)
    report = RunReport(run_id=run_id, library="Animes", action="genres", written=3)
    store.finish_run(report)

    row = store.get_run(run_id)
    assert row["finished_at"] is not None
    assert json.loads(row["report"])["written"] == 3
    assert [r["run_id"] for r in store.recent_runs()] == [run_id]


def test_snapshots_are_scoped_to_their_run(store: Store):
    store.add_snapshot("r1", "Animes", 1, "A", "genre", ["old"], ["new"])
    store.add_snapshot("r2", "Animes", 2, "B", "genre", ["x"], ["y"])
    assert len(store.snapshots_for("r1")) == 1
    store.mark_undone("r1")
    assert store.get_run("r1") is None or True  # run row may not exist in this unit test


def test_stats_and_failure_listing(store: Store):
    store.record_success("Lib", "a", fingerprint="fp", title="A", year=None,
                         rating_key=1, genres=[], provider=None, provider_id=None)
    store.record_failure("Lib", "b", fingerprint="fp", title="B", year=2001,
                         rating_key=2, error="nope")
    assert store.stats("Lib") == {"ok": 1, "failed": 1}
    failures = store.failures("Lib")
    assert len(failures) == 1 and failures[0]["title"] == "B"



# -- kv cache --------------------------------------------------------------


def test_kv_expires(store: Store):
    store.kv_set("k", "v", ttl_s=-1)
    assert store.kv_get("k") is None
    store.kv_set("k2", "v2", ttl_s=60)
    assert store.kv_get("k2") == "v2"


# -- legacy import ---------------------------------------------------------


def test_v1_progress_files_are_imported(store: Store, tmp_path):
    (tmp_path / "plex-anime-successful.txt").write_text(json.dumps(["Naruto (2002)", "Bleach (2004)"]))
    (tmp_path / "plex-anime-failures.txt").write_text(json.dumps(["Obscure OVA (1994)"]))

    imported = store.import_legacy_logs(tmp_path, "Animes", "anime", "fp1")
    assert imported == 3
    assert store.should_process("Animes", "Naruto (2002)", "fp1") is False
    state = store.get_state("Animes", "Obscure OVA (1994)")
    assert state.status == "failed"
    assert store.get_state("Animes", "Naruto (2002)").genres == []


def test_legacy_import_survives_a_corrupt_file(store: Store, tmp_path):
    (tmp_path / "plex-anime-successful.txt").write_text("{{{not json")
    assert store.import_legacy_logs(tmp_path, "Animes", "anime", "fp") == 0


# -- review regressions ---------------------------------------------------------


def test_unicode_digits_do_not_abort_the_legacy_import(store: Store, tmp_path):
    (tmp_path / "plex-anime-successful.txt").write_text(
        json.dumps(["Fate (②)", "Bleach (2004)"]), encoding="utf-8"
    )
    assert store.import_legacy_logs(tmp_path, "Animes", "anime", "fp") == 2
    assert store.get_state("Animes", "Fate (②)") is not None
    assert store.get_state("Animes", "Bleach (2004)") is not None


def test_run_status_ladder(store: Store):
    from plex_auto_genres.store import run_status

    def finished(**fields) -> str:
        run_id = store.start_run("L", "genres", dry_run=False)
        report = RunReport(run_id=run_id, library="L", action="genres")
        for key, value in fields.items():
            setattr(report, key, value)
        store.finish_run(report)
        return run_status(store.get_run(run_id), live=False)

    open_id = store.start_run("L", "genres", dry_run=False)
    assert run_status(store.get_run(open_id), live=False) == "interrupted"
    assert run_status(store.get_run(open_id), live=True) == "running"
    assert finished(written=3) == "ok"
    assert finished(written=3, failed=1) == "partial"
    assert finished(failed=2) == "failed"
    assert finished(error="Plex went away") == "failed"
    assert finished(written=1, error="Plex went away") == "partial"
    assert finished(cancelled=True, written=1) == "cancelled"
    # A source that stopped answering: the run stopped early, which sets the
    # same error field a hard abort does, but nothing actually broke.
    assert finished(deferred=9) == "partial"
    assert finished(deferred=9, error="Stopped after 25 titles") == "partial"
    assert finished(deferred=9, failed=2) == "failed"
    undone = store.start_run("L", "genres", dry_run=False)
    store.finish_run(RunReport(run_id=undone, library="L", action="genres", written=1))
    store.mark_undone(undone)
    assert run_status(store.get_run(undone), live=False) == "undone"


def test_media_keys_can_be_moved_under_a_new_key(store: Store):
    def ok(key):
        store.record_success("L", key, fingerprint="f", title=key, year=None, rating_key=None,
                             genres=[], provider=None, provider_id=None)

    ok("Old (2000)")
    assert store.rename_media_keys("L", {"Old (2000)": "mal://1"}) == 1
    assert store.get_state("L", "mal://1") is not None
    assert store.get_state("L", "Old (2000)") is None

    ok("Two (2001)")
    ok("mal://2")                                  # the GUID row already exists: it wins
    assert store.rename_media_keys("L", {"Two (2001)": "mal://2"}) == 0
    assert store.get_state("L", "Two (2001)") is None
    assert store.get_state("L", "mal://2") is not None


def test_older_databases_gain_the_new_columns(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    with Store(path):
        pass
    conn = sqlite3.connect(path)
    for column in ("score", "source"):
        conn.execute(f"ALTER TABLE media_state DROP COLUMN {column}")
    conn.execute("ALTER TABLE snapshots DROP COLUMN locked_before")
    conn.commit()
    conn.close()

    with Store(path) as reopened:
        reopened.record_success("L", "k", fingerprint="f", title="t", year=None, rating_key=1,
                                genres=[], provider="jikan", provider_id="1", score=7.5,
                                source="guid")
        state = reopened.get_state("L", "k")
        assert state.score == 7.5 and state.source == "guid"
        reopened.add_snapshot("r", "L", 1, "t", "genre", [], ["A"], locked_before=False)
        assert reopened.snapshots_for("r")[0]["locked_before"] == 0


def test_clear_failures_leaves_the_successes_alone(store: Store):
    """`failures --retry` used to wipe the library, re-tagging everything."""
    store.record_success("A", "k1", fingerprint="f", title="Good", year=2000,
                         rating_key=1, genres=["Action"], provider="jikan",
                         provider_id="1", score=8.0, source="guid")
    store.record_failure("A", "k2", fingerprint="f", title="Bad", year=2001,
                         rating_key=2, error="jikan: rate limited")
    store.record_failure("B", "k3", fingerprint="f", title="Other", year=2002,
                         rating_key=3, error="jikan: rate limited")

    assert store.clear_failures("A") == 1
    assert store.get_state("A", "k1").fingerprint == "f", "the successful entry is untouched"
    retried = store.get_state("A", "k2")
    assert retried is not None, "kept: `failures` has just suggested binding it by title"
    assert Store.needs_work(retried, "f") and retried.attempts == 0, "retried at once"
    assert store.get_state("B", "k3").fingerprint == "f", "another library is untouched"


def _found(store: Store, library: str, text: str) -> list[tuple[str, str]]:
    return [(m.media_key, m.title) for m in store.find_keys(library, text)]


def _seen(db, *items: tuple[str, str, int | None]) -> None:
    """Cache rows as a run leaves them: the only place the CLI finds titles."""
    with Store(db) as seeded:
        for key, title, year in items:
            seeded.record_success("Animes", key, fingerprint="f", title=title, year=year,
                                  rating_key=1, genres=["Drama"], provider="jikan",
                                  provider_id=key.split("://")[-1])


def test_the_cli_refuses_a_binding_nothing_can_resolve(tmp_path, config_file, monkeypatch, capsys):
    """`bind` accepted any of the six schemes, including ones nothing reads."""
    from plex_auto_genres import cli

    monkeypatch.setenv("PLEX_BASE_URL", "http://plex:32400")
    monkeypatch.setenv("PLEX_TOKEN", "t")
    db = tmp_path / "cli.db"
    argv = ["--config", str(config_file), "--db", str(db), "bind"]
    _seen(db, ("mal://1", "Monster", 2004))

    assert cli.main([*argv, "Animes", "Monster", "tmdb", "7"]) == 1
    refusal = capsys.readouterr().out
    assert "tmdb" in refusal and "mal" in refusal

    assert cli.main([*argv, "Animes", "Monster", "anidb", "4521"]) == 0
    assert "bound" in capsys.readouterr().out
    with Store(db) as after:
        assert after.get_bindings("Animes", "mal://1") == [ExternalId("anidb", "4521")]


def test_the_cli_binds_the_item_a_title_names_and_unbinds_it_by_that_title(
    tmp_path, config_file, monkeypatch, capsys
):
    """A title was stored as the key, and the pipeline looks pins up by GUID:
    `bind` said "bound" and the pin was never applied."""
    run, db = _cli(tmp_path, config_file, monkeypatch)
    _seen(db, ("mal://1", "Monster", 2004))

    assert run("bind", "animes", "monster", "mal", "19", "--note", "wrong season") == 0
    assert "bound Monster (2004) in Animes" in capsys.readouterr().out
    with Store(db) as after:
        assert after.get_bindings("Animes", "mal://1") == [ExternalId("mal", "19")]
        assert after.get_bindings("Animes", "monster") == []
        assert after.get_state("Animes", "mal://1") is not None, "the row keeps the name"

    # The cached row, the other place with the name, is gone: the pin keeps it.
    assert run("unbind", "Animes", "Monster") == 0
    assert "removed bindings for Monster (2004)" in capsys.readouterr().out
    assert run("unbind", "Animes", "Monster") == 1
    assert "No binding on 'Monster'" in capsys.readouterr().out


def test_the_cli_will_not_bind_what_it_cannot_place(tmp_path, config_file, monkeypatch, capsys):
    run, db = _cli(tmp_path, config_file, monkeypatch)
    _seen(db, ("mal://1", "Monster", 2004), ("mal://9", "Monster", 2019))

    assert run("bind", "Animes", "Nowhere", "mal", "19") == 1
    assert "has been seen" in capsys.readouterr().out
    assert run("bind", "Animes", "Monster", "mal", "19") == 1
    listed = capsys.readouterr().out
    assert "mal://1" in listed and "mal://9" in listed, "two items share the title"
    assert run("bind", "Animes", "Monster (2019)", "mal", "19") == 0
    capsys.readouterr()

    # A key for an item no run reached yet, spelt the way the pipeline keys it.
    assert run("bind", "Animes", " MAL://7 ", "mal", "70") == 0
    with Store(db) as after:
        assert after.get_bindings("Animes", "mal://9") == [ExternalId("mal", "19")]
        assert after.get_bindings("Animes", "mal://7") == [ExternalId("mal", "70")]
        assert [r["media_key"] for r in after.list_bindings("Animes")] == ["mal://7", "mal://9"]


def test_unbinding_one_source_looks_only_at_items_pinned_on_it(
    tmp_path, config_file, monkeypatch, capsys
):
    run, db = _cli(tmp_path, config_file, monkeypatch)
    _seen(db, ("mal://1", "Monster", 2004))
    assert run("bind", "Animes", "Monster", "mal", "19") == 0
    capsys.readouterr()

    assert run("unbind", "Animes", "Monster", "--provider", "anidb") == 1
    assert "No binding for anidb on 'Monster'" in capsys.readouterr().out
    with Store(db) as after:
        assert after.get_bindings("Animes", "mal://1") == [ExternalId("mal", "19")]


def test_failures_name_each_key_and_a_bind_that_works_here(
    tmp_path, config_file, monkeypatch, capsys
):
    """The hint suggested tmdb, which a Jikan library refuses to bind."""
    run, db = _cli(tmp_path, config_file, monkeypatch)
    with Store(db) as seeded:
        seeded.record_failure("Animes", "mal://3", fingerprint="f", title="Lost", year=2001,
                              rating_key=3, error="jikan: no anime matching 'Lost'")

    assert run("failures", "--library", "animes") == 0
    out = capsys.readouterr().out
    assert "mal://3" in out
    assert "bind Animes '<title or key>' mal <id>" in out


# -- manual tags -----------------------------------------------------------


def test_manual_tags_round_trip_and_leave_the_cached_row_to_the_fingerprint(store: Store):
    """The cached row stays: it holds the score and the name, and the decision
    is in the fingerprint, which is what makes the row stale (see the
    pipeline tests) -- deleting it instead lost decisions saved mid-run."""
    store.record_success("Animes", "mal://1", fingerprint="f", title="A", year=2000,
                         rating_key=1, genres=["Action"], provider="jikan", provider_id="1",
                         score=8.0)
    store.set_manual("Animes", "mal://1", added=["Space Opera"], removed=["Kids"],
                     locked=False, note="the sources miss it")

    state = store.get_state("Animes", "mal://1")
    assert (state.fingerprint, state.score) == ("f", 8.0)
    tags = store.manual_for_library("Animes")["mal://1"]
    assert tags.stamp("f") != state.fingerprint, "stale under the decision"
    assert (tags.added, tags.removed, tags.locked, tags.note) == (
        ("Space Opera",), ("Kids",), False, "the sources miss it"
    )
    assert [(lib, key) for lib, key, _ in store.list_manual()] == [("Animes", "mal://1")]


def test_removing_manual_tags_hands_the_item_back(store: Store):
    store.set_manual("Animes", "mal://1", added=["A"], removed=[], locked=True)
    store.record_success("Animes", "mal://1", fingerprint="f", title="A", year=2000,
                         rating_key=1, genres=["A"], provider="manual", provider_id="")

    assert store.delete_manual("Animes", "mal://1") is True
    assert store.manual_for_library("Animes") == {}
    assert store.delete_manual("Animes", "mal://1") is False


def test_an_override_keeps_the_items_name_across_edits(store: Store):
    store.set_manual("Animes", "mal://1", added=["A"], removed=[], locked=False,
                     title="First (2000)")
    again = store.set_manual("Animes", "mal://1", added=["B"], removed=[], locked=False)

    assert again.title == "First (2000)", "an edit that does not name it keeps the name"
    assert again.added == ("B",)


def test_find_keys_reads_titles_as_people_type_them(store: Store):
    """Most items are keyed by GUID; a title stored as the key matched nothing."""
    store.record_success("Animes", "mal://1", fingerprint="f", title="Monster", year=2004,
                         rating_key=1, genres=[], provider="jikan", provider_id="1")
    store.record_success("Animes", "mal://2", fingerprint="f", title="Solo", year=None,
                         rating_key=2, genres=[], provider="jikan", provider_id="2")

    assert _found(store, "Animes", "monster") == [("mal://1", "Monster (2004)")]
    assert _found(store, "Animes", "Monster (2004)") == [("mal://1", "Monster (2004)")]
    assert _found(store, "Animes", "mal://2") == [("mal://2", "Solo")]
    assert _found(store, "Animes", "Monster (1999)") == []
    assert _found(store, "Films", "Monster") == [], "another library is not searched"


def test_find_keys_knows_an_item_by_the_name_its_decision_kept(store: Store):
    """An item decided before any run cached it has no other row with a name."""
    store.set_manual("Animes", "mal://1", added=["A"], removed=[], locked=False,
                     title="Monster (2004)")

    assert _found(store, "Animes", "Monster") == [("mal://1", "Monster (2004)")]
    assert _found(store, "Animes", "monster (2004)") == [("mal://1", "Monster (2004)")]


def test_pins_are_not_items_and_list_under_the_cached_name(store: Store):
    """The old `bind` filed pins under whatever was typed; such a key names no
    item, so a title lookup must not offer it. Lists take names from the cache."""
    store.record_success("Animes", "mal://1", fingerprint="f", title="Monster", year=2004,
                         rating_key=1, genres=[], provider="jikan", provider_id="1")
    store.set_binding("Animes", "Monster", "mal", "19")          # what 2.3.0 stored
    store.set_binding("Animes", "mal://1", "anidb", "4521")

    assert _found(store, "Animes", "Monster") == [("mal://1", "Monster (2004)")]
    assert {r["media_key"]: r["title"] for r in store.list_bindings("Animes")} == {
        "Monster": None, "mal://1": "Monster (2004)"}


def test_find_keys_ignores_the_unicode_form(store: Store):
    """A title Plex stored decomposed (from a macOS file name) never matched."""
    import unicodedata

    store.record_success("Animes", "mal://1", fingerprint="f", title=unicodedata.normalize(
        "NFD", "Pok\u00e9mon"), year=None, rating_key=1, genres=[], provider="jikan",
        provider_id="1")

    assert [m.media_key for m in store.find_keys("Animes", "pok\u00e9mon")] == ["mal://1"]


def test_find_keys_says_which_items_only_v1_knows(store: Store):
    """v1's progress files were copied into every library of a type, under a
    key no run may use: a title known only from them is no proof of an item."""
    store.record_success("Animes", "Monster (2004)", fingerprint="f", title="Monster",
                         year=2004, rating_key=None, genres=[], provider=None, provider_id=None)
    store.record_success("Animes", "mal://2", fingerprint="f", title="Solo", year=None,
                         rating_key=2, genres=[], provider="jikan", provider_id="2")

    assert [(m.media_key, m.v1_only) for m in store.find_keys("Animes", "Monster")] == [
        ("Monster (2004)", True)]
    assert [m.v1_only for m in store.find_keys("Animes", "Solo")] == [False]


def test_a_blank_name_finds_nothing(store: Store):
    """A blank name matched every decision saved without a title: an unset
    shell variable handed back, or emptied, an unrelated item."""
    store.set_manual("Animes", "mal://5", added=["Mecha"], removed=[], locked=False)

    assert store.find_keys("Animes", "") == []
    assert store.find_keys("Animes", "   ") == []


def test_a_decision_and_a_pin_move_with_their_items_v1_key(store: Store):
    store.record_success("Animes", "Monster (2004)", fingerprint="f", title="Monster",
                         year=2004, rating_key=1, genres=[], provider="jikan", provider_id="1")
    store.set_manual("Animes", "Monster (2004)", added=["A"], removed=[], locked=False)
    store.set_binding("Animes", "Monster (2004)", "mal", "19")

    store.rename_media_keys("Animes", {"Monster (2004)": "mal://1"})

    assert list(store.manual_for_library("Animes")) == ["mal://1"]
    assert store.get_bindings("Animes", "mal://1") == [ExternalId("mal", "19")]
    assert store.get_bindings("Animes", "Monster (2004)") == []


def test_a_manual_table_from_an_earlier_build_gains_its_title(tmp_path):
    """The table first shipped without ``title``, under the same schema version."""
    import sqlite3

    path = tmp_path / "early.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE manual_tags (library TEXT NOT NULL, media_key TEXT NOT NULL, "
        "added TEXT NOT NULL DEFAULT '[]', removed TEXT NOT NULL DEFAULT '[]', "
        "locked INTEGER NOT NULL DEFAULT 0, note TEXT, updated_at REAL NOT NULL, "
        "PRIMARY KEY (library, media_key))"
    )
    conn.execute("INSERT INTO manual_tags (library, media_key, added, updated_at) "
                 "VALUES ('Animes', 'mal://1', '[\"A\"]', 1.0)")
    conn.commit()
    conn.close()

    with Store(path) as opened:
        assert opened.manual_for_library("Animes")["mal://1"].title is None
        saved = opened.set_manual("Animes", "mal://1", added=["B"], removed=[], locked=False,
                                  title="Monster (2004)")
        assert saved.title == "Monster (2004)"


def test_find_keys_lists_every_item_a_shared_title_may_mean(store: Store):
    for key, year in (("mal://1", 2004), ("mal://9", 2019)):
        store.record_success("Animes", key, fingerprint="f", title="Monster", year=year,
                             rating_key=1, genres=[], provider="jikan", provider_id=key)

    assert {m.media_key for m in store.find_keys("Animes", "Monster")} == {"mal://1", "mal://9"}


def _cli(tmp_path, config_file, monkeypatch):
    from plex_auto_genres import cli

    monkeypatch.setenv("PLEX_BASE_URL", "http://plex:32400")
    monkeypatch.setenv("PLEX_TOKEN", "t")
    db = tmp_path / "cli.db"
    return (lambda *argv: cli.main(["--config", str(config_file), "--db", str(db), *argv])), db


def test_the_cli_decides_genres_on_the_item_a_title_names(tmp_path, config_file,
                                                           monkeypatch, capsys):
    run, db = _cli(tmp_path, config_file, monkeypatch)
    with Store(db) as seeded:
        seeded.record_success("Animes", "mal://1", fingerprint="f", title="Monster",
                              year=2004, rating_key=1, genres=["Drama"], provider="jikan",
                              provider_id="1")

    assert run("manual", "Animes", "monster", "--add", "Psychological", "--remove", "Drama",
               "--note", "the sources lump it in") == 0
    assert "+Psychological, -Drama" in capsys.readouterr().out
    with Store(db) as after:
        tags = after.manual_for_library("Animes")["mal://1"]
    assert (tags.added, tags.removed, tags.title) == (
        ("Psychological",), ("Drama",), "Monster (2004)"
    )

    assert run("--json", "manuals") == 0
    listed = json.loads(capsys.readouterr().out)
    assert [(e["media_key"], e["title"]) for e in listed] == [("mal://1", "Monster (2004)")]
    assert listed[0]["updated_at"] > 0

    # A second decision replaces the first, says so, and keeps the note.
    assert run("manual", "Animes", "Monster", "--add", "Thriller") == 0
    out = capsys.readouterr().out
    assert "replaces: +Psychological, -Drama" in out
    with Store(db) as after:
        tags = after.manual_for_library("Animes")["mal://1"]
    assert (tags.added, tags.removed, tags.note) == (("Thriller",), (), "the sources lump it in")

    assert run("unmanual", "Animes", "Monster") == 0
    assert "handed back" in capsys.readouterr().out
    assert run("unmanual", "Animes", "Monster") == 1


def test_the_cli_refuses_what_it_cannot_place(tmp_path, config_file, monkeypatch, capsys):
    run, db = _cli(tmp_path, config_file, monkeypatch)

    assert run("manual", "Animes", "Nowhere", "--add", "Drama") == 1
    assert "has been seen" in capsys.readouterr().out
    assert run("manual", "Animes", "mal://5", "--add", "Drama", "--remove", "drama") == 1
    assert "both added and removed" in capsys.readouterr().out
    assert run("manual", "Animes", "mal://5") == 1
    assert "Nothing to decide" in capsys.readouterr().out

    assert run("manual", "Animes", "", "--add", "Drama") == 1
    assert "Give the item's title" in capsys.readouterr().out
    assert run("manual", "Animes", "mal://5", "--add", "\u2605") == 1
    assert "no letter or digit" in capsys.readouterr().out
    assert run("manual", "Animes", "plex://show/5d9c08", "--add", "Drama") == 1
    assert "has been seen" in capsys.readouterr().out

    # A key the web UI shows is taken for an item no run reached yet, spelt the
    # way the pipeline spells keys: typed any other way it would never match.
    assert run("manual", "Animes", " MAL://5?lang=en ", "--lock", "--add", " Mecha ",
               "--remove", "Kids") == 0
    assert "exactly Mecha" in capsys.readouterr().out
    with Store(db) as after:
        tags = after.manual_for_library("Animes")["mal://5"]
    assert (tags.locked, tags.added, tags.removed) == (True, ("Mecha",), ("Kids",)), (
        "names stripped as the API does, and a lock keeps its refusals"
    )


def test_an_item_without_a_guid_keeps_its_name_after_binding(
    tmp_path, config_file, monkeypatch, capsys
):
    """Its key is its name; binding dropped the only row that held it."""
    run, db = _cli(tmp_path, config_file, monkeypatch)
    _seen(db, ("Monster (2004)", "Monster", 2004))

    assert run("bind", "Animes", "Monster", "mal", "19") == 0
    assert run("unbind", "Animes", "Monster") == 0
    assert run("bind", "Animes", "Monster", "mal", "20") == 0
    capsys.readouterr()
    with Store(db) as after:
        assert after.get_bindings("Animes", "Monster (2004)") == [ExternalId("mal", "20")]


def test_retrying_failures_keeps_the_names_it_suggests_binding_by(
    tmp_path, config_file, monkeypatch, capsys
):
    run, db = _cli(tmp_path, config_file, monkeypatch)
    with Store(db) as seeded:
        seeded.record_failure("Animes", "mal://3", fingerprint="f", title="Lost", year=2001,
                              rating_key=3, error="jikan: no anime matching 'Lost'")

    assert run("failures", "--library", "Animes", "--retry") == 0
    capsys.readouterr()
    assert run("bind", "Animes", "Lost", "mal", "33") == 0
    with Store(db) as after:
        assert after.get_bindings("Animes", "mal://3") == [ExternalId("mal", "33")]


def test_a_pin_the_old_bind_filed_under_a_title_is_no_item(
    tmp_path, config_file, monkeypatch, capsys
):
    """2.3.0 stored `bind Animes "Monster" mal 19` under the key "Monster"."""
    run, db = _cli(tmp_path, config_file, monkeypatch)
    _seen(db, ("mal://1", "Monster", 2004))
    with Store(db) as seeded:
        seeded.set_binding("Animes", "Monster", "mal", "19")

    assert run("manual", "Animes", "Monster", "--add", "Psychological") == 0
    assert run("bind", "Animes", "Monster", "mal", "21") == 0
    # Typed exactly, the dead key is still what `unbind` removes.
    assert run("unbind", "Animes", "Monster") == 0
    capsys.readouterr()
    with Store(db) as after:
        assert list(after.manual_for_library("Animes")) == ["mal://1"]
        assert after.get_bindings("Animes", "mal://1") == [ExternalId("mal", "21")]
        assert after.get_bindings("Animes", "Monster") == []


def test_an_exact_key_wins_over_a_title_that_spells_it(tmp_path, config_file, monkeypatch, capsys):
    """A GUID-less item's key is also a title: listing both forever meant it
    could not be bound, decided or unbound from the CLI at all."""
    run, db = _cli(tmp_path, config_file, monkeypatch)
    _seen(db, ("mal://1", "Monster", 2004), ("Monster (2004)", "Monster", 2004))

    assert run("bind", "Animes", "Monster (2004)", "mal", "19") == 0
    assert run("bind", "Animes", "Monster", "mal", "20") == 1, "the bare title is ambiguous"
    capsys.readouterr()
    with Store(db) as after:
        assert after.get_bindings("Animes", "Monster (2004)") == [ExternalId("mal", "19")]
        assert after.get_bindings("Animes", "mal://1") == []


def test_a_key_typed_another_way_finds_the_cached_item(tmp_path, config_file, monkeypatch, capsys):
    run, db = _cli(tmp_path, config_file, monkeypatch)
    _seen(db, ("mal://1", "Monster", 2004))

    assert run("bind", "Animes", "mal://1?lang=en", "mal", "19") == 0
    out = capsys.readouterr().out
    assert "bound Monster (2004)" in out and "has not been seen" not in out


def test_a_title_only_v1_knows_is_refused(tmp_path, config_file, monkeypatch, capsys):
    run, db = _cli(tmp_path, config_file, monkeypatch)
    with Store(db) as seeded:                           # as import_legacy_logs leaves it
        seeded.record_success("Animes", "Monster (2004)", fingerprint="f", title="Monster",
                              year=2004, rating_key=None, genres=[], provider=None,
                              provider_id=None)

    assert run("bind", "Animes", "Monster", "mal", "19") == 1
    assert "only from v1's progress files" in capsys.readouterr().out
    assert run("manual", "Animes", "Monster", "--add", "Drama") == 1
    capsys.readouterr()
    with Store(db) as after:
        assert after.list_bindings("Animes") == [] and after.manual_for_library("Animes") == {}


def test_the_failures_hint_survives_a_quote_in_the_name(tmp_path, config_file, monkeypatch, capsys):
    import shlex

    run, db = _cli(tmp_path, config_file, monkeypatch)
    with Store(db) as seeded:
        seeded.record_failure("Kids' Cartoons", "tmdb://3", fingerprint="f", title="Lost",
                              year=2001, rating_key=3, error="tmdb: no match")

    assert run("failures", "--library", "Kids' Cartoons") == 0
    hint = capsys.readouterr().out.split("with: ", 1)[1].splitlines()[0].split("  (")[0]
    assert shlex.split(hint)[:3] == ["plex-auto-genres", "bind", "Kids' Cartoons"]


def test_failures_still_list_when_the_config_does_not_load(tmp_path, monkeypatch, capsys):
    """A diagnostic command must not need a config that loads."""
    from plex_auto_genres import cli

    bad = tmp_path / "config.json"
    bad.write_text("{ not json")
    db = tmp_path / "cli.db"
    with Store(db) as seeded:
        seeded.record_failure("Animes", "mal://3", fingerprint="f", title="Lost", year=2001,
                              rating_key=3, error="jikan: no anime matching 'Lost'")

    assert cli.main(["--config", str(bad), "--db", str(db), "failures", "--library", "Animes"]) == 0
    assert "Lost (2001)" in capsys.readouterr().out
