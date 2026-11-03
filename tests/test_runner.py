"""The agent loop, with a scripted model and the real tools, storage and event stream."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from fakes import text_turn, tool_turn, until
from kilo.agent.history import history_events
from kilo.agent.runner import (
    COMPACTION_INSTRUCTIONS, COMPACTION_RETRY_TOKENS, CONTINUE_AFTER_COMPACTION, INTERRUPTED_TOOL_RESULT, SUMMARY_HEADER,
    _api_messages,
)
from kilo.config import settings
from kilo.models.session import MessageRole


async def collect(kilo, session_id, text, **options):
    return [event async for event in kilo.runner.run_chat_stream(session_id, text, **options)]


def names(events):
    return [event["event"] for event in events]


def interrupted(call_id: str) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": INTERRUPTED_TOOL_RESULT}


def summary_message(text: str) -> dict:
    return {"role": "user", "content": f"{SUMMARY_HEADER}\n\n{text}"}


@pytest.fixture
async def session(kilo):
    return await kilo.db.create_session()


async def test_text_reply_emits_deltas_and_done(kilo, model, session):
    model.turns.append(text_turn("hello"))
    events = await collect(kilo, session.id, "hi")
    assert names(events) == ["user_message", "message_delta", "done"]
    assert events[-1]["data"]["content"] == "hello"


async def test_reply_is_stored_and_sent_back_next_turn(kilo, model, session):
    model.turns.extend([text_turn("one"), text_turn("two")])
    await collect(kilo, session.id, "first")
    await collect(kilo, session.id, "second")
    assert [m["role"] for m in model.requests[1]] == ["user", "assistant", "user"]
    assert model.requests[1][1] == {"role": "assistant", "content": "one"}


async def test_tool_call_round_trip(kilo, model, session, workspace):
    model.turns.extend([tool_turn("c1", "write_file", {"path": "a.txt", "content": "hi"}), text_turn("done")])
    events = await collect(kilo, session.id, "write it")
    assert names(events) == ["user_message", "tool_call_start", "tool_call_result", "message_delta", "done"]
    assert (workspace / "a.txt").read_text() == "hi"
    last = model.requests[1][-1]
    assert last["role"] == "tool" and last["tool_call_id"] == "c1"


async def test_tool_alias_reports_the_canonical_name(kilo, model, session):
    model.turns.extend([tool_turn("c1", "write", {"path": "a.txt", "content": "hi"}), text_turn("done")])
    events = await collect(kilo, session.id, "write it")
    assert events[1]["data"]["name"] == "write_file"


async def test_tool_error_goes_back_as_an_error_result(kilo, model, session):
    model.turns.extend([tool_turn("c1", "read_file", {"path": "nope.txt"}), text_turn("sorry")])
    await collect(kilo, session.id, "read it")
    assert json.loads(model.requests[1][-1]["content"])["status"] == "error"


async def test_unknown_tool_is_reported_to_model(kilo, model, session):
    model.turns.extend([tool_turn("c1", "no_such_tool", {}), text_turn("sorry")])
    events = await collect(kilo, session.id, "go")
    assert "Unknown tool" in events[2]["data"]["result"]["message"]
    assert names(events)[-1] == "done"


async def test_unparsable_arguments_are_not_executed(kilo, model, session, workspace):
    model.turns.extend([tool_turn("c1", "write_file", "{not json"), text_turn("sorry")])
    events = await collect(kilo, session.id, "go")
    assert "Invalid arguments" in events[2]["data"]["result"]["message"]


async def test_interrupted_tool_call_gets_error_result(kilo, session):
    await kilo.db.add_message(session.id, MessageRole.USER, content="go")
    await kilo.db.add_message(session.id, MessageRole.ASSISTANT, payload=tool_turn("c1", "shell", {})[0]["message"])
    messages = _api_messages(await kilo.db.get_messages(session.id))
    assert messages[-1] == interrupted("c1")


async def test_interrupted_tool_call_is_closed_before_the_next_user_message(kilo, model, session):
    await kilo.db.add_message(session.id, MessageRole.USER, content="go")
    await kilo.db.add_message(session.id, MessageRole.ASSISTANT, payload=tool_turn("c1", "shell", {})[0]["message"])
    model.turns.append(text_turn("ok"))
    await collect(kilo, session.id, "still there?")
    assert [m["role"] for m in model.requests[0]] == ["user", "assistant", "tool", "user"]
    assert model.requests[0][2] == interrupted("c1")


async def test_each_result_is_its_own_tool_message(kilo, model, session):
    both = tool_turn("c1", "read_file", {"path": "x"})
    both[0]["message"]["tool_calls"].append({"id": "c2", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "y"}'}})
    model.turns.extend([both, text_turn("ok")])
    await collect(kilo, session.id, "go")
    assert [(m["role"], m["tool_call_id"]) for m in model.requests[1][-2:]] == [("tool", "c1"), ("tool", "c2")]


async def test_same_session_runs_are_serialized(kilo, model, session):
    release = asyncio.Event()
    model.turns.extend([[release.wait, *text_turn("a")], text_turn("b")])
    first = asyncio.create_task(collect(kilo, session.id, "one"))
    await until(lambda: model.requests)
    second = asyncio.create_task(collect(kilo, session.id, "two"))
    await asyncio.sleep(0.05)
    assert len(model.requests) == 1  # the second waits for the first
    release.set()
    await asyncio.gather(first, second)
    stored = await kilo.db.get_messages(session.id)
    assert [m.content for m in stored] == ["one", "a", "two", "b"]


async def test_different_sessions_run_at_once(kilo, model, session):
    other = await kilo.db.create_session()
    release = asyncio.Event()
    model.turns.extend([[release.wait, *text_turn("a")], text_turn("b")])
    first = asyncio.create_task(collect(kilo, session.id, "one"))
    await until(lambda: model.requests)
    await collect(kilo, other.id, "two")  # would hang if it waited for the first
    release.set()
    await first


async def test_stop_cancels_running_turn(kilo, model, session):
    never = asyncio.Event()
    model.turns.append([never.wait])
    running = asyncio.create_task(collect(kilo, session.id, "one"))
    await until(lambda: model.requests)
    assert kilo.runner.stop(session.id) is True
    with pytest.raises(asyncio.CancelledError):
        await running
    assert kilo.runner.stop(session.id) is False
    model.turns.append(text_turn("back"))
    assert names(await collect(kilo, session.id, "two"))[-1] == "done"


async def test_llm_exception_becomes_error_event(kilo, model, session):
    model.turns.append([RuntimeError("boom")])
    events = await collect(kilo, session.id, "hi")
    assert events[-1] == {"event": "error", "data": {"error": "LLM error: boom"}}


async def test_stream_without_final_message_is_an_error(kilo, model, session):
    model.turns.append([{"type": "text", "delta": "partial"}])
    assert "without a final message" in (await collect(kilo, session.id, "hi"))[-1]["data"]["error"]


async def test_content_filter_discards_turn(kilo, model, session):
    model.turns.append(text_turn("no", finish_reason="content_filter"))
    events = await collect(kilo, session.id, "hi")
    assert events[-1]["data"]["error"] == "The model declined this request (content filter)."
    assert [m.role for m in await kilo.db.get_messages(session.id)] == [MessageRole.USER]


async def test_max_turns_reached(kilo, model, session):
    model.turns.extend([tool_turn("c1", "glob", {"pattern": "*"}), tool_turn("c2", "glob", {"pattern": "*"})])
    events = await collect(kilo, session.id, "go", max_turns=2)
    assert "maximum allowed turns" in events[-1]["data"]["error"]


async def test_cut_off_tool_call_not_executed(kilo, model, session, workspace):
    args = {"path": "a.txt", "content": "half"}
    model.turns.extend([tool_turn("c1", "write_file", args, finish_reason="length"), text_turn("retry")])
    events = await collect(kilo, session.id, "go")
    assert "Not executed" in events[2]["data"]["result"]["message"]
    assert not (workspace / "a.txt").exists()


async def test_reply_cut_off_without_tools_is_an_error(kilo, model, session):
    model.turns.append(text_turn("partial", finish_reason="length"))
    assert (await collect(kilo, session.id, "hi"))[-1]["data"]["error"] == "Response stopped early (length)."


async def test_reply_without_text_or_tool_calls_is_not_stored(kilo, model, session):
    model.turns.extend([text_turn("", finish_reason="length"), text_turn("ok")])
    assert (await collect(kilo, session.id, "hi"))[-1]["data"]["error"] == "Response stopped early (length)."
    assert [m.role for m in await kilo.db.get_messages(session.id)] == [MessageRole.USER]
    assert names(await collect(kilo, session.id, "again"))[-1] == "done"
    assert model.requests[1] == [{"role": "user", "content": "hi"}, {"role": "user", "content": "again"}]


async def test_blank_arguments_mean_no_arguments(kilo, model, session):
    model.turns.extend([tool_turn("c1", "glob", ""), text_turn("ok")])
    events = await collect(kilo, session.id, "go")
    assert events[1]["data"]["arguments"] == {}
    assert "Invalid arguments" not in json.dumps(events[2]["data"]["result"])


@pytest.mark.parametrize("raw,sent_back", [
    ('{"pattern": "*"}', '{"pattern": "*"}'),  # a JSON object goes back exactly as the model wrote it
    ("", "{}"), ("{not json", "{}"), ('{"pattern": "*', "{}"), ("[1, 2]", "{}"),  # providers refuse these in history
])
async def test_tool_call_arguments_go_back_as_a_json_object(kilo, model, session, raw, sent_back):
    model.turns.extend([tool_turn("c1", "glob", raw), text_turn("ok")])
    await collect(kilo, session.id, "go")
    [call] = model.requests[1][1]["tool_calls"]
    assert call["function"]["arguments"] == sent_back


async def test_first_message_titles_session(kilo, model, session):
    model.turns.append(text_turn("ok"))
    await collect(kilo, session.id, "Fix the build\nand more")
    assert (await kilo.db.get_session(session.id)).title == "Fix the build"


@pytest.fixture
async def big_session(kilo, session, monkeypatch):
    """A session past the compaction threshold."""
    monkeypatch.setattr(settings, "kilo_compact_at_tokens", 10_000)
    await kilo.db.add_message(session.id, MessageRole.USER, content="old question")
    await kilo.db.add_message(session.id, MessageRole.ASSISTANT, content="old answer", payload={"role": "assistant", "content": "old answer"})
    await kilo.db.update_session(session.id, metadata={"context_tokens": 20_000})
    return session


async def test_auto_compaction_replaces_history(kilo, model, big_session):
    model.turns.extend([text_turn("the summary"), text_turn("ok")])
    events = await collect(kilo, big_session.id, "new question")
    assert names(events)[:2] == ["compaction_start", "compaction_done"]
    assert events[1]["data"] == {"context_tokens": 20_000 + len("new question") // 4, "summary_tokens": len("the summary") // 4}
    assert model.requests[0] == [
        {"role": "user", "content": "old question"}, {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": COMPACTION_INSTRUCTIONS},
    ]
    assert model.max_tokens[:2] == [settings.kilo_compact_max_tokens, None]
    assert model.requests[1] == [summary_message("the summary"), {"role": "user", "content": "new question"}]


async def test_failed_compaction_keeps_history_and_sets_retry(kilo, model, big_session):
    model.turns.extend([[RuntimeError("no summary")], text_turn("ok")])
    events = await collect(kilo, big_session.id, "new question")
    assert events[1] == {"event": "compaction_failed", "data": {"error": "LLM error: no summary"}}
    assert names(events)[-1] == "done"
    assert model.requests[1][0] == {"role": "user", "content": "old question"}
    meta = (await kilo.db.get_session(big_session.id, include_messages=False)).metadata
    assert meta["compact_retry_at"] >= 20_000 + COMPACTION_RETRY_TOKENS


async def test_summary_cut_off_is_retried_once_with_more_room(kilo, model, big_session):
    model.turns.extend([text_turn("", finish_reason="length"), text_turn("", finish_reason="length"), text_turn("ok")])
    events = await collect(kilo, big_session.id, "new question")
    assert model.max_tokens[:2] == [settings.kilo_compact_max_tokens, 2 * settings.kilo_compact_max_tokens]
    assert events[1] == {"event": "compaction_failed", "data": {"error": "no summary was written (finish reason: length)"}}


async def test_tool_calls_in_a_summary_are_ignored(kilo, model, big_session, workspace):
    summary = tool_turn("c9", "write_file", {"path": "x.txt", "content": "no"})
    summary[-1]["message"]["content"] = "the summary"
    model.turns.extend([summary, text_turn("ok")])
    events = await collect(kilo, big_session.id, "new question")
    assert names(events)[:2] == ["compaction_start", "compaction_done"]
    assert not (workspace / "x.txt").exists()
    assert model.requests[1][0] == summary_message("the summary")


async def test_a_summary_without_text_fails(kilo, model, big_session):
    model.turns.extend([tool_turn("c9", "glob", {"pattern": "*"}), text_turn("ok")])
    events = await collect(kilo, big_session.id, "new question")
    assert events[1]["data"]["error"] == "no summary was written (finish reason: tool_calls)"


async def test_a_summary_stream_without_a_final_message_fails(kilo, model, big_session):
    model.turns.extend([[{"type": "text", "delta": "partial"}], text_turn("ok")])
    events = await collect(kilo, big_session.id, "new question")
    assert events[1]["data"]["error"] == "LLM error: the response stream ended without a final message"


async def test_compaction_mid_loop_continues_the_task(kilo, model, session, monkeypatch):
    monkeypatch.setattr(settings, "kilo_compact_at_tokens", 10_000)
    call = tool_turn("c1", "glob", {"pattern": "*"})
    call[-1]["context_tokens"] = 20_000
    model.turns.extend([call, text_turn("the summary"), text_turn("finished")])
    events = await collect(kilo, session.id, "go")
    assert "compaction_done" in names(events) and names(events)[-1] == "done"
    assert model.requests[2] == [summary_message("the summary"), {"role": "user", "content": CONTINUE_AFTER_COMPACTION}]


async def test_manual_compaction(kilo, model, session):
    model.turns.extend([text_turn("hello"), text_turn("the summary")])
    await collect(kilo, session.id, "hi")
    assert (await kilo.runner.compact(session.id))["event"] == "compaction_done"
    assert (await kilo.db.get_session(session.id, include_messages=False)).metadata["compactions"] == 1
    assert (await kilo.db.get_messages(session.id))[-1].role == MessageRole.SUMMARY


async def test_manual_compact_on_empty_session(kilo, session):
    outcome = await kilo.runner.compact(session.id)
    assert outcome == {"event": "compaction_failed", "data": {"error": "Nothing to compact yet."}}


async def test_compaction_after_an_interrupted_tool_call(kilo, model, session):
    await kilo.db.add_message(session.id, MessageRole.USER, content="go")
    await kilo.db.add_message(session.id, MessageRole.ASSISTANT, payload=tool_turn("c1", "shell", {})[0]["message"])
    model.turns.append(text_turn("the summary"))
    assert (await kilo.runner.compact(session.id))["event"] == "compaction_done"
    assert model.requests[0][-2:] == [interrupted("c1"), {"role": "user", "content": COMPACTION_INSTRUCTIONS}]


async def test_history_replays_what_the_stream_sent(kilo, model, session):
    model.turns.extend([tool_turn("c1", "glob", {"pattern": "*"}), text_turn("done")])
    live = await collect(kilo, session.id, "go")
    replayed = history_events(await kilo.db.get_messages(session.id), kilo.toolbox.canonical_name)
    assert names(replayed) == names(live)[:-1]  # `done` is not stored
    assert replayed[1]["data"]["arguments"] == {"pattern": "*"}
    assert replayed[2]["data"]["result"]["status"] == "success"


async def test_history_replays_reasoning_and_summaries(kilo, model, session):
    reply = text_turn("hi")
    reply[-1]["message"]["reasoning"] = "thinking it over"
    model.turns.extend([[{"type": "progress", "delta": "thinking it over"}, *reply], text_turn("the summary")])
    live = await collect(kilo, session.id, "hello")
    await kilo.runner.compact(session.id)
    replayed = history_events(await kilo.db.get_messages(session.id), kilo.toolbox.canonical_name)
    assert names(replayed) == [*names(live)[:-1], "compaction_done"]
    assert replayed[1] == {"event": "progress", "data": {"delta": "thinking it over"}}


def test_history_shows_a_non_json_tool_result_as_raw_text():
    message = SimpleNamespace(
        role=MessageRole.TOOL, created_at=None, content="plain", payload={"role": "tool", "tool_call_id": "c", "content": "plain"},
    )
    assert history_events([message])[0]["data"]["result"] == {"raw": "plain"}


async def test_every_stored_result_is_json_the_model_can_read(kilo, model, session):
    model.turns.extend([tool_turn("c1", "glob", {"pattern": "*"}), text_turn("done")])
    await collect(kilo, session.id, "go")
    tool_message = [m for m in await kilo.db.get_messages(session.id) if m.role == MessageRole.TOOL][0]
    assert json.loads(tool_message.content)["status"] == "success"
