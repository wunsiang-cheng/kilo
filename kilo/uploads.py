"""
Files users send, from the web terminal or Telegram: saved in uploads/ in the workspace, a folder per day, and
described to the model by where they are, never by their contents. The agent reads them with its tools.
"""

import json
import mimetypes
import unicodedata
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import AsyncIterable, BinaryIO, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from kilo import workspace
from kilo.clock import utc_now
from kilo.config import settings
from kilo.models.session import Attachment

UPLOADS = "uploads"
MAX_NAME_BYTES = 255  # the most a Linux file system takes for one name
MAX_EXTENSION_CHARS = 16  # a longer ".something" is part of the name, not its extension
MAX_SENT_NAME_CHARS = 255
# Kept in file names besides letters, marks and digits. A name goes into the agent's shell commands, and a forwarded
# file's name may be a stranger's: none of these runs anything there, quoted or not.
NAME_PUNCTUATION = frozenset(" .-_()+,=@")
GENERIC_TYPE = "application/octet-stream"
NOTE_HEADER = (
    "[Files sent with this message, saved in the workspace. Their contents are not included here; "
    "read them with your tools when you need them."
)


class UploadTooLarge(Exception):
    """The file is larger than KILO_UPLOAD_MAX_MB; nothing of it was kept."""


def root() -> Path:
    return settings.kilo_workspace / UPLOADS


def max_bytes() -> int:
    return settings.kilo_upload_max_mb * 1024 * 1024


def _split(name: str) -> Tuple[str, str]:
    """A file name's stem and extension (with its dot); a leading or long ".something" is no extension."""
    dot = name.rfind(".")
    if dot > 0 and len(name) - dot <= MAX_EXTENSION_CHARS:
        return name[:dot], name[dot:]
    return name, ""


def _fit(stem: str, extension: str, suffix: str = "") -> str:
    """stem + suffix + extension, the stem cut by whole characters to fit in MAX_NAME_BYTES of UTF-8."""
    room = MAX_NAME_BYTES - len((suffix + extension).encode())
    while len(stem.encode()) > room:
        stem = stem[:-1]
    return stem + suffix + extension


def safe_name(name: str) -> str:
    """
    The name a file is saved under: the last part of the name sent, in NFC, with every character but letters, marks,
    digits and NAME_PUNCTUATION replaced by "_", without leading spaces, dots or dashes (no hidden file, nothing a
    command reads as an option) or trailing spaces, at most MAX_NAME_BYTES long; "file" when nothing is left.
    """
    base = unicodedata.normalize("NFC", name.replace("\\", "/").rsplit("/", 1)[-1])
    base = "".join(c if unicodedata.category(c)[0] in "LMN" or c in NAME_PUNCTUATION else "_" for c in base)
    base = base.lstrip(" .-").rstrip(" ")
    return _fit(*_split(base or "file"))


def type_of(name: str, declared: Optional[str]) -> str:
    """
    A file's media type: from its name's extension first, since senders can declare it wrongly (Windows declares a
    .csv file as Excel's), then as the sender declared it, else application/octet-stream.
    """
    guessed = mimetypes.guess_type(name)[0]
    if guessed:
        return guessed
    declared = (declared or "").split(";", 1)[0].strip().lower()
    return declared if declared and declared != GENERIC_TYPE else GENERIC_TYPE


def _sent_name(name: Optional[str], path: Path) -> str:
    return name[:MAX_SENT_NAME_CHARS] if name and name.strip() else path.name


def _create(folder: Path, name: str) -> Tuple[Path, BinaryIO]:
    """A new file in `folder` under `name`, or name-2, name-3… when it is taken: never one that already exists."""
    stem, extension = _split(name)
    number = 1
    while True:
        path = folder / (name if number == 1 else _fit(stem, extension, f"-{number}"))
        try:
            return path, open(path, "xb")
        except FileExistsError:
            number += 1


async def save(
    chunks: AsyncIterable[bytes], name: str, declared_type: Optional[str] = None, now: Optional[datetime] = None,
) -> Attachment:
    """
    Save a file as it arrives, in uploads/<day> (the day in KILO_TIMEZONE; `now` is for tests), under its safe name or
    a numbered one. Nothing is kept of a file that does not arrive whole: one over KILO_UPLOAD_MAX_MB (UploadTooLarge),
    a source that breaks off, a disk that fails, an upload cancelled.
    """
    day = (now or utc_now()).astimezone(ZoneInfo(settings.kilo_timezone)).date().isoformat()
    folder = root() / day
    folder.mkdir(parents=True, exist_ok=True)
    path, handle = _create(folder, safe_name(name))
    size, limit = 0, max_bytes()
    try:
        with handle:
            async for chunk in chunks:
                size += len(chunk)
                if size > limit:
                    raise UploadTooLarge()
                handle.write(chunk)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return Attachment(
        path=path.relative_to(settings.kilo_workspace).as_posix(),
        name=_sent_name(name, path),
        type=type_of(path.name, declared_type),
        size=size,
    )


def attachment_at(path: str, name: Optional[str] = None, declared_type: Optional[str] = None) -> Attachment:
    """
    A file in uploads/ that a client names (relative to the workspace, or absolute), with its path normalized and its
    size read from the disk. Anything else, a link out of uploads/ included, is refused with ValueError.
    """
    found = workspace.resolve(path)
    if not found.is_relative_to(root().resolve()) or not found.is_file():
        raise ValueError(f"Attachment '{path}' is not a file in uploads/.")
    return Attachment(
        path=found.relative_to(workspace.root().resolve()).as_posix(),
        name=_sent_name(name, found),
        type=type_of(found.name, declared_type),
        size=found.stat().st_size,
    )


def format_size(size: int) -> str:
    """A size for people: bytes below 1 KB, then KB, MB or GB (of 1024), with one decimal."""
    if size < 1024:
        return f"{size} bytes"
    value, unit = size / 1024, "KB"
    for bigger in ("MB", "GB"):
        if value < 1024:
            break
        value, unit = value / 1024, bigger
    return f"{value:.1f} {unit}"


def note(attachments: Sequence[Attachment]) -> str:
    """What the model is told of the files sent with a message: where each one is, its type and size."""
    lines = [NOTE_HEADER]
    for attachment in attachments:
        details = [attachment.type, format_size(attachment.size)]
        if attachment.name != PurePosixPath(attachment.path).name:
            details.append(f"sent as {json.dumps(attachment.name, ensure_ascii=False)}")
        lines.append(f"- {attachment.path} ({', '.join(details)})")
    return "\n".join(lines) + "]"


def with_note(text: str, attachments: Sequence[Attachment]) -> str:
    """A user message as the model gets it: its text, then the note on the files sent with it (alone without text)."""
    if not attachments:
        return text
    return f"{text}\n\n{note(attachments)}" if text.strip() else note(attachments)
