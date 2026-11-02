"""
The chat reader: LINE chats as Kilo sees them. It decides which chats Kilo may touch (the served-chat
gate: the account lists' `chats`, every chat when empty), and who wrote each message. The account is used
by Kilo and by the people who run it, so its messages are Kilo's when the outbox sent them and the staff's
otherwise; a tester's may pose as a contact's, and other senders get the role the account lists give their
display name (manager, colleague, bot). Reads the page through the page adapter (LineBrowser, or a fake in
tests) and Kilo's sent messages from the LINE state.
"""

from typing import List, Protocol

from kilo.line.config import settings
from kilo.line.accounts import LineAccounts
from kilo.line.browser import LineError, same_text
from kilo.line.state import LineState
from kilo.models.line import ChatMessage, ChatRead, ListedChat, OpenedChat, PageMessage, Sender, SentMessage


class LinePage(Protocol):
    """What the chat reader and the outbox need of the LINE page (LineBrowser in production)."""

    async def chats(self) -> List[ListedChat]: ...
    async def read(self, name: str, limit: int = 20) -> OpenedChat: ...
    async def send(self, name: str, text: str) -> SentMessage: ...


class NotServed(LineError):
    """Kilo may not read or write in this chat."""


class ChatReader:
    def __init__(self, page: LinePage, state: LineState, accounts: LineAccounts):
        self.page, self.state, self.accounts = page, state, accounts

    async def chats(self) -> List[ListedChat]:
        """The served chats in the chat list, most recent first. Marks nothing as read."""
        accounts = self.accounts.current()
        return [c for c in await self.page.chats() if accounts.serves(c.name)]

    async def gate(self, name: str) -> None:
        """Raises NotServed unless Kilo may read and write in the chat; LineAccountsError while the lists are unusable."""
        if not self.accounts.current().serves(name):
            raise NotServed(f"Kilo may not use the chat '{name}'. `chats` lists the chats it may use.")

    async def read(self, name: str, limit: int = 20) -> ChatRead:
        """
        A served chat's latest `limit` messages, oldest first, each with its sender. Opening the chat marks
        its messages as read for the people who sent them.
        """
        await self.gate(name)
        opened = await self.page.read(name, limit)
        return ChatRead(chat=name, messages=await self._senders(name, opened.messages))

    async def _senders(self, chat: str, messages: List[PageMessage]) -> List[ChatMessage]:
        accounts = self.accounts.current()
        mine = await self.state.sent_to(chat)
        ids = {d.message_id for d in mine if d.message_id}
        prefix = settings.kilo_line_test_prefix
        resolved = []
        for m in messages:
            name = None
            if m.own and prefix and m.text.startswith(prefix):
                sender = Sender.TEST_CONTACT
            elif m.own:
                sender = Sender.KILO if m.id in ids or any(same_text(m.text, d.text) for d in mine) else Sender.ACCOUNT
            else:
                name = m.sender or "?"
                sender = accounts.sender_role(m.sender) or Sender.PERSON
            resolved.append(ChatMessage(
                id=m.id, at=m.at, sender=sender, name=name, kind=m.kind, text=m.text,
                reply_to=m.reply_to, read_by=m.read_by,
            ))
        return resolved
