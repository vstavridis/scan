from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.timeutil import utcnow_iso


def audit(conn: sqlite3.Connection, actor: str, action: str, details: dict[str, Any] | None = None) -> None:
    conn.execute(
        "INSERT INTO audit_log(ts, actor, action, details) VALUES (?, ?, ?, ?)",
        (utcnow_iso(), actor, action, json.dumps(details or {}, ensure_ascii=False, default=str)),
    )
