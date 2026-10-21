"""MCP client used by the agent for every tool call.

By default it connects to the telecom MCP server in-process. Set MCP_SERVER to an http(s) URL to
talk to a separately deployed MCP server over streamable HTTP instead; the agent code is the same.
"""

from __future__ import annotations

import json
from typing import Any

from mcp import Client

from app.config import get_settings
from app.observability import tracer


class ToolError(RuntimeError):
    pass


class TelecomToolClient:
    def __init__(self, target: str | None = None) -> None:
        target = target or get_settings().mcp_server
        if target == "inproc":
            from app.mcp_server.server import mcp

            self._server: Any = mcp
        else:
            self._server = target

    async def list_tools(self) -> list[str]:
        async with Client(self._server) as client:
            result = await client.list_tools()
            return [t.name for t in result.tools]

    async def call(self, name: str, **arguments: Any) -> Any:
        with tracer.span(f"mcp.{name}", tool=name):
            async with Client(self._server) as client:
                result = await client.call_tool(name, {k: v for k, v in arguments.items() if v is not None})
        if getattr(result, "is_error", False) or getattr(result, "isError", False):
            text = " ".join(getattr(c, "text", "") for c in result.content)
            raise ToolError(f"{name} failed: {text}")
        structured = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
        if structured is not None:
            # tools returning lists are wrapped as {"result": [...]}
            if isinstance(structured, dict) and set(structured) == {"result"}:
                return structured["result"]
            return structured
        texts = [getattr(c, "text", "") for c in result.content]
        try:
            return json.loads(texts[0]) if len(texts) == 1 else [json.loads(t) for t in texts]
        except (json.JSONDecodeError, IndexError):
            return texts
