"""Promotions at the till and loyalty points."""

from datetime import date, timedelta
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.conftest import login
from tests.test_pos import POS, _sale, cash, kopi, till  # noqa: F401
from tests.test_sales import store  # noqa: F401

pytestmark = pytest.mark.db
API = "/api/v1"
TODAY = date.today()


def _promo(t, **over):
    body = {
        "code": "P1",
        "name": "Promo",
        "kind": "PERCENT",
        "value": "10",
        "date_from": (TODAY - timedelta(days=1)).isoformat(),
        "date_to": (TODAY + timedelta(days=1)).isoformat(),
        **over,
    }
    r = t["pc"].post(f"{API}/promotions", json=body, headers=t["h"])
    assert r.status_code == 201, r.text
    return r.json()


def _quote(t, lines, **extra):
    r = t["pc"].post(f"{POS}/quote", json={"lines": lines, **extra}, headers=t["ch"])
    assert r.status_code == 200, r.text
    return r.json()


def test_buy_two_get_one_free_and_the_return_keeps_the_promotion_share(till) -> None:  # noqa: F811
    _promo(
        till,
        code="B2G1",
        name="Buy 2 get 1 free",
        kind="BUY_GET",
        buy_qty=2,
        get_qty=1,
        item_ids=[till["kopi"]],
    )
    q = _quote(till, [kopi("3")])
    # 3 x 3.33 = 9.99, one free (3.33) -> 6.66 + 10% tax 0.67 = 7.33
    assert (q["subtotal"], q["tax_total"], q["total"]) == ("6.66", "0.67", "7.33")
    assert (
        q["promotions"][0]["code"] == "B2G1" and q["lines"][0]["promo_name"] == "Buy 2 get 1 free"
    )

    sale = _sale(till, [kopi("3")], cash("10"))
    assert sale.status_code == 201, sale.text
    s = sale.json()
    assert (D(s["total"]), D(s["lines"][0]["promo_amount"])) == (D("7.35"), D("3.33"))  # 5-sen

    # one of the three comes back: a third of the line, net of its third of the promotion
    r = till["pc"].post(
        f"{POS}/returns",
        json={
            "client_uuid": "00000000-0000-4000-8000-000000000001",
            "receipt_no": s["receipt_no"],
            "lines": [{"line_no": 1, "qty": "1"}],
        },
        headers=till["ch"],
    )
    assert r.status_code == 201, r.text
    assert (D(r.json()["subtotal"]), D(r.json()["tax_total"])) == (D("-2.22"), D("-0.22"))


def test_bundle_dates_and_offline_sales(till) -> None:  # noqa: F811
    _promo(till, code="2FOR5", name="2 for RM5", kind="BUNDLE", buy_qty=2, value="5")
    _promo(
        till,
        code="OLD",
        name="Expired",
        kind="PERCENT",
        value="50",
        date_from=(TODAY - timedelta(days=10)).isoformat(),
        date_to=(TODAY - timedelta(days=5)).isoformat(),
    )
    q = _quote(till, [kopi("3")])
    assert [x["code"] for x in q["promotions"]] == ["2FOR5"]
    assert q["subtotal"] == "8.33"  # 2 for 5.00 + 3.33

    # rung up offline: the customer paid what the till showed then, list price
    s = _sale(till, [kopi("1")], cash("3.70"), apply_promotions=False).json()
    assert D(s["lines"][0]["promo_amount"]) == 0


def test_member_promotion_points_earned_and_spent(till) -> None:  # noqa: F811
    _promo(till, code="MEM10", name="Members 10%", kind="PERCENT", value="10", members_only=True)
    m = till["pc"].post(
        f"{API}/members",
        json={"card_no": "M0001", "name": "Ali", "phone": "0123456789"},
        headers=till["ch"],
    )
    assert m.status_code == 201, m.text  # the cashier signs members up
    m = m.json()

    assert _quote(till, [kopi("10")])["promotions"] == []
    q = _quote(till, [kopi("10")], member_card="0123456789")  # by phone number
    assert q["member"]["name"] == "Ali" and q["promotions"][0]["code"] == "MEM10"
    # 33.30 - 3.33 = 29.97 + 3.00 tax = 32.97
    s = _sale(till, [kopi("10")], [{"method": "CARD", "amount": "32.97"}], member_card="M0001")
    assert s.status_code == 201, s.text
    assert s.json()["points_earned"] == 32

    adj = till["pc"].post(
        f"{API}/members/{m['id']}/adjust", json={"points": 500, "note": "gift"}, headers=till["h"]
    )
    assert adj.json()["points"] == 532

    # pay a 3.66 kopi with 366 points: no points earned on what points paid
    one = _sale(
        till,
        [kopi("1")],
        [{"method": "POINTS", "amount": "3.33"}],
        member_card="M0001",
        apply_promotions=False,
    )
    assert one.status_code == 422  # points must cover the bill exactly here: 3.66 with tax
    paid = _sale(
        till,
        [kopi("1")],
        [{"method": "POINTS", "amount": "3.66"}],
        member_card="M0001",
        apply_promotions=False,
    )
    assert paid.status_code == 201, paid.text
    assert (paid.json()["points_used"], paid.json()["points_earned"]) == (366, 0)
    after = till["pc"].get(f"{API}/members/lookup", params={"card": "M0001"}, headers=till["ch"])
    assert after.json()["points"] == 166

    back = till["pc"].post(
        f"{POS}/returns",
        json={
            "client_uuid": "00000000-0000-4000-8000-000000000002",
            "receipt_no": paid.json()["receipt_no"],
            "lines": [{"line_no": 1, "qty": "1"}],
            "refund_method": "POINTS",
        },
        headers=till["ch"],
    )
    assert back.status_code == 201, back.text
    hist = till["pc"].get(f"{API}/members/{m['id']}/points", headers=till["ch"]).json()
    assert hist[0]["kind"] == "REFUND" and hist[0]["balance"] == 532

    # the books: points spent are a loyalty cost
    tb = (
        till["pc"]
        .get(
            f"{API}/accounting/trial-balance",
            params={"as_of": TODAY.isoformat()},
            headers=till["h"],
        )
        .json()
    )
    assert tb["total_debit"] == tb["total_credit"]

    nobody = _sale(till, [kopi("1")], [{"method": "POINTS", "amount": "3.66"}])
    assert nobody.status_code == 422


def test_only_managers_set_up_promotions(till) -> None:  # noqa: F811
    body = {
        "code": "X",
        "name": "X",
        "kind": "PERCENT",
        "value": "5",
        "date_from": TODAY.isoformat(),
        "date_to": TODAY.isoformat(),
    }
    cashier = TestClient(app)
    r = cashier.post(f"{API}/promotions", json=body, headers=login(cashier, "cashier1"))
    assert r.status_code == 403
    bad = till["pc"].post(f"{API}/promotions", json={**body, "kind": "BUY_GET"}, headers=till["h"])
    assert bad.status_code == 400
