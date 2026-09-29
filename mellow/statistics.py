"""Small formatting helpers for timestamps and Russian plurals.

Telegram hands out UTC datetimes; the bot prints them in UTC with an explicit marker so
nobody has to guess which clock a ban expires on.
"""

from __future__ import annotations

from datetime import datetime, timezone


def to_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def format_moment(value: datetime | None) -> str:
    moment = to_utc(value)
    if moment is None:
        return "—"
    return moment.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")


def plural(number: int, one: str, few: str, many: str) -> str:
    """«1 день», «3 дня», «90 дней» — the three Russian forms of a counted noun."""
    value = abs(int(number)) % 100
    if 11 <= value <= 14:
        return many
    value %= 10
    if value == 1:
        return one
    if 2 <= value <= 4:
        return few
    return many


def days_label(number: int) -> str:
    """«день», «дня» or «дней» for a number of days."""
    return plural(number, "день", "дня", "дней")
