"""Money documents and knock-off (schema fin), plus problem documents (core.doc_issue).

Every party balance is made of debit documents (they raise what is owed: sales invoice,
GRN) and credit documents (they lower it: receipt, customer CN, supplier CN, fee,
supplier payment). A knock-off row ties an amount of one debit document to one credit
document; whatever is not knocked off stays open (an unpaid invoice, an advance).
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

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
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import AuditColumns, UUIDPk

Money = Numeric(18, 4)
STATUSES = ("DRAFT", "SAVED", "POSTED", "VOIDED")
METHODS = ("CASH", "CHEQUE", "TRANSFER", "CARD", "EWALLET", "OTHER")


class MoneyHeader(UUIDPk, AuditColumns):
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.company.id"), index=True)
    doc_no: Mapped[str | None] = mapped_column(String(30))
    doc_date: Mapped[date] = mapped_column(Date)
    location_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.location.id"), index=True)
    status: Mapped[str] = mapped_column(String(10), server_default=text("'DRAFT'"))
    method: Mapped[str] = mapped_column(String(10), server_default=text("'TRANSFER'"))
    bank_ref: Mapped[str | None] = mapped_column(String(50))  # cheque / transfer reference
    amount: Mapped[Decimal] = mapped_column(Money, server_default=text("0"))
    remark: Mapped[str | None] = mapped_column(String(500))
    currency: Mapped[str] = mapped_column(String(3), server_default=text("'MYR'"))
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    posted_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voided_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    void_reason: Mapped[str | None] = mapped_column(String(300))


def _header_args():
    return (
        UniqueConstraint("company_id", "doc_no"),
        CheckConstraint(f"status IN {STATUSES}", name="status"),
        CheckConstraint(f"method IN {METHODS}", name="method"),
        {"schema": "fin"},
    )


class CustomerReceipt(MoneyHeader, Base):
    """Money in from a customer. Lines say which invoices it pays; the rest is an advance."""

    __tablename__ = "receipt_h"
    __table_args__ = _header_args()
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("md.customer.id"), index=True)
    # On post, money not allocated by the lines pays the oldest open invoices first;
    # False keeps it as an advance (deposit) for later.
    auto_allocate: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    lines: Mapped[list["CustomerReceiptLine"]] = relationship(
        back_populates="header",
        cascade="all, delete-orphan",
        order_by="CustomerReceiptLine.line_no",
    )


class CustomerReceiptLine(UUIDPk, Base):
    __tablename__ = "receipt_l"
    __table_args__ = {"schema": "fin"}
    header_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("fin.receipt_h.id", ondelete="CASCADE"), index=True
    )
    line_no: Mapped[int] = mapped_column(Integer)
    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sal.invoice_h.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(Money)
    header: Mapped[CustomerReceipt] = relationship(back_populates="lines")


class SupplierPayment(MoneyHeader, Base):
    """Money out to a supplier, keyed in as the supplier's payment advice reads: the
    invoices paid, the CNs and fees deducted. `amount` is the net cash (negative when the
    supplier pays us back)."""

    __tablename__ = "supplier_payment_h"
    __table_args__ = _header_args()
    supplier_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("md.supplier.id"), index=True)
    supplier_ref_no: Mapped[str | None] = mapped_column(String(50))  # their advice number
    lines: Mapped[list["SupplierPaymentLine"]] = relationship(
        back_populates="header",
        cascade="all, delete-orphan",
        order_by="SupplierPaymentLine.line_no",
    )


LINE_TYPES = ("INVOICE", "CN", "FEE", "OTHER")


class SupplierPaymentLine(UUIDPk, Base):
    __tablename__ = "supplier_payment_l"
    __table_args__ = (
        CheckConstraint(f"line_type IN {LINE_TYPES}", name="line_type"),
        {"schema": "fin"},
    )
    header_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("fin.supplier_payment_h.id", ondelete="CASCADE"), index=True
    )
    line_no: Mapped[int] = mapped_column(Integer)
    line_type: Mapped[str] = mapped_column(String(10))
    ref_no: Mapped[str | None] = mapped_column(String(50))  # number as written by supplier
    # INVOICE: amount paid; CN / FEE: amount deducted; OTHER: + pay more / - deduct
    amount: Mapped[Decimal] = mapped_column(Money)
    fee_type: Mapped[str | None] = mapped_column(String(20))
    tax_code_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("md.tax_code.id"))
    # Matching result: MATCHED, AMOUNT_DIFF, NOT_FOUND, NOT_READY (None for fees / other)
    matched_type: Mapped[str | None] = mapped_column(String(10))
    matched_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    matched_no: Mapped[str | None] = mapped_column(String(30))
    match_status: Mapped[str | None] = mapped_column(String(12))
    remark: Mapped[str | None] = mapped_column(String(200))
    header: Mapped[SupplierPayment] = relationship(back_populates="lines")


class SupplierFee(UUIDPk, AuditColumns, Base):
    """Freight, listing, promotion... deducted on a supplier payment (no paper of its own)."""

    __tablename__ = "supplier_fee"
    __table_args__ = (UniqueConstraint("company_id", "doc_no"), {"schema": "fin"})
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.company.id"), index=True)
    doc_no: Mapped[str] = mapped_column(String(30))
    doc_date: Mapped[date] = mapped_column(Date)
    supplier_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("md.supplier.id"), index=True)
    location_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.location.id"))
    fee_type: Mapped[str] = mapped_column(String(20))
    amount: Mapped[Decimal] = mapped_column(Money)  # tax inclusive
    tax_code_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("md.tax_code.id"))
    tax_amount: Mapped[Decimal] = mapped_column(Money, server_default=text("0"))
    payment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("fin.supplier_payment_h.id"), index=True
    )
    payment_line_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("fin.supplier_payment_l.id"))
    status: Mapped[str] = mapped_column(String(10), server_default=text("'POSTED'"))
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Knockoff(UUIDPk, Base):
    """`amount` of debit document settled by credit document. Never deleted: undoing one
    (or voiding the document that created it) stamps voided_at."""

    __tablename__ = "knockoff"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("party_type IN ('C', 'S')", name="party_type"),
        {"schema": "fin"},
    )
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.company.id"), index=True)
    party_type: Mapped[str] = mapped_column(String(1))  # C customer, S supplier
    party_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    debit_type: Mapped[str] = mapped_column(String(10))
    debit_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    debit_no: Mapped[str] = mapped_column(String(30))
    credit_type: Mapped[str] = mapped_column(String(10))
    credit_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    credit_no: Mapped[str] = mapped_column(String(30))
    amount: Mapped[Decimal] = mapped_column(Money)
    # The document whose posting made this knock-off (None = done by hand)
    source_type: Mapped[str | None] = mapped_column(String(10))
    source_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voided_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)


ISSUE_STATUSES = ("OPEN", "RESOLVED", "CANCELLED")


class DocIssue(UUIDPk, Base):
    """Problem document (问题单): something that did not reconcile and needs a person."""

    __tablename__ = "doc_issue"
    __table_args__ = (
        CheckConstraint(f"status IN {ISSUE_STATUSES}", name="status"),
        {"schema": "core"},
    )
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.company.id"), index=True)
    issue_type: Mapped[str] = mapped_column(String(30))  # MISSING_CN, AMOUNT_MISMATCH ...
    status: Mapped[str] = mapped_column(String(10), server_default=text("'OPEN'"))
    title: Mapped[str] = mapped_column(String(200))
    doc_type: Mapped[str] = mapped_column(String(10))
    doc_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    doc_no: Mapped[str | None] = mapped_column(String(30))
    party_type: Mapped[str | None] = mapped_column(String(1))
    party_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    resolution: Mapped[str | None] = mapped_column(String(500))


class SupplierRefund(MoneyHeader, Base):
    """Money back from a supplier: settles what they owe us (CNs, credit adjustments,
    an overpayment / advance). Lines name the credits refunded; none = oldest first."""

    __tablename__ = "supplier_refund_h"
    __table_args__ = _header_args()
    supplier_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("md.supplier.id"), index=True)
    supplier_ref_no: Mapped[str | None] = mapped_column(String(50))  # their refund note no.
    lines: Mapped[list["SupplierRefundLine"]] = relationship(
        back_populates="header",
        cascade="all, delete-orphan",
        order_by="SupplierRefundLine.line_no",
    )


class SupplierRefundLine(UUIDPk, Base):
    __tablename__ = "supplier_refund_l"
    __table_args__ = {"schema": "fin"}
    header_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("fin.supplier_refund_h.id", ondelete="CASCADE"), index=True
    )
    line_no: Mapped[int] = mapped_column(Integer)
    doc_type: Mapped[str] = mapped_column(String(10))  # SCN / SCA / PV
    doc_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    doc_no: Mapped[str | None] = mapped_column(String(30))
    amount: Mapped[Decimal] = mapped_column(Money)
    header: Mapped[SupplierRefund] = relationship(back_populates="lines")
