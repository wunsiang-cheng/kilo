# Kilo OpenRouter Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 kilo 透過 OpenRouter Chat Completions 呼叫模型，並完全移除 Anthropic。

**Architecture:** 新增 `kilo/openrouter.py`：用 httpx 直接串流 SSE，累積成一則 assistant 訊息，並處理錯誤與重試。agent 迴圈改用 Chat Completions 訊息格式：儲存 `payload`、新增 `SUMMARY` 角色、由 kilo 端產生壓縮摘要。工具定義改為 OpenAI function 格式。web、Telegram、排程看到的事件串流不變。

**Tech Stack:** Python ≥ 3.11、httpx（既有相依）、FastAPI、aiosqlite、pydantic-settings、pytest + pytest-asyncio（`asyncio_mode = "auto"`）、uv。

**Spec:** `docs/superpowers/specs/2026-11-03-openrouter-provider-design.md`。第 3 節是實測的 API 行為，第 4 節是各元件的規則；本計畫以「spec §x」引用。

## Global Constraints

- 端點固定為 `https://openrouter.ai/api/v1/chat/completions`。不新增任何執行期相依，只用 httpx；最後移除 `anthropic`。
- 設定（環境變數）：
  - `OPENROUTER_API_KEY`：`kilo-server` 啟動時必填。
  - `OPENROUTER_MODEL`：預設 `meta/muse-spark-1.3-contributor`。
  - `OPENROUTER_REASONING_EFFORT`：`max`、`xhigh`、`high`、`medium`、`low`、`minimal`、`none` 其中之一，預設 `high`。
  - `OPENROUTER_MAX_TOKENS`：選填，≥ 1；沒設定就不送。
- `KILO_COMPACT_AT_TOKENS`：預設 500000；只能是 0 或 ≥ 10000，沒有上限。`KILO_COMPACT_MAX_TOKENS`：預設 16000，範圍 1 到 64000。
- 重試：
  - 只在兩種情況重試：HTTP 408、429、500、502、503、504，以及收到回應 header 之前發生的 `httpx.TransportError`。最多重試 2 次。
  - 等待：`Retry-After` 不超過 60 秒就照它等，否則依序等 1 秒、2 秒。HTTP 200 之後的錯誤不重試。
- 逾時：`httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=10.0)`。
- 錯誤一律丟出 `OpenRouterError`，`str()` 為 `OpenRouter <status>: <message>`，沒有 status 時為 `OpenRouter: <message>`。runner 原本加的 `LLM error: ` 前綴不變。
- 送給 web、Telegram、排程的事件名稱與資料欄位不變（spec §1）。
- 程式碼註解與 docstring 用英文，沿用專案風格：`typing` 型別標註、只寫說明「為什麼」的短註解。
- API key 只能放在專案根目錄的 `.env`（已列入 gitignore），不得出現在任何被追蹤的檔案或 commit 中。
- 單元測試：`uv run pytest`，目前基準為 `336 passed, 5 deselected`。live 測試：`uv run pytest -m live`。
- 每個 commit 訊息的最後一行：`Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`。

## Review Focus

1. **網路封包在 SSE 任意位置切斷，包括中文多位元組字元中間**：重組後的事件必須和一次收到完全相同 → Task 1 `test_lines_split_across_network_chunks_are_reassembled`
2. **回覆只有推理，沒有文字也沒有工具呼叫**（例如推理時就碰到 length）：不能存成空的 assistant 訊息（送回時會被拒），下一則訊息仍要能正常對話 → Task 4 `test_reply_without_text_or_tool_calls_is_not_stored`
3. **沒有參數的工具呼叫，`arguments` 是空字串**：要當成 `{}` 執行，不能判定為參數無效 → Task 1 `test_blank_arguments_are_kept_as_sent`、Task 4 `test_blank_arguments_mean_no_arguments`
4. **閘道回傳非 JSON 的錯誤（例如 HTML），或 `Retry-After` 是 HTTP 日期、或超過 60 秒**：要顯示可讀的錯誤，改用 1 秒、2 秒的等待，不能崩潰 → Task 2 `test_a_non_json_error_body_is_shown_as_text`、`test_a_long_or_unreadable_retry_after_falls_back_to_backoff`
5. **server 在工具執行到一半時重啟，之後壓縮這段對話**：摘要請求裡沒有結果的工具呼叫，要先補上「被中斷」的結果，請求才合法 → Task 4 `test_compaction_after_an_interrupted_tool_call`

## 檔案結構

| 檔案 | 責任 | Task |
|---|---|---|
| `kilo/openrouter.py`（新） | `OpenRouterClient`：組請求、解析 SSE、累積回覆、錯誤、重試 | 1、2 |
| `tests/test_openrouter.py`（新） | 用錄製格式的 SSE 測試 client | 1、2 |
| `kilo/tools/base.py`、`toolbox.py`、`file_tools.py`、`search_tools.py`、`shell.py`、`mcp_tools.py`、`schedule_tools.py`、`mcp_manager.py` | 工具定義改用 `parameters`；`Toolbox.definitions()` 輸出 OpenRouter 的 `tools` 陣列 | 3 |
| `kilo/agent/chat_model.py`、`runner.py`、`history.py`、`kilo/models/session.py`、`kilo/storage/db.py` | Chat Completions 訊息、壓縮、重播、儲存 | 4 |
| `kilo/assembly.py`、`kilo/server/app.py`、`tests/live/test_live.py` | 接上 OpenRouter，並用 live 測試驗證 | 5 |
| `kilo/config.py`、`.env.example`、`README.md`、`pyproject.toml`、`uv.lock`；刪除 `kilo/claude.py`、`tests/test_claude_client.py` | 移除 Anthropic、更新文件 | 6 |

舊的 `kilo/claude.py` 和它的測試會一直保留到 Task 6 才刪。在那之前它的測試都以明確參數建構 client，所以照常通過。server 從 Task 5 起改用 OpenRouter。

---

### Task 1: OpenRouter client — 請求與串流累積

**Files:**
- Create: `kilo/openrouter.py`
- Modify: `kilo/config.py`：`Settings` 在 `anthropic_*` 之後新增 4 個欄位。`anthropic_*` 留到 Task 6 再刪。
- Modify: `tests/conftest.py:3,15`：清除環境變數的前綴加上 `OPENROUTER_`
- Test: `tests/test_openrouter.py`（新）、`tests/test_config.py`

**Interfaces:**
- Produces（Task 2、4、5 會用到）：
  - `kilo.openrouter.OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"`
  - `class OpenRouterError(Exception)`：`__init__(self, message: str, status: Optional[int] = None)`，有屬性 `status`；`__str__` 的格式見 Global Constraints。
  - `class OpenRouterClient`：
    - `__init__(self, model: Optional[str] = None, effort: Optional[str] = None, max_tokens: Optional[int] = None, api_key: Optional[str] = None, http: Optional[httpx.AsyncClient] = None, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep)`
    - `model`、`effort`、`api_key` 為 falsy 時改用 settings 的值；`max_tokens is None` 時改用 `settings.openrouter_max_tokens`。
  - `async def stream_turn(self, system: str, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]], max_tokens: Optional[int] = None) -> AsyncIterator[Dict[str, Any]]`：產生 `text`、`progress`、`done` 三種事件，格式見 spec §4.4。
  - `Settings` 新欄位：
    - `openrouter_api_key: Optional[str] = None`
    - `openrouter_model: str = "meta/muse-spark-1.3-contributor"`
    - `openrouter_reasoning_effort: Literal["max", "xhigh", "high", "medium", "low", "minimal", "none"] = "high"`
    - `openrouter_max_tokens: Optional[int] = Field(None, ge=1)`

- [ ] **Step 1: 寫失敗的測試**

建立 `tests/test_openrouter.py`：

```python
"""OpenRouterClient against SSE in the shape OpenRouter streams it (recorded 2026-11-03), through httpx's real streaming code."""

import json
import re
from typing import Any, Dict, List, Optional

import httpx
import pytest

from kilo.openrouter import OpenRouterClient, OpenRouterError

MODEL = "meta/muse-spark-1.3-contributor"
KEEPALIVE = ": OPENROUTER PROCESSING"
USAGE = {
    "prompt_tokens": 685, "completion_tokens": 794, "total_tokens": 1479, "cost": 0.000216,
    "prompt_tokens_details": {"cached_tokens": 113}, "completion_tokens_details": {"reasoning_tokens": 606},
}


def chunk(delta: Optional[Dict[str, Any]] = None, finish: Optional[str] = None, **extra: Any) -> Dict[str, Any]:
    """One streamed chunk, as OpenRouter sends it."""
    choice = {
        "index": 0, "delta": {"role": "assistant", **(delta or {})},
        "finish_reason": finish, "native_finish_reason": "completed" if finish else None,
    }
    return {"id": "gen-1", "object": "chat.completion.chunk", "model": MODEL, "provider": "Meta", "choices": [choice], **extra}


def sse(*items: Any) -> bytes:
    """A streamed body: a `data:` line per chunk (a str goes as is, e.g. a keepalive comment), then [DONE]."""
    lines = [item if isinstance(item, str) else "data: " + json.dumps(item, ensure_ascii=False) for item in items]
    return ("\n\n".join([*lines, "data: [DONE]"]) + "\n\n").encode()


TEXT = sse(
    chunk({"content": "", "reasoning": None, "reasoning_details": [
        {"type": "reasoning.encrypted", "data": "enc-a", "format": "meta-responses-v1", "id": "rs_1", "index": 0},
    ]}),
    chunk({"content": "你好"}),
    chunk({"content": ""}, finish="stop"),
    chunk({"content": ""}, finish="stop", usage={"prompt_tokens": 41, "completion_tokens": 183, "total_tokens": 224}),
)

TOOLS = sse(
    KEEPALIVE,
    chunk({"content": "", "reasoning": "Planning both calls.", "reasoning_details": [
        {"type": "reasoning.summary", "summary": "Planning both calls.", "format": "meta-responses-v1", "index": 0},
    ]}),
    KEEPALIVE,
    chunk({"content": "", "reasoning": None, "reasoning_details": [
        {"type": "reasoning.encrypted", "data": "enc-b", "format": "meta-responses-v1", "id": "rs_2", "index": 1},
    ]}),
    chunk({"content": "Doing both."}),
    chunk({"content": None, "tool_calls": [
        {"index": 0, "id": "call_a", "type": "function", "function": {"name": "write_file", "arguments": ""}}]}),
    chunk({"content": None, "tool_calls": [{"index": 0, "function": {"arguments": '{"path":"hello.txt"'}}]}),
    chunk({"content": None, "tool_calls": [{"index": 0, "function": {"arguments": ',"content":"MANGO"}'}}]}),
    chunk({"content": None, "tool_calls": [
        {"index": 1, "id": "call_b", "type": "function", "function": {"name": "glob", "arguments": '{"pattern":"*.txt"}'}}]}),
    chunk({"content": ""}, finish="tool_calls"),
    chunk({"content": ""}, finish="tool_calls", usage=USAGE),
)

TOOL_DEFINITION = {"type": "function", "function": {"name": "glob", "description": "d", "parameters": {"type": "object", "properties": {}}}}


def streaming(body: Any):
    return lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)


def client_for(responder, requests: Optional[List[httpx.Request]] = None, **options: Any) -> OpenRouterClient:
    def handle(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        return responder(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    return OpenRouterClient(model=MODEL, effort="low", api_key="test-key", http=http, **options)


async def turn(client: OpenRouterClient, tools: Any = (), max_tokens: Optional[int] = None) -> List[Dict[str, Any]]:
    messages = [{"role": "user", "content": "hi"}]
    return [event async for event in client.stream_turn("sys", messages, list(tools), max_tokens=max_tokens)]


async def test_text_reply_streams_then_finishes():
    events = await turn(client_for(streaming(TEXT)))
    assert events[:-1] == [{"type": "text", "delta": "你好"}]
    assert events[-1] == {
        "type": "done", "finish_reason": "stop", "context_tokens": 41 + 183,
        "message": {"role": "assistant", "content": "你好", "reasoning_details": [
            {"type": "reasoning.encrypted", "data": "enc-a", "format": "meta-responses-v1", "id": "rs_1", "index": 0},
        ]},
    }


async def test_parallel_tool_calls_are_assembled():
    events = await turn(client_for(streaming(TOOLS)))
    assert events[:-1] == [{"type": "progress", "delta": "Planning both calls."}, {"type": "text", "delta": "Doing both."}]
    assert events[-1] == {
        "type": "done", "finish_reason": "tool_calls", "context_tokens": 685 + 794,
        "message": {
            "role": "assistant", "content": "Doing both.", "reasoning": "Planning both calls.",
            "reasoning_details": [
                {"type": "reasoning.summary", "summary": "Planning both calls.", "format": "meta-responses-v1", "index": 0},
                {"type": "reasoning.encrypted", "data": "enc-b", "format": "meta-responses-v1", "id": "rs_2", "index": 1},
            ],
            "tool_calls": [
                {"id": "call_a", "type": "function", "function": {"name": "write_file", "arguments": '{"path":"hello.txt","content":"MANGO"}'}},
                {"id": "call_b", "type": "function", "function": {"name": "glob", "arguments": '{"pattern":"*.txt"}'}},
            ],
        },
    }


async def test_lines_split_across_network_chunks_are_reassembled():
    async def pieces():
        for start in range(0, len(TEXT), 7):  # cuts lines, and 你好 mid-character
            yield TEXT[start:start + 7]

    assert await turn(client_for(streaming(pieces()))) == await turn(client_for(streaming(TEXT)))


async def test_reasoning_detail_fragments_are_joined_by_index():
    body = sse(
        chunk({"reasoning": "Hel", "reasoning_details": [{"type": "reasoning.text", "text": "Hel", "index": 0}]}),
        chunk({"reasoning": "lo", "reasoning_details": [{"type": "reasoning.text", "text": "lo", "signature": "sig", "index": 0}]}),
        chunk({"content": "ok"}, finish="stop"),
    )
    events = await turn(client_for(streaming(body)))
    assert [event["delta"] for event in events if event["type"] == "progress"] == ["Hel", "lo"]
    assert events[-1]["message"]["reasoning"] == "Hello"
    assert events[-1]["message"]["reasoning_details"] == [{"type": "reasoning.text", "text": "Hello", "signature": "sig", "index": 0}]


async def test_a_tool_call_without_an_id_gets_one():
    body = sse(chunk({"tool_calls": [{"index": 0, "function": {"name": "glob", "arguments": "{}"}}]}), chunk({}, finish="tool_calls"))
    [call] = (await turn(client_for(streaming(body))))[-1]["message"]["tool_calls"]
    assert re.fullmatch(r"call_[0-9a-f]{24}", call["id"])


async def test_blank_arguments_are_kept_as_sent():
    body = sse(
        chunk({"tool_calls": [{"index": 0, "id": "c1", "type": "function", "function": {"name": "glob", "arguments": ""}}]}),
        chunk({}, finish="tool_calls"),
    )
    [call] = (await turn(client_for(streaming(body))))[-1]["message"]["tool_calls"]
    assert call == {"id": "c1", "type": "function", "function": {"name": "glob", "arguments": ""}}


async def test_without_usage_the_context_is_not_measured():
    done = (await turn(client_for(streaming(sse(chunk({"content": "ok"}, finish="stop"))))))[-1]
    assert done["context_tokens"] is None


async def test_request_carries_model_effort_and_the_system_prompt_first():
    requests: List[httpx.Request] = []
    await turn(client_for(streaming(TEXT), requests))
    [request] = requests
    body = json.loads(request.content)
    assert request.url == "https://openrouter.ai/api/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer test-key"
    assert body["model"] == MODEL and body["stream"] is True and body["reasoning"] == {"effort": "low"}
    assert body["messages"] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    assert "tools" not in body and "max_tokens" not in body


async def test_tools_are_sent_as_given():
    requests: List[httpx.Request] = []
    await turn(client_for(streaming(TEXT), requests), tools=[TOOL_DEFINITION])
    assert json.loads(requests[0].content)["tools"] == [TOOL_DEFINITION]


@pytest.mark.parametrize("configured,given,sent", [(1000, None, 1000), (1000, 50, 50), (None, 50, 50)])
async def test_max_tokens_given_for_the_call_wins(configured, given, sent):
    requests: List[httpx.Request] = []
    await turn(client_for(streaming(TEXT), requests, max_tokens=configured), max_tokens=given)
    assert json.loads(requests[0].content)["max_tokens"] == sent


async def test_an_error_in_the_stream_is_raised():
    failed = {
        "id": "gen-1", "object": "chat.completion.chunk", "error": {"code": 502, "message": "Provider disconnected"},
        "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "error"}],
    }
    with pytest.raises(OpenRouterError, match="Provider disconnected") as caught:
        await turn(client_for(streaming(sse(chunk({"content": "par"}), failed))))
    assert caught.value.status == 502


async def test_finish_reason_error_is_raised():
    with pytest.raises(OpenRouterError, match="ended with an error"):
        await turn(client_for(streaming(sse(chunk({"content": "par"}), chunk({}, finish="error")))))


async def test_a_stream_that_ends_before_the_reply_finishes_is_raised():
    with pytest.raises(OpenRouterError, match="ended before the reply finished"):
        await turn(client_for(streaming(sse(chunk({"content": "par"})))))


async def test_without_an_api_key_nothing_is_sent():
    requests: List[httpx.Request] = []
    client = OpenRouterClient(model=MODEL, http=httpx.AsyncClient(transport=httpx.MockTransport(requests.append)))
    with pytest.raises(OpenRouterError, match="OPENROUTER_API_KEY is not set"):
        await turn(client)
    assert requests == []
```

在 `tests/test_config.py` 做三處修改：
- `test_unusable_value_is_refused` 的參數加上 `("openrouter_reasoning_effort", "bogus"), ("openrouter_max_tokens", 0)`。
- `test_values_at_the_limits_are_accepted` 的參數加上 `("openrouter_max_tokens", 1)`。
- 新增下面這個測試：

```python
def test_openrouter_defaults():
    defaults = Settings(_env_file=None)
    assert defaults.openrouter_model == "meta/muse-spark-1.3-contributor"
    assert defaults.openrouter_reasoning_effort == "high"
    assert defaults.openrouter_api_key is None and defaults.openrouter_max_tokens is None
```

- [ ] **Step 2: 確認測試失敗**

Run: `uv run pytest tests/test_openrouter.py tests/test_config.py -q`
Expected：`tests/test_openrouter.py` 收集時就失敗，錯誤為 `ModuleNotFoundError: No module named 'kilo.openrouter'`；`test_openrouter_defaults` 和新加的參數失敗。

- [ ] **Step 3: 在 `kilo/config.py` 加上 4 個設定欄位**（見 Interfaces），上方加一行註解：`# The model, through OpenRouter.`

- [ ] **Step 4: 實作 `kilo/openrouter.py`**

- module docstring：`OpenRouter Chat Completions transport: one streamed agent turn, with client tools.`
- `stream_turn`：
  - 沒有 key 時，在送出任何請求之前丟出 `OpenRouterError("OPENROUTER_API_KEY is not set.")`。
  - 請求 body 依 spec §4.4：`tools` 非空才送；`max_tokens` 依序取呼叫參數、建構值，兩者都沒有就不送。header 為 `Authorization: Bearer <key>`。
  - 用 `request = self._http.build_request("POST", OPENROUTER_URL, json=body, headers=...)` 建請求，再 `response = await self._http.send(request, stream=True)` 送出，並在 `finally` 裡 `await response.aclose()`。Task 2 會在 `send` 外面加上重試。
  - 本 Task 先這樣處理 status ≥ 400：`await response.aread()` 後丟出 `OpenRouterError(response.text[:500] or response.reason_phrase, response.status_code)`。Task 2 會換成完整的錯誤解析。
- 解析與累積：
  - 用 `response.aiter_lines()` 逐行解析，規則見 spec §4.4「串流解析與累積」。
  - 累積狀態放在私有類別 `_Reply`：`add(chunk: Dict[str, Any]) -> List[Dict[str, Any]]` 回傳這個 chunk 要產生的 `text`／`progress` 事件；`message() -> Dict[str, Any]` 組出最後的 assistant 訊息。`stream_turn` 只負責 I/O。
  - `reasoning_details` 和 `tool_calls` 依「第一次出現」的順序輸出：用以 `index` 為 key 的 dict（Python 的 dict 會保留插入順序）；沒有 `index` 的 reasoning 項目，各自用一個新的 key 加入。
- 錯誤訊息的固定文字：
  - 串流裡從頭到尾沒有出現 `finish_reason`：`the response stream ended before the reply finished`
  - `finish_reason` 為 `error`：`the reply ended with an error ({native_finish_reason})`，沒有 `native_finish_reason` 時括號內寫 `no details`
- 沒有傳入 http client 時，預設建立 `httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=10.0))`。

- [ ] **Step 5: 更新 `tests/conftest.py`**

第 15 行的前綴改成 `("ANTHROPIC_", "KILO_", "OPENROUTER_")`；第 3 行 docstring 改成 `...with no ANTHROPIC_, OPENROUTER_ or KILO_ variables:`。

- [ ] **Step 6: 確認測試通過**

Run: `uv run pytest -q`
Expected：全部通過，數量為 336 加上這次新增的測試；`5 deselected`。

- [ ] **Step 7: Commit**

```bash
git add kilo/openrouter.py kilo/config.py tests/test_openrouter.py tests/test_config.py tests/conftest.py
git commit -m "feat: add OpenRouter streaming client" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: OpenRouter client — HTTP 錯誤與重試

**Files:**
- Modify: `kilo/openrouter.py`
- Test: `tests/test_openrouter.py`（在檔尾追加）

**Interfaces:**
- Consumes：Task 1 的 `OpenRouterClient`、`OpenRouterError`；測試 helper `client_for`、`turn`、`sse`、`chunk`、`TEXT`。
- Produces：module 常數 `RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})`、`MAX_RETRIES = 2`、`BACKOFF = (1.0, 2.0)`、`MAX_RETRY_AFTER = 60.0`。`stream_turn` 的簽名不變。

- [ ] **Step 1: 寫失敗的測試**（追加到 `tests/test_openrouter.py`）

```python
def respond(*responses: Any):
    """Answer each request with the next of `responses`: an httpx.Response, or an exception to raise."""
    queue = list(responses)

    def responder(request: httpx.Request) -> httpx.Response:
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return responder


def failure(status: int, body: Any, **headers: str) -> httpx.Response:
    if isinstance(body, dict):
        return httpx.Response(status, json=body, headers=headers)
    return httpx.Response(status, text=body, headers=headers)


def ok() -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=TEXT)


@pytest.fixture
def sleeps() -> List[float]:
    return []


def retrying(responses: List[Any], requests: List[httpx.Request], sleeps: List[float]) -> OpenRouterClient:
    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    return client_for(respond(*responses), requests, sleep=sleep)


async def test_an_http_error_reports_the_providers_own_message(sleeps):
    raw = '{"error":{"message":"only auto is supported for tool_choice"}}'
    body = {"error": {"message": "Provider returned error", "code": 400, "metadata": {"raw": raw, "provider_name": "Meta"}}}
    requests: List[httpx.Request] = []
    with pytest.raises(OpenRouterError) as caught:
        await turn(retrying([failure(400, body)], requests, sleeps))
    assert caught.value.status == 400
    assert str(caught.value) == f"OpenRouter 400: Provider returned error ({raw})"
    assert len(requests) == 1 and sleeps == []


async def test_moderation_reasons_are_reported(sleeps):
    body = {"error": {"message": "Input was flagged", "code": 403, "metadata": {"reasons": ["violence", "hate"]}}}
    with pytest.raises(OpenRouterError, match=r"Input was flagged \(reasons: violence, hate\)"):
        await turn(retrying([failure(403, body)], [], sleeps))


async def test_a_non_json_error_body_is_shown_as_text(sleeps):
    with pytest.raises(OpenRouterError, match="<html>Bad request</html>"):
        await turn(retrying([failure(400, "<html>Bad request</html>")], [], sleeps))


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404])
async def test_client_errors_are_not_retried(status, sleeps):
    requests: List[httpx.Request] = []
    with pytest.raises(OpenRouterError):
        await turn(retrying([failure(status, {"error": {"message": "no", "code": status}})], requests, sleeps))
    assert len(requests) == 1 and sleeps == []


async def test_a_rate_limit_is_retried_after_the_delay_it_asks_for(sleeps):
    requests: List[httpx.Request] = []
    limited = failure(429, {"error": {"message": "slow down", "code": 429}}, **{"Retry-After": "3"})
    events = await turn(retrying([limited, ok()], requests, sleeps))
    assert events[-1]["finish_reason"] == "stop" and len(requests) == 2 and sleeps == [3.0]


async def test_server_errors_back_off_then_give_up(sleeps):
    requests: List[httpx.Request] = []
    busy = [failure(503, {"error": {"message": "no provider available", "code": 503}}) for _ in range(3)]
    with pytest.raises(OpenRouterError, match="OpenRouter 503: no provider available"):
        await turn(retrying(busy, requests, sleeps))
    assert len(requests) == 3 and sleeps == [1.0, 2.0]


async def test_a_long_or_unreadable_retry_after_falls_back_to_backoff(sleeps):
    limited = lambda after: failure(429, {"error": {"message": "slow down", "code": 429}}, **{"Retry-After": after})
    await turn(retrying([limited("120"), limited("Wed, 21 Oct 2026 07:28:00 GMT"), ok()], [], sleeps))
    assert sleeps == [1.0, 2.0]


async def test_a_connection_failure_is_retried(sleeps):
    events = await turn(retrying([httpx.ConnectError("refused"), ok()], [], sleeps))
    assert events[-1]["finish_reason"] == "stop" and sleeps == [1.0]


async def test_a_connection_that_never_succeeds_is_reported(sleeps):
    with pytest.raises(OpenRouterError, match="could not reach OpenRouter"):
        await turn(retrying([httpx.ConnectError("refused") for _ in range(3)], [], sleeps))


async def test_an_error_after_the_reply_started_is_not_retried(sleeps):
    requests: List[httpx.Request] = []
    broken = sse(chunk({"content": "par"}), {"error": {"code": 502, "message": "Provider disconnected"}, "choices": []})
    with pytest.raises(OpenRouterError, match="Provider disconnected"):
        await turn(retrying([httpx.Response(200, content=broken), ok()], requests, sleeps))
    assert len(requests) == 1


async def test_a_connection_lost_mid_reply_is_reported_not_retried(sleeps):
    async def body():
        yield ("data: " + json.dumps(chunk({"content": "par"})) + "\n\n").encode()
        raise httpx.ReadError("connection reset")

    requests: List[httpx.Request] = []
    with pytest.raises(OpenRouterError, match="connection reset"):
        await turn(retrying([httpx.Response(200, content=body()), ok()], requests, sleeps))
    assert len(requests) == 1
```

- [ ] **Step 2: 確認測試失敗**

Run: `uv run pytest tests/test_openrouter.py -q`
Expected：重試相關測試失敗（請求數或 `sleeps` 不符），`metadata.raw` 和 `reasons` 的訊息測試也失敗；Task 1 的測試仍然通過。

- [ ] **Step 3: 在 `kilo/openrouter.py` 實作錯誤解析與重試**

- `async def _http_error(response: httpx.Response) -> OpenRouterError`：
  - 先 `await response.aread()`。body 是 JSON 時，訊息取 `error.message`（沒有就用 `reason_phrase`）。
  - 有 `error.metadata.raw` 時，附加 ` (<str(raw) 的前 500 字>)`。
  - 有 `error.metadata.reasons` 時，附加 ` (reasons: <以 ", " 串接>)`。
  - body 不是 JSON 時，訊息用 `response.text[:500]`，若為空字串則改用 `reason_phrase`。
  - status 一律用 HTTP 狀態碼。
- `def _retry_delay(response: httpx.Response, attempt: int) -> float`：`Retry-After` 能解析成 `float`，而且介於 0 和 `MAX_RETRY_AFTER` 之間，就用它；否則回傳 `BACKOFF[attempt]`。
- `async def _send(self, request: httpx.Request) -> httpx.Response`：最多嘗試 `MAX_RETRIES + 1` 次。
  - `send` 丟出 `httpx.TransportError`：還有次數就 `await self._sleep(BACKOFF[attempt])` 後重試；次數用完則丟出 `OpenRouterError(f"could not reach OpenRouter ({type(e).__name__}: {e})")`。
  - 狀態碼在 `RETRY_STATUSES` 裡而且還有次數：先關閉 response，再 `await self._sleep(_retry_delay(...))` 後重試。
  - 其他 status ≥ 400，或重試次數已用完：丟出 `await _http_error(response)`。
- 讀取串流的迴圈外面包一層 `except httpx.TransportError as e`，改丟出 `OpenRouterError(f"the connection broke during the reply ({type(e).__name__}: {e})") from e`，不重試。

- [ ] **Step 4: 確認測試通過**

Run: `uv run pytest -q`
Expected：全部通過。

- [ ] **Step 5: Commit**

```bash
git add kilo/openrouter.py tests/test_openrouter.py
git commit -m "feat: retry and explain OpenRouter errors" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: 工具定義改用 OpenRouter 的 function 格式

**Files:**
- Modify：
  - `kilo/tools/file_tools.py:36,67,102`
  - `kilo/tools/search_tools.py:43,83`
  - `kilo/tools/shell.py:22`、`kilo/tools/mcp_tools.py:22`、`kilo/tools/schedule_tools.py:31`
  - `kilo/tools/toolbox.py:20-28,59-71`
  - `kilo/tools/mcp_manager.py:423-439`
  - `kilo/tools/base.py:11`（docstring）
- Test: `tests/test_mcp_manager.py`、`tests/test_config.py:49,57`

**Interfaces:**
- Produces（Task 4 的 runner 會原樣傳給模型）：
  - `Tool.definition`：`{"name": str, "description": str, "parameters": <JSON Schema>}`
  - `MCPManager.tool_definitions() -> List[Dict[str, Any]]`：同上格式，`parameters` 來自 MCP tool 的 `input_schema`，並補上 `type: object` 和 `properties: {}` 的預設值。
  - `with_intent(definition)`：改為讀寫 `parameters`。
  - `Toolbox.definitions() -> List[Dict[str, Any]]`：回傳 `[{"type": "function", "function": definition}, ...]`。
- 不變：`MCPManager.describe(include_tools=True)` 回傳的 `input_schema` 欄位，那是 MCP 管理 API 的欄位。

- [ ] **Step 1: 改寫測試**

在 `tests/test_mcp_manager.py`：
- 第 55 行改為 `assert TOOL in [d["function"]["name"] for d in toolbox.definitions()]`
- `recording_tool` 把 `"input_schema"` 改成 `"parameters"`
- `test_every_tool_takes_a_required_intent` 改寫為：

```python
async def test_every_tool_takes_a_required_intent(manager):
    await manager.add_server(echo_config(tools=["echo"]))
    tool = recording_tool("read", [])
    definitions = Toolbox([tool], manager).definitions()
    assert [d["type"] for d in definitions] == ["function", "function"]
    assert [d["function"]["name"] for d in definitions] == ["read", TOOL]
    for definition in definitions:
        schema = definition["function"]["parameters"]
        assert list(schema["properties"])[0] == INTENT and schema["required"][0] == INTENT
    assert definitions[0]["function"]["parameters"]["required"] == [INTENT, "path"]
    assert INTENT not in tool.definition["parameters"]["properties"]  # the tool's own definition is left alone
```

- `test_a_tool_with_its_own_intent_keeps_it` 的斷言改為 `assert toolbox.definitions()[0] == {"type": "function", "function": tool.definition}`
- 新增兩個測試：

```python
async def test_mcp_tools_are_described_with_parameters(manager):
    await manager.add_server(echo_config())
    definition = next(d for d in manager.tool_definitions() if d["name"] == TOOL)
    assert set(definition) == {"name", "description", "parameters"} and definition["parameters"]["type"] == "object"


async def test_builtin_tools_are_described_with_parameters(kilo):
    for tool in kilo.tools:
        assert set(tool.definition) == {"name", "description", "parameters"}, tool.name
```

在 `tests/test_config.py` 第 49 行和第 57 行，把 `["input_schema"]` 改成 `["parameters"]`。

- [ ] **Step 2: 確認測試失敗**

Run: `uv run pytest tests/test_mcp_manager.py tests/test_config.py -q`
Expected：上述測試失敗，錯誤為 `KeyError: 'parameters'`、`KeyError: 'function'` 或集合不符。

- [ ] **Step 3: 實作**

- 8 個內建工具定義把 key 從 `input_schema` 改名為 `parameters`。
- `MCPManager.tool_definitions()` 輸出 `parameters`，docstring 改為 `OpenAI-style function definitions for every connected MCP tool.`
- `with_intent`、`Toolbox._has_own_intent` 改讀 `parameters`。
- `Toolbox.definitions()` 把每個定義包成 `{"type": "function", "function": ...}`，docstring 改為 `The tools array OpenRouter takes: built-in tools and connected MCP tools, each taking an intent.`
- `base.py` 的 docstring 改為 `definition: OpenAI-style function definition (name, description, parameters) sent to the LLM`。

舊的 Anthropic client 這時仍是預設的 client，但單元測試不會用到它，它在 Task 6 刪除。

- [ ] **Step 4: 確認測試通過**

Run: `uv run pytest -q`
Expected：全部通過。

- [ ] **Step 5: Commit**

```bash
git add kilo/tools tests/test_mcp_manager.py tests/test_config.py
git commit -m "refactor: describe tools in OpenRouter's function format" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Agent 迴圈、壓縮與重播改用 Chat Completions 訊息

**Files:**
- Modify：
  - `kilo/agent/chat_model.py`
  - `kilo/agent/runner.py`
  - `kilo/agent/history.py`
  - `kilo/models/session.py`
  - `kilo/storage/db.py`
- Test：`tests/fakes.py`、`tests/test_runner.py`、`tests/test_storage.py`、`tests/test_api.py:146-149`

**Interfaces:**
- Consumes：
  - `stream_turn` 的事件格式與 Task 1 的 client 相同。
  - Task 3 的 `Toolbox.definitions()` 原樣傳入 `tools`。
- Produces：
  - `ChatModel.stream_turn(system, messages, tools, max_tokens: Optional[int] = None)`：協定裡唯一的方法，`compact` 移除。
  - `MessageRole.SUMMARY = "summary"`；`Message.payload: Optional[Dict[str, Any]] = None`，取代原本的 `blocks`。
  - `Database.add_message(session_id, role, content=None, payload=None, message_id=None)`；DB 欄位 `payload TEXT`。
  - `kilo.agent.runner` 匯出：
    - `SUMMARY_HEADER = "[Summary of the conversation so far. It replaces the earlier messages, which you can no longer see.]"`
    - `_api_messages(db_messages) -> List[Dict[str, Any]]`
    - 沿用原本的 `COMPACTION_INSTRUCTIONS`、`CONTINUE_AFTER_COMPACTION`、`INTERRUPTED_TOOL_RESULT`、`COMPACTION_RETRY_TOKENS`、`_parse_tool_arguments`，內容不變。
  - fakes：
    - `text_turn(text, finish_reason="stop")`
    - `tool_turn(call_id, name, args, finish_reason="tool_calls")`
    - `FakeModel.requests`：每次呼叫的 messages
    - `FakeModel.max_tokens`：每次呼叫的 `max_tokens`

- [ ] **Step 1: 改寫 `tests/fakes.py`**

`text_turn`、`tool_turn` 和 `FakeModel.stream_turn` 換成下面的版本；刪除 `FakeModel.compact`、`compact_result`、`compactions`；在 import 加上 `import json`。

```python
def text_turn(text: str, finish_reason: str = "stop") -> List[Dict[str, Any]]:
    """One assistant turn that only says `text` (no text event when it is empty, as the client sends none)."""
    events: List[Dict[str, Any]] = [{"type": "text", "delta": text}] if text else []
    done = {"type": "done", "finish_reason": finish_reason, "message": {"role": "assistant", "content": text or None}, "context_tokens": 100}
    return [*events, done]


def tool_turn(call_id: str, name: str, args: Any, finish_reason: str = "tool_calls") -> List[Dict[str, Any]]:
    """One assistant turn that calls one tool; `args` given as a str is sent as the raw arguments."""
    arguments = args if isinstance(args, str) else json.dumps(args)
    call = {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}
    return [{
        "type": "done", "finish_reason": finish_reason, "context_tokens": 100,
        "message": {"role": "assistant", "content": None, "tool_calls": [call]},
    }]
```

`FakeModel` 的改動：
- `__init__` 設定 `self.requests: List[List[Dict[str, Any]]] = []` 和 `self.max_tokens: List[Optional[int]] = []`。
- `stream_turn(self, system, messages, tools, max_tokens=None)` 先記錄 `messages` 和 `max_tokens`，再照原本的方式逐一回放 turns。

- [ ] **Step 2: 改寫儲存與 API 測試**

- `tests/test_storage.py`：把 `test_session_round_trip_with_blocks` 換成下面兩個測試。

```python
async def test_session_round_trip_with_payload(db):
    session = await db.create_session(title=" Title ", metadata={"a": 1})
    payload = {"role": "assistant", "content": "學", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "glob", "arguments": "{}"}}]}
    await db.add_message(session.id, MessageRole.ASSISTANT, content="學", payload=payload)
    stored = await db.get_session(session.id)
    assert stored.title == "Title" and stored.metadata == {"a": 1}
    assert [(m.role, m.content, m.payload) for m in stored.messages] == [(MessageRole.ASSISTANT, "學", payload)]


async def test_a_summary_keeps_its_role(db):
    session = await db.create_session()
    await db.add_message(session.id, MessageRole.SUMMARY, content="the summary")
    [message] = await db.get_messages(session.id)
    assert (message.role, message.content, message.payload) == (MessageRole.SUMMARY, "the summary", None)
```

- `tests/test_api.py` 的 `test_compact_endpoint`：把 `model.turns.append(text_turn("hello"))` 改成 `model.turns.extend([text_turn("hello"), text_turn("the summary")])`。

- [ ] **Step 3: 改寫 `tests/test_runner.py`**

import 改為：

```python
from kilo.agent.runner import (
    COMPACTION_INSTRUCTIONS, COMPACTION_RETRY_TOKENS, CONTINUE_AFTER_COMPACTION, INTERRUPTED_TOOL_RESULT, SUMMARY_HEADER,
    _api_messages,
)
```

在 `names` 之後加上兩個 helper：

```python
def interrupted(call_id: str) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": INTERRUPTED_TOOL_RESULT}


def summary_message(text: str) -> dict:
    return {"role": "user", "content": f"{SUMMARY_HEADER}\n\n{text}"}
```

逐一改寫既有測試（下表沒列到的測試維持原樣）：

| 測試 | 改成 |
|---|---|
| `test_reply_is_stored_and_sent_back_next_turn` | 最後一行改為 `assert model.requests[1][1] == {"role": "assistant", "content": "one"}` |
| `test_tool_call_round_trip` | 最後兩行改為 `last = model.requests[1][-1]` 加上 `assert last["role"] == "tool" and last["tool_call_id"] == "c1"` |
| `test_tool_error_marks_is_error` | 改名為 `test_tool_error_goes_back_as_an_error_result`；斷言改為 `assert json.loads(model.requests[1][-1]["content"])["status"] == "error"` |
| `test_interrupted_tool_call_gets_error_result` | `add_message(..., payload=tool_turn("c1", "shell", {})[0]["message"])`；斷言改為 `assert messages[-1] == interrupted("c1")` |
| `test_interrupted_tool_call_is_closed_before_the_next_user_message` | 同上用 `payload`；roles 為 `["user", "assistant", "tool", "user"]`；`assert model.requests[0][2] == interrupted("c1")` |
| `test_results_of_one_turn_share_one_user_message` | 改名為 `test_each_result_is_its_own_tool_message`；第二個呼叫用 `both[0]["message"]["tool_calls"].append({"id": "c2", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "y"}'}})` 加入；斷言改為 `assert [(m["role"], m["tool_call_id"]) for m in model.requests[1][-2:]] == [("tool", "c1"), ("tool", "c2")]` |
| `test_refusal_discards_turn` | 改名為 `test_content_filter_discards_turn`；用 `text_turn("no", finish_reason="content_filter")`；`assert events[-1]["data"]["error"] == "The model declined this request (content filter)."`；儲存的 roles 仍只有 `[MessageRole.USER]` |
| `test_cut_off_tool_call_not_executed` | 用 `finish_reason="length"`，其餘不變 |
| `test_reply_cut_off_without_tools_is_an_error` | 用 `finish_reason="length"`；`assert (await collect(kilo, session.id, "hi"))[-1]["data"]["error"] == "Response stopped early (length)."` |
| `big_session` fixture | 舊回答改為 `add_message(session.id, MessageRole.ASSISTANT, content="old answer", payload={"role": "assistant", "content": "old answer"})` |
| `test_history_shows_a_non_json_tool_result_as_raw_text` | `SimpleNamespace(role=MessageRole.TOOL, created_at=None, content="plain", payload={"role": "tool", "tool_call_id": "c", "content": "plain"})` |

把壓縮相關的四個測試（`test_auto_compaction_replaces_history`、`test_failed_compaction_keeps_history_and_sets_retry`、`test_summary_cut_off_is_retried_once_with_more_room`、`test_compaction_mid_loop_continues_the_task`）和 `test_manual_compaction` 換成下面的版本，並新增其餘測試：

```python
async def test_auto_compaction_replaces_history(kilo, model, big_session):
    model.turns.extend([text_turn("the summary"), text_turn("ok")])
    events = await collect(kilo, big_session.id, "new question")
    assert names(events)[:2] == ["compaction_start", "compaction_done"]
    assert events[1]["data"] == {"context_tokens": 20_000 + len("new question") // 4, "summary_tokens": len("the summary") // 4}
    assert model.requests[0] == [
        {"role": "user", "content": "old question"}, {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": COMPACTION_INSTRUCTIONS},
    ]
    assert model.max_tokens[:2] == [settings.kilo_compact_max_tokens, None]
    assert model.requests[1] == [summary_message("the summary"), {"role": "user", "content": "new question"}]


async def test_failed_compaction_keeps_history_and_sets_retry(kilo, model, big_session):
    model.turns.extend([[RuntimeError("no summary")], text_turn("ok")])
    events = await collect(kilo, big_session.id, "new question")
    assert events[1] == {"event": "compaction_failed", "data": {"error": "LLM error: no summary"}}
    assert names(events)[-1] == "done"
    assert model.requests[1][0] == {"role": "user", "content": "old question"}
    meta = (await kilo.db.get_session(big_session.id, include_messages=False)).metadata
    assert meta["compact_retry_at"] >= 20_000 + COMPACTION_RETRY_TOKENS


async def test_summary_cut_off_is_retried_once_with_more_room(kilo, model, big_session):
    model.turns.extend([text_turn("", finish_reason="length"), text_turn("", finish_reason="length"), text_turn("ok")])
    events = await collect(kilo, big_session.id, "new question")
    assert model.max_tokens[:2] == [settings.kilo_compact_max_tokens, 2 * settings.kilo_compact_max_tokens]
    assert events[1] == {"event": "compaction_failed", "data": {"error": "no summary was written (finish reason: length)"}}


async def test_tool_calls_in_a_summary_are_ignored(kilo, model, big_session, workspace):
    summary = tool_turn("c9", "write_file", {"path": "x.txt", "content": "no"})
    summary[-1]["message"]["content"] = "the summary"
    model.turns.extend([summary, text_turn("ok")])
    events = await collect(kilo, big_session.id, "new question")
    assert names(events)[:2] == ["compaction_start", "compaction_done"]
    assert not (workspace / "x.txt").exists()
    assert model.requests[1][0] == summary_message("the summary")


async def test_a_summary_without_text_fails(kilo, model, big_session):
    model.turns.extend([tool_turn("c9", "glob", {"pattern": "*"}), text_turn("ok")])
    events = await collect(kilo, big_session.id, "new question")
    assert events[1]["data"]["error"] == "no summary was written (finish reason: tool_calls)"


async def test_compaction_mid_loop_continues_the_task(kilo, model, session, monkeypatch):
    monkeypatch.setattr(settings, "kilo_compact_at_tokens", 10_000)
    call = tool_turn("c1", "glob", {"pattern": "*"})
    call[-1]["context_tokens"] = 20_000
    model.turns.extend([call, text_turn("the summary"), text_turn("finished")])
    events = await collect(kilo, session.id, "go")
    assert "compaction_done" in names(events) and names(events)[-1] == "done"
    assert model.requests[2] == [summary_message("the summary"), {"role": "user", "content": CONTINUE_AFTER_COMPACTION}]


async def test_manual_compaction(kilo, model, session):
    model.turns.extend([text_turn("hello"), text_turn("the summary")])
    await collect(kilo, session.id, "hi")
    assert (await kilo.runner.compact(session.id))["event"] == "compaction_done"
    assert (await kilo.db.get_session(session.id, include_messages=False)).metadata["compactions"] == 1
    assert (await kilo.db.get_messages(session.id))[-1].role == MessageRole.SUMMARY


async def test_compaction_after_an_interrupted_tool_call(kilo, model, session):
    await kilo.db.add_message(session.id, MessageRole.USER, content="go")
    await kilo.db.add_message(session.id, MessageRole.ASSISTANT, payload=tool_turn("c1", "shell", {})[0]["message"])
    model.turns.append(text_turn("the summary"))
    assert (await kilo.runner.compact(session.id))["event"] == "compaction_done"
    assert model.requests[0][-2:] == [interrupted("c1"), {"role": "user", "content": COMPACTION_INSTRUCTIONS}]


async def test_reply_without_text_or_tool_calls_is_not_stored(kilo, model, session):
    model.turns.extend([text_turn("", finish_reason="length"), text_turn("ok")])
    assert (await collect(kilo, session.id, "hi"))[-1]["data"]["error"] == "Response stopped early (length)."
    assert [m.role for m in await kilo.db.get_messages(session.id)] == [MessageRole.USER]
    assert names(await collect(kilo, session.id, "again"))[-1] == "done"
    assert model.requests[1] == [{"role": "user", "content": "hi"}, {"role": "user", "content": "again"}]


async def test_blank_arguments_mean_no_arguments(kilo, model, session):
    model.turns.extend([tool_turn("c1", "glob", ""), text_turn("ok")])
    events = await collect(kilo, session.id, "go")
    assert events[1]["data"]["arguments"] == {}
    assert "Invalid arguments" not in json.dumps(events[2]["data"]["result"])


async def test_history_replays_reasoning_and_summaries(kilo, model, session):
    reply = text_turn("hi")
    reply[-1]["message"]["reasoning"] = "thinking it over"
    model.turns.extend([[{"type": "progress", "delta": "thinking it over"}, *reply], text_turn("the summary")])
    live = await collect(kilo, session.id, "hello")
    await kilo.runner.compact(session.id)
    replayed = history_events(await kilo.db.get_messages(session.id), kilo.toolbox.canonical_name)
    assert names(replayed) == [*names(live)[:-1], "compaction_done"]
    assert replayed[1] == {"event": "progress", "data": {"delta": "thinking it over"}}
```

- [ ] **Step 4: 確認測試失敗**

Run: `uv run pytest tests/test_runner.py tests/test_storage.py tests/test_api.py -q`
Expected：大量失敗，例如 `ImportError: cannot import name 'SUMMARY_HEADER'`、`AttributeError: SUMMARY`、`unexpected keyword argument 'payload'`。

- [ ] **Step 5: 改儲存層**

- `kilo/models/session.py`：
  - 新增 `MessageRole.SUMMARY = "summary"`。
  - `Message.blocks` 改為 `payload: Optional[Dict[str, Any]] = None`，註解：`The message as sent to OpenRouter (Chat Completions shape): an assistant turn as returned, or a tool result. None for user messages and summaries.`
- `kilo/storage/db.py`：`CREATE TABLE`、`_message`、`add_message(..., payload: Optional[Dict[str, Any]] = None, ...)`，以及兩處 `SELECT`，都把 `blocks` 改成 `payload`。

- [ ] **Step 6: 改協定與 runner**

- `kilo/agent/chat_model.py`：協定只留 `stream_turn(..., max_tokens: Optional[int] = None)`，docstring 改寫成描述 spec §4.4 的事件。
- `kilo/agent/runner.py`：
  - 刪除 `_tool_results_message`。
  - 新增 `SUMMARY_HEADER`，以及 `REQUEST_FIELDS = ("role", "content", "tool_calls", "reasoning_details")`。
  - `_is_summary` 改成判斷 `msg.role == MessageRole.SUMMARY`。
  - `_api_messages` 依 spec §4.5 的規則 1 到 6 重寫。assistant 訊息只取 `REQUEST_FIELDS` 中有出現的欄位。
  - 一般的 turn 呼叫 `stream_turn` 時不傳 `max_tokens`，讓 client 使用設定值。
- turn 收到 `done` 之後，依 spec §4.5 的步驟 1 到 5 處理。固定文字：
  - `The model declined this request (content filter).`
  - `Not executed: the response stopped ({finish_reason}) before this tool call was complete.`
  - `Response stopped early ({finish_reason}).`
- 工具結果用以下方式儲存，不再帶 `is_error`：

  ```python
  add_message(role=MessageRole.TOOL, content=result_str, payload={"role": "tool", "tool_call_id": call_id, "content": result_str})
  ```

- `_compact` 依 spec §4.6 處理：
  - 請求內容為 `_api_messages(歷史) + [{"role": "user", "content": COMPACTION_INSTRUCTIONS}]`。
  - `max_tokens` 依序用 `(cap, 2 * cap)`，只有 `finish_reason == "length"` 時才用第二個值重試。
  - 讀完全部事件，只保留 `done`。
  - 成功時存成 `MessageRole.SUMMARY`，`context_tokens` 和 `summary_tokens` 都是 `len(summary) // CHARS_PER_TOKEN`。
  - 失敗的固定文字：`no summary was written (finish reason: {finish_reason})`。
- 把本 Task 改到的 docstring 和註解，凡是提到 Messages API、content blocks、signed compaction 或 Claude 的，都改成描述 Chat Completions 的說法。

- [ ] **Step 7: 改重播**（`kilo/agent/history.py`）

依 spec §4.7 實作。工具參數用 runner 的 `_parse_tool_arguments` 解析，只取回傳的 dict，忽略錯誤；工具結果用 `_result(msg.content)`。

- [ ] **Step 8: 確認測試通過**

Run: `uv run pytest -q`
Expected：全部通過。Telegram 和排程的測試不用修改也會通過，因為它們只透過 `text_turn`、`tool_turn` 建 turn。

- [ ] **Step 9: Commit**

```bash
git add kilo/agent kilo/models/session.py kilo/storage/db.py tests/fakes.py tests/test_runner.py tests/test_storage.py tests/test_api.py
git commit -m "feat: run the agent loop on Chat Completions messages" -m "Summaries are written by the model as a normal turn and stored as a summary message." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: 讓 Kilo 實際跑在 OpenRouter 上

**Files:**
- Modify：
  - `kilo/assembly.py:76-80`
  - `kilo/server/app.py:91,102-111`
  - `tests/test_api.py:220-223,248-272`
  - `tests/live/test_live.py`
- Create（只在本機，不 commit）：專案根目錄的 `.env`

**Interfaces:**
- Consumes：Task 1、2 的 `OpenRouterClient`；Task 4 的 runner。
- Produces：
  - `Kilo.chat_model` 預設改為 `OpenRouterClient()`。
  - `/api/v1/health` 的 `"model"` 改為 `settings.openrouter_model`。
  - `kilo.server.app.main()` 在缺少 key 時丟出 `SystemExit("OPENROUTER_API_KEY is not set: put your OpenRouter API key in .env (see .env.example).")`。這個檢查要放在預設密碼檢查之前。

- [ ] **Step 1: 寫失敗的測試**

在 `tests/test_api.py`：
- `test_health_reports_the_setup` 改為 `assert body["model"] == settings.openrouter_model`。
- 既有的三個 `test_main_*` 測試，都加上 `monkeypatch.setattr(settings, "openrouter_api_key", "test-key")`，讓它們繼續測原本要測的東西。
- 新增：

```python
def test_main_refuses_to_start_without_an_api_key(monkeypatch):
    started = []
    monkeypatch.setattr(server.uvicorn, "run", lambda *a, **k: started.append(k))
    monkeypatch.setattr(settings, "openrouter_api_key", None)
    with pytest.raises(SystemExit, match="OPENROUTER_API_KEY is not set"):
        server.main()
    assert started == []
```

改寫 `tests/live/test_live.py`：
- 模組 docstring 第一行改為 `Real OpenRouter API calls, with the key from the project's .env. ...`。
- 移除 `import anthropic` 和 `kilo.claude`，改為 `from kilo.openrouter import OpenRouterClient`，並設 `MODEL = "meta/muse-spark-1.3-contributor"`。
- `api_key` fixture 改讀 `OPENROUTER_API_KEY`，讀不到時的錯誤訊息為 `Set OPENROUTER_API_KEY in .env to run the live tests.`
- `live_kilo` 改用 `OpenRouterClient(model=MODEL, effort="low", api_key=api_key)`。
- `real_server` 的 env 改用 `"OPENROUTER_API_KEY": api_key`。
- `test_real_server_process` 在現有斷言之後，加上一段使用工具的對話：

```python
        tooled = http.post(
            f"/api/v1/sessions/{session['id']}/chat",
            json={"message": "Use the shell tool to run `echo KILO-OK`, then reply with its output only.", "stream": False},
        ).json()
        assert "tool_call_result" in [event["event"] for event in tooled["events"]], tooled["events"][-1]
        assert "KILO-OK" in tooled["response"]
```

- [ ] **Step 2: 確認測試失敗**

Run: `uv run pytest tests/test_api.py -q`
Expected：`test_health_reports_the_setup` 和 `test_main_refuses_to_start_without_an_api_key` 失敗。

- [ ] **Step 3: 實作**

- `assembly.py`：`chat_model` 改為 `from kilo.openrouter import OpenRouterClient` 並 `return OpenRouterClient()`。
- `app.py`：health 的 `"model"` 改為 `settings.openrouter_model`；`main()` 開頭加上缺少 key 的檢查（訊息見 Interfaces）。

- [ ] **Step 4: 確認單元測試通過**

Run: `uv run pytest -q`
Expected：全部通過。

- [ ] **Step 5: 準備 live 測試的 key**

在專案根目錄建立 `.env`，只寫一行：`OPENROUTER_API_KEY=<使用者在對話中提供的開發用 key>`。執行 `git check-ignore .env`，輸出必須是 `.env`，確認它不會被 commit。

這把 key 在 2026-11-04 13:19（台北時間）到期，過期後要向使用者索取新的 key。

- [ ] **Step 6: 跑 live 測試**

Run: `uv run pytest -m live -v`
Expected：`5 passed`。花費遠低於 $0.01。失敗時先看錯誤訊息裡 OpenRouter 回傳的原因：
- 401：key 無效或過期。
- 402：額度不足。
- 400：參數有誤，`metadata.raw` 會寫明 provider 的原因。

- [ ] **Step 7: Commit**（不包含 `.env`）

```bash
git add kilo/assembly.py kilo/server/app.py tests/test_api.py tests/live/test_live.py
git commit -m "feat: run Kilo on OpenRouter" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: 移除 Anthropic，完成設定與文件

**Files:**
- Delete：`kilo/claude.py`、`tests/test_claude_client.py`
- Modify：`kilo/config.py`、`.env.example`、`README.md`、`pyproject.toml`、`uv.lock`、`tests/conftest.py`、`tests/test_config.py`

**Interfaces:**
- Produces：
  - `Settings` 不再有任何 `anthropic_*` 欄位。
  - `config.py` 不再有 `CONTEXT_WINDOW`、`MAX_OUTPUT_TOKENS`。
  - `kilo_compact_max_tokens: int = Field(16_000, ge=1, le=64_000)`。
  - `kilo_compact_at_tokens` 只能是 0 或 ≥ `MIN_COMPACT_AT_TOKENS`，違反時的錯誤訊息為 `KILO_COMPACT_AT_TOKENS must be 0 (off) or at least 10,000.`

- [ ] **Step 1: 寫失敗的測試**

在 `tests/test_config.py`：
- `test_example_is_parsed` 改為 `assert len(example_values()) >= 15`。
- 從兩個參數化清單中移除 `anthropic_max_tokens` 的參數。
- 新增：

```python
def test_the_only_model_settings_are_openrouters():
    model_settings = {name for name in Settings.model_fields if not name.startswith("kilo_")}
    assert model_settings == {"openrouter_api_key", "openrouter_model", "openrouter_reasoning_effort", "openrouter_max_tokens"}


def test_compaction_threshold_has_no_ceiling():
    assert Settings(_env_file=None, kilo_compact_at_tokens=5_000_000).kilo_compact_at_tokens == 5_000_000
```

- [ ] **Step 2: 確認測試失敗**

Run: `uv run pytest tests/test_config.py -q`
Expected：這兩個新測試失敗。前者是因為舊的 4 個模型設定欄位還在；後者是因為目前的上限 872,000。

- [ ] **Step 3: 修改 `kilo/config.py`**

刪除 `anthropic_*` 欄位與上方的註解，以及 `CONTEXT_WINDOW`、`MAX_OUTPUT_TOKENS` 兩個常數。`_compaction_fits` 改成只檢查下限，錯誤訊息見 Interfaces。`kilo_compact_max_tokens` 的上限改為 `le=64_000`。

- [ ] **Step 4: 改寫 `.env.example`**

把 `# --- Model (Anthropic API)` 整段換成：

```
# --- Model (OpenRouter) ------------------------------------------------------
# Required: kilo-server will not start without an OpenRouter API key (https://openrouter.ai/settings/keys).
# OPENROUTER_API_KEY=sk-or-v1-...
# Any model id listed on https://openrouter.ai/models
OPENROUTER_MODEL=meta/muse-spark-1.3-contributor
# max | xhigh | high | medium | low | minimal | none. Not every model offers every level; some cannot turn reasoning off.
OPENROUTER_REASONING_EFFORT=high
# Cap on one reply, reasoning included. Unset: the model's own limit. The conversation and the cap together must fit
# in the model's context, or the request is refused.
# OPENROUTER_MAX_TOKENS=
```

把 `# --- Context compaction` 整段換成：

```
# --- Context compaction ------------------------------------------------------
# A conversation this large (tokens) is summarized before its next request. 0 turns it off; otherwise at least 10000.
# Keep it well below the model's context length (OpenRouter lists it for each model): the summary request carries the
# whole conversation. Lower it when you switch to a model with a smaller context.
# Each request resends the whole conversation, so a lower value costs less per turn but summarizes sooner.
KILO_COMPACT_AT_TOKENS=500000
# Cap on one summary, reasoning included. At most 64000: a summary cut off is retried with twice this.
KILO_COMPACT_MAX_TOKENS=16000
```

- [ ] **Step 5: 移除相依、刪檔、清理測試設定**

```bash
uv remove anthropic
git rm kilo/claude.py tests/test_claude_client.py
```

- `pyproject.toml` 的 marker 改為 `markers = ["live: calls the real OpenRouter API (needs credentials)"]`。
- `tests/conftest.py` 第 15 行的前綴改為 `("KILO_", "OPENROUTER_")`；第 3 行 docstring 改為 `...with no OPENROUTER_ or KILO_ variables:`。

- [ ] **Step 6: 改寫 `README.md`**

| 位置 | 新內容 |
|---|---|
| 第 3 行 | `An autonomous AI agent with an unrestricted shell, scheduled jobs and MCP integrations, running on any model [OpenRouter](https://openrouter.ai) offers. You talk to it through a web terminal served by a single FastAPI process, or through a Telegram bot.` |
| Features 的 Agent loop | `streams Claude's replies` 改為 `streams the model's replies` |
| Quick start 第 1 句 | `Requires Python 3.11+, [uv](https://docs.astral.sh/uv/) and an [OpenRouter API key](https://openrouter.ai/settings/keys).` |
| Quick start 設定 key 那句 | `Set OPENROUTER_API_KEY in .env; the server will not start without it. Then start the server from the project folder:`（`OPENROUTER_API_KEY` 和 `.env` 用反引號包起來） |
| 設定表 `ANTHROPIC_MODEL` 那一列 | `` | `OPENROUTER_MODEL`, `OPENROUTER_REASONING_EFFORT` | Model (any OpenRouter model id) and reasoning effort | `` |
| 設定表新增一列（緊接在上一列之後） | `` | `KILO_COMPACT_AT_TOKENS` | Conversation size (tokens) that triggers a summary; keep it well below the model's context length | `` |

- [ ] **Step 7: 確認 Anthropic 已完全移除**

Run: `grep -rniE "anthropic|claude" kilo tests README.md .env.example pyproject.toml uv.lock`
Expected：沒有任何輸出（exit code 1）。如果還有找到，就改掉那一處（註解或 docstring 改成描述 OpenRouter 或「the model」），然後重跑。

- [ ] **Step 8: 跑完整驗證**

Run: `uv run pytest -q`，Expected：全部通過，`5 deselected`。
Run: `uv run pytest -m live -q`，Expected：`5 passed`。

另外，如果專案根目錄有舊的 `.kilo_data/kilo.db`（舊 schema），而且使用者確認沒有要保留的資料，就把它刪掉，並在回報中說明。

- [ ] **Step 9: Commit**

```bash
git add -A kilo tests README.md .env.example pyproject.toml uv.lock
git commit -m "chore: remove Anthropic" -m "Settings, docs and dependencies now describe OpenRouter only." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

執行 `git status --short`，確認 `.env` 不在任何一個 commit 裡。
