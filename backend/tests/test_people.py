import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.conftest import PASSWORD, login

pytestmark = pytest.mark.db


def _tag_id(client, headers, code) -> str:
    tags = client.get("/api/v1/tags", headers=headers).json()["items"]
    return next(t["id"] for t in tags if t["code"] == code)


def _user_id(client, headers, username) -> str:
    users = client.get("/api/v1/users", headers=headers).json()["items"]
    return next(u["id"] for u in users if u["username"] == username)


def test_tags_list_shows_builtin_tags_with_colors_and_counts(world, client) -> None:
    resp = client.get("/api/v1/tags", headers=login(client, "boss"))
    assert resp.status_code == 200
    tags = {t["code"]: t for t in resp.json()["items"]}
    assert set(tags) == {"owner", "clerk", "accountant", "cashier", "admin"}
    assert tags["clerk"]["name"] == "书记"
    assert tags["clerk"]["color"].startswith("#")
    assert tags["owner"]["user_count"] == 1
    assert tags["clerk"]["user_count"] == 1


def test_owner_assigns_tags_to_employee(world, client) -> None:
    h = login(client, "boss", edit=True)
    clerk = _user_id(client, h, "clerk1")
    tags = {_tag_id(client, h, "clerk"), _tag_id(client, h, "cashier")}
    resp = client.put(f"/api/v1/users/{clerk}/tags", json={"tag_ids": list(tags)}, headers=h)
    assert resp.status_code == 200
    assert {t["code"] for t in resp.json()["tags"]} == {"clerk", "cashier"}

    me = client.get("/api/v1/me", headers=login(TestClient(app), "clerk1")).json()
    assert "pos.sale.create" in me["permissions"]


def test_tag_change_applies_on_next_request(world, client) -> None:
    clerk_pc = TestClient(app)
    clerk_h = login(clerk_pc, "clerk1")
    assert "pur.doc.post" in clerk_pc.get("/api/v1/me", headers=clerk_h).json()["permissions"]

    h = login(client, "boss", edit=True)
    clerk = _user_id(client, h, "clerk1")
    client.put(
        f"/api/v1/users/{clerk}/tags",
        json={"tag_ids": [_tag_id(client, h, "cashier")]},
        headers=h,
    )
    perms = clerk_pc.get("/api/v1/me", headers=clerk_h).json()["permissions"]
    assert "pur.doc.post" not in perms


def test_clerk_cannot_assign_tags(world, client) -> None:
    h = login(client, "clerk1", edit=True)
    boss_view = login(TestClient(app), "boss")
    me_id = _user_id(TestClient(app), boss_view, "clerk1")
    owner = _tag_id(TestClient(app), boss_view, "owner")
    resp = client.put(f"/api/v1/users/{me_id}/tags", json={"tag_ids": [owner]}, headers=h)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"


def test_assigning_tags_needs_authorized_computer(world, client) -> None:
    h = login(client, "boss")  # not authorized
    clerk = _user_id(client, h, "clerk1")
    resp = client.put(
        f"/api/v1/users/{clerk}/tags", json={"tag_ids": [_tag_id(client, h, "clerk")]}, headers=h
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "DEVICE_NOT_AUTHORIZED"


def test_last_owner_cannot_be_removed(world, client) -> None:
    h = login(client, "boss", edit=True)
    me = _user_id(client, h, "boss")
    resp = client.put(
        f"/api/v1/users/{me}/tags", json={"tag_ids": [_tag_id(client, h, "clerk")]}, headers=h
    )
    assert resp.status_code == 409


def test_owner_creates_custom_tag_and_employee(world, client) -> None:
    h = login(client, "boss", edit=True)
    s2 = world.s2_id
    tag = client.post(
        "/api/v1/tags",
        json={
            "code": "stock_checker",
            "name": "盘点员",
            "color": "#13c2c2",
            "permissions": ["inv.balance.read", "inv.doc.create", "md.item.read"],
        },
        headers=h,
    )
    assert tag.status_code == 201, tag.text
    user = client.post(
        "/api/v1/users",
        json={
            "username": "ali",
            "display_name": "Ali",
            "password": PASSWORD,
            "language": "ms",
            "tag_ids": [tag.json()["id"]],
            "location_ids": [str(s2)],
        },
        headers=h,
    )
    assert user.status_code == 201, user.text
    me = client.get("/api/v1/me", headers=login(TestClient(app), "ali")).json()
    assert me["permissions"] == ["inv.balance.read", "inv.doc.create", "md.item.read"]
    assert [loc["code"] for loc in me["locations"]] == ["S2"]
    assert me["tags"] == [{"code": "stock_checker", "name": "盘点员", "color": "#13c2c2"}]


def test_builtin_tag_can_be_renamed_but_permissions_are_fixed(world, client) -> None:
    h = login(client, "boss", edit=True)
    clerk = _tag_id(client, h, "clerk")
    ok = client.patch(f"/api/v1/tags/{clerk}", json={"name": "文员", "color": "#000000"}, headers=h)
    assert ok.status_code == 200 and ok.json()["name"] == "文员"
    bad = client.patch(f"/api/v1/tags/{clerk}", json={"permissions": []}, headers=h)
    assert bad.status_code == 400


def test_unknown_permission_in_custom_tag_rejected(world, client) -> None:
    h = login(client, "boss", edit=True)
    resp = client.post(
        "/api/v1/tags",
        json={"code": "x_tag", "name": "X", "color": "#ffffff", "permissions": ["nope.x.y"]},
        headers=h,
    )
    assert resp.status_code == 400


def test_deactivating_employee_signs_them_out(world, client) -> None:
    cashier_pc = TestClient(app)
    ch = login(cashier_pc, "cashier1")
    h = login(client, "boss", edit=True)
    uid = _user_id(client, h, "cashier1")
    assert client.patch(f"/api/v1/users/{uid}", json={"is_active": False}, headers=h).is_success
    assert cashier_pc.get("/api/v1/me", headers=ch).status_code == 401
    assert cashier_pc.post("/api/v1/auth/refresh").status_code == 401


def test_users_list_is_company_scoped(world, client) -> None:
    names = {u["username"] for u in client.get(
        "/api/v1/users", headers=login(client, "boss")
    ).json()["items"]}
    assert "boss2" not in names
    assert {"boss", "clerk1", "cashier1", "acct"} <= names
