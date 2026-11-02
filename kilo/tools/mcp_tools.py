"""MCP management tool: lets the agent add, remove, connect, and inspect MCP servers."""

import json
from typing import Any, Dict

from pydantic import ValidationError

from kilo.tools.base import Tool
from kilo.tools.mcp_manager import MCPError, MCPManager, MCPServerConfig

MCP_TOOL_DEFINITION = {
    "name": "mcp",
    "description": (
        "Manage MCP (Model Context Protocol) servers. Connected servers expose their tools to you as "
        "'mcp__<server>__<tool>', available from your next step onward. "
        "Actions: 'list' (all servers and status), 'add' (register, persist, and connect a server), "
        "'remove' (disconnect and delete), 'connect' / 'disconnect' (toggle and persist enabled state), "
        "'restart', 'tools' (detailed tool list with input schemas), 'logs' (stderr of a stdio server). "
        "When a task needs an external integration, 'list' first and 'add' a server only if none fits; when a stdio "
        "server fails to connect, read its 'logs'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "add", "remove", "connect", "disconnect", "restart", "tools", "logs"],
                "description": "Management action to perform.",
            },
            "name": {
                "type": "string",
                "description": "Server name (letters, digits, '_' or '-'). Required for every action except 'list'.",
            },
            "type": {
                "type": "string",
                "enum": ["stdio", "http"],
                "description": "Transport for 'add': 'stdio' (local command) or 'http' (streamable HTTP). Default: 'http' with only a `url`, otherwise 'stdio'.",
            },
            "command": {
                "type": "string",
                "description": "Executable for stdio servers (e.g. 'npx', 'uvx', 'python').",
            },
            "args": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Arguments for the stdio command (e.g. ['-y', '@modelcontextprotocol/server-filesystem', '/tmp']).",
            },
            "env": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": "Extra environment variables for stdio servers (e.g. API keys).",
            },
            "cwd": {
                "type": "string",
                "description": "Working directory for stdio servers (default: workspace).",
            },
            "url": {
                "type": "string",
                "description": "Endpoint URL for http servers.",
            },
            "headers": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": "HTTP headers for http servers (e.g. Authorization).",
            },
            "tail": {
                "type": "integer",
                "description": "Number of log lines for 'logs' (default: 50).",
                "default": 50,
            },
        },
        "required": ["action"],
    },
}


async def execute_mcp_tool(manager: MCPManager, args: Dict[str, Any]) -> str:
    """Execute an MCP management action."""
    await manager.ensure_started()

    action = args.get("action", "list")
    name = (args.get("name") or "").strip()

    if action == "list":
        return json.dumps({"status": "success", "action": action, "servers": manager.list_servers()})

    if not name:
        return json.dumps({"status": "error", "message": f"Parameter 'name' is required for action '{action}'."})

    try:
        if action == "add":
            try:
                config = MCPServerConfig(
                    name=name,
                    type=args.get("type") or ("http" if args.get("url") and not args.get("command") else "stdio"),
                    command=args.get("command"),
                    args=args.get("args") or [],
                    env=args.get("env") or {},
                    cwd=args.get("cwd"),
                    url=args.get("url"),
                    headers=args.get("headers") or {},
                )
            except ValidationError as e:
                return json.dumps({"status": "error", "message": f"Invalid MCP server configuration: {e}"})
            result = await manager.add_server(config)
        elif action == "remove":
            await manager.remove_server(name)
            result = {"name": name, "message": f"MCP server '{name}' removed."}
        elif action == "connect":
            result = await manager.connect_and_enable(name)
        elif action == "disconnect":
            result = await manager.disconnect(name)
        elif action == "restart":
            result = await manager.restart(name)
        elif action == "tools":
            result = manager.describe(name, include_tools=True)
        elif action == "logs":
            try:
                tail = int(args.get("tail", 50))
            except (TypeError, ValueError):
                tail = 50
            result = {"name": name, "logs": manager.read_log(name, tail=tail)}
        else:
            return json.dumps({"status": "error", "message": f"Unknown MCP action '{action}'."})
    except MCPError as e:
        return json.dumps({"status": "error", "action": action, "message": str(e)})

    return json.dumps({"status": "success", "action": action, **result}, default=str)


def mcp_tool(manager: MCPManager) -> Tool:
    """The `mcp` tool, managing `manager`'s servers."""
    return Tool(
        definition=MCP_TOOL_DEFINITION,
        execute=lambda args: execute_mcp_tool(manager, args),
    )
