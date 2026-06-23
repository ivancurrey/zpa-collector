import pytest

from collector import cli


def test_default_runs_app_run(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "load_settings", lambda: "SETTINGS")

    def fake_asyncio_run(coro):
        seen["coro"] = coro
        coro.close()  # don't actually execute the loop
        return None

    monkeypatch.setattr(cli.asyncio, "run", fake_asyncio_run)

    async def fake_run(settings):
        seen["settings"] = settings

    monkeypatch.setattr(cli.app, "run", fake_run)
    rc = cli.main([])
    assert rc == 0
    assert "coro" in seen


def test_selftest_dispatch(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(cli.selftest, "run_selftest", lambda: (called.__setitem__("n", 1) or 0))
    rc = cli.main(["--selftest"])
    assert rc == 0
    assert called["n"] == 1


def test_synth_send_dispatch_parses_args(monkeypatch):
    captured = {}

    def fake_send(host, port, lines, *, tls=True):
        captured["host"] = host
        captured["port"] = port
        captured["tls"] = tls
        captured["nlines"] = len(lines)

    monkeypatch.setattr(cli.synth_sender, "send", fake_send)
    monkeypatch.setattr(cli.synth_sender, "default_lines", lambda: ["a", "b", "c"])
    rc = cli.main(["synth-send", "--host", "1.2.3.4", "--port", "4639",
                   "--count", "2", "--no-tls"])
    assert rc == 0
    assert captured["host"] == "1.2.3.4"
    assert captured["port"] == 4639
    assert captured["tls"] is False
    assert captured["nlines"] == 2
