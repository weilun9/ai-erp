import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Identity, String, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class EventLog(Base):
    """Append-only audit trail of who changed what (people, AI or MCP)."""

    __tablename__ = "event_log"
    __table_args__ = {"schema": "audit"}

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    company_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    actor_type: Mapped[str] = mapped_column(String(10))  # user / ai / mcp / system
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    device_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    action: Mapped[str] = mapped_column(String(40))  # create / update / assign_tags ...
    entity: Mapped[str] = mapped_column(String(40))  # md.item, core.device ...
    entity_id: Mapped[str | None] = mapped_column(String(64), index=True)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    request_id: Mapped[str | None] = mapped_column(String(64))
