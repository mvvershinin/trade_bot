"""Порог выхода по обратному сигналу: не отдавать прибыль комиссии.

Что это за правило
------------------
Просьба владельца счёта 10.09.2026: обратный сигнал не должен закрывать
позицию, если её прибыль **есть, но меньше** того, что съест комиссия.
Исключение — конец сессии, там выходим всегда.

Правило живёт в движке и только в нём: торговый модуль про позицию, про деньги
и про комиссию не знает по построению (`ARCHITECTURE.md` §3). Настройка —
`EngineSettings.min_exit_profit_sides`, порог в **комиссиях одной стороны**,
умолчание **ноль**, то есть правила нет.

Чего эти проверки стерегут
--------------------------
Четыре вещи, и каждая — отдельный способ сделать из этой правки убыток:

1. **Выключено по умолчанию.** Порог не должен менять ни одной сделки, пока
   его не включили. Доказательство сильнее этого файла — сверка с прототипом
   127 из 127; здесь проверяется то же самое поведенчески.
2. **Убыток не удерживается.** Стопа у стратегии нет, и обратный сигнал —
   единственный способ выйти из минуса. Порог, применённый к убытку, дал бы
   робота, который не выходит никогда.
3. **Конец окна, нерабочий день и дневной лимит порог не задерживает.**
4. **Журнал не врёт.** На свече, где сигнал был и его придержали, строки
   «сигнала на выход нет» быть не должно: по журналу решений потом объясняют
   сделки.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import time

import pytest

from engine import (
    EngineSettings,
    EngineState,
    ExitReason,
    Mode,
    OrderAction,
    Side,
    TradingWindow,
    process_closed_candle,
)
from strategies import Intent
from tests.engine_helpers import bar, decision, position

WINDOW = TradingWindow(time(10, 5), time(11, 0))
#: Цена входа круглая: при объёме 1 и рубле в пункте прибыль в рублях равна
#: движению цены в пунктах, и все числа ниже читаются глазом.
PRICE = 100_000.0
INSIDE = (10, 10)
OUTSIDE = (11, 30)
TARIFF = 14.0

#: Порог выключен — умолчание программы. Тариф задан: без него сравнивать
#: клетки «включено» и «выключено» было бы нечестно, комиссия меняет деньги.
OFF = EngineSettings(
    mode=Mode.REVERSE, window=WINDOW, commission_per_side=TARIFF,
)
#: Порог включён на круговую комиссию самой позиции: 2 × 14 ₽ × 1 = 28 ₽.
ON = OFF.replace(min_exit_profit_sides=2.0)


def entered(side: Side = Side.LONG, *, volume: float = 1.0):
    """Открытая позиция без тейка: выйти из неё можно только сигналом."""
    return position(side, price=PRICE, volume=volume)


def against(side: Side) -> Intent:
    """Намерение, которое для этой стороны означает обратный сигнал."""
    return Intent.SHORT if side is Side.LONG else Intent.LONG


def play(
    side: Side,
    close: float,
    *,
    settings: EngineSettings = ON,
    when: tuple[int, int] = INSIDE,
    volume: float = 1.0,
    intent: Intent | None = None,
    day: int = 17,
):
    """Одна закрытая свеча с обратным сигналом по позиции этой стороны.

    ⚠️ День по умолчанию — среда 17.06.2026, а не пятница. В пятницу движок
    закрывает позицию перед выходными **последней свечой дня**, и проверка
    «вне окна порог не действует» зеленела бы по чужой причине.
    """
    return process_closed_candle(
        EngineState(position=entered(side, volume=volume)),
        bar(*when, close=close, day=day),
        decision(against(side) if intent is None else intent, close=close),
        settings,
    )


def exits(outcome) -> list[ExitReason | None]:
    """Причины выхода поданных заявок — пусто, если заявок на выход нет."""
    return [
        order.exit_reason for order in outcome.orders
        if order.action is OrderAction.CLOSE
    ]


def reasons(outcome) -> str:
    """Весь журнал свечи одной строкой — для проверок текста."""
    return " | ".join(f"{line.event}: {line.reason}" for line in outcome.journal)


# ---------------------------------------------------------------------------
# Умолчание: правила нет
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize("profit", [1.0, 10.0, 27.0, 28.0, 500.0])
def test_switched_off_the_threshold_changes_nothing(side: Side, profit: float) -> None:
    """Порог выключен — выход по обратному сигналу идёт при любой прибыли.

    **Главное условие правки.** Сверка с прототипом стоит на этом: у прототипа
    порога нет, и умолчание обязано вести себя ровно как он.
    """
    sign = 1 if side is Side.LONG else -1
    outcome = play(side, PRICE + sign * profit, settings=OFF)
    assert exits(outcome) == [ExitReason.SIGNAL], (
        "выключенный порог придержал выход — умолчание разошлось с прототипом"
    )


def test_the_default_settings_have_no_threshold() -> None:
    """Умолчание — ноль, то есть правила нет. Не «маленькое число»."""
    assert EngineSettings().min_exit_profit_sides == 0.0


# ---------------------------------------------------------------------------
# Включённый порог: держит только мелкую прибыль
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize("profit", [1.0, 10.0, 27.0])
def test_a_small_profit_is_held_back(side: Side, profit: float) -> None:
    """Прибыль есть, но меньше 28 ₽ — заявки на выход нет, позиция остаётся."""
    sign = 1 if side is Side.LONG else -1
    outcome = play(side, PRICE + sign * profit)
    assert exits(outcome) == []
    assert outcome.orders == ()
    assert outcome.state.position is not None
    assert outcome.state.position.is_open


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize("profit", [28.0, 29.0, 500.0])
def test_a_profit_that_covers_the_costs_leaves_at_once(
    side: Side, profit: float
) -> None:
    """Прибыль доросла до порога — выходим. Граница включительная.

    ⚠️ Ровно 28 ₽ **выпускает**: порог — это «не меньше чем», а не «больше
    чем». Издержки при такой сделке покрыты полностью, держать её дальше
    правило не просило.
    """
    sign = 1 if side is Side.LONG else -1
    outcome = play(side, PRICE + sign * profit)
    assert exits(outcome) == [ExitReason.SIGNAL]


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize("move", [-0.5, -1.0, -50.0, -5000.0])
def test_a_losing_position_is_never_held_by_the_threshold(
    side: Side, move: float
) -> None:
    """**Убыток порог не держит никогда.** Прямое условие задания.

    Стопа у стратегии нет: обратный сигнал — единственный способ закрыть
    минус. Порог, натянутый на убыток, дал бы робота, который не выходит
    из позиции никогда, и это была бы не экономия на комиссии, а потеря счёта.
    """
    sign = 1 if side is Side.LONG else -1
    outcome = play(side, PRICE + sign * move)
    assert exits(outcome) == [ExitReason.SIGNAL], (
        "порог придержал убыточную позицию — это запрещено прямо"
    )


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_a_position_exactly_at_the_entry_price_is_not_held(side: Side) -> None:
    """Ноль прибыли — не «мелкая прибыль», а её отсутствие. Выходим."""
    outcome = play(side, PRICE)
    assert exits(outcome) == [ExitReason.SIGNAL]


@pytest.mark.parametrize("volume", [1.0, 2.0, 7.0])
def test_the_verdict_does_not_depend_on_the_volume(volume: float) -> None:
    """Объём сокращается: и прибыль, и порог растут вместе с ним.

    ⚠️ Это **свойство, а не совпадение**, и его надо знать, читая замер.
    Прибыль позиции — `движение × объём`, порог — `комиссии × объём`;
    объём сокращается, и правило сводится к «движение цены в рублях
    за контракт против комиссии за контракт». Порог, записанный **рублями
    на позицию**, вёл бы себя иначе и разошёлся бы с объёмом молча — ровно
    поэтому единица измерения здесь комиссии.
    """
    assert exits(play(Side.LONG, PRICE + 27.0, volume=volume)) == []
    assert exits(play(Side.LONG, PRICE + 29.0, volume=volume)) == [ExitReason.SIGNAL]


def test_the_threshold_scales_with_the_tariff() -> None:
    """И с тарифом тоже: удвоенная комиссия — удвоенный порог."""
    dearer = ON.replace(commission_per_side=2 * TARIFF)
    assert exits(play(Side.LONG, PRICE + 40.0)) == [ExitReason.SIGNAL]
    assert exits(play(Side.LONG, PRICE + 40.0, settings=dearer)) == []


def test_four_sides_ask_for_the_reversal_to_pay_for_itself_too() -> None:
    """Значение 4 — круговая комиссия позиции и той, в которую перевернёмся.

    То есть 56 ₽ вместо 28 ₽.
    Развилка «с чем сравнивать прибыль» выражена **числом настройки**,
    а не зашита: обе трактовки проверяются одним и тем же кодом.
    """
    both = ON.replace(min_exit_profit_sides=4.0)
    assert exits(play(Side.LONG, PRICE + 40.0)) == [ExitReason.SIGNAL]
    assert exits(play(Side.LONG, PRICE + 40.0, settings=both)) == []


# ---------------------------------------------------------------------------
# Где порог не действует
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_the_end_of_the_window_ignores_the_threshold(side: Side) -> None:
    """Конец окна — выходим всегда. Прямое исключение из просьбы владельца.

    Свеча вне окна, прибыль 1 ₽: порог сказал бы «держим», а «закрывать
    в конце окна» сильнее. Позиция, удержанная порогом через ночь, — это
    риск на всю ночь ради 27 ₽.
    """
    sign = 1 if side is Side.LONG else -1
    outcome = play(side, PRICE + sign * 1.0, when=OUTSIDE)
    assert exits(outcome) == [ExitReason.WINDOW_END]


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_a_signal_exit_outside_the_window_ignores_the_threshold(side: Side) -> None:
    """И выход по средней вне окна при выключенном «закрывать в конце окна».

    Порог сторожит **шаг 7**, внутри окна. Вне окна робот сворачивается,
    и задерживать его там правило не просили.
    """
    settings = ON.replace(close_on_time_end=False)
    sign = 1 if side is Side.LONG else -1
    outcome = play(side, PRICE + sign * 1.0, settings=settings, when=OUTSIDE)
    assert exits(outcome) == [ExitReason.SIGNAL]


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_close_only_mode_ignores_the_threshold(side: Side) -> None:
    """«Только закрытие» — команда «сворачиваемся», и она сильнее арифметики.

    Удерживать позицию ради 28 ₽ в режиме, который включают, чтобы выйти
    из рынка, значило бы не выполнить прямую волю владельца счёта.
    """
    settings = ON.replace(mode=Mode.CLOSE_ONLY)
    sign = 1 if side is Side.LONG else -1
    outcome = play(side, PRICE + sign * 1.0, settings=settings)
    assert exits(outcome) == [ExitReason.SIGNAL]


def test_the_threshold_does_not_hold_a_closing_position() -> None:
    """Позиция в состоянии «закрывается» повторной заявки не получает.

    Порог тут ни при чём — это прежнее правило прототипа, и оно обязано
    пережить правку: вторая заявка после исполнения первой открыла бы
    позицию в обратную сторону.
    """
    closing = replace(entered(), state=type(entered().state).CLOSING)
    outcome = process_closed_candle(
        EngineState(position=closing),
        bar(*INSIDE, close=PRICE + 1.0),
        decision(Intent.SHORT, close=PRICE + 1.0),
        ON,
    )
    assert outcome.orders == ()


# ---------------------------------------------------------------------------
# Задержка кончается сама
# ---------------------------------------------------------------------------

def test_the_position_leaves_on_the_first_bar_where_the_profit_grows() -> None:
    """Придержали — и вышли на первой же свече, где прибыль доросла.

    Это и есть выбранная трактовка развилки «пропустить сигнал или запомнить
    его»: сигнал **пропускается**, а у этой стратегии он повторяется на каждой
    свече, пока цена по ту сторону средней. Отдельного события «выйти, когда
    дорастёт» движок не заводит — его нет ни у прототипа, ни в ТЗ.
    """
    state = EngineState(position=entered())
    seen: list[list[ExitReason | None]] = []
    for index, profit in enumerate([5.0, 12.0, 26.0, 40.0]):
        close = PRICE + profit
        outcome = process_closed_candle(
            state, bar(10, 10 + 5 * index, close=close),
            decision(Intent.SHORT, close=close), ON,
        )
        seen.append(exits(outcome))
        state = outcome.state
    assert seen == [[], [], [], [ExitReason.SIGNAL]]


def test_the_signal_gone_means_no_exit_and_no_memory_of_it() -> None:
    """Сигнал пропал — выхода нет, и отложенный сигнал не всплывает потом.

    Мутация «запомнить сигнал и выйти, как только прибыль дорастёт» роняет
    эту проверку: там выход случился бы на второй свече, где сигнала уже нет.
    """
    state = EngineState(position=entered())
    first = process_closed_candle(
        state, bar(10, 10, close=PRICE + 5.0),
        decision(Intent.SHORT, close=PRICE + 5.0), ON,
    )
    assert exits(first) == []
    second = process_closed_candle(
        first.state, bar(10, 15, close=PRICE + 500.0),
        decision(Intent.LONG, close=PRICE + 500.0), ON,
    )
    assert exits(second) == [], (
        "выход подан без обратного сигнала — движок завёл событие, "
        "которого нет ни у прототипа, ни в ТЗ"
    )


# ---------------------------------------------------------------------------
# Журнал
# ---------------------------------------------------------------------------

def test_holding_the_position_is_explained_in_the_journal() -> None:
    """Действие робота без строки в журнале — дефект (ТЗ §4.6).

    В строке обязаны быть **оба числа**: сколько даёт позиция и каков порог.
    «Выход отложен» без цифр — это код ошибки русскими словами.
    """
    said = reasons(play(Side.LONG, PRICE + 12.0))
    assert "Выход отложен" in said
    assert "12 ₽" in said, said
    assert "28 ₽" in said, said
    assert "14 ₽" in said, said


def test_the_journal_does_not_say_there_was_no_signal() -> None:
    """На свече с придержанным выходом строки «сигнала на выход нет» нет.

    ⚠️ Ровно этот дефект и был бы тихим: обе строки правдоподобны, обе
    объясняют молчание робота, и вторая утверждает, что сигнала не было, —
    а он был. По журналу решений потом объясняют сделки.
    """
    said = reasons(play(Side.LONG, PRICE + 12.0))
    assert "сигнала на выход нет" not in said, said


def test_the_journal_still_says_it_when_the_signal_really_is_absent() -> None:
    """Обратная сторона: без сигнала строка остаётся прежней.

    Иначе предыдущая проверка зеленела бы на движке, который вообще перестал
    объяснять удержание позиции.
    """
    said = reasons(play(Side.LONG, PRICE + 12.0, intent=Intent.LONG))
    assert "сигнала на выход нет" in said, said


def test_the_explanation_is_written_once_per_reason_not_once_per_bar() -> None:
    """Строка пишется на смене причины, а не каждые пять минут.

    Иначе один прогон по истории даёт тысячи одинаковых строк, и настоящие
    решения в них тонут — то же правило, что у остальных объяснений молчания.
    """
    state = EngineState(position=entered())
    written: list[int] = []
    for index in range(3):
        close = PRICE + 12.0
        outcome = process_closed_candle(
            state, bar(10, 10 + 5 * index, close=close),
            decision(Intent.SHORT, close=close), ON,
        )
        written.append(len(outcome.journal))
        state = outcome.state
    assert written[0] == 1, outcome.journal
    assert written[1:] == [0, 0], "объяснение повторяется каждую свечу"


# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

def test_a_threshold_without_a_tariff_is_refused_out_loud() -> None:
    """Порог без комиссии — отказ, а не тихое «не работает».

    Считать порог не от чего, а подставленный ноль объявил бы любую прибыль
    окупающей издержки, то есть выключил бы правило молча (правило 13
    `CLAUDE.md`: молчание дороже поломки).
    """
    with pytest.raises(ValueError, match="тариф комиссии движку не сообщён"):
        EngineSettings(mode=Mode.REVERSE, window=WINDOW, min_exit_profit_sides=2.0)


def test_a_negative_threshold_is_refused() -> None:
    """«Правила нет» выражается нулём, а не отрицательным числом."""
    with pytest.raises(ValueError, match="отрицательным"):
        EngineSettings(
            mode=Mode.REVERSE, window=WINDOW,
            commission_per_side=TARIFF, min_exit_profit_sides=-1.0,
        )


def test_a_threshold_that_is_not_a_number_is_refused() -> None:
    """Строка вместо числа — отказ по типу, а не падение где-то дальше."""
    with pytest.raises(TypeError, match="число комиссий"):
        EngineSettings(
            mode=Mode.REVERSE, window=WINDOW,
            commission_per_side=TARIFF, min_exit_profit_sides="два",  # type: ignore[arg-type]  # проверяется отказ на негодном типе
        )


def test_zero_is_allowed_without_a_tariff() -> None:
    """Ноль — это «правила нет», и тариф ему не нужен.

    Иначе отказ сломал бы сверку с прототипом: она идёт без тарифа
    в настройках движка на двух точках архива.
    """
    assert EngineSettings(window=WINDOW).min_exit_profit_sides == 0.0
