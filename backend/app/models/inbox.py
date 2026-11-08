"""Scan intake (design doc section 5): every supplier paper goes to the inbox first.

pur.inbox_file        one uploaded file, kept for good as audit evidence
pur.inbox_extraction  each recognition attempt: result JSON, per-field confidence, cost
fin.bank_txn          bank transfer records read from statements / slips
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

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
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import UUIDPk

DOC_KINDS = ("INVOICE", "CN", "PAYMENT", "BANK", "RETURN_NOTE", "OTHER")
FILE_STATUSES = ("NEW", "EXTRACTED", "CONFIRMED", "REJECTED")


class InboxFile(UUIDPk, Base):
    __tablename__ = "inbox_file"
    __table_args__ = (
        UniqueConstraint("company_id", "sha256"),
        CheckConstraint(f"status IN {FILE_STATUSES}", name="status"),
        {"schema": "pur"},
    )
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.company.id"), index=True)
    location_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("core.location.id"))
    sha256: Mapped[str] = mapped_column(String(64))  # fingerprint: same file = same hash
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(50))
    size_bytes: Mapped[int] = mapped_column(Integer)
    storage_key: Mapped[str] = mapped_column(String(200))  # path under INBOX_DIR
    source: Mapped[str] = mapped_column(String(20), server_default=text("'UPLOAD'"))
    status: Mapped[str] = mapped_column(String(10), server_default=text("'NEW'"), index=True)
    doc_kind: Mapped[str | None] = mapped_column(String(12))  # guessed, then confirmed
    supplier_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("md.supplier.id"))
    doc_no: Mapped[str | None] = mapped_column(String(50))  # as read, for duplicate checks
    total: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    # Same supplier + number + amount as another file (a re-scan of the same paper)
    duplicate_of: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    linked_doc_type: Mapped[str | None] = mapped_column(String(10))
    linked_doc_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    note: Mapped[str | None] = mapped_column(String(300))
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)


class InboxExtraction(UUIDPk, Base):
    __tablename__ = "inbox_extraction"
    __table_args__ = {"schema": "pur"}
    file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pur.inbox_file.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(30))  # vision / manual / user
    model: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(10))  # DONE / FAILED
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    # path -> TRUSTED / CHECK / MISSING
    confidence: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    input_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    seconds: Mapped[Decimal | None] = mapped_column(Numeric(9, 2))
    error: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)


class BankTxn(UUIDPk, Base):
    __tablename__ = "bank_txn"
    __table_args__ = (
        CheckConstraint("direction IN ('IN', 'OUT')", name="direction"),
        {"schema": "fin"},
    )
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.company.id"), index=True)
    txn_date: Mapped[date] = mapped_column(Date)
    direction: Mapped[str] = mapped_column(String(3))
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    counterparty: Mapped[str | None] = mapped_column(String(200))
    reference: Mapped[str | None] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(String(300))
    source_file_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("pur.inbox_file.id"))
    matched_doc_type: Mapped[str | None] = mapped_column(String(10))
    matched_doc_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
