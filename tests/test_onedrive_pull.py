"""Αλλαγές απευθείας στο online Excel (π.χ. νέο προϊόν από άλλον χρήστη) -> κατάλογος της εφαρμογής.

Αιτία του προβλήματος: προϊόντα που προστέθηκαν στο Excel Online ΜΕΤΑ την εισαγωγή δεν υπήρχαν
στον κατάλογο, άρα το σκανάρισμά τους έδινε «ΔΕΝ ΒΡΕΘΗΚΕ». Τα barcode ταιριάζουν ΑΚΡΙΒΩΣ.
"""
from __future__ import annotations

import io

import openpyxl
import pytest

from app import backend
from app.config import DEFAULT_SOURCE_SHEET as SRC, DEFAULT_TARGET_SHEET as TGT
from app.services import excel_worker, onedrive, onedrive_outbox
from app.services import transfer as transfer_service
from app.services.catalog import active_import
from app.services.errors import ServiceError
from tests.conftest import login, scan
from tests.test_onedrive_outbox import repack, setup_remote


def online_edit(cloud, fn):
    wb = openpyxl.load_workbook(io.BytesIO(cloud["data"]))
    fn(wb)
    out = io.BytesIO()
    wb.save(out)
    cloud["data"] = out.getvalue()
    cloud["etag"] = cloud["etag"] + "+"


def add_rows(*rows):
    def fn(wb):
        ws = wb[SRC]
        last = max(r for r in range(1, ws.max_row + 1) if ws.cell(r, 8).value not in (None, ""))
        for i, (barcode, kila) in enumerate(rows, start=1):
            for col, v in enumerate(("ΓΑΛΒΑΝΙΖΕ", "0.50", "125ΧΡΟΛΟ", 1, kila, "ΝΕΟΣ ΠΕΛΑΤΗΣ", "PG", barcode,
                                     "Α", None, "R"), start=1):
                ws.cell(last + i, col, v)
    return fn


@pytest.fixture
def remote(app_env, synthetic_master, monkeypatch):
    cloud = setup_remote(app_env, synthetic_master, monkeypatch)
    meta_calls = []

    def metadata(drive, item, token=None):
        meta_calls.append(1)
        return {"eTag": cloud["etag"]}

    monkeypatch.setattr(onedrive, "file_metadata", metadata)
    cloud["meta_calls"] = meta_calls
    # το setup ξεκινά με διαφορετικό eTag από το binding: συγχρονίζουμε ώστε να είναι «ίδια»
    onedrive_outbox.pull()
    return cloud


def items():
    return {i["barcode"]: i for i in backend.dashboard(login("secretary"))["items"]}


def test_new_online_product_becomes_scannable_and_waiting_scan_is_found(remote):
    scan(login("scanner"), "PG7052")                    # σκαναρίστηκε πριν φανεί στο Excel της εφαρμογής
    assert items()["PG7052"]["state"] == "unknown"
    online_edit(remote, add_rows(("PG7052", 444), ("PG7039", 300), ("PG07053", 210)))
    out = onedrive_outbox.pull()
    assert out["changed"] and out["catalog"]["added"] == 3 and out["catalog"]["found"] == 1
    it = items()["PG7052"]
    assert it["state"] == "ok" and it["kila"] == "444"   # ο σκανάρισμα που περίμενε βρέθηκε
    res = scan(login("scanner"), "PG7039", "PG7053", "PG07053")
    assert [r["result"] for r in res] == ["ok", "unknown", "ok"]   # PG7053 ≠ PG07053 (ακριβής ταύτιση)
    assert active_import()["working_version"] == 2      # το online αρχείο έγινε νέα έκδοση
    # μεταφορά νέου προϊόντος: σωστή γραμμή του Excel
    sec = login("secretary")
    assert backend.transfer_item(sec, items()["PG7052"]["id"])["ok"]


def test_unchanged_etag_does_not_download(remote, monkeypatch):
    calls = []
    monkeypatch.setattr(onedrive, "_stable_download", lambda *a: calls.append(1))
    assert onedrive_outbox.pull() == {"changed": False}
    assert not calls and remote["meta_calls"]


def test_new_etag_with_same_cells_only_updates_etag(remote):
    remote["data"], remote["etag"] = repack(remote["data"]), "autosave-2"
    assert onedrive_outbox.pull() == {"changed": False}
    assert active_import()["working_version"] == 1 and onedrive.binding()["etag"] == "autosave-2"


def test_row_deleted_online_is_no_longer_found(remote):
    def delete(wb):
        ws = wb[SRC]
        row = next(r for r in range(2, ws.max_row + 1) if ws.cell(r, 8).value == "VS90002")
        ws.delete_rows(row)
    online_edit(remote, delete)
    out = onedrive_outbox.pull()
    assert out["catalog"]["removed"] == 1
    res = scan(login("scanner"), "VS90002", "VS11486")
    assert res[0]["result"] == "unknown" and res[0]["message"] == "Το barcode δεν βρέθηκε στο Excel"
    assert res[1]["result"] == "ok"                     # οι υπόλοιπες γραμμές μετακινήθηκαν σωστά
    assert backend.transfer_item(login("secretary"), items()["VS11486"]["id"])["ok"]


def test_queued_transfer_survives_pull_without_duplicate(remote):
    scan(login("scanner"), "VS11486")
    backend.queue_transfers(login("secretary"), [items()["VS11486"]["id"]])   # στην ουρά, όχι ακόμη στο Excel
    online_edit(remote, add_rows(("PG7100", 100)))
    out = onedrive_outbox.pull()
    assert out["catalog"]["added"] == 1                 # μόνο το νέο· το VS11486 δεν ξαναγίνεται διαθέσιμο
    assert transfer_service.process_pending()["completed"] == [1]
    res = scan(login("scanner"), "VS11486")
    assert res[0]["result"] == "unknown"                # μεταφέρθηκε, δεν «ξαναεμφανίστηκε»


def test_broken_online_file_is_not_adopted(remote):
    online_edit(remote, lambda wb: wb[SRC].cell(1, 8, "ΚΩΔΙΚΟΣ"))     # άλλαξε η επικεφαλίδα BARCODE
    with pytest.raises(ServiceError, match="σωστή μορφή"):
        onedrive_outbox.pull()
    assert active_import()["working_version"] == 1
    assert excel_worker.pull_onedrive_changes("t") is None
    assert "σωστή μορφή" in backend.sync_status(login("secretary"))["onedrive_pull_error"]["error"]


def test_merge_after_transfer_also_refreshes_catalog(remote, monkeypatch):
    """Μεταφορά ενώ άλλος χρήστης πρόσθεσε προϊόν online: συγχώνευση + νέο προϊόν στον κατάλογο."""
    def publish(drive, item, etag, candidate, token):
        remote["data"], remote["etag"] = repack(candidate), "after-upload"
    monkeypatch.setattr("app.services.onedrive_reconcile._publish", publish)
    online_edit(remote, add_rows(("PG7200", 120)))
    scan(login("scanner"), "VS11486")
    res = backend.transfer_item(login("secretary"), items()["VS11486"]["id"])
    assert res["ok"] and res["onedrive_synced"] is True
    assert scan(login("scanner"), "PG7200")[0]["result"] == "ok"
    wb = openpyxl.load_workbook(io.BytesIO(remote["data"]), read_only=True)
    assert "VS11486" in [r[0] for r in wb[TGT].iter_rows(min_col=8, max_col=8, values_only=True)]
    wb.close()
