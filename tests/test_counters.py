import dataclasses
import json
from datetime import datetime, timedelta, timezone

import pytest

from collector import fields
from collector.counters import CounterEngine, CounterRow
from tests.fixtures.sample_lines import SAMPLE_OPEN, SAMPLE_CLOSE, SAMPLE_SELF


NOW = datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc)


class FakeNameIndex:
    """Local stand-in for config_sync.NameIndex — do NOT import config_sync here.

    Resolves every name to a deterministic id 'id::<name>' so the same name
    always maps to the same object_id (proving id-keying / rename-safety), and
    distinct names get distinct ids. Names listed in `unresolvable` return None.
    """

    def __init__(self, unresolvable=()):
        self.unresolvable = set(unresolvable)

    def resolve(self, object_type, name, microtenant_id=None):
        if name in self.unresolvable:
            return None
        return f"id::{name}"


def _obj(line):
    return json.loads(line)


def _row_for(engine, object_type, object_id):
    for r in engine.snapshot():
        if r.object_type == object_type and r.object_id == object_id:
            return r
    return None


def test_connection_id_dedup_open_then_close_counts_once(settings):
    engine = CounterEngine(settings)
    idx = FakeNameIndex()

    counted_open = engine.ingest(_obj(SAMPLE_OPEN), idx, now=NOW, mono=1000.0)
    counted_close = engine.ingest(_obj(SAMPLE_CLOSE), idx, now=NOW, mono=1001.0)

    assert counted_open is True
    assert counted_close is False  # duplicate ConnectionID -> dropped

    seg = _row_for(engine, "segment", "id::Finance-App")
    assert seg is not None
    assert seg.access_count == 1


def test_self_conn_line_is_dropped(settings):
    engine = CounterEngine(settings)
    idx = FakeNameIndex()

    counted = engine.ingest(_obj(SAMPLE_SELF), idx, now=NOW, mono=2000.0)

    assert counted is False
    assert engine.snapshot() == []


def test_one_connection_updates_all_resolvable_dimensions(settings):
    engine = CounterEngine(settings)
    idx = FakeNameIndex()

    counted = engine.ingest(_obj(SAMPLE_OPEN), idx, now=NOW, mono=3000.0)
    assert counted is True

    expected = {
        ("segment", "id::Finance-App"),
        ("segment_group", "id::Finance"),
        ("rule", "id::Allow-Finance"),
        ("server", "id::fin-srv-01"),
        ("connector", "id::ac1"),
    }
    got = {(r.object_type, r.object_id) for r in engine.snapshot()}
    assert got == expected
    for r in engine.snapshot():
        assert r.access_count == 1


def test_unresolved_name_is_skipped_and_id_is_the_key(settings):
    engine = CounterEngine(settings)
    idx = FakeNameIndex(unresolvable={"Finance-App"})

    counted = engine.ingest(_obj(SAMPLE_OPEN), idx, now=NOW, mono=4000.0)
    assert counted is True  # other dimensions still resolved

    keys = {(r.object_type, r.object_id) for r in engine.snapshot()}
    assert ("segment", "id::Finance-App") not in keys      # unresolved -> skipped
    assert ("rule", "id::Allow-Finance") in keys           # id-keyed

    rule_row = _row_for(engine, "rule", "id::Allow-Finance")
    assert rule_row.object_id == "id::Allow-Finance"       # keyed on id
    assert rule_row.name == "Allow-Finance"                # name kept as label


def test_last_seen_capped_at_now_for_future_event(settings):
    engine = CounterEngine(settings)
    idx = FakeNameIndex()
    future = (NOW + timedelta(days=2)).isoformat().replace("+00:00", "Z")
    obj = _obj(SAMPLE_OPEN)
    obj["TimestampConnectionStart"] = future

    engine.ingest(obj, idx, now=NOW, mono=5000.0)

    seg = _row_for(engine, "segment", "id::Finance-App")
    assert seg.last_seen_ts == NOW.isoformat()             # capped, not the future ts


def test_daily_increments_per_utc_date_and_trims_to_90(settings):
    engine = CounterEngine(settings)
    idx = FakeNameIndex()

    base = datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc)
    for i in range(95):
        day = base + timedelta(days=i)
        obj = _obj(SAMPLE_OPEN)
        obj["ConnectionID"] = f"day-{i}"
        obj["TimestampConnectionStart"] = day.isoformat().replace("+00:00", "Z")
        engine.ingest(obj, idx, now=NOW, mono=6000.0 + i)

    seg = _row_for(engine, "segment", "id::Finance-App")
    assert len(seg.daily) == 90                 # trimmed to the last 90 dates
    earliest = (base + timedelta(days=5)).date().isoformat()
    assert min(seg.daily) == earliest           # oldest 5 dropped
    same_day = base + timedelta(days=94)
    obj = _obj(SAMPLE_OPEN)
    obj["ConnectionID"] = "day-94-again"
    obj["TimestampConnectionStart"] = same_day.isoformat().replace("+00:00", "Z")
    engine.ingest(obj, idx, now=NOW, mono=6200.0)
    seg = _row_for(engine, "segment", "id::Finance-App")
    assert seg.daily[same_day.date().isoformat()] == 2


def test_user_sample_is_bounded_distinct_and_fifo(settings):
    engine = CounterEngine(settings)  # recent_users_max=10, sampling on
    idx = FakeNameIndex()

    for i in range(12):
        obj = _obj(SAMPLE_OPEN)
        obj["ConnectionID"] = f"u-{i}"
        obj["Username"] = f"user{i}@example.com"
        engine.ingest(obj, idx, now=NOW, mono=7000.0 + i)

    seg = _row_for(engine, "segment", "id::Finance-App")
    assert len(seg.user_sample) == 10                      # bounded
    assert seg.user_sample[0] == "user2@example.com"    # user0,user1 evicted (FIFO)
    assert seg.user_sample[-1] == "user11@example.com"

    obj = _obj(SAMPLE_OPEN)
    obj["ConnectionID"] = "u-repeat"
    obj["Username"] = "user5@example.com"
    engine.ingest(obj, idx, now=NOW, mono=7100.0)
    seg = _row_for(engine, "segment", "id::Finance-App")
    assert seg.user_sample.count("user5@example.com") == 1
    assert seg.user_sample[-1] == "user5@example.com"


def test_user_sample_empty_when_disabled(settings):
    disabled = dataclasses.replace(settings, user_sample_enabled=False)
    engine = CounterEngine(disabled)
    idx = FakeNameIndex()

    engine.ingest(_obj(SAMPLE_OPEN), idx, now=NOW, mono=7200.0)

    seg = _row_for(engine, "segment", "id::Finance-App")
    assert seg.user_sample == []


def test_flush_writes_dirty_rows_then_load_rebuilds_identical(settings, db):
    engine = CounterEngine(settings)
    idx = FakeNameIndex()
    engine.ingest(_obj(SAMPLE_OPEN), idx, now=NOW, mono=8000.0)

    before = {(r.object_type, r.object_id): (r.access_count, r.state,
              r.last_seen_ts, tuple(r.user_sample), tuple(sorted(r.daily.items())))
              for r in engine.snapshot()}
    assert all(r.dirty for r in engine.snapshot())

    written = engine.flush(db)
    assert written == len(before)                          # all dirty rows written
    assert all(not r.dirty for r in engine.snapshot())     # dirty cleared

    assert engine.flush(db) == 0                            # nothing dirty -> zero

    fresh = CounterEngine(settings)
    fresh.load(db)
    after = {(r.object_type, r.object_id): (r.access_count, r.state,
             r.last_seen_ts, tuple(r.user_sample), tuple(sorted(r.daily.items())))
             for r in fresh.snapshot()}
    assert after == before
    assert all(not r.dirty for r in fresh.snapshot())      # loaded rows are clean


def test_record_coverage_upserts_current_hour_bucket(settings, db):
    engine = CounterEngine(settings)

    engine.record_coverage(db, NOW)
    engine.record_coverage(db, NOW)
    engine.record_coverage(db, NOW + timedelta(minutes=30))

    hour_iso = NOW.replace(minute=0, second=0, microsecond=0).isoformat()
    cur = db.execute(
        "SELECT hour_ts, record_count FROM coverage_hourly WHERE hour_ts = ?",
        (hour_iso,))
    rows = cur.fetchall()
    assert len(rows) == 1                                  # single bucket per hour
    assert rows[0]["record_count"] == 3                    # upsert incremented


def test_ttl_eviction_bounds_inflight_set(settings):
    """An in-flight ConnectionID older than the TTL is evicted on a later ingest,
    so the in-flight set is bounded by recency, not by the window."""
    engine = CounterEngine(settings)  # dedup_ttl_seconds=300
    idx = FakeNameIndex()
    engine.ingest(_obj(SAMPLE_OPEN), idx, now=NOW, mono=0.0)  # c-1, open -> in-flight
    assert "c-1" in engine._inflight

    other = _obj(SAMPLE_OPEN)
    other["ConnectionID"] = "c-late"
    engine.ingest(other, idx, now=NOW, mono=1000.0)  # 1000 > 300 TTL -> evicts c-1
    assert "c-1" not in engine._inflight              # evicted by TTL
    assert "c-late" in engine._inflight


def test_records_without_connection_id_are_each_counted(settings):
    """No ConnectionID -> nothing to dedup on, so each record counts."""
    engine = CounterEngine(settings)
    idx = FakeNameIndex()
    a = _obj(SAMPLE_OPEN); a.pop("ConnectionID")
    b = _obj(SAMPLE_OPEN); b.pop("ConnectionID")
    assert engine.ingest(a, idx, now=NOW, mono=1.0) is True
    assert engine.ingest(b, idx, now=NOW, mono=2.0) is True
    seg = _row_for(engine, "segment", "id::Finance-App")
    assert seg.access_count == 2
