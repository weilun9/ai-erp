"""Batch / expiry and serial numbers: a sub-ledger under the stock ledger.

Items with track_batch or track_serial carry their stock in lots (inv.lot): a batch number
with an optional expiry date, or one serial number per piece. stock.apply_moves calls
record() for every movement of such an item, in the same transaction:

- In (receipt, return, adjustment up): the line's lots, or - for the incoming half of a
  transfer - the same lots its outgoing half took; with none given the quantity goes to
  the lot "-" (stock with no number recorded), so no existing flow breaks.
- Out (sale, delivery, write-off, transfer out): the line's lots when given (each must
  have enough at that store), otherwise earliest expiry first (FEFO), oldest first for
  lots without a date; any shortfall comes from "-" (negative, like the stock itself).
- Void: reverse() undoes exactly what the document did.

Quantities are base units. A serial lot holds at most 1 piece.
"""

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.errors import BusinessRule
from app.models import Item, Lot, LotBalance, LotMove, StockBalance, StockLedger

NO_LOT = "-"
ZERO = Decimal(0)


class LotInvalid(BusinessRule):
    code = "LOT_INVALID"


@dataclass(frozen=True)
class LotQty:
    lot_no: str
    qty: Decimal  # base units, positive
    expiry_date: date | None = None


def clean(raw: list[Any] | None) -> list[dict[str, Any]] | None:
    """A line's lots from the API (dicts or models) as JSON for the line's `lots` column."""
    if not raw:
        return None
    out = []
    for x in raw:
        d = x if isinstance(x, dict) else x.model_dump()
        no = str(d.get("lot_no") or "").strip()
        qty = Decimal(str(d.get("qty") or 0))
        if not no or qty <= 0:
            raise LotInvalid("Each batch / serial needs a number and a quantity above 0")
        exp = d.get("expiry_date")
        out.append(
            {
                "lot_no": no,
                "expiry_date": exp.isoformat() if isinstance(exp, date) else (exp or None),
                "qty": str(qty),
            }
        )
    return out


def from_line(lots: list[dict[str, Any]] | None) -> tuple[LotQty, ...] | None:
    if not lots:
        return None
    return tuple(
        LotQty(
            lot_no=x["lot_no"],
            qty=Decimal(str(x["qty"])),
            expiry_date=date.fromisoformat(x["expiry_date"]) if x.get("expiry_date") else None,
        )
        for x in lots
    )


def tracked(db: Session, item_ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    """item_id -> "SERIAL" / "BATCH" for the items that track lots."""
    if not item_ids:
        return {}
    rows = db.execute(
        select(Item.id, Item.track_batch, Item.track_serial).where(
            Item.id.in_(item_ids), Item.track_batch.is_(True) | Item.track_serial.is_(True)
        )
    ).all()
    return {i: ("SERIAL" if serial else "BATCH") for i, _, serial in rows}


def _lot(
    db: Session, company_id: uuid.UUID, item_id: uuid.UUID, kind: str, no: str, exp: date | None
) -> Lot:
    db.execute(
        insert(Lot)
        .values(company_id=company_id, item_id=item_id, kind=kind, lot_no=no, expiry_date=exp)
        .on_conflict_do_nothing(constraint="uq_inv_lot")
    )
    lot = db.scalar(
        select(Lot).where(
            Lot.company_id == company_id, Lot.item_id == item_id, Lot.kind == kind, Lot.lot_no == no
        )
    )
    if exp is not None and lot.expiry_date is None:
        lot.expiry_date = exp
    elif exp is not None and lot.expiry_date != exp:
        raise LotInvalid(
            f"Batch {no} already has expiry date {lot.expiry_date.isoformat()}",
            {"lot_no": no, "expiry_date": lot.expiry_date.isoformat()},
        )
    return lot


def _balance(db: Session, company_id: uuid.UUID, lot_id: uuid.UUID, loc: uuid.UUID) -> LotBalance:
    db.execute(
        insert(LotBalance)
        .values(lot_id=lot_id, location_id=loc, company_id=company_id)
        .on_conflict_do_nothing()
    )
    return db.scalar(
        select(LotBalance)
        .where(LotBalance.lot_id == lot_id, LotBalance.location_id == loc)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _write(db: Session, company_id: uuid.UUID, lot: Lot, row: StockLedger, qty: Decimal) -> None:
    bal = _balance(db, company_id, lot.id, row.location_id)
    bal.qty = bal.qty + qty
    db.add(
        LotMove(
            company_id=company_id,
            lot_id=lot.id,
            location_id=row.location_id,
            ledger_id=row.id,
            doc_type=row.doc_type,
            doc_id=row.doc_id,
            doc_no=row.doc_no,
            line_id=row.line_id,
            qty=qty,
            is_reversal=row.is_reversal,
        )
    )


def _mirror(db: Session, row: StockLedger) -> tuple[LotQty, ...] | None:
    """The lots this document line took out earlier (the incoming half of a transfer)."""
    if row.line_id is None:
        return None
    taken = db.execute(
        select(Lot.lot_no, Lot.expiry_date, func.sum(LotMove.qty))
        .join(Lot, Lot.id == LotMove.lot_id)
        .where(
            LotMove.doc_type == row.doc_type,
            LotMove.doc_id == row.doc_id,
            LotMove.line_id == row.line_id,
            LotMove.is_reversal.is_(False),
            LotMove.qty < 0,
        )
        .group_by(Lot.lot_no, Lot.expiry_date)
    ).all()
    return tuple(LotQty(no, -q, exp) for no, exp, q in taken) or None


def record(db: Session, company_id: uuid.UUID, kind: str, move, row: StockLedger) -> None:
    """Split one stock movement of a tracked item into lots (see the module docstring)."""
    qty = move.qty
    given: tuple[LotQty, ...] | None = getattr(move, "lots", None)
    if given and sum(x.qty for x in given) != abs(qty):
        raise LotInvalid(
            "Batch / serial quantities must add up to the line quantity",
            {"line_id": str(row.line_id), "line_qty": str(abs(qty))},
        )
    if kind == "SERIAL" and given:
        bad = [x.lot_no for x in given if x.qty != 1]
        if bad:
            raise LotInvalid("Each serial number is one piece", {"serials": bad})
        dup = {x.lot_no for x in given if [y.lot_no for y in given].count(x.lot_no) > 1}
        if dup:
            raise LotInvalid("A serial number is listed twice", {"serials": sorted(dup)})

    if qty > 0:
        lots = given or _mirror(db, row) or (LotQty(NO_LOT, qty),)
        for x in lots:
            lot = _lot(db, company_id, move.item_id, kind, x.lot_no, x.expiry_date)
            if kind == "SERIAL" and x.lot_no != NO_LOT and given:
                held = db.scalar(
                    select(func.coalesce(func.sum(LotBalance.qty), 0)).where(
                        LotBalance.lot_id == lot.id
                    )
                )
                if held > 0:
                    raise LotInvalid(
                        f"Serial number {x.lot_no} is already in stock", {"serial": x.lot_no}
                    )
            _write(db, company_id, lot, row, x.qty)
        return

    need = -qty
    if given:
        for x in given:
            lot = db.scalar(
                select(Lot).where(
                    Lot.company_id == company_id,
                    Lot.item_id == move.item_id,
                    Lot.kind == kind,
                    Lot.lot_no == x.lot_no,
                )
            )
            have = _balance(db, company_id, lot.id, row.location_id).qty if lot else ZERO
            if lot is None or have < x.qty:
                raise LotInvalid(
                    f"{'Serial' if kind == 'SERIAL' else 'Batch'} {x.lot_no} has only "
                    f"{have.normalize():f} here",
                    {"lot_no": x.lot_no, "available": str(have)},
                )
            _write(db, company_id, lot, row, -x.qty)
        return

    # earliest expiry first; lots without a date oldest first; "-" last
    candidates = db.execute(
        select(Lot, LotBalance.qty)
        .join(LotBalance, LotBalance.lot_id == Lot.id)
        .where(
            Lot.company_id == company_id,
            Lot.item_id == move.item_id,
            Lot.kind == kind,
            LotBalance.location_id == row.location_id,
            LotBalance.qty > 0,
        )
        .order_by(
            (Lot.lot_no == NO_LOT).asc(),
            Lot.expiry_date.asc().nulls_last(),
            Lot.created_at,
            Lot.lot_no,
        )
    ).all()
    for lot, have in candidates:
        if need <= 0:
            break
        take = min(have, need)
        _write(db, company_id, lot, row, -take)
        need -= take
    if need > 0:
        _write(db, company_id, _lot(db, company_id, move.item_id, kind, NO_LOT, None), row, -need)


def open_unnumbered(db: Session, company_id: uuid.UUID, item_id: uuid.UUID, kind: str) -> None:
    """Tracking was just switched on: the stock already on hand has no numbers, so it starts
    in the lot "-" at each store (it goes out after the numbered lots, see record)."""
    for bal in db.scalars(
        select(StockBalance).where(StockBalance.item_id == item_id, StockBalance.qty != 0)
    ):
        lot = _lot(db, company_id, item_id, kind, NO_LOT, None)
        lb = _balance(db, company_id, lot.id, bal.location_id)
        held = db.scalar(
            select(func.coalesce(func.sum(LotBalance.qty), 0))
            .join(Lot, Lot.id == LotBalance.lot_id)
            .where(
                Lot.item_id == item_id, Lot.kind == kind, LotBalance.location_id == bal.location_id
            )
        )
        lb.qty = lb.qty + (bal.qty - held)  # whatever the lots do not already account for


def reverse(db: Session, company_id: uuid.UUID, pairs: list[tuple[int, StockLedger]]) -> None:
    """Undo a document's lot movements: pairs = (original ledger id, its reversing row)."""
    by_ledger = dict(pairs)
    if not by_ledger:
        return
    originals = db.scalars(
        select(LotMove)
        .where(LotMove.ledger_id.in_(by_ledger), LotMove.is_reversal.is_(False))
        .order_by(LotMove.id)
    ).all()
    for m in originals:
        lot = db.get(Lot, m.lot_id)
        _write(db, company_id, lot, by_ledger[m.ledger_id], -m.qty)


def lot_summary(db: Session, doc_type: str, doc_id: uuid.UUID) -> dict[uuid.UUID, list[dict]]:
    """line_id -> the lots a posted document actually moved (for showing on the document)."""
    rows = db.execute(
        select(LotMove.line_id, Lot.lot_no, Lot.expiry_date, func.sum(LotMove.qty))
        .join(Lot, Lot.id == LotMove.lot_id)
        .where(LotMove.doc_type == doc_type, LotMove.doc_id == doc_id)
        .group_by(LotMove.line_id, Lot.lot_no, Lot.expiry_date)
        .having(func.sum(LotMove.qty) != 0)
    ).all()
    out: dict[uuid.UUID, list[dict]] = {}
    for line_id, no, exp, q in rows:
        out.setdefault(line_id, []).append(
            {"lot_no": no, "expiry_date": exp.isoformat() if exp else None, "qty": str(abs(q))}
        )
    return out
