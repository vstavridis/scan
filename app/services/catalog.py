"""Εισαγωγή Excel (εκδόσεις) και κατάλογος αναζήτησης barcode.

Αρχεία στο object storage (Supabase Storage, private bucket):

    staging/<id>.xlsx                         ανεβασμένο, ελεγμένο, όχι ακόμη ενεργό
    imports/<NNNNN>/original.xlsx             αναλλοίωτο αρχικό αρχείο της εισαγωγής
    imports/<NNNNN>/working/v<NNNNN>[_opNNNNNN].xlsx  εκδόσεις working Excel (αμετάβλητες)
    backups/<χρονοσφραγίδα>_<λόγος>.xlsx       χειροκίνητα αντίγραφα ασφαλείας

Η PostgreSQL κρατά τον κατάλογο προϊόντων (για γρήγορη αναζήτηση σε κάθε scan, χωρίς
κατέβασμα Excel) και ποια έκδοση working Excel είναι η τρέχουσα.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.db.database import Conn, bump_version, connect, transaction
from app.excel.reader import ValidationReport, validate_workbook
from app.services.audit import audit
from app.services.errors import ConfirmationRequired, NotFound, ServiceError
from app.services.locks import lease
from app.storage import StorageError, cached_get, get_store, sha256_bytes
from app.timeutil import local_stamp, utcnow_iso

log = logging.getLogger(__name__)

_STAGING_ID_RE = re.compile(r"^[0-9a-f]{32}$")
MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # όριο αρχείου του Supabase Storage (free plan)
EXCEL_LOCK = "excel"


def original_object(import_id: int) -> str:
    return f"imports/{import_id:05d}/original.xlsx"


def working_object(import_id: int, version: int, op_id: int | None = None) -> str:
    """Όνομα αρχείου έκδοσης. Οι εκδόσεις από μεταφορές περιέχουν και τον αριθμό της ενέργειας,
    ώστε ένα "ορφανό" αρχείο από μεταφορά που διακόπηκε να μη συγκρούεται με νέα προσπάθεια."""
    suffix = f"_op{op_id:06d}" if op_id is not None else ""
    return f"imports/{import_id:05d}/working/v{version:05d}{suffix}.xlsx"


def _validate_bytes(data: bytes) -> ValidationReport:
    s = get_settings()
    s.cache_dir.mkdir(parents=True, exist_ok=True)
    tmp = s.cache_dir / f"validate-{uuid.uuid4().hex}.xlsx"
    tmp.write_bytes(data)
    try:
        return validate_workbook(tmp, s.source_sheet, s.target_sheet)
    finally:
        tmp.unlink(missing_ok=True)


# --------------------------------------------------------------------------- staging
def stage_upload(filename: str, data: bytes, actor: str) -> dict[str, Any]:
    """Ελέγχει το ανεβασμένο αρχείο και το φυλάσσει μόνιμα (staging) στο object storage."""
    filename = Path(filename).name
    if not filename.lower().endswith(".xlsx"):
        raise ServiceError("Επιτρέπονται μόνο αρχεία .xlsx.")
    if not data:
        raise ServiceError("Το αρχείο είναι κενό.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ServiceError("Το αρχείο είναι πολύ μεγάλο (όριο 50 MB).")
    report = _validate_bytes(data)
    staging_id = uuid.uuid4().hex
    obj = f"staging/{staging_id}.xlsx"
    try:
        digest = get_store().put_verified(obj, data)
    except StorageError as exc:
        raise ServiceError(f"Το αρχείο δεν αποθηκεύτηκε στο Supabase Storage: {exc}") from exc
    summary = report.summary()
    with transaction() as conn:
        conn.execute(
            "INSERT INTO staged_uploads(id, filename, object_path, sha256, uploaded_by, uploaded_at, report_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (staging_id, filename, obj, digest, actor, utcnow_iso(), json.dumps(summary, ensure_ascii=False)),
        )
        audit(conn, actor, "import.stage", {"filename": filename, "staging_id": staging_id, "ok": report.ok,
                                            "products": report.product_count})
    log.info("Staged %s as %s (ok=%s)", filename, staging_id, report.ok)
    return {"staging_id": staging_id, "filename": filename, "report": summary}


# --------------------------------------------------------------------------- activation
def active_import(conn: Conn | None = None) -> dict[str, Any] | None:
    def q(c: Conn):
        row = c.execute("SELECT * FROM excel_imports WHERE is_active = 1").fetchone()
        return dict(row) if row else None

    if conn is not None:
        return q(conn)
    with connect() as c:
        return q(c)


def count_active_items(conn: Conn) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM active_items WHERE status='active'").fetchone()["n"])


def activate(staging_id: str, actor: str, confirm_replace: bool = False) -> dict[str, Any]:
    if not _STAGING_ID_RE.match(staging_id or ""):
        raise ServiceError("Μη έγκυρο αναγνωριστικό αρχείου.")
    with connect() as conn:
        staged = conn.execute("SELECT * FROM staged_uploads WHERE id=?", (staging_id,)).fetchone()
    if staged is None:
        raise NotFound("Το αρχείο προς ενεργοποίηση δεν βρέθηκε. Ανεβάστε το ξανά.")
    staged = dict(staged)
    store = get_store()
    try:
        data = store.get(staged["object_path"])
    except StorageError as exc:
        raise ServiceError(f"Δεν ήταν δυνατή η ανάκτηση του αρχείου: {exc}") from exc
    if sha256_bytes(data) != staged["sha256"]:
        raise ServiceError("Το αποθηκευμένο αρχείο δεν ταιριάζει με αυτό που ελέγχθηκε. Ανεβάστε το ξανά.")
    report = _validate_bytes(data)
    if not report.ok:
        raise ServiceError("Το αρχείο δεν πέρασε τον έλεγχο: " + "; ".join(report.errors), extra={"report": report.summary()})
    s = get_settings()

    with lease(EXCEL_LOCK, busy_message="Εκτελείται μεταφορά ή άλλη εισαγωγή Excel. Δοκιμάστε ξανά σε λίγο."):
        with connect() as conn:
            n_active = count_active_items(conn)
            previous = active_import(conn)
        if n_active and not confirm_replace:
            raise ConfirmationRequired(
                f"Υπάρχουν {n_active} ενεργά barcode στη λίστα. Η αντικατάσταση του Excel απαιτεί ρητή επιβεβαίωση.",
                extra={"active_items": n_active},
            )
        # 1. νέα εγγραφή εισαγωγής (pending)
        with transaction() as conn:
            import_id = int(conn.execute(
                """INSERT INTO excel_imports(original_filename, original_sha256, imported_at, imported_by, status,
                       is_active, product_count, unique_barcodes, duplicate_barcodes, source_sheet, target_sheet, notes)
                   VALUES (?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?, ?, ?) RETURNING id""",
                (staged["filename"], staged["sha256"], utcnow_iso(), actor, report.product_count,
                 report.unique_barcodes, report.duplicate_barcodes, s.source_sheet, s.target_sheet,
                 json.dumps({"warnings": report.warnings}, ensure_ascii=False)),
            ).fetchone()["id"])
        # 2. αρχικό + πρώτη έκδοση working στο storage (με επαλήθευση)
        orig, work = original_object(import_id), working_object(import_id, 1)
        try:
            store.put_verified(orig, data)
            store.put_verified(work, data)
        except StorageError as exc:
            with transaction() as conn:
                conn.execute("UPDATE excel_imports SET status='failed', notes=? WHERE id=?", (str(exc), import_id))
            raise ServiceError(f"Η ενεργοποίηση ακυρώθηκε: το Excel δεν αποθηκεύτηκε ({exc}).") from exc
        # 3. ενεργοποίηση + κατάλογος + αντιστοίχιση ενεργής λίστας (ένα transaction)
        with transaction(lock="scans") as conn:
            conn.execute(
                "UPDATE excel_imports SET original_object=?, working_object=?, working_version=1, working_sha256=?, "
                "status='ready' WHERE id=?",
                (orig, work, staged["sha256"], import_id),
            )
            conn.execute("UPDATE excel_imports SET is_active=0 WHERE is_active=1")
            conn.execute("UPDATE excel_imports SET is_active=1 WHERE id=?", (import_id,))
            conn.executemany(
                """INSERT INTO product_records(import_id, sheet_row, barcode, barcode_norm, eidos, paxos, diastaseis,
                       temaxia, kila, pelatis, vspg, topothesia, thesi, rolo, extra_json, fingerprint, group_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (import_id, p.sheet_row, p.barcode, p.barcode_norm, p.values["eidos"], p.values["paxos"],
                     p.values["diastaseis"], p.values["temaxia"], p.values["kila"], p.values["pelatis"],
                     p.values["vspg"], p.values["topothesia"], p.values["thesi"], p.values["rolo"],
                     json.dumps(p.extra, ensure_ascii=False), p.fingerprint, p.group_key)
                    for p in report.products
                ],
            )
            remapped = _remap_active_items(conn, import_id)
            conn.execute("DELETE FROM staged_uploads WHERE id=?", (staging_id,))
            backups = [previous["working_object"]] if previous else []
            audit(conn, actor, "import.activate", {
                "import_id": import_id, "filename": staged["filename"], "products": report.product_count,
                "previous_import": previous["id"] if previous else None, "previous_working": backups,
                "remapped": remapped,
            })
            bump_version(conn)
    try:
        store.delete(staged["object_path"])
    except StorageError:
        log.warning("Could not delete staging object %s", staged["object_path"])
    log.info("Activated import %s (%s)", import_id, staged["filename"])
    return {"import_id": import_id, "filename": staged["filename"], "report": report.summary(), "backups": backups,
            "remapped": remapped}


def _remap_active_items(conn: Conn, import_id: int) -> dict[str, int]:
    """Ενημερώνει την ενεργή λίστα ώστε να δείχνει στον νέο κατάλογο."""
    counts = {"ok": 0, "ambiguous": 0, "unknown": 0}
    items = conn.execute("SELECT id, barcode_norm FROM active_items WHERE status='active'").fetchall()
    for it in items:
        matches = lookup(conn, import_id, it["barcode_norm"])
        if len(matches) == 1:
            state, pid = "ok", matches[0]["id"]
        elif len(matches) > 1:
            state, pid = "ambiguous", None
        else:
            state, pid = "unknown", None
        conn.execute("UPDATE active_items SET state=?, product_record_id=?, import_id=? WHERE id=?",
                     (state, pid, import_id, it["id"]))
        counts[state] += 1
    return counts


# --------------------------------------------------------------------------- lookups
def lookup(conn: Conn, import_id: int, barcode_norm: str, include_transferred: bool = False) -> list[Any]:
    sql = "SELECT * FROM product_records WHERE import_id=? AND barcode_norm=?"
    if not include_transferred:
        sql += " AND status='available'"
    return conn.execute(sql + " ORDER BY sheet_row", (import_id, barcode_norm)).fetchall()


def product_dict(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    d["extra"] = json.loads(d.pop("extra_json") or "{}")
    return d


def snapshot_from_record(row: Any) -> str:
    d = {k: row[k] for k in ("barcode", "eidos", "paxos", "diastaseis", "temaxia", "kila", "pelatis", "vspg",
                             "topothesia", "thesi", "rolo", "sheet_row")}
    d["extra"] = json.loads(row["extra_json"] or "{}")
    return json.dumps(d, ensure_ascii=False)


def list_imports() -> list[dict[str, Any]]:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM excel_imports ORDER BY id DESC").fetchall()]


def search_products(term: str, limit: int = 50) -> list[dict[str, Any]]:
    with connect() as conn:
        imp = active_import(conn)
        if imp is None:
            return []
        like = f"%{term.strip()}%"
        rows = conn.execute(
            """SELECT * FROM product_records WHERE import_id=? AND
                   (barcode LIKE ? OR eidos LIKE ? OR pelatis LIKE ?) ORDER BY sheet_row LIMIT ?""",
            (imp["id"], like, like, like, limit),
        ).fetchall()
        return [product_dict(r) for r in rows]  # type: ignore[misc]


# --------------------------------------------------------------------------- αρχεία
def working_bytes() -> tuple[str, bytes]:
    """(όνομα αρχείου, περιεχόμενο) του τρέχοντος ενημερωμένου Excel, με έλεγχο sha256."""
    imp = active_import()
    if imp is None:
        raise NotFound("Δεν υπάρχει ενεργό Excel.")
    path = cached_get(imp["working_object"], imp["working_sha256"])
    stem = Path(imp["original_filename"]).stem
    return f"{stem}_ενημερωμένο_v{imp['working_version']}_{local_stamp()[:15]}.xlsx", path.read_bytes()


def make_backup(actor: str) -> str:
    imp = active_import()
    if imp is None:
        raise NotFound("Δεν υπάρχει ενεργό Excel.")
    data = cached_get(imp["working_object"], imp["working_sha256"]).read_bytes()
    obj = f"backups/{local_stamp()}_manual_import-{imp['id']:05d}_v{imp['working_version']:05d}.xlsx"
    get_store().put_verified(obj, data)
    with transaction() as conn:
        audit(conn, actor, "backup.manual", {"object": obj})
    return obj


def list_backups() -> list[dict[str, Any]]:
    """Όλες οι αποθηκευμένες εκδόσεις: αρχικά, κάθε έκδοση working, χειροκίνητα backups."""
    store = get_store()
    out = []
    for prefix, kind in (("backups", "Χειροκίνητο αντίγραφο"), ("imports", "")):
        for o in store.list(prefix):
            label = kind
            if not label:
                label = "Αρχικό αρχείο" if o.path.endswith("original.xlsx") else "Έκδοση working Excel"
            out.append({"path": o.path, "size": o.size, "updated_at": o.updated_at, "kind": label})
    return sorted(out, key=lambda o: o["path"], reverse=True)


def get_object(path: str) -> bytes:
    if not (path.startswith("imports/") or path.startswith("backups/")):
        raise NotFound("Δεν βρέθηκε.")
    return get_store().get(path)
