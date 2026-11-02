import json
import re
from pathlib import Path

import pytest

from kilo.config import settings
from kilo.tools.output_limit import clip_line, limit_output

LIMIT = 100_000


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "kilo_data_dir", tmp_path)


def saved_file(result: dict) -> Path:
    return Path(re.search(r"saved in (.+?\.txt)", result["output_truncated"]).group(1))


def test_short_result_keeps_non_ascii_unescaped():
    out = limit_output("t", json.dumps({"a": "學"}))
    assert "學" in out and "\\u" not in out


def test_long_text_shortened_and_saved():
    long_text = "x" * 150_000 + "the-end"
    result = json.loads(limit_output("shell", json.dumps({"status": "failed", "exit_code": 3, "stdout": long_text})))
    assert len(json.dumps(result, ensure_ascii=False)) <= LIMIT
    assert result["status"] == "failed" and result["exit_code"] == 3
    assert result["stdout"].endswith("the-end")
    assert "characters omitted" in result["stdout"]
    assert long_text in saved_file(result).read_text(encoding="utf-8")


def test_long_list_keeps_first_items():
    items = [f"item-{i}" for i in range(50_000)]
    result = json.loads(limit_output("grep", json.dumps({"status": "success", "matches": items})))
    assert "keeps its first" in result["output_truncated"]
    assert result["matches"] == items[: len(result["matches"])]
    assert 0 < len(result["matches"]) < len(items)


def test_non_json_result_becomes_json():
    result = json.loads(limit_output("t", "y" * 200_000))
    assert "output" in result and "output_truncated" in result


def test_quote_heavy_text_stays_within_the_limit():
    out = limit_output("t", '"\n' * 100_000)
    assert len(out) <= LIMIT


def test_clip_line_says_how_long_the_line_was():
    assert clip_line("a" * 2000, 10) == "a" * 10 + " … [line cut: 2,000 characters]"
    assert clip_line("short", 10) == "short"
