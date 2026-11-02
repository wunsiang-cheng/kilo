"""
Run launcher: starts the work the scheduler fires. Each run is a turn in its schedule's session, like one the
user starts but begun by the system: it waits for a turn in progress there, has the same limits, streams to the
web terminal as it goes, and can be stopped. The launcher keeps the Run record and how it ended.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from kilo.clock import utc_now
from kilo.agent.runner import is_stop

from kilo.models.schedule import Run, RunKind, RunStatus, new_run_id
from kilo.storage.db import Database
from kilo.storage.schedules import ScheduleStore

logger = logging.getLogger(__name__)

INTERRUPTED = "Interrupted: the server stopped during this run."
STOPPED = "Stopped."

# The first line of every message that starts a run; it tells a run from what the user typed.
RUN_HEADER = "[System notice, not from the user: scheduled run "

# Builds the wake-up message from the run (its id and start time).
WakeUp = Callable[[Run], str]
# Told of every run as it ends or is skipped, once it is recorded (RunAnnouncer.announce).
Announce = Callable[[Run], None]
# Given each event of a run's turn, with its session (RunAnnouncer.session_event).
Publish = Callable[[str, Dict[str, Any]], None]


def is_run_message(content: Optional[str]) -> bool:
    return bool(content) and content.startswith(RUN_HEADER)


def run_started_event(content: str) -> Dict[str, Any]:
    """The message that starts a run, as the event that shows it (live and in its session's history)."""
    return {"event": "run_started", "data": {"content": content}}



@dataclass(frozen=True)
class RunAgent:
    """The agent of one kind of run."""

    runner: Callable[[], Any]  # the AgentRunner, built on first use


class RunLauncher:
    def __init__(
        self,
        store: ScheduleStore,
        db: Database,
        agents: Mapping[RunKind, RunAgent],
        clock: Callable[[], datetime] = utc_now,
        announce: Announce = lambda run: None,
        publish: Publish = lambda session_id, event: None,
    ):
        self.store, self.db, self.agents = store, db, dict(agents)
        self._clock = clock
        self._announce = announce
        self._publish = publish
        self._jobs: Dict[str, Tuple[Run, asyncio.Task]] = {}  # run id -> the run as started, and its job

    async def start(
        self,
        kind: RunKind,
        *,
        schedule_id: str,
        schedule_name: str,
        title: str,
        wake_up: WakeUp,
        scheduled_for: Optional[datetime] = None,
        origin_session_id: Optional[str] = None,
    ) -> Run:
        """
        Start a run in the background, as a turn in `origin_session_id`, and return it as started. Without that
        session (none given, or deleted) one is made for the run, titled `title`; the run's `session_id` says
        which, and its schedule keeps it for later runs.
        """
        agent = self.agents[kind]
        now = self._clock()
        run = Run(
            id=new_run_id(), kind=kind, schedule_id=schedule_id, schedule_name=schedule_name,
            status=RunStatus.RUNNING, scheduled_for=scheduled_for or now, started_at=now, origin_session_id=origin_session_id,
        )
        message = wake_up(run)
        runner = agent.runner()
        session = await self.db.get_session(origin_session_id, include_messages=False) if origin_session_id else None
        if session is None:
            session = await self.db.create_session(title=title[:200], metadata={"schedule_id": schedule_id})
        run.session_id = run.origin_session_id = session.id
        await self.store.save_run(run)
        task = asyncio.create_task(self._drive(runner, run.model_copy(), message))
        self._jobs[run.id] = (run, task)
        task.add_done_callback(lambda _: self._jobs.pop(run.id, None))
        return run

    async def skip(
        self, kind: RunKind, *, schedule_id: str, schedule_name: str, scheduled_for: datetime, reason: str,
        origin_session_id: Optional[str] = None,
    ) -> Run:
        """Record a run that did not start, with the reason."""
        now = self._clock()
        run = Run(
            id=new_run_id(), kind=kind, schedule_id=schedule_id, schedule_name=schedule_name, status=RunStatus.SKIPPED,
            scheduled_for=scheduled_for, started_at=now, finished_at=now, summary=reason, origin_session_id=origin_session_id,
        )
        await self.store.save_run(run)
        self._announce(run)
        return run

    async def _drive(self, runner: Any, run: Run, message: str) -> None:
        """Run the agent on the run's session, starting with `message`, and record how the run ended."""
        status, summary = RunStatus.FAILED, "The run ended without a final reply."
        try:
            async for event in runner.run_chat_stream(session_id=run.session_id, user_message=message):
                if event["event"] == "done":
                    status, summary = RunStatus.SUCCEEDED, event["data"].get("content") or ""
                elif event["event"] == "error":
                    status, summary = RunStatus.FAILED, event["data"].get("error")
                elif event["event"] == "user_message":
                    event = run_started_event(event["data"].get("content") or "")
                self._publish(run.session_id, event)
        except asyncio.CancelledError as e:
            if not is_stop(e):
                status, summary = RunStatus.FAILED, INTERRUPTED
                raise
            asyncio.current_task().uncancel()  # stopped by a person: the run ends here, recorded
            status, summary = RunStatus.FAILED, STOPPED
        except Exception as e:
            logger.exception("Run %s failed", run.id)
            status, summary = RunStatus.FAILED, f"Unexpected error: {e}"
        finally:
            run.status, run.summary, run.finished_at = status, summary, self._clock()
            await self.store.save_run(run)
            self._announce(run)

    # ------------------------------------------------------------------

    def running(self, schedule_id: Optional[str] = None, kind: Optional[RunKind] = None) -> bool:
        """Whether a run (of this schedule, or of this kind) is in progress."""
        return any(
            (schedule_id is None or run.schedule_id == schedule_id) and (kind is None or run.kind == kind)
            for run, _ in self._jobs.values()
        )

    async def recover(self) -> int:
        """Mark runs left 'running' by a previous server process as failed. Call once, before any run starts."""
        return await self.store.fail_unfinished_runs(INTERRUPTED, self._clock())

    def _tasks(self, kind: Optional[RunKind]) -> List[asyncio.Task]:
        return [task for run, task in self._jobs.values() if (kind is None or run.kind == kind) and not task.done()]

    async def stop(self, kind: Optional[RunKind] = None) -> None:
        """Interrupt runs in progress (of `kind`, or all); they are recorded as failed."""
        tasks = self._tasks(kind)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def wait_idle(self, kind: Optional[RunKind] = None) -> None:
        """Wait for runs in progress (of `kind`, or all) to finish (tests)."""
        while tasks := self._tasks(kind):
            await asyncio.gather(*tasks, return_exceptions=True)

