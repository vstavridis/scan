"""Μόνιμο ιστορικό σκαναρισμάτων, φίλτρα και εξαγωγή."""
from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from app.db.database import connect
from app.services.normalize import format_kilos, product_display
from app.timeutil import fmt_local, local_tz

RESULT_LABELS = {
    "ok": "Επιτυχές",
    "duplicate": "Διπλό",
    "unknown": "Άγνωστο",
    "ambiguous": "Αμφίσημο",
    "no_catalog": "Χωρίς ενεργό Excel",
    "invalid": "Μη έγκυρο",
}
TRANSFER_LABELS = {
    None: "—",
    "active": "Στην ενεργή λίστα",
    "transferred": "Μεταφέρθηκε",
    "removed": "Αφαιρέθηκε από λίστα",
}


def _local_day_bounds(d: date, end: bool = False) -> str:
    dt = datetime.combine(d + timedelta(days=1) if end else d, time.min, tzinfo=local_tz())
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _transfer_label(r: Any) -> str:
    item_status, op_status = r["item_status"], r["transfer_op_status"]
    if r["transfer_reverted_at"]:
        return "Επαναφέρθηκε στα Έτοιμα" if item_status != "transferred" else "Μεταφέρθηκε"
    if item_status == "transferred" and op_status in ("queued", "started"):
        return "Μεταφέρθηκε (ενημέρωση Excel σε εξέλιξη)"
    if r["result"] == "duplicate":
        return "Μεταφέρθηκε" if item_status == "transferred" else "—"
    return TRANSFER_LABELS.get(item_status, item_status)


def query(
    date_from: date | None = None,
    date_to: date | None = None,
    barcode: str | None = None,
    customer: str | None = None,
    result: str | None = None,
    transfer_status: str | None = None,
    limit: int = 5000,
) -> list[dict[str, Any]]:
    where = ["1=1"]
    params: list[Any] = []
    if date_from:
        where.append("s.received_at >= ?")
        params.append(_local_day_bounds(date_from))
    if date_to:
        where.append("s.received_at < ?")
        params.append(_local_day_bounds(date_to, end=True))
    if barcode:
        where.append("(s.barcode LIKE ? OR s.barcode_norm LIKE ?)")
        params += [f"%{barcode.strip()}%", f"%{barcode.strip().upper()}%"]
    if customer:
        where.append("s.pelatis LIKE ?")
        params.append(f"%{customer.strip()}%")
    if result:
        where.append("s.result = ?")
        params.append(result)
    if transfer_status:
        where.append("a.status = ?")
        params.append(transfer_status)
    sql = f"""
        SELECT s.*, a.status AS item_status, a.closed_at AS item_closed_at, a.closed_by AS item_closed_by,
               a.close_reason, a.barcode AS item_barcode, a.barcode_norm AS item_barcode_norm,
               a.original_barcode AS item_original_barcode,
               t.id AS transfer_op_id, t.status AS transfer_op_status, t.reverted_at AS transfer_reverted_at,
               t.finished_at AS transfer_at, t.queued_at AS transfer_queued_at, t.requested_by AS transfer_by,
               t.mode AS transfer_mode, t.target_row AS transfer_target_row, i.original_filename AS import_filename
        FROM scan_events s
        LEFT JOIN active_items a ON a.id = s.active_item_id
        LEFT JOIN transfer_operations t ON t.id = (
            SELECT MAX(x.id) FROM transfer_operations x WHERE x.active_item_id = s.active_item_id
              AND x.kind = 'transfer' AND x.status IN ('queued', 'started', 'completed'))
        LEFT JOIN excel_imports i ON i.id = s.import_id
        WHERE {' AND '.join(where)}
        ORDER BY s.id DESC LIMIT ?"""
    params.append(limit)
    out = []
    with connect() as conn:
        for r in conn.execute(sql, params):
            snap = json.loads(r["product_snapshot"]) if r["product_snapshot"] else {}
            out.append({
                "id": r["id"],
                "event_id": r["event_id"],
                "received_at": r["received_at"],
                "received_local": fmt_local(r["received_at"]),
                "client_ts_local": fmt_local(r["client_ts"]) if r["client_ts"] else "",
                "barcode": r["barcode"],
                "result": r["result"],
                "result_label": RESULT_LABELS.get(r["result"], r["result"]),
                "message": r["message"],
                "product": product_display(snap.get("eidos"), snap.get("paxos"), snap.get("diastaseis"),
                                           snap.get("kila"), snap.get("pelatis")) if snap else "",
                "eidos": snap.get("eidos", ""),
                "paxos": snap.get("paxos", ""),
                "diastaseis": snap.get("diastaseis", ""),
                "pelatis": snap.get("pelatis", ""),
                "kila": format_kilos(snap.get("kila")) if snap else "",
                "transfer_status": _transfer_label(r),
                "correction": (f"{r['barcode']} → {r['item_barcode']}"
                               if r["item_original_barcode"] and r["item_barcode_norm"]
                               and r["item_barcode_norm"] != r["barcode_norm"] else ""),
                "transfer_op_id": r["transfer_op_id"],
                "transfer_at_local": fmt_local(r["transfer_at"] or r["transfer_queued_at"])
                if (r["transfer_at"] or r["transfer_queued_at"]) else "",
                "transfer_by": r["transfer_by"] or "",
                "transfer_mode": r["transfer_mode"] or "",
                "import_id": r["import_id"],
                "import_filename": r["import_filename"] or "",
                "device_id": r["device_id"] or "",
            })
    return out


EXPORT_COLUMNS = [
    ("received_local", "Ημερομηνία/ώρα"),
    ("barcode", "Barcode"),
    ("result_label", "Αποτέλεσμα"),
    ("message", "Μήνυμα"),
    ("eidos", "Είδος"),
    ("paxos", "Πάχος"),
    ("diastaseis", "Διαστάσεις"),
    ("kila", "Κιλά"),
    ("pelatis", "Πελάτης"),
    ("correction", "Διόρθωση barcode"),
    ("transfer_status", "Κατάσταση μεταφοράς"),
    ("transfer_at_local", "Ημ/νία μεταφοράς"),
    ("transfer_by", "Χρήστης μεταφοράς"),
    ("import_id", "Έκδοση Excel (import)"),
    ("event_id", "Scan event ID"),
    ("device_id", "Συσκευή"),
]


def to_csv(rows: list[dict[str, Any]]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow([h for _k, h in EXPORT_COLUMNS])
    for r in rows:
        w.writerow([r.get(k, "") for k, _h in EXPORT_COLUMNS])
    return ("﻿" + buf.getvalue()).encode("utf-8")  # BOM για σωστά ελληνικά στο Excel


def to_xlsx(rows: list[dict[str, Any]]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = "Ιστορικό"
    ws.append([h for _k, h in EXPORT_COLUMNS])
    for c in ws[1]:
        c.font = Font(bold=True)
    for r in rows:
        values = []
        for k, _h in EXPORT_COLUMNS:
            v = r.get(k, "")
            if k == "kila" and v:
                try:
                    v = float(v)
                except ValueError:
                    pass
            values.append(v)
        ws.append(values)
    ws.freeze_panes = "A2"
    for col, width in zip("ABCDEFGHIJKLMNO", (20, 14, 12, 36, 20, 8, 16, 8, 20, 20, 20, 16, 10, 38, 20)):
        ws.column_dimensions[col].width = width
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def audit_log(limit: int = 500) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [{**dict(r), "ts_local": fmt_local(r["ts"])} for r in rows]
