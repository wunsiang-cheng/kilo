"""OpenRouter Chat Completions transport: one streamed agent turn, with client tools."""

import asyncio
import json
import uuid
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional

import httpx

from kilo.config import settings

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# OpenRouter sends keepalive comments while the model works, so the long read timeout only trips on a dead connection.
TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=10.0)
# Fields of a streamed reasoning detail that arrive in pieces; every other field arrives whole.
JOINED_DETAIL_FIELDS = ("text", "summary", "data")


class OpenRouterError(Exception):
    """A request OpenRouter refused, or a reply that failed. `status` is the HTTP (or error) code, when there is one."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status

    def __str__(self) -> str:
        message = self.args[0]
        return f"OpenRouter {self.status}: {message}" if self.status is not None else f"OpenRouter: {message}"


class _Reply:
    """One assistant turn, put together from the chunks it streams in."""

    def __init__(self) -> None:
        self.text = ""
        self.reasoning = ""
        self.details: Dict[Any, Dict[str, Any]] = {}  # reasoning details by index, in order of first appearance
        self.calls: Dict[Any, Dict[str, str]] = {}  # tool calls by index, in order of first appearance
        self.finish_reason: Optional[str] = None
        self.usage: Optional[Dict[str, Any]] = None

    def add(self, chunk: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Take one chunk; returns the progress and text events it carries."""
        error = chunk.get("error")
        if error:
            code = error.get("code")
            raise OpenRouterError(error.get("message") or "the reply failed", code if isinstance(code, int) else None)
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        events: List[Dict[str, Any]] = []
        for choice in chunk.get("choices") or []:  # one choice is asked for
            delta = choice.get("delta") or {}
            if delta.get("reasoning"):
                self.reasoning += delta["reasoning"]
                events.append({"type": "progress", "delta": delta["reasoning"]})
            if delta.get("content"):
                self.text += delta["content"]
                events.append({"type": "text", "delta": delta["content"]})
            for detail in delta.get("reasoning_details") or []:
                self._add_detail(detail)
            for call in delta.get("tool_calls") or []:
                self._add_call(call)
            finish = choice.get("finish_reason")
            if finish == "error":
                raise OpenRouterError(f"the reply ended with an error ({choice.get('native_finish_reason') or 'no details'})")
            if finish:
                self.finish_reason = finish
        return events

    def _add_detail(self, detail: Dict[str, Any]) -> None:
        index = detail.get("index")
        slot = self.details.setdefault(index if index is not None else ("own", len(self.details)), {})
        for field, value in detail.items():
            if field in JOINED_DETAIL_FIELDS and isinstance(value, str):
                slot[field] = slot.get(field, "") + value
            elif value is not None:
                slot[field] = value

    def _add_call(self, call: Dict[str, Any]) -> None:
        slot = self.calls.setdefault(call.get("index", 0), {"id": "", "name": "", "arguments": ""})
        function = call.get("function") or {}
        if call.get("id") and not slot["id"]:
            slot["id"] = call["id"]
        if function.get("name") and not slot["name"]:
            slot["name"] = function["name"]
        slot["arguments"] += function.get("arguments") or ""

    def message(self) -> Dict[str, Any]:
        """The assistant turn as it is stored and sent back: role and content, then only the fields that have a value."""
        message: Dict[str, Any] = {"role": "assistant", "content": self.text or None}
        if self.reasoning:
            message["reasoning"] = self.reasoning
        if self.details:
            message["reasoning_details"] = list(self.details.values())
        if self.calls:
            message["tool_calls"] = [
                {
                    "id": call["id"] or f"call_{uuid.uuid4().hex[:24]}",  # a result must name the call it answers
                    "type": "function",
                    "function": {"name": call["name"], "arguments": call["arguments"]},
                }
                for call in self.calls.values()
            ]
        return message

    def context_tokens(self) -> Optional[int]:
        """The conversation's size with this turn included: what the request read, plus what the reply added."""
        if not self.usage:
            return None
        return (self.usage.get("prompt_tokens") or 0) + (self.usage.get("completion_tokens") or 0)


class OpenRouterClient:
    def __init__(
        self,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        max_tokens: Optional[int] = None,
        api_key: Optional[str] = None,
        http: Optional[httpx.AsyncClient] = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.model = model or settings.openrouter_model
        self.effort = effort or settings.openrouter_reasoning_effort
        self.max_tokens = max_tokens if max_tokens is not None else settings.openrouter_max_tokens
        self.api_key = api_key or settings.openrouter_api_key
        # Tests pass a client with a mock transport.
        self._http = http or httpx.AsyncClient(timeout=TIMEOUT)
        self._sleep = sleep

    async def stream_turn(
        self,
        system: str,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        Stream one assistant turn. Yields events:
          - {"type": "text", "delta": str}
          - {"type": "progress", "delta": str}   readable reasoning text
          - {"type": "done", "finish_reason": str, "message": dict, "context_tokens": int | None}
        "message" is the assistant turn (content, reasoning, reasoning_details, tool_calls), to be stored and sent
        back; "context_tokens" is the conversation's size with this turn included. `max_tokens` caps this reply,
        in place of the client's own cap. Failures raise OpenRouterError.
        """
        if not self.api_key:
            raise OpenRouterError("OPENROUTER_API_KEY is not set.")
        body: Dict[str, Any] = {
            "model": self.model,
            "stream": True,
            "messages": [{"role": "system", "content": system}, *messages],
            "reasoning": {"effort": self.effort},
        }
        if tools:
            body["tools"] = tools
        cap = max_tokens if max_tokens is not None else self.max_tokens
        if cap is not None:
            body["max_tokens"] = cap
        request = self._http.build_request(
            "POST", OPENROUTER_URL, json=body, headers={"Authorization": f"Bearer {self.api_key}"},
        )

        reply = _Reply()
        response = await self._http.send(request, stream=True)
        try:
            if response.status_code >= 400:
                await response.aread()
                raise OpenRouterError(response.text[:500] or response.reason_phrase, response.status_code)
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue  # a blank line ends an event; a line starting with ":" is a keepalive comment
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    break
                for event in reply.add(json.loads(data)):
                    yield event
        finally:
            await response.aclose()

        if reply.finish_reason is None:
            raise OpenRouterError("the response stream ended before the reply finished")
        yield {
            "type": "done",
            "finish_reason": reply.finish_reason,
            "message": reply.message(),
            "context_tokens": reply.context_tokens(),
        }
