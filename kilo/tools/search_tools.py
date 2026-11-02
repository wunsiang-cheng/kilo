"""Search and location tools: glob and grep."""

import asyncio
import fnmatch
import json
import os
import re
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Set

from kilo import workspace
from kilo.tools.base import Tool
from kilo.tools.output_limit import clip_line

GREP_LINE_CHARS = 500  # longer matching or context lines are cut, with a marker giving their length

DEFAULT_IGNORE_DIRS: Set[str] = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".kilo_data",
    "node_modules",
    ".tox",
    "dist",
    "build",
    ".mypy_cache",
    ".ruff_cache",
}

_SKIPPED = ", ".join(sorted(DEFAULT_IGNORE_DIRS))

GLOB_TOOL_DEFINITION = {
    "name": "glob",
    "description": (
        "Find files and directories by name with a glob pattern (e.g. '**/*.py', 'src/**/*.ts', '*.json'). A bare name "
        "such as '*.py' matches at any depth unless `recursive` is false. Returns their paths, with `truncated` when "
        f"`max_results` was reached. Folders below `directory` named {_SKIPPED} are skipped, whatever `ignore_hidden` "
        "says; to look inside one, make it the `directory`."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob pattern to match files (e.g. '**/*.py', '*.env', 'tests/test_*.py').",
            },
            "directory": {
                "type": "string",
                "description": "Base directory to start search in (default: '.', the workspace).",
                "default": ".",
            },
            "recursive": {
                "type": "boolean",
                "description": "Whether to search recursively in subdirectories (default: true).",
                "default": True,
            },
            "ignore_hidden": {
                "type": "boolean",
                "description": "Whether to ignore hidden files and directories (default: true).",
                "default": True,
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of paths to return (default: 200).",
                "default": 200,
            },
        },
        "required": ["pattern"],
    },
}

GREP_TOOL_DEFINITION = {
    "name": "grep",
    "description": (
        "Search the text of a file, or of the files in a directory, with a Python regular expression (or plain text, "
        "with `is_regex` false). Returns each matching line with its file and number, lines cut at 500 characters, and "
        "`truncated` when `max_results` was reached. A directory search skips binary files, hidden files and folders, "
        f"and folders named {_SKIPPED}; give one of those as `path` to search it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Text or regular expression pattern to search for.",
            },
            "path": {
                "type": "string",
                "description": "Directory or file path to search in (default: '.', the workspace).",
                "default": ".",
            },
            "file_pattern": {
                "type": "string",
                "description": "Optional file name filter for a directory search (e.g. '*.py', '*.json', '*.md').",
            },
            "case_sensitive": {
                "type": "boolean",
                "description": "Whether the search is case-sensitive (default: true).",
                "default": True,
            },
            "is_regex": {
                "type": "boolean",
                "description": "Whether pattern is a regular expression (default: true).",
                "default": True,
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum matching lines to return (default: 100).",
                "default": 100,
            },
            "context_lines": {
                "type": "integer",
                "description": "Number of context lines before and after match (default: 0).",
                "default": 0,
            },
        },
        "required": ["pattern"],
    },
}


def _is_binary(file_path: Path) -> bool:
    """Whether a file looks binary (a NUL byte near its start). Size is no reason: large logs are text."""
    try:
        with open(file_path, "rb") as f:
            chunk = f.read(1024)
            return b"\x00" in chunk
    except Exception:
        return True


def _should_ignore_path(rel_parts: tuple, ignore_hidden: bool) -> bool:
    """Check if path contains ignored or hidden directory components."""
    for part in rel_parts:
        if part in DEFAULT_IGNORE_DIRS:
            return True
        if ignore_hidden and part.startswith(".") and part != ".":
            return True
    return False


def _grep_file(
    file_p: Path, display_path: str, regex: "re.Pattern", context_lines: int, matches: List[Dict[str, Any]], max_results: int,
) -> bool:
    """
    Add a file's matching lines to `matches`, reading it line by line (any size). Returns whether
    `max_results` was reached; the last match still gets its lines of context after it.
    """
    def entry(number: int, text: str) -> Dict[str, Any]:
        return {"line_number": number, "content": clip_line(text, GREP_LINE_CHARS)}

    before: deque = deque(maxlen=context_lines or None)
    waiting: List[List[Any]] = []  # [match, lines of context after it still to add]
    full = False
    try:
        with open(file_p, "r", encoding="utf-8", errors="replace") as f:
            for number, line in enumerate(f, start=1):
                line = line.rstrip("\r\n")
                for item in waiting:
                    item[0]["context"].append(entry(number, line))
                    item[1] -= 1
                waiting = [item for item in waiting if item[1] > 0]
                if full:
                    if not waiting:
                        break
                    continue
                if regex.search(line):
                    match: Dict[str, Any] = {"file": display_path, **entry(number, line)}
                    if context_lines > 0:
                        match["context"] = [*before, entry(number, line)]
                        waiting.append([match, context_lines])
                    matches.append(match)
                    full = len(matches) >= max_results
                if context_lines > 0:
                    before.append(entry(number, line))
    except OSError:
        pass
    return full


async def execute_glob_tool(args: Dict[str, Any]) -> str:
    return await asyncio.to_thread(_glob, args)


async def execute_grep_tool(args: Dict[str, Any]) -> str:
    return await asyncio.to_thread(_grep, args)


def _glob(args: Dict[str, Any]) -> str:
    """Execute glob file search tool."""
    pattern = args.get("pattern")
    if not pattern:
        return json.dumps({"status": "error", "message": "Missing required parameter 'pattern'."})

    dir_raw = args.get("directory", ".") or "."
    recursive = bool(args.get("recursive", True))
    ignore_hidden = bool(args.get("ignore_hidden", True))
    max_results = int(args.get("max_results", 200) or 200)

    try:
        base_dir = workspace.resolve(dir_raw)
        if not base_dir.exists():
            return json.dumps({
                "status": "error",
                "message": f"Directory not found: '{dir_raw}' (resolved: '{base_dir}').",
            })
        if not base_dir.is_dir():
            return json.dumps({
                "status": "error",
                "message": f"Path is not a directory: '{dir_raw}'.",
            })

        matches: List[str] = []
        truncated = False

        # Adjust pattern for recursive search if needed
        # If recursive=True and pattern is simple filename pattern (e.g. "*.py") without subdirs or **,
        # match in any subfolder
        patterns_to_try = [pattern]
        if recursive and "**" not in pattern and "/" not in pattern and "\\" not in pattern:
            patterns_to_try = [f"**/{pattern}", pattern]

        seen_paths: Set[Path] = set()

        if len(patterns_to_try) == 2:
            # A bare name pattern at any depth: walk the tree, never entering ignored folders.
            for root, dirs, files in os.walk(base_dir):
                dirs[:] = sorted(d for d in dirs if d not in DEFAULT_IGNORE_DIRS and not (ignore_hidden and d.startswith(".")))
                for name in [*dirs, *files]:
                    if (ignore_hidden and name.startswith(".")) or not fnmatch.fnmatchcase(name, pattern):
                        continue
                    matches.append(workspace.display(Path(root) / name))
                    if len(matches) >= max_results:
                        truncated = True
                        break
                if truncated:
                    break
            patterns_to_try = []

        for pat in patterns_to_try:
            for p in base_dir.glob(pat):
                if p in seen_paths:
                    continue
                seen_paths.add(p)

                # Relative to base_dir
                try:
                    rel_p = p.relative_to(base_dir)
                except ValueError:
                    rel_p = p

                if _should_ignore_path(rel_p.parts, ignore_hidden):
                    continue

                # Ignore top-level hidden file if ignore_hidden is set
                if ignore_hidden and p.name.startswith("."):
                    continue

                # Add match, as a path the file tools accept
                matches.append(workspace.display(p))
                if len(matches) >= max_results:
                    truncated = True
                    break
            if truncated:
                break

        matches.sort()

        return json.dumps({
            "status": "success",
            "directory": str(dir_raw),
            "pattern": pattern,
            "count": len(matches),
            "truncated": truncated,
            "matches": matches,
        })

    except Exception as e:
        return json.dumps({"status": "error", "message": f"Glob search failed: {str(e)}"})


def _grep(args: Dict[str, Any]) -> str:
    """Execute grep text search tool (in a thread: a large tree must not stall the server)."""
    pattern = args.get("pattern")
    if not pattern:
        return json.dumps({"status": "error", "message": "Missing required parameter 'pattern'."})

    path_raw = args.get("path", ".") or "."
    file_pattern = args.get("file_pattern")
    case_sensitive = bool(args.get("case_sensitive", True))
    is_regex = bool(args.get("is_regex", True))
    max_results = int(args.get("max_results", 100) or 100)
    context_lines = max(0, int(args.get("context_lines", 0) or 0))

    try:
        # Compile regex or literal
        flags = 0 if case_sensitive else re.IGNORECASE
        if is_regex:
            try:
                regex = re.compile(pattern, flags)
            except re.error as e:
                return json.dumps({"status": "error", "message": f"Invalid regular expression '{pattern}': {e}"})
        else:
            regex = re.compile(re.escape(pattern), flags)

        target_path = workspace.resolve(path_raw)
        if not target_path.exists():
            return json.dumps({
                "status": "error",
                "message": f"Path not found: '{path_raw}' (resolved: '{target_path}').",
            })

        files_to_search: List[Path] = []
        if target_path.is_file():
            files_to_search.append(target_path)
            base_dir = target_path.parent
        else:
            base_dir = target_path
            # Walk directory tree
            for root, dirs, files in os.walk(target_path):
                # Filter out ignored directories in-place so os.walk doesn't descend into them
                dirs[:] = [
                    d for d in dirs
                    if d not in DEFAULT_IGNORE_DIRS and not (d.startswith(".") and d != ".")
                ]
                for file_name in files:
                    if file_name.startswith("."):
                        continue
                    if file_pattern and not fnmatch.fnmatch(file_name, file_pattern):
                        continue
                    file_p = Path(root) / file_name
                    files_to_search.append(file_p)

        matches = []
        truncated = False

        for file_p in files_to_search:
            if _is_binary(file_p):
                continue
            display_path = workspace.display(file_p)
            truncated = _grep_file(file_p, display_path, regex, context_lines, matches, max_results)
            if truncated:
                break

        return json.dumps({
            "status": "success",
            "pattern": pattern,
            "path": str(path_raw),
            "file_pattern": file_pattern,
            "count": len(matches),
            "truncated": truncated,
            "matches": matches,
        })

    except Exception as e:
        return json.dumps({"status": "error", "message": f"Grep search failed: {str(e)}"})


GLOB_TOOL = Tool(
    definition=GLOB_TOOL_DEFINITION,
    execute=execute_glob_tool,
    aliases=("glob_files",),
)

GREP_TOOL = Tool(
    definition=GREP_TOOL_DEFINITION,
    execute=execute_grep_tool,
    aliases=("grep_search",),
)
