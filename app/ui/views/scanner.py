"""Σελίδα «Scanner / Αποθήκη»: οδηγεί στην αφιερωμένη οθόνη σάρωσης.

Η ίδια η σάρωση γίνεται σε ξεχωριστή σελίδα (HTML/JS) που εξυπηρετείται από το FastAPI,
ώστε η λήψη πλήκτρων, το IndexedDB και το Fullscreen API να λειτουργούν αξιόπιστα χωρίς
Streamlit reruns και χωρίς iframe.
"""
from __future__ import annotations

import streamlit as st

from app.ui.api_client import ApiClient, ApiError
from app.ui.common import scanner_url


def page() -> None:
    st.title("Scanner / Αποθήκη")
    url = scanner_url()
    st.markdown(
        f"""
Η οθόνη σάρωσης ανοίγει σε **ξεχωριστή σελίδα**, σχεδιασμένη για εργασία χωρίς ποντίκι:

* Διεύθυνση για το PC με τον δέκτη του scanner: **`{url}`**
* Πατήστε **«Πλήρης Οθόνη»** (ή F9). Έξοδος με **Esc**.
* Κάθε barcode αποθηκεύεται πρώτα τοπικά στον browser και μετά στέλνεται στον server· αν χαθεί το δίκτυο,
  συνεχίζει να καταγράφει και στέλνει αυτόματα τα εκκρεμή όταν επανέλθει.
"""
    )
    st.link_button("▶ Άνοιγμα οθόνης σάρωσης", url, type="primary")
    try:
        cfg = ApiClient().get("/api/config", timeout=5)
        if cfg.get("has_catalog"):
            st.success(f"Ενεργό Excel: {cfg.get('catalog_name')}")
        else:
            st.error("Δεν υπάρχει ενεργό Excel — οι σαρώσεις δεν θα είναι έγκυρες μέχρι να ανέβει αρχείο από τις Ρυθμίσεις.")
        term = "Enter ή Tab" if cfg.get("terminator") == "enter_tab" else "Enter"
        st.caption(f"Τερματικός χαρακτήρας scanner: {term}")
    except ApiError as exc:
        st.error(exc.message)
