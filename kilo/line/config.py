"""
The LINE server's settings, read from the environment and `.env` (in the folder it starts in) like Kilo's own.
Only these: the LINE server is a process of its own and needs nothing else of Kilo's configuration.
"""

from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class LineSettings(BaseSettings):
    # Where the LINE state (outgoing messages, in kilo.db) and the LINE browser profile live: Kilo's data dir
    kilo_data_dir: Path = Path(".kilo_data")
    kilo_timezone: str = "Asia/Taipei"  # the timezone message times are shown in
    kilo_line_cdp_url: str = "http://127.0.0.1:9333"  # the DevTools address of `kilo-line-browser`'s Chrome
    # The account lists (kilo/line/accounts.py): the chats the agent may use (empty: none; ["*"]: every chat) and the display
    # names of managers, colleagues and notification bots. Exact names, read again whenever the file changes.
    # Written with empty lists on first start when missing. Unset: line_accounts.json in the data dir.
    kilo_line_accounts: Path = Path(".kilo_data/line_accounts.json")
    # Tests only: messages the account itself sends that start with this are taken as a contact's (a tester
    # posting as a customer from the account); never set in production. KILO_LINE_TEST_PARENT_PREFIX is its old name.
    kilo_line_test_prefix: str = Field(
        "", validation_alias=AliasChoices("KILO_LINE_TEST_PREFIX", "KILO_LINE_TEST_PARENT_PREFIX"),
    )

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @field_validator("kilo_data_dir", "kilo_line_accounts")
    @classmethod
    def _absolute_path(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    @field_validator("kilo_timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            raise ValueError(f"unknown timezone '{value}': use an IANA name such as 'Asia/Taipei'.")
        return value

    @model_validator(mode="after")
    def _accounts_follow_data_dir(self) -> "LineSettings":
        if "kilo_line_accounts" not in self.model_fields_set:
            self.kilo_line_accounts = self.kilo_data_dir / "line_accounts.json"
        return self


settings = LineSettings()
