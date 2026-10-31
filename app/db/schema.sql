-- Σχήμα βάσης δεδομένων (SQLite, WAL mode).
-- Η βάση είναι η κεντρική πηγή αλήθειας για scan events, ενεργή λίστα και ιστορικό.

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Κάθε ανέβασμα Excel που ενεργοποιήθηκε (έκδοση εισαγωγής).
CREATE TABLE IF NOT EXISTS excel_imports (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    original_filename   TEXT    NOT NULL,
    original_path       TEXT    NOT NULL,        -- αναλλοίωτο αρχικό αρχείο
    working_path        TEXT    NOT NULL,        -- working copy που ενημερώνεται από τις μεταφορές
    original_sha256     TEXT    NOT NULL,
    imported_at         TEXT    NOT NULL,
    imported_by         TEXT    NOT NULL,
    is_active           INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0, 1)),
    product_count       INTEGER NOT NULL,
    unique_barcodes     INTEGER NOT NULL,
    duplicate_barcodes  INTEGER NOT NULL,
    source_sheet        TEXT    NOT NULL,
    target_sheet        TEXT    NOT NULL,
    notes               TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_imports_single_active ON excel_imports(is_active) WHERE is_active = 1;

-- Γραμμές προϊόντων του φύλλου πηγής (κατάλογος αναζήτησης).
CREATE TABLE IF NOT EXISTS product_records (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    import_id     INTEGER NOT NULL REFERENCES excel_imports(id),
    sheet_row     INTEGER NOT NULL,              -- γραμμή κατά την εισαγωγή (ενημερώνεται μετά από μεταφορές)
    barcode       TEXT    NOT NULL,              -- όπως στο Excel
    barcode_norm  TEXT    NOT NULL,              -- κανονικοποιημένο για αναζήτηση
    eidos         TEXT,
    paxos         TEXT,
    diastaseis    TEXT,
    temaxia       TEXT,
    kila          TEXT,                          -- Decimal ως κείμενο
    pelatis       TEXT,
    vspg          TEXT,
    topothesia    TEXT,
    thesi         TEXT,
    rolo          TEXT,
    extra_json    TEXT,
    fingerprint   TEXT    NOT NULL,              -- sha256 των ακατέργαστων τιμών της γραμμής
    group_key     TEXT    NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'available' CHECK (status IN ('available', 'transferred')),
    transferred_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_products_lookup ON product_records(import_id, barcode_norm);
CREATE INDEX IF NOT EXISTS ix_products_fp ON product_records(import_id, fingerprint);

-- Ενεργή λίστα (ό,τι φαίνεται στην αποθήκη και στη γραμματεία).
CREATE TABLE IF NOT EXISTS active_items (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    barcode             TEXT    NOT NULL,
    barcode_norm        TEXT    NOT NULL,
    state               TEXT    NOT NULL CHECK (state IN ('ok', 'ambiguous', 'unknown')),
    product_record_id   INTEGER REFERENCES product_records(id),
    import_id           INTEGER REFERENCES excel_imports(id),
    first_scan_event_id INTEGER,
    duplicate_count     INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT    NOT NULL,
    status              TEXT    NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'transferred', 'removed')),
    closed_at           TEXT,
    closed_by           TEXT,
    close_reason        TEXT
);
-- Ένα barcode μπορεί να υπάρχει μόνο μία φορά στην ενεργή λίστα.
CREATE UNIQUE INDEX IF NOT EXISTS ux_active_barcode ON active_items(barcode_norm) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS ix_active_status ON active_items(status);

-- Μόνιμο ιστορικό κάθε σκαναρίσματος (δεν διαγράφεται ποτέ).
CREATE TABLE IF NOT EXISTS scan_events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id         TEXT    NOT NULL UNIQUE,    -- μοναδικό ID από τον browser (idempotency)
    barcode          TEXT    NOT NULL,
    barcode_norm     TEXT    NOT NULL,
    client_ts        TEXT,
    received_at      TEXT    NOT NULL,
    device_id        TEXT,
    client_seq       INTEGER,
    result           TEXT    NOT NULL CHECK (result IN ('ok', 'duplicate', 'unknown', 'ambiguous', 'no_catalog', 'invalid')),
    message          TEXT,
    product_record_id INTEGER REFERENCES product_records(id),
    product_snapshot TEXT,                       -- JSON στοιχείων προϊόντος τη στιγμή του scan
    kila             TEXT,
    import_id        INTEGER REFERENCES excel_imports(id),
    active_item_id   INTEGER REFERENCES active_items(id)
);
CREATE INDEX IF NOT EXISTS ix_scans_received ON scan_events(received_at);
CREATE INDEX IF NOT EXISTS ix_scans_barcode ON scan_events(barcode_norm);
CREATE INDEX IF NOT EXISTS ix_scans_active ON scan_events(active_item_id);

-- Journal μεταφορών προς το φύλλο "Σβησμένα".
CREATE TABLE IF NOT EXISTS transfer_operations (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    active_item_id     INTEGER REFERENCES active_items(id),
    product_record_id  INTEGER NOT NULL REFERENCES product_records(id),
    import_id          INTEGER NOT NULL REFERENCES excel_imports(id),
    barcode            TEXT    NOT NULL,
    fingerprint        TEXT    NOT NULL,
    status             TEXT    NOT NULL CHECK (status IN ('started', 'completed', 'failed')),
    mode               TEXT,                     -- 'shift' (φυσική αφαίρεση) ή 'clear' (ασφαλής εναλλακτική)
    requested_by       TEXT    NOT NULL,
    started_at         TEXT    NOT NULL,
    finished_at        TEXT,
    source_row         INTEGER,
    target_row         INTEGER,
    excel_before_sha256 TEXT,
    excel_after_sha256  TEXT,
    backup_path        TEXT,
    warning            TEXT,
    error              TEXT
);
-- Αποτρέπει διπλή μεταφορά της ίδιας εγγραφής (διπλό click / ταυτόχρονα requests).
CREATE UNIQUE INDEX IF NOT EXISTS ux_transfer_once ON transfer_operations(product_record_id)
    WHERE status IN ('started', 'completed');

CREATE TABLE IF NOT EXISTS audit_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    actor    TEXT NOT NULL,
    action   TEXT NOT NULL,
    details  TEXT
);
CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit_log(ts);

CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
