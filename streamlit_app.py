"""Entrypoint για το Streamlit Community Cloud: Αποθήκη Barcode.

Ένα μόνο deployment· όλες οι οθόνες και η επιχειρησιακή λογική τρέχουν εδώ. Μόνιμα δεδομένα:
Supabase PostgreSQL + Supabase Storage (private bucket). Ρυθμίσεις: Streamlit Secrets.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

from app import backend  # noqa: E402
from app.config import get_settings  # noqa: E402

st.set_page_config(page_title="Αποθήκη Barcode", page_icon="📦", layout="wide")

problems = get_settings().problems()
if problems:
    st.title("Αποθήκη Barcode — απαιτείται ρύθμιση")
    st.error("Η εφαρμογή δεν έχει ρυθμιστεί ακόμη. Συμπληρώστε τα Streamlit Secrets (βλ. README / secrets.toml.example):")
    for p in problems:
        st.markdown(f"- {p}")
    st.stop()


from app.db.diagnostics import DatabaseConnectionError  # noqa: E402


def show_db_problem(exc: DatabaseConnectionError) -> None:
    """Η πραγματική αιτία (χωρίς κωδικούς/connection string) αντί για γενικό PoolTimeout."""
    d = exc.diagnosis
    st.error(f"**Η βάση δεδομένων δεν είναι διαθέσιμη — {d.title}**  \n{d.summary}")
    if d.target and not d.target.get("parse_error"):
        st.caption(f"Στόχος: `{d.target.get('user')}@{d.target.get('host')}:{d.target.get('port')}/"
                   f"{d.target.get('dbname')}` · sslmode: `{d.target.get('sslmode')}`")
    for line in d.details:
        st.markdown(f"- {line}")
    for hint in d.hints:
        st.info(hint)
    if st.button("Δοκιμή ξανά"):
        _startup.clear()
        st.rerun()


@st.cache_resource(show_spinner="Σύνδεση με τη βάση δεδομένων…")
def _startup() -> bool:
    backend.startup()  # σχήμα βάσης + recovery, μία φορά ανά διεργασία
    return True


try:
    _startup()
except DatabaseConnectionError as exc:
    st.title("Αποθήκη Barcode")
    show_db_problem(exc)
    st.stop()
except Exception as exc:  # pragma: no cover - εμφανίζεται στον χρήστη
    st.error(f"Δεν ήταν δυνατή η εκκίνηση: {type(exc).__name__}")
    st.stop()

from app.ui.common import inject_css, user_sidebar  # noqa: E402
from app.ui.views import dashboard, history, scanner, settings  # noqa: E402

if "code" in st.query_params and "state" in st.query_params:
    from app.services import onedrive
    try:
        account = onedrive.finish_authorization(str(st.query_params["code"]), str(st.query_params["state"]))
        st.session_state["onedrive_auth_message"] = "Επιτυχής σύνδεση Microsoft: " + account
    except Exception:
        st.session_state["onedrive_auth_message"] = "Η σύνδεση Microsoft απέτυχε. Δοκιμάστε ξανά από τις Ρυθμίσεις."
    st.query_params.clear()
    st.rerun()
elif "error" in st.query_params and "state" in st.query_params:
    st.session_state["onedrive_auth_message"] = "Η Microsoft δεν έδωσε άδεια σύνδεσης."
    st.query_params.clear()
    st.rerun()

inject_css()
user_sidebar()
if st.session_state.pop("onedrive_auth_message", None):
    st.info("Η Microsoft επέστρεψε στην εφαρμογή. Δείτε την ενότητα OneDrive στις Ρυθμίσεις.")

# Πρόσβαση: Private app στο Streamlit Community Cloud (μόνο προσκεκλημένοι). Όλες οι οθόνες για όλους.
nav = st.navigation([
    st.Page(dashboard.page, title="Γραμματεία / Dashboard", icon="📊", url_path="dashboard", default=True),
    st.Page(scanner.page, title="Scanner / Αποθήκη", icon="📟", url_path="scanner"),
    st.Page(history.page, title="Ιστορικό", icon="🗂️", url_path="history"),
    st.Page(settings.page, title="Ρυθμίσεις", icon="⚙️", url_path="settings"),
])
try:
    nav.run()
except DatabaseConnectionError as exc:
    show_db_problem(exc)
