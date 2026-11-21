"""Point of sale (design doc: POS module).

sal.pos_shift    one cashier's shift on one till (an approved computer): float, counted
                 cash, variance
sal.pos_sale     a receipt (SALE) or a refund against one (RETURN). Completed at once:
                 there is no draft; stock leaves (or comes back) in the same transaction.
                 client_uuid comes from the till, so a retry or an offline upload never
                 records the same sale twice.
sal.pos_line     items on the receipt, priced from the list (overrides need a supervisor)
sal.pos_payment  how it was paid; a return has negative amounts
sal.promotion    price promotions the till applies by itself (% / RM off, special price,
                 buy X get Y free, N for RM P), by date, store and item / category / brand
sal.member       loyalty members (card number); sal.points_move is their points ledger
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import UUIDPk

Money = Numeric(18, 4)
Qty = Numeric(18, 4)
SHIFT_STATUSES = ("OPEN", "CLOSED")
SALE_KINDS = ("SALE", "RETURN")
METHODS = ("CASH", "CARD", "EWALLET", "VOUCHER", "POINTS")
PROMO_KINDS = ("PERCENT", "AMOUNT", "PRICE", "BUY_GET", "BUNDLE")


class PosShift(UUIDPk, Base):
    __tablename__ = "pos_shift"
    __table_args__ = (
        CheckConstraint(f"status IN {SHIFT_STATUSES}", name="status"),
        # one open shift per till
        Index(
            "uq_sal_pos_shift_open_device",
            "device_id",
            unique=True,
            postgresql_where=text("status = 'OPEN'"),
        ),
        {"schema": "sal"},
    )
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.company.id"), index=True)
    location_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.location.id"), index=True)
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.device.id"), index=True)
    cashier_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.app_user.id"), index=True)
    shift_no: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(10), server_default=text("'OPEN'"))
    opening_float: Mapped[Decimal] = mapped_column(Money)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expected_cash: Mapped[Decimal | None] = mapped_column(Money)
    counted_cash: Mapped[Decimal | None] = mapped_column(Money)
    variance: Mapped[Decimal | None] = mapped_column(Money)  # counted - expected
    close_note: Mapped[str | None] = mapped_column(String(300))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))


CONSOLIDATION_STATUSES = ("OPEN", "RELEASED")


class PosConsolidation(UUIDPk, Base):
    """One month of walk-in POS receipts reported to LHDN as one consolidated e-invoice.
    OPEN = holds its receipts; RELEASED = its e-invoice was cancelled (or it was dropped
    before sending), so the receipts can be consolidated again."""

    __tablename__ = "pos_consolidation"
    __table_args__ = (
        # Not unique per month: receipts uploaded late (offline tills) after the month was
        # consolidated go into a second one. Locking the receipts stops double counting.
        CheckConstraint(f"status IN {CONSOLIDATION_STATUSES}", name="status"),
        {"schema": "sal"},
    )
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.company.id"), index=True)
    doc_no: Mapped[str] = mapped_column(String(30))
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(10), server_default=text("'OPEN'"))
    receipts: Mapped[int] = mapped_column(Integer)
    subtotal: Mapped[Decimal] = mapped_column(Money)
    tax_total: Mapped[Decimal] = mapped_column(Money)
    rounding: Mapped[Decimal] = mapped_column(Money)
    total: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3), server_default=text("'MYR'"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PosSale(UUIDPk, Base):
    __tablename__ = "pos_sale"
    __table_args__ = (
        UniqueConstraint("company_id", "client_uuid"),
        CheckConstraint(f"kind IN {SALE_KINDS}", name="kind"),
        {"schema": "sal"},
    )
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.company.id"), index=True)
    client_uuid: Mapped[uuid.UUID] = mapped_column(Uuid)  # made by the till: idempotency
    kind: Mapped[str] = mapped_column(String(10))
    receipt_no: Mapped[str] = mapped_column(String(30), index=True)
    shift_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sal.pos_shift.id"), index=True)
    location_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.location.id"), index=True)
    cashier_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.app_user.id"))
    customer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("md.customer.id"))
    ref_sale_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sal.pos_sale.id"), index=True
    )  # RETURN: the receipt it refunds
    sold_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    subtotal: Mapped[Decimal] = mapped_column(Money)
    tax_total: Mapped[Decimal] = mapped_column(Money)
    rounding: Mapped[Decimal] = mapped_column(Money, server_default=text("0"))  # 5-sen cash
    total: Mapped[Decimal] = mapped_column(Money)  # what the customer pays (or gets back)
    tendered: Mapped[Decimal] = mapped_column(Money)
    change: Mapped[Decimal] = mapped_column(Money, server_default=text("0"))
    cost_total: Mapped[Decimal] = mapped_column(Money, server_default=text("0"))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)  # supervisor override
    reason: Mapped[str | None] = mapped_column(String(200))  # RETURN
    member_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sal.member.id"), index=True)
    points_earned: Mapped[int] = mapped_column(Integer, server_default=text("0"))  # - on return
    points_used: Mapped[int] = mapped_column(Integer, server_default=text("0"))  # - = given back
    consolidation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sal.pos_consolidation.id"), index=True
    )  # the monthly consolidated e-invoice it was reported in
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    lines: Mapped[list["PosLine"]] = relationship(
        back_populates="sale", cascade="all, delete-orphan", order_by="PosLine.line_no"
    )
    payments: Mapped[list["PosPayment"]] = relationship(
        back_populates="sale", cascade="all, delete-orphan"
    )


class PosLine(UUIDPk, Base):
    __tablename__ = "pos_line"
    __table_args__ = {"schema": "sal"}
    sale_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sal.pos_sale.id", ondelete="CASCADE"), index=True
    )
    line_no: Mapped[int] = mapped_column(Integer)
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("md.item.id"), index=True)
    barcode: Mapped[str | None] = mapped_column(String(30))  # as scanned
    uom: Mapped[str] = mapped_column(String(10))
    qty: Mapped[Decimal] = mapped_column(Qty)  # negative on a RETURN
    factor: Mapped[Decimal] = mapped_column(Qty)
    base_qty: Mapped[Decimal] = mapped_column(Qty)
    unit_price: Mapped[Decimal] = mapped_column(Money)
    list_price: Mapped[Decimal | None] = mapped_column(Money)
    price_source: Mapped[str] = mapped_column(String(10))  # LIST / OVERRIDE / RETURN
    discount_pct: Mapped[Decimal] = mapped_column(Numeric(7, 4), server_default=text("0"))
    tax_code_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("md.tax_code.id"))
    tax_rate: Mapped[Decimal] = mapped_column(Numeric(9, 4), server_default=text("0"))
    line_subtotal: Mapped[Decimal] = mapped_column(Money)
    line_tax: Mapped[Decimal] = mapped_column(Money)
    line_total: Mapped[Decimal] = mapped_column(Money)
    unit_cost: Mapped[Decimal] = mapped_column(Money, server_default=text("0"))  # per base
    ref_line_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)  # RETURN: the line refunded
    # promotion applied by the till: RM taken off this line before tax (line_subtotal is net)
    promo_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sal.promotion.id"))
    promo_amount: Mapped[Decimal] = mapped_column(Money, server_default=text("0"))
    sale: Mapped[PosSale] = relationship(back_populates="lines")


class PosPayment(UUIDPk, Base):
    __tablename__ = "pos_payment"
    __table_args__ = (CheckConstraint(f"method IN {METHODS}", name="method"), {"schema": "sal"})
    sale_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sal.pos_sale.id", ondelete="CASCADE"), index=True
    )
    method: Mapped[str] = mapped_column(String(10))
    amount: Mapped[Decimal] = mapped_column(Money)  # net of change; negative = refund
    reference: Mapped[str | None] = mapped_column(String(60))  # card approval code etc.
    sale: Mapped[PosSale] = relationship(back_populates="payments")


class Promotion(UUIDPk, Base):
    """value: PERCENT % off; AMOUNT RM off per base unit; PRICE special price per base unit;
    BUNDLE price for buy_qty units. BUY_GET: buy buy_qty, get get_qty of them free (the
    cheapest). Targets: items, categories, brands (all empty = every item); stores (empty =
    all). One promotion per line - the one that saves the customer most."""

    __tablename__ = "promotion"
    __table_args__ = (
        CheckConstraint(f"kind IN {PROMO_KINDS}", name="kind"),
        UniqueConstraint("company_id", "code"),
        {"schema": "sal"},
    )
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.company.id"), index=True)
    code: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(100))  # shown on the receipt
    kind: Mapped[str] = mapped_column(String(10))
    value: Mapped[Decimal] = mapped_column(Money, server_default=text("0"))
    buy_qty: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    get_qty: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    date_from: Mapped[date] = mapped_column(Date)
    date_to: Mapped[date] = mapped_column(Date)
    members_only: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    item_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid), server_default=text("'{}'"))
    category_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid), server_default=text("'{}'"))
    brand_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid), server_default=text("'{}'"))
    location_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid), server_default=text("'{}'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))


class Member(UUIDPk, Base):
    """A loyalty member: earns points on till sales and spends them as a payment."""

    __tablename__ = "member"
    __table_args__ = (UniqueConstraint("company_id", "card_no"), {"schema": "sal"})
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.company.id"), index=True)
    card_no: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(100))
    phone: Mapped[str | None] = mapped_column(String(30), index=True)
    email: Mapped[str | None] = mapped_column(String(100))
    points: Mapped[int] = mapped_column(Integer, server_default=text("0"))  # kept by moves
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))


class PointsMove(Base):
    """Every change to a member's points: EARN / REDEEM on a sale, back on a return, ADJUST."""

    __tablename__ = "points_move"
    __table_args__ = {"schema": "sal"}
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    member_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sal.member.id"), index=True)
    sale_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sal.pos_sale.id"))
    kind: Mapped[str] = mapped_column(String(10))
    points: Mapped[int] = mapped_column(Integer)
    balance: Mapped[int] = mapped_column(Integer)  # after this move
    note: Mapped[str | None] = mapped_column(String(200))
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
