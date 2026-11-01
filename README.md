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

## 2. Ρύθμιση Supabase (project `warehouse-scanner`)

Project URL: `https://uwzqfxvyjjprrqaxuztx.supabase.co` · Bucket: `warehouse-excel` (Private) — ήδη δημιουργημένα.

1. **Πίνακες (migration)**: Dashboard → **SQL Editor** → *New query* → επικολλήστε ολόκληρο το
   [`supabase/migrations/001_warehouse_init.sql`](supabase/migrations/001_warehouse_init.sql) → **Run**.
   Δημιουργεί όλους τους πίνακες/indexes/constraints, ενεργοποιεί Row Level Security, αφαιρεί κάθε δικαίωμα από τους
   ρόλους `anon`/`authenticated` και επιβεβαιώνει ότι το bucket είναι **private**. Είναι idempotent (ασφαλές να ξανατρέξει).
   *Προαιρετικό*: η εφαρμογή εκτελεί το ίδιο σχήμα και μόνη της στην πρώτη εκκίνηση.
2. **Connection string βάσης**: κουμπί **Connect** (πάνω στο Dashboard) → **Session pooler** → αντιγράψτε το URI:
   `postgresql://postgres.uwzqfxvyjjprrqaxuztx:[YOUR-PASSWORD]@aws-…-<region>.pooler.supabase.com:5432/postgres`
   * βάλτε τον **Database password** στη θέση του `[YOUR-PASSWORD]` (ειδικοί χαρακτήρες URL-encoded: `@`→`%40`, `#`→`%23`, `/`→`%2F`)·
     αν τον ξεχάσατε: *Project Settings → Database → Reset database password*·
   * προσθέστε στο τέλος `?sslmode=require`·
   * **όχι** "Direct connection" (μόνο IPv6 — το Streamlit Cloud είναι IPv4).
3. **Secret key**: *Project Settings → API Keys → Secret keys* (`sb_secret_…`). Μπαίνει **μόνο** στα Streamlit Secrets
   (`[supabase] service_key`). Υποστηρίζεται και το παλαιό `service_role` key. **Ποτέ** το publishable/anon key (απορρίπτεται).
4. Τίποτα άλλο: δεν χρειάζονται storage policies, Edge Functions ή Auth users — όλη η πρόσβαση γίνεται server-side
   από την εφαρμογή Streamlit.

> Free plan Supabase: project χωρίς **καμία** χρήση για ~1 εβδομάδα παγώνει (σε καθημερινή χρήση δεν συμβαίνει· μετά
> από μεγάλες αργίες: *Restore project*). Αυτόματα backups βάσης μόνο σε Pro plan — τα Excel έχουν ήδη όλες τις εκδόσεις
> στο Storage και το ιστορικό εξάγεται σε CSV/Excel.

## 3. Deployment στο Streamlit Community Cloud (χωρίς τίποτα στα Windows)

1. Ο κώδικας βρίσκεται στο `vstavridis/scan`, branch `claude/focused-darwin-fivuu3` (κάντε merge στο `main` ή
   επιλέξτε απευθείας αυτό το branch).
2. https://share.streamlit.io → σύνδεση με GitHub → **Create app** → *Deploy a public app from GitHub*:
   * **Repository**: `vstavridis/scan` · **Branch**: `main` (ή το παραπάνω) · **Main file path**: `streamlit_app.py`
   * **App URL**: π.χ. `apothiki-barcode` → `https://apothiki-barcode.streamlit.app`
3. **Advanced settings** → **Python version: 3.12** → **Secrets**: επικολλήστε το
   [`.streamlit/secrets.toml.example`](.streamlit/secrets.toml.example) συμπληρωμένο:
   `[database] url` (βήμα 2.2), `[supabase] service_key` (βήμα 2.3), `[auth] secret_key` (τυχαίο ≥32 χαρακτήρες) και
   οι τρεις κωδικοί ρόλων. Το `url` και το `bucket` του Supabase είναι ήδη συμπληρωμένα.
4. **Deploy** (πρώτη εγκατάσταση ~2–3 λεπτά). Αν λείπει κάτι, εμφανίζεται σελίδα «απαιτείται ρύθμιση» με ακριβή
   λίστα. Logs: *Manage app → Logs*. Αλλαγή Secrets αργότερα: *⋮ → Settings → Secrets* (η εφαρμογή ξεκινά ξανά).
5. **Έλεγχος σύνδεσης**: συνδεθείτε ως Διαχειριστής → **Ρυθμίσεις**: πάνω-πάνω γράφει
   «PostgreSQL … (Supabase) — σύνδεση OK · Supabase Storage, bucket «warehouse-excel»». Αν υπάρχει πρόβλημα
   (λάθος κωδικός, bucket δημόσιο, λάθος key) εμφανίζεται με κόκκινο.

**Ορατότητα**: *Public* = όποιος έχει το link βλέπει τη σελίδα σύνδεσης (τα δεδομένα τα προστατεύουν οι κωδικοί ρόλων).
Για επιπλέον προστασία: *Private* + πρόσκληση των email των χρηστών (θα συνδέονται πρώτα με Google/GitHub).

### Πρώτη χρήση

1. **Admin**: URL → Ρόλος «Διαχειριστής» → **Ρυθμίσεις** → ανέβασμα `Σχισμενα (1).xlsx` → **Έλεγχος αρχείου** → **Ενεργοποίηση αρχείου**.
2. **Αποθήκη** (PC με τον δέκτη): ίδιο URL → «Αποθήκη (Scanner)» → τσεκαρισμένο «Να με θυμάται» → **Πλήρης Οθόνη** (ή F9)
   → **Δοκιμή ήχου** → σκανάρετε.
3. **Γραμματεία**: ίδιο URL → «Γραμματεία» → Dashboard.

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
supabase/migrations/001_warehouse_init.sql  πλήρες SQL migration (πίνακες, RLS, private bucket)
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

### Ταχύτητα: τι μετρήθηκε και πού είναι τα όρια

Μέτρηση: Chromium + `streamlit run streamlit_app.py` + PostgreSQL 16, 150 barcode «πληκτρολογημένα» όπως από scanner
(~2 ms/χαρακτήρα ≈ 18 barcode/s), με **τεχνητή καθυστέρηση σε κάθε εντολή SQL / BEGIN / COMMIT** για να
προσομοιωθεί απομακρυσμένη βάση (`APP_SIMULATE_DB_LATENCY_MS`):

Δύο ανεξάρτητες εκτελέσεις (εύρος τιμών):

| Καθυστέρηση ανά εντολή SQL | Όλα επιβεβαιωμένα σε | Ρυθμός | Χαμένα / διπλά |
|---|---|---|---|
| 0 ms | 7,9–8,3 s | **18,2–18,9 /s** | 0 / 0 |
| 10 ms | 8,0–8,2 s | **18,2–18,7 /s** | 0 / 0 |
| 30 ms (ρεαλιστικό, ίδια περιοχή) | 8,7 s | **17,2 /s** | 0 / 0 |
| 80 ms | 10,6–10,9 s | **13,7–14,2 /s** | 0 / 0 |
| 150 ms (πολύ απαισιόδοξο) | 11,8–16,5 s | **9,1–12,8 /s** — στο όριο | 0 / 0 |

Συμπέρασμα: με βάση στην ίδια περιοχή με το Streamlit Cloud (≤ 30–80 ms) ο στόχος των 10/s καλύπτεται με περιθώριο·
με πολύ αργή διαδρομή (≈150 ms ανά εντολή) ο ρυθμός επιβεβαίωσης πέφτει γύρω στο 10/s — η **καταγραφή** όμως δεν
επηρεάζεται (βλ. παρακάτω).

Γιατί αντέχει: ένα Streamlit rerun ανά barcode θα ήταν αργό (~0,1–0,5 s/rerun στο Cloud). Εδώ όμως **κάθε rerun
μεταφέρει ένα batch** (έως 100 barcode) και η βάση γράφει κάθε batch με ~10 εντολές, όσα barcode κι αν περιέχει.
Όσο πιο αργή η διαδρομή, τόσο μεγαλύτερα γίνονται αυτόματα τα batches.

**Περιορισμοί που δεν μπορούν να μετρηθούν εδώ** (απαιτούν το πραγματικό deployment): η καθυστέρηση browser ↔
Streamlit Cloud (websocket, Ελλάδα ↔ ΗΠΑ ~120–180 ms) και το φορτίο του κοινόχρηστου Community Cloud. Αυτά επηρεάζουν
**πότε έρχεται η επιβεβαίωση**, όχι **αν καταγράφεται** ένα barcode:

* η λήψη των πλήκτρων και η αποθήκευση στο IndexedDB γίνονται **τοπικά στον browser**, ανεξάρτητα από τον server —
  ο scanner δεν «περιμένει» ποτέ τον server·
* αν ο server καθυστερεί, τα barcode εμφανίζονται πορτοκαλί («ΕΚΚΡΕΜΕΙ») και επιβεβαιώνονται λίγο αργότερα·
  δεν χάνονται και δεν διπλασιάζονται (UNIQUE event_id).

**Πώς να το επιβεβαιώσετε μετά το deploy** (από οποιονδήποτε υπολογιστή με Python — όχι από τα PC της αποθήκης):
`python tools/scanner_simulator.py --mode browser --url https://<app>.streamlit.app --password <SCANNER_PW> --count 150`
(χρησιμοποιεί δοκιμαστικά barcode `SIM0001…`, που θα εμφανιστούν ως «άγνωστα» — αφαιρέστε τα μετά από τη Γραμματεία).
Ή απλώς σκανάρετε γρήγορα 30–50 ετικέτες και κοιτάξτε τον μετρητή «εκκρεμή»: πρέπει να μηδενίζει μέσα σε λίγα δευτερόλεπτα.

**Αν στην πράξη δεν φτάνει** (π.χ. το Community Cloud είναι υπερφορτωμένο), οι ασφαλείς επιλογές, κατά σειρά:
1. Τίποτα δεν χάνεται στο μεταξύ — η ουρά απλώς αδειάζει πιο αργά.
2. Supabase project σε περιοχή κοντά στο Streamlit Cloud (US East) για ελάχιστο round-trip βάσης.
3. Μεγαλύτερα batches (`MAX_BATCH` στο `scanner.js`).
4. Αφιερωμένοι πόροι (Streamlit in Snowflake ή οποιοσδήποτε container host για το ίδιο `streamlit_app.py`).
5. Μόνο ως έσχατη λύση: απευθείας αποστολή από τον browser σε Supabase Edge Function — απαιτεί δημόσιο κλειδί στον
   browser και ξεχωριστό backend, γι' αυτό **δεν** υλοποιήθηκε.
