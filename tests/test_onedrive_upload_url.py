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


@pytest.mark.parametrize("upload_url", [
    "https://sn3302.up.1drv.com/up/fake-token",
    "https://my.microsoftpersonalcontent.com/personal/fake/upload?token=fake",
])
def test_upload_accepts_official_personal_onedrive_host(monkeypatch, upload_url):
    calls = []
    def mock_graph(*args, **kw):
        calls.append((args, kw))
        return FakeGraphResponse(upload_url)
    monkeypatch.setattr(onedrive, "_graph", mock_graph)
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
            assert url == upload_url
            assert content == b"abc"
            assert headers["Content-Range"] == "bytes 0-2/3"
            return Response()
    monkeypatch.setattr(onedrive.httpx, "Client", Client)
    onedrive._upload("drive", "item", "etag", b"abc", "access-token")
    assert len(calls) == 1
    assert calls[0][1] == {"headers": {"If-Match": "etag"}}
    assert calls[0][0][2] == "/drives/drive/items/item/createUploadSession"



@pytest.mark.parametrize("code,expected", [
    (409, "HTTP 409"),
    (412, "HTTP 412"),
])
def test_graph_conflicts_keep_specific_http_status_and_never_leak_raw_message(monkeypatch, code, expected):
    class FakeResponse:
        status_code = code
        def json(self):
            return {"error": {"code": "nameAlreadyExists",
                              "message": "do not expose temporary-URL-secret"}}
    class FakeClient:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def request(self, *a, **kw): return FakeResponse()
    monkeypatch.setattr(onedrive.httpx, "Client", FakeClient)
    from app.services.errors import Conflict
    with pytest.raises(Conflict) as exc:
        onedrive._graph("bearer-secret", "POST", "/drives/foo/items/bar/createUploadSession")
    assert expected in exc.value.message
    assert "nameAlreadyExists" in exc.value.message
    assert "temporary-URL-secret" not in exc.value.message
    assert "bearer-secret" not in exc.value.message
