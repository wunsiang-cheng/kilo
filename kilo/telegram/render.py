"""What the bot says, as plain text (no parse mode: the agent's output is shown as written)."""

from typing import Any, Dict, List, Optional, Sequence
from zoneinfo import ZoneInfo

from kilo.config import settings
from kilo.models.schedule import Run
from kilo.models.session import Attachment
from kilo.uploads import format_size

# Longest message Telegram takes, in UTF-16 code units.
MESSAGE_LIMIT = 4096

HELP = """Talk to Kilo here; each message goes to the current session.
/new: start a new session
/sessions: switch to another session
/stop: stop the reply in progress (and drop messages waiting for it)
/compact: summarize the current session's history now
/help: this help
In a group, mention me or reply to me; it has sessions of its own."""

COMMANDS = [
    {"command": "new", "description": "Start a new session"},
    {"command": "sessions", "description": "Switch to another session"},
    {"command": "stop", "description": "Stop the reply in progress"},
    {"command": "compact", "description": "Summarize the session's history now"},
    {"command": "help", "description": "What the bot does"},
]


def units(text: str) -> int:
    """A text's length as Telegram counts it."""
    return len(text.encode("utf-16-le")) // 2


def split(text: str, limit: int = MESSAGE_LIMIT) -> List[str]:
    """A text as messages Telegram takes, broken at a line end where one is near."""
    parts: List[str] = []
    while units(text) > limit:
        cut = _fit(text, limit)
        newline = text.rfind("\n", 0, cut)
        if newline > cut // 2:
            cut = newline + 1
        parts.append(text[:cut])
        text = text[cut:]
    if text.strip() or not parts:
        parts.append(text)
    return parts


def _fit(text: str, limit: int) -> int:
    """How many characters of `text` fit in `limit` units."""
    used = 0
    for index, char in enumerate(text):
        used += 2 if ord(char) > 0xFFFF else 1
        if used > limit:
            return index
    return len(text)


def tool_label(name: str) -> str:
    """A tool's name for people: an MCP tool's as server/tool."""
    return name[len("mcp__"):].replace("__", "/", 1) if name.startswith("mcp__") else name


def tool_call_line(name: str, args: Dict[str, Any]) -> str:
    """A tool call: the tool and what the agent says the call is for, not its input (as the web terminal shows it)."""
    what = args.get("intent")
    return f"● {tool_label(name)}: {what}" if what else f"● {tool_label(name)}"


def tool_failed(result: Dict[str, Any]) -> bool:
    return result.get("status") in ("error", "failed", "timed_out", "killed")


def tokens(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    return f"{round(count / 1000)}K" if count >= 1000 else str(count)


def compaction_line(event: Dict[str, Any]) -> str:
    data = event.get("data") or {}
    if event["event"] == "compaction_start":
        return f"── compacting context ({tokens(data.get('context_tokens') or 0)} tokens)… ──"
    if event["event"] == "compaction_done":
        return (
            f"── context compacted: {tokens(data.get('context_tokens') or 0)} tokens → "
            f"a {tokens(data.get('summary_tokens') or 0)}-token summary ──"
        )
    return f"── context not compacted: {data.get('error') or 'unknown error'} ──"


def _window(s: Dict[str, Any]) -> str:
    if s.get("active_from") or s.get("active_until"):
        return f"  [{s.get('active_from') or '…'} to {s.get('active_until') or '…'}]"
    return ""


def _indent(text: str) -> str:
    return "\n".join(f"  {line}" for line in text.split("\n"))


def approval_request(s: Dict[str, Any]) -> str:
    """A proposal (scheduler.describe) the user is asked to approve: what would run, and for a change, what runs now."""
    p = s.get("proposal") or {}
    if s.get("change") == "update":
        head = f"◆ Kilo asks to change schedule \"{s['name']}\" ({s['id']}) to:"
    else:
        head = f"◆ Kilo asks to create a schedule ({s['id']}):"
    lines = [
        head,
        f"  {p.get('name', '')}: {p.get('when', '')} {p.get('timezone', '')}{_window(p)}{'  (paused)' if p.get('enabled') is False else ''}",
        _indent(f"Instructions: {p.get('instructions', '')}"),
    ]
    if s.get("change") == "update":
        lines.append(f"  Now: {s['when']} {s['timezone']}{_window(s)}{'' if s.get('enabled') else '  (paused)'}")
    return "\n".join(lines)


def approved(s: Dict[str, Any]) -> str:
    return f"✓ Approved. Next run: {s['next_run']}." if s.get("next_run") else "✓ Approved."


OUTCOMES = {"succeeded": "succeeded", "failed": "failed", "timed_out": "timed out", "skipped": "was skipped"}


def run_notice(run: Run) -> str:
    """How a scheduled run ended (when its turn does not show it itself): the outcome, then why."""
    status = run.status.value
    when = (run.finished_at or run.started_at).astimezone(ZoneInfo(settings.kilo_timezone)).strftime("%H:%M")
    text = f"◆ Scheduled run of \"{run.schedule_name}\" {OUTCOMES.get(status, status)} at {when} ({run.id})"
    return f"{text}\n{_indent(run.summary)}" if run.summary and run.summary.strip() else text


def run_started(content: str) -> str:
    """The message that starts a scheduled run (kilo/scheduling/launcher.py), as which run it is: no time, no job."""
    first = (content or "").partition("\n")[0]
    return f"◆ {first.replace('[System notice, not from the user: s', 'S', 1).rpartition(' · ')[0]}"


def sessions_list(count: int) -> str:
    return "Sessions, most recent first (▶ current):" if count else "No sessions yet."


# ---------------------------------------------------------------------------
# Files

UNSUPPORTED = "Only text and files are supported."


def _sized(label: str, size: Optional[int]) -> str:
    return f"{label} ({format_size(size)})" if size is not None else label


def saved(attachments: Sequence[Attachment]) -> str:
    """Files sent without a caption, saved and kept for the sender's next message."""
    if len(attachments) == 1:
        return f"📎 Saved {_sized(attachments[0].name, attachments[0].size)}. It goes to the agent with your next message."
    total = format_size(sum(attachment.size for attachment in attachments))
    return f"📎 Saved {len(attachments)} files ({total}). They go to the agent with your next message."


def too_big(label: str, size: Optional[int]) -> str:
    """A file past what a bot may fetch from Telegram (size None: Telegram said so, the message did not)."""
    return (
        f"{_sized(label, size)} is too big for a Telegram bot, which can only fetch files up to 20 MB. "
        "Send it from the web terminal instead."
    )


def over_limit(label: str, size: Optional[int], limit_mb: int) -> str:
    return f"{_sized(label, size)} is larger than the upload limit of {limit_mb} MB (KILO_UPLOAD_MAX_MB)."


def not_fetched(label: str, reason: str) -> str:
    return f"Could not fetch {label} from Telegram ({reason}). Send it again."
