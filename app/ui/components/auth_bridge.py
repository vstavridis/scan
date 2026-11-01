"""«Θυμήσου αυτόν τον υπολογιστή»: κρατά στο localStorage του browser ΜΟΝΟ ένα υπογεγραμμένο
token ρόλου (όχι κωδικούς, όχι κλειδιά βάσης), ώστε μια ανανέωση σελίδας να μη ζητά ξανά σύνδεση.
Το token επαληθεύεται στην Python (HMAC με το [auth] secret_key) και λήγει.
"""
from __future__ import annotations

import uuid

import streamlit as st

from app import backend

_JS = """
export default function (component) {
  const { data, setTriggerValue } = component;
  const K = 'wh.auth';
  if (!data) return;
  try {
    if (data.store) localStorage.setItem(K, data.store);
    if (data.clear) localStorage.removeItem(K);
  } catch (e) { /* storage μη διαθέσιμο */ }
  if (data.restore && window.__whAuthNonce !== data.restore) {
    window.__whAuthNonce = data.restore;
    let t = null;
    try { t = localStorage.getItem(K); } catch (e) { /* ignore */ }
    setTriggerValue(t ? 'token' : 'none', t || data.restore);
  }
}
"""

_bridge = st.components.v2.component("warehouse_auth_bridge", js=_JS, isolate_styles=True)
KEY = "wh_auth_bridge"


def _on_token() -> None:
    state = st.session_state.get(KEY)
    token = None
    try:
        token = state["token"] if state is not None else None
    except (KeyError, TypeError):
        token = getattr(state, "token", None)
    p = backend.restore(token if isinstance(token, str) else None)
    st.session_state["wh_restore_done"] = True
    if p is not None:
        st.session_state["principal"] = p
        st.session_state["just_logged_in"] = True


def _on_none() -> None:
    st.session_state["wh_restore_done"] = True


def run_bridge() -> None:
    """Να καλείται σε κάθε rerun, πριν από την πλοήγηση."""
    data: dict = {}
    if "principal" not in st.session_state and not st.session_state.get("wh_restore_done"):
        st.session_state.setdefault("wh_restore_nonce", uuid.uuid4().hex)
        data["restore"] = st.session_state["wh_restore_nonce"]
    store = st.session_state.pop("wh_store_token", None)
    if store:
        data["store"] = store
    if st.session_state.pop("wh_clear_token", False):
        data["clear"] = True
    _bridge(key=KEY, data=data, on_token_change=_on_token, on_none_change=_on_none, height="content")
