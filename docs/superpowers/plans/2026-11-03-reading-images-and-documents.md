# Kilo 讀圖片與文件 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 agent 用 `read_file` 讀圖片和文件：圖片交給主模型直接看，Word、Excel、PowerPoint、PDF 則轉成可以分頁、可以 grep 的 Markdown 文字。

**Architecture:**
- `kilo/images.py`：把圖片轉正、縮圖，存成 JPEG 放進 `<KILO_DATA_DIR>/images/`。
- `kilo/documents.py`：把文件轉成 Markdown，存進 `<KILO_DATA_DIR>/converted/`；PDF 的頁面可以轉成圖。
- 工具結果可以附圖，型別是 `ToolResult(text, images)`。runner 把附圖的參照記在 tool 訊息的 `payload`，組請求時在這一輪工具結果之後補一則帶圖的 user 訊息，最多附最近 10 張。
- 能不能看圖向 OpenRouter 查：`ChatModel.takes_images()`。

**Tech Stack:** Python ≥ 3.11、Pillow、pypdfium2、python-docx、openpyxl（加 defusedxml）、python-pptx、httpx、pytest + pytest-asyncio（`asyncio_mode = "auto"`）、uv；只在測試用的 pypdf 放在開發依賴。

**Spec:** `docs/superpowers/specs/2026-11-03-reading-images-and-documents-design.md`。本計畫以「spec §x」引用它。

## Global Constraints

- **依賴：** 只用 pip 套件，每個套件在需要它的 task 用 `uv add` 加入，`pyproject.toml` 和 `uv.lock` 跟著那個 task 一起 commit。
  - 正式依賴：`pillow>=12.0`、`python-docx>=1.2`、`openpyxl>=3.1.5`、`defusedxml>=0.7.1`、`python-pptx>=1.0.2`、`pypdfium2>=5.0`。
  - 開發依賴：`pypdf>=6.0`，只用來在測試裡做有密碼的 PDF。
  - 不要求任何系統軟體。
- **常數**（spec §3）：
  - `kilo.images`：`MAX_SIDE = 2048`、`JPEG_QUALITY = 85`、`KEEP_DAYS = 30`
  - `kilo.agent.runner`：`MAX_SHOWN_IMAGES = 10`、`IMAGE_TOKENS = 4_000`
  - `kilo.documents`：`MAX_CHARS = 10_000_000`、`MAX_UNZIPPED_BYTES = 1024**3`、`KEEP_DAYS = 30`、`CONVERTER_VERSION = 1`、`MAX_PAGES_SHOWN = 5`、`PAGE_DPI_LIMIT = 300`
  - `kilo.openrouter`：`LOOKUP_TIMEOUT = 10.0`
- **存放位置：** `<KILO_DATA_DIR>/images/` 和 `<KILO_DATA_DIR>/converted/`。每次呼叫時才讀 `settings.kilo_data_dir`，測試用 `data_dir` fixture 改掉它。
- **給模型的文字：** 一律英文，逐字照 spec。各 task 會列出它用到的原文。
- **不做的事：** 不加新設定、不做資料遷移、不改網頁終端和 Telegram。
- **程式風格：** 註解和 docstring 用英文，沿用專案風格：`typing` 型別標註，註解短，只說明「為什麼」。
- **測試：**
  - 單元測試：`uv run pytest`，目前基準是 `471 passed, 6 deselected`。每個 task 結束時整套都要通過。
  - live 測試：`uv run pytest -m live`，用專案 `.env` 裡的 key。這把 key 在 2026-11-04 13:19（台北）到期。
- **commit：** 每個 commit 訊息的最後一行是 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`。

## Review Focus

1. **大寫副檔名**（手機傳來的 `IMG_1234.JPG`、Windows 的 `REPORT.PDF`）：要照圖片和文件來讀，不能被當成二進位檔拒絕 → Task 8 `test_extensions_are_matched_in_any_case`
2. **模型把 `pages` 給成數字 `3`，不是字串 `"3"`**：要照常顯示那一頁 → Task 8 `test_pages_may_be_a_number`
3. **資料不是從 A1 開始的工作表**（例如報表的標題放在 B2）：列號和欄名要跟儲存格位置對得上 → Task 5 `test_cells_keep_their_addresses`
4. **Word 的文字方塊**：Word 會用 `mc:AlternateContent` 存兩份，文字不能重複出現，文字方塊也不能被當成圖片 → Task 4 `test_text_boxes_are_left_out`
5. **放在內容控制項（`w:sdt`）裡的文字**（例如長報告開頭的目錄）：要讀得出來 → Task 4 `test_content_controls_are_read`

## 檔案結構

| 檔案 | 責任 | Task |
|---|---|---|
| `kilo/images.py`（新） | 圖片轉正、縮圖、轉 JPEG、存放、data URL | 1 |
| `kilo/openrouter.py`、`kilo/agent/chat_model.py`、`tests/fakes.py` | 模型能不能看圖 | 2 |
| `kilo/tools/base.py`、`kilo/tools/toolbox.py`、`kilo/agent/runner.py` | 工具結果附圖、送圖給模型、壓縮指示 | 3 |
| `kilo/documents.py`（新）、`tests/samples.py`（新） | 轉檔、快取、檢查；docx、xlsx、pptx、pdf；PDF 頁面轉圖 | 4–7 |
| `kilo/tools/file_tools.py` | `read_file` 分流、說明、`pages` | 8 |
| `kilo/uploads.py`、`README.md`、`tests/live/test_live.py` | 附件說明、文件、真實模型驗證 | 9 |

---

### Task 1: 圖片（`kilo/images.py`）

**Files:**
- Create: `kilo/images.py`、`tests/test_images.py`
- Modify:
  - `pyproject.toml`、`uv.lock`：`uv add "pillow>=12.0"`
  - `tests/conftest.py`：新增 `data_dir` fixture。

**Interfaces:**
- Consumes: `settings.kilo_data_dir`
- Produces:
  - `tests/conftest.py`：fixture `data_dir(tmp_path, monkeypatch) -> Path`，建立 `tmp_path / "data"`，並把 `settings.kilo_data_dir` 設成它。`tests/test_output_limit.py` 有自己的同名 fixture，會蓋過這個，不用動它。
  - `kilo.images`：
    - `STORE = "images"`、`MAX_SIDE = 2048`、`JPEG_QUALITY = 85`、`KEEP_DAYS = 30`
    - `SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"})`
    - `@dataclass(frozen=True) class Stored`：`file: str`（存放處裡的檔名，`<sha256>.jpg`）、`width: int`、`height: int`（送給模型的尺寸）
    - `@dataclass(frozen=True) class ImageInfo`：`type: str`、`width: int`、`height: int`、`frames: int`、`shown: Stored`
    - `root() -> Path`：回傳 `settings.kilo_data_dir / STORE`。
    - `store(picture: PIL.Image.Image) -> Stored`
    - `read(path: Path) -> ImageInfo`：Pillow 的錯誤（`UnidentifiedImageError`、`DecompressionBombError`、`OSError`）直接丟出，由 Task 8 轉成錯誤訊息。
    - `data_url(file: str) -> Optional[str]`

- [ ] **Step 1: 加入依賴**

Run: `uv add "pillow>=12.0"`
Expected: `pyproject.toml` 的 dependencies 多了 `"pillow>=12.0"`，`uv.lock` 跟著更新。

- [ ] **Step 2: 在 `tests/conftest.py` 的 `workspace` fixture 後面加入 `data_dir`**

```python
@pytest.fixture
def data_dir(tmp_path, monkeypatch) -> Path:
    """KILO_DATA_DIR, for what Kilo keeps there by the settings: saved tool output, images, converted documents."""
    folder = tmp_path / "data"
    folder.mkdir()
    monkeypatch.setattr(settings, "kilo_data_dir", folder)
    return folder
```

- [ ] **Step 3: 寫失敗的測試 `tests/test_images.py`**

```python
"""Images prepared for the model and kept in the image store (kilo/images.py)."""

import base64
import os
import time

import pytest
from PIL import Image, UnidentifiedImageError

from kilo import images

pytestmark = pytest.mark.usefixtures("data_dir")
DAY = 86400


def saved(path, picture, **options):
    picture.save(path, **options)
    return path


def opened(info) -> Image.Image:
    return Image.open(images.root() / info.shown.file)


def test_a_photo_is_turned_upright(tmp_path):
    exif = Image.Exif()
    exif[0x0112] = 6  # the camera was turned: show it rotated 90° clockwise
    info = images.read(saved(tmp_path / "photo.jpg", Image.new("RGB", (40, 20), "red"), exif=exif))
    assert (info.width, info.height) == (20, 40)
    assert opened(info).size == (20, 40)


def test_a_large_image_is_shrunk_to_2048_on_its_long_side(tmp_path):
    info = images.read(saved(tmp_path / "wide.png", Image.new("RGB", (4096, 1024), "blue")))
    assert (info.width, info.height) == (4096, 1024)
    assert (info.shown.width, info.shown.height) == (2048, 512)
    assert opened(info).size == (2048, 512)


def test_a_small_image_keeps_its_size(tmp_path):
    info = images.read(saved(tmp_path / "small.png", Image.new("RGB", (300, 200), "blue")))
    assert (info.shown.width, info.shown.height) == (300, 200)


def test_everything_is_sent_as_jpeg(tmp_path):
    info = images.read(saved(tmp_path / "shot.png", Image.new("RGB", (30, 20), "blue")))
    assert info.type == "image/png"
    assert info.shown.file.endswith(".jpg") and opened(info).format == "JPEG"


def test_transparency_becomes_white(tmp_path):
    info = images.read(saved(tmp_path / "clear.png", Image.new("RGBA", (10, 10), (0, 0, 0, 0))))
    assert all(channel >= 250 for channel in opened(info).convert("RGB").getpixel((5, 5)))


def test_an_animation_shows_its_first_frame(tmp_path):
    frames = [Image.new("RGB", (20, 20), color) for color in ("red", "blue", "green")]
    path = tmp_path / "moving.gif"
    frames[0].save(path, save_all=True, append_images=frames[1:])
    info = images.read(path)
    assert info.frames == 3
    red, green, blue = opened(info).convert("RGB").getpixel((10, 10))
    assert red > 200 and green < 60 and blue < 60


def test_the_type_is_what_the_file_holds(tmp_path):
    assert images.read(saved(tmp_path / "named.jpg", Image.new("RGB", (8, 8)), format="PNG")).type == "image/png"


def test_the_same_image_is_stored_once(tmp_path):
    first = images.read(saved(tmp_path / "a.png", Image.new("RGB", (8, 8), "red")))
    second = images.read(saved(tmp_path / "b.png", Image.new("RGB", (8, 8), "red")))
    assert first.shown.file == second.shown.file
    assert [path.name for path in images.root().iterdir()] == [first.shown.file]


def test_images_unused_for_30_days_are_deleted():
    images.root().mkdir(parents=True)
    old, recent = images.root() / "old.jpg", images.root() / "recent.jpg"
    for path, days in ((old, 31), (recent, 29)):
        path.write_bytes(b"x")
        os.utime(path, (time.time() - days * DAY,) * 2)
    images.store(Image.new("RGB", (8, 8), "red"))
    assert not old.exists() and recent.exists()


def test_storing_an_image_again_keeps_it():
    first = images.store(Image.new("RGB", (8, 8), "red"))
    os.utime(images.root() / first.file, (time.time() - 31 * DAY,) * 2)
    images.store(Image.new("RGB", (8, 8), "red"))
    assert time.time() - (images.root() / first.file).stat().st_mtime < DAY


def test_a_stored_image_as_a_data_url():
    shown = images.store(Image.new("RGB", (8, 8), "red"))
    url = images.data_url(shown.file)
    assert url.startswith("data:image/jpeg;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == (images.root() / shown.file).read_bytes()


def test_a_missing_image_has_no_data_url():
    assert images.data_url("gone.jpg") is None


def test_only_files_in_the_store_are_sent(data_dir):
    (data_dir / "secret.jpg").write_bytes(b"x")
    images.root().mkdir(parents=True)
    assert images.data_url("../secret.jpg") is None


def test_a_broken_image_raises(tmp_path):
    (tmp_path / "broken.png").write_bytes(b"not an image")
    with pytest.raises(UnidentifiedImageError):
        images.read(tmp_path / "broken.png")
```

- [ ] **Step 4: 執行，確認失敗**

Run: `uv run pytest tests/test_images.py -q`
Expected: collection error，`ModuleNotFoundError: No module named 'kilo.images'`。

- [ ] **Step 5: 實作 `kilo/images.py`**

模組 docstring 說明：圖片會被準備成給模型看的樣子並存起來；組請求時把它讀成 data URL（spec §3.2）。

- `store(picture)`，依序做這幾件事：
  1. 有透明度的圖要疊在白底上：判斷條件是 `picture.mode in ("RGBA", "LA", "PA")` 或 `"transparency" in picture.info`。做法是轉成 RGBA，用 `Image.alpha_composite` 疊到白色的 RGBA 底圖上。
  2. 轉成 RGB。
  3. 長邊超過 `MAX_SIDE` 就用 `thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)` 等比縮小；小圖不放大。
  4. 存成 JPEG，`quality=JPEG_QUALITY`。檔名是 JPEG bytes 的 `sha256` hex 加 `.jpg`。
  5. 檔案已經存在就 `os.utime` 更新時間；不存在就先寫到同一資料夾的暫存檔（`.<name>.<uuid>.tmp`），再用 `os.replace` 改名。
  6. 刪掉 `root()` 裡修改時間早於 `KEEP_DAYS` 天的檔案。刪除時用 `missing_ok=True`，`stat` 失敗的就略過，因為別的程序可能同時在清理。
  7. 回傳 `Stored(name, width, height)`。
- `read(path)`：
  1. `with Image.open(path) as picture:` 開檔。
  2. `type` 用 `Image.MIME.get(picture.format, "application/octet-stream")`。
  3. `frames = getattr(picture, "n_frames", 1)`，接著 `picture.seek(0)`。
  4. `upright = ImageOps.exif_transpose(picture)`。
  5. 回傳 `ImageInfo(type, upright.width, upright.height, frames, store(upright))`。
- `data_url(file)`：只讀 `root() / Path(file).name`，所以路徑裡的資料夾一律不理會。讀不到（`OSError`）就回傳 `None`；讀得到就回傳 `"data:image/jpeg;base64," + base64`。

- [ ] **Step 6: 執行，確認通過**

Run: `uv run pytest tests/test_images.py -q`
Expected: `14 passed`

- [ ] **Step 7: 整套測試**

Run: `uv run pytest -q`
Expected: 全部通過，`6 deselected`。

- [ ] **Step 8: Commit**

```bash
git add kilo/images.py tests/test_images.py tests/conftest.py pyproject.toml uv.lock
git commit -m "feat: prepare images for the model in an image store

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: 模型能不能看圖（`OpenRouterClient.takes_images`）

**Files:**
- Modify: `kilo/openrouter.py`、`kilo/agent/chat_model.py`、`tests/fakes.py`、`tests/test_openrouter.py`

**Interfaces:**
- Consumes: `OpenRouterClient` 的 `self._http`、`self.model`
- Produces:
  - `ChatModel.takes_images() -> bool`（async）。在 Protocol 的 docstring 補一句：回答模型能不能收 `image_url` 片段，不確定時回 True。
  - `kilo.openrouter`：
    - `MODELS_URL = "https://openrouter.ai/api/v1/models"`、`LOOKUP_TIMEOUT = 10.0`
    - `logger = logging.getLogger(__name__)`
    - `OpenRouterClient.takes_images() -> bool`（async），查到的結果存在 `self._takes_images: Optional[bool]`。
  - `tests/fakes.py` 的 `FakeModel` 新增：
    - 屬性 `images: bool = True`：`takes_images` 的回答。
    - 屬性 `image_lookups: int = 0`：被問了幾次。
    - `async takes_images()`：計數後回傳 `self.images`。
- 原文：warning 寫成 `OpenRouter: could not look up whether {model} takes images ({error}); assuming it does.`，`{model}` 是 `self.model`。

- [ ] **Step 1: 寫失敗的測試，加在 `tests/test_openrouter.py` 最後**

```python
def listing(*modalities: str):
    body = {"data": {"id": MODEL, "name": "Muse Spark", "architecture": {"input_modalities": list(modalities)}, "endpoints": []}}
    return lambda request: httpx.Response(200, json=body)


def no_network(request):
    raise httpx.ConnectError("no network")


async def test_a_model_listed_with_image_input_takes_images():
    requests: List[httpx.Request] = []
    assert await client_for(listing("text", "image"), requests).takes_images() is True
    assert requests[0].url == f"https://openrouter.ai/api/v1/models/{MODEL}/endpoints"
    assert "authorization" not in requests[0].headers


async def test_a_text_only_model_takes_no_images():
    assert await client_for(listing("text")).takes_images() is False


@pytest.mark.parametrize("responder", [
    lambda request: httpx.Response(404, json={"error": {"message": "Model not found"}}),
    lambda request: httpx.Response(200, content=b"not json"),
    lambda request: httpx.Response(200, json={"data": {"architecture": {}}}),
    lambda request: httpx.Response(200, json={"data": {"architecture": {"input_modalities": "image"}}}),
    no_network,
])
async def test_when_unsure_a_model_is_taken_to_take_images(responder, caplog):
    assert await client_for(responder).takes_images() is True
    assert f"could not look up whether {MODEL} takes images" in caplog.text


async def test_a_variant_of_a_model_is_looked_up_as_the_model():
    requests: List[httpx.Request] = []

    def handle(request):
        requests.append(request)
        return listing("text")(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    client = OpenRouterClient(model=f"{MODEL}:free", effort="low", api_key="test-key", http=http)
    assert await client.takes_images() is False
    assert requests[0].url.path == f"/api/v1/models/{MODEL}/endpoints"


async def test_a_model_is_looked_up_once():
    requests: List[httpx.Request] = []
    client = client_for(listing("text", "image"), requests)
    assert [await client.takes_images(), await client.takes_images()] == [True, True]
    assert len(requests) == 1
```

- [ ] **Step 2: 執行，確認失敗**

Run: `uv run pytest tests/test_openrouter.py -q`
Expected: 新的 9 個測試失敗，`AttributeError: 'OpenRouterClient' object has no attribute 'takes_images'`；原本的測試都通過。

- [ ] **Step 3: 實作**

- `OpenRouterClient.__init__` 加一行 `self._takes_images: Optional[bool] = None`。
- `takes_images()`：值是 `None` 時才去查，查完存起來。
- 查詢的做法（spec §3.3）：
  1. `self._http.get(f"{MODELS_URL}/{self.model.split(':', 1)[0]}/endpoints", timeout=LOOKUP_TIMEOUT)`，不帶 Authorization。
  2. `raise_for_status()`，再取 `response.json()["data"]["architecture"]["input_modalities"]`。
  3. 取到的是 list，就回傳 `"image" in modalities`。
  4. 其他情況回傳 True，並用 `logger.warning` 記錄上面那句原文。其他情況包括：`httpx.HTTPError`、`ValueError`、`KeyError`、`TypeError`，以及 `input_modalities` 不是 list（這時 `{error}` 寫 `input_modalities is not a list`）。
- `ChatModel` Protocol 加上 `async def takes_images(self) -> bool: ...`，並更新 docstring。
- `FakeModel` 加上 Interfaces 列出的屬性和方法。

- [ ] **Step 4: 執行，確認通過**

Run: `uv run pytest tests/test_openrouter.py -q`
Expected: 全部通過。

- [ ] **Step 5: 整套測試**

Run: `uv run pytest -q`
Expected: 全部通過，`6 deselected`。

- [ ] **Step 6: Commit**

```bash
git add kilo/openrouter.py kilo/agent/chat_model.py tests/fakes.py tests/test_openrouter.py
git commit -m "feat: ask OpenRouter whether the model takes images

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: 工具結果附圖，送到模型

**Files:**
- Modify: `kilo/tools/base.py`、`kilo/tools/toolbox.py`、`kilo/agent/runner.py`、`tests/test_runner.py`、`tests/test_mcp_manager.py`

**Interfaces:**
- Consumes:
  - `images.store(picture) -> Stored`、`images.data_url(file) -> Optional[str]`（Task 1）
  - `ChatModel.takes_images()`，以及 `FakeModel.images`、`FakeModel.image_lookups`（Task 2）
- Produces:
  - `kilo.tools.base`，定義在 `Tool` 之前：
    - `@dataclass(frozen=True) class ShownImage`：`file: str`（存放處裡的檔名）、`label: str`（給模型看的標籤）
    - `@dataclass(frozen=True) class ToolResult`：`text: str`、`images: Tuple[ShownImage, ...] = ()`
    - `Tool.execute` 的型別改成 `Callable[[Dict[str, Any]], Awaitable[Union[str, ToolResult]]]`，docstring 跟著改。
  - `Toolbox.run(name, args) -> ToolResult`：文字經過 `limit_output`，附圖原樣帶出；工具回傳 `str` 時包成沒有附圖的 `ToolResult`。`Toolbox.execute(name, args) -> str` 改成回傳 `(await self.run(name, args)).text`。
  - `kilo.agent.runner`：
    - 常數：`MAX_SHOWN_IMAGES = 10`、`IMAGE_TOKENS = 4_000`
    - 原文（spec §3.3）：
      - `IMAGE_LABEL = "[Image: {label}]"`
      - `IMAGE_DROPPED = "[Image: {label}. No longer shown, to keep the conversation small; read the file again to see it.]"`
      - `IMAGE_MISSING = "[Image: {label}. No longer available; read the file again to see it.]"`
      - `IMAGE_UNSEEN = "[Image: {label}. Not shown: the model in use does not take images.]"`
    - `_api_messages(db_messages, show_images: bool = True)`
    - 有附圖時，tool 訊息的 `payload` 多一個欄位：`"images": [{"file": ..., "label": ...}]`。
  - `COMPACTION_INSTRUCTIONS` 在 `- Files the user sent, with their paths.` 這行後面加一行：`- What the images and documents read so far showed, where it matters later; images are not kept after this summary.`

- [ ] **Step 1: 寫失敗的 Toolbox 測試，加在 `tests/test_mcp_manager.py`**

把檔案開頭的 `from kilo.tools.base import Tool` 改成 `from kilo.tools.base import ShownImage, Tool, ToolResult`。

```python
def tool_returning(result):
    async def run(args):
        return result
    return Tool(definition={"name": "look", "description": "d", "parameters": {"type": "object", "properties": {}}}, execute=run)


async def test_run_gives_a_tool_s_text_and_images(manager):
    image = ShownImage(file="a.jpg", label="photo.jpg")
    toolbox = Toolbox([tool_returning(ToolResult(text='{"status": "success", "path": "照片.jpg"}', images=(image,)))], manager)
    result = await toolbox.run("look", {})
    assert result == ToolResult(text='{"status": "success", "path": "照片.jpg"}', images=(image,))
    assert await toolbox.execute("look", {}) == result.text


async def test_run_takes_a_text_result_as_one_without_images(manager):
    toolbox = Toolbox([tool_returning('{"status": "success"}')], manager)
    assert await toolbox.run("look", {}) == ToolResult(text='{"status": "success"}')
```

- [ ] **Step 2: 寫失敗的 runner 測試，加在 `tests/test_runner.py`**

在 import 加上：
- `from PIL import Image`
- `from kilo import images`
- `from kilo.tools.base import ShownImage, ToolResult`
- 從 `kilo.agent.runner` 再 import `IMAGE_TOKENS` 和 `MAX_SHOWN_IMAGES`

```python
def picture(label: str, shade: int = 0) -> ShownImage:
    """An image in the store, as a tool result shows it."""
    return ShownImage(file=images.store(Image.new("RGB", (8, 8), (shade, 0, 0))).file, label=label)


def image_part(shown: ShownImage) -> list:
    return [
        {"type": "text", "text": f"[Image: {shown.label}]"},
        {"type": "image_url", "image_url": {"url": images.data_url(shown.file)}},
    ]


def showing(monkeypatch, kilo, *shown: ShownImage) -> None:
    """Every tool call succeeds and shows `shown`."""
    async def run(name, args):
        return ToolResult(text=json.dumps({"status": "success"}), images=shown)
    monkeypatch.setattr(kilo.toolbox, "run", run)


async def add_turn(kilo, session, call_ids, shown_by_call) -> None:
    """A stored assistant turn calling `call_ids`, and a stored result for each call in `shown_by_call`."""
    calls = [{"id": c, "type": "function", "function": {"name": "read_file", "arguments": "{}"}} for c in call_ids]
    await kilo.db.add_message(session.id, MessageRole.ASSISTANT, payload={"role": "assistant", "content": None, "tool_calls": calls})
    for call_id, shown in shown_by_call.items():
        payload = {"role": "tool", "tool_call_id": call_id, "content": "{}", "images": [{"file": s.file, "label": s.label} for s in shown]}
        await kilo.db.add_message(session.id, MessageRole.TOOL, content="{}", payload=payload)


async def test_images_a_tool_shows_are_stored_and_follow_its_result(kilo, model, session, data_dir, monkeypatch):
    shown = picture("photo.jpg")
    showing(monkeypatch, kilo, shown)
    model.turns.extend([tool_turn("c1", "read_file", {"path": "photo.jpg"}), text_turn("a red square")])
    await collect(kilo, session.id, "look")
    stored = [m for m in await kilo.db.get_messages(session.id) if m.role == MessageRole.TOOL]
    assert stored[0].payload["images"] == [{"file": shown.file, "label": "photo.jpg"}]
    assert [m["role"] for m in model.requests[1]] == ["user", "assistant", "tool", "user"]
    assert model.requests[1][-1] == {"role": "user", "content": image_part(shown)}


async def test_one_message_follows_all_the_results_of_a_turn(kilo, session, data_dir):
    first, second = picture("a.png", 0), picture("b.png", 200)
    await kilo.db.add_message(session.id, MessageRole.USER, content="compare")
    await add_turn(kilo, session, ["c1", "c2"], {"c1": [first], "c2": [second]})
    messages = _api_messages(await kilo.db.get_messages(session.id))
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "tool", "user"]
    assert messages[-1]["content"] == image_part(first) + image_part(second)


async def test_images_follow_the_stand_ins_of_interrupted_calls(kilo, session, data_dir):
    shown = picture("a.png")
    await kilo.db.add_message(session.id, MessageRole.USER, content="look")
    await add_turn(kilo, session, ["c1", "c2"], {"c1": [shown]})
    messages = _api_messages(await kilo.db.get_messages(session.id))
    assert messages[-2:] == [interrupted("c2"), {"role": "user", "content": image_part(shown)}]


async def test_only_the_last_10_images_are_shown(kilo, session, data_dir):
    shown = [picture(f"{i}.png", i * 20) for i in range(MAX_SHOWN_IMAGES + 1)]
    await kilo.db.add_message(session.id, MessageRole.USER, content="look at them all")
    for i, image in enumerate(shown):
        await add_turn(kilo, session, [f"c{i}"], {f"c{i}": [image]})
    messages = _api_messages(await kilo.db.get_messages(session.id))
    image_messages = [m["content"] for m in messages if m["role"] == "user" and isinstance(m["content"], list)]
    assert image_messages[0] == [{"type": "text", "text": (
        "[Image: 0.png. No longer shown, to keep the conversation small; read the file again to see it.]"
    )}]
    assert image_messages[1:] == [image_part(image) for image in shown[1:]]


async def test_an_image_no_longer_stored_is_described(kilo, session, data_dir):
    await kilo.db.add_message(session.id, MessageRole.USER, content="look")
    await add_turn(kilo, session, ["c1"], {"c1": [ShownImage(file="gone.jpg", label="old.png")]})
    messages = _api_messages(await kilo.db.get_messages(session.id))
    assert messages[-1] == {"role": "user", "content": [
        {"type": "text", "text": "[Image: old.png. No longer available; read the file again to see it.]"},
    ]}


async def test_a_model_that_takes_no_images_is_told_instead(kilo, model, session, data_dir, monkeypatch):
    model.images = False
    showing(monkeypatch, kilo, picture("photo.jpg"))
    model.turns.extend([tool_turn("c1", "read_file", {"path": "photo.jpg"}), text_turn("I cannot see it")])
    await collect(kilo, session.id, "look")
    assert model.requests[1][-1] == {"role": "user", "content": [
        {"type": "text", "text": "[Image: photo.jpg. Not shown: the model in use does not take images.]"},
    ]}


async def test_a_conversation_without_images_does_not_ask_the_model(kilo, model, session):
    model.turns.extend([tool_turn("c1", "glob", {"pattern": "*"}), text_turn("done")])
    await collect(kilo, session.id, "go")
    assert model.image_lookups == 0


async def test_an_image_counts_toward_compaction(kilo, model, session, data_dir, monkeypatch):
    monkeypatch.setattr(settings, "kilo_compact_at_tokens", 10_000)
    showing(monkeypatch, kilo, picture("photo.jpg"))
    call = tool_turn("c1", "read_file", {"path": "photo.jpg"})
    call[-1]["context_tokens"] = 10_000 - IMAGE_TOKENS // 2  # below the threshold until the image counts
    model.turns.extend([call, text_turn("the summary"), text_turn("finished")])
    assert "compaction_done" in names(await collect(kilo, session.id, "look"))


async def test_the_summary_sees_the_images_and_later_requests_do_not(kilo, model, session, data_dir, monkeypatch):
    shown = picture("photo.jpg")
    showing(monkeypatch, kilo, shown)
    model.turns.extend([
        tool_turn("c1", "read_file", {"path": "photo.jpg"}), text_turn("red"), text_turn("the summary"), text_turn("ok"),
    ])
    await collect(kilo, session.id, "look")
    assert (await kilo.runner.compact(session.id))["event"] == "compaction_done"
    await collect(kilo, session.id, "and now?")
    assert {"role": "user", "content": image_part(shown)} in model.requests[2]
    assert "image_url" not in json.dumps(model.requests[3])


def test_a_summary_keeps_what_images_showed():
    assert (
        "- What the images and documents read so far showed, where it matters later; "
        "images are not kept after this summary." in COMPACTION_INSTRUCTIONS
    )
```

- [ ] **Step 3: 執行，確認失敗**

Run: `uv run pytest tests/test_runner.py tests/test_mcp_manager.py -q`
Expected: collection error，`ImportError: cannot import name 'ShownImage' from 'kilo.tools.base'`。

- [ ] **Step 4: 實作**

- `base.py`：加入 Interfaces 列出的兩個 dataclass，並改 `Tool.execute` 的型別。
- `toolbox.py`：
  - `_execute` 的回傳型別改成 `Union[str, ToolResult]`。
  - 新增 `run`：把結果統一成 `ToolResult`，`text` 套用 `limit_output(name, ...)`。
  - `execute` 改成呼叫 `run` 再取 `.text`，docstring 跟著改。
- `runner.py`：
  - **找出摘要之後的訊息：** 抽出 `_since_summary(db_messages)`，從最新摘要開始切，`_api_messages` 原本那幾行就是在做這件事。再寫 `_has_images(db_messages)`：摘要之後有沒有哪則 tool 訊息的 `payload` 帶 `images`。
  - **`_api_messages(db_messages, show_images=True)`：**
    1. 先依序收集摘要之後所有 tool 訊息裡的附圖。編號小於 `len(全部) - MAX_SHOWN_IMAGES` 的換成說明文字。
    2. 本來在處理每則非 tool 訊息之前、以及迴圈結束時都會呼叫 `close_pending()`。把這兩處都換成 `close_turn()`：先 `close_pending()`，如果這一輪有附圖，再接一則 `{"role": "user", "content": [...]}`。
    3. 每張圖產生的內容片段：
       - `show_images` 為 False：只放 `IMAGE_UNSEEN` 一段文字。
       - 編號太舊：只放 `IMAGE_DROPPED`。
       - `images.data_url(file)` 回傳 None：只放 `IMAGE_MISSING`。
       - 其他情況：`IMAGE_LABEL` 一段文字，接著 `{"type": "image_url", "image_url": {"url": data_url}}`。
    4. docstring 補上附圖的規則。
  - **`AgentRunner._request_messages(session_id) -> List[Dict[str, Any]]`：** 讀出歷史；`_has_images` 為真時才 `await self.client.takes_images()`，否則當作 True；再呼叫 `_api_messages(history, show)`。迴圈裡的每一輪和 `_compact` 都改用它來組請求。
  - **執行工具：** 改呼叫 `self.toolbox.run`。沒有執行的兩種情況（回覆被截斷、參數不合法）包成 `ToolResult(text=...)`。
    - `unmeasured += len(result.text) + len(result.images) * IMAGE_TOKENS * CHARS_PER_TOKEN`
    - `payload` 有附圖時加上 `"images"`。
    - 送出的 `tool_call_result` 事件照舊只解析文字。
  - **`COMPACTION_INSTRUCTIONS`：** 加上 Interfaces 列出的那一行。

- [ ] **Step 5: 執行，確認通過**

Run: `uv run pytest tests/test_runner.py tests/test_mcp_manager.py -q`
Expected: 全部通過。

- [ ] **Step 6: 整套測試**

Run: `uv run pytest -q`
Expected: 全部通過，`6 deselected`。

- [ ] **Step 7: Commit**

```bash
git add kilo/tools/base.py kilo/tools/toolbox.py kilo/agent/runner.py tests/test_runner.py tests/test_mcp_manager.py
git commit -m "feat: send the images tool results show after the results

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: 文件轉文字的核心與 Word（`kilo/documents.py`）

**Files:**
- Create: `kilo/documents.py`、`tests/test_documents.py`、`tests/samples.py`
- Modify: `pyproject.toml`、`uv.lock`：`uv add "python-docx>=1.2"`

**Interfaces:**
- Consumes: `settings.kilo_data_dir`；`data_dir` fixture（Task 1）
- Produces:
  - `kilo.documents`：
    - `STORE = "converted"`、`MAX_CHARS = 10_000_000`、`MAX_UNZIPPED_BYTES = 1024**3`、`KEEP_DAYS = 30`、`CONVERTER_VERSION = 1`
    - `class DocumentError(Exception)`：訊息是「`'{path}' …`」後半句，由 Task 8 補上前面的路徑。
    - 原文（spec §3.4、§3.6）：
      - `OLE_CONTAINER = "is password-protected or in an old Office format, so it cannot be read."`
      - `TOO_LARGE_UNZIPPED = "would take more than 1 GB unzipped, so it was not read."`
      - `STOPPED = "[Conversion stopped here: the text passed {limit:,} characters. The rest of the file is not included.]"`
    - `@dataclass(frozen=True) class Kind`：
      - `name: str`：例如 `"docx"`，就是 `converted_from` 的值。
      - `described: str`：例如 `"a Word document"`，放在錯誤訊息裡。
      - `lines: Callable[[Path], Iterator[str]]`
      - `zipped: bool`：True 表示是 Office Open XML 的 zip 檔，要檢查 OLE 檔頭和解壓後的大小。
    - `KINDS: Dict[str, Kind]`：鍵是小寫副檔名。這個 task 只放 `".docx": Kind("docx", "a Word document", _docx_lines, True)`。
    - `kind_of(path: Path) -> Optional[Kind]`
    - `root() -> Path`：回傳 `settings.kilo_data_dir / STORE`。
    - `convert(path: Path) -> Path`
    - `_table(rows: Sequence[Sequence[str]]) -> List[str]`：Markdown 表格，Task 5、6 也會用。
  - `tests/samples.py`：`png_bytes(color="red") -> io.BytesIO`、`word(path, build) -> Path`

- [ ] **Step 1: 加入依賴**

Run: `uv add "python-docx>=1.2"`
Expected: `pyproject.toml`、`uv.lock` 更新。

- [ ] **Step 2: 建立 `tests/samples.py`**

```python
"""Files of the kinds read_file reads, made on the spot, so the repository holds no binary test files."""

import io
from pathlib import Path
from typing import Any, Callable

import docx
from PIL import Image


def png_bytes(color: str = "red") -> io.BytesIO:
    buffer = io.BytesIO()
    Image.new("RGB", (40, 30), color).save(buffer, "PNG")
    buffer.seek(0)
    return buffer


def word(path: Path, build: Callable[[Any], Any]) -> Path:
    """A Word document that `build` fills (it gets a python-docx Document)."""
    document = docx.Document()
    build(document)
    document.save(str(path))
    return path
```

- [ ] **Step 3: 寫失敗的測試 `tests/test_documents.py`**

```python
"""Documents turned into Markdown text for read_file (kilo/documents.py)."""

import os
import time
from dataclasses import replace
from pathlib import Path

import pytest
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls
from docx.shared import Inches

from kilo import documents
from samples import png_bytes, word

pytestmark = pytest.mark.usefixtures("data_dir")
DAY = 86400
MC = 'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'


def text_of(path: Path) -> str:
    return documents.convert(path).read_text(encoding="utf-8")


def test_a_word_document_in_order(tmp_path):
    def build(document):
        document.add_heading("季度報告", level=0)
        document.add_heading("Results", level=2)
        document.add_paragraph("Sales grew.")
        document.add_paragraph("")
        document.add_paragraph("First point", style="List Bullet")
        document.add_paragraph("Nested point", style="List Bullet 2")
        table = document.add_table(rows=2, cols=2)
        table.cell(0, 0).text, table.cell(0, 1).text = "Item", "Qty"
        table.cell(1, 0).text = "Pen"
        table.cell(1, 0).add_paragraph("blue")
        table.cell(1, 1).text = "3 | 4"
        document.add_picture(png_bytes(), width=Inches(1))
        document.inline_shapes[0]._inline.docPr.set("descr", "A red box")

    assert text_of(word(tmp_path / "report.docx", build)) == "\n".join([
        "# 季度報告", "",
        "## Results", "",
        "Sales grew.", "",
        "- First point", "",
        "  - Nested point", "",
        "| Item | Qty |",
        "| --- | --- |",
        "| Pen<br>blue | 3 \\| 4 |", "",
        "[Image: A red box]", "",
    ])


def test_a_numbered_paragraph_is_a_list_item_at_its_level(tmp_path):
    def build(document):
        document.add_paragraph("Step two")._p.get_or_add_pPr().append(
            parse_xml(f'<w:numPr {nsdecls("w")}><w:ilvl w:val="1"/><w:numId w:val="1"/></w:numPr>'))

    assert text_of(word(tmp_path / "steps.docx", build)) == "  - Step two\n"


def test_links_insertions_tabs_and_breaks_are_read_and_deletions_are_not(tmp_path):
    def build(document):
        paragraph = document.add_paragraph("See ")
        for xml in (
            '<w:hyperlink {w}><w:r><w:t>the site</w:t></w:r></w:hyperlink>',
            '<w:ins {w} w:id="1" w:author="a"><w:r><w:t xml:space="preserve"> now</w:t></w:r></w:ins>',
            '<w:del {w} w:id="2" w:author="a"><w:r><w:delText> never</w:delText></w:r></w:del>',
            '<w:r {w}><w:tab/><w:t>tabbed</w:t><w:br/><w:t>next line</w:t></w:r>',
        ):
            paragraph._p.append(parse_xml(xml.format(w=nsdecls("w"))))

    assert text_of(word(tmp_path / "edits.docx", build)) == "See the site now\ttabbed\nnext line\n"


def test_text_boxes_are_left_out(tmp_path):
    box = (
        f'<w:r {nsdecls("w", "wp")} {MC}><mc:AlternateContent>'
        '<mc:Choice Requires="wps"><w:drawing><wp:inline><wp:docPr id="9" name="Text Box 1" descr="a box"/>'
        '</wp:inline><w:t>boxed</w:t></w:drawing></mc:Choice>'
        '<mc:Fallback><w:pict><w:t>boxed</w:t></w:pict></mc:Fallback>'
        '</mc:AlternateContent></w:r>'
    )

    def build(document):
        document.add_paragraph("Body")._p.append(parse_xml(box))

    assert text_of(word(tmp_path / "box.docx", build)) == "Body\n"


def test_content_controls_are_read(tmp_path):
    def build(document):
        document.element.body.insert(0, parse_xml(
            f'<w:sdt {nsdecls("w")}><w:sdtContent><w:p><w:r><w:t>Contents</w:t></w:r></w:p></w:sdtContent></w:sdt>'))
        document.add_paragraph("Chapter one")

    assert text_of(word(tmp_path / "toc.docx", build)) == "Contents\n\nChapter one\n"


def test_kinds_by_extension_in_any_case():
    assert documents.kind_of(Path("NOTES.DOCX")).name == "docx"
    assert documents.kind_of(Path("notes.txt")) is None


def test_the_same_content_is_converted_once(tmp_path, monkeypatch):
    first = word(tmp_path / "a.docx", lambda document: document.add_paragraph("same"))
    second = tmp_path / "b.docx"
    second.write_bytes(first.read_bytes())
    converted = documents.convert(first)
    again = replace(documents.KINDS[".docx"], lines=lambda path: pytest.fail("converted again"))
    monkeypatch.setitem(documents.KINDS, ".docx", again)
    assert documents.convert(second) == converted


def test_a_long_text_stops_at_the_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(documents, "MAX_CHARS", 30)

    def build(document):
        for i in range(10):
            document.add_paragraph(f"paragraph {i}")

    assert text_of(word(tmp_path / "long.docx", build)) == (
        "paragraph 0\n\nparagraph 1\n\n"
        "[Conversion stopped here: the text passed 30 characters. The rest of the file is not included.]\n"
    )


def test_a_file_too_large_unzipped_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(documents, "MAX_UNZIPPED_BYTES", 1000)
    with pytest.raises(documents.DocumentError) as error:
        documents.convert(word(tmp_path / "big.docx", lambda document: document.add_paragraph("x")))
    assert str(error.value) == "would take more than 1 GB unzipped, so it was not read."


def test_an_ole_container_is_refused(tmp_path):
    path = tmp_path / "locked.docx"
    path.write_bytes(bytes.fromhex("D0CF11E0A1B11AE1") + bytes(504))
    with pytest.raises(documents.DocumentError) as error:
        documents.convert(path)
    assert str(error.value) == "is password-protected or in an old Office format, so it cannot be read."


def test_conversions_unused_for_30_days_are_deleted(tmp_path):
    documents.root().mkdir(parents=True)
    old, recent = documents.root() / "old.md", documents.root() / "recent.md"
    for path, days in ((old, 31), (recent, 29)):
        path.write_text("x")
        os.utime(path, (time.time() - days * DAY,) * 2)
    documents.convert(word(tmp_path / "new.docx", lambda document: document.add_paragraph("new")))
    assert not old.exists() and recent.exists()
```

- [ ] **Step 4: 執行，確認失敗**

Run: `uv run pytest tests/test_documents.py -q`
Expected: collection error，`ImportError: cannot import name 'documents' from 'kilo'`。

- [ ] **Step 5: 實作 `kilo/documents.py`（核心與 docx）**

模組 docstring 說明：文件會被轉成 Markdown 存起來，給 `read_file` 分頁、給 grep 搜尋（spec §3.4）。

**`convert(path)`**
1. `kind_of(path)` 是 None 時丟出 `ValueError`。
2. 檔名是 `f"{CONVERTER_VERSION}\n".encode()` 加上檔案內容的 sha256，以 1 MiB 為一段讀進來。
3. `root()/<hex>.md` 已經存在：`os.utime` 更新時間後直接回傳。
4. `kind.zipped` 時先檢查：
   - 檔頭 8 bytes 是 `D0 CF 11 E0 A1 B1 1A E1`：丟出 `DocumentError(OLE_CONTAINER)`。
   - 用 `zipfile.ZipFile` 打開，所有項目的 `file_size` 加總超過 `MAX_UNZIPPED_BYTES`：丟出 `DocumentError(TOO_LARGE_UNZIPPED)`。
   - 其他錯誤，例如 `BadZipFile`，照原樣往外丟。
5. 用 `_capped(kind.lines(path))` 得到文字，先寫到暫存檔（`.<name>.<uuid>.tmp`），再用 `os.replace` 改名。
6. 刪掉 `root()` 裡修改時間早於 `KEEP_DAYS` 天的檔案，做法和 Task 1 相同。

**`_capped(lines) -> str`**：上限 `MAX_CHARS` 在函式裡才讀，測試才能用 monkeypatch 改掉它。

```python
def _capped(lines: Iterable[str]) -> str:
    """The lines joined, up to MAX_CHARS characters; past it the rest is left out, and a line says so."""
    kept: List[str] = []
    size = 0
    for line in lines:
        size += len(line) + 1
        if size > MAX_CHARS:
            kept += [STOPPED.format(limit=MAX_CHARS), ""]
            break
        kept.append(line)
    return "\n".join(kept)
```

**`_table(rows)`**
- 第一列當表頭，下一行是 `| --- | … |`，之後每列一行。
- 各列的格數補到最寬那一列。
- 每格的 `|` 寫成 `\|`；`\r\n`、`\n` 寫成 `<br>`。
- 每一行的格式是 `"| " + " | ".join(cells) + " |"`。

**docx**（spec §3.4）。命名空間：`w` 是 wordprocessingml/2006/main，`wp` 是 drawingml/2006/wordprocessingDrawing，`pic` 是 drawingml/2006/picture，`mc` 是 markup-compatibility/2006。

- **`_docx_lines(path)`：**
  1. 用 `docx.Document(str(path))` 打開。
  2. 依序走 body：`w:p` 是段落，`w:tbl` 是表格，`w:sdt` 就遞迴走它的 `w:sdtContent`；其他子元素（例如 `w:sectPr`）略過。
  3. 每個不是空的區塊輸出它的幾行，後面接一行 `""`。
- **段落的文字：** 往下走段落的子孫元素：
  - 跳過 `w:pPr`、`w:rPr`、`w:drawing`、`w:pict`、`mc:AlternateContent` 底下的所有內容。`w:pPr` 裡有定位點設定，`w:drawing`、`w:pict`、`mc:AlternateContent` 是文字方塊，Word 會存兩份。
  - `w:t` 取文字，`w:tab` 轉成 `\t`，`w:br`、`w:cr` 轉成 `\n`。
  - 最後去掉前後空白。
- **段落的寫法：** 依序判斷：
  - 樣式是 `Title`：寫 `# `。
  - 樣式是 `Heading N`（N 為 1–6）：寫 N 個 `#` 加一個空格。
  - 段落有 `w:pPr/w:numPr`，或樣式名稱以 `List` 開頭：寫 `"  " * (層級 - 1) + "- "`。層級依序取 `w:ilvl` 加 1、樣式名稱結尾的數字，都沒有就是 1。
  - 其他：照原文寫。

  樣式從 `docx.text.paragraph.Paragraph(element, document).style.name` 取得。
- **段落裡的圖片：** 每個裡面有 `pic:pic` 的 `wp:inline` 或 `wp:anchor` 另起一行，寫 `[Image: {wp:docPr 的 descr}]`；沒有 descr 就寫 `[Image]`。
- **表格：** 用 `docx.table.Table(element, document)`。每一格的文字是格子裡所有 `w:p` 的文字（包括巢狀表格裡的段落）去掉空的之後用 `\n` 連起來，再交給 `_table`。
- 沒有文字也沒有圖片的段落不輸出。

- [ ] **Step 6: 執行，確認通過**

Run: `uv run pytest tests/test_documents.py -q`
Expected: `11 passed`

- [ ] **Step 7: 整套測試**

Run: `uv run pytest -q`
Expected: 全部通過，`6 deselected`。

- [ ] **Step 8: Commit**

```bash
git add kilo/documents.py tests/test_documents.py tests/samples.py pyproject.toml uv.lock
git commit -m "feat: convert Word documents to Markdown text

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Excel

**Files:**
- Modify:
  - `kilo/documents.py`、`tests/samples.py`、`tests/test_documents.py`
  - `pyproject.toml`、`uv.lock`：`uv add "openpyxl>=3.1.5" "defusedxml>=0.7.1"`

**Interfaces:**
- Consumes: `Kind`、`KINDS`、`_table`、`_capped`、`MAX_CHARS`（Task 4）
- Produces:
  - `KINDS[".xlsx"] = Kind("xlsx", "an Excel workbook", _xlsx_lines, True)`
  - `tests/samples.py`：
    - `workbook(path, build) -> Path`
    - `with_saved_value(path, formula, value) -> Path`：把 `value` 存到 `formula` 旁邊，模擬 Excel 存檔時的樣子。

- [ ] **Step 1: 加入依賴**

Run: `uv add "openpyxl>=3.1.5" "defusedxml>=0.7.1"`
Expected: `pyproject.toml`、`uv.lock` 更新。有裝 defusedxml 時，openpyxl 會自動用它解析 XML。

- [ ] **Step 2: 在 `tests/samples.py` 加入建立工作簿的函式**

在 import 加上 `import zipfile` 和 `import openpyxl`。

```python
def workbook(path: Path, build: Callable[[Any], Any]) -> Path:
    """An Excel workbook that `build` fills (it gets an openpyxl Workbook)."""
    book = openpyxl.Workbook()
    build(book)
    book.save(str(path))
    return path


def with_saved_value(path: Path, formula: str, value: str) -> Path:
    """The workbook with `value` saved after `formula`, as Excel saves it: openpyxl saves formulas without values."""
    with zipfile.ZipFile(path) as source:
        entries = {name: source.read(name) for name in source.namelist()}
    sheet = "xl/worksheets/sheet1.xml"
    before = entries[sheet]
    entries[sheet] = before.replace(f"<f>{formula}</f><v />".encode(), f"<f>{formula}</f><v>{value}</v>".encode())
    assert entries[sheet] != before, f"no formula {formula} without a value in {path.name}"
    with zipfile.ZipFile(path, "w") as target:
        for name, data in entries.items():
            target.writestr(name, data)
    return path
```

- [ ] **Step 3: 寫失敗的測試，加在 `tests/test_documents.py`**

import 改成：
- `from datetime import datetime, time as clock_time`
- `from samples import png_bytes, with_saved_value, word, workbook`

```python
def test_a_sheet_is_a_table_with_its_column_letters_and_row_numbers(tmp_path):
    def build(book):
        sheet = book.active
        sheet.title = "銷售"
        sheet.append(["Date", "Item", "Amount", "Paid"])
        sheet.append([datetime(2026, 1, 3), "Pen | blue", 120, True])
        sheet.append([datetime(2026, 1, 4, 14, 30), "Line\nbreak", 0.5, False])
        sheet["A5"] = clock_time(9, 15)

    assert text_of(workbook(tmp_path / "sales.xlsx", build)) == "\n".join([
        "## Sheet 1: 銷售", "",
        "|   | A | B | C | D |",
        "| --- | --- | --- | --- | --- |",
        "| 1 | Date | Item | Amount | Paid |",
        "| 2 | 2026-01-03 | Pen \\| blue | 120 | TRUE |",
        "| 3 | 2026-01-04 14:30:00 | Line<br>break | 0.5 | FALSE |",
        "| 5 | 09:15:00 |  |  |  |", "",
    ])


def test_cells_keep_their_addresses(tmp_path):
    def build(book):
        book.active["C5"] = "far"
        book.active["E7"] = 3

    assert text_of(workbook(tmp_path / "far.xlsx", build)) == "\n".join([
        "## Sheet 1: Sheet", "",
        "|   | A | B | C | D | E |",
        "| --- | --- | --- | --- | --- | --- |",
        "| 5 |  |  | far |  |  |",
        "| 7 |  |  |  |  | 3 |", "",
    ])


def test_hidden_and_empty_sheets_are_marked(tmp_path):
    def build(book):
        book.active.title = "Empty"
        secret = book.create_sheet("Secret")
        secret.sheet_state = "hidden"
        secret["A1"] = "x"

    assert text_of(workbook(tmp_path / "sheets.xlsx", build)) == "\n".join([
        "## Sheet 1: Empty", "", "(empty)", "",
        "## Sheet 2: Secret (hidden)", "",
        "|   | A |", "| --- | --- |", "| 1 | x |", "",
    ])


def test_a_formula_shows_the_value_saved_with_it(tmp_path):
    def build(book):
        book.active.append([2, "=A1*2"])
        book.active.append([3, "=A2*2"])

    path = with_saved_value(workbook(tmp_path / "formulas.xlsx", build), "A1*2", "4")
    assert text_of(path).splitlines()[4:6] == ["| 1 | 2 | 4 |", "| 2 | 3 |  |"]


def test_a_long_sheet_stops_at_the_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(documents, "MAX_CHARS", 200)

    def build(book):
        for i in range(1000):
            book.active.append([i])

    text = text_of(workbook(tmp_path / "long.xlsx", build))
    assert text.endswith("[Conversion stopped here: the text passed 200 characters. The rest of the file is not included.]\n")
    assert "| 1000 |" not in text
```

- [ ] **Step 4: 執行，確認失敗**

Run: `uv run pytest tests/test_documents.py -q`
Expected: 新的 5 個測試失敗，`ValueError`（`kind_of` 還不認得 `.xlsx`）；Task 4 的 11 個通過。

- [ ] **Step 5: 實作 `_xlsx_lines(path)`**（spec §3.4）

1. 用 `openpyxl.load_workbook(str(path), read_only=True, data_only=True)` 打開，處理完一定要 `close()`。
2. 依序處理 `workbook.worksheets` 的每一張：
   - 先輸出 `## Sheet {n}: {title}`；`sheet_state != "visible"` 時加 ` (hidden)`。再輸出 `""`。
   - 用 `iter_rows(values_only=True)` 逐列讀。這個迭代從第 1 列、第 A 欄開始，前面的空列和空欄也會補上，所以列號可以直接用 `enumerate(..., 1)`。
   - 只留有值的列，記下列號和格式化後的值。
   - 收集到的字數超過 `MAX_CHARS` 時就停止讀列：`convert` 反正也會在那裡截斷，這樣大型工作表不會整份放進記憶體。
   - 表格的寬度是有值的最後一欄。
   - 沒有任何值：輸出 `(empty)`。
   - 有值：用 `_table([[" ", A, B, …], [列號, 值, …], …])` 輸出。表頭第一格是一個空白，所以表頭會是 `|   | A |`；欄名用 `openpyxl.utils.get_column_letter`。
   - 最後再輸出 `""`。
3. 值的寫法依序判斷，bool 要放在數字之前：
   - `None`：`""`
   - bool：`TRUE` / `FALSE`
   - 午夜的 `datetime`：`YYYY-MM-DD`
   - 其他 `datetime`：`isoformat(sep=" ")`
   - `date`、`time`：`isoformat()`
   - 其他：`str(value)`

   `|` 和換行留給 `_table` 處理。

- [ ] **Step 6: 執行，確認通過**

Run: `uv run pytest tests/test_documents.py -q`
Expected: `16 passed`

- [ ] **Step 7: 整套測試**

Run: `uv run pytest -q`
Expected: 全部通過，`6 deselected`。

- [ ] **Step 8: Commit**

```bash
git add kilo/documents.py tests/samples.py tests/test_documents.py pyproject.toml uv.lock
git commit -m "feat: convert Excel workbooks to Markdown tables

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: PowerPoint

**Files:**
- Modify:
  - `kilo/documents.py`、`tests/samples.py`、`tests/test_documents.py`
  - `pyproject.toml`、`uv.lock`：`uv add "python-pptx>=1.0.2"`

**Interfaces:**
- Consumes: `Kind`、`KINDS`、`_table`（Task 4）
- Produces:
  - `KINDS[".pptx"] = Kind("pptx", "a PowerPoint presentation", _pptx_lines, True)`
  - `tests/samples.py`：`presentation(path, build) -> Path`

- [ ] **Step 1: 加入依賴**

Run: `uv add "python-pptx>=1.0.2"`
Expected: `pyproject.toml`、`uv.lock` 更新。

- [ ] **Step 2: 在 `tests/samples.py` 加入建立簡報的函式**

在 import 加上 `import pptx`。

```python
def presentation(path: Path, build: Callable[[Any], Any]) -> Path:
    """A PowerPoint presentation that `build` fills (it gets a python-pptx Presentation)."""
    deck = pptx.Presentation()
    build(deck)
    deck.save(str(path))
    return path
```

- [ ] **Step 3: 寫失敗的測試，加在 `tests/test_documents.py`**

import 加上：
- `from pptx.chart.data import CategoryChartData`
- `from pptx.enum.chart import XL_CHART_TYPE`
- `from pptx.util import Inches as SlideInches`
- `presentation`：併進原本 `from samples import …` 那一行

```python
def test_a_slide_reads_from_top_to_bottom_then_left_to_right(tmp_path):
    def build(deck):
        slide = deck.slides.add_slide(deck.slide_layouts[5])  # title only
        slide.shapes.title.text = "季度結果"
        for left, text in ((5, "Right"), (1, "Left")):
            slide.shapes.add_textbox(SlideInches(left), SlideInches(2), SlideInches(3), SlideInches(1)).text_frame.text = text
        frame = slide.shapes.add_textbox(SlideInches(1), SlideInches(4), SlideInches(3), SlideInches(2)).text_frame
        frame.text = "Points"
        for level, text in ((1, "Revenue up"), (2, "In Asia")):
            paragraph = frame.add_paragraph()
            paragraph.text, paragraph.level = text, level
        broken = frame.add_paragraph()
        broken.text = "one"
        broken.add_line_break()
        broken.add_run().text = "two"

    assert text_of(presentation(tmp_path / "deck.pptx", build)) == "\n".join([
        "## Slide 1: 季度結果", "",
        "Left", "Right", "Points", "- Revenue up", "  - In Asia", "one\ntwo", "",
    ])


def test_tables_pictures_charts_notes_and_hidden_slides(tmp_path):
    def build(deck):
        slide = deck.slides.add_slide(deck.slide_layouts[6])  # blank: no title
        table = slide.shapes.add_table(2, 2, SlideInches(1), SlideInches(1), SlideInches(4), SlideInches(1)).table
        for (row, column), text in {(0, 0): "Item", (0, 1): "Qty", (1, 0): "Pen", (1, 1): "3"}.items():
            table.cell(row, column).text = text
        picture = slide.shapes.add_picture(png_bytes(), SlideInches(1), SlideInches(3))
        picture._element.nvPicPr.cNvPr.set("descr", "Logo")
        data = CategoryChartData()
        data.categories = ["Q1"]
        data.add_series("Sales", (1,))
        chart = slide.shapes.add_chart(
            XL_CHART_TYPE.COLUMN_CLUSTERED, SlideInches(1), SlideInches(5), SlideInches(4), SlideInches(2), data,
        ).chart
        chart.has_title = True
        chart.chart_title.text_frame.text = "Sales"
        slide.notes_slide.notes_text_frame.text = "Say it slowly"
        slide._element.set("show", "0")

    assert text_of(presentation(tmp_path / "parts.pptx", build)) == "\n".join([
        "## Slide 1 (hidden)", "",
        "| Item | Qty |", "| --- | --- |", "| Pen | 3 |",
        "[Picture: Logo]", "[Chart: Sales]", "Notes:", "Say it slowly", "",
    ])


def test_grouped_shapes_are_read_where_the_group_is(tmp_path):
    def build(deck):
        slide = deck.slides.add_slide(deck.slide_layouts[6])
        group = slide.shapes.add_group_shape()
        for top, text in ((3, "In group, lower"), (1, "In group, upper")):
            group.shapes.add_textbox(SlideInches(1), SlideInches(top), SlideInches(2), SlideInches(1)).text_frame.text = text
        slide.shapes.add_textbox(SlideInches(1), SlideInches(5), SlideInches(2), SlideInches(1)).text_frame.text = "Outside"

    assert text_of(presentation(tmp_path / "group.pptx", build)) == "\n".join([
        "## Slide 1", "", "In group, upper", "In group, lower", "Outside", "",
    ])


def test_slides_follow_one_another(tmp_path):
    def build(deck):
        for title in ("One", "Two"):
            deck.slides.add_slide(deck.slide_layouts[5]).shapes.title.text = title

    assert text_of(presentation(tmp_path / "two.pptx", build)) == "## Slide 1: One\n\n## Slide 2: Two\n"
```

- [ ] **Step 4: 執行，確認失敗**

Run: `uv run pytest tests/test_documents.py -q`
Expected: 新的 4 個測試失敗，`ValueError`（`.pptx` 還不認得）；之前的 16 個通過。

- [ ] **Step 5: 實作 `_pptx_lines(path)`**（spec §3.4）

1. 用 `pptx.Presentation(str(path))` 打開，依序處理每張投影片。
2. **標題：** 取自 `slide.shapes.title`（可能是 None）。標題文字裡的 `\x0b`、`\n` 換成空格，再去掉前後空白。
   - 有標題：輸出 `## Slide {n}: {標題}`；沒有：輸出 `## Slide {n}`。
   - `slide._element.get("show") == "0"` 時加 ` (hidden)`。
3. **內容：** 除了標題圖形以外（用 `shape_id` 比對），把圖形依 `(top or 0, left or 0)` 穩定排序，再依序處理：
   - 群組（`MSO_SHAPE_TYPE.GROUP`）：照同樣的順序遞迴處理 `shape.shapes`。
   - `pptx.shapes.picture.Picture`：輸出 `[Picture: {descr}]`，descr 取自 `shape._element.nvPicPr.cNvPr`；沒有 descr 就輸出 `[Picture]`。
   - `shape.has_chart`：輸出 `[Chart: {標題}]`；`chart.has_title` 為假或標題是空的，就輸出 `[Chart]`。
   - `shape.has_table`：交給 `_table`，每格的文字取 `cell.text`，`\x0b` 換成 `\n`。
   - `shape.has_text_frame`：每段一行，`\x0b` 換成 `\n` 後去掉前後空白，空的段落略過。層級 0 直接輸出；層級 1 以上輸出 `"  " * (level - 1) + "- " + text`。
4. **備忘稿：** `slide.has_notes_slide`，而且 `notes_text_frame` 不是 None、文字也不是空的，就在內容最後加上 `"Notes:"` 和備忘稿文字。
5. **輸出：** 有內容時先輸出 `""` 再輸出內容。每張投影片最後輸出一行 `""`。

- [ ] **Step 6: 執行，確認通過**

Run: `uv run pytest tests/test_documents.py -q`
Expected: `20 passed`

- [ ] **Step 7: 整套測試**

Run: `uv run pytest -q`
Expected: 全部通過，`6 deselected`。

- [ ] **Step 8: Commit**

```bash
git add kilo/documents.py tests/samples.py tests/test_documents.py pyproject.toml uv.lock
git commit -m "feat: convert PowerPoint presentations to Markdown text

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: PDF，以及把頁面轉成圖

**Files:**
- Modify:
  - `kilo/documents.py`、`tests/samples.py`、`tests/test_documents.py`
  - `pyproject.toml`、`uv.lock`：`uv add "pypdfium2>=5.0"`、`uv add --dev "pypdf>=6.0"`

**Interfaces:**
- Consumes: `Kind`、`KINDS`、`_capped`、`DocumentError`（Task 4）；`images.store`、`images.MAX_SIDE`、`images.Stored`（Task 1）
- Produces:
  - `KINDS[".pdf"] = Kind("pdf", "a PDF", _pdf_lines, False)`
  - `MAX_PAGES_SHOWN = 5`、`PAGE_DPI_LIMIT = 300`
  - `class PagesError(ValueError)`：訊息就是要給模型看的完整句子。
  - 原文（spec §3.4–§3.6）：
    - `PASSWORD_PROTECTED = "is password-protected, so it cannot be read."`
    - `NO_TEXT = '[No text on this page: it may be a scan or a picture. To see it, read it as an image with pages="{page}".]'`
    - `BAD_PAGES = 'pages must be page numbers or ranges, like "3", "3-5" or "1,4".'`
    - `OUT_OF_RANGE = "Page {page} is out of range: the PDF has {total} pages."`
    - `TOO_MANY_PAGES = "At most 5 pages can be shown at once; ask for the others in another call."`
  - `page_numbers(spec: str, total: int) -> List[int]`
  - `show_pages(path: Path, spec: str) -> Tuple[int, List[Tuple[int, images.Stored]]]`：回傳總頁數，以及每個要看的頁碼和它存好的圖。
  - `tests/samples.py`：`pdf(path, *texts, size=(595, 842)) -> Path`、`locked_pdf(path) -> Path`

- [ ] **Step 1: 加入依賴**

Run: `uv add "pypdfium2>=5.0" && uv add --dev "pypdf>=6.0"`
Expected: `pyproject.toml` 的 dependencies 和 dev group 都更新，`uv.lock` 也更新。

- [ ] **Step 2: 在 `tests/samples.py` 加入建立 PDF 的函式**

在 import 加上 `import ctypes`、`import pypdfium2 as pdfium`、`import pypdfium2.raw as pdfium_c`、`from pypdf import PdfWriter`。

```python
def pdf(path: Path, *texts: str, size=(595, 842)) -> Path:
    """A PDF with a page per text, in Helvetica; an empty text makes a page with no text at all, as a scan has."""
    document = pdfium.PdfDocument.new()
    for text in texts:
        page = document.new_page(*size)
        if text:
            font = pdfium_c.FPDFText_LoadStandardFont(document.raw, b"Helvetica")
            line = pdfium_c.FPDFPageObj_CreateTextObj(document.raw, font, 24)
            pdfium_c.FPDFText_SetText(line, (ctypes.c_ushort * (len(text) + 1))(*map(ord, text), 0))
            pdfium_c.FPDFPageObj_Transform(line, 1, 0, 0, 1, 36, size[1] - 60)
            pdfium_c.FPDFPage_InsertObject(page.raw, line)
            page.gen_content()
    with open(path, "wb") as file:
        document.save(file)
    return path


def locked_pdf(path: Path) -> Path:
    """A PDF that opens only with a password."""
    writer = PdfWriter()
    writer.add_blank_page(200, 200)
    writer.encrypt("secret", algorithm="RC4-128")
    with open(path, "wb") as file:
        writer.write(file)
    return path
```

- [ ] **Step 3: 寫失敗的測試，加在 `tests/test_documents.py`**

import 加上 `from kilo import images`，並把 `locked_pdf`、`pdf` 併進 `from samples import …` 那一行。

```python
TOO_MANY = "At most 5 pages can be shown at once; ask for the others in another call."
BAD = 'pages must be page numbers or ranges, like "3", "3-5" or "1,4".'


def test_a_pdf_has_a_section_per_page(tmp_path):
    assert text_of(pdf(tmp_path / "doc.pdf", "Hello page one", "")) == "\n".join([
        "## Page 1", "", "Hello page one", "",
        "## Page 2", "", '[No text on this page: it may be a scan or a picture. To see it, read it as an image with pages="2".]', "",
    ])


def test_a_password_protected_pdf_is_refused(tmp_path):
    with pytest.raises(documents.DocumentError) as error:
        documents.convert(locked_pdf(tmp_path / "locked.pdf"))
    assert str(error.value) == "is password-protected, so it cannot be read."


@pytest.mark.parametrize("spec,expected", [
    ("3", [3]), ("2-4", [2, 3, 4]), ("1, 4", [1, 4]), ("1-2,7", [1, 2, 7]), ("4,4,2", [4, 2]), (" 5 ", [5]),
])
def test_page_numbers(spec, expected):
    assert documents.page_numbers(spec, 10) == expected


@pytest.mark.parametrize("spec,message", [
    ("", BAD), ("three", BAD), ("5-3", BAD), ("1;2", BAD),
    ("0", "Page 0 is out of range: the PDF has 10 pages."),
    ("9-11", "Page 11 is out of range: the PDF has 10 pages."),
    ("1-6", TOO_MANY),
])
def test_page_numbers_that_cannot_be_shown(spec, message):
    with pytest.raises(documents.PagesError) as error:
        documents.page_numbers(spec, 10)
    assert str(error.value) == message


def test_pages_are_shown_as_images_2048_on_their_long_side(tmp_path):
    total, shown = documents.show_pages(pdf(tmp_path / "doc.pdf", "one", "two", "three"), "3,1")
    assert total == 3 and [number for number, _ in shown] == [3, 1]
    assert all((stored.width, stored.height) == (1448, 2048) for _, stored in shown)
    assert all((images.root() / stored.file).exists() for _, stored in shown)


def test_a_small_page_is_rendered_at_300_dpi_at_most(tmp_path):
    _, [(_, stored)] = documents.show_pages(pdf(tmp_path / "card.pdf", "card", size=(144, 72)), "1")
    assert (stored.width, stored.height) == (600, 300)


def test_pages_of_a_password_protected_pdf_are_refused(tmp_path):
    with pytest.raises(documents.DocumentError):
        documents.show_pages(locked_pdf(tmp_path / "locked.pdf"), "1")
```

- [ ] **Step 4: 執行，確認失敗**

Run: `uv run pytest tests/test_documents.py -q`
Expected: 新的測試失敗，`AttributeError: module 'kilo.documents' has no attribute 'page_numbers'`（以及 `.pdf` 還不認得）；之前的 20 個通過。

- [ ] **Step 5: 實作**

- **`_open_pdf(path) -> pdfium.PdfDocument`：** `pdfium.PdfDocument(str(path))`。遇到 `pdfium.PdfiumError`，訊息裡（不分大小寫）有 `password` 的話，改丟 `DocumentError(PASSWORD_PROTECTED)`，並加 `from None`；其他錯誤照原樣丟出。
- **`_pdf_lines(path)`：** 逐頁處理，頁面和 textpage 用完都要 `close()`，最後關閉文件。每一頁：
  1. 用 `get_textpage().get_text_bounded()` 取文字。
  2. `\r\n`、`\r` 換成 `\n`，每行去掉結尾空白，再去掉開頭和結尾的空行。
  3. 輸出 `## Page {n}` 和 `""`，接著輸出文字的每一行；沒有文字就輸出 `NO_TEXT.format(page=n)`。最後輸出 `""`。
- **`page_numbers(spec, total)`：** 依序檢查，每個錯誤都丟 `PagesError`：
  1. 去掉所有空白，用逗號切開。
  2. 每一段都要符合 `^(\d+)(?:-(\d+))?$`，否則是 `BAD_PAGES`；範圍的起點大於終點也是 `BAD_PAGES`。
  3. 逐頁檢查：不在 `1..total` 之內是 `OUT_OF_RANGE`。
  4. 加入時跳過重複的頁，保持給的順序；加入後超過 `MAX_PAGES_SHOWN` 頁就是 `TOO_MANY_PAGES`。
- **`show_pages(path, spec)`：** 用 `_open_pdf` 打開，算出 `page_numbers(spec, len(document))`。每一頁：
  - `width, height = page.get_size()`
  - `scale = min(images.MAX_SIDE / max(width, height), PAGE_DPI_LIMIT / 72)`
  - 在頁面關閉之前就呼叫 `images.store(page.render(scale=scale).to_pil())`，避免 PIL 圖還在共用 bitmap 的記憶體時頁面已經關掉。

  回傳 `(總頁數, [(頁碼, Stored), …])`。

- [ ] **Step 6: 執行，確認通過**

Run: `uv run pytest tests/test_documents.py -q`
Expected: `38 passed`

- [ ] **Step 7: 整套測試**

Run: `uv run pytest -q`
Expected: 全部通過，`6 deselected`。

- [ ] **Step 8: Commit**

```bash
git add kilo/documents.py tests/samples.py tests/test_documents.py pyproject.toml uv.lock
git commit -m "feat: convert PDFs to text and render their pages as images

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: `read_file` 讀圖片與文件

**Files:**
- Modify: `kilo/tools/file_tools.py`、`tests/test_file_tools.py`、`tests/test_runner.py`

**Interfaces:**
- Consumes:
  - `images.SUFFIXES`、`images.read`（Task 1）
  - `ShownImage`、`ToolResult`（Task 3）
  - `documents.kind_of`、`convert`、`show_pages`、`DocumentError`、`PagesError`、`Kind.name`、`Kind.described`（Tasks 4–7）
- Produces:
  - `execute_read_file_tool(args) -> ToolResult`：一律回傳 `ToolResult`，只有圖片和 PDF 頁面會附圖。
  - `READ_FILE_TOOL_DEFINITION`：description 改成 spec §3.1 的全文，並新增 `pages` 參數，型別 string，說明照 spec §3.1。
  - 原文（spec §3.1–§3.6）：
    - `HEIC_MESSAGE = "HEIC images are not supported. Ask for a JPEG or PNG instead."`
    - `OLD_OFFICE_MESSAGE = "Old Office formats (.doc, .xls, .ppt) are not supported. Ask for .docx, .xlsx or .pptx instead, or convert the file with LibreOffice (soffice) if it is installed."`
    - `BINARY_MESSAGE = "File '{path}' appears to be binary ({size} bytes). read_file reads text files, images (PNG, JPEG, WebP, GIF, BMP, TIFF), PDF, Word (.docx), Excel (.xlsx) and PowerPoint (.pptx) files."`
    - `PAGES_PDF_ONLY = "pages applies to PDF files only."`
    - `SHOWN_IMAGE_MESSAGE = "The image is shown in the message after the tool results."`
    - `SHOWN_FRAMES_MESSAGE = "The first of its {frames} frames is shown in the message after the tool results."`
    - `SHOWN_PAGES_MESSAGE = "The pages are shown as images in the message after the tool results."`

- [ ] **Step 1: 改測試的 helper，並寫失敗的測試（`tests/test_file_tools.py`）**

import 加上：
- `import docx`
- `from PIL import Image`
- `import samples`
- `from kilo import documents, images`
- `READ_FILE_TOOL`：併進原本 `from kilo.tools.file_tools import …` 那一行

把 `read` helper 改成：

```python
async def read(**args):
    return json.loads((await execute_read_file_tool(args)).text)
```

新增：

```python
OLD = (
    "Old Office formats (.doc, .xls, .ppt) are not supported. Ask for .docx, .xlsx or .pptx instead, "
    "or convert the file with LibreOffice (soffice) if it is installed."
)
TOO_MANY = "At most 5 pages can be shown at once; ask for the others in another call."
BAD_PAGES = 'pages must be page numbers or ranges, like "3", "3-5" or "1,4".'


def test_read_file_says_what_it_reads():
    definition = READ_FILE_TOOL.definition
    assert definition["description"] == (
        "Read a file. Text comes as numbered lines: each line with its number and ' | ' in front (e.g. '   12 | text'), "
        "which are not part of the file. Lines longer than 2,000 characters are cut, with a marker, and a long read "
        "stops early with `truncated` and a `message` saying where to continue. Bytes that are not UTF-8 show as "
        "replacement characters. PDF, Word (.docx), Excel (.xlsx) and PowerPoint (.pptx) files are converted to "
        "Markdown text, numbered and read in parts the same way; the result's `text_file` holds the whole text, for "
        "grep. Editing that text does not change the original file. Images (PNG, JPEG, WebP, GIF, BMP, TIFF) are shown "
        "to you in a message after the tool results, as are PDF pages asked for with `pages`. Other binary files are "
        "refused."
    )
    assert definition["parameters"]["properties"]["pages"] == {"type": "string", "description": (
        'PDF only: pages to look at as images rather than read as text, e.g. "3", "3-5" or "1,4"; at most 5. '
        "For scanned pages, and for charts and layouts the text leaves out."
    )}


async def test_an_image_is_shown_to_the_model(workspace, data_dir):
    Image.new("RGB", (4096, 1024), "blue").save(workspace / "wide.png")
    result = await execute_read_file_tool({"path": "wide.png"})
    assert json.loads(result.text) == {
        "status": "success", "path": "wide.png", "type": "image/png", "width": 4096, "height": 1024,
        "shown_width": 2048, "shown_height": 512, "message": "The image is shown in the message after the tool results.",
    }
    [shown] = result.images
    assert shown.label == "wide.png" and (images.root() / shown.file).exists()


async def test_an_animation_says_which_frame_is_shown(workspace, data_dir):
    frames = [Image.new("RGB", (8, 8), color) for color in ("red", "blue")]
    frames[0].save(workspace / "moving.gif", save_all=True, append_images=frames[1:])
    result = await read(path="moving.gif")
    assert result["frames"] == 2
    assert result["message"] == "The first of its 2 frames is shown in the message after the tool results."


async def test_extensions_are_matched_in_any_case(workspace, data_dir):
    Image.new("RGB", (8, 8)).save(workspace / "PHOTO.JPG", format="JPEG")
    assert (await read(path="PHOTO.JPG"))["type"] == "image/jpeg"
    samples.word(workspace / "NOTES.DOCX", lambda document: document.add_paragraph("upper"))
    assert (await read(path="NOTES.DOCX"))["converted_from"] == "docx"
    samples.pdf(workspace / "SCAN.PDF", "one")
    assert (await read(path="SCAN.PDF", pages="1"))["pages"] == [1]


async def test_a_broken_image_is_an_error(workspace, data_dir):
    (workspace / "broken.png").write_bytes(b"not an image")
    result = await read(path="broken.png")
    assert result["status"] == "error" and result["message"].startswith("Could not read 'broken.png' as an image: ")


async def test_a_document_is_read_as_numbered_text(workspace, data_dir):
    def build(document):
        document.add_heading("Title", 1)
        document.add_paragraph("Body")

    samples.word(workspace / "report.docx", build)
    result = await read(path="report.docx")
    assert result == {
        "status": "success", "path": "report.docx", "converted_from": "docx",
        "text_file": str(documents.convert(workspace / "report.docx")),
        "total_lines": 3, "start_line": 1, "end_line": 3, "lines_returned": 3, "truncated": False,
        "content": "    1 | # Title\n    2 | \n    3 | Body",
    }


async def test_a_long_document_is_read_in_parts(workspace, data_dir):
    def build(document):
        for i in range(10):
            document.add_paragraph(f"paragraph {i}")

    samples.word(workspace / "long.docx", build)
    first = await read(path="long.docx", max_lines=4)
    assert first["truncated"] is True and "start_line=5" in first["message"]
    assert (await read(path="long.docx", start_line=5, max_lines=1))["content"] == "    5 | paragraph 2"


@pytest.mark.parametrize("name,message", [
    ("photo.heic", "HEIC images are not supported. Ask for a JPEG or PNG instead."),
    ("old.doc", OLD), ("old.xls", OLD), ("old.ppt", OLD),
])
async def test_formats_not_supported_are_named(workspace, name, message):
    (workspace / name).write_bytes(b"\0binary")
    assert await read(path=name) == {"status": "error", "message": message}


async def test_other_binary_files_list_what_can_be_read(workspace):
    (workspace / "blob.bin").write_bytes(bytes(10))
    assert (await read(path="blob.bin"))["message"] == (
        "File 'blob.bin' appears to be binary (10 bytes). read_file reads text files, images (PNG, JPEG, WebP, GIF, "
        "BMP, TIFF), PDF, Word (.docx), Excel (.xlsx) and PowerPoint (.pptx) files."
    )


async def test_a_corrupt_document_is_an_error(workspace, data_dir):
    (workspace / "bad.docx").write_text("not a zip")
    assert (await read(path="bad.docx"))["message"].startswith("Could not read 'bad.docx' as a Word document: ")


async def test_a_locked_document_says_so(workspace, data_dir):
    (workspace / "locked.xlsx").write_bytes(bytes.fromhex("D0CF11E0A1B11AE1") + bytes(504))
    assert (await read(path="locked.xlsx"))["message"] == (
        "'locked.xlsx' is password-protected or in an old Office format, so it cannot be read."
    )


async def test_pdf_pages_are_shown_as_images(workspace, data_dir):
    samples.pdf(workspace / "scan.pdf", "one", "", "three")
    result = await execute_read_file_tool({"path": "scan.pdf", "pages": "2-3"})
    assert json.loads(result.text) == {
        "status": "success", "path": "scan.pdf", "total_pages": 3, "pages": [2, 3],
        "message": "The pages are shown as images in the message after the tool results.",
    }
    assert [shown.label for shown in result.images] == ["scan.pdf, page 2", "scan.pdf, page 3"]


async def test_pages_may_be_a_number(workspace, data_dir):
    samples.pdf(workspace / "scan.pdf", "one")
    assert (await read(path="scan.pdf", pages=1))["pages"] == [1]


@pytest.mark.parametrize("pages,message", [
    ("1-6", TOO_MANY), ("7", "Page 7 is out of range: the PDF has 6 pages."), ("x", BAD_PAGES),
])
async def test_pages_that_cannot_be_shown(workspace, data_dir, pages, message):
    samples.pdf(workspace / "six.pdf", *[f"page {i}" for i in range(1, 7)])
    assert await read(path="six.pdf", pages=pages) == {"status": "error", "message": message}


async def test_pages_apply_to_pdf_files_only(workspace):
    (workspace / "notes.txt").write_text("hi")
    assert await read(path="notes.txt", pages="1") == {"status": "error", "message": "pages applies to PDF files only."}


async def test_empty_pages_read_the_text(workspace, data_dir):
    samples.pdf(workspace / "doc.pdf", "one")
    assert (await read(path="doc.pdf", pages=""))["converted_from"] == "pdf"


async def test_a_password_protected_pdf_says_so(workspace, data_dir):
    samples.locked_pdf(workspace / "locked.pdf")
    assert (await read(path="locked.pdf"))["message"] == "'locked.pdf' is password-protected, so it cannot be read."
```

- [ ] **Step 2: 寫失敗的整合測試，加在 `tests/test_runner.py`**

```python
async def test_read_file_shows_an_image_to_the_model(kilo, model, session, workspace, data_dir):
    Image.new("RGB", (64, 32), "red").save(workspace / "red.png")
    model.turns.extend([tool_turn("c1", "read_file", {"path": "red.png"}), text_turn("red")])
    await collect(kilo, session.id, "what colour is red.png?")
    last = model.requests[1][-1]
    assert last["role"] == "user" and last["content"][0] == {"type": "text", "text": "[Image: red.png]"}
    assert last["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
```

- [ ] **Step 3: 執行，確認失敗**

Run: `uv run pytest tests/test_file_tools.py tests/test_runner.py -q`
Expected: 失敗。原本的 `read` helper 會遇到 `AttributeError: 'str' object has no attribute 'text'`，新測試則因為還不支援這些格式而失敗。

- [ ] **Step 4: 實作 `kilo/tools/file_tools.py`**

- **定義：** description 和 `pages` 參數照 Step 1 的測試原文。
- **抽出共用的分頁：** 從現在的 `_read_file` 把加行號、分頁那一段抽成 `_numbered(text: str, args: Dict[str, Any], head: Dict[str, Any]) -> Dict[str, Any]`。
  - 從 `splitlines`、`start_line`、`end_line`、`max_lines` 到 `READ_FILE_ROOM` 的預算，以及「超出範圍」的回覆，都照原本的程式。
  - `head` 是結果開頭的欄位：文字檔是 `status`、`path`；文件另外加 `converted_from`、`text_file`。
- **`_read_file(args) -> ToolResult` 的流程：**
  1. 缺參數、找不到檔案、不是一般檔案：照原本的錯誤。
  2. `pages = "" if args.get("pages") is None else str(args["pages"]).strip()`
  3. `kind = documents.kind_of(file_path)`。`pages` 不是空的，而且檔案不是 PDF（`kind is None or kind.name != "pdf"`）：回 `PAGES_PDF_ONLY`。
  4. 依副檔名（`suffix.lower()`）處理：
     - **在 `images.SUFFIXES` 裡：** 呼叫 `images.read`。
       - 結果欄位：`status`、`path`、`type`、`width`、`height`、`shown_width`、`shown_height`、`message`。有多格時加 `frames`，`message` 改用 `SHOWN_FRAMES_MESSAGE`。
       - 附圖：`(ShownImage(info.shown.file, 顯示路徑),)`。
       - 出錯：`Could not read '{path_raw}' as an image: {e}`。
     - **`.heic`、`.heif`：** 回 `HEIC_MESSAGE`。
     - **`.doc`、`.xls`、`.ppt`：** 回 `OLD_OFFICE_MESSAGE`。
     - **`kind` 不是 None，而且有 `pages`：** 呼叫 `documents.show_pages`。
       - 結果欄位：`status`、`path`、`total_pages`、`pages`、`message`（`SHOWN_PAGES_MESSAGE`）。
       - 附圖：每頁一個 `ShownImage(stored.file, f"{顯示路徑}, page {n}")`。
     - **`kind` 不是 None，沒有 `pages`：** 呼叫 `documents.convert`，再交給 `_numbered(text, args, head)`。`head` 的 `text_file` 用 `workspace.display(轉好的路徑)`。
  5. 文件出錯時：
     - `DocumentError` 回 `f"'{path_raw}' {e}"`。
     - `PagesError` 回 `str(e)`。
     - 其他例外回 `f"Could not read '{path_raw}' as {kind.described}: {e}"`。
  6. 其他檔案照原本的文字流程；二進位檔改用 `BINARY_MESSAGE`。
  7. 所有錯誤都是 `ToolResult(text=json.dumps({"status": "error", "message": ...}))`。
- **`execute_read_file_tool`：** 仍然用 `asyncio.to_thread` 執行，回傳型別改成 `ToolResult`。

- [ ] **Step 5: 執行，確認通過**

Run: `uv run pytest tests/test_file_tools.py tests/test_runner.py -q`
Expected: 全部通過。

- [ ] **Step 6: 整套測試**

Run: `uv run pytest -q`
Expected: 全部通過，`6 deselected`。

- [ ] **Step 7: Commit**

```bash
git add kilo/tools/file_tools.py tests/test_file_tools.py tests/test_runner.py
git commit -m "feat: read images, PDF and Office files with read_file

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: 附件說明、README、真實模型驗證

**Files:**
- Modify: `kilo/uploads.py`、`tests/test_uploads.py`、`README.md`、`tests/live/test_live.py`

**Interfaces:**
- Consumes: Task 8 的 `read_file`；`real_server` fixture（`tests/live/test_live.py`）
- Produces:
  - `uploads.NOTE_HEADER` 改成：`"[Files sent with this message, saved in the workspace. Their contents are not included here; read them with read_file when you need them."`

- [ ] **Step 1: 改測試 `tests/test_uploads.py::test_note_lists_each_file`**

預期的第一行改成：

```python
        "[Files sent with this message, saved in the workspace. Their contents are not included here; "
        "read them with read_file when you need them.\n"
```

- [ ] **Step 2: 執行，確認失敗**

Run: `uv run pytest tests/test_uploads.py -q`
Expected: `test_note_lists_each_file` 失敗，因為說明文字還是舊的；其他測試通過。

- [ ] **Step 3: 改 `NOTE_HEADER`，執行，確認通過**

Run: `uv run pytest tests/test_uploads.py -q`
Expected: 全部通過。

- [ ] **Step 4: 改 README**（spec §5）

- **Features 的 Files 那一行：** 在 `…and the agent is told where it is, not given its contents.` 後面加一句：`The agent reads it with \`read_file\`, which shows images to the model and turns PDF, Word, Excel and PowerPoint files into text.`
- **Files 一節：** 把 `For now \`read_file\` reads text files only.` 換成：

  ```
  `read_file` reads text files; images (PNG, JPEG, WebP, GIF, BMP, TIFF), which the model is shown; and PDF, Word (.docx), Excel (.xlsx) and PowerPoint (.pptx) files, as text. A scanned PDF page can be shown as an image instead. The model has to take images to see them (the default model does); otherwise the agent is told it cannot.
  ```
- **最後一段之後加一段：**

  ```
  To show an image, Kilo keeps a copy at most 2048 pixels on its long side in `images/` in the data directory (`KILO_DATA_DIR`), and the text of each document it reads in `converted/`; copies unused for 30 days are deleted. Only the 10 most recent images stay in the conversation.
  ```

- [ ] **Step 5: 寫 live 測試，加在 `tests/live/test_live.py` 最後**

import 加上 `import io` 和 `from PIL import Image, ImageDraw, ImageFont`。

```python
def test_real_image_is_seen(real_server):
    """The number is only drawn in the image, so the model must look at it with read_file to answer."""
    number = str(secrets.randbelow(9000) + 1000)
    picture = Image.new("RGB", (900, 320), "white")
    ImageDraw.Draw(picture).text((60, 60), number, fill="black", font=ImageFont.load_default(size=180))
    buffer = io.BytesIO()
    picture.save(buffer, "PNG")
    with httpx.Client(base_url=real_server, timeout=180) as http:
        login = http.post("/api/v1/auth/login", json={"username": DEFAULT_USERNAME, "password": DEFAULT_PASSWORD})
        assert login.status_code == 200
        attachment = http.post(
            "/api/v1/uploads", params={"name": "number.png"}, content=buffer.getvalue(),
            headers={"content-type": "image/png"},
        ).json()
        session = http.post("/api/v1/sessions").json()
        reply = http.post(f"/api/v1/sessions/{session['id']}/chat", json={
            "message": "What number is written in the image I sent? Reply with the number only.",
            "attachments": [attachment], "stream": False,
        }).json()
    calls = {event["data"]["name"] for event in reply["events"] if event["event"] == "tool_call_start"}
    assert "read_file" in calls, reply["events"]
    assert number in reply["response"]
```

- [ ] **Step 6: 執行 live 測試**

Run: `uv run pytest -m live -q`
Expected: 全部通過，包括 `test_real_image_is_seen`。`.env` 的 key 過期時會回 401，要先換新的 key。

- [ ] **Step 7: 手動檢查**

用 `uv run kilo-server` 啟動，需要的話用 `KILO_PORT` 換一個沒被占用的埠。在網頁終端：
1. 用 `/upload` 傳一張照片（手機照片，或 Pillow 畫的、帶 EXIF 轉向的 JPEG），問圖裡有什麼。agent 要呼叫 `read_file`，答案要正確。
2. 傳一個 docx，問裡面的一個細節。答案要正確，`read_file` 的結果要有 `converted_from: docx`。

- [ ] **Step 8: 整套測試**

Run: `uv run pytest -q`
Expected: 全部通過，`6 deselected`。

- [ ] **Step 9: Commit**

```bash
git add kilo/uploads.py tests/test_uploads.py README.md tests/live/test_live.py
git commit -m "docs: explain reading images and documents; verify a real model sees an image

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
