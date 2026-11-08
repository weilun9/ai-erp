"""Master data services: tax codes, UOMs, categories, items (UOMs + barcodes), parties.

Rules enforced here (not in the API), so the AI butler and MCP get the same behaviour:
- permission + same-company checks on every call
- optimistic locking: updates carry the version the caller loaded
- barcodes are unique by 11/12/13-digit match key, not just by exact text
- you cannot write a sensitive field you are not allowed to read (cost / pii / finance)
- every change is written to audit.event_log in the same transaction
"""

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.core import barcode as bc
from app.core.errors import BadRequest, BusinessRule, Conflict, Forbidden, NotFound, VersionConflict
from app.localization import pack
from app.models import (
    Company,
    Customer,
    Item,
    ItemBarcode,
    ItemCategory,
    ItemUom,
    Supplier,
    TaxCode,
    Uom,
)
from app.services import audit
from app.services.auth import Principal

FIELD_PERMS = {"cost": "field.cost.read", "pii": "field.pii.read", "finance": "field.finance.read"}

CUSTOMER_PII = (
    "phone",
    "email",
    "id_no",
    "address1",
    "address2",
    "address3",
    "postcode",
    "contact_person",
)
CUSTOMER_FINANCE = ("credit_limit",)


# --- helpers ------------------------------------------------------------------------------


def _country_pack(db: Session, p: Principal):
    return pack(db.get(Company, p.company_id).country)


def _own(db: Session, p: Principal, model, obj_id: uuid.UUID, label: str):
    obj = db.get(model, obj_id)
    if obj is None or obj.company_id != p.company_id:
        raise NotFound(f"{label} not found")
    return obj


def _check_version(obj, version: int) -> None:
    if obj.version != version:
        raise VersionConflict(
            "This record was changed by someone else; reload it",
            {"expected": version, "current": obj.version},
        )


def _assign(obj, changes: dict[str, Any]) -> None:
    """Apply only the fields the caller sent; null clears a field unless it is required."""
    columns = obj.__table__.columns
    for k, val in changes.items():
        if val is None and k in columns and not columns[k].nullable:
            raise BadRequest("Field cannot be empty", {"field": k})
        setattr(obj, k, val)


def _touch(obj, p: Principal) -> None:
    obj.version += 1
    obj.updated_by = p.user_id


def _new(model, p: Principal, **values):
    return model(company_id=p.company_id, created_by=p.user_id, updated_by=p.user_id, **values)


def _unique_code(db: Session, p: Principal, model, code: str, exclude: uuid.UUID | None = None):
    stmt = select(model.id).where(model.company_id == p.company_id, model.code == code)
    if exclude:
        stmt = stmt.where(model.id != exclude)
    if db.scalar(stmt):
        raise Conflict("Code already exists", {"code": code})


def _page(db: Session, stmt, page: int, page_size: int):
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    rows = db.scalars(stmt.offset((page - 1) * page_size).limit(page_size)).unique().all()
    return list(rows), total


# --- tax codes / UOMs / categories --------------------------------------------------------


def list_tax_codes(db: Session, p: Principal) -> list[TaxCode]:
    p.require("md.setup.read")
    return list(
        db.scalars(select(TaxCode).where(TaxCode.company_id == p.company_id).order_by(TaxCode.code))
    )


def _validate_tax(db: Session, p: Principal, rate: Decimal, tax_type: str) -> None:
    cp = _country_pack(db, p)
    if tax_type not in cp.TAX_TYPES:
        raise BadRequest("Unknown e-invoice tax type", {"allowed": sorted(cp.TAX_TYPES)})
    if not Decimal(0) <= rate <= Decimal(100):
        raise BadRequest("Rate must be between 0 and 100")


def create_tax_code(db: Session, p: Principal, **v: Any) -> TaxCode:
    p.require("md.setup.manage")
    _unique_code(db, p, TaxCode, v["code"])
    _validate_tax(db, p, v["rate"], v["einvoice_tax_type"])
    tc = _new(TaxCode, p, **v)
    db.add(tc)
    db.flush()
    audit.record(db, p, "create", "md.tax_code", tc.id, after=audit.snapshot(tc))
    db.commit()
    db.refresh(tc)
    return tc


def update_tax_code(db: Session, p: Principal, tax_id: uuid.UUID, version: int, **v: Any):
    p.require("md.setup.manage")
    tc = _own(db, p, TaxCode, tax_id, "Tax code")
    _check_version(tc, version)
    before = audit.snapshot(tc)
    _assign(tc, v)
    _validate_tax(db, p, tc.rate, tc.einvoice_tax_type)
    _touch(tc, p)
    db.flush()
    audit.record(db, p, "update", "md.tax_code", tc.id, before=before, after=audit.snapshot(tc))
    db.commit()
    db.refresh(tc)
    return tc


def list_uoms(db: Session, p: Principal) -> list[Uom]:
    p.require("md.setup.read")
    return list(db.scalars(select(Uom).where(Uom.company_id == p.company_id).order_by(Uom.code)))


def create_uom(db: Session, p: Principal, *, code: str, name: str, einvoice_code: str) -> Uom:
    p.require("md.setup.manage")
    _unique_code(db, p, Uom, code)
    u = _new(Uom, p, code=code, name=name, einvoice_code=einvoice_code)
    db.add(u)
    db.flush()
    audit.record(db, p, "create", "md.uom", u.id, after=audit.snapshot(u))
    db.commit()
    return u


def list_categories(db: Session, p: Principal) -> list[ItemCategory]:
    p.require("md.item.read")
    return list(
        db.scalars(
            select(ItemCategory)
            .where(ItemCategory.company_id == p.company_id)
            .order_by(ItemCategory.code)
        )
    )


def create_category(db: Session, p: Principal, *, code: str, name: str) -> ItemCategory:
    p.require("md.setup.manage")
    _unique_code(db, p, ItemCategory, code)
    c = _new(ItemCategory, p, code=code, name=name)
    db.add(c)
    db.flush()
    audit.record(db, p, "create", "md.item_category", c.id, after=audit.snapshot(c))
    db.commit()
    return c


# --- items --------------------------------------------------------------------------------


def _item_query(p: Principal):
    return (
        select(Item)
        .where(Item.company_id == p.company_id)
        .options(selectinload(Item.uoms), selectinload(Item.barcodes))
    )


def search_items(
    db: Session,
    p: Principal,
    *,
    q: str | None = None,
    category_id: uuid.UUID | None = None,
    active: bool | None = True,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[Item], int]:
    p.require("md.item.read")
    stmt = _item_query(p)
    if q:
        like = f"%{q.strip()}%"
        by_barcode = select(ItemBarcode.item_id).where(
            ItemBarcode.company_id == p.company_id, ItemBarcode.keys.overlap(bc.keys_for(q))
        )
        stmt = stmt.where(
            or_(
                Item.code.ilike(like),
                Item.name.ilike(like),
                Item.short_name.ilike(like),
                Item.id.in_(by_barcode),
            )
        )
    if category_id:
        stmt = stmt.where(Item.category_id == category_id)
    if active is not None:
        stmt = stmt.where(Item.is_active.is_(active))
    return _page(db, stmt.order_by(Item.code), page, page_size)


def get_item(db: Session, p: Principal, item_id: uuid.UUID) -> Item:
    p.require("md.item.read")
    item = db.scalar(_item_query(p).where(Item.id == item_id))
    if item is None:
        raise NotFound("Item not found")
    return item


def lookup_barcode(db: Session, p: Principal, code: str) -> list[tuple[Item, ItemBarcode]]:
    """Find items by barcode, tolerating 11/12/13-digit variants. >1 result = ambiguous."""
    p.require("md.item.read")
    rows = db.execute(
        select(Item, ItemBarcode)
        .join(ItemBarcode, ItemBarcode.item_id == Item.id)
        .where(
            ItemBarcode.company_id == p.company_id,
            ItemBarcode.keys.overlap(bc.keys_for(code)),
        )
        .options(selectinload(Item.uoms), selectinload(Item.barcodes))
    ).all()
    # Exact text match first.
    return sorted(((i, b) for i, b in rows), key=lambda r: r[1].barcode != bc.clean(code))


def _validate_item_refs(db: Session, p: Principal, v: dict[str, Any]) -> None:
    for key in ("purchase_tax_code_id", "sales_tax_code_id"):
        if v.get(key):
            _own(db, p, TaxCode, v[key], "Tax code")
    if v.get("category_id"):
        _own(db, p, ItemCategory, v["category_id"], "Category")
    cls = v.get("einvoice_classification")
    if cls and not _country_pack(db, p).validate_classification(cls):
        raise BadRequest("E-invoice classification must be a 3-digit code")


def _apply_uoms(
    db: Session, p: Principal, item: Item, base_uom: str, uoms: list[dict[str, Any]]
) -> None:
    known = {u.code for u in db.scalars(select(Uom).where(Uom.company_id == p.company_id))}
    codes = [u["uom"] for u in uoms]
    if len(set(codes)) != len(codes):
        raise BadRequest("Duplicate unit in item units")
    unknown = set(codes) - known
    if unknown:
        raise BadRequest("Unknown units", {"unknown": sorted(unknown)})
    base = [u for u in uoms if u["uom"] == base_uom]
    if not base or Decimal(base[0]["factor"]) != 1:
        raise BadRequest("The base unit must be listed with factor 1", {"base_uom": base_uom})
    if any(Decimal(u["factor"]) <= 0 for u in uoms):
        raise BadRequest("Unit factors must be positive")

    can_cost = p.has(FIELD_PERMS["cost"])
    if not can_cost and any(u.get("purchase_price") is not None for u in uoms):
        raise Forbidden("You may not set cost prices", {"required": [FIELD_PERMS["cost"]]})
    # Merge by unit code: update existing rows in place so (item_id, uom) never collides.
    existing = {u.uom: u for u in item.uoms}
    merged = []
    for u in uoms:
        row = existing.get(u["uom"]) or ItemUom(uom=u["uom"])
        row.factor = Decimal(u["factor"])
        row.selling_price = u.get("selling_price")
        if can_cost:  # users who cannot see cost keep whatever cost was there
            row.purchase_price = u.get("purchase_price")
        merged.append(row)
    item.uoms = merged


def _apply_barcodes(db: Session, p: Principal, item: Item, barcodes: list[dict[str, Any]]):
    item_uoms = {u.uom for u in item.uoms}
    seen: dict[str, str] = {}
    existing = {b.barcode: b for b in item.barcodes}
    new_rows = []
    for b in barcodes:
        code = bc.clean(b["barcode"])
        if not code:
            raise BadRequest("Empty barcode")
        if b["uom"] not in item_uoms:
            raise BadRequest("Barcode unit is not one of the item's units", {"barcode": code})
        keys = bc.keys_for(code)
        for k in keys:
            if k in seen:
                raise BadRequest(
                    "Two barcodes on this item are the same product code",
                    {"barcodes": [seen[k], code]},
                )
            seen[k] = code
        clash = db.execute(
            select(ItemBarcode.barcode, Item.code)
            .join(Item, Item.id == ItemBarcode.item_id)
            .where(
                ItemBarcode.company_id == p.company_id,
                ItemBarcode.keys.overlap(keys),
                ItemBarcode.item_id != item.id,
            )
        ).first()
        if clash:
            raise Conflict(
                "Barcode already belongs to another item",
                {"barcode": code, "existing_barcode": clash[0], "item_code": clash[1]},
            )
        row = existing.get(code) or ItemBarcode(company_id=p.company_id, barcode=code)
        row.uom = b["uom"]
        row.keys = keys
        new_rows.append(row)
    item.barcodes = new_rows


def _item_snapshot(item: Item) -> dict[str, Any]:
    return audit.snapshot(
        item,
        uoms=[
            {
                "uom": u.uom,
                "factor": u.factor,
                "selling_price": u.selling_price,
                "purchase_price": u.purchase_price,
            }
            for u in item.uoms
        ],
        barcodes=[{"barcode": b.barcode, "uom": b.uom} for b in item.barcodes],
    )


def create_item(
    db: Session,
    p: Principal,
    *,
    uoms: list[dict[str, Any]],
    barcodes: list[dict[str, Any]],
    **v: Any,
) -> Item:
    p.require("md.item.manage")
    _unique_code(db, p, Item, v["code"])
    _validate_item_refs(db, p, v)
    item = _new(Item, p, **v)
    db.add(item)
    db.flush()
    _apply_uoms(db, p, item, item.base_uom, uoms)
    db.flush()
    _apply_barcodes(db, p, item, barcodes)
    db.flush()
    audit.record(db, p, "create", "md.item", item.id, after=_item_snapshot(item))
    db.commit()
    return get_item(db, p, item.id)


def update_item(
    db: Session,
    p: Principal,
    item_id: uuid.UUID,
    version: int,
    *,
    uoms: list[dict[str, Any]] | None = None,
    barcodes: list[dict[str, Any]] | None = None,
    **v: Any,
) -> Item:
    p.require("md.item.manage")
    item = get_item(db, p, item_id)
    _check_version(item, version)
    before = _item_snapshot(item)
    changes = dict(v)
    if "code" in changes:
        _unique_code(db, p, Item, changes["code"], exclude=item.id)
    _validate_item_refs(db, p, changes)
    _assign(item, changes)
    if uoms is not None or "base_uom" in changes:
        current = [
            {
                "uom": u.uom,
                "factor": u.factor,
                "selling_price": u.selling_price,
                "purchase_price": u.purchase_price if p.has(FIELD_PERMS["cost"]) else None,
            }
            for u in item.uoms
        ]
        _apply_uoms(db, p, item, item.base_uom, uoms if uoms is not None else current)
        db.flush()
    if barcodes is not None:
        _apply_barcodes(db, p, item, barcodes)
    elif uoms is not None:
        orphan = [b.barcode for b in item.barcodes if b.uom not in {u.uom for u in item.uoms}]
        if orphan:
            raise BusinessRule("Barcodes still use a removed unit", {"barcodes": orphan})
    _touch(item, p)
    db.flush()
    audit.record(db, p, "update", "md.item", item.id, before=before, after=_item_snapshot(item))
    db.commit()
    return get_item(db, p, item.id)


# --- suppliers / customers ----------------------------------------------------------------

_PARTY = {
    "supplier": (Supplier, "md.supplier", "Supplier"),
    "customer": (Customer, "md.customer", "Customer"),
}


def _validate_party(db: Session, p: Principal, obj) -> None:
    cp = _country_pack(db, p)
    if obj.tin and not cp.validate_tin(obj.tin):
        raise BadRequest("TIN format is not valid", {"tin": obj.tin})
    if bool(obj.id_type) != bool(obj.id_no):
        raise BadRequest("id_type and id_no go together")
    if obj.id_type and not cp.validate_id(obj.id_type, obj.id_no):
        raise BadRequest("Registration / ID number is not valid", {"id_type": obj.id_type})
    if obj.msic_code and not cp.validate_msic(obj.msic_code):
        raise BadRequest("MSIC code must be 5 digits")
    if obj.state_code and obj.state_code not in cp.STATE_CODES:
        raise BadRequest("Unknown state code", {"allowed": sorted(cp.STATE_CODES)})
    if obj.email and "@" not in obj.email:
        raise BadRequest("Email is not valid")


def _guard_party_fields(p: Principal, kind: str, values: dict[str, Any]) -> None:
    if kind != "customer":
        return
    for fields, perm in ((CUSTOMER_PII, "pii"), (CUSTOMER_FINANCE, "finance")):
        if not p.has(FIELD_PERMS[perm]) and any(f in values for f in fields):
            raise Forbidden(f"You may not set {perm} fields", {"required": [FIELD_PERMS[perm]]})


def search_parties(
    db: Session,
    p: Principal,
    kind: str,
    *,
    q: str | None = None,
    active: bool | None = True,
    page: int = 1,
    page_size: int = 50,
):
    model, perm, _ = _PARTY[kind]
    p.require(f"{perm}.read")
    stmt = select(model).where(model.company_id == p.company_id)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(model.code.ilike(like), model.name.ilike(like), model.tin == q))
    if active is not None:
        stmt = stmt.where(model.is_active.is_(active))
    return _page(db, stmt.order_by(model.code), page, page_size)


def get_party(db: Session, p: Principal, kind: str, party_id: uuid.UUID):
    model, perm, label = _PARTY[kind]
    p.require(f"{perm}.read")
    return _own(db, p, model, party_id, label)


def create_party(db: Session, p: Principal, kind: str, **v: Any):
    model, perm, _ = _PARTY[kind]
    p.require(f"{perm}.manage")
    _guard_party_fields(p, kind, v)
    _unique_code(db, p, model, v["code"])
    obj = _new(model, p, **v)
    _validate_party(db, p, obj)
    db.add(obj)
    db.flush()
    audit.record(db, p, "create", f"md.{kind}", obj.id, after=audit.snapshot(obj))
    db.commit()
    db.refresh(obj)
    return obj


def update_party(db: Session, p: Principal, kind: str, party_id: uuid.UUID, version: int, **v):
    model, perm, label = _PARTY[kind]
    p.require(f"{perm}.manage")
    obj = _own(db, p, model, party_id, label)
    _check_version(obj, version)
    changes = dict(v)
    _guard_party_fields(p, kind, changes)
    if "code" in changes:
        _unique_code(db, p, model, changes["code"], exclude=obj.id)
    before = audit.snapshot(obj)
    _assign(obj, changes)
    _validate_party(db, p, obj)
    _touch(obj, p)
    db.flush()
    audit.record(db, p, "update", f"md.{kind}", obj.id, before=before, after=audit.snapshot(obj))
    db.commit()
    db.refresh(obj)
    return obj
