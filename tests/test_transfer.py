"""Tests 11–15: ασφαλής μεταφορά γραμμής, διατήρηση workbook, αποτυχίες χωρίς ψευδή επιτυχία."""
from __future__ import annotations

import re
import threading
import zipfile
from pathlib import Path

import pytest

from app.config import DEFAULT_SOURCE_SHEET as SRC, DEFAULT_TARGET_SHEET as TGT
from app.excel.package import XlsxPackage, iter_sheet_rows
from app.excel.reader import validate_workbook
from app.excel.writer import transfer_row
from tests.conftest import activate_workbook, login, scan, sha256


def _working_path(client) -> Path:
    imports = client.get("/api/admin/imports", headers=login(client, "admin")).json()
    return Path(next(i for i in imports if i["is_active"])["working_path"])


def _item_id(client, barcode: str) -> int:
    dash = client.get("/api/dashboard", headers=login(client, "secretary")).json()
    return next(i["id"] for i in dash["items"] if i["barcode"] == barcode)


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
def test_transfer_row_on_copy_preserves_workbook(workbook_copy, tmp_path, app_env):
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


def test_cross_sheet_references_are_adjusted(synthetic_master, tmp_path, app_env):
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


def test_unsafe_shift_falls_back_to_clear_with_warning(synthetic_master, tmp_path, app_env):
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


def test_changed_row_is_not_touched(synthetic_master, tmp_path, app_env):
    from app.excel.writer import RowNotFoundError
    rep = validate_workbook(synthetic_master, SRC, TGT)
    p = next(x for x in rep.products if x.barcode_norm == "VS11486")
    with pytest.raises(RowNotFoundError):
        transfer_row(synthetic_master, tmp_path / "o.xlsx", SRC, TGT, p.barcode_norm, "0" * 64)
    assert not (tmp_path / "o.xlsx").exists()


# ------------------------------------------------------------------ 14, 15: μέσω API
def test_transfer_via_api_updates_lists_and_totals(client, workbook_copy):
    activate_workbook(client, workbook_copy)
    scanner, sec = login(client, "scanner"), login(client, "secretary")
    scan(client, scanner, "VS11486", "VS11487")
    before = client.get("/api/dashboard", headers=sec).json()
    assert before["totals"]["total_kilos"] == "828"
    working = _working_path(client)
    original_input_sha = sha256(workbook_copy)

    item_id = _item_id(client, "VS11486")
    r = client.post(f"/api/items/{item_id}/transfer", headers=sec)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["barcode"] == "VS11486" and body["mode"] == "shift"
    assert Path(body["backup_path"]).is_file()

    # 14: εξαφανίζεται από την ενεργή λίστα (scanner + γραμματεία)
    active = client.get("/api/active", headers=scanner).json()
    assert [i["barcode"] for i in active["items"]] == ["VS11487"]
    after = client.get("/api/dashboard", headers=sec).json()
    assert [i["barcode"] for i in after["items"]] == ["VS11487"]
    # 15: σωστή ενημέρωση SUM
    assert after["totals"]["total_kilos"] == "416"
    assert after["totals"]["active_barcodes"] == 1
    assert [(g["product"], g["total_display"]) for g in after["groups"]] == [("ΗΛΕΚΤΡΟΓΑΛΒΑΝΙΖΕ × 0.70 × 180ΧΡΟΛΟ", "416")]

    # Excel: εγγραφή στο working copy, όχι στο αρχικό/ανεβασμένο αρχείο
    assert "VS11486" not in _barcodes(working, SRC)
    assert _barcodes(working, TGT)[-1] == "VS11486"
    assert sha256(workbook_copy) == original_input_sha
    imports = client.get("/api/admin/imports", headers=login(client, "admin")).json()
    original = Path(imports[0]["original_path"])
    assert sha256(original) == original_input_sha

    # ιστορικό
    hist = client.get("/api/history", params={"barcode": "VS11486"}, headers=sec).json()
    assert hist[0]["transfer_status"] == "Μεταφέρθηκε" and hist[0]["transfer_by"].startswith("test-secretary")

    # δεύτερη μεταφορά ίδιου item -> άρνηση
    r2 = client.post(f"/api/items/{item_id}/transfer", headers=sec)
    assert r2.status_code == 409
    # νέο scan του ίδιου barcode -> «έχει ήδη μεταφερθεί»
    res = scan(client, scanner, "VS11486")[0]
    assert res["result"] == "unknown" and "μεταφερθεί" in res["message"]


def test_second_transfer_after_shift_hits_right_row(client, synthetic_master):
    """Μετά από διαγραφή γραμμής, οι επόμενες μεταφορές πρέπει να βρίσκουν τη σωστή γραμμή."""
    activate_workbook(client, synthetic_master)
    scan(client, login(client, "scanner"), "VS90001", "VS11486", "TST0100")
    sec = login(client, "secretary")
    for bc in ("VS90001", "TST0100", "VS11486"):
        r = client.post(f"/api/items/{_item_id(client, bc)}/transfer", headers=sec)
        assert r.status_code == 200, r.text
    working = _working_path(client)
    src = _barcodes(working, SRC)
    assert not {"VS90001", "VS11486", "TST0100"} & set(src)
    assert "TST0099" in src and "TST0101" in src
    assert _barcodes(working, TGT)[-3:] == ["VS90001", "TST0100", "VS11486"]
    rep = validate_workbook(working, SRC, TGT)
    assert rep.ok


def test_concurrent_double_click_transfers_once(client, synthetic_master):
    activate_workbook(client, synthetic_master)
    scan(client, login(client, "scanner"), "VS11486")
    sec = login(client, "secretary")
    item = _item_id(client, "VS11486")
    results: list[int] = []

    def go():
        results.append(client.post(f"/api/items/{item}/transfer", headers=sec).status_code)

    threads = [threading.Thread(target=go) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(200) == 1, results
    assert _barcodes(_working_path(client), TGT).count("VS11486") == 1


# ------------------------------------------------------------------ 11: αποτυχία εγγραφής χωρίς ψευδή επιτυχία
def test_locked_excel_reports_error_and_changes_nothing(client, synthetic_master):
    activate_workbook(client, synthetic_master)
    scan(client, login(client, "scanner"), "VS11486")
    sec = login(client, "secretary")
    working = _working_path(client)
    before = sha256(working)
    lock = working.with_name("~$" + working.name)  # όπως το δημιουργεί το Excel όταν ανοίγει αρχείο
    lock.write_text("locked")
    r = client.post(f"/api/items/{_item_id(client, 'VS11486')}/transfer", headers=sec)
    assert r.status_code == 423
    assert "ανοιχτό" in r.json()["detail"]
    assert sha256(working) == before
    dash = client.get("/api/dashboard", headers=sec).json()
    assert [i["barcode"] for i in dash["items"]] == ["VS11486"]   # δεν αφαιρέθηκε
    lock.unlink()
    r = client.post(f"/api/items/{_item_id(client, 'VS11486')}/transfer", headers=sec)
    assert r.status_code == 200, r.text  # μετά το ξεκλείδωμα επιτρέπεται κανονικά


def test_failed_excel_replace_is_not_success(client, synthetic_master, monkeypatch):
    activate_workbook(client, synthetic_master)
    scan(client, login(client, "scanner"), "VS11486")
    sec = login(client, "secretary")
    working = _working_path(client)
    before = sha256(working)

    from app.excel import storage

    def boom(self, tmp, dest):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(storage.LocalExcelStore, "replace_atomic", boom)
    r = client.post(f"/api/items/{_item_id(client, 'VS11486')}/transfer", headers=sec)
    assert r.status_code == 400
    assert r.json()["code"] == "transfer_failed"
    assert sha256(working) == before
    dash = client.get("/api/dashboard", headers=sec).json()
    assert dash["totals"]["active_barcodes"] == 1
    hist = client.get("/api/history", headers=sec).json()
    assert hist[0]["transfer_status"] != "Μεταφέρθηκε"
    # δεν μένουν προσωρινά αρχεία
    assert not list(working.parent.glob("*.tmp.xlsx"))
    monkeypatch.undo()
    r = client.post(f"/api/items/{_item_id(client, 'VS11486')}/transfer", headers=sec)
    assert r.status_code == 200, r.text


def test_writer_error_is_not_success(client, synthetic_master, monkeypatch):
    activate_workbook(client, synthetic_master)
    scan(client, login(client, "scanner"), "VS11486")
    sec = login(client, "secretary")
    from app.excel.writer import UnsupportedStructureError
    from app.services import transfer as transfer_service

    def fail(*a, **k):
        raise UnsupportedStructureError("simulated")

    monkeypatch.setattr(transfer_service, "transfer_row", fail)
    r = client.post(f"/api/items/{_item_id(client, 'VS11486')}/transfer", headers=sec)
    assert r.status_code == 400 and "δεν έγινε" in r.json()["detail"]
    assert client.get("/api/dashboard", headers=sec).json()["totals"]["active_barcodes"] == 1


def test_recovery_after_crash_between_excel_and_db(client, synthetic_master, monkeypatch):
    activate_workbook(client, synthetic_master)
    scan(client, login(client, "scanner"), "VS11486")
    sec = login(client, "secretary")
    from app.services import transfer as transfer_service

    def crash(op_id, actor):
        raise RuntimeError("process killed (simulated)")

    monkeypatch.setattr(transfer_service, "_complete", crash)
    with pytest.raises(RuntimeError):
        transfer_service.transfer_item(_item_id(client, "VS11486"), "tester")
    monkeypatch.undo()
    # Το Excel γράφτηκε, η βάση όχι ακόμη -> η αναζήτηση στο startup ολοκληρώνει τη μεταφορά
    assert client.get("/api/dashboard", headers=sec).json()["totals"]["active_barcodes"] == 1
    report = transfer_service.recover_pending()
    assert report == [{"operation_id": 1, "outcome": "completed"}]
    assert client.get("/api/dashboard", headers=sec).json()["totals"]["active_barcodes"] == 0
