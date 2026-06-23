"""SQLite state layer: WAL connection, schema, forward-only migrate, meta kv."""
from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 2

_DDL = """
CREATE TABLE IF NOT EXISTS config_object (
  id TEXT, microtenant_id TEXT, type TEXT, name TEXT, enabled INTEGER,
  first_config_seen TEXT, last_config_seen TEXT, attrs_json TEXT,
  PRIMARY KEY (id, microtenant_id));
CREATE INDEX IF NOT EXISTS ix_cfgobj_type_name ON config_object(type, name);

CREATE TABLE IF NOT EXISTS config_edge (
  parent_type TEXT, parent_id TEXT, child_type TEXT, child_id TEXT, source TEXT,
  PRIMARY KEY (parent_type, parent_id, child_type, child_id));

CREATE TABLE IF NOT EXISTS usage_counter (
  object_type TEXT, object_id TEXT, name TEXT, state TEXT,
  access_count INTEGER, first_seen_ts TEXT, last_seen_ts TEXT,
  user_sample_json TEXT, daily_json TEXT, updated_at TEXT,
  PRIMARY KEY (object_type, object_id));

CREATE TABLE IF NOT EXISTS coverage_hourly (
  hour_ts TEXT PRIMARY KEY, record_count INTEGER);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def connect(db_path: str, *, read_only: bool = False) -> sqlite3.Connection:
    """Open a WAL SQLite connection (foreign_keys ON, Row factory).

    read_only opens a query_only connection for the HTTP thread.
    """
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    if read_only:
        conn.execute("PRAGMA query_only=1")
    return conn


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    return row[0]


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()


def migrate(conn: sqlite3.Connection) -> None:
    """Forward-only migration; no-op when meta.schema_version == SCHEMA_VERSION."""
    current = get_meta(conn, "schema_version")
    if current == str(SCHEMA_VERSION):
        return
    # v1 -> v2: collapse config_object rows that stored a globally-unique ZPA id
    # under more than one microtenant scope (the default-microtenant "0" overlap
    # bug). Keep the first-inserted row per (type, id) — lowest rowid, i.e. the
    # no-filter "" scope. No-op on a clean/empty table.
    conn.execute(
        "DELETE FROM config_object WHERE rowid NOT IN "
        "(SELECT MIN(rowid) FROM config_object GROUP BY type, id)")
    conn.commit()
    set_meta(conn, "schema_version", str(SCHEMA_VERSION))


def init_schema(conn: sqlite3.Connection) -> None:
    """Create all tables + indexes idempotently, then stamp the schema version so a
    freshly-initialized DB is at the current version (migrate() then no-ops).

    executescript() autocommits the DDL; migrate() commits the version stamp.
    """
    conn.executescript(_DDL)
    migrate(conn)
