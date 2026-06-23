import json
from datetime import datetime, timezone

from collector import fields
from tests.fixtures.sample_lines import SAMPLE_OPEN, SAMPLE_CLOSE, SAMPLE_SELF


def test_parse_line_valid_json_returns_dict():
    obj = fields.parse_line(SAMPLE_OPEN)
    assert isinstance(obj, dict)
    assert obj["ConnectionID"] == "c-1"
    assert obj["Application"] == "Finance-App"


def test_parse_line_accepts_bytes():
    obj = fields.parse_line(SAMPLE_OPEN.encode("utf-8"))
    assert isinstance(obj, dict)
    assert obj["Application"] == "Finance-App"


def test_parse_line_malformed_returns_none():
    assert fields.parse_line("{not json") is None
    assert fields.parse_line(b"\x00\x01 not json") is None
    assert fields.parse_line("") is None


def test_parse_line_non_object_json_returns_none():
    assert fields.parse_line("[1, 2, 3]") is None
    assert fields.parse_line("42") is None


def test_is_self_conn_true_for_self_line():
    obj = fields.parse_line(SAMPLE_SELF)
    assert fields.is_self_conn(obj) is True


def test_is_self_conn_false_for_real_record():
    obj = fields.parse_line(SAMPLE_OPEN)
    assert fields.is_self_conn(obj) is False


def test_is_self_conn_false_when_username_absent():
    assert fields.is_self_conn({"ConnectionID": "x"}) is False


def test_missing_required_empty_for_sample_open():
    obj = fields.parse_line(SAMPLE_OPEN)
    assert fields.missing_required(obj) == []


def test_missing_required_lists_stripped_keys():
    obj = fields.parse_line(SAMPLE_OPEN)
    obj.pop("Application")
    obj["ConnectionID"] = ""  # present but empty -> still missing
    assert fields.missing_required(obj) == ["Application", "ConnectionID"]


def test_is_brokered_true_for_normal_open_record():
    obj = fields.parse_line(SAMPLE_OPEN)
    assert fields.is_brokered(obj) is True


def test_is_brokered_true_for_normal_close_record():
    obj = fields.parse_line(SAMPLE_CLOSE)
    assert fields.is_brokered(obj) is True


def test_is_brokered_true_when_no_status_info_present():
    assert fields.is_brokered({"ConnectionID": "c-9", "Application": "App"}) is True


def test_is_brokered_false_on_explicit_policy_block():
    blocked = {"ConnectionID": "c-9", "InternalReason": "CONN_REJECTED_BY_POLICY"}
    assert fields.is_brokered(blocked) is False


def test_is_brokered_false_on_no_policy_found():
    blocked = {"ConnectionID": "c-9", "InternalReason": "NO_POLICY_FOUND_FOR_REQUEST"}
    assert fields.is_brokered(blocked) is False


def test_is_brokered_ignores_connection_status_lifecycle():
    obj = {"ConnectionID": "c-9", "ConnectionStatus": "close"}
    assert fields.is_brokered(obj) is True


def test_connection_id_returns_value():
    obj = fields.parse_line(SAMPLE_OPEN)
    assert fields.connection_id(obj) == "c-1"


def test_connection_id_none_when_absent():
    assert fields.connection_id({"Application": "App"}) is None


def test_server_identity_prefers_server():
    obj = fields.parse_line(SAMPLE_OPEN)
    assert fields.server_identity(obj) == "fin-srv-01"


def test_server_identity_falls_back_to_server_ip():
    obj = {"ServerIP": "10.20.0.5"}
    assert fields.server_identity(obj) == "10.20.0.5"


def test_server_identity_none_when_neither_present():
    assert fields.server_identity({"Application": "App"}) is None


def test_event_time_parses_timestamp_connection_start_as_utc():
    obj = fields.parse_line(SAMPLE_OPEN)
    ts = fields.event_time(obj)
    assert ts == datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)
    assert ts.tzinfo is not None


def test_event_time_falls_back_to_now_when_absent():
    before = datetime.now(timezone.utc)
    ts = fields.event_time({"Application": "App"})
    after = datetime.now(timezone.utc)
    assert ts.tzinfo is not None
    assert before <= ts <= after


def test_event_time_falls_back_to_now_when_unparseable():
    before = datetime.now(timezone.utc)
    ts = fields.event_time({"TimestampConnectionStart": "not-a-date"})
    after = datetime.now(timezone.utc)
    assert ts.tzinfo is not None
    assert before <= ts <= after
