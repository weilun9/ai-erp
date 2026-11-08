import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import Conflict, NotFound
from app.models import Location
from app.services.auth import Principal, visible_locations


def list_locations(db: Session, principal: Principal) -> list[Location]:
    principal.require("core.location.read")
    return visible_locations(db, principal)


def get_location(db: Session, principal: Principal, location_id: uuid.UUID) -> Location:
    principal.require("core.location.read")
    loc = db.get(Location, location_id)
    if (
        loc is None
        or loc.company_id != principal.company_id
        or not principal.can_see_location(loc.id)
    ):
        raise NotFound("Location not found")
    return loc


def create_location(
    db: Session, principal: Principal, *, code: str, name: str, type: str
) -> Location:
    principal.require("core.location.manage")
    exists = db.scalar(
        select(Location.id).where(
            Location.company_id == principal.company_id, Location.code == code
        )
    )
    if exists:
        raise Conflict("Location code already exists", {"code": code})
    loc = Location(
        company_id=principal.company_id,
        code=code,
        name=name,
        type=type,
        created_by=principal.user_id,
        updated_by=principal.user_id,
    )
    db.add(loc)
    db.commit()
    db.refresh(loc)
    return loc
