"""Ασφαλής μεταφορά γραμμής από το φύλλο πηγής στο φύλλο προορισμού.

Αρχή λειτουργίας
----------------
Το workbook είναι ZIP με XML parts. Αλλάζουμε ΜΟΝΟ:
  * το XML του φύλλου πηγής (αφαίρεση γραμμής),
  * το XML του φύλλου προορισμού (προσθήκη γραμμής στο τέλος των δεδομένων),
  * (αν χρειάζεται) definedNames στο workbook.xml,
  * το calcChain.xml αφαιρείται, ώστε το Excel να το ξαναχτίσει (τυπική, ασφαλής πρακτική).
Όλα τα υπόλοιπα parts (άλλα φύλλα, styles, form controls, VML, external links, printer
settings) αντιγράφονται αυτούσια.

Η γραμμή εντοπίζεται ξανά με barcode + fingerprint (όχι με αποθηκευμένο αριθμό γραμμής),
οπότε προηγούμενες διαγραφές δεν μπορούν να οδηγήσουν σε αλλαγή λάθος γραμμής.

Λειτουργίες αφαίρεσης από την πηγή
----------------------------------
* "shift": πραγματική διαγραφή γραμμής, με μετακίνηση των επόμενων γραμμών προς τα πάνω και
  προσαρμογή όλων των τύπων/περιοχών που επηρεάζονται (όπως η "Διαγραφή γραμμής" του Excel).
* "clear": ασφαλής εναλλακτική — καθαρίζονται οι τιμές και οι τύποι της γραμμής αλλά η γραμμή
  (και η μορφοποίησή της) μένει. Εφαρμόζεται μόνο όταν το "shift" δεν μπορεί να γίνει
  με βεβαιότητα, και επιστρέφεται προειδοποίηση.
"""
from __future__ import annotations

import html
import logging
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape as xml_escape

from app.excel.formula import FormulaShiftError, copy_formula, delete_row_in_formula, shrink_range_after_delete
from app.excel.package import XlsxPackage, iter_sheet_rows, row_fingerprint, split_ref
from app.excel.reader import BARCODE_COL, FINGERPRINT_COLUMNS
from app.services.normalize import normalize_barcode

log = logging.getLogger(__name__)


class TransferError(Exception):
    """Η μεταφορά δεν έγινε· το αρχείο δεν τροποποιήθηκε."""


class RowNotFoundError(TransferError):
    pass


class RowAmbiguousError(TransferError):
    pass


class UnsupportedStructureError(TransferError):
    pass


@dataclass
class TransferResult:
    mode: str
    source_row: int
    target_row: int
    warnings: list[str] = field(default_factory=list)
    modified_parts: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- XML helpers
_ROW_RE = re.compile(r"<row\b[^>]*?(?:/>|>.*?</row>)", re.S)
_CELL_RE = re.compile(r"<c\b[^>]*?(?:/>|>.*?</c>)", re.S)
_ATTR_R_RE = re.compile(r'\br="([A-Z]*)(\d+)"')
_F_RE = re.compile(r"<f\b([^>]*?)(?:/>|>(.*?)</f>)", re.S)

# Στοιχεία φύλλου που εξαρτώνται από αριθμούς γραμμών και δεν υποστηρίζονται στο "shift".
_UNSUPPORTED_FOR_SHIFT = (
    "<mergeCell", "<conditionalFormatting", "<dataValidation", "<hyperlink", "<tablePart",
    "<drawing ", "<legacyDrawing", "<controls", "<x14:", "<oleObjects", "<rowBreaks",
    "<protectedRange", "<scenarios", "<customSheetView", "<ignoredErrors", "<cellWatches",
)


@dataclass
class _SheetDoc:
    """Φύλλο σπασμένο σε: πριν το sheetData | γραμμές | μετά."""
    head: str
    rows: list[str]
    tail: str

    @classmethod
    def parse(cls, xml: str, label: str) -> "_SheetDoc":
        start = xml.find("<sheetData")
        if start < 0:
            raise UnsupportedStructureError(f"Το φύλλο {label} δεν έχει sheetData.")
        open_end = xml.find(">", start)
        if xml[open_end - 1] == "/":  # <sheetData/>
            return cls(xml[: open_end + 1].replace("<sheetData/>", "<sheetData>"), [], "</sheetData>" + xml[open_end + 1:])
        end = xml.find("</sheetData>", open_end)
        if end < 0:
            raise UnsupportedStructureError(f"Το φύλλο {label} έχει μη έγκυρο sheetData.")
        inner = xml[open_end + 1: end]
        rows = _ROW_RE.findall(inner)
        if "".join(rows) != inner:
            raise UnsupportedStructureError(f"Μη αναμενόμενη δομή γραμμών στο φύλλο {label}.")
        return cls(xml[: open_end + 1], rows, xml[end:])

    def render(self) -> str:
        return self.head + "".join(self.rows) + self.tail


def _row_number(row_xml: str) -> int:
    m = re.match(r'<row\b[^>]*?\br="(\d+)"', row_xml)
    if not m:
        raise UnsupportedStructureError("Γραμμή χωρίς αριθμό (r).")
    return int(m.group(1))


def _row_open_tag(row_xml: str) -> str:
    return row_xml[: row_xml.find(">") + 1]


def _row_cells(row_xml: str) -> list[str]:
    tag = _row_open_tag(row_xml)
    if tag.endswith("/>"):
        return []
    inner = row_xml[len(tag): -len("</row>")]
    cells = _CELL_RE.findall(inner)
    if "".join(cells) != inner:
        raise UnsupportedStructureError("Μη αναμενόμενη δομή κελιών.")
    return cells


def _row_has_content(row_xml: str) -> bool:
    return "<v>" in row_xml or "<v " in row_xml or "<f" in row_xml or "<is>" in row_xml


def _set_attr(tag: str, name: str, value: str | None) -> str:
    pattern = re.compile(r'\s%s="[^"]*"' % re.escape(name))
    if value is None:
        return pattern.sub("", tag)
    if pattern.search(tag):
        return pattern.sub(f' {name}="{value}"', tag, count=1)
    close = "/>" if tag.endswith("/>") else ">"
    return tag[: -len(close)] + f' {name}="{value}"' + close


def _cell_ref(cell_xml: str) -> tuple[str, int]:
    m = re.match(r'<c\b[^>]*?\br="([A-Z]+)(\d+)"', cell_xml)
    if not m:
        raise UnsupportedStructureError("Κελί χωρίς αναφορά (r).")
    return m.group(1), int(m.group(2))


def _rewrite_cell_formula(cell_xml: str, fn) -> str:
    """Εφαρμόζει fn(formula_text) -> formula_text στον τύπο του κελιού (αν υπάρχει)."""
    m = _F_RE.search(cell_xml)
    if not m:
        return cell_xml
    attrs, raw_text = m.group(1), m.group(2)
    if 't="shared"' in attrs or 't="array"' in attrs or 't="dataTable"' in attrs:
        raise FormulaShiftError("Shared/array formula — δεν υποστηρίζεται.")
    if raw_text is None:
        return cell_xml
    formula = html.unescape(raw_text)
    new_formula = fn(formula)
    if new_formula == formula:
        return cell_xml
    new_f = f"<f{attrs}>{xml_escape(new_formula)}</f>"
    return cell_xml[: m.start()] + new_f + cell_xml[m.end():]


def _renumber_cell(cell_xml: str, new_row: int) -> str:
    return re.sub(r'(<c\b[^>]*?\br=")([A-Z]+)\d+(")', lambda m: f"{m.group(1)}{m.group(2)}{new_row}{m.group(3)}", cell_xml, count=1)


def _renumber_row_tag(tag: str, new_row: int) -> str:
    return re.sub(r'(<row\b[^>]*?\br=")\d+(")', lambda m: f"{m.group(1)}{new_row}{m.group(2)}", tag, count=1)


# --------------------------------------------------------------------------- core
def _locate_row(pkg: XlsxPackage, sheet: str, barcode_norm: str, fingerprint: str) -> int:
    shared = pkg.shared_strings
    by_barcode: list[tuple[int, str]] = []
    for row in pkg.iter_rows(sheet):
        if row.number == 1:
            continue
        cell = row.cells.get(BARCODE_COL)
        val = cell.resolved(shared) if cell is not None else None
        if val is None or normalize_barcode(val) != barcode_norm:
            continue
        by_barcode.append((row.number, row_fingerprint(row, shared, FINGERPRINT_COLUMNS)))
    matches = [r for r, fp in by_barcode if fp == fingerprint]
    if not matches:
        if by_barcode:
            raise RowNotFoundError(
                "Η γραμμή του barcode στο Excel έχει αλλάξει από την εισαγωγή (διαφορετικά στοιχεία). "
                "Η μεταφορά ακυρώθηκε για ασφάλεια."
            )
        raise RowNotFoundError("Το barcode δεν βρέθηκε πλέον στο φύλλο πηγής (ίσως έχει ήδη μεταφερθεί).")
    if len(matches) > 1:
        raise RowAmbiguousError(
            f"Βρέθηκαν {len(matches)} πανομοιότυπες γραμμές για το barcode (γραμμές {matches}). "
            "Απαιτείται χειροκίνητος έλεγχος στο Excel."
        )
    return matches[0]


def _target_insert(doc: _SheetDoc) -> tuple[int, int, bool]:
    """Βρίσκει (index στη λίστα rows, αριθμό νέας γραμμής, αν αντικαθιστά κενή γραμμή)."""
    last_idx = -1
    for i in range(len(doc.rows) - 1, -1, -1):
        if _row_has_content(doc.rows[i]):
            last_idx = i
            break
    if last_idx < 0:
        raise UnsupportedStructureError("Το φύλλο προορισμού δεν έχει ούτε γραμμή επικεφαλίδων.")
    last_num = _row_number(doc.rows[last_idx])
    new_num = last_num + 1
    if last_idx + 1 < len(doc.rows) and _row_number(doc.rows[last_idx + 1]) == new_num:
        return last_idx + 1, new_num, True
    return last_idx + 1, new_num, False


def _build_target_row(source_row_xml: str, src_num: int, new_num: int, template_tag: str) -> str:
    delta = new_num - src_num
    cells = []
    for cell in _row_cells(source_row_xml):
        col, _r = _cell_ref(cell)
        new_cell = _renumber_cell(cell, new_num)
        try:
            new_cell = _rewrite_cell_formula(new_cell, lambda f: copy_formula(f, delta))
        except FormulaShiftError as exc:
            raise UnsupportedStructureError(f"Ο τύπος στο κελί {col}{src_num} δεν μπορεί να αντιγραφεί: {exc}") from exc
        cells.append(new_cell)
    tag = _renumber_row_tag(template_tag, new_num)
    for attr in ("hidden", "spans", "collapsed", "outlineLevel"):
        tag = _set_attr(tag, attr, None)
    if tag.endswith("/>"):
        tag = tag[:-2] + ">"
    return tag + "".join(cells) + "</row>"


def _update_dimension(head: str, fn) -> str:
    def repl(m: re.Match) -> str:
        return f'<dimension ref="{fn(m.group(1))}"'
    return re.sub(r'<dimension ref="([^"]+)"', repl, head, count=1)


def _sheet_check_for_shift(xml: str) -> list[str]:
    outside = xml[: xml.find("<sheetData")] + xml[xml.find("</sheetData>"):]
    return [tag.strip("< ") for tag in _UNSUPPORTED_FOR_SHIFT if tag in outside]


def _apply_shift(doc: _SheetDoc, src_idx: int, deleted: int, sheet_name: str) -> None:
    """Διαγραφή γραμμής + μετακίνηση επόμενων γραμμών + προσαρμογή τύπων του φύλλου."""
    new_rows: list[str] = []
    for i, row_xml in enumerate(doc.rows):
        if i == src_idx:
            continue
        num = _row_number(row_xml)
        if "<f" in row_xml and 't="shared"' in row_xml:
            raise FormulaShiftError("Το φύλλο πηγής περιέχει shared formulas.")
        tag = _row_open_tag(row_xml)
        cells = _row_cells(row_xml)
        new_num = num - 1 if num > deleted else num
        out_cells = []
        for cell in cells:
            c = _rewrite_cell_formula(cell, lambda f: delete_row_in_formula(f, deleted, True, sheet_name))
            if new_num != num:
                c = _renumber_cell(c, new_num)
            out_cells.append(c)
        if new_num != num:
            tag = _renumber_row_tag(tag, new_num)
        if tag.endswith("/>"):
            new_rows.append(tag)
        else:
            new_rows.append(tag + "".join(out_cells) + "</row>")
    doc.rows = new_rows

    # dimension, autoFilter, sortState, sortCondition
    doc.head = _update_dimension(doc.head, lambda ref: shrink_range_after_delete(ref, deleted))

    def tail_repl(m: re.Match) -> str:
        return f'{m.group(1)}ref="{shrink_range_after_delete(m.group(2), deleted)}"'

    doc.tail = re.sub(r'(<(?:autoFilter|sortState|sortCondition)\b[^>]*?\s)ref="([^"]+)"', tail_repl, doc.tail)


def _apply_clear(doc: _SheetDoc, src_idx: int) -> None:
    row_xml = doc.rows[src_idx]
    tag = _row_open_tag(row_xml)
    if 't="shared"' in row_xml or 't="array"' in row_xml:
        raise UnsupportedStructureError("Η γραμμή περιέχει shared/array τύπους· δεν μπορεί να καθαριστεί με ασφάλεια.")
    cells = []
    for cell in _row_cells(row_xml):
        col, num = _cell_ref(cell)
        s = re.search(r'\ss="(\d+)"', cell[: cell.find(">")])
        cells.append(f'<c r="{col}{num}"' + (f' s="{s.group(1)}"' if s else "") + "/>")
    doc.rows[src_idx] = (tag if not tag.endswith("/>") else tag[:-2] + ">") + "".join(cells) + "</row>"


def _rewrite_defined_names(workbook_xml: str, sheet_name: str, deleted: int) -> str:
    def repl(m: re.Match) -> str:
        attrs, text = m.group(1), m.group(2)
        formula = html.unescape(text)
        new = delete_row_in_formula(formula, deleted, False, sheet_name)
        if new == formula:
            return m.group(0)
        return f"<definedName{attrs}>{xml_escape(new)}</definedName>"

    return re.sub(r"<definedName\b([^>]*)>(.*?)</definedName>", repl, workbook_xml, flags=re.S)


def _external_references(pkg: XlsxPackage, source_part: str, target_part: str, sheet_name: str) -> list[str]:
    """Parts (εκτός πηγής) που μπορεί να έχουν ζωντανές αναφορές στο φύλλο πηγής."""
    hits = []
    for name in pkg.names:
        if name in (source_part, "xl/workbook.xml", "xl/sharedStrings.xml", "docProps/app.xml"):
            continue
        if not name.endswith(".xml"):
            continue
        if name.startswith(("xl/worksheets/", "xl/charts/", "xl/pivotCache/", "xl/pivotTables/", "xl/tables/")):
            if _mentions(pkg.read(name), sheet_name):
                hits.append(name)
    return hits


def _mentions(data: bytes, sheet_name: str) -> bool:
    """Ελέγχει αν το XML περιέχει το όνομα φύλλου, ακόμη κι αν είναι γραμμένο με XML entities."""
    if xml_escape(sheet_name).encode("utf-8") in data:
        return True
    if b"&#" in data:
        return sheet_name in html.unescape(data.decode("utf-8", errors="replace"))
    return False


def _rewrite_foreign_formulas(xml: str, sheet_name: str, deleted: int) -> str:
    """Προσαρμόζει τύπους άλλου φύλλου που δείχνουν στο φύλλο πηγής."""
    if not _mentions(xml.encode("utf-8"), sheet_name):
        return xml

    def repl(m: re.Match) -> str:
        attrs, raw_text = m.group(1), m.group(2)
        if raw_text is None:
            return m.group(0)
        formula = html.unescape(raw_text)
        if sheet_name not in formula:
            return m.group(0)
        new = delete_row_in_formula(formula, deleted, False, sheet_name)
        if new == formula:
            return m.group(0)
        if 't="shared"' in attrs or 't="array"' in attrs:
            raise FormulaShiftError("Shared/array τύπος άλλου φύλλου αναφέρεται στο φύλλο πηγής.")
        return f"<f{attrs}>{xml_escape(new)}</f>"

    return _F_RE.sub(repl, xml)


def _remove_calc_chain(parts: dict[str, bytes], pkg: XlsxPackage) -> set[str]:
    removed: set[str] = set()
    if "xl/calcChain.xml" not in pkg.names:
        return removed
    removed.add("xl/calcChain.xml")
    rels = parts.get("xl/_rels/workbook.xml.rels", pkg.read("xl/_rels/workbook.xml.rels")).decode("utf-8")
    rels = re.sub(r'<Relationship\b[^>]*?Target="(?:/xl/)?calcChain\.xml"[^>]*/>', "", rels)
    parts["xl/_rels/workbook.xml.rels"] = rels.encode("utf-8")
    ct = parts.get("[Content_Types].xml", pkg.read("[Content_Types].xml")).decode("utf-8")
    ct = re.sub(r'<Override\b[^>]*?PartName="/xl/calcChain\.xml"[^>]*/>', "", ct)
    parts["[Content_Types].xml"] = ct.encode("utf-8")
    return removed


def _write_zip(pkg: XlsxPackage, out_path: Path, parts: dict[str, bytes], removed: set[str]) -> None:
    with zipfile.ZipFile(out_path, "w") as zout:
        for info in pkg.zip.infolist():
            if info.filename in removed:
                continue
            data = parts[info.filename] if info.filename in parts else pkg.zip.read(info.filename)
            zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            zi.compress_type = info.compress_type
            zi.external_attr = info.external_attr
            zi.create_system = info.create_system
            zout.writestr(zi, data)


def _parse_row_fragment(sheet_head: str, row_xml: str):
    """Διαβάζει μία γραμμή XML χρησιμοποιώντας τα namespaces του φύλλου."""
    m = re.search(r"<worksheet\b[^>]*>", sheet_head)
    root_tag = m.group(0) if m else '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    wrapper = f"{root_tag}<sheetData>{row_xml}</sheetData></worksheet>".encode("utf-8")
    try:
        return next(iter(iter_sheet_rows(wrapper)))
    except (ET.ParseError, StopIteration) as exc:
        raise UnsupportedStructureError(f"Μη έγκυρη νέα γραμμή: {exc}") from exc


def _count_fp(xml_bytes: bytes, shared: list[str], barcode_norm: str, fingerprint: str) -> tuple[int, int]:
    """(# γραμμών με αυτό barcode+fingerprint, # γραμμών με barcode)."""
    same = 0
    total = 0
    for row in iter_sheet_rows(xml_bytes):
        if row.number == 1:
            continue
        c = row.cells.get(BARCODE_COL)
        v = c.resolved(shared) if c is not None else None
        if v:
            total += 1
            if normalize_barcode(v) == barcode_norm and row_fingerprint(row, shared, FINGERPRINT_COLUMNS) == fingerprint:
                same += 1
    return same, total


@dataclass
class Move:
    """Μία μετακίνηση γραμμής: από `from_sheet` (αφαίρεση) προς το τέλος του `to_sheet` (προσθήκη).

    Μεταφορά: «΄Ετοιμα προϊόντα» -> «Σβησμενα ». Επαναφορά: αντίστροφα.
    prefer_row: η αναμενόμενη γραμμή της πηγής (επαληθεύεται με barcode + fingerprint).
    pick_last: αν υπάρχουν πανομοιότυπες γραμμές, επιλέγεται η τελευταία (π.χ. επαναφορά πρόσφατης
    μεταφοράς από τα Σβησμένα, όπου μπορεί να υπάρχουν παλιές ίδιες εγγραφές).
    """
    from_sheet: str
    to_sheet: str
    barcode_norm: str
    fingerprint: str
    mode: str = "auto"
    prefer_row: int | None = None
    pick_last: bool = False


_FULL_COUNT_LIMIT = 4_000_000  # μέχρι αυτό το μέγεθος XML γίνεται πλήρης καταμέτρηση πριν/μετά


class WorkbookEditor:
    """Εφαρμόζει διαδοχικές μετακινήσεις γραμμών στη μνήμη και γράφει το αρχείο ΜΙΑ φορά.

    Κάθε μετακίνηση είναι ατομική: αν αποτύχει, η κατάσταση επιστρέφει όπως ήταν πριν από αυτήν
    (οι προηγούμενες μένουν). Όλα τα υπόλοιπα parts του πακέτου αντιγράφονται αυτούσια.
    """

    def __init__(self, src_path: Path):
        self.src_path = Path(src_path)
        self.pkg = XlsxPackage(self.src_path)
        self.shared = self.pkg.shared_strings
        self.docs: dict[str, _SheetDoc] = {}
        self.full_check: set[str] = set()      # parts με δομική αλλαγή -> πλήρης έλεγχος XML στο τέλος
        self.foreign: dict[str, str] = {}      # άλλα parts με ξαναγραμμένους τύπους
        self.workbook_xml = self.pkg.read("xl/workbook.xml").decode("utf-8")
        self._workbook_original = self.workbook_xml
        self.results: list[TransferResult] = []

    # -- βοηθητικά ------------------------------------------------------------
    def close(self) -> None:
        self.pkg.close()

    def __enter__(self) -> "WorkbookEditor":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _part(self, sheet: str) -> str:
        if sheet not in self.pkg.sheets:
            raise UnsupportedStructureError(f"Δεν βρέθηκε το φύλλο {sheet!r} στο Excel.")
        return self.pkg.sheets[sheet]

    def _doc(self, part: str, label: str) -> _SheetDoc:
        if part not in self.docs:
            self.docs[part] = _SheetDoc.parse(self.pkg.read(part).decode("utf-8"), label)
        return self.docs[part]

    def _current_xml(self, part: str) -> str:
        if part in self.docs:
            return self.docs[part].render()
        if part in self.foreign:
            return self.foreign[part]
        return self.pkg.read(part).decode("utf-8")

    def _row_obj(self, doc: _SheetDoc, row_xml: str):
        return _parse_row_fragment(doc.head, row_xml)

    def _row_matches(self, doc: _SheetDoc, row_xml: str, barcode_norm: str, fingerprint: str) -> bool:
        row = self._row_obj(doc, row_xml)
        cell = row.cells.get(BARCODE_COL)
        val = cell.resolved(self.shared) if cell is not None else None
        return (val is not None and normalize_barcode(val) == barcode_norm
                and row_fingerprint(row, self.shared, FINGERPRINT_COLUMNS) == fingerprint)

    def _locate(self, doc: _SheetDoc, m: Move) -> int:
        """Index της γραμμής στο doc.rows (επαλήθευση με barcode + fingerprint)."""
        if m.prefer_row is not None:
            for i, r in enumerate(doc.rows):
                if _row_number(r) == m.prefer_row:
                    if _row_has_content(r) and self._row_matches(doc, r, m.barcode_norm, m.fingerprint):
                        return i
                    break
        by_barcode: list[tuple[int, str]] = []
        for row in iter_sheet_rows(doc.render().encode("utf-8")):
            if row.number == 1:
                continue
            cell = row.cells.get(BARCODE_COL)
            val = cell.resolved(self.shared) if cell is not None else None
            if val is None or normalize_barcode(val) != m.barcode_norm:
                continue
            by_barcode.append((row.number, row_fingerprint(row, self.shared, FINGERPRINT_COLUMNS)))
        matches = [r for r, fp in by_barcode if fp == m.fingerprint]
        if not matches:
            if by_barcode:
                raise RowNotFoundError(
                    "Η γραμμή του barcode στο Excel έχει αλλάξει από την εισαγωγή (διαφορετικά στοιχεία). "
                    "Η ενέργεια ακυρώθηκε για ασφάλεια."
                )
            raise RowNotFoundError(f"Το barcode δεν βρέθηκε στο φύλλο «{m.from_sheet.strip()}».")
        if len(matches) > 1 and not m.pick_last:
            raise RowAmbiguousError(
                f"Βρέθηκαν {len(matches)} πανομοιότυπες γραμμές για το barcode (γραμμές {matches}). "
                "Απαιτείται χειροκίνητος έλεγχος στο Excel."
            )
        row_num = max(matches)
        idx = next((i for i, r in enumerate(doc.rows) if _row_number(r) == row_num), None)
        if idx is None:
            raise RowNotFoundError("Η γραμμή δεν βρέθηκε στο XML του φύλλου.")
        return idx

    def contains(self, sheet: str, barcode_norm: str, fingerprint: str) -> bool:
        """Υπάρχει στο φύλλο γραμμή με αυτό το barcode ΚΑΙ τα ίδια στοιχεία (fingerprint);"""
        doc = self._doc(self._part(sheet), repr(sheet))
        try:
            self._locate(doc, Move(sheet, sheet, barcode_norm, fingerprint, pick_last=True))
            return True
        except RowNotFoundError:
            return False

    # -- μία μετακίνηση --------------------------------------------------------
    def move(self, m: Move) -> TransferResult:
        if m.mode not in ("auto", "shift", "clear"):
            raise ValueError(m.mode)
        if m.from_sheet == m.to_sheet:
            raise ValueError("ίδιο φύλλο")
        src_part, tgt_part = self._part(m.from_sheet), self._part(m.to_sheet)
        src_doc = self._doc(src_part, repr(m.from_sheet))
        tgt_doc = self._doc(tgt_part, repr(m.to_sheet))
        snapshot = (
            {p: (d.head, list(d.rows), d.tail) for p, d in self.docs.items()},
            dict(self.foreign), self.workbook_xml, set(self.full_check),
        )
        try:
            result = self._move(m, src_part, tgt_part, src_doc, tgt_doc)
        except BaseException:
            docs, self.foreign, self.workbook_xml, self.full_check = snapshot
            for p, (head, rows, tail) in docs.items():
                self.docs[p].head, self.docs[p].rows, self.docs[p].tail = head, rows, tail
            raise
        self.results.append(result)
        return result

    def _move(self, m: Move, src_part: str, tgt_part: str, src_doc: _SheetDoc, tgt_doc: _SheetDoc) -> TransferResult:
        small_src = sum(len(r) for r in src_doc.rows) < _FULL_COUNT_LIMIT
        src_before = _count_fp(src_doc.render().encode("utf-8"), self.shared, m.barcode_norm, m.fingerprint) \
            if small_src else None
        src_idx = self._locate(src_doc, m)
        source_row_xml = src_doc.rows[src_idx]
        src_row_num = _row_number(source_row_xml)

        # 1) νέα γραμμή στο τέλος των δεδομένων του προορισμού
        ins_idx, new_num, replaces_empty = _target_insert(tgt_doc)
        template_tag = _row_open_tag(tgt_doc.rows[ins_idx]) if replaces_empty else _row_open_tag(tgt_doc.rows[ins_idx - 1])
        new_row_xml = _build_target_row(source_row_xml, src_row_num, new_num, template_tag)
        if replaces_empty:
            tgt_doc.rows[ins_idx] = new_row_xml
        else:
            tgt_doc.rows.insert(ins_idx, new_row_xml)

        def grow(ref: str) -> str:
            mm = re.match(r"^([A-Z]+)(\d+)(?::([A-Z]+)(\d+))?$", ref)
            if not mm:
                return ref
            if mm.group(3):
                return f"{mm.group(1)}{mm.group(2)}:{mm.group(3)}{max(int(mm.group(4)), new_num)}"
            return f"{mm.group(1)}{mm.group(2)}:{mm.group(1)}{max(int(mm.group(2)), new_num)}"

        tgt_doc.head = _update_dimension(tgt_doc.head, grow)
        warnings: list[str] = []

        # 2) αφαίρεση από την πηγή
        chosen = m.mode
        if m.mode in ("auto", "shift"):
            src_snapshot = (src_doc.head, list(src_doc.rows), src_doc.tail)
            foreign_before = dict(self.foreign)
            wb_before = self.workbook_xml
            full_before = set(self.full_check)
            try:
                reasons = _sheet_check_for_shift(src_doc.head + src_doc.tail)
                if reasons:
                    raise FormulaShiftError("Το φύλλο πηγής περιέχει: " + ", ".join(reasons))
                for part in self._foreign_parts(src_part, m.from_sheet):
                    if part.startswith(("xl/charts/", "xl/pivotCache/", "xl/pivotTables/", "xl/tables/")):
                        raise FormulaShiftError(f"Το {part} αναφέρεται στο φύλλο πηγής.")
                    if part in self.docs:
                        # Φύλλο που ήδη επεξεργαζόμαστε: μόνο οι γραμμές που αναφέρουν το φύλλο πηγής.
                        doc = self.docs[part]
                        for i, row_xml in enumerate(doc.rows):
                            if _mentions(row_xml.encode("utf-8"), m.from_sheet):
                                new_row = _rewrite_foreign_formulas(row_xml, m.from_sheet, src_row_num)
                                if new_row != row_xml:
                                    doc.rows[i] = new_row
                                    self.full_check.add(part)
                        continue
                    base = self._current_xml(part)
                    new_xml = _rewrite_foreign_formulas(base, m.from_sheet, src_row_num)
                    if new_xml != base:
                        self.foreign[part] = new_xml
                        self.full_check.add(part)
                self.workbook_xml = _rewrite_defined_names(self.workbook_xml, m.from_sheet, src_row_num)
                _apply_shift(src_doc, src_idx, src_row_num, m.from_sheet)
                self.full_check.add(src_part)
                chosen = "shift"
            except FormulaShiftError as exc:
                if m.mode == "shift":
                    raise UnsupportedStructureError(f"Η φυσική αφαίρεση γραμμής δεν είναι ασφαλής: {exc}") from exc
                src_doc.head, src_doc.rows, src_doc.tail = src_snapshot
                self.foreign, self.workbook_xml, self.full_check = foreign_before, wb_before, full_before
                _apply_clear(src_doc, src_idx)
                chosen = "clear"
                warnings.append(
                    "Η γραμμή δεν διαγράφηκε φυσικά από το φύλλο πηγής γιατί αυτό δεν ήταν ασφαλές "
                    f"({exc}). Αντιγράφηκε στο φύλλο προορισμού και η αρχική γραμμή {src_row_num} "
                    "καθαρίστηκε (έμεινε κενή, με τη μορφοποίησή της)."
                )
        else:
            _apply_clear(src_doc, src_idx)

        # 3) επαλήθευση της μετακίνησης
        if src_before is not None:
            src_after = _count_fp(src_doc.render().encode("utf-8"), self.shared, m.barcode_norm, m.fingerprint)
            if src_after != (src_before[0] - 1, src_before[1] - 1):
                raise UnsupportedStructureError("Επαλήθευση απέτυχε: η γραμμή δεν αφαιρέθηκε σωστά από την πηγή.")
        elif chosen == "clear":
            cleared = src_doc.rows[src_idx]
            if _row_has_content(cleared) or _row_number(cleared) != src_row_num:
                raise UnsupportedStructureError("Επαλήθευση απέτυχε: η γραμμή της πηγής δεν καθαρίστηκε.")
            self._row_obj(src_doc, cleared)  # έγκυρο XML
        else:
            self.full_check.add(src_part)
        new_row = self._row_obj(tgt_doc, new_row_xml)
        bc_cell = new_row.cells.get(BARCODE_COL)
        bc_val = bc_cell.resolved(self.shared) if bc_cell is not None else None
        if (new_row.number != new_num or bc_val is None or normalize_barcode(bc_val) != m.barcode_norm
                or row_fingerprint(new_row, self.shared, FINGERPRINT_COLUMNS) != m.fingerprint):
            raise UnsupportedStructureError("Επαλήθευση απέτυχε: η γραμμή δεν αντιγράφηκε σωστά στον προορισμό.")
        if sum(1 for r in tgt_doc.rows if _row_number(r) == new_num) != 1:
            raise UnsupportedStructureError("Επαλήθευση απέτυχε: διπλός αριθμός γραμμής στον προορισμό.")
        log.info("Move prepared: %s row %s -> %s row %s (%s)", m.from_sheet, src_row_num, m.to_sheet, new_num, chosen)
        return TransferResult(mode=chosen, source_row=src_row_num, target_row=new_num, warnings=warnings)

    def _foreign_parts(self, source_part: str, sheet_name: str) -> list[str]:
        key = (source_part, sheet_name)
        cache = self.__dict__.setdefault("_foreign_cache", {})
        if key in cache:
            return cache[key]
        hits = []
        for name in self.pkg.names:
            if name in (source_part, "xl/workbook.xml", "xl/sharedStrings.xml", "docProps/app.xml"):
                continue
            if not name.endswith(".xml"):
                continue
            if name.startswith(("xl/worksheets/", "xl/charts/", "xl/pivotCache/", "xl/pivotTables/", "xl/tables/")):
                # Οι γραμμές που προστίθενται είναι αντίγραφα γραμμών του workbook: ο έλεγχος στο
                # αρχικό περιεχόμενο αρκεί (cache ανά φύλλο πηγής για τις μαζικές μετακινήσεις).
                if _mentions(self.pkg.read(name), sheet_name) or (
                        name in self.foreign and _mentions(self.foreign[name].encode("utf-8"), sheet_name)):
                    hits.append(name)
        cache[key] = hits
        return hits

    # -- εγγραφή ---------------------------------------------------------------
    def finish(self, out_path: Path) -> list[str]:
        """Γράφει το νέο workbook (μία φορά) και επαληθεύει ότι ανοίγει. Επιστρέφει τα αλλαγμένα parts."""
        parts: dict[str, bytes] = {}
        for part, doc in self.docs.items():
            xml = doc.render()
            if xml.encode("utf-8") != self.pkg.read(part):
                parts[part] = xml.encode("utf-8")
        for part, xml in self.foreign.items():
            parts[part] = xml.encode("utf-8")
        if self.workbook_xml != self._workbook_original:
            parts["xl/workbook.xml"] = self.workbook_xml.encode("utf-8")
        for part, data in parts.items():
            # Φύλλα όπου μόνο προστέθηκαν/καθαρίστηκαν γραμμές έχουν ήδη ελεγχθεί γραμμή-γραμμή.
            if part in self.docs and part not in self.full_check:
                continue
            try:
                ET.fromstring(data)
            except ET.ParseError as exc:
                raise UnsupportedStructureError(f"Εσωτερικό σφάλμα: μη έγκυρο XML στο {part}: {exc}") from exc
        removed = _remove_calc_chain(parts, self.pkg) if parts else set()
        _write_zip(self.pkg, Path(out_path), parts, removed)
        with XlsxPackage(out_path) as check:
            if set(check.sheets) != set(self.pkg.sheets):
                raise UnsupportedStructureError("Επαλήθευση απέτυχε: χάθηκαν φύλλα.")
            _ = check.shared_strings
        return sorted(parts) + [f"-{p}" for p in sorted(removed)]


def apply_moves(src_path: Path, out_path: Path, moves: list[Move], stop_on_error: bool = True
                ) -> list[TransferResult | TransferError]:
    """Εφαρμόζει πολλές μετακινήσεις με τη σειρά και γράφει το out_path μία φορά.

    stop_on_error=False: μια μετακίνηση που αποτυγχάνει παραλείπεται (επιστρέφεται το σφάλμα της)
    και οι υπόλοιπες συνεχίζουν. Το src_path ΔΕΝ αλλάζει ποτέ.
    """
    out: list[TransferResult | TransferError] = []
    with WorkbookEditor(src_path) as ed:
        for m in moves:
            try:
                out.append(ed.move(m))
            except TransferError as exc:
                if stop_on_error:
                    raise
                out.append(exc)
        ed.finish(out_path)
    return out


def transfer_row(
    src_path: Path,
    out_path: Path,
    source_sheet: str,
    target_sheet: str,
    barcode_norm: str,
    fingerprint: str,
    mode: str = "auto",
) -> TransferResult:
    """Γράφει στο out_path νέο workbook με τη γραμμή μεταφερμένη. Το src_path ΔΕΝ αλλάζει."""
    if mode not in ("auto", "shift", "clear"):
        raise ValueError(mode)
    with WorkbookEditor(src_path) as ed:
        if source_sheet not in ed.pkg.sheets or target_sheet not in ed.pkg.sheets:
            raise UnsupportedStructureError("Δεν βρέθηκαν τα φύλλα πηγής/προορισμού στο ενεργό Excel.")
        res = ed.move(Move(source_sheet, target_sheet, barcode_norm, fingerprint, mode=mode))
        res.modified_parts = ed.finish(out_path)
    return res
