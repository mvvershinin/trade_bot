"""Режим «Лонг и шорт, один вход в день».

Слова владельца счёта: «это режим лонг и шорт — но без переворота — то есть
закрыли и всё, больше в этот день не торгуем».

Что стережётся:

* вход по сигналу средней в любую сторону, как в «Лонг и шорт»;
* обратный сигнал закрывает позицию **без** входа в обратную сторону —
  и при перевороте «через свечу», и при перевороте «в одной свече»;
* после любого выхода за календарную дату (сигнал, тейк, конец окна)
  новых входов нет, и журнал говорит почему;
* следующая дата снова входит;
* прежние режимы дату последнего выхода не читают — сверка с прототипом
  от нового поля не зависит.
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
    Mode,
    OrderAction,
    Reversal,
    Side,
    Step,
    TradingWindow,
    apply_fill,
    process_closed_candle,
)
from strategies import Intent
from tests.engine_helpers import (
    NextOpenExecutor,
    ScriptedStrategy,
    bar,
    candle,
    decision,
    position,
)

WINDOW = TradingWindow(time(10, 5), time(11, 0))
ONE = EngineSettings(mode=Mode.ONE_ENTRY_A_DAY, window=WINDOW, take_profit=False)
PRICE = 210_000.0
INSIDE = (10, 10)
TODAY = bar(*INSIDE).closes_at.date()


def run(
    state: EngineState,
    *,
    settings: EngineSettings = ONE,
    intent: Intent = Intent.LONG,
    when: tuple[int, int] = INSIDE,
    day: int = 19,
):
    return process_closed_candle(
        state,
        bar(*when, close=PRICE, day=day),
        decision(intent, close=PRICE),
        settings,
    )


def actions(outcome) -> list[OrderAction]:
    return [order.action for order in outcome.orders]


def _day(
    settings: EngineSettings, intents: list[Intent], *, touch: float | None = None,
):
    """Прогнать день свечами 10:05, 10:10, … по намерениям. Сделки и журнал.

    `touch` — `high` второй свечи: на ней исполняется вход, и тейк достаётся
    внутри неё же.
    """
    strategy = ScriptedStrategy([decision(i, close=PRICE) for i in intents])
    lines: list = []
    engine = Engine(strategy, settings, journal=lines.append)
    executor = NextOpenExecutor()

    async def scenario() -> None:
        for number in range(len(intents)):
            minute = 5 + 5 * number
            bar_ = candle(10, minute, close=PRICE)
            if number == 1 and touch is not None:
                bar_ = replace(bar_, high=touch)
            await engine.on_market_candle(bar_, executor)

    asyncio.run(scenario())
    return engine, executor, lines


@pytest.mark.parametrize("reversal", [Reversal.THROUGH_BAR, Reversal.SAME_BAR])
def test_reverse_signal_closes_without_entering_and_the_day_stays_empty(
    reversal: Reversal,
) -> None:
    """Лонг → обратный сигнал → выход, и дальше до конца дня пусто.

    Обратных сигналов после выхода три подряд, в обе стороны: ни один
    не даёт входа. Режим «в одной свече» здесь обязателен — в нём шаг 10
    подал бы вход сразу за выходом, пока сделки выхода ещё нет.
    """
    intents = [Intent.LONG, Intent.LONG, Intent.SHORT,
               Intent.SHORT, Intent.LONG, Intent.SHORT, Intent.LONG]
    engine, executor, lines = _day(ONE.replace(reversal=reversal), intents)

    assert len(executor.deals) == 1, executor.deals
    deal = executor.deals[0]
    assert deal.side is Side.LONG
    assert deal.exit_reason != "take"
    assert engine.state.position is None
    assert engine.state.entry_in_flight() is None, "после выхода подан вход"
    assert any("один вход в день" in line.reason for line in lines), [
        line.event for line in lines
    ]


def test_the_exit_bar_itself_submits_no_opposite_entry_same_bar() -> None:
    """Свеча обратного сигнала в «одной свече»: только снять и закрыть."""
    outcome = run(
        EngineState(position=position(Side.LONG, price=PRICE)),
        settings=ONE.replace(reversal=Reversal.SAME_BAR),
        intent=Intent.SHORT,
    )
    assert OrderAction.OPEN not in actions(outcome), actions(outcome)
    assert actions(outcome)[-1] is OrderAction.CLOSE
    assert any(
        "без переворота" in line.reason for line in outcome.journal
    ), [line.reason for line in outcome.journal]


def test_after_a_take_the_day_is_closed() -> None:
    """Тейк внутри свечи → ни одного входа до конца дня."""
    settings = ONE.replace(take_profit=True, stop_after_take_profit=False)
    intents = [Intent.LONG] * 6
    engine, executor, _ = _day(settings, intents, touch=PRICE * 1.01)
    assert len(executor.deals) == 1, executor.deals
    assert executor.deals[0].exit_reason == "take"
    assert engine.state.position is None
    assert engine.state.entry_in_flight() is None


def test_an_exit_earlier_today_blocks_the_entry_and_says_why() -> None:
    blocked = run(EngineState(last_exit_date=TODAY), intent=Intent.SHORT)
    assert blocked.orders == ()
    assert blocked.last_step is Step.STOP_AFTER_TAKE
    assert "день закрыт" in blocked.journal[0].reason
    assert "один вход в день" in blocked.journal[0].reason


def test_the_next_day_enters_again() -> None:
    tomorrow = run(EngineState(last_exit_date=TODAY), intent=Intent.LONG, day=22)
    assert actions(tomorrow) == [OrderAction.OPEN], "новый день, а входа нет"


def test_the_first_entry_of_the_day_is_made_in_both_directions() -> None:
    for intent in (Intent.LONG, Intent.SHORT):
        assert actions(run(EngineState(), intent=intent)) == [OrderAction.OPEN]


@pytest.mark.parametrize("mode", [Mode.REVERSE, Mode.LONG_ONLY])
def test_other_modes_do_not_read_the_last_exit_date(mode: Mode) -> None:
    """Сверка с прототипом держится на этом: поле пишется всегда, читает один режим."""
    outcome = run(
        EngineState(last_exit_date=TODAY), settings=ONE.replace(mode=mode),
    )
    assert actions(outcome) == [OrderAction.OPEN]


def test_an_exit_at_the_window_end_records_the_date() -> None:
    """Выход по концу окна — тоже выход: дата дня записывается сделкой."""
    held = EngineState(position=position(Side.LONG, price=PRICE))
    leaving = run(held, when=(11, 5), intent=Intent.LONG)
    assert actions(leaving) == [OrderAction.CLOSE]
    order = leaving.orders[-1]
    after = apply_fill(leaving.state, Fill(
        action=OrderAction.CLOSE, side=Side.LONG, volume=1.0, price=PRICE,
        at=bar(11, 5).closes_at, order_id=order.order_id,
    ), ONE)
    assert after.state.last_exit_date == TODAY
    assert actions(run(after.state, intent=Intent.SHORT)) == []
