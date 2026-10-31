"""Μονάδες: μετατόπιση αναφορών τύπων (copy / delete row)."""
from __future__ import annotations

import pytest

from app.excel.formula import FormulaShiftError, copy_formula, delete_row_in_formula, shrink_range_after_delete

SHEET = "΄Ετοιμα προϊόντα"
REAL = [
    'IFERROR(INDEX([1]!Table4[ΠΟΙΟΤΗΤΑ],MATCH(K157,[1]!Table4[Νο ΡΟΛΛΟΥ],0)),"-")',
    'IFERROR(VLOOKUP(K157,[1]ΣΒΗΣΜΕΝΑ!$F:$G,2,),"-")',
    'IFERROR(VLOOKUP(F157,ID!$A:$B,2,0),"-")',
]


def test_copy_formula_moves_relative_rows_only():
    assert copy_formula(REAL[0], 22030 - 157) == REAL[0].replace("K157", "K22030")
    assert copy_formula(REAL[1], 10) == REAL[1].replace("K157", "K167")   # $F:$G δεν αλλάζει
    assert copy_formula(REAL[2], -1) == REAL[2].replace("F157", "F156")
    assert copy_formula("$A$1+A$2+$B3", 5) == "$A$1+A$2+$B8"
    assert copy_formula('"K157"&K157', 1) == '"K157"&K158'                  # κείμενο σε εισαγωγικά μένει


def test_delete_row_semantics():
    assert delete_row_in_formula("K158*2", 157, True, SHEET) == "K157*2"
    assert delete_row_in_formula("K156*2", 157, True, SHEET) == "K156*2"
    assert delete_row_in_formula("SUM(E2:E300)", 157, True, SHEET) == "SUM(E2:E299)"
    assert delete_row_in_formula("SUM($E$158:$E$300)", 157, True, SHEET) == "SUM($E$157:$E$299)"
    assert delete_row_in_formula("SUM(E157:E300)", 157, True, SHEET) == "SUM(E157:E299)"
    assert delete_row_in_formula(f"SUM('{SHEET}'!E2:E400)+Other!E400", 157, False, SHEET) == \
        f"SUM('{SHEET}'!E2:E399)+Other!E400"
    assert delete_row_in_formula("Other!E400+E400", 157, False, SHEET) == "Other!E400+E400"
    with pytest.raises(FormulaShiftError):
        delete_row_in_formula("K157", 157, True, SHEET)
    with pytest.raises(FormulaShiftError):
        delete_row_in_formula(f"'{SHEET}'!H157", 157, False, SHEET)


def test_ranges_for_filters():
    assert shrink_range_after_delete("A1:P259", 157) == "A1:P258"
    assert shrink_range_after_delete("A1:P100", 157) == "A1:P100"
    assert shrink_range_after_delete(f"'{SHEET}'!$A$1:$P$236", 157) == f"'{SHEET}'!$A$1:$P$235"
