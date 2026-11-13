"""Predefined reports. The web screen, the AI butler and MCP all run these; nobody (and
no model) writes free SQL against the books.

Sales = posted sales invoices + POS receipts, minus posted customer credit notes and POS
refunds, before tax. POS sales count on their Malaysian business day.
Cost and gross profit appear only for users who may see cost.
"""

import uuid
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import Date, case, cast, func, literal, select, union_all
from sqlalchemy.orm import Session

from app.core.errors import NotFound
from app.localization.my import TIMEZONE
from app.models import (
    Customer,
    CustomerCn,
    CustomerCnLine,
    Grn,
    GrnLine,
    Item,
    ItemLocation,
    Location,
    PosLine,
    PosSale,
    SalesInvoice,
    SalesInvoiceLine,
    StockBalance,
    Supplier,
)
from app.services.auth import Principal
from app.services.doclib import q4

GroupBy = Literal["day", "item", "customer", "location"]
ZERO = Decimal(0)


def _location_filter(p: Principal, location_id: uuid.UUID | None):
    if location_id is not None and not p.can_see_location(location_id):
        raise NotFound("Location not found")
    if location_id is not None:
        return [location_id]
    return None if p.location_ids is None else list(p.location_ids)


def _sales_lines(p: Principal, date_from: date, date_to: date, locations):
    """One row per posted line: sign +1 for invoices, -1 for credit notes."""
    parts = []
    for header, line, sign in (
        (SalesInvoice, SalesInvoiceLine, 1),
        (CustomerCn, CustomerCnLine, -1),
    ):
        stmt = (
            select(
                header.doc_date.label("doc_date"),
                header.customer_id.label("customer_id"),
                header.location_id.label("location_id"),
                line.item_id.label("item_id"),
                (line.base_qty * sign).label("qty"),
                (line.line_subtotal * sign).label("sales"),
                (line.base_qty * line.unit_cost * sign).label("cost"),
                header.id.label("doc_id"),
                literal(sign).label("sign"),
            )
            .join(line, line.header_id == header.id)
            .where(
                header.company_id == p.company_id,
                header.status == "POSTED",
                header.doc_date.between(date_from, date_to),
            )
        )
        if locations is not None:
            stmt = stmt.where(header.location_id.in_(locations))
        parts.append(stmt)
    day = cast(func.timezone(TIMEZONE, PosSale.sold_at), Date)
    pos = (
        select(
            day.label("doc_date"),
            PosSale.customer_id.label("customer_id"),
            PosSale.location_id.label("location_id"),
            PosLine.item_id.label("item_id"),
            PosLine.base_qty.label("qty"),  # already negative on a refund
            PosLine.line_subtotal.label("sales"),
            (PosLine.base_qty * PosLine.unit_cost).label("cost"),
            PosSale.id.label("doc_id"),
            case((PosSale.kind == "SALE", 1), else_=-1).label("sign"),
        )
        .join(PosLine, PosLine.sale_id == PosSale.id)
        .where(PosSale.company_id == p.company_id, day.between(date_from, date_to))
    )
    if locations is not None:
        pos = pos.where(PosSale.location_id.in_(locations))
    parts.append(pos)
    return union_all(*parts).subquery()


def sales_summary(
    db: Session,
    p: Principal,
    *,
    date_from: date,
    date_to: date,
    group_by: GroupBy = "day",
    location_id: uuid.UUID | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    p.require("report.run", "sal.doc.read")
    if date_to < date_from:
        date_from, date_to = date_to, date_from
    s = _sales_lines(p, date_from, date_to, _location_filter(p, location_id))
    show_cost = p.has("field.cost.read")

    if group_by == "day":
        key, label = s.c.doc_date, s.c.doc_date
        joins = []
    elif group_by == "item":
        key, label = s.c.item_id, func.concat(Item.code, " ", Item.name)
        joins = [(Item, Item.id == s.c.item_id)]
    elif group_by == "customer":
        key = s.c.customer_id
        label = case(
            (Customer.id.is_(None), "Walk-in (POS)"),
            else_=func.concat(Customer.code, " ", Customer.name),
        )
        joins = [(Customer, Customer.id == s.c.customer_id)]
    else:
        key, label = s.c.location_id, func.concat(Location.code, " ", Location.name)
        joins = [(Location, Location.id == s.c.location_id)]

    sales = func.sum(s.c.sales)
    stmt = select(
        key.label("key"),
        label.label("label"),
        func.sum(s.c.qty).label("qty"),
        sales.label("sales"),
        func.sum(s.c.cost).label("cost"),
        func.count(func.distinct(case((s.c.sign == 1, s.c.doc_id)))).label("documents"),
    ).select_from(s)
    for model, on in joins:
        stmt = stmt.outerjoin(model, on)
    stmt = stmt.group_by(key, label)
    stmt = stmt.order_by(key) if group_by == "day" else stmt.order_by(sales.desc())
    rows = db.execute(stmt.limit(max(1, min(limit, 200)))).all()

    totals = db.execute(
        select(func.coalesce(func.sum(s.c.sales), 0), func.coalesce(func.sum(s.c.cost), 0))
    ).one()

    def row(r) -> dict[str, Any]:
        out: dict[str, Any] = {
            group_by: str(r.label),
            "qty": q4(r.qty),
            "sales": q4(r.sales),
            "documents": r.documents,  # invoices and POS receipts
        }
        if show_cost:
            out["cost"] = q4(r.cost)
            out["gross_profit"] = q4(r.sales - r.cost)
        return out

    result: dict[str, Any] = {
        "date_from": date_from,
        "date_to": date_to,
        "group_by": group_by,
        "rows": [row(r) for r in rows],
        "total_sales": q4(totals[0]),
    }
    if show_cost:
        result["total_cost"] = q4(totals[1])
        result["total_gross_profit"] = q4(totals[0] - totals[1])
    return result


def low_stock(
    db: Session,
    p: Principal,
    *,
    threshold: Decimal = Decimal(5),
    location_id: uuid.UUID | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Items at or below `threshold` base units per location; negative stock first."""
    p.require("report.run", "inv.balance.read")
    locations = _location_filter(p, location_id)
    stmt = (
        select(StockBalance, Item, Location)
        .join(Item, Item.id == StockBalance.item_id)
        .join(Location, Location.id == StockBalance.location_id)
        .where(
            StockBalance.company_id == p.company_id,
            StockBalance.qty <= threshold,
            Item.is_active.is_(True),
        )
    )
    if locations is not None:
        stmt = stmt.where(StockBalance.location_id.in_(locations))
    rows = db.execute(
        stmt.order_by(StockBalance.qty, Item.code).limit(max(1, min(limit, 200)))
    ).all()
    return {
        "threshold": threshold,
        "rows": [
            {
                "item": f"{i.code} {i.name}",
                "location": loc.code,
                "qty": b.qty,
                "uom": i.base_uom,
                "negative": b.qty < ZERO,
            }
            for b, i, loc in rows
        ],
    }


def reorder(
    db: Session,
    p: Principal,
    *,
    location_id: uuid.UUID | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Items at or below their reorder level (shop setting, else the item's), with a
    suggested order quantity: up to the maximum if set, else the reorder quantity, else
    back up to the reorder level."""
    p.require("report.run", "inv.balance.read")
    locations = _location_filter(p, location_id)
    il = ItemLocation
    level = func.coalesce(il.reorder_level, Item.reorder_level)
    max_q = func.coalesce(il.max_qty, Item.max_qty)
    re_q = func.coalesce(il.reorder_qty, Item.reorder_qty)
    qty = func.coalesce(StockBalance.qty, 0)
    stmt = (
        select(Item, Location, qty, level, max_q, re_q, il.shelf)
        .join(Location, Location.company_id == Item.company_id)
        .outerjoin(il, (il.item_id == Item.id) & (il.location_id == Location.id))
        .outerjoin(
            StockBalance,
            (StockBalance.item_id == Item.id) & (StockBalance.location_id == Location.id),
        )
        .where(
            Item.company_id == p.company_id,
            Item.is_active.is_(True),
            Location.is_active.is_(True),
            level.is_not(None),
            qty <= level,
        )
    )
    if locations is not None:
        stmt = stmt.where(Location.id.in_(locations))
    rows = db.execute(
        stmt.order_by(Location.code, (qty - level), Item.code).limit(max(1, min(limit, 500)))
    ).all()
    from app.services.purchasing import on_order  # avoid an import cycle

    item_ids = {r[0].id for r in rows}
    ordered = on_order(db, p.company_id, item_ids, locations)
    last_supplier = _last_suppliers(db, p, item_ids)
    out = []
    for item, loc, q, lv, mx, rq, shelf in rows:
        if mx is not None and mx > q:
            suggest = mx - q
        elif rq:
            suggest = rq
        else:
            suggest = max(lv - q, ZERO)
        coming = ordered.get((item.id, loc.id), ZERO)
        suggest = max(suggest - coming, ZERO)  # what is already on order is not asked again
        sup = last_supplier.get(item.id)
        out.append(
            {
                "item_id": item.id,
                "item_code": item.code,
                "item_name": item.name,
                "uom": item.base_uom,
                "location_id": loc.id,
                "location": loc.code,
                "shelf": shelf,
                "qty": q4(q),
                "reorder_level": lv,
                "max_qty": mx,
                "on_order": q4(coming),
                "suggested_qty": q4(suggest),
                "last_supplier_id": sup[0] if sup else None,
                "last_supplier": sup[1] if sup else None,
            }
        )
    return {"rows": out}


def _last_suppliers(db: Session, p: Principal, item_ids) -> dict:
    """Who last delivered each item (latest posted GRN): the default supplier to reorder from."""
    if not item_ids:
        return {}
    rows = db.execute(
        select(GrnLine.item_id, Supplier.id, Supplier.code)
        .join(Grn, Grn.id == GrnLine.header_id)
        .join(Supplier, Supplier.id == Grn.supplier_id)
        .where(
            Grn.company_id == p.company_id,
            Grn.status == "POSTED",
            GrnLine.item_id.in_(item_ids),
        )
        .order_by(GrnLine.item_id, Grn.doc_date.desc(), Grn.posted_at.desc())
        .distinct(GrnLine.item_id)
    ).all()
    return {i: (sid, code) for i, sid, code in rows}
