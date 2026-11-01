"""Explicit, safe repair of ONE pending OneDrive transfer after Excel Online repackaging.

Never overwrite changed worksheet content. Reapply the committed operation onto
the CURRENT online workbook, preserving its other ZIP/XML parts. Back up both the
remote original and new candidate before upload; verify the operation matches the
already committed Supabase workbook's cells/formulas. Never silently treat a
semantic match as user approval: this function is triggered by an explicit UI
recovery confirmation. Conservative fail-closed behavior on uncertain outcomes.
"""
from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

from app.db.database import bump_version, connect, transaction
from app.excel.package import XlsxPackage, row_fingerprint
from app.excel.reader import FINGERPRINT_COLUMNS
from app.excel.writer import TransferError, transfer_row
from app.services import audit, catalog, onedrive
from app.services.errors import Conflict, ServiceError
from app.services.locks import lease
from app.services.normalize import normalize_barcode
from app.storage import cached_get, get_store, sha256_bytes
from app.timeutil import utcnow_iso
from app.services.onedrive_diff import compare_workbook_cells

log = logging.getLogger(__name__)
JOURNAL_KEY = "onedrive.reconciliation"
MAX_SIMPLE_UPLOAD = 4 * 1024 * 1024


def _pending_op(imp: dict, b: dict) -> dict:
    current, synced = int(imp["working_version"]), int(b["synced_version"])
    if current != synced + 1:
        raise Conflict("Η αυτόματη αποκατάσταση υποστηρίζει μία εκκρεμή μεταφορά κάθε φορά. "
                       "Το αρχείο Supabase παραμένει ασφαλές.")
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM transfer_operations WHERE import_id=? AND base_version=? "
            "AND status='completed' ORDER BY id",
            (imp["id"], synced),
        ).fetchall()
    if len(rows) != 1 or rows[0]["new_object"] != imp["working_object"]:
        raise Conflict("Δεν υπάρχει μία μοναδική εκκρεμής μεταφορά που να ταιριάζει στο ενεργό Excel.")
    return dict(rows[0])


def _safe_cells_match(a: bytes, b: bytes) -> bool:
    r = compare_workbook_cells(a, b)
    return bool(r["sheet_names_equal"] and r["cell_values_equal"] and not r["truncated"])


def _remote_candidate(remote: bytes, op: dict, imp: dict, expected: bytes) -> bytes:
    """Regenerate one committed transfer using the current remote package as its source."""
    with TemporaryDirectory(prefix="warehouse-reconcile-") as tmp:
        src, dest = Path(tmp) / "remote.xlsx", Path(tmp) / "candidate.xlsx"
        src.write_bytes(remote)
        with XlsxPackage(src) as pkg:
            target_row = int(op["source_row"])
            row = next((r for r in pkg.iter_rows(imp["source_sheet"]) if r.number == target_row), None)
            if row is None:
                raise Conflict("Δεν βρέθηκε η αναμενόμενη γραμμή στο online Excel.")
            cell = row.cells.get("H")
            barcode = cell.resolved(pkg.shared_strings) if cell else None
            if normalize_barcode(barcode or "") != normalize_barcode(op["barcode"]):
                raise Conflict("Το barcode στη γραμμή του OneDrive άλλαξε. Απαιτείται χειροκίνητος έλεγχος.")
            fingerprint = row_fingerprint(row, pkg.shared_strings, FINGERPRINT_COLUMNS)
        try:
            result = transfer_row(src, dest, imp["source_sheet"], imp["target_sheet"],
                                  normalize_barcode(op["barcode"]), fingerprint, mode=op["mode"])
        except TransferError as exc:
            raise Conflict("Δεν ήταν δυνατή η ασφαλής επανεφαρμογή της μεταφοράς: " + str(exc)) from exc
        if (result.mode != op["mode"] or result.source_row != op["source_row"]
                or result.target_row != op["target_row"]):
            raise Conflict("Η μεταφορά στο online Excel θα επηρέαζε διαφορετική γραμμή. Δεν έγινε αλλαγή.")
        candidate = dest.read_bytes()
        if not _safe_cells_match(candidate, expected):
            raise Conflict("Το αποτέλεσμα δεν συμφωνεί με τα κελιά και τους τύπους του ασφαλούς Excel v2. "
                           "Δεν έγινε καμία αλλαγή στο OneDrive.")
        return candidate


def _publish(drive: str, item: str, etag: str, candidate: bytes, token: str) -> None:
    # Smaller Excel workbooks can be replaced using the documented direct PUT
    # endpoint; the session API was returning HTTP 409 for this OneDrive Personal file.
    # If-Match must be forwarded and checked by Microsoft (HTTP 412 on mismatch).
    if len(candidate) <= MAX_SIMPLE_UPLOAD:
        onedrive._graph(
            token, "PUT", f"/drives/{drive}/items/{item}/content",
            headers={"If-Match": etag, "Content-Type": "application/octet-stream"},
            content=candidate,
        )
    else:
        onedrive._upload(drive, item, etag, candidate, token)


def _recovery_record() -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key=?", (JOURNAL_KEY,)).fetchone()
    return json.loads(row["value"]) if row else None


def _store_recovery_record(data: dict) -> None:
    with transaction() as conn:
        conn.execute("INSERT INTO app_settings(key,value) VALUES (?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (JOURNAL_KEY, json.dumps(data)))


def _finalize(imp: dict, b: dict, online: bytes, new_etag: str, record: dict, actor: str) -> dict:
    """Commit an immutable OneDrive-sourced version and the binding in one DB TX."""
    latest_version = int(imp["working_version"]) + 1
    object_name = f"imports/{int(imp['id']):05d}/working/v{latest_version:05d}_onedrive_{uuid.uuid4().hex}.xlsx"
    new_sha = get_store().put_verified(object_name, online)
    new_b = dict(b)
    new_b["synced_version"] = latest_version
    new_b["etag"] = new_etag
    with transaction(lock="scans") as conn:
        changed = conn.execute(
            "UPDATE excel_imports SET working_version=?,working_object=?,working_sha256=? "
            "WHERE id=? AND working_version=? AND working_object=? AND working_sha256=? AND is_active=1",
            (latest_version, object_name, new_sha, imp["id"], imp["working_version"],
             imp["working_object"], imp["working_sha256"]),
        ).rowcount
        if changed != 1:
            raise Conflict("Η ενεργή έκδοση Supabase άλλαξε ενδιάμεσα. Ολοκληρώστε την αποκατάσταση με τεχνικό έλεγχο.")
        row = conn.execute("SELECT value FROM app_settings WHERE key=?", (onedrive.BOUND_KEY,)).fetchone()
        bound_now = json.loads(row["value"]) if row else None
        if not bound_now or int(bound_now["import_id"]) != int(imp["id"]) or int(bound_now["synced_version"]) != int(b["synced_version"]):
            raise Conflict("Η σύνδεση OneDrive άλλαξε ταυτόχρονα. Ολοκληρώστε την αποκατάσταση με τεχνικό έλεγχο.")
        conn.execute("UPDATE app_settings SET value=? WHERE key=?",
                     (json.dumps(new_b, ensure_ascii=False), onedrive.BOUND_KEY))
        conn.execute("DELETE FROM app_settings WHERE key=?", (JOURNAL_KEY,))
        audit.audit(conn, actor, "onedrive.reconcile", {
            "import_id": imp["id"], "new_version": latest_version,
            "online_backup": record["backup_obj"],
            "previous_working": imp["working_object"], "operation_id": record["op_id"],
        })
        bump_version(conn)
    return {"new_version": latest_version, "barcode": record["barcode"],
            "backup": record["backup_obj"]}


def recover_single_pending(actor: str) -> dict:
    """Explicit recovery; re-run safely if publish succeeded but DB finalization failed."""
    with lease(catalog.EXCEL_LOCK, busy_message="Άλλη ενέργεια στο Excel είναι σε εξέλιξη."):
        imp = catalog.active_import()
        if not imp:
            raise ServiceError("Δεν υπάρχει ενεργό Excel.")
        b = onedrive.bound_import(int(imp["id"]))
        if not b:
            raise ServiceError("Δεν υπάρχει δεσμευμένο online Excel.")
        op = _pending_op(imp, b)
        current = cached_get(imp["working_object"], imp["working_sha256"]).read_bytes()
        token = onedrive._access_token()
        remote, meta = onedrive._stable_download(b["drive_id"], b["item_id"], token)
        etag = meta["eTag"]
        record = _recovery_record()

        if record and (int(record["import_id"]) != int(imp["id"])
                       or int(record["op_id"]) != int(op["id"])):
            raise Conflict("Υπάρχει άλλη εκκρεμής διαδικασία αποκατάστασης. Δεν θα αντικαταστήσουμε αρχείο.")

        if record:
            candidate = get_store().get(record["candidate_obj"])
            if sha256_bytes(candidate) != record["candidate_sha"]:
                raise Conflict("Το προετοιμασμένο αντίγραφο αποκατάστασης δεν είναι έγκυρο.")
            if sha256_bytes(remote) == record["candidate_sha"] or _safe_cells_match(remote, candidate):
                # Microsoft may repackage uploaded XLSX bytes. If the online file
                # already has exactly the expected cell/formula state, DO NOT
                # overwrite it again. Adopt the actual online bytes instead.
                return _finalize(imp, b, remote, etag, record, actor)
            if sha256_bytes(remote) != record["remote_sha"] or etag != record["remote_etag"]:
                raise Conflict("Το OneDrive άλλαξε μετά την προετοιμασία. "
                               "Δεν θα ξαναστείλουμε το Excel αυτόματα.")
        else:
            # Need exact pre-transfer source to prove the online cells are unchanged.
            base = cached_get(op["base_object"], op["excel_before_sha256"]).read_bytes()
            if not _safe_cells_match(base, remote):
                raise Conflict("Το OneDrive δεν έχει ίδιες τιμές/τύπους με το αρχείο πριν από τη μεταφορά. "
                               "Δεν θα γίνει αυτόματη αποκατάσταση.")
            candidate = _remote_candidate(remote, op, imp, current)
            backup_obj = f"backups/onedrive_reconcile_before_{uuid.uuid4().hex}.xlsx"
            candidate_obj = f"imports/{int(imp['id']):05d}/working/reconcile_{uuid.uuid4().hex}.xlsx"
            get_store().put_verified(backup_obj, remote)
            get_store().put_verified(candidate_obj, candidate)
            record = {"import_id": imp["id"], "op_id": op["id"], "barcode": op["barcode"],
                      "remote_sha": sha256_bytes(remote), "remote_etag": etag,
                      "backup_obj": backup_obj, "candidate_obj": candidate_obj,
                      "candidate_sha": sha256_bytes(candidate)}
            _store_recovery_record(record)
        _publish(b["drive_id"], b["item_id"], etag, candidate, token)
        online, updated_meta = onedrive._stable_download(b["drive_id"], b["item_id"], token)
        if not _safe_cells_match(online, candidate):
            raise Conflict("Το OneDrive δεν επέστρεψε το αναμενόμενο αποτέλεσμα. "
                           "Τα αντίγραφα υπάρχουν στο Supabase, αλλά απαιτείται έλεγχος πριν συνεχίσουν οι μεταφορές.")
        return _finalize(imp, b, online, updated_meta["eTag"], record, actor)
