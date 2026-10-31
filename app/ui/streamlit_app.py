"""Είσοδος Streamlit: Γραμματεία, Ιστορικό, Ρυθμίσεις (και σύνδεσμος προς την οθόνη σάρωσης)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

from app.ui.api_client import ApiClient, ApiError  # noqa: E402
from app.ui.common import has, inject_css, login_sidebar  # noqa: E402
from app.ui.views import dashboard, history, scanner, settings  # noqa: E402

st.set_page_config(page_title="Αποθήκη Barcode", page_icon="📦", layout="wide")
inject_css()
login_sidebar()

if "dash_refresh" not in st.session_state and st.session_state.get("token"):
    try:
        cfg = ApiClient().get("/api/config", timeout=5)
        st.session_state["dash_refresh"] = float(cfg["dashboard_refresh_seconds"])
    except (ApiError, KeyError, ValueError):
        pass

scanner_page = st.Page(scanner.page, title="Scanner / Αποθήκη", icon="📟", url_path="scanner",
                       default=not has("dashboard"))
pages = [scanner_page]
home = scanner_page
if has("dashboard"):
    home = st.Page(dashboard.page, title="Γραμματεία / Dashboard", icon="📊", url_path="dashboard", default=True)
    pages = [home, scanner_page, st.Page(history.page, title="Ιστορικό", icon="🗂️", url_path="history")]
if has("admin"):
    pages.append(st.Page(settings.page, title="Ρυθμίσεις", icon="⚙️", url_path="settings"))

nav = st.navigation(pages)
if st.session_state.pop("just_logged_in", False) and nav.url_path != home.url_path:
    st.switch_page(home)
nav.run()
