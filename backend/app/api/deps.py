from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.errors import Unauthenticated
from app.core.security import decode_access_token
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
    claims = decode_access_token(creds.credentials)
    principal = load_principal(db, claims.user_id, claims.session_id) if claims else None
    if principal is None:
        raise Unauthenticated("Token invalid or expired")
    return principal


CurrentUser = Annotated[Principal, Depends(get_principal)]
