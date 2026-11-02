"""The agent turn loop's view of an LLM."""

from typing import Any, AsyncIterator, Dict, List, Protocol


class ChatModel(Protocol):
    """
    What the agent turn loop needs from an LLM: one streamed assistant turn per call,
    in Anthropic Messages API shapes (system prompt, content-block messages, tool definitions).
    stream_turn yields, in order:
      - {"type": "text", "delta": str} for each text delta
      - {"type": "progress", "delta": str} for progress-update text between tool calls
      - {"type": "done", "stop_reason": str, "stop_details": dict | None, "content": list, "context_tokens": int}
        last; "content" is the turn's content blocks (thinking, text, tool_use, ...) to send back unchanged,
        "context_tokens" the conversation's size with this turn included
    compact summarizes the conversation instead of taking a turn: it returns {"stop_reason": "compaction",
    "content": [compaction block], "summary_tokens": int}, or another stop_reason and no block when no
    summary was written.
    Failures are raised, not yielded.
    """

    def stream_turn(
        self,
        system: str,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
    ) -> AsyncIterator[Dict[str, Any]]: ...

    async def compact(
        self,
        system: str,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        instructions: str,
        max_tokens: int,
    ) -> Dict[str, Any]: ...
