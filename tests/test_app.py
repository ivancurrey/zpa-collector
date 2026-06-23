import asyncio
import ssl
import threading
from dataclasses import replace

import pytest

from collector import app, httpd, receiver
from collector.counters import CounterEngine


def _write_self_signed(crt_path, key_path):
    """Generate a throwaway self-signed cert/key at test time (no key is committed
    to the repo). Uses the `cryptography` dev dependency."""
    import datetime
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc))
        .not_valid_after(datetime.datetime(2100, 1, 1, tzinfo=datetime.timezone.utc))
        .sign(key, hashes.SHA256())
    )
    crt_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))


def test_make_ssl_context_loads_existing_cert(settings, tmp_path):
    crt = tmp_path / "receiver.crt"
    key = tmp_path / "receiver.key"
    _write_self_signed(crt, key)
    s = replace(settings, lss_cert_path=str(crt), lss_key_path=str(key))
    ctx = app.make_ssl_context(s)
    assert isinstance(ctx, ssl.SSLContext)


def test_make_ssl_context_missing_cert_raises_actionable_error(settings, tmp_path):
    s = replace(settings,
                lss_cert_path=str(tmp_path / "nope.crt"),
                lss_key_path=str(tmp_path / "nope.key"))
    with pytest.raises(FileNotFoundError) as ei:
        app.make_ssl_context(s)
    msg = str(ei.value).lower()
    assert "cert" in msg
    assert "lss_cert_path" in msg or "provide" in msg


def test_run_inits_db_loads_engine_flushes_and_shuts_down(settings, tmp_path, monkeypatch):
    crt = tmp_path / "receiver.crt"
    key = tmp_path / "receiver.key"
    _write_self_signed(crt, key)
    s = replace(settings,
                lss_cert_path=str(crt), lss_key_path=str(key),
                http_port=0, flush_seconds=0,
                config_sync_hour=99)

    calls = {"serve": 0, "load": 0, "http": 0, "flush": 0}

    class FakeServer:
        def close(self): pass
        async def wait_closed(self): pass

    async def fake_serve(settings_, engine, get_name_index, health, *, ssl_context=None):
        calls["serve"] += 1
        return FakeServer()

    real_load = CounterEngine.load
    def counted_load(self, conn):
        calls["load"] += 1
        return real_load(self, conn)

    real_flush = CounterEngine.flush
    def counted_flush(self, conn):
        calls["flush"] += 1
        return real_flush(self, conn)

    def fake_start_http(settings_, health, *, on_sync=None):
        calls["http"] += 1
        return threading.Thread(target=lambda: None)

    monkeypatch.setattr(receiver, "serve", fake_serve)
    monkeypatch.setattr(CounterEngine, "load", counted_load)
    monkeypatch.setattr(CounterEngine, "flush", counted_flush)
    monkeypatch.setattr(httpd, "start_http", fake_start_http)
    monkeypatch.setattr(app, "_sync_now", lambda *a, **k: None)

    async def driver():
        task = asyncio.ensure_future(app.run(s))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(driver())

    assert calls["serve"] == 1
    assert calls["load"] == 1
    assert calls["http"] == 1
    assert calls["flush"] >= 1
