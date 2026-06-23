"""Pull + normalize ZPA config into config_object/config_edge; build NameIndex.

All writes happen in ONE transaction: on any client failure we roll back so the
last-good config is left intact (no partial sync). RETIRED = an object present in a prior sync, absent now, that had prior usage.
config_object rows are never deleted, so retired_ids reflects "currently
retired" (it re-reports the same object each sync, not only the first time).
"""
import json
from dataclasses import dataclass

# resource path -> object type.
RESOURCE_TYPES = [
    ("application", "segment"),
    ("segmentGroup", "segment_group"),
    ("policySet/rules/policyType/ACCESS_POLICY", "rule"),
    ("serverGroup", "server_group"),
    ("appConnectorGroup", "connector_group"),
    ("connector", "connector"),
    ("server", "server"),
]

_UNSET = object()


class NameIndex:
    def __init__(self):
        # (type, microtenant_id_or_"") -> { name -> id }
        self._by_scope = {}
        # (type, name) -> set(ids) across all microtenants (for fallback)
        self._global = {}

    def add(self, object_type, name, obj_id, microtenant_id):
        mt = microtenant_id or ""
        self._by_scope.setdefault((object_type, mt), {})[name] = obj_id
        self._global.setdefault((object_type, name), set()).add(obj_id)

    def resolve(self, object_type, name, microtenant_id=_UNSET):
        # Unspecified microtenant (the hot-path call): resolve globally, but only
        # when the name maps to exactly one id across all tenants (else ambiguous).
        if microtenant_id is _UNSET:
            ids = self._global.get((object_type, name))
            if ids and len(ids) == 1:
                return next(iter(ids))
            return None
        # Explicit microtenant scope (None == the default tenant, stored as "").
        mt = microtenant_id or ""
        scoped = self._by_scope.get((object_type, mt))
        if scoped is not None and name in scoped:
            return scoped[name]
        return None


@dataclass
class SyncResult:
    ok: bool
    name_index: "NameIndex"
    object_count: int
    retired_ids: list  # list[tuple[str, str]] (object_type, object_id)
    error: str | None = None


def _edges_for(object_type, row):
    """Yield (parent_type, parent_id, child_type, child_id) from a config row."""
    if object_type == "segment":
        sg = row.get("segmentGroupId")
        if sg:
            yield ("segment_group", sg, "segment", row["id"])
    elif object_type == "server_group":
        for srv in row.get("servers") or []:
            sid = srv.get("id") if isinstance(srv, dict) else srv
            if sid:
                yield ("server_group", row["id"], "server", sid)


def _attrs_json(row):
    skip = {"id", "name", "enabled", "microtenantId"}
    extras = {k: v for k, v in row.items() if k not in skip}
    return json.dumps(extras, separators=(",", ":"), default=str)


def sync(client, conn, *, now):
    now_iso = now.isoformat()
    try:
        microtenants = client.list_microtenants()
        pulled = []  # (object_type, microtenant_id, row)
        edges = set()
        for mt in microtenants:
            for resource, object_type in RESOURCE_TYPES:
                for row in client.paged_get(resource, microtenant_id=mt):
                    if not row.get("id"):
                        continue
                    pulled.append((object_type, mt, row))
                    for e in _edges_for(object_type, row):
                        edges.add(e)
    except Exception as exc:  # client failure during pull (before any write)
        conn.rollback()  # no-op here (no writes issued yet); kept for safety
        return SyncResult(ok=False, name_index=load_name_index(conn),
                          object_count=0, retired_ids=[], error=str(exc))

    cur = conn.cursor()
    try:
        # prior (type,id) set, and which ids have usage, for RETIRED detection
        prior_ids = {(r["type"], r["id"])
                     for r in cur.execute(
                         "SELECT type, id FROM config_object").fetchall()}
        used_ids = {(r["object_type"], r["object_id"])
                    for r in cur.execute(
                        "SELECT object_type, object_id FROM usage_counter").fetchall()}

        seen_now = set()
        name_index = NameIndex()
        for object_type, mt, row in pulled:
            obj_id = row["id"]
            key = (object_type, obj_id)
            if key in seen_now:
                # A ZPA object id is globally unique, so once we've stored it we
                # never store it again under a second scope. This collapses the
                # default-microtenant overlap (e.g. id "0" returning the same
                # objects as the no-filter pull) that otherwise doubles every row.
                continue
            seen_now.add(key)
            name = row.get("name") or ""
            enabled = 1 if row.get("enabled", True) else 0
            attrs = _attrs_json(row)
            cur.execute(
                "INSERT INTO config_object "
                "(id, microtenant_id, type, name, enabled, "
                " first_config_seen, last_config_seen, attrs_json) "
                "VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id, microtenant_id) DO UPDATE SET "
                " type=excluded.type, name=excluded.name, enabled=excluded.enabled, "
                " last_config_seen=excluded.last_config_seen, "
                " attrs_json=excluded.attrs_json",
                (obj_id, mt or "", object_type, name, enabled,
                 now_iso, now_iso, attrs),
            )
            name_index.add(object_type, name, obj_id, mt)

        for (pt, pid, ct, cid) in edges:
            cur.execute(
                "INSERT OR IGNORE INTO config_edge "
                "(parent_type, parent_id, child_type, child_id, source) "
                "VALUES (?,?,?,?, 'config')",
                (pt, pid, ct, cid),
            )

        retired = sorted(
            (t, i) for (t, i) in (prior_ids - seen_now) if (t, i) in used_ids)

        cur.execute(
            "INSERT INTO meta(key, value) VALUES('last_config_sync', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (now_iso,))
        conn.commit()
        return SyncResult(ok=True, name_index=name_index,
                          object_count=len(seen_now), retired_ids=retired)
    except Exception as exc:
        conn.rollback()
        return SyncResult(ok=False, name_index=load_name_index(conn),
                          object_count=0, retired_ids=[], error=str(exc))


def load_name_index(conn):
    idx = NameIndex()
    for r in conn.execute(
            "SELECT id, microtenant_id, type, name FROM config_object").fetchall():
        mt = r["microtenant_id"] or None
        idx.add(r["type"], r["name"], r["id"], mt)
    return idx
