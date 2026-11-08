import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api.deps import DB, CurrentUser
from app.api.redact import sensitive, shape
from app.core.errors import NotFound
from app.models import Item, StockBalance, StockLedger

router = APIRouter(prefix="/stock", tags=["stock"])
EXCLUDE_UNSET = {"response_model_exclude_unset": True}


class BalanceOut(BaseModel):
    item_id: uuid.UUID
    item_code: str
    item_name: str
    base_uom: str
    location_id: uuid.UUID
    qty: Decimal
    avg_cost: Decimal | None = sensitive("cost")


class LedgerOut(BaseModel):
    id: int
    posted_at: datetime
    item_id: uuid.UUID
    location_id: uuid.UUID
    doc_type: str
    doc_id: uuid.UUID
    doc_no: str
    qty_in: Decimal
    qty_out: Decimal
    balance_qty: Decimal
    unit_cost: Decimal | None = sensitive("cost")
    avg_cost: Decimal | None = sensitive("cost")
    is_reversal: bool


class Page[T](BaseModel):
    items: list[T]
    total: int


def _scope(stmt, model, p):
    stmt = stmt.where(model.company_id == p.company_id)
    if p.location_ids is not None:
        stmt = stmt.where(model.location_id.in_(p.location_ids))
    return stmt


@router.get("/balance", **EXCLUDE_UNSET)
def balance(
    p: CurrentUser,
    db: DB,
    item_id: uuid.UUID | None = None,
    location_id: uuid.UUID | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=500)] = 100,
) -> Page[BalanceOut]:
    p.require("inv.balance.read")
    stmt = _scope(
        select(StockBalance, Item).join(Item, Item.id == StockBalance.item_id), StockBalance, p
    )
    if item_id:
        stmt = stmt.where(StockBalance.item_id == item_id)
    if location_id:
        if not p.can_see_location(location_id):
            raise NotFound("Location not found")
        stmt = stmt.where(StockBalance.location_id == location_id)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.execute(
        stmt.order_by(Item.code, StockBalance.location_id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return Page(
        items=[
            shape(
                BalanceOut,
                {
                    "item_id": b.item_id,
                    "item_code": i.code,
                    "item_name": i.name,
                    "base_uom": i.base_uom,
                    "location_id": b.location_id,
                    "qty": b.qty,
                    "avg_cost": b.avg_cost,
                },
                p,
            )
            for b, i in rows
        ],
        total=total,
    )


@router.get("/ledger", **EXCLUDE_UNSET)
def ledger(
    p: CurrentUser,
    db: DB,
    item_id: uuid.UUID,
    location_id: uuid.UUID | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=500)] = 100,
) -> Page[LedgerOut]:
    p.require("inv.balance.read")
    stmt = _scope(select(StockLedger), StockLedger, p).where(StockLedger.item_id == item_id)
    if location_id:
        stmt = stmt.where(StockLedger.location_id == location_id)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(
        stmt.order_by(StockLedger.id.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()
    return Page(items=[shape(LedgerOut, r, p) for r in rows], total=total)
