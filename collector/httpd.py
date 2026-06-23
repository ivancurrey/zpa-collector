import csv
import hmac
import io
import json
import threading
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from collector import classify
from collector import db as dbmod

CSV_COLUMNS = [
    "type", "name", "state", "band", "last_seen",
    "days_idle", "access_count", "confidence", "drift",
]

_METHODOLOGY_CAVEAT = (
    "Each row is an advisory candidate for review, never a delete order. "
    "The observation window cannot detect a usage cadence longer than the "
    "horizon (quarter-end, DR/failover, audit season); policy rule shadowing "
    "and Browser Access/PRA paths never appear in zpn_trans_log. Counts are "
    "advisory; presence/last_seen is the signal."
)


def build_csv(rows: list[dict], header: dict) -> str:
    """Render the candidate CSV: a leading '#'-comment header block (counts,
    coverage, thresholds, horizon, methodology caveat) then one CSV row per
    candidate. Usernames are NEVER included."""
    counts = header.get("counts", {}) or {}
    out = io.StringIO()
    out.write(f"# ZPA Hygiene candidate export — generated {header.get('generated_at', '')}\n")
    out.write(
        "# counts: active={a} attempted={t} silent={s}\n".format(
            a=counts.get("active", 0),
            t=counts.get("attempted", 0),
            s=counts.get("silent", 0),
        )
    )
    out.write(
        "# coverage: {pct}% over {days} days, {gaps} gaps\n".format(
            pct=header.get("coverage_pct", 0.0),
            days=header.get("coverage_days", 0),
            gaps=header.get("coverage_gaps", 0),
        )
    )
    out.write(
        "# thresholds: warm_days={w} stale_days={st}; horizon(window_days)={win}\n".format(
            w=header.get("warm_days", 0),
            st=header.get("stale_days", 0),
            win=header.get("window_days", 0),
        )
    )
    out.write(f"# caveat: {_METHODOLOGY_CAVEAT}\n")

    writer = csv.DictWriter(out, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow({
            "type": r.get("object_type", ""),
            "name": r.get("name", ""),
            "state": r.get("state", ""),
            "band": r.get("band", ""),
            "last_seen": r.get("last_seen", "") or "",
            "days_idle": "" if r.get("days_idle") is None else r.get("days_idle"),
            "access_count": r.get("access_count", 0),
            "confidence": r.get("confidence", ""),
            "drift": r.get("drift", "") or "",
        })
    return out.getvalue()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _summary_rows(conn) -> list[dict]:
    """config_object LEFT JOIN usage_counter -> one dict per CONFIG object.
    A config object with no usage row is silent (last_seen empty)."""
    cur = conn.cursor()
    cur.execute(
        "SELECT c.type AS object_type, c.id AS object_id, c.name AS name, "
        "       c.last_config_seen AS last_config_seen, "
        "       u.state AS state, u.access_count AS access_count, "
        "       u.last_seen_ts AS last_seen, u.first_seen_ts AS first_seen, "
        "       u.user_sample_json AS user_sample_json "
        "FROM config_object c "
        "LEFT JOIN usage_counter u "
        "  ON u.object_type = c.type AND u.object_id = c.id"
    )
    out = []
    for row in cur.fetchall():
        d = dict(row)
        d["access_count"] = d["access_count"] or 0
        d["last_seen"] = d["last_seen"] or ""
        d["is_ghost"] = False
        out.append(d)
    return out


def _ghost_rows(conn) -> list[dict]:
    """usage_counter rows with NO matching config_object = GHOST (usage for a
    name/id not in current config). Rare by design (usage is id-keyed from
    config), but surfaced when it occurs."""
    cur = conn.cursor()
    cur.execute(
        "SELECT u.object_type AS object_type, u.object_id AS object_id, "
        "       u.name AS name, u.state AS state, u.access_count AS access_count, "
        "       u.last_seen_ts AS last_seen, u.first_seen_ts AS first_seen, "
        "       u.user_sample_json AS user_sample_json "
        "FROM usage_counter u "
        "LEFT JOIN config_object c "
        "  ON c.type = u.object_type AND c.id = u.object_id "
        "WHERE c.id IS NULL"
    )
    out = []
    for row in cur.fetchall():
        d = dict(row)
        d["last_config_seen"] = None
        d["access_count"] = d["access_count"] or 0
        d["last_seen"] = d["last_seen"] or ""
        d["is_ghost"] = True
        out.append(d)
    return out


def _classified_rows(conn, *, warm_days, stale_days, now, settings) -> list[dict]:
    last_sync = dbmod.get_meta(conn, "last_config_sync")
    donot = set(settings.donotretire_list)
    rows = _summary_rows(conn) + _ghost_rows(conn)
    out = []
    for r in rows:
        name = r["name"]
        if name in donot:
            continue  # allowlisted (known-periodic): never a retire candidate
        last_seen = r["last_seen"]
        has_record = bool(last_seen)
        state = r.get("state") or ("silent" if not has_record else "attempted")
        band = classify.band(last_seen or None, now, warm_days, stale_days)
        idle = classify.days_idle(last_seen or None, now)
        observed_days = 0
        if r.get("first_seen") and last_seen:
            fd = classify.days_idle(r["first_seen"], now)
            ld = idle if idle is not None else 0
            observed_days = max(0, (fd or 0) - ld)
        if r.get("is_ghost"):
            drift = classify.DRIFT_GHOST
        elif (last_sync and has_record and r.get("last_config_seen")
              and r["last_config_seen"] != last_sync):
            drift = classify.DRIFT_RETIRED   # in config table but not refreshed this sync
        else:
            drift = classify.DRIFT_NONE
        try:
            user_count = len(json.loads(r.get("user_sample_json") or "[]"))
        except (ValueError, TypeError):
            user_count = 0
        out.append({
            "object_type": r["object_type"],
            "object_id": r["object_id"],
            "name": name,
            "state": state,
            "band": band,
            "last_seen": last_seen,
            "days_idle": idle,
            "access_count": r["access_count"],
            "confidence": classify.confidence(observed_days),
            "user_sample_size": user_count,
            "user_sample_capped": bool(user_count and user_count >= settings.recent_users_max),
            "drift": drift,
        })
    return out


def _thresholds(query: dict, settings) -> tuple[int, int, int]:
    def pick(key, default):
        vals = query.get(key)
        if not vals:
            return default
        try:
            return int(vals[0])
        except (TypeError, ValueError):
            return default
    return (pick("warm", settings.warm_days),
            pick("stale", settings.stale_days),
            pick("window", settings.window_days))


def start_http(settings, health, *, on_sync=None, _return_server=False):
    """Start a ThreadingHTTPServer in a daemon thread. The handler opens its OWN
    read_only db connection (WAL allows concurrent readers). Returns the thread
    (or (thread, server) when _return_server=True, for tests)."""
    if not settings.dash_token:
        raise ValueError(
            "DASH_TOKEN must be set; refusing to start the dashboard with an "
            "empty token (every authenticated route would be open).")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # quiet; logging is handled at app level

        def _open_db(self):
            return dbmod.connect(settings.db_path, read_only=True)

        def _authed(self) -> bool:
            tok = self.headers.get("X-Dash-Token")
            if tok and hmac.compare_digest(tok, settings.dash_token):
                return True
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Basic "):
                import base64
                try:
                    raw = base64.b64decode(auth[6:]).decode("utf-8")
                    return hmac.compare_digest(raw.split(":", 1)[-1], settings.dash_token)
                except Exception:
                    return False
            return False

        def _send_json(self, status, obj):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_text(self, status, text, ctype="text/plain; charset=utf-8"):
            body = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _unauthorized(self):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="zpa-collector"')
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            query = urllib.parse.parse_qs(parsed.query)

            if path == "/health":
                state, reasons = health.state(now=_now())
                status = 200 if state == "OK" else 503
                self._send_json(status, {"state": state, "reasons": reasons,
                                         "records_total": health.records_total,
                                         "active_conns": health.active_conns,
                                         "loop_lag_ms": health.loop_lag_ms})
                return

            if not self._authed():
                self._unauthorized()
                return

            warm, stale, window = _thresholds(query, settings)
            now = _now()

            if path == "/api/summary":
                conn = self._open_db()
                try:
                    rows = _classified_rows(conn, warm_days=warm,
                                            stale_days=stale, now=now,
                                            settings=settings)
                finally:
                    conn.close()
                summary = {}
                for r in rows:
                    t = r["object_type"]
                    bucket = summary.setdefault(
                        t, {"counts": {"active": 0, "attempted": 0, "silent": 0},
                            "bands": {}})
                    bucket["counts"][r["state"]] = bucket["counts"].get(r["state"], 0) + 1
                    bucket["bands"][r["band"]] = bucket["bands"].get(r["band"], 0) + 1
                self._send_json(200, summary)
                return

            if path.startswith("/api/objects/"):
                otype = urllib.parse.unquote(path[len("/api/objects/"):])
                conn = self._open_db()
                try:
                    rows = [r for r in _classified_rows(
                        conn, warm_days=warm, stale_days=stale, now=now,
                        settings=settings)
                        if r["object_type"] == otype]
                finally:
                    conn.close()
                self._send_json(200, {"rows": rows})
                return

            if path.startswith("/api/object/"):
                rest = path[len("/api/object/"):]
                parts = rest.split("/", 1)
                if len(parts) != 2:
                    self._send_json(404, {"error": "not found"})
                    return
                otype = urllib.parse.unquote(parts[0])
                oid = urllib.parse.unquote(parts[1])
                conn = self._open_db()
                try:
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT object_type, object_id, name, state, access_count, "
                        "first_seen_ts, last_seen_ts, user_sample_json, daily_json "
                        "FROM usage_counter WHERE object_type=? AND object_id=?",
                        (otype, oid))
                    row = cur.fetchone()
                finally:
                    conn.close()
                if row is None:
                    self._send_json(404, {"error": "not found"})
                    return
                d = dict(row)
                last_seen = d.get("last_seen_ts") or None
                detail = {
                    "object_type": d["object_type"],
                    "object_id": d["object_id"],
                    "name": d["name"],
                    "state": d.get("state") or "silent",
                    "band": classify.band(last_seen, now, warm, stale),
                    "last_seen": d.get("last_seen_ts") or "",
                    "days_idle": classify.days_idle(last_seen, now),
                    "access_count": d.get("access_count") or 0,
                    "user_sample": json.loads(d.get("user_sample_json") or "[]"),
                    "daily": json.loads(d.get("daily_json") or "{}"),
                }
                self._send_json(200, detail)
                return

            if path == "/export.csv":
                conn = self._open_db()
                try:
                    rows = _classified_rows(conn, warm_days=warm,
                                            stale_days=stale, now=now,
                                            settings=settings)
                    cov = health.coverage_summary(conn, window_days=window, now=now)
                finally:
                    conn.close()
                counts = {"active": 0, "attempted": 0, "silent": 0}
                for r in rows:
                    counts[r["state"]] = counts.get(r["state"], 0) + 1
                header = {
                    "counts": counts,
                    "coverage_pct": cov.get("pct", 0.0),
                    "coverage_days": cov.get("days", window),
                    "coverage_gaps": cov.get("gaps", 0),
                    "warm_days": warm, "stale_days": stale, "window_days": window,
                    "generated_at": now.isoformat(),
                }
                self._send_text(200, build_csv(rows, header),
                                ctype="text/csv; charset=utf-8")
                return

            if path == "/" or path.startswith("/static/"):
                import os
                rel = "index.html" if path == "/" else path[len("/static/"):]
                base_dir = os.path.join(os.path.dirname(__file__), "static")
                full = os.path.normpath(os.path.join(base_dir, rel))
                if not full.startswith(os.path.normpath(base_dir)) or not os.path.isfile(full):
                    self._send_text(404, "not found")
                    return
                with open(full, "r", encoding="utf-8") as fh:
                    self._send_text(200, fh.read(),
                                    ctype="text/html; charset=utf-8")
                return

            self._send_json(404, {"error": "not found"})

        def do_POST(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/api/sync":
                self._send_json(404, {"error": "not found"})
                return
            if not self._authed():
                self._unauthorized()
                return
            if on_sync is not None:
                on_sync()
            self._send_json(200, {"ok": True})

    server = ThreadingHTTPServer(("0.0.0.0", settings.http_port), Handler)
    thread = threading.Thread(target=server.serve_forever, name="httpd", daemon=True)
    thread.start()
    if _return_server:
        return thread, server
    return thread
