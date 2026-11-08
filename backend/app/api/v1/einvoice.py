"""/einvoice: LHDN MyInvois for sales invoices (INV) and customer credit notes (CN)."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.deps import DB, CurrentUser
from app.services import einvoice as svc

router = APIRouter(prefix="/einvoice", tags=["einvoice"])

DocType = Literal["INV", "CN"]


class EinvoiceOut(BaseModel):
    id: uuid.UUID
    doc_type: str
    doc_id: uuid.UUID
    doc_no: str
    type_code: str
    version: str
    environment: str
    status: str
    submission_uid: str | None
    uuid: str | None
    long_id: str | None
    share_url: str | None  # print as the QR code on the invoice
    errors: list[Any]
    last_error: str | None
    created_at: datetime
    submitted_at: datetime | None
    validated_at: datetime | None
    cancelled_at: datetime | None
    cancel_reason: str | None


def _out(row) -> EinvoiceOut:
    data = {c: getattr(row, c) for c in EinvoiceOut.model_fields if hasattr(row, c)}
    data["share_url"] = svc.link(row)
    return EinvoiceOut(**data)


class Preview(BaseModel):
    document: dict[str, Any]
    problems: list[dict[str, str]]  # LHDN would reject: fix first
    warnings: list[dict[str, str]]


class PendingDoc(BaseModel):
    doc_type: str
    id: uuid.UUID
    doc_no: str
    doc_date: date
    customer_id: uuid.UUID
    total: Decimal


@router.get("")
def list_einvoices(
    p: CurrentUser,
    db: DB,
    doc_id: uuid.UUID | None = None,
    status: Literal["PENDING", "SUBMITTED", "VALID", "INVALID", "FAILED", "CANCELLED"]
    | None = None,
) -> list[EinvoiceOut]:
    return [_out(r) for r in svc.list_einvoices(db, p, doc_id=doc_id, status=status)]


@router.get("/pending")
def pending(p: CurrentUser, db: DB) -> list[PendingDoc]:
    return [PendingDoc(**d) for d in svc.pending(db, p)]


@router.get("/preview/{doc_type}/{doc_id}")
def preview(doc_type: DocType, doc_id: uuid.UUID, p: CurrentUser, db: DB) -> Preview:
    return Preview(**svc.preview(db, p, doc_type, doc_id))


class SubmitIn(BaseModel):
    doc_type: DocType
    doc_id: uuid.UUID


@router.post("/submit")
def submit(body: SubmitIn, p: CurrentUser, db: DB) -> EinvoiceOut:
    return _out(svc.submit(db, p, body.doc_type, body.doc_id))


@router.get("/{einvoice_id}")
def get(einvoice_id: uuid.UUID, p: CurrentUser, db: DB) -> EinvoiceOut:
    return _out(svc.get(db, p, einvoice_id))


@router.post("/{einvoice_id}/refresh")
def refresh(einvoice_id: uuid.UUID, p: CurrentUser, db: DB) -> EinvoiceOut:
    return _out(svc.refresh(db, p, einvoice_id))


class CancelIn(BaseModel):
    reason: str = Field(min_length=3, max_length=300)


@router.post("/{einvoice_id}/cancel")
def cancel(einvoice_id: uuid.UUID, body: CancelIn, p: CurrentUser, db: DB) -> EinvoiceOut:
    return _out(svc.cancel(db, p, einvoice_id, body.reason))
