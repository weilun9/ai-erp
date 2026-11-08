import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.conftest import PASSWORD, login

pytestmark = pytest.mark.db


def _try_login(pc: TestClient, username: str):
    return pc.post("/api/v1/auth/login", json={"username": username, "password": PASSWORD})


def test_owner_approves_pending_computer_from_device_list(world, client) -> None:
    owner_h = login(client, "boss")

    shop_pc = TestClient(app)
    first = _try_login(shop_pc, "clerk1")
    assert first.status_code == 403
    pending_id = first.json()["error"]["details"]["device_id"]

    devices = {d["id"]: d for d in client.get("/api/v1/devices", headers=owner_h).json()["items"]}
    assert devices[pending_id]["is_authorized"] is False
    assert devices[pending_id]["is_current"] is False

    resp = client.post(f"/api/v1/devices/{pending_id}/authorize", headers=owner_h)
    assert resp.status_code == 200 and resp.json()["is_authorized"] is True

    assert _try_login(shop_pc, "clerk1").status_code == 200


def test_deauthorize_locks_computer_out_immediately(world, client) -> None:
    owner_h = login(client, "boss")
    other = TestClient(app)
    other_h = login(other, "clerk1")
    other_id = other.get("/api/v1/me", headers=other_h).json()["device"]["id"]

    client.post(f"/api/v1/devices/{other_id}/deauthorize", headers=owner_h)
    assert other.get("/api/v1/me", headers=other_h).status_code == 401
    assert other.post("/api/v1/auth/refresh").status_code == 401
    assert _try_login(other, "clerk1").status_code == 403


def test_sign_out_device_ends_sessions_but_keeps_approval(world, client) -> None:
    owner_h = login(client, "boss")
    phone = TestClient(app)
    phone_h = login(phone, "clerk1")
    phone_id = phone.get("/api/v1/me", headers=phone_h).json()["device"]["id"]

    resp = client.post(f"/api/v1/devices/{phone_id}/sign-out", headers=owner_h)
    assert resp.json() == {"sessions_ended": 1}
    assert phone.get("/api/v1/me", headers=phone_h).status_code == 401
    assert phone.post("/api/v1/auth/refresh").status_code == 401
    assert _try_login(phone, "clerk1").status_code == 200  # still an approved computer


def test_non_owner_cannot_manage_devices(world, client) -> None:
    resp = client.get("/api/v1/devices", headers=login(client, "acct"))
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"


def test_other_company_device_is_invisible(world, client) -> None:
    other = TestClient(app)
    h2 = login(other, "boss2")
    their_id = other.get("/api/v1/me", headers=h2).json()["device"]["id"]
    resp = client.post(f"/api/v1/devices/{their_id}/sign-out", headers=login(client, "boss"))
    assert resp.status_code == 404


def test_docs_hidden_outside_dev(monkeypatch) -> None:
    from app.core.config import get_settings
    from app.main import create_app

    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("JWT_SECRET", "x" * 40)
    get_settings.cache_clear()
    try:
        prod = TestClient(create_app())
        assert prod.get("/docs").status_code == 404
        assert prod.get("/openapi.json").status_code == 404
    finally:
        get_settings.cache_clear()
