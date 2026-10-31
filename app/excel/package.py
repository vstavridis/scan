"""Χαμηλού επιπέδου ανάγνωση πακέτου .xlsx (ZIP + XML).

Δεν χρησιμοποιούμε openpyxl για την εγγραφή, γιατί το openpyxl ξαναγράφει
ολόκληρο το workbook και χάνει form controls, VML, ορισμένα extLst κ.λπ.
Εδώ διαβάζουμε απευθείας τα XML parts, ώστε η ανάγνωση (import) και η
εγγραφή (transfer) να βλέπουν ακριβώς τα ίδια δεδομένα.
"""
from __future__ import annotations

import hashlib
import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree as ET

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_M = "{%s}" % NS_MAIN


class XlsxFormatError(Exception):
    """Το αρχείο δεν είναι έγκυρο .xlsx ή έχει μη αναμενόμενη δομή."""


# --------------------------------------------------------------------------- helpers
_CELL_REF_RE = re.compile(r"^([A-Z]{1,3})(\d+)$")


def col_to_index(col: str) -> int:
    n = 0
    for ch in col:
        n = n * 26 + (ord(ch) - 64)
    return n


def index_to_col(idx: int) -> str:
    s = ""
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        s = chr(65 + rem) + s
    return s


def split_ref(ref: str) -> tuple[str, int]:
    m = _CELL_REF_RE.match(ref)
    if not m:
        raise XlsxFormatError(f"Μη έγκυρη αναφορά κελιού: {ref}")
    return m.group(1), int(m.group(2))


@dataclass
class Cell:
    col: str
    row: int
    t: str | None          # τύπος κελιού (s, str, n, b, e, inlineStr)
    raw: str | None        # ακατέργαστη τιμή (<v>, ή κείμενο inlineStr)
    formula: str | None    # κείμενο τύπου (χωρίς '=')
    style: int             # index στο cellXfs
    shared_formula: bool = False

    def resolved(self, shared: list[str]) -> str | None:
        """Η 'ακατέργαστη' τιμή ως κείμενο (shared strings επιλυμένα)."""
        if self.raw is None:
            return None
        if self.t == "s":
            try:
                return shared[int(self.raw)]
            except (ValueError, IndexError) as exc:
                raise XlsxFormatError(f"Μη έγκυρο shared string στο {self.col}{self.row}") from exc
        return self.raw


@dataclass
class Row:
    number: int
    cells: dict[str, Cell] = field(default_factory=dict)
    hidden: bool = False

    def has_content(self) -> bool:
        return any(c.raw not in (None, "") or c.formula for c in self.cells.values())


@dataclass
class Styles:
    cell_numfmt: list[int]
    custom_formats: dict[int, str]

    def format_code(self, style_index: int) -> str:
        try:
            fid = self.cell_numfmt[style_index]
        except IndexError:
            return "General"
        if fid in self.custom_formats:
            return self.custom_formats[fid]
        return BUILTIN_FORMATS.get(fid, "General")


BUILTIN_FORMATS: dict[int, str] = {
    0: "General", 1: "0", 2: "0.00", 3: "#,##0", 4: "#,##0.00", 9: "0%", 10: "0.00%",
    11: "0.00E+00", 12: "# ?/?", 13: "# ??/??", 14: "mm-dd-yy", 15: "d-mmm-yy", 16: "d-mmm",
    17: "mmm-yy", 18: "h:mm AM/PM", 19: "h:mm:ss AM/PM", 20: "h:mm", 21: "h:mm:ss",
    22: "m/d/yy h:mm", 37: "#,##0 ;(#,##0)", 38: "#,##0 ;[Red](#,##0)", 39: "#,##0.00;(#,##0.00)",
    40: "#,##0.00;[Red](#,##0.00)", 45: "mm:ss", 46: "[h]:mm:ss", 47: "mmss.0", 48: "##0.0E+0", 49: "@",
}


# --------------------------------------------------------------------------- package
class XlsxPackage:
    """Read-only πρόσβαση σε ένα .xlsx."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        try:
            self.zip = zipfile.ZipFile(self.path, "r")
        except (zipfile.BadZipFile, OSError) as exc:
            raise XlsxFormatError("Το αρχείο δεν είναι έγκυρο .xlsx (ZIP).") from exc
        self.names = set(self.zip.namelist())
        if "xl/workbook.xml" not in self.names:
            self.zip.close()
            raise XlsxFormatError("Λείπει το xl/workbook.xml — δεν είναι αρχείο Excel .xlsx.")
        self._shared: list[str] | None = None
        self._styles: Styles | None = None
        self.sheets: dict[str, str] = self._read_sheet_map()  # όνομα -> part path

    def close(self) -> None:
        self.zip.close()

    def __enter__(self) -> "XlsxPackage":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def read(self, part: str) -> bytes:
        return self.zip.read(part)

    # -- workbook ----------------------------------------------------------
    def _read_sheet_map(self) -> dict[str, str]:
        wb = ET.fromstring(self.read("xl/workbook.xml"))
        rels = ET.fromstring(self.read("xl/_rels/workbook.xml.rels"))
        targets: dict[str, str] = {}
        for rel in rels.findall("{%s}Relationship" % NS_PKG_REL):
            target = rel.get("Target", "")
            if target.startswith("/"):
                part = target.lstrip("/")
            else:
                part = posixpath.normpath(posixpath.join("xl", target))
            targets[rel.get("Id", "")] = part
        out: dict[str, str] = {}
        sheets = wb.find(_M + "sheets")
        if sheets is None:
            raise XlsxFormatError("Το workbook δεν περιέχει φύλλα.")
        for sh in sheets.findall(_M + "sheet"):
            rid = sh.get("{%s}id" % NS_REL)
            name = sh.get("name")
            if name is None or rid not in targets:
                continue
            out[name] = targets[rid]
        return out

    def sheet_index(self, name: str) -> int:
        """Θέση (0-based) του φύλλου στη σειρά του workbook (localSheetId)."""
        return list(self.sheets).index(name)

    # -- shared strings / styles ------------------------------------------
    @property
    def shared_strings(self) -> list[str]:
        if self._shared is None:
            self._shared = parse_shared_strings(self.read("xl/sharedStrings.xml")) if "xl/sharedStrings.xml" in self.names else []
        return self._shared

    @property
    def styles(self) -> Styles:
        if self._styles is None:
            self._styles = parse_styles(self.read("xl/styles.xml")) if "xl/styles.xml" in self.names else Styles([], {})
        return self._styles

    # -- sheets --------------------------------------------------------------
    def sheet_xml(self, name: str) -> bytes:
        if name not in self.sheets:
            raise XlsxFormatError(f"Δεν βρέθηκε το φύλλο {name!r}.")
        return self.read(self.sheets[name])

    def iter_rows(self, name: str, max_row: int | None = None) -> Iterable[Row]:
        return iter_sheet_rows(self.sheet_xml(name), max_row=max_row)


def parse_shared_strings(data: bytes) -> list[str]:
    out: list[str] = []
    root = ET.fromstring(data)
    for si in root.findall(_M + "si"):
        # Συνένωση όλων των <t> εκτός από φωνητικά (rPh).
        parts: list[str] = []
        for child in si:
            if child.tag == _M + "t":
                parts.append(child.text or "")
            elif child.tag == _M + "r":
                t = child.find(_M + "t")
                if t is not None:
                    parts.append(t.text or "")
        out.append("".join(parts))
    return out


def parse_styles(data: bytes) -> Styles:
    root = ET.fromstring(data)
    custom: dict[int, str] = {}
    numfmts = root.find(_M + "numFmts")
    if numfmts is not None:
        for nf in numfmts.findall(_M + "numFmt"):
            custom[int(nf.get("numFmtId", "0"))] = nf.get("formatCode", "General")
    cell_numfmt: list[int] = []
    xfs = root.find(_M + "cellXfs")
    if xfs is not None:
        for xf in xfs.findall(_M + "xf"):
            cell_numfmt.append(int(xf.get("numFmtId", "0")))
    return Styles(cell_numfmt, custom)


def iter_sheet_rows(data: bytes, max_row: int | None = None) -> Iterable[Row]:
    """Διαβάζει γραμμές με iterparse (χαμηλή μνήμη ακόμη και για μεγάλα φύλλα)."""
    import io

    context = ET.iterparse(io.BytesIO(data), events=("end",))
    for _event, elem in context:
        if elem.tag != _M + "row":
            continue
        rnum = int(elem.get("r", "0"))
        if max_row is not None and rnum > max_row:
            break
        row = Row(number=rnum, hidden=elem.get("hidden") in ("1", "true"))
        for c in elem.findall(_M + "c"):
            ref = c.get("r", "")
            col, _r = split_ref(ref)
            t = c.get("t")
            f = c.find(_M + "f")
            v = c.find(_M + "v")
            raw: str | None
            if t == "inlineStr":
                is_ = c.find(_M + "is")
                raw = "".join((x.text or "") for x in is_.iter(_M + "t")) if is_ is not None else None
            else:
                raw = v.text if v is not None else None
            row.cells[col] = Cell(
                col=col,
                row=rnum,
                t=t,
                raw=raw,
                formula=(f.text if f is not None else None),
                style=int(c.get("s", "0")),
                shared_formula=(f is not None and f.get("t") == "shared"),
            )
        elem.clear()
        yield row


# --------------------------------------------------------------------------- values
def row_fingerprint(row: Row, shared: list[str], columns: Iterable[str]) -> str:
    """Σταθερό αποτύπωμα γραμμής από τις ακατέργαστες τιμές συγκεκριμένων στηλών."""
    parts = []
    for col in columns:
        c = row.cells.get(col)
        val = c.resolved(shared) if c is not None else None
        parts.append(f"{col}={'' if val is None else val}")
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


_DATE_TOKEN_RE = re.compile(r"(?<!\\)[dmyhs]", re.IGNORECASE)


def _strip_format_literals(code: str) -> str:
    code = re.sub(r'"[^"]*"', "", code)
    code = re.sub(r"\[[^\]]*\]", "", code)
    return code


def is_date_format(code: str) -> bool:
    if code in ("General", "@"):
        return False
    return bool(_DATE_TOKEN_RE.search(_strip_format_literals(code.split(";")[0])))


def number_to_decimal(raw: str) -> Decimal | None:
    try:
        d = Decimal(raw.strip())
    except (InvalidOperation, AttributeError):
        return None
    if not d.is_finite():
        return None
    # Αφαίρεση θορύβου κινητής υποδιαστολής (π.χ. 0.69999999999999996 -> 0.7).
    if len(d.as_tuple().digits) > 15:
        d = Decimal(repr(float(d)))
    return d


def format_decimal(d: Decimal) -> str:
    if d == d.to_integral_value():
        return str(d.quantize(Decimal(1)))
    s = format(d.normalize(), "f")
    return s


def excel_serial_to_datetime(serial: Decimal) -> datetime:
    return datetime(1899, 12, 30) + timedelta(days=float(serial))


def display_value(cell: Cell | None, shared: list[str], styles: Styles) -> str:
    """Τιμή κελιού για εμφάνιση, σεβόμενη τη μορφή αριθμού (π.χ. 0.70)."""
    if cell is None or cell.raw is None:
        return ""
    if cell.t in ("s", "str", "inlineStr"):
        return cell.resolved(shared) or ""
    if cell.t == "b":
        return "TRUE" if cell.raw == "1" else "FALSE"
    if cell.t == "e":
        return cell.raw
    d = number_to_decimal(cell.raw)
    if d is None:
        return cell.raw
    code = styles.format_code(cell.style)
    if is_date_format(code):
        try:
            dt = excel_serial_to_datetime(d)
            return dt.strftime("%d/%m/%Y") if dt.time() == datetime.min.time() else dt.strftime("%d/%m/%Y %H:%M")
        except (OverflowError, ValueError):
            return format_decimal(d)
    section = _strip_format_literals(code.split(";")[0])
    m = re.search(r"0\.(0+)", section)
    if m:
        decimals = len(m.group(1))
        q = d.quantize(Decimal(1).scaleb(-decimals))
        text = format(q, "f")
        return text
    if re.fullmatch(r"\s*#?,?#*0\s*", section or ""):
        return format(d.quantize(Decimal(1)), "f")
    return format_decimal(d)
