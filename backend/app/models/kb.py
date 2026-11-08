"""Knowledge base for the AI butler (design doc section 9): text knowledge only - rules,
SOPs, supplier terms, manuals. Stock and documents are never put here; tools read those.

ai.kb_document  one version of one document, with who may see it
ai.kb_chunk     searchable pieces: text, full-text vector, embedding (+ which model made it)

Visibility is copied onto every chunk so the search can filter in the same SQL statement:
visible_tags NULL = everyone, else the user needs one of those tags; visible_location_ids
NULL = every store, else the user must work at one of them.
"""

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import UUIDPk

KB_STATUSES = ("READY", "FAILED", "RETIRED")


class KbDocument(UUIDPk, Base):
    __tablename__ = "kb_document"
    __table_args__ = (
        CheckConstraint(f"status IN {KB_STATUSES}", name="status"),
        Index(
            "uq_ai_kb_document_live_key",
            "company_id",
            "doc_key",
            unique=True,
            postgresql_where=text("status = 'READY'"),
        ),
        {"schema": "ai"},
    )
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.company.id"), index=True)
    doc_key: Mapped[str] = mapped_column(String(80))  # same key = new version of it
    version: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(200))
    category: Mapped[str | None] = mapped_column(String(40))  # SOP, SUPPLIER, TAX, MANUAL...
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(80))
    sha256: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(200))
    visible_tags: Mapped[list[str] | None] = mapped_column(ARRAY(String(30)))
    visible_location_ids: Mapped[list[uuid.UUID] | None] = mapped_column(ARRAY(Uuid))
    status: Mapped[str] = mapped_column(String(10), server_default=text("'READY'"))
    chunks: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    embed_model: Mapped[str | None] = mapped_column(String(120))
    error: Mapped[str | None] = mapped_column(String(500))
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class KbChunk(Base):
    __tablename__ = "kb_chunk"
    __table_args__ = (
        Index("ix_ai_kb_chunk_tsv", "tsv", postgresql_using="gin"),
        {"schema": "ai"},
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.company.id"), index=True)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ai.kb_document.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer)
    section: Mapped[str | None] = mapped_column(String(300))  # heading path
    page: Mapped[int | None] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    tsv: Mapped[str] = mapped_column(TSVECTOR)
    # No fixed size: the model is configurable; rows only compare with the same model.
    embedding: Mapped[list[float] | None] = mapped_column(Vector())
    embed_model: Mapped[str | None] = mapped_column(String(120))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    visible_tags: Mapped[list[str] | None] = mapped_column(ARRAY(String(30)))
    visible_location_ids: Mapped[list[uuid.UUID] | None] = mapped_column(ARRAY(Uuid))
