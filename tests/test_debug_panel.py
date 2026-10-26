"""Лента «Отладка» без окна: что в неё попадает, чего нет, и что она не ломает.

Миниплан с поправками гейта — `.docs/plans/debug-panel.md`. Здесь стерегутся:

* события живого пути доходят до ленты: минута пошла и закрылась, бар
  движка с ценами и шагом разбора, слова движка, заявки, исполнения, сделки;
* пересчёт истории даёт одну строку итога, а не строку на каждый бар;
* отладочная строка, которая не собралась, не роняет ни поток котировок,
  ни ход движка — и не молчит: первая — вслух, остальные — счётом;
* токен не проходит в ленту ни одним из путей;
* в файл технического лога строки ленты не попадают, буфер не растёт;
* время строки — московское при любом поясе машины;
* модуль ленты не тянет Qt (ветки без окна живут без него).

Окно — `tests/test_debug_panel_window.py`, порт — `tests/test_debug_panel_port.py`.
Разделение то же, что у `test_app_observe.py`/`test_app_observe_port.py`:
здесь циклы `asyncio.run`, там — цикл поверх Qt.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import math
import os
import pathlib
import subprocess
import sys
import time
from collections import Counter
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from datetime import time as day_time
from types import SimpleNamespace
from typing import Any

import pytest

import backtest
from app import convert, debug_feed, live_feed
from app.debug_feed import PANEL_LOGGER, PanelHandler
from app.live_feed import LiveFeed, Watchers
from app.logs import setup_logging
from app.observe import LiveObserver
from backtest import Costs, HistoryRun
from broker.clock import ClockWatch
from broker.errors import NoConnection
from broker.redaction import MASK, REGISTRY
from broker.stream import CandleSnapshot
from engine import (
    EngineSettings,
    EngineState,
    ExitReason,
    Fill,
    JournalEntry,
    JournalLevel,
    Mode,
    OrderAction,
    Outcome,
    Side,
    Step,
    TradingWindow,
    close_time,
)
from market import (
    MSK,
    Candle,
    DecisionPhase,
    OrderKind,
    OrderRecord,
    Timeframe,
    TradeSide,
)
from strategies import MaReverseAlways, MaReverseAlwaysSettings
from ui.formatting import fmt_level, fmt_money, fmt_time
from ui.models import Connection, DebugLine, DecisionLevel

ROOT = pathlib.Path(__file__).resolve().parent.parent

DAY = datetime(2026, 6, 19, 7, 0, tzinfo=MSK)  # пятница, до открытия окна

#: Не токен, а метка под охраной реестра: настоящий токен в проверки
#: не попадает (правило 7), а реестр вырезает любое взятое под охрану значение.
SECRET = "debug-panel-guarded-marker-7f3a9c1e5b2d"


def bars(count: int, start: datetime = DAY) -> list[Candle]:
    """Пятиминутки с колебанием: средняя пересекается, решения и сделки есть."""
    out: list[Candle] = []
    for index in range(count):
        price = 100000.0 + 300.0 * math.sin(index / 9.0)
        out.append(Candle(
            time=start + timedelta(minutes=index * 5),
            open=price,
            high=price + 40.0,
            low=price - 40.0,
            close=price,
            volume=1.0,
            timeframe=Timeframe(5),
            filled_minutes=5,
        ))
    return out


def settings() -> EngineSettings:
    """Реверс с окном на весь день и без «стопа после тейка»: решения идут весь ход."""
    return EngineSettings(
        mode=Mode.REVERSE,
        window=TradingWindow(start=day_time(0, 0), end=day_time(23, 59)),
        take_profit_percent=0.5,
        stop_after_take_profit=False,
        commission_per_side=14.0,
    )


@pytest.fixture
def panel() -> PanelHandler:
    """Лента, поставленная как в программе. Снимает её `pristine_logging`."""
    return debug_feed.install()


def lines(handler: PanelHandler) -> list[DebugLine]:
    return list(handler.since(0, 1_000_000).lines)


def texts(handler: PanelHandler, *sources: str) -> list[str]:
    return [line.text for line in lines(handler) if not sources or line.source in sources]


def boom(*_args: object, **_kwargs: object) -> None:
    """Сборщик строки, который падает: так выглядит ошибка в построителе ленты."""
    raise RuntimeError("строка ленты сломана нарочно")


# ------------------------------------------------------------- ход движка

#: Куда ход говорит строки журнала: событие, причина, важность.
Say = Callable[[str, str, DecisionLevel], None]


async def _ride(history: int, live: int, say: Say) -> tuple[int, int, HistoryRun, HistoryRun]:
    """Пересчёт `history` баров одним проходом, затем `live` баров по одному.

    Возврат: заявок подано к концу пересчёта и всего, снимок после пересчёта
    и в конце хода.
    """
    observer = LiveObserver(
        strategy=MaReverseAlways(MaReverseAlwaysSettings()),
        settings=settings(),
        say=say,
        costs=Costs(commission_per_side=14.0),
    )
    series = bars(history + live)
    recounted = await observer.feed(series[:history])
    early = observer.submitted
    run = recounted
    for count in range(history + 1, history + live + 1):
        run = await observer.feed(series[:count])
    await observer.aclose()
    return early, observer.submitted, recounted, run


def ride(history: int, live: int, say: Say = debug_feed.note):
    """Ход движка. `say` по умолчанию — тот же выход в ленту, что у `HistoryPort.note`."""
    return asyncio.run(_ride(history, live, say))


def test_the_recount_gives_one_summary_line_not_a_line_per_bar(panel) -> None:
    """Пересчёт 1500 баров с заявками и сделками — в ленте строка итога и строка прогрева.

    Мутации, которые это роняет: снять проверку фазы в `_walk` (строка на бар)
    или в `_note_orders` (строки заявок пересчёта). Строка прогрева
    (`_say_warmup`) идёт через журнал программы и тоже считается.
    """
    submitted, _, recounted, _ = ride(1500, 0)
    assert submitted > 0 and recounted.summary.trades > 0, (
        "в пересчёте нет заявок — проверка фазы на строках заявок не проверена бы"
    )
    shown = texts(panel)
    assert len(shown) == 2, f"пересчёт дал в ленту {len(shown)} строк: {shown[:5]}"
    warmup, summary = shown
    assert warmup.startswith("Движок работает на живом ряду — ")
    assert summary.startswith("пересчёт по базе окончен: 1500 баров")
    assert f"сделок {recounted.summary.trades}," in summary
    assert f"заявок {submitted}," in summary


def test_live_bars_reach_the_panel_with_prices_step_and_engine_words(panel) -> None:
    """Каждый живой бар — строка с ценами свечи движка, фазой и шагом разбора.

    Слова движка этого бара (журнал) идут следом. Цены берутся из свечи,
    которую получил движок, — других баров здесь нет.
    """
    _, _, recounted, run = ride(120, 60)
    series = bars(180)
    shown = texts(panel, debug_feed.SOURCE_ENGINE)
    for candle in series[120:]:
        head = f"бар {fmt_time(candle.time)}–"
        found = [text for text in shown if text.startswith(head)]
        assert len(found) == 1, f"бар {candle.time:%H:%M} в ленте {len(found)} раз"
        assert ", вживую: " in found[0]
        assert f"З {fmt_level(candle.close)}" in found[0]
        assert "разбор дошёл до шага" in found[0]
    said = run.journal[len(recounted.journal):]
    assert said, "живые бары без единой строки движка — слова не проверены"
    # Каждое слово движка — ровно столько раз, сколько оно сказано: ни потерь,
    # ни повторов (курсор `_Tally.seen_words` двигается на каждом проходе).
    words = Counter(
        text for text in shown
        if not text.startswith(("бар ", "пересчёт по базе окончен", "живой ход движка"))
    )
    assert words == Counter(f"{entry.event} — {entry.reason}" for entry in said), (
        "слова движка в ленте не по одному разу"
    )
    summaries = [text for text in shown if text.startswith("пересчёт по базе окончен")]
    assert len(summaries) == 1, f"строка итога пересчёта — {len(summaries)} раз за ход"


WINDOW_START, WINDOW_END = day_time(7, 0), day_time(17, 0)


def test_a_live_bar_line_carries_the_engines_prices_average_window_and_step(panel) -> None:
    """Строка бара: все четыре цены свечи движка, его средняя, окно и шаг разбора.

    Окно 07:00–17:00, живые бары 15:00–19:00 — часть в окне, часть вне.
    Окно и шаг ожидаются по часам закрытия свечи, а не по ответу движка:
    вне окна разбор кончается шагом окна, в окне «Реверса» без стопа после
    тейка — логикой открытия (PROTOTYPE.md §2). Бар на самой границе окна
    не судится: границу стерегут проверки движка. Открытие отлично от
    закрытия, а верх от низа — подмена одной цены другой видна.
    """
    series = [dataclasses.replace(candle, open=candle.close - 15.0) for candle in bars(144)]
    window = TradingWindow(start=WINDOW_START, end=WINDOW_END)

    async def go() -> HistoryRun:
        observer = LiveObserver(
            strategy=MaReverseAlways(MaReverseAlwaysSettings()),
            settings=dataclasses.replace(settings(), window=window),
            say=lambda *_: None,
            costs=Costs(commission_per_side=14.0),
        )
        run = await observer.feed(series[:96])
        for count in range(97, 145):
            run = await observer.feed(series[:count])
        await observer.aclose()
        return run

    run = asyncio.run(go())
    averages = dict(run.average)
    shown = texts(panel, debug_feed.SOURCE_ENGINE)
    sides = {True: 0, False: 0}
    for candle in series[96:]:
        shut = close_time(candle)
        [line] = [text for text in shown if text.startswith(f"бар {fmt_time(candle.time)}–")]
        assert (
            f"О {fmt_level(candle.open)} В {fmt_level(candle.high)} "
            f"Н {fmt_level(candle.low)} З {fmt_level(candle.close)};"
        ) in line, line
        assert f"; средняя {fmt_level(averages[shut])};" in line, line
        if shut.time() == WINDOW_END:
            continue
        inside = WINDOW_START < shut.time() < WINDOW_END
        step = Step.OPEN if inside else Step.WINDOW
        sides[inside] += 1
        assert f"; {'в окне' if inside else 'вне окна'}; " in line, line
        assert line.endswith(
            f"разбор дошёл до шага {int(step)} — {debug_feed.STEP_WORDS[step]}"
        ), line
    assert all(sides.values()), f"нет баров по одну из сторон окна: {sides}"


def test_orders_fills_and_deals_of_the_live_run_reach_the_panel(panel) -> None:
    """Заявки, исполнения и закрытые сделки живых баров — по строке на каждое.

    Исполнения лежат у исполнителя отдельно от заявок (`executor.fills`),
    и без своего курсора лента молчала бы о главном в реверсной системе:
    сделки — это исполнения рыночных заявок на открытии следующего бара.
    """
    early, submitted, recounted, run = ride(120, 60)
    shown = texts(panel, debug_feed.SOURCE_ORDERS)
    fills = run.fills[len(recounted.fills):]
    deals = run.deals[len(recounted.deals):]
    assert fills and deals, "в живой части нет исполнений — проверка пуста"
    filled = [text for text in shown if text.startswith("исполнено: ")]
    assert len(filled) == len(fills)
    for fill, text in zip(fills, filled, strict=True):
        assert f"по {fmt_level(fill.price)} в {fmt_time(fill.at)}" in text
    closed = [text for text in shown if text.startswith("сделка закрыта")]
    assert len(closed) == len(deals)
    for deal, text in zip(deals, closed, strict=True):
        # Результат сделки — чистыми, за вычетом ненулевой комиссии (правило 4).
        assert deal.net is not None and deal.commission and deal.net != deal.gross
        assert text.endswith(f"чистыми {fmt_money(deal.net, sign=True)}"), text
    orders = [
        text for text in shown
        if not text.startswith(("исполнено: ", "сделка закрыта", "задет уровень"))
    ]
    assert len(orders) == submitted - early > 0


def _observer() -> LiveObserver:
    return LiveObserver(
        strategy=MaReverseAlways(MaReverseAlwaysSettings()),
        settings=settings(),
        say=lambda *_: None,
        costs=Costs(commission_per_side=14.0),
    )


def _since(handler: PanelHandler, seen: int) -> tuple[list[DebugLine], int]:
    """Строки ленты после `seen` и номер последней из них."""
    fresh = list(handler.since(seen, 1_000_000).lines)
    return fresh, fresh[-1].seq if fresh else seen


def _bar_head(candle: Candle) -> str:
    return f"бар {fmt_time(candle.time)}–"


def test_a_bar_reads_in_the_order_of_its_events(panel) -> None:
    """Исполнение на открытии бара — раньше строки бара, строка бара — раньше решения.

    Находка 12.10.2026 по снимку панели: бар 11:40 шёл «строка бара → слова
    движка о входе в лонг (закрытие 11:45) → исполнено: вход в шорт в 11:40»,
    и читалось, будто решили в лонг, а исполнилось в шорт. В пределах одного
    прохода: исполнения и сделки на открытии → строка бара → слова движка
    на закрытии → новые заявки.
    """
    series = bars(180)

    async def go() -> tuple[list[tuple[Candle, list[DebugLine], list[JournalEntry]]], HistoryRun]:
        observer = _observer()
        run = await observer.feed(series[:120])
        _, seen = _since(panel, 0)
        passes: list[tuple[Candle, list[DebugLine], list[JournalEntry]]] = []
        for count in range(121, 181):
            said = len(run.journal)
            run = await observer.feed(series[:count])
            fresh, seen = _since(panel, seen)
            passes.append((series[count - 1], fresh, list(run.journal[said:])))
        await observer.aclose()
        return passes, run

    passes, run = asyncio.run(go())
    checked = 0
    for candle, fresh, said in passes:
        shown = [line.text for line in fresh]
        [head] = [row for row, text in enumerate(shown) if text.startswith(_bar_head(candle))]
        fills = [row for row, text in enumerate(shown) if text.startswith("исполнено: ")]
        deals = [row for row, text in enumerate(shown) if text.startswith("сделка закрыта")]
        assert len(fills) == len([fill for fill in run.fills if fill.at == candle.time])
        assert all(row < head for row in fills + deals), (
            f"бар {candle.time:%H:%M}: исполнение на открытии стоит после строки бара:\n"
            + "\n".join(shown)
        )
        # Слова этого прохода, сказанные на закрытии бара, — решение по нему.
        closing = {
            f"{entry.event} — {entry.reason}" for entry in said if entry.at == close_time(candle)
        }
        words = [
            row for row, line in enumerate(fresh)
            if line.source == debug_feed.SOURCE_ENGINE and line.text in closing
        ]
        assert len(words) == len(closing) and all(row > head for row in words), (
            f"бар {candle.time:%H:%M}: решение на закрытии стоит до строки бара:\n"
            + "\n".join(shown)
        )
        orders = [
            row for row, line in enumerate(fresh)
            if row > head and line.source == debug_feed.SOURCE_ORDERS
        ]
        assert all(row > max(words, default=head) for row in orders), (
            f"бар {candle.time:%H:%M}: новая заявка раньше слов движка:\n" + "\n".join(shown)
        )
        checked += bool(fills and words and orders)
    assert checked >= 3, f"баров с исполнением, решением и заявкой — {checked}: проверка пуста"


def test_a_fill_at_the_first_minute_reaches_the_panel_before_the_bar_closes(panel) -> None:
    """Исполнение по первой минуте бара (решение 0065) — в ленте сразу, а не на закрытии.

    Ход исполняет заявку по открытию незакрытого бара за минуты до его
    закрытия. Лента обязана сказать об этом тогда же — со словами движка
    об исполнении — и не повторить исполнение на закрытии. Строки бара
    до закрытия нет: бара ещё нет.
    """
    series = bars(220)

    async def go() -> tuple[Candle, list[DebugLine], list[DebugLine], HistoryRun, list[str]]:
        observer = _observer()
        run = await observer.feed(series[:120])
        count = 120
        while not run.waiting:
            count += 1
            assert count < len(series), "в ряду не нашлось ждущей заявки"
            run = await observer.feed(series[:count])
        _, seen = _since(panel, 0)
        bar = series[count]
        before = len(run.journal)
        shown = await observer.feed(series[:count], dataclasses.replace(
            bar, high=bar.open, low=bar.open, close=bar.open,
            filled_minutes=1, unsettled=True,
        ))
        said = [f"{entry.event} — {entry.reason}" for entry in shown.journal[before:]]
        early, seen = _since(panel, seen)
        await observer.feed(series[: count + 1])
        late, _ = _since(panel, seen)
        run = observer.run
        await observer.aclose()
        return bar, early, late, run, said

    bar, early, late, run, said = asyncio.run(go())
    fills = [fill for fill in run.fills if fill.at == bar.time]
    assert fills, "по первой минуте ничего не исполнилось — проверка пуста"
    shown = [line.text for line in early if line.text.startswith("исполнено: ")]
    assert len(shown) == len(fills), (
        "исполнение по первой минуте не дошло до ленты до закрытия бара:\n"
        + "\n".join(line.text for line in early)
    )
    for fill, text in zip(fills, shown, strict=True):
        assert f"по {fmt_level(fill.price)} в {fmt_time(fill.at)}" in text
    assert said and all(text in [line.text for line in early] for text in said), (
        "слова движка об исполнении по первой минуте не дошли до ленты"
    )
    assert not [line for line in early if line.text.startswith(_bar_head(bar))]
    assert not [line for line in late if line.text.startswith("исполнено: ")], (
        "исполнение по первой минуте повторено в ленте на закрытии бара"
    )
    assert [line for line in late if line.text.startswith(_bar_head(bar))]


def test_a_level_touch_stands_before_the_fill_it_caused() -> None:
    """Тейк внутри бара: касание → исполнение по нему → сделка → слова движка → бар.

    У строки касания для базы время — закрытие бара, у исполнения — его
    начало: по одному времени касание встало бы после собственной сделки.
    Строки поданы в ленту вперемешку — порядок обязана навести она сама.
    """
    handler = debug_feed.install()
    candle = bars(1, DAY + timedelta(hours=4))[0]
    shut = close_time(candle)
    take = backtest.Plan(
        order_id="take-1", decided_at=shut, price=candle.high, side=Side.LONG,
        action=OrderAction.CLOSE, exit_reason=ExitReason.TAKE_PROFIT,
    )
    fill = Fill(
        action=OrderAction.CLOSE, side=Side.LONG, volume=1.0, price=candle.high,
        at=candle.time, order_id="take-1", commission=14.0,
    )
    deal = backtest.Deal(
        side=Side.LONG, volume=1.0, entry_time=candle.time - timedelta(minutes=30),
        entry_price=candle.low, entry_order_id="open-0", exit_time=candle.time,
        exit_price=candle.high, exit_order_id="take-1", exit_reason=ExitReason.TAKE_PROFIT,
        commission=28.0,
    )
    closed = JournalEntry(at=candle.time, event="Лонг закрыт", reason="по тейку")
    decided = JournalEntry(at=shut, event="Вход в шорт", reason="ниже средней")
    order = OrderRecord(
        order_id="open-1", at=shut, kind=OrderKind.SUBMITTED, action="open",
        side=TradeSide.SHORT, volume=1.0, close_price=candle.close,
    )
    debug_feed.happened(
        fills=[fill], deals=[deal], words=[closed, decided],
        records=[order, convert.level_hit_record(
            take, volume=1.0, close=candle.close, average=None, phase=DecisionPhase.LIVE,
        )],
        bar=debug_feed.BarSeen(
            candle, Outcome(state=EngineState(), steps=(Step.OPEN,), in_window=True),
            candle.close, DecisionPhase.LIVE,
        ),
    )
    shown = [line.text for line in lines(handler)]
    starts = (
        "задет уровень", "исполнено: выход из лонга", "сделка закрыта", "Лонг закрыт — ",
        _bar_head(candle), "Вход в шорт — ", "заявка на вход в шорт",
    )
    assert len(shown) == len(starts), shown
    for text, start in zip(shown, starts, strict=True):
        assert text.startswith(start), "\n".join(shown)


def test_a_broken_panel_line_does_not_stop_the_live_run(monkeypatch, caplog, panel) -> None:
    """Сломанная строка ленты: ход идёт до конца, сделки те же, отказ сказан один раз.

    Мутация «снять защиту» (`_guarded`) роняет: исключение из `_walk`
    превращается в «Живой ход прекращён», и ход останавливается.
    """
    *_, honest = ride(120, 30, lambda *_: None)
    monkeypatch.setattr(debug_feed, "_emit", boom)
    said: list[tuple[str, str, DecisionLevel]] = []
    *_, broken = ride(120, 30, lambda *row: said.append(row))
    assert broken.halted == ""
    assert broken.bars == 150
    assert broken.deals == honest.deals, "лента изменила поведение хода"
    assert "Живой ход прекращён" not in [event for event, _, _ in said]
    assert debug_feed.LOSSES.count >= 30
    told = [r for r in caplog.records if "строка ленты «Отладка» не собрана" in r.getMessage()]
    assert len(told) == 1, "первая потеря — одной строкой, дальше счёт"
    assert told[0].levelno == logging.WARNING
    assert panel.since(0, 10).lost == debug_feed.LOSSES.count


# ------------------------------------------------------------- поток котировок


TICKER = "MXU6"
MINUTE_A = datetime(2026, 9, 4, 5, 38, tzinfo=UTC)  # 08:38 МСК
MINUTE_B = MINUTE_A + timedelta(minutes=1)
#: Открытие минуты A: одно на все её снимки и не равное ни одному закрытию.
MINUTE_A_OPEN = 224_870.0


def snapshot(
    minute: datetime, close: float, turnover: float, opening: float | None = None
) -> CandleSnapshot:
    """Снимок минуты; `opening` — открытие, отличное от закрытия.

    Без него строка, подставившая одну цену вместо другой, читалась бы верной.
    """
    first = close if opening is None else opening
    return CandleSnapshot(
        ticker=TICKER, class_code="SPBFUT", timeframe="M1", opened_at=minute,
        open=first, high=max(first, close) + 50.0, low=min(first, close) - 50.0,
        close=close, turnover=turnover,
    )


class _Socket:
    """Сокет из готовых снимков: отдаёт их и молчит до отмены, как живой."""

    def __init__(self, snapshots: list[CandleSnapshot]) -> None:
        self._left = list(snapshots)
        self.delivered = 0

    def __aiter__(self) -> _Socket:
        return self

    async def __anext__(self) -> CandleSnapshot:
        if not self._left:
            await asyncio.Event().wait()
        self.delivered += 1
        return self._left.pop(0)


class _Stream:
    """Подмена `candle_stream`: заходы по очереди — сокет либо отказ на входе."""

    def __init__(self, *sessions: _Socket | BaseException) -> None:
        self._sessions = list(sessions)

    @asynccontextmanager
    async def __call__(
        self, session: object, *, ticker: str, class_code: str, timeframe: str = "M1"
    ) -> AsyncIterator[_Socket]:
        outcome = self._sessions.pop(0) if self._sessions else _Socket([])
        if isinstance(outcome, BaseException):
            raise outcome
        yield outcome


class _Worker:
    """Поток данных без базы: принимает записанные минуты списком."""

    def __init__(self) -> None:
        self.opened = False
        self.written: list[Candle] = []

    async def open(self) -> None:
        self.opened = True

    async def put_minutes(self, ticker: str, minutes: list[Candle], source: object) -> None:
        self.written.extend(minutes)


class _Heard:
    """Что поток сказал наружу. `say` идёт в ленту, как у порта (`note`)."""

    def __init__(self) -> None:
        self.closed: list[str] = []
        self.connections: list[Connection] = []
        self.said: list[str] = []

    def watchers(self) -> Watchers:
        def say(event: str, reason: str, level: DecisionLevel) -> None:
            self.said.append(f"{event} — {reason}")
            debug_feed.note(event, reason, level)

        return Watchers(
            connection=self.connections.append,
            closed_minute=self.closed.append,
            growing_minute=lambda *_: None,
            say=say,
            connected=lambda _minute: None,
            halt=lambda *_: None,
        )


def stream_of(
    monkeypatch, *sessions: _Socket | BaseException
) -> tuple[LiveFeed, _Worker, _Heard]:
    monkeypatch.setattr(live_feed, "candle_stream", _Stream(*sessions))
    monkeypatch.setattr(live_feed, "snapshot_of", lambda raw: raw)
    monkeypatch.setattr(live_feed, "RETRY_FIRST", 0.0)
    monkeypatch.setattr(live_feed, "RETRY_CAP", 0.0)
    worker, heard = _Worker(), _Heard()
    session: Any = SimpleNamespace(clock=ClockWatch())
    data: Any = worker  # дублёр потока данных: поток просит у него три вещи
    feed = LiveFeed(
        session, data, ticker=TICKER, class_code="SPBFUT", watchers=heard.watchers()
    )
    return feed, worker, heard


async def until(condition: Callable[[], bool], what: str) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 3.0
    while not condition():
        assert loop.time() < deadline, f"не дождались: {what}"
        await asyncio.sleep(0.001)


def run_two_minutes(monkeypatch) -> tuple[_Worker, _Heard]:
    """Три снимка минуты A и один минуты B: минута A закрыта, B пошла."""
    socket = _Socket([
        snapshot(MINUTE_A, 224_900.0, 224_900.0, MINUTE_A_OPEN),
        snapshot(MINUTE_A, 224_850.0, 2 * 224_850.0, MINUTE_A_OPEN),
        snapshot(MINUTE_A, 224_800.0, 3 * 224_800.0, MINUTE_A_OPEN),
        snapshot(MINUTE_B, 224_750.0, 224_750.0),
    ])
    feed, worker, heard = stream_of(monkeypatch, socket)

    async def scenario() -> None:
        feed.start()
        try:
            await until(lambda: heard.closed == [TICKER], "запись минуты A")
            await until(lambda: socket.delivered == 4, "снимок минуты B")
            await asyncio.sleep(0.01)
        finally:
            feed.request_stop()
            await feed.aclose()

    asyncio.run(scenario())
    return worker, heard


def test_stream_minutes_reach_the_panel_once_each(monkeypatch, panel) -> None:
    """Минута пошла — одна строка на первом снимке, а не на каждом; закрыта — с ценами.

    Строка техлога «оборот → контракты» в ленту не идёт: её числа уже есть
    в строке ленты (`extra={"panel": False}`).
    """
    logging.getLogger().setLevel(logging.INFO)  # вернёт `pristine_logging`
    run_two_minutes(monkeypatch)
    shown = texts(panel, debug_feed.SOURCE_STREAM)
    assert shown == [
        f"{TICKER}: минута 08:38 пошла, первый снимок — цена {fmt_level(224_900.0)}",
        f"{TICKER}: минута 08:38 закрыта и записана в базу — О {fmt_level(MINUTE_A_OPEN)} "
        f"В {fmt_level(224_920.0)} Н {fmt_level(224_750.0)} З {fmt_level(224_800.0)}, 3 контр.",
        f"{TICKER}: минута 08:39 пошла, первый снимок — цена {fmt_level(224_750.0)}",
    ]
    assert not [text for text in texts(panel) if "оборот" in text], "близнец строки ленты"


def test_a_broken_panel_line_does_not_take_the_quote_stream_down(monkeypatch, panel) -> None:
    """Сломанная строка ленты в `_accept` и `_close_minute`: минута записана, поток жив.

    Мутация «снять защиту» роняет: исключение уходит в `LiveFeed._ride`,
    поток снимается до ручного «Подключиться» (`Connection.OFFLINE`).
    """
    monkeypatch.setattr(debug_feed, "_emit", boom)
    worker, heard = run_two_minutes(monkeypatch)
    assert [minute.close for minute in worker.written] == [224_800.0]
    assert heard.closed == [TICKER]
    assert not [line for line in heard.said if "Ошибка программы" in line]
    assert not [line for line in heard.said if "Поток котировок снят" in line]
    assert debug_feed.LOSSES.count >= 3, "минута A пошла и закрыта, минута B пошла"


def test_a_broken_stream_says_its_reason_once_in_the_panel(monkeypatch, panel) -> None:
    """Обрыв: в ленте одна строка о нём (журнал), а не две (журнал и техлог).

    Мутация — снять `extra={"panel": False}` у `LiveFeed._complain`: строка
    техлога уровня WARNING встаёт в ленту рядом с той же причиной.
    """
    socket = _Socket([snapshot(MINUTE_A, 224_900.0, 224_900.0)])
    feed, _, heard = stream_of(monkeypatch, NoConnection("connect refused"), socket)

    async def scenario() -> None:
        feed.start()
        try:
            await until(lambda: socket.delivered == 1, "снимок после обрыва")
        finally:
            feed.request_stop()
            await feed.aclose()

    asyncio.run(scenario())
    about = [line for line in lines(panel) if "Нет связи с брокером" in line.text]
    assert len(about) == 1, [line.text for line in about]
    assert about[0].level is DecisionLevel.WARNING
    assert about[0].source == debug_feed.SOURCE_JOURNAL


# ------------------------------------------------------------- обработчик


def test_panel_lines_never_reach_the_log_file(tmp_path: pathlib.Path) -> None:
    """Тысяча строк ленты — ноль строк в `terminal.log`; строка техлога — там и там.

    Мутация `propagate=True` на `app.panel` роняет: строки уходят к корню,
    то есть в файл, и съедают его ротацию.
    """
    setup = setup_logging(tmp_path / "logs")
    handler = debug_feed.install()
    for number in range(1000):
        debug_feed.tell(debug_feed.SOURCE_ENGINE, "строка ленты %d", number)
    logging.getLogger("app.port").info("строка техлога")
    for each in logging.getLogger().handlers:
        each.flush()
    assert setup.path is not None
    written = setup.path.read_text(encoding="utf-8")
    assert "строка техлога" in written
    assert "строка ленты" not in written
    assert len(texts(handler, debug_feed.SOURCE_ENGINE)) == 1000
    assert "строка техлога" in texts(handler)


def test_installing_twice_gives_one_line_per_record() -> None:
    """`setup_logging` зовётся дважды, `install` — тоже: дублей строк нет."""
    first = debug_feed.install()
    second = debug_feed.install()
    assert first is second
    assert [type(h) for h in logging.getLogger().handlers].count(PanelHandler) == 1
    debug_feed.tell(debug_feed.SOURCE_ENGINE, "одна строка")
    logging.getLogger("app.port").warning("одно предупреждение")
    assert texts(first) == ["одна строка", "одно предупреждение"]


def test_foreign_chatter_and_marked_twins_stay_out_of_the_panel(panel) -> None:
    """`httpx` уровня INFO и строка с `panel=False` — мимо ленты; WARNING `httpx` — в ней."""
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger("httpx").info("HTTP Request: GET https://example.invalid")
    logging.getLogger("httpx").warning("повтор запроса")
    logging.getLogger("app.port").warning("близнец", extra={"panel": False})
    logging.getLogger("app.port").info("своя строка техлога")
    assert texts(panel) == ["повтор запроса", "своя строка техлога"]


def test_the_buffer_keeps_the_newest_lines_and_counts_the_evicted() -> None:
    """После N+500 строк: в буфере N, «вытеснено 500», порция не длиннее просьбы."""
    handler = PanelHandler(capacity=50)
    for number in range(550):
        handler.handle(logging.makeLogRecord({
            "name": PANEL_LOGGER, "levelno": logging.INFO, "levelname": "INFO",
            "msg": "строка %d", "args": (number,),
        }))
    batch = handler.since(0, 1000)
    assert len(batch.lines) == 50
    assert batch.evicted == 500
    assert batch.lines[0].text == "строка 500"
    assert len(handler.since(0, 7).lines) == 7
    assert handler.since(batch.lines[-1].seq, 1000).lines == ()


def test_the_installed_feed_holds_five_thousand_lines_and_counts_the_rest(panel) -> None:
    """Лента программы — та, что ставит `install`, — держит 5000 строк, не больше.

    5000 — число миниплана (два торговых дня). Проверка выше берёт буфер
    с пределом, заданным руками, и лента без предела у `install` или с
    поднятым пределом её не роняет.
    """
    for number in range(5000 + 25):
        debug_feed.tell(debug_feed.SOURCE_ENGINE, "строка %d", number)
    batch = panel.since(0, 1_000_000)
    assert len(batch.lines) == 5000
    assert batch.evicted == 25
    assert batch.lines[0].text == "строка 25"


def test_an_error_reaches_the_panel_as_an_error_line_not_a_warning(panel) -> None:
    """Ошибка — строкой уровня ошибки: в окне она красная, а не жёлтая.

    Два пути: строка журнала программы уровня ERROR (`HistoryPort.note`)
    и запись технического лога уровня ERROR. Мутации: ERROR журнала отдан
    уровнем WARNING (`LOG_LEVELS`), ERROR записи понижен в `_parts`, запись
    уровня ERROR не доходит вовсе.
    """
    debug_feed.note("Живой ход движка", "прекращён ошибкой программы", DecisionLevel.ERROR)
    logging.getLogger("app.port").error("живая свеча не дорисована")
    assert [(line.text, line.level) for line in lines(panel)] == [
        ("Живой ход движка — прекращён ошибкой программы", DecisionLevel.ERROR),
        ("живая свеча не дорисована", DecisionLevel.ERROR),
    ]


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="на Windows `tzset` нет")
def test_the_time_of_a_line_is_moscow_whatever_the_machine_zone(monkeypatch) -> None:
    """Машина в Новосибирске (+7), запись в 07:00 UTC — в ленте 10:00 МСК, а не 14:00."""
    monkeypatch.setenv("TZ", "Asia/Novosibirsk")
    time.tzset()
    try:
        handler = PanelHandler(10)
        handler.handle(logging.makeLogRecord({
            "name": PANEL_LOGGER, "levelno": logging.INFO, "levelname": "INFO",
            "msg": "строка", "created": datetime(2026, 10, 12, 7, 0, tzinfo=UTC).timestamp(),
        }))
        [line] = handler.since(0, 10).lines
        assert line.time.utcoffset() == timedelta(hours=3)
        assert (line.time.hour, line.time.minute) == (10, 0)
    finally:
        monkeypatch.undo()
        time.tzset()


def test_the_token_does_not_reach_the_panel_by_any_path(panel) -> None:
    """Значение под охраной реестра не попадает в ленту ни одним из пяти путей.

    Аргумент строки ленты, текст исключения, строка журнала (`note`), запись
    чужого слоя через корень и запись чужой библиотеки (`httpx`, уровень
    WARNING — такие в ленту идут). Мутация «`scrub` убран из `emit`» роняет
    все; чистка, пропустившая один путь, роняет свою строку.
    """
    assert REGISTRY.remember(SECRET)
    try:
        debug_feed.tell(debug_feed.SOURCE_ENGINE, "аргумент %s", SECRET)
        logging.getLogger("app.port").error(
            "отказ", exc_info=RuntimeError(f"ответ сервера: {SECRET}")
        )
        debug_feed.note("Отказ брокера", f"текст отказа {SECRET}", DecisionLevel.WARNING)
        logging.getLogger("market.worker").warning("ответ %s", SECRET)
        logging.getLogger("httpx").warning("повтор запроса: %s", SECRET)
        shown = texts(panel)
    finally:
        REGISTRY.forget(SECRET)
    assert len(shown) == 5
    assert not [text for text in shown if SECRET in text]
    assert all(MASK in text for text in shown)


def test_a_broken_line_is_said_once_and_then_counted(monkeypatch, caplog, panel) -> None:
    """Первая несобранная строка — одна строка WARNING (в ленту и в лог), дальше счёт."""
    monkeypatch.setattr(debug_feed, "_emit", boom)
    for _ in range(5):
        debug_feed.tell(debug_feed.SOURCE_ENGINE, "строка")
    assert debug_feed.LOSSES.count == 5
    told = [r for r in caplog.records if "не собрана" in r.getMessage()]
    assert len(told) == 1
    batch = panel.since(0, 100)
    assert batch.lost == 5
    assert [line.level for line in batch.lines] == [DecisionLevel.WARNING]


def test_a_handler_failure_is_counted_not_printed(capfd) -> None:
    """Запись с испорченным форматом: ни строки в консоль, отказ сосчитан.

    Запись подаётся обработчику ленты прямо: на корне проверки стоит
    обработчик pytest, который на испорченной записи роняет проверку сам.
    """
    handler = PanelHandler(10)
    handler.handle(broken_record())
    assert capfd.readouterr().err == ""
    assert handler.since(0, 10).lost == 1
    assert handler.since(0, 10).lines == ()


def broken_record() -> logging.LogRecord:
    """Запись с испорченным форматом: на ней обработчик ленты уходит в `handleError`."""
    return logging.makeLogRecord({
        "name": "app.port", "levelno": logging.WARNING, "levelname": "WARNING",
        "msg": "число %d", "args": ("не число",),
    })


def test_a_handler_failure_does_not_silence_the_first_builder_failure(
    monkeypatch, tmp_path: pathlib.Path
) -> None:
    """Сначала отказ обработчика, потом построитель бросает на каждом баре — в журнале строка.

    Стережёт: первый отказ построителя строки ленты доходит до технического
    журнала (файл `terminal.log`) строкой «строка ленты … не собрана
    (happened)» с типом исключения — даже когда до него обработчик ленты
    уже счёл собственный отказ (`handleError`). И причина отказа
    обработчика остаётся первой причиной, которую лента отдаёт окну.
    Мутация молчания — отказ обработчика занимает то же «первое», что
    и построитель (один признак на оба, как до 12.10.2026), — роняет
    проверку: в журнале пусто, в шапке одно число.
    """
    setup = setup_logging(tmp_path / "logs")
    handler = debug_feed.install()
    handler.handle(broken_record())
    assert debug_feed.LOSSES.state()[0] == 1
    monkeypatch.setattr(debug_feed, "PHASE_WORDS", {})
    candle = bars(1)[0]
    seen = debug_feed.BarSeen(
        candle, Outcome(state=EngineState(), steps=(Step.OPEN,), in_window=True),
        candle.close, DecisionPhase.LIVE,
    )
    for _ in range(3):
        debug_feed.happened(bar=seen)
    for each in logging.getLogger().handlers:
        each.flush()
    assert setup.path is not None
    written = setup.path.read_text(encoding="utf-8")
    told = [row for row in written.splitlines() if "строка ленты «Отладка» не собрана" in row]
    assert len(told) == 1, written[-2000:]
    assert "(happened)" in told[0] and "KeyError" in told[0], told[0]
    count, cause = debug_feed.LOSSES.state()
    assert count == 4
    assert cause.startswith("TypeError") and "app.port" in cause, cause
    assert handler.since(0, 100).cause == cause


def told_builder_failures(caplog) -> list[str]:
    """Строки технического журнала об отказе построителя строки ленты."""
    return [
        record.getMessage() for record in caplog.records
        if "строка ленты «Отладка» не собрана" in record.getMessage()
    ]


def test_the_first_builder_failure_is_said_once_whatever_comes_between(
    monkeypatch, caplog, panel
) -> None:
    """Между отказами построителя окно читает ленту и отказывает обработчик — в журнале одна строка.

    Стережёт: признак «отказ построителя уже сказан» не сбрасывают ни
    чтение ленты окном (`since` — каждые `POLL_MS`), ни отказ самого
    обработчика (`handleError`). Иначе битый построитель писал бы в журнал
    строку на каждый бар, а не одну строку и счёт в шапке. Мутации — сброс
    признака в `_Losses.state` или в ветке `aloud=False` у `_Losses.add` —
    роняют проверку.
    """
    monkeypatch.setattr(debug_feed, "_emit", boom)
    for _ in range(3):
        debug_feed.tell(debug_feed.SOURCE_ENGINE, "строка")
        panel.since(0, 100)
        panel.handle(broken_record())
    told = told_builder_failures(caplog)
    assert len(told) == 1, told
    assert debug_feed.LOSSES.state()[0] == 6


def test_taking_the_feed_down_starts_the_loss_count_afresh(monkeypatch, caplog) -> None:
    """После `uninstall` счёт с нуля, причины нет, и первый отказ построителя снова сказан.

    Стережёт изоляцию проверок (правило 14): счёт потерь один на процесс,
    и снимает его только `uninstall` — уборка `pristine_logging`. Мутация —
    `_Losses.reset` не сбрасывает признак «сказано» — делает проверки
    «сказано один раз» зависимыми от того, шла ли перед ними другая.
    """
    monkeypatch.setattr(debug_feed, "_emit", boom)
    debug_feed.tell(debug_feed.SOURCE_ENGINE, "строка")
    debug_feed.uninstall()
    assert debug_feed.LOSSES.state() == (0, "")
    caplog.clear()
    debug_feed.tell(debug_feed.SOURCE_ENGINE, "строка")
    told = told_builder_failures(caplog)
    assert len(told) == 1, told


# ------------------------------------------------------------- таблицы


def test_every_engine_step_has_words() -> None:
    assert set(debug_feed.STEP_WORDS) == set(Step)


def test_every_phase_has_words() -> None:
    assert set(debug_feed.PHASE_WORDS) == set(DecisionPhase)


def test_every_market_action_of_a_fill_has_words() -> None:
    assert {action for action in OrderAction if action.is_market} <= set(debug_feed.FILL_WORDS)


def test_every_level_has_a_log_level() -> None:
    assert set(debug_feed.LOG_LEVELS) == set(DecisionLevel)
    assert set(debug_feed.JOURNAL_LEVELS) == set(JournalLevel)


def test_the_feed_module_pulls_no_qt_and_says_nothing_to_the_console() -> None:
    """Свежий интерпретатор: модуль ленты без Qt, и строка без ленты — не в консоль.

    Модуль ввозится до `QT_API` и в ветках без окна (`app/main.py`), поэтому
    PySide6 ему не положен. Строка `app.panel` уровня WARNING при неустановленной
    ленте не уходит в `logging.lastResort`, то есть в консоль сырым текстом
    до чистки: `propagate=False` стоит вместе с `NullHandler`. Мутация — снять
    `NullHandler`. Отдельный процесс, а не эта проверка: pytest сам вешает
    свои обработчики на логгеры без выхода к корню, и `lastResort` здесь
    не сработал бы никогда.
    """
    code = (
        "import logging, sys, app.debug_feed as feed\n"
        "feed.tell(feed.SOURCE_ENGINE, 'строка %s', 'без ленты', level=logging.WARNING)\n"
        "print('PySide6' in sys.modules)\n"
    )
    environment = {**os.environ, "PYTHONPATH": str(ROOT)}
    done = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=environment,
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.strip() == "False", "app/debug_feed.py тянет Qt"
    assert done.stderr == "", f"строка ленты ушла в консоль: {done.stderr[-500:]}"
