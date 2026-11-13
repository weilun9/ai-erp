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


ATTRIBUTE_KINDS = ("BRAND", "GROUP", "COLOR", "SIZE", "UDF1", "UDF2", "UDF3")


class ItemAttribute(UUIDPk, AuditColumns, Base):
    """Lookup lists for item grouping (brand, group, colour, size, three user-defined)."""

    __tablename__ = "item_attribute"
    __table_args__ = (UniqueConstraint("company_id", "kind", "code"), {"schema": SCHEMA})

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.company.id"), index=True)
    kind: Mapped[str] = mapped_column(String(10))
    code: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


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
    description_2: Mapped[str | None] = mapped_column(String(200))
    article_no: Mapped[str | None] = mapped_column(String(50))
    # Grouping: each points at an md.item_attribute row of the matching kind.
    brand_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("md.item_attribute.id"))
    group_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("md.item_attribute.id"))
    color_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("md.item_attribute.id"))
    size_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("md.item_attribute.id"))
    udf1_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("md.item_attribute.id"))
    udf2_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("md.item_attribute.id"))
    udf3_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("md.item_attribute.id"))
    sales_note: Mapped[str | None] = mapped_column(String(2000))
    purchase_note: Mapped[str | None] = mapped_column(String(2000))
    # Company-wide reorder defaults in base units; md.item_location overrides per shop.
    min_qty: Mapped[Decimal | None] = mapped_column(Qty)
    max_qty: Mapped[Decimal | None] = mapped_column(Qty)
    reorder_level: Mapped[Decimal | None] = mapped_column(Qty)
    reorder_qty: Mapped[Decimal | None] = mapped_column(Qty)
    # Alerts: selling below this price per base unit warns (BELOW_MIN_PRICE).
    min_selling_price: Mapped[Decimal | None] = mapped_column(Money)

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
    selling_price: Mapped[Decimal | None] = mapped_column(Money)  # price level 1
    price_2: Mapped[Decimal | None] = mapped_column(Money)
    price_3: Mapped[Decimal | None] = mapped_column(Money)
    price_4: Mapped[Decimal | None] = mapped_column(Money)
    price_5: Mapped[Decimal | None] = mapped_column(Money)
    purchase_price: Mapped[Decimal | None] = mapped_column(Money)  # cost: field.cost.read

    item: Mapped[Item] = relationship(back_populates="uoms")


class ItemLocation(Base):
    """Per-shop stock settings: reorder levels that override the item's, and the shelf."""

    __tablename__ = "item_location"
    __table_args__ = {"schema": SCHEMA}

    item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("md.item.id", ondelete="CASCADE"), primary_key=True
    )
    location_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.location.id"), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.company.id"), index=True)
    min_qty: Mapped[Decimal | None] = mapped_column(Qty)
    max_qty: Mapped[Decimal | None] = mapped_column(Qty)
    reorder_level: Mapped[Decimal | None] = mapped_column(Qty)
    reorder_qty: Mapped[Decimal | None] = mapped_column(Qty)
    shelf: Mapped[str | None] = mapped_column(String(30))


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
    price_level: Mapped[int] = mapped_column(Integer, server_default=text("1"))  # 1..5
    currency: Mapped[str] = mapped_column(String(3), server_default=text("'MYR'"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
