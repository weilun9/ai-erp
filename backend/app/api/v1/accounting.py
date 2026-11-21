"""/accounting: chart of accounts, journals (from documents + manual vouchers) and the
financial statements. Every read first books any posted document that has no journal yet."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import DB, CurrentUser
from app.services import bankrec
from app.services import ledger as svc

router = APIRouter(prefix="/accounting", tags=["accounting"])

AccountType = Literal["ASSET", "LIABILITY", "EQUITY", "INCOME", "EXPENSE"]


class AccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    code: str
    name: str
    type: str
    system_key: str | None
    is_active: bool
    version: int


class AccountIn(BaseModel):
    code: str = Field(min_length=1, max_length=20, pattern=r"^\S+$")
    name: str = Field(min_length=1, max_length=100)
    type: AccountType


class AccountPatch(BaseModel):
    version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    is_active: bool | None = None


class JournalLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    line_no: int
    account_id: uuid.UUID
    debit: Decimal
    credit: Decimal
    party_type: str | None
    party_id: uuid.UUID | None
    memo: str | None


class JournalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    doc_no: str
    journal_date: date
    source_type: str
    source_id: uuid.UUID | None
    source_no: str | None
    location_id: uuid.UUID | None
    memo: str | None
    is_reversal: bool
    status: str
    voided_at: datetime | None
    void_reason: str | None
    lines: list[JournalLineOut]


class Page[T](BaseModel):
    items: list[T]
    total: int


class ManualLine(BaseModel):
    account_id: uuid.UUID
    debit: Decimal = Field(default=Decimal(0), ge=0, max_digits=18, decimal_places=2)
    credit: Decimal = Field(default=Decimal(0), ge=0, max_digits=18, decimal_places=2)
    memo: str | None = Field(default=None, max_length=200)


class ManualIn(BaseModel):
    journal_date: date
    memo: str = Field(min_length=1, max_length=300)
    lines: list[ManualLine] = Field(min_length=2, max_length=200)


class VoidIn(BaseModel):
    reason: str = Field(min_length=3, max_length=300)


@router.get("/accounts")
def list_accounts(p: CurrentUser, db: DB) -> Page[AccountOut]:
    rows = svc.list_accounts(db, p)
    return Page(items=[AccountOut.model_validate(a) for a in rows], total=len(rows))


@router.post("/accounts", status_code=201)
def create_account(body: AccountIn, p: CurrentUser, db: DB) -> AccountOut:
    return AccountOut.model_validate(svc.create_account(db, p, **body.model_dump()))


@router.patch("/accounts/{account_id}")
def update_account(account_id: uuid.UUID, body: AccountPatch, p: CurrentUser, db: DB) -> AccountOut:
    v = body.model_dump(exclude_unset=True)
    version = v.pop("version")
    return AccountOut.model_validate(svc.update_account(db, p, account_id, version, **v))


@router.post("/sync")
def sync(p: CurrentUser, db: DB) -> dict[str, int]:
    """Book every posted / voided document that has no journal yet (reads do this too)."""
    return {"journals_made": svc.run_sync(db, p)}


@router.get("/journals")
def list_journals(
    p: CurrentUser,
    db: DB,
    date_from: date | None = None,
    date_to: date | None = None,
    source_type: str | None = None,
    q: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Page[JournalOut]:
    rows, total = svc.list_journals(
        db,
        p,
        date_from=date_from,
        date_to=date_to,
        source_type=source_type,
        q=q,
        page=page,
        page_size=page_size,
    )
    return Page(items=[JournalOut.model_validate(j) for j in rows], total=total)


@router.get("/journals/{journal_id}")
def get_journal(journal_id: uuid.UUID, p: CurrentUser, db: DB) -> JournalOut:
    return JournalOut.model_validate(svc.get_journal(db, p, journal_id))


@router.post("/journals", status_code=201)
def create_manual(body: ManualIn, p: CurrentUser, db: DB) -> JournalOut:
    """A manual journal voucher (JV): must balance; AR, AP and inventory are off limits."""
    v = body.model_dump()
    return JournalOut.model_validate(svc.create_manual(db, p, **v))


@router.post("/journals/{journal_id}/void")
def void_manual(journal_id: uuid.UUID, body: VoidIn, p: CurrentUser, db: DB) -> JournalOut:
    return JournalOut.model_validate(svc.void_manual(db, p, journal_id, body.reason))


@router.get("/trial-balance")
def trial_balance(p: CurrentUser, db: DB, as_of: date) -> dict[str, Any]:
    return svc.trial_balance(db, p, as_of)


@router.get("/profit-and-loss")
def profit_and_loss(p: CurrentUser, db: DB, date_from: date, date_to: date) -> dict[str, Any]:
    return svc.profit_and_loss(db, p, date_from, date_to)


@router.get("/balance-sheet")
def balance_sheet(p: CurrentUser, db: DB, as_of: date) -> dict[str, Any]:
    return svc.balance_sheet(db, p, as_of)


@router.get("/ledger/{account_id}")
def account_ledger(
    account_id: uuid.UUID, p: CurrentUser, db: DB, date_from: date, date_to: date
) -> dict[str, Any]:
    return svc.account_ledger(db, p, account_id, date_from, date_to)


# --- bank reconciliation -----------------------------------------------------------------------


class StatementLineIn(BaseModel):
    txn_date: date
    amount: Decimal = Field(max_digits=18, decimal_places=2)  # > 0 money in, < 0 money out
    description: str | None = Field(default=None, max_length=300)
    reference: str | None = Field(default=None, max_length=100)
    counterparty: str | None = Field(default=None, max_length=200)


class StatementIn(BaseModel):
    account_id: uuid.UUID | None = None  # None = the default bank account
    lines: list[StatementLineIn] = Field(min_length=1, max_length=2000)


class MatchIn(BaseModel):
    journal_line_ids: list[uuid.UUID] = Field(min_length=1, max_length=200)


class BookIn(BaseModel):
    account_id: uuid.UUID  # the other side: bank charges, interest received...
    memo: str | None = Field(default=None, max_length=200)


@router.get("/bank/accounts")
def bank_accounts(p: CurrentUser, db: DB) -> list[AccountOut]:
    """Asset accounts that can be reconciled (the default Bank first)."""
    p.require("acc.read")
    not_bank = ("AR", "INVENTORY", "INPUT_TAX")
    rows = [
        a
        for a in svc.list_accounts(db, p)
        if a.type == "ASSET" and a.is_active and a.system_key not in not_bank
    ]
    rows.sort(key=lambda a: (a.system_key != "BANK", a.code))
    return [AccountOut.model_validate(a) for a in rows]


@router.post("/bank/statement", status_code=201)
def add_statement(body: StatementIn, p: CurrentUser, db: DB) -> dict[str, int]:
    return bankrec.add_lines(db, p, body.account_id, [x.model_dump() for x in body.lines])


@router.get("/bank/statement")
def get_statement(
    p: CurrentUser,
    db: DB,
    account_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    unmatched_only: bool = False,
) -> list[dict[str, Any]]:
    return bankrec.statement(
        db, p, account_id, date_from=date_from, date_to=date_to, unmatched_only=unmatched_only
    )


@router.get("/bank/book")
def get_book(
    p: CurrentUser, db: DB, account_id: uuid.UUID | None = None, date_to: date | None = None
) -> list[dict[str, Any]]:
    return bankrec.book_lines(db, p, account_id, date_to=date_to)


@router.get("/bank/statement/{txn_id}/suggest")
def suggest(txn_id: uuid.UUID, p: CurrentUser, db: DB) -> list[dict[str, Any]]:
    return bankrec.suggest(db, p, txn_id)


@router.post("/bank/statement/{txn_id}/match", status_code=204)
def match(txn_id: uuid.UUID, body: MatchIn, p: CurrentUser, db: DB) -> None:
    bankrec.match(db, p, txn_id, body.journal_line_ids)


@router.post("/bank/statement/{txn_id}/unmatch", status_code=204)
def unmatch(txn_id: uuid.UUID, p: CurrentUser, db: DB) -> None:
    bankrec.unmatch(db, p, txn_id)


@router.delete("/bank/statement/{txn_id}", status_code=204)
def delete_statement_line(txn_id: uuid.UUID, p: CurrentUser, db: DB) -> None:
    bankrec.delete_line(db, p, txn_id)


@router.post("/bank/statement/{txn_id}/book", status_code=201)
def book_statement_line(txn_id: uuid.UUID, body: BookIn, p: CurrentUser, db: DB) -> dict[str, Any]:
    return bankrec.book_from_statement(db, p, txn_id, body.account_id, body.memo)


@router.post("/bank/auto-match")
def auto_match(p: CurrentUser, db: DB, account_id: uuid.UUID | None = None) -> dict[str, int]:
    return {"matched": bankrec.auto_match(db, p, account_id)}


@router.get("/bank/reconciliation")
def reconciliation(
    p: CurrentUser,
    db: DB,
    as_of: date,
    account_id: uuid.UUID | None = None,
    statement_balance: Decimal | None = None,
) -> dict[str, Any]:
    return bankrec.reconciliation(db, p, account_id, as_of, statement_balance)


# --- year-end close ----------------------------------------------------------------------------


class CloseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    year_end: date
    journal_id: uuid.UUID
    net_profit: Decimal
    closed_at: datetime
    reopened_at: datetime | None


class CloseIn(BaseModel):
    year_end: date


@router.get("/year-end")
def list_year_ends(p: CurrentUser, db: DB) -> dict[str, Any]:
    closes = svc.list_closes(db, p)
    return {
        "closed_through": svc.closed_through(db, p.company_id),
        "closes": [CloseOut.model_validate(c) for c in closes],
    }


@router.post("/year-end", status_code=201)
def close_year(body: CloseIn, p: CurrentUser, db: DB) -> CloseOut:
    return CloseOut.model_validate(svc.close_year(db, p, body.year_end))


@router.post("/year-end/reopen")
def reopen_year(p: CurrentUser, db: DB) -> CloseOut:
    return CloseOut.model_validate(svc.reopen_year(db, p))
