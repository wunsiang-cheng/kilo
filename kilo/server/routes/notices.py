"""The server's news for the web terminal, as it happens: how each scheduled run ended, and the turns the system starts."""

import asyncio
import json
from typing import Any, AsyncIterator, Callable, Dict

import anyio
from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse

from kilo.agent.history import run_event
from kilo.assembly import get_kilo
from kilo.models.schedule import Run
from kilo.scheduling.announcer import RunAnnouncer
from kilo.server.auth import COOKIE

router = APIRouter(tags=["notices"])

# How often an open stream checks that its login still holds and that the server is not stopping.
CHECK_SECONDS = 0.5
# Keep-alive comments, for proxies that close quiet connections.
PING_SECONDS = 15


async def run_notices(
    announcer: RunAnnouncer, keep_open: Callable[[], bool], check_seconds: float = CHECK_SECONDS,
) -> AsyncIterator[Dict[str, Any]]:
    """
    From now on, for as long as `keep_open()` holds: a `run_finished` event for each run that ends, and a
    `session_event` ({session_id, event}) for each event of a turn the system starts in a session.
    """
    with announcer.listen() as notices:
        while True:
            try:
                notice = await asyncio.wait_for(notices.get(), check_seconds)
            except TimeoutError:
                notice = None
            if not keep_open():
                return
            if isinstance(notice, Run):
                event = run_event(notice)
                yield {"event": event["event"], "data": json.dumps(event["data"])}
            elif notice is not None:
                yield {"event": "session_event", "data": json.dumps(notice, default=str)}


@router.get("/notices")
async def notices(request: Request) -> EventSourceResponse:
    """
    Server-sent events, for as long as the login lasts: `run_finished` with the run, as each run of a
    schedule made in a session ends, and `session_event` as a turn the system started in a session (a
    scheduled run) streams; the web terminal shows them if that session is open.
    """
    kilo = get_kilo()
    token = request.cookies.get(COOKIE)
    stopping = anyio.Event()  # set when the server begins to stop (Ctrl+C), so the stream ends cleanly
    return EventSourceResponse(
        run_notices(kilo.run_announcer, lambda: kilo.logins.valid(token) and not stopping.is_set()),
        ping=PING_SECONDS, shutdown_event=stopping, shutdown_grace_period=4 * CHECK_SECONDS,
    )
