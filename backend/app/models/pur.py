"""Purchasing documents (schema pur). Every document is a header (_h) + lines (_l).

GRN doubles as the supplier invoice (it carries the supplier's invoice number), as in
DynaMod's purchase entry. Supplier CN returns goods; its completeness status supports the
"vague CN first, complete later" flow of design doc section 5.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
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
COMPLETENESS = ("RECORDED", "PROVISIONAL", "COMPLETE", "DISPUTED")


class PurchaseHeader(UUIDPk, AuditColumns):
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.company.id"), index=True)
    doc_no: Mapped[str | None] = mapped_column(String(30))  # assigned on save
    doc_date: Mapped[date] = mapped_column(Date)
    supplier_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("md.supplier.id"), index=True)
    location_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.location.id"), index=True)
    status: Mapped[str] = mapped_column(String(10), server_default=text("'DRAFT'"))
    supplier_ref_no: Mapped[str | None] = mapped_column(String(50))  # supplier's own number
    remark: Mapped[str | None] = mapped_column(String(500))
    currency: Mapped[str] = mapped_column(String(3), server_default=text("'MYR'"))
    subtotal: Mapped[Decimal] = mapped_column(Money, server_default=text("0"))
    tax_total: Mapped[Decimal] = mapped_column(Money, server_default=text("0"))
    total: Mapped[Decimal] = mapped_column(Money, server_default=text("0"))
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
            {"schema": "pur"},
        )


class PurchaseLine(UUIDPk):
    line_no: Mapped[int] = mapped_column(Integer)
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("md.item.id"))
    uom: Mapped[str] = mapped_column(String(10))
    qty: Mapped[Decimal] = mapped_column(Qty)
    factor: Mapped[Decimal] = mapped_column(Qty)  # snapshot of item_uom.factor
    base_qty: Mapped[Decimal] = mapped_column(Qty)  # qty * factor
    unit_price: Mapped[Decimal] = mapped_column(Money)  # per `uom`, before discount
    # Where unit_price came from: DOC (typed from the document), INVOICE (taken from the
    # original GRN/invoice for a return), MISSING (return with no price yet; cannot save).
    price_source: Mapped[str] = mapped_column(String(10), server_default=text("'DOC'"))
    discount_pct: Mapped[Decimal] = mapped_column(Numeric(7, 4), server_default=text("0"))
    tax_code_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("md.tax_code.id"))
    tax_rate: Mapped[Decimal] = mapped_column(Numeric(9, 4), server_default=text("0"))
    line_subtotal: Mapped[Decimal] = mapped_column(Money)
    line_tax: Mapped[Decimal] = mapped_column(Money)
    line_total: Mapped[Decimal] = mapped_column(Money)
    unit_cost: Mapped[Decimal] = mapped_column(Money)  # per base unit, net of discount, ex tax
    remark: Mapped[str | None] = mapped_column(String(200))


class Grn(PurchaseHeader, Base):
    __tablename__ = "grn_h"
    lines: Mapped[list["GrnLine"]] = relationship(
        back_populates="header", cascade="all, delete-orphan", order_by="GrnLine.line_no"
    )


class GrnLine(PurchaseLine, Base):
    __tablename__ = "grn_l"
    __table_args__ = {"schema": "pur"}
    header_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pur.grn_h.id", ondelete="CASCADE"), index=True
    )
    # The purchase order line this delivery fills, if it came from a PO.
    po_line_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("pur.po_l.id"), index=True)
    header: Mapped[Grn] = relationship(back_populates="lines")


class SupplierCn(PurchaseHeader, Base):
    __tablename__ = "supplier_cn_h"
    completeness: Mapped[str] = mapped_column(String(12), server_default=text("'COMPLETE'"))
    ref_invoice_no: Mapped[str | None] = mapped_column(String(50))
    lines: Mapped[list["SupplierCnLine"]] = relationship(
        back_populates="header", cascade="all, delete-orphan", order_by="SupplierCnLine.line_no"
    )

    @declared_attr.directive
    def __table_args__(cls):
        return (
            UniqueConstraint("company_id", "doc_no"),
            CheckConstraint(f"status IN {STATUSES}", name="status"),
            CheckConstraint(f"completeness IN {COMPLETENESS}", name="completeness"),
            {"schema": "pur"},
        )


class SupplierCnLine(PurchaseLine, Base):
    __tablename__ = "supplier_cn_l"
    __table_args__ = {"schema": "pur"}
    header_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pur.supplier_cn_h.id", ondelete="CASCADE"), index=True
    )
    header: Mapped[SupplierCn] = relationship(back_populates="lines")


class PurchaseOrder(PurchaseHeader, Base):
    """What we ordered from the supplier. Never moves stock; "posted" = issued to the
    supplier. GRNs receive against its lines (partial deliveries allowed) until it is
    fully received or closed by hand."""

    __tablename__ = "po_h"
    expected_date: Mapped[date | None] = mapped_column(Date)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    lines: Mapped[list["PurchaseOrderLine"]] = relationship(
        back_populates="header", cascade="all, delete-orphan", order_by="PurchaseOrderLine.line_no"
    )


class PurchaseOrderLine(PurchaseLine, Base):
    __tablename__ = "po_l"
    __table_args__ = {"schema": "pur"}
    header_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pur.po_h.id", ondelete="CASCADE"), index=True
    )
    header: Mapped[PurchaseOrder] = relationship(back_populates="lines")
