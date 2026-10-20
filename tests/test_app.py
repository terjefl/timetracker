import re

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("app.db.DB_PATH", str(tmp_path / "test.db"))
    from app.main import app
    with TestClient(app) as c:
        yield c


def make_customer(client, name="Gokartutleie AS", rate="650"):
    r = client.post("/kunder/ny", data={"name": name, "phone": "99 88 77 66", "email": "post@example.no",
                                        "rate_name": "Standard", "hourly_rate": rate})
    assert r.status_code == 200
    return int(re.search(r"/kunder/(\d+)", str(r.url)).group(1))


def test_parse_hours():
    from app.main import parse_hours
    assert parse_hours("1,5") == 1.5
    assert parse_hours("1.25") == 1.25
    assert parse_hours("1:30") == 1.5
    assert parse_hours("45m") == 0.75


def test_formatting():
    from app.main import fmt_hours, fmt_money
    assert fmt_money(12345.5) == "12 345,50 kr"
    assert fmt_hours(1.5) == "1,5"
    assert fmt_hours(2.0) == "2"


def test_pages_render(client):
    cid = make_customer(client)
    for url in ["/", "/kunder", f"/kunder/{cid}", "/kunder/ny", "/timer", "/rapport", "/fakturaer",
                "/innstillinger", "/healthz"]:
        assert client.get(url).status_code == 200, url


def test_single_rate_is_hidden_and_used(client):
    cid = make_customer(client)
    client.post("/timer", data={"customer_id": cid, "work_date": "2026-10-01", "hours": "2", "description": "Kasse"})
    page = client.get("/rapport", params={"kunde": cid}).text
    assert "1 300,00 kr" in page
    assert "<th>Type</th>" not in page  # bare én pristype → ingen typekolonne


def test_multiple_rates_and_invoice_flow(client):
    cid = make_customer(client)
    client.post(f"/kunder/{cid}/priser", data={"name": "Helg", "hourly_rate": "900"})
    from app import db
    with db.get_db() as conn:
        helg = conn.execute("SELECT id FROM rate_types WHERE name = 'Helg'").fetchone()["id"]

    client.post("/timer", data={"customer_id": cid, "work_date": "2026-10-03", "hours": "1,5",
                                "description": "Nettverk", "rate_type_id": helg})
    client.post("/timer", data={"customer_id": cid, "work_date": "2026-10-06", "hours": "2",
                                "description": "Booking-system"})
    page = client.get("/rapport", params={"kunde": cid}).text
    assert "<th>Type</th>" in page
    assert "2 650,00 kr" in page  # 1,5*900 + 2*650

    # Endret pris påvirker ikke allerede førte timer
    with db.get_db() as conn:
        std = conn.execute("SELECT id FROM rate_types WHERE name = 'Standard'").fetchone()["id"]
    client.post(f"/priser/{std}", data={"name": "Standard", "hourly_rate": "700", "action": "lagre"})
    assert "2 650,00 kr" in client.get("/rapport", params={"kunde": cid}).text

    r = client.post("/fakturaer", data={"customer_id": cid})
    inv_id = int(re.search(r"/fakturaer/(\d+)", str(r.url)).group(1))
    assert "Faktura" in r.text and "2 650,00 kr" in r.text

    # Timene er nå borte fra rapporten over ufakturerte
    assert "Ingen timer i utvalget" in client.get("/rapport", params={"kunde": cid}).text

    client.post(f"/fakturaer/{inv_id}/status", data={"status": "sendt"})
    with db.get_db() as conn:
        entry_id = conn.execute("SELECT id FROM entries LIMIT 1").fetchone()["id"]
    # Sendt faktura låser timene
    r = client.post(f"/timer/{entry_id}/slett", headers={"referer": "http://testserver/timer"})
    assert "sendt faktura" in r.text

    # Nye timer kommer med på neste rapport, de gamle gjør det ikke
    client.post("/timer", data={"customer_id": cid, "work_date": "2026-10-10", "hours": "1", "description": "Ny jobb"})
    page = client.get("/rapport", params={"kunde": cid}).text
    assert "Ny jobb" in page and "Nettverk" not in page

    r = client.post("/fakturaer", data={"customer_id": cid})
    assert ">2<" in r.text or "Fakturanr." in r.text


def test_delete_draft_releases_entries(client):
    cid = make_customer(client)
    client.post("/timer", data={"customer_id": cid, "work_date": "2026-10-01", "hours": "3", "description": "X"})
    r = client.post("/fakturaer", data={"customer_id": cid})
    inv_id = int(re.search(r"/fakturaer/(\d+)", str(r.url)).group(1))
    client.post(f"/fakturaer/{inv_id}/slett")
    assert "1 950,00 kr" in client.get("/rapport", params={"kunde": cid}).text
    from app import db
    with db.get_db() as conn:
        assert db.get_settings(conn)["next_invoice_number"] == "1"


def test_csv_export(client):
    cid = make_customer(client)
    client.post("/timer", data={"customer_id": cid, "work_date": "2026-10-01", "hours": "1:15", "description": "CSV"})
    r = client.get("/rapport.csv", params={"kunde": cid})
    assert r.status_code == 200
    assert "01.10.2026;Gokartutleie AS;Standard;1,25;650,00;812,50;CSV" in r.text
