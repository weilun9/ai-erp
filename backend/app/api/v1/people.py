"""Owner-facing management: tags, employees, devices."""

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.deps import DB, CurrentUser
from app.models import AppUser, Device, Tag
from app.services import people as svc

router = APIRouter(tags=["people"])

Color = Field(pattern=r"^#[0-9a-fA-F]{6}$")
Language = Literal["zh", "en", "ms"]


class Page[T](BaseModel):
    items: list[T]
    total: int


# --- tags ---------------------------------------------------------------------------------


class TagBrief(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    color: str


class TagOut(TagBrief):
    all_locations: bool
    is_system: bool
    permissions: list[str]
    user_count: int


def _tag_out(t: Tag, user_count: int) -> TagOut:
    return TagOut(
        id=t.id,
        code=t.code,
        name=t.name,
        color=t.color,
        all_locations=t.all_locations,
        is_system=t.is_system,
        permissions=sorted(pp.code for pp in t.permissions),
        user_count=user_count,
    )


class TagCreate(BaseModel):
    code: str = Field(min_length=2, max_length=30, pattern=r"^[a-z0-9_]+$")
    name: str = Field(min_length=1, max_length=100)
    color: str = Color
    all_locations: bool = False
    permissions: set[str] = set()


class TagUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    permissions: set[str] | None = None


@router.get("/tags")
def list_tags(p: CurrentUser, db: DB) -> Page[TagOut]:
    rows = svc.list_tags(db, p)
    return Page(items=[_tag_out(t, n) for t, n in rows], total=len(rows))


@router.post("/tags", status_code=201)
def create_tag(body: TagCreate, p: CurrentUser, db: DB) -> TagOut:
    return _tag_out(svc.create_tag(db, p, **body.model_dump()), 0)


@router.patch("/tags/{tag_id}")
def update_tag(tag_id: uuid.UUID, body: TagUpdate, p: CurrentUser, db: DB) -> TagOut:
    t = svc.update_tag(db, p, tag_id, **body.model_dump())
    return _tag_out(t, 0)


# --- employees ----------------------------------------------------------------------------


class LocationBrief(BaseModel):
    id: uuid.UUID
    code: str
    name: str


class UserOut(BaseModel):
    id: uuid.UUID
    username: str
    display_name: str
    language: str
    is_active: bool
    tags: list[TagBrief]
    locations: list[LocationBrief]


def _user_out(u: AppUser) -> UserOut:
    return UserOut(
        id=u.id,
        username=u.username,
        display_name=u.display_name,
        language=u.language,
        is_active=u.is_active,
        tags=[TagBrief(id=t.id, code=t.code, name=t.name, color=t.color) for t in u.tags],
        locations=[LocationBrief(id=loc.id, code=loc.code, name=loc.name) for loc in u.locations],
    )


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=50)
    display_name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)
    language: Language = "zh"
    tag_ids: set[uuid.UUID] = set()
    location_ids: set[uuid.UUID] = set()


class UserUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    language: Language | None = None
    is_active: bool | None = None
    location_ids: set[uuid.UUID] | None = None
    password: str | None = Field(default=None, max_length=200)


class TagAssignment(BaseModel):
    tag_ids: set[uuid.UUID]


@router.get("/users")
def list_users(p: CurrentUser, db: DB) -> Page[UserOut]:
    rows = svc.list_users(db, p)
    return Page(items=[_user_out(u) for u in rows], total=len(rows))


@router.post("/users", status_code=201)
def create_user(body: UserCreate, p: CurrentUser, db: DB) -> UserOut:
    return _user_out(svc.create_user(db, p, **body.model_dump()))


@router.patch("/users/{user_id}")
def update_user(user_id: uuid.UUID, body: UserUpdate, p: CurrentUser, db: DB) -> UserOut:
    return _user_out(svc.update_user(db, p, user_id, **body.model_dump()))


@router.put("/users/{user_id}/tags")
def set_user_tags(user_id: uuid.UUID, body: TagAssignment, p: CurrentUser, db: DB) -> UserOut:
    return _user_out(svc.set_user_tags(db, p, user_id, body.tag_ids))


# --- devices ------------------------------------------------------------------------------


class DeviceOut(BaseModel):
    id: uuid.UUID
    name: str
    can_edit: bool
    user_agent: str | None
    last_ip: str | None
    last_seen_at: datetime
    authorized_at: datetime | None
    is_current: bool


def _device_out(d: Device, p) -> DeviceOut:
    return DeviceOut(
        id=d.id,
        name=d.name,
        can_edit=d.can_edit,
        user_agent=d.user_agent,
        last_ip=d.last_ip,
        last_seen_at=d.last_seen_at,
        authorized_at=d.authorized_at,
        is_current=d.id == p.device_id,
    )


class DeviceRename(BaseModel):
    name: str = Field(min_length=1, max_length=100)


@router.get("/devices")
def list_devices(p: CurrentUser, db: DB) -> Page[DeviceOut]:
    rows = svc.list_devices(db, p)
    return Page(items=[_device_out(d, p) for d in rows], total=len(rows))


@router.post("/devices/{device_id}/authorize")
def authorize_device(device_id: uuid.UUID, p: CurrentUser, db: DB) -> DeviceOut:
    return _device_out(svc.set_device_edit(db, p, device_id, True), p)


@router.post("/devices/{device_id}/deauthorize")
def deauthorize_device(device_id: uuid.UUID, p: CurrentUser, db: DB) -> DeviceOut:
    return _device_out(svc.set_device_edit(db, p, device_id, False), p)


@router.patch("/devices/{device_id}")
def rename_device(device_id: uuid.UUID, body: DeviceRename, p: CurrentUser, db: DB) -> DeviceOut:
    return _device_out(svc.rename_device(db, p, device_id, body.name), p)


@router.post("/devices/{device_id}/sign-out")
def sign_out_device(device_id: uuid.UUID, p: CurrentUser, db: DB) -> dict[str, int]:
    return {"sessions_ended": svc.sign_out_device(db, p, device_id)}
