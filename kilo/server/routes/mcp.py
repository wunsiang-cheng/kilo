"""MCP server management API routes."""

from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Query

from kilo.assembly import get_kilo
from kilo.tools.mcp_manager import MCPError, MCPServerConfig, MCPServerNotFound

router = APIRouter(prefix="/mcp", tags=["mcp"])


def _http_error(e: MCPError) -> HTTPException:
    return HTTPException(status_code=404 if isinstance(e, MCPServerNotFound) else 400, detail=str(e))


@router.get("/servers")
async def list_servers() -> List[Dict[str, Any]]:
    """List configured MCP servers with connection status."""
    return get_kilo().mcp.list_servers()


@router.post("/servers")
async def add_server(config: MCPServerConfig) -> Dict[str, Any]:
    """Register (and persist) a new MCP server, connecting it if enabled."""
    try:
        return await get_kilo().mcp.add_server(config)
    except MCPError as e:
        raise _http_error(e)


@router.delete("/servers/{name}")
async def remove_server(name: str) -> Dict[str, Any]:
    """Disconnect and delete an MCP server."""
    try:
        await get_kilo().mcp.remove_server(name)
    except MCPError as e:
        raise _http_error(e)
    return {"name": name, "message": f"MCP server '{name}' removed."}


@router.get("/servers/{name}/tools")
async def get_server_tools(name: str) -> Dict[str, Any]:
    """List tools exposed by an MCP server."""
    try:
        return get_kilo().mcp.describe(name, include_tools=True)
    except MCPError as e:
        raise _http_error(e)


@router.get("/servers/{name}/health")
async def server_health(name: str) -> Dict[str, Any]:
    """Whether an MCP server can be used now (a connected server must answer a ping), and if not, why."""
    return (await get_kilo().mcp.health(name)).model_dump()


@router.post("/servers/{name}/connect")
async def connect_server(name: str) -> Dict[str, Any]:
    """Enable and connect an MCP server."""
    try:
        return await get_kilo().mcp.connect_and_enable(name)
    except MCPError as e:
        raise _http_error(e)


@router.post("/servers/{name}/disconnect")
async def disconnect_server(name: str) -> Dict[str, Any]:
    """Disconnect and disable an MCP server."""
    try:
        return await get_kilo().mcp.disconnect(name)
    except MCPError as e:
        raise _http_error(e)


@router.get("/servers/{name}/logs")
async def get_server_logs(name: str, tail: int = Query(50, ge=1, le=1000)) -> Dict[str, Any]:
    """Get stderr logs of a stdio MCP server."""
    try:
        return {"name": name, "logs": get_kilo().mcp.read_log(name, tail=tail)}
    except MCPError as e:
        raise _http_error(e)
