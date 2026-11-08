"""Authentication, devices, sessions and the Principal.

Every entry point (web API, AI butler, MCP) builds the same Principal and passes it
to the service layer, so permission checks live in one place.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from app.core.errors import Forbidden
from app.core.security import hash_token, new_opaque_token, verify_password
from app.models import AppUser, Device, Location, Tag, UserSession

# A session without "remember this device" ends after this long.
SHORT_SESSION = timedelta(hours=12)


@dataclass(frozen=True)
class Principal:
    user_id: uuid.UUID
    company_id: uuid.UUID
    username: str
    display_name: str
    language: str
    tags: frozenset[str]
    permissions: frozenset[str]
    # None means every location of the company.
    location_ids: frozenset[uuid.UUID] | None
    session_id: uuid.UUID
    device_id: uuid.UUID
    device_name: str

    def has(self, code: str) -> bool:
        return code in self.permissions

    def require(self, *codes: str) -> None:
        missing = [c for c in codes if c not in self.permissions]
        if missing:
            raise Forbidden("Missing permission", {"required": missing})

    def can_see_location(self, location_id: uuid.UUID) -> bool:
        return self.location_ids is None or location_id in self.location_ids


def _now() -> datetime:
    return datetime.now(UTC)


def authenticate(db: Session, username: str, password: str) -> AppUser | None:
    user = db.scalar(select(AppUser).where(AppUser.username == username))
    ok = verify_password(password, user.password_hash if user else None)
    if not ok or user is None or not user.is_active:
        return None
    return user


# --- devices & sessions ----------------------------------------------------------------


def resolve_device(
    db: Session,
    *,
    company_id: uuid.UUID,
    device_token: str | None,
    name: str | None,
    user_agent: str | None,
    ip: str | None,
) -> tuple[Device, str]:
    """Find the device behind the cookie, or register a new one as pending authorization.

    Returns (device, raw_token); the raw token is what goes back into the cookie.
    """
    device = None
    if device_token:
        device = db.scalar(select(Device).where(Device.token_hash == hash_token(device_token)))
        if device is not None and device.company_id != company_id:
            device = None  # same browser, other company: register separately
    if device is None:
        device_token = new_opaque_token()
        device = Device(
            company_id=company_id,
            name=(name or _guess_name(user_agent))[:100],
            token_hash=hash_token(device_token),
            is_authorized=False,
        )
        db.add(device)
    elif name:
        device.name = name[:100]
    device.user_agent = (user_agent or "")[:300] or None
    device.last_ip = ip
    device.last_seen_at = _now()
    db.flush()
    assert device_token is not None
    return device, device_token


def _guess_name(user_agent: str | None) -> str:
    ua = user_agent or ""
    os_name = next(
        (
            n
            for k, n in (
                ("Windows", "Windows"),
                ("Mac OS", "Mac"),
                ("Android", "Android"),
                ("iPhone", "iPhone"),
                ("iPad", "iPad"),
                ("Linux", "Linux"),
            )
            if k in ua
        ),
        "Unknown",
    )
    browser = next(
        (
            n
            for k, n in (
                ("Edg/", "Edge"),
                ("Chrome/", "Chrome"),
                ("Firefox/", "Firefox"),
                ("Safari/", "Safari"),
            )
            if k in ua
        ),
        "Browser",
    )
    return f"{browser} on {os_name}"


def create_session(
    db: Session, user: AppUser, device: Device, remember: bool
) -> tuple[UserSession, str]:
    token = new_opaque_token()
    session = UserSession(
        user_id=user.id,
        device_id=device.id,
        token_hash=hash_token(token),
        remember=remember,
        expires_at=None if remember else _now() + SHORT_SESSION,
    )
    db.add(session)
    db.flush()
    return session, token


def _active_session_filter():
    now = _now()
    return (
        UserSession.revoked_at.is_(None),
        (UserSession.expires_at.is_(None)) | (UserSession.expires_at > now),
    )


def find_session_by_token(db: Session, token: str) -> UserSession | None:
    return db.scalar(
        select(UserSession).where(
            UserSession.token_hash == hash_token(token), *_active_session_filter()
        )
    )


def revoke_sessions(
    db: Session,
    *,
    session_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    device_id: uuid.UUID | None = None,
) -> int:
    stmt = update(UserSession).where(UserSession.revoked_at.is_(None))
    if session_id:
        stmt = stmt.where(UserSession.id == session_id)
    if user_id:
        stmt = stmt.where(UserSession.user_id == user_id)
    if device_id:
        stmt = stmt.where(UserSession.device_id == device_id)
    return db.execute(stmt.values(revoked_at=_now())).rowcount


# --- principal ---------------------------------------------------------------------------


def load_principal(db: Session, user_id: uuid.UUID, session_id: uuid.UUID) -> Principal | None:
    """Rebuilt on every request, so revoking a session or a device takes effect at once."""
    row = db.execute(
        select(UserSession, Device)
        .join(Device, Device.id == UserSession.device_id)
        .where(
            UserSession.id == session_id,
            UserSession.user_id == user_id,
            Device.is_authorized.is_(True),  # de-authorizing a computer locks it out at once
            *_active_session_filter(),
        )
    ).first()
    if row is None:
        return None
    _, device = row

    user = db.scalar(
        select(AppUser)
        .where(AppUser.id == user_id, AppUser.is_active.is_(True))
        .options(
            selectinload(AppUser.tags).selectinload(Tag.permissions),
            selectinload(AppUser.locations),
        )
    )
    if user is None or device.company_id != user.company_id:
        return None

    permissions = frozenset(p.code for t in user.tags for p in t.permissions)
    location_ids: frozenset[uuid.UUID] | None
    if any(t.all_locations for t in user.tags):
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
        tags=frozenset(t.code for t in user.tags),
        permissions=permissions,
        location_ids=location_ids,
        session_id=session_id,
        device_id=device.id,
        device_name=device.name,
    )


def visible_locations(db: Session, principal: Principal) -> list[Location]:
    stmt = select(Location).where(Location.company_id == principal.company_id)
    if principal.location_ids is not None:
        stmt = stmt.where(Location.id.in_(principal.location_ids))
    return list(db.scalars(stmt.order_by(Location.code)))
