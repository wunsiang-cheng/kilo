"""
Real Claude API calls, with the key from the project's `.env`. They cost a little, so they run only when asked:

    uv run pytest -m live
"""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import anthropic
import httpx
import pytest
from dotenv import dotenv_values

from kilo.assembly import Kilo
from kilo.claude import ClaudeClient
from kilo.config import DEFAULT_PASSWORD, DEFAULT_USERNAME

pytestmark = pytest.mark.live

ROOT = Path(__file__).resolve().parent.parent.parent
MODEL = "claude-sonnet-5-5"


@pytest.fixture(scope="module")
def api_key() -> str:
    key = dotenv_values(ROOT / ".env").get("ANTHROPIC_API_KEY")
    if not key:
        pytest.fail("Set ANTHROPIC_API_KEY in .env to run the live tests.")
    return key


@pytest.fixture
async def live_kilo(tmp_path, workspace, api_key):
    client = ClaudeClient(
        model=MODEL, effort="low", max_tokens=2048, client=anthropic.AsyncAnthropic(api_key=api_key),
    )
    built = Kilo(data_dir=tmp_path, chat_model=client)
    await built.init()
    yield built
    await built.shutdown()


async def chat(kilo, session_id, text):
    events = [event async for event in kilo.runner.run_chat_stream(session_id, text)]
    assert events[-1]["event"] == "done", events[-1]
    return events


async def test_real_reply(live_kilo):
    session = await live_kilo.db.create_session()
    events = await chat(live_kilo, session.id, "Reply with exactly the word PINEAPPLE and nothing else.")
    assert "PINEAPPLE" in events[-1]["data"]["content"]


async def test_real_tool_loop(live_kilo, workspace):
    session = await live_kilo.db.create_session()
    events = await chat(
        live_kilo, session.id,
        "Use the write_file tool to write exactly the text MANGO to the file hello.txt. Then reply DONE.",
    )
    assert "tool_call_result" in [event["event"] for event in events]
    assert (workspace / "hello.txt").read_text().strip() == "MANGO"


async def test_real_follow_up_uses_history(live_kilo):
    session = await live_kilo.db.create_session()
    await chat(live_kilo, session.id, "My code word is ZEBRA-42. Reply only OK.")
    events = await chat(live_kilo, session.id, "What is my code word? Reply with it only.")
    assert "ZEBRA-42" in events[-1]["data"]["content"]


async def test_real_manual_compaction(live_kilo):
    session = await live_kilo.db.create_session()
    await chat(live_kilo, session.id, "My code word is LYCHEE-7. Reply only OK.")
    outcome = await live_kilo.runner.compact(session.id)
    assert outcome["event"] == "compaction_done", outcome
    events = await chat(live_kilo, session.id, "What is my code word? Reply with it only.")
    assert "LYCHEE-7" in events[-1]["data"]["content"]


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def real_server(tmp_path, api_key):
    """`kilo-server`'s entry point in a process of its own, with the defaults of .env.example for the model."""
    port = free_port()
    env = {
        **os.environ, "ANTHROPIC_API_KEY": api_key, "KILO_PORT": str(port), "KILO_WORKSPACE": str(tmp_path / "ws"),
        "KILO_DATA_DIR": str(tmp_path / "data"), "KILO_MCP_CONFIG": str(tmp_path / "data" / "mcp_servers.json"),
    }
    process = subprocess.Popen(
        [sys.executable, "-c", "from kilo.server.app import main; main()"], cwd=tmp_path, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                httpx.get(f"{base}/health", timeout=1)
                break
            except httpx.TransportError:
                assert process.poll() is None, "the server exited"
                assert time.monotonic() < deadline, "the server did not start"
                time.sleep(0.2)
        yield base
    finally:
        process.terminate()
        process.wait(10)


def test_real_server_process(real_server):
    with httpx.Client(base_url=real_server, timeout=120) as http:
        assert http.get("/health").json() == {"status": "healthy"}
        assert http.get("/api/v1/sessions").status_code == 401

        login = http.post("/api/v1/auth/login", json={"username": DEFAULT_USERNAME, "password": DEFAULT_PASSWORD})
        assert login.status_code == 200
        health = http.get("/api/v1/health").json()
        assert health["model"] == MODEL

        session = http.post("/api/v1/sessions").json()
        reply = http.post(
            f"/api/v1/sessions/{session['id']}/chat",
            json={"message": "Reply with exactly the word KIWI and nothing else.", "stream": False},
        ).json()
        assert "KIWI" in reply["response"], reply["events"][-1]
