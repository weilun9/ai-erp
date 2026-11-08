"""Stock documents (schema inv): adjustment, write-off, transfer, stocktake.

Same header (_h) + lines (_l) shape and DRAFT -> SAVED -> POSTED -> VOIDED lifecycle as
the trading documents. Lines carry `unit_cost` per base unit, filled from the stock ledger
on post (an adjustment may give the cost of stock it adds).
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, declared_attr, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import AuditColumns, UUIDPk

Money = Numeric(18, 4)
Qty = Numeric(18, 4)
STATUSES = ("DRAFT", "SAVED", "POSTED", "VOIDED")


class StockHeader(UUIDPk, AuditColumns):
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.company.id"), index=True)
    doc_no: Mapped[str | None] = mapped_column(String(30))
    doc_date: Mapped[date] = mapped_column(Date)
    location_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.location.id"), index=True)
    status: Mapped[str] = mapped_column(String(10), server_default=text("'DRAFT'"))
    reason: Mapped[str | None] = mapped_column(String(20))
    remark: Mapped[str | None] = mapped_column(String(500))
    cost_total: Mapped[Decimal] = mapped_column(Money, server_default=text("0"))  # on post
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    posted_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voided_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    void_reason: Mapped[str | None] = mapped_column(String(300))

    @declared_attr.directive
    def __table_args__(cls):
        return (
            UniqueConstraint("company_id", "doc_no"),
            CheckConstraint(f"status IN {STATUSES}", name="status"),
            {"schema": "inv"},
        )


class StockLine(UUIDPk):
    line_no: Mapped[int] = mapped_column(Integer)
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("md.item.id"))
    uom: Mapped[str] = mapped_column(String(10))
    # Adjustment: + adds, - removes. Stocktake: the counted quantity. Others: quantity.
    qty: Mapped[Decimal] = mapped_column(Qty)
    factor: Mapped[Decimal] = mapped_column(Qty)
    base_qty: Mapped[Decimal] = mapped_column(Qty)
    unit_cost: Mapped[Decimal] = mapped_column(Money, server_default=text("0"))
    reason: Mapped[str | None] = mapped_column(String(20))
    remark: Mapped[str | None] = mapped_column(String(200))


def _line_fk(table: str):
    return mapped_column(ForeignKey(f"inv.{table}.id", ondelete="CASCADE"), index=True)


class StockAdjustment(StockHeader, Base):
    __tablename__ = "adjustment_h"
    lines: Mapped[list["StockAdjustmentLine"]] = relationship(
        back_populates="header",
        cascade="all, delete-orphan",
        order_by="StockAdjustmentLine.line_no",
    )


class StockAdjustmentLine(StockLine, Base):
    __tablename__ = "adjustment_l"
    __table_args__ = {"schema": "inv"}
    header_id: Mapped[uuid.UUID] = _line_fk("adjustment_h")
    # Cost of stock added, as typed (per base unit); None = current average
    given_cost: Mapped[Decimal | None] = mapped_column(Money)
    header: Mapped[StockAdjustment] = relationship(back_populates="lines")


class StockWriteOff(StockHeader, Base):
    __tablename__ = "writeoff_h"
    lines: Mapped[list["StockWriteOffLine"]] = relationship(
        back_populates="header", cascade="all, delete-orphan", order_by="StockWriteOffLine.line_no"
    )


class StockWriteOffLine(StockLine, Base):
    __tablename__ = "writeoff_l"
    __table_args__ = {"schema": "inv"}
    header_id: Mapped[uuid.UUID] = _line_fk("writeoff_h")
    header: Mapped[StockWriteOff] = relationship(back_populates="lines")


class StockTransfer(StockHeader, Base):
    """Moves stock from location_id to to_location_id at the cost it leaves with."""

    __tablename__ = "transfer_h"
    to_location_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.location.id"), index=True)
    lines: Mapped[list["StockTransferLine"]] = relationship(
        back_populates="header", cascade="all, delete-orphan", order_by="StockTransferLine.line_no"
    )


class StockTransferLine(StockLine, Base):
    __tablename__ = "transfer_l"
    __table_args__ = {"schema": "inv"}
    header_id: Mapped[uuid.UUID] = _line_fk("transfer_h")
    header: Mapped[StockTransfer] = relationship(back_populates="lines")


class Stocktake(StockHeader, Base):
    """Counted quantities; posting books the difference against the system quantity at
    counted_at. With zero_uncounted, items not on the count are taken as zero."""

    __tablename__ = "stocktake_h"
    zero_uncounted: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # When the shelves were counted (set on save if not given). The difference is taken
    # against the system quantity at this moment, so sales after the count still count.
    counted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lines: Mapped[list["StocktakeLine"]] = relationship(
        back_populates="header", cascade="all, delete-orphan", order_by="StocktakeLine.line_no"
    )


class StocktakeLine(StockLine, Base):
    __tablename__ = "stocktake_l"
    __table_args__ = {"schema": "inv"}
    header_id: Mapped[uuid.UUID] = _line_fk("stocktake_h")
    system_qty: Mapped[Decimal | None] = mapped_column(Qty)  # base units, on post
    diff_qty: Mapped[Decimal | None] = mapped_column(Qty)  # counted - system, on post
    header: Mapped[Stocktake] = relationship(back_populates="lines")
