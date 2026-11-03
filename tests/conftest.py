"""
Shared fixtures. Settings are read when `kilo.config` is imported, from the environment and from `.env` in
the current directory, so this file imports them from an empty folder with no ANTHROPIC_, OPENROUTER_ or KILO_ variables:
nothing here can read the real `.env` or touch the real data directory or workspace.
"""

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_sandbox = Path(tempfile.mkdtemp(prefix="kilo-tests-"))
_start_dir = os.getcwd()
os.chdir(_sandbox)
for _name in [n for n in os.environ if n.startswith(("ANTHROPIC_", "KILO_", "OPENROUTER_"))]:
    del os.environ[_name]
os.environ["KILO_WORKSPACE"] = str(_sandbox / "ws")

import httpx  # noqa: E402
import pytest  # noqa: E402

from fakes import FakeClock, FakeModel  # noqa: E402
from kilo.assembly import Kilo, set_kilo  # noqa: E402
from kilo.auth import Logins  # noqa: E402
from kilo.config import settings  # noqa: E402
import kilo.line.config  # noqa: E402,F401
from kilo.server.app import app  # noqa: E402

os.chdir(_start_dir)

USERNAME, PASSWORD = "tester", "correct-horse"


@pytest.fixture(autouse=True)
def away_from_the_project(tmp_path, monkeypatch):
    """Kilo reads `.env` relative to the current directory at run time too (MCP header values)."""
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def workspace(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setattr(settings, "kilo_workspace", root)
    return root


@pytest.fixture
def model() -> FakeModel:
    return FakeModel()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 10, 28, 9, 0, tzinfo=timezone.utc))


@pytest.fixture
async def kilo(tmp_path, workspace, model, clock):
    built = Kilo(data_dir=tmp_path, clock=clock, chat_model=model, logins=Logins(USERNAME, PASSWORD, clock=clock))
    await built.init()
    yield built
    await built.shutdown()


@pytest.fixture
async def client(kilo):
    set_kilo(kilo)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as http:
        yield http
    set_kilo(None)


@pytest.fixture
async def authed(client):
    response = await client.post("/api/v1/auth/login", json={"username": USERNAME, "password": PASSWORD})
    assert response.status_code == 200
    return client
