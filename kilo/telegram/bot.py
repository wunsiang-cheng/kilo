"""
The Telegram bot: long-polls the Bot API and talks to Kilo for the users listed in KILO_TELEGRAM_ALLOWED_USERS,
in private chats and in the groups listed in KILO_TELEGRAM_ALLOWED_CHATS (it leaves any other group); everyone
else is ignored, and never reaches the agent. In a group it answers commands, mentions and replies to it, and
uses only the group's own sessions, since everyone there sees what it says. Each chat has a current session
(shared with the web terminal).
A message is a turn there; messages sent while one runs wait for it. A turn shows as one status message that
lists its tool calls as they happen, then the final reply. The turns the system starts in a session the chat
used (scheduled runs) show the same way, and a schedule the agent proposes comes with Approve / Reject buttons.
"""

import asyncio
import hashlib
import logging
import re
import time
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

from kilo.agent.runner import AgentRunner, is_stop
from kilo.models.schedule import Run, Schedule
from kilo.scheduling.announcer import RunAnnouncer
from kilo.scheduling.scheduler import ScheduleError, Scheduler, describe
from kilo.storage.db import Database
from kilo.telegram import render
from kilo.telegram.api import BotAPI, TelegramError, Unauthorized
from kilo.telegram.store import TelegramStore

logger = logging.getLogger(__name__)

POLL_SECONDS = 50  # how long one getUpdates waits for news
RETRY_SECONDS = (1, 2, 5, 15, 30, 60)  # waits between failed polls, the last repeated
EDIT_SECONDS = 1.5  # a status message is edited at most this often (Telegram limits edits per chat)
TYPING_SECONDS = 4.5  # "typing…" lasts 5 seconds; it is sent again this often while a turn runs
LINE_LIMIT = 1000  # longest status line
SESSIONS_SHOWN = 10
NO_PREVIEW = {"is_disabled": True}
GROUP_TYPES = ("group", "supergroup")


def is_group(chat_id: int) -> bool:
    """Telegram gives groups negative ids, users positive ones."""
    return chat_id < 0


def reply_to(message_id: Optional[int]) -> Optional[Dict[str, Any]]:
    return {"message_id": message_id, "allow_sending_without_reply": True} if message_id else None


def person(user: Dict[str, Any]) -> str:
    """A Telegram user for people: their name, and @username when they have one."""
    name = " ".join(filter(None, (user.get("first_name"), user.get("last_name")))) or str(user.get("id"))
    return f"{name} (@{user['username']})" if user.get("username") else name


def proposal_tag(schedule: Schedule) -> str:
    """Names the proposal a button answers, so a button for one the agent has since replaced does nothing."""
    pending = schedule.pending.model_dump_json() if schedule.pending else ""
    return hashlib.sha256(pending.encode()).hexdigest()[:8]


class StatusMessage:
    """A message listing a turn's tool calls as they happen; a full one is left as it is and another begun."""

    def __init__(self, api: BotAPI, chat_id: int, edit_seconds: float = EDIT_SECONDS, reply: Optional[int] = None):
        self.api, self.chat_id, self.edit_seconds, self.reply = api, chat_id, edit_seconds, reply
        self.lines: list[str] = []
        self.message_id: Optional[int] = None
        self.shown = ""  # the text as last sent
        self._sent_at = float("-inf")
        self._later: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    async def add(self, line: str) -> None:
        line = line if len(line) <= LINE_LIMIT else line[: LINE_LIMIT - 1] + "…"
        if self.lines and render.units("\n".join([*self.lines, line])) > render.MESSAGE_LIMIT:
            await self.flush()
            self.lines, self.message_id, self.shown = [], None, ""
        self.lines.append(line)
        wait = self._sent_at + self.edit_seconds - time.monotonic()
        if wait <= 0:
            await self.flush()
        elif self._later is None:
            self._later = asyncio.create_task(self._flush_after(wait))

    async def _flush_after(self, wait: float) -> None:
        await asyncio.sleep(wait)
        self._later = None
        await self.flush()

    async def flush(self) -> None:
        async with self._lock:
            text = "\n".join(self.lines)
            if not text or text == self.shown:
                return
            try:
                if self.message_id is None:
                    sent = await self.api.call(
                        "sendMessage", chat_id=self.chat_id, text=text, link_preview_options=NO_PREVIEW,
                        reply_parameters=reply_to(self.reply),
                    )
                    self.message_id = sent["message_id"]
                else:
                    await self.api.call(
                        "editMessageText", chat_id=self.chat_id, message_id=self.message_id, text=text,
                        link_preview_options=NO_PREVIEW,
                    )
                self.shown = text
            except TelegramError as e:
                logger.warning("Telegram status message not updated: %s", e)
            self._sent_at = time.monotonic()

    async def close(self) -> None:
        if self._later is not None:
            self._later.cancel()
            self._later = None
        await self.flush()


class TurnView:
    """Shows one turn's events in a chat: its tool calls in a status message, then the final reply."""

    def __init__(self, bot: "TelegramBot", chat_id: int, show_errors: bool = True, reply: Optional[int] = None):
        """`reply`: the message the turn answers (in a group), which its first message replies to."""
        self.bot, self.chat_id, self.show_errors, self.reply = bot, chat_id, show_errors, reply
        self.status = StatusMessage(bot.api, chat_id, bot.edit_seconds, reply)

    async def show(self, event: Dict[str, Any]) -> None:
        kind, data = event["event"], event.get("data") or {}
        if kind == "tool_call_start":
            await self.status.add(render.tool_call_line(data.get("name") or "", data.get("arguments") or {}))
        elif kind == "tool_call_result":
            if render.tool_failed(data.get("result") or {}):
                await self.status.add("  ✗ failed")
        elif kind in ("compaction_start", "compaction_done", "compaction_failed"):
            await self.status.add(render.compaction_line(event))
        elif kind == "done":
            await self.status.close()
            if (data.get("content") or "").strip():
                await self.bot.say(self.chat_id, data["content"], reply=None if self.status.message_id else self.reply)
        elif kind == "error":
            await self.status.close()
            if self.show_errors:
                await self.bot.say(
                    self.chat_id, f"✗ {data.get('error') or 'Unknown error.'}", reply=None if self.status.message_id else self.reply,
                )

    async def close(self) -> None:
        await self.status.close()


@dataclass
class Chat:
    """A chat's messages waiting for a turn, and the turn running."""

    queue: "asyncio.Queue[Tuple[str, str, Optional[int]]]" = field(default_factory=asyncio.Queue)  # (session id, text, message id)
    worker: Optional[asyncio.Task] = None
    turn: Optional[asyncio.Task] = None
    turn_session: Optional[str] = None

    @property
    def busy(self) -> bool:
        return self.worker is not None and not self.worker.done()


class TelegramBot:
    def __init__(
        self,
        api: BotAPI,
        store: TelegramStore,
        db: Database,
        runner: AgentRunner,
        scheduler: Scheduler,
        announcer: RunAnnouncer,
        allowed_users: FrozenSet[int],
        allowed_chats: FrozenSet[int] = frozenset(),
        edit_seconds: float = EDIT_SECONDS,
    ):
        self.api, self.store, self.db, self.runner = api, store, db, runner
        self.scheduler, self.announcer = scheduler, announcer
        self.allowed_users, self.allowed_chats = allowed_users, allowed_chats
        self.edit_seconds = edit_seconds
        self.me: Dict[str, Any] = {}  # the bot itself (getMe), to tell what in a group is meant for it
        self._chats: Dict[int, Chat] = {}
        self._runs: Dict[str, TurnView] = {}  # session id -> the view of the system turn running there
        self._asked: Set[Tuple[str, str]] = set()  # (schedule id, proposal tag) of the proposals shown with buttons
        self._tasks: Set[asyncio.Task] = set()

    # ------------------------------------------------------------------
    # Lifetime

    async def start(self) -> None:
        await self.store.init()
        self._spawn(self._poll())
        self._spawn(self._watch_runs())

    async def stop(self) -> None:
        tasks = [*self._tasks, *(t for chat in self._chats.values() for t in (chat.worker, chat.turn) if t)]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        await self.api.close()

    def _spawn(self, coro: Any) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ------------------------------------------------------------------
    # Updates

    async def _poll(self) -> None:
        failures = 0
        while True:
            try:
                self.me = await self.api.call("getMe")
                break
            except Unauthorized:
                logger.error("Telegram rejected KILO_TELEGRAM_BOT_TOKEN; the bot is off until the server restarts.")
                return
            except TelegramError as e:
                logger.warning("Telegram not reached (%s); trying again.", e)
                await asyncio.sleep(RETRY_SECONDS[min(failures, len(RETRY_SECONDS) - 1)])
                failures += 1
        logger.info("Telegram bot @%s is on.", self.me.get("username"))
        try:
            await self.api.call("setMyCommands", commands=render.COMMANDS)
        except TelegramError as e:
            logger.warning("Telegram commands menu not set: %s", e)
        await self._ask_all_approvals()

        offset: Optional[int] = None
        failures = 0
        while True:
            try:
                updates = await self.api.call(
                    "getUpdates", http_timeout=POLL_SECONDS + 10,
                    offset=offset, timeout=POLL_SECONDS, allowed_updates=["message", "callback_query", "my_chat_member"],
                )
                failures = 0
            except Unauthorized:
                logger.error("Telegram rejected KILO_TELEGRAM_BOT_TOKEN; the bot is off until the server restarts.")
                return
            except TelegramError as e:
                logger.warning("Telegram poll failed (%s); trying again.", e)
                await asyncio.sleep(RETRY_SECONDS[min(failures, len(RETRY_SECONDS) - 1)])
                failures += 1
                continue
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    await self.handle(update)
                except Exception:
                    logger.exception("Telegram update %s not handled", update.get("update_id"))

    async def handle(self, update: Dict[str, Any]) -> None:
        """One update: a message, a press of one of the bot's buttons, or the bot added to a chat."""
        if "callback_query" in update:
            await self._button(update["callback_query"])
            return
        if "my_chat_member" in update:
            await self._joined(update["my_chat_member"])
            return
        message = update.get("message")
        if not message:
            return
        sender, chat = message.get("from") or {}, message.get("chat") or {}
        chat_id = chat.get("id")
        if chat.get("type") in GROUP_TYPES:
            if chat_id not in self.allowed_chats:
                await self._leave(chat)
                return
            if not self._meant_for_me(message):
                return
        elif chat.get("type") != "private":
            return
        if sender.get("id") not in self.allowed_users:
            logger.info("Telegram message from user %s ignored: not in KILO_TELEGRAM_ALLOWED_USERS.", sender.get("id"))
            return
        text = message.get("text")
        if text is None:
            await self.say(chat_id, "Only text messages are supported for now.", reply=self._reply(message))
        elif text.startswith("/"):
            command, _, _ = text[1:].partition(" ")
            name, _, target = command.partition("@")
            if not target or target.lower() == (self.me.get("username") or "").lower():
                await self._command(chat_id, name.lower())
        else:
            if is_group(chat_id):
                text = self._unmention(text)
                text = f"{text}\n\n[Sent by {person(sender)} in the Telegram group \"{chat.get('title') or chat_id}\"]"
            if text.strip():
                await self._enqueue(chat_id, text, self._reply(message))

    def _meant_for_me(self, message: Dict[str, Any]) -> bool:
        """In a group: a command, a mention of the bot, or a reply to one of its messages."""
        text = message.get("text") or message.get("caption") or ""
        replied = ((message.get("reply_to_message") or {}).get("from") or {}).get("id")
        username = self.me.get("username")
        return (
            text.startswith("/")
            or (replied is not None and replied == self.me.get("id"))
            or (bool(username) and f"@{username}".lower() in text.lower())
        )

    def _unmention(self, text: str) -> str:
        username = self.me.get("username")
        return re.sub(rf"@{re.escape(username)}\b", "", text, flags=re.IGNORECASE).strip() if username else text

    @staticmethod
    def _reply(message: Dict[str, Any]) -> Optional[int]:
        """The message to answer by replying to it: in a group, the one that asked."""
        return message.get("message_id") if is_group((message.get("chat") or {}).get("id") or 0) else None

    async def _joined(self, change: Dict[str, Any]) -> None:
        """The bot's membership of a chat changed: it leaves a group it was added to that is not listed."""
        chat = change.get("chat") or {}
        status = (change.get("new_chat_member") or {}).get("status")
        if chat.get("type") in GROUP_TYPES and status in ("member", "administrator") and chat.get("id") not in self.allowed_chats:
            await self._leave(chat)

    async def _leave(self, chat: Dict[str, Any]) -> None:
        logger.info(
            "Leaving Telegram group %s (\"%s\"): not in KILO_TELEGRAM_ALLOWED_CHATS.", chat.get("id"), chat.get("title") or "",
        )
        try:
            await self.api.call("leaveChat", chat_id=chat.get("id"))
        except TelegramError as e:
            logger.warning("Telegram group not left: %s", e)

    # ------------------------------------------------------------------
    # Commands

    async def _command(self, chat_id: int, command: str) -> None:
        if command in ("start", "help"):
            await self.say(chat_id, render.HELP)
        elif command == "new":
            await self.store.use(chat_id, await self._new_session(chat_id))
            await self.say(chat_id, "New session started.")
        elif command == "sessions":
            await self._sessions(chat_id)
        elif command == "stop":
            await self._stop(chat_id)
        elif command == "compact":
            self._spawn(self._compact(chat_id))
        else:
            await self.say(chat_id, "Unknown command. /help lists them.")

    async def _sessions(self, chat_id: int) -> None:
        sessions = await self._usable_sessions(chat_id)
        current = await self.store.current(chat_id)
        buttons = [
            [{"text": f"{'▶ ' if s.id == current else ''}{s.title[:60]}", "callback_data": f"s:{s.id}"}] for s in sessions
        ]
        await self.api.call(
            "sendMessage", chat_id=chat_id, text=render.sessions_list(len(sessions)),
            reply_markup={"inline_keyboard": buttons} if buttons else None,
        )

    async def _usable_sessions(self, chat_id: int) -> List[Any]:
        """The sessions a chat may switch to, most recent first: in a group, only its own."""
        if not is_group(chat_id):
            return await self.db.list_sessions(limit=SESSIONS_SHOWN)
        sessions = [await self.db.get_session(sid, include_messages=False) for sid in await self.store.owned(chat_id)]
        return sorted(filter(None, sessions), key=lambda s: s.updated_at, reverse=True)[:SESSIONS_SHOWN]

    async def _new_session(self, chat_id: int) -> str:
        """A new session for the chat; a group's belongs to it."""
        session = await self.db.create_session()
        if is_group(chat_id):
            await self.store.own(session.id, chat_id)
        return session.id

    async def _may_use(self, chat_id: int, session_id: str) -> bool:
        """Whether the chat may use the session: a group, only its own."""
        return not is_group(chat_id) or await self.store.owner(session_id) == chat_id

    async def _stop(self, chat_id: int) -> None:
        chat = self._chats.get(chat_id)
        dropped = 0
        if chat is not None:
            while not chat.queue.empty():
                chat.queue.get_nowait()
                dropped += 1
        session_id = chat.turn_session if chat and chat.turn_session else await self.store.current(chat_id)
        stopped = bool(session_id) and self.runner.stop(session_id)
        if not stopped and not dropped:
            await self.say(chat_id, "Nothing is running.")
        elif dropped:
            await self.say(chat_id, f"Dropped {dropped} waiting message{'s' if dropped != 1 else ''}.")

    async def _compact(self, chat_id: int) -> None:
        session_id = await self.store.current(chat_id)
        if session_id is None or await self.db.get_session(session_id, include_messages=False) is None:
            await self.say(chat_id, "Nothing to compact yet.")
            return
        await self.say(chat_id, render.compaction_line(await self.runner.compact(session_id)))

    # ------------------------------------------------------------------
    # Turns

    async def _session(self, chat_id: int) -> str:
        """The chat's current session; a new one when it has none (or it was deleted)."""
        session_id = await self.store.current(chat_id)
        if (
            session_id is None
            or await self.db.get_session(session_id, include_messages=False) is None
            or not await self._may_use(chat_id, session_id)
        ):
            session_id = await self._new_session(chat_id)
        await self.store.use(chat_id, session_id)
        return session_id

    async def _enqueue(self, chat_id: int, text: str, reply: Optional[int] = None) -> None:
        chat = self._chats.setdefault(chat_id, Chat())
        chat.queue.put_nowait((await self._session(chat_id), text, reply))
        if chat.busy:
            await self.say(chat_id, "⏳ Queued: it runs after the reply in progress.", reply=reply)
        else:
            chat.worker = asyncio.create_task(self._work(chat_id, chat))

    async def _work(self, chat_id: int, chat: Chat) -> None:
        while not chat.queue.empty():
            session_id, text, reply = chat.queue.get_nowait()
            chat.turn_session = session_id
            chat.turn = asyncio.create_task(self._turn(chat_id, session_id, text, reply))
            try:
                await chat.turn
            except Exception:
                logger.exception("Telegram turn in session %s failed", session_id)
            finally:
                chat.turn, chat.turn_session = None, None

    async def _turn(self, chat_id: int, session_id: str, text: str, reply: Optional[int] = None) -> None:
        view = TurnView(self, chat_id, reply=reply)
        typing = asyncio.create_task(self._typing(chat_id))
        try:
            async with aclosing(self.runner.run_chat_stream(session_id=session_id, user_message=text)) as events:
                async for event in events:
                    await view.show(event)
        except asyncio.CancelledError as e:
            if not is_stop(e):
                raise
            asyncio.current_task().uncancel()  # stopped by /stop (or the web terminal): the turn ends here
            await view.close()
            await self.say(chat_id, "■ Stopped.")
        finally:
            typing.cancel()
        await self._ask_approvals(chat_id, session_id)

    async def _typing(self, chat_id: int) -> None:
        while True:
            try:
                await self.api.call("sendChatAction", chat_id=chat_id, action="typing")
            except TelegramError:
                pass
            await asyncio.sleep(TYPING_SECONDS)

    # ------------------------------------------------------------------
    # Scheduled runs: the turns the system starts in a session a chat used, and how runs ended

    async def _watch_runs(self) -> None:
        with self.announcer.listen() as notices:
            while True:
                notice = await notices.get()
                try:
                    await self.notice(notice)
                except Exception:
                    logger.exception("Telegram did not show a scheduled run's news")

    async def notice(self, notice: Any) -> None:
        if isinstance(notice, Run):
            view = self._runs.pop(notice.session_id or "", None)
            if view is not None:
                await view.close()
            chat_id = await self.store.chat_of(notice.origin_session_id) if notice.origin_session_id else None
            if chat_id is not None:
                await self.say(chat_id, render.run_notice(notice))
            return
        session_id, event = notice["session_id"], notice["event"]
        if event["event"] == "run_started":
            chat_id = await self.store.chat_of(session_id)
            if chat_id is None:
                return
            await self.say(chat_id, render.run_started((event.get("data") or {}).get("content") or ""))
            self._runs[session_id] = TurnView(self, chat_id, show_errors=False)  # a failed run's notice says why
            return
        view = self._runs.get(session_id)
        if view is None:
            return
        await view.show(event)
        if event["event"] in ("done", "error"):
            del self._runs[session_id]
            await self._ask_approvals(view.chat_id, session_id)

    # ------------------------------------------------------------------
    # Approvals

    async def _ask_all_approvals(self) -> None:
        """At start: the proposals made in sessions a chat used, still awaiting an answer."""
        try:
            for schedule in await self.scheduler.awaiting_approval():
                chat_id = await self.store.chat_of(schedule.origin_session_id) if schedule.origin_session_id else None
                if chat_id is not None:
                    await self._ask(chat_id, schedule)
        except Exception:
            logger.exception("Telegram did not ask for the proposals awaiting approval")

    async def _ask_approvals(self, chat_id: int, session_id: str) -> None:
        """Ask for each proposal made in the session that the chat was not asked about yet."""
        for schedule in await self.scheduler.awaiting_approval(session_id):
            await self._ask(chat_id, schedule)

    async def _ask(self, chat_id: int, schedule: Schedule) -> None:
        tag = proposal_tag(schedule)
        if (schedule.id, tag) in self._asked:
            return
        self._asked.add((schedule.id, tag))
        await self.api.call(
            "sendMessage", chat_id=chat_id, text=render.approval_request(describe(schedule)), link_preview_options=NO_PREVIEW,
            reply_markup={"inline_keyboard": [[
                {"text": "Approve", "callback_data": f"a:{schedule.id}:{tag}"},
                {"text": "Reject", "callback_data": f"r:{schedule.id}:{tag}"},
            ]]},
        )

    async def _button(self, query: Dict[str, Any]) -> None:
        sender = query.get("from") or {}
        user = sender.get("id")
        message = query.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        answer: Optional[str] = None
        if user in self.allowed_users and (chat_id == user or chat_id in self.allowed_chats):
            kind, _, rest = (query.get("data") or "").partition(":")
            if kind == "s":
                outcome = await self._switch(chat_id, rest)
            elif kind in ("a", "r"):
                outcome = await self._answer(kind == "a", *rest.partition(":")[::2])
                if is_group(chat_id):
                    outcome = f"{outcome} ({person(sender)})"
            else:
                outcome = None
            if outcome is not None:
                answer = outcome
                try:
                    await self.api.call(
                        "editMessageText", chat_id=chat_id, message_id=message["message_id"],
                        text=f"{message.get('text') or ''}\n\n{outcome}" if kind != "s" else outcome,
                        link_preview_options=NO_PREVIEW,
                    )
                except TelegramError as e:
                    logger.warning("Telegram message not updated after a button: %s", e)
        try:
            await self.api.call("answerCallbackQuery", callback_query_id=query["id"], text=answer[:200] if answer else None)
        except TelegramError:
            pass

    async def _switch(self, chat_id: int, session_id: str) -> str:
        session = await self.db.get_session(session_id, include_messages=False)
        if session is None or not await self._may_use(chat_id, session_id):
            return "That session no longer exists."
        await self.store.use(chat_id, session.id)
        return f"Switched to session \"{session.title}\"."

    async def _answer(self, approve: bool, schedule_id: str, tag: str) -> str:
        schedule = await self.scheduler.get(schedule_id)
        if schedule is None or schedule.pending is None or proposal_tag(schedule) != tag:
            return "This proposal is no longer awaiting approval."
        try:
            if approve:
                return render.approved(describe(await self.scheduler.approve(schedule_id)))
            await self.scheduler.reject(schedule_id)
            return "✗ Rejected."
        except ScheduleError as e:
            return f"✗ {e}"

    # ------------------------------------------------------------------

    async def say(self, chat_id: int, text: str, reply: Optional[int] = None) -> None:
        """Send text, in as many messages as it takes; the first replies to message `reply`, when given."""
        for index, part in enumerate(render.split(text)):
            try:
                await self.api.call("sendMessage", chat_id=chat_id, text=part, reply_parameters=reply_to(reply) if index == 0 else None)
            except TelegramError as e:
                logger.warning("Telegram message not sent: %s", e)
                return
