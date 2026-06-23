from datetime import datetime, timedelta, timezone

import pytest

from collector.health import Health, LOOP_LAG_WATERMARK_MS


NOW = datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.isoformat()


def test_mutators_update_fields():
    h = Health()
    h.on_connect()
    h.on_connect()
    assert h.active_conns == 2
    h.on_disconnect()
    assert h.active_conns == 1

    ts = _iso(NOW)
    h.on_record(ts)
    assert h.records_total == 1
    assert h.last_record_ts == ts

    h.on_parse_error()
    assert h.parse_errors == 1

    h.flag_template_mismatch()
    assert h.template_mismatch is True

    h.note_flush(ok=False)
    assert h.flush_failures == 1
    h.note_flush(ok=True)
    assert h.flush_failures == 1  # success does not decrement

    sync_ts = _iso(NOW)
    h.note_config_sync(sync_ts, ok=True)
    assert h.last_config_sync_ts == sync_ts
    assert h.config_sync_failed is False
    h.note_config_sync(sync_ts, ok=False)
    assert h.config_sync_failed is True

    h.note_loop_lag(42.5)
    assert h.loop_lag_ms == 42.5


def test_state_ok_when_fresh_and_quiet():
    h = Health(last_record_ts=_iso(NOW - timedelta(minutes=5)))
    status, reasons = h.state(now=NOW)
    assert status == "OK"
    assert reasons == []


def test_state_ok_when_no_records_yet_but_otherwise_healthy():
    h = Health(last_record_ts=None)
    status, reasons = h.state(now=NOW)
    assert status == "OK"


def test_state_degraded_when_config_sync_failed():
    h = Health(last_record_ts=_iso(NOW), config_sync_failed=True)
    status, reasons = h.state(now=NOW)
    assert status == "DEGRADED"
    assert any("config" in r.lower() for r in reasons)


def test_state_degraded_when_template_mismatch():
    h = Health(last_record_ts=_iso(NOW), template_mismatch=True)
    status, reasons = h.state(now=NOW)
    assert status == "DEGRADED"
    assert any("template" in r.lower() for r in reasons)


def test_state_degraded_when_records_stale_beyond_window():
    h = Health(last_record_ts=_iso(NOW - timedelta(hours=3)))
    status, reasons = h.state(now=NOW, stale_after_s=7200)
    assert status == "DEGRADED"
    assert any("stale" in r.lower() for r in reasons)


def test_state_at_risk_when_flush_failures():
    h = Health(last_record_ts=_iso(NOW), flush_failures=2)
    status, reasons = h.state(now=NOW)
    assert status == "AT-RISK"
    assert any("flush" in r.lower() for r in reasons)


def test_state_at_risk_when_loop_lag_crosses_watermark():
    h = Health(last_record_ts=_iso(NOW), loop_lag_ms=LOOP_LAG_WATERMARK_MS + 1)
    status, reasons = h.state(now=NOW)
    assert status == "AT-RISK"
    assert any("lag" in r.lower() for r in reasons)


def test_at_risk_takes_precedence_over_degraded():
    h = Health(last_record_ts=_iso(NOW), template_mismatch=True, flush_failures=1)
    status, reasons = h.state(now=NOW)
    assert status == "AT-RISK"
    assert any("template" in r.lower() for r in reasons)
    assert any("flush" in r.lower() for r in reasons)


def test_coverage_summary_counts_gaps_over_window(db):
    h0 = NOW.replace(minute=0, second=0, microsecond=0)
    covered = [h0 - timedelta(hours=23), h0 - timedelta(hours=1)]
    db.executemany(
        "INSERT INTO coverage_hourly (hour_ts, record_count) VALUES (?, ?)",
        [(c.isoformat(), 10) for c in covered],
    )
    db.commit()

    health = Health()
    summary = health.coverage_summary(db, window_days=1, now=NOW)
    assert summary["days"] == 1
    assert summary["covered"] == 2
    assert summary["gaps"] == 22
    assert summary["pct"] == pytest.approx(2 / 24 * 100, rel=1e-3)


def test_coverage_summary_empty_window_is_all_gaps(db):
    health = Health()
    summary = health.coverage_summary(db, window_days=1, now=NOW)
    assert summary["covered"] == 0
    assert summary["gaps"] == 24
    assert summary["pct"] == 0.0
