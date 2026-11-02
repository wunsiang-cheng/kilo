"""
The assembled Kilo: every long-lived collaborator, built once on first use and handed to whatever needs
it. Nothing else builds them or keeps them in globals: tools are made here from what they act on, the
server's routes reach them through `get_kilo()`, and tests build a Kilo of their own, over a temp
directory and with fakes given by name (`Kilo(data_dir=tmp_path, chat_model=FakeModel(), ...)`).
"""

import logging
from datetime import datetime
from functools import cached_property
from pathlib import Path
from typing import Any, Callable, List, Optional

from kilo import workspace
from kilo.clock import utc_now
from kilo.agent.chat_model import ChatModel
from kilo.agent.runner import AgentRunner
from kilo.auth import Logins
from kilo.config import settings
from kilo.models.schedule import RunKind
from kilo.scheduling.announcer import RunAnnouncer
from kilo.scheduling.launcher import RunAgent, RunLauncher
from kilo.scheduling.scheduler import Scheduler
from kilo.storage.db import Database
from kilo.storage.schedules import ScheduleStore
from kilo.telegram.api import BotAPI
from kilo.telegram.bot import TelegramBot
from kilo.telegram.store import TelegramStore
from kilo.tools.base import Tool
from kilo.tools.file_tools import EDIT_FILE_TOOL, READ_FILE_TOOL, WRITE_FILE_TOOL
from kilo.tools.mcp_manager import MCPManager
from kilo.tools.mcp_tools import mcp_tool
from kilo.tools.schedule_tools import schedule_tool
from kilo.tools.search_tools import GLOB_TOOL, GREP_TOOL
from kilo.tools.shell import shell_tool
from kilo.tools.task_manager import TaskManager
from kilo.tools.toolbox import Toolbox

logger = logging.getLogger(__name__)



class Kilo:
    def __init__(
        self,
        data_dir: Optional[Path] = None,
        clock: Callable[[], datetime] = utc_now,
        **given: Any,
    ):
        """
        `data_dir` holds every database and file Kilo keeps (default KILO_DATA_DIR; the MCP config is then
        KILO_MCP_CONFIG). Any collaborator can be `given` by its attribute name instead of being built.
        """
        self.data_dir = data_dir or settings.kilo_data_dir
        self.mcp_config = settings.kilo_mcp_config if data_dir is None else data_dir / "mcp_servers.json"
        self.clock = clock
        unknown = [name for name in given if not isinstance(getattr(type(self), name, None), cached_property)]
        if unknown:
            raise TypeError(f"Kilo has no collaborator called {', '.join(unknown)}.")
        self.__dict__.update(given)  # cached_property reads them from here

    # ------------------------------------------------------------------
    # Storage

    @cached_property
    def db(self) -> Database:
        return Database(db_path=self.data_dir / "kilo.db")

    @cached_property
    def schedule_store(self) -> ScheduleStore:
        return ScheduleStore(db_path=self.data_dir / "kilo.db")

    # ------------------------------------------------------------------
    # Outside Kilo: the model, MCP servers and processes

    @cached_property
    def chat_model(self) -> ChatModel:
        from kilo.claude import ClaudeClient

        return ClaudeClient()

    @cached_property
    def mcp(self) -> MCPManager:
        return MCPManager(config_path=self.mcp_config, log_dir=self.data_dir / "mcp_logs")

    @cached_property
    def task_manager(self) -> TaskManager:
        return TaskManager(data_dir=self.data_dir)

    # ------------------------------------------------------------------
    # The agent and its runs

    @cached_property
    def tools(self) -> List[Tool]:
        """Every built-in tool, in the order they are presented to the LLM."""
        return [
            READ_FILE_TOOL, WRITE_FILE_TOOL, EDIT_FILE_TOOL, GLOB_TOOL, GREP_TOOL,
            shell_tool(self.task_manager), mcp_tool(self.mcp), schedule_tool(self.scheduler),
        ]

    @cached_property
    def toolbox(self) -> Toolbox:
        """The agent's tools in chats and scheduled runs: every built-in tool and every connected MCP server's."""
        return Toolbox(self.tools, self.mcp)

    @cached_property
    def runner(self) -> AgentRunner:
        """The agent of chats and scheduled runs."""
        return AgentRunner(self.chat_model, self.toolbox, self.db)

    @cached_property
    def launcher(self) -> RunLauncher:
        return RunLauncher(
            self.schedule_store, self.db,
            agents={
                RunKind.SCHEDULED: RunAgent(runner=lambda: self.runner),
            },
            clock=self.clock,
            announce=self.run_announcer.announce,
            publish=self.run_announcer.session_event,
        )

    @cached_property
    def run_announcer(self) -> RunAnnouncer:
        """Tells the web terminals open now how each run ended."""
        return RunAnnouncer()

    @cached_property
    def scheduler(self) -> Scheduler:
        return Scheduler(self.schedule_store, self.launcher, clock=self.clock)

    # ------------------------------------------------------------------
    # The web terminal

    @cached_property
    def logins(self) -> Logins:
        """Its one user (KILO_USERNAME / KILO_PASSWORD) and the browsers logged in."""
        return Logins(clock=self.clock)

    # ------------------------------------------------------------------
    # Telegram (optional)

    @cached_property
    def telegram(self) -> Optional[TelegramBot]:
        """The Telegram bot, when KILO_TELEGRAM_BOT_TOKEN is set."""
        if not settings.kilo_telegram_bot_token:
            return None
        return TelegramBot(
            BotAPI(settings.kilo_telegram_bot_token), TelegramStore(self.data_dir / "kilo.db"), self.db, self.runner,
            self.scheduler, self.run_announcer, settings.telegram_allowed_users, settings.telegram_allowed_chats,
        )

    # ------------------------------------------------------------------

    async def init(self) -> None:
        """Create the databases' tables and the workspace folder."""
        await self.db.init()
        await self.schedule_store.init()
        workspace.root().mkdir(parents=True, exist_ok=True)

    async def startup(self) -> None:
        """Start storage, MCP servers, and the scheduler before serving requests."""
        await self.init()
        await self.mcp.ensure_started()
        await self.launcher.recover()
        await self.scheduler.start()
        if self.telegram is not None:
            await self.telegram.start()

    async def shutdown(self) -> None:
        """Stop the Telegram bot, interrupt runs, then close MCP server connections."""
        if self.telegram is not None:
            await self.telegram.stop()
        await self.scheduler.stop()
        await self.launcher.stop()
        await self.mcp.shutdown()


_kilo: Optional[Kilo] = None


def get_kilo() -> Kilo:
    """The server's Kilo (built from the settings on first use)."""
    global _kilo
    if _kilo is None:
        _kilo = Kilo()
    return _kilo


def set_kilo(kilo: Optional[Kilo]) -> None:
    """Replace the server's Kilo (tests); None builds a new one from the settings on next use."""
    global _kilo
    _kilo = kilo
