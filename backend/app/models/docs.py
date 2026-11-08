"""Document infrastructure shared by every module: numbering and posting status."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DocSequence(Base):
    """Next document number per company and document type (row-locked when used)."""

    __tablename__ = "doc_sequence"
    __table_args__ = {"schema": "core"}

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.company.id"), primary_key=True)
    doc_type: Mapped[str] = mapped_column(String(10), primary_key=True)
    prefix: Mapped[str] = mapped_column(String(10))
    width: Mapped[int] = mapped_column(Integer, server_default=text("6"))
    next_no: Mapped[int] = mapped_column(BigInteger, server_default=text("1"))


class PostingJob(Base):
    """One row per posting step of a document (STOCK, MATCHING, STATEMENT, TAX, EINVOICE...).

    Improves on DynaMod's IsPostStock / IsPostAccount Y/N columns: adding a step needs
    no schema change, and each step records its own attempts and last error.
    """

    __tablename__ = "posting_job"
    __table_args__ = (UniqueConstraint("doc_type", "doc_id", "step"), {"schema": "core"})

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    doc_type: Mapped[str] = mapped_column(String(10))
    doc_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    step: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(10))  # PENDING / DONE / FAILED
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
