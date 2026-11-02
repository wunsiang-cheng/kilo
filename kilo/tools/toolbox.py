"""Toolbox: the agent's tool catalog and dispatcher (built-in tools plus live MCP tools)."""

import json
from typing import Any, Dict, Iterable, List

from kilo.tools.base import Tool
from kilo.tools.mcp_manager import MCP_TOOL_PREFIX, MCPManager
from kilo.tools.output_limit import limit_output


# Every tool the agent calls takes this parameter too: what the call is for, in one sentence. The web terminal shows
# it in place of the call's input and output. It is removed before the tool runs (unless the tool has its own).
INTENT = "intent"
INTENT_PROPERTY = {
    "type": "string",
    "description": "One short sentence saying what this call is for. The user sees it instead of the call's input and output.",
}


def with_intent(definition: Dict[str, Any]) -> Dict[str, Any]:
    """A tool definition with the required `intent` parameter added (left as it is if the tool has one already)."""
    schema = dict(definition.get("input_schema") or {})
    properties = schema.get("properties") or {}
    if INTENT in properties:
        return definition
    schema["properties"] = {INTENT: INTENT_PROPERTY, **properties}  # first, so the call states it before its input
    schema["required"] = [INTENT, *schema.get("required", [])]
    return {**definition, "input_schema": schema}


def _error(message: str) -> str:
    return json.dumps({"status": "error", "message": message})


class Toolbox:
    """
    Every tool the agent can call. Built-in tools are fixed at construction; MCP tools
    come and go with the MCP manager's connections. Results are always JSON strings, and
    failures are reported as {"status": "error", "message": ...} rather than raised.
    """

    def __init__(self, tools: Iterable[Tool], mcp_manager: MCPManager):
        self._tools: Dict[str, Tool] = {}
        self._names: Dict[str, str] = {}
        for tool in tools:
            for name in (tool.name, *tool.aliases):
                if name in self._names:
                    raise ValueError(f"Tool name '{name}' is declared more than once.")
                if name.startswith(MCP_TOOL_PREFIX):
                    raise ValueError(f"Tool name '{name}' uses the reserved MCP prefix '{MCP_TOOL_PREFIX}'.")
                self._names[name] = tool.name
            self._tools[tool.name] = tool
        self._mcp = mcp_manager

    async def ensure_started(self) -> None:
        """Connect configured MCP servers so their tools are available."""
        await self._mcp.ensure_started()

    def definitions(self) -> List[Dict[str, Any]]:
        """Anthropic tool definitions of built-in tools and connected MCP tools, each taking an `intent`."""
        own = [tool.definition for tool in self._tools.values()] + self._mcp.tool_definitions()
        return [with_intent(definition) for definition in own]

    def _has_own_intent(self, name: str) -> bool:
        """Whether the tool declares an `intent` parameter of its own (then it gets the call's)."""
        canonical = self._names.get(name)
        if canonical is not None:
            definition = self._tools[canonical].definition
        else:
            definition = next((d for d in self._mcp.tool_definitions() if d["name"] == name), {})
        return INTENT in (definition.get("input_schema") or {}).get("properties", {})

    def canonical_name(self, name: str) -> str:
        """The canonical name for a built-in tool alias; any other name is returned unchanged."""
        return self._names.get(name, name)

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        """
        Execute a tool by name or alias and return its JSON string result, as the model will see it:
        within KILO_TOOL_OUTPUT_MAX_CHARS (see `limit_output`). The call's `intent` is not passed on to the tool.
        """
        if INTENT in args and not self._has_own_intent(name):
            args = {key: value for key, value in args.items() if key != INTENT}
        return limit_output(name, await self._execute(name, args))

    async def _execute(self, name: str, args: Dict[str, Any]) -> str:
        if name.startswith(MCP_TOOL_PREFIX) and self._mcp.has_tool(name):
            try:
                return json.dumps(await self._mcp.call_tool(name, args), default=str)
            except Exception as e:
                return _error(f"Unexpected error while executing MCP tool '{name}': {str(e)}")

        canonical_name = self._names.get(name)
        if canonical_name is None:
            return _error(f"Unknown tool '{name}'. Available tools: {', '.join(self._tools)}.")

        try:
            return await self._tools[canonical_name].execute(args)
        except Exception as e:
            return _error(f"Unexpected error while executing tool '{name}': {str(e)}")

