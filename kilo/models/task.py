"""Task data models for shell command execution."""

from datetime import datetime
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field

from kilo.clock import utc_now



class TaskStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    KILLED = "killed"
    TIMED_OUT = "timed_out"


class TaskInfo(BaseModel):
    task_id: str
    command: str
    pid: Optional[int] = None
    status: TaskStatus = TaskStatus.RUNNING
    start_time: datetime = Field(default_factory=utc_now)
    end_time: Optional[datetime] = None
    exit_code: Optional[int] = None
    log_file: str

    @property
    def duration_seconds(self) -> float:
        end = self.end_time or utc_now()
        return (end - self.start_time).total_seconds()


class ShellResult(BaseModel):
    action: str = "run"
    task_id: Optional[str] = None
    status: str
    command: Optional[str] = None
    stdout: str = ""
    stderr: str = ""
    exit_code: Optional[int] = None
    duration_seconds: Optional[float] = None
    message: Optional[str] = None
