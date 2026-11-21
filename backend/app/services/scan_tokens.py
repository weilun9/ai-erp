"""Tokens for scanning apps (the Doc2Excel desktop app) that push papers into the inbox.

Same rules as MCP tokens (mcp_tokens.py): a remembered session with purpose SCAN on the
computer that asked for it, so it can only be made from an approved computer by a logged-in
user who may create purchase documents; de-authorizing that computer, signing it out or
deactivating the user kills it at once; it is never accepted as a web login; and it can do
one thing only - upload a paper into the scan queue (see /inbox/remote). The raw token is
shown once; only its SHA-256 is stored.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import NotFound
from app.models import AppUser, Device, UserSession
from app.services import audit
from app.services.auth import Principal, create_session, find_session_by_token, load_principal

PURPOSE = "SCAN"
PERMISSION = "pur.doc.create"


def issue(db: Session, p: Principal) -> tuple[UserSession, str]:
    p.require(PERMISSION)
    user = db.get(AppUser, p.user_id)
    device = db.get(Device, p.device_id)
    session, token = create_session(db, user, device, remember=True, purpose=PURPOSE)
    audit.record(db, p, "scan_token_issue", "core.user_session", session.id)
    db.commit()
    return session, token


def active(db: Session, p: Principal) -> list[UserSession]:
    return list(
        db.scalars(
            select(UserSession)
            .where(
                UserSession.user_id == p.user_id,
                UserSession.purpose == PURPOSE,
                UserSession.revoked_at.is_(None),
            )
            .order_by(UserSession.created_at.desc())
        )
    )


def revoke(db: Session, p: Principal, session_id: uuid.UUID) -> None:
    from app.services.auth import revoke_sessions

    s = db.get(UserSession, session_id)
    if s is None or s.user_id != p.user_id or s.purpose != PURPOSE:
        raise NotFound("Token not found")
    revoke_sessions(db, session_id=session_id)
    audit.record(db, p, "scan_token_revoke", "core.user_session", session_id)
    db.commit()


def principal(db: Session, token: str) -> Principal | None:
    """Who this scan token acts for right now (None = revoked, locked out or unknown)."""
    session = find_session_by_token(db, token, purpose=PURPOSE)
    if session is None:
        return None
    p = load_principal(db, session.user_id, session.id)
    if p is None or not p.has(PERMISSION):
        return None
    return p
