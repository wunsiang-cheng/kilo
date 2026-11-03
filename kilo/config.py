"""Application Configuration."""

from pathlib import Path
from typing import FrozenSet, Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_COMPACT_AT_TOKENS = 10_000
MIN_TOOL_OUTPUT_CHARS = 10_000
# The web terminal's login out of the box. Public, so it is accepted only from this computer (kilo/auth.py).
DEFAULT_USERNAME = "drg@drg.best"
DEFAULT_PASSWORD = "9487skrskr"


class Settings(BaseSettings):
    # The model, through OpenRouter. kilo-server refuses to start without the key (kilo/server/app.py).
    openrouter_api_key: Optional[str] = None
    openrouter_model: str = "meta/muse-spark-1.3-contributor"
    openrouter_reasoning_effort: Literal["max", "xhigh", "high", "medium", "low", "minimal", "none"] = "high"
    openrouter_max_tokens: Optional[int] = Field(None, ge=1)

    # Server Settings
    kilo_host: str = "127.0.0.1"
    kilo_port: int = Field(8000, ge=1, le=65535)
    # The web terminal's only user
    kilo_username: str = DEFAULT_USERNAME
    kilo_password: str = DEFAULT_PASSWORD
    # The agent's fixed working directory: relative paths in every tool resolve here.
    kilo_workspace: Path = Path("~/kilo-workspace")
    kilo_data_dir: Path = Path(".kilo_data")
    kilo_default_timeout: int = Field(600, ge=1)
    # Largest tool result the model sees (characters, about 4 per token); longer ones are shortened and saved
    # in full under <data dir>/tool-output.
    kilo_tool_output_max_chars: int = 100_000
    kilo_mcp_config: Path = Path(".kilo_data/mcp_servers.json")  # unset: mcp_servers.json in the data dir

    # Scheduled jobs
    kilo_timezone: str = "Asia/Taipei"  # default timezone of schedules
    kilo_job_catchup_minutes: int = Field(5, ge=0)  # a run missed by more than this (server down) is skipped

    # Telegram, an optional second interface: a bot that only the users listed may talk to, in private chats and
    # in the groups listed. Unset token: no bot. Users are numeric Telegram user ids (not @usernames, which can
    # change), groups numeric chat ids (negative), comma-separated.
    kilo_telegram_bot_token: Optional[str] = None
    kilo_telegram_allowed_users: str = ""
    kilo_telegram_allowed_chats: str = ""

    # Compaction: a conversation this large (tokens) is summarized before its next request; 0 turns it off.
    # It should stay well below the model's context length, which differs from model to model.
    kilo_compact_at_tokens: int = 500_000
    # Cap on one summary, reasoning included; a summary cut off is retried with twice this
    kilo_compact_max_tokens: int = Field(16_000, ge=1, le=64_000)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("kilo_workspace", "kilo_data_dir", "kilo_mcp_config")
    @classmethod
    def _absolute_path(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    @field_validator("kilo_username", "kilo_password")
    @classmethod
    def _login_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank: anyone could log in and use the agent's shell.")
        return value

    @field_validator("kilo_timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            raise ValueError(f"unknown timezone '{value}': use an IANA name such as 'Asia/Taipei'.")
        return value

    @field_validator("kilo_tool_output_max_chars")
    @classmethod
    def _output_limit_usable(cls, value: int) -> int:
        if value < MIN_TOOL_OUTPUT_CHARS:
            raise ValueError(f"KILO_TOOL_OUTPUT_MAX_CHARS must be at least {MIN_TOOL_OUTPUT_CHARS:,}.")
        return value

    @field_validator("kilo_telegram_bot_token")
    @classmethod
    def _token_or_none(cls, value: Optional[str]) -> Optional[str]:
        return value.strip() or None if value is not None else None

    @field_validator("kilo_telegram_allowed_users")
    @classmethod
    def _user_ids(cls, value: str) -> str:
        for item in filter(None, (part.strip() for part in value.split(","))):
            if not item.isdigit():
                raise ValueError(f"'{item}' is not a Telegram user id: list numeric ids, comma-separated.")
        return value

    @field_validator("kilo_telegram_allowed_chats")
    @classmethod
    def _chat_ids(cls, value: str) -> str:
        for item in filter(None, (part.strip() for part in value.split(","))):
            if not item.removeprefix("-").isdigit():
                raise ValueError(f"'{item}' is not a Telegram chat id: list numeric ids (groups' are negative), comma-separated.")
        return value

    @property
    def telegram_allowed_users(self) -> FrozenSet[int]:
        return frozenset(int(part) for part in self.kilo_telegram_allowed_users.split(",") if part.strip())

    @property
    def telegram_allowed_chats(self) -> FrozenSet[int]:
        return frozenset(int(part) for part in self.kilo_telegram_allowed_chats.split(",") if part.strip())

    @model_validator(mode="after")
    def _telegram_has_users(self) -> "Settings":
        if self.kilo_telegram_bot_token and not self.telegram_allowed_users:
            raise ValueError("KILO_TELEGRAM_ALLOWED_USERS must list at least one user id when KILO_TELEGRAM_BOT_TOKEN is set.")
        return self

    @model_validator(mode="after")
    def _mcp_config_follows_data_dir(self) -> "Settings":
        if "kilo_mcp_config" not in self.model_fields_set:
            self.kilo_mcp_config = self.kilo_data_dir / "mcp_servers.json"
        return self

    @model_validator(mode="after")
    def _compaction_threshold_usable(self) -> "Settings":
        # No upper bound: the context window is the model's, and models differ (see .env.example).
        if self.kilo_compact_at_tokens and self.kilo_compact_at_tokens < MIN_COMPACT_AT_TOKENS:
            raise ValueError(f"KILO_COMPACT_AT_TOKENS must be 0 (off) or at least {MIN_COMPACT_AT_TOKENS:,}.")
        return self


settings = Settings()

# Ensure directories exist
settings.kilo_data_dir.mkdir(parents=True, exist_ok=True)
(settings.kilo_data_dir / "tasks").mkdir(parents=True, exist_ok=True)
settings.kilo_workspace.mkdir(parents=True, exist_ok=True)
