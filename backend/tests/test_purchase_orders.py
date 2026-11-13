"""Step 28: purchase orders - issue, receive into GRNs (partial, over-delivery warned),
close, on-order quantity in the item overview and the reorder report."""

import pytest

from tests import test_sales
from tests.test_sales import TODAY, _balance

pytestmark = pytest.mark.db

store = test_sales.store  # the shared fixture: MILO, 48 in stock at HQ, supplier NESTLE
PO = "/api/v1/purchase/purchase-order"
GRN = "/api/v1/purchase/grn"


def _act(client, s, base, doc, action, **extra):
    r = client.post(
        f"{base}/{doc['id']}/{action}", json={"version": doc["version"], **extra}, headers=s["h"]
    )
    return r


def _issued_po(client, s, lines, **over):
    body = {
        "doc_date": TODAY,
        "supplier_id": s["supplier"],
        "location_id": s["hq"],
        "lines": lines,
        **over,
    }
    d = client.post(PO, json=body, headers=s["h"])
    assert d.status_code == 201, d.text
    d = _act(client, s, PO, d.json(), "save")
    assert d.status_code == 200, d.text
    d = _act(client, s, PO, d.json(), "post")
    assert d.status_code == 200, d.text
    return d.json()


def _ctn(s, qty, **extra):
    return {"item_id": s["milo"], "uom": "CTN", "qty": qty, **extra}


def _post_grn(client, s, grn, ref):
    g = client.patch(
        f"{GRN}/{grn['id']}",
        json={"version": grn["version"], "supplier_ref_no": ref},
        headers=s["h"],
    ).json()
    saved = _act(client, s, GRN, g, "save")
    assert saved.status_code == 200, saved.text
    posted = _act(client, s, GRN, saved.json(), "post")
    assert posted.status_code == 200, posted.text
    return saved.json()["warnings"], posted.json()


def test_po_price_defaults_to_last_paid_else_must_be_typed(store, client) -> None:
    d = client.post(
        PO,
        json={
            "doc_date": TODAY,
            "supplier_id": store["supplier"],
            "location_id": store["hq"],
            "lines": [_ctn(store, "2"), {"item_id": store["soap"], "uom": "PCS", "qty": "1"}],
        },
        headers=store["h"],
    ).json()
    milo, soap = d["lines"]
    # last GRN paid 48.00 a carton = 2.00 per piece
    assert (milo["price_source"], milo["unit_price"]) == ("LAST", "48.0000")
    assert soap["price_source"] == "MISSING"  # never bought, no purchase price
    assert _act(client, store, PO, d, "save").status_code == 422

    before = _balance(client, store, store["milo"])
    po = _issued_po(client, store, [_ctn(store, "5", unit_price="50.00")])
    assert po["doc_no"].startswith("PO") and po["status"] == "POSTED"
    assert _balance(client, store, store["milo"]) == before


def test_receive_partially_then_the_rest(store, client) -> None:
    po = _issued_po(client, store, [_ctn(store, "5", unit_price="50.00", discount_pct="10")])
    line = po["lines"][0]["id"]

    grn = client.post(
        f"{GRN}/receive-po",
        json={"po_id": po["id"], "lines": [{"source_line_id": line, "qty": "2"}]},
        headers=store["h"],
    )
    assert grn.status_code == 201, grn.text
    g = grn.json()
    gl = g["lines"][0]
    assert (gl["po_line_id"], gl["qty"], gl["unit_price"], gl["discount_pct"]) == (
        line,
        "2.0000",
        "50.0000",
        "10.0000",
    )
    warnings, _ = _post_grn(client, store, g, "INV-P1")
    assert warnings == []

    got = client.get(f"{PO}/{po['id']}", headers=store["h"]).json()
    assert got["lines"][0]["received_qty"] == "2.0000"
    ov = client.get(f"/api/v1/items/{store['milo']}/overview", headers=store["h"]).json()
    hq = next(r for r in ov["locations"] if r["location_code"] == "HQ")
    assert hq["on_order"] == "72.0000"  # 3 CTN of 24 still to come

    rest = client.post(f"{GRN}/receive-po", json={"po_id": po["id"]}, headers=store["h"]).json()
    assert rest["lines"][0]["qty"] == "3.0000"
    _post_grn(client, store, rest, "INV-P2")
    again = client.post(f"{GRN}/receive-po", json={"po_id": po["id"]}, headers=store["h"])
    assert again.status_code == 422
    ov = client.get(f"/api/v1/items/{store['milo']}/overview", headers=store["h"]).json()
    assert ov["total_on_order"] == "0"


def test_over_delivery_is_warned_and_po_cannot_be_voided_after(store, client) -> None:
    po = _issued_po(client, store, [_ctn(store, "1", unit_price="50.00")])
    g = client.post(
        f"{GRN}/receive-po",
        json={"po_id": po["id"], "lines": [{"source_line_id": po["lines"][0]["id"], "qty": "2"}]},
        headers=store["h"],
    ).json()
    warnings, _ = _post_grn(client, store, g, "INV-OVER")
    assert [w["code"] for w in warnings] == ["OVER_PO"]
    assert warnings[0]["lines"][0]["ordered_left"] == "1.0000"

    po = client.get(f"{PO}/{po['id']}", headers=store["h"]).json()
    r = _act(client, store, PO, po, "void", reason="mistake")
    assert r.status_code == 422


def test_close_stops_on_order_and_receiving(store, client) -> None:
    po = _issued_po(client, store, [_ctn(store, "4", unit_price="50.00")])
    closed = _act(client, store, PO, po, "close")
    assert closed.status_code == 200 and closed.json()["closed_at"]
    ov = client.get(f"/api/v1/items/{store['milo']}/overview", headers=store["h"]).json()
    assert ov["total_on_order"] == "0"
    r = client.post(f"{GRN}/receive-po", json={"po_id": po["id"]}, headers=store["h"])
    assert r.status_code == 422
    reopened = _act(client, store, PO, closed.json(), "close", reopen=True)
    assert reopened.json()["closed_at"] is None
    ov = client.get(f"/api/v1/items/{store['milo']}/overview", headers=store["h"]).json()
    assert ov["total_on_order"] == "96.0000"


def test_reorder_suggestion_takes_off_what_is_on_order(store, client) -> None:
    h = store["h"]
    it = client.get(f"/api/v1/items/{store['milo']}", headers=h).json()
    client.patch(
        f"/api/v1/items/{store['milo']}",
        json={"version": it["version"], "reorder_level": "50", "max_qty": "120"},
        headers=h,
    )
    _issued_po(client, store, [_ctn(store, "1", unit_price="50.00")])
    rows = client.get(
        "/api/v1/reports/reorder", params={"location_id": store["hq"]}, headers=h
    ).json()["rows"]
    (hq,) = rows
    assert (hq["on_order"], hq["suggested_qty"]) == ("24.0000", "48.0000")  # 120-48-24
    assert hq["last_supplier"] == "NESTLE"


def test_grn_cannot_use_another_suppliers_po_line(store, client) -> None:
    po = _issued_po(client, store, [_ctn(store, "1", unit_price="50.00")])
    other = client.post(
        "/api/v1/suppliers", json={"code": "OTHER", "name": "Other"}, headers=store["h"]
    ).json()["id"]
    r = client.post(
        GRN,
        json={
            "doc_date": TODAY,
            "supplier_id": other,
            "location_id": store["hq"],
            "lines": [_ctn(store, "1", unit_price="50", po_line_id=po["lines"][0]["id"])],
        },
        headers=store["h"],
    )
    assert r.status_code == 400
