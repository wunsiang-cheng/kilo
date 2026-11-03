"""
The Telegram Bot API over HTTPS. The token is part of every request's URL, so nothing here logs a URL or an
HTTP library's error text: errors carry the API's own description, or the kind of network failure.
"""

import asyncio
import logging
from typing import Any, AsyncIterator, Optional

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://api.telegram.org"
# Times a call waits out "Too Many Requests" (as long as the API says) before giving up.
RATE_LIMIT_RETRIES = 3
# A file download: a connection within 10 seconds, then no more than a minute without data.
DOWNLOAD_TIMEOUT = httpx.Timeout(10.0, read=60.0)

# httpx logs each request's URL at INFO, and this URL holds the token.
logging.getLogger("httpx").setLevel(logging.WARNING)


class TelegramError(Exception):
    """A call failed: the API refused it, or it did not reach the API."""

    def __init__(self, description: str, code: Optional[int] = None):
        super().__init__(description)
        self.code = code


class Unauthorized(TelegramError):
    """The API rejected the token."""


class BotAPI:
    def __init__(self, token: str, client: Optional[httpx.AsyncClient] = None, base_url: str = BASE_URL):
        self._url = f"{base_url}/bot{token}/"
        self._files = f"{base_url}/file/bot{token}/"  # where a file getFile located is fetched from
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=None))

    async def call(self, method: str, http_timeout: float = 30.0, **params: Any) -> Any:
        """Call a Bot API method with these parameters (None ones left out); returns its result."""
        body = {name: value for name, value in params.items() if value is not None}
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            try:
                response = await self._client.post(self._url + method, json=body, timeout=http_timeout)
                reply = response.json()
            except httpx.HTTPError as e:
                raise TelegramError(f"{method}: {type(e).__name__}") from None
            except ValueError:
                raise TelegramError(f"{method}: HTTP {response.status_code} without a JSON reply", response.status_code) from None
            if reply.get("ok"):
                return reply.get("result")
            code, description = reply.get("error_code"), f"{method}: {reply.get('description', 'refused')}"
            retry_after = (reply.get("parameters") or {}).get("retry_after")
            if code == 429 and retry_after is not None and attempt < RATE_LIMIT_RETRIES:
                await asyncio.sleep(float(retry_after))
                continue
            raise (Unauthorized if code == 401 else TelegramError)(description, code)

    async def download(self, file_path: str) -> AsyncIterator[bytes]:
        """
        A file that getFile located, in chunks as they arrive (a bot may fetch files of up to 20 MB). Errors say the
        HTTP status or the kind of network failure, never the URL, which holds the token.
        """
        try:
            async with self._client.stream("GET", self._files + file_path, timeout=DOWNLOAD_TIMEOUT) as response:
                if response.status_code != 200:
                    raise TelegramError(f"download: HTTP {response.status_code}", response.status_code)
                async for chunk in response.aiter_bytes():
                    yield chunk
        except httpx.HTTPError as e:
            raise TelegramError(f"download: {type(e).__name__}") from None

    async def close(self) -> None:
        await self._client.aclose()
