"""LSS TLS receiver: explicit newline framing + asyncio hot path (stdlib only)."""

import asyncio
import time
from datetime import datetime, timezone

from collector import fields


def iter_frames(buffer: bytearray, max_line_bytes: int) -> "tuple[list[bytes], bytearray]":
    """Split a byte buffer into complete newline-delimited lines.

    Returns ``(lines, remaining)`` where ``remaining`` is the bytes after the
    last newline (a partial trailing line, carried to the next read). Any single
    line longer than ``max_line_bytes`` is dropped. An over-long *partial*
    trailing run (no newline yet, past the cap) is discarded too, so the buffer
    can never grow unbounded.
    """
    lines: list[bytes] = []
    start = 0
    while True:
        nl = buffer.find(b"\n", start)
        if nl == -1:
            break
        line = bytes(buffer[start:nl])
        if len(line) <= max_line_bytes:
            lines.append(line)
        # else: over-long complete line -> drop silently
        start = nl + 1
    remaining = bytearray(buffer[start:])
    if len(remaining) > max_line_bytes:
        # over-long partial with no terminator yet: discard so we never buffer
        # forever; the next newline (if any) re-syncs the stream.
        remaining = bytearray()
    return lines, remaining


async def serve(settings, engine, get_name_index, health, *, ssl_context=None):
    """Start the asyncio LSS receiver and return the running ``asyncio.Server``.

    Per connection: read bytes -> ``iter_frames`` -> ``parse_line`` (parse_error
    on None) -> ``missing_required`` guard (flags template_mismatch) -> dropped
    if self-conn -> ``engine.ingest``. One bad connection never crashes the
    server; exceptions inside a handler are swallowed after cleanup.
    """

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        health.on_connect()
        buffer = bytearray()
        max_line = settings.max_line_bytes
        name_index = get_name_index()
        try:
            while True:
                try:
                    chunk = await reader.read(65536)
                except (ConnectionResetError, asyncio.IncompleteReadError, OSError):
                    break
                if not chunk:
                    break
                buffer.extend(chunk)
                lines, buffer = iter_frames(buffer, max_line)
                now = datetime.now(timezone.utc)
                mono = time.monotonic()
                for raw in lines:
                    _process_line(raw, engine, name_index, health,
                                  now=now, mono=mono)
        except Exception:
            # Resilience: a single misbehaving connection must not take down the
            # server. Drop it quietly; the next connection is unaffected.
            pass
        finally:
            health.on_disconnect()
            try:
                writer.close()
            except Exception:
                pass

    server = await asyncio.start_server(
        handle, host="0.0.0.0", port=settings.lss_port, ssl=ssl_context)
    return server


def _process_line(raw: bytes, engine, name_index, health, *, now, mono) -> None:
    obj = fields.parse_line(raw)
    if obj is None:
        health.on_parse_error()
        return
    if fields.missing_required(obj):
        # records flow but the LSS template omits fields we need: loud guard.
        health.flag_template_mismatch()
        return
    if fields.is_self_conn(obj):
        return
    counted = engine.ingest(obj, name_index, now=now, mono=mono)
    if counted:
        health.on_record(now.isoformat())
