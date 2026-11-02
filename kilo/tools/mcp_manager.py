"""MCP Manager: connects to MCP servers and exposes their tools to the agent."""

import asyncio
import json
import os
import re
import time
from contextlib import AsyncExitStack
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple

from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp import types as mcp_types
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from dotenv import dotenv_values
from pydantic import BaseModel, Field

from kilo.config import settings

MCP_TOOL_PREFIX = "mcp__"
SERVER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
MAX_TOOL_NAME_LENGTH = 64
PING_TIMEOUT = 10.0  # seconds a connected server has to answer a health check
# Seconds between attempts to reconnect a server that keeps failing: after the 1st, 2nd, 3rd... failure.
RECONNECT_BACKOFF = (60.0, 120.0, 300.0, 900.0)


class MCPError(Exception):
    """An MCP server request that cannot be carried out; the message says why."""


class MCPServerNotFound(MCPError):
    pass


class MCPHealth(BaseModel):
    """Whether one MCP server can be used now, and if not, why."""

    name: str
    usable: bool
    status: str  # an MCPServerStatus value, or "not_configured"
    problem: Optional[str] = None


class MCPServerConfig(BaseModel):
    """Persisted configuration of a single MCP server."""

    name: str
    type: Literal["stdio", "http"] = "stdio"  # http: streamable HTTP
    command: Optional[str] = None
    args: List[str] = Field(default_factory=list)
    env: Dict[str, str] = Field(default_factory=dict)
    cwd: Optional[str] = None
    url: Optional[str] = None
    headers: Dict[str, str] = Field(default_factory=dict)  # values may use ${NAME}: an environment or .env variable
    tools: List[str] = Field(default_factory=list)  # only these of the server's tools are offered; empty: all
    enabled: bool = True

    def to_file_entry(self) -> Dict[str, Any]:
        """Serialize to the `mcpServers` JSON entry (name is the key)."""
        data = self.model_dump(exclude={"name"}, exclude_defaults=True)
        data["type"] = self.type
        return data


class MCPServerStatus(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


class MCPConnection:
    """Live state of one MCP server connection, owned by a dedicated asyncio task."""

    def __init__(self, config: MCPServerConfig):
        self.config = config
        self.status = MCPServerStatus.DISCONNECTED
        self.error: Optional[str] = None
        self.session: Optional[ClientSession] = None
        self.server_info: Optional[str] = None
        self.tools: List[Any] = []
        self.task: Optional[asyncio.Task] = None
        self.ready = asyncio.Event()
        self.stop = asyncio.Event()
        self.failures = 0  # reconnect attempts failed in a row
        self.retry_at = 0.0  # no reconnect attempt before this (the manager's clock)


_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand(value: str) -> str:
    """Replace ${NAME} with the environment variable NAME, or its value in .env, so secrets stay out of the config file."""
    def lookup(match: "re.Match") -> str:
        name = match.group(1)
        return os.environ.get(name) or dotenv_values(".env").get(name) or ""
    return _VARIABLE.sub(lookup, value)


def _sanitize(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)


def _content_to_text(block: Any) -> str:
    """Render a single MCP content block as text for the LLM."""
    block_type = getattr(block, "type", "")
    if block_type == "text":
        return block.text
    if block_type in ("image", "audio"):
        return f"[{block_type} content: {getattr(block, 'mime_type', 'unknown')}]"
    if block_type == "resource":
        resource = block.resource
        text = getattr(resource, "text", None)
        return text if text is not None else f"[embedded resource: {resource.uri}]"
    if block_type == "resource_link":
        return f"[resource link: {block.uri}]"
    return str(block)


class MCPManager:
    """
    Loads MCP server configs, maintains connections, and dispatches MCP tool calls. Management calls return
    a server's description and raise MCPServerNotFound or MCPError; tool calls return tool-result dicts.
    """

    def __init__(
        self,
        config_path: Optional[Path] = None,
        connect_timeout: float = 30.0,
        log_dir: Optional[Path] = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.config_path = config_path or settings.kilo_mcp_config
        self.log_dir = log_dir or self.config_path.parent / "mcp_logs"
        self.connect_timeout = connect_timeout
        self._clock = clock
        self.connections: Dict[str, MCPConnection] = {}
        # Exposed LLM tool name -> (server name, original MCP tool name)
        self._tool_index: Dict[str, Tuple[str, str]] = {}
        self._start_lock = asyncio.Lock()
        self._load_config()

    # ---- Configuration -------------------------------------------------

    def _load_config(self):
        if not self.config_path.exists():
            return
        data = json.loads(self.config_path.read_text(encoding="utf-8") or "{}")
        for name, entry in data.get("mcpServers", {}).items():
            entry = dict(entry)
            if "type" not in entry:
                entry["type"] = "stdio" if entry.get("command") else "http"
            if entry["type"] not in ("stdio", "http"):
                raise MCPError(
                    f"MCP server '{name}' in {self.config_path} has type '{entry['type']}'; only 'stdio' and 'http' "
                    "(streamable HTTP) are supported. Change or remove it."
                )
            self.connections[name] = MCPConnection(MCPServerConfig(name=name, **entry))

    def _save_config(self):
        data = {"mcpServers": {name: c.config.to_file_entry() for name, c in self.connections.items()}}
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.config_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp_path.replace(self.config_path)

    # ---- Lifecycle -----------------------------------------------------

    async def ensure_started(self):
        """
        Connect every enabled server that is not connected: all of them the first time, then (called before each
        turn) the ones that failed or dropped, each no sooner than its backoff allows (`reconnect`). A server that
        cannot connect is left in error; it never stops the others or the turn.
        """
        async with self._start_lock:
            names = [
                n for n, c in self.connections.items()
                if c.config.enabled and c.status not in (MCPServerStatus.CONNECTED, MCPServerStatus.CONNECTING)
            ]
            await asyncio.gather(*(self.reconnect(n) for n in names))

    async def shutdown(self):
        await asyncio.gather(*(self.disconnect(n, persist=False) for n in list(self.connections)))

    def _open_transport(self, config: MCPServerConfig, errlog):
        if config.type == "stdio":
            params = StdioServerParameters(
                command=config.command,
                args=config.args,
                env=config.env or None,
                cwd=config.cwd or str(settings.kilo_workspace),
            )
            return stdio_client(params, errlog=errlog)
        headers = {name: expand(value) for name, value in config.headers.items()} or None
        http_client = create_mcp_http_client(headers=headers)
        return streamable_http_client(config.url, http_client=http_client)

    async def _run_connection(self, conn: MCPConnection):
        """Own the transport + session context for the whole life of the connection."""
        config = conn.config
        try:
            async with AsyncExitStack() as stack:
                errlog = None
                if config.type == "stdio":
                    self.log_dir.mkdir(parents=True, exist_ok=True)
                    errlog = stack.enter_context(open(self.log_dir / f"{config.name}.log", "a", encoding="utf-8"))
                read, write = await stack.enter_async_context(self._open_transport(config, errlog))
                session = await stack.enter_async_context(ClientSession(read, write))
                init = await session.initialize()
                conn.server_info = f"{init.server_info.name} {init.server_info.version}".strip()
                conn.tools = await self._list_all_tools(session)
                conn.session = session
                conn.status = MCPServerStatus.CONNECTED
                conn.error = None
                self._rebuild_tool_index()
                conn.ready.set()
                await conn.stop.wait()
        except BaseException as e:  # noqa: BLE001 - surface any transport/session failure
            if conn.status != MCPServerStatus.DISCONNECTED and not isinstance(e, asyncio.CancelledError):
                conn.status = MCPServerStatus.ERROR
                conn.error = f"{type(e).__name__}: {e}"
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
        finally:
            conn.session = None
            conn.tools = []
            if conn.status == MCPServerStatus.CONNECTED:
                conn.status = MCPServerStatus.DISCONNECTED
            self._rebuild_tool_index()
            conn.ready.set()

    @staticmethod
    async def _list_all_tools(session: ClientSession) -> List[Any]:
        tools: List[Any] = []
        cursor = None
        while True:
            params = mcp_types.PaginatedRequestParams(cursor=cursor) if cursor else None
            result = await session.list_tools(params=params)
            tools.extend(result.tools)
            cursor = result.next_cursor
            if not cursor:
                return tools

    def _conn(self, name: str) -> MCPConnection:
        conn = self.connections.get(name)
        if conn is None:
            raise MCPServerNotFound(f"MCP server '{name}' is not configured.")
        return conn

    async def connect(self, name: str) -> Dict[str, Any]:
        conn = self._conn(name)
        if conn.status == MCPServerStatus.CONNECTED:
            return self.describe(name)

        conn.status = MCPServerStatus.CONNECTING
        conn.error = None
        conn.ready = asyncio.Event()
        conn.stop = asyncio.Event()
        conn.task = asyncio.create_task(self._run_connection(conn))
        try:
            await asyncio.wait_for(conn.ready.wait(), timeout=self.connect_timeout)
        except asyncio.TimeoutError:
            conn.task.cancel()
            conn.status = MCPServerStatus.ERROR
            conn.error = f"Timed out after {self.connect_timeout}s while connecting."

        if conn.status != MCPServerStatus.CONNECTED:
            raise MCPError(f"Failed to connect to MCP server '{name}': {conn.error}")
        return self.describe(name)

    async def disconnect(self, name: str, persist: bool = True) -> Dict[str, Any]:
        conn = self._conn(name)
        conn.status = MCPServerStatus.DISCONNECTED
        conn.stop.set()
        if conn.task:
            try:
                await asyncio.wait_for(asyncio.shield(conn.task), timeout=10)
            except asyncio.TimeoutError:
                conn.task.cancel()
            conn.task = None
        if persist and conn.config.enabled:
            conn.config.enabled = False
            self._save_config()
        return self.describe(name)

    # ---- Health --------------------------------------------------------

    async def health(self, name: str) -> MCPHealth:
        """Whether server `name` can be used now. A connected server must answer a ping; one that does not is dropped."""
        conn = self.connections.get(name)
        if conn is None:
            return MCPHealth(name=name, usable=False, status="not_configured", problem="not configured")
        if conn.status == MCPServerStatus.CONNECTED and conn.session is not None:
            try:
                await asyncio.wait_for(conn.session.send_ping(), timeout=PING_TIMEOUT)
                return MCPHealth(name=name, usable=True, status=conn.status.value)
            except Exception as e:  # noqa: BLE001 - a dead transport fails in many ways
                conn.status = MCPServerStatus.ERROR
                conn.error = f"Not answering: {type(e).__name__}: {e}".rstrip(": ")
                conn.stop.set()
        problem = "disabled" if not conn.config.enabled else conn.error or conn.status.value
        return MCPHealth(name=name, usable=False, status=conn.status.value, problem=problem)

    async def reconnect(self, name: str) -> MCPHealth:
        """
        Connect an enabled server that is not usable, unless an attempt failed recently: after each failure in
        a row the next attempt waits longer (RECONNECT_BACKOFF). Returns the server's health afterwards.
        """
        conn = self.connections.get(name)
        if conn is None or not conn.config.enabled or self._clock() < conn.retry_at:
            return await self.health(name)
        health = await self.health(name)
        if health.usable:
            return health
        await self.disconnect(name, persist=False)  # ends a dropped connection's task before a new one starts
        try:
            await self.connect(name)
        except MCPError:
            pass
        health = await self.health(name)
        if health.usable:
            conn.failures, conn.retry_at = 0, 0.0
        else:
            conn.failures += 1
            conn.retry_at = self._clock() + RECONNECT_BACKOFF[min(conn.failures, len(RECONNECT_BACKOFF)) - 1]
        return health

    # ---- Server management --------------------------------------------

    async def add_server(self, config: MCPServerConfig, connect: bool = True) -> Dict[str, Any]:
        if not SERVER_NAME_PATTERN.match(config.name):
            raise MCPError("Server name must be 1-32 chars of letters, digits, '_' or '-'.")
        if config.name in self.connections:
            raise MCPError(f"MCP server '{config.name}' already exists. Remove it first to replace it.")
        if config.type == "stdio" and not config.command:
            raise MCPError("A 'command' is required for stdio MCP servers.")
        if config.type == "http" and not config.url:
            raise MCPError("A 'url' is required for http MCP servers.")

        self.connections[config.name] = MCPConnection(config)
        self._save_config()
        if connect and config.enabled:
            try:
                return await self.connect(config.name)
            except MCPError as e:
                raise MCPError(f"{e} The server was saved; fix the issue and use action='connect', or action='remove'.")
        return self.describe(config.name)

    async def remove_server(self, name: str) -> None:
        await self.disconnect(name, persist=False)
        del self.connections[name]
        self._save_config()
        self._rebuild_tool_index()

    async def connect_and_enable(self, name: str) -> Dict[str, Any]:
        conn = self._conn(name)
        if not conn.config.enabled:
            conn.config.enabled = True
            self._save_config()
        return await self.connect(name)

    async def restart(self, name: str) -> Dict[str, Any]:
        await self.disconnect(name, persist=False)
        return await self.connect(name)

    def describe(self, name: str, include_tools: bool = False) -> Dict[str, Any]:
        conn = self._conn(name)
        config = conn.config
        offered = [t for t in conn.tools if not config.tools or t.name in config.tools]
        info: Dict[str, Any] = {
            "name": name,
            "type": config.type,
            "target": " ".join([config.command or "", *config.args]).strip() if config.type == "stdio" else config.url,
            "enabled": config.enabled,
            "server_status": conn.status.value,
            "server_info": conn.server_info,
            "tool_count": len(offered),
            "error": conn.error,
        }
        exposed = {v: k for k, v in self._tool_index.items()}
        if include_tools:
            info["tools"] = [
                {
                    "name": exposed.get((name, t.name), t.name),
                    "description": (t.description or "").strip(),
                    "input_schema": t.input_schema,
                }
                for t in offered
            ]
        else:
            info["tools"] = [exposed.get((name, t.name), t.name) for t in offered]
        return info

    def list_servers(self) -> List[Dict[str, Any]]:
        return [self.describe(name) for name in self.connections]

    def read_log(self, name: str, tail: int = 50) -> str:
        """The last `tail` lines a stdio server wrote to stderr."""
        self._conn(name)
        log_path = self.log_dir / f"{name}.log"
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines() if log_path.exists() else []
        return "\n".join(lines[-tail:])

    # ---- Tool exposure & dispatch --------------------------------------

    def _rebuild_tool_index(self):
        index: Dict[str, Tuple[str, str]] = {}
        for name, conn in self.connections.items():
            for tool in conn.tools:
                if conn.config.tools and tool.name not in conn.config.tools:
                    continue
                exposed = f"{MCP_TOOL_PREFIX}{_sanitize(name)}__{_sanitize(tool.name)}"[:MAX_TOOL_NAME_LENGTH]
                if exposed not in index:
                    index[exposed] = (name, tool.name)
        self._tool_index = index

    def has_tool(self, exposed_name: str) -> bool:
        return exposed_name in self._tool_index

    def tool_definitions(self) -> List[Dict[str, Any]]:
        """Anthropic tool definitions for every connected MCP tool."""
        definitions = []
        for exposed, (server_name, tool_name) in self._tool_index.items():
            conn = self.connections[server_name]
            tool = next((t for t in conn.tools if t.name == tool_name), None)
            if tool is None:
                continue
            schema = dict(tool.input_schema or {})
            schema.setdefault("type", "object")
            schema.setdefault("properties", {})
            description = (tool.description or "").strip() or f"Tool '{tool_name}'"
            definitions.append({
                "name": exposed,
                "description": f"[MCP server: {server_name}] {description}",
                "input_schema": schema,
            })
        return definitions

    async def call_tool(self, exposed_name: str, args: Dict[str, Any], timeout: float = 120.0) -> Dict[str, Any]:
        target = self._tool_index.get(exposed_name)
        if not target:
            return {"status": "error", "message": f"Unknown MCP tool '{exposed_name}'."}
        server_name, tool_name = target
        conn = self.connections[server_name]
        if conn.session is None or conn.status != MCPServerStatus.CONNECTED:
            return {"status": "error", "message": f"MCP server '{server_name}' is not connected ({conn.status.value})."}

        try:
            result = await conn.session.call_tool(tool_name, args or {}, read_timeout_seconds=timeout)
        except Exception as e:
            return {
                "status": "error",
                "message": f"MCP tool '{tool_name}' on server '{server_name}' failed: {type(e).__name__}: {e}",
            }

        blocks = getattr(result, "content", None) or []
        text = "\n".join(_content_to_text(b) for b in blocks)
        structured = getattr(result, "structured_content", None)
        if not text and structured is not None:
            text = json.dumps(structured, indent=2, ensure_ascii=False)

        if getattr(result, "is_error", False):
            return {"status": "error", "server": server_name, "tool": tool_name, "message": text or "MCP tool returned an error."}
        return {"status": "success", "server": server_name, "tool": tool_name, "content": text}

