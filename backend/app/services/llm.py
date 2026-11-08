"""Chat model with tool calling, reached through any OpenAI-compatible endpoint.

Configured by LLM_* settings (DeepSeek, the company model gateway, Qwen, a local model...).
Nothing configured = the AI butler is off. Tests swap in a scripted model with
`set_chat_model`.
"""

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from app.core.config import get_settings


class LlmError(Exception):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON text exactly as the model wrote it

    def as_dict(self) -> dict[str, str]:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}


@dataclass
class Reply:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0


class ChatModel(Protocol):
    name: str

    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], tool_choice: str = "auto"
    ) -> Reply: ...


class OpenAICompatible:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key, self.name, self.timeout = api_key, model, timeout

    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], tool_choice: str = "auto"
    ) -> Reply:
        body: dict[str, Any] = {"model": self.name, "messages": messages, "temperature": 0.1}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = tool_choice  # "none" = must answer in words now
        started = time.monotonic()
        try:
            resp = httpx.post(
                f"{self.base_url}/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise LlmError(f"Model endpoint unreachable: {exc}") from exc
        if resp.status_code != 200:
            raise LlmError(f"Model endpoint returned {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        msg = data["choices"][0]["message"]
        usage = data.get("usage") or {}
        return Reply(
            content=msg.get("content"),
            tool_calls=[
                ToolCall(c["id"], c["function"]["name"], c["function"].get("arguments") or "{}")
                for c in msg.get("tool_calls") or []
            ],
            model=data.get("model", self.name),
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            seconds=time.monotonic() - started,
        )


def to_wire(role: str, content: str | None, tool_calls=None, tool_call_id=None) -> dict:
    """One stored message in the OpenAI chat format."""
    if role == "assistant" and tool_calls:
        return {
            "role": "assistant",
            "content": content,
            "tool_calls": [
                {
                    "id": c["id"],
                    "type": "function",
                    "function": {"name": c["name"], "arguments": c["arguments"]},
                }
                for c in tool_calls
            ],
        }
    if role == "tool":
        return {"role": "tool", "tool_call_id": tool_call_id, "content": content or ""}
    return {"role": role, "content": content or ""}


def tool_schema(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": parameters},
    }


def parse_arguments(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"arguments are not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("arguments must be a JSON object")
    return value


_model: ChatModel | None = None
_overridden = False


def set_chat_model(model: ChatModel | None) -> None:
    global _model, _overridden
    _model, _overridden = model, True


def reset_chat_model() -> None:
    global _model, _overridden
    _model, _overridden = None, False


def get_chat_model() -> ChatModel | None:
    if _overridden:
        return _model
    s = get_settings()
    if not (s.llm_base_url and s.llm_model):
        return None
    return OpenAICompatible(s.llm_base_url, s.llm_api_key, s.llm_model, s.llm_timeout_seconds)


# Some models write their native tool-call markup as text when they may not call tools
# (DeepSeek: <｜DSML｜...>). Never show that to a user.
_MARKUP = re.compile(r"<[|｜]+[A-Za-z]*[|｜]+|</?tool_call|<function_calls")


def clean_answer(text: str | None) -> str | None:
    if not text or not text.strip() or _MARKUP.search(text):
        return None
    return text.strip()
