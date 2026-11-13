"""Supplier money documents that move no stock (DynaMod's supplier module):

- debit note (SDN): the supplier charges more - raises what we owe, paid like an invoice;
- credit adjustment (SCA): a supplier credit note with no goods back (price adjustment,
  rebate, short delivery) - lowers what we owe; goods returns stay supplier CNs (SCN);
- refund (SRF): money the supplier pays back - settles their CNs / credit adjustments /
  our overpayment (an open payment). Lines name the credits; none = oldest first.

Same lifecycle as every document: DRAFT -> SAVED (numbered) -> POSTED -> VOIDED.
Notes are knocked off later by payments (or by hand); a refund knocks off when posted.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.core.errors import BadRequest, BusinessRule, DocState, NotFound, VersionConflict
from app.models import (
    Location,
    Supplier,
    SupplierCreditAdj,
    SupplierCreditAdjLine,
    SupplierDebitNote,
    SupplierDebitNoteLine,
    SupplierRefund,
    SupplierRefundLine,
    TaxCode,
)
from app.services import audit
from app.services import knockoff as ko
from app.services.auth import Principal
from app.services.doclib import EDITABLE, HUNDRED, q2, today
from app.services.numbering import next_doc_no

ZERO = Decimal(0)


@dataclass(frozen=True)
class Kind:
    key: str
    header: type
    line: type | None
    code: str  # knock-off / numbering code
    perm: str  # pur / fin
    entity: str


KINDS = {
    k.key: k
    for k in (
        Kind(
            "debit-note", SupplierDebitNote, SupplierDebitNoteLine, "SDN", "pur", "pur.supplier_dn"
        ),
        Kind(
            "credit-adjustment",
            SupplierCreditAdj,
            SupplierCreditAdjLine,
            "SCA",
            "pur",
            "pur.supplier_adj",
        ),
        Kind("refund", SupplierRefund, SupplierRefundLine, "SRF", "fin", "fin.supplier_refund"),
    )
}
REFUNDABLE = ("SCN", "SCA", "PV")  # supplier credits that money back can settle


def _kind(key: str) -> Kind:
    try:
        return KINDS[key]
    except KeyError as exc:
        raise NotFound("Unknown document type") from exc


def _query(k: Kind, p: Principal):
    stmt = (
        select(k.header)
        .where(k.header.company_id == p.company_id)
        .options(selectinload(k.header.lines))
        .execution_options(populate_existing=True)
    )
    if p.location_ids is not None:
        stmt = stmt.where(k.header.location_id.in_(p.location_ids))
    return stmt


def get_doc(db: Session, p: Principal, kind: str, doc_id: uuid.UUID, *, lock: bool = False):
    k = _kind(kind)
    p.require(f"{k.perm}.doc.read")
    stmt = _query(k, p).where(k.header.id == doc_id)
    if lock:
        stmt = stmt.with_for_update(of=k.header)
    doc = db.scalar(stmt)
    if doc is None:
        raise NotFound("Document not found")
    return doc


def list_docs(
    db: Session,
    p: Principal,
    kind: str,
    *,
    status: str | None = None,
    supplier_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 50,
):
    k = _kind(kind)
    p.require(f"{k.perm}.doc.read")
    h = k.header
    stmt = _query(k, p)
    if status:
        stmt = stmt.where(h.status == status)
    if supplier_id:
        stmt = stmt.where(h.supplier_id == supplier_id)
    if date_from:
        stmt = stmt.where(h.doc_date >= date_from)
    if date_to:
        stmt = stmt.where(h.doc_date <= date_to)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(h.doc_no.ilike(like), h.supplier_ref_no.ilike(like)))
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    rows = db.scalars(
        stmt.order_by(h.doc_date.desc(), h.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return list(rows), total


# --- building -----------------------------------------------------------------------------


def _check_header(db: Session, p: Principal, v: dict[str, Any]) -> None:
    if v.get("supplier_id"):
        s = db.get(Supplier, v["supplier_id"])
        if s is None or s.company_id != p.company_id:
            raise BadRequest("Supplier not found")
        if not s.is_active:
            raise BusinessRule("Supplier is inactive", {"supplier": s.code})
    if v.get("location_id"):
        loc = db.get(Location, v["location_id"])
        if loc is None or loc.company_id != p.company_id or not p.can_see_location(loc.id):
            raise BadRequest("Location not found")


def _note_lines(db: Session, p: Principal, k: Kind, lines: list[dict[str, Any]]) -> list:
    taxes = {t.id: t for t in db.scalars(select(TaxCode).where(TaxCode.company_id == p.company_id))}
    out = []
    for n, ln in enumerate(lines, start=1):
        tax = taxes.get(ln["tax_code_id"]) if ln.get("tax_code_id") else None
        if ln.get("tax_code_id") and tax is None:
            raise BadRequest("Tax code not found", {"line": n})
        amount = q2(Decimal(ln["amount"]))
        rate = tax.rate if tax else ZERO
        line_tax = q2(amount * rate / HUNDRED)
        out.append(
            k.line(
                line_no=n,
                description=ln["description"],
                ref_doc_no=ln.get("ref_doc_no"),
                amount=amount,
                tax_code_id=tax.id if tax else None,
                tax_rate=rate,
                line_tax=line_tax,
                line_total=amount + line_tax,
            )
        )
    return out


def _refund_lines(db: Session, p: Principal, doc, lines: list[dict[str, Any]]) -> list:
    out = []
    for n, ln in enumerate(lines, start=1):
        if ln["doc_type"] not in REFUNDABLE:
            raise BadRequest("A refund settles a supplier CN, credit adjustment or payment")
        m = ko.doc_type(ln["doc_type"]).model
        target = db.get(m, ln["doc_id"])
        if (
            target is None
            or target.company_id != p.company_id
            or target.supplier_id != doc.supplier_id
        ):
            raise BadRequest("Document not found for this supplier", {"line": n})
        out.append(
            SupplierRefundLine(
                line_no=n,
                doc_type=ln["doc_type"],
                doc_id=target.id,
                doc_no=target.doc_no,
                amount=q2(Decimal(ln["amount"])),
            )
        )
    return out


def _set_lines(db: Session, p: Principal, k: Kind, doc, lines: list[dict[str, Any]]) -> None:
    if k.key == "refund":
        doc.lines = _refund_lines(db, p, doc, lines)
        return
    doc.lines = _note_lines(db, p, k, lines)
    doc.subtotal = sum((ln.amount for ln in doc.lines), ZERO)
    doc.tax_total = sum((ln.line_tax for ln in doc.lines), ZERO)
    doc.total = doc.subtotal + doc.tax_total


def _snapshot(doc) -> dict[str, Any]:
    return audit.snapshot(
        doc,
        lines=[
            {c: getattr(ln, c) for c in ("description", "doc_no", "amount") if hasattr(ln, c)}
            for ln in doc.lines
        ],
    )


# --- commands -----------------------------------------------------------------------------


def create_doc(db: Session, p: Principal, kind: str, *, lines: list[dict[str, Any]], **v: Any):
    k = _kind(kind)
    p.require(f"{k.perm}.doc.create")
    _check_header(db, p, v)
    doc = k.header(
        company_id=p.company_id, status="DRAFT", created_by=p.user_id, updated_by=p.user_id, **v
    )
    _set_lines(db, p, k, doc, lines)
    db.add(doc)
    db.flush()
    audit.record(db, p, "create", k.entity, doc.id, after=_snapshot(doc))
    db.commit()
    return get_doc(db, p, kind, doc.id)


def update_doc(
    db: Session,
    p: Principal,
    kind: str,
    doc_id: uuid.UUID,
    version: int,
    *,
    lines: list[dict[str, Any]] | None = None,
    **v: Any,
):
    k = _kind(kind)
    p.require(f"{k.perm}.doc.create")
    doc = get_doc(db, p, kind, doc_id, lock=True)
    if doc.status not in EDITABLE:
        raise DocState(f"A {doc.status} document cannot be edited", {"status": doc.status})
    if doc.version != version:
        raise VersionConflict("This document was changed by someone else; reload it")
    before = _snapshot(doc)
    _check_header(db, p, v)
    for key, val in v.items():
        if val is None and key in ("doc_date", "supplier_id", "location_id", "amount", "method"):
            raise BadRequest("Field cannot be empty", {"field": key})
        setattr(doc, key, val)
    if lines is not None:
        _set_lines(db, p, k, doc, lines)
        db.flush()
    warnings = _validate(db, p, k, doc) if doc.status == "SAVED" else []
    doc.version += 1
    doc.updated_by = p.user_id
    db.flush()
    audit.record(db, p, "update", k.entity, doc.id, before=before, after=_snapshot(doc))
    db.commit()
    return get_doc(db, p, kind, doc.id), warnings


def delete_doc(db: Session, p: Principal, kind: str, doc_id: uuid.UUID) -> None:
    k = _kind(kind)
    p.require(f"{k.perm}.doc.create")
    doc = get_doc(db, p, kind, doc_id, lock=True)
    if doc.status != "DRAFT":
        raise DocState("Only drafts can be deleted; void it instead", {"status": doc.status})
    audit.record(db, p, "delete", k.entity, doc.id, before=_snapshot(doc))
    db.delete(doc)
    db.commit()


def _validate(db: Session, p: Principal, k: Kind, doc) -> list[dict[str, Any]]:
    if doc.doc_date > today(db, p):
        raise BusinessRule("Document date is in the future — check the year on the original")
    if k.key == "refund":
        if doc.amount <= 0:
            raise BusinessRule("Enter the amount the supplier refunded")
        named = sum((ln.amount for ln in doc.lines), ZERO)
        if named > doc.amount:
            raise BusinessRule(
                "The lines add up to more than the refund",
                {"lines": str(named), "amount": str(doc.amount)},
            )
        return []
    if not doc.lines:
        raise BusinessRule("A document needs at least one line")
    if any(ln.amount <= 0 for ln in doc.lines):
        raise BusinessRule("Line amounts must be more than zero")
    ref = (doc.supplier_ref_no or "").strip()
    if ref:
        dup = db.scalar(
            select(k.header.doc_no).where(
                k.header.company_id == p.company_id,
                k.header.supplier_id == doc.supplier_id,
                func.upper(func.trim(k.header.supplier_ref_no)) == ref.upper(),
                k.header.status.in_(("SAVED", "POSTED")),
                k.header.id != doc.id,
            )
        )
        if dup:
            raise BusinessRule(
                "This supplier document number is already recorded",
                {"supplier_ref_no": ref, "existing_doc_no": dup},
            )
        return []
    return [{"code": "SUPPLIER_REF_MISSING", "message": "Supplier document number is empty"}]


def save_doc(db: Session, p: Principal, kind: str, doc_id: uuid.UUID, version: int):
    k = _kind(kind)
    p.require(f"{k.perm}.doc.create")
    doc = get_doc(db, p, kind, doc_id, lock=True)
    if doc.status != "DRAFT":
        raise DocState("Only drafts can be saved", {"status": doc.status})
    if doc.version != version:
        raise VersionConflict("This document was changed by someone else; reload it")
    warnings = _validate(db, p, k, doc)
    if not doc.doc_no:
        doc.doc_no = next_doc_no(db, p.company_id, k.code)
    doc.status = "SAVED"
    doc.version += 1
    doc.updated_by = p.user_id
    audit.record(db, p, "save", k.entity, doc.id, after={"doc_no": doc.doc_no})
    db.commit()
    return get_doc(db, p, kind, doc.id), warnings


def _post_refund(db: Session, p: Principal, doc) -> None:
    """Knock the refund off the credits it names, then (with what is left) the supplier's
    oldest open credits. More money than they owe us cannot be booked as a refund."""
    if doc.lines:
        refs = [(ln.doc_type, ln.doc_id) for ln in doc.lines]
        targets = ko.lock_docs(db, p, refs)
        credits = [(ln.doc_type, targets[ln.doc_id], ln.amount) for ln in doc.lines]
    else:
        open_credits = [
            r
            for r in ko.open_items(db, p, "S", doc.supplier_id)
            if r["side"] == "C" and r["doc_type"] in REFUNDABLE
        ]
        targets = ko.lock_docs(db, p, [(r["doc_type"], r["id"]) for r in open_credits])
        credits = [(r["doc_type"], targets[r["id"]], r["open"]) for r in open_credits]
    room = sum((min(cap, ko.open_amount(db, ko.doc_type(c), d)) for c, d, cap in credits), ZERO)
    if doc.amount > room + ko.CENT:
        raise BusinessRule(
            "The supplier does not owe us that much: record the extra as a payment instead",
            {"refund": str(doc.amount), "open_credits": str(room)},
        )
    doc.status = "POSTED"
    db.flush()
    ko.settle(db, p, [("SRF", doc, doc.amount)], credits, ("SRF", doc.id))


def post_doc(db: Session, p: Principal, kind: str, doc_id: uuid.UUID, version: int):
    k = _kind(kind)
    p.require(f"{k.perm}.doc.post")
    doc = get_doc(db, p, kind, doc_id, lock=True)
    if doc.status != "SAVED":
        raise DocState("Only saved documents can be posted", {"status": doc.status})
    if doc.version != version:
        raise VersionConflict("This document was changed by someone else; reload it")
    _validate(db, p, k, doc)
    if k.key == "refund":
        _post_refund(db, p, doc)
    doc.status = "POSTED"
    doc.posted_at = datetime.now(UTC)
    doc.posted_by = p.user_id
    doc.version += 1
    doc.updated_by = p.user_id
    amount = doc.amount if k.key == "refund" else doc.total
    audit.record(db, p, "post", k.entity, doc.id, after={"doc_no": doc.doc_no, "amount": amount})
    db.commit()
    return get_doc(db, p, kind, doc.id)


def void_doc(db: Session, p: Principal, kind: str, doc_id: uuid.UUID, version: int, reason: str):
    k = _kind(kind)
    p.require(f"{k.perm}.doc.void")
    doc = get_doc(db, p, kind, doc_id, lock=True)
    if doc.status not in ("SAVED", "POSTED"):
        raise DocState("Only saved or posted documents can be voided", {"status": doc.status})
    if doc.version != version:
        raise VersionConflict("This document was changed by someone else; reload it")
    was = doc.status
    if was == "POSTED":
        ko.release_for_void(db, [doc.id], doc.id, p.user_id)
    doc.status = "VOIDED"
    doc.voided_at = datetime.now(UTC)
    doc.voided_by = p.user_id
    doc.void_reason = reason
    doc.version += 1
    doc.updated_by = p.user_id
    audit.record(db, p, "void", k.entity, doc.id, before={"status": was}, after={"reason": reason})
    db.commit()
    return get_doc(db, p, kind, doc.id)


def open_amount(db: Session, kind: str, doc) -> Decimal | None:
    if doc.status != "POSTED":
        return None
    return ko.open_amount(db, ko.doc_type(_kind(kind).code), doc)
