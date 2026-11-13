"""Sales documents (schema sal): quotation, delivery order, sales invoice, customer CN.

Same header (_h) + lines (_l) shape as purchasing. Lines keep `unit_cost` (per base unit,
filled in when the document is posted, from the stock ledger) so gross profit is known.
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
from app.models.pur import NoteLine

Money = Numeric(18, 4)
Qty = Numeric(18, 4)
STATUSES = ("DRAFT", "SAVED", "POSTED", "VOIDED")


class SalesHeader(UUIDPk, AuditColumns):
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.company.id"), index=True)
    doc_no: Mapped[str | None] = mapped_column(String(30))  # assigned on save
    doc_date: Mapped[date] = mapped_column(Date)
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("md.customer.id"), index=True)
    location_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.location.id"), index=True)
    status: Mapped[str] = mapped_column(String(10), server_default=text("'DRAFT'"))
    customer_ref_no: Mapped[str | None] = mapped_column(String(50))  # customer's PO number
    remark: Mapped[str | None] = mapped_column(String(500))
    currency: Mapped[str] = mapped_column(String(3), server_default=text("'MYR'"))
    subtotal: Mapped[Decimal] = mapped_column(Money, server_default=text("0"))
    tax_total: Mapped[Decimal] = mapped_column(Money, server_default=text("0"))
    total: Mapped[Decimal] = mapped_column(Money, server_default=text("0"))
    cost_total: Mapped[Decimal] = mapped_column(Money, server_default=text("0"))  # on post
    # The document this one was converted from (quotation -> DO -> invoice -> CN).
    source_kind: Mapped[str | None] = mapped_column(String(20))
    source_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
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
            {"schema": "sal"},
        )


class SalesLine(UUIDPk):
    line_no: Mapped[int] = mapped_column(Integer)
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("md.item.id"))
    uom: Mapped[str] = mapped_column(String(10))
    qty: Mapped[Decimal] = mapped_column(Qty)
    factor: Mapped[Decimal] = mapped_column(Qty)
    base_qty: Mapped[Decimal] = mapped_column(Qty)
    unit_price: Mapped[Decimal] = mapped_column(Money)  # selling price per `uom`
    # DOC (typed), LIST (item's selling price), INVOICE (return priced from the invoice),
    # MISSING (no price; cannot save).
    price_source: Mapped[str] = mapped_column(String(10), server_default=text("'DOC'"))
    discount_pct: Mapped[Decimal] = mapped_column(Numeric(7, 4), server_default=text("0"))
    tax_code_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("md.tax_code.id"))
    tax_rate: Mapped[Decimal] = mapped_column(Numeric(9, 4), server_default=text("0"))
    line_subtotal: Mapped[Decimal] = mapped_column(Money)
    line_tax: Mapped[Decimal] = mapped_column(Money)
    line_total: Mapped[Decimal] = mapped_column(Money)
    unit_cost: Mapped[Decimal] = mapped_column(Money, server_default=text("0"))  # on post
    remark: Mapped[str | None] = mapped_column(String(200))


class Quotation(SalesHeader, Base):
    __tablename__ = "quotation_h"
    valid_until: Mapped[date | None] = mapped_column(Date)
    lines: Mapped[list["QuotationLine"]] = relationship(
        back_populates="header", cascade="all, delete-orphan", order_by="QuotationLine.line_no"
    )


class QuotationLine(SalesLine, Base):
    __tablename__ = "quotation_l"
    __table_args__ = {"schema": "sal"}
    header_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sal.quotation_h.id", ondelete="CASCADE"), index=True
    )
    header: Mapped[Quotation] = relationship(back_populates="lines")


class DeliveryOrder(SalesHeader, Base):
    __tablename__ = "do_h"
    lines: Mapped[list["DeliveryOrderLine"]] = relationship(
        back_populates="header", cascade="all, delete-orphan", order_by="DeliveryOrderLine.line_no"
    )


class DeliveryOrderLine(SalesLine, Base):
    __tablename__ = "do_l"
    __table_args__ = {"schema": "sal"}
    header_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sal.do_h.id", ondelete="CASCADE"), index=True
    )
    # Delivered against this sales order line.
    so_line_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sal.so_l.id"), index=True)
    header: Mapped[DeliveryOrder] = relationship(back_populates="lines")


class SalesInvoice(SalesHeader, Base):
    __tablename__ = "invoice_h"
    due_date: Mapped[date | None] = mapped_column(Date)  # doc_date + customer term, on save
    lines: Mapped[list["SalesInvoiceLine"]] = relationship(
        back_populates="header", cascade="all, delete-orphan", order_by="SalesInvoiceLine.line_no"
    )


class SalesInvoiceLine(SalesLine, Base):
    __tablename__ = "invoice_l"
    __table_args__ = {"schema": "sal"}
    header_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sal.invoice_h.id", ondelete="CASCADE"), index=True
    )
    # Invoiced from this DO line: the goods already left with the DO, so no stock moves.
    do_line_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sal.do_l.id"), index=True)
    # Invoiced straight from this sales order line (no DO in between).
    so_line_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sal.so_l.id"), index=True)
    header: Mapped[SalesInvoice] = relationship(back_populates="lines")


class CustomerCn(SalesHeader, Base):
    __tablename__ = "customer_cn_h"
    ref_invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sal.invoice_h.id"), index=True
    )
    reason: Mapped[str | None] = mapped_column(String(200))
    lines: Mapped[list["CustomerCnLine"]] = relationship(
        back_populates="header", cascade="all, delete-orphan", order_by="CustomerCnLine.line_no"
    )


class CustomerCnLine(SalesLine, Base):
    __tablename__ = "customer_cn_l"
    __table_args__ = {"schema": "sal"}
    header_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sal.customer_cn_h.id", ondelete="CASCADE"), index=True
    )
    header: Mapped[CustomerCn] = relationship(back_populates="lines")


class SalesOrder(SalesHeader, Base):
    """What the customer ordered. Never moves stock; "posted" = confirmed. DOs or invoices
    fulfil its lines (in parts) until it is fully delivered or closed by hand."""

    __tablename__ = "so_h"
    delivery_date: Mapped[date | None] = mapped_column(Date)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    lines: Mapped[list["SalesOrderLine"]] = relationship(
        back_populates="header", cascade="all, delete-orphan", order_by="SalesOrderLine.line_no"
    )


class SalesOrderLine(SalesLine, Base):
    __tablename__ = "so_l"
    __table_args__ = {"schema": "sal"}
    header_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sal.so_h.id", ondelete="CASCADE"), index=True
    )
    header: Mapped[SalesOrder] = relationship(back_populates="lines")


class CustomerDebitNote(SalesHeader, Base):
    """We charge the customer more (under-billed, delivery charge...): raises what they owe,
    moves no stock. Paid like an invoice."""

    __tablename__ = "customer_dn_h"
    due_date: Mapped[date | None] = mapped_column(Date)
    lines: Mapped[list["CustomerDebitNoteLine"]] = relationship(
        back_populates="header",
        cascade="all, delete-orphan",
        order_by="CustomerDebitNoteLine.line_no",
    )


class CustomerDebitNoteLine(NoteLine, Base):
    __tablename__ = "customer_dn_l"
    __table_args__ = {"schema": "sal"}
    header_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sal.customer_dn_h.id", ondelete="CASCADE"), index=True
    )
    header: Mapped[CustomerDebitNote] = relationship(back_populates="lines")
