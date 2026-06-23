"""CounterEngine: ConnectionID dedup + in-memory per-dimension counters + flush/load.

Stdlib only. Usage is keyed on the resolved config object_id (the log name is
resolved -> id via the injected name_index and kept only as a label), so a
rename never produces a false retire. A connection is deduped by ConnectionID (open + close = one count, modulo a
replayed record arriving after the connection was evicted from the in-flight
set — acceptable advisory drift, since presence/last_seen stay correct); counts
are advisory, presence/last_seen is the verdict. last_seen is capped at now. No raw logs are stored.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import sqlite3

from collector import fields


@dataclass
class CounterRow:
    object_type: str
    object_id: str
    name: str
    state: str = "attempted"          # promoted to "active" on first brokered hit
    access_count: int = 0
    first_seen_ts: str = ""
    last_seen_ts: str = ""
    user_sample: list = field(default_factory=list)   # bounded, distinct, FIFO
    daily: dict = field(default_factory=dict)          # {iso_date: count}, last 90 days
    dirty: bool = False


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class CounterEngine:
    def __init__(self, settings):
        self.settings = settings
        self._rows: dict[tuple[str, str], CounterRow] = {}
        # ConnectionID -> mono timestamp of first sighting (bounded by in-flight conns)
        self._inflight: dict[str, float] = {}

    # -- dedup -------------------------------------------------------------
    def _evict_expired(self, mono: float) -> None:
        ttl = self.settings.dedup_ttl_seconds
        if ttl <= 0:
            return
        cutoff = mono - ttl
        stale = [cid for cid, seen in self._inflight.items() if seen <= cutoff]
        for cid in stale:
            del self._inflight[cid]

    def _is_duplicate(self, obj: dict, mono: float) -> bool:
        """First sighting of a ConnectionID -> record + return False; any later
        record for it -> True (evicting on Close so the set stays bounded by
        in-flight connections, not by the window)."""
        self._evict_expired(mono)
        cid = fields.connection_id(obj)
        if not cid:
            return False  # no dedup key -> count it
        is_close = str(obj.get("ConnectionStatus", "")).lower() == "close"
        if cid in self._inflight:
            if is_close:
                del self._inflight[cid]  # connection finished; free the slot
            return True
        self._inflight[cid] = mono
        return False

    # -- ingest ------------------------------------------------------------
    def ingest(self, obj, name_index, *, now: datetime, mono: float) -> bool:
        if fields.is_self_conn(obj):
            return False
        if self._is_duplicate(obj, mono):
            return False

        ev = fields.event_time(obj)
        ts = ev if ev <= now else now          # last_seen capped at now
        ts_iso = _iso(ts)
        day = ts.astimezone(timezone.utc).date().isoformat()
        brokered = fields.is_brokered(obj)
        user = obj.get("Username")

        counted_any = False
        # NOTE(v1): the server dimension keys off the 'Server' name; a record
        # with only ServerIP (no Server) is not counted as a server — there is
        # no name-index path for raw IPs. Deferred.
        for object_type, log_field in fields.DIMENSION_FIELDS.items():
            name = obj.get(log_field)
            if not name:
                continue
            object_id = name_index.resolve(object_type, name)
            if not object_id:
                continue  # unresolved name -> skip this dimension (not counted)
            self._touch(object_type, object_id, name, ts_iso, day, brokered, user)
            counted_any = True
        return counted_any

    def _touch(self, object_type, object_id, name, ts_iso, day, brokered, user) -> None:
        key = (object_type, object_id)
        row = self._rows.get(key)
        if row is None:
            row = CounterRow(object_type=object_type, object_id=object_id,
                             name=name, first_seen_ts=ts_iso)
            self._rows[key] = row
        row.name = name  # last-seen log name as label
        row.access_count += 1
        if not row.first_seen_ts:
            row.first_seen_ts = ts_iso
        if ts_iso > row.last_seen_ts:
            row.last_seen_ts = ts_iso
        if brokered:
            row.state = "active"
        # daily, trimmed to the last 90 distinct UTC dates
        row.daily[day] = row.daily.get(day, 0) + 1
        if len(row.daily) > 90:
            for old in sorted(row.daily)[:-90]:
                del row.daily[old]
        # user sample: bounded, distinct, FIFO
        if self.settings.user_sample_enabled and user:
            if user in row.user_sample:
                row.user_sample.remove(user)
            row.user_sample.append(user)
            cap = self.settings.recent_users_max
            if len(row.user_sample) > cap:
                row.user_sample = row.user_sample[-cap:]
        row.dirty = True

    def snapshot(self):
        return list(self._rows.values())

    # -- persistence -------------------------------------------------------
    def flush(self, conn: sqlite3.Connection) -> int:
        dirty = [r for r in self._rows.values() if r.dirty]
        if not dirty:
            return 0
        now_iso = _iso(datetime.now(timezone.utc))
        with conn:
            for r in dirty:
                conn.execute(
                    """INSERT INTO usage_counter
                         (object_type, object_id, name, state, access_count,
                          first_seen_ts, last_seen_ts, user_sample_json,
                          daily_json, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(object_type, object_id) DO UPDATE SET
                         name=excluded.name,
                         state=excluded.state,
                         access_count=excluded.access_count,
                         first_seen_ts=excluded.first_seen_ts,
                         last_seen_ts=excluded.last_seen_ts,
                         user_sample_json=excluded.user_sample_json,
                         daily_json=excluded.daily_json,
                         updated_at=excluded.updated_at""",
                    (r.object_type, r.object_id, r.name, r.state, r.access_count,
                     r.first_seen_ts, r.last_seen_ts,
                     json.dumps(r.user_sample, separators=(",", ":")),
                     json.dumps(r.daily, separators=(",", ":")),
                     now_iso),
                )
        for r in dirty:
            r.dirty = False
        return len(dirty)

    def load(self, conn: sqlite3.Connection) -> None:
        self._rows = {}
        cur = conn.execute(
            """SELECT object_type, object_id, name, state, access_count,
                      first_seen_ts, last_seen_ts, user_sample_json, daily_json
                 FROM usage_counter""")
        for row in cur.fetchall():
            self._rows[(row["object_type"], row["object_id"])] = CounterRow(
                object_type=row["object_type"],
                object_id=row["object_id"],
                name=row["name"],
                state=row["state"],
                access_count=row["access_count"],
                first_seen_ts=row["first_seen_ts"] or "",
                last_seen_ts=row["last_seen_ts"] or "",
                user_sample=json.loads(row["user_sample_json"] or "[]"),
                daily=json.loads(row["daily_json"] or "{}"),
                dirty=False,
            )

    def record_coverage(self, conn: sqlite3.Connection, now: datetime) -> None:
        hour = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        hour_iso = hour.isoformat()
        with conn:
            conn.execute(
                """INSERT INTO coverage_hourly (hour_ts, record_count)
                   VALUES (?, 1)
                   ON CONFLICT(hour_ts) DO UPDATE SET
                     record_count = record_count + 1""",
                (hour_iso,),
            )
