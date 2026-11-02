"""
LINE outbox: submit a message to send it immediately and record the outcome. The chat reader uses this
record to tell Kilo's messages from those of the people who also use the account.
"""

import asyncio
from typing import List, Optional

from kilo.line.browser import LineError, LineUnconfirmed
from kilo.line.reader import ChatReader, LinePage
from kilo.line.state import LineState
from kilo.clock import utc_now
from kilo.models.line import OutboxMessage, OutboxStatus

MAX_TEXT = 5000  # characters in one message


class LineOutbox:
    def __init__(self, state: LineState, reader: ChatReader, page: LinePage):
        self.state, self.reader, self.page = state, reader, page
        self._mutex = asyncio.Lock()  # one send at a time

    async def list(self, status: Optional[OutboxStatus] = None, limit: int = 50) -> List[OutboxMessage]:
        """Newest first; all statuses when `status` is None."""
        return await self.state.messages(status, limit)

    async def submit(self, chat: str, text: str, note: str = "") -> OutboxMessage:
        """Record the agent's message and immediately attempt to send it."""
        chat, text = chat.strip(), text.strip()
        if not chat or not text:
            raise LineError("Both the chat name and the text are required.")
        if len(text) > MAX_TEXT:
            raise LineError(f"The message is {len(text):,} characters; one message takes at most {MAX_TEXT:,}.")
        async with self._mutex:
            await self.reader.gate(chat)
            message = await self.state.save_message(OutboxMessage(chat=chat, text=text, note=note.strip()))
            return await self._send(message)

    async def _send(self, message: OutboxMessage) -> OutboxMessage:
        """Called under the mutex. Unconfirmed sends count as sent to avoid duplicate messages."""
        try:
            sent = await self.page.send(message.chat, message.text)
        except LineUnconfirmed as e:
            changes = {"status": OutboxStatus.SENT, "error": str(e)}
        except LineError as e:
            changes = {"status": OutboxStatus.FAILED, "error": str(e)}
        else:
            changes = {"status": OutboxStatus.SENT, "error": None, "message_id": sent.message_id}
        return await self.state.save_message(message.model_copy(update={
            **changes, "completed_at": utc_now(),
        }))
