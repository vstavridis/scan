"""Ρυθμίσεις (μόνο Admin): ανέβασμα/έλεγχος/ενεργοποίηση Excel, λήψεις, backups, ρυθμίσεις scanner."""
from __future__ import annotations

from urllib.parse import quote

import pandas as pd
import streamlit as st

from app.timeutil import fmt_local
from app.ui.api_client import ApiError
from app.ui.common import client, handle_api_error, require

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _active_import_box(imports: list[dict]) -> None:
    active = next((i for i in imports if i["is_active"]), None)
    st.subheader("Ενεργό αρχείο Excel")
    if not active:
        st.error("Δεν υπάρχει ενεργό Excel. Οι σαρώσεις δεν θεωρούνται έγκυρες μέχρι να ενεργοποιηθεί αρχείο.")
        return
    c = st.columns(4)
    c[0].metric("Αρχείο", active["original_filename"])
    c[1].metric("Τελευταία εισαγωγή", fmt_local(active["imported_at"]))
    c[2].metric("Import ID", f"#{active['id']}")
    c[3].metric("Προϊόντα", active["product_count"])
    st.caption(f"Μοναδικά barcode: {active['unique_barcodes']} · Διπλά barcode στο Excel: {active['duplicate_barcodes']} · "
               f"Εισαγωγή από: {active['imported_by']}")


def _report(rep: dict) -> None:
    if rep["ok"]:
        st.success("✔ Το αρχείο πέρασε τον έλεγχο.")
    for e in rep["errors"]:
        st.error(e)
    for w in rep["warnings"]:
        st.warning(w)
    c = st.columns(4)
    c[0].metric("Διαθέσιμα προϊόντα", rep["product_count"])
    c[1].metric("Μοναδικά barcode", rep["unique_barcodes"])
    c[2].metric("Διπλά barcode στο Excel", rep["duplicate_barcodes"])
    c[3].metric("Γραμμές χωρίς barcode", len(rep["rows_without_barcode"]))
    with st.expander("Φύλλα και επικεφαλίδες"):
        st.write("Φύλλα του workbook:", ", ".join(f"«{n}»" for n in rep["sheet_names"]))
        st.write(f"Φύλλο πηγής: «{rep['source_sheet']}» — επικεφαλίδες A–K: {rep['headers']}")
        st.write(f"Φύλλο προορισμού: «{rep['target_sheet']}» — επικεφαλίδες A–K: {rep['target_headers']}")
    if rep["duplicates"]:
        with st.expander(f"Διπλά barcode ({len(rep['duplicates'])})", expanded=True):
            st.dataframe(pd.DataFrame([{"Barcode": k, "Γραμμές Excel": ", ".join(map(str, v))}
                                       for k, v in rep["duplicates"].items()]), hide_index=True)
    if rep.get("preview"):
        st.write("Προεπισκόπηση (πρώτες γραμμές):")
        df = pd.DataFrame(rep["preview"]).rename(columns={
            "row": "Γραμμή", "barcode": "BARCODE", "eidos": "ΕΙΔΟΣ", "paxos": "ΠΑΧΟΣ", "diastaseis": "ΔΙΑΣΤΑΣΕΙΣ",
            "temaxia": "ΤΕΜΑΧΙΑ", "kila": "ΚΙΛΑ", "pelatis": "ΠΕΛΑΤΗΣ", "vspg": "VS/PG", "topothesia": "ΤΟΠΟΘΕΣΙΑ",
            "thesi": "ΘΕΣΗ", "rolo": "ΡΟΛΟ"})
        st.dataframe(df, hide_index=True, use_container_width=True)


def _upload_section(api) -> None:
    st.subheader("Ανέβασμα νέου Excel")
    up = st.file_uploader("Αρχείο .xlsx", type=["xlsx"], accept_multiple_files=False)
    if up is not None and st.button("Έλεγχος αρχείου", type="primary"):
        with st.spinner("Αποθήκευση και έλεγχος…"):
            try:
                res = api.post(f"/api/admin/imports/stage?filename={quote(up.name)}", content=up.getvalue(), timeout=180)
                st.session_state["staged"] = res
            except ApiError as exc:
                handle_api_error(exc)
    staged = st.session_state.get("staged")
    if not staged:
        return
    st.markdown(f"**Αρχείο προς ενεργοποίηση:** {staged['filename']}")
    _report(staged["report"])
    if not staged["report"]["ok"]:
        return
    confirm = st.checkbox(
        "Επιβεβαιώνω την αντικατάσταση του ενεργού Excel. Αν υπάρχουν ενεργά barcode στη λίστα, θα αντιστοιχιστούν "
        "ξανά στο νέο αρχείο (δημιουργείται αντίγραφο ασφαλείας πριν την αλλαγή).", key="confirm_replace")
    if st.button("Ενεργοποίηση αρχείου", type="primary"):
        try:
            with st.spinner("Ενεργοποίηση…"):
                res = api.post("/api/admin/imports/activate",
                               json={"staging_id": staged["staging_id"], "confirm_replace": confirm}, timeout=300)
            st.session_state.pop("staged", None)
            st.success(f"✔ Ενεργοποιήθηκε η εισαγωγή #{res['import_id']} ({res['filename']}). "
                       f"Αντιστοίχιση ενεργής λίστας: {res['remapped']}")
            if res.get("backups"):
                st.info("Αντίγραφα ασφαλείας: " + ", ".join(b.split('/')[-1].split('\\')[-1] for b in res["backups"]))
        except ApiError as exc:
            if exc.code == "confirmation_required":
                st.warning(exc.message + " Τσεκάρετε την επιβεβαίωση και πατήστε ξανά «Ενεργοποίηση».")
            else:
                handle_api_error(exc)


def _downloads(api) -> None:
    st.subheader("Λήψεις και αντίγραφα ασφαλείας")
    c1, c2 = st.columns(2)
    if c1.button("Προετοιμασία τρέχοντος ενημερωμένου Excel"):
        try:
            st.session_state["working_xlsx"] = api.get("/api/admin/download/working", raw=True, timeout=120)
        except ApiError as exc:
            handle_api_error(exc)
    if st.session_state.get("working_xlsx"):
        c1.download_button("⬇ Λήψη ενημερωμένου Excel", st.session_state["working_xlsx"],
                           file_name="enimeromeno.xlsx", mime=XLSX_MIME)
    if c2.button("Δημιουργία αντιγράφου ασφαλείας τώρα"):
        try:
            res = api.post("/api/admin/backups")
            c2.success(f"Δημιουργήθηκε: {res['name']}")
        except ApiError as exc:
            handle_api_error(exc)
    try:
        backups = api.get("/api/admin/backups")
    except ApiError as exc:
        handle_api_error(exc)
        return
    if backups:
        with st.expander(f"Αντίγραφα ασφαλείας ({len(backups)})"):
            names = [b["name"] for b in backups]
            sel = st.selectbox("Επιλογή αντιγράφου", names,
                               format_func=lambda n: f"{n}  ({next(b for b in backups if b['name']==n)['modified']})")
            if st.button("Προετοιμασία λήψης αντιγράφου"):
                try:
                    st.session_state["backup_dl"] = (sel, api.get(f"/api/admin/backups/{quote(sel)}", raw=True, timeout=120))
                except ApiError as exc:
                    handle_api_error(exc)
            if st.session_state.get("backup_dl"):
                name, data = st.session_state["backup_dl"]
                st.download_button(f"⬇ {name}", data, file_name=name)


def _scanner_settings(api) -> None:
    st.subheader("Ρυθμίσεις σάρωσης και μεταφοράς")
    try:
        cfg = api.get("/api/admin/settings")
    except ApiError as exc:
        handle_api_error(exc)
        return
    with st.form("cfg"):
        term = st.radio("Τερματικός χαρακτήρας scanner", ["enter", "enter_tab"],
                        index=["enter", "enter_tab"].index(cfg["scanner_terminator"]),
                        format_func=lambda v: {"enter": "Enter", "enter_tab": "Enter ή Tab"}[v], horizontal=True)
        mapping = st.radio("Ανάγνωση πλήκτρων", ["physical", "layout"],
                           index=["physical", "layout"].index(cfg["scanner_key_mapping"]),
                           format_func=lambda v: {"physical": "Φυσικά πλήκτρα (σωστό ακόμη και με ελληνική διάταξη)",
                                                  "layout": "Τρέχουσα διάταξη πληκτρολογίου"}[v])
        sound = st.checkbox("Ήχος ενεργός από προεπιλογή", value=cfg["scanner_sound_default"])
        gap = st.number_input("Μέγιστο κενό μεταξύ πλήκτρων (ms, 0 = χωρίς όριο)", 0, 5000,
                              int(cfg["scanner_inter_key_timeout_ms"]), step=50)
        refresh = st.number_input("Ανανέωση dashboard (δευτερόλεπτα)", 0.2, 60.0, float(cfg["dashboard_refresh_seconds"]), step=0.5)
        mode = st.selectbox("Τρόπος αφαίρεσης από «Έτοιμα προϊόντα»", ["auto", "shift", "clear"],
                            index=["auto", "shift", "clear"].index(cfg["transfer_mode"]),
                            format_func=lambda v: {
                                "auto": "Αυτόματα: διαγραφή γραμμής όταν είναι ασφαλής, αλλιώς καθαρισμός (συνιστάται)",
                                "shift": "Μόνο διαγραφή γραμμής (αλλιώς η μεταφορά ακυρώνεται)",
                                "clear": "Μόνο καθαρισμός γραμμής (η γραμμή μένει κενή)"}[v])
        if st.form_submit_button("Αποθήκευση ρυθμίσεων", type="primary"):
            try:
                api.put("/api/admin/settings", json={
                    "scanner_terminator": term, "scanner_key_mapping": mapping, "scanner_sound_default": sound,
                    "scanner_inter_key_timeout_ms": int(gap), "dashboard_refresh_seconds": float(refresh),
                    "transfer_mode": mode})
                st.session_state["dash_refresh"] = float(refresh)
                st.success("Οι ρυθμίσεις αποθηκεύτηκαν. Η οθόνη σάρωσης τις παίρνει αυτόματα μέσα σε 30 δευτ. (ή με ανανέωση).")
            except ApiError as exc:
                handle_api_error(exc)


def page() -> None:
    if not require("admin"):
        return
    st.title("Ρυθμίσεις")
    api = client()
    try:
        imports = api.get("/api/admin/imports")
    except ApiError as exc:
        handle_api_error(exc)
        return
    _active_import_box(imports)
    st.divider()
    _upload_section(api)
    st.divider()
    _downloads(api)
    st.divider()
    _scanner_settings(api)
    st.divider()
    with st.expander(f"Ιστορικό εισαγωγών Excel ({len(imports)})"):
        if imports:
            st.dataframe(pd.DataFrame([{
                "Import ID": i["id"], "Αρχείο": i["original_filename"], "Ημερομηνία": fmt_local(i["imported_at"]),
                "Από": i["imported_by"], "Ενεργό": "✔" if i["is_active"] else "", "Προϊόντα": i["product_count"],
                "Διπλά": i["duplicate_barcodes"]} for i in imports]), hide_index=True, use_container_width=True)
    with st.expander("Αρχείο ενεργειών (audit log)"):
        try:
            log = api.get("/api/audit")
            st.dataframe(pd.DataFrame([{"Ώρα": r["ts_local"], "Χρήστης": r["actor"], "Ενέργεια": r["action"],
                                        "Λεπτομέρειες": r["details"]} for r in log]), hide_index=True, use_container_width=True)
        except ApiError as exc:
            handle_api_error(exc)
