"""End-to-end: η πραγματική εφαρμογή Streamlit (`streamlit run streamlit_app.py`) σε Chromium.

Προσομοιώνουν keyboard-wedge scanner (πλήκτρα + Enter) μέσα στη σελίδα Streamlit και ελέγχουν:
ταχύτητα με πραγματικές μετρήσεις, επιβεβαίωση μόνο μετά από commit, διπλά/άγνωστα/αμφίσημα,
διακοπή/επανεκκίνηση server, ανανέωση σελίδας με εκκρεμή, χαμένα/διπλά acknowledgements,
συγχρονισμό αποθήκης-γραμματείας, πλήρη οθόνη και ελληνική διάταξη πληκτρολογίου.

Βάση: PostgreSQL αν οριστεί WAREHOUSE_TEST_PG, αλλιώς SQLite. Storage: τοπικός φάκελος.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import httpx
import pytest

from app import backend
from tests.conftest import (PASSWORDS, ROOT, activate_workbook, configure_env, free_port, login, pg_admin_url,
                            temp_pg_database)

playwright = pytest.importorskip("playwright.sync_api")
pytestmark = [pytest.mark.browser, pytest.mark.slow]


class StreamlitServer:
    def __init__(self, env: dict[str, str], latency_ms: int = 0):
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.env = {**os.environ, **env, "APP_SIMULATE_DB_LATENCY_MS": str(latency_ms), "PYTHONPATH": str(ROOT)}
        self.proc: subprocess.Popen | None = None

    def start(self) -> "StreamlitServer":
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "streamlit", "run", str(ROOT / "streamlit_app.py"), "--server.port", str(self.port),
             "--server.headless", "true", "--browser.gatherUsageStats", "false", "--server.fileWatcherType", "none"],
            cwd=ROOT, env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                if httpx.get(self.url + "/_stcore/health", timeout=1).status_code == 200:
                    return self
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        raise RuntimeError("Το Streamlit δεν ξεκίνησε")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(20)


@contextmanager
def app_server(tmp_path: Path, latency_ms: int = 0) -> Iterator[StreamlitServer]:
    data = tmp_path / "data"
    if pg_admin_url():
        with temp_pg_database() as url:
            env = configure_env(data, url)
            srv = StreamlitServer(env, latency_ms).start()
            try:
                yield srv
            finally:
                srv.stop()
    else:
        env = configure_env(data)
        srv = StreamlitServer(env, latency_ms).start()
        try:
            yield srv
        finally:
            srv.stop()


@pytest.fixture
def browser():
    with playwright.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:
            path = os.environ.get("PLAYWRIGHT_CHROMIUM_PATH", "/opt/pw-browsers/chromium")
            if not os.path.exists(path):
                pytest.skip("Chromium δεν είναι διαθέσιμο")
            b = p.chromium.launch(executable_path=path, args=["--no-sandbox"])
        yield b
        b.close()


def st_login(page, url: str, role: str, name: str, password: str) -> None:
    page.goto(url)
    page.wait_for_selector('input[aria-label="Κωδικός"]', timeout=30000)
    labels = {"scanner": "Αποθήκη (Scanner)", "secretary": "Γραμματεία", "admin": "Διαχειριστής"}
    if role != "scanner":
        page.locator('[data-testid="stSelectbox"]').click()
        page.get_by_role("option", name=labels[role]).click()
    page.fill('input[aria-label="Όνομα χρήστη / συσκευής"]', name)
    page.fill('input[aria-label="Κωδικός"]', password)
    page.get_by_role("button", name="Σύνδεση").click()
    page.wait_for_selector("text=Αποσύνδεση", timeout=30000)


def open_scanner(browser, url: str):
    ctx = browser.new_context(viewport={"width": 1400, "height": 950})
    page = ctx.new_page()
    st_login(page, url, "scanner", "Αποθήκη PC1", PASSWORDS["scanner"])
    page.wait_for_selector(".whs-root", timeout=30000)
    wait_connected(page)
    page.mouse.click(700, 600)   # ένα κλικ στην αρχή της βάρδιας
    return ctx, page


def wait_connected(page, timeout=30000) -> None:
    page.wait_for_function("document.querySelector('[data-ref=conn]') && "
                           "document.querySelector('[data-ref=conn]').textContent.includes('ΣΥΝΔΕΔΕΜΕΝΟ')", timeout=timeout)


def wedge(page, code: str) -> None:
    page.keyboard.type(code, delay=2)   # ~2 ms ανά χαρακτήρα, όπως ένας γρήγορος scanner
    page.keyboard.press("Enter")


def pending(page) -> int:
    return page.evaluate("window.__whScannerState.pending.size")


def idb_count(page) -> int:
    return page.evaluate("""() => new Promise(res => { const r = indexedDB.open('warehouse-scanner');
        r.onsuccess = () => { const q = r.result.transaction('events').objectStore('events').count();
        q.onsuccess = () => res(q.result); }; })""")


def history_count() -> int:
    return len(backend.history_rows(login("secretary")))


def _rate_run(browser, srv, synthetic_master, n: int) -> tuple[float, float, int]:
    activate_workbook(synthetic_master)
    ctx, page = open_scanner(browser, srv.url)
    codes = [f"TST{i:04d}" for i in range(1, n + 1)]
    start = time.perf_counter()
    for c in codes:
        wedge(page, c)
    typing = time.perf_counter() - start
    page.wait_for_function("window.__whScannerState.pending.size === 0", timeout=120000)
    total = time.perf_counter() - start
    resends = page.evaluate("window.__whScannerState.resends || 0")
    assert [i["barcode"] for i in backend.dashboard(login("secretary"))["items"]] == codes  # όλα, με τη σειρά
    assert history_count() == n                                                          # καμία διπλή εγγραφή
    assert idb_count(page) == 0                                                           # η τοπική ουρά άδειασε
    ctx.close()
    return typing, total, resends


def test_rapid_scans_measured_rate(browser, tmp_path, synthetic_master):
    with app_server(tmp_path) as srv:
        typing, total, resends = _rate_run(browser, srv, synthetic_master, 150)
    rate = 150 / total
    print(f"\n[μέτρηση] 150 barcode: πληκτρολόγηση {typing:.2f}s, όλα επιβεβαιωμένα σε {total:.2f}s "
          f"= {rate:.1f} barcode/s (επαναποστολές: {resends})")
    assert rate >= 10, rate


@pytest.mark.parametrize("latency_ms", [10, 30, 80, 150])
def test_rapid_scans_with_remote_db_latency(browser, tmp_path, synthetic_master, latency_ms):
    """Προσομοίωση απομακρυσμένης βάσης (Supabase): +latency σε ΚΑΘΕ εντολή SQL / BEGIN / COMMIT.

    Ο scanner πληκτρολογεί ~18 barcode/s. Ελέγχεται πάντα ότι ΚΑΝΕΝΑ δεν χάνεται ούτε διπλασιάζεται.
    Το όριο των 10/s απαιτείται για ρεαλιστική καθυστέρηση (ίδια περιοχή: ≤30 ms)· για μεγαλύτερη
    καθυστέρηση η μέτρηση απλώς καταγράφεται.
    """
    with app_server(tmp_path, latency_ms=latency_ms) as srv:
        typing, total, resends = _rate_run(browser, srv, synthetic_master, 150)
    rate = 150 / total
    print(f"\n[μέτρηση, +{latency_ms}ms ανά εντολή SQL] 150 barcode: πληκτρολόγηση {typing:.2f}s, "
          f"όλα επιβεβαιωμένα σε {total:.2f}s = {rate:.1f} barcode/s (επαναποστολές: {resends})")
    if latency_ms <= 30:
        assert rate >= 10, rate


def test_feedback_colors_duplicates_unknown_ambiguous(browser, tmp_path, synthetic_master):
    with app_server(tmp_path) as srv:
        activate_workbook(synthetic_master)
        ctx, page = open_scanner(browser, srv.url)
        wedge(page, "VS11486")
        page.wait_for_selector(".whs-fb-ok", timeout=15000)
        assert "ΗΛΕΚΤΡΟΓΑΛΒΑΝΙΖΕ × 0.70 × 180ΧΡΟΛΟ / 412 - ΑΠΟΘΗΚΗ" in page.inner_text("[data-ref=fb-text]")
        wedge(page, "VS11486")
        page.wait_for_selector(".whs-fb-dup", timeout=15000)
        assert "ήδη σκαναριστεί" in page.inner_text("[data-ref=fb-text]")
        wedge(page, "NOPE-1")
        page.wait_for_selector(".whs-fb-err", timeout=15000)
        wedge(page, "EX434-1")
        page.wait_for_function("document.querySelector('[data-ref=fb-text]').textContent.includes('Αμφίσημο')", timeout=15000)
        assert page.locator(".whs-list li").count() == 3       # το διπλό δεν προστέθηκε ξανά
        totals = backend.dashboard(login("secretary"))["totals"]
        assert totals["total_kilos"] == "412" and totals["duplicate_scans"] == 1
        ctx.close()


def test_offline_queue_and_server_restart(browser, tmp_path, synthetic_master):
    with app_server(tmp_path) as srv:
        activate_workbook(synthetic_master)
        ctx, page = open_scanner(browser, srv.url)
        for c in ("TST0001", "TST0002", "TST0003"):
            wedge(page, c)
        page.wait_for_function("window.__whScannerState.pending.size === 0", timeout=20000)
        srv.stop()                                   # ο server "πέφτει" (π.χ. redeploy / διακοπή δικτύου)
        codes = [f"TST{i:04d}" for i in range(10, 30)]
        for c in codes:
            wedge(page, c)
        page.wait_for_function("window.__whScannerState.pending.size === 20")
        page.wait_for_function("document.querySelector('[data-ref=c-pending]').textContent === '20'")
        page.wait_for_function("document.querySelector('[data-ref=conn]').textContent.includes('ΧΩΡΙΣ ΣΥΝΔΕΣΗ')",
                               timeout=20000)
        assert page.locator(".whs-list li.s-pending").count() == 20
        assert idb_count(page) == 20                 # φυλάσσονται στο IndexedDB
        assert history_count() == 3
        srv.start()                                  # επανέρχεται (ίδια βάση/storage)
        page.wait_for_function("window.__whScannerState.pending.size === 0", timeout=90000)
        hist = backend.history_rows(login("secretary"))
        assert len(hist) == 23 and len({h["event_id"] for h in hist}) == 23
        assert [i["barcode"] for i in backend.dashboard(login("secretary"))["items"]][-20:] == codes
        ctx.close()


def test_reload_keeps_pending_and_login(browser, tmp_path, synthetic_master):
    with app_server(tmp_path) as srv:
        activate_workbook(synthetic_master)
        ctx, page = open_scanner(browser, srv.url)
        page.evaluate("window.__whScannerState.setTrigger = () => {}")   # τίποτα δεν φτάνει στον server
        for c in ("TST0001", "TST0002", "TST0003", "TST0004", "TST0005"):
            wedge(page, c)
        page.wait_for_function("window.__whScannerState.pending.size === 5")
        page.wait_for_function("document.querySelector('[data-ref=c-pending]').textContent === '5'")
        assert history_count() == 0
        page.reload()                                # ανανέωση σελίδας με 5 εκκρεμή
        page.wait_for_selector(".whs-root", timeout=30000)          # χωρίς νέο login ("θυμήσου τη συσκευή")
        assert page.locator('input[aria-label="Κωδικός"]').count() == 0
        page.wait_for_function("window.__whScannerState.pending.size === 0", timeout=30000)
        assert history_count() == 5 and idb_count(page) == 0
        ctx.close()


def test_lost_and_duplicated_acknowledgements(browser, tmp_path, synthetic_master):
    with app_server(tmp_path) as srv:
        activate_workbook(synthetic_master)
        ctx, page = open_scanner(browser, srv.url)
        # 1) κάθε batch φτάνει ΔΥΟ φορές στον server (διπλό rerun)
        page.evaluate("""() => { const S = window.__whScannerState; const orig = S.setTrigger;
            S.setTrigger = (n, v) => { orig(n, v); setTimeout(() => orig(n, v), 30); }; }""")
        for c in ("TST0001", "TST0002", "TST0003"):
            wedge(page, c)
        page.wait_for_function("window.__whScannerState.pending.size === 0", timeout=20000)
        # 2) το πρώτο batch χάνεται εντελώς -> επαναποστολή μετά από ~5s
        page.evaluate("""() => { const S = window.__whScannerState; const orig = S.__orig || S.setTrigger;
            let dropped = false; S.setTrigger = (n, v) => { if (!dropped) { dropped = true; return; } orig(n, v); }; }""")
        wedge(page, "TST0004")
        page.wait_for_function("window.__whScannerState.pending.size === 0", timeout=30000)
        hist = backend.history_rows(login("secretary"))
        assert sorted(h["barcode"] for h in hist) == ["TST0001", "TST0002", "TST0003", "TST0004"]
        assert {h["result"] for h in hist} == {"ok"}       # καμία ψευδής «διπλή» καταχώρηση
        page.wait_for_selector(".whs-fb-ok")
        ctx.close()


def test_two_computers_stay_in_sync(browser, tmp_path, synthetic_master):
    with app_server(tmp_path) as srv:
        activate_workbook(synthetic_master)
        sctx, scanner_page = open_scanner(browser, srv.url)
        dctx = browser.new_context(viewport={"width": 1500, "height": 1100})
        desk = dctx.new_page()
        st_login(desk, srv.url, "secretary", "Μαρία", PASSWORDS["secretary"])
        desk.wait_for_selector("text=Γραμματεία — Ζωντανή εικόνα", timeout=30000)
        wedge(scanner_page, "VS11486")
        wedge(scanner_page, "VS11487")
        t = time.perf_counter()
        desk.wait_for_selector("text=Γενικό σύνολο: 828 κιλά", timeout=15000)
        print(f"\n[μέτρηση] εμφάνιση στη γραμματεία σε {time.perf_counter() - t:.1f}s")
        desk.get_by_role("tab", name="Αναλυτική λίστα").click()
        desk.get_by_role("button", name="Μεταφορά στα Σβησμένα").first.click()
        desk.get_by_role("button", name="✔ Ναι, μεταφορά στα Σβησμένα").click()
        desk.wait_for_selector("text=μεταφέρθηκε στα Σβησμένα", timeout=60000)
        # η αποθήκη βλέπει την αλλαγή χωρίς καμία ενέργεια
        scanner_page.wait_for_function("document.querySelectorAll('.whs-list li').length === 1", timeout=15000)
        assert scanner_page.inner_text(".whs-list li .bc") == "VS11487"
        sctx.close()
        dctx.close()


def test_fullscreen_and_greek_layout(browser, tmp_path, synthetic_master):
    with app_server(tmp_path) as srv:
        activate_workbook(synthetic_master)
        ctx, page = open_scanner(browser, srv.url)
        page.click("[data-ref=btn-fs]")
        page.wait_for_function("document.querySelector('.whs-root').classList.contains('whs-kiosk')")
        page.wait_for_function("document.fullscreenElement === document.querySelector('.whs-root')", timeout=5000)
        assert not page.is_visible("[data-ref=btn-fs]")            # στο fullscreen: μόνο λίστα/μετρητές/σύνδεση
        assert page.is_visible("[data-ref=conn]") and page.is_visible("[data-ref=c-ok]") and page.is_visible("[data-ref=c-pending]")
        # ελληνική διάταξη: τα φυσικά πλήκτρα V,S δίνουν Ω,Σ
        page.evaluate("""() => {
          const keys = [['Ω','KeyV',true],['Σ','KeyS',true],['1','Digit1'],['1','Digit1'],['4','Digit4'],['8','Digit8'],['6','Digit6']];
          for (const [key, code, shift] of keys)
            document.dispatchEvent(new KeyboardEvent('keydown', {key, code, shiftKey: !!shift, bubbles: true, cancelable: true}));
          document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', bubbles: true, cancelable: true}));
        }""")
        page.wait_for_selector(".whs-fb-ok", timeout=15000)
        assert [i["barcode"] for i in backend.dashboard(login("secretary"))["items"]] == ["VS11486"]
        page.keyboard.press("Escape")
        page.wait_for_function("!document.fullscreenElement")
        page.wait_for_function("!document.querySelector('.whs-root').classList.contains('whs-kiosk')")
        ctx.close()


def test_admin_uploads_excel_from_settings(browser, tmp_path, synthetic_master):
    with app_server(tmp_path) as srv:
        ctx = browser.new_context(viewport={"width": 1500, "height": 1100}, accept_downloads=True)
        page = ctx.new_page()
        st_login(page, srv.url, "admin", "Admin", PASSWORDS["admin"])
        page.get_by_role("link", name="Ρυθμίσεις").click()
        page.wait_for_selector("text=Ανέβασμα νέου Excel", timeout=30000)
        page.set_input_files("input[type=file]", str(synthetic_master))
        page.get_by_role("button", name="Έλεγχος αρχείου").click()
        page.wait_for_selector("text=Το αρχείο πέρασε τον έλεγχο", timeout=60000)
        page.get_by_role("button", name="Ενεργοποίηση αρχείου").click()
        page.wait_for_selector("text=Ενεργοποιήθηκε η εισαγωγή", timeout=60000)
        imports = backend.imports(login("admin"))
        assert imports[0]["is_active"] == 1 and imports[0]["product_count"] == 309
        page.get_by_role("button", name="Προετοιμασία τρέχοντος ενημερωμένου Excel").click()
        with page.expect_download() as dl:
            page.get_by_role("button", name="⬇ Λήψη ενημερωμένου Excel").click()
        out = tmp_path / "dl.xlsx"
        dl.value.save_as(out)
        assert out.read_bytes() == synthetic_master.read_bytes()
        page.screenshot(path=str(tmp_path / "settings.png"), full_page=True)
        ctx.close()
