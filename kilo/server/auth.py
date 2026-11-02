"""
Every request needs a login, except the web terminal's own files, logging in, and the bare health check.
Checked here, in front of the whole app, so a new route is protected without anyone remembering to.
"""

from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketClose

from kilo.assembly import get_kilo

COOKIE = "kilo_login"
PUBLIC_PATHS = frozenset({"/", "/health", "/api/v1/auth/login"})
PUBLIC_PREFIX = "/static/"  # the page's scripts and styles; StaticFiles keeps requests inside its folder


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIX)


class LoginRequired:
    """ASGI middleware: refuses a request without a valid login cookie (401), unless its path is public."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket") and not is_public(scope["path"]):
            if not get_kilo().logins.valid(HTTPConnection(scope).cookies.get(COOKIE)):
                if scope["type"] == "websocket":
                    await WebSocketClose(code=1008)(scope, receive, send)
                else:
                    await JSONResponse({"detail": "Login required."}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)
