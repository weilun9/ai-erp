import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from app.db.session import SessionLocal
from app.localization.my import TIMEZONE
from app.main import app
from app.models import PosSale
from tests.test_einvoice import SELLER, lhdn  # noqa: F401
from tests.test_pos import _return, _sale, cash, kopi, till  # noqa: F401
from tests.test_purchasing import _parallel
from tests.test_sales import store  # noqa: F401

pytestmark = pytest.mark.db
POS = "/api/v1/pos"
E = "/api/v1/einvoice"


def _last_month():
    first = datetime.now(ZoneInfo(TIMEZONE)).date().replace(day=1)
    day = first - timedelta(days=15)
    return day, datetime(day.year, day.month, day.day, 12, tzinfo=ZoneInfo(TIMEZONE))


def _backdate(*receipts):
    """Test setup: pretend these receipts were rung up last month."""
    _, when = _last_month()
    with SessionLocal() as db:
        db.execute(update(PosSale).where(PosSale.receipt_no.in_(receipts)).values(sold_at=when))
        db.commit()


@pytest.fixture
def month(till, client):  # noqa: F811
    """Last month at HQ: two KOPI sales (ST10, cash), one MILO carton (NA, card), and a
    refund of one KOPI from the first sale. Seller profile filled in for e-invoicing."""
    a = _sale(till, [kopi("2")], cash("10")).json()  # 6.66 + 0.67 -> 7.35 cash
    b = _sale(till, [kopi("1")], cash("5")).json()  # 3.33 + 0.33 -> 3.65 cash
    c = _sale(
        till,
        [{"item_id": till["milo"], "uom": "CTN", "qty": "1"}],
        [{"method": "CARD", "amount": "55"}],
    ).json()
    r = _return(till, a["receipt_no"], [{"line_no": 1, "qty": "1"}]).json()  # -3.66 -> -3.65
    _backdate(a["receipt_no"], b["receipt_no"], c["receipt_no"], r["receipt_no"])
    v = client.get("/api/v1/company", headers=till["h"]).json()["version"]
    assert (
        client.patch(
            "/api/v1/company", json={"version": v, **SELLER}, headers=till["h"]
        ).status_code
        == 200
    )
    day, _ = _last_month()
    return {**till, "period": day.isoformat(), "receipts": (a, b, c, r)}


def _create(client, m):
    return client.post(f"{POS}/consolidations", json={"period": m["period"]}, headers=m["h"])


def test_preview_groups_by_store_and_tax_net_of_refunds(month, client) -> None:
    pv = client.get(
        f"{POS}/consolidations/preview", params={"period": month["period"]}, headers=month["h"]
    ).json()
    assert pv["receipts"] == 3
    assert [(x["description"], x["tax_type"], x["subtotal"], x["tax"]) for x in pv["lines"]] == [
        ("HQ receipts POS000001-POS000002 (2 sales, 1 refunds)", "01", "6.66", "0.67"),
        ("HQ receipts POS000003 (1 sales)", "06", "55.00", "0.00"),
    ]
    # cash rounding: 7.33 -> 7.35 (+0.02), 3.66 -> 3.65 (-0.01), refund -3.66 -> -3.65 (+0.01)
    assert (pv["subtotal"], pv["tax_total"]) == ("61.66", "0.67")
    assert pv["rounding"] == "0.02" and pv["total"] == "62.35"
    assert pv["left_out"] == []


def test_month_must_be_over_and_is_taken_once(month, client) -> None:
    this_month = datetime.now(ZoneInfo(TIMEZONE)).date().isoformat()
    early = client.post(f"{POS}/consolidations", json={"period": this_month}, headers=month["h"])
    assert early.status_code == 422

    def go():
        c = TestClient(app)
        return lambda: (
            c.post(
                f"{POS}/consolidations", json={"period": month["period"]}, headers=month["h"]
            ).status_code
        )

    assert sorted(_parallel(go(), go())) == [201, 422]  # two clicks, one document
    [con] = client.get(f"{POS}/consolidations", headers=month["h"]).json()
    assert con["doc_no"] == "CON000001" and con["receipts"] == 3 and con["total"] == "62.3500"
    cashier = client.get(f"{POS}/consolidations", headers=month["ch"])
    assert cashier.status_code == 403


def test_ubl_document_follows_the_sdk_consolidated_sample(month, client) -> None:
    con = _create(client, month).json()
    pv = client.get(f"{E}/preview/CON/{con['id']}", headers=month["h"]).json()
    assert pv["problems"] == []
    inv = pv["document"]["Invoice"][0]
    assert inv["InvoiceTypeCode"][0]["_"] == "01"
    assert inv["InvoicePeriod"][0]["Description"][0]["_"] == "Monthly"
    buyer = inv["AccountingCustomerParty"][0]["Party"][0]
    ids = {x["ID"][0]["schemeID"]: x["ID"][0]["_"] for x in buyer["PartyIdentification"]}
    assert ids == {"TIN": "EI00000000010", "BRN": "NA", "SST": "NA", "TTX": "NA"}
    assert buyer["PostalAddress"][0]["CountrySubentityCode"][0]["_"] == "17"
    line = inv["InvoiceLine"][0]
    assert line["Item"][0]["CommodityClassification"][0]["ItemClassificationCode"][0] == {
        "_": "004",
        "listID": "CLASS",
    }
    assert line["InvoicedQuantity"][0] == {"_": 1, "unitCode": "C62"}
    total = inv["LegalMonetaryTotal"][0]
    assert total["PayableRoundingAmount"][0]["_"] == 0.02
    assert total["PayableAmount"][0]["_"] == 62.35


def test_submit_cancel_and_redo(month, client, lhdn) -> None:  # noqa: F811
    con = _create(client, month).json()
    pending = client.get(f"{E}/pending", headers=month["h"]).json()
    assert [(d["doc_type"], d["doc_no"]) for d in pending] == [("CON", "CON000001")]
    sent = client.post(
        f"{E}/submit", json={"doc_type": "CON", "doc_id": con["id"]}, headers=month["h"]
    ).json()
    assert sent["status"] == "SUBMITTED" and sent["type_code"] == "01"
    e = client.post(f"{E}/{sent['id']}/refresh", headers=month["h"]).json()
    assert e["status"] == "VALID"
    assert (
        client.post(f"{POS}/consolidations/{con['id']}/drop", headers=month["h"]).status_code == 409
    )  # live e-invoice

    r = client.post(f"{E}/{sent['id']}/cancel", json={"reason": "wrong month"}, headers=month["h"])
    assert r.status_code == 200
    again = client.get(f"{POS}/consolidations/{con['id']}", headers=month["h"]).json()
    assert again["status"] == "RELEASED" and again["receipts"] == 3  # history kept
    assert _create(client, month).json()["doc_no"] == "CON000002"  # receipts free again


def test_drop_frees_receipts_before_sending(month, client) -> None:
    con = _create(client, month).json()
    r = client.post(f"{POS}/consolidations/{con['id']}/drop", headers=month["h"]).json()
    assert r["status"] == "RELEASED"
    assert _create(client, month).status_code == 201


def test_refund_after_reporting_is_left_out_for_a_credit_note(month, client) -> None:
    _create(client, month)
    b = month["receipts"][1]
    late = _return(month, b["receipt_no"], [{"line_no": 1, "qty": "1"}])  # this month
    assert late.status_code == 201
    this_month = datetime.now(ZoneInfo(TIMEZONE)).date().isoformat()
    pv = client.get(
        f"{POS}/consolidations/preview", params={"period": this_month}, headers=month["h"]
    ).json()
    assert pv["receipts"] == 0 and pv["lines"] == []
    assert pv["left_out"] == [
        {
            "receipt_no": late.json()["receipt_no"],
            "refunds": b["receipt_no"],
            "why": "the original receipt was already reported in an earlier consolidated "
            "e-invoice: a credit note is needed (not automated yet)",
        }
    ]


def test_offline_receipts_uploaded_late_go_into_a_second_document(month, client) -> None:
    _create(client, month)
    late = _sale(
        month, [kopi()], cash("5"), client_uuid=str(uuid.uuid4())
    ).json()  # uploaded after the month was done
    _backdate(late["receipt_no"])
    second = _create(client, month).json()
    assert second["doc_no"] == "CON000002" and second["receipts"] == 1
