from collections.abc import Callable
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.errors import Unauthenticated
from app.core.security import decode_token
from app.db.session import get_db
from app.services.auth import Principal, load_principal

_bearer = HTTPBearer(auto_error=False)

DB = Annotated[Session, Depends(get_db)]


def get_principal(
    db: DB,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    if creds is None:
        raise Unauthenticated("Not logged in")
    user_id = decode_token(creds.credentials, "access")
    principal = load_principal(db, user_id) if user_id else None
    if principal is None:
        raise Unauthenticated("Token invalid or expired")
    return principal


CurrentUser = Annotated[Principal, Depends(get_principal)]


def require(*codes: str) -> Callable[[Principal], Principal]:
    """Route dependency: `p: Annotated[Principal, Depends(require("x.y.z"))]`."""

    def _check(principal: CurrentUser) -> Principal:
        principal.require(*codes)
        return principal

    return _check
