"""Best-effort OneDrive mirror: replay committed Supabase transfers onto current online XLSX.

The warehouse stays operational if Microsoft has a lock, ETag drift, or network
failure. Transfer operations are durable PostgreSQL outbox entries and immutable
Supabase versions. On each retry, ONLY completed ops are replayed in sequence
on the latest downloaded OneDrive workbook. Every referenced barcode, row,
result and final workbook semantics are checked. A direct write uses If-Match.

This is not a substitute for Microsoft Graph workbook APIs, which aren't
supported by OneDrive Personal. An always-open file may temporarily refuse
writes. Retries happen automatically in the background Excel sync worker
(app.services.excel_worker, about every minute while pending) and on the Settings retry button.
"""
from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

from app.db.database import connect, transaction, bump_version
from app.excel.writer import Move, TransferError, WorkbookEditor
from app.services import catalog, onedrive
from app.services.audit import audit
from app.services.errors import Conflict, ServiceError
from app.services.locks import lease
from app.services.normalize import normalize_barcode
from app.services.onedrive_diff import compare_workbook_cells
from app.storage import cached_get, get_store, sha256_bytes
from app.timeutil import utcnow_iso

log = logging.getLogger(__name__)
QUEUE_JOURNAL = "onedrive.outbox"
MAX_BATCH_OPS = 2000


def _same_cells(first: bytes, second: bytes) -> bool:
    result = compare_workbook_cells(first, second)
    return bool(result["sheet_names_equal"] and result["cell_values_equal"]
                and not result["truncated"])


def _pending_ops(imp: dict, b: dict) -> list[dict]:
    """Ολοκληρωμένες ενέργειες (μεταφορές/επαναφορές) που δεν έχουν περάσει ακόμη στο OneDrive.

    Μία έκδοση Excel μπορεί να περιέχει πολλές ενέργειες (ομαδική μεταφορά): όλες έχουν ίδιο
    base_version/new_object και σειρά `seq`. Κάθε έκδοση από synced_version έως working_version
    πρέπει να καλύπτεται πλήρως, αλλιώς δεν γράφουμε τίποτα στο online αρχείο.
    """
    start, end = int(b["synced_version"]), int(imp["working_version"])
    if start >= end:
        return []
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM transfer_operations WHERE import_id=? AND "
            "base_version>=? AND base_version<? AND status='completed' "
            "ORDER BY base_version, COALESCE(seq, 0), id",
            (imp["id"], start, end),
        ).fetchall()
    ops = [dict(r) for r in rows]
    groups = _groups(ops)
    if (len(ops) > MAX_BATCH_OPS
            or [int(g[0]["base_version"]) for g in groups] != list(range(start, end))
            or not all(o.get("base_object") and o.get("new_object") and
                       o.get("excel_before_sha256") and o.get("excel_after_sha256")
                       for o in ops)
            or any(len({(o["new_object"], o["excel_after_sha256"], o["base_object"]) for o in g}) != 1
                   for g in groups)
            or groups[-1][0]["new_object"] != imp["working_object"]):
        raise Conflict(
            "Δεν μπορεί να επαληθευτεί πλήρως η σειρά των εκκρεμών μεταφορών "
            "στο OneDrive. Η αποθήκη συνεχίζει στο Supabase, αλλά χρειάζεται "
            "τεχνική συμφιλίωση πριν γίνει εγγραφή στο online αρχείο."
        )
    return ops


def _groups(ops: list[dict]) -> list[list[dict]]:
    """Ομαδοποίηση ενεργειών ανά έκδοση Excel (base_version), με τη σειρά εφαρμογής τους."""
    groups: list[list[dict]] = []
    for op in ops:
        if groups and groups[-1][0]["base_version"] == op["base_version"]:
            groups[-1].append(op)
        else:
            groups.append([op])
    return groups


def _version_data(ops: list[dict], count: int) -> bytes:
    """Περιεχόμενο της έκδοσης μετά από `count` εκδόσεις-ομάδες (0 = πριν από όλες)."""
    groups = _groups(ops)
    if count == 0:
        op = groups[0][0]
        return cached_get(op["base_object"], op["excel_before_sha256"]).read_bytes()
    op = groups[count - 1][0]
    return cached_get(op["new_object"], op["excel_after_sha256"]).read_bytes()


def _remote_prefix(remote: bytes, ops: list[dict]) -> int:
    # It is possible that an earlier upload succeeded but its acknowledgement
    # was lost. Identify the latest *fully committed* matching worksheet state.
    # Reject any real external worksheet change (no automatic destructive merge).
    for count in range(len(_groups(ops)), -1, -1):
        if _same_cells(remote, _version_data(ops, count)):
            return count
    raise Conflict(
        "Το online Excel περιέχει διαφορετικές τιμές ή τύπους από όλες "
        "τις αποθηκευμένες εκδόσεις. Δεν θα αντικαταστήσουμε αλλαγές τρίτων. "
        "Οι μεταφορές παραμένουν στο Supabase."
    )


def _replay(remote: bytes, ops: list[dict], prefix: int, expected: bytes,
            source_sheet: str, target_sheet: str) -> bytes:
    """Εφαρμόζει τις εκκρεμείς εκδόσεις-ομάδες (μετά το prefix) στο online αρχείο, σε ένα πέρασμα.

    Κάθε ενέργεια πρέπει να αλλάξει ΑΚΡΙΒΩΣ τις ίδιες γραμμές με αυτές που καταγράφηκαν.
    """
    pending = [op for g in _groups(ops)[prefix:] for op in g]
    with TemporaryDirectory(prefix="warehouse-onedrive-outbox-") as tmp:
        cur, nxt = Path(tmp) / "current.xlsx", Path(tmp) / "next.xlsx"
        cur.write_bytes(remote)
        try:
            with WorkbookEditor(cur) as ed:
                for op in pending:
                    restore = op.get("kind") == "restore"
                    move = Move(target_sheet if restore else source_sheet,
                                source_sheet if restore else target_sheet,
                                normalize_barcode(op["barcode"]), op["fingerprint"], mode=op["mode"],
                                prefer_row=int(op["source_row"]), pick_last=restore)
                    res = ed.move(move)
                    if (res.mode != op["mode"] or int(res.source_row) != int(op["source_row"])
                            or int(res.target_row) != int(op["target_row"])):
                        raise Conflict("Η εκκρεμής μεταφορά θα άλλαζε διαφορετικές γραμμές. "
                                       "Δεν έγινε αντικατάσταση.")
                ed.finish(nxt)
        except (Conflict, ServiceError):
            raise
        except TransferError as exc:
            raise Conflict("Η μεταφορά δεν μπορεί να επαναληφθεί στο online Excel: " + str(exc)) from exc
        except Exception as exc:
            log.warning("Outbox XML replay could not read Microsoft package: %s", type(exc).__name__)
            raise ServiceError("Το Excel Online αποθηκεύτηκε με δομή που δεν μπορεί "
                               "να ενημερωθεί με ασφάλεια. Οι μεταφορές παραμένουν "
                               "στο Supabase, χωρίς αντικατάσταση.") from None
        candidate = nxt.read_bytes()
    if not _same_cells(candidate, expected):
        raise Conflict("Το αποτέλεσμα των εκκρεμών μεταφορών δεν συμφωνεί "
                       "με το ενημερωμένο Excel στο Supabase. Δεν έγινε αντικατάσταση.")
    return candidate


def _journal() -> dict | None:
    with connect() as db:
        row = db.execute("SELECT value FROM app_settings WHERE key=?", (QUEUE_JOURNAL,)).fetchone()
    return json.loads(row["value"]) if row else None


def _save_journal(record: dict) -> None:
    with transaction() as db:
        db.execute("INSERT INTO app_settings(key,value) VALUES (?,?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (QUEUE_JOURNAL, json.dumps(record)))


def _adopt(imp: dict, b: dict, online: bytes, meta: dict, record: dict,
           actor: str) -> dict:
    """New version + synced binding + audit, committed atomically."""
    version = int(imp["working_version"]) + 1
    destination = (f"imports/{int(imp['id']):05d}/working/"
                   f"v{version:05d}_onedrive_{uuid.uuid4().hex}.xlsx")
    digest = get_store().put_verified(destination, online)
    new_binding = dict(b)
    new_binding["synced_version"] = version
    new_binding["etag"] = meta["eTag"]
    new_binding["synced_at"] = utcnow_iso()
    with transaction(lock="scans") as db:
        row = db.execute(
            "UPDATE excel_imports SET working_version=?,working_object=?,working_sha256=? "
            "WHERE id=? AND is_active=1 AND working_version=? AND working_object=? "
            "AND working_sha256=?",
            (version, destination, digest, imp["id"], imp["working_version"],
             imp["working_object"], imp["working_sha256"]),
        )
        if row.rowcount != 1:
            raise Conflict("Η έκδοση Supabase άλλαξε κατά τον συγχρονισμό.")
        existing = db.execute("SELECT value FROM app_settings WHERE key=?",
                              (onedrive.BOUND_KEY,)).fetchone()
        latest = json.loads(existing["value"]) if existing else None
        if (not latest or int(latest["import_id"]) != int(imp["id"])
                or int(latest["synced_version"]) != int(b["synced_version"])
                or latest["drive_id"] != b["drive_id"]
                or latest["item_id"] != b["item_id"]):
            raise Conflict("Η σύνδεση OneDrive άλλαξε κατά τον συγχρονισμό.")
        db.execute("UPDATE app_settings SET value=? WHERE key=?",
                   (json.dumps(new_binding, ensure_ascii=False), onedrive.BOUND_KEY))
        db.execute("DELETE FROM app_settings WHERE key=?", (QUEUE_JOURNAL,))
        db.execute("DELETE FROM app_settings WHERE key=?", (onedrive.ERROR_KEY,))
        # Legacy one-operation reconciliation journal, if any, is superseded.
        db.execute("DELETE FROM app_settings WHERE key=?", ("onedrive.reconciliation",))
        audit(db, actor, "onedrive.outbox_synced", {
            "import_id": imp["id"], "new_version": version,
            "backup": record["backup_obj"], "operations": record["op_ids"],
        })
        bump_version(db)
    return {"synced": True, "version": version,
            "operations": len(record["op_ids"]), "backup": record["backup_obj"]}


def flush_under_lease(actor: str = "onedrive-mirror") -> dict:
    imp = catalog.active_import()
    if not imp:
        raise ServiceError("Δεν υπάρχει ενεργό Excel.")
    b = onedrive.bound_import(int(imp["id"]))
    if not b:
        return {"synced": True, "operations": 0}
    ops = _pending_ops(imp, b)
    if not ops:
        return {"synced": True, "operations": 0}

    token = onedrive._access_token()
    remote, meta = onedrive._stable_download(b["drive_id"], b["item_id"], token)
    desired = cached_get(imp["working_object"], imp["working_sha256"]).read_bytes()
    prefix = _remote_prefix(remote, ops)
    candidate = _replay(remote, ops, prefix, desired, imp["source_sheet"],
                        imp["target_sheet"])
    if not _same_cells(candidate, desired):
        raise Conflict("Τα τελικά κελιά δεν συμφωνούν. Δεν έγινε συγχρονισμός.")

    # Journal + immutable backups BEFORE attempting to touch the online file.
    # If an earlier push succeeded but the acknowledgement was lost, prefix ==
    # len(ops) and no duplicate update is made.
    prior = _journal()
    if prior and int(prior["import_id"]) != int(imp["id"]):
        raise Conflict("Υπάρχει άλυτη αποκατάσταση προηγούμενης εισαγωγής.")
    if (prior and prior["target_sha"] == imp["working_sha256"]
            and prior["op_ids"] == [o["id"] for o in ops]
            and _same_cells(remote, get_store().get(prior["candidate_obj"]))):
        return _adopt(imp, b, remote, meta, prior, actor)
    backup_obj = f"backups/onedrive_before_sync_{uuid.uuid4().hex}.xlsx"
    candidate_obj = (f"imports/{int(imp['id']):05d}/working/"
                     f"outbox_{uuid.uuid4().hex}.xlsx")
    store = get_store()
    store.put_verified(backup_obj, remote)
    store.put_verified(candidate_obj, candidate)
    record = {"import_id": imp["id"], "target_sha": imp["working_sha256"],
              "op_ids": [o["id"] for o in ops], "backup_obj": backup_obj,
              "candidate_obj": candidate_obj, "remote_etag": meta["eTag"]}
    _save_journal(record)
    if prefix != len(_groups(ops)):
        # Uses If-Match so a concurrent cloud edit is rejected, not overwritten.
        from app.services.onedrive_reconcile import _publish
        _publish(b["drive_id"], b["item_id"], meta["eTag"], candidate, token)
        remote, meta = onedrive._stable_download(b["drive_id"], b["item_id"], token)
        if not _same_cells(remote, candidate):
            raise Conflict("Το OneDrive δεν επέστρεψε το αναμενόμενο αποτέλεσμα. "
                           "Τα αντίγραφα διατηρήθηκαν στο Supabase.")
    return _adopt(imp, b, remote, meta, record, actor)


def retry_sync(actor: str = "onedrive-mirror") -> dict:
    with lease(catalog.EXCEL_LOCK, busy_message="Άλλη ενέργεια Excel είναι σε εξέλιξη."):
        return flush_under_lease(actor)
