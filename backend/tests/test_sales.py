from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.session import SessionLocal
from app.main import app
from app.models import StockLedger
from tests.conftest import login
from tests.test_purchasing import _parallel

pytestmark = pytest.mark.db

TODAY = date.today().isoformat()


@pytest.fixture
def store(world, client):
    """Owner logged in; MILO (PCS 2.50 / CTN of 24 at 55.00), SOAP (no selling price);
    48 MILO in stock at HQ with average cost 1.50; customer ACME (30 days, limit 100)."""
    h = login(client, "boss")
    taxes = {t["code"]: t["id"] for t in client.get("/api/v1/tax-codes", headers=h).json()["items"]}

    def item(code, uoms):
        r = client.post(
            "/api/v1/items",
            json={
                "code": code,
                "name": code,
                "base_uom": "PCS",
                "sales_tax_code_id": taxes["NA"],
                "uoms": uoms,
            },
            headers=h,
        )
        assert r.status_code == 201, r.text
        return r.json()["id"]

    milo = item(
        "MILO",
        [
            {"uom": "PCS", "factor": "1", "selling_price": "2.50"},
            {"uom": "CTN", "factor": "24", "selling_price": "55.00"},
        ],
    )
    soap = item("SOAP", [{"uom": "PCS", "factor": "1"}])
    supplier = client.post(
        "/api/v1/suppliers", json={"code": "NESTLE", "name": "Nestle"}, headers=h
    ).json()["id"]
    customer = client.post(
        "/api/v1/customers",
        json={"code": "ACME", "name": "Acme Mart", "term_days": 30, "credit_limit": "100"},
        headers=h,
    )
    assert customer.status_code == 201, customer.text
    s = {
        "h": h,
        "milo": milo,
        "soap": soap,
        "supplier": supplier,
        "customer": customer.json()["id"],
        "hq": str(world.hq_id),
    }
    for ref, price in (("INV-A", "24.00"), ("INV-B", "48.00")):  # 1.00 and 2.00 per piece
        grn = client.post(
            "/api/v1/purchase/grn",
            json={
                "doc_date": TODAY,
                "supplier_id": supplier,
                "location_id": s["hq"],
                "supplier_ref_no": ref,
                "lines": [{"item_id": milo, "uom": "CTN", "qty": "1", "unit_price": price}],
            },
            headers=h,
        ).json()
        grn = _act(client, s, grn, "save", "/api/v1/purchase/grn").json()
        assert _act(client, s, grn, "post", "/api/v1/purchase/grn").status_code == 200
    return s


def _url(kind):
    return f"/api/v1/sales/{kind}"


def _doc(client, s, kind, lines, **over):
    body = {
        "doc_date": TODAY,
        "customer_id": s["customer"],
        "location_id": s["hq"],
        "lines": lines,
        **over,
    }
    return client.post(_url(kind), json=body, headers=s["h"])


def _act(client, s, doc, action, base=None, **extra):
    return client.post(
        f"{base}/{doc['id']}/{action}",
        json={"version": doc["version"], **extra},
        headers=s["h"],
    )


def _do(client, s, kind, doc, action, **extra):
    return _act(client, s, doc, action, _url(kind), **extra)


def _posted(client, s, kind, lines, **over):
    d = _doc(client, s, kind, lines, **over)
    assert d.status_code == 201, d.text
    d = _do(client, s, kind, d.json(), "save")
    assert d.status_code == 200, d.text
    r = _do(client, s, kind, d.json(), "post")
    assert r.status_code == 200, r.text
    return r.json()


def _convert(client, s, target, source_kind, source):
    return client.post(
        f"{_url(target)}/convert",
        json={"source_kind": source_kind, "source_id": source["id"]},
        headers=s["h"],
    )


def _balance(client, s, item):
    rows = client.get(
        "/api/v1/stock/balance",
        params={"item_id": item, "location_id": s["hq"]},
        headers=s["h"],
    ).json()["items"]
    return (rows[0]["qty"], rows[0]["avg_cost"]) if rows else None


def milo(s, qty, uom="PCS", **extra):
    return {"item_id": s["milo"], "uom": uom, "qty": qty, **extra}


# --- prices -------------------------------------------------------------------------------


def test_price_comes_from_line_then_item_selling_price(store, client) -> None:
    d = _doc(
        client,
        store,
        "quotation",
        [
            milo(store, "2", "CTN"),  # list price per carton
            milo(store, "3", unit_price="2.20"),  # typed price wins
            {"item_id": store["soap"], "uom": "PCS", "qty": "1"},  # no price anywhere
        ],
    ).json()
    ctn, typed, soap = d["lines"]
    assert (ctn["price_source"], ctn["unit_price"], ctn["line_subtotal"]) == (
        "LIST",
        "55.0000",
        "110.0000",
    )
    assert (typed["price_source"], typed["line_subtotal"]) == ("DOC", "6.6000")
    assert soap["price_source"] == "MISSING"

    r = _do(client, store, "quotation", d, "save")
    assert r.status_code == 422 and r.json()["error"]["details"]["lines"] == [3]


def test_quotation_is_not_posted_but_converts(store, client) -> None:
    q = _doc(client, store, "quotation", [milo(store, "5")], valid_until=TODAY).json()
    q = _do(client, store, "quotation", q, "save").json()
    assert q["doc_no"] == "QT000001"
    r = _do(client, store, "quotation", q, "post")
    assert r.status_code == 409 and r.json()["error"]["code"] == "DOC_STATE"

    do = _convert(client, store, "delivery-order", "quotation", q)
    assert do.status_code == 201, do.text
    do = do.json()
    assert do["status"] == "DRAFT" and do["source_kind"] == "quotation"
    assert do["source_id"] == q["id"]
    # The quoted price is carried over as agreed
    assert (do["lines"][0]["unit_price"], do["lines"][0]["price_source"]) == ("2.5000", "DOC")
    assert _balance(client, store, store["milo"]) == ("48.0000", "1.5000")  # untouched


def test_draft_quotation_cannot_be_converted(store, client) -> None:
    q = _doc(client, store, "quotation", [milo(store, "1")]).json()
    r = _convert(client, store, "delivery-order", "quotation", q)
    assert r.status_code == 409


# --- delivery and invoicing ---------------------------------------------------------------


def test_do_takes_stock_out_at_average_and_records_profit(store, client) -> None:
    do = _posted(client, store, "delivery-order", [milo(store, "10")])
    assert do["doc_no"] == "DO000001"
    ln = do["lines"][0]
    assert ln["unit_cost"] == "1.5000"
    assert (do["subtotal"], do["cost_total"], do["gross_profit"]) == (
        "25.0000",
        "15.0000",
        "10.0000",
    )
    assert _balance(client, store, store["milo"]) == ("38.0000", "1.5000")


def test_invoice_from_do_moves_no_stock_and_can_be_partial(store, client) -> None:
    do = _posted(client, store, "delivery-order", [milo(store, "10", unit_price="2.40")])

    inv = _convert(client, store, "sales-invoice", "delivery-order", do).json()
    ln = inv["lines"][0]
    assert ln["do_line_id"] == do["lines"][0]["id"] and ln["qty"] == "10.0000"
    assert ln["unit_price"] == "2.4000"

    # Invoice only 6 of the 10 now
    inv = client.patch(
        f"{_url('sales-invoice')}/{inv['id']}",
        json={
            "version": inv["version"],
            "lines": [{**milo(store, "6"), "do_line_id": ln["do_line_id"], "unit_price": "2.40"}],
        },
        headers=store["h"],
    ).json()
    inv = _do(client, store, "sales-invoice", inv, "save").json()
    inv = _do(client, store, "sales-invoice", inv, "post").json()
    assert inv["lines"][0]["unit_cost"] == "1.5000"  # the DO's cost
    assert inv["gross_profit"] == "5.4000"  # 6 x (2.40 - 1.50)
    assert _balance(client, store, store["milo"]) == ("38.0000", "1.5000")  # DO moved it
    with SessionLocal() as db:
        assert not db.scalars(select(StockLedger).where(StockLedger.doc_type == "INV")).all()

    # The next conversion offers only what is left
    rest = _convert(client, store, "sales-invoice", "delivery-order", do).json()
    assert rest["lines"][0]["qty"] == "4.0000"
    # ... and asking for more is refused on save
    rest = client.patch(
        f"{_url('sales-invoice')}/{rest['id']}",
        json={
            "version": rest["version"],
            "lines": [{**milo(store, "5"), "do_line_id": do["lines"][0]["id"]}],
        },
        headers=store["h"],
    ).json()
    r = _do(client, store, "sales-invoice", rest, "save")
    assert r.status_code == 422
    assert r.json()["error"]["details"]["lines"][0]["left"] == "4.0000"

    # A DO with invoices cannot be voided
    do = client.get(f"{_url('delivery-order')}/{do['id']}", headers=store["h"]).json()
    r = _do(client, store, "delivery-order", do, "void", reason="wrong customer")
    assert r.status_code == 409 and r.json()["error"]["details"]["documents"] == [inv["doc_no"]]


def _pick(client, s, do, lines):
    return client.post(
        f"{_url('sales-invoice')}/convert",
        json={"source_kind": "delivery-order", "source_id": do["id"], "lines": lines},
        headers=s["h"],
    )


def test_do_lines_and_quantities_can_be_picked_for_an_invoice(store, client) -> None:
    do = _posted(
        client,
        store,
        "delivery-order",
        [milo(store, "10", unit_price="2.40"), milo(store, "1", uom="CTN", unit_price="27.00")],
    )
    pcs, ctn = do["lines"]

    # 4 of the 10 pieces now; the carton stays for later
    inv = _pick(client, store, do, [{"source_line_id": pcs["id"], "qty": "4"}])
    assert inv.status_code == 201, inv.text
    inv = inv.json()
    assert [(ln["do_line_id"], ln["qty"]) for ln in inv["lines"]] == [(pcs["id"], "4.0000")]
    assert inv["lines"][0]["unit_price"] == "2.4000"  # the DO's price comes along
    inv = _do(client, store, "sales-invoice", inv, "save").json()
    _do(client, store, "sales-invoice", inv, "post")

    # the delivery order now says what is invoiced on each line
    seen = client.get(f"{_url('delivery-order')}/{do['id']}", headers=store["h"]).json()
    assert [ln["invoiced_qty"] for ln in seen["lines"]] == ["4.0000", "0.0000"]
    # ... and other documents do not carry that field
    assert "invoiced_qty" not in inv["lines"][0]

    # more than what is left is refused, with the numbers
    r = _pick(client, store, do, [{"source_line_id": pcs["id"], "qty": "7"}])
    assert r.status_code == 422
    assert r.json()["error"]["details"]["lines"] == [
        {"do_line": 1, "left": "6.0000", "asked": "7.0000"}
    ]

    # the rest in one go: 6 pieces and the carton
    rest = _pick(
        client,
        store,
        do,
        [{"source_line_id": pcs["id"], "qty": "6"}, {"source_line_id": ctn["id"], "qty": "1"}],
    ).json()
    assert [ln["qty"] for ln in rest["lines"]] == ["6.0000", "1.0000"]
    assert rest["lines"][1]["uom"] == "CTN"


def test_picking_is_checked(store, client) -> None:
    do = _posted(client, store, "delivery-order", [milo(store, "3", unit_price="2.40")])
    other = _posted(client, store, "delivery-order", [milo(store, "1", unit_price="2.40")])
    line = do["lines"][0]["id"]

    r = _pick(client, store, do, [{"source_line_id": other["lines"][0]["id"], "qty": "1"}])
    assert r.status_code == 400  # a line of another delivery order
    r = _pick(client, store, do, [{"source_line_id": line, "qty": "1"}] * 2)
    assert r.status_code == 400  # the same line twice
    assert _pick(client, store, do, []).status_code == 400  # nothing picked
    assert _pick(client, store, do, [{"source_line_id": line, "qty": "0"}]).status_code == 400

    # picking only makes sense for a delivery order
    q = _doc(client, store, "quotation", [milo(store, "1", unit_price="2.40")]).json()
    q = _do(client, store, "quotation", q, "save").json()
    r = client.post(
        f"{_url('delivery-order')}/convert",
        json={"source_kind": "quotation", "source_id": q["id"], "lines": []},
        headers=store["h"],
    )
    assert r.status_code == 400


def test_fully_invoiced_do_has_nothing_to_convert(store, client) -> None:
    do = _posted(client, store, "delivery-order", [milo(store, "2")])
    inv = _convert(client, store, "sales-invoice", "delivery-order", do).json()
    _do(client, store, "sales-invoice", inv, "save")
    r = _convert(client, store, "sales-invoice", "delivery-order", do)
    assert r.status_code == 422


def test_concurrent_invoices_cannot_bill_a_do_twice(store, client) -> None:
    do = _posted(client, store, "delivery-order", [milo(store, "10")])
    drafts = [
        _convert(client, store, "sales-invoice", "delivery-order", do).json() for _ in range(2)
    ]

    def save(d):
        c = TestClient(app)
        return lambda: (
            c.post(
                f"{_url('sales-invoice')}/{d['id']}/save",
                json={"version": d["version"]},
                headers=store["h"],
            ).status_code
        )

    assert sorted(_parallel(save(drafts[0]), save(drafts[1]))) == [200, 422]


def test_direct_invoice_takes_stock_and_sets_due_date(store, client) -> None:
    inv = _posted(client, store, "sales-invoice", [milo(store, "1", "CTN")])
    assert inv["doc_no"] == "INV000001"
    assert inv["due_date"] == (date.today() + timedelta(days=30)).isoformat()
    assert inv["cost_total"] == "36.0000"  # 24 x 1.50
    assert _balance(client, store, store["milo"]) == ("24.0000", "1.5000")

    # Void puts the stock back at the same cost
    _do(client, store, "sales-invoice", inv, "void", reason="keyed twice")
    assert _balance(client, store, store["milo"]) == ("48.0000", "1.5000")


def test_selling_more_than_on_hand_goes_negative(store, client) -> None:
    _posted(client, store, "sales-invoice", [milo(store, "50")])
    assert _balance(client, store, store["milo"])[0] == "-2.0000"


# --- warnings -----------------------------------------------------------------------------


def test_credit_limit_and_below_cost_warnings(store, client) -> None:
    d = _doc(
        client,
        store,
        "sales-invoice",
        [milo(store, "40", unit_price="1.20"), milo(store, "1", "CTN")],
    ).json()
    r = _do(client, store, "sales-invoice", d, "save")
    assert r.status_code == 200, r.text  # warnings do not block
    w = {x["code"]: x for x in r.json()["warnings"]}
    assert w["CREDIT_LIMIT"]["credit_limit"] == "100.0000"
    assert w["CREDIT_LIMIT"]["this_invoice"] == "103.0000"  # 48.00 + 55.00
    assert w["BELOW_COST"]["lines"] == [1]
    assert w["BELOW_COST"]["detail"][0]["avg_cost"] == "1.5000"


# --- customer returns ---------------------------------------------------------------------


def test_customer_return_uses_invoice_price_and_original_cost(store, client) -> None:
    inv = _posted(
        client, store, "sales-invoice", [milo(store, "12", unit_price="2.50", discount_pct="10")]
    )
    # Cost moves on after the sale: 36 @ 1.50 + 24 @ 3.00 -> 60 @ 2.10
    grn = client.post(
        "/api/v1/purchase/grn",
        json={
            "doc_date": TODAY,
            "supplier_id": store["supplier"],
            "location_id": store["hq"],
            "supplier_ref_no": "INV-C",
            "lines": [{"item_id": store["milo"], "uom": "CTN", "qty": "1", "unit_price": "72"}],
        },
        headers=store["h"],
    ).json()
    grn = _act(client, store, grn, "save", "/api/v1/purchase/grn").json()
    _act(client, store, grn, "post", "/api/v1/purchase/grn")
    assert _balance(client, store, store["milo"]) == ("60.0000", "2.1000")

    cn = _doc(client, store, "customer-cn", [milo(store, "4")], ref_invoice_id=inv["id"]).json()
    ln = cn["lines"][0]
    assert (ln["price_source"], ln["unit_price"]) == ("INVOICE", "2.2500")  # net of discount
    cn = _do(client, store, "customer-cn", cn, "save").json()
    cn = _do(client, store, "customer-cn", cn, "post").json()
    assert cn["doc_no"] == "CN000001"
    assert cn["lines"][0]["unit_cost"] == "1.5000"  # back at the cost it left with
    # 60 @ 2.10 = 126.00 + 4 @ 1.50 = 6.00 -> 64 pcs, 132.00 -> 2.0625
    assert _balance(client, store, store["milo"]) == ("64.0000", "2.0625")

    # The invoice cannot be voided while the return stands
    inv = client.get(f"{_url('sales-invoice')}/{inv['id']}", headers=store["h"]).json()
    r = _do(client, store, "sales-invoice", inv, "void", reason="mistake")
    assert r.status_code == 409


def test_return_converted_from_invoice(store, client) -> None:
    inv = _posted(client, store, "sales-invoice", [milo(store, "3")])
    cn = _convert(client, store, "customer-cn", "sales-invoice", inv)
    assert cn.status_code == 201, cn.text
    cn = cn.json()
    assert cn["ref_invoice_id"] == inv["id"] and cn["lines"][0]["unit_price"] == "2.5000"


def test_return_without_price_or_invoice_cannot_be_saved(store, client) -> None:
    cn = _doc(client, store, "customer-cn", [milo(store, "1")]).json()
    assert cn["lines"][0]["price_source"] == "MISSING"
    r = _do(client, store, "customer-cn", cn, "save")
    assert r.status_code == 422


def test_return_against_another_customers_invoice_is_refused(store, client) -> None:
    inv = _posted(client, store, "sales-invoice", [milo(store, "1")])
    other = client.post(
        "/api/v1/customers", json={"code": "OTHER", "name": "Other"}, headers=store["h"]
    ).json()["id"]
    cn = _doc(
        client,
        store,
        "customer-cn",
        [milo(store, "1")],
        customer_id=other,
        ref_invoice_id=inv["id"],
    ).json()
    assert _do(client, store, "customer-cn", cn, "save").status_code == 422


# --- access -------------------------------------------------------------------------------


def test_cashier_cannot_open_sales_documents(store, client) -> None:
    h = login(client, "cashier1")
    assert client.get(_url("sales-invoice"), headers=h).status_code == 403
