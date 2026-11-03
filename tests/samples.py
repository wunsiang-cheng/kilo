"""Files of the kinds read_file reads, made on the spot, so the repository holds no binary test files."""

import io
from pathlib import Path
from typing import Any, Callable

import docx
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
