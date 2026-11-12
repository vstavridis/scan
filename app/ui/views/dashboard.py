"""Dashboard — ζωντανή εικόνα ενεργών barcode, SUM κιλών, μεταφορές."""
from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

import pandas as pd
import streamlit as st

from app.services.normalize import format_kilos, normalize_text, parse_kilos
from app.timeutil import fmt_local
from app import backend
from app.services.errors import ServiceError
from app.ui.common import principal, require, show_error

PAGE_SIZE = 40


def _matches(item: dict[str, Any], term: str) -> bool:
    if not term:
        return True
    t = normalize_text(term)
    hay = " ".join(str(item.get(k) or "") for k in ("barcode", "eidos", "pelatis", "diastaseis"))
    return t in normalize_text(hay) or term.strip().upper() in hay.upper()


FLASH_SECONDS = 20


def _flash(kind: str, msg: str) -> None:
    st.session_state.setdefault("dash_msgs", []).append((kind, msg, time.time()))


def _show_flash() -> None:
    """Τα μηνύματα μένουν ορατά για λίγο, παρά την αυτόματη ανανέωση κάθε δευτερόλεπτο."""
    now = time.time()
    msgs = [m for m in st.session_state.get("dash_msgs", []) if now - m[2] < FLASH_SECONDS]
    st.session_state["dash_msgs"] = msgs
    for kind, msg, _ts in msgs:
        getattr(st, kind)(msg)


def _invalidate() -> None:
    """Μετά από ενέργεια: νέα εικόνα αμέσως, χωρίς αναμονή για την επόμενη ανανέωση."""
    st.session_state.pop("secretary_snapshot", None)


def _transfer(items: list[dict[str, Any]]) -> None:
    """Γρήγορη καταχώριση μεταφοράς· το Excel ενημερώνεται στο παρασκήνιο (με ασφάλεια, μία φορά)."""
    try:
        res = backend.queue_transfers(principal(), [i["id"] for i in items])
    except ServiceError as exc:
        _flash("error", f"✖ {exc.message}")
        return
    queued = res["queued"]
    if len(queued) == 1:
        q = queued[0]
        _flash("success", f"✔ Το {q['barcode']} μεταφέρθηκε στα Σβησμένα "
                          f"({format_kilos(q.get('kila')) or '—'} κιλά). Το Excel ενημερώνεται αυτόματα.")
    elif queued:
        _flash("success", f"✔ Μεταφέρθηκαν {len(queued)} barcode στα Σβησμένα — σύνολο "
                          f"{res['total_kilos_display']} κιλά. Το Excel ενημερώνεται αυτόματα σε μία νέα έκδοση.")
    for sk in res["skipped"]:
        _flash("warning", f"⚠ {sk['barcode']}: {sk['reason']}")
    for q in queued:
        st.session_state.pop(f"sel-{q['item_id']}", None)
    _invalidate()


def _do(action: str, item: dict[str, Any], **payload: Any) -> None:
    p = principal()
    try:
        if action == "transfer":
            _transfer([item])
            return
        if action == "remove":
            backend.remove_item(p, item["id"], payload.get("reason", ""))
            _flash("info", f"Το {item['barcode']} αφαιρέθηκε από την ενεργή λίστα (καταγράφηκε στο ιστορικό).")
        elif action == "resolve":
            backend.resolve_item(p, item["id"], payload["product_record_id"])
            _flash("success", f"Ορίστηκε η γραμμή Excel {payload.get('sheet_row')} για το {item['barcode']}.")
        elif action == "correct":
            res = backend.correct_item(p, item["id"], payload["barcode"])
            kind = "success" if res["state"] == "ok" else "warning"
            _flash(kind, ("✔ " if kind == "success" else "⚠ ") + res["message"])
    except ServiceError as exc:
        _flash("error", f"✖ {item['barcode']}: {exc.message}")
    _invalidate()


def _metrics(totals: dict[str, Any]) -> None:
    c = st.columns(5)
    c[0].metric("Ενεργά barcode", totals["active_barcodes"])
    c[1].metric("Συνολικά κιλά", totals["total_kilos_display"])
    c[2].metric("Ομάδες προϊόντων", totals["groups"])
    c[3].metric("Διπλά σκαναρίσματα", totals["duplicate_scans"])
    c[4].metric("Άγνωστα / αμφίσημα", f"{totals['unknown']} / {totals['ambiguous']}")


def _groups_tab(groups: list[dict[str, Any]], items_by_id: dict[int, dict[str, Any]], term: str) -> None:
    if term:
        groups = [g for g in groups if any(_matches(items_by_id[b["id"]], term) for b in g["barcodes"])]
    if not groups:
        st.info("Δεν υπάρχουν ενεργά προϊόντα για ομαδοποίηση.")
        return
    df = pd.DataFrame([
        {"Προϊόν": g["product"], "Συνολικά κιλά": format_kilos(g["total"]), "Πελάτης": g["pelatis"], "Τεμ. barcode": g["count"]}
        for g in groups
    ])
    st.dataframe(df, hide_index=True, width="stretch",
                 column_config={"Συνολικά κιλά": st.column_config.TextColumn(width="small")})
    total = sum((Decimal(g["total"]) for g in groups), Decimal(0))
    st.markdown(f"<div class='total-line'>Γενικό σύνολο: {format_kilos(total)} κιλά</div>", unsafe_allow_html=True)
    if len(groups) > 15:
        # Streamlit renders expander contents even when the expander is closed.
        # Hundreds of dataframes rebuilt on each refresh used to keep the
        # browser constantly loading. For large batches render ONE detail list.
        st.caption("Επιλέξτε ομάδα για να δείτε τα barcode που την αποτελούν:")
        options = {str(i): g for i, g in enumerate(groups)}
        chosen = st.selectbox(
            "Αναλυτικά barcode ομάδας",
            options=list(options),
            format_func=lambda key: (
                f"{options[key]['product']} — {options[key]['pelatis']} "
                f"— {options[key]['total_display']} κιλά ({options[key]['count']})"
            ),
            key="dash_selected_group",
        )
        if chosen is not None:
            g = options[chosen]
            st.dataframe(pd.DataFrame([
                {"Barcode": b["barcode"], "Κιλά": format_kilos(b["kila"])}
                for b in g["barcodes"]
            ]), hide_index=True, width="stretch")
    else:
        st.caption("Ανάπτυξη ομάδας για τα barcode που τη συνθέτουν:")
        for g in groups:
            missing = f" · ⚠ {g['missing_kilos']} χωρίς κιλά" if g["missing_kilos"] else ""
            with st.expander(f"{g['product']}  —  {g['pelatis']}  —  {g['total_display']} κιλά  ({g['count']}){missing}"):
                st.dataframe(pd.DataFrame([
                    {"Barcode": b["barcode"], "Κιλά": format_kilos(b["kila"])}
                    for b in g["barcodes"]
                ]), hide_index=True, width="stretch")


def _selection_bar(rows: list[dict[str, Any]], chunk: list[dict[str, Any]]) -> None:
    """Μαζική μεταφορά: σύνολο επιλεγμένων και κιλών, ένα κουμπί για όλα."""
    ok_rows = [i for i in rows if i["state"] == "ok"]
    chosen = [i for i in ok_rows if st.session_state.get(f"sel-{i['id']}")]
    total = sum((parse_kilos(i.get("kila")) or Decimal(0) for i in chosen), Decimal(0))
    with st.container(border=True):
        c = st.columns([2.6, 1.3, 1.3, 2.4])
        c[0].markdown(f"<div class='row-cell'><b>Επιλεγμένα: {len(chosen)}</b> · σύνολο "
                      f"<b>{format_kilos(total)}</b> κιλά</div>", unsafe_allow_html=True)
        if c[1].button("Επιλογή σελίδας", key="selall-page", width="stretch"):
            for i in chunk:
                if i["state"] == "ok":
                    st.session_state[f"sel-{i['id']}"] = True
            st.rerun(scope="fragment")
        if c[2].button("Καθαρισμός", key="selall-clear", width="stretch", disabled=not chosen):
            for k in [k for k in st.session_state if str(k).startswith("sel-")]:
                st.session_state.pop(k, None)
            st.rerun(scope="fragment")
        if c[3].button(f"Μεταφορά επιλεγμένων στα Σβησμένα ({len(chosen)})", key="selall-transfer",
                       type="primary", width="stretch", disabled=not chosen):
            _transfer(chosen)
            st.rerun(scope="fragment")


def _detail_tab(items: list[dict[str, Any]], term: str) -> None:
    rows = [i for i in items if _matches(i, term)]
    if not rows:
        st.info("Δεν υπάρχουν εγγραφές.")
        return
    pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = st.number_input("Σελίδα", 1, pages, 1, key="dash_page") if pages > 1 else 1
    chunk = rows[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
    _selection_bar(rows, chunk)
    widths = [0.3, 1.15, 1.5, 0.65, 1.1, 0.65, 1.25, 1.05, 2.0, 0.6]
    hdr = st.columns(widths)
    for col, label in zip(hdr, ["", "Barcode", "Είδος", "Πάχος", "Διαστάσεις", "Κιλά", "Πελάτης", "Κατάσταση",
                                "Ενέργεια", ""]):
        col.markdown(f"<div class='hdr'>{label}</div>", unsafe_allow_html=True)
    for it in chunk:
        c = st.columns(widths)
        badge = f"<span class='badge b-{it['state']}'>{it['state_label']}</span>"
        if it.get("duplicate_count"):
            badge += f" <span class='badge b-dup'>διπλό ×{it['duplicate_count']}</span>"
        if it["state"] == "ok":
            c[0].checkbox("Επιλογή", key=f"sel-{it['id']}", label_visibility="collapsed")
        c[1].markdown(f"<div class='row-cell mono'>{it['barcode']}</div>", unsafe_allow_html=True)
        c[2].markdown(f"<div class='row-cell'>{it.get('eidos') or '—'}</div>", unsafe_allow_html=True)
        c[3].markdown(f"<div class='row-cell'>{it.get('paxos') or ''}</div>", unsafe_allow_html=True)
        c[4].markdown(f"<div class='row-cell'>{it.get('diastaseis') or ''}</div>", unsafe_allow_html=True)
        c[5].markdown(f"<div class='row-cell'>{format_kilos(it.get('kila')) if it['state']=='ok' else ''}</div>", unsafe_allow_html=True)
        c[6].markdown(f"<div class='row-cell'>{it.get('pelatis') or ''}</div>", unsafe_allow_html=True)
        c[7].markdown(f"<div class='row-cell'>{badge}</div>", unsafe_allow_html=True)
        if it["state"] == "ok":
            if c[8].button("Μεταφορά στα Σβησμένα", key=f"tr-{it['id']}", width="stretch"):
                _do("transfer", it)
                st.rerun(scope="fragment")
        else:
            c[8].markdown("<div class='row-cell'>Έλεγχος ⟶ «Προς έλεγχο»</div>", unsafe_allow_html=True)
        if c[9].button("✕", key=f"rm-{it['id']}", help="Αφαίρεση από την ενεργή λίστα"):
            _do("remove", it)
            st.rerun(scope="fragment")


def _review_tab(items: list[dict[str, Any]]) -> None:
    review = [i for i in items if i["state"] != "ok"]
    if not review:
        st.success("Δεν υπάρχουν barcode προς έλεγχο.")
        return
    for it in review:
        with st.container(border=True):
            editing = st.session_state.get("edit_item") == it["id"]
            h = st.columns([0.6, 8])
            if h[0].button("✏️", key=f"ed-{it['id']}", help="Διόρθωση barcode (π.χ. V11722 → VS11722)"):
                st.session_state["edit_item"] = None if editing else it["id"]
                st.rerun(scope="fragment")
            h[1].markdown(f"<div class='row-cell'><span class='mono'>{it['barcode']}</span> &nbsp; "
                          f"<span class='badge b-{it['state']}'>{it['state_label']}</span> &nbsp; "
                          f"<small>σκαναρίστηκε {fmt_local(it['created_at'])}</small></div>", unsafe_allow_html=True)
            if editing:
                with st.form(f"edit-form-{it['id']}", border=False):
                    new_bc = st.text_input("Σωστό barcode", value=it["barcode"], key=f"edv-{it['id']}")
                    e = st.columns([1, 1, 4])
                    save = e[0].form_submit_button("💾 Αποθήκευση", type="primary")
                    cancel = e[1].form_submit_button("Άκυρο")
                if save:
                    st.session_state.pop("edit_item", None)
                    _do("correct", it, barcode=new_bc)
                    st.rerun(scope="fragment")
                if cancel:
                    st.session_state.pop("edit_item", None)
                    st.rerun(scope="fragment")
            if it.get("message"):
                st.caption(it["message"])
            if it["state"] == "ambiguous":
                cache = st.session_state.setdefault("cands", {})
                cands = cache.get(it["id"])
                if cands is None:
                    try:
                        cands = cache[it["id"]] = backend.candidates(principal(), it["id"])
                    except ServiceError as exc:
                        st.error(exc.message)
                        cands = []
                st.write("Πιθανές αντιστοιχίσεις στο Excel — επιλέξτε τη σωστή γραμμή:")
                for cnd in cands:
                    c = st.columns([0.8, 4, 1.6])
                    c[0].markdown(f"<div class='row-cell'>Γραμμή {cnd['sheet_row']}</div>", unsafe_allow_html=True)
                    c[1].markdown(
                        f"<div class='row-cell'>{cnd['eidos']} × {cnd['paxos']} × {cnd['diastaseis']} / "
                        f"<b>{format_kilos(cnd['kila'])}</b> κιλά - {cnd['pelatis']} · τεμ. {cnd['temaxia']} · "
                        f"{cnd['topothesia']} · ρολό {cnd['rolo']}</div>", unsafe_allow_html=True)
                    if c[2].button("Επιλογή αυτής", key=f"res-{it['id']}-{cnd['id']}"):
                        _do("resolve", it, product_record_id=cnd["id"], sheet_row=cnd["sheet_row"])
                        st.rerun(scope="fragment")
            if st.button("Αφαίρεση από τη λίστα", key=f"rmr-{it['id']}"):
                _do("remove", it)
                st.rerun(scope="fragment")


def page() -> None:
    if not require("dashboard"):
        return
    st.title("Dashboard — Ζωντανή εικόνα")
    term = st.text_input("🔎 Αναζήτηση (barcode, είδος ή πελάτης)", key="dash_search", placeholder="π.χ. VS114 ή ΑΠΟΘΗΚΗ")
    try:
        configured_refresh = float(backend.public_config()["dashboard_refresh_seconds"])
    except Exception:
        configured_refresh = 4.0
    # An unconditional Streamlit fragment every 1.5s keeps the app visibly
    # "running" and continuously rebuilds dataframes. Four seconds gives a
    # predictable near-live view while reducing server/browser load.
    refresh = max(4.0, configured_refresh)

    @st.fragment(run_every=refresh)
    def live() -> None:
        try:
            cached = st.session_state.get("secretary_snapshot")
            # Don't reuse a snapshot from a previous deployment missing new
            # fields, even when the underlying data version hasn't changed.
            old_version = (int(cached["version"]) if cached is not None
                           and "sync" in cached else None)
            fresh = backend.dashboard_if_changed(principal(), old_version)
            if fresh is not None:
                st.session_state["secretary_snapshot"] = fresh
            data = st.session_state.get("secretary_snapshot")
            if data is None:
                data = backend.dashboard(principal())
                st.session_state["secretary_snapshot"] = data
        except ServiceError as exc:
            show_error(exc)
            return
        imp = data.get("import")
        if imp is None:
            st.error("Δεν υπάρχει ενεργό Excel. Ο διαχειριστής πρέπει να ανεβάσει αρχείο από τις Ρυθμίσεις.")
        else:
            st.caption(f"Ενεργό Excel: **{imp['original_filename']}** · εισαγωγή #{imp['id']} · "
                       f"{fmt_local(imp['imported_at'])} · αυτόματη ανανέωση κάθε {refresh:g} δευτ.")
        _show_flash()
        # Already included in the consistent dashboard snapshot: no extra
        # OneDrive credential or DB reads on the critical refresh path.
        sync = data.get("sync") or {}
        if imp is not None and sync.get("pending"):
            st.info(f"⏳ Ενημέρωση Excel σε εξέλιξη: {sync['pending']} μεταφορές/επαναφορές σε αναμονή "
                    "(γίνεται αυτόματα). Λεπτομέρειες: Ιστορικό → Συγχρονισμός.")
        if imp is not None and data.get("onedrive_pending"):
            err = sync.get("onedrive_last_error")
            reason = (f"  \n**Αιτία τελευταίας προσπάθειας** ({fmt_local(err['at'])}): {err['error']}"
                      if err else "")
            st.warning("Το online Excel στο OneDrive δεν έχει ακόμη όλες τις μεταφορές. Οι μεταφορές είναι "
                       "αποθηκευμένες με ασφάλεια στο Supabase και η αποθήκη δουλεύει κανονικά· το OneDrive "
                       "ξαναδοκιμάζεται αυτόματα. Αν το αρχείο είναι ανοιχτό στο Excel Online, κλείστε το για "
                       "λίγο ώστε να ενημερωθεί. Λεπτομέρειες: Ιστορικό → Συγχρονισμός." + reason)
        _metrics(data["totals"])
        items = data["items"]
        items_by_id = {i["id"]: i for i in items}
        tab_sum, tab_list, tab_review = st.tabs(
            ["Συγκεντρωτικά (SUM)", "Αναλυτική λίστα", "Προς έλεγχο"], key="dash_tabs", on_change="rerun")
        with tab_sum:
            _groups_tab(data["groups"], items_by_id, term)
        with tab_list:
            _detail_tab(items, term)
        with tab_review:
            n = data["totals"]["problem"]
            if n:
                st.error(f"{n} barcode χρειάζονται έλεγχο (αμφίσημα ή άγνωστα).")
            _review_tab(items)

    live()

