"""LSS line -> needed-field dict; self-conn filter; field-presence guard.

Stdlib only. The hot path reads only the fields below. ConnectionStatus is a
lifecycle field (Open/Close), NOT the success gate (spec §8); the success
predicate (is_brokered) keys off InternalReason/status (spec §5.4 / §15).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

SELF_CONN_USERNAME = "ZPA LSS Client"

# Fields the hot path reads (the LSS template recipe in README must emit these):
REQUIRED_FIELDS = ("Application", "ConnectionID")  # presence guard keys

DIMENSION_FIELDS = {  # object_type -> log field carrying the NAME
    "segment": "Application",
    "segment_group": "AppGroup",
    "rule": "Policy",
    "server": "Server",
    "connector": "Connector",
}

# Explicit policy-block / error signals in InternalReason that mean "not brokered"
# (kept conservative — spec §5.4 / §15: only an explicit block flips the bit).
_BLOCK_REASON_SUBSTRINGS = ("REJECTED_BY_POLICY", "NO_POLICY_FOUND")


def parse_line(line: str | bytes) -> dict | None:
    """json.loads a single LSS line; return dict, or None on malformed JSON.

    Caller counts parse_error on None. A valid-JSON non-object (array/number)
    also returns None — it is not a usable record.
    """
    if isinstance(line, bytes):
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError:
            return None
    try:
        obj = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    return obj


def is_self_conn(obj: dict) -> bool:
    """True when the record is the LSS stream's own self-connection."""
    return obj.get("Username") == SELF_CONN_USERNAME


def missing_required(obj: dict) -> list[str]:
    """Return REQUIRED_FIELDS that are absent or empty; [] when all present."""
    missing = []
    for key in REQUIRED_FIELDS:
        value = obj.get(key)
        if value is None or value == "":
            missing.append(key)
    return missing


def is_brokered(obj: dict) -> bool:
    """Success predicate over InternalReason/status (NOT ConnectionStatus).

    Conservative per spec §5.4 / §15: default True (any real record = usage);
    only an explicit policy-block / error status flips it to False. We never
    gate on ConnectionStatus (that is lifecycle Open/Close, spec §8).
    """
    reason = obj.get("InternalReason")
    if not reason:
        return True
    reason_upper = str(reason).upper()
    for marker in _BLOCK_REASON_SUBSTRINGS:
        if marker in reason_upper:
            return False
    return True


def connection_id(obj: dict) -> str | None:
    """The connection dedup key, or None if absent."""
    value = obj.get("ConnectionID")
    return value if value else None


def server_identity(obj: dict) -> str | None:
    """Resolved server name: Server, else ServerIP, else None."""
    return obj.get("Server") or obj.get("ServerIP") or None


def event_time(obj: dict) -> datetime:
    """Parse TimestampConnectionStart to tz-aware UTC; fall back to now().

    Accepts ISO-8601 with a trailing 'Z' (mapped to +00:00). Any naive parsed
    value is assumed UTC. On absence or parse failure, returns now() (UTC).
    """
    raw = obj.get("TimestampConnectionStart")
    if raw:
        text = str(raw)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
    return datetime.now(timezone.utc)
