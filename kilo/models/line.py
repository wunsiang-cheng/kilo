"""LINE models: what the LINE page shows, chats and messages as Kilo reads them, and outgoing messages."""

from datetime import datetime
from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field

from kilo.clock import utc_now



# ---------------------------------------------------------------------------
# The LINE page, as the page adapter (LineBrowser) gives it


class ListedChat(BaseModel):
    """One row of LINE's chat list. Its time is shown as LINE writes it ("3:45 PM", "Sunday")."""

    name: str
    kind: str  # "group", "room", "person" or "other"
    members: Optional[int] = None
    unread: int = 0
    last_message: str = ""
    last_time: str = ""

class PageMessage(BaseModel):
    """One message in an open chat. The account is used by Kilo and by people, so the page only knows it is `own`."""

    id: str
    at: Optional[datetime] = None
    own: bool = False  # written from the logged-in account
    sender: Optional[str] = None  # the display name, for other people's messages
    kind: str = "text"
    text: str = ""
    reply_to: Optional[str] = None
    read_by: int = 0


class OpenedChat(BaseModel):
    name: str
    kind: str  # as in ListedChat
    messages: List[PageMessage] = Field(default_factory=list)  # oldest first


class SentMessage(BaseModel):
    message_id: str
    at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Chats and messages as Kilo reads them (the chat reader)


class Sender(str, Enum):
    KILO = "kilo"  # the account, a message in the outbox
    ACCOUNT = "account"  # the account, not in the outbox: one of the people who run it
    TEST_CONTACT = "test_contact"  # the account posting as a contact (tests only)
    MANAGER = "manager"  # a display name in the account lists' managers
    COLLEAGUE = "colleague"  # a display name in the account lists' colleagues
    BOT = "bot"  # a display name in the account lists' bots: a notification bot
    PERSON = "person"  # anyone else


class ChatMessage(BaseModel):
    id: str
    at: Optional[datetime] = None
    sender: Sender
    name: Optional[str] = None  # the display name of a manager, colleague, bot or other person
    kind: str = "text"
    text: str = ""
    reply_to: Optional[str] = None
    read_by: int = 0


class ChatRead(BaseModel):
    chat: str
    messages: List[ChatMessage] = Field(default_factory=list)  # oldest first


# ---------------------------------------------------------------------------
# The outbox


class OutboxStatus(str, Enum):
    SENDING = "sending"  # a send attempt is in progress
    SENT = "sent"
    FAILED = "failed"  # sending did not work


class OutboxMessage(BaseModel):
    id: Optional[int] = None
    chat: str  # the chat's exact name in LINE
    text: str
    note: str = ""  # internal explanation of which message this answers
    status: OutboxStatus = OutboxStatus.SENDING
    error: Optional[str] = None  # why the last send failed or could not be confirmed
    message_id: Optional[str] = None  # LINE's id of the sent message, to tell Kilo's messages from the account staff's
    created_at: datetime = Field(default_factory=utc_now)
    completed_at: Optional[datetime] = None  # when the latest send attempt finished
    session_id: Optional[str] = None  # older records only: the session and run that sent it
    run_id: Optional[str] = None
