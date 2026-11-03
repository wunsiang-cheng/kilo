"""File operations tools: read_file, write_file, edit_file."""

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Dict, List

from kilo import documents, images, workspace
from kilo.config import settings
from kilo.tools.base import ShownImage, Tool, ToolResult
from kilo.tools.output_limit import clip_line

MAX_LINE_CHARS = 2000  # longer lines are cut, with a marker giving their length
READ_FILE_ROOM = 2000  # characters of the output limit kept for the result's other fields

HEIC_SUFFIXES = frozenset({".heic", ".heif"})
OLD_OFFICE_SUFFIXES = frozenset({".doc", ".xls", ".ppt"})
HEIC_MESSAGE = "HEIC images are not supported. Ask for a JPEG or PNG instead."
OLD_OFFICE_MESSAGE = (
    "Old Office formats (.doc, .xls, .ppt) are not supported. Ask for .docx, .xlsx or .pptx instead, "
    "or convert the file with LibreOffice (soffice) if it is installed."
)
BINARY_MESSAGE = (
    "File '{path}' appears to be binary ({size} bytes). read_file reads text files, images (PNG, JPEG, WebP, GIF, "
    "BMP, TIFF), PDF, Word (.docx), Excel (.xlsx) and PowerPoint (.pptx) files."
)
PAGES_PDF_ONLY = "pages applies to PDF files only."
SHOWN_IMAGE_MESSAGE = "The image is shown in the message after the tool results."
SHOWN_FRAMES_MESSAGE = "The first of its {frames} frames is shown in the message after the tool results."
SHOWN_PAGES_MESSAGE = "The pages are shown as images in the message after the tool results."


def _is_binary_file(file_path: Path) -> bool:
    """Check if file appears to be binary by sniffing initial bytes."""
    try:
        with open(file_path, "rb") as f:
            chunk = f.read(4096)
            return b"\x00" in chunk
    except Exception:
        return False


READ_FILE_TOOL_DEFINITION = {
    "name": "read_file",
    "description": (
        "Read a file. Text comes as numbered lines: each line with its number and ' | ' in front (e.g. '   12 | text'), "
        "which are not part of the file. Lines longer than 2,000 characters are cut, with a marker, and a long read "
        "stops early with `truncated` and a `message` saying where to continue. Bytes that are not UTF-8 show as "
        "replacement characters. PDF, Word (.docx), Excel (.xlsx) and PowerPoint (.pptx) files are converted to "
        "Markdown text, numbered and read in parts the same way; the result's `text_file` holds the whole text, for "
        "grep. Editing that text does not change the original file. Images (PNG, JPEG, WebP, GIF, BMP, TIFF) are shown "
        "to you in a message after the tool results, as are PDF pages asked for with `pages`. Other binary files are "
        "refused."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "The path of the file to read (relative to the workspace, or absolute).",
            },
            "start_line": {
                "type": "integer",
                "description": "Optional 1-based start line to begin reading from (default: 1).",
                "default": 1,
            },
            "end_line": {
                "type": "integer",
                "description": "Optional 1-based end line to stop reading at (inclusive).",
            },
            "max_lines": {
                "type": "integer",
                "description": "Maximum number of lines to return (default: 2000).",
                "default": 2000,
            },
            "pages": {
                "type": "string",
                "description": (
                    'PDF only: pages to look at as images rather than read as text, e.g. "3", "3-5" or "1,4"; at most 5. '
                    "For scanned pages, and for charts and layouts the text leaves out."
                ),
            },
        },
        "required": ["path"],
    },
}

WRITE_FILE_TOOL_DEFINITION = {
    "name": "write_file",
    "description": (
        "Create a file, replace a whole file, or append to one, as UTF-8. Missing parent directories are created."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "The path of the file to write (relative to the workspace, or absolute).",
            },
            "content": {
                "type": "string",
                "description": "The text content to write into the file.",
            },
            "append": {
                "type": "boolean",
                "description": "If true, append content to the end of the file; otherwise create or overwrite (default: false).",
                "default": False,
            },
            "overwrite": {
                "type": "boolean",
                "description": "If false and append is false, fail if the file already exists (default: true).",
                "default": True,
            },
        },
        "required": ["path", "content"],
    },
}

EDIT_FILE_TOOL_DEFINITION = {
    "name": "edit_file",
    "description": (
        "Change part of an existing UTF-8 file by replacing the text `old_str` with `new_str`; use this rather than "
        "rewriting a whole file. Read the lines first: `old_str` must match the file exactly, whitespace and indentation "
        "included, without read_file's line numbers. In a file with CRLF line ends, `\\n` matches them and they are kept. "
        "An empty `new_str` deletes the text. Fails and leaves the file unchanged when `old_str` is not found, when it "
        "occurs more than once and `replace_all` is false, when it equals `new_str`, or when the file is not UTF-8."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "The path of the file to edit (relative to the workspace, or absolute).",
            },
            "old_str": {
                "type": "string",
                "description": "The exact target string to replace in the file.",
            },
            "new_str": {
                "type": "string",
                "description": "The replacement string.",
            },
            "replace_all": {
                "type": "boolean",
                "description": "Whether to replace all occurrences. If false, fails if target appears multiple times (default: false).",
                "default": False,
            },
        },
        "required": ["path", "old_str", "new_str"],
    },
}


def _error(message: str) -> ToolResult:
    return ToolResult(text=json.dumps({"status": "error", "message": message}))


def _numbered(text: str, args: Dict[str, Any], head: Dict[str, Any]) -> Dict[str, Any]:
    """
    The lines of `text` a read asks for (start_line, end_line, max_lines), numbered, after the fields in `head`.
    The read stops early, at a whole line the next read can follow, before the output limit.
    """
    all_lines = text.splitlines(keepends=False)
    total_lines = len(all_lines)

    start_line = int(args.get("start_line", 1) or 1)
    start_line = max(1, start_line)

    end_line_arg = args.get("end_line")
    if end_line_arg is not None:
        end_line = int(end_line_arg)
    else:
        end_line = total_lines

    max_lines = int(args.get("max_lines", 2000) or 2000)
    max_lines = max(1, max_lines)

    if start_line > total_lines:
        return {
            **head,
            "total_lines": total_lines,
            "start_line": start_line,
            "end_line": end_line,
            "lines_returned": 0,
            "content": "",
            "message": f"start_line ({start_line}) is greater than total lines ({total_lines}).",
        }

    # Slice 0-based
    start_idx = start_line - 1
    end_idx = min(end_line, total_lines)
    if end_idx < start_idx:
        end_idx = start_idx

    sliced = all_lines[start_idx:end_idx]
    truncated = len(sliced) > max_lines
    sliced = sliced[:max_lines]

    # Stop before the output limit, so the result ends on a whole line the next read can follow.
    numbered: List[str] = []
    size, budget = 0, settings.kilo_tool_output_max_chars - READ_FILE_ROOM
    for line_no, line in enumerate(sliced, start=start_line):
        entry = f"{line_no:5d} | {clip_line(line, MAX_LINE_CHARS)}"
        if numbered and size + len(entry) + 1 > budget:
            truncated = True
            break
        numbered.append(entry)
        size += len(entry) + 1

    actual_end_line = start_idx + len(numbered)
    result: Dict[str, Any] = {
        **head,
        "total_lines": total_lines,
        "start_line": start_line,
        "end_line": actual_end_line,
        "lines_returned": len(numbered),
        "truncated": truncated,
        "content": "\n".join(numbered),
    }
    if truncated:
        result["message"] = f"Not the whole range: continue with start_line={actual_end_line + 1}."
    return result


def _read_image(file_path: Path, path_raw: str) -> ToolResult:
    """An image, shown to the model after the tool results, and what it is."""
    try:
        info = images.read(file_path)
    except Exception as e:
        return _error(f"Could not read '{path_raw}' as an image: {e}")
    shown_as = workspace.display(file_path)
    result: Dict[str, Any] = {
        "status": "success", "path": shown_as, "type": info.type, "width": info.width, "height": info.height,
        "shown_width": info.shown.width, "shown_height": info.shown.height, "message": SHOWN_IMAGE_MESSAGE,
    }
    if info.frames > 1:
        result["frames"] = info.frames
        result["message"] = SHOWN_FRAMES_MESSAGE.format(frames=info.frames)
    return ToolResult(text=json.dumps(result), images=(ShownImage(info.shown.file, shown_as),))


def _read_document(file_path: Path, path_raw: str, kind: documents.Kind, args: Dict[str, Any], pages: str) -> ToolResult:
    """A document's converted text, read like a text file's; or the PDF pages `pages` names, shown as images."""
    shown_as = workspace.display(file_path)
    try:
        if pages:
            total, shown = documents.show_pages(file_path, pages)
            result = {
                "status": "success", "path": shown_as, "total_pages": total, "pages": [number for number, _ in shown],
                "message": SHOWN_PAGES_MESSAGE,
            }
            return ToolResult(
                text=json.dumps(result),
                images=tuple(ShownImage(stored.file, f"{shown_as}, page {number}") for number, stored in shown),
            )
        converted = documents.convert(file_path)
        text = converted.read_text(encoding="utf-8")
    except documents.DocumentError as e:
        return _error(f"'{path_raw}' {e}")
    except documents.PagesError as e:
        return _error(str(e))
    except Exception as e:
        return _error(f"Could not read '{path_raw}' as {kind.described}: {e}")
    head = {"status": "success", "path": shown_as, "converted_from": kind.name, "text_file": workspace.display(converted)}
    return ToolResult(text=json.dumps(_numbered(text, args, head)))


def _read_file(args: Dict[str, Any]) -> ToolResult:
    """Execute read_file: a text file's lines, a document's text, or an image (PDF pages) shown to the model."""
    path_raw = args.get("path")
    if not path_raw:
        return _error("Missing required parameter 'path'.")

    try:
        file_path = workspace.resolve(str(path_raw))
        if not file_path.exists():
            return _error(f"File not found: '{path_raw}' (resolved: '{file_path}').")
        if not file_path.is_file():
            return _error(f"Path is not a regular file: '{path_raw}'.")

        suffix = file_path.suffix.lower()
        kind = documents.kind_of(file_path)
        pages = "" if args.get("pages") is None else str(args["pages"]).strip()  # a model may send a number
        if pages and (kind is None or kind.name != "pdf"):
            return _error(PAGES_PDF_ONLY)
        if suffix in images.SUFFIXES:
            return _read_image(file_path, str(path_raw))
        if suffix in HEIC_SUFFIXES:
            return _error(HEIC_MESSAGE)
        if suffix in OLD_OFFICE_SUFFIXES:
            return _error(OLD_OFFICE_MESSAGE)
        if kind is not None:
            return _read_document(file_path, str(path_raw), kind, args, pages)

        if _is_binary_file(file_path):
            return _error(BINARY_MESSAGE.format(path=path_raw, size=file_path.stat().st_size))

        # Read file with UTF-8 encoding
        try:
            content = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        head = {"status": "success", "path": workspace.display(file_path)}
        return ToolResult(text=json.dumps(_numbered(content, args, head)))

    except Exception as e:
        return _error(f"Failed to read file '{path_raw}': {str(e)}")


def _write_file(args: Dict[str, Any]) -> str:
    """Execute write_file tool and return JSON string result."""
    path_raw = args.get("path")
    if not path_raw:
        return json.dumps({"status": "error", "message": "Missing required parameter 'path'."})

    content = args.get("content")
    if content is None:
        return json.dumps({"status": "error", "message": "Missing required parameter 'content'."})

    append = bool(args.get("append", False))
    overwrite = bool(args.get("overwrite", True))

    try:
        file_path = workspace.resolve(str(path_raw))

        # Check existing file
        exists_before = file_path.exists()
        if exists_before and not append and not overwrite:
            return json.dumps({
                "status": "error",
                "message": f"File '{path_raw}' already exists and overwrite is set to False.",
            })

        # Ensure parent directories exist
        file_path.parent.mkdir(parents=True, exist_ok=True)

        mode = "a" if append else "w"
        with open(file_path, mode, encoding="utf-8") as f:
            f.write(content)

        bytes_written = len(content.encode("utf-8"))
        lines_written = len(content.splitlines())

        action_mode = "appended" if append else ("overwritten" if exists_before else "created")

        return json.dumps({
            "status": "success",
            "path": workspace.display(file_path),
            "mode": action_mode,
            "bytes_written": bytes_written,
            "lines_written": lines_written,
            "message": f"Successfully {action_mode} file '{workspace.display(file_path)}' ({bytes_written} bytes).",
        })

    except Exception as e:
        return json.dumps({"status": "error", "message": f"Failed to write file '{path_raw}': {str(e)}"})


def _line_end(match: "re.Match") -> str:
    """The line end for new lines replacing a match in a file with CRLF: the match's own, else its line's."""
    text = match.group()
    if "\n" not in text:
        end = match.string.find("\n", match.end())
        if end == -1:  # the file's last line, with no line end of its own
            return "\r\n"
        text = match.string[end - 1 : end + 1]
    return "\r\n" if "\r\n" in text else "\n"


def _edit_file(args: Dict[str, Any]) -> str:
    """Execute edit_file tool with exact target string matching."""
    path_raw = args.get("path")
    if not path_raw:
        return json.dumps({"status": "error", "message": "Missing required parameter 'path'."})

    old_str = args.get("old_str")
    if old_str is None:
        return json.dumps({"status": "error", "message": "Missing required parameter 'old_str'."})

    new_str = args.get("new_str")
    if new_str is None:
        return json.dumps({"status": "error", "message": "Missing required parameter 'new_str'."})

    replace_all = bool(args.get("replace_all", False))

    try:
        file_path = workspace.resolve(str(path_raw))
        if not file_path.exists():
            return json.dumps({
                "status": "error",
                "message": f"File not found: '{path_raw}'.",
            })
        if not file_path.is_file():
            return json.dumps({
                "status": "error",
                "message": f"Path is not a regular file: '{path_raw}'.",
            })

        if old_str == new_str:
            return json.dumps({
                "status": "error",
                "message": "old_str and new_str are identical; no edit was performed.",
            })

        # Strict: a file that is not UTF-8 is refused, not rewritten with every odd byte replaced.
        try:
            content = file_path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as e:
            return json.dumps({
                "status": "error",
                "message": f"File '{path_raw}' is not UTF-8 text ({e.reason} at byte {e.start}); edit_file left it unchanged.",
            })
        # Matched with \n line ends, as the model writes them; lines the edit leaves alone keep theirs.
        crlf = "\r\n" in content
        if crlf:
            old_str, new_str = old_str.replace("\r\n", "\n"), new_str.replace("\r\n", "\n")
            target = re.compile(r"\r?\n".join(re.escape(line) for line in old_str.split("\n")))
            occurrences = len(target.findall(content))
        else:
            occurrences = content.count(old_str)
        if occurrences == 0:
            return json.dumps({
                "status": "error",
                "message": (
                    f"Target string 'old_str' was not found in '{path_raw}'. "
                    "Please verify exact whitespace, indentation, and newlines."
                ),
            })

        if occurrences > 1 and not replace_all:
            return json.dumps({
                "status": "error",
                "message": (
                    f"Target string 'old_str' occurs {occurrences} times in '{path_raw}'. "
                    "To prevent unintended edits, provide more surrounding context to make it unique, "
                    "or set 'replace_all' to true."
                ),
                "occurrences": occurrences,
            })

        replacements = occurrences if replace_all else 1
        if crlf:
            new_content = target.sub(
                lambda match: new_str.replace("\n", _line_end(match)), content, count=0 if replace_all else 1,
            )
        elif replace_all:
            new_content = content.replace(old_str, new_str)
        else:
            new_content = content.replace(old_str, new_str, 1)

        file_path.write_bytes(new_content.encode("utf-8"))

        return json.dumps({
            "status": "success",
            "path": workspace.display(file_path),
            "replacements": replacements,
            "message": f"Successfully replaced {replacements} occurrence(s) in '{workspace.display(file_path)}'.",
        })

    except Exception as e:
        return json.dumps({"status": "error", "message": f"Failed to edit file '{path_raw}': {str(e)}"})


# File work runs in a thread, so a large file does not stall the server (other sessions, schedules, Esc).
async def execute_read_file_tool(args: Dict[str, Any]) -> ToolResult:
    return await asyncio.to_thread(_read_file, args)


async def execute_write_file_tool(args: Dict[str, Any]) -> str:
    return await asyncio.to_thread(_write_file, args)


async def execute_edit_file_tool(args: Dict[str, Any]) -> str:
    return await asyncio.to_thread(_edit_file, args)


READ_FILE_TOOL = Tool(
    definition=READ_FILE_TOOL_DEFINITION,
    execute=execute_read_file_tool,
    aliases=("read",),
)

WRITE_FILE_TOOL = Tool(
    definition=WRITE_FILE_TOOL_DEFINITION,
    execute=execute_write_file_tool,
    aliases=("write",),
)

EDIT_FILE_TOOL = Tool(
    definition=EDIT_FILE_TOOL_DEFINITION,
    execute=execute_edit_file_tool,
    aliases=("edit",),
)
