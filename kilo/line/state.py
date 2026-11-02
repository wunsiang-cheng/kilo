"""
LINE's local state: outgoing messages (`line_outbox`) in `kilo.db` of Kilo's data dir. The table keeps the
`session_id` and `run_id` columns of older records; new messages leave them empty.
"""

from pathlib import Path
from typing import Any, List, Optional

import aiosqlite

from kilo.models.line import OutboxMessage, OutboxStatus

_COLUMNS = [c for c in OutboxMessage.model_fields if c != "id"]


class LineState:
    def __init__(self, data_dir: Path):
        self.db_path = data_dir / "kilo.db"

    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS line_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat TEXT NOT NULL,
                    text TEXT NOT NULL,
                    note TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    message_id TEXT,
                    session_id TEXT,
                    run_id TEXT
                );
            """)
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_line_outbox_status ON line_outbox (status);")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_line_outbox_chat ON line_outbox (chat);")
            await conn.commit()

    # ------------------------------------------------------------------
    # Outgoing messages

    async def save_message(self, message: OutboxMessage) -> OutboxMessage:
        values = message.model_dump(mode="json")
        async with aiosqlite.connect(self.db_path) as conn:
            if message.id is None:
                cursor = await conn.execute(
                    f"INSERT INTO line_outbox ({', '.join(_COLUMNS)}) VALUES ({', '.join('?' for _ in _COLUMNS)})",
                    [values[c] for c in _COLUMNS],
                )
                message = message.model_copy(update={"id": cursor.lastrowid})
            else:
                await conn.execute(
                    f"UPDATE line_outbox SET {', '.join(f'{c} = ?' for c in _COLUMNS)} WHERE id = ?",
                    [*(values[c] for c in _COLUMNS), message.id],
                )
            await conn.commit()
        return message

    async def _messages(self, sql: str, params: Any = ()) -> List[OutboxMessage]:
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            rows = await (await conn.execute(sql, params)).fetchall()
        return [OutboxMessage.model_validate(dict(row)) for row in rows]

    async def messages(self, status: Optional[OutboxStatus] = None, limit: int = 50) -> List[OutboxMessage]:
        """Newest first; all statuses when `status` is None."""
        if status is None:
            return await self._messages("SELECT * FROM line_outbox ORDER BY id DESC LIMIT ?", (limit,))
        return await self._messages(
            "SELECT * FROM line_outbox WHERE status = ? ORDER BY id DESC LIMIT ?", (status.value, limit),
        )

    async def sent_to(self, chat: str, limit: int = 200) -> List[OutboxMessage]:
        """Kilo's messages that went out to `chat`, newest first."""
        return await self._messages(
            "SELECT * FROM line_outbox WHERE chat = ? AND status = ? ORDER BY id DESC LIMIT ?",
            (chat, OutboxStatus.SENT.value, limit),
        )


