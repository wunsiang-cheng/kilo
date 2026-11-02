"""
LINE through its Chrome extension: Kilo drives the extension's page in the dedicated Chrome
(started by `kilo-line-browser`) over the Chrome DevTools Protocol, reading the page's text
rather than screenshots. Everything here depends on the extension's page structure, kept in SEL.
This is the page adapter behind the chat reader and the outbox: it returns what the page shows
(ListedChat, OpenedChat, SentMessage), and leaves deciding who wrote what to the chat reader.
"""

import asyncio
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from kilo.line.config import settings
from kilo.models.line import ListedChat, OpenedChat, PageMessage, SentMessage

EXTENSION_ID = "ophjlpahpchlmihnnnihgmmeilfjmjjc"
PAGE_PREFIX = f"chrome-extension://{EXTENSION_ID}/"

# The extension's page structure (LINE 3.7.2). Its CSS-module class names end in a hash that changes
# between builds, so they are matched by prefix.
SEL = {
    "nav_chats": 'button[aria-label="Chat"]',
    "nav_friends": 'button[aria-label="Friend"]',
    "login_form": 'input[type="email"]',
    "chat_item": '[class*="chatlistItem-module__chatlist_item"]',
    "friend_item": '[class*="friendlistItem-module__item"]',
    "friend_search": '[class*="friendlist-module__list_wrap"] input[class*="searchInput-module__input"]',
    "open_chat": 'button[aria-label="Go chatroom"]',
    "room": '[class*="chatroom-module__chatroom"]',
    "room_name": '[class*="chatroomHeader-module__name"]',
    "message_list": '[class*="chatroom-module__chatroom"] [role="log"]',
    "editor": "textarea-ex",
    "editor_input": "textarea-ex textarea",
}

# Reads the chat list. Titles keep only the name: member counts are separate elements. Emoji are images:
# their alt text stands in for them.
_CHATS_JS = """(sel) => [...document.querySelectorAll(sel.chat_item)].map(item => {
  const text = (part) => {
    const node = item.querySelector(`[class*="chatlistItem-module__${part}"]`);
    if (!node) return '';
    const copy = node.cloneNode(true);
    copy.querySelectorAll('img[alt]').forEach(img => img.replaceWith(img.alt));
    return copy.textContent.trim();
  };
  return {
    name: item.querySelector('[class*="chatlistItem-module__title_box"] pre')?.textContent.trim() ?? '',
    mid: item.getAttribute('data-mid') || '',
    members: Number((text('member_count').match(/\\d+/) || [0])[0]) || null,
    unread: Number(text('message_count')) || 0,
    last_message: text('description'),
    last_time: text('date'),
  };
})"""

# Reads the messages on screen, newest first (the list is laid out bottom-up). Emoji are images:
# their alt text stands in for them.
_MESSAGES_JS = """(sel) => {
  const textOf = (node) => {
    if (!node) return '';
    const copy = node.cloneNode(true);
    copy.querySelectorAll('img[alt]').forEach(img => img.replaceWith(img.alt));
    return copy.textContent.trim();
  };
  const list = document.querySelector(sel.message_list);
  if (!list) return [];
  return [...list.children].filter(m => m.querySelector('[data-message-id]')).map(m => {
    const wrap = m.querySelector('[class*="MessageContent-module__content_wrap"]');
    const kind = (wrap?.className.match(/(\\w+)MessageContent-module__content_wrap/) || [])[1] || 'other';
    const quoted = m.querySelector('[class*="replyMessageContent-module__message"]');
    const reply = m.querySelector('[class*="replyMessageContent-module__reply_content"]');
    const time = m.querySelector('time[datetime]')?.getAttribute('datetime');
    return {
      id: m.querySelector('[data-message-id]').getAttribute('data-message-id'),
      sender_id: m.getAttribute('data-mid') || '',
      sender: m.querySelector('[class*="username-module__username"]')?.textContent.trim() || null,
      time: time ? new Date(time).toISOString() : null,
      kind: kind === 'reply' ? 'text' : kind,
      text: textOf(reply || m.querySelector('[class*="textMessageContent-module__text"]') || wrap),
      reply_to: quoted ? textOf(quoted) : null,
      read_by: Number((m.querySelector('[class*="metaInfo-module__read_count"]')?.textContent.match(/\\d+/) || [0])[0]),
    };
  });
}"""

# The logged-in account's id, from the extension's storage key `lcs_plain_<id>`.
_OWN_ID_JS = """() => (Object.keys(localStorage).find(k => /^lcs_plain_[A-Za-z0-9_-]{20,}$/.test(k)) || '').slice(10)"""

# Scrolls the message list up to its oldest loaded message, which makes the page load older ones.
_SCROLL_UP_JS = """(sel) => { const l = document.querySelector(sel.message_list); if (l) l.scrollTop = -l.scrollHeight; }"""


class LineError(Exception):
    """A LINE request that could not be carried out; the message says why."""


class LineUnconfirmed(LineError):
    """A message was sent but could not be seen arriving in the chat: it must not be sent again unchecked."""


def squash(text: str) -> str:
    """Text without whitespace and emoji variation selectors, which the page may change."""
    return re.sub(r"[\s\ufe0f]+", "", text)


def same_text(a: str, b: str) -> bool:
    return squash(a) == squash(b)


def _kind(mid: str) -> str:
    """A chat's kind from the first letter of its LINE id."""
    return {"C": "group", "R": "room", "U": "person"}.get(mid[:1], "other")


def _moment(iso: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")) if iso else None


def _listed(raw: Dict[str, Any]) -> ListedChat:
    return ListedChat(
        name=raw["name"], kind=_kind(raw["mid"]), members=raw["members"], unread=raw["unread"],
        last_message=raw["last_message"], last_time=raw["last_time"],
    )


class LineBrowser:
    """
    The LINE page in the dedicated Chrome. One operation at a time: the page has one chat open,
    so reading and sending take turns. Connects on first use and again after Chrome restarts.
    """

    def __init__(self, cdp_url: Optional[str] = None):
        self.cdp_url = cdp_url or settings.kilo_line_cdp_url
        self._lock = asyncio.Lock()
        self._playwright = None
        self._browser = None

    async def _page(self):
        from playwright.async_api import async_playwright

        if self._browser is None or not self._browser.is_connected():
            if self._playwright is None:
                self._playwright = await async_playwright().start()
            try:
                self._browser = await self._playwright.chromium.connect_over_cdp(self.cdp_url, timeout=10_000)
            except Exception as e:
                raise LineError(
                    f"The LINE browser is not running at {self.cdp_url}: start it with `kilo-line-browser` ({e})."
                )
        pages = [p for context in self._browser.contexts for p in context.pages if p.url.startswith(PAGE_PREFIX)]
        if not pages:
            context = self._browser.contexts[0]
            page = await context.new_page()
            await page.goto(f"{PAGE_PREFIX}index.html")
            pages = [page]
        page = pages[0]
        if await page.locator(SEL["login_form"]).count():
            raise LineError("LINE is logged out: someone must log in again in the LINE browser window.")
        if not await page.locator(SEL["nav_chats"]).count():
            raise LineError("The LINE page is not ready or its layout is not recognized (was the extension updated?).")
        return page

    async def close(self) -> None:
        """Disconnect (the LINE browser keeps running)."""
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()
        self._browser = self._playwright = None

    async def chats(self) -> List[ListedChat]:
        """The chat list as LINE shows it, most recent first. Reading it marks nothing as read."""
        async with self._lock:
            page = await self._page()
            await page.click(SEL["nav_chats"])
            await page.wait_for_timeout(500)
            return [_listed(raw) for raw in await page.evaluate(_CHATS_JS, SEL)]

    async def _open(self, page, name: str) -> str:
        """Open the chat called `name` (exact) and return its id."""
        await page.click(SEL["nav_chats"])
        await page.wait_for_timeout(300)
        names = await page.evaluate(_CHATS_JS, SEL)
        matches = [i for i, chat in enumerate(names) if chat["name"] == name]
        if len(matches) > 1:
            raise LineError(f"More than one chat is called '{name}'.")
        if matches:
            await page.locator(SEL["chat_item"]).nth(matches[0]).locator(SEL["open_chat"]).click()
        else:
            # Not in the recent chats: look it up among friends and groups.
            await page.click(SEL["nav_friends"])
            search = page.locator(SEL["friend_search"])
            await search.fill(name)
            await page.wait_for_timeout(800)
            items = page.locator(SEL["friend_item"])
            found = [i for i in range(await items.count())
                     if (await items.nth(i).locator("pre").first.inner_text()).strip() == name]
            if len(found) != 1:
                await search.fill("")
                if found:
                    raise LineError(f"More than one friend or group is called '{name}'.")
                shown = [chat["name"] for chat in names]
                raise LineError(f"No chat, friend or group called '{name}'. Recent chats: {shown}.")
            await items.nth(found[0]).locator(SEL["open_chat"]).click()
            await search.fill("")
        for _ in range(50):
            header = page.locator(SEL["room_name"])
            if await header.count() and (await header.first.inner_text()).strip() == name:
                await page.wait_for_timeout(500)
                return await page.locator(SEL["room"]).first.get_attribute("data-mid") or ""
            await page.wait_for_timeout(100)
        raise LineError(f"The chat '{name}' did not open.")

    async def read(self, name: str, limit: int = 20) -> OpenedChat:
        """
        The latest `limit` messages of one chat, oldest first. Opening the chat marks its messages
        as read for the people who sent them.
        """
        async with self._lock:
            page = await self._page()
            chat_id = await self._open(page, name)
            own_id = await page.evaluate(_OWN_ID_JS)
            messages = await page.evaluate(_MESSAGES_JS, SEL)
            while len(messages) < limit:
                await page.evaluate(_SCROLL_UP_JS, SEL)
                await page.wait_for_timeout(1200)
                more = await page.evaluate(_MESSAGES_JS, SEL)
                if len(more) <= len(messages):
                    break
                messages = more
        latest = list(reversed(messages[:limit]))
        return OpenedChat(name=name, kind=_kind(chat_id), messages=[
            PageMessage(
                id=m["id"], at=_moment(m["time"]), own=bool(own_id) and m["sender_id"].startswith(own_id[:40]),
                sender=m["sender"], kind=m["kind"], text=m["text"], reply_to=m["reply_to"], read_by=m["read_by"],
            )
            for m in latest
        ])

    async def send(self, name: str, text: str) -> SentMessage:
        """
        Send `text` to the chat called `name`, then confirm it appears there as a new message from
        this account. Refuses when someone is typing in the message box.
        """
        if not text.strip():
            raise LineError("The message is empty.")
        async with self._lock:
            page = await self._page()
            await self._open(page, name)
            if await page.locator(SEL["editor"]).first.get_attribute("data-is-empty") != "true":
                raise LineError(f"The message box of '{name}' has unsent text in it; nothing was sent.")
            own_id = await page.evaluate(_OWN_ID_JS)
            before = {m["id"] for m in await page.evaluate(_MESSAGES_JS, SEL)}
            await page.locator(SEL["editor_input"]).focus()
            await page.keyboard.insert_text(text)
            if (await page.locator(SEL["room_name"]).first.inner_text()).strip() != name:  # moved while typing
                raise LineError("The chat changed while typing; the text was left unsent in the message box.")
            await page.keyboard.press("Enter")
            for _ in range(100):
                await page.wait_for_timeout(100)
                for m in await page.evaluate(_MESSAGES_JS, SEL):
                    if m["id"] not in before and m["sender_id"].startswith(own_id[:40]) and same_text(m["text"], text):
                        return SentMessage(message_id=m["id"], at=_moment(m["time"]))
        raise LineUnconfirmed(f"Sent to '{name}', but the message did not show up in the chat within 10 seconds: check it.")
