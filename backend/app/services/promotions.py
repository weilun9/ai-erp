"""Promotions the till applies by itself.

Kinds (see models.pos.Promotion): PERCENT, AMOUNT (RM off per base unit), PRICE (special
price per base unit), BUY_GET (buy N, get M of them free - the cheapest units), BUNDLE
(N units for RM P - the dearest units, so the customer saves most).

apply() runs while the till prices a sale, before tax: each line can take one promotion,
the one that saves most, and only lines at the list price with no manual discount qualify
(a supervisor's price beats a promotion). The saving is stored on the line (promo_amount)
and taken off its subtotal, so tax, receipts, returns and the books all see the net price.
"""

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import BadRequest, Conflict, NotFound, VersionConflict
from app.models import Item, PosLine, Promotion
from app.services import audit
from app.services.auth import Principal
from app.services.doclib import q2

ZERO = Decimal(0)
HUNDRED = Decimal(100)
PERM = "pos.promo.manage"


# --- set-up ----------------------------------------------------------------------------------


def _check(v: dict[str, Any]) -> None:
    kind = v.get("kind")
    if v.get("date_to") and v.get("date_from") and v["date_to"] < v["date_from"]:
        raise BadRequest("The end date is before the start date")
    value = Decimal(str(v.get("value") or 0))
    if kind == "PERCENT" and not (ZERO < value <= HUNDRED):
        raise BadRequest("A percentage between 0 and 100")
    if kind in ("AMOUNT", "PRICE", "BUNDLE") and value <= 0 and kind != "PRICE":
        raise BadRequest("An amount above 0")
    if kind == "BUY_GET" and (int(v.get("buy_qty") or 0) < 1 or int(v.get("get_qty") or 0) < 1):
        raise BadRequest("Buy at least 1 and get at least 1")
    if kind == "BUNDLE" and int(v.get("buy_qty") or 0) < 2:
        raise BadRequest("A bundle is 2 or more units")


def list_promotions(db: Session, p: Principal, *, active_on: date | None = None) -> list[Promotion]:
    p.require("pos.sale.create")
    stmt = select(Promotion).where(Promotion.company_id == p.company_id)
    if active_on:
        stmt = stmt.where(
            Promotion.is_active.is_(True),
            Promotion.date_from <= active_on,
            Promotion.date_to >= active_on,
        )
    return list(db.scalars(stmt.order_by(Promotion.date_from.desc(), Promotion.code)))


def get_promotion(db: Session, p: Principal, promo_id: uuid.UUID) -> Promotion:
    promo = db.get(Promotion, promo_id)
    if promo is None or promo.company_id != p.company_id:
        raise NotFound("Promotion not found")
    return promo


def create_promotion(db: Session, p: Principal, **v: Any) -> Promotion:
    p.require(PERM)
    _check(v)
    v["code"] = v["code"].strip().upper()
    if db.scalar(
        select(Promotion.id).where(
            Promotion.company_id == p.company_id, Promotion.code == v["code"]
        )
    ):
        raise Conflict("That promotion code is taken", {"code": v["code"]})
    promo = Promotion(company_id=p.company_id, created_by=p.user_id, **v)
    db.add(promo)
    db.flush()
    audit.record(db, p, "create", "sal.promotion", promo.id, after=audit.snapshot(promo))
    db.commit()
    return promo


def update_promotion(
    db: Session, p: Principal, promo_id: uuid.UUID, version: int, **v: Any
) -> Promotion:
    p.require(PERM)
    promo = get_promotion(db, p, promo_id)
    if promo.version != version:
        raise VersionConflict("This promotion was changed by someone else; reload it")
    before = audit.snapshot(promo)
    merged = {**audit.snapshot(promo), **v}
    for k in ("date_from", "date_to"):
        if isinstance(merged[k], str):
            merged[k] = date.fromisoformat(merged[k])
    _check(merged)
    for k, x in v.items():
        setattr(promo, k, x)
    promo.version += 1
    db.flush()
    audit.record(
        db, p, "update", "sal.promotion", promo.id, before=before, after=audit.snapshot(promo)
    )
    db.commit()
    return promo


# --- at the till -------------------------------------------------------------------------------


@dataclass
class _Unit:
    line: int  # index into the lines
    price: Decimal  # this unit's price after any line discount


def _targets(promo: Promotion, item: Item) -> bool:
    if not (promo.item_ids or promo.category_ids or promo.brand_ids):
        return True
    return (
        item.id in promo.item_ids
        or (item.category_id is not None and item.category_id in promo.category_ids)
        or (item.brand_id is not None and item.brand_id in promo.brand_ids)
    )


def _saving(promo: Promotion, lines: list[PosLine], idx: list[int]) -> dict[int, Decimal]:
    """line index -> RM this promotion would take off it."""
    out: dict[int, Decimal] = {}
    v = promo.value
    if promo.kind in ("PERCENT", "AMOUNT", "PRICE"):
        for i in idx:
            ln = lines[i]
            gross = ln.line_subtotal
            if promo.kind == "PERCENT":
                off = gross * v / HUNDRED
            elif promo.kind == "AMOUNT":
                off = v * ln.base_qty
            else:
                off = gross - v * ln.base_qty
            off = q2(max(ZERO, min(off, gross)))
            if off:
                out[i] = off
        return out

    # unit-based kinds: whole base units only
    units: list[_Unit] = []
    for i in idx:
        ln = lines[i]
        n = int(ln.base_qty.to_integral_value(rounding=ROUND_FLOOR))
        if n <= 0:
            continue
        each = ln.line_subtotal / ln.base_qty
        units += [_Unit(i, each) for _ in range(n)]
    if promo.kind == "BUY_GET":
        group = promo.buy_qty + promo.get_qty
        free = (len(units) // group) * promo.get_qty
        units.sort(key=lambda u: u.price)  # the cheapest go free
        for u in units[:free]:
            out[u.line] = out.get(u.line, ZERO) + u.price
    else:  # BUNDLE
        sets = len(units) // promo.buy_qty
        units.sort(key=lambda u: u.price, reverse=True)  # the dearest go in the bundles
        taken = units[: sets * promo.buy_qty]
        value = sum((u.price for u in taken), ZERO)
        saving = value - promo.value * sets
        if saving > 0 and value > 0:
            for u in taken:  # spread over the units in proportion to their price
                out[u.line] = out.get(u.line, ZERO) + saving * u.price / value
    return {i: q2(x) for i, x in out.items() if q2(x) > 0}


def apply(
    db: Session,
    company_id: uuid.UUID,
    *,
    location_id: uuid.UUID,
    on: date,
    lines: list[PosLine],
    items: list[Item],
    member: bool,
) -> list[dict[str, Any]]:
    """Take promotions off the lines (in place). -> what was applied, for the receipt."""
    promos = db.scalars(
        select(Promotion).where(
            Promotion.company_id == company_id,
            Promotion.is_active.is_(True),
            Promotion.date_from <= on,
            Promotion.date_to >= on,
        )
    ).all()
    promos = [
        x
        for x in promos
        if (not x.location_ids or location_id in x.location_ids) and (member or not x.members_only)
    ]
    open_ = [i for i, ln in enumerate(lines) if ln.price_source == "LIST" and not ln.discount_pct]
    taken: set[int] = set()
    used: list[dict[str, Any]] = []
    while promos:
        best = None
        for promo in promos:
            idx = [i for i in open_ if i not in taken and _targets(promo, items[i])]
            saving = _saving(promo, lines, idx) if idx else {}
            total = sum(saving.values(), ZERO)
            if total and (best is None or total > best[2]):
                best = (promo, saving, total)
        if best is None:
            break
        promo, saving, total = best
        for i, off in saving.items():
            ln = lines[i]
            ln.promo_id, ln.promo_amount = promo.id, off
            ln.line_subtotal = ln.line_subtotal - off
            ln.line_tax = q2(ln.line_subtotal * ln.tax_rate / HUNDRED)
            ln.line_total = ln.line_subtotal + ln.line_tax
            taken.add(i)
        used.append({"id": promo.id, "code": promo.code, "name": promo.name, "saving": total})
        promos = [x for x in promos if x.id != promo.id]
    return used
