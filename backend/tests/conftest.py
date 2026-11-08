import os
import uuid

# Must run before any app import so settings/engine point at the test database.
os.environ["POSTGRES_DB"] = "erp_test"
os.environ["APP_ENV"] = "test"

from dataclasses import dataclass  # noqa: E402
from pathlib import Path  # noqa: E402

import psycopg  # noqa: E402
import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select, text  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import AppUser, Company, Device, Location, Tag  # noqa: E402
from app.services.seed import bootstrap, sync_system_tags  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]
PASSWORD = "Test-password-123"


@pytest.fixture(scope="session")
def migrated() -> None:
    s = get_settings()
    with psycopg.connect(
        host=s.postgres_host,
        port=s.postgres_port,
        user=s.postgres_user,
        password=s.postgres_password,
        dbname="postgres",
        autocommit=True,
    ) as conn:
        if not conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (s.postgres_db,)
        ).fetchone():
            conn.execute(f'CREATE DATABASE "{s.postgres_db}"')
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")


@dataclass
class World:
    company_id: object
    hq_id: object
    s2_id: object
    s3_id: object
    other_company_loc_id: object


def _user(db, company, username, tag_codes, locations=(), active=True):
    tags = {t.code: t for t in db.scalars(select(Tag).where(Tag.company_id == company.id))}
    u = AppUser(
        company_id=company.id,
        username=username,
        display_name=username,
        password_hash=hash_password(PASSWORD),
        is_active=active,
        tags=[tags[c] for c in tag_codes],
        locations=list(locations),
    )
    db.add(u)
    return u


@pytest.fixture
def world(migrated) -> World:
    """Company T1 (HQ, S2, S3) with one user per tag, plus a second company T2."""
    with SessionLocal() as db:
        db.execute(text("TRUNCATE core.company, core.permission CASCADE"))
        db.commit()
        bootstrap(
            db,
            company_code="T1",
            company_name="Test One",
            hq_code="HQ",
            hq_name="Head Office",
            admin_username="boss",
            admin_password=PASSWORD,
        )
        t1 = db.scalar(select(Company).where(Company.code == "T1"))
        hq = db.scalar(select(Location).where(Location.code == "HQ"))
        s2 = Location(company_id=t1.id, code="S2", name="Store 2")
        s3 = Location(company_id=t1.id, code="S3", name="Store 3")
        db.add_all([s2, s3])
        db.flush()
        _user(db, t1, "clerk1", ["clerk"], [s2])
        _user(db, t1, "cashier1", ["cashier"], [s2])
        _user(db, t1, "acct", ["accountant"])
        _user(db, t1, "gone", ["owner"], active=False)

        t2 = Company(code="T2", name="Test Two")
        db.add(t2)
        db.flush()
        sync_system_tags(db, t2)
        x1 = Location(company_id=t2.id, code="X1", name="Other company store")
        db.add(x1)
        db.flush()
        _user(db, t2, "boss2", ["owner"])
        db.commit()
        return World(t1.id, hq.id, s2.id, s3.id, x1.id)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def approve_device(device_id: str) -> None:
    """What the owner does in the device list (bypassing the API for test setup)."""
    with SessionLocal() as db:
        db.get(Device, uuid.UUID(device_id)).is_authorized = True
        db.commit()


def login(
    client: TestClient,
    username: str,
    password: str = PASSWORD,
    *,
    remember: bool = True,
) -> dict[str, str]:
    """Log in on this client's "computer", approving it first if it is new."""
    body = {"username": username, "password": password, "remember": remember}
    resp = client.post("/api/v1/auth/login", json=body)
    if resp.status_code == 403:
        err = resp.json()["error"]
        assert err["code"] == "DEVICE_NOT_AUTHORIZED", resp.text
        approve_device(err["details"]["device_id"])
        resp = client.post("/api/v1/auth/login", json=body)
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}
