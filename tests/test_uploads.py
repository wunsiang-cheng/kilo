"""Saving the files users send, and describing them to the model (kilo/uploads.py)."""

import asyncio
from datetime import datetime, timezone

import pytest

from kilo import uploads
from kilo.config import settings
from kilo.models.session import Attachment

NOW = datetime(2026, 11, 3, 16, 30, tzinfo=timezone.utc)  # already 2026-11-04 in Asia/Taipei
DAY = "uploads/2026-11-04"


async def chunks(*parts: bytes):
    for part in parts:
        yield part


async def save(data: bytes = b"hello", name: str = "notes.txt", declared=None) -> Attachment:
    return await uploads.save(chunks(data), name, declared, now=NOW)


async def test_a_file_is_saved_in_the_day_folder(workspace):
    attachment = await save(b"hello", "notes.txt")
    assert attachment == Attachment(path=f"{DAY}/notes.txt", name="notes.txt", type="text/plain", size=5)
    assert (workspace / DAY / "notes.txt").read_bytes() == b"hello"


async def test_the_name_sent_is_kept(workspace):
    saved = await save(name="../a;b.txt")
    assert saved.path == f"{DAY}/a_b.txt" and saved.name == "../a;b.txt"
    assert len((await save(name="x" * 300 + ".txt")).name) == 255


async def test_the_same_name_gets_a_number(workspace):
    paths = [(await save(name="notes.txt")).path for _ in range(3)]
    assert paths == [f"{DAY}/notes.txt", f"{DAY}/notes-2.txt", f"{DAY}/notes-3.txt"]


async def test_names_taken_at_the_same_moment_stay_distinct(workspace):
    saved = await asyncio.gather(*(save(bytes([i]), "same.bin") for i in range(5)))
    assert len({a.path for a in saved}) == 5
    assert sorted((workspace / a.path).read_bytes() for a in saved) == [bytes([i]) for i in range(5)]


async def test_a_numbered_long_name_stays_within_255_bytes(workspace):
    long = "報" * 100 + ".pdf"
    await save(name=long)
    second = await save(name=long)
    assert second.path.endswith("-2.pdf")
    assert len(second.path.rsplit("/", 1)[1].encode()) <= 255


async def test_over_the_limit_nothing_is_left(workspace, monkeypatch):
    monkeypatch.setattr(settings, "kilo_upload_max_mb", 1)
    with pytest.raises(uploads.UploadTooLarge):
        await uploads.save(chunks(b"x" * 1024 * 1024, b"y"), "big.bin", now=NOW)
    assert list((workspace / DAY).iterdir()) == []


async def test_exactly_the_limit_is_accepted(workspace, monkeypatch):
    monkeypatch.setattr(settings, "kilo_upload_max_mb", 1)
    assert (await uploads.save(chunks(b"x" * 1024 * 1024), "full.bin", now=NOW)).size == 1024 * 1024


async def test_a_source_that_fails_leaves_nothing(workspace):
    async def broken():
        yield b"part"
        raise ConnectionError("gone")

    with pytest.raises(ConnectionError):
        await uploads.save(broken(), "cut.bin", now=NOW)
    assert list((workspace / DAY).iterdir()) == []


@pytest.mark.parametrize("sent,saved", [
    ("report.pdf", "report.pdf"),
    ("報告 (1).pdf", "報告 (1).pdf"),
    ("../../etc/passwd", "passwd"),
    ("C:\\Users\\ann\\photo.jpg", "photo.jpg"),
    ("a;rm -rf ~.txt", "a_rm -rf _.txt"),
    ("$(id)`x`.sh", "_(id)_x_.sh"),
    ("tab\there\nnew.txt", "tab_here_new.txt"),
    ("Tom's \"notes\".txt", "Tom_s _notes_.txt"),
    (".env", "env"),
    ("-rf", "rf"),
    ("  .. -x.txt  ", "x.txt"),
    ("", "file"),
    ("...", "file"),
    ("dir/", "file"),
    ("e\u0301te\u0301.txt", "\u00e9t\u00e9.txt"),
])
def test_safe_name(sent, saved):
    assert uploads.safe_name(sent) == saved


def test_a_long_name_is_cut_to_255_bytes_keeping_its_extension():
    name = uploads.safe_name("報" * 100 + ".pdf")
    assert name == "報" * 83 + ".pdf" and len(name.encode()) == 253


def test_a_long_extension_is_not_kept_whole():
    name = uploads.safe_name("a." + "x" * 300)
    assert name == "a." + "x" * 253 and len(name.encode()) == 255


@pytest.mark.parametrize("name,declared,expected", [
    ("data.csv", "application/vnd.ms-excel", "text/csv"),
    ("README", "text/plain; charset=utf-8", "text/plain"),
    ("blob", "Image/PNG", "image/png"),
    ("blob", "application/octet-stream", "application/octet-stream"),
    ("blob", None, "application/octet-stream"),
    ("blob", "", "application/octet-stream"),
])
def test_type_of(name, declared, expected):
    assert uploads.type_of(name, declared) == expected


async def test_an_attachment_is_found_again_by_its_path(workspace):
    saved = await save(b"hello", "notes.txt")
    assert uploads.attachment_at(saved.path, "notes.txt", "text/plain") == saved
    assert uploads.attachment_at(str(workspace / saved.path)).path == saved.path
    assert uploads.attachment_at(f"uploads/../{saved.path}").path == saved.path
    assert uploads.attachment_at(saved.path).name == "notes.txt"


async def test_the_size_comes_from_the_disk(workspace):
    saved = await save(b"hello", "notes.txt")
    (workspace / saved.path).write_bytes(b"longer now")
    assert uploads.attachment_at(saved.path).size == 10


@pytest.mark.parametrize("path", ["notes.txt", "uploads", f"{DAY}/missing.txt", "../outside.txt", "/etc/passwd"])
def test_only_files_in_uploads_can_be_attached(workspace, path):
    (workspace / "notes.txt").write_text("x")
    (workspace / DAY).mkdir(parents=True)
    with pytest.raises(ValueError, match=rf"^Attachment '{path}' is not a file in uploads/\.$"):
        uploads.attachment_at(path)


def test_a_link_out_of_uploads_cannot_be_attached(workspace, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("x")
    (workspace / DAY).mkdir(parents=True)
    (workspace / DAY / "link.txt").symlink_to(secret)
    with pytest.raises(ValueError):
        uploads.attachment_at(f"{DAY}/link.txt")


@pytest.mark.parametrize("size,text", [
    (0, "0 bytes"), (1023, "1023 bytes"), (1024, "1.0 KB"), (2_400_000, "2.3 MB"), (3 * 1024**3, "3.0 GB"),
])
def test_format_size(size, text):
    assert uploads.format_size(size) == text


REPORT = Attachment(path="uploads/2026-11-03/report.pdf", name="report.pdf", type="application/pdf", size=2_400_000)
RENAMED = Attachment(path="uploads/2026-11-03/report-2.pdf", name="report.pdf", type="application/pdf", size=1_150_000)


def test_note_lists_each_file():
    assert uploads.note([REPORT, RENAMED]) == (
        "[Files sent with this message, saved in the workspace. Their contents are not included here; "
        "read them with your tools when you need them.\n"
        "- uploads/2026-11-03/report.pdf (application/pdf, 2.3 MB)\n"
        '- uploads/2026-11-03/report-2.pdf (application/pdf, 1.1 MB, sent as "report.pdf")]'
    )


def test_an_original_name_cannot_break_the_note():
    odd = Attachment(path="uploads/2026-11-03/a_b.txt", name='a"]\nb.txt', type="text/plain", size=1)
    assert uploads.note([odd]).splitlines()[1] == '- uploads/2026-11-03/a_b.txt (text/plain, 1 bytes, sent as "a\\"]\\nb.txt")]'


def test_with_note():
    assert uploads.with_note("look", [REPORT]) == f"look\n\n{uploads.note([REPORT])}"
    assert uploads.with_note("  ", [REPORT]) == uploads.note([REPORT])
    assert uploads.with_note("look", []) == "look"
