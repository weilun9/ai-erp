"""/ai: the AI butler chat and the confirm cards for what it proposes."""

import json
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.deps import DB, CurrentUser
from app.services import butler, mcp_tokens
from app.services import butler_tools as tools

router = APIRouter(prefix="/ai", tags=["ai"])


class ConversationOut(BaseModel):
    id: uuid.UUID
    title: str
    created_at: datetime
    updated_at: datetime


class ToolCallOut(BaseModel):
    name: str
    arguments: dict[str, Any] | str


class MessageOut(BaseModel):
    id: int
    role: str
    content: str | None
    tool_calls: list[ToolCallOut] | None = None
    tool_name: str | None = None
    created_at: datetime


class ActionOut(BaseModel):
    id: uuid.UUID
    tool: str
    status: str
    summary: dict[str, Any]
    result: dict[str, Any] | None
    error: str | None
    expires_at: datetime
    created_at: datetime
    decided_at: datetime | None


class TurnOut(BaseModel):
    conversation: ConversationOut
    messages: list[MessageOut]  # this turn: question, tool calls and results, answer
    pending_actions: list[ActionOut]  # cards to show with Confirm / Cancel
    tool_calls: int


class StartIn(BaseModel):
    title: str | None = Field(default=None, max_length=120)


class AskIn(BaseModel):
    content: str = Field(min_length=1, max_length=4000)


class ToolOut(BaseModel):
    name: str
    description: str
    write: bool


def _args(text: str) -> dict[str, Any] | str:
    try:
        return json.loads(text)
    except ValueError:
        return text


def _msg(m) -> MessageOut:
    return MessageOut(
        id=m.id,
        role=m.role,
        content=m.content,
        tool_calls=[
            ToolCallOut(name=c["name"], arguments=_args(c["arguments"])) for c in m.tool_calls
        ]
        if m.tool_calls
        else None,
        tool_name=m.tool_name,
        created_at=m.created_at,
    )


def _conv(c) -> ConversationOut:
    return ConversationOut.model_validate(c, from_attributes=True)


def _action(a) -> ActionOut:
    return ActionOut.model_validate(a, from_attributes=True)


@router.get("/tools")
def my_tools(p: CurrentUser) -> list[ToolOut]:
    """What the butler can do for this user (same list the model is given)."""
    p.require("ai.chat.use")
    return [
        ToolOut(name=t.name, description=t.description, write=t.write) for t in tools.available(p)
    ]


@router.post("/conversations", status_code=201)
def start(body: StartIn, p: CurrentUser, db: DB) -> ConversationOut:
    return _conv(butler.start(db, p, body.title))


@router.get("/conversations")
def list_conversations(p: CurrentUser, db: DB) -> list[ConversationOut]:
    return [_conv(c) for c in butler.conversations(db, p)]


@router.get("/conversations/{conversation_id}/messages")
def history(conversation_id: uuid.UUID, p: CurrentUser, db: DB) -> list[MessageOut]:
    c = butler.conversation(db, p, conversation_id)
    return [_msg(m) for m in butler.messages(db, c.id)]


@router.post("/conversations/{conversation_id}/messages")
def ask(conversation_id: uuid.UUID, body: AskIn, p: CurrentUser, db: DB) -> TurnOut:
    turn = butler.chat(db, p, conversation_id, body.content)
    return TurnOut(
        conversation=_conv(turn["conversation"]),
        messages=[_msg(m) for m in turn["messages"]],
        pending_actions=[_action(a) for a in turn["pending_actions"]],
        tool_calls=turn["tool_calls"],
    )


@router.get("/actions")
def pending_actions(p: CurrentUser, db: DB) -> list[ActionOut]:
    return [_action(a) for a in butler.pending(db, p)]


@router.post("/actions/{action_id}/confirm")
def confirm(action_id: uuid.UUID, p: CurrentUser, db: DB) -> ActionOut:
    return _action(butler.confirm(db, p, action_id))


@router.post("/actions/{action_id}/cancel")
def cancel(action_id: uuid.UUID, p: CurrentUser, db: DB) -> ActionOut:
    return _action(butler.cancel(db, p, action_id))


# --- MCP access tokens --------------------------------------------------------------------


class McpTokenOut(BaseModel):
    id: uuid.UUID
    created_at: datetime
    last_used_at: datetime


class McpTokenNew(McpTokenOut):
    token: str  # shown once
    usage: str


@router.post("/mcp-tokens", status_code=201)
def new_mcp_token(p: CurrentUser, db: DB) -> McpTokenNew:
    """A token for an AI client on this computer: read tools only, as you."""
    s, token = mcp_tokens.issue(db, p)
    return McpTokenNew(
        id=s.id,
        created_at=s.created_at,
        last_used_at=s.last_used_at,
        token=token,
        usage="ERP_MCP_TOKEN=<token> python -m app.mcp_server",
    )


@router.get("/mcp-tokens")
def list_mcp_tokens(p: CurrentUser, db: DB) -> list[McpTokenOut]:
    return [McpTokenOut.model_validate(s, from_attributes=True) for s in mcp_tokens.active(db, p)]


@router.delete("/mcp-tokens/{token_id}", status_code=204)
def revoke_mcp_token(token_id: uuid.UUID, p: CurrentUser, db: DB) -> None:
    mcp_tokens.revoke(db, p, token_id)
