"""Public demo site (DEMO_MODE): open login, read-only administration, AI question limit."""

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.main import app
from app.services import llm
from tests.conftest import PASSWORD, login
from tests.test_ai import Scripted

pytestmark = pytest.mark.db
API = "/api/v1"


@pytest.fixture
def demo(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "demo_mode", True)
    monkeypatch.setattr(s, "demo_password", PASSWORD)
    monkeypatch.setattr(s, "demo_accounts", "boss:Owner, cashier1:Cashier")
    return s


def test_login_page_gets_nothing_when_demo_is_off(client) -> None:
    assert client.get(f"{API}/public/demo").json() == {"demo": False}


def test_demo_site_offers_its_accounts_without_a_login(world, client, demo) -> None:
    info = client.get(f"{API}/public/demo").json()
    assert info["demo"] is True and info["password"] == PASSWORD
    assert info["accounts"] == [
        {"username": "boss", "label": "Owner"},
        {"username": "cashier1", "label": "Cashier"},
    ]


def test_new_browser_logs_in_without_approval_only_on_the_demo_site(world) -> None:
    body = {"username": "cashier1", "password": PASSWORD}
    assert TestClient(app).post(f"{API}/auth/login", json=body).status_code == 403


def test_new_browser_logs_in_on_the_demo_site(world, demo) -> None:
    pc = TestClient(app)
    r = pc.post(f"{API}/auth/login", json={"username": "cashier1", "password": PASSWORD})
    assert r.status_code == 200, r.text
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert pc.get(f"{API}/me", headers=h).json()["username"] == "cashier1"
    # a wrong password is still a wrong password
    bad = TestClient(app).post(f"{API}/auth/login", json={"username": "boss", "password": "x"})
    assert bad.status_code == 401


def test_demo_visitors_cannot_change_accounts_devices_or_company(world, client, demo) -> None:
    h = login(client, "boss")
    users = client.get(f"{API}/users", headers=h).json()["items"]  # reading still works
    target = next(u for u in users if u["username"] == "cashier1")
    me = client.get(f"{API}/me", headers=h).json()

    attempts = [
        ("patch", f"/users/{target['id']}", {"is_active": False}),
        ("post", "/users", {"username": "x1", "display_name": "X", "password": PASSWORD}),
        ("post", f"/devices/{me['device']['id']}/deauthorize", None),
        ("patch", "/company", {"version": 1, "name": "Hacked"}),
        ("post", "/tags", {"code": "x", "name": "X"}),
    ]
    for method, path, body in attempts:
        r = getattr(client, method)(f"{API}{path}", json=body, headers=h)
        assert r.status_code == 403, (path, r.text)
        assert r.json()["error"]["code"] == "DEMO_READ_ONLY"
    # ordinary work is not blocked
    assert client.get(f"{API}/customers", headers=h).status_code == 200


def test_the_same_requests_reach_the_api_when_demo_is_off(world, client) -> None:
    h = login(client, "boss")
    r = client.post(f"{API}/tags", json={"code": "x", "name": "X"}, headers=h)
    assert r.status_code != 403, r.text  # reaches the route (which then asks for a colour)


def test_ai_butler_stops_at_the_daily_limit(world, client, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "ai_daily_limit", 2)
    llm.set_chat_model(Scripted(("say", "one"), ("say", "two"), ("say", "three")))
    try:
        h = login(client, "boss")
        conv = client.post(f"{API}/ai/conversations", json={}, headers=h).json()["id"]
        ask = lambda: client.post(  # noqa: E731
            f"{API}/ai/conversations/{conv}/messages", json={"content": "hi"}, headers=h
        )
        assert ask().status_code == 200 and ask().status_code == 200
        third = ask()
        assert third.status_code == 422
        assert third.json()["error"]["details"] == {"daily_limit": 2}
        # the refused question is not stored, so it does not count against tomorrow either
        msgs = client.get(f"{API}/ai/conversations/{conv}/messages", headers=h).json()
        assert [m["content"] for m in msgs if m["role"] == "user"] == ["hi", "hi"]
    finally:
        llm.reset_chat_model()


def test_hosted_database_url_is_used_with_the_psycopg_driver() -> None:
    rest = "u:p@db.example.com/neondb?sslmode=require"
    s = Settings(DATABASE_URL=f"postgres://{rest}", _env_file=None)
    assert s.database_url == f"postgresql+psycopg://{rest}"
    assert Settings(_env_file=None).database_url.startswith("postgresql+psycopg://erp:")
