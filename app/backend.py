"""Backend της εφαρμογής: το μοναδικό σημείο από όπου το UI (Streamlit) αλλάζει δεδομένα.

Κάθε συνάρτηση δέχεται τον συνδεδεμένο χρήστη (Principal) και ελέγχει δικαιώματα ΕΔΩ, στην
Python, ανεξάρτητα από το ποια κουμπιά εμφανίζονται. Αντικαθιστά τα παλιά FastAPI endpoints.
"""
from __future__ import annotations

import logging
import time
from datetime import date
from typing import Any

from app.auth import ROLE_LABELS, Principal, authorize, check_password, issue_token, verify_token
from app.config import get_settings
from app.db.database import init_db
from app.logging_setup import setup_logging
from app.services import aggregation, catalog, history, scans, settings_service, transfer
from app.services.errors import ServiceError

log = logging.getLogger(__name__)

_started = False


def startup() -> None:
    """Μία φορά ανά διεργασία: σχήμα βάσης + recovery μεταφορών που διακόπηκαν."""
    global _started
    if _started:
        return
    setup_logging()
    init_db()
    recovered = transfer.recover_pending()
    if recovered:
        log.warning("Transfer recovery: %s", recovered)
    _started = True


# --------------------------------------------------------------------------- σύνδεση
def login(role: str, password: str, name: str = "") -> tuple[Principal, str]:
    if role not in ROLE_LABELS:
        raise ServiceError("Άγνωστος ρόλος.")
    if not check_password(role, password):
        time.sleep(0.7)  # επιβράδυνση δοκιμών κωδικού
        raise ServiceError("Λάθος κωδικός ή ο ρόλος δεν έχει ρυθμιστεί.", status_code=401, code="unauthorized")
    name = (name or "").strip()[:60] or ROLE_LABELS[role]
    token, _exp = issue_token(role, name)
    return Principal(role=role, name=name), token


def restore(token: str | None) -> Principal | None:
    """Επαναφορά σύνδεσης από υπογεγραμμένο token (π.χ. μετά από ανανέωση σελίδας)."""
    return verify_token(token) if token else None


def public_config() -> dict[str, Any]:
    cfg = settings_service.get_all()
    imp = catalog.active_import()
    return {
        "terminator": cfg["scanner_terminator"],
        "key_mapping": cfg["scanner_key_mapping"],
        "sound_default": cfg["scanner_sound_default"],
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


def candidates(p: Principal | None, item_id: int) -> list[dict[str, Any]]:
    authorize(p, "dashboard")
    return aggregation.candidates_for(item_id)


def transfer_item(p: Principal | None, item_id: int) -> dict[str, Any]:
    authorize(p, "transfer")
    return transfer.transfer_item(item_id, p.actor)  # type: ignore[union-attr]


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


def health() -> dict[str, Any]:
    """Διαγνωστικά για τη σελίδα Ρυθμίσεων (χωρίς μυστικά)."""
    s = get_settings()
    out: dict[str, Any] = {"database": "postgres" if s.uses_postgres else "sqlite (μόνο τοπικά)",
                           "problems": s.problems()}
    from app.storage import SupabaseObjectStore, get_store
    store = get_store()
    if isinstance(store, SupabaseObjectStore):
        problem = store.check_bucket()
        if problem:
            out["problems"].append(problem)
    return out
