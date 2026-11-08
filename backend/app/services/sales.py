"""Sales documents: quotation, delivery order (DO), sales invoice, customer CN (return).

Lifecycle as purchasing: DRAFT --save--> SAVED --post--> POSTED --void--> VOIDED.
- Quotation never touches stock and is never posted: it is converted into a DO or invoice.
  Voiding a saved quotation cancels it.
- DO posts stock out at the moving-average cost.
- Sales invoice posts stock out for its own lines. Lines invoiced from a posted DO
  (do_line_id) left with the DO, so they move no stock and take the DO's cost. A DO line
  can be invoiced in parts, never beyond its quantity.
- Customer CN brings stock back at the cost it left with on the referenced invoice (else the
  current average). Its price follows the supplier-return rule: the CN's own price, else
  the price on the referenced invoice, else MISSING, which blocks saving.
- Selling price: typed on the line, else the item's selling price for that unit, else
  MISSING.
- Negative stock is allowed (company decision 2026-11-08).
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.core.errors import BadRequest, BusinessRule, DocState, NotFound, VersionConflict
from app.models import (
    Customer,
    CustomerCn,
    CustomerCnLine,
    DeliveryOrder,
    DeliveryOrderLine,
    Item,
    ItemUom,
    Location,
    PostingJob,
    Quotation,
    QuotationLine,
    SalesInvoice,
    SalesInvoiceLine,
    StockBalance,
    TaxCode,
)
from app.services import audit, stock
from app.services.auth import Principal
from app.services.doclib import EDITABLE, check_common, line_amounts, q2, q4, set_totals, today
from app.services.numbering import next_doc_no


@dataclass(frozen=True)
class Kind:
    key: str
    header: type
    line: type
    doc_type: str
    direction: int  # stock: -1 out, +1 in, 0 none
    entity: str


KINDS: dict[str, Kind] = {
    k.key: k
    for k in (
        Kind("quotation", Quotation, QuotationLine, "QT", 0, "sal.quotation"),
        Kind("delivery-order", DeliveryOrder, DeliveryOrderLine, "DO", -1, "sal.do"),
        Kind("sales-invoice", SalesInvoice, SalesInvoiceLine, "INV", -1, "sal.invoice"),
        Kind("customer-cn", CustomerCn, CustomerCnLine, "CN", +1, "sal.customer_cn"),
    )
}

# What can be converted into what, and the status the source must have.
CONVERSIONS: dict[tuple[str, str], str] = {
    ("quotation", "delivery-order"): "SAVED",
    ("quotation", "sales-invoice"): "SAVED",
    ("delivery-order", "sales-invoice"): "POSTED",
    ("sales-invoice", "customer-cn"): "POSTED",
}
ACTIVE = ("SAVED", "POSTED")  # documents that count (drafts and voids do not)


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
    p.require("sal.doc.read")
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
    customer_id: uuid.UUID | None = None,
    location_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 50,
):
    k = _kind(kind)
    p.require("sal.doc.read")
    h = k.header
    stmt = _query(k, p)
    if status:
        stmt = stmt.where(h.status == status)
    if customer_id:
        stmt = stmt.where(h.customer_id == customer_id)
    if location_id:
        stmt = stmt.where(h.location_id == location_id)
    if date_from:
        stmt = stmt.where(h.doc_date >= date_from)
    if date_to:
        stmt = stmt.where(h.doc_date <= date_to)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(h.doc_no.ilike(like), h.customer_ref_no.ilike(like)))
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    rows = db.scalars(
        stmt.order_by(h.doc_date.desc(), h.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return list(rows), total


# --- building -----------------------------------------------------------------------------


def _check_header_refs(db: Session, p: Principal, v: dict[str, Any]) -> None:
    if "customer_id" in v:
        c = db.get(Customer, v["customer_id"])
        if c is None or c.company_id != p.company_id:
            raise BadRequest("Customer not found")
        if not c.is_active:
            raise BusinessRule("Customer is inactive", {"customer": c.code})
    if "location_id" in v:
        loc = db.get(Location, v["location_id"])
        if loc is None or loc.company_id != p.company_id or not p.can_see_location(loc.id):
            raise BadRequest("Location not found")
    if v.get("ref_invoice_id"):
        inv = db.get(SalesInvoice, v["ref_invoice_id"])
        if inv is None or inv.company_id != p.company_id:
            raise BadRequest("Referenced invoice not found")


def _invoice_terms(
    db: Session, invoice_id: uuid.UUID | None
) -> dict[uuid.UUID, tuple[Decimal, Decimal]]:
    """Per item on a posted invoice: (net price per base unit, cost per base unit)."""
    if not invoice_id:
        return {}
    L = SalesInvoiceLine
    rows = db.execute(
        select(
            L.item_id,
            func.sum(L.line_subtotal),
            func.sum(L.base_qty * L.unit_cost),
            func.sum(L.base_qty),
        )
        .join(SalesInvoice, SalesInvoice.id == L.header_id)
        .where(SalesInvoice.id == invoice_id, SalesInvoice.status == "POSTED")
        .group_by(L.item_id)
    ).all()
    return {item: (value / qty, cost / qty) for item, value, cost, qty in rows if qty}


def _do_lines(db: Session, ids: set[uuid.UUID], *, lock: bool = False):
    if not ids:
        return {}
    stmt = (
        select(DeliveryOrderLine)
        .where(DeliveryOrderLine.id.in_(ids))
        .options(selectinload(DeliveryOrderLine.header))
        .order_by(DeliveryOrderLine.id)  # fixed lock order
    )
    if lock:
        stmt = stmt.with_for_update(of=DeliveryOrderLine)
    return {ln.id: ln for ln in db.scalars(stmt)}


def _invoiced_qty(
    db: Session, do_line_ids: set[uuid.UUID], exclude_invoice: uuid.UUID | None = None
) -> dict[uuid.UUID, Decimal]:
    """Base quantity of each DO line already on saved or posted invoices."""
    if not do_line_ids:
        return {}
    stmt = (
        select(SalesInvoiceLine.do_line_id, func.sum(SalesInvoiceLine.base_qty))
        .join(SalesInvoice, SalesInvoice.id == SalesInvoiceLine.header_id)
        .where(SalesInvoiceLine.do_line_id.in_(do_line_ids), SalesInvoice.status.in_(ACTIVE))
        .group_by(SalesInvoiceLine.do_line_id)
    )
    if exclude_invoice:
        stmt = stmt.where(SalesInvoice.id != exclude_invoice)
    return dict(db.execute(stmt).all())


def _build_lines(
    db: Session,
    p: Principal,
    k: Kind,
    lines: list[dict[str, Any]],
    *,
    customer_id: uuid.UUID,
    ref_invoice_id: uuid.UUID | None = None,
) -> list:
    items = {
        i.id: i
        for i in db.scalars(
            select(Item)
            .where(Item.company_id == p.company_id, Item.id.in_({ln["item_id"] for ln in lines}))
            .options(selectinload(Item.uoms))
        )
    }
    taxes = {t.id: t for t in db.scalars(select(TaxCode).where(TaxCode.company_id == p.company_id))}
    terms = _invoice_terms(db, ref_invoice_id) if k.key == "customer-cn" else {}
    do_lines = (
        _do_lines(db, {ln["do_line_id"] for ln in lines if ln.get("do_line_id")})
        if k.key == "sales-invoice"
        else {}
    )
    out = []
    for n, ln in enumerate(lines, start=1):
        item = items.get(ln["item_id"])
        if item is None:
            raise BadRequest("Item not found", {"line": n})
        uom: ItemUom | None = next((u for u in item.uoms if u.uom == ln["uom"]), None)
        if uom is None:
            raise BadRequest(
                "Unit is not one of the item's units",
                {"line": n, "item": item.code, "allowed": [u.uom for u in item.uoms]},
            )
        tax_id = ln.get("tax_code_id", item.sales_tax_code_id)
        tax = taxes.get(tax_id) if tax_id else None
        if tax_id and tax is None:
            raise BadRequest("Tax code not found", {"line": n})

        extra: dict[str, Any] = {}
        if k.key == "sales-invoice" and ln.get("do_line_id"):
            dl = do_lines.get(ln["do_line_id"])
            if (
                dl is None
                or dl.header.company_id != p.company_id
                or dl.header.status != "POSTED"
                or dl.header.customer_id != customer_id
                or dl.item_id != item.id
            ):
                raise BadRequest(
                    "DO line must be a posted DO line of this customer for the same item",
                    {"line": n},
                )
            extra["do_line_id"] = dl.id

        qty = Decimal(ln["qty"])
        disc = Decimal(ln.get("discount_pct") or 0)
        if ln.get("unit_price") is not None:
            price, source = Decimal(ln["unit_price"]), "DOC"
        elif k.key == "customer-cn":
            if item.id in terms:
                price, disc, source = q4(terms[item.id][0] * uom.factor), Decimal(0), "INVOICE"
            else:
                price, source = Decimal(0), "MISSING"
        elif uom.selling_price is not None:
            price, source = uom.selling_price, "LIST"
        else:
            price, source = Decimal(0), "MISSING"
        rate = tax.rate if tax else Decimal(0)
        subtotal, line_tax = line_amounts(qty, price, disc, rate)
        out.append(
            k.line(
                line_no=n,
                item_id=item.id,
                uom=uom.uom,
                qty=qty,
                factor=uom.factor,
                base_qty=q4(qty * uom.factor),
                unit_price=price,
                price_source=source,
                discount_pct=disc,
                tax_code_id=tax.id if tax else None,
                tax_rate=rate,
                line_subtotal=subtotal,
                line_tax=line_tax,
                line_total=subtotal + line_tax,
                unit_cost=Decimal(0),
                remark=ln.get("remark"),
                **extra,
            )
        )
    return out


def _line_input(ln, *, keep_price: bool = True) -> dict[str, Any]:
    own = ln.price_source == "DOC"
    return {
        "item_id": ln.item_id,
        "uom": ln.uom,
        "qty": ln.qty,
        "unit_price": ln.unit_price if (own and keep_price) else None,
        "discount_pct": ln.discount_pct if own else 0,
        "tax_code_id": ln.tax_code_id,
        "remark": ln.remark,
        "do_line_id": getattr(ln, "do_line_id", None),
    }


def _snapshot(doc) -> dict[str, Any]:
    return audit.snapshot(
        doc,
        lines=[
            {
                "item_id": ln.item_id,
                "uom": ln.uom,
                "qty": ln.qty,
                "unit_price": ln.unit_price,
                "discount_pct": ln.discount_pct,
                "line_total": ln.line_total,
            }
            for ln in doc.lines
        ],
    )


# --- validation on save -------------------------------------------------------------------


def _outstanding(db: Session, p: Principal, customer_id: uuid.UUID) -> Decimal:
    """Posted invoices minus posted customer CNs. Receipts will be subtracted once they exist."""

    def total(h) -> Decimal:
        return db.scalar(
            select(func.coalesce(func.sum(h.total), 0)).where(
                h.company_id == p.company_id, h.customer_id == customer_id, h.status == "POSTED"
            )
        )

    return total(SalesInvoice) - total(CustomerCn)


def _check_do_quantities(db: Session, doc) -> None:
    """A DO line is never invoiced beyond what was delivered. Locks the DO lines so two
    invoices saved at the same time cannot both take the same quantity."""
    wanted: dict[uuid.UUID, Decimal] = {}
    for ln in doc.lines:
        if ln.do_line_id:
            wanted[ln.do_line_id] = wanted.get(ln.do_line_id, Decimal(0)) + ln.base_qty
    if not wanted:
        return
    do_lines = _do_lines(db, set(wanted), lock=True)
    done = _invoiced_qty(db, set(wanted), exclude_invoice=doc.id)
    over = []
    for line_id, qty in wanted.items():
        dl = do_lines.get(line_id)
        if dl is None or dl.header.status != "POSTED":
            raise BusinessRule("The DO behind this invoice is no longer posted")
        left = dl.base_qty - done.get(line_id, Decimal(0))
        if qty > left:
            over.append(
                {
                    "do_no": dl.header.doc_no,
                    "do_line": dl.line_no,
                    "left": str(left),
                    "asked": str(qty),
                }
            )
    if over:
        raise BusinessRule("More than delivered on the DO would be invoiced", {"lines": over})


def _validate_for_save(db: Session, p: Principal, k: Kind, doc) -> list[dict[str, Any]]:
    check_common(db, p, doc)
    missing = [ln.line_no for ln in doc.lines if ln.price_source == "MISSING"]
    if missing:
        msg = (
            "Return price missing: enter the price, or the original invoice so its price is used"
            if k.key == "customer-cn"
            else "Selling price missing: enter a price, or set the item's selling price"
        )
        raise BusinessRule(msg, {"lines": missing})

    if k.key == "quotation" and doc.valid_until and doc.valid_until < doc.doc_date:
        raise BusinessRule("Valid-until date is before the quotation date")
    if k.key == "customer-cn" and doc.ref_invoice_id:
        inv = db.get(SalesInvoice, doc.ref_invoice_id)
        if inv.status != "POSTED" or inv.customer_id != doc.customer_id:
            raise BusinessRule(
                "The referenced invoice must be a posted invoice of the same customer",
                {"invoice_no": inv.doc_no, "status": inv.status},
            )
    if k.key == "sales-invoice":
        _check_do_quantities(db, doc)

    warnings: list[dict[str, Any]] = []
    if k.direction < 0 or k.key == "quotation":
        warnings += _below_cost(db, p, doc)
    if k.key == "sales-invoice":
        customer = db.get(Customer, doc.customer_id)
        if customer.credit_limit is not None:
            outstanding = _outstanding(db, p, doc.customer_id)
            if outstanding + doc.total > customer.credit_limit:
                w: dict[str, Any] = {
                    "code": "CREDIT_LIMIT",
                    "message": "This invoice takes the customer over the credit limit",
                }
                if p.has("field.finance.read"):
                    w |= {
                        "credit_limit": str(customer.credit_limit),
                        "outstanding": str(outstanding),
                        "this_invoice": str(doc.total),
                    }
                warnings.append(w)
    return warnings


def _below_cost(db: Session, p: Principal, doc) -> list[dict[str, Any]]:
    avg = dict(
        db.execute(
            select(StockBalance.item_id, StockBalance.avg_cost).where(
                StockBalance.location_id == doc.location_id,
                StockBalance.item_id.in_({ln.item_id for ln in doc.lines}),
            )
        ).all()
    )
    low = [
        ln
        for ln in doc.lines
        if ln.base_qty and avg.get(ln.item_id) and ln.line_subtotal / ln.base_qty < avg[ln.item_id]
    ]
    if not low:
        return []
    w: dict[str, Any] = {
        "code": "BELOW_COST",
        "message": "Selling below the average cost",
        "lines": [ln.line_no for ln in low],
    }
    if p.has("field.cost.read"):
        w["detail"] = [
            {
                "line": ln.line_no,
                "net_price_per_unit": str(q4(ln.line_subtotal / ln.base_qty)),
                "avg_cost": str(avg[ln.item_id]),
            }
            for ln in low
        ]
    return [w]


# --- commands -----------------------------------------------------------------------------


def create_doc(db: Session, p: Principal, kind: str, *, lines: list[dict[str, Any]], **v: Any):
    k = _kind(kind)
    p.require("sal.doc.create")
    _check_header_refs(db, p, v)
    doc = k.header(
        company_id=p.company_id, status="DRAFT", created_by=p.user_id, updated_by=p.user_id, **v
    )
    doc.lines = _build_lines(
        db, p, k, lines, customer_id=doc.customer_id, ref_invoice_id=v.get("ref_invoice_id")
    )
    set_totals(doc)
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
    p.require("sal.doc.create")
    doc = get_doc(db, p, kind, doc_id, lock=True)
    if doc.status not in EDITABLE:
        raise DocState(f"A {doc.status} document cannot be edited", {"status": doc.status})
    if doc.version != version:
        raise VersionConflict("This document was changed by someone else; reload it")
    before = _snapshot(doc)
    _check_header_refs(db, p, v)
    for key, val in v.items():
        if val is None and key in ("doc_date", "customer_id", "location_id"):
            raise BadRequest("Field cannot be empty", {"field": key})
        setattr(doc, key, val)
    if lines is None and {"customer_id", "ref_invoice_id"} & v.keys():
        # Re-derive prices that were not typed (list / invoice prices) and re-check DO links.
        lines = [_line_input(ln) for ln in doc.lines]
    if lines is not None:
        doc.lines = _build_lines(
            db,
            p,
            k,
            lines,
            customer_id=doc.customer_id,
            ref_invoice_id=getattr(doc, "ref_invoice_id", None),
        )
        db.flush()
    set_totals(doc)
    warnings: list[dict[str, Any]] = []
    if doc.status == "SAVED":
        warnings = _validate_for_save(db, p, k, doc)
        _derive_on_save(db, k, doc)
    doc.version += 1
    doc.updated_by = p.user_id
    db.flush()
    audit.record(db, p, "update", k.entity, doc.id, before=before, after=_snapshot(doc))
    db.commit()
    return get_doc(db, p, kind, doc.id), warnings


def _derive_on_save(db: Session, k: Kind, doc) -> None:
    if k.key == "sales-invoice":
        doc.due_date = doc.doc_date + timedelta(days=db.get(Customer, doc.customer_id).term_days)


def delete_doc(db: Session, p: Principal, kind: str, doc_id: uuid.UUID) -> None:
    k = _kind(kind)
    p.require("sal.doc.create")
    doc = get_doc(db, p, kind, doc_id, lock=True)
    if doc.status != "DRAFT":
        raise DocState("Only drafts can be deleted; void it instead", {"status": doc.status})
    audit.record(db, p, "delete", k.entity, doc.id, before=_snapshot(doc))
    db.delete(doc)
    db.commit()


def save_doc(db: Session, p: Principal, kind: str, doc_id: uuid.UUID, version: int):
    k = _kind(kind)
    p.require("sal.doc.create")
    doc = get_doc(db, p, kind, doc_id, lock=True)
    if doc.status != "DRAFT":
        raise DocState("Only drafts can be saved", {"status": doc.status})
    if doc.version != version:
        raise VersionConflict("This document was changed by someone else; reload it")
    warnings = _validate_for_save(db, p, k, doc)
    _derive_on_save(db, k, doc)
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


def _moves(db: Session, k: Kind, doc) -> list[stock.Move]:
    if k.key == "customer-cn":
        terms = _invoice_terms(db, doc.ref_invoice_id)
        return [
            stock.Move(
                item_id=ln.item_id,
                location_id=doc.location_id,
                qty=ln.base_qty,
                # back in at the cost it left with; unknown -> current average
                unit_cost=q4(terms[ln.item_id][1]) if ln.item_id in terms else None,
                line_id=ln.id,
            )
            for ln in doc.lines
        ]
    moves = []
    if k.key == "sales-invoice":
        delivered = _do_lines(db, {ln.do_line_id for ln in doc.lines if ln.do_line_id})
    for ln in doc.lines:
        if k.key == "sales-invoice" and ln.do_line_id:
            ln.unit_cost = delivered[ln.do_line_id].unit_cost  # already left with the DO
            continue
        moves.append(
            stock.Move(
                item_id=ln.item_id,
                location_id=doc.location_id,
                qty=-ln.base_qty,
                unit_cost=None,  # out at moving average
                line_id=ln.id,
            )
        )
    return moves


def post_doc(db: Session, p: Principal, kind: str, doc_id: uuid.UUID, version: int):
    k = _kind(kind)
    p.require("sal.doc.post")
    doc = get_doc(db, p, kind, doc_id, lock=True)  # row lock: no double posting
    if k.direction == 0:
        raise DocState("Quotations are not posted; convert it into a DO or an invoice")
    if doc.status != "SAVED":
        raise DocState("Only saved documents can be posted", {"status": doc.status})
    if doc.version != version:
        raise VersionConflict("This document was changed by someone else; reload it")
    if k.key == "sales-invoice":
        _check_do_quantities(db, doc)

    moves = _moves(db, k, doc)
    if moves:
        rows = stock.apply_moves(
            db,
            company_id=p.company_id,
            doc_type=k.doc_type,
            doc_id=doc.id,
            doc_no=doc.doc_no,
            moves=moves,
        )
        cost = {r.line_id: r.unit_cost for r in rows}
        for ln in doc.lines:
            if ln.id in cost:
                ln.unit_cost = cost[ln.id]
        db.add(
            PostingJob(
                company_id=p.company_id,
                doc_type=k.doc_type,
                doc_id=doc.id,
                step="STOCK",
                status="DONE",
                attempts=1,
            )
        )
    doc.cost_total = q2(sum((ln.base_qty * ln.unit_cost for ln in doc.lines), Decimal(0)))
    doc.status = "POSTED"
    doc.posted_at = datetime.now(UTC)
    doc.posted_by = p.user_id
    doc.version += 1
    doc.updated_by = p.user_id
    audit.record(db, p, "post", k.entity, doc.id, after={"doc_no": doc.doc_no, "total": doc.total})
    db.commit()
    return get_doc(db, p, kind, doc.id)


def _dependants(db: Session, k: Kind, doc) -> list[str]:
    """Active documents that rely on this one and must be voided first."""
    if k.key == "delivery-order":
        return list(
            db.scalars(
                select(SalesInvoice.doc_no)
                .join(SalesInvoiceLine, SalesInvoiceLine.header_id == SalesInvoice.id)
                .where(
                    SalesInvoiceLine.do_line_id.in_([ln.id for ln in doc.lines]),
                    SalesInvoice.status.in_(ACTIVE),
                )
                .distinct()
            )
        )
    if k.key == "sales-invoice":
        return list(
            db.scalars(
                select(CustomerCn.doc_no).where(
                    CustomerCn.ref_invoice_id == doc.id, CustomerCn.status.in_(ACTIVE)
                )
            )
        )
    return []


def void_doc(db: Session, p: Principal, kind: str, doc_id: uuid.UUID, version: int, reason: str):
    k = _kind(kind)
    p.require("sal.doc.void")
    doc = get_doc(db, p, kind, doc_id, lock=True)
    if doc.status not in ACTIVE:
        raise DocState("Only saved or posted documents can be voided", {"status": doc.status})
    if doc.version != version:
        raise VersionConflict("This document was changed by someone else; reload it")
    blocking = _dependants(db, k, doc)
    if blocking:
        raise DocState("Void the documents made from this one first", {"documents": blocking})
    was = doc.status
    if was == "POSTED" and k.direction != 0:
        stock.reverse_doc(
            db, company_id=p.company_id, doc_type=k.doc_type, doc_id=doc.id, doc_no=doc.doc_no
        )
    doc.status = "VOIDED"
    doc.voided_at = datetime.now(UTC)
    doc.voided_by = p.user_id
    doc.void_reason = reason
    doc.version += 1
    doc.updated_by = p.user_id
    audit.record(db, p, "void", k.entity, doc.id, before={"status": was}, after={"reason": reason})
    db.commit()
    return get_doc(db, p, kind, doc.id)


def convert(
    db: Session,
    p: Principal,
    target: str,
    source_kind: str,
    source_id: uuid.UUID,
    doc_date: date | None = None,
):
    """Start a draft `target` from a quotation, DO or invoice (copying its lines)."""
    _kind(target)
    need = CONVERSIONS.get((source_kind, target))
    if need is None:
        raise BadRequest(
            "This conversion is not supported",
            {"allowed": [f"{s} -> {t}" for s, t in CONVERSIONS]},
        )
    src = get_doc(db, p, source_kind, source_id)
    if src.status != need:
        raise DocState(f"Only a {need} {source_kind} can be converted", {"status": src.status})

    if source_kind == "delivery-order":
        done = _invoiced_qty(db, {ln.id for ln in src.lines})
        lines = []
        for ln in src.lines:
            left = ln.base_qty - done.get(ln.id, Decimal(0))
            if left <= 0:
                continue
            row = _line_input(ln)
            row["unit_price"] = ln.unit_price  # the price agreed on the DO, whatever its source
            row["discount_pct"] = ln.discount_pct
            row["qty"] = q4(left / ln.factor)
            row["do_line_id"] = ln.id
            lines.append(row)
        if not lines:
            raise BusinessRule("Everything on this DO is already invoiced")
    else:
        lines = []
        for ln in src.lines:
            row = _line_input(ln)
            row["unit_price"], row["discount_pct"] = ln.unit_price, ln.discount_pct
            row["do_line_id"] = None
            lines.append(row)

    header: dict[str, Any] = {
        "doc_date": doc_date or today(db, p),
        "customer_id": src.customer_id,
        "location_id": src.location_id,
        "customer_ref_no": src.customer_ref_no,
        "source_kind": source_kind,
        "source_id": src.id,
    }
    if target == "customer-cn":
        header["ref_invoice_id"] = src.id
    return create_doc(db, p, target, lines=lines, **header)
