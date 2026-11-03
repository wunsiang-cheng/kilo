import json

import pytest
from PIL import Image

import samples
from kilo import documents, images
from kilo.tools.file_tools import (
    READ_FILE_TOOL, execute_edit_file_tool, execute_read_file_tool, execute_write_file_tool,
)

pytestmark = pytest.mark.usefixtures("workspace")


async def read(**args):
    return json.loads((await execute_read_file_tool(args)).text)


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


OLD = (
    "Old Office formats (.doc, .xls, .ppt) are not supported. Ask for .docx, .xlsx or .pptx instead, "
    "or convert the file with LibreOffice (soffice) if it is installed."
)
TOO_MANY = "At most 5 pages can be shown at once; ask for the others in another call."
BAD_PAGES = 'pages must be page numbers or ranges, like "3", "3-5" or "1,4".'


def test_read_file_says_what_it_reads():
    definition = READ_FILE_TOOL.definition
    assert definition["description"] == (
        "Read a file. Text comes as numbered lines: each line with its number and ' | ' in front (e.g. '   12 | text'), "
        "which are not part of the file. Lines longer than 2,000 characters are cut, with a marker, and a long read "
        "stops early with `truncated` and a `message` saying where to continue. Bytes that are not UTF-8 show as "
        "replacement characters. PDF, Word (.docx), Excel (.xlsx) and PowerPoint (.pptx) files are converted to "
        "Markdown text, numbered and read in parts the same way; the result's `text_file` holds the whole text, for "
        "grep. Editing that text does not change the original file. Images (PNG, JPEG, WebP, GIF, BMP, TIFF) are shown "
        "to you in a message after the tool results, as are PDF pages asked for with `pages`. Other binary files are "
        "refused."
    )
    assert definition["parameters"]["properties"]["pages"] == {"type": "string", "description": (
        'PDF only: pages to look at as images rather than read as text, e.g. "3", "3-5" or "1,4"; at most 5. '
        "For scanned pages, and for charts and layouts the text leaves out."
    )}


async def test_an_image_is_shown_to_the_model(workspace, data_dir):
    Image.new("RGB", (4096, 1024), "blue").save(workspace / "wide.png")
    result = await execute_read_file_tool({"path": "wide.png"})
    assert json.loads(result.text) == {
        "status": "success", "path": "wide.png", "type": "image/png", "width": 4096, "height": 1024,
        "shown_width": 2048, "shown_height": 512, "message": "The image is shown in the message after the tool results.",
    }
    [shown] = result.images
    assert shown.label == "wide.png" and (images.root() / shown.file).exists()


async def test_an_animation_says_which_frame_is_shown(workspace, data_dir):
    frames = [Image.new("RGB", (8, 8), color) for color in ("red", "blue")]
    frames[0].save(workspace / "moving.gif", save_all=True, append_images=frames[1:])
    result = await read(path="moving.gif")
    assert result["frames"] == 2
    assert result["message"] == "The first of its 2 frames is shown in the message after the tool results."


async def test_extensions_are_matched_in_any_case(workspace, data_dir):
    Image.new("RGB", (8, 8)).save(workspace / "PHOTO.JPG", format="JPEG")
    assert (await read(path="PHOTO.JPG"))["type"] == "image/jpeg"
    samples.word(workspace / "NOTES.DOCX", lambda document: document.add_paragraph("upper"))
    assert (await read(path="NOTES.DOCX"))["converted_from"] == "docx"
    samples.pdf(workspace / "SCAN.PDF", "one")
    assert (await read(path="SCAN.PDF", pages="1"))["pages"] == [1]


async def test_a_broken_image_is_an_error(workspace, data_dir):
    (workspace / "broken.png").write_bytes(b"not an image")
    result = await read(path="broken.png")
    assert result["status"] == "error" and result["message"].startswith("Could not read 'broken.png' as an image: ")


async def test_a_document_is_read_as_numbered_text(workspace, data_dir):
    def build(document):
        document.add_heading("Title", 1)
        document.add_paragraph("Body")

    samples.word(workspace / "report.docx", build)
    result = await read(path="report.docx")
    assert result == {
        "status": "success", "path": "report.docx", "converted_from": "docx",
        "text_file": str(documents.convert(workspace / "report.docx")),
        "total_lines": 3, "start_line": 1, "end_line": 3, "lines_returned": 3, "truncated": False,
        "content": "    1 | # Title\n    2 | \n    3 | Body",
    }


async def test_a_long_document_is_read_in_parts(workspace, data_dir):
    def build(document):
        for i in range(10):
            document.add_paragraph(f"paragraph {i}")

    samples.word(workspace / "long.docx", build)
    first = await read(path="long.docx", max_lines=4)
    assert first["truncated"] is True and "start_line=5" in first["message"]
    assert (await read(path="long.docx", start_line=5, max_lines=1))["content"] == "    5 | paragraph 2"


@pytest.mark.parametrize("name,message", [
    ("photo.heic", "HEIC images are not supported. Ask for a JPEG or PNG instead."),
    ("old.doc", OLD), ("old.xls", OLD), ("old.ppt", OLD),
])
async def test_formats_not_supported_are_named(workspace, name, message):
    (workspace / name).write_bytes(b"\0binary")
    assert await read(path=name) == {"status": "error", "message": message}


async def test_other_binary_files_list_what_can_be_read(workspace):
    (workspace / "blob.bin").write_bytes(bytes(10))
    assert (await read(path="blob.bin"))["message"] == (
        "File 'blob.bin' appears to be binary (10 bytes). read_file reads text files, images (PNG, JPEG, WebP, GIF, "
        "BMP, TIFF), PDF, Word (.docx), Excel (.xlsx) and PowerPoint (.pptx) files."
    )


async def test_a_corrupt_document_is_an_error(workspace, data_dir):
    (workspace / "bad.docx").write_text("not a zip")
    assert (await read(path="bad.docx"))["message"].startswith("Could not read 'bad.docx' as a Word document: ")


async def test_a_locked_document_says_so(workspace, data_dir):
    (workspace / "locked.xlsx").write_bytes(bytes.fromhex("D0CF11E0A1B11AE1") + bytes(504))
    assert (await read(path="locked.xlsx"))["message"] == (
        "'locked.xlsx' is password-protected or in an old Office format, so it cannot be read."
    )


async def test_pdf_pages_are_shown_as_images(workspace, data_dir):
    samples.pdf(workspace / "scan.pdf", "one", "", "three")
    result = await execute_read_file_tool({"path": "scan.pdf", "pages": "2-3"})
    assert json.loads(result.text) == {
        "status": "success", "path": "scan.pdf", "total_pages": 3, "pages": [2, 3],
        "message": "The pages are shown as images in the message after the tool results.",
    }
    assert [shown.label for shown in result.images] == ["scan.pdf, page 2", "scan.pdf, page 3"]


async def test_pages_may_be_a_number(workspace, data_dir):
    samples.pdf(workspace / "scan.pdf", "one")
    assert (await read(path="scan.pdf", pages=1))["pages"] == [1]


@pytest.mark.parametrize("pages,message", [
    ("1-6", TOO_MANY), ("7", "Page 7 is out of range: the PDF has 6 pages."), ("x", BAD_PAGES),
])
async def test_pages_that_cannot_be_shown(workspace, data_dir, pages, message):
    samples.pdf(workspace / "six.pdf", *[f"page {i}" for i in range(1, 7)])
    assert await read(path="six.pdf", pages=pages) == {"status": "error", "message": message}


async def test_pages_apply_to_pdf_files_only(workspace):
    (workspace / "notes.txt").write_text("hi")
    assert await read(path="notes.txt", pages="1") == {"status": "error", "message": "pages applies to PDF files only."}


async def test_empty_pages_read_the_text(workspace, data_dir):
    samples.pdf(workspace / "doc.pdf", "one")
    assert (await read(path="doc.pdf", pages=""))["converted_from"] == "pdf"


async def test_a_password_protected_pdf_says_so(workspace, data_dir):
    samples.locked_pdf(workspace / "locked.pdf")
    assert (await read(path="locked.pdf"))["message"] == "'locked.pdf' is password-protected, so it cannot be read."
