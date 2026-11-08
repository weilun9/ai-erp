"""Inventory (schema inv): append-only stock ledger and the balance it maintains."""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Numeric,
    String,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

Qty = Numeric(18, 4)
Cost = Numeric(18, 4)


class StockLedger(Base):
    """Every stock movement, never updated or deleted. Voids add reversing rows."""

    __tablename__ = "stock_ledger"
    __table_args__ = (
        Index("ix_inv_stock_ledger_item_loc", "item_id", "location_id", "id"),
        Index("ix_inv_stock_ledger_doc", "doc_type", "doc_id"),
        {"schema": "inv"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("md.item.id"))
    location_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.location.id"))
    doc_type: Mapped[str] = mapped_column(String(10))
    doc_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    doc_no: Mapped[str] = mapped_column(String(30))
    line_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    qty_in: Mapped[Decimal] = mapped_column(Qty, server_default=text("0"))
    qty_out: Mapped[Decimal] = mapped_column(Qty, server_default=text("0"))
    unit_cost: Mapped[Decimal] = mapped_column(Cost)  # per base unit
    balance_qty: Mapped[Decimal] = mapped_column(Qty)  # after this movement
    avg_cost: Mapped[Decimal] = mapped_column(Cost)  # after this movement
    is_reversal: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))


class StockBalance(Base):
    """Current quantity and moving-average cost per item and location (from the ledger)."""

    __tablename__ = "stock_balance"
    __table_args__ = {"schema": "inv"}

    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("md.item.id"), primary_key=True)
    location_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.location.id"), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    qty: Mapped[Decimal] = mapped_column(Qty, server_default=text("0"))
    avg_cost: Mapped[Decimal] = mapped_column(Cost, server_default=text("0"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
