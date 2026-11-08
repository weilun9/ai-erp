"""Helpers shared by every document module (purchasing, sales, later stock documents)."""

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import BusinessRule
from app.localization import pack
from app.models import Company, Item
from app.services.auth import Principal

Q2 = Decimal("0.01")
Q4 = Decimal("0.0001")
HUNDRED = Decimal(100)
EDITABLE = ("DRAFT", "SAVED")


def q2(v: Decimal) -> Decimal:
    return v.quantize(Q2, rounding=ROUND_HALF_UP)


def q4(v: Decimal) -> Decimal:
    return v.quantize(Q4, rounding=ROUND_HALF_UP)


def today(db: Session, p: Principal) -> date:
    """Today in the company's own time zone (UTC+8 for Malaysia)."""
    offset = pack(db.get(Company, p.company_id).country).UTC_OFFSET_HOURS
    return datetime.now(timezone(timedelta(hours=offset))).date()


def set_totals(doc) -> None:
    doc.subtotal = sum((ln.line_subtotal for ln in doc.lines), Decimal(0))
    doc.tax_total = sum((ln.line_tax for ln in doc.lines), Decimal(0))
    doc.total = doc.subtotal + doc.tax_total


def check_common(db: Session, p: Principal, doc) -> None:
    """Rules every document must pass before it is saved."""
    if not doc.lines:
        raise BusinessRule("A document needs at least one line")
    now = today(db, p)
    if doc.doc_date > now:
        raise BusinessRule(
            "Document date is in the future — check the year on the original",
            {"doc_date": doc.doc_date.isoformat(), "today": now.isoformat()},
        )
    inactive = db.scalars(
        select(Item.code).where(
            Item.id.in_({ln.item_id for ln in doc.lines}), Item.is_active.is_(False)
        )
    ).all()
    if inactive:
        raise BusinessRule("Inactive items on the document", {"items": sorted(inactive)})


def line_amounts(
    qty: Decimal, price: Decimal, disc: Decimal, rate: Decimal
) -> tuple[Decimal, Decimal]:
    """(subtotal, tax) for one line, both rounded to cents."""
    subtotal = q2(qty * price * (HUNDRED - disc) / HUNDRED)
    return subtotal, q2(subtotal * rate / HUNDRED)


def ids(values) -> set[uuid.UUID]:
    return {v for v in values if v is not None}
