"""Scheduler: owns schedules and fires due ones, each as a run started by the run launcher in the schedule's session."""

import asyncio
import logging
import uuid
from datetime import datetime, time, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from kilo.clock import utc_now
from kilo.config import settings
from kilo.models.schedule import Run, RunKind, Schedule, ScheduleSpec, ScheduleUpdate
from kilo.scheduling.cron import Cron
from kilo.scheduling.launcher import RUN_HEADER, RunLauncher
from kilo.storage.schedules import ScheduleStore

logger = logging.getLogger(__name__)

# How long the loop sleeps at most between checks, so clock changes and missed wake-ups heal quickly.
MAX_SLEEP_SECONDS = 60.0
# A one-off time this far in the past is still accepted (it fires right away).
ONE_OFF_SLACK = timedelta(minutes=1)


class ScheduleError(Exception):
    """A schedule request that cannot be carried out; the message says why."""


class ScheduleNotFound(ScheduleError):
    pass



def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ScheduleError(f"Unknown timezone '{name}'. Use an IANA name such as 'Asia/Taipei'.")


def _cron(expression: str) -> Cron:
    try:
        return Cron.parse(expression)
    except ValueError as e:
        raise ScheduleError(f"Invalid cron expression: {e}.")


def _local(moment: Optional[datetime], tz: str) -> Optional[str]:
    return moment.astimezone(ZoneInfo(tz)).strftime("%a %Y-%m-%d %H:%M") if moment else None


def next_run(schedule: Schedule, after: datetime) -> Optional[datetime]:
    """When an enabled schedule next fires after `after` (UTC), or None if it never will."""
    if schedule.run_at is not None:
        return schedule.run_at
    tz = ZoneInfo(schedule.timezone)
    start = after.astimezone(tz)
    if schedule.active_from:
        window_start = datetime.combine(schedule.active_from, time.min, tz)
        start = max(start, window_start - timedelta(minutes=1))
    moment = Cron.parse(schedule.cron).next_after(start)
    if moment is None:
        return None
    if schedule.active_until and moment.date() > schedule.active_until:
        return None
    return moment.astimezone(timezone.utc)


def slot_to_fire(schedule: Schedule, now: datetime) -> Optional[datetime]:
    """
    The time a due schedule fires for: its `next_run_at`, or after long downtime the first later cron slot still
    inside KILO_JOB_CATCHUP_MINUTES. None when it is too late for any of them.
    """
    catchup = timedelta(minutes=settings.kilo_job_catchup_minutes)
    if now - schedule.next_run_at <= catchup:
        return schedule.next_run_at
    if schedule.cron is None:
        return None
    slot = next_run(schedule, now - catchup - timedelta(seconds=1))  # a slot exactly at the limit still counts
    return slot if slot is not None and slot <= now else None


def _when(fields: Dict[str, Any], tz: str) -> str:
    """A schedule's timing in words, from its fields as stored or as proposed."""
    if fields.get("cron"):
        return f"cron '{fields['cron']}'"
    if fields.get("run_in_minutes") is not None:
        return f"once, {fields['run_in_minutes']} minute(s) after approval"
    run_at = fields.get("run_at")
    if run_at is not None and run_at.tzinfo is None:
        run_at = run_at.replace(tzinfo=ZoneInfo(tz))
    return f"once at {_local(run_at, tz)}"


def describe_spec(spec: ScheduleSpec) -> Dict[str, Any]:
    """A proposed definition, for the agent and for the user who approves it."""
    tz = spec.timezone or settings.kilo_timezone
    return {
        "name": spec.name,
        "when": _when(spec.model_dump(), tz),
        "timezone": tz,
        "active_from": spec.active_from.isoformat() if spec.active_from else None,
        "active_until": spec.active_until.isoformat() if spec.active_until else None,
        "enabled": spec.enabled,
        "instructions": spec.instructions,
    }


def describe(schedule: Schedule) -> Dict[str, Any]:
    """
    A schedule for tool output and the web terminal, with times in its own timezone. One awaiting approval
    carries the proposal: `change` is 'create' for a new schedule, 'update' for a change to an approved one.
    """
    described = {
        "id": schedule.id,
        "name": schedule.name,
        "when": _when(schedule.model_dump(), schedule.timezone),
        "timezone": schedule.timezone,
        "active_from": schedule.active_from.isoformat() if schedule.active_from else None,
        "active_until": schedule.active_until.isoformat() if schedule.active_until else None,
        "enabled": schedule.enabled,
        "approved": schedule.approved,
        "awaiting_approval": schedule.pending is not None,
        "next_run": _local(schedule.next_run_at, schedule.timezone) if schedule.enabled else None,
        "last_run": _local(schedule.last_run_at, schedule.timezone),
        "instructions": schedule.instructions,
        "session_id": schedule.origin_session_id,
    }
    if schedule.pending is not None:
        described["change"] = "update" if schedule.approved else "create"
        described["proposal"] = describe_spec(schedule.pending)
    return described


def wake_up_message(schedule: Schedule, run: Run) -> str:
    """
    The message that starts a scheduled run in its schedule's session: the run, the time and the job.
    Carries everything that changes between runs, so the system prompt stays fixed.
    """
    return (
        f"{RUN_HEADER}{run.id} of \"{schedule.name}\" ({schedule.id}) · "
        f"{_local(run.scheduled_for, schedule.timezone)} {schedule.timezone}]\n"
        f"Job:\n{schedule.instructions}"
    )


_EDITABLE = ("name", "instructions", "cron", "run_at", "run_in_minutes", "timezone", "active_from", "active_until", "enabled")


class Scheduler:
    """
    The only way schedules change, and the only place they fire. The agent proposes schedules and changes to
    them; each takes effect only once the user approves it, so a schedule fires only as the user approved it.
    Pausing and deleting take effect at once. The loop fires due schedules, each as a run started by the run
    launcher in the schedule's session, and records every firing (or skip) as a Run.
    """

    def __init__(self, store: ScheduleStore, launcher: RunLauncher, clock: Callable[[], datetime] = utc_now):
        self.store, self.launcher = store, launcher
        self._clock = clock
        self._mutex = asyncio.Lock()  # schedule reads-modify-writes (ticks and edits) one at a time
        self._loop_task: Optional[asyncio.Task] = None
        self._wake = asyncio.Event()

    # ------------------------------------------------------------------
    # Schedule management
    # ------------------------------------------------------------------

    def _build(self, fields: Dict[str, Any], schedule_id: str, now: datetime) -> Schedule:
        name = (fields.get("name") or "").strip()
        instructions = (fields.get("instructions") or "").strip()
        if not name:
            raise ScheduleError("A schedule needs a name.")
        if not instructions:
            raise ScheduleError("A schedule needs instructions: the job the agent does on each run.")
        tz_name = fields.get("timezone") or settings.kilo_timezone
        tz = _zone(tz_name)

        cron, run_at = fields.get("cron"), fields.get("run_at")
        if fields.get("run_in_minutes") is not None:
            if fields["run_in_minutes"] < 1:
                raise ScheduleError("run_in_minutes must be at least 1.")
            run_at = now + timedelta(minutes=fields["run_in_minutes"])
        if (cron is None) == (run_at is None):
            raise ScheduleError("Give exactly one of 'cron' (recurring) or 'run_at' / 'run_in_minutes' (one-off).")

        active_from, active_until = fields.get("active_from"), fields.get("active_until")
        if cron is not None:
            _cron(cron)
            if active_from and active_until and active_until < active_from:
                raise ScheduleError("active_until is before active_from.")
        else:
            if active_from or active_until:
                raise ScheduleError("active_from / active_until apply to cron schedules only.")
            if run_at.tzinfo is None:
                run_at = run_at.replace(tzinfo=tz)
            run_at = run_at.astimezone(timezone.utc)

        return Schedule(
            id=schedule_id,
            name=name,
            instructions=instructions,
            cron=cron.strip() if cron else None,
            run_at=run_at,
            timezone=tz_name,
            active_from=active_from,
            active_until=active_until,
            enabled=bool(fields.get("enabled", True)),
            origin_session_id=fields.get("origin_session_id"),
        )

    def _plan(self, schedule: Schedule, now: datetime) -> None:
        """Validate an enabled schedule against the clock, and set next_run_at."""
        if not schedule.enabled:
            schedule.next_run_at = None
            return
        if schedule.run_at is not None and schedule.run_at < now - ONE_OFF_SLACK:
            raise ScheduleError(
                f"run_at ({_local(schedule.run_at, schedule.timezone)} {schedule.timezone}) is in the past."
            )
        schedule.next_run_at = next_run(schedule, now)
        if schedule.next_run_at is None:
            raise ScheduleError("This schedule would never run: no matching time inside its active dates.")

    def _check(self, spec: ScheduleSpec, schedule_id: str, now: datetime) -> Schedule:
        """The schedule `spec` defines, refused now if it could not be approved now."""
        schedule = self._build(spec.model_dump(), schedule_id, now)
        self._plan(schedule, now)
        return schedule

    async def _get(self, schedule_id: str) -> Schedule:
        schedule = await self.store.get_schedule(schedule_id)
        if schedule is None:
            raise ScheduleNotFound(f"Schedule '{schedule_id}' not found.")
        return schedule

    async def create(self, spec: ScheduleSpec) -> Schedule:
        """
        Propose a new schedule. It does not run until the user approves it. A session has one schedule at a time
        (approved, paused or proposed): one made in a session that has one is refused.
        """
        async with self._mutex:
            now = self._clock()
            if spec.origin_session_id is not None:
                existing = next((s for s in await self.store.list_schedules() if s.origin_session_id == spec.origin_session_id), None)
                if existing is not None:
                    raise ScheduleError(
                        f"This conversation already has schedule '{existing.id}' (\"{existing.name}\"), and a conversation "
                        "has one schedule at a time. Update or delete it instead."
                    )
            schedule = self._check(spec, f"sched_{uuid.uuid4().hex[:8]}", now)
            schedule.next_run_at = None
            schedule.pending = spec
            schedule.created_at = schedule.updated_at = now
            await self.store.save_schedule(schedule)
        return schedule

    async def update(self, schedule_id: str, changes: ScheduleUpdate) -> Schedule:
        """
        Propose a change, on top of any change already proposed. An approved schedule runs as it was until the
        user approves it; a schedule not yet approved shows its new proposal at once.
        """
        async with self._mutex:
            now = self._clock()
            current = await self._get(schedule_id)
            fields = current.pending.model_dump() if current.pending else {
                name: getattr(current, name, None) for name in _EDITABLE
            }
            given = changes.model_dump(include=changes.model_fields_set)
            if "cron" in given or "run_at" in given or "run_in_minutes" in given:
                # Switching between recurring and one-off: the new timing replaces the old one.
                fields.update(cron=None, run_at=None, run_in_minutes=None)
            fields.update(given)
            proposal = ScheduleSpec(**{**fields, "origin_session_id": current.origin_session_id})
            proposed = self._check(proposal, current.id, now)
            if not current.approved:
                proposed.next_run_at = None
                proposed.created_at = current.created_at
                current = proposed
            current.pending, current.updated_at = proposal, now
            await self.store.save_schedule(current)
        return current

    async def pause(self, schedule_id: str) -> Schedule:
        """Stop a schedule firing, at once (a change awaiting approval is kept, paused too)."""
        async with self._mutex:
            schedule = await self._get(schedule_id)
            schedule.enabled, schedule.next_run_at, schedule.updated_at = False, None, self._clock()
            if schedule.pending is not None:
                schedule.pending.enabled = False
            await self.store.save_schedule(schedule)
        return schedule

    async def resume(self, schedule_id: str) -> Schedule:
        """Propose firing a paused schedule again."""
        schedule = await self.get(schedule_id)
        if schedule is not None and schedule.approved and schedule.enabled and schedule.pending is None:
            raise ScheduleError(f"Schedule '{schedule_id}' is not paused.")
        return await self.update(schedule_id, ScheduleUpdate(enabled=True))

    async def delete(self, schedule_id: str) -> None:
        """Delete a schedule, at once. A run in progress finishes; its record is kept."""
        async with self._mutex:
            await self._get(schedule_id)
            await self.store.delete_schedule(schedule_id)

    async def approve(self, schedule_id: str) -> Schedule:
        """
        The user approves what awaits approval: the schedule then runs as proposed. A proposal that can no
        longer run as given (its one-off time has passed) is discarded instead, as if refused.
        """
        async with self._mutex:
            now = self._clock()
            current = await self._get(schedule_id)
            if current.pending is None:
                raise ScheduleError(f"Schedule '{schedule_id}' has nothing awaiting approval.")
            try:
                schedule = self._check(current.pending, current.id, now)
            except ScheduleError as e:
                await self._discard(current)
                raise ScheduleError(f"{e} The proposal was discarded.")
            schedule.approved = True
            schedule.created_at, schedule.last_run_at, schedule.updated_at = current.created_at, current.last_run_at, now
            schedule.origin_session_id = current.origin_session_id
            await self.store.save_schedule(schedule)
        self._wake.set()
        return schedule

    async def reject(self, schedule_id: str) -> Optional[Schedule]:
        """
        The user refuses what awaits approval: a new schedule is deleted (None is returned), a change to an
        approved one dropped, and the schedule runs on as it was.
        """
        async with self._mutex:
            current = await self._get(schedule_id)
            if current.pending is None:
                raise ScheduleError(f"Schedule '{schedule_id}' has nothing awaiting approval.")
            return await self._discard(current)

    async def _discard(self, schedule: Schedule) -> Optional[Schedule]:
        if not schedule.approved:
            await self.store.delete_schedule(schedule.id)
            return None
        schedule.pending, schedule.updated_at = None, self._clock()
        await self.store.save_schedule(schedule)
        return schedule

    async def awaiting_approval(self, session_id: Optional[str] = None) -> List[Schedule]:
        """Schedules with a proposal awaiting approval (of those made in a session, when given), oldest first."""
        return [
            s for s in await self.store.list_schedules()
            if s.pending is not None and (session_id is None or s.origin_session_id == session_id)
        ]

    async def get(self, schedule_id: str) -> Optional[Schedule]:
        return await self.store.get_schedule(schedule_id)

    async def list(self) -> List[Schedule]:
        return await self.store.list_schedules()

    async def list_runs(self, schedule_id: Optional[str] = None, limit: int = 20) -> List[Run]:
        return await self.store.list_runs(schedule_id=schedule_id, limit=limit)

    # ------------------------------------------------------------------
    # Firing
    # ------------------------------------------------------------------

    def _skip_reason(self, schedule: Schedule) -> Optional[str]:
        if self.launcher.running(schedule_id=schedule.id):
            return "Skipped: the previous run is still in progress."
        return None

    async def _fire(self, schedule: Schedule, scheduled_for: datetime) -> Run:
        """
        Start the schedule's run in the background, in its session. A schedule without one (made outside a
        session, or its session deleted) is given one here, kept for its later runs; the caller saves it.
        Returns the run as it started.
        """
        run = await self.launcher.start(
            RunKind.SCHEDULED, schedule_id=schedule.id, schedule_name=schedule.name, scheduled_for=scheduled_for,
            title=schedule.name, wake_up=lambda run: wake_up_message(schedule, run),
            origin_session_id=schedule.origin_session_id,
        )
        schedule.origin_session_id = run.session_id
        return run

    async def tick(self) -> List[Run]:
        """Fire every due schedule once, or record why it was skipped. Returns the runs recorded."""
        runs: List[Run] = []
        async with self._mutex:
            now = self._clock()
            for schedule in await self.store.list_schedules():
                if not (schedule.approved and schedule.enabled) or schedule.next_run_at is None or schedule.next_run_at > now:
                    continue
                due = schedule.next_run_at
                slot = slot_to_fire(schedule, now)
                if slot is None:
                    reason: Optional[str] = (
                        f"Missed: the server was not running at {_local(due, schedule.timezone)}."
                    )
                else:
                    due = slot
                    reason = self._skip_reason(schedule)

                if reason:
                    run = await self.launcher.skip(
                        RunKind.SCHEDULED, schedule_id=schedule.id, schedule_name=schedule.name, scheduled_for=due,
                        reason=reason, origin_session_id=schedule.origin_session_id,
                    )
                else:
                    run = await self._fire(schedule, due)
                    schedule.last_run_at = now
                runs.append(run)

                if schedule.run_at is not None:  # one-off: done after its single firing (its runs keep its name)
                    await self.store.delete_schedule(schedule.id)
                    continue
                schedule.next_run_at = next_run(schedule, now)
                await self.store.save_schedule(schedule)
        return runs

    async def _seconds_until_next(self) -> float:
        upcoming = [s.next_run_at for s in await self.store.list_schedules() if s.approved and s.enabled and s.next_run_at]
        if not upcoming:
            return MAX_SLEEP_SECONDS
        delay = (min(upcoming) - self._clock()).total_seconds()
        return min(max(delay, 0.5), MAX_SLEEP_SECONDS)

    async def _loop(self) -> None:
        while True:
            self._wake.clear()
            try:
                await self.tick()
                delay = await self._seconds_until_next()
            except Exception:
                logger.exception("Scheduler tick failed")
                delay = MAX_SLEEP_SECONDS
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except TimeoutError:
                pass

    async def start(self) -> None:
        """Start firing schedules."""
        await self.store.init()
        if self._loop_task is None:
            self._loop_task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop the loop and interrupt scheduled runs in progress (they are recorded as failed)."""
        if self._loop_task is not None:
            self._loop_task.cancel()
            await asyncio.gather(self._loop_task, return_exceptions=True)
            self._loop_task = None
        await self.launcher.stop(RunKind.SCHEDULED)

    async def wait_idle(self) -> None:
        """Wait for every scheduled run in progress to finish (used by tests)."""
        await self.launcher.wait_idle(RunKind.SCHEDULED)

