"""The observer as an MCP client: external tools, one connection, N forks.

Detectors declare what they need — `{"mcp": "code-search"}` for a whole server, or
`mcp__code-search__query` for one tool — and this resolves those grants against the
servers configured under `mcp.servers`. The worker holds **one session per server
and shares it across every fork**: requests are independent, so concurrency is fine;
what is never shared is a *result*, which stays in the fork that asked (§Tools
inside a fork).

**Protocol work is exactly what should not be hand-rolled**, so this is a thin
adapter over the official `mcp` Python SDK and nothing else. When the SDK is not
installed the hub does not quietly pretend to work: it logs once at warning level,
records the reason in the status file, and every granted MCP tool is *absent* from
the fork's tool list — a detector that needed one produces no answer rather than a
confident answer built without it.

Tool names are mangled to `mcp__<server>__<tool>` on the way out and unmangled on
dispatch, which is what makes a per-detector allowlist expressible by name.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from .tools import ToolError, ToolGrant

PREFIX = "mcp__"


def mangle(server: str, tool: str) -> str:
    return f"{PREFIX}{server}__{tool}"


def unmangle(name: str) -> tuple[str, str]:
    if not name.startswith(PREFIX):
        raise ToolError(f"not an MCP tool name: {name}")
    server, _, tool = name[len(PREFIX):].partition("__")
    if not server or not tool:
        raise ToolError(f"malformed MCP tool name: {name}")
    return server, tool


@dataclass
class Server:
    """One configured server: its transport, its session, and what it offers."""

    name: str
    spec: dict[str, Any]
    session: Any = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    _stack: Any = None


class McpHub:
    """Connections to the configured MCP servers, shared across a pass's forks."""

    def __init__(self, servers: dict[str, Any], log: Any = None) -> None:
        self.log = log
        self.servers: dict[str, Server] = {
            name: Server(name=name, spec=spec)
            for name, spec in (servers or {}).items() if isinstance(spec, dict)
        }
        self.available = False
        self.reason = ""

    # ── lifecycle ───────────────────────────────────────────────────────────
    async def start(self) -> None:
        """Connect to every configured server. A failure disables that server only."""
        if not self.servers:
            return
        try:
            import mcp  # noqa: F401
        except ImportError:
            self.reason = ("the `mcp` Python SDK is not installed, so MCP-granted tools are "
                           "unavailable to every detector that declared them "
                           "(pip install mcp)")
            if self.log:
                self.log.warning("second-brain: %s", self.reason)
            return

        self.available = True
        for server in self.servers.values():
            try:
                await self._connect(server)
            except Exception as exc:                               # noqa: BLE001
                server.error = f"{type(exc).__name__}: {exc}"
                if self.log:
                    self.log.warning("MCP server %s unavailable: %s", server.name, server.error)

    async def _connect(self, server: Server) -> None:
        from contextlib import AsyncExitStack

        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        stack = AsyncExitStack()
        spec = server.spec
        if spec.get("url"):
            from mcp.client.streamable_http import streamablehttp_client
            read, write, *_ = await stack.enter_async_context(
                streamablehttp_client(str(spec["url"]), headers=dict(spec.get("headers") or {})))
        else:
            params = StdioServerParameters(
                command=str(spec.get("command", "")),
                args=[str(a) for a in (spec.get("args") or [])],
                env=dict(spec.get("env") or {}) or None,
            )
            read, write = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        listing = await session.list_tools()

        server._stack = stack
        server.session = session
        server.tools = [
            {"name": tool.name, "description": tool.description or "",
             "input_schema": tool.inputSchema or {"type": "object", "properties": {}}}
            for tool in listing.tools
        ]
        if self.log:
            self.log.info("MCP server %s: %d tools", server.name, len(server.tools))

    async def close(self) -> None:
        for server in self.servers.values():
            if server._stack is not None:
                try:
                    await server._stack.aclose()
                except Exception:                                  # noqa: BLE001
                    pass
                server._stack = None
                server.session = None

    # ── the Toolbox interface ───────────────────────────────────────────────
    def definitions(self, grant: ToolGrant) -> list[dict[str, Any]]:
        """The granted MCP tools, named `mcp__<server>__<tool>`, in a stable order."""
        out: list[dict[str, Any]] = []
        for server in self.servers.values():
            if server.session is None:
                continue
            whole = server.name in grant.mcp_servers
            for tool in server.tools:
                name = mangle(server.name, tool["name"])
                if whole or name in grant.mcp_tools:
                    out.append({**tool, "name": name})
        return sorted(out, key=lambda t: t["name"])

    async def call(self, name: str, args: dict[str, Any], grant: ToolGrant) -> str:
        """Dispatch one MCP call, re-checking the grant rather than trusting the name."""
        server_name, tool_name = unmangle(name)
        if server_name not in grant.mcp_servers and name not in grant.mcp_tools:
            raise ToolError(f"{name} is not available to this detector")
        server = self.servers.get(server_name)
        if server is None:
            raise ToolError(f"no such MCP server: {server_name}")
        if server.session is None:
            raise ToolError(server.error or f"MCP server {server_name} is not connected")

        result = await asyncio.wait_for(server.session.call_tool(tool_name, args), timeout=60)
        return _render(result)

    def status(self) -> dict[str, Any]:
        """What `/second-brain-stats` shows about external tools."""
        return {
            "available": self.available,
            "reason": self.reason,
            "servers": {s.name: (s.error or f"{len(s.tools)} tools") for s in self.servers.values()},
        }


def _render(result: Any) -> str:
    """Flatten an MCP tool result into the text a fork's message list carries."""
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(str(text))
        else:
            parts.append(f"[{getattr(block, 'type', 'content')}]")
    rendered = "\n".join(parts) or "(no content)"
    if getattr(result, "isError", False):
        return f"error: {rendered}"
    return rendered
