"""Σελίδα «Scanner / Αποθήκη»: η οθόνη σάρωσης μέσα στην εφαρμογή Streamlit."""
from __future__ import annotations

import streamlit as st

from app import backend
from app.services.errors import ServiceError
from app.ui.common import require, show_error
from app.ui.components.scanner import scanner_panel


def page() -> None:
    if not require("scan"):
        return
    try:
        cfg = backend.public_config()
    except ServiceError as exc:
        show_error(exc)
        return
    if not cfg["has_catalog"]:
        st.error("Δεν υπάρχει ενεργό Excel — οι σαρώσεις καταγράφονται αλλά δεν θα είναι έγκυρες μέχρι να "
                 "ανεβάσει ο διαχειριστής αρχείο από τις Ρυθμίσεις.")
    scanner_panel(cfg)
    st.caption("Σκανάρετε χωρίς ποντίκι — η σελίδα «ακούει» το πληκτρολόγιο. **Πλήρης Οθόνη** ή F9, έξοδος με Esc. "
               "Κάθε barcode φυλάσσεται πρώτα στον browser και διαγράφεται από εκεί μόνο όταν επιβεβαιωθεί η "
               "αποθήκευση στη βάση.")
