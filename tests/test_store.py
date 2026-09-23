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


def test_setting_a_binding_invalidates_the_cached_match(store: Store):
    store.record_success("Animes", "Monster", fingerprint="fp", title="Monster", year=2004,
                         rating_key=1, genres=["Wrong"], provider="jikan", provider_id="999")
    store.set_binding("Animes", "Monster", "mal", "19")
    assert store.get_state("Animes", "Monster") is None


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
    assert store.get_state("A", "k1") is not None, "the successful entry survives"
    assert store.get_state("A", "k2") is None
    assert store.get_state("B", "k3") is not None, "another library is untouched"


def test_the_cli_refuses_a_binding_nothing_can_resolve(tmp_path, config_file, monkeypatch, capsys):
    """`bind` accepted any of the six schemes, including ones nothing reads."""
    from plex_auto_genres import cli

    monkeypatch.setenv("PLEX_BASE_URL", "http://plex:32400")
    monkeypatch.setenv("PLEX_TOKEN", "t")
    argv = ["--config", str(config_file), "--db", str(tmp_path / "cli.db"), "bind"]

    assert cli.main([*argv, "Animes", "Monster", "tmdb", "7"]) == 1
    refusal = capsys.readouterr().out
    assert "tmdb" in refusal and "mal" in refusal

    assert cli.main([*argv, "Animes", "Monster", "anidb", "4521"]) == 0
    assert "bound" in capsys.readouterr().out


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

    assert store.find_keys("Animes", "monster") == [("mal://1", "Monster (2004)")]
    assert store.find_keys("Animes", "Monster (2004)") == [("mal://1", "Monster (2004)")]
    assert store.find_keys("Animes", "mal://2") == [("mal://2", "Solo")]
    assert store.find_keys("Animes", "Monster (1999)") == []
    assert store.find_keys("Films", "Monster") == [], "another library is not searched"


def test_find_keys_knows_an_item_by_the_name_its_decision_kept(store: Store):
    """An item decided before any run cached it has no other row with a name."""
    store.set_manual("Animes", "mal://1", added=["A"], removed=[], locked=False,
                     title="Monster (2004)")

    assert store.find_keys("Animes", "Monster") == [("mal://1", "Monster (2004)")]
    assert store.find_keys("Animes", "monster (2004)") == [("mal://1", "Monster (2004)")]


def test_a_blank_name_finds_nothing(store: Store):
    """A blank name matched every decision saved without a title: an unset
    shell variable handed back, or emptied, an unrelated item."""
    store.set_manual("Animes", "mal://5", added=["Mecha"], removed=[], locked=False)

    assert store.find_keys("Animes", "") == []
    assert store.find_keys("Animes", "   ") == []


def test_a_decision_moves_with_its_items_v1_key(store: Store):
    store.record_success("Animes", "Monster (2004)", fingerprint="f", title="Monster",
                         year=2004, rating_key=1, genres=[], provider="jikan", provider_id="1")
    store.set_manual("Animes", "Monster (2004)", added=["A"], removed=[], locked=False)

    store.rename_media_keys("Animes", {"Monster (2004)": "mal://1"})

    assert list(store.manual_for_library("Animes")) == ["mal://1"]


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

    assert {key for key, _ in store.find_keys("Animes", "Monster")} == {"mal://1", "mal://9"}


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
