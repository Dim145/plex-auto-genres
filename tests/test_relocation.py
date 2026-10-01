"""The database leaves logs/ for its place beside the config, and never silently starts empty."""

from __future__ import annotations

import errno
import json
import logging
import os
import shutil
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from plex_auto_genres import cli, migration
from plex_auto_genres.errors import PagError
from plex_auto_genres.migration import LEGACY_DB, relocate_database
from plex_auto_genres.server import create_app
from plex_auto_genres.server.auth import AuthSettings
from plex_auto_genres.store import DB_FILENAME, Store, default_db_path

CONFIG = {
    "version": 2,
    "libraries": [{"library": "Anime", "type": "anime", "useGenres": True}],
}

as_root = pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                             reason="root reads and writes through any permission")


@pytest.fixture
def install(tmp_path, monkeypatch):
    """A 2.4-style install: config in config/, the database in logs/ under the cwd."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PAG_LEGACY_DB", raising=False)
    monkeypatch.setattr(migration, "LOCK_WAIT_S", 0.05)
    config = tmp_path / "config" / "config.json"
    config.parent.mkdir()
    config.write_text(json.dumps(CONFIG))
    (tmp_path / "logs").mkdir()
    return config


@pytest.fixture
def quiet_log(caplog):
    """Everything the migration logs, whatever an earlier test set its level to."""
    with caplog.at_level(logging.DEBUG, logger="plex_auto_genres.migration"):
        yield caplog


def remember_some(store: Store) -> None:
    """What only a person can put in the database, plus the login secret."""
    store.set_binding("Anime", "mal://1", "mal", "1", note="the right one")
    store.set_manual("Anime", "mal://2", added=["Drama"], removed=["Kids"], locked=False,
                     title="Some Show (2004)")
    store.kv_set("auth_secret_v1", "abc123")


def assert_remembered(path) -> None:
    with Store(path) as store:
        assert [(r["media_key"], r["provider_id"], r["note"]) for r in store.list_bindings()] \
            == [("mal://1", "1", "the right one")]
        decisions = store.manual_for_library("Anime")
        assert decisions["mal://2"].added == ("Drama",)
        assert decisions["mal://2"].removed == ("Kids",)
        assert store.kv_get("auth_secret_v1") == "abc123"


def no_leftovers(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if ".partial" in p.name)


def test_the_default_database_sits_beside_the_config(tmp_path):
    assert default_db_path(tmp_path / "config" / "config.json") == tmp_path / "config" / DB_FILENAME


def test_an_old_database_moves_beside_the_config(install, tmp_path, quiet_log):
    with Store(LEGACY_DB) as store:
        remember_some(store)
    LEGACY_DB.chmod(0o640)
    target = default_db_path(install)

    kept = relocate_database(target, install)

    assert_remembered(target)
    # The old file stays as a backup, under a name nothing opens.
    assert kept == tmp_path / "logs" / f"{DB_FILENAME}.moved"
    assert kept.is_file()
    assert not (tmp_path / "logs" / DB_FILENAME).exists()
    assert no_leftovers(target.parent) == []
    assert target.stat().st_mode & 0o777 == 0o640, "the permissions the old file had"
    assert "Moved the state database" in quiet_log.text
    assert f"The old file is kept as {DB_FILENAME}.moved." in quiet_log.text
    # Done once: the next start has nothing to do and says nothing.
    quiet_log.clear()
    assert relocate_database(target, install) is None
    assert quiet_log.text == ""


def test_the_move_carries_what_only_the_wal_held(install, tmp_path):
    # A process killed mid-run leaves its last writes in the -wal file only.
    live = tmp_path / "live" / DB_FILENAME
    store = Store(live)
    remember_some(store)
    wal = live.with_name(live.name + "-wal")
    assert wal.stat().st_size > 0, "the writes must still be in the WAL for this to mean anything"
    shutil.copy2(live, LEGACY_DB)
    shutil.copy2(wal, LEGACY_DB.with_name(LEGACY_DB.name + "-wal"))
    store.close()

    relocate_database(default_db_path(install), install)

    assert_remembered(default_db_path(install))
    assert not LEGACY_DB.with_name(LEGACY_DB.name + "-wal").exists()
    assert no_leftovers(default_db_path(install).parent) == []


def test_an_empty_database_beside_the_config_gives_way(install, quiet_log):
    # A `doctor` run before /logs was mounted made one; it must not hide the real one.
    with Store(LEGACY_DB) as store:
        remember_some(store)
    target = default_db_path(install)
    with Store(target) as store:
        store.kv_set("mal_taxonomy_v1", "[]")  # caches do not make a database worth keeping

    relocate_database(target, install)

    assert_remembered(target)
    assert "Removed the empty database" in quiet_log.text


def test_a_database_with_data_beside_the_config_wins(install, quiet_log):
    with Store(LEGACY_DB) as store:
        remember_some(store)
    target = default_db_path(install)
    with Store(target) as store:
        store.set_binding("Anime", "mal://9", "mal", "9")

    assert relocate_database(target, install) is None

    with Store(target) as store:
        assert [r["media_key"] for r in store.list_bindings()] == ["mal://9"]
    assert LEGACY_DB.is_file(), "the old one is left for its owner to decide about"
    # Both are named, with what each holds, and the way to keep the older one.
    assert "Two state databases" in quiet_log.text
    assert "(1 binding)" in quiet_log.text
    assert "(1 binding, 1 decision made by hand)" in quiet_log.text
    assert "delete plex-auto-genres.db and its -wal and -shm files" in quiet_log.text


@pytest.mark.parametrize("stranger", ["another app's database", "not a database at all"])
def test_a_file_that_is_not_one_of_ours_is_never_taken_for_empty(install, quiet_log, stranger):
    with Store(LEGACY_DB) as store:
        remember_some(store)
    target = default_db_path(install)
    if stranger == "another app's database":
        with sqlite3.connect(target) as conn:
            conn.execute("CREATE TABLE notes (body TEXT)")
            conn.execute("INSERT INTO notes VALUES ('keep me')")
        conn.close()
    else:
        target.write_bytes(b"something else entirely" * 50)
    before = target.read_bytes()

    # Starting on it would write this app's tables into someone else's file,
    # and run without the bindings that wait in logs/.
    with pytest.raises(PagError, match="is not one of this app's databases"):
        relocate_database(target, install)

    assert target.read_bytes() == before
    assert LEGACY_DB.is_file()


def test_a_zero_byte_file_beside_the_config_gives_way(install):
    # `touch`, a probe that only connected, a kill during the first creation.
    with Store(LEGACY_DB) as store:
        remember_some(store)
    target = default_db_path(install)
    target.touch()

    relocate_database(target, install)

    assert_remembered(target)


def test_a_cli_install_without_a_config_directory_still_moves(tmp_path, monkeypatch):
    # `run --library X --type T` and `bind` never needed a config file.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(migration, "LOCK_WAIT_S", 0.05)
    with Store(LEGACY_DB) as store:
        remember_some(store)

    assert cli.main(["bindings"]) == 0

    assert_remembered(tmp_path / "config" / DB_FILENAME)


@pytest.mark.parametrize("db", ["default", "chosen by hand"])
def test_a_logs_that_is_a_file_or_unrelated_blocks_nothing(tmp_path, monkeypatch, db):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "logs").write_text("an unrelated file that happens to be called logs")
    config = tmp_path / "config" / "config.json"
    target = default_db_path(config) if db == "default" else tmp_path / "var" / "state.db"

    assert relocate_database(target, config) is None


def test_one_directory_mounted_at_both_places_is_left_alone(install, quiet_log):
    target = default_db_path(install)
    with Store(target) as store:
        remember_some(store)
    os.link(target, LEGACY_DB)  # what the same host directory at /config and /logs looks like

    assert relocate_database(target, install) is None

    assert LEGACY_DB.is_file()
    assert quiet_log.text == "", "never tell anyone to delete the only copy"
    assert_remembered(target)


def test_a_database_chosen_by_hand_stays_where_it_points(install, tmp_path, quiet_log):
    with Store(LEGACY_DB) as store:
        remember_some(store)
    elsewhere = tmp_path / "var" / "state.db"

    assert relocate_database(elsewhere, install) is None

    assert not elsewhere.exists()
    assert LEGACY_DB.is_file()
    assert [r.levelno for r in quiet_log.records if "so it was not moved" in r.getMessage()] \
        == [logging.WARNING]


def test_the_image_names_where_the_old_database_is(install, tmp_path, monkeypatch):
    # A container given another working directory must still find /logs.
    old = tmp_path / "volumes" / "logs" / DB_FILENAME
    with Store(old) as store:
        remember_some(store)
    monkeypatch.setenv("PAG_LEGACY_DB", str(old))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    relocate_database(default_db_path(install), install)

    assert_remembered(default_db_path(install))
    assert not old.exists()


def test_a_file_that_is_not_a_database_stops_the_start(install):
    LEGACY_DB.write_bytes(b"this is not a database" * 100)
    target = default_db_path(install)

    with pytest.raises(PagError, match="is damaged"):
        relocate_database(target, install)

    # Nothing changed: no empty database in its place, the old file untouched.
    assert not target.exists()
    assert LEGACY_DB.read_bytes().startswith(b"this is not a database")
    assert no_leftovers(target.parent) == []


def test_a_damaged_database_stops_the_start(install):
    with Store(LEGACY_DB) as store:
        for n in range(400):
            store.set_binding("Anime", f"mal://{n}", "mal", str(n), note="x" * 200)
    with sqlite3.connect(LEGACY_DB) as conn:
        conn.execute("PRAGMA journal_mode=DELETE")  # everything in the one file
        pages = conn.execute("PRAGMA page_count").fetchone()[0]
        size = conn.execute("PRAGMA page_size").fetchone()[0]
    with LEGACY_DB.open("r+b") as raw:  # a page in the middle, header intact
        raw.seek((pages // 2) * size)
        raw.write(b"\xff" * 64)

    # Which step notices depends on the SQLite version: the copy's check, or
    # an earlier read. The verdict is the same.
    with pytest.raises(PagError, match="is damaged"):
        relocate_database(default_db_path(install), install)

    assert not default_db_path(install).exists()
    assert LEGACY_DB.is_file()
    assert no_leftovers(default_db_path(install).parent) == []


def test_a_copy_that_does_not_check_out_stops_the_start(install, monkeypatch):
    with Store(LEGACY_DB) as store:
        remember_some(store)
    monkeypatch.setattr(migration, "_quick_check", lambda conn: "Page 7: never used")

    with pytest.raises(PagError, match="does not check out: Page 7: never used"):
        relocate_database(default_db_path(install), install)

    assert not default_db_path(install).exists()
    assert LEGACY_DB.is_file()
    assert no_leftovers(default_db_path(install).parent) == []


@as_root
def test_a_config_directory_the_app_cannot_write_stops_the_move(install, tmp_path):
    with Store(LEGACY_DB) as store:
        remember_some(store)
    (tmp_path / "config").chmod(0o555)
    try:
        with pytest.raises(PagError, match="could not be moved .* writable"):
            relocate_database(default_db_path(install), install)
    finally:
        (tmp_path / "config").chmod(0o755)
    assert LEGACY_DB.is_file()


def test_an_old_database_still_open_elsewhere_is_not_moved(install):
    # The previous container still running: what it writes next would be lost.
    with Store(LEGACY_DB) as store:
        remember_some(store)
        with pytest.raises(PagError, match="open in another process"):
            relocate_database(default_db_path(install), install)

    assert not default_db_path(install).exists()
    assert LEGACY_DB.is_file()


def test_a_wal_left_by_a_vanished_database_is_set_aside(install, tmp_path, quiet_log):
    with Store(LEGACY_DB) as store:
        remember_some(store)
    target = default_db_path(install)
    # Someone deleted the .db of an earlier attempt and left its -wal behind.
    other = tmp_path / "other" / DB_FILENAME
    leftover = Store(other)
    leftover.kv_set("x", "y")
    shutil.copy2(other.with_name(other.name + "-wal"), target.with_name(target.name + "-wal"))
    leftover.close()

    relocate_database(target, install)

    assert_remembered(target)
    assert target.with_name(f"{DB_FILENAME}-wal.orphaned").is_file()
    assert "Set plex-auto-genres.db-wal aside" in quiet_log.text


@as_root
def test_an_unreadable_logs_directory_stops_the_start(install, tmp_path):
    logs = tmp_path / "logs"
    with Store(LEGACY_DB) as store:
        remember_some(store)
    logs.chmod(0)
    try:
        with pytest.raises(PagError, match="Cannot look into"):
            relocate_database(default_db_path(install), install)
    finally:
        logs.chmod(0o755)
    assert not default_db_path(install).exists()


@as_root
def test_an_unreadable_logs_directory_does_not_block_a_database_chosen_by_hand(install, tmp_path):
    logs = tmp_path / "logs"
    logs.chmod(0)
    try:
        assert relocate_database(tmp_path / "var" / "state.db", install) is None
    finally:
        logs.chmod(0o755)


def test_a_source_that_vanished_is_not_recreated_empty(tmp_path):
    gone = tmp_path / "gone.db"
    with pytest.raises(sqlite3.OperationalError):
        migration._copy_database(gone, tmp_path / "x.db")  # pylint: disable=protected-access
    assert not gone.exists()
    assert no_leftovers(tmp_path) == []


def test_a_copy_never_replaces_a_database_already_in_place(tmp_path):
    partial, target = tmp_path / ".x.partial", tmp_path / "x.db"
    partial.write_text("late copy")
    target.write_text("live database")

    assert migration._publish(partial, target) is False  # pylint: disable=protected-access
    assert target.read_text() == "live database"
    assert not partial.exists()


def test_without_hard_links_the_copy_still_lands(tmp_path, monkeypatch):
    def no_links(*_args):
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(migration.os, "link", no_links)
    partial, target = tmp_path / ".x.partial", tmp_path / "x.db"
    partial.write_text("copy")

    assert migration._publish(partial, target) is True  # pylint: disable=protected-access
    assert target.read_text() == "copy"
    assert not partial.exists()


def test_an_old_file_that_cannot_be_renamed_still_counts_as_moved(install, monkeypatch, quiet_log):
    with Store(LEGACY_DB) as store:
        remember_some(store)
    real_replace = Path.replace

    def read_only_logs(self, target):
        if self.parent.name == "logs":
            raise PermissionError(errno.EACCES, "Permission denied")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", read_only_logs)

    assert relocate_database(default_db_path(install), install) is None

    assert_remembered(default_db_path(install))
    assert "Could not rename" in quiet_log.text
    assert "Moved the state database" in quiet_log.text


def test_losing_the_race_to_another_process_is_not_an_error(install, monkeypatch):
    with Store(LEGACY_DB) as store:
        remember_some(store)
    target = default_db_path(install)

    def beaten(source, dest):
        # Another process finished the move while this one was getting ready.
        shutil.copy2(source, dest)
        source.rename(source.with_name(source.name + ".moved"))
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(migration, "_copy_database", beaten)

    assert relocate_database(target, install) is None
    assert_remembered(target)


def test_the_cli_moves_the_database_before_opening_it(install, tmp_path, capsys):
    with Store(LEGACY_DB) as store:
        remember_some(store)

    assert cli.main(["--config", str(install), "--json", "bindings"]) == 0

    listed = json.loads(capsys.readouterr().out)
    assert [row["media_key"] for row in listed] == ["mal://1"]
    assert_remembered(tmp_path / "config" / DB_FILENAME)


def test_the_cli_refuses_to_start_empty_when_the_move_fails(install, tmp_path, capsys):
    LEGACY_DB.write_bytes(b"not a database" * 100)

    assert cli.main(["--config", str(install), "bindings"]) == 1

    assert "is damaged" in capsys.readouterr().err
    assert not (tmp_path / "config" / DB_FILENAME).exists()


def test_a_fresh_cli_install_keeps_everything_in_the_config_directory(install, tmp_path):
    assert cli.main(["--config", str(install), "bindings"]) == 0

    assert (tmp_path / "config" / DB_FILENAME).is_file()
    assert list((tmp_path / "logs").iterdir()) == []


@as_root
def test_a_config_directory_the_cli_cannot_write_says_how_to_go_on(install, tmp_path, capsys):
    (tmp_path / "config").chmod(0o555)
    try:
        assert cli.main(["--config", str(install), "bindings"]) != 0
    finally:
        (tmp_path / "config").chmod(0o755)

    err = capsys.readouterr().err
    assert "Cannot open the state database" in err
    assert "--db" in err


def test_the_server_moves_the_database_before_opening_it(install, tmp_path):
    with Store(LEGACY_DB) as store:
        remember_some(store)

    app = create_app(install, static_dir=tmp_path / "no-ui", auth=AuthSettings(password=None))
    with TestClient(app):
        pass

    assert_remembered(tmp_path / "config" / DB_FILENAME)
    assert (tmp_path / "logs" / f"{DB_FILENAME}.moved").is_file()


def test_the_server_does_not_start_on_an_empty_database_when_the_move_fails(install, tmp_path):
    LEGACY_DB.write_bytes(b"not a database" * 100)

    app = create_app(install, static_dir=tmp_path / "no-ui", auth=AuthSettings(password=None))
    with pytest.raises(PagError, match="is damaged"), TestClient(app):
        pass

    assert not (tmp_path / "config" / DB_FILENAME).exists()
