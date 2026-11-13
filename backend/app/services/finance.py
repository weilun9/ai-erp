"""Customer receipts (OR) and supplier payments (PV).

Lifecycle as other documents: DRAFT -> SAVED -> POSTED -> VOIDED.

Receipt: lines name the invoices it pays. On post, the lines are knocked off; with
auto_allocate the rest pays the customer's oldest open invoices; what is left stays open
on the receipt as an advance.

Supplier payment: keyed in as the supplier's payment advice reads (design doc section 5):
INVOICE lines paid, CN and FEE lines deducted, OTHER adjustments; `amount` is the net
cash. Lines are matched to our GRNs / supplier CNs by the supplier's own number (also
with prefix and leading zeros ignored). On post:
- matched invoices are knocked off with the matched CNs, the fees and the cash;
- FEE lines become fee documents (no paper of their own);
- a provisional CN whose amount agrees becomes COMPLETE; a CN whose amount differs
  becomes DISPUTED; anything that does not reconcile opens a problem document;
- a negative net (supplier pays us back) is a debit that settles leftover credits.
"""

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.core.errors import (
    BadRequest,
    BusinessRule,
    DocState,
    DuplicateSupplierDoc,
    NotFound,
    VersionConflict,
)
from app.models import (
    Customer,
    CustomerReceipt,
    CustomerReceiptLine,
    Grn,
    Location,
    SalesInvoice,
    Supplier,
    SupplierCn,
    SupplierCreditAdj,
    SupplierDebitNote,
    SupplierFee,
    SupplierPayment,
    SupplierPaymentLine,
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
    doc_type: str
    party_type: str
    party_attr: str
    party_model: type
    entity: str


KINDS = {
    k.key: k
    for k in (
        Kind("receipt", CustomerReceipt, "OR", "C", "customer_id", Customer, "fin.receipt"),
        Kind(
            "supplier-payment",
            SupplierPayment,
            "PV",
            "S",
            "supplier_id",
            Supplier,
            "fin.supplier_payment",
        ),
    )
}


def _kind(key: str) -> Kind:
    try:
        return KINDS[key]
    except KeyError as exc:
        raise NotFound("Unknown document type") from exc


# --- reading ------------------------------------------------------------------------------


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
    p.require("fin.doc.read")
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
    party_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    page: int = 1,
    page_size: int = 50,
):
    k = _kind(kind)
    p.require("fin.doc.read")
    h = k.header
    stmt = _query(k, p)
    if status:
        stmt = stmt.where(h.status == status)
    if party_id:
        stmt = stmt.where(getattr(h, k.party_attr) == party_id)
    if date_from:
        stmt = stmt.where(h.doc_date >= date_from)
    if date_to:
        stmt = stmt.where(h.doc_date <= date_to)
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    rows = db.scalars(
        stmt.order_by(h.doc_date.desc(), h.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return list(rows), total


def open_amounts(db: Session, docs) -> dict[uuid.UUID, Decimal]:
    """Unallocated money per receipt / payment (an advance), for display."""
    done = ko.knocked(db, [d.id for d in docs])
    return {d.id: abs(d.amount) - done.get(d.id, ZERO) for d in docs}


def list_fees(db: Session, p: Principal, supplier_id: uuid.UUID | None = None):
    p.require("fin.doc.read")
    stmt = select(SupplierFee).where(SupplierFee.company_id == p.company_id)
    if supplier_id:
        stmt = stmt.where(SupplierFee.supplier_id == supplier_id)
    return list(db.scalars(stmt.order_by(SupplierFee.doc_date.desc(), SupplierFee.doc_no.desc())))


# --- building -----------------------------------------------------------------------------


def _check_header_refs(db: Session, p: Principal, k: Kind, v: dict[str, Any]) -> None:
    if k.party_attr in v:
        party = db.get(k.party_model, v[k.party_attr])
        if party is None or party.company_id != p.company_id:
            raise BadRequest("Customer / supplier not found")
        if not party.is_active:
            raise BusinessRule("Customer / supplier is inactive", {"code": party.code})
    if "location_id" in v:
        loc = db.get(Location, v["location_id"])
        if loc is None or loc.company_id != p.company_id or not p.can_see_location(loc.id):
            raise BadRequest("Location not found")


def _receipt_lines(db: Session, p: Principal, lines: list[dict[str, Any]]) -> list:
    out = []
    for n, ln in enumerate(lines, start=1):
        inv = db.get(SalesInvoice, ln["invoice_id"])
        if inv is None or inv.company_id != p.company_id:
            raise BadRequest("Invoice not found", {"line": n})
        out.append(CustomerReceiptLine(line_no=n, invoice_id=inv.id, amount=Decimal(ln["amount"])))
    return out


def _payment_lines(db: Session, p: Principal, lines: list[dict[str, Any]]) -> list:
    taxes = {t.id for t in db.scalars(select(TaxCode).where(TaxCode.company_id == p.company_id))}
    out = []
    for n, ln in enumerate(lines, start=1):
        t, amt = ln["line_type"], Decimal(ln["amount"])
        if t in ("INVOICE", "CN") and not (ln.get("ref_no") or "").strip():
            raise BadRequest("Invoice / CN lines need the supplier's number", {"line": n})
        if t == "FEE" and not ln.get("fee_type"):
            raise BadRequest("Fee lines need a fee type", {"line": n})
        if (t == "OTHER" and amt == 0) or (t != "OTHER" and amt <= 0):
            raise BadRequest("Amount must be more than zero", {"line": n})
        if ln.get("tax_code_id") and ln["tax_code_id"] not in taxes:
            raise BadRequest("Tax code not found", {"line": n})
        out.append(
            SupplierPaymentLine(
                line_no=n,
                line_type=t,
                ref_no=(ln.get("ref_no") or "").strip() or None,
                amount=amt,
                fee_type=ln.get("fee_type") if t == "FEE" else None,
                tax_code_id=ln.get("tax_code_id") if t == "FEE" else None,
                remark=ln.get("remark"),
            )
        )
    return out


def _payment_net(doc) -> Decimal:
    sign = {"INVOICE": 1, "CN": -1, "FEE": -1, "OTHER": 1}
    return sum((sign[ln.line_type] * ln.amount for ln in doc.lines), ZERO)


def _norm(no: str | None) -> str:
    return re.sub(r"[^0-9A-Z]", "", (no or "").upper())


def _digits(no: str | None) -> str:
    return re.sub(r"\D", "", no or "").lstrip("0")


def _find(db: Session, p: Principal, model, supplier_id, ref_no: str):
    """The supplier's document by its own number: exact (ignoring case, spaces and
    punctuation) first, then by digits alone (prefix and leading zeros ignored)."""
    col = model.supplier_ref_no
    base = select(model).where(
        model.company_id == p.company_id,
        model.supplier_id == supplier_id,
        model.status.in_(("SAVED", "POSTED")),
        col.is_not(None),
    )
    exact = db.scalars(
        base.where(func.upper(func.regexp_replace(col, "[^0-9A-Za-z]", "", "g")) == _norm(ref_no))
    ).all()
    if len(exact) == 1:
        return exact[0]
    digits = _digits(ref_no)
    if not exact and digits:
        loose = db.scalars(
            base.where(func.ltrim(func.regexp_replace(col, r"\D", "", "g"), "0") == digits)
        ).all()
        if len(loose) == 1:
            return loose[0]
    return None


def _match_lines(db: Session, p: Principal, doc) -> None:
    for ln in doc.lines:
        ln.matched_type = ln.matched_id = ln.matched_no = ln.match_status = None
        if ln.line_type not in ("INVOICE", "CN"):
            continue
        # an invoice line may name a GRN or a debit note; a CN line a goods return or a
        # credit note without goods
        pairs = (
            ((Grn, "GRN"), (SupplierDebitNote, "SDN"))
            if ln.line_type == "INVOICE"
            else ((SupplierCn, "SCN"), (SupplierCreditAdj, "SCA"))
        )
        found, code = None, None
        for model, c in pairs:
            found = _find(db, p, model, doc.supplier_id, ln.ref_no)
            if found is not None:
                code = c
                break
        if found is None:
            ln.match_status = "NOT_FOUND"
            continue
        ln.matched_type, ln.matched_id, ln.matched_no = code, found.id, found.doc_no
        ready = found.status == "POSTED" and (
            code != "SCN" or found.completeness in ("PROVISIONAL", "COMPLETE")
        )
        if not ready:
            ln.match_status = "NOT_READY"
        elif abs(found.total - ln.amount) < ko.CENT:
            ln.match_status = "MATCHED"
        else:
            ln.match_status = "AMOUNT_DIFF"


def _snapshot(doc) -> dict[str, Any]:
    return audit.snapshot(doc, lines=[audit.snapshot(ln) for ln in doc.lines])


# --- validation ---------------------------------------------------------------------------


def _check_date(db: Session, p: Principal, doc) -> None:
    now = today(db, p)
    if doc.doc_date > now:
        raise BusinessRule(
            "Document date is in the future — check the year on the original",
            {"doc_date": doc.doc_date.isoformat(), "today": now.isoformat()},
        )


def _receipt_checks(db: Session, p: Principal, doc) -> list[dict[str, Any]]:
    if doc.amount <= 0:
        raise BusinessRule("Receipt amount must be more than zero")
    allocated = sum((ln.amount for ln in doc.lines), ZERO)
    if allocated > doc.amount:
        raise BusinessRule(
            "Lines allocate more than was received",
            {"amount": str(doc.amount), "allocated": str(allocated)},
        )
    per_invoice: dict[uuid.UUID, Decimal] = {}
    for ln in doc.lines:
        per_invoice[ln.invoice_id] = per_invoice.get(ln.invoice_id, ZERO) + ln.amount
    invs = ko.lock_docs(db, p, [("INV", i) for i in per_invoice])
    dt = ko.doc_type("INV")
    too_much = []
    for inv_id, amt in per_invoice.items():
        inv = invs[inv_id]
        if inv.status != "POSTED" or inv.customer_id != doc.customer_id:
            raise BusinessRule(
                "Receipts can only pay posted invoices of the same customer",
                {"invoice_no": inv.doc_no},
            )
        left = ko.open_amount(db, dt, inv)
        if amt > left:
            too_much.append({"invoice_no": inv.doc_no, "open": str(left), "amount": str(amt)})
    if too_much:
        raise BusinessRule("More than the invoice's open amount", {"invoices": too_much})
    if allocated < doc.amount and not doc.auto_allocate:
        return [
            {
                "code": "ADVANCE",
                "message": "Part of the receipt is not allocated and stays as an advance",
                "advance": str(doc.amount - allocated),
            }
        ]
    return []


def _payment_checks(db: Session, p: Principal, doc) -> list[dict[str, Any]]:
    if not doc.lines:
        raise BusinessRule("A payment needs at least one line")
    _match_lines(db, p, doc)
    seen: dict[uuid.UUID, int] = {}
    for ln in doc.lines:
        if ln.matched_id:
            if ln.matched_id in seen:
                raise BusinessRule(
                    "The same document is on two lines",
                    {"doc_no": ln.matched_no, "lines": [seen[ln.matched_id], ln.line_no]},
                )
            seen[ln.matched_id] = ln.line_no
    ref = (doc.supplier_ref_no or "").strip()
    if ref:
        dup = db.scalar(
            select(SupplierPayment.doc_no).where(
                SupplierPayment.company_id == p.company_id,
                SupplierPayment.supplier_id == doc.supplier_id,
                func.upper(func.trim(SupplierPayment.supplier_ref_no)) == ref.upper(),
                SupplierPayment.status.in_(("SAVED", "POSTED")),
                SupplierPayment.id != doc.id,
            )
        )
        if dup:
            raise DuplicateSupplierDoc(
                "This payment advice is already recorded",
                {"supplier_ref_no": ref, "existing_doc_no": dup},
            )
    messages = {
        "NOT_FOUND": "No document in the system with this number",
        "NOT_READY": "Document found but not posted / not confirmed yet",
        "AMOUNT_DIFF": "Amount differs from the document in the system",
    }
    warnings = []
    for ln in doc.lines:
        if ln.match_status in messages:
            w = {
                "code": f"PAYMENT_{ln.match_status}",
                "message": messages[ln.match_status],
                "line": ln.line_no,
                "ref_no": ln.ref_no,
                "amount": str(ln.amount),
            }
            if ln.matched_id:
                found = db.get(Grn if ln.matched_type == "GRN" else SupplierCn, ln.matched_id)
                w |= {"doc_no": found.doc_no, "doc_total": str(found.total)}
            warnings.append(w)
    if doc.amount < 0:
        warnings.append(
            {
                "code": "SUPPLIER_REFUND",
                "message": "Deductions are more than the invoices: the supplier owes us",
                "amount": str(-doc.amount),
            }
        )
    return warnings


def _validate(db: Session, p: Principal, k: Kind, doc) -> list[dict[str, Any]]:
    _check_date(db, p, doc)
    if k.key == "receipt":
        return _receipt_checks(db, p, doc)
    return _payment_checks(db, p, doc)


# --- commands -----------------------------------------------------------------------------


def _set_lines(db: Session, p: Principal, k: Kind, doc, lines: list[dict[str, Any]]) -> None:
    if k.key == "receipt":
        doc.lines = _receipt_lines(db, p, lines)
    else:
        doc.lines = _payment_lines(db, p, lines)
        doc.amount = _payment_net(doc)
        db.flush()
        _match_lines(db, p, doc)


def create_doc(db: Session, p: Principal, kind: str, *, lines: list[dict[str, Any]], **v: Any):
    k = _kind(kind)
    p.require("fin.doc.create")
    _check_header_refs(db, p, k, v)
    doc = k.header(
        company_id=p.company_id, status="DRAFT", created_by=p.user_id, updated_by=p.user_id, **v
    )
    db.add(doc)
    _set_lines(db, p, k, doc, lines)
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
    p.require("fin.doc.create")
    doc = get_doc(db, p, kind, doc_id, lock=True)
    if doc.status not in EDITABLE:
        raise DocState(f"A {doc.status} document cannot be edited", {"status": doc.status})
    if doc.version != version:
        raise VersionConflict("This document was changed by someone else; reload it")
    before = _snapshot(doc)
    _check_header_refs(db, p, k, v)
    for key, val in v.items():
        if val is None and key in ("doc_date", k.party_attr, "location_id", "method", "amount"):
            raise BadRequest("Field cannot be empty", {"field": key})
        setattr(doc, key, val)
    if lines is not None:
        _set_lines(db, p, k, doc, lines)
    elif k.key == "supplier-payment":
        _match_lines(db, p, doc)
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
    p.require("fin.doc.create")
    doc = get_doc(db, p, kind, doc_id, lock=True)
    if doc.status != "DRAFT":
        raise DocState("Only drafts can be deleted; void it instead", {"status": doc.status})
    audit.record(db, p, "delete", k.entity, doc.id, before=_snapshot(doc))
    db.delete(doc)
    db.commit()


def save_doc(db: Session, p: Principal, kind: str, doc_id: uuid.UUID, version: int):
    k = _kind(kind)
    p.require("fin.doc.create")
    doc = get_doc(db, p, kind, doc_id, lock=True)
    if doc.status != "DRAFT":
        raise DocState("Only drafts can be saved", {"status": doc.status})
    if doc.version != version:
        raise VersionConflict("This document was changed by someone else; reload it")
    warnings = _validate(db, p, k, doc)
    if not doc.doc_no:
        doc.doc_no = next_doc_no(db, p.company_id, k.doc_type)
    doc.status = "SAVED"
    doc.version += 1
    doc.updated_by = p.user_id
    audit.record(
        db, p, "save", k.entity, doc.id, after={"doc_no": doc.doc_no, "warnings": warnings}
    )
    db.commit()
    return get_doc(db, p, kind, doc.id), warnings


def _post_receipt(db: Session, p: Principal, doc) -> None:
    _receipt_checks(db, p, doc)  # re-check under lock: invoices may have been paid since
    source = ("OR", doc.id)
    for ln in doc.lines:
        inv = ko.lock_docs(db, p, [("INV", ln.invoice_id)])[ln.invoice_id]
        ko.knock(db, p, ("INV", inv), ("OR", doc), ln.amount, source=source)
    if not doc.auto_allocate:
        return
    left = ko.open_amount(db, ko.doc_type("OR"), doc)
    if left <= 0:
        return
    ids = db.scalars(
        select(SalesInvoice.id).where(
            SalesInvoice.company_id == p.company_id,
            SalesInvoice.customer_id == doc.customer_id,
            SalesInvoice.status == "POSTED",
        )
    ).all()
    invs = ko.lock_docs(db, p, [("INV", i) for i in ids])
    oldest_first = sorted(
        invs.values(), key=lambda i: (i.due_date or i.doc_date, i.doc_date, i.doc_no)
    )
    n = len(doc.lines)
    for inv in oldest_first:
        if left <= 0:
            break
        take = min(left, ko.open_amount(db, ko.doc_type("INV"), inv))
        if take <= 0:
            continue
        ko.knock(db, p, ("INV", inv), ("OR", doc), take, source=source)
        n += 1
        doc.lines.append(CustomerReceiptLine(line_no=n, invoice_id=inv.id, amount=take))
        left -= take


def _post_payment(db: Session, p: Principal, doc) -> None:
    _payment_checks(db, p, doc)
    party = ("S", doc.supplier_id)
    refs = [(ln.matched_type, ln.matched_id) for ln in doc.lines if ln.matched_id]
    docs = ko.lock_docs(db, p, refs)
    source = ("PV", doc.id)
    taxes = {t.id: t for t in db.scalars(select(TaxCode).where(TaxCode.company_id == p.company_id))}

    debits: list[tuple[str, Any, Decimal]] = []
    credits: list[tuple[str, Any, Decimal]] = []
    issues: list[tuple[str, str, dict[str, Any]]] = []
    for ln in doc.lines:
        found = docs.get(ln.matched_id) if ln.matched_id else None
        info = {"line": ln.line_no, "ref_no": ln.ref_no, "amount": ln.amount}
        if found is not None:
            info |= {"doc_no": found.doc_no, "doc_total": found.total}
        if ln.line_type == "INVOICE":
            if ln.match_status in ("MATCHED", "AMOUNT_DIFF"):
                debits.append((ln.matched_type, found, ln.amount))
        elif ln.line_type == "CN":
            if ln.match_status == "MATCHED":
                if getattr(found, "completeness", None) == "PROVISIONAL":
                    found.completeness = "COMPLETE"  # confirmed by the supplier's advice
                credits.append((ln.matched_type, found, ln.amount))
            elif ln.match_status == "AMOUNT_DIFF" and ln.matched_type == "SCN":
                found.completeness = "DISPUTED"
        elif ln.line_type == "FEE":
            tax = taxes.get(ln.tax_code_id) if ln.tax_code_id else None
            rate = tax.rate if tax else ZERO
            fee = SupplierFee(
                company_id=p.company_id,
                doc_no=next_doc_no(db, p.company_id, "FEE"),
                doc_date=doc.doc_date,
                supplier_id=doc.supplier_id,
                location_id=doc.location_id,
                fee_type=ln.fee_type,
                amount=ln.amount,
                tax_code_id=ln.tax_code_id,
                tax_amount=q2(ln.amount * rate / (HUNDRED + rate)),  # amount includes tax
                payment_id=doc.id,
                payment_line_id=ln.id,
                created_by=p.user_id,
                updated_by=p.user_id,
            )
            db.add(fee)
            db.flush()
            credits.append(("FEE", fee, fee.amount))

        kind_name = "invoice" if ln.line_type == "INVOICE" else "CN"
        if ln.match_status == "NOT_FOUND":
            code = "MISSING_INVOICE" if ln.line_type == "INVOICE" else "MISSING_CN"
            issues.append((code, f"Payment lists {kind_name} {ln.ref_no}, not in the system", info))
        elif ln.match_status == "NOT_READY":
            issues.append(
                ("DOC_NOT_READY", f"{kind_name} {ln.ref_no} is not posted / confirmed", info)
            )
        elif ln.match_status == "AMOUNT_DIFF":
            issues.append(
                ("AMOUNT_MISMATCH", f"{kind_name} {ln.ref_no}: payment amount differs", info)
            )

    if doc.amount > 0:
        credits.append(("PV", doc, doc.amount))
    elif doc.amount < 0:
        debits.append(("PV", doc, -doc.amount))
    doc.status = "POSTED"  # knock-off needs both sides posted
    db.flush()
    ko.settle(db, p, debits, credits, source)
    for code, title, info in issues:
        ko.raise_issue(db, p, code, title, doc_type_code="PV", doc=doc, party=party, details=info)


def post_doc(db: Session, p: Principal, kind: str, doc_id: uuid.UUID, version: int):
    k = _kind(kind)
    p.require("fin.doc.post")
    doc = get_doc(db, p, kind, doc_id, lock=True)
    if doc.status != "SAVED":
        raise DocState("Only saved documents can be posted", {"status": doc.status})
    if doc.version != version:
        raise VersionConflict("This document was changed by someone else; reload it")
    doc.status = "POSTED"
    db.flush()
    if k.key == "receipt":
        _post_receipt(db, p, doc)
    else:
        _post_payment(db, p, doc)
    doc.posted_at = datetime.now(UTC)
    doc.posted_by = p.user_id
    doc.version += 1
    doc.updated_by = p.user_id
    audit.record(
        db, p, "post", k.entity, doc.id, after={"doc_no": doc.doc_no, "amount": doc.amount}
    )
    db.commit()
    return get_doc(db, p, kind, doc.id)


def void_doc(db: Session, p: Principal, kind: str, doc_id: uuid.UUID, version: int, reason: str):
    k = _kind(kind)
    p.require("fin.doc.void")
    doc = get_doc(db, p, kind, doc_id, lock=True)
    if doc.status not in ("SAVED", "POSTED"):
        raise DocState("Only saved or posted documents can be voided", {"status": doc.status})
    if doc.version != version:
        raise VersionConflict("This document was changed by someone else; reload it")
    was = doc.status
    now = datetime.now(UTC)
    if was == "POSTED":
        fees = (
            db.scalars(select(SupplierFee).where(SupplierFee.payment_id == doc.id)).all()
            if k.key == "supplier-payment"
            else []
        )
        ko.release_for_void(db, [doc.id, *(f.id for f in fees)], doc.id, p.user_id)
        for f in fees:
            f.status, f.voided_at = "VOIDED", now
        ko.cancel_issues(db, doc.id, p.user_id)
    doc.status = "VOIDED"
    doc.voided_at = now
    doc.voided_by = p.user_id
    doc.void_reason = reason
    doc.version += 1
    doc.updated_by = p.user_id
    audit.record(db, p, "void", k.entity, doc.id, before={"status": was}, after={"reason": reason})
    db.commit()
    return get_doc(db, p, kind, doc.id)
