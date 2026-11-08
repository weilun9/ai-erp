import copy
import io
from datetime import date, timedelta
from decimal import Decimal

import httpx
import pytest
from PIL import Image

from app.core.config import get_settings
from app.services import vision
from tests.conftest import login
from tests.test_purchasing import _posted as pur_posted
from tests.test_purchasing import shop  # noqa: F401

TODAY = date.today().isoformat()
IN = "/api/v1/inbox"


def _png(color="white") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (60, 40), color).save(buf, format="PNG")
    return buf.getvalue()


def _pdf() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (200, 280), "white").save(buf, format="PDF")
    return buf.getvalue()


class Fake:
    """Stands in for the vision model: returns fixed data, records what it was sent."""

    name = "vision"

    def __init__(self, data, uncertain=(), fail=False):
        self.data, self.uncertain, self.fail, self.pages = data, list(uncertain), fail, None

    def extract(self, pages, hint):
        self.pages = pages
        if self.fail:
            raise vision.ExtractionError("model timed out")
        return vision.Extracted(
            data=copy.deepcopy(self.data),
            model="fake-vl",
            input_tokens=1200,
            output_tokens=300,
            seconds=1.5,
            uncertain=self.uncertain,
        )


@pytest.fixture(autouse=True)
def inbox_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "inbox_dir", str(tmp_path))
    yield
    vision.reset_extractor()


@pytest.fixture
def mighty(shop, client):  # noqa: F811
    """Mighty Bakery; BREAD has carton barcode 9556345310072 (24 pcs)."""
    r = client.patch(
        f"/api/v1/items/{shop['bread']}",
        json={
            "version": 1,
            "barcodes": [{"barcode": "9556345310072", "uom": "CTN"}],
        },
        headers=shop["h"],
    )
    assert r.status_code == 200, r.text
    return shop


def _upload(client, s, content=None, name="scan.png", **form):
    return client.post(
        IN,
        files={"file": (name, content or _png(), "application/octet-stream")},
        data={"location_id": s["hq"], **form},
        headers=s["h"],
    )


INVOICE = {
    "kind": "INVOICE",
    "supplier_name": "MIGHTY BAKERY SDN BHD",
    "doc_no": "IJB5001",
    "doc_date": date.today().strftime("%d/%m/%Y"),  # printed day/month/year
    "subtotal": "RM 68.92",
    "total": "68.92",
    "lines": [
        {
            "barcode": "9556345310072",
            "description": "White bread 400g",
            "qty": 2,
            "uom": "CTN",
            "unit_price": "19.46",
            "amount": "38.92",
        },
        {"description": "Cake", "qty": "10", "uom": "pcs", "unit_price": "3.00", "amount": "30.00"},
    ],
}


def _extract(client, s, file_id, hint=None):
    return client.post(f"{IN}/{file_id}/extract", json={"hint": hint}, headers=s["h"])


# --- upload -------------------------------------------------------------------------------


@pytest.mark.db
def test_upload_keeps_one_entry_per_file(mighty, client) -> None:
    content = _png()
    r = _upload(client, mighty, content)
    assert r.status_code == 201, r.text
    f = r.json()
    assert (f["status"], f["content_type"], f["already_uploaded"]) == ("NEW", "image/png", False)

    again = _upload(client, mighty, content, name="copy.png")
    assert again.status_code == 200
    assert again.json()["id"] == f["id"] and again.json()["already_uploaded"] is True

    got = client.get(f"{IN}/{f['id']}/content", headers=mighty["h"])
    assert got.status_code == 200 and got.content == content
    assert got.headers["content-type"] == "image/png"
    assert got.headers["x-content-type-options"] == "nosniff"


@pytest.mark.db
def test_only_documents_and_images_are_accepted(mighty, client) -> None:
    r = _upload(client, mighty, b"<script>alert(1)</script>", name="evil.png")
    assert r.status_code == 400


@pytest.mark.db
def test_cashier_cannot_upload(mighty, client) -> None:
    h = login(client, "cashier1")
    r = client.post(IN, files={"file": ("a.png", _png(), "image/png")}, headers=h)
    assert r.status_code == 403


# --- recognition and review ---------------------------------------------------------------


@pytest.mark.db
def test_invoice_is_read_checked_and_confirmed_into_a_draft_grn(mighty, client) -> None:
    fake = Fake(INVOICE)
    vision.set_extractor(fake)
    f = _upload(client, mighty).json()

    r = _extract(client, mighty, f["id"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "EXTRACTED" and body["doc_no"] == "IJB5001"
    assert body["extraction"]["input_tokens"] == 1200
    assert fake.pages[0][1] == "image/jpeg"
    rv = body["review"]
    assert rv["ready"] is True and rv["findings"] == []
    assert rv["data"]["supplier_id"] == mighty["supplier"]
    assert rv["data"]["doc_date"] == TODAY
    f_ = rv["fields"]
    assert f_["supplier"] == "CHECK"  # name only contained in the printed one
    assert (f_["lines.0.item"], f_["lines.0.uom"]) == ("TRUSTED", "TRUSTED")  # by barcode
    assert f_["lines.1.item"] == "CHECK"  # by description
    assert rv["data"]["lines"][1]["uom"] == "PCS"

    c = client.post(
        f"{IN}/{f['id']}/confirm",
        json={"kind": "INVOICE", "data": rv["data"]},
        headers=mighty["h"],
    )
    assert c.status_code == 200, c.text
    assert c.json()["doc_type"] == "GRN" and c.json()["file"]["status"] == "CONFIRMED"
    grn = client.get(f"/api/v1/purchase/grn/{c.json()['doc_id']}", headers=mighty["h"]).json()
    assert (grn["status"], grn["supplier_ref_no"], grn["total"]) == (
        "DRAFT",
        "IJB5001",
        "70.4200",
    )  # + 5% SST on cake
    assert [ln["uom"] for ln in grn["lines"]] == ["CTN", "PCS"]

    # The paper stays attached to the document as evidence
    files = client.get(IN, params={"doc_id": grn["id"]}, headers=mighty["h"]).json()
    assert [x["id"] for x in files["items"]] == [f["id"]]

    twice = client.post(
        f"{IN}/{f['id']}/confirm",
        json={"kind": "INVOICE", "data": rv["data"]},
        headers=mighty["h"],
    )
    assert twice.status_code == 409


@pytest.mark.db
def test_pdf_pages_are_rendered_for_the_model(mighty, client) -> None:
    fake = Fake(INVOICE)
    vision.set_extractor(fake)
    f = _upload(client, mighty, _pdf(), name="invoice.pdf").json()
    assert f["content_type"] == "application/pdf"
    assert _extract(client, mighty, f["id"]).status_code == 200
    assert len(fake.pages) == 1 and fake.pages[0][0][:3] == b"\xff\xd8\xff"


@pytest.mark.db
def test_checks_flag_what_does_not_add_up(mighty, client) -> None:
    vision.set_extractor(None)  # recognition off: the clerk types it in
    f = _upload(client, mighty).json()
    assert _extract(client, mighty, f["id"]).status_code == 422

    data = copy.deepcopy(INVOICE)
    data["doc_no"] = "INV-77"  # not Mighty's IJB format
    data["doc_date"] = (date.today() + timedelta(days=3)).isoformat()
    data["lines"][0]["amount"] = "40.00"  # 2 x 19.46 = 38.92
    data["total"] = data["subtotal"] = "100.00"
    rv = client.post(
        f"{IN}/{f['id']}/review", json={"kind": "INVOICE", "data": data}, headers=mighty["h"]
    ).json()
    codes = {x["code"] for x in rv["findings"]}
    assert codes == {"DOC_NO_FORMAT", "DATE_FUTURE", "LINE_AMOUNT", "TOTAL_MISMATCH"}
    assert rv["fields"]["doc_no"] == "CHECK" and rv["fields"]["total"] == "CHECK"
    assert rv["ready"] is True  # warnings only; a person decides


@pytest.mark.db
def test_missing_fields_block_confirm(mighty, client) -> None:
    f = _upload(client, mighty).json()
    data = copy.deepcopy(INVOICE)
    data["supplier_name"] = "Somebody Else Trading"
    data["lines"][1]["description"] = "zz unknown thing"
    rv = client.post(
        f"{IN}/{f['id']}/review", json={"kind": "INVOICE", "data": data}, headers=mighty["h"]
    ).json()
    assert rv["ready"] is False
    assert rv["fields"]["supplier"] == "MISSING" and rv["fields"]["lines.1.item"] == "MISSING"

    r = client.post(
        f"{IN}/{f['id']}/confirm", json={"kind": "INVOICE", "data": data}, headers=mighty["h"]
    )
    assert r.status_code == 422
    assert {"supplier", "lines.1.item"} <= set(r.json()["error"]["details"]["missing"])
    assert client.get(f"{IN}/{f['id']}", headers=mighty["h"]).json()["status"] == "NEW"


@pytest.mark.db
def test_already_recorded_invoice_is_blocked(mighty, client) -> None:
    pur_posted(
        client,
        mighty,
        "grn",
        [{"item_id": mighty["cake"], "uom": "PCS", "qty": "1", "unit_price": "3"}],
        ref="IJB5001",
    )
    vision.set_extractor(Fake(INVOICE))
    f = _upload(client, mighty).json()
    rv = _extract(client, mighty, f["id"]).json()["review"]
    assert rv["ready"] is False
    assert [x["code"] for x in rv["findings"]] == ["DUPLICATE_DOC"]


@pytest.mark.db
def test_model_doubts_are_marked_for_checking(mighty, client) -> None:
    vision.set_extractor(Fake(INVOICE, uncertain=["lines.0.qty", "doc_date"]))
    f = _upload(client, mighty).json()
    fields = _extract(client, mighty, f["id"]).json()["review"]["fields"]
    assert fields["lines.0.qty"] == "CHECK" and fields["doc_date"] == "CHECK"


@pytest.mark.db
def test_failed_recognition_is_recorded(mighty, client) -> None:
    vision.set_extractor(Fake(INVOICE, fail=True))
    f = _upload(client, mighty).json()
    r = _extract(client, mighty, f["id"])
    assert r.status_code == 422 and "timed out" in r.json()["error"]["details"]["error"]
    ex = client.get(f"{IN}/{f['id']}", headers=mighty["h"]).json()["extraction"]
    assert ex["status"] == "FAILED"


# --- other kinds --------------------------------------------------------------------------


@pytest.mark.db
def test_payment_advice_becomes_a_draft_payment(mighty, client) -> None:
    pur_posted(
        client,
        mighty,
        "grn",
        [{"item_id": mighty["cake"], "uom": "PCS", "qty": "10", "unit_price": "10"}],
        ref="IJB6001",
    )
    vision.set_extractor(
        Fake(
            {
                "kind": "PAYMENT",
                "supplier_name": "Mighty Bakery",
                "doc_no": "PA-9",
                "doc_date": TODAY,
                "total": "100.00",
                "payment_lines": [
                    # 10 x 10.00 + 5% SST
                    {"line_type": "INVOICE", "ref_no": "IJB6001", "amount": "105.00"},
                    {"line_type": "FEE", "fee_type": "transport", "amount": "5.00"},
                ],
            }
        )
    )
    f = _upload(client, mighty).json()
    rv = _extract(client, mighty, f["id"]).json()["review"]
    assert rv["ready"] is True and rv["fields"]["supplier"] == "TRUSTED"
    assert rv["fields"]["payment_lines.1.fee_type"] == "CHECK"  # unknown type -> OTHER
    c = client.post(
        f"{IN}/{f['id']}/confirm", json={"kind": "PAYMENT", "data": rv["data"]}, headers=mighty["h"]
    ).json()
    pv = client.get(f"/api/v1/finance/supplier-payment/{c['doc_id']}", headers=mighty["h"]).json()
    assert pv["amount"] == "100.0000" and pv["lines"][0]["match_status"] == "MATCHED"


@pytest.mark.db
def test_vague_return_note_is_recorded_for_later(mighty, client) -> None:
    f = _upload(client, mighty).json()
    data = {
        "supplier_name": "Mighty Bakery",
        "doc_date": TODAY,
        "lines": [{"description": "roti 3 bungkus", "qty": 3}],
    }
    c = client.post(
        f"{IN}/{f['id']}/confirm", json={"kind": "RETURN_NOTE", "data": data}, headers=mighty["h"]
    )
    assert c.status_code == 200, c.text
    cn = client.get(
        f"/api/v1/purchase/supplier-cn/{c.json()['doc_id']}", headers=mighty["h"]
    ).json()
    assert cn["completeness"] == "RECORDED" and cn["lines"] == []
    assert "roti 3 bungkus" in cn["remark"]


@pytest.mark.db
def test_bank_slip_is_kept_as_a_bank_record(mighty, client) -> None:
    f = _upload(client, mighty).json()
    data = {
        "bank": {
            "date": TODAY,
            "amount": "1,250.00",
            "direction": "OUT",
            "counterparty": "MIGHTY BAKERY",
            "reference": "IBG 7781",
        }
    }
    c = client.post(
        f"{IN}/{f['id']}/confirm", json={"kind": "BANK", "data": data}, headers=mighty["h"]
    )
    assert c.status_code == 200, c.text
    assert c.json()["doc_type"] == "BANK"


@pytest.mark.db
def test_reject(mighty, client) -> None:
    f = _upload(client, mighty).json()
    r = client.post(f"{IN}/{f['id']}/reject", json={"reason": "not ours"}, headers=mighty["h"])
    assert r.json()["status"] == "REJECTED"
    again = client.post(
        f"{IN}/{f['id']}/confirm", json={"kind": "BANK", "data": {}}, headers=mighty["h"]
    )
    assert again.status_code in (409, 422)


# --- provider and cleaning (no database) --------------------------------------------------


def test_openai_compatible_provider_reads_fenced_json(monkeypatch) -> None:
    seen = {}

    def fake_post(url, json, headers, timeout):
        seen.update(url=url, auth=headers.get("Authorization"), body=json)
        reply = {
            "choices": [
                {"message": {"content": '```json\n{"kind": "CN", "uncertain": ["total"]}\n```'}}
            ],
            "usage": {"prompt_tokens": 900, "completion_tokens": 40},
        }
        return httpx.Response(200, json=reply, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    ex = vision.OpenAICompatible("http://gw.local/v1/", "k-1", "qwen-vl", 30)
    got = ex.extract([(b"\xff\xd8\xffdata", "image/jpeg")], "CN")
    assert seen["url"] == "http://gw.local/v1/chat/completions" and seen["auth"] == "Bearer k-1"
    image = seen["body"]["messages"][1]["content"][1]["image_url"]["url"]
    assert image.startswith("data:image/jpeg;base64,")
    assert got.data == {"kind": "CN"} and got.uncertain == ["total"]
    assert (got.input_tokens, got.output_tokens) == (900, 40)


def test_cleaning_numbers_and_malaysian_dates() -> None:
    assert vision.num("RM 1,234.50") == Decimal("1234.50")
    assert vision.num("-") is None and vision.num(True) is None
    assert vision.day("31/12/2026") == date(2026, 12, 31)
    assert vision.day("05-01-26") == date(2026, 1, 5)
    assert vision.day("2026-11-08") == date(2026, 11, 8)
    assert vision.day("31/02/2026") is None
