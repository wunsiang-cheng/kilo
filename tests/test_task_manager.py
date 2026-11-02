"""Real commands through the TaskManager and the shell tool. Each runs a Python script, so it works on every platform."""

import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

import pytest

from fakes import until
from kilo.tools.shell import execute_shell_tool
from kilo.tools.task_manager import TaskManager


@pytest.fixture
def manager(tmp_path, workspace):
    return TaskManager(data_dir=tmp_path)


def py(workspace: Path, code: str) -> str:
    """A shell command running `code` as a script."""
    script = workspace / f"script_{uuid.uuid4().hex[:6]}.py"
    script.write_text(code, encoding="utf-8")
    return f'"{sys.executable}" "{script}"'


async def test_run_captures_output_and_exit_code(manager, workspace):
    result = await manager.run(py(workspace, "print('out')\nimport sys\nprint('err', file=sys.stderr)"))
    assert result.status == "completed" and result.exit_code == 0
    assert result.stdout.strip() == "out" and result.stderr.strip() == "err"


async def test_nonzero_exit_is_failed(manager, workspace):
    result = await manager.run(py(workspace, "raise SystemExit(3)"))
    assert result.status == "failed" and result.exit_code == 3


async def test_runs_in_workspace(manager, workspace):
    result = await manager.run(py(workspace, "import os\nprint(os.getcwd())"))
    assert Path(result.stdout.strip()).resolve() == workspace.resolve()


async def test_timeout_stops_the_process(manager, workspace):
    code = "import time\nprint('started', flush=True)\ntime.sleep(30)"
    started = time.monotonic()
    result = await manager.run(py(workspace, code), timeout=2)
    assert result.status == "timed_out" and result.exit_code == -1
    assert time.monotonic() - started < 12
    assert "started" in result.stdout


async def test_no_timeout_given_uses_the_configured_default(manager, workspace, monkeypatch):
    monkeypatch.setattr("kilo.config.settings.kilo_default_timeout", 1)
    result = await manager.run(py(workspace, "import time\ntime.sleep(30)"))
    assert result.status == "timed_out" and "after 1 seconds" in result.message


HEARTBEAT = """
import subprocess, sys, time
child = "import time\\nwhile True:\\n    open('beat.txt', 'a').write('.')\\n    time.sleep(0.05)"
subprocess.Popen([sys.executable, "-c", child])
time.sleep(30)
"""


async def test_timeout_kills_child_processes(manager, workspace):
    await manager.run(py(workspace, HEARTBEAT), timeout=2)
    beat = workspace / "beat.txt"
    assert beat.exists()
    size = beat.stat().st_size
    await asyncio.sleep(0.5)
    assert beat.stat().st_size == size  # nothing is writing any more


async def test_non_utf8_output_does_not_crash(manager, workspace):
    code = "import sys\nsys.stdout.buffer.write(b'\\xb4\\xfa\\xff ok')"
    result = await manager.run(py(workspace, code))
    assert result.status == "completed" and result.stdout.endswith("ok")


async def test_flood_of_output_is_capped(manager, workspace, monkeypatch):
    monkeypatch.setattr("kilo.config.settings.kilo_tool_output_max_chars", 10_000)
    code = "import sys\nsys.stdout.write('x' * 1_000_000 + 'the-end')"
    result = await manager.run(py(workspace, code))
    assert len(result.stdout) < 30_000
    assert "omitted" in result.stdout and result.stdout.endswith("the-end")


async def test_cancelled_run_kills_command(manager, workspace):
    running = asyncio.create_task(manager.run(py(workspace, HEARTBEAT), timeout=30))
    beat = workspace / "beat.txt"
    await until(beat.exists)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    await until(lambda: not manager._terminations)
    size = beat.stat().st_size
    await asyncio.sleep(0.5)
    assert beat.stat().st_size == size
    assert manager.list_tasks()[0].status.value == "killed"


async def test_list_filters_by_status(manager, workspace):
    await manager.run(py(workspace, "pass"))
    await manager.run(py(workspace, "raise SystemExit(1)"))
    assert [t.exit_code for t in manager.list_tasks(status="failed")] == [1]


async def shell(manager, **args):
    return json.loads(await execute_shell_tool(manager, args))


async def test_shell_tool_runs_a_command(manager, workspace):
    result = await shell(manager, command=py(workspace, "print('hi')"))
    assert result["status"] == "completed" and result["stdout"].strip() == "hi"


@pytest.mark.parametrize("args", [{}, {"action": "status"}, {"action": "logs"}, {"action": "kill"}, {"action": "nope"}])
async def test_shell_tool_rejects_incomplete_calls(manager, args):
    assert (await shell(manager, **args))["status"] == "error"


async def test_unknown_task_id_reports_error(manager):
    assert (await shell(manager, action="status", task_id="task_x"))["status"] == "error"
    assert (await shell(manager, action="logs", task_id="task_x"))["status"] == "error"
