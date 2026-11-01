"""Αποθήκευση αρχείων (Excel, backups) σε object storage.

* SupabaseObjectStore: private bucket του Supabase Storage μέσω REST API. Το service key
  χρησιμοποιείται ΜΟΝΟ από την Python (server-side) και δεν φτάνει ποτέ στον browser.
* LocalObjectStore: τοπικός φάκελος, μόνο για tests/τοπική ανάπτυξη.

Τα αντικείμενα των εκδόσεων Excel είναι **αμετάβλητα** (δεν γίνεται overwrite): κάθε μεταφορά
ανεβάζει νέα έκδοση και η βάση δείχνει ποια είναι η τρέχουσα. Έτσι κάθε προηγούμενη έκδοση
μένει διαθέσιμη ως backup.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import httpx

from app.config import ConfigError, get_settings

log = logging.getLogger(__name__)

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_SAFE_PATH = re.compile(r"^[A-Za-z0-9_.\-/]+$")


class StorageError(Exception):
    """Αποτυχία αποθήκευσης/ανάκτησης αρχείου."""


@dataclass
class StoredObject:
    path: str
    size: int
    updated_at: str


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _check_path(path: str) -> str:
    if not _SAFE_PATH.match(path) or ".." in path or path.startswith("/"):
        raise StorageError(f"Μη έγκυρη διαδρομή αρχείου: {path}")
    return path


class ObjectStore(ABC):
    @abstractmethod
    def put(self, path: str, data: bytes, content_type: str = XLSX_MIME) -> None:
        """Ανεβάζει νέο αντικείμενο. Αποτυγχάνει αν υπάρχει ήδη (χωρίς overwrite)."""

    @abstractmethod
    def get(self, path: str) -> bytes: ...

    @abstractmethod
    def list(self, prefix: str) -> list[StoredObject]: ...

    @abstractmethod
    def delete(self, path: str) -> None: ...

    def put_verified(self, path: str, data: bytes, content_type: str = XLSX_MIME) -> str:
        """Ανέβασμα + επαλήθευση με ξανακατέβασμα και σύγκριση sha256."""
        digest = sha256_bytes(data)
        self.put(path, data, content_type)
        back = self.get(path)
        if sha256_bytes(back) != digest:
            raise StorageError(f"Η επαλήθευση του αρχείου {path} απέτυχε (διαφορετικό περιεχόμενο).")
        return digest


class LocalObjectStore(ObjectStore):
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def _p(self, path: str) -> Path:
        return self.root / _check_path(path)

    def put(self, path: str, data: bytes, content_type: str = XLSX_MIME) -> None:
        p = self._p(path)
        if p.exists():
            raise StorageError(f"Το αρχείο {path} υπάρχει ήδη.")
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".part")
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)

    def get(self, path: str) -> bytes:
        p = self._p(path)
        if not p.is_file():
            raise StorageError(f"Δεν βρέθηκε το αρχείο {path}.")
        return p.read_bytes()

    def list(self, prefix: str) -> list[StoredObject]:
        base = self.root / prefix if prefix else self.root
        out = []
        if base.exists():
            for f in base.rglob("*"):
                if f.is_file() and not f.name.endswith(".part"):
                    st = f.stat()
                    out.append(StoredObject(str(f.relative_to(self.root)).replace(os.sep, "/"), st.st_size,
                                            time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(st.st_mtime))))
        return sorted(out, key=lambda o: o.path)

    def delete(self, path: str) -> None:
        p = self._p(path)
        if p.exists():
            p.unlink()


class SupabaseObjectStore(ObjectStore):
    """Supabase Storage REST API (https://supabase.com/docs/reference/api)."""

    def __init__(self, url: str, service_key: str, bucket: str, transport: httpx.BaseTransport | None = None):
        self.base = url.rstrip("/") + "/storage/v1"
        self.bucket = bucket
        self.headers = {"Authorization": f"Bearer {service_key}", "apikey": service_key}
        self._transport = transport

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0), headers=self.headers, transport=self._transport)

    def _obj_url(self, path: str) -> str:
        return f"{self.base}/object/{self.bucket}/{quote(_check_path(path))}"

    def _retry(self, fn, what: str):
        last: Exception | None = None
        for attempt in range(4):
            try:
                return fn()
            except httpx.TransportError as exc:  # δίκτυο: επανάληψη
                last = exc
                time.sleep(0.5 * 2 ** attempt)
        raise StorageError(f"Αποτυχία σύνδεσης με το Supabase Storage ({what}): {last}")

    def put(self, path: str, data: bytes, content_type: str = XLSX_MIME) -> None:
        def go():
            with self._client() as c:
                r = c.post(self._obj_url(path), content=data,
                           headers={"Content-Type": content_type, "x-upsert": "false", "cache-control": "no-cache"})
            if r.status_code == 409 or (r.status_code == 400 and "exists" in r.text.lower()):
                # Αν υπάρχει ήδη με ΙΔΙΟ περιεχόμενο (π.χ. επανάληψη μετά από χαμένη απάντηση), είναι επιτυχία.
                if sha256_bytes(self.get(path)) == sha256_bytes(data):
                    return
                raise StorageError(f"Το αρχείο {path} υπάρχει ήδη με διαφορετικό περιεχόμενο.")
            if r.status_code >= 400:
                raise StorageError(f"Αποτυχία αποθήκευσης {path}: HTTP {r.status_code} {r.text[:200]}")
        self._retry(go, f"upload {path}")

    def get(self, path: str) -> bytes:
        def go():
            with self._client() as c:
                r = c.get(self._obj_url(path))
            if r.status_code == 404 or (r.status_code == 400 and "not found" in r.text.lower()):
                raise StorageError(f"Δεν βρέθηκε το αρχείο {path}.")
            if r.status_code >= 400:
                raise StorageError(f"Αποτυχία ανάκτησης {path}: HTTP {r.status_code} {r.text[:200]}")
            return r.content
        return self._retry(go, f"download {path}")

    def list(self, prefix: str) -> list[StoredObject]:
        """Αναδρομική λίστα (το Storage API επιστρέφει "φακέλους" με id=null)."""
        out: list[StoredObject] = []
        stack = [prefix.strip("/")]
        while stack:
            folder = stack.pop()
            offset = 0
            while True:
                def go():
                    with self._client() as c:
                        r = c.post(f"{self.base}/object/list/{self.bucket}",
                                   json={"prefix": folder, "limit": 1000, "offset": offset,
                                         "sortBy": {"column": "name", "order": "asc"}})
                    if r.status_code >= 400:
                        raise StorageError(f"Αποτυχία λίστας αρχείων: HTTP {r.status_code} {r.text[:200]}")
                    return r.json()
                items = self._retry(go, "list")
                for it in items:
                    full = f"{folder}/{it['name']}" if folder else it["name"]
                    if it.get("id") is None:
                        stack.append(full)
                    else:
                        meta = it.get("metadata") or {}
                        out.append(StoredObject(full, int(meta.get("size") or 0), it.get("updated_at") or ""))
                if len(items) < 1000:
                    break
                offset += 1000
        return sorted(out, key=lambda o: o.path)

    def delete(self, path: str) -> None:
        def go():
            with self._client() as c:
                r = c.request("DELETE", f"{self.base}/object/{self.bucket}", json={"prefixes": [_check_path(path)]})
            if r.status_code >= 400:
                raise StorageError(f"Αποτυχία διαγραφής {path}: HTTP {r.status_code}")
        self._retry(go, "delete")

    def check_bucket(self) -> str | None:
        """Επιστρέφει μήνυμα προβλήματος ή None. Προειδοποιεί αν το bucket είναι δημόσιο."""
        try:
            with self._client() as c:
                r = c.get(f"{self.base}/bucket/{self.bucket}")
        except httpx.HTTPError as exc:
            return f"Δεν υπάρχει σύνδεση με το Supabase Storage: {exc}"
        if r.status_code >= 400:
            return f"Το bucket «{self.bucket}» δεν βρέθηκε ή το service key δεν είναι σωστό (HTTP {r.status_code})."
        if r.json().get("public"):
            return f"ΠΡΟΣΟΧΗ: το bucket «{self.bucket}» είναι ΔΗΜΟΣΙΟ. Ορίστε το ως private στο Supabase."
        return None


_store: ObjectStore | None = None


def get_store() -> ObjectStore:
    global _store
    if _store is None:
        s = get_settings()
        if s.supabase_url and s.supabase_service_key:
            _store = SupabaseObjectStore(s.supabase_url, s.supabase_service_key, s.supabase_bucket)
        elif s.dev_storage_dir:
            _store = LocalObjectStore(s.dev_storage_dir)
        else:
            raise ConfigError("Δεν έχει οριστεί αποθήκευση αρχείων ([supabase] url/service_key στα Secrets).")
    return _store


def set_store(store: ObjectStore | None) -> None:
    global _store
    _store = store


def cached_get(path: str, expected_sha: str | None = None) -> Path:
    """Κατεβάζει αντικείμενο σε τοπική προσωρινή cache (μόνο cache, όχι αποθήκευση).

    Τα αντικείμενα είναι αμετάβλητα, άρα μία έκδοση μπορεί να ξαναχρησιμοποιηθεί με ασφάλεια
    όσο ταιριάζει το sha256.
    """
    cache = get_settings().cache_dir
    cache.mkdir(parents=True, exist_ok=True)
    local = cache / (path.replace("/", "__"))
    if local.is_file() and expected_sha and sha256_bytes(local.read_bytes()) == expected_sha:
        return local
    data = get_store().get(path)
    if expected_sha and sha256_bytes(data) != expected_sha:
        raise StorageError(f"Το αρχείο {path} δεν ταιριάζει με την καταγεγραμμένη έκδοση (sha256).")
    tmp = local.with_name(local.name + f".{os.getpid()}.part")
    tmp.write_bytes(data)
    os.replace(tmp, local)
    return local
