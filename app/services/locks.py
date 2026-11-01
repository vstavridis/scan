"""Leases στη βάση για ενέργειες που διαρκούν πέρα από ένα transaction (π.χ. εγγραφή Excel).

Στο Streamlit Cloud μπορεί να τρέχουν ταυτόχρονα πολλά sessions (και, για λίγο, δύο διεργασίες
κατά το redeploy). Ένα in-process lock δεν αρκεί· το lease στη βάση ισχύει για όλους.
Αν η διεργασία που το κρατά σταματήσει, το lease λήγει μόνο του.
"""
from __future__ import annotations

import threading
import time
import uuid
from contextlib import contextmanager
from datetime import timedelta
from typing import Iterator

from app.db.database import transaction
from app.services.errors import Conflict
from app.timeutil import utcnow

_local = threading.Lock()


def _iso(dt) -> str:
    return dt.isoformat(timespec="milliseconds")


def try_acquire(name: str, holder: str, seconds: int) -> bool:
    now = utcnow()
    with transaction() as conn:
        row = conn.execute(
            """INSERT INTO app_locks(name, holder, expires_at) VALUES (?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET holder=excluded.holder, expires_at=excluded.expires_at
               WHERE app_locks.expires_at < ? RETURNING holder""",
            (name, holder, _iso(now + timedelta(seconds=seconds)), _iso(now)),
        ).fetchone()
    return row is not None and row["holder"] == holder


def release(name: str, holder: str) -> None:
    with transaction() as conn:
        conn.execute("DELETE FROM app_locks WHERE name=? AND holder=?", (name, holder))


@contextmanager
def lease(name: str, seconds: int = 300, wait: float = 20.0, busy_message: str | None = None) -> Iterator[str]:
    """Αποκλειστικό lease· περιμένει έως `wait` δευτερόλεπτα, αλλιώς Conflict."""
    holder = uuid.uuid4().hex
    deadline = time.monotonic() + wait
    with _local:
        while not try_acquire(name, holder, seconds):
            if time.monotonic() > deadline:
                raise Conflict(busy_message or "Εκτελείται ήδη άλλη ενέργεια στο Excel. Δοκιμάστε ξανά σε λίγο.")
            time.sleep(0.25)
        try:
            yield holder
        finally:
            release(name, holder)
