"""The time now, in UTC: the default clock of everything that takes one (tests pass their own)."""

from datetime import datetime, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
