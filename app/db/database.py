"""Πρόσβαση στη βάση: Supabase PostgreSQL (παραγωγή) ή SQLite (μόνο τοπικά tests/ανάπτυξη).

Οι services γράφουν SQL με `?` placeholders· ο adapter τα μετατρέπει για το psycopg.
Ό,τι SQL χρησιμοποιείται είναι κοινό υποσύνολο PostgreSQL/SQLite (RETURNING, ON CONFLICT,
partial unique indexes).

Σειριοποίηση εγγραφών:
* PostgreSQL: `pg_advisory_xact_lock` ανά "κανάλι" (π.χ. scans) μέσα στο transaction.
  Λειτουργεί και πίσω από τον Supabase pooler, γιατί είναι transaction-scoped.
* SQLite: `BEGIN IMMEDIATE` + process lock.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
import threading
import zlib
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from app.config import ConfigError, get_settings

log = logging.getLogger(__name__)

SCHEMA_VERSION = "3"
_DIR = Path(__file__).resolve().parent
_init_lock = threading.Lock()
_initialized: set[str] = set()
_sqlite_write_lock = threading.RLock()
_pool = None
_pool_url: str | None = None


# Μόνο για δοκιμές: προσομοίωση καθυστέρησης δικτύου (ms) σε ΚΑΘΕ εντολή SQL.
_SIM_LATENCY = float(os.environ.get("APP_SIMULATE_DB_LATENCY_MS", "0") or 0) / 1000.0


def _rt(n: int = 1) -> None:
    if _SIM_LATENCY:
        time.sleep(_SIM_LATENCY * n)


class IntegrityError(Exception):
    """Παραβίαση constraint (unique κ.λπ.), ανεξάρτητα από τη βάση."""


class Conn:
    """Ενιαίο interface πάνω από sqlite3 / psycopg connections."""

    def __init__(self, raw: Any, kind: str):
        self.raw = raw
        self.kind = kind

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        if _SIM_LATENCY:  # μόνο για δοκιμές: round-trip δικτύου ανά εντολή (π.χ. προς Supabase)
            time.sleep(_SIM_LATENCY)
        try:
            if self.kind == "pg":
                return self.raw.execute(sql.replace("%", "%%").replace("?", "%s"), tuple(params))
            return self.raw.execute(sql, tuple(params))
        except sqlite3.IntegrityError as exc:
            raise IntegrityError(str(exc)) from exc
        except Exception as exc:
            if self.kind == "pg":
                import psycopg

                if isinstance(exc, psycopg.errors.IntegrityError):
                    raise IntegrityError(str(exc)) from exc
            raise

    def executemany(self, sql: str, rows: list[Sequence[Any]]) -> None:
        if _SIM_LATENCY:
            time.sleep(_SIM_LATENCY)
        try:
            if self.kind == "pg":
                with self.raw.cursor() as cur:
                    cur.executemany(sql.replace("%", "%%").replace("?", "%s"), rows)
            else:
                self.raw.executemany(sql, rows)
        except sqlite3.IntegrityError as exc:
            raise IntegrityError(str(exc)) from exc

    def lock(self, channel: str) -> None:
        """Σειριοποίηση κρίσιμων εγγραφών μέσα στο τρέχον transaction."""
        if self.kind == "pg":
            self.execute("SELECT pg_advisory_xact_lock(?)", (zlib.crc32(channel.encode()) & 0x7FFFFFFF,))
        # SQLite: το BEGIN IMMEDIATE έχει ήδη αποκλειστικό write lock


# --------------------------------------------------------------------------- connections
def _sqlite_connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


POOL_MAX_SIZE = 4          # ήπιο προς τον Supabase Session pooler (περιορισμένο pool size στο free plan)
POOL_TIMEOUT_S = 15.0


def _preflight(url: str) -> None:
    """Μία απευθείας σύνδεση πριν από το pool: σε αποτυχία, raise με την ΠΡΑΓΜΑΤΙΚΗ αιτία."""
    import psycopg

    from app.db.diagnostics import DatabaseConnectionError, diagnose

    try:
        with psycopg.connect(url, connect_timeout=10, autocommit=True, prepare_threshold=None,
                             application_name="warehouse-preflight") as c:
            c.execute("SELECT 1")
    except Exception:
        raise DatabaseConnectionError(diagnose(url)) from None


def _pg_pool(url: str):
    global _pool, _pool_url
    if _pool is None or _pool_url != url:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        _preflight(url)
        pool = ConnectionPool(
            url,
            min_size=1,
            max_size=POOL_MAX_SIZE,
            kwargs={"row_factory": dict_row, "autocommit": True, "prepare_threshold": None,
                    "connect_timeout": 10, "application_name": "warehouse-streamlit"},
            check=ConnectionPool.check_connection,
            max_idle=300,           # κλείνει αδρανείς συνδέσεις (λιγότερη πίεση στον pooler)
            reconnect_timeout=60,
            open=False,
            timeout=POOL_TIMEOUT_S,
            name="warehouse",
        )
        pool.open(wait=True, timeout=POOL_TIMEOUT_S)
        _pool, _pool_url = pool, url
    return _pool


def close_pool() -> None:
    global _pool, _pool_url
    if _pool is not None:
        _pool.close()
    _pool = None
    _pool_url = None


@contextmanager
def _raw() -> Iterator[Conn]:
    s = get_settings()
    _rt()  # έλεγχος σύνδεσης του pool
    if s.database_url:
        from psycopg_pool import PoolTimeout

        from app.db.diagnostics import DatabaseConnectionError, diagnose

        try:
            pool = _pg_pool(s.database_url)
        except PoolTimeout:
            close_pool()
            raise DatabaseConnectionError(diagnose(s.database_url)) from None
        try:
            cm = pool.connection()
            c = cm.__enter__()
        except PoolTimeout:
            # Το pool δεν έδωσε σύνδεση: διάγνωση με ανεξάρτητη σύνδεση για την πραγματική αιτία.
            d = diagnose(s.database_url)
            if d.ok:
                stats = pool.get_stats()
                d.category = "exhausted"
                d.summary = (f"Όλες οι συνδέσεις του pool ({stats.get('pool_size', '?')}/{POOL_MAX_SIZE}) είναι "
                             f"απασχολημένες· η βάση απαντά κανονικά σε νέα σύνδεση.")
                from app.db.diagnostics import HINTS
                d.hints = HINTS["exhausted"]
            else:
                close_pool()  # π.χ. άλλαξε ο κωδικός: νέο pool στην επόμενη προσπάθεια
            raise DatabaseConnectionError(d) from None
        try:
            yield Conn(c, "pg")
        except BaseException as exc:
            cm.__exit__(type(exc), exc, exc.__traceback__)
            raise
        else:
            cm.__exit__(None, None, None)
    elif s.dev_sqlite_path:
        s.dev_sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        c = _sqlite_connect(s.dev_sqlite_path)
        try:
            yield Conn(c, "sqlite")
        finally:
            c.close()
    else:
        raise ConfigError("Δεν έχει οριστεί βάση δεδομένων ([database] url στα Secrets).")


def _db_key() -> str:
    s = get_settings()
    return s.database_url or str(s.dev_sqlite_path)


def init_db() -> None:
    key = _db_key()
    with _init_lock:
        if key in _initialized:
            return
        with _raw() as conn:
            if conn.kind == "pg":
                sql = (_DIR / "schema_postgres.sql").read_text(encoding="utf-8")
                with conn.raw.transaction():
                    conn.raw.execute("SELECT pg_advisory_xact_lock(424242)")
                    conn.raw.execute(sql)
            else:
                conn.raw.executescript((_DIR / "schema_sqlite.sql").read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (SCHEMA_VERSION,))
            conn.execute("INSERT INTO app_settings(key, value) VALUES('data_version', '0') ON CONFLICT(key) DO NOTHING")
        _initialized.add(key)
        log.info("Database ready (%s)", "postgres" if get_settings().database_url else "sqlite")


def reset_init_cache() -> None:
    _initialized.clear()


@contextmanager
def connect() -> Iterator[Conn]:
    """Σύνδεση για ανάγνωση (autocommit)."""
    init_db()
    with _raw() as conn:
        yield conn


@contextmanager
def snapshot() -> Iterator[Conn]:
    """Συνεπές στιγμιότυπο μόνο-ανάγνωσης (όλα τα queries βλέπουν την ίδια κατάσταση)."""
    init_db()
    with _raw() as conn:
        _rt(2)  # BEGIN + ROLLBACK
        if conn.kind == "pg":
            conn.raw.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        else:
            conn.raw.execute("BEGIN")
        try:
            yield conn
        finally:
            conn.raw.execute("COMMIT" if conn.kind == "sqlite" else "ROLLBACK")


@contextmanager
def transaction(lock: str | None = None) -> Iterator[Conn]:
    """Transaction· COMMIT στο τέλος, ROLLBACK σε exception.

    lock: προαιρετικό κανάλι σειριοποίησης (π.χ. "scans").
    """
    init_db()
    if get_settings().database_url:
        with _raw() as conn:
            _rt(2)  # BEGIN + COMMIT
            with conn.raw.transaction():
                if lock:
                    conn.lock(lock)
                yield conn
        return
    with _sqlite_write_lock:
        with _raw() as conn:
            _rt(2)
            conn.raw.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.raw.execute("ROLLBACK")
                raise
            conn.raw.execute("COMMIT")


def bump_version(conn: Conn) -> int:
    """Αυξάνει τον μετρητή αλλαγών (φθηνό polling από όλες τις οθόνες)."""
    row = conn.execute(
        "UPDATE app_settings SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key='data_version' RETURNING value"
    ).fetchone()
    return int(row["value"])


def get_version(conn: Conn) -> int:
    row = conn.execute("SELECT value FROM app_settings WHERE key='data_version'").fetchone()
    return int(row["value"]) if row else 0


import atexit  # noqa: E402

atexit.register(close_pool)
