"""/boms and /inventory/assemblies: bills of materials and making / breaking up products."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import DB, CurrentUser
from app.services import assembly as svc

router = APIRouter(tags=["assembly"])


class BomLineIn(BaseModel):
    item_id: uuid.UUID
    qty: Decimal = Field(gt=0, max_digits=18, decimal_places=4)  # base units


class BomIn(BaseModel):
    output_qty: Decimal = Field(default=Decimal(1), gt=0, max_digits=18, decimal_places=4)
    note: str | None = Field(default=None, max_length=300)
    lines: list[BomLineIn] = Field(default_factory=list, max_length=200)  # empty = remove


class BomLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    line_no: int
    item_id: uuid.UUID
    qty: Decimal


class BomOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    item_id: uuid.UUID
    output_qty: Decimal
    note: str | None
    version: int
    lines: list[BomLineOut]


@router.get("/boms")
def list_boms(p: CurrentUser, db: DB) -> list[BomOut]:
    return [BomOut.model_validate(b) for b in svc.list_boms(db, p)]


@router.get("/items/{item_id}/bom")
def get_bom(item_id: uuid.UUID, p: CurrentUser, db: DB) -> BomOut | None:
    b = svc.get_bom(db, p, item_id)
    return BomOut.model_validate(b) if b else None


@router.put("/items/{item_id}/bom")
def set_bom(item_id: uuid.UUID, body: BomIn, p: CurrentUser, db: DB) -> BomOut | None:
    b = svc.set_bom(
        db,
        p,
        item_id,
        output_qty=body.output_qty,
        note=body.note,
        lines=[x.model_dump() for x in body.lines],
    )
    return BomOut.model_validate(b) if b else None


@router.get("/items/{item_id}/bom/needs")
def bom_needs(
    item_id: uuid.UUID, qty: Decimal, location_id: uuid.UUID, p: CurrentUser, db: DB
) -> list[dict]:
    return svc.needs(db, p, item_id, qty, location_id)


class AssemblyIn(BaseModel):
    item_id: uuid.UUID
    qty: Decimal = Field(gt=0, max_digits=18, decimal_places=4)  # base units of the product
    location_id: uuid.UUID
    doc_date: date
    direction: Literal["ASSEMBLE", "DISASSEMBLE"] = "ASSEMBLE"
    remark: str | None = Field(default=None, max_length=300)


class AssemblyLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    line_no: int
    item_id: uuid.UUID
    qty: Decimal
    unit_cost: Decimal


class AssemblyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    doc_no: str
    doc_date: date
    location_id: uuid.UUID
    item_id: uuid.UUID
    direction: str
    qty: Decimal
    unit_cost: Decimal
    cost_total: Decimal
    status: str
    remark: str | None
    version: int
    created_at: datetime
    void_reason: str | None
    lines: list[AssemblyLineOut]


class VoidIn(BaseModel):
    version: int
    reason: str = Field(min_length=1, max_length=300)


@router.get("/inventory/assemblies")
def list_assemblies(p: CurrentUser, db: DB) -> list[AssemblyOut]:
    return [AssemblyOut.model_validate(x) for x in svc.list_assemblies(db, p)]


@router.post("/inventory/assemblies", status_code=201)
def make(body: AssemblyIn, p: CurrentUser, db: DB) -> AssemblyOut:
    return AssemblyOut.model_validate(svc.make(db, p, **body.model_dump()))


@router.post("/inventory/assemblies/{doc_id}/void")
def void(doc_id: uuid.UUID, body: VoidIn, p: CurrentUser, db: DB) -> AssemblyOut:
    return AssemblyOut.model_validate(svc.void(db, p, doc_id, body.version, body.reason))
