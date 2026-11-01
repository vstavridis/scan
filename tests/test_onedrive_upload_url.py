"""OneDrive upload URL validation regression checks.

The upload URL is a preauthorized credential: never leak its path/query to the UI.
"""
from __future__ import annotations

import pytest

from app.services import onedrive
from app.services.errors import ServiceError


class FakeGraphResponse:
    def __init__(self, url):
        self.url = url

    def json(self):
        return {"uploadUrl": self.url}


@pytest.mark.parametrize("invalid", [
    None,
    "",
    "https://not-microsoft.example/up/secret-value?token=SUPERSECRET",
    "http://sn3302.up.1drv.com/up/secret-value",
    "https://sn3302.up.1drv.com:444/up/secret-value",
    "https://user:password@sn3302.up.1drv.com/up/secret-value",
])
def test_upload_rejects_unknown_or_malformed_url_without_leaking_secret(monkeypatch, invalid):
    monkeypatch.setattr(onedrive, "_graph", lambda *a, **kw: FakeGraphResponse(invalid))
    with pytest.raises(ServiceError) as exc:
        onedrive._upload("drive", "item", "etag", b"abc", "access-token")
    message = str(exc.value)
    assert "SUPERSECRET" not in message
    assert "secret-value" not in message
    assert "access-token" not in message
    assert "password" not in message


def test_upload_accepts_official_personal_onedrive_host(monkeypatch):
    monkeypatch.setattr(onedrive, "_graph",
                        lambda *a, **kw: FakeGraphResponse("https://sn3302.up.1drv.com/up/fake-token"))
    class Response:
        status_code = 201
    class Client:
        def __init__(self, *a, **kw):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def put(self, url, content, headers):
            assert url == "https://sn3302.up.1drv.com/up/fake-token"
            assert content == b"abc"
            assert headers["Content-Range"] == "bytes 0-2/3"
            return Response()
    monkeypatch.setattr(onedrive.httpx, "Client", Client)
    onedrive._upload("drive", "item", "etag", b"abc", "access-token")
