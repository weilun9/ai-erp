"""Master data (schema md): tax codes, UOMs, categories, items, suppliers, customers."""

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import AuditColumns, UUIDPk
from app.models.party import EinvoiceParty

SCHEMA = "md"
Money = Numeric(18, 4)
Qty = Numeric(18, 4)


class TaxCode(UUIDPk, AuditColumns, Base):
    __tablename__ = "tax_code"
    __table_args__ = (UniqueConstraint("company_id", "code"), {"schema": SCHEMA})

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.company.id"), index=True)
    code: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(100))
    rate: Mapped[Decimal] = mapped_column(Numeric(9, 4))
    einvoice_tax_type: Mapped[str] = mapped_column(String(2))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class Uom(UUIDPk, AuditColumns, Base):
    __tablename__ = "uom"
    __table_args__ = (UniqueConstraint("company_id", "code"), {"schema": SCHEMA})

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.company.id"), index=True)
    code: Mapped[str] = mapped_column(String(10))
    name: Mapped[str] = mapped_column(String(50))
    einvoice_code: Mapped[str] = mapped_column(String(3))


class ItemCategory(UUIDPk, AuditColumns, Base):
    __tablename__ = "item_category"
    __table_args__ = (UniqueConstraint("company_id", "code"), {"schema": SCHEMA})

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.company.id"), index=True)
    code: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(100))


class Item(UUIDPk, AuditColumns, Base):
    __tablename__ = "item"
    __table_args__ = (UniqueConstraint("company_id", "code"), {"schema": SCHEMA})

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.company.id"), index=True)
    code: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(200))
    short_name: Mapped[str | None] = mapped_column(String(60))  # receipt / shelf label
    category_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("md.item_category.id"))
    base_uom: Mapped[str] = mapped_column(String(10))
    purchase_tax_code_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("md.tax_code.id"))
    sales_tax_code_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("md.tax_code.id"))
    einvoice_classification: Mapped[str] = mapped_column(String(3), server_default=text("'022'"))
    track_batch: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))

    uoms: Mapped[list["ItemUom"]] = relationship(
        back_populates="item", cascade="all, delete-orphan", order_by="ItemUom.factor"
    )
    barcodes: Mapped[list["ItemBarcode"]] = relationship(
        back_populates="item", cascade="all, delete-orphan", order_by="ItemBarcode.barcode"
    )


class ItemUom(UUIDPk, Base):
    """Packing units of an item. factor = how many base units (1 CTN = 24 PCS)."""

    __tablename__ = "item_uom"
    __table_args__ = (UniqueConstraint("item_id", "uom"), {"schema": SCHEMA})

    item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("md.item.id", ondelete="CASCADE"), index=True
    )
    uom: Mapped[str] = mapped_column(String(10))
    factor: Mapped[Decimal] = mapped_column(Qty)
    selling_price: Mapped[Decimal | None] = mapped_column(Money)
    purchase_price: Mapped[Decimal | None] = mapped_column(Money)  # cost: field.cost.read

    item: Mapped[Item] = relationship(back_populates="uoms")


class ItemBarcode(UUIDPk, Base):
    __tablename__ = "item_barcode"
    __table_args__ = (
        UniqueConstraint("company_id", "barcode"),
        Index("ix_md_item_barcode_keys", "keys", postgresql_using="gin"),
        {"schema": SCHEMA},
    )

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.company.id"))
    item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("md.item.id", ondelete="CASCADE"), index=True
    )
    barcode: Mapped[str] = mapped_column(String(30))
    uom: Mapped[str] = mapped_column(String(10))
    # Match keys from app.core.barcode.keys_for (11/12/13-digit tolerant).
    keys: Mapped[list[str]] = mapped_column(ARRAY(String(40)))

    item: Mapped[Item] = relationship(back_populates="barcodes")


class Supplier(UUIDPk, AuditColumns, EinvoiceParty, Base):
    __tablename__ = "supplier"
    __table_args__ = (UniqueConstraint("company_id", "code"), {"schema": SCHEMA})

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.company.id"), index=True)
    code: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(200))
    contact_person: Mapped[str | None] = mapped_column(String(100))
    term_days: Mapped[int] = mapped_column(Integer, server_default=text("30"))
    currency: Mapped[str] = mapped_column(String(3), server_default=text("'MYR'"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    # Supplier habits the AI and checks rely on (doc numbers, unit conversions, settlement).
    profile: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))


class Customer(UUIDPk, AuditColumns, EinvoiceParty, Base):
    __tablename__ = "customer"
    __table_args__ = (UniqueConstraint("company_id", "code"), {"schema": SCHEMA})

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.company.id"), index=True)
    code: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(200))
    contact_person: Mapped[str | None] = mapped_column(String(100))
    term_days: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    credit_limit: Mapped[Decimal | None] = mapped_column(Money)  # finance
    currency: Mapped[str] = mapped_column(String(3), server_default=text("'MYR'"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
