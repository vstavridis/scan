"""Κεντρικές ρυθμίσεις εφαρμογής.

Παραγωγή (Streamlit Community Cloud): όλα από τα **Streamlit Secrets** (`st.secrets`):

    [database]   url            -> Supabase PostgreSQL (connection string, pooler)
    [supabase]   url, service_key, bucket  -> Supabase Storage (private bucket)

Πρόσβαση χρηστών: Private app στο Streamlit Cloud (μόνο προσκεκλημένοι) — χωρίς εσωτερικό login.

Τοπική ανάπτυξη / tests: environment variables (APP_*). Με `APP_DEV_SQLITE` και
`APP_DEV_STORAGE_DIR` η εφαρμογή χρησιμοποιεί τοπικό SQLite και φάκελο αντί για Supabase.
Το SQLite ΔΕΝ επιτρέπεται στο Streamlit Cloud (προσωρινό filesystem).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Ακριβή ονόματα φύλλων όπως υπάρχουν στο πραγματικό workbook.
# Προσοχή: το πρώτο ξεκινά με U+0384 (GREEK TONOS) και το δεύτερο έχει κενό στο τέλος.
DEFAULT_SOURCE_SHEET = "΄Ετοιμα προϊόντα"
DEFAULT_TARGET_SHEET = "Σβησμενα "


class ConfigError(Exception):
    """Λείπουν ή είναι λάθος οι ρυθμίσεις (Secrets)."""


def _secrets() -> dict[str, Any]:
    """Διαβάζει τα Streamlit Secrets, αν υπάρχουν (χωρίς να αποτυγχάνει εκτός Streamlit)."""
    try:
        import streamlit as st

        try:
            return {k: (dict(v) if hasattr(v, "keys") else v) for k, v in st.secrets.items()}
        except Exception:  # δεν υπάρχει secrets.toml
            return {}
    except ImportError:  # pragma: no cover
        return {}


def _get(secrets: dict[str, Any], section: str, key: str, env: str, default: Any = None) -> Any:
    value = os.environ.get(env)
    if value not in (None, ""):
        return value
    sec = secrets.get(section)
    if isinstance(sec, dict) and sec.get(key) not in (None, ""):
        return sec[key]
    return default


@dataclass
class Settings:
    # βάση
    database_url: str | None = None
    dev_sqlite_path: Path | None = None
    # αποθήκευση αρχείων
    supabase_url: str | None = None
    supabase_service_key: str | None = None
    supabase_bucket: str | None = None
    dev_storage_dir: Path | None = None
    onedrive_client_id: str | None = None
    onedrive_client_secret: str | None = None
    onedrive_redirect_uri: str | None = None
    # λοιπά
    timezone: str = "Europe/Athens"
    source_sheet: str = DEFAULT_SOURCE_SHEET
    target_sheet: str = DEFAULT_TARGET_SHEET
    cache_dir: Path = Path(os.environ.get("APP_CACHE_DIR", "/tmp/warehouse-cache"))
    log_level: str = "INFO"

    @property
    def uses_postgres(self) -> bool:
        return bool(self.database_url)

    def problems(self) -> list[str]:
        """Λίστα προβλημάτων ρύθμισης (κενή = όλα εντάξει)."""
        out = []
        if not self.database_url and not self.dev_sqlite_path:
            out.append("Λείπει το `[database] url` (Supabase PostgreSQL connection string) από τα Secrets.")
        if not (self.supabase_url and self.supabase_service_key) and not self.dev_storage_dir:
            out.append("Λείπουν τα `[supabase] url` και `service_key` από τα Secrets (Supabase Storage).")
        if not self.dev_storage_dir and not self.supabase_bucket:
            out.append("Λείπει το `[supabase] bucket` από τα Secrets.")
        return out


def load_settings() -> Settings:
    sec = _secrets()
    sqlite = _get(sec, "dev", "sqlite_path", "APP_DEV_SQLITE")
    storage_dir = _get(sec, "dev", "storage_dir", "APP_DEV_STORAGE_DIR")
    return Settings(
        database_url=_get(sec, "database", "url", "APP_DATABASE_URL"),
        dev_sqlite_path=Path(sqlite) if sqlite else None,
        supabase_url=(_get(sec, "supabase", "url", "APP_SUPABASE_URL") or None),
        supabase_service_key=_get(sec, "supabase", "service_key", "APP_SUPABASE_SERVICE_KEY"),
        supabase_bucket=_get(sec, "supabase", "bucket", "APP_SUPABASE_BUCKET"),
        dev_storage_dir=Path(storage_dir) if storage_dir else None,
        onedrive_client_id=_get(sec, "onedrive", "client_id", "APP_ONEDRIVE_CLIENT_ID"),
        onedrive_client_secret=_get(sec, "onedrive", "client_secret", "APP_ONEDRIVE_CLIENT_SECRET"),
        onedrive_redirect_uri=_get(sec, "onedrive", "redirect_uri", "APP_ONEDRIVE_REDIRECT_URI"),
        timezone=_get(sec, "app", "timezone", "APP_TIMEZONE", "Europe/Athens"),
        source_sheet=_get(sec, "app", "source_sheet", "APP_SOURCE_SHEET", DEFAULT_SOURCE_SHEET),
        target_sheet=_get(sec, "app", "target_sheet", "APP_TARGET_SHEET", DEFAULT_TARGET_SHEET),
        log_level=_get(sec, "app", "log_level", "APP_LOG_LEVEL", "INFO"),
    )


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def reset_settings() -> None:
    """Χρησιμοποιείται από τα tests για επαναφόρτωση ρυθμίσεων."""
    global _settings
    _settings = None
