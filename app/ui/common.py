"""Κοινά στοιχεία του Streamlit UI: σύνδεση, ρόλοι, στυλ."""
from __future__ import annotations

import streamlit as st

from app.config import get_settings
from app.ui.api_client import ApiClient, ApiError

ROLE_LABELS = {"scanner": "Αποθήκη (Scanner)", "secretary": "Γραμματεία", "admin": "Διαχειριστής"}
PERMISSIONS = {
    "dashboard": {"secretary", "admin"},
    "admin": {"admin"},
    "view": {"scanner", "secretary", "admin"},
}

CSS = """
<style>
:root { --ok:#15803d; --dup:#ca8a04; --err:#b91c1c; }
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
@media (prefers-color-scheme: dark) {
  div[data-testid="stMetric"] { background: #262a30; border-color: #3a3f47; }
}
</style>
"""


def inject_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def client() -> ApiClient:
    return ApiClient(st.session_state.get("token"))


def current_role() -> str | None:
    return st.session_state.get("role")


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


def handle_api_error(exc: ApiError) -> None:
    if exc.status == 401:
        for k in ("token", "role", "name"):
            st.session_state.pop(k, None)
        st.error("Η σύνδεση έληξε. Συνδεθείτε ξανά.")
    else:
        st.error(exc.message)


def login_sidebar() -> None:
    with st.sidebar:
        st.markdown("### 🏭 Αποθήκη Barcode")
        if st.session_state.get("token"):
            st.success(f"**{st.session_state.get('name')}**  \n{ROLE_LABELS.get(st.session_state.get('role'), '')}")
            if st.button("Αποσύνδεση", use_container_width=True):
                for k in list(st.session_state.keys()):
                    del st.session_state[k]
                st.rerun()
            return
        with st.form("login"):
            st.markdown("**Σύνδεση**")
            role = st.selectbox("Ρόλος", ["secretary", "admin", "scanner"], format_func=lambda r: ROLE_LABELS[r])
            name = st.text_input("Όνομα χρήστη", max_chars=60, placeholder="π.χ. Μαρία")
            password = st.text_input("Κωδικός", type="password")
            ok = st.form_submit_button("Σύνδεση", use_container_width=True)
        if ok:
            try:
                data = ApiClient().post("/api/auth/login", json={"role": role, "password": password, "name": name})
            except ApiError as exc:
                st.error(exc.message)
                return
            st.session_state.update(token=data["token"], role=data["role"], name=data["name"], just_logged_in=True)
            st.rerun()


def scanner_url() -> str:
    s = get_settings()
    host = "localhost"
    try:
        h = st.context.headers.get("host") or ""
        if h:
            host = h.rsplit(":", 1)[0] if not h.startswith("[") else h.split("]")[0] + "]"
    except Exception:
        pass
    return f"http://{host}:{s.api_port}/scanner/"
