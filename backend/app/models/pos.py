"""Point of sale (design doc: POS module).

sal.pos_shift    one cashier's shift on one till (an approved computer): float, counted
                 cash, variance
sal.pos_sale     a receipt (SALE) or a refund against one (RETURN). Completed at once:
                 there is no draft; stock leaves (or comes back) in the same transaction.
                 client_uuid comes from the till, so a retry or an offline upload never
                 records the same sale twice.
sal.pos_line     items on the receipt, priced from the list (overrides need a supervisor)
sal.pos_payment  how it was paid; a return has negative amounts
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import UUIDPk

Money = Numeric(18, 4)
Qty = Numeric(18, 4)
SHIFT_STATUSES = ("OPEN", "CLOSED")
SALE_KINDS = ("SALE", "RETURN")
METHODS = ("CASH", "CARD", "EWALLET", "VOUCHER")


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
