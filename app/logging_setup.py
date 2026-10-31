from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from app.config import get_settings

_configured = False


def setup_logging(name: str = "app") -> None:
    global _configured
    if _configured:
        return
    s = get_settings()
    root = logging.getLogger()
    root.setLevel(s.log_level.upper())
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    fh = RotatingFileHandler(s.logs_dir / f"{name}.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler) for h in root.handlers):
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)
    _configured = True
