"""Read-only XLSX comparison: distinguish package rewrites from real Excel cell edits."""
from __future__ import annotations

from io import BytesIO
from zipfile import ZipFile

from openpyxl import Workbook, load_workbook

from app.services.onedrive_diff import compare_workbook_cells


def workbook_bytes() -> bytes:
    w = Workbook()
    s = w.active
    s.title = "΄Ετοιμα προϊόντα"
    s["A1"] = "Είδος"
    s["H1"] = "Barcode"
    s["A2"] = "Δοκιμαστικό"
    s["H2"] = "VS11525"
    s["E2"] = 416.0
    s["K2"] = "=E2*2"
    t = w.create_sheet("Σβησμενα ")
    t["H1"] = "Barcode"
    o = BytesIO()
    w.save(o)
    return o.getvalue()


def repack_with_only_zip_comment(data: bytes) -> bytes:
    src = BytesIO(data)
    out = BytesIO()
    with ZipFile(src) as a, ZipFile(out, "w") as b:
        for item in a.infolist():
            b.writestr(item, a.read(item.filename))
        b.comment = b"OneDrive metadata only"
    return out.getvalue()


def test_same_excel_cells_after_package_rewrite():
    original = workbook_bytes()
    rewritten = repack_with_only_zip_comment(original)
    assert original != rewritten
    result = compare_workbook_cells(original, rewritten)
    assert result["cell_values_equal"] is True
    assert result["differences"] == 0
    assert result["sheet_names_equal"] is True


def test_real_barcode_change_detected_with_location():
    original = workbook_bytes()
    w = load_workbook(BytesIO(original))
    w["΄Ετοιμα προϊόντα"]["H2"] = "VS99999"
    out = BytesIO()
    w.save(out)
    result = compare_workbook_cells(original, out.getvalue())
    assert result["cell_values_equal"] is False
    assert result["differences"] >= 1
    assert any(d["cell"] == "H2" and d["online"].endswith("VS99999") for d in result["examples"])


def test_changed_formula_detected():
    original = workbook_bytes()
    w = load_workbook(BytesIO(original))
    w["΄Ετοιμα προϊόντα"]["K2"] = "=E2*3"
    out = BytesIO()
    w.save(out)
    result = compare_workbook_cells(original, out.getvalue())
    assert result["cell_values_equal"] is False
    assert any(d["cell"] == "K2" and "E2*3" in d["online"] for d in result["examples"])


def test_added_worksheet_is_detected():
    original = workbook_bytes()
    w = load_workbook(BytesIO(original))
    w.create_sheet("Επιπλέον")
    out = BytesIO()
    w.save(out)
    result = compare_workbook_cells(original, out.getvalue())
    assert result["sheet_names_equal"] is False
    assert result["cell_values_equal"] is False
