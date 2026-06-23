import csv
import io
import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone

import pytest

from collector import httpd
from collector.health import Health


NOW = datetime(2026, 6, 21, 0, 0, 0, tzinfo=timezone.utc)


def _candidate_rows():
    return [
        {
            "object_type": "segment",
            "name": "Finance-App",
            "state": "active",
            "band": "green",
            "last_seen": "2026-06-20T12:05:00+00:00",
            "days_idle": 0,
            "access_count": 12,
            "confidence": "low",
            "drift": "",
        },
        {
            "object_type": "rule",
            "name": "Allow-Finance",
            "state": "silent",
            "band": "red",
            "last_seen": "",
            "days_idle": None,
            "access_count": 0,
            "confidence": "low",
            "drift": "ghost",
        },
    ]


def _header():
    return {
        "counts": {"active": 1, "attempted": 0, "silent": 1},
        "coverage_pct": 92.5,
        "coverage_days": 30,
        "coverage_gaps": 2,
        "warm_days": 7,
        "stale_days": 30,
        "window_days": 30,
        "generated_at": NOW.isoformat(),
    }


def test_build_csv_has_comment_header_and_rows():
    out = httpd.build_csv(_candidate_rows(), _header())
    lines = out.splitlines()
    comment_lines = [ln for ln in lines if ln.startswith("#")]
    assert comment_lines, "expected a leading # comment block"
    blob = "\n".join(comment_lines)
    assert "coverage" in blob.lower()
    assert "92.5" in blob
    assert "2 gaps" in blob.lower() or "gaps: 2" in blob.lower() or "gaps=2" in blob.lower()
    assert "30" in blob
    assert "candidate for review" in blob.lower()


def test_build_csv_columns_and_data_rows():
    out = httpd.build_csv(_candidate_rows(), _header())
    data = "\n".join(ln for ln in out.splitlines() if not ln.startswith("#"))
    reader = csv.DictReader(io.StringIO(data))
    cols = reader.fieldnames
    assert cols == [
        "type", "name", "state", "band", "last_seen",
        "days_idle", "access_count", "confidence", "drift",
    ]
    rows = list(reader)
    assert rows[0]["type"] == "segment"
    assert rows[0]["name"] == "Finance-App"
    assert rows[1]["drift"] == "ghost"


def test_build_csv_never_contains_usernames():
    out = httpd.build_csv(_candidate_rows(), _header())
    low = out.lower()
    assert "user_sample" not in low
    assert "username" not in low
    assert "@" not in out


def _seed_summary_db(conn):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO config_object "
        "(id, microtenant_id, type, name, enabled, first_config_seen, "
        " last_config_seen, attrs_json) VALUES (?,?,?,?,?,?,?,?)",
        ("seg-1", "", "segment", "Finance-App", 1,
         "2026-01-01T00:00:00+00:00", "2026-06-21T00:00:00+00:00", "{}"),
    )
    cur.execute(
        "INSERT INTO usage_counter "
        "(object_type, object_id, name, state, access_count, first_seen_ts, "
        " last_seen_ts, user_sample_json, daily_json, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("segment", "seg-1", "Finance-App", "active", 12,
         "2026-06-20T12:00:00+00:00", "2026-06-20T12:05:00+00:00",
         json.dumps(["alice@example.com"]), json.dumps({"2026-06-20": 12}),
         "2026-06-20T12:05:00+00:00"),
    )
    conn.commit()


@pytest.fixture
def http_server(settings, db):
    _seed_summary_db(db)
    health = Health()
    sync_calls = {"n": 0}

    from dataclasses import replace
    s = replace(settings, http_port=0)
    thread, server = httpd.start_http(s, health, on_sync=lambda: sync_calls.__setitem__("n", sync_calls["n"] + 1), _return_server=True)
    port = server.server_address[1]
    base = f"http://127.0.0.1:{port}"
    yield base, health, sync_calls, s
    server.shutdown()
    thread.join(timeout=5)


def _get(url, token=None, method="GET"):
    req = urllib.request.Request(url, method=method)
    if token:
        req.add_header("X-Dash-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def test_health_200_when_ok(http_server):
    base, health, _, _ = http_server
    status, body = _get(f"{base}/health")
    assert status == 200
    assert json.loads(body)["state"] == "OK"


def test_health_non_200_when_not_ok(http_server):
    base, health, _, _ = http_server
    health.template_mismatch = True
    status, _ = _get(f"{base}/health")
    assert status != 200


def test_protected_route_requires_token(http_server):
    base, _, _, s = http_server
    status, _ = _get(f"{base}/api/summary")
    assert status == 401
    status, _ = _get(f"{base}/api/summary", token=s.dash_token)
    assert status == 200


def test_summary_reflects_seeded_usage(http_server):
    base, _, _, s = http_server
    status, body = _get(f"{base}/api/summary", token=s.dash_token)
    assert status == 200
    payload = json.loads(body)
    seg = payload["segment"]
    assert seg["counts"]["active"] == 1


def test_summary_band_recomputed_from_query_overrides(http_server):
    base, _, _, s = http_server
    status, body = _get(
        f"{base}/api/summary?warm=0&stale=0&window=30", token=s.dash_token)
    payload = json.loads(body)
    bands = payload["segment"]["bands"]
    assert bands.get("green", 0) == 0
    assert sum(bands.values()) == 1


def test_object_drilldown_includes_user_sample(http_server):
    base, _, _, s = http_server
    status, body = _get(
        f"{base}/api/object/segment/seg-1", token=s.dash_token)
    assert status == 200
    payload = json.loads(body)
    assert payload["user_sample"] == ["alice@example.com"]


def test_export_csv_is_text_csv_without_usernames(http_server):
    base, _, _, s = http_server
    req = urllib.request.Request(f"{base}/export.csv")
    req.add_header("X-Dash-Token", s.dash_token)
    with urllib.request.urlopen(req, timeout=5) as resp:
        ctype = resp.headers.get("Content-Type", "")
        body = resp.read().decode("utf-8")
    assert "text/csv" in ctype
    assert "alice@example.com" not in body
    assert "@" not in body


def test_post_sync_requires_token_and_invokes_callback(http_server):
    base, _, sync_calls, s = http_server
    status, _ = _get(f"{base}/api/sync", method="POST")
    assert status == 401
    assert sync_calls["n"] == 0
    status, _ = _get(f"{base}/api/sync", token=s.dash_token, method="POST")
    assert status == 200
    assert sync_calls["n"] == 1


def test_start_http_refuses_empty_dash_token(settings):
    from dataclasses import replace
    bad = replace(settings, dash_token="")
    with pytest.raises(ValueError):
        httpd.start_http(bad, Health())


def test_summary_includes_user_sample_size(http_server):
    base, _, _, s = http_server
    status, body = _get(f"{base}/api/objects/segment", token=s.dash_token)
    rows = json.loads(body)["rows"]
    seg = [r for r in rows if r["object_id"] == "seg-1"][0]
    assert seg["user_sample_size"] == 1   # one seeded username
    assert seg["user_sample_capped"] is False   # 1 < recent_users_max


def test_retired_drift_when_config_not_refreshed_this_sync(http_server, db):
    # Stamp a NEWER last_config_sync than seg-1's last_config_seen -> RETIRED.
    db.execute("INSERT INTO meta(key,value) VALUES('last_config_sync','2026-06-22T00:00:00+00:00') "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
    db.commit()
    base, _, _, s = http_server
    status, body = _get(f"{base}/api/objects/segment", token=s.dash_token)
    seg = [r for r in json.loads(body)["rows"] if r["object_id"] == "seg-1"][0]
    assert seg["drift"] == "retired"


def test_donotretire_list_excludes_object(settings, db):
    from dataclasses import replace
    _seed_summary_db(db)
    health = Health()
    s = replace(settings, http_port=0, donotretire_list=("Finance-App",))
    thread, server = httpd.start_http(s, health, _return_server=True)
    port = server.server_address[1]
    try:
        status, body = _get(f"http://127.0.0.1:{port}/api/objects/segment", token=s.dash_token)
        rows = json.loads(body)["rows"]
        assert all(r["name"] != "Finance-App" for r in rows)  # allowlisted -> excluded
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_user_sample_capped_flag_when_sample_full(settings, db):
    from dataclasses import replace
    _seed_summary_db(db)
    health = Health()
    s = replace(settings, http_port=0, recent_users_max=1)  # 1 seeded user saturates
    thread, server = httpd.start_http(s, health, _return_server=True)
    port = server.server_address[1]
    try:
        body = _get(f"http://127.0.0.1:{port}/api/objects/segment", token=s.dash_token)[1]
        seg = [r for r in json.loads(body)["rows"] if r["object_id"] == "seg-1"][0]
        assert seg["user_sample_size"] == 1
        assert seg["user_sample_capped"] is True
    finally:
        server.shutdown()
        thread.join(timeout=5)
