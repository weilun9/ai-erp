import uuid
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db.session import SessionLocal
from app.localization.my import TIMEZONE
from app.main import app
from app.models import EventLog, PosSale
from tests.conftest import login
from tests.test_purchasing import _parallel
from tests.test_sales import _balance, store  # noqa: F401

pytestmark = pytest.mark.db
POS = "/api/v1/pos"
BOSS = {"username": "boss", "password": "Test-password-123"}


@pytest.fixture
def till(store, client):  # noqa: F811
    """KOPI: PCS 3.33 + 10% sales tax, barcode 9556345310072 (100 in stock at HQ, cost 2.00).
    Cashier1 may work at HQ and has a shift open on this till with a RM200 float."""
    h = store["h"]
    taxes = {t["code"]: t["id"] for t in client.get("/api/v1/tax-codes", headers=h).json()["items"]}
    kopi = client.post(
        "/api/v1/items",
        json={
            "code": "KOPI",
            "name": "Kopi 3in1",
            "base_uom": "PCS",
            "sales_tax_code_id": taxes["ST10"],
            "uoms": [{"uom": "PCS", "factor": "1", "selling_price": "3.33"}],
            "barcodes": [{"barcode": "9556345310072", "uom": "PCS"}],
        },
        headers=h,
    ).json()["id"]
    grn = client.post(
        "/api/v1/purchase/grn",
        json={
            "doc_date": date.today().isoformat(),
            "supplier_id": store["supplier"],
            "location_id": store["hq"],
            "lines": [{"item_id": kopi, "uom": "PCS", "qty": "100", "unit_price": "2"}],
        },
        headers=h,
    ).json()
    for action in ("save", "post"):
        grn = client.post(
            f"/api/v1/purchase/grn/{grn['id']}/{action}",
            json={"version": grn["version"]},
            headers=h,
        ).json()
    users = client.get("/api/v1/users", headers=h).json()["items"]
    me = next(u for u in users if u["username"] == "cashier1")
    client.patch(
        f"/api/v1/users/{me['id']}",
        json={"location_ids": [x["id"] for x in me["locations"]] + [store["hq"]]},
        headers=h,
    )
    counter = TestClient(app)  # the shop's till
    ch = login(counter, "cashier1")
    shift = counter.post(
        f"{POS}/shifts", json={"location_id": store["hq"], "opening_float": "200"}, headers=ch
    )
    assert shift.status_code == 201, shift.text
    return {**store, "kopi": kopi, "pc": counter, "ch": ch, "shift": shift.json()}


def _sale(t, lines, payments, **extra):
    body = {"client_uuid": str(uuid.uuid4()), "lines": lines, "payments": payments, **extra}
    return t["pc"].post(f"{POS}/sales", json=body, headers=t["ch"])


def kopi(qty="1", **kw):
    return {"barcode": "955634531007", "qty": qty, **kw}  # scanner drops the check digit


def cash(amount):
    return [{"method": "CASH", "amount": amount}]


# --- selling ------------------------------------------------------------------------------


def test_cash_sale_rounds_to_five_sen_and_moves_stock(till, client) -> None:
    r = _sale(till, [kopi()], cash("5"))
    assert r.status_code == 201, r.text
    s = r.json()
    assert (s["subtotal"], s["tax_total"]) == ("3.3300", "0.3300")
    assert (s["rounding"], s["total"], s["change"]) == ("-0.0100", "3.6500", "1.3500")
    assert s["receipt_no"] == "POS000001" and s["lines"][0]["barcode"] == "9556345310072"
    assert "cost_total" not in s and "unit_cost" not in s["lines"][0]  # cashier sees no cost
    assert _balance(client, till, till["kopi"]) == ("99.0000", "2.0000")
    owner_view = client.get(f"{POS}/sales/POS000001", headers=till["h"]).json()
    assert owner_view["cost_total"] == "2.0000"


def test_card_is_exact_and_cannot_overpay(till) -> None:
    ok = _sale(till, [kopi()], [{"method": "CARD", "amount": "3.66", "reference": "A1"}])
    assert ok.status_code == 201 and ok.json()["rounding"] == "0.0000"
    short = _sale(till, [kopi()], [{"method": "CARD", "amount": "3.65"}])
    over = _sale(till, [kopi()], [{"method": "CARD", "amount": "4"}])
    assert short.status_code == over.status_code == 422
    mixed = _sale(till, [kopi()], [{"method": "CARD", "amount": "2"}, *cash("2")])
    m = mixed.json()
    assert (m["total"], m["change"]) == ("3.6500", "0.3500")  # cash part 1.66 -> 1.65
    assert {p["method"]: p["amount"] for p in m["payments"]} == {"CARD": "2.0000", "CASH": "1.6500"}
    assert _sale(till, [kopi()], cash("3.60")).status_code == 422  # not enough


def test_same_client_uuid_is_recorded_once(till, client) -> None:
    body = {"client_uuid": str(uuid.uuid4()), "lines": [kopi("2")], "payments": cash("10")}
    first = till["pc"].post(f"{POS}/sales", json=body, headers=till["ch"])
    again = till["pc"].post(f"{POS}/sales", json=body, headers=till["ch"])
    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json()["replayed"] and again.json()["id"] == first.json()["id"]

    body["client_uuid"] = str(uuid.uuid4())

    def send():
        c = TestClient(app)
        return lambda: c.post(f"{POS}/sales", json=body, headers=till["ch"]).status_code

    assert sorted(_parallel(send(), send())) == [200, 201]
    with SessionLocal() as db:
        n = db.scalar(select(func.count()).where(PosSale.client_uuid == body["client_uuid"]))
    assert n == 1
    assert _balance(client, till, till["kopi"]) == ("96.0000", "2.0000")  # 2 + 2 only


def test_offline_sale_keeps_its_time_but_must_fit_the_shift(till) -> None:
    earlier = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    r = _sale(till, [kopi()], cash("5"), sold_at=earlier)
    assert r.status_code == 201 and r.json()["sold_at"].startswith(earlier[:16])
    old = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    assert _sale(till, [kopi()], cash("5"), sold_at=old).status_code == 400


def test_price_override_needs_a_supervisor(till) -> None:
    line = [kopi(unit_price="3.00")]
    no = _sale(till, line, cash("5"))
    assert no.status_code == 403
    assert no.json()["error"]["details"]["code"] == "SUPERVISOR_REQUIRED"
    wrong = _sale(till, line, cash("5"), supervisor={**BOSS, "password": "guess"})
    assert wrong.status_code == 403
    clerk = _sale(
        till, line, cash("5"), supervisor={"username": "clerk1", "password": BOSS["password"]}
    )
    assert clerk.status_code == 403  # a real login, but not a supervisor
    ok = _sale(till, line, cash("5"), supervisor=BOSS)
    assert ok.status_code == 201 and ok.json()["approved_by"]
    assert ok.json()["lines"][0]["price_source"] == "OVERRIDE"
    assert _sale(till, [kopi(discount_pct="10")], cash("5")).status_code == 403
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).where(EventLog.action == "pos_override")) == 1


def test_selling_needs_your_own_open_shift(store, client) -> None:  # noqa: F811
    pc = TestClient(app)
    ch = login(pc, "cashier1")
    body = {
        "client_uuid": str(uuid.uuid4()),
        "lines": [{"item_id": store["milo"], "qty": "1"}],
        "payments": cash("5"),
    }
    r = pc.post(f"{POS}/sales", json=body, headers=ch)
    assert r.status_code == 409 and "Open a shift" in r.json()["error"]["message"]


def test_one_open_shift_per_till(till) -> None:
    r = till["pc"].post(
        f"{POS}/shifts", json={"location_id": till["hq"], "opening_float": "0"}, headers=till["ch"]
    )
    assert r.status_code == 409


# --- returns ------------------------------------------------------------------------------


def _return(t, receipt, lines, **extra):
    body = {"client_uuid": str(uuid.uuid4()), "receipt_no": receipt, "lines": lines, **extra}
    return t["pc"].post(f"{POS}/returns", json=body, headers=t["ch"])


def test_return_refunds_the_way_it_was_paid_and_restocks_at_cost(till, client) -> None:
    sale = _sale(till, [kopi("3")], [{"method": "CARD", "amount": "10.99"}]).json()
    r = _return(till, sale["receipt_no"], [{"line_no": 1, "qty": "2"}], reason="expired")
    assert r.status_code == 201, r.text
    ret = r.json()
    assert ret["kind"] == "RETURN" and ret["receipt_no"] == "RTN000001"
    # 2 of 3: 6.66 + 10% tax (0.666 -> 0.67)
    assert ret["total"] == "-7.3300" and ret["payments"] == [
        {"method": "CARD", "amount": "-7.3300", "reference": None}
    ]
    assert _balance(client, till, till["kopi"]) == ("99.0000", "2.0000")
    over = _return(till, sale["receipt_no"], [{"line_no": 1, "qty": "2"}])
    assert over.status_code == 422 and over.json()["error"]["details"]["can_return"] == "1.0000"
    cash_refund = _return(
        till, sale["receipt_no"], [{"line_no": 1, "qty": "1"}], refund_method="CASH"
    )
    assert cash_refund.status_code == 422  # it was paid by card


def test_two_tills_cannot_both_refund_the_last_one(till) -> None:
    sale = _sale(till, [kopi("1")], cash("5")).json()
    other = TestClient(app)
    oh = login(other, "boss")
    other.post(f"{POS}/shifts", json={"location_id": till["hq"], "opening_float": "0"}, headers=oh)

    def refund(pc, h):
        body = {
            "client_uuid": str(uuid.uuid4()),
            "receipt_no": sale["receipt_no"],
            "lines": [{"line_no": 1, "qty": "1"}],
        }
        c = TestClient(app, cookies=pc.cookies)
        return lambda: c.post(f"{POS}/returns", json=body, headers=h).status_code

    assert sorted(_parallel(refund(till["pc"], till["ch"]), refund(other, oh))) == [201, 422]


def test_large_refund_needs_a_supervisor(till) -> None:
    sale = _sale(till, [{"item_id": till["milo"], "uom": "CTN", "qty": "1"}], cash("55")).json()
    no = _return(till, sale["receipt_no"], [{"line_no": 1, "qty": "1"}])
    assert no.status_code == 403
    ok = _return(till, sale["receipt_no"], [{"line_no": 1, "qty": "1"}], supervisor=BOSS)
    assert ok.status_code == 201 and ok.json()["total"] == "-55.0000"


# --- closing ------------------------------------------------------------------------------


def test_close_shift_counts_cash_and_needs_a_note_when_off(till) -> None:
    _sale(till, [kopi()], cash("5"))  # +3.65 cash
    _sale(till, [kopi()], [{"method": "EWALLET", "amount": "3.66"}])
    sale = _sale(till, [kopi("2")], cash("10")).json()  # +7.30 cash
    _return(till, sale["receipt_no"], [{"line_no": 1, "qty": "1"}])  # -3.65 cash
    sid = till["shift"]["id"]
    rep = till["pc"].get(f"{POS}/shifts/{sid}/report", headers=till["ch"]).json()
    assert rep["expected_cash"] == "207.35"  # 200 + 3.65 + 7.35 (7.33) - 3.65 (3.66)
    assert rep["payments"] == {"CASH": "7.35", "EWALLET": "3.66"}
    assert rep["sales"]["count"] == 3 and rep["returns"]["count"] == 1

    url = f"{POS}/shifts/{sid}/close"
    off = till["pc"].post(url, json={"counted_cash": "200"}, headers=till["ch"])
    assert off.status_code == 422 and off.json()["error"]["details"]["variance"] == "-7.35"
    ok = till["pc"].post(url, json={"counted_cash": "207.20"}, headers=till["ch"]).json()
    assert ok["status"] == "CLOSED" and ok["variance"] == "-0.1500"
    assert _sale(till, [kopi()], cash("5")).status_code == 409  # shift is closed


def test_reports_include_the_till(till, client) -> None:
    _sale(till, [kopi("2")], cash("10"))
    today = datetime.now(ZoneInfo(TIMEZONE)).date().isoformat()  # the shop's business day
    summary = client.get(
        "/api/v1/reports/sales-summary",
        params={"date_from": today, "date_to": today, "group_by": "customer"},
        headers=till["h"],
    ).json()
    walk_in = next(r for r in summary["rows"] if r["customer"] == "Walk-in (POS)")
    assert walk_in["sales"] == "6.6600" and walk_in["gross_profit"] == "2.6600"
    tax = client.get(
        f"{POS}/tax-summary", params={"date_from": today, "date_to": today}, headers=till["h"]
    ).json()
    assert tax["receipts"] == 1 and tax["first_receipt"] == "POS000001"
    assert tax["by_tax"] == [
        {"tax_code": "ST10", "rate": "10.0000", "kind": "SALE", "subtotal": "6.66", "tax": "0.67"}
    ]


def test_returns_in_pieces_never_refund_more_tax_than_charged(till, client) -> None:
    h = till["h"]
    taxes = {t["code"]: t["id"] for t in client.get("/api/v1/tax-codes", headers=h).json()["items"]}
    gum = client.post(
        "/api/v1/items",
        json={
            "code": "GUM",
            "name": "Gum",
            "base_uom": "PCS",
            "sales_tax_code_id": taxes["ST10"],
            "uoms": [{"uom": "PCS", "factor": "1", "selling_price": "0.05"}],
        },
        headers=h,
    ).json()["id"]
    sale = _sale(till, [{"item_id": gum, "qty": "2"}], [{"method": "CARD", "amount": "0.11"}])
    assert sale.json()["tax_total"] == "0.0100"  # 0.10 x 10%
    back = []
    for _ in range(2):
        r = _return(till, sale.json()["receipt_no"], [{"line_no": 1, "qty": "1"}]).json()
        back.append(r["tax_total"])
    assert back == ["-0.0100", "0.0000"]  # 0.005 rounds up once, then exactly what is left


def test_quote_prices_like_the_sale_and_saves_nothing(till, client) -> None:
    body = {"lines": [kopi("2"), kopi("1", unit_price="3.00")]}
    q = till["pc"].post(f"{POS}/quote", json=body, headers=till["ch"]).json()
    assert [(x["code"], x["line_total"]) for x in q["lines"]] == [
        ("KOPI", "7.33"),
        ("KOPI", "3.30"),
    ]
    assert (q["total"], q["cash_total"]) == ("10.63", "10.65")
    assert q["needs_supervisor"] == ["line 2: price 3.00 instead of 3.3300"]
    assert _balance(client, till, till["kopi"]) == ("100.0000", "2.0000")  # nothing sold
