"""/audit: the audit trail - who changed what, when, from which computer (read only)."""

import uuid
from datetime import date, datetime, time, timedelta
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import String, cast, func, or_, select

from app.api.deps import DB, CurrentUser
from app.localization.my import TIMEZONE
from app.models import AppUser, Device, EventLog

router = APIRouter(prefix="/audit", tags=["audit"])
PERMISSION = "audit.read"


class EventOut(BaseModel):
    id: int
    at: datetime
    actor_type: str
    actor_id: uuid.UUID | None
    actor_name: str | None
    device_name: str | None
    action: str
    entity: str
    entity_id: str | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    request_id: str | None


class EventPage(BaseModel):
    items: list[EventOut]
    total: int


def _day_start(d: date) -> datetime:
    """Midnight of a shop day (Malaysian time), so 'today' means the shop's today."""
    return datetime.combine(d, time.min, tzinfo=ZoneInfo(TIMEZONE))


@router.get("/events")
def list_events(
    p: CurrentUser,
    db: DB,
    date_from: date | None = None,
    date_to: date | None = None,
    actor_id: uuid.UUID | None = None,
    actor_type: str | None = None,
    entity: Annotated[str | None, Query(max_length=40)] = None,  # "sal" or "sal.invoice"
    entity_id: Annotated[str | None, Query(max_length=64)] = None,
    action: Annotated[str | None, Query(max_length=40)] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,  # text inside the change
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> EventPage:
    """Newest first. Only this company's events; changes to secrets are never stored."""
    p.require(PERMISSION)
    where = [EventLog.company_id == p.company_id]
    if date_from:
        where.append(EventLog.at >= _day_start(date_from))
    if date_to:
        where.append(EventLog.at < _day_start(date_to + timedelta(days=1)))
    if actor_id:
        where.append(EventLog.actor_id == actor_id)
    if actor_type:
        where.append(EventLog.actor_type == actor_type)
    if entity:
        where.append(
            EventLog.entity == entity if "." in entity else EventLog.entity.startswith(f"{entity}.")
        )
    if entity_id:
        where.append(EventLog.entity_id == entity_id)
    if action:
        where.append(EventLog.action == action)
    if q:
        like = f"%{q.strip()}%"
        where.append(
            or_(
                cast(EventLog.after, String).ilike(like),
                cast(EventLog.before, String).ilike(like),
                EventLog.entity_id.ilike(like),
            )
        )
    total = db.scalar(select(func.count()).select_from(EventLog).where(*where)) or 0
    rows = db.execute(
        select(EventLog, AppUser.display_name, Device.name)
        .outerjoin(AppUser, AppUser.id == EventLog.actor_id)
        .outerjoin(Device, Device.id == EventLog.device_id)
        .where(*where)
        .order_by(EventLog.at.desc(), EventLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    items = [
        EventOut(
            id=e.id,
            at=e.at,
            actor_type=e.actor_type,
            actor_id=e.actor_id,
            actor_name=name,
            device_name=device,
            action=e.action,
            entity=e.entity,
            entity_id=e.entity_id,
            before=e.before,
            after=e.after,
            request_id=e.request_id,
        )
        for e, name, device in rows
    ]
    return EventPage(items=items, total=total)


@router.get("/facets")
def facets(p: CurrentUser, db: DB) -> dict[str, list[str]]:
    """The actions and record types that occur, for the screen's filters."""
    p.require(PERMISSION)
    mine = EventLog.company_id == p.company_id
    actions = db.scalars(select(EventLog.action).where(mine).distinct().order_by(EventLog.action))
    entities = db.scalars(select(EventLog.entity).where(mine).distinct().order_by(EventLog.entity))
    return {"actions": list(actions), "entities": list(entities)}
