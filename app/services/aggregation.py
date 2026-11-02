"""Ενεργή λίστα και συγκεντρωτικός πίνακας (SUM κιλών ανά Είδος/Πάχος/Διαστάσεις/Πελάτη)."""
from __future__ import annotations

import json
from collections import OrderedDict
from decimal import Decimal
from typing import Any, Iterable

from app.db.database import Conn, connect, get_version, snapshot
from app.services.normalize import format_kilos, group_key, parse_kilos, product_display, product_label

STATE_LABELS = {"ok": "Ενεργό", "ambiguous": "Αμφίσημο barcode", "unknown": "Άγνωστο barcode"}


def active_items(conn: Conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT a.id, a.barcode, a.barcode_norm, a.state, a.product_record_id, a.duplicate_count, a.created_at,
                  a.import_id, p.eidos, p.paxos, p.diastaseis, p.temaxia, p.kila, p.pelatis, p.vspg, p.topothesia,
                  p.thesi, p.rolo, p.group_key, p.sheet_row, p.extra_json
           FROM active_items a LEFT JOIN product_records p ON p.id = a.product_record_id
           WHERE a.status='active' ORDER BY a.id"""
    ).fetchall()
    msgs = {}
    for r in conn.execute(
        """SELECT active_item_id, message FROM scan_events WHERE id IN (
               SELECT first_scan_event_id FROM active_items WHERE status='active')"""
    ):
        msgs[r["active_item_id"]] = r["message"]
    out = []
    for r in rows:
        d = dict(r)
        d["extra"] = json.loads(d.pop("extra_json") or "{}")
        d["state_label"] = STATE_LABELS.get(d["state"], d["state"])
        d["message"] = msgs.get(d["id"])
        d["display"] = (
            product_display(d["eidos"], d["paxos"], d["diastaseis"], d["kila"], d["pelatis"]) if d["state"] == "ok" else None
        )
        out.append(d)
    return out


def aggregate(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ομαδοποίηση μόνο των 'ok' γραμμών· τα αμφίσημα/άγνωστα δεν προσθέτουν κιλά."""
    groups: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
    for it in items:
        if it.get("state") != "ok":
            continue
        key = it.get("group_key") or group_key(it.get("eidos"), it.get("paxos"), it.get("diastaseis"), it.get("pelatis"))
        g = groups.get(key)
        if g is None:
            g = groups[key] = {
                "group_key": key,
                "product": product_label(it.get("eidos"), it.get("paxos"), it.get("diastaseis")),
                "eidos": it.get("eidos"),
                "paxos": it.get("paxos"),
                "diastaseis": it.get("diastaseis"),
                "pelatis": it.get("pelatis"),
                "total": Decimal(0),
                "count": 0,
                "missing_kilos": 0,
                "barcodes": [],
            }
        kilos = parse_kilos(it.get("kila"))
        if kilos is None:
            g["missing_kilos"] += 1
        else:
            g["total"] += kilos
        g["count"] += 1
        g["barcodes"].append({"barcode": it["barcode"], "kila": it.get("kila"), "id": it["id"]})
    out = []
    for g in groups.values():
        g["total_display"] = format_kilos(g["total"])
        g["total"] = str(g["total"])
        out.append(g)
    return out


def totals(items: list[dict[str, Any]], groups: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [i for i in items if i["state"] == "ok"]
    total = sum((parse_kilos(i.get("kila")) or Decimal(0) for i in ok), Decimal(0))
    return {
        "active_barcodes": len(ok),
        "total_kilos": str(total),
        "total_kilos_display": format_kilos(total),
        "groups": len(groups),
        "duplicate_scans": sum(int(i.get("duplicate_count") or 0) for i in items),
        "ambiguous": sum(1 for i in items if i["state"] == "ambiguous"),
        "unknown": sum(1 for i in items if i["state"] == "unknown"),
        "problem": sum(1 for i in items if i["state"] != "ok"),
        "all_items": len(items),
    }


def dashboard_snapshot_if_changed(last_version: int | None = None) -> dict[str, Any] | None:
    """One cheap version query on idle polls; rebuild full dashboard only on changes.

    The version and the data are read inside a single consistent snapshot.
    OneDrive mirror lag is derived from the same snapshot, avoiding extra DB
    pool connections/queries in every Streamlit fragment tick.
    """
    from app.services import catalog

    with snapshot() as conn:
        version = get_version(conn)
        if last_version is not None and version == last_version:
            return None
        items = active_items(conn)
        imp = catalog.active_import(conn)
        mirror = conn.execute(
            "SELECT value FROM app_settings WHERE key='onedrive.bound'"
        ).fetchone()
    mirror_pending = False
    if imp is not None and mirror:
        try:
            binding = json.loads(mirror["value"])
            mirror_pending = (
                int(binding["import_id"]) == int(imp["id"])
                and int(binding["synced_version"]) < int(imp["working_version"])
            )
        except (TypeError, ValueError, KeyError):
            # Never break the secretary dashboard due to a malformed optional setting.
            mirror_pending = False
    groups = aggregate(items)
    return {
        "version": version, "items": items, "groups": groups,
        "totals": totals(items, groups), "import": imp,
        "onedrive_pending": mirror_pending,
    }


def dashboard_snapshot() -> dict[str, Any]:
    """Full snapshot for existing backend/tests; polling may use the conditional variant."""
    result = dashboard_snapshot_if_changed()
    assert result is not None
    return result


def scanner_snapshot() -> dict[str, Any]:
    """Ελαφριά εκδοχή για την οθόνη αποθήκης."""
    with snapshot() as conn:
        version = get_version(conn)
        rows = conn.execute(
            """SELECT a.id, a.barcode, a.state, a.duplicate_count, a.created_at,
                      p.eidos, p.paxos, p.diastaseis, p.kila, p.pelatis
               FROM active_items a LEFT JOIN product_records p ON p.id = a.product_record_id
               WHERE a.status='active' ORDER BY a.id"""
        ).fetchall()
        has_catalog = conn.execute("SELECT 1 AS x FROM excel_imports WHERE is_active=1").fetchone() is not None
    items = []
    for r in rows:
        items.append({
            "id": r["id"],
            "barcode": r["barcode"],
            "state": r["state"],
            "dup": r["duplicate_count"],
            "display": product_display(r["eidos"], r["paxos"], r["diastaseis"], r["kila"], r["pelatis"]) if r["state"] == "ok" else None,
        })
    return {"version": version, "items": items, "has_catalog": has_catalog}


def candidates_for(item_id: int) -> list[dict[str, Any]]:
    from app.services import catalog

    with connect() as conn:
        it = conn.execute("SELECT * FROM active_items WHERE id=?", (item_id,)).fetchone()
        if it is None:
            return []
        imp = catalog.active_import(conn)
        if imp is None:
            return []
        return [catalog.product_dict(r) for r in catalog.lookup(conn, imp["id"], it["barcode_norm"])]  # type: ignore[misc]
