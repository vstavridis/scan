"""Σύνδεση SQLite (WAL) και βοηθητικά για transactions."""
from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.config import get_settings

log = logging.getLogger(__name__)

SCHEMA_VERSION = "1"
_SCHEMA_FILE = Path(__file__).with_name("schema.sql")
_init_lock = threading.Lock()
_initialized: set[str] = set()

# Ένας κοινός lock για όλες τις εγγραφές της διεργασίας. Το SQLite σειριοποιεί
# ούτως ή άλλως τους writers· ο lock απλώς αποφεύγει άσκοπο "database is locked".
write_lock = threading.RLock()


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")  # durability: το ack δίνεται μόνο μετά από fsync
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(path: Path | None = None) -> None:
    path = path or get_settings().db_path
    key = str(path)
    with _init_lock:
        if key in _initialized:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = _connect(path)
        try:
            conn.executescript(_SCHEMA_FILE.read_text(encoding="utf-8"))
            conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO NOTHING",
                (SCHEMA_VERSION,),
            )
            conn.execute("INSERT INTO app_settings(key, value) VALUES('data_version', '0') ON CONFLICT(key) DO NOTHING")
        finally:
            conn.close()
        _initialized.add(key)
        log.info("Database ready at %s", path)


def reset_init_cache() -> None:
    _initialized.clear()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Σύνδεση για ανάγνωση (ή αυτόνομες εντολές)."""
    init_db()
    conn = _connect(get_settings().db_path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT. Σε exception γίνεται ROLLBACK."""
    init_db()
    with write_lock:
        conn = _connect(get_settings().db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")
        finally:
            conn.close()


def bump_version(conn: sqlite3.Connection) -> int:
    """Αυξάνει τον μετρητή αλλαγών (χρησιμοποιείται για φθηνό polling από τα UI)."""
    conn.execute("UPDATE app_settings SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key='data_version'")
    row = conn.execute("SELECT value FROM app_settings WHERE key='data_version'").fetchone()
    return int(row["value"])


def get_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM app_settings WHERE key='data_version'").fetchone()
    return int(row["value"]) if row else 0
