"""Session and Message data models."""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from kilo.clock import utc_now



class MessageRole(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    SUMMARY = "summary"  # what a compaction left of the conversation before it


class Message(BaseModel):
    id: str
    session_id: str
    role: MessageRole
    content: Optional[str] = None
    # The message as sent to OpenRouter (Chat Completions shape): an assistant turn as returned, or a tool result.
    # None for user messages and summaries.
    payload: Optional[Dict[str, Any]] = None
    created_at: datetime = Field(default_factory=utc_now)


class Session(BaseModel):
    id: str
    title: str
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    messages: List[Message] = Field(default_factory=list)


class SessionCreate(BaseModel):
    title: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class SessionUpdate(BaseModel):
    title: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
