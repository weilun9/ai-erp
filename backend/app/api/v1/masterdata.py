"""Master data endpoints: /tax-codes, /uoms, /item-categories, /items, /suppliers, /customers."""

import re
import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.api.deps import DB, CurrentUser
from app.api.redact import sensitive, shape
from app.core import barcode as bc
from app.services import masterdata as svc

router = APIRouter(tags=["master data"])

Code = Annotated[str, Field(min_length=1, max_length=30, pattern=r"^\S+$")]
Money = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=4)]
Qty = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=4)]
PageNo = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=200)]
EXCLUDE_UNSET = {"response_model_exclude_unset": True}


class Page[T](BaseModel):
    items: list[T]
    total: int


class Versioned(BaseModel):
    version: int = Field(ge=1, description="The version you loaded (optimistic lock)")


# --- tax codes / UOMs / categories --------------------------------------------------------


class TaxCodeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    code: str
    name: str
    rate: Decimal
    einvoice_tax_type: str
    is_active: bool
    version: int


class TaxCodeIn(BaseModel):
    code: Code
    name: str = Field(min_length=1, max_length=100)
    rate: Decimal = Field(ge=0, le=100, decimal_places=4)
    einvoice_tax_type: str = Field(min_length=1, max_length=2)


class TaxCodePatch(Versioned):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    rate: Decimal | None = Field(default=None, ge=0, le=100, decimal_places=4)
    einvoice_tax_type: str | None = Field(default=None, min_length=1, max_length=2)
    is_active: bool | None = None


@router.get("/tax-codes")
def list_tax_codes(p: CurrentUser, db: DB) -> Page[TaxCodeOut]:
    rows = svc.list_tax_codes(db, p)
    return Page(items=[TaxCodeOut.model_validate(r) for r in rows], total=len(rows))


@router.post("/tax-codes", status_code=201)
def create_tax_code(body: TaxCodeIn, p: CurrentUser, db: DB) -> TaxCodeOut:
    return TaxCodeOut.model_validate(svc.create_tax_code(db, p, **body.model_dump()))


@router.patch("/tax-codes/{tax_id}")
def update_tax_code(tax_id: uuid.UUID, body: TaxCodePatch, p: CurrentUser, db: DB) -> TaxCodeOut:
    v = body.model_dump(exclude_unset=True)
    return TaxCodeOut.model_validate(svc.update_tax_code(db, p, tax_id, v.pop("version"), **v))


class UomOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    code: str
    name: str
    einvoice_code: str


class UomIn(BaseModel):
    code: str = Field(min_length=1, max_length=10, pattern=r"^[A-Z0-9]+$")
    name: str = Field(min_length=1, max_length=50)
    einvoice_code: str = Field(min_length=1, max_length=3)


@router.get("/uoms")
def list_uoms(p: CurrentUser, db: DB) -> Page[UomOut]:
    rows = svc.list_uoms(db, p)
    return Page(items=[UomOut.model_validate(r) for r in rows], total=len(rows))


@router.post("/uoms", status_code=201)
def create_uom(body: UomIn, p: CurrentUser, db: DB) -> UomOut:
    return UomOut.model_validate(svc.create_uom(db, p, **body.model_dump()))


class CategoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    code: str
    name: str


class CategoryIn(BaseModel):
    code: Code
    name: str = Field(min_length=1, max_length=100)


@router.get("/item-categories")
def list_categories(p: CurrentUser, db: DB) -> Page[CategoryOut]:
    rows = svc.list_categories(db, p)
    return Page(items=[CategoryOut.model_validate(r) for r in rows], total=len(rows))


@router.post("/item-categories", status_code=201)
def create_category(body: CategoryIn, p: CurrentUser, db: DB) -> CategoryOut:
    return CategoryOut.model_validate(svc.create_category(db, p, **body.model_dump()))


# --- item attributes ----------------------------------------------------------------------

AttrKind = Literal["BRAND", "GROUP", "COLOR", "SIZE", "UDF1", "UDF2", "UDF3"]


class AttributeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    kind: str
    code: str
    name: str
    is_active: bool
    version: int


class AttributeIn(BaseModel):
    kind: AttrKind
    code: Code
    name: str = Field(min_length=1, max_length=100)


class AttributePatch(Versioned):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    is_active: bool | None = None


@router.get("/item-attributes")
def list_attributes(p: CurrentUser, db: DB, kind: AttrKind | None = None) -> Page[AttributeOut]:
    rows = svc.list_attributes(db, p, kind)
    return Page(items=[AttributeOut.model_validate(r) for r in rows], total=len(rows))


@router.post("/item-attributes", status_code=201)
def create_attribute(body: AttributeIn, p: CurrentUser, db: DB) -> AttributeOut:
    return AttributeOut.model_validate(svc.create_attribute(db, p, **body.model_dump()))


@router.patch("/item-attributes/{attr_id}")
def update_attribute(
    attr_id: uuid.UUID, body: AttributePatch, p: CurrentUser, db: DB
) -> AttributeOut:
    v = body.model_dump(exclude_unset=True)
    version = v.pop("version")
    return AttributeOut.model_validate(svc.update_attribute(db, p, attr_id, version, **v))


# --- items --------------------------------------------------------------------------------


class ItemUomOut(BaseModel):
    uom: str
    factor: Decimal
    selling_price: Decimal | None
    price_2: Decimal | None = None
    price_3: Decimal | None = None
    price_4: Decimal | None = None
    price_5: Decimal | None = None
    purchase_price: Decimal | None = sensitive("cost")


class BarcodeOut(BaseModel):
    barcode: str
    uom: str
    ean13: str | None


class ItemOut(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    short_name: str | None
    category_id: uuid.UUID | None
    base_uom: str
    purchase_tax_code_id: uuid.UUID | None
    sales_tax_code_id: uuid.UUID | None
    einvoice_classification: str
    track_batch: bool
    is_active: bool
    description_2: str | None = None
    article_no: str | None = None
    brand_id: uuid.UUID | None = None
    group_id: uuid.UUID | None = None
    color_id: uuid.UUID | None = None
    size_id: uuid.UUID | None = None
    udf1_id: uuid.UUID | None = None
    udf2_id: uuid.UUID | None = None
    udf3_id: uuid.UUID | None = None
    sales_note: str | None = None
    purchase_note: str | None = None
    min_qty: Decimal | None = None
    max_qty: Decimal | None = None
    reorder_level: Decimal | None = None
    reorder_qty: Decimal | None = None
    min_selling_price: Decimal | None = None
    version: int
    uoms: list[ItemUomOut]
    barcodes: list[BarcodeOut]


def _item_out(item, p) -> ItemOut:
    return shape(
        ItemOut,
        item,
        p,
        uoms=[shape(ItemUomOut, u, p) for u in item.uoms],
        barcodes=[
            BarcodeOut(barcode=b.barcode, uom=b.uom, ean13=bc.full_ean13(b.barcode))
            for b in item.barcodes
        ],
    )


class ItemUomIn(BaseModel):
    uom: str = Field(min_length=1, max_length=10)
    factor: Decimal = Field(gt=0, max_digits=18, decimal_places=4)
    selling_price: Money | None = None
    price_2: Money | None = None
    price_3: Money | None = None
    price_4: Money | None = None
    price_5: Money | None = None
    purchase_price: Money | None = None


class BarcodeIn(BaseModel):
    barcode: str = Field(min_length=1, max_length=30)
    uom: str = Field(min_length=1, max_length=10)


class ItemIn(BaseModel):
    code: Code
    name: str = Field(min_length=1, max_length=200)
    short_name: str | None = Field(default=None, max_length=60)
    category_id: uuid.UUID | None = None
    base_uom: str = Field(min_length=1, max_length=10)
    purchase_tax_code_id: uuid.UUID | None = None
    sales_tax_code_id: uuid.UUID | None = None
    einvoice_classification: str = Field(default="022", pattern=r"^\d{3}$")
    track_batch: bool = False
    description_2: str | None = Field(default=None, max_length=200)
    article_no: str | None = Field(default=None, max_length=50)
    brand_id: uuid.UUID | None = None
    group_id: uuid.UUID | None = None
    color_id: uuid.UUID | None = None
    size_id: uuid.UUID | None = None
    udf1_id: uuid.UUID | None = None
    udf2_id: uuid.UUID | None = None
    udf3_id: uuid.UUID | None = None
    sales_note: str | None = Field(default=None, max_length=2000)
    purchase_note: str | None = Field(default=None, max_length=2000)
    min_qty: Qty | None = None
    max_qty: Qty | None = None
    reorder_level: Qty | None = None
    reorder_qty: Qty | None = None
    min_selling_price: Money | None = None
    uoms: list[ItemUomIn] = Field(min_length=1)
    barcodes: list[BarcodeIn] = []


class ItemPatch(Versioned):
    code: Code | None = None
    name: str | None = Field(default=None, min_length=1, max_length=200)
    short_name: str | None = Field(default=None, max_length=60)
    category_id: uuid.UUID | None = None
    base_uom: str | None = Field(default=None, min_length=1, max_length=10)
    purchase_tax_code_id: uuid.UUID | None = None
    sales_tax_code_id: uuid.UUID | None = None
    einvoice_classification: str | None = Field(default=None, pattern=r"^\d{3}$")
    track_batch: bool | None = None
    is_active: bool | None = None
    description_2: str | None = Field(default=None, max_length=200)
    article_no: str | None = Field(default=None, max_length=50)
    brand_id: uuid.UUID | None = None
    group_id: uuid.UUID | None = None
    color_id: uuid.UUID | None = None
    size_id: uuid.UUID | None = None
    udf1_id: uuid.UUID | None = None
    udf2_id: uuid.UUID | None = None
    udf3_id: uuid.UUID | None = None
    sales_note: str | None = Field(default=None, max_length=2000)
    purchase_note: str | None = Field(default=None, max_length=2000)
    min_qty: Qty | None = None
    max_qty: Qty | None = None
    reorder_level: Qty | None = None
    reorder_qty: Qty | None = None
    min_selling_price: Money | None = None
    uoms: list[ItemUomIn] | None = Field(default=None, min_length=1)
    barcodes: list[BarcodeIn] | None = None


class LookupHit(BaseModel):
    matched_barcode: str
    matched_uom: str
    exact: bool
    item: ItemOut


class LookupOut(BaseModel):
    query: str
    ambiguous: bool
    hits: list[LookupHit]


@router.get("/items", **EXCLUDE_UNSET)
def search_items(
    p: CurrentUser,
    db: DB,
    q: str | None = None,
    category_id: uuid.UUID | None = None,
    attribute_id: uuid.UUID | None = None,
    active: bool | None = True,
    page: PageNo = 1,
    page_size: PageSize = 50,
) -> Page[ItemOut]:
    rows, total = svc.search_items(
        db,
        p,
        q=q,
        category_id=category_id,
        attribute_id=attribute_id,
        active=active,
        page=page,
        page_size=page_size,
    )
    return Page(items=[_item_out(i, p) for i in rows], total=total)


@router.get("/items/lookup", **EXCLUDE_UNSET)
def lookup_barcode(
    p: CurrentUser, db: DB, barcode: Annotated[str, Query(min_length=1, max_length=30)]
) -> LookupOut:
    """Scan / type a barcode. 11, 12 and 13-digit forms of the same code all match."""
    hits = svc.lookup_barcode(db, p, barcode)
    cleaned = bc.clean(barcode)
    return LookupOut(
        query=cleaned,
        ambiguous=len({i.id for i, _ in hits}) > 1,
        hits=[
            LookupHit(
                matched_barcode=b.barcode,
                matched_uom=b.uom,
                exact=b.barcode == cleaned,
                item=_item_out(i, p),
            )
            for i, b in hits
        ],
    )


@router.get("/items/{item_id}", **EXCLUDE_UNSET)
def get_item(item_id: uuid.UUID, p: CurrentUser, db: DB) -> ItemOut:
    return _item_out(svc.get_item(db, p, item_id), p)


class ReorderSet(BaseModel):
    min_qty: Decimal | None = None
    max_qty: Decimal | None = None
    reorder_level: Decimal | None = None
    reorder_qty: Decimal | None = None


class ItemLocationOut(BaseModel):
    location_id: uuid.UUID
    location_code: str
    location_name: str
    qty: Decimal
    avg_cost: Decimal | None = sensitive("cost")
    own: ReorderSet
    effective: ReorderSet
    shelf: str | None
    below_reorder: bool


class ItemSupplierOut(BaseModel):
    supplier_id: uuid.UUID
    supplier_code: str
    supplier_name: str
    last_date: date
    last_doc_no: str | None
    documents: int
    base_qty: Decimal
    last_unit_cost: Decimal | None = sensitive("cost")


class PurchasePriceOut(BaseModel):
    doc_id: uuid.UUID
    doc_no: str | None
    doc_date: date
    supplier_code: str
    uom: str
    qty: Decimal
    unit_price: Decimal
    discount_pct: Decimal
    unit_cost: Decimal


class ItemOverviewOut(BaseModel):
    item_id: uuid.UUID
    locations: list[ItemLocationOut]
    total_qty: Decimal
    suppliers: list[ItemSupplierOut]
    purchase_history: list[PurchasePriceOut]
    cost_hidden: bool


class ItemLocationIn(BaseModel):
    location_id: uuid.UUID
    min_qty: Qty | None = None
    max_qty: Qty | None = None
    reorder_level: Qty | None = None
    reorder_qty: Qty | None = None
    shelf: str | None = Field(default=None, max_length=30)


class CopyReorderIn(BaseModel):
    item_id: uuid.UUID | None = Field(default=None, description="None = every active item")
    fields: list[Literal["min_qty", "max_qty", "reorder_level", "reorder_qty"]] = Field(
        min_length=1
    )


def _overview_out(ov, p) -> ItemOverviewOut:
    return ItemOverviewOut(
        item_id=ov["item_id"],
        locations=[shape(ItemLocationOut, r, p) for r in ov["locations"]],
        total_qty=ov["total_qty"],
        suppliers=[shape(ItemSupplierOut, r, p) for r in ov["suppliers"]],
        purchase_history=[PurchasePriceOut(**r) for r in ov["purchase_history"]],
        cost_hidden=ov["cost_hidden"],
    )


@router.get("/items/{item_id}/overview", **EXCLUDE_UNSET)
def item_overview(item_id: uuid.UUID, p: CurrentUser, db: DB) -> ItemOverviewOut:
    """Stock per shop with reorder settings, suppliers who supplied it, purchase prices."""
    return _overview_out(svc.item_overview(db, p, item_id), p)


@router.put("/items/{item_id}/locations", **EXCLUDE_UNSET)
def set_item_locations(
    item_id: uuid.UUID, body: list[ItemLocationIn], p: CurrentUser, db: DB
) -> ItemOverviewOut:
    rows = [r.model_dump() for r in body]
    return _overview_out(svc.set_item_locations(db, p, item_id, rows), p)


@router.post("/items/copy-reorder-to-locations")
def copy_reorder(body: CopyReorderIn, p: CurrentUser, db: DB) -> dict[str, int]:
    """Copy the item's reorder values to every shop (one item, or every item)."""
    n = svc.copy_reorder_to_locations(db, p, item_id=body.item_id, fields=list(body.fields))
    return {"updated": n}


@router.post("/items", status_code=201, **EXCLUDE_UNSET)
def create_item(body: ItemIn, p: CurrentUser, db: DB) -> ItemOut:
    v = body.model_dump(exclude_unset=True)
    v.setdefault("uoms", [])
    v.setdefault("barcodes", [])
    for u in v["uoms"]:
        u.setdefault("purchase_price", None)
    return _item_out(svc.create_item(db, p, **v), p)


@router.patch("/items/{item_id}", **EXCLUDE_UNSET)
def update_item(item_id: uuid.UUID, body: ItemPatch, p: CurrentUser, db: DB) -> ItemOut:
    v = body.model_dump(exclude_unset=True)
    version = v.pop("version")
    return _item_out(svc.update_item(db, p, item_id, version, **v), p)


# --- suppliers / customers ----------------------------------------------------------------


class PartyBase(BaseModel):
    tin: str | None = Field(default=None, max_length=20)
    id_type: str | None = Field(default=None, max_length=10)
    sst_no: str | None = Field(default=None, max_length=50)
    msic_code: str | None = Field(default=None, max_length=5)
    city: str | None = Field(default=None, max_length=50)
    state_code: str | None = Field(default=None, max_length=2)
    country_code: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    term_days: int | None = Field(default=None, ge=0, le=365)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")

    @field_validator("tin", "id_type", "sst_no")
    @classmethod
    def _upper(cls, v: str | None) -> str | None:
        return re.sub(r"\s+", "", v).upper() if v else v


class SupplierProfile(BaseModel):
    """Supplier habits the AI and the intake checks read (design doc section 10)."""

    invoice_no_pattern: str | None = None  # regex, e.g. ^IJB\d+$
    cn_no_pattern: str | None = None
    refund_no_pattern: str | None = None
    sst_marks: dict[str, str] = {}  # receipt mark -> tax code, e.g. {"*": "ST5", "#": "NA"}
    uom_notes: str | None = None  # e.g. "1 Bd = 30 PKT"
    settlement_notes: str | None = None
    tax_notes: str | None = None

    @field_validator("invoice_no_pattern", "cn_no_pattern", "refund_no_pattern")
    @classmethod
    def _regex(cls, v: str | None) -> str | None:
        if v:
            try:
                re.compile(v)
            except re.error as exc:
                raise ValueError(f"not a valid pattern: {exc}") from exc
        return v


class SupplierFields(PartyBase):
    id_no: str | None = Field(default=None, max_length=30)
    address1: str | None = Field(default=None, max_length=150)
    address2: str | None = Field(default=None, max_length=150)
    address3: str | None = Field(default=None, max_length=150)
    postcode: str | None = Field(default=None, max_length=10)
    phone: str | None = Field(default=None, max_length=30)
    email: str | None = Field(default=None, max_length=200)
    contact_person: str | None = Field(default=None, max_length=100)
    profile: SupplierProfile | None = None


class SupplierIn(SupplierFields):
    code: Code
    name: str = Field(min_length=1, max_length=200)


class SupplierPatch(SupplierFields, Versioned):
    code: Code | None = None
    name: str | None = Field(default=None, min_length=1, max_length=200)
    is_active: bool | None = None


class SupplierOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    code: str
    name: str
    tin: str | None
    id_type: str | None
    id_no: str | None
    sst_no: str | None
    msic_code: str | None
    address1: str | None
    address2: str | None
    address3: str | None
    city: str | None
    postcode: str | None
    state_code: str | None
    country_code: str
    phone: str | None
    email: str | None
    contact_person: str | None
    term_days: int
    currency: str
    is_active: bool
    profile: SupplierProfile
    version: int


class CustomerFields(PartyBase):
    id_no: str | None = Field(default=None, max_length=30)
    address1: str | None = Field(default=None, max_length=150)
    address2: str | None = Field(default=None, max_length=150)
    address3: str | None = Field(default=None, max_length=150)
    postcode: str | None = Field(default=None, max_length=10)
    phone: str | None = Field(default=None, max_length=30)
    email: str | None = Field(default=None, max_length=200)
    contact_person: str | None = Field(default=None, max_length=100)
    credit_limit: Money | None = None
    price_level: int | None = Field(default=None, ge=1, le=5)


class CustomerIn(CustomerFields):
    code: Code
    name: str = Field(min_length=1, max_length=200)


class CustomerPatch(CustomerFields, Versioned):
    code: Code | None = None
    name: str | None = Field(default=None, min_length=1, max_length=200)
    is_active: bool | None = None


class CustomerOut(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    tin: str | None
    id_type: str | None
    id_no: str | None = sensitive("pii")
    sst_no: str | None
    msic_code: str | None
    address1: str | None = sensitive("pii")
    address2: str | None = sensitive("pii")
    address3: str | None = sensitive("pii")
    city: str | None
    postcode: str | None = sensitive("pii")
    state_code: str | None
    country_code: str
    phone: str | None = sensitive("pii")
    email: str | None = sensitive("pii")
    contact_person: str | None = sensitive("pii")
    term_days: int
    credit_limit: Decimal | None = sensitive("finance")
    price_level: int
    currency: str
    is_active: bool
    version: int


def _party_values(body: BaseModel) -> dict[str, Any]:
    v = body.model_dump(exclude_unset=True)
    if "profile" in v:
        v["profile"] = v["profile"] or {}
    for key in ("term_days", "price_level", "currency", "country_code"):  # NOT NULL columns
        if key in v and v[key] is None:
            del v[key]
    return v


@router.get("/suppliers")
def list_suppliers(
    p: CurrentUser,
    db: DB,
    q: str | None = None,
    active: bool | None = True,
    page: PageNo = 1,
    page_size: PageSize = 50,
) -> Page[SupplierOut]:
    rows, total = svc.search_parties(
        db, p, "supplier", q=q, active=active, page=page, page_size=page_size
    )
    return Page(items=[SupplierOut.model_validate(r) for r in rows], total=total)


@router.get("/suppliers/{party_id}")
def get_supplier(party_id: uuid.UUID, p: CurrentUser, db: DB) -> SupplierOut:
    return SupplierOut.model_validate(svc.get_party(db, p, "supplier", party_id))


@router.post("/suppliers", status_code=201)
def create_supplier(body: SupplierIn, p: CurrentUser, db: DB) -> SupplierOut:
    return SupplierOut.model_validate(svc.create_party(db, p, "supplier", **_party_values(body)))


@router.patch("/suppliers/{party_id}")
def update_supplier(
    party_id: uuid.UUID, body: SupplierPatch, p: CurrentUser, db: DB
) -> SupplierOut:
    v = _party_values(body)
    version = v.pop("version")
    return SupplierOut.model_validate(svc.update_party(db, p, "supplier", party_id, version, **v))


@router.get("/customers", **EXCLUDE_UNSET)
def list_customers(
    p: CurrentUser,
    db: DB,
    q: str | None = None,
    active: bool | None = True,
    page: PageNo = 1,
    page_size: PageSize = 50,
) -> Page[CustomerOut]:
    rows, total = svc.search_parties(
        db, p, "customer", q=q, active=active, page=page, page_size=page_size
    )
    return Page(items=[shape(CustomerOut, r, p) for r in rows], total=total)


@router.get("/customers/{party_id}", **EXCLUDE_UNSET)
def get_customer(party_id: uuid.UUID, p: CurrentUser, db: DB) -> CustomerOut:
    return shape(CustomerOut, svc.get_party(db, p, "customer", party_id), p)


@router.post("/customers", status_code=201, **EXCLUDE_UNSET)
def create_customer(body: CustomerIn, p: CurrentUser, db: DB) -> CustomerOut:
    return shape(CustomerOut, svc.create_party(db, p, "customer", **_party_values(body)), p)


@router.patch("/customers/{party_id}", **EXCLUDE_UNSET)
def update_customer(
    party_id: uuid.UUID, body: CustomerPatch, p: CurrentUser, db: DB
) -> CustomerOut:
    v = _party_values(body)
    version = v.pop("version")
    return shape(CustomerOut, svc.update_party(db, p, "customer", party_id, version, **v), p)
