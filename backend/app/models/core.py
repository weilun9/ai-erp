import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    String,
    Table,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import AuditColumns, UUIDPk
from app.models.party import EinvoiceParty

SCHEMA = "core"

LOCATION_TYPES = ("STORE", "WAREHOUSE", "ECOM")
LANGUAGES = ("zh", "en", "ms")


class Company(UUIDPk, AuditColumns, EinvoiceParty, Base):
    """Our own company; its party fields are the e-invoice supplier block."""

    __tablename__ = "company"
    __table_args__ = {"schema": SCHEMA}

    code: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    country: Mapped[str] = mapped_column(String(2), server_default=text("'MY'"))
    business_activity: Mapped[str | None] = mapped_column(String(300))
    einvoice_enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    base_currency: Mapped[str] = mapped_column(String(3), server_default=text("'MYR'"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class Location(UUIDPk, AuditColumns, Base):
    __tablename__ = "location"
    __table_args__ = (
        UniqueConstraint("company_id", "code"),
        CheckConstraint(f"type IN {LOCATION_TYPES}", name="type"),
        {"schema": SCHEMA},
    )

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.company.id"), index=True)
    code: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(200))
    type: Mapped[str] = mapped_column(String(10), server_default=text("'STORE'"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class Permission(Base):
    __tablename__ = "permission"
    __table_args__ = {"schema": SCHEMA}

    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    description: Mapped[str] = mapped_column(String(200))


tag_permission = Table(
    "tag_permission",
    Base.metadata,
    Column("tag_id", ForeignKey("core.tag.id", ondelete="CASCADE"), primary_key=True),
    Column(
        "permission_code",
        ForeignKey("core.permission.code", ondelete="CASCADE"),
        primary_key=True,
    ),
    schema=SCHEMA,
)

user_tag = Table(
    "user_tag",
    Base.metadata,
    Column("user_id", ForeignKey("core.app_user.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("core.tag.id", ondelete="CASCADE"), primary_key=True),
    schema=SCHEMA,
)

user_location_scope = Table(
    "user_location_scope",
    Base.metadata,
    Column("user_id", ForeignKey("core.app_user.id", ondelete="CASCADE"), primary_key=True),
    Column("location_id", ForeignKey("core.location.id", ondelete="CASCADE"), primary_key=True),
    schema=SCHEMA,
)


class Tag(UUIDPk, AuditColumns, Base):
    """A label the owner puts on employees; each tag carries a set of permissions."""

    __tablename__ = "tag"
    __table_args__ = (UniqueConstraint("company_id", "code"), {"schema": SCHEMA})

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.company.id"), index=True)
    code: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(100))
    color: Mapped[str] = mapped_column(String(7), server_default=text("'#8c8c8c'"))
    # True: users with this tag see every location of the company.
    all_locations: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # True: created from a template in app.core.permissions; permissions re-synced by the seeder.
    is_system: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))

    permissions: Mapped[list[Permission]] = relationship(secondary=tag_permission)


class AppUser(UUIDPk, AuditColumns, Base):
    __tablename__ = "app_user"
    __table_args__ = (
        CheckConstraint(f"language IN {LANGUAGES}", name="language"),
        {"schema": SCHEMA},
    )

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.company.id"), index=True)
    username: Mapped[str] = mapped_column(String(50), unique=True)
    display_name: Mapped[str] = mapped_column(String(100))
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    language: Mapped[str] = mapped_column(String(2), server_default=text("'zh'"))
    default_location_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("core.location.id")
    )

    tags: Mapped[list[Tag]] = relationship(secondary=user_tag)
    locations: Mapped[list[Location]] = relationship(secondary=user_location_scope)


class Device(UUIDPk, AuditColumns, Base):
    """A browser / computer, identified by a long-lived HttpOnly cookie.

    Only devices the owner authorized may sign in; new ones wait as pending.
    """

    __tablename__ = "device"
    __table_args__ = {"schema": SCHEMA}

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.company.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_agent: Mapped[str | None] = mapped_column(String(300))
    last_ip: Mapped[str | None] = mapped_column(String(45))
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    is_authorized: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    authorized_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    authorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserSession(UUIDPk, Base):
    """One signed-in user on one device. Remembered sessions have no expiry."""

    __tablename__ = "user_session"
    __table_args__ = {"schema": SCHEMA}

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core.app_user.id", ondelete="CASCADE"), index=True
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core.device.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    remember: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # None = never expires (remembered device).
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
