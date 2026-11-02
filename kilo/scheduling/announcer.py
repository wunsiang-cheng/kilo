"""
Run announcer: tells the web terminals open now how each scheduled run ended, so its schedule's origin
session shows it at once, and streams the turns the system starts in a session (scheduled runs) as they go.
A terminal opened later sees both in the session's history instead.
"""

import asyncio
import logging
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Set, Union

from kilo.models.schedule import Run

logger = logging.getLogger(__name__)

# Items a listener may fall behind by; a turn streams many events, but only a stalled connection reaches it.
BACKLOG = 2000

# What listeners get: an ended run, or {"session_id": ..., "event": ...} of a turn the system started.
Notice = Union[Run, Dict[str, Any]]


class RunAnnouncer:
    def __init__(self):
        self._listeners: Set[asyncio.Queue] = set()

    def announce(self, run: Run) -> None:
        """Hand an ended run to every listener, when its session shows a notice of it (Run.shown_in_origin)."""
        if run.shown_in_origin:
            self._put(run)

    def session_event(self, session_id: str, event: Dict[str, Any]) -> None:
        """Hand an event of a turn the system started in a session to every listener."""
        self._put({"session_id": session_id, "event": event})

    def _put(self, notice: Notice) -> None:
        for queue in self._listeners:
            try:
                queue.put_nowait(notice)
            except asyncio.QueueFull:
                logger.warning("A web terminal fell %d notices behind; it shows the rest in sessions' history only.", BACKLOG)

    @contextmanager
    def listen(self) -> Iterator[asyncio.Queue]:
        """A queue of the notices given while the block runs."""
        queue: asyncio.Queue = asyncio.Queue(BACKLOG)
        self._listeners.add(queue)
        try:
            yield queue
        finally:
            self._listeners.discard(queue)
