"""Stock posting: the only code that writes inv.stock_ledger / inv.stock_balance.

- Balances are locked with SELECT ... FOR UPDATE in a fixed order (item, location), so two
  documents posting the same item at the same time serialize instead of losing updates.
- Cost is moving average per item and location, in base units.
- Runs inside the caller's transaction: the document's status change and its stock
  movements commit together or not at all.
"""

import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.errors import StockInsufficient
from app.models import StockBalance, StockLedger

Q4 = Decimal("0.0001")
ZERO = Decimal(0)


def q4(v: Decimal) -> Decimal:
    return v.quantize(Q4, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Move:
    item_id: uuid.UUID
    location_id: uuid.UUID
    qty: Decimal  # base units; > 0 in, < 0 out
    # In: cost per base unit. Out: None = take at current average (a sale);
    # a value = remove exactly that value (a supplier return at its price, or a void).
    unit_cost: Decimal | None
    line_id: uuid.UUID | None = None


def _lock_balances(
    db: Session, company_id: uuid.UUID, keys: list[tuple[uuid.UUID, uuid.UUID]]
) -> dict[tuple[uuid.UUID, uuid.UUID], StockBalance]:
    for item_id, loc_id in keys:
        db.execute(
            insert(StockBalance)
            .values(item_id=item_id, location_id=loc_id, company_id=company_id)
            .on_conflict_do_nothing()
        )
    out = {}
    for item_id, loc_id in keys:  # sorted order -> no deadlocks between documents
        out[(item_id, loc_id)] = db.scalar(
            select(StockBalance)
            .where(StockBalance.item_id == item_id, StockBalance.location_id == loc_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    return out


def apply_moves(
    db: Session,
    *,
    company_id: uuid.UUID,
    doc_type: str,
    doc_id: uuid.UUID,
    doc_no: str,
    moves: list[Move],
    is_reversal: bool = False,
    allow_negative: bool = True,
) -> list[StockLedger]:
    """Negative stock is allowed (company decision 2026-11-08); pass allow_negative=False
    for flows that must not oversell."""
    keys = sorted({(m.item_id, m.location_id) for m in moves}, key=lambda k: (str(k[0]), str(k[1])))
    balances = _lock_balances(db, company_id, keys)

    # Check every outgoing total first, so a failing document writes nothing.
    if not allow_negative:
        need: dict[tuple, Decimal] = {}
        for m in moves:
            need[(m.item_id, m.location_id)] = need.get((m.item_id, m.location_id), ZERO) + m.qty
        short = [
            {
                "item_id": str(k[0]),
                "location_id": str(k[1]),
                "on_hand": str(balances[k].qty),
                "change": str(v),
            }
            for k, v in need.items()
            if v < 0 and balances[k].qty + v < 0
        ]
        if short:
            raise StockInsufficient("Not enough stock", {"lines": short})

    rows = []
    for m in moves:
        bal = balances[(m.item_id, m.location_id)]
        q, a = bal.qty, bal.avg_cost
        if m.qty > 0:
            cost = m.unit_cost if m.unit_cost is not None else a
            new_q = q + m.qty
            if q <= 0 or new_q <= 0:
                new_a = cost
            else:
                new_a = (q * a + m.qty * cost) / new_q
        else:
            out = -m.qty
            new_q = q - out
            if m.unit_cost is None:
                cost, new_a = a, a
            else:
                cost = m.unit_cost
                new_a = (q * a - out * cost) / new_q if new_q > 0 else a
                if new_a < 0:
                    new_a = a
        bal.qty, bal.avg_cost = q4(new_q), q4(new_a)
        row = StockLedger(
            company_id=company_id,
            item_id=m.item_id,
            location_id=m.location_id,
            doc_type=doc_type,
            doc_id=doc_id,
            doc_no=doc_no,
            line_id=m.line_id,
            qty_in=m.qty if m.qty > 0 else ZERO,
            qty_out=-m.qty if m.qty < 0 else ZERO,
            unit_cost=q4(cost),
            balance_qty=bal.qty,
            avg_cost=bal.avg_cost,
            is_reversal=is_reversal,
        )
        db.add(row)
        rows.append(row)
    db.flush()
    return rows


def reverse_doc(
    db: Session, *, company_id: uuid.UUID, doc_type: str, doc_id: uuid.UUID, doc_no: str
) -> list[StockLedger]:
    """Undo a posted document's movements at their original cost (used by void)."""
    original = db.scalars(
        select(StockLedger)
        .where(
            StockLedger.doc_type == doc_type,
            StockLedger.doc_id == doc_id,
            StockLedger.is_reversal.is_(False),
        )
        .order_by(StockLedger.id)
    ).all()
    moves = [
        Move(
            item_id=r.item_id,
            location_id=r.location_id,
            qty=r.qty_out - r.qty_in,
            unit_cost=r.unit_cost,
            line_id=r.line_id,
        )
        for r in original
    ]
    return apply_moves(
        db,
        company_id=company_id,
        doc_type=doc_type,
        doc_id=doc_id,
        doc_no=doc_no,
        moves=moves,
        is_reversal=True,
    )
