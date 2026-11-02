"""Остановка по слову исполнителя (`ExecutionHalted`) и частичная сделка.

Решение 0066: у порта исполнителя три исхода, кроме «принято», — определённый
отказ, остановка с известной причиной и неизвестность. Здесь стерегутся два
свойства второго:

* причина в журнале и в `halted` — **слова исполнителя**, а не «исполнитель
  не ответил» или «не знает, дошла ли заявка»: отказ биржи, названный обрывом
  связи, отправляет человека искать не то;
* сделки, известные исполнителю к остановке, движок **разбирает до строки
  остановки** — позиция в окне не расходится со счётом.

И одно соседнее: сделка не на объём своей заявки (частичное исполнение)
останавливает робота, а не остаётся строкой ERROR при работающем роботе.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import replace
from datetime import time

import pytest

from engine import (
    Engine,
    EngineSettings,
    EngineState,
    ExecutionHalted,
    ExecutionRefused,
    ExitReason,
    Fill,
    JournalEntry,
    JournalLevel,
    Mode,
    OrderAction,
    OrderRequest,
    PriceSeen,
    Side,
    TakeProfit,
    TradingWindow,
    take_order_id,
)
from engine.pipeline import _arm_order, _cancel_order
from strategies import Intent
from tests.engine_helpers import (
    NextOpenExecutor,
    RecordingExecutor,
    ScriptedStrategy,
    bar,
    candle,
    decision,
    position,
)

WINDOW = TradingWindow(time(10, 5), time(11, 0))
WORKING = EngineSettings(mode=Mode.REVERSE, window=WINDOW, take_profit=False)
PRICE = 210_000.0
EXCHANGE_SAYS = "биржа отклонила заявку после приёма: инструмент на дискретном аукционе"
HALT_EVENT = "Исполнитель остановил торговлю"
#: Слова двух других остановок. Ни одно из них не должно стоять там, где
#: исполнитель назвал причину сам.
SILENCE_WORDS = ("не ответил", "не знает, дошла ли", "Заявка не подана")


class HaltingOnSubmit:
    """Исполнитель, у которого подача кончается остановкой с причиной."""

    def __init__(self, halted: ExecutionHalted) -> None:
        self.halted = halted
        self.submitted: list[OrderRequest] = []

    async def submit(self, order: OrderRequest) -> None:
        self.submitted.append(order)
        raise self.halted

    async def fills_at(self, market_candle) -> Sequence[Fill]:
        return ()


class HaltingOnFills:
    """Исполнитель, у которого вопрос о сделках кончается остановкой."""

    def __init__(self, halted: ExecutionHalted) -> None:
        self.halted = halted
        self.submitted: list[OrderRequest] = []

    async def submit(self, order: OrderRequest) -> None:
        self.submitted.append(order)

    async def fills_at(self, market_candle) -> Sequence[Fill]:
        raise self.halted


def _said_in_own_words(engine: Engine, lines: list[JournalEntry]) -> None:
    """Общая проверка: причина исполнителя — и в журнале, и в `halted`."""
    assert engine.halted, "остановка исполнителя не остановила робота"
    last = lines[-1]
    assert last.event == HALT_EVENT, f"последняя строка — «{last.event}»"
    assert last.level is JournalLevel.ERROR
    for text in (engine.halted, f"{last.event}. {last.reason}"):
        assert EXCHANGE_SAYS in text, "причина исполнителя потеряна"
        for wrong in SILENCE_WORDS:
            assert wrong not in text, f"отказ биржи назван «{wrong}»"


def test_an_exchange_rejection_after_acceptance_is_named_by_its_own_reason_on_submit() -> None:
    """Подача кончилась `ExecutionHalted` — в журнале слова исполнителя.

    Стережёт: остановка по `ExecutionHalted` из `submit` пишет строку
    «Исполнитель остановил торговлю» с причиной исполнителя и ставит её же
    в `halted`; слов про обрыв и «не знает, дошла ли заявка» в ней нет.
    """
    lines: list[JournalEntry] = []
    engine = Engine(
        ScriptedStrategy([decision(Intent.LONG, close=PRICE)]), WORKING,
        journal=lines.append,
    )
    executor = HaltingOnSubmit(ExecutionHalted(EXCHANGE_SAYS))
    asyncio.run(engine.on_market_candle(candle(10, 5, close=PRICE), executor))

    assert [order.action for order in executor.submitted] == [OrderAction.OPEN]
    _said_in_own_words(engine, lines)
    assert engine.position is None
    assert engine.state.pending == (), "заявку, кончившуюся остановкой, движок счёл принятой"


def test_an_exchange_rejection_is_named_by_its_own_reason_on_the_deals_question() -> None:
    """Вопрос о сделках кончился `ExecutionHalted` — та же причина, свеча не разобрана.

    Стережёт: `ExecutionHalted` из `fills_at` не читается как молчание
    исполнителя («Сделки исполнителя не получены»), робот стоит, свеча
    в торговый модуль не ушла и заявок не подано.
    """
    lines: list[JournalEntry] = []
    strategy = ScriptedStrategy([decision(Intent.LONG, close=PRICE)])
    engine = Engine(strategy, WORKING, journal=lines.append)
    executor = HaltingOnFills(ExecutionHalted(EXCHANGE_SAYS))
    asyncio.run(engine.on_market_candle(candle(10, 5, close=PRICE), executor))

    _said_in_own_words(engine, lines)
    assert "Сделки исполнителя не получены" not in engine.halted
    assert strategy.seen == [], "свеча ушла в модуль по состоянию, которое отстало"
    assert executor.submitted == []


def _closing_long() -> tuple[EngineState, OrderRequest]:
    """Лонг, по которому уже подан рыночный выход: заявка в полёте."""
    held = position(Side.LONG, price=PRICE)
    exit_order = OrderRequest(
        action=OrderAction.CLOSE, side=Side.LONG, volume=1.0,
        submitted_at=bar(10, 5).closes_at, reason="выход", order_id="close:long:тест",
        exit_reason=ExitReason.SIGNAL,
    )
    state = EngineState(position=held, pending=(exit_order,))
    return state, exit_order


def test_a_deal_handed_over_with_the_halt_on_the_deals_question_is_not_lost() -> None:
    """Сделка, отданная вместе с остановкой, разобрана — и раньше строки остановки.

    Стережёт: `ExecutionHalted.fills` из `fills_at` проходят через разбор
    сделки — позиция закрыта, строка «Лонг закрыт» стоит в журнале перед
    строкой остановки, предупреждения о неразобранных сделках нет.
    """
    state, exit_order = _closing_long()
    deal = Fill(
        action=OrderAction.CLOSE, side=Side.LONG, volume=1.0, price=PRICE + 300,
        at=candle(10, 10).time, order_id=exit_order.order_id,
    )
    lines: list[JournalEntry] = []
    engine = Engine(ScriptedStrategy([]), WORKING, state=state, journal=lines.append)
    executor = HaltingOnFills(ExecutionHalted(EXCHANGE_SAYS, fills=[deal]))
    asyncio.run(engine.on_market_candle(candle(10, 10, close=PRICE), executor))

    assert engine.position is None, "сделка, пришедшая с остановкой, потеряна"
    assert engine.state.pending == ()
    events = [entry.event for entry in lines]
    assert "Лонг закрыт" in events
    assert events.index("Лонг закрыт") < events.index(HALT_EVENT), (
        "строка остановки встала раньше разбора известной сделки"
    )
    assert "Сделки исполнителя не разобраны" not in events
    _said_in_own_words(engine, lines)


def test_a_deal_handed_over_with_the_halt_on_submit_is_not_lost() -> None:
    """То же на подаче: тейк сработал, пока снималось, — сделка разобрана.

    Сценарий боевой: робот идёт на выход по сигналу, подаёт снятие тейка,
    а исполнитель останавливает торговлю и отдаёт сделку по сработавшему
    тейку. Стережёт: сделка из `ExecutionHalted.fills`, брошенного из
    `submit`, разобрана после закрепления свечи — позиция закрыта по тейку,
    дата тейка записана, строка о сделке стоит раньше строки остановки.
    """
    held = replace(
        position(Side.LONG, price=PRICE),
        take=TakeProfit(percent=0.5, level=211_050.0),
    )
    arm = _arm_order(held, bar(10, 5).closes_at, 211_050.0)
    state = EngineState(position=held, pending=(arm,))
    deal = Fill(
        action=OrderAction.CLOSE, side=Side.LONG, volume=1.0, price=211_050.0,
        at=candle(10, 5).time, order_id=take_order_id(held),
    )
    lines: list[JournalEntry] = []
    engine = Engine(
        ScriptedStrategy([decision(Intent.SHORT, close=PRICE)]),
        WORKING.replace(take_profit=True), state=state, journal=lines.append,
    )
    executor = HaltingOnSubmit(ExecutionHalted(EXCHANGE_SAYS, fills=(deal,)))
    asyncio.run(engine.on_market_candle(candle(10, 5, close=PRICE), executor))

    assert [order.action for order in executor.submitted] == [
        OrderAction.CANCEL_TAKE_PROFIT
    ], "после остановки на снятии движок подал ещё что-то"
    assert engine.position is None, "сделка по тейку, пришедшая с остановкой, потеряна"
    assert engine.state.take_profit_date == deal.at.date()
    events = [entry.event for entry in lines]
    assert events.index("Лонг закрыт") < events.index(HALT_EVENT)
    _said_in_own_words(engine, lines)


def test_the_rest_of_a_batch_is_settled_before_the_halt_not_written_off() -> None:
    """Остановка на заявке, рождённой сделкой: остаток пачки разобран, а не потерян.

    Скользящий тейк: наблюдение цены двигает уровень, вооружение нового
    уровня кончается остановкой, а в той же пачке следом пришла сделка
    по прежнему уровню. Стережёт: сделка из остатка пачки разобрана
    (позиция закрыта), а не списана строкой «Сделки исполнителя
    не разобраны»; заявок после остановки не подаётся.
    """
    plan = TakeProfit(
        percent=0.5, trailing=True, start_percent=0.1, offset_percent=0.2,
        step_percent=0.0, level=210_578.0, peak=211_000.0,
    )
    held = replace(position(Side.LONG, price=PRICE), take=plan)
    arm = _arm_order(held, bar(10, 5).closes_at, 210_578.0)
    state = EngineState(position=held, pending=(arm,))
    stop_deal = Fill(
        action=OrderAction.CLOSE, side=Side.LONG, volume=1.0, price=210_578.0,
        at=candle(10, 10).time, order_id=take_order_id(held),
    )

    class Batch:
        def __init__(self) -> None:
            self.submitted: list[OrderRequest] = []
            self.calls = 0

        async def submit(self, order: OrderRequest) -> None:
            self.submitted.append(order)
            raise ExecutionHalted(EXCHANGE_SAYS)

        async def fills_at(self, market_candle) -> Sequence[Fill | PriceSeen]:
            self.calls += 1
            if self.calls > 1:
                raise AssertionError("после остановки движок спросил сделки ещё раз")
            return [PriceSeen(at=market_candle.time, price=212_000.0), stop_deal]

    lines: list[JournalEntry] = []
    engine = Engine(
        ScriptedStrategy([]), WORKING.replace(take_profit=True),
        state=state, journal=lines.append,
    )
    executor = Batch()
    asyncio.run(engine.on_market_candle(candle(10, 10, close=PRICE), executor))

    assert [order.action for order in executor.submitted] == [
        OrderAction.ARM_TAKE_PROFIT
    ]
    assert engine.position is None, "сделка из остатка пачки списана, а не разобрана"
    events = [entry.event for entry in lines]
    assert "Сделки исполнителя не разобраны" not in events
    assert events.index("Лонг закрыт") < events.index(HALT_EVENT)
    _said_in_own_words(engine, lines)


def test_the_model_halts_on_demand_and_hands_its_deals_over() -> None:
    """Модель исполнения выдаёт остановку по требованию теста — со сделками.

    Решение 0066: ветка, недостижимая моделью, существует только в бою.
    Стережёт: `halt_at` модели бросает `ExecutionHalted` на вопросе о сделках
    по названной свече и кладёт в него исполнение по открытию; движок
    открывает позицию по этой сделке и останавливается словами исполнителя.
    Рождённое сделкой вооружение не подаётся — после остановки заявок нет.
    """
    settings = WORKING.replace(take_profit=True)
    fill_bar = candle(10, 10, close=PRICE)
    model = NextOpenExecutor(halt_at={fill_bar.time: EXCHANGE_SAYS})
    lines: list[JournalEntry] = []
    engine = Engine(
        ScriptedStrategy([decision(Intent.LONG, close=PRICE)]), settings,
        journal=lines.append,
    )
    asyncio.run(engine.on_market_candle(candle(10, 5, close=PRICE), model))
    asyncio.run(engine.on_market_candle(fill_bar, model))

    assert engine.position is not None, "исполнение по открытию потеряно с остановкой"
    assert engine.position.side is Side.LONG
    assert model.armed == {}, "после остановки движок вооружил уровень"
    events = [entry.event for entry in lines]
    assert events.index("Лонг открыт") < events.index(HALT_EVENT)
    assert "Тейк-профит выставлен" not in events, (
        "журнал обещает уровень, которого после остановки никто не подал"
    )
    _said_in_own_words(engine, lines)


def test_the_model_halts_on_submit_by_the_order_name() -> None:
    """`halt_on` модели: подача названной заявки кончается остановкой.

    Стережёт: модель выражает `ExecutionHalted` на подаче по имени заявки,
    и движок называет его словами исполнителя.
    """
    at = bar(10, 10).closes_at  # закрытие свечи 10:05 — момент подачи
    model = NextOpenExecutor(halt_on={f"open:long:{at.isoformat()}": EXCHANGE_SAYS})
    lines: list[JournalEntry] = []
    engine = Engine(
        ScriptedStrategy([decision(Intent.LONG, close=PRICE)]), WORKING,
        journal=lines.append,
    )
    asyncio.run(engine.on_market_candle(candle(10, 5, close=PRICE), model))

    assert model.pending == [], "заявка, кончившаяся остановкой, легла в очередь модели"
    _said_in_own_words(engine, lines)


def test_a_partial_deal_stops_the_robot() -> None:
    """Сделка не на объём своей заявки — остановка, а не строка и дальше.

    Решение 0066, Р6: исполнитель отдаёт сделку только на весь объём.
    Пришла частичная — на счёте объём, которого движок не видит. Стережёт:
    робот остановлен, причина — «Сделка противоречит своей заявке», позиция
    в состоянии не тронута, свеча в модуль не ушла и заявок не подано.
    """
    held = position(Side.LONG, price=PRICE, volume=2.0)
    exit_order = OrderRequest(
        action=OrderAction.CLOSE, side=Side.LONG, volume=2.0,
        submitted_at=bar(10, 5).closes_at, reason="выход", order_id="close:long:тест",
        exit_reason=ExitReason.SIGNAL,
    )
    state = EngineState(position=held, pending=(exit_order,))
    partial = Fill(
        action=OrderAction.CLOSE, side=Side.LONG, volume=1.0, price=PRICE,
        at=candle(10, 10).time, order_id=exit_order.order_id,
    )
    lines: list[JournalEntry] = []
    strategy = ScriptedStrategy([decision(Intent.SHORT, close=PRICE)])
    engine = Engine(strategy, WORKING, state=state, journal=lines.append)
    executor = RecordingExecutor(scripted=[[partial]])
    asyncio.run(engine.on_market_candle(candle(10, 10, close=PRICE), executor))

    assert engine.halted, "частичная сделка не остановила робота"
    assert engine.halted.startswith("Сделка противоречит своей заявке")
    assert engine.position is not None and engine.position.volume == 2.0
    assert strategy.seen == [], "после частичной сделки движок решал дальше"
    assert executor.submitted == []
    assert lines[-1].level is JournalLevel.ERROR
    assert "Робот остановлен" in lines[-1].reason


def test_deals_behind_a_rejected_carried_deal_are_named_not_dropped() -> None:
    """Сделка, отданная с остановкой, не сопоставилась — следующие названы, а не выброшены.

    В `ExecutionHalted.fills` две сделки: первая чужая (заявки в полёте
    с таким именем нет), вторая — наш выход. После чужой состояние уже
    не сходится со счётом, и разбирать дальше нельзя; но молча выбросить
    вторую — значит скрыть от человека, что движок отстал от счёта ещё
    на одну сделку (правило 13).
    """
    state, exit_order = _closing_long()
    stranger = Fill(
        action=OrderAction.CLOSE, side=Side.LONG, volume=1.0, price=PRICE,
        at=candle(10, 10).time, order_id="close:long:чужая",
    )
    ours = Fill(
        action=OrderAction.CLOSE, side=Side.LONG, volume=1.0, price=PRICE + 300,
        at=candle(10, 10).time, order_id=exit_order.order_id,
    )
    lines: list[JournalEntry] = []
    engine = Engine(ScriptedStrategy([]), WORKING, state=state, journal=lines.append)
    executor = HaltingOnFills(ExecutionHalted(EXCHANGE_SAYS, fills=[stranger, ours]))
    asyncio.run(engine.on_market_candle(candle(10, 10, close=PRICE), executor))

    assert engine.halted
    lost = [entry for entry in lines if entry.event == "Сделки исполнителя не разобраны"]
    assert len(lost) == 1, "сделка за отвергнутой выброшена молча"
    assert f"{PRICE + 300:g}" in lost[0].reason


def test_the_halt_line_says_which_orders_of_the_candle_were_never_sent() -> None:
    """Остановка на снятии тейка: строка говорит, что выход этой свечи не подавался.

    Выше в журнале стоит решение «выход из лонга» — без оговорки человек
    прочитал бы его как поданный выход и искал бы у брокера заявку,
    которой не было.
    """
    held = replace(
        position(Side.LONG, price=PRICE),
        take=TakeProfit(percent=0.5, level=211_050.0),
    )
    state = EngineState(
        position=held, pending=(_arm_order(held, bar(10, 5).closes_at, 211_050.0),)
    )
    lines: list[JournalEntry] = []
    engine = Engine(
        ScriptedStrategy([decision(Intent.SHORT, close=PRICE)]),
        WORKING.replace(take_profit=True), state=state, journal=lines.append,
    )
    executor = HaltingOnSubmit(ExecutionHalted(EXCHANGE_SAYS))
    asyncio.run(engine.on_market_candle(candle(10, 5, close=PRICE), executor))

    assert [order.action for order in executor.submitted] == [OrderAction.CANCEL_TAKE_PROFIT]
    halt = [entry for entry in lines if entry.event == HALT_EVENT]
    assert len(halt) == 1
    assert "Ещё 1 заявка этой свечи не подавалась" in halt[0].reason


def test_the_model_halts_on_cancelling_the_named_level_and_only_once() -> None:
    """`halt_on` по имени снимаемого уровня — снятие кончается остановкой, один раз.

    Боевой случай: биржа сняла тейк сама, и снятие его исполнитель встречает
    остановкой. Имя у снятия своё (`cancel:take:…`), тест называет уровень.
    Стережёт обещание модели из её докстринга: остановка по имени того, кого
    заявка снимает, и однократность — повтор той же подачи остановкой
    не кончается.
    """
    held = replace(
        position(Side.LONG, price=PRICE), take=TakeProfit(percent=0.5, level=211_050.0)
    )
    cancel = _cancel_order(held, bar(10, 5).closes_at, "выход по сигналу")
    model = NextOpenExecutor(halt_on={take_order_id(held): EXCHANGE_SAYS})

    with pytest.raises(ExecutionHalted) as halted:
        asyncio.run(model.submit(cancel))
    assert halted.value.human == EXCHANGE_SAYS
    try:
        asyncio.run(model.submit(cancel))
    except ExecutionHalted:
        pytest.fail("остановка по требованию сработала второй раз")
    except ExecutionRefused:
        pass  # снимать нечего — это уже не остановка


def test_an_exit_deal_short_of_the_position_stops_the_robot() -> None:
    """Выход на объём заявки, но не позиции, — тоже остановка, а не строка и дальше.

    Вторая дорога частичного исполнения (решение 0066, Р6): сделка совпала
    со своей заявкой (1 из 1), а позиция у движка — 2. Закрыть её по такой
    сделке нельзя, и работать дальше тоже: остаток на счёте движку не виден.
    Стережёт: робот остановлен строкой «Сделка на выход не на весь объём»,
    позиция не тронута, свеча в модуль не ушла, заявок не подано.
    """
    held = position(Side.LONG, price=PRICE, volume=2.0)
    exit_order = OrderRequest(
        action=OrderAction.CLOSE, side=Side.LONG, volume=1.0,
        submitted_at=bar(10, 5).closes_at, reason="выход", order_id="close:long:тест",
        exit_reason=ExitReason.SIGNAL,
    )
    state = EngineState(position=held, pending=(exit_order,))
    short_of_it = Fill(
        action=OrderAction.CLOSE, side=Side.LONG, volume=1.0, price=PRICE,
        at=candle(10, 10).time, order_id=exit_order.order_id,
    )
    lines: list[JournalEntry] = []
    strategy = ScriptedStrategy([decision(Intent.SHORT, close=PRICE)])
    engine = Engine(strategy, WORKING, state=state, journal=lines.append)
    executor = RecordingExecutor(scripted=[[short_of_it]])
    asyncio.run(engine.on_market_candle(candle(10, 10, close=PRICE), executor))

    assert engine.halted.startswith("Сделка на выход не на весь объём"), engine.halted
    assert engine.position is not None and engine.position.volume == 2.0
    assert strategy.seen == [], "после неполного выхода движок решал дальше"
    assert executor.submitted == []
    assert lines[-1].level is JournalLevel.ERROR
    assert "Робот остановлен" in lines[-1].reason
