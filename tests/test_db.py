import sqlite3

import pytest

from collector import db as dbmod
from collector.config import Settings


TABLES = {"config_object", "config_edge", "usage_counter", "coverage_hourly", "meta"}


def _make_settings(tmp_path) -> Settings:
    return Settings(zs_vanity_domain="acme", zs_client_id="id", zs_client_secret="sec",
                    zpa_customer_id="123", dash_token="t",
                    db_path=str(tmp_path / "state.db"))


def _table_names(conn) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


def test_connect_sets_wal_and_row_factory(tmp_path):
    conn = dbmod.connect(str(tmp_path / "state.db"))
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk == 1
        dbmod.init_schema(conn)
        row = conn.execute(
            "SELECT key, value FROM meta WHERE key='schema_version'").fetchone()
        # Row factory: indexable by column name.
        assert row["key"] == "schema_version"
    finally:
        conn.close()


def test_init_schema_creates_all_tables(db):
    assert TABLES.issubset(_table_names(db))


def test_init_schema_is_idempotent(tmp_path):
    conn = dbmod.connect(str(tmp_path / "state.db"))
    try:
        dbmod.init_schema(conn)
        dbmod.init_schema(conn)  # must not raise
        assert TABLES.issubset(_table_names(conn))
    finally:
        conn.close()


def test_meta_roundtrip(db):
    assert dbmod.get_meta(db, "absent") is None
    assert dbmod.get_meta(db, "absent", "fallback") == "fallback"
    dbmod.set_meta(db, "window_start", "2026-06-21T00:00:00+00:00")
    assert dbmod.get_meta(db, "window_start") == "2026-06-21T00:00:00+00:00"
    # set is an upsert.
    dbmod.set_meta(db, "window_start", "2026-06-22T00:00:00+00:00")
    assert dbmod.get_meta(db, "window_start") == "2026-06-22T00:00:00+00:00"


def test_migrate_sets_schema_version(db):
    dbmod.migrate(db)
    assert dbmod.get_meta(db, "schema_version") == str(dbmod.SCHEMA_VERSION)


def test_migrate_is_noop_when_current(db):
    dbmod.migrate(db)
    dbmod.migrate(db)  # second call must be a no-op, no error
    assert dbmod.get_meta(db, "schema_version") == str(dbmod.SCHEMA_VERSION)


def test_read_only_connection_rejects_writes(tmp_path):
    path = str(tmp_path / "state.db")
    rw = dbmod.connect(path)
    dbmod.init_schema(rw)
    rw.close()

    ro = dbmod.connect(path, read_only=True)
    try:
        # Reads work.
        ro.execute("SELECT 1").fetchone()
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("INSERT INTO meta(key, value) VALUES ('x', 'y')")
    finally:
        ro.close()


def test_wal_two_connection_read_after_write(tmp_path):
    path = str(tmp_path / "state.db")
    writer = dbmod.connect(path)
    dbmod.init_schema(writer)
    reader = dbmod.connect(path, read_only=True)
    try:
        dbmod.set_meta(writer, "window_start", "2026-06-21T00:00:00+00:00")
        # A separate read-only connection sees the committed write (WAL invariant).
        row = reader.execute(
            "SELECT value FROM meta WHERE key='window_start'").fetchone()
        assert row is not None
        assert row[0] == "2026-06-21T00:00:00+00:00"
    finally:
        writer.close()
        reader.close()


def test_migrate_v2_dedups_config_object_by_type_id(tmp_path):
    """v1->v2 migration collapses a globally-unique id stored under multiple
    microtenant scopes (the default-microtenant '0' overlap bug) to one row."""
    conn = dbmod.connect(str(tmp_path / "state.db"))
    try:
        dbmod.init_schema(conn)
        for mt in ("", "0"):
            conn.execute(
                "INSERT INTO config_object (id, microtenant_id, type, name, enabled, "
                "first_config_seen, last_config_seen, attrs_json) "
                "VALUES ('seg1', ?, 'segment', 'App-One', 1, 't', 't', '{}')", (mt,))
        conn.commit()
        dbmod.set_meta(conn, "schema_version", "1")  # force the v1->v2 path
        assert conn.execute("SELECT COUNT(*) FROM config_object").fetchone()[0] == 2
        dbmod.migrate(conn)
        rows = conn.execute("SELECT id, microtenant_id FROM config_object").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "seg1"
        assert dbmod.get_meta(conn, "schema_version") == "2"
    finally:
        conn.close()
