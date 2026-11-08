"""Field-level permissions for responses.

Mark a response field with `sensitive("cost" | "pii" | "finance")`. `shape()` builds the
model leaving those fields *unset* when the caller lacks the matching permission, and the
route uses `response_model_exclude_unset=True`, so the key is absent from the JSON —
a cashier's response simply has no cost price in it.
"""

from typing import Any

from pydantic import BaseModel, Field

from app.services.auth import Principal
from app.services.masterdata import FIELD_PERMS


def sensitive(kind: str) -> Any:
    assert kind in FIELD_PERMS
    return Field(default=None, json_schema_extra={"x-sensitive": kind})


def _kind(cls: type[BaseModel], name: str) -> str | None:
    extra = cls.model_fields[name].json_schema_extra
    return extra.get("x-sensitive") if isinstance(extra, dict) else None


def shape[M: BaseModel](cls: type[M], source: Any, p: Principal, **overrides: Any) -> M:
    data: dict[str, Any] = {}
    for name in cls.model_fields:
        kind = _kind(cls, name)
        if kind and not p.has(FIELD_PERMS[kind]):
            continue
        if name in overrides:
            data[name] = overrides[name]
        elif isinstance(source, dict):
            if name in source:
                data[name] = source[name]
        else:
            data[name] = getattr(source, name)
    return cls.model_validate(data)
