"""Who is calling a tool. Set by the turn loop; read by code that records its caller."""

from contextvars import ContextVar
from typing import Optional

# The session whose agent turn is executing the current tool call.
current_session_id: ContextVar[Optional[str]] = ContextVar("current_session_id", default=None)