"""Batch / expiry and serial numbers: receipts record them, sales take earliest expiry first,
transfers carry them along, voids put them back, and every lot can be traced."""

from datetime import date, timedelta

import pytest

from tests.test_sales import TODAY, store  # noqa: F401

pytestmark = pytest.mark.db
API = "/api/v1"
SOON = (date.today() + timedelta(days=10)).isoformat()
LATER = (date.today() + timedelta(days=100)).isoformat()


def _item(client, s, code, **flags):
    r = client.post(
        f"{API}/items",
        json={
            "code": code,
            "name": code,
            "base_uom": "PCS",
            "uoms": [{"uom": "PCS", "factor": "1", "selling_price": "10.00"}],
            **flags,
        },
        headers=s["h"],
    )
    assert r.status_code == 201, r.text
    assert r.json()["track_batch"] == flags.get("track_batch", False)
    return r.json()["id"]


def _save(client, s, url, doc):
    r = client.post(f"{url}/{doc['id']}/save", json={"version": doc["version"]}, headers=s["h"])
    assert r.status_code == 200, r.text
    return r.json()


def _post(client, s, url, doc):
    return client.post(f"{url}/{doc['id']}/post", json={"version": doc["version"]}, headers=s["h"])


def _grn(client, s, item, qty, lots, ref):
    url = f"{API}/purchase/grn"
    line = {"item_id": item, "uom": "PCS", "qty": qty, "unit_price": "4.00"}
    if lots is not None:
        line["lots"] = lots
    body = {
        "doc_date": TODAY,
        "supplier_id": s["supplier"],
        "location_id": s["hq"],
        "supplier_ref_no": ref,
        "lines": [line],
    }
    r = client.post(url, json=body, headers=s["h"])
    assert r.status_code == 201, r.text
    return url, _save(client, s, url, r.json())


def _invoice(client, s, item, qty, lots=None):
    url = f"{API}/sales/sales-invoice"
    line = {"item_id": item, "uom": "PCS", "qty": qty}
    if lots:
        line["lots"] = lots
    body = {
        "doc_date": TODAY,
        "customer_id": s["customer"],
        "location_id": s["hq"],
        "lines": [line],
    }
    r = client.post(url, json=body, headers=s["h"])
    assert r.status_code == 201, r.text
    return url, _save(client, s, url, r.json())


def _lots(client, s, item, **params):
    rows = client.get(f"{API}/lots", params={"item_id": item, **params}, headers=s["h"]).json()
    return {(x["lot_no"], x["location_id"]): x["qty"] for x in rows["items"]}


def test_batches_go_out_earliest_expiry_first_and_come_back_on_void(store, client) -> None:  # noqa: F811
    s = store
    pan = _item(client, s, "PANADOL", track_batch=True)

    url, grn = _grn(client, s, pan, "20", None, "B-0")
    r = _post(client, s, url, grn)
    assert r.status_code == 422 and r.json()["error"]["code"] == "LOT_INVALID"

    lots = [
        {"lot_no": "B2", "expiry_date": LATER, "qty": "10"},
        {"lot_no": "B1", "expiry_date": SOON, "qty": "10"},
    ]
    url, grn = _grn(client, s, pan, "20", lots, "B-1")
    assert _post(client, s, url, grn).status_code == 200
    assert _lots(client, s, pan) == {("B1", s["hq"]): "10.0000", ("B2", s["hq"]): "10.0000"}

    expiring = client.get(
        f"{API}/lots", params={"status": "expiring", "days": 30}, headers=s["h"]
    ).json()["items"]
    assert [(x["lot_no"], x["days_left"]) for x in expiring] == [("B1", 10)]

    url, inv = _invoice(client, s, pan, "12")  # no lots given: earliest expiry first
    posted = _post(client, s, url, inv)
    assert posted.status_code == 200, posted.text
    inv = posted.json()
    assert _lots(client, s, pan) == {("B2", s["hq"]): "8.0000"}
    moved = client.get(f"{API}/lots/doc/INV/{inv['id']}", headers=s["h"]).json()
    took = {x["lot_no"]: x["qty"] for x in moved[inv["lines"][0]["id"]]}
    assert took == {"B1": "10.0000", "B2": "2.0000"}

    r = client.post(
        f"{url}/{inv['id']}/void",
        json={"version": inv["version"], "reason": "test"},
        headers=s["h"],
    )
    assert r.status_code == 200, r.text
    assert _lots(client, s, pan) == {("B1", s["hq"]): "10.0000", ("B2", s["hq"]): "10.0000"}


def test_named_batch_must_have_stock_and_transfer_keeps_the_batches(store, world, client) -> None:  # noqa: F811
    s = store
    pan = _item(client, s, "PANADOL2", track_batch=True)
    url, grn = _grn(client, s, pan, "5", [{"lot_no": "X1", "expiry_date": SOON, "qty": "5"}], "X")
    assert _post(client, s, url, grn).status_code == 200

    url, inv = _invoice(client, s, pan, "6", [{"lot_no": "X1", "qty": "6"}])
    r = _post(client, s, url, inv)
    assert r.status_code == 422 and r.json()["error"]["code"] == "LOT_INVALID"

    # a batch cannot change its expiry date later
    url2, grn2 = _grn(
        client, s, pan, "1", [{"lot_no": "X1", "expiry_date": LATER, "qty": "1"}], "Y"
    )
    assert _post(client, s, url2, grn2).json()["error"]["code"] == "LOT_INVALID"

    s2 = str(world.s2_id)
    tr_url = f"{API}/inventory/transfer"
    body = {
        "doc_date": TODAY,
        "location_id": s["hq"],
        "to_location_id": s2,
        "lines": [{"item_id": pan, "uom": "PCS", "qty": "3"}],
    }
    tr = _save(client, s, tr_url, client.post(tr_url, json=body, headers=s["h"]).json())
    assert _post(client, s, tr_url, tr).status_code == 200
    assert _lots(client, s, pan) == {("X1", s["hq"]): "2.0000", ("X1", s2): "3.0000"}

    lot_id = client.get(f"{API}/lots", params={"item_id": pan}, headers=s["h"]).json()["items"][0][
        "lot_id"
    ]
    trace = client.get(f"{API}/lots/{lot_id}/trace", headers=s["h"]).json()
    assert [(m["doc_type"], m["qty"]) for m in trace["moves"]] == [
        ("GRN", "5.0000"),
        ("TRF", "-3.0000"),
        ("TRF", "3.0000"),
    ]


def test_serial_numbers_are_one_piece_each_and_traceable(store, client) -> None:  # noqa: F811
    s = store
    phone = _item(client, s, "PHONE", track_serial=True)

    url, grn = _grn(client, s, phone, "2", [{"lot_no": "SN1", "qty": "2"}], "P0")
    assert _post(client, s, url, grn).json()["error"]["code"] == "LOT_INVALID"  # 2 pieces, 1 no.

    lots = [{"lot_no": "SN1", "qty": "1"}, {"lot_no": "SN2", "qty": "1"}]
    url, grn = _grn(client, s, phone, "2", lots, "P1")
    assert _post(client, s, url, grn).status_code == 200

    url, again = _grn(client, s, phone, "1", [{"lot_no": "SN1", "qty": "1"}], "P2")
    r = _post(client, s, url, again)
    assert (
        r.json()["error"]["code"] == "LOT_INVALID"
        and "already in stock" in r.json()["error"]["message"]
    )

    url, inv = _invoice(client, s, phone, "1", [{"lot_no": "SN2", "qty": "1"}])
    inv = _post(client, s, url, inv).json()
    assert _lots(client, s, phone) == {("SN1", s["hq"]): "1.0000"}

    found = client.get(f"{API}/lots", params={"q": "SN2", "status": "all"}, headers=s["h"]).json()
    trace = client.get(f"{API}/lots/{found['items'][0]['lot_id']}/trace", headers=s["h"]).json()
    assert trace["kind"] == "SERIAL"
    assert [m["doc_no"] for m in trace["moves"]][-1] == inv["doc_no"]


def test_untracked_items_are_unaffected(store, client) -> None:  # noqa: F811
    s = store
    url, inv = _invoice(client, s, s["milo"], "5")
    assert _post(client, s, url, inv).status_code == 200
    assert _lots(client, s, s["milo"], status="all") == {}


def test_switching_tracking_on_keeps_the_stock_on_hand_as_unnumbered(store, client) -> None:  # noqa: F811
    s = store
    item = client.get(f"{API}/items/{s['milo']}", headers=s["h"]).json()
    r = client.patch(
        f"{API}/items/{s['milo']}",
        json={"version": item["version"], "track_batch": True},
        headers=s["h"],
    )
    assert r.status_code == 200, r.text
    assert _lots(client, s, s["milo"]) == {("-", s["hq"]): "48.0000"}
    url, inv = _invoice(client, s, s["milo"], "5")  # no numbers yet: from the "-" lot
    assert _post(client, s, url, inv).status_code == 200
    assert _lots(client, s, s["milo"]) == {("-", s["hq"]): "43.0000"}
