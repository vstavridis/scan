"""Ταυτότητα χρήστη για το ιστορικό ενεργειών (audit).

Η πρόσβαση στην εφαρμογή ελέγχεται από το Streamlit Community Cloud: η εφαρμογή είναι **Private**
και την ανοίγουν μόνο προσκεκλημένοι χρήστες. Δεν υπάρχει εσωτερικό login, κωδικοί ή tokens·
όλοι οι προσκεκλημένοι έχουν πρόσβαση σε όλες τις οθόνες.

Το Principal χρησιμοποιείται μόνο για να καταγράφεται ΠΟΙΟΣ έκανε μια ενέργεια (π.χ. μεταφορά):
το email του προσκεκλημένου χρήστη (st.user.email στο Streamlit Cloud) ή το όνομα που δήλωσε.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.services.errors import ServiceError


@dataclass(frozen=True)
class Principal:
    name: str
    email: str | None = None

    @property
    def actor(self) -> str:
        if self.email and self.email not in self.name:
            return f"{self.name} <{self.email}>"
        return self.name


class PermissionDenied(ServiceError):
    status_code = 403
    code = "forbidden"


def authorize(principal: Principal | None, _action: str = "") -> Principal:
    """Κάθε ενέργεια του backend απαιτεί γνωστή ταυτότητα (για το audit). Δεν υπάρχουν ρόλοι."""
    if principal is None or not principal.name:
        raise PermissionDenied("Άγνωστος χρήστης.")
    return principal


def make_principal(name: str | None = None, email: str | None = None) -> Principal:
    name = (name or "").strip()[:60]
    email = (email or "").strip()[:120] or None
    return Principal(name=name or email or "Χρήστης", email=email)
