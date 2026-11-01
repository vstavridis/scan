"""Tests 2 & 3: SUM ομάδων και διαχωρισμός πελατών."""
from __future__ import annotations

from decimal import Decimal

from app import backend
from app.services.aggregation import aggregate, totals
from app.services.normalize import group_key
from tests.conftest import activate_workbook, login, scan


def _item(i, eidos, paxos, diast, kila, pelatis, state="ok"):
    return {"id": i, "barcode": f"B{i}", "state": state, "eidos": eidos, "paxos": paxos, "diastaseis": diast,
            "kila": kila, "pelatis": pelatis, "group_key": group_key(eidos, paxos, diast, pelatis), "duplicate_count": 0}


def test_sum_example_from_specification():
    items = [
        _item(1, "ΗΛΕΚΤΡΟΓΑΛΒΑΝΙΖΕ", "0.70", "180ΧΡΟΛΟ", "512", "ΑΠΟΘΗΚΗ"),
        _item(2, "ΗΛΕΚΤΡΟΓΑΛΒΑΝΙΖΕ", "0.70", "180ΧΡΟΛΟ", "412", "ΑΠΟΘΗΚΗ"),
        _item(3, "ΗΛΕΚΤΡΟΓΑΛΒΑΝΙΖΕ", "0.70", "180ΧΡΟΛΟ", "412", "ΚΥΡΙΑΚΙΔΗΣ"),
        _item(4, "ΓΑΛΒΑΝΙΖΕ", "0.70", "180ΧΡΟΛΟ", "412", "ΚΥΡΙΑΚΙΔΗΣ"),
    ]
    groups = aggregate(items)
    table = [(g["product"], g["total_display"], g["pelatis"]) for g in groups]
    assert table == [
        ("ΗΛΕΚΤΡΟΓΑΛΒΑΝΙΖΕ × 0.70 × 180ΧΡΟΛΟ", "924", "ΑΠΟΘΗΚΗ"),
        ("ΗΛΕΚΤΡΟΓΑΛΒΑΝΙΖΕ × 0.70 × 180ΧΡΟΛΟ", "412", "ΚΥΡΙΑΚΙΔΗΣ"),
        ("ΓΑΛΒΑΝΙΖΕ × 0.70 × 180ΧΡΟΛΟ", "412", "ΚΥΡΙΑΚΙΔΗΣ"),
    ]
    assert totals(items, groups)["total_kilos"] == "1748"
    assert [b["barcode"] for b in groups[0]["barcodes"]] == ["B1", "B2"]


def test_different_customers_never_merge():
    items = [_item(1, "Χ", "1", "Υ", "10", "ΑΛΦΑ"), _item(2, "Χ", "1", "Υ", "10", "ΒΗΤΑ")]
    assert len(aggregate(items)) == 2


def test_normalization_groups_equivalent_values():
    # 0.7 == 0.70, λατινικό X == ελληνικό Χ, κενά/πεζά
    items = [
        _item(1, "ΗΛΕΚΤΡΟΓΑΛΒΑΝΙΖΕ", "0.70", "180ΧΡΟΛΟ", "100", "ΑΠΟΘΗΚΗ"),
        _item(2, "ηλεκτρογαλβανιζε ", "0.7", "180XΡΟΛΟ", "50.5", " ΑΠΟΘΗΚΗ"),
    ]
    groups = aggregate(items)
    assert len(groups) == 1
    assert Decimal(groups[0]["total"]) == Decimal("150.5")
    assert groups[0]["eidos"] == "ΗΛΕΚΤΡΟΓΑΛΒΑΝΙΖΕ"  # αρχική τιμή για εμφάνιση


def test_unknown_and_ambiguous_do_not_add_kilos():
    items = [_item(1, "Α", "1", "Β", "10", "Γ"), _item(2, None, None, None, None, None, state="unknown"),
             _item(3, None, None, None, None, None, state="ambiguous")]
    groups = aggregate(items)
    assert totals(items, groups)["total_kilos"] == "10"
    assert totals(items, groups)["problem"] == 2


def test_sum_via_backend(app_env, synthetic_master):
    activate_workbook(synthetic_master)
    scan(login("scanner"), "VS90001", "VS11486", "VS90002", "VS90003")
    dash = backend.dashboard(login("secretary"))
    table = sorted((g["product"], g["total_display"], g["pelatis"]) for g in dash["groups"])
    assert table == sorted([
        ("ΗΛΕΚΤΡΟΓΑΛΒΑΝΙΖΕ × 0.70 × 180ΧΡΟΛΟ", "924", "ΑΠΟΘΗΚΗ"),
        ("ΗΛΕΚΤΡΟΓΑΛΒΑΝΙΖΕ × 0.70 × 180ΧΡΟΛΟ", "412", "ΚΥΡΙΑΚΙΔΗΣ"),
        ("ΓΑΛΒΑΝΙΖΕ × 0.70 × 180ΧΡΟΛΟ", "412", "ΚΥΡΙΑΚΙΔΗΣ"),
    ])
    assert dash["totals"]["total_kilos"] == "1748"
    assert dash["totals"]["groups"] == 3
