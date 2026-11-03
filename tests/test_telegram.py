"""The Telegram bot, against a stand-in for the Bot API."""

import asyncio
import json
import re
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx
import pytest

from fakes import text_turn, tool_turn, until
from kilo.config import Settings, settings
from kilo.models.session import Attachment
from kilo.telegram import render
from kilo.telegram.api import BotAPI, TelegramError, Unauthorized
from kilo.telegram.bot import TelegramBot
from kilo.telegram.store import TelegramStore

USER = 42
FRIEND = 43
STRANGER = 7
GROUP = -100
BOT_ID = 999


class FakeAPI:
    """Records each call; getUpdates hands out what `updates` is given, then waits."""

    def __init__(self):
        self.calls: List[tuple] = []
        self.updates: asyncio.Queue = asyncio.Queue()
        self._next_id = 100
        self.files: Dict[str, Tuple[str, bytes]] = {}  # file id -> (file path, content), for getFile and download
        self.too_big: Set[str] = set()  # file ids getFile refuses as too big
        self.broken: Set[str] = set()  # file ids whose download fails
        self.gate: Optional[asyncio.Event] = None  # downloads wait for it while it is set and closed

    async def call(self, method: str, http_timeout: float = 30.0, **params: Any) -> Any:
        self.calls.append((method, params))
        if method == "getMe":
            return {"username": "kilo_bot"}
        if method == "getUpdates":
            return [await self.updates.get()]
        if method == "sendMessage":
            self._next_id += 1
            return {"message_id": self._next_id}
        if method == "getFile":
            if params["file_id"] in self.too_big:
                raise TelegramError("getFile: Bad Request: file is too big", 400)
            return {"file_id": params["file_id"], "file_path": self.files[params["file_id"]][0]}
        return True

    def add_file(self, file_id: str, file_path: str, content: bytes) -> None:
        self.files[file_id] = (file_path, content)

    async def download(self, file_path: str):
        if self.gate is not None:
            await self.gate.wait()
        file_id = next(i for i, (path, _) in self.files.items() if path == file_path)
        if file_id in self.broken:
            raise TelegramError("download: ConnectError")
        content = self.files[file_id][1]
        for start in range(0, len(content), 4):
            yield content[start:start + 4]

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
        frozenset({USER, FRIEND}), frozenset({GROUP}), edit_seconds=0,
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


def photo(file_id: str = "p1", size: int = 8) -> List[Dict[str, Any]]:
    return [{"file_id": f"{file_id}-small", "file_size": 2}, {"file_id": file_id, "file_size": size}]


def document(file_id="d1", name="report.pdf", size=6, mime="application/pdf") -> Dict[str, Any]:
    item = {"file_id": file_id, "file_name": name, "mime_type": mime}
    if size is not None:
        item["file_size"] = size
    return item


async def idle(bot: TelegramBot) -> None:
    """Waits until every message sent so far is taken in, and every turn they started has ended."""
    for intake in list(bot._intakes.values()):
        if intake.worker is not None:
            await intake.worker
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
# Files


async def test_a_file_with_a_caption_goes_to_the_agent_with_it(bot, api, model, workspace):
    api.add_file("d1", "documents/file_1.pdf", b"%PDF-1")
    model.turns.append(text_turn("A report."))
    await bot.handle(message(caption="summarize", document=document()))
    await idle(bot)
    [request] = model.requests
    content = request[-1]["content"]
    assert content.startswith("summarize\n\n[Files sent with this message")
    path = re.search(r"- (uploads/\S+/report\.pdf) \(application/pdf, 6 bytes\)", content).group(1)
    assert (workspace / path).read_bytes() == b"%PDF-1"
    assert api.sent()[-1] == "A report."


async def test_a_file_alone_waits_for_the_next_message(bot, api, model):
    api.add_file("p1", "photos/file_2.jpg", b"jpegdata")
    model.turns.append(text_turn("A cat."))
    await bot.handle(message(photo=photo()))
    await idle(bot)
    assert model.requests == []
    assert re.fullmatch(r"📎 Saved photo-\d{6}\.jpg \(8 bytes\)\. It goes to the agent with your next message\.", api.sent()[-1])
    await bot.handle(message("what is this?"))
    await idle(bot)
    [request] = model.requests
    assert request[-1]["content"].startswith("what is this?\n\n[Files sent with this message")
    assert re.search(r"- uploads/\S+/photo-\d{6}\.jpg \(image/jpeg, 8 bytes\)", request[-1]["content"])


async def test_text_sent_while_a_file_downloads_takes_it_along(bot, api, model):
    api.add_file("p1", "photos/file_2.jpg", b"jpegdata")
    api.gate = asyncio.Event()
    model.turns.append(text_turn("ok"))
    await bot.handle(message(photo=photo()))
    await bot.handle(message("here it is"))
    assert model.requests == []
    api.gate.set()
    await idle(bot)
    [request] = model.requests
    assert "photo-" in request[-1]["content"]


async def test_commands_are_answered_while_a_file_downloads(bot, api):
    api.add_file("p1", "photos/file_2.jpg", b"jpegdata")
    api.gate = asyncio.Event()
    await bot.handle(message(photo=photo()))
    await bot.handle(message("/stop"))
    assert api.sent() == ["Nothing is running."]
    api.gate.set()
    await idle(bot)


async def test_commands_leave_held_files_for_the_next_message(bot, api, model):
    api.add_file("p1", "photos/file_2.jpg", b"jpegdata")
    model.turns.append(text_turn("ok"))
    await bot.handle(message(photo=photo()))
    await idle(bot)
    await bot.handle(message("/new"))
    await bot.handle(message("look"))
    await idle(bot)
    assert "photo-" in model.requests[0][-1]["content"]


async def test_held_files_outlast_a_restart(bot, api, model, kilo, tmp_path):
    api.add_file("p1", "photos/file_2.jpg", b"jpegdata")
    await bot.handle(message(photo=photo()))
    await idle(bot)
    restarted = TelegramBot(
        api, TelegramStore(tmp_path / "kilo.db"), kilo.db, kilo.runner, kilo.scheduler, kilo.run_announcer,
        frozenset({USER}), edit_seconds=0,
    )
    await restarted.store.init()
    restarted.me = bot.me
    model.turns.append(text_turn("ok"))
    await restarted.handle(message("look"))
    await idle(restarted)
    assert "photo-" in model.requests[0][-1]["content"]
    await restarted.stop()


async def test_a_file_too_big_for_telegram_is_not_fetched(bot, api):
    await bot.handle(message(document=document("d9", "film.mp4", 21 * 1024 * 1024, "video/mp4")))
    await idle(bot)
    assert api.sent() == [
        "film.mp4 (21.0 MB) is too big for a Telegram bot, which can only fetch files up to 20 MB. "
        "Send it from the web terminal instead."
    ]
    assert not [c for c in api.calls if c[0] == "getFile"]


async def test_a_file_over_the_upload_limit_is_not_fetched(bot, api, monkeypatch):
    monkeypatch.setattr(settings, "kilo_upload_max_mb", 1)
    await bot.handle(message(document=document("d9", "big.zip", 2 * 1024 * 1024, "application/zip")))
    await idle(bot)
    assert api.sent() == ["big.zip (2.0 MB) is larger than the upload limit of 1 MB (KILO_UPLOAD_MAX_MB)."]
    assert not [c for c in api.calls if c[0] == "getFile"]


async def test_a_file_without_a_size_is_checked_as_it_is_saved(bot, api, monkeypatch, workspace):
    monkeypatch.setattr(settings, "kilo_upload_max_mb", 1)
    api.add_file("v1", "videos/file_3.mp4", b"x" * (1024 * 1024 + 1))
    await bot.handle(message(video={"file_id": "v1", "mime_type": "video/mp4"}))
    await idle(bot)
    assert api.sent() == ["The video is larger than the upload limit of 1 MB (KILO_UPLOAD_MAX_MB)."]
    assert [p for p in workspace.rglob("*") if p.is_file()] == []


async def test_telegram_refusing_a_big_file_is_explained(bot, api):
    api.add_file("v1", "videos/file_3.mp4", b"")
    api.too_big.add("v1")
    await bot.handle(message(video={"file_id": "v1"}))
    await idle(bot)
    assert api.sent() == [
        "The video is too big for a Telegram bot, which can only fetch files up to 20 MB. Send it from the web terminal instead."
    ]


async def test_a_failed_download_is_reported_and_nothing_held(bot, api, model):
    api.add_file("d1", "documents/file_1.pdf", b"%PDF-1")
    api.broken.add("d1")
    model.turns.append(text_turn("ok"))
    await bot.handle(message(document=document()))
    await idle(bot)
    assert api.sent() == ["Could not fetch report.pdf from Telegram (download: ConnectError). Send it again."]
    await bot.handle(message("hi"))
    await idle(bot)
    assert model.requests[0][-1]["content"] == "hi"


async def test_stickers_are_not_supported(bot, api):
    await bot.handle(message(sticker={"file_id": "s1"}))
    assert api.sent() == ["Only text and files are supported."]


async def test_group_files_not_meant_for_the_bot_are_not_fetched(bot, api):
    api.add_file("p1", "photos/file_2.jpg", b"jpegdata")
    await bot.handle(group(photo=photo()))
    await idle(bot)
    assert api.calls == []


async def test_a_caption_that_only_mentions_the_bot_is_held(bot, api, model):
    api.add_file("p1", "photos/file_2.jpg", b"jpegdata")
    await bot.handle(group(caption="@kilo_bot", photo=photo()))
    await idle(bot)
    assert model.requests == []
    assert api.sent(GROUP)[-1].startswith("📎 Saved photo-")


async def test_held_files_go_only_with_their_senders_message(bot, api, model):
    api.add_file("p1", "photos/file_2.jpg", b"jpegdata")
    model.turns += [text_turn("hi friend"), text_turn("a photo")]
    await bot.handle(group(caption="@kilo_bot", photo=photo()))
    await idle(bot)
    await bot.handle(group("@kilo_bot hello", user=FRIEND))
    await idle(bot)
    await bot.handle(group("@kilo_bot what is it?"))
    await idle(bot)
    first, second = model.requests
    assert "Files sent" not in first[-1]["content"]
    assert "photo-" in second[-1]["content"] and "[Sent by Ann (@ann)" in second[-1]["content"]


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


HELD_PHOTO = Attachment(path="uploads/2026-11-03/photo-1.jpg", name="photo-1.jpg", type="image/jpeg", size=10)
HELD_DOC = Attachment(path="uploads/2026-11-03/a.pdf", name="a.pdf", type="application/pdf", size=20)


async def test_download_streams_the_file():
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, content=b"photo bytes")

    assert b"".join([chunk async for chunk in mock_api(handler).download("photos/file_1.jpg")]) == b"photo bytes"
    assert seen == ["/file/bot123:SECRET/photos/file_1.jpg"]


async def test_download_errors_never_show_the_token():
    def missing(request):
        return httpx.Response(404)

    def down(request):
        raise httpx.ConnectError(f"cannot reach {request.url}")

    for handler, text in ((missing, "download: HTTP 404"), (down, "download: ConnectError")):
        with pytest.raises(TelegramError) as error:
            async for _ in mock_api(handler).download("photos/file_1.jpg"):
                pass
        assert str(error.value) == text and error.value.__cause__ is None


async def test_held_files_are_taken_once_in_order(tmp_path):
    store = TelegramStore(tmp_path / "kilo.db")
    await store.init()
    await store.hold(USER, USER, [HELD_PHOTO])
    await store.hold(USER, USER, [HELD_DOC])
    await store.hold(GROUP, USER, [HELD_DOC])
    assert await store.take(USER, USER) == [HELD_PHOTO, HELD_DOC]
    assert await store.take(USER, USER) == []
    assert await store.take(GROUP, USER) == [HELD_DOC]


async def test_held_files_outlast_the_store(tmp_path):
    first = TelegramStore(tmp_path / "kilo.db")
    await first.init()
    await first.hold(USER, USER, [HELD_PHOTO])
    again = TelegramStore(tmp_path / "kilo.db")
    await again.init()
    assert await again.take(USER, USER) == [HELD_PHOTO]


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
