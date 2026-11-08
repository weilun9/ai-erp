"""Sync the permission registry into the DB and bootstrap a company. Idempotent."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.permissions import PERMISSIONS, ROLE_TEMPLATES
from app.core.security import hash_password
from app.models import AppUser, Company, Location, Permission, Role


def sync_permissions(db: Session) -> None:
    existing = {p.code: p for p in db.scalars(select(Permission))}
    for code, desc in PERMISSIONS.items():
        if code in existing:
            existing[code].description = desc
        else:
            db.add(Permission(code=code, description=desc))
    # Codes removed from the registry are deleted (cascades from role_permission).
    for code, perm in existing.items():
        if code not in PERMISSIONS:
            db.delete(perm)
    db.flush()


def sync_system_roles(db: Session, company: Company) -> dict[str, Role]:
    perms = {p.code: p for p in db.scalars(select(Permission))}
    roles = {
        r.code: r
        for r in db.scalars(select(Role).where(Role.company_id == company.id))
    }
    for tpl in ROLE_TEMPLATES.values():
        role = roles.get(tpl.code)
        if role is None:
            role = Role(company_id=company.id, code=tpl.code, is_system=True)
            db.add(role)
            roles[tpl.code] = role
        if role.is_system:
            role.name = tpl.name
            role.all_locations = tpl.all_locations
            role.permissions = [perms[c] for c in sorted(tpl.permissions)]
    db.flush()
    return roles


def bootstrap(
    db: Session,
    *,
    company_code: str,
    company_name: str,
    hq_code: str,
    hq_name: str,
    admin_username: str,
    admin_password: str,
    admin_display_name: str = "Administrator",
) -> AppUser:
    """Create the company, an HQ location, system roles and an owner+admin user."""
    sync_permissions(db)

    company = db.scalar(select(Company).where(Company.code == company_code))
    if company is None:
        company = Company(code=company_code, name=company_name)
        db.add(company)
        db.flush()
    roles = sync_system_roles(db, company)

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
            roles=[roles["owner"], roles["admin"]],
        )
        db.add(user)
    db.commit()
    return user
