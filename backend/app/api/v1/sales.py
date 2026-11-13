"""/sales/{kind}: quotation, delivery-order, sales-invoice, customer-cn."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, Field

from app.api.deps import DB, CurrentUser
from app.api.redact import sensitive, shape
from app.services import sales as svc

router = APIRouter(prefix="/sales", tags=["sales"])

Kind = Literal["quotation", "sales-order", "delivery-order", "sales-invoice", "customer-cn"]
Status = Literal["DRAFT", "SAVED", "POSTED", "VOIDED"]
EXCLUDE_UNSET = {"response_model_exclude_unset": True}
# Header fields that only some kinds have.
ONLY_FOR = {
    "valid_until": "quotation",
    "delivery_date": "sales-order",
    "ref_invoice_id": "customer-cn",
    "reason": "customer-cn",
}


class LineIn(BaseModel):
    item_id: uuid.UUID
    uom: str = Field(min_length=1, max_length=10)
    qty: Decimal = Field(gt=0, max_digits=18, decimal_places=4)
    # Left out: the item's selling price for this unit (a return: the invoice price).
    unit_price: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=4)
    discount_pct: Decimal = Field(default=Decimal(0), ge=0, le=100, decimal_places=4)
    tax_code_id: uuid.UUID | None = None
    remark: str | None = Field(default=None, max_length=200)
    do_line_id: uuid.UUID | None = None  # sales-invoice only: invoicing a delivered DO line
    so_line_id: uuid.UUID | None = None  # DO / invoice: fulfilling a sales order line


class DocIn(BaseModel):
    doc_date: date
    customer_id: uuid.UUID
    location_id: uuid.UUID
    customer_ref_no: str | None = Field(default=None, max_length=50)
    remark: str | None = Field(default=None, max_length=500)
    valid_until: date | None = None
    delivery_date: date | None = None
    ref_invoice_id: uuid.UUID | None = None
    reason: str | None = Field(default=None, max_length=200)
    lines: list[LineIn] = Field(default=[], max_length=500)


class DocPatch(BaseModel):
    version: int = Field(ge=1)
    doc_date: date | None = None
    customer_id: uuid.UUID | None = None
    location_id: uuid.UUID | None = None
    customer_ref_no: str | None = Field(default=None, max_length=50)
    remark: str | None = Field(default=None, max_length=500)
    valid_until: date | None = None
    delivery_date: date | None = None
    ref_invoice_id: uuid.UUID | None = None
    reason: str | None = Field(default=None, max_length=200)
    lines: list[LineIn] | None = Field(default=None, max_length=500)


class PickIn(BaseModel):
    source_line_id: uuid.UUID
    qty: Decimal = Field(gt=0, max_digits=18, decimal_places=4)


class ConvertIn(BaseModel):
    source_kind: Kind
    source_id: uuid.UUID
    doc_date: date | None = None
    # delivery order -> invoice: the DO lines to invoice now (default: all that is left)
    lines: list[PickIn] | None = Field(default=None, max_length=500)


class VersionIn(BaseModel):
    version: int = Field(ge=1)


class VoidIn(VersionIn):
    reason: str = Field(min_length=3, max_length=300)


class LineOut(BaseModel):
    id: uuid.UUID
    line_no: int
    item_id: uuid.UUID
    uom: str
    qty: Decimal
    factor: Decimal
    base_qty: Decimal
    unit_price: Decimal
    price_source: str
    discount_pct: Decimal
    tax_code_id: uuid.UUID | None
    tax_rate: Decimal
    line_subtotal: Decimal
    line_tax: Decimal
    line_total: Decimal
    unit_cost: Decimal | None = sensitive("cost")
    remark: str | None
    do_line_id: uuid.UUID | None = None
    so_line_id: uuid.UUID | None = None
    # sales order, single document only: delivered or invoiced so far
    fulfilled_qty: Decimal | None = None
    # delivery order, single document only: how much of the line is invoiced already
    invoiced_qty: Decimal | None = None


class DocOut(BaseModel):
    id: uuid.UUID
    doc_no: str | None
    doc_date: date
    customer_id: uuid.UUID
    location_id: uuid.UUID
    status: str
    customer_ref_no: str | None
    remark: str | None
    currency: str
    subtotal: Decimal
    tax_total: Decimal
    total: Decimal
    cost_total: Decimal | None = sensitive("cost")
    gross_profit: Decimal | None = sensitive("cost")  # subtotal - cost, once posted
    valid_until: date | None = None
    delivery_date: date | None = None
    closed_at: datetime | None = None
    due_date: date | None = None
    ref_invoice_id: uuid.UUID | None = None
    reason: str | None = None
    source_kind: str | None
    source_id: uuid.UUID | None
    posted_at: datetime | None
    voided_at: datetime | None
    void_reason: str | None
    version: int
    lines: list[LineOut]
    warnings: list[dict[str, Any]] = []


class Page[T](BaseModel):
    items: list[T]
    total: int


def _out(
    doc,
    p,
    warnings: list[dict[str, Any]] | None = None,
    invoiced: dict[uuid.UUID, Decimal] | None = None,
    fulfilled: dict[uuid.UUID, Decimal] | None = None,
) -> DocOut:
    data = {c: getattr(doc, c) for c in DocOut.model_fields if hasattr(doc, c)}
    data["lines"] = [
        shape(
            LineOut,
            {c: getattr(ln, c) for c in LineOut.model_fields if hasattr(ln, c)}
            | ({"invoiced_qty": invoiced.get(ln.id, Decimal(0))} if invoiced is not None else {})
            | (
                {"fulfilled_qty": fulfilled.get(ln.id, Decimal(0))} if fulfilled is not None else {}
            ),
            p,
        )
        for ln in doc.lines
    ]
    # an order (or quotation) has no cost until goods go out on a DO or invoice
    booked = doc.status == "POSTED" and not hasattr(doc, "delivery_date")
    data["gross_profit"] = doc.subtotal - doc.cost_total if booked else None
    if warnings is not None:
        data["warnings"] = warnings
    return shape(DocOut, data, p)


def _values(body: BaseModel, kind: str) -> dict[str, Any]:
    v = body.model_dump(exclude_unset=True)
    for field, only in ONLY_FOR.items():
        if kind != only:
            v.pop(field, None)
    if v.get("lines") is not None:
        v["lines"] = [dict(ln) for ln in v["lines"]]
        for ln in v["lines"]:
            if kind != "sales-invoice":
                ln.pop("do_line_id", None)
            if kind not in ("delivery-order", "sales-invoice"):
                ln.pop("so_line_id", None)
    return v


@router.get("/{kind}", **EXCLUDE_UNSET)
def list_docs(
    kind: Kind,
    p: CurrentUser,
    db: DB,
    status: Status | None = None,
    customer_id: uuid.UUID | None = None,
    location_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    q: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Page[DocOut]:
    rows, total = svc.list_docs(
        db,
        p,
        kind,
        status=status,
        customer_id=customer_id,
        location_id=location_id,
        date_from=date_from,
        date_to=date_to,
        q=q,
        page=page,
        page_size=page_size,
    )
    return Page(items=[_out(d, p) for d in rows], total=total)


@router.get("/{kind}/{doc_id}", **EXCLUDE_UNSET)
def get_doc(kind: Kind, doc_id: uuid.UUID, p: CurrentUser, db: DB) -> DocOut:
    doc = svc.get_doc(db, p, kind, doc_id)
    invoiced = svc.invoiced_by_line(db, doc) if kind == "delivery-order" else None
    fulfilled = svc.fulfilled_by_line(db, doc) if kind == "sales-order" else None
    return _out(doc, p, invoiced=invoiced, fulfilled=fulfilled)


class CloseIn(VersionIn):
    reopen: bool = False


@router.post("/sales-order/{doc_id}/close", **EXCLUDE_UNSET)
def close_so(doc_id: uuid.UUID, body: CloseIn, p: CurrentUser, db: DB) -> DocOut:
    """Stop waiting for the rest of the sales order (reopen=true undoes it)."""
    doc = svc.close_so(db, p, doc_id, body.version, reopen=body.reopen)
    return _out(doc, p, fulfilled=svc.fulfilled_by_line(db, doc))


@router.post("/{kind}", status_code=201, **EXCLUDE_UNSET)
def create_doc(kind: Kind, body: DocIn, p: CurrentUser, db: DB) -> DocOut:
    v = _values(body, kind)
    v.setdefault("lines", [])
    return _out(svc.create_doc(db, p, kind, **v), p)


@router.post("/{kind}/convert", status_code=201, **EXCLUDE_UNSET)
def convert(kind: Kind, body: ConvertIn, p: CurrentUser, db: DB) -> DocOut:
    pick = [ln.model_dump() for ln in body.lines] if body.lines is not None else None
    return _out(svc.convert(db, p, kind, body.source_kind, body.source_id, body.doc_date, pick), p)


@router.patch("/{kind}/{doc_id}", **EXCLUDE_UNSET)
def update_doc(kind: Kind, doc_id: uuid.UUID, body: DocPatch, p: CurrentUser, db: DB) -> DocOut:
    v = _values(body, kind)
    version = v.pop("version")
    doc, warnings = svc.update_doc(db, p, kind, doc_id, version, **v)
    return _out(doc, p, warnings)


@router.delete("/{kind}/{doc_id}", status_code=204)
def delete_doc(kind: Kind, doc_id: uuid.UUID, p: CurrentUser, db: DB) -> Response:
    svc.delete_doc(db, p, kind, doc_id)
    return Response(status_code=204)


@router.post("/{kind}/{doc_id}/save", **EXCLUDE_UNSET)
def save_doc(kind: Kind, doc_id: uuid.UUID, body: VersionIn, p: CurrentUser, db: DB) -> DocOut:
    doc, warnings = svc.save_doc(db, p, kind, doc_id, body.version)
    return _out(doc, p, warnings)


@router.post("/{kind}/{doc_id}/post", **EXCLUDE_UNSET)
def post_doc(kind: Kind, doc_id: uuid.UUID, body: VersionIn, p: CurrentUser, db: DB) -> DocOut:
    return _out(svc.post_doc(db, p, kind, doc_id, body.version), p)


@router.post("/{kind}/{doc_id}/void", **EXCLUDE_UNSET)
def void_doc(kind: Kind, doc_id: uuid.UUID, body: VoidIn, p: CurrentUser, db: DB) -> DocOut:
    return _out(svc.void_doc(db, p, kind, doc_id, body.version, body.reason), p)
