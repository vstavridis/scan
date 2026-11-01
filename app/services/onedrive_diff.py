"""Read-only, bounded Excel worksheet comparison for OneDrive conflict diagnostics.

Compares every worksheet's nonempty cell values and formulas, irrespective of XLSX
ZIP parts, shared-string indices, or cached formula calculations. This is a
DIAGNOSTIC, NOT proof that workbooks are interchangeable: formatting, tables,
drawings, VBA/extensions, and workbook-level metadata are not compared.
Never treat a match as permission to overwrite a remote file automatically.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from app.excel.package import Row, XlsxPackage


MAX_DIFFS = 20
MAX_TOTAL_DIFFERENCES = 10_000


def _signature(row: Row | None, shared: list[str]) -> dict[str, tuple[str, str]]:
    if row is None:
        return {}
    out: dict[str, tuple[str, str]] = {}
    for col, cell in row.cells.items():
        if cell.formula is not None or cell.shared_formula:
            # Cached formula results may be recalculated by Excel Online.
            value = ("Τύπος", cell.formula or "(κοινόχρηστος τύπος)")
        else:
            raw = cell.resolved(shared)
            if raw in (None, ""):
                continue
            if cell.t in ("n", None) and raw is not None:
                try:
                    val = Decimal(raw)
                    if val.is_finite():
                        raw = format(val.normalize(), "f")
                except InvalidOperation:
                    pass
            value = ("Τιμή", raw)
        out[col] = value
    return out


def compare_workbook_cells(left: bytes, right: bytes, *, limit: int = MAX_DIFFS) -> dict[str, Any]:
    """Compare *all* sheet cell values/formulas, report bounded diagnostic samples.

    This comparison intentionally excludes formatting, named ranges, drawings,
    comments, tables and package parts. Even identical cells must NOT trigger an
    automatic overwrite: preserving non-cell changes is still important.
    """
    if not 1 <= limit <= 100:
        raise ValueError("limit must be 1..100")
    with TemporaryDirectory(prefix="onedrive-compare-") as temp:
        a_path = Path(temp) / "baseline.xlsx"
        b_path = Path(temp) / "online.xlsx"
        a_path.write_bytes(left)
        b_path.write_bytes(right)
        with XlsxPackage(a_path) as a, XlsxPackage(b_path) as b:
            left_names = list(a.sheets)
            right_names = list(b.sheets)
            sheet_names_equal = left_names == right_names
            total = 0
            examples: list[dict[str, str | int]] = []
            per_sheet = []
            truncated = False
            for name in sorted(set(left_names) | set(right_names)):
                if name not in a.sheets or name not in b.sheets:
                    per_sheet.append({"sheet": name, "different": "Διαφορετική παρουσία φύλλου"})
                    total += 1
                    if len(examples) < limit:
                        examples.append({"sheet": name, "cell": "—", "before": "Υπάρχει" if name in a.sheets else "Απουσιάζει",
                                         "online": "Υπάρχει" if name in b.sheets else "Απουσιάζει"})
                    continue
                a_rows = iter(a.iter_rows(name))
                b_rows = iter(b.iter_rows(name))
                ra = next(a_rows, None)
                rb = next(b_rows, None)
                count = 0
                a_shared = a.shared_strings
                b_shared = b.shared_strings
                while ra is not None or rb is not None:
                    rownum = min(ra.number if ra else 10**12, rb.number if rb else 10**12)
                    ca = _signature(ra if ra and ra.number == rownum else None, a_shared)
                    cb = _signature(rb if rb and rb.number == rownum else None, b_shared)
                    for col in sorted(set(ca) | set(cb)):
                        av, bv = ca.get(col), cb.get(col)
                        if av == bv:
                            continue
                        total += 1
                        count += 1
                        if len(examples) < limit:
                            def fmt(item):
                                if not item:
                                    return "(κενό)"
                                return (item[0] + ": " + item[1])[:140]
                            examples.append({"sheet": name, "cell": f"{col}{rownum}",
                                             "before": fmt(av), "online": fmt(bv)})
                        if total >= MAX_TOTAL_DIFFERENCES:
                            truncated = True
                            break
                    if total >= MAX_TOTAL_DIFFERENCES:
                        break
                    if ra is not None and ra.number == rownum:
                        ra = next(a_rows, None)
                    if rb is not None and rb.number == rownum:
                        rb = next(b_rows, None)
                per_sheet.append({"sheet": name, "different": count})
                if truncated:
                    break
            return {"sheet_names_equal": sheet_names_equal, "cell_values_equal": total == 0 and not truncated,
                    "differences": total, "truncated": truncated,
                    "examples": examples, "per_sheet": per_sheet,
                    "notes": ("Ελέγχθηκαν τιμές και τύποι κελιών όλων των φύλλων. "
                              "Δεν συγκρίθηκαν μορφοποιήσεις, σχόλια, πίνακες, εικόνες "
                              "ή άλλες εσωτερικές δομές του Excel.")}
