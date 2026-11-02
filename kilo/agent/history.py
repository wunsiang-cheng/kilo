"""
A stored conversation as the events its live stream sent (see AgentRunner.run_chat_stream), so one renderer
draws both: a reply as it streams, and a session's earlier conversation when it is opened again.
Not replayed: errors and `done` (never stored), and the message the loop adds after a mid-reply compaction.
Added: how each run of a schedule made in the session ended, where it ended (the agent never sees these).
"""

import json
from collections import deque
from typing import Any, Callable, Dict, List, Sequence

from kilo.agent.runner import CONTINUE_AFTER_COMPACTION
from kilo.models.schedule import Run
from kilo.scheduling.launcher import is_run_message, run_started_event
from kilo.models.session import Message, MessageRole


def run_event(run: Run) -> Dict[str, Any]:
    """How a scheduled run ended, as the event that shows it in its schedule's origin session (also sent live)."""
    return {"event": "run_finished", "data": run.model_dump(mode="json")}


def _result(content: Any) -> Dict[str, Any]:
    """A stored tool result's content as the stream's `result`: the tool's JSON object, else the raw text."""
    try:
        parsed = json.loads(content) if isinstance(content, str) else content
    except json.JSONDecodeError:
        parsed = None
    return parsed if isinstance(parsed, dict) else {"raw": content if isinstance(content, str) else json.dumps(content)}


def history_events(
    messages: List[Message], canonical_name: Callable[[str], str] = lambda name: name, runs: Sequence[Run] = (),
) -> List[Dict[str, Any]]:
    """
    The events of a session's stored messages, in order. `canonical_name` maps a tool alias the model
    used to the tool's own name, as the live stream reports it (Toolbox.canonical_name). `runs` (ended,
    of schedules made in the session) that it shows a notice of go between the messages by the time they ended.
    """
    events: List[Dict[str, Any]] = []
    tool_names: Dict[str, str] = {}  # tool_use id -> the tool's name, for the result that follows
    ended = deque(sorted((run for run in runs if run.shown_in_origin), key=lambda run: run.finished_at or run.started_at))

    for msg in messages:
        while ended and (ended[0].finished_at or ended[0].started_at) <= msg.created_at:
            events.append(run_event(ended.popleft()))
        if msg.role == MessageRole.USER:
            if is_run_message(msg.content):
                events.append(run_started_event(msg.content or ""))
            elif msg.content != CONTINUE_AFTER_COMPACTION:
                events.append({"event": "user_message", "data": {
                    "id": msg.id, "session_id": msg.session_id, "content": msg.content or "",
                }})

        elif msg.role == MessageRole.ASSISTANT:
            blocks = msg.blocks if msg.blocks is not None else [{"type": "text", "text": msg.content or ""}]
            for block in blocks:
                kind = block.get("type")
                if kind == "text" and block.get("text"):
                    events.append({"event": "message_delta", "data": {"delta": block["text"]}})
                elif kind == "thinking" and block.get("thinking"):
                    events.append({"event": "progress", "data": {"delta": block["thinking"]}})
                elif kind == "tool_use":
                    name = canonical_name(block.get("name", "unknown"))
                    tool_names[block["id"]] = name
                    arguments = block.get("input")
                    events.append({"event": "tool_call_start", "data": {
                        "tool_call_id": block["id"], "name": name,
                        "arguments": arguments if isinstance(arguments, dict) else {},
                    }})
                elif kind == "compaction":
                    # The counts of the moment are not stored; the renderer says only that it happened.
                    events.append({"event": "compaction_done", "data": {}})

        elif msg.role == MessageRole.TOOL:
            for block in msg.blocks or []:
                if block.get("type") == "tool_result":
                    call_id = block.get("tool_use_id", "")
                    events.append({"event": "tool_call_result", "data": {
                        "tool_call_id": call_id, "name": tool_names.get(call_id, ""), "result": _result(block.get("content")),
                    }})

    events.extend(run_event(run) for run in ended)
    return events
