"""Μετατόπιση αναφορών κελιών σε τύπους Excel.

Δύο λειτουργίες:
* copy_formula: όπως το copy/paste του Excel (σχετικές γραμμές μετακινούνται κατά delta,
  απόλυτες `$` μένουν ίδιες).
* delete_row_in_formula: όπως η "Διαγραφή γραμμής" του Excel (αναφορές του ίδιου φύλλου
  κάτω από τη διαγραμμένη γραμμή ανεβαίνουν κατά 1, ανεξάρτητα από `$`).

Για ασφάλεια, όταν κάτι δεν αναγνωρίζεται με βεβαιότητα, γίνεται raise FormulaShiftError
και ο καλών εφαρμόζει ασφαλή εναλλακτική αντί να αλλοιώσει το αρχείο.
"""
from __future__ import annotations

import re

from openpyxl.formula.tokenizer import Token, Tokenizer

MAX_ROWS = 1_048_576


class FormulaShiftError(Exception):
    pass


_CELL_RE = re.compile(r"^(\$?)([A-Za-z]{1,3})(\$?)(\d+)$")
_ROWS_RE = re.compile(r"^(\$?)(\d+)$")
_COLS_RE = re.compile(r"^\$?[A-Za-z]{1,3}$")


def _split_sheet(ref: str) -> tuple[str | None, str]:
    """Χωρίζει 'Sheet'!A1 σε (prefix, 'A1'). prefix=None όταν δεν υπάρχει φύλλο."""
    in_quote = False
    depth = 0
    last_bang = -1
    i = 0
    while i < len(ref):
        ch = ref[i]
        if ch == "'":
            if in_quote and i + 1 < len(ref) and ref[i + 1] == "'":
                i += 2
                continue
            in_quote = not in_quote
        elif not in_quote and ch == "[":
            depth += 1
        elif not in_quote and ch == "]":
            depth -= 1
        elif ch == "!" and not in_quote and depth == 0:
            last_bang = i
        i += 1
    if last_bang < 0:
        return None, ref
    return ref[:last_bang], ref[last_bang + 1:]


def sheet_name_from_prefix(prefix: str) -> tuple[bool, str]:
    """Επιστρέφει (is_external, όνομα φύλλου) από prefix όπως 'Name' ή [1]Sheet."""
    external = prefix.startswith("[") or prefix.startswith("'[")
    p = prefix
    if p.startswith("'") and p.endswith("'"):
        p = p[1:-1].replace("''", "'")
    if p.startswith("["):
        close = p.find("]")
        p = p[close + 1:]
    return external, p


def _transform_ref(ref: str, fn) -> str:
    """Εφαρμόζει fn(row:int, absolute:bool) -> int|None σε κάθε γραμμή μιας αναφοράς."""
    parts = ref.split(":")
    if len(parts) > 2:
        raise FormulaShiftError(f"Μη υποστηριζόμενη αναφορά: {ref}")
    out = []
    kinds = []
    for part in parts:
        m = _CELL_RE.match(part)
        if m:
            kinds.append("cell")
            out.append((m, None))
            continue
        m2 = _ROWS_RE.match(part)
        if m2 and len(parts) == 2:
            kinds.append("row")
            out.append((None, m2))
            continue
        if _COLS_RE.match(part) and len(parts) == 2:
            kinds.append("col")
            out.append((None, None))
            continue
        return ref  # όνομα, structured reference, #REF! κ.λπ. — δεν αφορά γραμμές
    if "col" in kinds:
        if any(k != "col" for k in kinds):
            raise FormulaShiftError(f"Μη υποστηριζόμενη αναφορά: {ref}")
        return ref  # ολόκληρες στήλες: δεν επηρεάζονται

    rows = []
    for (mc, mr) in out:
        if mc is not None:
            rows.append((int(mc.group(4)), mc.group(3) == "$"))
        else:
            rows.append((int(mr.group(2)), mr.group(1) == "$"))
    new_rows = fn(rows)
    pieces = []
    for (mc, mr), new_row in zip(out, new_rows):
        if mc is not None:
            pieces.append(f"{mc.group(1)}{mc.group(2)}{mc.group(3)}{new_row}")
        else:
            pieces.append(f"{mr.group(1)}{new_row}")
    return ":".join(pieces)


def _rewrite(formula: str, ref_fn) -> str:
    """Tokenize, εφαρμογή ref_fn σε κάθε RANGE operand, επανασύνθεση."""
    text = formula if formula.startswith("=") else "=" + formula
    tok = Tokenizer(text)
    if tok.render() != text:
        raise FormulaShiftError("Ο τύπος δεν αναπαράγεται με ασφάλεια από τον tokenizer.")
    changed = False
    for t in tok.items:
        if t.type == Token.OPERAND and t.subtype == Token.RANGE:
            new_value = ref_fn(t.value)
            if new_value != t.value:
                t.value = new_value
                changed = True
    if not changed:
        return formula
    rendered = tok.render()
    return rendered if formula.startswith("=") else rendered[1:]


def copy_formula(formula: str, delta: int) -> str:
    """Αντιγραφή τύπου delta γραμμές πιο κάτω (σχετικές αναφορές μετακινούνται)."""
    if delta == 0:
        return formula

    def rows_fn(rows):
        res = []
        for r, absolute in rows:
            nr = r if absolute else r + delta
            if nr < 1 or nr > MAX_ROWS:
                raise FormulaShiftError("Η αντιγραφή του τύπου βγαίνει εκτός ορίων φύλλου.")
            res.append(nr)
        return res

    def ref_fn(value: str) -> str:
        prefix, ref = _split_sheet(value)
        new_ref = _transform_ref(ref, rows_fn)
        return new_ref if prefix is None else f"{prefix}!{new_ref}"

    return _rewrite(formula, ref_fn)


def delete_row_in_formula(formula: str, deleted_row: int, same_sheet: bool, sheet_name: str) -> str:
    """Προσαρμογή τύπου μετά από διαγραφή της γραμμής deleted_row του φύλλου sheet_name.

    same_sheet=True όταν ο τύπος βρίσκεται στο ίδιο φύλλο (μη-προσδιορισμένες αναφορές αφορούν αυτό).
    """

    def rows_fn(rows):
        if len(rows) == 1:
            r, _abs = rows[0]
            if r == deleted_row:
                raise FormulaShiftError("Ο τύπος αναφέρεται στη γραμμή που αφαιρείται.")
            return [r - 1 if r > deleted_row else r]
        (start, _a1), (end, _a2) = rows
        if start > end:
            start, end = end, start
        if start == end == deleted_row:
            raise FormulaShiftError("Ο τύπος αναφέρεται μόνο στη γραμμή που αφαιρείται.")
        new_start = start - 1 if start > deleted_row else start
        new_end = end - 1 if end >= deleted_row else end
        return [new_start, new_end]

    def ref_fn(value: str) -> str:
        prefix, ref = _split_sheet(value)
        if prefix is None:
            if not same_sheet:
                return value
        else:
            external, name = sheet_name_from_prefix(prefix)
            if external or name != sheet_name:
                return value
        new_ref = _transform_ref(ref, rows_fn)
        return new_ref if prefix is None else f"{prefix}!{new_ref}"

    return _rewrite(formula, ref_fn)


def shrink_range_after_delete(ref: str, deleted_row: int) -> str:
    """Για autoFilter/dimension/definedName: προσαρμογή περιοχής μετά τη διαγραφή γραμμής."""
    try:
        def rows_fn(rows):
            if len(rows) == 1:
                r, _ = rows[0]
                return [r - 1 if r > deleted_row else r]
            (start, _a), (end, _b) = rows
            new_start = start - 1 if start > deleted_row else start
            new_end = end - 1 if end >= deleted_row and end > new_start else end
            return [new_start, max(new_end, new_start)]
        prefix, r = _split_sheet(ref)
        new_ref = _transform_ref(r, rows_fn)
        return new_ref if prefix is None else f"{prefix}!{new_ref}"
    except FormulaShiftError:
        return ref
