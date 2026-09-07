"""Обрыв связи: состояние, счётчик времени, строки журнала.

Заказчик: «обрыв связи — очень важно!!! интернет прерывается часто»
(ТЗ §4.9, `DOMAIN.md` §7). Требования, которые здесь проверяются:

* обрыв виден сразу, идёт счётчик времени без связи;
* всё время обрыва пишется в журнал: когда пропала, когда восстановилась,
  сколько длилась;
* пока связи нет, робот не принимает решений вслепую — и в журнале это
  сказано словами, а не подразумевается.

Шаги 3–5 порядка восстановления (прогон алгоритма, сравнение факта
с расчётом, приведение позиции) — работа движка и этап 2. Здесь их нет.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from broker.connection import ConnectionState, Outage
from broker.errors import NoConnection, ServerFailure, Timeout

START = datetime(2026, 8, 30, 10, 31, 5, tzinfo=timezone.utc)


def at(**shift: float) -> datetime:
    return START + timedelta(**shift)


def test_fresh_state_is_online() -> None:
    state = ConnectionState()
    assert state.online is True
    assert state.offline_for(START) is None
    assert state.describe(START) == "Связь с брокером есть"


def test_outage_opens_with_a_journal_line() -> None:
    state = ConnectionState()
    line = state.went_offline(START, NoConnection("ConnectError"))

    assert state.online is False
    assert line is not None
    assert "Связь с брокером пропала" in line
    assert "не принимает решений" in line
    assert "устаревшим данным" in line


def test_repeated_failures_do_not_spam_the_journal() -> None:
    """Час без интернета не должен состоять из одной записи, повторённой сто раз."""
    state = ConnectionState()
    assert state.went_offline(START, NoConnection()) is not None
    assert state.went_offline(at(seconds=2), NoConnection()) is None
    assert state.went_offline(at(seconds=6), Timeout(10.0)) is None
    assert state.attempts == 3
    assert len(state.history) == 0, "обрыв ещё не кончился"


def test_recovery_reports_how_long_it_lasted() -> None:
    state = ConnectionState()
    state.went_offline(START, NoConnection())
    line = state.came_online(at(seconds=155))

    assert state.online is True
    assert line is not None
    assert "восстановлена" in line
    assert "2 минуты 35 секунд" in line, line
    assert "Пропущенные свечи будут догружены" in line
    assert "фактическая позиция" in line


def test_outage_is_kept_in_history() -> None:
    """ТЗ §4.9: всё время обрыва пишется в журнал."""
    state = ConnectionState()
    state.went_offline(START, ServerFailure())
    state.came_online(at(minutes=3))
    state.went_offline(at(hours=1), NoConnection())
    state.came_online(at(hours=1, minutes=1))

    assert len(state.history) == 2
    assert state.history[0].duration(at(hours=2)) == timedelta(minutes=3)
    assert state.history[1].duration(at(hours=2)) == timedelta(minutes=1)


def test_counter_runs_while_offline() -> None:
    state = ConnectionState()
    state.went_offline(START, NoConnection())
    assert state.offline_for(at(seconds=40)) == timedelta(seconds=40)
    assert "40 секунд" in state.describe(at(seconds=40))
    assert state.describe(at(seconds=40)).startswith("Нет связи")


def test_recovery_without_an_outage_is_quiet() -> None:
    """Успешный запрос при живой связи журнал не засоряет."""
    state = ConnectionState()
    assert state.came_online(START) is None


def test_attempt_counter_resets_on_recovery() -> None:
    state = ConnectionState()
    state.went_offline(START, NoConnection())
    state.went_offline(at(seconds=1), NoConnection())
    assert state.attempts == 2
    state.came_online(at(seconds=5))
    assert state.attempts == 0


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (1, "1 секунда"),
        (2, "2 секунды"),
        (5, "5 секунд"),
        (11, "11 секунд"),
        (61, "1 минута 1 секунда"),
        (120, "2 минуты"),
        (3600, "1 час"),
        (3725, "1 час 2 минуты 5 секунд"),
        (0, "0 секунд"),
    ],
)
def test_duration_is_written_the_way_a_person_says_it(seconds: int, expected: str) -> None:
    """«2 минуты 35 секунд», а не «155 s»: строку читает владелец счёта."""
    state = ConnectionState()
    state.went_offline(START, NoConnection())
    line = state.came_online(at(seconds=seconds))
    assert line is not None
    assert f"Без связи: {expected}." in line, line


def test_outage_reason_is_the_human_text() -> None:
    """В журнале причина названа словами, а не кодом ошибки."""
    state = ConnectionState()
    state.went_offline(START, NoConnection("ConnectError"))
    outage: Outage = state.current  # type: ignore[assignment]
    assert "ConnectError" not in outage.reason
    assert "Нет связи с брокером" in outage.reason
