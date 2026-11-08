"""E-invoice flow for posted sales invoices and customer credit notes.

preview  -> build the UBL document and list what LHDN would reject (nothing is sent)
submit   -> send it; SUBMITTED with LHDN's UUID, or INVALID / FAILED with the reason
refresh  -> ask LHDN for the result: VALID (with the share / QR link) or INVALID
cancel   -> within 72 hours of validation; after that a credit note is the only way

A document has at most one active e-invoice (PENDING / SUBMITTED / VALID). A sales
invoice or CN with an active e-invoice cannot be voided until the e-invoice is cancelled.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import BusinessRule, DocState, NotFound
from app.models import CustomerCn, Einvoice, PosConsolidation, SalesInvoice
from app.models.einvoice import ACTIVE
from app.services import audit, pos_consolidation, sales
from app.services.auth import Principal
from app.services.einvoice_ubl import TYPE_CODES, VERSION, build, build_consolidated
from app.services.myinvois import MyInvoisError, encode, get_client, share_url

KINDS = {"INV": ("sales-invoice", SalesInvoice), "CN": ("customer-cn", CustomerCn)}
CANCEL_WINDOW = timedelta(hours=72)


def _kind(doc_type: str):
    try:
        return KINDS[doc_type]
    except KeyError as exc:
        raise NotFound(
            "E-invoices are for sales invoices (INV), credit notes (CN) and monthly "
            "consolidated POS documents (CON)"
        ) from exc


def _load(db: Session, p: Principal, doc_type: str, doc_id: uuid.UUID):
    if doc_type == "CON":
        return pos_consolidation.get(db, p, doc_id)
    kind, _ = _kind(doc_type)
    return sales.get_doc(db, p, kind, doc_id)


def _not_ready(doc_type: str, doc) -> str | None:
    if doc_type == "CON":
        return None if doc.status == "OPEN" else "Released: its receipts were freed"
    return None if doc.status == "POSTED" else "Only posted documents are sent"


def _build(db: Session, p: Principal, doc_type: str, doc, now=None):
    if doc_type == "CON":
        return build_consolidated(db, p.company_id, doc, pos_consolidation.of(db, doc), now=now)
    return build(db, p.company_id, doc_type, doc, original=_original(db, doc_type, doc))


def active_for(db: Session, doc_id: uuid.UUID) -> Einvoice | None:
    return db.scalar(select(Einvoice).where(Einvoice.doc_id == doc_id, Einvoice.status.in_(ACTIVE)))


def _original(db: Session, doc_type: str, doc) -> tuple[str, str] | None:
    if doc_type != "CN" or not doc.ref_invoice_id:
        return None
    inv = db.get(SalesInvoice, doc.ref_invoice_id)
    e = active_for(db, inv.id)
    return (inv.doc_no, e.uuid) if e is not None and e.status == "VALID" else None


def preview(db: Session, p: Principal, doc_type: str, doc_id: uuid.UUID) -> dict[str, Any]:
    doc = _load(db, p, doc_type, doc_id)
    document, problems, warnings = _build(db, p, doc_type, doc)
    if (why := _not_ready(doc_type, doc)) is not None:
        problems.insert(0, {"field": "status", "message": why})
    if doc_type == "CON":
        warnings += pos_consolidation.reminders(doc.period_start)
    return {"document": document, "problems": problems, "warnings": warnings}


def _client():
    client = get_client()
    if client is None:
        raise BusinessRule(
            "MyInvois is not set up",
            {"settings": ["MYINVOIS_ENV", "MYINVOIS_CLIENT_ID", "MYINVOIS_CLIENT_SECRET"]},
        )
    return client


def submit(db: Session, p: Principal, doc_type: str, doc_id: uuid.UUID) -> Einvoice:
    p.require("sal.einvoice.submit")
    client = _client()
    doc = _load(db, p, doc_type, doc_id)
    if (why := _not_ready(doc_type, doc)) is not None:
        raise DocState(why, {"status": doc.status})
    if active_for(db, doc.id) is not None:
        raise DocState("This document already has an e-invoice")
    document, problems, warnings = _build(db, p, doc_type, doc)
    if problems:
        raise BusinessRule("Fix these before sending to LHDN", {"problems": problems})
    enc = encode(document)
    row = Einvoice(
        company_id=p.company_id,
        doc_type=doc_type,
        doc_id=doc.id,
        doc_no=doc.doc_no,
        type_code=TYPE_CODES[doc_type],
        version=VERSION,
        environment=client.environment,
        status="PENDING",
        payload=document,
        document_hash=enc.document_hash,
        attempts=1,
        created_by=p.user_id,
    )
    db.add(row)
    try:
        db.commit()  # the partial unique index stops a second sender here
    except IntegrityError as exc:
        db.rollback()
        raise DocState("This document is already being sent") from exc

    try:
        resp = client.submit([(doc.doc_no, enc)])
    except MyInvoisError as exc:
        row.status = "FAILED"
        row.last_error = str(exc)[:500]
        row.errors = [{"status": exc.status, "body": exc.body}]
    else:
        row.submission_uid = resp.get("submissionUID") or resp.get("submissionUid")
        ok = [
            d
            for d in resp.get("acceptedDocuments") or []
            if d.get("invoiceCodeNumber") == doc.doc_no
        ]
        bad = [
            d
            for d in resp.get("rejectedDocuments") or []
            if d.get("invoiceCodeNumber") == doc.doc_no
        ]
        if ok:
            row.status, row.uuid = "SUBMITTED", ok[0]["uuid"]
            row.submitted_at = datetime.now(UTC)
        else:
            row.status = "INVALID"
            row.errors = [d.get("error") for d in bad] or [{"message": "Not in the reply"}]
    audit.record(
        db,
        p,
        "einvoice_submit",
        "sal.einvoice",
        row.id,
        after={"doc_no": row.doc_no, "status": row.status, "uuid": row.uuid},
    )
    db.commit()
    return row


def get(db: Session, p: Principal, einvoice_id: uuid.UUID, *, lock: bool = False) -> Einvoice:
    p.require("sal.doc.read")
    stmt = select(Einvoice).where(Einvoice.id == einvoice_id, Einvoice.company_id == p.company_id)
    if lock:
        stmt = stmt.with_for_update()
    row = db.scalar(stmt.execution_options(populate_existing=True))
    if row is None:
        raise NotFound("E-invoice not found")
    return row


def _when(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


def refresh(db: Session, p: Principal, einvoice_id: uuid.UUID) -> Einvoice:
    """Ask LHDN how the document stands (validation takes a few seconds after submit)."""
    p.require("sal.einvoice.submit")
    row = get(db, p, einvoice_id, lock=True)
    if row.status not in ("SUBMITTED", "VALID") or not row.uuid:
        return row
    try:
        det = _client().details(row.uuid)
    except MyInvoisError as exc:
        raise BusinessRule("Could not read the result from MyInvois", {"error": str(exc)}) from exc
    status = (det.get("status") or "").lower()
    before = row.status
    if status == "valid":
        row.status = "VALID"
        row.long_id = det.get("longId") or row.long_id
        row.validated_at = _when(det.get("dateTimeValidated")) or datetime.now(UTC)
    elif status == "invalid":
        row.status = "INVALID"
        steps = (det.get("validationResults") or {}).get("validationSteps") or []
        row.errors = [s for s in steps if (s.get("status") or "").lower() == "invalid"]
    elif status == "cancelled":
        row.status = "CANCELLED"
        row.cancelled_at = _when(det.get("cancelDateTime")) or datetime.now(UTC)
        row.cancel_reason = row.cancel_reason or det.get("documentStatusReason")
        _release(db, p, row, "cancelled at LHDN")
    if row.status != before:
        audit.record(
            db,
            p,
            "einvoice_status",
            "sal.einvoice",
            row.id,
            before={"status": before},
            after={"status": row.status},
        )
    db.commit()
    return row


def cancel(db: Session, p: Principal, einvoice_id: uuid.UUID, reason: str) -> Einvoice:
    p.require("sal.einvoice.submit")
    row = get(db, p, einvoice_id, lock=True)
    if row.status != "VALID":
        raise DocState("Only a validated e-invoice can be cancelled", {"status": row.status})
    if row.validated_at and datetime.now(UTC) - row.validated_at > CANCEL_WINDOW:
        raise BusinessRule(
            "More than 72 hours since LHDN validated it; issue a credit note instead",
            {"validated_at": row.validated_at.isoformat()},
        )
    if row.doc_type == "INV":
        notes = db.scalars(
            select(Einvoice.doc_no)
            .join(CustomerCn, CustomerCn.id == Einvoice.doc_id)
            .where(CustomerCn.ref_invoice_id == row.doc_id, Einvoice.status.in_(ACTIVE))
        ).all()
        if notes:
            raise DocState(
                "Cancel the credit note e-invoices that refer to it first", {"documents": notes}
            )
    try:
        _client().cancel(row.uuid, reason)
    except MyInvoisError as exc:
        raise BusinessRule(
            "MyInvois did not cancel it", {"error": str(exc), "reply": exc.body}
        ) from exc
    row.status, row.cancelled_at, row.cancel_reason = "CANCELLED", datetime.now(UTC), reason
    _release(db, p, row, reason)
    audit.record(db, p, "einvoice_cancel", "sal.einvoice", row.id, after={"reason": reason})
    db.commit()
    return row


def _release(db: Session, p: Principal, row: Einvoice, why: str) -> None:
    """A cancelled consolidated e-invoice frees its POS receipts for a new one."""
    if row.doc_type == "CON":
        con = pos_consolidation.get(db, p, row.doc_id, lock=True)
        pos_consolidation.release(db, con, p, why)


def list_einvoices(
    db: Session, p: Principal, *, doc_id: uuid.UUID | None = None, status: str | None = None
) -> list[Einvoice]:
    p.require("sal.doc.read")
    stmt = select(Einvoice).where(Einvoice.company_id == p.company_id)
    if doc_id:
        stmt = stmt.where(Einvoice.doc_id == doc_id)
    if status:
        stmt = stmt.where(Einvoice.status == status)
    return list(db.scalars(stmt.order_by(Einvoice.created_at.desc()).limit(500)))


def pending(db: Session, p: Principal) -> list[dict[str, Any]]:
    """Posted invoices and credit notes that have no active e-invoice yet."""
    p.require("sal.doc.read")
    out = []
    for doc_type, (_, model) in KINDS.items():
        sent = select(Einvoice.doc_id).where(Einvoice.status.in_(ACTIVE))
        rows = db.scalars(
            select(model)
            .where(
                model.company_id == p.company_id,
                model.status == "POSTED",
                model.id.not_in(sent),
            )
            .order_by(model.doc_date, model.doc_no)
        )
        out += [
            {
                "doc_type": doc_type,
                "id": d.id,
                "doc_no": d.doc_no,
                "doc_date": d.doc_date,
                "customer_id": d.customer_id,
                "total": d.total,
            }
            for d in rows
        ]
    sent = select(Einvoice.doc_id).where(Einvoice.status.in_(ACTIVE))
    for con in db.scalars(
        select(PosConsolidation).where(
            PosConsolidation.company_id == p.company_id,
            PosConsolidation.status == "OPEN",
            PosConsolidation.id.not_in(sent),
        )
    ):
        out.append(
            {
                "doc_type": "CON",
                "id": con.id,
                "doc_no": con.doc_no,
                "doc_date": con.period_end,
                "customer_id": None,
                "total": con.total,
            }
        )
    return out


def link(row: Einvoice) -> str | None:
    return share_url(row.environment, row.uuid, row.long_id) if row.uuid and row.long_id else None
