import json
from pathlib import Path

import pytest

from kilo.tools.search_tools import execute_glob_tool, execute_grep_tool


async def glob(**args):
    return json.loads(await execute_glob_tool(args))


async def grep(**args):
    return json.loads(await execute_grep_tool(args))


@pytest.fixture
def tree(workspace):
    (workspace / "src").mkdir()
    (workspace / "src" / "app.py").write_text("import os\nvalue = 1\n", encoding="utf-8")
    (workspace / "notes.md").write_text("Value notes\n", encoding="utf-8")
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config.py").write_text("value = 2\n", encoding="utf-8")
    (workspace / "blob.bin").write_bytes(b"value\x00")
    return workspace


def posix(paths):
    return [Path(p).as_posix() for p in paths]


async def test_glob_bare_name_matches_at_any_depth(tree):
    result = await glob(pattern="*.py")
    assert posix(result["matches"]) == ["src/app.py"]


async def test_glob_path_pattern(tree):
    assert posix((await glob(pattern="src/*.py"))["matches"]) == ["src/app.py"]


async def test_glob_stops_at_max_results(tree):
    result = await glob(pattern="*", max_results=1)
    assert result["count"] == 1 and result["truncated"] is True


async def test_glob_missing_directory_is_an_error(tree):
    assert (await glob(pattern="*", directory="nope"))["status"] == "error"


async def test_grep_finds_line_with_number(tree):
    result = await grep(pattern="value = 1")
    assert result["count"] == 1
    match = result["matches"][0]
    assert Path(match["file"]).as_posix() == "src/app.py"
    assert match["line_number"] == 2


async def test_grep_skips_ignored_folders_and_binary_files(tree):
    files = {Path(m["file"]).as_posix() for m in (await grep(pattern="value"))["matches"]}
    assert files == {"src/app.py"}


async def test_grep_case_insensitive(tree):
    assert (await grep(pattern="value", case_sensitive=False, file_pattern="*.md"))["count"] == 1


async def test_grep_literal_pattern(tree):
    assert (await grep(pattern="value.=", is_regex=False))["count"] == 0


async def test_grep_context_lines(tree):
    match = (await grep(pattern="value", path="src/app.py", context_lines=1))["matches"][0]
    assert [line["line_number"] for line in match["context"]] == [1, 2]


async def test_grep_no_match_is_success(tree):
    result = await grep(pattern="absent")
    assert result["status"] == "success" and result["count"] == 0


async def test_grep_invalid_regex_is_an_error(tree):
    assert (await grep(pattern="("))["status"] == "error"
