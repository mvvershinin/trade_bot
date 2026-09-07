"""Замер доступной глубины истории (Э1-3) — что считает сам замер.

Сеть здесь не нужна: проверяется, что замер по данным базы даёт те числа,
на которых потом будет строиться вывод «есть чем закрыть независимый период
или нет». Сам поход на биржу — отдельный, ручной прогон.
"""

from __future__ import annotations

import pathlib
from datetime import date, timedelta

import pytest

from market.depth import measured_depth
from market.storage import CandleStore, Source

from market_helpers import minute, minutes_from, msk


@pytest.fixture
def store(tmp_path: pathlib.Path):
    with CandleStore(tmp_path / "candles.sqlite3") as opened:
        yield opened


def test_depth_of_empty_database(store: CandleStore) -> None:
    depth = measured_depth(store, "MXU6")
    assert depth.minutes == 0
    assert depth.trading_days == 0
    assert depth.calendar_days == 0
    assert "минутной истории нет" in str(depth)


def test_depth_counts_trading_days_not_calendar_days(store: CandleStore) -> None:
    """Торговых дней меньше календарных — выходные в них не попадают.

    Путать эти два числа нельзя: длина независимого периода считается
    в торговых днях.
    """
    store.put_minutes("MXU6", minutes_from(msk(2026, 8, 24, 10, 0), 5), Source.ISS)
    store.put_minutes("MXU6", minutes_from(msk(2026, 8, 28, 10, 0), 5), Source.ISS)

    depth = measured_depth(store, "MXU6")
    assert depth.trading_days == 2
    assert depth.calendar_days == 5
    assert depth.days == [date(2026, 8, 24), date(2026, 8, 28)]
    assert depth.minutes == 10


def test_depth_boundaries_are_the_real_first_and_last_minute(store: CandleStore) -> None:
    store.put_minutes(
        "MXU6",
        [minute(msk(2025, 9, 6, 10, 18)), minute(msk(2026, 8, 29, 18, 59))],
        Source.ISS,
    )
    depth = measured_depth(store, "MXU6")
    assert depth.first == msk(2025, 9, 6, 10, 18)
    assert depth.last == msk(2026, 8, 29, 18, 59)
    assert depth.calendar_days == 358


def test_thin_history_is_visible_in_the_numbers(store: CandleStore) -> None:
    """Год данных и год торгов — не одно и то же.

    Контракт может числиться год, а торговаться два дня: 299 торговых дней
    при 77 тысячах минуток означают в среднем 257 минут в день вместо ~840.
    Замер обязан показывать оба числа, иначе «истории на год» прочтут как
    «есть на чём проверять».
    """
    days = [msk(2026, 1, 1) + timedelta(days=i) for i in range(10)]
    for day in days:
        store.put_minutes("MXU6", minutes_from(day.replace(hour=10), 2), Source.ISS)

    depth = measured_depth(store, "MXU6")
    assert depth.trading_days == 10
    assert depth.minutes == 20
    assert depth.minutes / depth.trading_days == 2
