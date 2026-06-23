import socket
import threading

from collector import synth_sender
from collector.fields import SELF_CONN_USERNAME


def _recv_all(sock, expected_lines, timeout=5.0):
    sock.settimeout(timeout)
    buf = b""
    while buf.count(b"\n") < expected_lines:
        chunk = sock.recv(65536)
        if not chunk:
            break
        buf += chunk
    return buf


def test_default_lines_shape():
    lines = synth_sender.default_lines()
    assert len(lines) == 3
    assert all(isinstance(x, str) for x in lines)
    import json
    open_obj, close_obj, self_obj = (json.loads(x) for x in lines)
    assert open_obj["ConnectionID"] == close_obj["ConnectionID"]
    assert open_obj["ConnectionID"] != self_obj["ConnectionID"]
    assert self_obj["Username"] == SELF_CONN_USERNAME
    assert open_obj["Application"] == "Finance-App"


def test_send_writes_newline_delimited_lines_plain():
    lines = synth_sender.default_lines()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    host, port = listener.getsockname()

    received = {}

    def _accept():
        conn, _ = listener.accept()
        with conn:
            received["bytes"] = _recv_all(conn, expected_lines=len(lines))

    t = threading.Thread(target=_accept, daemon=True)
    t.start()

    synth_sender.send(host, port, lines, tls=False)
    t.join(timeout=5.0)
    listener.close()

    raw = received["bytes"]
    decoded = raw.decode("utf-8").rstrip("\n")
    out_lines = decoded.split("\n")
    assert len(out_lines) == len(lines)
    assert out_lines == lines
    assert any(SELF_CONN_USERNAME in ln for ln in out_lines)
