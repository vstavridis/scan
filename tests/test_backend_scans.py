"""Σάρωση μέσω backend: διπλά, άγνωστα, idempotency, δικαιώματα, ταυτόχρονα sessions, εισαγωγές."""
from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import backend
from app.auth import PermissionDenied
from app.services.errors import ServiceError
from tests.conftest import activate_workbook, login, scan


def test_rescan_is_duplicate_and_not_counted_twice(app_env, synthetic_master):
    activate_workbook(synthetic_master)
    res = scan(login("scanner"), "VS11486", "VS11487", "VS11486")
    assert [r["result"] for r in res] == ["ok", "ok", "duplicate"]
    assert res[2]["message"] == "Το barcode έχει ήδη σκαναριστεί"
    sec = login("secretary")
    dash = backend.dashboard(sec)
    assert dash["totals"]["active_barcodes"] == 2
    assert dash["totals"]["total_kilos"] == "828"          # 412 + 416, όχι 1240
    assert dash["totals"]["duplicate_scans"] == 1
    hist = backend.history_rows(sec, barcode="VS11486")
    assert sorted(r["result"] for r in hist) == ["duplicate", "ok"]   # το διπλό μένει στο ιστορικό


def test_duplicate_across_batches_and_within_batch(app_env, synthetic_master):
    activate_workbook(synthetic_master)
    p = login("scanner")
    first = scan(p, "TST0001", "TST0002", "TST0001", "TST0001")
    assert [r["result"] for r in first] == ["ok", "ok", "duplicate", "duplicate"]
    assert first[2]["active_item_id"] == first[0]["active_item_id"]
    second = scan(p, "TST0002", "TST0003")
    assert [r["result"] for r in second] == ["duplicate", "ok"]
    items = {i["barcode"]: i for i in backend.dashboard(login("secretary"))["items"]}
    assert items["TST0001"]["duplicate_count"] == 2 and items["TST0002"]["duplicate_count"] == 1


def test_unknown_barcode(app_env, synthetic_master):
    activate_workbook(synthetic_master)
    res = scan(login("scanner"), "NOPE-999")[0]
    assert res["result"] == "unknown"
    assert "δεν βρέθηκε" in res["message"]
    dash = backend.dashboard(login("secretary"))
    assert dash["totals"]["unknown"] == 1
    assert dash["totals"]["total_kilos"] == "0"


def test_same_event_id_is_idempotent(app_env, synthetic_master):
    activate_workbook(synthetic_master)
    p = login("scanner")
    eid = "evt-0000-retry-0001"
    first = scan(p, "VS11486", ids=[eid])[0]
    for _ in range(3):  # π.χ. χαμένο acknowledgement -> ο browser ξαναστέλνει
        again = scan(p, "VS11486", ids=[eid])[0]
        assert again["result"] == "ok"
        assert again["server_id"] == first["server_id"]
        assert again["replayed"] is True
    both = scan(p, "VS11487", "VS11487", ids=["evt-same-batch-1", "evt-same-batch-1"])
    assert both[0]["server_id"] == both[1]["server_id"]
    sec = login("secretary")
    assert len(backend.history_rows(sec)) == 2
    assert backend.dashboard(sec)["totals"]["duplicate_scans"] == 0


def test_rapid_large_batches(app_env, synthetic_master):
    """300 barcode σε batches των 50 (όπως όταν ο browser συσσωρεύει σαρώσεις)."""
    activate_workbook(synthetic_master)
    p = login("scanner")
    codes = [f"TST{i:04d}" for i in range(1, 301)]
    t = time.perf_counter()
    results = []
    for i in range(0, 300, 50):
        results += scan(p, *codes[i:i + 50])
    elapsed = time.perf_counter() - t
    assert [r["result"] for r in results] == ["ok"] * 300
    assert len({r["server_id"] for r in results}) == 300
    assert elapsed < 10, elapsed
    assert backend.dashboard(login("secretary"))["totals"]["active_barcodes"] == 300


def test_no_active_excel_is_never_valid(app_env):
    assert scan(login("scanner"), "VS11486")[0]["result"] == "no_catalog"


def test_no_internal_login_all_screens_available(app_env, synthetic_master):
    """Χωρίς ρόλους/κωδικούς: κάθε προσκεκλημένος χρήστης έχει πρόσβαση σε όλες τις λειτουργίες.
    Οι ενέργειες απαιτούν μόνο ταυτότητα (για το audit) και καταγράφονται με το όνομα/email."""
    activate_workbook(synthetic_master)
    user = backend.identity("Μαρία", "maria@example.com")
    assert user.actor == "Μαρία <maria@example.com>"
    scan(user, "VS11486")
    assert backend.dashboard(user)["totals"]["total_kilos"] == "412"
    assert backend.scanner_view(user)["items"][0]["barcode"] == "VS11486"
    assert backend.history_rows(user)
    assert backend.get_settings_values(user)["scanner_terminator"] == "enter"
    assert backend.imports(user)
    item = backend.dashboard(user)["items"][0]
    backend.transfer_item(user, item["id"])
    log = backend.audit_log(user)
    done = next(r for r in log if r["action"] == "transfer.completed")
    assert done["actor"] == "Μαρία <maria@example.com>"
    # χωρίς ταυτότητα δεν εκτελείται τίποτα (π.χ. κλήση εκτός συνεδρίας)
    with pytest.raises(PermissionDenied):
        backend.submit_scans(None, [{"event_id": uuid.uuid4().hex, "barcode": "X"}])
    assert backend.identity(None, "x@y.gr").name == "x@y.gr" and backend.identity().name == "Χρήστης"


def test_concurrent_sessions_same_barcode_counts_once(app_env, synthetic_master):
    """Πολλά sessions (threads της ίδιας διεργασίας Streamlit) σκανάρουν ταυτόχρονα."""
    activate_workbook(synthetic_master)
    p = login("scanner")
    with ThreadPoolExecutor(max_workers=8) as pool:
        res = list(pool.map(lambda _: scan(p, "VS11486")[0], range(16)))
    kinds = sorted(r["result"] for r in res)
    assert kinds.count("ok") == 1 and kinds.count("duplicate") == 15
    assert backend.dashboard(login("secretary"))["totals"]["total_kilos"] == "412"


def test_concurrent_sessions_many_barcodes(app_env, synthetic_master):
    activate_workbook(synthetic_master)
    p = login("scanner")
    codes = [f"TST{i:04d}" for i in range(1, 121)]
    events = [(uuid.uuid4().hex, c) for c in codes]
    replays = events[:30]   # επαναποστολές από "δεύτερη καρτέλα"
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(scan, p, c, ids=[e]) for e, c in events + replays]
        out = [f.result()[0] for f in futures]
    assert all(r["result"] == "ok" for r in out)
    sec = login("secretary")
    assert backend.dashboard(sec)["totals"]["active_barcodes"] == 120
    hist = backend.history_rows(sec)
    assert len(hist) == 120 and len({r["event_id"] for r in hist}) == 120


def test_replace_excel_requires_confirmation_and_keeps_history(app_env, synthetic_master):
    activate_workbook(synthetic_master)
    scan(login("scanner"), "VS11486")
    admin = login("admin")
    staged = backend.stage_excel(admin, "new.xlsx", synthetic_master.read_bytes())
    with pytest.raises(ServiceError) as exc:
        backend.activate_excel(admin, staged["staging_id"])
    assert exc.value.code == "confirmation_required"
    res = backend.activate_excel(admin, staged["staging_id"], confirm_replace=True)
    assert res["backups"] == ["imports/00001/working/v00001.xlsx"]   # η προηγούμενη έκδοση μένει
    imports = backend.imports(admin)
    assert len(imports) == 2 and sum(i["is_active"] for i in imports) == 1
    dash = backend.dashboard(login("secretary"))
    assert dash["items"][0]["import_id"] == res["import_id"]
    assert len(backend.history_rows(login("secretary"))) == 1
    paths = {b["path"] for b in backend.backups(admin)}
    assert {"imports/00001/original.xlsx", "imports/00001/working/v00001.xlsx",
            "imports/00002/original.xlsx", "imports/00002/working/v00001.xlsx"} <= paths


def test_invalid_upload_rejected(app_env):
    admin = login("admin")
    staged = backend.stage_excel(admin, "bad.xlsx", b"not a zip")
    assert staged["report"]["ok"] is False and staged["report"]["errors"]
    with pytest.raises(ServiceError):
        backend.activate_excel(admin, staged["staging_id"])
    with pytest.raises(ServiceError):
        backend.stage_excel(admin, "bad.csv", b"a,b")


def test_history_filters(app_env, synthetic_master):
    activate_workbook(synthetic_master)
    scan(login("scanner"), "VS90002", "VS11486", "NOPE")
    sec = login("secretary")
    assert {r["barcode"] for r in backend.history_rows(sec, customer="ΚΥΡΙΑΚ")} == {"VS90002"}
    assert {r["barcode"] for r in backend.history_rows(sec, result="unknown")} == {"NOPE"}
    rows = backend.history_rows(sec)
    assert backend.history_export(sec, rows, "csv").decode("utf-8-sig").count("\n") == 4
    assert backend.history_export(sec, rows, "xlsx")[:2] == b"PK"


def test_parallel_scan_threads_do_not_deadlock(app_env, synthetic_master):
    activate_workbook(synthetic_master)
    p = login("scanner")
    errors: list[Exception] = []

    def worker(k: int) -> None:
        try:
            scan(p, *[f"TST{(k * 10 + i) % 300 + 1:04d}" for i in range(10)])
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(k,)) for k in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors
