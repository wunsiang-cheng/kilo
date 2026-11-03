"""Files of the kinds read_file reads, made on the spot, so the repository holds no binary test files."""

import io
import zipfile
from pathlib import Path
from typing import Any, Callable

import docx
import openpyxl
import pptx
from PIL import Image


def png_bytes(color: str = "red") -> io.BytesIO:
    buffer = io.BytesIO()
    Image.new("RGB", (40, 30), color).save(buffer, "PNG")
    buffer.seek(0)
    return buffer


def word(path: Path, build: Callable[[Any], Any]) -> Path:
    """A Word document that `build` fills (it gets a python-docx Document)."""
    document = docx.Document()
    build(document)
    document.save(str(path))
    return path


def workbook(path: Path, build: Callable[[Any], Any]) -> Path:
    """An Excel workbook that `build` fills (it gets an openpyxl Workbook)."""
    book = openpyxl.Workbook()
    build(book)
    book.save(str(path))
    return path


def with_saved_value(path: Path, formula: str, value: str) -> Path:
    """The workbook with `value` saved after `formula`, as Excel saves it: openpyxl saves formulas without values."""
    with zipfile.ZipFile(path) as source:
        entries = {name: source.read(name) for name in source.namelist()}
    sheet = "xl/worksheets/sheet1.xml"
    before = entries[sheet]
    for empty in ("<v />", "<v></v>"):  # as ElementTree writes it, and as lxml does (openpyxl uses it when installed)
        entries[sheet] = entries[sheet].replace(f"<f>{formula}</f>{empty}".encode(), f"<f>{formula}</f><v>{value}</v>".encode())
    assert entries[sheet] != before, f"no formula {formula} without a value in {path.name}"
    with zipfile.ZipFile(path, "w") as target:
        for name, data in entries.items():
            target.writestr(name, data)
    return path


def presentation(path: Path, build: Callable[[Any], Any]) -> Path:
    """A PowerPoint presentation that `build` fills (it gets a python-pptx Presentation)."""
    deck = pptx.Presentation()
    build(deck)
    deck.save(str(path))
    return path
