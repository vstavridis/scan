/*
 * Οθόνη σάρωσης αποθήκης.
 *
 * Ροή κάθε barcode:
 *   πλήκτρα -> buffer -> τερματικός χαρακτήρας (Enter / Tab)
 *   -> νέο event με μοναδικό event_id + αύξοντα seq
 *   -> αποθήκευση στο IndexedDB (durability: strict)
 *   -> ασύγχρονη αποστολή σε batches προς /api/scans (με τη σειρά seq)
 *   -> ο server απαντά ΜΟΝΟ μετά από commit -> διαγραφή από IndexedDB -> πράσινο/κίτρινο/κόκκινο
 * Σε αποτυχία δικτύου: επαναπροσπάθεια με backoff· τα events μένουν "εκκρεμή" (πορτοκαλί).
 * Το API είναι idempotent ως προς το event_id, οπότε η επαναποστολή δεν δημιουργεί διπλές εγγραφές.
 */
'use strict';
(function () {
  // ------------------------------------------------------------------ utils
  const $ = (id) => document.getElementById(id);
  const LS = { token: 'wh.token', name: 'wh.name', device: 'wh.device', seq: 'wh.seq', sound: 'wh.sound' };
  function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* ignore */ } }
  function lsDel(k) { try { localStorage.removeItem(k); } catch (e) { /* ignore */ } }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }
  function uuid() {
    if (window.crypto && crypto.randomUUID && window.isSecureContext) return crypto.randomUUID();
    const b = crypto.getRandomValues(new Uint8Array(16)); // διαθέσιμο και σε http:// (LAN)
    b[6] = (b[6] & 0x0f) | 0x40; b[8] = (b[8] & 0x3f) | 0x80;
    const h = Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
    return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
  }
  function normBarcode(s) { return String(s || '').replace(/\s+/g, '').toUpperCase(); }

  // ------------------------------------------------------------------ state
  const state = {
    token: lsGet(LS.token),
    name: lsGet(LS.name) || '',
    deviceId: lsGet(LS.device) || null,
    cfg: { terminator: 'enter', key_mapping: 'physical', sound_default: true, inter_key_timeout_ms: 0, has_catalog: true },
    items: [],
    version: null,
    pending: new Map(),        // event_id -> event (δεν έχει επιβεβαιωθεί από τον server)
    results: new Map(),        // event_id -> αποτέλεσμα server (τελευταία ~100)
    online: null,              // null = άγνωστο, true/false
    authNeeded: false,
    buffer: '',
    lastKeyAt: 0,
    idbOk: true,
    idbError: null,
    sending: false,
    polling: false,
    backoff: 500,
    retryTimer: null,
    latestEventId: null,
    flash: new Map(),          // barcode_norm -> {cls, until}
    soundOn: true,
    lastSeq: 0,
  };
  if (!state.deviceId) { state.deviceId = uuid(); lsSet(LS.device, state.deviceId); }
  state.lastSeq = parseInt(lsGet(LS.seq) || '0', 10) || 0;
  const savedSound = lsGet(LS.sound);

  function nextSeq() {
    state.lastSeq = Math.max(state.lastSeq + 1, Date.now() * 100);
    lsSet(LS.seq, String(state.lastSeq));
    return state.lastSeq;
  }

  // ------------------------------------------------------------------ IndexedDB queue
  const IDB = {
    db: null,
    open() {
      return new Promise((resolve, reject) => {
        if (!('indexedDB' in window) || !window.indexedDB) { reject(new Error('IndexedDB δεν υποστηρίζεται')); return; }
        let req;
        try { req = indexedDB.open('warehouse-scanner', 1); } catch (e) { reject(e); return; }
        req.onupgradeneeded = () => {
          const db = req.result;
          if (!db.objectStoreNames.contains('events')) {
            const st = db.createObjectStore('events', { keyPath: 'event_id' });
            st.createIndex('seq', 'seq');
          }
        };
        req.onsuccess = () => { IDB.db = req.result; resolve(IDB.db); };
        req.onerror = () => reject(req.error || new Error('IndexedDB error'));
        req.onblocked = () => reject(new Error('IndexedDB blocked'));
      });
    },
    tx(mode) {
      try { return IDB.db.transaction('events', mode, { durability: 'strict' }); } catch (e) { return IDB.db.transaction('events', mode); }
    },
    put(rec) {
      return new Promise((resolve, reject) => {
        const tx = IDB.tx('readwrite');
        tx.objectStore('events').put(rec);
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error);
        tx.onabort = () => reject(tx.error || new Error('abort'));
      });
    },
    del(ids) {
      return new Promise((resolve, reject) => {
        const tx = IDB.tx('readwrite');
        const st = tx.objectStore('events');
        ids.forEach((id) => st.delete(id));
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error);
      });
    },
    all() {
      return new Promise((resolve, reject) => {
        const tx = IDB.db.transaction('events', 'readonly');
        const req = tx.objectStore('events').getAll();
        req.onsuccess = () => resolve(req.result || []);
        req.onerror = () => reject(req.error);
      });
    },
  };

  function storageFailed(err) {
    if (state.idbOk) console.error('IndexedDB', err);
    state.idbOk = false;
    state.idbError = String((err && err.message) || err);
    scheduleRender();
  }

  // ------------------------------------------------------------------ sound (Web Audio)
  let actx = null;
  let nextSoundAt = 0;
  function audio() {
    if (!actx) {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return null;
      actx = new AC();
    }
    if (actx.state === 'suspended') actx.resume().catch(() => {});
    return actx;
  }
  function tone(c, freq, start, dur, type, gain) {
    const o = c.createOscillator();
    const g = c.createGain();
    o.type = type; o.frequency.value = freq;
    g.gain.setValueAtTime(0.0001, start);
    g.gain.exponentialRampToValueAtTime(gain, start + 0.01);
    g.gain.setValueAtTime(gain, start + Math.max(0.01, dur - 0.03));
    g.gain.exponentialRampToValueAtTime(0.0001, start + dur);
    o.connect(g).connect(c.destination);
    o.start(start); o.stop(start + dur + 0.02);
  }
  // Διακριτοί ήχοι: OK = σύντομο ψηλό "μπιπ", ΔΙΠΛΟ = δύο μεσαία, ΣΦΑΛΜΑ = χαμηλό βουητό.
  const SOUNDS = {
    ok: [[1400, 0, 0.08, 'sine', 0.35]],
    dup: [[740, 0, 0.11, 'triangle', 0.45], [740, 0.16, 0.11, 'triangle', 0.45]],
    err: [[180, 0, 0.5, 'square', 0.22], [140, 0.05, 0.45, 'square', 0.12]],
  };
  function play(kind, force) {
    if (!state.soundOn && !force) return;
    const c = audio();
    if (!c || c.state !== 'running') return;
    const now = c.currentTime;
    if (nextSoundAt < now) nextSoundAt = now;
    // σε πολύ γρήγορη σάρωση δεν συσσωρεύουμε ήχους επιτυχίας· τα σφάλματα παίζουν πάντα
    if (kind === 'ok' && nextSoundAt - now > 0.6) return;
    let end = 0;
    for (const [f, off, d, t, g] of SOUNDS[kind]) { tone(c, f, nextSoundAt + off, d, t, g); end = Math.max(end, off + d); }
    nextSoundAt += end + 0.03;
  }

  // ------------------------------------------------------------------ HTTP
  function authHeaders() {
    const h = { 'Content-Type': 'application/json' };
    if (state.token) h.Authorization = 'Bearer ' + state.token;
    return h;
  }
  async function fetchT(url, opts, ms) {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), ms || 8000);
    try { return await fetch(url, Object.assign({ cache: 'no-store', signal: ctl.signal }, opts || {})); }
    finally { clearTimeout(t); }
  }
  function setOnline(v) {
    if (state.online !== v) { state.online = v; scheduleRender(); }
  }
  function needAuth() {
    state.authNeeded = true;
    showLogin(true);
    scheduleRender();
  }

  // ------------------------------------------------------------------ capture
  function captureBarcode(raw) {
    const code = raw.trim();
    if (!code) return;
    const ev = { event_id: uuid(), barcode: code, client_ts: new Date().toISOString(), seq: nextSeq(), ready: false };
    state.pending.set(ev.event_id, ev);
    state.latestEventId = ev.event_id;
    scheduleRender();
    const rec = { event_id: ev.event_id, barcode: ev.barcode, client_ts: ev.client_ts, seq: ev.seq };
    if (state.idbOk && IDB.db) {
      IDB.put(rec).then(() => { ev.ready = true; kick(); })
        .catch((e) => { storageFailed(e); ev.ready = true; ev.memoryOnly = true; kick(); });
    } else {
      ev.ready = true; ev.memoryOnly = true; kick();
    }
  }

  // ------------------------------------------------------------------ sender
  function kick() { if (!state.sending) sendLoop(); }

  function scheduleRetry() {
    clearTimeout(state.retryTimer);
    state.retryTimer = setTimeout(() => { state.retryTimer = null; sendLoop(); }, state.backoff);
    state.backoff = Math.min(state.backoff * 2, 10000);
  }

  async function sendLoop() {
    if (state.sending) return;
    if (!state.token || state.authNeeded) return;
    state.sending = true;
    try {
      for (;;) {
        const all = Array.from(state.pending.values()).sort((a, b) => a.seq - b.seq);
        // Διατήρηση σειράς: στέλνουμε μόνο ό,τι προηγείται του πρώτου μη-αποθηκευμένου event.
        const batch = [];
        for (const ev of all) { if (!ev.ready) break; batch.push(ev); if (batch.length >= 100) break; }
        if (!batch.length) break;
        const body = JSON.stringify({
          device_id: state.name || state.deviceId,
          events: batch.map((e) => ({ event_id: e.event_id, barcode: e.barcode, client_ts: e.client_ts, seq: e.seq })),
        });
        const resp = await fetchT('/api/scans', { method: 'POST', headers: authHeaders(), body }, 10000);
        if (resp.status === 401 || resp.status === 403) { needAuth(); throw new Error('auth'); }
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const data = await resp.json();
        setOnline(true);
        state.backoff = 500;
        const acked = [];
        for (const r of data.results) {
          if (state.pending.delete(r.event_id)) acked.push(r.event_id);
          onResult(r);
        }
        if (acked.length && state.idbOk && IDB.db) IDB.del(acked).catch(storageFailed);
        pollActive(true);
        scheduleRender();
      }
    } catch (e) {
      if (String(e && e.message) !== 'auth') { setOnline(false); scheduleRetry(); }
    } finally {
      state.sending = false;
      scheduleRender();
    }
  }

  const RESULT_CLASS = { ok: 'ok', duplicate: 'dup', unknown: 'err', ambiguous: 'err', no_catalog: 'err', invalid: 'err' };
  function onResult(r) {
    state.results.set(r.event_id, r);
    if (state.results.size > 120) state.results.delete(state.results.keys().next().value);
    const cls = RESULT_CLASS[r.result] || 'err';
    play(cls);
    const key = normBarcode(r.barcode);
    if (cls === 'dup') state.flash.set(key, { cls: 'flash-dup', until: Date.now() + 2500 });
    else if (cls === 'ok') state.flash.set(key, { cls: 'flash-ok', until: Date.now() + 900 });
  }

  // ------------------------------------------------------------------ polling ενεργής λίστας
  async function pollActive(force) {
    if (!state.token || state.authNeeded) return;
    if (state.polling && !force) return;
    state.polling = true;
    try {
      const q = state.version != null && !force ? '?since=' + state.version : '';
      const resp = await fetchT('/api/active' + q, { headers: authHeaders() }, 5000);
      if (resp.status === 401 || resp.status === 403) { needAuth(); return; }
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      const d = await resp.json();
      setOnline(true);
      if (d.changed !== false) {
        state.items = d.items;
        state.version = d.version;
        state.cfg.has_catalog = d.has_catalog;
        scheduleRender();
      }
      if (state.pending.size && !state.sending) kick();
    } catch (e) {
      setOnline(false);
    } finally {
      state.polling = false;
    }
  }

  async function loadConfig() {
    try {
      const resp = await fetchT('/api/config', {}, 5000);
      if (resp.ok) {
        const c = await resp.json();
        Object.assign(state.cfg, c);
        if (savedSound === null) state.soundOn = !!c.sound_default;
        scheduleRender();
      }
    } catch (e) { /* offline */ }
  }

  // ------------------------------------------------------------------ πληκτρολόγιο
  // Χαρτογράφηση φυσικών πλήκτρων (US layout), ώστε το barcode να διαβάζεται σωστά
  // ακόμη κι αν στα Windows είναι ενεργή η ελληνική διάταξη πληκτρολογίου.
  const SHIFT_DIGITS = { '1': '!', '2': '@', '3': '#', '4': '$', '5': '%', '6': '^', '7': '&', '8': '*', '9': '(', '0': ')' };
  const PUNCT = {
    Minus: ['-', '_'], Equal: ['=', '+'], Period: ['.', '>'], Comma: [',', '<'], Slash: ['/', '?'],
    Semicolon: [';', ':'], Quote: ["'", '"'], BracketLeft: ['[', '{'], BracketRight: [']', '}'],
    Backslash: ['\\', '|'], Backquote: ['`', '~'], Space: [' ', ' '],
    NumpadDivide: ['/', '/'], NumpadMultiply: ['*', '*'], NumpadSubtract: ['-', '-'], NumpadAdd: ['+', '+'], NumpadDecimal: ['.', '.'],
  };
  function mapKey(e) {
    const k = e.key;
    if (state.cfg.key_mapping === 'physical' && e.code) {
      let m = /^Key([A-Z])$/.exec(e.code);
      if (m) {
        // Λατινικό γράμμα από τη διάταξη: το κρατάμε ως έχει (σωστή πεζότητα).
        if (k && k.length === 1 && /[A-Za-z]/.test(k)) return k;
        // Άλλη διάταξη (π.χ. ελληνική: V -> Ω, Q -> ;): γράμμα από το φυσικό πλήκτρο.
        let upper;
        if (k && k.length === 1 && k.toUpperCase() !== k.toLowerCase()) upper = k === k.toUpperCase();
        else upper = e.shiftKey !== !!(e.getModifierState && e.getModifierState('CapsLock'));
        return upper ? m[1] : m[1].toLowerCase();
      }
      if (k && k.length === 1 && k.charCodeAt(0) < 128) return k;
      m = /^Digit(\d)$/.exec(e.code);
      if (m) return e.shiftKey ? SHIFT_DIGITS[m[1]] : m[1];
      m = /^Numpad(\d)$/.exec(e.code);
      if (m) return m[1];
      if (PUNCT[e.code]) return PUNCT[e.code][e.shiftKey ? 1 : 0];
    }
    return k && k.length === 1 ? k : null;
  }

  function onKeyDown(e) {
    if (!$('login-overlay').classList.contains('hidden')) return; // πληκτρολόγηση στη φόρμα σύνδεσης
    audio(); // το πρώτο πλήκτρο ξεκλειδώνει τον ήχο (autoplay policy)
    if (e.key === 'F9') { e.preventDefault(); toggleFullscreen(); return; }
    if (e.ctrlKey || e.altKey || e.metaKey) return;
    const isTerm = e.key === 'Enter' || e.code === 'NumpadEnter' || (e.key === 'Tab' && state.cfg.terminator === 'enter_tab');
    if (isTerm) {
      e.preventDefault();
      const code = state.buffer;
      state.buffer = '';
      captureBarcode(code);
      renderBuffer();
      return;
    }
    if (e.key === 'Tab') { e.preventDefault(); return; }
    if (e.key === 'Backspace') { e.preventDefault(); state.buffer = state.buffer.slice(0, -1); renderBuffer(); return; }
    if (e.key === 'Escape') { state.buffer = ''; renderBuffer(); return; }
    const ch = mapKey(e);
    if (ch === null) return;
    e.preventDefault();
    const now = performance.now();
    const gap = state.cfg.inter_key_timeout_ms;
    if (gap > 0 && state.buffer && now - state.lastKeyAt > gap) state.buffer = '';
    state.lastKeyAt = now;
    state.buffer += ch;
    renderBuffer();
  }

  // ------------------------------------------------------------------ rendering (ομαδικό, 1 φορά ανά frame)
  let renderQueued = false;
  function scheduleRender() {
    if (renderQueued) return;
    renderQueued = true;
    requestAnimationFrame(() => { renderQueued = false; render(); });
  }
  function renderBuffer() { $('buffer').textContent = state.buffer; }

  let lastListKey = '';
  function render() {
    // σύνδεση
    const conn = $('conn');
    if (!state.token || state.authNeeded) { conn.className = 'pill pill-auth'; conn.textContent = 'ΑΠΑΙΤΕΙΤΑΙ ΣΥΝΔΕΣΗ'; }
    else if (state.online === true) { conn.className = 'pill pill-ok'; conn.textContent = '● ΣΥΝΔΕΔΕΜΕΝΟ'; }
    else if (state.online === false) { conn.className = 'pill pill-off'; conn.textContent = '✕ ΧΩΡΙΣ ΣΥΝΔΕΣΗ'; }
    else { conn.className = 'pill pill-wait'; conn.textContent = 'Σύνδεση…'; }

    const okCount = state.items.filter((i) => i.state === 'ok').length;
    const problem = state.items.length - okCount;
    $('c-ok').textContent = okCount;
    $('c-pending').textContent = state.pending.size;
    $('c-pending').classList.toggle('has', state.pending.size > 0);
    $('c-problem').textContent = problem;

    // προειδοποιήσεις
    const warns = [];
    if (!state.cfg.has_catalog) warns.push(['', 'ΔΕΝ ΥΠΑΡΧΕΙ ΕΝΕΡΓΟ EXCEL — οι σαρώσεις καταγράφονται αλλά ΔΕΝ είναι έγκυρες.']);
    if (!state.idbOk) warns.push(['', 'Η τοπική αποθήκευση του browser δεν λειτουργεί (' + esc(state.idbError || '') + '). Σε διακοπή σύνδεσης ΜΗΝ ανανεώσετε/κλείσετε τη σελίδα.']);
    if (state.online === false && state.pending.size) warns.push(['warn', `Χωρίς σύνδεση με τον server: ${state.pending.size} barcode φυλάσσονται τοπικά και θα σταλούν αυτόματα.`]);
    if (actx && actx.state !== 'running' && state.soundOn) warns.push(['warn', 'Ο ήχος θα ενεργοποιηθεί με το πρώτο πλήκτρο ή κλικ.']);
    $('warnings').innerHTML = warns.map(([c, t]) => `<div class="${c}">${t}</div>`).join('');

    // banner τελευταίου scan
    const fb = $('feedback');
    const latestId = state.latestEventId;
    if (latestId) {
      const r = state.results.get(latestId);
      const p = state.pending.get(latestId);
      if (r) {
        fb.className = 'fb fb-' + (RESULT_CLASS[r.result] || 'err');
        $('fb-barcode').textContent = r.barcode;
        $('fb-text').textContent = r.result === 'ok' ? (r.display || r.message) : (r.message + (r.display ? ' — ' + r.display : ''));
      } else if (p) {
        fb.className = 'fb fb-pending';
        $('fb-barcode').textContent = p.barcode;
        $('fb-text').textContent = state.online === false ? 'Εκκρεμεί — αποθηκεύτηκε τοπικά, αναμονή σύνδεσης' : 'Αναμονή επιβεβαίωσης από τον server…';
      }
    }

    // λίστα
    const now = Date.now();
    for (const [k, v] of state.flash) if (v.until < now) state.flash.delete(k);
    const latestBarcode = (() => {
      if (!latestId) return null;
      const r = state.results.get(latestId) || state.pending.get(latestId);
      return r ? normBarcode(r.barcode) : null;
    })();
    const pend = Array.from(state.pending.values()).sort((a, b) => a.seq - b.seq);
    const rows = [];
    for (const it of state.items) {
      const k = normBarcode(it.barcode);
      const fl = state.flash.get(k);
      const desc = it.state === 'ok' ? it.display : it.state === 'ambiguous' ? 'ΑΜΦΙΣΗΜΟ — έλεγχος από γραμματεία' : 'ΔΕΝ ΒΡΕΘΗΚΕ στο Excel';
      const cls = ['s-' + it.state, k === latestBarcode && !pend.length ? 'latest' : '', fl ? fl.cls : ''].join(' ');
      rows.push(`<li class="${cls}"><span class="bc">${esc(it.barcode)}</span><span class="desc">${esc(desc || '')}${it.dup ? ` · σκαναρίστηκε ${it.dup + 1}×` : ''}</span></li>`);
    }
    pend.forEach((p, i) => {
      const cls = 's-pending' + (i === pend.length - 1 ? ' latest' : '');
      rows.push(`<li class="${cls}"><span class="bc">${esc(p.barcode)}</span><span class="desc">ΕΚΚΡΕΜΕΙ — αναμονή επιβεβαίωσης</span></li>`);
    });
    const html = rows.join('');
    if (html !== lastListKey) {
      lastListKey = html;
      $('list').innerHTML = html;
      $('empty').classList.toggle('hidden', rows.length > 0);
      const wrap = $('list-wrap');
      const last = $('list').lastElementChild;
      if (last) last.scrollIntoView({ block: 'end' });
      else wrap.scrollTop = 0;
    }
    $('btn-sound').textContent = 'Ήχος: ' + (state.soundOn ? 'ΝΑΙ' : 'ΟΧΙ');
    $('btn-sound').setAttribute('aria-pressed', String(state.soundOn));
    $('device').textContent = state.name ? 'Συσκευή: ' + state.name : '';
    if (state.flash.size) setTimeout(scheduleRender, 300);
  }

  // ------------------------------------------------------------------ fullscreen
  async function toggleFullscreen() {
    try {
      if (!document.fullscreenElement) await document.documentElement.requestFullscreen({ navigationUI: 'hide' });
      else await document.exitFullscreen();
    } catch (e) {
      alert('Ο browser δεν επέτρεψε την πλήρη οθόνη: ' + e.message);
    }
  }
  document.addEventListener('fullscreenchange', () => {
    document.body.classList.toggle('fs', !!document.fullscreenElement);
    scheduleRender();
  });

  // ------------------------------------------------------------------ login
  function showLogin(v) {
    $('login-overlay').classList.toggle('hidden', !v);
    if (v) { $('login-name').value = state.name || ''; setTimeout(() => $('login-password').focus(), 50); }
  }
  $('login-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    $('login-error').textContent = '';
    try {
      const resp = await fetchT('/api/auth/login', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ role: $('login-role').value, password: $('login-password').value, name: $('login-name').value }),
      }, 8000);
      const d = await resp.json().catch(() => ({}));
      if (!resp.ok) { $('login-error').textContent = d.detail || 'Αποτυχία σύνδεσης'; return; }
      state.token = d.token; state.name = d.name; state.authNeeded = false;
      lsSet(LS.token, d.token); lsSet(LS.name, d.name);
      $('login-password').value = '';
      showLogin(false);
      document.activeElement && document.activeElement.blur();
      setOnline(true);
      pollActive(true);
      kick();
    } catch (err) {
      $('login-error').textContent = 'Δεν υπάρχει σύνδεση με τον server.';
    }
  });

  // ------------------------------------------------------------------ εστίαση
  function updateFocus() {
    const loginOpen = !$('login-overlay').classList.contains('hidden');
    $('focus-overlay').classList.toggle('hidden', loginOpen || document.hasFocus());
  }
  window.addEventListener('blur', updateFocus);
  window.addEventListener('focus', () => { updateFocus(); if (document.activeElement && document.activeElement !== document.body) document.activeElement.blur(); });
  $('focus-overlay').addEventListener('click', () => { window.focus(); updateFocus(); });
  setInterval(updateFocus, 1000);

  // ------------------------------------------------------------------ κουμπιά
  function blurAfter(fn) { return (e) => { fn(e); e.currentTarget.blur(); }; }
  $('btn-fs').addEventListener('click', blurAfter(toggleFullscreen));
  $('btn-sound-test').addEventListener('click', blurAfter(() => {
    audio();
    setTimeout(() => { play('ok', true); setTimeout(() => play('dup', true), 450); setTimeout(() => play('err', true), 1000); }, 60);
  }));
  $('btn-sound').addEventListener('click', blurAfter(() => {
    state.soundOn = !state.soundOn; lsSet(LS.sound, state.soundOn ? '1' : '0'); audio(); scheduleRender();
  }));
  $('btn-logout').addEventListener('click', blurAfter(() => {
    if (!confirm('Αποσύνδεση αυτής της συσκευής; Τα εκκρεμή barcode παραμένουν αποθηκευμένα τοπικά.')) return;
    state.token = null; lsDel(LS.token); showLogin(true); scheduleRender();
  }));
  document.addEventListener('click', () => audio(), true);
  document.addEventListener('keydown', onKeyDown, true);
  window.addEventListener('online', () => { state.backoff = 500; kick(); pollActive(true); });
  window.addEventListener('beforeunload', (e) => {
    if (state.pending.size && (!state.idbOk || Array.from(state.pending.values()).some((p) => p.memoryOnly || !p.ready))) {
      e.preventDefault(); e.returnValue = '';
    }
  });

  // ------------------------------------------------------------------ εκκίνηση
  async function init() {
    if (savedSound !== null) state.soundOn = savedSound === '1';
    try {
      await IDB.open();
      const stored = await IDB.all();
      stored.sort((a, b) => a.seq - b.seq);
      for (const rec of stored) {
        state.pending.set(rec.event_id, Object.assign({}, rec, { ready: true }));
        state.lastSeq = Math.max(state.lastSeq, rec.seq || 0);
      }
      if (navigator.storage && navigator.storage.persist) navigator.storage.persist().catch(() => {});
    } catch (e) {
      storageFailed(e);
    }
    await loadConfig();
    if (!state.token) showLogin(true);
    scheduleRender();
    pollActive(true);
    kick();
    setInterval(() => pollActive(false), 1000);
    setInterval(() => { if (state.pending.size && !state.sending && !state.retryTimer) kick(); }, 3000);
    setInterval(loadConfig, 30000);
  }
  init();

  // για αυτοματοποιημένες δοκιμές / simulator μέσα στον browser
  window.__scanner = { state, captureBarcode };
})();
