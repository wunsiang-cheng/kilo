"""The agent turn loop's view of an LLM."""

from typing import Any, AsyncIterator, Dict, List, Optional, Protocol


class ChatModel(Protocol):
    """
    What the agent turn loop needs from an LLM: one streamed assistant turn per call, in Chat Completions shapes
    (a system prompt; user, assistant and tool messages; function tool definitions).
    stream_turn yields, in order:
      - {"type": "text", "delta": str} for each text delta
      - {"type": "progress", "delta": str} for readable reasoning text
      - {"type": "done", "finish_reason": str, "message": dict, "context_tokens": int | None} last;
        "finish_reason" is "stop", "tool_calls", "length" or "content_filter"; "message" is the assistant turn
        (content, reasoning, reasoning_details, tool_calls) to store and send back; "context_tokens" is the
        conversation's size with this turn included
    `max_tokens` caps this one reply (a summary's) in place of the model's configured cap.
    Failures are raised, not yielded.
    takes_images says whether user messages may carry images (image_url parts); True when that cannot be told.
    """

    def stream_turn(
        self,
        system: str,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[Dict[str, Any]]: ...

    async def takes_images(self) -> bool: ...
