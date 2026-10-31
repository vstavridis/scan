"""Κοινά fixtures.

Κάθε test τρέχει σε δικό του προσωρινό φάκελο δεδομένων και σε ΑΝΤΙΓΡΑΦΟ του workbook.
Το πραγματικό αρχείο (αν υπάρχει) δεν τροποποιείται ποτέ — ελέγχεται με sha256.

Πραγματικό αρχείο: ορίστε WAREHOUSE_REAL_XLSX=/path/to/Σχισμενα (1).xlsx ή τοποθετήστε το
στο tests/fixtures/local/ (ο φάκελος αγνοείται από το git).
"""
from __future__ import annotations

import hashlib
import os
import shutil
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Iterator

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.fixtures.make_fixture import build  # noqa: E402

PASSWORDS = {"admin": "admin-pw", "secretary": "sec-pw", "scanner": "scan-pw"}


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


def configure_env(data_dir: Path) -> None:
    os.environ["APP_DATA_DIR"] = str(data_dir)
    os.environ["APP_ADMIN_PASSWORD"] = PASSWORDS["admin"]
    os.environ["APP_SECRETARY_PASSWORD"] = PASSWORDS["secretary"]
    os.environ["APP_SCANNER_PASSWORD"] = PASSWORDS["scanner"]
    os.environ["APP_SECRET_KEY"] = "test-secret-key"
    from app import config
    from app.db import database
    from app.excel import storage

    config.reset_settings()
    database.reset_init_cache()
    storage.set_store(None)


@pytest.fixture
def app_env(tmp_path: Path) -> Iterator[Path]:
    data_dir = tmp_path / "data"
    configure_env(data_dir)
    yield data_dir
    from app.excel import storage
    storage.set_store(None)


@pytest.fixture
def client(app_env):
    from fastapi.testclient import TestClient

    from app.api.main import create_app

    with TestClient(create_app()) as c:
        yield c


def login(client, role: str) -> dict[str, str]:
    r = client.post("/api/auth/login", json={"role": role, "password": PASSWORDS[role], "name": f"test-{role}"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def activate_workbook(client, path: Path, confirm: bool = False) -> dict:
    h = login(client, "admin")
    r = client.post("/api/admin/imports/stage", params={"filename": path.name}, content=path.read_bytes(), headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["report"]["ok"], r.json()["report"]["errors"]
    r = client.post("/api/admin/imports/activate", json={"staging_id": r.json()["staging_id"], "confirm_replace": confirm},
                    headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def scan(client, headers, *barcodes: str, ids: list[str] | None = None) -> list[dict]:
    import uuid
    events = [{"event_id": (ids[i] if ids else uuid.uuid4().hex), "barcode": b} for i, b in enumerate(barcodes)]
    r = client.post("/api/scans", json={"device_id": "pytest", "events": events}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["results"]


# --------------------------------------------------------------------------- live server
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LiveServer:
    """Πραγματικός uvicorn server σε thread (για ταυτόχρονα requests και restart)."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.server = None
        self.thread = None

    def start(self) -> "LiveServer":
        import uvicorn

        configure_env(self.data_dir)
        from app.api.main import create_app

        config = uvicorn.Config(create_app(), host="127.0.0.1", port=self.port, log_level="warning", workers=1)
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        import httpx
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                if httpx.get(self.url + "/api/health", timeout=1).status_code == 200:
                    return self
            except httpx.HTTPError:
                time.sleep(0.1)
        raise RuntimeError("Ο server δεν ξεκίνησε")

    def stop(self) -> None:
        if self.server is not None:
            self.server.should_exit = True
            self.thread.join(timeout=20)
            self.server = None


@pytest.fixture
def live_server(tmp_path: Path) -> Iterator[LiveServer]:
    srv = LiveServer(tmp_path / "data").start()
    yield srv
    srv.stop()
