"""Выход по концу окна с предельной ценой — часть движка (задача З8, Ф1).

Все проверки — на чистой функции `process_closed_candle` и собранном
состоянии, а не через прогон тестера: модели лимитной заявки в тестере
до Ф2 нет, и прогон позеленел бы из-за поведения исполнителя, а не движка
(правило 14, «предмет свой»).

Что стережёт каждый тест — в его докстринге, строкой «Стережёт».
"""

from __future__ import annotations

from datetime import time

import pytest

from app import convert
from engine import (
    EngineSettings,
    EngineState,
    JournalLevel,
    Mode,
    OrderAction,
    OrderRequest,
    Side,
    TimeExitOrder,
    TradingWindow,
    process_closed_candle,
)
from engine.contracts import ExitReason, PositionState
from engine.pipeline import Outcome
from strategies import Intent
from tests.engine_helpers import bar, decision, position
from ui.models import Mode as WindowMode
from ui.models import Settings, TimeExitKind

WINDOW = TradingWindow(time(10, 5), time(11, 0))
MARKET = EngineSettings(mode=Mode.REVERSE, window=WINDOW, take_profit=False)
LIMIT = MARKET.replace(time_exit_order=TimeExitOrder.LIMIT, price_step=25.0)
#: Закрытие вне сетки шага 25: 210 013 − 250 = 209 763, к сетке вниз 209 750;
#: 210 013 + 250 = 210 263, к сетке вверх 210 275.
OFF_GRID = 210_013.0
#: Понедельник 22.06.2026: завтра торговый день, и выход по концу окна —
#: именно по окну. В пятницу его подал бы и «нерабочий день впереди»,
#: и тест на снятую галочку зеленел бы чужим путём.
MONDAY = 22


def _at(
    state: EngineState,
    settings: EngineSettings,
    *,
    when: tuple[int, int] = (11, 5),
    close: float = OFF_GRID,
    intent: Intent = Intent.NONE,
) -> Outcome:
    return process_closed_candle(
        state, bar(*when, close=close, day=MONDAY), decision(intent, close=close), settings
    )


def _close_of(outcome: Outcome) -> OrderRequest:
    closes = [o for o in outcome.orders if o.action is OrderAction.CLOSE]
    assert len(closes) == 1, f"ожидалась одна заявка на выход: {outcome.orders}"
    return closes[0]


def _open(side: Side) -> EngineState:
    return EngineState(position=position(side, price=210_000.0))


# ------------------------------------------------------------ предел и сетка

@pytest.mark.parametrize(
    "side,expected",
    [(Side.LONG, 209_750.0), (Side.SHORT, 210_275.0)],
)
def test_the_limit_sits_n_steps_away_from_the_close_rounded_away_from_the_market(
    side: Side, expected: float
) -> None:
    """Стережёт знак, сторону и округление предела.

    Выход из лонга — продажа: предел ниже закрытия, к сетке вниз. Выход
    из шорта — покупка: выше, к сетке вверх. Перевёрнутый знак ставит
    продажу выше рынка — заявка не исполнится никогда; округление к рынку
    подаёт предел ближе, чем названо в настройке.
    """
    order = _close_of(_at(_open(side), LIMIT))
    assert order.limit_price == expected
    assert order.exit_reason is ExitReason.WINDOW_END


def test_a_price_on_the_grid_stays_on_the_grid_in_decimal() -> None:
    """Стережёт счёт в `Decimal` от строки, а не от двоичного float.

    0,3 при шаге 0,1 лежит ровно на сетке. Из двоичного вида это
    2,999… шагов, и округление вниз уронило бы предел на шаг — 0,2.
    """
    settings = LIMIT.replace(price_step=0.1, time_exit_limit_steps=0)
    order = _close_of(_at(_open(Side.LONG), settings, close=0.3))
    assert order.limit_price == 0.3


def test_the_journal_names_the_limit_and_how_it_was_counted() -> None:
    """Стережёт причину человеческим языком: предел и его вывод в строке."""
    outcome = _at(_open(Side.LONG), LIMIT)
    order = _close_of(outcome)
    assert "предельной ценой 209 750" in order.reason
    assert "210 013 − 10 × шаг 25" in order.reason


# -------------------------------------------------- где предела быть не должно

def test_the_default_market_form_carries_no_limit() -> None:
    """Стережёт путь алгоритма №1: умолчание — рыночный выход, как прежде."""
    order = _close_of(_at(_open(Side.LONG), MARKET))
    assert order.limit_price is None
    assert "предельной" not in order.reason


def test_an_exit_by_the_average_stays_market_even_with_the_limit_form() -> None:
    """Стережёт: предел — только у выхода по времени, не у выхода по сигналу."""
    outcome = _at(_open(Side.LONG), LIMIT, when=(10, 10), intent=Intent.SHORT)
    order = _close_of(outcome)
    assert order.exit_reason is ExitReason.SIGNAL
    assert order.limit_price is None


#: Пятница 26.06.2026: завтра суббота, нерабочий день.
FRIDAY = 26


def test_the_exit_before_a_day_off_carries_the_limit_too() -> None:
    """Стережёт: предел — и у выхода перед нерабочим днём, а не только по концу окна.

    Сначала — что пятница здесь действительно «перед нерабочим днём»:
    при рынке и снятой галочке выход случается и называется этой причиной.
    Иначе тест зеленел бы на дне, где прощального выхода нет вовсе.
    Мутация: предел только при `farewell is None` — выход в пятницу уходит
    по рынку, без предела.
    """
    friday = bar(11, 5, close=OFF_GRID, day=FRIDAY)
    plain = process_closed_candle(
        _open(Side.LONG), friday, decision(Intent.NONE, close=OFF_GRID),
        MARKET.replace(close_on_time_end=False),
    )
    assert _close_of(plain).exit_reason is ExitReason.NON_TRADING_DAY
    limited = process_closed_candle(
        _open(Side.LONG), friday, decision(Intent.NONE, close=OFF_GRID),
        LIMIT.replace(close_on_time_end=False),
    )
    assert _close_of(limited).limit_price == 209_750.0


def test_the_limit_form_closes_at_the_window_end_even_with_the_box_unticked() -> None:
    """Стережёт: жёсткое закрытие не слушается снятой галочки.

    `LIMIT` сам значит «закрывать по концу окна всегда». Без этого алгоритм,
    требующий жёсткого выхода, держал бы позицию через окно при снятой
    галочке «Закрывать в конце окна».
    """
    settings = LIMIT.replace(close_on_time_end=False)
    order = _close_of(_at(_open(Side.LONG), settings))
    assert order.exit_reason is ExitReason.WINDOW_END
    assert order.limit_price == 209_750.0


# ------------------------------------------------ не исполнилась — вариант А

def _closing_with(settings: EngineSettings) -> EngineState:
    """Позиция «закрывается»: заявка на выход по концу окна подана."""
    state = _at(_open(Side.LONG), settings).state
    assert state.position is not None
    assert state.position.state is PositionState.CLOSING
    return state


def test_an_unfilled_limit_exit_waits_its_own_bars_then_halts_without_any_order() -> None:
    """Стережёт порог лимитного выхода и вариант А.

    Ожидание 1 свеча: первая свеча после подачи — ждём, вторая — остановка.
    Рыночного выхода, снятия или новой заявки при остановке нет: обратную
    позицию робот не создаёт. Строка называет предел и порядок рук.
    """
    settings = LIMIT.replace(time_exit_wait_bars=1, close_wait_bars=3)
    state = _closing_with(settings)
    first = _at(state, settings, when=(11, 10))
    assert first.halt == ""
    second = _at(first.state, settings, when=(11, 15))
    assert second.halt, "лимитный выход не исполнился, а робот молчит"
    assert second.orders == ()
    assert "209 750" in second.halt
    assert "СНАЧАЛА снимите" in second.halt
    # Мутация молчания: причина остановки уходит наверх (`halt`), а в журнал
    # решений строка не пишется — человек, открывший журнал, не видит, что
    # заявка с пределом осталась у брокера и что её надо снять первой.
    said = [
        row for row in second.journal
        if row.event == "Выход с предельной ценой не исполнился"
    ]
    assert len(said) == 1, [row.event for row in second.journal]
    assert said[0].level is JournalLevel.ERROR
    assert "209 750" in said[0].reason
    assert "СНАЧАЛА снимите" in said[0].reason


def test_the_threshold_follows_the_submitted_order_not_the_current_form() -> None:
    """Стережёт: порог берётся у поданной заявки, а не у настройки.

    Заявка подана с пределом, затем форму переключили на рынок. Порог
    рыночного выхода (3 свечи) здесь чужой — остановка обязана наступить
    по лимитному (1 свеча).
    """
    settings = LIMIT.replace(time_exit_wait_bars=1, close_wait_bars=3)
    state = _closing_with(settings)
    switched = settings.replace(time_exit_order=TimeExitOrder.MARKET)
    first = _at(state, switched, when=(11, 10))
    second = _at(first.state, switched, when=(11, 15))
    assert second.halt.startswith("Выход с предельной ценой не исполнился")


def test_a_market_exit_keeps_its_own_threshold() -> None:
    """Стережёт обратное: рыночный выход не берёт лимитный порог."""
    settings = MARKET.replace(time_exit_wait_bars=1, close_wait_bars=3)
    state = _closing_with(settings)
    for minute in (10, 15, 20):
        outcome = _at(state, settings, when=(11, minute))
        assert outcome.halt == "", f"рыночный выход остановлен на 11:{minute}"
        state = outcome.state
    assert _at(state, settings, when=(11, 25)).halt.startswith("Выход не исполняется")


# ---------------------------------------------------------- «Выключен»

def test_off_names_the_limit_of_the_exit_order_left_with_the_broker() -> None:
    """Стережёт строку «Выключен» поверх лимитной заявки на выход.

    Шаг 1 выходит раньше шага 5: заявка живёт и может исполниться при
    возврате цены. Человек обязан знать её предел.
    """
    state = _closing_with(LIMIT)
    outcome = _at(state, LIMIT.replace(mode=Mode.OFF), when=(11, 10))
    assert outcome.orders == ()
    reason = outcome.journal[0].reason
    assert "заявка на выход с предельной ценой 209 750" in reason


# ---------------------------------------------------- настройки и заявка

def test_without_a_price_step_the_border_gives_the_engine_a_market_exit() -> None:
    """Стережёт: движок «шаг 0 при LIMIT» отвергает, а граница окна — нет.

    Шаг цены сообщает биржа (05.10.2026); пока его нет, граница отдаёт
    движку «по рынку» вместо отказа. Отказ здесь гасил «Применить» до
    прихода карточки: перечень изменений перед подтверждением собирается
    через эту же границу (`convert.all_changes`). Мутация: вернуть отказ
    в `convert.engine_settings` — проверка падает.
    """
    with pytest.raises(ValueError, match="шаг цены"):
        EngineSettings(time_exit_order=TimeExitOrder.LIMIT)
    engine = convert.engine_settings(
        Settings(price_step=0.0, time_exit_order=TimeExitKind.LIMIT),
        WindowMode.REVERSE,
    )
    assert engine.time_exit_order is TimeExitOrder.MARKET, engine
    lines, trouble = convert.all_changes(
        Settings(time_exit_order=TimeExitKind.LIMIT),
        Settings(time_exit_order=TimeExitKind.LIMIT, average_period=21),
    )
    assert trouble == "", f"перечень перед «Применить» не собран без шага: {trouble}"
    assert lines, "перечень перед «Применить» пуст"


def test_the_price_step_reaches_the_engine_from_the_one_window_field() -> None:
    """Стережёт одно поле шага: число из окна доходит до движка."""
    engine = convert.engine_settings(Settings(price_step=25.0), WindowMode.REVERSE)
    assert engine.price_step == 25.0


@pytest.mark.parametrize(
    "changes",
    [
        {"time_exit_limit_steps": -1},
        {"time_exit_wait_bars": 0},
        {"time_exit_wait_bars": 13},
        {"price_step": -1.0},
    ],
)
def test_impossible_time_exit_numbers_are_refused(changes: dict) -> None:
    """Стережёт границы: отрицательный отступ, ожидание вне 1…12, шаг < 0."""
    with pytest.raises(ValueError):
        EngineSettings(**changes)


def test_a_limit_price_belongs_only_to_an_exit_order() -> None:
    """Стережёт строку `FIELD_RULES`: предел у входа — отказ, а не тихая цена."""
    with pytest.raises(ValueError, match="предельная цена"):
        OrderRequest(
            action=OrderAction.OPEN, side=Side.LONG, volume=1.0,
            submitted_at=bar(10, 10).closes_at, reason="вход",
            order_id="open:x", limit_price=100.0,
        )
    with pytest.raises(ValueError, match="больше нуля"):
        OrderRequest(
            action=OrderAction.CLOSE, side=Side.LONG, volume=1.0,
            submitted_at=bar(10, 10).closes_at, reason="выход",
            order_id="close:x", exit_reason=ExitReason.WINDOW_END, limit_price=0.0,
        )
