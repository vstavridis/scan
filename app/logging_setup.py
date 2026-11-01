from __future__ import annotations

import logging

from app.config import get_settings

_configured = False


def setup_logging() -> None:
    """Logs στο stdout (το Streamlit Cloud τα εμφανίζει στο "Manage app" → Logs)."""
    global _configured
    if _configured:
        return
    root = logging.getLogger()
    root.setLevel(get_settings().log_level.upper())
    if not any(getattr(h, "_warehouse", False) for h in root.handlers):
        h = logging.StreamHandler()
        h._warehouse = True  # type: ignore[attr-defined]
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
        root.addHandler(h)
    # httpx INFO includes full redirected OneDrive URLs containing temporary
    # 'tempauth' credentials. Suppress request-level URL logging in Cloud logs.
    # We still surface sanitized HTTP status/error-code diagnostics in the UI.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    _configured = True
