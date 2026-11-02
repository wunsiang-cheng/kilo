"""Schedule and run data models for scheduled agent jobs."""

import uuid
from datetime import date, datetime
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field

from kilo.clock import utc_now



class RunStatus(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"  # historical: runs had a time limit
    SKIPPED = "skipped"


class ScheduleSpec(BaseModel):
    """
    What the agent proposes for a schedule, as given (run_in_minutes stays relative until the user approves it).
    Exactly one of cron, run_at, run_in_minutes.
    """

    name: str
    instructions: str
    cron: Optional[str] = None  # recurring, e.g. "*/30 9-18 * * mon-fri"
    run_at: Optional[datetime] = None  # one-off; a time without offset is in the schedule's timezone
    run_in_minutes: Optional[int] = None  # one-off, relative to now
    timezone: Optional[str] = None  # IANA name; default KILO_TIMEZONE
    active_from: Optional[date] = None  # first day runs may happen (inclusive)
    active_until: Optional[date] = None  # last day runs may happen (inclusive)
    enabled: bool = True
    origin_session_id: Optional[str] = None  # the session it is made in, where its runs happen


class ScheduleUpdate(BaseModel):
    """Changes to a schedule; only the fields given are changed (null clears an optional field)."""

    name: Optional[str] = None
    instructions: Optional[str] = None
    cron: Optional[str] = None
    run_at: Optional[datetime] = None
    run_in_minutes: Optional[int] = None
    timezone: Optional[str] = None
    active_from: Optional[date] = None
    active_until: Optional[date] = None
    enabled: Optional[bool] = None


class Schedule(BaseModel):
    id: str
    name: str
    instructions: str
    cron: Optional[str] = None
    run_at: Optional[datetime] = None  # UTC
    timezone: str
    active_from: Optional[date] = None
    active_until: Optional[date] = None
    enabled: bool = True
    # Approved schedules fire. `pending` is a definition the agent proposed that awaits the user's approval:
    # a new schedule's (then not yet approved), or a change to an approved one, which runs as it was meanwhile.
    approved: bool = False
    pending: Optional[ScheduleSpec] = None
    origin_session_id: Optional[str] = None  # the session its runs happen in (made at its first run when none)
    next_run_at: Optional[datetime] = None  # UTC; None when it will not fire again
    last_run_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class RunKind(str, Enum):
    """What started a run, which decides its agent (tools and system prompt)."""

    SCHEDULED = "scheduled"  # a schedule fired
    LINE = "line"  # historical automatic LINE runs; new LINE work uses conversations or schedules


def new_run_id() -> str:
    return f"run_{uuid.uuid4().hex[:8]}"


class Run(BaseModel):
    """One firing of a schedule: a turn in its schedule's session running the job, or a skip with its reason."""

    id: str
    kind: RunKind = RunKind.SCHEDULED
    schedule_id: str
    schedule_name: str
    session_id: Optional[str] = None
    status: RunStatus
    scheduled_for: datetime
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: Optional[datetime] = None
    summary: Optional[str] = None  # the agent's final reply, or why the run failed or was skipped
    origin_session_id: Optional[str] = None  # its schedule's session (runs before 2026-11 had a session of their own)

    @property
    def shown_in_origin(self) -> bool:
        """
        Whether its schedule's session shows a notice of how it ended: a run that was a turn there shows its
        outcome itself, unless it never started (skipped) or did not finish (failed).
        """
        return self.origin_session_id is not None and (
            self.status != RunStatus.SUCCEEDED or self.session_id != self.origin_session_id
        )
