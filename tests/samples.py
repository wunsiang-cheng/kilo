"""Files of the kinds read_file reads, made on the spot, so the repository holds no binary test files."""

import ctypes
import io
import re
import zipfile
from pathlib import Path
from typing import Any, Callable

import docx
import openpyxl
import pptx
import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
from PIL import Image
from pypdf import PdfWriter


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


def _rewrite_sheet(path: Path, change: Callable[[bytes], bytes]) -> Path:
    """The workbook with its first sheet's XML changed by `change` (which must change it)."""
    with zipfile.ZipFile(path) as source:
        entries = {name: source.read(name) for name in source.namelist()}
    sheet = "xl/worksheets/sheet1.xml"
    before = entries[sheet]
    entries[sheet] = change(before)
    assert entries[sheet] != before, f"{path.name}: the sheet was not changed"
    with zipfile.ZipFile(path, "w") as target:
        for name, data in entries.items():
            target.writestr(name, data)
    return path


def with_saved_value(path: Path, formula: str, value: str) -> Path:
    """The workbook with `value` saved after `formula`, as Excel saves it: openpyxl saves formulas without values."""
    def change(sheet: bytes) -> bytes:
        for empty in ("<v />", "<v></v>"):  # as ElementTree writes it, and as lxml does (openpyxl uses it when installed)
            sheet = sheet.replace(f"<f>{formula}</f>{empty}".encode(), f"<f>{formula}</f><v>{value}</v>".encode())
        return sheet

    return _rewrite_sheet(path, change)


def with_dimension(path: Path, ref: str) -> Path:
    """The workbook with the range its first sheet says it uses set to `ref`, as some programs write it wrong."""
    return _rewrite_sheet(path, lambda sheet: re.sub(rb'<dimension ref="[^"]*"\s*/>', f'<dimension ref="{ref}"/>'.encode(), sheet))


def presentation(path: Path, build: Callable[[Any], Any]) -> Path:
    """A PowerPoint presentation that `build` fills (it gets a python-pptx Presentation)."""
    deck = pptx.Presentation()
    build(deck)
    deck.save(str(path))
    return path


def pdf(path: Path, *texts: str, size=(595, 842)) -> Path:
    """A PDF with a page per text, in Helvetica; an empty text makes a page with no text at all, as a scan has."""
    document = pdfium.PdfDocument.new()
    for text in texts:
        page = document.new_page(*size)
        if text:
            font = pdfium_c.FPDFText_LoadStandardFont(document.raw, b"Helvetica")
            line = pdfium_c.FPDFPageObj_CreateTextObj(document.raw, font, 24)
            pdfium_c.FPDFText_SetText(line, (ctypes.c_ushort * (len(text) + 1))(*map(ord, text), 0))
            pdfium_c.FPDFPageObj_Transform(line, 1, 0, 0, 1, 36, size[1] - 60)
            pdfium_c.FPDFPage_InsertObject(page.raw, line)
            page.gen_content()
    with open(path, "wb") as file:
        document.save(file)
    return path


def locked_pdf(path: Path) -> Path:
    """A PDF that opens only with a password."""
    writer = PdfWriter()
    writer.add_blank_page(200, 200)
    writer.encrypt("secret", algorithm="RC4-128")
    with open(path, "wb") as file:
        writer.write(file)
    return path
