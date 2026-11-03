# Kilo：檔案上傳（設計規格）

- 日期：2026-11-03
- 狀態：設計已逐段確認，待審閱 spec
- 範圍：網頁終端與 Telegram 接收使用者傳來的檔案、存放檔案，以及告訴 agent 檔案在哪

## 1. 目標與約束

**目標**：使用者可以從網頁終端或 Telegram 傳任何檔案給 kilo。檔案存進 workspace 的 `uploads/`，不交給 LLM；送給模型的訊息只附上每個檔案的位置、類型、大小，由 agent 自己用工具去讀。

**約束**

- 檔案內容絕不放進送給模型的請求：不轉成文字、不轉成圖片、不做多模態輸入。
- 不限制檔案類型，只限制大小。
- 不新增讀檔工具。現有的 `read_file` 只讀純文字；圖片、影片、Office 的讀取工具在下一階段。
- 不新增套件依賴。
- 不需要資料遷移：附件記在 messages 表既有的 `payload` 欄位，Telegram 的新資料表用 `CREATE TABLE IF NOT EXISTS` 建立。舊的使用者訊息 `payload` 是 `None`，照常運作。
- 介面文字（網頁終端、Telegram bot 的回覆、給模型的說明）沿用現有慣例，用英文。

**成功標準**

1. 單元測試全部通過（`uv run pytest`）。
2. live 測試全部通過（`uv run pytest -m live`），包括新的「上傳文字檔、模型自己讀出暗號」。
3. 在真的瀏覽器裡操作網頁終端，§3.4 的每一項都符合。
4. 給模型的請求裡只有檔案的路徑與描述，沒有檔案內容（由測試確認）。

**不做（非目標）**

- agent 把檔案傳回給使用者（下載連結、Telegram 傳檔）。
- 圖片、影片、Office 等讀檔工具（下一階段）。
- 自動清理：刪 session 不刪檔案，舊檔由工程師自行清理。
- 網頁上傳的百分比進度條、檔案瀏覽器、列出已上傳檔案的指令。
- Telegram 超過 20 MB 的檔案（需要自架 Bot API server）。
- Telegram 的貼圖、位置、聯絡人、投票等非檔案訊息。
- LINE 對話裡的檔案：LINE 是 agent 操作的對象，不是使用者跟 kilo 對話的介面。
- 掃毒、檔案類型白名單。

## 2. 現況

- 網頁終端（`kilo/web/`）只有一行輸入框。聊天 API `POST /api/v1/sessions/{id}/chat` 只收 JSON `{"message", "stream"}`，空訊息回 400。
- Telegram bot（`kilo/telegram/bot.py`）收到任何非文字訊息都回 `Only text messages are supported for now.`。群組裡只處理名單內的人對 bot 說的訊息；`_meant_for_me` 已經會看說明文字（caption）。
- 使用者訊息存成 `MessageRole.USER`：`content` 是文字，`payload` 是 `None`。`_api_messages` 直接用 `content` 組成 user 訊息。
- `read_file` 讀 UTF-8 文字，遇到二進位檔只回報 `appears to be binary`，不讀內容。agent 另有不受限的 `shell`。
- workspace（`KILO_WORKSPACE`）是 agent 的工作目錄，工具的相對路徑都在這裡解析。system prompt 的 workspace 段落由 `workspace.prompt_section()` 產生。
- 所有 `/api/v1/*` 路徑都由 `LoginRequired` middleware 要求登入。
- 網頁的 CSP 是 `connect-src 'self'`，同源 `fetch` 上傳不受影響。
- 網頁終端在 agent 回覆時會隱藏輸入框（`Terminal.refresh`）。

## 3. 設計

### 3.1 存放（新模組 `kilo/uploads.py`）

網頁和 Telegram 共用這個模組。

**位置**：`<KILO_WORKSPACE>/uploads/<YYYY-MM-DD>/`。日期取上傳當下 `KILO_TIMEZONE` 的日期，資料夾需要時才建立。按日期分而不按 session 分，是因為 Telegram 先存著的檔案，可能在換了 session 之後才送出。

**檔名**（`safe_name(name) -> str`）：保留原始檔名，依序處理：

1. 只取最後一個 `/` 或 `\` 之後的部分。
2. 正規化成 Unicode NFC（macOS 送來的檔名可能是分解形式）。
3. 以下字元以外，一律換成 `_`（控制字元也包括在內）：
   - 字母、組合記號、數字（Unicode 類別 L、M、N）；
   - 空白（U+0020）；
   - `. - _ ( ) + , = @`。

   理由：檔名會出現在 agent 寫的 shell 指令裡，而轉傳來的檔案，檔名可能是陌生人取的。`;`、`&`、`|`、`$`、`` ` ``、引號、`\`、`<`、`>` 這類字元，在沒加引號或加了雙引號時都可能執行指令；換掉之後，最壞只是指令出錯，不會執行到別的東西。
4. 去掉開頭的空白、`.`、`-`（避免變成隱藏檔，或被當成指令選項，例如 `-rf`），也去掉結尾的空白。
5. 結果是空的就用 `file`。
6. UTF-8 超過 255 bytes 就截短：副檔名保留，截短主檔名，且不切斷字元。副檔名是最後一個 `.` 和它後面的字元，總長不超過 16 個字元才算副檔名。

**同名**：當天資料夾已有同名檔案時，依序試 `<主檔名>-2<副檔名>`、`-3`，依此類推。檔案以獨佔方式建立（`open(..., "xb")`），所以同時上傳同名檔也不會互相覆蓋。加上編號後仍不超過 255 bytes。

**大小上限**：新設定 `KILO_UPLOAD_MAX_MB`，整數，至少 1，預設 500（1 MB = 1024 × 1024 bytes）。寫入途中一超過就停止，刪掉已寫入的部分，丟出 `UploadTooLarge`。

**寫入失敗**：任何錯誤（超過上限、磁碟滿、沒有權限、來源中斷）都先刪掉已寫入的部分，再把錯誤往上丟。

**類型**（`type_of(name, declared)`）：

1. `mimetypes.guess_type(檔名)` 判斷得出來，就用它。
2. 否則用傳送端標的類型（瀏覽器的 `Content-Type`、Telegram 的 `mime_type`），去掉 `;` 之後的參數、轉小寫。空的或是 `application/octet-stream` 不算。
3. 都沒有就是 `application/octet-stream`。

副檔名優先，是因為傳送端的標示可能不準。例如 Windows 會把 `.csv` 標成 Excel 的類型。

**附件資料**（`Attachment`，放在 `kilo/models/session.py`）：

| 欄位 | 內容 |
| --- | --- |
| `path` | 正規化後、相對於 workspace 的路徑（用 `/` 分隔），例如 `uploads/2026-11-03/report.pdf` |
| `name` | 傳送端給的原始檔名，最多 255 字元。Telegram 沒給檔名時，是自動產生的名字 |
| `type` | 依上面規則判斷的類型 |
| `size` | 大小（bytes） |

**模組介面**

- `async def save(chunks: AsyncIterable[bytes], name: str, declared_type: Optional[str] = None) -> Attachment`
- `def attachment_at(path: str, name: Optional[str] = None, declared_type: Optional[str] = None) -> Attachment`
  - 確認 `path` 解析後（含 symlink）是 `uploads/` 裡存在的一般檔案，不符合就丟 `ValueError`。
  - 大小讀磁碟，類型照上面的規則判斷。
  - 沒給 `name` 時，用路徑裡的檔名。
- `def note(attachments: Sequence[Attachment]) -> str`：§3.2 的說明文字。
- `def format_size(size: int) -> str`：
  - 小於 1024 時寫成 `N bytes`，再往上依序是 `KB`、`MB`、`GB`（1 KB = 1024 bytes）。
  - 取一位小數，例如 `2.3 MB`。

### 3.2 訊息與模型看到的內容

**儲存**

- 使用者訊息的 `content` 存使用者打的文字；只傳檔案時是空字串。
- 有附件時 `payload = {"attachments": [<Attachment>…]}`，沒有時維持 `None`。
- `Message.payload` 的說明改成涵蓋這個用途。

**送給模型**：`_api_messages` 組 user 訊息時，有附件就在文字後面空一行，接上 `note(attachments)`；文字是空的就只放說明。說明的確切格式：

```
[Files sent with this message, saved in the workspace. Their contents are not included here; read them with your tools when you need them.
- uploads/2026-11-03/report.pdf (application/pdf, 2.3 MB)
- uploads/2026-11-03/report-2.pdf (application/pdf, 1.1 MB, sent as "report.pdf")]
```

- 每個檔案一行：路徑、類型、`format_size` 算出的大小。
- `name` 跟路徑裡的檔名不同時，才加 `sent as`。檔名寫成 JSON 字串（`json.dumps(name, ensure_ascii=False)`），檔名裡的引號或換行都不會破壞這段格式。
- 說明在每次組請求時從附件資料產生，每次結果都一樣，prompt cache 照常命中。

**system prompt**：`workspace.prompt_section()` 加這一句：

```
Files the user sends you are saved in `uploads/` (a folder per day); each one is listed in the message it came with.
```

**壓縮**：`COMPACTION_INSTRUCTIONS` 的保留清單加一項：`- Files the user sent, with their paths.`

**`AgentRunner.run_chat_stream`**：新增參數 `attachments: Sequence[Attachment] = ()`；排程等其他呼叫端不受影響。

- 存訊息時，照上面的方式存附件。
- 判斷是否要先壓縮時，估算新增大小也把說明文字算進去。
- `user_message` 事件的 `data` 多一個 `attachments`：附件資料的 list，沒有附件時是 `[]`。
- 自動標題：文字的第一行是空的、但有附件時，改用第一個附件的 `name`，同樣最多 40 字元。

**重播**：`history_events` 產生的 `user_message` 也帶 `attachments`，從 `payload` 取，沒有時是 `[]`。

### 3.3 網頁終端：API

**上傳**：`POST /api/v1/uploads?name=<檔名>`（新檔 `kilo/server/routes/uploads.py`）

- request body 就是檔案內容本身，不用 multipart；`Content-Type` 是瀏覽器對這個檔案標的類型。一次上傳一個檔案。
- 用 raw body 的理由：可以邊收邊寫進 `uploads/`、邊檢查大小。multipart 要先把整個檔案收完、暫存到系統暫存區，程式才拿得到，大檔等於多寫一次。
- 錯誤處理：
  - `name` 沒給或只有空白：400 `name is required.`
  - `Content-Length` 已經超過上限：不讀內容，直接 413。
  - 寫入途中超過上限：413，並刪掉已寫入的部分。
  - 以上兩種 413 的 detail 都是 `File is larger than the upload limit of <N> MB (KILO_UPLOAD_MAX_MB).`，`<N>` 是設定值。
  - 磁碟錯誤：500，detail 是 `Could not save <name>: <錯誤>`。
  - 連線中斷：刪掉已寫入的部分。
- 成功：201，body 是 `Attachment`。
- 需要登入：`LoginRequired` 已經涵蓋所有 `/api/v1/*`。

**聊天**：`POST /api/v1/sessions/{id}/chat` 的 request 格式：

```python
class AttachmentRef(BaseModel):  # 上傳 API 回傳的物件；size 等其他欄位一律忽略
    path: str
    name: Optional[str] = None
    type: Optional[str] = None

class ChatRequest(BaseModel):
    message: str = ""
    attachments: List[AttachmentRef] = []
    stream: bool = True
```

- 先確認 session 存在，不存在回 404。
- 文字是空白而且沒有附件：400 `Message cannot be empty.`（沿用現有訊息）。
- 每個附件交給 `uploads.attachment_at(path, name, type)` 檢查。有任何一個不符合，就回 400 `Attachment '<path>' is not a file in uploads/.`，整則訊息不送出。附件大小一律以磁碟上的為準。
- 檢查通過後以 `run_chat_stream(..., attachments=...)` 執行；串流與非串流回應都一樣。

### 3.4 網頁終端：操作

- **附加檔案的三種方式**
  - `/upload`：開啟瀏覽器的檔案選擇視窗，可以多選，手機瀏覽器也能用。
  - 把檔案拖進頁面：登入後隨時可以，包括 agent 回覆中。
  - 在輸入框貼上檔案（例如截圖）：只有剪貼簿裡有檔案時才攔下來上傳，一般文字照常貼上。
- **上傳**：選好的檔案依序一個一個上傳，每個檔案會顯示：
  - 開始：`Uploading report.pdf (2.3 MB)…`
  - 成功：`📎 report.pdf → uploads/2026-11-03/report.pdf (2.3 MB)`，並加進待送清單。
  - 失敗：`Upload of report.pdf failed: <detail>`，不加進待送清單。
- **待送清單**
  - 存在網頁端；提示字元會顯示數量，例如 `kilo [1a2b3c4d] 📎2>`。
  - 換 session 時保留，會跟著下一則訊息，送到當時開著的 session。
  - 重新整理頁面就沒了，但檔案還在磁碟上。
- **按 Enter**
  - 有文字或有待送檔案就送出，兩者一起送。沒打字但有待送檔案，就只送檔案。
  - 還有檔案在上傳時，顯示 `Waiting for uploads to finish…`，等傳完再送。等待期間：
    - 有檔案上傳失敗：不送出，文字放回輸入框，顯示 `Not sent: an upload failed. Your message is back at the prompt.`
    - 按 Esc：不送出，文字放回輸入框；上傳繼續進行，成功的照樣加進待送清單。
  - 伺服器接受這則訊息、串流開始之後，才清空待送清單；被拒絕（例如 400）就保留。
  - 斜線指令不影響待送清單。
- **`/upload clear`**：清空待送清單，顯示 `Cleared 2 pending files; they stay in uploads/.`；清單本來就是空的時，顯示 `No files are waiting to be sent.`
- **顯示**：在使用者那行訊息下方，每個附件一行：`  📎 uploads/2026-11-03/report.pdf (2.3 MB)`。
  - 送出時，從待送清單顯示。
  - 重新打開對話時，從 `user_message` 事件的 `attachments` 顯示。
- **說明與補全**：`/help` 加上 `/upload`、`/upload clear`，以及拖放、貼上的說明；Tab 補全加上 `/upload` 和它的 `clear`。
- 和現有程式一樣，所有文字都用 `textContent` 寫進畫面，檔名不會被當成 HTML。

### 3.5 Telegram

**接受的類型與檔名**：依下表順序檢查訊息欄位。GIF 動畫訊息會同時帶 `animation` 和 `document`，所以 `animation` 排在最前面。

| 欄位 | 沒有檔名時的名字 |
| --- | --- |
| `animation` | `animation-<HHMMSS><ext>` |
| `document` | `document-<HHMMSS><ext>` |
| `photo`（取最大尺寸） | `photo-<HHMMSS><ext>` |
| `video` | `video-<HHMMSS><ext>` |
| `audio` | `audio-<HHMMSS><ext>` |
| `voice` | `voice-<HHMMSS><ext>` |
| `video_note` | `video-note-<HHMMSS><ext>` |

- 有 `file_name` 就用它當 `name`（`document`、`video`、`audio`、`animation` 可能會有）。
- `<HHMMSS>` 是收到時 `KILO_TIMEZONE` 的時間；`<ext>` 是 `getFile` 回傳的 `file_path` 的副檔名，例如 `.jpg`、`.oga`。
- 傳送端類型用 Telegram 的 `mime_type`（照片沒有這個欄位）。
- 其他訊息（貼圖、位置、聯絡人、投票等）回 `Only text and files are supported.`，取代現在的 `Only text messages are supported for now.`

**大小檢查**：下載前先看訊息裡的 `file_size`。

- 超過 20 MB（Telegram bot 能下載的上限，20 × 1024 × 1024 bytes）時回：
  `<label> (<size>) is too big for a Telegram bot, which can only fetch files up to 20 MB. Send it from the web terminal instead.`
- 超過 `KILO_UPLOAD_MAX_MB` 時回：
  `<label> (<size>) is larger than the upload limit of <N> MB (KILO_UPLOAD_MAX_MB).`
- 兩者都超過時，回第一句。
- 訊息沒有標大小時，照常下載。之後若：
  - `getFile` 回報檔案太大：回第一句，不帶 `(<size>)`；
  - 寫入途中超過上限：回第二句，不帶 `(<size>)`。
- `<label>` 是 `file_name`；沒有時用 `The photo`、`The video` 這類說法。

**下載**：新方法 `BotAPI.download(file_path)` 以串流方式取得 `https://api.telegram.org/file/bot<token>/<file_path>`，交給 `uploads.save`。

- 網址含 token，所以不寫進任何 log 或錯誤訊息。錯誤只帶失敗的種類或 HTTP 狀態碼，跟現有的 `BotAPI.call` 一樣。
- 失敗時回 `Could not fetch <label> from Telegram (<原因>). Send it again.`，並刪掉已寫入的部分。
- 下載不在接收更新的迴圈裡進行，不會卡住其他聊天、指令或按鈕。

**先存，等下一則訊息**（使用者選定的做法）：

- **只有檔案、沒有說明文字的訊息**：檔案存好後，保留給「這個聊天裡的這個人」，不呼叫模型。bot 回覆那則訊息：
  - 一個檔案：`📎 Saved report.pdf (2.3 MB). It goes to the agent with your next message.`
  - 多個檔案（相簿）：`📎 Saved 3 files (5.1 MB). They go to the agent with your next message.`
- **同一個人在同一個聊天的下一則文字訊息，或附了說明文字的檔案**：
  - 帶上這個人所有保留中的檔案（依收到順序），再加上這則訊息自己的檔案。
  - 一起排進對話佇列，跑一輪。
- **指令**（`/new`、`/sessions`、`/stop`、`/compact` 等）不會用掉保留中的檔案。
- **保留清單存在 `TelegramStore` 的新資料表**，跟其他 Telegram 資料表在同一個資料庫檔，所以伺服器重啟也不會遺失：
  - 資料表：`telegram_held_files (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, attachment TEXT NOT NULL)`
  - `hold(chat_id, user_id, attachments)`：存入。
  - `take(chat_id, user_id) -> List[Attachment]`：依 `id` 排序取出，並在同一個交易裡刪除。
- 對話佇列的項目多帶附件；`_turn` 把附件交給 `run_chat_stream(..., attachments=...)`。

**順序保證**：同一個聊天裡，帶文字或檔案的訊息依到達順序處理。檔案還在下載、或相簿還在收集時送出的文字，會等前面的檔案處理完，所以一定帶得到那些檔案。指令和按鈕不排在這個順序裡，收到就處理。

**相簿**（同一個 `media_group_id`）：在同一個聊天裡收集，最後一項到達後 1 秒內沒有新的一項，就當成一則訊息處理。

- 檔案：所有項目的檔案，依到達順序。
- 文字：第一個有說明文字的項目的說明文字。都沒有的話，當成沒有文字：保留起來，只回一次「已存」。
- 群組裡只要任一項是對 bot 說的，整本相簿就算是對 bot 說的。
- 傳送者：第一項的傳送者。

**群組**：沿用文字訊息的規則。

- 只處理名單內的人對 bot 說的訊息：指令、文字或說明文字裡有 @bot，或回覆 bot 的訊息。其他訊息裡的檔案一律不下載。
- 說明文字跟一般文字一樣，去掉 @bot，再加上 `[Sent by … in the Telegram group "…"]`。去掉 @bot 之後是空的，就當成沒有說明文字（保留起來，等下一則訊息）。
- 保留清單按人分開，一個人保留的檔案不會跟著別人的訊息送出。

### 3.6 錯誤處理總表

| 情況 | 網頁終端 | Telegram |
| --- | --- | --- |
| 超過 `KILO_UPLOAD_MAX_MB` | 413，顯示錯誤，不加進待送清單 | 回覆說明，不保留 |
| 超過 20 MB | — | 回覆請改用網頁終端，不下載 |
| 磁碟錯誤、來源中斷、下載失敗 | 刪掉部分檔案；回 500 或連線錯誤，顯示錯誤 | 刪掉部分檔案；回覆原因，請使用者重傳 |
| 送出時附件不在 `uploads/` 裡或已不存在 | 400，不送出，待送清單保留 | — |
| 附件事後被刪掉或搬走 | 歷史照樣列出；agent 讀的時候自己會發現 | 同左 |

## 4. 測試策略

TDD。Telegram 用測試裡現有的假 Telegram API，不連真的 Telegram。

- **`tests/test_uploads.py`（新）**
  - `safe_name`：
    - 路徑部分、`../`、控制字元、shell 特殊字元換成 `_`；
    - 開頭的 `.`、`-` 去掉，處理完變空時用 `file`；
    - 長中文檔名依 bytes 截短，副檔名保留；
    - NFC 正規化。
  - 同名檔案依序加 `-2`、`-3`。
  - 超過上限丟 `UploadTooLarge`，並刪掉部分檔案；來源中途丟錯時，也刪掉部分檔案。
  - 類型判斷的先後順序。
  - `attachment_at`：`uploads/` 外的路徑、`..`、指到外面的 symlink、不存在的檔案都拒絕；大小讀磁碟。
  - `note`、`format_size` 的確切文字；`sent as` 只在改名時出現。
- **`tests/test_api.py`**
  - 上傳：
    - 201 與回傳內容，存下的檔案內容與上傳的一致；
    - 413 的兩種情況：`Content-Length` 超過、實際內容超過；
    - 缺 `name` 回 400，未登入回 401。
  - 聊天：
    - 附件存進 `payload`，`user_message` 事件帶 `attachments`；
    - 送給模型的請求有說明文字，沒有檔案內容；
    - 只有檔案也能送；
    - 附件路徑不合格回 400，而且沒有存下任何訊息。
- **`tests/test_runner.py`**
  - `_api_messages` 的組法，有文字和沒文字兩種。
  - `history_events` 帶附件。
  - 用檔名當自動標題。
  - system prompt 與壓縮指示的新文字。
- **`tests/test_telegram.py`**
  - 有說明文字的檔案：下載、存檔，跑一輪並帶上附件。
  - 沒有說明文字的檔案：回「已存」、不跑；下一則文字帶上它，只跑一輪。
  - 檔案還在下載時送出的文字，也帶得到這個檔案。
  - 相簿合成一則，只回一次「已存」。
  - 超過 20 MB、超過上限：不下載，回覆說明。
  - 下載失敗：回覆原因，不保留。
  - 群組：沒對 bot 說的檔案不下載；保留清單按人分開。
  - 新的 bot 實例（模擬重啟）仍取得到保留中的檔案。
  - 指令不會用掉保留中的檔案。
  - 貼圖回 `Only text and files are supported.`
  - 錯誤訊息和 log 裡都不出現 token。
- **`tests/test_config.py`**：`KILO_UPLOAD_MAX_MB` 預設 500，小於 1 時拒絕。
- **live 測試（`tests/live/test_live.py`）**：在真的 server 上，經上傳 API 傳一個內含隨機暗號的 `.txt`，再帶著這個附件問「我傳的檔案裡，暗號是什麼」。
  - 必須出現 `read_file`（或 `shell`）的工具呼叫，而且回覆裡有暗號。
  - 暗號不在 prompt 裡，所以這同時證明模型拿到的是位置，不是內容。
- **網頁介面**：專案沒有 JS 測試環境。實作後用真的瀏覽器（Chrome DevTools）逐項操作，確認 §3.4 的每一項。
- **Telegram 實機（選擇性）**：本機沒有設定 bot token。如果使用者提供測試用的 bot，再用手機實際傳照片、相簿、文件確認一次。

## 5. 文件與設定

- `kilo/config.py`：`kilo_upload_max_mb: int = Field(500, ge=1)`。
- `.env.example`（Tools 一節）加上：

  ```
  # Largest file accepted from the web terminal or Telegram, in MB (a Telegram bot can fetch at most 20 MB).
  # If a proxy is in front of Kilo, raise its request size limit too (nginx allows 1 MB by default).
  KILO_UPLOAD_MAX_MB=500
  ```

- README：
  - Features 加一項 **Files**：可從網頁終端（`/upload`、拖放、貼上）或 Telegram 傳任何檔案；檔案存在 workspace 的 `uploads/`，agent 只會被告知位置。
  - 新增「Files」一節：存放位置與命名規則、大小上限、反向代理的上限要一起調高、不會自動清理。
  - Telegram 一節：原本的 `Only text messages are supported.` 改成支援文字與檔案，並說明沒有說明文字的檔案會等下一則訊息、上限 20 MB。
  - Configuration 表加上 `KILO_UPLOAD_MAX_MB`。

## 6. 風險與後續

- **磁碟用量只增不減**：沒有自動清理，由工程師定期清掉 `uploads/` 底下的舊日期資料夾。
- **反向代理的上限**：前面若有 nginx 之類的代理而沒有調高上限，大檔會直接在代理被擋下（413）。README 會說明要怎麼調。
- **相簿被拆開**：網路慢、項目間隔超過 1 秒時，相簿可能被拆成兩則。後半若沒有說明文字，會被保留下來、跟著下一則訊息送出，不會遺失。
- **檔名與檔案內容裡的提示注入**：
  - 檔名已處理成 shell 安全的字元，原始檔名以 JSON 字串呈現。
  - 檔案內容只有在 agent 主動讀取時才會進入對話，風險跟它讀任何檔案或網頁相同。
- **下一階段**：加入圖片、影片、Office 的讀取工具後，可以在說明文字裡提示該用哪個工具。說明是組請求時才產生，所以舊對話也會一起更新。

## 7. 受影響檔案

- 新增：
  - `kilo/uploads.py`
  - `kilo/server/routes/uploads.py`
  - `tests/test_uploads.py`
- 修改：
  - 設定與資料模型：`kilo/config.py`、`kilo/models/session.py`、`kilo/models/api.py`
  - server：`kilo/server/app.py`、`kilo/server/routes/sessions.py`
  - agent：`kilo/agent/runner.py`、`kilo/agent/history.py`、`kilo/workspace.py`
  - Telegram：`kilo/telegram/api.py`、`kilo/telegram/bot.py`、`kilo/telegram/store.py`
  - 網頁：`kilo/web/static/api.js`、`main.js`、`commands.js`、`render.js`、`terminal.js`
  - 文件：`README.md`、`.env.example`
  - 測試：`tests/test_api.py`、`tests/test_runner.py`、`tests/test_telegram.py`、`tests/test_config.py`、`tests/live/test_live.py`
