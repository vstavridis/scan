"""Βοηθητικά για ώρα. Αποθήκευση σε UTC (ISO 8601), εμφάνιση σε Europe/Athens."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.config import get_settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    return utcnow().isoformat(timespec="milliseconds")


def local_tz() -> ZoneInfo:
    return ZoneInfo(get_settings().timezone)


def to_local(value: str | datetime | None) -> datetime | None:
    if value is None or value == "":
        return None
    dt = datetime.fromisoformat(value) if isinstance(value, str) else value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(local_tz())


def fmt_local(value: str | datetime | None, with_ms: bool = False) -> str:
    dt = to_local(value)
    if dt is None:
        return ""
    if with_ms:
        return dt.strftime("%d/%m/%Y %H:%M:%S.") + f"{dt.microsecond // 1000:03d}"
    return dt.strftime("%d/%m/%Y %H:%M:%S")


def local_stamp() -> str:
    """Χρονοσφραγίδα για ονόματα αρχείων (τοπική ώρα)."""
    return datetime.now(local_tz()).strftime("%Y%m%d-%H%M%S-%f")[:-3]
