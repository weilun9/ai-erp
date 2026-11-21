"""/lots: batch / expiry and serial numbers - what is where, what expires, and the trace."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import or_, select

from app.api.deps import DB, CurrentUser
from app.core.errors import NotFound
from app.localization.my import business_date
from app.models import Item, Location, Lot, LotBalance, LotMove
from app.services import lots as svc

router = APIRouter(prefix="/lots", tags=["lots"])
PERMISSION = "inv.balance.read"


class LotIn(BaseModel):
    """One batch or serial number on a document line (quantity in base units)."""

    lot_no: str = Field(min_length=1, max_length=60)
    expiry_date: date | None = None
    qty: Decimal = Field(gt=0, max_digits=18, decimal_places=4)


class LotRow(BaseModel):
    lot_id: uuid.UUID
    item_id: uuid.UUID
    item_code: str
    item_name: str
    kind: str
    lot_no: str
    expiry_date: date | None
    days_left: int | None
    location_id: uuid.UUID
    location_code: str
    qty: Decimal


class LotPage(BaseModel):
    items: list[LotRow]
    total: int


@router.get("")
def list_lots(
    p: CurrentUser,
    db: DB,
    item_id: uuid.UUID | None = None,
    location_id: uuid.UUID | None = None,
    q: Annotated[str | None, Query(max_length=60)] = None,  # lot / serial no, item code/name
    status: Literal["in_stock", "expiring", "expired", "all"] = "in_stock",
    days: Annotated[int, Query(ge=1, le=3650)] = 30,  # "expiring" = within this many days
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=500)] = 100,
) -> LotPage:
    """Lots per store, earliest expiry first. Stores you may not see are left out."""
    p.require(PERMISSION)
    today = business_date()
    where = [Lot.company_id == p.company_id]
    if item_id:
        where.append(Lot.item_id == item_id)
    if location_id:
        where.append(LotBalance.location_id == location_id)
    if q:
        like = f"%{q.strip()}%"
        where.append(or_(Lot.lot_no.ilike(like), Item.code.ilike(like), Item.name.ilike(like)))
    if status != "all":
        where.append(LotBalance.qty > 0)
    if status == "expired":
        where.append(Lot.expiry_date < today)
    elif status == "expiring":
        where.append(Lot.expiry_date >= today)
        where.append(Lot.expiry_date <= date.fromordinal(today.toordinal() + days))
    rows = db.execute(
        select(Lot, Item.code, Item.name, LotBalance.location_id, Location.code, LotBalance.qty)
        .join(LotBalance, LotBalance.lot_id == Lot.id)
        .join(Item, Item.id == Lot.item_id)
        .join(Location, Location.id == LotBalance.location_id)
        .where(*where)
        .order_by(Lot.expiry_date.asc().nulls_last(), Item.code, Lot.lot_no, Location.code)
    ).all()
    visible = [r for r in rows if p.can_see_location(r[3])]
    start = (page - 1) * page_size
    items = [
        LotRow(
            lot_id=lot.id,
            item_id=lot.item_id,
            item_code=code,
            item_name=name,
            kind=lot.kind,
            lot_no=lot.lot_no,
            expiry_date=lot.expiry_date,
            days_left=(lot.expiry_date - today).days if lot.expiry_date else None,
            location_id=loc_id,
            location_code=loc_code,
            qty=qty,
        )
        for lot, code, name, loc_id, loc_code, qty in visible[start : start + page_size]
    ]
    return LotPage(items=items, total=len(visible))


class TraceMove(BaseModel):
    at: datetime
    doc_type: str
    doc_id: uuid.UUID
    doc_no: str
    location_code: str
    qty: Decimal
    is_reversal: bool


class LotTrace(BaseModel):
    lot_id: uuid.UUID
    item_id: uuid.UUID
    item_code: str
    item_name: str
    kind: str
    lot_no: str
    expiry_date: date | None
    moves: list[TraceMove]


@router.get("/{lot_id}/trace")
def trace(lot_id: uuid.UUID, p: CurrentUser, db: DB) -> LotTrace:
    """Every document that brought this batch / serial in or took it out, oldest first
    (a recall: which customers got batch X; a warranty: who bought serial Y)."""
    p.require(PERMISSION)
    lot = db.get(Lot, lot_id)
    if lot is None or lot.company_id != p.company_id:
        raise NotFound("Batch / serial not found")
    item = db.get(Item, lot.item_id)
    moves = db.execute(
        select(LotMove, Location.code)
        .join(Location, Location.id == LotMove.location_id)
        .where(LotMove.lot_id == lot_id)
        .order_by(LotMove.id)
    ).all()
    return LotTrace(
        lot_id=lot.id,
        item_id=lot.item_id,
        item_code=item.code,
        item_name=item.name,
        kind=lot.kind,
        lot_no=lot.lot_no,
        expiry_date=lot.expiry_date,
        moves=[
            TraceMove(
                at=m.posted_at,
                doc_type=m.doc_type,
                doc_id=m.doc_id,
                doc_no=m.doc_no,
                location_code=code,
                qty=m.qty,
                is_reversal=m.is_reversal,
            )
            for m, code in moves
            if p.can_see_location(m.location_id)
        ],
    )


@router.get("/doc/{doc_type}/{doc_id}")
def doc_lots(doc_type: str, doc_id: uuid.UUID, p: CurrentUser, db: DB) -> dict[str, list[dict]]:
    """line id -> the lots a posted document actually moved (incl. ones picked by expiry)."""
    p.require(PERMISSION)
    first = db.scalar(select(LotMove).where(LotMove.doc_type == doc_type, LotMove.doc_id == doc_id))
    if first is not None and first.company_id != p.company_id:
        raise NotFound("Document not found")
    return {str(k): v for k, v in svc.lot_summary(db, doc_type, doc_id).items()}
