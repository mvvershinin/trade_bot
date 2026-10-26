"""Минутки под барами прогона — запрос к базе одним местом.

Решение 0059 §3: скользящий уровень едет внутри пятиминутки по минуткам,
и получает их **только исполнитель** прогона (`backtest.replay(minutes=)`).
Источник свечей и движок их не видят. Здесь нет ни одного правила обхода —
только «какие минутки и в каком порядке их цены считать пройденными»;
порядок — допущение прогона из окна (`convert.minute_order_of`).

И одно правило подачи (B-058, решение владельца счёта 03.10.2026):
минутки подаются, только если включён скользящий уровень **и** свеча
не крупнее порога из настроек (`convert.minute_bar_limit_of`). Неподвижному
тейку минутки сделок не меняют (замерено, `test_backtest_minute_walk`),
а на крупной свече прогон упирается в предел кругов бара. Правило —
одной функцией `minute_plan` на оба входа: окно и склейку.

Два входа, одна выборка: склейка читает базу сама в потоке данных
(`app/stitched.py::load_pieces`), прогон окна — через фасад потока
(`app/port.py::_replay`). Вторая копия запроса разошлась бы с первой
по границам, и куски склейки проверялись бы по другим минуткам, чем
тот же контракт без склейки.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from app import convert
from backtest import CoarseBar, HistoryRun, MinuteOrder, Minutes
from market import Candle, MarketWorker, Timeframe
from market.storage import CandleStore
from ui.models import Settings

__all__ = ["MinutePlan", "minute_plan", "minutes_between", "minutes_under"]


@dataclass(frozen=True, slots=True)
class MinutePlan:
    """Подавать ли прогону минутки — и что сказать, если не подаются.

    `order` задан — минутки подаются в этом порядке; `None` — нет, уровни
    сторожатся по размаху свечи, как до минуток. `coarse` задан, только
    когда скользящий уровень включён, а свеча крупнее порога: это и есть
    случай, о котором отчёт обязан сказать (`backtest/assumptions.py`).
    """

    order: MinuteOrder | None
    coarse: CoarseBar | None = None

    def mark(self, run: HistoryRun) -> HistoryRun:
        """Итог прогона с пометкой о не поданных минутках — для допущений."""
        return replace(run, coarse_bar=self.coarse) if self.coarse is not None else run


def minute_plan(values: Settings, timeframe: Timeframe) -> MinutePlan:
    """Решение B-058: минутки — только скользящему уровню и только на малой свече.

    Порог включительно: свеча, равная порогу, минутки получает.
    """
    if not values.trailing_enabled:
        return MinutePlan(None)
    limit = convert.minute_bar_limit_of(values)
    if timeframe.minutes > limit:
        return MinutePlan(None, CoarseBar(timeframe.minutes, limit))
    return MinutePlan(convert.minute_order_of(values))


def minutes_between(
    store: CandleStore, symbol: str, since: datetime, until: datetime, order: MinuteOrder
) -> Minutes:
    """Минутки инструмента в полуинтервале `[since, until)` и порядок обхода.

    Пусто — тоже ответ: минутки поданы, но их нет, и исполнитель посчитает
    каждый такой бар в `bars_without_minutes`, а отчёт скажет об этом вслух.
    """
    return Minutes(store.minutes(symbol, since, until), order)


async def minutes_under(
    worker: MarketWorker, symbol: str, bars: Sequence[Candle], order: MinuteOrder
) -> Minutes:
    """Минутки под рядом баров прогона — от начала первого до закрытия последнего."""
    if not bars:
        return Minutes((), order)
    since, until = bars[0].time, bars[-1].close_time
    return await worker.call(
        lambda store: minutes_between(store, symbol, since, until, order)
    )
