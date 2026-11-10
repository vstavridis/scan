"""Background worker: αδειάζει την ουρά μεταφορών/επαναφορών προς το Excel (και το OneDrive).

* Ξυπνά αμέσως μόλις καταχωριστεί μια ενέργεια (`kick()`), αλλιώς κάθε λίγα δευτερόλεπτα.
* Όλες οι εκκρεμείς ενέργειες εφαρμόζονται μαζί σε μία νέα έκδοση Excel (app.services.transfer).
* Ένας μόνο εγγραφέας για όλα τα sessions/διεργασίες: lease «excel» στη βάση. Αν το έχει άλλη
  διεργασία, αυτή απλώς περιμένει τον επόμενο γύρο.
* OneDrive (αν έχει συνδεθεί): καθρέφτης «best effort» μετά από κάθε νέα έκδοση και περιοδικά
  όσο υπάρχουν εκκρεμότητες. Δεν σταματά ποτέ την αποθήκη.
* Αν η εφαρμογή σταματήσει, οι ενέργειες μένουν στη βάση και συνεχίζονται στην επόμενη εκκίνηση.
"""
from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger(__name__)

IDLE_SECONDS = 5.0
ONEDRIVE_RETRY_SECONDS = 60.0

_event = threading.Event()
_thread: threading.Thread | None = None
_start_lock = threading.Lock()
_last_onedrive = 0.0
_stop = threading.Event()


def kick() -> None:
    """Ξυπνά τον worker (μετά από νέα καταχώριση)."""
    _event.set()


def running() -> bool:
    return _thread is not None and _thread.is_alive()


def start() -> None:
    global _thread
    with _start_lock:
        if running():
            return
        _stop.clear()
        _thread = threading.Thread(target=_loop, name="excel-sync-worker", daemon=True)
        _thread.start()
        log.info("Excel sync worker started")
    kick()


def stop(timeout: float = 10.0) -> None:
    """Σταματά τον worker (tests / τερματισμός)."""
    global _thread
    _stop.set()
    _event.set()
    if _thread is not None:
        _thread.join(timeout)
    _thread = None


def run_once() -> dict:
    """Ένας γύρος: ουρά Excel και (αν χρειάζεται) OneDrive. Χρησιμοποιείται και από τα tests."""
    global _last_onedrive
    from app.services import transfer
    from app.services.errors import Conflict, ServiceError

    out = {"processed": 0, "onedrive": None}
    changed = False
    for _ in range(20):  # έως 20 εκδόσεις ανά γύρο (100 ενέργειες η καθεμία)
        try:
            res = transfer.process_pending("excel-sync", wait=5)
        except Conflict:
            break  # άλλη διεργασία έχει το lease
        except ServiceError as exc:
            log.warning("Excel sync will retry: %s", exc.message)
            break
        out["processed"] += res["processed"]
        changed = changed or bool(res.get("completed"))
        if not res["processed"]:
            break
    try:
        from app.services import onedrive
        due = time.monotonic() - _last_onedrive > ONEDRIVE_RETRY_SECONDS
        if (changed or due) and onedrive.status().get("pending"):
            _last_onedrive = time.monotonic()
            synced, _warning = transfer._onedrive_mirror("onedrive-mirror")
            out["onedrive"] = synced
    except Exception as exc:  # το OneDrive δεν σταματά ποτέ τον worker
        log.warning("OneDrive mirror check failed (%s)", type(exc).__name__)
    return out


def _loop() -> None:
    while not _stop.is_set():
        _event.wait(IDLE_SECONDS)
        _event.clear()
        if _stop.is_set():
            break
        try:
            run_once()
        except Exception:
            log.exception("Excel sync worker round failed")
