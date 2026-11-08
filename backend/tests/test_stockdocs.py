from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.conftest import login
from tests.test_purchasing import _parallel
from tests.test_sales import _balance, milo, store  # noqa: F401
from tests.test_sales import _posted as sal_posted

pytestmark = pytest.mark.db

TODAY = date.today().isoformat()
INV = "/api/v1/inventory"


def _doc(client, s, kind, lines, **over):
    body = {"doc_date": TODAY, "location_id": s["hq"], "lines": lines, **over}
    return client.post(f"{INV}/{kind}", json=body, headers=s["h"])


def _act(client, s, kind, doc, action, **extra):
    return client.post(
        f"{INV}/{kind}/{doc['id']}/{action}",
        json={"version": doc["version"], **extra},
        headers=s["h"],
    )


def _posted(client, s, kind, lines, **over):
    d = _doc(client, s, kind, lines, **over)
    assert d.status_code == 201, d.text
    d = _act(client, s, kind, d.json(), "save")
    assert d.status_code == 200, d.text
    r = _act(client, s, kind, d.json(), "post")
    assert r.status_code == 200, r.text
    return r.json()


def _bal_at(client, s, loc):
    rows = client.get(
        "/api/v1/stock/balance",
        params={"item_id": s["milo"], "location_id": loc},
        headers=s["h"],
    ).json()["items"]
    return (rows[0]["qty"], rows[0]["avg_cost"]) if rows else None


@pytest.fixture
def s(store, world):  # noqa: F811
    """48 MILO at HQ, average 1.50; S2 empty."""
    return {**store, "s2": str(world.s2_id)}


# --- adjustment ---------------------------------------------------------------------------


def test_adjustment_adds_at_given_cost_and_removes_at_average(s, client) -> None:
    d = _posted(
        client,
        s,
        "adjustment",
        [
            milo(s, "1", "CTN", unit_cost="72.00"),  # 24 found, 3.00 each
            milo(s, "-2"),
        ],
        reason="FOUND",
    )
    assert d["doc_no"] == "ADJ000001"
    # (48 x 1.50 + 24 x 3.00) / 72 = 2.00; then 2 out at 2.00
    assert _balance(client, s, s["milo"]) == ("70.0000", "2.0000")
    assert [ln["unit_cost"] for ln in d["lines"]] == ["3.0000", "2.0000"]
    assert d["cost_total"] == "68.0000"  # +72.00 - 4.00

    _act(client, s, "adjustment", d, "void", reason="counted twice")
    assert _balance(client, s, s["milo"]) == ("48.0000", "1.5000")


def test_adjustment_needs_a_reason(s, client) -> None:
    d = _doc(client, s, "adjustment", [milo(s, "1")]).json()
    r = _act(client, s, "adjustment", d, "save")
    assert r.status_code == 422 and "FOUND" in r.json()["error"]["details"]["allowed"]


# --- write-off ----------------------------------------------------------------------------


def test_write_off_removes_at_average_cost(s, client) -> None:
    d = _posted(client, s, "write-off", [milo(s, "5", reason="EXPIRED")])
    assert d["doc_no"] == "WO000001" and d["cost_total"] == "7.5000"
    assert _balance(client, s, s["milo"]) == ("43.0000", "1.5000")


def test_write_off_reason_must_be_known(s, client) -> None:
    d = _doc(client, s, "write-off", [milo(s, "1")], reason="EATEN").json()
    assert _act(client, s, "write-off", d, "save").status_code == 422


# --- transfer -----------------------------------------------------------------------------


def test_transfer_moves_stock_at_its_cost(s, client) -> None:
    d = _posted(client, s, "transfer", [milo(s, "1", "CTN")], to_location_id=s["s2"])
    assert d["doc_no"] == "TRF000001" and d["cost_total"] == "36.0000"
    assert _bal_at(client, s, s["hq"]) == ("24.0000", "1.5000")
    assert _bal_at(client, s, s["s2"]) == ("24.0000", "1.5000")

    # The receiving store (clerk1 only sees S2) can see it
    h = login(client, "clerk1")
    seen = client.get(f"{INV}/transfer", headers=h).json()
    assert [x["doc_no"] for x in seen["items"]] == ["TRF000001"]

    _act(client, s, "transfer", d, "void", reason="wrong store")
    assert _bal_at(client, s, s["hq"]) == ("48.0000", "1.5000")
    assert _bal_at(client, s, s["s2"])[0] == "0.0000"


def test_transfer_to_the_same_store_is_refused(s, client) -> None:
    d = _doc(client, s, "transfer", [milo(s, "1")], to_location_id=s["hq"]).json()
    assert _act(client, s, "transfer", d, "save").status_code == 422


def test_opposite_transfers_at_once_do_not_deadlock(s, client) -> None:
    _posted(client, s, "transfer", [milo(s, "20")], to_location_id=s["s2"])
    a = _act(
        client,
        s,
        "transfer",
        _doc(client, s, "transfer", [milo(s, "5")], to_location_id=s["s2"]).json(),
        "save",
    ).json()
    b = _act(
        client,
        s,
        "transfer",
        _doc(
            client, s, "transfer", [milo(s, "3")], location_id=s["s2"], to_location_id=s["hq"]
        ).json(),
        "save",
    ).json()

    def post(d):
        c = TestClient(app)
        return lambda: (
            c.post(
                f"{INV}/transfer/{d['id']}/post", json={"version": d["version"]}, headers=s["h"]
            ).status_code
        )

    assert _parallel(post(a), post(b)) == [200, 200]
    assert _bal_at(client, s, s["hq"])[0] == "26.0000"  # 48 - 20 - 5 + 3
    assert _bal_at(client, s, s["s2"])[0] == "22.0000"


# --- stocktake ----------------------------------------------------------------------------


def test_stocktake_books_the_difference(s, client) -> None:
    # Counted 1 carton + 20 loose = 44 against 48
    d = _doc(client, s, "stocktake", [milo(s, "1", "CTN"), milo(s, "20")]).json()
    saved = _act(client, s, "stocktake", d, "save").json()
    w = saved["warnings"][0]
    assert w["code"] == "STOCKTAKE_DIFF" and w["items"][0]["counted_qty"] == "44.0000"

    t = _act(client, s, "stocktake", saved, "post").json()
    first, second = t["lines"]
    assert (first["system_qty"], first["diff_qty"]) == ("48.0000", "-4.0000")
    assert second["diff_qty"] is None  # the item's difference sits on its first line
    assert t["cost_total"] == "-6.0000"
    assert _balance(client, s, s["milo"]) == ("44.0000", "1.5000")


def test_sales_after_the_count_are_kept(s, client) -> None:
    d = _doc(client, s, "stocktake", [milo(s, "45")]).json()
    saved = _act(client, s, "stocktake", d, "save").json()  # counted now: 45 vs 48
    sal_posted(client, s, "delivery-order", [milo(s, "10")])  # 10 leave after the count
    t = _act(client, s, "stocktake", saved, "post").json()
    assert t["lines"][0]["diff_qty"] == "-3.0000"
    assert _balance(client, s, s["milo"])[0] == "35.0000"  # 48 - 10 - 3


def test_full_count_zeroes_items_not_counted(s, client) -> None:
    soap_in = _posted(
        client,
        s,
        "adjustment",
        [{"item_id": s["soap"], "uom": "PCS", "qty": "6", "unit_cost": "1"}],
        reason="OPENING",
    )
    assert soap_in["status"] == "POSTED"
    t = _posted(client, s, "stocktake", [milo(s, "48")], zero_uncounted=True)
    soap = [ln for ln in t["lines"] if ln["item_id"] == s["soap"]][0]
    assert (soap["qty"], soap["system_qty"], soap["diff_qty"]) == ("0.0000", "6.0000", "-6.0000")
    assert _balance(client, s, s["soap"])[0] == "0.0000"
    assert _balance(client, s, s["milo"])[0] == "48.0000"


# --- access -------------------------------------------------------------------------------


def test_cost_is_hidden_and_cashier_cannot_create(s, client) -> None:
    d = _posted(client, s, "write-off", [milo(s, "1")], reason="DAMAGED")
    h = login(client, "cashier1")  # sees stock, no cost permission, no stock documents
    r = client.post(
        f"{INV}/write-off",
        json={"doc_date": TODAY, "location_id": s["hq"], "lines": [milo(s, "1")]},
        headers=h,
    )
    assert r.status_code == 403
    assert d["cost_total"] == "1.5000"
