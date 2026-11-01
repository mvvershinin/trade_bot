"""День, прожитый до запуска (`DayRecord`, `Engine.on_day`) — решение 0066, Р7.

Перезапуск программы в тот же день не должен снимать «стоп после тейка»:
иначе робот, остановившийся на день по правилу, после перезапуска входит
снова. Значение дня кладут в движок снаружи — как деньги счёта
(`on_funds`), — а правило остаётся в движке, на шаге 9.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time

import pytest

from engine import (
    AccountFunds,
    DayMoney,
    DayRecord,
    Engine,
    EngineSettings,
    EngineState,
    JournalEntry,
    Mode,
    OrderAction,
    Side,
    TradingWindow,
    is_take_order,
    take_order_id,
)
from strategies import Intent
from tests.engine_helpers import (
    MSK,
    RecordingExecutor,
    ScriptedStrategy,
    candle,
    decision,
    position,
)

WINDOW = TradingWindow(time(10, 5), time(11, 0))
WORKING = EngineSettings(mode=Mode.REVERSE, window=WINDOW, take_profit=False)
TODAY = date(2026, 6, 19)
STARTED = datetime(2026, 6, 19, 10, 0, tzinfo=MSK)
MORNING = AccountFunds(
    at=datetime(2026, 6, 19, 9, 0, tzinfo=MSK), equity=1_000_000.0, free=800_000.0
)


def _first_candle(
    settings: EngineSettings, record: DayRecord | None
) -> tuple[RecordingExecutor, list[JournalEntry]]:
    """Свежий движок после «перезапуска», первая свеча в окне с сигналом в лонг."""
    lines: list[JournalEntry] = []
    engine = Engine(
        ScriptedStrategy([decision(Intent.LONG)]), settings, journal=lines.append
    )
    if record is not None:
        engine.on_day(record, at=STARTED)
    executor = RecordingExecutor()
    asyncio.run(engine.on_market_candle(candle(10, 5), executor))
    return executor, lines


def test_after_a_restart_on_a_take_day_no_entry_is_sent() -> None:
    """День, где тейк уже был, — вход после перезапуска не подаётся.

    Стережёт: дата тейка из значения дня доходит до шага 9, и «стоп после
    тейка» срабатывает так же, как без перезапуска, — заявок нет, в журнале
    «Сделок сегодня больше не будет».
    """
    executor, lines = _first_candle(
        WORKING, DayRecord(day=TODAY, take_profit_date=TODAY)
    )
    assert executor.submitted == [], "перезапуск снял «стоп после тейка»"
    assert "Сделок сегодня больше не будет" in [entry.event for entry in lines]


def test_without_the_day_value_the_entry_goes_as_before() -> None:
    """Без значения дня поведение прежнее: сигнал в окне — заявка на вход.

    Стережёт обратную сторону: значение дня не запрещает вход само по себе.
    """
    executor, _ = _first_candle(WORKING, None)
    assert [order.action for order in executor.submitted] == [OrderAction.OPEN]


def test_a_day_without_a_take_does_not_forbid_the_entry() -> None:
    """Значение дня без тейка — вход подаётся: запрещает факт тейка, а не само значение."""
    executor, _ = _first_candle(
        WORKING, DayRecord(day=TODAY, realized=-1_500.0, opening=MORNING)
    )
    assert [order.action for order in executor.submitted] == [OrderAction.OPEN]


def test_one_entry_a_day_remembers_the_exit_across_a_restart() -> None:
    """Режим «один вход в день»: выход до перезапуска закрывает день и после него."""
    executor, _ = _first_candle(
        WORKING.replace(mode=Mode.ONE_ENTRY_A_DAY),
        DayRecord(day=TODAY, last_exit_date=TODAY),
    )
    assert executor.submitted == []


def test_the_day_is_restored_without_replacing_the_state() -> None:
    """Деньги и даты дня ложатся в состояние, позиция и заявки не тронуты.

    Стережёт: `on_day` не подменяет состояние целиком — открытая позиция
    остаётся; результат дня и счёт на утро — из значения; строка в журнале
    называет и результат, и тейк.
    """
    held = position(Side.LONG, price=210_000.0)
    lines: list[JournalEntry] = []
    engine = Engine(
        ScriptedStrategy([]), WORKING, state=EngineState(position=held),
        journal=lines.append,
    )
    entries = engine.on_day(
        DayRecord(day=TODAY, opening=MORNING, realized=-1_500.0, take_profit_date=TODAY),
        at=STARTED,
    )

    assert engine.position == held, "значение дня переписало позицию"
    day = engine.state.day
    assert day is not None and day.date == TODAY
    assert day.realized == -1_500.0
    assert day.opening == MORNING
    assert engine.state.take_profit_date == TODAY
    assert lines == list(entries) and len(entries) == 1
    assert "результат дня -1 500 ₽" in entries[0].reason, entries[0].reason
    assert "«стоп после тейка»" in entries[0].reason


def test_a_take_seen_in_this_session_is_not_undone_by_the_record() -> None:
    """Дата события назад не едет: значение без тейка не стирает тейк этой сессии."""
    engine = Engine(
        ScriptedStrategy([]), WORKING, state=EngineState(take_profit_date=TODAY)
    )
    engine.on_day(DayRecord(day=TODAY), at=STARTED)
    assert engine.state.take_profit_date == TODAY


def test_the_earlier_snapshot_stays_the_morning_one() -> None:
    """Счёт на утро — первый снимок даты: снимок после запуска его не вытесняет."""
    engine = Engine(ScriptedStrategy([]), WORKING)
    later = AccountFunds(
        at=datetime(2026, 6, 19, 10, 0, tzinfo=MSK), equity=990_000.0, free=790_000.0
    )
    engine.on_funds(later)
    engine.on_day(DayRecord(day=TODAY, opening=MORNING), at=STARTED)
    assert engine.state.day is not None
    assert engine.state.day.opening == MORNING


def test_yesterday_does_not_touch_today_money_and_says_so() -> None:
    """Значение старше дня движка денег не трогает — и журнал это говорит."""
    lines: list[JournalEntry] = []
    engine = Engine(ScriptedStrategy([]), WORKING, journal=lines.append)
    engine.on_funds(AccountFunds(
        at=datetime(2026, 6, 20, 9, 0, tzinfo=MSK), equity=1_000_000.0, free=800_000.0
    ))
    engine.on_day(DayRecord(day=TODAY, realized=-1_500.0), at=STARTED)
    assert engine.state.day is not None
    assert engine.state.day.date == date(2026, 6, 20)
    assert engine.state.day.realized == 0.0
    assert "Движок уже живёт в дате 20.06.2026" in lines[-1].reason


def test_a_moment_instead_of_a_date_is_refused_out_loud() -> None:
    """Момент вместо даты отвергается: с датой свечи он не сравнится никогда."""
    with pytest.raises(TypeError, match="календарная дата"):
        DayRecord(day=TODAY, take_profit_date=datetime(2026, 6, 19, 11, 0, tzinfo=MSK))


def test_without_a_clock_the_caller_names_the_moment() -> None:
    """Своих часов у движка нет: до первой свечи момент записи называет вызывающий."""
    engine = Engine(ScriptedStrategy([]), WORKING)
    with pytest.raises(ValueError, match="который час"):
        engine.on_day(DayRecord(day=TODAY))


def test_an_incomplete_day_result_is_not_called_complete() -> None:
    """Сделки без комиссии и чужие исполнения — строка говорит «неполон».

    Стережёт правду строки: при неизвестной комиссии результат выше
    настоящего, и «с комиссией обеих сторон» без оговорки было бы неправдой.
    """
    engine = Engine(ScriptedStrategy([]), WORKING)
    entries = engine.on_day(
        DayRecord(day=TODAY, realized=-1_500.0, unknown_commissions=2, foreign=("x-1",)),
        at=STARTED,
    )
    text = entries[0].reason
    assert "неполон" in text
    assert "у 2 сделок комиссия неизвестна" in text
    assert "1 исполнение без своей команды не учтено" in text


def test_a_complete_day_has_no_reservation() -> None:
    """Обратная сторона: полный день оговорки не получает."""
    engine = Engine(ScriptedStrategy([]), WORKING)
    entries = engine.on_day(DayRecord(day=TODAY, realized=-1_500.0), at=STARTED)
    assert "неполон" not in entries[0].reason


def test_the_take_name_predicate_knows_the_take_and_only_it() -> None:
    """`is_take_order` узнаёт имя вооружения и не узнаёт вход, выход и снятие.

    Стережёт единый формат: имя строит `take_order_id`, узнаёт этот предикат,
    и по нему слой данных восстанавливает дату тейка после перезапуска.
    Снятие (`cancel:take:…`) тейком не считается — сделок по нему не бывает.
    """
    held = position(Side.LONG, price=210_000.0)
    short = position(Side.SHORT, price=210_000.0)
    assert is_take_order(take_order_id(held))
    assert is_take_order(take_order_id(short))
    for name in (
        f"open:long:{STARTED.isoformat()}",
        f"close:long:{STARTED.isoformat()}",
        f"cancel:{take_order_id(held)}:{STARTED.isoformat()}",
        "",
    ):
        assert not is_take_order(name), name


def test_an_older_take_in_the_record_does_not_move_the_session_take_back() -> None:
    """Дата тейка назад не едет и тогда, когда в значении дня она тоже есть.

    Тейк этой сессии — сегодня, значение дня называет вчерашний (последний
    тейк, известный базе на момент сборки). Принять вчерашнюю значило бы
    снять «стоп после тейка» на сегодня. То же с датой выхода — «один вход
    в день». Стережёт: из двух дат остаётся более поздняя.
    """
    yesterday = date(2026, 6, 18)
    engine = Engine(
        ScriptedStrategy([]), WORKING,
        state=EngineState(take_profit_date=TODAY, last_exit_date=TODAY),
    )
    engine.on_day(
        DayRecord(day=TODAY, take_profit_date=yesterday, last_exit_date=yesterday),
        at=STARTED,
    )
    assert engine.state.take_profit_date == TODAY, "дата тейка уехала назад"
    assert engine.state.last_exit_date == TODAY, "дата выхода уехала назад"


def test_the_day_result_from_the_record_replaces_not_adds() -> None:
    """Результат дня из значения заменяет результат движка, а не прибавляется к нему.

    Значение дня полное — в нём уже есть сделки, которые движок видел
    в этой сессии. Сложение посчитало бы их дважды, и дневной результат
    в окне разошёлся бы со счётом. Стережёт: −500 у движка и −1 500
    в значении дают −1 500, а не −2 000.
    """
    engine = Engine(
        ScriptedStrategy([]), WORKING,
        state=EngineState(day=DayMoney(date=TODAY, opening=MORNING, realized=-500.0)),
    )
    engine.on_day(DayRecord(day=TODAY, realized=-1_500.0), at=STARTED)
    assert engine.state.day is not None
    assert engine.state.day.realized == -1_500.0, "результат дня сложен, а не заменён"
    assert engine.state.day.opening == MORNING
