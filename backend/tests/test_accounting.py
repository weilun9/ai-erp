"""Step 31: general ledger - journals from documents (and their voids), manual journal
vouchers, trial balance, profit and loss, balance sheet, account ledger."""

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from tests import test_sales
from tests.conftest import login
from tests.test_sales import TODAY, _do, _doc, _posted, milo

pytestmark = pytest.mark.db

store = test_sales.store  # 2 GRNs posted: MILO 48 pcs for RM 72.00 (avg 1.50), supplier NESTLE
ACC = "/api/v1/accounting"
# Voids are booked on the shop's (Malaysian) business day, which can be a day after UTC.
BIZ = datetime.now(ZoneInfo("Asia/Kuala_Lumpur")).date().isoformat()


def _accounts(client, h):
    rows = client.get(f"{ACC}/accounts", headers=h).json()["items"]
    return {a["system_key"] or a["code"]: a for a in rows}


def _tb(client, h):
    r = client.get(f"{ACC}/trial-balance", params={"as_of": BIZ}, headers=h).json()
    assert r["total_debit"] == r["total_credit"], r
    return {row["code"]: Decimal(row["balance"]) for row in r["rows"]}


def test_purchases_are_booked_and_the_books_balance(store, client) -> None:
    h = store["h"]
    acc = _accounts(client, h)
    tb = _tb(client, h)
    assert tb[acc["INVENTORY"]["code"]] == Decimal("72.00")
    assert tb[acc["AP"]["code"]] == Decimal("72.00")
    again = client.post(f"{ACC}/sync", headers=h).json()
    assert again["journals_made"] == 0  # idempotent

    js = client.get(f"{ACC}/journals", params={"source_type": "GRN"}, headers=h).json()
    assert js["total"] == 2
    ap_line = next(ln for ln in js["items"][0]["lines"] if ln["account_id"] == acc["AP"]["id"])
    assert (ap_line["party_type"], ap_line["party_id"]) == ("S", store["supplier"])


def test_sale_books_revenue_and_cost_and_void_reverses(store, client) -> None:
    h = store["h"]
    acc = _accounts(client, h)
    inv = _posted(client, store, "sales-invoice", [milo(store, "10", unit_price="2.50")])
    pl = client.get(
        f"{ACC}/profit-and-loss", params={"date_from": TODAY, "date_to": BIZ}, headers=h
    ).json()
    assert (pl["net_sales"], pl["gross_profit"], pl["net_profit"]) == ("25.00", "10.00", "10.00")
    tb = _tb(client, h)
    assert tb[acc["AR"]["code"]] == Decimal("25.00")
    assert tb[acc["INVENTORY"]["code"]] == Decimal("57.00")

    bs = client.get(f"{ACC}/balance-sheet", params={"as_of": BIZ}, headers=h).json()
    assert bs["balanced"] is True and bs["profit_to_date"] == "10.00"

    v = _do(client, store, "sales-invoice", inv, "void", reason="wrong customer")
    assert v.status_code == 200, v.text
    pl = client.get(
        f"{ACC}/profit-and-loss", params={"date_from": TODAY, "date_to": BIZ}, headers=h
    ).json()
    assert pl["net_profit"] == "0.00"
    js = client.get(f"{ACC}/journals", params={"source_type": "INV"}, headers=h).json()
    assert sorted(j["is_reversal"] for j in js["items"]) == [False, True]


def test_receipt_moves_receivable_to_cash(store, client) -> None:
    h = store["h"]
    acc = _accounts(client, h)
    _posted(client, store, "sales-invoice", [milo(store, "4", unit_price="2.50")])
    r = client.post(
        "/api/v1/finance/receipt",
        json={
            "doc_date": TODAY,
            "customer_id": store["customer"],
            "location_id": store["hq"],
            "method": "CASH",
            "amount": "10",
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
    tb = _tb(client, h)
    assert tb[acc["CASH"]["code"]] == Decimal("10.00")
    assert acc["AR"]["code"] not in tb or tb[acc["AR"]["code"]] == 0


def test_manual_journal_rules(store, client) -> None:
    h = store["h"]
    acc = _accounts(client, h)
    capital = {
        "journal_date": TODAY,
        "memo": "Owner puts money in",
        "lines": [
            {"account_id": acc["BANK"]["id"], "debit": "1000"},
            {"account_id": acc["CAPITAL"]["id"], "credit": "1000"},
        ],
    }
    j = client.post(f"{ACC}/journals", json=capital, headers=h)
    assert j.status_code == 201, j.text
    assert j.json()["doc_no"].startswith("JV")
    bs = client.get(f"{ACC}/balance-sheet", params={"as_of": BIZ}, headers=h).json()
    assert bs["balanced"] is True and bs["total_equity"] == "1000.00"

    unbalanced = capital | {
        "lines": [
            {"account_id": acc["BANK"]["id"], "debit": "10"},
            {"account_id": acc["CAPITAL"]["id"], "credit": "9"},
        ]
    }
    assert client.post(f"{ACC}/journals", json=unbalanced, headers=h).status_code == 422
    to_ar = capital | {
        "lines": [
            {"account_id": acc["AR"]["id"], "debit": "10"},
            {"account_id": acc["CAPITAL"]["id"], "credit": "10"},
        ]
    }
    assert client.post(f"{ACC}/journals", json=to_ar, headers=h).status_code == 422

    v = client.post(f"{ACC}/journals/{j.json()['id']}/void", json={"reason": "typo"}, headers=h)
    assert v.status_code == 200 and v.json()["status"] == "VOIDED"
    tb = _tb(client, h)
    assert acc["BANK"]["code"] not in tb or tb[acc["BANK"]["code"]] == 0

    ledger = client.get(
        f"{ACC}/ledger/{acc['BANK']['id']}",
        params={"date_from": TODAY, "date_to": BIZ},
        headers=h,
    ).json()
    assert [ln["debit"] for ln in ledger["lines"]] == ["1000.00", "0.00"]
    assert ledger["closing"] == "0.00"


def test_own_accounts_and_who_may_see_the_books(store, client) -> None:
    h = store["h"]
    r = client.post(
        f"{ACC}/accounts", json={"code": "6200", "name": "Rent", "type": "EXPENSE"}, headers=h
    )
    assert r.status_code == 201, r.text
    dup = client.post(
        f"{ACC}/accounts", json={"code": "6200", "name": "x", "type": "EXPENSE"}, headers=h
    )
    assert dup.status_code == 409
    ar = _accounts(client, h)["AR"]
    off = client.patch(
        f"{ACC}/accounts/{ar['id']}", json={"version": ar["version"], "is_active": False}, headers=h
    )
    assert off.status_code == 422

    clerk = login(client, "clerk1")
    assert client.get(f"{ACC}/accounts", headers=clerk).status_code == 403
    acct = login(client, "acct")
    assert (
        client.get(f"{ACC}/trial-balance", params={"as_of": BIZ}, headers=acct).status_code == 200
    )


def test_quotation_books_nothing(store, client) -> None:
    h = store["h"]
    q = _doc(client, store, "quotation", [milo(store, "1")]).json()
    _do(client, store, "quotation", q, "save")
    before = client.get(f"{ACC}/journals", headers=h).json()["total"]
    client.post(f"{ACC}/sync", headers=h)
    assert client.get(f"{ACC}/journals", headers=h).json()["total"] == before
