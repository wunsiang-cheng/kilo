"""The HTTP API through the real app, its login middleware and its routes."""

import asyncio
import sys
from pathlib import Path

import pytest

import kilo.server.app as server
from fakes import text_turn, tool_turn
from conftest import PASSWORD, USERNAME
from kilo.agent.history import run_event
from kilo.auth import Logins
from kilo.config import DEFAULT_PASSWORD, settings
from kilo.models.schedule import Run, RunStatus, ScheduleSpec, ScheduleUpdate
from kilo.server.routes.notices import run_notices

API = "/api/v1"
LOGIN = f"{API}/auth/login"
ECHO = str(Path(__file__).parent / "fixtures" / "echo_mcp_server.py")


async def test_public_paths_need_no_login(client):
    assert (await client.get("/health")).json() == {"status": "healthy"}
    page = await client.get("/")
    assert page.status_code == 200
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert (await client.get("/static/main.js")).status_code == 200


@pytest.mark.parametrize("path", [
    "/docs", "/openapi.json", f"{API}/health", f"{API}/sessions", f"{API}/tasks",
    f"{API}/approvals", f"{API}/mcp/servers", f"{API}/notices", f"{API}/auth/me",
])
async def test_everything_else_needs_login(client, path):
    assert (await client.get(path)).status_code == 401


async def test_commands_cannot_run_without_login(client):
    response = await client.post(f"{API}/tasks/run", json={"command": "echo hi"})
    assert response.status_code == 401


async def test_forged_cookie_is_refused(client):
    client.cookies.set("kilo_login", "forged")
    assert (await client.get(f"{API}/auth/me")).status_code == 401


async def test_login_cookie_flags(client):
    response = await client.post(LOGIN, json={"username": USERNAME, "password": PASSWORD})
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie


async def test_me_after_login(authed):
    assert (await authed.get(f"{API}/auth/me")).json() == {"username": USERNAME}


async def test_wrong_password_401_then_lockout_429(client):
    for _ in range(5):
        response = await client.post(LOGIN, json={"username": USERNAME, "password": "wrong"})
        assert response.status_code == 401
    locked = await client.post(LOGIN, json={"username": USERNAME, "password": PASSWORD})
    assert locked.status_code == 429 and "retry-after" in locked.headers


async def test_logout_ends_session(authed):
    assert (await authed.post(f"{API}/auth/logout")).status_code == 200
    assert (await authed.get(f"{API}/auth/me")).status_code == 401


@pytest.fixture
def default_login(kilo, clock):
    kilo.logins = Logins("u", DEFAULT_PASSWORD, clock=clock)


@pytest.mark.usefixtures("default_login")
async def test_default_password_works_from_this_computer(client):
    assert (await client.post(LOGIN, json={"username": "u", "password": DEFAULT_PASSWORD})).status_code == 200


@pytest.mark.usefixtures("default_login")
@pytest.mark.parametrize("headers", [{"x-forwarded-for": "8.8.8.8"}, {"host": "evil.example"}])
async def test_default_password_refused_through_proxy_or_rebinding(client, headers):
    response = await client.post(LOGIN, json={"username": "u", "password": DEFAULT_PASSWORD}, headers=headers)
    assert response.status_code == 401 and "only from this computer" in response.json()["detail"]


async def test_session_crud(authed):
    created = await authed.post(f"{API}/sessions", json={"title": "mine"})
    assert created.status_code == 201
    session_id = created.json()["id"]
    assert [s["id"] for s in (await authed.get(f"{API}/sessions")).json()] == [session_id]
    assert (await authed.patch(f"{API}/sessions/{session_id}", json={"title": " new "})).json()["title"] == "new"
    assert (await authed.patch(f"{API}/sessions/{session_id}", json={"title": " "})).status_code == 400
    assert (await authed.delete(f"{API}/sessions/{session_id}")).status_code == 200
    assert (await authed.get(f"{API}/sessions/{session_id}")).status_code == 404


async def test_session_can_be_created_without_a_body(authed):
    assert (await authed.post(f"{API}/sessions")).status_code == 201


@pytest.fixture
async def session_id(authed):
    return (await authed.post(f"{API}/sessions")).json()["id"]


async def test_chat_non_streaming_returns_reply(authed, session_id, model):
    model.turns.append(text_turn("hello"))
    response = await authed.post(f"{API}/sessions/{session_id}/chat", json={"message": "hi", "stream": False})
    assert response.json()["response"] == "hello"


async def test_chat_streaming_emits_sse_events(authed, session_id, model):
    model.turns.append(text_turn("hello"))
    response = await authed.post(f"{API}/sessions/{session_id}/chat", json={"message": "hi"})
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: message_delta" in response.text and "event: done" in response.text


async def test_conversation_can_be_shown_again(authed, session_id, model):
    model.turns.extend([tool_turn("c1", "glob", {"pattern": "*"}), text_turn("done")])
    await authed.post(f"{API}/sessions/{session_id}/chat", json={"message": "go", "stream": False})
    body = (await authed.get(f"{API}/sessions/{session_id}/events")).json()
    assert [e["event"] for e in body["events"]] == ["user_message", "tool_call_start", "tool_call_result", "message_delta"]
    assert "messages" not in body["session"]


async def test_empty_message_400(authed, session_id):
    response = await authed.post(f"{API}/sessions/{session_id}/chat", json={"message": "  "})
    assert response.status_code == 400


@pytest.mark.parametrize("path", ["chat", "compact", "stop", "events"])
async def test_unknown_session_404(authed, path):
    call = authed.get if path == "events" else authed.post
    kwargs = {} if path == "events" else {"json": {"message": "hi"}}
    assert (await call(f"{API}/sessions/sess_x/{path}", **kwargs)).status_code == 404


async def test_stop_says_when_nothing_is_running(authed, session_id):
    assert (await authed.post(f"{API}/sessions/{session_id}/stop")).json() == {"stopped": False}


async def test_compact_endpoint(authed, session_id, model):
    model.turns.append(text_turn("hello"))
    await authed.post(f"{API}/sessions/{session_id}/chat", json={"message": "hi", "stream": False})
    assert (await authed.post(f"{API}/sessions/{session_id}/compact")).json()["event"] == "compaction_done"


async def test_task_run_endpoint_runs_command(authed):
    command = f'"{sys.executable}" -c "print(6 * 7)"'
    response = await authed.post(f"{API}/tasks/run", json={"command": command})
    body = response.json()
    assert body["status"] == "completed" and body["stdout"].strip() == "42"
    logs = await authed.get(f"{API}/tasks/{body['task_id']}/logs")
    assert "42" in logs.json()["logs"]
    assert [t["task_id"] for t in (await authed.get(f"{API}/tasks")).json()] == [body["task_id"]]


async def test_task_run_uses_the_configured_default_timeout(authed, kilo, monkeypatch):
    seen = {}
    original = kilo.task_manager.run

    async def spy(command, timeout=None, cwd=None):
        seen["timeout"] = timeout
        return await original(command, timeout=timeout, cwd=cwd)

    monkeypatch.setattr(kilo.task_manager, "run", spy)
    await authed.post(f"{API}/tasks/run", json={"command": f'"{sys.executable}" -c "pass"'})
    assert seen["timeout"] is None


async def test_unknown_task_404(authed):
    assert (await authed.get(f"{API}/tasks/task_x")).status_code == 404
    assert (await authed.get(f"{API}/tasks/task_x/logs")).status_code == 404


async def test_approvals(authed, kilo):
    spec = ScheduleSpec(name="n", instructions="do it", cron="0 * * * *", origin_session_id="sess_a")
    proposed = await kilo.scheduler.create(spec)
    await kilo.scheduler.create(spec.model_copy(update={"origin_session_id": "sess_b"}))
    listed = (await authed.get(f"{API}/approvals", params={"session_id": "sess_a"})).json()
    assert [(s["id"], s["change"]) for s in listed] == [(proposed.id, "create")]

    approved = await authed.post(f"{API}/approvals/{proposed.id}/approve")
    assert approved.status_code == 200 and approved.json()["approved"] is True
    assert (await authed.post(f"{API}/approvals/{proposed.id}/approve")).status_code == 400

    await kilo.scheduler.update(proposed.id, ScheduleUpdate(name="m"))
    refused = (await authed.post(f"{API}/approvals/{proposed.id}/reject")).json()
    assert refused["schedule"]["name"] == "n" and refused["schedule"]["awaiting_approval"] is False


async def test_unknown_approval_404(authed):
    assert (await authed.post(f"{API}/approvals/sched_x/approve")).status_code == 404
    assert (await authed.post(f"{API}/approvals/sched_x/reject")).status_code == 404


async def test_schedule_management_routes_are_gone(authed):
    assert (await authed.post(f"{API}/schedules", json={"name": "n", "instructions": "x", "cron": "0 * * * *"})).status_code == 404
    assert (await authed.get(f"{API}/runs")).status_code == 404


async def test_mcp_routes(authed):
    assert (await authed.get(f"{API}/mcp/servers")).json() == []
    added = await authed.post(f"{API}/mcp/servers", json={"name": "echo", "command": sys.executable, "args": [ECHO]})
    assert added.status_code == 200
    assert (await authed.get(f"{API}/mcp/servers/echo/health")).json()["usable"] is True
    assert (await authed.get(f"{API}/mcp/servers/echo/tools")).status_code == 200
    assert (await authed.delete(f"{API}/mcp/servers/echo")).status_code == 200


async def test_unknown_mcp_server_404(authed):
    assert (await authed.delete(f"{API}/mcp/servers/ghost")).status_code == 404
    assert (await authed.post(f"{API}/mcp/servers/ghost/connect")).status_code == 404


async def test_health_reports_the_setup(authed):
    body = (await authed.get(f"{API}/health")).json()
    assert body["model"] == settings.anthropic_model
    assert body["running_tasks"] == 0 and body["schedules_enabled"] == 0


async def test_docs_available_after_login(authed):
    assert (await authed.get("/docs")).status_code == 200


async def test_notices_stream_run_endings_and_system_turns(kilo, clock):
    run = Run(
        id="run_1", schedule_id="s", schedule_name="n", status=RunStatus.FAILED, scheduled_for=clock(),
        origin_session_id="sess_1",
    )
    open_for = iter([True, True, False])
    stream = run_notices(kilo.run_announcer, lambda: next(open_for), check_seconds=0.01)
    pending = asyncio.ensure_future(stream.__anext__())
    await asyncio.sleep(0)
    kilo.run_announcer.announce(run)
    kilo.run_announcer.session_event("sess_1", {"event": "done", "data": {}})
    notice = await pending
    assert notice["event"] == run_event(run)["event"]
    assert (await stream.__anext__())["event"] == "session_event"
    with pytest.raises(StopAsyncIteration):
        await stream.__anext__()


def test_main_refuses_default_password_on_public_host(monkeypatch):
    started = []
    monkeypatch.setattr(server.uvicorn, "run", lambda *a, **k: started.append(a))
    monkeypatch.setattr(settings, "kilo_host", "0.0.0.0")
    monkeypatch.setattr(settings, "kilo_password", DEFAULT_PASSWORD)
    with pytest.raises(SystemExit):
        server.main()
    assert started == []


def test_main_serves_on_public_host_with_own_password(monkeypatch):
    started = []
    monkeypatch.setattr(server.uvicorn, "run", lambda *a, **k: started.append(k))
    monkeypatch.setattr(settings, "kilo_host", "0.0.0.0")
    monkeypatch.setattr(settings, "kilo_password", "my-own-password")
    server.main()
    assert started[0]["host"] == "0.0.0.0"


def test_main_serves_locally_with_default_password(monkeypatch):
    started = []
    monkeypatch.setattr(server.uvicorn, "run", lambda *a, **k: started.append(k))
    monkeypatch.setattr(settings, "kilo_password", DEFAULT_PASSWORD)
    server.main()
    assert started[0]["host"] == "127.0.0.1"
