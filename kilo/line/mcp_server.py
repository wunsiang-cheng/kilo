"""
`kilo-line-mcp`: the LINE account as an MCP server (stdio), for Kilo or any MCP client. Its tools read the
account's chats and send messages through the dedicated Chrome of `kilo-line-browser`, which must be running.
Settings come from the environment and `.env` (kilo/line/config.py), so start it in Kilo's folder.
"""

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from kilo.line.accounts import EVERY_CHAT, LineAccounts, ensure_accounts_file
from kilo.line.browser import LineBrowser, LineError
from kilo.line.config import settings
from kilo.line.outbox import LineOutbox
from kilo.line.reader import ChatReader, LinePage
from kilo.line.state import LineState
from kilo.line.tools import DESCRIPTIONS, INSTRUCTIONS, LineTools

logger = logging.getLogger(__name__)


def build_server(page: LinePage, state: LineState, accounts: LineAccounts) -> MCPServer:
    """The server over `page` (LineBrowser, or a fake in tests); `state` must be initialized before a tool runs."""
    reader = ChatReader(page, state, accounts)
    tools = LineTools(reader, LineOutbox(state, reader, page))

    async def run(action, *args: Any) -> Dict[str, Any]:
        try:
            return await action(*args)
        except LineError as e:
            raise ToolError(str(e)) from e

    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
        await state.init()
        ensure_accounts_file(accounts.path)
        try:
            chats = accounts.current().chats
            if not chats:
                logger.warning("The \"chats\" list in %s is empty: no chat can be read or written until you list some.", accounts.path)
            elif EVERY_CHAT in chats:
                logger.warning("The \"chats\" list in %s holds \"*\": every chat of the account can be read and written.", accounts.path)
        except LineError as e:
            logger.warning("%s", e)
        try:
            yield
        finally:
            if hasattr(page, "close"):
                await page.close()

    server = MCPServer("line", instructions=INSTRUCTIONS, lifespan=lifespan)

    @server.tool(description=DESCRIPTIONS["chats"])
    async def chats() -> Dict[str, Any]:
        return await run(tools.chats)

    @server.tool(description=DESCRIPTIONS["read"])
    async def read(chat: str, limit: int = 20) -> Dict[str, Any]:
        return await run(tools.read, chat, limit)

    @server.tool(description=DESCRIPTIONS["send"])
    async def send(chat: str, text: str, note: str = "") -> Dict[str, Any]:
        return await run(tools.send, chat, text, note)

    @server.tool(description=DESCRIPTIONS["outbox"])
    async def outbox() -> Dict[str, Any]:
        return await run(tools.recent)

    return server


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    build_server(LineBrowser(), LineState(settings.kilo_data_dir), LineAccounts(settings.kilo_line_accounts)).run("stdio")


if __name__ == "__main__":
    main()
