import pytest

from app.core.security import create_token
from tests.conftest import PASSWORD, login

pytestmark = pytest.mark.db


def test_login_returns_access_token_and_refresh_cookie(world, client) -> None:
    resp = client.post("/api/v1/auth/login", json={"username": "boss", "password": PASSWORD})
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 15 * 60
    cookie = resp.headers["set-cookie"]
    assert "refresh_token=" in cookie
    assert "HttpOnly" in cookie
    assert "Path=/api/v1/auth" in cookie


@pytest.mark.parametrize(
    "username,password",
    [("boss", "wrong-password"), ("nobody", PASSWORD), ("gone", PASSWORD)],
)
def test_login_failures_look_identical(world, client, username, password) -> None:
    resp = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 401
    assert resp.json() == {
        "error": {
            "code": "UNAUTHENTICATED",
            "message": "Invalid username or password",
            "details": {},
        }
    }


def test_login_validation_error_uses_envelope(world, client) -> None:
    resp = client.post("/api/v1/auth/login", json={"username": ""})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_me_requires_token(world, client) -> None:
    resp = client.get("/api/v1/me")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHENTICATED"


def test_me_rejects_refresh_token_as_bearer(world, client) -> None:
    from sqlalchemy import select

    from app.db.session import SessionLocal
    from app.models import AppUser

    with SessionLocal() as db:
        uid = db.scalar(select(AppUser.id).where(AppUser.username == "boss"))
    refresh, _ = create_token(uid, "refresh")
    resp = client.get("/api/v1/me", headers={"Authorization": f"Bearer {refresh}"})
    assert resp.status_code == 401


def test_me_for_owner(world, client) -> None:
    resp = client.get("/api/v1/me", headers=login(client, "boss"))
    assert resp.status_code == 200
    me = resp.json()
    assert me["roles"] == ["admin", "owner"]
    assert me["all_locations"] is True
    assert [loc["code"] for loc in me["locations"]] == ["HQ", "S2", "S3"]
    assert "pur.doc.post" in me["permissions"]


def test_me_for_cashier_is_narrow(world, client) -> None:
    me = client.get("/api/v1/me", headers=login(client, "cashier1")).json()
    assert me["all_locations"] is False
    assert [loc["code"] for loc in me["locations"]] == ["S2"]
    assert "field.cost.read" not in me["permissions"]
    assert "pos.sale.create" in me["permissions"]


def test_refresh_with_cookie_issues_new_access_token(world, client) -> None:
    login(client, "clerk1")  # TestClient keeps the refresh cookie
    resp = client.post("/api/v1/auth/refresh")
    assert resp.status_code == 200
    token = resp.json()["access_token"]
    me = client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert me.json()["username"] == "clerk1"


def test_refresh_without_cookie_fails(world, client) -> None:
    resp = client.post("/api/v1/auth/refresh")
    assert resp.status_code == 401


def test_logout_clears_refresh_cookie(world, client) -> None:
    login(client, "boss")
    assert client.post("/api/v1/auth/logout").status_code == 204
    assert client.post("/api/v1/auth/refresh").status_code == 401
