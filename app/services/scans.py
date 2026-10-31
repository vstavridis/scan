"""Επεξεργασία scan events (idempotent, transactional).

Κάθε batch επεξεργάζεται σε ΕΝΑ transaction με τη σειρά που στάλθηκε. Το API απαντά
(acknowledgement) μόνο αφού γίνει COMMIT. Η επαναποστολή ίδιου event_id επιστρέφει
το αποθηκευμένο αποτέλεσμα χωρίς νέα καταχώρηση.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from typing import Any

from app.db.database import bump_version, connect, get_version, transaction
from app.services import catalog
from app.services.normalize import normalize_barcode, product_display
from app.timeutil import utcnow_iso

log = logging.getLogger(__name__)

MAX_BARCODE_LEN = 128

MESSAGES = {
    "ok": "Καταχωρήθηκε",
    "duplicate": "Το barcode έχει ήδη σκαναριστεί",
    "unknown": "Το barcode δεν βρέθηκε στο Excel",
    "unknown_transferred": "Το barcode έχει ήδη μεταφερθεί στα Σβησμένα",
    "ambiguous": "Αμφίσημο barcode — υπάρχει σε περισσότερες γραμμές του Excel",
    "no_catalog": "Δεν υπάρχει ενεργό Excel — η σάρωση ΔΕΝ είναι έγκυρη",
    "invalid": "Μη έγκυρο barcode",
}


@dataclass
class ScanIn:
    event_id: str
    barcode: str
    client_ts: str | None = None
    seq: int | None = None


def _display_for(conn: sqlite3.Connection, product_record_id: int | None) -> tuple[str | None, str | None, str | None]:
    if not product_record_id:
        return None, None, None
    p = conn.execute("SELECT * FROM product_records WHERE id=?", (product_record_id,)).fetchone()
    if p is None:
        return None, None, None
    return (product_display(p["eidos"], p["paxos"], p["diastaseis"], p["kila"], p["pelatis"]), p["kila"],
            catalog.snapshot_from_record(p))


def _result_from_row(row: sqlite3.Row) -> dict[str, Any]:
    snap = json.loads(row["product_snapshot"]) if row["product_snapshot"] else None
    display = None
    if snap:
        display = product_display(snap.get("eidos"), snap.get("paxos"), snap.get("diastaseis"), snap.get("kila"),
                                  snap.get("pelatis"))
    return {
        "event_id": row["event_id"],
        "server_id": row["id"],
        "barcode": row["barcode"],
        "result": row["result"],
        "message": row["message"],
        "display": display,
        "kila": row["kila"],
        "active_item_id": row["active_item_id"],
        "received_at": row["received_at"],
    }


def _process_one(conn: sqlite3.Connection, ev: ScanIn, device_id: str | None, imp: dict | None) -> dict[str, Any]:
    existing = conn.execute("SELECT * FROM scan_events WHERE event_id=?", (ev.event_id,)).fetchone()
    if existing is not None:
        out = _result_from_row(existing)
        out["replayed"] = True
        return out

    barcode = (ev.barcode or "").strip()
    norm = normalize_barcode(barcode)
    now = utcnow_iso()
    result: str
    message: str
    product_id: int | None = None
    active_id: int | None = None
    import_id = imp["id"] if imp else None

    if not norm or len(norm) > MAX_BARCODE_LEN:
        result, message = "invalid", MESSAGES["invalid"]
    elif imp is None:
        result, message = "no_catalog", MESSAGES["no_catalog"]
    else:
        active = conn.execute(
            "SELECT id, product_record_id, state FROM active_items WHERE barcode_norm=? AND status='active'", (norm,)
        ).fetchone()
        if active is not None:
            result, message = "duplicate", MESSAGES["duplicate"]
            active_id = active["id"]
            product_id = active["product_record_id"]
            conn.execute("UPDATE active_items SET duplicate_count = duplicate_count + 1 WHERE id=?", (active_id,))
        else:
            matches = catalog.lookup(conn, imp["id"], norm)
            if len(matches) == 1:
                result, message, state = "ok", MESSAGES["ok"], "ok"
                product_id = matches[0]["id"]
            elif len(matches) > 1:
                result, message, state = "ambiguous", MESSAGES["ambiguous"], "ambiguous"
            else:
                transferred = catalog.lookup(conn, imp["id"], norm, include_transferred=True)
                result, state = "unknown", "unknown"
                message = MESSAGES["unknown_transferred"] if transferred else MESSAGES["unknown"]
            cur = conn.execute(
                """INSERT INTO active_items(barcode, barcode_norm, state, product_record_id, import_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (barcode, norm, state, product_id, import_id, now),
            )
            active_id = int(cur.lastrowid)

    display, kila, snapshot = _display_for(conn, product_id)
    cur = conn.execute(
        """INSERT INTO scan_events(event_id, barcode, barcode_norm, client_ts, received_at, device_id, client_seq,
               result, message, product_record_id, product_snapshot, kila, import_id, active_item_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (ev.event_id, barcode[:256], norm[:256], ev.client_ts, now, device_id, ev.seq, result, message,
         product_id, snapshot, kila if result == "ok" else None, import_id, active_id),
    )
    scan_id = int(cur.lastrowid)
    if active_id is not None and result != "duplicate":
        conn.execute("UPDATE active_items SET first_scan_event_id=? WHERE id=?", (scan_id, active_id))
    return {
        "event_id": ev.event_id,
        "server_id": scan_id,
        "barcode": barcode,
        "result": result,
        "message": message,
        "display": display,
        "kila": kila,
        "active_item_id": active_id,
        "received_at": now,
        "replayed": False,
    }


def process_batch(events: list[ScanIn], device_id: str | None = None) -> dict[str, Any]:
    """Επεξεργάζεται τα events με τη σειρά τους σε ένα transaction. Επιστρέφει μετά το COMMIT."""
    if not events:
        with connect() as conn:
            return {"results": [], "version": get_version(conn)}
    with transaction() as conn:
        imp = catalog.active_import(conn)
        results = [_process_one(conn, ev, device_id, imp) for ev in events]
        if any(not r.get("replayed") for r in results):
            version = bump_version(conn)
        else:
            version = get_version(conn)
    return {"results": results, "version": version}
