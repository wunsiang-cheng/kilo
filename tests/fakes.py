"""Stand-ins for what lies outside Kilo (the LLM and the clock), and how tests wait for what runs in the background."""

import asyncio
import time
from datetime import datetime, timedelta
from typing import Any, AsyncIterator, Dict, List, Optional


async def until(condition, seconds: float = 10) -> None:
    """Waits for `condition()` by real time: a count of short sleeps lasts far less than it adds up to on Windows."""
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        await asyncio.sleep(0.02)


def text_turn(text: str, stop_reason: str = "end_turn") -> List[Dict[str, Any]]:
    """One assistant turn that only says `text`."""
    return [
        {"type": "text", "delta": text},
        {
            "type": "done", "stop_reason": stop_reason, "stop_details": None,
            "content": [{"type": "text", "text": text}], "context_tokens": 100,
        },
    ]


def tool_turn(call_id: str, name: str, args: Dict[str, Any], stop_reason: str = "tool_use") -> List[Dict[str, Any]]:
    """One assistant turn that calls one tool."""
    return [{
        "type": "done", "stop_reason": stop_reason, "stop_details": None,
        "content": [{"type": "tool_use", "id": call_id, "name": name, "input": args}], "context_tokens": 100,
    }]


class FakeModel:
    """A ChatModel playing back `turns`, one per request. An Exception in a turn is raised at that point."""

    def __init__(self, turns: Optional[List[List[Any]]] = None):
        self.turns: List[List[Any]] = list(turns or [])
        self.requests: List[List[Dict[str, Any]]] = []
        self.compact_result: Dict[str, Any] = {
            "stop_reason": "compaction",
            "content": [{"type": "compaction", "content": "summary"}],
            "summary_tokens": 50,
        }
        self.compactions = 0

    async def stream_turn(self, system: str, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> AsyncIterator[Dict[str, Any]]:
        self.requests.append(messages)
        if not self.turns:
            raise AssertionError("FakeModel was asked for more turns than it has.")
        for item in self.turns.pop(0):
            if isinstance(item, Exception):
                raise item
            if callable(item):  # a coroutine function: wait here, to hold the turn open
                await item()
                continue
            yield item

    async def compact(self, system: str, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]], instructions: str, max_tokens: int) -> Dict[str, Any]:
        self.compactions += 1
        if isinstance(self.compact_result, Exception):
            raise self.compact_result
        return self.compact_result


class FakeClock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)
