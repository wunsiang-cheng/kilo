"""Agent Execution Engine: Manages conversation, reAct loop, and tool execution."""

import asyncio
import json
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

from kilo import workspace
from kilo.config import settings
from kilo.context import current_session_id
from kilo.agent.chat_model import ChatModel
from kilo.models.session import MessageRole
from kilo.storage.db import Database
from kilo.tools import Toolbox

SYSTEM_PROMPT_INTRO = """You are an AI agent, and you need to use your tools to help users perform tasks."""

SYSTEM_PROMPT_GUIDELINES = """Key guidelines:
- Keep responses and actions concise and direct. Do not proactively provide additional information or perform extended tasks unless requested by the user.
- If the user's intent or needs are unclear, ask the user directly. You may also offer suggestions and opinions.
"""

COMPACTION_INSTRUCTIONS = """Summarize this conversation between a user and Kilo, an autonomous AI agent working in a terminal and a fixed workspace. Kilo will continue from your summary alone: everything before it is replaced by it. Keep:
- The user's goals and requests, and the latest request still open.
- Decisions made and why; constraints and preferences the user stated.
- Files and directories created, edited or inspected, with exact paths and the state they were left in.
- Commands that matter later, with their outcomes; errors not yet resolved.
- Schedule, run and task ids, and other identifiers in use.
- What was in progress at the end, and the next steps.
Leave out content that can be read again from files or tools; say where it is instead.
Write the summary as plain text. Do not call any tools."""

# Added after a compaction in the middle of a tool loop, so the request ends on a user turn.
CONTINUE_AFTER_COMPACTION = "[The conversation so far was compacted into the summary above. Continue the task from where it left off.]"
COMPACTION_RETRY_TOKENS = 50_000  # after a failed compaction, grow this much before trying again
CHARS_PER_TOKEN = 4  # rough size of text not yet measured by the API (new messages, tool results)
# Heads the summary, which takes the place of the conversation it replaces.
SUMMARY_HEADER = "[Summary of the conversation so far. It replaces the earlier messages, which you can no longer see.]"
# The fields of a stored assistant turn that go back to the model; its readable reasoning is only shown again.
REQUEST_FIELDS = ("role", "content", "tool_calls", "reasoning_details")


def _is_summary(msg: Any) -> bool:
    """A stored compaction: the summary the model wrote of the conversation before it."""
    return msg.role == MessageRole.SUMMARY


def _compactable(db_messages: List[Any]) -> bool:
    """Whether there is history to summarize: something after the latest summary."""
    return bool(db_messages) and not _is_summary(db_messages[-1])


def _parse_tool_arguments(raw: Any) -> Tuple[Dict[str, Any], Optional[str]]:
    """
    Parse a tool call's arguments into a dict. Missing or blank arguments mean no arguments.
    Returns (args, None) on success, or ({}, reason) when the arguments are unusable.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {}, None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as e:
            return {}, f"not valid JSON ({e.msg} at position {e.pos}); received: {raw[:200]!r}"
    if not isinstance(raw, dict):
        return {}, f"arguments must be a JSON object, received {type(raw).__name__}"
    return raw, None


INTERRUPTED_TOOL_RESULT = "Tool call was interrupted before it returned a result."


def _api_messages(db_messages: List[Any]) -> List[Dict[str, Any]]:
    """
    Rebuild the Chat Completions history from stored messages. Deterministic, so every request replays an
    identical prefix (the provider's prompt cache keeps hitting, and the reasoning sent back stays valid).
    History starts at the latest summary: it replaces everything before it, which stays stored but is never
    sent again. Each tool result is a tool message of its own; a tool call with no stored result (the run was
    interrupted) gets an error result before the next message, so the history stays valid.
    """
    summaries = [i for i, msg in enumerate(db_messages) if _is_summary(msg)]
    if summaries:
        db_messages = db_messages[summaries[-1]:]
    messages: List[Dict[str, Any]] = []
    pending: List[str] = []  # calls of the latest assistant turn still without a result

    def close_pending() -> None:
        messages.extend({"role": "tool", "tool_call_id": call_id, "content": INTERRUPTED_TOOL_RESULT} for call_id in pending)
        pending.clear()

    for msg in db_messages:
        if msg.role == MessageRole.TOOL:
            call_id = (msg.payload or {}).get("tool_call_id")
            if call_id in pending:
                pending.remove(call_id)
            messages.append({"role": "tool", "tool_call_id": call_id, "content": msg.content or ""})
            continue
        close_pending()
        if msg.role == MessageRole.ASSISTANT:
            if msg.payload is None:
                messages.append({"role": "assistant", "content": msg.content})
            else:
                messages.append({field: msg.payload[field] for field in REQUEST_FIELDS if field in msg.payload})
            pending.extend(call["id"] for call in (msg.payload or {}).get("tool_calls") or [])
        elif msg.role == MessageRole.SUMMARY:
            messages.append({"role": "user", "content": f"{SUMMARY_HEADER}\n\n{msg.content or ''}"})
        else:
            messages.append({"role": "user", "content": msg.content or ""})
    close_pending()
    return messages


# One agent loop at a time per session (a chat and a scheduled run must not interleave writes).
_session_locks: Dict[str, asyncio.Lock] = {}
# The task running each session's turn now, whoever started it, so the turn can be stopped (AgentRunner.stop).
_active_turns: Dict[str, asyncio.Task] = {}
# The message a turn is cancelled with when it is stopped (rather than, say, the server stopping).
STOP = "turn stopped"


def is_stop(error: BaseException) -> bool:
    """Whether a CancelledError comes from AgentRunner.stop."""
    return isinstance(error, asyncio.CancelledError) and STOP in error.args


class AgentRunner:
    def __init__(
        self,
        client: ChatModel,
        toolbox: Toolbox,
        db: Database,
        intro: str = SYSTEM_PROMPT_INTRO,
        guidelines: str = SYSTEM_PROMPT_GUIDELINES,
    ):
        self.client, self.toolbox, self.db = client, toolbox, db
        self.intro, self.guidelines = intro, guidelines  # the system prompt's opening and closing parts

    def _system_prompt(self) -> str:
        return "\n".join([self.intro, workspace.prompt_section(), self.guidelines])

    async def run_chat_stream(
        self,
        session_id: str,
        user_message: str,
        max_turns: int = 100,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Run the agent loop for a session and yield streaming SSE events.
        Events emitted:
          - {"event": "user_message", "data": {...}}
          - {"event": "message_delta", "data": {"delta": str}}
          - {"event": "progress", "data": {"delta": str}}  (progress-update text between tool calls)
          - {"event": "tool_call_start", "data": {"tool_call_id": str, "name": str, "arguments": dict}}
          - {"event": "tool_call_result", "data": {"tool_call_id": str, "result": dict}}
          - {"event": "done", "data": {"content": str}}
          - {"event": "error", "data": {"error": str}}
        Runs on the same session are serialized: a second call waits until the first finishes. The turn in
        progress can be stopped with `stop`.
        """
        lock = _session_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            task = asyncio.current_task()
            if task is not None:
                _active_turns[session_id] = task
            try:
                async for event in self._run_chat_stream(session_id, user_message, max_turns):
                    yield event
            finally:
                if _active_turns.get(session_id) is task:
                    del _active_turns[session_id]

    def stop(self, session_id: str) -> bool:
        """
        Stop the session's turn in progress, whether the user or the system (a schedule) started it.
        Its task is cancelled with STOP; returns whether there was a turn to stop.
        """
        task = _active_turns.get(session_id)
        if task is None or task.done():
            return False
        task.cancel(STOP)
        return True

    async def compact(self, session_id: str) -> Dict[str, Any]:
        """
        Compact a session now, as `/compact` asks; waits for a run in progress on it to finish.
        Returns the outcome as the stream reports it: a compaction_done or compaction_failed event.
        """
        lock = _session_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            if not _compactable(await self._history(session_id)):
                return {"event": "compaction_failed", "data": {"error": "Nothing to compact yet."}}
            await self.toolbox.ensure_started()
            meta = await self._metadata(session_id)
            outcome: Dict[str, Any] = {}
            async for event in self._compact(session_id, meta.get("context_tokens") or 0, manual=True):
                outcome = event
            return outcome

    async def _history(self, session_id: str) -> List[Any]:
        """The stored messages a request is built from: from the latest summary on (all of them before one)."""
        return await self.db.get_messages(session_id, since_id=(await self._metadata(session_id)).get("summary_message_id"))

    async def _metadata(self, session_id: str) -> Dict[str, Any]:
        session = await self.db.get_session(session_id, include_messages=False)
        return session.metadata if session else {}

    @staticmethod
    def _compaction_due(estimate: int, meta: Dict[str, Any]) -> bool:
        threshold = settings.kilo_compact_at_tokens
        return bool(threshold) and estimate >= max(threshold, meta.get("compact_retry_at") or 0)

    async def _compact(self, session_id: str, estimate: int, manual: bool = False) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Replace the session's history with a summary the model writes in a turn of its own: the conversation,
        then the summary instructions. Yields compaction_start, then compaction_done or compaction_failed.
        A failure is not fatal: the conversation goes on with its full history, and compaction waits until
        it has grown another COMPACTION_RETRY_TOKENS.
        """
        yield {"event": "compaction_start", "data": {"context_tokens": estimate, "manual": manual}}
        # The conversation's own system prompt and tools, so the request reuses the prefix the provider has cached.
        # (Tools cannot be turned off with tool_choice "none": some providers refuse it.)
        messages = _api_messages(await self._history(session_id)) + [{"role": "user", "content": COMPACTION_INSTRUCTIONS}]
        try:
            for max_tokens in (settings.kilo_compact_max_tokens, 2 * settings.kilo_compact_max_tokens):
                final: Optional[Dict[str, Any]] = None
                async for event in self.client.stream_turn(
                    system=self._system_prompt(),
                    messages=messages,
                    tools=self.toolbox.definitions(),
                    max_tokens=max_tokens,
                ):
                    if event.get("type") == "done":
                        final = event
                if final is None:
                    raise RuntimeError("the response stream ended without a final message")
                if final.get("finish_reason") != "length":  # a summary cut off is retried once with more room
                    break
        except Exception as e:
            error = f"LLM error: {e}"
        else:
            finish = final.get("finish_reason")
            # The summary is the reply's text; tool calls the model made anyway are not run.
            summary = ((final.get("message") or {}).get("content") or "").strip()
            if finish in ("stop", "tool_calls") and summary:
                summary_message = await self.db.add_message(session_id=session_id, role=MessageRole.SUMMARY, content=summary)
                after = len(summary) // CHARS_PER_TOKEN
                compactions = (await self._metadata(session_id)).get("compactions") or 0
                # Until the next reply measures it, the conversation counts as its summary (system prompt and tools aside).
                await self.db.update_session(session_id, metadata={
                    "context_tokens": after, "compactions": compactions + 1, "compact_retry_at": None,
                    "summary_message_id": summary_message.id,  # later requests read the history from here
                })
                yield {"event": "compaction_done", "data": {"context_tokens": estimate, "summary_tokens": after}}
                return
            error = f"no summary was written (finish reason: {finish})"
        await self.db.update_session(session_id, metadata={"compact_retry_at": estimate + COMPACTION_RETRY_TOKENS})
        yield {"event": "compaction_failed", "data": {"error": error}}

    async def _run_chat_stream(
        self,
        session_id: str,
        user_message: str,
        max_turns: int,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        # Connect configured MCP servers so their tools are available (and a summary sees the same tools)
        await self.toolbox.ensure_started()

        # Compact first if the conversation is too large to take this message as well
        # (a large first message alone is no reason: there is nothing before it to summarize)
        meta = await self._metadata(session_id)
        context = meta.get("context_tokens") or 0  # as measured after the last turn
        incoming = len(user_message)
        if self._compaction_due(context + incoming // CHARS_PER_TOKEN, meta) and _compactable(
            await self._history(session_id)
        ):
            async for event in self._compact(session_id, context + incoming // CHARS_PER_TOKEN):
                yield event
            meta = await self._metadata(session_id)
            context = meta.get("context_tokens") or 0

        # Save user message
        user_msg_obj = await self.db.add_message(
            session_id=session_id,
            role=MessageRole.USER,
            content=user_message,
        )
        yield {
            "event": "user_message",
            "data": {
                "id": user_msg_obj.id,
                "session_id": session_id,
                "content": user_message,
            },
        }

        # Auto-title session if it's currently untitled or generic
        session = await self.db.get_session(session_id, include_messages=False)
        if session and session.title.startswith("Session sess_"):
            new_title = user_message.strip().split("\n")[0][:40]
            if new_title:
                await self.db.update_session(session_id, title=new_title)

        unmeasured = incoming  # characters added since the API last measured the conversation
        turn = 0
        while turn < max_turns:
            turn += 1
            # Mid-loop compaction, once every tool result of the last turn is in
            estimate = context + unmeasured // CHARS_PER_TOKEN
            if turn > 1 and self._compaction_due(estimate, meta):
                compacted = False
                async for event in self._compact(session_id, estimate):
                    compacted = event["event"] == "compaction_done"
                    yield event
                meta = await self._metadata(session_id)
                if compacted:
                    await self.db.add_message(session_id=session_id, role=MessageRole.USER, content=CONTINUE_AFTER_COMPACTION)
                    context, unmeasured = meta.get("context_tokens") or 0, len(CONTINUE_AFTER_COMPACTION)

            llm_messages = _api_messages(await self._history(session_id))
            current_assistant_content = ""
            final: Optional[Dict[str, Any]] = None

            try:
                async for chunk in self.client.stream_turn(
                    system=self._system_prompt(),
                    messages=llm_messages,
                    tools=self.toolbox.definitions(),
                ):
                    chunk_type = chunk.get("type")

                    if chunk_type == "text":
                        delta = chunk.get("delta", "")
                        current_assistant_content += delta
                        yield {"event": "message_delta", "data": {"delta": delta}}

                    elif chunk_type == "progress":
                        yield {"event": "progress", "data": {"delta": chunk.get("delta", "")}}

                    elif chunk_type == "done":
                        final = chunk

            except Exception as e:
                err_str = f"LLM error: {str(e)}"
                yield {"event": "error", "data": {"error": err_str}}
                return

            if final is None:
                yield {"event": "error", "data": {"error": "LLM error: the response stream ended without a final message."}}
                return

            finish_reason = final.get("finish_reason")
            message = final.get("message") or {}
            if final.get("context_tokens"):
                context, unmeasured = final["context_tokens"], 0
                await self.db.update_session(session_id, metadata={"context_tokens": context})

            if finish_reason == "content_filter":
                # The provider's content filter stopped the reply; the partial turn is discarded.
                yield {"event": "error", "data": {"error": "The model declined this request (content filter)."}}
                return

            tool_calls = message.get("tool_calls") or []
            # An assistant message with neither text nor tool calls is refused by some providers when sent back.
            if message.get("content") or tool_calls:
                await self.db.add_message(
                    session_id=session_id,
                    role=MessageRole.ASSISTANT,
                    content=message.get("content") or None,
                    payload=message,
                )

            if tool_calls:
                for tool_call in tool_calls:
                    call_id = tool_call["id"]
                    function = tool_call.get("function") or {}
                    fn_name = self.toolbox.canonical_name(function.get("name") or "unknown")
                    args, args_error = _parse_tool_arguments(function.get("arguments"))

                    yield {
                        "event": "tool_call_start",
                        "data": {
                            "tool_call_id": call_id,
                            "name": fn_name,
                            "arguments": args,
                        },
                    }

                    if finish_reason == "length":
                        # The reply was cut off, so this call's arguments may be incomplete. (Some providers end a
                        # turn of tool calls with "stop", so only a cut-off reply keeps its calls from running.)
                        result_str = json.dumps({
                            "status": "error",
                            "message": f"Not executed: the response stopped ({finish_reason}) before this tool call was complete.",
                        })
                    elif args_error is None:
                        token = current_session_id.set(session_id)
                        try:
                            result_str = await self.toolbox.execute(fn_name, args)
                        finally:
                            current_session_id.reset(token)
                    else:
                        # Not executed: the model gets the error as this call's result and can retry.
                        result_str = json.dumps({
                            "status": "error",
                            "message": f"Invalid arguments for tool '{fn_name}': {args_error}",
                        })

                    # Parse result for structured event emission
                    try:
                        parsed_res = json.loads(result_str)
                    except Exception:
                        parsed_res = {"raw": result_str}

                    unmeasured += len(result_str)
                    await self.db.add_message(
                        session_id=session_id,
                        role=MessageRole.TOOL,
                        content=result_str,
                        payload={"role": "tool", "tool_call_id": call_id, "content": result_str},
                    )

                    yield {
                        "event": "tool_call_result",
                        "data": {
                            "tool_call_id": call_id,
                            "name": fn_name,
                            "result": parsed_res,
                        },
                    }

                # Continue next turn to let the model process tool results
                continue

            if finish_reason != "stop":
                yield {"event": "error", "data": {"error": f"Response stopped early ({finish_reason})."}}
                return

            yield {
                "event": "done",
                "data": {"content": current_assistant_content},
            }
            return

        yield {
            "event": "error",
            "data": {"error": f"Reached maximum allowed turns ({max_turns})."},
        }
