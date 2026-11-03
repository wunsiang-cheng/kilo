"""Session management and chat routes."""

import json
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException, Query
from sse_starlette.sse import EventSourceResponse

from kilo import uploads
from kilo.agent.history import history_events
from kilo.assembly import get_kilo
from kilo.models.api import ChatRequest
from kilo.models.session import Session, SessionCreate, SessionUpdate

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post("", response_model=Session, status_code=201)
async def create_session(payload: Optional[SessionCreate] = None) -> Session:
    """Create a new agent session."""
    db = get_kilo().db
    title = payload.title if payload else None
    meta = payload.metadata if payload else None
    session = await db.create_session(title=title, metadata=meta)
    return session


@router.get("", response_model=List[Session])
async def list_sessions(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> List[Session]:
    """List sessions ordered by last updated."""
    db = get_kilo().db
    return await db.list_sessions(limit=limit, offset=offset)


@router.get("/{session_id}", response_model=Session)
async def get_session(session_id: str) -> Session:
    """Get session by ID along with its message history."""
    db = get_kilo().db
    session = await db.get_session(session_id, include_messages=True)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found.")
    return session


@router.get("/{session_id}/events")
async def get_session_events(session_id: str) -> Dict[str, Any]:
    """
    The session (without messages) and its conversation as the events a chat streams, to show it again,
    with how each run of a schedule made in it ended.
    """
    kilo = get_kilo()
    session = await kilo.db.get_session(session_id, include_messages=True)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found.")
    runs = await kilo.schedule_store.finished_runs_from(session_id)
    return {
        "session": session.model_dump(mode="json", exclude={"messages"}),
        "events": history_events(session.messages, kilo.toolbox.canonical_name, runs),
    }


@router.patch("/{session_id}", response_model=Session)
async def update_session(session_id: str, payload: SessionUpdate) -> Session:
    """Update session title or metadata."""
    db = get_kilo().db
    title = payload.title.strip() if payload.title is not None else None
    if title == "":
        raise HTTPException(status_code=400, detail="Title cannot be empty.")
    session = await db.update_session(session_id, title=title, metadata=payload.metadata)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found.")
    return session


@router.delete("/{session_id}")
async def delete_session(session_id: str) -> Dict[str, Any]:
    """Delete session and its associated messages."""
    db = get_kilo().db
    deleted = await db.delete_session(session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found.")
    return {"status": "success", "message": f"Session '{session_id}' deleted."}


@router.post("/{session_id}/compact")
async def compact_session(session_id: str) -> Dict[str, Any]:
    """
    Summarize the session's history now (as auto-compaction does at KILO_COMPACT_AT_TOKENS).
    Waits for a run in progress on the session. Returns the compaction_done or compaction_failed event.
    """
    if not await get_kilo().db.get_session(session_id, include_messages=False):
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found.")
    return await get_kilo().runner.compact(session_id)


@router.post("/{session_id}/stop")
async def stop_session_turn(session_id: str) -> Dict[str, Any]:
    """
    Stop the session's turn in progress, whoever started it: the user or a schedule. A stopped scheduled run is recorded as failed ("Stopped."). Says whether there was one.
    """
    if not await get_kilo().db.get_session(session_id, include_messages=False):
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found.")
    return {"stopped": get_kilo().runner.stop(session_id)}


@router.post("/{session_id}/chat")
async def chat_session(session_id: str, req: ChatRequest):
    """
    Send a message to the session and run the agent loop.
    Streams SSE events by default.
    """
    db, runner = get_kilo().db, get_kilo().runner
    session = await db.get_session(session_id, include_messages=False)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found.")

    if not req.message.strip() and not req.attachments:
        raise HTTPException(status_code=400, detail="Message cannot be empty.")
    try:
        # Each must be a file in uploads/; its size is read from the disk, whatever the request says.
        attachments = [uploads.attachment_at(a.path, a.name, a.type) for a in req.attachments]
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if req.stream:
        async def event_generator():
            async for ev in runner.run_chat_stream(session_id=session_id, user_message=req.message, attachments=attachments):
                yield {
                    "event": ev["event"],
                    "data": json.dumps(ev["data"]),
                }

        return EventSourceResponse(event_generator())
    else:
        # Non-streaming collection
        events = []
        final_content = ""
        async for ev in runner.run_chat_stream(session_id=session_id, user_message=req.message, attachments=attachments):
            events.append(ev)
            if ev["event"] == "done":
                final_content = ev["data"].get("content", "")
        return {
            "session_id": session_id,
            "response": final_content,
            "events": events,
        }
