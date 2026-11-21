"""Bank reconciliation and year-end close."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests import test_sales
from tests.conftest import login
from tests.test_accounting import ACC, BIZ, _accounts, _tb
from tests.test_sales import _posted, milo

pytestmark = pytest.mark.db
store = test_sales.store
BANK = f"{ACC}/bank"
D = date.fromisoformat(BIZ)


def day(n: int) -> str:
    return (D + timedelta(days=n)).isoformat()


def _jv(client, h, when, dr, cr, amount, memo="test"):
    r = client.post(
        f"{ACC}/journals",
        json={
            "journal_date": when,
            "memo": memo,
            "lines": [{"account_id": dr, "debit": amount}, {"account_id": cr, "credit": amount}],
        },
        headers=h,
    )
    return r


def test_statement_is_matched_booked_and_reconciled(store, client) -> None:
    h = store["h"]
    acc = _accounts(client, h)
    bank, capital, charges = acc["BANK"]["id"], acc["CAPITAL"]["id"], acc["PURCHASE_EXP"]["id"]
    assert _jv(client, h, day(0), bank, capital, "1000").status_code == 201
    assert _jv(client, h, day(0), charges, bank, "200").status_code == 201

    lines = [
        {"txn_date": day(0), "amount": "1000.00", "description": "Owner deposit"},
        {"txn_date": day(1), "amount": "-200.00", "description": "Cheque 001"},
        {"txn_date": day(1), "amount": "-15.00", "description": "Service charge"},
    ]
    r = client.post(f"{BANK}/statement", json={"lines": lines}, headers=h)
    assert r.json() == {"added": 3, "skipped": 0}
    again = client.post(f"{BANK}/statement", json={"lines": lines}, headers=h).json()
    assert again == {"added": 0, "skipped": 3}  # pasting the same statement twice is safe

    assert client.post(f"{BANK}/auto-match", headers=h).json() == {"matched": 2}
    open_ = client.get(f"{BANK}/statement", params={"unmatched_only": True}, headers=h).json()
    assert [x["amount"] for x in open_] == ["-15.00"]

    fee = open_[0]["id"]
    booked = client.post(f"{BANK}/statement/{fee}/book", json={"account_id": charges}, headers=h)
    assert booked.status_code == 201, booked.text
    assert client.get(f"{BANK}/statement", params={"unmatched_only": True}, headers=h).json() == []
    # (the charge is booked on its statement date, tomorrow; the reconciliation below sees it)

    rec = client.get(
        f"{BANK}/reconciliation", params={"as_of": day(1), "statement_balance": "785"}, headers=h
    ).json()
    assert (rec["book_balance"], rec["difference"]) == ("785.00", "0.00")

    # a deposit in the books that the bank has not shown yet
    assert _jv(client, h, day(1), bank, capital, "300").status_code == 201
    rec = client.get(
        f"{BANK}/reconciliation", params={"as_of": day(1), "statement_balance": "785"}, headers=h
    ).json()
    assert rec["deposits_in_transit"] == "300.00"
    assert (rec["book_balance"], rec["expected_statement_balance"], rec["difference"]) == (
        "1085.00",
        "785.00",
        "0.00",
    )

    # matching must add up, and can be undone
    dep = client.get(f"{BANK}/book", headers=h).json()
    assert [x["amount"] for x in dep] == ["300.00"]
    late = client.post(
        f"{BANK}/statement",
        json={"lines": [{"txn_date": day(2), "amount": "299.00", "description": "Deposit"}]},
        headers=h,
    )
    assert late.status_code == 201
    txn = client.get(f"{BANK}/statement", params={"unmatched_only": True}, headers=h).json()[0]
    bad = client.post(
        f"{BANK}/statement/{txn['id']}/match", json={"journal_line_ids": [dep[0]["id"]]}, headers=h
    )
    assert bad.status_code == 422
    assert client.get(f"{BANK}/statement/{txn['id']}/suggest", headers=h).json() == []

    cashier = TestClient(app)
    assert cashier.get(f"{BANK}/statement", headers=login(cashier, "cashier1")).status_code == 403


def test_year_end_close_moves_the_result_and_locks_the_year(store, client) -> None:
    h = store["h"]
    acc = _accounts(client, h)
    bank, income, retained = acc["BANK"]["id"], acc["OTHER_INCOME"]["id"], acc["RETAINED"]["id"]
    assert _jv(client, h, day(-10), bank, income, "500").status_code == 201

    assert client.post(f"{ACC}/year-end", json={"year_end": BIZ}, headers=h).status_code == 422
    r = client.post(f"{ACC}/year-end", json={"year_end": day(-5)}, headers=h)
    assert r.status_code == 201, r.text
    assert r.json()["net_profit"] == "500.00"

    # the closed year's P&L still shows its result; the balance sheet holds it as retained
    pl = client.get(
        f"{ACC}/profit-and-loss", params={"date_from": day(-30), "date_to": day(-5)}, headers=h
    ).json()
    assert pl["net_profit"] == "500.00"
    tb = _tb(client, h)
    assert tb[acc["RETAINED"]["code"]] == Decimal("500.00")
    assert acc["OTHER_INCOME"]["code"] not in tb or tb[acc["OTHER_INCOME"]["code"]] == 0
    bs = client.get(f"{ACC}/balance-sheet", params={"as_of": BIZ}, headers=h).json()
    assert bs["balanced"] is True

    # nothing can be booked in the closed year; a late document lands on the first open day
    assert _jv(client, h, day(-7), bank, income, "1").status_code == 422
    inv = _posted(
        client, store, "sales-invoice", [milo(store, "2", unit_price="2.50")], doc_date=day(-7)
    )
    journals = client.get(f"{ACC}/journals", params={"date_from": day(-30)}, headers=h).json()
    booked = [j for j in journals["items"] if j.get("source_no") == inv["doc_no"]]
    assert booked and booked[0]["journal_date"] == day(-4)

    assert client.post(f"{ACC}/year-end", json={"year_end": day(-6)}, headers=h).status_code == 422
    listed = client.get(f"{ACC}/year-end", headers=h).json()
    assert listed["closed_through"] == day(-5)

    # reopening reverses the close and frees the dates again
    assert client.post(f"{ACC}/year-end/reopen", headers=h).status_code == 200
    assert client.get(f"{ACC}/year-end", headers=h).json()["closed_through"] is None
    assert _tb(client, h).get(acc["RETAINED"]["code"], Decimal(0)) == 0
    assert _jv(client, h, day(-7), bank, income, "1").status_code == 201
    _ = retained
