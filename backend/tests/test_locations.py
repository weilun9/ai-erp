import pytest

from tests.conftest import login

pytestmark = pytest.mark.db


def _codes(resp) -> list[str]:
    return [row["code"] for row in resp.json()["items"]]


def test_owner_sees_all_own_company_locations(world, client) -> None:
    resp = client.get("/api/v1/locations", headers=login(client, "boss"))
    assert resp.status_code == 200
    assert _codes(resp) == ["HQ", "S2", "S3"]  # never T2's X1
    assert resp.json()["total"] == 3


def test_clerk_sees_only_assigned_locations(world, client) -> None:
    resp = client.get("/api/v1/locations", headers=login(client, "clerk1"))
    assert _codes(resp) == ["S2"]


def test_accountant_sees_all_locations(world, client) -> None:
    resp = client.get("/api/v1/locations", headers=login(client, "acct"))
    assert _codes(resp) == ["HQ", "S2", "S3"]


def test_out_of_scope_location_is_404_not_403(world, client) -> None:
    headers = login(client, "clerk1")
    assert client.get(f"/api/v1/locations/{world.s2_id}", headers=headers).status_code == 200
    resp = client.get(f"/api/v1/locations/{world.s3_id}", headers=headers)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_other_company_location_is_invisible_even_to_owner(world, client) -> None:
    resp = client.get(
        f"/api/v1/locations/{world.other_company_loc_id}", headers=login(client, "boss")
    )
    assert resp.status_code == 404


def test_cashier_cannot_create_location(world, client) -> None:
    resp = client.post(
        "/api/v1/locations",
        json={"code": "S9", "name": "New"},
        headers=login(client, "cashier1"),
    )
    assert resp.status_code == 403
    assert resp.json()["error"] == {
        "code": "FORBIDDEN",
        "message": "Missing permission",
        "details": {"required": ["core.location.manage"]},
    }


def test_admin_creates_location_and_duplicate_is_conflict(world, client) -> None:
    headers = login(client, "boss")
    body = {"code": "ECOM", "name": "E-commerce stock", "type": "ECOM"}
    resp = client.post("/api/v1/locations", json=body, headers=headers)
    assert resp.status_code == 201
    assert resp.json()["type"] == "ECOM"
    assert resp.json()["version"] == 1

    again = client.post("/api/v1/locations", json=body, headers=headers)
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "CONFLICT"


def test_location_code_is_validated(world, client) -> None:
    resp = client.post(
        "/api/v1/locations",
        json={"code": "bad code", "name": "x"},
        headers=login(client, "boss"),
    )
    assert resp.status_code == 400
