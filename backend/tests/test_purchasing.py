import threading
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.session import SessionLocal
from app.main import app
from app.models import PostingJob, StockLedger
from tests.conftest import login

pytestmark = pytest.mark.db

TODAY = date.today().isoformat()


@pytest.fixture
def shop(world, client):
    """Owner logged in, two items, one supplier with a document-number profile."""
    h = login(client, "boss")
    taxes = {t["code"]: t["id"] for t in client.get("/api/v1/tax-codes", headers=h).json()["items"]}

    def item(code, tax, factor_ctn=None):
        uoms = [{"uom": "PCS", "factor": "1"}]
        if factor_ctn:
            uoms.append({"uom": "CTN", "factor": str(factor_ctn)})
        r = client.post(
            "/api/v1/items",
            json={
                "code": code,
                "name": code,
                "base_uom": "PCS",
                "purchase_tax_code_id": taxes[tax],
                "uoms": uoms,
            },
            headers=h,
        )
        assert r.status_code == 201, r.text
        return r.json()["id"]

    supplier = client.post(
        "/api/v1/suppliers",
        json={
            "code": "MIGHTY",
            "name": "Mighty Bakery",
            "profile": {"invoice_no_pattern": r"^IJB\d+$", "cn_no_pattern": r"^CJB\d+$"},
        },
        headers=h,
    ).json()["id"]
    return {
        "h": h,
        "bread": item("BREAD", "NA", 24),
        "cake": item("CAKE", "ST5"),
        "supplier": supplier,
        "hq": str(world.hq_id),
        "s2": str(world.s2_id),
        "taxes": taxes,
    }


def _doc(client, shop, kind="grn", lines=None, **over):
    body = {
        "doc_date": TODAY,
        "supplier_id": shop["supplier"],
        "location_id": shop["hq"],
        "supplier_ref_no": over.pop("ref", None),
        "lines": lines or [],
        **over,
    }
    return client.post(f"/api/v1/purchase/{kind}", json=body, headers=shop["h"])


def _act(client, shop, kind, doc, action, **extra):
    return client.post(
        f"/api/v1/purchase/{kind}/{doc['id']}/{action}",
        json={"version": doc["version"], **extra},
        headers=shop["h"],
    )


def _posted(client, shop, kind, lines, **over):
    d = _doc(client, shop, kind, lines, **over).json()
    d = _act(client, shop, kind, d, "save").json()
    r = _act(client, shop, kind, d, "post")
    assert r.status_code == 200, r.text
    return r.json()


def _balance(client, shop, item, loc=None):
    rows = client.get(
        "/api/v1/stock/balance",
        params={"item_id": item, "location_id": loc or shop["hq"]},
        headers=shop["h"],
    ).json()["items"]
    return rows[0] if rows else None


# --- amounts ------------------------------------------------------------------------------


def test_carton_pricing_gives_cost_per_piece(shop, client) -> None:
    # Design doc: 19.46 per CTN of 24 -> 0.8108 per piece
    r = _doc(
        client,
        shop,
        lines=[{"item_id": shop["bread"], "uom": "CTN", "qty": "1", "unit_price": "19.46"}],
        ref="IJB1001",
    )
    assert r.status_code == 201, r.text
    ln = r.json()["lines"][0]
    assert ln["base_qty"] == "24.0000"
    assert ln["line_subtotal"] == "19.4600"
    assert ln["unit_cost"] == "0.8108"
    assert r.json()["doc_no"] is None and r.json()["status"] == "DRAFT"


def test_discount_and_sst(shop, client) -> None:
    r = _doc(
        client,
        shop,
        lines=[
            # 0.50 x 96% = 0.48 per piece, 100 pcs
            {
                "item_id": shop["bread"],
                "uom": "PCS",
                "qty": "100",
                "unit_price": "0.50",
                "discount_pct": "4",
            },
            # cake: item default purchase tax ST5
            {"item_id": shop["cake"], "uom": "PCS", "qty": "10", "unit_price": "3.00"},
        ],
    ).json()
    bread, cake = r["lines"]
    assert bread["line_subtotal"] == "48.0000" and bread["unit_cost"] == "0.4800"
    assert bread["line_tax"] == "0.0000"
    assert cake["tax_rate"] == "5.0000" and cake["line_tax"] == "1.5000"
    assert r["subtotal"] == "78.0000" and r["tax_total"] == "1.5000" and r["total"] == "79.5000"


# --- lifecycle ----------------------------------------------------------------------------


def test_save_numbers_and_post_writes_stock(shop, client) -> None:
    line = {"item_id": shop["bread"], "uom": "CTN", "qty": "2", "unit_price": "19.46"}
    d = _doc(client, shop, lines=[line], ref="IJB2001").json()
    saved = _act(client, shop, "grn", d, "save").json()
    assert saved["status"] == "SAVED" and saved["doc_no"] == "GRN000001"
    assert saved["warnings"] == []
    posted = _act(client, shop, "grn", saved, "post").json()
    assert posted["status"] == "POSTED" and posted["posted_at"]

    bal = _balance(client, shop, shop["bread"])
    assert bal["qty"] == "48.0000" and bal["avg_cost"] == "0.8108"
    with SessionLocal() as db:
        job = db.scalar(select(PostingJob).where(PostingJob.doc_id == posted["id"]))
        assert (job.step, job.status) == ("STOCK", "DONE")
        led = db.scalar(select(StockLedger).where(StockLedger.doc_id == posted["id"]))
        assert led.doc_no == "GRN000001" and led.qty_in == Decimal("48")

    # Posted documents are read-only and cannot be posted twice.
    edit = client.patch(
        f"/api/v1/purchase/grn/{posted['id']}",
        json={"version": posted["version"], "remark": "x"},
        headers=shop["h"],
    )
    assert edit.status_code == 409 and edit.json()["error"]["code"] == "DOC_STATE"
    again = _act(client, shop, "grn", posted, "post")
    assert again.status_code == 409


def test_numbers_are_sequential(shop, client) -> None:
    line = {"item_id": shop["cake"], "uom": "PCS", "qty": "1", "unit_price": "1"}
    nos = []
    for ref in ("IJB1", "IJB2"):
        d = _doc(client, shop, lines=[line], ref=ref).json()
        nos.append(_act(client, shop, "grn", d, "save").json()["doc_no"])
    assert nos == ["GRN000001", "GRN000002"]


def test_moving_average_and_void_reversal(shop, client) -> None:
    pcs = lambda q, p: [{"item_id": shop["cake"], "uom": "PCS", "qty": q, "unit_price": p}]  # noqa: E731
    _posted(client, shop, "grn", pcs("10", "1.00"), ref="IJB10")
    second = _posted(client, shop, "grn", pcs("10", "2.00"), ref="IJB11")
    bal = _balance(client, shop, shop["cake"])
    assert (bal["qty"], bal["avg_cost"]) == ("20.0000", "1.5000")

    _posted(client, shop, "supplier-cn", pcs("5", "1.50"), ref="CJB1")
    bal = _balance(client, shop, shop["cake"])
    assert (bal["qty"], bal["avg_cost"]) == ("15.0000", "1.5000")  # returns leave avg

    voided = _act(client, shop, "grn", second, "void", reason="Entered twice")
    assert voided.status_code == 200 and voided.json()["status"] == "VOIDED"
    bal = _balance(client, shop, shop["cake"])
    # remove 10 @ 2.00 from 15 @ 1.50 -> 5 left worth 2.50 -> 0.50 each
    assert (bal["qty"], bal["avg_cost"]) == ("5.0000", "0.5000")

    ledger = client.get(
        "/api/v1/stock/ledger", params={"item_id": shop["cake"]}, headers=shop["h"]
    ).json()["items"]
    assert ledger[0]["is_reversal"] is True and ledger[0]["qty_out"] == "10.0000"
    assert len(ledger) == 4  # never deleted


def test_stock_may_go_negative(shop, client) -> None:
    pcs = lambda q: [{"item_id": shop["cake"], "uom": "PCS", "qty": q, "unit_price": "1"}]  # noqa: E731
    _posted(client, shop, "grn", pcs("20"), ref="IJB20")
    _posted(client, shop, "supplier-cn", pcs("50"))
    bal = _balance(client, shop, shop["cake"])
    assert bal["qty"] == "-30.0000"
    assert bal["avg_cost"] == "1.0000"  # average kept while negative

    # The next receipt starts the average afresh from its own cost.
    _posted(
        client,
        shop,
        "grn",
        [{"item_id": shop["cake"], "uom": "PCS", "qty": "40", "unit_price": "2"}],
        ref="IJB21",
    )
    bal = _balance(client, shop, shop["cake"])
    assert (bal["qty"], bal["avg_cost"]) == ("10.0000", "2.0000")


def test_voiding_posted_cn_puts_stock_back(shop, client) -> None:
    pcs = lambda q: [{"item_id": shop["cake"], "uom": "PCS", "qty": q, "unit_price": "1"}]  # noqa: E731
    _posted(client, shop, "grn", pcs("10"), ref="IJB30")
    cn = _posted(client, shop, "supplier-cn", pcs("4"))
    assert _balance(client, shop, shop["cake"])["qty"] == "6.0000"
    _act(client, shop, "supplier-cn", cn, "void", reason="Supplier refused return")
    assert _balance(client, shop, shop["cake"])["qty"] == "10.0000"


def test_only_drafts_can_be_deleted(shop, client) -> None:
    line = [{"item_id": shop["cake"], "uom": "PCS", "qty": "1", "unit_price": "1"}]
    d = _doc(client, shop, lines=line, ref="IJB40").json()
    saved = _act(client, shop, "grn", d, "save").json()
    assert client.delete(f"/api/v1/purchase/grn/{d['id']}", headers=shop["h"]).status_code == 409
    d2 = _doc(client, shop, lines=line).json()
    assert client.delete(f"/api/v1/purchase/grn/{d2['id']}", headers=shop["h"]).status_code == 204
    assert saved["status"] == "SAVED"


# --- DynaMod lessons ----------------------------------------------------------------------


def test_duplicate_supplier_invoice_is_blocked(shop, client) -> None:
    line = [{"item_id": shop["cake"], "uom": "PCS", "qty": "1", "unit_price": "1"}]
    first = _doc(client, shop, lines=line, ref="IJB500").json()
    _act(client, shop, "grn", first, "save")
    dup = _doc(client, shop, lines=line, ref=" ijb500 ").json()
    r = _act(client, shop, "grn", dup, "save")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "DUPLICATE_SUPPLIER_DOC"
    assert r.json()["error"]["details"]["existing_doc_no"] == "GRN000001"


def test_number_not_matching_supplier_format_warns(shop, client) -> None:
    # Lam Soon case: order number typed where the invoice number belongs.
    line = [{"item_id": shop["cake"], "uom": "PCS", "qty": "1", "unit_price": "1"}]
    d = _doc(client, shop, lines=line, ref="PO-7788").json()
    r = _act(client, shop, "grn", d, "save")
    assert r.status_code == 200 and r.json()["status"] == "SAVED"
    assert [w["code"] for w in r.json()["warnings"]] == ["SUPPLIER_REF_FORMAT"]


def test_future_date_is_blocked(shop, client) -> None:
    # Zeiss case: 2026 written instead of 2025 -> date after today.
    future = (date.today() + timedelta(days=400)).isoformat()
    line = [{"item_id": shop["cake"], "uom": "PCS", "qty": "1", "unit_price": "1"}]
    d = _doc(client, shop, lines=line, ref="IJB600", doc_date=future).json()
    r = _act(client, shop, "grn", d, "save")
    assert r.status_code == 422
    assert r.json()["error"]["details"]["doc_date"] == future


def test_unit_must_belong_to_item(shop, client) -> None:
    r = _doc(
        client, shop, lines=[{"item_id": shop["cake"], "uom": "CTN", "qty": "1", "unit_price": "1"}]
    )
    assert r.status_code == 400 and r.json()["error"]["details"]["allowed"] == ["PCS"]


def test_recorded_cn_cannot_post_until_items_confirmed(shop, client) -> None:
    line = [{"item_id": shop["cake"], "uom": "PCS", "qty": "1", "unit_price": "1"}]
    _posted(client, shop, "grn", line, ref="IJB700")
    cn = _doc(client, shop, "supplier-cn", line, completeness="RECORDED").json()
    assert cn["completeness"] == "RECORDED"
    cn = _act(client, shop, "supplier-cn", cn, "save").json()
    assert _act(client, shop, "supplier-cn", cn, "post").status_code == 422


# --- permissions and scope ----------------------------------------------------------------


def test_clerk_only_works_in_own_store(shop, client) -> None:
    line = [{"item_id": shop["cake"], "uom": "PCS", "qty": "1", "unit_price": "1"}]
    hq_doc = _doc(client, shop, lines=line, ref="IJB800").json()

    pc = TestClient(app)
    ch = login(pc, "clerk1")  # scoped to S2
    body = {"doc_date": TODAY, "supplier_id": shop["supplier"], "lines": line}
    assert (
        pc.post(
            "/api/v1/purchase/grn", json={**body, "location_id": shop["hq"]}, headers=ch
        ).status_code
        == 400
    )
    assert (
        pc.post(
            "/api/v1/purchase/grn", json={**body, "location_id": shop["s2"]}, headers=ch
        ).status_code
        == 201
    )
    assert pc.get(f"/api/v1/purchase/grn/{hq_doc['id']}", headers=ch).status_code == 404
    assert pc.get("/api/v1/purchase/grn", headers=ch).json()["total"] == 1


def test_accountant_reads_but_cannot_create(shop, client) -> None:
    pc = TestClient(app)
    ah = login(pc, "acct")
    assert pc.get("/api/v1/purchase/grn", headers=ah).status_code == 200
    r = pc.post(
        "/api/v1/purchase/grn",
        json={"doc_date": TODAY, "supplier_id": shop["supplier"], "location_id": shop["hq"]},
        headers=ah,
    )
    assert r.status_code == 403


def test_cashier_sees_stock_qty_but_not_cost(shop, client) -> None:
    line = [{"item_id": shop["cake"], "uom": "PCS", "qty": "3", "unit_price": "2"}]
    _posted(client, shop, "grn", line, ref="IJB900", location_id=shop["s2"])
    pc = TestClient(app)
    ch = login(pc, "cashier1")
    rows = pc.get("/api/v1/stock/balance", headers=ch).json()["items"]
    assert rows == [
        {
            "item_id": shop["cake"],
            "item_code": "CAKE",
            "item_name": "CAKE",
            "base_uom": "PCS",
            "location_id": shop["s2"],
            "qty": "3.0000",
        }
    ]
    assert pc.get("/api/v1/purchase/grn", headers=ch).status_code == 403


# --- concurrency --------------------------------------------------------------------------


def _parallel(*calls):
    results = [None] * len(calls)
    barrier = threading.Barrier(len(calls))

    def run(i, fn):
        barrier.wait()
        results[i] = fn()

    threads = [threading.Thread(target=run, args=(i, fn)) for i, fn in enumerate(calls)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_concurrent_returns_lose_no_update(shop, client) -> None:
    pcs = lambda q: [{"item_id": shop["cake"], "uom": "PCS", "qty": q, "unit_price": "1"}]  # noqa: E731
    _posted(client, shop, "grn", pcs("10"), ref="IJB1000")
    cns = []
    for _ in range(2):
        d = _doc(client, shop, "supplier-cn", pcs("7")).json()
        cns.append(_act(client, shop, "supplier-cn", d, "save").json())

    def post(cn):
        c = TestClient(app)
        return lambda: (
            c.post(
                f"/api/v1/purchase/supplier-cn/{cn['id']}/post",
                json={"version": cn["version"]},
                headers=shop["h"],
            ).status_code
        )

    assert _parallel(post(cns[0]), post(cns[1])) == [200, 200]
    # Both applied, one after the other (row lock): 10 - 7 - 7
    assert _balance(client, shop, shop["cake"])["qty"] == "-4.0000"


def test_same_document_cannot_be_posted_twice_concurrently(shop, client) -> None:
    pcs = [{"item_id": shop["cake"], "uom": "PCS", "qty": "5", "unit_price": "1"}]
    d = _doc(client, shop, lines=pcs, ref="IJB1100").json()
    d = _act(client, shop, "grn", d, "save").json()

    def post():
        c = TestClient(app)
        return lambda: (
            c.post(
                f"/api/v1/purchase/grn/{d['id']}/post",
                json={"version": d["version"]},
                headers=shop["h"],
            ).status_code
        )

    codes = sorted(_parallel(post(), post()))
    assert codes == [200, 409]
    assert _balance(client, shop, shop["cake"])["qty"] == "5.0000"


# --- return price: CN price first, then the original invoice ----------------------------


def _two_receipts(client, shop):
    pcs = lambda q, pr, ref: _posted(  # noqa: E731
        client,
        shop,
        "grn",
        [{"item_id": shop["bread"], "uom": "CTN", "qty": q, "unit_price": pr}],
        ref=ref,
    )
    pcs("1", "24.00", "IJB3001")  # 24 pcs @ 1.00
    pcs("1", "48.00", "IJB3002")  # 24 pcs @ 2.00 -> avg 1.50


def test_return_uses_its_own_price_first(shop, client) -> None:
    _two_receipts(client, shop)
    cn = _posted(
        client,
        shop,
        "supplier-cn",
        [{"item_id": shop["bread"], "uom": "PCS", "qty": "12", "unit_price": "1.80"}],
        ref_invoice_no="IJB3001",  # ignored: the CN has its own price
    )
    ln = cn["lines"][0]
    assert (ln["price_source"], ln["unit_price"], ln["line_subtotal"]) == (
        "DOC",
        "1.8000",
        "21.6000",
    )
    bal = _balance(client, shop, shop["bread"])
    # 48 @ 1.50 = 72.00, minus 12 @ 1.80 = 21.60 -> 36 pcs worth 50.40 -> 1.40
    assert (bal["qty"], bal["avg_cost"]) == ("36.0000", "1.4000")


def test_return_without_price_takes_invoice_price(shop, client) -> None:
    _two_receipts(client, shop)
    cn = _doc(
        client,
        shop,
        "supplier-cn",
        [{"item_id": shop["bread"], "uom": "CTN", "qty": "1"}],
        ref_invoice_no=" ijb3002 ",
    ).json()
    ln = cn["lines"][0]
    assert ln["price_source"] == "INVOICE"
    assert ln["unit_price"] == "48.0000"  # 2.00 per piece x 24 per carton
    cn = _act(client, shop, "supplier-cn", cn, "save").json()
    _act(client, shop, "supplier-cn", cn, "post")
    bal = _balance(client, shop, shop["bread"])
    # 72.00 - 48.00 = 24.00 for 24 pcs -> 1.00
    assert (bal["qty"], bal["avg_cost"]) == ("24.0000", "1.0000")


def test_return_without_any_price_cannot_be_saved(shop, client) -> None:
    _two_receipts(client, shop)
    cn = _doc(
        client, shop, "supplier-cn", [{"item_id": shop["bread"], "uom": "PCS", "qty": "3"}]
    ).json()
    assert cn["lines"][0]["price_source"] == "MISSING"  # draft is fine
    r = _act(client, shop, "supplier-cn", cn, "save")
    assert r.status_code == 422 and r.json()["error"]["details"]["lines"] == [1]

    # Adding the invoice number later fills the price in.
    fixed = client.patch(
        f"/api/v1/purchase/supplier-cn/{cn['id']}",
        json={"version": cn["version"], "ref_invoice_no": "IJB3001"},
        headers=shop["h"],
    ).json()
    assert fixed["lines"][0]["price_source"] == "INVOICE"
    assert fixed["lines"][0]["unit_price"] == "1.0000"
    assert _act(client, shop, "supplier-cn", fixed, "save").status_code == 200


def test_item_not_on_referenced_invoice_is_missing(shop, client) -> None:
    _two_receipts(client, shop)
    cn = _doc(
        client,
        shop,
        "supplier-cn",
        [{"item_id": shop["cake"], "uom": "PCS", "qty": "1"}],
        ref_invoice_no="IJB3001",
    ).json()
    assert cn["lines"][0]["price_source"] == "MISSING"


def test_grn_line_still_needs_a_price(shop, client) -> None:
    r = _doc(client, shop, lines=[{"item_id": shop["cake"], "uom": "PCS", "qty": "1"}])
    assert r.status_code == 400
