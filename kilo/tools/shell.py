"""Shell tool definition and execution router for AI Agent."""

import json
import platform
import sys
from typing import Any, Dict
from kilo.config import settings
from kilo.tools.base import Tool
from kilo.tools.task_manager import TaskManager

SHELL_NAME = "cmd.exe" if sys.platform == "win32" else "sh"  # what asyncio.create_subprocess_shell runs

SHELL_TOOL_DEFINITION = {
    "name": "shell",
    "description": (
        "Run commands with full access to this computer, or look up the commands already run. Use it for programs, "
        "tests, builds and git; reading, searching and editing files have tools of their own. "
        f"Commands run in {SHELL_NAME} on {platform.system()}, starting in the workspace. Each call is a new process, so "
        "`cd` and environment variables do not carry over to the next. Nothing can type into a command: one that waits "
        "for input runs until its timeout. A command past its `timeout` is stopped, and what it printed comes back."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["run", "list", "status", "logs"],
                "description": (
                    "The operation to perform: 'run' to execute a command; 'list' to show recent tasks; "
                    "'status' to inspect a specific task; 'logs' to view output logs of a task."
                ),
                "default": "run",
            },
            "command": {
                "type": "string",
                "description": "The exact shell command line string to execute. Required when action is 'run'.",
            },
            "timeout": {
                "type": "integer",
                "description": (
                    f"Maximum seconds to wait for the command before timing out (default: {settings.kilo_default_timeout})."
                ),
                "default": settings.kilo_default_timeout,
            },
            "task_id": {
                "type": "string",
                "description": "The ID of the task to inspect or read logs from. Required for 'status' and 'logs'.",
            },
            "tail": {
                "type": "integer",
                "description": "Number of lines from end of task log to return (default: 50). Used with 'logs'.",
                "default": 50,
            },
        },
        "required": [],
    },
}


async def execute_shell_tool(task_manager: TaskManager, args: Dict[str, Any]) -> str:
    """Execute the shell tool based on provided arguments and return a string result for LLM."""
    action = args.get("action", "run")

    if action == "run":
        command = args.get("command")
        if not command:
            return json.dumps({"status": "error", "message": "Missing 'command' parameter for action 'run'."})

        timeout = args.get("timeout")
        if timeout is not None:
            try:
                timeout = int(timeout)
            except ValueError:
                timeout = None  # run then uses KILO_DEFAULT_TIMEOUT

        res = await task_manager.run(command=command, timeout=timeout)
        return json.dumps({
            "status": res.status,
            "task_id": res.task_id,
            "command": res.command,
            "exit_code": res.exit_code,
            "duration_seconds": res.duration_seconds,
            "stdout": res.stdout,
            "stderr": res.stderr,
            "message": res.message,
        })

    elif action == "list":
        status_filter = args.get("status")
        tasks = task_manager.list_tasks(status=status_filter)
        result = [
            {
                "task_id": t.task_id,
                "command": t.command,
                "pid": t.pid,
                "status": t.status.value,
                "start_time": t.start_time.isoformat(),
                "duration_seconds": round(t.duration_seconds, 2),
                "exit_code": t.exit_code,
            }
            for t in tasks
        ]
        return json.dumps({"status": "success", "count": len(result), "tasks": result})

    elif action == "status":
        task_id = args.get("task_id")
        if not task_id:
            return json.dumps({"status": "error", "message": "Missing 'task_id' parameter for action 'status'."})

        info = task_manager.get_task(task_id)
        if not info:
            return json.dumps({"status": "error", "message": f"Task '{task_id}' not found."})

        return json.dumps({
            "status": "success",
            "task": {
                "task_id": info.task_id,
                "command": info.command,
                "pid": info.pid,
                "status": info.status.value,
                "start_time": info.start_time.isoformat(),
                "end_time": info.end_time.isoformat() if info.end_time else None,
                "duration_seconds": round(info.duration_seconds, 2),
                "exit_code": info.exit_code,
                "log_file": info.log_file,
            },
        })

    elif action == "logs":
        task_id = args.get("task_id")
        if not task_id:
            return json.dumps({"status": "error", "message": "Missing 'task_id' parameter for action 'logs'."})

        tail = args.get("tail", 50)
        try:
            tail = int(tail)
        except ValueError:
            tail = 50

        ok, logs = task_manager.get_task_logs(task_id=task_id, tail=tail)
        if not ok:
            return json.dumps({"status": "error", "message": logs})

        return json.dumps({
            "status": "success",
            "task_id": task_id,
            "tail_lines": tail,
            "logs": logs,
        })

    else:
        return json.dumps({
            "status": "error",
            "message": f"Unknown shell action: '{action}'. Valid actions are 'run', 'list', 'status', 'logs'.",
        })


def shell_tool(task_manager: TaskManager) -> Tool:
    """The `shell` tool, running commands through `task_manager`."""
    return Tool(
        definition=SHELL_TOOL_DEFINITION,
        execute=lambda args: execute_shell_tool(task_manager, args),
    )
