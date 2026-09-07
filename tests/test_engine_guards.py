"""Три предохранителя по деньгам: потолок объёма, дневной лимит, запас ГО.

Каждый тест стережёт одно поведение, и оно названо в докстроке первой строкой.
Здесь нет проверок «функция вернула то, что ей дали»: подача на вход того же,
что ожидается на выходе, — самая частая форма теста, который выглядит проверкой
и не проверяет ничего (замер 04.09.2026, восемь таких за сутки).

Что здесь **не** проверяется: сверка с прототипом. Она живёт в
`tests/test_engine_reference_parity.py`, и там же стоят две мутации на эти же
предохранители — «включённый ломает 127 сделок» и «выключенный неотличим
от отсутствующего».
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, time, timedelta
from typing import cast

import pytest

from engine import (
    FUNDS_MAX_AGE,
    AccountFunds,
    DayMoney,
    Engine,
    EngineSettings,
    EngineState,
    ExitReason,
    Fill,
    JournalEntry,
    JournalLevel,
    Mode,
    OrderAction,
    OrderExecutor,
    OrderRequest,
    PositionState,
    Side,
    Step,
    TradingWindow,
    entry_size,
    process_closed_candle,
)
from strategies import Intent
from tests.engine_helpers import (
    MSK,
    ListSource,
    RecordingExecutor,
    ScriptedStrategy,
    bar,
    candle,
    decision,
    position,
)

WINDOW = TradingWindow(time(10, 5), time(11, 0))
#: Рабочие настройки без тейка: тейк к предохранителям отношения не имеет,
#: а с ним к каждой заявке добавляется вооружение и разбирать становится нечего.
WORKING = EngineSettings(mode=Mode.REVERSE, window=WINDOW, take_profit=False)
INSIDE = (10, 10)
OUTSIDE = (11, 30)
#: Момент закрытия свечи `INSIDE`. Нужен там, где `entry_size` зовут напрямую:
#: у него теперь есть срок годности снимка, и «когда» — такой же довод,
#: как «сколько».
AT_INSIDE = datetime(2026, 6, 19, *INSIDE, tzinfo=MSK)


def run(
    state: EngineState | None = None,
    *,
    settings: EngineSettings = WORKING,
    intent: Intent = Intent.LONG,
    when: tuple[int, int] = INSIDE,
    close: float = 100.0,
    day: int = 19,
):
    """Разбор одной свечи. Цена закрытия задаётся: от неё считается бумажный убыток."""
    return process_closed_candle(
        EngineState() if state is None else state,
        bar(*when, close=close, day=day),
        decision(intent, close=close),
        settings,
    )


def funds(
    *,
    equity: float = 100_000.0,
    free: float = 100_000.0,
    margin: float | None = 10_000.0,
    hour: int = 10,
    minute: int = 9,
    day: int = 19,
) -> AccountFunds:
    """Снимок счёта, положенный внутрь движка снаружи.

    ⚠️ Умолчание — **за минуту до разбираемой свечи** (`INSIDE` = 10:10),
    а не «на утро». Снимок счёта имеет срок годности (`FUNDS_MAX_AGE`,
    решение 0040), и утренний снимок к десяти утра уже протух: тест про
    арифметику обеспечения на нём проверял бы отказ по устаревшим данным,
    а не то, что написано в его имени. Утро называется явно там, где оно
    и есть предмет проверки, — в тестах про базу дневного лимита.
    """
    return AccountFunds(
        at=datetime(2026, 6, day, hour, minute, tzinfo=MSK),
        equity=equity, free=free, margin_per_contract=margin,
    )


def with_money(
    state: EngineState, snapshot: AccountFunds, realized: float = 0.0
) -> EngineState:
    """Состояние, в котором деньги дня уже известны движку."""
    return replace(
        state,
        funds=snapshot,
        day=DayMoney(
            date=snapshot.at.date(), opening=snapshot, realized=realized
        ),
    )


def reasons(outcome) -> str:
    """Все причины строк журнала одной свечи в одну строку — для поиска слов."""
    return " | ".join(entry.reason for entry in outcome.journal)


# --------------------------------------------------------------------------
# Потолок объёма
# --------------------------------------------------------------------------

def test_the_cap_stops_an_entry_bigger_than_itself() -> None:
    """Заявка на вход объёмом выше потолка не подаётся вовсе."""
    outcome = run(settings=WORKING.replace(volume=10.0, volume_cap=5.0))
    assert outcome.orders == ()
    assert "выше потолка" in reasons(outcome)
    assert "10 контрактов" in reasons(outcome) and "5 контрактов" in reasons(outcome)


def test_the_cap_lets_through_an_entry_equal_to_itself() -> None:
    """Объём **ровно по потолку** проходит: потолок это «не больше», а не «меньше».

    Мутация к предыдущему тесту: сдвинутое на единицу неравенство запретило бы
    торговать владельцу счёта, поставившему объём ровно по своему же потолку,
    и обнаружилось бы это только в бою.
    """
    outcome = run(settings=WORKING.replace(volume=5.0, volume_cap=5.0))
    assert [order.action for order in outcome.orders] == [OrderAction.OPEN]
    assert outcome.orders[0].volume == 5.0


def test_the_cap_never_blocks_the_way_out() -> None:
    """Потолок сторожит вход и только его: выйти из большой позиции можно всегда.

    Позиция в пять контрактов при потолке в один: обратный сигнал обязан
    подать заявку на выход **на весь объём**. Потолок, применённый к выходу,
    запер бы такую позицию в рынке навсегда — предохранитель против опечатки
    сам стал бы ловушкой.
    """
    held = position(Side.LONG, volume=5.0)
    outcome = run(
        EngineState(position=held),
        settings=WORKING.replace(volume=5.0, volume_cap=1.0),
        intent=Intent.SHORT,
    )
    exits = [order for order in outcome.orders if order.action is OrderAction.CLOSE]
    assert len(exits) == 1
    assert exits[0].volume == 5.0


def test_the_cap_is_measured_against_the_setting_not_against_the_money() -> None:
    """Потолок сравнивается с **заданным** объёмом, до урезания под обеспечение.

    Опечатка «50 вместо 5» при потолке 5 и деньгах ровно на три контракта:
    проверь потолок после урезания — заявка на три уложилась бы в потолок
    и ушла бы к брокеру, а настройка осталась бы вдесятеро больше задуманной.
    """
    state = with_money(EngineState(), funds(free=30_000.0, margin=10_000.0))
    outcome = run(state, settings=WORKING.replace(volume=50.0, volume_cap=5.0))
    assert outcome.orders == ()
    assert "выше потолка" in reasons(outcome)


def test_the_cap_off_by_default_lets_any_volume_through() -> None:
    """Умолчание — потолка нет: движок с умолчаниями ведёт себя как прототип."""
    assert EngineSettings().volume_cap is None
    outcome = run(settings=WORKING.replace(volume=1000.0))
    assert [order.volume for order in outcome.orders] == [1000.0]


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan")])
def test_a_cap_that_is_not_a_cap_is_refused_at_the_border(bad: float) -> None:
    """Ноль и отрицательный потолок — не «потолка нет», а робот без права входа."""
    with pytest.raises(ValueError, match="потолок объёма"):
        EngineSettings(volume_cap=bad)


# --------------------------------------------------------------------------
# Запас свободных средств и ГО
# --------------------------------------------------------------------------

def test_a_shortfall_of_margin_cuts_the_volume_instead_of_skipping_the_signal() -> None:
    """Не хватает на полный объём — вход **уменьшенным**, а не пропуск сигнала.

    Решение 0014 меняет черновой умолчание ТЗ §4.4-И («пропустить сигнал»)
    ровно здесь.
    """
    state = with_money(EngineState(), funds(free=35_000.0, margin=10_000.0))
    outcome = run(
        state,
        settings=WORKING.replace(volume=5.0, free_funds_reserve_percent=10.0),
    )
    assert [order.volume for order in outcome.orders] == [3.0]
    assert "Объём уменьшен" in reasons(outcome)


def test_the_reserve_is_taken_out_before_the_division() -> None:
    """Запас вычитается **до** деления на ГО, а не после.

    Свободных 100 000, ГО контракта 40 000, запас 25%. Верно: доступны
    75 000 — это **один** контракт. Вычти запас после деления, и выйдет
    два контракта по 0,75 = полтора: дробного контракта не бывает, а риска
    в полтора раза больше, чем разрешил владелец счёта.

    ⚠️ Числа подобраны так, чтобы два порядка **расходились**. На круглых
    (100 000 / 10 000 при запасе 30%) оба дают семь, и тест был бы зелёным
    при любом порядке — то есть не проверял бы ничего. Проверено мутацией.
    """
    state = with_money(EngineState(), funds(free=100_000.0, margin=40_000.0))
    outcome = run(
        state,
        settings=WORKING.replace(volume=2.0, free_funds_reserve_percent=25.0),
    )
    assert [order.volume for order in outcome.orders] == [1.0]


def test_not_enough_even_for_one_contract_skips_the_signal_without_stopping() -> None:
    """На один контракт не хватает — сигнал пропущен, робот продолжает работать.

    Заявку нулевого объёма подать нельзя, а останавливаться из-за поднятого
    биржей ГО значило бы звать человека на штатное рыночное событие.
    """
    state = with_money(EngineState(), funds(free=5_000.0, margin=10_000.0))
    outcome = run(
        state,
        settings=WORKING.replace(volume=1.0, free_funds_reserve_percent=10.0),
    )
    assert outcome.orders == ()
    assert outcome.halt == ""
    assert "не хватает" in reasons(outcome)
    assert "ГО контракта" in reasons(outcome)


def test_without_a_snapshot_the_margin_check_does_not_touch_the_volume() -> None:
    """Снимка счёта нет — объём тот, что задан: движок не выдумывает деньги."""
    outcome = run(settings=WORKING.replace(volume=4.0, free_funds_reserve_percent=30.0))
    assert [order.volume for order in outcome.orders] == [4.0]


def test_a_snapshot_without_margin_size_cannot_check_anything() -> None:
    """ГО не сообщено — проверять нечем, и это говорится словами, а не молчанием."""
    state = with_money(EngineState(), funds(margin=None))
    outcome = run(state, settings=WORKING.replace(free_funds_reserve_percent=30.0))
    assert [order.volume for order in outcome.orders] == [1.0]
    assert "НЕ ПРОВЕРЕНО" in reasons(outcome)
    assert "ГО контракта" in reasons(outcome)


def test_a_cleared_checkbox_does_not_trim_the_volume_at_all() -> None:
    """Запас 0 % — проверка обеспечения **выключена**, а не «запас нулевой».

    Ноль означает снятую галочку: так подписано поле в окне и так обещано
    в `app/convert._Guard.off` и `app/port.NO_GUARDS` — «пока не включили,
    робот ведёт себя в точности как прежде». До правки 05.09.2026 объём
    всё равно резался до `floor(свободные / ГО)`, и владелец счёта со всеми
    снятыми галочками получил бы в бою 3 контракта вместо заданных 5.

    ⚠️ Числа те же, что в тесте про урезание выше: там с включённой галочкой
    выходит 3, здесь со снятой обязано выйти 5. Одна пара чисел, два исхода —
    иначе тест был бы зелёным при любой реализации.
    """
    state = with_money(EngineState(), funds(free=35_000.0, margin=10_000.0))
    outcome = run(state, settings=WORKING.replace(volume=5.0))
    assert [order.volume for order in outcome.orders] == [5.0]
    assert "Объём уменьшен" not in reasons(outcome)


def test_negative_free_funds_block_the_entry() -> None:
    """Свободные средства ушли в минус — входа нет.

    `math.floor` от отрицательного даёт отрицательное, и без явного нуля
    робот подал бы заявку на минус три контракта — `OrderRequest` её бы
    отверг уже изнутри разбора, то есть в месте, к ошибке отношения
    не имеющем.
    """
    state = with_money(EngineState(), funds(free=-30_000.0, margin=10_000.0))
    sizing = entry_size(
        state,
        WORKING.replace(volume=1.0, free_funds_reserve_percent=10.0),
        AT_INSIDE,
    )
    assert sizing.volume == 0.0
    assert sizing.affordable == 0.0


# --------------------------------------------------------------------------
# Дневной лимит убытка
# --------------------------------------------------------------------------

def test_the_paper_loss_counts_together_with_the_realized_one() -> None:
    """Бумажный убыток открытой позиции считается вместе с зафиксированным.

    Зафиксировано −600 ₽, лимит 1% от 100 000 = 1 000 ₽ — сам по себе не лимит.
    Открытый лонг от 100 при закрытии 95 даёт ещё −500 ₽, и вместе это 1 100 ₽.
    Считай только зафиксированное — робот продолжил бы торговать ровно тогда,
    когда убыток самый большой (решение 0014).
    """
    held = position(Side.LONG, price=100.0, volume=1.0)
    state = with_money(
        EngineState(position=held), funds(equity=100_000.0), realized=-600.0
    )
    settings = WORKING.replace(
        daily_loss_limit_percent=1.0, ruble_per_point=100.0, volume=1.0
    )
    assert run(state, settings=settings, intent=Intent.LONG, close=95.0).halt
    # Та же свеча без открытой позиции лимита не достигает: −600 против −1 000.
    flat = with_money(EngineState(), funds(equity=100_000.0), realized=-600.0)
    assert run(flat, settings=settings, close=95.0).halt == ""


def test_the_limit_closes_the_position_and_names_the_reason() -> None:
    """Достигнутый лимит закрывает позицию заявкой с причиной «дневной лимит»."""
    held = position(Side.LONG, price=100.0, volume=1.0)
    state = with_money(EngineState(position=held), funds(equity=10_000.0))
    outcome = run(
        state,
        settings=WORKING.replace(
            daily_loss_limit_percent=1.0, ruble_per_point=100.0
        ),
        close=95.0,
    )
    exits = [order for order in outcome.orders if order.action is OrderAction.CLOSE]
    assert [order.exit_reason for order in exits] == [ExitReason.DAILY_LOSS]
    assert ExitReason.DAILY_LOSS.label == "дневной лимит убытка"


def test_the_limit_line_names_every_number_it_used() -> None:
    """Строка журнала называет счёт, порог, зафиксированное и бумажное.

    «Достигнут дневной лимит» без цифр — это код ошибки русскими словами:
    владелец счёта не может ни пересчитать порог, ни понять, из чего он сложился.
    """
    held = position(Side.LONG, price=100.0, volume=1.0)
    state = with_money(
        EngineState(position=held), funds(equity=10_000.0), realized=-40.0
    )
    outcome = run(
        state,
        settings=WORKING.replace(
            daily_loss_limit_percent=1.0, ruble_per_point=100.0
        ),
        close=99.0,
    )
    said = reasons(outcome)
    for number in ("10 000 ₽", "100 ₽", "-40 ₽", "-100 ₽"):
        assert number in said, f"в строке журнала нет числа {number}: {said}"
    assert "вручную" in said
    levels = [entry.level for entry in outcome.journal]
    assert JournalLevel.ERROR in levels


def test_the_limit_works_outside_the_trading_window() -> None:
    """Лимит стоит **до** шага 6 и потому срабатывает вне окна.

    Вне окна позиция законно живёт при выключенном «закрывать в конце окна»,
    а шаг 6 выходит из обработки в любом случае. Проверка, поставленная
    после него, там не сработала бы никогда — и убыток рос бы до открытия
    следующего окна.
    """
    held = position(Side.LONG, price=100.0, volume=1.0)
    state = with_money(EngineState(position=held), funds(equity=10_000.0))
    outcome = run(
        state,
        settings=WORKING.replace(
            daily_loss_limit_percent=1.0, ruble_per_point=100.0,
            close_on_time_end=False,
        ),
        when=OUTSIDE, close=95.0,
    )
    assert outcome.halt
    assert outcome.last_step is Step.REARM_TAKE_PROFIT, (
        "разбор обязан закончиться на шаге 5: лимит стоит между шагами 5 и 6"
    )


def test_the_off_mode_and_the_warmup_are_stronger_than_the_limit() -> None:
    """Шаги 1 и 2 сильнее предохранителя: выключенный робот позицией не управляет.

    Иначе «Выключен» перестал бы означать «робот ею больше не управляет»
    и сам приводил бы к сделке — прямо против `DOMAIN.md` §3.
    """
    held = position(Side.LONG, price=100.0, volume=1.0)
    state = with_money(EngineState(position=held), funds(equity=10_000.0))
    breach = WORKING.replace(daily_loss_limit_percent=1.0, ruble_per_point=100.0)
    off = run(state, settings=breach.replace(mode=Mode.OFF), close=95.0)
    assert off.halt == "" and off.orders == ()
    warming = process_closed_candle(
        state, bar(*INSIDE, close=95.0),
        decision(Intent.LONG, close=95.0, skip_bar=True, warmed_up=False),
        breach,
    )
    assert warming.halt == "" and warming.orders == ()


def test_the_limit_stays_quiet_when_it_is_not_set() -> None:
    """Лимит не задан — робот не останавливается ни при каком убытке."""
    held = position(Side.LONG, price=100.0, volume=1.0)
    state = with_money(
        EngineState(position=held), funds(equity=10_000.0), realized=-9_000.0
    )
    outcome = run(state, settings=WORKING.replace(ruble_per_point=100.0), close=1.0)
    assert outcome.halt == ""


def test_yesterdays_equity_is_not_used_for_todays_limit() -> None:
    """Снимок другой даты для сегодняшнего лимита не годится.

    Порог считался бы от денег, которых на счёте уже нет. Пока сегодняшнего
    снимка нет, лимит честно считается непроверенным.
    """
    settings = WORKING.replace(
        daily_loss_limit_percent=1.0, ruble_per_point=100.0
    )
    held = position(Side.LONG, price=100.0, volume=1.0)
    state = with_money(
        EngineState(position=held), funds(equity=10_000.0, day=18), realized=-5_000.0
    )
    holding = run(state, settings=settings, close=95.0, day=19)
    assert holding.halt == "", (
        "вчерашний счёт и вчерашний убыток не имеют отношения к сегодняшнему "
        "лимиту: робот встал бы утром, ничего сегодня не потеряв"
    )
    assert "НЕ ПРОВЕРЕНО" in reasons(holding), (
        "молчание здесь — тот самый дефект: позиция открыта, лимит задан, "
        "а считать его не от чего"
    )
    # ⚠️ А вот **входа** в том же положении не будет вовсе (решение 0037):
    # вчерашний снимок это «источник счёта был и замолчал», и предохранитель,
    # который в момент неизвестности пускает, предохранителем не является.
    # До 0037 здесь стояло «вход помечен НЕ ПРОВЕРЕНО», то есть робот входил.
    flat = replace(state, position=None)
    entering = run(flat, settings=settings, day=19)
    assert entering.orders == (), (
        "вчерашний снимок счёта — не основание для сегодняшнего входа"
    )
    assert "состояние счёта устарело" in " ".join(
        entry.event for entry in entering.journal
    )


def test_the_limit_is_reached_exactly_at_the_threshold() -> None:
    """Убыток **ровно** в лимит — это достигнутый лимит, а не «ещё чуть-чуть можно».

    Владелец счёта назвал цифру, до которой согласен потерять, а не после
    которой. Мутация на единицу в другую сторону: 99 ₽ из 100 не останавливают.
    """
    settings = WORKING.replace(
        daily_loss_limit_percent=1.0, ruble_per_point=1.0
    )
    at_the_line = with_money(EngineState(), funds(equity=10_000.0), realized=-100.0)
    just_under = with_money(EngineState(), funds(equity=10_000.0), realized=-99.0)
    assert run(at_the_line, settings=settings).halt
    assert run(just_under, settings=settings).halt == ""


# --------------------------------------------------------------------------
# Деньги дня: откуда берётся зафиксированное
# --------------------------------------------------------------------------

def opened_and_closed(engine: Engine, *, entry: float, exit_price: float) -> None:
    """Провести через движок вход и выход, обе сделки с комиссией 14 ₽."""
    entry_order = OrderRequest(
        action=OrderAction.OPEN, side=Side.LONG, volume=1.0,
        submitted_at=bar(*INSIDE).closes_at, reason="вход по сценарию",
        order_id="open:тест",
    )
    engine._state = replace(engine.state, pending=(entry_order,))  # noqa: SLF001 — сценарий подставляет заявку в полёте
    engine.on_fill(Fill(
        action=OrderAction.OPEN, side=Side.LONG, volume=1.0, price=entry,
        at=bar(10, 15).closes_at, order_id="open:тест", commission=14.0,
    ))
    exit_order = OrderRequest(
        action=OrderAction.CLOSE, side=Side.LONG, volume=1.0,
        submitted_at=bar(10, 20).closes_at, reason="выход по сценарию",
        order_id="close:тест", exit_reason=ExitReason.SIGNAL,
    )
    engine._state = replace(engine.state, pending=(exit_order,))  # noqa: SLF001 — то же и для выхода
    engine.on_fill(Fill(
        action=OrderAction.CLOSE, side=Side.LONG, volume=1.0, price=exit_price,
        at=bar(10, 25).closes_at, order_id="close:тест", commission=14.0,
    ))


def test_the_days_result_counts_the_commission_of_both_sides() -> None:
    """Зафиксированное за день — это результат **минус комиссия обеих сторон**.

    Пять пунктов по 100 ₽ это +500 ₽ валовой прибыли, а на счёте останется
    472 ₽. Валовая прибыль в этом проекте результатом не считается
    (`DOMAIN.md` §5), и дневной лимит убытка тем более обязан считать честно.
    """
    engine = Engine(
        ScriptedStrategy([]), WORKING.replace(ruble_per_point=100.0)
    )
    opened_and_closed(engine, entry=100.0, exit_price=105.0)
    assert engine.state.day is not None
    assert engine.state.day.realized == pytest.approx(500.0 - 28.0)


def test_a_fill_on_a_new_date_starts_the_day_from_scratch() -> None:
    """Сделка новой даты обнуляет и результат дня, и счёт на утро.

    Вчерашний размер счёта для сегодняшнего лимита не годится, а вчерашний
    убыток тем более: иначе робот встал бы утром, ничего сегодня не потеряв.
    """
    engine = Engine(ScriptedStrategy([]), WORKING.replace(ruble_per_point=100.0))
    engine.on_funds(funds(equity=10_000.0, day=19))
    yesterday = engine.state.day
    assert yesterday is not None
    engine._state = replace(  # noqa: SLF001 — сценарий доводит вчерашний убыток до нужного
        engine.state, day=replace(yesterday, realized=-5_000.0)
    )
    order = OrderRequest(
        action=OrderAction.OPEN, side=Side.LONG, volume=1.0,
        submitted_at=bar(*INSIDE, day=20).closes_at, reason="вход",
        order_id="open:завтра",
    )
    engine._state = replace(engine.state, pending=(order,))  # noqa: SLF001 — заявка в полёте
    engine.on_fill(Fill(
        action=OrderAction.OPEN, side=Side.LONG, volume=1.0, price=100.0,
        at=bar(10, 15, day=20).closes_at, order_id="open:завтра", commission=14.0,
    ))
    day = engine.state.day
    assert day is not None
    assert day.date.day == 20
    assert day.realized == pytest.approx(-14.0)
    assert day.opening is None, "счёт на утро обязан спрашиваться заново"


# --------------------------------------------------------------------------
# Как деньги попадают в движок
# --------------------------------------------------------------------------

def test_the_first_snapshot_of_the_date_becomes_the_morning_equity() -> None:
    """Первый снимок даты становится счётом на утро, следующие его не двигают.

    Иначе просадка сама уменьшала бы порог, и остановка наступала бы всё
    раньше (`DOMAIN.md` §4).
    """
    engine = Engine(ScriptedStrategy([]), WORKING)
    engine.on_funds(funds(equity=100_000.0, hour=9, minute=55))
    engine.on_funds(funds(equity=40_000.0, hour=10, minute=30))
    day = engine.state.day
    assert day is not None and day.opening is not None
    assert day.opening.equity == 100_000.0
    assert engine.state.funds is not None
    assert engine.state.funds.equity == 40_000.0, (
        "последний снимок обязан обновляться: по нему считаются свободные средства"
    )


def test_only_the_first_snapshot_of_the_date_writes_a_line() -> None:
    """Строка в журнал — на появление базы дня, а не на каждый опрос портфеля."""
    written: list[JournalEntry] = []
    engine = Engine(
        ScriptedStrategy([]),
        WORKING.replace(daily_loss_limit_percent=2.0),
        journal=written.append,
    )
    first = engine.on_funds(funds(equity=100_000.0, hour=9, minute=55))
    again = engine.on_funds(funds(equity=99_000.0, hour=10, minute=0))
    tomorrow = engine.on_funds(funds(equity=99_000.0, day=20))
    assert len(first) == 1 and again == () and len(tomorrow) == 1
    assert len(written) == 2
    assert "2 000 ₽" in first[0].reason, (
        "строка обязана назвать порог в рублях: процент от счёта человек "
        "в уме не считает"
    )
    assert "09:55" in first[0].reason, (
        "строка обязана назвать время снимка: «на утро» — обещание, которое "
        "движок сдержать не может"
    )


def test_a_snapshot_without_a_limit_says_so_out_loud() -> None:
    """Лимит не задан — снимок принят, и об этом сказано прямо."""
    engine = Engine(ScriptedStrategy([]), WORKING)
    entries = engine.on_funds(funds())
    assert "не задан" in entries[0].reason


def test_reset_forgets_the_money_of_the_day() -> None:
    """`reset()` уносит и снимок, и счёт на утро, и результат дня.

    Иначе после ручного перезапуска лимит считался бы от вчерашних денег
    и от чужого убытка.
    """
    engine = Engine(ScriptedStrategy([]), WORKING)
    engine.on_funds(funds())
    engine.reset()
    assert engine.state.funds is None
    assert engine.state.day is None


def test_the_engine_has_no_way_to_ask_for_money() -> None:
    """Деньги **кладут внутрь**, движок за ними не ходит.

    Проверяется устройство, а не намерение: у `Engine.on_funds` один довод —
    значение, и никакого поставщика он не принимает и не хранит. Появись
    у движка ссылка на того, кто отдаёт портфель, — он начал бы различать
    боевой путь и прогон по истории (`ARCHITECTURE.md` §1).
    """
    import inspect

    from engine import guards, runner

    names = list(inspect.signature(Engine.on_funds).parameters)
    assert names == ["self", "funds"]
    assert "funds" not in Engine.__slots__, (
        "снимок обязан лежать в состоянии движка, а не отдельным полем "
        "рядом с ним: иначе он переживёт reset() и разъедется с деньгами дня"
    )
    for module in (guards, runner):
        source = inspect.getsource(module)
        for forbidden in ("broker", "market", "backtest", "app."):
            assert f"import {forbidden}" not in source, (
                f"{module.__name__} импортирует {forbidden} — движок начал "
                "знать, откуда приходят деньги"
            )


# --------------------------------------------------------------------------
# После срабатывания
# --------------------------------------------------------------------------

def test_after_the_limit_the_robot_does_not_resume_by_itself() -> None:
    """Возобновление после дневного лимита — только вручную (решение 0014).

    Прогон продолжает подавать свечи; робот обязан молчать на всех до самого
    конца и снова заработать только после `reset()`.
    """
    written: list[JournalEntry] = []
    settings = WORKING.replace(
        daily_loss_limit_percent=1.0, ruble_per_point=100.0, volume=1.0
    )
    engine = Engine(
        ScriptedStrategy([decision(Intent.LONG, close=95.0) for _ in range(4)]),
        settings,
        state=with_money(
            EngineState(position=position(Side.LONG, price=100.0)),
            funds(equity=10_000.0),
        ),
        journal=written.append,
    )
    executor = RecordingExecutor()

    import asyncio

    async def scenario() -> None:
        moment = datetime(2026, 6, 19, 10, 5, tzinfo=MSK)
        for step in range(4):
            await engine.on_market_candle(
                candle(10, 5 + step * 5, close=95.0),
                cast(OrderExecutor, executor),
            )
        assert moment is not None

    asyncio.run(scenario())
    assert engine.halted, "робот обязан остановиться на достигнутом лимите"
    assert "Дневной лимит убытка" in engine.halted
    submitted = [order.action for order in executor.submitted]
    assert submitted == [OrderAction.CLOSE], (
        "после остановки не должно уйти ни одной заявки, кроме выхода: "
        f"{submitted}"
    )
    engine.reset()
    assert engine.halted == "", "снимает остановку только человек — `reset()`"


def test_the_line_warns_that_the_closing_fill_will_not_be_seen() -> None:
    """Журнал говорит вслух: сделку по этой заявке остановленный робот не увидит.

    Остановленный движок исполнителя больше не спрашивает. Промолчать здесь —
    значит дать владельцу счёта прочитать «выход подан» и решить, что всё
    закончилось само.
    """
    held = position(Side.LONG, price=100.0, volume=1.0)
    state = with_money(EngineState(position=held), funds(equity=10_000.0))
    outcome = run(
        state,
        settings=WORKING.replace(
            daily_loss_limit_percent=1.0, ruble_per_point=100.0
        ),
        close=95.0,
    )
    assert "уже не увидит" in reasons(outcome)
    assert "приложении брокера" in reasons(outcome)


def test_a_position_already_closing_is_not_asked_to_close_twice() -> None:
    """Позиция в состоянии «закрывается» второй заявки на выход не получает.

    Вторая рыночная заявка после исполнения первой открывает позицию
    в обратную сторону — на полный объём и без единой строки о том, откуда
    она взялась.
    """
    held = replace(
        position(Side.LONG, price=100.0, volume=1.0), state=PositionState.CLOSING
    )
    state = with_money(EngineState(position=held), funds(equity=10_000.0))
    outcome = run(
        state,
        settings=WORKING.replace(
            daily_loss_limit_percent=1.0, ruble_per_point=100.0
        ),
        close=95.0,
    )
    assert outcome.halt
    assert [order.action for order in outcome.orders] == []


# --------------------------------------------------------------------------
# Умолчания
# --------------------------------------------------------------------------

def test_at_the_defaults_not_a_single_guard_fires() -> None:
    """Умолчания — ни одна из трёх проверок не срабатывает.

    На этом стоит сверка с прототипом: у него предохранителей нет вовсе.
    Здесь проверяется отдельно от неё, потому что сверка — это данные,
    а это утверждение про настройки.
    """
    plain = EngineSettings()
    assert plain.volume_cap is None
    assert plain.daily_loss_limit_percent == 0.0
    assert plain.free_funds_reserve_percent == 0.0
    sizing = entry_size(EngineState(), plain, AT_INSIDE)
    assert sizing.volume == plain.volume and not sizing.blocked and not sizing.cut


@pytest.mark.parametrize(
    "name", ["daily_loss_limit_percent", "free_funds_reserve_percent"]
)
@pytest.mark.parametrize("bad", [-1.0, 100.0, 250.0])
def test_a_percent_that_is_not_a_guard_is_refused_at_the_border(
    name: str, bad: float
) -> None:
    """Сто процентов и больше предохранителем не является, отрицательный тоже."""
    with pytest.raises(ValueError):
        EngineSettings(**{name: bad})  # type: ignore[arg-type]


def test_the_source_never_reaches_for_the_extremes_of_the_bar() -> None:
    """Предохранители считают по цене **закрытия**, `high` и `low` не читают.

    Внутрибарного срабатывания у лимита нет: решение принимается на закрытии
    свечи, как и все прочие. Запрет на весь слой стережёт отдельный разбор
    исходников (`tests/test_engine_take_profit.py`); здесь — про новый модуль.
    """
    import inspect

    from engine import guards

    source = inspect.getsource(guards)
    assert ".high" not in source and ".low" not in source


def test_the_state_carries_the_money_through_a_candle_untouched() -> None:
    """Разбор свечи денег дня не трогает: он их только читает.

    `_done` пересобирает состояние из «до подачи заявок» плюс сами заявки —
    значит любая правка денег внутри разбора потерялась бы молча, и лимит
    считал бы по устаревшему числу.
    """
    state = with_money(EngineState(), funds(equity=100_000.0), realized=-123.0)
    outcome = run(state)
    assert outcome.state.day == state.day
    assert outcome.state.funds == state.funds


def test_a_source_of_candles_is_not_a_source_of_money() -> None:
    """Свечи и деньги приходят разными дорогами и не путаются.

    Прогон, которому денег не сообщили, работает как раньше: это тот самый
    случай, в котором сверка с прототипом и живёт.
    """
    import asyncio

    engine = Engine(
        ScriptedStrategy([decision(Intent.LONG) for _ in range(3)]), WORKING
    )
    executor = RecordingExecutor()

    async def scenario() -> None:
        source = ListSource([candle(10, 5 + step * 5) for step in range(3)])
        async for item in source.candles():
            await engine.on_market_candle(item, cast(OrderExecutor, executor))

    asyncio.run(scenario())
    assert engine.state.funds is None
    assert engine.state.day is None
    assert [order.action for order in executor.submitted] == [OrderAction.OPEN]


# --------------------------------------------------------------------------
# Срок годности снимка счёта (решения 0037 и 0040)
# --------------------------------------------------------------------------

def test_a_stale_snapshot_closes_the_entry_and_names_both_times() -> None:
    """Снимок счёта протух — вход не открывается, и сказано словами.

    Решение 0037: предохранитель, который в момент неизвестности пускает,
    предохранителем не является. Проверяется **пара** вещей, и обе нужны:
    заявки нет, и в журнале стоят оба времени — по ним владелец счёта
    отличает молчащий портфель от убежавших часов машины.
    """
    settings = WORKING.replace(volume=1.0, free_funds_reserve_percent=30.0)
    stale = with_money(EngineState(), funds(hour=10, minute=0))
    outcome = run(stale, settings=settings)
    assert outcome.orders == (), "вход по снимку счёта десятиминутной давности"
    assert outcome.halt == "", "это отказ во входе, а не остановка робота"
    said = reasons(outcome)
    assert "10:00:00" in said and "10:10:00" in said, said
    assert "10 мин 0 с" in said, said


def test_a_fresh_snapshot_a_minute_before_the_bar_opens_the_entry() -> None:
    """Парная мутация: тот же расклад со свежим снимком вход открывает.

    Без неё предыдущий тест был бы зелёным и у робота, который не входит
    никогда.
    """
    settings = WORKING.replace(volume=1.0, free_funds_reserve_percent=30.0)
    fresh = with_money(EngineState(), funds(hour=10, minute=9))
    outcome = run(fresh, settings=settings)
    assert [order.volume for order in outcome.orders] == [1.0]


def test_a_snapshot_exactly_at_the_age_limit_is_still_good() -> None:
    """Ровно `FUNDS_MAX_AGE` — снимок ещё годен, секундой старше — уже нет.

    Неравенство названо вслух, потому что граница здесь решает, будет сделка
    или нет, а «примерно две минуты» в таком месте означает «как получится».
    """
    settings = WORKING.replace(volume=1.0, free_funds_reserve_percent=30.0)
    at_the_line = AccountFunds(
        at=AT_INSIDE - FUNDS_MAX_AGE, equity=100_000.0, free=100_000.0,
        margin_per_contract=10_000.0,
    )
    past_it = replace(at_the_line, at=at_the_line.at - timedelta(seconds=1))
    good = with_money(EngineState(), at_the_line)
    assert entry_size(good, settings, AT_INSIDE).stale is False
    late = with_money(EngineState(), past_it)
    assert entry_size(late, settings, AT_INSIDE).stale is True


def test_a_snapshot_newer_than_the_bar_is_good() -> None:
    """Снимок «из будущего» годен, и на любой отрыв вперёд.

    В бою так каждую свечу: свеча закрылась в 10:10:00, а опрос портфеля
    идёт по своим часам и пришёл позже. Отрыв бывает и большим — бар,
    догруженный после обрыва, разбирается тогда, когда снимок уже намного
    новее его.

    ⚠️ Разрыв взят заведомо больше срока годности (десять минут против
    двух). На двух минутах тест был бы зелёным и у проверки по модулю
    разности, то есть у той, которая запрещает вход по **свежему** снимку, —
    проверено мутацией.
    """
    settings = WORKING.replace(volume=1.0, free_funds_reserve_percent=30.0)
    ahead = with_money(EngineState(), funds(hour=10, minute=20))
    assert entry_size(ahead, settings, AT_INSIDE).stale is False


def test_a_snapshot_that_never_came_does_not_block_anything() -> None:
    """Снимка не было ни разу — вход открыт: это прогон по истории и наблюдение.

    ⚠️ Ровно та граница, ради которой различаются «не было» и «протух».
    Запрети вход и здесь — встанут прогон по истории, сверка с прототипом
    и наблюдение на живом потоке, а движок начнёт различать, откуда пришла
    свеча (`ARCHITECTURE.md` §1).
    """
    settings = WORKING.replace(volume=4.0, free_funds_reserve_percent=30.0)
    outcome = run(settings=settings)
    assert [order.volume for order in outcome.orders] == [4.0]
    assert "НЕ ПРОВЕРЕНО" in reasons(outcome)


def test_an_unknown_margin_size_does_not_block_the_entry() -> None:
    """ГО под контракт не сообщено — вход **открыт**, иначе робот встал бы навсегда.

    По HTTP брокер отдаёт ГО только делением `lockedForFutures` открытой
    позиции на количество (`broker/account.py`). Значит перед первым входом
    за день ГО неизвестно **всегда**, и запрет на этом месте — вечная
    блокировка: число, которого робот ждёт, берётся из позиции, которую он
    из-за этого числа не открывает.

    Решение 0037 говорит про **размер счёта**, а не про любую слепоту.
    """
    settings = WORKING.replace(volume=2.0, free_funds_reserve_percent=30.0)
    state = with_money(EngineState(), funds(margin=None))
    outcome = run(state, settings=settings)
    assert [order.volume for order in outcome.orders] == [2.0]
    assert "НЕ ПРОВЕРЕНО" in reasons(outcome)


def test_with_both_money_guards_off_a_stale_snapshot_blocks_nothing() -> None:
    """Обе галочки сняты — устаревший снимок входу не мешает.

    Владелец счёта прямо сказал, что торгует без проверки денег. Запретить
    ему вход из-за молчащего портфеля значило бы включить предохранитель,
    который он выключил.
    """
    stale = with_money(EngineState(), funds(hour=9, minute=0))
    outcome = run(stale, settings=WORKING.replace(volume=3.0))
    assert [order.volume for order in outcome.orders] == [3.0]


def test_a_stale_snapshot_is_never_used_to_trim_the_volume() -> None:
    """По протухшим числам объём не считается — даже когда вход разрешён.

    Галочка запаса снята, значит вход не запрещён; но и урезать объём
    по свободным средствам трёхчасовой давности нельзя. Проверка идёт
    по `affordable`: `None` означает «не считали», ноль означал бы «посчитали
    и не хватило».
    """
    stale = with_money(EngineState(), funds(free=5_000.0, margin=10_000.0,
                                            hour=9, minute=0))
    sizing = entry_size(
        stale, WORKING.replace(volume=3.0, free_funds_reserve_percent=30.0),
        AT_INSIDE,
    )
    assert sizing.stale is True
    assert sizing.affordable is None


def test_the_morning_equity_does_not_expire_inside_its_own_date() -> None:
    """База дневного лимита обязана быть старой: это счёт **на утро**.

    Снимок 09:55 к 11:30 протух для проверки обеспечения и остаётся годным
    для лимита убытка: внутри дня база не пересчитывается (решение 0035),
    иначе просадка сама уменьшала бы порог. Срок годности не имеет права
    подменить это правило.
    """
    settings = WORKING.replace(daily_loss_limit_percent=1.0, ruble_per_point=100.0)
    held = position(Side.LONG, price=100.0, volume=1.0)
    state = with_money(
        EngineState(position=held), funds(equity=10_000.0, hour=9, minute=55)
    )
    outcome = run(state, settings=settings, when=OUTSIDE, close=99.0)
    assert outcome.halt != "", "лимит не сработал: утренняя база сочтена протухшей"
    assert "10 000 ₽" in reasons(outcome)


def test_the_freshness_window_leaves_room_for_four_polls() -> None:
    """Срок годности снимка и темп опроса портфеля живут в разных слоях.

    Движок про `broker/` не знает и знать не может (`ARCHITECTURE.md` §2),
    поэтому одной константы у них быть не должно. Разъехаться молча им
    не даёт этот тест: опрос раз в 30 секунд против двух минут годности —
    четыре пропущенных ответа подряд, прежде чем робот перестанет входить.
    Ужать годность или растянуть опрос так, чтобы запас пропал, — значит
    получить отказ во входе от одной потерянной пачки ответов.
    """
    from broker.account import SNAPSHOT_INTERVAL, SNAPSHOT_MAX_AGE

    assert SNAPSHOT_INTERVAL * 4 <= FUNDS_MAX_AGE, (
        "запас между опросом портфеля и сроком годности снимка меньше "
        "четырёх опросов"
    )
    assert SNAPSHOT_MAX_AGE == FUNDS_MAX_AGE, (
        "брокер и движок считают снимок годным разное время: одно из двух "
        "чисел двигали, а второе забыли"
    )
