import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.session import SessionLocal
from app.main import app
from app.models import EventLog
from tests.conftest import login

pytestmark = pytest.mark.db


def _tax_id(client, h, code) -> str:
    rows = client.get("/api/v1/tax-codes", headers=h).json()["items"]
    return next(r["id"] for r in rows if r["code"] == code)


def _bread(client, h, **over):
    body = {
        "code": "BREAD400",
        "name": "White bread 400g",
        "base_uom": "PCS",
        "sales_tax_code_id": _tax_id(client, h, "NA"),
        "uoms": [
            {"uom": "PCS", "factor": "1", "selling_price": "3.20", "purchase_price": "2.4500"},
            {"uom": "CTN", "factor": "24", "selling_price": "72.00", "purchase_price": "55.00"},
        ],
        "barcodes": [
            {"barcode": "955634531007", "uom": "PCS"},  # stored the DynaMod way (12 digits)
            {"barcode": "9556345310089", "uom": "CTN"},
        ],
    }
    body.update(over)
    return client.post("/api/v1/items", json=body, headers=h)


def test_malaysia_tax_codes_and_units_are_seeded(world, client) -> None:
    h = login(client, "boss")
    taxes = {t["code"]: t for t in client.get("/api/v1/tax-codes", headers=h).json()["items"]}
    assert taxes["ST10"]["rate"] == "10.0000"
    assert taxes["ST10"]["einvoice_tax_type"] == "01"
    assert taxes["SV8"]["einvoice_tax_type"] == "02"
    assert taxes["EX"]["einvoice_tax_type"] == "E"
    uoms = {u["code"]: u for u in client.get("/api/v1/uoms", headers=h).json()["items"]}
    assert uoms["PCS"]["einvoice_code"] == "C62"
    assert uoms["KG"]["einvoice_code"] == "KGM"


def test_create_item_and_lookup_by_any_barcode_length(world, client) -> None:
    h = login(client, "boss")
    resp = _bread(client, h)
    assert resp.status_code == 201, resp.text
    item = resp.json()
    assert item["version"] == 1
    assert item["einvoice_classification"] == "022"
    assert {b["ean13"] for b in item["barcodes"]} == {"9556345310072", "9556345310089"}

    for scanned in ("9556345310072", "955634531007"):
        hit = client.get("/api/v1/items/lookup", params={"barcode": scanned}, headers=h).json()
        assert hit["ambiguous"] is False
        assert [x["item"]["code"] for x in hit["hits"]] == ["BREAD400"]
        assert hit["hits"][0]["matched_uom"] == "PCS"
    assert hit["hits"][0]["exact"] is True

    miss = client.get("/api/v1/items/lookup", params={"barcode": "9999999999999"}, headers=h)
    assert miss.json()["hits"] == []


def test_upc_11_digit_scan_finds_item(world, client) -> None:
    h = login(client, "boss")
    resp = _bread(
        client,
        h,
        code="SODA",
        name="Soda can",
        barcodes=[{"barcode": "036000291452", "uom": "PCS"}],
    )
    assert resp.status_code == 201, resp.text
    for scanned in ("03600029145", "0036000291452", "036000291452"):
        hits = client.get("/api/v1/items/lookup", params={"barcode": scanned}, headers=h).json()
        assert [x["item"]["code"] for x in hits["hits"]] == ["SODA"], scanned


def test_same_product_code_in_another_length_is_a_duplicate(world, client) -> None:
    h = login(client, "boss")
    assert _bread(client, h).status_code == 201
    dup = _bread(
        client,
        h,
        code="BREAD-DUP",
        barcodes=[{"barcode": "9556345310072", "uom": "PCS"}],  # 13-digit form of the 12
    )
    assert dup.status_code == 409
    assert dup.json()["error"]["details"]["item_code"] == "BREAD400"


def test_search_by_name_and_by_barcode(world, client) -> None:
    h = login(client, "boss")
    _bread(client, h)
    by_name = client.get("/api/v1/items", params={"q": "white"}, headers=h).json()
    assert by_name["total"] == 1
    by_code = client.get("/api/v1/items", params={"q": "9556345310072"}, headers=h).json()
    assert by_code["total"] == 1


def test_cashier_never_sees_cost(world, client) -> None:
    _bread(client, login(client, "boss"))
    cashier = TestClient(app)
    h = login(cashier, "cashier1")
    item = cashier.get("/api/v1/items", headers=h).json()["items"][0]
    for u in item["uoms"]:
        assert "purchase_price" not in u
        assert "selling_price" in u
    hit = cashier.get("/api/v1/items/lookup", params={"barcode": "955634531007"}, headers=h)
    assert "purchase_price" not in hit.json()["hits"][0]["item"]["uoms"][0]


def test_owner_sees_cost(world, client) -> None:
    item = _bread(client, login(client, "boss")).json()
    pcs = next(u for u in item["uoms"] if u["uom"] == "PCS")
    assert pcs["purchase_price"] == "2.4500"


def test_base_unit_must_have_factor_one(world, client) -> None:
    h = login(client, "boss")
    resp = _bread(client, h, uoms=[{"uom": "PCS", "factor": "2"}], barcodes=[])
    assert resp.status_code == 400


def test_barcode_unit_must_belong_to_item(world, client) -> None:
    h = login(client, "boss")
    resp = _bread(client, h, barcodes=[{"barcode": "1111111111116", "uom": "KG"}])
    assert resp.status_code == 400


def test_update_needs_current_version(world, client) -> None:
    h = login(client, "boss")
    item = _bread(client, h).json()
    url = f"/api/v1/items/{item['id']}"
    ok = client.patch(url, json={"version": 1, "name": "White bread 400 g"}, headers=h)
    assert ok.status_code == 200 and ok.json()["version"] == 2
    stale = client.patch(url, json={"version": 1, "name": "x"}, headers=h)
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "VERSION_CONFLICT"


def test_null_clears_optional_field_but_not_required(world, client) -> None:
    h = login(client, "boss")
    item = _bread(client, h, short_name="BREAD").json()
    url = f"/api/v1/items/{item['id']}"
    cleared = client.patch(url, json={"version": 1, "short_name": None}, headers=h)
    assert cleared.json()["short_name"] is None
    bad = client.patch(url, json={"version": 2, "name": None}, headers=h)
    assert bad.status_code == 400


def test_user_without_cost_permission_keeps_existing_cost(world, client) -> None:
    # A custom tag that may edit items but not see cost.
    boss_h = login(client, "boss")
    tag = client.post(
        "/api/v1/tags",
        json={
            "code": "item_editor",
            "name": "Item editor",
            "color": "#123456",
            "permissions": ["md.item.read", "md.item.manage", "md.setup.read"],
        },
        headers=boss_h,
    ).json()
    client.post(
        "/api/v1/users",
        json={
            "username": "editor",
            "display_name": "Editor",
            "password": "Test-password-123",
            "tag_ids": [tag["id"]],
        },
        headers=boss_h,
    )
    item = _bread(client, boss_h).json()

    pc = TestClient(app)
    eh = login(pc, "editor")
    new_uoms = [
        {"uom": "PCS", "factor": "1", "selling_price": "3.50"},
        {"uom": "CTN", "factor": "24", "selling_price": "80.00"},
    ]
    resp = pc.patch(
        f"/api/v1/items/{item['id']}", json={"version": 1, "uoms": new_uoms}, headers=eh
    )
    assert resp.status_code == 200, resp.text
    assert "purchase_price" not in resp.json()["uoms"][0]

    sneaky = pc.patch(
        f"/api/v1/items/{item['id']}",
        json={"version": 2, "uoms": [{**new_uoms[0], "purchase_price": "0.01"}, new_uoms[1]]},
        headers=eh,
    )
    assert sneaky.status_code == 403

    owner_view = client.get(f"/api/v1/items/{item['id']}", headers=boss_h).json()
    pcs = next(u for u in owner_view["uoms"] if u["uom"] == "PCS")
    assert pcs["selling_price"] == "3.5000"
    assert pcs["purchase_price"] == "2.4500"  # untouched


def test_item_changes_are_audited_with_request_id(world, client) -> None:
    h = login(client, "boss")
    resp = _bread(client, h)
    rid = resp.headers["X-Request-ID"]
    item = resp.json()
    client.patch(
        f"/api/v1/items/{item['id']}", json={"version": 1, "name": "Roti putih"}, headers=h
    )
    with SessionLocal() as db:
        events = db.scalars(
            select(EventLog).where(EventLog.entity_id == item["id"]).order_by(EventLog.id)
        ).all()
    assert [e.action for e in events] == ["create", "update"]
    assert events[0].request_id == rid
    assert events[0].actor_type == "user"
    assert events[1].before == {"name": "White bread 400g", "version": 1}
    assert events[1].after == {"name": "Roti putih", "version": 2}


def test_clerk_cannot_edit_tax_codes(world, client) -> None:
    h = login(client, "clerk1")
    assert client.get("/api/v1/tax-codes", headers=h).status_code == 200
    resp = client.post(
        "/api/v1/tax-codes",
        json={"code": "X1", "name": "x", "rate": "1", "einvoice_tax_type": "01"},
        headers=h,
    )
    assert resp.status_code == 403


def test_tax_code_type_must_be_a_myinvois_type(world, client) -> None:
    resp = client.post(
        "/api/v1/tax-codes",
        json={"code": "ST8", "name": "x", "rate": "8", "einvoice_tax_type": "99"},
        headers=login(client, "boss"),
    )
    assert resp.status_code == 400
    assert "01" in resp.json()["error"]["details"]["allowed"]


def test_items_are_company_scoped(world, client) -> None:
    item = _bread(client, login(client, "boss")).json()
    other = TestClient(app)
    h2 = login(other, "boss2")
    assert other.get(f"/api/v1/items/{item['id']}", headers=h2).status_code == 404
    hits = other.get("/api/v1/items/lookup", params={"barcode": "955634531007"}, headers=h2)
    assert hits.json()["hits"] == []
