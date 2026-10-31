"""Ιστορικό σκαναρισμάτων με φίλτρα και εξαγωγή CSV/Excel."""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import streamlit as st

from app.timeutil import local_tz
from app.ui.api_client import ApiError
from app.ui.common import client, handle_api_error, require

RESULTS = {"": "Όλα", "ok": "Επιτυχές", "duplicate": "Διπλό", "unknown": "Άγνωστο", "ambiguous": "Αμφίσημο",
           "no_catalog": "Χωρίς ενεργό Excel", "invalid": "Μη έγκυρο"}
TRANSFER = {"": "Όλες", "active": "Στην ενεργή λίστα", "transferred": "Μεταφέρθηκε", "removed": "Αφαιρέθηκε"}

COLUMNS = {
    "received_local": "Ημ/νία & ώρα", "barcode": "Barcode", "result_label": "Αποτέλεσμα", "product": "Προϊόν",
    "kila": "Κιλά", "pelatis": "Πελάτης", "transfer_status": "Μεταφορά", "transfer_at_local": "Ημ/νία μεταφοράς",
    "transfer_by": "Χρήστης μεταφοράς", "import_id": "Excel #", "message": "Μήνυμα", "device_id": "Συσκευή",
    "event_id": "Event ID",
}


def page() -> None:
    if not require("dashboard"):
        return
    st.title("Ιστορικό σκαναρισμάτων")
    st.caption("Μόνιμη καταγραφή κάθε σκαναρίσματος. Δεν διαγράφεται με μεταφορές, νέες εισαγωγές Excel ή επανεκκίνηση. "
               "Ώρες σε Europe/Athens.")
    today = datetime.now(local_tz()).date()
    with st.form("hist_filters"):
        c = st.columns([2, 1.4, 1.4, 1.2, 1.2])
        dates = c[0].date_input("Ημερομηνίες", (today - timedelta(days=7), today), format="DD/MM/YYYY")
        barcode = c[1].text_input("Barcode")
        customer = c[2].text_input("Πελάτης")
        result = c[3].selectbox("Αποτέλεσμα", list(RESULTS), format_func=lambda k: RESULTS[k])
        tstatus = c[4].selectbox("Κατάσταση", list(TRANSFER), format_func=lambda k: TRANSFER[k])
        st.form_submit_button("Αναζήτηση", type="primary")
    if isinstance(dates, (list, tuple)):
        d_from = dates[0] if len(dates) > 0 else None
        d_to = dates[1] if len(dates) > 1 else d_from
    else:
        d_from = d_to = dates
    params = {k: v for k, v in {
        "date_from": d_from.isoformat() if d_from else None, "date_to": d_to.isoformat() if d_to else None,
        "barcode": barcode or None, "customer": customer or None, "result": result or None,
        "transfer_status": tstatus or None,
    }.items() if v}
    try:
        rows = client().get("/api/history", params=params, timeout=30)
    except ApiError as exc:
        handle_api_error(exc)
        return
    st.write(f"**{len(rows)}** εγγραφές")
    if rows:
        df = pd.DataFrame(rows)[list(COLUMNS)].rename(columns=COLUMNS)
        st.dataframe(df, hide_index=True, use_container_width=True, height=560)

    c1, c2, _ = st.columns([1, 1, 3])
    for col, fmt, label, mime in (
        (c1, "csv", "CSV", "text/csv"),
        (c2, "xlsx", "Excel", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ):
        key = f"hist_export_{fmt}"
        if col.button(f"Προετοιμασία εξαγωγής {label}", key=f"prep_{fmt}"):
            try:
                st.session_state[key] = client().get("/api/history/export", params={**params, "fmt": fmt}, raw=True)
            except ApiError as exc:
                handle_api_error(exc)
        if st.session_state.get(key):
            col.download_button(f"⬇ Λήψη {label}", st.session_state[key],
                                file_name=f"istoriko_{today:%Y%m%d}.{fmt}", mime=mime, key=f"dl_{fmt}")
