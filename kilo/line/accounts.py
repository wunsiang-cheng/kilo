"""
The LINE account lists (KILO_LINE_ACCOUNTS): which chats Kilo may use, and the display names of the managers,
colleagues and notification bots who write in them. Engineers keep the file. Names are matched exactly, after
`normalize` evens out spacing and full-width characters, so nothing is guessed from a name's wording.

The file is read again whenever it changes, so edits take effect without a restart. A file that is missing or
cannot be used stops LINE until it is fixed; it is never read as "no restrictions". An empty `chats` list serves
no chat, so a new file opens nothing; the only way to leave every chat open is to write `"chats": ["*"]`.
"""

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import FrozenSet, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from kilo.line.browser import LineError
from kilo.models.line import Sender

EVERY_CHAT = "*"
DEFAULT_FILE = '{\n  "chats": [],\n  "managers": [],\n  "colleagues": [],\n  "bots": []\n}\n'
_INVISIBLE = re.compile(r"[\u200b-\u200d\u2060\ufeff\ufe0f]")


class LineAccountsError(LineError):
    """The account lists cannot be used; the message says where and why."""


def normalize(name: str) -> str:
    """A name as the lists compare it: names are typed by hand, so width, spacing and invisible marks are evened out."""
    return re.sub(r"\s+", " ", _INVISIBLE.sub("", unicodedata.normalize("NFKC", name))).strip()


class _File(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chats: List[str]  # required: leaving it out must not quietly open every chat
    managers: List[str] = Field(default_factory=list)
    colleagues: List[str] = Field(default_factory=list)
    bots: List[str] = Field(default_factory=list)

    @field_validator("chats", "managers", "colleagues", "bots")
    @classmethod
    def _names(cls, names: List[str]) -> List[str]:
        cleaned = [normalize(n) for n in names]
        if "" in cleaned:
            raise ValueError("a name is empty")
        return cleaned

    @model_validator(mode="after")
    def _one_role_each(self) -> "_File":
        roles = (("managers", self.managers), ("colleagues", self.colleagues), ("bots", self.bots))
        for i, (first, first_names) in enumerate(roles):
            for second, second_names in roles[i + 1:]:
                both = sorted(set(first_names) & set(second_names))
                if both:
                    raise ValueError(f"a name can have one role only; in both {first} and {second}: {both}")
        return self


@dataclass(frozen=True)
class Accounts:
    """The lists as read from the file, names normalized."""

    chats: FrozenSet[str]
    managers: FrozenSet[str]
    colleagues: FrozenSet[str]
    bots: FrozenSet[str]

    def serves(self, chat: str) -> bool:
        """Whether Kilo may read and write in the chat: a listed one, or any chat when `chats` holds "*"."""
        return EVERY_CHAT in self.chats or normalize(chat) in self.chats

    def sender_role(self, name: Optional[str]) -> Optional[Sender]:
        """The role the lists give a sender's display name, or None for anyone not listed."""
        if not name:
            return None
        name = normalize(name)
        for role, names in ((Sender.MANAGER, self.managers), (Sender.COLLEAGUE, self.colleagues), (Sender.BOT, self.bots)):
            if name in names:
                return role
        return None


def load_accounts(path: Path) -> Accounts:
    """The lists in `path`; raises LineAccountsError when the file is missing, empty or invalid."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise LineAccountsError(f"The LINE account lists {path} are missing; Kilo uses no LINE chat until the file is back.")
    except (OSError, UnicodeDecodeError) as e:
        raise LineAccountsError(f"The LINE account lists {path} cannot be read: {e}")
    if not text.strip():
        raise LineAccountsError(f'The LINE account lists {path} are empty. Write {{"chats": ["*"]}} to leave every chat open.')
    try:
        data = _File.model_validate(json.loads(text))
    except (json.JSONDecodeError, ValidationError) as e:
        raise LineAccountsError(f"The LINE account lists {path} are not usable:\n{e}")
    return Accounts(frozenset(data.chats), frozenset(data.managers), frozenset(data.colleagues), frozenset(data.bots))


class LineAccounts:
    """The account lists file, read again whenever it changes."""

    def __init__(self, path: Path):
        self.path = path
        self._seen: Optional[Tuple[Tuple[int, int], Accounts]] = None

    def current(self) -> Accounts:
        """The lists as the file has them now; raises LineAccountsError, and nothing is served, while it is unusable."""
        try:
            stat = self.path.stat()
        except OSError:
            self._seen = None
            return load_accounts(self.path)  # raises, saying whether the file is missing or unreadable
        key = (stat.st_mtime_ns, stat.st_size)
        if self._seen is None or self._seen[0] != key:
            self._seen = (key, load_accounts(self.path))
        return self._seen[1]


def ensure_accounts_file(path: Path) -> None:
    """Write empty lists to `path`, to be edited, when there is no file there; never overwrites."""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEFAULT_FILE, encoding="utf-8")
