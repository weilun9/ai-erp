"""/finance: receipts, supplier payments, fees, knock-offs, open items, statements.
/issues: problem documents raised when something does not reconcile."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, Field

from app.api.deps import DB, CurrentUser
from app.core.errors import BadRequest
from app.services import finance as svc
from app.services import knockoff as ko

router = APIRouter(prefix="/finance", tags=["finance"])
issues_router = APIRouter(prefix="/issues", tags=["issues"])

Kind = Literal["receipt", "supplier-payment"]
Status = Literal["DRAFT", "SAVED", "POSTED", "VOIDED"]
Method = Literal["CASH", "CHEQUE", "TRANSFER", "CARD", "EWALLET", "OTHER"]
PartyType = Literal["customer", "supplier"]
DocCode = Literal["INV", "CN", "OR", "GRN", "SCN", "FEE", "PV"]
Amount = Annotated[Decimal, Field(max_digits=18, decimal_places=4)]
PARTY_FIELD = {"receipt": "customer_id", "supplier-payment": "supplier_id"}


class LineIn(BaseModel):
    # receipt
    invoice_id: uuid.UUID | None = None
    # supplier payment
    line_type: Literal["INVOICE", "CN", "FEE", "OTHER"] | None = None
    ref_no: str | None = Field(default=None, max_length=50)
    fee_type: Literal["FREIGHT", "LISTING", "PROMOTION", "DISPLAY", "REBATE", "OTHER"] | None = None
    tax_code_id: uuid.UUID | None = None
    remark: str | None = Field(default=None, max_length=200)
    amount: Amount


class DocIn(BaseModel):
    doc_date: date
    customer_id: uuid.UUID | None = None
    supplier_id: uuid.UUID | None = None
    location_id: uuid.UUID
    method: Method = "TRANSFER"
    bank_ref: str | None = Field(default=None, max_length=50)
    amount: Amount | None = None  # receipt only; a payment's amount is worked out
    auto_allocate: bool = True  # receipt only
    supplier_ref_no: str | None = Field(default=None, max_length=50)  # payment only
    remark: str | None = Field(default=None, max_length=500)
    lines: list[LineIn] = Field(default=[], max_length=500)


class DocPatch(BaseModel):
    version: int = Field(ge=1)
    doc_date: date | None = None
    customer_id: uuid.UUID | None = None
    supplier_id: uuid.UUID | None = None
    location_id: uuid.UUID | None = None
    method: Method | None = None
    bank_ref: str | None = Field(default=None, max_length=50)
    amount: Amount | None = None
    auto_allocate: bool | None = None
    supplier_ref_no: str | None = Field(default=None, max_length=50)
    remark: str | None = Field(default=None, max_length=500)
    lines: list[LineIn] | None = Field(default=None, max_length=500)


class VersionIn(BaseModel):
    version: int = Field(ge=1)


class VoidIn(VersionIn):
    reason: str = Field(min_length=3, max_length=300)


class LineOut(BaseModel):
    id: uuid.UUID
    line_no: int
    amount: Decimal
    invoice_id: uuid.UUID | None = None
    line_type: str | None = None
    ref_no: str | None = None
    fee_type: str | None = None
    tax_code_id: uuid.UUID | None = None
    matched_type: str | None = None
    matched_id: uuid.UUID | None = None
    matched_no: str | None = None
    match_status: str | None = None
    remark: str | None = None


class DocOut(BaseModel):
    id: uuid.UUID
    doc_no: str | None
    doc_date: date
    customer_id: uuid.UUID | None = None
    supplier_id: uuid.UUID | None = None
    location_id: uuid.UUID
    status: str
    method: str
    bank_ref: str | None
    amount: Decimal
    unallocated: Decimal | None = None  # posted: money not yet knocked off (advance)
    auto_allocate: bool | None = None
    supplier_ref_no: str | None = None
    remark: str | None
    currency: str
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


def _out(db, doc, warnings=None, unallocated=None) -> DocOut:
    data = _cols(DocOut, doc)
    data["lines"] = [LineOut(**_cols(LineOut, ln)) for ln in doc.lines]
    if doc.status == "POSTED":
        data["unallocated"] = (
            unallocated if unallocated is not None else svc.open_amounts(db, [doc])[doc.id]
        )
    if warnings is not None:
        data["warnings"] = warnings
    return DocOut(**data)


def _values(body: BaseModel, kind: str) -> dict[str, Any]:
    v = body.model_dump(exclude_unset=True)
    other = "supplier_id" if kind == "receipt" else "customer_id"
    v.pop(other, None)
    if kind == "receipt":
        v.pop("supplier_ref_no", None)
        if v.get("lines") is not None:
            v["lines"] = [
                {"invoice_id": ln["invoice_id"], "amount": ln["amount"]} for ln in v["lines"]
            ]
            if any(ln["invoice_id"] is None for ln in v["lines"]):
                raise BadRequest("Receipt lines need an invoice")
    else:
        v.pop("amount", None)
        v.pop("auto_allocate", None)
        if v.get("lines") is not None:
            if any(ln.get("line_type") is None for ln in v["lines"]):
                raise BadRequest("Payment lines need a line type")
    return v


# --- knock-off, balances, fees (static paths first) -----------------------------------------


class KnockIn(BaseModel):
    debit_type: DocCode
    debit_id: uuid.UUID
    credit_type: DocCode
    credit_id: uuid.UUID
    amount: Amount | None = None  # default: as much as both documents allow


class KnockOut(BaseModel):
    id: uuid.UUID
    party_type: str
    party_id: uuid.UUID
    debit_type: str
    debit_id: uuid.UUID
    debit_no: str
    credit_type: str
    credit_id: uuid.UUID
    credit_no: str
    amount: Decimal
    source_type: str | None
    source_id: uuid.UUID | None
    created_at: datetime
    voided_at: datetime | None


@router.get("/knockoffs")
def list_knockoffs(doc_id: uuid.UUID, p: CurrentUser, db: DB) -> list[KnockOut]:
    return [KnockOut(**_cols(KnockOut, k)) for k in ko.list_knockoffs(db, p, doc_id)]


@router.post("/knockoffs", status_code=201)
def knock(body: KnockIn, p: CurrentUser, db: DB) -> KnockOut:
    k = ko.manual_knock(
        db, p, body.debit_type, body.debit_id, body.credit_type, body.credit_id, body.amount
    )
    return KnockOut(**_cols(KnockOut, k))


@router.delete("/knockoffs/{knockoff_id}")
def undo_knock(knockoff_id: uuid.UUID, p: CurrentUser, db: DB) -> KnockOut:
    return KnockOut(**_cols(KnockOut, ko.undo_knock(db, p, knockoff_id)))


class OpenItem(BaseModel):
    doc_type: str
    id: uuid.UUID
    doc_no: str
    doc_date: date
    due_date: date | None
    side: str  # D owed to us / by us, C credit (payment, CN, advance)
    amount: Decimal
    settled: Decimal
    open: Decimal


class OpenItems(BaseModel):
    items: list[OpenItem]
    balance: Decimal


@router.get("/open-items")
def open_items(party_type: PartyType, party_id: uuid.UUID, p: CurrentUser, db: DB) -> OpenItems:
    pt = ko.PARTY_TYPES[party_type]
    items = ko.open_items(db, p, pt, party_id)
    return OpenItems(
        items=[OpenItem(**i) for i in items],
        balance=ko.party_balance(db, p.company_id, pt, party_id),
    )


class StatementLine(BaseModel):
    doc_date: date
    doc_type: str
    id: uuid.UUID
    doc_no: str
    debit: Decimal
    credit: Decimal
    balance: Decimal


class Statement(BaseModel):
    opening: Decimal
    lines: list[StatementLine]
    closing: Decimal


@router.get("/statement")
def statement(
    party_type: PartyType,
    party_id: uuid.UUID,
    p: CurrentUser,
    db: DB,
    date_from: date | None = None,
    date_to: date | None = None,
) -> Statement:
    return Statement(
        **ko.statement(db, p, ko.PARTY_TYPES[party_type], party_id, date_from, date_to)
    )


class FeeOut(BaseModel):
    id: uuid.UUID
    doc_no: str
    doc_date: date
    supplier_id: uuid.UUID
    fee_type: str
    amount: Decimal
    tax_code_id: uuid.UUID | None
    tax_amount: Decimal
    payment_id: uuid.UUID
    status: str


@router.get("/supplier-fees")
def list_fees(p: CurrentUser, db: DB, supplier_id: uuid.UUID | None = None) -> list[FeeOut]:
    return [FeeOut(**_cols(FeeOut, f)) for f in svc.list_fees(db, p, supplier_id)]


# --- receipts and payments ----------------------------------------------------------------


@router.get("/{kind}")
def list_docs(
    kind: Kind,
    p: CurrentUser,
    db: DB,
    status: Status | None = None,
    party_id: uuid.UUID | None = None,
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
        party_id=party_id,
        date_from=date_from,
        date_to=date_to,
        page=page,
        page_size=page_size,
    )
    left = svc.open_amounts(db, [d for d in rows if d.status == "POSTED"])
    return Page(items=[_out(db, d, unallocated=left.get(d.id)) for d in rows], total=total)


@router.get("/{kind}/{doc_id}")
def get_doc(kind: Kind, doc_id: uuid.UUID, p: CurrentUser, db: DB) -> DocOut:
    return _out(db, svc.get_doc(db, p, kind, doc_id))


@router.post("/{kind}", status_code=201)
def create_doc(kind: Kind, body: DocIn, p: CurrentUser, db: DB) -> DocOut:
    v = _values(body, kind)
    if v.get(PARTY_FIELD[kind]) is None:
        raise BadRequest(f"{PARTY_FIELD[kind]} is required")
    if kind == "receipt" and v.get("amount") is None:
        raise BadRequest("amount is required")
    v.setdefault("lines", [])
    return _out(db, svc.create_doc(db, p, kind, **v))


@router.patch("/{kind}/{doc_id}")
def update_doc(kind: Kind, doc_id: uuid.UUID, body: DocPatch, p: CurrentUser, db: DB) -> DocOut:
    v = _values(body, kind)
    version = v.pop("version")
    doc, warnings = svc.update_doc(db, p, kind, doc_id, version, **v)
    return _out(db, doc, warnings)


@router.delete("/{kind}/{doc_id}", status_code=204)
def delete_doc(kind: Kind, doc_id: uuid.UUID, p: CurrentUser, db: DB) -> Response:
    svc.delete_doc(db, p, kind, doc_id)
    return Response(status_code=204)


@router.post("/{kind}/{doc_id}/save")
def save_doc(kind: Kind, doc_id: uuid.UUID, body: VersionIn, p: CurrentUser, db: DB) -> DocOut:
    doc, warnings = svc.save_doc(db, p, kind, doc_id, body.version)
    return _out(db, doc, warnings)


@router.post("/{kind}/{doc_id}/post")
def post_doc(kind: Kind, doc_id: uuid.UUID, body: VersionIn, p: CurrentUser, db: DB) -> DocOut:
    return _out(db, svc.post_doc(db, p, kind, doc_id, body.version))


@router.post("/{kind}/{doc_id}/void")
def void_doc(kind: Kind, doc_id: uuid.UUID, body: VoidIn, p: CurrentUser, db: DB) -> DocOut:
    return _out(db, svc.void_doc(db, p, kind, doc_id, body.version, body.reason))


# --- problem documents --------------------------------------------------------------------


class IssueOut(BaseModel):
    id: uuid.UUID
    issue_type: str
    status: str
    title: str
    doc_type: str
    doc_id: uuid.UUID
    doc_no: str | None
    party_type: str | None
    party_id: uuid.UUID | None
    details: dict[str, Any]
    created_at: datetime
    resolved_at: datetime | None
    resolution: str | None


class ResolveIn(BaseModel):
    resolution: str = Field(min_length=3, max_length=500)


@issues_router.get("")
def list_issues(
    p: CurrentUser,
    db: DB,
    status: Literal["OPEN", "RESOLVED", "CANCELLED"] | None = None,
    doc_id: uuid.UUID | None = None,
) -> list[IssueOut]:
    return [
        IssueOut(**_cols(IssueOut, i)) for i in ko.list_issues(db, p, status=status, doc_id=doc_id)
    ]


@issues_router.post("/{issue_id}/resolve")
def resolve_issue(issue_id: uuid.UUID, body: ResolveIn, p: CurrentUser, db: DB) -> IssueOut:
    return IssueOut(**_cols(IssueOut, ko.resolve_issue(db, p, issue_id, body.resolution)))
