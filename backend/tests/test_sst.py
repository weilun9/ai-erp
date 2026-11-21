"""SST return working paper: sales and till sales less credit notes and refunds, per code."""

import uuid
from datetime import date
from decimal import Decimal as D

import pytest

from tests.test_pos import POS, cash, kopi, till  # noqa: F401
from tests.test_sales import _do, _doc, _posted, milo, store  # noqa: F401

pytestmark = pytest.mark.db
TODAY = date.today().isoformat()


def test_sst_return_adds_up_sales_less_returns_per_tax_code(till, client) -> None:  # noqa: F811
    s = till
    taxes = {
        t["code"]: t["id"] for t in client.get("/api/v1/tax-codes", headers=s["h"]).json()["items"]
    }
    st10, sv6 = taxes["ST10"], taxes["SV6"]

    # invoice: 4 x 2.50 at 10% sales tax, plus a service line at 6%
    inv = _posted(
        client,
        s,
        "sales-invoice",
        [
            milo(s, "4", unit_price="2.50", tax_code_id=st10),
            milo(s, "2", unit_price="5.00", tax_code_id=sv6),
        ],
    )
    # a credit note for one piece of the first line
    cn = _doc(
        client,
        s,
        "customer-cn",
        [milo(s, "1", unit_price="2.50", tax_code_id=st10)],
        ref_invoice_id=inv["id"],
    ).json()
    cn = _do(client, s, "customer-cn", cn, "save").json()
    assert _do(client, s, "customer-cn", cn, "post").status_code == 200
    # a voided invoice counts for nothing
    gone = _posted(client, s, "sales-invoice", [milo(s, "10", unit_price="2.50", tax_code_id=st10)])
    assert _do(client, s, "sales-invoice", gone, "void", reason="typo").status_code == 200
    # the till: one KOPI at 3.33 + 10%
    sale = s["pc"].post(
        f"{POS}/sales",
        json={"client_uuid": str(uuid.uuid4()), "lines": [kopi()], "payments": cash("5")},
        headers=s["ch"],
    )
    assert sale.status_code == 201, sale.text

    r = client.get(
        "/api/v1/reports/sst-return", params={"date_from": TODAY, "date_to": TODAY}, headers=s["h"]
    )
    assert r.status_code == 200, r.text
    rep = r.json()
    rows = {x["code"]: x for x in rep["rows"]}
    st = rows["ST10"]
    assert (D(st["sales_value"]), D(st["sales_tax"])) == (D("13.33"), D("1.33"))
    assert (D(st["less_value"]), D(st["less_tax"])) == (D("2.50"), D("0.25"))
    assert (D(st["net_value"]), D(st["net_tax"])) == (D("10.83"), D("1.08"))
    assert st["kind"] == "SALES" and rows["SV6"]["kind"] == "SERVICE"
    assert D(rows["SV6"]["net_tax"]) == D("0.60")
    assert D(rep["sales_tax_payable"]) == D("1.08")
    assert D(rep["service_tax_payable"]) == D("0.60")
    assert D(rep["total_payable"]) == D("1.68")
    assert rep["documents"] == {"invoices": 1, "credit_notes": 1, "till_sales": 1}

    bad = client.get(
        "/api/v1/reports/sst-return",
        params={"date_from": TODAY, "date_to": "2000-01-01"},
        headers=s["h"],
    )
    assert bad.status_code == 400
