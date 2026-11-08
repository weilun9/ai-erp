"""/inventory/{kind}: adjustment, write-off, transfer, stocktake."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, Field

from app.api.deps import DB, CurrentUser
from app.api.redact import sensitive, shape
from app.services import stockdocs as svc

router = APIRouter(prefix="/inventory", tags=["inventory"])

Kind = Literal["adjustment", "write-off", "transfer", "stocktake"]
Status = Literal["DRAFT", "SAVED", "POSTED", "VOIDED"]
EXCLUDE_UNSET = {"response_model_exclude_unset": True}
ONLY_FOR = {
    "to_location_id": "transfer",
    "zero_uncounted": "stocktake",
    "counted_at": "stocktake",
}


class LineIn(BaseModel):
    item_id: uuid.UUID
    uom: str = Field(min_length=1, max_length=10)
    # adjustment: + adds / - removes; stocktake: counted quantity
    qty: Decimal = Field(max_digits=18, decimal_places=4)
    unit_cost: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=4)
    reason: str | None = Field(default=None, max_length=20)
    remark: str | None = Field(default=None, max_length=200)


class DocIn(BaseModel):
    doc_date: date
    location_id: uuid.UUID
    to_location_id: uuid.UUID | None = None
    zero_uncounted: bool = False
    counted_at: datetime | None = None  # stocktake: when counted (default: on save)
    reason: str | None = Field(default=None, max_length=20)
    remark: str | None = Field(default=None, max_length=500)
    lines: list[LineIn] = Field(default=[], max_length=2000)


class DocPatch(BaseModel):
    version: int = Field(ge=1)
    doc_date: date | None = None
    location_id: uuid.UUID | None = None
    to_location_id: uuid.UUID | None = None
    zero_uncounted: bool | None = None
    counted_at: datetime | None = None
    reason: str | None = Field(default=None, max_length=20)
    remark: str | None = Field(default=None, max_length=500)
    lines: list[LineIn] | None = Field(default=None, max_length=2000)


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
    unit_cost: Decimal | None = sensitive("cost")
    reason: str | None
    remark: str | None
    system_qty: Decimal | None = None
    diff_qty: Decimal | None = None


class DocOut(BaseModel):
    id: uuid.UUID
    doc_no: str | None
    doc_date: date
    location_id: uuid.UUID
    to_location_id: uuid.UUID | None = None
    zero_uncounted: bool | None = None
    counted_at: datetime | None = None
    status: str
    reason: str | None
    remark: str | None
    cost_total: Decimal | None = sensitive("cost")
    posted_at: datetime | None
    voided_at: datetime | None
    void_reason: str | None
    version: int
    lines: list[LineOut]
    warnings: list[dict[str, Any]] = []


class Page[T](BaseModel):
    items: list[T]
    total: int


def _cols(model: type[BaseModel], obj) -> dict[str, Any]:
    return {c: getattr(obj, c) for c in model.model_fields if hasattr(obj, c)}


def _out(doc, p, warnings: list[dict[str, Any]] | None = None) -> DocOut:
    data = _cols(DocOut, doc)
    data["lines"] = [shape(LineOut, _cols(LineOut, ln), p) for ln in doc.lines]
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
    return v


@router.get("/{kind}", **EXCLUDE_UNSET)
def list_docs(
    kind: Kind,
    p: CurrentUser,
    db: DB,
    status: Status | None = None,
    location_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Page[DocOut]:
    rows, total = svc.list_docs(
        db,
        p,
        kind,
        status=status,
        location_id=location_id,
        date_from=date_from,
        date_to=date_to,
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
