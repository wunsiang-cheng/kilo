"""A small stdio MCP server for the MCP manager's tests."""

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

server = MCPServer("echo")


@server.tool(description="Return the text.")
def echo(text: str) -> str:
    return f"echo: {text}"


@server.tool(description="Always fails.")
def fail() -> str:
    raise ToolError("this tool failed")


server.run("stdio")
