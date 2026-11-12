"""Ουρά μεταφορών: μαζική μεταφορά, επαναφορά στα Έτοιμα, διόρθωση barcode, worker, κατάσταση συγχρονισμού."""
from __future__ import annotations

import io
import json

import openpyxl
import pytest

from app import backend
from app.config import DEFAULT_SOURCE_SHEET as SRC, DEFAULT_TARGET_SHEET as TGT
from app.db.database import connect, transaction
from app.services import excel_worker, onedrive, onedrive_outbox
from app.services import transfer as transfer_service
from app.services.errors import Conflict, ServiceError
from tests.conftest import ROOT, activate_workbook, login, pg_admin_url, scan, temp_pg_database, working_file
from tests.test_transfer import _active_import, _barcodes, _item_id


def _ids(*barcodes: str) -> list[int]:
    return [_item_id(b) for b in barcodes]


# --------------------------------------------------------------------------- μαζική μεταφορά
def test_bulk_transfer_one_version_with_total_kilos(app_env, synthetic_master, tmp_path):
    activate_workbook(synthetic_master)
    scan(login("scanner"), "VS11486", "VS11487", "VS90001", "TST0001", "NOPE")
    sec = login("secretary")
    ids = _ids("VS11486", "VS11487", "VS90001", "TST0001")
    res = backend.queue_transfers(sec, ids + [_item_id("NOPE")])
    assert [q["barcode"] for q in res["queued"]] == ["VS11486", "VS11487", "VS90001", "TST0001"]
    assert res["total_kilos"] == "1441" and res["total_kilos_display"] == "1441"
    assert [s["barcode"] for s in res["skipped"]] == ["NOPE"]          # άγνωστο: δεν μεταφέρεται
    # αμέσως εκτός ενεργής λίστας, πριν το Excel
    assert [i["barcode"] for i in backend.dashboard(sec)["items"]] == ["NOPE"]
    assert backend.sync_status(sec)["pending"] == 4
    # δεύτερο κλικ (διπλό): τίποτα δεν καταχωρίζεται δύο φορές
    again = backend.queue_transfers(sec, ids)
    assert not again["queued"] and len(again["skipped"]) == 4

    out = transfer_service.process_pending()
    assert sorted(out["completed"]) == [1, 2, 3, 4] and out["version"] == 2
    assert _active_import()["working_version"] == 2                  # μία νέα έκδοση για όλα
    work = working_file(tmp_path)
    for bc in ("VS11486", "VS11487", "VS90001", "TST0001"):
        assert bc not in _barcodes(work, SRC) and _barcodes(work, TGT).count(bc) == 1
    with connect() as conn:
        seqs = [r["seq"] for r in conn.execute("SELECT seq FROM transfer_operations ORDER BY id").fetchall()]
    assert seqs == [1, 2, 3, 4]
    assert backend.sync_status(sec)["pending"] == 0


def test_bulk_transfer_respects_batch_limit(app_env, synthetic_master, tmp_path, monkeypatch):
    monkeypatch.setattr(transfer_service, "MAX_OPS_PER_VERSION", 3)
    activate_workbook(synthetic_master)
    codes = [f"TST{i:04d}" for i in range(1, 8)]
    scan(login("scanner"), *codes)
    backend.queue_transfers(login("secretary"), _ids(*codes))
    rounds = 0
    while transfer_service.process_pending()["processed"]:
        rounds += 1
    assert rounds == 3 and _active_import()["working_version"] == 4
    work = working_file(tmp_path)
    assert all(_barcodes(work, TGT).count(c) == 1 for c in codes)


# --------------------------------------------------------------------------- επαναφορά
def test_restore_puts_product_back_at_last_free_row(app_env, synthetic_master, tmp_path):
    activate_workbook(synthetic_master)
    scan(login("scanner"), "VS11486", "VS11487")
    sec = login("secretary")
    backend.queue_transfers(sec, _ids("VS11486", "VS11487"))
    transfer_service.process_pending()
    before = working_file(tmp_path)
    src_rows_before = len(_barcodes(before, SRC))

    ops = {t["barcode"]: t for t in backend.list_transfers(sec)}
    res = backend.restore_transfer(login("secretary"), ops["VS11486"]["id"], wait=True)
    assert res["ok"] and res["new_version"] == 3
    work = working_file(tmp_path)
    src = _barcodes(work, SRC)
    assert src[-1] == "VS11486" and len(src) == src_rows_before + 1    # τελευταία ελεύθερη σειρά
    assert "VS11486" not in _barcodes(work, TGT)
    assert "VS11487" in _barcodes(work, TGT)                          # η άλλη μεταφορά έμεινε
    # το προϊόν ξανά διαθέσιμο, το barcode ξανά στην ενεργή λίστα, και μπορεί να ξαναμεταφερθεί
    dash = backend.dashboard(sec)
    assert [i["barcode"] for i in dash["items"]] == ["VS11486"] and dash["items"][0]["state"] == "ok"
    listed = {t["barcode"]: t for t in backend.list_transfers(sec)}
    assert listed["VS11486"]["reverted_at"] and listed["VS11486"]["restore_status"] == "completed"
    with pytest.raises(Conflict):
        backend.restore_transfer(sec, ops["VS11486"]["id"], wait=True)  # όχι δεύτερη επαναφορά
    log = [r["action"] for r in backend.audit_log(login("admin"))]
    assert "transfer.restored" in log

    again = backend.transfer_item(sec, _item_id("VS11486"))
    assert again["ok"]
    work = working_file(tmp_path)
    assert "VS11486" not in _barcodes(work, SRC) and _barcodes(work, TGT).count("VS11486") == 1


def test_restore_rejected_for_unfinished_or_old_import(app_env, synthetic_master):
    activate_workbook(synthetic_master)
    scan(login("scanner"), "VS11486")
    sec = login("secretary")
    q = backend.queue_transfers(sec, _ids("VS11486"))
    with pytest.raises(Conflict, match="δεν έχει ολοκληρωθεί"):
        backend.restore_transfer(sec, q["queued"][0]["operation_id"])
    transfer_service.process_pending()
    activate_workbook(synthetic_master)                 # νέα εισαγωγή Excel
    with pytest.raises(ServiceError, match="προηγούμενο Excel"):
        backend.restore_transfer(sec, q["queued"][0]["operation_id"])


def test_restore_and_new_transfer_in_same_version(app_env, synthetic_master, tmp_path):
    activate_workbook(synthetic_master)
    scan(login("scanner"), "VS11486", "VS11487")
    sec = login("secretary")
    backend.transfer_item(sec, _item_id("VS11486"))
    op = backend.list_transfers(sec)[0]
    backend.restore_transfer(sec, op["id"])               # στην ουρά
    backend.queue_transfers(sec, _ids("VS11487"))         # στην ουρά
    out = transfer_service.process_pending()
    assert len(out["completed"]) == 2 and _active_import()["working_version"] == 3
    work = working_file(tmp_path)
    assert "VS11486" in _barcodes(work, SRC) and "VS11486" not in _barcodes(work, TGT)
    assert "VS11487" in _barcodes(work, TGT) and "VS11487" not in _barcodes(work, SRC)


# --------------------------------------------------------------------------- διόρθωση barcode
def test_correct_barcode_from_review(app_env, synthetic_master):
    activate_workbook(synthetic_master)
    scanner = login("scanner")
    scan(scanner, "V11486")
    sec = login("secretary")
    item = backend.dashboard(sec)["items"][0]
    assert item["state"] == "unknown"
    res = backend.correct_item(sec, item["id"], " VS11486 ")
    assert res["result"] == "ok" and res["state"] == "ok"
    dash = backend.dashboard(sec)
    assert [(i["barcode"], i["state"]) for i in dash["items"]] == [("VS11486", "ok")]
    assert dash["totals"]["problem"] == 0 and dash["totals"]["total_kilos"] == "412"
    view = backend.scanner_view(scanner)
    assert [i["barcode"] for i in view["items"]] == ["VS11486"]
    hist = backend.history_rows(sec)[0]
    assert hist["barcode"] == "V11486" and hist["correction"] == "V11486 → VS11486"   # το σκανάρισμα μένει
    # μεταφέρεται κανονικά με το σωστό barcode
    assert backend.transfer_item(sec, item["id"])["barcode"] == "VS11486"


def test_correct_barcode_unknown_and_duplicate(app_env, synthetic_master):
    activate_workbook(synthetic_master)
    scan(login("scanner"), "VS11486", "V11486", "XX1")
    sec = login("secretary")
    items = {i["barcode"]: i for i in backend.dashboard(sec)["items"]}
    res = backend.correct_item(sec, items["XX1"]["id"], "XX2")
    assert res["result"] == "unknown"                                 # μένει στα «Προς έλεγχο»
    with pytest.raises(ServiceError):
        backend.correct_item(sec, items["XX1"]["id"], "XX2")         # ίδιο barcode
    with pytest.raises(ServiceError):
        backend.correct_item(sec, items["XX1"]["id"], "   ")
    dup = backend.correct_item(sec, items["V11486"]["id"], "VS11486")
    assert dup["result"] == "duplicate" and dup["item_id"] == items["VS11486"]["id"]
    dash = backend.dashboard(sec)
    assert sorted(i["barcode"] for i in dash["items"]) == ["VS11486", "XX2"]
    assert next(i for i in dash["items"] if i["barcode"] == "VS11486")["duplicate_count"] == 1


def test_remove_is_immediate(app_env, synthetic_master):
    activate_workbook(synthetic_master)
    scan(login("scanner"), "VS11486", "XX1")
    sec = login("secretary")
    backend.remove_item(sec, _item_id("XX1"))
    assert [i["barcode"] for i in backend.dashboard(sec)["items"]] == ["VS11486"]


# --------------------------------------------------------------------------- worker / κατάσταση
def test_worker_round_processes_queue_and_status(app_env, synthetic_master, tmp_path):
    activate_workbook(synthetic_master)
    scan(login("scanner"), "VS11486", "VS11487")
    sec = login("secretary")
    status = backend.sync_status(sec)
    assert status["pending"] == 0 and status["last_ok_at"] is None and status["excel_version"] == 1
    backend.queue_transfers(sec, _ids("VS11486", "VS11487"))
    snap = backend.dashboard_if_changed(sec, None)
    assert snap["sync"]["pending"] == 2
    out = excel_worker.run_once()
    assert out["processed"] == 2
    status = backend.sync_status(sec)
    assert status["pending"] == 0 and status["last_ok_at"] and status["excel_version"] == 2
    assert status["onedrive"]["bound"] is False
    hist = {r["barcode"]: r for r in backend.history_rows(sec)}
    assert hist["VS11486"]["transfer_status"] == "Μεταφέρθηκε"
    assert excel_worker.run_once()["processed"] == 0


def test_worker_thread_picks_up_kick(app_env, synthetic_master, tmp_path):
    import time
    activate_workbook(synthetic_master)
    scan(login("scanner"), "VS11486")
    excel_worker.start()
    try:
        assert excel_worker.running()
        backend.queue_transfers(login("secretary"), _ids("VS11486"))
        deadline = time.time() + 30
        while transfer_service.pending_count() and time.time() < deadline:
            time.sleep(0.1)
    finally:
        excel_worker.stop()
    assert not excel_worker.running()
    assert transfer_service.pending_count() == 0
    assert _active_import()["working_version"] == 2


# --------------------------------------------------------------------------- OneDrive καθρέφτης
def test_onedrive_replays_batched_version_and_restore(app_env, synthetic_master, monkeypatch):
    from tests.test_onedrive_outbox import repack, setup_remote
    cloud = setup_remote(app_env, synthetic_master, monkeypatch)
    calls = []

    def locked(*a):
        calls.append("locked")
        raise Conflict("Microsoft 409: file is in use")

    monkeypatch.setattr("app.services.onedrive_reconcile._publish", locked)
    scan(login("scanner"), "VS11486", "VS11487", "VS90001")
    sec = login("secretary")
    backend.queue_transfers(sec, _ids("VS11486", "VS11487", "VS90001"))
    transfer_service.process_pending()                                        # v2: 3 μεταφορές μαζί
    op = next(t for t in backend.list_transfers(sec) if t["barcode"] == "VS11487")
    backend.restore_transfer(sec, op["id"])
    transfer_service.process_pending()                                        # v3: επαναφορά
    assert onedrive.status()["pending"] is True
    st = backend.sync_status(sec)
    assert st["onedrive"]["pending"] is True

    def publish(drive, item, etag, candidate, token):
        cloud["data"], cloud["etag"] = repack(candidate), "after-upload"
        calls.append("success")

    monkeypatch.setattr("app.services.onedrive_reconcile._publish", publish)
    outcome = onedrive_outbox.retry_sync()
    assert outcome["synced"] is True and outcome["operations"] == 4
    state = onedrive.status()
    assert state["pending"] is False and state["synced_at"]
    wb = openpyxl.load_workbook(io.BytesIO(cloud["data"]), read_only=True)
    src = [r[0] for r in wb[SRC].iter_rows(min_col=8, max_col=8, values_only=True)]
    tgt = [r[0] for r in wb[TGT].iter_rows(min_col=8, max_col=8, values_only=True)]
    wb.close()
    assert "VS11486" not in src and "VS90001" not in src and src.count("VS11487") == 1
    assert tgt.count("VS11486") == 1 and tgt.count("VS90001") == 1 and "VS11487" not in tgt


# --------------------------------------------------------------------------- αναβάθμιση σχήματος
@pytest.mark.skipif(not pg_admin_url(), reason="WAREHOUSE_TEST_PG δεν ορίστηκε")
def test_schema_upgrade_from_v2_keeps_transfers():
    import psycopg

    old = (ROOT / "tests" / "fixtures" / "migration_v2.sql").read_text(encoding="utf-8")
    new = (ROOT / "supabase" / "migrations" / "001_warehouse_init.sql").read_text(encoding="utf-8")
    with temp_pg_database() as url:
        with psycopg.connect(url, autocommit=True) as c:
            c.execute("CREATE SCHEMA storage; CREATE TABLE storage.buckets (id text primary key, name text, "
                      "public boolean, file_size_limit bigint)")
            c.execute(old)
            c.execute("SET session_replication_role = replica")             # χωρίς FK για τα δοκιμαστικά rows
            c.execute("INSERT INTO transfer_operations(product_record_id, import_id, barcode, fingerprint, status, requested_by, "
                      "started_at) VALUES (1, 1, 'VS1', 'f', 'completed', 'x', '2026-01-01')")
            c.execute(new)
            c.execute(new)                                                  # idempotent
            row = c.execute("SELECT kind, status, reverted_at FROM transfer_operations").fetchone()
            assert row == ("transfer", "completed", None)
            assert c.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == "3"
            c.execute("INSERT INTO transfer_operations(product_record_id, import_id, barcode, fingerprint, status, kind, "
                      "requested_by, started_at) VALUES (2, 1, 'VS2', 'f', 'queued', 'transfer', 'x', '2026-01-01')")
            with pytest.raises(psycopg.errors.CheckViolation):
                c.execute("INSERT INTO transfer_operations(product_record_id, import_id, barcode, fingerprint, status, kind, "
                          "requested_by, started_at) VALUES (3, 1, 'VS3', 'f', 'queued', 'other', 'x', '2026-01-01')")


def test_startup_after_restore_and_retransfer(app_env, synthetic_master, tmp_path):
    """Regression: μετά από επαναφορά + νέα μεταφορά του ίδιου προϊόντος, η επανεκκίνηση
    (ξανά εφαρμογή του σχήματος) δεν πρέπει να αποτυγχάνει με UniqueViolation."""
    from app.db import database
    activate_workbook(synthetic_master)
    scan(login("scanner"), "VS11486")
    sec = login("secretary")
    backend.transfer_item(sec, _item_id("VS11486"))
    backend.restore_transfer(sec, backend.list_transfers(sec)[0]["id"], wait=True)
    backend.transfer_item(sec, _item_id("VS11486"))
    for _ in range(2):                                   # δύο "reboot"
        database.reset_init_cache()
        database.init_db()
    with connect() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM transfer_operations WHERE product_record_id="
                         "(SELECT product_record_id FROM transfer_operations LIMIT 1) "
                         "AND status='completed'").fetchone()["n"]
    assert n == 3                                        # μεταφορά, επαναφορά, νέα μεταφορά
    assert backend.dashboard(sec)["items"] == []         # η εφαρμογή λειτουργεί κανονικά


def test_onedrive_locked_retries_do_not_pile_up_backups(app_env, synthetic_master, monkeypatch):
    """Αρχείο ανοιχτό στο Excel Online: οι αυτόματες επαναλήψεις δεν ανεβάζουν νέα αντίγραφα κάθε φορά,
    και η αιτία εμφανίζεται στο Dashboard."""
    from tests.test_onedrive_outbox import repack, setup_remote
    cloud = setup_remote(app_env, synthetic_master, monkeypatch)
    from app.storage import get_store
    store = get_store()
    uploads: list[str] = []
    real_put = store.put_verified
    monkeypatch.setattr(store, "put_verified", lambda path, data, *a, **k: (uploads.append(path),
                                                                           real_put(path, data, *a, **k))[1])

    def locked(*a):
        raise Conflict("Microsoft 409: file is in use")

    monkeypatch.setattr("app.services.onedrive_reconcile._publish", locked)
    scan(login("scanner"), "VS11486")
    sec = login("secretary")
    backend.queue_transfers(sec, _ids("VS11486"))
    excel_worker.run_once()
    first = [u for u in uploads if "onedrive_before_sync" in u or "outbox_" in u]
    assert len(first) == 2
    for _ in range(3):
        synced, warning = transfer_service._onedrive_mirror("onedrive-mirror")
        assert synced is False and warning
    again = [u for u in uploads if "onedrive_before_sync" in u or "outbox_" in u]
    assert again == first                                   # κανένα νέο αντίγραφο στις επαναλήψεις
    snap = backend.dashboard_if_changed(sec, None)
    assert snap["onedrive_pending"] and "409" in snap["sync"]["onedrive_last_error"]["error"]

    def publish(drive, item, etag, candidate, token):
        cloud["data"], cloud["etag"] = repack(candidate), "after-upload"

    monkeypatch.setattr("app.services.onedrive_reconcile._publish", publish)
    synced, _ = transfer_service._onedrive_mirror("onedrive-mirror")
    assert synced is True and onedrive.status()["pending"] is False
    snap = backend.dashboard_if_changed(sec, None)
    assert not snap["onedrive_pending"] and snap["sync"]["onedrive_last_error"] is None
