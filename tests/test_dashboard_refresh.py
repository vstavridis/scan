"""Conditional secretary dashboard polling must be fast and fresh on real scans."""
from __future__ import annotations

import json

from app import backend
from app.db.database import transaction, bump_version
from app.services import aggregation
from tests.conftest import activate_workbook, login, scan


def test_idle_dashboard_poll_skips_expensive_queries(app_env, synthetic_master, monkeypatch):
    activate_workbook(synthetic_master)
    p = login("secretary")
    data = backend.dashboard_if_changed(p, None)
    assert data is not None and data["items"] == []
    assert data["onedrive_pending"] is False

    def must_not_query(*a, **k):
        raise AssertionError("idle heartbeat must not re-fetch active items")
    monkeypatch.setattr(aggregation, "active_items", must_not_query)
    assert backend.dashboard_if_changed(p, data["version"]) is None


def test_new_scan_invalidates_cached_dashboard(app_env, synthetic_master):
    activate_workbook(synthetic_master)
    p = login("secretary")
    previous = backend.dashboard_if_changed(p, None)
    scan(login("scanner"), "VS11486")
    changed = backend.dashboard_if_changed(p, previous["version"])
    assert changed is not None
    assert changed["version"] != previous["version"]
    assert [x["barcode"] for x in changed["items"]] == ["VS11486"]
    assert backend.dashboard_if_changed(p, changed["version"]) is None


def test_one_drive_lag_is_included_in_same_snapshot(app_env, synthetic_master):
    activate_workbook(synthetic_master)
    p = login("secretary")
    before = backend.dashboard(p)
    assert before["onedrive_pending"] is False
    imp = before["import"]
    b = {"import_id": imp["id"], "synced_version": 0,
         "drive_id": "fake-drive", "item_id": "fake-item"}
    with transaction() as db:
        db.execute("INSERT INTO app_settings(key,value) VALUES(?,?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   ("onedrive.bound", json.dumps(b)))
        bump_version(db)
    after = backend.dashboard_if_changed(p, before["version"])
    assert after is not None
    assert after["onedrive_pending"] is True
    b["synced_version"] = after["import"]["working_version"]
    with transaction() as db:
        db.execute("UPDATE app_settings SET value=? WHERE key=?",
                   (json.dumps(b), "onedrive.bound"))
        bump_version(db)
    synced = backend.dashboard_if_changed(p, after["version"])
    assert synced is not None and synced["onedrive_pending"] is False
