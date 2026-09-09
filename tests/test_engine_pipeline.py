"""Десять шагов обработки закрытой свечи: порядок, снимок, режимы, журнал.

Порядок шагов проверяется явно и по номерам. Он не падает при перестановке —
он просто даёт другие сделки при тех же настройках, поэтому «прочитать глазами»
здесь недостаточно (PROTOTYPE.md §2).

Отдельно и подробно проверено `skip_bar`: свеча, помеченная модулем, не доходит
до шага 5 и до шага 6. Ошибка «прочитал это как обычное „сигнала нет“» сверкой
с прототипом **не ловится** — на эталонном отрезке прогрев приходится на время,
когда позиции ещё нет, и оба шага пустые.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import time

import pytest

from engine import (
    EngineSettings,
    EngineState,
    ExitReason,
    Fill,
    JournalLevel,
    Mode,
    OrderAction,
    OrderRequest,
    Position,
    PositionState,
    Reversal,
    Side,
    Step,
    TradingWindow,
    after_submit,
    apply_fill,
    exit_signal,
    process_closed_candle,
)
from strategies import EmaReverseSettings, FactKind, Intent
from strategies.ema_reverse import describe
from tests.engine_helpers import MSK, bar, decision, position

WINDOW = TradingWindow(time(10, 5), time(11, 0))
WORKING = EngineSettings(mode=Mode.REVERSE, window=WINDOW)
#: Настройки без тейка. На них проверяется всё, что к тейку отношения не имеет:
#: иначе к каждой заявке добавляется вооружение, а к каждой сделке открытия —
#: заявка, рождённая ею. Две конфигурации архива с выключенным тейком — тоже
#: рабочие, а не вырожденные (PROTOTYPE.md §1).
NO_TAKE = WORKING.replace(take_profit=False)
INSIDE = (10, 10)          # свеча, закрывшаяся в 10:10 — внутри окна
OUTSIDE = (11, 30)         # закрывшаяся в 11:30 — снаружи


def market_orders(outcome) -> list[OrderAction]:
    """Только рыночные заявки: вооружение и снятие тейка — не сделки."""
    return [order.action for order in outcome.orders if order.action.is_market]


def closing(state: EngineState, reason: ExitReason, price: float):
    """Состояние с поданной заявкой на выход и сделка по ней.

    Причина выхода живёт в **заявке движка**, а не в сделке: у `Fill` поля
    причины нет вовсе (решение 0008, пункт 4). Значит собрать сделку на выход
    без заявки больше нельзя — и это ровно то, что проверяется.
    """
    assert state.position is not None
    order = OrderRequest(
        action=OrderAction.CLOSE, side=state.position.side,
        volume=state.position.volume, submitted_at=bar(*INSIDE).closes_at,
        reason="выход по сценарию теста", order_id="close:тест",
        exit_reason=reason,
    )
    fill = Fill(
        action=OrderAction.CLOSE, side=state.position.side,
        volume=state.position.volume, price=price,
        at=bar(10, 20).closes_at, order_id=order.order_id,
    )
    return replace(state, pending=(order,)), fill


def run(
    state: EngineState | None = None,
    *,
    settings: EngineSettings = WORKING,
    intent: Intent = Intent.LONG,
    when: tuple[int, int] = INSIDE,
    day: int = 19,
    **decision_kwargs: object,
):
    return process_closed_candle(
        EngineState() if state is None else state,
        bar(*when, day=day),
        decision(intent, **decision_kwargs),  # type: ignore[arg-type]
        settings,
    )


# --------------------------------------------------------------------------
# Порядок десяти шагов
# --------------------------------------------------------------------------

def test_a_full_pass_walks_all_ten_steps_in_order() -> None:
    """Свеча внутри окна, будни, режим реверс, позиции нет — проходятся все десять.

    Номера — из PROTOTYPE.md §2. Тест ловит и пропуск шага, и перестановку.
    """
    outcome = run()
    assert outcome.steps == (
        Step.MODE_OFF, Step.WARMUP, Step.SNAPSHOT, Step.CLOSE_TIME,
        Step.REARM_TAKE_PROFIT, Step.WINDOW, Step.CLOSE_BY_SIGNAL,
        Step.CLOSE_ONLY, Step.STOP_AFTER_TAKE, Step.OPEN,
    )
    assert [int(step) for step in outcome.steps] == list(range(1, 11))


@pytest.mark.parametrize(
    "case,settings,state,when,kwargs,last",
    [
        ("режим «Выключен»", EngineSettings(mode=Mode.OFF, window=WINDOW),
         None, INSIDE, {}, Step.MODE_OFF),
        ("прогрев", WORKING, None, INSIDE, {"skip_bar": True, "warmed_up": False},
         Step.WARMUP),
        ("вне окна", WORKING, None, OUTSIDE, {}, Step.WINDOW),
        ("режим «только закрытие»",
         EngineSettings(mode=Mode.CLOSE_ONLY, window=WINDOW), None, INSIDE, {},
         Step.CLOSE_ONLY),
    ],
)
def test_each_exit_stops_at_its_own_step(
    case: str, settings: EngineSettings, state, when, kwargs, last: Step
) -> None:
    """Каждый выход из обработки происходит ровно на своём шаге и не дальше."""
    outcome = run(state, settings=settings, when=when, **kwargs)
    assert outcome.last_step is last, case
    assert max(outcome.steps) == last, case


def test_stop_after_take_profit_exits_at_step_nine() -> None:
    """Шаг 9 стоит после закрытия и до открытия: выйти можно, войти уже нет.

    Саму дату тейка выставляет Э1-8; здесь проверяется место шага в порядке.
    """
    state = EngineState(take_profit_date=bar(*INSIDE).closes_at.date())
    outcome = run(state, settings=WORKING.replace(stop_after_take_profit=True))
    assert outcome.last_step is Step.STOP_AFTER_TAKE
    assert outcome.orders == ()
    assert "стоп после тейка" in outcome.journal[-1].reason


def test_stop_after_take_profit_can_be_switched_off() -> None:
    state = EngineState(take_profit_date=bar(*INSIDE).closes_at.date())
    outcome = run(state, settings=WORKING.replace(stop_after_take_profit=False))
    assert outcome.last_step is Step.OPEN
    assert len(outcome.orders) == 1


def test_the_step_numbers_never_go_backwards() -> None:
    """Шаги идут строго по возрастанию — ни один не выполняется дважды."""
    for settings in (WORKING, EngineSettings(mode=Mode.CLOSE_ONLY, window=WINDOW)):
        for when in (INSIDE, OUTSIDE):
            steps = run(settings=settings, when=when).steps
            assert list(steps) == sorted(set(steps)), (settings.mode, when)


# --------------------------------------------------------------------------
# skip_bar — свеча не идёт в обработку ЦЕЛИКОМ
# --------------------------------------------------------------------------

def test_a_skipped_bar_never_reaches_steps_five_and_six() -> None:
    """`skip_bar` — выход ДО перестановки тейка и ДО закрытия по концу окна.

    Это шаг 2 прототипа, а не «сигнала нет». Разница видна только тогда,
    когда позиция уже открыта: на эталонном отрезке прогрев приходится
    на время, когда позиции ещё нет, и сверка эту ошибку не поймает.
    """
    outcome = run(skip_bar=True, warmed_up=False)
    assert Step.REARM_TAKE_PROFIT not in outcome.steps
    assert Step.WINDOW not in outcome.steps
    assert outcome.steps == (Step.MODE_OFF, Step.WARMUP)


def test_a_skipped_bar_does_not_close_an_open_position_at_the_window_edge() -> None:
    """Прогрев с уже открытой позицией: выхода по концу окна НЕ происходит.

    Ровно тот случай, которого нет в эталонном датасете — прогон, начатый
    внутри открытой позиции. Если прочитать `skip_bar` как обычное
    «намерения нет», робот закроет позицию по концу окна на прогреве.
    """
    state = EngineState(position=position(Side.LONG))
    outcome = run(state, when=OUTSIDE, skip_bar=True, warmed_up=False)
    assert outcome.orders == ()
    assert outcome.state.position is not None
    assert outcome.state.position.state is PositionState.OPEN


def test_a_repeated_bar_is_skipped_although_the_warmup_is_long_past() -> None:
    """Повторно поданная свеча: прогрев позади, но обрабатывать её нельзя.

    Два поля, а не одно, именно поэтому: `warmed_up` истинно, `skip_bar` тоже.
    """
    state = EngineState(position=position(Side.LONG))
    outcome = run(state, when=OUTSIDE, skip_bar=True, warmed_up=True,
                  reason="Свеча подана повторно — уже учтена")
    assert outcome.orders == ()
    assert outcome.steps == (Step.MODE_OFF, Step.WARMUP)
    assert "повторно" in outcome.journal[0].reason


def test_a_bar_with_no_intent_is_processed_to_the_end() -> None:
    """`Intent.NONE` — это не `skip_bar`: обработка свечи продолжается.

    При закрытии ровно на средней нет ни входа, ни выхода — шестое исключение
    из «всегда в позиции» (PROTOTYPE.md §3). Но шаги 5 и 6 при этом
    выполняются: позиция на границе окна закроется.
    """
    outcome = run(intent=Intent.NONE)
    assert outcome.steps[-1] is Step.OPEN
    assert outcome.orders == ()

    state = EngineState(position=position(Side.LONG))
    edge = run(state, intent=Intent.NONE, when=OUTSIDE)
    assert [order.exit_reason for order in edge.orders] == [ExitReason.WINDOW_END]


# --------------------------------------------------------------------------
# Снимок позиций — шаг 3, ДО закрытия
# --------------------------------------------------------------------------

def test_the_snapshot_is_the_position_as_it_was_before_the_exit_order() -> None:
    """Снимок берётся до закрытия: в нём позиция ещё «открыта».

    После шага 7 состояние движка говорит «закрывается», а снимок —
    «открыта». На шаге 10 спрашивают снимок, и потому вход в обратную
    сторону не подаётся.
    """
    state = EngineState(position=position(Side.LONG))
    outcome = run(state, intent=Intent.SHORT)
    assert outcome.snapshot is not None
    assert outcome.snapshot.state is PositionState.OPEN
    assert outcome.state.position is not None
    assert outcome.state.position.state is PositionState.CLOSING


def test_a_reverse_signal_submits_only_the_exit_on_this_candle() -> None:
    """Обратный сигнал: подаётся ОДНА заявка — на выход. Вход не подаётся.

    Это и есть «переворот через свечу»: снимок не пуст, шаг 10 не срабатывает.
    """
    state = EngineState(position=position(Side.LONG))
    outcome = run(state, intent=Intent.SHORT)
    assert len(outcome.orders) == 1
    order = outcome.orders[0]
    assert order.action is OrderAction.CLOSE
    assert order.side is Side.LONG
    assert order.exit_reason is ExitReason.SIGNAL
    assert Step.OPEN in outcome.steps, "шаг 10 выполняется, но входа не даёт"


def test_a_closing_position_blocks_entry_on_the_following_candle_too() -> None:
    """Пока сделки не было, позиция числится открытой и держит рынок занятым.

    Заявка подана, исполнения ещё нет. Снимок не пуст — входа нет,
    и повторной заявки на выход тоже нет.
    """
    state = EngineState(position=position(Side.LONG, state=PositionState.CLOSING))
    outcome = run(state, intent=Intent.SHORT)
    assert outcome.orders == ()
    assert outcome.state.position is not None


def test_entry_returns_only_after_the_exit_deal_has_arrived() -> None:
    """Три свечи подряд: выход подан, сделка пришла, вход подан.

    Между выходом и входом ровно одна свеча — так делает прототип.
    """
    state = EngineState(position=position(Side.LONG))

    first = run(state, settings=NO_TAKE, intent=Intent.SHORT)
    assert [order.action for order in first.orders] == [OrderAction.CLOSE]

    filled = apply_fill(first.state, Fill(
        action=OrderAction.CLOSE, side=Side.LONG, volume=1.0, price=99.0,
        at=bar(10, 15).closes_at, order_id=first.orders[0].order_id,
    ), NO_TAKE)
    assert filled.state.position is None

    second = run(filled.state, settings=NO_TAKE, intent=Intent.SHORT, when=(10, 15))
    assert [order.action for order in second.orders] == [OrderAction.OPEN]
    assert second.orders[0].side is Side.SHORT


def test_a_held_position_without_a_reverse_signal_produces_no_orders() -> None:
    """Сигнала на выход нет — позиция держится, заявок нет, но строка есть."""
    state = EngineState(position=position(Side.LONG))
    outcome = run(state, settings=NO_TAKE, intent=Intent.LONG)
    assert outcome.orders == ()
    assert outcome.journal, "робот молчит без объяснения — это дефект журнала"
    assert "держим" in outcome.journal[0].event


# --------------------------------------------------------------------------
# Шаг 6 — окно и выходные
# --------------------------------------------------------------------------

def test_outside_the_window_the_position_is_closed_to_cash() -> None:
    """Закрытие по концу окна идёт без оглядки на среднюю.

    Намерение здесь «лонг», то есть сигнала на выход из лонга нет, —
    и позиция всё равно закрывается.
    """
    state = EngineState(position=position(Side.LONG))
    outcome = run(state, intent=Intent.LONG, when=OUTSIDE)
    assert [order.exit_reason for order in outcome.orders] == [ExitReason.WINDOW_END]
    assert outcome.last_step is Step.WINDOW


def test_the_engine_cancels_an_intent_as_the_module_promises() -> None:
    """Оговорка модуля «общие настройки могут отменить намерение» — исполняется.

    Описание торгового модуля кончается фразой про то, что войдёт ли робот
    на самом деле, решают общие настройки. Это утверждение про **движок**,
    и сам модуль исполнить его не может: слой стратегий про `engine/` не знает
    (ARCHITECTURE.md §2). Поэтому факт объявлен модулем
    (`FactKind.ENGINE_MAY_OVERRIDE`), а исполняется здесь — передача записана
    в `tests/test_strategies_description.py::DELEGATED_FACTS` и сверяется
    списком.

    Исполняется буквально: движку подаётся намерение «лонг» из пустой позиции,
    и ровно одна общая настройка — торговое окно — отменяет его. Ни одной
    рыночной заявки не подаётся.

    Мутация, обязанная ронять проверку: убрать из описания фразу «может
    отменить любое намерение» (`_BOUNDARY_NOTE` → «ни одна общая настройка
    намерение не отменит»). 09.09.2026 такая подмена проходила молча
    (`B-041`).
    """
    outcome = run(intent=Intent.LONG, when=OUTSIDE)
    assert market_orders(outcome) == [], (
        "намерение «лонг» вне торгового окна дошло до заявки — тогда фраза "
        "описания про общие настройки была бы неправдой"
    )
    assert outcome.last_step is Step.WINDOW

    fact = describe(EmaReverseSettings()).fact(FactKind.ENGINE_MAY_OVERRIDE)
    assert fact is not None, (
        "модуль перестал объявлять факт про общие настройки — описание "
        "правила читалось бы как полное правило поведения робота"
    )
    assert fact.answer in fact.text, (
        f"факт про общие настройки отвечает «{fact.answer}», а в его тексте "
        f"этого нет: «{fact.text}»"
    )


def test_with_closing_at_the_window_end_switched_off_only_the_signal_closes() -> None:
    """«Закрывать в конце окна» выключено — выход только по сигналу средней.

    Ветка восстановлена по исходнику прототипа и **данными не подкреплена**
    (PROTOTYPE.md §9): в единственном полном журнале позиция ночь не переживает.

    ⚠️ День здесь **четверг**, а не умолчательная пятница, и это не мелочь:
    перед нерабочим днём позиция закрывается независимо от этой галочки
    (решение 0038), и на пятнице проверка проверяла бы уже не то, ради чего
    поставлена. Соседний тест ниже стережёт ровно пятницу.
    """
    settings = NO_TAKE.replace(close_on_time_end=False)
    state = EngineState(position=position(Side.LONG))

    holding = run(state, settings=settings, intent=Intent.LONG, when=OUTSIDE, day=18)
    assert market_orders(holding) == []
    assert holding.state.position is not None

    closing = run(state, settings=settings, intent=Intent.SHORT, when=OUTSIDE, day=18)
    assert [
        order.exit_reason for order in closing.orders if order.action.is_market
    ] == [ExitReason.SIGNAL]


def test_the_position_leaves_before_a_non_trading_day_even_with_the_switch_off() -> None:
    """Перед нерабочим днём позиция закрывается, даже если галочка снята.

    Стережёт решение 0038, следствие 1: «закрывать в конце окна» можно
    выключить, и тогда позиция живёт до обратного сигнала — но **не через
    нерабочий день**. Иначе снятая галочка молча отменила бы решение
    владельца счёта.

    19.06.2026 — пятница, сигнала на выход нет (намерение «лонг» при лонге),
    окно позади. Завтра суббота.
    """
    settings = NO_TAKE.replace(close_on_time_end=False)
    state = EngineState(position=position(Side.LONG))

    outcome = run(state, settings=settings, intent=Intent.LONG, when=OUTSIDE, day=19)

    assert [
        order.exit_reason for order in outcome.orders if order.action.is_market
    ] == [ExitReason.NON_TRADING_DAY]


def test_the_farewell_exit_names_the_coming_day_and_not_the_end_of_the_window() -> None:
    """Причина выхода перед нерабочим днём отличается от конца окна — словами.

    Решение 0038, следствие 2: человек обязан видеть, что закрыли не по
    расписанию окна. Проверяется и причина заявки, и строка журнала.
    """
    settings = NO_TAKE.replace(close_on_time_end=False)
    state = EngineState(position=position(Side.LONG))

    outcome = run(state, settings=settings, intent=Intent.LONG, when=OUTSIDE, day=19)

    said = " ".join(f"{entry.event} {entry.reason}" for entry in outcome.journal)
    assert "перед нерабочим днём" in said
    assert "суббота" in said
    assert outcome.orders[0].exit_reason is not ExitReason.WINDOW_END


def test_with_the_switch_on_the_reason_stays_the_end_of_the_window() -> None:
    """Включённая галочка — причина прежняя, хотя завтра и выходной.

    Обратная сторона решения 0038: выход в этом случае произошёл бы и без
    всякого календаря, по расписанию окна. Подмена причины каждую пятницу
    означала бы новую строку журнала о старом, ничуть не изменившемся событии.
    """
    state = EngineState(position=position(Side.LONG))

    outcome = run(state, settings=NO_TAKE, intent=Intent.LONG, when=OUTSIDE, day=19)

    assert [
        order.exit_reason for order in outcome.orders if order.action.is_market
    ] == [ExitReason.WINDOW_END]


def test_before_the_window_the_position_is_not_closed_early_on_a_short_day() -> None:
    """Закрытие перед нерабочим днём происходит ПОСЛЕ окна, а не с утра.

    Свеча 09:30 пятницы тоже «вне окна», но окно ещё впереди. Закрыться на
    ней значило бы отдать целый торговый день: позиция, доставшаяся с вечера
    четверга, исчезла бы до того, как робот вообще начал работать.
    """
    settings = NO_TAKE.replace(close_on_time_end=False)
    state = EngineState(position=position(Side.LONG))

    outcome = run(state, settings=settings, intent=Intent.LONG, when=(9, 30), day=19)

    assert market_orders(outcome) == []
    assert outcome.state.position is not None


def test_outside_the_window_a_closing_position_gets_no_second_exit_order() -> None:
    """Заявка на выход уже подана — вне окна вторая не подаётся.

    На истории это безобидно: заявка исполняется первым же тиком следующей
    свечи. В бою вторая рыночная заявка после исполнения первой **открывает
    позицию в обратную сторону** (PROTOTYPE.md §2, уточнение 1).

    Ветка журнала здесь до Э1-8 не выполнялась ни разу — она числилась
    непроверенным текстом на пути, по которому идут деньги.
    """
    closing = replace(position(Side.LONG), state=PositionState.CLOSING)
    outcome = run(EngineState(position=closing), settings=NO_TAKE, when=OUTSIDE)
    assert outcome.orders == ()
    assert outcome.journal[0].event == "Торговли нет"
    assert "уже подана" in outcome.journal[0].reason


def test_outside_the_window_with_no_position_nothing_happens() -> None:
    outcome = run(when=OUTSIDE)
    assert outcome.orders == ()
    assert outcome.last_step is Step.WINDOW
    assert "вне окна" in outcome.journal[0].reason


def test_a_weekend_candle_closes_the_position_and_blocks_entry() -> None:
    """Выходной — та же ветка шага 6, что и «вне окна». 20.06.2026 — суббота."""
    state = EngineState(position=position(Side.LONG))
    outcome = run(state, settings=NO_TAKE, intent=Intent.LONG, when=INSIDE, day=20)
    assert [order.exit_reason for order in outcome.orders] == [ExitReason.WINDOW_END]
    assert "суббота" in outcome.journal[0].reason

    empty = run(when=INSIDE, day=20)
    assert empty.orders == ()
    assert empty.last_step is Step.WINDOW


def test_weekend_trading_can_be_switched_on() -> None:
    settings = WORKING.replace(trade_in_weekend=True)
    outcome = run(settings=settings, when=INSIDE, day=20)
    assert outcome.last_step is Step.OPEN
    assert [order.action for order in outcome.orders] == [OrderAction.OPEN]


# --------------------------------------------------------------------------
# Режимы: шаги 1, 8 и ограничение стороны на шаге 10
# --------------------------------------------------------------------------

def test_switched_off_does_not_close_an_open_position() -> None:
    """«Выключен» позицию не закрывает — намеренно, и говорит об этом громко.

    Выключение не должно само по себе приводить к сделке (DOMAIN.md §3).
    Но тихое выключение с оставленной позицией — дефект, поэтому строка
    журнала здесь уровня «предупреждение».
    """
    settings = EngineSettings(mode=Mode.OFF, window=WINDOW)
    state = EngineState(position=position(Side.LONG))
    outcome = run(state, settings=settings, intent=Intent.SHORT)
    assert outcome.orders == ()
    assert outcome.state.position == state.position
    assert outcome.journal[0].level is JournalLevel.WARNING
    assert "не закрывается" in outcome.journal[0].reason


def test_close_only_closes_by_signal_but_never_enters() -> None:
    """«Только закрытие»: шаг 7 отрабатывает, шаг 8 останавливает обработку."""
    settings = EngineSettings(mode=Mode.CLOSE_ONLY, window=WINDOW)
    state = EngineState(position=position(Side.LONG))

    closing = run(state, settings=settings, intent=Intent.SHORT)
    assert [order.exit_reason for order in closing.orders] == [ExitReason.SIGNAL]
    assert closing.last_step is Step.CLOSE_ONLY

    empty = run(settings=settings, intent=Intent.LONG)
    assert empty.orders == ()
    assert Step.OPEN not in empty.steps


@pytest.mark.parametrize(
    "mode,allowed,blocked",
    [
        (Mode.LONG_ONLY, Intent.LONG, Intent.SHORT),
        (Mode.SHORT_ONLY, Intent.SHORT, Intent.LONG),
    ],
)
def test_one_sided_modes_open_only_their_own_side(
    mode: Mode, allowed: Intent, blocked: Intent
) -> None:
    """«Только лонг» и «только шорт» ограничивают вход, но не выход."""
    settings = EngineSettings(mode=mode, window=WINDOW)
    opened = run(settings=settings, intent=allowed)
    assert [order.action for order in opened.orders] == [OrderAction.OPEN]

    refused = run(settings=settings, intent=blocked)
    assert refused.orders == ()
    assert "режим" in refused.journal[0].reason.lower()


def test_a_one_sided_mode_still_closes_the_opposite_position() -> None:
    """Переключились на «только лонг» — открытый шорт закроется по сигналу."""
    settings = EngineSettings(mode=Mode.LONG_ONLY, window=WINDOW)
    state = EngineState(position=position(Side.SHORT))
    outcome = run(state, settings=settings, intent=Intent.LONG)
    assert [order.exit_reason for order in outcome.orders] == [ExitReason.SIGNAL]


# --------------------------------------------------------------------------
# Условие входа и выхода
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "side,intent,expected",
    [
        (Side.LONG, Intent.SHORT, True),
        (Side.LONG, Intent.LONG, False),
        (Side.LONG, Intent.NONE, False),
        (Side.SHORT, Intent.LONG, True),
        (Side.SHORT, Intent.SHORT, False),
        (Side.SHORT, Intent.NONE, False),
    ],
)
def test_exit_signal_maps_intent_to_the_position_side(
    side: Side, intent: Intent, expected: bool
) -> None:
    """Выход из лонга — намерение «шорт», и наоборот. `NONE` выходом не является."""
    assert exit_signal(side, intent) is expected


def test_entry_volume_comes_from_the_settings() -> None:
    outcome = run(settings=WORKING.replace(volume=3.0))
    assert outcome.orders[0].volume == 3.0
    assert "3 контракта" in outcome.orders[0].reason


# --------------------------------------------------------------------------
# Сделки исполнителя
# --------------------------------------------------------------------------

def test_an_entry_deal_creates_the_position_at_the_deal_price_and_time() -> None:
    """Цену и время сделки назначает исполнитель, а не движок."""
    moment = bar(10, 15).closes_at
    entry = run(intent=Intent.SHORT, settings=NO_TAKE.replace(volume=2.0)).orders[0]
    result = apply_fill(EngineState(pending=(entry,)), Fill(
        action=OrderAction.OPEN, side=Side.SHORT, volume=2.0, price=284_800.0,
        at=moment, order_id=entry.order_id,
    ), NO_TAKE)
    assert result.state.position is not None
    assert result.state.position.side is Side.SHORT
    assert result.state.position.entry_price == 284_800.0
    assert result.state.position.entry_time == moment
    assert result.journal[0].level is JournalLevel.TRADE


def test_an_exit_deal_reports_the_result_in_points() -> None:
    """Строка журнала считает результат и честно говорит про комиссию."""
    state = EngineState(position=position(Side.LONG, price=284_800.0, volume=2.0))
    result = apply_fill(*closing(state, ExitReason.WINDOW_END, 285_000.0), NO_TAKE)
    assert result.state.position is None
    reason = result.journal[0].reason
    assert "+400" in reason
    assert "конец торгового окна" in reason
    assert "не сообщена" in reason, "нулевая комиссия молча — это неправда в отчёте"


def test_a_reported_commission_shows_up_as_its_own_number() -> None:
    state = EngineState(position=position(Side.SHORT, price=100.0))
    state, fill = closing(state, ExitReason.SIGNAL, 90.0)
    result = apply_fill(state, replace(fill, commission=28.0), NO_TAKE)
    assert "+10" in result.journal[0].reason
    assert "Комиссия 28 ₽" in result.journal[0].reason


def test_an_unexpected_deal_does_not_change_the_state() -> None:
    """Сделка, которой движок не ждал, пишется ошибкой и ничего не меняет.

    Подстроиться под неожиданный ответ брокера молча — худшее, что здесь
    можно сделать: расхождение разбирается сверкой позиции (этап 2).
    """
    orphan = apply_fill(EngineState(), Fill(
        action=OrderAction.CLOSE, side=Side.LONG, volume=1.0, price=100.0,
        at=bar(10, 20).closes_at, order_id="close:long:чужая",
    ), NO_TAKE)
    assert orphan.state == EngineState()
    assert orphan.journal[0].level is JournalLevel.ERROR

    entry = run(intent=Intent.SHORT, settings=NO_TAKE).orders[0]
    state = EngineState(position=position(Side.LONG), pending=(entry,))
    twice = apply_fill(state, Fill(
        action=OrderAction.OPEN, side=Side.SHORT, volume=1.0, price=100.0,
        at=bar(10, 20).closes_at, order_id=entry.order_id,
    ), NO_TAKE)
    assert twice.state == state
    assert twice.journal[0].level is JournalLevel.ERROR


# --------------------------------------------------------------------------
# Журнал решений
# --------------------------------------------------------------------------

def test_every_order_carries_a_line_in_plain_language() -> None:
    """Действие робота без строки в журнале — дефект, даже если оно верное."""
    cases = [
        run(EngineState(), intent=Intent.LONG),
        run(EngineState(position=position(Side.LONG)), intent=Intent.SHORT),
        run(EngineState(position=position(Side.LONG)), intent=Intent.LONG,
            when=OUTSIDE),
    ]
    for outcome in cases:
        assert outcome.orders
        assert len(outcome.journal) >= len(outcome.orders)
        for entry in outcome.journal:
            assert entry.event and entry.reason
            assert "=" not in entry.event, "код вместо фразы в журнале решений"


def test_a_repeated_reason_for_silence_is_not_written_twice() -> None:
    """Одна и та же причина молчания не повторяется на каждой свече.

    Иначе прогон по истории даёт десять тысяч одинаковых строк «вне окна»,
    и настоящие события в них тонут.
    """
    first = run(when=OUTSIDE)
    assert len(first.journal) == 1
    second = run(first.state, when=OUTSIDE)
    assert second.journal == ()


def test_the_reason_is_written_again_when_it_changes() -> None:
    """Смена причины молчания — новая строка. Выходные и «вне окна» различаются."""
    outside = run(when=OUTSIDE)
    weekend = run(outside.state, when=INSIDE, day=20)
    assert len(weekend.journal) == 1
    assert "суббота" in weekend.journal[0].reason


def test_an_action_resets_the_silence_so_the_next_reason_is_written() -> None:
    quiet = run(when=OUTSIDE)
    acted = run(quiet.state, intent=Intent.LONG)
    assert acted.orders
    assert acted.state.quiet_note == ""
    again = run(acted.state, when=OUTSIDE)
    assert len(again.journal) == 1


def test_journal_entries_are_stamped_with_the_candle_close_time() -> None:
    """Время строки — закрытие свечи, а не системные часы.

    У движка своих часов нет: функция, которая посмотрит на `datetime.now()`,
    перестанет одинаково работать на истории и в бою.
    """
    outcome = run()
    assert all(entry.at == bar(*INSIDE).closes_at for entry in outcome.journal)
    assert outcome.closes_at.tzinfo is not None
    assert outcome.closes_at.astimezone(MSK).hour == 10


# --------------------------------------------------------------------------
# Закрывающаяся позиция: ни второй заявки, ни входа — во всех трёх ветках
# --------------------------------------------------------------------------
# Это самые опасные ветки разбора и до Э1-7 они не вызывались ни одним тестом.
# Общее у них: заявка на выход уже у исполнителя, сделки по ней ещё не было.
# Вторая рыночная заявка после исполнения первой не «закрывает ещё раз» —
# она ОТКРЫВАЕТ позицию в обратную сторону (PROTOTYPE.md §2, уточнение 1).

def test_a_closing_position_outside_the_window_gets_no_second_order() -> None:
    """Вне окна, «закрывать в конце окна» включено, заявка уже подана."""
    state = EngineState(position=position(Side.LONG, state=PositionState.CLOSING))
    outcome = run(state, intent=Intent.LONG, when=OUTSIDE)
    assert outcome.orders == ()
    assert outcome.last_step is Step.WINDOW
    assert outcome.state.position is not None, "позиция исчезла без сделки"
    assert "уже подана" in outcome.journal[0].reason


def test_a_closing_position_outside_the_window_is_left_alone_by_the_signal_too() -> None:
    """То же при выключенном «закрывать в конце окна» и сигнале на выход.

    Сигнал средней здесь есть (лонг против намерения «шорт»), и без проверки
    состояния вторая заявка ушла бы именно тут.
    """
    settings = WORKING.replace(close_on_time_end=False)
    state = EngineState(position=position(Side.LONG, state=PositionState.CLOSING))
    outcome = run(state, settings=settings, intent=Intent.SHORT, when=OUTSIDE)
    assert outcome.orders == ()
    assert outcome.state.position is not None
    assert outcome.state.position.state is PositionState.CLOSING


def test_switched_off_over_a_closing_position_says_the_order_is_still_out_there() -> None:
    """«Выключен» поверх поданной заявки: позиция остаётся, и об этом сказано.

    Робот за исполнением больше не следит — сделка придёт, а узнать о ней
    будет неоткуда, кроме приложения брокера. Общая строка «позиция осталась»
    про висящую заявку умалчивает, поэтому строка отдельная.
    """
    settings = EngineSettings(mode=Mode.OFF, window=WINDOW)
    state = EngineState(position=position(Side.LONG, state=PositionState.CLOSING))
    outcome = run(state, settings=settings, intent=Intent.SHORT)
    assert outcome.orders == ()
    assert outcome.last_step is Step.MODE_OFF
    assert outcome.state.position is not None
    assert outcome.state.position.state is PositionState.CLOSING
    assert outcome.journal[0].level is JournalLevel.WARNING
    assert "заявка на выход уже подана" in outcome.journal[0].reason


# --------------------------------------------------------------------------
# Два состояния разбора: подача заявки идёт ДО того, как движок себе запишет
# --------------------------------------------------------------------------

def test_the_analysis_returns_the_state_for_both_answers_of_the_executor() -> None:
    """Разбор не решает, чем кончится подача: он возвращает оба исхода.

    Записать «позиция закрывается» до ответа исполнителя — значит при отказе
    брокера получить позицию, закрытую в журнале и открытую на счёте.
    """
    state = EngineState(position=position(Side.LONG))
    outcome = run(state, settings=NO_TAKE, intent=Intent.SHORT)
    assert len(outcome.orders) == 1

    refused = outcome.state_if_refused
    assert refused is not None
    assert refused.position is not None
    assert refused.position.state is PositionState.OPEN, (
        "состояние «заявка не принята» уже помечено закрывающимся"
    )
    assert refused.pending == ()

    assert outcome.state.position is not None
    assert outcome.state.position.state is PositionState.CLOSING
    assert outcome.state.pending == (outcome.orders[0],)


def test_the_two_states_are_the_same_thing_counted_from_two_ends() -> None:
    """`state` — это `state_if_refused` плюс последствия всех заявок.

    Инвариант держит `after_submit`: одна функция и для разбора, и для
    адаптера. Две копии этого правила разъехались бы молча.
    """
    state = EngineState(position=position(Side.LONG))
    outcome = run(state, intent=Intent.SHORT)

    replayed = outcome.state_if_refused
    assert replayed is not None
    for order in outcome.orders:
        replayed = after_submit(replayed, order)
    assert replayed == outcome.state

    assert outcome.state_after(0) == outcome.state_if_refused
    assert outcome.state_after(len(outcome.orders)) == outcome.state
    with pytest.raises(ValueError, match="такого ответа исполнителя не бывает"):
        outcome.state_after(len(outcome.orders) + 1)


def test_a_candle_without_orders_gives_the_same_state_either_way() -> None:
    """Заявок нет — принимать нечего, и оба состояния совпадают."""
    outcome = run(intent=Intent.NONE)
    assert outcome.orders == ()
    assert outcome.state_after(0) == outcome.state


# --------------------------------------------------------------------------
# Неподтверждённая заявка на вход: второй раз движок не входит
# --------------------------------------------------------------------------

def test_a_submitted_entry_is_not_submitted_again_while_the_deal_is_missing() -> None:
    """Подтверждение входа потерялось — второй заявки нет.

    Позиция появляется только по сделке. Без памяти о поданной заявке снимок
    на следующей свече снова пуст, и движок входит второй раз: двойной объём
    на счёте при одном сигнале.
    """
    first = run(EngineState(), settings=NO_TAKE, intent=Intent.LONG)
    assert [order.action for order in first.orders] == [OrderAction.OPEN]
    assert first.state.pending

    second = run(first.state, settings=NO_TAKE, intent=Intent.LONG, when=(10, 15))
    assert second.orders == (), "движок вошёл второй раз по той же заявке"
    assert second.last_step is Step.OPEN
    assert second.journal[0].event == "Ждём исполнения входа"
    assert "двойной объём" in second.journal[0].reason


def test_the_deal_releases_the_hold_and_the_engine_can_act_again() -> None:
    """Сделка пришла — заявка перестаёт быть неподтверждённой."""
    first = run(EngineState(), settings=NO_TAKE, intent=Intent.LONG)
    filled = apply_fill(first.state, Fill(
        action=OrderAction.OPEN, side=Side.LONG, volume=1.0, price=100.0,
        at=bar(10, 15).closes_at, order_id=first.orders[0].order_id,
    ), NO_TAKE)
    assert filled.state.pending == ()
    assert filled.state.position is not None

    exit_now = run(filled.state, settings=NO_TAKE, intent=Intent.SHORT, when=(10, 15))
    assert [order.action for order in exit_now.orders] == [OrderAction.CLOSE]


# --------------------------------------------------------------------------
# Сделка, которой движок не ждал: сверяются сторона и объём, а не только факт
# --------------------------------------------------------------------------

def test_a_hand_made_close_from_the_brokers_app_does_not_touch_our_position() -> None:
    """Владелец счёта закрыл руками свой шорт — наш лонг остаётся нашим.

    Такая сделка не ссылается ни на одну заявку движка, потому что движок её
    не подавал. Приняв её, движок обнулил бы лонг, посчитал результат
    по формуле лонга, написал «Лонг закрыт» и вошёл заново на следующей
    свече. На счёте оказалось бы два контракта.
    """
    state = EngineState(position=position(Side.LONG, volume=1.0))
    result = apply_fill(state, Fill(
        action=OrderAction.CLOSE, side=Side.SHORT, volume=1.0, price=100.0,
        at=bar(10, 20).closes_at,
    ), NO_TAKE)
    assert result.state == state, "чужая сделка изменила состояние движка"
    assert result.journal[0].level is JournalLevel.ERROR
    assert "без имени" in result.journal[0].reason


def test_an_exit_deal_on_the_other_side_does_not_close_our_position() -> None:
    """Заявка своя, а позиция у движка другой стороны — сделка не применяется.

    Так выглядит расхождение с брокером: заявка на выход из лонга исполнилась,
    а движок к этому моменту числит шорт. Списать позицию по такой сделке —
    значит остаться в рынке, считая, что вышел.
    """
    state = EngineState(position=position(Side.LONG, volume=1.0))
    state, fill = closing(state, ExitReason.SIGNAL, 100.0)
    drifted = replace(state, position=position(Side.SHORT, volume=1.0))
    result = apply_fill(drifted, fill, NO_TAKE)
    assert result.state == drifted, "чужая сделка изменила состояние движка"
    assert result.journal[0].level is JournalLevel.ERROR
    assert "не по той стороне" in result.journal[0].event


def test_a_partial_exit_deal_does_not_close_the_whole_position() -> None:
    """Исполнено 3 из 5 — позиция не исчезает, и в журнале не написано «5»."""
    state = EngineState(position=position(Side.LONG, volume=5.0))
    state, fill = closing(state, ExitReason.SIGNAL, 100.0)
    result = apply_fill(state, replace(fill, volume=3.0), NO_TAKE)
    assert result.state == state
    assert result.state.position is not None
    assert result.state.position.volume == 5.0
    assert result.journal[0].level is JournalLevel.ERROR
    assert "3 контракта" in result.journal[0].reason
    assert "5 контрактов" in result.journal[0].reason


def test_an_entry_deal_that_contradicts_the_submitted_order_is_refused() -> None:
    """Подавали вход в лонг, пришёл шорт — состояние не меняется."""
    order = OrderRequest(
        action=OrderAction.OPEN, side=Side.LONG, volume=1.0,
        submitted_at=bar(*INSIDE).closes_at, reason="Закрытие выше средней → лонг",
        order_id="open:long:проверка",
    )
    state = EngineState(pending=(order,))
    result = apply_fill(state, Fill(
        action=OrderAction.OPEN, side=Side.SHORT, volume=1.0, price=100.0,
        at=bar(10, 15).closes_at, order_id=order.order_id,
    ), NO_TAKE)
    assert result.state == state
    assert result.state.position is None
    assert result.journal[0].level is JournalLevel.ERROR


def test_a_deal_without_a_submitted_order_is_a_discrepancy_now() -> None:
    """Заявки нет вовсе — сделка **не применяется**. Поведение изменено Э1-8.

    До решения 0008 такая сделка принималась: «движок мог быть перезапущен».
    Теперь сопоставление идёт по имени заявки, и из него следует обратное —
    сделка, не сопоставившаяся ни с одной заявкой в полёте, это расхождение,
    и оно уходит в сверку, а не применяется молча (решение 0008, пункт 6).

    Цена смены: закрытие руками из приложения брокера больше не подхватывается
    движком автоматически. Это и было целью — подхватывать чужую сделку значит
    списать свою позицию по чужому событию.
    """
    result = apply_fill(EngineState(), Fill(
        action=OrderAction.OPEN, side=Side.LONG, volume=1.0, price=100.0,
        at=bar(10, 15).closes_at,
    ), NO_TAKE)
    assert result.state == EngineState()
    assert result.journal[0].level is JournalLevel.ERROR
    assert "без имени" in result.journal[0].reason


def test_a_deal_with_no_volume_or_a_nan_price_never_becomes_a_position() -> None:
    """Разбор — на границе значения, а не через свечу изнутри обработки.

    Сделка нулевого объёма создавала бы позицию, по которой нельзя подать
    ни одной заявки, и отказ вылезал бы на следующей свече внутри разбора.
    Цена `nan` не падает вовсе — она расползается по результату в пунктах.
    """
    moment = bar(10, 15).closes_at
    with pytest.raises(ValueError, match="объём сделки"):
        Fill(OrderAction.OPEN, Side.LONG, 0.0, 100.0, moment)
    with pytest.raises(ValueError, match="цена сделки"):
        Fill(OrderAction.OPEN, Side.LONG, 1.0, float("nan"), moment)
    with pytest.raises(ValueError, match="объём позиции"):
        Position(side=Side.LONG, volume=0.0, entry_price=100.0, entry_time=moment)
    with pytest.raises(ValueError, match="цена входа"):
        Position(
            side=Side.LONG, volume=1.0, entry_price=float("inf"), entry_time=moment
        )


def test_the_exit_reason_comes_from_the_engines_own_order() -> None:
    """Причину выхода называет заявка движка, а не исполнитель.

    У `Fill` поля причины нет вовсе (решение 0008, пункт 4): сказать «это был
    тейк» исполнитель не может ни полем, ни ценой. Иначе одна и та же сделка
    получила бы разные причины в тестере и в бою — на истории цена совпала бы
    с уровнем ровно, а в бою не совпала бы никогда.
    """
    state = EngineState(position=position(Side.LONG, price=100.0))
    state, fill = closing(state, ExitReason.WINDOW_END, 110.0)
    result = apply_fill(state, fill, NO_TAKE)
    assert "конец торгового окна" in result.journal[0].reason


def test_a_deal_on_the_armed_level_is_a_take_profit_because_the_order_says_so() -> None:
    """Сделка по вооружённой заявке — тейк. И только по этому признаку.

    Цена сделки здесь **не равна** уровню: так бывает в бою — проскальзывание
    и шаг цены. Причина всё равно «тейк-профит», потому что сослалась сделка
    на заявку вооружения.
    """
    armed = position(Side.LONG, price=100.0, take=100.5)
    order = OrderRequest(
        action=OrderAction.ARM_TAKE_PROFIT, side=Side.LONG, volume=1.0,
        submitted_at=bar(*INSIDE).closes_at, reason="сторожим уровень",
        order_id="take:long:тест", price=100.5,
    )
    state = EngineState(position=armed, pending=(order,))
    result = apply_fill(state, Fill(
        action=OrderAction.CLOSE, side=Side.LONG, volume=1.0, price=100.25,
        at=bar(10, 20).closes_at, order_id=order.order_id,
    ), NO_TAKE)
    assert result.state.position is None
    assert "тейк-профит" in result.journal[0].reason
    assert result.state.take_profit_date == bar(10, 20).closes_at.date()


def test_a_fractional_volume_keeps_its_unit_in_the_journal() -> None:
    """«0,5» без единицы читается как половина чего угодно."""
    outcome = run(settings=WORKING.replace(volume=0.5), intent=Intent.LONG)
    assert "0,5 контракта" in outcome.orders[0].reason


# --------------------------------------------------------------------------
# Форма свечи проверяется и здесь: функция публичная
# --------------------------------------------------------------------------

def test_the_analysis_refuses_a_candle_of_the_data_layer() -> None:
    """Свеча со временем НАЧАЛА не проходит в разбор молча.

    `process_closed_candle` зовут не только через `Engine`: её зовут прогон
    по истории и сверка. Проверка на входе в движок такие вызовы не прикрывает.
    """
    class Stranger:
        time = bar(*INSIDE).closes_at
        closes_at = bar(*INSIDE).closes_at
        open = high = low = close = 100.0

    with pytest.raises(TypeError, match="НАЧАЛО"):
        process_closed_candle(
            EngineState(), Stranger(), decision(Intent.LONG), WORKING
        )


def test_the_orders_of_one_candle_are_a_known_short_list() -> None:
    """Сколько заявок бывает на одной свече — перебором по всем осям.

    ⚠️ **Обе оси, добавленные в Э1-8, входят в перебор.** Прежняя редакция
    жёстко брала «через свечу» и выключенный тейк, то есть исключала ровно то,
    что появилось: с вооружённым тейком и режимом «в одной свече» свеча несёт
    **три** заявки — снять сторожа, закрыть, войти в обратную сторону.

    Проверяется не «не больше одной», а форма списка: рыночных заявок
    не больше двух и только в режиме «в одной свече», вооружение и закрытие
    вместе не уходят никогда (`_settle`), снятие всегда стоит **до** закрытия.
    """
    states = [
        EngineState(),
        EngineState(position=position(Side.LONG)),
        EngineState(position=position(Side.SHORT)),
        EngineState(position=position(Side.LONG, state=PositionState.CLOSING)),
        EngineState(position=position(Side.LONG, take=105.0)),
        EngineState(position=position(Side.SHORT, take=95.0)),
    ]
    seen = 0
    for mode in Mode:
        for close_end in (True, False):
            for reversal in Reversal:
                for take in (True, False):
                    settings = EngineSettings(
                        mode=mode, window=WINDOW, close_on_time_end=close_end,
                        reversal=reversal, take_profit=take,
                    )
                    for state in states:
                        for intent in Intent:
                            for when in (INSIDE, OUTSIDE):
                                seen += 1
                                outcome = run(
                                    state, settings=settings, intent=intent,
                                    when=when,
                                )
                                _check_order_shape(outcome, settings, state, intent)
    assert seen == 1440, f"перебор выродился: разобрано {seen} случаев"


def _check_order_shape(outcome, settings, state, intent) -> None:
    """Форма списка заявок одной свечи. Что здесь возможно, а что нет."""
    where = f"{settings.mode}, {settings.reversal}, {state}, {intent}"
    actions = [order.action for order in outcome.orders]
    market = [action for action in actions if action.is_market]

    assert len(market) <= 2, f"{where}: {len(market)} рыночных заявок"
    if len(market) == 2:
        assert settings.reversal is Reversal.SAME_BAR, (
            f"{where}: две рыночные заявки вне режима «в одной свече»"
        )
        assert market == [OrderAction.CLOSE, OrderAction.OPEN], (
            f"{where}: вход подан раньше выхода — {market}"
        )
    assert not (
        OrderAction.ARM_TAKE_PROFIT in actions and OrderAction.CLOSE in actions
    ), f"{where}: вооружение ушло к брокеру вместе с закрытием"
    if OrderAction.CANCEL_TAKE_PROFIT in actions:
        assert OrderAction.CLOSE in actions, f"{where}: снятие без закрытия"
        assert actions.index(OrderAction.CANCEL_TAKE_PROFIT) < actions.index(
            OrderAction.CLOSE
        ), f"{where}: закрытие подано раньше снятия"
    # Заявок на вход в полёте не больше одной: на этом стоит защита
    # от двойного объёма.
    entries = [
        order for order in outcome.state.pending
        if order.action is OrderAction.OPEN
    ]
    assert len(entries) <= 1, f"{where}: {len(entries)} заявок на вход в полёте"
