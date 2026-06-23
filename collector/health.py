"""Health / coverage telemetry with loud, non-silent degradation (C9).

State thresholds (documented, chosen here — not magic):
  * DEGRADED — a correctness problem that makes verdicts untrustworthy but does
    not threaten the process: config sync failed, the LSS template is missing
    required fields, or records have gone stale beyond ``stale_after_s``
    (default 2 h).
  * AT-RISK — a saturation / durability problem that means we may be losing
    data right now: any flush failure, or event-loop lag at/above the watermark
    (the single-process ceiling is in sight). AT-RISK outranks DEGRADED.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# Event-loop lag (ms) at/above which we are "approaching the single-process
# ceiling" and report AT-RISK. 1 s of lag means the loop is a full flush-tick
# behind; well past acceptable for a metadata-rate ingest path.
LOOP_LAG_WATERMARK_MS = 1000.0


def _parse_ts(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass
class Health:
    last_record_ts: str | None = None
    active_conns: int = 0
    records_total: int = 0
    parse_errors: int = 0
    flush_failures: int = 0
    template_mismatch: bool = False
    last_config_sync_ts: str | None = None
    config_sync_failed: bool = False
    loop_lag_ms: float = 0.0

    # ---- mutators ---------------------------------------------------------
    def on_connect(self) -> None:
        self.active_conns += 1

    def on_disconnect(self) -> None:
        self.active_conns -= 1

    def on_record(self, ts: str) -> None:
        self.records_total += 1
        self.last_record_ts = ts

    def on_parse_error(self) -> None:
        self.parse_errors += 1

    def flag_template_mismatch(self) -> None:
        self.template_mismatch = True

    def note_flush(self, ok: bool) -> None:
        if not ok:
            self.flush_failures += 1

    def note_config_sync(self, ts: str, ok: bool) -> None:
        self.last_config_sync_ts = ts
        self.config_sync_failed = not ok

    def note_loop_lag(self, ms: float) -> None:
        self.loop_lag_ms = ms

    # ---- state ------------------------------------------------------------
    def state(self, *, now: datetime, stale_after_s: int = 7200) -> "tuple[str, list[str]]":
        degraded: list[str] = []
        at_risk: list[str] = []

        if self.config_sync_failed:
            degraded.append("config sync failed — verdicts run against stale config")
        if self.template_mismatch:
            degraded.append("LSS template mismatch — required fields missing")
        if self.last_record_ts is not None:
            idle_s = (now - _parse_ts(self.last_record_ts)).total_seconds()
            if idle_s > stale_after_s:
                degraded.append(
                    f"records stale — no record for {int(idle_s)}s (>{stale_after_s}s)")

        if self.flush_failures > 0:
            at_risk.append(
                f"flush failing — {self.flush_failures} failed flush(es), losing increments")
        if self.loop_lag_ms >= LOOP_LAG_WATERMARK_MS:
            at_risk.append(
                f"event-loop lag {self.loop_lag_ms:.0f}ms >= {LOOP_LAG_WATERMARK_MS:.0f}ms "
                "watermark — approaching single-process ceiling")

        reasons = at_risk + degraded
        if at_risk:
            return "AT-RISK", reasons
        if degraded:
            return "DEGRADED", reasons
        return "OK", []

    # ---- coverage ---------------------------------------------------------
    def coverage_summary(self, conn, *, window_days: int, now: datetime) -> dict:
        """Compute coverage over the trailing ``window_days`` from coverage_hourly.

        Buckets the window into hourly slots (``window_days * 24``), counts how
        many slots have a coverage_hourly row with record_count > 0, and reports
        the gap count and coverage percentage. Hour buckets are floored to the
        top of the hour to match how the ingest path stamps them.
        """
        total_buckets = window_days * 24
        top_of_hour = now.replace(minute=0, second=0, microsecond=0)
        window_start = top_of_hour - timedelta(hours=total_buckets - 1)

        rows = conn.execute(
            "SELECT hour_ts FROM coverage_hourly "
            "WHERE hour_ts >= ? AND hour_ts <= ? AND record_count > 0",
            (window_start.isoformat(), top_of_hour.isoformat()),
        ).fetchall()

        covered_hours = set()
        for row in rows:
            ts = row["hour_ts"] if not isinstance(row, tuple) else row[0]
            covered_hours.add(_parse_ts(ts).replace(minute=0, second=0, microsecond=0))

        covered = len(covered_hours)
        gaps = total_buckets - covered
        pct = (covered / total_buckets * 100.0) if total_buckets else 0.0
        return {"days": window_days, "covered": covered, "gaps": gaps, "pct": pct}
