"""Ίδιος κωδικός με διαφορετικά μηδενικά: η πινακίδα γράφει «PG7052», το Excel «PG07052» (και αντίστροφα)."""
from __future__ import annotations

from app import backend
from app.services.normalize import barcode_variants
from tests.conftest import activate_workbook, login, scan


def test_variants():
    assert barcode_variants("PG7052")[:2] == ["PG07052", "PG007052"]
    assert "PG7052" in barcode_variants("PG07052")
    assert barcode_variants("EX434-1") == [] and barcode_variants("ABC") == [] and barcode_variants("") == []
    assert "VS11486" not in barcode_variants("VS11486")


def test_scan_finds_code_written_with_other_zeros(app_env, synthetic_master):
    """Στο Excel: TST0001 / VS11486. Σκανάρισμα TST1, TST01 (χωρίς μηδενικά) και VS011486 (με μηδενικό)."""
    activate_workbook(synthetic_master)
    res = scan(login("scanner"), "TST1", "VS011486", "TST2")
    assert [r["result"] for r in res] == ["ok", "ok", "ok"], res
    assert "στο Excel ως TST0001" in res[0]["message"]
    dash = backend.dashboard(login("secretary"))
    items = {i["barcode"]: i for i in dash["items"]}
    assert items["TST1"]["state"] == "ok" and items["VS011486"]["state"] == "ok"
    assert dash["totals"]["total_kilos"] == "615"           # 101 + 412 + 102 (κιλά των γραμμών του Excel)


def test_exact_spelling_wins_and_transfer_uses_excel_barcode(app_env, synthetic_master, tmp_path):
    from tests.test_transfer import _barcodes, working_file
    from app.config import DEFAULT_TARGET_SHEET as TGT
    activate_workbook(synthetic_master)
    res = scan(login("scanner"), "TST0003")
    assert res[0]["result"] == "ok" and res[0]["message"] == "Καταχωρήθηκε"
    scan(login("scanner"), "TST4")
    sec = login("secretary")
    item = next(i for i in backend.dashboard(sec)["items"] if i["barcode"] == "TST4")
    out = backend.transfer_item(sec, item["id"])
    assert out["ok"] and out["barcode"] == "TST0004"           # στο Excel μεταφέρεται η γραμμή του TST0004
    assert _barcodes(working_file(tmp_path), TGT).count("TST0004") == 1


def test_correction_and_unknown_still_work(app_env, synthetic_master):
    activate_workbook(synthetic_master)
    scan(login("scanner"), "PG99999", "XX5")
    sec = login("secretary")
    items = {i["barcode"]: i for i in backend.dashboard(sec)["items"]}
    assert items["PG99999"]["state"] == "unknown" and items["XX5"]["state"] == "unknown"
    res = backend.correct_item(sec, items["XX5"]["id"], "TST5")
    assert res["result"] == "ok"


def test_real_workbook_pg_codes(app_env, real_master):
    """Πραγματικό αρχείο: «PG02237» (5 ψηφία) βρίσκεται και ως «PG2237»· «PG3543» και ως «PG03543»."""
    activate_workbook(real_master)
    res = scan(login("scanner"), "PG2237", "PG03543", "PG02156")
    assert [r["result"] for r in res] == ["ok", "ok", "ok"], res
    assert "PG02237" in res[0]["message"] and "PG3543" in res[1]["message"]
