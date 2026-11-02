"""ClaudeClient against recorded-shape API responses, through the SDK's real streaming code."""

import json
from typing import Any, Dict, List

import anthropic
import httpx2
import pytest

from kilo.claude import ClaudeClient


def sse(*events: Dict[str, Any]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


USAGE = {"input_tokens": 10, "output_tokens": 1, "cache_read_input_tokens": 5, "cache_creation_input_tokens": 0}
MESSAGE = {
    "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-haiku-5-5", "content": [],
    "stop_reason": None, "stop_sequence": None, "usage": USAGE,
}


def text_stream(text: str) -> bytes:
    return sse(
        {"type": "message_start", "message": MESSAGE},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 3}},
        {"type": "message_stop"},
    )


def tool_stream() -> bytes:
    return sse(
        {"type": "message_start", "message": MESSAGE},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "c1", "name": "glob", "input": {}}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"pattern": "*"}'}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None}, "usage": {"output_tokens": 3}},
        {"type": "message_stop"},
    )


def client_for(requests: List[httpx2.Request], responder) -> ClaudeClient:
    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return responder(request)

    sdk = anthropic.AsyncAnthropic(
        api_key="test-key", base_url="http://test.invalid", max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handle)),
    )
    return ClaudeClient(model="claude-haiku-5-5", effort="low", max_tokens=1000, client=sdk)


def streaming(body: bytes):
    return lambda request: httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body)


async def turn(client: ClaudeClient) -> List[Dict[str, Any]]:
    messages = [{"role": "user", "content": "hi"}]
    return [event async for event in client.stream_turn("system", messages, [])]


async def test_stream_yields_text_then_done():
    events = await turn(client_for([], streaming(text_stream("hello"))))
    assert events[0] == {"type": "text", "delta": "hello"}
    done = events[-1]
    assert done["type"] == "done" and done["stop_reason"] == "end_turn"
    assert done["content"] == [{"type": "text", "text": "hello"}]


async def test_done_measures_the_whole_context():
    done = (await turn(client_for([], streaming(text_stream("hello")))))[-1]
    assert done["context_tokens"] == 10 + 5 + 3


async def test_tool_use_block_comes_back_whole():
    done = (await turn(client_for([], streaming(tool_stream()))))[-1]
    assert done["stop_reason"] == "tool_use"
    assert done["content"] == [{"type": "tool_use", "id": "c1", "name": "glob", "input": {"pattern": "*"}}]


async def test_request_carries_model_effort_max_tokens():
    requests: List[httpx2.Request] = []
    await turn(client_for(requests, streaming(text_stream("hello"))))
    body = json.loads(requests[0].content)
    assert body["model"] == "claude-haiku-5-5"
    assert body["max_tokens"] == 1000
    assert body["output_config"] == {"effort": "low"}
    assert body["stream"] is True


async def test_request_offers_only_the_tools_given():
    requests: List[httpx2.Request] = []
    await turn(client_for(requests, streaming(text_stream("hello"))))
    assert json.loads(requests[0].content)["tools"] == []


async def test_api_error_is_raised():
    failing = lambda request: httpx2.Response(
        429, json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}},
    )
    with pytest.raises(anthropic.RateLimitError):
        await turn(client_for([], failing))


def compaction_stream(summary: str = "summary") -> bytes:
    iterations = [{"type": "compaction", "input_tokens": 10, "output_tokens": 42}]
    return sse(
        {"type": "message_start", "message": MESSAGE},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "compaction", "content": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "compaction_delta", "content": summary}},
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta", "delta": {"stop_reason": "compaction", "stop_sequence": None},
            "usage": {"output_tokens": 42, "iterations": iterations},
        },
        {"type": "message_stop"},
    )


async def compact(client: ClaudeClient, max_tokens: int = 500) -> Dict[str, Any]:
    return await client.compact("system", [{"role": "user", "content": "hi"}], [], "Summarize.", max_tokens)


async def test_compact_accepts_the_retry_size():
    client = client_for([], streaming(compaction_stream()))
    assert (await compact(client, max_tokens=32_000))["stop_reason"] == "compaction"


async def test_compact_returns_the_summary_block():
    requests: List[httpx2.Request] = []
    result = await compact(client_for(requests, streaming(compaction_stream("the summary"))))
    assert result["stop_reason"] == "compaction"
    [block] = result["content"]
    assert block["type"] == "compaction" and block["content"] == "the summary"
    assert result["summary_tokens"] == 42
    assert json.loads(requests[0].content)["compaction"]["instructions"] == "Summarize."
