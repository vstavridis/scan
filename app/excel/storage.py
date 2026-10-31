"""Υπηρεσία αποθήκευσης αρχείων Excel.

Ξεχωριστή αφαίρεση (ExcelStore) ώστε στο μέλλον να προστεθεί π.χ. OneDriveExcelStore
χωρίς αλλαγές στη λογική μεταφοράς/εισαγωγής.
"""
from __future__ import annotations

import logging
import os
import shutil
import stat
import threading
from abc import ABC, abstractmethod
from pathlib import Path

from app.config import get_settings
from app.timeutil import local_stamp

log = logging.getLogger(__name__)


class ExcelLockedError(Exception):
    """Το αρχείο είναι ανοιχτό/κλειδωμένο από άλλη εφαρμογή (π.χ. Excel)."""


LOCKED_MESSAGE = (
    "Το αρχείο Excel είναι ανοιχτό ή κλειδωμένο από άλλη εφαρμογή (π.χ. Microsoft Excel). "
    "Κλείστε το αρχείο και δοκιμάστε ξανά. Δεν έγινε καμία αλλαγή."
)


class ExcelStore(ABC):
    @abstractmethod
    def import_dir(self, import_id: int) -> Path: ...

    @abstractmethod
    def save_original(self, import_id: int, staged: Path, filename: str) -> tuple[Path, Path]: ...

    @abstractmethod
    def backup(self, path: Path, reason: str) -> Path: ...

    @abstractmethod
    def ensure_writable(self, path: Path) -> None: ...

    @abstractmethod
    def temp_path_for(self, path: Path) -> Path: ...

    @abstractmethod
    def replace_atomic(self, tmp: Path, dest: Path) -> None: ...


class LocalExcelStore(ExcelStore):
    """Αρχεία σε τοπικό φάκελο του server."""

    def __init__(self, root: Path | None = None, backups: Path | None = None):
        s = get_settings()
        self.root = root or s.imports_dir
        self.backups = backups or s.backups_dir
        self.root.mkdir(parents=True, exist_ok=True)
        self.backups.mkdir(parents=True, exist_ok=True)

    def import_dir(self, import_id: int) -> Path:
        d = self.root / f"import-{import_id:05d}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save_original(self, import_id: int, staged: Path, filename: str) -> tuple[Path, Path]:
        d = self.import_dir(import_id)
        original = d / "original.xlsx"
        working = d / "working.xlsx"
        shutil.copy2(staged, original)
        _fsync_file(original)
        # Το αρχικό αρχείο μένει μόνο για ανάγνωση.
        try:
            os.chmod(original, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
        except OSError:  # pragma: no cover
            pass
        (d / "original_filename.txt").write_text(filename, encoding="utf-8")
        shutil.copy2(staged, working)
        _fsync_file(working)
        return original, working

    def backup(self, path: Path, reason: str) -> Path:
        safe_reason = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in reason)[:40]
        dest = self.backups / f"{local_stamp()}_{safe_reason}_{path.parent.name}.xlsx"
        shutil.copy2(path, dest)
        _fsync_file(dest)
        log.info("Backup created: %s", dest)
        return dest

    def ensure_writable(self, path: Path) -> None:
        owner_file = path.with_name("~$" + path.name)
        if owner_file.exists():
            raise ExcelLockedError(LOCKED_MESSAGE)
        try:
            with open(path, "r+b"):
                pass
        except PermissionError as exc:
            raise ExcelLockedError(LOCKED_MESSAGE) from exc

    def temp_path_for(self, path: Path) -> Path:
        return path.with_name(f".{path.stem}.{os.getpid()}.{threading.get_ident()}.tmp.xlsx")

    def replace_atomic(self, tmp: Path, dest: Path) -> None:
        _fsync_file(tmp)
        try:
            os.replace(tmp, dest)
        except PermissionError as exc:
            raise ExcelLockedError(LOCKED_MESSAGE) from exc
        try:
            _fsync_dir(dest.parent)
        except OSError:  # η αντικατάσταση έγινε ήδη· το fsync φακέλου είναι επιπλέον προστασία
            log.warning("Directory fsync failed for %s", dest.parent)


def _fsync_file(path: Path) -> None:
    with open(path, "rb+") as fh:
        os.fsync(fh.fileno())


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":  # τα Windows δεν υποστηρίζουν fsync σε φάκελο
        return
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# Ένας writer τη φορά για όλα τα αρχεία Excel της διεργασίας.
excel_write_lock = threading.Lock()

_store: ExcelStore | None = None


def get_store() -> ExcelStore:
    global _store
    if _store is None:
        _store = LocalExcelStore()
    return _store


def set_store(store: ExcelStore | None) -> None:
    global _store
    _store = store
