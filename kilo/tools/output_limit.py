"""
Tool output size: every tool result reaches the model through `limit_output`, which keeps it under
KILO_TOOL_OUTPUT_MAX_CHARS. An oversized result keeps its shape (valid JSON, every short field intact):
its longest texts are shortened in the middle, and the full output is saved to a file the agent can
read in parts.
"""

import json
import time
import uuid
from pathlib import Path
from typing import Any, List, Optional, Tuple

from kilo.config import settings

OMITTED = "\n[… {count:,} characters omitted …]\n"
MIN_KEEP = 1000  # a shortened text keeps at least this much of its head and tail
NOTE_ROOM = 600  # characters kept free for the output_truncated note
SPILL_DIR = "tool-output"
SPILL_DAYS = 7  # saved full outputs older than this are deleted


def clip_line(line: str, max_chars: int) -> str:
    """One line of file or search output, cut to max_chars with a marker saying how long it was."""
    if len(line) <= max_chars:
        return line
    return f"{line[:max_chars]} … [line cut: {len(line):,} characters]"


def _dumps(obj: Any, indent: Optional[int] = None) -> str:
    # Non-ASCII text as itself: "學" costs several times the tokens of "學".
    return json.dumps(obj, ensure_ascii=False, indent=indent, default=str)


def _texts(obj: Any, path: Tuple = ()) -> List[Tuple[Tuple, str]]:
    """Every string value in a JSON value, with its path."""
    if isinstance(obj, str):
        return [(path, obj)]
    items = obj.items() if isinstance(obj, dict) else enumerate(obj) if isinstance(obj, list) else ()
    return [found for key, value in items for found in _texts(value, path + (key,))]


def _lists(obj: Any, path: Tuple = ()) -> List[Tuple[Tuple, list]]:
    """Every list in a JSON value, with its path."""
    found = [(path, obj)] if isinstance(obj, list) else []
    items = obj.items() if isinstance(obj, dict) else enumerate(obj) if isinstance(obj, list) else ()
    return found + [inner for key, value in items for inner in _lists(value, path + (key,))]


def _replace(obj: Any, path: Tuple, value: Any) -> None:
    for key in path[:-1]:
        obj = obj[key]
    obj[path[-1]] = value


def _shorten(text: str, keep: int) -> str:
    """Keep `keep` characters: two thirds from the head, one third from the tail."""
    head = keep * 2 // 3
    return text[:head] + OMITTED.format(count=len(text) - keep) + text[len(text) - (keep - head):]


def _label(path: Tuple) -> str:
    return ".".join(str(key) for key in path) or "output"


def _spill(tool: str, obj: Any, raw: str) -> Path:
    """Save a full output where the agent can read it: long texts as plain text, after the rest as JSON."""
    folder = settings.kilo_data_dir / SPILL_DIR
    folder.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - SPILL_DAYS * 86400
    for old in folder.glob("*.txt"):
        if old.stat().st_mtime < cutoff:
            old.unlink(missing_ok=True)

    if obj is None:
        content = raw
    elif not isinstance(obj, (dict, list)):
        content = obj if isinstance(obj, str) else _dumps(obj)
    else:
        long_texts = [(p, t) for p, t in _texts(obj) if len(t) > MIN_KEEP]
        outline = json.loads(_dumps(obj))
        for p, _ in long_texts:
            _replace(outline, p, f"<{_label(p)}: below>")
        content = "\n\n".join([_dumps(outline, indent=2)] + [f"===== {_label(p)} =====\n{t}" for p, t in long_texts])
    path = folder / f"{time.strftime('%Y%m%d-%H%M%S')}-{tool.replace('/', '_')[:40]}-{uuid.uuid4().hex[:6]}.txt"
    path.write_text(content + "\n", encoding="utf-8")
    return path


def limit_output(tool: str, result: str) -> str:
    """A tool result as the model will see it: JSON with non-ASCII text unescaped, within the size limit."""
    try:
        obj: Any = json.loads(result)
        text = _dumps(obj)
    except (TypeError, ValueError):
        obj, text = None, result
    limit = settings.kilo_tool_output_max_chars
    if len(text) <= limit:
        return text

    original, saved = len(text), _spill(tool, obj, text)
    budget = limit - NOTE_ROOM
    shortened: Any = json.loads(text) if isinstance(obj, (dict, list)) else None
    kept = ""
    if shortened is not None:
        # First the longest texts, shortened in the middle...
        while len(text) > budget:
            path, longest = max(_texts(shortened), key=lambda found: len(found[1]), default=((), ""))
            if len(longest) <= MIN_KEEP:
                break
            _replace(shortened, path, _shorten(longest, max(MIN_KEEP, len(longest) - (len(text) - budget) - 100)))
            text = _dumps(shortened)
        # ...then the longest list (many matches, many rows), cut to its first items.
        if len(text) > budget:
            path, items = max(_lists(shortened), key=lambda found: len(_dumps(found[1])), default=((), []))
            if path and len(items) > 1:
                low, high = 1, len(items)  # the most items that fit
                while low < high:
                    middle = (low + high + 1) // 2
                    _replace(shortened, path, items[:middle])
                    low, high = (middle, high) if len(_dumps(shortened)) <= budget else (low, middle - 1)
                _replace(shortened, path, items[:low])
                text = _dumps(shortened)
                kept = f" The list {_label(path)} keeps its first {low:,} of {len(items):,} items."
        if not isinstance(shortened, dict):
            shortened = {"status": "success", "output": shortened}
            text = _dumps(shortened)
    if shortened is None or len(text) > budget:  # not JSON, or no way to cut it inside: the start and end of it all
        status = shortened.get("status", "success") if isinstance(shortened, dict) else "success"
        whole, keep = (_dumps(obj) if obj is not None else text), budget - 100
        shortened = {"status": status, "output": _shorten(whole, keep)}
        while len(_dumps(shortened)) > budget:  # escaped quotes and newlines take more room
            keep -= len(_dumps(shortened)) - budget + 50
            shortened = {"status": status, "output": _shorten(whole, keep)}
        kept = ""

    shortened["output_truncated"] = (
        f"This result was {original:,} characters, over the {limit:,}-character limit for one tool result, "
        "so its longest parts were shortened (marked '[… N characters omitted …]')." + kept + " The full output "
        f"is saved in {saved}: read it in parts with read_file (start_line/end_line) or search it with grep."
    )
    return _dumps(shortened)
