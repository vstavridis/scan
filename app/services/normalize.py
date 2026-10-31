"""Κανονικοποίηση barcode και κλειδιών ομαδοποίησης.

Οι αρχικές τιμές διατηρούνται πάντα για εμφάνιση· η κανονικοποίηση
χρησιμοποιείται μόνο για σύγκριση/αναζήτηση/ομαδοποίηση.
"""
from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_WS_RE = re.compile(r"\s+")

# Λατινικοί χαρακτήρες που μοιάζουν με ελληνικούς (συχνό λάθος πληκτρολόγησης στο Excel,
# π.χ. "180XΡΟΛΟ" με λατινικό X αντί για ελληνικό Χ). Χρησιμοποιείται ΜΟΝΟ στο κλειδί ομαδοποίησης.
_LATIN_TO_GREEK = str.maketrans({
    "A": "Α", "B": "Β", "E": "Ε", "H": "Η", "I": "Ι", "K": "Κ", "M": "Μ", "N": "Ν",
    "O": "Ο", "P": "Ρ", "T": "Τ", "X": "Χ", "Y": "Υ", "Z": "Ζ",
})


def normalize_barcode(value: str | None) -> str:
    """Barcode για αναζήτηση: χωρίς κενά/χαρακτήρες ελέγχου, κεφαλαία, NFKC."""
    if value is None:
        return ""
    s = unicodedata.normalize("NFKC", str(value))
    s = _CONTROL_RE.sub("", s)
    s = _WS_RE.sub("", s)
    return s.upper()


def _strip_accents(s: str) -> str:
    decomposed = unicodedata.normalize("NFD", s)
    return unicodedata.normalize("NFC", "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn"))


def normalize_text(value: str | None) -> str:
    if value is None:
        return ""
    s = unicodedata.normalize("NFKC", str(value))
    s = _CONTROL_RE.sub("", s)
    s = _WS_RE.sub(" ", s).strip().upper()
    s = _strip_accents(s).replace("΄", "").replace("΅", "")
    s = s.translate(_LATIN_TO_GREEK)
    s = s.replace("×", "Χ").replace(" ", "")
    return s


def normalize_thickness(value: str | None) -> str:
    """Το πάχος συγκρίνεται αριθμητικά όταν γίνεται (0.70 == 0.7 == 0,70)."""
    if value is None:
        return ""
    s = str(value).strip().replace(",", ".")
    try:
        d = Decimal(s)
        if d.is_finite():
            return format(d.normalize(), "f")
    except InvalidOperation:
        pass
    return normalize_text(value)


def group_key(eidos: str | None, paxos: str | None, diastaseis: str | None, pelatis: str | None) -> str:
    return "\x1f".join((
        normalize_text(eidos),
        normalize_thickness(paxos),
        normalize_text(diastaseis),
        normalize_text(pelatis),
    ))


def parse_kilos(value: str | None) -> Decimal | None:
    """Ασφαλής μετατροπή κιλών σε Decimal (ποτέ float)."""
    if value is None:
        return None
    s = str(value).strip().replace(" ", "")
    if not s:
        return None
    if "," in s and "." not in s:
        s = s.replace(",", ".")
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def format_kilos(d: Decimal | str | None) -> str:
    if d is None or d == "":
        return ""
    if not isinstance(d, Decimal):
        parsed = parse_kilos(d)
        if parsed is None:
            return str(d)
        d = parsed
    if d == d.to_integral_value():
        return str(d.quantize(Decimal(1)))
    return format(d.normalize(), "f")


def product_label(eidos: str | None, paxos: str | None, diastaseis: str | None) -> str:
    return f"{eidos or '—'} × {paxos or '—'} × {diastaseis or '—'}"


def product_display(eidos: str | None, paxos: str | None, diastaseis: str | None,
                    kila: str | None, pelatis: str | None) -> str:
    """Π.χ. 'ΗΛΕΚΤΡΟΓΑΛΒΑΝΙΖΕ × 0.70 × 180ΧΡΟΛΟ / 412 - ΑΠΟΘΗΚΗ'."""
    return f"{product_label(eidos, paxos, diastaseis)} / {format_kilos(kila) or '—'} - {pelatis or '—'}"
