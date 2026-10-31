"""Tests 7, 8, 10: ταυτόχρονες υποβολές, burst ≥100 events σε <10s, restart χωρίς απώλεια."""
from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from tests.conftest import PASSWORDS, LiveServer


def _token(url: str, role: str) -> dict[str, str]:
    r = httpx.post(url + "/api/auth/login", json={"role": role, "password": PASSWORDS[role], "name": role})
    r.raise_for_status()
    return {"Authorization": "Bearer " + r.json()["token"]}


def _activate(url: str, path) -> None:
    h = _token(url, "admin")
    r = httpx.post(url + "/api/admin/imports/stage", params={"filename": path.name}, content=path.read_bytes(),
                   headers=h, timeout=120)
    r.raise_for_status()
    r = httpx.post(url + "/api/admin/imports/activate", json={"staging_id": r.json()["staging_id"]}, headers=h, timeout=120)
    r.raise_for_status()


def _post(url: str, headers, events, timeout=15):
    with httpx.Client(timeout=timeout) as c:
        r = c.post(url + "/api/scans", json={"device_id": "load", "events": events}, headers=headers)
        r.raise_for_status()
        return r.json()["results"]


def test_concurrent_submissions(live_server: LiveServer, synthetic_master):
    _activate(live_server.url, synthetic_master)
    h = _token(live_server.url, "scanner")
    barcodes = [f"TST{i:04d}" for i in range(1, 121)]
    events = [{"event_id": uuid.uuid4().hex, "barcode": b} for b in barcodes]
    # 120 ταυτόχρονα requests (ένα event το καθένα) από 24 threads, + επανάληψη 40 από αυτά
    replays = events[:40]
    with ThreadPoolExecutor(max_workers=24) as pool:
        futures = [pool.submit(_post, live_server.url, h, [e]) for e in events + replays]
        results = [f.result()[0] for f in futures]
    assert all(r["result"] == "ok" for r in results), {r["result"] for r in results}
    sec = _token(live_server.url, "secretary")
    dash = httpx.get(live_server.url + "/api/dashboard", headers=sec, timeout=30).json()
    assert dash["totals"]["active_barcodes"] == 120
    hist = httpx.get(live_server.url + "/api/history", headers=sec, timeout=30).json()
    assert len(hist) == 120
    assert len({r["event_id"] for r in hist}) == 120


def test_concurrent_same_barcode_counts_once(live_server: LiveServer, synthetic_master):
    _activate(live_server.url, synthetic_master)
    h = _token(live_server.url, "scanner")
    with ThreadPoolExecutor(max_workers=10) as pool:
        res = list(pool.map(lambda _: _post(live_server.url, h, [{"event_id": uuid.uuid4().hex, "barcode": "VS11486"}])[0],
                            range(10)))
    assert sorted(r["result"] for r in res).count("ok") == 1
    assert sorted(r["result"] for r in res).count("duplicate") == 9
    sec = _token(live_server.url, "secretary")
    dash = httpx.get(live_server.url + "/api/dashboard", headers=sec).json()
    assert dash["totals"]["total_kilos"] == "412"


@pytest.mark.slow
def test_burst_scanner_rate(live_server: LiveServer, synthetic_master):
    """Προσομοίωση scanner: 300 σαρώσεις με ρυθμό ~30/s (3× η απαίτηση), όπως τις στέλνει ο browser."""
    _activate(live_server.url, synthetic_master)
    h = _token(live_server.url, "scanner")
    barcodes = [f"TST{i:04d}" for i in range(1, 301)]
    events = [{"event_id": uuid.uuid4().hex, "barcode": b, "seq": i} for i, b in enumerate(barcodes)]
    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = []
        for e in events:
            futures.append((time.perf_counter(), pool.submit(_post, live_server.url, h, [e])))
            time.sleep(1 / 30)
        results = []
        for t0, f in futures:
            results.append(f.result()[0])
    elapsed = time.perf_counter() - start
    assert elapsed < 10 + 1, elapsed
    assert len(results) == 300 and all(r["result"] == "ok" for r in results)
    sec = _token(live_server.url, "secretary")
    assert httpx.get(live_server.url + "/api/dashboard", headers=sec).json()["totals"]["active_barcodes"] == 300


def test_burst_100_in_batches_under_10s(live_server: LiveServer, synthetic_master):
    """Burst 100 events (με διπλά και άγνωστα) — όλα επιβεβαιώνονται μέσα σε 10 δευτερόλεπτα."""
    _activate(live_server.url, synthetic_master)
    h = _token(live_server.url, "scanner")
    codes = [f"TST{i:04d}" for i in range(1, 81)] + ["TST0001"] * 10 + [f"UNK{i}" for i in range(10)]
    events = [{"event_id": uuid.uuid4().hex, "barcode": b} for b in codes]
    start = time.perf_counter()
    results = []
    for i in range(0, len(events), 10):  # 10 events ανά request (όπως ο browser όταν συσσωρεύονται)
        results += _post(live_server.url, h, events[i:i + 10])
    elapsed = time.perf_counter() - start
    assert elapsed < 10, elapsed
    kinds = [r["result"] for r in results]
    assert kinds.count("ok") == 80 and kinds.count("duplicate") == 10 and kinds.count("unknown") == 10


def test_restart_keeps_history_and_active_list(tmp_path, synthetic_master):
    data = tmp_path / "data"
    srv = LiveServer(data).start()
    try:
        _activate(srv.url, synthetic_master)
        h = _token(srv.url, "scanner")
        _post(srv.url, h, [{"event_id": uuid.uuid4().hex, "barcode": b} for b in ("VS11486", "VS11487", "VS11486")])
        sec = _token(srv.url, "secretary")
        dash = httpx.get(srv.url + "/api/dashboard", headers=sec).json()
        item = next(i for i in dash["items"] if i["barcode"] == "VS11486")
        r = httpx.post(srv.url + f"/api/items/{item['id']}/transfer", headers=sec, timeout=60)
        assert r.status_code == 200, r.text
    finally:
        srv.stop()

    srv2 = LiveServer(data).start()  # νέα διεργασία-server, ίδιος φάκελος δεδομένων
    try:
        sec = _token(srv2.url, "secretary")
        hist = httpx.get(srv2.url + "/api/history", headers=sec).json()
        assert len(hist) == 3
        assert {r["result"] for r in hist} == {"ok", "duplicate"}
        assert any(r["transfer_status"] == "Μεταφέρθηκε" for r in hist)
        dash = httpx.get(srv2.url + "/api/dashboard", headers=sec).json()
        assert [i["barcode"] for i in dash["items"]] == ["VS11487"]
        assert dash["totals"]["total_kilos"] == "416"
        assert dash["import"] is not None  # το Excel παραμένει ενεργό μετά το restart
        # ίδιο event ID μετά από restart εξακολουθεί να είναι idempotent
        first = hist[-1]
        again = _post(srv2.url, _token(srv2.url, "scanner"), [{"event_id": first["event_id"], "barcode": first["barcode"]}])[0]
        assert again["replayed"] is True
    finally:
        srv2.stop()
