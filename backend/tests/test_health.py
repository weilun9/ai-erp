import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db.base import SCHEMAS
from app.db.session import engine
from app.main import app

client = TestClient(app)


def test_healthz() -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.db
def test_healthz_db_reports_pgvector() -> None:
    resp = client.get("/healthz/db")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "database": "up", "pgvector": True}


@pytest.mark.db
def test_module_schemas_exist() -> None:
    with engine.connect() as conn:
        found = set(
            conn.execute(text("SELECT schema_name FROM information_schema.schemata")).scalars()
        )
    assert set(SCHEMAS) <= found
