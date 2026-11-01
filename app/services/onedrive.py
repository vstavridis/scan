"""OneDrive Personal via Microsoft Graph delegated OAuth; optional source-of-truth Excel.

The streamlit app stays usable without this integration. Microsoft tokens are encrypted
at rest in PostgreSQL, never sent to the scanner/browser or added to GitHub.
Each transfer is durable in Supabase first; a failed OneDrive sync is explicit/pending.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from urllib.parse import urlencode, urlparse

import httpx

from app.config import get_settings
from app.db.database import connect, transaction
from app.services.errors import Conflict, ServiceError
from app.services.locks import lease
from app.services import catalog
from app.storage import sha256_bytes, cached_get

log = logging.getLogger(__name__)
AUTH_BASE = "https://login.microsoftonline.com/consumers/oauth2/v2.0"
GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = "Files.ReadWrite User.Read offline_access"
MAX_BYTES = 50 * 1024 * 1024
CHUNK = 5 * 327680
AUTH_KEY = "onedrive.oauth"
BOUND_KEY = "onedrive.bound"


def configured() -> bool:
    s = get_settings()
    return bool(s.onedrive_client_id and s.onedrive_client_secret and s.onedrive_redirect_uri)


def _cfg():
    s = get_settings()
    if not configured():
        raise ServiceError("Συμπληρώστε client_id, client_secret και redirect_uri στα Streamlit Secrets [onedrive].")
    return s


def _cipher():
    from cryptography.fernet import Fernet
    key = hashlib.sha256(("warehouse-onedrive-token-v1:" + _cfg().onedrive_client_secret).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def _get(key: str) -> str | None:
    with connect() as db:
        row = db.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None


def _put(key: str, value: str) -> None:
    with transaction() as db:
        db.execute("INSERT INTO app_settings(key,value) VALUES (?,?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def _state() -> str:
    payload = str(int(time.time())) + "." + secrets.token_urlsafe(24)
    sig = hmac.new(_cfg().onedrive_client_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return payload + "." + sig


def _verify_state(state: str) -> None:
    try:
        stamp, nonce, provided = state.split(".")
        body = stamp + "." + nonce
        expected = hmac.new(_cfg().onedrive_client_secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        valid = hmac.compare_digest(provided, expected) and 0 <= time.time() - int(stamp) <= 600
    except (ValueError, AttributeError):
        valid = False
    if not valid:
        raise ServiceError("Η σύνδεση Microsoft έληξε ή δεν έχει έγκυρο state. Ξεκινήστε ξανά από τις Ρυθμίσεις.")


def authorization_url() -> str:
    s = _cfg()
    return AUTH_BASE + "/authorize?" + urlencode({
        "client_id": s.onedrive_client_id,
        "response_type": "code",
        "redirect_uri": s.onedrive_redirect_uri,
        "scope": SCOPES,
        "state": _state(),
        "response_mode": "query",
    })


def _token_request(data: dict) -> dict:
    s = _cfg()
    body = {"client_id": s.onedrive_client_id, "client_secret": s.onedrive_client_secret,
            "scope": SCOPES, **data}
    try:
        r = httpx.post(AUTH_BASE + "/token", data=body, timeout=30)
        r.raise_for_status()
        out = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        # OAuth errors might include IDs; never surface a secret/token or the raw response.
        log.warning("OneDrive token exchange failure: %s", type(exc).__name__)
        raise ServiceError("Η Microsoft δεν έδωσε πρόσβαση. Ελέγξτε Redirect URI, Client Secret και άδειες Files.ReadWrite.") from None
    if not out.get("access_token"):
        raise ServiceError("Η Microsoft δεν επέστρεψε access token.")
    return out


def _graph(token: str, method: str, path: str, **kwargs) -> httpx.Response:
    try:
        with httpx.Client(timeout=60, follow_redirects=True) as client:
            r = client.request(method, GRAPH + path, headers={"Authorization": "Bearer " + token,
                                                                **kwargs.pop("headers", {})}, **kwargs)
        if r.status_code in (409, 412):
            raise Conflict("Το online Excel άλλαξε στο OneDrive. Δεν θα αντικαταστήσουμε νεότερο αρχείο.")
        if r.status_code == 401:
            raise ServiceError("Η εξουσιοδότηση OneDrive έληξε. Συνδεθείτε ξανά από τις Ρυθμίσεις.")
        if r.status_code == 403:
            raise ServiceError("Η Microsoft αρνήθηκε πρόσβαση στο Excel. Ελέγξτε τα Files.ReadWrite και ότι το αρχείο είναι δικό σας.")
        if r.status_code == 404:
            raise ServiceError("Το αρχείο OneDrive δεν βρέθηκε. Ελέγξτε τον σύνδεσμο.")
        r.raise_for_status()
        return r
    except (Conflict, ServiceError):
        raise
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("OneDrive Graph request failed: %s", type(exc).__name__)
        raise ServiceError("Δεν ήταν δυνατή η επικοινωνία με το Microsoft OneDrive. Δοκιμάστε ξανά.") from None


def finish_authorization(code: str, state: str) -> str:
    _verify_state(state)
    result = _token_request({"grant_type": "authorization_code", "code": code,
                             "redirect_uri": _cfg().onedrive_redirect_uri})
    refresh = result.get("refresh_token")
    if not refresh:
        raise ServiceError("Δεν δόθηκε refresh token. Βεβαιωθείτε ότι ζητήθηκε το offline_access.")
    profile = _graph(result["access_token"], "GET", "/me").json()
    account = profile.get("userPrincipalName") or profile.get("mail") or profile.get("displayName") or "Microsoft"
    record = json.dumps({"refresh": _cipher().encrypt(refresh.encode()).decode(), "account": account})
    _put(AUTH_KEY, record)
    return account


def connected_account() -> str | None:
    raw = _get(AUTH_KEY) if configured() else None
    try:
        return json.loads(raw)["account"] if raw else None
    except (ValueError, KeyError):
        return None


def _access_token() -> str:
    # Serialize refresh-token rotations across Streamlit sessions/processes.
    with transaction(lock="onedrive_auth") as db:
        row = db.execute("SELECT value FROM app_settings WHERE key=?", (AUTH_KEY,)).fetchone()
        if not row:
            raise ServiceError("Το OneDrive δεν είναι συνδεδεμένο. Πατήστε «Σύνδεση Microsoft» στις Ρυθμίσεις.")
        saved = json.loads(row["value"])
        try:
            refresh = _cipher().decrypt(saved["refresh"].encode()).decode()
        except Exception:
            raise ServiceError("Το αποθηκευμένο Microsoft token δεν διαβάζεται. Συνδεθείτε ξανά.") from None
        result = _token_request({"grant_type": "refresh_token", "refresh_token": refresh})
        if result.get("refresh_token"):
            saved["refresh"] = _cipher().encrypt(result["refresh_token"].encode()).decode()
            db.execute("UPDATE app_settings SET value=? WHERE key=?",
                       (json.dumps(saved), AUTH_KEY))
        return result["access_token"]


def _encoded_share(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in {
        "1drv.ms", "onedrive.live.com", "www.onedrive.com", "onedrive.com"
    }:
        raise ServiceError("Βάλτε τον HTTPS σύνδεσμο κοινής χρήσης OneDrive (π.χ. https://1drv.ms/...).")
    return "u!" + base64.urlsafe_b64encode(url.strip().encode()).decode().rstrip("=")


def file_metadata(drive_id: str, item_id: str, token: str | None = None) -> dict:
    token = token or _access_token()
    return _graph(token, "GET", f"/drives/{drive_id}/items/{item_id}").json()


def _download(drive: str, item: str, token: str) -> bytes:
    resp = _graph(token, "GET", f"/drives/{drive}/items/{item}/content")
    if len(resp.content) > MAX_BYTES:
        raise ServiceError("Το Excel υπερβαίνει το όριο των 50 MB.")
    return resp.content


def _stable_download(drive: str, item: str, token: str) -> tuple[bytes, dict]:
    before = file_metadata(drive, item, token)
    data = _download(drive, item, token)
    after = file_metadata(drive, item, token)
    if not before.get("eTag") or before["eTag"] != after.get("eTag"):
        raise Conflict("Το Excel άλλαξε κατά τη λήψη του από το OneDrive. Δοκιμάστε ξανά.")
    return data, after


def stage_from_link(url: str, actor: str) -> dict:
    if not connected_account():
        raise ServiceError("Συνδεθείτε πρώτα στον Microsoft λογαριασμό σας.")
    token = _access_token()
    shared = _graph(token, "GET", "/shares/" + _encoded_share(url) + "/driveItem",
                    headers={"Prefer": "redeemSharingLink"}).json()
    drive = (shared.get("parentReference") or {}).get("driveId")
    item = shared.get("id")
    name = shared.get("name", "")
    if not drive or not item or not name.lower().endswith(".xlsx"):
        raise ServiceError("Ο σύνδεσμος δεν αντιστοιχεί σε υποστηριζόμενο αρχείο Excel .xlsx.")
    data, meta = _stable_download(drive, item, token)
    staged = catalog.stage_upload(meta.get("name") or name, data, actor)
    return {"staged": staged, "drive_id": drive, "item_id": item, "etag": meta["eTag"],
            "link": url, "sha256": sha256_bytes(data), "name": meta.get("name") or name}


def binding() -> dict | None:
    raw = _get(BOUND_KEY)
    return json.loads(raw) if raw else None


def bound_import(import_id: int) -> dict | None:
    b = binding()
    return b if b and int(b["import_id"]) == int(import_id) else None


def status() -> dict:
    b = binding()
    imp = catalog.active_import()
    active = bool(b and imp and int(b["import_id"]) == int(imp["id"]))
    pending = active and int(b["synced_version"]) < int(imp["working_version"])
    return {"connected": connected_account(), "bound": active, "pending": pending,
            "name": b.get("name") if active else None, "link": b.get("link") if active else None}


def activate_staged(stage: dict, actor: str, confirm_replace: bool = False) -> dict:
    """Commit catalog and OneDrive binding atomically, with recovery for older partial activations."""
    current = status()
    if current["pending"]:
        raise Conflict("Υπάρχει εκκρεμής μεταφορά στο OneDrive. Κάντε πρώτα συγχρονισμό.")
    token = _access_token()
    meta = file_metadata(stage["drive_id"], stage["item_id"], token)
    if meta.get("eTag") != stage["etag"]:
        raise Conflict("Το online Excel άλλαξε μετά τον έλεγχο. Πατήστε ξανά «Έλεγχος Excel από OneDrive».")

    staged = stage["staged"]
    stage_id = staged["staging_id"]
    with connect() as conn:
        exists = conn.execute("SELECT id FROM staged_uploads WHERE id=?", (stage_id,)).fetchone()

    if not exists:
        # Recovery for old releases: catalog.activate() may already have committed and
        # deleted staging before OneDrive binding was saved. Never create a new import
        # or bind a different file without verifying SHA-256 against both copies.
        active = catalog.active_import()
        b = binding()
        if (active and int(active["working_version"]) == 1
                and active["working_sha256"] == stage["sha256"]
                and active["original_sha256"] == stage["sha256"]
                and active["original_filename"] == stage["name"]
                and not (b and int(b["import_id"]) == int(active["id"]))):
            remote, latest = _stable_download(stage["drive_id"], stage["item_id"], token)
            if latest.get("eTag") != stage["etag"] or sha256_bytes(remote) != stage["sha256"]:
                raise Conflict("Το OneDrive έχει αλλάξει. Ξανακάντε έλεγχο αντί για σύνδεση παλιάς έκδοσης.")
            # Before binding ensure the active version really exists in safe storage.
            cached_get(active["working_object"], active["working_sha256"])
            obj = {"import_id": active["id"], "drive_id": stage["drive_id"],
                   "item_id": stage["item_id"], "etag": stage["etag"],
                   "synced_version": 1, "link": stage["link"], "name": stage["name"]}
            with lease(catalog.EXCEL_LOCK):
                with transaction() as conn:
                    a = conn.execute("SELECT id,working_sha256,working_version FROM excel_imports "
                                     "WHERE is_active=1").fetchone()
                    if not a or a["id"] != active["id"] or a["working_version"] != 1 or a["working_sha256"] != stage["sha256"]:
                        raise Conflict("Το ενεργό Excel άλλαξε. Κάντε εκ νέου έλεγχο.")
                    conn.execute("INSERT INTO app_settings(key,value) VALUES (?,?) "
                                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                 (BOUND_KEY, json.dumps(obj, ensure_ascii=False)))
            return {"import_id": active["id"], "filename": active["original_filename"], "recovered": True}
        raise ServiceError("Η προηγούμενη προεπισκόπηση έληξε ή ενεργοποιήθηκε ήδη άλλο αρχείο. "
                           "Πατήστε ξανά «Έλεγχος Excel από OneDrive».")

    obj = {"drive_id": stage["drive_id"], "item_id": stage["item_id"],
           "etag": stage["etag"], "synced_version": 1,
           "link": stage["link"], "name": stage["name"]}
    return catalog.activate(stage_id, actor, confirm_replace,
                            onedrive_binding=obj)


def assert_ready_for_transfer(imp: dict) -> None:
    b = bound_import(int(imp["id"]))
    if not b:
        return
    if int(b["synced_version"]) != int(imp["working_version"]):
        raise Conflict("Εκκρεμεί ενημέρωση του OneDrive. Πηγαίνετε Ρυθμίσεις → Επανάληψη συγχρονισμού.")
    meta = file_metadata(b["drive_id"], b["item_id"])
    if meta.get("eTag") != b["etag"]:
        raise Conflict("Κάποιος άλλαξε το online Excel. Ανανεώστε τον κατάλογο από το OneDrive πριν μεταφέρετε προϊόντα.")


def _upload(drive: str, item: str, expected_etag: str, data: bytes, token: str) -> None:
    # The upload-session endpoint explicitly supports conditional If-Match for OneDrive Personal.
    resp = _graph(token, "POST", f"/drives/{drive}/items/{item}/createUploadSession",
                  headers={"If-Match": expected_etag},
                  json={"item": {"@microsoft.graph.conflictBehavior": "replace"}}).json()
    upload_url = resp.get("uploadUrl", "")
    parsed = urlparse(upload_url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (
        host.endswith(".1drv.com") or host.endswith(".sharepoint.com") or host.endswith(".onedrive.com")
        or host in {"api.onedrive.com", "storage.live.com"} or host.endswith(".storage.live.com")
    ):
        raise ServiceError("Η Microsoft επέστρεψε μη έγκυρη διεύθυνση upload.")
    total = len(data)
    if not total or total > MAX_BYTES:
        raise ServiceError("Μη έγκυρο μέγεθος Excel για OneDrive.")
    for start in range(0, total, CHUNK):
        end = min(start + CHUNK, total)
        try:
            with httpx.Client(timeout=90) as client:
                part = client.put(upload_url, content=data[start:end], headers={
                    "Content-Length": str(end - start),
                    "Content-Range": f"bytes {start}-{end-1}/{total}",
                })
            if part.status_code in (409, 412):
                raise Conflict("Το online Excel τροποποιήθηκε ταυτόχρονα. Ο συγχρονισμός σταμάτησε.")
            if part.status_code not in (200, 201, 202):
                raise ServiceError(f"Το OneDrive δεν ολοκλήρωσε την αποθήκευση (HTTP {part.status_code}).")
            if end < total and part.status_code != 202:
                raise ServiceError("Μη αναμενόμενη απάντηση στο τμήμα upload του OneDrive.")
        except httpx.HTTPError:
            raise ServiceError("Διακόπηκε η μεταφορά του ενημερωμένου Excel στο OneDrive.") from None


def sync_pending_under_lease() -> bool:
    """Call while holding the Excel lease. Raises rather than overwrites a changed remote file."""
    imp = catalog.active_import()
    if not imp:
        raise ServiceError("Δεν υπάρχει ενεργό Excel.")
    b = bound_import(int(imp["id"]))
    if not b:
        return False
    current_version = int(imp["working_version"])
    if int(b["synced_version"]) == current_version:
        return True
    if int(b["synced_version"]) > current_version:
        raise Conflict("Ασυμφωνία εκδόσεων OneDrive. Απαιτείται έλεγχος.")
    token = _access_token()
    drive, item = b["drive_id"], b["item_id"]
    data = cached_get(imp["working_object"], imp["working_sha256"]).read_bytes()
    remote, meta = _stable_download(drive, item, token)
    if sha256_bytes(remote) != imp["working_sha256"]:
        if meta["eTag"] != b["etag"]:
            raise Conflict("Το OneDrive άλλαξε εξωτερικά. Δεν αντικαθιστούμε τις αλλαγές. "
                           "Ελέγξτε το αρχείο και το εκκρεμές αντίγραφο στο Supabase.")
        _upload(drive, item, b["etag"], data, token)
        remote, meta = _stable_download(drive, item, token)
        if sha256_bytes(remote) != imp["working_sha256"]:
            raise ServiceError("Ο έλεγχος μετά το ανέβασμα απέτυχε. Ο συγχρονισμός παραμένει εκκρεμής.")
    b["etag"] = meta["eTag"]
    b["synced_version"] = current_version
    with transaction() as db:
        row = db.execute("SELECT value FROM app_settings WHERE key=?", (BOUND_KEY,)).fetchone()
        old = json.loads(row["value"]) if row else None
        if not old or int(old["import_id"]) != int(imp["id"]):
            raise Conflict("Το ενεργό Excel άλλαξε κατά τον συγχρονισμό.")
        db.execute("UPDATE app_settings SET value=? WHERE key=?", (json.dumps(b, ensure_ascii=False), BOUND_KEY))
    return True


def retry_sync() -> bool:
    with lease(catalog.EXCEL_LOCK, busy_message="Εκτελείται άλλη αλλαγή Excel. Δοκιμάστε ξανά."):
        return sync_pending_under_lease()
