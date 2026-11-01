"""Κοινά fixtures.

Κάθε test τρέχει σε δική του βάση, δικό του object storage (τοπικός φάκελος) και σε ΑΝΤΙΓΡΑΦΟ
του workbook. Το πραγματικό αρχείο (αν υπάρχει) δεν τροποποιείται ποτέ — ελέγχεται με sha256.

Βάσεις: τα tests του backend τρέχουν σε SQLite ΚΑΙ σε PostgreSQL όταν οριστεί
WAREHOUSE_TEST_PG=postgresql://user@host:port/postgres (δημιουργείται προσωρινή βάση ανά test).

Πραγματικό αρχείο: ορίστε WAREHOUSE_REAL_XLSX=/path/to/Σχισμενα (1).xlsx ή τοποθετήστε το
στο tests/fixtures/local/ (ο φάκελος αγνοείται από το git).
"""
from __future__ import annotations

import hashlib
import os
import shutil
import socket
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.auth import Principal  # noqa: E402
from tests.fixtures.make_fixture import build  # noqa: E402



def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def find_real_workbook() -> Path | None:
    env = os.environ.get("WAREHOUSE_REAL_XLSX")
    if env and Path(env).is_file():
        return Path(env)
    local = ROOT / "tests" / "fixtures" / "local"
    if local.is_dir():
        files = sorted(local.glob("*.xlsx"))
        if files:
            return files[0]
    return None


@pytest.fixture(scope="session")
def synthetic_master(tmp_path_factory) -> Path:
    return build(tmp_path_factory.mktemp("fixture") / "synthetic.xlsx")


@pytest.fixture(scope="session")
def real_master() -> Path:
    p = find_real_workbook()
    if p is None:
        pytest.skip("Δεν βρέθηκε το πραγματικό αρχείο (WAREHOUSE_REAL_XLSX ή tests/fixtures/local/).")
    return p


@pytest.fixture(params=["synthetic", "real"])
def master_workbook(request) -> Path:
    if request.param == "real":
        return request.getfixturevalue("real_master")
    return request.getfixturevalue("synthetic_master")


@pytest.fixture
def workbook_copy(master_workbook: Path, tmp_path: Path) -> Iterator[Path]:
    """Αντίγραφο σε προσωρινό φάκελο· επαληθεύει ότι το αρχικό δεν άλλαξε."""
    before = sha256(master_workbook)
    dst = tmp_path / "input" / master_workbook.name
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(master_workbook, dst)
    yield dst
    assert sha256(master_workbook) == before, "Το αρχικό αρχείο δοκιμής τροποποιήθηκε!"


def pg_admin_url() -> str | None:
    return os.environ.get("WAREHOUSE_TEST_PG")


def _reset_app_state() -> None:
    from app import backend, config, storage
    from app.db import database

    database.close_pool()
    config.reset_settings()
    database.reset_init_cache()
    storage.set_store(None)
    backend._started = False


def configure_env(data_dir: Path, db_url: str | None = None) -> dict[str, str]:
    """Ορίζει env για την εφαρμογή (βάση + storage + κωδικοί) και επαναφέρει τις caches."""
    data_dir.mkdir(parents=True, exist_ok=True)
    env = {
        "APP_DEV_STORAGE_DIR": str(data_dir / "storage"),
        "APP_CACHE_DIR": str(data_dir / "cache"),
        "APP_SUPABASE_BUCKET": "warehouse-excel",
    }
    if db_url:
        env["APP_DATABASE_URL"] = db_url
        os.environ.pop("APP_DEV_SQLITE", None)
    else:
        env["APP_DEV_SQLITE"] = str(data_dir / "warehouse.sqlite3")
        os.environ.pop("APP_DATABASE_URL", None)
    os.environ.update(env)
    _reset_app_state()
    return env


@contextmanager
def temp_pg_database() -> Iterator[str]:
    import psycopg

    admin = pg_admin_url()
    name = "wh_test_" + uuid.uuid4().hex[:12]
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}"')
    base, _, _db = admin.rpartition("/")
    try:
        yield f"{base}/{name}"
    finally:
        _reset_app_state()
        with psycopg.connect(admin, autocommit=True) as c:
            c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _db_params() -> list[str]:
    return ["sqlite", "postgres"] if pg_admin_url() else ["sqlite"]


@pytest.fixture(params=_db_params())
def app_env(request, tmp_path: Path) -> Iterator[Path]:
    data_dir = tmp_path / "data"
    if request.param == "postgres":
        with temp_pg_database() as url:
            configure_env(data_dir, url)
            yield data_dir
    else:
        configure_env(data_dir)
        yield data_dir
    _reset_app_state()


def login(role: str) -> Principal:
    from app import backend
    return backend.identity(f"test-{role}")   # χωρίς κωδικούς: μόνο ταυτότητα για το audit


def activate_workbook(path: Path, confirm: bool = False) -> dict:
    from app import backend
    admin = login("admin")
    staged = backend.stage_excel(admin, path.name, path.read_bytes())
    assert staged["report"]["ok"], staged["report"]["errors"]
    return backend.activate_excel(admin, staged["staging_id"], confirm)


def scan(p: Principal, *barcodes: str, ids: list[str] | None = None) -> list[dict]:
    from app import backend
    events = [{"event_id": (ids[i] if ids else uuid.uuid4().hex), "barcode": b} for i, b in enumerate(barcodes)]
    return backend.submit_scans(p, events, device_id="pytest")["results"]


def working_file(tmp_path: Path) -> Path:
    """Κατεβάζει την τρέχουσα έκδοση του working Excel από το storage."""
    from app import backend
    _name, data = backend.working_excel(login("admin"))
    out = tmp_path / f"working-{uuid.uuid4().hex[:6]}.xlsx"
    out.write_bytes(data)
    return out


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
