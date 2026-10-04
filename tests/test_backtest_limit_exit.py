"""Тестер исполняет заявку на выход с предельной ценой честно (задача З8, Ф2).

Движок настоящий, исполнитель — `HistoryExecutor` через `replay`: предмет
теста — модель исполнения (`backtest/execution.py`, правило 5 шапки),
а движок здесь — источник заявки с пределом, какой она будет в прогоне.
Сценарий один на все тесты: понедельник 22.06.2026, окно 10:05–11:00,
вход в лонг по 1 000 на открытии 10:10, выход по концу окна подаётся
на закрытии 11:00 с пределом 1 000 − 10 × 1 = 990 (продажа не ниже 990).
Свечи с 11:00 — предмет каждого теста.

Что стережёт каждый тест — в его докстринге, строкой «Стережёт».
"""

from __future__ import annotations

import asyncio
from datetime import datetime, time

import pytest

from backtest import Costs, ExecutionModel, HistoryRun, LimitExits, Minutes, replay
from engine import (
    EngineSettings,
    ExitReason,
    Fill,
    Mode,
    OrderAction,
    OrderRequest,
    PriceSeen,
    Side,
    TimeExitOrder,
    TradingWindow,
)
from strategies import Intent
from tests.engine_helpers import MSK, FakeCandle, FakeTimeframe, ScriptedStrategy, decision

PRICE = 1_000.0
LIMIT_PRICE = 990.0
WINDOW = TradingWindow(time(10, 5), time(11, 0))
MARKET = EngineSettings(
    mode=Mode.REVERSE, window=WINDOW, take_profit=False, commission_per_side=1.0,
    price_step=1.0,
)
LIMIT = MARKET.replace(time_exit_order=TimeExitOrder.LIMIT)

Prices = tuple[float, float, float, float]


def _at(hour: int, minute: int) -> datetime:
    return datetime(2026, 6, 22, hour, minute, tzinfo=MSK)


def _five(hour: int, minute: int, prices: Prices) -> FakeCandle:
    o, h, low, c = prices
    return FakeCandle(time=_at(hour, minute), open=o, high=h, low=low, close=c)


def _one(hour: int, minute: int, prices: Prices) -> FakeCandle:
    o, h, low, c = prices
    return FakeCandle(
        time=_at(hour, minute), open=o, high=h, low=low, close=c,
        timeframe=FakeTimeframe(1), filled_minutes=1,
    )


FLAT = (PRICE, PRICE, PRICE, PRICE)


def _run(
    after: list[Prices],
    settings: EngineSettings = LIMIT,
    *,
    minutes: list[FakeCandle] | None = None,
    costs: Costs | None = None,
) -> HistoryRun:
    """Ровные свечи 10:00–10:55, вход на 10:10; `after` — свечи с 11:00."""
    bars = [_five(10, m, FLAT) for m in range(0, 60, 5)]
    bars += [_five(11, 5 * i, prices) for i, prices in enumerate(after)]
    script = [
        decision(Intent.LONG if i == 1 else Intent.NONE, close=bar.close)
        for i, bar in enumerate(bars)
    ]
    return asyncio.run(replay(
        bars, ScriptedStrategy(script), settings, costs=costs,
        minutes=None if minutes is None else Minutes(minutes),
    ))


def _exit(run: HistoryRun) -> tuple[float, datetime]:
    assert [deal.exit_reason for deal in run.deals] == [ExitReason.WINDOW_END], run.deals
    return run.deals[0].exit_price, run.deals[0].exit_time


# ---------------------------------------------------------------------------

def test_an_open_not_worse_than_the_limit_fills_at_the_open() -> None:
    """Стережёт: `open` не хуже предела → сделка по `open` первой свечи.

    Открытие 995 выше предела 990: продажа исполняется по 995, а не по
    пределу и не по касанию. Счётчик — «по открытию первой свечи».
    """
    run = _run([(995.0, 996.0, 994.0, 995.0), FLAT])
    assert _exit(run) == (995.0, _at(11, 0))
    assert run.limit_exits.at_open == 1
    assert run.limit_exits.at_touch == 0


def test_an_open_exactly_at_the_limit_is_not_worse_and_fills_at_the_open() -> None:
    """Стережёт равенство: `open == предел` — «не хуже», сделка по открытию.

    Открытие ровно 990. Мутация «строго лучше предела» уводит заявку
    в круг касания: цена та же, 990, но исход записан как касание —
    и в допущениях прогона «по открытию 0, по касанию 1» там, где
    исполнение было по открытию.
    """
    run = _run([(LIMIT_PRICE, 996.0, 989.0, 995.0), FLAT])
    assert _exit(run) == (LIMIT_PRICE, _at(11, 0))
    assert run.limit_exits.at_open == 1
    assert run.limit_exits.at_touch == 0


def test_slippage_does_not_push_the_fill_past_the_limit() -> None:
    """Стережёт: проскальзывание сдвигает цену против позиции, но не за предел.

    Открытие 991, проскальзывание 5 шагов по 1 → 986 по рынку; лимитная
    продажа хуже 990 не исполняется — сделка по 990.
    """
    costs = Costs(commission_per_side=1.0, price_step=1.0, slippage_steps=5.0)
    run = _run([(991.0, 992.0, 990.0, 991.0), FLAT], costs=costs)
    assert _exit(run)[0] == LIMIT_PRICE


def test_a_limit_worse_at_the_open_waits_and_fills_at_the_limit_on_a_touch() -> None:
    """Стережёт: `open` хуже предела → не по `open`, а по пределу при касании.

    Открытие 985 ниже предела; внутри свечи цена дорастает до 995. Рыночная
    дала бы 985, «лимит по открытию» — тоже 985 или ничего; честно — 990.
    """
    run = _run([(985.0, 995.0, 980.0, 993.0), FLAT])
    assert _exit(run) == (LIMIT_PRICE, _at(11, 0))
    assert run.limit_exits.at_touch == 1
    assert run.limit_exits.at_open == 0
    assert not run.halted


def test_a_gap_across_the_limit_on_a_later_bar_fills_at_its_open_and_is_counted() -> None:
    """Стережёт: разрыв через предел на более поздней свече — по `open`, отдельным счётом.

    Исход в нашу пользу (правило 5 шапки `backtest/execution.py`), и потому
    не смешивается с исполнением по открытию первой свечи.
    """
    run = _run([(985.0, 989.0, 980.0, 986.0), (995.0, 996.0, 994.0, 995.0)])
    assert _exit(run) == (995.0, _at(11, 5))
    assert run.limit_exits.at_later_open == 1
    assert run.limit_exits.at_open == 0


@pytest.mark.parametrize(
    "minutes",
    [
        # Касание внутри минутки 11:02.
        [
            _one(11, 0, (985.0, 987.0, 980.0, 986.0)),
            _one(11, 1, (986.0, 988.0, 984.0, 985.0)),
            _one(11, 2, (985.0, 995.0, 984.0, 993.0)),
            _one(11, 3, (993.0, 994.0, 992.0, 993.0)),
        ],
        # Разрыв между минутками: 985 → 993, предел 990 перепрыгнут.
        [
            _one(11, 0, (985.0, 987.0, 980.0, 986.0)),
            _one(11, 1, (986.0, 988.0, 984.0, 985.0)),
            _one(11, 2, (993.0, 995.0, 992.0, 993.0)),
            _one(11, 3, (993.0, 994.0, 992.0, 993.0)),
        ],
    ],
    ids=["touch-inside-a-minute", "gap-between-minutes"],
)
def test_the_limit_is_touched_by_the_minute_walk_the_same_as_by_the_bar_range(
    minutes: list[FakeCandle],
) -> None:
    """Стережёт: предел ищется и обходом минуток, и размахом бара — одинаково.

    Мутация «касание предела только в пути размаха бара» (`_level_fills`,
    без `_fire`): с минутками предел не задет ни разу, движок за порог
    ожидания останавливается, а прогон без минуток остаётся зелёным.
    Разрыв между минутками — по пределу, а не по открытию минутки (правило 5).
    """
    after = [(985.0, 995.0, 980.0, 993.0), FLAT]
    plain = _run(after)
    walked = _run(after, minutes=minutes)
    assert _exit(walked) == _exit(plain) == (LIMIT_PRICE, _at(11, 0))
    assert walked.limit_exits == plain.limit_exits
    assert walked.limit_exits.at_touch == 1
    assert not walked.halted


def test_the_limit_touch_comes_after_the_prices_before_it_not_in_the_open_round() -> None:
    """Стережёт контракт кругов `fills_at`: касание предела — не в круге открытия.

    Открытие 985 хуже предела 990, касание — в минутке 11:02. Честно:
    первый ответ — наблюдение цены, сделка по 990 приходит позже, после
    цен, пройденных до касания (`engine/ports.py`, `fills_at`). Мутация
    «касание ищется в круге открытия» (`_limit_at_open` смотрит размах бара)
    отдаёт сделку первым же ответом — раньше, чем рынок до предела дошёл;
    сумма сделки та же, и прогоны через `replay` этого не видят.
    """
    model = ExecutionModel()
    model.use_minutes(Minutes([
        _one(11, 0, (985.0, 987.0, 980.0, 986.0)),
        _one(11, 1, (986.0, 988.0, 984.0, 985.0)),
        _one(11, 2, (985.0, 995.0, 984.0, 993.0)),
        _one(11, 3, (993.0, 994.0, 992.0, 993.0)),
    ]))
    answers: list[list[Fill | PriceSeen]] = []

    async def go() -> None:
        await model.submit(OrderRequest(
            action=OrderAction.OPEN, side=Side.LONG, volume=1.0,
            submitted_at=_at(10, 10), reason="вход", order_id="open:long",
        ))
        assert [type(item) for item in await model.fills_at(_five(10, 10, FLAT))] == [Fill]
        await model.submit(OrderRequest(
            action=OrderAction.CLOSE, side=Side.LONG, volume=1.0,
            submitted_at=_at(11, 0), reason="выход по концу окна",
            order_id="close:long", exit_reason=ExitReason.WINDOW_END,
            limit_price=LIMIT_PRICE,
        ))
        for _ in range(64):
            answer = list(await model.fills_at(_five(11, 0, (985.0, 995.0, 980.0, 993.0))))
            if not answer:
                return
            answers.append(answer)
        raise AssertionError("обмен по свече не сошёлся за 64 круга")

    asyncio.run(go())
    fills = [item for answer in answers for item in answer if isinstance(item, Fill)]
    assert [(fill.price, fill.order_id) for fill in fills] == [(LIMIT_PRICE, "close:long")]
    assert answers and not any(isinstance(item, Fill) for item in answers[0]), (
        f"касание предела отдано в круге открытия: {answers[0]}"
    )
    first = next(i for i, answer in enumerate(answers) if any(isinstance(x, Fill) for x in answer))
    assert all(isinstance(item, PriceSeen) for answer in answers[:first] for item in answer)


def test_an_unfilled_limit_stops_the_run_and_is_named_in_the_assumptions() -> None:
    """Стережёт молчание: неисполненный выход виден в итоге прогона числом.

    Цена ни разу не доходит до 990: движок останавливается по порогу
    ожидания, позиция открыта. Мутации «счётчик не пишется» и «оговорка
    не строится» роняют тест: число 1 обязано стоять и в результате,
    и в тексте допущения рядом с причиной остановки.
    """
    run = _run([(985.0, 989.0, 980.0, 986.0)] * 3)
    assert run.deals == ()
    assert run.halted
    assert run.limit_exits.unfilled == 1
    found = [a for a in run.assumptions if a.name == "Выход по времени с предельной ценой"]
    assert len(found) == 1
    assert "не исполнилось 1" in found[0].short
    assert "Не исполнилось 1" in found[0].text
    assert "робот остановлен" in found[0].text


def test_a_market_exit_is_unchanged_and_says_nothing_about_limits() -> None:
    """Стережёт: при `MARKET` выход — по `open`, как до Ф2, и оговорки нет.

    Тот же ряд, что в тесте касания: рыночная продажа исполняется по 985,
    счётчики пусты, допущения «с предельной ценой» в списке нет.
    """
    run = _run([(985.0, 995.0, 980.0, 993.0), FLAT], MARKET)
    assert _exit(run) == (985.0, _at(11, 0))
    assert run.limit_exits.submitted == 0
    assert all(a.name != "Выход по времени с предельной ценой" for a in run.assumptions)


def test_limit_counts_of_stitched_pieces_add_up_field_by_field() -> None:
    """Стережёт: сумма исходов по кускам склейки не теряет ни одного поля."""
    one = LimitExits(submitted=2, at_open=1, at_later_open=0, at_touch=1, unfilled=0)
    two = LimitExits(submitted=3, at_open=0, at_later_open=1, at_touch=0, unfilled=2)
    assert one + two == LimitExits(
        submitted=5, at_open=1, at_later_open=1, at_touch=1, unfilled=2
    )
