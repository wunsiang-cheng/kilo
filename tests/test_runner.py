"""The agent loop, with a scripted model and the real tools, storage and event stream."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from fakes import text_turn, tool_turn, until
from kilo.agent.history import history_events
from kilo.agent.runner import COMPACTION_RETRY_TOKENS, INTERRUPTED_TOOL_RESULT, _api_messages
from kilo.config import settings
from kilo.models.session import MessageRole


async def collect(kilo, session_id, text, **options):
    return [event async for event in kilo.runner.run_chat_stream(session_id, text, **options)]


def names(events):
    return [event["event"] for event in events]


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
    assert model.requests[1][1]["content"] == [{"type": "text", "text": "one"}]


async def test_tool_call_round_trip(kilo, model, session, workspace):
    model.turns.extend([tool_turn("c1", "write_file", {"path": "a.txt", "content": "hi"}), text_turn("done")])
    events = await collect(kilo, session.id, "write it")
    assert names(events) == ["user_message", "tool_call_start", "tool_call_result", "message_delta", "done"]
    assert (workspace / "a.txt").read_text() == "hi"
    last = model.requests[1][-1]
    assert last["role"] == "user" and last["content"][0]["tool_use_id"] == "c1"


async def test_tool_alias_reports_the_canonical_name(kilo, model, session):
    model.turns.extend([tool_turn("c1", "write", {"path": "a.txt", "content": "hi"}), text_turn("done")])
    events = await collect(kilo, session.id, "write it")
    assert events[1]["data"]["name"] == "write_file"


async def test_tool_error_marks_is_error(kilo, model, session):
    model.turns.extend([tool_turn("c1", "read_file", {"path": "nope.txt"}), text_turn("sorry")])
    await collect(kilo, session.id, "read it")
    assert model.requests[1][-1]["content"][0]["is_error"] is True


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
    await kilo.db.add_message(session.id, MessageRole.ASSISTANT, blocks=tool_turn("c1", "shell", {})[0]["content"])
    messages = _api_messages(await kilo.db.get_messages(session.id))
    result = messages[-1]["content"][0]
    assert result == {"type": "tool_result", "tool_use_id": "c1", "content": INTERRUPTED_TOOL_RESULT, "is_error": True}


async def test_interrupted_tool_call_is_closed_before_the_next_user_message(kilo, model, session):
    await kilo.db.add_message(session.id, MessageRole.USER, content="go")
    await kilo.db.add_message(session.id, MessageRole.ASSISTANT, blocks=tool_turn("c1", "shell", {})[0]["content"])
    model.turns.append(text_turn("ok"))
    await collect(kilo, session.id, "still there?")
    assert [m["role"] for m in model.requests[0]] == ["user", "assistant", "user", "user"]
    assert model.requests[0][2]["content"][0]["is_error"] is True


async def test_results_of_one_turn_share_one_user_message(kilo, model, session):
    both = tool_turn("c1", "read_file", {"path": "x"})
    both[0]["content"].append({"type": "tool_use", "id": "c2", "name": "read_file", "input": {"path": "y"}})
    model.turns.extend([both, text_turn("ok")])
    await collect(kilo, session.id, "go")
    assert [b["tool_use_id"] for b in model.requests[1][-1]["content"]] == ["c1", "c2"]


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


async def test_refusal_discards_turn(kilo, model, session):
    model.turns.append(text_turn("no", stop_reason="refusal"))
    events = await collect(kilo, session.id, "hi")
    assert "declined" in events[-1]["data"]["error"]
    assert [m.role for m in await kilo.db.get_messages(session.id)] == [MessageRole.USER]


async def test_max_turns_reached(kilo, model, session):
    model.turns.extend([tool_turn("c1", "glob", {"pattern": "*"}), tool_turn("c2", "glob", {"pattern": "*"})])
    events = await collect(kilo, session.id, "go", max_turns=2)
    assert "maximum allowed turns" in events[-1]["data"]["error"]


async def test_cut_off_tool_call_not_executed(kilo, model, session, workspace):
    args = {"path": "a.txt", "content": "half"}
    model.turns.extend([tool_turn("c1", "write_file", args, stop_reason="max_tokens"), text_turn("retry")])
    events = await collect(kilo, session.id, "go")
    assert "Not executed" in events[2]["data"]["result"]["message"]
    assert not (workspace / "a.txt").exists()


async def test_reply_cut_off_without_tools_is_an_error(kilo, model, session):
    model.turns.append(text_turn("partial", stop_reason="max_tokens"))
    assert "stopped early" in (await collect(kilo, session.id, "hi"))[-1]["data"]["error"]


async def test_first_message_titles_session(kilo, model, session):
    model.turns.append(text_turn("ok"))
    await collect(kilo, session.id, "Fix the build\nand more")
    assert (await kilo.db.get_session(session.id)).title == "Fix the build"


@pytest.fixture
async def big_session(kilo, session, monkeypatch):
    """A session past the compaction threshold."""
    monkeypatch.setattr(settings, "kilo_compact_at_tokens", 10_000)
    await kilo.db.add_message(session.id, MessageRole.USER, content="old question")
    await kilo.db.add_message(session.id, MessageRole.ASSISTANT, blocks=[{"type": "text", "text": "old answer"}])
    await kilo.db.update_session(session.id, metadata={"context_tokens": 20_000})
    return session


async def test_auto_compaction_replaces_history(kilo, model, big_session):
    model.turns.append(text_turn("ok"))
    events = await collect(kilo, big_session.id, "new question")
    assert names(events)[:2] == ["compaction_start", "compaction_done"]
    sent = model.requests[0]
    assert sent[0] == {"role": "assistant", "content": model.compact_result["content"]}
    assert sent[1] == {"role": "user", "content": "new question"}


async def test_failed_compaction_keeps_history_and_sets_retry(kilo, model, big_session):
    model.compact_result = RuntimeError("no summary")
    model.turns.append(text_turn("ok"))
    events = await collect(kilo, big_session.id, "new question")
    assert "compaction_failed" in names(events) and names(events)[-1] == "done"
    assert model.requests[0][0]["content"] == "old question"
    meta = (await kilo.db.get_session(big_session.id, include_messages=False)).metadata
    assert meta["compact_retry_at"] >= 20_000 + COMPACTION_RETRY_TOKENS


async def test_summary_cut_off_is_retried_once_with_more_room(kilo, model, big_session):
    model.compact_result = {"stop_reason": "max_tokens", "content": []}
    model.turns.append(text_turn("ok"))
    events = await collect(kilo, big_session.id, "new question")
    assert model.compactions == 2 and "compaction_failed" in names(events)


async def test_compaction_mid_loop_continues_the_task(kilo, model, session, monkeypatch):
    monkeypatch.setattr(settings, "kilo_compact_at_tokens", 10_000)
    call = tool_turn("c1", "glob", {"pattern": "*"})
    call[-1]["context_tokens"] = 20_000
    model.turns.extend([call, text_turn("finished")])
    events = await collect(kilo, session.id, "go")
    assert "compaction_done" in names(events) and names(events)[-1] == "done"
    assert model.requests[1][-1]["role"] == "user"


async def test_manual_compaction(kilo, model, session):
    model.turns.append(text_turn("hello"))
    await collect(kilo, session.id, "hi")
    assert (await kilo.runner.compact(session.id))["event"] == "compaction_done"
    assert (await kilo.db.get_session(session.id, include_messages=False)).metadata["compactions"] == 1


async def test_manual_compact_on_empty_session(kilo, session):
    outcome = await kilo.runner.compact(session.id)
    assert outcome == {"event": "compaction_failed", "data": {"error": "Nothing to compact yet."}}


async def test_history_replays_what_the_stream_sent(kilo, model, session):
    model.turns.extend([tool_turn("c1", "glob", {"pattern": "*"}), text_turn("done")])
    live = await collect(kilo, session.id, "go")
    replayed = history_events(await kilo.db.get_messages(session.id), kilo.toolbox.canonical_name)
    assert names(replayed) == names(live)[:-1]  # `done` is not stored
    assert replayed[1]["data"]["arguments"] == {"pattern": "*"}
    assert replayed[2]["data"]["result"]["status"] == "success"


def test_history_shows_a_non_json_tool_result_as_raw_text():
    message = SimpleNamespace(
        role=MessageRole.TOOL, created_at=None, blocks=[{"type": "tool_result", "tool_use_id": "c", "content": "plain"}],
    )
    assert history_events([message])[0]["data"]["result"] == {"raw": "plain"}


async def test_every_stored_result_is_json_the_model_can_read(kilo, model, session):
    model.turns.extend([tool_turn("c1", "glob", {"pattern": "*"}), text_turn("done")])
    await collect(kilo, session.id, "go")
    tool_message = [m for m in await kilo.db.get_messages(session.id) if m.role == MessageRole.TOOL][0]
    assert json.loads(tool_message.content)["status"] == "success"
