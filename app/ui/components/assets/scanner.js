/*
 * Οθόνη σάρωσης αποθήκης — Streamlit Custom Component v2 (χωρίς iframe).
 *
 * Ροή κάθε barcode:
 *   πλήκτρα -> buffer -> Enter (ή Tab) -> event { event_id (UUID), barcode, client_ts, seq }
 *   -> IndexedDB (durability: strict) ΠΡΙΝ από οποιαδήποτε αποστολή
 *   -> setTriggerValue('batch', {...}) : batch προς την Python (ένα rerun για ΠΟΛΛΑ barcode)
 *   -> Python: INSERT στη PostgreSQL + COMMIT -> τα αποτελέσματα επιστρέφουν στο `data.acks`
 *   -> μόνο τότε: διαγραφή από IndexedDB + χρώμα/ήχος.
 * Αν δεν έρθει επιβεβαίωση (χαμένο rerun, διακοπή δικτύου, restart), το batch ξαναστέλνεται.
 * Η βάση έχει UNIQUE(event_id): η επαναποστολή δεν δημιουργεί διπλές εγγραφές.
 *
 * Η Python ξανακαλεί αυτή τη συνάρτηση σε κάθε rerun με νέα `data`· όλη η κατάσταση ζει
 * στο window.__whScanner ώστε να επιβιώνει από τα reruns.
 */
const RESEND_MS = 5000;        // επαναποστολή αν δεν έρθει επιβεβαίωση
const MAX_BATCH = 100;

const S = window.__whScanner || (window.__whScanner = {
  inited: false, root: null, refs: {}, setTrigger: null,
  cfg: { terminator: 'enter', key_mapping: 'physical', sound_default: true, inter_key_timeout_ms: 0 },
  items: [], hasCatalog: true, user: '', error: null,
  pending: new Map(), results: new Map(), handledAcks: new Set(),
  inflight: null, backoff: 1000, retryAt: 0,
  lastDataAt: 0, lastTick: null, refreshMs: 2000,
  buffer: '', lastKeyAt: 0, latestEventId: null, flash: new Map(),
  idbOk: true, idbError: null, db: null, soundOn: true, soundSet: false, lastSeq: 0,
  kiosk: false, renderQueued: false, lastListHtml: '',
});

// ------------------------------------------------------------------ utils
function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* ignore */ } }
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function uuid() {
  if (window.crypto && crypto.randomUUID) { try { return crypto.randomUUID(); } catch (e) { /* insecure context */ } }
  const b = crypto.getRandomValues(new Uint8Array(16));
  b[6] = (b[6] & 0x0f) | 0x40; b[8] = (b[8] & 0x3f) | 0x80;
  const h = Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}
function normBarcode(s) { return String(s || '').replace(/\s+/g, '').toUpperCase(); }
function nextSeq() {
  S.lastSeq = Math.max(S.lastSeq + 1, Date.now() * 100);
  lsSet('wh.seq', String(S.lastSeq));
  return S.lastSeq;
}

// ------------------------------------------------------------------ IndexedDB
const IDB = {
  open() {
    return new Promise((resolve, reject) => {
      if (!window.indexedDB) { reject(new Error('IndexedDB δεν υποστηρίζεται')); return; }
      let req;
      try { req = indexedDB.open('warehouse-scanner', 1); } catch (e) { reject(e); return; }
      req.onupgradeneeded = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains('events')) db.createObjectStore('events', { keyPath: 'event_id' }).createIndex('seq', 'seq');
      };
      req.onsuccess = () => { S.db = req.result; resolve(S.db); };
      req.onerror = () => reject(req.error || new Error('IndexedDB error'));
      req.onblocked = () => reject(new Error('IndexedDB blocked'));
    });
  },
  tx(mode) {
    try { return S.db.transaction('events', mode, { durability: 'strict' }); } catch (e) { return S.db.transaction('events', mode); }
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
      const req = S.db.transaction('events', 'readonly').objectStore('events').getAll();
      req.onsuccess = () => resolve(req.result || []);
      req.onerror = () => reject(req.error);
    });
  },
};
function storageFailed(err) {
  if (S.idbOk) console.error('IndexedDB', err);
  S.idbOk = false;
  S.idbError = String((err && err.message) || err);
  scheduleRender();
}

// ------------------------------------------------------------------ ήχοι (Web Audio)
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
// OK = σύντομο ψηλό "μπιπ", ΔΙΠΛΟ = δύο μεσαία, ΣΦΑΛΜΑ = χαμηλό βουητό
const SOUNDS = {
  ok: [[1400, 0, 0.08, 'sine', 0.35]],
  dup: [[740, 0, 0.11, 'triangle', 0.45], [740, 0.16, 0.11, 'triangle', 0.45]],
  err: [[180, 0, 0.5, 'square', 0.22], [140, 0.05, 0.45, 'square', 0.12]],
};
function play(kind, force) {
  if (!S.soundOn && !force) return;
  const c = audio();
  if (!c || c.state !== 'running' || !SOUNDS[kind]) return;
  const now = c.currentTime;
  if (nextSoundAt < now) nextSoundAt = now;
  if (kind === 'ok' && nextSoundAt - now > 0.6) return; // πολύ γρήγορη σάρωση: όχι συσσώρευση "μπιπ"
  let end = 0;
  for (const [f, off, d, t, g] of SOUNDS[kind]) { tone(c, f, nextSoundAt + off, d, t, g); end = Math.max(end, off + d); }
  nextSoundAt += end + 0.03;
}

// ------------------------------------------------------------------ λήψη barcode
function captureBarcode(raw) {
  const code = raw.trim();
  if (!code) return;
  const ev = { event_id: uuid(), barcode: code, client_ts: new Date().toISOString(), seq: nextSeq(), ready: false };
  S.pending.set(ev.event_id, ev);
  S.latestEventId = ev.event_id;
  scheduleRender();
  const rec = { event_id: ev.event_id, barcode: ev.barcode, client_ts: ev.client_ts, seq: ev.seq };
  if (S.idbOk && S.db) {
    IDB.put(rec).then(() => { ev.ready = true; maybeSend(); })
      .catch((e) => { storageFailed(e); ev.ready = true; ev.memoryOnly = true; maybeSend(); });
  } else { ev.ready = true; ev.memoryOnly = true; maybeSend(); }
}

// ------------------------------------------------------------------ αποστολή (batching)
function maybeSend(force) {
  if (!S.setTrigger) return;
  const now = Date.now();
  if (S.inflight && !force && now - S.inflight.sentAt < RESEND_MS) return;  // περιμένουμε επιβεβαίωση
  if (now < S.retryAt && !force) return;
  const all = Array.from(S.pending.values()).sort((a, b) => a.seq - b.seq);
  const batch = [];
  for (const ev of all) { if (!ev.ready) break; batch.push(ev); if (batch.length >= MAX_BATCH) break; } // σειρά σάρωσης
  if (!batch.length) { S.inflight = null; return; }
  if (S.inflight) S.resends = (S.resends || 0) + 1;
  S.inflight = { id: uuid(), ids: new Set(batch.map((e) => e.event_id)), sentAt: now };
  try {
    S.setTrigger('batch', {
      batch_id: S.inflight.id,
      events: batch.map((e) => ({ event_id: e.event_id, barcode: e.barcode, client_ts: e.client_ts, seq: e.seq })),
    });
  } catch (e) {
    console.error('setTriggerValue', e);
    S.inflight.sentAt = 0;
  }
  scheduleRender();
}

const RESULT_CLASS = { ok: 'ok', duplicate: 'dup', unknown: 'err', ambiguous: 'err', no_catalog: 'err', invalid: 'err' };

function onAck(r) {
  if (S.handledAcks.has(r.event_id)) return;
  S.handledAcks.add(r.event_id);
  if (S.handledAcks.size > 5000) S.handledAcks = new Set(Array.from(S.handledAcks).slice(-2500));
  const wasPending = S.pending.delete(r.event_id);
  S.results.set(r.event_id, r);
  if (S.results.size > 200) S.results.delete(S.results.keys().next().value);
  if (!wasPending) return; // επιβεβαίωση για event άλλης καρτέλας/παλιότερο: χωρίς ήχο
  const cls = RESULT_CLASS[r.result] || 'err';
  play(cls);
  const key = normBarcode(r.barcode);
  if (cls === 'dup') S.flash.set(key, { cls: 'flash-dup', until: Date.now() + 2500 });
  else if (cls === 'ok') S.flash.set(key, { cls: 'flash-ok', until: Date.now() + 900 });
}

function onData(data) {
  if (!data) return;
  const now = Date.now();
  if (data.tick !== S.lastTick) { S.lastTick = data.tick; S.lastDataAt = now; }
  if (data.cfg) {
    Object.assign(S.cfg, data.cfg);
    if (!S.soundSet) { const v = lsGet('wh.sound'); S.soundOn = v === null ? !!S.cfg.sound_default : v === '1'; S.soundSet = true; }
  }
  if (data.refresh_seconds) S.refreshMs = data.refresh_seconds * 1000;
  if (Array.isArray(data.items)) S.items = data.items;
  S.hasCatalog = data.has_catalog !== false;
  S.user = data.user || '';
  S.error = data.error || null;
  const acked = [];
  for (const r of (data.acks || [])) {
    if (S.pending.has(r.event_id)) acked.push(r.event_id);
    onAck(r);
  }
  if (acked.length && S.idbOk && S.db) IDB.del(acked).catch(storageFailed);
  if (S.inflight) {
    const still = Array.from(S.inflight.ids).some((id) => S.pending.has(id));
    if (!still) { S.inflight = null; S.backoff = 1000; S.retryAt = 0; }
  }
  if (S.error && S.inflight) {
    // ο server δεν μπόρεσε να αποθηκεύσει: επανάληψη με backoff
    S.inflight = null;
    S.retryAt = now + S.backoff;
    S.backoff = Math.min(S.backoff * 2, 15000);
  }
  scheduleRender();
  maybeSend();
}

// ------------------------------------------------------------------ πληκτρολόγιο
// Χαρτογράφηση φυσικών πλήκτρων (US): σωστό barcode ακόμη και με ελληνική διάταξη (V -> Ω).
const SHIFT_DIGITS = { 1: '!', 2: '@', 3: '#', 4: '$', 5: '%', 6: '^', 7: '&', 8: '*', 9: '(', 0: ')' };
const PUNCT = {
  Minus: ['-', '_'], Equal: ['=', '+'], Period: ['.', '>'], Comma: [',', '<'], Slash: ['/', '?'],
  Semicolon: [';', ':'], Quote: ["'", '"'], BracketLeft: ['[', '{'], BracketRight: [']', '}'],
  Backslash: ['\\', '|'], Backquote: ['`', '~'], Space: [' ', ' '],
  NumpadDivide: ['/', '/'], NumpadMultiply: ['*', '*'], NumpadSubtract: ['-', '-'], NumpadAdd: ['+', '+'], NumpadDecimal: ['.', '.'],
};
function mapKey(e) {
  const k = e.key;
  if (S.cfg.key_mapping === 'physical' && e.code) {
    let m = /^Key([A-Z])$/.exec(e.code);
    if (m) {
      if (k && k.length === 1 && /[A-Za-z]/.test(k)) return k;
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
function isForeignInput(t) {
  if (!t || !t.tagName) return false;
  if (S.root && S.root.contains(t)) return false;
  return t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName);
}
function onKeyDown(e) {
  if (!S.root || !S.root.isConnected) return;   // η σελίδα σάρωσης δεν είναι ανοιχτή
  if (isForeignInput(e.target)) return;          // πληκτρολόγηση σε άλλο πεδίο της εφαρμογής
  audio();                                       // το πρώτο πλήκτρο ξεκλειδώνει τον ήχο
  if (e.key === 'F9') { e.preventDefault(); toggleFullscreen(); return; }
  if (e.ctrlKey || e.altKey || e.metaKey) return;
  const isTerm = e.key === 'Enter' || e.code === 'NumpadEnter' || (e.key === 'Tab' && S.cfg.terminator === 'enter_tab');
  if (isTerm) {
    e.preventDefault(); e.stopPropagation();
    const code = S.buffer; S.buffer = '';
    captureBarcode(code); renderBuffer();
    return;
  }
  if (e.key === 'Tab') { e.preventDefault(); return; }
  if (e.key === 'Backspace') { e.preventDefault(); S.buffer = S.buffer.slice(0, -1); renderBuffer(); return; }
  if (e.key === 'Escape') {
    // Έξοδος από πλήρη οθόνη (ο browser συνήθως το κάνει ήδη μόνος του με Esc)
    if (S.kiosk || document.fullscreenElement) toggleFullscreen();
    S.buffer = ''; renderBuffer(); return;
  }
  const ch = mapKey(e);
  if (ch === null) return;
  e.preventDefault(); e.stopPropagation();
  const now = performance.now();
  const gap = S.cfg.inter_key_timeout_ms;
  if (gap > 0 && S.buffer && now - S.lastKeyAt > gap) S.buffer = '';
  S.lastKeyAt = now;
  S.buffer += ch;
  renderBuffer();
}

// ------------------------------------------------------------------ πλήρης οθόνη
function setKiosk(v) {
  S.kiosk = v;
  if (S.root) S.root.classList.toggle('whs-kiosk', v);
  scheduleRender();
}
async function toggleFullscreen() {
  if (document.fullscreenElement || S.kiosk) {
    if (document.fullscreenElement) { try { await document.exitFullscreen(); } catch (e) { /* ignore */ } }
    setKiosk(false);
    return;
  }
  setKiosk(true); // πάντα: η λίστα καλύπτει όλο το παράθυρο
  try {
    if (S.root.requestFullscreen) await S.root.requestFullscreen({ navigationUI: 'hide' });
    S.fsDenied = false;
  } catch (e) {
    // π.χ. όταν η εφαρμογή είναι μέσα σε iframe χωρίς allow="fullscreen": μένει η λειτουργία kiosk
    S.fsDenied = true;
  }
  scheduleRender();
}
document.addEventListener('fullscreenchange', () => {
  if (!document.fullscreenElement && S.kiosk) setKiosk(false);
  scheduleRender();
});

// ------------------------------------------------------------------ rendering (ομαδικό, μία φορά ανά frame)
function scheduleRender() {
  if (S.renderQueued) return;
  S.renderQueued = true;
  requestAnimationFrame(() => { S.renderQueued = false; render(); });
}
function renderBuffer() { if (S.refs.buffer) S.refs.buffer.textContent = S.buffer; }

function connState() {
  const stale = Date.now() - S.lastDataAt > Math.max(7000, 3 * S.refreshMs);
  if (!navigator.onLine || stale) return 'off';
  if (S.error) return 'err';
  return S.lastDataAt ? 'ok' : 'wait';
}

function render() {
  const R = S.refs;
  if (!S.root || !S.root.isConnected || !R.conn) return;
  const cs = connState();
  const CONN = {
    ok: ['whs-pill whs-pill-ok', '● ΣΥΝΔΕΔΕΜΕΝΟ'],
    off: ['whs-pill whs-pill-off', '✕ ΧΩΡΙΣ ΣΥΝΔΕΣΗ'],
    err: ['whs-pill whs-pill-off', '✕ ΣΦΑΛΜΑ ΒΑΣΗΣ'],
    wait: ['whs-pill whs-pill-wait', 'Σύνδεση…'],
  }[cs];
  R.conn.className = CONN[0]; R.conn.textContent = CONN[1];
  const okCount = S.items.filter((i) => i.state === 'ok').length;
  R['c-ok'].textContent = okCount;
  R['c-pending'].textContent = S.pending.size;
  R['c-pending'].classList.toggle('whs-has', S.pending.size > 0);
  R['c-problem'].textContent = S.items.length - okCount;

  const warns = [];
  if (!S.hasCatalog) warns.push(['', 'ΔΕΝ ΥΠΑΡΧΕΙ ΕΝΕΡΓΟ EXCEL — οι σαρώσεις καταγράφονται αλλά ΔΕΝ είναι έγκυρες.']);
  if (!S.idbOk) warns.push(['', 'Η τοπική αποθήκευση του browser δεν λειτουργεί (' + esc(S.idbError || '') + '). Σε διακοπή σύνδεσης ΜΗΝ ανανεώσετε τη σελίδα.']);
  if (S.error) warns.push(['', 'Σφάλμα αποθήκευσης στον server: ' + esc(S.error) + ' — τα barcode φυλάσσονται τοπικά και ξαναστέλνονται.']);
  if (cs === 'off' && S.pending.size) warns.push(['warn', `Χωρίς σύνδεση: ${S.pending.size} barcode φυλάσσονται τοπικά και θα σταλούν αυτόματα.`]);
  if (S.fsDenied && S.kiosk) warns.push(['warn', 'Ο browser δεν επέτρεψε πραγματική πλήρη οθόνη εδώ — πατήστε F11 για πλήρη οθόνη browser.']);
  if (actx && actx.state !== 'running' && S.soundOn) warns.push(['warn', 'Ο ήχος θα ενεργοποιηθεί με το πρώτο πλήκτρο ή κλικ.']);
  R.warnings.innerHTML = warns.map(([c, t]) => `<div class="${c}">${t}</div>`).join('');

  const latestId = S.latestEventId;
  if (latestId) {
    const r = S.results.get(latestId);
    const p = S.pending.get(latestId);
    if (r) {
      R.feedback.className = 'whs-fb whs-fb-' + (RESULT_CLASS[r.result] || 'err');
      R['fb-barcode'].textContent = r.barcode;
      R['fb-text'].textContent = r.result === 'ok' ? (r.display || r.message) : (r.message + (r.display ? ' — ' + r.display : ''));
    } else if (p) {
      R.feedback.className = 'whs-fb whs-fb-pending';
      R['fb-barcode'].textContent = p.barcode;
      R['fb-text'].textContent = cs === 'off' ? 'Εκκρεμεί — αποθηκεύτηκε τοπικά, αναμονή σύνδεσης' : 'Αναμονή επιβεβαίωσης…';
    }
  }

  const now = Date.now();
  for (const [k, v] of S.flash) if (v.until < now) S.flash.delete(k);
  let latestBarcode = null;
  if (latestId) { const r = S.results.get(latestId) || S.pending.get(latestId); if (r) latestBarcode = normBarcode(r.barcode); }
  const pend = Array.from(S.pending.values()).sort((a, b) => a.seq - b.seq);
  const rows = [];
  for (const it of S.items) {
    const k = normBarcode(it.barcode);
    const fl = S.flash.get(k);
    const desc = it.state === 'ok' ? it.display : it.state === 'ambiguous' ? 'ΑΜΦΙΣΗΜΟ — έλεγχος από γραμματεία' : 'ΔΕΝ ΒΡΕΘΗΚΕ στο Excel';
    const cls = ['s-' + it.state, k === latestBarcode && !pend.length ? 'latest' : '', fl ? fl.cls : ''].join(' ');
    rows.push(`<li class="${cls}"><span class="bc">${esc(it.barcode)}</span><span class="desc">${esc(desc || '')}${it.dup ? ` · σκαναρίστηκε ${it.dup + 1}×` : ''}</span></li>`);
  }
  pend.forEach((p, i) => {
    rows.push(`<li class="s-pending${i === pend.length - 1 ? ' latest' : ''}"><span class="bc">${esc(p.barcode)}</span><span class="desc">ΕΚΚΡΕΜΕΙ — αναμονή επιβεβαίωσης</span></li>`);
  });
  const html = rows.join('');
  if (html !== S.lastListHtml) {
    S.lastListHtml = html;
    R.list.innerHTML = html;
    R.empty.classList.toggle('hidden', rows.length > 0);
    R['list-wrap'].scrollTop = R['list-wrap'].scrollHeight; // αυτόματη κύλιση στο τελευταίο
  }
  R['btn-sound'].textContent = 'Ήχος: ' + (S.soundOn ? 'ΝΑΙ' : 'ΟΧΙ');
  R.device.textContent = S.user ? 'Χρήστης: ' + S.user : '';
  R.root.dataset.pending = String(S.pending.size);
  if (S.flash.size) setTimeout(scheduleRender, 300);
}

// ------------------------------------------------------------------ σύνδεση με το DOM
function attach(parentElement) {
  const root = parentElement.querySelector('.whs-root');
  if (!root) return;
  if (root === S.root) return;
  S.root = root;
  S.refs = { root };
  root.querySelectorAll('[data-ref]').forEach((el) => { S.refs[el.dataset.ref] = el; });
  S.lastListHtml = '';
  if (S.kiosk) root.classList.add('whs-kiosk');
  const blurAfter = (fn) => (e) => { fn(e); e.currentTarget.blur(); };
  S.refs['btn-fs'].addEventListener('click', blurAfter(toggleFullscreen));
  S.refs['btn-sound-test'].addEventListener('click', blurAfter(() => {
    audio();
    setTimeout(() => { play('ok', true); setTimeout(() => play('dup', true), 450); setTimeout(() => play('err', true), 1000); }, 60);
  }));
  S.refs['btn-sound'].addEventListener('click', blurAfter(() => {
    S.soundOn = !S.soundOn; lsSet('wh.sound', S.soundOn ? '1' : '0'); audio(); scheduleRender();
  }));
  S.refs.focus.addEventListener('click', () => { window.focus(); updateFocus(); });
}
function updateFocus() {
  if (S.refs.focus) S.refs.focus.classList.toggle('hidden', document.hasFocus());
}

async function initOnce() {
  S.inited = true;
  S.lastSeq = parseInt(lsGet('wh.seq') || '0', 10) || 0;
  document.addEventListener('keydown', onKeyDown, true);
  document.addEventListener('click', () => audio(), true);
  window.addEventListener('blur', updateFocus);
  window.addEventListener('focus', updateFocus);
  window.addEventListener('online', () => { S.retryAt = 0; maybeSend(true); scheduleRender(); });
  window.addEventListener('offline', scheduleRender);
  window.addEventListener('beforeunload', (e) => {
    if (S.pending.size && (!S.idbOk || Array.from(S.pending.values()).some((p) => p.memoryOnly || !p.ready))) {
      e.preventDefault(); e.returnValue = '';
    }
  });
  setInterval(() => { updateFocus(); maybeSend(); scheduleRender(); }, 1000);
  try {
    await IDB.open();
    const stored = await IDB.all();   // εκκρεμή από προηγούμενη φόρτωση της σελίδας
    stored.sort((a, b) => a.seq - b.seq);
    for (const rec of stored) {
      if (!S.pending.has(rec.event_id)) S.pending.set(rec.event_id, Object.assign({}, rec, { ready: true }));
      S.lastSeq = Math.max(S.lastSeq, rec.seq || 0);
    }
    if (navigator.storage && navigator.storage.persist) navigator.storage.persist().catch(() => {});
  } catch (e) {
    storageFailed(e);
  }
  scheduleRender();
  maybeSend(true);
}

export default function (component) {
  const { data, setTriggerValue, parentElement } = component;
  S.setTrigger = setTriggerValue;
  attach(parentElement);
  if (!S.inited) initOnce();
  onData(data);
  // Χωρίς cleanup: η κατάσταση (ουρά, listeners) πρέπει να επιβιώνει από τα Streamlit reruns.
  return () => {};
}

// για αυτοματοποιημένες δοκιμές
window.__whScannerState = S;
window.__whCapture = captureBarcode;
