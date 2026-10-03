"""Модель исполнения обходит минутки под пятиминуткой (Ф3, решение 0059 §3).

Проверяется `backtest/execution.py`: исполнитель по точкам минуток сторожит
уровень **по отрезкам** между соседними точками и отдаёт движку наблюдения
цены — по одному на ответ `fills_at`. По ним скользящий уровень едет внутри
бара, а уровень, переподанный по наблюдению, сторожится со следующего отрезка.

Что стережёт каждый тест — сказано в его докстринге фразой «Стережёт:»,
чтобы ломающий мутацией знал, какое поведение ломать.
"""

from __future__ import annotations

import asyncio
import csv
import pathlib
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime, time, timezone

import pytest

import backtest.history as history_module
from backtest import (
    ExecutionModel,
    HistoryExecutor,
    MinuteOrder,
    Minutes,
    minute_points,
    replay,
)
from engine import (
    Engine,
    EngineSettings,
    EngineState,
    ExitReason,
    Fill,
    LevelTouch,
    MarketCandle,
    Mode,
    OrderAction,
    OrderRequest,
    PriceSeen,
    Side,
    TradingWindow,
)
from market.aggregate import build_bars
from market.candles import M5, MINUTE, Candle
from strategies import EmaReverse, EmaReverseSettings, Intent
from tests.engine_helpers import (
    MSK,
    FakeCandle,
    FakeTimeframe,
    ScriptedStrategy,
    decision,
)

REPO = pathlib.Path(__file__).resolve().parent.parent
#: Минутки MXZ6 из локальной базы — вне git (`reference/minutes/README.md`).
MXZ6 = REPO / "reference" / "minutes" / "MXZ6.csv"

Answer = Sequence[Fill | PriceSeen]
React = Callable[[Fill | PriceSeen], Sequence[OrderRequest]]


def at(hour: int, minute: int) -> datetime:
    return datetime(2026, 6, 19, hour, minute, tzinfo=MSK)


Prices = tuple[float, float, float, float]


def five(hour: int, minute: int, prices: Prices) -> FakeCandle:
    """Пятиминутка: цены — открытие, максимум, минимум, закрытие."""
    o, h, low, c = prices
    return FakeCandle(time=at(hour, minute), open=o, high=h, low=low, close=c)


def one(hour: int, minute: int, prices: Prices) -> FakeCandle:
    """Минутка: цены — открытие, максимум, минимум, закрытие."""
    o, h, low, c = prices
    return FakeCandle(
        time=at(hour, minute), open=o, high=h, low=low, close=c,
        timeframe=FakeTimeframe(1), filled_minutes=1,
    )


def entry(side: Side = Side.LONG, when: datetime | None = None) -> OrderRequest:
    moment = at(10, 10) if when is None else when
    return OrderRequest(
        action=OrderAction.OPEN, side=side, volume=1.0, submitted_at=moment,
        reason="вход по сценарию", order_id=f"open:{side.value}",
    )


def arm(level: float, when: datetime, touch: LevelTouch = LevelTouch.FALL) -> OrderRequest:
    """Вооружение. Имя устойчиво — повторное заменяет прежнее, как у движка."""
    return OrderRequest(
        action=OrderAction.ARM_TAKE_PROFIT, side=Side.LONG, volume=1.0,
        submitted_at=when, reason="сторожим уровень", order_id="take:long",
        price=level, touch=touch, exit_reason=ExitReason.TRAILING_TAKE,
    )


def exchange(model: ExecutionModel, bar: MarketCandle, react: React) -> list[Answer]:
    """Обмен по одной свече, как его ведёт движок: до пустого ответа.

    `react` — подставная половина движка: заявки, рождённые элементом ответа.
    """
    answers: list[Answer] = []

    async def go() -> None:
        for _ in range(64):
            answer = await model.fills_at(bar)
            if not answer:
                return
            answers.append(answer)
            for item in answer:
                for order in react(item):
                    await model.submit(order)
        raise AssertionError("обмен по свече не сошёлся за 64 круга")

    asyncio.run(go())
    return answers


def seen_prices(answers: Sequence[Answer]) -> list[float]:
    return [item.price for answer in answers for item in answer if isinstance(item, PriceSeen)]


def fills_of(answers: Sequence[Answer]) -> list[Fill]:
    return [item for answer in answers for item in answer if isinstance(item, Fill)]


# ---------------------------------------------------------------------------
# Касание — по отрезку, а не по накопленному размаху
# ---------------------------------------------------------------------------

def test_a_moved_level_is_touched_by_the_segment_not_by_the_range_already_passed() -> None:
    """Стережёт: касание проверяется по отрезку между соседними точками.

    Лонг, уровень позади рынка (`FALL`) на 98. Минутка 100 → 99 → 102 → 101,5.
    На 102 подставной движок поднимает уровень до 101. Дальше цена идёт
    только 102 → 101,5 — уровня 101 она не касается. Минимум минутки (99)
    и бара лежит ниже 101, но пройден **до** сдвига: проверка по накопленному
    размаху выдала бы сделку по цене, до которой рынок после сдвига не дошёл.
    """
    model = ExecutionModel()
    model.use_minutes(Minutes([one(10, 10, (100, 102, 99, 101.5))]))

    def react(item: Fill | PriceSeen) -> Sequence[OrderRequest]:
        if isinstance(item, Fill) and item.action is OrderAction.OPEN:
            return [arm(98.0, item.at)]
        if isinstance(item, PriceSeen) and item.price == 102:
            return [arm(101.0, item.at)]
        return []

    asyncio.run(model.submit(entry()))
    answers = exchange(model, five(10, 10, (100, 102, 99, 101.5)), react)

    assert seen_prices(answers) == [100, 99, 102, 101.5]
    closes = [fill for fill in fills_of(answers) if fill.action is OrderAction.CLOSE]
    assert closes == [], f"уровень 101 задет ценой, пройденной до его сдвига: {closes}"
    assert model.armed["take:long"].order.price == 101.0


def test_a_level_moved_inside_the_bar_is_guarded_from_the_next_segment() -> None:
    """Стережёт: переподанный по наблюдению уровень сторожится в этом же баре.

    Вооружение по наблюдению помечено временем минутки, то есть позже начала
    бара. Сторож, проверяющий готовность по началу бара, оставил бы позицию
    без уровня до конца бара: прежний уровень заменён, новый «ещё не подан».
    Здесь новый уровень 101 задевается следующей минуткой того же бара —
    и сделка обязана случиться, по уровню, со временем начала бара.
    """
    model = ExecutionModel()
    model.use_minutes(Minutes([
        one(10, 10, (100, 102, 100, 102)),
        one(10, 11, (102, 102, 100.5, 100.5)),
    ]))

    def react(item: Fill | PriceSeen) -> Sequence[OrderRequest]:
        if isinstance(item, Fill) and item.action is OrderAction.OPEN:
            return [arm(98.0, item.at)]
        if isinstance(item, PriceSeen) and item.price == 102:
            return [arm(101.0, item.at)]
        return []

    asyncio.run(model.submit(entry()))
    answers = exchange(model, five(10, 10, (100, 102, 100, 100.5)), react)

    closes = [fill for fill in fills_of(answers) if fill.action is OrderAction.CLOSE]
    assert [(fill.price, fill.at) for fill in closes] == [(101.0, at(10, 10))], (
        "уровень, сдвинутый внутри бара, в этом баре не сторожился — позиция "
        f"осталась без уровня до закрытия бара: {closes}"
    )


# ---------------------------------------------------------------------------
# Порядок точек — из параметра
# ---------------------------------------------------------------------------

def test_the_order_of_the_minute_prices_comes_from_the_parameter() -> None:
    """Стережёт: порядок обхода берётся из `Minutes.order`, а не зашит.

    Минутка с ближним к открытию **благоприятным** экстремумом: для лонга
    «ближний первым» идёт к максимуму, «неблагоприятный первым» — к минимуму.
    Наблюдённые цены обязаны различаться; зашитый в обход порядок дал бы
    одинаковые.
    """
    minute = one(10, 10, (100, 101, 97, 99))

    def walked(order: MinuteOrder) -> list[float]:
        model = ExecutionModel()
        model.use_minutes(Minutes([minute], order))
        asyncio.run(model.submit(entry()))
        return seen_prices(exchange(model, five(10, 10, (100, 101, 97, 99)), lambda _: []))

    assert walked(MinuteOrder.NEAR_FIRST) == [100, 101, 97, 99]
    assert walked(MinuteOrder.ADVERSE_FIRST) == [100, 97, 101, 99]


@pytest.mark.parametrize(
    ("side", "expected"),
    [(Side.LONG, (100, 98, 102, 101)), (Side.SHORT, (100, 102, 98, 101))],
)
def test_at_equal_distance_the_adverse_extreme_comes_first(
    side: Side, expected: tuple[float, ...]
) -> None:
    """Стережёт: при равном удалении первым идёт неблагоприятный для позиции."""
    minute = one(10, 10, (100, 102, 98, 101))
    assert minute_points(minute, MinuteOrder.NEAR_FIRST, side) == expected


def test_a_flat_minute_is_one_point() -> None:
    """Стережёт: плоская минутка — одна точка, а не четыре наблюдения."""
    minute = one(10, 10, (100, 100, 100, 100))
    for order in MinuteOrder:
        assert minute_points(minute, order, Side.LONG) == (100,)


# ---------------------------------------------------------------------------
# Наблюдения: по одному на ответ, не с исполнением открытия, не в `fills`
# ---------------------------------------------------------------------------

def test_one_price_per_answer_never_with_the_opening_fill_and_never_in_fills() -> None:
    """Стережёт: не больше одного наблюдения на ответ (контракт порта).

    И три соседних свойства того же ответа: наблюдение не смешивается
    с исполнением по открытию; наблюдения идут при открытой ноге ещё
    **до** вооружения уровня (фильтрует движок, не исполнитель); в список
    сделок модели наблюдение не попадает.
    """
    model = ExecutionModel()
    model.use_minutes(Minutes([
        one(10, 10, (100, 103, 99, 102)),
        one(10, 11, (102, 104, 101, 103)),
    ]))
    asyncio.run(model.submit(entry()))
    answers = exchange(model, five(10, 10, (100, 104, 99, 103)), lambda _: [])

    assert all(sum(isinstance(i, PriceSeen) for i in a) <= 1 for a in answers), answers
    opening = [a for a in answers if any(isinstance(i, Fill) for i in a)]
    assert len(opening) == 1
    assert all(isinstance(i, Fill) for i in opening[0]), opening
    assert seen_prices(answers) == [100, 99, 103, 102, 102, 101, 104, 103]
    assert all(isinstance(fill, Fill) for fill in model.fills)
    assert [fill.action for fill in model.fills] == [OrderAction.OPEN]


def test_without_an_open_leg_the_walk_says_nothing() -> None:
    """Стережёт: без открытой ноги наблюдений нет — обмен кончается сразу."""
    model = ExecutionModel()
    model.use_minutes(Minutes([one(10, 10, (100, 103, 99, 102))]))
    assert exchange(model, five(10, 10, (100, 103, 99, 102)), lambda _: []) == []


# ---------------------------------------------------------------------------
# Минутки строго по интервалу бара; бар без минуток — как раньше
# ---------------------------------------------------------------------------

def test_minutes_are_taken_strictly_from_the_interval_of_the_bar() -> None:
    """Стережёт: минутка соседнего бара в обход не попадает.

    Под баром 10:10 лежат минутки 10:09 и 10:15 — обе чужие. Бар без своих
    минуток сторожится по размаху, наблюдений не даёт и считается.
    """
    model = ExecutionModel()
    model.use_minutes(Minutes([
        one(10, 9, (90, 90, 90, 90)),
        one(10, 15, (110, 110, 110, 110)),
    ]))
    asyncio.run(model.submit(entry()))
    answers = exchange(model, five(10, 10, (100, 103, 99, 102)), lambda _: [])

    assert seen_prices(answers) == []
    assert model.bars_without_minutes == 1


def test_a_bar_without_minutes_guards_the_level_by_its_range_as_before() -> None:
    """Стережёт: без минуток уровень сторожится по размаху бара, как до Ф3."""
    plain = ExecutionModel()
    fed = ExecutionModel()
    fed.use_minutes(Minutes([one(9, 0, (1, 1, 1, 1))]))
    for model in (plain, fed):
        asyncio.run(model.submit(entry()))
        answers = exchange(
            model, five(10, 10, (100, 103, 99, 102)),
            lambda item: [arm(102.5, item.at, LevelTouch.RISE)]
            if isinstance(item, Fill) and item.action is OrderAction.OPEN else [],
        )
        closes = [f.price for f in fills_of(answers) if f.action is OrderAction.CLOSE]
        assert closes == [102.5]
        assert seen_prices(answers) == []
    # Бар без минуток считается один раз, сколько бы кругов обмена по нему ни было.
    assert fed.bars_without_minutes == 1
    assert plain.bars_without_minutes == 0


# ---------------------------------------------------------------------------
# Через настоящий движок: уровень едет внутри бара и не едет назад
# ---------------------------------------------------------------------------

PRICE = 210_000.0
TRAILING = EngineSettings(
    mode=Mode.REVERSE, window=TradingWindow(time(10, 5), time(11, 0)),
    trailing_take_profit=True,
    trailing_start_percent=0.5,
    trailing_offset_percent=0.2,
    trailing_step_percent=0.05,
)


def test_with_the_real_engine_the_level_moves_inside_the_bar_and_never_back() -> None:
    """Стережёт: уровень внутри бара едет только в сторону прибыли и срабатывает.

    Движок настоящий, исполнитель — `HistoryExecutor` с минутками. Лонг
    от 210 000; цена дорастает до 211 500, откатывает к 211 300, обновляет
    вершину на 212 000 и падает к 211 500 — в той же пятиминутке. Уровни
    вооружений обязаны только расти (откат к 211 300 — не повод двигать
    уровень назад), а выход — случиться внутри бара по последнему,
    верхнему, уровню, с причиной «скользящий уровень».
    """
    bar = five(10, 10, (PRICE, 212_000, PRICE, 211_550))
    minutes = [
        one(10, 10, (PRICE, 211_500, PRICE, 211_400)),
        one(10, 11, (211_400, 212_000, 211_300, 211_900)),
        one(10, 12, (211_900, 211_950, 211_500, 211_550)),
    ]
    order = entry(when=at(10, 10))
    arms: list[OrderRequest] = []

    class Counting(HistoryExecutor):
        async def submit(self, order: OrderRequest) -> None:
            if order.action is OrderAction.ARM_TAKE_PROFIT:
                arms.append(order)
            await super().submit(order)

    executor = Counting(commission_per_side=1.0)
    executor.use_minutes(Minutes(minutes))
    engine = Engine(
        ScriptedStrategy([decision(Intent.LONG, close=bar.close)]),
        replace(TRAILING, commission_per_side=1.0),
        state=EngineState(pending=(order,)),
    )

    async def go() -> None:
        await executor.submit(order)
        await engine.on_market_candle(bar, executor)

    asyncio.run(go())

    levels = [request.price or 0.0 for request in arms]
    assert len(levels) >= 2, f"уровень внутри бара не поехал: {levels}"
    assert levels == sorted(set(levels)), f"уровень ехал назад или повторялся: {levels}"
    assert [deal.exit_reason for deal in executor.deals] == [ExitReason.TRAILING_TAKE]
    deal = executor.deals[0]
    assert deal.exit_price == levels[-1]
    assert deal.exit_time == bar.time


# ---------------------------------------------------------------------------
# Вторая стена: неподвижный тейк по минуткам MXZ6 — те же сделки и ARM
# ---------------------------------------------------------------------------

def _mxz6() -> list[Candle]:
    with MXZ6.open(newline="") as source:
        return [
            Candle(
                time=datetime.fromtimestamp(int(row["ts"]), timezone.utc).astimezone(MSK),
                open=float(row["open"]), high=float(row["high"]),
                low=float(row["low"]), close=float(row["close"]),
                volume=float(row["volume"]), timeframe=MINUTE, filled_minutes=1,
            )
            for row in csv.DictReader(source)
        ]


@pytest.mark.slow
@pytest.mark.skipif(not MXZ6.exists(), reason="минуток MXZ6 нет: reference/ вне git")
@pytest.mark.parametrize(
    "window",
    [TradingWindow(), TradingWindow(time(0, 0), time(0, 0))],
    ids=["window-default", "whole-day"],
)
def test_a_fixed_take_gives_the_same_deals_and_arms_with_minutes_and_without(
    window: TradingWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Стережёт: неподвижный тейк минутками не меняется — вторая стена (§3).

    Пятиминутки собраны из тех же минуток. С минутками и без — тот же список
    сделок, тот же слой расчётов и то же число вооружений: неподвижный тейк
    наблюдений не читает, а касание по отрезкам при неподвижном уровне
    совпадает с касанием по размаху бара. Число вооружений не сравнивается
    с запомненным — оно меряется в обоих прогонах.
    """
    minutes = _mxz6()
    bars = build_bars(minutes, M5)
    settings = EngineSettings(mode=Mode.REVERSE, window=window, commission_per_side=17.0)
    arms: list[int] = []
    made: list[HistoryExecutor] = []

    class Counting(HistoryExecutor):
        def __init__(self, **rest: object) -> None:
            super().__init__(**rest)  # type: ignore[arg-type]  # параметры replay
            made.append(self)
            arms.append(0)

        async def submit(self, order: OrderRequest) -> None:
            if order.action is OrderAction.ARM_TAKE_PROFIT:
                arms[-1] += 1
            await super().submit(order)

    monkeypatch.setattr(history_module, "HistoryExecutor", Counting)

    def run(fed: Minutes | None) -> history_module.HistoryRun:
        return asyncio.run(replay(
            bars, EmaReverse(EmaReverseSettings()), settings, minutes=fed,
        ))

    plain = run(None)
    walked = run(Minutes(minutes))

    assert plain.deals, "ни одной сделки — сравнивать нечего"
    assert walked.deals == plain.deals
    assert walked.plans == plain.plans
    assert arms[1] == arms[0] > 0
    assert made[1].bars_without_minutes == 0
    assert made[1].orphans == []
    assert not walked.halted


# ---------------------------------------------------------------------------
# Склейка: минутки куска доходят до его прогона
# ---------------------------------------------------------------------------

def test_the_minutes_of_a_piece_reach_the_replay_of_that_piece(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Стережёт: `replay_pieces` передаёт прогону минутки **своего** куска.

    Проводка в одну строку — та, что написана и не вызывается. Каждый кусок
    обязан получить свои минутки; кусок без минуток — `None`, а не чужие.
    """
    from backtest import stitched

    got: list[tuple[int, Minutes | None]] = []

    async def fake_replay(
        candles: Sequence[object], *_: object, minutes: Minutes | None = None, **__: object
    ) -> history_module.HistoryRun:
        got.append((len(candles), minutes))
        return await replay(candles, EmaReverse(EmaReverseSettings()), EngineSettings())

    monkeypatch.setattr(stitched, "replay", fake_replay)
    own = Minutes([one(10, 10, (100, 100, 100, 100))])
    pieces = [
        stitched.Piece(
            "MXU6", at(10, 0).date(), at(10, 0).date(), (), (five(10, 5, (1, 1, 1, 1)),), 0,
        ),
        stitched.Piece(
            "MXZ6", at(10, 0).date(), at(10, 0).date(), (), (five(10, 10, (1, 1, 1, 1)),), 0,
            minutes=own,
        ),
    ]
    asyncio.run(stitched.replay_pieces(
        pieces, lambda: EmaReverse(EmaReverseSettings()), EngineSettings(),
        costs=stitched.Costs(commission_per_side=1.0),
    ))
    assert [minutes for _, minutes in got] == [None, own]


# ---------------------------------------------------------------------------
# Ф5: прогон говорит, по минуткам ли он шёл и сколько баров прошло без них
# ---------------------------------------------------------------------------

def test_empty_minutes_count_every_bar_as_one_without_minutes() -> None:
    """Стережёт: поданные, но пустые минутки — каждый бар без минуток, а не тишина.

    Пустая выборка из базы (минуток за период нет) — это прогон целиком
    по размаху бара. Исполнитель, путающий «минутки не поданы» с «поданы
    пустыми», сказал бы `bars_without_minutes == 0` и отчёт промолчал бы.
    """
    model = ExecutionModel()
    model.use_minutes(Minutes([]))
    asyncio.run(model.submit(entry()))
    exchange(model, five(10, 10, (100, 103, 99, 102)), lambda _: [])
    assert model.bars_without_minutes == 1


def test_the_run_names_its_minute_order_and_the_bars_it_walked_by_range() -> None:
    """Стережёт: бары без минуток доходят до допущений прогона числом.

    Минутки лежат только под первым из трёх баров. Прогон идёт, два бара
    сторожатся по размаху — и строка допущений называет «2 свечи из 3».
    Без минуток вовсе строки нет: прогон не по минуткам о порядке их цен
    не говорит. Проверяется строка в допущениях прогона, а не счётчик
    исполнителя: счётчик, не дошедший до отчёта, человеку не виден.
    """
    bars = [
        five(10, 5, (100, 101, 99, 100)),
        five(10, 10, (100, 101, 99, 100)),
        five(10, 15, (100, 101, 99, 100)),
    ]
    minutes = Minutes([one(10, 5 + m, (100, 101, 99, 100)) for m in range(5)],
                      MinuteOrder.ADVERSE_FIRST)

    def run(fed: Minutes | None) -> history_module.HistoryRun:
        return asyncio.run(replay(
            bars, EmaReverse(EmaReverseSettings()), EngineSettings(), minutes=fed,
        ))

    walked = run(minutes)
    assert walked.minute_order is MinuteOrder.ADVERSE_FIRST
    assert walked.bars_without_minutes == 2
    line = next(
        (item for item in walked.assumptions if item.name == "Порядок цен внутри минуты"),
        None,
    )
    assert line is not None, "прогон по минуткам не назвал порядок их цен"
    assert "2 свечи из 3" in line.short, line.short
    assert "худшая для позиции" in line.short, line.short

    plain = run(None)
    assert plain.minute_order is None
    assert all(item.name != "Порядок цен внутри минуты" for item in plain.assumptions)
