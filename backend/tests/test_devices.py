import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.conftest import login

pytestmark = pytest.mark.db


def test_owner_authorizes_another_computer(world, client) -> None:
    owner_h = login(client, "boss", edit=True)

    shop_pc = TestClient(app)
    clerk_h = login(shop_pc, "clerk1")
    new = {"code": "S7", "name": "Store 7"}
    # A new computer starts read-only.
    me = shop_pc.get("/api/v1/me", headers=clerk_h).json()
    assert me["device"]["can_edit"] is False

    devices = client.get("/api/v1/devices", headers=owner_h).json()["items"]
    assert len(devices) == 2
    shop = next(d for d in devices if d["id"] == me["device"]["id"])
    assert shop["is_current"] is False

    resp = client.post(f"/api/v1/devices/{shop['id']}/authorize", headers=owner_h)
    assert resp.status_code == 200 and resp.json()["can_edit"] is True
    assert shop_pc.get("/api/v1/me", headers=clerk_h).json()["device"]["can_edit"] is True

    # Owner logging in on the now-authorized shop PC can write from it.
    boss_on_shop = login(shop_pc, "boss")
    assert shop_pc.post("/api/v1/locations", json=new, headers=boss_on_shop).status_code == 201


def test_deauthorize_takes_effect_immediately(world, client) -> None:
    owner_h = login(client, "boss", edit=True)
    me = client.get("/api/v1/me", headers=owner_h).json()
    other = TestClient(app)
    other_h = login(other, "boss", edit=True)
    other_id = other.get("/api/v1/me", headers=other_h).json()["device"]["id"]

    client.post(f"/api/v1/devices/{other_id}/deauthorize", headers=owner_h)
    resp = other.post("/api/v1/locations", json={"code": "S8", "name": "x"}, headers=other_h)
    assert resp.json()["error"]["code"] == "DEVICE_NOT_AUTHORIZED"
    assert me["device"]["can_edit"] is True


def test_sign_out_device_ends_its_sessions(world, client) -> None:
    owner_h = login(client, "boss", edit=True)
    lost_phone = TestClient(app)
    phone_h = login(lost_phone, "clerk1")
    phone_id = lost_phone.get("/api/v1/me", headers=phone_h).json()["device"]["id"]

    resp = client.post(f"/api/v1/devices/{phone_id}/sign-out", headers=owner_h)
    assert resp.json() == {"sessions_ended": 1}
    assert lost_phone.get("/api/v1/me", headers=phone_h).status_code == 401
    assert lost_phone.post("/api/v1/auth/refresh").status_code == 401


def test_unauthorized_owner_computer_cannot_self_authorize(world, client) -> None:
    h = login(client, "boss")
    my_id = client.get("/api/v1/me", headers=h).json()["device"]["id"]
    resp = client.post(f"/api/v1/devices/{my_id}/authorize", headers=h)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "DEVICE_NOT_AUTHORIZED"


def test_non_owner_cannot_list_devices(world, client) -> None:
    resp = client.get("/api/v1/devices", headers=login(client, "acct"))
    assert resp.status_code == 403


def test_other_company_device_is_invisible(world, client) -> None:
    other = TestClient(app)
    h2 = login(other, "boss2", edit=True)
    their_id = other.get("/api/v1/me", headers=h2).json()["device"]["id"]
    resp = client.post(
        f"/api/v1/devices/{their_id}/sign-out", headers=login(client, "boss", edit=True)
    )
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
