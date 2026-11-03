"""API request and response schemas."""

from typing import Any, Dict, List, Optional
from pydantic import BaseModel


class AttachmentRef(BaseModel):
    """A file sent with a message: as the upload API returned it (its other fields, such as size, are ignored)."""

    path: str
    name: Optional[str] = None
    type: Optional[str] = None


class ChatRequest(BaseModel):
    message: str = ""
    attachments: List[AttachmentRef] = []
    stream: bool = True


class SSEEvent(BaseModel):
    event: str
    data: Dict[str, Any]
