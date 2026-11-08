"""Authentication and the Principal: who is calling and what they may do.

Every entry point (web API, AI butler, MCP) builds the same Principal and passes it
to the service layer, so permission checks live in one place.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.errors import Forbidden
from app.core.security import verify_password
from app.models import AppUser, Location, Role


@dataclass(frozen=True)
class Principal:
    user_id: uuid.UUID
    company_id: uuid.UUID
    username: str
    display_name: str
    language: str
    roles: frozenset[str]
    permissions: frozenset[str]
    # None means every location of the company.
    location_ids: frozenset[uuid.UUID] | None

    def has(self, code: str) -> bool:
        return code in self.permissions

    def require(self, *codes: str) -> None:
        missing = [c for c in codes if c not in self.permissions]
        if missing:
            raise Forbidden("Missing permission", {"required": missing})

    def can_see_location(self, location_id: uuid.UUID) -> bool:
        return self.location_ids is None or location_id in self.location_ids


def authenticate(db: Session, username: str, password: str) -> AppUser | None:
    user = db.scalar(select(AppUser).where(AppUser.username == username))
    ok = verify_password(password, user.password_hash if user else None)
    if not ok or user is None or not user.is_active:
        return None
    return user


def load_principal(db: Session, user_id: uuid.UUID) -> Principal | None:
    user = db.scalar(
        select(AppUser)
        .where(AppUser.id == user_id, AppUser.is_active.is_(True))
        .options(
            selectinload(AppUser.roles).selectinload(Role.permissions),
            selectinload(AppUser.locations),
        )
    )
    if user is None:
        return None

    permissions = frozenset(p.code for r in user.roles for p in r.permissions)
    location_ids: frozenset[uuid.UUID] | None
    if any(r.all_locations for r in user.roles):
        location_ids = None
    else:
        location_ids = frozenset(
            loc.id for loc in user.locations if loc.company_id == user.company_id
        )
    return Principal(
        user_id=user.id,
        company_id=user.company_id,
        username=user.username,
        display_name=user.display_name,
        language=user.language,
        roles=frozenset(r.code for r in user.roles),
        permissions=permissions,
        location_ids=location_ids,
    )


def visible_locations(db: Session, principal: Principal) -> list[Location]:
    stmt = select(Location).where(Location.company_id == principal.company_id)
    if principal.location_ids is not None:
        stmt = stmt.where(Location.id.in_(principal.location_ids))
    return list(db.scalars(stmt.order_by(Location.code)))
