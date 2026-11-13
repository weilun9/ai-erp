"""Step 29: supplier invoice header extras - D/O no., reference, import declaration,
terms -> due date, document discount spread over the lines, tax-inclusive prices,
cent adjustment, and paid / balance on a posted GRN."""

from datetime import date, timedelta

import pytest

from tests import test_sales
from tests.test_sales import TODAY

pytestmark = pytest.mark.db

store = test_sales.store  # MILO (CTN of 24), SOAP, supplier NESTLE, HQ
GRN = "/api/v1/purchase/grn"


def _taxes(client, s):
    rows = client.get("/api/v1/tax-codes", headers=s["h"]).json()["items"]
    return {t["code"]: t["id"] for t in rows}


def _grn(client, s, lines, **over):
    body = {
        "doc_date": TODAY,
        "supplier_id": s["supplier"],
        "location_id": s["hq"],
        "supplier_ref_no": over.pop("ref", "INV-H1"),
        "lines": lines,
        **over,
    }
    r = client.post(GRN, json=body, headers=s["h"])
    assert r.status_code == 201, r.text
    return r.json()


def test_header_fields_and_due_date_from_supplier_terms(store, client) -> None:
    h = store["h"]
    sup = client.get(f"/api/v1/suppliers/{store['supplier']}", headers=h).json()
    client.patch(
        f"/api/v1/suppliers/{store['supplier']}",
        json={"version": sup["version"], "term_days": 45},
        headers=h,
    )
    g = _grn(
        client,
        store,
        [{"item_id": store["milo"], "uom": "CTN", "qty": "1", "unit_price": "48"}],
        supplier_do_no="DO-778",
        reference="PO by phone",
        import_decl_no="K1-2026-001",
    )
    assert (g["supplier_do_no"], g["reference"], g["import_decl_no"]) == (
        "DO-778",
        "PO by phone",
        "K1-2026-001",
    )
    assert g["term_days"] == 45
    assert g["due_date"] == (date.fromisoformat(TODAY) + timedelta(days=45)).isoformat()
    g = client.patch(
        f"{GRN}/{g['id']}", json={"version": g["version"], "term_days": 0}, headers=h
    ).json()
    assert g["due_date"] == TODAY


def test_document_discount_is_spread_over_lines_and_cost(store, client) -> None:
    g = _grn(
        client,
        store,
        [
            {"item_id": store["milo"], "uom": "CTN", "qty": "1", "unit_price": "60.00"},
            {"item_id": store["soap"], "uom": "PCS", "qty": "10", "unit_price": "4.00"},
        ],
        header_discount="10.00",
        rounding_adj="-0.01",
    )
    milo, soap = g["lines"]
    assert (milo["disc_alloc"], milo["line_subtotal"]) == ("6.0000", "54.0000")
    assert (soap["disc_alloc"], soap["line_subtotal"]) == ("4.0000", "36.0000")
    assert milo["unit_cost"] == "2.2500"  # 54 / 24 pieces
    assert (g["subtotal"], g["total"]) == ("90.0000", "89.9900")

    too_much = client.patch(
        f"{GRN}/{g['id']}",
        json={"version": g["version"], "header_discount": "200"},
        headers=store["h"],
    )
    assert too_much.status_code == 422


def test_tax_inclusive_price_is_split(store, client) -> None:
    taxes = _taxes(client, store)
    g = _grn(
        client,
        store,
        [
            {
                "item_id": store["soap"],
                "uom": "PCS",
                "qty": "1",
                "unit_price": "11.00",
                "price_incl_tax": True,
                "tax_code_id": taxes["ST10"],
            }
        ],
    )
    (ln,) = g["lines"]
    assert (ln["line_subtotal"], ln["line_tax"], ln["line_total"]) == (
        "10.0000",
        "1.0000",
        "11.0000",
    )
    assert ln["unit_cost"] == "10.0000"  # SST is not cost


def test_posted_grn_shows_paid_and_balance(store, client) -> None:
    h = store["h"]
    g = _grn(
        client, store, [{"item_id": store["soap"], "uom": "PCS", "qty": "10", "unit_price": "5"}]
    )
    g = client.post(f"{GRN}/{g['id']}/save", json={"version": g["version"]}, headers=h).json()
    g = client.post(f"{GRN}/{g['id']}/post", json={"version": g["version"]}, headers=h).json()
    got = client.get(f"{GRN}/{g['id']}", headers=h).json()
    assert (got["paid"], got["balance"]) == ("0", "50.0000")

    pv = client.post(
        "/api/v1/finance/supplier-payment",
        json={
            "doc_date": TODAY,
            "supplier_id": store["supplier"],
            "location_id": store["hq"],
            "amount": "20",
            "method": "TRANSFER",
            "lines": [{"line_type": "INVOICE", "ref_no": "INV-H1", "amount": "20"}],
        },
        headers=h,
    )
    assert pv.status_code == 201, pv.text
    pv = pv.json()
    pv = client.post(
        f"/api/v1/finance/supplier-payment/{pv['id']}/save",
        json={"version": pv["version"]},
        headers=h,
    ).json()
    r = client.post(
        f"/api/v1/finance/supplier-payment/{pv['id']}/post",
        json={"version": pv["version"]},
        headers=h,
    )
    assert r.status_code == 200, r.text
    got = client.get(f"{GRN}/{g['id']}", headers=h).json()
    assert (got["paid"], got["balance"]) == ("20.0000", "30.0000")
