"""Purchasing documents: GRN (goods received + supplier invoice) and supplier CN (return).

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
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
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
from app.localization import pack
from app.models import (
    Company,
    Grn,
    GrnLine,
    Item,
    ItemUom,
    Location,
    PostingJob,
    Supplier,
    SupplierCn,
    SupplierCnLine,
    TaxCode,
)
from app.services import audit, stock
from app.services.auth import Principal
from app.services.numbering import next_doc_no

Q2 = Decimal("0.01")
Q4 = Decimal("0.0001")
HUNDRED = Decimal(100)


@dataclass(frozen=True)
class Kind:
    key: str
    header: type
    line: type
    doc_type: str
    direction: int  # +1 stock in, -1 stock out
    entity: str
    profile_pattern: str  # supplier profile key for supplier_ref_no format


KINDS: dict[str, Kind] = {
    "grn": Kind("grn", Grn, GrnLine, "GRN", +1, "pur.grn", "invoice_no_pattern"),
    "supplier-cn": Kind(
        "supplier-cn", SupplierCn, SupplierCnLine, "SCN", -1, "pur.supplier_cn", "cn_no_pattern"
    ),
}

EDITABLE = ("DRAFT", "SAVED")


def _kind(key: str) -> Kind:
    try:
        return KINDS[key]
    except KeyError as exc:
        raise NotFound("Unknown document type") from exc


def _today(db: Session, p: Principal) -> date:
    offset = pack(db.get(Company, p.company_id).country).UTC_OFFSET_HOURS
    return datetime.now(timezone(timedelta(hours=offset))).date()


def _q2(v: Decimal) -> Decimal:
    return v.quantize(Q2, rounding=ROUND_HALF_UP)


def _q4(v: Decimal) -> Decimal:
    return v.quantize(Q4, rounding=ROUND_HALF_UP)


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

        qty = Decimal(ln["qty"])
        disc = Decimal(ln.get("discount_pct") or 0)
        if ln.get("unit_price") is not None:
            price, source = Decimal(ln["unit_price"]), "DOC"
        elif k.direction > 0:
            raise BadRequest("Unit price is required", {"line": n})
        elif item.id in invoice_costs:
            price, disc, source = _q4(invoice_costs[item.id] * uom.factor), Decimal(0), "INVOICE"
        else:
            price, source = Decimal(0), "MISSING"
        net_price = price * (HUNDRED - disc) / HUNDRED
        subtotal = _q2(qty * net_price)
        rate = tax.rate if tax else Decimal(0)
        line_tax = _q2(subtotal * rate / HUNDRED)
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
                line_subtotal=subtotal,
                line_tax=line_tax,
                line_total=subtotal + line_tax,
                # cost per base unit, net of discount, excluding tax (SST is not cost here)
                unit_cost=_q4(subtotal / base_qty) if base_qty else Decimal(0),
                remark=ln.get("remark"),
            )
        )
    return out


def _set_totals(doc) -> None:
    doc.subtotal = sum((ln.line_subtotal for ln in doc.lines), Decimal(0))
    doc.tax_total = sum((ln.line_tax for ln in doc.lines), Decimal(0))
    doc.total = doc.subtotal + doc.tax_total


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
    if not doc.lines:
        raise BusinessRule("A document needs at least one line")
    missing = [ln.line_no for ln in doc.lines if ln.price_source == "MISSING"]
    if missing:
        raise BusinessRule(
            "Return price missing: enter the price from the CN, or the original invoice "
            "number so the invoice price can be used",
            {"lines": missing, "ref_invoice_no": getattr(doc, "ref_invoice_no", None)},
        )
    today = _today(db, p)
    if doc.doc_date > today:
        raise BusinessRule(
            "Document date is in the future — check the year on the original",
            {"doc_date": doc.doc_date.isoformat(), "today": today.isoformat()},
        )
    inactive = db.scalars(
        select(Item.code).where(
            Item.id.in_({ln.item_id for ln in doc.lines}), Item.is_active.is_(False)
        )
    ).all()
    if inactive:
        raise BusinessRule("Inactive items on the document", {"items": sorted(inactive)})

    warnings: list[dict[str, Any]] = []
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


# --- commands -----------------------------------------------------------------------------


def create_doc(db: Session, p: Principal, kind: str, *, lines: list[dict[str, Any]], **v: Any):
    k = _kind(kind)
    p.require("pur.doc.create")
    _check_header_refs(db, p, v)
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
    _set_totals(doc)
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
                "remark": ln.remark,
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
    _set_totals(doc)
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
    doc.status = "POSTED"
    doc.posted_at = datetime.now(UTC)
    doc.posted_by = p.user_id
    doc.version += 1
    doc.updated_by = p.user_id
    audit.record(db, p, "post", k.entity, doc.id, after={"doc_no": doc.doc_no, "total": doc.total})
    db.commit()
    return get_doc(db, p, kind, doc.id)


def void_doc(db: Session, p: Principal, kind: str, doc_id: uuid.UUID, version: int, reason: str):
    k = _kind(kind)
    p.require("pur.doc.void")
    doc = get_doc(db, p, kind, doc_id, lock=True)
    if doc.status not in ("SAVED", "POSTED"):
        raise DocState("Only saved or posted documents can be voided", {"status": doc.status})
    if doc.version != version:
        raise VersionConflict("This document was changed by someone else; reload it")
    was = doc.status
    if was == "POSTED":
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
