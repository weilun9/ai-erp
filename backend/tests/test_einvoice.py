import base64
import hashlib
import json
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from sqlalchemy import update

from app.db.session import SessionLocal
from app.models import Einvoice
from app.services import myinvois
from tests.conftest import login
from tests.test_sales import _do as sal_do
from tests.test_sales import _posted as sal_posted
from tests.test_sales import milo, store  # noqa: F401

E = "/api/v1/einvoice"


class FakeLhdn:
    """A stand-in MyInvois: checks the envelope like LHDN does and answers like it."""

    def __init__(self):
        self.logins = 0
        self.docs: dict[str, dict] = {}
        self.reject_next = False
        self.down = False
        self.verdict = "Valid"
        self.n = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("no route to host", request=request)
        path = request.url.path
        if path == "/connect/token":
            form = dict(x.split("=") for x in request.content.decode().split("&"))
            assert form["grant_type"] == "client_credentials" and form["scope"] == "InvoicingAPI"
            self.logins += 1
            return httpx.Response(200, json={"access_token": f"t{self.logins}", "expires_in": 3600})
        assert request.headers["authorization"].startswith("Bearer t")
        if path == "/api/v1.0/documentsubmissions/":
            body = json.loads(request.content)
            accepted, rejected = [], []
            for d in body["documents"]:
                raw = base64.b64decode(d["document"])
                assert hashlib.sha256(raw).hexdigest() == d["documentHash"]
                doc = json.loads(raw)
                if self.reject_next:
                    rejected.append(
                        {
                            "invoiceCodeNumber": d["codeNumber"],
                            "error": {"code": "BadArgument", "message": "Buyer TIN invalid"},
                        }
                    )
                    continue
                self.n += 1
                uid = f"F9D425P6DS7D8IU{self.n:011d}"
                self.docs[uid] = doc
                accepted.append({"uuid": uid, "invoiceCodeNumber": d["codeNumber"]})
            self.reject_next = False
            return httpx.Response(
                202,
                json={
                    "submissionUID": "HJSD135P2S7D8IU",
                    "acceptedDocuments": accepted,
                    "rejectedDocuments": rejected,
                },
            )
        if path.endswith("/details"):
            uid = path.split("/")[-2]
            if self.verdict == "Valid":
                return httpx.Response(
                    200,
                    json={
                        "uuid": uid,
                        "status": "Valid",
                        "longId": f"LIJAF97HJJKH{uid[-6:]}",
                        "dateTimeValidated": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    },
                )
            return httpx.Response(
                200,
                json={
                    "uuid": uid,
                    "status": "Invalid",
                    "validationResults": {
                        "status": "Invalid",
                        "validationSteps": [
                            {"name": "Step01-Structure", "status": "Valid"},
                            {
                                "name": "Step03-Taxpayer",
                                "status": "Invalid",
                                "error": {"error": "Supplier TIN not registered"},
                            },
                        ],
                    },
                },
            )
        if path.endswith("/state"):
            body = json.loads(request.content)
            assert body["status"] == "cancelled" and body["reason"]
            return httpx.Response(200, json={"uuid": path.split("/")[-2], "status": "Cancelled"})
        return httpx.Response(404)


@pytest.fixture
def lhdn():
    fake = FakeLhdn()
    myinvois.set_client(
        myinvois.MyInvoisClient(
            "sandbox", "client-1", "secret-1", transport=httpx.MockTransport(fake)
        )
    )
    yield fake
    myinvois.reset_client()


SELLER = {
    "tin": "C1234567890",
    "id_type": "BRN",
    "id_no": "202001012345",
    "sst_no": "W10-1808-32000001",
    "msic_code": "47190",
    "business_activity": "Retail sale in non-specialised stores",
    "address1": "No. 1, Jalan Bakery",
    "city": "Johor Bahru",
    "postcode": "80000",
    "state_code": "01",
    "phone": "+6071234567",
    "email": "accounts@example.com",
    "einvoice_enabled": True,
}


@pytest.fixture
def ready(store, client):  # noqa: F811
    h = store["h"]
    v = client.get("/api/v1/company", headers=h).json()["version"]
    r = client.patch("/api/v1/company", json={"version": v, **SELLER}, headers=h)
    assert r.status_code == 200, r.text
    r = client.patch(
        f"/api/v1/customers/{store['customer']}",
        json={
            "version": 1,
            "tin": "C9876543210",
            "id_type": "BRN",
            "id_no": "201901000001",
            "address1": "Lot 5, Jalan Pasar",
            "city": "Kuala Lumpur",
            "postcode": "50000",
            "state_code": "14",
            "phone": "+60312345678",
        },
        headers=h,
    )
    assert r.status_code == 200, r.text
    return store


def _invoice(client, s, qty="10", **extra):
    return sal_posted(client, s, "sales-invoice", [milo(s, qty, **extra)])


def _submit(client, s, doc_type, doc_id):
    return client.post(f"{E}/submit", json={"doc_type": doc_type, "doc_id": doc_id}, headers=s["h"])


def _valid(client, s, doc_type, doc):
    r = _submit(client, s, doc_type, doc["id"])
    assert r.status_code == 200, r.text
    e = client.post(f"{E}/{r.json()['id']}/refresh", headers=s["h"]).json()
    assert e["status"] == "VALID", e
    return e


# --- company profile ----------------------------------------------------------------------


@pytest.mark.db
def test_company_profile_is_checked(store, client) -> None:  # noqa: F811
    h = store["h"]
    v = client.get("/api/v1/company", headers=h).json()["version"]
    bad = client.patch(
        "/api/v1/company",
        json={"version": v, "tin": "12345", "msic_code": "4719", "state_code": "99"},
        headers=h,
    )
    assert bad.status_code == 422
    assert set(bad.json()["error"]["details"]["fields"]) == {"tin", "msic_code", "state_code"}
    cashier = login(client, "cashier1")
    assert client.patch("/api/v1/company", json={"version": v}, headers=cashier).status_code == 403


# --- document -----------------------------------------------------------------------------


@pytest.mark.db
def test_preview_builds_the_ubl_invoice(ready, client) -> None:
    inv = _invoice(client, ready, "10", unit_price="2.50", discount_pct="10")
    pv = client.get(f"{E}/preview/INV/{inv['id']}", headers=ready["h"]).json()
    assert pv["problems"] == [] and pv["warnings"] == []
    doc = pv["document"]
    assert doc["_D"].endswith("Invoice-2")
    i = doc["Invoice"][0]
    assert i["ID"] == [{"_": inv["doc_no"]}]
    assert i["InvoiceTypeCode"] == [{"_": "01", "listVersionID": "1.0"}]
    assert i["IssueTime"][0]["_"].endswith("Z")
    seller = i["AccountingSupplierParty"][0]["Party"][0]
    assert seller["IndustryClassificationCode"][0]["_"] == "47190"
    assert {x["ID"][0]["schemeID"]: x["ID"][0]["_"] for x in seller["PartyIdentification"]} == {
        "TIN": "C1234567890",
        "BRN": "202001012345",
        "SST": "W10-1808-32000001",
        "TTX": "NA",
    }
    line = i["InvoiceLine"][0]
    assert line["InvoicedQuantity"] == [{"_": 10.0, "unitCode": "C62"}]
    assert line["ItemPriceExtension"][0]["Amount"][0]["_"] == 25.0  # before discount
    assert line["AllowanceCharge"][0]["Amount"][0]["_"] == 2.5
    assert line["LineExtensionAmount"][0]["_"] == 22.5
    assert line["TaxTotal"][0]["TaxSubtotal"][0]["TaxCategory"][0]["ID"] == [{"_": "06"}]
    total = i["LegalMonetaryTotal"][0]
    assert total["PayableAmount"] == [{"_": 22.5, "currencyID": "MYR"}]


@pytest.mark.db
def test_preview_lists_what_lhdn_would_reject(store, client) -> None:  # noqa: F811
    d = client.post(
        "/api/v1/sales/sales-invoice",
        json={
            "doc_date": date.today().isoformat(),
            "customer_id": store["customer"],
            "location_id": store["hq"],
            "lines": [milo(store, "1")],
        },
        headers=store["h"],
    ).json()
    pv = client.get(f"{E}/preview/INV/{d['id']}", headers=store["h"]).json()
    fields = {x["field"] for x in pv["problems"]}
    assert {"status", "company.einvoice_enabled", "company.tin", "company.msic_code"} <= fields
    # Customer without TIN: general public TIN, with a warning
    assert pv["warnings"][0]["field"] == "customer.tin"
    buyer = pv["document"]["Invoice"][0]["AccountingCustomerParty"][0]["Party"][0]
    assert buyer["PartyIdentification"][0]["ID"] == [{"_": "EI00000000010", "schemeID": "TIN"}]


# --- submit, validate, cancel -------------------------------------------------------------


@pytest.mark.db
def test_submit_then_validate_gives_the_qr_link(ready, client, lhdn) -> None:
    inv = _invoice(client, ready)
    r = _submit(client, ready, "INV", inv["id"])
    assert r.status_code == 200, r.text
    e = r.json()
    assert e["status"] == "SUBMITTED" and e["uuid"] and e["environment"] == "sandbox"
    assert e["submission_uid"] == "HJSD135P2S7D8IU"
    assert lhdn.docs[e["uuid"]]["Invoice"][0]["ID"] == [{"_": inv["doc_no"]}]

    e = client.post(f"{E}/{e['id']}/refresh", headers=ready["h"]).json()
    assert e["status"] == "VALID" and e["validated_at"]
    assert (
        e["share_url"] == f"https://preprod.myinvois.hasil.gov.my/{e['uuid']}/share/{e['long_id']}"
    )
    assert lhdn.logins == 1  # token reused

    again = _submit(client, ready, "INV", inv["id"])
    assert again.status_code == 409
    pending = client.get(f"{E}/pending", headers=ready["h"]).json()
    assert inv["id"] not in [x["id"] for x in pending]


@pytest.mark.db
def test_rejected_or_failed_can_be_sent_again(ready, client, lhdn) -> None:
    inv = _invoice(client, ready)
    lhdn.reject_next = True
    e = _submit(client, ready, "INV", inv["id"]).json()
    assert e["status"] == "INVALID" and e["errors"][0]["message"] == "Buyer TIN invalid"

    lhdn.down = True
    e = _submit(client, ready, "INV", inv["id"]).json()
    assert e["status"] == "FAILED" and "Cannot reach MyInvois" in e["last_error"]

    lhdn.down = False
    e = _submit(client, ready, "INV", inv["id"]).json()
    assert e["status"] == "SUBMITTED"
    history = client.get(E, params={"doc_id": inv["id"]}, headers=ready["h"]).json()
    assert sorted(x["status"] for x in history) == ["FAILED", "INVALID", "SUBMITTED"]


@pytest.mark.db
def test_validation_errors_come_back(ready, client, lhdn) -> None:
    lhdn.verdict = "Invalid"
    inv = _invoice(client, ready)
    e = _submit(client, ready, "INV", inv["id"]).json()
    e = client.post(f"{E}/{e['id']}/refresh", headers=ready["h"]).json()
    assert e["status"] == "INVALID"
    assert [s["name"] for s in e["errors"]] == ["Step03-Taxpayer"]


@pytest.mark.db
def test_validated_invoice_must_be_cancelled_before_void(ready, client, lhdn) -> None:
    inv = _invoice(client, ready)
    e = _valid(client, ready, "INV", inv)
    blocked = sal_do(client, ready, "sales-invoice", inv, "void", reason="wrong customer")
    assert blocked.status_code == 409 and blocked.json()["error"]["details"] == {
        "einvoice_status": "VALID"
    }

    c = client.post(f"{E}/{e['id']}/cancel", json={"reason": "wrong customer"}, headers=ready["h"])
    assert c.status_code == 200 and c.json()["status"] == "CANCELLED"
    ok = sal_do(client, ready, "sales-invoice", inv, "void", reason="wrong customer")
    assert ok.status_code == 200


@pytest.mark.db
def test_no_cancel_after_72_hours(ready, client, lhdn) -> None:
    inv = _invoice(client, ready)
    e = _valid(client, ready, "INV", inv)
    with SessionLocal() as db:
        db.execute(
            update(Einvoice)
            .where(Einvoice.id == e["id"])
            .values(validated_at=datetime.now(UTC) - timedelta(hours=73))
        )
        db.commit()
    r = client.post(f"{E}/{e['id']}/cancel", json={"reason": "too late"}, headers=ready["h"])
    assert r.status_code == 422 and "credit note" in r.json()["error"]["message"]


@pytest.mark.db
def test_credit_note_refers_to_the_original_e_invoice(ready, client, lhdn) -> None:
    inv = _invoice(client, ready)
    cn = sal_posted(client, ready, "customer-cn", [milo(ready, "2")], ref_invoice_id=inv["id"])
    pv = client.get(f"{E}/preview/CN/{cn['id']}", headers=ready["h"]).json()
    assert [x["field"] for x in pv["problems"]] == ["ref_invoice_id"]  # original not sent yet

    e_inv = _valid(client, ready, "INV", inv)
    pv = client.get(f"{E}/preview/CN/{cn['id']}", headers=ready["h"]).json()
    assert pv["problems"] == []
    i = pv["document"]["Invoice"][0]
    assert i["InvoiceTypeCode"][0]["_"] == "02"
    ref = i["BillingReference"][0]["InvoiceDocumentReference"][0]
    assert ref == {"ID": [{"_": inv["doc_no"]}], "UUID": [{"_": e_inv["uuid"]}]}

    _valid(client, ready, "CN", cn)
    r = client.post(f"{E}/{e_inv['id']}/cancel", json={"reason": "mistake"}, headers=ready["h"])
    assert r.status_code == 409 and r.json()["error"]["details"]["documents"] == [cn["doc_no"]]


@pytest.mark.db
def test_not_set_up_and_not_allowed(ready, client) -> None:
    inv = _invoice(client, ready)
    myinvois.set_client(None)
    try:
        r = _submit(client, ready, "INV", inv["id"])
        assert r.status_code == 422 and "not set up" in r.json()["error"]["message"]
    finally:
        myinvois.reset_client()
    acct = login(client, "acct")  # accountant reads, does not send
    r = client.post(f"{E}/submit", json={"doc_type": "INV", "doc_id": inv["id"]}, headers=acct)
    assert r.status_code == 403


# --- client details (no database) ---------------------------------------------------------


def test_encoding_hash_matches_the_bytes_sent() -> None:
    enc = myinvois.encode({"Invoice": [{"ID": [{"_": "INV-é"}]}]})
    raw = base64.b64decode(enc.document)
    assert raw == enc.raw and hashlib.sha256(raw).hexdigest() == enc.document_hash
    assert json.loads(raw)["Invoice"][0]["ID"][0]["_"] == "INV-é"


def test_client_logs_in_again_when_the_token_expires_early() -> None:
    calls = {"login": 0, "api": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/connect/token":
            calls["login"] += 1
            assert request.headers["onbehalfof"] == "C1234567890"
            return httpx.Response(200, json={"access_token": f"t{calls['login']}"})
        calls["api"] += 1
        if request.headers["authorization"] == "Bearer t1":
            return httpx.Response(401)
        return httpx.Response(200, json={"status": "Valid"})

    c = myinvois.MyInvoisClient(
        "production",
        "id",
        "secret",
        onbehalfof="C1234567890",
        transport=httpx.MockTransport(handler),
    )
    assert c.details("ABC")["status"] == "Valid"
    assert calls == {"login": 2, "api": 2}
    assert str(c.http.base_url) == "https://api.myinvois.hasil.gov.my"
