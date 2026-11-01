"""Κοινά στοιχεία του Streamlit UI: σύνδεση, ρόλοι, στυλ."""
from __future__ import annotations

import streamlit as st

from app import backend
from app.auth import Principal
from app.services.errors import ServiceError

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


def _cloud_email() -> str | None:
    """Email του προσκεκλημένου χρήστη (Private app στο Streamlit Cloud), αν είναι διαθέσιμο."""
    try:
        user = st.user
        if getattr(user, "is_logged_in", None) is False:
            return None
        email = user.get("email") if hasattr(user, "get") else getattr(user, "email", None)
        return str(email) if email and "@" in str(email) else None
    except Exception:
        return None


def principal() -> Principal:
    """Ο χρήστης της συνεδρίας: email από το Streamlit Cloud + προαιρετικό όνομα (για το audit)."""
    return backend.identity(st.session_state.get("wh_user_name"), _cloud_email())


def require(_permission: str = "") -> bool:
    """Συμβατότητα: όλοι οι προσκεκλημένοι χρήστες έχουν πρόσβαση σε όλες τις οθόνες."""
    return True


def show_error(exc: ServiceError) -> None:
    st.error(exc.message)


def user_sidebar() -> None:
    """Ποιος χρησιμοποιεί την εφαρμογή (για το ιστορικό). Χωρίς κωδικούς."""
    with st.sidebar:
        st.markdown("### 🏭 Αποθήκη Barcode")
        email = _cloud_email()
        if email:
            st.caption(f"Συνδεδεμένος μέσω Streamlit: **{email}**")
        if "wh_user_name" not in st.session_state:
            st.session_state["wh_user_name"] = (st.query_params.get("user") or "")[:60]

        def _remember() -> None:  # το όνομα μένει στο URL (σελιδοδείκτης) μετά από ανανέωση
            name = st.session_state.get("wh_user_name", "").strip()
            if name:
                st.query_params["user"] = name
            elif "user" in st.query_params:
                del st.query_params["user"]

        st.text_input("Όνομα χρήστη / συσκευής (για το ιστορικό)", key="wh_user_name", max_chars=60,
                      placeholder="π.χ. Μαρία ή Αποθήκη PC1", on_change=_remember)
        name = st.session_state.get("wh_user_name", "").strip()
        if name and st.query_params.get("user") != name:
            st.query_params["user"] = name   # η πλοήγηση στο μενού αφαιρεί τις παραμέτρους του URL
