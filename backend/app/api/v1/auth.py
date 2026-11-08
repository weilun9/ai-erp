"""Login with "remember this device".

Two HttpOnly cookies, both scoped to /api/v1/auth so they are never sent elsewhere:
- device_token: identifies this computer; kept across logouts.
  Only computers the owner authorized may sign in. A correct password on a new computer
  registers it as pending and returns 403 DEVICE_NOT_AUTHORIZED; once the owner
  authorizes it, the same login succeeds.
- session_token: the signed-in session; with remember=true it never expires server-side
  and the cookie is renewed on every refresh, so the user stays signed in.
The short-lived access JWT (Authorization: Bearer) is what other endpoints accept.
"""

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Cookie, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.api.deps import DB
from app.core.config import get_settings
from app.core.errors import DeviceNotAuthorized, Unauthenticated
from app.core.security import create_access_token
from app.models import Device
from app.services.auth import (
    authenticate,
    create_session,
    find_session_by_token,
    load_principal,
    resolve_device,
    revoke_sessions,
)

router = APIRouter(prefix="/auth", tags=["auth"])

DEVICE_COOKIE = "device_token"
SESSION_COOKIE = "session_token"
COOKIE_PATH = "/api/v1/auth"
# Browsers cap cookie lifetime at ~400 days; it is re-issued on every refresh.
LONG_COOKIE_SECONDS = 400 * 24 * 3600


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=1, max_length=200)
    remember: bool = True
    device_name: str | None = Field(default=None, max_length=100)


class DeviceOut(BaseModel):
    id: uuid.UUID
    name: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    device: DeviceOut


def _set_cookie(response: Response, name: str, value: str, persistent: bool) -> None:
    response.set_cookie(
        name,
        value,
        max_age=LONG_COOKIE_SECONDS if persistent else None,
        httponly=True,
        secure=not get_settings().is_dev,
        samesite="strict",
        path=COOKIE_PATH,
    )


def _token_out(user_id, session_id, device: Device) -> TokenOut:
    access, ttl = create_access_token(user_id, session_id)
    return TokenOut(
        access_token=access,
        expires_in=ttl,
        device=DeviceOut(id=device.id, name=device.name),
    )


@router.post("/login", response_model=TokenOut)
def login(
    body: LoginIn,
    request: Request,
    response: Response,
    db: DB,
    device_token: Annotated[str | None, Cookie(alias=DEVICE_COOKIE)] = None,
) -> TokenOut | JSONResponse:
    user = authenticate(db, body.username, body.password)
    if user is None:
        # Same message for unknown user, wrong password and disabled account.
        raise Unauthenticated("Invalid username or password")
    device, raw_device = resolve_device(
        db,
        company_id=user.company_id,
        device_token=device_token,
        name=body.device_name,
        user_agent=request.headers.get("user-agent"),
        ip=request.client.host if request.client else None,
    )
    if not device.is_authorized:
        db.commit()  # keep the pending device so the owner can find and approve it
        err = DeviceNotAuthorized(
            "This computer is not authorized. Ask the owner to approve it, then log in again.",
            {"device_id": str(device.id), "device_name": device.name},
        )
        resp = JSONResponse(
            status_code=err.status_code,
            content={"error": {"code": err.code, "message": err.message, "details": err.details}},
        )
        _set_cookie(resp, DEVICE_COOKIE, raw_device, persistent=True)
        return resp
    session, raw_session = create_session(db, user, device, body.remember)
    db.commit()
    _set_cookie(response, DEVICE_COOKIE, raw_device, persistent=True)
    _set_cookie(response, SESSION_COOKIE, raw_session, persistent=body.remember)
    return _token_out(user.id, session.id, device)


@router.post("/refresh")
def refresh(
    response: Response,
    db: DB,
    session_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    device_token: Annotated[str | None, Cookie(alias=DEVICE_COOKIE)] = None,
) -> TokenOut:
    session = find_session_by_token(db, session_token) if session_token else None
    principal = load_principal(db, session.user_id, session.id) if session else None
    if session is None or principal is None:
        raise Unauthenticated("Session ended, please log in again")
    session.last_used_at = datetime.now(UTC)
    device = db.get(Device, session.device_id)
    db.commit()
    # Slide both cookies forward so a remembered device stays signed in indefinitely.
    if session.remember:
        _set_cookie(response, SESSION_COOKIE, session_token, persistent=True)
    if device_token:
        _set_cookie(response, DEVICE_COOKIE, device_token, persistent=True)
    return _token_out(session.user_id, session.id, device)


@router.post("/logout", status_code=204)
def logout(
    response: Response,
    db: DB,
    session_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> Response:
    session = find_session_by_token(db, session_token) if session_token else None
    if session is not None:
        revoke_sessions(db, session_id=session.id)
        db.commit()
    # The device cookie stays: the computer is still remembered and still authorized.
    response.delete_cookie(SESSION_COOKIE, path=COOKIE_PATH)
    response.status_code = 204
    return response
