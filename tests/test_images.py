"""Images prepared for the model and kept in the image store (kilo/images.py)."""

import base64
import os
import time

import pytest
from PIL import Image, UnidentifiedImageError

from kilo import images

pytestmark = pytest.mark.usefixtures("data_dir")
DAY = 86400


def saved(path, picture, **options):
    picture.save(path, **options)
    return path


def opened(info) -> Image.Image:
    return Image.open(images.root() / info.shown.file)


def test_a_photo_is_turned_upright(tmp_path):
    exif = Image.Exif()
    exif[0x0112] = 6  # the camera was turned: show it rotated 90° clockwise
    info = images.read(saved(tmp_path / "photo.jpg", Image.new("RGB", (40, 20), "red"), exif=exif))
    assert (info.width, info.height) == (20, 40)
    assert opened(info).size == (20, 40)


def test_a_large_image_is_shrunk_to_2048_on_its_long_side(tmp_path):
    info = images.read(saved(tmp_path / "wide.png", Image.new("RGB", (4096, 1024), "blue")))
    assert (info.width, info.height) == (4096, 1024)
    assert (info.shown.width, info.shown.height) == (2048, 512)
    assert opened(info).size == (2048, 512)


def test_a_small_image_keeps_its_size(tmp_path):
    info = images.read(saved(tmp_path / "small.png", Image.new("RGB", (300, 200), "blue")))
    assert (info.shown.width, info.shown.height) == (300, 200)


def test_everything_is_sent_as_jpeg(tmp_path):
    info = images.read(saved(tmp_path / "shot.png", Image.new("RGB", (30, 20), "blue")))
    assert info.type == "image/png"
    assert info.shown.file.endswith(".jpg") and opened(info).format == "JPEG"


def test_transparency_becomes_white(tmp_path):
    info = images.read(saved(tmp_path / "clear.png", Image.new("RGBA", (10, 10), (0, 0, 0, 0))))
    assert all(channel >= 250 for channel in opened(info).convert("RGB").getpixel((5, 5)))


def test_an_animation_shows_its_first_frame(tmp_path):
    frames = [Image.new("RGB", (20, 20), color) for color in ("red", "blue", "green")]
    path = tmp_path / "moving.gif"
    frames[0].save(path, save_all=True, append_images=frames[1:])
    info = images.read(path)
    assert info.frames == 3
    red, green, blue = opened(info).convert("RGB").getpixel((10, 10))
    assert red > 200 and green < 60 and blue < 60


def test_the_type_is_what_the_file_holds(tmp_path):
    assert images.read(saved(tmp_path / "named.jpg", Image.new("RGB", (8, 8)), format="PNG")).type == "image/png"


def test_the_same_image_is_stored_once(tmp_path):
    first = images.read(saved(tmp_path / "a.png", Image.new("RGB", (8, 8), "red")))
    second = images.read(saved(tmp_path / "b.png", Image.new("RGB", (8, 8), "red")))
    assert first.shown.file == second.shown.file
    assert [path.name for path in images.root().iterdir()] == [first.shown.file]


def test_images_unused_for_30_days_are_deleted():
    images.root().mkdir(parents=True)
    old, recent = images.root() / "old.jpg", images.root() / "recent.jpg"
    for path, days in ((old, 31), (recent, 29)):
        path.write_bytes(b"x")
        os.utime(path, (time.time() - days * DAY,) * 2)
    images.store(Image.new("RGB", (8, 8), "red"))
    assert not old.exists() and recent.exists()


def test_storing_an_image_again_keeps_it():
    first = images.store(Image.new("RGB", (8, 8), "red"))
    os.utime(images.root() / first.file, (time.time() - 31 * DAY,) * 2)
    images.store(Image.new("RGB", (8, 8), "red"))
    assert time.time() - (images.root() / first.file).stat().st_mtime < DAY


def test_a_stored_image_as_a_data_url():
    shown = images.store(Image.new("RGB", (8, 8), "red"))
    url = images.data_url(shown.file)
    assert url.startswith("data:image/jpeg;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == (images.root() / shown.file).read_bytes()


def test_a_missing_image_has_no_data_url():
    assert images.data_url("gone.jpg") is None


def test_only_files_in_the_store_are_sent(data_dir):
    (data_dir / "secret.jpg").write_bytes(b"x")
    images.root().mkdir(parents=True)
    assert images.data_url("../secret.jpg") is None


def test_a_broken_image_raises(tmp_path):
    (tmp_path / "broken.png").write_bytes(b"not an image")
    with pytest.raises(UnidentifiedImageError):
        images.read(tmp_path / "broken.png")


def test_a_large_image_is_shrunk_before_it_is_copied(tmp_path, monkeypatch):
    seen = []

    def watching(function):
        def watched(picture, *args, **kwargs):
            seen.append(picture.size)
            return function(picture, *args, **kwargs)
        return watched

    monkeypatch.setattr(images.ImageOps, "exif_transpose", watching(images.ImageOps.exif_transpose))
    monkeypatch.setattr(images, "_flatten", watching(images._flatten))
    for name, mode in (("big.png", "RGBA"), ("big.jpg", "RGB")):
        info = images.read(saved(tmp_path / name, Image.new(mode, (3000, 2000), "red")))
        assert (info.width, info.height, info.shown.width) == (3000, 2000, 2048)
    exif = Image.Exif()
    exif[0x0112] = 6
    info = images.read(saved(tmp_path / "turned.jpg", Image.new("RGB", (3000, 2000), "red"), exif=exif))
    assert (info.width, info.height, info.shown.height) == (2000, 3000, 2048)
    assert len(seen) == 6 and max(max(size) for size in seen) <= images.MAX_SIDE
