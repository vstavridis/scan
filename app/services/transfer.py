"""Μεταφορά προϊόντων στο φύλλο "Σβησμένα" με operation journal, version check και recovery.

Η PostgreSQL και το object storage δεν έχουν κοινό atomic transaction. Γι' αυτό:

* Οι εκδόσεις του working Excel στο storage είναι ΑΜΕΤΑΒΛΗΤΕΣ: κάθε μεταφορά ανεβάζει
  ΝΕΟ αρχείο (v<N+1>) και ποτέ δεν αντικαθιστά το προηγούμενο (που μένει ως backup).
* Η αλλαγή "δεσμεύεται" με ΕΝΑ PostgreSQL transaction που κάνει μαζί:
  - compare-and-set του δείκτη `excel_imports.working_version` (N -> N+1),
  - `transfer_operations.status = 'completed'`,
  - αφαίρεση από την ενεργή λίστα, ενημέρωση καταλόγου, ιστορικό.
  Αν αποτύχει οτιδήποτε πριν από αυτό, ο δείκτης μένει στη v<N>: το Excel ΔΕΝ άλλαξε και
  η μεταφορά καταγράφεται ως αποτυχημένη. Δεν υπάρχει ενδιάμεση "μισή" κατάσταση.

Βήματα:
 1. Έλεγχος δικαιωμάτων (backend) και εγκυρότητας.
 2. Lease "excel" στη βάση (μία εγγραφή Excel τη φορά, για όλα τα sessions/διεργασίες).
 3. Journal: INSERT transfer_operations('started') — ο unique index αποκλείει διπλή μεταφορά.
 4. Κατέβασμα της τρέχουσας έκδοσης + έλεγχος sha256 (version check).
 5. Ασφαλής μετασχηματισμός (app.excel.writer — αμετάβλητος Excel engine) + επαλήθευση.
 6. Ανέβασμα v<N+1> και επαλήθευση με ξανακατέβασμα (sha256).
 7. Commit στη βάση (CAS δείκτη + completed). Ανανέωση εικόνας σε όλες τις οθόνες (data_version).

Recovery: μια μεταφορά 'started' που έμεινε (π.χ. restart του Streamlit) χωρίς commit
σημαίνει ότι ο δείκτης δεν μετακινήθηκε· σημειώνεται 'failed' όταν λήξει το lease.
"""
from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import Any

from app.config import get_settings
from app.db.database import Conn, IntegrityError, bump_version, connect, transaction
from app.excel.writer import TransferError, transfer_row
from app.services import settings_service
from app.services.audit import audit
from app.services.catalog import EXCEL_LOCK, working_object
from app.services.errors import Conflict, NotFound, ServiceError
from app.services.locks import lease
from app.storage import StorageError, cached_get, get_store
from app.timeutil import utcnow, utcnow_iso

log = logging.getLogger(__name__)

LEASE_SECONDS = 300


def _load_item(conn: Conn, item_id: int) -> Any:
    it = conn.execute("SELECT * FROM active_items WHERE id=?", (item_id,)).fetchone()
    if it is None:
        raise NotFound("Η εγγραφή δεν βρέθηκε.")
    return it


def _check_item(conn: Conn, item_id: int) -> tuple[dict, dict, dict]:
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
    return dict(it), dict(imp), dict(prod)


def transfer_item(item_id: int, actor: str) -> dict[str, Any]:
    # γρήγορος έλεγχος πριν από το lease (καθαρά μηνύματα χωρίς αναμονή)
    with connect() as conn:
        _check_item(conn, item_id)
    recover_pending()
    with lease(EXCEL_LOCK, seconds=LEASE_SECONDS):
        # --- 3. έλεγχοι (ξανά, μέσα στο lease) + journal "started" -----------
        with transaction() as conn:
            it, imp, prod = _check_item(conn, item_id)
            try:
                op_id = int(conn.execute(
                    """INSERT INTO transfer_operations(active_item_id, product_record_id, import_id, barcode,
                           fingerprint, status, requested_by, started_at, base_version, base_object,
                           excel_before_sha256)
                       VALUES (?, ?, ?, ?, ?, 'started', ?, ?, ?, ?, ?) RETURNING id""",
                    (item_id, prod["id"], imp["id"], prod["barcode"], prod["fingerprint"], actor, utcnow_iso(),
                     imp["working_version"], imp["working_object"], imp["working_sha256"]),
                ).fetchone()["id"])
            except IntegrityError as exc:
                raise Conflict("Η μεταφορά αυτού του προϊόντος βρίσκεται ήδη σε εξέλιξη ή έχει ολοκληρωθεί.") from exc

        cache = get_settings().cache_dir
        out = cache / f"transfer-{uuid.uuid4().hex}.xlsx"
        try:
            # --- 4. τρέχουσα έκδοση + version check (sha256) ------------------
            base = cached_get(imp["working_object"], imp["working_sha256"])
            # --- 5. μετασχηματισμός (ο υπάρχων Excel engine) -------------------
            mode = settings_service.get("transfer_mode")
            result = transfer_row(base, out, imp["source_sheet"], imp["target_sheet"], prod["barcode_norm"],
                                  prod["fingerprint"], mode=mode)
            data = out.read_bytes()
            new_version = int(imp["working_version"]) + 1
            new_obj = working_object(imp["id"], new_version, op_id)
            # --- 6. νέα έκδοση στο storage + επαλήθευση ----------------------
            new_sha = get_store().put_verified(new_obj, data)
            with transaction() as conn:
                conn.execute(
                    "UPDATE transfer_operations SET new_object=?, excel_after_sha256=?, mode=?, source_row=?, "
                    "target_row=?, warning=? WHERE id=?",
                    (new_obj, new_sha, result.mode, result.source_row, result.target_row,
                     "\n".join(result.warnings) or None, op_id),
                )
            # --- 7. commit: CAS δείκτη έκδοσης + ολοκλήρωση ---------------------
            _complete(op_id, actor, imp["id"], int(imp["working_version"]), new_version, new_obj, new_sha)
        except TransferError as exc:
            _fail(op_id, str(exc), actor)
            raise ServiceError(f"Η μεταφορά δεν έγινε: {exc}", code="transfer_failed") from exc
        except StorageError as exc:
            _fail(op_id, f"Αποθήκευση Excel: {exc}", actor)
            raise ServiceError(f"Η μεταφορά δεν έγινε: το Excel δεν αποθηκεύτηκε ({exc}). Δεν άλλαξε τίποτα.",
                               code="transfer_failed") from exc
        except Conflict as exc:
            _fail(op_id, exc.message, actor)
            raise
        except Exception as exc:
            log.exception("Transfer %s failed", op_id)
            _fail(op_id, f"Απρόσμενο σφάλμα: {exc}", actor)
            raise ServiceError(f"Η μεταφορά δεν έγινε λόγω σφάλματος: {exc}", code="transfer_failed") from exc
        finally:
            out.unlink(missing_ok=True)

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
        "new_version": new_version,
        "backup_object": op["base_object"],
    }


def _fail(op_id: int, error: str, actor: str) -> None:
    with transaction() as conn:
        conn.execute("UPDATE transfer_operations SET status='failed', error=?, finished_at=? "
                     "WHERE id=? AND status='started'", (error, utcnow_iso(), op_id))
        audit(conn, actor, "transfer.failed", {"operation_id": op_id, "error": error})
        bump_version(conn)


def _complete(op_id: int, actor: str, import_id: int, base_version: int, new_version: int,
              new_obj: str, new_sha: str) -> None:
    with transaction(lock="scans") as conn:
        moved = conn.execute(
            "UPDATE excel_imports SET working_version=?, working_object=?, working_sha256=? "
            "WHERE id=? AND working_version=? AND is_active=1",
            (new_version, new_obj, new_sha, import_id, base_version),
        ).rowcount
        if moved != 1:
            # Κάποιος άλλος άλλαξε το Excel (ή νέα εισαγωγή): ΔΕΝ δεσμεύουμε τίποτα.
            raise Conflict("Το Excel άλλαξε στο μεταξύ (έλεγχος έκδοσης). Η μεταφορά ακυρώθηκε· δοκιμάστε ξανά.")
        op = conn.execute("SELECT * FROM transfer_operations WHERE id=?", (op_id,)).fetchone()
        if op is None or op["status"] != "started":
            raise Conflict("Η εγγραφή μεταφοράς δεν είναι πλέον σε εξέλιξη.")
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
            "operation_id": op_id, "barcode": op["barcode"], "mode": op["mode"], "source_row": op["source_row"],
            "target_row": op["target_row"], "excel_version": new_version, "backup_object": op["base_object"],
        })
        bump_version(conn)


def recover_pending(max_age_seconds: int = LEASE_SECONDS) -> list[dict[str, Any]]:
    """Μεταφορές 'started' παλαιότερες από το lease: δεν δεσμεύτηκαν ποτέ -> 'failed'.

    Ο δείκτης έκδοσης αλλάζει μόνο μαζί με το 'completed', άρα μια 'started' εγγραφή
    σημαίνει πάντα ότι το working Excel έμεινε στην προηγούμενη έκδοση.
    """
    cutoff = (utcnow() - timedelta(seconds=max_age_seconds)).isoformat(timespec="milliseconds")
    with connect() as conn:
        stale = [dict(r) for r in conn.execute(
            "SELECT id FROM transfer_operations WHERE status='started' AND started_at < ?", (cutoff,)).fetchall()]
    for op in stale:
        _fail(op["id"], "Διακοπή πριν την ολοκλήρωση (π.χ. επανεκκίνηση εφαρμογής). Το Excel δεν άλλαξε.",
              "system-recovery")
        log.warning("Recovered stale transfer %s", op["id"])
    return [{"operation_id": op["id"], "outcome": "rolled_back"} for op in stale]


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
            "SELECT 1 AS x FROM transfer_operations WHERE active_item_id=? AND status='started'", (item_id,)
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
