"""General ledger: chart of accounts, journals made from posted documents, manual journal
vouchers, and the financial statements (trial balance, profit and loss, balance sheet,
account ledger).

Every posted document gets one journal (same date); a voided one also gets a reversing
journal dated the day it was voided. `sync()` makes whatever journals are missing - it runs
before every accounting read, so the books always match the documents and no posting code
in the other modules has to know about accounting.

What each document books (Dr / Cr):
  sales invoice     AR total / sales + output tax;  COGS / inventory (stock it took out)
  delivery order    COGS / inventory
  customer CN       sales returns + output tax / AR;  inventory / COGS (stock back)
  receipt           cash or bank / AR
  GRN               inventory + input tax (+/- rounding) / AP
  supplier CN       AP / inventory + input tax (+/- rounding)
  supplier payment  AP / bank (a negative payment the other way round)
  fee on a payment  AP / other income + output tax
  supplier DN       purchase charges + input tax / AP
  supplier credit   AP / other income + input tax
  supplier refund   bank / AP
  POS sale/return   cash or card takings / sales + output tax + rounding; COGS / inventory
  stock documents   inventory / stock adjustments (or the reverse); transfers book nothing
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, selectinload

from app.core.errors import BadRequest, BusinessRule, Conflict, NotFound, VersionConflict
from app.localization.my import TIMEZONE
from app.models import (
    Account,
    CustomerCn,
    CustomerDebitNote,
    CustomerReceipt,
    DeliveryOrder,
    Grn,
    Journal,
    JournalLine,
    PosSale,
    SalesInvoice,
    StockAdjustment,
    StockLedger,
    Stocktake,
    StockWriteOff,
    SupplierCn,
    SupplierCreditAdj,
    SupplierDebitNote,
    SupplierFee,
    SupplierPayment,
    SupplierRefund,
)
from app.services import audit
from app.services.auth import Principal
from app.services.numbering import next_doc_no

ZERO = Decimal(0)
CENT = Decimal("0.01")
TZ = ZoneInfo(TIMEZONE)

# code, name, type, system key
DEFAULT_CHART = (
    ("1000", "Cash in hand", "ASSET", "CASH"),
    ("1010", "Bank", "ASSET", "BANK"),
    ("1100", "Trade receivables", "ASSET", "AR"),
    ("1200", "Inventory", "ASSET", "INVENTORY"),
    ("1300", "Tax paid on purchases", "ASSET", "INPUT_TAX"),
    ("2000", "Trade payables", "LIABILITY", "AP"),
    ("2100", "SST payable", "LIABILITY", "OUTPUT_TAX"),
    ("3000", "Capital", "EQUITY", "CAPITAL"),
    ("3100", "Retained earnings", "EQUITY", "RETAINED"),
    ("4000", "Sales", "INCOME", "SALES"),
    ("4010", "Sales returns", "INCOME", "SALES_RETURNS"),
    ("4100", "Supplier rebates and fees", "INCOME", "OTHER_INCOME"),
    ("5000", "Cost of goods sold", "EXPENSE", "COGS"),
    ("5100", "Stock adjustments and write-offs", "EXPENSE", "STOCK_ADJ"),
    ("6000", "Purchase charges", "EXPENSE", "PURCHASE_EXP"),
    ("6900", "Rounding", "EXPENSE", "ROUNDING"),
)
# Party sub-ledgers and stock are kept by the documents; a manual journal may not touch them.
CONTROL = ("AR", "AP", "INVENTORY")
DEBIT_NORMAL = ("ASSET", "EXPENSE")


def q2(v) -> Decimal:
    return Decimal(v).quantize(CENT)


# --- chart of accounts --------------------------------------------------------------------


def ensure_chart(db: Session, company_id: uuid.UUID) -> dict[str, Account]:
    """The system accounts by key, creating any the company does not have yet."""
    have = {
        a.system_key: a
        for a in db.scalars(
            select(Account).where(Account.company_id == company_id, Account.system_key.is_not(None))
        )
    }
    codes = set(db.scalars(select(Account.code).where(Account.company_id == company_id)))
    for code, name, type_, key in DEFAULT_CHART:
        if key in have:
            continue
        while code in codes:  # the user took the default code: use the next free one
            code = str(int(code) + 1)
        a = Account(company_id=company_id, code=code, name=name, type=type_, system_key=key)
        db.add(a)
        codes.add(code)
        have[key] = a
    db.flush()
    return have


def list_accounts(db: Session, p: Principal) -> list[Account]:
    p.require("acc.read")
    ensure_chart(db, p.company_id)
    db.commit()
    return list(
        db.scalars(select(Account).where(Account.company_id == p.company_id).order_by(Account.code))
    )


def create_account(db: Session, p: Principal, *, code: str, name: str, type: str) -> Account:
    p.require("acc.account.manage")
    if db.scalar(
        select(Account.id).where(Account.company_id == p.company_id, Account.code == code)
    ):
        raise Conflict("Account code already exists", {"code": code})
    a = Account(
        company_id=p.company_id,
        code=code,
        name=name,
        type=type,
        created_by=p.user_id,
        updated_by=p.user_id,
    )
    db.add(a)
    db.flush()
    audit.record(db, p, "create", "acc.account", a.id, after=audit.snapshot(a))
    db.commit()
    return a


def update_account(db: Session, p: Principal, account_id: uuid.UUID, version: int, **v: Any):
    p.require("acc.account.manage")
    a = db.get(Account, account_id)
    if a is None or a.company_id != p.company_id:
        raise NotFound("Account not found")
    if a.version != version:
        raise VersionConflict("This account was changed by someone else; reload it")
    if a.system_key and v.get("is_active") is False:
        raise BusinessRule("Documents post to this account; it cannot be switched off")
    before = audit.snapshot(a)
    for k, val in v.items():
        if val is not None:
            setattr(a, k, val)
    a.version += 1
    a.updated_by = p.user_id
    db.flush()
    audit.record(db, p, "update", "acc.account", a.id, before=before, after=audit.snapshot(a))
    db.commit()
    return a


# --- what each document books --------------------------------------------------------------


@dataclass
class Entry:
    journal_date: date
    location_id: uuid.UUID | None
    memo: str
    lines: list[tuple[str, Decimal, str | None, uuid.UUID | None]]  # key, signed (+Dr), party


def _local(ts: datetime) -> date:
    return ts.astimezone(TZ).date()


def _money_key(method: str | None) -> str:
    return "CASH" if method == "CASH" else "BANK"


def _stock_values(db: Session, code: str, ids: list[uuid.UUID]) -> dict[uuid.UUID, Decimal]:
    """Value each document moved into (+) or out of (-) stock, from the stock ledger."""
    if not ids:
        return {}
    rows = db.execute(
        select(
            StockLedger.doc_id,
            func.sum((StockLedger.qty_in - StockLedger.qty_out) * StockLedger.unit_cost),
        )
        .where(
            StockLedger.doc_type == code,
            StockLedger.doc_id.in_(ids),
            StockLedger.is_reversal.is_(False),
        )
        .group_by(StockLedger.doc_id)
    ).all()
    return {i: q2(v) for i, v in rows}


def _cogs(lines, value: Decimal) -> None:
    if value:
        lines.append(("INVENTORY", value, None, None))
        lines.append(("COGS", -value, None, None))


def _sales_invoice(d, sv):
    lines = [
        ("AR", d.total, "C", d.customer_id),
        ("SALES", -d.subtotal, None, None),
        ("OUTPUT_TAX", -d.tax_total, None, None),
    ]
    _cogs(lines, sv.get(d.id, ZERO))
    return lines


def _delivery_order(d, sv):
    lines = []
    _cogs(lines, sv.get(d.id, ZERO))
    return lines


def _customer_cn(d, sv):
    lines = [
        ("SALES_RETURNS", d.subtotal, None, None),
        ("OUTPUT_TAX", d.tax_total, None, None),
        ("AR", -d.total, "C", d.customer_id),
    ]
    _cogs(lines, sv.get(d.id, ZERO))
    return lines


def _customer_dn(d, sv):
    return [
        ("AR", d.total, "C", d.customer_id),
        ("SALES", -d.subtotal, None, None),
        ("OUTPUT_TAX", -d.tax_total, None, None),
    ]


def _receipt(d, sv):
    return [(_money_key(d.method), d.amount, None, None), ("AR", -d.amount, "C", d.customer_id)]


def _grn(d, sv):
    return [
        ("INVENTORY", d.subtotal, None, None),
        ("INPUT_TAX", d.tax_total, None, None),
        ("ROUNDING", d.rounding_adj, None, None),
        ("AP", -d.total, "S", d.supplier_id),
    ]


def _supplier_cn(d, sv):
    return [
        ("AP", d.total, "S", d.supplier_id),
        ("INVENTORY", -d.subtotal, None, None),
        ("INPUT_TAX", -d.tax_total, None, None),
        ("ROUNDING", -d.rounding_adj, None, None),
    ]


def _payment(d, sv):
    return [("AP", d.amount, "S", d.supplier_id), (_money_key(d.method), -d.amount, None, None)]


def _fee(d, sv):
    return [
        ("AP", d.amount, "S", d.supplier_id),
        ("OTHER_INCOME", -(d.amount - d.tax_amount), None, None),
        ("OUTPUT_TAX", -d.tax_amount, None, None),
    ]


def _supplier_dn(d, sv):
    return [
        ("PURCHASE_EXP", d.subtotal, None, None),
        ("INPUT_TAX", d.tax_total, None, None),
        ("AP", -d.total, "S", d.supplier_id),
    ]


def _supplier_adj(d, sv):
    return [
        ("AP", d.total, "S", d.supplier_id),
        ("OTHER_INCOME", -d.subtotal, None, None),
        ("INPUT_TAX", -d.tax_total, None, None),
    ]


def _supplier_refund(d, sv):
    return [(_money_key(d.method), d.amount, None, None), ("AP", -d.amount, "S", d.supplier_id)]


def _pos(d, sv):
    lines = [(_money_key(pay.method), pay.amount, None, None) for pay in d.payments]
    lines += [
        ("SALES", -d.subtotal, None, None),
        ("OUTPUT_TAX", -d.tax_total, None, None),
        ("ROUNDING", -d.rounding, None, None),
    ]
    _cogs(lines, sv.get(d.id, ZERO))
    return lines


def _stock_doc(d, sv):
    v = sv.get(d.id, ZERO)
    return [("INVENTORY", v, None, None), ("STOCK_ADJ", -v, None, None)] if v else []


@dataclass(frozen=True)
class Source:
    code: str  # journal source_type (the document's own code)
    model: type
    build: Any
    stock_code: str | None = None  # stock ledger doc_type, when it moved stock
    label: str = ""


SOURCES = (
    Source("INV", SalesInvoice, _sales_invoice, "INV", "Sales invoice"),
    Source("DO", DeliveryOrder, _delivery_order, "DO", "Delivery order"),
    Source("CN", CustomerCn, _customer_cn, "CN", "Customer CN"),
    Source("CDN", CustomerDebitNote, _customer_dn, None, "Customer debit note"),
    Source("OR", CustomerReceipt, _receipt, None, "Receipt"),
    Source("GRN", Grn, _grn, None, "Goods received"),
    Source("SCN", SupplierCn, _supplier_cn, None, "Goods return"),
    Source("PV", SupplierPayment, _payment, None, "Supplier payment"),
    Source("FEE", SupplierFee, _fee, None, "Fee deducted"),
    Source("SDN", SupplierDebitNote, _supplier_dn, None, "Supplier debit note"),
    Source("SCA", SupplierCreditAdj, _supplier_adj, None, "Supplier credit note"),
    Source("SRF", SupplierRefund, _supplier_refund, None, "Supplier refund"),
    Source("POS", PosSale, _pos, None, "POS"),
    Source("ADJ", StockAdjustment, _stock_doc, "ADJ", "Stock adjustment"),
    Source("WO", StockWriteOff, _stock_doc, "WO", "Write-off"),
    Source("STK", Stocktake, _stock_doc, "STK", "Stocktake"),
)


def _doc_no(d) -> str | None:
    return getattr(d, "doc_no", None) or getattr(d, "receipt_no", None)


def _doc_date(d) -> date:
    return _local(d.sold_at) if isinstance(d, PosSale) else d.doc_date


def _make(db, company_id, accounts, src: Source, d, lines, *, reversal: bool, when: date):
    j = Journal(
        company_id=company_id,
        doc_no=next_doc_no(db, company_id, "JNL"),
        journal_date=when,
        source_type=src.code,
        source_id=d.id,
        source_no=_doc_no(d),
        location_id=getattr(d, "location_id", None),
        memo=f"{'Void of ' if reversal else ''}{src.label} {_doc_no(d) or ''}".strip(),
        is_reversal=reversal,
    )
    sums: dict[tuple[str, str | None, Any], Decimal] = defaultdict(lambda: ZERO)
    for key, amount, ptype, pid in lines:
        sums[(key, ptype, pid)] += q2(amount) * (-1 if reversal else 1)
    total = sum(sums.values(), ZERO)
    if total:  # never leave a journal out of balance; cents go to rounding
        sums[("ROUNDING", None, None)] -= total
    n = 0
    for (key, ptype, pid), amount in sums.items():
        if not amount:
            continue
        n += 1
        j.lines.append(
            JournalLine(
                line_no=n,
                account_id=accounts[key].id,
                debit=amount if amount > 0 else ZERO,
                credit=-amount if amount < 0 else ZERO,
                party_type=ptype,
                party_id=pid,
            )
        )
    db.add(j)
    return j


def sync(db: Session, company_id: uuid.UUID) -> int:
    """Make the journals that are missing for posted and voided documents. Safe to run at
    any time and from two requests at once (one waits for the other)."""
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext('acc.sync'), hashtext(:c))"),
        {"c": str(company_id)},
    )
    accounts = ensure_chart(db, company_id)
    have = set(
        db.execute(
            select(Journal.source_type, Journal.source_id, Journal.is_reversal).where(
                Journal.company_id == company_id, Journal.source_id.is_not(None)
            )
        ).all()
    )
    made = 0
    for src in SOURCES:
        m = src.model
        stmt = select(m).where(m.company_id == company_id)
        if m is not PosSale:
            stmt = stmt.where(m.status.in_(("POSTED", "VOIDED")))
            if hasattr(m, "posted_at"):
                stmt = stmt.where(m.posted_at.is_not(None))
        if m is PosSale:
            stmt = stmt.options(selectinload(PosSale.payments))
        todo = [
            d
            for d in db.scalars(stmt)
            if (src.code, d.id, False) not in have
            or (getattr(d, "status", None) == "VOIDED" and (src.code, d.id, True) not in have)
        ]
        if not todo:
            continue
        stock_code = src.stock_code or ("POS" if m is PosSale else None)
        sv: dict[uuid.UUID, Decimal] = {}
        if m is PosSale:
            sv = _stock_values(db, "POS", [d.id for d in todo if d.kind == "SALE"])
            sv |= _stock_values(db, "POSR", [d.id for d in todo if d.kind != "SALE"])
        elif stock_code:
            sv = _stock_values(db, stock_code, [d.id for d in todo])
        for d in todo:
            lines = src.build(d, sv)
            if (src.code, d.id, False) not in have:
                _make(db, company_id, accounts, src, d, lines, reversal=False, when=_doc_date(d))
                made += 1
            if getattr(d, "status", None) == "VOIDED" and (src.code, d.id, True) not in have:
                when = _local(d.voided_at) if d.voided_at else _doc_date(d)
                _make(db, company_id, accounts, src, d, lines, reversal=True, when=when)
                made += 1
    db.flush()
    return made


def run_sync(db: Session, p: Principal) -> int:
    p.require("acc.read")
    n = sync(db, p.company_id)
    db.commit()
    return n


# --- manual journal vouchers ----------------------------------------------------------------


def create_manual(
    db: Session, p: Principal, *, journal_date: date, memo: str, lines: list[dict[str, Any]]
) -> Journal:
    """A journal voucher keyed in by the accountant (opening balances, depreciation,
    accruals...). It must balance and may not touch AR / AP / inventory, which the
    documents keep."""
    p.require("acc.journal.create")
    accounts = {
        a.id: a for a in db.scalars(select(Account).where(Account.company_id == p.company_id))
    }
    if len(lines) < 2:
        raise BadRequest("A journal needs at least two lines")
    dr = cr = ZERO
    j = Journal(
        company_id=p.company_id,
        doc_no=next_doc_no(db, p.company_id, "JV"),
        journal_date=journal_date,
        source_type="JV",
        memo=memo,
        created_by=p.user_id,
    )
    for n, ln in enumerate(lines, start=1):
        a = accounts.get(ln["account_id"])
        if a is None:
            raise BadRequest("Account not found", {"line": n})
        if not a.is_active:
            raise BusinessRule("Account is switched off", {"line": n, "account": a.code})
        if a.system_key in CONTROL:
            raise BusinessRule(
                "Receivables, payables and inventory are kept by the documents; "
                "use a document (or a knock-off) instead",
                {"line": n, "account": a.code},
            )
        d, c = q2(ln.get("debit") or 0), q2(ln.get("credit") or 0)
        if (d > 0) == (c > 0) or d < 0 or c < 0:
            raise BadRequest("Each line has either a debit or a credit", {"line": n})
        dr += d
        cr += c
        j.lines.append(
            JournalLine(line_no=n, account_id=a.id, debit=d, credit=c, memo=ln.get("memo"))
        )
    if dr != cr:
        raise BusinessRule(
            "Debits and credits do not balance", {"debit": str(dr), "credit": str(cr)}
        )
    db.add(j)
    db.flush()
    audit.record(db, p, "create", "acc.journal", j.id, after={"doc_no": j.doc_no, "amount": dr})
    db.commit()
    return get_journal(db, p, j.id)


def void_manual(db: Session, p: Principal, journal_id: uuid.UUID, reason: str) -> Journal:
    p.require("acc.journal.create")
    j = get_journal(db, p, journal_id)
    if j.source_type != "JV":
        raise BusinessRule("Journals from documents are voided by voiding the document")
    if j.status != "POSTED":
        raise BusinessRule("This journal is already voided")
    rev = Journal(
        company_id=p.company_id,
        doc_no=next_doc_no(db, p.company_id, "JV"),
        journal_date=_local(datetime.now(UTC)),
        source_type="JV",
        memo=f"Void of {j.doc_no}: {reason}",
        is_reversal=True,
        reverses_id=j.id,
        created_by=p.user_id,
    )
    for ln in j.lines:
        rev.lines.append(
            JournalLine(
                line_no=ln.line_no,
                account_id=ln.account_id,
                debit=ln.credit,
                credit=ln.debit,
                memo=ln.memo,
            )
        )
    j.status, j.voided_at, j.void_reason = "VOIDED", datetime.now(UTC), reason
    db.add(rev)
    db.flush()
    audit.record(db, p, "void", "acc.journal", j.id, after={"reason": reason})
    db.commit()
    return get_journal(db, p, j.id)


def get_journal(db: Session, p: Principal, journal_id: uuid.UUID) -> Journal:
    p.require("acc.read")
    j = db.scalar(
        select(Journal)
        .where(Journal.id == journal_id, Journal.company_id == p.company_id)
        .options(selectinload(Journal.lines))
        .execution_options(populate_existing=True)
    )
    if j is None:
        raise NotFound("Journal not found")
    return j


def list_journals(
    db: Session,
    p: Principal,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    source_type: str | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 50,
):
    p.require("acc.read")
    sync(db, p.company_id)
    db.commit()
    stmt = (
        select(Journal)
        .where(Journal.company_id == p.company_id)
        .options(selectinload(Journal.lines))
    )
    if date_from:
        stmt = stmt.where(Journal.journal_date >= date_from)
    if date_to:
        stmt = stmt.where(Journal.journal_date <= date_to)
    if source_type:
        stmt = stmt.where(Journal.source_type == source_type)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where((Journal.doc_no.ilike(like)) | (Journal.source_no.ilike(like)))
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    rows = db.scalars(
        stmt.order_by(Journal.journal_date.desc(), Journal.doc_no.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return list(rows), total


# --- statements ------------------------------------------------------------------------------


def _movements(db: Session, company_id, *, date_from: date | None, date_to: date | None):
    stmt = (
        select(JournalLine.account_id, func.sum(JournalLine.debit), func.sum(JournalLine.credit))
        .join(Journal, Journal.id == JournalLine.journal_id)
        .where(Journal.company_id == company_id)
        .group_by(JournalLine.account_id)
    )
    if date_from:
        stmt = stmt.where(Journal.journal_date >= date_from)
    if date_to:
        stmt = stmt.where(Journal.journal_date <= date_to)
    return {a: (d or ZERO, c or ZERO) for a, d, c in db.execute(stmt).all()}


def _accounts(db: Session, company_id) -> list[Account]:
    return list(
        db.scalars(select(Account).where(Account.company_id == company_id).order_by(Account.code))
    )


def _row(a: Account, d: Decimal, c: Decimal) -> dict[str, Any]:
    natural = d - c if a.type in DEBIT_NORMAL else c - d
    return {
        "account_id": a.id,
        "code": a.code,
        "name": a.name,
        "type": a.type,
        "debit": d,
        "credit": c,
        "balance": natural,  # in the account's normal direction
    }


def _prepare(db: Session, p: Principal) -> None:
    p.require("acc.read")
    sync(db, p.company_id)
    db.commit()


def trial_balance(db: Session, p: Principal, as_of: date) -> dict[str, Any]:
    _prepare(db, p)
    mv = _movements(db, p.company_id, date_from=None, date_to=as_of)
    rows = []
    for a in _accounts(db, p.company_id):
        d, c = mv.get(a.id, (ZERO, ZERO))
        if not d and not c:
            continue
        net = d - c
        rows.append(
            _row(a, d, c) | {"debit_balance": max(net, ZERO), "credit_balance": max(-net, ZERO)}
        )
    return {
        "as_of": as_of,
        "rows": rows,
        "total_debit": sum((r["debit_balance"] for r in rows), ZERO),
        "total_credit": sum((r["credit_balance"] for r in rows), ZERO),
    }


def profit_and_loss(db: Session, p: Principal, date_from: date, date_to: date) -> dict[str, Any]:
    _prepare(db, p)
    mv = _movements(db, p.company_id, date_from=date_from, date_to=date_to)
    income, expense = [], []
    for a in _accounts(db, p.company_id):
        if a.type not in ("INCOME", "EXPENSE"):
            continue
        d, c = mv.get(a.id, (ZERO, ZERO))
        if not d and not c:
            continue
        (income if a.type == "INCOME" else expense).append(_row(a, d, c))
    ti = sum((r["balance"] for r in income), ZERO)
    te = sum((r["balance"] for r in expense), ZERO)
    by_key = {a.id: a.system_key for a in _accounts(db, p.company_id)}
    sales = sum(
        (r["balance"] for r in income if by_key[r["account_id"]] in ("SALES", "SALES_RETURNS")),
        ZERO,
    )
    cogs = sum((r["balance"] for r in expense if by_key[r["account_id"]] == "COGS"), ZERO)
    return {
        "date_from": date_from,
        "date_to": date_to,
        "income": income,
        "expense": expense,
        "total_income": ti,
        "total_expense": te,
        "net_sales": sales,
        "gross_profit": sales - cogs,
        "net_profit": ti - te,
    }


def balance_sheet(db: Session, p: Principal, as_of: date) -> dict[str, Any]:
    _prepare(db, p)
    mv = _movements(db, p.company_id, date_from=None, date_to=as_of)
    out: dict[str, list] = {"ASSET": [], "LIABILITY": [], "EQUITY": []}
    profit = ZERO
    for a in _accounts(db, p.company_id):
        d, c = mv.get(a.id, (ZERO, ZERO))
        if not d and not c:
            continue
        if a.type in out:
            out[a.type].append(_row(a, d, c))
        else:
            profit += c - d  # income minus expense, not closed into retained earnings yet
    ta = sum((r["balance"] for r in out["ASSET"]), ZERO)
    tl = sum((r["balance"] for r in out["LIABILITY"]), ZERO)
    te = sum((r["balance"] for r in out["EQUITY"]), ZERO) + profit
    return {
        "as_of": as_of,
        "assets": out["ASSET"],
        "liabilities": out["LIABILITY"],
        "equity": out["EQUITY"],
        "profit_to_date": profit,
        "total_assets": ta,
        "total_liabilities": tl,
        "total_equity": te,
        "balanced": ta == tl + te,
    }


def account_ledger(
    db: Session, p: Principal, account_id: uuid.UUID, date_from: date, date_to: date
) -> dict[str, Any]:
    _prepare(db, p)
    a = db.get(Account, account_id)
    if a is None or a.company_id != p.company_id:
        raise NotFound("Account not found")
    sign = 1 if a.type in DEBIT_NORMAL else -1
    before = (
        db.execute(
            select(func.sum(JournalLine.debit - JournalLine.credit))
            .join(Journal, Journal.id == JournalLine.journal_id)
            .where(JournalLine.account_id == a.id, Journal.journal_date < date_from)
        ).scalar()
        or ZERO
    )
    rows = db.execute(
        select(JournalLine, Journal)
        .join(Journal, Journal.id == JournalLine.journal_id)
        .where(
            JournalLine.account_id == a.id,
            Journal.journal_date >= date_from,
            Journal.journal_date <= date_to,
        )
        .order_by(Journal.journal_date, Journal.doc_no, JournalLine.line_no)
    ).all()
    running = before * sign
    lines = []
    for ln, j in rows:
        running += (ln.debit - ln.credit) * sign
        lines.append(
            {
                "journal_id": j.id,
                "journal_no": j.doc_no,
                "date": j.journal_date,
                "source_type": j.source_type,
                "source_id": j.source_id,
                "source_no": j.source_no,
                "memo": ln.memo or j.memo,
                "debit": ln.debit,
                "credit": ln.credit,
                "balance": running,
            }
        )
    return {
        "account": _row(a, ZERO, ZERO) | {"system_key": a.system_key},
        "date_from": date_from,
        "date_to": date_to,
        "opening": before * sign,
        "lines": lines,
        "closing": running,
    }
