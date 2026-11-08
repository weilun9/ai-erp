import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column


class UUIDPk:
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )


class AuditColumns:
    """created/updated stamps and an optimistic-lock version on every business table."""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
