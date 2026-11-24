"""Уровень неподвижного тейка на сетке шага цены (решение 0066, Р1).

В бою неподвижный тейк — лимитная заявка у брокера, а цена вне сетки шага
биржей не принимается. Уровень к сетке приводит **движок**, от рынка: у лонга
вверх, у шорта вниз — так уровень не оказывается ближе к рынку, чем названо
в настройке. Модель исполнения судит касание **тем же** уровнем: она берёт
его из заявки вооружения и сама не округляет ничего.

При шаге ноль («шаг неизвестен») — прежнее округление до целого, половина
к чётному, как у прототипа: проверка-образец идёт именно так и не меняется.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import time

import pytest

from engine import (
    Engine,
    EngineSettings,
    EngineState,
    Fill,
    JournalEntry,
    Mode,
    OrderAction,
    OrderRequest,
    Side,
    TakeProfit,
    TradingWindow,
    fixed_level,
)
from strategies import Intent
from tests.engine_helpers import (
    FakeCandle,
    NextOpenExecutor,
    RecordingExecutor,
    ScriptedStrategy,
    bar,
    candle,
    decision,
    position,
)

WINDOW = TradingWindow(time(10, 5), time(11, 0))
ON_GRID = EngineSettings(mode=Mode.REVERSE, window=WINDOW, price_step=25.0)
ENTRY = 216_100.0  # 0,5% — 1 080,5: точный уровень лонга 217 180,5, шорта 215 019,5



@pytest.mark.parametrize(
    "side,expected",
    [(Side.LONG, 217_200.0), (Side.SHORT, 215_000.0)],
    ids=["long-up", "short-down"],
)
def test_the_fixed_level_goes_onto_the_grid_away_from_the_market(
    side: Side, expected: float
) -> None:
    """Шаг 25: лонг 217 180,5 → 217 200 (вверх), шорт 215 019,5 → 215 000 (вниз).

    Стережёт направление: округление к рынку дало бы 217 175 и 215 025 —
    тейк ближе, чем названо в настройке.
    """
    sign = 1 if side is Side.LONG else -1
    assert fixed_level(ENTRY, sign, 0.5, 25.0) == expected


def test_the_grid_rounds_the_exact_level_not_the_whole_one() -> None:
    """Округляется точное значение, а не уже сведённое к целому.

    209 000 + 0,5% = 210 045; при шаге 0,5 и цене 209 001 точный уровень
    210 046,005 — лонг вверх → 210 046,5. Сведённый сначала к целому
    (210 046) встал бы на узел ближе к рынку. Стережёт: уровень не ближе
    к рынку, чем точный.
    """
    level = fixed_level(209_001.0, 1, 0.5, 0.5)
    assert level == 210_046.5


def test_a_level_already_on_the_grid_stays_where_it_is() -> None:
    """Уровень, лежащий на узле, не уезжает на шаг дальше.

    200 000 + 0,5% = 201 000 ровно, это узел сетки 25. Стережёт счёт без
    двоичного хвоста: 201 000,000…1 ушёл бы вверх на 201 025.
    """
    assert fixed_level(200_000.0, 1, 0.5, 25.0) == 201_000.0
    assert fixed_level(200_000.0, -1, 0.5, 25.0) == 199_000.0


@pytest.mark.parametrize(
    "entry,sign,expected",
    [
        (216_100.0, 1, 217_180.0),
        (230_300.0, 1, 231_452.0),
        (231_700.0, -1, 230_542.0),
    ],
)
def test_at_step_zero_the_levels_are_the_prototype_ones(
    entry: float, sign: int, expected: float
) -> None:
    """Шаг ноль — уровни прототипа, до целого, половина к чётному.

    Три случая журнала прототипа, различающие округление в обе стороны.
    Стережёт: без шага сетка не вмешивается, и проверка-образец не меняется.
    """
    assert fixed_level(entry, sign, 0.5) == expected
    assert fixed_level(entry, sign, 0.5, 0.0) == expected


def _opened(settings: EngineSettings) -> tuple[Engine, RecordingExecutor, list[JournalEntry]]:
    """Лонг открывается сделкой по 216 100 — вооружение рождается от неё."""
    entry = OrderRequest(
        action=OrderAction.OPEN, side=Side.LONG, volume=1.0,
        submitted_at=bar(10, 10).closes_at, reason="вход", order_id="open:тест",
    )
    deal = Fill(
        action=OrderAction.OPEN, side=Side.LONG, volume=1.0, price=ENTRY,
        at=candle(10, 10).time, order_id=entry.order_id,
    )
    lines: list[JournalEntry] = []
    engine = Engine(
        ScriptedStrategy([decision(Intent.LONG, close=ENTRY)] * 2), settings,
        state=EngineState(pending=(entry,)), journal=lines.append,
    )
    executor = RecordingExecutor(scripted=[[deal]])
    asyncio.run(engine.on_market_candle(candle(10, 10, close=ENTRY), executor))
    return engine, executor, lines


def _arms(executor: RecordingExecutor) -> list[float | None]:
    return [
        order.price for order in executor.submitted
        if order.action is OrderAction.ARM_TAKE_PROFIT
    ]


def test_the_engine_arms_the_long_take_at_217_200_on_step_25() -> None:
    """Сделка входа по 216 100, шаг 25: вооружение на 217 200, у позиции тот же уровень.

    Стережёт: цену на сетку кладёт движок — в заявке вооружения уже 217 200,
    и уровень позиции после принятой заявки тот же.
    """
    engine, executor, _ = _opened(ON_GRID)
    assert _arms(executor) == [217_200.0, 217_200.0]
    assert engine.position is not None
    assert engine.position.take.level == 217_200.0


def test_at_step_zero_the_engine_arms_the_prototype_level() -> None:
    """Шаг ноль — вооружение на 217 180, как до решения 0066."""
    engine, executor, _ = _opened(ON_GRID.replace(price_step=0.0))
    assert _arms(executor) == [217_180.0, 217_180.0]


def test_the_journal_says_the_level_was_moved_onto_the_grid() -> None:
    """Строка «Тейк-профит выставлен» называет сетку и сторону.

    Без хвоста строка «217 200 = цена входа 216 100 + 0,5%» была бы неправдой
    на 19,5 пункта.
    """
    _, _, lines = _opened(ON_GRID)
    placed = [entry for entry in lines if entry.event == "Тейк-профит выставлен"]
    assert placed, "строки о вооружении нет"
    assert "к сетке шага 25 вверх" in placed[0].reason


def test_a_step_change_on_the_fly_does_not_move_an_open_fixed_take() -> None:
    """Шаг замораживается с планом тейка: смена на ходу открытую позицию не трогает.

    ТЗ §4.4 А: новое значение — со следующей сделки. Стережёт: после смены
    шага на 10 вооружение на следующей свече остаётся 217 200, а не 217 190.
    """
    engine, executor, _ = _opened(ON_GRID)
    engine.apply_settings(ON_GRID.replace(price_step=10.0))
    asyncio.run(engine.on_market_candle(candle(10, 15, close=ENTRY), executor))
    assert _arms(executor)[-1] == 217_200.0


def _bar(minute: int, *, open_: float, high: float, low: float, close: float) -> FakeCandle:
    base = candle(10, minute)
    return FakeCandle(time=base.time, open=open_, high=high, low=low, close=close)


def test_the_model_judges_the_touch_by_the_grid_level() -> None:
    """Модель судит касание по 217 200: 217 190 — не касание, 217 200 — выход по 217 200.

    Стережёт: прогон и бой видят один уровень. Свеча с максимумом 217 190
    задела бы уровень прототипа 217 180 и закрыла бы позицию; на сетке шага
    25 уровень 217 200, и выход случается только на свече, дошедшей до него,
    по цене 217 200.
    """
    script = [decision(Intent.LONG, close=ENTRY)] * 4
    engine = Engine(ScriptedStrategy(script), ON_GRID)
    model = NextOpenExecutor()
    asyncio.run(engine.on_market_candle(
        _bar(5, open_=ENTRY, high=ENTRY, low=ENTRY, close=ENTRY), model
    ))
    asyncio.run(engine.on_market_candle(
        _bar(10, open_=ENTRY, high=216_150.0, low=216_000.0, close=ENTRY), model
    ))
    assert engine.position is not None, "вход не исполнился"
    assert engine.position.take.level == 217_200.0

    asyncio.run(engine.on_market_candle(
        _bar(15, open_=216_500.0, high=217_190.0, low=216_400.0, close=217_000.0), model
    ))
    assert engine.position is not None, "модель сработала на 217 190 — ниже уровня 217 200"
    assert model.deals == []

    asyncio.run(engine.on_market_candle(
        _bar(20, open_=217_000.0, high=217_200.0, low=216_900.0, close=217_100.0), model
    ))
    assert engine.state.position is None, "касание 217 200 не закрыло позицию"
    assert [(deal.exit_price, deal.exit_reason) for deal in model.deals] == [
        (217_200.0, "take")
    ]


def test_a_plan_with_a_step_arms_on_the_grid_and_the_model_judges_by_it() -> None:
    """План позиции со шагом 25: вооружение на 217 200, и модель судит по 217 200.

    Не зависит от проводки шага из настроек: план собран сразу со шагом —
    так он выглядит после входа, когда проводка включена. Стережёт движок
    (шаг 5 кладёт уровень на сетку из замороженного плана) и модель
    (касание судится уровнем из заявки: 217 190 — не касание, 217 200 —
    выход по 217 200).
    """
    held = replace(
        position(Side.LONG, price=ENTRY),
        take=TakeProfit(percent=0.5, price_step=25.0),
    )
    script = [decision(Intent.LONG, close=ENTRY)] * 3
    engine = Engine(ScriptedStrategy(script), ON_GRID, state=EngineState(position=held))
    model = NextOpenExecutor()
    model.place(Side.LONG, ENTRY, held.entry_time)
    asyncio.run(engine.on_market_candle(
        _bar(10, open_=ENTRY, high=216_150.0, low=216_000.0, close=ENTRY), model
    ))
    assert engine.position is not None
    assert engine.position.take.level == 217_200.0, "шаг 5 не положил уровень на сетку"

    asyncio.run(engine.on_market_candle(
        _bar(15, open_=216_500.0, high=217_190.0, low=216_400.0, close=217_000.0), model
    ))
    assert model.deals == [], "модель сработала на 217 190 — ниже уровня 217 200"

    asyncio.run(engine.on_market_candle(
        _bar(20, open_=217_000.0, high=217_200.0, low=216_900.0, close=217_100.0), model
    ))
    assert [(deal.exit_price, deal.exit_reason) for deal in model.deals] == [
        (217_200.0, "take")
    ]
    assert engine.state.position is None
