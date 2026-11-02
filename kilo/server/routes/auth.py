"""Logging in and out of the web terminal. The login is a cookie the browser sends with every request."""

from typing import Any, Dict
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from kilo.assembly import get_kilo
from kilo.auth import LOGIN_LIFETIME, LoginRefused, TooManyAttempts, is_local
from kilo.server.auth import COOKIE

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
async def log_in(credentials: LoginRequest, request: Request, response: Response) -> Dict[str, Any]:
    """Check the username and password and set the login cookie. Needs no login itself."""
    logins = get_kilo().logins
    client = request.client.host if request.client else None
    forwarded = any(h in request.headers for h in ("forwarded", "x-forwarded-for", "x-real-ip"))
    direct = is_local(request.url.hostname) and not forwarded
    try:
        token = logins.log_in(credentials.username, credentials.password, client, direct)
    except TooManyAttempts as e:
        raise HTTPException(status_code=429, detail=str(e), headers={"Retry-After": str(e.retry_after)})
    except LoginRefused as e:
        raise HTTPException(status_code=401, detail=str(e))
    response.set_cookie(
        COOKIE, token, max_age=int(LOGIN_LIFETIME.total_seconds()), path="/",
        # Out of reach of the page's scripts, never sent by other sites' requests, and over HTTPS only when served so.
        httponly=True, samesite="strict", secure=request.url.scheme == "https",
    )
    return {"username": logins.username}


@router.post("/logout")
async def log_out(request: Request, response: Response) -> Dict[str, Any]:
    get_kilo().logins.log_out(request.cookies.get(COOKIE))
    response.delete_cookie(COOKIE, path="/")
    return {"status": "success"}


@router.get("/me")
async def who_am_i() -> Dict[str, Any]:
    """The logged-in user (any other caller gets 401)."""
    return {"username": get_kilo().logins.username}
