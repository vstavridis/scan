"""Γέφυρα Python <-> JavaScript για την οθόνη σάρωσης (Streamlit Custom Component v2).

* Το JS στέλνει batches με `setTriggerValue('batch', {...})`.
* Το callback `_on_batch` εκτελείται ΠΡΙΝ από το σώμα του fragment: γράφει στη βάση (COMMIT)
  και κρατά τα αποτελέσματα στο session.
* Τα αποτελέσματα (acks) επιστρέφουν στο JS μέσω `data.acks` στο ίδιο rerun και, για ασφάλεια,
  επαναλαμβάνονται για ~2 λεπτά σε κάθε επόμενο rerun (αν χαθεί ένα rerun, δεν χάνεται το ack).
* Το fragment ξανατρέχει περιοδικά (run_every) ώστε η λίστα να δείχνει και τις αλλαγές της
  γραμματείας (μεταφορές) και να υπάρχει "heartbeat" για την ένδειξη σύνδεσης.
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

import streamlit as st

from app import backend
from app.services.errors import ServiceError
from app.ui.common import principal

log = logging.getLogger(__name__)

_ASSETS = Path(__file__).resolve().parent / "assets"
KEY = "wh_scanner"
ACKS = "wh_scanner_acks"
ERR = "wh_scanner_error"
TICK = "wh_scanner_tick"
ACK_TTL_SECONDS = 120
ACK_MAX = 400

_component = st.components.v2.component(
    "warehouse_scanner",
    html=(_ASSETS / "scanner.html").read_text(encoding="utf-8"),
    css=(_ASSETS / "scanner.css").read_text(encoding="utf-8"),
    js=(_ASSETS / "scanner.js").read_text(encoding="utf-8"),
    isolate_styles=False,
)


def _acks() -> "OrderedDict[str, dict[str, Any]]":
    if ACKS not in st.session_state:
        st.session_state[ACKS] = OrderedDict()
    return st.session_state[ACKS]


def _read_batch() -> dict[str, Any] | None:
    state = st.session_state.get(KEY)
    if state is None:
        return None
    try:
        value = state["batch"]
    except (KeyError, TypeError):
        value = getattr(state, "batch", None)
    return value if isinstance(value, dict) else None


def _on_batch() -> None:
    """Callback: αποθήκευση batch στη βάση. Δεν πετάει exceptions (το JS ξαναστέλνει)."""
    batch = _read_batch()
    if not batch or not isinstance(batch.get("events"), list):
        return
    p = principal()
    try:
        res = backend.submit_scans(p, batch["events"], device_id=p.name if p else None)
    except ServiceError as exc:
        st.session_state[ERR] = exc.message
        return
    except Exception as exc:  # π.χ. βάση μη διαθέσιμη
        log.exception("Scan batch failed")
        st.session_state[ERR] = f"Η βάση δεν είναι διαθέσιμη ({type(exc).__name__})."
        return
    st.session_state.pop(ERR, None)
    acks = _acks()
    now = time.time()
    for r in res["results"]:
        acks[r["event_id"]] = {
            "event_id": r["event_id"], "barcode": r["barcode"], "result": r["result"], "message": r["message"],
            "display": r.get("display"), "t": now,
        }
        acks.move_to_end(r["event_id"])
    while len(acks) > ACK_MAX:
        acks.popitem(last=False)


def _recent_acks() -> list[dict[str, Any]]:
    acks = _acks()
    cutoff = time.time() - ACK_TTL_SECONDS
    while acks and next(iter(acks.values()))["t"] < cutoff:
        acks.popitem(last=False)
    return [{k: v for k, v in a.items() if k != "t"} for a in acks.values()]


def scanner_panel(cfg: dict[str, Any]) -> None:
    refresh = float(cfg.get("scanner_refresh_seconds") or 2.0)

    @st.fragment(run_every=refresh)
    def panel() -> None:
        p = principal()
        st.session_state[TICK] = st.session_state.get(TICK, 0) + 1
        view: dict[str, Any] = {"items": [], "has_catalog": True}
        error = st.session_state.get(ERR)
        try:
            view = backend.scanner_view(p)
        except ServiceError as exc:
            error = exc.message
        except Exception as exc:
            log.exception("Scanner view failed")
            error = f"Η βάση δεν είναι διαθέσιμη ({type(exc).__name__})."
        data = {
            "tick": st.session_state[TICK],
            "items": view.get("items", []),
            "has_catalog": view.get("has_catalog", True),
            "acks": _recent_acks(),
            "error": error,
            "user": p.name if p else "",
            "refresh_seconds": refresh,
            "cfg": {k: cfg[k] for k in ("terminator", "key_mapping", "sound_default", "inter_key_timeout_ms")},
        }
        _component(key=KEY, data=data, on_batch_change=_on_batch, height="content")

    panel()
