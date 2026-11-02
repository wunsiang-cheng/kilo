"""API request and response schemas."""

from typing import Any, Dict, List, Optional
from pydantic import BaseModel


class ChatRequest(BaseModel):
    message: str
    stream: bool = True


class SSEEvent(BaseModel):
    event: str
    data: Dict[str, Any]
