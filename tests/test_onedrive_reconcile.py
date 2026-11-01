"""Safety tests for explicit OneDrive reconciliation, with mocked Microsoft network.

Synthetic workbook only; never mutates a real OneDrive file.
"""
from __future__ import annotations

import io
import json
from zipfile import ZipFile

import pytest

from app import backend
from app.db.database import transaction
from app.services import onedrive, onedrive_reconcile
from app.services.catalog import active_import, working_object
from app.services.errors import Conflict
from app.storage import get_store
from tests.conftest import activate_workbook, login, scan


def _repacked(data: bytes) -> bytes:
    out = io.BytesIO()
    with ZipFile(io.BytesIO(data)) as src, ZipFile(out, "w") as target:
        for item in src.infolist():
            target.writestr(item, src.read(item.filename))
        target.comment = b"Online metadata only"
    return out.getvalue()


def _pending(synthetic_master):
    activate_workbook(synthetic_master)
    p = login("secretary")
    scan(login("scanner"), "VS11486")
    item = next(x for x in backend.dashboard(p)["items"] if x["barcode"] == "VS11486")
    backend.transfer_item(p, item["id"])
    imp = active_import()
    b = {"import_id": imp["id"], "drive_id": "drive", "item_id": "item",
         "etag": "etag-original", "synced_version": 1, "name": "synthetic.xlsx",
         "link": "https://1drv.ms/x/test"}
    with transaction() as conn:
        conn.execute("INSERT INTO app_settings(key,value) VALUES (?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (onedrive.BOUND_KEY, json.dumps(b)))
    old = get_store().get(working_object(imp["id"], 1))
    current = get_store().get(imp["working_object"])
    return _repacked(old), current


def test_reconcile_reapplies_pending_operation_to_online_copy(app_env, synthetic_master, monkeypatch):
    original_online, committed = _pending(synthetic_master)
    state = {"data": original_online, "etag": "online-repackaged"}
    written = []

    monkeypatch.setattr(onedrive, "_access_token", lambda: "not-a-real-token")
    monkeypatch.setattr(onedrive, "_stable_download",
                        lambda *a: (state["data"], {"eTag": state["etag"]}))
    def pretend_put(drive, item, etag, data, token):
        assert etag == state["etag"]
        written.append(data)
        state["data"], state["etag"] = _repacked(data), "etag-updated"
    monkeypatch.setattr(onedrive_reconcile, "_publish", pretend_put)

    result = onedrive_reconcile.recover_single_pending("test-secretary")
    assert len(written) == 1
    assert result["new_version"] == 3
    assert result["barcode"] == "VS11486"
    imp = active_import()
    assert imp["working_version"] == 3
    assert get_store().get(imp["working_object"]) == state["data"]
    assert onedrive.binding()["synced_version"] == 3
    assert onedrive.binding()["etag"] == "etag-updated"
    assert get_store().get(result["backup"]) == original_online
    assert onedrive_reconcile._safe_cells_match(state["data"], committed)
    assert onedrive_reconcile._recovery_record() is None


def test_reconcile_refuses_real_external_cell_edit(app_env, synthetic_master, monkeypatch):
    original_online, committed = _pending(synthetic_master)
    # Corrupt one cell value by changing original bytes while keeping valid XLSX.
    import openpyxl
    w = openpyxl.load_workbook(io.BytesIO(original_online))
    w["΄Ετοιμα προϊόντα"]["H2"] = "CHANGED-EXTERNALLY"
    dest = io.BytesIO()
    w.save(dest)
    monkeypatch.setattr(onedrive, "_access_token", lambda: "fake-token")
    monkeypatch.setattr(onedrive, "_stable_download",
                        lambda *a: (dest.getvalue(), {"eTag": "external-edit"}))
    def must_not_upload(*a):
        raise AssertionError("No upload permitted")
    monkeypatch.setattr(onedrive_reconcile, "_publish", must_not_upload)

    with pytest.raises(Conflict, match="δεν έχει ίδιες τιμές"):
        onedrive_reconcile.recover_single_pending("test-secretary")
    assert active_import()["working_version"] == 2
    assert onedrive.binding()["synced_version"] == 1
