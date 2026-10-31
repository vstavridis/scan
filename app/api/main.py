"""FastAPI backend: λήψη barcode, ενεργή λίστα, μεταφορές, ιστορικό, διαχείριση."""
from __future__ import annotations

import logging
import re
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import timeutil
from app.api.auth import ROLE_LABELS, Principal, check_password, issue_token, require
from app.config import get_settings
from app.db.database import connect, get_version, init_db, transaction
from app.excel.storage import excel_write_lock, get_store
from app.logging_setup import setup_logging
from app.services import aggregation, catalog, history, scans, settings_service, transfer
from app.services.audit import audit
from app.services.errors import NotFound, ServiceError

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent / "web" / "scanner"


# --------------------------------------------------------------------------- models
class LoginIn(BaseModel):
    role: str
    password: str
    name: str = Field(default="", max_length=60)


class ScanEventIn(BaseModel):
    event_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9\-_]+$")
    barcode: str = Field(max_length=512)
    client_ts: str | None = Field(default=None, max_length=40)
    seq: int | None = None


class ScanBatchIn(BaseModel):
    device_id: str | None = Field(default=None, max_length=64)
    events: list[ScanEventIn] = Field(max_length=500)


class ResolveIn(BaseModel):
    product_record_id: int


class RemoveIn(BaseModel):
    reason: str = Field(default="", max_length=200)


class ActivateIn(BaseModel):
    staging_id: str
    confirm_replace: bool = False


# --------------------------------------------------------------------------- app
@asynccontextmanager
async def lifespan(_app: FastAPI):
    setup_logging("api")
    init_db()
    recovered = transfer.recover_pending()
    if recovered:
        log.warning("Transfer recovery: %s", recovered)
    catalog.cleanup_staging()
    log.info("API ready")
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Αποθήκη Barcode API", version="1.0", lifespan=lifespan)

    @app.exception_handler(ServiceError)
    async def service_error_handler(_req: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code,
                            content={"detail": exc.message, "code": exc.code, "extra": exc.extra})

    @app.middleware("http")
    async def no_cache_api(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/") or request.url.path.startswith("/scanner"):
            response.headers["Cache-Control"] = "no-store"
        return response

    # ---- public ------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/scanner/")

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        with connect() as conn:
            version = get_version(conn)
        return {"ok": True, "version": version, "time": timeutil.utcnow_iso()}

    @app.get("/api/config")
    def public_config() -> dict[str, Any]:
        cfg = settings_service.get_all()
        imp = catalog.active_import()
        return {
            "terminator": cfg["scanner_terminator"],
            "key_mapping": cfg["scanner_key_mapping"],
            "sound_default": cfg["scanner_sound_default"],
            "inter_key_timeout_ms": cfg["scanner_inter_key_timeout_ms"],
            "dashboard_refresh_seconds": cfg["dashboard_refresh_seconds"],
            "has_catalog": imp is not None,
            "catalog_name": imp["original_filename"] if imp else None,
            "roles_configured": sorted(get_settings().passwords),
        }

    @app.post("/api/auth/login")
    def login(body: LoginIn) -> dict[str, Any]:
        if body.role not in ROLE_LABELS:
            raise HTTPException(400, "Άγνωστος ρόλος.")
        if not check_password(body.role, body.password):
            time.sleep(0.7)  # επιβράδυνση δοκιμών κωδικού
            raise HTTPException(401, "Λάθος κωδικός ή ο ρόλος δεν έχει ρυθμιστεί.")
        name = body.name.strip() or ROLE_LABELS[body.role]
        token, exp = issue_token(body.role, name)
        return {"token": token, "role": body.role, "name": name, "expires": exp}

    @app.get("/api/auth/me")
    def me(p: Principal = Depends(require("view"))) -> dict[str, Any]:
        return {"role": p.role, "name": p.name}

    # ---- scans -------------------------------------------------------------
    @app.post("/api/scans")
    def post_scans(body: ScanBatchIn, p: Principal = Depends(require("scan"))) -> dict[str, Any]:
        events = [scans.ScanIn(e.event_id, e.barcode, e.client_ts, e.seq) for e in body.events]
        return scans.process_batch(events, device_id=body.device_id or p.name)

    @app.get("/api/active")
    def get_active(since: int | None = Query(default=None), _p: Principal = Depends(require("view"))) -> dict[str, Any]:
        if since is not None:
            with connect() as conn:
                v = get_version(conn)
            if v == since:
                return {"version": v, "changed": False}
        snap = aggregation.scanner_snapshot()
        snap["changed"] = True
        return snap

    # ---- dashboard -----------------------------------------------------------
    @app.get("/api/dashboard")
    def dashboard(_p: Principal = Depends(require("dashboard"))) -> dict[str, Any]:
        return aggregation.dashboard_snapshot()

    @app.get("/api/version")
    def version(_p: Principal = Depends(require("view"))) -> dict[str, Any]:
        with connect() as conn:
            return {"version": get_version(conn)}

    @app.get("/api/items/{item_id}/candidates")
    def candidates(item_id: int, _p: Principal = Depends(require("dashboard"))) -> list[dict[str, Any]]:
        return aggregation.candidates_for(item_id)

    @app.post("/api/items/{item_id}/transfer")
    def do_transfer(item_id: int, p: Principal = Depends(require("transfer"))) -> dict[str, Any]:
        return transfer.transfer_item(item_id, p.actor)

    @app.post("/api/items/{item_id}/resolve")
    def do_resolve(item_id: int, body: ResolveIn, p: Principal = Depends(require("transfer"))) -> dict[str, Any]:
        return transfer.resolve_ambiguous(item_id, body.product_record_id, p.actor)

    @app.post("/api/items/{item_id}/remove")
    def do_remove(item_id: int, body: RemoveIn, p: Principal = Depends(require("transfer"))) -> dict[str, Any]:
        return transfer.remove_item(item_id, p.actor, body.reason)

    @app.get("/api/products/search")
    def product_search(q: str = "", limit: int = Query(50, ge=1, le=5000),
                       _p: Principal = Depends(require("dashboard"))) -> list[dict[str, Any]]:
        return catalog.search_products(q, limit)

    # ---- history -------------------------------------------------------------
    def _history(date_from, date_to, barcode, customer, result, transfer_status):
        return history.query(date_from=date_from, date_to=date_to, barcode=barcode, customer=customer,
                             result=result, transfer_status=transfer_status)

    @app.get("/api/history")
    def get_history(
        date_from: date | None = None, date_to: date | None = None, barcode: str | None = None,
        customer: str | None = None, result: str | None = None, transfer_status: str | None = None,
        _p: Principal = Depends(require("dashboard")),
    ) -> list[dict[str, Any]]:
        return _history(date_from, date_to, barcode, customer, result, transfer_status)

    @app.get("/api/history/export")
    def export_history(
        fmt: str = Query("csv", pattern="^(csv|xlsx)$"),
        date_from: date | None = None, date_to: date | None = None, barcode: str | None = None,
        customer: str | None = None, result: str | None = None, transfer_status: str | None = None,
        _p: Principal = Depends(require("dashboard")),
    ) -> Response:
        rows = _history(date_from, date_to, barcode, customer, result, transfer_status)
        stamp = timeutil.local_stamp()[:15]
        if fmt == "csv":
            return _download(history.to_csv(rows), f"istoriko_{stamp}.csv", "text/csv; charset=utf-8")
        return _download(history.to_xlsx(rows), f"istoriko_{stamp}.xlsx",
                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @app.get("/api/audit")
    def get_audit(_p: Principal = Depends(require("admin"))) -> list[dict[str, Any]]:
        return history.audit_log()

    # ---- admin ---------------------------------------------------------------
    @app.post("/api/admin/imports/stage")
    async def stage(request: Request, filename: str = Query(..., max_length=255),
                    p: Principal = Depends(require("admin"))) -> dict[str, Any]:
        data = await request.body()
        return catalog.stage_upload(Path(filename).name, data, p.actor)

    @app.post("/api/admin/imports/activate")
    def activate(body: ActivateIn, p: Principal = Depends(require("admin"))) -> dict[str, Any]:
        return catalog.activate(body.staging_id, p.actor, body.confirm_replace)

    @app.get("/api/admin/imports")
    def imports(_p: Principal = Depends(require("admin"))) -> list[dict[str, Any]]:
        return catalog.list_imports()

    @app.get("/api/admin/settings")
    def get_settings_ep(_p: Principal = Depends(require("admin"))) -> dict[str, Any]:
        return settings_service.get_all()

    @app.put("/api/admin/settings")
    def put_settings(values: dict[str, Any], p: Principal = Depends(require("admin"))) -> dict[str, Any]:
        try:
            return settings_service.update(values, p.actor)
        except ValueError as exc:
            raise ServiceError(str(exc)) from exc

    @app.get("/api/admin/download/working")
    def download_working(_p: Principal = Depends(require("admin"))) -> Response:
        imp = catalog.active_import()
        if imp is None:
            raise NotFound("Δεν υπάρχει ενεργό Excel.")
        with excel_write_lock:
            data = Path(imp["working_path"]).read_bytes()
        stem = Path(imp["original_filename"]).stem
        return _download(data, f"{stem}_ενημερωμένο_{timeutil.local_stamp()[:15]}.xlsx",
                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @app.post("/api/admin/backups")
    def make_backup(p: Principal = Depends(require("admin"))) -> dict[str, Any]:
        imp = catalog.active_import()
        if imp is None:
            raise NotFound("Δεν υπάρχει ενεργό Excel.")
        with excel_write_lock:
            path = get_store().backup(Path(imp["working_path"]), "manual")
        with transaction() as conn:
            audit(conn, p.actor, "backup.manual", {"path": str(path)})
        return {"name": path.name}

    @app.get("/api/admin/backups")
    def list_backups(_p: Principal = Depends(require("admin"))) -> list[dict[str, Any]]:
        d = get_settings().backups_dir
        files = sorted(d.glob("*"), key=lambda f: f.stat().st_mtime, reverse=True)
        return [{"name": f.name, "size": f.stat().st_size,
                 "modified": timeutil.fmt_local(datetime.fromtimestamp(f.stat().st_mtime, timezone.utc))}
                for f in files[:300] if f.is_file()]

    @app.get("/api/admin/backups/{name}")
    def get_backup(name: str, _p: Principal = Depends(require("admin"))) -> FileResponse:
        if not re.fullmatch(r"[\w\-. ]+", name):
            raise NotFound("Δεν βρέθηκε.")
        path = get_settings().backups_dir / name
        if not path.is_file():
            raise NotFound("Δεν βρέθηκε.")
        return FileResponse(path, filename=name)

    # ---- scanner web app ---------------------------------------------------
    app.mount("/scanner", StaticFiles(directory=str(WEB_DIR), html=True), name="scanner")
    return app


def _download(data: bytes, filename: str, media_type: str) -> Response:
    ascii_name = re.sub(r"[^A-Za-z0-9_.-]", "_", filename)
    return Response(
        content=data,
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"},
    )


app = create_app()
