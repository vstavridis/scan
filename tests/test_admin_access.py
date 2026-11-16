"""Πρόσβαση στις Ρυθμίσεις: λογαριασμός Streamlit + (προαιρετικά) λίστα `[admin] emails` στα Secrets."""
from __future__ import annotations

import pytest

from app.auth import PermissionDenied, authorize, can_manage, make_principal
from app.config import _email_list, reset_settings


@pytest.fixture
def admins(monkeypatch):
    def set_(value):
        if value is None:
            monkeypatch.delenv("APP_ADMIN_EMAILS", raising=False)
        else:
            monkeypatch.setenv("APP_ADMIN_EMAILS", value)
        reset_settings()
    yield set_
    monkeypatch.delenv("APP_ADMIN_EMAILS", raising=False)
    reset_settings()


def test_email_list_parsing():
    assert _email_list(["A@X.gr", " b@x.gr ", "not-an-email"]) == {"a@x.gr", "b@x.gr"}
    assert _email_list("a@x.gr, B@x.gr; c@x.gr") == {"a@x.gr", "b@x.gr", "c@x.gr"}
    assert _email_list(None) == frozenset()


def test_without_list_any_streamlit_account(admins):
    admins(None)
    assert can_manage("someone@x.gr")
    assert not can_manage(None) and not can_manage("test@example.com")


def test_with_list_only_listed_emails(admins):
    admins("boss@x.gr,Office@x.gr")
    assert can_manage("boss@x.gr") and can_manage("OFFICE@x.gr")
    assert not can_manage("worker@x.gr") and not can_manage(None)
    authorize(make_principal("Boss", "boss@x.gr"), "admin")
    with pytest.raises(PermissionDenied):
        authorize(make_principal("Worker", "worker@x.gr"), "admin")
    authorize(make_principal("Worker", "worker@x.gr"), "transfer")      # οι άλλες ενέργειες επιτρέπονται
    authorize(make_principal("Αποθήκη PC1"), "scan")
