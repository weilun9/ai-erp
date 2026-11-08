"""Sync the permission registry into the DB and bootstrap a company. Idempotent."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.permissions import PERMISSIONS, TAG_TEMPLATES
from app.core.security import hash_password
from app.models import AppUser, Company, Location, Permission, Tag


def sync_permissions(db: Session) -> None:
    existing = {p.code: p for p in db.scalars(select(Permission))}
    for code, desc in PERMISSIONS.items():
        if code in existing:
            existing[code].description = desc
        else:
            db.add(Permission(code=code, description=desc))
    # Codes removed from the registry are deleted (cascades from tag_permission).
    for code, perm in existing.items():
        if code not in PERMISSIONS:
            db.delete(perm)
    db.flush()


def sync_system_tags(db: Session, company: Company) -> dict[str, Tag]:
    """Create missing system tags and reset their permissions to the template.

    Name and color are only set on creation, so the owner can rename / recolor them.
    """
    perms = {p.code: p for p in db.scalars(select(Permission))}
    tags = {t.code: t for t in db.scalars(select(Tag).where(Tag.company_id == company.id))}
    for tpl in TAG_TEMPLATES.values():
        tag = tags.get(tpl.code)
        if tag is None:
            tag = Tag(
                company_id=company.id,
                code=tpl.code,
                name=tpl.name,
                color=tpl.color,
                is_system=True,
            )
            db.add(tag)
            tags[tpl.code] = tag
        if tag.is_system:
            tag.all_locations = tpl.all_locations
            tag.permissions = [perms[c] for c in sorted(tpl.permissions)]
    db.flush()
    return tags


def bootstrap(
    db: Session,
    *,
    company_code: str,
    company_name: str,
    hq_code: str,
    hq_name: str,
    admin_username: str,
    admin_password: str,
    admin_display_name: str = "Owner",
) -> AppUser:
    """Create the company, an HQ location, system tags and an owner user."""
    sync_permissions(db)

    company = db.scalar(select(Company).where(Company.code == company_code))
    if company is None:
        company = Company(code=company_code, name=company_name)
        db.add(company)
        db.flush()
    tags = sync_system_tags(db, company)

    hq = db.scalar(
        select(Location).where(Location.company_id == company.id, Location.code == hq_code)
    )
    if hq is None:
        hq = Location(company_id=company.id, code=hq_code, name=hq_name, type="STORE")
        db.add(hq)
        db.flush()

    user = db.scalar(select(AppUser).where(AppUser.username == admin_username))
    if user is None:
        user = AppUser(
            company_id=company.id,
            username=admin_username,
            display_name=admin_display_name,
            password_hash=hash_password(admin_password),
            default_location_id=hq.id,
            tags=[tags["owner"]],
        )
        db.add(user)
    db.commit()
    return user
