"""
The web terminal's login: one user, KILO_USERNAME with KILO_PASSWORD. Logging in hands the browser a
random token (kept in a cookie by the server's routes). Tokens live in memory only: a restart signs every
browser out, which is also how a changed password takes effect everywhere.
"""

import hmac
import ipaddress
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Dict, Optional

from kilo.clock import utc_now
from kilo.config import DEFAULT_PASSWORD, settings

LOGIN_LIFETIME = timedelta(days=7)
MAX_FAILURES = 5  # wrong attempts in a row from one address before it is locked out
LOCKOUT = timedelta(minutes=15)  # how long a lockout lasts; an older run of failures is forgotten

DEFAULT_PASSWORD_REMOTE = (
    "The default password works only from this computer. Set KILO_PASSWORD in .env and restart the server."
)



def is_local(host: Optional[str]) -> bool:
    """Whether a host name or address is this computer (a loopback address, or `localhost`)."""
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class LoginRefused(Exception):
    """Wrong username or password, or the default password from another computer."""


class TooManyAttempts(LoginRefused):
    def __init__(self, retry_after: int):
        super().__init__(f"Too many failed logins from this address. Try again in {max(1, retry_after // 60)} minute(s).")
        self.retry_after = retry_after  # seconds


@dataclass
class _Failures:
    count: int
    last: datetime  # the latest wrong attempt


class Logins:
    def __init__(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        clock: Callable[[], datetime] = utc_now,
    ):
        self.username = settings.kilo_username if username is None else username
        self.password = settings.kilo_password if password is None else password
        self._clock = clock
        self._tokens: Dict[str, datetime] = {}  # token -> when it expires
        self._failures: Dict[str, _Failures] = {}  # client address -> its current run of wrong attempts

    @property
    def default_password(self) -> bool:
        return self.password == DEFAULT_PASSWORD

    def log_in(self, username: str, password: str, client: Optional[str], direct: bool = True) -> str:
        """
        A new login token for the right username and password from `client` (the caller's address).
        `direct` says the request came straight from this computer: its Host is this computer too and no proxy
        forwarded it. The default password needs both, so neither a web page whose name was pointed at
        127.0.0.1 (DNS rebinding) nor a visitor through a proxy on this computer can use it.
        Raises TooManyAttempts while the address is locked out, LoginRefused otherwise.
        """
        now = self._clock()
        self._failures = {a: f for a, f in self._failures.items() if now - f.last < LOCKOUT}
        address = client or "unknown"
        failures = self._failures.get(address)
        if failures and failures.count >= MAX_FAILURES:
            raise TooManyAttempts(int((LOCKOUT - (now - failures.last)).total_seconds()))

        # Both compared in full, in constant time, so the timing tells nothing about either.
        name_ok = hmac.compare_digest(username.encode(), self.username.encode())
        password_ok = hmac.compare_digest(password.encode(), self.password.encode())
        if not (name_ok and password_ok):
            self._failures[address] = _Failures(count=(failures.count if failures else 0) + 1, last=now)
            raise LoginRefused("Wrong username or password.")
        if self.default_password and not (direct and is_local(client)):
            raise LoginRefused(DEFAULT_PASSWORD_REMOTE)

        self._failures.pop(address, None)
        self._tokens = {t: expiry for t, expiry in self._tokens.items() if expiry > now}
        token = secrets.token_urlsafe(32)
        self._tokens[token] = now + LOGIN_LIFETIME
        return token

    def valid(self, token: Optional[str]) -> bool:
        """Whether `token` is a login that has not expired or been logged out."""
        expiry = self._tokens.get(token) if token else None
        if expiry is None:
            return False
        if expiry <= self._clock():
            del self._tokens[token]
            return False
        return True

    def log_out(self, token: Optional[str]) -> None:
        if token:
            self._tokens.pop(token, None)
