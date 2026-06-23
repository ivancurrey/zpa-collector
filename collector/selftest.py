"""--selftest harness: loopback receiver + golden lines -> assert dedup + CSV.

Fully self-contained: a trivial identity NameIndex resolves name->name (no
OneAPI), and a PLAIN-TCP loopback receiver (no cert needed — TLS and
make_ssl_context are validated by test_app and by the SE field smoke test that
points synth_sender at the real provisioned TLS receiver). Stdlib only.
"""

import asyncio
import os
import tempfile
from datetime import datetime, timezone

from collector import db as dbmod
from collector import synth_sender
from collector.config import Settings
from collector.counters import CounterEngine
from collector.health import Health
from collector.httpd import build_csv
from collector.receiver import serve


class _IdentityNameIndex:
    """Resolves any (type, name) to the name itself — no config sync required."""

    def resolve(self, object_type, name, microtenant_id=None):
        return name


async def _run() -> int:
    with tempfile.TemporaryDirectory(prefix="zpa-selftest-") as tmpdir:
        db_path = os.path.join(tmpdir, "state.db")

        settings = Settings(
            zs_vanity_domain="selftest", zs_client_id="id", zs_client_secret="sec",
            zpa_customer_id="0", dash_token="selftest",
            lss_port=0, db_path=db_path,
        )

        conn = dbmod.connect(db_path)
        dbmod.init_schema(conn)

        engine = CounterEngine(settings)
        health = Health()
        name_index = _IdentityNameIndex()

        # Plain-TCP loopback receiver (no cert needed for the self-test).
        server = await serve(settings, engine, lambda: name_index, health,
                             ssl_context=None)
        bound_port = server.sockets[0].getsockname()[1]

        loop = asyncio.get_running_loop()
        lines = synth_sender.default_lines()
        try:
            await loop.run_in_executor(
                None, lambda: synth_sender.send("127.0.0.1", bound_port, lines, tls=False))
            for _ in range(50):
                await asyncio.sleep(0.05)
                seg = [r for r in engine.snapshot()
                       if r.object_type == "segment" and r.object_id == "Finance-App"]
                if seg and seg[0].access_count >= 1:
                    break
        finally:
            server.close()
            await server.wait_closed()

        engine.flush(conn)

        seg_rows = [r for r in engine.snapshot()
                    if r.object_type == "segment" and r.object_id == "Finance-App"]
        if len(seg_rows) != 1:
            print(f"FAIL: expected exactly one Finance-App segment row, got {len(seg_rows)}")
            conn.close()
            return 1
        if seg_rows[0].access_count != 1:
            print("FAIL: Finance-App segment counted "
                  f"{seg_rows[0].access_count} times (open+close must dedup to 1)")
            conn.close()
            return 1

        csv_rows = [{
            "object_type": r.object_type, "name": r.name, "state": r.state, "band": "green",
            "last_seen": r.last_seen_ts, "days_idle": 0, "access_count": r.access_count,
            "confidence": "low", "drift": "",
        } for r in seg_rows]
        header = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "counts": {"active": len(seg_rows), "attempted": 0, "silent": 0},
            "coverage_pct": 0.0, "coverage_days": settings.window_days, "coverage_gaps": 0,
            "warm_days": settings.warm_days, "stale_days": settings.stale_days,
            "window_days": settings.window_days,
        }
        csv_text = build_csv(csv_rows, header)
        conn.close()
        if not csv_text or "Finance-App" not in csv_text:
            print("FAIL: build_csv produced no usable output")
            return 1

        print("PASS: open+close deduped to one Finance-App count; CSV export OK")
        return 0


def run_selftest() -> int:
    return asyncio.run(_run())
