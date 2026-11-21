"""Accounting export: Excel workbook and CSV of the journals."""

import io

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.main import app
from tests import test_sales
from tests.conftest import login
from tests.test_accounting import BIZ
from tests.test_sales import TODAY, _posted, milo

pytestmark = pytest.mark.db
store = test_sales.store
URL = "/api/v1/accounting/export"


def test_workbook_and_csv_carry_every_journal_line(store, client) -> None:
    h = store["h"]
    inv = _posted(client, store, "sales-invoice", [milo(store, "4", unit_price="2.50")])
    params = {"date_from": TODAY, "date_to": BIZ}

    r = client.get(URL, params=params, headers=h)
    assert r.status_code == 200, r.text
    assert "spreadsheetml" in r.headers["content-type"]
    assert "attachment" in r.headers["content-disposition"]
    wb = load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames == [
        "About",
        "Journals",
        "Trial balance",
        "Profit and loss",
        "Balance sheet",
        "Chart of accounts",
    ]
    journals = list(wb["Journals"].iter_rows(min_row=2, values_only=True))
    assert any(row[3] == inv["doc_no"] for row in journals)  # source no
    assert round(sum(row[8] for row in journals), 2) == round(sum(row[9] for row in journals), 2)
    total = list(wb["Trial balance"].iter_rows(values_only=True))[-1]
    assert total[1] == "Total" and total[3] == total[4]

    c = client.get(URL, params={**params, "format": "csv"}, headers=h)
    assert c.status_code == 200
    text = c.content.decode("utf-8")
    assert text.startswith("﻿Date,Journal no,Source")
    assert inv["doc_no"] in text

    cashier = TestClient(app)
    assert cashier.get(URL, params=params, headers=login(cashier, "cashier1")).status_code == 403
