from typing import Annotated

from fastapi import APIRouter, Cookie, Response
from pydantic import BaseModel, Field

from app.api.deps import DB
from app.core.config import get_settings
from app.core.errors import Unauthenticated
from app.core.security import create_token, decode_token
from app.services.auth import authenticate, load_principal

router = APIRouter(prefix="/auth", tags=["auth"])

REFRESH_COOKIE = "refresh_token"
REFRESH_PATH = "/api/v1/auth"


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=1, max_length=200)


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


def _issue(response: Response, user_id) -> TokenOut:
    access, access_ttl = create_token(user_id, "access")
    refresh, refresh_ttl = create_token(user_id, "refresh")
    response.set_cookie(
        REFRESH_COOKIE,
        refresh,
        max_age=refresh_ttl,
        httponly=True,
        secure=not get_settings().is_dev,
        samesite="strict",
        path=REFRESH_PATH,
    )
    return TokenOut(access_token=access, expires_in=access_ttl)


@router.post("/login")
def login(body: LoginIn, response: Response, db: DB) -> TokenOut:
    user = authenticate(db, body.username, body.password)
    if user is None:
        # Same message for unknown user, wrong password and disabled account.
        raise Unauthenticated("Invalid username or password")
    return _issue(response, user.id)


@router.post("/refresh")
def refresh(
    response: Response,
    db: DB,
    refresh_token: Annotated[str | None, Cookie(alias=REFRESH_COOKIE)] = None,
) -> TokenOut:
    user_id = decode_token(refresh_token, "refresh") if refresh_token else None
    if user_id is None or load_principal(db, user_id) is None:
        raise Unauthenticated("Refresh token invalid or expired")
    return _issue(response, user_id)


@router.post("/logout", status_code=204)
def logout(response: Response) -> Response:
    response.delete_cookie(REFRESH_COOKIE, path=REFRESH_PATH)
    response.status_code = 204
    return response
