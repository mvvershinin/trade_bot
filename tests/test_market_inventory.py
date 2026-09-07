"""Опись базы: `market/inventory.py`.

Что здесь стережётся, по одной строке на тест
---------------------------------------------
* редкий день не выдаётся за плотный, и наоборот;
* мера плотности берётся **из данных**, а не из выдуманной длины сессии;
* пустые дни внутри ряда попадают в опись, а не выпадают из неё;
* `first_dense` отвечает на вопрос «с какого дня рядом можно торговать»;
* пустая база — фраза, а не пустая таблица.

Сети здесь нет вовсе: опись читает базу и больше ничего.
"""

from __future__ import annotations

import pathlib
from datetime import date, datetime, timedelta

from market_helpers import minute

from market import (
    DENSE_SHARE,
    MSK,
    CandleStore,
    Source,
    observed_session,
    take_inventory,
)

#: Форма настоящих данных по MXU6 (замер 05.09.2026, сверен с ISS построчно):
#: пока контракт дальний — единицы минут в день, после перехода в ближние —
#: около тысячи. Огрублено до трёх дней каждого вида.
FAR = 3
NEAR = 400


def _fill(path: pathlib.Path, plan: dict[date, int], *, symbol: str = "MXU6") -> None:
    """Разложить по дням столько минуток, сколько сказано, и отметить дни."""
    with CandleStore(path) as store:
        for day, count in plan.items():
            start = datetime.combine(day, datetime.min.time(), MSK) + timedelta(hours=10)
            candles = [minute(start + timedelta(minutes=i)) for i in range(count)]
            if candles:
                store.put_minutes(symbol, candles, Source.ISS)
        store.mark_days_requested(
            symbol, list(plan), now=datetime(2026, 7, 1, tzinfo=MSK)
        )


def _series(path: pathlib.Path) -> None:
    """Ряд, повторяющий форму настоящего: дальний контракт, потом ближний."""
    plan = {date(2026, 6, 1) + timedelta(days=i): FAR for i in range(5)}
    plan[date(2026, 6, 6)] = 0          # выходной посреди ряда
    plan[date(2026, 6, 7)] = 0
    plan.update({date(2026, 6, 8) + timedelta(days=i): NEAR for i in range(3)})
    _fill(path, plan)


def test_the_measure_of_a_full_day_comes_from_the_data(tmp_path: pathlib.Path) -> None:
    """Мера полного дня — самый плотный день ряда, а не выдуманная сессия.

    Абсолютного порога здесь нет намеренно: торгового календаря у слоя нет,
    а число минут в сессии биржа не сообщает.
    """
    path = tmp_path / "c.sqlite3"
    _series(path)
    with CandleStore(path) as store:
        inventory = take_inventory(store, "MXU6")
    assert inventory.busiest == NEAR
    # Плотных дней три, все по 400 минуток. Назван самый ранний из равных:
    # он отвечает «с каких пор ряд такой», последний из равных — ни на что.
    assert inventory.busiest_day == date(2026, 6, 8)


def test_a_thin_day_is_not_counted_as_dense(tmp_path: pathlib.Path) -> None:
    """Три минутки в дне — это редкий день, и он назван редким.

    Ровно тот случай, который однажды приняли за недокачанную историю:
    данные целые, их просто мало, потому что контракт был дальним.
    """
    path = tmp_path / "c.sqlite3"
    _series(path)
    with CandleStore(path) as store:
        inventory = take_inventory(store, "MXU6")
    assert len(inventory.dense) == 3
    assert len(inventory.sparse) == 5
    assert len(inventory.empty) == 2
    assert {day.day for day in inventory.sparse} == {
        date(2026, 6, 1) + timedelta(days=i) for i in range(5)
    }


def test_the_dense_edge_is_the_declared_share_not_a_hidden_number(
    tmp_path: pathlib.Path,
) -> None:
    """Граница плотного дня — ровно `DENSE_SHARE` от самого плотного."""
    edge = int(NEAR * DENSE_SHARE)
    path = tmp_path / "c.sqlite3"
    _fill(path, {
        date(2026, 6, 1): NEAR,
        date(2026, 6, 2): edge,        # ровно на границе — плотный
        date(2026, 6, 3): edge - 1,    # на минутку ниже — редкий
    })
    with CandleStore(path) as store:
        inventory = take_inventory(store, "MXU6")
    assert {day.day for day in inventory.dense} == {date(2026, 6, 1), date(2026, 6, 2)}
    assert {day.day for day in inventory.sparse} == {date(2026, 6, 3)}


def test_empty_days_inside_the_series_stay_in_the_picture(
    tmp_path: pathlib.Path,
) -> None:
    """День без единой сделки — самостоятельный факт, а не пропуск в описи.

    Опись строится сплошным календарём: выкинув такие дни, мы показали бы
    историю плотнее, чем она есть.
    """
    path = tmp_path / "c.sqlite3"
    _series(path)
    with CandleStore(path) as store:
        inventory = take_inventory(store, "MXU6")
    assert len(inventory.days) == 10, "ряд обязан идти сплошным календарём"
    assert {day.day for day in inventory.empty} == {date(2026, 6, 6), date(2026, 6, 7)}


def test_the_first_dense_day_answers_when_trading_becomes_possible(
    tmp_path: pathlib.Path,
) -> None:
    """`first_dense` — прямой ответ на «почему видно только с такого-то числа»."""
    path = tmp_path / "c.sqlite3"
    _series(path)
    with CandleStore(path) as store:
        inventory = take_inventory(store, "MXU6")
    assert inventory.first_dense == date(2026, 6, 8)
    assert inventory.first == date(2026, 6, 1), "начало ряда и начало торговли — разное"


def test_a_series_without_any_dense_day_says_so(tmp_path: pathlib.Path) -> None:
    """Все дни редкие — торговать нельзя, и это сказано, а не показано нулём.

    ⚠️ Мера относительная, поэтому самый плотный день ряда плотный **всегда**:
    он равен сам себе. Значит `first_dense` пуст только у пустого ряда, и
    отдельный случай «редко всё» ловится сравнением с настоящей сессией,
    которого у слоя нет. Тест закрепляет это ограничение вслух.
    """
    path = tmp_path / "c.sqlite3"
    _fill(path, {date(2026, 6, 1) + timedelta(days=i): FAR for i in range(3)})
    with CandleStore(path) as store:
        inventory = take_inventory(store, "MXU6")
    assert inventory.first_dense == date(2026, 6, 1)
    assert inventory.busiest == FAR


def test_an_empty_base_gives_an_empty_inventory_not_a_crash(
    tmp_path: pathlib.Path,
) -> None:
    """По незагруженному инструменту опись пуста и не падает на делении."""
    path = tmp_path / "c.sqlite3"
    _series(path)
    with CandleStore(path) as store:
        inventory = take_inventory(store, "MXZ6")
    assert inventory.days == ()
    assert inventory.busiest == 0
    assert inventory.first is None and inventory.first_dense is None
    assert inventory.minutes == 0


def test_the_months_add_up_to_the_whole_series(tmp_path: pathlib.Path) -> None:
    """Помесячная таблица — тот же ряд, просто свёрнутый: суммы обязаны сойтись."""
    path = tmp_path / "c.sqlite3"
    _series(path)
    with CandleStore(path) as store:
        inventory = take_inventory(store, "MXU6")
    months = inventory.by_month()
    assert sum(row.days for row in months) == len(inventory.days)
    assert sum(row.dense for row in months) == len(inventory.dense)
    assert sum(row.sparse for row in months) == len(inventory.sparse)
    assert sum(row.empty for row in months) == len(inventory.empty)


def test_days_still_to_be_requested_are_visible(tmp_path: pathlib.Path) -> None:
    """Дни, которые загрузка перезапросит, видно в описи, а не только в базе."""
    path = tmp_path / "c.sqlite3"
    _series(path)
    with CandleStore(path) as store:
        # День внутри ряда, за который не спрашивали вовсе.
        store.put_minutes(
            "MXU6",
            [minute(datetime(2026, 6, 12, 10, 0, tzinfo=MSK))],
            Source.ISS,
        )
        inventory = take_inventory(store, "MXU6")
    assert date(2026, 6, 11) in inventory.to_request
    assert date(2026, 6, 8) not in inventory.to_request


def test_one_day_can_be_asked_about_without_building_the_whole_series(
    tmp_path: pathlib.Path,
) -> None:
    """Спросить про один день дешевле, чем собрать опись целиком."""
    path = tmp_path / "c.sqlite3"
    _series(path)
    with CandleStore(path) as store:
        assert observed_session(store, "MXU6", date(2026, 6, 8)) == NEAR
        assert observed_session(store, "MXU6", date(2026, 6, 6)) == 0
