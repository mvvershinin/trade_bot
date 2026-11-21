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
    limit_beyond,
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
#: Закрытие вне сетки шага 25, отступ по умолчанию 2 % (решение 0067):
#: 210 013 − 4 200,26 = 205 812,74, к сетке вниз 205 800;
#: 210 013 + 4 200,26 = 214 213,26, к сетке вверх 214 225.
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
    [(Side.LONG, 205_800.0), (Side.SHORT, 214_225.0)],
)
def test_the_limit_sits_a_percent_away_from_the_close_rounded_away_from_the_market(
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

    10 − 3 % = 9,7 при шаге 0,1 лежит ровно на сетке. Из двоичного вида это
    96,999… шагов, и округление вниз уронило бы предел на шаг — 9,6.
    """
    settings = LIMIT.replace(price_step=0.1, time_exit_limit_percent=3.0)
    order = _close_of(_at(_open(Side.LONG), settings, close=10.0))
    assert order.limit_price == 9.7


def test_the_journal_names_the_limit_and_how_it_was_counted() -> None:
    """Стережёт причину человеческим языком: предел и его вывод в строке."""
    outcome = _at(_open(Side.LONG), LIMIT)
    order = _close_of(outcome)
    assert "Предел заявки 205 800" in order.reason
    assert "210 013 − 2 %, к сетке шага 25 вниз" in order.reason


def test_the_limit_words_hold_for_any_executor_behind_the_port() -> None:
    """Стережёт правку 1.2 гейта: слова движка верны и в прогоне, и в бою.

    Боевой исполнитель вправе подать вместо предела границу ценового коридора
    биржи (решение 0067), и она может лежать по любую сторону от предела —
    в том числе ближе к рынку (`/risk` 04.11.2026, п. 5). Обещание «не хуже
    предела» без оговорки было бы ложью в журнале «факта»: человек искал бы
    у брокера заявку по пределу и сделку не хуже него. Обещание «дальше
    от рынка» — тоже. Ветки по исполнителю в движке нет — оговорка стоит
    в тексте для обоих.

    Мутация: вернуть безусловное «продажа не хуже предела», убрать оговорку
    об исполнителе или вернуть «дальше от рынка» — проверка падает.
    """
    reason = _close_of(_at(_open(Side.LONG), LIMIT)).reason
    assert "Поданный предел — продажа не дешевле 205 800" in reason, reason
    assert "вправе подать вместо него границу ценового коридора биржи" in reason, reason
    assert "по любую сторону от предела" in reason, reason
    assert "дальше от рынка" not in reason, reason
    assert "не хуже предела" not in reason, reason
    short = _close_of(_at(_open(Side.SHORT), LIMIT)).reason
    assert "Поданный предел — покупка не дороже 214 225" in short, short


def test_another_percent_gives_another_limit() -> None:
    """Стережёт: предел считается от настройки, а не от зашитого числа (правило 16)."""
    three = LIMIT.replace(time_exit_limit_percent=3.0)
    # 210 013 × 0,97 = 203 712,61 → к сетке 25 вниз 203 700.
    assert _close_of(_at(_open(Side.LONG), three)).limit_price == 203_700.0


# ------------------------------------------------ одно правило на два слоя

@pytest.mark.parametrize(
    ("price", "percent", "step", "buying", "expected"),
    [
        pytest.param(OFF_GRID, 2.0, 25.0, False, 205_800.0, id="продажа-вниз"),
        pytest.param(OFF_GRID, 2.0, 25.0, True, 214_225.0, id="покупка-вверх"),
        pytest.param(1_000.0, 1.0, 1.0, False, 990.0, id="продажа-на-сетке"),
        pytest.param(1_000.0, 1.0, 1.0, True, 1_010.0, id="покупка-на-сетке"),
        pytest.param(10.0, 3.0, 0.1, False, 9.7, id="десятичный-шаг-продажа"),
        pytest.param(10.0, 3.0, 0.1, True, 10.3, id="десятичный-шаг-покупка"),
        pytest.param(10.0, 99.99, 1.0, False, 1.0, id="продажа-не-ниже-шага"),
    ],
)
def test_the_percent_limit_is_away_from_the_market_and_on_the_grid(
    price: float, percent: float, step: float, buying: bool, expected: float
) -> None:
    """Стережёт `limit_beyond` — одно правило цены на движок и исполнитель (Р-3).

    Покупка — не ниже `цена × (1 + p)`, продажа — не выше `цена × (1 − p)`,
    обе на сетке шага и не дальше одного шага от точного значения; продажа
    не ниже одного шага. Мутации: знак, округление к рынку, двоичный счёт
    вместо десятичного, снятый пол в один шаг.
    """
    got = limit_beyond(price, percent, step, buying=buying)
    assert got == expected
    exact = price * (1 + percent / 100) if buying else price * (1 - percent / 100)
    if expected > step:
        assert (got >= exact - 1e-9) if buying else (got <= exact + 1e-9), (got, exact)
        assert abs(got - exact) < step, (got, exact)


@pytest.mark.parametrize(
    ("price", "percent", "step"),
    [
        pytest.param(100.0, 2.0, 0.0, id="шаг-ноль"),
        pytest.param(100.0, 2.0, -1.0, id="шаг-отрицательный"),
        pytest.param(100.0, 2.0, float("nan"), id="шаг-не-число"),
        pytest.param(100.0, 0.0, 1.0, id="процент-ноль"),
        pytest.param(100.0, -2.0, 1.0, id="процент-отрицательный"),
        pytest.param(0.0, 2.0, 1.0, id="цена-ноль"),
    ],
)
def test_the_percent_limit_refuses_impossible_inputs_aloud(
    price: float, percent: float, step: float
) -> None:
    """Стережёт отказ вслух: предел без шага или без процента совпал бы с ценой молча."""
    with pytest.raises(ValueError, match="предельную цену не посчитать"):
        limit_beyond(price, percent, step, buying=False)


@pytest.mark.parametrize("percent", [0.05, 20.5, 50.0])
def test_a_window_percent_out_of_bounds_is_refused_on_apply(percent: float) -> None:
    """Стережёт границы поля при «Применить»: файл правят и руками (Р-5).

    Движок проверяет только арифметику (0 < p < 100); 50 % из файла без этой
    проверки ушли бы в предел молча. Мутация: убрать проверку границ
    в `convert._time_exit_percent` — отказа нет, проверка падает.
    """
    with pytest.raises(ValueError, match="отступ предельной цены"):
        convert.engine_settings(
            Settings(price_step=25.0, time_exit_limit_pct=percent), WindowMode.REVERSE
        )
    edge = convert.engine_settings(
        Settings(price_step=25.0, time_exit_limit_pct=20.0), WindowMode.REVERSE
    )
    assert edge.time_exit_limit_percent == 20.0


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
    assert _close_of(limited).limit_price == 205_800.0


def test_the_limit_form_closes_at_the_window_end_even_with_the_box_unticked() -> None:
    """Стережёт: жёсткое закрытие не слушается снятой галочки.

    `LIMIT` сам значит «закрывать по концу окна всегда». Без этого алгоритм,
    требующий жёсткого выхода, держал бы позицию через окно при снятой
    галочке «Закрывать в конце окна».
    """
    settings = LIMIT.replace(close_on_time_end=False)
    order = _close_of(_at(_open(Side.LONG), settings))
    assert order.exit_reason is ExitReason.WINDOW_END
    assert order.limit_price == 205_800.0


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
    assert "205 800" in second.halt
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
    assert "205 800" in said[0].reason
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
    assert "заявка на выход с пределом движка 205 800" in reason


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
        {"time_exit_limit_percent": -1.0},
        {"time_exit_limit_percent": 0.0},
        {"time_exit_limit_percent": 100.0},
        {"time_exit_limit_percent": float("nan")},
        {"time_exit_wait_bars": 0},
        {"time_exit_wait_bars": 13},
        {"price_step": -1.0},
    ],
)
def test_impossible_time_exit_numbers_are_refused(changes: dict) -> None:
    """Стережёт границы: отступ вне (0; 100) %, ожидание вне 1…12, шаг < 0."""
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
