"""Μεταφορά προϊόντων στο φύλλο "Σβησμένα" με journal και recovery.

Ροή (κάθε βήμα είναι durable):
 1. INSERT transfer_operations(status='started')  — ο unique index αποκλείει διπλή μεταφορά.
 2. Backup του working Excel.
 3. Δημιουργία νέου workbook σε προσωρινό αρχείο + επαλήθευση.
 4. Καταγραφή before/after sha256 στο journal (COMMIT).
 5. Ατομική αντικατάσταση του working Excel (os.replace).
 6. Ενημέρωση βάσης (COMMIT): ολοκληρωμένη μεταφορά, αφαίρεση από ενεργή λίστα, ιστορικό.

Αν η διεργασία σταματήσει μεταξύ 5 και 6, το recover_pending() στην εκκίνηση συγκρίνει το
sha256 του working αρχείου με το journal και ολοκληρώνει ή ακυρώνει τη μεταφορά.
Επιτυχία ΔΕΝ επιστρέφεται ποτέ αν δεν γράφτηκε το Excel.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

from app.db.database import bump_version, connect, transaction
from app.excel.reader import file_sha256
from app.excel.storage import ExcelLockedError, excel_write_lock, get_store
from app.excel.writer import TransferError, transfer_row
from app.services import settings_service
from app.services.audit import audit
from app.services.errors import Conflict, Locked, NotFound, ServiceError
from app.timeutil import utcnow_iso

log = logging.getLogger(__name__)


def _load_item(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row:
    it = conn.execute("SELECT * FROM active_items WHERE id=?", (item_id,)).fetchone()
    if it is None:
        raise NotFound("Η εγγραφή δεν βρέθηκε.")
    return it


def transfer_item(item_id: int, actor: str) -> dict[str, Any]:
    # --- 1. έλεγχοι + journal "started" ------------------------------------
    with transaction() as conn:
        it = _load_item(conn, item_id)
        if it["status"] == "transferred":
            raise Conflict("Το barcode έχει ήδη μεταφερθεί.")
        if it["status"] != "active":
            raise Conflict("Η εγγραφή δεν είναι πλέον ενεργή.")
        if it["state"] == "ambiguous":
            raise ServiceError("Αμφίσημο barcode: επιλέξτε πρώτα τη σωστή γραμμή του Excel.")
        if it["state"] != "ok" or not it["product_record_id"]:
            raise ServiceError("Το barcode δεν αντιστοιχεί σε προϊόν του Excel· δεν μπορεί να μεταφερθεί.")
        imp = conn.execute("SELECT * FROM excel_imports WHERE is_active=1").fetchone()
        prod = conn.execute("SELECT * FROM product_records WHERE id=?", (it["product_record_id"],)).fetchone()
        if imp is None or prod is None or prod["import_id"] != imp["id"]:
            raise Conflict("Το προϊόν δεν ανήκει στο ενεργό Excel. Ανανεώστε τη σελίδα.")
        if prod["status"] != "available":
            raise Conflict("Το προϊόν έχει ήδη μεταφερθεί.")
        try:
            cur = conn.execute(
                """INSERT INTO transfer_operations(active_item_id, product_record_id, import_id, barcode, fingerprint,
                       status, requested_by, started_at) VALUES (?, ?, ?, ?, ?, 'started', ?, ?)""",
                (item_id, prod["id"], imp["id"], prod["barcode"], prod["fingerprint"], actor, utcnow_iso()),
            )
        except sqlite3.IntegrityError as exc:
            raise Conflict("Η μεταφορά αυτού του προϊόντος βρίσκεται ήδη σε εξέλιξη ή έχει ολοκληρωθεί.") from exc
        op_id = int(cur.lastrowid)
        imp = dict(imp)
        prod = dict(prod)

    store = get_store()
    working = Path(imp["working_path"])
    tmp = store.temp_path_for(working)
    try:
        with excel_write_lock:
            # --- 2-3. backup + νέο workbook --------------------------------
            store.ensure_writable(working)
            before = file_sha256(working)
            backup = store.backup(working, f"before-transfer-{prod['barcode']}")
            mode = settings_service.get("transfer_mode")
            result = transfer_row(working, tmp, imp["source_sheet"], imp["target_sheet"], prod["barcode_norm"],
                                  prod["fingerprint"], mode=mode)
            after = file_sha256(tmp)
            # --- 4. journal: γνωρίζουμε πριν/μετά ---------------------------
            with transaction() as conn:
                conn.execute(
                    """UPDATE transfer_operations SET excel_before_sha256=?, excel_after_sha256=?, backup_path=?,
                           mode=?, source_row=?, target_row=?, warning=? WHERE id=?""",
                    (before, after, str(backup), result.mode, result.source_row, result.target_row,
                     "\n".join(result.warnings) or None, op_id),
                )
            # --- 5. ατομική αντικατάσταση -----------------------------------
            store.ensure_writable(working)
            store.replace_atomic(tmp, working)
    except ExcelLockedError as exc:
        _fail(op_id, str(exc), actor)
        raise Locked(str(exc)) from exc
    except TransferError as exc:
        _fail(op_id, str(exc), actor)
        raise ServiceError(f"Η μεταφορά δεν έγινε: {exc}", code="transfer_failed") from exc
    except Exception as exc:
        log.exception("Transfer %s failed", op_id)
        _fail(op_id, f"Απρόσμενο σφάλμα: {exc}", actor)
        raise ServiceError(f"Η μεταφορά δεν έγινε λόγω σφάλματος εγγραφής Excel: {exc}", code="transfer_failed") from exc
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass

    # --- 6. ενημέρωση βάσης ------------------------------------------------
    _complete(op_id, actor)
    with connect() as conn:
        op = dict(conn.execute("SELECT * FROM transfer_operations WHERE id=?", (op_id,)).fetchone())
    return {
        "ok": True,
        "operation_id": op_id,
        "barcode": prod["barcode"],
        "mode": op["mode"],
        "source_row": op["source_row"],
        "target_row": op["target_row"],
        "warning": op["warning"],
        "backup_path": op["backup_path"],
    }


def _fail(op_id: int, error: str, actor: str) -> None:
    with transaction() as conn:
        conn.execute("UPDATE transfer_operations SET status='failed', error=?, finished_at=? WHERE id=?",
                     (error, utcnow_iso(), op_id))
        audit(conn, actor, "transfer.failed", {"operation_id": op_id, "error": error})
        bump_version(conn)


def _complete(op_id: int, actor: str) -> None:
    with transaction() as conn:
        op = conn.execute("SELECT * FROM transfer_operations WHERE id=?", (op_id,)).fetchone()
        if op is None or op["status"] != "started":
            return
        now = utcnow_iso()
        conn.execute("UPDATE transfer_operations SET status='completed', finished_at=? WHERE id=?", (now, op_id))
        conn.execute("UPDATE product_records SET status='transferred', transferred_at=? WHERE id=?",
                     (now, op["product_record_id"]))
        if op["mode"] == "shift" and op["source_row"]:
            # οι επόμενες γραμμές ανέβηκαν κατά μία θέση
            conn.execute(
                "UPDATE product_records SET sheet_row = sheet_row - 1 WHERE import_id=? AND sheet_row > ? AND status='available'",
                (op["import_id"], op["source_row"]),
            )
        if op["active_item_id"]:
            conn.execute(
                """UPDATE active_items SET status='transferred', closed_at=?, closed_by=?, close_reason='transfer'
                   WHERE id=? AND status='active'""",
                (now, op["requested_by"], op["active_item_id"]),
            )
        audit(conn, actor, "transfer.completed", {
            "operation_id": op_id, "barcode": op["barcode"], "mode": op["mode"],
            "source_row": op["source_row"], "target_row": op["target_row"],
        })
        bump_version(conn)


def recover_pending() -> list[dict[str, Any]]:
    """Ολοκληρώνει/ακυρώνει μεταφορές που διακόπηκαν (κλήση στην εκκίνηση)."""
    report = []
    with connect() as conn:
        ops = [dict(r) for r in conn.execute(
            """SELECT t.*, i.working_path FROM transfer_operations t JOIN excel_imports i ON i.id = t.import_id
               WHERE t.status='started'"""
        )]
    for op in ops:
        working = Path(op["working_path"])
        current = file_sha256(working) if working.is_file() else None
        if op["excel_after_sha256"] and current == op["excel_after_sha256"]:
            _complete(op["id"], "system-recovery")
            outcome = "completed"
        elif current is not None and current == op["excel_before_sha256"]:
            _fail(op["id"], "Διακοπή πριν την εγγραφή του Excel (αυτόματη ακύρωση στην επανεκκίνηση).", "system-recovery")
            outcome = "rolled_back"
        elif op["excel_before_sha256"] is None:
            _fail(op["id"], "Διακοπή πριν ξεκινήσει η εγγραφή του Excel.", "system-recovery")
            outcome = "rolled_back"
        else:
            _fail(op["id"], "Το Excel δεν ταιριάζει ούτε με την πριν ούτε με τη μετά κατάσταση· απαιτείται έλεγχος.",
                  "system-recovery")
            outcome = "needs_review"
        log.warning("Recovered transfer %s: %s", op["id"], outcome)
        report.append({"operation_id": op["id"], "outcome": outcome})
    return report


# --------------------------------------------------------------------------- άλλες ενέργειες
def resolve_ambiguous(item_id: int, product_record_id: int, actor: str) -> dict[str, Any]:
    with transaction() as conn:
        it = _load_item(conn, item_id)
        if it["status"] != "active" or it["state"] != "ambiguous":
            raise Conflict("Η εγγραφή δεν είναι αμφίσημη ή δεν είναι πλέον ενεργή.")
        prod = conn.execute(
            "SELECT * FROM product_records WHERE id=? AND barcode_norm=? AND status='available'",
            (product_record_id, it["barcode_norm"]),
        ).fetchone()
        imp = conn.execute("SELECT id FROM excel_imports WHERE is_active=1").fetchone()
        if prod is None or imp is None or prod["import_id"] != imp["id"]:
            raise ServiceError("Η επιλεγμένη γραμμή δεν αντιστοιχεί σε αυτό το barcode.")
        conn.execute("UPDATE active_items SET state='ok', product_record_id=? WHERE id=?", (prod["id"], item_id))
        audit(conn, actor, "item.resolve", {"item_id": item_id, "barcode": it["barcode"], "product_record_id": prod["id"],
                                            "sheet_row": prod["sheet_row"], "kila": prod["kila"]})
        bump_version(conn)
    return {"ok": True}


def remove_item(item_id: int, actor: str, reason: str = "") -> dict[str, Any]:
    with transaction() as conn:
        it = _load_item(conn, item_id)
        if it["status"] != "active":
            raise Conflict("Η εγγραφή δεν είναι πλέον ενεργή.")
        busy = conn.execute(
            "SELECT 1 FROM transfer_operations WHERE active_item_id=? AND status='started'", (item_id,)
        ).fetchone()
        if busy:
            raise Conflict("Η εγγραφή μεταφέρεται αυτή τη στιγμή.")
        conn.execute(
            "UPDATE active_items SET status='removed', closed_at=?, closed_by=?, close_reason=? WHERE id=?",
            (utcnow_iso(), actor, reason or "Αφαίρεση από τη λίστα", item_id),
        )
        audit(conn, actor, "item.remove", {"item_id": item_id, "barcode": it["barcode"], "reason": reason})
        bump_version(conn)
    return {"ok": True}
