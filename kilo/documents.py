"""
Documents the agent reads (Word, Excel, PowerPoint, PDF), converted to Markdown text and kept in the converted store
(<KILO_DATA_DIR>/converted/), once per content: read_file numbers and pages the text like a text file's, and grep
can search it whole.
"""

import datetime as dt
import hashlib
import os
import re
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence

import docx
import docx.table
import docx.text.paragraph
import openpyxl
from openpyxl.utils import get_column_letter

from kilo.config import settings

STORE = "converted"
MAX_CHARS = 10_000_000  # a document's text stops here
MAX_UNZIPPED_BYTES = 1024**3  # an Office file larger than this unzipped is not opened (a zip bomb, or near enough)
KEEP_DAYS = 30  # converted texts unused for longer are deleted; reading the document again makes its text again
CONVERTER_VERSION = 1  # part of each text's name: a new version converts every document anew

# Each completes the sentence "'<path>' ..." (read_file says which file).
OLE_CONTAINER = "is password-protected or in an old Office format, so it cannot be read."
TOO_LARGE_UNZIPPED = "would take more than 1 GB unzipped, so it was not read."
STOPPED = "[Conversion stopped here: the text passed {limit:,} characters. The rest of the file is not included.]"

# An encrypted Office file is an OLE compound file, as the old binary formats are, not a zip.
OLE_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
PIC = "{http://schemas.openxmlformats.org/drawingml/2006/picture}"
MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
# Not text of the paragraph: its properties (tab stops are w:tab too), and drawings and text boxes, which Word stores
# twice (a modern copy and a fallback).
NOT_TEXT = frozenset({W + "pPr", W + "rPr", W + "drawing", W + "pict", MC + "AlternateContent"})


class DocumentError(Exception):
    """A document that cannot be read, for a reason the model is told; the message completes "'<path>' ..."."""


@dataclass(frozen=True)
class Kind:
    """A kind of document read_file converts."""

    name: str  # as the result's converted_from says it: "docx"
    described: str  # for errors: "a Word document"
    lines: Callable[[Path], Iterator[str]]  # the document's Markdown, line by line
    zipped: bool  # an Office Open XML file (a zip): checked for an OLE container and for its size unzipped


def root() -> Path:
    return settings.kilo_data_dir / STORE


def _table(rows: Sequence[Sequence[str]]) -> List[str]:
    """Rows as a Markdown table, the first as its header; | and line breaks in a cell are written so they stay in it."""
    if not rows:
        return []
    width = max(len(row) for row in rows)

    def line(row: Sequence[str]) -> str:
        cells = [cell.replace("|", "\\|").replace("\r\n", "\n").replace("\n", "<br>") for cell in row]
        return "| " + " | ".join(cells + [""] * (width - len(cells))) + " |"

    return [line(rows[0]), "| " + " | ".join(["---"] * width) + " |", *(line(row) for row in rows[1:])]


def _capped(lines: Iterable[str]) -> str:
    """The lines joined, up to MAX_CHARS characters; past it the rest is left out, and a line says so."""
    kept: List[str] = []
    size = 0
    for line in lines:
        size += len(line) + 1
        if size > MAX_CHARS:
            kept += [STOPPED.format(limit=MAX_CHARS), ""]
            break
        kept.append(line)
    return "\n".join(kept)


def _sweep(folder: Path) -> None:
    """Delete files not used for KEEP_DAYS days; another process may be deleting them at the same time."""
    cutoff = time.time() - KEEP_DAYS * 86400
    for path in folder.iterdir():
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
        except OSError:
            continue


def _check_zip(path: Path) -> None:
    """Refuse an Office file that is an OLE container or too large unzipped; a broken zip raises as zipfile does."""
    with open(path, "rb") as file:
        if file.read(len(OLE_MAGIC)) == OLE_MAGIC:
            raise DocumentError(OLE_CONTAINER)
    with zipfile.ZipFile(path) as archive:
        if sum(info.file_size for info in archive.infolist()) > MAX_UNZIPPED_BYTES:
            raise DocumentError(TOO_LARGE_UNZIPPED)


def convert(path: Path) -> Path:
    """
    The document's text as Markdown, in a file in the converted store, made once per content (and converter version).
    Raises DocumentError for a document that cannot be read for a reason the model is told; other failures (a broken
    file) raise as the library reading it raised them.
    """
    kind = kind_of(path)
    if kind is None:
        raise ValueError(f"Not a document read_file converts: {path.name}")
    digest = hashlib.sha256(f"{CONVERTER_VERSION}\n".encode())
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(1 << 20), b""):
            digest.update(chunk)
    folder = root()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{digest.hexdigest()}.md"
    if target.exists():
        os.utime(target)
        return target
    if kind.zipped:
        _check_zip(path)
    text = _capped(kind.lines(path))
    temporary = folder / f".{target.name}.{uuid.uuid4().hex}.tmp"
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, target)  # another session reading it meanwhile never finds half a text
    _sweep(folder)
    return target


# Word


def _word_text(element: Any) -> str:
    """The text of a paragraph (or anything in one): its runs, links and insertions, not its deletions or drawings."""
    parts: List[str] = []

    def walk(node: Any) -> None:
        for child in node:
            if child.tag in NOT_TEXT:
                continue
            if child.tag == W + "t":
                parts.append(child.text or "")
            elif child.tag == W + "tab":
                parts.append("\t")
            elif child.tag in (W + "br", W + "cr"):
                parts.append("\n")
            else:
                walk(child)

    walk(element)
    return "".join(parts).strip()


def _word_prefix(element: Any, document: Any) -> str:
    """What marks a paragraph in Markdown: # for a title or heading, - (indented by level) for a list item."""
    style = docx.text.paragraph.Paragraph(element, document).style
    name = (style.name if style is not None else None) or ""
    if name == "Title":
        return "# "
    heading = re.fullmatch(r"Heading ([1-6])", name)
    if heading:
        return "#" * int(heading[1]) + " "
    numbering = element.find(f"{W}pPr/{W}numPr")
    if numbering is None and not name.startswith("List"):
        return ""
    level_element = numbering.find(f"{W}ilvl") if numbering is not None else None
    if level_element is not None:
        level = int(level_element.get(W + "val", "0")) + 1
    else:
        trailing = re.search(r"(\d+)$", name)
        level = int(trailing[1]) if trailing else 1
    return "  " * (level - 1) + "- "


def _word_pictures(element: Any) -> List[str]:
    """A line for each picture in a paragraph (a drawing holding a pic:pic; a text box holds none)."""
    lines = []
    for drawing in element.iter(WP + "inline", WP + "anchor"):
        if next(drawing.iter(PIC + "pic"), None) is None:
            continue
        properties = drawing.find(WP + "docPr")
        description = properties.get("descr") if properties is not None else None
        lines.append(f"[Image: {description}]" if description else "[Image]")
    return lines


def _word_cell(cell: Any) -> str:
    """A table cell's text: its paragraphs (a nested table's included), one per line."""
    return "\n".join(text for paragraph in cell._tc.iter(W + "p") if (text := _word_text(paragraph)))


def _word_blocks(parent: Any) -> Iterator[Any]:
    """The paragraphs and tables of a body in order, those in content controls (a table of contents) included."""
    for child in parent.iterchildren():
        if child.tag in (W + "p", W + "tbl"):
            yield child
        elif child.tag == W + "sdt":
            content = child.find(W + "sdtContent")
            if content is not None:
                yield from _word_blocks(content)


def _docx_lines(path: Path) -> Iterator[str]:
    document = docx.Document(str(path))
    for element in _word_blocks(document.element.body):
        if element.tag == W + "tbl":
            block = _table([[_word_cell(cell) for cell in row.cells] for row in docx.table.Table(element, document).rows])
        else:
            text = _word_text(element)
            block = ([_word_prefix(element, document) + text] if text else []) + _word_pictures(element)
        if block:
            yield from block
            yield ""


# Excel


def _sheet_value(value: Any) -> str:
    """A cell's value as text: dates and times in ISO form, booleans as Excel shows them, nothing for an empty cell."""
    if value is None:
        return ""
    if isinstance(value, bool):  # before numbers: a bool is an int
        return "TRUE" if value else "FALSE"
    if isinstance(value, dt.datetime):
        return value.date().isoformat() if value.time() == dt.time() else value.isoformat(sep=" ")
    if isinstance(value, (dt.date, dt.time)):
        return value.isoformat()
    return str(value)


def _xlsx_lines(path: Path) -> Iterator[str]:
    """
    Each sheet as a table headed by its column letters, each row with its number, so cells keep their addresses.
    Formulas show the value saved with them. Rows stop being read once the text passes MAX_CHARS (convert stops
    there anyway), so a huge sheet is never held whole.
    """
    book = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    try:
        gathered = 0
        for number, sheet in enumerate(book.worksheets, 1):
            hidden = " (hidden)" if sheet.sheet_state != "visible" else ""
            yield f"## Sheet {number}: {sheet.title}{hidden}"
            yield ""
            rows: List[List[str]] = []
            width = 0
            # Rows from 1 and cells from column A, the empty ones before data filled in: positions are addresses.
            for row_number, values in enumerate(sheet.iter_rows(values_only=True), 1):
                if gathered > MAX_CHARS:
                    break
                cells = [_sheet_value(value) for value in values]
                filled = [column for column, cell in enumerate(cells, 1) if cell]
                if not filled:
                    continue
                width = max(width, filled[-1])
                rows.append([str(row_number), *cells])
                gathered += sum(len(cell) + 3 for cell in cells)
            if rows:
                header = [" ", *(get_column_letter(column) for column in range(1, width + 1))]
                yield from _table([header, *(row[: width + 1] for row in rows)])
            else:
                yield "(empty)"
            yield ""
            if gathered > MAX_CHARS:
                return
    finally:
        book.close()


KINDS: Dict[str, Kind] = {
    ".docx": Kind("docx", "a Word document", _docx_lines, zipped=True),
    ".xlsx": Kind("xlsx", "an Excel workbook", _xlsx_lines, zipped=True),
}


def kind_of(path: Path) -> Optional[Kind]:
    """The kind of document a file is, by its extension in any case; None when read_file does not convert it."""
    return KINDS.get(path.suffix.lower())
