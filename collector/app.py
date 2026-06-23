"""Async wiring: receiver + counter flush + nightly/on-demand config sync + HTTP.

make_ssl_context loads a provisioned cert/key (the App Connector requires a
PUBLIC-CA-signed receiver cert — self-signed is rejected; an absent cert raises
an actionable error pointing at the runbook).
run() owns the asyncio loop; the HTTP dashboard runs in its own thread.
"""

import asyncio
import logging
import os
import ssl
from datetime import datetime, timezone

from collector import db as dbmod
from collector import httpd
from collector import receiver
from collector.config import Settings
from collector.counters import CounterEngine
from collector.health import Health

log = logging.getLogger("collector.app")

# Holds the active Settings so _sync_now can construct a OneAPIClient lazily.
_current_settings: list = [None]


def make_ssl_context(settings: Settings) -> ssl.SSLContext:
    """Build the receiver's server-side TLS context from the cert/key on the
    mounted volume. The App Connector validates the receiver's cert chain, so it
    must be PUBLIC-CA-signed (or signed by the connector's enrollment CA) — a
    self-signed cert is rejected with an 'Unknown CA' TLS failure (see README
    §2). We require the cert/key to exist and raise an actionable error if not."""
    crt = settings.lss_cert_path
    key = settings.lss_key_path
    missing = [p for p in (crt, key) if not os.path.isfile(p)]
    if missing:
        raise FileNotFoundError(
            "Receiver TLS cert/key not found at "
            f"LSS_CERT_PATH={crt!r} / LSS_KEY_PATH={key!r}. "
            "Provide a cert+key signed by a PUBLIC root CA (or the App "
            "Connector's enrollment CA) on the data volume — ZPA rejects a "
            "self-signed receiver cert with an 'Unknown CA' TLS failure (README "
            f"§2). Missing: {', '.join(missing)}."
        )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=crt, keyfile=key)
    return ctx


def _sync_now(client, conn, health, set_index, now):
    """Run one config sync off-loop; update the NameIndex holder + health.
    Imported lazily so importing app never pulls oneapi at module load.
    `now` is positional (called via run_in_executor, which can't pass kwargs)."""
    from collector import config_sync
    from collector import oneapi
    if client is None:
        client = oneapi.OneAPIClient(_current_settings[0])
    result = config_sync.sync(client, conn, now=now)
    health.note_config_sync(now.isoformat(), result.ok)
    if result.ok:
        set_index(result.name_index)
    return result


async def run(settings: Settings) -> None:
    """Init db, load engine, start the HTTP thread, schedule nightly + on-demand
    config sync, start the receiver, and run the flush loop measuring event-loop
    lag into health. Shuts down cleanly on cancellation."""
    _current_settings[0] = settings
    loop = asyncio.get_running_loop()
    health = Health()

    # --- db + engine ---
    conn = dbmod.connect(settings.db_path)
    dbmod.init_schema(conn)
    dbmod.migrate(conn)
    engine = CounterEngine(settings)
    engine.load(conn)

    # --- name index holder (swapped atomically by sync) ---
    name_index_holder: list = [None]
    try:
        from collector import config_sync
        name_index_holder[0] = config_sync.load_name_index(conn)
    except Exception:  # pragma: no cover - boot before first sync
        name_index_holder[0] = None

    def get_name_index():
        return name_index_holder[0]

    def set_name_index(idx):
        name_index_holder[0] = idx

    sync_requested = asyncio.Event()

    def request_sync():
        loop.call_soon_threadsafe(sync_requested.set)

    # --- TLS context FIRST: raises early if the cert is absent, before we bind
    #     the HTTP port (so a retry after provisioning the cert finds it free).
    ssl_context = make_ssl_context(settings)

    # --- HTTP serving thread (POST /api/sync flips the event) ---
    http_thread = httpd.start_http(settings, health, on_sync=request_sync)

    # --- TLS receiver ---
    server = await receiver.serve(settings, engine, get_name_index, health,
                                  ssl_context=ssl_context)

    async def run_sync():
        now = datetime.now(timezone.utc)
        await loop.run_in_executor(
            None, _sync_now, None, conn, health, set_name_index, now)

    async def flush_once():
        n = await loop.run_in_executor(None, engine.flush, conn)
        health.note_flush(True)
        return n

    flush_interval = max(0.0, float(settings.flush_seconds))
    last_synced_hour_key = None
    last_records_total = 0
    try:
        while True:
            now = datetime.now(timezone.utc)
            hour_key = now.strftime("%Y-%m-%d") + f":{now.hour}"
            if now.hour == settings.config_sync_hour and hour_key != last_synced_hour_key:
                last_synced_hour_key = hour_key
                await run_sync()
            if sync_requested.is_set():
                sync_requested.clear()
                await run_sync()

            try:
                await flush_once()
            except Exception as exc:  # flush failure: loud, keep serving
                log.error("flush failed: %s", exc)
                health.note_flush(False)

            # Coverage heartbeat: mark the current hour covered ONLY when records
            # actually arrived since the last flush. Bumping every flush would
            # mark every up-hour covered and make an LSS outage undetectable.
            if health.records_total > last_records_total:
                last_records_total = health.records_total
                try:
                    await loop.run_in_executor(
                        None, engine.record_coverage, conn,
                        datetime.now(timezone.utc))
                except Exception as exc:
                    log.error("record_coverage failed: %s", exc)

            expected = max(flush_interval, 0.001)
            slept_at = loop.time()
            await asyncio.sleep(flush_interval)
            actual = loop.time() - slept_at
            lag_ms = max(0.0, (actual - expected) * 1000.0)
            health.note_loop_lag(lag_ms)
    except asyncio.CancelledError:
        log.info("shutting down: flushing and closing")
        try:
            await loop.run_in_executor(None, engine.flush, conn)
        except Exception:
            pass
        server.close()
        await server.wait_closed()
        conn.close()
        raise
