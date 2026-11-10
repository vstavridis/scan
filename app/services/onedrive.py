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
ERROR_KEY = "onedrive.last_error"


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
            # HTTP 409 from createUploadSession is NOT proof that the remote
            # Excel changed. Only 412 is a failed If-Match precondition.
            # Extract the documented Microsoft error *code*, never the body:
            # Graph messages may include preauthorized URLs or identifiers.
            import re
            error_code = ""
            try:
                detail = r.json().get("error", {}).get("code", "")
                if isinstance(detail, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,60}", detail):
                    error_code = " · " + detail
            except (ValueError, TypeError, AttributeError):
                pass
            if r.status_code == 412:
                raise Conflict("Το OneDrive απέρριψε το If-Match (HTTP 412" + error_code
                               + "). Η έκδοση του αρχείου άλλαξε. Δεν έγινε αντικατάσταση.")
            raise Conflict("Η Microsoft απέρριψε τη δημιουργία upload session "
                           "(HTTP 409" + error_code + "). Αυτό μπορεί να είναι κλείδωμα "
                           "ή σύγκρουση αρχείου, όχι απαραίτητα νέα έκδοση. "
                           "Το αντίγραφο Supabase παραμένει ασφαλές. "
                           "Κλείστε το Excel Online και ελέγξτε την αιτία πριν από νέα προσπάθεια.")
        if r.status_code == 401:
            raise ServiceError("Η εξουσιοδότηση OneDrive έληξε. Συνδεθείτε ξανά από τις Ρυθμίσεις.")
        if r.status_code == 403:
            raise ServiceError("Η Microsoft αρνήθηκε πρόσβαση στο Excel. Ελέγξτε τα Files.ReadWrite και ότι το αρχείο είναι δικό σας.")
        if r.status_code == 404:
            raise ServiceError("Το αρχείο OneDrive δεν βρέθηκε. Ελέγξτε τον σύνδεσμο.")
        if r.status_code == 423:
            raise ServiceError("Το OneDrive αναφέρει ότι το Excel είναι κλειδωμένο (HTTP 423). "
                               "Κλείστε το στο Excel Online και επαναλάβετε τον συγχρονισμό.")
        if r.status_code == 429:
            raise ServiceError("Η Microsoft περιόρισε προσωρινά τα αιτήματα (HTTP 429). "
                               "Δοκιμάστε ξανά αργότερα.")
        if r.status_code >= 400:
            # Expose only bounded HTTP status and a validated Microsoft error CODE, not
            # the response body: raw Graph errors may contain internal IDs and URLs.
            errcode = ""
            try:
                import re
                value = r.json().get("error", {}).get("code", "")
                if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,60}", value):
                    errcode = " / " + value
            except (ValueError, AttributeError, TypeError):
                pass
            raise ServiceError(f"Το OneDrive απέρριψε το αίτημα (HTTP {r.status_code}{errcode}). "
                               "Το ενημερωμένο Excel παραμένει ασφαλές στο Supabase.")
        return r
    except (Conflict, ServiceError):
        raise
    except httpx.TimeoutException:
        raise ServiceError("Έληξε ο χρόνος αναμονής της Microsoft. Το αρχείο Supabase παραμένει ασφαλές.") from None
    except httpx.HTTPError as exc:
        log.warning("OneDrive Graph request failed: %s", type(exc).__name__)
        raise ServiceError("Δεν ήταν δυνατή η επικοινωνία με το OneDrive. Δοκιμάστε ξανά.") from None


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
            "name": b.get("name") if active else None, "link": b.get("link") if active else None,
            "synced_version": int(b["synced_version"]) if active else None,
            "synced_at": b.get("synced_at") if active else None}


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
    # This is an UPDATE of an existing item by its stable item id. Microsoft
    # documents that no JSON body is needed. In particular, don't request
    # "conflictBehavior: replace" (a filename collision policy for new items).
    # Keep If-Match so a concurrent change fails with HTTP 412.
    resp = _graph(token, "POST", f"/drives/{drive}/items/{item}/createUploadSession",
                  headers={"If-Match": expected_etag}).json()
    upload_url = resp.get("uploadUrl")
    if not isinstance(upload_url, str) or not upload_url:
        # A successful Graph response is not guaranteed to contain a session URL.
        # No preauthorized URL / token appears in diagnostics.
        raise ServiceError("Η Microsoft δεν επέστρεψε uploadUrl στο createUploadSession. "
                           "Το Excel παραμένει ασφαλές στο Supabase.")
    parsed = urlparse(upload_url)
    host = (parsed.hostname or "").lower()
    allowed = (
        host.endswith(".1drv.com")
        or host in {"1drv.com", "api.onedrive.com", "onedrive.com", "storage.live.com"}
        or host.endswith(".onedrive.com")
        or host.endswith(".sharepoint.com")
        or host == "my.microsoftpersonalcontent.com"
        or host.endswith(".microsoftpersonalcontent.com")
        or host.endswith(".storage.live.com")
    )
    try:
        valid_port = parsed.port in (None, 443)
    except ValueError:
        valid_port = False
    if (parsed.scheme != "https" or not host or not allowed or not valid_port
            or parsed.username is not None or parsed.password is not None
            or parsed.fragment or len(upload_url) > 12000):
        # uploadUrl is a bearer-equivalent secret: NEVER surface path/query or
        # the full URL. A strict ASCII hostname alone is safe for troubleshooting.
        import re
        safe_host = host if re.fullmatch(r"[a-z0-9.-]{1,253}", host) else "(μη αναγνώσιμο)"
        reason = "μη επιτρεπτός host" if not allowed else "μη έγκυρο HTTPS URL"
        raise ServiceError(
            f"Η Microsoft επέστρεψε διεύθυνση upload με {reason}: {safe_host}. "
            "Στείλτε μόνο το όνομα του host, ποτέ ολόκληρη τη διεύθυνση upload. "
            "Δεν έγινε καμία εγγραφή στο OneDrive."
        )
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


def _baseline_sha(import_id: int, synced_version: int) -> str:
    """SHA of the durable workbook version that the remote file is expected to contain.

    Read from the completed transfer's journal, not the current working workbook.
    This permits an eTag-only metadata change while refusing to overwrite any
    actual external changes to the OneDrive file.
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT base_object, excel_before_sha256 FROM transfer_operations "
            "WHERE import_id=? AND base_version=? AND status='completed' ORDER BY id LIMIT 1",
            (import_id, synced_version),
        ).fetchone()
    if not row or not row["base_object"] or not row["excel_before_sha256"]:
        raise Conflict("Δεν βρέθηκε αξιόπιστη προηγούμενη έκδοση για σύγκριση με το OneDrive. "
                       "Καμία αλλαγή δεν έγινε στο online αρχείο.")
    # Check the actual immutable Supabase backup as well as the journal hash.
    baseline = cached_get(row["base_object"], row["excel_before_sha256"]).read_bytes()
    if sha256_bytes(baseline) != row["excel_before_sha256"]:
        raise Conflict("Το αντίγραφο ασφαλείας του Excel δεν συμφωνεί με το ιστορικό μεταφοράς.")
    return row["excel_before_sha256"]


def sync_pending_under_lease() -> bool:
    """Publish an already-committed version safely. Caller holds the Excel lease.

    Never assume that a changed eTag means changed content; conversely never ignore
    a content change merely because the eTag happens to be equal. The download is
    compared against the immutable pre-transfer workbook before overwriting it.
    """
    imp = catalog.active_import()
    if not imp:
        raise ServiceError("Δεν υπάρχει ενεργό Excel.")
    b = bound_import(int(imp["id"]))
    if not b:
        return False
    current_version = int(imp["working_version"])
    previously_synced = int(b["synced_version"])
    if previously_synced == current_version:
        return True
    if int(b["synced_version"]) > current_version:
        raise Conflict("Ασυμφωνία εκδόσεων OneDrive. Απαιτείται έλεγχος.")

    token = _access_token()
    drive, item = b["drive_id"], b["item_id"]
    data = cached_get(imp["working_object"], imp["working_sha256"]).read_bytes()
    remote, meta = _stable_download(drive, item, token)
    remote_sha = sha256_bytes(remote)
    target_sha = imp["working_sha256"]

    if remote_sha != target_sha:
        baseline_sha = _baseline_sha(int(imp["id"]), int(b["synced_version"]))
        if remote_sha != baseline_sha:
            raise Conflict(
                "Το online Excel έχει ΔΙΑΦΟΡΕΤΙΚΟ ΠΕΡΙΕΧΟΜΕΝΟ από την έκδοση πριν τη μεταφορά. "
                "Δεν θα αντικαταστήσουμε αλλαγές που μπορεί να έγιναν στο OneDrive. "
                "Η ενημερωμένη έκδοση βρίσκεται ασφαλής στο Supabase (Ρυθμίσεις → Λήψεις). "
                "Χρειάζεται σύγκριση των δύο Excel πριν συνεχίσουμε."
            )
        if meta["eTag"] != b["etag"]:
            log.info("OneDrive eTag drift with identical workbook bytes; use verified latest eTag for upload")
        # If-Match protects against a new concurrent edit after _stable_download.
        _upload(drive, item, meta["eTag"], data, token)
        remote, meta = _stable_download(drive, item, token)
        if sha256_bytes(remote) != target_sha:
            raise ServiceError(
                "Ο έλεγχος μετά το ανέβασμα απέτυχε. Η μεταφορά στο Supabase διατηρήθηκε· "
                "το OneDrive παραμένει σε εκκρεμότητα."
            )

    # The uploaded version is identical, or an earlier upload actually succeeded
    # but its acknowledgement/DB update was lost. Mark it synced without re-upload.
    b["etag"] = meta["eTag"]
    b["synced_version"] = current_version
    with transaction() as db:
        row = db.execute("SELECT value FROM app_settings WHERE key=?", (BOUND_KEY,)).fetchone()
        old = json.loads(row["value"]) if row else None
        if (not old or int(old["import_id"]) != int(imp["id"])
                or int(old["synced_version"]) != previously_synced
                or old["drive_id"] != b["drive_id"]
                or old["item_id"] != b["item_id"]):
            raise Conflict("Η κατάσταση συγχρονισμού άλλαξε. Ελέγξτε την έκδοση του OneDrive.")
        db.execute("UPDATE app_settings SET value=? WHERE key=?",
                   (json.dumps(b, ensure_ascii=False), BOUND_KEY))
    return True



def inspect_pending() -> dict:
    """Read-only diagnostic comparing actual OneDrive workbook with pre-transfer backup.

    No upload, no changes to the online workbook or to pending sync status.
    Reads all worksheets, shows only a bounded number of cell-level differences.
    """
    from app.services.onedrive_diff import compare_workbook_cells

    with lease(catalog.EXCEL_LOCK, busy_message="Εκτελείται άλλη ενέργεια Excel. Δοκιμάστε ξανά."):
        imp = catalog.active_import()
        if imp is None:
            raise ServiceError("Δεν υπάρχει ενεργό Excel.")
        b = bound_import(int(imp["id"]))
        if b is None:
            raise ServiceError("Δεν υπάρχει συνδεδεμένο OneDrive Excel.")
        synced_version = int(b["synced_version"])
        current_version = int(imp["working_version"])
        if synced_version >= current_version:
            raise ServiceError("Το OneDrive δεν εμφανίζεται ως εκκρεμές· δεν χρειάζεται σύγκριση.")
        with connect() as conn:
            prior = conn.execute(
                "SELECT base_object, excel_before_sha256 FROM transfer_operations "
                "WHERE import_id=? AND base_version=? AND status='completed' ORDER BY id LIMIT 1",
                (int(imp["id"]), synced_version),
            ).fetchone()
        if not prior:
            raise ServiceError("Δεν βρέθηκε το αντίγραφο Excel πριν από τη μεταφορά.")
        baseline = cached_get(prior["base_object"], prior["excel_before_sha256"]).read_bytes()
        latest = cached_get(imp["working_object"], imp["working_sha256"]).read_bytes()
        token = _access_token()
        online, meta = _stable_download(b["drive_id"], b["item_id"], token)
        try:
            result = compare_workbook_cells(baseline, online)
        except Exception as exc:
            # Never let malformed or unsupported XLSX structure take down Settings.
            # The original workbooks remain intact and no upload is attempted.
            log.warning("OneDrive read-only XLSX comparison failed: %s", type(exc).__name__)
            raise ServiceError("Δεν ολοκληρώθηκε η σύγκριση των Excel. "
                               "Τα αρχεία παραμένουν αμετάβλητα. Ελέγξτε ότι το "
                               "OneDrive αρχείο ανοίγει σωστά ως .xlsx.") from None
        result.update({
            "original_version": synced_version,
            "updated_version": current_version,
            "etag_changed": meta.get("eTag") != b["etag"],
            "online_identical_before": sha256_bytes(online) == sha256_bytes(baseline),
            "online_identical_after": sha256_bytes(online) == sha256_bytes(latest),
            "online_bytes_different": sha256_bytes(online) != sha256_bytes(baseline),
        })
        return result


def retry_sync() -> bool:
    """Drain all durable pending transfers in order, preserving the latest online workbook."""
    from app.services.onedrive_outbox import retry_sync as flush
    return bool(flush()["synced"])
