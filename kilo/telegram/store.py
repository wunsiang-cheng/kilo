"""What the bot keeps (same database file as sessions): each chat's current session, the chat each session was used from, and the sessions each group owns."""

from pathlib import Path
from typing import List, Optional
import aiosqlite


class TelegramStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS telegram_chats (chat_id INTEGER PRIMARY KEY, session_id TEXT NOT NULL)"
            )
            # Where the turns the system starts in a session (scheduled runs) are shown: the chat it was last used from.
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS telegram_sessions (session_id TEXT PRIMARY KEY, chat_id INTEGER NOT NULL)"
            )
            # The sessions made in a group, the only ones it may use (everyone in it sees what the bot says).
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS telegram_owned (session_id TEXT PRIMARY KEY, chat_id INTEGER NOT NULL)"
            )
            await conn.commit()

    async def current(self, chat_id: int) -> Optional[str]:
        """The chat's current session, if it has one."""
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute("SELECT session_id FROM telegram_chats WHERE chat_id = ?", (chat_id,))
            row = await cursor.fetchone()
            return row[0] if row else None

    async def use(self, chat_id: int, session_id: str) -> None:
        """Make the session the chat's current one, and the chat the one that shows the session's scheduled runs."""
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(
                "INSERT INTO telegram_chats (chat_id, session_id) VALUES (?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET session_id = excluded.session_id",
                (chat_id, session_id),
            )
            await conn.execute(
                "INSERT INTO telegram_sessions (session_id, chat_id) VALUES (?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET chat_id = excluded.chat_id",
                (session_id, chat_id),
            )
            await conn.commit()

    async def chat_of(self, session_id: str) -> Optional[int]:
        """The chat the session was last used from, if any."""
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute("SELECT chat_id FROM telegram_sessions WHERE session_id = ?", (session_id,))
            row = await cursor.fetchone()
            return row[0] if row else None

    async def own(self, session_id: str, chat_id: int) -> None:
        """Make the session the group's own."""
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("INSERT OR REPLACE INTO telegram_owned (session_id, chat_id) VALUES (?, ?)", (session_id, chat_id))
            await conn.commit()

    async def owner(self, session_id: str) -> Optional[int]:
        """The group the session belongs to, if any."""
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute("SELECT chat_id FROM telegram_owned WHERE session_id = ?", (session_id,))
            row = await cursor.fetchone()
            return row[0] if row else None

    async def owned(self, chat_id: int) -> List[str]:
        """The sessions that belong to the group."""
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute("SELECT session_id FROM telegram_owned WHERE chat_id = ?", (chat_id,))
            return [row[0] for row in await cursor.fetchall()]
