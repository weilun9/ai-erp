"""/pos: the till. Shifts, sales, returns, shift reports."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

from app.api.deps import DB, CurrentUser
from app.api.redact import sensitive, shape
from app.services import pos as svc

router = APIRouter(prefix="/pos", tags=["pos"])
Method = Literal["CASH", "CARD", "EWALLET", "VOUCHER"]


class ShiftOpenIn(BaseModel):
    location_id: uuid.UUID
    opening_float: Decimal = Field(ge=0, max_digits=18, decimal_places=2)


class ShiftCloseIn(BaseModel):
    counted_cash: Decimal = Field(ge=0, max_digits=18, decimal_places=2)
    note: str | None = Field(default=None, max_length=300)


class ShiftOut(BaseModel):
    id: uuid.UUID
    shift_no: str
    location_id: uuid.UUID
    cashier_id: uuid.UUID
    status: str
    opening_float: Decimal
    opened_at: datetime
    expected_cash: Decimal | None
    counted_cash: Decimal | None
    variance: Decimal | None
    close_note: str | None
    closed_at: datetime | None


class Supervisor(BaseModel):
    """A supervisor keys in their own login at the till to approve an override."""

    username: str = Field(max_length=50)
    password: str = Field(max_length=200)


class SaleLineIn(BaseModel):
    barcode: str | None = Field(default=None, max_length=30)
    item_id: uuid.UUID | None = None
    uom: str | None = Field(default=None, max_length=10)  # default: the barcode's unit
    qty: Decimal = Field(gt=0, max_digits=18, decimal_places=4)
    unit_price: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=4)
    discount_pct: Decimal = Field(default=Decimal(0), ge=0, le=100, decimal_places=4)


class PaymentIn(BaseModel):
    method: Method
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)  # cash: tendered
    reference: str | None = Field(default=None, max_length=60)


class SaleIn(BaseModel):
    client_uuid: uuid.UUID  # made by the till; sending it again returns the same sale
    lines: list[SaleLineIn] = Field(min_length=1, max_length=300)
    payments: list[PaymentIn] = Field(min_length=1, max_length=5)
    customer_id: uuid.UUID | None = None
    sold_at: datetime | None = None  # offline sales: when it happened
    supervisor: Supervisor | None = None


class ReturnLineIn(BaseModel):
    line_no: int = Field(ge=1)
    qty: Decimal = Field(gt=0, max_digits=18, decimal_places=4)


class ReturnIn(BaseModel):
    client_uuid: uuid.UUID
    receipt_no: str = Field(max_length=30)
    lines: list[ReturnLineIn] = Field(min_length=1, max_length=300)
    refund_method: Method | None = None  # default: how it was paid
    reason: str | None = Field(default=None, max_length=200)
    supervisor: Supervisor | None = None


class LineOut(BaseModel):
    line_no: int
    item_id: uuid.UUID
    barcode: str | None
    uom: str
    qty: Decimal
    unit_price: Decimal
    list_price: Decimal | None
    price_source: str
    discount_pct: Decimal
    tax_rate: Decimal
    line_subtotal: Decimal
    line_tax: Decimal
    line_total: Decimal
    unit_cost: Decimal | None = sensitive("cost")


class PaymentOut(BaseModel):
    method: str
    amount: Decimal
    reference: str | None


class SaleOut(BaseModel):
    id: uuid.UUID
    kind: str
    receipt_no: str
    client_uuid: uuid.UUID
    shift_id: uuid.UUID
    location_id: uuid.UUID
    customer_id: uuid.UUID | None
    ref_sale_id: uuid.UUID | None
    sold_at: datetime
    subtotal: Decimal
    tax_total: Decimal
    rounding: Decimal
    total: Decimal
    tendered: Decimal
    change: Decimal
    cost_total: Decimal | None = sensitive("cost")
    approved_by: uuid.UUID | None
    reason: str | None
    lines: list[LineOut]
    payments: list[PaymentOut]
    replayed: bool = False  # true = this client_uuid was already recorded


def _shift(s) -> ShiftOut:
    return ShiftOut.model_validate(s, from_attributes=True)


def _sale(s, p, replayed: bool = False) -> SaleOut:
    data = {k: getattr(s, k) for k in SaleOut.model_fields if hasattr(s, k)}
    data["lines"] = [
        shape(LineOut, {k: getattr(x, k) for k in LineOut.model_fields}, p) for x in s.lines
    ]
    data["payments"] = [PaymentOut.model_validate(x, from_attributes=True) for x in s.payments]
    data["replayed"] = replayed
    return shape(SaleOut, data, p)


EXCLUDE_UNSET = {"response_model_exclude_unset": True}


@router.post("/shifts", status_code=201)
def open_shift(body: ShiftOpenIn, p: CurrentUser, db: DB) -> ShiftOut:
    return _shift(svc.open_shift(db, p, body.location_id, body.opening_float))


@router.get("/shifts/current")
def current_shift(p: CurrentUser, db: DB) -> ShiftOut | None:
    """The open shift on this till, if any."""
    p.require("pos.sale.create")
    s = svc.current_shift(db, p)
    return _shift(s) if s else None


@router.get("/shifts")
def list_shifts(
    p: CurrentUser, db: DB, status: Literal["OPEN", "CLOSED"] | None = None
) -> list[ShiftOut]:
    return [_shift(s) for s in svc.list_shifts(db, p, status=status)]


@router.get("/shifts/{shift_id}/report")
def shift_report(shift_id: uuid.UUID, p: CurrentUser, db: DB) -> dict[str, Any]:
    return svc.shift_report(db, p, shift_id)


@router.post("/shifts/{shift_id}/close")
def close_shift(shift_id: uuid.UUID, body: ShiftCloseIn, p: CurrentUser, db: DB) -> ShiftOut:
    return _shift(svc.close_shift(db, p, shift_id, body.counted_cash, body.note))


@router.post("/sales", status_code=201, **EXCLUDE_UNSET)
def sell(body: SaleIn, p: CurrentUser, db: DB, response: Response) -> SaleOut:
    sale, replayed = svc.sell(db, p, _plain(body))
    if replayed:
        response.status_code = 200
    return _sale(sale, p, replayed)


@router.post("/returns", status_code=201, **EXCLUDE_UNSET)
def refund(body: ReturnIn, p: CurrentUser, db: DB, response: Response) -> SaleOut:
    sale, replayed = svc.refund(db, p, _plain(body))
    if replayed:
        response.status_code = 200
    return _sale(sale, p, replayed)


@router.get("/sales/{receipt_no}", **EXCLUDE_UNSET)
def get_sale(receipt_no: str, p: CurrentUser, db: DB) -> SaleOut:
    return _sale(svc.get_sale(db, p, receipt_no=receipt_no), p)


@router.get("/tax-summary")
def tax_summary(p: CurrentUser, db: DB, date_from: date, date_to: date) -> dict[str, Any]:
    """POS takings by tax code: basis for the monthly consolidated e-invoice and SST."""
    return svc.tax_summary(db, p, date_from, date_to)


def _plain(body: BaseModel) -> dict[str, Any]:
    v = body.model_dump()
    v["lines"] = [{k: x for k, x in ln.items() if x is not None} for ln in v["lines"]]
    return v
