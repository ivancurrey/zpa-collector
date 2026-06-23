import asyncio
import socket
import threading

import pytest

from collector.receiver import iter_frames, serve


# ---- pure framing tests ---------------------------------------------------

def test_iter_frames_multiple_complete_lines_in_one_buffer():
    buf = bytearray(b"alpha\nbravo\ncharlie\n")
    lines, remaining = iter_frames(buf, max_line_bytes=65536)
    assert lines == [b"alpha", b"bravo", b"charlie"]
    assert bytes(remaining) == b""


def test_iter_frames_partial_trailing_line_retained_then_completed():
    buf = bytearray(b"alpha\nbravo\nchar")
    lines, remaining = iter_frames(buf, max_line_bytes=65536)
    assert lines == [b"alpha", b"bravo"]
    assert bytes(remaining) == b"char"
    remaining.extend(b"lie\ndelta\n")
    lines2, remaining2 = iter_frames(remaining, max_line_bytes=65536)
    assert lines2 == [b"charlie", b"delta"]
    assert bytes(remaining2) == b""


def test_iter_frames_overlong_line_dropped_without_breaking_following_frames():
    big = b"x" * 50
    buf = bytearray(b"good1\n" + big + b"\ngood2\n")
    lines, remaining = iter_frames(buf, max_line_bytes=16)
    assert lines == [b"good1", b"good2"]
    assert bytes(remaining) == b""


def test_iter_frames_overlong_partial_prefix_is_discarded_not_buffered():
    buf = bytearray(b"good\n" + b"y" * 100)
    lines, remaining = iter_frames(buf, max_line_bytes=16)
    assert lines == [b"good"]
    assert bytes(remaining) == b""


# ---- integration test for serve() ----------------------------------------

from datetime import datetime, timezone

from tests.fixtures.sample_lines import SAMPLE_OPEN


class FakeNameIndex:
    def resolve(self, object_type, name, microtenant_id=None):
        return f"{object_type}:{name}"


class FakeHealth:
    def __init__(self):
        self.active_conns = 0
        self.records_total = 0
        self.parse_errors = 0
        self.template_mismatch = False
        self.last_record_ts = None

    def on_connect(self):
        self.active_conns += 1

    def on_disconnect(self):
        self.active_conns -= 1

    def on_record(self, ts):
        self.records_total += 1
        self.last_record_ts = ts

    def on_parse_error(self):
        self.parse_errors += 1

    def flag_template_mismatch(self):
        self.template_mismatch = True


def _send_line(host, port, payload: bytes):
    with socket.create_connection((host, port), timeout=5) as sock:
        sock.sendall(payload)
        sock.shutdown(socket.SHUT_WR)
        sock.recv(16)


async def _drain_until(predicate, timeout_s: float = 5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


@pytest.mark.asyncio
async def test_serve_counts_a_real_line_over_plain_tcp(settings, db):
    from collector.counters import CounterEngine

    settings = _with(settings, lss_port=0, max_line_bytes=65536)
    engine = CounterEngine(settings)
    name_index = FakeNameIndex()
    health = FakeHealth()

    server = await serve(settings, engine, lambda: name_index, health,
                         ssl_context=None)
    host, port = _server_addr(server)
    try:
        await asyncio.get_running_loop().run_in_executor(
            None, _send_line, host, port, SAMPLE_OPEN.encode() + b"\n")
        assert await _drain_until(lambda: health.records_total >= 1)
        assert health.template_mismatch is False
        assert health.parse_errors == 0
        rows = engine.snapshot()
        assert any(r.object_type == "segment" for r in rows)
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_serve_bumps_parse_errors_on_malformed_line(settings, db):
    from collector.counters import CounterEngine

    settings = _with(settings, lss_port=0)
    engine = CounterEngine(settings)
    health = FakeHealth()
    server = await serve(settings, engine, lambda: FakeNameIndex(), health,
                         ssl_context=None)
    host, port = _server_addr(server)
    try:
        await asyncio.get_running_loop().run_in_executor(
            None, _send_line, host, port, b"{not json\n")
        assert await _drain_until(lambda: health.parse_errors >= 1)
        assert health.records_total == 0
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_serve_flags_template_mismatch_when_required_fields_missing(settings, db):
    from collector.counters import CounterEngine

    settings = _with(settings, lss_port=0)
    engine = CounterEngine(settings)
    health = FakeHealth()
    server = await serve(settings, engine, lambda: FakeNameIndex(), health,
                         ssl_context=None)
    host, port = _server_addr(server)
    try:
        await asyncio.get_running_loop().run_in_executor(
            None, _send_line, host, port, b'{"Username":"a@b.c"}\n')
        assert await _drain_until(lambda: health.template_mismatch is True)
        assert health.records_total == 0
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_serve_survives_a_bad_connection(settings, db):
    from collector.counters import CounterEngine

    settings = _with(settings, lss_port=0)
    engine = CounterEngine(settings)
    health = FakeHealth()
    server = await serve(settings, engine, lambda: FakeNameIndex(), health,
                         ssl_context=None)
    host, port = _server_addr(server)
    try:
        bad = socket.create_connection((host, port), timeout=5)
        bad.sendall(b"partial-no-newline")
        bad.close()
        await asyncio.get_running_loop().run_in_executor(
            None, _send_line, host, port, SAMPLE_OPEN.encode() + b"\n")
        assert await _drain_until(lambda: health.records_total >= 1)
    finally:
        server.close()
        await server.wait_closed()


def _server_addr(server):
    sock = server.sockets[0]
    host, port = sock.getsockname()[:2]
    return ("127.0.0.1" if host == "0.0.0.0" else host), port


def _with(settings, **changes):
    from dataclasses import replace
    return replace(settings, **changes)
