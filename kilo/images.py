"""
Images the agent reads, prepared for the model and kept in the image store (<KILO_DATA_DIR>/images/): turned
upright, flattened onto white, shrunk to MAX_SIDE on their long side and saved as JPEG, once per content. A request
sends them as data: URLs (see the runner), so the conversation stores only their names.
"""

import base64
import hashlib
import io
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PIL import Image, ImageOps

from kilo.config import settings

STORE = "images"
MAX_SIDE = 2048  # providers shrink larger images to about this anyway (measured 2026-11-03: same tokens at 3000 px)
JPEG_QUALITY = 85
KEEP_DAYS = 30  # stored images unused for longer are deleted; a conversation that still shows one says it is gone
SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"})


@dataclass(frozen=True)
class Stored:
    """An image in the store: its file name there, and its size as the model sees it."""

    file: str
    width: int
    height: int


@dataclass(frozen=True)
class ImageInfo:
    """An image file as read: its type, its upright size, how many frames it has, and what was stored of it."""

    type: str
    width: int
    height: int
    frames: int
    shown: Stored


def root() -> Path:
    return settings.kilo_data_dir / STORE


def _flatten(picture: Image.Image) -> Image.Image:
    """The image in RGB, with what was transparent made white (a model may see transparency as black)."""
    if picture.mode in ("RGBA", "LA", "PA") or "transparency" in picture.info:
        rgba = picture.convert("RGBA")
        picture = Image.alpha_composite(Image.new("RGBA", rgba.size, (255, 255, 255, 255)), rgba)
    return picture.convert("RGB")


def _sweep(folder: Path) -> None:
    """Delete files not used for KEEP_DAYS days; another process may be deleting them at the same time."""
    cutoff = time.time() - KEEP_DAYS * 86400
    for path in folder.iterdir():
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
        except OSError:
            continue


def store(picture: Image.Image) -> Stored:
    """Keep an image as the model is sent it: flattened, at most MAX_SIDE on its long side, as JPEG, once per content."""
    picture = _flatten(picture)
    if max(picture.size) > MAX_SIDE:
        picture.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)  # keeps the ratio; never enlarges
    buffer = io.BytesIO()
    picture.save(buffer, "JPEG", quality=JPEG_QUALITY)
    data = buffer.getvalue()
    name = hashlib.sha256(data).hexdigest() + ".jpg"
    folder = root()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    if path.exists():
        os.utime(path)
    else:
        temporary = folder / f".{name}.{uuid.uuid4().hex}.tmp"
        temporary.write_bytes(data)
        os.replace(temporary, path)  # a request reading it meanwhile never finds half a file
    _sweep(folder)
    return Stored(name, picture.width, picture.height)


def read(path: Path) -> ImageInfo:
    """
    Open an image file, take its first frame, turn it upright as its EXIF data says, and store it for the model.
    Pillow's errors (not an image, too many pixels, unreadable) are raised as they are.
    """
    with Image.open(path) as picture:
        kind = Image.MIME.get(picture.format or "", "application/octet-stream")
        frames = getattr(picture, "n_frames", 1)
        picture.seek(0)
        upright = ImageOps.exif_transpose(picture)
        return ImageInfo(kind, upright.width, upright.height, frames, store(upright))


def data_url(file: str) -> Optional[str]:
    """A stored image as a data: URL, or None when it is no longer in the store. Only the store is ever read."""
    try:
        data = (root() / Path(file).name).read_bytes()
    except OSError:
        return None
    return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")
