"""Κοινά στοιχεία του Streamlit UI: σύνδεση, ρόλοι, στυλ."""
from __future__ import annotations

import streamlit as st

from app import backend
from app.auth import ROLE_LABELS, Principal
from app.services.errors import ServiceError

PERMISSIONS = {
    "dashboard": {"secretary", "admin"},
    "admin": {"admin"},
    "scan": {"scanner", "admin"},
    "view": {"scanner", "secretary", "admin"},
}

CSS = """
<style>
.block-container { padding-top: 1.6rem; max-width: 1500px; }
div[data-testid="stMetric"] { background: #f6f7f9; border: 1px solid #e3e6ea; border-radius: 10px; padding: 10px 14px; }
div[data-testid="stMetricValue"] { font-variant-numeric: tabular-nums; }
.badge { display:inline-block; padding:2px 10px; border-radius:999px; font-weight:700; font-size:0.85rem; }
.b-ok { background:#dcfce7; color:#14532d; } .b-ambiguous, .b-unknown { background:#fee2e2; color:#7f1d1d; }
.b-dup { background:#fef9c3; color:#713f12; }
.row-cell { padding-top: 0.45rem; font-size: 0.98rem; }
.mono { font-family: Consolas, "Roboto Mono", monospace; font-weight: 700; }
.hdr { font-weight: 700; color:#4b5563; border-bottom: 2px solid #e5e7eb; padding-bottom: 4px; }
.total-line { font-size: 1.35rem; font-weight: 800; margin-top: .4rem; }
/* Χωρίς "θάμπωμα" κατά τις περιοδικές ανανεώσεις (st.fragment run_every) */
[data-stale="true"] { opacity: 1 !important; transition: none !important; }
@media (prefers-color-scheme: dark) {
  div[data-testid="stMetric"] { background: #262a30; border-color: #3a3f47; }
}
</style>
"""


def inject_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def principal() -> Principal | None:
    return st.session_state.get("principal")


def current_role() -> str | None:
    p = principal()
    return p.role if p else None


def has(permission: str) -> bool:
    return current_role() in PERMISSIONS[permission]


def require(permission: str) -> bool:
    if has(permission):
        return True
    if current_role() is None:
        st.info("Συνδεθείτε από την πλαϊνή στήλη για να δείτε αυτή τη σελίδα.")
    else:
        st.error("Ο ρόλος σας δεν έχει πρόσβαση σε αυτή τη σελίδα.")
    return False


def show_error(exc: ServiceError) -> None:
    st.error(exc.message)


def logout() -> None:
    for k in list(st.session_state.keys()):
        del st.session_state[k]
    st.session_state["wh_clear_token"] = True
    st.session_state["wh_restore_done"] = True


def login_sidebar() -> None:
    with st.sidebar:
        st.markdown("### 🏭 Αποθήκη Barcode")
        p = principal()
        if p is not None:
            st.success(f"**{p.name}**  \n{ROLE_LABELS.get(p.role, '')}")
            if st.button("Αποσύνδεση", width="stretch"):
                logout()
                st.rerun()
            return
        with st.form("login"):
            st.markdown("**Σύνδεση**")
            role = st.selectbox("Ρόλος", ["scanner", "secretary", "admin"], format_func=lambda r: ROLE_LABELS[r])
            name = st.text_input("Όνομα χρήστη / συσκευής", max_chars=60, placeholder="π.χ. Μαρία ή Αποθήκη PC1")
            password = st.text_input("Κωδικός", type="password")
            remember = st.checkbox("Να με θυμάται αυτός ο υπολογιστής", value=True)
            ok = st.form_submit_button("Σύνδεση", width="stretch")
        if ok:
            try:
                p, token = backend.login(role, password, name)
            except ServiceError as exc:
                st.error(exc.message)
                return
            st.session_state["principal"] = p
            st.session_state["just_logged_in"] = True
            if remember:
                st.session_state["wh_store_token"] = token
            st.rerun()
