# Kilo：讀圖片與文件（設計規格）

- 日期：2026-11-03
- 狀態：設計已逐段確認，待審閱 spec
- 範圍：擴充 `read_file`，讓 agent 能看圖片（主模型直接看），也能把 Word、Excel、PowerPoint、PDF 轉成文字來讀

## 1. 目標與約束

**目標**：使用者傳來的照片、截圖、Word、Excel、PowerPoint、PDF，或 workspace 裡任何這類檔案，agent 都能用 `read_file` 讀。圖片讓主模型直接看到；文件轉成可以分頁、可以 grep 的文字。

**約束**

- 只有一個讀檔工具：擴充 `read_file`，不新增工具。
- 圖片給主模型直接看。OpenRouter 的 tool 訊息只能是文字，所以在工具結果後面補一則帶圖片的 user 訊息。
- 文件在本機轉成文字。PDF 不用 OpenRouter 的原生 PDF 輸入：那樣整份 PDF 每輪都要重送，也沒辦法分頁。
- 只用 pip 套件，`uv sync` 就裝好，不要求伺服器安裝 LibreOffice、poppler、tesseract 這類系統軟體。授權必須寬鬆，所以不用 AGPL 的 PyMuPDF。
- 不加新設定：尺寸、張數、頁數、保留天數都是程式常數。
- 不需要資料遷移：附圖記在 messages 表既有的 `payload` 欄位。
- 給模型看的文字（工具說明、工具結果、圖片標籤）沿用現有慣例，用英文。

**成功標準**

1. 單元測試全部通過（`uv run pytest`）。
2. live 測試全部通過（`uv run pytest -m live`），包括新的「上傳一張畫了數字的圖，模型用 `read_file` 讀出數字」。
3. 在網頁終端實際傳一張照片和一個 docx，agent 能正確回答它們的內容。
4. 送給模型的請求裡，圖片只出現在工具結果後面的 user 訊息裡，最多 10 張（由測試確認）。

**不做（非目標）**

- 影片、音訊。
- HEIC、舊版 Office（.doc/.xls/.ppt）、OpenDocument（.odt/.ods/.odp）、含巨集的 .docm/.xlsm/.pptm。
- OCR。掃描檔讓模型自己看圖，不在本機辨識文字。
- 把投影片或 Word 頁面轉成圖片（需要 LibreOffice）。
- 把 Office 檔裡嵌入的圖片給模型看。轉出的文字只留 `[Image: …]`、`[Picture: …]` 標記。
- 把 MCP 工具回傳的圖片給模型看。目前它們會換成 `[image content: …]`，以後可以沿用這次的機制。
- 在網頁終端或 Telegram 顯示 agent 看過的圖。
- 寫入或修改 Office、PDF 檔。

## 2. 現況

- `read_file`（`kilo/tools/file_tools.py`）只讀 UTF-8 文字：
  - 檔案開頭 4096 bytes 裡有 NUL 就當成二進位檔拒絕。
  - 輸出帶行號，用 `start_line`、`end_line`、`max_lines` 分頁，在輸出上限前停在一整行結束的地方（`READ_FILE_ROOM`）。
  - 在背景 thread 執行（`asyncio.to_thread`）。
- 工具結果是 JSON 字串（`Tool.execute` 回傳 `str`），由 `Toolbox.execute` 套用 `limit_output`。正式程式裡只有 runner 呼叫 `Toolbox.execute`。
- runner 把每個工具結果存成 `MessageRole.TOOL`，`payload` 是 `{"role": "tool", "tool_call_id", "content"}`。每次請求，`_api_messages` 都從資料庫重組歷史。重組是決定性的，所以供應商的 prompt cache 會一直命中。
- OpenRouter Chat Completions 官方型別定義：tool 訊息的 `content` 只能是字串，`image_url` 之類的內容片段只能放在 user 訊息裡。
- 目前的模型 `meta/muse-spark-1.3-contributor` 的 `input_modalities` 有 `image`。OpenRouter 的公開 API `GET /api/v1/models/{model}/endpoints` 不需要金鑰就查得到。
- 2026-11-03 的實測：
  - 工具結果後面接一則帶圖片的 user 訊息，模型能正確說出圖片內容。用 96×64 的小圖加上低推理強度時，它答錯了顏色。
  - 圖片佔的 token：512×384 約 270，1024×768 約 970，2048×1536 約 4,000；3000×2250 也是約 4,000，供應商會自己縮圖。
  - 一個請求帶 12 張圖可以送出。
- MCP 工具回傳的圖片會換成 `[image content: <type>]` 文字。
- 附件說明 `uploads.NOTE_HEADER` 叫 agent「read them with your tools」。README 寫「For now `read_file` reads text files only.」

## 3. 設計

### 3.1 `read_file` 分流

依副檔名分流，不分大小寫：

| 副檔名 | 處理方式 |
| --- | --- |
| `.png .jpg .jpeg .webp .gif .bmp .tif .tiff` | 圖片（§3.2） |
| `.docx .xlsx .pptx .pdf` | 轉成文字（§3.4）；PDF 有給 `pages` 時改成把頁面當圖片（§3.5） |
| `.heic .heif` | 錯誤：`HEIC images are not supported. Ask for a JPEG or PNG instead.` |
| `.doc .xls .ppt` | 錯誤：`Old Office formats (.doc, .xls, .ppt) are not supported. Ask for .docx, .xlsx or .pptx instead, or convert the file with LibreOffice (soffice) if it is installed.` |
| 其他 | 跟現在一樣當文字讀 |

- 二進位檔的錯誤訊息改成列出支援的格式：`File '{path}' appears to be binary ({size} bytes). read_file reads text files, images (PNG, JPEG, WebP, GIF, BMP, TIFF), PDF, Word (.docx), Excel (.xlsx) and PowerPoint (.pptx) files.`
- 圖片會忽略 `start_line`、`end_line`、`max_lines`。
- 給了 `pages` 但檔案不是 PDF：錯誤 `pages applies to PDF files only.`
- 新的工具說明：

  > Read a file. Text comes as numbered lines: each line with its number and ' | ' in front (e.g. '   12 | text'), which are not part of the file. Lines longer than 2,000 characters are cut, with a marker, and a long read stops early with `truncated` and a `message` saying where to continue. Bytes that are not UTF-8 show as replacement characters. PDF, Word (.docx), Excel (.xlsx) and PowerPoint (.pptx) files are converted to Markdown text, numbered and read in parts the same way; the result's `text_file` holds the whole text, for grep. Editing that text does not change the original file. Images (PNG, JPEG, WebP, GIF, BMP, TIFF) are shown to you in a message after the tool results, as are PDF pages asked for with `pages`. Other binary files are refused.

- 新參數 `pages`（string）的說明：

  > PDF only: pages to look at as images rather than read as text, e.g. "3", "3-5" or "1,4"; at most 5. For scanned pages, and for charts and layouts the text leaves out.

### 3.2 圖片（新模組 `kilo/images.py`）

常數：`MAX_SIDE = 2048`、`JPEG_QUALITY = 85`、`KEEP_DAYS = 30`、存放處 `<KILO_DATA_DIR>/images/`。

讀一張圖的步驟：

1. 用 Pillow 開檔。Pillow 預設的像素上限（`Image.MAX_IMAGE_PIXELS`）照舊：太大的圖會丟出 `DecompressionBombError`，當成錯誤回報。
2. 有多格的圖（動畫 GIF、WebP、多頁 TIFF）只取第一格，並記下格數。
3. 依 EXIF 方向轉正（`ImageOps.exif_transpose`）。
4. 有透明度的圖（RGBA、LA、帶透明色的 P）疊在白底上，再轉成 RGB。
5. 長邊超過 2048 px 就等比縮小（LANCZOS），小圖不放大。
6. 一律存成 JPEG（品質 85）。檔名是 JPEG 內容的 SHA-256 加 `.jpg`。
   - 已經有同名檔時不重寫，只更新它的修改時間。
   - 每次存新檔時，順便刪掉存放處裡超過 30 天沒更新的檔案。

工具結果（文字）：

```json
{"status": "success", "path": "uploads/2026-11-03/photo.jpg", "type": "image/jpeg",
 "width": 4032, "height": 3024, "shown_width": 2048, "shown_height": 1536,
 "message": "The image is shown in the message after the tool results."}
```

- `type` 是 Pillow 認出的原始格式（`Image.MIME`）。`width`、`height` 是轉正後的原始尺寸，`shown_*` 是送給模型的尺寸。
- 有多格時加上 `"frames": 12`，`message` 改成 `The first of its 12 frames is shown in the message after the tool results.`
- 附圖記成 `ShownImage(file="<sha256>.jpg", label="uploads/2026-11-03/photo.jpg")`。`label` 就是結果裡的 `path`（`workspace.display`）。

### 3.3 圖片怎麼送到模型

**工具結果可以附圖**（`kilo/tools/base.py`、`kilo/tools/toolbox.py`）

- 新增兩個型別：
  - `ShownImage(file: str, label: str)`
  - `ToolResult(text: str, images: Tuple[ShownImage, ...] = ())`
- `Tool.execute` 可以回傳 `str` 或 `ToolResult`。
- 新增 `Toolbox.run(name, args) -> ToolResult`：文字照舊經過 `limit_output`，圖片原樣帶出。`Toolbox.execute` 保留，回傳 `run(...)` 的文字，給既有的呼叫者用。

**runner 記下附圖**（`kilo/agent/runner.py`）

- 執行工具改呼叫 `toolbox.run`。
- tool 訊息的 `payload` 有附圖時多一個欄位：`"images": [{"file": "<sha256>.jpg", "label": "..."}]`。
- 估算還沒量過的大小時，每張附圖加 `IMAGE_TOKENS = 4_000` token。這個數字是實測 2048 px 的值，只用來判斷要不要壓縮對話。

**組請求時補上圖片訊息**（`_api_messages(db_messages, show_images=True)`）

- 依序找出最新摘要之後所有 tool 訊息裡的附圖，最後 `MAX_SHOWN_IMAGES = 10` 張要附上。
- 一輪的 tool 訊息（包括替被中斷的呼叫補上的結果）都排完之後，如果其中有附圖，就接一則 user 訊息。訊息裡每張圖依序放一段文字，加上圖片本身或一句說明：
  - 正常附上：文字 `[Image: {label}]`，接著 `{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,…"}}`。
  - 不在最後 10 張內：`[Image: {label}. No longer shown, to keep the conversation small; read the file again to see it.]`
  - 存放處找不到檔案：`[Image: {label}. No longer available; read the file again to see it.]`
  - `show_images` 為 False：`[Image: {label}. Not shown: the model in use does not take images.]`
- 只存參照，跟附件說明一樣在組請求時才產生內容，所以資料庫不放圖片資料。
- 壓縮對話（`_compact`）用同樣的方法組請求，圖片也會一起送給寫摘要的模型。

**模型能不能看圖**

- `ChatModel` 新增 `async takes_images() -> bool`。
- `OpenRouterClient.takes_images()` 的查法：
  - 呼叫 `GET https://openrouter.ai/api/v1/models/{model}/endpoints`。`{model}` 會先去掉 `:free` 這類後綴；不帶金鑰；逾時 10 秒。
  - 只有回應裡的 `data.architecture.input_modalities` 是 list 而且沒有 `image`，才回 False。
  - 其他情況，包括 HTTP 錯誤、連不上、格式不對，都回 True，並記錄 warning：`OpenRouter: could not look up whether {model} takes images ({error}); assuming it does.`
  - 查到的結果存在 client 裡，每個程序只查一次。
- 只有要送出的歷史裡有附圖時，runner 才呼叫 `takes_images()`，所以沒有圖的對話不會多一次網路請求。

**壓縮指示**

在 `COMPACTION_INSTRUCTIONS` 的「- Files the user sent, with their paths.」後面加一條：

`- What the images and documents read so far showed, where it matters later; images are not kept after this summary.`

### 3.4 文件轉文字（新模組 `kilo/documents.py`）

常數：`MAX_CHARS = 10_000_000`、`MAX_UNZIPPED_BYTES = 1024**3`、`KEEP_DAYS = 30`、存放處 `<KILO_DATA_DIR>/converted/`、`CONVERTER_VERSION = 1`。

**轉換與存放**

- `convert(path) -> Path` 回傳轉好的 Markdown 檔。
- 檔名是「`CONVERTER_VERSION` 加上檔案內容」的 SHA-256，加 `.md`。改了轉換方式就把版本號加一，舊的轉檔結果就不會再被用到。
- 已經有同名檔時直接用，只更新它的修改時間。
- 新轉的檔先寫到暫存檔再改名，兩個 session 同時轉同一個檔案也不會讀到寫一半的內容。
- 每次寫新檔時，順便刪掉超過 30 天沒更新的檔案。
- 轉出的文字到 1,000 萬字就停，最後加一行：`[Conversion stopped here: the text passed 10,000,000 characters. The rest of the file is not included.]`

**`read_file` 怎麼回傳**

- 轉好的文字照文字檔的方式加行號、分頁。
- 結果多兩個欄位：`"converted_from": "docx"`（或 `xlsx`、`pptx`、`pdf`），以及 `"text_file": "<轉好的檔案路徑>"`。路徑用 `workspace.display` 顯示；資料夾在 workspace 外（預設如此）時是絕對路徑。

**docx**（python-docx）

- 依文件順序讀 body：`w:p` 是段落，`w:tbl` 是表格，`w:sdt`（內容控制項，例如目錄）讀它裡面的內容。
- 段落文字：
  - 取段落裡所有 `w:t`，所以包含超連結和追蹤修訂裡新增的字。刪除的字（`w:delText`）不算。
  - `w:tab` 轉成 tab，`w:br`、`w:cr` 轉成換行。
- 標題：樣式 `Title` 寫成 `#`，`Heading N`（N 為 1–6）寫成 N 個 `#`。
- 清單：
  - 段落有 `w:numPr`，或樣式名稱以 `List` 開頭，就是清單項目。
  - 層級依序取 `w:ilvl` 加 1、樣式名稱結尾的數字（`List Bullet 2` 是 2），都沒有就是 1。
  - 寫成 `"  " * (層級 - 1) + "- " + 文字`。
- 圖片：段落裡每個 `wp:docPr` 另起一行，寫成 `[Image: {descr}]`；沒有替代文字就寫 `[Image]`。
- 表格轉成 Markdown 表格：
  - 第一列當表頭。
  - 同一格裡的多個段落用 `<br>` 連起來，`|` 寫成 `\|`。
  - 合併的儲存格照 python-docx 的結果重複出現；巢狀表格的文字攤平放進外層的格子。
- 區塊之間空一行，空段落略過。
- 不包含頁首頁尾、註腳、註解、文字方塊。

**xlsx**（openpyxl，`read_only=True, data_only=True`）

- 每張工作表一節：`## Sheet {n}: {title}`，隱藏的（`sheet_state` 不是 `visible`）加 ` (hidden)`，接著空一行。
- 沒有任何值的工作表寫 `(empty)`。有值的寫成 Markdown 表格：
  - 表頭是 `|   | A | B | … |`，一直到任何一列有值的最後一欄。
  - 每一列有值的列寫成 `| {列號} | 值 | 值 | … |`。整列空白的省略，但列號保持原本的位置。
- 值的寫法：
  - `None` 寫成空白。
  - 午夜的 datetime 寫 `YYYY-MM-DD`，其他 datetime 寫 `YYYY-MM-DD HH:MM:SS`。
  - date 寫 ISO 格式，time 寫 `HH:MM:SS`。
  - bool 寫 `TRUE`、`FALSE`。
  - 其他用 `str(value)`。
  - `|` 寫成 `\|`，換行寫成 `<br>`。
- 公式顯示檔案裡存的計算結果。有些程式存檔時不存計算結果，這些格子會是空白。
- read-only 模式下 openpyxl 報的範圍可能不準，所以逐列讀完才決定範圍。

**pptx**（python-pptx）

- 每張投影片一節：`## Slide {n}: {標題}`；沒有標題就寫 `## Slide {n}`；隱藏的（`p:sld` 的 `show="0"`）加 ` (hidden)`。
- 標題以外的圖形依 (top, left) 排序（沒有位置的當 0）。群組圖形依同樣順序遞迴處理。
- 文字框每段一行：層級 0 直接寫；層級 1 以上寫成 `"  " * (層級 - 1) + "- " + 文字`。
- 表格寫法同 docx。
- 圖片寫 `[Picture: {descr}]`，沒有替代文字就寫 `[Picture]`。圖表寫 `[Chart: {標題}]`，沒有標題就寫 `[Chart]`。
- 備忘稿不是空的時候，放在投影片最後：先一行 `Notes:`，再接備忘稿文字。
- 投影片之間空一行。

**pdf**（pypdfium2）

- 每頁一節：`## Page {n}`，空一行，再接頁面文字。
  - `\r\n` 和 `\r` 統一成 `\n`，去掉每行結尾的空白。
- 去掉空白後沒有文字的頁面寫：`[No text on this page: it may be a scan or a picture. To see it, read it as an image with pages="{n}".]`
- 有密碼的 PDF：pdfium 的錯誤訊息含 `password` 時，回報 `'{path}' is password-protected, so it cannot be read.`

### 3.5 PDF 頁面當圖片（`pages`）

- 格式：逗號分隔的頁碼或範圍，例如 `"3"`、`"3-5"`、`"1,4"`、`"1-2,7"`。
  - 空白忽略；範圍必須由小到大；重複的頁去掉；照給的順序。
  - 頁數超過 5 頁就回錯誤。
- 轉成圖：
  - 每頁用 `page.render(scale=…)` 轉成圖。scale 讓長邊剛好 2048 px，但不超過 300 DPI（`scale <= 300 / 72`）。
  - 轉好的圖照 §3.2 的方式處理和存放。
  - 標籤是 `{path}, page {n}`。
- 工具結果：

  ```json
  {"status": "success", "path": "scan.pdf", "total_pages": 12, "pages": [3, 4, 5],
   "message": "The pages are shown as images in the message after the tool results."}
  ```

### 3.6 錯誤處理總表

所有錯誤都回 `{"status": "error", "message": …}`，不丟出例外。

| 情況 | 訊息 |
| --- | --- |
| HEIC | `HEIC images are not supported. Ask for a JPEG or PNG instead.` |
| 舊版 Office | `Old Office formats (.doc, .xls, .ppt) are not supported. Ask for .docx, .xlsx or .pptx instead, or convert the file with LibreOffice (soffice) if it is installed.` |
| 其他二進位檔 | `File '{path}' appears to be binary ({size} bytes). read_file reads text files, images (…) …`（全文見 §3.1） |
| 檔案壞掉或副檔名不符 | `Could not read '{path}' as {kind}: {reason}`，`{kind}` 是 `an image`、`a PDF`、`a Word document`、`an Excel workbook`、`a PowerPoint presentation` 其中之一 |
| `.docx/.xlsx/.pptx` 實際上是 OLE 容器（開頭是 `D0 CF 11 E0`，加密的 Office 檔或改了副檔名的舊格式） | `'{path}' is password-protected or in an old Office format, so it cannot be read.` |
| 有密碼的 PDF | `'{path}' is password-protected, so it cannot be read.` |
| zip 解壓後總大小超過 1 GiB | `'{path}' would take more than 1 GB unzipped, so it was not read.` |
| 圖片像素超過 Pillow 的上限 | 歸在「檔案壞掉」那一列，`{reason}` 是 Pillow 的訊息 |
| 對非 PDF 給了 `pages` | `pages applies to PDF files only.` |
| `pages` 格式不對 | `pages must be page numbers or ranges, like "3", "3-5" or "1,4".` |
| 頁碼超出範圍 | `Page {n} is out of range: the PDF has {total} pages.` |
| 一次超過 5 頁 | `At most 5 pages can be shown at once; ask for the others in another call.` |
| 存放處寫不進去（例如磁碟滿了） | 歸在「檔案壞掉」那一列，`{reason}` 是作業系統的訊息 |
| 組請求時圖片檔不見了、超出 10 張、模型不能看圖 | 換成 §3.3 的說明文字，請求照常送出 |
| 查不到模型能不能看圖 | 當作可以看，記錄 warning（§3.3） |

## 4. 測試策略

測試用的檔案都在測試裡即時產生，不把二進位檔放進 git：
- 圖片用 Pillow 畫。
- Office 檔用 python-docx、openpyxl、python-pptx 寫。
- PDF 用 pypdfium2 做，包含有文字的頁面（`FPDFText_LoadStandardFont`）和沒有文字的頁面。
- 有密碼的 PDF 用 pypdf 做。pypdf 只加進開發依賴。

**`tests/test_images.py`（新）**

- 依 EXIF 方向轉正。
- 長邊縮到 2048，小圖不放大。
- 透明的地方變成白色。
- 動畫取第一格，並回報格數。
- 同一張圖存成同一個檔名，而且只有一份。
- 超過 30 天的檔案在存新檔時被刪掉。
- 壞掉的圖回錯誤。

**`tests/test_documents.py`（新）**

- docx：
  - 標題的 `#` 數。
  - 清單：樣式造成的清單和 `w:numPr` 造成的清單，層級縮排都正確。
  - 表格的表頭、`<br>`、`\|`。
  - `[Image: 替代文字]`。
  - 區塊照文件順序。
- xlsx：
  - 欄名和列號。
  - 整列空白的省略，列號不變。
  - 日期與時間的寫法、TRUE/FALSE。
  - 隱藏的工作表會標示，空的工作表寫 `(empty)`。
- pptx：
  - 標題，以及依位置排序的圖形。
  - 層級縮排、表格。
  - `[Picture]`、備忘稿、隱藏的投影片。
- pdf：
  - 頁標。
  - 沒有文字的頁面的說明。
  - 有密碼的 PDF。
- 共通：
  - 同樣內容只轉一次。
  - 到 `MAX_CHARS` 時截斷並加註；測試時把上限調小。
  - 解壓後太大的檔案被拒絕；測試時把上限調小。
  - OLE 容器（以 `D0 CF 11 E0` 開頭的檔案）的訊息。
  - 超過 30 天的轉檔結果被刪掉。

**`tests/test_file_tools.py`（擴充）**

- 依副檔名分流；HEIC、舊版 Office、其他二進位檔的訊息。
- 圖片結果的欄位和附圖。
- 轉成文字的文件照行號分頁，`converted_from`、`text_file` 都有出現。
- `pages`：
  - 正常情況：頁數正確、標籤正確。
  - 錯誤情況：格式不對、超出範圍、超過 5 頁、檔案不是 PDF。

**`tests/test_runner.py`（擴充）**

- 附圖存進 tool 訊息的 `payload`。
- 請求裡圖片訊息的位置：
  - 緊接在一輪的 tool 訊息之後。
  - 一輪有好幾個呼叫時只補一則訊息，順序正確。
  - 有被中斷的呼叫時，排在補上的結果之後。
- 圖片數量：
  - 超過 10 張時，最舊的換成說明。
  - 圖檔不見時換成說明。
  - `takes_images()` 為 False 時全部換成說明。
- 沒有圖的對話不會呼叫 `takes_images()`。
- 壓縮對話之後，摘要之前的圖片不再送出。

**`tests/test_openrouter.py`（擴充）**

用 httpx `MockTransport` 測 `takes_images()`：
- 有 `image` 回 True，沒有回 False。
- HTTP 錯誤、格式不對時回 True。
- `:free` 後綴會先去掉。
- 只查一次。

**`tests/test_mcp_manager.py`（Toolbox 的測試在這裡，擴充）**

`Toolbox.run` 回傳 `ToolResult`：文字經過 `limit_output`，附圖原樣帶出。`execute` 仍然回文字。

**`tests/live/test_live.py`（新增一個）**

- 用 Pillow 畫一張大數字（例如 7342）的 PNG，透過上傳 API 附在訊息裡，問「圖上寫的數字是多少」。
- 必須出現 `read_file` 的工具呼叫，而且回覆裡有 7342。
- `.env` 的 API key 在 2026-11-04 13:19（台北）到期，到期後要換新的 key 才能跑。

**手動檢查**

在網頁終端傳一張手機照片和一個 docx，各問一個關於內容的問題。

## 5. 文件與設定

- `pyproject.toml`：
  - dependencies 加 `pillow>=12.0`、`pypdfium2>=5.0`、`python-docx>=1.2`、`openpyxl>=3.1.5`、`python-pptx>=1.0.2`、`defusedxml>=0.7.1`。
  - dev 加 `pypdf`。
  - 一併更新 `uv.lock`。
- `kilo/uploads.py` 的 `NOTE_HEADER`：「read them with your tools when you need them」改成「read them with read_file when you need them」。說明是組請求時才產生的，所以舊對話也會跟著換，代價只是換的那一次快取沒命中。
- README：
  - Features 的 Files 那一行補一句：agent 用 `read_file` 讀它們，包括圖片（模型直接看）和 PDF、Word、Excel、PowerPoint（轉成文字）。
  - Files 一節：把「For now `read_file` reads text files only.」改成支援的格式，並說明 `.kilo_data/images/`、`.kilo_data/converted/` 和 30 天自動清理。
- `.env.example`：不變。
- 網頁終端和 Telegram：不變。

## 6. 風險與後續

**風險**

- **請求變大**：每輪最多重送 10 張圖。每張 JPEG 約 0.3–0.6 MB，base64 之後再多三分之一，一個請求最多約 8 MB；上傳慢的時候，每輪會多幾秒。真的成問題時可以調小 `MAX_SHOWN_IMAGES` 或 `MAX_SIDE`。
- **換模型**：各供應商對每個請求的圖片數有不同的上限。最多 10 張已經比實測可行的 12 張保守；如果 OpenRouter 的模型資訊有錯，請求會失敗，並顯示 OpenRouter 的錯誤訊息。
- **JPEG 的細小文字**：2048 px、品質 85 時，一般的字都看得清楚。PDF 頁面約 175 DPI（A4），也看得清楚。
- **轉換不完整**：
  - docx 的文字方塊、頁首頁尾、註腳不會出現。
  - pptx 的圖表數據、SmartArt 不會出現。
  - xlsx 沒存計算結果的公式會是空白。
  - 多欄排版的 PDF，抽出來的文字順序可能亂掉。這時 agent 可以用 `pages` 直接看頁面。
- **時間**：大型 xlsx 第一次轉可能要幾十秒。`/stop` 停不了背景 thread（現有的 thread 工具都是這樣），只是丟掉結果。
- **安全**：要解析的檔案可能是陌生人轉傳來的。這次的防護是：
  - 限制解壓後的大小和像素數。
  - XML 不展開實體（openpyxl 透過 defusedxml；python-docx、python-pptx 本身就不展開）。
  - Pillow、PDFium、lxml 是 C 寫的函式庫，要定期更新依賴。

**後續**

- MCP 工具回傳的圖片也用同樣方式給模型看。
- 把 Office 檔內嵌的圖片拿出來給模型看。
- 有安裝 LibreOffice 時，把投影片、Word 頁面轉成圖片看，也順便支援舊版格式。
- HEIC（pillow-heif）。

## 7. 受影響檔案

- 新增：`kilo/images.py`、`kilo/documents.py`、`tests/test_images.py`、`tests/test_documents.py`
- 修改：
  - 程式：`kilo/tools/file_tools.py`、`kilo/tools/base.py`、`kilo/tools/toolbox.py`、`kilo/agent/runner.py`、`kilo/agent/chat_model.py`、`kilo/openrouter.py`、`kilo/uploads.py`
  - 測試：`tests/fakes.py`（`FakeModel.takes_images`）、`tests/test_file_tools.py`、`tests/test_runner.py`、`tests/test_openrouter.py`、`tests/test_mcp_manager.py`、`tests/test_uploads.py`（說明文字）、`tests/live/test_live.py`
  - 其他：`pyproject.toml`、`uv.lock`、`README.md`
