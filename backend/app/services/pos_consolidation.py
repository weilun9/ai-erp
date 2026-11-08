"""Monthly consolidated e-invoice for walk-in POS sales (LHDN MyInvois).

Walk-in customers who do not ask for their own e-invoice are reported once a month in one
consolidated e-invoice. What the SDK confirms (sdk.myinvois.hasil.gov.my, checked
2026-11-09): classification code 004 "Consolidated e-Invoice", buyer TIN EI00000000010
with BRN/SST/TTX/phone/address "NA" and state code 17, one line per range of receipts.

Here: one line per store x tax rate, e.g. "HQ receipts POS000001-POS000150 (148 sales,
2 refunds)", quantity 1, net of refunds made in the same month.

Not automated (not confirmed in the SDK pages): a refund of a receipt that was already
reported in an earlier month. It is left out and listed as needing a credit note.
The guideline's deadline (7 days after the month, per LHDN's e-invoice guideline) is
shown as a reminder, not enforced.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.orm import Session, aliased

from app.core.errors import BusinessRule, DocState, NotFound
from app.localization.my import TIMEZONE
from app.models import Location, PosConsolidation, PosLine, PosSale, TaxCode
from app.services import audit
from app.services.auth import Principal
from app.services.doclib import q2
from app.services.numbering import next_doc_no

ZERO = Decimal(0)
REMIND_AFTER_DAYS = 7


def month(period: date) -> tuple[date, date]:
    start = period.replace(day=1)
    end = (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    return start, end


def local_today() -> date:
    return datetime.now(ZoneInfo(TIMEZONE)).date()


def _bounds(start: date, end: date) -> tuple[datetime, datetime]:
    tz = ZoneInfo(TIMEZONE)  # the shop's business days
    return (
        datetime.combine(start, datetime.min.time(), tz),
        datetime.combine(end + timedelta(days=1), datetime.min.time(), tz),
    )


@dataclass
class Group:
    location: str
    tax_type: str
    tax_name: str
    rate: Decimal
    subtotal: Decimal = ZERO
    tax: Decimal = ZERO
    sales: set[str] = field(default_factory=set)
    refunds: set[str] = field(default_factory=set)

    @property
    def description(self) -> str:
        nos = sorted(self.sales) or sorted(self.refunds)
        span = f"{nos[0]}-{nos[-1]}" if len(nos) > 1 else nos[0]
        what = f"{len(self.sales)} sales" + (
            f", {len(self.refunds)} refunds" if self.refunds else ""
        )
        return f"{self.location} receipts {span} ({what})"


@dataclass
class Summary:
    groups: list[Group]
    sale_ids: list[uuid.UUID]
    receipts: int
    subtotal: Decimal
    tax_total: Decimal
    rounding: Decimal
    left_out: list[dict[str, str]]

    @property
    def total(self) -> Decimal:
        return self.subtotal + self.tax_total + self.rounding


def _summarize(
    db: Session, where: list, *, lock: bool = False, own: uuid.UUID | None = None
) -> Summary:
    """Group the receipts matched by `where`. A refund whose original receipt is in another
    consolidated document (`own` = this one) is left out: it needs a credit note."""
    original = aliased(PosSale)
    stmt = (
        select(
            PosSale.id,
            PosSale.kind,
            PosSale.receipt_no,
            PosSale.rounding,
            original.consolidation_id,
            original.receipt_no,
        )
        .outerjoin(original, original.id == PosSale.ref_sale_id)
        .where(*where)
    )
    if lock:
        stmt = stmt.with_for_update(of=PosSale)
    rows = db.execute(stmt).all()
    left_out = [
        {
            "receipt_no": no,
            "refunds": orig_no,
            "why": "the original receipt was already reported in an earlier consolidated "
            "e-invoice: a credit note is needed (not automated yet)",
        }
        for _, kind, no, _, orig_con, orig_no in rows
        if kind == "RETURN" and orig_con is not None and orig_con != own
    ]
    skip = {x["receipt_no"] for x in left_out}
    kept = [r for r in rows if r[2] not in skip]
    ids = [r[0] for r in kept]
    groups: dict[tuple, Group] = {}
    if ids:
        lines = db.execute(
            select(
                Location.code,
                TaxCode.einvoice_tax_type,
                TaxCode.name,
                PosLine.tax_rate,
                PosSale.kind,
                PosSale.receipt_no,
                PosLine.line_subtotal,
                PosLine.line_tax,
            )
            .join(PosSale, PosSale.id == PosLine.sale_id)
            .join(Location, Location.id == PosSale.location_id)
            .outerjoin(TaxCode, TaxCode.id == PosLine.tax_code_id)
            .where(PosSale.id.in_(ids))
        ).all()
        for loc, ttype, tname, rate, kind, no, sub, tax in lines:
            key = (loc, ttype or "06", rate)
            g = groups.setdefault(key, Group(loc, ttype or "06", tname or "Not applicable", rate))
            g.subtotal += sub
            g.tax += tax
            (g.sales if kind == "SALE" else g.refunds).add(no)
    ordered = [groups[k] for k in sorted(groups, key=lambda k: (k[0], k[1], k[2]))]
    return Summary(
        groups=ordered,
        sale_ids=ids,
        receipts=sum(1 for r in kept if r[1] == "SALE"),
        subtotal=q2(sum((g.subtotal for g in ordered), ZERO)),
        tax_total=q2(sum((g.tax for g in ordered), ZERO)),
        rounding=q2(sum((r[3] for r in kept), ZERO)),
        left_out=left_out,
    )


def pending(db: Session, p: Principal, period: date, *, lock: bool = False) -> Summary:
    """Receipts of the month not yet in a consolidated e-invoice."""
    lo, hi = _bounds(*month(period))
    where = [
        PosSale.company_id == p.company_id,
        PosSale.sold_at >= lo,
        PosSale.sold_at < hi,
        PosSale.consolidation_id.is_(None),
    ]
    return _summarize(db, where, lock=lock)


def of(db: Session, con: PosConsolidation) -> Summary:
    """The receipts a consolidated document holds."""
    return _summarize(db, [PosSale.consolidation_id == con.id], own=con.id)


def reminders(period: date) -> list[dict[str, str]]:
    start, end = month(period)
    out = []
    late = local_today() - end
    if late > timedelta(days=REMIND_AFTER_DAYS):
        out.append(
            {
                "field": "period",
                "message": f"{late.days} days after the month ended: "
                f"LHDN's guideline asks for consolidated e-invoices within "
                f"{REMIND_AFTER_DAYS} days",
            }
        )
    return out


def create(db: Session, p: Principal, period: date) -> PosConsolidation:
    """Take the month's unreported receipts into a new consolidated document."""
    p.require("sal.einvoice.submit")
    start, end = month(period)
    if end >= local_today():
        raise BusinessRule("The month is not over yet", {"period_end": end.isoformat()})
    s = pending(db, p, period, lock=True)  # a second click waits here, then finds nothing
    if not s.sale_ids:
        raise BusinessRule("No unreported POS receipts in this month", {"left_out": s.left_out})
    if s.subtotal + s.tax_total <= 0:
        raise BusinessRule("Refunds are more than sales this month; nothing to report")
    con = PosConsolidation(
        company_id=p.company_id,
        doc_no=next_doc_no(db, p.company_id, "CON"),
        period_start=start,
        period_end=end,
        receipts=s.receipts,
        subtotal=s.subtotal,
        tax_total=s.tax_total,
        rounding=s.rounding,
        total=s.total,
        created_by=p.user_id,
    )
    db.add(con)
    db.flush()
    db.execute(update(PosSale).where(PosSale.id.in_(s.sale_ids)).values(consolidation_id=con.id))
    audit.record(
        db,
        p,
        "pos_consolidate",
        "sal.pos_consolidation",
        con.id,
        after={"period": start.isoformat(), "receipts": s.receipts, "total": s.total},
    )
    db.commit()
    db.refresh(con)
    return con


def get(db: Session, p: Principal, con_id: uuid.UUID, *, lock: bool = False) -> PosConsolidation:
    p.require("sal.doc.read")
    stmt = select(PosConsolidation).where(
        PosConsolidation.id == con_id, PosConsolidation.company_id == p.company_id
    )
    if lock:
        stmt = stmt.with_for_update()
    con = db.scalar(stmt)
    if con is None:
        raise NotFound("Consolidated document not found")
    return con


def release(db: Session, con: PosConsolidation, p: Principal | None, why: str) -> None:
    """Its e-invoice was cancelled (or it was never sent): free the receipts again."""
    if con.status != "OPEN":
        return
    con.status, con.released_at = "RELEASED", datetime.now(UTC)
    db.execute(
        update(PosSale).where(PosSale.consolidation_id == con.id).values(consolidation_id=None)
    )
    if p is not None:
        audit.record(
            db, p, "pos_consolidation_release", "sal.pos_consolidation", con.id, after={"why": why}
        )


def drop(db: Session, p: Principal, con_id: uuid.UUID) -> PosConsolidation:
    """Undo a consolidated document that has no live e-invoice (e.g. made too early)."""
    from app.services.einvoice import active_for

    p.require("sal.einvoice.submit")
    con = get(db, p, con_id, lock=True)
    if con.status != "OPEN":
        raise DocState("Already released")
    if active_for(db, con.id) is not None:
        raise DocState("It has a live e-invoice: cancel the e-invoice instead")
    release(db, con, p, "dropped before sending")
    db.commit()
    db.refresh(con)
    return con


def listing(db: Session, p: Principal) -> list[PosConsolidation]:
    p.require("sal.doc.read")
    return list(
        db.scalars(
            select(PosConsolidation)
            .where(PosConsolidation.company_id == p.company_id)
            .order_by(PosConsolidation.period_start.desc(), PosConsolidation.doc_no.desc())
        )
    )


def as_dict(s: Summary) -> dict[str, Any]:
    return {
        "receipts": s.receipts,
        "subtotal": s.subtotal,
        "tax_total": s.tax_total,
        "rounding": s.rounding,
        "total": q2(s.total),
        "lines": [
            {
                "description": g.description,
                "tax_type": g.tax_type,
                "rate": g.rate,
                "subtotal": q2(g.subtotal),
                "tax": q2(g.tax),
            }
            for g in s.groups
        ],
        "left_out": s.left_out,
    }
