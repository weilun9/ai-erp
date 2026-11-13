"""/supplier-notes/{kind}: `debit-note`, `credit-adjustment` (credit note without goods) and
`refund` (money back from the supplier). None of them moves stock."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, Field

from app.api.deps import DB, CurrentUser
from app.services import supnotes as svc

router = APIRouter(prefix="/supplier-notes", tags=["supplier notes"])

Kind = Literal["debit-note", "credit-adjustment", "refund"]
Status = Literal["DRAFT", "SAVED", "POSTED", "VOIDED"]
Method = Literal["CASH", "CHEQUE", "TRANSFER", "CARD", "EWALLET", "OTHER"]
Amount = Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=2)]
EXCLUDE_UNSET = {"response_model_exclude_unset": True}


class LineIn(BaseModel):
    # notes
    description: str | None = Field(default=None, min_length=1, max_length=200)
    ref_doc_no: str | None = Field(default=None, max_length=50)
    tax_code_id: uuid.UUID | None = None
    # refund: which supplier credit it settles
    doc_type: Literal["SCN", "SCA", "PV"] | None = None
    doc_id: uuid.UUID | None = None
    amount: Amount


class DocIn(BaseModel):
    doc_date: date
    supplier_id: uuid.UUID
    location_id: uuid.UUID
    supplier_ref_no: str | None = Field(default=None, max_length=50)
    remark: str | None = Field(default=None, max_length=500)
    # refund only
    amount: Amount | None = None
    method: Method | None = None
    bank_ref: str | None = Field(default=None, max_length=50)
    lines: list[LineIn] = Field(default=[], max_length=200)


class DocPatch(BaseModel):
    version: int = Field(ge=1)
    doc_date: date | None = None
    supplier_id: uuid.UUID | None = None
    location_id: uuid.UUID | None = None
    supplier_ref_no: str | None = Field(default=None, max_length=50)
    remark: str | None = Field(default=None, max_length=500)
    amount: Amount | None = None
    method: Method | None = None
    bank_ref: str | None = Field(default=None, max_length=50)
    lines: list[LineIn] | None = Field(default=None, max_length=200)


class VersionIn(BaseModel):
    version: int = Field(ge=1)


class VoidIn(VersionIn):
    reason: str = Field(min_length=3, max_length=300)


class LineOut(BaseModel):
    id: uuid.UUID
    line_no: int
    description: str | None = None
    ref_doc_no: str | None = None
    tax_code_id: uuid.UUID | None = None
    tax_rate: Decimal | None = None
    line_tax: Decimal | None = None
    line_total: Decimal | None = None
    doc_type: str | None = None
    doc_id: uuid.UUID | None = None
    doc_no: str | None = None
    amount: Decimal


class DocOut(BaseModel):
    id: uuid.UUID
    kind: str
    doc_no: str | None
    doc_date: date
    supplier_id: uuid.UUID
    location_id: uuid.UUID
    status: str
    supplier_ref_no: str | None
    remark: str | None
    subtotal: Decimal | None = None
    tax_total: Decimal | None = None
    total: Decimal | None = None
    amount: Decimal | None = None
    method: str | None = None
    bank_ref: str | None = None
    open: Decimal | None = None  # posted only: what is not knocked off yet
    posted_at: datetime | None
    voided_at: datetime | None
    void_reason: str | None
    version: int
    lines: list[LineOut]
    warnings: list[dict[str, Any]] = []


class Page[T](BaseModel):
    items: list[T]
    total: int


def _out(kind: str, doc, warnings=None, open_amount=None) -> DocOut:
    v = {c: getattr(doc, c) for c in DocOut.model_fields if hasattr(doc, c)}
    v["kind"] = kind
    v["lines"] = [
        LineOut(**{c: getattr(ln, c) for c in LineOut.model_fields if hasattr(ln, c)})
        for ln in doc.lines
    ]
    if warnings is not None:
        v["warnings"] = warnings
    if open_amount is not None:
        v["open"] = open_amount
    return DocOut(**v)


def _values(body: BaseModel, kind: str) -> dict[str, Any]:
    v = body.model_dump(exclude_unset=True)
    if kind != "refund":
        for f in ("amount", "method", "bank_ref"):
            v.pop(f, None)
    lines = v.get("lines")
    if lines is not None:
        for n, ln in enumerate(lines, start=1):
            if kind == "refund" and not (ln.get("doc_type") and ln.get("doc_id")):
                raise_bad(f"Line {n}: name the CN, credit note or payment refunded")
            if kind != "refund" and not ln.get("description"):
                raise_bad(f"Line {n}: describe what the amount is for")
    return v


def raise_bad(msg: str):
    from app.core.errors import BadRequest

    raise BadRequest(msg)


@router.get("/{kind}")
def list_docs(
    kind: Kind,
    p: CurrentUser,
    db: DB,
    status: Status | None = None,
    supplier_id: uuid.UUID | None = None,
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
        date_from=date_from,
        date_to=date_to,
        q=q,
        page=page,
        page_size=page_size,
    )
    return Page(items=[_out(kind, d) for d in rows], total=total)


@router.get("/{kind}/{doc_id}")
def get_doc(kind: Kind, doc_id: uuid.UUID, p: CurrentUser, db: DB) -> DocOut:
    doc = svc.get_doc(db, p, kind, doc_id)
    return _out(kind, doc, open_amount=svc.open_amount(db, kind, doc))


@router.post("/{kind}", status_code=201)
def create_doc(kind: Kind, body: DocIn, p: CurrentUser, db: DB) -> DocOut:
    v = _values(body, kind)
    v.setdefault("lines", [])
    return _out(kind, svc.create_doc(db, p, kind, **v))


@router.patch("/{kind}/{doc_id}")
def update_doc(kind: Kind, doc_id: uuid.UUID, body: DocPatch, p: CurrentUser, db: DB) -> DocOut:
    v = _values(body, kind)
    version = v.pop("version")
    doc, warnings = svc.update_doc(db, p, kind, doc_id, version, **v)
    return _out(kind, doc, warnings)


@router.delete("/{kind}/{doc_id}", status_code=204)
def delete_doc(kind: Kind, doc_id: uuid.UUID, p: CurrentUser, db: DB) -> Response:
    svc.delete_doc(db, p, kind, doc_id)
    return Response(status_code=204)


@router.post("/{kind}/{doc_id}/save")
def save_doc(kind: Kind, doc_id: uuid.UUID, body: VersionIn, p: CurrentUser, db: DB) -> DocOut:
    doc, warnings = svc.save_doc(db, p, kind, doc_id, body.version)
    return _out(kind, doc, warnings)


@router.post("/{kind}/{doc_id}/post")
def post_doc(kind: Kind, doc_id: uuid.UUID, body: VersionIn, p: CurrentUser, db: DB) -> DocOut:
    doc = svc.post_doc(db, p, kind, doc_id, body.version)
    return _out(kind, doc, open_amount=svc.open_amount(db, kind, doc))


@router.post("/{kind}/{doc_id}/void")
def void_doc(kind: Kind, doc_id: uuid.UUID, body: VoidIn, p: CurrentUser, db: DB) -> DocOut:
    return _out(kind, svc.void_doc(db, p, kind, doc_id, body.version, body.reason))
