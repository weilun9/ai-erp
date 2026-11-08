import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.conftest import login

pytestmark = pytest.mark.db

MIGHTY = {
    "code": "MIGHTY",
    "name": "Mighty Bakery Sdn Bhd",
    "tin": "C20880050010",
    "id_type": "BRN",
    "id_no": "201901000005",
    "sst_no": "W10-1808-31000000",
    "msic_code": "10711",
    "address1": "Lot 1, Jalan Industri",
    "city": "Johor Bahru",
    "postcode": "81100",
    "state_code": "01",
    "term_days": 30,
    "profile": {
        "invoice_no_pattern": r"^IJB\d+$",
        "cn_no_pattern": r"^CJB\d+$",
        "refund_no_pattern": r"^RJB\d+$",
        "sst_marks": {"*": "ST5", "#": "NA"},
        "settlement_notes": "CN offsets same-day invoice; balance paid in cash",
    },
}

CUSTOMER = {
    "code": "C001",
    "name": "Kedai Runcit Ali",
    "tin": "IG12345678090",
    "id_type": "NRIC",
    "id_no": "900101015555",
    "phone": "+60123456789",
    "email": "ali@example.com",
    "address1": "12 Jalan Mawar",
    "postcode": "81200",
    "city": "Johor Bahru",
    "state_code": "01",
    "credit_limit": "5000",
}


def test_supplier_with_einvoice_fields_and_profile(world, client) -> None:
    h = login(client, "clerk1")
    resp = client.post("/api/v1/suppliers", json=MIGHTY, headers=h)
    assert resp.status_code == 201, resp.text
    s = resp.json()
    assert s["country_code"] == "MYS"
    assert s["profile"]["sst_marks"] == {"*": "ST5", "#": "NA"}
    assert s["profile"]["cn_no_pattern"] == r"^CJB\d+$"


@pytest.mark.parametrize(
    "field,value",
    [
        ("tin", "12345"),
        ("msic_code", "1071"),
        ("state_code", "99"),
        ("id_no", None),  # id_type without id_no
    ],
)
def test_supplier_einvoice_validation(world, client, field, value) -> None:
    body = {**MIGHTY, field: value}
    resp = client.post("/api/v1/suppliers", json=body, headers=login(client, "boss"))
    assert resp.status_code == 400, resp.text


def test_bad_regex_in_profile_rejected(world, client) -> None:
    body = {**MIGHTY, "profile": {"invoice_no_pattern": "IJB(("}}
    resp = client.post("/api/v1/suppliers", json=body, headers=login(client, "boss"))
    assert resp.status_code == 400


def test_general_public_tin_accepted(world, client) -> None:
    body = {"code": "WALKIN", "name": "Walk-in customer", "tin": "EI00000000010"}
    resp = client.post("/api/v1/customers", json=body, headers=login(client, "boss"))
    assert resp.status_code == 201, resp.text


def test_customer_pii_and_credit_hidden_from_cashier(world, client) -> None:
    c = client.post("/api/v1/customers", json=CUSTOMER, headers=login(client, "boss")).json()
    assert c["phone"] == "+60123456789" and c["credit_limit"] == "5000.0000"

    pos = TestClient(app)
    tag_h = login(pos, "acct")  # accountant: sees pii + finance via field.*
    seen = pos.get(f"/api/v1/customers/{c['id']}", headers=tag_h).json()
    assert seen["phone"] == "+60123456789"

    # Give the cashier customer read access via a custom tag, still without pii/finance.
    boss_h = login(client, "boss")
    tag = client.post(
        "/api/v1/tags",
        json={
            "code": "pos_customer",
            "name": "POS customer lookup",
            "color": "#00aa00",
            "permissions": ["md.customer.read"],
        },
        headers=boss_h,
    ).json()
    users = client.get("/api/v1/users", headers=boss_h).json()["items"]
    cashier = next(u for u in users if u["username"] == "cashier1")
    tag_ids = [t["id"] for t in cashier["tags"]] + [tag["id"]]
    client.put(f"/api/v1/users/{cashier['id']}/tags", json={"tag_ids": tag_ids}, headers=boss_h)

    till = TestClient(app)
    ch = login(till, "cashier1")
    view = till.get(f"/api/v1/customers/{c['id']}", headers=ch).json()
    assert view["name"] == "Kedai Runcit Ali"
    for hidden in ("phone", "email", "id_no", "address1", "postcode", "credit_limit"):
        assert hidden not in view, hidden
    assert view["city"] == "Johor Bahru"


def test_cannot_write_pii_without_permission(world, client) -> None:
    boss_h = login(client, "boss")
    tag = client.post(
        "/api/v1/tags",
        json={
            "code": "cust_editor",
            "name": "Customer editor",
            "color": "#111111",
            "permissions": ["md.customer.read", "md.customer.manage"],
        },
        headers=boss_h,
    ).json()
    client.post(
        "/api/v1/users",
        json={
            "username": "cust_ed",
            "display_name": "CE",
            "password": "Test-password-123",
            "tag_ids": [tag["id"]],
        },
        headers=boss_h,
    )
    pc = TestClient(app)
    h = login(pc, "cust_ed")
    ok = pc.post("/api/v1/customers", json={"code": "C9", "name": "No contact"}, headers=h)
    assert ok.status_code == 201
    bad = pc.post("/api/v1/customers", json={"code": "C10", "name": "x", "phone": "123"}, headers=h)
    assert bad.status_code == 403
    assert bad.json()["error"]["details"]["required"] == ["field.pii.read"]


def test_party_update_and_version(world, client) -> None:
    h = login(client, "boss")
    s = client.post("/api/v1/suppliers", json=MIGHTY, headers=h).json()
    url = f"/api/v1/suppliers/{s['id']}"
    resp = client.patch(url, json={"version": 1, "term_days": 14, "email": None}, headers=h)
    assert resp.status_code == 200 and resp.json()["term_days"] == 14
    assert client.patch(url, json={"version": 1, "term_days": 7}, headers=h).status_code == 409


def test_supplier_search(world, client) -> None:
    h = login(client, "boss")
    client.post("/api/v1/suppliers", json=MIGHTY, headers=h)
    assert client.get("/api/v1/suppliers", params={"q": "mighty"}, headers=h).json()["total"] == 1
    by_tin = client.get("/api/v1/suppliers", params={"q": "C20880050010"}, headers=h).json()
    assert by_tin["total"] == 1
