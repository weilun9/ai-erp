"""/promotions and /members: till promotions and loyalty members."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import DB, CurrentUser
from app.services import loyalty
from app.services import promotions as svc

router = APIRouter(tags=["promotions"])
Kind = Literal["PERCENT", "AMOUNT", "PRICE", "BUY_GET", "BUNDLE"]


class PromoIn(BaseModel):
    code: str = Field(min_length=1, max_length=30, pattern=r"^\S+$")
    name: str = Field(min_length=1, max_length=100)
    kind: Kind
    value: Decimal = Field(default=Decimal(0), ge=0, max_digits=18, decimal_places=4)
    buy_qty: int = Field(default=0, ge=0, le=1000)
    get_qty: int = Field(default=0, ge=0, le=1000)
    date_from: date
    date_to: date
    members_only: bool = False
    item_ids: list[uuid.UUID] = Field(default_factory=list, max_length=500)
    category_ids: list[uuid.UUID] = Field(default_factory=list, max_length=200)
    brand_ids: list[uuid.UUID] = Field(default_factory=list, max_length=200)
    location_ids: list[uuid.UUID] = Field(default_factory=list, max_length=200)


class PromoPatch(BaseModel):
    version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    value: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=4)
    buy_qty: int | None = Field(default=None, ge=0, le=1000)
    get_qty: int | None = Field(default=None, ge=0, le=1000)
    date_from: date | None = None
    date_to: date | None = None
    members_only: bool | None = None
    is_active: bool | None = None
    item_ids: list[uuid.UUID] | None = Field(default=None, max_length=500)
    category_ids: list[uuid.UUID] | None = Field(default=None, max_length=200)
    brand_ids: list[uuid.UUID] | None = Field(default=None, max_length=200)
    location_ids: list[uuid.UUID] | None = Field(default=None, max_length=200)


class PromoOut(PromoIn):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    is_active: bool
    version: int


@router.get("/promotions")
def list_promotions(p: CurrentUser, db: DB, active_on: date | None = None) -> list[PromoOut]:
    return [PromoOut.model_validate(x) for x in svc.list_promotions(db, p, active_on=active_on)]


@router.post("/promotions", status_code=201)
def create_promotion(body: PromoIn, p: CurrentUser, db: DB) -> PromoOut:
    return PromoOut.model_validate(svc.create_promotion(db, p, **body.model_dump()))


@router.patch("/promotions/{promo_id}")
def update_promotion(promo_id: uuid.UUID, body: PromoPatch, p: CurrentUser, db: DB) -> PromoOut:
    v = body.model_dump(exclude_unset=True)
    version = v.pop("version")
    return PromoOut.model_validate(svc.update_promotion(db, p, promo_id, version, **v))


# --- loyalty members ---------------------------------------------------------------------------


class MemberIn(BaseModel):
    card_no: str = Field(min_length=1, max_length=30)
    name: str = Field(min_length=1, max_length=100)
    phone: str | None = Field(default=None, max_length=30)
    email: str | None = Field(default=None, max_length=100)


class MemberPatch(BaseModel):
    version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    phone: str | None = Field(default=None, max_length=30)
    email: str | None = Field(default=None, max_length=100)
    is_active: bool | None = None


class MemberOut(MemberIn):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    points: int
    is_active: bool
    joined_at: datetime
    version: int


class AdjustIn(BaseModel):
    points: int = Field(ge=-1_000_000, le=1_000_000)
    note: str = Field(min_length=1, max_length=200)


class MoveOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    kind: str
    points: int
    balance: int
    sale_id: uuid.UUID | None
    note: str | None
    created_at: datetime


@router.get("/members")
def list_members(p: CurrentUser, db: DB, q: str | None = None) -> list[MemberOut]:
    return [MemberOut.model_validate(m) for m in loyalty.list_members(db, p, q)]


@router.get("/members/lookup")
def lookup_member(card: str, p: CurrentUser, db: DB) -> MemberOut:
    """The till: a scanned card or a typed phone number."""
    return MemberOut.model_validate(loyalty.find(db, p, card))


@router.get("/members/settings")
def loyalty_settings(p: CurrentUser) -> dict[str, Decimal]:
    p.require("pos.sale.create")
    return {"earn_per_rm": loyalty.earn_rate(), "point_value": loyalty.point_value()}


@router.post("/members", status_code=201)
def create_member(body: MemberIn, p: CurrentUser, db: DB) -> MemberOut:
    return MemberOut.model_validate(loyalty.create(db, p, **body.model_dump()))


@router.patch("/members/{member_id}")
def update_member(member_id: uuid.UUID, body: MemberPatch, p: CurrentUser, db: DB) -> MemberOut:
    v = body.model_dump(exclude_unset=True)
    version = v.pop("version")
    return MemberOut.model_validate(loyalty.update(db, p, member_id, version, **v))


@router.post("/members/{member_id}/adjust")
def adjust_points(member_id: uuid.UUID, body: AdjustIn, p: CurrentUser, db: DB) -> MemberOut:
    return MemberOut.model_validate(loyalty.adjust(db, p, member_id, body.points, body.note))


@router.get("/members/{member_id}/points")
def points_history(member_id: uuid.UUID, p: CurrentUser, db: DB) -> list[MoveOut]:
    return [MoveOut.model_validate(x) for x in loyalty.history(db, p, member_id)]
