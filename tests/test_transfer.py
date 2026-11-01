"""Μεταφορά γραμμής: ασφάλεια Excel (engine) + ροή μέσω backend (storage, έκδοση, journal, recovery)."""
from __future__ import annotations

import re
import threading
import zipfile
from pathlib import Path

import pytest

from app import backend
from app.config import DEFAULT_SOURCE_SHEET as SRC, DEFAULT_TARGET_SHEET as TGT
from app.db.database import connect, transaction
from app.excel.package import XlsxPackage, iter_sheet_rows
from app.excel.reader import validate_workbook
from app.excel.writer import transfer_row
from app.services.errors import ServiceError
from app.storage import get_store, sha256_bytes
from tests.conftest import activate_workbook, login, scan, sha256, working_file


def _item_id(barcode: str) -> int:
    dash = backend.dashboard(login("secretary"))
    return next(i["id"] for i in dash["items"] if i["barcode"] == barcode)


def _active_import() -> dict:
    return next(i for i in backend.imports(login("admin")) if i["is_active"])


def _barcodes(path: Path, sheet: str) -> list[str]:
    with XlsxPackage(path) as pkg:
        shared = pkg.shared_strings
        out = []
        for row in pkg.iter_rows(sheet):
            c = row.cells.get("H")
            if row.number > 1 and c is not None and c.raw is not None:
                out.append(c.resolved(shared))
        return out


# ------------------------------------------------------------------ 12, 13: writer σε αντίγραφο
def test_transfer_row_on_copy_preserves_workbook(workbook_copy, tmp_path):
    rep = validate_workbook(workbook_copy, SRC, TGT)
    p = next(x for x in rep.products if x.barcode_norm == "VS11486")
    out = tmp_path / "out.xlsx"
    original_sha = sha256(workbook_copy)

    res = transfer_row(workbook_copy, out, SRC, TGT, p.barcode_norm, p.fingerprint)

    assert sha256(workbook_copy) == original_sha          # η πηγή δεν αλλάζει
    assert res.mode == "shift"
    src_before, src_after = _barcodes(workbook_copy, SRC), _barcodes(out, SRC)
    tgt_before, tgt_after = _barcodes(workbook_copy, TGT), _barcodes(out, TGT)
    expected = list(src_before)
    expected.remove("VS11486")
    assert src_after == expected                           # ακριβώς μία γραμμή λιγότερη, ίδια σειρά
    assert tgt_after == tgt_before + ["VS11486"]           # στο τέλος του φύλλου προορισμού

    with zipfile.ZipFile(workbook_copy) as a, zipfile.ZipFile(out) as b:
        names_a, names_b = set(a.namelist()), set(b.namelist())
        with XlsxPackage(workbook_copy) as pkg:
            src_part, tgt_part = pkg.sheets[SRC], pkg.sheets[TGT]
        allowed = {src_part, tgt_part, "xl/workbook.xml", "[Content_Types].xml", "xl/_rels/workbook.xml.rels",
                   "xl/calcChain.xml"}
        # foreign sheets που ίσως ενημερώθηκαν επειδή αναφέρονται στο φύλλο πηγής
        allowed |= {p.lstrip("-") for p in res.modified_parts}
        assert names_a - names_b <= {"xl/calcChain.xml"}
        assert names_b <= names_a
        for name in names_b:
            if name not in allowed:
                assert a.read(name) == b.read(name), f"Άλλαξε απρόσμενα το {name}"
        # όλα τα φύλλα υπάρχουν, με τα ίδια ονόματα
        assert XlsxPackage(out).sheets.keys() == XlsxPackage(workbook_copy).sheets.keys()

        # οι υπάρχουσες γραμμές του φύλλου προορισμού έμειναν byte-for-byte ίδιες
        old_t = a.read(tgt_part).decode("utf-8")
        new_t = b.read(tgt_part).decode("utf-8")
        old_rows = old_t[old_t.find("<sheetData>"): old_t.rfind("<row ")]
        assert old_rows in new_t

        # κρίσιμοι τύποι: κάθε γραμμή του φύλλου πηγής δείχνει στη δική της γραμμή
        for row in iter_sheet_rows(b.read(src_part)):
            for cell in row.cells.values():
                if cell.formula:
                    for ref_row in re.findall(r"(?<![A-Z$:!\]])[KF](\d+)\b", cell.formula):
                        assert int(ref_row) == row.number, (row.number, cell.formula)
        # η νέα γραμμή στον προορισμό έχει τύπους που δείχνουν στη νέα της θέση
        new_row = [r for r in iter_sheet_rows(b.read(tgt_part)) if r.number == res.target_row][0]
        formulas = [c.formula for c in new_row.cells.values() if c.formula]
        assert formulas, "οι τύποι της γραμμής πρέπει να μεταφερθούν"
        for f in formulas:
            for ref_row in re.findall(r"(?<![A-Z$:!\]])[A-Z]{1,2}(\d+)\b", f):
                assert int(ref_row) == res.target_row, f

    # το νέο αρχείο ανοίγει με openpyxl και ξαναπερνά τον έλεγχο
    import openpyxl
    wb = openpyxl.load_workbook(out, read_only=True)
    assert SRC in wb.sheetnames and TGT in wb.sheetnames
    wb.close()
    rep2 = validate_workbook(out, SRC, TGT)
    assert rep2.ok and rep2.product_count == rep.product_count - 1


def test_cross_sheet_references_are_adjusted(synthetic_master, tmp_path):
    rep = validate_workbook(synthetic_master, SRC, TGT)
    p = next(x for x in rep.products if x.barcode_norm == "VS11486")
    out = tmp_path / "out.xlsx"
    transfer_row(synthetic_master, out, SRC, TGT, p.barcode_norm, p.fingerprint)
    with XlsxPackage(out) as pkg:
        id_row2 = next(r for r in pkg.iter_rows("ID") if r.number == 2)
        id_row3 = next(r for r in pkg.iter_rows("ID") if r.number == 3)
    # =SUM('΄Ετοιμα προϊόντα'!E2:E400) -> E2:E399 μετά τη διαγραφή μίας γραμμής
    assert id_row2.cells["D"].formula == f"SUM('{SRC}'!E2:E399)"
    assert id_row3.cells["D"].formula == f"'{SRC}'!H2"


def test_unsafe_shift_falls_back_to_clear_with_warning(synthetic_master, tmp_path):
    # Το ID!D3 δείχνει ακριβώς στη γραμμή του EX1898: η φυσική διαγραφή θα έσπαγε τον τύπο.
    rep = validate_workbook(synthetic_master, SRC, TGT)
    p = next(x for x in rep.products if x.barcode_norm == "EX1898")
    out = tmp_path / "out.xlsx"
    res = transfer_row(synthetic_master, out, SRC, TGT, p.barcode_norm, p.fingerprint)
    assert res.mode == "clear"
    assert res.warnings and "δεν διαγράφηκε φυσικά" in res.warnings[0]
    assert "EX1898" not in _barcodes(out, SRC)
    assert _barcodes(out, TGT)[-1] == "EX1898"
    with XlsxPackage(out) as pkg:
        d3 = next(r for r in pkg.iter_rows("ID") if r.number == 3).cells["D"]
    assert d3.formula == f"'{SRC}'!H2"  # ο τύπος δεν αλλοιώθηκε
    # σε λειτουργία "shift" μόνο, η μεταφορά ακυρώνεται αντί να αλλοιώσει
    from app.excel.writer import UnsupportedStructureError
    with pytest.raises(UnsupportedStructureError):
        transfer_row(synthetic_master, tmp_path / "o2.xlsx", SRC, TGT, p.barcode_norm, p.fingerprint, mode="shift")


def test_changed_row_is_not_touched(synthetic_master, tmp_path):
    from app.excel.writer import RowNotFoundError
    rep = validate_workbook(synthetic_master, SRC, TGT)
    p = next(x for x in rep.products if x.barcode_norm == "VS11486")
    with pytest.raises(RowNotFoundError):
        transfer_row(synthetic_master, tmp_path / "o.xlsx", SRC, TGT, p.barcode_norm, "0" * 64)
    assert not (tmp_path / "o.xlsx").exists()


# ------------------------------------------------------------------ ροή μέσω backend + storage
def test_transfer_updates_lists_totals_and_storage(app_env, workbook_copy, tmp_path):
    activate_workbook(workbook_copy)
    sec = login("secretary")
    scan(login("scanner"), "VS11486", "VS11487")
    assert backend.dashboard(sec)["totals"]["total_kilos"] == "828"
    original_input_sha = sha256(workbook_copy)

    item_id = _item_id("VS11486")
    res = backend.transfer_item(sec, item_id)
    assert res["ok"] and res["barcode"] == "VS11486" and res["mode"] == "shift"
    assert res["new_version"] == 2 and res["backup_object"] == "imports/00001/working/v00001.xlsx"

    # εξαφανίζεται από την ενεργή λίστα (αποθήκη + γραμματεία) και τα σύνολα ενημερώνονται
    assert [i["barcode"] for i in backend.scanner_view(login("scanner"))["items"]] == ["VS11487"]
    after = backend.dashboard(sec)
    assert [i["barcode"] for i in after["items"]] == ["VS11487"]
    assert after["totals"]["total_kilos"] == "416" and after["totals"]["active_barcodes"] == 1
    assert [(g["product"], g["total_display"]) for g in after["groups"]] == [("ΗΛΕΚΤΡΟΓΑΛΒΑΝΙΖΕ × 0.70 × 180ΧΡΟΛΟ", "416")]

    # Excel: νέα έκδοση στο storage· αρχικό και προηγούμενη έκδοση ΑΝΑΛΛΟΙΩΤΑ
    work = working_file(tmp_path)
    assert "VS11486" not in _barcodes(work, SRC)
    assert _barcodes(work, TGT)[-1] == "VS11486"
    store = get_store()
    assert sha256_bytes(store.get("imports/00001/original.xlsx")) == original_input_sha
    assert sha256_bytes(store.get("imports/00001/working/v00001.xlsx")) == original_input_sha
    imp = _active_import()
    assert imp["working_version"] == 2 and imp["working_sha256"] == sha256(work)

    hist = backend.history_rows(sec, barcode="VS11486")
    assert hist[0]["transfer_status"] == "Μεταφέρθηκε" and hist[0]["transfer_by"].startswith("test-secretary")
    with pytest.raises(ServiceError) as exc:          # δεύτερη μεταφορά -> άρνηση
        backend.transfer_item(sec, item_id)
    assert exc.value.status_code == 409
    res = scan(login("scanner"), "VS11486")[0]           # νέο scan -> «έχει ήδη μεταφερθεί»
    assert res["result"] == "unknown" and "μεταφερθεί" in res["message"]


def test_excel_persists_across_restart(app_env, synthetic_master, tmp_path):
    """Μετά από restart (νέα διεργασία, άδειες caches) το Excel και η έκδοσή του παραμένουν."""
    from tests.conftest import _reset_app_state
    activate_workbook(synthetic_master)
    scan(login("scanner"), "VS11486")
    backend.transfer_item(login("secretary"), _item_id("VS11486"))
    before = sha256(working_file(tmp_path))
    _reset_app_state()                                   # σαν να ξεκίνησε ξανά το Streamlit
    import shutil
    from app.config import get_settings
    shutil.rmtree(get_settings().cache_dir, ignore_errors=True)
    backend.startup()
    assert _active_import()["working_version"] == 2
    assert sha256(working_file(tmp_path)) == before
    assert len(backend.history_rows(login("secretary"))) == 1


def test_second_transfer_after_shift_hits_right_row(app_env, synthetic_master, tmp_path):
    activate_workbook(synthetic_master)
    scan(login("scanner"), "VS90001", "VS11486", "TST0100")
    sec = login("secretary")
    for bc in ("VS90001", "TST0100", "VS11486"):
        backend.transfer_item(sec, _item_id(bc))
    work = working_file(tmp_path)
    src = _barcodes(work, SRC)
    assert not {"VS90001", "VS11486", "TST0100"} & set(src)
    assert "TST0099" in src and "TST0101" in src
    assert _barcodes(work, TGT)[-3:] == ["VS90001", "TST0100", "VS11486"]
    assert validate_workbook(work, SRC, TGT).ok
    assert _active_import()["working_version"] == 4


def test_concurrent_transfers_are_safe(app_env, synthetic_master, tmp_path):
    """Διπλό click + δύο γραμματείς ταυτόχρονα: κάθε προϊόν μεταφέρεται μία φορά, καμία χαμένη αλλαγή."""
    activate_workbook(synthetic_master)
    scan(login("scanner"), "VS11486", "VS11487", "VS90001")
    sec = login("secretary")
    ids = [_item_id(b) for b in ("VS11486", "VS11487", "VS90001")]
    outcomes: list[str] = []
    lock = threading.Lock()

    def go(item_id: int) -> None:
        try:
            backend.transfer_item(sec, item_id)
            r = "ok"
        except ServiceError as exc:
            r = f"err{exc.status_code}"
        with lock:
            outcomes.append(r)

    threads = [threading.Thread(target=go, args=(i,)) for i in ids for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    assert outcomes.count("ok") == 3, outcomes
    assert all(o in ("ok", "err409") for o in outcomes), outcomes
    work = working_file(tmp_path)
    tgt = _barcodes(work, TGT)
    for bc in ("VS11486", "VS11487", "VS90001"):
        assert tgt.count(bc) == 1                       # καμία διπλή εγγραφή, καμία χαμένη
        assert bc not in _barcodes(work, SRC)
    assert _active_import()["working_version"] == 4


# ------------------------------------------------------------------ αποτυχίες χωρίς ψευδή επιτυχία
def test_storage_failure_is_not_success(app_env, synthetic_master, monkeypatch, tmp_path):
    activate_workbook(synthetic_master)
    scan(login("scanner"), "VS11486")
    sec = login("secretary")
    before = sha256(working_file(tmp_path))
    store = get_store()
    from app.storage import StorageError

    def boom(path, data, content_type=None):
        raise StorageError("Supabase Storage μη διαθέσιμο (προσομοίωση)")

    monkeypatch.setattr(store, "put", boom)
    with pytest.raises(ServiceError) as exc:
        backend.transfer_item(sec, _item_id("VS11486"))
    assert exc.value.code == "transfer_failed" and "δεν αποθηκεύτηκε" in exc.value.message
    monkeypatch.undo()
    assert sha256(working_file(tmp_path)) == before and _active_import()["working_version"] == 1
    assert backend.dashboard(sec)["totals"]["active_barcodes"] == 1
    assert backend.history_rows(sec)[0]["transfer_status"] != "Μεταφέρθηκε"
    backend.transfer_item(sec, _item_id("VS11486"))      # μετά την αποκατάσταση: κανονικά
    assert _active_import()["working_version"] == 2


def test_corrupted_upload_is_detected(app_env, synthetic_master, monkeypatch, tmp_path):
    """Αν το storage αποθηκεύσει άλλο περιεχόμενο από αυτό που στείλαμε, η μεταφορά ακυρώνεται."""
    activate_workbook(synthetic_master)
    scan(login("scanner"), "VS11486")
    store = get_store()
    real_put = store.put
    monkeypatch.setattr(store, "put", lambda path, data, content_type=None: real_put(path, data[:-10] + b"x" * 10))
    with pytest.raises(ServiceError):
        backend.transfer_item(login("secretary"), _item_id("VS11486"))
    monkeypatch.undo()
    assert _active_import()["working_version"] == 1


def test_writer_error_is_not_success(app_env, synthetic_master, monkeypatch):
    activate_workbook(synthetic_master)
    scan(login("scanner"), "VS11486")
    from app.excel.writer import UnsupportedStructureError
    from app.services import transfer as transfer_service

    def fail(*a, **k):
        raise UnsupportedStructureError("simulated")

    monkeypatch.setattr(transfer_service, "transfer_row", fail)
    with pytest.raises(ServiceError) as exc:
        backend.transfer_item(login("secretary"), _item_id("VS11486"))
    assert "δεν έγινε" in exc.value.message
    assert backend.dashboard(login("secretary"))["totals"]["active_barcodes"] == 1


def test_version_conflict_aborts_without_change(app_env, synthetic_master, monkeypatch, tmp_path):
    """Αν το working Excel άλλαξε από άλλη διεργασία στο μεταξύ, η μεταφορά δεν δεσμεύεται."""
    activate_workbook(synthetic_master)
    scan(login("scanner"), "VS11486")
    from app.services import transfer as transfer_service
    real_complete = transfer_service._complete

    def bump_then_complete(op_id, actor, import_id, base_version, *rest):
        with transaction() as conn:   # άλλη "διεργασία" προχώρησε την έκδοση
            conn.execute("UPDATE excel_imports SET working_version = working_version + 1 WHERE id=?", (import_id,))
        return real_complete(op_id, actor, import_id, base_version, *rest)

    monkeypatch.setattr(transfer_service, "_complete", bump_then_complete)
    with pytest.raises(ServiceError) as exc:
        backend.transfer_item(login("secretary"), _item_id("VS11486"))
    assert "έκδοσης" in exc.value.message
    with connect() as conn:
        op = conn.execute("SELECT status FROM transfer_operations").fetchone()
    assert op["status"] == "failed"
    assert backend.dashboard(login("secretary"))["totals"]["active_barcodes"] == 1


def test_recovery_of_interrupted_transfer(app_env, synthetic_master, monkeypatch, tmp_path):
    """Η διεργασία "πέθανε" μετά το ανέβασμα της νέας έκδοσης αλλά πριν το commit."""
    activate_workbook(synthetic_master)
    scan(login("scanner"), "VS11486")
    sec = login("secretary")
    before = sha256(working_file(tmp_path))
    from app.services import transfer as transfer_service

    def crash(*a, **k):
        raise SystemExit("process killed (simulated)")

    monkeypatch.setattr(transfer_service, "_complete", crash)
    with pytest.raises(SystemExit):
        backend.transfer_item(sec, _item_id("VS11486"))
    monkeypatch.undo()
    with connect() as conn:
        assert conn.execute("SELECT status FROM transfer_operations").fetchone()["status"] == "started"
    # Το Excel δεν άλλαξε (ο δείκτης έκδοσης δεν μετακινήθηκε)
    assert sha256(working_file(tmp_path)) == before and _active_import()["working_version"] == 1
    assert transfer_service.recover_pending(max_age_seconds=0) == [{"operation_id": 1, "outcome": "rolled_back"}]
    with connect() as conn:
        assert conn.execute("SELECT status FROM transfer_operations").fetchone()["status"] == "failed"
        conn.execute("DELETE FROM app_locks")  # το lease της "νεκρής" διεργασίας θα έληγε μόνο του
    res = backend.transfer_item(sec, _item_id("VS11486"))   # η μεταφορά μπορεί να γίνει ξανά
    assert res["new_version"] == 2
