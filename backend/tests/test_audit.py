"""Audit trail screen API: who changed what, newest first, filtered, company-scoped."""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.conftest import login

pytestmark = pytest.mark.db
API = "/api/v1"


def test_owner_sees_who_changed_what(world, client) -> None:
    h = login(client, "boss")
    r = client.post(
        f"{API}/tax-codes",
        json={"code": "AUD8", "name": "Audit test 8%", "rate": "8", "einvoice_tax_type": "01"},
        headers=h,
    )
    assert r.status_code == 201, r.text
    tax_id = r.json()["id"]

    page = client.get(f"{API}/audit/events", params={"entity_id": tax_id}, headers=h).json()
    assert page["total"] == 1
    ev = page["items"][0]
    assert ev["action"] == "create" and ev["entity"] == "md.tax_code"
    assert ev["actor_name"] and ev["device_name"]
    assert ev["after"]["code"] == "AUD8"

    # filters: record type prefix, action, text inside the change
    for params in ({"entity": "md"}, {"entity": "md.tax_code"}, {"q": "Audit test 8"}):
        ids = [
            e["entity_id"]
            for e in client.get(f"{API}/audit/events", params=params, headers=h).json()["items"]
        ]
        assert tax_id in ids, params
    other = client.get(
        f"{API}/audit/events", params={"entity": "sal", "q": "AUD8"}, headers=h
    ).json()
    assert other["total"] == 0

    facets = client.get(f"{API}/audit/facets", headers=h).json()
    assert "create" in facets["actions"] and "md.tax_code" in facets["entities"]


def test_other_company_and_staff_without_permission_see_nothing(world, client) -> None:
    h = login(client, "boss")
    client.post(
        f"{API}/tax-codes",
        json={"code": "AUD6", "name": "Audit test 6%", "rate": "6", "einvoice_tax_type": "01"},
        headers=h,
    )
    other = TestClient(app)
    page = client.get(f"{API}/audit/events", params={"q": "AUD6"}, headers=login(other, "boss2"))
    assert page.status_code == 200 and page.json()["total"] == 0
    cashier = TestClient(app)
    r = cashier.get(f"{API}/audit/events", headers=login(cashier, "cashier1"))
    assert r.status_code == 403
