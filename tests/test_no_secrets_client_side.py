"""Κανένα μυστικό δεν φτάνει στον browser: ούτε στο JavaScript/HTML ούτε στα δεδομένα του component."""
from __future__ import annotations

import json
import re
import subprocess

from tests.conftest import ROOT

ASSETS = ROOT / "app" / "ui" / "components" / "assets"


def test_component_assets_have_no_credentials():
    for f in ASSETS.iterdir():
        text = f.read_text(encoding="utf-8").lower()
        for needle in ("supabase", "service_key", "sb_secret", "postgresql://", "apikey", "password", "secret_key"):
            assert needle not in text, f"{needle} στο {f.name}"


def test_scanner_component_data_contains_only_display_data(app_env, synthetic_master, monkeypatch):
    """Ό,τι στέλνει η Python στο JS: λίστα, αποτελέσματα, ρυθμίσεις οθόνης — χωρίς Secrets."""
    from app import backend
    from app.config import get_settings
    from tests.conftest import activate_workbook, login, scan

    activate_workbook(synthetic_master)
    scan(login("scanner"), "VS11486")
    view = backend.scanner_view(login("scanner"))
    cfg = backend.public_config()
    blob = json.dumps({"view": view, "cfg": cfg}, ensure_ascii=False)
    s = get_settings()
    for secret in [s.database_url or "", s.supabase_service_key or ""]:
        if secret:
            assert secret not in blob
    assert "postgres" not in blob.lower() and "sb_secret" not in blob
    assert set(cfg) == {"terminator", "key_mapping", "sound_default", "auto_fullscreen", "inter_key_timeout_ms",
                        "dashboard_refresh_seconds", "scanner_refresh_seconds", "has_catalog", "catalog_name"}


def test_no_secrets_committed():
    files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    assert ".streamlit/secrets.toml" not in files and ".env" not in files
    pattern = re.compile(r"sb_secret_[A-Za-z0-9_\-]{20,}|eyJhbGciOi[A-Za-z0-9_\-]{30,}\.[A-Za-z0-9_\-]{30,}")
    for f in files:
        p = ROOT / f
        if p.suffix in {".py", ".js", ".toml", ".md", ".sql", ".html", ".css", ".txt", ".example"} and p.is_file():
            assert not pattern.search(p.read_text(encoding="utf-8", errors="ignore")), f
