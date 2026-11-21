"""Bank reconciliation: tick the bank statement against the books.

Statement lines are fin.bank_txn rows (keyed in, pasted from the bank's CSV, or read from a
scanned statement). Book lines are journal lines on the bank account (debit = money in,
credit = money out). Matching links one statement line to the book line(s) that make up
the same amount (acc.bank_match). Statement lines with no book entry yet (bank charges,
interest) get one booked from here, already matched.

The reconciliation at a date: book balance - deposits not yet on the statement + payments
not yet through the bank = what the statement should show; the difference to the closing
balance the user reads off the statement should be 0.
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import BadRequest, BusinessRule, NotFound
from app.models import Account, BankMatch, BankTxn, Journal, JournalLine
from app.services import audit, ledger
from app.services.auth import Principal
from app.services.numbering import next_doc_no

ZERO = Decimal(0)
WINDOW = 10  # days either side when looking for the book entry of a statement line
READ, WRITE = "acc.read", "acc.journal.create"


def bank_account(db: Session, p: Principal, account_id: uuid.UUID | None) -> Account:
    if account_id is None:
        return ledger.ensure_chart(db, p.company_id)["BANK"]
    a = db.get(Account, account_id)
    if a is None or a.company_id != p.company_id or a.type != "ASSET":
        raise NotFound("Bank account not found")
    return a


def _signed(t: BankTxn) -> Decimal:
    return t.amount if t.direction == "IN" else -t.amount


def _of_account(db: Session, p: Principal, a: Account):
    default = a.system_key == "BANK"
    cond = BankTxn.account_id == a.id
    return (cond | BankTxn.account_id.is_(None)) if default else cond


def add_lines(
    db: Session, p: Principal, account_id: uuid.UUID | None, lines: list[dict[str, Any]]
) -> dict[str, int]:
    """Statement lines (amount > 0 money in, < 0 money out). An identical line already
    there (same date, amount, reference and text) is skipped, so a statement can be pasted
    twice safely."""
    p.require(WRITE)
    a = bank_account(db, p, account_id)
    added = skipped = 0
    for ln in lines:
        amount = Decimal(str(ln["amount"]))
        if not amount:
            raise BadRequest("A statement line needs an amount")
        direction = "IN" if amount > 0 else "OUT"
        desc = (ln.get("description") or "").strip() or None
        ref = (ln.get("reference") or "").strip() or None
        dup = db.scalar(
            select(BankTxn.id).where(
                BankTxn.company_id == p.company_id,
                _of_account(db, p, a),
                BankTxn.txn_date == ln["txn_date"],
                BankTxn.direction == direction,
                BankTxn.amount == abs(amount),
                BankTxn.reference.is_not_distinct_from(ref),
                BankTxn.description.is_not_distinct_from(desc),
            )
        )
        if dup:
            skipped += 1
            continue
        db.add(
            BankTxn(
                company_id=p.company_id,
                account_id=a.id,
                txn_date=ln["txn_date"],
                direction=direction,
                amount=abs(amount),
                reference=ref,
                description=desc,
                counterparty=(ln.get("counterparty") or "").strip() or None,
                created_by=p.user_id,
            )
        )
        added += 1
    db.commit()
    return {"added": added, "skipped": skipped}


def _matched(db: Session, txn_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[uuid.UUID]]:
    out: dict[uuid.UUID, list[uuid.UUID]] = {}
    if txn_ids:
        for t, ln in db.execute(
            select(BankMatch.bank_txn_id, BankMatch.journal_line_id).where(
                BankMatch.bank_txn_id.in_(txn_ids)
            )
        ):
            out.setdefault(t, []).append(ln)
    return out


def statement(
    db: Session,
    p: Principal,
    account_id: uuid.UUID | None,
    *,
    date_from: date | None,
    date_to: date | None,
    unmatched_only: bool,
) -> list[dict[str, Any]]:
    p.require(READ)
    a = bank_account(db, p, account_id)
    stmt = select(BankTxn).where(BankTxn.company_id == p.company_id, _of_account(db, p, a))
    if date_from:
        stmt = stmt.where(BankTxn.txn_date >= date_from)
    if date_to:
        stmt = stmt.where(BankTxn.txn_date <= date_to)
    txns = list(db.scalars(stmt.order_by(BankTxn.txn_date, BankTxn.created_at)))
    matched = _matched(db, [t.id for t in txns])
    rows = [
        {
            "id": t.id,
            "txn_date": t.txn_date,
            "amount": ledger.q2(_signed(t)),
            "description": t.description,
            "reference": t.reference,
            "counterparty": t.counterparty,
            "matched_line_ids": matched.get(t.id, []),
        }
        for t in txns
    ]
    return [r for r in rows if not r["matched_line_ids"]] if unmatched_only else rows


def _book_query(p: Principal, a: Account):
    taken = select(BankMatch.journal_line_id)
    return (
        select(JournalLine, Journal)
        .join(Journal, Journal.id == JournalLine.journal_id)
        .where(
            Journal.company_id == p.company_id,
            JournalLine.account_id == a.id,
            JournalLine.id.not_in(taken),
        )
    )


def _book_row(ln: JournalLine, j: Journal) -> dict[str, Any]:
    return {
        "id": ln.id,
        "date": j.journal_date,
        "amount": ln.debit - ln.credit,
        "journal_no": j.doc_no,
        "source_type": j.source_type,
        "source_id": j.source_id,
        "source_no": j.source_no,
        "memo": ln.memo or j.memo,
    }


def book_lines(
    db: Session, p: Principal, account_id: uuid.UUID | None, *, date_to: date | None
) -> list[dict[str, Any]]:
    """Book entries on the bank account not matched to any statement line yet."""
    p.require(READ)
    ledger.sync(db, p.company_id)
    db.commit()
    a = bank_account(db, p, account_id)
    stmt = _book_query(p, a)
    if date_to:
        stmt = stmt.where(Journal.journal_date <= date_to)
    rows = db.execute(stmt.order_by(Journal.journal_date, Journal.doc_no)).all()
    return [_book_row(ln, j) for ln, j in rows]


def suggest(db: Session, p: Principal, txn_id: uuid.UUID) -> list[dict[str, Any]]:
    """Unmatched book lines of the same amount within WINDOW days, nearest date first."""
    p.require(READ)
    t = _txn(db, p, txn_id)
    a = bank_account(db, p, t.account_id)
    amount = _signed(t)
    rows = db.execute(
        _book_query(p, a).where(
            (JournalLine.debit - JournalLine.credit) == amount,
            Journal.journal_date.between(
                t.txn_date - timedelta(days=WINDOW), t.txn_date + timedelta(days=WINDOW)
            ),
        )
    ).all()
    rows.sort(key=lambda r: abs((r[1].journal_date - t.txn_date).days))
    return [_book_row(ln, j) for ln, j in rows]


def _txn(db: Session, p: Principal, txn_id: uuid.UUID) -> BankTxn:
    t = db.get(BankTxn, txn_id)
    if t is None or t.company_id != p.company_id:
        raise NotFound("Statement line not found")
    return t


def match(db: Session, p: Principal, txn_id: uuid.UUID, line_ids: list[uuid.UUID]) -> None:
    p.require(WRITE)
    t = _txn(db, p, txn_id)
    if db.scalar(select(func.count()).where(BankMatch.bank_txn_id == t.id)):
        raise BusinessRule("This statement line is already matched; unmatch it first")
    a = bank_account(db, p, t.account_id)
    if not line_ids:
        raise BadRequest("Pick the book entries this statement line shows")
    found = db.execute(_book_query(p, a).where(JournalLine.id.in_(line_ids))).all()
    if len(found) != len(set(line_ids)):
        raise BusinessRule("Some entries are not on this bank account or are matched already")
    total = sum((ln.debit - ln.credit for ln, _ in found), ZERO)
    if total != _signed(t):
        raise BusinessRule(
            "The entries do not add up to the statement amount",
            {"statement": str(_signed(t)), "entries": str(total)},
        )
    for ln, _ in found:
        db.add(
            BankMatch(
                company_id=p.company_id,
                bank_txn_id=t.id,
                journal_line_id=ln.id,
                matched_by=p.user_id,
            )
        )
    audit.record(db, p, "match", "fin.bank_txn", t.id, after={"lines": [str(x) for x in line_ids]})
    db.commit()


def unmatch(db: Session, p: Principal, txn_id: uuid.UUID) -> None:
    p.require(WRITE)
    t = _txn(db, p, txn_id)
    for m in db.scalars(select(BankMatch).where(BankMatch.bank_txn_id == t.id)):
        db.delete(m)
    audit.record(db, p, "unmatch", "fin.bank_txn", t.id)
    db.commit()


def delete_line(db: Session, p: Principal, txn_id: uuid.UUID) -> None:
    """A statement line keyed in by mistake (only while unmatched)."""
    p.require(WRITE)
    t = _txn(db, p, txn_id)
    if db.scalar(select(func.count()).where(BankMatch.bank_txn_id == t.id)):
        raise BusinessRule("Unmatch this statement line before deleting it")
    audit.record(db, p, "delete", "fin.bank_txn", t.id, before={"amount": _signed(t)})
    db.delete(t)
    db.commit()


def auto_match(db: Session, p: Principal, account_id: uuid.UUID | None) -> int:
    """Match every statement line that has exactly one same-amount book entry nearby that
    no other statement line also wants."""
    p.require(WRITE)
    ledger.sync(db, p.company_id)
    lines = statement(db, p, account_id, date_from=None, date_to=None, unmatched_only=True)
    wants = {r["id"]: [b["id"] for b in suggest(db, p, r["id"])] for r in lines}
    claims: dict[uuid.UUID, int] = {}
    for ids in wants.values():
        for i in ids:
            claims[i] = claims.get(i, 0) + 1
    n = 0
    for txn_id, ids in wants.items():
        if len(ids) == 1 and claims[ids[0]] == 1:
            db.add(
                BankMatch(
                    company_id=p.company_id,
                    bank_txn_id=txn_id,
                    journal_line_id=ids[0],
                    matched_by=p.user_id,
                )
            )
            n += 1
    if n:
        audit.record(db, p, "auto_match", "fin.bank_txn", None, after={"matched": n})
    db.commit()
    return n


def book_from_statement(
    db: Session, p: Principal, txn_id: uuid.UUID, contra_id: uuid.UUID, memo: str | None
) -> dict[str, Any]:
    """Book a statement line that has no entry yet (bank charges, interest, a standing
    order) against another account, and match it at once."""
    p.require(WRITE)
    t = _txn(db, p, txn_id)
    if db.scalar(select(func.count()).where(BankMatch.bank_txn_id == t.id)):
        raise BusinessRule("This statement line is already matched")
    a = bank_account(db, p, t.account_id)
    contra = db.get(Account, contra_id)
    if contra is None or contra.company_id != p.company_id or contra.id == a.id:
        raise NotFound("Account not found")
    if contra.system_key in ledger.CONTROL:
        raise BusinessRule(
            "Customer and supplier money goes through a receipt or a payment, which then matches"
        )
    ledger.check_open(db, p.company_id, t.txn_date)
    amount = _signed(t)
    text_ = memo or t.description or "Bank statement"
    j = Journal(
        company_id=p.company_id,
        doc_no=next_doc_no(db, p.company_id, "JV"),
        journal_date=t.txn_date,
        source_type="JV",
        memo=f"Bank: {text_}"[:300],
        created_by=p.user_id,
    )
    bank_line = JournalLine(
        line_no=1,
        account_id=a.id,
        debit=max(amount, ZERO),
        credit=max(-amount, ZERO),
        memo=text_[:200],
    )
    j.lines.append(bank_line)
    j.lines.append(
        JournalLine(
            line_no=2,
            account_id=contra.id,
            debit=max(-amount, ZERO),
            credit=max(amount, ZERO),
            memo=text_[:200],
        )
    )
    db.add(j)
    db.flush()
    db.add(
        BankMatch(
            company_id=p.company_id,
            bank_txn_id=t.id,
            journal_line_id=bank_line.id,
            matched_by=p.user_id,
        )
    )
    audit.record(db, p, "create", "acc.journal", j.id, after={"doc_no": j.doc_no, "bank": True})
    db.commit()
    return {"journal_id": j.id, "doc_no": j.doc_no}


def reconciliation(
    db: Session,
    p: Principal,
    account_id: uuid.UUID | None,
    as_of: date,
    statement_balance: Decimal | None,
) -> dict[str, Any]:
    p.require(READ)
    ledger.sync(db, p.company_id)
    db.commit()
    a = bank_account(db, p, account_id)
    d, c = db.execute(
        select(
            func.coalesce(func.sum(JournalLine.debit), 0),
            func.coalesce(func.sum(JournalLine.credit), 0),
        )
        .join(Journal, Journal.id == JournalLine.journal_id)
        .where(JournalLine.account_id == a.id, Journal.journal_date <= as_of)
    ).one()
    book = d - c
    # a book line counts as through the bank by as_of when its statement line is dated by then
    through = (
        select(BankMatch.journal_line_id)
        .join(BankTxn, BankTxn.id == BankMatch.bank_txn_id)
        .where(BankTxn.txn_date <= as_of)
    )
    open_rows = db.execute(
        select(JournalLine, Journal)
        .join(Journal, Journal.id == JournalLine.journal_id)
        .where(
            JournalLine.account_id == a.id,
            Journal.journal_date <= as_of,
            JournalLine.id.not_in(through),
        )
        .order_by(Journal.journal_date)
    ).all()
    deposits = [_book_row(ln, j) for ln, j in open_rows if ln.debit > 0]
    payments = [_book_row(ln, j) for ln, j in open_rows if ln.credit > 0]
    dep = sum((r["amount"] for r in deposits), ZERO)
    pay = -sum((r["amount"] for r in payments), ZERO)
    unbooked = [
        r for r in statement(db, p, a.id, date_from=None, date_to=as_of, unmatched_only=True)
    ]
    expected = book - dep + pay
    return {
        "account": {"id": a.id, "code": a.code, "name": a.name},
        "as_of": as_of,
        "book_balance": book,
        "deposits_in_transit": dep,
        "unpresented_payments": pay,
        "expected_statement_balance": expected,
        "statement_balance": statement_balance,
        "difference": (statement_balance - expected) if statement_balance is not None else None,
        "deposits": deposits,
        "payments": payments,
        "unbooked_statement_lines": unbooked,
    }
