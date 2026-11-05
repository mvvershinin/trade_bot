"""Дата закрытого дня — чистая функция `market.day.entries_closed_date` (Р-10 п. 6, Н5).

Слой хранилища уже отсекает команды позже конца дня запросом; сама функция
обязана держать свой договор и без него: по МСК, не позже `day`, самая поздняя.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from market.day import entries_closed_date


def _closes(name: str) -> bool:
    return name.startswith("unfilled-day:")


def test_a_command_just_after_midnight_moscow_is_dated_by_moscow_not_by_utc() -> None:
    """01:30 МСК 28.10 — это 22:30 UTC 27.10, а закрыт день 28.10."""
    commands = [("unfilled-day:long:x", datetime(2026, 10, 27, 22, 30, tzinfo=UTC))]
    assert entries_closed_date(commands, day=date(2026, 10, 28), closes_day=_closes) == date(
        2026, 10, 28
    )


def test_the_latest_closing_command_wins_and_later_days_are_ignored() -> None:
    commands = [
        ("unfilled-day:long:a", datetime(2026, 10, 26, 10, 0, tzinfo=UTC)),
        ("unfilled-day:long:b", datetime(2026, 10, 28, 10, 0, tzinfo=UTC)),
        ("unfilled-day:long:c", datetime(2026, 10, 29, 10, 0, tzinfo=UTC)),
        ("close:long:d", datetime(2026, 10, 28, 11, 0, tzinfo=UTC)),
    ]
    assert entries_closed_date(commands, day=date(2026, 10, 28), closes_day=_closes) == date(
        2026, 10, 28
    )
    assert entries_closed_date(commands, day=date(2026, 10, 27), closes_day=_closes) == date(
        2026, 10, 26
    )
    assert entries_closed_date(commands, day=date(2026, 10, 25), closes_day=_closes) is None
