"""Δημιουργεί συνθετικό workbook με την ίδια δομή με το πραγματικό αρχείο.

Χρησιμοποιείται στα tests ώστε να μην χρειάζεται το production Excel στο repository.
Περιέχει: κρυφά φύλλα, ακριβή ονόματα φύλλων (με U+0384 και τελικό κενό), επικεφαλίδες A..P,
τύπους ανά γραμμή (L..O), cross-sheet SUM που δείχνει στο φύλλο πηγής, autoFilter,
μορφή αριθμού 0.00, διπλό barcode (EX434-1) και τα προϊόντα του παραδείγματος SUM.
"""
from __future__ import annotations

import sys
from pathlib import Path

from openpyxl import Workbook
from openpyxl.worksheet.filters import AutoFilter

SOURCE = "΄Ετοιμα προϊόντα"
TARGET = "Σβησμενα "
HEADERS = ["ΕΙΔΟΣ", "ΠΑΧΟΣ", "ΔΙΑΣΤΑΣΕΙΣ", "ΤΕΜΑΧΙΑ", "ΚΙΛΑ", "ΠΕΛΑΤΗΣ", "VS/PG", "BARCODE", "ΤΟΠΟΘΕΣΙΑ",
           "ΘΕΣΗ", "ΡΟΛΟ", "ΠΟΙΟΤΗΤΑ", "ΧΩΡΑ", "ID", "ΚΟΣΤΟΣ", "ΗΜΕΡΟΜΗΝΙΑ"]

# (είδος, πάχος, διαστάσεις, τεμάχια, κιλά, πελάτης, vs/pg, barcode, τοποθεσία, θέση, ρολό)
PRODUCTS = [
    ("ΓΑΛΒΑΝΙΖΕ", "1.00", "96+110ΧΡΟΛΟ", "1+1", 626, "FENPLAST", "EX", "EX1898", "ΜΙΚΡΟ", None, "16082FB02111"),
    ("MIXED", "(-)", "(-)", 5, 497, "ΑΠΟΘΗΚΗ", "EX", "EX434-1", "Ε", None, "ΔΕΝ ΥΠΑΡΧΕΙ"),
    ("MIXED", "(-)", "(-)", 5, 490, "ΑΠΟΘΗΚΗ", "EX", "EX434-1", "Ε", None, "ΔΕΝ ΥΠΑΡΧΕΙ"),
    ("ΗΛΕΚΤΡΟΓΑΛΒΑΝΙΖΕ", "0.70", "180ΧΡΟΛΟ", 1, 512, "ΑΠΟΘΗΚΗ", "VS", "VS90001", "ΓΡΑΦΕΙΟ", None, 4447970),
    ("ΗΛΕΚΤΡΟΓΑΛΒΑΝΙΖΕ", "0.70", "180ΧΡΟΛΟ", 1, 412, "ΑΠΟΘΗΚΗ", "VS", "VS11486", "ΓΡΑΦΕΙΟ", None, 4447971),
    ("ΗΛΕΚΤΡΟΓΑΛΒΑΝΙΖΕ", "0.70", "180ΧΡΟΛΟ", 1, 412, "ΚΥΡΙΑΚΙΔΗΣ", "VS", "VS90002", "ΓΡΑΦΕΙΟ", None, 4447972),
    ("ΓΑΛΒΑΝΙΖΕ", "0.70", "180ΧΡΟΛΟ", 1, 412, "ΚΥΡΙΑΚΙΔΗΣ", "VS", "VS90003", "ΓΡΑΦΕΙΟ", None, 4447973),
    ("ΗΛΕΚΤΡΟΓΑΛΒΑΝΙΖΕ", "0.70", "180ΧΡΟΛΟ", 1, 416, "ΑΠΟΘΗΚΗ", "VS", "VS11487", "ΓΡΑΦΕΙΟ", None, 445535),
    ("ΓΑΛΒΑΝΙΖΕ", 2.5, "14,8ΧΡΟΛΟ", 8, 348.5, "ΑΠΟΘΗΚΗ", "PG", "PG255", None, None, None),
]
N_FILLER = 300  # TST0001..TST0300 για burst/ταυτόχρονες δοκιμές


def filler(i: int) -> tuple:
    return ("ΓΑΛΒΑΝΙΖΕ", "0.50", f"{100 + i % 7}ΧΡΟΛΟ", 1, 100 + i, f"ΠΕΛΑΤΗΣ {i % 5}", "PG", f"TST{i:04d}", "7", None, f"R{i}")


def build(path: Path) -> Path:
    wb = Workbook()
    ws_id = wb.active
    ws_id.title = "ID"
    ws_id.append(["ΠΕΛΑΤΗΣ", "ID"])
    ws_id.append(["ΚΥΡΙΑΚΙΔΗΣ", 45])
    ws_id["D1"] = "ΣΥΝΟΛΟ ΚΙΛΩΝ ΕΤΟΙΜΩΝ"
    ws_id["D2"] = f"=SUM('{SOURCE}'!E2:E400)"
    ws_id["D3"] = f"='{SOURCE}'!H2"  # δείχνει στη γραμμή του EX1898 (δοκιμή ασφαλούς εναλλακτικής)
    ws_id.sheet_state = "hidden"

    fason = wb.create_sheet("FASON")
    fason.append(["Α", "Β"])
    fason.append(["κείμενο", 1])
    fason.sheet_state = "hidden"

    src = wb.create_sheet(SOURCE)
    src.append(HEADERS)
    rows = list(PRODUCTS) + [filler(i) for i in range(1, N_FILLER + 1)]
    for n, p in enumerate(rows, start=2):
        src.append(list(p))
        src[f"L{n}"] = f'=IFERROR(VLOOKUP(K{n},FASON!$A:$B,2,0),"-")'
        src[f"M{n}"] = f'=IFERROR(VLOOKUP(F{n},ID!$A:$B,2,0),"-")'
        src[f"N{n}"] = f'=IFERROR(VLOOKUP(F{n},ID!$A:$B,2,0),"-")'
        src[f"O{n}"] = f"=E{n}*2"
        src[f"B{n}"].number_format = "0.00"
    last = len(rows) + 1
    src.auto_filter = AutoFilter(ref=f"A1:P{last}")
    src.freeze_panes = "A2"

    tgt = wb.create_sheet(TARGET)
    tgt.append(HEADERS[:13] + ["Δ.Π."])
    for n, bc in enumerate(["PG1", "PG2", "PG3"], start=2):
        tgt.append(["ΓΑΛΒΑΝΙΖΕ", 2.5, "14,8ΧΡΟΛΟ", 8, 300 + n, "ΑΠΟΘΗΚΗ", "PG", bc, None, None, None])
        tgt[f"L{n}"] = f'=IFERROR(VLOOKUP(K{n},FASON!$A:$B,2,0),"-")'
        tgt[f"O{n}"] = f"=E{n}*2"
    tgt.auto_filter = AutoFilter(ref="A1:N4")

    hidden = wb.create_sheet("Σβησμένα1")
    hidden.append(HEADERS[:11])
    hidden.append(["ΠΑΛΙΟ", "1", "Χ", 1, 1, "Χ", "VS", "OLD1"])
    hidden.sheet_state = "hidden"

    # ίδια σειρά με το πραγματικό αρχείο: κρυφά πρώτα, μετά τα δύο βασικά
    wb.move_sheet(SOURCE, offset=wb.sheetnames.index("Σβησμένα1") - wb.sheetnames.index(SOURCE))
    wb.move_sheet(TARGET, offset=len(wb.sheetnames) - 1 - wb.sheetnames.index(TARGET))
    wb.active = wb.sheetnames.index(SOURCE)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "synthetic.xlsx")
    print(build(out))
