"""Απλή αλλά πραγματική προστασία endpoints με ρόλους.

Token = base64url(JSON payload) + "." + HMAC-SHA256 υπογραφή με APP_SECRET_KEY.
Οι κωδικοί ρόλων ορίζονται σε environment variables (εκτός GitHub).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Query

from app.config import get_settings

ROLES = ("scanner", "secretary", "admin")
ROLE_LABELS = {"scanner": "Αποθήκη (Scanner)", "secretary": "Γραμματεία", "admin": "Διαχειριστής"}

# Ποιοι ρόλοι επιτρέπονται ανά δικαίωμα (ο admin έχει όλα).
PERMISSIONS = {
    "scan": {"scanner", "admin"},
    "view": {"scanner", "secretary", "admin"},
    "dashboard": {"secretary", "admin"},
    "transfer": {"secretary", "admin"},
    "admin": {"admin"},
}


@dataclass
class Principal:
    role: str
    name: str

    @property
    def actor(self) -> str:
        return f"{self.name} ({ROLE_LABELS.get(self.role, self.role)})"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(payload: str) -> str:
    key = get_settings().secret_key.encode("utf-8")
    return _b64(hmac.new(key, payload.encode("ascii"), hashlib.sha256).digest())


def issue_token(role: str, name: str) -> tuple[str, int]:
    s = get_settings()
    hours = s.scanner_token_hours if role == "scanner" else s.user_token_hours
    exp = int(time.time()) + hours * 3600
    payload = _b64(json.dumps({"r": role, "n": name, "e": exp}, ensure_ascii=False).encode("utf-8"))
    return f"{payload}.{_sign(payload)}", exp


def verify_token(token: str) -> Principal | None:
    try:
        payload, sig = token.split(".", 1)
    except ValueError:
        return None
    if not hmac.compare_digest(sig, _sign(payload)):
        return None
    try:
        data = json.loads(_unb64(payload))
    except (ValueError, json.JSONDecodeError):
        return None
    if data.get("e", 0) < time.time() or data.get("r") not in ROLES:
        return None
    return Principal(role=data["r"], name=str(data.get("n") or data["r"]))


def check_password(role: str, password: str) -> bool:
    expected = get_settings().passwords.get(role)
    if not expected:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), password.encode("utf-8"))


def _principal(authorization: str | None = Header(default=None), token: str | None = Query(default=None)) -> Principal:
    raw = None
    if authorization and authorization.lower().startswith("bearer "):
        raw = authorization[7:].strip()
    elif token:
        raw = token
    if not raw:
        raise HTTPException(status_code=401, detail="Απαιτείται σύνδεση.")
    p = verify_token(raw)
    if p is None:
        raise HTTPException(status_code=401, detail="Η σύνδεση έληξε ή δεν είναι έγκυρη. Συνδεθείτε ξανά.")
    return p


def require(permission: str):
    allowed = PERMISSIONS[permission]

    def dep(p: Principal = Depends(_principal)) -> Principal:
        if p.role not in allowed:
            raise HTTPException(status_code=403, detail="Δεν έχετε δικαίωμα για αυτή την ενέργεια.")
        return p

    return dep
