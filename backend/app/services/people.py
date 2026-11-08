"""Employees, tags and devices — managed by the owner."""

import re
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.core.errors import BadRequest, Conflict, Forbidden, NotFound
from app.core.permissions import PERMISSIONS
from app.core.security import hash_password
from app.models import AppUser, Device, Location, Permission, Tag, user_tag
from app.services.auth import Principal, revoke_sessions

MIN_PASSWORD = 10
OWNER_TAG = "owner"


# --- tags ---------------------------------------------------------------------------------


def list_tags(db: Session, p: Principal) -> list[tuple[Tag, int]]:
    p.require("core.tag.read")
    counts = dict(
        db.execute(
            select(user_tag.c.tag_id, func.count())
            .join(AppUser, AppUser.id == user_tag.c.user_id)
            .where(AppUser.company_id == p.company_id, AppUser.is_active.is_(True))
            .group_by(user_tag.c.tag_id)
        ).all()
    )
    tags = db.scalars(
        select(Tag)
        .where(Tag.company_id == p.company_id)
        .options(selectinload(Tag.permissions))
        .order_by(Tag.is_system.desc(), Tag.name)
    ).all()
    return [(t, counts.get(t.id, 0)) for t in tags]


def _get_tag(db: Session, p: Principal, tag_id: uuid.UUID) -> Tag:
    tag = db.get(Tag, tag_id)
    if tag is None or tag.company_id != p.company_id:
        raise NotFound("Tag not found")
    return tag


def _check_grantable(p: Principal, codes: set[str]) -> None:
    unknown = codes - PERMISSIONS.keys()
    if unknown:
        raise BadRequest("Unknown permission codes", {"unknown": sorted(unknown)})
    beyond = codes - p.permissions
    if beyond:
        # Nobody can hand out more than they have.
        raise Forbidden("Cannot grant permissions you do not have", {"permissions": sorted(beyond)})


def create_tag(
    db: Session,
    p: Principal,
    *,
    code: str,
    name: str,
    color: str,
    all_locations: bool,
    permissions: set[str],
) -> Tag:
    p.require("core.tag.manage")
    _check_grantable(p, permissions)
    if all_locations and p.location_ids is not None:
        raise Forbidden("Cannot grant all-location access you do not have")
    if db.scalar(select(Tag.id).where(Tag.company_id == p.company_id, Tag.code == code)):
        raise Conflict("Tag code already exists", {"code": code})
    tag = Tag(
        company_id=p.company_id,
        code=code,
        name=name,
        color=color,
        all_locations=all_locations,
        is_system=False,
        created_by=p.user_id,
        updated_by=p.user_id,
        permissions=list(db.scalars(select(Permission).where(Permission.code.in_(permissions)))),
    )
    db.add(tag)
    db.commit()
    return tag


def update_tag(
    db: Session,
    p: Principal,
    tag_id: uuid.UUID,
    *,
    name: str | None,
    color: str | None,
    permissions: set[str] | None,
) -> Tag:
    p.require("core.tag.manage")
    tag = _get_tag(db, p, tag_id)
    if name is not None:
        tag.name = name
    if color is not None:
        tag.color = color
    if permissions is not None:
        if tag.is_system:
            raise BadRequest("Permissions of built-in tags are fixed; create a custom tag")
        _check_grantable(p, permissions | {pp.code for pp in tag.permissions})
        tag.permissions = list(
            db.scalars(select(Permission).where(Permission.code.in_(permissions)))
        )
    tag.updated_by = p.user_id
    db.commit()
    return tag


# --- employees ----------------------------------------------------------------------------


def list_users(db: Session, p: Principal) -> list[AppUser]:
    p.require("core.user.read")
    return list(
        db.scalars(
            select(AppUser)
            .where(AppUser.company_id == p.company_id)
            .options(selectinload(AppUser.tags), selectinload(AppUser.locations))
            .order_by(AppUser.username)
        )
    )


def _get_user(db: Session, p: Principal, user_id: uuid.UUID) -> AppUser:
    user = db.get(AppUser, user_id)
    if user is None or user.company_id != p.company_id:
        raise NotFound("User not found")
    return user


def _load_tags(db: Session, p: Principal, tag_ids: set[uuid.UUID]) -> list[Tag]:
    tags = list(
        db.scalars(
            select(Tag)
            .where(Tag.company_id == p.company_id, Tag.id.in_(tag_ids))
            .options(selectinload(Tag.permissions))
        )
    )
    if len(tags) != len(tag_ids):
        raise BadRequest("Unknown tag ids")
    return tags


def _load_locations(db: Session, p: Principal, location_ids: set[uuid.UUID]) -> list[Location]:
    locs = list(
        db.scalars(
            select(Location).where(
                Location.company_id == p.company_id, Location.id.in_(location_ids)
            )
        )
    )
    if len(locs) != len(location_ids):
        raise BadRequest("Unknown location ids")
    return locs


def _check_tag_change(p: Principal, before: list[Tag], after: list[Tag]) -> None:
    changed = {t.id: t for t in before + after if (t in before) != (t in after)}
    codes = {pp.code for t in changed.values() for pp in t.permissions}
    _check_grantable(p, codes)
    if any(t.all_locations for t in changed.values()) and p.location_ids is not None:
        raise Forbidden("Cannot grant all-location access you do not have")


def _assert_owner_remains(db: Session, company_id: uuid.UUID) -> None:
    n = db.scalar(
        select(func.count())
        .select_from(AppUser)
        .join(user_tag, user_tag.c.user_id == AppUser.id)
        .join(Tag, Tag.id == user_tag.c.tag_id)
        .where(
            AppUser.company_id == company_id,
            AppUser.is_active.is_(True),
            Tag.code == OWNER_TAG,
        )
    )
    if not n:
        raise Conflict("The company must keep at least one active owner")


_USERNAME = re.compile(r"^[a-z0-9_.-]{3,50}$")


def create_user(
    db: Session,
    p: Principal,
    *,
    username: str,
    display_name: str,
    password: str,
    language: str,
    tag_ids: set[uuid.UUID],
    location_ids: set[uuid.UUID],
) -> AppUser:
    p.require("core.user.manage")
    if tag_ids:
        p.require("core.tag.assign")
    if not _USERNAME.match(username):
        raise BadRequest("Username: 3-50 chars, lowercase letters, digits, _ . -")
    if len(password) < MIN_PASSWORD:
        raise BadRequest(f"Password must be at least {MIN_PASSWORD} characters")
    if db.scalar(select(AppUser.id).where(AppUser.username == username)):
        raise Conflict("Username already taken", {"username": username})
    tags = _load_tags(db, p, tag_ids)
    _check_tag_change(p, [], tags)
    user = AppUser(
        company_id=p.company_id,
        username=username,
        display_name=display_name,
        password_hash=hash_password(password),
        language=language,
        tags=tags,
        locations=_load_locations(db, p, location_ids),
        created_by=p.user_id,
        updated_by=p.user_id,
    )
    db.add(user)
    db.commit()
    return user


def set_user_tags(
    db: Session, p: Principal, user_id: uuid.UUID, tag_ids: set[uuid.UUID]
) -> AppUser:
    p.require("core.tag.assign")
    user = _get_user(db, p, user_id)
    new_tags = _load_tags(db, p, tag_ids)
    _check_tag_change(p, list(user.tags), new_tags)
    user.tags = new_tags
    user.updated_by = p.user_id
    db.flush()
    _assert_owner_remains(db, p.company_id)
    db.commit()
    return user


def update_user(
    db: Session,
    p: Principal,
    user_id: uuid.UUID,
    *,
    display_name: str | None,
    language: str | None,
    is_active: bool | None,
    location_ids: set[uuid.UUID] | None,
    password: str | None,
) -> AppUser:
    p.require("core.user.manage")
    user = _get_user(db, p, user_id)
    if display_name is not None:
        user.display_name = display_name
    if language is not None:
        user.language = language
    if location_ids is not None:
        user.locations = _load_locations(db, p, location_ids)
    if password is not None:
        if len(password) < MIN_PASSWORD:
            raise BadRequest(f"Password must be at least {MIN_PASSWORD} characters")
        user.password_hash = hash_password(password)
        revoke_sessions(db, user_id=user.id)  # a reset signs the user out everywhere
    if is_active is not None and is_active != user.is_active:
        user.is_active = is_active
        if not is_active:
            revoke_sessions(db, user_id=user.id)
    user.updated_by = p.user_id
    db.flush()
    _assert_owner_remains(db, p.company_id)
    db.commit()
    return user


# --- devices ------------------------------------------------------------------------------


def list_devices(db: Session, p: Principal) -> list[Device]:
    p.require("core.device.manage")
    return list(
        db.scalars(
            select(Device)
            .where(Device.company_id == p.company_id)
            .order_by(Device.last_seen_at.desc())
        )
    )


def _get_device(db: Session, p: Principal, device_id: uuid.UUID) -> Device:
    device = db.get(Device, device_id)
    if device is None or device.company_id != p.company_id:
        raise NotFound("Device not found")
    return device


def set_device_edit(
    db: Session, p: Principal, device_id: uuid.UUID, can_edit: bool
) -> Device:
    p.require("core.device.manage")
    device = _get_device(db, p, device_id)
    device.can_edit = can_edit
    device.authorized_by = p.user_id if can_edit else None
    device.authorized_at = datetime.now(UTC) if can_edit else None
    device.updated_by = p.user_id
    db.commit()
    return device


def rename_device(db: Session, p: Principal, device_id: uuid.UUID, name: str) -> Device:
    p.require("core.device.manage")
    device = _get_device(db, p, device_id)
    device.name = name
    device.updated_by = p.user_id
    db.commit()
    return device


def sign_out_device(db: Session, p: Principal, device_id: uuid.UUID) -> int:
    p.require("core.device.manage")
    _get_device(db, p, device_id)
    n = revoke_sessions(db, device_id=device_id)
    db.commit()
    return n
