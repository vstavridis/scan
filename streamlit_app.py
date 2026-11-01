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


@st.cache_resource(show_spinner="Σύνδεση με τη βάση δεδομένων…")
def _startup() -> bool:
    backend.startup()  # σχήμα βάσης + recovery, μία φορά ανά διεργασία
    return True


try:
    _startup()
except Exception as exc:  # pragma: no cover - εμφανίζεται στον χρήστη
    st.error(f"Δεν ήταν δυνατή η σύνδεση με τη βάση δεδομένων: {type(exc).__name__}: {exc}")
    st.stop()

from app.ui.common import has, inject_css, login_sidebar  # noqa: E402
from app.ui.components.auth_bridge import run_bridge  # noqa: E402
from app.ui.views import dashboard, history, scanner, settings  # noqa: E402

inject_css()
run_bridge()
login_sidebar()

scanner_page = st.Page(scanner.page, title="Scanner / Αποθήκη", icon="📟", url_path="scanner",
                       default=not has("dashboard"))
pages = []
home = scanner_page
if has("dashboard"):
    home = st.Page(dashboard.page, title="Γραμματεία / Dashboard", icon="📊", url_path="dashboard", default=True)
    pages.append(home)
if has("scan") or not has("view"):
    pages.append(scanner_page)  # και για μη συνδεδεμένους: η σελίδα ζητά σύνδεση
if has("dashboard"):
    pages.append(st.Page(history.page, title="Ιστορικό", icon="🗂️", url_path="history"))
if has("admin"):
    pages.append(st.Page(settings.page, title="Ρυθμίσεις", icon="⚙️", url_path="settings"))

nav = st.navigation(pages)
if st.session_state.pop("just_logged_in", False) and nav.url_path != home.url_path:
    st.switch_page(home)
nav.run()
