import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    String,
    Table,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import AuditColumns, UUIDPk

SCHEMA = "core"

LOCATION_TYPES = ("STORE", "WAREHOUSE", "ECOM")
LANGUAGES = ("zh", "en", "ms")


class Company(UUIDPk, AuditColumns, Base):
    __tablename__ = "company"
    __table_args__ = {"schema": SCHEMA}

    code: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    tax_no: Mapped[str | None] = mapped_column(String(50))
    sst_no: Mapped[str | None] = mapped_column(String(50))
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


role_permission = Table(
    "role_permission",
    Base.metadata,
    Column("role_id", ForeignKey("core.role.id", ondelete="CASCADE"), primary_key=True),
    Column(
        "permission_code",
        ForeignKey("core.permission.code", ondelete="CASCADE"),
        primary_key=True,
    ),
    schema=SCHEMA,
)

user_role = Table(
    "user_role",
    Base.metadata,
    Column("user_id", ForeignKey("core.app_user.id", ondelete="CASCADE"), primary_key=True),
    Column("role_id", ForeignKey("core.role.id", ondelete="CASCADE"), primary_key=True),
    schema=SCHEMA,
)

user_location_scope = Table(
    "user_location_scope",
    Base.metadata,
    Column("user_id", ForeignKey("core.app_user.id", ondelete="CASCADE"), primary_key=True),
    Column("location_id", ForeignKey("core.location.id", ondelete="CASCADE"), primary_key=True),
    schema=SCHEMA,
)


class Role(UUIDPk, AuditColumns, Base):
    __tablename__ = "role"
    __table_args__ = (UniqueConstraint("company_id", "code"), {"schema": SCHEMA})

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.company.id"), index=True)
    code: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(100))
    # True: users with this role see every location of the company.
    all_locations: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # True: created from a template in app.core.permissions and re-synced by the seeder.
    is_system: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))

    permissions: Mapped[list[Permission]] = relationship(secondary=role_permission)


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

    roles: Mapped[list[Role]] = relationship(secondary=user_role)
    locations: Mapped[list[Location]] = relationship(secondary=user_location_scope)
