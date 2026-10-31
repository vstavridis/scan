"""Κεντρικές ρυθμίσεις εφαρμογής.

Όλες οι ρυθμίσεις διαβάζονται από environment variables ή από αρχείο `.env`
στη ρίζα του project (που ΔΕΝ ανεβαίνει στο GitHub).
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Ακριβή ονόματα φύλλων όπως υπάρχουν στο πραγματικό workbook.
# Προσοχή: το πρώτο ξεκινά με U+0384 (GREEK TONOS) και το δεύτερο έχει κενό στο τέλος.
DEFAULT_SOURCE_SHEET = "΄Ετοιμα προϊόντα"
DEFAULT_TARGET_SHEET = "Σβησμενα "


def _load_dotenv(path: Path) -> None:
    """Απλός loader για `.env` (KEY=VALUE). Δεν αντικαθιστά υπάρχουσες μεταβλητές."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


@dataclass
class Settings:
    data_dir: Path
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    ui_port: int = 8501
    api_internal_url: str = "http://127.0.0.1:8000"
    timezone: str = "Europe/Athens"
    secret_key: str = ""
    passwords: dict[str, str] = field(default_factory=dict)
    scanner_token_hours: int = 24 * 30
    user_token_hours: int = 12
    source_sheet: str = DEFAULT_SOURCE_SHEET
    target_sheet: str = DEFAULT_TARGET_SHEET
    log_level: str = "INFO"

    # --- παράγωγα paths -------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.data_dir / "warehouse.sqlite3"

    @property
    def imports_dir(self) -> Path:
        return self.data_dir / "imports"

    @property
    def staging_dir(self) -> Path:
        return self.data_dir / "staging"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.imports_dir, self.staging_dir, self.backups_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)


def _resolve_secret(data_dir: Path) -> str:
    key = _env("APP_SECRET_KEY")
    if key:
        return key
    # Αν δεν δόθηκε, δημιουργείται μία φορά και φυλάσσεται τοπικά στον φάκελο δεδομένων.
    data_dir.mkdir(parents=True, exist_ok=True)
    key_file = data_dir / "secret.key"
    if key_file.is_file():
        return key_file.read_text(encoding="utf-8").strip()
    key = secrets.token_hex(32)
    key_file.write_text(key, encoding="utf-8")
    return key


def load_settings() -> Settings:
    _load_dotenv(PROJECT_ROOT / ".env")
    data_dir = Path(_env("APP_DATA_DIR", str(PROJECT_ROOT / "data")))  # type: ignore[arg-type]
    if not data_dir.is_absolute():
        data_dir = (PROJECT_ROOT / data_dir).resolve()
    api_port = int(_env("APP_API_PORT", "8000"))  # type: ignore[arg-type]
    passwords = {
        role: pw
        for role, pw in (
            ("admin", _env("APP_ADMIN_PASSWORD")),
            ("secretary", _env("APP_SECRETARY_PASSWORD")),
            ("scanner", _env("APP_SCANNER_PASSWORD")),
        )
        if pw
    }
    s = Settings(
        data_dir=data_dir,
        api_host=_env("APP_API_HOST", "0.0.0.0"),  # type: ignore[arg-type]
        api_port=api_port,
        ui_port=int(_env("APP_UI_PORT", "8501")),  # type: ignore[arg-type]
        api_internal_url=_env("APP_API_INTERNAL_URL", f"http://127.0.0.1:{api_port}"),  # type: ignore[arg-type]
        timezone=_env("APP_TIMEZONE", "Europe/Athens"),  # type: ignore[arg-type]
        secret_key=_resolve_secret(data_dir),
        passwords=passwords,
        scanner_token_hours=int(_env("APP_SCANNER_TOKEN_HOURS", str(24 * 30))),  # type: ignore[arg-type]
        user_token_hours=int(_env("APP_USER_TOKEN_HOURS", "12")),  # type: ignore[arg-type]
        source_sheet=_env("APP_SOURCE_SHEET", DEFAULT_SOURCE_SHEET),  # type: ignore[arg-type]
        target_sheet=_env("APP_TARGET_SHEET", DEFAULT_TARGET_SHEET),  # type: ignore[arg-type]
        log_level=_env("APP_LOG_LEVEL", "INFO"),  # type: ignore[arg-type]
    )
    s.ensure_dirs()
    return s


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
