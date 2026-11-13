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
# DATABASE_URL set = a hosted database (the public demo site). It has one database, so
# instead of drop-and-create the demo empties the application's schemas; that only
# happens when DEMO_RESET_REMOTE=1 says so.
REMOTE = bool(os.environ.get("DATABASE_URL"))
if not REMOTE:
    os.environ["POSTGRES_DB"] = DEMO_DB  # before any app import: settings are cached
os.environ.setdefault("APP_ENV", "dev")
warnings.filterwarnings("ignore", category=DeprecationWarning)

from pathlib import Path  # noqa: E402

import psycopg  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.db.session import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Device  # noqa: E402
from app.services.seed import bootstrap  # noqa: E402

API = "/api/v1"
PASSWORD = os.environ.get("DEMO_PASSWORD", "demo-password-2026")
TODAY = date.today()
BACKEND = Path(__file__).resolve().parents[1]
SCHEMAS = ("core", "md", "pur", "sal", "inv", "fin", "ai", "audit", "acc")

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
    if REMOTE:
        if os.environ.get("DEMO_RESET_REMOTE") != "1":
            raise SystemExit(
                "DATABASE_URL is set: run with DEMO_RESET_REMOTE=1 to wipe that database"
            )
        with engine.begin() as conn:
            conn.exec_driver_sql(f"DROP SCHEMA IF EXISTS {', '.join(SCHEMAS)} CASCADE")
            conn.exec_driver_sql("DROP TABLE IF EXISTS public.alembic_version")
        _migrate_and_bootstrap()
        return
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
    _migrate_and_bootstrap()


def _migrate_and_bootstrap() -> None:
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
    attr = {
        code: office.post("/item-attributes", {"kind": kind, "code": code, "name": name})["id"]
        for kind, code, name in (
            ("BRAND", "NESTLE", "Nestle"),
            ("GROUP", "BEVERAGE", "Beverages"),
            ("SIZE", "1KG", "1 kg"),
        )
    }
    milo = office.post(
        "/items",
        {
            "code": "MILO-1KG",
            "name": "Milo 1kg",
            "description_2": "Chocolate malt drink powder",
            "brand_id": attr["NESTLE"],
            "group_id": attr["BEVERAGE"],
            "size_id": attr["1KG"],
            "reorder_level": "24",
            "max_qty": "60",
            "min_selling_price": "22.00",
            "base_uom": "PCS",
            "sales_tax_code_id": taxes["NA"],
            "barcodes": [{"barcode": "9556001000019", "uom": "PCS"}],
            "uoms": [
                {"uom": "PCS", "factor": "1", "selling_price": "26.90", "price_2": "25.50"},
                {"uom": "CTN", "factor": "12", "selling_price": "300.00", "price_2": "290.00"},
            ],
        },
    )["id"]
    say("Milo 1kg (brand Nestle, group Beverages): PCS RM 26.90, CTN of 12 RM 300.00;")
    say("  wholesale price level 2: PCS RM 25.50 / CTN RM 290.00; reorder at 24, fill up to 60")
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
    po, _ = office.posted(
        "/purchase/purchase-order",
        {
            "doc_date": TODAY.isoformat(),
            "supplier_id": nestle,
            "location_id": hq,
            "lines": [{"item_id": milo, "uom": "CTN", "qty": "3", "unit_price": "264.00"}],
        },
    )
    part = office.post(
        "/purchase/grn/receive-po",
        {"po_id": po["id"], "lines": [{"source_line_id": po["lines"][0]["id"], "qty": "1"}]},
    )
    say(f"{po['doc_no']} issued for 3 CTN; receiving it starts a GRN draft for 1 CTN")
    gone = office.http.delete(API + f"/purchase/grn/{part['id']}", headers=office.h)
    assert gone.status_code == 204, gone.text
    say("  (draft dropped: the 3 CTN stay on order and show on the item and reorder list)")
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

    chapter(10, "Knowledge base: company rules the AI answers from, with sources")
    from app.kb_samples import DOCS, upload_all
    from app.services.embeddings import get_embedder

    def upload(path, files, data):
        return check(office.http.post(API + path, files=files, data=data, headers=office.h))

    upload_all(upload)
    for _, _, title, _, tags in DOCS:
        say(f"Uploaded '{title}' - visible to {', '.join(tags) if tags else 'everyone'}")
    embedder = get_embedder()
    say(f"Search: {'vector, ' + embedder.name if embedder else 'keyword only (no EMBED_*)'}")
    q = "Nestle minimum order"
    for who, pc in (("Owner", office), ("Cashier", counter)):
        hits = pc.get("/kb/search", q=q, limit=2)
        found = "; ".join(f"{h['title']} > {h['section']}" for h in hits) or "nothing"
        say(f"{who} searches '{q}': {found}")
    if get_chat_model() is not None:
        conv = counter.post("/ai/conversations", {})["id"]
        question = "顾客没有收据可以退货吗？退款超过 RM50 要怎么办？"
        turn = counter.post(f"/ai/conversations/{conv}/messages", {"content": question})
        say(f"Cashier asks: {question}")
        say(f"A: {turn['messages'][-1]['content']}")

    chapter(11, "The till: scan, 5-sen rounding, offline retry, supervisor, refund, close")
    shift = counter.post("/pos/shifts", {"location_id": hq, "opening_float": "200"})
    say(f"Aminah opens {shift['shift_no']} on the shop counter with a RM200.00 float")
    sale_body = {
        "client_uuid": str(uuid.uuid4()),
        "lines": [{"barcode": "955600100001", "qty": "1"}],  # 12 digits, no check digit
        "payments": [{"method": "CASH", "amount": "30"}],
    }
    sale = counter.post("/pos/sales", sale_body)
    say(
        f"{sale['receipt_no']}: Milo 1kg RM{float(sale['subtotal']):.2f}, paid RM30.00 cash, "
        f"change RM{float(sale['change']):.2f}"
    )
    again = counter.http.post(f"{API}/pos/sales", json=sale_body, headers=counter.h)
    say(
        f"Till retries the same sale after a network drop -> {again.status_code}, "
        f"same receipt {again.json()['receipt_no']}, stock moved once"
    )
    odd = {
        "client_uuid": str(uuid.uuid4()),
        "lines": [{"barcode": "9556001000019", "qty": "1", "unit_price": "24.99"}],
        "payments": [{"method": "CASH", "amount": "25"}],
    }
    r = counter.http.post(f"{API}/pos/sales", json=odd, headers=counter.h)
    say(f"Cashier types a lower price -> {r.status_code} {r.json()['error']['details']['code']}")
    odd["supervisor"] = {"username": "owner", "password": PASSWORD}
    sale2 = counter.post("/pos/sales", odd)
    say(
        f"Owner keys in their login at the till -> {sale2['receipt_no']} at RM24.99, "
        f"cash rounds RM24.99 to RM{float(sale2['total']):.2f}"
    )
    refund = counter.post(
        "/pos/returns",
        {
            "client_uuid": str(uuid.uuid4()),
            "receipt_no": sale["receipt_no"],
            "lines": [{"line_no": 1, "qty": "1"}],
            "reason": "dented tin",
        },
    )
    say(f"{refund['receipt_no']}: refund RM{-float(refund['total']):.2f} cash (dented tin)")
    twice = counter.http.post(
        f"{API}/pos/returns",
        json={
            "client_uuid": str(uuid.uuid4()),
            "receipt_no": sale["receipt_no"],
            "lines": [{"line_no": 1, "qty": "1"}],
        },
        headers=counter.h,
    )
    say(f"Refunding the same tin again -> {twice.status_code}: {twice.json()['error']['message']}")
    report = counter.get(f"/pos/shifts/{shift['id']}/report")
    expected = float(report["expected_cash"])
    say(f"Drawer should hold RM{expected:.2f}; Aminah counts RM{expected - 0.05:.2f}")
    closed = counter.post(
        f"/pos/shifts/{shift['id']}/close", {"counted_cash": f"{expected - 0.05:.2f}"}
    )
    say(f"Shift closed, variance RM{float(closed['variance']):.2f} (under RM5, no note needed)")

    chapter(12, "Month end: walk-in sales go to LHDN as one consolidated e-invoice")
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo

    from sqlalchemy import update

    from app.localization.my import TIMEZONE
    from app.models import PosSale

    first = _dt.now(ZoneInfo(TIMEZONE)).date().replace(day=1)
    last = first - timedelta(days=1)
    with SessionLocal() as db:  # demo only: pretend today's till receipts were last month
        db.execute(
            update(PosSale).values(
                sold_at=_dt(last.year, last.month, 15, 12, tzinfo=ZoneInfo(TIMEZONE))
            )
        )
        db.commit()
    say(f"(Demo setup: today's receipts moved to {last:%B %Y} so that month is over)")
    pv = office.get("/pos/consolidations/preview", period=last.isoformat())
    for line in pv["lines"]:
        say(
            f"  line: {line['description']}  RM{float(line['subtotal']):.2f} "
            f"+ tax RM{float(line['tax']):.2f}"
        )
    say(f"Cash rounding RM{float(pv['rounding']):.2f}; total RM{float(pv['total']):.2f}")
    con = office.post("/pos/consolidations", {"period": last.isoformat()})
    ubl = office.get(f"/einvoice/preview/CON/{con['id']}")
    inv = ubl["document"]["Invoice"][0]
    buyer = inv["AccountingCustomerParty"][0]["Party"][0]["PartyIdentification"][0]["ID"][0]
    cls = inv["InvoiceLine"][0]["Item"][0]["CommodityClassification"][0]
    say(
        f"{con['doc_no']}: buyer TIN {buyer['_']}, classification "
        f"{cls['ItemClassificationCode'][0]['_']}, period "
        f"{inv['InvoicePeriod'][0]['StartDate'][0]['_']} to "
        f"{inv['InvoicePeriod'][0]['EndDate'][0]['_']}"
    )
    say(f"Would LHDN reject it? {[x['message'] for x in ubl['problems']] or 'no'}")

    chapter(13, "The books: every document above is already in the general ledger")
    accs = {x["system_key"]: x["id"] for x in office.get("/accounting/accounts")["items"]}
    office.post(
        "/accounting/journals",
        {
            "journal_date": TODAY.replace(month=1, day=1).isoformat(),
            "memo": "Opening capital paid into the bank",
            "lines": [
                {"account_id": accs["BANK"], "debit": "10000"},
                {"account_id": accs["CAPITAL"], "credit": "10000"},
            ],
        },
    )
    say("Journal voucher: owner's RM10,000 capital into the bank (the only entry by hand)")
    tb = office.get("/accounting/trial-balance", as_of=TODAY.isoformat())
    say(
        f"Trial balance: debits RM{float(tb['total_debit']):.2f} = "
        f"credits RM{float(tb['total_credit']):.2f}"
    )
    if tb["total_debit"] != tb["total_credit"]:
        raise SystemExit("Trial balance does not balance")
    pl = office.get(
        "/accounting/profit-and-loss",
        date_from=TODAY.replace(month=1, day=1).isoformat(),
        date_to=TODAY.isoformat(),
    )
    say(
        f"This year: net sales RM{float(pl['net_sales']):.2f}, gross profit "
        f"RM{float(pl['gross_profit']):.2f}, net profit RM{float(pl['net_profit']):.2f}"
    )
    bs = office.get("/accounting/balance-sheet", as_of=TODAY.isoformat())
    if not bs["balanced"]:
        raise SystemExit("Balance sheet does not balance")
    say(
        f"Balance sheet: assets RM{float(bs['total_assets']):.2f} = liabilities "
        f"RM{float(bs['total_liabilities']):.2f} + equity RM{float(bs['total_equity']):.2f}"
    )

    print(f"\nDone. Data kept in database '{DEMO_DB}'. Explore it with the API docs:")
    print("   set POSTGRES_DB=erp_demo, run uvicorn app.main:app, open /docs, log in as owner")


if __name__ == "__main__":
    main()
