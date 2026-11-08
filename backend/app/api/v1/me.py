import uuid

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import DB, CurrentUser
from app.models import Tag
from app.services.auth import visible_locations

router = APIRouter(tags=["me"])


class LocationBrief(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    type: str


class TagBrief(BaseModel):
    code: str
    name: str
    color: str


class DeviceBrief(BaseModel):
    id: uuid.UUID
    name: str
    can_edit: bool


class MeOut(BaseModel):
    id: uuid.UUID
    company_id: uuid.UUID
    username: str
    display_name: str
    language: str
    tags: list[TagBrief]
    permissions: list[str]
    all_locations: bool
    locations: list[LocationBrief]
    device: DeviceBrief


@router.get("/me")
def me(p: CurrentUser, db: DB) -> MeOut:
    tags = db.scalars(
        select(Tag)
        .where(Tag.company_id == p.company_id, Tag.code.in_(p.tags))
        .order_by(Tag.code)
    )
    return MeOut(
        id=p.user_id,
        company_id=p.company_id,
        username=p.username,
        display_name=p.display_name,
        language=p.language,
        tags=[TagBrief(code=t.code, name=t.name, color=t.color) for t in tags],
        permissions=sorted(p.permissions),
        all_locations=p.location_ids is None,
        locations=[
            LocationBrief(id=loc.id, code=loc.code, name=loc.name, type=loc.type)
            for loc in visible_locations(db, p)
        ],
        device=DeviceBrief(id=p.device_id, name=p.device_name, can_edit=p.device_can_edit),
    )
