"""Step 30: supplier debit notes, credit notes without goods, and refunds from suppliers."""

import pytest

from tests import test_sales
from tests.test_sales import TODAY

pytestmark = pytest.mark.db

store = test_sales.store  # supplier NESTLE with posted GRNs INV-A (24.00) and INV-B (48.00)
NOTES = "/api/v1/supplier-notes"
FIN = "/api/v1/finance"


def _note(client, s, kind, ref, lines, **extra):
    r = client.post(
        f"{NOTES}/{kind}",
        json={
            "doc_date": TODAY,
            "supplier_id": s["supplier"],
            "location_id": s["hq"],
            "supplier_ref_no": ref,
            "lines": lines,
            **extra,
        },
        headers=s["h"],
    )
    assert r.status_code == 201, r.text
    return r.json()


def _act(client, s, base, doc, action, **extra):
    return client.post(
        f"{base}/{doc['id']}/{action}", json={"version": doc["version"], **extra}, headers=s["h"]
    )


def _posted(client, s, kind, ref, lines, **extra):
    d = _note(client, s, kind, ref, lines, **extra)
    saved = _act(client, s, f"{NOTES}/{kind}", d, "save")
    assert saved.status_code == 200, saved.text
    posted = _act(client, s, f"{NOTES}/{kind}", saved.json(), "post")
    assert posted.status_code == 200, posted.text
    return posted.json()


def _balance(client, s):
    r = client.get(
        f"{FIN}/open-items",
        params={"party_type": "supplier", "party_id": s["supplier"]},
        headers=s["h"],
    ).json()
    return r["balance"], {i["doc_no"]: i["open"] for i in r["items"]}


def _pay(client, s, lines):
    d = client.post(
        f"{FIN}/supplier-payment",
        json={
            "doc_date": TODAY,
            "supplier_id": s["supplier"],
            "location_id": s["hq"],
            "lines": lines,
        },
        headers=s["h"],
    )
    assert d.status_code == 201, d.text
    d = _act(client, s, f"{FIN}/supplier-payment", d.json(), "save").json()
    r = _act(client, s, f"{FIN}/supplier-payment", d, "post")
    assert r.status_code == 200, r.text
    return d


def test_debit_note_raises_what_we_owe_and_is_paid_like_an_invoice(store, client) -> None:
    taxes = {
        t["code"]: t["id"]
        for t in client.get("/api/v1/tax-codes", headers=store["h"]).json()["items"]
    }
    before, _ = _balance(client, store)
    dn = _posted(
        client,
        store,
        "debit-note",
        "DN-501",
        [
            {
                "description": "Freight not billed on INV-A",
                "ref_doc_no": "INV-A",
                "amount": "10.00",
                "tax_code_id": taxes["SV8"],
            },
        ],
    )
    assert (dn["doc_no"][:2], dn["total"], dn["open"]) == ("DN", "10.8000", "10.8000")
    after, items = _balance(client, store)
    assert float(after) - float(before) == pytest.approx(10.8)

    pv = _pay(client, store, [{"line_type": "INVOICE", "ref_no": "dn 501", "amount": "10.80"}])
    assert pv["lines"][0]["matched_type"] == "SDN"
    got = client.get(f"{NOTES}/debit-note/{dn['id']}", headers=store["h"]).json()
    assert got["open"] == "0.0000"


def test_credit_adjustment_lowers_what_we_owe_without_moving_stock(store, client) -> None:
    stock_before = client.get(
        "/api/v1/stock/balance", params={"item_id": store["milo"]}, headers=store["h"]
    ).json()["items"]
    before, _ = _balance(client, store)
    _posted(
        client,
        store,
        "credit-adjustment",
        "CA-9",
        [{"description": "Price adjustment", "amount": "5"}],
    )
    after, _ = _balance(client, store)
    assert float(before) - float(after) == pytest.approx(5)
    stock_after = client.get(
        "/api/v1/stock/balance", params={"item_id": store["milo"]}, headers=store["h"]
    ).json()["items"]
    assert stock_after == stock_before

    # a payment advice deducts it as a CN line
    pv = _pay(
        client,
        store,
        [
            {"line_type": "INVOICE", "ref_no": "INV-A", "amount": "24"},
            {"line_type": "CN", "ref_no": "CA-9", "amount": "5"},
        ],
    )
    assert [ln["matched_type"] for ln in pv["lines"]] == ["GRN", "SCA"]
    assert pv["amount"] == "19.0000"


def test_refund_settles_supplier_credits(store, client) -> None:
    ca = _posted(
        client, store, "credit-adjustment", "CA-20", [{"description": "Rebate", "amount": "30"}]
    )
    _, items = _balance(client, store)
    assert items[ca["doc_no"]] == "30.0000"

    too_much = _note(client, store, "refund", "RF-1", [], amount="40", method="TRANSFER")
    too_much = _act(client, store, f"{NOTES}/refund", too_much, "save").json()
    r = _act(client, store, f"{NOTES}/refund", too_much, "post")
    assert r.status_code == 422

    rf = _posted(
        client,
        store,
        "refund",
        "RF-2",
        [{"doc_type": "SCA", "doc_id": ca["id"], "amount": "30"}],
        amount="30",
        method="CHEQUE",
        bank_ref="CHQ 123",
    )
    assert rf["doc_no"].startswith("SRF") and rf["open"] == "0.0000"
    _, items = _balance(client, store)
    assert ca["doc_no"] not in items

    # voiding the refund gives the credit back
    v = _act(client, store, f"{NOTES}/refund", rf, "void", reason="cheque bounced")
    assert v.status_code == 200, v.text
    _, items = _balance(client, store)
    assert items[ca["doc_no"]] == "30.0000"


def test_refund_without_lines_takes_oldest_credits(store, client) -> None:
    a = _posted(
        client, store, "credit-adjustment", "CA-31", [{"description": "Rebate", "amount": "10"}]
    )
    b = _posted(
        client, store, "credit-adjustment", "CA-32", [{"description": "Rebate", "amount": "10"}]
    )
    _posted(client, store, "refund", "RF-3", [], amount="15", method="TRANSFER")
    _, items = _balance(client, store)
    assert a["doc_no"] not in items and items[b["doc_no"]] == "5.0000"


def test_notes_need_lines_and_unique_supplier_numbers(store, client) -> None:
    d = _note(client, store, "debit-note", "DN-1", [])
    assert _act(client, store, f"{NOTES}/debit-note", d, "save").status_code == 422
    _posted(client, store, "debit-note", "DN-2", [{"description": "x", "amount": "1"}])
    dup = _note(client, store, "debit-note", "dn-2", [{"description": "y", "amount": "1"}])
    assert _act(client, store, f"{NOTES}/debit-note", dup, "save").status_code == 422
    bad = client.post(
        f"{NOTES}/refund",
        json={
            "doc_date": TODAY,
            "supplier_id": store["supplier"],
            "location_id": store["hq"],
            "amount": "5",
            "lines": [{"amount": "5"}],
        },
        headers=store["h"],
    )
    assert bad.status_code == 400
