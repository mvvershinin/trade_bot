"""Событие «уровень не исполнился» — сторона движка (решение 0067, Р-10).

Касание скользящего уровня, найденное после обрыва связи; выход по нему подан
заявкой «исполнить целиком или снять» и снят целиком. Исполнитель отдаёт движку
`LevelUnfilled` в ответе `fills_at`, а решает движок — таблицей 0067:

* в убытке, «Заходить заново при обрыве связи» включена — держать;
* в убытке, выключена — закрыть и до конца дня не входить;
* не в убытке — закрыть, выход засчитывается тейком.

Номера в докстрингах — пункты §6 плана координатора 04.11.2026
(`.docs/coordination/coordinator-2026-11-04-1233.md`), поведение из задания,
а не из кода. Сторона исполнителя (граница «после обрыва», `ALREADY_TRIGGERED`
до отдачи события, сделка касания не отдаётся) — фаза 2.1, здесь её нет.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time

import pytest

from backtest import ExecutionModel, Guard
from engine import (
    DayRecord,
    Engine,
    EngineSettings,
    EngineState,
    ExecutionHalted,
    ExitReason,
    Fill,
    JournalEntry,
    LevelUnfilled,
    Mode,
    OrderAction,
    OrderExecutor,
    OrderRequest,
    Position,
    PriceSeen,
    Refusal,
    Reversal,
    Side,
    TakeProfit,
    TradingWindow,
    close_time,
    closes_entries_day,
    is_take_order,
    limit_beyond,
    take_order_id,
)
from engine.pipeline import _arm_order
from engine.ports import ExecutionRefused
from market.day import DayEvent, day_of
from strategies import Intent
from tests.engine_helpers import (
    MSK,
    FakeCandle,
    NextOpenExecutor,
    ScriptedStrategy,
    candle,
    decision,
)

ENTRY = 1000.0
LEVEL = 1008.0
PEAK = 1010.0
STEP = 25.0
TODAY = date(2026, 6, 19)
ENTRY_AT = datetime(2026, 6, 19, 10, 5, tzinfo=MSK)
TOUCHED_AT = datetime(2026, 6, 19, 10, 12, tzinfo=MSK)

#: Весь день — окно, переворот в одной свече, скользящий уровень. Параметры
#: скользящего здесь НАРОЧНО другие, чем у плана позиции (`PLAN`): сброс плана
#: обязан взять план позиции, а не настройки (ТЗ §4.4 А).
SETTINGS = EngineSettings(
    mode=Mode.REVERSE,
    window=TradingWindow(start=time(0, 0), end=time(0, 0)),
    reversal=Reversal.SAME_BAR,
    take_profit=True,
    trailing_take_profit=True,
    trailing_start_percent=0.9,
    trailing_offset_percent=0.4,
    trailing_step_percent=0.1,
    price_step=STEP,
)

#: План, замороженный на входе: порог 0,5 %, отступ 0,2 %, вершина 1010.
PLAN = TakeProfit(
    percent=0.5, trailing=True, start_percent=0.5, offset_percent=0.2,
    step_percent=0.05, level=LEVEL, peak=PEAK,
)
HELD = Position(
    side=Side.LONG, volume=1.0, entry_price=ENTRY, entry_time=ENTRY_AT, take=PLAN,
)
ARM = _arm_order(HELD, datetime(2026, 6, 19, 10, 10, tzinfo=MSK), LEVEL)
EVENT = LevelUnfilled(
    order_id=take_order_id(HELD), at=TOUCHED_AT,
    reason="выход «исполнить целиком или снять» по уровню 1008 снят биржей",
)

Message = Fill | PriceSeen | LevelUnfilled


@dataclass
class _Scripted:
    """Исполнитель-сценарий: ответы `fills_at` по очереди, отказы по имени."""

    answers: list[Sequence[Message]] = field(default_factory=list)
    submitted: list[OrderRequest] = field(default_factory=list)
    refuse: dict[str, Refusal] = field(default_factory=dict)
    halt: ExecutionHalted | None = None

    async def submit(self, order: OrderRequest) -> None:
        if order.order_id in self.refuse:
            raise ExecutionRefused("так велел сценарий теста", self.refuse[order.order_id])
        self.submitted.append(order)

    async def fills_at(self, _candle: object) -> Sequence[Message]:
        if self.halt is not None:
            halted, self.halt = self.halt, None
            raise halted
        return self.answers.pop(0) if self.answers else ()


def _engine(
    intent: Intent = Intent.LONG,
    settings: EngineSettings = SETTINGS,
    state: EngineState | None = None,
    bars: int = 1,
) -> tuple[Engine, list[JournalEntry]]:
    lines: list[JournalEntry] = []
    engine = Engine(
        ScriptedStrategy([decision(intent)] * bars), settings,
        state=state if state is not None else EngineState(position=HELD, pending=(ARM,)),
        journal=lines.append,
    )
    return engine, lines


def _run(engine: Engine, executor: OrderExecutor, *candles: FakeCandle) -> None:
    async def go() -> None:
        for one in candles:
            await engine.on_market_candle(one, executor)
    asyncio.run(go())


def _bar_t(close: float) -> FakeCandle:
    """Свеча T 10:10–10:15, в обмене которой приходит событие."""
    return candle(10, 10, close=close)


# --------------------------------------------------------------------------
# 11. В убытке, настройка включена — держать
# --------------------------------------------------------------------------

def test_hold_in_a_loss_keeps_the_position_and_starts_the_trailing_over() -> None:
    """§6 п. 11: остановки нет, выхода нет, позиция та же, план — `initial()`.

    Стережёт: событие не останавливает робота и не рождает выход при
    включённой настройке; план тейка начинается заново из ПЛАНА ПОЗИЦИИ
    (порог, отступ и шаг — как при входе, а не из изменённых настроек);
    шаг 5 той же свечи не вооружает старую вершину 1010 (без сброса он
    подал бы вооружение на 1008 и замкнул круг «касание — снятый выход»).
    """
    executor = _Scripted(answers=[[EVENT]])
    engine, lines = _engine(Intent.LONG, SETTINGS.replace(hold_after_unfilled_level=True))
    _run(engine, executor, _bar_t(992.0))

    assert engine.halted == ""
    # Скользящий начинается заново — значит, и условная у брокера ставится
    # заново от цены входа (`D-196`), а не уровень от старой вершины.
    activation = PLAN.activation_level(ENTRY, 1)
    assert [(o.action, o.price, o.trail_offset) for o in executor.submitted] == [
        (OrderAction.ARM_TAKE_PROFIT, activation, PLAN.offset_percent)
    ], "в убытке с включённой настройкой — только условная заново, без выхода"
    held = engine.position
    assert held is not None and held.is_open
    assert (held.side, held.entry_price, held.volume) == (Side.LONG, ENTRY, 1.0)
    assert held.take == replace(PLAN.initial(ENTRY, 1), level=None, activation=activation)
    assert (held.take.start_percent, held.take.offset_percent) == (0.5, 0.2), (
        "план сброшен из настроек движка, а не из плана позиции"
    )
    assert held.take.peak is None and held.take.level is None
    assert engine.state.order_in_flight(EVENT.order_id) is None
    assert engine.state.entries_closed_date is None
    assert any(e.event == "Уровень не исполнился — позиция остаётся" for e in lines)


@pytest.mark.parametrize(
    ("reversal", "expected"),
    [
        (Reversal.SAME_BAR, [OrderAction.CLOSE, OrderAction.OPEN]),
        (Reversal.THROUGH_BAR, [OrderAction.CLOSE]),
    ],
    ids=["same-bar", "through-bar"],
)
def test_hold_then_the_average_against_reverses_on_the_same_bar(
    reversal: Reversal, expected: list[OrderAction]
) -> None:
    """§6 п. 11: средняя против — выход по сигналу той же свечи, без снятия тейка.

    Стережёт: после события решение по средней принимается на ЭТОЙ ЖЕ закрытой
    свече, «как будто касания не было»; снятия уровня нет — сторожа уже нет;
    переворот — по моменту переворота.
    """
    executor = _Scripted(answers=[[EVENT]])
    settings = SETTINGS.replace(hold_after_unfilled_level=True, reversal=reversal)
    engine, _ = _engine(Intent.SHORT, settings)
    _run(engine, executor, _bar_t(992.0))

    assert engine.halted == ""
    assert [order.action for order in executor.submitted] == expected
    close = executor.submitted[0]
    assert close.exit_reason is ExitReason.SIGNAL
    assert close.submitted_at == close_time(_bar_t(992.0))


# --------------------------------------------------------------------------
# 12. В убытке, настройка выключена — закрыть и до конца дня не входить
# --------------------------------------------------------------------------

def test_a_loss_without_the_setting_closes_at_the_bar_close_with_a_limit() -> None:
    """§6 п. 12: рождённый выход помечен закрытием свечи T, предел — закрытие − 2 %.

    Стережёт Н1: время заявки — закрытие свечи обмена, а не момент касания
    (иначе модель исполнит её по `open` той же свечи, а боевой исполнитель
    не найдёт цены свечи сигнала). Имя — «закрывает день», не тейк.
    """
    executor = _Scripted(answers=[[EVENT]])
    engine, _ = _engine(Intent.LONG)
    _run(engine, executor, _bar_t(992.0))

    assert engine.halted == ""
    (born,) = executor.submitted
    assert born.action is OrderAction.CLOSE and born.side is Side.LONG
    assert born.submitted_at == close_time(_bar_t(992.0)) != EVENT.at
    assert born.limit_price == limit_beyond(992.0, 2.0, STEP, buying=False) == 950.0
    assert born.exit_reason is ExitReason.LEVEL_UNFILLED
    assert closes_entries_day(born.order_id) and not is_take_order(born.order_id)
    assert engine.state.entries_closed_date == TODAY
    assert engine.position is not None and not engine.position.is_open


def test_a_closed_day_holds_even_with_the_stop_after_take_off() -> None:
    """§6 п. 12: входов до конца даты нет и при выключенном «стопе после тейка».

    Стережёт: запрет — своё правило шага 9, а не «стоп после тейка».
    """
    settings = SETTINGS.replace(stop_after_take_profit=False)
    state = EngineState(entries_closed_date=TODAY)
    engine, lines = _engine(Intent.LONG, settings, state)
    _run(engine, _Scripted(), candle(10, 30))
    assert engine.state.pending == ()
    assert any(
        e.event == "Сделок сегодня больше не будет" and "неисполненного уровня" in e.reason
        for e in lines
    )

    tomorrow = candle(10, 30, day=22)  # понедельник
    engine2, _ = _engine(Intent.LONG, settings, EngineState(entries_closed_date=TODAY))
    executor = _Scripted()
    _run(engine2, executor, tomorrow)
    assert [order.action for order in executor.submitted] == [OrderAction.OPEN], (
        "запрет — до конца ДАТЫ, а не навсегда"
    )


def test_a_closed_day_survives_a_restart_without_an_execution() -> None:
    """§6 п. 12: после перезапуска в ту же дату — тоже нет, и без исполнения выхода.

    Стережёт движковую половину Н5: значение дня с `entries_closed_date`
    (его соберёт слой данных по команде брокеру, фаза 2.3) закрывает день,
    даже когда ни тейка, ни выхода в этот день движку не сообщено.
    """
    engine, lines = _engine(Intent.LONG, SETTINGS, EngineState())
    record = DayRecord(day=TODAY, entries_closed_date=TODAY)
    engine.on_day(record, at=datetime(2026, 6, 19, 10, 0, tzinfo=MSK))
    executor = _Scripted()
    _run(engine, executor, candle(10, 30))
    assert executor.submitted == []
    assert any("до конца дня не входить" in e.reason for e in lines)


def test_the_closed_day_date_does_not_go_back() -> None:
    """Дата закрытого дня назад не едет: старое значение дня её не отменяет."""
    engine, _ = _engine(Intent.LONG, SETTINGS, EngineState(entries_closed_date=TODAY))
    engine.on_day(
        DayRecord(day=TODAY, entries_closed_date=date(2026, 6, 18)),
        at=datetime(2026, 6, 19, 10, 0, tzinfo=MSK),
    )
    assert engine.state.entries_closed_date == TODAY


# --------------------------------------------------------------------------
# 13. Не в убытке — выход засчитывается тейком
# --------------------------------------------------------------------------

def test_not_in_a_loss_closes_and_leaves_the_day_open() -> None:
    """§6 п. 13: рождённый выход с именем тейка; день решением не закрыт.

    Равенство закрытия и цены входа — «не в убытке».
    """
    executor = _Scripted(answers=[[EVENT]])
    engine, _ = _engine(Intent.LONG, SETTINGS.replace(hold_after_unfilled_level=True))
    _run(engine, executor, _bar_t(ENTRY))

    (born,) = executor.submitted
    assert is_take_order(born.order_id) and not closes_entries_day(born.order_id)
    assert born.submitted_at == close_time(_bar_t(ENTRY))
    assert engine.state.entries_closed_date is None


# --------------------------------------------------------------------------
# Модель прогона: настоящее касание, исполнение по open T+1
# --------------------------------------------------------------------------

def _model_run(
    t_close: float,
    after: Sequence[tuple[float, float, float, float]],
    settings: EngineSettings,
    intents: Sequence[Intent],
) -> tuple[Engine, NextOpenExecutor, list[JournalEntry], list[FakeCandle]]:
    """Вход по open 1000, вершина 1010, касание 1008 на T, дальше свечи `after`.

    `after` — (open, high, low, close) свечей T+1, T+2, … Событие выдаёт
    модель по требованию теста (`unfilled_on`), а не сценарий.
    """
    model = NextOpenExecutor()
    lines: list[JournalEntry] = []
    script = [decision(Intent.LONG), decision(Intent.LONG)]
    script += [decision(intent) for intent in intents]
    engine = Engine(ScriptedStrategy(script), settings, journal=lines.append)
    signal = candle(10, 0, close=1000.0)
    rise = replace(candle(10, 5, close=1010.0), open=ENTRY, low=ENTRY)
    touch = replace(candle(10, 10, close=t_close), open=1009.0, high=1009.0, low=t_close - 2)
    bars: list[FakeCandle] = [signal, rise, touch]
    for number, (o, h, low, c) in enumerate(after):
        bars.append(replace(candle(10, 15 + 5 * number, close=c), open=o, high=h, low=low))

    async def go() -> None:
        for number, one in enumerate(bars):
            if number == 2:
                held = engine.position
                assert held is not None and held.take.level is not None, (
                    "сценарий сломан: к свече касания скользящий уровень не вооружён"
                )
                model.unfilled_on[take_order_id(held)] = "снята целиком"
            await engine.on_market_candle(one, model)
    asyncio.run(go())
    return engine, model, lines, bars


#: Скользящий уровень у плана позиции: порог 0,5 %, отступ 0,2 %.
MODEL_SETTINGS = SETTINGS.replace(
    trailing_start_percent=0.5, trailing_offset_percent=0.2, trailing_step_percent=0.05,
)


def test_the_model_fills_the_born_exit_at_the_next_open_not_before_the_touch() -> None:
    """§6 п. 12: в модели рождённый выход исполняется по `open` T+1.

    Стережёт Н1 сквозь настоящую модель: выход, помеченный временем касания
    (начало T), модель исполнила бы следующим кругом обмена по `open` той же T
    — 1009, по цене ДО касания. Здесь он обязан пройти по `open` T+1 — 995.
    """
    engine, model, _, bars = _model_run(
        992.0, [(995.0, 996.0, 990.0, 994.0), (994.0, 995.0, 993.0, 994.0)],
        MODEL_SETTINGS, [Intent.LONG, Intent.LONG, Intent.LONG],
    )
    exits = [fill for fill in model.fills if fill.action is OrderAction.CLOSE]
    (deal,) = exits
    assert closes_entries_day(deal.order_id)
    assert deal.at == bars[3].time and deal.price == 995.0
    assert engine.halted == ""
    assert engine.position is None
    assert [o for o in model.submitted_market if o.action is OrderAction.OPEN][1:] == [], (
        "после закрытого решением дня робот вошёл снова"
    )


def test_the_model_gives_no_event_without_the_test_asking() -> None:
    """§6 п. 20: без требования теста касание — обычная сделка по уровню.

    Стережёт: модель прогона событие сама не выдаёт — образец сделок
    прогона по истории от Р-10 не меняется.
    """
    model = NextOpenExecutor()
    engine = Engine(
        ScriptedStrategy([decision(Intent.LONG)] * 3), MODEL_SETTINGS,
    )
    signal = candle(10, 0, close=1000.0)
    rise = replace(candle(10, 5, close=1010.0), open=ENTRY, low=ENTRY)
    touch = replace(candle(10, 10, close=992.0), open=1009.0, high=1009.0, low=990.0)

    async def go() -> None:
        for one in (signal, rise, touch):
            await engine.on_market_candle(one, model)
    asyncio.run(go())
    exits = [fill for fill in model.fills if fill.action is OrderAction.CLOSE]
    (deal,) = exits
    assert deal.order_id.startswith("take:") and deal.price == LEVEL


def test_the_born_exit_waits_from_the_next_bar() -> None:
    """§6 п. 18: неисполненный рождённый выход — остановка, считая с T+1.

    `time_exit_wait_bars = 1`: T не считается, T+1 — одна свеча ожидания,
    остановка — на T+2. Счёт с T остановил бы робота на свечу раньше, чем
    выход по концу окна с тем же пределом (П2).
    """
    stuck = (940.0, 945.0, 930.0, 940.0)  # предел продажи 950 не достигнут
    engine, _, lines, _ = _model_run(
        992.0, [stuck], MODEL_SETTINGS, [Intent.LONG, Intent.LONG],
    )
    assert engine.halted == "", "остановка на T+1 — счёт ожидания начат со свечи T"

    engine, _, lines, _ = _model_run(
        992.0, [stuck, stuck], MODEL_SETTINGS, [Intent.LONG, Intent.LONG, Intent.LONG],
    )
    assert "Выход с предельной ценой не исполнился" in engine.halted
    assert any(e.event == "Выход с предельной ценой не исполнился" for e in lines)


def test_a_take_by_the_born_exit_matches_the_day_rebuilt_from_the_journal() -> None:
    """§6 п. 13: сделка «не в убытке» ставит дату тейка — та же, что у `day_of`.

    Стережёт Н3/В4: признак тейка у сделки — по ИМЕНИ заявки, одним
    предикатом в сессии (`_closed`) и после перезапуска (`is_take_order`
    в слое данных). Второго входа в ту же дату при «стопе после тейка» нет —
    ни в сессии, ни в новом движке, получившем день из журнала.
    """
    engine, model, _, bars = _model_run(
        1002.0, [(1001.0, 1003.0, 1000.0, 1002.0), (1002.0, 1003.0, 1001.0, 1002.0)],
        MODEL_SETTINGS, [Intent.LONG, Intent.LONG, Intent.LONG],
    )
    (deal,) = [fill for fill in model.fills if fill.action is OrderAction.CLOSE]
    assert is_take_order(deal.order_id)
    assert engine.state.take_profit_date == TODAY
    assert engine.state.entries_closed_date is None
    entries = [o for o in model.submitted_market if o.action is OrderAction.OPEN]
    assert len(entries) == 1, "после тейка робот вошёл снова в ту же дату"

    events = [
        DayEvent(at=fill.at, gross=0.0, commission=None,
                 exit=fill.action is OrderAction.CLOSE, take=is_take_order(fill.order_id))
        for fill in model.fills
    ]
    rebuilt = day_of(events, account=None, day=TODAY)
    assert rebuilt.take_profit_date == engine.state.take_profit_date

    restarted, _ = _engine(Intent.LONG, MODEL_SETTINGS, EngineState())
    restarted.on_day(
        DayRecord(day=TODAY, take_profit_date=rebuilt.take_profit_date),
        at=datetime(2026, 6, 19, 10, 30, tzinfo=MSK),
    )
    executor = _Scripted()
    _run(restarted, executor, candle(10, 30))
    assert executor.submitted == []


def test_not_in_a_loss_with_the_stop_after_take_off_trades_on() -> None:
    """§6 п. 13: «не в убытке» дату дня НЕ закрывает — без «стопа после тейка» вход есть.

    Стережёт мутацию «закрыть дату и здесь»: закрытый решением день не пустил
    бы вход и при выключенном «стопе после тейка».
    """
    settings = MODEL_SETTINGS.replace(stop_after_take_profit=False)
    _, model, _, _ = _model_run(
        1002.0, [(1001.0, 1003.0, 1000.0, 1002.0), (1002.0, 1003.0, 1001.0, 1002.0)],
        settings, [Intent.LONG, Intent.LONG, Intent.LONG],
    )
    entries = [o for o in model.submitted_market if o.action is OrderAction.OPEN]
    assert len(entries) == 2


# --------------------------------------------------------------------------
# 15. Остановка со словами: событие разобрать нельзя
# --------------------------------------------------------------------------

def _fixed_held() -> tuple[Position, OrderRequest]:
    held = replace(HELD, take=TakeProfit(percent=0.5, level=1005.0))
    return held, _arm_order(held, datetime(2026, 6, 19, 10, 5, tzinfo=MSK), 1005.0)


@pytest.mark.parametrize(
    ("case", "words"),
    [
        ("no-order", "в полёте нет"),
        ("fixed-take", "неподвижного тейка"),
        ("unsettled", "незакрытой свече"),
    ],
)
def test_an_event_that_cannot_be_read_halts_with_words(case: str, words: str) -> None:
    """§6 п. 15: без заявки в полёте, для неподвижного тейка, по незакрытой свече.

    Стережёт молчание: событие не пропускается — робот останавливается,
    причина названа и в журнале, и в `halted`; заявок нет.
    """
    state = EngineState(position=HELD, pending=(ARM,))
    event = EVENT
    bar = _bar_t(992.0)
    if case == "no-order":
        state = EngineState(position=HELD)
    elif case == "fixed-take":
        held, arm = _fixed_held()
        state = EngineState(position=held, pending=(arm,))
        event = replace(EVENT, order_id=arm.order_id)
    else:
        bar = replace(bar, unsettled=True)
    executor = _Scripted(answers=[[event]])
    engine, lines = _engine(Intent.LONG, SETTINGS, state)
    _run(engine, executor, bar)

    assert words in engine.halted
    assert executor.submitted == []
    assert any(e.event == "Событие исполнителя не разобрано" and words in e.reason
               for e in lines)
    assert engine.state.position == state.position, "состояние при отказе не трогается"


# --------------------------------------------------------------------------
# 16. Отказ рождённого выхода — остановка своими словами
# --------------------------------------------------------------------------

def test_a_refused_born_exit_halts_with_its_own_words() -> None:
    """§6 п. 16: «выход после неисполненного уровня не принят, позиция открыта без сторожа».

    Стережёт молчание: общая строка отказа выхода обещала бы «выход повторит
    следующая свеча» — повторять некому. Заодно Н2: план позиции к этому
    моменту уже сброшен, и вершина 1010 не вооружится заново.
    """
    born = f"unfilled-day:long:{close_time(_bar_t(992.0)).isoformat()}"
    executor = _Scripted(answers=[[EVENT]], refuse={born: Refusal.UNKNOWN})
    engine, lines = _engine(Intent.LONG)
    _run(engine, executor, _bar_t(992.0))

    assert "Выход после неисполненного уровня не принят, позиция открыта без сторожа" in (
        engine.halted
    )
    assert not any("повторит следующая свеча" in e.reason for e in lines)
    assert executor.submitted == []
    held = engine.position
    assert held is not None and held.take.peak is None and held.take.level is None


@pytest.mark.parametrize(
    ("close", "prefix", "warned"),
    [(992.0, "unfilled-day", True), (ENTRY, "unfilled-take", False)],
    ids=["closes-the-day", "take"],
)
def test_a_refused_day_closing_exit_says_not_to_resume_today(
    close: float, prefix: str, warned: bool
) -> None:
    """`/risk` 04.11.2026 п. 4: отказ выхода «до конца дня не входить» — «сегодня не возобновляйте».

    Известная дыра Р-10: заявку могли отклонить до записи в журнал команд,
    и после «Возобновить» запрет на вход не восстановится. Стережёт, что это
    сказано в строке остановки — там, где человек решает, возобновлять ли
    работу, — и сказано ТОЛЬКО у выхода, закрывающего день: у выхода «не
    в убытке» запрета нет, и предупреждение о нём было бы неправдой.
    Мутации: убрать хвост — падает первый случай; печатать его всегда — второй.
    """
    born = f"{prefix}:long:{close_time(_bar_t(close)).isoformat()}"
    executor = _Scripted(answers=[[EVENT]], refuse={born: Refusal.UNKNOWN})
    engine, lines = _engine(Intent.LONG)
    _run(engine, executor, _bar_t(close))

    stop = next(e for e in lines if e.event == "Выход после неисполненного уровня не принят")
    for said in (engine.halted, stop.reason):
        assert ("сегодня не возобновляйте" in said) is warned, said
        assert (
            "запрет на вход до конца дня после перезапуска не восстановится" in said
        ) is warned, said


# --------------------------------------------------------------------------
# 17. Остановка посреди пачки — событие названо, а не выброшено
# --------------------------------------------------------------------------

def test_an_event_carried_by_a_halt_is_named() -> None:
    """§6 п. 17: событие, отданное вместе с `ExecutionHalted`, — своя строка.

    Стережёт Н6: фильтр «только сделки» в разборе остановки выбросил бы его.
    """
    executor = _Scripted(halt=ExecutionHalted("поток статусов оборвался", unfilled=[EVENT]))
    engine, lines = _engine()
    _run(engine, executor, _bar_t(992.0))

    named = [e for e in lines if e.event == "Событие исполнителя не разобрано"]
    assert len(named) == 1 and EVENT.order_id in named[0].reason
    assert "уровень не исполнился» (1) названы" in engine.halted


def test_an_event_behind_a_halting_deal_is_named() -> None:
    """§6 п. 17: остаток пачки после сделки, остановившей робота, — событие названо."""
    stray = Fill(
        action=OrderAction.CLOSE, side=Side.LONG, volume=1.0, price=1001.0,
        at=datetime(2026, 6, 19, 10, 10, tzinfo=MSK), order_id="чужая",
    )
    executor = _Scripted(answers=[[stray, EVENT]])
    engine, lines = _engine()
    _run(engine, executor, _bar_t(992.0))

    assert "Сделка без своей заявки" in engine.halted
    assert any(
        e.event == "Событие исполнителя не разобрано" and EVENT.order_id in e.reason
        for e in lines
    )


def test_the_model_carries_the_event_with_its_halt() -> None:
    """§6 п. 17: модель отдаёт событие вместе с остановкой, а не теряет его."""
    model = ExecutionModel(halt_at={_bar_t(992.0).time: "так велел сценарий"})
    model.armed[ARM.order_id] = Guard(order=ARM, leg=None)
    model.unfilled_on[ARM.order_id] = "снята целиком"
    touch = replace(_bar_t(992.0), open=1009.0, high=1009.0, low=990.0)
    with pytest.raises(ExecutionHalted) as halted:
        asyncio.run(model.fills_at(touch))
    assert halted.value.fills == ()
    assert [e.order_id for e in halted.value.unfilled] == [ARM.order_id]


def test_the_fact_book_passes_the_event_to_the_engine() -> None:
    """§6 п. 17: обёртка «факта» (`app/observe.py`) отдаёт движку ответ без отбора."""
    from app.observe import _FactBook

    book = _FactBook(_Scripted(answers=[[EVENT]]), ruble_per_point=1.0)
    answer = asyncio.run(book.fills_at(_bar_t(992.0)))
    assert list(answer) == [EVENT]
    assert book.fills == []


# --------------------------------------------------------------------------
# 19. «Выключен»
# --------------------------------------------------------------------------

def test_off_still_sends_the_born_exit() -> None:
    """§6 п. 19: в «Выключен» рождённый выход подаётся (П1)."""
    executor = _Scripted(answers=[[EVENT]])
    engine, _ = _engine(Intent.LONG, SETTINGS.replace(mode=Mode.OFF))
    _run(engine, executor, _bar_t(992.0))
    assert [o.action for o in executor.submitted] == [OrderAction.CLOSE]
    assert engine.halted == ""


def test_off_holds_without_a_level_and_without_a_reversal_and_says_so() -> None:
    """§6 п. 19: «держать» в «Выключен» — без уровня и без переворота, строка журнала."""
    executor = _Scripted(answers=[[EVENT]])
    settings = SETTINGS.replace(mode=Mode.OFF, hold_after_unfilled_level=True)
    engine, lines = _engine(Intent.SHORT, settings)
    _run(engine, executor, _bar_t(992.0))

    assert executor.submitted == []
    held = engine.position
    assert held is not None and held.is_open and held.take.level is None
    line = next(e for e in lines if e.event == "Уровень не исполнился — позиция остаётся")
    assert "«Выключен»" in line.reason and "ни сторожимого уровня, ни переворота" in line.reason


# --------------------------------------------------------------------------
# Имена и контракт
# --------------------------------------------------------------------------

def test_the_names_of_the_two_born_exits_are_told_apart_by_one_predicate_each() -> None:
    """Н3: тейк — вооружение и выход «не в убытке»; закрытый день — выход «в убытке»."""
    at = close_time(_bar_t(992.0)).isoformat()
    assert is_take_order(take_order_id(HELD))
    assert is_take_order(f"unfilled-take:long:{at}")
    assert not is_take_order(f"unfilled-day:long:{at}")
    assert closes_entries_day(f"unfilled-day:long:{at}")
    for name in (take_order_id(HELD), f"unfilled-take:long:{at}", f"close:long:{at}",
                 f"open:long:{at}", f"cancel:{take_order_id(HELD)}:{at}", ""):
        assert not closes_entries_day(name), name


def test_the_born_exit_passes_the_field_rules_with_a_limit() -> None:
    """Предел у заявки на выход после неисполненного уровня разрешён; у входа — нет."""
    with pytest.raises(ValueError, match="после неисполненного уровня"):
        OrderRequest(
            action=OrderAction.OPEN, side=Side.LONG, volume=1.0,
            submitted_at=ENTRY_AT, reason="тест", order_id="open:тест", limit_price=950.0,
        )
