"""Synthetic LSS sender: a test driver and an SE field smoke-test tool.

Opens a (TLS or plain) TCP socket to a running collector receiver and writes
newline-delimited JSON records, exactly as a ZPA App Connector's LSS feed would.
Stdlib only.
"""

import json
import socket
import ssl

# A brokered open+close pair sharing one ConnectionID, plus one self-conn line.
_OPEN = {
    "ConnectionID": "synth-1",
    "ConnectionStatus": "open",
    "InternalReason": "OPEN_OR_ACTIVE_CONNECTION",
    "Username": "selftest@example.com",
    "Host": "app.int.example",
    "Application": "Finance-App",
    "AppGroup": "Finance",
    "Server": "fin-srv-01",
    "ServerIP": "10.20.0.5",
    "Policy": "Allow-Finance",
    "Connector": "ac1",
    "TimestampConnectionStart": "2026-06-20T12:00:00Z",
}
_CLOSE = {
    "ConnectionID": "synth-1",
    "ConnectionStatus": "close",
    "InternalReason": "BRK_MT_TERMINATED",
    "Username": "selftest@example.com",
    "Application": "Finance-App",
    "AppGroup": "Finance",
    "Server": "fin-srv-01",
    "ServerIP": "10.20.0.5",
    "Policy": "Allow-Finance",
    "Connector": "ac1",
    "TimestampConnectionStart": "2026-06-20T12:00:00Z",
    "TimestampConnectionEnd": "2026-06-20T12:05:00Z",
}
_SELF = {
    "ConnectionID": "synth-self",
    "ConnectionStatus": "open",
    "Username": "ZPA LSS Client",
    "Application": "zpa-lss",
    "Policy": "n/a",
}


def default_lines() -> list[str]:
    """A brokered open+close pair (same ConnectionID) + one self-conn line."""
    return [
        json.dumps(_OPEN, separators=(",", ":")),
        json.dumps(_CLOSE, separators=(",", ":")),
        json.dumps(_SELF, separators=(",", ":")),
    ]


def send(host: str, port: int, lines: list[str], *, tls: bool = True) -> None:
    """Open a (TLS or plain) socket to host:port and write each line + newline."""
    raw = socket.create_connection((host, port), timeout=10)
    try:
        if tls:
            ctx = ssl.create_default_context()
            # The receiver presents a self-signed cert; this is a smoke test.
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(raw, server_hostname=host)
        else:
            sock = raw
        try:
            for line in lines:
                sock.sendall(line.encode("utf-8") + b"\n")
        finally:
            if tls:
                try:
                    sock.unwrap()
                except (ssl.SSLError, OSError):
                    pass
    finally:
        raw.close()
