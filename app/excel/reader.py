"""Ανάγνωση και επαλήθευση του workbook προϊόντων."""
from __future__ import annotations

import hashlib
import json
import logging
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from app.excel.package import Row, XlsxFormatError, XlsxPackage, display_value, row_fingerprint
from app.services.normalize import group_key, normalize_barcode, normalize_text, parse_kilos

log = logging.getLogger(__name__)

# Στήλες του φύλλου πηγής (A..K). Υπάρχουν και επιπλέον στήλες (L.., τύποι κ.λπ.).
COLUMNS: dict[str, tuple[str, str]] = {
    "A": ("eidos", "ΕΙΔΟΣ"),
    "B": ("paxos", "ΠΑΧΟΣ"),
    "C": ("diastaseis", "ΔΙΑΣΤΑΣΕΙΣ"),
    "D": ("temaxia", "ΤΕΜΑΧΙΑ"),
    "E": ("kila", "ΚΙΛΑ"),
    "F": ("pelatis", "ΠΕΛΑΤΗΣ"),
    "G": ("vspg", "VS/PG"),
    "H": ("barcode", "BARCODE"),
    "I": ("topothesia", "ΤΟΠΟΘΕΣΙΑ"),
    "J": ("thesi", "ΘΕΣΗ"),
    "K": ("rolo", "ΡΟΛΟ"),
}
BARCODE_COL = "H"
FINGERPRINT_COLUMNS = tuple(COLUMNS.keys())


@dataclass
class ProductRow:
    sheet_row: int
    barcode: str
    barcode_norm: str
    values: dict[str, str]          # eidos, paxos, ... (τιμές εμφάνισης)
    extra: dict[str, str]           # επιπλέον στήλες: επικεφαλίδα -> τιμή
    fingerprint: str
    group_key: str

    @property
    def kila(self) -> str:
        return self.values.get("kila", "")


@dataclass
class ValidationReport:
    ok: bool = False
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    sheet_names: list[str] = field(default_factory=list)
    source_sheet: str = ""
    target_sheet: str = ""
    headers: list[str] = field(default_factory=list)
    target_headers: list[str] = field(default_factory=list)
    products: list[ProductRow] = field(default_factory=list)
    duplicates: dict[str, list[int]] = field(default_factory=dict)
    rows_without_barcode: list[int] = field(default_factory=list)
    rows_without_kilos: list[int] = field(default_factory=list)
    sha256: str = ""

    @property
    def product_count(self) -> int:
        return len(self.products)

    @property
    def unique_barcodes(self) -> int:
        return len({p.barcode_norm for p in self.products})

    @property
    def duplicate_barcodes(self) -> int:
        return len(self.duplicates)

    def summary(self) -> dict:
        return {
            "ok": self.ok,
            "errors": self.errors,
            "warnings": self.warnings,
            "sheet_names": self.sheet_names,
            "source_sheet": self.source_sheet,
            "target_sheet": self.target_sheet,
            "headers": self.headers,
            "target_headers": self.target_headers,
            "product_count": self.product_count,
            "unique_barcodes": self.unique_barcodes,
            "duplicate_barcodes": self.duplicate_barcodes,
            "duplicates": self.duplicates,
            "rows_without_barcode": self.rows_without_barcode[:50],
            "rows_without_kilos": self.rows_without_kilos[:50],
            "sha256": self.sha256,
            "preview": [
                {"row": p.sheet_row, "barcode": p.barcode, **p.values} for p in self.products[:30]
            ],
        }


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _loose(name: str) -> str:
    s = unicodedata.normalize("NFD", name)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn" and ch not in "΄΅'΄`")
    return s.strip().upper()


def _check_headers(row: Row | None, pkg: XlsxPackage, label: str, report: ValidationReport) -> list[str]:
    headers: list[str] = []
    if row is None:
        report.errors.append(f"Το φύλλο {label} δεν έχει γραμμή επικεφαλίδων.")
        return headers
    for col, (_key, expected) in COLUMNS.items():
        actual = display_value(row.cells.get(col), pkg.shared_strings, pkg.styles)
        headers.append(actual)
        if normalize_text(actual) != normalize_text(expected):
            report.errors.append(
                f"Φύλλο {label}: η στήλη {col} έχει επικεφαλίδα «{actual}», αναμενόταν «{expected}»."
            )
    return headers


def product_from_row(row: Row, pkg: XlsxPackage, extra_headers: dict[str, str]) -> ProductRow | None:
    shared, styles = pkg.shared_strings, pkg.styles
    bc_cell = row.cells.get(BARCODE_COL)
    barcode = display_value(bc_cell, shared, styles).strip()
    if not barcode:
        return None
    values = {key: display_value(row.cells.get(col), shared, styles).strip() for col, (key, _h) in COLUMNS.items()}
    values["barcode"] = barcode
    extra: dict[str, str] = {}
    for col, header in extra_headers.items():
        v = display_value(row.cells.get(col), shared, styles).strip()
        if v:
            extra[header or col] = v
    return ProductRow(
        sheet_row=row.number,
        barcode=barcode,
        barcode_norm=normalize_barcode(barcode),
        values=values,
        extra=extra,
        fingerprint=row_fingerprint(row, shared, FINGERPRINT_COLUMNS),
        group_key=group_key(values["eidos"], values["paxos"], values["diastaseis"], values["pelatis"]),
    )


def read_products(pkg: XlsxPackage, source_sheet: str) -> tuple[Row | None, list[ProductRow], list[int]]:
    header: Row | None = None
    products: list[ProductRow] = []
    no_barcode: list[int] = []
    extra_headers: dict[str, str] = {}
    for row in pkg.iter_rows(source_sheet):
        if row.number == 1:
            header = row
            for col, cell in row.cells.items():
                if col not in COLUMNS:
                    extra_headers[col] = display_value(cell, pkg.shared_strings, pkg.styles).strip()
            continue
        p = product_from_row(row, pkg, extra_headers)
        if p is None:
            # γραμμή με στοιχεία αλλά χωρίς barcode
            if any(row.cells.get(c) is not None and row.cells[c].raw not in (None, "") for c in "ABCDEFG"):
                no_barcode.append(row.number)
            continue
        products.append(p)
    return header, products, no_barcode


def validate_workbook(path: Path, source_sheet: str, target_sheet: str) -> ValidationReport:
    report = ValidationReport(source_sheet=source_sheet, target_sheet=target_sheet)
    try:
        report.sha256 = file_sha256(path)
        pkg = XlsxPackage(path)
    except XlsxFormatError as exc:
        report.errors.append(str(exc))
        return report
    except OSError as exc:
        report.errors.append(f"Αδυναμία ανάγνωσης αρχείου: {exc}")
        return report
    try:
        report.sheet_names = list(pkg.sheets)
        for wanted in (source_sheet, target_sheet):
            if wanted not in pkg.sheets:
                similar = [n for n in pkg.sheets if _loose(n) == _loose(wanted)]
                msg = f"Δεν βρέθηκε το φύλλο {wanted!r} (ακριβές όνομα)."
                if similar:
                    msg += f" Βρέθηκε παρόμοιο: {similar[0]!r} — ελέγξτε τόνους/κενά ή τη ρύθμιση ονόματος φύλλου."
                report.errors.append(msg)
        if report.errors:
            return report

        header, products, no_bc = read_products(pkg, source_sheet)
        report.headers = _check_headers(header, pkg, repr(source_sheet), report)
        target_header = next(iter(pkg.iter_rows(target_sheet, max_row=1)), None)
        report.target_headers = _check_headers(target_header, pkg, repr(target_sheet), report)

        report.products = products
        report.rows_without_barcode = no_bc
        by_bc: dict[str, list[int]] = defaultdict(list)
        for p in products:
            by_bc[p.barcode_norm].append(p.sheet_row)
            if parse_kilos(p.kila) is None:
                report.rows_without_kilos.append(p.sheet_row)
        report.duplicates = {bc: rows for bc, rows in by_bc.items() if len(rows) > 1}

        if not products:
            report.errors.append("Δεν βρέθηκαν προϊόντα με barcode στη στήλη H.")
        if report.duplicates:
            report.warnings.append(
                f"{len(report.duplicates)} barcode εμφανίζονται σε περισσότερες από μία γραμμές· "
                "θα σημειώνονται ως «Αμφίσημο barcode»."
            )
        if no_bc:
            report.warnings.append(f"{len(no_bc)} γραμμές έχουν στοιχεία αλλά κενό barcode (θα αγνοηθούν).")
        if report.rows_without_kilos:
            report.warnings.append(f"{len(report.rows_without_kilos)} προϊόντα δεν έχουν αριθμητικά κιλά.")
        report.ok = not report.errors
        return report
    except XlsxFormatError as exc:
        report.errors.append(str(exc))
        return report
    except Exception as exc:  # pragma: no cover - απρόβλεπτη δομή
        log.exception("Validation failed")
        report.errors.append(f"Απρόσμενο σφάλμα ανάγνωσης: {exc}")
        return report
    finally:
        pkg.close()


def snapshot_json(p: ProductRow) -> str:
    return json.dumps({**p.values, "sheet_row": p.sheet_row, "extra": p.extra}, ensure_ascii=False)
