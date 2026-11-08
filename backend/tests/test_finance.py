from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.conftest import login
from tests.test_purchasing import _act as pur_act
from tests.test_purchasing import _doc as pur_doc
from tests.test_purchasing import (
    _parallel,
    shop,  # noqa: F401
)
from tests.test_purchasing import _posted as pur_posted
from tests.test_sales import _do as sal_do
from tests.test_sales import _posted as sal_posted
from tests.test_sales import milo, store  # noqa: F401

pytestmark = pytest.mark.db

TODAY = date.today().isoformat()
FIN = "/api/v1/finance"


def _fin(client, s, kind, **body):
    body.setdefault("doc_date", TODAY)
    body.setdefault("location_id", s["hq"])
    return client.post(f"{FIN}/{kind}", json=body, headers=s["h"])


def _act(client, s, kind, doc, action, **extra):
    return client.post(
        f"{FIN}/{kind}/{doc['id']}/{action}",
        json={"version": doc["version"], **extra},
        headers=s["h"],
    )


def _post(client, s, kind, **body):
    d = _fin(client, s, kind, **body)
    assert d.status_code == 201, d.text
    d = _act(client, s, kind, d.json(), "save")
    assert d.status_code == 200, d.text
    r = _act(client, s, kind, d.json(), "post")
    assert r.status_code == 200, r.text
    return r.json()


def _open(client, s, party_type, party_id):
    r = client.get(
        f"{FIN}/open-items",
        params={"party_type": party_type, "party_id": party_id},
        headers=s["h"],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    return {i["doc_no"]: i["open"] for i in body["items"]}, body["balance"]


# --- customers ----------------------------------------------------------------------------


@pytest.fixture
def owed(store, client):  # noqa: F811
    """ACME owes two invoices: INV000001 25.00 (older) and INV000002 50.00."""
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    a = sal_posted(client, store, "sales-invoice", [milo(store, "10")], doc_date=yesterday)
    b = sal_posted(client, store, "sales-invoice", [milo(store, "20")])
    return {**store, "inv_a": a, "inv_b": b}


def test_receipt_pays_the_named_invoice(owed, client) -> None:
    r = _post(
        client,
        owed,
        "receipt",
        customer_id=owed["customer"],
        amount="30",
        method="CASH",
        lines=[{"invoice_id": owed["inv_b"]["id"], "amount": "30"}],
    )
    assert r["doc_no"] == "OR000001" and r["unallocated"] == "0.0000"
    items, balance = _open(client, owed, "customer", owed["customer"])
    assert items == {"INV000001": "25.0000", "INV000002": "20.0000"}
    assert balance == "45.0000"


def test_receipt_pays_oldest_first_and_keeps_the_rest_as_advance(owed, client) -> None:
    r = _post(client, owed, "receipt", customer_id=owed["customer"], amount="100")
    assert [(ln["invoice_id"], ln["amount"]) for ln in r["lines"]] == [
        (owed["inv_a"]["id"], "25.0000"),
        (owed["inv_b"]["id"], "50.0000"),
    ]
    assert r["unallocated"] == "25.0000"
    items, balance = _open(client, owed, "customer", owed["customer"])
    assert items == {"OR000001": "25.0000"} and balance == "-25.0000"  # we owe them

    st = client.get(
        f"{FIN}/statement",
        params={"party_type": "customer", "party_id": owed["customer"]},
        headers=owed["h"],
    ).json()
    assert [(ln["doc_no"], ln["balance"]) for ln in st["lines"]] == [
        ("INV000001", "25.0000"),
        ("INV000002", "75.0000"),
        ("OR000001", "-25.0000"),
    ]
    assert st["closing"] == "-25.0000"


def test_deposit_without_allocation_and_manual_knockoff(owed, client) -> None:
    d = _fin(
        client, owed, "receipt", customer_id=owed["customer"], amount="40", auto_allocate=False
    ).json()
    saved = _act(client, owed, "receipt", d, "save").json()
    assert saved["warnings"][0]["code"] == "ADVANCE"
    r = _act(client, owed, "receipt", saved, "post").json()
    assert r["unallocated"] == "40.0000"

    k = client.post(
        f"{FIN}/knockoffs",
        json={
            "debit_type": "INV",
            "debit_id": owed["inv_b"]["id"],
            "credit_type": "OR",
            "credit_id": r["id"],
        },
        headers=owed["h"],
    )
    assert k.status_code == 201, k.text
    assert k.json()["amount"] == "40.0000"  # as much as both allow
    items, _ = _open(client, owed, "customer", owed["customer"])
    assert items["INV000002"] == "10.0000" and "OR000001" not in items

    # Too much is refused
    bad = client.post(
        f"{FIN}/knockoffs",
        json={
            "debit_type": "INV",
            "debit_id": owed["inv_a"]["id"],
            "credit_type": "OR",
            "credit_id": r["id"],
            "amount": "1",
        },
        headers=owed["h"],
    )
    assert bad.status_code == 422

    # Undo puts it back
    assert client.delete(f"{FIN}/knockoffs/{k.json()['id']}", headers=owed["h"]).status_code == 200
    items, _ = _open(client, owed, "customer", owed["customer"])
    assert items["INV000002"] == "50.0000" and items["OR000001"] == "40.0000"


def test_receipt_line_cannot_exceed_invoice(owed, client) -> None:
    d = _fin(
        client,
        owed,
        "receipt",
        customer_id=owed["customer"],
        amount="30",
        lines=[{"invoice_id": owed["inv_a"]["id"], "amount": "30"}],
    ).json()
    r = _act(client, owed, "receipt", d, "save")
    assert r.status_code == 422
    assert r.json()["error"]["details"]["invoices"][0]["open"] == "25.0000"


def test_concurrent_receipts_cannot_pay_one_invoice_twice(owed, client) -> None:
    docs = []
    for _ in range(2):
        d = _fin(
            client,
            owed,
            "receipt",
            customer_id=owed["customer"],
            amount="25",
            auto_allocate=False,
            lines=[{"invoice_id": owed["inv_a"]["id"], "amount": "25"}],
        ).json()
        docs.append(_act(client, owed, "receipt", d, "save").json())

    def post(d):
        c = TestClient(app)
        return lambda: (
            c.post(
                f"{FIN}/receipt/{d['id']}/post", json={"version": d["version"]}, headers=owed["h"]
            ).status_code
        )

    assert sorted(_parallel(post(docs[0]), post(docs[1]))) == [200, 422]


def test_customer_return_is_knocked_off_its_invoice(owed, client) -> None:
    cn = sal_posted(
        client, owed, "customer-cn", [milo(owed, "4")], ref_invoice_id=owed["inv_b"]["id"]
    )
    items, balance = _open(client, owed, "customer", owed["customer"])
    assert items["INV000002"] == "40.0000" and balance == "65.0000"

    sal_do(client, owed, "customer-cn", cn, "void", reason="entered wrongly")
    items, _ = _open(client, owed, "customer", owed["customer"])
    assert items["INV000002"] == "50.0000"


def test_paid_invoice_cannot_be_voided_until_receipt_is(owed, client) -> None:
    r = _post(client, owed, "receipt", customer_id=owed["customer"], amount="25")
    inv = client.get(f"/api/v1/sales/sales-invoice/{owed['inv_a']['id']}", headers=owed["h"]).json()
    blocked = sal_do(client, owed, "sales-invoice", inv, "void", reason="mistake")
    assert blocked.status_code == 409
    assert blocked.json()["error"]["details"]["knocked_with"] == ["INV000001", "OR000001"]

    assert _act(client, owed, "receipt", r, "void", reason="bounced cheque").status_code == 200
    assert sal_do(client, owed, "sales-invoice", inv, "void", reason="mistake").status_code == 200


def test_receipts_lower_the_credit_limit_check(owed, client) -> None:
    _post(client, owed, "receipt", customer_id=owed["customer"], amount="75")
    d = client.post(
        "/api/v1/sales/sales-invoice",
        json={
            "doc_date": TODAY,
            "customer_id": owed["customer"],
            "location_id": owed["hq"],
            "lines": [milo(owed, "30")],  # 75.00: limit 100, nothing owed now
        },
        headers=owed["h"],
    ).json()
    r = sal_do(client, owed, "sales-invoice", d, "save").json()
    assert not [w for w in r["warnings"] if w["code"] == "CREDIT_LIMIT"]


# --- suppliers ----------------------------------------------------------------------------


@pytest.fixture
def payable(shop, client):  # noqa: F811
    """Mighty: invoice IJB1001 (GRN, 100.00) and provisional CN CJB7 (20.00)."""
    bread = lambda price: [  # noqa: E731
        {"item_id": shop["bread"], "uom": "CTN", "qty": "1", "unit_price": price}
    ]
    grn = pur_posted(client, shop, "grn", bread("100"), ref="IJB1001")
    cn = pur_doc(
        client, shop, "supplier-cn", bread("20"), ref="CJB7", completeness="PROVISIONAL"
    ).json()
    cn = pur_act(client, shop, "supplier-cn", cn, "save").json()
    cn = pur_act(client, shop, "supplier-cn", cn, "post").json()
    return {**shop, "grn": grn, "cn": cn}


def _issues(client, s, doc_id):
    return client.get("/api/v1/issues", params={"doc_id": doc_id}, headers=s["h"]).json()


def test_payment_advice_reconciles_invoice_cn_and_fee(payable, client) -> None:
    sv8 = payable["taxes"]["SV8"]
    d = _fin(
        client,
        payable,
        "supplier-payment",
        supplier_id=payable["supplier"],
        supplier_ref_no="PAY-88",
        lines=[
            {"line_type": "INVOICE", "ref_no": "ijb-01001", "amount": "100"},  # loose number
            {"line_type": "CN", "ref_no": "CJB 7", "amount": "20"},
            {"line_type": "FEE", "fee_type": "FREIGHT", "amount": "10.80", "tax_code_id": sv8},
        ],
    )
    assert d.status_code == 201, d.text
    d = d.json()
    assert d["amount"] == "69.2000"  # 100 - 20 - 10.80
    assert [ln["match_status"] for ln in d["lines"]] == ["MATCHED", "MATCHED", None]
    assert d["lines"][0]["matched_no"] == payable["grn"]["doc_no"]

    d = _act(client, payable, "supplier-payment", d, "save").json()
    assert d["warnings"] == []
    p = _act(client, payable, "supplier-payment", d, "post").json()
    assert p["unallocated"] == "0.0000"

    items, balance = _open(client, payable, "supplier", payable["supplier"])
    assert items == {} and balance == "0.0000"
    cn = client.get(
        f"/api/v1/purchase/supplier-cn/{payable['cn']['id']}", headers=payable["h"]
    ).json()
    assert cn["completeness"] == "COMPLETE"
    fees = client.get(f"{FIN}/supplier-fees", headers=payable["h"]).json()
    assert [(f["fee_type"], f["amount"], f["tax_amount"]) for f in fees] == [
        ("FREIGHT", "10.8000", "0.8000")
    ]
    assert _issues(client, payable, p["id"]) == []


def test_payment_with_unknown_cn_opens_a_problem(payable, client) -> None:
    d = _fin(
        client,
        payable,
        "supplier-payment",
        supplier_id=payable["supplier"],
        lines=[
            {"line_type": "INVOICE", "ref_no": "IJB1001", "amount": "100"},
            {"line_type": "CN", "ref_no": "CJB999", "amount": "15"},
        ],
    ).json()
    saved = _act(client, payable, "supplier-payment", d, "save").json()
    assert [w["code"] for w in saved["warnings"]] == ["PAYMENT_NOT_FOUND"]
    p = _act(client, payable, "supplier-payment", saved, "post").json()

    # Cash 85 pays the invoice down to 15: the missing CN is still owed to us on paper
    items, _ = _open(client, payable, "supplier", payable["supplier"])
    assert items[payable["grn"]["doc_no"]] == "15.0000"
    issues = _issues(client, payable, p["id"])
    assert [(i["issue_type"], i["status"]) for i in issues] == [("MISSING_CN", "OPEN")]
    assert issues[0]["details"]["ref_no"] == "CJB999"

    r = client.post(
        f"/api/v1/issues/{issues[0]['id']}/resolve",
        json={"resolution": "Supplier sent the CN; recorded as SCN000002"},
        headers=payable["h"],
    )
    assert r.status_code == 200 and r.json()["status"] == "RESOLVED"


def test_cn_amount_differs_marks_it_disputed(payable, client) -> None:
    p = _post(
        client,
        payable,
        "supplier-payment",
        supplier_id=payable["supplier"],
        lines=[
            {"line_type": "INVOICE", "ref_no": "IJB1001", "amount": "100"},
            {"line_type": "CN", "ref_no": "CJB7", "amount": "22.50"},
        ],
    )
    cn = client.get(
        f"/api/v1/purchase/supplier-cn/{payable['cn']['id']}", headers=payable["h"]
    ).json()
    assert cn["completeness"] == "DISPUTED"
    assert [i["issue_type"] for i in _issues(client, payable, p["id"])] == ["AMOUNT_MISMATCH"]
    items, _ = _open(client, payable, "supplier", payable["supplier"])
    assert items[payable["cn"]["doc_no"]] == "20.0000"  # not knocked off


def test_supplier_pays_us_back_when_cn_is_bigger(shop, client) -> None:  # noqa: F811
    bread = lambda price: [  # noqa: E731
        {"item_id": shop["bread"], "uom": "CTN", "qty": "1", "unit_price": price}
    ]
    grn = pur_posted(client, shop, "grn", bread("10"), ref="IJB2001")
    cn = pur_posted(client, shop, "supplier-cn", bread("30"), ref="CJB2002")
    p = _post(
        client,
        shop,
        "supplier-payment",
        supplier_id=shop["supplier"],
        method="CHEQUE",
        lines=[
            {"line_type": "INVOICE", "ref_no": "IJB2001", "amount": "10"},
            {"line_type": "CN", "ref_no": "CJB2002", "amount": "30"},
        ],
    )
    assert p["amount"] == "-20.0000" and p["unallocated"] == "0.0000"
    items, balance = _open(client, shop, "supplier", shop["supplier"])
    assert items == {} and balance == "0.0000"
    assert grn["doc_no"] and cn["doc_no"]


def test_voiding_a_payment_undoes_everything_it_did(payable, client) -> None:
    p = _post(
        client,
        payable,
        "supplier-payment",
        supplier_id=payable["supplier"],
        lines=[
            {"line_type": "INVOICE", "ref_no": "IJB1001", "amount": "100"},
            {"line_type": "FEE", "fee_type": "LISTING", "amount": "5"},
            {"line_type": "CN", "ref_no": "CJB404", "amount": "1"},
        ],
    )
    # The GRN is paid, so it cannot be voided now
    grn = client.get(f"/api/v1/purchase/grn/{payable['grn']['id']}", headers=payable["h"]).json()
    assert pur_act(client, payable, "grn", grn, "void", reason="mistake").status_code == 409

    v = _act(client, payable, "supplier-payment", p, "void", reason="wrong supplier")
    assert v.status_code == 200, v.text
    items, _ = _open(client, payable, "supplier", payable["supplier"])
    assert items[payable["grn"]["doc_no"]] == "100.0000"
    fees = client.get(f"{FIN}/supplier-fees", headers=payable["h"]).json()
    assert fees[0]["status"] == "VOIDED"
    assert [i["status"] for i in _issues(client, payable, p["id"])] == ["CANCELLED"]


def test_same_payment_advice_twice_is_refused(payable, client) -> None:
    line = [{"line_type": "INVOICE", "ref_no": "IJB1001", "amount": "50"}]
    _post(
        client,
        payable,
        "supplier-payment",
        supplier_id=payable["supplier"],
        supplier_ref_no="PAY-1",
        lines=line,
    )
    d = _fin(
        client,
        payable,
        "supplier-payment",
        supplier_id=payable["supplier"],
        supplier_ref_no="pay-1 ",
        lines=line,
    ).json()
    r = _act(client, payable, "supplier-payment", d, "save")
    assert r.status_code == 409 and r.json()["error"]["code"] == "DUPLICATE_SUPPLIER_DOC"


# --- access -------------------------------------------------------------------------------


def test_cashier_has_no_finance_access(world, client) -> None:
    h = login(client, "cashier1")
    assert client.get(f"{FIN}/receipt", headers=h).status_code == 403
    assert client.get("/api/v1/issues", headers=h).status_code == 403
