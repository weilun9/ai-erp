"""Step 33: BI dashboard - headline figures vs the previous period, trend buckets, rankings,
channel mix and balances, with cost and finance figures hidden from those who may not see them."""

from datetime import date, timedelta

import pytest

from tests import test_sales
from tests.conftest import login
from tests.test_sales import TODAY, _posted, milo

pytestmark = pytest.mark.db

store = test_sales.store  # 48 MILO at HQ, average cost 1.50; GRNs owe NESTLE 72.00
BI = "/api/v1/reports/bi"


def _bi(client, h, days=30, **extra):
    start = (date.fromisoformat(TODAY) - timedelta(days=days - 1)).isoformat()
    r = client.get(BI, params={"date_from": start, "date_to": TODAY, **extra}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def test_headline_rankings_and_balances(store, client) -> None:
    h = store["h"]
    _posted(client, store, "sales-invoice", [milo(store, "10", unit_price="2.50")])
    bi = _bi(client, h)
    cur = bi["current"]
    assert (cur["sales"], cur["documents"], cur["gross_profit"], cur["margin_pct"]) == (
        "25.0000",
        1,
        "10.0000",
        "40.0000",
    )
    assert cur["avg_sale"] == "25.0000"
    assert bi["previous"]["sales"] == "0.0000"
    assert bi["bucket"] == "day" and len(bi["trend"]) == 30
    assert bi["trend"][-1]["sales"] == "25.0000"
    assert bi["items"][0]["label"] == "MILO MILO" and bi["items"][0]["qty"] == "10.0000"
    assert [c["label"] for c in bi["channels"]] == ["INVOICE"]
    assert bi["customers"][0]["label"].startswith("ACME")
    assert bi["categories"][0]["label"] == "No category"
    snap = bi["snapshot"]
    assert snap["stock_value"] == "57.0000"  # 38 left at 1.50
    assert (snap["customers_owe"], snap["we_owe_suppliers"]) == ("25.0000", "72.0000")


def test_longer_periods_group_by_week_or_month(store, client) -> None:
    assert _bi(client, store["h"], days=120)["bucket"] == "week"
    month = _bi(client, store["h"], days=400)
    assert month["bucket"] == "month"
    assert all(t["period"].endswith("-01") for t in month["trend"])


def test_who_sees_what(store, client) -> None:
    cashier = login(client, "cashier1")
    start = (date.fromisoformat(TODAY) - timedelta(days=6)).isoformat()
    r = client.get(BI, params={"date_from": start, "date_to": TODAY}, headers=cashier)
    assert r.status_code == 403  # no sales documents, no reports

    acct = login(client, "acct")
    bi = _bi(client, acct, days=7)
    assert "gross_profit" in bi["current"]  # accountants see cost and finance fields
    assert "customers_owe" in bi["snapshot"]
