"""SQLite-backed async storage for schedules and their runs (same database file as sessions)."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Type, TypeVar
import aiosqlite
from pydantic import BaseModel, TypeAdapter

from kilo.config import settings
from kilo.models.schedule import Run, RunKind, RunStatus, Schedule

Model = TypeVar("Model", bound=BaseModel)

_SCHEDULE_COLUMNS = list(Schedule.model_fields)
_RUN_COLUMNS = list(Run.model_fields)


_DATETIME = TypeAdapter(datetime)


def _iso(moment: datetime) -> str:
    """A UTC datetime in the same text form as stored columns, so they compare as strings."""
    return _DATETIME.dump_python(moment, mode="json")


def _row(model: BaseModel) -> Dict[str, Any]:
    """A model as column values: JSON-mode scalars (datetimes as ISO strings), nested models as JSON text."""
    values = model.model_dump(mode="json")
    if values.get("pending") is not None:
        values["pending"] = json.dumps(values["pending"])
    return values


def _model(cls: Type[Model], row: aiosqlite.Row) -> Model:
    values = dict(row)
    if "enabled" in values:
        values["enabled"] = bool(values["enabled"])
    if "approved" in values:
        values["approved"] = bool(values["approved"])
    if values.get("pending") is not None:
        values["pending"] = json.loads(values["pending"])
    return cls.model_validate(values)


class ScheduleStore:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or (settings.kilo_data_dir / "kilo.db")

    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS schedules (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    instructions TEXT NOT NULL,
                    cron TEXT,
                    run_at TEXT,
                    timezone TEXT NOT NULL,
                    active_from TEXT,
                    active_until TEXT,
                    enabled INTEGER NOT NULL,
                    approved INTEGER NOT NULL,
                    pending TEXT,
                    next_run_at TEXT,
                    last_run_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    schedule_id TEXT NOT NULL,
                    schedule_name TEXT NOT NULL,
                    session_id TEXT,
                    status TEXT NOT NULL,
                    scheduled_for TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    summary TEXT
                );
            """)
            columns = {row[1] for row in await (await conn.execute("PRAGMA table_info(runs)")).fetchall()}
            if "kind" not in columns:  # databases made before runs had kinds: LINE runs were told by their schedule id
                await conn.execute(f"ALTER TABLE runs ADD COLUMN kind TEXT NOT NULL DEFAULT '{RunKind.SCHEDULED.value}'")
                await conn.execute("UPDATE runs SET kind = ? WHERE schedule_id = ?", (RunKind.LINE.value, "line"))
            # Databases made before schedules remembered their origin session: their runs are shown nowhere.
            for table in ("schedules", "runs"):
                columns = {row[1] for row in await (await conn.execute(f"PRAGMA table_info({table})")).fetchall()}
                if "origin_session_id" not in columns:
                    await conn.execute(f"ALTER TABLE {table} ADD COLUMN origin_session_id TEXT")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_schedule_id ON runs (schedule_id);")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_started_at ON runs (started_at);")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_origin_session_id ON runs (origin_session_id);")
            await conn.commit()

    async def _upsert(self, table: str, columns: List[str], model: BaseModel) -> None:
        values = _row(model)
        placeholders = ", ".join("?" for _ in columns)
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(
                f"INSERT OR REPLACE INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
                [values[c] for c in columns],
            )
            await conn.commit()

    async def _select(self, cls: Type[Model], sql: str, params: tuple = ()) -> List[Model]:
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(sql, params)
            return [_model(cls, row) for row in await cursor.fetchall()]

    # Schedules

    async def save_schedule(self, schedule: Schedule) -> None:
        await self._upsert("schedules", _SCHEDULE_COLUMNS, schedule)

    async def get_schedule(self, schedule_id: str) -> Optional[Schedule]:
        rows = await self._select(Schedule, "SELECT * FROM schedules WHERE id = ?", (schedule_id,))
        return rows[0] if rows else None

    async def list_schedules(self) -> List[Schedule]:
        return await self._select(Schedule, "SELECT * FROM schedules ORDER BY created_at ASC, rowid ASC")

    async def delete_schedule(self, schedule_id: str) -> bool:
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
            await conn.commit()
            return cursor.rowcount > 0

    # Runs

    async def save_run(self, run: Run) -> None:
        await self._upsert("runs", _RUN_COLUMNS, run)

    async def get_run(self, run_id: str) -> Optional[Run]:
        rows = await self._select(Run, "SELECT * FROM runs WHERE id = ?", (run_id,))
        return rows[0] if rows else None

    async def list_runs(self, schedule_id: Optional[str] = None, limit: int = 20) -> List[Run]:
        """Most recent first."""
        if schedule_id:
            return await self._select(
                Run, "SELECT * FROM runs WHERE schedule_id = ? ORDER BY started_at DESC, rowid DESC LIMIT ?",
                (schedule_id, limit),
            )
        return await self._select(Run, "SELECT * FROM runs ORDER BY started_at DESC, rowid DESC LIMIT ?", (limit,))

    async def finished_runs_from(self, session_id: str) -> List[Run]:
        """The finished (or skipped) runs of schedules made in a session, in the order they ended."""
        return await self._select(
            Run, "SELECT * FROM runs WHERE origin_session_id = ? AND status != ? ORDER BY julianday(finished_at), rowid",
            (session_id, RunStatus.RUNNING.value),
        )

    async def fail_unfinished_runs(self, reason: str, finished_at: datetime) -> int:
        """Mark runs left 'running' (the server stopped mid-run) as failed."""
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute(
                "UPDATE runs SET status = ?, finished_at = ?, summary = ? WHERE status = ?",
                (RunStatus.FAILED.value, _iso(finished_at), reason, RunStatus.RUNNING.value),
            )
            await conn.commit()
            return cursor.rowcount

