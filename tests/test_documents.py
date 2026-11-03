"""Documents turned into Markdown text for read_file (kilo/documents.py)."""

import os
import time
from dataclasses import replace
from pathlib import Path

import pytest
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls
from docx.shared import Inches

from kilo import documents
from samples import png_bytes, word

pytestmark = pytest.mark.usefixtures("data_dir")
DAY = 86400
MC = 'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'


def text_of(path: Path) -> str:
    return documents.convert(path).read_text(encoding="utf-8")


def test_a_word_document_in_order(tmp_path):
    def build(document):
        document.add_heading("季度報告", level=0)
        document.add_heading("Results", level=2)
        document.add_paragraph("Sales grew.")
        document.add_paragraph("")
        document.add_paragraph("First point", style="List Bullet")
        document.add_paragraph("Nested point", style="List Bullet 2")
        table = document.add_table(rows=2, cols=2)
        table.cell(0, 0).text, table.cell(0, 1).text = "Item", "Qty"
        table.cell(1, 0).text = "Pen"
        table.cell(1, 0).add_paragraph("blue")
        table.cell(1, 1).text = "3 | 4"
        document.add_picture(png_bytes(), width=Inches(1))
        document.inline_shapes[0]._inline.docPr.set("descr", "A red box")

    assert text_of(word(tmp_path / "report.docx", build)) == "\n".join([
        "# 季度報告", "",
        "## Results", "",
        "Sales grew.", "",
        "- First point", "",
        "  - Nested point", "",
        "| Item | Qty |",
        "| --- | --- |",
        "| Pen<br>blue | 3 \\| 4 |", "",
        "[Image: A red box]", "",
    ])


def test_a_numbered_paragraph_is_a_list_item_at_its_level(tmp_path):
    def build(document):
        document.add_paragraph("Step two")._p.get_or_add_pPr().append(
            parse_xml(f'<w:numPr {nsdecls("w")}><w:ilvl w:val="1"/><w:numId w:val="1"/></w:numPr>'))

    assert text_of(word(tmp_path / "steps.docx", build)) == "  - Step two\n"


def test_links_insertions_tabs_and_breaks_are_read_and_deletions_are_not(tmp_path):
    def build(document):
        paragraph = document.add_paragraph("See ")
        for xml in (
            '<w:hyperlink {w}><w:r><w:t>the site</w:t></w:r></w:hyperlink>',
            '<w:ins {w} w:id="1" w:author="a"><w:r><w:t xml:space="preserve"> now</w:t></w:r></w:ins>',
            '<w:del {w} w:id="2" w:author="a"><w:r><w:delText> never</w:delText></w:r></w:del>',
            '<w:r {w}><w:tab/><w:t>tabbed</w:t><w:br/><w:t>next line</w:t></w:r>',
        ):
            paragraph._p.append(parse_xml(xml.format(w=nsdecls("w"))))

    assert text_of(word(tmp_path / "edits.docx", build)) == "See the site now\ttabbed\nnext line\n"


def test_text_boxes_are_left_out(tmp_path):
    box = (
        f'<w:r {nsdecls("w", "wp")} {MC}><mc:AlternateContent>'
        '<mc:Choice Requires="wps"><w:drawing><wp:inline><wp:docPr id="9" name="Text Box 1" descr="a box"/>'
        '</wp:inline><w:t>boxed</w:t></w:drawing></mc:Choice>'
        '<mc:Fallback><w:pict><w:t>boxed</w:t></w:pict></mc:Fallback>'
        '</mc:AlternateContent></w:r>'
    )

    def build(document):
        document.add_paragraph("Body")._p.append(parse_xml(box))

    assert text_of(word(tmp_path / "box.docx", build)) == "Body\n"


def test_content_controls_are_read(tmp_path):
    def build(document):
        document.element.body.insert(0, parse_xml(
            f'<w:sdt {nsdecls("w")}><w:sdtContent><w:p><w:r><w:t>Contents</w:t></w:r></w:p></w:sdtContent></w:sdt>'))
        document.add_paragraph("Chapter one")

    assert text_of(word(tmp_path / "toc.docx", build)) == "Contents\n\nChapter one\n"


def test_kinds_by_extension_in_any_case():
    assert documents.kind_of(Path("NOTES.DOCX")).name == "docx"
    assert documents.kind_of(Path("notes.txt")) is None


def test_the_same_content_is_converted_once(tmp_path, monkeypatch):
    first = word(tmp_path / "a.docx", lambda document: document.add_paragraph("same"))
    second = tmp_path / "b.docx"
    second.write_bytes(first.read_bytes())
    converted = documents.convert(first)
    again = replace(documents.KINDS[".docx"], lines=lambda path: pytest.fail("converted again"))
    monkeypatch.setitem(documents.KINDS, ".docx", again)
    assert documents.convert(second) == converted


def test_a_long_text_stops_at_the_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(documents, "MAX_CHARS", 30)

    def build(document):
        for i in range(10):
            document.add_paragraph(f"paragraph {i}")

    assert text_of(word(tmp_path / "long.docx", build)) == (
        "paragraph 0\n\nparagraph 1\n\n"
        "[Conversion stopped here: the text passed 30 characters. The rest of the file is not included.]\n"
    )


def test_a_file_too_large_unzipped_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(documents, "MAX_UNZIPPED_BYTES", 1000)
    with pytest.raises(documents.DocumentError) as error:
        documents.convert(word(tmp_path / "big.docx", lambda document: document.add_paragraph("x")))
    assert str(error.value) == "would take more than 1 GB unzipped, so it was not read."


def test_an_ole_container_is_refused(tmp_path):
    path = tmp_path / "locked.docx"
    path.write_bytes(bytes.fromhex("D0CF11E0A1B11AE1") + bytes(504))
    with pytest.raises(documents.DocumentError) as error:
        documents.convert(path)
    assert str(error.value) == "is password-protected or in an old Office format, so it cannot be read."


def test_conversions_unused_for_30_days_are_deleted(tmp_path):
    documents.root().mkdir(parents=True)
    old, recent = documents.root() / "old.md", documents.root() / "recent.md"
    for path, days in ((old, 31), (recent, 29)):
        path.write_text("x")
        os.utime(path, (time.time() - days * DAY,) * 2)
    documents.convert(word(tmp_path / "new.docx", lambda document: document.add_paragraph("new")))
    assert not old.exists() and recent.exists()
