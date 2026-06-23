from datetime import datetime, timezone

import pytest

from collector import db as dbmod
from collector.config import Settings
from collector.config_sync import sync, load_name_index
from collector.oneapi import OneAPIError


NOW = datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc)


def _settings(tmp_path):
    return Settings(zs_vanity_domain="acme", zs_client_id="id", zs_client_secret="sec",
                    zpa_customer_id="123", dash_token="t",
                    db_path=str(tmp_path / "state.db"))


@pytest.fixture
def conn(tmp_path):
    c = dbmod.connect(_settings(tmp_path).db_path)
    dbmod.init_schema(c)
    yield c
    c.close()


class FakeClient:
    """Canned OneAPI client. data = {mt: {resource: [rows...]}}."""
    def __init__(self, data, microtenants=None, raise_on=None):
        self._data = data
        self._mts = microtenants if microtenants is not None else [None]
        self._raise_on = raise_on  # (resource, mt) that should raise

    def list_microtenants(self):
        return list(self._mts)

    def paged_get(self, resource, *, microtenant_id=None, version="v1"):
        if self._raise_on == (resource, microtenant_id):
            raise OneAPIError("GET boom (500)")
        return list(self._data.get(microtenant_id, {}).get(resource, []))


def _rows(conn, sql, *args):
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def test_sync_upserts_objects_and_config_edges(conn):
    data = {None: {
        "application": [{"id": "seg1", "name": "Finance-App", "enabled": True,
                         "segmentGroupId": "sg1"}],
        "segmentGroup": [{"id": "sg1", "name": "Finance", "enabled": True}],
        "policySet/rules/policyType/ACCESS_POLICY": [
            {"id": "r1", "name": "Allow-Finance"}],
        "serverGroup": [{"id": "svg1", "name": "Fin-Servers",
                         "servers": [{"id": "srv1"}]}],
        "appConnectorGroup": [{"id": "cg1", "name": "DC-Connectors"}],
        "connector": [{"id": "conn1", "name": "ac1"}],
        "server": [{"id": "srv1", "name": "fin-srv-01"}],
    }}
    res = sync(FakeClient(data), conn, now=NOW)
    assert res.ok is True
    objs = _rows(conn, "SELECT id, type, name, microtenant_id, "
                       "first_config_seen, last_config_seen FROM config_object "
                       "ORDER BY type, id")
    by_type = {(o["type"], o["id"]) for o in objs}
    assert ("segment", "seg1") in by_type
    assert ("segment_group", "sg1") in by_type
    assert ("rule", "r1") in by_type
    assert ("server_group", "svg1") in by_type
    assert ("connector_group", "cg1") in by_type
    assert ("connector", "conn1") in by_type
    assert ("server", "srv1") in by_type
    assert res.object_count == 7
    for o in objs:
        assert o["first_config_seen"] == NOW.isoformat()
        assert o["last_config_seen"] == NOW.isoformat()

    edges = {(e["parent_type"], e["parent_id"], e["child_type"], e["child_id"], e["source"])
             for e in _rows(conn, "SELECT * FROM config_edge")}
    assert ("segment_group", "sg1", "segment", "seg1", "config") in edges
    assert ("server_group", "svg1", "server", "srv1", "config") in edges


def test_name_index_resolves_within_type_and_microtenant(conn):
    data = {
        None: {"application": [{"id": "seg-default", "name": "Shared-App"}]},
        "mt-1": {"application": [{"id": "seg-mt1", "name": "Shared-App"}]},
    }
    res = sync(FakeClient(data, microtenants=[None, "mt-1"]), conn, now=NOW)
    assert res.ok is True
    idx = res.name_index
    assert idx.resolve("segment", "Shared-App", microtenant_id=None) == "seg-default"
    assert idx.resolve("segment", "Shared-App", microtenant_id="mt-1") == "seg-mt1"
    assert idx.resolve("segment", "Shared-App") is None


def test_name_index_unique_name_falls_back_across_microtenants(conn):
    data = {
        None: {"application": []},
        "mt-1": {"application": [{"id": "seg-only", "name": "Solo-App"}]},
    }
    res = sync(FakeClient(data, microtenants=[None, "mt-1"]), conn, now=NOW)
    assert res.name_index.resolve("segment", "Solo-App") == "seg-only"
    assert res.name_index.resolve("segment", "Nope") is None


def test_load_name_index_rebuilds_from_db(conn):
    data = {None: {"application": [{"id": "seg1", "name": "Finance-App"}]}}
    sync(FakeClient(data), conn, now=NOW)
    idx = load_name_index(conn)
    assert idx.resolve("segment", "Finance-App") == "seg1"


def test_retired_detection_for_object_gone_with_prior_usage(conn):
    data1 = {None: {"application": [
        {"id": "seg1", "name": "App-One"}, {"id": "seg2", "name": "App-Two"}]}}
    sync(FakeClient(data1), conn, now=NOW)
    conn.execute("INSERT INTO usage_counter "
                 "(object_type, object_id, name, state, access_count, "
                 " first_seen_ts, last_seen_ts, user_sample_json, daily_json, updated_at) "
                 "VALUES ('segment','seg2','App-Two','active',3,?,?,'[]','{}',?)",
                 (NOW.isoformat(), NOW.isoformat(), NOW.isoformat()))
    conn.commit()
    data2 = {None: {"application": [{"id": "seg1", "name": "App-One"}]}}
    res = sync(FakeClient(data2), conn, now=NOW)
    assert res.ok is True
    assert ("segment", "seg2") in res.retired_ids
    assert ("segment", "seg1") not in res.retired_ids


def test_gone_object_without_usage_is_not_retired(conn):
    data1 = {None: {"application": [
        {"id": "seg1", "name": "App-One"}, {"id": "seg2", "name": "App-Two"}]}}
    sync(FakeClient(data1), conn, now=NOW)
    data2 = {None: {"application": [{"id": "seg1", "name": "App-One"}]}}
    res = sync(FakeClient(data2), conn, now=NOW)
    assert res.retired_ids == []


def test_client_failure_returns_not_ok_and_no_partial_commit(conn):
    data1 = {None: {"application": [{"id": "seg1", "name": "App-One"}]}}
    sync(FakeClient(data1), conn, now=NOW)
    before = [dict(r) for r in conn.execute(
        "SELECT id, name FROM config_object").fetchall()]
    data2 = {None: {"application": [{"id": "seg2", "name": "App-Two"}]}}
    bad = FakeClient(data2, raise_on=("server", None))
    res = sync(bad, conn, now=NOW)
    assert res.ok is False
    assert res.error
    after = [dict(r) for r in conn.execute(
        "SELECT id, name FROM config_object").fetchall()]
    assert after == before  # prior config intact, no partial 'seg2'
    assert res.name_index.resolve("segment", "App-One") == "seg1"


def test_first_config_seen_preserved_last_advanced_on_resync(conn):
    t1 = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 21, 0, 0, 0, tzinfo=timezone.utc)
    data = {None: {"application": [{"id": "seg1", "name": "Finance-App"}]}}
    sync(FakeClient(data), conn, now=t1)
    sync(FakeClient(data), conn, now=t2)
    row = dict(conn.execute(
        "SELECT first_config_seen, last_config_seen FROM config_object "
        "WHERE id='seg1'").fetchone())
    assert row["first_config_seen"] == t1.isoformat()   # set once, preserved
    assert row["last_config_seen"] == t2.isoformat()    # advanced on re-sync


def test_sync_stamps_last_config_sync(conn):
    from collector import db as dbmod
    data = {None: {"application": [{"id": "seg1", "name": "Finance-App"}]}}
    sync(FakeClient(data), conn, now=NOW)
    assert dbmod.get_meta(conn, "last_config_sync") == NOW.isoformat()


def test_default_microtenant_overlap_stores_each_object_once(conn):
    """Default microtenant id '0' returns the SAME objects as the no-filter pull;
    a globally-unique id must be stored ONCE, not duplicated across scopes."""
    apps = [{"id": "seg1", "name": "App-One"}, {"id": "seg2", "name": "App-Two"}]
    data = {None: {"application": list(apps)}, "0": {"application": list(apps)}}
    res = sync(FakeClient(data, microtenants=[None, "0"]), conn, now=NOW)
    assert res.ok is True
    rows = _rows(conn, "SELECT id, microtenant_id FROM config_object WHERE type='segment'")
    assert {r["id"] for r in rows} == {"seg1", "seg2"}
    assert len(rows) == 2          # once each, not 4
    assert res.object_count == 2
