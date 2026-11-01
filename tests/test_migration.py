"""Το SQL migration του Supabase πρέπει να είναι ίδιο με το σχήμα που εφαρμόζει η εφαρμογή,
και να εκτελείται σε πραγματική PostgreSQL (δύο φορές: idempotent)."""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import ROOT, pg_admin_url, temp_pg_database

MIGRATION = ROOT / "supabase" / "migrations" / "001_warehouse_init.sql"
SCHEMA = ROOT / "app" / "db" / "schema_postgres.sql"


def test_migration_contains_app_schema():
    assert SCHEMA.read_text(encoding="utf-8") in MIGRATION.read_text(encoding="utf-8")


@pytest.mark.skipif(not pg_admin_url(), reason="WAREHOUSE_TEST_PG δεν ορίστηκε")
def test_migration_runs_twice_on_postgres():
    import psycopg

    sql = MIGRATION.read_text(encoding="utf-8")
    with temp_pg_database() as url:
        with psycopg.connect(url, autocommit=True) as c:
            # Σε σκέτη PostgreSQL δεν υπάρχει το schema "storage" του Supabase: το προσομοιώνουμε.
            c.execute("CREATE SCHEMA storage; CREATE TABLE storage.buckets (id text primary key, name text, "
                      "public boolean, file_size_limit bigint)")
            c.execute(sql)
            c.execute(sql)
            tables = {r[0] for r in c.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='public'").fetchall()}
            assert {"scan_events", "active_items", "product_records", "excel_imports", "transfer_operations",
                    "audit_log", "app_settings", "app_locks", "staged_uploads", "schema_meta"} <= tables
            assert c.execute("SELECT public FROM storage.buckets WHERE id='warehouse-excel'").fetchone()[0] is False
            rls = c.execute("SELECT bool_and(rowsecurity) FROM pg_tables WHERE schemaname='public'").fetchone()[0]
            assert rls is True
