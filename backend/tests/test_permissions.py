import pytest
from sqlalchemy import func, select

from app.core.permissions import PERMISSIONS, TAG_TEMPLATES
from app.db.session import SessionLocal
from app.models import Company, Permission, Tag
from app.services.seed import sync_permissions, sync_system_tags


def test_tag_templates_match_design_doc() -> None:
    t = TAG_TEMPLATES
    assert set(t) == {"owner", "clerk", "accountant", "cashier", "admin"}
    # Cashiers never see cost price.
    assert "field.cost.read" not in t["cashier"].permissions
    # Accountants only report; they do not create or post documents, nor handle issues.
    assert not {c for c in t["accountant"].permissions if c.endswith((".create", ".post"))}
    assert "core.issue.handle" not in t["accountant"].permissions
    # Clerks and owners handle problem documents.
    assert "core.issue.handle" in t["clerk"].permissions
    assert "core.issue.handle" in t["owner"].permissions
    # Data scope.
    assert not t["clerk"].all_locations and not t["cashier"].all_locations
    assert t["owner"].all_locations and t["accountant"].all_locations
    assert t["owner"].permissions == frozenset(PERMISSIONS)


@pytest.mark.db
def test_seed_is_idempotent(world) -> None:
    with SessionLocal() as db:
        company = db.scalar(select(Company).where(Company.code == "T1"))
        for _ in range(2):
            sync_permissions(db)
            sync_system_tags(db, company)
        db.commit()
        assert db.scalar(select(func.count()).select_from(Permission)) == len(PERMISSIONS)
        tags = db.scalars(select(Tag).where(Tag.company_id == company.id)).all()
        assert len(tags) == len(TAG_TEMPLATES)
        owner = next(t for t in tags if t.code == "owner")
        assert {p.code for p in owner.permissions} == set(PERMISSIONS)


def test_write_detection() -> None:
    from app.core.permissions import is_write

    assert not is_write("md.item.read")
    assert not is_write("report.run")
    assert not is_write("ai.chat.use")
    assert is_write("pur.doc.post")
    assert is_write("core.tag.assign")
    assert is_write("pos.sale.create")
