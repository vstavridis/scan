"""Εισαγωγή Excel (εκδόσεις) και κατάλογος αναζήτησης barcode."""
from __future__ import annotations

import json
import logging
import re
import shutil
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.db.database import bump_version, connect, transaction
from app.excel.reader import ValidationReport, snapshot_json, validate_workbook
from app.excel.storage import excel_write_lock, get_store
from app.services.audit import audit
from app.services.errors import ConfirmationRequired, NotFound, ServiceError
from app.timeutil import local_stamp, utcnow_iso

log = logging.getLogger(__name__)

_STAGING_ID_RE = re.compile(r"^[0-9a-f]{32}$")
MAX_UPLOAD_BYTES = 200 * 1024 * 1024


# --------------------------------------------------------------------------- staging
def _staging_path(staging_id: str) -> Path:
    if not _STAGING_ID_RE.match(staging_id):
        raise ServiceError("Μη έγκυρο αναγνωριστικό αρχείου.")
    return get_settings().staging_dir / f"{staging_id}.xlsx"


def stage_upload(filename: str, data: bytes, actor: str) -> dict[str, Any]:
    """Αποθηκεύει το ανεβασμένο αρχείο σε μόνιμο φάκελο staging και το ελέγχει."""
    if not filename.lower().endswith(".xlsx"):
        raise ServiceError("Επιτρέπονται μόνο αρχεία .xlsx.")
    if not data:
        raise ServiceError("Το αρχείο είναι κενό.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ServiceError("Το αρχείο είναι πολύ μεγάλο.")
    staging_id = uuid.uuid4().hex
    path = _staging_path(staging_id)
    path.write_bytes(data)
    (path.with_suffix(".name")).write_text(filename, encoding="utf-8")
    s = get_settings()
    report = validate_workbook(path, s.source_sheet, s.target_sheet)
    log.info("Staged %s as %s (ok=%s)", filename, staging_id, report.ok)
    with transaction() as conn:
        audit(conn, actor, "import.stage", {"filename": filename, "staging_id": staging_id, "ok": report.ok,
                                            "products": report.product_count})
    return {"staging_id": staging_id, "filename": filename, "report": report.summary()}


# --------------------------------------------------------------------------- activation
def active_import(conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    def q(c: sqlite3.Connection):
        row = c.execute("SELECT * FROM excel_imports WHERE is_active = 1").fetchone()
        return dict(row) if row else None

    if conn is not None:
        return q(conn)
    with connect() as c:
        return q(c)


def count_active_items(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM active_items WHERE status='active'").fetchone()[0]


def _backup_database(reason: str) -> Path:
    s = get_settings()
    dest = s.backups_dir / f"{local_stamp()}_db_{reason}.sqlite3"
    src = sqlite3.connect(str(s.db_path))
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return dest


def activate(staging_id: str, actor: str, confirm_replace: bool = False) -> dict[str, Any]:
    path = _staging_path(staging_id)
    if not path.is_file():
        raise NotFound("Το αρχείο προς ενεργοποίηση δεν βρέθηκε. Ανεβάστε το ξανά.")
    name_file = path.with_suffix(".name")
    filename = name_file.read_text(encoding="utf-8") if name_file.is_file() else path.name
    s = get_settings()
    report: ValidationReport = validate_workbook(path, s.source_sheet, s.target_sheet)
    if not report.ok:
        raise ServiceError("Το αρχείο δεν πέρασε τον έλεγχο: " + "; ".join(report.errors), extra={"report": report.summary()})

    store = get_store()
    with excel_write_lock:
        with connect() as conn:
            n_active = count_active_items(conn)
            previous = active_import(conn)
        if n_active and not confirm_replace:
            raise ConfirmationRequired(
                f"Υπάρχουν {n_active} ενεργά barcode στη λίστα. Η αντικατάσταση του Excel απαιτεί ρητή επιβεβαίωση.",
                extra={"active_items": n_active},
            )
        backups: list[str] = []
        if previous is not None:
            prev_working = Path(previous["working_path"])
            if prev_working.is_file():
                backups.append(str(store.backup(prev_working, "before-new-import")))
            backups.append(str(_backup_database("before-new-import")))

        with transaction() as conn:
            cur = conn.execute(
                """INSERT INTO excel_imports(original_filename, original_path, working_path, original_sha256,
                       imported_at, imported_by, is_active, product_count, unique_barcodes, duplicate_barcodes,
                       source_sheet, target_sheet, notes)
                   VALUES (?, '', '', ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)""",
                (filename, report.sha256, utcnow_iso(), actor, report.product_count, report.unique_barcodes,
                 report.duplicate_barcodes, s.source_sheet, s.target_sheet,
                 json.dumps({"warnings": report.warnings}, ensure_ascii=False)),
            )
            import_id = int(cur.lastrowid)
            original, working = store.save_original(import_id, path, filename)
            conn.execute("UPDATE excel_imports SET original_path=?, working_path=? WHERE id=?",
                         (str(original), str(working), import_id))
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
            audit(conn, actor, "import.activate", {
                "import_id": import_id, "filename": filename, "products": report.product_count,
                "backups": backups, "remapped": remapped,
            })
            bump_version(conn)
    try:
        path.unlink()
        name_file.unlink(missing_ok=True)
    except OSError:
        pass
    log.info("Activated import %s (%s)", import_id, filename)
    return {"import_id": import_id, "filename": filename, "report": report.summary(), "backups": backups,
            "remapped": remapped}


def _remap_active_items(conn: sqlite3.Connection, import_id: int) -> dict[str, int]:
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
def lookup(conn: sqlite3.Connection, import_id: int, barcode_norm: str, include_transferred: bool = False) -> list[sqlite3.Row]:
    sql = "SELECT * FROM product_records WHERE import_id=? AND barcode_norm=?"
    if not include_transferred:
        sql += " AND status='available'"
    return conn.execute(sql + " ORDER BY sheet_row", (import_id, barcode_norm)).fetchall()


def product_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    d["extra"] = json.loads(d.pop("extra_json") or "{}")
    return d


def snapshot_from_record(row: sqlite3.Row) -> str:
    d = {k: row[k] for k in ("barcode", "eidos", "paxos", "diastaseis", "temaxia", "kila", "pelatis", "vspg",
                             "topothesia", "thesi", "rolo", "sheet_row")}
    d["extra"] = json.loads(row["extra_json"] or "{}")
    return json.dumps(d, ensure_ascii=False)


def list_imports() -> list[dict[str, Any]]:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM excel_imports ORDER BY id DESC")]


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


def cleanup_staging(max_age_hours: int = 72) -> None:
    import time
    cutoff = time.time() - max_age_hours * 3600
    for f in get_settings().staging_dir.glob("*"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


def copy_working(dest: Path) -> Path:
    imp = active_import()
    if imp is None:
        raise NotFound("Δεν υπάρχει ενεργό Excel.")
    with excel_write_lock:
        shutil.copy2(imp["working_path"], dest)
    return dest


__all__ = ["stage_upload", "activate", "active_import", "lookup", "list_imports", "snapshot_json"]
