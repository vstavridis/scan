"""Μεταφορές «Έτοιμα → Σβησμένα», επαναφορές και διορθώσεις, με ουρά, journal και recovery.

Ροή (γρήγορη για τον χρήστη, ασφαλής για το Excel)
--------------------------------------------------
1. **Καταχώριση (ένα κλικ, ένα DB transaction):** η μεταφορά γράφεται ως `queued` στο journal
   `transfer_operations`. Το προϊόν και το barcode φεύγουν αμέσως από την ενεργή λίστα
   (αποθήκη + γραμματεία). Ο unique index αποκλείει διπλή μεταφορά (διπλό κλικ, δύο υπολογιστές).
2. **Συγχρονισμός Excel (παρασκήνιο, `process_pending`):** όλες οι εκκρεμείς ενέργειες
   εφαρμόζονται με τη σειρά τους σε ΕΝΑ πέρασμα του Excel engine και ανεβαίνει ΜΙΑ νέα,
   αμετάβλητη έκδοση στο Supabase Storage (με επαλήθευση sha256). Η αλλαγή δεσμεύεται με ΕΝΑ
   PostgreSQL transaction: compare-and-set του δείκτη έκδοσης + ολοκλήρωση των ενεργειών.
3. **Αποτυχία:** αν μια μετακίνηση δεν μπορεί να γίνει με ασφάλεια (π.χ. η γραμμή άλλαξε στο
   Excel), η ενέργεια σημειώνεται `failed` και το προϊόν/barcode επιστρέφουν στην ενεργή λίστα.
   Αν αποτύχει η αποθήκευση (δίκτυο), οι ενέργειες μένουν στην ουρά και ξαναδοκιμάζονται.
   Το Excel δεν αλλάζει ποτέ "μισό": ο δείκτης έκδοσης μετακινείται μόνο μαζί με το commit.

Επαναφορά λανθασμένης μεταφοράς: νέα ενέργεια `restore` που μετακινεί τη γραμμή από τα
«Σβησμένα» στην τελευταία ελεύθερη σειρά των «Έτοιμων προϊόντων», σε νέα έκδοση Excel, χωρίς
να αγγίζει άλλες μεταφορές. Η αρχική μεταφορά σημειώνεται `reverted_at` (μένει στο ιστορικό).
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

from app.config import get_settings
from app.db.database import Conn, IntegrityError, bump_version, connect, transaction
from app.excel.writer import Move, TransferResult, apply_moves
from app.services import catalog, settings_service
from app.services.audit import audit
from app.services.catalog import EXCEL_LOCK, working_object
from app.services.errors import Conflict, NotFound, ServiceError
from app.services.locks import lease
from app.services.normalize import format_kilos, normalize_barcode, parse_kilos
from app.storage import StorageError, cached_get, get_store
from app.timeutil import utcnow, utcnow_iso

log = logging.getLogger(__name__)

LEASE_SECONDS = 300
MAX_OPS_PER_VERSION = 100
MAX_ATTEMPTS = 5
SYNC_OK_KEY = "sync.last_ok_at"
SYNC_ERR_KEY = "sync.last_error"


# --------------------------------------------------------------------------- βοηθητικά
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


def _set_setting(conn: Conn, key: str, value: Any) -> None:
    conn.execute("INSERT INTO app_settings(key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value, ensure_ascii=False)))


def _reactivate_item(conn: Conn, item_id: int | None) -> bool:
    """Επιστρέφει ένα barcode στην ενεργή λίστα (αν δεν υπάρχει ήδη ενεργό ίδιο barcode)."""
    if not item_id:
        return False
    it = conn.execute("SELECT * FROM active_items WHERE id=?", (item_id,)).fetchone()
    if it is None or it["status"] == "active":
        return False
    other = conn.execute("SELECT 1 AS x FROM active_items WHERE barcode_norm=? AND status='active' AND id<>?",
                         (it["barcode_norm"], item_id)).fetchone()
    if other:
        return False
    conn.execute("UPDATE active_items SET status='active', closed_at=NULL, closed_by=NULL, close_reason=NULL "
                 "WHERE id=?", (item_id,))
    return True


# --------------------------------------------------------------------------- 1. καταχώριση
def queue_transfers(item_ids: list[int], actor: str) -> dict[str, Any]:
    """Καταχωρίζει μία ή πολλές μεταφορές σε ΕΝΑ transaction (γρήγορο). Το Excel ενημερώνεται μετά."""
    ids = list(dict.fromkeys(int(i) for i in item_ids))
    if not ids:
        raise ServiceError("Δεν επιλέχθηκε κανένα barcode.")
    queued: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    total = Decimal(0)
    now = utcnow_iso()
    with transaction(lock="scans") as conn:
        for item_id in ids:
            try:
                it, imp, prod = _check_item(conn, item_id)
            except ServiceError as exc:
                row = conn.execute("SELECT barcode FROM active_items WHERE id=?", (item_id,)).fetchone()
                skipped.append({"item_id": item_id, "barcode": row["barcode"] if row else str(item_id),
                                "reason": exc.message})
                continue
            exists = conn.execute(
                "SELECT 1 AS x FROM transfer_operations WHERE product_record_id=? AND kind='transfer' "
                "AND status IN ('queued','started','completed') AND reverted_at IS NULL", (prod["id"],)).fetchone()
            if exists:
                skipped.append({"item_id": item_id, "barcode": prod["barcode"],
                                "reason": "Η μεταφορά αυτού του προϊόντος βρίσκεται ήδη σε εξέλιξη ή έχει ολοκληρωθεί."})
                continue
            try:
                op_id = int(conn.execute(
                    """INSERT INTO transfer_operations(active_item_id, product_record_id, import_id, barcode,
                           fingerprint, status, kind, requested_by, started_at, queued_at, kila)
                       VALUES (?, ?, ?, ?, ?, 'queued', 'transfer', ?, ?, ?, ?) RETURNING id""",
                    (item_id, prod["id"], imp["id"], prod["barcode"], prod["fingerprint"], actor, now, now,
                     prod["kila"]),
                ).fetchone()["id"])
            except IntegrityError:
                skipped.append({"item_id": item_id, "barcode": prod["barcode"],
                                "reason": "Η μεταφορά αυτού του προϊόντος βρίσκεται ήδη σε εξέλιξη ή έχει ολοκληρωθεί."})
                continue
            conn.execute("UPDATE product_records SET status='transferred', transferred_at=? WHERE id=?",
                         (now, prod["id"]))
            conn.execute("UPDATE active_items SET status='transferred', closed_at=?, closed_by=?, "
                         "close_reason='transfer' WHERE id=?", (now, actor, item_id))
            kila = parse_kilos(prod["kila"])
            if kila is not None:
                total += kila
            queued.append({"operation_id": op_id, "item_id": item_id, "barcode": prod["barcode"],
                           "kila": prod["kila"]})
        if queued:
            audit(conn, actor, "transfer.queued", {
                "operations": [q["operation_id"] for q in queued], "barcodes": [q["barcode"] for q in queued],
                "total_kilos": str(total)})
            bump_version(conn)
    return {"queued": queued, "skipped": skipped, "total_kilos": str(total),
            "total_kilos_display": format_kilos(total)}


def queue_restore(transfer_op_id: int, actor: str) -> dict[str, Any]:
    """Καταχωρίζει επαναφορά μιας ολοκληρωμένης μεταφοράς στα «Έτοιμα προϊόντα»."""
    now = utcnow_iso()
    with transaction(lock="scans") as conn:
        op = conn.execute("SELECT * FROM transfer_operations WHERE id=?", (transfer_op_id,)).fetchone()
        if op is None or op["kind"] != "transfer":
            raise NotFound("Η μεταφορά δεν βρέθηκε.")
        if op["reverted_at"]:
            raise Conflict("Η μεταφορά έχει ήδη επαναφερθεί στα Έτοιμα.")
        if op["status"] in ("queued", "started"):
            raise Conflict("Η μεταφορά δεν έχει ολοκληρωθεί ακόμη στο Excel· περιμένετε λίγα δευτερόλεπτα.")
        if op["status"] != "completed":
            raise Conflict("Μόνο ολοκληρωμένες μεταφορές μπορούν να επαναφερθούν.")
        imp = conn.execute("SELECT * FROM excel_imports WHERE is_active=1").fetchone()
        if imp is None or imp["id"] != op["import_id"]:
            raise Conflict("Η μεταφορά ανήκει σε προηγούμενο Excel (έχει γίνει νέα εισαγωγή)· δεν επαναφέρεται.")
        try:
            restore_id = int(conn.execute(
                """INSERT INTO transfer_operations(active_item_id, product_record_id, import_id, barcode, fingerprint,
                       status, kind, restore_of, requested_by, started_at, queued_at, kila)
                   VALUES (?, ?, ?, ?, ?, 'queued', 'restore', ?, ?, ?, ?, ?) RETURNING id""",
                (op["active_item_id"], op["product_record_id"], op["import_id"], op["barcode"], op["fingerprint"],
                 op["id"], actor, now, now, op["kila"]),
            ).fetchone()["id"])
        except IntegrityError as exc:
            raise Conflict("Η επαναφορά αυτής της μεταφοράς βρίσκεται ήδη σε εξέλιξη.") from exc
        audit(conn, actor, "restore.queued", {"operation_id": restore_id, "restore_of": op["id"],
                                              "barcode": op["barcode"]})
        bump_version(conn)
    return {"operation_id": restore_id, "barcode": op["barcode"]}


# --------------------------------------------------------------------------- 2. συγχρονισμός Excel
def _revert_failed_transfer(conn: Conn, op: dict) -> None:
    """Μια μεταφορά που δεν έγινε στο Excel: το προϊόν/barcode ξαναγίνονται διαθέσιμα."""
    if op["kind"] != "transfer":
        return
    conn.execute("UPDATE product_records SET status='available', transferred_at=NULL WHERE id=? "
                 "AND status='transferred'", (op["product_record_id"],))
    _reactivate_item(conn, op["active_item_id"])


def _fail_ops(ops: list[dict], error: str, actor: str = "excel-sync", errors: dict[int, str] | None = None) -> None:
    if not ops:
        return
    now = utcnow_iso()
    with transaction(lock="scans") as conn:
        for op in ops:
            msg = (errors or {}).get(op["id"], error)
            done = conn.execute("UPDATE transfer_operations SET status='failed', error=?, finished_at=? "
                                "WHERE id=? AND status IN ('queued','started')", (msg[:1000], now, op["id"])).rowcount
            if done:
                _revert_failed_transfer(conn, op)
                audit(conn, actor, f"{op['kind']}.failed", {"operation_id": op["id"], "barcode": op["barcode"],
                                                            "error": msg[:500]})
        bump_version(conn)


def _requeue(ops: list[dict], error: str) -> list[dict]:
    """Προσωρινό σφάλμα (π.χ. δίκτυο): πίσω στην ουρά. Μετά από MAX_ATTEMPTS: επιστρέφονται για αποτυχία."""
    give_up: list[dict] = []
    with transaction() as conn:
        for op in ops:
            attempts = int(op.get("attempts") or 0) + 1
            if attempts >= MAX_ATTEMPTS:
                give_up.append(op)
                continue
            conn.execute("UPDATE transfer_operations SET status='queued', attempts=?, error=? "
                         "WHERE id=? AND status='started'", (attempts, error[:1000], op["id"]))
        _set_setting(conn, SYNC_ERR_KEY, {"at": utcnow_iso(), "error": error[:500]})
        bump_version(conn)
    return give_up


def recover_pending(max_age_seconds: int = LEASE_SECONDS) -> list[dict[str, Any]]:
    """Ενέργειες 'started' που έμειναν (π.χ. restart): ο δείκτης έκδοσης δεν μετακινήθηκε ποτέ χωρίς
    commit, άρα το Excel είναι στην προηγούμενη έκδοση· επιστρέφουν στην ουρά για νέα προσπάθεια."""
    cutoff = (utcnow() - timedelta(seconds=max_age_seconds)).isoformat(timespec="milliseconds")
    with connect() as conn:
        stale = [dict(r) for r in conn.execute(
            "SELECT * FROM transfer_operations WHERE status='started' AND started_at < ?", (cutoff,)).fetchall()]
    if stale:
        give_up = _requeue(stale, "Διακοπή πριν την ολοκλήρωση (π.χ. επανεκκίνηση εφαρμογής). Νέα προσπάθεια.")
        _fail_ops(give_up, "Επαναλαμβανόμενη διακοπή συγχρονισμού Excel.", "system-recovery")
        log.warning("Recovered %d interrupted Excel operations", len(stale))
    return [{"operation_id": op["id"], "outcome": "requeued"} for op in stale]


def pending_count() -> int:
    with connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM transfer_operations WHERE status IN ('queued','started')"
                           ).fetchone()
    return int(row["n"])


def process_pending(actor: str = "excel-sync", wait: float = 20.0) -> dict[str, Any]:
    """Εφαρμόζει τις εκκρεμείς ενέργειες (έως 100) σε ΜΙΑ νέα έκδοση Excel."""
    if pending_count() == 0:
        return {"processed": 0, "completed": [], "failed": []}
    with lease(EXCEL_LOCK, seconds=LEASE_SECONDS, wait=wait,
               busy_message="Ο συγχρονισμός του Excel εκτελείται ήδη. Οι ενέργειες είναι καταχωρημένες."):
        recover_pending()
        return _process_under_lease(actor)


def _process_under_lease(actor: str) -> dict[str, Any]:
    with transaction(lock="scans") as conn:
        imp_row = conn.execute("SELECT * FROM excel_imports WHERE is_active=1").fetchone()
        ops = [dict(r) for r in conn.execute(
            "SELECT * FROM transfer_operations WHERE status='queued' ORDER BY id LIMIT ?",
            (MAX_OPS_PER_VERSION,)).fetchall()]
        if not ops:
            return {"processed": 0, "completed": [], "failed": []}
        imp = dict(imp_row) if imp_row else None
        stale = [o for o in ops if imp is None or o["import_id"] != imp["id"]]
        ops = [o for o in ops if imp is not None and o["import_id"] == imp["id"]]
        now = utcnow_iso()
        for o in ops:
            conn.execute("UPDATE transfer_operations SET status='started', started_at=?, base_version=?, "
                         "base_object=?, excel_before_sha256=? WHERE id=? AND status='queued'",
                         (now, imp["working_version"], imp["working_object"], imp["working_sha256"], o["id"]))
            o["status"] = "started"
    if stale:
        _fail_ops(stale, "Το Excel αντικαταστάθηκε με νέα εισαγωγή πριν ολοκληρωθεί η ενέργεια.", actor)
    if not ops:
        return {"processed": len(stale), "completed": [], "failed": [o["id"] for o in stale]}

    # για επαναφορές: η γραμμή στα «Σβησμένα» από την αρχική μεταφορά
    with connect() as conn:
        for o in ops:
            if o["kind"] == "restore":
                orig = conn.execute("SELECT target_row FROM transfer_operations WHERE id=?",
                                    (o["restore_of"],)).fetchone()
                o["_prefer_row"] = orig["target_row"] if orig else None

    cache = get_settings().cache_dir
    cache.mkdir(parents=True, exist_ok=True)
    out = cache / f"sync-{uuid.uuid4().hex}.xlsx"
    mode = settings_service.get("transfer_mode")
    done: list[tuple[dict, TransferResult]] = []
    bad: list[tuple[dict, Exception]] = []
    try:
        base = cached_get(imp["working_object"], imp["working_sha256"])
        moves = []
        for o in ops:
            if o["kind"] == "transfer":
                moves.append(Move(imp["source_sheet"], imp["target_sheet"], normalize_barcode(o["barcode"]),
                                  o["fingerprint"], mode=mode))
            else:
                # Από τα «Σβησμένα» δεν γίνεται φυσική διαγραφή (κουμπιά macro, shared formulas):
                # η γραμμή καθαρίζεται και το προϊόν μπαίνει στην τελευταία ελεύθερη σειρά των «Έτοιμων».
                moves.append(Move(imp["target_sheet"], imp["source_sheet"], normalize_barcode(o["barcode"]),
                                  o["fingerprint"], mode="clear", prefer_row=o.get("_prefer_row"), pick_last=True))
        results = apply_moves(base, out, moves, stop_on_error=False)
        done = [(o, r) for o, r in zip(ops, results) if isinstance(r, TransferResult)]
        bad = [(o, r) for o, r in zip(ops, results) if not isinstance(r, TransferResult)]
        if bad:
            _fail_ops([o for o, _ in bad], "Δεν έγινε με ασφάλεια στο Excel.", actor,
                      errors={o["id"]: f"Δεν έγινε με ασφάλεια στο Excel: {r}" for o, r in bad})
        if not done:
            return {"processed": len(ops), "completed": [], "failed": [o["id"] for o, _ in bad]}
        data = out.read_bytes()
        new_version = int(imp["working_version"]) + 1
        new_obj = working_object(imp["id"], new_version, done[0][0]["id"])
        if int(done[0][0].get("attempts") or 0):
            # νέα προσπάθεια: ποτέ πάνω σε "ορφανό" αρχείο προηγούμενης αποτυχημένης προσπάθειας
            new_obj = new_obj[:-len(".xlsx")] + f"_r{uuid.uuid4().hex[:8]}.xlsx"
        new_sha = get_store().put_verified(new_obj, data)
        _commit(imp, done, new_version, new_obj, new_sha, actor)
    except StorageError as exc:
        started = [o for o, _ in done] or [o for o in ops if o not in [b for b, _ in bad]]
        _fail_ops(_requeue(started, f"Αποθήκευση Excel: {exc}"), f"Αποθήκευση Excel: {exc}", actor)
        raise ServiceError(f"Το Excel δεν αποθηκεύτηκε ({exc}). Οι ενέργειες παραμένουν στην ουρά.",
                           code="sync_retry") from exc
    except Conflict as exc:
        started = [o for o, _ in done] or [o for o in ops if o not in [b for b, _ in bad]]
        _fail_ops(_requeue(started, exc.message), exc.message, actor)
        raise
    except Exception as exc:
        log.exception("Excel sync failed")
        started = [o for o in ops if o not in [b for b, _ in bad]]
        _fail_ops(started, f"Απρόσμενο σφάλμα: {exc}", actor)
        raise ServiceError(f"Ο συγχρονισμός του Excel απέτυχε: {exc}", code="transfer_failed") from exc
    finally:
        out.unlink(missing_ok=True)
    return {"processed": len(ops), "completed": [o["id"] for o, _ in done], "failed": [o["id"] for o, _ in bad],
            "version": new_version}


def _commit(imp: dict, done: list[tuple[dict, TransferResult]], new_version: int, new_obj: str, new_sha: str,
            actor: str) -> None:
    with transaction(lock="scans") as conn:
        moved = conn.execute(
            "UPDATE excel_imports SET working_version=?, working_object=?, working_sha256=? "
            "WHERE id=? AND working_version=? AND is_active=1",
            (new_version, new_obj, new_sha, imp["id"], imp["working_version"]),
        ).rowcount
        if moved != 1:
            raise Conflict("Το Excel άλλαξε στο μεταξύ (έλεγχος έκδοσης). Νέα προσπάθεια.")
        now = utcnow_iso()
        for seq, (o, res) in enumerate(done, start=1):
            cur = conn.execute("SELECT status FROM transfer_operations WHERE id=?", (o["id"],)).fetchone()
            if cur is None or cur["status"] != "started":
                raise Conflict("Η ενέργεια δεν είναι πλέον σε εξέλιξη.")
            conn.execute(
                "UPDATE transfer_operations SET status='completed', finished_at=?, seq=?, mode=?, source_row=?, "
                "target_row=?, warning=?, new_object=?, excel_after_sha256=?, error=NULL WHERE id=?",
                (now, seq, res.mode, res.source_row, res.target_row, "\n".join(res.warnings) or None,
                 new_obj, new_sha, o["id"]))
            if o["kind"] == "transfer":
                if res.mode == "shift":
                    # οι επόμενες γραμμές των «Έτοιμων» ανέβηκαν κατά μία θέση
                    conn.execute("UPDATE product_records SET sheet_row = sheet_row - 1 WHERE import_id=? "
                                 "AND sheet_row > ? AND status='available'", (o["import_id"], res.source_row))
                audit(conn, o["requested_by"], "transfer.completed", {
                    "operation_id": o["id"], "barcode": o["barcode"], "mode": res.mode,
                    "source_row": res.source_row, "target_row": res.target_row, "excel_version": new_version,
                    "backup_object": imp["working_object"]})
            else:
                conn.execute("UPDATE product_records SET status='available', transferred_at=NULL, sheet_row=? "
                             "WHERE id=?", (res.target_row, o["product_record_id"]))
                conn.execute("UPDATE transfer_operations SET reverted_at=?, reverted_by=? WHERE id=?",
                             (now, o["requested_by"], o["restore_of"]))
                back = _reactivate_item(conn, o["active_item_id"])
                audit(conn, o["requested_by"], "transfer.restored", {
                    "operation_id": o["id"], "restore_of": o["restore_of"], "barcode": o["barcode"],
                    "from_row": res.source_row, "to_row": res.target_row, "excel_version": new_version,
                    "back_to_active_list": back, "backup_object": imp["working_object"]})
        _set_setting(conn, SYNC_OK_KEY, now)
        conn.execute("DELETE FROM app_settings WHERE key=?", (SYNC_ERR_KEY,))
        bump_version(conn)


# --------------------------------------------------------------------------- σύγχρονη εκτέλεση (API/tests)
def _onedrive_mirror(actor: str) -> tuple[bool, str | None]:
    from app.services import onedrive, onedrive_outbox

    imp = catalog.active_import()
    if not imp or not onedrive.bound_import(int(imp["id"])):
        return True, None
    try:
        with lease(EXCEL_LOCK, seconds=LEASE_SECONDS, wait=30):
            mirror = onedrive_outbox.flush_under_lease(actor)
        return bool(mirror["synced"]), None
    except Exception as exc:
        log.warning("OneDrive mirror pending (%s)", type(exc).__name__)
        reason = exc.message if isinstance(exc, ServiceError) else type(exc).__name__
        try:
            with transaction() as conn:
                _set_setting(conn, onedrive.ERROR_KEY, {"at": utcnow_iso(), "error": str(reason)[:500]})
                bump_version(conn)  # το Dashboard δείχνει αμέσως την αιτία
        except Exception:
            pass
        return False, ("Η μεταφορά ολοκληρώθηκε στο Supabase, αλλά το online OneDrive δεν έχει ενημερωθεί ακόμη. "
                       "Οι επόμενες μεταφορές επιτρέπονται και παραμένουν ασφαλείς. Θα ξαναδοκιμαστεί αυτόματα "
                       "(ή Ρυθμίσεις → OneDrive → Επανάληψη συγχρονισμού). ΜΗΝ επαναλάβετε την ίδια μεταφορά.")


def _op(op_id: int) -> dict[str, Any]:
    with connect() as conn:
        return dict(conn.execute("SELECT * FROM transfer_operations WHERE id=?", (op_id,)).fetchone())


def _run_now(op_id: int, actor: str) -> dict[str, Any]:
    """Εκτελεί τον συγχρονισμό τώρα και επιστρέφει την τελική κατάσταση της ενέργειας."""
    for _ in range(3):
        if _op(op_id)["status"] not in ("queued", "started"):
            break
        try:
            process_pending(actor, wait=60)
        except ServiceError as exc:
            log.info("Excel sync attempt did not finish: %s", exc.code)
    op = _op(op_id)
    if op["status"] == "failed":
        raise ServiceError(f"Η μεταφορά δεν έγινε: {op['error']}", code="transfer_failed")
    if op["status"] != "completed":
        raise ServiceError(f"Η μεταφορά δεν έγινε ακόμη στο Excel ({op.get('error') or 'αναμονή'}). "
                           "Παραμένει καταχωρημένη και θα ολοκληρωθεί αυτόματα.", code="transfer_failed")
    return op


def transfer_item(item_id: int, actor: str) -> dict[str, Any]:
    """Σύγχρονη μεταφορά ενός barcode (καταχώριση + συγχρονισμός Excel τώρα)."""
    res = queue_transfers([item_id], actor)
    if not res["queued"]:
        reason = res["skipped"][0]["reason"] if res["skipped"] else "Η μεταφορά δεν καταχωρίστηκε."
        if "ήδη" in reason or "ενεργ" in reason or "ανήκει" in reason:
            raise Conflict(reason)
        raise ServiceError(reason)
    op_id = res["queued"][0]["operation_id"]
    op = _run_now(op_id, actor)
    synced, warning = _onedrive_mirror(actor)
    return {"ok": True, "operation_id": op_id, "barcode": op["barcode"], "mode": op["mode"],
            "source_row": op["source_row"], "target_row": op["target_row"], "warning": op["warning"],
            "new_version": int(op["base_version"]) + 1, "backup_object": op["base_object"],
            "onedrive_synced": synced, "onedrive_warning": warning}


def restore_transfer(transfer_op_id: int, actor: str, wait: bool = True) -> dict[str, Any]:
    res = queue_restore(transfer_op_id, actor)
    if not wait:
        return {"ok": True, "queued": True, **res}
    op = _run_now(res["operation_id"], actor)
    synced, warning = _onedrive_mirror(actor)
    return {"ok": True, "queued": False, "operation_id": op["id"], "barcode": op["barcode"],
            "source_row": op["source_row"], "target_row": op["target_row"],
            "new_version": int(op["base_version"]) + 1, "onedrive_synced": synced, "onedrive_warning": warning}


def sync_now(actor: str) -> dict[str, Any]:
    out = process_pending(actor, wait=60)
    synced, warning = _onedrive_mirror(actor)
    out.update(onedrive_synced=synced, onedrive_warning=warning)
    return out


# --------------------------------------------------------------------------- κατάσταση συγχρονισμού
def sync_status(conn: Conn | None = None) -> dict[str, Any]:
    def q(c: Conn) -> dict[str, Any]:
        pend = c.execute("SELECT COUNT(*) AS n, MIN(queued_at) AS oldest FROM transfer_operations "
                         "WHERE status IN ('queued','started')").fetchone()
        since = (utcnow() - timedelta(hours=24)).isoformat(timespec="milliseconds")
        failed = [dict(r) for r in c.execute(
            "SELECT id, kind, barcode, error, finished_at FROM transfer_operations WHERE status='failed' "
            "AND finished_at >= ? ORDER BY id DESC LIMIT 20", (since,)).fetchall()]
        settings = {r["key"]: r["value"] for r in c.execute(
            "SELECT key, value FROM app_settings WHERE key IN (?, ?, ?)",
            (SYNC_OK_KEY, SYNC_ERR_KEY, "onedrive.last_error")).fetchall()}
        imp = c.execute("SELECT id, working_version FROM excel_imports WHERE is_active=1").fetchone()
        return {
            "pending": int(pend["n"]), "oldest_pending": pend["oldest"], "failed_24h": failed,
            "last_ok_at": json.loads(settings[SYNC_OK_KEY]) if SYNC_OK_KEY in settings else None,
            "last_error": json.loads(settings[SYNC_ERR_KEY]) if SYNC_ERR_KEY in settings else None,
            "excel_version": int(imp["working_version"]) if imp else None,
            "onedrive_last_error": (json.loads(settings["onedrive.last_error"])
                                    if "onedrive.last_error" in settings else None),
        }

    if conn is not None:
        return q(conn)
    with connect() as c:
        return q(c)


def list_transfers(limit: int = 200, barcode: str | None = None) -> list[dict[str, Any]]:
    """Μεταφορές για επαναφορά (πιο πρόσφατες πρώτα), με την κατάσταση της επαναφοράς τους."""
    sql = ("SELECT t.*, i.is_active AS import_active, "
           "(SELECT r.status FROM transfer_operations r WHERE r.restore_of = t.id AND r.kind='restore' "
           " ORDER BY r.id DESC LIMIT 1) AS restore_status, "
           "(SELECT r.error FROM transfer_operations r WHERE r.restore_of = t.id AND r.kind='restore' "
           " ORDER BY r.id DESC LIMIT 1) AS restore_error "
           "FROM transfer_operations t LEFT JOIN excel_imports i ON i.id = t.import_id "
           "WHERE t.kind='transfer' AND t.status IN ('queued','started','completed')")
    params: list[Any] = []
    if barcode:
        sql += " AND (t.barcode LIKE ? OR UPPER(t.barcode) LIKE ?)"
        params += [f"%{barcode.strip()}%", f"%{barcode.strip().upper()}%"]
    sql += " ORDER BY t.id DESC LIMIT ?"
    params.append(limit)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


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


def correct_item(item_id: int, new_barcode: str, actor: str) -> dict[str, Any]:
    """Διόρθωση barcode που χτυπήθηκε λάθος (π.χ. σκισμένη ετικέτα: V11722 → VS11722).

    Το αρχικό σκανάρισμα μένει αναλλοίωτο στο ιστορικό· η εγγραφή της ενεργής λίστας παίρνει το
    σωστό barcode, αναζητείται ξανά στο Excel και εμφανίζεται σωστά σε αποθήκη και γραμματεία.
    """
    barcode = (new_barcode or "").strip()
    norm = normalize_barcode(barcode)
    if not norm or len(norm) > 128:
        raise ServiceError("Δώστε έγκυρο barcode.")
    with transaction(lock="scans") as conn:
        it = _load_item(conn, item_id)
        if it["status"] != "active":
            raise Conflict("Η εγγραφή δεν είναι πλέον ενεργή.")
        old = it["barcode"]
        if norm == it["barcode_norm"]:
            raise ServiceError("Το barcode δεν άλλαξε.")
        imp = conn.execute("SELECT id FROM excel_imports WHERE is_active=1").fetchone()
        original = it["original_barcode"] or old
        other = conn.execute("SELECT * FROM active_items WHERE barcode_norm=? AND status='active' AND id<>?",
                             (norm, item_id)).fetchone()
        if other is not None:
            # Το σωστό barcode είναι ήδη στη λίστα: η εγγραφή συγχωνεύεται ως διπλό σκανάρισμα.
            conn.execute("UPDATE active_items SET duplicate_count = duplicate_count + 1 + ? WHERE id=?",
                         (int(it["duplicate_count"] or 0), other["id"]))
            conn.execute("UPDATE active_items SET status='removed', closed_at=?, closed_by=?, close_reason=?, "
                         "original_barcode=? WHERE id=?",
                         (utcnow_iso(), actor, f"Διόρθωση σε {other['barcode']} (υπήρχε ήδη)", original, item_id))
            result, state, target_id = "duplicate", other["state"], other["id"]
        else:
            matches = catalog.lookup(conn, imp["id"], norm) if imp else []
            if len(matches) == 1:
                state, pid = "ok", matches[0]["id"]
            elif len(matches) > 1:
                state, pid = "ambiguous", None
            else:
                state, pid = "unknown", None
            conn.execute("UPDATE active_items SET barcode=?, barcode_norm=?, state=?, product_record_id=?, "
                         "import_id=?, original_barcode=? WHERE id=?",
                         (barcode, norm, state, pid, imp["id"] if imp else None, original, item_id))
            result, target_id = state, item_id
        audit(conn, actor, "item.correct", {"item_id": item_id, "from": old, "to": barcode, "result": result})
        bump_version(conn)
    messages = {
        "ok": f"Το {old} διορθώθηκε σε {barcode} και βρέθηκε στο Excel.",
        "ambiguous": f"Το {old} διορθώθηκε σε {barcode}, αλλά το {barcode} υπάρχει σε περισσότερες γραμμές του Excel.",
        "unknown": f"Το {old} διορθώθηκε σε {barcode}, αλλά ούτε το {barcode} βρέθηκε στο Excel.",
        "duplicate": f"Το {barcode} ήταν ήδη στη λίστα· το {old} καταχωρίστηκε ως διπλό σκανάρισμα του {barcode}.",
    }
    return {"ok": True, "result": result, "state": state, "item_id": target_id, "message": messages[result]}


def remove_item(item_id: int, actor: str, reason: str = "") -> dict[str, Any]:
    with transaction() as conn:
        it = _load_item(conn, item_id)
        if it["status"] != "active":
            raise Conflict("Η εγγραφή δεν είναι πλέον ενεργή.")
        busy = conn.execute(
            "SELECT 1 AS x FROM transfer_operations WHERE active_item_id=? AND status IN ('queued','started')",
            (item_id,)).fetchone()
        if busy:
            raise Conflict("Η εγγραφή μεταφέρεται αυτή τη στιγμή.")
        conn.execute(
            "UPDATE active_items SET status='removed', closed_at=?, closed_by=?, close_reason=? WHERE id=?",
            (utcnow_iso(), actor, reason or "Αφαίρεση από τη λίστα", item_id),
        )
        audit(conn, actor, "item.remove", {"item_id": item_id, "barcode": it["barcode"], "reason": reason})
        bump_version(conn)
    return {"ok": True}
