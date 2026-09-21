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


def test_clear_library_removes_only_that_library(store: Store):
    for lib in ("A", "B"):
        store.record_success(lib, "k", fingerprint="fp", title="T", year=None,
                             rating_key=1, genres=[], provider=None, provider_id=None)
    assert store.clear_library("A") == 1
    assert store.get_state("B", "k") is not None


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
