"""Task management API routes."""

from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from kilo.models.task import ShellResult, TaskInfo
from kilo.assembly import get_kilo

router = APIRouter(prefix="/tasks", tags=["tasks"])


class TaskRunRequest(BaseModel):
    command: str
    timeout: Optional[int] = None  # None: KILO_DEFAULT_TIMEOUT


@router.get("", response_model=List[TaskInfo])
async def list_tasks(status: Optional[str] = Query(None)) -> List[TaskInfo]:
    """List all tracked tasks, optionally filtered by status (running, completed, failed, killed, timed_out)."""
    tm = get_kilo().task_manager
    return tm.list_tasks(status=status)


@router.get("/{task_id}", response_model=TaskInfo)
async def get_task(task_id: str) -> TaskInfo:
    """Get details for a specific task."""
    tm = get_kilo().task_manager
    task = tm.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found.")
    return task


@router.get("/{task_id}/logs")
async def get_task_logs(task_id: str, tail: int = Query(50, ge=1, le=1000)) -> Dict[str, Any]:
    """Get stdout/stderr logs for a specific task."""
    tm = get_kilo().task_manager
    ok, logs = tm.get_task_logs(task_id, tail=tail)
    if not ok:
        raise HTTPException(status_code=404, detail=logs)
    return {
        "task_id": task_id,
        "tail": tail,
        "logs": logs,
    }


@router.post("/run", response_model=ShellResult)
async def run_command(req: TaskRunRequest) -> ShellResult:
    """Directly execute a shell command via the TaskManager."""
    tm = get_kilo().task_manager
    return await tm.run(command=req.command, timeout=req.timeout)
