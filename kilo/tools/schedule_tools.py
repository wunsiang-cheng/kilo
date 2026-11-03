"""Schedule tool: the agent proposes scheduled runs, which the user approves, and manages them."""

import json
from datetime import datetime
from typing import Any, Dict
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from kilo.config import settings
from kilo.context import current_session_id
from kilo.models.schedule import ScheduleSpec, ScheduleUpdate
from kilo.scheduling.scheduler import ScheduleError, Scheduler, describe
from kilo.tools.base import Tool

_TIMING_FIELDS = ("name", "instructions", "cron", "run_at", "run_in_minutes", "timezone", "active_from", "active_until")

SCHEDULE_TOOL_DEFINITION = {
    "name": "schedule",
    "description": (
        "Schedule future work. A conversation has one schedule at a time: to change what it does or when, update it "
        "(or delete it first). A one-off schedule is deleted once it has run. Each run continues this conversation: a message from the system with the "
        "schedule's instructions, which you then carry out with everything said here so far; the user sees it here too. "
        "Actions: 'list' (all schedules and the current time), 'create', 'update', 'pause', 'resume', 'delete', "
        "'runs' (recent runs with their outcome). 'create', 'update' and 'resume' are proposals: the user is asked to "
        "approve each, and only then does it take effect. Until then a new schedule does not run and a "
        "changed one runs as it was; a refused new schedule is deleted, a refused change dropped. 'list' shows what "
        "awaits approval. 'pause' and 'delete' take effect at once. The list holds every conversation's schedules: "
        "change another conversation's only when the user asks."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "create", "update", "pause", "resume", "delete", "runs"],
                "description": "Operation to perform.",
            },
            "id": {
                "type": "string",
                "description": "Schedule id. Required for 'update', 'pause', 'resume', 'delete'; optional filter for 'runs'.",
            },
            "name": {"type": "string", "description": "Short name of the job. Required for 'create'."},
            "instructions": {
                "type": "string",
                "description": "What to do on each run; it can refer to this conversation. Required for 'create'.",
            },
            "cron": {
                "type": "string",
                "description": (
                    "Recurring timing, instead of `run_at` or `run_in_minutes`: a 5-field cron expression in the schedule's timezone, "
                    "e.g. '*/30 9-18 * * mon-fri' (every 30 minutes, 9:00-18:59, weekdays) or '0 9 * * *' (daily 9:00)."
                ),
            },
            "run_at": {
                "type": "string",
                "description": "One-off time, ISO 8601 (e.g. '2026-10-28T09:00'); without an offset it is in the schedule's timezone.",
            },
            "run_in_minutes": {"type": "integer", "description": "One-off, this many minutes from now (at least 1)."},
            "timezone": {"type": "string", "description": "IANA timezone (default: the server's, see 'list')."},
            "active_from": {"type": "string", "description": "Cron only: first date runs may happen (YYYY-MM-DD)."},
            "active_until": {"type": "string", "description": "Cron only: last date runs may happen (YYYY-MM-DD)."},
            "limit": {"type": "integer", "description": "Number of runs for 'runs' (default: 10).", "default": 10},
        },
        "required": ["action"],
    },
}


def _now() -> str:
    return datetime.now(ZoneInfo(settings.kilo_timezone)).strftime("%a %Y-%m-%d %H:%M")


async def execute_schedule_tool(scheduler: Scheduler, args: Dict[str, Any]) -> str:
    """Execute a schedule action for the agent."""
    action = args.get("action", "list")
    schedule_id = (args.get("id") or "").strip()

    try:
        if action == "list":
            schedules = await scheduler.list()
            return json.dumps({
                "status": "success",
                "action": action,
                "now": f"{_now()} {settings.kilo_timezone}",
                "schedules": [describe(s) for s in schedules],
            })

        if action == "runs":
            runs = await scheduler.list_runs(schedule_id=schedule_id or None, limit=int(args.get("limit") or 10))
            return json.dumps({
                "status": "success",
                "action": action,
                "runs": [r.model_dump(mode="json") for r in runs],
            })

        if action == "create":
            spec = ScheduleSpec(**{k: args[k] for k in _TIMING_FIELDS if args.get(k) is not None})
            spec.origin_session_id = current_session_id.get()  # its runs continue this conversation
            schedule = await scheduler.create(spec)
        elif not schedule_id:
            return json.dumps({"status": "error", "message": f"Parameter 'id' is required for action '{action}'."})
        elif action == "update":
            changes = ScheduleUpdate(**{k: args[k] for k in _TIMING_FIELDS if args.get(k) is not None})
            schedule = await scheduler.update(schedule_id, changes)
        elif action == "pause":
            schedule = await scheduler.pause(schedule_id)
        elif action == "resume":
            schedule = await scheduler.resume(schedule_id)
        elif action == "delete":
            await scheduler.delete(schedule_id)
            return json.dumps({"status": "success", "action": action, "message": f"Schedule '{schedule_id}' deleted."})
        else:
            return json.dumps({"status": "error", "message": f"Unknown schedule action '{action}'."})
    except ValidationError as e:
        return json.dumps({"status": "error", "message": f"Invalid schedule parameters: {e}"})
    except ScheduleError as e:
        return json.dumps({"status": "error", "message": str(e)})

    status = "awaiting_approval" if schedule.pending is not None and action != "pause" else "success"
    result = {"status": status, "action": action, "now": f"{_now()} {settings.kilo_timezone}", "schedule": describe(schedule)}
    if status == "awaiting_approval":
        result["message"] = "Proposed: the user is asked to approve it. Nothing changes until they do."
    return json.dumps(result)


def schedule_tool(scheduler: Scheduler) -> Tool:
    """The `schedule` tool, acting on `scheduler` for the agent."""
    return Tool(
        definition=SCHEDULE_TOOL_DEFINITION,
        execute=lambda args: execute_schedule_tool(scheduler, args),
    )
