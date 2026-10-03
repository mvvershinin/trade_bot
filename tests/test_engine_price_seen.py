"""Наблюдение цены внутри бара: скользящий уровень едет между закрытиями свечей.

Решение 0059 §3: лучшую достигнутую цену **сообщает исполнитель** — в ответе
`fills_at` приходит не сделка, а `PriceSeen` (время, цена). Движок пересчитывает
уровень той же арифметикой `TakeProfit.advance` и подаёт вооружение, только
если уровень сдвинулся. Здесь проверяется движок, а не модель исполнения:
исполнитель подставной, ответы записаны сценарием, по одному наблюдению
на ответ — так велит контракт порта.

Что стережёт каждый тест — сказано в его докстринге одной фразой
«Стережёт:», чтобы ломающий мутацией знал, какое поведение ломать.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, time, timedelta

from engine import (
    Engine,
    EngineSettings,
    EngineState,
    ExecutionRefused,
    Fill,
    JournalEntry,
    Mode,
    OrderAction,
    OrderRequest,
    PositionState,
    PriceSeen,
    Refusal,
    Side,
    TakeProfit,
    TradingWindow,
    apply_price,
)
from strategies import Intent
from tests.engine_helpers import (
    FakeCandle,
    ScriptedStrategy,
    bar,
    candle,
    decision,
    position,
)

WINDOW = TradingWindow(time(10, 5), time(11, 0))
PRICE = 210_000.0

#: Скользящий тейк: включение на +0,5 %, отступ 0,2 %, шаг подтяжки 0,05 %.
TRAILING = EngineSettings(
    mode=Mode.REVERSE, window=WINDOW,
    trailing_take_profit=True,
    trailing_start_percent=0.5,
    trailing_offset_percent=0.2,
    trailing_step_percent=0.05,
)
#: Неподвижный тейк 0,5 % — прототип.
FIXED = EngineSettings(mode=Mode.REVERSE, window=WINDOW)

#: Вход — заявка, поданная на закрытии свечи 10:05–10:10, исполняется
#: по открытию свечи 10:10. Имя заявки сделка называет, иначе робот встанет.
ENTRY = OrderRequest(
    action=OrderAction.OPEN, side=Side.LONG, volume=1.0,
    submitted_at=bar(10, 10).closes_at, reason="вход по сценарию",
    order_id="open:long:сценарий",
)

Answer = Sequence[Fill | PriceSeen]


class Talker:
    """Подставной исполнитель: ответы `fills_at` — записанный сценарий.

    Сценарий — плоская очередь ответов на все свечи подряд; пустой ответ
    заканчивает обмен по текущей свече, следующий ответ уже про следующую.
    Заявки только записываются: исполняет их сценарий, а не модель.
    """

    def __init__(self, answers: Sequence[Answer]) -> None:
        self.answers = list(answers)
        self.submitted: list[OrderRequest] = []

    async def submit(self, order: OrderRequest) -> None:
        self.submitted.append(order)

    async def fills_at(self, market_candle: object) -> Answer:
        return self.answers.pop(0) if self.answers else ()


def at(hour: int, minute: int, second: int = 0) -> datetime:
    return candle(hour, minute).time + timedelta(seconds=second)


def opening() -> Fill:
    return Fill(
        action=OrderAction.OPEN, side=Side.LONG, volume=1.0, price=PRICE,
        at=at(10, 10), order_id=ENTRY.order_id,
    )


def seen(price: float, second: int) -> list[PriceSeen]:
    """Ответ из одного наблюдения внутри свечи 10:10–10:15."""
    return [PriceSeen(at=at(10, 10, second), price=price)]


def drive(
    settings: EngineSettings,
    answers: Sequence[Answer],
    candles: Sequence[FakeCandle],
    intents: Sequence[Intent] | None = None,
) -> tuple[Engine, Talker, list[JournalEntry]]:
    """Прогнать свечи через движок с подставным исполнителем."""
    lines: list[JournalEntry] = []
    script = [
        decision(intent, close=item.close)
        for intent, item in zip(
            intents or [Intent.LONG] * len(candles), candles, strict=True
        )
    ]
    engine = Engine(
        ScriptedStrategy(script), settings,
        state=EngineState(pending=(ENTRY,)), journal=lines.append,
    )
    talker = Talker(answers)

    async def go() -> None:
        for item in candles:
            await engine.on_market_candle(item, talker)

    asyncio.run(go())
    return engine, talker, lines


def arms(talker: Talker) -> list[OrderRequest]:
    return [o for o in talker.submitted if o.action is OrderAction.ARM_TAKE_PROFIT]


# --------------------------------------------------------------------------
# Скользящий уровень едет внутри бара
# --------------------------------------------------------------------------

def test_the_trailing_level_switches_on_and_moves_inside_the_bar() -> None:
    """Скользящий уровень включается и едет по наблюдениям внутри бара.

    Стережёт: наблюдение цены пересчитывает уровень через `advance`
    и на каждый сдвиг подаёт вооружение — включение внутри бара тоже.

    Вход 210 000, включение на 211 050. 211 100 включает уровень
    211 100 − 0,2 % = 210 678; 211 500 двигает его на 211 077 (сдвиг 399
    больше шага 105). 211 520 — новая вершина, но сдвиг уровня 20 меньше
    шага: вершина запоминается, вооружения нет. Без наблюдений закрытие
    211 000 уровня не включило бы вовсе: прибыль 0,476 % меньше порога.
    """
    engine, talker, lines = drive(TRAILING, [
        [opening()], seen(211_100.0, 60), seen(211_500.0, 120),
        seen(211_520.0, 180), (),
    ], [candle(10, 10, close=211_000.0)])

    by_seen = [o for o in arms(talker) if o.submitted_at < bar(10, 15).closes_at]
    assert [o.price for o in by_seen] == [210_678.0, 211_077.0], (
        "по наблюдениям уровень не включился или не поехал"
    )
    assert [o.submitted_at for o in by_seen] == [at(10, 10, 60), at(10, 10, 120)]
    assert engine.position is not None
    assert engine.position.take_profit == 211_077.0
    assert engine.position.take.peak == 211_520.0, (
        "вершина внутри бара забыта: закрытие ниже неё вернуло бы уровень назад"
    )
    moved = [e for e in lines if e.at in (at(10, 10, 60), at(10, 10, 120))]
    assert [e.event for e in moved] == [
        "Тейк-профит выставлен", "Скользящий тейк подтянут",
    ], "сдвиг уровня внутри бара не написан в журнал"
    assert "внутри бара" in moved[1].reason


def test_a_price_worse_than_the_peak_never_moves_the_level_back() -> None:
    """Наблюдение хуже вершины уровень назад не двигает.

    Стережёт: наблюдение хуже лучшей достигнутой цены не трогает ни вершину,
    ни уровень и вооружения не подаёт (приёмочное правило ТЗ §9 внутри бара).
    """
    peaked = replace(
        position(Side.LONG, price=PRICE),
        take=TakeProfit(
            trailing=True, start_percent=0.5, offset_percent=0.2,
            step_percent=0.05, level=211_077.0, peak=211_500.0,
        ),
    )
    state = EngineState(position=peaked)
    for worse in (211_499.0, 211_100.0, 210_000.0, 205_000.0):
        result = apply_price(state, PriceSeen(at=at(10, 20), price=worse), TRAILING)
        assert result.orders == (), f"наблюдение {worse:g} подало вооружение"
        assert result.state.position is not None
        assert result.state.position.take.peak == 211_500.0
        assert result.state.position.take_profit == 211_077.0
        assert result.written == ()


# --------------------------------------------------------------------------
# Вторая стена: неподвижный тейк наблюдений не читает
# --------------------------------------------------------------------------

def test_a_fixed_take_ignores_prices_seen_inside_the_bar() -> None:
    """Неподвижный тейк + наблюдения: заявки те же, что без них.

    Стережёт: при неподвижном тейке наблюдения не дают ни одной лишней
    заявки — поданное и итоговое состояние те же, что без наблюдений.

    Держится на правиле «вооружение по наблюдению — только при сдвиге
    уровня и новой вершине», а не на первой строке `advance`.
    """
    candles = [candle(10, 10, close=PRICE), candle(10, 15, close=PRICE)]
    plain, quiet, _ = drive(FIXED, [[opening()], (), ()], candles)
    watched, talker, _ = drive(FIXED, [
        [opening()], seen(212_000.0, 60), seen(209_000.0, 120),
        seen(215_000.0, 180), (),
        [PriceSeen(at=at(10, 16), price=230_000.0)], (),
    ], candles)

    def shape(orders: list[OrderRequest]) -> list[tuple[object, ...]]:
        return [(o.action, o.price, o.order_id, o.submitted_at) for o in orders]

    assert shape(talker.submitted) == shape(quiet.submitted)
    # Вооружение от сделки входа и по одному на каждом закрытии — и только.
    assert len(arms(talker)) == len(arms(quiet)) == 3
    assert watched.state == plain.state


def test_a_fixed_take_left_without_a_level_is_not_rearmed_by_a_price() -> None:
    """Неподвижный тейк без уровня по наблюдению не перевооружается.

    Стережёт: неподвижный тейк, уровень которого погашен (отказ
    в вооружении), не вооружается заново по наблюдению — вооружит шаг 5
    на закрытии. Иначе каждое наблюдение подавало бы вооружение заново.
    """
    bare = replace(position(Side.LONG, price=PRICE), take=TakeProfit(percent=0.5))
    result = apply_price(
        EngineState(position=bare), PriceSeen(at=at(10, 20), price=215_000.0), FIXED
    )
    assert result.orders == ()
    assert result.state == EngineState(position=bare)


# --------------------------------------------------------------------------
# Без открытой ноги наблюдение не вооружает
# --------------------------------------------------------------------------

def test_a_price_without_a_position_is_skipped_silently() -> None:
    """Стережёт: наблюдение при пустом счёте — ни заявки, ни строки журнала."""
    result = apply_price(
        EngineState(), PriceSeen(at=at(10, 20), price=215_000.0), TRAILING
    )
    assert result.orders == ()
    assert result.written == ()
    assert result.state == EngineState()


def test_a_price_after_the_exit_was_submitted_does_not_arm_the_level() -> None:
    """После поданного выхода наблюдение уровень не вооружает.

    Стережёт: после поданных снятия и выхода (позиция «закрывается»)
    наблюдение уровень не вооружает — даже при новой вершине.

    Свеча 10:10 включает уровень, свеча 10:15 даёт обратный сигнал: снятие
    и закрытие поданы. На свече 10:20 сделка выхода ещё не пришла,
    а исполнитель сообщает цену много выше вершины.
    """
    engine, talker, _ = drive(TRAILING, [
        [opening()], seen(211_500.0, 60), (),
        (),
        [PriceSeen(at=at(10, 21), price=213_000.0)], (),
    ], [
        candle(10, 10, close=211_000.0),
        candle(10, 15, close=211_000.0),
        candle(10, 20, close=211_000.0),
    ], [Intent.LONG, Intent.SHORT, Intent.SHORT])

    actions = [o.action for o in talker.submitted]
    assert OrderAction.CANCEL_TAKE_PROFIT in actions and OrderAction.CLOSE in actions
    exit_at = actions.index(OrderAction.CLOSE)
    assert OrderAction.ARM_TAKE_PROFIT not in actions[exit_at:], (
        "наблюдение после поданного выхода вооружило уровень"
    )
    assert engine.position is not None
    assert engine.position.state is PositionState.CLOSING
    assert engine.position.take_profit is None


def test_a_price_after_a_refused_exit_does_not_arm_the_level() -> None:
    """После отклонённого выхода наблюдение уровень не вооружает.

    Стережёт: снятие прошло, а выход отклонён — нога открыта и без уровня,
    счётчик отказов выхода не пуст. Наблюдение с новой вершиной уровень
    не ставит: его погасил сам движок ради выхода.
    """
    cancelled = replace(
        position(Side.LONG, price=PRICE),
        take=TakeProfit(
            trailing=True, start_percent=0.5, offset_percent=0.2,
            step_percent=0.05, level=None, peak=211_500.0,
        ),
    )
    state = EngineState(position=cancelled, exit_blocked_bars=1)
    result = apply_price(state, PriceSeen(at=at(10, 20), price=213_000.0), TRAILING)
    assert result.orders == ()
    assert result.state == state


def test_a_price_seen_by_a_switched_off_robot_neither_arms_nor_moves_the_peak() -> None:
    """В режиме «Выключен» наблюдение не вооружает уровень и не двигает вершину.

    Стережёт: при `Mode.OFF` наблюдение цены, которое у включённого робота
    включило бы скользящий уровень или подтянуло его, не даёт ни одной заявки,
    ни строки журнала, и вершина в состоянии остаётся прежней — иначе после
    включения уровень прыгнул бы от цены, увиденной у выключенного робота
    (risk 2026-10-03-1429 №1).

    Два случая: уровень ещё не включён (вершины нет) и уровень уже стоит.
    Контроль — те же наблюдения в режиме «реверс» дают вооружение.
    """
    switched_off = replace(TRAILING, mode=Mode.OFF)
    trailing = TakeProfit(
        trailing=True, start_percent=0.5, offset_percent=0.2, step_percent=0.05,
    )
    cases = (
        replace(trailing, level=None, peak=None),
        replace(trailing, level=211_077.0, peak=211_500.0),
    )
    for take in cases:
        state = EngineState(
            position=replace(position(Side.LONG, price=PRICE), take=take)
        )
        price = PriceSeen(at=at(10, 20), price=213_000.0)
        live = apply_price(state, price, TRAILING)
        assert live.orders, "контроль: у включённого робота наблюдение вооружает"
        result = apply_price(state, price, switched_off)
        assert result.orders == (), "выключенный робот вооружил уровень"
        assert result.written == ()
        assert result.state == state, "выключенный робот сдвинул вершину"


def test_an_engine_switched_off_between_bars_arms_nothing_inside_the_next_bar() -> None:
    """Выключение между свечами: на следующей свече внутри бара заявок нет.

    Стережёт: путь через `Engine` — после `apply_settings(mode=OFF)`
    наблюдения следующей свечи не подают ни одного вооружения
    (воспроизведение аудита: было четыре ARM подряд).
    """
    lines: list[JournalEntry] = []
    engine = Engine(
        ScriptedStrategy([
            decision(Intent.LONG, close=PRICE),
            decision(Intent.LONG, close=PRICE + 2_900),
        ]),
        TRAILING, state=EngineState(pending=(ENTRY,)), journal=lines.append,
    )
    talker = Talker([
        [opening()], (),
        *[[PriceSeen(at=at(10, 15, 10 * i), price=PRICE + 600 * i)] for i in range(1, 6)],
        (),
    ])

    async def go() -> None:
        await engine.on_market_candle(candle(10, 10, close=PRICE), talker)
        engine.apply_settings(replace(TRAILING, mode=Mode.OFF))
        before = len(talker.submitted)
        await engine.on_market_candle(candle(10, 15, close=PRICE + 2_900), talker)
        assert talker.submitted[before:] == [], "выключенный робот подал заявку"

    asyncio.run(go())
    assert engine.position is not None
    assert engine.position.take.peak is None


class RefusingExit(Talker):
    """Подставной исполнитель, определённо отклоняющий заявку на выход."""

    async def submit(self, order: OrderRequest) -> None:
        if order.action is OrderAction.CLOSE:
            raise ExecutionRefused("аукцион", Refusal.UNKNOWN)
        await super().submit(order)


def _refused_exit_run(settings: EngineSettings) -> list[JournalEntry]:
    """Лонг, обратный сигнал на 10:15 и 10:20, выход оба раза отклонён."""
    lines: list[JournalEntry] = []
    engine = Engine(
        ScriptedStrategy([
            decision(intent, close=211_000.0)
            for intent in (Intent.LONG, Intent.SHORT, Intent.SHORT)
        ]),
        settings, state=EngineState(pending=(ENTRY,)), journal=lines.append,
    )
    talker = RefusingExit([[opening()], seen(211_500.0, 60), (), (), ()])

    async def go() -> None:
        for minute in (10, 15, 20):
            await engine.on_market_candle(candle(10, minute, close=211_000.0), talker)

    asyncio.run(go())
    assert not engine.halted, "определённый отказ в выходе не останавливает робота"
    return lines


def test_the_first_refused_exit_says_the_inside_bar_trailing_is_off() -> None:
    """Первый отказ на пути выхода пишет, что подтяжка внутри бара выключена.

    Стережёт (`D-140`, правило 13): после первого определённого отказа
    на пути выхода у позиции со скользящим тейком в журнале есть строка
    «Подтяжка тейка внутри бара выключена» — ровно одна, второй отказ
    по той же позиции её не повторяет. Иначе последствие отказа молчит:
    уровень внутри бара больше не едет, а владелец счёта об этом не знает.
    """
    lines = _refused_exit_run(TRAILING)
    off = [e for e in lines if e.event == "Подтяжка тейка внутри бара выключена"]
    refused = [e for e in lines if e.event == "Заявка отклонена исполнителем"]
    assert len(refused) >= 2, "сценарий: выход отклонён на двух свечах"
    assert len(off) == 1, f"строк о выключенной подтяжке: {len(off)}, нужна одна"
    assert off[0].at == refused[0].at
    assert lines.index(off[0]) > lines.index(refused[0])


class RefusingCancel(Talker):
    """Подставной исполнитель, отклоняющий снятие тейка: «уровень уже задет»."""

    async def submit(self, order: OrderRequest) -> None:
        if order.action is OrderAction.CANCEL_TAKE_PROFIT:
            raise ExecutionRefused("уровень уже задет", Refusal.UNKNOWN)
        await super().submit(order)


def test_a_refused_take_cancel_also_says_the_inside_bar_trailing_is_off() -> None:
    """Отказ в снятии тейка — тоже первый отказ на пути выхода, и строка есть.

    Стережёт (`D-140`, правило 13): счётчик отказов растёт и на отказе
    в снятии тейка (`after_refused_cancel`), подтяжка внутри бара после него
    выключена — значит, строка «Подтяжка тейка внутри бара выключена»
    обязана появиться и здесь, ровно одна. Сторож выше проверяет только
    отказ в заявке на выход: строку, написанную лишь для него, он не ловил.
    """
    lines: list[JournalEntry] = []
    engine = Engine(
        ScriptedStrategy([
            decision(intent, close=211_000.0)
            for intent in (Intent.LONG, Intent.SHORT, Intent.SHORT)
        ]),
        TRAILING, state=EngineState(pending=(ENTRY,)), journal=lines.append,
    )
    talker = RefusingCancel([[opening()], seen(211_500.0, 60), (), (), ()])

    async def go() -> None:
        for minute in (10, 15, 20):
            await engine.on_market_candle(candle(10, minute, close=211_000.0), talker)

    asyncio.run(go())
    refused = [e for e in lines if e.event == "Заявка отклонена исполнителем"]
    off = [e for e in lines if e.event == "Подтяжка тейка внутри бара выключена"]
    assert refused, "сценарий: снятие тейка не было отклонено"
    assert engine.state.exit_blocked_bars, "сценарий: счётчик отказов не вырос"
    assert len(off) == 1, f"строк о выключенной подтяжке: {len(off)}, нужна одна"
    assert off[0].at == refused[0].at


def test_a_fixed_take_gets_no_line_about_inside_bar_trailing() -> None:
    """Стережёт: у неподвижного тейка отказ на выходе строки о подтяжке не даёт.

    Подтяжки внутри бара у неподвижного тейка нет — строка о её выключении
    была бы неправдой.
    """
    lines = _refused_exit_run(FIXED)
    assert any(e.event == "Заявка отклонена исполнителем" for e in lines)
    assert not [e for e in lines if e.event == "Подтяжка тейка внутри бара выключена"]


# --------------------------------------------------------------------------
# Движок не читает high/low — поведенчески, а не по тексту исходников
# --------------------------------------------------------------------------

def test_poisoned_high_and_low_change_nothing_while_prices_come_through_the_port() -> None:
    """Отравленные high/low свечи ничего не меняют.

    Стережёт: уровни и заявки движка зависят от закрытий и наблюдений,
    но не от `high`/`low` свечи.

    Абсурдные, но конечные значения: `nan` не годится — `advance` на нечисле
    возвращает план как есть, и движок, читающий `high`, такой сторож прошёл бы.
    Скользящий тейк включается на закрытии и едет на нескольких закрытиях
    подряд, плюс наблюдения внутри баров.
    """
    closes = [211_000.0, 211_200.0, 211_600.0, 212_100.0]
    answers: list[Answer] = [
        [opening()], seen(211_150.0, 90), (),
        [PriceSeen(at=at(10, 17), price=211_400.0)], (),
        (),
        [PriceSeen(at=at(10, 27), price=212_400.0)], (),
    ]
    clean = [candle(10, 10 + 5 * n, close=c) for n, c in enumerate(closes)]
    poisoned = [
        replace(item, high=item.close * 2, low=item.close / 2) for item in clean
    ]
    first, honest, _ = drive(TRAILING, answers, clean)
    second, tainted, _ = drive(TRAILING, answers, poisoned)

    def shape(orders: list[OrderRequest]) -> list[tuple[object, ...]]:
        return [(o.action, o.price, o.touch, o.submitted_at) for o in orders]

    assert len(arms(honest)) >= 4, "сценарий не довёл скользящий уровень до движения"
    assert shape(tainted.submitted) == shape(honest.submitted)
    assert second.state == first.state


# --------------------------------------------------------------------------
# Наблюдение — не сделка: в «потерянные сделки» оно не попадает
# --------------------------------------------------------------------------

def stray() -> Fill:
    """Сделка без своей заявки — останавливает робота посреди ответа."""
    return Fill(
        action=OrderAction.CLOSE, side=Side.SHORT, volume=1.0, price=PRICE,
        at=at(10, 10), order_id="чужая",
    )


def test_a_price_left_after_a_halt_is_not_reported_as_a_lost_deal() -> None:
    """Наблюдение из остатка ответа — не потерянная сделка.

    Стережёт: остаток ответа после остановки называет потерянными только
    сделки. Наблюдение счёта не меняет, и строка «ещё N сделок не разобраны»
    про него означала бы расхождение с брокером, которого нет.
    """
    _, _, lines = drive(TRAILING, [
        [stray(), PriceSeen(at=at(10, 11), price=PRICE)],
    ], [candle(10, 10, close=PRICE)])
    assert not [e for e in lines if e.event == "Сделки исполнителя не разобраны"]

    _, _, lines = drive(TRAILING, [
        [stray(), PriceSeen(at=at(10, 11), price=PRICE), stray()],
    ], [candle(10, 10, close=PRICE)])
    lost = [e for e in lines if e.event == "Сделки исполнителя не разобраны"]
    assert len(lost) == 1 and "ещё 1 сделок" in lost[0].reason, (
        "наблюдение посчитано потерянной сделкой либо сделка не названа"
    )


def test_the_time_of_a_price_moves_nothing_but_the_peak_in_the_state() -> None:
    """Время наблюдения состояние не двигает.

    Стережёт: наблюдение с новой вершиной меняет в состоянии **только
    вершину** — даже другой календарной датой. Дата «стопа после тейка»,
    деньги дня, тишина журнала и счётчики берут время из сделок и закрытий
    свечей; уровень ставит принятая заявка, а не разбор наблюдения.
    """
    p = replace(
        position(Side.LONG, price=PRICE),
        take=TakeProfit(
            trailing=True, start_percent=0.5, offset_percent=0.2,
            step_percent=0.05, level=210_678.0, peak=211_100.0,
        ),
    )
    state = EngineState(position=p, pending=(ENTRY,))
    later = at(10, 20) + timedelta(days=3)
    result = apply_price(state, PriceSeen(at=later, price=211_500.0), TRAILING)
    assert result.state == replace(
        state, position=replace(p, take=replace(p.take, peak=211_500.0))
    )
    assert [(o.price, o.submitted_at) for o in result.orders] == [(211_077.0, later)]
    assert [e.at for e in result.journal] == [later]
