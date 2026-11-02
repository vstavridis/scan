"""Legacy OneDrive retry wrapper must use the durable mirror, without lost edits."""
from __future__ import annotations

import io
import json

import openpyxl
import pytest

from app import backend
from app.db.database import transaction
from app.services import onedrive
from app.services.catalog import active_import, working_object
from app.services.errors import Conflict
from app.storage import get_store
from tests.conftest import activate_workbook, login, scan


def _pending_after_one_transfer(synthetic_master):
    activate_workbook(synthetic_master)
    p = login("secretary")
    scan(login("scanner"), "VS11486")
    item_id = next(i["id"] for i in backend.dashboard(p)["items"] if i["barcode"] == "VS11486")
    assert backend.transfer_item(p, item_id)["ok"]
    imp = active_import()
    assert imp and imp["working_version"] == 2
    original = get_store().get(working_object(imp["id"], 1))
    changed = get_store().get(imp["working_object"])
    binding = {"import_id": imp["id"], "drive_id": "test-drive", "item_id": "test-item",
               "etag": "old-etag", "synced_version": 1, "link": "https://1drv.ms/x/test",
               "name": "synthetic.xlsx"}
    with transaction() as db:
        db.execute("INSERT INTO app_settings(key,value) VALUES (?,?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (onedrive.BOUND_KEY, json.dumps(binding)))
    return original, changed


def test_onedrive_etag_only_drift_is_safe_to_sync(app_env, synthetic_master, monkeypatch):
    original, changed = _pending_after_one_transfer(synthetic_master)
    read = iter([(original, {"eTag": "changed-etag"}),
                 (changed, {"eTag": "upload-etag"})])
    calls = []
    monkeypatch.setattr(onedrive, "_access_token", lambda: "test-access-token")
    monkeypatch.setattr(onedrive, "_stable_download", lambda *_: next(read))
    monkeypatch.setattr(
        "app.services.onedrive_reconcile._publish",
        lambda drive, item, etag, data, token: calls.append((drive, item, etag, data, token)))
    assert onedrive.retry_sync() is True
    assert len(calls) == 1
    assert calls[0][2] == "changed-etag"
    assert onedrive.status()["pending"] is False
    assert onedrive.binding()["etag"] == "upload-etag"


def test_onedrive_real_external_edit_is_never_overwritten(app_env, synthetic_master, monkeypatch):
    original, changed = _pending_after_one_transfer(synthetic_master)
    wb = openpyxl.load_workbook(io.BytesIO(original))
    wb["΄Ετοιμα προϊόντα"]["H2"] = "REAL-EXTERNAL-EDIT"
    output = io.BytesIO()
    wb.save(output)
    monkeypatch.setattr(onedrive, "_access_token", lambda: "test-access-token")
    monkeypatch.setattr(onedrive, "_stable_download",
                        lambda *_: (output.getvalue(), {"eTag": "changed-etag"}))
    def unsafe_upload(*args):
        raise AssertionError("Real external edits must never be overwritten")
    monkeypatch.setattr("app.services.onedrive_reconcile._publish", unsafe_upload)
    with pytest.raises(Conflict, match="διαφορετικές τιμές"):
        onedrive.retry_sync()
    assert onedrive.status()["pending"] is True


def test_onedrive_lost_ack_already_uploaded_marks_synced(app_env, synthetic_master, monkeypatch):
    original, changed = _pending_after_one_transfer(synthetic_master)
    monkeypatch.setattr(onedrive, "_access_token", lambda: "test-access-token")
    monkeypatch.setattr(onedrive, "_stable_download",
                        lambda *_: (changed, {"eTag": "new-etag"}))
    def unexpected_upload(*args):
        raise AssertionError("Remote is already correct; never upload twice")
    monkeypatch.setattr("app.services.onedrive_reconcile._publish", unexpected_upload)
    assert onedrive.retry_sync() is True
    assert onedrive.status()["pending"] is False
    assert onedrive.binding()["etag"] == "new-etag"
