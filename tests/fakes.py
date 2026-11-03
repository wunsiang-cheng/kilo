"""Stand-ins for what lies outside Kilo (the LLM and the clock), and how tests wait for what runs in the background."""

import asyncio
import json
import time
from datetime import datetime, timedelta
from typing import Any, AsyncIterator, Dict, List, Optional


async def until(condition, seconds: float = 10) -> None:
    """Waits for `condition()` by real time: a count of short sleeps lasts far less than it adds up to on Windows."""
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        await asyncio.sleep(0.02)


def text_turn(text: str, finish_reason: str = "stop") -> List[Dict[str, Any]]:
    """One assistant turn that only says `text` (no text event when it is empty, as the client sends none)."""
    events: List[Dict[str, Any]] = [{"type": "text", "delta": text}] if text else []
    done = {"type": "done", "finish_reason": finish_reason, "message": {"role": "assistant", "content": text or None}, "context_tokens": 100}
    return [*events, done]


def tool_turn(call_id: str, name: str, args: Any, finish_reason: str = "tool_calls") -> List[Dict[str, Any]]:
    """One assistant turn that calls one tool; `args` given as a str is sent as the raw arguments."""
    arguments = args if isinstance(args, str) else json.dumps(args)
    call = {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}
    return [{
        "type": "done", "finish_reason": finish_reason, "context_tokens": 100,
        "message": {"role": "assistant", "content": None, "tool_calls": [call]},
    }]


class FakeModel:
    """A ChatModel playing back `turns`, one per request. An Exception in a turn is raised at that point."""

    def __init__(self, turns: Optional[List[List[Any]]] = None):
        self.turns: List[List[Any]] = list(turns or [])
        self.requests: List[List[Dict[str, Any]]] = []  # the messages of each call
        self.max_tokens: List[Optional[int]] = []  # the max_tokens of each call

    async def stream_turn(
        self, system: str, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]], max_tokens: Optional[int] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        self.requests.append(messages)
        self.max_tokens.append(max_tokens)
        if not self.turns:
            raise AssertionError("FakeModel was asked for more turns than it has.")
        for item in self.turns.pop(0):
            if isinstance(item, Exception):
                raise item
            if callable(item):  # a coroutine function: wait here, to hold the turn open
                await item()
                continue
            yield item


class FakeClock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)
