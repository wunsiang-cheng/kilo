"""Anthropic Messages API transport: one streamed agent turn, with client tools."""

from typing import Any, AsyncGenerator, Dict, List, Optional

import anthropic

from kilo.config import settings

BETAS = [
    "thinking-display-updates-2026-08-18",  # thinking.display = "updates"
    "thinking-binding-controls-2026-08-01",  # thinking.block_binding
    "server-side-fallback-2026-07-01",  # fallbacks = "default"
    "compact-2026-09-04",  # compaction on demand; also needed on every request that carries the block
]

THINKING: Dict[str, Any] = {
    "type": "adaptive",
    # Progress notes between tool calls come back as readable thinking text; reasoning stays hidden.
    "display": "updates",
    # The tools array changes when MCP servers connect mid-session; drop invalidated thinking
    # blocks instead of failing the request.
    "block_binding": {"prefix_mismatch_behavior": "drop_block"},
}


def context_tokens(usage: Dict[str, Any]) -> int:
    """
    The conversation's size after a turn: what its last request read, plus what the reply added.
    A turn that reports iterations ran several rounds; the last one read the most.
    """
    rounds = [i for i in usage.get("iterations") or [] if i.get("type") != "compaction"]
    last = rounds[-1] if rounds else usage
    read = sum(last.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    return read + (last.get("output_tokens") or 0)


class ClaudeClient:
    def __init__(
        self,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        max_tokens: Optional[int] = None,
        client: Optional[anthropic.AsyncAnthropic] = None,
    ):
        self.model = model or settings.anthropic_model
        self.effort = effort or settings.anthropic_effort
        self.max_tokens = max_tokens or settings.anthropic_max_tokens
        # None lets the SDK resolve credentials; tests pass a client with a mock transport.
        self._client = client or anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    async def stream_turn(
        self,
        system: str,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Stream one assistant turn. Yields events:
          - {"type": "text", "delta": str}
          - {"type": "progress", "delta": str}   progress-update thinking text
          - {"type": "done", "stop_reason": str, "stop_details": dict | None, "content": list, "context_tokens": int}
        "content" is the assistant turn's content blocks, to be sent back unchanged; "context_tokens"
        is the conversation's size with this turn included.
        API failures raise the SDK's typed exceptions.
        """
        async with self._client.beta.messages.stream(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=messages,
            tools=tools,
            thinking=THINKING,
            output_config={"effort": self.effort},
            cache_control={"type": "ephemeral"},
            fallbacks="default",
            betas=BETAS,
        ) as stream:
            async for event in stream:
                if event.type == "text":
                    yield {"type": "text", "delta": event.text}
                elif event.type == "thinking":
                    if event.thinking:
                        yield {"type": "progress", "delta": event.thinking}
            final = await stream.get_final_message()

        yield {
            "type": "done",
            "stop_reason": final.stop_reason,
            "stop_details": final.stop_details.to_dict(mode="json") if final.stop_details else None,
            # exclude_unset (the default) echoes each block exactly as the API sent it.
            "content": [block.to_dict(mode="json", warnings=False) for block in final.content],
            "context_tokens": context_tokens(final.usage.to_dict(mode="json", warnings=False)),
        }

    async def compact(
        self,
        system: str,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        instructions: str,
        max_tokens: int,
    ) -> Dict[str, Any]:
        """
        Summarize the conversation on the server (on-demand compaction). Same system prompt, tools and
        thinking as the conversation's turns. Returns {"stop_reason", "stop_details", "content",
        "summary_tokens"}: stop_reason "compaction" with content [the signed compaction block] on
        success, otherwise the summarization call's own stop reason and no block.
        """
        # Streamed: the SDK refuses a request this large without streaming.
        async with self._client.beta.messages.stream(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            tools=tools,
            thinking=THINKING,
            output_config={"effort": self.effort},
            cache_control={"type": "ephemeral"},
            compaction={"type": "summarize", "instructions": instructions},
            betas=BETAS,
        ) as stream:
            final = await stream.get_final_message()
        usage = final.usage.to_dict(mode="json", warnings=False)
        summary = [i for i in usage.get("iterations") or [] if i.get("type") == "compaction"]
        return {
            "stop_reason": final.stop_reason,
            "stop_details": final.stop_details.to_dict(mode="json") if final.stop_details else None,
            "content": [block.to_dict(mode="json", warnings=False) for block in final.content],
            "summary_tokens": summary[-1].get("output_tokens", 0) if summary else 0,
        }
