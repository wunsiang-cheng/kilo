"""Settings: defaults stay what .env.example says, and bad values are refused."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

import kilo
from kilo.config import Settings, settings
from kilo.line.config import LineSettings
from kilo.server.routes.tasks import TaskRunRequest
from kilo.tools.shell import SHELL_TOOL_DEFINITION, execute_shell_tool

ROOT = Path(__file__).resolve().parent.parent


def example_values() -> dict:
    """The uncommented settings in .env.example."""
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    return dict(m.groups() for m in map(re.compile(r"^([A-Z_]+)=(.*)$").match, lines) if m)


def test_example_is_parsed():
    assert len(example_values()) >= 15


@pytest.mark.parametrize("key,value", example_values().items())
def test_default_matches_env_example(key, value):
    source = LineSettings if key.startswith("KILO_LINE_") else Settings
    actual = getattr(source(_env_file=None), key.lower())
    if key == "KILO_WORKSPACE":
        return  # the test run sets this one, away from the real workspace
    if isinstance(actual, Path):
        assert actual == Path(value).expanduser().resolve()
    else:
        assert str(actual) == value


def test_tests_cannot_see_the_real_env_file():
    assert not Path(".env").exists()


def test_shell_schema_states_the_configured_timeout():
    timeout = SHELL_TOOL_DEFINITION["parameters"]["properties"]["timeout"]
    assert timeout["default"] == settings.kilo_default_timeout
    assert str(settings.kilo_default_timeout) in timeout["description"]


def test_shell_schema_follows_the_setting():
    code = (
        "from kilo.tools.shell import SHELL_TOOL_DEFINITION as d\n"
        "print(d['parameters']['properties']['timeout']['default'])"
    )
    package_root = str(Path(kilo.__file__).resolve().parent.parent)  # this copy of Kilo, whatever is installed
    env = {**os.environ, "KILO_DEFAULT_TIMEOUT": "45", "PYTHONPATH": package_root}
    run = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True)
    assert run.stdout.strip() == "45"


class RecordingTaskManager:
    async def run(self, command, timeout=None):
        from kilo.models.task import ShellResult

        self.timeout = timeout
        return ShellResult(action="run", status="completed", command=command)


async def test_bad_timeout_falls_back_to_the_configured_default():
    manager = RecordingTaskManager()
    result = json.loads(await execute_shell_tool(manager, {"command": "x", "timeout": "soon"}))
    assert result["status"] == "completed"
    assert manager.timeout is None  # run takes None as KILO_DEFAULT_TIMEOUT


def test_rest_run_request_has_no_fixed_timeout():
    assert TaskRunRequest(command="x").timeout is None


def test_compaction_threshold_outside_range_is_refused():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, kilo_compact_at_tokens=9_999)
    assert Settings(_env_file=None, kilo_compact_at_tokens=0).kilo_compact_at_tokens == 0


def test_compaction_threshold_has_no_ceiling():
    assert Settings(_env_file=None, kilo_compact_at_tokens=5_000_000).kilo_compact_at_tokens == 5_000_000


def test_the_only_model_settings_are_openrouters():
    model_settings = {name for name in Settings.model_fields if not name.startswith("kilo_")}
    assert model_settings == {"openrouter_api_key", "openrouter_model", "openrouter_reasoning_effort", "openrouter_max_tokens"}


def test_tool_output_limit_has_a_floor():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, kilo_tool_output_max_chars=9_999)


@pytest.mark.parametrize("name,value", [
    ("kilo_password", ""), ("kilo_password", "   "), ("kilo_username", ""),
    ("kilo_timezone", "Mars/Base"), ("kilo_timezone", "Asia"),
    ("kilo_port", 0), ("kilo_port", 70_000),
    ("kilo_compact_max_tokens", 0), ("kilo_compact_max_tokens", 64_001),
    ("kilo_default_timeout", 0), ("kilo_default_timeout", -1),
    ("kilo_job_catchup_minutes", -1),
    ("openrouter_reasoning_effort", "bogus"), ("openrouter_max_tokens", 0),
])
def test_unusable_value_is_refused(name, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{name: value})


@pytest.mark.parametrize("name,value", [
    ("kilo_port", 1), ("kilo_port", 65_535), ("kilo_compact_max_tokens", 64_000),
    ("kilo_job_catchup_minutes", 0), ("openrouter_max_tokens", 1),
])
def test_values_at_the_limits_are_accepted(name, value):
    assert getattr(Settings(_env_file=None, **{name: value}), name) == value


def test_openrouter_defaults():
    defaults = Settings(_env_file=None)
    assert defaults.openrouter_model == "meta/muse-spark-1.3-contributor"
    assert defaults.openrouter_reasoning_effort == "high"
    assert defaults.openrouter_api_key is None and defaults.openrouter_max_tokens is None


def test_line_settings_refuse_an_unknown_timezone():
    with pytest.raises(ValidationError):
        LineSettings(_env_file=None, kilo_timezone="Mars/Base")


@pytest.mark.parametrize("name", ["kilo_data_dir", "kilo_timezone"])
def test_settings_kept_in_both_classes_share_their_default(name):
    assert getattr(Settings(_env_file=None), name) == getattr(LineSettings(_env_file=None), name)


def test_mcp_config_defaults_to_the_data_dir(tmp_path):
    assert Settings(_env_file=None, kilo_data_dir=tmp_path).kilo_mcp_config == tmp_path.resolve() / "mcp_servers.json"


def test_explicit_mcp_config_stays(tmp_path):
    chosen = tmp_path / "elsewhere.json"
    moved = Settings(_env_file=None, kilo_data_dir=tmp_path / "data", kilo_mcp_config=chosen)
    assert moved.kilo_mcp_config == chosen.resolve()


def test_line_accounts_default_to_the_data_dir(tmp_path):
    moved = LineSettings(_env_file=None, kilo_data_dir=tmp_path)
    assert moved.kilo_line_accounts == tmp_path.resolve() / "line_accounts.json"


def test_explicit_line_accounts_stay(tmp_path):
    chosen = tmp_path / "elsewhere.json"
    moved = LineSettings(_env_file=None, kilo_data_dir=tmp_path / "data", kilo_line_accounts=chosen)
    assert moved.kilo_line_accounts == chosen.resolve()


def test_default_paths_are_the_documented_ones():
    assert Settings(_env_file=None).kilo_mcp_config == Path(".kilo_data/mcp_servers.json").resolve()
    assert LineSettings(_env_file=None).kilo_line_accounts == Path(".kilo_data/line_accounts.json").resolve()
