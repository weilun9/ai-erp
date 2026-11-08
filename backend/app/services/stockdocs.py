"""Stock documents: adjustment (ADJ), write-off (WO), transfer (TRF), stocktake (STK).

Lifecycle DRAFT -> SAVED -> POSTED -> VOIDED like every document; voiding a posted one
writes reversing ledger rows at the original cost.
- Adjustment: + lines add stock (at the cost given, else the current average),
  - lines remove it at the average. A reason is required.
- Write-off: removes stock at the average (expired, damaged, lost...). Reason required.
- Transfer: out of `location_id` at the average, into `to_location_id` at that same cost,
  in one transaction (both locations' balances locked together, in a fixed order).
- Stocktake: lines are counted quantities (an item may be counted on several lines, e.g.
  cartons and loose pieces). Posting locks the balances, compares with the system
  quantity at that moment and books the difference at the average. With zero_uncounted
  every item on hand at the location but not counted is taken as zero.
Negative stock is allowed (company decision 2026-11-08).
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session, selectinload

from app.core.errors import BadRequest, BusinessRule, DocState, NotFound, VersionConflict
from app.models import (
    Item,
    ItemUom,
    Location,
    PostingJob,
    StockAdjustment,
    StockAdjustmentLine,
    StockLedger,
    Stocktake,
    StocktakeLine,
    StockTransfer,
    StockTransferLine,
    StockWriteOff,
    StockWriteOffLine,
)
from app.services import audit, stock
from app.services.auth import Principal
from app.services.doclib import EDITABLE, check_common, q2, q4
from app.services.numbering import next_doc_no

ZERO = Decimal(0)

ADJUST_REASONS = ("FOUND", "MISCOUNT", "OPENING", "RECLASS", "OTHER")
WRITEOFF_REASONS = ("EXPIRED", "DAMAGED", "LOST", "THEFT", "SAMPLE", "OWN_USE", "OTHER")


@dataclass(frozen=True)
class Kind:
    key: str
    header: type
    line: type
    doc_type: str
    entity: str
    reasons: tuple[str, ...] | None  # required reason codes; None = optional free text


KINDS = {
    k.key: k
    for k in (
        Kind(
            "adjustment",
            StockAdjustment,
            StockAdjustmentLine,
            "ADJ",
            "inv.adjustment",
            ADJUST_REASONS,
        ),
        Kind("write-off", StockWriteOff, StockWriteOffLine, "WO", "inv.writeoff", WRITEOFF_REASONS),
        Kind("transfer", StockTransfer, StockTransferLine, "TRF", "inv.transfer", None),
        Kind("stocktake", Stocktake, StocktakeLine, "STK", "inv.stocktake", None),
    )
}


def _kind(key: str) -> Kind:
    try:
        return KINDS[key]
    except KeyError as exc:
        raise NotFound("Unknown document type") from exc


# --- reading ------------------------------------------------------------------------------


def _query(k: Kind, p: Principal):
    h = k.header
    stmt = (
        select(h)
        .where(h.company_id == p.company_id)
        .options(selectinload(h.lines))
        .execution_options(populate_existing=True)
    )
    if p.location_ids is not None:
        seen = h.location_id.in_(p.location_ids)
        if k.key == "transfer":  # the receiving store sees it too
            seen = or_(seen, h.to_location_id.in_(p.location_ids))
        stmt = stmt.where(seen)
    return stmt


def get_doc(db: Session, p: Principal, kind: str, doc_id: uuid.UUID, *, lock: bool = False):
    k = _kind(kind)
    p.require("inv.balance.read")
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
    location_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    page: int = 1,
    page_size: int = 50,
):
    k = _kind(kind)
    p.require("inv.balance.read")
    h = k.header
    stmt = _query(k, p)
    if status:
        stmt = stmt.where(h.status == status)
    if location_id:
        loc = h.location_id == location_id
        if k.key == "transfer":
            loc = or_(loc, h.to_location_id == location_id)
        stmt = stmt.where(loc)
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


# --- building -----------------------------------------------------------------------------


def _check_header_refs(db: Session, p: Principal, k: Kind, v: dict[str, Any]) -> None:
    if "location_id" in v:
        loc = db.get(Location, v["location_id"])
        if loc is None or loc.company_id != p.company_id or not p.can_see_location(loc.id):
            raise BadRequest("Location not found")
    if v.get("to_location_id"):
        loc = db.get(Location, v["to_location_id"])
        if loc is None or loc.company_id != p.company_id:
            raise BadRequest("Receiving location not found")


def _build_lines(db: Session, p: Principal, k: Kind, lines: list[dict[str, Any]]) -> list:
    items = {
        i.id: i
        for i in db.scalars(
            select(Item)
            .where(Item.company_id == p.company_id, Item.id.in_({ln["item_id"] for ln in lines}))
            .options(selectinload(Item.uoms))
        )
    }
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
        qty = Decimal(ln["qty"])
        if k.key == "adjustment":
            if qty == 0:
                raise BadRequest("Quantity cannot be zero", {"line": n})
        elif k.key == "stocktake":
            if qty < 0:
                raise BadRequest("Counted quantity cannot be negative", {"line": n})
        elif qty <= 0:
            raise BadRequest("Quantity must be more than zero", {"line": n})
        extra: dict[str, Any] = {}
        if k.key == "adjustment":
            cost = ln.get("unit_cost")  # per `uom`, only meaningful when adding
            extra["given_cost"] = (
                q4(Decimal(cost) / uom.factor) if cost is not None and qty > 0 else None
            )
        out.append(
            k.line(
                line_no=n,
                item_id=item.id,
                uom=uom.uom,
                qty=qty,
                factor=uom.factor,
                base_qty=q4(qty * uom.factor),
                unit_cost=ZERO,
                reason=ln.get("reason"),
                remark=ln.get("remark"),
                **extra,
            )
        )
    return out


def _snapshot(doc) -> dict[str, Any]:
    return audit.snapshot(
        doc,
        lines=[
            {"item_id": ln.item_id, "uom": ln.uom, "qty": ln.qty, "reason": ln.reason}
            for ln in doc.lines
        ],
    )


# --- validation ---------------------------------------------------------------------------


def qty_at(db: Session, location_id, at: datetime, item_ids=None) -> dict[uuid.UUID, Decimal]:
    """System quantity per item at `location_id` at moment `at`, from the stock ledger."""
    L = StockLedger
    stmt = (
        select(L.item_id, L.balance_qty)
        .ext(distinct_on(L.item_id))
        .where(L.location_id == location_id, L.posted_at <= at)
        .order_by(L.item_id, L.id.desc())
    )
    if item_ids is not None:
        stmt = stmt.where(L.item_id.in_(list(item_ids)))
    return dict(db.execute(stmt).all())


def _counted(doc) -> dict[uuid.UUID, Decimal]:
    out: dict[uuid.UUID, Decimal] = {}
    for ln in doc.lines:
        out[ln.item_id] = out.get(ln.item_id, ZERO) + ln.base_qty
    return out


def _validate(db: Session, p: Principal, k: Kind, doc) -> list[dict[str, Any]]:
    check_common(db, p, doc)
    if k.reasons is not None:
        bad = [ln.line_no for ln in doc.lines if (ln.reason or doc.reason) not in k.reasons]
        if bad:
            raise BusinessRule(
                "Give a reason on the document or on each line",
                {"lines": bad, "allowed": list(k.reasons)},
            )
    if k.key == "transfer":
        if not doc.to_location_id:
            raise BusinessRule("Choose the receiving location")
        if doc.to_location_id == doc.location_id:
            raise BusinessRule("Sending and receiving locations are the same")

    warnings: list[dict[str, Any]] = []
    if k.key == "stocktake":
        if doc.counted_at is None:
            # database clock, the same one that stamps the stock ledger
            doc.counted_at = db.scalar(select(func.now()))
        counted = _counted(doc)
        system = qty_at(db, doc.location_id, doc.counted_at, counted)
        diffs = [
            {
                "item_id": str(item),
                "system_qty": str(system.get(item, ZERO)),
                "counted_qty": str(qty),
            }
            for item, qty in counted.items()
            if qty != system.get(item, ZERO)
        ]
        if diffs:
            warnings.append(
                {
                    "code": "STOCKTAKE_DIFF",
                    "message": "Counted quantities differ from the system",
                    "items": diffs,
                }
            )
    return warnings


# --- commands -----------------------------------------------------------------------------


def create_doc(db: Session, p: Principal, kind: str, *, lines: list[dict[str, Any]], **v: Any):
    k = _kind(kind)
    p.require("inv.doc.create")
    _check_header_refs(db, p, k, v)
    doc = k.header(
        company_id=p.company_id, status="DRAFT", created_by=p.user_id, updated_by=p.user_id, **v
    )
    doc.lines = _build_lines(db, p, k, lines)
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
    p.require("inv.doc.create")
    doc = get_doc(db, p, kind, doc_id, lock=True)
    if doc.status not in EDITABLE:
        raise DocState(f"A {doc.status} document cannot be edited", {"status": doc.status})
    if doc.version != version:
        raise VersionConflict("This document was changed by someone else; reload it")
    before = _snapshot(doc)
    _check_header_refs(db, p, k, v)
    for key, val in v.items():
        if val is None and key in ("doc_date", "location_id", "to_location_id"):
            raise BadRequest("Field cannot be empty", {"field": key})
        setattr(doc, key, val)
    if lines is not None:
        doc.lines = _build_lines(db, p, k, lines)
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
    p.require("inv.doc.create")
    doc = get_doc(db, p, kind, doc_id, lock=True)
    if doc.status != "DRAFT":
        raise DocState("Only drafts can be deleted; void it instead", {"status": doc.status})
    audit.record(db, p, "delete", k.entity, doc.id, before=_snapshot(doc))
    db.delete(doc)
    db.commit()


def save_doc(db: Session, p: Principal, kind: str, doc_id: uuid.UUID, version: int):
    k = _kind(kind)
    p.require("inv.doc.create")
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


def _apply(db: Session, p: Principal, k: Kind, doc, moves: list[stock.Move]):
    if not moves:
        return []
    return stock.apply_moves(
        db,
        company_id=p.company_id,
        doc_type=k.doc_type,
        doc_id=doc.id,
        doc_no=doc.doc_no,
        moves=moves,
    )


def _post_stocktake(db: Session, p: Principal, k: Kind, doc) -> list:
    """Book counted - system (as at counted_at) as a change to today's balance, so
    movements after the count are kept."""
    loc = doc.location_id
    at = doc.counted_at
    system = qty_at(db, loc, at, None if doc.zero_uncounted else set(_counted(doc)))
    if doc.zero_uncounted:
        missing = sorted(
            (i for i, q in system.items() if q != 0 and i not in _counted(doc)), key=str
        )
        base_uoms = dict(
            db.execute(select(Item.id, Item.base_uom).where(Item.id.in_(missing))).all()
        )
        n = len(doc.lines)
        for item_id in missing:
            n += 1
            doc.lines.append(
                k.line(
                    line_no=n,
                    item_id=item_id,
                    uom=base_uoms[item_id],
                    qty=ZERO,
                    factor=Decimal(1),
                    base_qty=ZERO,
                    unit_cost=ZERO,
                    remark="Not counted: taken as zero",
                )
            )
        db.flush()
    counted = _counted(doc)
    moves = []
    first: set[uuid.UUID] = set()
    for ln in doc.lines:
        if ln.item_id in first:
            continue  # an item's difference sits on its first line
        first.add(ln.item_id)
        ln.system_qty = system.get(ln.item_id, ZERO)
        ln.diff_qty = counted[ln.item_id] - ln.system_qty
        if ln.diff_qty:
            moves.append(
                stock.Move(
                    item_id=ln.item_id,
                    location_id=loc,
                    qty=ln.diff_qty,
                    unit_cost=None,
                    line_id=ln.id,
                )
            )
    return _apply(db, p, k, doc, moves)


def post_doc(db: Session, p: Principal, kind: str, doc_id: uuid.UUID, version: int):
    k = _kind(kind)
    p.require("inv.doc.post")
    doc = get_doc(db, p, kind, doc_id, lock=True)  # row lock: no double posting
    if doc.status != "SAVED":
        raise DocState("Only saved documents can be posted", {"status": doc.status})
    if doc.version != version:
        raise VersionConflict("This document was changed by someone else; reload it")

    loc = doc.location_id
    if k.key == "adjustment":
        rows = _apply(
            db,
            p,
            k,
            doc,
            [
                stock.Move(
                    item_id=ln.item_id,
                    location_id=loc,
                    qty=ln.base_qty,
                    unit_cost=ln.given_cost if ln.base_qty > 0 else None,
                    line_id=ln.id,
                )
                for ln in doc.lines
            ],
        )
    elif k.key == "write-off":
        rows = _apply(
            db,
            p,
            k,
            doc,
            [
                stock.Move(
                    item_id=ln.item_id,
                    location_id=loc,
                    qty=-ln.base_qty,
                    unit_cost=None,
                    line_id=ln.id,
                )
                for ln in doc.lines
            ],
        )
    elif k.key == "transfer":
        to = doc.to_location_id
        keys = {(ln.item_id, x) for ln in doc.lines for x in (loc, to)}
        stock.lock_balances(db, p.company_id, sorted(keys, key=lambda x: (str(x[0]), str(x[1]))))
        rows = _apply(
            db,
            p,
            k,
            doc,
            [
                stock.Move(
                    item_id=ln.item_id,
                    location_id=loc,
                    qty=-ln.base_qty,
                    unit_cost=None,
                    line_id=ln.id,
                )
                for ln in doc.lines
            ],
        )
        cost = {r.line_id: r.unit_cost for r in rows}
        _apply(
            db,
            p,
            k,
            doc,
            [
                stock.Move(
                    item_id=ln.item_id,
                    location_id=to,
                    qty=ln.base_qty,
                    unit_cost=cost[ln.id],
                    line_id=ln.id,
                )
                for ln in doc.lines
            ],
        )
    else:
        rows = _post_stocktake(db, p, k, doc)

    cost = {r.line_id: r.unit_cost for r in rows}
    total = ZERO
    for ln in doc.lines:
        if ln.id in cost:
            ln.unit_cost = cost[ln.id]
        qty = ln.diff_qty if k.key == "stocktake" else ln.base_qty
        total += (qty or ZERO) * ln.unit_cost
    # Value moved: + added / - removed for adjustment and stocktake; amount for the rest.
    doc.cost_total = q2(total)
    if rows:
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
    audit.record(
        db, p, "post", k.entity, doc.id, after={"doc_no": doc.doc_no, "value": doc.cost_total}
    )
    db.commit()
    return get_doc(db, p, kind, doc.id)


def void_doc(db: Session, p: Principal, kind: str, doc_id: uuid.UUID, version: int, reason: str):
    k = _kind(kind)
    p.require("inv.doc.void")
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
