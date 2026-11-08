import uuid
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import DB, CurrentUser
from app.services import locations as svc

router = APIRouter(prefix="/locations", tags=["locations"])


class LocationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    type: str
    is_active: bool
    version: int


class LocationCreate(BaseModel):
    code: str = Field(min_length=1, max_length=20, pattern=r"^[A-Z0-9_-]+$")
    name: str = Field(min_length=1, max_length=200)
    type: Literal["STORE", "WAREHOUSE", "ECOM"] = "STORE"


class Page[T](BaseModel):
    items: list[T]
    total: int


@router.get("")
def list_locations(p: CurrentUser, db: DB) -> Page[LocationOut]:
    rows = svc.list_locations(db, p)
    return Page(items=[LocationOut.model_validate(r) for r in rows], total=len(rows))


@router.get("/{location_id}")
def get_location(location_id: uuid.UUID, p: CurrentUser, db: DB) -> LocationOut:
    return LocationOut.model_validate(svc.get_location(db, p, location_id))


@router.post("", status_code=201)
def create_location(body: LocationCreate, p: CurrentUser, db: DB) -> LocationOut:
    loc = svc.create_location(db, p, code=body.code, name=body.name, type=body.type)
    return LocationOut.model_validate(loc)
