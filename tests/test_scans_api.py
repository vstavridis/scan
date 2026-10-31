"""Tests 4, 6, 9 + δικαιώματα: διπλό scan, άγνωστο barcode, επαναποστολή event ID."""
from __future__ import annotations

from tests.conftest import activate_workbook, login, scan


def test_rescan_is_duplicate_and_not_counted_twice(client, synthetic_master):
    activate_workbook(client, synthetic_master)
    h = login(client, "scanner")
    res = scan(client, h, "VS11486", "VS11487", "VS11486")
    assert [r["result"] for r in res] == ["ok", "ok", "duplicate"]
    assert res[2]["message"] == "Το barcode έχει ήδη σκαναριστεί"
    dash = client.get("/api/dashboard", headers=login(client, "secretary")).json()
    assert dash["totals"]["active_barcodes"] == 2
    assert dash["totals"]["total_kilos"] == "828"          # 412 + 416, όχι 1240
    assert dash["totals"]["duplicate_scans"] == 1
    # το διπλό γράφεται στο ιστορικό ως νέα προσπάθεια
    hist = client.get("/api/history", params={"barcode": "VS11486"}, headers=login(client, "secretary")).json()
    assert sorted(r["result"] for r in hist) == ["duplicate", "ok"]


def test_unknown_barcode(client, synthetic_master):
    activate_workbook(client, synthetic_master)
    res = scan(client, login(client, "scanner"), "NOPE-999")[0]
    assert res["result"] == "unknown"
    assert "δεν βρέθηκε" in res["message"]
    dash = client.get("/api/dashboard", headers=login(client, "secretary")).json()
    assert dash["totals"]["unknown"] == 1
    assert dash["totals"]["total_kilos"] == "0"


def test_same_event_id_is_idempotent(client, synthetic_master):
    activate_workbook(client, synthetic_master)
    h = login(client, "scanner")
    eid = "evt-0000-retry-0001"
    first = scan(client, h, "VS11486", ids=[eid])[0]
    for _ in range(3):
        again = scan(client, h, "VS11486", ids=[eid])[0]
        assert again["result"] == "ok"
        assert again["server_id"] == first["server_id"]
        assert again["replayed"] is True
    # ίδιο event ID δύο φορές μέσα στο ίδιο batch
    both = scan(client, h, "VS11487", "VS11487", ids=["evt-same-batch-1", "evt-same-batch-1"])
    assert both[0]["server_id"] == both[1]["server_id"]
    hist = client.get("/api/history", headers=login(client, "secretary")).json()
    assert len(hist) == 2
    dash = client.get("/api/dashboard", headers=login(client, "secretary")).json()
    assert dash["totals"]["duplicate_scans"] == 0


def test_no_active_excel_is_never_valid(client):
    res = scan(client, login(client, "scanner"), "VS11486")[0]
    assert res["result"] == "no_catalog"


def test_role_protection_on_backend(client, synthetic_master):
    activate_workbook(client, synthetic_master)
    scanner, sec = login(client, "scanner"), login(client, "secretary")
    assert client.post("/api/scans", json={"events": []}).status_code == 401
    assert client.get("/api/dashboard", headers=scanner).status_code == 403
    assert client.post("/api/items/1/transfer", headers=scanner).status_code == 403
    assert client.post("/api/scans", json={"events": []}, headers=sec).status_code == 403
    assert client.get("/api/admin/settings", headers=sec).status_code == 403
    assert client.post("/api/admin/imports/stage", params={"filename": "x.xlsx"}, content=b"x", headers=sec).status_code == 403
    assert client.post("/api/auth/login", json={"role": "admin", "password": "wrong"}).status_code == 401
    bad = {"Authorization": "Bearer " + sec["Authorization"][7:-2] + "xx"}
    assert client.get("/api/active", headers=bad).status_code == 401


def test_replace_excel_requires_confirmation(client, synthetic_master):
    activate_workbook(client, synthetic_master)
    scan(client, login(client, "scanner"), "VS11486")
    h = login(client, "admin")
    staged = client.post("/api/admin/imports/stage", params={"filename": "new.xlsx"},
                         content=synthetic_master.read_bytes(), headers=h).json()
    r = client.post("/api/admin/imports/activate", json={"staging_id": staged["staging_id"]}, headers=h)
    assert r.status_code == 409 and r.json()["code"] == "confirmation_required"
    r = client.post("/api/admin/imports/activate", json={"staging_id": staged["staging_id"], "confirm_replace": True},
                    headers=h)
    assert r.status_code == 200
    assert r.json()["backups"], "πρέπει να γίνει backup πριν την αντικατάσταση"
    imports = client.get("/api/admin/imports", headers=h).json()
    assert len(imports) == 2 and sum(i["is_active"] for i in imports) == 1  # το ιστορικό εισαγωγών μένει
    dash = client.get("/api/dashboard", headers=login(client, "secretary")).json()
    assert dash["items"][0]["import_id"] == r.json()["import_id"]  # η ενεργή λίστα αντιστοιχίστηκε στο νέο αρχείο


def test_invalid_upload_rejected(client):
    h = login(client, "admin")
    r = client.post("/api/admin/imports/stage", params={"filename": "bad.xlsx"}, content=b"not a zip", headers=h)
    assert r.status_code == 200
    rep = r.json()["report"]
    assert rep["ok"] is False and rep["errors"]
    r2 = client.post("/api/admin/imports/activate", json={"staging_id": r.json()["staging_id"]}, headers=h)
    assert r2.status_code == 400
