"""Ιστορικό: σκαναρίσματα (φίλτρα, εξαγωγή) και μεταφορές με επαναφορά."""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import streamlit as st

from app.services.normalize import format_kilos
from app.timeutil import fmt_local, local_tz
from app import backend
from app.services.errors import ServiceError
from app.ui.common import principal, require, show_error

RESULTS = {"": "Όλα", "ok": "Επιτυχές", "duplicate": "Διπλό", "unknown": "Άγνωστο", "ambiguous": "Αμφίσημο",
           "no_catalog": "Χωρίς ενεργό Excel", "invalid": "Μη έγκυρο"}
TRANSFER = {"": "Όλες", "active": "Στην ενεργή λίστα", "transferred": "Μεταφέρθηκε", "removed": "Αφαιρέθηκε"}

COLUMNS = {
    "received_local": "Ημ/νία & ώρα", "barcode": "Barcode", "result_label": "Αποτέλεσμα", "product": "Προϊόν",
    "kila": "Κιλά", "pelatis": "Πελάτης", "transfer_status": "Μεταφορά", "transfer_at_local": "Ημ/νία μεταφοράς",
    "transfer_by": "Χρήστης μεταφοράς", "correction": "Διόρθωση barcode", "import_id": "Excel #", "message": "Μήνυμα", "device_id": "Συσκευή",
    "event_id": "Event ID",
}


TRANSFERS_SHOWN = 25   # μόνο οι πιο πρόσφατες μεταφορές


def _transfer_status(t: dict) -> tuple[str, bool]:
    """(κείμενο κατάστασης, επιτρέπεται επαναφορά)."""
    if t.get("reverted_at"):
        return f"↩ Επαναφέρθηκε στα Έτοιμα ({fmt_local(t['reverted_at'])})", False
    if t.get("restore_status") in ("queued", "started"):
        return "↩ Επαναφορά σε εξέλιξη…", False
    if t["status"] in ("queued", "started"):
        return "Ενημέρωση Excel σε εξέλιξη…", False
    if not t.get("import_active"):
        return "Προηγούμενο Excel", False
    text = f"Στα Σβησμένα (γραμμή {t['target_row']})" if t.get("target_row") else "Στα Σβησμένα"
    if t.get("restore_status") == "failed":
        text += f" · η επαναφορά απέτυχε: {t.get('restore_error') or ''}"
    return text, True


def _transfers_tab() -> None:
    st.caption("Αν μεταφέρθηκε λάθος barcode, πατήστε «Επαναφορά στα Έτοιμα». Το προϊόν μπαίνει στην τελευταία "
               "ελεύθερη σειρά του φύλλου «΄Ετοιμα προϊόντα» σε νέα έκδοση Excel, επιστρέφει στην ενεργή λίστα "
               "και οι υπόλοιπες μεταφορές δεν επηρεάζονται.")
    term = st.text_input("Barcode", key="tr_search", placeholder="π.χ. VS11722")
    st.fragment(run_every=5)(_transfers_list)(term)


def _transfers_list(term: str) -> None:
    msg = st.session_state.pop("restore_msg", None)
    if msg:
        getattr(st, msg[0])(msg[1])
    try:
        rows = backend.list_transfers(principal(), barcode=term or None, limit=TRANSFERS_SHOWN)
    except ServiceError as exc:
        show_error(exc)
        return
    if not rows:
        st.info("Δεν υπάρχουν μεταφορές.")
        return
    st.caption(f"Οι {len(rows)} πιο πρόσφατες μεταφορές (έως {TRANSFERS_SHOWN}). "
               "Για παλαιότερες, γράψτε το barcode στην αναζήτηση.")
    widths = [1.1, 0.7, 1.4, 1.1, 2.2, 2.1]
    hdr = st.columns(widths)
    for col, label in zip(hdr, ["Barcode", "Κιλά", "Πότε", "Χρήστης", "Κατάσταση", ""]):
        col.markdown(f"<div class='hdr'>{label}</div>", unsafe_allow_html=True)
    for t in rows:
        c = st.columns(widths)
        text, can = _transfer_status(t)
        c[0].markdown(f"<div class='row-cell mono'>{t['barcode']}</div>", unsafe_allow_html=True)
        c[1].markdown(f"<div class='row-cell'>{format_kilos(t.get('kila'))}</div>", unsafe_allow_html=True)
        c[2].markdown(f"<div class='row-cell'>{fmt_local(t.get('finished_at') or t.get('queued_at'))}</div>",
                      unsafe_allow_html=True)
        c[3].markdown(f"<div class='row-cell'>{t.get('requested_by') or ''}</div>", unsafe_allow_html=True)
        c[4].markdown(f"<div class='row-cell'>{text}</div>", unsafe_allow_html=True)
        if can and c[5].button("↩ Επαναφορά στα Έτοιμα", key=f"restore-{t['id']}", width="stretch"):
            try:
                backend.restore_transfer(principal(), t["id"])
                st.session_state["restore_msg"] = (
                    "success", f"✔ Το {t['barcode']} επανέρχεται στα Έτοιμα προϊόντα και στην ενεργή λίστα. "
                               "Το Excel ενημερώνεται αυτόματα σε νέα έκδοση.")
            except ServiceError as exc:
                st.session_state["restore_msg"] = ("error", f"✖ {t['barcode']}: {exc.message}")
            st.rerun(scope="fragment")


def page() -> None:
    if not require("dashboard"):
        return
    st.title("Ιστορικό")
    tab_scans, tab_transfers = st.tabs(
        ["Σκαναρίσματα", "Μεταφορές & Επαναφορά"], key="hist_tabs", on_change="rerun")
    with tab_transfers:
        _transfers_tab()
    with tab_scans:
        _scans()


def _scans() -> None:
    today = datetime.now(local_tz()).date()
    st.caption("Μόνιμη καταγραφή κάθε σκαναρίσματος. Δεν διαγράφεται με μεταφορές, νέες εισαγωγές Excel ή επανεκκίνηση. "
               "Ώρες σε Europe/Athens.")
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
    try:
        rows = backend.history_rows(principal(), date_from=d_from, date_to=d_to, barcode=barcode or None,
                                    customer=customer or None, result=result or None,
                                    transfer_status=tstatus or None)
    except ServiceError as exc:
        show_error(exc)
        return
    st.write(f"**{len(rows)}** εγγραφές")
    if rows:
        df = pd.DataFrame(rows)[list(COLUMNS)].rename(columns=COLUMNS)
        st.dataframe(df, hide_index=True, width="stretch", height=560)

    c1, c2, _ = st.columns([1, 1, 3])
    for col, fmt, label, mime in (
        (c1, "csv", "CSV", "text/csv"),
        (c2, "xlsx", "Excel", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ):
        key = f"hist_export_{fmt}"
        if col.button(f"Προετοιμασία εξαγωγής {label}", key=f"prep_{fmt}"):
            try:
                st.session_state[key] = backend.history_export(principal(), rows, fmt)
            except ServiceError as exc:
                show_error(exc)
        if st.session_state.get(key):
            col.download_button(f"⬇ Λήψη {label}", st.session_state[key],
                                file_name=f"istoriko_{today:%Y%m%d}.{fmt}", mime=mime, key=f"dl_{fmt}")
