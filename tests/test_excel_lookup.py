"""Tests 1 & 5: αναζήτηση barcode και διπλά barcode μέσα στο Excel."""
from __future__ import annotations

from app.config import DEFAULT_SOURCE_SHEET, DEFAULT_TARGET_SHEET
from app.excel.reader import validate_workbook
from app.services.normalize import format_kilos, normalize_barcode, parse_kilos, product_display
from tests.conftest import activate_workbook, login, scan


def test_sheet_names_are_exact():
    assert DEFAULT_SOURCE_SHEET == "΄Ετοιμα προϊόντα"
    assert DEFAULT_TARGET_SHEET.endswith(" ") and DEFAULT_TARGET_SHEET.strip() == "Σβησμενα"


def test_validate_and_lookup_vs11486(workbook_copy, app_env):
    rep = validate_workbook(workbook_copy, DEFAULT_SOURCE_SHEET, DEFAULT_TARGET_SHEET)
    assert rep.ok, rep.errors
    matches = [p for p in rep.products if p.barcode_norm == "VS11486"]
    assert len(matches) == 1
    p = matches[0]
    assert p.values["eidos"] == "ΗΛΕΚΤΡΟΓΑΛΒΑΝΙΖΕ"
    assert p.values["paxos"] == "0.70"          # διατηρείται η μορφή (όχι 0.7)
    assert p.values["diastaseis"] == "180ΧΡΟΛΟ"
    assert parse_kilos(p.values["kila"]) == 412
    assert p.values["pelatis"] == "ΑΠΟΘΗΚΗ"
    assert p.values["topothesia"] == "ΓΡΑΦΕΙΟ"
    assert product_display(p.values["eidos"], p.values["paxos"], p.values["diastaseis"], p.values["kila"],
                           p.values["pelatis"]) == "ΗΛΕΚΤΡΟΓΑΛΒΑΝΙΖΕ × 0.70 × 180ΧΡΟΛΟ / 412 - ΑΠΟΘΗΚΗ"


def test_scan_vs11486_via_api(client, workbook_copy):
    activate_workbook(client, workbook_copy)
    res = scan(client, login(client, "scanner"), "VS11486")[0]
    assert res["result"] == "ok"
    assert res["kila"] == "412"
    assert res["display"] == "ΗΛΕΚΤΡΟΓΑΛΒΑΝΙΖΕ × 0.70 × 180ΧΡΟΛΟ / 412 - ΑΠΟΘΗΚΗ"


def test_excel_duplicate_barcode_detected(workbook_copy, app_env):
    rep = validate_workbook(workbook_copy, DEFAULT_SOURCE_SHEET, DEFAULT_TARGET_SHEET)
    assert "EX434-1" in rep.duplicates
    assert len(rep.duplicates["EX434-1"]) == 2
    kilos = sorted(parse_kilos(p.kila) for p in rep.products if p.barcode_norm == "EX434-1")
    assert kilos[0] != kilos[1]  # διαφορετικά κιλά — δεν επιλέγουμε αυθαίρετα


def test_ambiguous_scan_adds_no_kilos_and_cannot_transfer(client, workbook_copy):
    activate_workbook(client, workbook_copy)
    res = scan(client, login(client, "scanner"), "EX434-1")[0]
    assert res["result"] == "ambiguous"
    assert "Αμφίσημο" in res["message"]
    sec = login(client, "secretary")
    dash = client.get("/api/dashboard", headers=sec).json()
    assert dash["totals"]["ambiguous"] == 1
    assert dash["totals"]["total_kilos"] == "0"
    item = dash["items"][0]
    cands = client.get(f"/api/items/{item['id']}/candidates", headers=sec).json()
    assert len(cands) == 2
    # δεν επιτρέπεται αυτόματη μεταφορά
    r = client.post(f"/api/items/{item['id']}/transfer", headers=sec)
    assert r.status_code == 400
    # μετά από ρητή επιλογή γραμμής, μετράνε τα κιλά της επιλεγμένης γραμμής
    chosen = next(c for c in cands if parse_kilos(c["kila"]) == 490)
    r = client.post(f"/api/items/{item['id']}/resolve", json={"product_record_id": chosen["id"]}, headers=sec)
    assert r.status_code == 200
    dash = client.get("/api/dashboard", headers=sec).json()
    assert dash["totals"]["total_kilos"] == "490"


def test_normalization():
    assert normalize_barcode(" vs11486\r") == "VS11486"
    assert format_kilos(parse_kilos("412.0")) == "412"
    assert format_kilos(parse_kilos("348,5")) == "348.5"
    assert parse_kilos("abc") is None
