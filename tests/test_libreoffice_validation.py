"""Ανεξάρτητη επαλήθευση: το Excel μετά τη μεταφορά ανοίγει σε άλλη εφαρμογή λογιστικών φύλλων
(LibreOffice) και τα δεδομένα/τύποι είναι σωστά. Παραλείπεται αν δεν υπάρχει LibreOffice."""
from __future__ import annotations

import csv
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.config import DEFAULT_SOURCE_SHEET as SRC, DEFAULT_TARGET_SHEET as TGT
from app.excel.reader import validate_workbook
from app.excel.writer import transfer_row

pytestmark = pytest.mark.slow
SOFFICE = shutil.which("soffice") or shutil.which("libreoffice")


def _to_csv(xlsx: Path, outdir: Path) -> dict[str, list[list[str]]]:
    outdir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, LANG="C.UTF-8", LC_ALL="C.UTF-8", HOME=str(outdir))
    subprocess.run(
        [SOFFICE, "--headless", "--norestore",
         "--convert-to", "csv:Text - txt - csv (StarCalc):44,34,76,1,,0,false,true,false,false,false,-1",
         "--outdir", str(outdir), str(xlsx)],
        check=True, capture_output=True, timeout=600, env=env)
    sheets = {}
    for f in outdir.glob(f"{xlsx.stem}-*.csv"):
        name = f.name[len(xlsx.stem) + 1:-4]
        with open(f, encoding="utf-8", newline="") as fh:
            sheets[name] = list(csv.reader(fh))
    return sheets


@pytest.mark.skipif(SOFFICE is None, reason="LibreOffice δεν είναι εγκατεστημένο")
def test_transferred_workbook_opens_and_computes(synthetic_master, tmp_path, app_env):
    rep = validate_workbook(synthetic_master, SRC, TGT)
    p = next(x for x in rep.products if x.barcode_norm == "VS11486")
    out = tmp_path / "out.xlsx"
    transfer_row(synthetic_master, out, SRC, TGT, p.barcode_norm, p.fingerprint)
    sheets = _to_csv(out, tmp_path / "csv")
    assert {SRC, TGT, "ID", "FASON", "Σβησμένα1"} <= set(sheets)
    src_codes = [r[7] for r in sheets[SRC][1:] if len(r) > 7 and r[7]]
    tgt_rows = [r for r in sheets[TGT][1:] if len(r) > 7 and r[7]]
    assert "VS11486" not in src_codes
    assert tgt_rows[-1][7] == "VS11486" and tgt_rows[-1][4] == "412"
    # ο τύπος O (=E*2) υπολογίζεται στη νέα θέση: 824
    assert tgt_rows[-1][14] == "824"
    # cross-sheet SUM: σύνολο κιλών χωρίς τη μεταφερμένη γραμμή
    expected = sum(float(x.kila) for x in rep.products) - 412
    assert float(sheets["ID"][1][3]) == pytest.approx(expected)


@pytest.mark.skipif(SOFFICE is None, reason="LibreOffice δεν είναι εγκατεστημένο")
def test_real_workbook_after_transfer_matches_original(real_master, tmp_path, app_env):
    """Πραγματικό αρχείο: όλα τα φύλλα ίδια με το αρχικό εκτός από τη μία μετακινημένη γραμμή."""
    work = tmp_path / "in.xlsx"
    shutil.copy2(real_master, work)
    rep = validate_workbook(work, SRC, TGT)
    p = next(x for x in rep.products if x.barcode_norm == "VS11486")
    out = tmp_path / "out.xlsx"
    transfer_row(work, out, SRC, TGT, p.barcode_norm, p.fingerprint)
    before = _to_csv(work, tmp_path / "a")
    after = _to_csv(out, tmp_path / "b")
    assert set(before) == set(after)

    def strip_now(rows):  # οι τύποι NOW() αλλάζουν τιμή σε κάθε άνοιγμα
        import re
        return [[re.sub(r"\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2}", "NOW", c) for c in r] for r in rows]

    for name in before:
        b, a = strip_now(before[name]), strip_now(after[name])
        if name == SRC:
            idx = next(i for i, r in enumerate(b) if len(r) > 7 and r[7] == "VS11486")
            assert a == b[:idx] + b[idx + 1:]
        elif name == TGT:
            b_data = [r for r in b if any(r)]
            a_data = [r for r in a if any(r)]
            assert a_data[:-1] == b_data
            moved = before[SRC][next(i for i, r in enumerate(before[SRC]) if len(r) > 7 and r[7] == "VS11486")]
            assert a_data[-1][:11] == moved[:11]
        else:
            assert a == b, f"Το φύλλο {name!r} άλλαξε"
