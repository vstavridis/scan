"""Διάγνωση σύνδεσης PostgreSQL (Supabase) με ανεξάρτητη σύνδεση psycopg.

Το connection pool της psycopg κρύβει την αιτία: όταν η σύνδεση αποτυγχάνει, ξαναδοκιμάζει στο
παρασκήνιο και ο χρήστης βλέπει μόνο "PoolTimeout: couldn't get a connection after 30 sec".
Εδώ κάνουμε μία απευθείας σύνδεση, βήμα-βήμα (ρυθμίσεις -> DNS -> TCP -> PostgreSQL), και
ταξινομούμε το σφάλμα:

    password      λάθος database password
    user_or_host  λάθος username / project ref / Session Pooler host (π.χ. "Tenant or user not found")
    dns           το host δεν βρίσκεται (DNS)
    network       timeout / άρνηση σύνδεσης / δίκτυο (π.χ. direct host μόνο IPv6)
    ssl           πρόβλημα SSL / sslmode
    exhausted     εξαντλημένες συνδέσεις (pooler ή βάση)
    config        λάθος μορφή connection string / βάση που δεν υπάρχει
    ok            η σύνδεση λειτουργεί

ΑΣΦΑΛΕΙΑ: ποτέ δεν επιστρέφεται/καταγράφεται κωδικός ή πλήρες connection string. Εμφανίζονται μόνο
host, port, username (περιέχει το δημόσιο project ref), database και sslmode.
"""
from __future__ import annotations

import logging
import re
import socket
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

LABELS = {
    "ok": "Η σύνδεση με τη βάση λειτουργεί",
    "password": "Λάθος κωδικός βάσης (database password)",
    "user_or_host": "Λάθος username ή Session Pooler host",
    "dns": "Το host της βάσης δεν βρέθηκε (DNS)",
    "network": "Δεν υπάρχει δικτυακή σύνδεση με τη βάση (timeout / άρνηση)",
    "ssl": "Πρόβλημα SSL",
    "exhausted": "Εξαντλημένες συνδέσεις",
    "config": "Λάθος μορφή connection string",
    "unknown": "Άγνωστο σφάλμα σύνδεσης",
}


@dataclass
class Diagnosis:
    category: str
    summary: str
    details: list[str] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)
    target: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.category == "ok"

    @property
    def title(self) -> str:
        return LABELS.get(self.category, self.category)

    def as_text(self) -> str:
        lines = [f"{self.title}: {self.summary}"]
        lines += [f"• {d}" for d in self.details]
        lines += [f"→ {h}" for h in self.hints]
        return "\n".join(lines)


class DatabaseConnectionError(Exception):
    """Η βάση δεν είναι διαθέσιμη· περιέχει τη διάγνωση (χωρίς μυστικά)."""

    def __init__(self, diagnosis: Diagnosis):
        super().__init__(diagnosis.as_text())
        self.diagnosis = diagnosis


# --------------------------------------------------------------------------- ασφαλής περιγραφή URL
def _parse(url: str) -> dict[str, str]:
    from psycopg.conninfo import conninfo_to_dict

    return {k: str(v) for k, v in conninfo_to_dict(url).items() if v is not None}


def describe_target(url: str) -> dict[str, Any]:
    """Στοιχεία σύνδεσης ΧΩΡΙΣ κωδικό."""
    try:
        p = _parse(url)
    except Exception:
        return {"parse_error": True}
    pw = p.get("password", "")
    return {
        "host": p.get("host", ""),
        "port": p.get("port", "5432"),
        "user": p.get("user", ""),
        "dbname": p.get("dbname", ""),
        "sslmode": p.get("sslmode", "(προεπιλογή)"),
        "has_password": bool(pw),
        "password_placeholder": bool(re.search(r"YOUR[-_ ]?PASSWORD|^\[.*\]$", pw, re.I)),
    }


def redact(text: str, url: str | None) -> str:
    """Αφαιρεί κωδικό και connection string από μήνυμα σφάλματος."""
    out = str(text)
    if url:
        out = out.replace(url, "<connection string>")
        try:
            pw = _parse(url).get("password")
        except Exception:
            pw = None
        if pw:
            out = out.replace(pw, "***")
    out = re.sub(r"postgres(?:ql)?://[^\s'\"]+", "<connection string>", out)
    out = re.sub(r"password=\S+", "password=***", out)
    return out.strip()


def classify(message: str) -> str:
    m = message.lower()
    if "password authentication failed" in m or "invalid password" in m:
        return "password"
    if any(k in m for k in ("too many connections", "remaining connection slots", "max client connections",
                            "maxclientsinsessionmode", "connection limit", "max_client_conn",
                            "sorry, too many clients")):
        return "exhausted"
    if any(k in m for k in ("tenant or user not found", "role \"", "no such user",
                            "user not found")) and "database \"" not in m:
        return "user_or_host"
    if any(k in m for k in ("could not translate host name", "name or service not known", "nodename nor servname",
                            "temporary failure in name resolution", "no address associated")):
        return "dns"
    if "ssl" in m or "certificate" in m:
        return "ssl"
    if any(k in m for k in ("timeout expired", "timed out", "connection refused", "network is unreachable",
                            "no route to host", "server closed the connection unexpectedly",
                            "connection reset")):
        return "network"
    if "database \"" in m and "does not exist" in m:
        return "config"
    if "invalid connection option" in m or "invalid dsn" in m or "missing \"=\"" in m:
        return "config"
    return "unknown"


HINTS = {
    "password": ["Ελέγξτε τον Database password στο `[database] url`. Αν τον ξεχάσατε: Supabase → Project Settings → "
                 "Database → Reset database password, και ενημερώστε τα Streamlit Secrets.",
                 "Ειδικοί χαρακτήρες στον κωδικό γράφονται URL-encoded (@ → %40, # → %23, / → %2F, : → %3A)."],
    "user_or_host": ["Αντιγράψτε ξανά ΑΚΡΙΒΩΣ το URI από Supabase → Connect → Session pooler.",
                     "Το username πρέπει να είναι `postgres.<project-ref>` (με τελεία) και το host "
                     "`aws-…-<region>.pooler.supabase.com` της ΣΩΣΤΗΣ περιοχής του project."],
    "dns": ["Ελέγξτε ότι το host γράφτηκε σωστά (χωρίς κενά/αγκύλες).",
            "Χρησιμοποιήστε το Session pooler host (`…pooler.supabase.com`), όχι το direct `db.<ref>.supabase.co`."],
    "network": ["Χρησιμοποιήστε τον Session pooler (θύρα 5432, IPv4). Το «Direct connection» είναι μόνο IPv6 και "
                "δεν λειτουργεί από το Streamlit Cloud.",
                "Ελέγξτε ότι το Supabase project δεν είναι σε παύση (Paused) — Dashboard → Restore project."],
    "ssl": ["Χρησιμοποιήστε `?sslmode=require` στο τέλος του connection string."],
    "exhausted": ["Περιμένετε 1–2 λεπτά (κλείνουν οι παλιές συνδέσεις μετά από επανεκκίνηση της εφαρμογής).",
                  "Supabase → Database → Settings: ελέγξτε το pool size του pooler. Η εφαρμογή χρησιμοποιεί έως 4 "
                  "συνδέσεις.", "Μην τρέχουν ταυτόχρονα πολλά αντίγραφα της εφαρμογής με την ίδια βάση."],
    "config": ["Το `[database] url` πρέπει να έχει μορφή "
               "`postgresql://postgres.<ref>:<κωδικός>@<host>:5432/postgres?sslmode=require`."],
    "unknown": ["Δείτε το πλήρες (χωρίς μυστικά) μήνυμα παραπάνω και τα logs της εφαρμογής."],
}


def _static_checks(t: dict[str, Any]) -> tuple[str | None, list[str]]:
    """Έλεγχοι μορφής πριν από οποιαδήποτε σύνδεση."""
    notes: list[str] = []
    if t.get("parse_error"):
        return "config", ["Το connection string δεν διαβάζεται."]
    host, user = t["host"], t["user"]
    if not host:
        return "config", ["Λείπει το host."]
    if t["password_placeholder"]:
        return "password", ["Ο κωδικός είναι ακόμη το placeholder `[YOUR-PASSWORD]` — βάλτε τον πραγματικό κωδικό "
                            "(χωρίς αγκύλες)."]
    if not t["has_password"]:
        notes.append("Το connection string δεν περιέχει κωδικό (το Supabase απαιτεί τον Database password).")
    if re.match(r"^db\.[a-z0-9]+\.supabase\.co$", host):
        notes.append("Χρησιμοποιείται direct host (`db.<ref>.supabase.co`), που είναι μόνο IPv6. Από το Streamlit "
                     "Cloud χρειάζεται ο Session pooler.")
    if "pooler.supabase.com" in host and "." not in user:
        return "user_or_host", ["Με τον Supabase pooler το username πρέπει να είναι `postgres.<project-ref>`, "
                                f"όχι σκέτο `{user}`."]
    if "pooler.supabase.com" in host and str(t["port"]) == "6543":
        notes.append("Θύρα 6543 = Transaction pooler. Προτείνεται ο Session pooler (θύρα 5432).")
    if t["sslmode"] in ("(προεπιλογή)", "disable", "allow"):
        notes.append("Προτείνεται `?sslmode=require` στο τέλος του connection string.")
    return None, notes


def diagnose(url: str | None, timeout: float = 10.0) -> Diagnosis:
    """Βήμα-βήμα έλεγχος με ΑΝΕΞΑΡΤΗΤΗ σύνδεση (όχι από το pool)."""
    start = time.monotonic()

    def done(cat: str, summary: str, details: list[str]) -> Diagnosis:
        d = Diagnosis(cat, summary, details, HINTS.get(cat, []) if cat != "ok" else [], target,
                      int((time.monotonic() - start) * 1000))
        log.warning("DB diagnosis: %s (host=%s, port=%s, user=%s) %s", cat, target.get("host"), target.get("port"),
                    target.get("user"), summary) if cat != "ok" else log.info("DB diagnosis: ok")
        return d

    if not url:
        target: dict[str, Any] = {}
        return done("config", "Δεν έχει οριστεί `[database] url` στα Streamlit Secrets.", [])
    target = describe_target(url)
    cat, notes = _static_checks(target)
    where = f"{target.get('user', '?')}@{target.get('host', '?')}:{target.get('port', '?')}/{target.get('dbname', '?')}"
    if cat:
        return done(cat, f"Ρυθμίσεις σύνδεσης ({where})", notes)
    host, port = target["host"], int(target["port"] or 5432)

    # 1. DNS
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return done("dns", f"Το «{host}» δεν βρέθηκε ({exc.strerror or exc}).", notes)
    families = sorted({"IPv6" if i[0] == socket.AF_INET6 else "IPv4" for i in infos})
    notes.append(f"DNS: {host} → {', '.join(families)}")

    # 2. TCP
    last: Exception | None = None
    for fam, typ, proto, _c, addr in infos:
        try:
            with socket.socket(fam, typ, proto) as s:
                s.settimeout(min(timeout, 8.0))
                s.connect(addr)
            last = None
            break
        except OSError as exc:
            last = exc
    if last is not None:
        kind = "timeout" if isinstance(last, (socket.timeout, TimeoutError)) else (last.strerror or str(last))
        if families == ["IPv6"]:
            notes.append("Το host έχει μόνο IPv6 διευθύνσεις (direct connection) — το Streamlit Cloud χρειάζεται IPv4.")
        return done("network", f"Δεν ανοίγει σύνδεση TCP προς {host}:{port} ({kind}).", notes)
    notes.append(f"TCP: {host}:{port} — OK")

    # 3. PostgreSQL (TLS + αυθεντικοποίηση) με ανεξάρτητη σύνδεση psycopg
    try:
        import psycopg

        with psycopg.connect(url, connect_timeout=int(max(2, timeout)), autocommit=True, prepare_threshold=None,
                             application_name="warehouse-diagnostics") as conn:
            row = conn.execute("SELECT current_setting('server_version'), current_user, "
                               "(SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()), "
                               "current_setting('max_connections')").fetchone()
            ssl_on = bool(conn.pgconn.ssl_in_use)
    except Exception as exc:  # psycopg.OperationalError κ.λπ.
        msg = redact(str(exc), url)
        cat = classify(msg)
        return done(cat, msg.splitlines()[0][:300] if msg else type(exc).__name__,
                    notes + [f"Μήνυμα PostgreSQL: {msg[:500]}"])
    notes.append(f"PostgreSQL {row[0]}, χρήστης {row[1]}, SSL: {'ναι' if ssl_on else 'ΟΧΙ'}, "
                 f"συνδέσεις: {row[2]}/{row[3]}")
    return done("ok", f"{where} — συνδέθηκε επιτυχώς", notes)
