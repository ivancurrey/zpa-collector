"""Pure classification functions: band / confidence / usage_state / drift.

No I/O, no now() inside — `now` is always passed in by the caller. Heat bands
are a pure function of (last_seen, now, thresholds), evaluated at read time
(spec §5.4); the env thresholds are only defaults, overridable per request.
"""
from __future__ import annotations

from datetime import datetime, timezone

DRIFT_GHOST = "ghost"
DRIFT_RETIRED = "retired"
DRIFT_NONE = ""


def _parse_iso(ts: str) -> datetime | None:
    """Parse an ISO-8601 timestamp to tz-aware UTC; None on failure."""
    text = ts.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def days_idle(last_seen_ts: str | None, now: datetime) -> int | None:
    """Whole days between last_seen and now; None when last_seen is missing.

    Negative deltas (last_seen ahead of now) are clamped to 0; upstream caps
    last_seen at now, but we stay defensive here too.
    """
    if not last_seen_ts:
        return None
    seen = _parse_iso(last_seen_ts)
    if seen is None:
        return None
    delta_days = (now - seen).days
    return max(0, delta_days)


def band(last_seen_ts: str | None, now: datetime, warm_days: int, stale_days: int) -> str:
    """green | yellow | orange | red. None/empty/unparseable last_seen -> red."""
    idle = days_idle(last_seen_ts, now)
    if idle is None:
        return "red"
    if idle <= warm_days:
        return "green"
    if idle <= stale_days:
        return "yellow"
    return "orange"


def confidence(observed_days: int) -> str:
    """Observation-horizon confidence tier: low (<90) | medium (<365) | high."""
    if observed_days < 90:
        return "low"
    if observed_days < 365:
        return "medium"
    return "high"


def usage_state(has_brokered: bool, has_record: bool) -> str:
    """Three-state usage model (spec §5.4).

    active   — at least one brokered (real) connection observed.
    attempted— records exist but none were brokered (referenced/misconfigured).
    silent   — no records in the observation horizon (the retire candidate).
    """
    if has_brokered:
        return "active"
    if has_record:
        return "attempted"
    return "silent"
