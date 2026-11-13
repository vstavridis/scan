"""Ρυθμίσεις (μόνο Admin): ανέβασμα/έλεγχος/ενεργοποίηση Excel, λήψεις, backups, ρυθμίσεις scanner."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from app.timeutil import fmt_local
from app import backend
from app.services.errors import ServiceError
from app.ui.common import principal, require, show_error

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
               f"Εισαγωγή από: {active['imported_by']} · Τρέχουσα έκδοση working Excel: v{active['working_version']} "
               f"(κάθε μεταφορά δημιουργεί νέα έκδοση· οι προηγούμενες μένουν ως backups)")


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
        st.dataframe(df, hide_index=True, width="stretch")


def _upload_section() -> None:
    st.subheader("Ανέβασμα νέου Excel")
    up = st.file_uploader("Αρχείο .xlsx", type=["xlsx"], accept_multiple_files=False)
    if up is not None and st.button("Έλεγχος αρχείου", type="primary"):
        with st.spinner("Έλεγχος και αποθήκευση στο Supabase Storage…"):
            try:
                st.session_state["staged"] = backend.stage_excel(principal(), up.name, up.getvalue())
            except ServiceError as exc:
                show_error(exc)
    staged = st.session_state.get("staged")
    if not staged:
        return
    st.markdown(f"**Αρχείο προς ενεργοποίηση:** {staged['filename']}")
    _report(staged["report"])
    if not staged["report"]["ok"]:
        return
    confirm = st.checkbox(
        "Επιβεβαιώνω την αντικατάσταση του ενεργού Excel. Αν υπάρχουν ενεργά barcode στη λίστα, θα αντιστοιχιστούν "
        "ξανά στο νέο αρχείο (η προηγούμενη έκδοση του Excel μένει αποθηκευμένη ως backup).", key="confirm_replace")
    if st.button("Ενεργοποίηση αρχείου", type="primary"):
        try:
            with st.spinner("Ενεργοποίηση…"):
                res = backend.activate_excel(principal(), staged["staging_id"], confirm)
            st.session_state.pop("staged", None)
            st.success(f"✔ Ενεργοποιήθηκε η εισαγωγή #{res['import_id']} ({res['filename']}). "
                       f"Αντιστοίχιση ενεργής λίστας: {res['remapped']}")
            if res.get("backups"):
                st.info("Η προηγούμενη έκδοση παραμένει ως backup: " + ", ".join(res["backups"]))
        except ServiceError as exc:
            if exc.code == "confirmation_required":
                st.warning(exc.message + " Τσεκάρετε την επιβεβαίωση και πατήστε ξανά «Ενεργοποίηση».")
            else:
                show_error(exc)


def _onedrive_section() -> None:
    from app.services import onedrive

    st.subheader("OneDrive — online Excel")
    recovery_msg = st.session_state.pop("onedrive_recovery_success", None)
    if recovery_msg:
        st.success(recovery_msg)
    if not onedrive.configured():
        st.info("Προσθέστε [onedrive] client_id, client_secret, redirect_uri στα Streamlit Secrets.")
        return
    try:
        state = onedrive.status()
    except ServiceError as exc:
        show_error(exc)
        return
    if not state["connected"]:
        st.warning("Δεν έχει δοθεί ακόμη άδεια Microsoft.")
        st.link_button("🔗 Σύνδεση Microsoft OneDrive", onedrive.authorization_url())
        return
    st.success("Συνδεδεμένο Microsoft OneDrive: " + state["connected"])
    if state["bound"]:
        st.write("Συνδεδεμένο αρχείο:", state["name"])
        st.link_button("Άνοιγμα online Excel", state["link"])
        if state["pending"]:
            st.warning("Το OneDrive έχει εκκρεμείς αλλαγές. Οι σαρώσεις και οι μεταφορές "
                       "ΣΥΝΕΧΙΖΟΥΝ κανονικά στο Supabase. Το online Excel ενδέχεται "
                       "να δείχνει παλιότερη εικόνα μέχρι να επιτραπεί η ενημέρωσή του.")
            if st.button("Επανάληψη συγχρονισμού OneDrive"):
                try:
                    onedrive.retry_sync()
                    st.session_state["onedrive_recovery_success"] = (
                        "Το online Excel ενημερώθηκε με όλες τις εκκρεμείς μεταφορές.")
                    st.rerun()
                except ServiceError as exc:
                    show_error(exc)
            st.markdown("#### Έλεγχος πραγματικών διαφορών (χωρίς καμία αλλαγή)")
            st.caption("Σύγκριση του online αρχείου με το αντίγραφο πριν από την εκκρεμή μεταφορά. "
                       "Η σύγκριση διαβάζει μόνο τα δύο Excel και δεν τα αντικαθιστά.")
            if st.button("🔎 Σύγκριση OneDrive με το ασφαλές αντίγραφο", key="onedrive_compare"):
                try:
                    with st.spinner("Συγκρίνω τις τιμές και τους τύπους κελιών όλων των φύλλων…"):
                        st.session_state["onedrive_diff_report"] = onedrive.inspect_pending()
                except ServiceError as exc:
                    show_error(exc)
            report = st.session_state.get("onedrive_diff_report")
            if report:
                st.write(f"Σύγκριση: έκδοση πριν από τη μεταφορά **v{report['original_version']}** "
                         f"↔ online OneDrive · ενημερωμένη έκδοση Supabase **v{report['updated_version']}**")
                if report["online_identical_after"]:
                    st.success("Το online Excel έχει ήδη ακριβώς τα ίδια bytes με το ενημερωμένο Supabase. "
                               "Πατήστε «Επανάληψη συγχρονισμού» για να επιβεβαιωθεί η έκδοση.")
                elif report["online_identical_before"]:
                    st.success("Το online Excel είναι ακριβώς ίδιο με το αντίγραφο πριν από τη μεταφορά. "
                               "Ο συγχρονισμός μπορεί να επαναληφθεί χωρίς σύγκρουση περιεχομένου.")
                elif report["cell_values_equal"]:
                    st.warning("Όλες οι τιμές και οι τύποι κελιών είναι ίδιοι, αλλά τα αρχεία διαφέρουν "
                               "σε άλλα δεδομένα. Δεν γίνεται αυτόματη αντικατάσταση: μπορεί να έχουν "
                               "αλλάξει μορφοποιήσεις ή άλλα στοιχεία.")
                else:
                    st.error(f"Βρέθηκαν {report['differences']}"
                             + (" ή περισσότερες" if report["truncated"] else "")
                             + " διαφορές σε τιμές/τύπους κελιών. Απαιτείται έλεγχος πριν τη συγχώνευση.")
                if report["etag_changed"]:
                    st.caption("Το OneDrive έχει και διαφορετικό αναγνωριστικό έκδοσης (ETag).")
                st.caption(report["notes"])
                if report["per_sheet"]:
                    with st.expander("Διαφορές ανά φύλλο", expanded=True):
                        st.dataframe(pd.DataFrame(report["per_sheet"]).rename(columns={
                            "sheet": "Φύλλο", "different": "Κελιά με διαφορά"}),
                            hide_index=True, width="stretch")
                if report["examples"]:
                    st.write("Ενδεικτικά διαφορετικά κελιά:")
                    st.dataframe(pd.DataFrame(report["examples"]).rename(columns={
                        "sheet": "Φύλλο", "cell": "Κελί", "before": "Πριν από μεταφορά",
                        "online": "Στο OneDrive"}), hide_index=True, width="stretch")
                if report["cell_values_equal"] and not report["online_identical_after"]:
                    st.info("Όταν το online αρχείο έχει τις ίδιες τιμές, η "
                            "«Επανάληψη συγχρονισμού» επανεφαρμόζει τις εκκρεμείς "
                            "μεταφορές στην τρέχουσα έκδοση OneDrive, με ελέγχους "
                            "και αντίγραφα ασφαλείας. Αν το Excel Online κρατά "
                            "το αρχείο κλειδωμένο, θα παραμείνει εκκρεμές χωρίς "
                            "να διακοπεί η λειτουργία αποθήκης.")
        else:
            st.caption("Οι αλλαγές έχουν συγχρονιστεί. Εξωτερικές τροποποιήσεις ελέγχονται πριν από τη μεταφορά.")
    st.caption("Δοκιμή προσωπικού OneDrive. Το ιστορικό και τα backups παραμένουν στο Supabase.")
    url = st.text_input("Σύνδεσμος Excel από OneDrive", placeholder="https://1drv.ms/x/...", key="onedrive_share_url")
    if st.button("Έλεγχος Excel από OneDrive", disabled=not url.strip()):
        try:
            with st.spinner("Διαβάζω το online Excel…"):
                st.session_state["onedrive_stage"] = onedrive.stage_from_link(url, principal().actor)
        except ServiceError as exc:
            show_error(exc)
    stage = st.session_state.get("onedrive_stage")
    if not stage:
        return
    st.write("Προεπισκόπηση:", stage["name"])
    _report(stage["staged"]["report"])
    if not stage["staged"]["report"]["ok"]:
        return
    confirmed = st.checkbox("Επιβεβαιώνω την αντικατάσταση του ενεργού καταλόγου από το δοκιμαστικό OneDrive Excel.", key="onedrive_confirm")
    if st.button("Ενεργοποίηση OneDrive Excel", disabled=not confirmed):
        try:
            res = onedrive.activate_staged(stage, principal().actor, confirm_replace=True)
            st.session_state.pop("onedrive_stage", None)
            st.success(f"Συνδέθηκε το Excel — εισαγωγή #{res['import_id']}.")
            st.rerun()
        except ServiceError as exc:
            show_error(exc)


def _downloads() -> None:
    st.subheader("Λήψεις και αντίγραφα ασφαλείας")
    p = principal()
    c1, c2 = st.columns(2)
    if c1.button("Προετοιμασία τρέχοντος ενημερωμένου Excel"):
        try:
            st.session_state["working_xlsx"] = backend.working_excel(p)
        except ServiceError as exc:
            show_error(exc)
    if st.session_state.get("working_xlsx"):
        name, data = st.session_state["working_xlsx"]
        c1.download_button("⬇ Λήψη ενημερωμένου Excel", data, file_name=name, mime=XLSX_MIME)
    if c2.button("Δημιουργία αντιγράφου ασφαλείας τώρα"):
        try:
            c2.success(f"Δημιουργήθηκε: {backend.make_backup(p)}")
        except ServiceError as exc:
            show_error(exc)
    try:
        backups = backend.backups(p)
    except ServiceError as exc:
        show_error(exc)
        return
    if backups:
        with st.expander(f"Αποθηκευμένα αρχεία / backups ({len(backups)})"):
            st.dataframe(pd.DataFrame([{"Αρχείο": b["path"], "Είδος": b["kind"], "Μέγεθος (KB)": round(b["size"] / 1024),
                                        "Ενημέρωση": b["updated_at"][:19].replace("T", " ")} for b in backups]),
                         hide_index=True, width="stretch")
            sel = st.selectbox("Επιλογή αρχείου για λήψη", [b["path"] for b in backups])
            if st.button("Προετοιμασία λήψης"):
                try:
                    st.session_state["backup_dl"] = (sel, backend.backup_bytes(p, sel))
                except ServiceError as exc:
                    show_error(exc)
            if st.session_state.get("backup_dl"):
                name, data = st.session_state["backup_dl"]
                st.download_button(f"⬇ {name}", data, file_name=name.replace("/", "_"), mime=XLSX_MIME)


def _scanner_settings() -> None:
    st.subheader("Ρυθμίσεις σάρωσης και μεταφοράς")
    try:
        cfg = backend.get_settings_values(principal())
    except ServiceError as exc:
        show_error(exc)
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
        autofs = st.checkbox("Η σελίδα Scanner ανοίγει σε πλήρη οθόνη", value=cfg["scanner_auto_fullscreen"],
                             help="Η λίστα καλύπτει αμέσως όλο το παράθυρο· η πλήρης οθόνη του browser ενεργοποιείται "
                                  "με το πρώτο κλικ ή το πρώτο barcode (ο browser δεν επιτρέπει να γίνει μόνη της). "
                                  "Έξοδος με Esc.")
        gap = st.number_input("Μέγιστο κενό μεταξύ πλήκτρων (ms, 0 = χωρίς όριο)", 0, 5000,
                              int(cfg["scanner_inter_key_timeout_ms"]), step=50)
        refresh = st.number_input("Ανανέωση dashboard (δευτερόλεπτα)", 0.5, 60.0, float(cfg["dashboard_refresh_seconds"]), step=0.5)
        st.caption("Η Γραμματεία ανανεώνεται τουλάχιστον κάθε 4 δευτερόλεπτα, "
                   "ακόμη κι αν έχει αποθηκευτεί μικρότερη τιμή, ώστε να μην φορτώνει αδιάκοπα. "
                   "Ο scanner έχει ξεχωριστό ρυθμό και δεν επηρεάζεται.")
        srefresh = st.number_input("Ανανέωση λίστας στην οθόνη σάρωσης (δευτερόλεπτα)", 0.5, 60.0,
                                   float(cfg["scanner_refresh_seconds"]), step=0.5)
        mode = st.selectbox("Τρόπος αφαίρεσης από «Έτοιμα προϊόντα»", ["auto", "shift", "clear"],
                            index=["auto", "shift", "clear"].index(cfg["transfer_mode"]),
                            format_func=lambda v: {
                                "auto": "Αυτόματα: διαγραφή γραμμής όταν είναι ασφαλής, αλλιώς καθαρισμός (συνιστάται)",
                                "shift": "Μόνο διαγραφή γραμμής (αλλιώς η μεταφορά ακυρώνεται)",
                                "clear": "Μόνο καθαρισμός γραμμής (η γραμμή μένει κενή)"}[v])
        if st.form_submit_button("Αποθήκευση ρυθμίσεων", type="primary"):
            try:
                backend.update_settings(principal(), {
                    "scanner_terminator": term, "scanner_key_mapping": mapping, "scanner_sound_default": sound,
                    "scanner_auto_fullscreen": autofs,
                    "scanner_inter_key_timeout_ms": int(gap), "dashboard_refresh_seconds": float(refresh),
                    "scanner_refresh_seconds": float(srefresh), "transfer_mode": mode})
                st.success("Οι ρυθμίσεις αποθηκεύτηκαν. Οι οθόνες τις παίρνουν με την επόμενη ανανέωση σελίδας.")
            except ServiceError as exc:
                show_error(exc)


def page() -> None:
    if not require("admin"):
        return
    st.title("Ρυθμίσεις")
    p = principal()
    try:
        health = backend.health()
        imports = backend.imports(p)
    except ServiceError as exc:
        show_error(exc)
        return
    for problem in health["problems"]:
        st.error(problem)
    st.caption(f"Βάση δεδομένων: {health['database']} · Αρχεία: {health.get('storage', '')}")
    with st.expander("Διάγνωση σύνδεσης βάσης"):
        st.caption("Ανεξάρτητη δοκιμαστική σύνδεση: ρυθμίσεις → DNS → TCP → PostgreSQL. "
                   "Δεν εμφανίζονται κωδικοί ή το connection string.")
        if st.button("Εκτέλεση διάγνωσης"):
            d = backend.diagnose_database(p)
            (st.success if d.ok else st.error)(f"**{d.title}** — {d.summary} ({d.elapsed_ms} ms)")
            for line in d.details:
                st.markdown(f"- {line}")
            for hint in d.hints:
                st.info(hint)
    _active_import_box(imports)
    st.divider()
    _onedrive_section()
    st.divider()
    _upload_section()
    st.divider()
    _downloads()
    st.divider()
    _scanner_settings()
    st.divider()
    with st.expander(f"Ιστορικό εισαγωγών Excel ({len(imports)})"):
        if imports:
            st.dataframe(pd.DataFrame([{
                "Import ID": i["id"], "Αρχείο": i["original_filename"], "Ημερομηνία": fmt_local(i["imported_at"]),
                "Από": i["imported_by"], "Κατάσταση": i["status"], "Ενεργό": "✔" if i["is_active"] else "",
                "Έκδοση working": i["working_version"], "Προϊόντα": i["product_count"],
                "Διπλά": i["duplicate_barcodes"]} for i in imports]), hide_index=True, width="stretch")
    with st.expander("Αρχείο ενεργειών (audit log)"):
        try:
            log = backend.audit_log(p)
            st.dataframe(pd.DataFrame([{"Ώρα": r["ts_local"], "Χρήστης": r["actor"], "Ενέργεια": r["action"],
                                        "Λεπτομέρειες": r["details"]} for r in log]), hide_index=True, width="stretch")
        except ServiceError as exc:
            show_error(exc)
