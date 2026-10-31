"""Thin client του Streamlit προς το FastAPI backend.

Όλες οι αλλαγές δεδομένων (μεταφορές, εισαγωγές, ρυθμίσεις) περνούν από το API,
ώστε να ισχύει παντού ο ίδιος έλεγχος δικαιωμάτων και ένας μόνο writer για το Excel.
"""
from __future__ import annotations

from typing import Any

import httpx

from app.config import get_settings


class ApiError(Exception):
    def __init__(self, message: str, status: int = 0, code: str = "", extra: dict | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.extra = extra or {}


class ApiClient:
    def __init__(self, token: str | None = None, base_url: str | None = None):
        self.base_url = base_url or get_settings().api_internal_url
        self.token = token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def request(self, method: str, path: str, *, timeout: float = 30.0, raw: bool = False, **kwargs: Any) -> Any:
        try:
            with httpx.Client(base_url=self.base_url, timeout=timeout) as c:
                resp = c.request(method, path, headers=self._headers(), **kwargs)
        except httpx.HTTPError as exc:
            raise ApiError(f"Δεν υπάρχει σύνδεση με το backend ({self.base_url}). Βεβαιωθείτε ότι τρέχει το run.py.") from exc
        if resp.status_code >= 400:
            try:
                data = resp.json()
            except ValueError:
                data = {}
            detail = data.get("detail") if isinstance(data, dict) else None
            if isinstance(detail, list):  # σφάλματα validation του FastAPI
                detail = "; ".join(str(d.get("msg")) for d in detail)
            raise ApiError(detail or f"Σφάλμα {resp.status_code}", resp.status_code,
                           data.get("code", "") if isinstance(data, dict) else "",
                           data.get("extra") if isinstance(data, dict) else None)
        if raw:
            return resp.content
        return resp.json()

    def get(self, path: str, **kw: Any) -> Any:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw: Any) -> Any:
        return self.request("POST", path, **kw)

    def put(self, path: str, **kw: Any) -> Any:
        return self.request("PUT", path, **kw)
