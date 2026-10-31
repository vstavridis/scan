"""Ρυθμίσεις εφαρμογής που αλλάζει ο Admin (αποθηκεύονται στη βάση)."""
from __future__ import annotations

import json
from typing import Any

from app.db.database import bump_version, connect, transaction
from app.services.audit import audit

DEFAULTS: dict[str, Any] = {
    # Τερματικός χαρακτήρας scanner: "enter" ή "enter_tab"
    "scanner_terminator": "enter",
    # "physical": χαρτογράφηση με βάση το φυσικό πλήκτρο (λειτουργεί και με ελληνική διάταξη)
    # "layout": ό,τι χαρακτήρα δίνει η τρέχουσα διάταξη πληκτρολογίου
    "scanner_key_mapping": "physical",
    "scanner_sound_default": True,
    # Μέγιστο κενό (ms) μεταξύ πλήκτρων· μετά από αυτό, ο buffer θεωρείται νέα πληκτρολόγηση
    "scanner_inter_key_timeout_ms": 0,
    "dashboard_refresh_seconds": 1.0,
    # "auto": φυσική αφαίρεση όταν είναι ασφαλής, αλλιώς καθαρισμός γραμμής με προειδοποίηση
    "transfer_mode": "auto",
}

ALLOWED: dict[str, Any] = {
    "scanner_terminator": {"enter", "enter_tab"},
    "scanner_key_mapping": {"physical", "layout"},
    "transfer_mode": {"auto", "shift", "clear"},
}


def get_all() -> dict[str, Any]:
    out = dict(DEFAULTS)
    with connect() as conn:
        for row in conn.execute("SELECT key, value FROM app_settings WHERE key LIKE 'cfg.%'"):
            key = row["key"][4:]
            if key in DEFAULTS:
                out[key] = json.loads(row["value"])
    return out


def get(key: str) -> Any:
    return get_all()[key]


def validate(values: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in values.items():
        if key not in DEFAULTS:
            raise ValueError(f"Άγνωστη ρύθμιση: {key}")
        if key in ALLOWED and value not in ALLOWED[key]:
            raise ValueError(f"Μη έγκυρη τιμή για {key}: {value}")
        default = DEFAULTS[key]
        if isinstance(default, bool):
            value = bool(value)
        elif isinstance(default, int):
            value = int(value)
            if value < 0:
                raise ValueError(f"Μη έγκυρη τιμή για {key}")
        elif isinstance(default, float):
            value = float(value)
            if not 0.2 <= value <= 60:
                raise ValueError("Ο χρόνος ανανέωσης πρέπει να είναι 0.2–60 δευτερόλεπτα")
        clean[key] = value
    return clean


def update(values: dict[str, Any], actor: str) -> dict[str, Any]:
    clean = validate(values)
    with transaction() as conn:
        for key, value in clean.items():
            conn.execute(
                "INSERT INTO app_settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (f"cfg.{key}", json.dumps(value)),
            )
        audit(conn, actor, "settings.update", clean)
        bump_version(conn)
    return get_all()
