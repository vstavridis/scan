"""Scanner simulator — δοκιμές υψηλής ταχύτητας χωρίς φυσικό barcode scanner.

Δύο τρόποι:

1. backend: στέλνει batches scan events κατευθείαν στον backend της εφαρμογής (ίδιος κώδικας με
   την οθόνη σάρωσης), στη βάση που ορίζουν τα Secrets/env. Μετρά καθυστερήσεις ανά batch και
   επαληθεύει ότι κάθε event επιβεβαιώθηκε ακριβώς μία φορά (και με επαναποστολές).

2. browser: ανοίγει την εφαρμογή Streamlit (τοπικά ή στο Streamlit Cloud) σε Chromium,
   συνδέεται ως Scanner και «πληκτρολογεί» barcode + Enter όπως ένας keyboard-wedge scanner.

Παραδείγματα:
    python tools/scanner_simulator.py --mode browser --url https://<app>.streamlit.app --headed --count 100
    python tools/scanner_simulator.py --mode backend --count 300 --batch 10      # με τα Secrets/env της βάσης

ΠΡΟΣΟΧΗ: καταχωρεί πραγματικά σκαναρίσματα. Χρησιμοποιήστε δοκιμαστικό project/βάση.
"""
from __future__ import annotations

import argparse
import random
import statistics
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def pick(known: list[str], count: int, dup: float, unknown: float) -> list[str]:
    out: list[str] = []
    for i in range(count):
        x = random.random()
        if out and x < dup:
            out.append(random.choice(out))
        elif x < dup + unknown or not known:
            out.append(f"UNKNOWN-{uuid.uuid4().hex[:6].upper()}")
        else:
            out.append(known[i % len(known)])
    return out


def run_backend(args: argparse.Namespace) -> int:
    from app import backend
    backend.startup()
    scanner = lookup = backend.identity("simulator")
    known = [p["barcode"] for p in backend.search_products(lookup, "", limit=5000)]
    codes = pick(known, args.count, args.duplicates, args.unknown)
    events = [{"event_id": uuid.uuid4().hex, "barcode": c, "seq": i} for i, c in enumerate(codes)]
    acks: dict[str, dict] = {}
    lat: list[float] = []
    start = time.perf_counter()
    for i in range(0, len(events), args.batch):
        batch = events[i:i + args.batch]
        t = time.perf_counter()
        res = backend.submit_scans(scanner, batch, device_id="simulator")
        lat.append(time.perf_counter() - t)
        for r in res["results"]:
            acks[r["event_id"]] = r
        if args.resend and random.random() < args.resend:   # επαναποστολή ίδιων event_id
            again = backend.submit_scans(scanner, batch, device_id="simulator")["results"]
            assert all(a["replayed"] for a in again), "επαναποστολή δημιούργησε νέα εγγραφή!"
        if args.rate > 0:
            time.sleep(max(0.0, len(batch) / args.rate - lat[-1]))
    elapsed = time.perf_counter() - start
    missing = [e for e in events if e["event_id"] not in acks]
    kinds: dict[str, int] = {}
    for r in acks.values():
        kinds[r["result"]] = kinds.get(r["result"], 0) + 1
    print(f"Σαρώσεις: {len(events)}  επιβεβαιώθηκαν: {len(acks)}  χάθηκαν: {len(missing)}")
    print(f"Χρόνος: {elapsed:.2f}s  ρυθμός: {len(events) / elapsed:.1f} scans/s  "
          f"batch: median {statistics.median(lat) * 1000:.0f} ms, max {max(lat) * 1000:.0f} ms")
    print("Αποτελέσματα:", kinds)
    return 1 if missing else 0


def run_browser(args: argparse.Namespace) -> int:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=not args.headed)
        except Exception:
            browser = p.chromium.launch(headless=not args.headed, executable_path=args.chromium, args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1400, "height": 950},
                                  storage_state=args.storage_state if args.storage_state else None)
        page = ctx.new_page()
        page.goto(args.url.rstrip("/") + "/scanner?user=Simulator")
        page.wait_for_selector(".whs-root", timeout=120000)
        page.mouse.click(700, 600)
        codes = [l.strip() for l in open(args.barcodes_file, encoding="utf-8") if l.strip()] if args.barcodes_file \
            else [f"{args.prefix}{i:04d}" for i in range(1, args.count + 1)]
        start = time.perf_counter()
        for c in codes[: args.count]:
            page.keyboard.type(c, delay=args.key_delay)
            page.keyboard.press("Enter")
            if args.rate > 0:
                time.sleep(max(0.0, 1 / args.rate - args.key_delay / 1000 * len(c)))
        typed = time.perf_counter() - start
        page.wait_for_function("window.__whScannerState.pending.size === 0", timeout=300000)
        total = time.perf_counter() - start
        n = min(len(codes), args.count)
        print(f"Πληκτρολογήθηκαν {n} barcode σε {typed:.2f}s, όλα επιβεβαιώθηκαν σε {total:.2f}s ({n / total:.1f}/s), "
              f"επαναποστολές: {page.evaluate('window.__whScannerState.resends || 0')}")
        browser.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["backend", "browser"], default="browser")
    ap.add_argument("--url", default="http://localhost:8501")
    ap.add_argument("--storage-state", default=None,
                    help="browser mode, Private app: αρχείο Playwright storage state με σύνδεση στο Streamlit Cloud")
    ap.add_argument("--count", type=int, default=100)
    ap.add_argument("--rate", type=float, default=0, help="σαρώσεις/δευτερόλεπτο (0 = όσο πιο γρήγορα)")
    ap.add_argument("--batch", type=int, default=5, help="events ανά batch (backend mode)")
    ap.add_argument("--duplicates", type=float, default=0.05)
    ap.add_argument("--unknown", type=float, default=0.02)
    ap.add_argument("--resend", type=float, default=0.05, help="ποσοστό επαναποστολής ίδιων event_id (backend mode)")
    ap.add_argument("--prefix", default="SIM", help="browser mode: barcode <prefix>0001..")
    ap.add_argument("--barcodes-file", default=None, help="browser mode: αρχείο με ένα barcode ανά γραμμή")
    ap.add_argument("--key-delay", type=float, default=2, help="ms ανά πλήκτρο (browser mode)")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--chromium", default="/opt/pw-browsers/chromium")
    args = ap.parse_args()
    return run_backend(args) if args.mode == "backend" else run_browser(args)


if __name__ == "__main__":
    raise SystemExit(main())
