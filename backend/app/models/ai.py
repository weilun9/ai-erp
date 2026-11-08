"""AI butler (design doc section 8).

ai.conversation    one chat thread, owned by one user
ai.message         every turn: user text, model replies, tool calls and tool results
ai.pending_action  a write the AI proposed; nothing happens until the user confirms it
ai.usage           tokens and time for every model call (budgeting)
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import UUIDPk

ROLES = ("user", "assistant", "tool")
ACTION_STATUSES = ("PENDING", "CONFIRMED", "CANCELLED", "EXPIRED", "FAILED")


class Conversation(UUIDPk, Base):
    __tablename__ = "conversation"
    __table_args__ = {"schema": "ai"}
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.company.id"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.app_user.id"), index=True)
    title: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Message(Base):
    __tablename__ = "message"
    __table_args__ = (CheckConstraint(f"role IN {ROLES}", name="role"), {"schema": "ai"})
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ai.conversation.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(10))
    content: Mapped[str | None] = mapped_column(Text)
    # assistant: [{"id", "name", "arguments"}]; tool: the call it answers
    tool_calls: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    tool_call_id: Mapped[str | None] = mapped_column(String(100))
    tool_name: Mapped[str | None] = mapped_column(String(60))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PendingAction(UUIDPk, Base):
    __tablename__ = "pending_action"
    __table_args__ = (
        CheckConstraint(f"status IN {ACTION_STATUSES}", name="status"),
        {"schema": "ai"},
    )
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.company.id"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.app_user.id"), index=True)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ai.conversation.id", ondelete="SET NULL"), index=True
    )
    tool: Mapped[str] = mapped_column(String(60))
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB)  # what the confirm card shows
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)  # exactly what runs on confirm
    status: Mapped[str] = mapped_column(String(10), server_default=text("'PENDING'"), index=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(String(500))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Usage(Base):
    __tablename__ = "usage"
    __table_args__ = {"schema": "ai"}
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.company.id"), index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    purpose: Mapped[str] = mapped_column(String(20))  # chat / vision
    model: Mapped[str | None] = mapped_column(String(100))
    input_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    seconds: Mapped[float] = mapped_column(Numeric(8, 2), server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
