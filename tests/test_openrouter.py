"""OpenRouterClient against SSE in the shape OpenRouter streams it (recorded 2026-11-03), through httpx's real streaming code."""

import json
import re
from typing import Any, Dict, List, Optional

import httpx
import pytest

from kilo.openrouter import OpenRouterClient, OpenRouterError

MODEL = "meta/muse-spark-1.3-contributor"
KEEPALIVE = ": OPENROUTER PROCESSING"
USAGE = {
    "prompt_tokens": 685, "completion_tokens": 794, "total_tokens": 1479, "cost": 0.000216,
    "prompt_tokens_details": {"cached_tokens": 113}, "completion_tokens_details": {"reasoning_tokens": 606},
}


def chunk(delta: Optional[Dict[str, Any]] = None, finish: Optional[str] = None, **extra: Any) -> Dict[str, Any]:
    """One streamed chunk, as OpenRouter sends it."""
    choice = {
        "index": 0, "delta": {"role": "assistant", **(delta or {})},
        "finish_reason": finish, "native_finish_reason": "completed" if finish else None,
    }
    return {"id": "gen-1", "object": "chat.completion.chunk", "model": MODEL, "provider": "Meta", "choices": [choice], **extra}


def sse(*items: Any) -> bytes:
    """A streamed body: a `data:` line per chunk (a str goes as is, e.g. a keepalive comment), then [DONE]."""
    lines = [item if isinstance(item, str) else "data: " + json.dumps(item, ensure_ascii=False) for item in items]
    return ("\n\n".join([*lines, "data: [DONE]"]) + "\n\n").encode()


TEXT = sse(
    chunk({"content": "", "reasoning": None, "reasoning_details": [
        {"type": "reasoning.encrypted", "data": "enc-a", "format": "meta-responses-v1", "id": "rs_1", "index": 0},
    ]}),
    chunk({"content": "你好"}),
    chunk({"content": ""}, finish="stop"),
    chunk({"content": ""}, finish="stop", usage={"prompt_tokens": 41, "completion_tokens": 183, "total_tokens": 224}),
)

TOOLS = sse(
    KEEPALIVE,
    chunk({"content": "", "reasoning": "Planning both calls.", "reasoning_details": [
        {"type": "reasoning.summary", "summary": "Planning both calls.", "format": "meta-responses-v1", "index": 0},
    ]}),
    KEEPALIVE,
    chunk({"content": "", "reasoning": None, "reasoning_details": [
        {"type": "reasoning.encrypted", "data": "enc-b", "format": "meta-responses-v1", "id": "rs_2", "index": 1},
    ]}),
    chunk({"content": "Doing both."}),
    chunk({"content": None, "tool_calls": [
        {"index": 0, "id": "call_a", "type": "function", "function": {"name": "write_file", "arguments": ""}}]}),
    chunk({"content": None, "tool_calls": [{"index": 0, "function": {"arguments": '{"path":"hello.txt"'}}]}),
    chunk({"content": None, "tool_calls": [{"index": 0, "function": {"arguments": ',"content":"MANGO"}'}}]}),
    chunk({"content": None, "tool_calls": [
        {"index": 1, "id": "call_b", "type": "function", "function": {"name": "glob", "arguments": '{"pattern":"*.txt"}'}}]}),
    chunk({"content": ""}, finish="tool_calls"),
    chunk({"content": ""}, finish="tool_calls", usage=USAGE),
)

TOOL_DEFINITION = {"type": "function", "function": {"name": "glob", "description": "d", "parameters": {"type": "object", "properties": {}}}}


def streaming(body: Any):
    return lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)


def client_for(responder, requests: Optional[List[httpx.Request]] = None, **options: Any) -> OpenRouterClient:
    def handle(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        return responder(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    return OpenRouterClient(model=MODEL, effort="low", api_key="test-key", http=http, **options)


async def turn(client: OpenRouterClient, tools: Any = (), max_tokens: Optional[int] = None) -> List[Dict[str, Any]]:
    messages = [{"role": "user", "content": "hi"}]
    return [event async for event in client.stream_turn("sys", messages, list(tools), max_tokens=max_tokens)]


async def test_text_reply_streams_then_finishes():
    events = await turn(client_for(streaming(TEXT)))
    assert events[:-1] == [{"type": "text", "delta": "你好"}]
    assert events[-1] == {
        "type": "done", "finish_reason": "stop", "context_tokens": 41 + 183,
        "message": {"role": "assistant", "content": "你好", "reasoning_details": [
            {"type": "reasoning.encrypted", "data": "enc-a", "format": "meta-responses-v1", "id": "rs_1", "index": 0},
        ]},
    }


async def test_parallel_tool_calls_are_assembled():
    events = await turn(client_for(streaming(TOOLS)))
    assert events[:-1] == [{"type": "progress", "delta": "Planning both calls."}, {"type": "text", "delta": "Doing both."}]
    assert events[-1] == {
        "type": "done", "finish_reason": "tool_calls", "context_tokens": 685 + 794,
        "message": {
            "role": "assistant", "content": "Doing both.", "reasoning": "Planning both calls.",
            "reasoning_details": [
                {"type": "reasoning.summary", "summary": "Planning both calls.", "format": "meta-responses-v1", "index": 0},
                {"type": "reasoning.encrypted", "data": "enc-b", "format": "meta-responses-v1", "id": "rs_2", "index": 1},
            ],
            "tool_calls": [
                {"id": "call_a", "type": "function", "function": {"name": "write_file", "arguments": '{"path":"hello.txt","content":"MANGO"}'}},
                {"id": "call_b", "type": "function", "function": {"name": "glob", "arguments": '{"pattern":"*.txt"}'}},
            ],
        },
    }


async def test_lines_split_across_network_chunks_are_reassembled():
    async def pieces():
        for start in range(0, len(TEXT), 7):  # cuts lines, and 你好 mid-character
            yield TEXT[start:start + 7]

    assert await turn(client_for(streaming(pieces()))) == await turn(client_for(streaming(TEXT)))


async def test_reasoning_detail_fragments_are_joined_by_index():
    body = sse(
        chunk({"reasoning": "Hel", "reasoning_details": [{"type": "reasoning.text", "text": "Hel", "index": 0}]}),
        chunk({"reasoning": "lo", "reasoning_details": [{"type": "reasoning.text", "text": "lo", "signature": "sig", "index": 0}]}),
        chunk({"content": "ok"}, finish="stop"),
    )
    events = await turn(client_for(streaming(body)))
    assert [event["delta"] for event in events if event["type"] == "progress"] == ["Hel", "lo"]
    assert events[-1]["message"]["reasoning"] == "Hello"
    assert events[-1]["message"]["reasoning_details"] == [{"type": "reasoning.text", "text": "Hello", "signature": "sig", "index": 0}]


async def test_a_tool_call_without_an_id_gets_one():
    body = sse(chunk({"tool_calls": [{"index": 0, "function": {"name": "glob", "arguments": "{}"}}]}), chunk({}, finish="tool_calls"))
    [call] = (await turn(client_for(streaming(body))))[-1]["message"]["tool_calls"]
    assert re.fullmatch(r"call_[0-9a-f]{24}", call["id"])


async def test_blank_arguments_are_kept_as_sent():
    body = sse(
        chunk({"tool_calls": [{"index": 0, "id": "c1", "type": "function", "function": {"name": "glob", "arguments": ""}}]}),
        chunk({}, finish="tool_calls"),
    )
    [call] = (await turn(client_for(streaming(body))))[-1]["message"]["tool_calls"]
    assert call == {"id": "c1", "type": "function", "function": {"name": "glob", "arguments": ""}}


async def test_without_usage_the_context_is_not_measured():
    done = (await turn(client_for(streaming(sse(chunk({"content": "ok"}, finish="stop"))))))[-1]
    assert done["context_tokens"] is None


async def test_request_carries_model_effort_and_the_system_prompt_first():
    requests: List[httpx.Request] = []
    await turn(client_for(streaming(TEXT), requests))
    [request] = requests
    body = json.loads(request.content)
    assert request.url == "https://openrouter.ai/api/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer test-key"
    assert body["model"] == MODEL and body["stream"] is True and body["reasoning"] == {"effort": "low"}
    assert body["messages"] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    assert "tools" not in body and "max_tokens" not in body


async def test_tools_are_sent_as_given():
    requests: List[httpx.Request] = []
    await turn(client_for(streaming(TEXT), requests), tools=[TOOL_DEFINITION])
    assert json.loads(requests[0].content)["tools"] == [TOOL_DEFINITION]


@pytest.mark.parametrize("configured,given,sent", [(1000, None, 1000), (1000, 50, 50), (None, 50, 50)])
async def test_max_tokens_given_for_the_call_wins(configured, given, sent):
    requests: List[httpx.Request] = []
    await turn(client_for(streaming(TEXT), requests, max_tokens=configured), max_tokens=given)
    assert json.loads(requests[0].content)["max_tokens"] == sent


async def test_an_error_in_the_stream_is_raised():
    failed = {
        "id": "gen-1", "object": "chat.completion.chunk", "error": {"code": 502, "message": "Provider disconnected"},
        "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "error"}],
    }
    with pytest.raises(OpenRouterError, match="Provider disconnected") as caught:
        await turn(client_for(streaming(sse(chunk({"content": "par"}), failed))))
    assert caught.value.status == 502


async def test_finish_reason_error_is_raised():
    with pytest.raises(OpenRouterError, match="ended with an error"):
        await turn(client_for(streaming(sse(chunk({"content": "par"}), chunk({}, finish="error")))))


async def test_a_stream_that_ends_before_the_reply_finishes_is_raised():
    with pytest.raises(OpenRouterError, match="ended before the reply finished"):
        await turn(client_for(streaming(sse(chunk({"content": "par"})))))


async def test_without_an_api_key_nothing_is_sent():
    requests: List[httpx.Request] = []
    client = OpenRouterClient(model=MODEL, http=httpx.AsyncClient(transport=httpx.MockTransport(requests.append)))
    with pytest.raises(OpenRouterError, match="OPENROUTER_API_KEY is not set"):
        await turn(client)
    assert requests == []
