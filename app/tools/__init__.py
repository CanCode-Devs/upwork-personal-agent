from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from app.models import ToolSpec

ToolHandler = Callable[..., Awaitable[Any]]

_REGISTRY: dict[str, tuple[ToolSpec, ToolHandler]] = {}


def register_tool(spec: ToolSpec, handler: ToolHandler) -> None:
    _REGISTRY[spec["name"]] = (spec, handler)


def openai_tools() -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    for spec, _handler in _REGISTRY.items():
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": spec["name"],
                    "description": spec["description"],
                    "parameters": spec["parameters"],
                },
            }
        )
    return tools


async def call_tool(name: str, **kwargs: Any) -> Any:
    if name not in _REGISTRY:
        raise KeyError(f"Unknown tool: {name}")
    _spec, handler = _REGISTRY[name]
    return await handler(**kwargs)


def listed_specs() -> list[ToolSpec]:
    return [spec for spec, _handler in _REGISTRY.values()]
