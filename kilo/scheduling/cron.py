"""Five-field cron expressions (minute hour day-of-month month day-of-week), evaluated in wall-clock time."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import FrozenSet, Optional, Tuple

_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_DAYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
_MACROS = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
}

# (low, high, names) per field; day-of-week accepts 7 as Sunday.
_FIELDS: Tuple[Tuple[str, int, int, Tuple[str, ...]], ...] = (
    ("minute", 0, 59, ()),
    ("hour", 0, 23, ()),
    ("day of month", 1, 31, ()),
    ("month", 1, 12, _MONTHS),
    ("day of week", 0, 7, _DAYS),
)

# Far enough to cover every valid expression (Feb 29 on a given weekday recurs within 28 years).
_SEARCH_LIMIT = timedelta(days=366 * 28)


def _value(token: str, name: str, low: int, names: Tuple[str, ...]) -> int:
    lowered = token.lower()
    if lowered in names:
        return names.index(lowered) + low  # jan = 1, sun = 0
    if not token.isdigit():
        raise ValueError(f"invalid {name} value '{token}'")
    return int(token)


def _parse_field(text: str, name: str, low: int, high: int, names: Tuple[str, ...]) -> FrozenSet[int]:
    values = set()
    for part in text.split(","):
        range_part, _, step_text = part.partition("/")
        step = 1
        if step_text:
            if not step_text.isdigit() or int(step_text) == 0:
                raise ValueError(f"invalid {name} step '{step_text}'")
            step = int(step_text)
        if range_part == "*":
            start, end = low, high
        elif "-" in range_part:
            first, _, last = range_part.partition("-")
            start, end = _value(first, name, low, names), _value(last, name, low, names)
        else:
            start = _value(range_part, name, low, names)
            end = high if step_text else start
        if not (low <= start <= high and low <= end <= high) or start > end:
            raise ValueError(f"{name} '{range_part}' is outside {low}-{high}")
        values.update(range(start, end + 1, step))
    return frozenset(values)


@dataclass(frozen=True)
class Cron:
    expression: str
    minutes: FrozenSet[int]
    hours: FrozenSet[int]
    days: FrozenSet[int]
    months: FrozenSet[int]
    weekdays: FrozenSet[int]  # 0 = Sunday
    days_restricted: bool
    weekdays_restricted: bool

    @classmethod
    def parse(cls, expression: str) -> "Cron":
        """Parse an expression like '*/30 9-18 * * mon-fri'. Raises ValueError with the reason."""
        text = _MACROS.get(expression.strip().lower(), expression)
        fields = text.split()
        if len(fields) != 5:
            raise ValueError(f"cron expression '{expression}' must have 5 fields (minute hour day month weekday)")
        minutes, hours, days, months, weekdays = (
            _parse_field(field, *spec) for field, spec in zip(fields, _FIELDS)
        )
        return cls(
            expression=expression.strip(),
            minutes=minutes,
            hours=hours,
            days=days,
            months=months,
            weekdays=frozenset(d % 7 for d in weekdays),
            days_restricted=fields[2] != "*",
            weekdays_restricted=fields[4] != "*",
        )

    def _day_matches(self, moment: datetime) -> bool:
        day_ok = moment.day in self.days
        weekday_ok = (moment.weekday() + 1) % 7 in self.weekdays
        if self.days_restricted and self.weekdays_restricted:
            return day_ok or weekday_ok  # standard cron: either field may match
        return day_ok and weekday_ok

    def next_after(self, moment: datetime) -> Optional[datetime]:
        """
        The first matching minute strictly after `moment`, in moment's timezone (wall-clock fields),
        or None if the expression never matches (e.g. February 30).
        """
        tz = moment.tzinfo
        current = moment.replace(tzinfo=None, second=0, microsecond=0) + timedelta(minutes=1)
        limit = current + _SEARCH_LIMIT
        while current < limit:
            if current.month not in self.months:
                year, month = (current.year + 1, 1) if current.month == 12 else (current.year, current.month + 1)
                current = current.replace(year=year, month=month, day=1, hour=0, minute=0)
            elif not self._day_matches(current):
                current = current.replace(hour=0, minute=0) + timedelta(days=1)
            elif current.hour not in self.hours:
                current = current.replace(minute=0) + timedelta(hours=1)
            elif current.minute not in self.minutes:
                current += timedelta(minutes=1)
            else:
                return current.replace(tzinfo=tz)
        return None
