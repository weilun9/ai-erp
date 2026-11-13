"""Accounting (schema acc): chart of accounts and the general ledger (journals).

Journals are made from posted documents by app.services.ledger (one per document, plus a
reversing one when the document is voided) and by hand (manual journal vouchers, JV).
They are never edited: a mistake is voided, which adds a reversing journal.
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
from app.models.mixins import AuditColumns, UUIDPk

SCHEMA = "acc"
Money = Numeric(18, 2)
ACCOUNT_TYPES = ("ASSET", "LIABILITY", "EQUITY", "INCOME", "EXPENSE")


class Account(UUIDPk, AuditColumns, Base):
    __tablename__ = "account"
    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_account_code"),
        UniqueConstraint("company_id", "system_key", name="uq_account_system_key"),
        CheckConstraint(f"type IN {ACCOUNT_TYPES}", name="type"),
        {"schema": SCHEMA},
    )

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.company.id"), index=True)
    code: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(100))
    type: Mapped[str] = mapped_column(String(10))
    # The accounts documents post to (AR, AP, INVENTORY, SALES...); None = user's own.
    system_key: Mapped[str | None] = mapped_column(String(20))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class Journal(UUIDPk, Base):
    __tablename__ = "journal"
    __table_args__ = (
        UniqueConstraint("company_id", "doc_no"),
        # one journal per document, and at most one reversal of it
        Index(
            "uq_acc_journal_source",
            "company_id",
            "source_type",
            "source_id",
            "is_reversal",
            unique=True,
            postgresql_where=text("source_id IS NOT NULL"),
        ),
        {"schema": SCHEMA},
    )

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.company.id"), index=True)
    doc_no: Mapped[str] = mapped_column(String(30))
    journal_date: Mapped[date] = mapped_column(Date, index=True)
    source_type: Mapped[str] = mapped_column(String(10))  # INV, GRN, PV... or JV (by hand)
    source_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    source_no: Mapped[str | None] = mapped_column(String(30))
    location_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("core.location.id"))
    memo: Mapped[str | None] = mapped_column(String(300))
    is_reversal: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    reverses_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    status: Mapped[str] = mapped_column(String(10), server_default=text("'POSTED'"))
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    void_reason: Mapped[str | None] = mapped_column(String(300))
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    lines: Mapped[list["JournalLine"]] = relationship(
        back_populates="journal", cascade="all, delete-orphan", order_by="JournalLine.line_no"
    )


class JournalLine(UUIDPk, Base):
    __tablename__ = "journal_line"
    __table_args__ = (
        CheckConstraint("debit >= 0 AND credit >= 0", name="amounts"),
        {"schema": SCHEMA},
    )

    journal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("acc.journal.id", ondelete="CASCADE"), index=True
    )
    line_no: Mapped[int] = mapped_column(Integer)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("acc.account.id"), index=True)
    debit: Mapped[Decimal] = mapped_column(Money, server_default=text("0"))
    credit: Mapped[Decimal] = mapped_column(Money, server_default=text("0"))
    party_type: Mapped[str | None] = mapped_column(String(1))  # C / S on AR / AP lines
    party_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    memo: Mapped[str | None] = mapped_column(String(200))
    journal: Mapped[Journal] = relationship(back_populates="lines")
