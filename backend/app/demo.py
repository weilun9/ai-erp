"""End-to-end demo: one command walks a small Malaysian shop through a working day.

    python -m app.demo

Everything goes through the real HTTP API in-process (FastAPI TestClient), against its
own database `erp_demo`, which is wiped and rebuilt each run. Afterwards the data stays,
so the API can be explored at /docs with POSTGRES_DB=erp_demo (log in as `owner`).
"""

import os
import sys
import threading
import uuid
import warnings
from datetime import date, timedelta

DEMO_DB = "erp_demo"
os.environ["POSTGRES_DB"] = DEMO_DB  # before any app import: settings are cached
os.environ.setdefault("APP_ENV", "dev")
warnings.filterwarnings("ignore", category=DeprecationWarning)

from pathlib import Path  # noqa: E402

import psycopg  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Device  # noqa: E402
from app.services.seed import bootstrap  # noqa: E402

API = "/api/v1"
PASSWORD = os.environ.get("DEMO_PASSWORD", "demo-password-2026")
TODAY = date.today()
BACKEND = Path(__file__).resolve().parents[1]

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


# --- output -------------------------------------------------------------------------------


def chapter(n: int, title: str) -> None:
    print(f"\n{'=' * 78}\n {n}. {title}\n{'=' * 78}")


def say(text: str) -> None:
    print(f"   {text}")


def money(v) -> str:
    return f"RM {float(v):,.2f}"


def check(resp, *ok: int):
    if resp.status_code not in (ok or (200, 201)):
        raise SystemExit(f"Unexpected {resp.status_code} from {resp.request.url}: {resp.text}")
    return resp.json()


# --- setup --------------------------------------------------------------------------------


def fresh_database() -> None:
    s = get_settings()
    assert s.postgres_db == DEMO_DB  # never touch any other database
    with psycopg.connect(
        host=s.postgres_host,
        port=s.postgres_port,
        user=s.postgres_user,
        password=s.postgres_password,
        dbname="postgres",
        autocommit=True,
    ) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{DEMO_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{DEMO_DB}"')
    cfg = Config()  # no ini file: keeps alembic's log lines out of the demo output
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    command.upgrade(cfg, "head")
    with SessionLocal() as db:
        bootstrap(
            db,
            company_code="DEMO",
            company_name="Kedai Runcit Demo Sdn Bhd",
            hq_code="HQ",
            hq_name="Johor Bahru HQ",
            admin_username="owner",
            admin_password=PASSWORD,
        )
        db.commit()


class Computer:
    """One browser on one computer: keeps its own device cookie."""

    def __init__(self, name: str):
        self.name = name
        self.http = TestClient(app)
        self.h: dict[str, str] = {}

    def login(self, username: str) -> dict:
        body = {"username": username, "password": PASSWORD, "remember": True}
        r = self.http.post(f"{API}/auth/login", json=body)
        if r.status_code == 200:
            self.h = {"Authorization": f"Bearer {r.json()['access_token']}"}
        return r

    def get(self, path, **params):
        return check(self.http.get(API + path, params=params, headers=self.h))

    def post(self, path, body=None, *ok):
        return check(self.http.post(API + path, json=body or {}, headers=self.h), *ok)

    def patch(self, path, body):
        return check(self.http.patch(API + path, json=body, headers=self.h))

    def act(self, base, doc, action, **extra):
        return self.post(f"{base}/{doc['id']}/{action}", {"version": doc["version"], **extra})

    def posted(self, base, body):
        d = self.post(base, body)
        d = self.act(base, d, "save")
        warnings = d.get("warnings") or []
        return self.act(base, d, "post"), warnings


# --- the story ----------------------------------------------------------------------------


def main() -> None:
    print("AI ERP backend demo - every step below is a real API call")
    fresh_database()

    chapter(1, "Only computers the owner approved can log in")
    office = Computer("office PC")
    r = office.login("owner")
    err = r.json()["error"]
    say(f"Owner, right password, new computer -> {r.status_code} {err['code']}")
    with SessionLocal() as db:  # the very first computer is approved from the server CLI
        db.get(Device, uuid.UUID(err["details"]["device_id"])).is_authorized = True
        db.commit()
    say("First computer approved on the server (python -m app.cli authorize-device <id>)")
    say(f"Owner logs in again -> {office.login('owner').status_code}, device remembered")

    tags = {t["code"]: t["id"] for t in office.get("/tags")["items"]}
    hq = office.get("/locations")["items"][0]["id"]
    office.post(
        "/users",
        {
            "username": "aminah",
            "display_name": "Aminah (cashier)",
            "password": PASSWORD,
            "tag_ids": [tags["cashier"]],
            "location_ids": [hq],
        },
    )
    counter = Computer("shop counter")
    r = counter.login("aminah")
    say(f"Cashier Aminah on the shop counter -> {r.status_code} {r.json()['error']['code']}")
    office.post(f"/devices/{r.json()['error']['details']['device_id']}/authorize")
    again = counter.login("aminah").status_code
    say(f"Owner approves that computer from the office -> Aminah: {again}")

    chapter(2, "Master data: items with carton/piece units, supplier, customer")
    taxes = {t["code"]: t["id"] for t in office.get("/tax-codes")["items"]}
    milo = office.post(
        "/items",
        {
            "code": "MILO-1KG",
            "name": "Milo 1kg",
            "base_uom": "PCS",
            "sales_tax_code_id": taxes["NA"],
            "barcodes": [{"barcode": "9556001000019", "uom": "PCS"}],
            "uoms": [
                {"uom": "PCS", "factor": "1", "selling_price": "26.90"},
                {"uom": "CTN", "factor": "12", "selling_price": "300.00"},
            ],
        },
    )["id"]
    say("Milo 1kg: PCS RM 26.90, CTN of 12 RM 300.00")
    hit = counter.get("/items/lookup", barcode="955600100001")
    found = hit["hits"][0]["item"]["code"]
    say(f"Scanner reads only 12 digits (no check digit) -> still finds {found}")
    nestle = office.post("/suppliers", {"code": "NESTLE", "name": "Nestle Products Sdn Bhd"})["id"]
    customer = office.post(
        "/customers",
        {
            "code": "AHSENG",
            "name": "Kedai Ah Seng",
            "term_days": 30,
            "credit_limit": "600",
        },
    )["id"]
    say("Supplier Nestle; customer Kedai Ah Seng (30 days, credit limit RM 600)")

    chapter(3, "Buying at two prices -> moving-average cost")
    for ref, price in (("NIV-1001", "240.00"), ("NIV-1002", "264.00")):
        grn, _ = office.posted(
            "/purchase/grn",
            {
                "doc_date": TODAY.isoformat(),
                "supplier_id": nestle,
                "location_id": hq,
                "supplier_ref_no": ref,
                "lines": [{"item_id": milo, "uom": "CTN", "qty": "2", "unit_price": price}],
            },
        )
        say(f"{grn['doc_no']} (supplier invoice {ref}): 2 CTN @ {money(price)}")
    bal = office.get("/stock/balance", item_id=milo, location_id=hq)["items"][0]
    say(f"Stock {float(bal['qty']):.0f} pcs, average cost {money(bal['avg_cost'])} per piece")
    seen = counter.get("/stock/balance", item_id=milo, location_id=hq)["items"][0]
    hidden = "avg_cost" not in seen
    say(f"Same call as the cashier: qty {float(seen['qty']):.0f}, cost hidden = {hidden}")

    chapter(4, "Selling: profit, below-cost and credit-limit warnings")
    sale = lambda qty, price, days_ago=0: {  # noqa: E731
        "doc_date": (TODAY - timedelta(days=days_ago)).isoformat(),
        "customer_id": customer,
        "location_id": hq,
        "lines": [{"item_id": milo, "uom": "PCS", "qty": qty, "unit_price": price}],
    }
    inv1, w = office.posted("/sales/sales-invoice", sale("12", "26.90", days_ago=40))
    total, profit = money(inv1["total"]), money(inv1["gross_profit"])
    say(f"{inv1['doc_no']}: 12 pcs, total {total}, gross profit {profit}")
    inv2, w = office.posted("/sales/sales-invoice", sale("10", "19.00", days_ago=5))
    say(f"{inv2['doc_no']}: 10 pcs at RM 19.00 -> warnings {[x['code'] for x in w]}")
    inv3, w = office.posted("/sales/sales-invoice", sale("4", "26.90"))
    codes = [x["code"] for x in w]
    say(f"{inv3['doc_no']}: 4 pcs, Ah Seng now owes over RM 600 -> warnings {codes}")
    bal = office.get("/stock/balance", item_id=milo, location_id=hq)["items"][0]
    say(f"Stock left {float(bal['qty']):.0f} pcs (selling past zero is allowed and flagged later)")

    chapter(5, "Receiving money: knocked off against the oldest invoice first")
    receipt, w = office.posted(
        "/finance/receipt",
        {
            "doc_date": TODAY.isoformat(),
            "location_id": hq,
            "customer_id": customer,
            "method": "TRANSFER",
            "amount": "400",
        },
    )
    say(f"{receipt['doc_no']}: RM 400.00 by bank transfer, nothing chosen")
    items = office.get("/finance/open-items", party_type="customer", party_id=customer)
    for it in items["items"]:
        say(f"  open {it['doc_no']:<10} {money(it['open'])}")
    say(f"{inv1['doc_no']} is fully paid; balance owed {money(items['balance'])}")

    chapter(6, "Two receipts for the same invoice posted at the same moment")
    drafts = []
    for _ in range(2):
        d = office.post(
            "/finance/receipt",
            {
                "doc_date": TODAY.isoformat(),
                "location_id": hq,
                "customer_id": customer,
                "amount": "107.60",
                "auto_allocate": False,
                "lines": [{"invoice_id": inv3["id"], "amount": "107.60"}],
            },
        )
        drafts.append(office.act("/finance/receipt", d, "save"))
    results: list[int] = [0, 0]
    gate = threading.Barrier(2)

    def post(i: int) -> None:
        c = TestClient(app)
        gate.wait()
        r = c.post(
            f"{API}/finance/receipt/{drafts[i]['id']}/post",
            json={"version": drafts[i]["version"]},
            headers=office.h,
        )
        results[i] = r.status_code

    threads = [threading.Thread(target=post, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    say(f"Status codes {sorted(results)}: row locks let exactly one through")

    chapter(7, "Supplier payment advice: match by their numbers, book the bank fee")
    pv = office.post(
        "/finance/supplier-payment",
        {
            "doc_date": TODAY.isoformat(),
            "location_id": hq,
            "supplier_id": nestle,
            "supplier_ref_no": "REMIT-5531",
            "lines": [
                {"line_type": "INVOICE", "ref_no": "niv 1001", "amount": "480"},
                {"line_type": "INVOICE", "ref_no": "NIV-9999", "amount": "100"},
                {"line_type": "FEE", "fee_type": "OTHER", "amount": "1.00"},
            ],
        },
    )
    for ln in pv["lines"]:
        say(
            f"  {ln['line_type']:<8}{ln.get('ref_no') or '':<10} -> {ln['match_status'] or 'fee'}"
            f" {ln.get('matched_no') or ''}"
        )
    pv = office.act("/finance/supplier-payment", pv, "save")
    pv = office.act("/finance/supplier-payment", pv, "post")
    issues = office.get("/issues", doc_id=pv["id"])
    issues = issues["items"] if isinstance(issues, dict) else issues
    say(f"Posted {pv['doc_no']}: GRN000001 knocked off, GRN000002 still open")
    for i in issues:
        say(f"  problem opened for follow-up: {i['issue_type']} - {i['title']}")

    chapter(8, "LHDN MyInvois e-invoice: build and check before sending")
    company = office.get("/company")
    office.patch(
        "/company",
        {
            "version": company["version"],
            "tin": "C1234567890",
            "id_type": "BRN",
            "id_no": "202001012345",
            "msic_code": "47190",
            "business_activity": "Retail sale in non-specialised stores",
            "address1": "No. 1, Jalan Demo",
            "city": "Johor Bahru",
            "postcode": "80000",
            "state_code": "01",
            "phone": "+6071234567",
            "email": "accounts@example.com",
            "einvoice_enabled": True,
        },
    )
    ahseng = office.get(f"/customers/{customer}")
    office.patch(
        f"/customers/{customer}",
        {
            "version": ahseng["version"],
            "tin": "C9876543210",
            "id_type": "BRN",
            "id_no": "201901000001",
            "address1": "Lot 5, Jalan Pasar",
            "city": "Johor Bahru",
            "postcode": "80300",
            "state_code": "01",
            "phone": "+6072345678",
        },
    )
    say("Company and customer tax details filled in (TIN, BRN, MSIC, address)")
    pv = office.get(f"/einvoice/preview/INV/{inv1['id']}")
    say(f"Preview {inv1['doc_no']}: UBL 2.1 JSON, {len(str(pv['document']))} characters")
    say(f"Would LHDN reject it? {[p['message'] for p in pv['problems']] or 'no'}")
    say(f"Warnings: {[x['message'] for x in pv['warnings']] or 'none'}")
    say("Submitting needs MyInvois sandbox credentials (MYINVOIS_* in .env)")

    chapter(9, "AI butler: the model only gets the tools this user may use")
    for who, pc in (("Owner", office), ("Cashier", counter)):
        names = [t["name"] for t in pc.get("/ai/tools")]
        say(f"{who} ({len(names)} tools): {', '.join(names)}")
    say("Every tool runs as the user in a rolled-back savepoint; drafts wait for Confirm")
    from app.services.llm import get_chat_model

    if get_chat_model() is None:
        say("No LLM_* settings, so no live question (set LLM_BASE_URL/API_KEY/MODEL)")
    else:
        conv = office.post("/ai/conversations", {})["id"]
        question = "How much Milo 1kg do we have, and what does Kedai Ah Seng still owe?"
        turn = office.post(f"/ai/conversations/{conv}/messages", {"content": question})
        say(f"Q: {question}")
        for m in turn["messages"]:
            for c in m.get("tool_calls") or []:
                say(f"  tool call: {c['name']} {c['arguments']}")
        say(f"A: {turn['messages'][-1]['content']}")

    print(f"\nDone. Data kept in database '{DEMO_DB}'. Explore it with the API docs:")
    print("   set POSTGRES_DB=erp_demo, run uvicorn app.main:app, open /docs, log in as owner")


if __name__ == "__main__":
    main()
