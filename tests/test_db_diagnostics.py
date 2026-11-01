"""Διάγνωση σύνδεσης βάσης: η πραγματική αιτία αντί για γενικό PoolTimeout, χωρίς διαρροή μυστικών."""
from __future__ import annotations

import os
import threading
import time

import pytest

from app.db.diagnostics import DatabaseConnectionError, classify, describe_target, diagnose, redact
from tests.conftest import _reset_app_state, configure_env, free_port, pg_admin_url, temp_pg_database

SECRET = "Sup3r$ecret-pw"


# --------------------------------------------------------------------------- ταξινόμηση μηνυμάτων
@pytest.mark.parametrize("message,expected", [
    ('connection failed: FATAL:  password authentication failed for user "postgres"', "password"),
    ("connection failed: FATAL:  Tenant or user not found", "user_or_host"),
    ('FATAL:  role "postgres.wrongref" does not exist', "user_or_host"),
    ("FATAL:  Max client connections reached", "exhausted"),
    ("FATAL: MaxClientsInSessionMode: max clients reached - in Session mode max clients are limited to pool_size",
     "exhausted"),
    ("FATAL:  remaining connection slots are reserved for roles with the SUPERUSER attribute", "exhausted"),
    ("server does not support SSL, but SSL was required", "ssl"),
    ('could not translate host name "aws-0-xx.pooler.supabase.com" to address: Name or service not known', "dns"),
    ("connection to server at \"1.2.3.4\", port 5432 failed: timeout expired", "network"),
    ('FATAL:  database "postgress" does not exist', "config"),
])
def test_classify(message, expected):
    assert classify(message) == expected


# --------------------------------------------------------------------------- έλεγχοι χωρίς δίκτυο
def test_placeholder_password_detected_without_connecting():
    d = diagnose("postgresql://postgres.abc:[YOUR-PASSWORD]@aws-0-eu-central-1.pooler.supabase.com:5432/postgres")
    assert d.category == "password" and "placeholder" in d.as_text()


def test_pooler_requires_project_ref_in_username():
    d = diagnose(f"postgresql://postgres:{SECRET}@aws-0-eu-central-1.pooler.supabase.com:5432/postgres?sslmode=require")
    assert d.category == "user_or_host"
    assert SECRET not in d.as_text()


def test_target_description_and_redaction_never_leak_password():
    url = f"postgresql://postgres.abc:{SECRET}@aws-0-x.pooler.supabase.com:5432/postgres?sslmode=require"
    t = describe_target(url)
    assert t["host"] == "aws-0-x.pooler.supabase.com" and t["user"] == "postgres.abc" and t["has_password"]
    assert SECRET not in str(t)
    assert SECRET not in redact(f"error for {url} with password={SECRET}", url)
    assert "<connection string>" in redact(f"error for {url}", url)


def test_dns_failure():
    d = diagnose(f"postgresql://postgres.abc:{SECRET}@no-such-host-for-warehouse.invalid:5432/postgres", timeout=5)
    assert d.category == "dns" and SECRET not in d.as_text()


def test_tcp_refused_is_network():
    d = diagnose(f"postgresql://u:{SECRET}@127.0.0.1:{free_port()}/postgres", timeout=3)
    assert d.category == "network" and "TCP" in d.summary


# --------------------------------------------------------------------------- πραγματική PostgreSQL
needs_pg = pytest.mark.skipif(not pg_admin_url(), reason="WAREHOUSE_TEST_PG δεν ορίστηκε")


def _host_port() -> tuple[str, str]:
    from app.db.diagnostics import _parse
    p = _parse(pg_admin_url())
    return p.get("host", "127.0.0.1"), p.get("port", "5432")


@needs_pg
def test_success_reports_server_and_ssl():
    d = diagnose(pg_admin_url())
    assert d.ok and any("PostgreSQL" in x for x in d.details)


@needs_pg
def test_wrong_password_on_real_server():
    host, port = _host_port()
    d = diagnose(f"postgresql://wh_pw:{SECRET}@{host}:{port}/postgres", timeout=5)
    if d.ok:
        pytest.skip("ο τοπικός server δέχεται χωρίς κωδικό (trust)· δεν ελέγχεται")
    assert d.category == "password", d.as_text()
    assert SECRET not in d.as_text()


@needs_pg
def test_ssl_required_but_not_supported():
    host, port = _host_port()
    d = diagnose(f"postgresql://postgres:{SECRET}@{host}:{port}/postgres?sslmode=require", timeout=5)
    if d.ok:
        pytest.skip("ο τοπικός server υποστηρίζει SSL")
    assert d.category == "ssl", d.as_text()


@needs_pg
def test_exhausted_connections_on_real_server():
    import psycopg
    host, port = _host_port()
    with psycopg.connect(pg_admin_url(), autocommit=True) as admin:
        admin.execute("DROP ROLE IF EXISTS wh_limited")
        admin.execute("CREATE ROLE wh_limited LOGIN CONNECTION LIMIT 1")
        try:
            url = f"postgresql://wh_limited:x@{host}:{port}/postgres"
            with psycopg.connect(url):
                d = diagnose(url, timeout=5)
            assert d.category == "exhausted", d.as_text()
        finally:
            admin.execute("DROP ROLE IF EXISTS wh_limited")


@needs_pg
def test_app_reports_real_cause_quickly_instead_of_pool_timeout(tmp_path):
    """Λάθος κωδικός στα Secrets: σφάλμα με την αιτία σε λίγα δευτερόλεπτα, όχι PoolTimeout μετά από 30″."""
    from app import backend
    host, port = _host_port()
    configure_env(tmp_path / "d", f"postgresql://wh_pw:{SECRET}@{host}:{port}/postgres")
    t = time.monotonic()
    with pytest.raises(DatabaseConnectionError) as exc:
        backend.startup()
    elapsed = time.monotonic() - t
    _reset_app_state()
    if exc.value.diagnosis.category == "ok":
        pytest.skip("trust auth")
    assert exc.value.diagnosis.category == "password"
    assert elapsed < 15, elapsed
    assert SECRET not in str(exc.value)


@needs_pg
def test_pool_exhaustion_is_diagnosed(tmp_path, monkeypatch):
    from app.db import database
    monkeypatch.setattr(database, "POOL_MAX_SIZE", 1)
    monkeypatch.setattr(database, "POOL_TIMEOUT_S", 1.0)
    with temp_pg_database() as url:
        configure_env(tmp_path / "d", url)
        database.init_db()
        holding = threading.Event()
        release = threading.Event()

        def hold() -> None:
            with database.connect():
                holding.set()
                release.wait(10)

        th = threading.Thread(target=hold)
        th.start()
        holding.wait(10)
        try:
            with pytest.raises(DatabaseConnectionError) as exc:
                with database.connect():
                    pass
            assert exc.value.diagnosis.category == "exhausted"
            assert "pool" in exc.value.diagnosis.summary
        finally:
            release.set()
            th.join(10)
        with database.connect() as c:   # μετά την αποδέσμευση λειτουργεί κανονικά
            assert c.execute("SELECT 1 AS x").fetchone()["x"] == 1


def test_logs_do_not_contain_password(caplog):
    caplog.set_level("DEBUG")
    diagnose(f"postgresql://postgres.abc:{SECRET}@no-such-host-for-warehouse.invalid:5432/postgres", timeout=3)
    diagnose(f"postgresql://u:{SECRET}@127.0.0.1:{free_port()}/postgres", timeout=2)
    assert SECRET not in caplog.text
    assert "postgresql://" not in caplog.text
