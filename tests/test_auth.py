import pytest

from kilo.auth import LoginRefused, Logins, TooManyAttempts, is_local
from kilo.config import DEFAULT_PASSWORD

HOME = "127.0.0.1"


@pytest.fixture
def logins(clock) -> Logins:
    return Logins("u", "p", clock=clock)


def test_right_login_gives_a_valid_token(logins):
    assert logins.valid(logins.log_in("u", "p", HOME))


@pytest.mark.parametrize("username,password", [("u", "x"), ("x", "p")])
def test_wrong_login_refused(logins, username, password):
    with pytest.raises(LoginRefused):
        logins.log_in(username, password, HOME)


def test_unknown_token_is_not_valid(logins):
    assert not logins.valid("forged")
    assert not logins.valid(None)


def test_token_expires_after_seven_days(logins, clock):
    token = logins.log_in("u", "p", HOME)
    clock.advance(days=7)
    assert not logins.valid(token)


def test_log_out_invalidates_token(logins):
    token = logins.log_in("u", "p", HOME)
    logins.log_out(token)
    assert not logins.valid(token)


def fail(logins, times, client=HOME):
    for _ in range(times):
        with pytest.raises(LoginRefused):
            logins.log_in("u", "wrong", client)


def test_five_failures_lock_out_then_fifteen_minutes_release(logins, clock):
    fail(logins, 5)
    with pytest.raises(TooManyAttempts):
        logins.log_in("u", "p", HOME)
    clock.advance(minutes=15)
    assert logins.valid(logins.log_in("u", "p", HOME))


def test_lockout_is_per_address(logins):
    fail(logins, 5)
    assert logins.valid(logins.log_in("u", "p", "10.0.0.2"))


def test_success_clears_failures(logins):
    fail(logins, 4)
    logins.log_in("u", "p", HOME)
    fail(logins, 4)
    assert logins.valid(logins.log_in("u", "p", HOME))


def test_default_password_refused_remotely():
    logins = Logins("u", DEFAULT_PASSWORD)
    with pytest.raises(LoginRefused):
        logins.log_in("u", DEFAULT_PASSWORD, "8.8.8.8")
    with pytest.raises(LoginRefused):
        logins.log_in("u", DEFAULT_PASSWORD, HOME, direct=False)
    assert logins.valid(logins.log_in("u", DEFAULT_PASSWORD, HOME))


def test_is_local():
    assert all(is_local(host) for host in ("localhost", "127.0.0.1", "::1"))
    assert not any(is_local(host) for host in ("0.0.0.0", "", None, "example.com"))
