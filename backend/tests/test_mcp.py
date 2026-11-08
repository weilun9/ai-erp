import json

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp import Client

from app.main import app
from app.mcp_server import build
from tests.conftest import login
from tests.test_sales import store  # noqa: F401

pytestmark = pytest.mark.db


def _token(client, h):
    r = client.post("/api/v1/ai/mcp-tokens", headers=h)
    assert r.status_code == 201, r.text
    return r.json()


def _mcp(token, *calls):
    """Connect in-process like an MCP client would; returns tool names and call results."""

    async def go():
        async with Client(build(token)) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            out = []
            for name, args in calls:
                res = await c.call_tool(name, args)
                out.append((json.loads(res.content[0].text), res.is_error))
            return names, out

    return anyio.run(go)


def test_mcp_offers_read_tools_as_the_tokens_user(store, client) -> None:  # noqa: F811
    tok = _token(client, store["h"])["token"]
    names, [(stock, err), (draft, refused)] = _mcp(
        tok,
        ("get_stock_balance", {"item": "MILO"}),
        ("draft_document", {"doc_type": "quotation", "party": "ACME", "lines": []}),
    )
    assert "draft_document" not in names and {"get_stock_balance", "run_report"} <= names
    assert not err and stock["result"]["by_store"][0]["avg_cost"] == "1.5000"
    assert refused and draft["error"] == "NOT_AVAILABLE"  # writes stay behind confirm


def test_cashier_token_sees_what_a_cashier_sees(store, client) -> None:  # noqa: F811
    tok = _token(client, login(client, "cashier1"))["token"]
    names, [(res, err)] = _mcp(tok, ("run_report", {"report": "low_stock"}))
    assert "run_report" not in names and err and res["error"] == "NOT_AVAILABLE"


def test_revoking_or_deauthorizing_cuts_access_at_once(store, client) -> None:  # noqa: F811
    t = _token(client, store["h"])
    assert [x["id"] for x in client.get("/api/v1/ai/mcp-tokens", headers=store["h"]).json()] == [
        t["id"]
    ]
    assert client.delete(f"/api/v1/ai/mcp-tokens/{t['id']}", headers=store["h"]).status_code == 204
    _, [(res, err)] = _mcp_no_list(t["token"])
    assert err and res["error"] == "LOCKED"

    shop_pc = TestClient(app)  # clerk1 on another computer
    t = _token(shop_pc, login(shop_pc, "clerk1"))
    devices = client.get("/api/v1/devices", headers=store["h"]).json()["items"]
    for d in devices:  # the owner locks out every computer but the one they are on
        if not d.get("is_current"):
            client.post(f"/api/v1/devices/{d['id']}/deauthorize", headers=store["h"])
    _, [(res, err)] = _mcp_no_list(t["token"])
    assert err and res["error"] == "LOCKED"


def _mcp_no_list(token):
    async def go():
        async with Client(build(token)) as c:
            res = await c.call_tool("list_stores", {})
            return None, [(json.loads(res.content[0].text), res.is_error)]

    return anyio.run(go)


def test_mcp_token_is_not_a_web_login(store, client) -> None:  # noqa: F811
    tok = _token(client, store["h"])["token"]
    client.cookies.clear()
    client.cookies.set("session_token", tok, path="/api/v1/auth")
    assert client.post("/api/v1/auth/refresh").status_code == 401
