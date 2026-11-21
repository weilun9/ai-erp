"""Inventory (schema inv): append-only stock ledger and the balance it maintains."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Numeric,
    String,
    UniqueConstraint,
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


class Lot(Base):
    """A batch (with an optional expiry date) or a serial number of one item."""

    __tablename__ = "lot"
    __table_args__ = (
        UniqueConstraint("company_id", "item_id", "kind", "lot_no", name="uq_inv_lot"),
        {"schema": "inv"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    company_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("md.item.id"), index=True)
    kind: Mapped[str] = mapped_column(String(6))  # BATCH / SERIAL
    lot_no: Mapped[str] = mapped_column(String(60))  # "-" = stock with no number recorded
    expiry_date: Mapped[date | None] = mapped_column(Date, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LotBalance(Base):
    """Quantity of one lot at one location (base units), kept by stock posting."""

    __tablename__ = "lot_balance"
    __table_args__ = {"schema": "inv"}

    lot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("inv.lot.id"), primary_key=True)
    location_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.location.id"), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    qty: Mapped[Decimal] = mapped_column(Qty, server_default=text("0"))


class LotMove(Base):
    """Which lots each stock movement took or added - the trace of a batch or serial."""

    __tablename__ = "lot_move"
    __table_args__ = (
        Index("ix_inv_lot_move_doc", "doc_type", "doc_id"),
        Index("ix_inv_lot_move_lot", "lot_id", "id"),
        {"schema": "inv"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    lot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("inv.lot.id"))
    location_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.location.id"))
    ledger_id: Mapped[int] = mapped_column(ForeignKey("inv.stock_ledger.id"))
    doc_type: Mapped[str] = mapped_column(String(10))
    doc_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    doc_no: Mapped[str] = mapped_column(String(30))
    line_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    qty: Mapped[Decimal] = mapped_column(Qty)  # > 0 in, < 0 out
    is_reversal: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
