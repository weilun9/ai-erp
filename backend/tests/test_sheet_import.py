"""The AI butler's Excel templates: hand one out, fill it, attach it, agree."""

import io
from datetime import date

import pytest
from openpyxl import load_workbook

from tests.test_ai import AI, _chat, _ok, model  # noqa: F401
from tests.test_sales import store  # noqa: F401

pytestmark = pytest.mark.db
API = "/api/v1"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _template(client, s, m):
    """The butler offers a GRN template with what it knows; download it."""
    args = {
        "doc_type": "grn",
        "party": "NESTLE",
        "ref_no": "AR11",
        "lines": [{"code": "1901", "name": "Marie biscuit"}, {"code": "MILO"}],
    }
    m(("tool", "excel_template", args), ("say", "here"))
    _, conv = _chat(client, s["h"], "INSERT IT")
    link = None
    for msg in client.get(f"{AI}/conversations/{conv}/messages", headers=s["h"]).json():
        if msg["role"] == "tool" and "Download the Excel template" in (msg["content"] or ""):
            link = msg["content"].split("](/download")[1].split(")")[0]
    assert link and link.startswith("/ai/templates/grn?p=")
    r = client.get(f"{API}{link}", headers=s["h"])
    assert r.status_code == 200 and r.headers["content-type"] == XLSX
    return conv, load_workbook(io.BytesIO(r.content))


def _attach(client, s, conv, wb):
    out = io.BytesIO()
    wb.save(out)
    r = client.post(
        f"{AI}/conversations/{conv}/sheet",
        files={"file": ("filled.xlsx", out.getvalue(), XLSX)},
        headers=s["h"],
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_template_filled_and_attached_becomes_new_items_and_a_draft(store, client, model) -> None:  # noqa: F811
    s = store
    conv, wb = _template(client, s, model)
    doc = {r[0].value: r[1] for r in wb["Document"].iter_rows(min_row=2)}
    assert doc["Supplier code *"].value == "NESTLE" and doc["Supplier invoice no"].value == "AR11"
    rows = list(wb["Lines"].iter_rows(min_row=2, values_only=True))
    assert [r[0] for r in rows] == ["1901", "MILO"] and rows[0][1] == "Marie biscuit"

    wb["Document"]["B6"] = "HQ"  # store code (this company has several stores)
    ws = wb["Lines"]  # the user fills the rest
    ws["C2"], ws["D2"], ws["E2"], ws["F2"] = "PCS", 24, 1.10, 1.80
    ws["D3"], ws["E3"] = 2, 1.20
    turn = _attach(client, s, conv, wb)
    assert turn["pending_actions"], turn["messages"][-1]["content"]
    [card] = turn["pending_actions"]
    assert card["summary"]["action"].startswith("From your Excel")
    assert "1901 Marie biscuit (PCS)" in card["summary"]["checks"][0]
    assert turn["messages"][0]["content"] == "📎 filled.xlsx"

    done = client.post(f"{AI}/actions/{card['id']}/confirm", headers=s["h"]).json()
    assert done["status"] == "CONFIRMED", done
    assert done["result"]["doc_type"] == "grn" and done["result"]["items_created"] == ["1901"]
    grn = client.get(f"{API}/purchase/grn/{done['result']['doc_id']}", headers=s["h"]).json()
    assert grn["status"] == "DRAFT" and grn["supplier_ref_no"] == "AR11"
    assert sorted(ln["qty"] for ln in grn["lines"]) == ["2.0000", "24.0000"]
    item = client.get(f"{API}/items", params={"q": "1901"}, headers=s["h"]).json()["items"][0]
    assert item["name"] == "Marie biscuit" and item["uoms"][0]["selling_price"] == "1.8000"


def test_a_half_filled_template_says_what_to_fix(store, client, model) -> None:  # noqa: F811
    s = store
    conv, wb = _template(client, s, model)
    wb["Lines"]["D2"] = 5  # 1901: qty but no price
    wb["Lines"]["A4"], wb["Lines"]["D4"], wb["Lines"]["E4"] = "NOPE", 1, 1  # unknown, no name
    turn = _attach(client, s, conv, wb)
    assert turn["pending_actions"] == []
    text = turn["messages"][-1]["content"]
    assert "row 2 (1901): a goods received note needs the unit price" in text
    assert "NOPE is not in the ERP" in text

    bad = client.post(
        f"{AI}/conversations/{conv}/sheet",
        files={"file": ("x.xlsx", b"not excel", XLSX)},
        headers=s["h"],
    )
    assert bad.status_code == 400
    assert date.today()  # keeps the import used
