"""Point of sale: shifts, sales, returns.

Rules (company SOP in the knowledge base, plus Malaysian practice):
- A till is an approved computer. It has at most one open shift; sales go into the
  cashier's own open shift on that till.
- Prices come from the item list. A different price or a discount needs a supervisor
  (someone with pos.supervise) to key in their own username and password.
- Cash totals are rounded to the nearest 5 sen (rounding mechanism); card and e-wallet
  are charged to the cent. Change is only given from cash.
- A return refers to the original receipt, can never refund more than was sold (rows are
  locked so two tills cannot both refund the last item), puts stock back at the cost it
  left at, and refunds by one of the original payment methods. Refunds above
  POS_REFUND_APPROVAL_LIMIT need a supervisor.
- Closing a shift compares counted cash with float + cash taken - cash refunded. A
  difference above POS_VARIANCE_NOTE_LIMIT must be explained.
- Every sale carries the till's client_uuid: sending it again returns the first result,
  so a retry or an offline upload never records a sale twice.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.core.config import get_settings
from app.core.errors import BadRequest, BusinessRule, DocState, Forbidden, NotFound
from app.localization.my import TIMEZONE
from app.models import (
    AppUser,
    Item,
    Location,
    PosLine,
    PosPayment,
    PosSale,
    PosShift,
    Tag,
    TaxCode,
)
from app.services import audit, masterdata
from app.services.auth import Principal, authenticate
from app.services.doclib import line_amounts, q2, q4
from app.services.numbering import next_doc_no
from app.services.stock import Move, apply_moves

ZERO = Decimal(0)
FIVE_SEN = Decimal("0.05")
CLOCK_SKEW = timedelta(minutes=5)


def round05(v: Decimal) -> Decimal:
    """Malaysian rounding mechanism: to the nearest 5 sen (x.x1, x.x2 down; x.x3, x.x4 up)."""
    return (v / FIVE_SEN).quantize(Decimal(1), rounding=ROUND_HALF_UP) * FIVE_SEN


# --- supervisor override ------------------------------------------------------------------


@dataclass
class Approval:
    user_id: uuid.UUID | None
    reasons: list[str]


def _supervisor(db: Session, p: Principal, cred: dict[str, str] | None, reasons: list[str]):
    """Nothing needed, the cashier may do it, or a supervisor keyed in their password."""
    if not reasons or p.has("pos.supervise"):
        return Approval(p.user_id if reasons else None, reasons)
    if not cred:
        raise Forbidden(
            "A supervisor must approve this", {"code": "SUPERVISOR_REQUIRED", "reasons": reasons}
        )
    user = authenticate(db, cred.get("username", ""), cred.get("password", ""))
    if user is None or user.company_id != p.company_id:
        raise Forbidden("Supervisor login failed", {"code": "SUPERVISOR_REQUIRED"})
    user = db.scalar(
        select(AppUser)
        .where(AppUser.id == user.id)
        .options(selectinload(AppUser.tags).selectinload(Tag.permissions))
    )
    if "pos.supervise" not in {perm.code for t in user.tags for perm in t.permissions}:
        raise Forbidden("That user may not approve this", {"code": "SUPERVISOR_REQUIRED"})
    return Approval(user.id, reasons)


# --- shifts -------------------------------------------------------------------------------


def current_shift(db: Session, p: Principal, *, lock: bool = False) -> PosShift | None:
    stmt = select(PosShift).where(PosShift.device_id == p.device_id, PosShift.status == "OPEN")
    if lock:
        stmt = stmt.with_for_update()
    return db.scalar(stmt)


def open_shift(
    db: Session, p: Principal, location_id: uuid.UUID, opening_float: Decimal
) -> PosShift:
    p.require("pos.sale.create")
    loc = db.get(Location, location_id)
    if loc is None or loc.company_id != p.company_id or not p.can_see_location(location_id):
        raise NotFound("Location not found")
    if current_shift(db, p) is not None:
        raise DocState("This till already has an open shift; close it first")
    shift = PosShift(
        company_id=p.company_id,
        location_id=location_id,
        device_id=p.device_id,
        cashier_id=p.user_id,
        shift_no=next_doc_no(db, p.company_id, "SHIFT"),
        opening_float=q2(opening_float),
    )
    db.add(shift)
    try:
        db.commit()  # the partial unique index stops two shifts on one till
    except IntegrityError as exc:
        db.rollback()
        raise DocState("This till already has an open shift; close it first") from exc
    db.refresh(shift)
    return shift


def _my_open_shift(db: Session, p: Principal) -> PosShift:
    shift = current_shift(db, p, lock=True)
    if shift is None:
        raise DocState("Open a shift on this till first")
    if shift.cashier_id != p.user_id:
        raise DocState("Another cashier's shift is open on this till; they must close it")
    return shift


def cash_expected(db: Session, shift: PosShift) -> Decimal:
    cash = db.scalar(
        select(func.coalesce(func.sum(PosPayment.amount), 0))
        .join(PosSale, PosSale.id == PosPayment.sale_id)
        .where(PosSale.shift_id == shift.id, PosPayment.method == "CASH")
    )
    return q2(shift.opening_float + cash)


def close_shift(
    db: Session, p: Principal, shift_id: uuid.UUID, counted_cash: Decimal, note: str | None
) -> PosShift:
    p.require("pos.shift.close")
    shift = db.scalar(select(PosShift).where(PosShift.id == shift_id).with_for_update())
    if shift is None or shift.company_id != p.company_id:
        raise NotFound("Shift not found")
    if shift.cashier_id != p.user_id and not p.has("pos.supervise"):
        raise Forbidden("Only the cashier or a supervisor can close this shift")
    if shift.status != "OPEN":
        raise DocState("This shift is already closed")
    expected = cash_expected(db, shift)
    variance = q2(counted_cash - expected)
    limit = Decimal(str(get_settings().pos_variance_note_limit))
    if abs(variance) > limit and not (note and note.strip()):
        raise BusinessRule(
            f"Cash is off by more than RM{limit:.2f}: recount, and if it still differs, "
            "explain it in the note",
            {
                "expected": str(expected),
                "counted": str(q2(counted_cash)),
                "variance": str(variance),
            },
        )
    shift.expected_cash, shift.counted_cash, shift.variance = expected, q2(counted_cash), variance
    shift.status, shift.closed_at, shift.closed_by = "CLOSED", datetime.now(UTC), p.user_id
    shift.close_note = note.strip()[:300] if note else None
    shift.version += 1
    audit.record(
        db,
        p,
        "pos_shift_close",
        "sal.pos_shift",
        shift.id,
        after={"expected": expected, "counted": shift.counted_cash, "variance": variance},
    )
    db.commit()
    db.refresh(shift)  # amounts as stored
    return shift


def get_shift(db: Session, p: Principal, shift_id: uuid.UUID) -> PosShift:
    shift = db.get(PosShift, shift_id)
    if shift is None or shift.company_id != p.company_id:
        raise NotFound("Shift not found")
    if shift.cashier_id != p.user_id and not p.has("pos.supervise"):
        raise NotFound("Shift not found")
    return shift


def shift_report(db: Session, p: Principal, shift_id: uuid.UUID) -> dict[str, Any]:
    """The X / Z report: what the shift took, by method, and the cash it should hold."""
    shift = get_shift(db, p, shift_id)
    by_method = dict(
        db.execute(
            select(PosPayment.method, func.sum(PosPayment.amount))
            .join(PosSale, PosSale.id == PosPayment.sale_id)
            .where(PosSale.shift_id == shift.id)
            .group_by(PosPayment.method)
        ).all()
    )
    sums = {
        kind: row
        for kind, *row in db.execute(
            select(
                PosSale.kind,
                func.count(),
                func.sum(PosSale.subtotal),
                func.sum(PosSale.tax_total),
                func.sum(PosSale.rounding),
                func.sum(PosSale.total),
            )
            .where(PosSale.shift_id == shift.id)
            .group_by(PosSale.kind)
        )
    }

    def part(kind):
        n, sub, tax, rnd, tot = sums.get(kind, (0, ZERO, ZERO, ZERO, ZERO))
        return {
            "count": n,
            "subtotal": q2(sub),
            "tax": q2(tax),
            "rounding": q2(rnd),
            "total": q2(tot),
        }

    return {
        "shift_no": shift.shift_no,
        "status": shift.status,
        "opened_at": shift.opened_at,
        "closed_at": shift.closed_at,
        "opening_float": shift.opening_float,
        "sales": part("SALE"),
        "returns": part("RETURN"),
        "payments": {m: q2(v) for m, v in sorted(by_method.items())},
        "expected_cash": cash_expected(db, shift),
        "counted_cash": shift.counted_cash,
        "variance": shift.variance,
    }


def list_shifts(db: Session, p: Principal, *, status: str | None = None) -> list[PosShift]:
    stmt = select(PosShift).where(PosShift.company_id == p.company_id)
    if not p.has("pos.supervise"):
        stmt = stmt.where(PosShift.cashier_id == p.user_id)
    if p.location_ids is not None:
        stmt = stmt.where(PosShift.location_id.in_(p.location_ids))
    if status:
        stmt = stmt.where(PosShift.status == status)
    return list(db.scalars(stmt.order_by(PosShift.opened_at.desc()).limit(200)))


# --- selling ------------------------------------------------------------------------------


def _claim(db: Session, p: Principal, client_uuid: uuid.UUID) -> PosSale | None:
    """Serialize work on one client_uuid (a till retrying while the first try is still
    running), then return the sale if it was already recorded."""
    db.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(str(client_uuid), 0))))
    return _replay(db, p, client_uuid)


def _replay(db: Session, p: Principal, client_uuid: uuid.UUID) -> PosSale | None:
    return db.scalar(
        select(PosSale).where(
            PosSale.company_id == p.company_id, PosSale.client_uuid == client_uuid
        )
    )


def _when(shift: PosShift, sold_at: datetime | None) -> datetime:
    now = datetime.now(UTC)
    if sold_at is None:
        return now
    if sold_at.tzinfo is None:
        raise BadRequest("sold_at needs a time zone")
    if sold_at > now + CLOCK_SKEW or sold_at < shift.opened_at - CLOCK_SKEW:
        raise BadRequest("sold_at must fall inside this shift (check the till's clock)")
    return sold_at


def _resolve(db: Session, p: Principal, n: int, ln: dict[str, Any]) -> tuple[Item, str, str | None]:
    if ln.get("barcode"):
        hits = masterdata.lookup_barcode(db, p, ln["barcode"])
        items = {i.id for i, _ in hits}
        if not hits:
            raise BadRequest("Barcode not found", {"line": n, "barcode": ln["barcode"]})
        if len(items) > 1:
            raise BadRequest(
                "Barcode matches several items", {"line": n, "items": [i.code for i, _ in hits]}
            )
        item, code = hits[0]
        return item, ln.get("uom") or code.uom, code.barcode
    if ln.get("item_id"):
        item = db.scalar(
            select(Item)
            .where(Item.id == ln["item_id"], Item.company_id == p.company_id)
            .options(selectinload(Item.uoms))
        )
        if item is None:
            raise BadRequest("Item not found", {"line": n})
        return item, ln.get("uom") or item.base_uom, None
    raise BadRequest("Each line needs a barcode or an item", {"line": n})


def _settle(
    total: Decimal, payments: list[dict[str, Any]]
) -> tuple[Decimal, Decimal, Decimal, list[PosPayment]]:
    """-> (rounding, tendered, change, payment rows). Cash is rounded to 5 sen."""
    if not payments:
        raise BadRequest("Add a payment")
    other = sum((Decimal(str(x["amount"])) for x in payments if x["method"] != "CASH"), ZERO)
    cash_in = sum((Decimal(str(x["amount"])) for x in payments if x["method"] == "CASH"), ZERO)
    if other > total:
        raise BusinessRule(
            "Card / e-wallet payments are more than the bill",
            {"total": str(total), "non_cash": str(other)},
        )
    has_cash = any(x["method"] == "CASH" for x in payments)
    cash_due = total - other
    rounding = round05(cash_due) - cash_due if has_cash else ZERO
    cash_due += rounding
    if has_cash and cash_in < cash_due:
        raise BusinessRule("Not enough paid", {"due": str(cash_due), "paid": str(cash_in)})
    if not has_cash and other != total:
        raise BusinessRule(
            "Payments do not add up to the bill", {"total": str(total), "paid": str(other)}
        )
    change = cash_in - cash_due if has_cash else ZERO
    rows = [
        PosPayment(
            method=x["method"],
            amount=q2(Decimal(str(x["amount"]))) if x["method"] != "CASH" else q2(cash_due),
            reference=x.get("reference"),
        )
        for x in payments
        if x["method"] != "CASH"
    ]
    if has_cash:
        rows.append(PosPayment(method="CASH", amount=q2(cash_due), reference=None))
    return q2(rounding), q2(other + cash_in), q2(change), rows


def sell(db: Session, p: Principal, body: dict[str, Any]) -> tuple[PosSale, bool]:
    """-> (sale, replayed). replayed = this client_uuid was already recorded."""
    p.require("pos.sale.create")
    if (old := _claim(db, p, body["client_uuid"])) is not None:
        db.commit()  # release the lock
        return get_sale(db, p, sale_id=old.id), True
    shift = _my_open_shift(db, p)
    if not body.get("lines"):
        raise BadRequest("A sale needs at least one line")
    taxes = {t.id: t for t in db.scalars(select(TaxCode).where(TaxCode.company_id == p.company_id))}

    lines, needs = [], []
    for n, ln in enumerate(body["lines"], start=1):
        item, uom_code, barcode = _resolve(db, p, n, ln)
        uom = next((u for u in item.uoms if u.uom == uom_code), None)
        if uom is None:
            raise BadRequest(
                "Unit is not one of the item's units",
                {"line": n, "item": item.code, "allowed": [u.uom for u in item.uoms]},
            )
        qty = Decimal(str(ln["qty"]))
        if qty <= 0:
            raise BadRequest("Quantity must be above zero", {"line": n})
        disc = Decimal(str(ln.get("discount_pct") or 0))
        listed = uom.selling_price
        if ln.get("unit_price") is not None:
            price, source = Decimal(str(ln["unit_price"])), "OVERRIDE"
        elif listed is not None:
            price, source = listed, "LIST"
        else:
            raise BusinessRule(
                "This item has no selling price; ask a supervisor", {"line": n, "item": item.code}
            )
        if source == "OVERRIDE" and price != listed:
            needs.append(f"line {n}: price {price} instead of {listed}")
        if disc:
            needs.append(f"line {n}: {disc}% discount")
        tax = taxes.get(item.sales_tax_code_id) if item.sales_tax_code_id else None
        rate = tax.rate if tax else ZERO
        subtotal, line_tax = line_amounts(qty, price, disc, rate)
        lines.append(
            PosLine(
                line_no=n,
                item_id=item.id,
                barcode=barcode,
                uom=uom.uom,
                qty=qty,
                factor=uom.factor,
                base_qty=q4(qty * uom.factor),
                unit_price=price,
                list_price=listed,
                price_source="LIST" if price == listed else "OVERRIDE",
                discount_pct=disc,
                tax_code_id=tax.id if tax else None,
                tax_rate=rate,
                line_subtotal=subtotal,
                line_tax=line_tax,
                line_total=subtotal + line_tax,
            )
        )
    approval = _supervisor(db, p, body.get("supervisor"), needs)

    subtotal = sum((x.line_subtotal for x in lines), ZERO)
    tax_total = sum((x.line_tax for x in lines), ZERO)
    rounding, tendered, change, payments = _settle(subtotal + tax_total, body["payments"])

    sale = PosSale(
        company_id=p.company_id,
        client_uuid=body["client_uuid"],
        kind="SALE",
        receipt_no=next_doc_no(db, p.company_id, "POS"),
        shift_id=shift.id,
        location_id=shift.location_id,
        cashier_id=p.user_id,
        customer_id=body.get("customer_id"),
        sold_at=_when(shift, body.get("sold_at")),
        subtotal=subtotal,
        tax_total=tax_total,
        rounding=rounding,
        total=subtotal + tax_total + rounding,
        tendered=tendered,
        change=change,
        approved_by=approval.user_id if approval.reasons else None,
        lines=lines,
        payments=payments,
    )
    return _book(
        db,
        p,
        sale,
        [Move(x.item_id, shift.location_id, -x.base_qty, None) for x in lines],
        approval,
    )


def _book(db, p, sale: PosSale, moves: list[Move], approval: Approval) -> tuple[PosSale, bool]:
    db.add(sale)
    try:
        db.flush()
    except IntegrityError:  # backstop; _claim normally serializes the same client_uuid
        db.rollback()
        return _replay(db, p, sale.client_uuid), True
    moves = [
        Move(m.item_id, m.location_id, m.qty, m.unit_cost, line.id)
        for m, line in zip(moves, sale.lines, strict=True)
    ]
    rows = apply_moves(
        db,
        company_id=p.company_id,
        doc_type="POS" if sale.kind == "SALE" else "POSR",
        doc_id=sale.id,
        doc_no=sale.receipt_no,
        moves=moves,
    )
    for line, row in zip(sale.lines, rows, strict=True):
        line.unit_cost = row.unit_cost
    sale.cost_total = q2(sum((abs(x.base_qty) * x.unit_cost for x in sale.lines), ZERO))
    if approval.reasons:
        audit.record(
            db,
            p,
            "pos_override",
            "sal.pos_sale",
            sale.id,
            after={"approved_by": approval.user_id, "reasons": approval.reasons},
        )
    db.commit()
    return get_sale(db, p, sale_id=sale.id), False  # reloaded: amounts as stored


# --- returns ------------------------------------------------------------------------------


def refund(db: Session, p: Principal, body: dict[str, Any]) -> tuple[PosSale, bool]:
    p.require("pos.return.create")
    if (old := _claim(db, p, body["client_uuid"])) is not None:
        db.commit()  # release the lock
        return get_sale(db, p, sale_id=old.id), True
    shift = _my_open_shift(db, p)
    original = db.scalar(
        select(PosSale)
        .where(
            PosSale.company_id == p.company_id,
            PosSale.receipt_no == body["receipt_no"],
            PosSale.kind == "SALE",
        )
        .options(selectinload(PosSale.lines), selectinload(PosSale.payments))
        .with_for_update()  # one refund of a receipt at a time
    )
    if original is None or not p.can_see_location(original.location_id):
        raise NotFound("Receipt not found")
    done = {
        ref: (qty, sub, tax)
        for ref, qty, sub, tax in db.execute(
            select(
                PosLine.ref_line_id,
                func.sum(-PosLine.qty),
                func.sum(-PosLine.line_subtotal),
                func.sum(-PosLine.line_tax),
            )
            .join(PosSale, PosSale.id == PosLine.sale_id)
            .where(PosSale.ref_sale_id == original.id)
            .group_by(PosLine.ref_line_id)
        )
    }
    by_no = {x.line_no: x for x in original.lines}
    lines, moves = [], []
    for n, ln in enumerate(body["lines"], start=1):
        src = by_no.get(ln["line_no"])
        if src is None:
            raise BadRequest("No such line on the receipt", {"line_no": ln["line_no"]})
        qty = Decimal(str(ln["qty"]))
        got_qty, got_sub, got_tax = done.get(src.id, (ZERO, ZERO, ZERO))
        left = src.qty - got_qty
        if qty <= 0 or qty > left:
            raise BusinessRule(
                "Cannot return more than was sold",
                {"line_no": src.line_no, "can_return": str(left)},
            )
        if qty == left:  # the last of this line: exactly what is left, so rounding over
            subtotal = src.line_subtotal - got_sub  # several returns never refunds more
            line_tax = src.line_tax - got_tax  # than was charged
        else:
            subtotal, line_tax = line_amounts(qty, src.unit_price, src.discount_pct, src.tax_rate)
        base = q4(qty * src.factor)
        lines.append(
            PosLine(
                line_no=n,
                item_id=src.item_id,
                barcode=src.barcode,
                uom=src.uom,
                qty=-qty,
                factor=src.factor,
                base_qty=-base,
                unit_price=src.unit_price,
                list_price=src.list_price,
                price_source="RETURN",
                discount_pct=src.discount_pct,
                tax_code_id=src.tax_code_id,
                tax_rate=src.tax_rate,
                line_subtotal=-subtotal,
                line_tax=-line_tax,
                line_total=-(subtotal + line_tax),
                ref_line_id=src.id,
            )
        )
        moves.append(Move(src.item_id, shift.location_id, base, src.unit_cost))
    if not lines:
        raise BadRequest("Choose what is being returned")

    subtotal = sum((x.line_subtotal for x in lines), ZERO)
    tax_total = sum((x.line_tax for x in lines), ZERO)
    gross = subtotal + tax_total  # negative
    paid_with = [x.method for x in original.payments]
    method = body.get("refund_method") or paid_with[0]
    if method not in paid_with:  # SOP: refund the way they paid
        raise BusinessRule("Refund by one of the ways it was paid", {"allowed": paid_with})
    rounding = round05(gross) - gross if method == "CASH" else ZERO
    total = gross + rounding
    limit = Decimal(str(get_settings().pos_refund_approval_limit))
    needs = [f"refund RM{-total:.2f} is above RM{limit:.2f}"] if -total > limit else []
    approval = _supervisor(db, p, body.get("supervisor"), needs)

    sale = PosSale(
        company_id=p.company_id,
        client_uuid=body["client_uuid"],
        kind="RETURN",
        receipt_no=next_doc_no(db, p.company_id, "POSR"),
        shift_id=shift.id,
        location_id=shift.location_id,
        cashier_id=p.user_id,
        customer_id=original.customer_id,
        ref_sale_id=original.id,
        sold_at=datetime.now(UTC),
        subtotal=subtotal,
        tax_total=tax_total,
        rounding=q2(rounding),
        total=q2(total),
        tendered=q2(total),
        approved_by=approval.user_id if approval.reasons else None,
        reason=(body.get("reason") or "")[:200] or None,
        lines=lines,
        payments=[PosPayment(method=method, amount=q2(total), reference=None)],
    )
    audit.record(
        db,
        p,
        "pos_refund",
        "sal.pos_sale",
        original.id,
        after={"receipt": original.receipt_no, "refund": q2(total), "method": method},
    )
    return _book(db, p, sale, moves, approval)


# --- reading ------------------------------------------------------------------------------


def get_sale(db: Session, p: Principal, *, sale_id=None, receipt_no=None) -> PosSale:
    p.require("pos.sale.create")
    stmt = (
        select(PosSale)
        .where(PosSale.company_id == p.company_id)
        .options(selectinload(PosSale.lines), selectinload(PosSale.payments))
    )
    stmt = (
        stmt.where(PosSale.id == sale_id)
        if sale_id
        else stmt.where(PosSale.receipt_no == receipt_no)
    )
    sale = db.scalar(stmt.execution_options(populate_existing=True))  # as stored
    if sale is None or not p.can_see_location(sale.location_id):
        raise NotFound("Receipt not found")
    return sale


def tax_summary(db: Session, p: Principal, date_from: date, date_to: date) -> dict[str, Any]:
    """POS takings by tax code for a period: the basis of the monthly consolidated
    e-invoice for walk-in customers and of the SST return."""
    p.require("report.run")
    tz = ZoneInfo(TIMEZONE)  # business days, not UTC days
    start = datetime.combine(date_from, datetime.min.time(), tz)
    end = datetime.combine(date_to + timedelta(days=1), datetime.min.time(), tz)
    base = [PosSale.company_id == p.company_id, PosSale.sold_at >= start, PosSale.sold_at < end]
    if p.location_ids is not None:
        base.append(PosSale.location_id.in_(p.location_ids))
    rows = db.execute(
        select(
            TaxCode.code,
            PosLine.tax_rate,
            PosSale.kind,
            func.sum(PosLine.line_subtotal),
            func.sum(PosLine.line_tax),
        )
        .join(PosSale, PosSale.id == PosLine.sale_id)
        .outerjoin(TaxCode, TaxCode.id == PosLine.tax_code_id)
        .where(*base)
        .group_by(TaxCode.code, PosLine.tax_rate, PosSale.kind)
        .order_by(TaxCode.code, PosSale.kind)
    ).all()
    receipts = db.execute(
        select(
            func.count(),
            func.min(PosSale.receipt_no),
            func.max(PosSale.receipt_no),
            func.coalesce(func.sum(PosSale.rounding), 0),
        ).where(*base, PosSale.kind == "SALE")
    ).one()
    return {
        "date_from": date_from,
        "date_to": date_to,
        "receipts": receipts[0],
        "first_receipt": receipts[1],
        "last_receipt": receipts[2],
        "rounding": q2(receipts[3]),
        "by_tax": [
            {
                "tax_code": code or "NONE",
                "rate": rate,
                "kind": kind,
                "subtotal": q2(sub),
                "tax": q2(tax),
            }
            for code, rate, kind, sub, tax in rows
        ],
    }
