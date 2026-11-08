"""/purchase/{kind}: kind is `grn` (goods received / supplier invoice) or `supplier-cn`."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, Field

from app.api.deps import DB, CurrentUser
from app.api.redact import sensitive, shape
from app.services import purchasing as svc

router = APIRouter(prefix="/purchase", tags=["purchasing"])

Kind = Literal["grn", "supplier-cn"]
Status = Literal["DRAFT", "SAVED", "POSTED", "VOIDED"]
Completeness = Literal["RECORDED", "PROVISIONAL", "COMPLETE", "DISPUTED"]
EXCLUDE_UNSET = {"response_model_exclude_unset": True}


class LineIn(BaseModel):
    item_id: uuid.UUID
    uom: str = Field(min_length=1, max_length=10)
    qty: Decimal = Field(gt=0, max_digits=18, decimal_places=4)
    unit_price: Decimal = Field(ge=0, max_digits=18, decimal_places=4)
    discount_pct: Decimal = Field(default=Decimal(0), ge=0, le=100, decimal_places=4)
    tax_code_id: uuid.UUID | None = None
    remark: str | None = Field(default=None, max_length=200)


class DocIn(BaseModel):
    doc_date: date
    supplier_id: uuid.UUID
    location_id: uuid.UUID
    supplier_ref_no: str | None = Field(default=None, max_length=50)
    remark: str | None = Field(default=None, max_length=500)
    # supplier-cn only
    completeness: Completeness | None = None
    ref_invoice_no: str | None = Field(default=None, max_length=50)
    lines: list[LineIn] = Field(default=[], max_length=500)


class DocPatch(BaseModel):
    version: int = Field(ge=1)
    doc_date: date | None = None
    supplier_id: uuid.UUID | None = None
    location_id: uuid.UUID | None = None
    supplier_ref_no: str | None = Field(default=None, max_length=50)
    remark: str | None = Field(default=None, max_length=500)
    completeness: Completeness | None = None
    ref_invoice_no: str | None = Field(default=None, max_length=50)
    lines: list[LineIn] | None = Field(default=None, max_length=500)


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
    unit_price: Decimal | None = sensitive("cost")
    discount_pct: Decimal
    tax_code_id: uuid.UUID | None
    tax_rate: Decimal
    line_subtotal: Decimal | None = sensitive("cost")
    line_tax: Decimal | None = sensitive("cost")
    line_total: Decimal | None = sensitive("cost")
    unit_cost: Decimal | None = sensitive("cost")
    remark: str | None


class DocOut(BaseModel):
    id: uuid.UUID
    doc_no: str | None
    doc_date: date
    supplier_id: uuid.UUID
    location_id: uuid.UUID
    status: str
    supplier_ref_no: str | None
    remark: str | None
    currency: str
    subtotal: Decimal | None = sensitive("cost")
    tax_total: Decimal | None = sensitive("cost")
    total: Decimal | None = sensitive("cost")
    completeness: str | None = None
    ref_invoice_no: str | None = None
    posted_at: datetime | None
    voided_at: datetime | None
    void_reason: str | None
    version: int
    lines: list[LineOut]
    warnings: list[dict[str, Any]] = []


class Page[T](BaseModel):
    items: list[T]
    total: int


def _out(doc, p, warnings: list[dict[str, Any]] | None = None) -> DocOut:
    extra: dict[str, Any] = {"lines": [shape(LineOut, ln, p) for ln in doc.lines]}
    if hasattr(doc, "completeness"):
        extra["completeness"] = doc.completeness
        extra["ref_invoice_no"] = doc.ref_invoice_no
    if warnings is not None:
        extra["warnings"] = warnings
    return shape(
        DocOut,
        {c: getattr(doc, c) for c in DocOut.model_fields if hasattr(doc, c)},
        p,
        **extra,
    )


def _values(body: BaseModel, kind: str) -> dict[str, Any]:
    v = body.model_dump(exclude_unset=True)
    if kind != "supplier-cn":
        for f in ("completeness", "ref_invoice_no"):
            v.pop(f, None)
    if "lines" in v and v["lines"] is not None:
        v["lines"] = [dict(ln) for ln in v["lines"]]
    return v


@router.get("/{kind}", **EXCLUDE_UNSET)
def list_docs(
    kind: Kind,
    p: CurrentUser,
    db: DB,
    status: Status | None = None,
    supplier_id: uuid.UUID | None = None,
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
        supplier_id=supplier_id,
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
    return _out(svc.get_doc(db, p, kind, doc_id), p)


@router.post("/{kind}", status_code=201, **EXCLUDE_UNSET)
def create_doc(kind: Kind, body: DocIn, p: CurrentUser, db: DB) -> DocOut:
    v = _values(body, kind)
    v.setdefault("lines", [])
    return _out(svc.create_doc(db, p, kind, **v), p)


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
