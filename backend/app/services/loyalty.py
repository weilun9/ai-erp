"""Loyalty members and points.

A member earns LOYALTY_EARN_PER_RM points per ringgit paid (not counting what was paid
with points) and spends points as a till payment, each point worth LOYALTY_POINT_VALUE
ringgit. A return takes back the points its share of the sale earned and gives back the
points it was paid with. Every change is a sal.points_move row; member.points is the sum.
"""

import uuid
from decimal import ROUND_FLOOR, Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import BadRequest, BusinessRule, Conflict, NotFound, VersionConflict
from app.models import Member, PointsMove
from app.services import audit
from app.services.auth import Principal

PERM = "pos.member.manage"


def earn_rate() -> Decimal:
    return Decimal(str(get_settings().loyalty_earn_per_rm))


def point_value() -> Decimal:
    return Decimal(str(get_settings().loyalty_point_value))


def points_for(amount: Decimal) -> int:
    """Points needed to pay `amount` (must be a whole number of points)."""
    pts = amount / point_value()
    if pts != pts.to_integral_value():
        raise BusinessRule("Points pay in steps of one point", {"point_value": str(point_value())})
    return int(pts)


def earned(paid: Decimal) -> int:
    return int((paid * earn_rate()).to_integral_value(rounding=ROUND_FLOOR)) if paid > 0 else 0


def find(db: Session, p: Principal, card_or_phone: str, *, lock: bool = False) -> Member:
    p.require("pos.sale.create")
    key = card_or_phone.strip()
    stmt = select(Member).where(
        Member.company_id == p.company_id,
        Member.is_active.is_(True),
        or_(Member.card_no == key, Member.phone == key),
    )
    if lock:
        stmt = stmt.with_for_update()
    rows = db.scalars(stmt).all()
    if not rows:
        raise NotFound("No member with that card or phone number")
    if len(rows) > 1:
        raise BadRequest("Several members share that phone number; scan the card")
    return rows[0]


def get(db: Session, p: Principal, member_id: uuid.UUID, *, lock: bool = False) -> Member:
    stmt = select(Member).where(Member.id == member_id, Member.company_id == p.company_id)
    m = db.scalar(stmt.with_for_update() if lock else stmt)
    if m is None:
        raise NotFound("Member not found")
    return m


def list_members(db: Session, p: Principal, q: str | None) -> list[Member]:
    p.require("pos.sale.create")
    stmt = select(Member).where(Member.company_id == p.company_id)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(Member.card_no.ilike(like), Member.name.ilike(like), Member.phone.ilike(like))
        )
    return list(db.scalars(stmt.order_by(Member.joined_at.desc()).limit(200)))


def create(db: Session, p: Principal, **v: Any) -> Member:
    p.require(PERM)
    v["card_no"] = v["card_no"].strip()
    if db.scalar(
        select(Member.id).where(Member.company_id == p.company_id, Member.card_no == v["card_no"])
    ):
        raise Conflict("That card number is taken", {"card_no": v["card_no"]})
    m = Member(company_id=p.company_id, **v)
    db.add(m)
    db.flush()
    audit.record(db, p, "create", "sal.member", m.id, after=audit.snapshot(m))
    db.commit()
    return m


def update(db: Session, p: Principal, member_id: uuid.UUID, version: int, **v: Any) -> Member:
    p.require(PERM)
    m = get(db, p, member_id)
    if m.version != version:
        raise VersionConflict("This member was changed by someone else; reload")
    before = audit.snapshot(m)
    for k, x in v.items():
        setattr(m, k, x)
    m.version += 1
    db.flush()
    audit.record(db, p, "update", "sal.member", m.id, before=before, after=audit.snapshot(m))
    db.commit()
    return m


def move(
    db: Session,
    p: Principal,
    m: Member,
    points: int,
    kind: str,
    *,
    sale_id: uuid.UUID | None = None,
    note: str | None = None,
) -> None:
    """Add (or take) points; the caller holds the member row lock and commits."""
    if not points:
        return
    m.points += points
    db.add(
        PointsMove(
            company_id=p.company_id,
            member_id=m.id,
            sale_id=sale_id,
            kind=kind,
            points=points,
            balance=m.points,
            note=note,
            created_by=p.user_id,
        )
    )


def adjust(db: Session, p: Principal, member_id: uuid.UUID, points: int, note: str) -> Member:
    p.require(PERM)
    m = get(db, p, member_id, lock=True)
    if m.points + points < 0:
        raise BusinessRule("The member does not have that many points", {"points": m.points})
    move(db, p, m, points, "ADJUST", note=note)
    audit.record(db, p, "points_adjust", "sal.member", m.id, after={"points": points, "note": note})
    db.commit()
    return m


def history(db: Session, p: Principal, member_id: uuid.UUID) -> list[PointsMove]:
    p.require("pos.sale.create")
    get(db, p, member_id)
    return list(
        db.scalars(
            select(PointsMove)
            .where(PointsMove.member_id == member_id)
            .order_by(PointsMove.id.desc())
            .limit(200)
        )
    )
