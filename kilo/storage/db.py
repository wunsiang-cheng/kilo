"""SQLite-backed async storage for Sessions and Messages."""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import aiosqlite

from kilo.config import settings
from kilo.models.session import Message, MessageRole, Session


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(iso_str: str) -> datetime:
    return datetime.fromisoformat(iso_str)


def _message(r: Any) -> Message:
    return Message(
        id=r["id"],
        session_id=r["session_id"],
        role=MessageRole(r["role"]),
        content=r["content"],
        blocks=json.loads(r["blocks"]) if r["blocks"] else None,
        created_at=_parse_iso(r["created_at"]),
    )


class Database:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or (settings.kilo_data_dir / "kilo.db")

    async def init(self) -> None:
        """Initialize database tables and indexes."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA foreign_keys = ON;")
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}'
                );
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT,
                    blocks TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
                );
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_session_id ON messages (session_id);
            """)
            await conn.commit()

    async def create_session(
        self,
        title: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        session_id: Optional[str] = None,
    ) -> Session:
        sid = session_id or f"sess_{uuid.uuid4().hex[:12]}"
        now = _iso_now()
        sess_title = title.strip() if title and title.strip() else f"Session {sid}"
        meta_json = json.dumps(metadata or {})

        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(
                """
                INSERT INTO sessions (id, title, created_at, updated_at, metadata)
                VALUES (?, ?, ?, ?, ?)
                """,
                (sid, sess_title, now, now, meta_json),
            )
            await conn.commit()

        return Session(
            id=sid,
            title=sess_title,
            created_at=_parse_iso(now),
            updated_at=_parse_iso(now),
            metadata=metadata or {},
            messages=[],
        )

    async def get_session(self, session_id: str, include_messages: bool = True) -> Optional[Session]:
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT id, title, created_at, updated_at, metadata FROM sessions WHERE id = ?",
                (session_id,),
            )
            row = await cursor.fetchone()
            if not row:
                return None

            messages: List[Message] = []
            if include_messages:
                msg_cursor = await conn.execute(
                    """
                    SELECT id, session_id, role, content, blocks, created_at
                    FROM messages
                    WHERE session_id = ?
                    ORDER BY rowid ASC
                    """,
                    (session_id,),
                )
                messages = [_message(r) for r in await msg_cursor.fetchall()]

            return Session(
                id=row["id"],
                title=row["title"],
                created_at=_parse_iso(row["created_at"]),
                updated_at=_parse_iso(row["updated_at"]),
                metadata=json.loads(row["metadata"]),
                messages=messages,
            )

    async def list_sessions(self, limit: int = 50, offset: int = 0) -> List[Session]:
        sessions: List[Session] = []
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                """
                SELECT id, title, created_at, updated_at, metadata
                FROM sessions
                ORDER BY updated_at DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            )
            rows = await cursor.fetchall()
            for r in rows:
                sessions.append(
                    Session(
                        id=r["id"],
                        title=r["title"],
                        created_at=_parse_iso(r["created_at"]),
                        updated_at=_parse_iso(r["updated_at"]),
                        metadata=json.loads(r["metadata"]),
                        messages=[],
                    )
                )
        return sessions

    async def update_session(
        self,
        session_id: str,
        title: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[Session]:
        now = _iso_now()
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT id, title, metadata FROM sessions WHERE id = ?", (session_id,))
            row = await cursor.fetchone()
            if not row:
                return None

            new_title = title if title is not None else row["title"]
            curr_meta = json.loads(row["metadata"])
            if metadata is not None:
                curr_meta.update(metadata)
            new_meta_json = json.dumps(curr_meta)

            await conn.execute(
                """
                UPDATE sessions
                SET title = ?, metadata = ?, updated_at = ?
                WHERE id = ?
                """,
                (new_title, new_meta_json, now, session_id),
            )
            await conn.commit()

        return await self.get_session(session_id, include_messages=False)

    async def delete_session(self, session_id: str) -> bool:
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("PRAGMA foreign_keys = ON;")
            cursor = await conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            await conn.commit()
            return cursor.rowcount > 0

    async def add_message(
        self,
        session_id: str,
        role: MessageRole,
        content: Optional[str] = None,
        blocks: Optional[List[Dict[str, Any]]] = None,
        message_id: Optional[str] = None,
    ) -> Message:
        mid = message_id or f"msg_{uuid.uuid4().hex[:12]}"
        now = _iso_now()
        blocks_json = json.dumps(blocks) if blocks is not None else None

        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(
                """
                INSERT INTO messages (id, session_id, role, content, blocks, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (mid, session_id, role.value, content, blocks_json, now),
            )
            # Update session's updated_at
            await conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, session_id),
            )
            await conn.commit()

        return Message(
            id=mid,
            session_id=session_id,
            role=role,
            content=content,
            blocks=blocks,
            created_at=_parse_iso(now),
        )

    async def get_messages(self, session_id: str, since_id: Optional[str] = None) -> List[Message]:
        """
        A session's messages in the order they were added (insertion order: a clock set back cannot reorder
        them). With `since_id`, only that message and the ones after it, e.g. from the latest summary on.
        """
        query = "SELECT id, session_id, role, content, blocks, created_at FROM messages WHERE session_id = ?"
        params: tuple = (session_id,)
        if since_id:
            query += " AND rowid >= COALESCE((SELECT rowid FROM messages WHERE id = ? AND session_id = ?), 0)"
            params += (since_id, session_id)
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(query + " ORDER BY rowid ASC", params)
            return [_message(r) for r in await cursor.fetchall()]

