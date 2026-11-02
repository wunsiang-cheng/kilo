import json

import pytest

from kilo.tools.file_tools import execute_edit_file_tool, execute_read_file_tool, execute_write_file_tool

pytestmark = pytest.mark.usefixtures("workspace")


async def read(**args):
    return json.loads(await execute_read_file_tool(args))


async def write(**args):
    return json.loads(await execute_write_file_tool(args))


async def edit(**args):
    return json.loads(await execute_edit_file_tool(args))


async def test_relative_path_resolves_in_workspace(workspace):
    await write(path="a.txt", content="hi")
    assert (workspace / "a.txt").read_text() == "hi"


async def test_absolute_path_works_outside_workspace(tmp_path):
    target = tmp_path / "outside.txt"
    await write(path=str(target), content="hi")
    assert target.read_text() == "hi"


async def test_write_creates_parents_then_read_numbers_lines():
    await write(path="deep/er/a.txt", content="one\ntwo\n")
    result = await read(path="deep/er/a.txt")
    assert result["status"] == "success"
    assert result["total_lines"] == 2
    assert result["content"] == "    1 | one\n    2 | two"


async def test_write_appends():
    await write(path="a.txt", content="one")
    result = await write(path="a.txt", content="two", append=True)
    assert result["mode"] == "appended"
    assert (await read(path="a.txt"))["content"].endswith("onetwo")


async def test_read_range_and_truncation_message():
    await write(path="a.txt", content="\n".join(str(i) for i in range(1, 11)))
    result = await read(path="a.txt", start_line=1, max_lines=2)
    assert result["truncated"] is True
    assert result["end_line"] == 2
    assert "start_line=3" in result["message"]


async def test_read_start_past_end_returns_nothing():
    await write(path="a.txt", content="one")
    result = await read(path="a.txt", start_line=5)
    assert result["status"] == "success" and result["lines_returned"] == 0


async def test_read_missing_file_is_an_error():
    assert (await read(path="nope.txt"))["status"] == "error"


async def test_read_binary_refused(workspace):
    (workspace / "b.bin").write_bytes(b"\x00\x01\x02")
    result = await read(path="b.bin")
    assert result["status"] == "error" and "binary" in result["message"]


async def test_write_overwrite_false_refused(workspace):
    await write(path="a.txt", content="one")
    assert (await write(path="a.txt", content="two", overwrite=False))["status"] == "error"
    assert (workspace / "a.txt").read_text() == "one"


async def test_edit_replaces_unique_target(workspace):
    await write(path="a.txt", content="hello world")
    assert (await edit(path="a.txt", old_str="world", new_str="kilo"))["replacements"] == 1
    assert (workspace / "a.txt").read_text() == "hello kilo"


async def test_edit_refuses_ambiguous_target(workspace):
    await write(path="a.txt", content="x x")
    result = await edit(path="a.txt", old_str="x", new_str="y")
    assert result["status"] == "error" and result["occurrences"] == 2
    assert (workspace / "a.txt").read_text() == "x x"


async def test_edit_replace_all(workspace):
    await write(path="a.txt", content="x x")
    assert (await edit(path="a.txt", old_str="x", new_str="y", replace_all=True))["replacements"] == 2
    assert (workspace / "a.txt").read_text() == "y y"


async def test_edit_missing_target_is_an_error():
    await write(path="a.txt", content="x")
    assert (await edit(path="a.txt", old_str="nope", new_str="y"))["status"] == "error"


async def test_edit_identical_strings_refused():
    await write(path="a.txt", content="x")
    assert (await edit(path="a.txt", old_str="x", new_str="x"))["status"] == "error"


async def test_edit_preserves_crlf(workspace):
    target = workspace / "a.txt"
    target.write_bytes(b"a\r\nb\r\n")
    result = await edit(path="a.txt", old_str="a\nb", new_str="c\nd")
    assert result["status"] == "success"
    assert target.read_bytes() == b"c\r\nd\r\n"


async def test_edit_leaves_other_lines_of_a_mixed_file_alone(workspace):
    target = workspace / "a.txt"
    target.write_bytes(b"a\nb\r\nc\n")
    await edit(path="a.txt", old_str="a", new_str="x")
    assert target.read_bytes() == b"x\nb\r\nc\n"


async def test_edit_matches_across_mixed_line_ends(workspace):
    target = workspace / "a.txt"
    target.write_bytes(b"a\nb\r\nc\n")
    result = await edit(path="a.txt", old_str="a\nb\nc", new_str="z")
    assert result["replacements"] == 1
    assert target.read_bytes() == b"z\n"


@pytest.mark.parametrize("before,after", [
    (b"a\nb\r\nc\n", b"a\nb\r\nnew\r\nc\n"),  # on a CRLF line of a mixed file
    (b"a\r\nb\nc\r\n", b"a\r\nb\nnew\nc\r\n"),  # on an LF line of a mixed file
    (b"a\r\nb", b"a\r\nb\r\nnew"),  # on a last line with no line end
])
async def test_added_lines_take_the_line_end_of_where_they_land(workspace, before, after):
    target = workspace / "a.txt"
    target.write_bytes(before)
    await edit(path="a.txt", old_str="b", new_str="b\nnew")
    assert target.read_bytes() == after


async def test_edit_replace_all_in_crlf_file(workspace):
    target = workspace / "a.txt"
    target.write_bytes(b"x\r\nx\r\n")
    assert (await edit(path="a.txt", old_str="x", new_str="y", replace_all=True))["replacements"] == 2
    assert target.read_bytes() == b"y\r\ny\r\n"


async def test_edit_ambiguous_target_refused_in_crlf_file(workspace):
    target = workspace / "a.txt"
    target.write_bytes(b"x\r\nx\r\n")
    assert (await edit(path="a.txt", old_str="x", new_str="y"))["occurrences"] == 2
    assert target.read_bytes() == b"x\r\nx\r\n"


async def test_edit_refuses_non_utf8_and_leaves_file(workspace):
    target = workspace / "a.txt"
    target.write_bytes(b"\xff\xfe x")
    result = await edit(path="a.txt", old_str="x", new_str="y")
    assert result["status"] == "error" and "not UTF-8" in result["message"]
    assert target.read_bytes() == b"\xff\xfe x"
