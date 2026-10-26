"""Незакрытый показ свечи не меняет ни одной сделки (решение 0065).

Живой ход показывает движку первую записанную минуту бара T как незакрытую
свечу: по её открытию исполняется заявка, поданная на закрытии T-1. Всё
остальное — сторожа уровней, касание предела, наблюдения цены — исполнитель
судит только по закрытой T (`ExecutionModel.fills_at`). Значит ряд «каждая
свеча один–четыре раза незакрытой, затем закрытой» обязан дать **те же**
сделки, исполнения, журнал, расчёты уровней, разрывы и выходы с пределом,
что и ряд «только закрытые». Расхождение означало бы, что незакрытый показ
что-то решил, — то самое, от чего написано решение 0033.

Порт здесь не участвует: движок и исполнитель прогона (`HistoryExecutor`)
напрямую, минутки синтетические, бары собираются тем же `build_bars`, что
у порта. Отдельно — случай, где равенства с рядом «только закрытые» быть
не может: незакрытая T показана, а закрытая T не пришла вовсе. У него свой
эталон.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta
from typing import cast

import pytest

from backtest import Costs, Deal, HistoryExecutor, LevelGaps, LimitExits, Plan
from backtest.execution import Minutes
from engine import (
    Engine,
    EngineSettings,
    ExitReason,
    Fill,
    JournalEntry,
    MarketCandle,
    Mode,
    PriceSeen,
    Reversal,
    TimeExitOrder,
    TradingWindow,
)
from market import MSK, Candle, Timeframe, build_bars
from strategies import Intent, MaReverseAlways, MaReverseAlwaysSettings, Strategy
from tests.engine_helpers import ScriptedStrategy, candle, decision

M1 = Timeframe(1)
M5 = Timeframe(5)
#: Два торговых дня подряд, четверг и пятница: «стоп после тейка» считается
#: по календарной дате, и одного дня на него мало.
DAYS = (datetime(2026, 6, 18, 10, 0, tzinfo=MSK), datetime(2026, 6, 19, 10, 0, tzinfo=MSK))
PER_DAY = 600


def minutes() -> list[Candle]:
    """Минутки двух дней: волна в 1,4 % и рябь, открытие — закрытие прошлой.

    Числа не подбирались под результат: волна нужна, чтобы средняя
    пересекалась и тейк в 0,5 % достигался, рябь — чтобы скользящий
    уровень двигался внутри бара. Скачок на открытии каждого бара —
    чтобы бар открывался за уровнем и возвращался к нему внутри: только
    так видно, судит ли исполнитель уровень по огрызку незакрытого бара
    (по огрызку это разрыв, по закрытому бару — нет).
    """

    def price(index: float) -> float:
        return 100_000.0 + 700.0 * math.sin(index / 25.0) + 120.0 * math.sin(index / 6.3)

    out: list[Candle] = []
    for day, start in enumerate(DAYS):
        for shift in range(PER_DAY):
            index = day * PER_DAY + shift
            # Скачок держится первую минуту бара и уходит на второй.
            jump = 400.0 * math.sin((index // 5) * 2.1)
            opened = price(index) + (jump if shift % 5 in (0, 1) else 0.0)
            closed = price(index + 1) + (jump if shift % 5 == 0 else 0.0)
            out.append(Candle(
                time=start + timedelta(minutes=shift),
                open=opened,
                high=max(opened, closed) + 15.0 + 10.0 * abs(math.sin(index / 3.7)),
                low=min(opened, closed) - 15.0 - 10.0 * abs(math.cos(index / 5.1)),
                close=closed,
                volume=1.0,
                timeframe=M1,
                filled_minutes=1,
            ))
    return out


MINUTES = minutes()
BARS = build_bars(MINUTES, M5)


def with_partials(bars: Sequence[Candle]) -> list[Candle]:
    """Каждый бар 1–4 раза незакрытым (первые 1…k минуток), затем закрытым.

    Незакрытый бар собирается так же, как у порта: `build_bars` по минуткам
    бара с границей полноты внутри интервала. Открытие у него то же, что
    у закрытого, размах и закрытие — огрызок.
    """
    by_bar: dict[datetime, list[Candle]] = {}
    for minute in MINUTES:
        start = minute.time - timedelta(minutes=minute.time.minute % 5)
        by_bar.setdefault(start, []).append(minute)
    out: list[Candle] = []
    for number, bar in enumerate(bars):
        inside = by_bar[bar.time]
        for count in range(1, 2 + number % 4):
            [partial] = build_bars(
                inside[:count], M5, known_until=bar.time + timedelta(minutes=count)
            )
            assert partial.unsettled and partial.open == bar.open
            out.append(partial)
        out.append(bar)
    return out


#: Ответ исполнителя на незакрытую свечу: свеча, имена заявок, ждавших
#: до вопроса, и что исполнитель ответил.
type Answer = tuple[MarketCandle, frozenset[str], tuple[Fill | PriceSeen, ...]]


@dataclass(frozen=True)
class Seen:
    """Всё, что прогон дал наружу: по этим полям пути и сравниваются."""

    deals: tuple[Deal, ...]
    fills: tuple[Fill, ...]
    journal: tuple[JournalEntry, ...]
    level_plans: tuple[Plan, ...]
    gaps: LevelGaps
    limits: LimitExits
    halted: str
    #: Ответы исполнителя на незакрытые свечи (`Answering`), по порядку.
    partials: tuple[Answer, ...] = ()


class Answering(HistoryExecutor):
    """Исполнитель прогона, который запоминает свои ответы на незакрытые свечи.

    Правил не меняет: вопрос и ответ проходят насквозь. Нужен проверке
    контракта `engine/ports.py::fills_at` — «по незакрытой свече только
    исполнения по открытию», — которую равенство итогов не видит.
    """

    def __init__(self, *, costs: Costs, ruble_per_point: float) -> None:
        super().__init__(costs=costs, ruble_per_point=ruble_per_point)
        self.answers: list[Answer] = []

    async def fills_at(self, candle: MarketCandle) -> Sequence[Fill | PriceSeen]:
        waiting = frozenset(order.order_id for order in self.pending)
        answer = await super().fills_at(candle)
        if candle.unsettled:
            self.answers.append((candle, waiting, tuple(answer)))
        return answer


async def ride(
    feed: Sequence[MarketCandle],
    settings: EngineSettings,
    *,
    with_minutes: bool,
    strategy: Strategy | None = None,
) -> Seen:
    """Движок и исполнитель прогона по ряду — без порта и без базы."""
    journal: list[JournalEntry] = []
    engine = Engine(
        strategy or MaReverseAlways(MaReverseAlwaysSettings()), settings,
        journal=journal.append,
    )
    executor = Answering(
        costs=Costs(commission_per_side=settings.commission_per_side),
        ruble_per_point=settings.ruble_per_point,
    )
    if with_minutes:
        executor.use_minutes(Minutes(MINUTES))
    for item in feed:
        await engine.on_market_candle(item, executor)
        if engine.halted:
            break
    return Seen(
        deals=tuple(executor.deals),
        fills=tuple(executor.fills),
        journal=tuple(journal),
        level_plans=tuple(executor.level_plans),
        gaps=replace(executor.gaps),
        limits=executor.limits,
        halted=engine.halted,
        partials=tuple(executor.answers),
    )


@pytest.fixture(scope="module")
def loop():
    """Обычный цикл asyncio на файл: Qt здесь не нужен (как в `test_app_observe`)."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def drive(loop):
    def run[T](work: Callable[[], Coroutine[object, object, T]]) -> T:
        return cast("T", loop.run_until_complete(asyncio.wait_for(work(), 60.0)))

    return run


BASE = EngineSettings(
    mode=Mode.REVERSE,
    window=TradingWindow(start=time(0, 0), end=time(23, 59)),
    take_profit_percent=0.5,
    # Без «стопа после тейка», иначе первый же тейк кончает день и сделок
    # на сверку остаётся три-четыре. Сам стоп — отдельным случаем ниже.
    stop_after_take_profit=False,
    commission_per_side=14.0,
)


def _take(seen: Seen) -> bool:
    return any(deal.exit_reason is ExitReason.TAKE_PROFIT for deal in seen.deals)


def _trailing(seen: Seen) -> bool:
    return any(deal.exit_reason is ExitReason.TRAILING_TAKE for deal in seen.deals)


def _same_bar_reversal(seen: Seen) -> bool:
    deals = seen.deals
    return any(
        later.entry_time == earlier.exit_time
        and later.side is not earlier.side
        for earlier, later in zip(deals, deals[1:], strict=False)
    )


def _limit(seen: Seen) -> bool:
    return seen.limits.submitted > 0


#: Случай → настройки, минутки исполнителю и признак «случай не пустой».
#: Без признака равенство двух пустых списков сделок зеленело бы на любом
#: прогоне, где нужного события не случилось.
CASES: dict[str, tuple[EngineSettings, bool, Callable[[Seen], bool]]] = {
    "минутки, неподвижный тейк": (BASE, True, _take),
    "скользящий тейк по минуткам": (
        BASE.replace(trailing_take_profit=True), True, _trailing,
    ),
    "скользящий тейк по размаху бара": (
        BASE.replace(trailing_take_profit=True), False, _trailing,
    ),
    "переворот в одной свече": (
        BASE.replace(reversal=Reversal.SAME_BAR), False, _same_bar_reversal,
    ),
    "переворот через свечу": (
        BASE.replace(reversal=Reversal.THROUGH_BAR), False, lambda seen: bool(seen.deals),
    ),
    "выход по времени с пределом": (
        BASE.replace(
            window=TradingWindow(start=time(10, 30), end=time(13, 0)),
            # Без тейка позиция доживает до конца окна, иначе выхода по времени нет.
            take_profit=False,
            stop_after_take_profit=False,
            time_exit_order=TimeExitOrder.LIMIT,
            price_step=1.0,
        ),
        False,
        _limit,
    ),
    "стоп после тейка": (
        BASE.replace(stop_after_take_profit=True, take_profit_percent=0.3), True, _take,
    ),
}


@pytest.mark.parametrize("case", list(CASES))
def test_partial_shows_of_every_bar_change_nothing(drive, case: str) -> None:
    """Стережёт: незакрытый показ бара не меняет ни сделок, ни журнала, ни счётчиков.

    Каждый бар показан 1–4 раза незакрытым, затем закрытым; против ряда
    только из закрытых — равенство поле в поле: сделки, исполнения, журнал
    решений, расчёты уровней, разрывы через уровень, выходы с пределом,
    остановка. Ломается, если исполнитель по незакрытой свече судит уровень
    или предел, запоминает её закрытие как прошлое либо движок решает по ней.
    Обход минуток по незакрытой свече итогов не меняет — его ловит
    `test_a_partial_bar_is_answered_only_with_fills_at_its_open`.
    """
    settings, with_minutes, eventful = CASES[case]

    async def go() -> tuple[Seen, Seen]:
        closed = await ride(BARS, settings, with_minutes=with_minutes)
        shown = await ride(with_partials(BARS), settings, with_minutes=with_minutes)
        return closed, shown

    closed, shown = drive(go)
    assert eventful(closed), f"«{case}»: нужного события в ряду нет — сверять нечего"
    assert not closed.halted, closed.halted
    assert shown.deals == closed.deals, (
        f"«{case}»: сделок {len(shown.deals)} против {len(closed.deals)} — "
        "незакрытый показ изменил список сделок"
    )
    assert shown.fills == closed.fills, f"«{case}»: исполнения разошлись"
    assert shown.journal == closed.journal, f"«{case}»: журнал решений разошёлся"
    assert shown.level_plans == closed.level_plans, f"«{case}»: расчёты уровней разошлись"
    assert shown.gaps == closed.gaps, f"«{case}»: разрывы {shown.gaps} против {closed.gaps}"
    assert shown.limits == closed.limits, f"«{case}»: выходы с пределом разошлись"
    assert shown.halted == closed.halted, f"«{case}»: остановка разошлась: {shown.halted}"


@pytest.mark.parametrize("case", list(CASES))
def test_a_partial_bar_is_answered_only_with_fills_at_its_open(drive, case: str) -> None:
    """Стережёт: на незакрытую свечу исполнитель отвечает только исполнениями по открытию.

    Каждый ответ на незакрытый показ — исполнения заявок, ждавших открытия
    этого бара, со временем его начала. Ни касания уровня, ни наблюдения
    цены (`PriceSeen`): минуты, в которые цена дошла бы до уровня, у живого
    хода ещё не наступили.

    Равенства итогов (`test_partial_shows_of_every_bar_change_nothing`) для
    этого мало. Когда все минутки известны исполнителю заранее, обход минуток
    по незакрытой свече даёт те же сделки, что и по закрытой, — только
    раньше, чем они случились: в живом ходе это тейк, показанный на первой
    минуте бара и случившийся на четвёртой. Сегодня живому исполнителю
    минутки не подаются (`use_minutes` зовёт только `backtest.history.replay`,
    а он незакрытых свечей не получает), и до этой дороги один шаг — подать
    их. Ломается, если по незакрытой свече исполнитель обходит минутки,
    судит уровень или исполняет не по её открытию.
    """
    settings, with_minutes, _ = CASES[case]

    async def go() -> Seen:
        return await ride(with_partials(BARS), settings, with_minutes=with_minutes)

    shown = drive(go)
    filled = [item for _, _, answer in shown.partials for item in answer]
    assert filled, f"«{case}»: по незакрытым показам не исполнено ничего — проверять нечего"
    for partial, waiting, answer in shown.partials:
        moment = f"{partial.time:%d.%m %H:%M}"
        for item in answer:
            assert isinstance(item, Fill), (
                f"«{case}»: на незакрытый бар {moment} исполнитель ответил "
                f"наблюдением цены {item.price:g} в {item.at:%H:%M} — обход минуток, "
                "которых у живого хода ещё нет"
            )
            assert item.order_id in waiting, (
                f"«{case}»: на незакрытый бар {moment} исполнена заявка "
                f"{item.order_id}, которая открытия не ждала, — касание уровня "
                "по огрызку"
            )
            assert item.at == partial.time, (
                f"«{case}»: исполнение по незакрытому бару {moment} помечено "
                f"{item.at:%H:%M}, а не началом бара"
            )


#: Что показано между огрызком T и закрытой T+1, когда закрытой T не было.
#: Незакрытая T+1 — штатный путь порта, а не выдумка: конец T он замечает
#: только на первой минуте T+1 и тем же проходом показывает её незакрытой
#: (`HistoryPort._forming_of`) — с ждущей заявкой или без.
SHOWN_BEFORE_T1: dict[str, tuple[MarketCandle, ...]] = {
    "ничего": (),
    "незакрытая T+1": (
        replace(
            candle(10, 15, close=101_000.0, filled_minutes=1, unsettled=True),
            open=101_000.0, high=101_000.0, low=101_000.0,
        ),
    ),
}


@pytest.mark.parametrize("between", list(SHOWN_BEFORE_T1))
def test_a_partial_bar_whose_closed_bar_never_came_does_not_stop_the_robot(
    drive, between: str
) -> None:
    """Стережёт: закрытая T не пришла — робот не останавливается «ошибкой исполнителя».

    Живой ход исполнил вход по открытию незакрытой T (100 000), закрытую T
    порт выбросил как неполную, следующей пришла закрытая T+1 — целиком выше
    тейка 100 500. Закрытие T-1 (101 000) тоже выше тейка: по нему сторож разрыва
    судил бы, что разрыва не было, и падал бы `assert` → «Ошибка внутри
    исполнителя», робот остановлен. Огрызок T закрылся на 100 600 — тоже
    за тейком: «прошлым» не годится и он. Прошлый интервал показан только
    незакрытым, значит судить по закрытию T-1 нельзя: касание за размахом
    T+1 — разрыв (`LevelGaps.rise`), выход по уровню, робот работает.

    Эталон свой: с рядом «только закрытые» он не совпадает по построению —
    там вход был бы по открытию T+1.

    Вариант «незакрытая T+1»: перед закрытой T+1 показана она же незакрытой.
    Прошлый интервал от этого закрытым не становится — T по-прежнему показана
    только огрызком. Ломается, если исполнитель помнит лишь **последний**
    незакрытый показ: тогда показ T+1 затирает показ T, и закрытая T+1 судится
    по закрытию T-1 — `assert` в `_gapped`, робот остановлен.
    """
    settings = BASE.replace(stop_after_take_profit=True)
    script = [decision(Intent.NONE), decision(Intent.LONG), decision(Intent.LONG)]
    feed = [
        candle(10, 0, close=101_000.0),
        candle(10, 5, close=101_000.0),                                   # T-1: «лонг»
        # T: первая минута, уже задевшая тейк 100 500 и закрывшаяся за ним.
        replace(
            candle(10, 10, close=100_600.0, filled_minutes=1, unsettled=True),
            open=100_000.0, low=100_000.0,
        ),
        # Закрытой T нет: порт выбросил её как неполную (`_trusted`).
        *SHOWN_BEFORE_T1[between],
        replace(candle(10, 15, close=101_000.0), high=101_200.0, low=100_800.0),
    ]

    async def go() -> Seen:
        return await ride(
            feed, settings, with_minutes=False, strategy=ScriptedStrategy(script)
        )

    seen = drive(go)
    assert not seen.halted, f"робот остановлен: {seen.halted}"
    assert len(seen.deals) == 1, seen.deals
    (deal,) = seen.deals
    assert deal.entry_time == feed[2].time, "вход не по открытию незакрытой T"
    assert deal.entry_price == pytest.approx(100_000.0)
    assert deal.exit_time == feed[-1].time
    assert deal.exit_price == pytest.approx(100_500.0), "выход не по уровню тейка"
    assert deal.exit_reason is ExitReason.TAKE_PROFIT
    assert (seen.gaps.rise, seen.gaps.fall) == (1, 0), (
        f"касание за размахом T+1 не названо разрывом: {seen.gaps}"
    )
