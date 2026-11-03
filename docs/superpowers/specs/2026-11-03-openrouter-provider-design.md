# Kilo：以 OpenRouter 取代 Anthropic（設計規格）

- 日期：2026-11-03
- 狀態：設計已逐段確認，待審閱 spec
- 範圍：kilo 的 LLM provider 層與其上受影響的格式、設定、測試、文件

## 1. 目標與約束

**目標**：kilo 改由 OpenRouter 呼叫模型，以便在多個模型（Muse、GPT、Gemini、DeepSeek 等）之間選擇、降低成本；Anthropic 的 SDK、設定、訊息格式、文件與測試全部移除。

**約束**

- 沒有資料需要遷移，不需相容舊版本：DB schema、環境變數、儲存的訊息格式可直接改。
- 使用的模型幾乎都是 1M context；換模型時由維護的工程師調整設定，不做自動偵測。
- 對外行為不變：web terminal、Telegram、排程收到的事件串流（`user_message`、`message_delta`、`progress`、`tool_call_start`、`tool_call_result`、`compaction_*`、`done`、`error`）名稱與資料欄位都維持原樣，三個介面不需修改。
- 開發測試模型：`meta/muse-spark-1.3-contributor`。

**成功標準**

1. 單元測試全部通過（`uv run pytest`）。
2. live 測試以 Muse Spark 全部通過（`uv run pytest -m live`）：回覆、工具迴圈、多輪記憶、手動壓縮後仍記得、實際啟動 server 對話。
3. 在 `kilo/`、`tests/`、`README.md`、`.env.example`、`pyproject.toml`、`uv.lock` 中搜尋 `anthropic` 與 `claude`（不分大小寫）結果為零（`docs/superpowers/` 下描述這次遷移的 spec 與計畫除外）。
4. 實際啟動 `kilo-server`，完成一段使用工具的對話。

**不做（非目標）**

- 跨模型 fallback（Anthropic 的 `fallbacks="default"`）：不替代。同一模型在不同 provider 間的切換 OpenRouter 預設就會做；之後若需要，可加 OpenRouter 的 `models` 陣列。
- 明確的 prompt caching（`cache_control`）：不做，依賴 provider 的隱式快取（Muse Spark 實測有命中）。
- 自動偵測模型 context 長度、啟動時驗證模型 ID。
- OpenRouter 歸屬 header（`HTTP-Referer`、`X-Title`）。
- OpenRouter 的 Responses API 與 Anthropic 相容端點（`/api/v1/messages`）。
- 多模態輸入。

## 2. 現況

Anthropic 依賴集中在 `kilo/claude.py`（`ClaudeClient`，anthropic SDK、beta 功能：adaptive thinking 的 progress 顯示、伺服器端 compaction、fallbacks、`cache_control`），但格式滲透到：

- `kilo/agent/runner.py`：以 Anthropic content blocks 重建歷史（tool results 合併成一則 user 訊息）、`stop_reason`（`tool_use`、`end_turn`、`refusal`、`max_tokens`）、伺服器端壓縮。
- `kilo/agent/chat_model.py`：`ChatModel` 協定（`stream_turn` 與 `compact`）。
- `kilo/agent/history.py`：由 blocks 重播事件。
- 工具定義的 `input_schema`：`kilo/tools/` 下 5 個內建工具檔、`toolbox.py`、`mcp_manager.py`、`base.py`。
- `kilo/config.py`：`ANTHROPIC_*` 設定、寫死的 `CONTEXT_WINDOW = 1_000_000` 與 `MAX_OUTPUT_TOKENS = 128_000`。
- `kilo/models/session.py`、`kilo/storage/db.py`：`blocks` 欄位。
- 測試、README、`.env.example`、`pyproject.toml`。

## 3. 已實測的 OpenRouter 行為（2026-11-03，模型 `meta/muse-spark-1.3-contributor`）

實作與測試以這些事實為準：

1. **串流格式**：`POST https://openrouter.ai/api/v1/chat/completions`、`stream: true` 回傳 SSE。每行 `data: {chunk}`，以 `data: [DONE]` 結束；等待期間會送 `: OPENROUTER PROCESSING` 註解行（keepalive）。
2. **chunk 內容**：`choices[0].delta` 可能帶 `content`、`reasoning`（可讀的推理摘要文字）、`reasoning_details`、`tool_calls`；`choices[0].finish_reason`（`stop`、`tool_calls`、`length`…）與 `native_finish_reason`。最後一個 chunk 帶 `usage`（`prompt_tokens`、`completion_tokens`、`prompt_tokens_details.cached_tokens`、`completion_tokens_details.reasoning_tokens`、`cost`）。`finish_reason` 可能在連續兩個 chunk 中出現。
3. **reasoning_details**：每個項目有 `index`、`type`（`reasoning.summary` 帶 `summary`；`reasoning.encrypted` 帶 `data`；另有 `format`、`id`）。工具迴圈中把累積的 `reasoning_details` 原樣放回 assistant 訊息送回，API 接受且延續推理（快取命中 625 tokens）。同時送回 `reasoning` 文字也被接受，但不採用（見 4.3）。
4. **平行工具呼叫**：`tool_calls` 以 `index` 區分；第一個片段帶 `id`、`type`、`function.name`，之後的片段只帶 `function.arguments` 的分段字串。
5. **reasoning.effort**：接受 `max`、`xhigh`、`high`、`medium`、`low`、`minimal`、`none`；其他值回 400。Muse Spark 的推理是強制的，`none` 會回 400（"Reasoning is mandatory for this endpoint and cannot be disabled."）。
6. **錯誤格式**：HTTP 4xx/5xx，body 為 `{"error": {"message", "code", "metadata"}}`。provider 端的錯誤只寫 `"Provider returned error"`，真正原因在 `metadata.raw`（provider 原始 JSON 字串）。
7. **`tool_choice: "none"` 被拒**：OpenRouter 的模型資訊宣稱支援，但 Meta provider 回 400（`only "auto" is supported for tool_choice`）。
8. **max_tokens**：prompt 加 `max_tokens` 超過模型 context 會回 400。不送 `max_tokens` 則正常。
9. **截斷**：`finish_reason: "length"`（`native_finish_reason: "max_output_tokens"`）。
10. **摘要請求**：歷史含工具呼叫時，帶同一組 tools（不設 `tool_choice`）加上「只寫摘要、不要呼叫工具」的 user 訊息，模型回純文字，且吃到快取；不帶 tools 也可行，但無快取。
11. **摘要放置**：`[system, user(摘要), user(新問題)]`（連續兩則 user 訊息）可正常回答，模型能用摘要中的資訊。
12. **測試 key**：額度上限 $10，到期時間 2026-11-04 05:19 UTC（台北 13:19）。之後執行 live 測試需換新 key。
13. **回放歷史裡的工具參數**（最終審查時補測）：Muse Spark 對歷史中 `arguments` 為空字串或無效 JSON 的 assistant 工具呼叫回 400（`` `arguments` must be valid JSON ``）；DeepInfra 提供的 mistral-small-3.2、gemma-3-27b 對無效 JSON 也會在串流中途回 400。改成 `"{}"` 後（保留原本的加密 `reasoning_details`、content 有無皆可）Muse 接受。
14. **其他 provider 對 kilo 歷史形狀的接受度**（最終審查時補測）：Muse Spark、deepseek-v4-flash、gpt-oss-120b、mistral-small-3.2、gemma-3-27b 都接受連續兩則 user 訊息、另一個模型的加密 `reasoning_details`，以及呼叫不在 `tools` 清單中的函式名稱；mistral-small-3.2、gemma-3-27b 不支援推理，但帶 `reasoning.effort` 也照常回應。

## 4. 設計

### 4.1 設定、相依、文件

`kilo/config.py` 的 `Settings`：

| 欄位（環境變數） | 型別與預設 | 驗證與用途 |
|---|---|---|
| `openrouter_api_key`（`OPENROUTER_API_KEY`） | `Optional[str] = None` | 必填，但在 Settings 層保持可選；`kilo-server` 的 `main()` 在缺少時以 `SystemExit` 拒絕啟動：`OPENROUTER_API_KEY is not set: put your OpenRouter API key in .env (see .env.example).` |
| `openrouter_model`（`OPENROUTER_MODEL`） | `str = "meta/muse-spark-1.3-contributor"` | 任意 OpenRouter 模型 ID |
| `openrouter_reasoning_effort`（`OPENROUTER_REASONING_EFFORT`） | `Literal["max", "xhigh", "high", "medium", "low", "minimal", "none"] = "high"` | 送為 `reasoning.effort` |
| `openrouter_max_tokens`（`OPENROUTER_MAX_TOKENS`） | `Optional[int] = Field(None, ge=1)` | 有值才送 `max_tokens` |
| `kilo_compact_at_tokens` | `int = 500_000` | 0（關閉）或至少 `MIN_COMPACT_AT_TOKENS`（10,000）；不再有上限 |
| `kilo_compact_max_tokens` | `int = Field(16_000, ge=1, le=64_000)` | 摘要請求的 `max_tokens`（推理也算在內）；被截斷時以兩倍重試一次 |

- 刪除 `CONTEXT_WINDOW`、`MAX_OUTPUT_TOKENS` 常數與 `anthropic_*` 欄位；`_compaction_fits` 驗證改為只檢查下限。
- `.env.example` 的 Model 段落改為 OpenRouter：`OPENROUTER_API_KEY` 註解掉（必填說明），`OPENROUTER_MODEL`、`OPENROUTER_REASONING_EFFORT` 未註解，`OPENROUTER_MAX_TOKENS` 註解掉並說明「不設定則使用模型自身上限」。壓縮段落刪除 Haiku 說明，改為：「保持遠低於模型的 context 長度（OpenRouter 的模型頁有列出）：摘要請求會帶上整段對話。換成 context 較小的模型時請調低。」
- `test_example_is_parsed` 的門檻（目前 `> 15`）隨未註解設定少一項而調整為 `>= 15`。
- `pyproject.toml`：以 `uv remove anthropic` 移除相依（同步更新 `uv.lock`）；pytest marker 說明改為 `live: calls the real OpenRouter API (needs credentials)`。
- README：介紹、Features、Quick start（需要 OpenRouter API key；`OPENROUTER_API_KEY` 必填）、設定表（`OPENROUTER_MODEL`、`OPENROUTER_REASONING_EFFORT`）全部改寫，不再提及 Claude 或 Anthropic。
- `kilo/server/app.py` 的 `/api/v1/health` 回傳 `"model": settings.openrouter_model`。
- 刪除 `kilo/claude.py`、`tests/test_claude_client.py`。

### 4.2 工具定義

- `Tool.definition` 改為 OpenAI function 物件：`{"name", "description", "parameters"}`（取代 `input_schema`）。修改：`file_tools.py`（3 個）、`search_tools.py`（2 個）、`shell.py`、`mcp_tools.py`、`schedule_tools.py`、`base.py` 的說明。
- `toolbox.with_intent` 與 `Toolbox._has_own_intent` 改讀寫 `parameters`。
- `MCPManager.tool_definitions()` 把 MCP 的 `inputSchema` 輸出為 `parameters`（仍補上 `type: object`、`properties: {}` 預設值）。
- `Toolbox.definitions()` 回傳 OpenRouter 的 `tools` 陣列：`[{"type": "function", "function": definition}, ...]`，client 原樣送出。
- 工具名稱已符合 `^[A-Za-z0-9_-]{1,64}$`，`intent` 參數機制不變。`MCPManager.describe(include_tools=True)` 回傳的 `input_schema` 是 MCP 管理 API 的欄位，不變。

### 4.3 訊息儲存

- `Message.blocks: Optional[List[Dict]]` 改為 `Message.payload: Optional[Dict[str, Any]]`：Chat Completions 格式的訊息物件。DB 欄位 `blocks` 改名 `payload`（`CREATE TABLE`、`_message`、`add_message(payload=...)`、各 `SELECT`）。
- `MessageRole` 新增 `SUMMARY = "summary"`：壓縮摘要。
- 部署注意：舊的 `.kilo_data/kilo.db` 必須刪除（schema 不同，新程式寫入時會直接報錯，不會誤讀）。

| role | `content` | `payload` |
|---|---|---|
| `user` | 使用者文字（含排程啟動訊息、`CONTINUE_AFTER_COMPACTION`） | `None` |
| `assistant` | 回覆文字，或 `None` | client 回傳的 assistant 訊息：`role`、`content`，以及有值才有的 `reasoning`、`reasoning_details`、`tool_calls` |
| `tool` | 工具結果 JSON 字串 | `{"role": "tool", "tool_call_id": ..., "content": ...}` |
| `summary` | 摘要文字 | `None` |

- 送給 API 的 assistant 訊息只取 `role`、`content`、`tool_calls`、`reasoning_details`（有值者）；`reasoning` 僅供重播顯示，不送回。
- 儲存時，工具呼叫的 `arguments` 若是 JSON 物件就逐字保留，否則（空白、被截斷、格式錯誤、非物件）存成 `"{}"`：部分 provider 拒絕歷史中出現這些參數（第 3 節第 13 點），否則整個 session 之後都會失敗。該呼叫的結果已說明參數出了什麼問題。
- session metadata（`context_tokens`、`summary_message_id`、`compactions`、`compact_retry_at`）語意不變。
- `GET /api/v1/sessions/{id}` 回傳的訊息欄位隨之由 `blocks` 變為 `payload`，role 可能出現 `summary`。web terminal 與 Telegram 不使用這些欄位（它們用事件串流與 `/events`）。

### 4.4 OpenRouter client（新檔 `kilo/openrouter.py`，取代 `kilo/claude.py`）

**`ChatModel` 協定**（`kilo/agent/chat_model.py`）只剩一個方法：

```python
def stream_turn(
    self, system: str, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]], max_tokens: Optional[int] = None,
) -> AsyncIterator[Dict[str, Any]]: ...
```

依序產生：

- `{"type": "text", "delta": str}`：來自 `delta.content`。
- `{"type": "progress", "delta": str}`：來自 `delta.reasoning`。
- 最後一個 `{"type": "done", "finish_reason": str, "message": dict, "context_tokens": int | None}`：
  - `finish_reason`：`stop`、`tool_calls`、`length`、`content_filter`（`error` 不會出現，會改為丟出例外）。
  - `message`：`{"role": "assistant", "content": 文字或 None}`，加上有值才有的 `reasoning`（字串）、`reasoning_details`（list）、`tool_calls`（`[{"id", "type": "function", "function": {"name", "arguments"}}]`，`arguments` 為原始 JSON 字串）。
  - `context_tokens`：`usage.prompt_tokens + usage.completion_tokens`；沒有 usage 時為 `None`。

失敗一律丟出例外，不以事件回報。`compact()` 移除。

**`OpenRouterClient`**

- 建構參數：`model`、`effort`、`max_tokens`、`api_key`（預設皆取自 settings）、`http: Optional[httpx.AsyncClient]`（測試注入 `MockTransport`；預設自建，timeout 為 `httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=10.0)`）、`sleep`（重試等待用，預設 `asyncio.sleep`，測試可注入）。
- 請求：`POST https://openrouter.ai/api/v1/chat/completions`，header `Authorization: Bearer <key>`，body：
  - `model`、`stream: true`
  - `messages`：`[{"role": "system", "content": system}, *messages]`
  - `reasoning: {"effort": effort}`
  - `tools`：非空時才送
  - `max_tokens`：呼叫參數優先，其次設定值，都沒有就不送
- 缺少 API key：送出前丟出 `OpenRouterError`（`OPENROUTER_API_KEY is not set.`）。

**串流解析與累積**

- 略過空行與 `:` 開頭的行；`data: [DONE]` 結束；其餘 `data: ` 後為 JSON chunk。
- chunk 含 `error` 欄位：丟出 `OpenRouterError`（訊息取 `error.message`；`error.code` 為整數時作為 status，否則 status 為 `None`）。
- 取 `choices[0]`：
  - `delta.content` 非空：累積並產生 `text` 事件。
  - `delta.reasoning` 非空：累積並產生 `progress` 事件。
  - `delta.reasoning_details`：依 `index` 合併；`text`、`summary`、`data` 三個字串欄位串接，其他欄位以最後的非空值為準；沒有 `index` 的項目視為新項目附加。依第一次出現的順序輸出，項目保留原有欄位（含 `index`）。
  - `delta.tool_calls`：依 `index` 合併，依第一次出現的順序輸出；`id`、`function.name` 取第一次出現的非空值，`function.arguments` 串接；結束時仍沒有 `id` 的呼叫產生 `call_<uuid4 hex 前 24 碼>`。
  - `finish_reason` 非空：記下（以最後一個為準）；值為 `error` 時丟出 `OpenRouterError`。
- chunk 含 `usage`：記下。
- 串流結束時沒有收到任何 `finish_reason`：丟出 `OpenRouterError`（`the response stream ended before the reply finished`）。

**錯誤**

- `OpenRouterError(Exception)`：屬性 `status: Optional[int]`；`str()` 為 `OpenRouter <status>: <message>`，無 status 時為 `OpenRouter: <message>`。runner 既有的 `LLM error: ...` 包裝不變。
- HTTP 狀態碼 ≥ 400：讀取 body。JSON 時取 `error.message`；若 `error.metadata.raw` 存在，附加 ` (<raw 前 500 字>)`；若 `error.metadata.reasons` 存在（審核拒絕），附加 ` (reasons: a, b)`。非 JSON 時取 body 前 500 字。

**重試**（只在串流開始前）

- 可重試：HTTP 408、429、500、502、503、504，以及送出請求到收到回應 header 之間的 `httpx.TransportError`（連線失敗、逾時等）。
- 最多重試 2 次（共 3 次嘗試）。等待：`Retry-After` header 可解析為秒數且 ≤ 60 時照它等，否則第 1 次重試等 1 秒、第 2 次等 2 秒。
- 不重試：其他 4xx（400、401、402、403、404…）、HTTP 200 之後串流中途的任何錯誤（避免重複輸出）。
- `/stop` 取消 task 時，`async with` 會關閉 HTTP 串流。

### 4.5 Agent 迴圈（`kilo/agent/runner.py`）

**`_api_messages(db_messages)`**：由儲存的訊息重建請求，結果須可重現（確保快取前綴一致）：

1. 從最新一則 `SUMMARY` 開始（之前的訊息仍保存但不再送出）。
2. `SUMMARY` → `{"role": "user", "content": f"{SUMMARY_HEADER}\n\n{content}"}`，其中 `SUMMARY_HEADER = "[Summary of the conversation so far. It replaces the earlier messages, which you can no longer see.]"`。
3. `USER` → `{"role": "user", "content": content}`。
4. `ASSISTANT` → payload 中的標準欄位（4.3）；payload 為 `None` 時為 `{"role": "assistant", "content": content}`。其 `tool_calls` 的 id 列入「待回覆」。
5. `TOOL` → `{"role": "tool", "tool_call_id": payload["tool_call_id"], "content": content}`，並從「待回覆」移除。每個結果各一則 tool 訊息（不再合併）。
6. 遇到非 tool 訊息前、以及結尾時，替仍待回覆的呼叫補上 `{"role": "tool", "tool_call_id": id, "content": INTERRUPTED_TOOL_RESULT}`。

**每個 turn 收到 `done` 之後**

1. `context_tokens` 有值：更新 session metadata（同現行）。
2. `finish_reason == "content_filter"`：產生 `error` 事件 `The model declined this request (content filter).`，此 turn 不儲存，結束。
3. `message` 有文字或有 `tool_calls` 才儲存為 `ASSISTANT`（`content` 為文字或 `None`，`payload` 為 `message`）。空的 assistant 訊息送回時有些 provider 會拒絕。
4. 有 `tool_calls`：逐一處理後進入下一輪：
   - 名稱經 `toolbox.canonical_name` 轉換；`arguments` 以既有 `_parse_tool_arguments` 解析（字串或空白皆可）。
   - `finish_reason == "length"`：不執行，結果為 `Not executed: the response stopped (length) before this tool call was complete.`。
   - 參數無法解析：不執行，結果為既有的 `Invalid arguments ...` 錯誤。
   - 其他情況（含 provider 帶工具呼叫卻回 `stop`）：執行。
   - 結果儲存為 `TOOL`（`content` 為結果字串，`payload` 為 tool 訊息），並產生 `tool_call_start`、`tool_call_result` 事件（同現行）。
5. 沒有 `tool_calls`：`stop` → `done` 事件；其他（`length`）→ `error` 事件 `Response stopped early (length).`。

其餘（session 鎖、`/stop`、`max_turns`、自動命名、事件格式）不變。

### 4.6 壓縮（改為 kilo 端產生摘要）

觸發條件不變：`KILO_COMPACT_AT_TOKENS` 門檻、失敗後的 `compact_retry_at`、`/compact`、工具迴圈中途。

**`_compact(session_id, estimate, manual)`**

1. 產生 `compaction_start`（同現行）。
2. 請求：`_api_messages(歷史) + [{"role": "user", "content": COMPACTION_INSTRUCTIONS}]`，同一個 system prompt、同一組 tools、`max_tokens = KILO_COMPACT_MAX_TOKENS`。不設 `tool_choice`（實測會被拒）；指示文字已要求只寫純文字、不呼叫工具。完整消耗 `stream_turn`，不把文字串流給使用者。
3. `finish_reason == "length"`：以 `2 * KILO_COMPACT_MAX_TOKENS` 重試一次（同現行）。
4. 成功條件：`finish_reason` 為 `stop` 或 `tool_calls`，且 `message.content` 去除空白後非空。模型若呼叫了工具，忽略該呼叫、採用文字。
5. 成功：
   - 儲存 `SUMMARY` 訊息（`content` 為摘要）。
   - metadata：`context_tokens = len(摘要) // CHARS_PER_TOKEN`、`compactions + 1`、`compact_retry_at = None`、`summary_message_id = 該訊息 id`。
   - 產生 `compaction_done`，`summary_tokens` 同為上述估算值（OpenRouter 的 completion tokens 含推理 token，不適合直接使用）。
6. 失敗（例外、文字為空、`content_filter`、兩次都 `length`）：`compact_retry_at = estimate + COMPACTION_RETRY_TOKENS`，產生 `compaction_failed`（訊息：例外為 `LLM error: ...`；其他為 `no summary was written (finish reason: <reason>)`）。對話以完整歷史繼續（同現行）。

工具迴圈中途壓縮成功後，仍加入 `CONTINUE_AFTER_COMPACTION` user 訊息（同現行）。之後的請求以「摘要（user）＋下一則 user 訊息」開頭。

`_is_summary`、`_compactable` 改以 `role == SUMMARY` 判斷。

### 4.7 歷史重播（`kilo/agent/history.py`）

由儲存的訊息產生與直播相同的事件：

- `USER`：同現行（排程啟動訊息 → `run_started`；`CONTINUE_AFTER_COMPACTION` 略過；其他 → `user_message`）。
- `SUMMARY` → `compaction_done`（`data` 為空）。
- `ASSISTANT`（payload 為 `None` 時視為 `{"content": content}`）依序產生：
  - `progress`：`payload.reasoning` 非空時
  - `message_delta`：`content` 非空時
  - 每個 `tool_call` 產生 `tool_call_start`：名稱經 `canonical_name`；`arguments` 解析為 dict，失敗或非 dict 時為 `{}`
- `TOOL` → `tool_call_result`：以 `payload.tool_call_id` 對應名稱，結果用既有 `_result` 解析。
- 排程 run 的插入邏輯不變。

已知差異：同一 turn 中推理與文字交錯輸出時，重播會變成推理在前、文字在後（Chat Completions 不保留交錯順序）。

### 4.8 組裝

`kilo/assembly.py` 的 `chat_model` 改為建立 `OpenRouterClient()`。

## 5. 測試策略

**單元測試**（不連網）

- 新增 `tests/test_openrouter.py`（以 `httpx.MockTransport` 重播第 3 節錄到的 SSE 格式，加密資料縮短）：
  - 純文字回覆：`text` 事件與 `done`（`finish_reason`、`message`、`context_tokens == prompt + completion`）。
  - 平行工具呼叫、`arguments` 分段：合併後的 `tool_calls`。
  - `delta.reasoning` → `progress`；`reasoning_details` 依 index 合併（summary 串接、encrypted 保留）。
  - keepalive 註解行被忽略。
  - 請求 body：`model`、`reasoning.effort`、system 為第一則、`stream`；tools 為空時不送；`max_tokens` 不送／設定值／呼叫參數優先。
  - HTTP 400 帶 `metadata.raw`：錯誤訊息包含 provider 原始訊息。
  - 串流中途 error chunk、`finish_reason: error`、沒有 finish 就結束：皆丟出 `OpenRouterError`。
  - 429 或 503 後成功：有重試；400 不重試；重試最多 2 次；`Retry-After` 被採用（以注入的 `sleep` 驗證）。
  - 缺少 tool call id：自動產生。
  - 缺少 API key：丟出錯誤且不送請求。
- `tests/fakes.py`：`text_turn(text, finish_reason="stop")`、`tool_turn(call_id, name, args, finish_reason="tool_calls")` 產生新的 `done` 格式（`args` 為字串時原樣作為 `arguments`，否則 `json.dumps`）；`FakeModel` 另記錄每次呼叫的 `max_tokens`；移除 `compact()` 與 `compact_result`。
- `tests/test_runner.py`：改為新格式；壓縮測試改寫為以腳本化的摘要 turn 進行（摘要請求以指示結尾、下一次請求以摘要開頭、摘要時的工具呼叫被忽略、空白摘要算失敗、`length` 以兩倍 `max_tokens` 重試、工具迴圈中途壓縮後繼續）；`content_filter`、`length` 截斷。
- 其他：`test_storage.py`（`payload` 與 `SUMMARY`）、`test_config.py`（新設定名稱與界限、`.env.example` 對應）、`test_mcp_manager.py`（`parameters`）、`test_api.py`（health 的 model；`/compact` 需要腳本化的摘要 turn）、`conftest.py`（清除 `OPENROUTER_` 開頭的環境變數）。
- Telegram、排程測試透過 fakes 間接更新，測試本身不需修改。

**Live 測試**（`uv run pytest -m live`）

- key 從專案的 `.env`（已在 `.gitignore`）讀取 `OPENROUTER_API_KEY`；模型 `meta/muse-spark-1.3-contributor`；effort `low`。
- 沿用現有 5 個情境：回覆、工具迴圈（`write_file`）、多輪記憶、手動壓縮後仍記得、實際啟動 server 進程對話（health 的 model 為預設模型）。

**最後驗證**：成功標準 1 至 4。

## 6. 風險與後續

- **JSON Schema 相容性**：部分 provider 對工具 schema 較嚴格；MCP 伺服器提供的 schema 若被拒，會以 LLM error 顯示。目前不處理。
- **推理顯示量**：輸出完整推理過程的模型（非摘要）會讓 progress 很長；之後可加顯示開關。
- **快取**：只依賴隱式快取；沒有隱式快取的模型每輪成本較高，可用 `KILO_COMPACT_AT_TOKENS` 調整。
- **連續兩則 user 訊息**：已在 5 個 provider 上驗證可接受（第 3 節第 14 點）；其他 provider 未驗證。
- **contributor 方案的資料用途**：OpenRouter 說明未寫明；正式環境使用前請確認 OpenRouter 帳號的 privacy 設定。
- **測試 key 到期**：2026-11-04 台北 13:19 之後執行 live 測試需換新 key。

## 7. 受影響檔案

| 檔案 | 變更 |
|---|---|
| `kilo/openrouter.py` | 新增：`OpenRouterClient`、`OpenRouterError` |
| `kilo/claude.py` | 刪除 |
| `kilo/agent/chat_model.py` | 協定改為只有 `stream_turn(..., max_tokens=None)` |
| `kilo/agent/runner.py` | `_api_messages`、turn 處理、kilo 端壓縮、`SUMMARY_HEADER` |
| `kilo/agent/history.py` | 由 payload 重播、`SUMMARY` |
| `kilo/assembly.py` | 建立 `OpenRouterClient` |
| `kilo/config.py` | `OPENROUTER_*` 設定、移除 context 常數與上限驗證 |
| `kilo/models/session.py` | `payload`、`MessageRole.SUMMARY` |
| `kilo/storage/db.py` | 欄位 `payload` |
| `kilo/tools/base.py`、`toolbox.py`、`file_tools.py`、`search_tools.py`、`shell.py`、`mcp_tools.py`、`schedule_tools.py`、`mcp_manager.py` | `input_schema` → `parameters`；`definitions()` 包成 `tools` 陣列 |
| `kilo/server/app.py` | health 的 model；缺少 API key 時拒絕啟動 |
| `README.md`、`.env.example`、`pyproject.toml`、`uv.lock` | 文件、設定範例、移除 anthropic 相依 |
| `tests/test_openrouter.py` | 新增 |
| `tests/test_claude_client.py` | 刪除 |
| `tests/fakes.py`、`conftest.py`、`test_runner.py`、`test_storage.py`、`test_config.py`、`test_mcp_manager.py`、`test_api.py`、`live/test_live.py` | 依上述改寫 |
