"""Κοινά στοιχεία του Streamlit UI: σύνδεση, ρόλοι, στυλ."""
from __future__ import annotations

import streamlit as st

from app import backend
from app.auth import Principal
from app.services.errors import ServiceError

CSS = """
<style>
:root {
  --wh-ink: #111827;          /* κύριο κείμενο */
  --wh-ink-2: #374151;        /* δευτερεύον κείμενο */
  --wh-muted: #4b5563;        /* labels (contrast ≥ 7:1 σε λευκό) */
  --wh-line: #d1d5db;         /* λεπτό γκρι border */
  --wh-card: #ffffff;
  --wh-accent: #15803d;
}
.block-container { padding-top: 1.6rem; max-width: 1500px; }

/* ---- Metric cards: λευκές, σκούρο κείμενο, ανεξάρτητα από το θέμα του λειτουργικού ---------- */
div[data-testid="stMetric"] {
  background: var(--wh-card) !important;
  border: 1px solid var(--wh-line);
  border-radius: 10px;
  padding: 14px 18px;
  box-shadow: 0 1px 2px rgba(17, 24, 39, .06), 0 1px 3px rgba(17, 24, 39, .08);
  min-height: 100%;
}
div[data-testid="stMetric"] [data-testid="stMetricLabel"],
div[data-testid="stMetric"] [data-testid="stMetricLabel"] * {
  color: var(--wh-muted) !important;
  font-weight: 600;
  font-size: 0.92rem;
  opacity: 1 !important;
}
div[data-testid="stMetric"] [data-testid="stMetricValue"],
div[data-testid="stMetric"] [data-testid="stMetricValue"] * {
  color: var(--wh-ink) !important;
  font-weight: 800;
  font-variant-numeric: tabular-nums;
}
div[data-testid="stMetric"] [data-testid="stMetricDelta"],
div[data-testid="stMetric"] [data-testid="stMetricDelta"] * {
  color: var(--wh-ink-2) !important;
}
div[data-testid="stMetric"] [data-testid="stMetricDelta"] svg { fill: var(--wh-ink-2) !important; }
div[data-testid="stMetric"] [data-testid="stTooltipIcon"] svg { color: var(--wh-muted) !important; }

/* ---- Tabs: ευανάγνωστες καρτέλες με σαφή ένδειξη επιλογής ------------------------------- */
/* Streamlit ≥1.5x: React Aria (data-testid="stTab"). Παλαιότερες εκδόσεις: BaseWeb (data-baseweb="tab"). */
div[data-testid="stTabs"] [role="tablist"] {
  gap: 4px;
  border-bottom: 1px solid var(--wh-line);
}
div[data-testid="stTabs"] [data-testid="stTab"],
div[data-testid="stTabs"] button[data-baseweb="tab"] {
  background: #f3f4f6 !important;
  color: var(--wh-ink-2) !important;
  border: 1px solid var(--wh-line);
  border-bottom: none;
  border-radius: 8px 8px 0 0;
  padding: 8px 16px;
}
div[data-testid="stTabs"] [data-testid="stTab"] p,
div[data-testid="stTabs"] button[data-baseweb="tab"] p {
  color: var(--wh-ink-2) !important;
  font-weight: 600;
  font-size: 0.98rem;
}
div[data-testid="stTabs"] [data-testid="stTab"][aria-selected="true"],
div[data-testid="stTabs"] button[data-baseweb="tab"][aria-selected="true"] {
  background: var(--wh-card) !important;
  color: var(--wh-accent) !important;
}
div[data-testid="stTabs"] [data-testid="stTab"][aria-selected="true"] p,
div[data-testid="stTabs"] button[data-baseweb="tab"][aria-selected="true"] p { color: var(--wh-accent) !important; }
div[data-testid="stTabs"] .react-aria-SelectionIndicator,
div[data-testid="stTabs"] [data-baseweb="tab-highlight"] { background-color: var(--wh-accent) !important; }

/* ---- Πίνακες / επικεφαλίδες στηλών ------------------------------------------------------- */
div[data-testid="stDataFrame"] { border: 1px solid var(--wh-line); border-radius: 8px; }
.hdr { font-weight: 700; font-size: 0.9rem; color: var(--wh-ink-2) !important; background: #f3f4f6;
       border-bottom: 2px solid var(--wh-line); padding: 6px 4px 4px; border-radius: 6px 6px 0 0;
       white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-height: 2.1rem; }
.row-cell { padding-top: 0.45rem; font-size: 0.98rem; color: var(--wh-ink); }

/* ---- Λοιπά ------------------------------------------------------------------------------- */
.badge { display:inline-block; padding:2px 10px; border-radius:999px; font-weight:700; font-size:0.85rem; }
.b-ok { background:#dcfce7; color:#14532d; } .b-ambiguous, .b-unknown { background:#fee2e2; color:#7f1d1d; }
.b-dup { background:#fef9c3; color:#713f12; }
.mono { font-family: Consolas, "Roboto Mono", monospace; font-weight: 700; }
.sync-card { background: var(--wh-card); border: 1px solid var(--wh-line); border-radius: 10px;
             padding: 12px 16px; min-height: 5.2rem; }
.sync-label { font-size: 0.85rem; font-weight: 600; color: var(--wh-ink-2); }
.sync-value { font-size: 1.25rem; font-weight: 800; color: var(--wh-ink); margin-top: 6px; }
.total-line { font-size: 1.35rem; font-weight: 800; margin-top: .4rem; color: var(--wh-ink); }
/* Χωρίς "θάμπωμα" κατά τις περιοδικές ανανεώσεις (st.fragment run_every) */
[data-stale="true"] { opacity: 1 !important; transition: none !important; }
/* Χωρίς το «Running… / Stop» πάνω δεξιά σε κάθε περιοδική ανανέωση (Scanner/Dashboard/Ιστορικό).
   Οι πραγματικές ενέργειες δείχνουν δικά τους μηνύματα/spinners. */
[data-testid="stStatusWidget"] { visibility: hidden !important; }
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
    """Ποιος χρησιμοποιεί την εφαρμογή (για το ιστορικό ενεργειών). Χωρίς κωδικούς και χωρίς πεδία.

    Ταυτότητα: το email του προσκεκλημένου χρήστη (Streamlit Cloud). Προαιρετικά, όνομα συσκευής
    μέσω της διεύθυνσης (σελιδοδείκτης), π.χ. .../scanner?user=Αποθήκη%20PC1.
    """
    if "wh_user_name" not in st.session_state:
        st.session_state["wh_user_name"] = (st.query_params.get("user") or "").strip()[:60]
    name = st.session_state["wh_user_name"]
    if name and st.query_params.get("user") != name:
        st.query_params["user"] = name   # η πλοήγηση στο μενού αφαιρεί τις παραμέτρους του URL
    email = _cloud_email()
    if email:
        with st.sidebar:
            st.caption(f"Συνδεδεμένος: **{email}**")
