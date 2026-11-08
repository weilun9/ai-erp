"""LHDN MyInvois e-invoice submissions (schema sal).

One row per submission attempt of a sales invoice (type 01) or customer credit note
(type 02). At most one *active* row (PENDING / SUBMITTED / VALID) per document, enforced
by a partial unique index, so a document can never be sent twice at the same time.
INVALID / FAILED / CANCELLED rows stay as history; the document may then be sent again.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import UUIDPk

EINVOICE_STATUSES = ("PENDING", "SUBMITTED", "VALID", "INVALID", "FAILED", "CANCELLED")
ACTIVE = ("PENDING", "SUBMITTED", "VALID")


class Einvoice(UUIDPk, Base):
    __tablename__ = "einvoice"
    __table_args__ = (
        CheckConstraint(f"status IN {EINVOICE_STATUSES}", name="status"),
        Index(
            "uq_sal_einvoice_active_doc",
            "doc_id",
            unique=True,
            postgresql_where=text("status IN ('PENDING', 'SUBMITTED', 'VALID')"),
        ),
        {"schema": "sal"},
    )
    company_id: Mapped[UUID] = mapped_column(ForeignKey("core.company.id"), index=True)
    doc_type: Mapped[str] = mapped_column(String(10))  # INV / CN
    doc_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    doc_no: Mapped[str] = mapped_column(String(30))
    type_code: Mapped[str] = mapped_column(String(2))  # 01 invoice, 02 credit note
    version: Mapped[str] = mapped_column(String(5))  # document type version, e.g. 1.0
    environment: Mapped[str] = mapped_column(String(10))  # sandbox / production
    status: Mapped[str] = mapped_column(String(10), server_default=text("'PENDING'"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)  # the UBL JSON as sent
    document_hash: Mapped[str] = mapped_column(String(64))
    submission_uid: Mapped[str | None] = mapped_column(String(40))
    uuid: Mapped[str | None] = mapped_column(String(40), index=True)  # LHDN document UUID
    long_id: Mapped[str | None] = mapped_column(String(100))
    errors: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    last_error: Mapped[str | None] = mapped_column(String(500))
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_by: Mapped[UUID | None] = mapped_column(Uuid)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(String(300))
