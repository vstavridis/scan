# Αποθήκη Barcode — Σάρωση & Διαχείριση Αποθέματος (Streamlit Cloud)

Web εφαρμογή που ανοίγει **μόνο από browser**. Δεν εγκαθίσταται τίποτα στους υπολογιστές Windows: ούτε Python, ούτε τοπικός server, ούτε PowerShell.

* **Scanner / Αποθήκη**: καταγράφει σε πραγματικό χρόνο τα barcode του ασύρματου scanner (keyboard wedge). Έχει μεγάλα γράμματα, χρώματα, ήχους, πλήρη οθόνη και **τοπική ουρά (IndexedDB)** για διακοπές σύνδεσης.
* **Γραμματεία / Dashboard**: ζωντανή εικόνα από άλλο υπολογιστή, SUM κιλών ανά είδος/πάχος/διαστάσεις/πελάτη, έλεγχος αμφίσημων/άγνωστων, **μεταφορά** από `΄Ετοιμα προϊόντα` σε `Σβησμενα `.
* **Ιστορικό**: μόνιμη καταγραφή κάθε σκαναρίσματος και μεταφοράς, φίλτρα, εξαγωγή CSV/Excel.
* **Ρυθμίσεις** (Admin): ανέβασμα/έλεγχος/ενεργοποίηση Excel, στατιστικά, λήψη ενημερωμένου Excel και backups.

---

## 1. Αρχιτεκτονική

```
  PC αποθήκης (scanner USB)            PC γραμματείας
  Browser ─────────────┐               ┌──────────── Browser
                       ▼               ▼
        ┌────────── Streamlit Community Cloud ───────────┐
        │  streamlit_app.py  (ένα deployment)            │
        │   • σελίδες Streamlit (multipage)              │
        │   • Custom Component v2: οθόνη σάρωσης (JS)    │
        │   • app/backend.py: έλεγχος δικαιωμάτων +      │
        │     επιχειρησιακή λογική (Python)              │
        └───────┬───────────────────────────┬────────────┘
                │ SQL (pooler, TLS)         │ REST (service key, μόνο server-side)
                ▼                           ▼
     Supabase PostgreSQL            Supabase Storage (PRIVATE bucket)
     scans, ενεργή λίστα,           original Excel, εκδόσεις working
     ιστορικό, μεταφορές,           Excel, backups
     κατάλογος προϊόντων
```

* **Ένα deployment**: χωρίς FastAPI, Uvicorn ή άλλο server. Το Supabase χρησιμοποιείται μόνο για αποθήκευση.
* **Κοινή πηγή αλήθειας** η PostgreSQL. Η αποθήκη και η γραμματεία βλέπουν τα ίδια δεδομένα, και το `st.session_state` δεν χρησιμοποιείται ως βάση.
* **Credentials μόνο στα Streamlit Secrets.** Ο browser δεν βλέπει ποτέ κλειδιά Supabase. Κρατά μόνο ένα υπογεγραμμένο token ρόλου, για το «Να με θυμάται αυτός ο υπολογιστής».
* **Δικαιώματα στην Python**: κάθε ενέργεια περνά από το `app/backend.py`, που ελέγχει τον ρόλο (Scanner / Γραμματεία / Admin) ανεξάρτητα από τα κουμπιά.

### Πώς δουλεύει η σάρωση μέσα στο Streamlit

Η οθόνη σάρωσης είναι **Streamlit Custom Component v2**. Τρέχει απευθείας στη σελίδα, χωρίς iframe, με αμφίδρομη επικοινωνία JavaScript ↔ Python:

1. Το JavaScript πιάνει τα πλήκτρα του scanner. Με το **Enter** ολοκληρώνεται το barcode.
2. Δημιουργείται event με **μοναδικό UUID** και αύξοντα αριθμό σειράς.
3. Το event γράφεται **αμέσως στο IndexedDB** του browser (`durability: strict`).
4. Τα εκκρεμή στέλνονται στην Python **ομαδικά (batch)** με `setTriggerValue`. Έτσι γίνεται **ένα rerun για πολλά barcode**, όχι ένα ανά barcode, και ποτέ δεν χρησιμοποιείται `st.text_input`.
5. Η Python τα γράφει στην PostgreSQL σε **ένα transaction** (set-based: ~10 εντολές ανά batch, όσα barcode κι αν έχει).
6. **Μετά το COMMIT**, τα αποτελέσματα επιστρέφουν στον browser. Μόνο τότε το event σβήνει από το IndexedDB και η γραμμή γίνεται πράσινη/κίτρινη/κόκκινη.
7. Αν δεν έρθει επιβεβαίωση (χαμένο rerun, διακοπή δικτύου, restart του Cloud, ανανέωση σελίδας), το batch **ξαναστέλνεται**. Η βάση έχει **`UNIQUE(event_id)`**, άρα καμία επαναποστολή δεν δημιουργεί διπλή εγγραφή. Τα αποτελέσματα επαναλαμβάνονται για 2 λεπτά σε κάθε ανανέωση, ώστε να μη χάνονται.

Η λίστα ανανεώνεται κάθε 2 δευτερόλεπτα (`st.fragment(run_every=…)`), ώστε να φαίνονται και οι μεταφορές της γραμματείας. Η ανανέωση αυτή λειτουργεί και ως «heartbeat» για την ένδειξη σύνδεσης.

### Excel: ακεραιότητα με PostgreSQL + Storage

Ο υπάρχων Excel engine (`app/excel/`) **δεν άλλαξε**. Αλλάζει μόνο το XML των δύο φύλλων και διατηρεί τύπους, εξωτερικές συνδέσεις, κουμπιά macro και κρυφά φύλλα. Επειδή η βάση και το object storage δεν έχουν κοινό atomic transaction:

* Κάθε έκδοση του working Excel είναι **αμετάβλητο αρχείο** (`imports/00001/working/v00002_op000001.xlsx`). Κάθε μεταφορά ανεβάζει **νέα έκδοση** και δεν αντικαθιστά ποτέ την προηγούμενη, που μένει ως backup.
* Ροή μεταφοράς:
  1. έλεγχος δικαιωμάτων και μοναδικότητας
  2. **lease** «excel» στη βάση (μία εγγραφή Excel τη φορά, για όλα τα sessions)
  3. **journal** `transfer_operations(started)` (unique index ⇒ καμία διπλή μεταφορά)
  4. κατέβασμα τρέχουσας έκδοσης + έλεγχος sha256
  5. μετασχηματισμός + επαλήθευση
  6. ανέβασμα νέας έκδοσης + **ξανακατέβασμα και σύγκριση sha256**
  7. **ένα** transaction που κάνει compare-and-set του δείκτη έκδοσης (`working_version N → N+1`) και ολοκληρώνει τη μεταφορά, την ενεργή λίστα και το ιστορικό
* Αν αποτύχει οτιδήποτε πριν από το βήμα 7, ο δείκτης μένει στην παλιά έκδοση: **το Excel δεν άλλαξε** και η μεταφορά καταγράφεται ως αποτυχημένη. Δεν εμφανίζεται ποτέ επιτυχία χωρίς σωστά αποθηκευμένο Excel. Μια μεταφορά που διακόπηκε (restart) ακυρώνεται αυτόματα όταν λήξει το lease.

---

## 2. Ρύθμιση Supabase (μία φορά, ~10 λεπτά)

1. **Δημιουργία project**: https://supabase.com → *New project*.
   * **Region**: κοντά στο Streamlit Cloud, που τρέχει στις ΗΠΑ. Προτείνεται **East US (N. Virginia)**, για λιγότερη καθυστέρηση ανά σάρωση.
   * Ορίστε **Database password** και κρατήστε τον.
2. **Private bucket** για τα Excel: *Storage → New bucket* → όνομα `warehouse-excel` → **Public bucket: OFF** → File size limit 50 MB.
   (Εναλλακτικά εκτελέστε το `supabase/storage_bucket.sql` στο *SQL Editor*.) Δεν χρειάζονται policies.
3. **Connection string της βάσης**: πατήστε **Connect** (πάνω στη σελίδα του project) → **Session pooler**. Είναι συμβατό με το IPv4 δίκτυο του Streamlit Cloud. Αντιγράψτε το URI:
   `postgresql://postgres.<project-ref>:<PASSWORD>@aws-0-<region>.pooler.supabase.com:5432/postgres`
   * Αντικαταστήστε `<PASSWORD>` με τον κωδικό της βάσης. Αν περιέχει ειδικούς χαρακτήρες (`@ : / ? #`), γράψτε τους URL-encoded (π.χ. `@` → `%40`).
   * Προσθέστε στο τέλος `?sslmode=require`.
   * **Μη** χρησιμοποιήσετε το "Direct connection", που είναι μόνο IPv6.
4. **Project URL και service key**: *Project Settings → API keys / Data API*:
   * **Project URL**: `https://<project-ref>.supabase.co`
   * **service_role** key (ή το νέο **secret** key `sb_secret_…`). ⚠ Μυστικό: μπαίνει **μόνο** στα Streamlit Secrets.
5. **Πίνακες**: δημιουργούνται **αυτόματα** στην πρώτη εκκίνηση της εφαρμογής, από το `app/db/schema_postgres.sql`.
   * Το ίδιο script ενεργοποιεί Row Level Security και αφαιρεί κάθε δικαίωμα από τους ρόλους `anon`/`authenticated`, ώστε οι πίνακες να μην είναι προσβάσιμοι από το δημόσιο REST API του Supabase.
   * Αν προτιμάτε, εκτελέστε το χειροκίνητα στο *SQL Editor*.

> Free plan Supabase: ένα project παγώνει μετά από ~1 εβδομάδα **χωρίς καμία χρήση**. Σε κανονική εργασία δεν συμβαίνει. Μετά από μεγάλες αργίες μπορεί να χρειαστεί *Restore project* στο dashboard του Supabase. Για αυτόματα ημερήσια backups της βάσης απαιτείται Pro plan. Τα αρχεία Excel έχουν ήδη όλες τις εκδόσεις στο Storage.

## 3. Deployment στο Streamlit Community Cloud

1. Ο κώδικας είναι στο GitHub `vstavridis/scan`, στο branch `claude/focused-darwin-fivuu3`. Κάντε merge στο `main` (συνιστάται) ή διαλέξτε απευθείας αυτό το branch στο επόμενο βήμα.
2. https://share.streamlit.io → **Create app** → *Deploy a public app from GitHub*:
   * **Repository**: `vstavridis/scan`
   * **Branch**: `main` (ή το παραπάνω branch)
   * **Main file path**: `streamlit_app.py`
   * **App URL**: π.χ. `apothiki-barcode` → `https://apothiki-barcode.streamlit.app`
3. **Advanced settings**:
   * **Python version**: 3.12 (δοκιμασμένο και σε 3.13)
   * **Secrets**: επικολλήστε το περιεχόμενο του `.streamlit/secrets.toml.example` με τις πραγματικές τιμές: connection string, Supabase URL, service key, bucket, `secret_key` και τρεις κωδικούς ρόλων.
4. **Deploy**. Η πρώτη εκκίνηση εγκαθιστά τα `requirements.txt` (~2–3 λεπτά) και δημιουργεί τους πίνακες.
5. Αν κάτι λείπει από τα Secrets, η εφαρμογή εμφανίζει σελίδα «απαιτείται ρύθμιση» με το τι ακριβώς λείπει. Τα logs: *Manage app → Logs*.

**Ορατότητα**: με *Sharing → Public*, οποιοσδήποτε έχει το link βλέπει τη σελίδα σύνδεσης, και οι κωδικοί ρόλων προστατεύουν τα δεδομένα. Για επιπλέον προστασία ορίστε την εφαρμογή **Private** και προσκαλέστε τα email των χρηστών. Σε αυτή την περίπτωση κάθε χρήστης συνδέεται πρώτα με λογαριασμό Google/GitHub.

### Πρώτη χρήση

1. **Admin**: ανοίξτε το URL → Ρόλος «Διαχειριστής» → **Ρυθμίσεις** → ανέβασμα `Σχισμενα (1).xlsx` → **Έλεγχος αρχείου** → **Ενεργοποίηση αρχείου**.
2. **Αποθήκη** (PC με τον δέκτη): ανοίξτε το ίδιο URL → Ρόλος «Αποθήκη (Scanner)» → τσεκαρισμένο «Να με θυμάται» → **Πλήρης Οθόνη** (ή F9) → **Δοκιμή ήχου** → σκανάρετε.
3. **Γραμματεία**: ίδιο URL → Ρόλος «Γραμματεία» → Dashboard.

| Ποιος | Διεύθυνση | Ρόλος |
|---|---|---|
| Υπάλληλος αποθήκης | `https://<app>.streamlit.app` | Αποθήκη (Scanner) |
| Γραμματεία | `https://<app>.streamlit.app` | Γραμματεία |
| Διαχειριστής | `https://<app>.streamlit.app` → Ρυθμίσεις | Διαχειριστής |

---

## 4. Δοκιμές

Τρέχουν σε αντίγραφα δεδομένων. Το αρχικό αρχείο δοκιμής δεν τροποποιείται ποτέ (έλεγχος sha256).

```bash
pip install -r requirements-dev.txt
```
```bash
python -m playwright install chromium
```
```bash
python -m pytest
```

* Χωρίς ρυθμίσεις, τα tests χρησιμοποιούν **SQLite + τοπικό φάκελο**. Η SQLite επιτρέπεται **μόνο** για τοπικά tests και όχι στο Cloud.
* Με `WAREHOUSE_TEST_PG=postgresql://postgres@localhost:5432/postgres`, όλα τα tests του backend τρέχουν **και σε πραγματική PostgreSQL** (δημιουργείται προσωρινή βάση ανά test).
* Πραγματικό αρχείο: αντιγράψτε το στο `tests/fixtures/local/`, που εξαιρείται από το git.
* `tools/scanner_simulator.py`: προσομοίωση scanner υψηλής ταχύτητας. Σε `--mode browser` πληκτρολογεί barcode στην πραγματική εφαρμογή, και δέχεται και URL του Streamlit Cloud. Χρησιμοποιήστε δοκιμαστικό Supabase project.

## 5. Δομή project

```
streamlit_app.py              entrypoint (Streamlit Cloud)
requirements.txt              εξαρτήσεις (δοκιμασμένες εκδόσεις)
.streamlit/config.toml        ρυθμίσεις Streamlit
.streamlit/secrets.toml.example  πρότυπο Secrets (χωρίς πραγματικούς κωδικούς)
supabase/storage_bucket.sql   δημιουργία private bucket
app/backend.py                ενέργειες UI + έλεγχος δικαιωμάτων
app/auth.py                   ρόλοι, υπογεγραμμένα tokens
app/config.py                 ρυθμίσεις από Secrets/env
app/storage.py                Supabase Storage (REST) / τοπικός φάκελος για tests
app/db/                       σύνδεση PostgreSQL/SQLite, σχήματα (schema_postgres.sql)
app/excel/                    Excel engine (ανάγνωση, ασφαλής μεταφορά, τύποι) — αμετάβλητος
app/services/                 scans, catalog, aggregation, transfer, history, locks, settings
app/ui/components/            Custom Component v2: οθόνη σάρωσης (assets/*.js|css|html), auth bridge
app/ui/views/                 σελίδες: dashboard, scanner, history, settings
tests/                        pytest (backend σε SQLite+PostgreSQL, Excel, Supabase client, Streamlit+Chromium)
tools/scanner_simulator.py    simulator υψηλής ταχύτητας
```

## 6. Περιορισμοί και τι μένει να ελεγχθεί στην πράξη

* **Πλήρης οθόνη στο Streamlit Cloud**: το Cloud εμφανίζει την εφαρμογή μέσα στο δικό του πλαίσιο. Αν ο browser δεν επιτρέψει το Fullscreen API εκεί, η οθόνη σάρωσης περνά σε λειτουργία «kiosk» (καλύπτει όλο το παράθυρο) και προτείνει **F11**. Ελέγξτε το στο πραγματικό URL.
* **Αδράνεια εφαρμογής**: το Community Cloud κοιμίζει εφαρμογές χωρίς επισκέψεις για αρκετές ώρες. Η πρώτη επίσκεψη μετά δείχνει κουμπί «Yes, get this app back up!», και η επανεκκίνηση παίρνει ~1 λεπτό. Τα δεδομένα δεν χάνονται (Supabase). Μια ανοιχτή καρτέλα σάρωσης κρατά την εφαρμογή ενεργή.
* **Επανεκκίνηση/redeploy ενώ σκανάρετε**: τα barcode μένουν στο IndexedDB και στέλνονται όταν επανέλθει η σύνδεση (δοκιμασμένο). Αν η σελίδα κλείσει ή ανανεωθεί **όσο ο server είναι εκτός**, δεν μπορεί να ξαναφορτώσει μέχρι να επανέλθει. Τα barcode όμως μένουν στο IndexedDB και στέλνονται μετά. Δεν υπάρχει εγγύηση αν διαγραφούν τα δεδομένα του browser ή χαλάσει η συσκευή.
* **Ταχύτητα**: μετρήθηκε σε Chromium με τοπικό server, με και χωρίς τεχνητή καθυστέρηση βάσης (βλ. παρακάτω). Στο Cloud εξαρτάται από τη διαδρομή browser → Streamlit Cloud → Supabase. Μετρήστε με `tools/scanner_simulator.py --mode browser --url https://<app>.streamlit.app` και με τον πραγματικό scanner.
* **Φυσικός scanner**: πρέπει να ελεγχθούν ο τερματικός χαρακτήρας (Enter/Tab), τυχόν prefix/suffix και η ελληνική διάταξη πληκτρολογίου.
* **Supabase free plan**: όρια μεγέθους (500 MB βάση, 1 GB storage, 50 MB/αρχείο), παύση μετά από ~1 εβδομάδα αδράνειας, χωρίς αυτόματα backups βάσης.

### Μετρήσεις (τοπικά, Chromium + Streamlit + PostgreSQL 16)

| Σενάριο | Αποτέλεσμα |
|---|---|
| 150 barcode, ~2 ms/χαρακτήρα | όλα επιβεβαιωμένα σε ~8,4 s ≈ **18 barcode/s**, 0 επαναποστολές, 0 διπλές εγγραφές |
| Ίδιο, με +60 ms καθυστέρηση σε κάθε σύνδεση βάσης | ≈ **19 barcode/s** (το batching απορροφά την καθυστέρηση) |
| Εμφάνιση νέου scan στη γραμματεία | ~1–2 s |
