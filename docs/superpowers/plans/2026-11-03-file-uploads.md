# Kilo 檔案上傳 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓使用者從網頁終端或 Telegram 傳任何檔案給 kilo。檔案存進 workspace 的 `uploads/`，agent 只會被告知檔案在哪裡，拿不到檔案內容。

**Architecture:**
- 新模組 `kilo/uploads.py` 負責存檔、命名、大小上限，以及給模型看的附件說明，網頁和 Telegram 共用。
- 使用者訊息的附件清單存在 `payload`；`_api_messages` 組請求時，才把說明接在訊息後面。
- 網頁：先用 raw-body 上傳 API 存檔，再透過聊天 API 的 `attachments` 欄位把附件帶進訊息。
- Telegram bot：下載檔案；沒有說明文字（caption）的檔案先保留，跟著同一個人的下一則訊息送出。同一個聊天的訊息依到達順序處理。

**Tech Stack:** Python ≥ 3.11、FastAPI／Starlette（`Request.stream()`）、aiosqlite、httpx（Telegram 下載）、pydantic、pytest + pytest-asyncio（`asyncio_mode = "auto"`）、uv；網頁是原生 ES modules。

**Spec:** `docs/superpowers/specs/2026-11-03-file-uploads-design.md`。本計畫以「spec §x」引用它。

## Global Constraints

- 檔案內容絕不能出現在送給模型的請求裡（spec §1）。
- 不新增套件依賴，`pyproject.toml`、`uv.lock` 都不動。上傳用 raw body，不用 multipart。
- 不做資料遷移：附件存在 messages 表的 `payload` 欄位；Telegram 的新資料表用 `CREATE TABLE IF NOT EXISTS` 建立。
- 存放位置：`<KILO_WORKSPACE>/uploads/<YYYY-MM-DD>/`，日期以 `KILO_TIMEZONE` 計算。
- `KILO_UPLOAD_MAX_MB`：整數，至少 1，預設 500。1 MB = 1024 × 1024 bytes。
- Telegram bot 可下載的上限是 `20 * 1024 * 1024` bytes。
- 相簿在最後一項到達後，再等 1 秒沒有新的一項就收齊。
- 文字一律用英文，並照 spec 原文：包括網頁終端、Telegram 回覆、給模型的說明。各 task 會列出它用到的原文。
- 程式碼的註解與 docstring 用英文，沿用專案風格：`typing` 型別標註、只寫說明「為什麼」的短註解。
- 測試：
  - 單元測試：`uv run pytest`，目前基準為 `379 passed, 5 deselected`。
  - live 測試：`uv run pytest -m live`，用專案 `.env` 裡的 key。
- 每個 commit 訊息的最後一行：`Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`。

## Review Focus

1. **兩個同名檔案同時上傳**（例如兩個分頁同時傳 `image.png`）：要存成兩個不同的檔案，任一個都不能被覆蓋 → Task 1 `test_names_taken_at_the_same_moment_stay_distinct`
2. **沒有 `Content-Length` 的上傳（chunked）超過上限**：要回 413，不留下任何部分檔案，也不能把整個 body 讀進記憶體 → Task 3 `test_upload_without_a_length_is_cut_off_at_the_limit`
3. **相簿還在收集時就送出的文字**：要帶上整本相簿的檔案，不能只帶到一部分，也不能漏掉 → Task 7 `test_text_sent_while_an_album_gathers_takes_it_along`
4. **檔案還在下載時送出的指令（`/stop`）**：要立刻回應，不能等下載完 → Task 6 `test_commands_are_answered_while_a_file_downloads`
5. **聊天請求裡的附件路徑寫法不尋常**：
   - 絕對路徑、或含 `..` 的路徑：要正規化成同一個相對路徑。
   - 經由 symlink 指到 `uploads/` 外面的路徑：要拒絕。

   → Task 1 `test_an_attachment_is_found_again_by_its_path`、`test_a_link_out_of_uploads_cannot_be_attached`

## 檔案結構

| 檔案 | 責任 | Task |
|---|---|---|
| `kilo/uploads.py`（新） | 存檔、檔名、大小上限、類型、附件說明 | 1 |
| `kilo/models/session.py` | `Attachment` 模型；`payload` 說明 | 1、2 |
| `kilo/config.py`、`.env.example` | `KILO_UPLOAD_MAX_MB` | 1 |
| `kilo/agent/runner.py`、`kilo/agent/history.py`、`kilo/workspace.py` | 訊息帶附件、system prompt、壓縮指示、重播 | 2 |
| `kilo/server/routes/uploads.py`（新）、`kilo/server/app.py`、`kilo/server/routes/sessions.py`、`kilo/models/api.py` | 上傳 API、聊天附件 | 3 |
| `kilo/web/index.html`、`kilo/web/static/*.js` | `/upload`、拖放、貼上、待送清單、顯示 | 4 |
| `kilo/telegram/api.py`、`kilo/telegram/store.py` | 下載、保留清單 | 5 |
| `kilo/telegram/bot.py`、`kilo/telegram/render.py` | 收檔、保留、順序、相簿 | 6、7 |
| `README.md`、`tests/live/test_live.py` | 文件、真實模型驗證 | 8 |

---

### Task 1: 存檔核心（`kilo/uploads.py`）與設定

**Files:**
- Create: `kilo/uploads.py`、`tests/test_uploads.py`
- Modify:
  - `kilo/models/session.py`：在 `Message` 之前新增 `Attachment`。
  - `kilo/config.py`：在 `kilo_tool_output_max_chars` 之後新增設定。
  - `.env.example`：在 Tools 一節加入新設定。
  - `tests/test_config.py`

**Interfaces:**
- Consumes: `settings.kilo_workspace`、`settings.kilo_timezone`、`kilo.workspace.resolve`、`kilo.workspace.root`
- Produces:
  - `kilo.models.session.Attachment(BaseModel)`：`path: str`、`name: str`、`type: str`、`size: int`
  - `settings.kilo_upload_max_mb: int = Field(500, ge=1)`
  - `kilo.uploads`：
    - `UPLOADS = "uploads"`
    - `class UploadTooLarge(Exception)`
    - `root() -> Path`：回傳 `settings.kilo_workspace / UPLOADS`。
    - `max_bytes() -> int`：每次呼叫時才讀設定。
    - `safe_name(name: str) -> str`
    - `type_of(name: str, declared: Optional[str]) -> str`
    - `async save(chunks: AsyncIterable[bytes], name: str, declared_type: Optional[str] = None, now: Optional[datetime] = None) -> Attachment`
      - `now` 只用來決定日期資料夾，預設為現在時間。這是 spec 介面之外為了測試加的參數。
    - `attachment_at(path: str, name: Optional[str] = None, declared_type: Optional[str] = None) -> Attachment`
    - `format_size(size: int) -> str`
    - `note(attachments: Sequence[Attachment]) -> str`
    - `with_note(text: str, attachments: Sequence[Attachment]) -> str`：
      - 沒有附件：回傳 `text`。
      - `text` 是空白：只回傳 `note(...)`。
      - 其他情況：回傳 `f"{text}\n\n{note(...)}"`。

- [ ] **Step 1: 寫失敗的測試 `tests/test_uploads.py`**

```python
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
```

- [ ] **Step 2: 在 `tests/test_config.py` 加上新設定的範圍測試**
  - `test_unusable_value_is_refused` 的參數加上 `("kilo_upload_max_mb", 0)`。
  - `test_values_at_the_limits_are_accepted` 的參數加上 `("kilo_upload_max_mb", 1)`。

`.env.example` 的預設值，由現有的 `test_default_matches_env_example` 自動比對。

- [ ] **Step 3: 跑測試，確認失敗**

Run: `uv run pytest tests/test_uploads.py tests/test_config.py -q`
Expected: FAIL。
- `test_uploads.py` 在收集階段就報 `ModuleNotFoundError: No module named 'kilo.uploads'`。
- `test_config.py` 中 `kilo_upload_max_mb` 的兩個案例失敗，因為 `Settings` 還沒有這個欄位。

- [ ] **Step 4: 加入 `Attachment`、設定與 `.env.example`**
  - `Attachment` 的 docstring：`A file the user sent, saved in uploads/ (kilo/uploads.py).`
  - `.env.example` 照 spec §5 原文加入三行（兩行註解加 `KILO_UPLOAD_MAX_MB=500`），放在 `KILO_TOOL_OUTPUT_MAX_CHARS` 之後。

- [ ] **Step 5: 實作 `kilo/uploads.py`**

規則照 spec §3.1。下面幾點是測試決定不了的：

- **`safe_name`**：
  - 先取最後一個 `/` 或 `\` 之後的部分，再用 `unicodedata.normalize("NFC", …)` 正規化。
  - 逐字元檢查：`unicodedata.category(c)[0] in "LMN"`，或 `c in " .-_()+,=@"` 就保留，其餘換成 `_`。
  - 接著 `lstrip(" .-")`、`rstrip(" ")`；結果是空的就用 `"file"`；最後套用 `_fit(stem, ext)`。
- **副檔名切分 `_split(name) -> (stem, ext)`**：
  - `i = name.rfind(".")`；`i > 0` 且 `len(name) - i <= 16` 時，`ext = name[i:]`。
  - 否則 `ext = ""`。
- **`_fit(stem, ext, suffix="")`**：
  - 把 `stem` 從尾端逐字元去掉，直到 `stem + suffix + ext` 的 UTF-8 不超過 255 bytes。
  - 只在字元邊界切。
- **`save` 的步驟**：
  1. 日期資料夾 `root() / (now or utc_now()).astimezone(ZoneInfo(settings.kilo_timezone)).date().isoformat()`，用 `mkdir(parents=True, exist_ok=True)` 建立。
  2. 依序試檔名 `safe`、`_fit(stem, ext, "-2")`、`-3`，以此類推，用 `open(path, "xb")` 建立；遇到 `FileExistsError` 就試下一個。
  3. 每個 chunk 先累加大小：超過 `max_bytes()` 就丟 `UploadTooLarge`，否則直接 `write`。
  4. 整段寫入包在 `try/except BaseException` 裡：任何例外（包括取消）都先 `unlink(missing_ok=True)` 再重新丟出。
- **`Attachment.name`**：
  - 原始檔名有非空白字元時，取前 255 個字元。
  - 否則用存檔時的檔名。
- **`type_of`**：
  1. `mimetypes.guess_type(name)[0]`。用全域表，Linux 上會包含系統的 `/etc/mime.types`。
  2. 判斷不出來時，改用 `declared`：取 `;` 之前的部分，`strip().lower()`，空的或是 `application/octet-stream` 都不算。
  3. 兩者都沒有就是 `"application/octet-stream"`。
- **`attachment_at`**：
  - 用 `workspace.resolve(path)` 取得真實路徑（含 symlink），必須同時符合兩點，否則丟 `ValueError(f"Attachment '{path}' is not a file in uploads/.")`：
    - `is_relative_to(root().resolve())`；
    - `is_file()`。
  - `path` 存成 `resolved.relative_to(workspace.root().resolve()).as_posix()`；`size` 用 `stat().st_size`。
  - `name` 沒給時，用路徑裡的檔名。
- **`note`**：
  - 第一行（原文）：
    `[Files sent with this message, saved in the workspace. Their contents are not included here; read them with your tools when you need them.`
  - 每個檔案一行：`- {path} ({type}, {format_size(size)})`。
    - `name` 不等於 `PurePosixPath(path).name` 時，括號內再加 `, sent as {json.dumps(name, ensure_ascii=False)}`。
  - 最後一行結尾接上 `]`。
- **`format_size`**：
  - 小於 1024：`f"{size} bytes"`。
  - 否則依序除以 1024，換成 `KB`、`MB`、`GB`，一位小數；到 `GB` 就不再往上。

- [ ] **Step 6: 跑測試，確認通過**

Run: `uv run pytest tests/test_uploads.py tests/test_config.py -q`
Expected: PASS

Run: `uv run pytest -q`
Expected: 全部通過（基準 379 加上本 task 新增的測試數），`5 deselected`。

- [ ] **Step 7: Commit**

```bash
git add kilo/uploads.py kilo/models/session.py kilo/config.py .env.example tests/test_uploads.py tests/test_config.py
git commit -m "feat: save uploaded files in the workspace's uploads folder" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: 訊息帶附件（runner、重播、system prompt）

**Files:**
- Modify:
  - `kilo/agent/runner.py`
  - `kilo/agent/history.py`
  - `kilo/workspace.py`
  - `kilo/models/session.py`：只改 `Message.payload` 的註解。
- Test: `tests/test_runner.py`

**Interfaces:**
- Consumes: Task 1 的 `Attachment`、`uploads.with_note`、`uploads.note`
- Produces:
  - `AgentRunner.run_chat_stream(session_id, user_message, max_turns=100, attachments: Sequence[Attachment] = ())`
  - `user_message` 事件的 `data` 多了 `"attachments"`：內容是 `[a.model_dump() for a in attachments]`，沒有附件時是 `[]`。
  - 存檔的使用者訊息：有附件時 `payload == {"attachments": [a.model_dump(), …]}`，沒有時是 `None`。
  - `history_events` 產生的 `user_message` 也帶 `attachments`。

- [ ] **Step 1: 在 `tests/test_runner.py` 加入失敗的測試**

```python
from kilo import uploads
from kilo.models.session import Attachment

PHOTO = Attachment(path="uploads/2026-11-03/photo.jpg", name="photo.jpg", type="image/jpeg", size=2048)


async def test_files_sent_are_stored_and_described_to_the_model(kilo, model, session):
    model.turns.append(text_turn("ok"))
    events = await collect(kilo, session.id, "what is this?", attachments=[PHOTO])
    [stored] = [m for m in await kilo.db.get_messages(session.id) if m.role == MessageRole.USER]
    assert stored.content == "what is this?"
    assert stored.payload == {"attachments": [PHOTO.model_dump()]}
    assert model.requests[0][-1] == {"role": "user", "content": f"what is this?\n\n{uploads.note([PHOTO])}"}
    assert events[0]["data"]["attachments"] == [PHOTO.model_dump()]


async def test_files_alone_are_sent_as_their_note_and_title_the_session(kilo, model, session):
    model.turns.append(text_turn("ok"))
    await collect(kilo, session.id, "", attachments=[PHOTO])
    assert model.requests[0][-1]["content"] == uploads.note([PHOTO])
    assert (await kilo.db.get_session(session.id)).title == "photo.jpg"


async def test_a_message_without_files_has_none(kilo, model, session):
    model.turns.append(text_turn("ok"))
    events = await collect(kilo, session.id, "hi")
    assert events[0]["data"]["attachments"] == []
    assert (await kilo.db.get_messages(session.id))[0].payload is None


async def test_history_shows_the_files_sent(kilo, model, session):
    model.turns.append(text_turn("ok"))
    await collect(kilo, session.id, "look", attachments=[PHOTO])
    replayed = history_events(await kilo.db.get_messages(session.id), kilo.toolbox.canonical_name)
    assert replayed[0]["data"]["attachments"] == [PHOTO.model_dump()]


def test_history_of_a_message_without_files_lists_none():
    message = SimpleNamespace(role=MessageRole.USER, created_at=None, content="hi", payload=None, id="m", session_id="s")
    assert history_events([message])[0]["data"]["attachments"] == []


def test_the_system_prompt_says_where_files_go(kilo):
    assert (
        "Files the user sends you are saved in `uploads/` (a folder per day); each one is listed in the message it came with."
        in kilo.runner._system_prompt()
    )


def test_a_summary_keeps_the_files_sent():
    assert "- Files the user sent, with their paths." in COMPACTION_INSTRUCTIONS


async def test_the_note_counts_toward_compaction(kilo, model, session, monkeypatch):
    monkeypatch.setattr(settings, "kilo_compact_at_tokens", 10_000)
    await kilo.db.add_message(session.id, MessageRole.USER, content="old question")
    await kilo.db.add_message(
        session.id, MessageRole.ASSISTANT, content="old answer", payload={"role": "assistant", "content": "old answer"},
    )
    await kilo.db.update_session(session.id, metadata={"context_tokens": 9_990})  # "hi" alone stays below it
    model.turns.extend([text_turn("the summary"), text_turn("ok")])
    events = await collect(kilo, session.id, "hi", attachments=[PHOTO])
    assert names(events)[:2] == ["compaction_start", "compaction_done"]
```

- [ ] **Step 2: 跑測試，確認失敗**

Run: `uv run pytest tests/test_runner.py -q`
Expected: FAIL。
- 帶 `attachments=` 的測試丟 `TypeError: ... unexpected keyword argument 'attachments'`。
- 歷史、system prompt、壓縮指示的測試在斷言時失敗（例如 `KeyError: 'attachments'`、`assert ... in ...`）。

- [ ] **Step 3: 實作**
  - **`runner.py`**：
    - `_api_messages` 處理 user 訊息的分支（目前是 `else`）改成：
      `{"role": "user", "content": uploads.with_note(msg.content or "", [Attachment.model_validate(a) for a in (msg.payload or {}).get("attachments") or []])}`
    - `run_chat_stream` 與 `_run_chat_stream` 新增 `attachments` 參數並往下傳。
      - `incoming = len(uploads.with_note(user_message, attachments))`。
      - 存訊息時帶 `payload`（沒有附件時是 `None`），事件帶 `attachments`。
      - 自動標題：`user_message.strip().split("\n")[0][:40]` 是空的就改用 `attachments[0].name[:40]`（沒有附件時維持空的）。
  - **`COMPACTION_INSTRUCTIONS`**：在 `- Files and directories created, …` 那一行之後，加一行 `- Files the user sent, with their paths.`
  - **`workspace.prompt_section()`**：在現有兩行之後加上 spec §3.2 的原文那一句。
  - **`history.py`**：user 訊息的事件資料加上 `"attachments": (msg.payload or {}).get("attachments") or []`。
  - **`Message.payload` 的註解**改成：
    `# The message as sent to OpenRouter (Chat Completions shape): an assistant turn as returned, or a tool result.`
    `# For a user message, the files sent with it ({"attachments": [...]}), when there are any. None otherwise.`

- [ ] **Step 4: 跑測試，確認通過**

Run: `uv run pytest tests/test_runner.py -q`
Expected: PASS

Run: `uv run pytest -q`
Expected: 全部通過。

- [ ] **Step 5: Commit**

```bash
git add kilo/agent/runner.py kilo/agent/history.py kilo/workspace.py kilo/models/session.py tests/test_runner.py
git commit -m "feat: tell the agent where the files sent with a message are" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: 上傳 API 與聊天附件

**Files:**
- Create: `kilo/server/routes/uploads.py`
- Modify:
  - `kilo/server/app.py`：在 sessions 之後 `include_router` 上傳 API。
  - `kilo/server/routes/sessions.py`
  - `kilo/models/api.py`
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes：
  - Task 1：`uploads.save`、`uploads.attachment_at`、`uploads.max_bytes`、`uploads.UploadTooLarge`、`Attachment`
  - Task 2：`run_chat_stream(..., attachments=...)`
- Produces:
  - `POST /api/v1/uploads?name=<檔名>`：body 是檔案內容，成功回 201，body 是 `Attachment`。
  - `kilo.models.api.AttachmentRef(BaseModel)`：`path: str`、`name: Optional[str] = None`、`type: Optional[str] = None`。其他欄位一律忽略。
  - `ChatRequest`：`message: str = ""`、`attachments: List[AttachmentRef] = []`、`stream: bool = True`。

- [ ] **Step 1: 在 `tests/test_api.py` 加入失敗的測試**

```python
async def upload(http, data: bytes = b"secret words", name: str = "notes.txt", content_type: str = "text/plain"):
    return await http.post(f"{API}/uploads", params={"name": name}, content=data, headers={"content-type": content_type})


async def test_upload_saves_the_body_as_a_file(authed, workspace):
    response = await upload(authed)
    assert response.status_code == 201
    body = response.json()
    assert body["path"].startswith("uploads/") and body["path"].endswith("/notes.txt")
    assert {k: body[k] for k in ("name", "type", "size")} == {"name": "notes.txt", "type": "text/plain", "size": 12}
    assert (workspace / body["path"]).read_bytes() == b"secret words"


@pytest.mark.parametrize("params", [{}, {"name": "  "}])
async def test_upload_needs_a_name(authed, params):
    response = await authed.post(f"{API}/uploads", params=params, content=b"x")
    assert response.status_code == 400 and response.json()["detail"] == "name is required."


async def test_upload_needs_login(client):
    assert (await upload(client)).status_code == 401


async def test_upload_over_the_limit_is_refused_unread(authed, workspace, monkeypatch):
    monkeypatch.setattr(settings, "kilo_upload_max_mb", 1)
    response = await upload(authed, b"x" * (1024 * 1024 + 1), "big.bin")
    assert response.status_code == 413
    assert response.json()["detail"] == "File is larger than the upload limit of 1 MB (KILO_UPLOAD_MAX_MB)."
    assert not (workspace / "uploads").exists()


async def test_upload_without_a_length_is_cut_off_at_the_limit(authed, workspace, monkeypatch):
    monkeypatch.setattr(settings, "kilo_upload_max_mb", 1)

    async def body():
        for _ in range(3):
            yield b"x" * (512 * 1024)

    response = await authed.post(f"{API}/uploads", params={"name": "big.bin"}, content=body())
    assert response.status_code == 413
    assert [p for p in (workspace / "uploads").rglob("*") if p.is_file()] == []


async def test_upload_reports_a_disk_error(authed, workspace):
    (workspace / "uploads").write_text("not a folder")
    response = await upload(authed)
    assert response.status_code == 500
    assert response.json()["detail"].startswith("Could not save notes.txt: ")


async def test_chat_sends_files_by_their_place_not_their_contents(authed, session_id, model):
    attachment = (await upload(authed)).json()
    model.turns.append(text_turn("ok"))
    reply = (await authed.post(f"{API}/sessions/{session_id}/chat", json={
        "message": "read this", "attachments": [attachment], "stream": False,
    })).json()
    sent = model.requests[0][-1]["content"]
    assert sent.startswith("read this\n\n[Files sent with this message") and attachment["path"] in sent
    assert "secret words" not in sent
    assert reply["events"][0]["data"]["attachments"] == [attachment]


async def test_chat_with_only_files(authed, session_id, model):
    attachment = (await upload(authed)).json()
    model.turns.append(text_turn("ok"))
    response = await authed.post(f"{API}/sessions/{session_id}/chat", json={"attachments": [attachment], "stream": False})
    assert response.status_code == 200
    assert model.requests[0][-1]["content"].startswith("[Files sent with this message")


@pytest.mark.parametrize("path", ["notes.txt", "uploads/2026-11-03/missing.txt"])
async def test_chat_refuses_files_not_in_uploads(authed, session_id, workspace, kilo, path):
    (workspace / "notes.txt").write_text("x")
    response = await authed.post(f"{API}/sessions/{session_id}/chat", json={"message": "hi", "attachments": [{"path": path}]})
    assert response.status_code == 400
    assert response.json()["detail"] == f"Attachment '{path}' is not a file in uploads/."
    assert await kilo.db.get_messages(session_id) == []
```

- [ ] **Step 2: 跑測試，確認失敗**

Run: `uv run pytest tests/test_api.py -q`
Expected: FAIL。
- 上傳相關的測試得到 404：路由還不存在。
- `test_upload_needs_login` 已經會通過：middleware 對所有 `/api/v1/*` 都回 401。它是防止退步用的，不是這個 task 才讓它通過。
- `test_chat_*` 因為上傳失敗、或 `attachments` 被忽略，在斷言時失敗。

- [ ] **Step 3: 實作 `kilo/server/routes/uploads.py`**

```python
@router.post("", response_model=Attachment, status_code=201)
async def upload(request: Request, name: Optional[str] = Query(None)) -> Attachment:
```

處理順序：
1. `name` 沒給或只有空白：`HTTPException(400, "name is required.")`。
2. `Content-Length` 是數字而且大於 `uploads.max_bytes()`：413，不讀 body。
3. 呼叫 `await uploads.save(request.stream(), name, request.headers.get("content-type"))`，並處理錯誤：
   - `UploadTooLarge`：413。
   - `OSError as e`：`HTTPException(500, f"Could not save {name}: {e}")`。
   - 兩種 413 的 detail 都是 `f"File is larger than the upload limit of {settings.kilo_upload_max_mb} MB (KILO_UPLOAD_MAX_MB)."`。

endpoint 不能宣告任何 body 參數，FastAPI 才不會先把整個 body 讀進來。模組 docstring 說明為什麼用 raw body（spec §3.3）。

- [ ] **Step 4: 實作聊天端**
  - `kilo/models/api.py` 加上 `AttachmentRef`，`ChatRequest` 改成上面的三個欄位。
  - `chat_session`：
    1. 確認 session 存在（404）之後，若 `not req.message.strip() and not req.attachments`，回 400 `Message cannot be empty.`。
    2. 依序呼叫 `uploads.attachment_at(a.path, a.name, a.type)`。遇到 `ValueError` 就回 `HTTPException(400, str(e))`，不呼叫 runner。
    3. 串流與非串流兩個分支都把 `attachments=` 傳給 `run_chat_stream`。

- [ ] **Step 5: 跑測試，確認通過**

Run: `uv run pytest tests/test_api.py -q`
Expected: PASS（現有的 `test_empty_message_400`、`test_unknown_session_404` 也照樣通過）

Run: `uv run pytest -q`
Expected: 全部通過。

- [ ] **Step 6: Commit**

```bash
git add kilo/server/routes/uploads.py kilo/server/app.py kilo/server/routes/sessions.py kilo/models/api.py tests/test_api.py
git commit -m "feat: upload files over the API and attach them to a message" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: 網頁終端

**Files:**
- Modify:
  - `kilo/web/index.html`
  - `kilo/web/static/api.js`
  - `kilo/web/static/main.js`
  - `kilo/web/static/commands.js`
  - `kilo/web/static/render.js`
  - `kilo/web/static/terminal.js`

**Interfaces:**
- Consumes: Task 3 的上傳 API，以及聊天 API 的 `attachments` 欄位；`user_message` 事件的 `attachments`（Task 2）。
- Produces:
  - `api.upload(file) -> Attachment`
  - `api.chat(sessionId, message, attachments, signal)`
  - `render.formatSize(bytes)`：規則跟 `uploads.format_size` 相同。
  - `render.attachmentLine(a)`：回傳 `` `  📎 ${a.path} (${formatSize(a.size)})` ``。

這個 task 沒有自動化測試：專案沒有 JS 測試環境，spec §4 定為用真的瀏覽器驗證，也就是 Step 7 的清單。Python 測試必須維持全部通過。

- [ ] **Step 1: `api.js`**
  - `request()`：body 是 `Blob`（含 `File`）時原樣送出，`content-type` 用 `body.type || "application/octet-stream"`；其他 body 照舊轉成 JSON。
  - 新增 `upload: async (file) => (await request("POST", withQuery("/api/v1/uploads", { name: file.name }), file)).json()`。
  - `chat(sessionId, message, attachments, signal)`：body 改成 `{ message, attachments, stream: true }`。

- [ ] **Step 2: `render.js`**
  - 新增 `formatSize` 和 `attachmentLine`。
  - `help()` 的指令表在 `/compact` 之前加兩列：
    - `["/upload", "Attach files to your next message (or drop them on the page, or paste them)"]`
    - `["/upload clear", "Forget the files waiting to be sent (they stay in uploads/)"]`

- [ ] **Step 3: `terminal.js`**
  - `ChatView.render` 處理 `user_message` 時，即時和重播都要顯示附件：在現有的 echo 之後，每個 `data.attachments` 項目各印一行 `attachmentLine(a)`（class `dim`）。
  - 使用者一送出，即時的 `user_message` 事件就會到，所以這同時涵蓋 spec §3.4「顯示」的兩點。

- [ ] **Step 4: `index.html`**
  - 在 `#screen` 裡、兩個 form 之外，加入 `<input id="file-picker" type="file" multiple hidden>`。

- [ ] **Step 5: `main.js`**

**狀態**：`app.pending = []`、`app.uploads = Promise.resolve()`、`app.uploading = 0`、`app.failedUploads = 0`。

**提示字元**：
- `app.updatePrompt()` 設成 `` `kilo [${id.slice(-8)}]${n ? ` 📎${n}` : ""}>` ``；沒有 session 時是 `kilo>`。
- `openSession` 改呼叫它，取代原本的 `term.setPrompt(...)`。

**`app.attach(files)`**：每個檔案依序串在 `app.uploads` 後面上傳，`app.uploading` 先加一，`finally` 時減一。每個檔案的處理：
- 開始：印 `Uploading ${file.name} (${formatSize(file.size)})…`（dim）。
- 成功：加進 `app.pending`，更新提示字元，印 `📎 ${file.name} → ${a.path} (${formatSize(a.size)})`。
- 失敗：`app.failedUploads += 1`，再看錯誤種類：
  - `LoggedOut`：呼叫 `showLogin(LOGIN_ENDED)`。
  - 其他：印 `Upload of ${file.name} failed: ${error.message}`（err）。

**附加檔案的入口**：
- `app.pickFiles()`：把 `#file-picker` 的 `value` 清空後呼叫 `click()`。啟動時在它的 `change` 事件上綁定 `app.attach(picker.files)`。
- 拖放：
  - `document` 的 `dragover`：`dataTransfer.types` 含 `"Files"` 時 `preventDefault()`。
  - `drop`：有檔案時 `preventDefault()`；未鎖定（`!term.locked`）才呼叫 `app.attach`。
- 貼上：`term.input` 的 `paste` 事件中，`clipboardData.files.length > 0` 時 `preventDefault()` 並呼叫 `app.attach`；只有文字時不攔。

**`app.clearPending()`**：
- 有待送檔案（n 個）：清空清單、更新提示字元，然後印：
  - `Cleared ${n} pending files; they stay in uploads/.`
  - n 為 1 時改印 `Cleared 1 pending file; it stays in uploads/.`
- 沒有待送檔案：印 `No files are waiting to be sent.`

**`showLogin`**：清空 `app.pending`，效果跟重新整理頁面一樣。

**`term.onSubmit`**：
- 只有 `!line && !app.pending.length && !app.uploading` 時才直接 return。
- 斜線指令照舊；其他輸入交給 `send(text, line)`：

```js
async function send(text, line) {
  if (app.uploading) {
    term.line("Waiting for uploads to finish…", "dim");
    const failures = app.failedUploads;
    const stopped = new Promise((resolve) => app.signal.addEventListener("abort", () => resolve(false), { once: true }));
    const finished = await Promise.race([app.uploads.then(() => true), stopped]);
    if (!finished || app.failedUploads > failures) {
      term.line(finished ? "Not sent: an upload failed. Your message is back at the prompt."
                         : "^C — not sent. Your message is back at the prompt.", finished ? "err" : "dim");
      term.input.value = text; // shown again when the prompt returns
      return;
    }
  }
  await chat(line);
}
```

**`chat(message)`**：
- 先複製 `const attachments = app.pending.slice()`，再呼叫 `api.chat(app.sessionId, message, attachments, app.signal)`。
- 收到第一個事件時，只從 `app.pending` 移除這次送出的那幾個，再更新提示字元。回覆中才拖進來的檔案要留著。
- 錯誤處理：`ApiError` 印 `error.message`（err），其餘錯誤照舊。

- [ ] **Step 6: `commands.js`**
  - `COMMANDS` 加上 `"/upload": ["clear"]`。
  - `runCommand` 加上 `case "/upload"`：
    - `sub === "clear"`：呼叫 `app.clearPending()`。
    - 沒有 `sub`：呼叫 `app.pickFiles()`。這一步要在 `runCommand` 遇到第一個 `await` 之前執行，才能延續按 Enter 的使用者操作，瀏覽器才肯開檔案視窗。
    - 其他：印 `Usage: /upload | /upload clear`。

- [ ] **Step 7: 用真的瀏覽器驗證（Chrome DevTools MCP）**

先在 scratchpad 準備測試檔：
- `notes.txt`：內容 `The code word is PAPAYA.` 加一個換行，共 25 bytes。
- `small.txt`：約 300 KB。
- `big.bin`：2 MB。

從專案資料夾在背景啟動 server，`.env` 會提供 key，登入用預設帳密：

`KILO_WORKSPACE=<scratch>/ws KILO_DATA_DIR=<scratch>/data KILO_MCP_CONFIG=<scratch>/data/mcp_servers.json KILO_PORT=8765 KILO_UPLOAD_MAX_MB=1 uv run kilo-server`

開 `http://127.0.0.1:8765` 登入後，逐項確認，每項都要看到括號裡的結果：

1. **`/upload` 選檔**：輸入 `/upload` 後，對 `#file-picker` 設定 `notes.txt`。
   - 優先用 `upload_file` 工具；如果碰不到隱藏的 input，就用 `evaluate_script` 透過 `DataTransfer` 指定檔案，再觸發 `change` 事件。
   - （畫面出現 `Uploading notes.txt (25 bytes)…` 和 `📎 notes.txt → uploads/<今天>/notes.txt (25 bytes)`，提示字元變成 `kilo [xxxxxxxx] 📎1>`。）
2. **只送檔案**：輸入框空白直接按 Enter。
   - （你那行下面出現 `  📎 uploads/<今天>/notes.txt (25 bytes)`，agent 讀檔後回覆提到 PAPAYA，提示字元恢復成沒有 📎。）
3. **拖放**：用 `evaluate_script` 在 `document` 上觸發一個帶 `File` 的 `drop` 事件。
   - （出現 📎 行，數量加 1。）
4. **貼上**：用 `evaluate_script` 在 `#input` 上觸發一個帶 `File` 的 `paste` 事件。
   - （出現 📎 行，數量再加 1。）
   - 再觸發一次只有文字的貼上。（文字照常貼進輸入框，沒有上傳。）
5. **換 session 保留待送清單**：執行 `/session new`。
   - （新的提示字元仍有 `📎2`。）
6. **`/upload clear`**：
   - 清單有 2 個時。（印 `Cleared 2 pending files; they stay in uploads/.`）
   - 再執行一次。（印 `No files are waiting to be sent.`）
7. **等上傳完再送**：用 `emulate` 把網路設成 Slow 3G，附加 `small.txt` 後立刻輸入 `hi` 按 Enter。
   - （先印 `Waiting for uploads to finish…`，上傳完才送出，附件列在 `hi` 下方。）
8. **等待中上傳失敗**：維持 Slow 3G，依序附加 `small.txt`、`big.bin`，在第一個上傳期間輸入 `look` 按 Enter。
   - （出現 `Upload of big.bin failed: File is larger than the upload limit of 1 MB (KILO_UPLOAD_MAX_MB).`，接著 `Not sent: an upload failed. Your message is back at the prompt.`；輸入框裡是 `look`，`small.txt` 留在待送清單。）
9. **等待中按 Esc**：附加 `small.txt`，輸入 `wait` 按 Enter 後立刻按 Esc。
   - （出現 `^C — not sent. Your message is back at the prompt.`，輸入框裡是 `wait`；上傳完成後 📎 數量加 1。）
10. **重新打開對話**：用 `/session switch <第 2 項的 session>` 切回去。
    - （重播時，那則訊息下方有 📎 行。）
11. **說明**：執行 `/help`。
    - （列出 `/upload`、`/upload clear` 兩列。）

驗證完停掉 server，刪掉 scratchpad 的 ws 和 data。

- [ ] **Step 8: 跑 Python 測試**

Run: `uv run pytest -q`
Expected: 全部通過；`test_public_paths_need_no_login` 會確認靜態檔照常提供。

- [ ] **Step 9: Commit**

```bash
git add kilo/web
git commit -m "feat: attach files in the web terminal with /upload, drag and drop or paste" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Telegram：下載與保留清單

**Files:**
- Modify: `kilo/telegram/api.py`、`kilo/telegram/store.py`
- Test: `tests/test_telegram.py`

**Interfaces:**
- Consumes: `Attachment`（Task 1）
- Produces:
  - `BotAPI.download(file_path: str) -> AsyncIterator[bytes]`（async generator）。
    - 失敗時丟 `TelegramError`，訊息是 `download: HTTP <status>` 或 `download: <例外類別名稱>`，不帶網址，`__cause__` 為 `None`。
  - `TelegramStore.hold(chat_id: int, user_id: int, attachments: Sequence[Attachment]) -> None`
  - `TelegramStore.take(chat_id: int, user_id: int) -> List[Attachment]`：依 `id` 排序取出，在同一個交易裡刪除。

- [ ] **Step 1: 寫失敗的測試**（放在 `# The Bot API client` 一節之後）

```python
from kilo.models.session import Attachment

HELD_PHOTO = Attachment(path="uploads/2026-11-03/photo-1.jpg", name="photo-1.jpg", type="image/jpeg", size=10)
HELD_DOC = Attachment(path="uploads/2026-11-03/a.pdf", name="a.pdf", type="application/pdf", size=20)


async def test_download_streams_the_file():
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, content=b"photo bytes")

    assert b"".join([chunk async for chunk in mock_api(handler).download("photos/file_1.jpg")]) == b"photo bytes"
    assert seen == ["/file/bot123:SECRET/photos/file_1.jpg"]


async def test_download_errors_never_show_the_token():
    def missing(request):
        return httpx.Response(404)

    def down(request):
        raise httpx.ConnectError(f"cannot reach {request.url}")

    for handler, text in ((missing, "download: HTTP 404"), (down, "download: ConnectError")):
        with pytest.raises(TelegramError) as error:
            async for _ in mock_api(handler).download("photos/file_1.jpg"):
                pass
        assert str(error.value) == text and error.value.__cause__ is None


async def test_held_files_are_taken_once_in_order(tmp_path):
    store = TelegramStore(tmp_path / "kilo.db")
    await store.init()
    await store.hold(USER, USER, [HELD_PHOTO])
    await store.hold(USER, USER, [HELD_DOC])
    await store.hold(GROUP, USER, [HELD_DOC])
    assert await store.take(USER, USER) == [HELD_PHOTO, HELD_DOC]
    assert await store.take(USER, USER) == []
    assert await store.take(GROUP, USER) == [HELD_DOC]


async def test_held_files_outlast_the_store(tmp_path):
    first = TelegramStore(tmp_path / "kilo.db")
    await first.init()
    await first.hold(USER, USER, [HELD_PHOTO])
    again = TelegramStore(tmp_path / "kilo.db")
    await again.init()
    assert await again.take(USER, USER) == [HELD_PHOTO]
```

- [ ] **Step 2: 跑測試，確認失敗**

Run: `uv run pytest tests/test_telegram.py -q -k "download or held_files"`
Expected: FAIL，訊息是 `AttributeError: 'BotAPI' object has no attribute 'download'`，以及 `'TelegramStore' object has no attribute 'hold'`。

- [ ] **Step 3: 實作**
  - **`BotAPI`**：
    - `__init__` 加上 `self._files = f"{base_url}/file/bot{token}/"`。
    - `download` 用 `self._client.stream("GET", self._files + file_path, timeout=httpx.Timeout(10.0, read=60.0))`，邊收邊 `yield` 給呼叫端（`aiter_bytes()`）。
    - 狀態碼不是 200：丟 `TelegramError(f"download: HTTP {status}", status)`。
    - `httpx.HTTPError`：丟 `TelegramError(f"download: {type(e).__name__}") from None`。
  - **`TelegramStore.init`**：
    - 加入 spec §3.5 的 `telegram_held_files` 資料表，`attachment` 欄位存 `Attachment.model_dump_json()`。
    - 模組 docstring 要提到保留中的檔案。

- [ ] **Step 4: 跑測試，確認通過**

Run: `uv run pytest tests/test_telegram.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add kilo/telegram/api.py kilo/telegram/store.py tests/test_telegram.py
git commit -m "feat: download Telegram files and keep files waiting for a user's next message" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Telegram bot：收檔、保留、順序

**Files:**
- Modify: `kilo/telegram/bot.py`、`kilo/telegram/render.py`
- Test: `tests/test_telegram.py`

**Interfaces:**
- Consumes：
  - Task 1：`uploads.save`、`uploads.max_bytes`、`uploads.UploadTooLarge`、`uploads.format_size`
  - Task 2：`run_chat_stream(..., attachments=...)`
  - Task 5：`BotAPI.download`、`TelegramStore.hold/take`
- Produces（Task 7 會用到）:
  - `TelegramBot._take_in(chat_id, message)`：決定訊息要立刻處理，還是排進 intake。
  - `TelegramBot._take(chat_id, item)`：處理一個 intake 項目，`item` 是一則訊息的 dict。
  - `Intake` dataclass（`queue`、`worker`、`busy`）；`self._intakes: Dict[int, Intake]`。

- [ ] **Step 1: 更新測試替身與 fixture**

`FakeAPI` 加上：

```python
        self.files: Dict[str, Tuple[str, bytes]] = {}  # file id -> (file path, content), for getFile and download
        self.too_big: Set[str] = set()  # file ids getFile refuses as too big
        self.broken: Set[str] = set()  # file ids whose download fails
        self.gate: Optional[asyncio.Event] = None  # downloads wait for it while it is set and closed

    def add_file(self, file_id: str, file_path: str, content: bytes) -> None:
        self.files[file_id] = (file_path, content)

    async def download(self, file_path: str):
        if self.gate is not None:
            await self.gate.wait()
        file_id = next(i for i, (path, _) in self.files.items() if path == file_path)
        if file_id in self.broken:
            raise TelegramError("download: ConnectError")
        content = self.files[file_id][1]
        for start in range(0, len(content), 4):
            yield content[start:start + 4]
```

`call` 加上 `getFile` 的處理：
- 在 `too_big` 裡：丟 `TelegramError("getFile: Bad Request: file is too big", 400)`。
- 其他：回 `{"file_id": …, "file_path": self.files[file_id][0]}`。

其他調整：
- `FRIEND = 43`，並加進 `bot` fixture 的允許名單：`frozenset({USER, FRIEND})`。
- 新增建立訊息的輔助函式：

```python
def photo(file_id: str = "p1", size: int = 8) -> List[Dict[str, Any]]:
    return [{"file_id": f"{file_id}-small", "file_size": 2}, {"file_id": file_id, "file_size": size}]


def document(file_id="d1", name="report.pdf", size=6, mime="application/pdf") -> Dict[str, Any]:
    item = {"file_id": file_id, "file_name": name, "mime_type": mime}
    if size is not None:
        item["file_size"] = size
    return item
```

- `idle()` 要先等每個 `bot._intakes` 的 `worker`，再等每個 chat 的 `worker`。
- 刪掉 `test_only_text_is_taken`，由下面的 `test_stickers_are_not_supported` 取代。
- 檔頭補上 `import re`、`from kilo.config import settings` 與 typing 的 `Optional`、`Set`、`Tuple`。

- [ ] **Step 2: 寫失敗的測試**（新的一節 `# Files`）

```python
async def test_a_file_with_a_caption_goes_to_the_agent_with_it(bot, api, model, workspace):
    api.add_file("d1", "documents/file_1.pdf", b"%PDF-1")
    model.turns.append(text_turn("A report."))
    await bot.handle(message(caption="summarize", document=document()))
    await idle(bot)
    [request] = model.requests
    content = request[-1]["content"]
    assert content.startswith("summarize\n\n[Files sent with this message")
    path = re.search(r"- (uploads/\S+/report\.pdf) \(application/pdf, 6 bytes\)", content).group(1)
    assert (workspace / path).read_bytes() == b"%PDF-1"
    assert api.sent()[-1] == "A report."


async def test_a_file_alone_waits_for_the_next_message(bot, api, model):
    api.add_file("p1", "photos/file_2.jpg", b"jpegdata")
    model.turns.append(text_turn("A cat."))
    await bot.handle(message(photo=photo()))
    await idle(bot)
    assert model.requests == []
    assert re.fullmatch(r"📎 Saved photo-\d{6}\.jpg \(8 bytes\)\. It goes to the agent with your next message\.", api.sent()[-1])
    await bot.handle(message("what is this?"))
    await idle(bot)
    [request] = model.requests
    assert request[-1]["content"].startswith("what is this?\n\n[Files sent with this message")
    assert re.search(r"- uploads/\S+/photo-\d{6}\.jpg \(image/jpeg, 8 bytes\)", request[-1]["content"])


async def test_text_sent_while_a_file_downloads_takes_it_along(bot, api, model):
    api.add_file("p1", "photos/file_2.jpg", b"jpegdata")
    api.gate = asyncio.Event()
    model.turns.append(text_turn("ok"))
    await bot.handle(message(photo=photo()))
    await bot.handle(message("here it is"))
    assert model.requests == []
    api.gate.set()
    await idle(bot)
    [request] = model.requests
    assert "photo-" in request[-1]["content"]


async def test_commands_are_answered_while_a_file_downloads(bot, api):
    api.add_file("p1", "photos/file_2.jpg", b"jpegdata")
    api.gate = asyncio.Event()
    await bot.handle(message(photo=photo()))
    await bot.handle(message("/stop"))
    assert api.sent() == ["Nothing is running."]
    api.gate.set()
    await idle(bot)


async def test_commands_leave_held_files_for_the_next_message(bot, api, model):
    api.add_file("p1", "photos/file_2.jpg", b"jpegdata")
    model.turns.append(text_turn("ok"))
    await bot.handle(message(photo=photo()))
    await idle(bot)
    await bot.handle(message("/new"))
    await bot.handle(message("look"))
    await idle(bot)
    assert "photo-" in model.requests[0][-1]["content"]


async def test_held_files_outlast_a_restart(bot, api, model, kilo, tmp_path):
    api.add_file("p1", "photos/file_2.jpg", b"jpegdata")
    await bot.handle(message(photo=photo()))
    await idle(bot)
    restarted = TelegramBot(
        api, TelegramStore(tmp_path / "kilo.db"), kilo.db, kilo.runner, kilo.scheduler, kilo.run_announcer,
        frozenset({USER}), edit_seconds=0,
    )
    await restarted.store.init()
    restarted.me = bot.me
    model.turns.append(text_turn("ok"))
    await restarted.handle(message("look"))
    await idle(restarted)
    assert "photo-" in model.requests[0][-1]["content"]
    await restarted.stop()


async def test_a_file_too_big_for_telegram_is_not_fetched(bot, api):
    await bot.handle(message(document=document("d9", "film.mp4", 21 * 1024 * 1024, "video/mp4")))
    await idle(bot)
    assert api.sent() == [
        "film.mp4 (21.0 MB) is too big for a Telegram bot, which can only fetch files up to 20 MB. "
        "Send it from the web terminal instead."
    ]
    assert not [c for c in api.calls if c[0] == "getFile"]


async def test_a_file_over_the_upload_limit_is_not_fetched(bot, api, monkeypatch):
    monkeypatch.setattr(settings, "kilo_upload_max_mb", 1)
    await bot.handle(message(document=document("d9", "big.zip", 2 * 1024 * 1024, "application/zip")))
    await idle(bot)
    assert api.sent() == ["big.zip (2.0 MB) is larger than the upload limit of 1 MB (KILO_UPLOAD_MAX_MB)."]
    assert not [c for c in api.calls if c[0] == "getFile"]


async def test_a_file_without_a_size_is_checked_as_it_is_saved(bot, api, monkeypatch, workspace):
    monkeypatch.setattr(settings, "kilo_upload_max_mb", 1)
    api.add_file("v1", "videos/file_3.mp4", b"x" * (1024 * 1024 + 1))
    await bot.handle(message(video={"file_id": "v1", "mime_type": "video/mp4"}))
    await idle(bot)
    assert api.sent() == ["The video is larger than the upload limit of 1 MB (KILO_UPLOAD_MAX_MB)."]
    assert [p for p in workspace.rglob("*") if p.is_file()] == []


async def test_telegram_refusing_a_big_file_is_explained(bot, api):
    api.add_file("v1", "videos/file_3.mp4", b"")
    api.too_big.add("v1")
    await bot.handle(message(video={"file_id": "v1"}))
    await idle(bot)
    assert api.sent() == [
        "The video is too big for a Telegram bot, which can only fetch files up to 20 MB. Send it from the web terminal instead."
    ]


async def test_a_failed_download_is_reported_and_nothing_held(bot, api, model):
    api.add_file("d1", "documents/file_1.pdf", b"%PDF-1")
    api.broken.add("d1")
    model.turns.append(text_turn("ok"))
    await bot.handle(message(document=document()))
    await idle(bot)
    assert api.sent() == ["Could not fetch report.pdf from Telegram (download: ConnectError). Send it again."]
    await bot.handle(message("hi"))
    await idle(bot)
    assert model.requests[0][-1]["content"] == "hi"


async def test_stickers_are_not_supported(bot, api):
    await bot.handle(message(sticker={"file_id": "s1"}))
    assert api.sent() == ["Only text and files are supported."]


async def test_group_files_not_meant_for_the_bot_are_not_fetched(bot, api):
    api.add_file("p1", "photos/file_2.jpg", b"jpegdata")
    await bot.handle(group(photo=photo()))
    await idle(bot)
    assert api.calls == []


async def test_a_caption_that_only_mentions_the_bot_is_held(bot, api, model):
    api.add_file("p1", "photos/file_2.jpg", b"jpegdata")
    await bot.handle(group(caption="@kilo_bot", photo=photo()))
    await idle(bot)
    assert model.requests == []
    assert api.sent(GROUP)[-1].startswith("📎 Saved photo-")


async def test_held_files_go_only_with_their_senders_message(bot, api, model):
    api.add_file("p1", "photos/file_2.jpg", b"jpegdata")
    model.turns += [text_turn("hi friend"), text_turn("a photo")]
    await bot.handle(group(caption="@kilo_bot", photo=photo()))
    await idle(bot)
    await bot.handle(group("@kilo_bot hello", user=FRIEND))
    await idle(bot)
    await bot.handle(group("@kilo_bot what is it?"))
    await idle(bot)
    first, second = model.requests
    assert "Files sent" not in first[-1]["content"]
    assert "photo-" in second[-1]["content"] and "[Sent by Ann (@ann)" in second[-1]["content"]
```

- [ ] **Step 3: 跑測試，確認失敗**

Run: `uv run pytest tests/test_telegram.py -q`
Expected: FAIL。新加的檔案測試失敗：bot 仍回 `Only text messages are supported for now.`，或因為沒有任何回覆而失敗。原有的文字訊息測試照樣通過。
`test_group_files_not_meant_for_the_bot_are_not_fetched` 已經會通過：群組的過濾本來就有。它用來確保這個 task 改了 `handle` 之後，過濾仍然有效。

- [ ] **Step 4: `render.py` 加入回覆文字**（原文照 spec §3.5）

```python
UNSUPPORTED = "Only text and files are supported."

def saved(attachments: Sequence[Attachment]) -> str: ...
def too_big(label: str, size: Optional[int]) -> str: ...
def over_limit(label: str, size: Optional[int], limit_mb: int) -> str: ...
def not_fetched(label: str, reason: str) -> str: ...
```

- 有大小時，`label` 寫成 `f"{label} ({format_size(size)})"`；沒有大小時只寫 `label`。
- `saved` 的兩種寫法：
  - 一個檔案：`📎 Saved {name} ({size}). It goes to the agent with your next message.`
  - 多個檔案：`📎 Saved {n} files ({總大小}). They go to the agent with your next message.`

- [ ] **Step 5: `bot.py` 實作**

**常數**：
- `TELEGRAM_FILE_LIMIT = 20 * 1024 * 1024`
- `FILE_FIELDS = ("animation", "document", "photo", "video", "audio", "voice", "video_note")`
- 沒有檔名時的名稱前綴（`video_note` 用 `video-note`，其餘跟欄位同名）。
- `<label>` 的說法：`The animation`、`The document`、`The photo`、`The video`、`The audio file`、`The voice message`、`The video message`。

**`TelegramFile(kind, file_id, name, mime_type, size)`** frozen dataclass：
- 有 `label` property：有 `name` 就用 `name`，沒有就用上面的說法。
- `file_of(message) -> Optional[TelegramFile]`：依 `FILE_FIELDS` 的順序，取第一個有值的欄位；`photo` 取 list 的最後一項（最大尺寸）。

**`Intake`** dataclass：`queue: asyncio.Queue`、`worker: Optional[asyncio.Task]`、`busy` property，寫法同 `Chat`。

**`handle`**：
- 指令判斷不變。
- `text is None and file_of(message) is None` 時，回 `render.UNSUPPORTED`。
- 其他情況改為 `await self._take_in(chat_id, message)`。

**`_take_in`**：

```python
intake = self._intakes.setdefault(chat_id, Intake())
if "text" in message and not intake.busy:
    await self._take(chat_id, message)  # nothing sent before it is still being taken in: at once, as before
    return
intake.queue.put_nowait(message)
if not intake.busy:
    intake.worker = self._spawn(self._take_all(chat_id, intake))
```

**`_take_all`**：依序處理 `queue` 裡的每一項，每項都 `try/except Exception`，失敗時用 `logger.exception` 記錄，跟 `_work` 一樣。

**`_take(chat_id, message)`**：
- `messages = [message]`、`first = messages[0]`、`sender = first.get("from") or {}`、`reply = self._reply(first)`。
- **文字訊息**：做法同現有的 `handle`。
  - 群組：用 `_unmention`，再加上 `[Sent by …]`；這段抽成 `_signed(message, text)`。
  - `text.strip()` 不是空的才呼叫 `_accept(chat_id, sender, text, [], reply)`，然後 return。
- **檔案訊息**：
  1. 取說明文字：`caption = first.get("caption") or ""`；群組先 `_unmention`。
  2. 決定文字：`text = (self._signed(first, caption) if 群組 else caption) if caption.strip() else ""`。
  3. 對每個 `file_of(m)` 依序 `await self._fetch(...)`，收集成功的附件。
  4. 有文字：呼叫 `_accept(…, text, attachments, reply)`。
  5. 沒有文字但有附件：呼叫 `self.store.hold(chat_id, sender["id"], attachments)`，再 `say(chat_id, render.saved(attachments), reply=reply)`。

**`_accept(chat_id, sender, text, files, reply)`**：
- 先 `held = await self.store.take(chat_id, sender.get("id"))`。
- 再 `await self._enqueue(chat_id, text, reply, [*held, *files])`。

**`_fetch(chat_id, file, reply) -> Optional[Attachment]`**：

1. **下載前的大小檢查**：
   - `file.size` 超過 `TELEGRAM_FILE_LIMIT`：回 `too_big`。
   - 否則超過 `uploads.max_bytes()`：回 `over_limit`（帶大小）。
2. **下載**：
   - `getFile` 取 `file_path`。結果沒有 `file_path` 時，當成太大處理：回 `too_big`（不帶大小）。
   - 沒有檔名時，產生 `f"{prefix}-{datetime.now(ZoneInfo(settings.kilo_timezone)):%H%M%S}{PurePosixPath(file_path).suffix}"`。
   - 回傳 `await uploads.save(self.api.download(file_path), name, file.mime_type)`。
3. **錯誤處理**：
   - `UploadTooLarge`：回 `over_limit`，不帶大小。
   - `TelegramError`：訊息含 `"file is too big"` 時回 `too_big`（不帶大小），其他回 `not_fetched(label, str(e))`。
   - `OSError`：回 `not_fetched(label, str(e))`。
   - 以上情況都回傳 `None`。
4. 所有回覆都用 `self.say(chat_id, …, reply=reply)`。

**對話佇列帶附件**：
- `Chat.queue` 的項目改成 `(session id, text, message id, attachments)`。
- `_enqueue(chat_id, text, reply=None, attachments=())`。
- `_work`、`_turn` 把附件一路傳到 `run_chat_stream(..., attachments=attachments)`。
- `/stop` 清掉的等待中訊息，連同附件一起丟掉。

**`stop()`**：intake worker 都是用 `self._spawn` 建立的，原本的取消流程就會一併處理，不需要另外改。

- [ ] **Step 6: 跑測試，確認通過**

Run: `uv run pytest tests/test_telegram.py -q`
Expected: PASS

Run: `uv run pytest -q`
Expected: 全部通過。

- [ ] **Step 7: Commit**

```bash
git add kilo/telegram/bot.py kilo/telegram/render.py tests/test_telegram.py
git commit -m "feat: take files in Telegram, holding uncaptioned ones for the next message" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Telegram 相簿

**Files:**
- Modify: `kilo/telegram/bot.py`
- Test: `tests/test_telegram.py`

**Interfaces:**
- Consumes: Task 6 的 `_take_in`、`_take`、`Intake`
- Produces：
  - `ALBUM_SECONDS = 1.0`
  - `TelegramBot.__init__(…, album_seconds: float = ALBUM_SECONDS)`
  - `Album` dataclass：`messages`、`complete: asyncio.Event`、`timer`

- [ ] **Step 1: 更新 fixture，寫失敗的測試**

`bot` fixture 加上 `album_seconds=0.05`。新的一節 `# Albums`：

```python
async def test_an_album_is_one_message(bot, api, model):
    for n in (1, 2, 3):
        api.add_file(f"p{n}", f"photos/file_{n}.jpg", b"jpegdata")
    model.turns.append(text_turn("Three photos."))
    await bot.handle(message(caption="compare these", photo=photo("p1"), media_group_id="g1", message_id=1))
    await bot.handle(message(photo=photo("p2"), media_group_id="g1", message_id=2))
    await bot.handle(message(photo=photo("p3"), media_group_id="g1", message_id=3))
    await idle(bot)
    [request] = model.requests
    assert request[-1]["content"].startswith("compare these\n\n[Files sent")
    assert request[-1]["content"].count("- uploads/") == 3


async def test_an_album_without_a_caption_is_held_with_one_reply(bot, api, model):
    for n in (1, 2):
        api.add_file(f"p{n}", f"photos/file_{n}.jpg", b"jpegdata")
    await bot.handle(message(photo=photo("p1"), media_group_id="g1", message_id=1))
    await bot.handle(message(photo=photo("p2"), media_group_id="g1", message_id=2))
    await idle(bot)
    assert model.requests == []
    assert api.sent() == ["📎 Saved 2 files (16 bytes). They go to the agent with your next message."]


async def test_text_sent_while_an_album_gathers_takes_it_along(bot, api, model):
    for n in (1, 2):
        api.add_file(f"p{n}", f"photos/file_{n}.jpg", b"jpegdata")
    model.turns.append(text_turn("ok"))
    await bot.handle(message(photo=photo("p1"), media_group_id="g1", message_id=1))
    await bot.handle(message(photo=photo("p2"), media_group_id="g1", message_id=2))
    await bot.handle(message("these two"))
    await idle(bot)
    [request] = model.requests
    assert request[-1]["content"].startswith("these two\n\n[Files sent")
    assert request[-1]["content"].count("- uploads/") == 2


async def test_a_group_album_counts_when_one_item_is_meant_for_the_bot(bot, api, model):
    for n in (1, 2):
        api.add_file(f"p{n}", f"photos/file_{n}.jpg", b"jpegdata")
    model.turns.append(text_turn("ok"))
    await bot.handle(group(caption="@kilo_bot which is better?", photo=photo("p1"), media_group_id="g1", message_id=1))
    await bot.handle(group(photo=photo("p2"), media_group_id="g1", message_id=2))
    await idle(bot)
    [request] = model.requests
    assert request[-1]["content"].count("- uploads/") == 2


async def test_a_group_album_not_meant_for_the_bot_is_not_fetched(bot, api):
    api.add_file("p1", "photos/file_1.jpg", b"jpegdata")
    await bot.handle(group(photo=photo("p1"), media_group_id="g1", message_id=1))
    await idle(bot)
    assert not [c for c in api.calls if c[0] == "getFile"]
```

- [ ] **Step 2: 跑測試，確認失敗**

Run: `uv run pytest tests/test_telegram.py -q -k "album"`
Expected: FAIL。
- 相簿的每一項都被當成獨立的訊息：例如 `count("- uploads/") == 1`，或回了好幾次「已存」。
- 群組相簿的第二項在 `_meant_for_me` 就被擋掉了。
- `test_a_group_album_not_meant_for_the_bot_is_not_fetched` 已經會通過。它確保這個 task 把群組判斷挪到收齊之後，沒對 bot 說的相簿仍然不會被下載。

- [ ] **Step 3: 實作**

**`handle`**：群組的檢查改成 `if not message.get("media_group_id") and not self._meant_for_me(message): return`。相簿留到收齊後整體判斷。

**`_take_in`**：訊息有 `media_group_id` 時，以 `key = (chat_id, media_group_id)` 查 `self._albums`。
- **已經有這本相簿**：把訊息加進 `messages`，取消舊的 `timer`，重新 `_spawn(self._close_album(key, album))`，然後 return。
- **還沒有**：建立 `Album([message])`，存進 `self._albums`，啟動 timer，然後把這本相簿當成一個項目，照一般訊息的方式排進 intake。
- 只有純文字訊息走「立刻處理」的那條路。

**`_close_album(key, album)`**：
- `await asyncio.sleep(self.album_seconds)`。
- `self._albums.pop(key, None)`，然後 `album.complete.set()`。

**`_take`**：
- 項目是 `Album` 時，先 `await item.complete.wait()`，再設 `messages = item.messages`。
- 進入檔案分支前加上：`if is_group(chat_id) and not any(self._meant_for_me(m) for m in messages): return`。
- 說明文字改取 `next((m["caption"] for m in messages if m.get("caption")), "")`。
- 檔案是所有 `messages` 的檔案，依到達順序。

- [ ] **Step 4: 跑測試，確認通過**

Run: `uv run pytest tests/test_telegram.py -q`
Expected: PASS

Run: `uv run pytest -q`
Expected: 全部通過。

- [ ] **Step 5: Commit**

```bash
git add kilo/telegram/bot.py tests/test_telegram.py
git commit -m "feat: treat a Telegram album as one message" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: 文件與 live 測試

**Files:**
- Modify: `README.md`、`tests/live/test_live.py`

**Interfaces:**
- Consumes: 前面所有 task 的對外行為。
- Produces: 文件；以真實模型驗證「拿到的是位置，不是內容」。

- [ ] **Step 1: 寫 live 測試**（放在 `test_real_server_process` 之後）

```python
def test_real_upload_is_read_not_sent(real_server):
    word = f"MANGO-{secrets.token_hex(3).upper()}"
    with httpx.Client(base_url=real_server, timeout=180) as http:
        login = http.post("/api/v1/auth/login", json={"username": DEFAULT_USERNAME, "password": DEFAULT_PASSWORD})
        assert login.status_code == 200
        attachment = http.post(
            "/api/v1/uploads", params={"name": "code.txt"}, content=f"The code word is {word}.\n".encode(),
            headers={"content-type": "text/plain"},
        ).json()
        session = http.post("/api/v1/sessions").json()
        reply = http.post(f"/api/v1/sessions/{session['id']}/chat", json={
            "message": "What is the code word in the file I sent? Reply with the code word only.",
            "attachments": [attachment], "stream": False,
        }).json()
    calls = {event["data"]["name"] for event in reply["events"] if event["event"] == "tool_call_start"}
    assert calls & {"read_file", "shell"}, reply["events"]
    assert word in reply["response"]
```

檔頭補上 `import secrets`。暗號只在檔案裡；請求裡只有路徑，所以模型一定要讀檔才答得出來。

- [ ] **Step 2: 跑 live 測試**

Run: `uv run pytest -m live -q`
Expected: 全部通過，共 6 個（原本 5 個加上這個）。這個測試必須是真的讀到暗號才通過。

- [ ] **Step 3: 更新 README**
  - **Features**：在 Tools 之後加一項 **Files**：可以從網頁終端（`/upload`、拖放、貼上）或 Telegram 傳任何檔案；檔案存進 workspace 的 `uploads/`，agent 只會被告知位置，不會收到內容。
  - **新增 `## Files` 一節**，放在 Configuration 之後，涵蓋 spec §5 列的內容：
    - 位置：`uploads/<date>/`，日期依 `KILO_TIMEZONE`。
    - 命名：檔名會改成 shell 安全的寫法；同名時加上 `-2` 之類的編號。
    - 訊息裡會列出路徑、類型、大小；目前 `read_file` 只讀得了文字檔。
    - 網頁的操作方式，以及待送清單。
    - Telegram：有說明文字就立刻送出；沒有就等你的下一則訊息；相簿算一則；上限 20 MB。
    - 上限：`KILO_UPLOAD_MAX_MB`。前面若有代理，要一起調高它的上限（例如 nginx 的 `client_max_body_size`，預設 1 MB）。
    - 不會自動清理。
  - **Telegram 一節**：把 `Only text messages are supported.` 改成「也支援檔案（見 Files）」。
  - **Configuration 表**：加上 `KILO_UPLOAD_MAX_MB` 一列。

- [ ] **Step 4: 全部測試**

Run: `uv run pytest -q`
Expected: 全部通過，`5 deselected`。

Run: `git diff main --stat -- pyproject.toml uv.lock`
Expected: 沒有輸出（沒有新增依賴）。

Telegram 實機測試是選擇性的（spec §4）：本機沒有 bot token，需要使用者提供測試用的 bot，才用手機傳照片、相簿、文件各一次。沒有就略過，並在最後的回報裡說明。

- [ ] **Step 5: Commit**

```bash
git add README.md tests/live/test_live.py
git commit -m "docs: explain sending files; verify a real model reads an upload" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
