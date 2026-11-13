"""Step 27: item grouping, price levels, minimum price, per-shop reorder settings,
item overview (stock per shop, suppliers, purchase prices) and the reorder report."""

import pytest

from tests import test_sales
from tests.conftest import login
from tests.test_sales import _do, _doc, milo

store = test_sales.store  # the shared fixture

pytestmark = pytest.mark.db


def _attr(client, h, kind, code):
    r = client.post(
        "/api/v1/item-attributes", json={"kind": kind, "code": code, "name": code}, headers=h
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _item(client, s):
    return client.get(f"/api/v1/items/{s['milo']}", headers=s["h"]).json()


def _patch_item(client, s, **v):
    it = _item(client, s)
    r = client.patch(
        f"/api/v1/items/{s['milo']}", json={"version": it["version"], **v}, headers=s["h"]
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_grouping_attributes_are_checked_and_searchable(store, client) -> None:
    h = store["h"]
    nestle = _attr(client, h, "BRAND", "NESTLE")
    drinks = _attr(client, h, "GROUP", "DRINKS")
    dup = client.post(
        "/api/v1/item-attributes", json={"kind": "BRAND", "code": "nestle", "name": "x"}, headers=h
    )
    assert dup.status_code == 409

    it = _item(client, store)
    wrong = client.patch(
        f"/api/v1/items/{store['milo']}",
        json={"version": it["version"], "brand_id": drinks},
        headers=h,
    )
    assert wrong.status_code == 400

    out = _patch_item(
        client,
        store,
        brand_id=nestle,
        group_id=drinks,
        description_2="Chocolate malt",
        sales_note="Promo till Dec",
    )
    assert out["brand_id"] == nestle and out["description_2"] == "Chocolate malt"
    found = client.get("/api/v1/items", params={"attribute_id": nestle}, headers=h).json()
    assert [i["code"] for i in found["items"]] == ["MILO"]
    brands = client.get("/api/v1/item-attributes", params={"kind": "BRAND"}, headers=h).json()
    assert [b["code"] for b in brands["items"]] == ["NESTLE"]


def test_customer_price_level_picks_the_level_price(store, client) -> None:
    h = store["h"]
    _patch_item(
        client,
        store,
        uoms=[
            {"uom": "PCS", "factor": "1", "selling_price": "2.50", "price_3": "2.10"},
            {"uom": "CTN", "factor": "24", "selling_price": "55.00"},
        ],
    )
    cust = client.get(f"/api/v1/customers/{store['customer']}", headers=h).json()
    assert cust["price_level"] == 1
    r = client.patch(
        f"/api/v1/customers/{store['customer']}",
        json={"version": cust["version"], "price_level": 3},
        headers=h,
    )
    assert r.status_code == 200, r.text

    d = _doc(client, store, "quotation", [milo(store, "1"), milo(store, "1", "CTN")]).json()
    pcs, ctn = d["lines"]
    assert (pcs["price_source"], pcs["unit_price"]) == ("LIST", "2.1000")
    assert ctn["unit_price"] == "55.0000"  # no level-3 price on the carton: price 1


def test_selling_below_minimum_price_warns(store, client) -> None:
    _patch_item(client, store, min_selling_price="2.30")
    d = _doc(client, store, "quotation", [milo(store, "2", unit_price="2.00")]).json()
    r = _do(client, store, "quotation", d, "save")
    assert r.status_code == 200, r.text
    warn = {w["code"]: w for w in r.json()["warnings"]}
    assert warn["BELOW_MIN_PRICE"]["lines"] == [1]


def test_overview_shows_stock_per_shop_suppliers_and_prices(store, client, world) -> None:
    ov = client.get(f"/api/v1/items/{store['milo']}/overview", headers=store["h"]).json()
    by_loc = {r["location_code"]: r for r in ov["locations"]}
    assert by_loc["HQ"]["qty"] == "48.0000" and by_loc["S2"]["qty"] == "0"
    assert by_loc["HQ"]["avg_cost"] == "1.5000"
    assert ov["total_qty"] == "48.0000"
    (sup,) = ov["suppliers"]
    assert (sup["supplier_code"], sup["documents"], sup["base_qty"]) == ("NESTLE", 2, "48.0000")
    assert {r["unit_price"] for r in ov["purchase_history"]} == {"24.0000", "48.0000"}

    cashier = login(client, "cashier1")
    hidden = client.get(f"/api/v1/items/{store['milo']}/overview", headers=cashier).json()
    assert hidden["cost_hidden"] is True and hidden["purchase_history"] == []
    assert [r["location_code"] for r in hidden["locations"]] == ["S2"]
    assert "avg_cost" not in hidden["locations"][0]
    assert hidden["suppliers"] == []  # those deliveries went to HQ, which she cannot see


def test_reorder_levels_per_shop_and_report(store, client, world) -> None:
    h = store["h"]
    _patch_item(client, store, reorder_level="50", max_qty="120")
    rows = client.get("/api/v1/reports/reorder", headers=h).json()["rows"]
    hq = next(r for r in rows if r["location"] == "HQ")
    assert (hq["qty"], hq["suggested_qty"]) == ("48.0000", "72.0000")  # up to max 120
    assert {r["location"] for r in rows} == {"HQ", "S2", "S3"}

    # HQ keeps a smaller level of its own and a shelf; it drops off the report.
    ov = client.put(
        f"/api/v1/items/{store['milo']}/locations",
        json=[{"location_id": store["hq"], "reorder_level": "10", "shelf": "A-03"}],
        headers=h,
    )
    assert ov.status_code == 200, ov.text
    hq_row = next(r for r in ov.json()["locations"] if r["location_code"] == "HQ")
    assert hq_row["effective"]["reorder_level"] == "10.0000"
    assert hq_row["effective"]["max_qty"] == "120.0000"  # still the item's
    assert hq_row["shelf"] == "A-03" and hq_row["below_reorder"] is False
    rows = client.get("/api/v1/reports/reorder", headers=h).json()["rows"]
    assert "HQ" not in {r["location"] for r in rows}

    # "Update reorder level to all locations" overwrites the shop's own value.
    r = client.post(
        "/api/v1/items/copy-reorder-to-locations",
        json={"item_id": store["milo"], "fields": ["reorder_level"]},
        headers=h,
    )
    assert r.status_code == 200 and r.json()["updated"] == 3
    rows = client.get("/api/v1/reports/reorder", headers=h).json()["rows"]
    assert "HQ" in {r["location"] for r in rows}

    bad = client.put(
        f"/api/v1/items/{store['milo']}/locations",
        json=[{"location_id": str(world.other_company_loc_id), "reorder_level": "1"}],
        headers=h,
    )
    assert bad.status_code == 404
    clerk = login(client, "clerk1")
    assert client.get("/api/v1/reports/reorder", headers=clerk).status_code == 200
