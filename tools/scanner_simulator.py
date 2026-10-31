"""Scanner simulator — δοκιμές υψηλής ταχύτητας χωρίς φυσικό barcode scanner.

Δύο τρόποι:

1. api (προεπιλογή): στέλνει scan events απευθείας στο API, όπως ο browser
   (ένα event ανά request ή σε batches), με ρυθμιζόμενο ρυθμό, διπλά, άγνωστα
   και επαναποστολές ίδιου event_id. Μετρά καθυστερήσεις και επαληθεύει ότι
   κάθε event επιβεβαιώθηκε ακριβώς μία φορά.

2. browser: ανοίγει την πραγματική οθόνη σάρωσης σε Chromium (Playwright) και
   «πληκτρολογεί» τα barcode + Enter όπως ένας keyboard-wedge scanner.

Παραδείγματα:
    python tools/scanner_simulator.py --url http://127.0.0.1:8000 --password ΚΩΔΙΚΟΣ --lookup-password ΚΩΔ_ΓΡΑΜΜ --count 200 --rate 20
    python tools/scanner_simulator.py --mode browser --password ΚΩΔΙΚΟΣ --count 100 --rate 15

Χρησιμοποιήστε το σε δοκιμαστικό φάκελο δεδομένων (APP_DATA_DIR) — καταχωρεί πραγματικά σκαναρίσματα.
"""
from __future__ import annotations

import argparse
import random
import statistics
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx


def login(url: str, role: str, password: str) -> str:
    r = httpx.post(f"{url}/api/auth/login", json={"role": role, "password": password, "name": "simulator"}, timeout=10)
    if r.status_code != 200:
        sys.exit(f"Αποτυχία σύνδεσης: {r.text}")
    return r.json()["token"]


def known_barcodes(args: argparse.Namespace) -> list[str]:
    """Barcode από τον ενεργό κατάλογο (απαιτεί κωδικό Γραμματείας/Admin: --lookup-password)."""
    if args.prefix:
        return [f"{args.prefix}{i:04d}" for i in range(1, args.count + 1)]
    if not args.lookup_password:
        print("Προσοχή: χωρίς --lookup-password ή --prefix τα barcode θα είναι τυχαία (άγνωστα).")
        return [f"SIM{i:05d}" for i in range(args.count)]
    token = login(args.url, args.lookup_role, args.lookup_password)
    r = httpx.get(f"{args.url}/api/products/search", params={"q": "", "limit": 5000},
                  headers={"Authorization": f"Bearer {token}"}, timeout=30)
    r.raise_for_status()
    return [p["barcode"] for p in r.json()] or [f"SIM{i:05d}" for i in range(args.count)]


def pick_barcodes(known: list[str], count: int, dup_ratio: float, unknown_ratio: float) -> list[str]:
    out = []
    for i in range(count):
        x = random.random()
        if out and x < dup_ratio:
            out.append(random.choice(out))
        elif x < dup_ratio + unknown_ratio:
            out.append(f"UNKNOWN-{uuid.uuid4().hex[:6].upper()}")
        else:
            out.append(known[i % len(known)])
    return out


def run_api(args: argparse.Namespace) -> int:
    token = login(args.url, args.role, args.password)
    headers = {"Authorization": f"Bearer {token}"}
    barcodes = pick_barcodes(known_barcodes(args), args.count, args.duplicates, args.unknown)
    events = [{"event_id": uuid.uuid4().hex, "barcode": b, "client_ts": None, "seq": i} for i, b in enumerate(barcodes)]
    acks: dict[str, dict] = {}
    lat: list[float] = []
    lock = threading.Lock()
    errors = 0

    def send(batch: list[dict]) -> None:
        nonlocal errors
        for attempt in range(8):
            t0 = time.perf_counter()
            try:
                with httpx.Client(timeout=10) as c:
                    r = c.post(f"{args.url}/api/scans", json={"device_id": "simulator", "events": batch}, headers=headers)
                r.raise_for_status()
                dt = time.perf_counter() - t0
                with lock:
                    for res in r.json()["results"]:
                        if res["event_id"] in acks and not res.get("replayed"):
                            print("ΣΦΑΛΜΑ: διπλή επιβεβαίωση", res["event_id"])
                        acks[res["event_id"]] = res
                    lat.append(dt)
                if args.resend and random.random() < args.resend:  # επαναποστολή ίδιου event_id
                    with httpx.Client(timeout=10) as c:
                        c.post(f"{args.url}/api/scans", json={"device_id": "simulator", "events": batch}, headers=headers)
                return
            except httpx.HTTPError:
                with lock:
                    errors += 1
                time.sleep(min(2 ** attempt * 0.2, 5))

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for i in range(0, len(events), args.batch):
            pool.submit(send, events[i:i + args.batch])
            if args.rate > 0:
                time.sleep(args.batch / args.rate)
    elapsed = time.perf_counter() - start
    kinds: dict[str, int] = {}
    for r in acks.values():
        kinds[r["result"]] = kinds.get(r["result"], 0) + 1
    missing = [e["event_id"] for e in events if e["event_id"] not in acks]
    print(f"Σαρώσεις: {len(events)}  επιβεβαιώθηκαν: {len(acks)}  χάθηκαν: {len(missing)}  σφάλματα δικτύου: {errors}")
    print(f"Χρόνος: {elapsed:.2f}s  ρυθμός: {len(events) / elapsed:.1f} scans/s")
    if lat:
        q = statistics.quantiles(lat, n=20) if len(lat) >= 20 else [max(lat)] * 19
        print(f"Καθυστέρηση request: median {statistics.median(lat) * 1000:.0f} ms, p95 {q[18] * 1000:.0f} ms, max {max(lat) * 1000:.0f} ms")
    print("Αποτελέσματα:", kinds)
    return 1 if missing else 0


def run_browser(args: argparse.Namespace) -> int:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=not args.headed)
        except Exception:
            browser = p.chromium.launch(headless=not args.headed, executable_path=args.chromium, args=["--no-sandbox"])
        page = browser.new_page()
        page.goto(f"{args.url}/scanner/")
        page.fill("#login-name", "Simulator")
        page.select_option("#login-role", args.role)
        page.fill("#login-password", args.password)
        page.click("#login-form button[type=submit]")
        page.wait_for_selector("#login-overlay.hidden", state="attached")
        page.mouse.click(5, 300)
        barcodes = pick_barcodes(known_barcodes(args), args.count, args.duplicates, args.unknown)
        start = time.perf_counter()
        for b in barcodes:
            page.keyboard.type(b, delay=args.key_delay)
            page.keyboard.press("Enter")
            if args.rate > 0:
                time.sleep(max(0.0, 1 / args.rate - 0.002 * len(b)))
        typed = time.perf_counter() - start
        page.wait_for_function("window.__scanner.state.pending.size === 0", timeout=120000)
        total = time.perf_counter() - start
        print(f"Πληκτρολογήθηκαν {len(barcodes)} barcode σε {typed:.2f}s, όλα επιβεβαιώθηκαν σε {total:.2f}s "
              f"({len(barcodes) / total:.1f}/s)")
        browser.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--role", default="scanner")
    ap.add_argument("--password", required=True)
    ap.add_argument("--mode", choices=["api", "browser"], default="api")
    ap.add_argument("--count", type=int, default=100)
    ap.add_argument("--rate", type=float, default=10, help="σαρώσεις/δευτερόλεπτο (0 = όσο πιο γρήγορα)")
    ap.add_argument("--batch", type=int, default=1, help="events ανά request (api mode)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--duplicates", type=float, default=0.05, help="ποσοστό ξανασκαναρισμάτων")
    ap.add_argument("--unknown", type=float, default=0.02, help="ποσοστό άγνωστων barcode")
    ap.add_argument("--resend", type=float, default=0.05, help="ποσοστό επαναποστολής ίδιου event_id")
    ap.add_argument("--prefix", default=None, help="π.χ. TST για barcode TST0001.. (αλλιώς από τον κατάλογο)")
    ap.add_argument("--lookup-role", default="secretary", help="ρόλος για ανάγνωση καταλόγου barcode")
    ap.add_argument("--lookup-password", default=None, help="κωδικός Γραμματείας/Admin για πραγματικά barcode")
    ap.add_argument("--key-delay", type=float, default=2, help="ms ανά πλήκτρο (browser mode)")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--chromium", default="/opt/pw-browsers/chromium")
    args = ap.parse_args()
    return run_api(args) if args.mode == "api" else run_browser(args)


if __name__ == "__main__":
    raise SystemExit(main())
