"""Knock-off (对冲) and party balances.

A party's balance is debit documents (sales invoice, GRN: they raise what is owed) minus
credit documents (receipt, customer CN, supplier CN, fee, supplier payment). A knock-off
ties an amount of one debit document to one credit document of the same party; what is
left on a document is its open amount (an unpaid invoice, an advance, an unused CN).

Documents are locked FOR UPDATE (in id order) before their open amount is read, so two
receipts paying the same invoice at the same time cannot both take the same balance.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select, union_all
from sqlalchemy.orm import Session

from app.core.errors import BadRequest, BusinessRule, DocState, NotFound
from app.models import (
    CustomerCn,
    CustomerReceipt,
    DocIssue,
    Grn,
    Knockoff,
    SalesInvoice,
    SupplierCn,
    SupplierFee,
    SupplierPayment,
)
from app.services import audit
from app.services.auth import Principal

ZERO = Decimal(0)
CENT = Decimal("0.005")


@dataclass(frozen=True)
class DocType:
    code: str
    model: type
    party_type: str  # C customer, S supplier
    side: str  # D raises the balance, C lowers it (a negative amount flips it)
    amount_attr: str

    @property
    def party_attr(self) -> str:
        return "customer_id" if self.party_type == "C" else "supplier_id"


DOCS: dict[str, DocType] = {
    d.code: d
    for d in (
        DocType("INV", SalesInvoice, "C", "D", "total"),
        DocType("CN", CustomerCn, "C", "C", "total"),
        DocType("OR", CustomerReceipt, "C", "C", "amount"),
        DocType("GRN", Grn, "S", "D", "total"),
        DocType("SCN", SupplierCn, "S", "C", "total"),
        DocType("FEE", SupplierFee, "S", "C", "amount"),
        DocType("PV", SupplierPayment, "S", "C", "amount"),
    )
}
PARTY_TYPES = {"customer": "C", "supplier": "S"}


def doc_type(code: str) -> DocType:
    try:
        return DOCS[code]
    except KeyError as exc:
        raise BadRequest("Unknown document type", {"allowed": sorted(DOCS)}) from exc


def side_value(dt: DocType, doc) -> tuple[str, Decimal]:
    v = getattr(doc, dt.amount_attr)
    if v < 0:
        return ("C" if dt.side == "D" else "D"), -v
    return dt.side, v


def knocked(db: Session, ids) -> dict[uuid.UUID, Decimal]:
    """Amount already knocked off, per document id (either side)."""
    ids = list(ids)
    if not ids:
        return {}
    K = Knockoff
    live = K.voided_at.is_(None)
    u = union_all(
        select(K.debit_id.label("doc_id"), K.amount).where(live, K.debit_id.in_(ids)),
        select(K.credit_id.label("doc_id"), K.amount).where(live, K.credit_id.in_(ids)),
    ).subquery()
    return dict(db.execute(select(u.c.doc_id, func.sum(u.c.amount)).group_by(u.c.doc_id)).all())


def open_amount(db: Session, dt: DocType, doc) -> Decimal:
    return side_value(dt, doc)[1] - knocked(db, [doc.id]).get(doc.id, ZERO)


def lock_docs(db: Session, p: Principal, refs: list[tuple[str, uuid.UUID]]) -> dict:
    """Lock documents in id order (no deadlocks); returns {id: doc}."""
    out = {}
    for code, doc_id in sorted(set(refs), key=lambda r: str(r[1])):
        m = doc_type(code).model
        doc = db.scalar(
            select(m)
            .where(m.id == doc_id, m.company_id == p.company_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if doc is None:
            raise NotFound("Document not found", {"doc_type": code, "id": str(doc_id)})
        out[doc_id] = doc
    return out


def knock(
    db: Session,
    p: Principal,
    debit: tuple[str, Any],
    credit: tuple[str, Any],
    amount: Decimal,
    *,
    source: tuple[str, uuid.UUID] | None = None,
) -> Knockoff:
    """Knock `amount` of the debit document off with the credit document. Both must be
    locked by the caller."""
    (dcode, ddoc), (ccode, cdoc) = debit, credit
    ddt, cdt = doc_type(dcode), doc_type(ccode)
    for dt, doc, want in ((ddt, ddoc, "D"), (cdt, cdoc, "C")):
        if doc.status != "POSTED":
            raise DocState("Only posted documents can be knocked off", {"doc_no": doc.doc_no})
        if side_value(dt, doc)[0] != want:
            raise BadRequest(
                "Knock a debit document (invoice) off with a credit document "
                "(receipt, payment, CN, fee)",
                {"doc_no": doc.doc_no},
            )
    if ddt.party_type != cdt.party_type or getattr(ddoc, ddt.party_attr) != getattr(
        cdoc, cdt.party_attr
    ):
        raise BusinessRule("Both documents must belong to the same customer or supplier")
    if amount <= 0:
        raise BadRequest("Amount must be more than zero")
    for dt, doc in ((ddt, ddoc), (cdt, cdoc)):
        left = open_amount(db, dt, doc)
        if amount > left + CENT / 10:
            raise BusinessRule(
                "Amount is more than what is open on the document",
                {"doc_no": doc.doc_no, "open": str(left), "amount": str(amount)},
            )
    k = Knockoff(
        company_id=p.company_id,
        party_type=ddt.party_type,
        party_id=getattr(ddoc, ddt.party_attr),
        debit_type=dcode,
        debit_id=ddoc.id,
        debit_no=ddoc.doc_no,
        credit_type=ccode,
        credit_id=cdoc.id,
        credit_no=cdoc.doc_no,
        amount=amount,
        source_type=source[0] if source else None,
        source_id=source[1] if source else None,
        created_by=p.user_id,
    )
    db.add(k)
    db.flush()
    return k


def settle(
    db: Session,
    p: Principal,
    debits: list[tuple[str, Any, Decimal]],
    credits: list[tuple[str, Any, Decimal]],
    source: tuple[str, uuid.UUID],
) -> list[Knockoff]:
    """Knock debits off with credits in the order given, each up to its cap and its open
    amount. What cannot be matched stays open."""
    caps = [[code, doc, cap] for code, doc, cap in credits]
    made = []
    for dcode, ddoc, dcap in debits:
        need = min(dcap, open_amount(db, doc_type(dcode), ddoc))
        for c in caps:
            if need <= 0:
                break
            avail = min(c[2], open_amount(db, doc_type(c[0]), c[1]))
            take = min(need, avail)
            if take > 0:
                made.append(knock(db, p, (dcode, ddoc), (c[0], c[1]), take, source=source))
                c[2] -= take
                need -= take
    return made


def release_for_void(db: Session, doc_ids: list[uuid.UUID], source_id: uuid.UUID, user_id) -> None:
    """Called while voiding a document: knock-offs it made itself are undone; any other
    knock-off on it (a receipt paying this invoice...) must be undone first."""
    K = Knockoff
    others = db.scalars(
        select(K).where(
            K.voided_at.is_(None),
            or_(K.debit_id.in_(doc_ids), K.credit_id.in_(doc_ids)),
            or_(K.source_id.is_(None), K.source_id != source_id),
        )
    ).all()
    if others:
        raise DocState(
            "This document is knocked off; void the receipt / payment or undo the knock-off first",
            {"knocked_with": sorted({k.credit_no for k in others} | {k.debit_no for k in others})},
        )
    now = datetime.now(UTC)
    for k in db.scalars(select(K).where(K.voided_at.is_(None), K.source_id == source_id)):
        k.voided_at, k.voided_by = now, user_id


# --- by hand ------------------------------------------------------------------------------


def manual_knock(
    db: Session,
    p: Principal,
    debit_type: str,
    debit_id: uuid.UUID,
    credit_type: str,
    credit_id: uuid.UUID,
    amount: Decimal | None,
) -> Knockoff:
    p.require("fin.doc.create")
    docs = lock_docs(db, p, [(debit_type, debit_id), (credit_type, credit_id)])
    ddoc, cdoc = docs[debit_id], docs[credit_id]
    if amount is None:
        amount = min(
            open_amount(db, doc_type(debit_type), ddoc),
            open_amount(db, doc_type(credit_type), cdoc),
        )
    k = knock(db, p, (debit_type, ddoc), (credit_type, cdoc), amount)
    audit.record(db, p, "knockoff", "fin.knockoff", k.id, after=audit.snapshot(k))
    db.commit()
    return k


def undo_knock(db: Session, p: Principal, knockoff_id: uuid.UUID) -> Knockoff:
    p.require("fin.doc.create")
    k = db.scalar(
        select(Knockoff)
        .where(Knockoff.id == knockoff_id, Knockoff.company_id == p.company_id)
        .with_for_update()
    )
    if k is None:
        raise NotFound("Knock-off not found")
    if k.voided_at is not None:
        raise DocState("This knock-off was already undone")
    if k.source_id is not None:
        raise DocState(
            "This knock-off was made by a document; void that document instead",
            {"source": k.source_type},
        )
    k.voided_at, k.voided_by = datetime.now(UTC), p.user_id
    audit.record(db, p, "undo", "fin.knockoff", k.id, before=audit.snapshot(k))
    db.commit()
    return k


def list_knockoffs(db: Session, p: Principal, doc_id: uuid.UUID) -> list[Knockoff]:
    p.require("fin.doc.read")
    K = Knockoff
    return list(
        db.scalars(
            select(K)
            .where(K.company_id == p.company_id, or_(K.debit_id == doc_id, K.credit_id == doc_id))
            .order_by(K.created_at)
        )
    )


# --- balances -----------------------------------------------------------------------------


def _party_docs(db: Session, company_id, party_type: str, party_id, *, before: date | None = None):
    for dt in DOCS.values():
        if dt.party_type != party_type:
            continue
        m = dt.model
        stmt = select(m).where(
            m.company_id == company_id,
            getattr(m, dt.party_attr) == party_id,
            m.status == "POSTED",
        )
        if before:
            stmt = stmt.where(m.doc_date < before)
        for doc in db.scalars(stmt):
            yield dt, doc


def party_balance(db: Session, company_id, party_type: str, party_id) -> Decimal:
    """What the customer owes us / what we owe the supplier (credits subtract)."""
    total = ZERO
    for dt, doc in _party_docs(db, company_id, party_type, party_id):
        side, v = side_value(dt, doc)
        total += v if side == "D" else -v
    return total


def open_items(db: Session, p: Principal, party_type: str, party_id: uuid.UUID) -> list[dict]:
    p.require("fin.doc.read")
    docs = list(_party_docs(db, p.company_id, party_type, party_id))
    done = knocked(db, [d.id for _, d in docs])
    out = []
    for dt, doc in docs:
        side, v = side_value(dt, doc)
        left = v - done.get(doc.id, ZERO)
        if abs(left) < CENT:
            continue
        out.append(
            {
                "doc_type": dt.code,
                "id": doc.id,
                "doc_no": doc.doc_no,
                "doc_date": doc.doc_date,
                "due_date": getattr(doc, "due_date", None),
                "side": side,
                "amount": v,
                "settled": done.get(doc.id, ZERO),
                "open": left,
            }
        )
    out.sort(key=lambda r: (r["due_date"] or r["doc_date"], r["doc_date"], r["doc_no"]))
    return out


def statement(
    db: Session,
    p: Principal,
    party_type: str,
    party_id: uuid.UUID,
    date_from: date | None,
    date_to: date | None,
) -> dict[str, Any]:
    """Statement of account (往来账): opening balance, every posted document, closing."""
    p.require("fin.doc.read")
    opening = ZERO
    rows = []
    for dt, doc in _party_docs(db, p.company_id, party_type, party_id):
        side, v = side_value(dt, doc)
        signed = v if side == "D" else -v
        if date_from and doc.doc_date < date_from:
            opening += signed
        elif not date_to or doc.doc_date <= date_to:
            rows.append((doc.doc_date, doc.posted_at, dt.code, doc, side, v, signed))
    rows.sort(key=lambda r: (r[0], r[1] or datetime.min.replace(tzinfo=UTC)))
    balance = opening
    lines = []
    for d, _, code, doc, side, v, signed in rows:
        balance += signed
        lines.append(
            {
                "doc_date": d,
                "doc_type": code,
                "id": doc.id,
                "doc_no": doc.doc_no,
                "debit": v if side == "D" else ZERO,
                "credit": v if side == "C" else ZERO,
                "balance": balance,
            }
        )
    return {"opening": opening, "lines": lines, "closing": balance}


# --- problem documents --------------------------------------------------------------------


def raise_issue(
    db: Session,
    p: Principal,
    issue_type: str,
    title: str,
    *,
    doc_type_code: str,
    doc,
    party: tuple[str, uuid.UUID] | None = None,
    details: dict[str, Any] | None = None,
) -> DocIssue:
    issue = DocIssue(
        company_id=p.company_id,
        issue_type=issue_type,
        title=title,
        doc_type=doc_type_code,
        doc_id=doc.id,
        doc_no=doc.doc_no,
        party_type=party[0] if party else None,
        party_id=party[1] if party else None,
        details=audit.jsonable(details or {}),
        created_by=p.user_id,
    )
    db.add(issue)
    return issue


def list_issues(
    db: Session, p: Principal, *, status: str | None = None, doc_id: uuid.UUID | None = None
) -> list[DocIssue]:
    p.require("core.issue.read")
    stmt = select(DocIssue).where(DocIssue.company_id == p.company_id)
    if status:
        stmt = stmt.where(DocIssue.status == status)
    if doc_id:
        stmt = stmt.where(DocIssue.doc_id == doc_id)
    return list(db.scalars(stmt.order_by(DocIssue.created_at.desc())))


def resolve_issue(db: Session, p: Principal, issue_id: uuid.UUID, resolution: str) -> DocIssue:
    p.require("core.issue.handle")
    issue = db.scalar(
        select(DocIssue)
        .where(DocIssue.id == issue_id, DocIssue.company_id == p.company_id)
        .with_for_update()
    )
    if issue is None:
        raise NotFound("Problem document not found")
    if issue.status != "OPEN":
        raise DocState("This problem is already closed", {"status": issue.status})
    issue.status = "RESOLVED"
    issue.resolution = resolution
    issue.resolved_at, issue.resolved_by = datetime.now(UTC), p.user_id
    audit.record(db, p, "resolve", "core.doc_issue", issue.id, after={"resolution": resolution})
    db.commit()
    return issue


def cancel_issues(db: Session, doc_id: uuid.UUID, user_id) -> None:
    now = datetime.now(UTC)
    for i in db.scalars(
        select(DocIssue).where(DocIssue.doc_id == doc_id, DocIssue.status == "OPEN")
    ):
        i.status, i.resolved_at, i.resolved_by = "CANCELLED", now, user_id
        i.resolution = "Document voided"
