"""The Telegram bot, against a stand-in for the Bot API."""

import asyncio
import json
from typing import Any, Dict, List

import httpx
import pytest

from fakes import text_turn, tool_turn, until
from kilo.config import Settings
from kilo.telegram import render
from kilo.telegram.api import BotAPI, TelegramError, Unauthorized
from kilo.telegram.bot import TelegramBot
from kilo.telegram.store import TelegramStore

USER = 42
STRANGER = 7
GROUP = -100
BOT_ID = 999


class FakeAPI:
    """Records each call; getUpdates hands out what `updates` is given, then waits."""

    def __init__(self):
        self.calls: List[tuple] = []
        self.updates: asyncio.Queue = asyncio.Queue()
        self._next_id = 100

    async def call(self, method: str, http_timeout: float = 30.0, **params: Any) -> Any:
        self.calls.append((method, params))
        if method == "getMe":
            return {"username": "kilo_bot"}
        if method == "getUpdates":
            return [await self.updates.get()]
        if method == "sendMessage":
            self._next_id += 1
            return {"message_id": self._next_id}
        return True

    async def close(self) -> None:
        pass

    def sent(self, chat_id: int = USER) -> List[str]:
        """The text of every message sent to the chat, and of every edit, in order."""
        return [p["text"] for m, p in self.calls if m in ("sendMessage", "editMessageText") and p.get("chat_id") == chat_id]

    def messages(self, chat_id: int = USER) -> List[Dict[str, Any]]:
        return [p for m, p in self.calls if m == "sendMessage" and p.get("chat_id") == chat_id]


@pytest.fixture
async def api():
    return FakeAPI()


@pytest.fixture
async def bot(kilo, api, tmp_path):
    built = TelegramBot(
        api, TelegramStore(tmp_path / "kilo.db"), kilo.db, kilo.runner, kilo.scheduler, kilo.run_announcer,
        frozenset({USER}), frozenset({GROUP}), edit_seconds=0,
    )
    await built.store.init()
    built.me = {"id": BOT_ID, "username": "kilo_bot"}
    yield built
    await built.stop()


def message(text=None, user=USER, chat_type="private", chat_id=None, message_id=1, **extra) -> Dict[str, Any]:
    chat = {"id": chat_id if chat_id is not None else user, "type": chat_type}
    if chat_type != "private":
        chat["title"] = "Team"
    body = {"message_id": message_id, "from": {"id": user, "first_name": "Ann", "username": "ann"}, "chat": chat, **extra}
    if text is not None:
        body["text"] = text
    return {"update_id": 1, "message": body}


def press(data: str, text: str = "card", user=USER, chat_id=None) -> Dict[str, Any]:
    chat_id = chat_id if chat_id is not None else user
    return {"update_id": 2, "callback_query": {
        "id": "q1", "from": {"id": user, "first_name": "Ann", "username": "ann"}, "data": data,
        "message": {"message_id": 9, "chat": {"id": chat_id, "type": "private" if chat_id > 0 else "supergroup"}, "text": text},
    }}


def group(text=None, user=USER, **extra) -> Dict[str, Any]:
    return message(text, user=user, chat_type="supergroup", chat_id=GROUP, **extra)


async def idle(bot: TelegramBot) -> None:
    for chat in list(bot._chats.values()):
        if chat.worker is not None:
            await chat.worker


def buttons(params: Dict[str, Any]) -> List[str]:
    return [b["callback_data"] for row in params["reply_markup"]["inline_keyboard"] for b in row]


# ---------------------------------------------------------------------------
# Who may talk to it


async def test_ignores_users_not_listed(bot, api, model):
    await bot.handle(message("hi", user=STRANGER))
    await bot.handle(group("@kilo_bot hi", user=STRANGER))
    await idle(bot)
    assert api.calls == []
    assert model.requests == []


async def test_only_text_is_taken(bot, api):
    await bot.handle(message(photo=[{"file_id": "x"}]))
    assert api.sent() == ["Only text messages are supported for now."]


# ---------------------------------------------------------------------------
# Turns


async def test_a_turn_shows_tool_calls_by_intent_then_the_reply(bot, api, model, workspace):
    model.turns += [
        tool_turn("c1", "read_file", {"intent": "Read the notes", "path": "missing.txt"}),
        text_turn("All done."),
    ]
    await bot.handle(message("read my notes"))
    await idle(bot)
    sent = api.sent()
    assert sent[0] == "● read_file: Read the notes"
    assert sent[1] == "● read_file: Read the notes\n  ✗ failed"  # the status message, edited
    assert sent[-1] == "All done."
    assert not any("missing.txt" in text for text in sent)
    assert ("sendChatAction", {"chat_id": USER, "action": "typing"}) in api.calls
    session = await kilo_session(bot)
    assert [m.content for m in session.messages if m.role.value == "user"][0] == "read my notes"


async def kilo_session(bot):
    return await bot.db.get_session(await bot.store.current(USER))


async def test_an_error_is_shown(bot, api, model):
    model.turns.append([RuntimeError("boom")])
    await bot.handle(message("hi"))
    await idle(bot)
    assert api.sent()[-1].startswith("✗ ")


async def test_a_message_sent_during_a_turn_waits_for_it(bot, api, model):
    release = asyncio.Event()
    model.turns += [[release.wait, *text_turn("first")], text_turn("second")]
    await bot.handle(message("one"))
    await until(lambda: model.requests)
    await bot.handle(message("two"))
    assert api.sent()[-1] == "⏳ Queued: it runs after the reply in progress."
    release.set()
    await idle(bot)
    replies = [t for t in api.sent() if t in ("first", "second")]
    assert replies == ["first", "second"]


async def test_stop_ends_the_turn_and_drops_waiting_messages(bot, api, model):
    model.turns.append([asyncio.Event().wait])  # never ends by itself
    await bot.handle(message("one"))
    await until(lambda: model.requests)
    await bot.handle(message("two"))
    await bot.handle(message("/stop"))
    await idle(bot)
    assert "Dropped 1 waiting message." in api.sent()
    assert api.sent()[-1] == "■ Stopped."
    assert len(model.requests) == 1


async def test_stop_with_nothing_running(bot, api):
    await bot.handle(message("/stop"))
    assert api.sent() == ["Nothing is running."]


async def test_a_long_reply_is_split(bot, api, model):
    model.turns.append(text_turn("x" * 5000))
    await bot.handle(message("hi"))
    await idle(bot)
    assert [len(t) for t in api.sent()] == [4096, 904]


# ---------------------------------------------------------------------------
# Commands and sessions


async def test_new_and_switching_sessions(bot, api, model):
    model.turns += [text_turn("a"), text_turn("b")]
    await bot.handle(message("first"))
    await idle(bot)
    first = await bot.store.current(USER)
    await bot.handle(message("/new@kilo_bot"))
    second = await bot.store.current(USER)
    assert second != first and api.sent()[-1] == "New session started."

    await bot.handle(message("/sessions"))
    listing = api.messages()[-1]
    assert f"s:{first}" in buttons(listing) and f"s:{second}" in buttons(listing)
    await bot.handle(press(f"s:{first}"))
    assert await bot.store.current(USER) == first
    assert api.sent()[-1].startswith("Switched to session")

    await bot.handle(message("again"))
    await idle(bot)
    session = await bot.db.get_session(first)
    assert [m.content for m in session.messages if m.role.value == "user"] == ["first", "again"]


async def test_help_and_unknown_commands(bot, api):
    await bot.handle(message("/start"))
    await bot.handle(message("/nope"))
    assert api.sent() == [render.HELP, "Unknown command. /help lists them."]


async def test_compact_without_a_session(bot, api):
    await bot.handle(message("/compact"))
    await asyncio.gather(*bot._tasks)
    assert api.sent() == ["Nothing to compact yet."]


# ---------------------------------------------------------------------------
# Approvals


def propose(call_id="c1", **fields):
    args = {"intent": "Remind me", "action": "create", "name": "tea", "instructions": "Say tea time", "run_in_minutes": 15, **fields}
    return tool_turn(call_id, "schedule", args)


async def test_a_proposal_is_asked_with_buttons_and_approved(bot, api, model, kilo):
    model.turns += [propose(), text_turn("Proposed.")]
    await bot.handle(message("remind me of tea"))
    await idle(bot)
    card = api.messages()[-1]
    [schedule] = await kilo.scheduler.awaiting_approval()
    assert card["text"].startswith(f"◆ Kilo asks to create a schedule ({schedule.id}):")
    approve, reject = buttons(card)
    assert approve.startswith(f"a:{schedule.id}:") and reject.startswith(f"r:{schedule.id}:")

    await bot.handle(press(approve, text=card["text"]))
    stored = await kilo.scheduler.get(schedule.id)
    assert stored.approved and stored.pending is None
    assert api.sent()[-1].startswith(card["text"] + "\n\n✓ Approved. Next run:")
    assert api.calls[-1][0] == "answerCallbackQuery"

    await bot.handle(press(approve))  # again: nothing awaits approval now
    assert api.sent()[-1].endswith("This proposal is no longer awaiting approval.")


async def test_a_proposal_is_asked_once(bot, api, model, kilo):
    model.turns += [propose(), text_turn("Proposed."), text_turn("Hi.")]
    await bot.handle(message("remind me"))
    await bot.handle(message("hello"))
    await idle(bot)
    cards = [m for m in api.messages() if "reply_markup" in m]
    assert len(cards) == 1


async def test_rejecting_deletes_a_new_schedule(bot, api, model, kilo):
    model.turns += [propose(), text_turn("Proposed.")]
    await bot.handle(message("remind me"))
    await idle(bot)
    _, reject = buttons(api.messages()[-1])
    await bot.handle(press(reject))
    assert await kilo.scheduler.list() == []
    assert api.sent()[-1].endswith("✗ Rejected.")


async def test_a_button_for_a_replaced_proposal_does_nothing(bot, api, model, kilo):
    model.turns += [propose(), text_turn("Proposed.")]
    await bot.handle(message("remind me"))
    await idle(bot)
    approve, _ = buttons(api.messages()[-1])
    [schedule] = await kilo.scheduler.awaiting_approval()
    await bot.handle(press(approve[:-1] + ("0" if approve[-1] != "0" else "1")))
    assert not (await kilo.scheduler.get(schedule.id)).approved


async def test_buttons_from_others_are_ignored(bot, api, model, kilo):
    model.turns += [propose(), text_turn("Proposed.")]
    await bot.handle(message("remind me"))
    await idle(bot)
    approve, _ = buttons(api.messages()[-1])
    await bot.handle(press(approve, user=STRANGER))
    [schedule] = await kilo.scheduler.awaiting_approval()
    assert not schedule.approved


# ---------------------------------------------------------------------------
# Scheduled runs


async def test_a_scheduled_run_shows_in_the_chat_that_used_its_session(bot, api, model, kilo, clock):
    model.turns += [propose(), text_turn("Proposed.")]
    await bot.handle(message("remind me"))
    await idle(bot)
    approve, _ = buttons(api.messages()[-1])
    await bot.handle(press(approve))

    watching = bot._spawn(bot._watch_runs())
    await asyncio.sleep(0)
    model.turns += [tool_turn("c2", "glob", {"intent": "Look around", "pattern": "*"}), text_turn("Tea time!")]
    clock.advance(minutes=15)
    [run] = await kilo.scheduler.tick()
    await kilo.launcher.wait_idle()
    await until(lambda: "Tea time!" in api.sent())
    sent = api.sent()
    started = next(t for t in sent if t.startswith(f"◆ Scheduled run {run.id} of "))
    assert started == f'◆ Scheduled run {run.id} of "{run.schedule_name}" ({run.schedule_id})'
    assert "● glob: Look around" in sent
    assert sent[-1] == "Tea time!"
    watching.cancel()


async def test_a_skipped_run_is_reported(bot, api, kilo):
    session = await kilo.db.create_session()
    await bot.store.use(USER, session.id)
    from kilo.models.schedule import Run, RunStatus
    from datetime import datetime, timezone
    now = datetime(2026, 10, 28, 9, 0, tzinfo=timezone.utc)
    run = Run(
        id="run_1", schedule_id="sched_1", schedule_name="tea", status=RunStatus.SKIPPED, scheduled_for=now,
        started_at=now, finished_at=now, summary="The previous run is still going.", origin_session_id=session.id,
    )
    await bot.notice(run)
    assert api.sent() == ['◆ Scheduled run of "tea" was skipped at 17:00 (run_1)\n  The previous run is still going.']


async def test_runs_of_sessions_no_chat_used_are_not_shown(bot, api, kilo):
    session = await kilo.db.create_session()
    await bot.notice({"session_id": session.id, "event": {"event": "run_started", "data": {"content": "x"}}})
    await bot.notice({"session_id": session.id, "event": {"event": "done", "data": {"content": "y"}}})
    assert api.calls == []


# ---------------------------------------------------------------------------
# Polling


async def test_polls_updates_and_sets_the_command_menu(bot, api, model):
    model.turns.append(text_turn("pong"))
    await bot.start()
    api.updates.put_nowait(message("ping"))
    await until(lambda: "pong" in api.sent())
    assert ("setMyCommands", {"commands": render.COMMANDS}) in api.calls


# ---------------------------------------------------------------------------
# The Bot API client


def mock_api(handler) -> BotAPI:
    return BotAPI("123:SECRET", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_api_returns_the_result_and_drops_none_params():
    seen = []

    def handler(request):
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 5}})

    api = mock_api(handler)
    assert await api.call("sendMessage", chat_id=1, text="hi", reply_markup=None) == {"message_id": 5}
    assert seen == [("/bot123:SECRET/sendMessage", {"chat_id": 1, "text": "hi"})]


async def test_api_errors_never_show_the_token():
    def refused(request):
        return httpx.Response(401, json={"ok": False, "error_code": 401, "description": "Unauthorized"})

    def down(request):
        raise httpx.ConnectError(f"cannot reach {request.url}")

    with pytest.raises(Unauthorized) as refused_error:
        await mock_api(refused).call("getMe")
    with pytest.raises(TelegramError) as down_error:
        await mock_api(down).call("getMe")
    for error in (refused_error.value, down_error.value):
        assert "SECRET" not in str(error) and error.__cause__ is None
    assert str(down_error.value) == "getMe: ConnectError"


async def test_api_waits_out_rate_limits():
    replies = [
        httpx.Response(429, json={"ok": False, "error_code": 429, "description": "Too Many Requests", "parameters": {"retry_after": 0}}),
        httpx.Response(200, json={"ok": True, "result": True}),
    ]
    assert await mock_api(lambda request: replies.pop(0)).call("editMessageText") is True


# ---------------------------------------------------------------------------
# Text and settings


def test_split_counts_utf16_and_breaks_at_lines():
    assert render.split("short") == ["short"]
    emoji = "😀" * 3000  # 6000 units
    parts = render.split(emoji)
    assert "".join(parts) == emoji and all(render.units(p) <= render.MESSAGE_LIMIT for p in parts)
    lines = ("a" * 100 + "\n") * 60
    first, second = render.split(lines)
    assert first.endswith("\n") and first + second == lines


def test_settings_need_users_with_a_token():
    with pytest.raises(ValueError):
        Settings(_env_file=None, kilo_telegram_bot_token="123:abc")
    with pytest.raises(ValueError):
        Settings(_env_file=None, kilo_telegram_bot_token="123:abc", kilo_telegram_allowed_users="@me")
    allowed = Settings(_env_file=None, kilo_telegram_bot_token="123:abc", kilo_telegram_allowed_users=" 1, 22 ,")
    assert allowed.telegram_allowed_users == frozenset({1, 22})
    assert Settings(_env_file=None, kilo_telegram_bot_token=" ").kilo_telegram_bot_token is None


def test_no_bot_without_a_token(kilo):
    assert kilo.telegram is None


# ---------------------------------------------------------------------------
# Groups


async def test_leaves_groups_not_listed(bot, api, model):
    await bot.handle(message("@kilo_bot hi", chat_type="group", chat_id=-555))
    await bot.handle({"update_id": 3, "my_chat_member": {
        "chat": {"id": -556, "type": "supergroup", "title": "Other"}, "new_chat_member": {"status": "member"},
    }})
    await bot.handle({"update_id": 4, "my_chat_member": {
        "chat": {"id": GROUP, "type": "supergroup", "title": "Team"}, "new_chat_member": {"status": "member"},
    }})
    assert [c for c in api.calls if c[0] == "leaveChat"] == [("leaveChat", {"chat_id": -555}), ("leaveChat", {"chat_id": -556})]
    assert model.requests == []


async def test_a_group_answers_only_what_is_meant_for_it(bot, api, model):
    model.turns += [text_turn("one"), text_turn("two")]
    await bot.handle(group("just chatting"))
    await bot.handle(group("@kilo_bot what time is it?", message_id=5))
    await idle(bot)
    await bot.handle(group("and now?", message_id=6, reply_to_message={"message_id": 3, "from": {"id": BOT_ID}}))
    await idle(bot)
    assert len(model.requests) == 2
    session = await bot.db.get_session(await bot.store.current(GROUP))
    first, second = [m.content for m in session.messages if m.role.value == "user"]
    assert first == 'what time is it?\n\n[Sent by Ann (@ann) in the Telegram group "Team"]'
    assert second.startswith("and now?")
    assert session.title == "what time is it?"
    one, two = [p for p in api.messages(GROUP) if p["text"] in ("one", "two")]
    assert one["reply_parameters"]["message_id"] == 5 and two["reply_parameters"]["message_id"] == 6


async def test_commands_for_another_bot_are_ignored(bot, api):
    await bot.handle(group("/help@other_bot"))
    await bot.handle(group("/help@kilo_bot"))
    assert api.sent(GROUP) == [render.HELP]


async def test_a_group_uses_only_its_own_sessions(bot, api, model):
    model.turns += [text_turn("private"), text_turn("group")]
    await bot.handle(message("my secret"))
    await idle(bot)
    private = await bot.store.current(USER)
    await bot.handle(group("@kilo_bot hello"))
    await idle(bot)
    mine = await bot.store.current(GROUP)
    assert mine != private

    await bot.handle(group("/sessions"))
    assert buttons(api.messages(GROUP)[-1]) == [f"s:{mine}"]
    await bot.handle(press(f"s:{private}", chat_id=GROUP))
    assert await bot.store.current(GROUP) == mine
    assert api.sent(GROUP)[-1] == "That session no longer exists."

    await bot.handle(message("/sessions"))  # a private chat may use any session, the group's included
    assert f"s:{mine}" in buttons(api.messages(USER)[-1])

    await bot.handle(group("/new"))
    assert await bot.store.owner(await bot.store.current(GROUP)) == GROUP


async def test_anyone_listed_may_approve_in_a_group(bot, api, model, kilo):
    model.turns += [propose(), text_turn("Proposed.")]
    await bot.handle(group("@kilo_bot remind us"))
    await idle(bot)
    card = api.messages(GROUP)[-1]
    approve, _ = buttons(card)
    await bot.handle(press(approve, user=STRANGER, chat_id=GROUP))
    [schedule] = await kilo.scheduler.awaiting_approval()
    await bot.handle(press(approve, text=card["text"], chat_id=GROUP))
    assert (await kilo.scheduler.get(schedule.id)).approved
    assert api.sent(GROUP)[-1].endswith("(Ann (@ann))")


def test_settings_take_group_ids():
    allowed = Settings(_env_file=None, kilo_telegram_allowed_chats="-100123, -5")
    assert allowed.telegram_allowed_chats == frozenset({-100123, -5})
    with pytest.raises(ValueError):
        Settings(_env_file=None, kilo_telegram_allowed_chats="team")
