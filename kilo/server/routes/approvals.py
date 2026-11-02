"""Approval API routes: the user's answers to the schedules the agent proposes (the only way a proposal takes effect)."""

from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException, Query

from kilo.assembly import get_kilo
from kilo.scheduling.scheduler import ScheduleError, ScheduleNotFound, describe

router = APIRouter(prefix="/approvals", tags=["approvals"])


def _http_error(e: ScheduleError) -> HTTPException:
    return HTTPException(status_code=404 if isinstance(e, ScheduleNotFound) else 400, detail=str(e))


@router.get("")
async def list_approvals(session_id: Optional[str] = Query(None)) -> List[Dict[str, Any]]:
    """Schedules with a proposal awaiting approval (of those made in a session, when given), oldest first."""
    return [describe(s) for s in await get_kilo().scheduler.awaiting_approval(session_id)]


@router.post("/{schedule_id}/approve")
async def approve(schedule_id: str) -> Dict[str, Any]:
    """Approve the schedule's proposal: it then runs as proposed."""
    try:
        return describe(await get_kilo().scheduler.approve(schedule_id))
    except ScheduleError as e:
        raise _http_error(e)


@router.post("/{schedule_id}/reject")
async def reject(schedule_id: str) -> Dict[str, Any]:
    """Refuse the schedule's proposal: a new schedule is deleted, a change dropped."""
    try:
        schedule = await get_kilo().scheduler.reject(schedule_id)
    except ScheduleError as e:
        raise _http_error(e)
    if schedule is None:
        return {"status": "success", "message": f"Schedule '{schedule_id}' was refused and deleted."}
    return {"status": "success", "message": f"The change to schedule '{schedule_id}' was refused.", "schedule": describe(schedule)}
