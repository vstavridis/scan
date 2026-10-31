"""Δοκιμές της οθόνης σάρωσης σε πραγματικό Chromium (Playwright).

Προσομοιώνουν keyboard-wedge scanner (πλήκτρα + Enter), διακοπή σύνδεσης, ανανέωση σελίδας
με εκκρεμή barcode (IndexedDB), χαμένη απάντηση server (idempotency) και ελληνική διάταξη.
"""
from __future__ import annotations

import os
import time

import httpx
import pytest

from tests.conftest import PASSWORDS
from tests.test_concurrency_restart import _activate, _token

playwright = pytest.importorskip("playwright.sync_api")
pytestmark = [pytest.mark.browser, pytest.mark.slow]


@pytest.fixture
def browser():
    with playwright.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # pragma: no cover
            path = os.environ.get("PLAYWRIGHT_CHROMIUM_PATH", "/opt/pw-browsers/chromium")
            if not os.path.exists(path):
                pytest.skip(f"Chromium δεν είναι διαθέσιμο: {exc}")
            b = p.chromium.launch(executable_path=path, args=["--no-sandbox"])
        yield b
        b.close()


def _open_scanner(browser, url):
    ctx = browser.new_context()
    page = ctx.new_page()
    page.goto(url + "/scanner/")
    page.fill("#login-name", "Αποθήκη PC1")
    page.fill("#login-password", PASSWORDS["scanner"])
    page.click("#login-form button[type=submit]")
    page.wait_for_selector("#login-overlay.hidden", state="attached")
    page.wait_for_function("document.getElementById('conn').textContent.includes('ΣΥΝΔΕΔΕΜΕΝΟ')")
    page.mouse.click(5, 300)  # όπως ο υπάλληλος: ένα κλικ στη σελίδα στην αρχή της βάρδιας
    return ctx, page


def _wedge(page, code: str) -> None:
    """Όπως ο scanner: πληκτρολογεί τους χαρακτήρες και στέλνει Enter."""
    page.keyboard.type(code)
    page.keyboard.press("Enter")


def _pending(page) -> int:
    return page.evaluate("window.__scanner.state.pending.size")


def _active(url) -> list[dict]:
    return httpx.get(url + "/api/dashboard", headers=_token(url, "secretary"), timeout=30).json()["items"]


def test_keyboard_wedge_speed_and_colors(browser, live_server, synthetic_master):
    _activate(live_server.url, synthetic_master)
    ctx, page = _open_scanner(browser, live_server.url)
    codes = [f"TST{i:04d}" for i in range(1, 151)]
    start = time.perf_counter()
    for c in codes:
        _wedge(page, c)
    typing = time.perf_counter() - start
    page.wait_for_function("window.__scanner.state.pending.size === 0", timeout=30000)
    total = time.perf_counter() - start
    rate = len(codes) / total
    print(f"\n150 barcodes: πληκτρολόγηση {typing:.2f}s, όλα επιβεβαιωμένα σε {total:.2f}s ({rate:.1f}/s)")
    assert rate >= 10, rate
    items = _active(live_server.url)
    assert [i["barcode"] for i in items] == codes  # όλα, με τη σωστή σειρά
    page.wait_for_function("document.querySelectorAll('#list li').length === 150")

    # χρώματα/μηνύματα: διπλό -> κίτρινο, άγνωστο -> κόκκινο, ok -> πράσινο
    _wedge(page, "TST0001")
    page.wait_for_selector("#feedback.fb-dup")
    assert "ήδη σκαναριστεί" in page.inner_text("#fb-text")
    _wedge(page, "NOPE-1")
    page.wait_for_selector("#feedback.fb-err")
    _wedge(page, "VS11486")
    page.wait_for_selector("#feedback.fb-ok")
    assert "ΗΛΕΚΤΡΟΓΑΛΒΑΝΙΖΕ × 0.70 × 180ΧΡΟΛΟ / 412 - ΑΠΟΘΗΚΗ" in page.inner_text("#fb-text")
    ctx.close()


def test_offline_queue_survives_reload(browser, live_server, synthetic_master):
    _activate(live_server.url, synthetic_master)
    ctx, page = _open_scanner(browser, live_server.url)
    # «πέφτει» ο server: όλα τα API calls αποτυγχάνουν, η σελίδα μένει ανοιχτή
    page.route("**/api/**", lambda route: route.abort())
    codes = [f"TST{i:04d}" for i in range(1, 21)]
    for c in codes:
        _wedge(page, c)
    page.wait_for_function("window.__scanner.state.pending.size === 20")
    page.wait_for_function("document.getElementById('c-pending').textContent === '20'")
    page.wait_for_function("document.getElementById('conn').textContent.includes('ΧΩΡΙΣ ΣΥΝΔΕΣΗ')")
    assert page.locator("#list li.s-pending").count() == 20
    assert _active(live_server.url) == []

    # ανανέωση σελίδας ενώ τα 20 είναι ακόμη εκκρεμή (τα στατικά αρχεία φορτώνουν, το API όχι)
    page.reload()
    page.wait_for_function("window.__scanner && window.__scanner.state.pending.size === 20")

    # επανέρχεται η σύνδεση -> αποστέλλονται και επιβεβαιώνονται όλα, με τη σειρά
    page.unroute("**/api/**")
    page.wait_for_function("window.__scanner.state.pending.size === 0", timeout=30000)
    assert [i["barcode"] for i in _active(live_server.url)] == codes
    count = page.evaluate("""() => new Promise(res => {
        const r = indexedDB.open('warehouse-scanner'); r.onsuccess = () => {
          const q = r.result.transaction('events').objectStore('events').count(); q.onsuccess = () => res(q.result); };
    })""")
    assert count == 0  # η τοπική ουρά άδειασε μόνο μετά την επιβεβαίωση
    ctx.close()


def test_lost_response_is_resent_without_duplicate(browser, live_server, synthetic_master):
    _activate(live_server.url, synthetic_master)
    ctx, page = _open_scanner(browser, live_server.url)
    state = {"dropped": 0}

    def drop_first_response(route):
        if state["dropped"] < 2:
            state["dropped"] += 1
            route.fetch()   # ο server ΕΠΕΞΕΡΓΑΖΕΤΑΙ και κάνει commit…
            route.abort()   # …αλλά η απάντηση χάνεται στο δίκτυο
        else:
            route.continue_()

    page.route("**/api/scans", drop_first_response)
    _wedge(page, "VS11486")
    page.wait_for_function("window.__scanner.state.pending.size === 0", timeout=30000)
    hist = httpx.get(live_server.url + "/api/history", headers=_token(live_server.url, "secretary")).json()
    assert len(hist) == 1 and hist[0]["result"] == "ok"   # μία μόνο καταχώρηση παρά τις επαναποστολές
    page.wait_for_selector("#feedback.fb-ok")             # και ο χρήστης βλέπει πράσινο, όχι «διπλό»
    ctx.close()


def test_greek_keyboard_layout_is_mapped(browser, live_server, synthetic_master):
    _activate(live_server.url, synthetic_master)
    ctx, page = _open_scanner(browser, live_server.url)
    # Με ελληνική διάταξη, το πλήκτρο V δίνει 'Ω' και το S δίνει 'Σ'.
    page.evaluate("""() => {
      const keys = [['Ω','KeyV',true],['Σ','KeyS',true],['1','Digit1'],['1','Digit1'],['4','Digit4'],['8','Digit8'],['6','Digit6'],
                    ['-','Minus'],[';','KeyQ',true]];
      for (const [key, code, shift] of keys) {
        document.dispatchEvent(new KeyboardEvent('keydown', {key, code, shiftKey: !!shift, bubbles: true, cancelable: true}));
      }
      document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', bubbles: true, cancelable: true}));
    }""")
    page.wait_for_function("window.__scanner.state.pending.size === 0")
    # το 'VS11486-Q' δεν υπάρχει στο Excel, αλλά καταγράφεται με σωστούς λατινικούς χαρακτήρες
    assert [i["barcode"] for i in _active(live_server.url)] == ["VS11486-Q"]
    ctx.close()


def test_fullscreen_button_uses_fullscreen_api(browser, live_server, synthetic_master):
    _activate(live_server.url, synthetic_master)
    ctx, page = _open_scanner(browser, live_server.url)
    page.click("#btn-fs")
    page.wait_for_function("!!document.fullscreenElement", timeout=5000)
    # το layout αλλάζει στο event fullscreenchange, που φτάνει λίγο μετά
    page.wait_for_function("document.body.classList.contains('fs')", timeout=5000)
    assert not page.is_visible("#btn-fs")          # στο fullscreen κρύβονται τα κουμπιά
    assert page.is_visible("#conn") and page.is_visible("#c-ok")
    page.evaluate("document.exitFullscreen()")
    page.wait_for_function("!document.fullscreenElement")
    # μετά το κλικ στο κουμπί, το Enter του scanner δεν «πατάει» το κουμπί
    _wedge(page, "VS11486")
    page.wait_for_function("window.__scanner.state.pending.size === 0")
    assert not page.evaluate("!!document.fullscreenElement")
    ctx.close()
