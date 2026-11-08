import uuid

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.deps import DB, CurrentUser
from app.services.auth import visible_locations

router = APIRouter(tags=["me"])


class LocationBrief(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    type: str


class MeOut(BaseModel):
    id: uuid.UUID
    company_id: uuid.UUID
    username: str
    display_name: str
    language: str
    roles: list[str]
    permissions: list[str]
    all_locations: bool
    locations: list[LocationBrief]


@router.get("/me")
def me(p: CurrentUser, db: DB) -> MeOut:
    return MeOut(
        id=p.user_id,
        company_id=p.company_id,
        username=p.username,
        display_name=p.display_name,
        language=p.language,
        roles=sorted(p.roles),
        permissions=sorted(p.permissions),
        all_locations=p.location_ids is None,
        locations=[
            LocationBrief(id=loc.id, code=loc.code, name=loc.name, type=loc.type)
            for loc in visible_locations(db, p)
        ],
    )
