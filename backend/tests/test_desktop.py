"""Desktop app (DESKTOP_MODE): this computer logs in without approval, and the same server
serves the built web app."""

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app, create_app
from tests.conftest import PASSWORD

API = "/api/v1"


@pytest.mark.db
def test_desktop_app_approves_only_this_computer(world, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "desktop_mode", True)
    body = {"username": "cashier1", "password": PASSWORD}
    here = TestClient(app, client=("127.0.0.1", 50000))
    r = here.post(f"{API}/auth/login", json=body)
    assert r.status_code == 200, r.text
    # a request that did not come from this computer still needs the owner's approval
    other = TestClient(app, client=("192.168.1.20", 50000))
    assert other.post(f"{API}/auth/login", json=body).status_code == 403


@pytest.mark.db
def test_loopback_is_not_enough_without_desktop_mode(world) -> None:
    body = {"username": "cashier1", "password": PASSWORD}
    here = TestClient(app, client=("127.0.0.1", 50000))
    assert here.post(f"{API}/auth/login", json=body).status_code == 403


def test_desktop_server_serves_the_web_app(tmp_path, monkeypatch) -> None:
    (tmp_path / "index.html").write_text("<div id=root></div>", encoding="utf-8")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path.parent / "secret.txt").write_text("no", encoding="utf-8")
    monkeypatch.setattr(get_settings(), "frontend_dir", str(tmp_path))
    c = TestClient(create_app())

    assert c.get("/assets/app.js").text == "console.log(1)"
    page = c.get("/purchase/grn/123")  # an app route: the app handles it
    assert page.status_code == 200 and "root" in page.text
    assert page.headers["cache-control"] == "no-cache"
    assert "root" in c.get("/%2e%2e/secret.txt").text  # nothing outside the web app folder
    missing = c.get(f"{API}/no-such-thing")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "NOT_FOUND"
    assert c.get("/healthz").status_code == 200
