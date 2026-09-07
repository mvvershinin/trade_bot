"""Склейка движка: перекладка свечи, два порта, синхронное ядро и адаптер.

Здесь же — сторожевые проверки уровня архитектуры, каждая из которых ломается
одной строкой и при этом ничего не роняет:

* движок не различает боевой путь и прогон по истории (ARCHITECTURE.md §1);
* у движка нет своих часов: «сейчас» — это время закрытия поданной свечи;
* `async` живёт только на портах-адаптерах, разбор свечи синхронный
  (решение 0005);
* свеча слоя данных не проходит в модуль стратегии молча, и наоборот.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
from datetime import datetime, time, timedelta

import pytest

from engine import (
    Engine,
    EngineSettings,
    EngineState,
    Fill,
    JournalEntry,
    JournalLevel,
    MarketCandle,
    Mode,
    OrderAction,
    OrderRequest,
    PartialCandles,
    PositionState,
    Side,
    Step,
    TradingWindow,
    check_market_candle,
    close_time,
    intake,
    to_bar,
)
from strategies import Bar, EmaReverse, Intent
from tests.engine_helpers import (
    MSK,
    FakeCandle,
    FakeTimeframe,
    ListSource,
    NextOpenExecutor,
    RecordingExecutor,
    ScriptedStrategy,
    candle,
    decision,
    position,
)

ENGINE = pathlib.Path(__file__).resolve().parent.parent / "engine"
SOURCES = sorted(path for path in ENGINE.rglob("*.py") if "__pycache__" not in path.parts)
WINDOW = TradingWindow(time(10, 5), time(11, 0))
WORKING = EngineSettings(mode=Mode.REVERSE, window=WINDOW, take_profit=False)
#: ⚠️ Тейк здесь выключен намеренно, и не «чтобы не мешал». Уровень
#: округляется до ЦЕЛОГО (PROTOTYPE.md §5), а цены в этих тестах — около ста:
#: 0,5% от 100 это 0,5, банковское округление сводит уровень обратно к 100,
#: и тейк срабатывает в той же свече, в которой позиция открылась. На реальном
#: инструменте (цена ~210 000, тейк ~1 050 пунктов) этого не бывает. Само это
#: свойство проверяется отдельно, в tests/test_engine_take_profit.py.
WITH_TAKE = WORKING.replace(take_profit=True)


# --------------------------------------------------------------------------
# Перекладка свечи — шаг 4 и защита от подмены слоёв
# --------------------------------------------------------------------------

def test_the_close_time_is_the_start_plus_the_timeframe() -> None:
    """У свечи хранится только начало. Закрытие движок считает сам.

    Считать иначе — значит сдвинуть торговое окно на одну свечу.
    """
    assert close_time(candle(10, 5)) == datetime(2026, 6, 19, 10, 10, tzinfo=MSK)
    assert close_time(candle(10, 0, minutes=15)) == datetime(
        2026, 6, 19, 10, 15, tzinfo=MSK
    )


def test_the_conversion_puts_the_close_time_into_closes_at() -> None:
    """Свеча слоя данных → свеча модуля: `time` начала становится `closes_at`."""
    converted = to_bar(candle(10, 5, close=284_800.0))
    assert isinstance(converted, Bar)
    assert converted.closes_at == datetime(2026, 6, 19, 10, 10, tzinfo=MSK)
    assert converted.close == 284_800.0
    assert not hasattr(converted, "time")


def test_a_bar_of_the_strategy_layer_is_refused_by_the_source_side() -> None:
    """Обратная подмена тоже ловится: объект с `closes_at` — не свеча источника.

    Проверка стоит с обеих сторон: `strategies.check_bar` отвергает `time`,
    а движок отвергает `closes_at`. Иначе весь график и все сделки уезжают
    на один бар, и заметить это можно только сравнением с чужим терминалом.
    """
    wrong = Bar(closes_at=datetime(2026, 6, 19, 10, 10, tzinfo=MSK),
                open=1.0, high=1.0, low=1.0, close=1.0)
    with pytest.raises(TypeError, match="closes_at"):
        check_market_candle(wrong)


def test_an_object_without_the_needed_fields_is_refused() -> None:
    with pytest.raises(TypeError, match="не свеча источника"):
        check_market_candle(object())


def test_a_timeframe_without_minutes_is_refused() -> None:
    """Без размера свечи время закрытия считать нечем — отказ вслух."""
    class Empty:
        pass

    broken = FakeCandle(time=datetime(2026, 6, 19, 10, 5, tzinfo=MSK))
    object.__setattr__(broken, "timeframe", Empty())
    with pytest.raises(TypeError, match="минутах"):
        check_market_candle(broken)


def test_an_unsettled_candle_never_reaches_the_strategy() -> None:
    """Незакрытая свеча в решении не участвует. Это правило, а не настройка."""
    taken = intake(candle(10, 5, unsettled=True))
    assert taken.accepted is False
    assert "не закрыта" in taken.skipped

    strategy = ScriptedStrategy([])
    engine = Engine(strategy, WORKING)
    outcome = engine.on_candle(candle(10, 5, unsettled=True))
    assert strategy.seen == [], "незакрытая свеча попала в среднюю"
    assert outcome.steps == ()
    assert outcome.skipped.startswith("Свеча ещё не закрыта")


def test_an_unsettled_candle_does_not_write_a_line_in_the_decision_journal() -> None:
    """Незакрытая свеча — не событие, а поток данных.

    Живой поток присылает текущий, ещё не закрытый бар постоянно. Строка
    на каждый его приход утопила бы настоящие события в журнале решений:
    288 строк в день на пятиминутках только за то, что данные идут.
    """
    lines = []
    engine = Engine(ScriptedStrategy([]), WORKING, journal=lines.append)
    for _ in range(20):
        engine.on_candle(candle(10, 5, unsettled=True))
    assert lines == []


def test_a_dropped_incomplete_candle_is_always_written_down() -> None:
    """Отброшенная неполная свеча меняет ряд средней — молчать об этом нельзя."""
    lines = []
    engine = Engine(
        ScriptedStrategy([]),
        WORKING.replace(partial_candles=PartialCandles.SKIP),
        journal=lines.append,
    )
    engine.on_candle(candle(10, 5, filled_minutes=2))
    assert len(lines) == 1
    assert "не войдёт ни в среднюю" in lines[0].reason
    # ⚠️ И вторая половина той же правды: сделки по этой свече движок
    # принимает. Настройка про РЯД для индикатора, а не про исполнение —
    # торги в интервале шли, неполна была наша сборка. Умолчать об этом
    # значило бы объяснить владельцу счёта сделку, которой в журнале нет.
    assert "исполняется по первой цене этого интервала" in lines[0].reason


def test_a_source_that_hides_the_completeness_is_refused_at_the_boundary() -> None:
    """Полнота свечи — обязательное поле порта, а не необязательное дополнение.

    Отказ обязан случиться на приёме и при **любой** настройке. Пока поле
    спрашивали только при включённой «пропускать неполные», получалась
    галочка, которая работает в тестере и роняет цикл обработки в бою —
    на первой же свече после того, как владелец счёта её включит.
    """
    class NoCount:
        time = datetime(2026, 6, 19, 10, 5, tzinfo=MSK)
        timeframe = FakeTimeframe(5)
        open = high = low = close = 100.0
        unsettled = False

    assert not isinstance(NoCount(), MarketCandle), (
        "порт перестал требовать полноту свечи — источник, который её "
        "не считает, снова проходит молча"
    )
    assert isinstance(candle(10, 5), MarketCandle), "проверка выродилась"

    with pytest.raises(TypeError, match="filled_minutes"):
        intake(NoCount())
    with pytest.raises(TypeError, match="filled_minutes"):
        intake(NoCount(), partial=PartialCandles.SKIP)
    with pytest.raises(TypeError, match="filled_minutes"):
        check_market_candle(NoCount())


def test_a_candle_without_a_timezone_is_refused_before_the_strategy_sees_it() -> None:
    """Наивное время отсеивается на приёме, а не на шаге 6.

    Позже — значит уже после того, как закрытие вошло в среднюю: ряд модуля
    испорчен, и прогон придётся начинать заново.
    """
    naive = FakeCandle(time=datetime(2026, 6, 19, 10, 5), timeframe=FakeTimeframe(5))
    strategy = ScriptedStrategy([decision(Intent.LONG)])
    engine = Engine(strategy, WORKING)
    with pytest.raises(ValueError, match="без часового пояса"):
        engine.on_candle(naive)
    assert strategy.seen == [], "испорченная свеча успела войти в среднюю"


def test_an_incomplete_candle_is_accepted_by_default() -> None:
    """Неполная свеча по умолчанию принимается.

    Для неликвидного времени неполнота это норма: биржа не отдаёт минуту,
    в которой не было сделок. Выброшенная свеча не входит в среднюю
    и сдвигает прогрев — то есть меняет список сделок.
    """
    partial = candle(10, 5, filled_minutes=2)
    assert intake(partial).accepted is True
    assert intake(partial, partial=PartialCandles.SKIP).accepted is False


def test_incomplete_candles_can_be_switched_off() -> None:
    strategy = ScriptedStrategy([])
    engine = Engine(strategy, WORKING.replace(partial_candles=PartialCandles.SKIP))
    outcome = engine.on_candle(candle(10, 5, filled_minutes=1))
    assert strategy.seen == []
    assert "1 минут из 5" in outcome.journal[0].reason


# --------------------------------------------------------------------------
# Свечи подаются модулю всегда
# --------------------------------------------------------------------------

def test_the_strategy_is_fed_even_when_the_robot_is_switched_off() -> None:
    """В режиме «Выключен» свеча всё равно доходит до модуля.

    У прототипа индикатор считает платформа, независимо от режима робота.
    Не подать свечу — значит выбросить её из средней: десять минут
    в «Выключен» дадут другой список сделок, и сверка это заметит поздно.
    Режимом фильтруется намерение, а не подача.
    """
    strategy = ScriptedStrategy([decision(Intent.LONG)])
    engine = Engine(strategy, EngineSettings(mode=Mode.OFF, window=WINDOW))
    outcome = engine.on_candle(candle(10, 5))
    assert len(strategy.seen) == 1
    assert outcome.orders == ()
    assert outcome.steps == (Step.MODE_OFF,)


def test_the_strategy_is_fed_outside_the_trading_window_and_on_weekends() -> None:
    """Средняя считается по всем свечам ряда, включая вечер и выходные.

    Окно и запрет торговли в выходные ограничивают только торговлю
    (PROTOTYPE.md §3). Иначе на границе дня средняя сбрасывалась бы.
    """
    strategy = ScriptedStrategy([decision(Intent.LONG) for _ in range(3)])
    engine = Engine(strategy, WORKING)
    engine.on_candle(candle(23, 55))              # вечерняя сессия
    engine.on_candle(candle(10, 5, day=20))       # суббота
    engine.on_candle(candle(3, 0, day=21))        # воскресенье, ночь
    assert len(strategy.seen) == 3


# --------------------------------------------------------------------------
# Синхронное ядро
# --------------------------------------------------------------------------

def test_the_core_needs_no_event_loop() -> None:
    """Разбор свечи — обычный вызов. Сверка обязана быть сравнением списков.

    Разбор корутиной затянул бы в каждую построчную сверку цикл событий,
    и воспроизводимость стала бы зависеть от планировщика (решение 0005).
    """
    strategy = ScriptedStrategy([decision(Intent.LONG)])
    engine = Engine(strategy, WORKING)
    outcome = engine.on_candle(candle(10, 5))
    assert [order.action for order in outcome.orders] == [OrderAction.OPEN]
    with pytest.raises(RuntimeError):
        asyncio.get_running_loop()


def test_the_journal_sink_receives_every_line() -> None:
    lines: list[JournalEntry] = []
    strategy = ScriptedStrategy([decision(Intent.LONG), decision(Intent.SHORT)])
    engine = Engine(strategy, WORKING, journal=lines.append)
    first = engine.on_candle(candle(10, 5))
    engine.on_fill(Fill(
        OrderAction.OPEN, Side.LONG, 1.0, 100.0,
        datetime(2026, 6, 19, 10, 10, tzinfo=MSK),
        order_id=first.orders[0].order_id,
    ))
    engine.on_candle(candle(10, 10))
    assert len(lines) >= 3
    assert all(entry.reason for entry in lines)


def test_settings_change_on_the_fly_and_the_journal_names_both_values() -> None:
    """Изменение настройки пишется с прежним и новым значением (ТЗ §4.4 А)."""
    strategy = ScriptedStrategy([decision(Intent.LONG)])
    engine = Engine(strategy, WORKING)
    engine.on_candle(candle(10, 5))
    entries = engine.apply_settings(WORKING.replace(volume=2.0, mode=Mode.LONG_ONLY))
    reasons = " | ".join(entry.reason for entry in entries)
    assert "Объём: 1 → 2" in reasons
    assert "Режим: Лонг и шорт (переворот) → Только лонг" in reasons
    assert engine.settings.volume == 2.0


def test_a_settings_change_does_not_touch_the_open_position() -> None:
    """Новое значение действует со следующей сделки, открытую позицию не трогает."""
    strategy = ScriptedStrategy([decision(Intent.LONG)])
    engine = Engine(
        strategy, WORKING, state=EngineState(position=position(Side.LONG, volume=1.0))
    )
    engine.on_candle(candle(10, 5))
    engine.apply_settings(WORKING.replace(volume=5.0))
    assert engine.position is not None
    assert engine.position.volume == 1.0


def test_the_engine_has_no_clock_of_its_own() -> None:
    """До первой свечи движок не знает, который час, и говорит это прямо."""
    engine = Engine(ScriptedStrategy([]), WORKING)
    with pytest.raises(ValueError, match="своих часов"):
        engine.apply_settings(WORKING.replace(volume=2.0))
    stamped = engine.apply_settings(
        WORKING.replace(volume=3.0), at=datetime(2026, 6, 19, 10, 10, tzinfo=MSK)
    )
    assert stamped[0].at == datetime(2026, 6, 19, 10, 10, tzinfo=MSK)


def test_an_unsettled_candle_does_not_move_the_engine_clock_into_the_future() -> None:
    """Время закрытия незакрытой свечи ещё не наступило.

    Стоило бы движку принять его за «сейчас», и запись об изменении настройки
    легла бы в журнал будущим временем — раньше, чем событие произошло.
    """
    engine = Engine(ScriptedStrategy([decision(Intent.LONG)]), WORKING)
    engine.on_candle(candle(10, 5))
    engine.on_candle(candle(10, 10, unsettled=True))
    entries = engine.apply_settings(WORKING.replace(volume=2.0))
    assert entries[0].at == datetime(2026, 6, 19, 10, 10, tzinfo=MSK)


def test_reset_starts_a_new_series_for_both_module_and_engine() -> None:
    strategy = ScriptedStrategy([decision(Intent.LONG)])
    engine = Engine(strategy, WORKING, state=EngineState(position=position(Side.LONG)))
    engine.on_candle(candle(10, 5))
    engine.reset()
    assert strategy.resets == 1
    assert engine.state == EngineState()


# --------------------------------------------------------------------------
# Порты
# --------------------------------------------------------------------------

def test_deals_are_collected_before_the_candle_is_analysed() -> None:
    """Сначала сделки этой свечи, потом разбор её закрытия.

    Заявка, поданная на прошлой свече, у прототипа исполняется по `open`
    текущей: к моменту закрытия позиция уже есть. Разбор по устаревшему
    состоянию дал бы вторую заявку на вход.
    """
    strategy = ScriptedStrategy([decision(Intent.LONG)])
    entry = OrderRequest(
        action=OrderAction.OPEN, side=Side.LONG, volume=1.0,
        submitted_at=datetime(2026, 6, 19, 10, 5, tzinfo=MSK),
        reason="заявка прошлой свечи", order_id="open:long:прошлая свеча",
    )
    engine = Engine(strategy, WORKING, state=EngineState(pending=(entry,)))
    executor = RecordingExecutor(scripted=[[Fill(
        OrderAction.OPEN, Side.LONG, 1.0, 100.0,
        datetime(2026, 6, 19, 10, 5, tzinfo=MSK), order_id=entry.order_id,
    )]])
    outcome = asyncio.run(engine.on_market_candle(candle(10, 5), executor))
    assert engine.position is not None, "сделка не доехала до состояния движка"
    assert outcome.orders == (), "движок вошёл второй раз поверх открытой позиции"
    assert executor.seen, "исполнителю не показали свечу"


def test_orders_reach_the_executor_after_the_candle_is_analysed() -> None:
    strategy = ScriptedStrategy([decision(Intent.LONG)])
    engine = Engine(strategy, WORKING)
    executor = RecordingExecutor()
    asyncio.run(engine.on_market_candle(candle(10, 5), executor))
    assert [order.action for order in executor.submitted] == [OrderAction.OPEN]
    assert executor.submitted[0].submitted_at == datetime(
        2026, 6, 19, 10, 10, tzinfo=MSK
    )


def test_the_whole_stream_runs_through_the_two_ports() -> None:
    """`run` — единственное место с вводом-выводом, и оно тонкое.

    Пять свечей: вход, разворот, вход в обратную сторону. Сделки считает
    подставная модель исполнения — движок не умеет исполнять заявки сам.
    """
    script = [decision(Intent.LONG), decision(Intent.SHORT), decision(Intent.SHORT),
              decision(Intent.SHORT), decision(Intent.SHORT)]
    strategy = ScriptedStrategy(script)
    engine = Engine(strategy, WORKING)
    executor = NextOpenExecutor()
    source = ListSource([
        candle(10, 5, close=100.0),
        candle(10, 10, close=101.0),
        candle(10, 15, close=99.0),
        candle(10, 20, close=98.0),
        candle(10, 25, close=97.0),
    ])
    asyncio.run(engine.run(source, executor))
    assert len(strategy.seen) == 5
    assert len(executor.deals) == 1
    deal = executor.deals[0]
    assert deal.side is Side.LONG
    # Заявка на вход подана на закрытии свечи 10:05–10:10, исполнена по открытию
    # следующей: временем сделки записано её НАЧАЛО, 10:10.
    assert deal.entry_time == datetime(2026, 6, 19, 10, 10, tzinfo=MSK)
    assert deal.exit_time == datetime(2026, 6, 19, 10, 15, tzinfo=MSK)
    assert engine.position is not None
    assert engine.position.side is Side.SHORT
    assert engine.position.entry_time == datetime(2026, 6, 19, 10, 20, tzinfo=MSK)


def test_a_reversal_takes_exactly_one_timeframe() -> None:
    """Между выходом и входом в обратную сторону ровно один таймфрейм.

    Не «до пяти минут», а ровно пять: выход исполняется по открытию свечи
    i+1, вход подаётся на её закрытии и исполняется по открытию i+2
    (PROTOTYPE.md §2).
    """
    script = [decision(Intent.LONG)] + [decision(Intent.SHORT)] * 5
    engine = Engine(ScriptedStrategy(script), WORKING)
    executor = NextOpenExecutor()
    source = ListSource([candle(10, 5 * step, close=100.0 + step) for step in range(1, 7)])
    asyncio.run(engine.run(source, executor))
    assert len(executor.deals) == 1
    exit_at = executor.deals[0].exit_time
    assert engine.position is not None, "переворот не состоялся"
    assert engine.position.side is Side.SHORT
    assert engine.position.entry_time - exit_at == timedelta(minutes=5)


# --------------------------------------------------------------------------
# Отказ исполнителя: заявка подаётся ДО того, как движок себе что-то запишет
# --------------------------------------------------------------------------

class Refusing:
    """Исполнитель, который заявку не принимает. Таймаут, отказ, обрыв связи.

    Для движка это один случай: он **не знает**, дошла ли заявка до брокера.
    """

    def __init__(self, error: BaseException | None = None) -> None:
        self.submitted: list[object] = []
        self.error = error or TimeoutError("ответ брокера не пришёл за 5 с")

    async def submit(self, order) -> None:
        self.submitted.append(order)
        raise self.error

    async def fills_at(self, market_candle):
        return ()


def test_a_refused_entry_is_not_written_down_as_submitted() -> None:
    """Отказ на входе: строка ERROR, явная остановка, состояние не тронуто.

    Раньше исключение улетало из цикла свечей и убивало его молча — без
    строки в журнале и без уровня ERROR.
    """
    lines: list[JournalEntry] = []
    engine = Engine(
        ScriptedStrategy([decision(Intent.LONG)]), WORKING, journal=lines.append
    )
    executor = Refusing()
    outcome = asyncio.run(engine.on_market_candle(candle(10, 5), executor))

    assert len(executor.submitted) == 1, "заявка вообще не дошла до исполнителя"
    assert outcome.orders, "разбор не принял решения"
    assert engine.state.pending == (), (
        "движок запомнил заявку, которую исполнитель не принял"
    )
    assert engine.position is None
    assert engine.halted, "робот молча продолжил работу после отказа"

    assert lines[-1].level is JournalLevel.ERROR
    assert lines[-1].event == "Заявка не подана"
    assert "двойной объём" in lines[-1].reason


def test_a_refused_exit_leaves_the_position_open_in_the_state_too() -> None:
    """Отказ на выходе: позиция не помечается закрывающейся.

    Иначе она закрыта в журнале и открыта на счёте, а робот ею больше
    не управляет: на следующих свечах ветка «заявка уже подана — ждём сделки»
    напишет одну строку, а дальше подавление повторов погасит и её.
    """
    lines: list[JournalEntry] = []
    engine = Engine(
        ScriptedStrategy([decision(Intent.SHORT)]),
        WORKING,
        state=EngineState(position=position(Side.LONG)),
        journal=lines.append,
    )
    asyncio.run(engine.on_market_candle(candle(10, 5), Refusing()))

    assert engine.position is not None
    assert engine.position.state is PositionState.OPEN, (
        "позиция закрыта в состоянии движка по заявке, которую не приняли"
    )
    assert engine.halted
    assert [entry.level for entry in lines][-1] is JournalLevel.ERROR
    assert any("подаём заявку на выход" in entry.reason.lower() for entry in lines), (
        "решение робота осталось без строки в журнале"
    )


def test_the_stream_stops_at_the_refusal_instead_of_running_on_blindly() -> None:
    """`run` доходит до отказа и останавливается. Не падает и не продолжает."""
    script = [decision(Intent.LONG)] * 4
    strategy = ScriptedStrategy(script)
    engine = Engine(strategy, WORKING)
    source = ListSource([candle(10, 5 * step) for step in range(1, 5)])
    asyncio.run(engine.run(source, Refusing()))

    assert engine.halted
    assert len(strategy.seen) == 1, "движок продолжил разбирать свечи после отказа"


def test_a_halted_engine_decides_nothing_and_says_so() -> None:
    """После остановки свеча не разбирается и в модуль не идёт."""
    strategy = ScriptedStrategy([decision(Intent.LONG)])
    engine = Engine(strategy, WORKING)
    asyncio.run(engine.on_market_candle(candle(10, 5), Refusing()))

    executor = RecordingExecutor()
    outcome = asyncio.run(engine.on_market_candle(candle(10, 10), executor))
    assert outcome.orders == ()
    assert outcome.steps == ()
    assert "остановлен" in outcome.skipped
    assert executor.submitted == []
    assert executor.seen == [], "остановленный робот всё равно спросил исполнителя"
    assert len(strategy.seen) == 1


def test_the_halt_is_lifted_only_by_starting_the_series_over() -> None:
    """Само по себе время остановку не снимает — снимает `reset()`."""
    engine = Engine(ScriptedStrategy([decision(Intent.LONG)]), WORKING)
    asyncio.run(engine.on_market_candle(candle(10, 5), Refusing()))
    assert engine.halted
    engine.reset()
    assert engine.halted == ""


def test_a_journal_that_refuses_a_line_stops_the_robot_too() -> None:
    """Запись журнала — ввод-вывод, и падать она может.

    Действие робота без строки в журнале — дефект (ТЗ §4.6). Значит отказ
    приёмника это остановка, а не «продолжим молча». И не смерть цикла:
    строки публикуются последними, состояние к этому моменту уже согласовано
    с исполнителем.
    """
    def broken(entry: JournalEntry) -> None:
        raise OSError("файл журнала недоступен")

    engine = Engine(ScriptedStrategy([decision(Intent.LONG)]), WORKING, journal=broken)
    executor = RecordingExecutor()
    outcome = asyncio.run(engine.on_market_candle(candle(10, 5), executor))

    assert [order.action for order in executor.submitted] == [OrderAction.OPEN], (
        "заявка не ушла — значит порядок «сначала подать» нарушен"
    )
    assert outcome.orders
    assert engine.halted
    assert "Журнал решений не принял строку" in engine.halted


def test_the_run_loop_survives_a_broken_journal_and_stops_after_it() -> None:
    """Отказ журнала не убивает задачу: цикл останавливается штатно."""
    def broken(entry: JournalEntry) -> None:
        raise RuntimeError("окно закрылось")

    engine = Engine(ScriptedStrategy([decision(Intent.LONG)] * 3), WORKING, journal=broken)
    source = ListSource([candle(10, 5 * step) for step in range(1, 4)])
    asyncio.run(engine.run(source, RecordingExecutor()))
    assert engine.halted


def test_a_lost_confirmation_does_not_turn_into_a_double_entry() -> None:
    """Заявка принята, сделка не пришла — второй заявки на вход нет.

    Исполнитель здесь принимает заявки и молчит о сделках: так выглядит
    потерянное подтверждение — брокер заявку взял, известие не дошло.
    Позиция появляется только по сделке, поэтому без памяти о поданной заявке
    снимок на следующей свече снова пуст и движок входит второй раз.
    На счёте это двойной объём при одном сигнале.
    """
    script = [decision(Intent.LONG)] * 4
    engine = Engine(ScriptedStrategy(script), WORKING)
    executor = RecordingExecutor()
    source = ListSource([candle(10, 5 * step) for step in range(1, 5)])
    asyncio.run(engine.run(source, executor))

    assert [order.action for order in executor.submitted] == [OrderAction.OPEN], (
        "движок подал вход больше одного раза, не дождавшись сделки"
    )
    assert engine.position is None, "позиция появилась без сделки"
    assert engine.state.pending is not None
    assert engine.halted == "", "молчание исполнителя — не отказ, робот работает"


def test_an_executor_that_cannot_report_deals_stops_the_robot_as_well() -> None:
    """Не узнать о сделках так же опасно, как не подать заявку.

    Разбор пошёл бы по устаревшему состоянию: позиция уже закрыта у брокера,
    а движок об этом не знает — и подаёт вторую заявку по тому же поводу.
    """
    class Silent:
        async def submit(self, order) -> None:
            raise AssertionError("до подачи заявки дойти не должно")

        async def fills_at(self, market_candle):
            raise ConnectionError("связь с брокером потеряна")

    lines: list[JournalEntry] = []
    strategy = ScriptedStrategy([decision(Intent.LONG)])
    engine = Engine(strategy, WORKING, journal=lines.append)
    outcome = asyncio.run(engine.on_market_candle(candle(10, 5), Silent()))

    assert outcome.orders == ()
    assert engine.halted
    assert strategy.seen == [], "свеча ушла в модуль по устаревшему состоянию"
    assert lines[-1].level is JournalLevel.ERROR
    assert lines[-1].event == "Сделки исполнителя не получены"


def test_a_halted_engine_refuses_the_synchronous_entry_too() -> None:
    """Остановка действует и на синхронное ядро, а не только на адаптер."""
    def broken(entry: JournalEntry) -> None:
        raise OSError("журнал недоступен")

    strategy = ScriptedStrategy([decision(Intent.LONG)])
    engine = Engine(strategy, WORKING, journal=broken)
    engine.on_candle(candle(10, 5))
    assert engine.halted

    outcome = engine.on_candle(candle(10, 10))
    assert outcome.orders == ()
    assert outcome.steps == ()
    assert len(strategy.seen) == 1


# --------------------------------------------------------------------------
# Повтор свечи ловит движок, а не торговый модуль
# --------------------------------------------------------------------------

def test_the_engine_itself_drops_a_repeated_candle() -> None:
    """Свеча за то же время, поданная второй раз, дальше приёма не идёт.

    Подставной модуль здесь **своей** защиты от повтора не имеет — как
    и любой второй торговый модуль, который её не скопирует. Повтор после
    переподключения событие штатное, и без защиты в движке он даёт вторую
    заявку на вход: тот же сигнал, двойной объём.
    """
    strategy = ScriptedStrategy([decision(Intent.LONG)])
    engine = Engine(strategy, WORKING)
    executor = RecordingExecutor()

    asyncio.run(engine.on_market_candle(candle(10, 5), executor))
    again = asyncio.run(engine.on_market_candle(candle(10, 5), executor))

    assert len(strategy.seen) == 1, "повторная свеча вошла в ряд модуля дважды"
    assert [order.action for order in executor.submitted] == [OrderAction.OPEN]
    assert again.steps == ()
    assert "повторно" in again.skipped


def test_a_repeated_candle_is_a_calm_line_not_an_alarm() -> None:
    """Повтор — штатное событие потока, и строка про него спокойная.

    Тревога на штатном событии со временем перестаёт читаться, а отброшенная
    неполная свеча — рядом и другого уровня — читаться обязана.
    """
    lines: list[JournalEntry] = []
    engine = Engine(ScriptedStrategy([decision(Intent.LONG)]), WORKING, journal=lines.append)
    engine.on_candle(candle(10, 5))
    engine.on_candle(candle(10, 5))
    assert lines[-1].level is JournalLevel.INFO
    assert "переподключения" in lines[-1].reason


def test_a_candle_going_backwards_is_not_smoothed_over() -> None:
    """Ход времени назад — порча ряда, и она обязана останавливать прогон.

    Это не повтор: повтор штатен, а ряд, поехавший назад, дальше считать
    нельзя. Останавливает его торговый модуль — он ведёт свой ряд.
    """
    engine = Engine(EmaReverse(), WORKING)
    engine.on_candle(candle(10, 10))
    with pytest.raises(ValueError):
        engine.on_candle(candle(10, 5))


# --------------------------------------------------------------------------
# Сторожевые проверки архитектуры
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def parsed() -> list[tuple[pathlib.Path, ast.AST, str]]:
    result = []
    for path in SOURCES:
        text = path.read_text(encoding="utf-8")
        result.append((path, ast.parse(text, filename=str(path)), text))
    assert result, "в engine/ не разобрано ни одного файла — проверка вакуумна"
    return result


#: Имена, по которым видно, что движок начал различать боевой путь и прогон.
#: Список — денилист, и он не может быть полным: `if hasattr(source, "replay")`
#: и любое иносказание пройдут мимо. Он ловит не всякое нарушение, а самое
#: вероятное — то, которое пишут не задумываясь.
ROLE_WORDS = {
    "is_live", "islive", "live", "live_mode", "is_backtest", "isbacktest",
    "backtest", "backtesting", "is_test", "istest", "test_mode", "simulation",
    "is_simulation", "simulated", "dry_run", "paper", "replay", "tester",
    "historical", "history_mode", "тестер", "боевой", "историч",
}


def _names(tree: ast.AST) -> set[str]:
    """Имена в коде: переменные, поля, аргументы, функции, классы, псевдонимы."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.arg):
            found.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.add(node.name)
        elif isinstance(node, ast.keyword) and node.arg:
            found.add(node.arg)
        elif isinstance(node, ast.alias):
            found.add((node.asname or node.name).split(".")[-1])
    return found


def _role_traces(tree: ast.AST) -> list[str]:
    """Следы того, что движок узнал, с кем разговаривает."""
    return sorted(name for name in _names(tree) if name.lower() in ROLE_WORDS)


def test_the_engine_cannot_tell_a_backtest_from_the_real_thing(parsed) -> None:
    """Ни одного признака, по которому различается боевой путь и прогон.

    Нарушение не падает и оставляет тесты зелёными, а продукт возвращает
    к состоянию «тестер врёт, доказать нечем» (ARCHITECTURE.md §1).
    """
    guilty = [
        f"{path.name}: {_role_traces(tree)}"
        for path, tree, _ in parsed
        if _role_traces(tree)
    ]
    assert not guilty, (
        "движок начал различать боевой режим и прогон по истории:\n  "
        + "\n  ".join(guilty)
    )


def test_the_role_check_catches_every_word_it_lists() -> None:
    """Канарейка: срабатывает **каждое** слово словаря, а не «какое-нибудь».

    Выборочная канарейка — половина защиты: она подтверждает, что механизм
    жив, и ничего не говорит про конкретную запись. Опечатка в одном слове
    из двадцати не проявилась бы никак, а слово в словаре есть ровно потому,
    что понятие опасное. Образец — `tests/test_strategies_contract.py`.
    """
    blind = [
        word for word in ROLE_WORDS
        if not _role_traces(ast.parse(f"{word} = 1"))
        or not _role_traces(ast.parse(f"value = source.{word}"))
    ]
    assert not blind, f"слова словаря не срабатывают: {sorted(blind)}"

    planted = ast.parse(
        "def choose(source):\n"
        "    if source.is_backtest:\n"
        "        return 1\n"
        "    return 2\n"
    )
    assert _role_traces(planted) == ["is_backtest"]

    # И то, ради чего словарь расширен: короткие формы без приставки `is_`.
    assert _role_traces(ast.parse("if self.backtest:\n    pass\n")) == ["backtest"]

    clean = ast.parse(
        "def on_candle(candle):\n"
        "    average = candle.close\n"
        "    return average\n"
    )
    assert _role_traces(clean) == []


#: Обращения к часам: и через точку (`datetime.now()`), и голым именем
#: (`monotonic()` после `from time import monotonic`).
CLOCK_ATTRS = {
    "now", "utcnow", "today", "monotonic", "time", "perf_counter",
    "time_ns", "sleep",
}
CLOCK_BARE = {"monotonic", "perf_counter", "time_ns", "sleep"}
CLOCK_MODULES = {"time"}


def _clock_traces(tree: ast.AST) -> list[str]:
    """Следы обращения к системным часам. Пустой список — часов нет.

    Голое имя `time()` в список не попадает намеренно: `datetime.time(10, 5)`
    — это граница торгового окна, а не часы. Модуль `time` при этом запрещён
    целиком, поэтому `from time import time` мимо не пройдёт.
    """
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Attribute) and target.attr in CLOCK_ATTRS:
                found.append(f"{node.lineno}: {target.attr}()")
            elif isinstance(target, ast.Name) and target.id in CLOCK_BARE:
                found.append(f"{node.lineno}: {target.id}()")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in CLOCK_MODULES:
                    found.append(f"{node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] in CLOCK_MODULES:
                found.append(f"{node.lineno}: from {node.module} import ...")
    return found


def test_the_engine_never_asks_the_system_clock(parsed) -> None:
    """«Сейчас» для движка — это время закрытия поданной свечи.

    Обращение к системным часам делает поведение на истории и в бою разным,
    и разница не воспроизводится: тот же датасет даст другой результат
    завтра.
    """
    guilty = [
        f"{path.name}:{trace}"
        for path, tree, _ in parsed
        for trace in _clock_traces(tree)
    ]
    assert not guilty, (
        "движок посмотрел на системные часы — на истории и в бою он будет "
        "вести себя по-разному:\n  " + "\n  ".join(guilty)
    )


def test_the_clock_check_sees_the_ways_around_it() -> None:
    """Канарейка: и через точку, и голым именем, и через импорт модуля."""
    assert _clock_traces(ast.parse("x = datetime.now()"))
    assert _clock_traces(ast.parse("x = date.today()"))
    assert _clock_traces(ast.parse("from time import monotonic\nx = monotonic()"))
    assert _clock_traces(ast.parse("import time\nx = time.time()"))
    assert _clock_traces(ast.parse("import time as clock\nx = clock.time()"))

    # Граница окна часами не является: иначе тест начнут отключать.
    assert _clock_traces(ast.parse("from datetime import time\nstart = time(10, 5)")) == []
    assert _clock_traces(ast.parse("moment = close_time(candle)")) == []


def _async_outside_ports(pairs) -> list[str]:
    """Корутины за пределами портов-адаптеров."""
    allowed = {"ports.py", "runner.py"}
    return [
        f"{name}: {node.name}"
        for name, tree in pairs
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and name not in allowed
    ]


def test_async_lives_only_on_the_port_adapters(parsed) -> None:
    """Разбор свечи синхронный; `await` — только там, где физически сеть и база.

    Иначе каждая построчная сверка тянет в проверку цикл событий
    (решение 0005, уточнение к пунктам 14–15/18).
    """
    guilty = _async_outside_ports([(path.name, tree) for path, tree, _ in parsed])
    assert not guilty, (
        "асинхронность уехала за пределы портов-адаптеров:\n  " + "\n  ".join(guilty)
    )


def test_the_async_check_is_not_blind() -> None:
    """Канарейка: корутина в разборе свечи была бы замечена."""
    planted = ast.parse("async def process(bar):\n    return bar\n")
    assert _async_outside_ports([("pipeline.py", planted)]) == ["pipeline.py: process"]
    assert _async_outside_ports([("runner.py", planted)]) == []


#: Модули, в которых ввода-вывода нет по построению. Не только `pipeline.py`:
#: свеча, окно, настройки и значения — такая же чистая часть, и файл, который
#: полезет в сеть или в базу, там так же недопустим.
PURE = {"pipeline.py", "contracts.py", "settings.py", "window.py", "bars.py"}
IO_MODULES = {
    "httpx", "websockets", "sqlite3", "asyncio", "socket", "requests",
    "urllib", "pathlib", "os", "logging", "sys", "subprocess", "shutil",
}
IO_CALLS = {"print", "open", "input"}


def _io_traces(tree: ast.AST) -> list[str]:
    """Ввод-вывод: импорт наружу, печать, чтение файла, ввод с клавиатуры."""
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in IO_MODULES:
                    found.append(f"{node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] in IO_MODULES:
                found.append(f"{node.lineno}: from {node.module} import ...")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in IO_CALLS:
                found.append(f"{node.lineno}: {node.func.id}()")
    return found


def test_the_pure_core_has_no_input_or_output_at_all(parsed) -> None:
    """Ни сети, ни базы, ни файлов, ни печати — и это проверено, а не обещано.

    Прежняя проверка смотрела только импорты и только в `pipeline.py`:
    переименование файла превращало её в ноль утверждений, а `print` в разборе
    свечи проходил насквозь. Отсюда список ниже и сверка, что он весь найден.
    """
    checked: set[str] = set()
    guilty: list[str] = []
    for path, tree, _ in parsed:
        if path.name not in PURE:
            continue
        checked.add(path.name)
        guilty += [f"{path.name}:{trace}" for trace in _io_traces(tree)]
    assert checked == PURE, (
        f"проверка выродилась: не разобраны файлы {sorted(PURE - checked)}. "
        "Файл переименовали или удалили, а проверка молча перестала "
        "что-либо утверждать"
    )
    assert not guilty, "ввод-вывод в чистой части движка:\n  " + "\n  ".join(guilty)


def test_the_io_check_is_not_blind() -> None:
    """Канарейка: печать и открытый файл в чистой части были бы замечены."""
    assert _io_traces(ast.parse("print('позиция открыта')"))
    assert _io_traces(ast.parse("data = open('/tmp/x').read()"))
    assert _io_traces(ast.parse("import sqlite3"))
    assert _io_traces(ast.parse("from os import environ"))
    assert _io_traces(ast.parse("value = state.position.volume")) == []


def test_the_engine_imports_only_the_strategy_layer(parsed) -> None:
    """`engine/` → `strategies/` и больше ни на один слой (ARCHITECTURE.md §2)."""
    layers = {"market", "broker", "backtest", "ui", "app"}
    guilty: list[str] = []
    for path, tree, _ in parsed:
        for node in ast.walk(tree):
            names: set[str] = set()
            if isinstance(node, ast.Import):
                names = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = {node.module.split(".")[0]}
            if names & layers:
                guilty.append(f"{path.name}:{node.lineno} → {sorted(names & layers)}")
    assert not guilty, "движок потянул чужой слой:\n  " + "\n  ".join(guilty)


def test_every_engine_module_says_what_it_is_for(parsed) -> None:
    for path, tree, _ in parsed:
        assert ast.get_docstring(tree), f"у {path.name} нет докстроки"


def test_the_fake_candle_matches_the_shape_the_port_expects() -> None:
    """Подставка теста и настоящая свеча слоя данных — одной формы.

    Если бы это было не так, все тесты движка проверяли бы вымысел.
    """
    from market import M5, Candle

    real = Candle(
        time=datetime(2026, 6, 19, 10, 5, tzinfo=MSK),
        open=1.0, high=2.0, low=0.5, close=1.5, volume=10.0, timeframe=M5,
        filled_minutes=5,
    )
    check_market_candle(real)
    assert close_time(real) == real.close_time
    fake = FakeCandle(time=real.time, timeframe=FakeTimeframe(5))
    for field in ("time", "timeframe", "open", "high", "low", "close",
                  "volume", "filled_minutes", "unsettled"):
        assert hasattr(fake, field) and hasattr(real, field), field
