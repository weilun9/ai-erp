"""/company: our own company profile — the seller block on every e-invoice."""

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.deps import DB, CurrentUser
from app.core.errors import BusinessRule, VersionConflict
from app.localization import pack
from app.models import Company
from app.services import audit

router = APIRouter(prefix="/company", tags=["company"])

PARTY_FIELDS = (
    "tin",
    "id_type",
    "id_no",
    "sst_no",
    "msic_code",
    "address1",
    "address2",
    "address3",
    "city",
    "postcode",
    "state_code",
    "country_code",
    "phone",
    "email",
)


class CompanyOut(BaseModel):
    code: str
    name: str
    country: str
    business_activity: str | None
    einvoice_enabled: bool
    base_currency: str
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
    version: int


class CompanyPatch(BaseModel):
    version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    business_activity: str | None = Field(default=None, max_length=300)
    einvoice_enabled: bool | None = None
    tin: str | None = Field(default=None, max_length=20)
    id_type: str | None = Field(default=None, max_length=10)
    id_no: str | None = Field(default=None, max_length=30)
    sst_no: str | None = Field(default=None, max_length=50)
    msic_code: str | None = Field(default=None, max_length=5)
    address1: str | None = Field(default=None, max_length=150)
    address2: str | None = Field(default=None, max_length=150)
    address3: str | None = Field(default=None, max_length=150)
    city: str | None = Field(default=None, max_length=50)
    postcode: str | None = Field(default=None, max_length=10)
    state_code: str | None = Field(default=None, max_length=2)
    phone: str | None = Field(default=None, max_length=30)
    email: str | None = Field(default=None, max_length=200)


def _out(c: Company) -> CompanyOut:
    return CompanyOut(**{f: getattr(c, f) for f in CompanyOut.model_fields})


@router.get("")
def get_company(p: CurrentUser, db: DB) -> CompanyOut:
    p.require("core.company.read")
    return _out(db.get(Company, p.company_id))


@router.patch("")
def update_company(body: CompanyPatch, p: CurrentUser, db: DB) -> CompanyOut:
    p.require("core.company.manage")
    c = db.get(Company, p.company_id, with_for_update=True)
    if c.version != body.version:
        raise VersionConflict("The company profile was changed by someone else; reload it")
    v: dict[str, Any] = body.model_dump(exclude_unset=True, exclude={"version"})
    my = pack(c.country)
    bad = []
    if v.get("tin") and not my.validate_tin(v["tin"].upper()):
        bad.append("tin")
    if v.get("msic_code") and not my.validate_msic(v["msic_code"]):
        bad.append("msic_code")
    if v.get("state_code") and v["state_code"] not in my.STATE_CODES:
        bad.append("state_code")
    id_type = v.get("id_type", c.id_type)
    id_no = v.get("id_no", c.id_no)
    if (
        ("id_type" in v or "id_no" in v)
        and id_type
        and id_no
        and not my.validate_id(id_type, id_no)
    ):
        bad.append("id_no")
    if bad:
        raise BusinessRule("Some values are not valid", {"fields": bad})
    if v.get("tin"):
        v["tin"] = v["tin"].upper()
    before = audit.snapshot(c)
    for key, val in v.items():
        setattr(c, key, val)
    c.version += 1
    c.updated_by = p.user_id
    db.flush()
    audit.record(db, p, "update", "core.company", c.id, before=before, after=audit.snapshot(c))
    db.commit()
    db.refresh(c)
    return _out(c)
