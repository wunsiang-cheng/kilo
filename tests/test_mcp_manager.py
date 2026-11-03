"""MCP over a real stdio connection to tests/fixtures/echo_mcp_server.py."""

import json
import sys
from pathlib import Path

import pytest

from kilo.tools.base import Tool
from kilo.tools.mcp_manager import MCPError, MCPManager, MCPServerConfig, MCPServerNotFound, MCPServerStatus, expand
from kilo.tools.mcp_tools import execute_mcp_tool
from kilo.tools.toolbox import INTENT, Toolbox

ECHO = str(Path(__file__).parent / "fixtures" / "echo_mcp_server.py")
TOOL = "mcp__echo__echo"


def echo_config(**fields) -> MCPServerConfig:
    return MCPServerConfig(**{"name": "echo", "command": sys.executable, "args": [ECHO], **fields})


@pytest.fixture
def config_path(tmp_path) -> Path:
    return tmp_path / "mcp_servers.json"


@pytest.fixture
async def manager(config_path, tmp_path):
    built = MCPManager(config_path=config_path, log_dir=tmp_path / "logs")
    yield built
    await built.shutdown()


async def test_add_connect_and_call_tool(manager):
    await manager.add_server(echo_config())
    assert manager.has_tool(TOOL)
    assert TOOL in [definition["name"] for definition in manager.tool_definitions()]
    result = await manager.call_tool(TOOL, {"text": "hi"})
    assert result["status"] == "success" and result["content"] == "echo: hi"


async def test_tool_error_is_reported_not_raised(manager):
    await manager.add_server(echo_config())
    result = await manager.call_tool("mcp__echo__fail", {})
    assert result["status"] == "error" and "this tool failed" in result["message"]


async def test_unknown_tool_is_reported(manager):
    assert (await manager.call_tool("mcp__echo__nope", {}))["status"] == "error"


async def test_toolbox_routes_mcp_calls(manager):
    await manager.add_server(echo_config())
    toolbox = Toolbox([], manager)
    assert TOOL in [d["function"]["name"] for d in toolbox.definitions()]
    assert json.loads(await toolbox.execute(TOOL, {"text": "hi"}))["content"] == "echo: hi"


def recording_tool(name, seen, properties=None):
    async def run(args):
        seen.append(args)
        return "{}"

    schema = {"type": "object", "properties": properties or {"path": {"type": "string"}}, "required": ["path"]}
    return Tool(definition={"name": name, "description": "d", "parameters": schema}, execute=run)


async def test_every_tool_takes_a_required_intent(manager):
    await manager.add_server(echo_config(tools=["echo"]))
    tool = recording_tool("read", [])
    definitions = Toolbox([tool], manager).definitions()
    assert [d["type"] for d in definitions] == ["function", "function"]
    assert [d["function"]["name"] for d in definitions] == ["read", TOOL]
    for definition in definitions:
        schema = definition["function"]["parameters"]
        assert list(schema["properties"])[0] == INTENT and schema["required"][0] == INTENT
    assert definitions[0]["function"]["parameters"]["required"] == [INTENT, "path"]
    assert INTENT not in tool.definition["parameters"]["properties"]  # the tool's own definition is left alone


async def test_mcp_tools_are_described_with_parameters(manager):
    await manager.add_server(echo_config())
    definition = next(d for d in manager.tool_definitions() if d["name"] == TOOL)
    assert set(definition) == {"name", "description", "parameters"} and definition["parameters"]["type"] == "object"


async def test_builtin_tools_are_described_with_parameters(kilo):
    for tool in kilo.tools:
        assert set(tool.definition) == {"name", "description", "parameters"}, tool.name


async def test_intent_is_not_passed_to_the_tool(manager):
    await manager.add_server(echo_config())
    seen = []
    toolbox = Toolbox([recording_tool("read", seen)], manager)
    await toolbox.execute("read", {INTENT: "look at the config", "path": "a"})
    assert seen == [{"path": "a"}]
    result = json.loads(await toolbox.execute(TOOL, {INTENT: "say hi", "text": "hi"}))
    assert result["content"] == "echo: hi"


async def test_a_tool_with_its_own_intent_keeps_it(manager):
    seen = []
    tool = recording_tool("plan", seen, properties={INTENT: {"type": "string", "description": "the tool's own"}})
    toolbox = Toolbox([tool], manager)
    assert toolbox.definitions()[0] == {"type": "function", "function": tool.definition}
    await toolbox.execute("plan", {INTENT: "x"})
    assert seen == [{INTENT: "x"}]


async def test_toolbox_refuses_reserved_and_duplicate_names(manager):
    async def run(args):
        return "{}"

    tool = lambda name: Tool(definition={"name": name}, execute=run)
    with pytest.raises(ValueError, match="reserved"):
        Toolbox([tool("mcp__x")], manager)
    with pytest.raises(ValueError, match="more than once"):
        Toolbox([tool("a"), tool("a")], manager)


async def test_tool_filter_limits_offered_tools(manager):
    await manager.add_server(echo_config(tools=["echo"]))
    assert [d["name"] for d in manager.tool_definitions()] == [TOOL]
    assert (await manager.call_tool("mcp__echo__fail", {}))["status"] == "error"
    assert not manager.has_tool("mcp__echo__fail")


async def test_config_persists_and_reloads(manager, config_path, tmp_path):
    await manager.add_server(echo_config(), connect=False)
    saved = json.loads(config_path.read_text(encoding="utf-8"))["mcpServers"]["echo"]
    assert saved["type"] == "stdio" and saved["args"] == [ECHO]

    again = MCPManager(config_path=config_path, log_dir=tmp_path / "logs")
    await again.ensure_started()
    try:
        assert again.has_tool(TOOL)
    finally:
        await again.shutdown()


async def test_remove_server_drops_tools_and_config(manager, config_path):
    await manager.add_server(echo_config())
    await manager.remove_server("echo")
    assert not manager.has_tool(TOOL)
    assert json.loads(config_path.read_text(encoding="utf-8"))["mcpServers"] == {}


async def test_disconnect_stays_off_after_reload(manager, config_path, tmp_path):
    await manager.add_server(echo_config())
    await manager.disconnect("echo")
    assert not manager.has_tool(TOOL)
    again = MCPManager(config_path=config_path, log_dir=tmp_path / "logs")
    await again.ensure_started()
    try:
        assert not again.has_tool(TOOL)
    finally:
        await again.shutdown()


async def test_bad_command_reports_error_status(manager):
    config = echo_config(name="broken", command="no-such-program-here", args=[])
    with pytest.raises(MCPError):
        await manager.add_server(config)
    assert manager.connections["broken"].status == MCPServerStatus.ERROR
    health = await manager.health("broken")
    assert not health.usable and health.problem
    assert manager.list_servers()[0]["name"] == "broken"  # kept, to fix or remove


async def test_a_broken_server_does_not_stop_the_others(manager):
    with pytest.raises(MCPError):
        await manager.add_server(echo_config(name="broken", command="no-such-program-here", args=[]))
    await manager.add_server(echo_config())
    assert (await manager.call_tool(TOOL, {"text": "hi"}))["status"] == "success"


async def test_duplicate_and_invalid_servers_refused(manager):
    await manager.add_server(echo_config(), connect=False)
    with pytest.raises(MCPError, match="already exists"):
        await manager.add_server(echo_config(), connect=False)
    with pytest.raises(MCPError, match="Server name"):
        await manager.add_server(echo_config(name="bad name"), connect=False)
    with pytest.raises(MCPError, match="command"):
        await manager.add_server(MCPServerConfig(name="none"), connect=False)
    with pytest.raises(MCPError, match="url"):
        await manager.add_server(MCPServerConfig(name="none", type="http"), connect=False)


async def test_unknown_server_not_found(manager):
    with pytest.raises(MCPServerNotFound):
        await manager.connect("ghost")


def test_expand_reads_environment(monkeypatch):
    monkeypatch.setenv("KILO_TEST_TOKEN", "secret")
    assert expand("Bearer ${KILO_TEST_TOKEN}") == "Bearer secret"
    assert expand("${KILO_TEST_UNDEFINED}") == ""


def test_unknown_type_in_config_refused(config_path):
    config_path.write_text('{"mcpServers": {"x": {"type": "sse", "url": "http://x"}}}', encoding="utf-8")
    with pytest.raises(MCPError, match="sse"):
        MCPManager(config_path=config_path)


def test_type_is_inferred_from_the_entry(config_path):
    config_path.write_text(
        '{"mcpServers": {"a": {"command": "x"}, "b": {"url": "http://x"}}}', encoding="utf-8",
    )
    connections = MCPManager(config_path=config_path).connections
    assert connections["a"].config.type == "stdio" and connections["b"].config.type == "http"


async def test_agent_tool_adds_and_lists_servers(manager):
    added = json.loads(await execute_mcp_tool(manager, {
        "action": "add", "name": "echo", "command": sys.executable, "args": [ECHO],
    }))
    assert added["status"] == "success"
    listed = json.loads(await execute_mcp_tool(manager, {"action": "list"}))
    assert [s["name"] for s in listed["servers"]] == ["echo"]


async def test_agent_tool_needs_a_name_and_known_action(manager):
    assert json.loads(await execute_mcp_tool(manager, {"action": "add"}))["status"] == "error"
    assert json.loads(await execute_mcp_tool(manager, {"action": "nope", "name": "x"}))["status"] == "error"
