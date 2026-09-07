"""Подпорки для тестов слоя `market/`.

Отдельный модуль, а не `conftest.py`: `conftest.py` — общий для всего каталога
тестов, и класть в него частности одного слоя значит собирать там всё подряд.
Импортируется напрямую (`from market_helpers import ...`), как и `synthetic.py`.

Главное здесь — подставной транспорт. Тесты не ходят в сеть: разбор ответа,
склейка страниц, политика повторов и вся догрузка проверяются на заранее
заготовленных телах ответов. Тест, который ходит в интернет, падает от чужого
сбоя и зеленеет от чужой удачи — доказывать им нечего.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timedelta

from market.candles import MINUTE, MSK, Candle, Timeframe

__all__ = [
    "ALLOWED_TIMEFRAMES",
    "ISS_COLUMNS",
    "MXU6_ROW",
    "SECURITY_COLUMNS",
    "FakeTransport",
    "iss_body",
    "minute",
    "minutes_from",
    "msk",
    "no_sleep",
    "pages_handler",
    "security_body",
]


def _allowed_timeframes() -> tuple[int, ...]:
    """Все размеры свечи, которые принимает сам валидатор `Timeframe`.

    Перебором, а не списком от руки. Списков «всех допустимых таймфреймов»
    в тестах было два, и они **не совпадали друг с другом**: в одном не было
    4, 6 и 12 минут, в другом — 180, 360 и 480. Ни один не был выведен
    из `Timeframe.__post_init__`, то есть «проверено на всех размерах»
    означало «проверено на тех, что вспомнились».

    Ожидаемый состав держит отдельная проверка в `test_market_candles`:
    здесь список **производный**, и сам по себе он ничего не доказывает.
    """
    accepted = []
    for minutes in range(1, 1441):
        try:
            Timeframe(minutes)
        except ValueError:
            continue
        accepted.append(minutes)
    return tuple(accepted)


#: Размеры свечи, которые слой принимает. Выведены перебором через валидатор.
ALLOWED_TIMEFRAMES = _allowed_timeframes()

ISS_COLUMNS = ["open", "close", "high", "low", "value", "volume", "begin", "end"]


def iss_body(
    candles: Sequence[Candle],
    *,
    columns: Sequence[str] = tuple(ISS_COLUMNS),
    bom: bool = False,
    span_seconds: int = 59,
) -> bytes:
    """Тело ответа ISS с этими свечами. Порядок колонок задаётся снаружи.

    `span_seconds` — на сколько секунд `end` отстоит от `begin`. У минутки
    сервер пишет 59 (`10:05:00` … `10:05:59`), у десятиминутки 599. Разбор
    проверяет размер свечи **по этим данным**, а не по параметру `interval`,
    поэтому подделать ответ покрупнее должно быть можно только здесь.
    """
    by_name = {
        "open": lambda c: c.open,
        "close": lambda c: c.close,
        "high": lambda c: c.high,
        "low": lambda c: c.low,
        "value": lambda c: 0,
        "volume": lambda c: c.volume,
        "begin": lambda c: c.time.strftime("%Y-%m-%d %H:%M:%S"),
        "end": lambda c: (
            c.time + timedelta(seconds=span_seconds)
        ).strftime("%Y-%m-%d %H:%M:%S"),
    }
    payload = {
        "candles": {
            "columns": list(columns),
            "data": [[by_name[name](candle) for name in columns] for candle in candles],
        }
    }
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return b"\xef\xbb\xbf" + raw if bom else raw


class FakeTransport:
    """Транспорт, который отдаёт заранее заготовленное. Сети нет."""

    def __init__(self, handler: Callable[[str], bytes]) -> None:
        self._handler = handler
        self.urls: list[str] = []

    def get(self, url: str, *, timeout: float) -> bytes:
        self.urls.append(url)
        return self._handler(url)


def no_sleep(_seconds: float) -> None:
    """Заглушка пауз: тест повторов не должен длиться семь секунд."""
    return None


def minute(
    moment: datetime,
    *,
    open: float = 100.0,
    high: float | None = None,
    low: float | None = None,
    close: float | None = None,
    volume: float = 1.0,
) -> Candle:
    """Минутная свеча с понятными умолчаниями."""
    return Candle(
        time=moment,
        open=open,
        high=open if high is None else high,
        low=open if low is None else low,
        close=open if close is None else close,
        volume=volume,
        timeframe=MINUTE,
        filled_minutes=1,
    )


def minutes_from(start: datetime, count: int, *, step: int = 1) -> list[Candle]:
    """Подряд идущие минутки от `start`."""
    return [
        minute(start + timedelta(minutes=i * step), open=100.0 + i, volume=i + 1)
        for i in range(count)
    ]


def msk(year: int, month: int, day: int, hour: int = 0, minute_: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute_, tzinfo=MSK)


def pages_handler(pages: Iterable[bytes]) -> Callable[[str], bytes]:
    """Отдавать страницы по порядку обращений, дальше — пусто."""
    queue = list(pages)
    empty = iss_body([])

    def handler(_url: str) -> bytes:
        return queue.pop(0) if queue else empty

    return handler


#: Колонки живого ответа `securities` срочного рынка, в том же порядке.
SECURITY_COLUMNS = (
    "SECID", "BOARDID", "SHORTNAME", "MINSTEP", "LOTVOLUME",
    "INITIALMARGIN", "STEPPRICE", "IMTIME", "BUYSELLFEE", "SCALPERFEE",
)

#: Карточка `MXU6` с биржи, замер 05.09.2026. Числа настоящие и проверены
#: живым запросом дважды — 05.09.2026 днём и вечером, строка в строку.
#:
#: ⚠️ Стоимость пункта здесь ровно 1 ₽, и это **сторож сверки с прототипом**:
#: 127 сделок из 127 посчитаны при единице. Правка чисел в этой строке
#: меняет деньги эталонного прогона.
MXU6_ROW: dict[str, object] = {
    "SECID": "MXU6", "BOARDID": "RFUD", "SHORTNAME": "MIX-9.26",
    "MINSTEP": 25.0, "LOTVOLUME": 1, "INITIALMARGIN": 23124.62,
    "STEPPRICE": 25.0, "IMTIME": "2026-09-04 07:00:01",
    "BUYSELLFEE": 14.68, "SCALPERFEE": 7.34,
}


def security_body(
    *rows: dict[str, object], columns: tuple[str, ...] = SECURITY_COLUMNS
) -> bytes:
    """Тело ответа ISS с карточками инструмента. Порядок колонок — снаружи.

    Общая на два файла (`test_market_iss.py` и `test_market_point.py`),
    и не ради экономии: две копии одной заготовки уже расходились в этом
    проекте — два списка «всех допустимых размеров свечи» не совпадали
    друг с другом, и «проверено на всех» означало «на тех, что вспомнились».
    """
    return json.dumps({
        "securities": {
            "columns": list(columns),
            "data": [[row.get(name) for name in columns] for row in rows],
        }
    }).encode("utf-8")
