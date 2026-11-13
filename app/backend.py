"""Backend της εφαρμογής: το μοναδικό σημείο από όπου το UI (Streamlit) αλλάζει δεδομένα.

Η πρόσβαση στην εφαρμογή ελέγχεται από το Streamlit Community Cloud (Private app, μόνο
προσκεκλημένοι χρήστες). Κάθε συνάρτηση δέχεται την ταυτότητα του χρήστη (Principal) μόνο για το
ιστορικό ενεργειών (audit). Δεν υπάρχουν δημόσια API endpoints: όλα εκτελούνται μέσα στον Python
server του Streamlit και τα credentials της Supabase δεν φεύγουν ποτέ από αυτόν.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

from app.auth import Principal, authorize, make_principal
from app.config import get_settings
from app.db.database import init_db
from app.logging_setup import setup_logging
from app.services import aggregation, excel_worker, catalog, history, scans, settings_service, transfer
from app.services.errors import ServiceError

log = logging.getLogger(__name__)

_started = False


def startup(start_worker: bool = False) -> None:
    """Μία φορά ανά διεργασία: σχήμα βάσης + recovery μεταφορών που διακόπηκαν.

    start_worker=True (εφαρμογή Streamlit): ξεκινά τον background συγχρονισμό του Excel, που
    ολοκληρώνει και ό,τι έμεινε στην ουρά από προηγούμενη εκτέλεση.
    """
    global _started
    if not _started:
        setup_logging()
        init_db()
        recovered = transfer.recover_pending()
        if recovered:
            log.warning("Transfer recovery: %s", recovered)
        _started = True
    if start_worker:
        excel_worker.start()


# --------------------------------------------------------------------------- ταυτότητα
def identity(name: str | None = None, email: str | None = None) -> Principal:
    """Ο χρήστης της τρέχουσας συνεδρίας (για το audit). Χωρίς κωδικούς."""
    return make_principal(name, email)


def public_config() -> dict[str, Any]:
    cfg = settings_service.get_all()
    imp = catalog.active_import()
    return {
        "terminator": cfg["scanner_terminator"],
        "key_mapping": cfg["scanner_key_mapping"],
        "sound_default": cfg["scanner_sound_default"],
        "auto_fullscreen": cfg["scanner_auto_fullscreen"],
        "inter_key_timeout_ms": cfg["scanner_inter_key_timeout_ms"],
        "dashboard_refresh_seconds": cfg["dashboard_refresh_seconds"],
        "scanner_refresh_seconds": cfg["scanner_refresh_seconds"],
        "has_catalog": imp is not None,
        "catalog_name": imp["original_filename"] if imp else None,
    }


# --------------------------------------------------------------------------- σάρωση
def submit_scans(p: Principal | None, events: list[dict[str, Any]], device_id: str | None = None) -> dict[str, Any]:
    authorize(p, "scan")
    clean: list[scans.ScanIn] = []
    for e in events[:500]:
        eid = str(e.get("event_id") or "")
        if not (8 <= len(eid) <= 64) or not all(ch.isalnum() or ch in "-_" for ch in eid):
            continue  # μη έγκυρο event_id: αγνοείται (δεν μπορεί να επιβεβαιωθεί)
        seq = e.get("seq")
        clean.append(scans.ScanIn(eid, str(e.get("barcode") or "")[:512], (e.get("client_ts") or None),
                                  int(seq) if isinstance(seq, (int, float)) else None))
    return scans.process_batch(clean, device_id=(device_id or p.name)[:64])  # type: ignore[union-attr]


def scanner_view(p: Principal | None) -> dict[str, Any]:
    authorize(p, "view")
    return aggregation.scanner_snapshot()


# --------------------------------------------------------------------------- γραμματεία
def dashboard(p: Principal | None) -> dict[str, Any]:
    authorize(p, "dashboard")
    return aggregation.dashboard_snapshot()


def dashboard_if_changed(p: Principal | None, last_version: int | None) -> dict[str, Any] | None:
    """Fast, read-only conditional poll; cached UI stays responsive between scans."""
    authorize(p, "dashboard")
    return aggregation.dashboard_snapshot_if_changed(last_version)


def candidates(p: Principal | None, item_id: int) -> list[dict[str, Any]]:
    authorize(p, "dashboard")
    return aggregation.candidates_for(item_id)


def transfer_item(p: Principal | None, item_id: int) -> dict[str, Any]:
    authorize(p, "transfer")
    return transfer.transfer_item(item_id, p.actor)  # type: ignore[union-attr]


def queue_transfers(p: Principal | None, item_ids: list[int]) -> dict[str, Any]:
    """Γρήγορη καταχώριση μίας ή πολλών μεταφορών· το Excel ενημερώνεται στο παρασκήνιο."""
    authorize(p, "transfer")
    res = transfer.queue_transfers(item_ids, p.actor)  # type: ignore[union-attr]
    if res["queued"]:
        excel_worker.kick()
    return res


def restore_transfer(p: Principal | None, transfer_op_id: int, wait: bool = False) -> dict[str, Any]:
    """Επαναφορά λανθασμένης μεταφοράς στα «Έτοιμα προϊόντα» (τελευταία ελεύθερη σειρά)."""
    authorize(p, "transfer")
    res = transfer.restore_transfer(transfer_op_id, p.actor, wait=wait)  # type: ignore[union-attr]
    excel_worker.kick()
    return res


def list_transfers(p: Principal | None, barcode: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    authorize(p, "dashboard")
    return transfer.list_transfers(limit=min(max(limit, 1), 1000), barcode=barcode)


def sync_status(p: Principal | None) -> dict[str, Any]:
    authorize(p, "dashboard")
    out = transfer.sync_status()
    out["worker_running"] = excel_worker.running()
    try:
        from app.services import onedrive
        out["onedrive"] = onedrive.status()
    except Exception:
        out["onedrive"] = {"bound": False, "pending": False}
    return out


def sync_now(p: Principal | None) -> dict[str, Any]:
    authorize(p, "transfer")
    return transfer.sync_now(p.actor)  # type: ignore[union-attr]


def correct_item(p: Principal | None, item_id: int, barcode: str) -> dict[str, Any]:
    """Διόρθωση barcode που χτυπήθηκε λάθος (στα «Προς έλεγχο»)."""
    authorize(p, "transfer")
    return transfer.correct_item(item_id, barcode[:128], p.actor)  # type: ignore[union-attr]


def resolve_item(p: Principal | None, item_id: int, product_record_id: int) -> dict[str, Any]:
    authorize(p, "transfer")
    return transfer.resolve_ambiguous(item_id, product_record_id, p.actor)  # type: ignore[union-attr]


def remove_item(p: Principal | None, item_id: int, reason: str = "") -> dict[str, Any]:
    authorize(p, "transfer")
    return transfer.remove_item(item_id, p.actor, reason[:200])  # type: ignore[union-attr]


def search_products(p: Principal | None, term: str, limit: int = 50) -> list[dict[str, Any]]:
    authorize(p, "dashboard")
    return catalog.search_products(term, min(max(limit, 1), 5000))


# --------------------------------------------------------------------------- ιστορικό
def history_rows(p: Principal | None, *, date_from: date | None = None, date_to: date | None = None,
                 barcode: str | None = None, customer: str | None = None, result: str | None = None,
                 transfer_status: str | None = None) -> list[dict[str, Any]]:
    authorize(p, "dashboard")
    return history.query(date_from=date_from, date_to=date_to, barcode=barcode, customer=customer,
                         result=result, transfer_status=transfer_status)


def history_export(p: Principal | None, rows: list[dict[str, Any]], fmt: str) -> bytes:
    authorize(p, "dashboard")
    return history.to_csv(rows) if fmt == "csv" else history.to_xlsx(rows)


def audit_log(p: Principal | None) -> list[dict[str, Any]]:
    authorize(p, "admin")
    return history.audit_log()


# --------------------------------------------------------------------------- admin
def stage_excel(p: Principal | None, filename: str, data: bytes) -> dict[str, Any]:
    authorize(p, "admin")
    return catalog.stage_upload(filename, data, p.actor)  # type: ignore[union-attr]


def activate_excel(p: Principal | None, staging_id: str, confirm_replace: bool = False) -> dict[str, Any]:
    authorize(p, "admin")
    return catalog.activate(staging_id, p.actor, confirm_replace)  # type: ignore[union-attr]


def imports(p: Principal | None) -> list[dict[str, Any]]:
    authorize(p, "admin")
    return catalog.list_imports()


def working_excel(p: Principal | None) -> tuple[str, bytes]:
    authorize(p, "admin")
    return catalog.working_bytes()


def make_backup(p: Principal | None) -> str:
    authorize(p, "admin")
    return catalog.make_backup(p.actor)  # type: ignore[union-attr]


def backups(p: Principal | None) -> list[dict[str, Any]]:
    authorize(p, "admin")
    return catalog.list_backups()


def backup_bytes(p: Principal | None, path: str) -> bytes:
    authorize(p, "admin")
    return catalog.get_object(path)


def get_settings_values(p: Principal | None) -> dict[str, Any]:
    authorize(p, "admin")
    return settings_service.get_all()


def update_settings(p: Principal | None, values: dict[str, Any]) -> dict[str, Any]:
    authorize(p, "admin")
    try:
        return settings_service.update(values, p.actor)  # type: ignore[union-attr]
    except ValueError as exc:
        raise ServiceError(str(exc)) from exc


def diagnose_database(p: Principal | None) -> Any:
    """Διάγνωση σύνδεσης βάσης με ανεξάρτητη σύνδεση (χωρίς μυστικά στο αποτέλεσμα)."""
    authorize(p, "admin")
    from app.db.diagnostics import diagnose
    s = get_settings()
    if not s.database_url:
        from app.db.diagnostics import Diagnosis
        return Diagnosis("ok", "Τοπική SQLite (μόνο για δοκιμές)")
    return diagnose(s.database_url)


def health() -> dict[str, Any]:
    """Διαγνωστικά για τη σελίδα Ρυθμίσεων (χωρίς μυστικά)."""
    s = get_settings()
    out: dict[str, Any] = {"database": "postgres" if s.uses_postgres else "sqlite (μόνο τοπικά)",
                           "problems": s.problems()}
    try:
        from app.db.database import connect
        with connect() as conn:
            if conn.kind == "pg":
                row = conn.execute("SELECT current_setting('server_version') AS v").fetchone()
                out["database"] = f"PostgreSQL {row['v']} (Supabase) — σύνδεση OK"
    except Exception as exc:  # pragma: no cover
        from app.db.diagnostics import DatabaseConnectionError
        reason = exc.diagnosis.title if isinstance(exc, DatabaseConnectionError) else type(exc).__name__
        out["problems"].append(f"Η βάση δεν απαντά: {reason}")
    from app.storage import SupabaseObjectStore, get_store
    store = get_store()
    out["storage"] = f"Supabase Storage, bucket «{store.bucket}»" if isinstance(store, SupabaseObjectStore) \
        else "τοπικός φάκελος (μόνο για δοκιμές)"
    if isinstance(store, SupabaseObjectStore):
        problem = store.check_bucket()
        if problem:
            out["problems"].append(problem)
    return out
