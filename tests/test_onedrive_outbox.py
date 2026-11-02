"""End-to-end Supabase outbox tests; Microsoft API calls are mocked.

The warehouse must accept NEW transfers while the online Excel is open and
refuses writes; on retry, the queued sequence is applied exactly once and
the online package becomes the new immutable working copy.
"""
from __future__ import annotations

import io
import json
from zipfile import ZipFile

import openpyxl
import pytest

from app import backend
from app.db.database import transaction
from app.services import onedrive, onedrive_outbox
from app.services.catalog import active_import, working_object
from app.services.errors import Conflict
from app.storage import get_store
from tests.conftest import activate_workbook, login, scan


def repack(data: bytes) -> bytes:
    buffer = io.BytesIO()
    with ZipFile(io.BytesIO(data)) as source, ZipFile(buffer, "w") as target:
        for item in source.infolist():
            target.writestr(item, source.read(item.filename))
        target.comment = b"Repacked by Excel Online without cell value changes"
    return buffer.getvalue()


def setup_remote(app_env, synthetic_master, monkeypatch):
    activate_workbook(synthetic_master)
    imp = active_import()
    original = get_store().get(working_object(imp["id"], 1))
    cloud = {"data": repack(original), "etag": "office-autosave-etag"}
    b = {"import_id": imp["id"], "drive_id": "fake-drive", "item_id": "fake-item",
         "etag": "etag-from-first-import", "synced_version": 1,
         "name": "synthetic.xlsx", "link": "https://1drv.ms/x/test"}
    with transaction() as db:
        db.execute("INSERT INTO app_settings(key,value) VALUES (?,?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (onedrive.BOUND_KEY, json.dumps(b)))
    monkeypatch.setattr(onedrive, "_access_token", lambda: "fake-token")
    monkeypatch.setattr(onedrive, "_stable_download",
                        lambda *a: (cloud["data"], {"eTag": cloud["etag"]}))
    return cloud


def move(barcode):
    p = login("secretary")
    scan(login("scanner"), barcode)
    item = next(x for x in backend.dashboard(p)["items"] if x["barcode"] == barcode)
    return backend.transfer_item(p, item["id"])


def test_two_transfers_continue_when_onedrive_locked_and_replay_after(app_env, synthetic_master, monkeypatch):
    cloud = setup_remote(app_env, synthetic_master, monkeypatch)
    calls = []

    def locked(*a):
        calls.append("locked")
        raise Conflict("Microsoft 409: file is in use")

    monkeypatch.setattr("app.services.onedrive_reconcile._publish", locked)
    first = move("VS11486")
    second = move("VS11487")
    assert first["ok"] and second["ok"]
    assert first["onedrive_synced"] is False
    assert second["onedrive_synced"] is False
    assert active_import()["working_version"] == 3
    assert onedrive.status()["pending"] is True

    def publish(drive, item, etag, candidate, token):
        assert etag == cloud["etag"]
        assert drive == "fake-drive" and item == "fake-item"
        cloud["data"] = repack(candidate)
        cloud["etag"] = "after-upload"
        calls.append("success")

    monkeypatch.setattr("app.services.onedrive_reconcile._publish", publish)
    outcome = onedrive_outbox.retry_sync()
    assert outcome["synced"] is True and outcome["operations"] == 2
    assert active_import()["working_version"] == 4
    assert onedrive.status()["pending"] is False
    assert calls[-1] == "success"
    with io.BytesIO(cloud["data"]) as stream:
        workbook = openpyxl.load_workbook(stream, read_only=True)
        source_barcodes = [cell.value for cell in workbook["΄Ετοιμα προϊόντα"]["H"]]
        dest_barcodes = [cell.value for cell in workbook["Σβησμενα "]["H"]]
        workbook.close()
    assert "VS11486" not in source_barcodes
    assert "VS11487" not in source_barcodes
    assert dest_barcodes.count("VS11486") == 1
    assert dest_barcodes.count("VS11487") == 1
    assert onedrive_outbox.retry_sync()["operations"] == 0


def test_real_external_cell_edit_does_not_stop_warehouse_but_blocks_mirror(app_env, synthetic_master, monkeypatch):
    cloud = setup_remote(app_env, synthetic_master, monkeypatch)
    wb = openpyxl.load_workbook(io.BytesIO(cloud["data"]))
    wb["΄Ετοιμα προϊόντα"]["H2"] = "EXTERNAL-CELL-EDIT"
    output = io.BytesIO()
    wb.save(output)
    cloud["data"], cloud["etag"] = output.getvalue(), "external-change"
    calls = []
    monkeypatch.setattr("app.services.onedrive_reconcile._publish",
                        lambda *a: calls.append("unsafe-write"))

    result = move("VS11486")
    assert result["ok"] and result["onedrive_synced"] is False
    assert not calls
    with pytest.raises(Conflict, match="διαφορετικές τιμές"):
        onedrive_outbox.retry_sync()
    assert active_import()["working_version"] == 2
    assert onedrive.status()["pending"] is True


def test_single_transfer_auto_replays_repacked_online_workbook(app_env, synthetic_master, monkeypatch):
    cloud = setup_remote(app_env, synthetic_master, monkeypatch)

    def publish(drive, item, etag, candidate, token):
        cloud["data"], cloud["etag"] = repack(candidate), "after-upload"
    monkeypatch.setattr("app.services.onedrive_reconcile._publish", publish)
    result = move("VS11486")
    assert result["ok"] and result["onedrive_synced"] is True
    assert active_import()["working_version"] == 3
    assert onedrive.status()["pending"] is False
