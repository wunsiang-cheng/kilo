"""FastAPI Application Server for Kilo Agent: the API and the web terminal, behind one login."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from kilo.assembly import get_kilo
from kilo.auth import is_local
from kilo.config import DEFAULT_PASSWORD, settings
from kilo.server.auth import LoginRequired
from kilo.server.routes.auth import router as auth_router
from kilo.server.routes.mcp import router as mcp_router
from kilo.server.routes.notices import router as notices_router
from kilo.server.routes.approvals import router as approvals_router
from kilo.server.routes.sessions import router as sessions_router
from kilo.server.routes.tasks import router as tasks_router
from kilo.server.routes.uploads import router as uploads_router

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# The page drives a shell: it runs only its own scripts and styles, talks only to this server,
# and cannot be framed by another site.
PAGE_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: storage and the workspace, MCP servers, then firing schedules
    await get_kilo().startup()
    yield
    # Shutdown: interrupt scheduled runs, then close MCP server connections
    await get_kilo().shutdown()


app = FastAPI(
    title="Kilo AI Agent Backend",
    description="Backend API for Kilo Autonomous Agent with Unrestricted Shell & Task Execution",
    version="0.1.0",
    lifespan=lifespan,
)

# No CORS: the web terminal is served from this same origin, and no other site may call the API.
app.add_middleware(LoginRequired)

# Register API routes
app.include_router(auth_router, prefix="/api/v1")
app.include_router(sessions_router, prefix="/api/v1")
app.include_router(uploads_router, prefix="/api/v1")
app.include_router(tasks_router, prefix="/api/v1")
app.include_router(mcp_router, prefix="/api/v1")
app.include_router(approvals_router, prefix="/api/v1")
app.include_router(notices_router, prefix="/api/v1")

# The web terminal
app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")


@app.get("/", include_in_schema=False)
async def web_terminal() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html", headers=PAGE_HEADERS)


@app.get("/health", tags=["system"])
async def liveness() -> Dict[str, str]:
    """Whether the server is up. Needs no login, so it says nothing else."""
    return {"status": "healthy"}


@app.get("/api/v1/health", tags=["system"])
async def health_check() -> Dict[str, Any]:
    """Health check endpoint."""
    kilo = get_kilo()
    tasks = kilo.task_manager.list_tasks()
    running_tasks = [t for t in tasks if t.status.value == "running"]

    return {
        "status": "healthy",
        "agent": "Kilo",
        "model": settings.openrouter_model,
        "compact_at_tokens": settings.kilo_compact_at_tokens,
        "timezone": settings.kilo_timezone,
        "workspace": str(settings.kilo_workspace),
        "total_tasks": len(tasks),
        "running_tasks": len(running_tasks),
        "schedules_enabled": sum(1 for s in await kilo.scheduler.list() if s.approved and s.enabled),
        "mcp_servers_connected": sum(1 for s in kilo.mcp.list_servers() if s["server_status"] == "connected"),
    }


def main():
    """Start uvicorn server directly."""
    if not settings.openrouter_api_key:
        # Every reply would fail; say so now rather than at the first message.
        raise SystemExit("OPENROUTER_API_KEY is not set: put your OpenRouter API key in .env (see .env.example).")
    if settings.kilo_password == DEFAULT_PASSWORD:
        if not is_local(settings.kilo_host):
            # Anyone who has read the code could log in and use the agent's shell.
            raise SystemExit(
                f"Refusing to serve on {settings.kilo_host} with the default password, which is public. "
                "Set KILO_PASSWORD in .env, or keep KILO_HOST on 127.0.0.1."
            )
        logger.warning("Kilo is using the default password; set KILO_PASSWORD in .env to change it.")
    uvicorn.run(
        "kilo.server.app:app",
        host=settings.kilo_host,
        port=settings.kilo_port,
        reload=False,
    )


if __name__ == "__main__":
    main()
