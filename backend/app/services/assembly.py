"""Bills of materials and assembly.

A bill of materials says what goes into a product: making output_qty base units of the
item takes these components (base units). An assembly at a store makes (ASSEMBLE) or
breaks up (DISASSEMBLE) a quantity of the product:

- ASSEMBLE: the components go out at their moving-average cost (earliest-expiry batches
  first for tracked items); the product comes in at the total of what went out.
- DISASSEMBLE: the product goes out at its average cost; the components come back with that
  value shared out in proportion to their current average cost (or their quantities when
  none has a cost yet).

Stock moves only from one inventory item to another, so the books' inventory value does
not change and no journal is needed. Voiding reverses the stock exactly.
"""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.errors import BadRequest, BusinessRule, DocState, NotFound, VersionConflict
from app.models import Assembly, AssemblyLine, Bom, BomLine, Item, StockBalance
from app.services import audit, stock
from app.services.auth import Principal
from app.services.doclib import q4
from app.services.numbering import next_doc_no

ZERO = Decimal(0)


# --- bills of materials ---------------------------------------------------------------------


def _item(db: Session, p: Principal, item_id: uuid.UUID) -> Item:
    item = db.get(Item, item_id)
    if item is None or item.company_id != p.company_id:
        raise NotFound("Item not found")
    return item


def get_bom(db: Session, p: Principal, item_id: uuid.UUID) -> Bom | None:
    p.require("md.item.read")
    _item(db, p, item_id)
    return db.scalar(
        select(Bom)
        .where(Bom.company_id == p.company_id, Bom.item_id == item_id)
        .options(selectinload(Bom.lines))
    )


def list_boms(db: Session, p: Principal) -> list[Bom]:
    p.require("md.item.read")
    return list(
        db.scalars(
            select(Bom).where(Bom.company_id == p.company_id).options(selectinload(Bom.lines))
        )
    )


def set_bom(
    db: Session,
    p: Principal,
    item_id: uuid.UUID,
    *,
    output_qty: Decimal,
    lines: list[dict[str, Any]],
    note: str | None,
) -> Bom | None:
    """Replace the product's bill of materials (no lines = remove it)."""
    p.require("md.item.manage")
    _item(db, p, item_id)
    bom = get_bom(db, p, item_id)
    before = _bom_snapshot(bom) if bom else None
    if not lines:
        if bom is not None:
            db.delete(bom)
            audit.record(db, p, "delete", "inv.bom", bom.id, before=before)
            db.commit()
        return None
    seen: set[uuid.UUID] = set()
    for n, ln in enumerate(lines, start=1):
        comp = _item(db, p, ln["item_id"])
        if comp.id == item_id:
            raise BusinessRule("A product cannot be one of its own components", {"line": n})
        if comp.id in seen:
            raise BadRequest("A component is listed twice", {"line": n, "item": comp.code})
        seen.add(comp.id)
    if bom is None:
        bom = Bom(company_id=p.company_id, item_id=item_id, created_by=p.user_id)
        db.add(bom)
    else:
        bom.version += 1
    bom.output_qty, bom.note, bom.updated_by = output_qty, note, p.user_id
    bom.lines = [
        BomLine(line_no=n, item_id=ln["item_id"], qty=ln["qty"]) for n, ln in enumerate(lines, 1)
    ]
    db.flush()
    audit.record(
        db,
        p,
        "update" if before else "create",
        "inv.bom",
        bom.id,
        before=before,
        after=_bom_snapshot(bom),
    )
    db.commit()
    return get_bom(db, p, item_id)


def _bom_snapshot(bom: Bom) -> dict[str, Any]:
    return {
        "output_qty": bom.output_qty,
        "lines": [{"item_id": x.item_id, "qty": x.qty} for x in bom.lines],
    }


# --- assembly ----------------------------------------------------------------------------------


def _avg_costs(db: Session, items: set[uuid.UUID], loc: uuid.UUID) -> dict[uuid.UUID, Decimal]:
    return dict(
        db.execute(
            select(StockBalance.item_id, StockBalance.avg_cost).where(
                StockBalance.item_id.in_(items), StockBalance.location_id == loc
            )
        ).all()
    )


def needs(db: Session, p: Principal, item_id: uuid.UUID, qty: Decimal, location_id: uuid.UUID):
    """What making qty of the product takes, with what the store has of each."""
    p.require("inv.balance.read")
    bom = get_bom(db, p, item_id)
    if bom is None:
        raise BusinessRule("This item has no bill of materials")
    have = dict(
        db.execute(
            select(StockBalance.item_id, StockBalance.qty).where(
                StockBalance.item_id.in_([x.item_id for x in bom.lines]),
                StockBalance.location_id == location_id,
            )
        ).all()
    )
    return [
        {
            "item_id": x.item_id,
            "qty": q4(x.qty * qty / bom.output_qty),
            "on_hand": have.get(x.item_id, ZERO),
        }
        for x in bom.lines
    ]


def make(
    db: Session,
    p: Principal,
    *,
    item_id: uuid.UUID,
    qty: Decimal,
    location_id: uuid.UUID,
    doc_date: date,
    direction: str,
    remark: str | None,
    allow_short: bool = False,
) -> Assembly:
    p.require("inv.doc.create", "inv.doc.post")
    if qty <= 0:
        raise BadRequest("Quantity must be above zero")
    if not p.can_see_location(location_id):
        raise NotFound("Store not found")
    bom = get_bom(db, p, item_id)
    if bom is None:
        raise BusinessRule("This item has no bill of materials")
    parts = [(x.item_id, q4(x.qty * qty / bom.output_qty)) for x in bom.lines]

    doc = Assembly(
        company_id=p.company_id,
        doc_no=next_doc_no(db, p.company_id, "ASM"),
        doc_date=doc_date,
        location_id=location_id,
        item_id=item_id,
        direction=direction,
        qty=qty,
        remark=remark,
        posted_at=datetime.now(UTC),
        created_by=p.user_id,
        lines=[AssemblyLine(line_no=n, item_id=i, qty=q) for n, (i, q) in enumerate(parts, 1)],
    )
    db.add(doc)
    db.flush()
    common = {"company_id": p.company_id, "doc_type": "ASM", "doc_id": doc.id, "doc_no": doc.doc_no}

    if direction == "ASSEMBLE":
        rows = stock.apply_moves(
            db,
            moves=[stock.Move(ln.item_id, location_id, -ln.qty, None, ln.id) for ln in doc.lines],
            allow_negative=allow_short,  # making from stock that is not there is a mistake
            **common,
        )
        total = sum((r.unit_cost * ln.qty for r, ln in zip(rows, doc.lines, strict=True)), ZERO)
        for r, ln in zip(rows, doc.lines, strict=True):
            ln.unit_cost = r.unit_cost
        doc.unit_cost = q4(total / qty)
        stock.apply_moves(
            db, moves=[stock.Move(item_id, location_id, qty, doc.unit_cost)], **common
        )
        doc.cost_total = q4(total)
    elif direction == "DISASSEMBLE":
        out = stock.apply_moves(
            db,
            moves=[stock.Move(item_id, location_id, -qty, None)],
            allow_negative=allow_short,
            **common,
        )[0]
        value = out.unit_cost * qty
        costs = _avg_costs(db, {ln.item_id for ln in doc.lines}, location_id)
        weights = [costs.get(ln.item_id, ZERO) * ln.qty for ln in doc.lines]
        if not sum(weights, ZERO):
            weights = [ln.qty for ln in doc.lines]
        whole = sum(weights, ZERO)
        for ln, w in zip(doc.lines, weights, strict=True):
            ln.unit_cost = q4(value * w / whole / ln.qty) if ln.qty else ZERO
        stock.apply_moves(
            db,
            moves=[
                stock.Move(ln.item_id, location_id, ln.qty, ln.unit_cost, ln.id) for ln in doc.lines
            ],
            **common,
        )
        doc.unit_cost, doc.cost_total = out.unit_cost, q4(value)
    else:
        raise BadRequest("Assemble or disassemble")
    audit.record(
        db,
        p,
        "post",
        "inv.assembly",
        doc.id,
        after={"doc_no": doc.doc_no, "direction": direction, "qty": qty},
    )
    db.commit()
    return get(db, p, doc.id)


def get(db: Session, p: Principal, doc_id: uuid.UUID) -> Assembly:
    p.require("inv.balance.read")
    doc = db.scalar(
        select(Assembly).where(Assembly.id == doc_id).options(selectinload(Assembly.lines))
    )
    if doc is None or doc.company_id != p.company_id or not p.can_see_location(doc.location_id):
        raise NotFound("Assembly not found")
    return doc


def list_assemblies(db: Session, p: Principal, limit: int = 100) -> list[Assembly]:
    p.require("inv.balance.read")
    rows = db.scalars(
        select(Assembly)
        .where(Assembly.company_id == p.company_id)
        .options(selectinload(Assembly.lines))
        .order_by(Assembly.created_at.desc())
        .limit(limit)
    ).all()
    return [x for x in rows if p.can_see_location(x.location_id)]


def void(db: Session, p: Principal, doc_id: uuid.UUID, version: int, reason: str) -> Assembly:
    p.require("inv.doc.void")
    doc = get(db, p, doc_id)
    db.refresh(doc, with_for_update=True)
    if doc.status != "POSTED":
        raise DocState("Only posted assemblies can be voided", {"status": doc.status})
    if doc.version != version:
        raise VersionConflict("This assembly was changed by someone else; reload it")
    stock.reverse_doc(db, company_id=p.company_id, doc_type="ASM", doc_id=doc.id, doc_no=doc.doc_no)
    doc.status, doc.voided_at, doc.voided_by, doc.void_reason = (
        "VOIDED",
        datetime.now(UTC),
        p.user_id,
        reason,
    )
    doc.version += 1
    audit.record(db, p, "void", "inv.assembly", doc.id, after={"reason": reason})
    db.commit()
    return get(db, p, doc.id)
