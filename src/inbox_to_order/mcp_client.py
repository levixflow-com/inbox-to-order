"""MCP client side: launch the Mail and Demo ERP servers over stdio, discover their tools, call them.

The app and the model never import the server modules; they only see tools discovered with
`tools/list` and invoked with `tools/call`. `McpSession` keeps the servers running between UI clicks.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from mcp import Client, StdioServerParameters
from mcp.client.stdio import get_default_environment
from mcp.types import Tool

from .config import PROJECT_ROOT

SERVER_MODULES = {
    "mail": "inbox_to_order.mcp_servers.mail_server",
    "erp": "inbox_to_order.mcp_servers.erp_server",
}

# Only these settings are passed to server subprocesses. In particular the AI provider key is
# never handed to the MCP servers: they have no reason to hold it.
SERVER_ENV_KEYS = (
    "INBOX_MODE",
    "DEMO_ERP_DB",
    "FIXTURES_DIR",
    "GMAIL_CREDENTIALS_FILE",
    "GMAIL_TOKEN_FILE",
    "GMAIL_QUERY",
    "PYTHONPATH",
    "VIRTUAL_ENV",
)

# Tools the language model may request: read-only, and further limited per call by agent.gate_tool_call.
# Everything else (listing the whole mailbox, listing orders, and above all create_order/update_order)
# is reserved for application code, and the ERP writes run only after an operator's confirmation.
MODEL_ALLOWED_TOOLS = frozenset(
    {"get_order_thread", "get_text_attachment", "find_customer", "find_product", "get_order"}
)


class ToolCallError(RuntimeError):
    """A tool returned isError, or the call was refused before reaching the server."""

    def __init__(self, tool: str, code: str, message: str) -> None:
        super().__init__(f"{tool}: {code}: {message}")
        self.tool = tool
        self.code = code
        self.message = message


AuditHook = Callable[[str, str, str, dict[str, Any], str], None]  # (actor, server, tool, args, status)


@dataclass
class Toolbox:
    clients: dict[str, Client]
    tools: dict[str, tuple[str, Tool]] = field(default_factory=dict)  # tool name -> (server, Tool)
    on_call: AuditHook | None = None

    async def discover(self) -> None:
        for server_name, client in self.clients.items():
            listing = await client.list_tools()
            for tool in listing.tools:
                if tool.name in self.tools:
                    raise RuntimeError(f"Duplicate tool name {tool.name!r} across MCP servers")
                self.tools[tool.name] = (server_name, tool)

    def tool_names(self) -> list[str]:
        return sorted(self.tools)

    async def call(self, name: str, arguments: dict[str, Any], *, actor: str = "app") -> dict[str, Any]:
        if name not in self.tools:
            self.record(actor, "-", name, arguments, "refused_unknown_tool")
            raise ToolCallError(name, "UNKNOWN_TOOL", f"No MCP server exposes a tool named {name!r}.")
        server_name, _ = self.tools[name]
        result = await self.clients[server_name].call_tool(name, arguments)
        if result.is_error:
            text = " ".join(getattr(c, "text", "") for c in result.content).strip()
            code, _, msg = text.partition(": ")
            # MCPServer prefixes errors with "Error executing tool <name>: "
            if code.startswith("Error executing tool"):
                code, _, msg = msg.partition(": ")
            code = code if code.isupper() else "TOOL_ERROR"
            self.record(actor, server_name, name, arguments, f"error:{code}")
            raise ToolCallError(name, code, msg or text)
        if result.structured_content is None:
            self.record(actor, server_name, name, arguments, "error:NO_STRUCTURED_CONTENT")
            raise ToolCallError(name, "NO_STRUCTURED_CONTENT", "Tool returned no structured content.")
        self.record(actor, server_name, name, arguments, "ok")
        return result.structured_content

    def record(self, actor: str, server: str, tool: str, args: dict[str, Any], status: str) -> None:
        if self.on_call:
            self.on_call(actor, server, tool, args, status)


def _server_params(module: str) -> StdioServerParameters:
    env = get_default_environment()
    env.update({k: os.environ[k] for k in SERVER_ENV_KEYS if k in os.environ})
    env["PYTHONIOENCODING"] = "utf-8"
    return StdioServerParameters(command=sys.executable, args=["-m", module], env=env, cwd=str(PROJECT_ROOT))


@asynccontextmanager
async def open_toolbox(on_call: AuditHook | None = None, *, read_timeout_seconds: float = 60) -> AsyncIterator[Toolbox]:
    """Start the Mail and Demo ERP MCP servers as stdio subprocesses and discover their tools."""
    async with AsyncExitStack() as stack:
        clients: dict[str, Client] = {}
        for name, module in SERVER_MODULES.items():
            client = Client(_server_params(module), read_timeout_seconds=read_timeout_seconds)
            clients[name] = await stack.enter_async_context(client)
        box = Toolbox(clients=clients, on_call=on_call)
        await box.discover()
        yield box


def anthropic_tool_definitions(box: Toolbox, allowed: frozenset[str] = MODEL_ALLOWED_TOOLS) -> list[dict[str, Any]]:
    """Convert discovered MCP tools into Claude API tool definitions (allow-listed only)."""
    defs = []
    for name in sorted(allowed & set(box.tools)):
        _, tool = box.tools[name]
        defs.append({"name": tool.name, "description": tool.description or "", "input_schema": tool.input_schema})
    return defs


class McpSession:
    """Keeps one Toolbox (both servers) open on a background event loop, so repeated UI actions do not
    pay the server start-up cost each time. Single user, local use only."""

    def __init__(self, start_timeout: float = 60) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="mcp-session", daemon=True)
        self._thread.start()
        self._stop: asyncio.Event | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self.box: Toolbox | None = None
        self.started_seconds: float | None = None
        asyncio.run_coroutine_threadsafe(self._hold(), self._loop)
        if not self._ready.wait(start_timeout):
            raise TimeoutError("MCP servers did not start in time")
        if self._error is not None:
            raise self._error

    async def _hold(self) -> None:
        import time

        t0 = time.perf_counter()
        self._stop = asyncio.Event()
        try:
            async with open_toolbox() as box:
                self.box = box
                self.started_seconds = time.perf_counter() - t0
                self._ready.set()
                await self._stop.wait()
        except BaseException as exc:  # noqa: BLE001 - reported to the caller of __init__
            self._error = exc
            self._ready.set()

    def run(self, fn: Callable[[Toolbox], Coroutine[Any, Any, Any]], timeout: float = 300) -> Any:
        """Run `fn(box)` on the session loop and wait for its result."""
        if self.box is None:
            raise RuntimeError("MCP session is not running")
        box = self.box
        return asyncio.run_coroutine_threadsafe(fn(box), self._loop).result(timeout)

    def close(self) -> None:
        if self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
