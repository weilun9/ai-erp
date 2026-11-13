from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.session import SessionLocal
from app.main import app
from app.models import Device, UserSession
from tests.conftest import PASSWORD, approve_device, login

pytestmark = pytest.mark.db


def _raw_login(client, username="boss", **extra):
    return client.post(
        "/api/v1/auth/login", json={"username": username, "password": PASSWORD, **extra}
    )


def _login(client, username="boss", **extra):
    """Raw login on an approved computer (approves it on first use)."""
    resp = _raw_login(client, username, **extra)
    if resp.status_code == 403:
        approve_device(resp.json()["error"]["details"]["device_id"])
        resp = _raw_login(client, username, **extra)
    return resp


def test_new_computer_cannot_log_in_until_owner_approves(world, client) -> None:
    resp = _raw_login(client, device_name="Front desk PC")
    assert resp.status_code == 403
    err = resp.json()["error"]
    assert err["code"] == "DEVICE_NOT_AUTHORIZED"
    assert err["details"]["device_name"] == "Front desk PC"
    cookies = resp.headers.get_list("set-cookie")
    assert any(c.startswith("device_token=") for c in cookies)  # remembered as pending
    assert not any(c.startswith("session_token=") for c in cookies)  # but not signed in
    with SessionLocal() as db:
        assert db.scalar(select(UserSession)) is None
        assert db.scalar(select(Device)).is_authorized is False

    approve_device(err["details"]["device_id"])
    again = _raw_login(client)
    assert again.status_code == 200
    assert again.json()["device"]["id"] == err["details"]["device_id"]


def test_wrong_password_on_new_computer_registers_nothing(world, client) -> None:
    resp = client.post("/api/v1/auth/login", json={"username": "boss", "password": "nope-nope"})
    assert resp.status_code == 401
    with SessionLocal() as db:
        assert db.scalar(select(Device)) is None


def test_login_sets_device_and_session_cookies(world, client) -> None:
    resp = _login(client, device_name="Front desk PC")
    assert resp.status_code == 200
    body = resp.json()
    assert body["expires_in"] == 15 * 60
    assert body["device"]["name"] == "Front desk PC"
    cookies = resp.headers.get_list("set-cookie")
    for name in ("device_token=", "session_token="):
        c = next(c for c in cookies if c.startswith(name))
        assert "HttpOnly" in c and "Path=/api/v1/auth" in c and "Max-Age=" in c


def test_not_remembered_session_cookie_is_browser_session_only(world, client) -> None:
    resp = _login(client, remember=False)
    session_cookie = next(
        c for c in resp.headers.get_list("set-cookie") if c.startswith("session_token=")
    )
    assert "Max-Age" not in session_cookie
    with SessionLocal() as db:
        s = db.scalar(select(UserSession))
        assert s.expires_at is not None and not s.remember


def test_remembered_session_never_expires(world, client) -> None:
    _login(client)
    with SessionLocal() as db:
        s = db.scalar(select(UserSession))
        assert s.remember and s.expires_at is None
        # Pretend it was created a year ago: still valid.
        s.created_at = s.last_used_at = datetime.now(UTC) - timedelta(days=365)
        db.commit()
    assert client.post("/api/v1/auth/refresh").status_code == 200


def test_same_computer_is_recognized_on_next_login(world, client) -> None:
    first = _login(client).json()["device"]["id"]
    client.post("/api/v1/auth/logout")
    second = _login(client, "clerk1").json()["device"]["id"]
    assert first == second
    other_pc = TestClient(app)
    assert _login(other_pc).json()["device"]["id"] != first


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


def test_garbage_bearer_is_rejected(world, client) -> None:
    resp = client.get("/api/v1/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert resp.status_code == 401


def test_me_for_owner(world, client) -> None:
    me = client.get("/api/v1/me", headers=login(client, "boss")).json()
    assert [t["code"] for t in me["tags"]] == ["owner"]
    assert me["tags"][0]["name"] == "Owner"
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
    login(client, "clerk1")
    resp = client.post("/api/v1/auth/refresh")
    assert resp.status_code == 200
    token = resp.json()["access_token"]
    me = client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert me.json()["username"] == "clerk1"


def test_refresh_without_cookie_fails(world, client) -> None:
    assert client.post("/api/v1/auth/refresh").status_code == 401


def test_logout_ends_session_immediately(world, client) -> None:
    headers = login(client, "boss")
    assert client.post("/api/v1/auth/logout").status_code == 204
    assert client.post("/api/v1/auth/refresh").status_code == 401
    # The still-unexpired access token dies with the session.
    assert client.get("/api/v1/me", headers=headers).status_code == 401


def test_deactivated_user_is_signed_out(world, client) -> None:
    headers = login(client, "clerk1")
    with SessionLocal() as db:
        from app.models import AppUser

        db.scalar(select(AppUser).where(AppUser.username == "clerk1")).is_active = False
        db.commit()
    assert client.get("/api/v1/me", headers=headers).status_code == 401


def test_device_record_tracks_last_seen(world, client) -> None:
    _login(client)
    with SessionLocal() as db:
        d = db.scalar(select(Device))
        assert d.last_seen_at is not None
        assert d.token_hash and len(d.token_hash) == 64  # only the hash is stored
