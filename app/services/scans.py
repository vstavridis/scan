"""Επεξεργασία scan events (idempotent, transactional).

Κάθε batch επεξεργάζεται σε ΕΝΑ transaction με τη σειρά που στάλθηκε. Τα αποτελέσματα
(acknowledgements) επιστρέφονται στον browser μόνο αφού γίνει COMMIT. Η επαναποστολή ίδιου
event_id επιστρέφει το αποθηκευμένο αποτέλεσμα χωρίς νέα καταχώρηση (UNIQUE event_id).
Τα batches σειριοποιούνται (advisory lock "scans"), ώστε δύο ταυτόχρονα σκαναρίσματα του
ίδιου barcode να μη γίνουν και τα δύο "νέα".
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from app.db.database import Conn, bump_version, connect, get_version, transaction
from app.services import catalog
from app.services.normalize import normalize_barcode, product_display
from app.timeutil import utcnow_iso

log = logging.getLogger(__name__)

MAX_BARCODE_LEN = 128

MESSAGES = {
    "ok": "Καταχωρήθηκε",
    "duplicate": "Το barcode έχει ήδη σκαναριστεί",
    "unknown": "Το barcode δεν βρέθηκε στο Excel",
    "unknown_transferred": "Το barcode έχει ήδη μεταφερθεί στα Σβησμένα",
    "ambiguous": "Αμφίσημο barcode — υπάρχει σε περισσότερες γραμμές του Excel",
    "no_catalog": "Δεν υπάρχει ενεργό Excel — η σάρωση ΔΕΝ είναι έγκυρη",
    "invalid": "Μη έγκυρο barcode",
}


@dataclass
class ScanIn:
    event_id: str
    barcode: str
    client_ts: str | None = None
    seq: int | None = None


def _result_from_row(row: Any) -> dict[str, Any]:
    snap = json.loads(row["product_snapshot"]) if row["product_snapshot"] else None
    display = None
    if snap:
        display = product_display(snap.get("eidos"), snap.get("paxos"), snap.get("diastaseis"), snap.get("kila"),
                                  snap.get("pelatis"))
    return {
        "event_id": row["event_id"],
        "server_id": row["id"],
        "barcode": row["barcode"],
        "result": row["result"],
        "message": row["message"],
        "display": display,
        "kila": row["kila"],
        "active_item_id": row["active_item_id"],
        "received_at": row["received_at"],
    }


def _marks(n: int) -> str:
    return ",".join("?" * n)


def process_batch(events: list[ScanIn], device_id: str | None = None) -> dict[str, Any]:
    """Επεξεργάζεται τα events με τη σειρά τους σε ΕΝΑ transaction και επιστρέφει μετά το COMMIT.

    Set-based: ~10 εντολές SQL ανά batch, ανεξάρτητα από το πλήθος των barcode. Σημαντικό για
    απομακρυσμένη βάση (Supabase), όπου κάθε εντολή κοστίζει ένα round-trip δικτύου.
    """
    if not events:
        with connect() as conn:
            return {"results": [], "version": get_version(conn)}
    with transaction(lock="scans") as conn:
        imp = catalog.active_import(conn)
        import_id = imp["id"] if imp else None
        uniq_ids = list(dict.fromkeys(e.event_id for e in events))
        existing = {r["event_id"]: r for r in conn.execute(
            f"SELECT * FROM scan_events WHERE event_id IN ({_marks(len(uniq_ids))})", uniq_ids).fetchall()}

        norms = sorted({normalize_barcode(e.barcode) for e in events if e.event_id not in existing} - {""})
        active: dict[str, dict[str, Any]] = {}
        products: dict[str, list[Any]] = {}
        if imp is not None and norms:
            for r in conn.execute(
                f"SELECT id, barcode_norm, product_record_id FROM active_items WHERE status='active' "
                f"AND barcode_norm IN ({_marks(len(norms))})", norms).fetchall():
                active[r["barcode_norm"]] = dict(r)
            for r in conn.execute(
                f"SELECT * FROM product_records WHERE import_id=? AND barcode_norm IN ({_marks(len(norms))}) "
                f"ORDER BY sheet_row", [import_id, *norms]).fetchall():
                products.setdefault(r["barcode_norm"], []).append(r)
        by_id = {r["id"]: r for rows in products.values() for r in rows}

        now = utcnow_iso()
        plans: list[dict[str, Any]] = []          # ένα ανά event, με τη σειρά
        first_of_event: dict[str, dict[str, Any]] = {}
        new_items: dict[str, dict[str, Any]] = {}  # norm -> νέα εγγραφή ενεργής λίστας
        dup_inc: dict[Any, int] = {}               # active id ή ("new", norm) -> πόσα διπλά
        for ev in events:
            if ev.event_id in existing:
                plans.append({"kind": "replay", "row": existing[ev.event_id]})
                continue
            if ev.event_id in first_of_event:          # ίδιο event_id δύο φορές στο ίδιο batch
                plans.append({"kind": "same", "ref": first_of_event[ev.event_id]})
                continue
            barcode = (ev.barcode or "").strip()
            norm = normalize_barcode(barcode)
            plan: dict[str, Any] = {"kind": "new", "ev": ev, "barcode": barcode, "norm": norm,
                                    "product_id": None, "active": None}
            if not norm or len(norm) > MAX_BARCODE_LEN:
                plan.update(result="invalid", message=MESSAGES["invalid"])
            elif imp is None:
                plan.update(result="no_catalog", message=MESSAGES["no_catalog"])
            elif norm in active:
                a = active[norm]
                plan.update(result="duplicate", message=MESSAGES["duplicate"], active=a["id"],
                            product_id=a["product_record_id"])
                dup_inc[a["id"]] = dup_inc.get(a["id"], 0) + 1
            elif norm in new_items:
                item = new_items[norm]
                plan.update(result="duplicate", message=MESSAGES["duplicate"], active=("new", norm),
                            product_id=item["product_id"])
                dup_inc[("new", norm)] = dup_inc.get(("new", norm), 0) + 1
            else:
                rows = products.get(norm, [])
                available = [r for r in rows if r["status"] == "available"]
                if len(available) == 1:
                    plan.update(result="ok", message=MESSAGES["ok"], product_id=available[0]["id"])
                elif len(available) > 1:
                    plan.update(result="ambiguous", message=MESSAGES["ambiguous"])
                else:
                    plan.update(result="unknown",
                                message=MESSAGES["unknown_transferred"] if rows else MESSAGES["unknown"])
                new_items[norm] = {"barcode": barcode, "state": plan["result"], "product_id": plan["product_id"],
                                   "plan": plan}
                plan["active"] = ("new", norm)
            first_of_event[ev.event_id] = plan
            plans.append(plan)

        # 1) νέες εγγραφές ενεργής λίστας (μία εντολή)
        new_ids: dict[str, int] = {}
        if new_items:
            vals = []
            for norm, it in new_items.items():
                vals += [it["barcode"], norm, it["state"], it["product_id"], import_id, now]
            for r in conn.execute(
                "INSERT INTO active_items(barcode, barcode_norm, state, product_record_id, import_id, created_at) "
                f"VALUES {','.join(['(?, ?, ?, ?, ?, ?)'] * len(new_items))} RETURNING id, barcode_norm", vals,
            ).fetchall():
                new_ids[r["barcode_norm"]] = int(r["id"])

        def active_id(ref: Any) -> int | None:
            if isinstance(ref, tuple):
                return new_ids[ref[1]]
            return ref

        # 2) μετρητές διπλών (μία εντολή)
        if dup_inc:
            pairs = [(active_id(k), n) for k, n in dup_inc.items()]
            case = " ".join("WHEN ? THEN ?" for _ in pairs)
            conn.execute(
                f"UPDATE active_items SET duplicate_count = duplicate_count + (CASE id {case} ELSE 0 END) "
                f"WHERE id IN ({_marks(len(pairs))})",
                [x for pr in pairs for x in pr] + [pr[0] for pr in pairs],
            )

        # 3) ιστορικό (μία εντολή)
        new_plans = [pl for pl in plans if pl["kind"] == "new"]
        scan_ids: dict[str, int] = {}
        if new_plans:
            vals = []
            for pl in new_plans:
                prod = by_id.get(pl["product_id"]) if pl["product_id"] else None
                snapshot = catalog.snapshot_from_record(prod) if prod is not None else None
                pl["display"] = (product_display(prod["eidos"], prod["paxos"], prod["diastaseis"], prod["kila"],
                                                 prod["pelatis"]) if prod is not None else None)
                pl["kila"] = prod["kila"] if prod is not None else None
                pl["active_id"] = active_id(pl["active"]) if pl["active"] is not None else None
                ev = pl["ev"]
                vals += [ev.event_id, pl["barcode"][:256], pl["norm"][:256], ev.client_ts, now, device_id, ev.seq,
                         pl["result"], pl["message"], pl["product_id"], snapshot,
                         prod["pelatis"] if prod is not None else None,
                         pl["kila"] if pl["result"] == "ok" else None, import_id, pl["active_id"]]
            for r in conn.execute(
                "INSERT INTO scan_events(event_id, barcode, barcode_norm, client_ts, received_at, device_id, "
                "client_seq, result, message, product_record_id, product_snapshot, pelatis, kila, import_id, "
                f"active_item_id) VALUES {','.join(['(' + _marks(15) + ')'] * len(new_plans))} "
                "RETURNING id, event_id", vals,
            ).fetchall():
                scan_ids[r["event_id"]] = int(r["id"])

        # 4) πρώτο σκανάρισμα κάθε νέας εγγραφής (μία εντολή)
        if new_items:
            pairs = [(new_ids[n], scan_ids[it["plan"]["ev"].event_id]) for n, it in new_items.items()]
            case = " ".join("WHEN ? THEN ?" for _ in pairs)
            conn.execute(
                f"UPDATE active_items SET first_scan_event_id = (CASE id {case} END) "
                f"WHERE id IN ({_marks(len(pairs))})",
                [x for pr in pairs for x in pr] + [pr[0] for pr in pairs],
            )

        results: list[dict[str, Any]] = []
        for pl in plans:
            if pl["kind"] == "replay":
                out = _result_from_row(pl["row"])
                out["replayed"] = True
            elif pl["kind"] == "same":
                ref = pl["ref"]
                out = {"event_id": ref["ev"].event_id, "server_id": scan_ids[ref["ev"].event_id],
                       "barcode": ref["barcode"], "result": ref["result"], "message": ref["message"],
                       "display": ref.get("display"), "kila": ref.get("kila"), "active_item_id": ref.get("active_id"),
                       "received_at": now, "replayed": True}
            else:
                out = {"event_id": pl["ev"].event_id, "server_id": scan_ids[pl["ev"].event_id],
                       "barcode": pl["barcode"], "result": pl["result"], "message": pl["message"],
                       "display": pl["display"], "kila": pl["kila"], "active_item_id": pl["active_id"],
                       "received_at": now, "replayed": False}
            results.append(out)
        version = bump_version(conn) if new_plans else get_version(conn)
    return {"results": results, "version": version}
