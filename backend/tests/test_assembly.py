"""Bills of materials, assembling and breaking up products."""

from datetime import date

import pytest

from tests.test_sales import _balance, store  # noqa: F401

pytestmark = pytest.mark.db
API = "/api/v1"
TODAY = date.today().isoformat()


def _hamper(client, s):
    r = client.post(
        f"{API}/items",
        json={
            "code": "HAMPER",
            "name": "Raya hamper",
            "base_uom": "PCS",
            "uoms": [{"uom": "PCS", "factor": "1", "selling_price": "30"}],
        },
        headers=s["h"],
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_assemble_uses_component_cost_and_void_puts_it_back(store, client) -> None:  # noqa: F811
    s = store  # MILO: 48 at HQ, average 1.50
    hamper = _hamper(client, s)
    bom = client.put(
        f"{API}/items/{hamper}/bom",
        json={"output_qty": "1", "lines": [{"item_id": s["milo"], "qty": "2"}]},
        headers=s["h"],
    )
    assert bom.status_code == 200, bom.text

    need = client.get(
        f"{API}/items/{hamper}/bom/needs",
        params={"qty": "5", "location_id": s["hq"]},
        headers=s["h"],
    ).json()
    assert [(x["qty"], x["on_hand"]) for x in need] == [("10.0000", "48.0000")]

    r = client.post(
        f"{API}/inventory/assemblies",
        json={"item_id": hamper, "qty": "5", "location_id": s["hq"], "doc_date": TODAY},
        headers=s["h"],
    )
    assert r.status_code == 201, r.text
    asm = r.json()
    assert asm["doc_no"] == "ASM000001" and asm["unit_cost"] == "3.0000"
    assert _balance(client, s, s["milo"]) == ("38.0000", "1.5000")
    assert _balance(client, s, hamper) == ("5.0000", "3.0000")

    # break one up again: the components come back at the hamper's cost
    r = client.post(
        f"{API}/inventory/assemblies",
        json={
            "item_id": hamper,
            "qty": "1",
            "location_id": s["hq"],
            "doc_date": TODAY,
            "direction": "DISASSEMBLE",
        },
        headers=s["h"],
    )
    assert r.status_code == 201, r.text
    assert r.json()["lines"][0]["unit_cost"] == "1.5000"
    assert _balance(client, s, s["milo"]) == ("40.0000", "1.5000")
    assert _balance(client, s, hamper) == ("4.0000", "3.0000")

    v = client.post(
        f"{API}/inventory/assemblies/{r.json()['id']}/void",
        json={"version": r.json()["version"], "reason": "test"},
        headers=s["h"],
    )
    assert v.status_code == 200 and v.json()["status"] == "VOIDED"
    assert _balance(client, s, hamper) == ("5.0000", "3.0000")

    # not enough components: nothing is made
    r = client.post(
        f"{API}/inventory/assemblies",
        json={"item_id": hamper, "qty": "100", "location_id": s["hq"], "doc_date": TODAY},
        headers=s["h"],
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "STOCK_INSUFFICIENT"

    # the books do not move: stock went from one item to another
    tb = client.get(
        f"{API}/accounting/trial-balance", params={"as_of": TODAY}, headers=s["h"]
    ).json()
    assert tb["total_debit"] == tb["total_credit"]


def test_bom_rules(store, client) -> None:  # noqa: F811
    s = store
    hamper = _hamper(client, s)
    own = client.put(
        f"{API}/items/{hamper}/bom",
        json={"lines": [{"item_id": hamper, "qty": "1"}]},
        headers=s["h"],
    )
    assert own.status_code == 422
    twice = client.put(
        f"{API}/items/{hamper}/bom",
        json={"lines": [{"item_id": s["milo"], "qty": "1"}, {"item_id": s["milo"], "qty": "2"}]},
        headers=s["h"],
    )
    assert twice.status_code == 400
    assert client.get(f"{API}/items/{hamper}/bom", headers=s["h"]).json() is None
    nobom = client.post(
        f"{API}/inventory/assemblies",
        json={"item_id": s["milo"], "qty": "1", "location_id": s["hq"], "doc_date": TODAY},
        headers=s["h"],
    )
    assert nobom.status_code == 422
