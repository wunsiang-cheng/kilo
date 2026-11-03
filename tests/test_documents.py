"""Documents turned into Markdown text for read_file (kilo/documents.py)."""

import os
import time
from dataclasses import replace
from datetime import datetime, time as clock_time
from pathlib import Path

import pytest
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls
from docx.shared import Inches
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.oxml.xmlchemy import OxmlElement
from pptx.util import Inches as SlideInches

from kilo import documents, images
from samples import locked_pdf, pdf, png_bytes, presentation, with_dimension, with_saved_value, word, workbook

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


def test_a_sheet_is_a_table_with_its_column_letters_and_row_numbers(tmp_path):
    def build(book):
        sheet = book.active
        sheet.title = "銷售"
        sheet.append(["Date", "Item", "Amount", "Paid"])
        sheet.append([datetime(2026, 1, 3), "Pen | blue", 120, True])
        sheet.append([datetime(2026, 1, 4, 14, 30), "Line\nbreak", 0.5, False])
        sheet["A5"] = clock_time(9, 15)

    assert text_of(workbook(tmp_path / "sales.xlsx", build)) == "\n".join([
        "## Sheet 1: 銷售", "",
        "|   | A | B | C | D |",
        "| --- | --- | --- | --- | --- |",
        "| 1 | Date | Item | Amount | Paid |",
        "| 2 | 2026-01-03 | Pen \\| blue | 120 | TRUE |",
        "| 3 | 2026-01-04 14:30:00 | Line<br>break | 0.5 | FALSE |",
        "| 5 | 09:15:00 |  |  |  |", "",
    ])


def test_cells_keep_their_addresses(tmp_path):
    def build(book):
        book.active["C5"] = "far"
        book.active["E7"] = 3

    assert text_of(workbook(tmp_path / "far.xlsx", build)) == "\n".join([
        "## Sheet 1: Sheet", "",
        "|   | A | B | C | D | E |",
        "| --- | --- | --- | --- | --- | --- |",
        "| 5 |  |  | far |  |  |",
        "| 7 |  |  |  |  | 3 |", "",
    ])


def test_hidden_and_empty_sheets_are_marked(tmp_path):
    def build(book):
        book.active.title = "Empty"
        secret = book.create_sheet("Secret")
        secret.sheet_state = "hidden"
        secret["A1"] = "x"

    assert text_of(workbook(tmp_path / "sheets.xlsx", build)) == "\n".join([
        "## Sheet 1: Empty", "", "(empty)", "",
        "## Sheet 2: Secret (hidden)", "",
        "|   | A |", "| --- | --- |", "| 1 | x |", "",
    ])


def test_a_formula_shows_the_value_saved_with_it(tmp_path):
    def build(book):
        book.active.append([2, "=A1*2"])
        book.active.append([3, "=A2*2"])

    path = with_saved_value(workbook(tmp_path / "formulas.xlsx", build), "A1*2", "4")
    assert text_of(path).splitlines()[4:6] == ["| 1 | 2 | 4 |", "| 2 | 3 |  |"]


def test_a_long_sheet_stops_at_the_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(documents, "MAX_CHARS", 200)

    def build(book):
        for i in range(1000):
            book.active.append([i])

    text = text_of(workbook(tmp_path / "long.xlsx", build))
    assert text.endswith("[Conversion stopped here: the text passed 200 characters. The rest of the file is not included.]\n")
    assert "| 1000 |" not in text


def test_a_slide_reads_from_top_to_bottom_then_left_to_right(tmp_path):
    def build(deck):
        slide = deck.slides.add_slide(deck.slide_layouts[5])  # title only
        slide.shapes.title.text = "季度結果"
        for left, text in ((5, "Right"), (1, "Left")):
            slide.shapes.add_textbox(SlideInches(left), SlideInches(2), SlideInches(3), SlideInches(1)).text_frame.text = text
        frame = slide.shapes.add_textbox(SlideInches(1), SlideInches(4), SlideInches(3), SlideInches(2)).text_frame
        frame.text = "Points"
        for level, text in ((1, "Revenue up"), (2, "In Asia")):
            paragraph = frame.add_paragraph()
            paragraph.text, paragraph.level = text, level
        broken = frame.add_paragraph()
        broken.text = "one"
        broken.add_line_break()
        broken.add_run().text = "two"

    assert text_of(presentation(tmp_path / "deck.pptx", build)) == "\n".join([
        "## Slide 1: 季度結果", "",
        "Left", "Right", "Points", "- Revenue up", "  - In Asia", "one\ntwo", "",
    ])


def test_tables_pictures_charts_notes_and_hidden_slides(tmp_path):
    def build(deck):
        slide = deck.slides.add_slide(deck.slide_layouts[6])  # blank: no title
        table = slide.shapes.add_table(2, 2, SlideInches(1), SlideInches(1), SlideInches(4), SlideInches(1)).table
        for (row, column), text in {(0, 0): "Item", (0, 1): "Qty", (1, 0): "Pen", (1, 1): "3"}.items():
            table.cell(row, column).text = text
        picture = slide.shapes.add_picture(png_bytes(), SlideInches(1), SlideInches(3))
        picture._element.nvPicPr.cNvPr.set("descr", "Logo")
        data = CategoryChartData()
        data.categories = ["Q1"]
        data.add_series("Sales", (1,))
        chart = slide.shapes.add_chart(
            XL_CHART_TYPE.COLUMN_CLUSTERED, SlideInches(1), SlideInches(5), SlideInches(4), SlideInches(2), data,
        ).chart
        chart.has_title = True
        chart.chart_title.text_frame.text = "Sales"
        slide.notes_slide.notes_text_frame.text = "Say it slowly"
        slide._element.set("show", "0")

    assert text_of(presentation(tmp_path / "parts.pptx", build)) == "\n".join([
        "## Slide 1 (hidden)", "",
        "| Item | Qty |", "| --- | --- |", "| Pen | 3 |",
        "[Picture: Logo]", "[Chart: Sales]", "Notes:", "Say it slowly", "",
    ])


def test_grouped_shapes_are_read_where_the_group_is(tmp_path):
    def build(deck):
        slide = deck.slides.add_slide(deck.slide_layouts[6])
        group = slide.shapes.add_group_shape()
        for top, text in ((3, "In group, lower"), (1, "In group, upper")):
            group.shapes.add_textbox(SlideInches(1), SlideInches(top), SlideInches(2), SlideInches(1)).text_frame.text = text
        slide.shapes.add_textbox(SlideInches(1), SlideInches(5), SlideInches(2), SlideInches(1)).text_frame.text = "Outside"

    assert text_of(presentation(tmp_path / "group.pptx", build)) == "\n".join([
        "## Slide 1", "", "In group, upper", "In group, lower", "Outside", "",
    ])


def test_slides_follow_one_another(tmp_path):
    def build(deck):
        for title in ("One", "Two"):
            deck.slides.add_slide(deck.slide_layouts[5]).shapes.title.text = title

    assert text_of(presentation(tmp_path / "two.pptx", build)) == "## Slide 1: One\n\n## Slide 2: Two\n"


TOO_MANY = "At most 5 pages can be shown at once; ask for the others in another call."
BAD = 'pages must be page numbers or ranges, like "3", "3-5" or "1,4".'


def test_a_pdf_has_a_section_per_page(tmp_path):
    assert text_of(pdf(tmp_path / "doc.pdf", "Hello page one", "")) == "\n".join([
        "## Page 1", "", "Hello page one", "",
        "## Page 2", "", '[No text on this page: it may be a scan or a picture. To see it, read it as an image with pages="2".]', "",
    ])


def test_a_password_protected_pdf_is_refused(tmp_path):
    with pytest.raises(documents.DocumentError) as error:
        documents.convert(locked_pdf(tmp_path / "locked.pdf"))
    assert str(error.value) == "is password-protected, so it cannot be read."


@pytest.mark.parametrize("spec,expected", [
    ("3", [3]), ("2-4", [2, 3, 4]), ("1, 4", [1, 4]), ("1-2,7", [1, 2, 7]), ("4,4,2", [4, 2]), (" 5 ", [5]),
])
def test_page_numbers(spec, expected):
    assert documents.page_numbers(spec, 10) == expected


@pytest.mark.parametrize("spec,message", [
    ("", BAD), ("three", BAD), ("5-3", BAD), ("1;2", BAD),
    ("0", "Page 0 is out of range: the PDF has 10 pages."),
    ("9-11", "Page 11 is out of range: the PDF has 10 pages."),
    ("1-6", TOO_MANY),
])
def test_page_numbers_that_cannot_be_shown(spec, message):
    with pytest.raises(documents.PagesError) as error:
        documents.page_numbers(spec, 10)
    assert str(error.value) == message


def test_pages_are_shown_as_images_2048_on_their_long_side(tmp_path):
    total, shown = documents.show_pages(pdf(tmp_path / "doc.pdf", "one", "two", "three"), "3,1")
    assert total == 3 and [number for number, _ in shown] == [3, 1]
    assert all((stored.width, stored.height) == (1448, 2048) for _, stored in shown)
    assert all((images.root() / stored.file).exists() for _, stored in shown)


def test_a_small_page_is_rendered_at_300_dpi_at_most(tmp_path):
    _, [(_, stored)] = documents.show_pages(pdf(tmp_path / "card.pdf", "card", size=(144, 72)), "1")
    assert (stored.width, stored.height) == (600, 300)


def test_pages_of_a_password_protected_pdf_are_refused(tmp_path):
    with pytest.raises(documents.DocumentError):
        documents.show_pages(locked_pdf(tmp_path / "locked.pdf"), "1")


def test_a_sheet_that_states_its_range_wrong_is_read_whole(tmp_path):
    def build(book):
        book.active.append(["Name", "Qty"])
        book.active.append(["Pen", 3])
        book.active["D4"] = "far"

    assert text_of(with_dimension(workbook(tmp_path / "dimension.xlsx", build), "A1")) == "\n".join([
        "## Sheet 1: Sheet", "",
        "|   | A | B | C | D |",
        "| --- | --- | --- | --- | --- |",
        "| 1 | Name | Qty |  |  |",
        "| 2 | Pen | 3 |  |  |",
        "| 4 |  |  |  | far |", "",
    ])


def test_ink_on_a_slide_is_left_out(tmp_path):
    def build(deck):
        slide = deck.slides.add_slide(deck.slide_layouts[5])
        slide.shapes.title.text = "Annotated"
        slide.shapes.add_textbox(SlideInches(1), SlideInches(2), SlideInches(3), SlideInches(1)).text_frame.text = "Body"
        slide.shapes._spTree.append(OxmlElement("p:contentPart"))  # pen strokes, as PowerPoint saves them

    assert text_of(presentation(tmp_path / "ink.pptx", build)) == "## Slide 1: Annotated\n\nBody\n"


def test_text_boxes_in_a_table_are_left_out(tmp_path):
    box = (
        f'<w:r {nsdecls("w", "wp")} {MC} xmlns:v="urn:schemas-microsoft-com:vml"><mc:AlternateContent>'
        '<mc:Choice Requires="wps"><w:drawing><wp:anchor><wp:docPr id="7" name="Text Box 7"/>'
        '<w:txbxContent><w:p><w:r><w:t>BOX</w:t></w:r></w:p></w:txbxContent></wp:anchor></w:drawing></mc:Choice>'
        '<mc:Fallback><w:pict><v:shape><w:txbxContent><w:p><w:r><w:t>BOX</w:t></w:r></w:p></w:txbxContent>'
        '</v:shape></w:pict></mc:Fallback>'
        '</mc:AlternateContent></w:r>'
    )

    def build(document):
        cell = document.add_table(rows=1, cols=1).cell(0, 0)
        cell.text = "cell text"
        cell.paragraphs[0]._p.append(parse_xml(box))

    assert text_of(word(tmp_path / "table-box.docx", build)) == "| cell text |\n| --- |\n"
