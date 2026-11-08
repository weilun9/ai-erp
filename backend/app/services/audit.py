import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.core.request_context import request_id_var
from app.models import EventLog
from app.services.auth import Principal

_SKIP = {"password_hash", "token_hash", "created_at", "updated_at", "created_by", "updated_by"}


def _jsonable(v: Any) -> Any:
    if isinstance(v, uuid.UUID | Decimal):
        return str(v)
    if isinstance(v, datetime | date):
        return v.isoformat()
    if isinstance(v, list):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    return v


def snapshot(obj: Any, **extra: Any) -> dict[str, Any]:
    """Column values of an ORM object (secrets excluded), plus extra keys."""
    data = {
        c.key: getattr(obj, c.key) for c in inspect(obj).mapper.column_attrs if c.key not in _SKIP
    }
    data.update(extra)
    return _jsonable(data)


def record(
    db: Session,
    p: Principal,
    action: str,
    entity: str,
    entity_id: Any,
    *,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    actor_type: str = "user",
) -> None:
    """Add an audit event to the current transaction (committed with the change)."""
    before = _jsonable(before) if before is not None else None
    after = _jsonable(after) if after is not None else None
    if before is not None and after is not None:
        changed = {k for k in after if before.get(k) != after.get(k)}
        before = {k: before.get(k) for k in changed}
        after = {k: after[k] for k in changed}
        if not changed:
            return
    db.add(
        EventLog(
            company_id=p.company_id,
            actor_type=actor_type,
            actor_id=p.user_id,
            device_id=p.device_id,
            action=action,
            entity=entity,
            entity_id=str(entity_id) if entity_id is not None else None,
            before=before,
            after=after,
            request_id=request_id_var.get(),
        )
    )
