"""The LINE account lists. The rest of LINE needs Chrome and a real account, and is not tested."""

import json
import os

import pytest

from kilo.line.accounts import DEFAULT_FILE, LineAccounts, LineAccountsError, ensure_accounts_file, load_accounts
from kilo.models.line import Sender


@pytest.fixture
def path(tmp_path):
    return tmp_path / "line_accounts.json"


def write(path, **lists):
    path.write_text(json.dumps(lists), encoding="utf-8")


def test_missing_file_serves_nothing(path):
    with pytest.raises(LineAccountsError):
        LineAccounts(path).current()


def test_empty_file_is_refused(path):
    path.write_text(" ", encoding="utf-8")
    with pytest.raises(LineAccountsError):
        load_accounts(path)


def test_ensure_accounts_file_creates_empty_lists(path):
    ensure_accounts_file(path)
    assert path.read_text(encoding="utf-8") == DEFAULT_FILE
    assert not load_accounts(path).serves("any chat")


def test_ensure_accounts_file_never_overwrites(path):
    write(path, chats=["kept"])
    ensure_accounts_file(path)
    assert load_accounts(path).serves("kept")


def test_only_listed_chats_are_served(path):
    write(path, chats=["Team"])
    accounts = load_accounts(path)
    assert accounts.serves("Team") and not accounts.serves("Other")


def test_star_serves_every_chat(path):
    write(path, chats=["*"])
    assert load_accounts(path).serves("anything")


def test_names_are_normalized(path):
    write(path, chats=["Ｔｅａｍ  A"])
    assert load_accounts(path).serves("Team A​")


def test_sender_roles(path):
    write(path, chats=[], managers=["Ann"], colleagues=["Bo"], bots=["Bot"])
    accounts = load_accounts(path)
    assert accounts.sender_role("Ann") == Sender.MANAGER
    assert accounts.sender_role("Bo") == Sender.COLLEAGUE
    assert accounts.sender_role("Bot") == Sender.BOT
    assert accounts.sender_role("Eve") is None
    assert accounts.sender_role(None) is None


def test_name_with_two_roles_refused(path):
    write(path, chats=[], managers=["Ann"], bots=["Ann"])
    with pytest.raises(LineAccountsError):
        load_accounts(path)


def test_chats_key_is_required(path):
    write(path, managers=[])
    with pytest.raises(LineAccountsError):
        load_accounts(path)


def test_unknown_key_refused(path):
    write(path, chats=[], typo=[])
    with pytest.raises(LineAccountsError):
        load_accounts(path)


def test_edit_is_picked_up_without_restart(path):
    write(path, chats=["Old"])
    accounts = LineAccounts(path)
    assert accounts.current().serves("Old")
    write(path, chats=["Newer"])
    os.utime(path, ns=(1, 1))  # a different stamp even on a coarse clock
    assert accounts.current().serves("Newer")


def test_broken_edit_stops_serving(path):
    write(path, chats=["Old"])
    accounts = LineAccounts(path)
    accounts.current()
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(LineAccountsError):
        accounts.current()
