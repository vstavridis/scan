"""Supabase Storage client: έλεγχος αιτημάτων REST με ψεύτικο server (χωρίς πραγματικό Supabase).

Επαληθεύει: private αιτήματα με service key (μόνο server-side), ανέβασμα χωρίς overwrite,
επαλήθευση sha256 μετά το ανέβασμα, αναδρομική λίστα, και όλη τη ροή import -> scan -> μεταφορά.
"""
from __future__ import annotations

import json
from urllib.parse import unquote

import httpx
import pytest

from app import backend
from app.storage import StorageError, SupabaseObjectStore, set_store
from tests.conftest import activate_workbook, login, scan, working_file

KEY = "service-role-test-key"


class FakeSupabase:
    def __init__(self, public: bool = False):
        self.objects: dict[str, bytes] = {}
        self.public = public
        self.requests: list[httpx.Request] = []
        self.corrupt_next_get = False

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        assert req.headers["authorization"] == f"Bearer {KEY}" and req.headers["apikey"] == KEY
        path = unquote(req.url.path)
        pre = "/storage/v1/object/"
        if req.method == "GET" and path == "/storage/v1/bucket/wh":
            return httpx.Response(200, json={"id": "wh", "name": "wh", "public": self.public})
        if req.method == "POST" and path == "/storage/v1/object/list/wh":
            body = json.loads(req.content)
            prefix = body["prefix"].strip("/")
            seen, out = set(), []
            for k, v in sorted(self.objects.items()):
                if prefix and not k.startswith(prefix + "/"):
                    continue
                rest = k[len(prefix) + 1:] if prefix else k
                head, sep, _ = rest.partition("/")
                if sep:
                    if head not in seen:
                        seen.add(head)
                        out.append({"name": head, "id": None})
                else:
                    out.append({"name": rest, "id": "x", "updated_at": "2026-11-01T10:00:00Z",
                                "metadata": {"size": len(v)}})
            return httpx.Response(200, json=out[body["offset"]: body["offset"] + body["limit"]])
        if path.startswith(pre + "wh/"):
            key = path[len(pre + "wh/"):]
            if req.method == "POST":
                if key in self.objects and req.headers.get("x-upsert") != "true":
                    return httpx.Response(400, json={"statusCode": "409", "error": "Duplicate",
                                                     "message": "The resource already exists"})
                self.objects[key] = req.content
                return httpx.Response(200, json={"Key": f"wh/{key}"})
            if req.method == "GET":
                if key not in self.objects:
                    return httpx.Response(400, json={"statusCode": "404", "error": "not_found",
                                                     "message": "Object not found"})
                data = self.objects[key]
                if self.corrupt_next_get:
                    self.corrupt_next_get = False
                    data = data[:-1] + b"!"
                return httpx.Response(200, content=data)
        if req.method == "DELETE" and path == "/storage/v1/object/wh":
            for k in json.loads(req.content)["prefixes"]:
                self.objects.pop(k, None)
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"message": f"unexpected {req.method} {path}"})


@pytest.fixture
def fake():
    f = FakeSupabase()
    store = SupabaseObjectStore("https://proj.supabase.co", KEY, "wh", transport=httpx.MockTransport(f.handler))
    yield f, store


def test_put_get_list_and_no_overwrite(fake):
    f, store = fake
    digest = store.put_verified("imports/00001/original.xlsx", b"abc")
    assert f.objects["imports/00001/original.xlsx"] == b"abc"
    post = next(r for r in f.requests if r.method == "POST")
    assert post.headers["x-upsert"] == "false"            # ποτέ overwrite
    store.put("imports/00001/original.xlsx", b"abc")       # ίδιο περιεχόμενο (retry): αποδεκτό
    with pytest.raises(StorageError):
        store.put("imports/00001/original.xlsx", b"different")
    store.put("imports/00001/working/v00001.xlsx", b"w1")
    store.put("backups/b.xlsx", b"b")
    assert [o.path for o in store.list("imports")] == ["imports/00001/original.xlsx", "imports/00001/working/v00001.xlsx"]
    assert store.get("backups/b.xlsx") == b"b"
    with pytest.raises(StorageError):
        store.get("missing.xlsx")
    with pytest.raises(StorageError):
        store.get("../etc/passwd")
    assert len(digest) == 64
    assert store.check_bucket() is None


def test_public_bucket_is_flagged():
    f = FakeSupabase(public=True)
    store = SupabaseObjectStore("https://p.supabase.co", KEY, "wh", transport=httpx.MockTransport(f.handler))
    assert "ΔΗΜΟΣΙΟ" in store.check_bucket()


def test_verification_detects_corruption(fake):
    f, store = fake
    f.corrupt_next_get = True
    with pytest.raises(StorageError):
        store.put_verified("x/y.xlsx", b"content")


def test_full_flow_through_supabase_client(app_env, synthetic_master, fake, tmp_path):
    f, store = fake
    set_store(store)
    activate_workbook(synthetic_master)
    scan(login("scanner"), "VS11486")
    sec = login("secretary")
    item = backend.dashboard(sec)["items"][0]
    res = backend.transfer_item(sec, item["id"])
    assert res["new_version"] == 2
    keys = sorted(f.objects)
    assert "imports/00001/original.xlsx" in keys and "imports/00001/working/v00001.xlsx" in keys
    assert any(k.startswith("imports/00001/working/v00002_op") for k in keys)
    assert not any(k.startswith("staging/") for k in keys)   # το staging καθαρίστηκε
    assert working_file(tmp_path).read_bytes() == f.objects[[k for k in keys if "v00002" in k][0]]
