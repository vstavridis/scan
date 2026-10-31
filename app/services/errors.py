from __future__ import annotations


class ServiceError(Exception):
    """Σφάλμα με κατανοητό ελληνικό μήνυμα για τον χρήστη."""

    status_code = 400
    code = "error"

    def __init__(self, message: str, *, code: str | None = None, status_code: int | None = None, extra: dict | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code
        self.extra = extra or {}


class NotFound(ServiceError):
    status_code = 404
    code = "not_found"


class Conflict(ServiceError):
    status_code = 409
    code = "conflict"


class ConfirmationRequired(ServiceError):
    status_code = 409
    code = "confirmation_required"


class Locked(ServiceError):
    status_code = 423
    code = "excel_locked"
