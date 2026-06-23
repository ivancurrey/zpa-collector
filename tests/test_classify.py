from datetime import datetime, timedelta, timezone

from collector import classify

NOW = datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc)


def _ago(days: int) -> str:
    """ISO-8601 UTC string for `days` before NOW."""
    return (NOW - timedelta(days=days)).isoformat()


def test_band_green_within_warm_days():
    assert classify.band(_ago(3), NOW, warm_days=7, stale_days=30) == "green"


def test_band_green_at_warm_boundary():
    assert classify.band(_ago(7), NOW, warm_days=7, stale_days=30) == "green"


def test_band_yellow_within_stale_days():
    assert classify.band(_ago(20), NOW, warm_days=7, stale_days=30) == "yellow"


def test_band_yellow_at_stale_boundary():
    assert classify.band(_ago(30), NOW, warm_days=7, stale_days=30) == "yellow"


def test_band_orange_beyond_stale_days():
    assert classify.band(_ago(45), NOW, warm_days=7, stale_days=30) == "orange"


def test_band_red_when_last_seen_none():
    assert classify.band(None, NOW, warm_days=7, stale_days=30) == "red"


def test_band_red_when_last_seen_empty():
    assert classify.band("", NOW, warm_days=7, stale_days=30) == "red"


def test_days_idle_basic_math():
    assert classify.days_idle(_ago(12), NOW) == 12


def test_days_idle_zero_when_just_now():
    assert classify.days_idle(NOW.isoformat(), NOW) == 0


def test_days_idle_none_when_missing():
    assert classify.days_idle(None, NOW) is None
    assert classify.days_idle("", NOW) is None


def test_days_idle_none_when_unparseable():
    assert classify.days_idle("not-a-date", NOW) is None


def test_days_idle_clamps_future_last_seen_to_zero():
    future = (NOW + timedelta(days=5)).isoformat()
    assert classify.days_idle(future, NOW) == 0


def test_days_idle_handles_trailing_z():
    assert classify.days_idle("2026-06-11T12:00:00Z", NOW) == 10


def test_confidence_low_below_90_days():
    assert classify.confidence(0) == "low"
    assert classify.confidence(89) == "low"


def test_confidence_medium_at_90_to_364():
    assert classify.confidence(90) == "medium"
    assert classify.confidence(200) == "medium"
    assert classify.confidence(364) == "medium"


def test_confidence_high_at_365_and_beyond():
    assert classify.confidence(365) == "high"
    assert classify.confidence(1000) == "high"


def test_usage_state_active_when_brokered():
    assert classify.usage_state(has_brokered=True, has_record=True) == "active"


def test_usage_state_attempted_when_record_but_not_brokered():
    assert classify.usage_state(has_brokered=False, has_record=True) == "attempted"


def test_usage_state_silent_when_no_record():
    assert classify.usage_state(has_brokered=False, has_record=False) == "silent"


def test_usage_state_active_dominates_even_without_record_flag():
    assert classify.usage_state(has_brokered=True, has_record=False) == "active"
