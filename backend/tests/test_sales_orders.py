"""Step 32: sales orders (confirm, deliver / invoice line by line, close, on-order qty for
customers) and customer debit notes."""

import pytest

from tests import test_sales
from tests.test_sales import TODAY, _balance, _do, _doc, milo

pytestmark = pytest.mark.db

store = test_sales.store  # MILO: 48 pcs at HQ, PCS 2.50 / CTN of 24 at 55.00; customer ACME
SAL = "/api/v1/sales"


def _confirmed_so(client, s, lines):
    d = _doc(client, s, "sales-order", lines)
    assert d.status_code == 201, d.text
    d = _do(client, s, "sales-order", d.json(), "save")
    assert d.status_code == 200, d.text
    d = _do(client, s, "sales-order", d.json(), "post")
    assert d.status_code == 200, d.text
    return d.json()


def _from_so(client, s, target, so, pick=None):
    body = {"source_kind": "sales-order", "source_id": so["id"]}
    if pick is not None:
        body["lines"] = pick
    return client.post(f"{SAL}/{target}/convert", json=body, headers=s["h"])


def _overview_hq(client, s):
    ov = client.get(f"/api/v1/items/{s['milo']}/overview", headers=s["h"]).json()
    return next(r for r in ov["locations"] if r["location_code"] == "HQ")


def test_quotation_becomes_an_order_that_moves_no_stock(store, client) -> None:
    q = _doc(client, store, "quotation", [milo(store, "2", "CTN")]).json()
    q = _do(client, store, "quotation", q, "save").json()
    so = client.post(
        f"{SAL}/sales-order/convert",
        json={"source_kind": "quotation", "source_id": q["id"]},
        headers=store["h"],
    )
    assert so.status_code == 201, so.text
    so = so.json()
    so = _do(client, store, "sales-order", so, "save").json()
    before = _balance(client, store, store["milo"])
    posted = _do(client, store, "sales-order", so, "post")
    assert posted.status_code == 200, posted.text
    assert posted.json()["doc_no"].startswith("SO")
    assert _balance(client, store, store["milo"]) == before
    assert _overview_hq(client, store)["on_sales_order"] == "48.0000"


def test_deliver_part_then_invoice_the_rest(store, client) -> None:
    so = _confirmed_so(client, store, [milo(store, "2", "CTN")])
    line = so["lines"][0]["id"]
    do = _from_so(client, store, "delivery-order", so, [{"source_line_id": line, "qty": "1"}])
    assert do.status_code == 201, do.text
    do = do.json()
    assert (do["lines"][0]["so_line_id"], do["lines"][0]["unit_price"]) == (line, "55.0000")
    do = _do(client, store, "delivery-order", do, "save").json()
    assert do["warnings"] == [] or all(w["code"] != "OVER_SO" for w in do["warnings"])
    assert _do(client, store, "delivery-order", do, "post").status_code == 200

    got = client.get(f"{SAL}/sales-order/{so['id']}", headers=store["h"]).json()
    assert got["lines"][0]["fulfilled_qty"] == "1.0000"
    assert _overview_hq(client, store)["on_sales_order"] == "24.0000"

    inv = _from_so(client, store, "sales-invoice", so).json()
    assert inv["lines"][0]["qty"] == "1.0000"
    assert _do(client, store, "sales-invoice", inv, "save").status_code == 200
    assert _overview_hq(client, store)["on_sales_order"] == "0"
    assert _from_so(client, store, "delivery-order", so).status_code == 422

    so = client.get(f"{SAL}/sales-order/{so['id']}", headers=store["h"]).json()
    blocked = _do(client, store, "sales-order", so, "void", reason="customer cancelled")
    assert blocked.status_code == 409  # the DO and the invoice must be voided first


def test_more_than_ordered_is_warned(store, client) -> None:
    so = _confirmed_so(client, store, [milo(store, "1", "CTN")])
    do = _from_so(
        client, store, "delivery-order", so, [{"source_line_id": so["lines"][0]["id"], "qty": "2"}]
    ).json()
    saved = _do(client, store, "delivery-order", do, "save").json()
    assert "OVER_SO" in [w["code"] for w in saved["warnings"]]


def test_close_and_reopen(store, client) -> None:
    so = _confirmed_so(client, store, [milo(store, "1", "CTN")])
    closed = client.post(
        f"{SAL}/sales-order/{so['id']}/close", json={"version": so["version"]}, headers=store["h"]
    )
    assert closed.status_code == 200 and closed.json()["closed_at"]
    assert _overview_hq(client, store)["on_sales_order"] == "0"
    assert _from_so(client, store, "delivery-order", so).status_code == 422
    reopened = client.post(
        f"{SAL}/sales-order/{so['id']}/close",
        json={"version": closed.json()["version"], "reopen": True},
        headers=store["h"],
    )
    assert reopened.json()["closed_at"] is None


def test_customer_debit_note_is_owed_and_paid_like_an_invoice(store, client) -> None:
    h = store["h"]
    dn = client.post(
        "/api/v1/customer-notes/customer-debit-note",
        json={
            "doc_date": TODAY,
            "customer_id": store["customer"],
            "location_id": store["hq"],
            "lines": [{"description": "Delivery charge", "amount": "20"}],
        },
        headers=h,
    )
    assert dn.status_code == 201, dn.text
    dn = dn.json()
    base = "/api/v1/customer-notes/customer-debit-note"
    dn = client.post(f"{base}/{dn['id']}/save", json={"version": dn["version"]}, headers=h).json()
    assert dn["doc_no"].startswith("CDN") and dn["due_date"]
    dn = client.post(f"{base}/{dn['id']}/post", json={"version": dn["version"]}, headers=h)
    assert dn.status_code == 200, dn.text
    dn = dn.json()
    assert dn["open"] == "20.0000"

    r = client.post(
        "/api/v1/finance/receipt",
        json={
            "doc_date": TODAY,
            "customer_id": store["customer"],
            "location_id": store["hq"],
            "method": "CASH",
            "amount": "20",
            "lines": [],
        },
        headers=h,
    ).json()
    r = client.post(
        f"/api/v1/finance/receipt/{r['id']}/save", json={"version": r["version"]}, headers=h
    ).json()
    p = client.post(
        f"/api/v1/finance/receipt/{r['id']}/post", json={"version": r["version"]}, headers=h
    )
    assert p.status_code == 200, p.text
    assert client.get(f"{base}/{dn['id']}", headers=h).json()["open"] == "0.0000"

    js = client.get("/api/v1/accounting/journals", params={"source_type": "CDN"}, headers=h).json()
    assert js["total"] == 1
