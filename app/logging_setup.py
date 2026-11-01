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
    _configured = True
