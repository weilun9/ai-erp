"""Purchasing documents: purchase order (PO), GRN (goods received + supplier invoice) and
supplier CN (return).

A PO never moves stock: posting it means it is issued to the supplier. GRNs receive against
PO lines (po_line_id), partially if need be; what is not received yet is "on order" until
the PO is fully received or closed by hand.

Lifecycle (design doc section 4):
    DRAFT  --save-->  SAVED  --post-->  POSTED  --void-->  VOIDED
      |                 |------------------void----------------^
      +--delete (drafts only)
- DRAFT: anything may change; the AI's drafts wait here.
- SAVED: validated, document number assigned; still editable (re-validated).
- POSTED: stock written in the same transaction (STOCK step); read-only.
- VOIDED: never deleted; posted documents get reversing ledger rows.
"""

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select
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
    Grn,
    GrnLine,
    Item,
    ItemUom,
    Location,
    PostingJob,
    PurchaseOrder,
    PurchaseOrderLine,
    Supplier,
    SupplierCn,
    SupplierCnLine,
    TaxCode,
)
from app.services import audit, stock
from app.services import knockoff as ko
from app.services.auth import Principal
from app.services.doclib import EDITABLE, check_common, line_amounts, q2
from app.services.doclib import q4 as _q4
from app.services.doclib import today as _today
from app.services.numbering import next_doc_no


@dataclass(frozen=True)
class Kind:
    key: str
    header: type
    line: type
    doc_type: str
    direction: int  # +1 stock in, -1 stock out
    entity: str
    profile_pattern: str  # supplier profile key for supplier_ref_no format ("" = none)


KINDS: dict[str, Kind] = {
    "purchase-order": Kind(
        "purchase-order", PurchaseOrder, PurchaseOrderLine, "PO", 0, "pur.purchase_order", ""
    ),
    "grn": Kind("grn", Grn, GrnLine, "GRN", +1, "pur.grn", "invoice_no_pattern"),
    "supplier-cn": Kind(
        "supplier-cn", SupplierCn, SupplierCnLine, "SCN", -1, "pur.supplier_cn", "cn_no_pattern"
    ),
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
        # Always reload from the DB so callers see stored values (rounding, defaults).
        .execution_options(populate_existing=True)
    )
    if p.location_ids is not None:
        stmt = stmt.where(k.header.location_id.in_(p.location_ids))
    return stmt


def get_doc(db: Session, p: Principal, kind: str, doc_id: uuid.UUID, *, lock: bool = False):
    k = _kind(kind)
    p.require("pur.doc.read")
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
    location_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 50,
):
    k = _kind(kind)
    p.require("pur.doc.read")
    h = k.header
    stmt = _query(k, p)
    if status:
        stmt = stmt.where(h.status == status)
    if supplier_id:
        stmt = stmt.where(h.supplier_id == supplier_id)
    if location_id:
        stmt = stmt.where(h.location_id == location_id)
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


def _check_header_refs(db: Session, p: Principal, v: dict[str, Any]) -> None:
    if "supplier_id" in v:
        s = db.get(Supplier, v["supplier_id"])
        if s is None or s.company_id != p.company_id:
            raise BadRequest("Supplier not found")
        if not s.is_active:
            raise BusinessRule("Supplier is inactive", {"supplier": s.code})
    if "location_id" in v:
        loc = db.get(Location, v["location_id"])
        if loc is None or loc.company_id != p.company_id or not p.can_see_location(loc.id):
            raise BadRequest("Location not found")


def _invoice_costs(
    db: Session, p: Principal, supplier_id: uuid.UUID | None, invoice_no: str | None
) -> dict[uuid.UUID, Decimal]:
    """Cost per base unit of each item on the supplier's posted invoice (GRN) invoice_no."""
    if not supplier_id or not invoice_no or not invoice_no.strip():
        return {}
    rows = db.execute(
        select(GrnLine.item_id, func.sum(GrnLine.line_subtotal), func.sum(GrnLine.base_qty))
        .join(Grn, Grn.id == GrnLine.header_id)
        .where(
            Grn.company_id == p.company_id,
            Grn.supplier_id == supplier_id,
            Grn.status == "POSTED",
            func.upper(func.trim(Grn.supplier_ref_no)) == invoice_no.strip().upper(),
        )
        .group_by(GrnLine.item_id)
    ).all()
    return {item_id: value / qty for item_id, value, qty in rows if qty}


def _build_lines(
    db: Session,
    p: Principal,
    k: Kind,
    lines: list[dict[str, Any]],
    *,
    supplier_id: uuid.UUID | None = None,
    ref_invoice_no: str | None = None,
) -> list:
    """Price rule for returns: the CN's own price first; if the CN has none, the price on
    the original invoice (ref_invoice_no); otherwise MISSING, which blocks saving."""
    invoice_costs = _invoice_costs(db, p, supplier_id, ref_invoice_no) if k.direction < 0 else {}
    item_ids = {ln["item_id"] for ln in lines}
    items = {
        i.id: i
        for i in db.scalars(
            select(Item)
            .where(Item.company_id == p.company_id, Item.id.in_(item_ids))
            .options(selectinload(Item.uoms))
        )
    }
    taxes = {t.id: t for t in db.scalars(select(TaxCode).where(TaxCode.company_id == p.company_id))}
    last_cost = _last_costs(db, p, item_ids) if k.direction == 0 else {}
    po_lines = (
        _po_lines(db, {ln["po_line_id"] for ln in lines if ln.get("po_line_id")})
        if k.key == "grn"
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
        tax_id = ln.get("tax_code_id", item.purchase_tax_code_id)
        tax = taxes.get(tax_id) if tax_id else None
        if tax_id and tax is None:
            raise BadRequest("Tax code not found", {"line": n})

        extra: dict[str, Any] = {}
        pol = None
        if ln.get("po_line_id"):
            pol = po_lines.get(ln["po_line_id"])
            if (
                pol is None
                or pol.header.company_id != p.company_id
                or pol.header.status != "POSTED"
                or pol.header.supplier_id != supplier_id
                or pol.item_id != item.id
            ):
                raise BadRequest(
                    "PO line must be on an issued PO of this supplier for the same item",
                    {"line": n},
                )
            extra["po_line_id"] = pol.id

        qty = Decimal(ln["qty"])
        disc = Decimal(ln.get("discount_pct") or 0)
        if ln.get("unit_price") is not None:
            price, source = Decimal(ln["unit_price"]), "DOC"
        elif pol is not None and pol.uom == uom.uom:
            price, disc, source = pol.unit_price, pol.discount_pct, "PO"
        elif k.direction == 0:
            # A PO may leave the price out: the item's purchase price, else the last price
            # paid (cost per base unit x this unit's factor), else missing.
            if uom.purchase_price is not None:
                price, source = uom.purchase_price, "LIST"
            elif item.id in last_cost:
                price, source = _q4(last_cost[item.id] * uom.factor), "LAST"
            else:
                price, source = Decimal(0), "MISSING"
        elif k.direction > 0:
            raise BadRequest("Unit price is required", {"line": n})
        elif item.id in invoice_costs:
            price, disc, source = _q4(invoice_costs[item.id] * uom.factor), Decimal(0), "INVOICE"
        else:
            price, source = Decimal(0), "MISSING"
        rate = tax.rate if tax else Decimal(0)
        subtotal, line_tax = line_amounts(qty, price, disc, rate)
        base_qty = _q4(qty * uom.factor)
        out.append(
            k.line(
                line_no=n,
                item_id=item.id,
                uom=uom.uom,
                qty=qty,
                factor=uom.factor,
                base_qty=base_qty,
                unit_price=price,
                price_source=source,
                discount_pct=disc,
                tax_code_id=tax.id if tax else None,
                tax_rate=rate,
                price_incl_tax=bool(ln.get("price_incl_tax")),
                line_subtotal=subtotal,
                line_tax=line_tax,
                line_total=subtotal + line_tax,
                # cost per base unit, net of discount, excluding tax (SST is not cost here)
                unit_cost=_q4(subtotal / base_qty) if base_qty else Decimal(0),
                remark=ln.get("remark"),
                **extra,
            )
        )
    return out


def _last_costs(db: Session, p: Principal, item_ids: set) -> dict:
    """Net cost per base unit on each item's latest posted GRN line."""
    rows = db.execute(
        select(GrnLine.item_id, GrnLine.unit_cost)
        .join(Grn, Grn.id == GrnLine.header_id)
        .where(
            Grn.company_id == p.company_id, Grn.status == "POSTED", GrnLine.item_id.in_(item_ids)
        )
        .order_by(GrnLine.item_id, Grn.doc_date.desc(), Grn.posted_at.desc())
        .distinct(GrnLine.item_id)
    ).all()
    return dict(rows)


def _settle(doc) -> None:
    """Line and header amounts from the line inputs, the header discount and the cent
    adjustment. Tax-inclusive lines are split into net + tax first; the header discount is
    then spread over the lines by net value (the last line takes the cents left over), so
    each line's cost and tax are net of it. Totals: subtotal + tax + rounding_adj."""
    hundred = Decimal(100)
    parts = []
    for ln in doc.lines:
        gross = q2(ln.qty * ln.unit_price * (hundred - ln.discount_pct) / hundred)
        pre = q2(gross * hundred / (hundred + ln.tax_rate)) if ln.price_incl_tax else gross
        parts.append((ln, gross, pre))
    total_pre = sum((pre for _, _, pre in parts), Decimal(0))
    disc = doc.header_discount or Decimal(0)
    if disc < 0:
        raise BadRequest("The document discount cannot be negative")
    if disc > total_pre:
        raise BusinessRule(
            "The document discount is more than the goods are worth",
            {"discount": str(disc), "subtotal": str(total_pre)},
        )
    given = Decimal(0)
    for i, (ln, gross, pre) in enumerate(parts):
        if i == len(parts) - 1:
            alloc = disc - given
        else:
            alloc = q2(disc * pre / total_pre) if total_pre else Decimal(0)
            given += alloc
        net = pre - alloc
        if ln.price_incl_tax and not alloc:
            tax = gross - pre  # keep the supplier's tax-inclusive amount to the cent
        else:
            tax = q2(net * ln.tax_rate / hundred)
        ln.disc_alloc = alloc
        ln.line_subtotal = net
        ln.line_tax = tax
        ln.line_total = net + tax
        ln.unit_cost = _q4(net / ln.base_qty) if ln.base_qty else Decimal(0)
    doc.subtotal = sum((ln.line_subtotal for ln in doc.lines), Decimal(0))
    doc.tax_total = sum((ln.line_tax for ln in doc.lines), Decimal(0))
    doc.total = doc.subtotal + doc.tax_total + (doc.rounding_adj or Decimal(0))
    doc.due_date = (
        doc.doc_date + timedelta(days=doc.term_days) if doc.term_days is not None else None
    )


def _po_lines(db: Session, ids: set, *, lock: bool = False) -> dict:
    if not ids:
        return {}
    stmt = (
        select(PurchaseOrderLine)
        .where(PurchaseOrderLine.id.in_(ids))
        .options(selectinload(PurchaseOrderLine.header))
    )
    if lock:
        stmt = stmt.with_for_update(of=PurchaseOrderLine)
    return {ln.id: ln for ln in db.scalars(stmt)}


def _received_qty(db: Session, po_line_ids: set, exclude_grn=None) -> dict:
    """Base quantity of each PO line already on saved or posted GRNs."""
    if not po_line_ids:
        return {}
    stmt = (
        select(GrnLine.po_line_id, func.sum(GrnLine.base_qty))
        .join(Grn, Grn.id == GrnLine.header_id)
        .where(GrnLine.po_line_id.in_(po_line_ids), Grn.status.in_(ACTIVE))
        .group_by(GrnLine.po_line_id)
    )
    if exclude_grn:
        stmt = stmt.where(Grn.id != exclude_grn)
    return dict(db.execute(stmt).all())


def received_by_line(db: Session, po) -> dict:
    """For a PO: quantity of each line (in the line's own unit) received so far."""
    done = _received_qty(db, {ln.id for ln in po.lines})
    return {ln.id: _q4(done.get(ln.id, Decimal(0)) / ln.factor) for ln in po.lines}


def on_order(
    db: Session, company_id: uuid.UUID, item_ids=None, location_ids=None
) -> dict[tuple[uuid.UUID, uuid.UUID], Decimal]:
    """Base quantity ordered and not yet received, per (item, location): issued POs that
    are not closed; over-deliveries never make it negative."""
    received = (
        select(GrnLine.po_line_id, func.sum(GrnLine.base_qty).label("qty"))
        .join(Grn, Grn.id == GrnLine.header_id)
        .where(Grn.status.in_(ACTIVE), GrnLine.po_line_id.is_not(None))
        .group_by(GrnLine.po_line_id)
        .subquery()
    )
    left = func.greatest(PurchaseOrderLine.base_qty - func.coalesce(received.c.qty, 0), 0)
    stmt = (
        select(PurchaseOrderLine.item_id, PurchaseOrder.location_id, func.sum(left))
        .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.header_id)
        .outerjoin(received, received.c.po_line_id == PurchaseOrderLine.id)
        .where(
            PurchaseOrder.company_id == company_id,
            PurchaseOrder.status == "POSTED",
            PurchaseOrder.closed_at.is_(None),
        )
        .group_by(PurchaseOrderLine.item_id, PurchaseOrder.location_id)
    )
    if item_ids is not None:
        stmt = stmt.where(PurchaseOrderLine.item_id.in_(item_ids))
    if location_ids is not None:
        stmt = stmt.where(PurchaseOrder.location_id.in_(location_ids))
    return {(i, loc): q for i, loc, q in db.execute(stmt).all() if q}


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


def _validate_for_save(db: Session, p: Principal, k: Kind, doc) -> list[dict[str, Any]]:
    """Hard rules raise; soft findings come back as warnings for the user to check."""
    check_common(db, p, doc)
    missing = [ln.line_no for ln in doc.lines if ln.price_source == "MISSING"]
    if missing and k.direction == 0:
        raise BusinessRule(
            "Price missing: enter the price, or set the item's purchase price",
            {"lines": missing},
        )
    if missing:
        raise BusinessRule(
            "Return price missing: enter the price from the CN, or the original invoice "
            "number so the invoice price can be used",
            {"lines": missing, "ref_invoice_no": getattr(doc, "ref_invoice_no", None)},
        )

    warnings: list[dict[str, Any]] = []
    if k.key == "purchase-order":
        if doc.expected_date and doc.expected_date < doc.doc_date:
            raise BusinessRule("Expected delivery date is before the order date")
        return warnings  # supplier_ref_no on a PO is their quotation no.: no checks
    if k.key == "grn":
        warnings += _over_po(db, doc)
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
            raise DuplicateSupplierDoc(
                "This supplier document number is already recorded",
                {"supplier_ref_no": ref, "existing_doc_no": dup},
            )
        supplier = db.get(Supplier, doc.supplier_id)
        pattern = (supplier.profile or {}).get(k.profile_pattern)
        if pattern and not re.match(pattern, ref):
            warnings.append(
                {
                    "code": "SUPPLIER_REF_FORMAT",
                    "message": "Number does not follow this supplier's usual format",
                    "expected_pattern": pattern,
                    "value": ref,
                }
            )
    elif k.key == "grn":
        warnings.append(
            {"code": "SUPPLIER_REF_MISSING", "message": "Supplier invoice number is empty"}
        )
    return warnings


def _over_po(db: Session, doc) -> list[dict[str, Any]]:
    """Suppliers sometimes deliver more than ordered: allowed, but flagged."""
    wanted: dict[uuid.UUID, Decimal] = {}
    for ln in doc.lines:
        if ln.po_line_id:
            wanted[ln.po_line_id] = wanted.get(ln.po_line_id, Decimal(0)) + ln.base_qty
    if not wanted:
        return []
    po_lines = _po_lines(db, set(wanted), lock=True)
    done = _received_qty(db, set(wanted), exclude_grn=doc.id)
    over = []
    for line_id, qty in wanted.items():
        pl = po_lines[line_id]
        left = pl.base_qty - done.get(line_id, Decimal(0))
        if qty > left:
            over.append(
                {
                    "po_no": pl.header.doc_no,
                    "po_line": pl.line_no,
                    "ordered_left": str(_q4(left / pl.factor)),
                    "received": str(_q4(qty / pl.factor)),
                    "uom": pl.uom,
                }
            )
    if not over:
        return []
    return [{"code": "OVER_PO", "message": "More received than is left on the PO", "lines": over}]


# --- commands -----------------------------------------------------------------------------


def create_doc(db: Session, p: Principal, kind: str, *, lines: list[dict[str, Any]], **v: Any):
    k = _kind(kind)
    p.require("pur.doc.create")
    _check_header_refs(db, p, v)
    if k.key == "grn" and v.get("term_days") is None:
        v["term_days"] = db.get(Supplier, v["supplier_id"]).term_days
    doc = k.header(
        company_id=p.company_id,
        status="DRAFT",
        created_by=p.user_id,
        updated_by=p.user_id,
        **v,
    )
    doc.lines = _build_lines(
        db, p, k, lines, supplier_id=doc.supplier_id, ref_invoice_no=v.get("ref_invoice_no")
    )
    _settle(doc)
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
    p.require("pur.doc.create")
    doc = get_doc(db, p, kind, doc_id, lock=True)
    if doc.status not in EDITABLE:
        raise DocState(f"A {doc.status} document cannot be edited", {"status": doc.status})
    if doc.version != version:
        raise VersionConflict("This document was changed by someone else; reload it")
    before = _snapshot(doc)
    _check_header_refs(db, p, v)
    for key, val in v.items():
        if val is None and key in ("doc_date", "supplier_id", "location_id", "currency"):
            raise BadRequest("Field cannot be empty", {"field": key})
        setattr(doc, key, val)
    reprice = k.direction < 0 and bool({"ref_invoice_no", "supplier_id"} & v.keys())
    if lines is None and reprice:
        # Re-derive invoice prices for lines that did not carry their own price.
        lines = [
            {
                "item_id": ln.item_id,
                "uom": ln.uom,
                "qty": ln.qty,
                "unit_price": ln.unit_price if ln.price_source == "DOC" else None,
                "discount_pct": ln.discount_pct if ln.price_source == "DOC" else 0,
                "tax_code_id": ln.tax_code_id,
                "price_incl_tax": ln.price_incl_tax,
                "remark": ln.remark,
                "po_line_id": getattr(ln, "po_line_id", None),
            }
            for ln in doc.lines
        ]
    if lines is not None:
        doc.lines = _build_lines(
            db,
            p,
            k,
            lines,
            supplier_id=doc.supplier_id,
            ref_invoice_no=getattr(doc, "ref_invoice_no", None),
        )
        db.flush()
    _settle(doc)
    warnings: list[dict[str, Any]] = []
    if doc.status == "SAVED":
        warnings = _validate_for_save(db, p, k, doc)
    doc.version += 1
    doc.updated_by = p.user_id
    db.flush()
    audit.record(db, p, "update", k.entity, doc.id, before=before, after=_snapshot(doc))
    db.commit()
    return get_doc(db, p, kind, doc.id), warnings


def delete_doc(db: Session, p: Principal, kind: str, doc_id: uuid.UUID) -> None:
    k = _kind(kind)
    p.require("pur.doc.create")
    doc = get_doc(db, p, kind, doc_id, lock=True)
    if doc.status != "DRAFT":
        raise DocState("Only drafts can be deleted; void it instead", {"status": doc.status})
    audit.record(db, p, "delete", k.entity, doc.id, before=_snapshot(doc))
    db.delete(doc)
    db.commit()


def save_doc(db: Session, p: Principal, kind: str, doc_id: uuid.UUID, version: int):
    k = _kind(kind)
    p.require("pur.doc.create")
    doc = get_doc(db, p, kind, doc_id, lock=True)
    if doc.status != "DRAFT":
        raise DocState("Only drafts can be saved", {"status": doc.status})
    if doc.version != version:
        raise VersionConflict("This document was changed by someone else; reload it")
    warnings = _validate_for_save(db, p, k, doc)
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


def post_doc(db: Session, p: Principal, kind: str, doc_id: uuid.UUID, version: int):
    k = _kind(kind)
    p.require("pur.doc.post")
    doc = get_doc(db, p, kind, doc_id, lock=True)  # row lock: no double posting
    if doc.status != "SAVED":
        raise DocState("Only saved documents can be posted", {"status": doc.status})
    if doc.version != version:
        raise VersionConflict("This document was changed by someone else; reload it")
    if getattr(doc, "completeness", None) == "RECORDED":
        raise BusinessRule("Confirm the returned items before posting this CN")

    if k.direction != 0:
        _post_stock(db, p, k, doc)
    doc.status = "POSTED"
    doc.posted_at = datetime.now(UTC)
    doc.posted_by = p.user_id
    doc.version += 1
    doc.updated_by = p.user_id
    audit.record(db, p, "post", k.entity, doc.id, after={"doc_no": doc.doc_no, "total": doc.total})
    db.commit()
    return get_doc(db, p, kind, doc.id)


def _post_stock(db: Session, p: Principal, k: Kind, doc) -> None:
    stock.apply_moves(
        db,
        company_id=p.company_id,
        doc_type=k.doc_type,
        doc_id=doc.id,
        doc_no=doc.doc_no,
        moves=[
            stock.Move(
                item_id=ln.item_id,
                location_id=doc.location_id,
                qty=ln.base_qty * k.direction,
                # receipts add at cost; returns remove exactly the returned value
                unit_cost=ln.unit_cost,
                line_id=ln.id,
            )
            for ln in doc.lines
        ],
    )
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


def void_doc(db: Session, p: Principal, kind: str, doc_id: uuid.UUID, version: int, reason: str):
    k = _kind(kind)
    p.require("pur.doc.void")
    doc = get_doc(db, p, kind, doc_id, lock=True)
    if doc.status not in ("SAVED", "POSTED"):
        raise DocState("Only saved or posted documents can be voided", {"status": doc.status})
    if doc.version != version:
        raise VersionConflict("This document was changed by someone else; reload it")
    was = doc.status
    if k.key == "purchase-order":
        used = _received_qty(db, {ln.id for ln in doc.lines})
        if any(used.values()):
            raise BusinessRule(
                "Goods were already received on this PO: void those GRNs first, or close the PO"
            )
    elif was == "POSTED":
        ko.release_for_void(db, [doc.id], doc.id, p.user_id)
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


def close_po(db: Session, p: Principal, doc_id: uuid.UUID, version: int, reopen: bool = False):
    """Stop waiting for the rest of an issued PO (or reopen it): it leaves "on order"."""
    p.require("pur.doc.post")
    doc = get_doc(db, p, "purchase-order", doc_id, lock=True)
    if doc.status != "POSTED":
        raise DocState("Only an issued (posted) PO can be closed", {"status": doc.status})
    if doc.version != version:
        raise VersionConflict("This document was changed by someone else; reload it")
    if reopen:
        doc.closed_at, doc.closed_by = None, None
    else:
        doc.closed_at, doc.closed_by = datetime.now(UTC), p.user_id
    doc.version += 1
    doc.updated_by = p.user_id
    audit.record(
        db,
        p,
        "reopen" if reopen else "close",
        "pur.purchase_order",
        doc.id,
        after={"doc_no": doc.doc_no},
    )
    db.commit()
    return get_doc(db, p, "purchase-order", doc.id)


def receive_po(
    db: Session,
    p: Principal,
    po_id: uuid.UUID,
    *,
    doc_date: date | None = None,
    pick: list[dict[str, Any]] | None = None,
):
    """Start a draft GRN from an issued PO. `pick` = [{"source_line_id", "qty"}] in the PO
    line's unit; without it everything not received yet is taken."""
    po = get_doc(db, p, "purchase-order", po_id)
    if po.status != "POSTED":
        raise DocState("Only an issued (posted) PO can be received", {"status": po.status})
    if po.closed_at is not None:
        raise BusinessRule("This PO is closed; reopen it to receive more")
    done = _received_qty(db, {ln.id for ln in po.lines})
    wanted: dict[uuid.UUID, Decimal] | None = None
    if pick is not None:
        known = {ln.id for ln in po.lines}
        wanted = {}
        for row in pick:
            if row["source_line_id"] not in known:
                raise BadRequest("That line is not on this PO")
            if row["source_line_id"] in wanted:
                raise BadRequest("The same PO line is picked twice")
            wanted[row["source_line_id"]] = Decimal(row["qty"])
        if not wanted:
            raise BadRequest("Pick at least one line to receive")
    lines = []
    for ln in po.lines:
        left = max(ln.base_qty - done.get(ln.id, Decimal(0)), Decimal(0))
        if wanted is not None:
            if ln.id not in wanted:
                continue
            qty = wanted[ln.id]  # more than ordered is allowed; saving warns OVER_PO
        elif left <= 0:
            continue
        else:
            qty = _q4(left / ln.factor)
        lines.append(
            {
                "item_id": ln.item_id,
                "uom": ln.uom,
                "qty": qty,
                "unit_price": ln.unit_price,
                "discount_pct": ln.discount_pct,
                "tax_code_id": ln.tax_code_id,
                "price_incl_tax": ln.price_incl_tax,
                "remark": ln.remark,
                "po_line_id": ln.id,
            }
        )
    if not lines:
        raise BusinessRule("Everything on this PO is already received")
    return create_doc(
        db,
        p,
        "grn",
        lines=lines,
        doc_date=doc_date or _today(db, p),
        supplier_id=po.supplier_id,
        location_id=po.location_id,
        remark=f"From {po.doc_no}",
    )


def paid_so_far(db: Session, doc):
    """Amount of a posted GRN / supplier CN already knocked off (payments, CNs, ...)."""
    if doc.status != "POSTED":
        return Decimal(0)
    return ko.knocked(db, [doc.id]).get(doc.id, Decimal(0))
