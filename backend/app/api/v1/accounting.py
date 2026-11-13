"""/accounting: chart of accounts, journals (from documents + manual vouchers) and the
financial statements. Every read first books any posted document that has no journal yet."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import DB, CurrentUser
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
