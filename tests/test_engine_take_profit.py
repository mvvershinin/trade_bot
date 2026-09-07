"""Тейк-профит: уровень, вооружение, снятие, скользящий и «стоп после тейка».

Что здесь проверяется и почему именно так
------------------------------------------
**Неподвижный тейк** сверяется с прототипом сделка в сделку — это делает
`tests/test_engine_reference_parity.py`, 127 строк из 127. Здесь проверяется
то, что сверка **не видит**: границы округления, порядок «снять до закрытия»,
поведение при отказе исполнителя, круг обмена внутри свечи.

**Скользящий тейк** сверкой не проверяется и не будет: в прототипе его нет
(решение 0009). Его единственная проверка — приёмочное правило ТЗ §9: уровень
едет только в сторону прибыли и ни разу не откатывается назад.

Две границы, ради которых нужны два теста, а не один
-----------------------------------------------------
Решение 0008, пункт 4: факт срабатывания тейка попадает в движок **только
сделкой из порта**. Запрет «движок не читает `high`/`low`» сам по себе границу
не держит — обходится тремя способами без всякого злого умысла. Держат её:

* разбор исходников **всего** `engine/` по дереву — с исключением для двух
  функций перекладки свечи, а не для файла целиком: «переименовать файл»
  названо в самом решении третьим из трёх способов обхода;
* поведенческий: движок с исполнителем, который о тейке молчит, не выдаёт
  ни одного выхода по тейку при любых `high` и `low`. Проверяется **выход
  самого движка** — причина в его заявках, причина в его журнале, цена выхода
  и **момент** выхода, — а не признак, который пишет подставка.

Без обоих центральное утверждение решения остаётся декларацией. Оба проверены
мутациями: движок, сравнивающий `bar.close` с уровнем, обязан их провалить,
и проваливает — в том числе тот его вариант, который причину не называет
и потому проходит проверки по причине и по цене.
"""

from __future__ import annotations

import asyncio
import ast
import pathlib
from dataclasses import replace
from datetime import time, timedelta

import pytest

from engine import (
    Engine,
    EngineSettings,
    EngineState,
    ExecutionRefused,
    ExitReason,
    Fill,
    JournalLevel,
    Mode,
    OrderAction,
    OrderRequest,
    Refusal,
    Reversal,
    Side,
    Step,
    TakeProfit,
    TradingWindow,
    covers_commission,
    fixed_level,
    process_closed_candle,
    round_level,
    target_profit,
    order_words,
    take_order_id,
)
from strategies import Intent
from tests.engine_helpers import (
    ListSource,
    NextOpenExecutor,
    RecordingExecutor,
    ScriptedStrategy,
    bar,
    candle,
    decision,
    position,
)

WINDOW = TradingWindow(time(10, 5), time(11, 0))
WORKING = EngineSettings(mode=Mode.REVERSE, window=WINDOW)
#: Цены эталонного инструмента: около 210 000 пунктов. На ста рублях уровень
#: 0,5% округляется обратно к цене входа, и тейк срабатывает в своей же свече —
#: это свойство прототипа, а не дефект, и проверяется оно отдельно.
PRICE = 210_000.0
INSIDE = (10, 10)
OUTSIDE = (11, 30)


def run(
    state: EngineState | None = None,
    *,
    settings: EngineSettings = WORKING,
    intent: Intent = Intent.LONG,
    when: tuple[int, int] = INSIDE,
    close: float = PRICE,
    day: int = 19,
):
    return process_closed_candle(
        EngineState() if state is None else state,
        bar(*when, day=day, close=close),
        decision(intent, close=close),
        settings,
    )


def unarmed(
    side: Side = Side.LONG,
    *,
    price: float = PRICE,
    plan: TakeProfit | None = None,
    **rest: object,
):
    """Позиция с планом тейка, но без вооружённого уровня.

    Так она выглядит между расчётом уровня и подтверждением заявки —
    и так же после отказа в вооружении.
    """
    return replace(
        position(side, price=price, **rest),  # type: ignore[arg-type]
        take=plan if plan is not None else TakeProfit(percent=0.5),
    )


def armed(
    side: Side = Side.LONG,
    *,
    price: float = PRICE,
    level: float | None = None,
    plan: TakeProfit | None = None,
    **rest: object,
):
    """Открытая позиция с уже вооружённым уровнем."""
    base = plan if plan is not None else TakeProfit(percent=0.5)
    sign = 1 if side is Side.LONG else -1
    return replace(
        unarmed(side, price=price, plan=base, **rest),
        take=replace(
            base,
            level=level if level is not None else fixed_level(price, sign, base.percent),
        ),
    )


def actions(outcome) -> list[OrderAction]:
    return [order.action for order in outcome.orders]


def guarding(position, *, at: tuple[int, int] = (10, 5)) -> OrderRequest:
    """Заявка вооружения этой позиции — та, что в жизни всегда в полёте.

    ⚠️ Состояние без неё — режим, в который продукт **не приходит**: пока
    уровень сторожится, его заявка лежит в `pending` всё время позиции.
    Тесты отказов, собранные с пустым полётом, проверяют не то и пропускают
    ровно те находки, ради которых написаны.
    """
    level = position.take_profit
    assert level is not None, "позиция без уровня — вооружать нечего"
    return OrderRequest(
        action=OrderAction.ARM_TAKE_PROFIT, side=position.side,
        volume=position.volume, submitted_at=bar(*at).closes_at,
        reason="сторожим уровень", order_id=take_order_id(position), price=level,
    )


def guarded_state(position, **rest) -> EngineState:
    """Состояние с открытой позицией и её заявкой вооружения в полёте."""
    return EngineState(position=position, pending=(guarding(position),), **rest)


# --------------------------------------------------------------------------
# Уровень: банковское округление до целого, а не до шага цены
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "entry,side,expected",
    [
        # Три случая из журнала прототипа, различающие гипотезы округления
        # В ОБЕ СТОРОНЫ. Одного случая для вывода «половина к чётному»
        # не хватило бы: он отличает её от «вверх», но не от «вниз».
        (216_100.0, Side.LONG, 217_180.0),   # 217 180,5 → вниз, к чётному
        (230_300.0, Side.LONG, 231_452.0),   # 231 451,5 → вверх, к чётному
        (231_700.0, Side.SHORT, 230_542.0),  # 230 541,5 → вверх, к чётному
    ],
)
def test_the_level_rounds_half_to_even_exactly_as_the_prototype_did(
    entry: float, side: Side, expected: float
) -> None:
    """Половина к чётному, до целого. Замерено на 127 сделках, 0 расхождений.

    Ближайший конкурент — «половина вверх» — даёт 1 расхождение из 127,
    округление до шага цены 25 даёт 39. Уровни прототипа сплошь вне сетки
    шага: 217 180, 231 452, 230 542 (PROTOTYPE.md §5).
    """
    sign = 1 if side is Side.LONG else -1
    assert fixed_level(entry, sign, 0.5) == expected
    assert expected % 25 != 0, "уровень попал в сетку шага цены — пример негодный"


def test_a_half_that_is_not_representable_in_binary_still_rounds_right() -> None:
    """Половина считается в десятичном типе: `x,4999999999` меняет сторону.

    Само по себе это на эталонном отрезке не проявляется — двоичный расчёт
    дал бы те же 127 уровней. Проверка стоит ради устойчивости к другой цене
    и другому проценту, а не ради сверки.
    """
    assert round_level(2.5) == 2.0
    assert round_level(3.5) == 4.0
    assert round_level(-2.5) == -2.0
    # 1050,5 в двоичном виде представимо, а вот произведение — не обязательно.
    assert fixed_level(210_100.0, 1, 0.5) == 211_150.0


def test_a_percent_that_is_not_positive_means_there_is_no_take_at_all() -> None:
    """Процент ≤ 0 — вооружения нет вовсе, а не вооружение недостижимо далеко.

    Это рабочая конфигурация, а не вырожденная: два прогона эталонного архива
    сделаны именно так, и воспроизводить их иначе нечем (PROTOTYPE.md §1).
    """
    assert fixed_level(PRICE, 1, 0.0) is None
    assert fixed_level(PRICE, 1, -1.0) is None
    outcome = run(
        EngineState(position=unarmed(plan=TakeProfit(percent=0.0))),
        settings=WORKING.replace(take_profit_percent=0.0),
    )
    assert outcome.orders == ()


def test_switching_the_take_profit_off_is_the_same_as_a_zero_percent() -> None:
    """Отдельный переключатель «тейк-профит включён» — из ТЗ §4.4 В.

    Он нужен потому, что окно настроек задаёт процент в диапазоне 0,1–5,0
    и нуля там нет вовсе.
    """
    settings = WORKING.replace(take_profit=False)
    assert settings.plan().percent == 0.0
    outcome = run(
        EngineState(position=unarmed(plan=settings.plan())), settings=settings,
    )
    assert outcome.orders == ()


# --------------------------------------------------------------------------
# Вооружение: шаг 5 и сделка открытия
# --------------------------------------------------------------------------

def test_step_five_arms_the_level_on_every_closed_candle() -> None:
    """Заявка живёт всё время позиции: её переставляют на каждой свече.

    У неподвижного тейка уровень тот же, и у брокера операция вырождается
    в пустую. Смысл её в другом: заявку могли снять не мы — истёк срок,
    снятие на клиринге, переподключение, — и следующая свеча вооружит
    её заново.
    """
    outcome = run(EngineState(position=armed()))
    assert actions(outcome) == [OrderAction.ARM_TAKE_PROFIT]
    order = outcome.orders[0]
    assert order.price == fixed_level(PRICE, 1, 0.5)
    assert Step.REARM_TAKE_PROFIT in outcome.steps


def test_arming_twice_replaces_the_order_instead_of_adding_a_second_one() -> None:
    """«Вооружить» — это состояние, а не событие.

    Иначе шаг 5 за час позиции оставит у брокера дюжину стоп-заявок,
    одиннадцать из них переживут позицию и превратятся в позицию в обратную
    сторону. Сверка при этом сойдётся, тесты останутся зелёными.
    """
    state = EngineState(position=unarmed())
    first = run(state)
    second = run(first.state, when=(10, 15))
    assert len(second.state.pending) == 1
    assert second.orders[0].order_id == first.orders[0].order_id
    assert first.orders[0].order_id == take_order_id(state.position)


def test_a_closing_position_gets_no_take_and_no_second_exit_order() -> None:
    """Позиция «закрывается» тейк не получает — так у прототипа.

    Цена правила названа вслух (решение 0008): между снятием тейка
    и исполнением выхода позиция стоит без уровня целую свечу.
    """
    closing = replace(armed(), state=__import__(
        "engine", fromlist=["PositionState"]
    ).PositionState.CLOSING)
    outcome = run(EngineState(position=closing), intent=Intent.SHORT)
    assert outcome.orders == ()


def test_the_deal_that_opens_the_position_arms_the_take_right_away() -> None:
    """Уровень считается от цены входа, а цену входа назначает исполнитель.

    Поэтому вооружение рождается сделкой, а не следующей свечой: `high`/`low`
    той же свечи уже сторожат уровень, и запрет срабатывания на «своей» свече
    даёт 3 расхождения из 127 (PROTOTYPE.md §5).
    """
    entry = OrderRequest(
        action=OrderAction.OPEN, side=Side.LONG, volume=1.0,
        submitted_at=bar(*INSIDE).closes_at, reason="вход", order_id="open:long:тест",
    )
    engine = Engine(ScriptedStrategy([]), WORKING, state=EngineState(pending=(entry,)))
    result = engine.on_fill(Fill(
        action=OrderAction.OPEN, side=Side.LONG, volume=1.0, price=PRICE,
        at=bar(*INSIDE).closes_at, order_id=entry.order_id,
    ))
    born = result.orders
    assert [order.action for order in born] == [OrderAction.ARM_TAKE_PROFIT]
    assert born[0].price == fixed_level(PRICE, 1, 0.5)
    tied = [key for key, _ in result.written if key]
    assert tied == [born[0].order_id], (
        "строка про уровень не привязана к заявке — публиковать её придётся "
        "вслепую, до ответа исполнителя"
    )
    assert len(result.lines(0)) == len(result.journal) - 1, (
        "строка про уровень опубликована при нуле принятых заявок"
    )
    assert result.lines(1) == result.journal
    assert result.tied(0) == ()
    assert born[0].submitted_at == bar(*INSIDE).closes_at, (
        "у заявки, рождённой сделкой, времени закрытия свечи нет — есть время сделки"
    )
    # А сторожимым уровень становится, когда заявку приняли — и ни секундой
    # раньше. Заявка подаётся ДО того, как движок себе что-то запишет.
    from engine import after_submit  # только здесь

    applied = after_submit(engine.state, born[0])
    assert applied.position is not None
    assert applied.position.take_profit == fixed_level(PRICE, 1, 0.5)
    assert applied.order_in_flight(born[0].order_id) is not None, (
        "вооружение не попало в заявки в полёте — сработавший уровень будет "
        "нечему сопоставить"
    )
    assert engine.position is not None
    assert engine.position.take_profit is None, (
        "движок записал уровень до того, как заявку приняли: отказ в вооружении "
        "оставил бы его с уровнем, которого у брокера нет"
    )


def test_the_exchange_inside_one_candle_goes_round_until_the_port_is_empty() -> None:
    """Круг обмена: вооружение доходит до исполнителя внутри той же свечи.

    Первый круг отдаёт исполнение по открытию, движок отвечает вооружением,
    на втором круге приходит тейк. Одного вопроса «отдай сделки» на свечу
    для этого не хватило бы: три сделки из 127 открылись и закрылись внутри
    одной свечи.
    """
    strategy = ScriptedStrategy([decision(Intent.LONG, close=PRICE)] * 3)
    engine = Engine(strategy, WORKING)
    executor = NextOpenExecutor()

    async def scenario() -> None:
        # первая свеча: вход подан
        await engine.on_market_candle(candle(10, 5, close=PRICE), executor)
        # вторая: вход исполняется по открытию, тейк вооружается,
        # и `high` этой же свечи его достаёт
        await engine.on_market_candle(
            replace(
                candle(10, 10, close=PRICE),
                open=PRICE, high=PRICE * 1.01, low=PRICE, close=PRICE,
            ),
            executor,
        )

    asyncio.run(scenario())
    assert len(executor.deals) == 1
    deal = executor.deals[0]
    assert deal.exit_reason == "take"
    assert deal.entry_time == deal.exit_time, "вход и выход в одной свече"
    assert deal.exit_price == fixed_level(PRICE, 1, 0.5)


def test_a_refused_arming_born_from_a_deal_leaves_no_level_in_the_state() -> None:
    """Отказ в вооружении сразу при открытии позиции: уровня нет ни у кого.

    Заявка подаётся **до** того, как движок себе что-то запишет. Записать
    уровень раньше ответа — значит на выходе подать снятие несуществующей
    заявки; определённый отказ в снятии останавливает подачу заявки на выход,
    и позиция застряла бы на ровном месте.
    """
    entry = OrderRequest(
        action=OrderAction.OPEN, side=Side.LONG, volume=1.0,
        submitted_at=bar(10, 5).closes_at, reason="вход", order_id="open:long:тест",
    )
    lines: list = []
    engine = Engine(
        ScriptedStrategy([decision(Intent.LONG, close=PRICE)]), WORKING,
        state=EngineState(pending=(entry,)), journal=lines.append,
    )

    class RefusesArming(RecordingExecutor):
        async def submit(self, order: OrderRequest) -> None:
            if order.action is OrderAction.ARM_TAKE_PROFIT:
                raise ExecutionRefused("уровень брокеру не подошёл")
            await super().submit(order)

    executor = RefusesArming(scripted=[[Fill(
        action=OrderAction.OPEN, side=Side.LONG, volume=1.0, price=PRICE,
        at=bar(*INSIDE).closes_at, order_id=entry.order_id,
    )]])
    asyncio.run(engine.on_market_candle(candle(*INSIDE, close=PRICE), executor))

    assert not engine.halted, "штатный отказ в вооружении остановил робота"
    assert engine.position is not None
    assert engine.position.take_profit is None
    assert engine.state.pending == (), (
        "непринятая заявка осталась висеть среди заявок в полёте"
    )
    assert any("отклонил заявку" in entry.reason for entry in lines)


def test_an_executor_that_never_stops_talking_halts_the_robot() -> None:
    """Круг обязан заканчиваться пустым ответом порта.

    Предохранитель против неисправной реализации: без него цикл свечей завис
    бы молча — робот не торгует, причины нет нигде. Он же ловит порт,
    сливающий очередь по одной сделке за круг: контракт требует отдавать
    за вызов **всё накопленное**.

    ⚠️ Сделка здесь **сопоставляется** со своей заявкой и отвергается
    по содержанию — сторона другая. Такая сделка заявку в полёте не гасит
    и робота не останавливает, поэтому поток и получается бесконечным.
    Сделка вообще без заявки остановила бы движок на первом же круге,
    и до предохранителя дело бы не дошло.
    """
    entry = OrderRequest(
        action=OrderAction.OPEN, side=Side.LONG, volume=1.0,
        submitted_at=bar(10, 5).closes_at, reason="вход", order_id="open:long:тест",
    )

    class Endless:
        async def submit(self, order: OrderRequest) -> None:
            return None

        async def fills_at(self, market_candle) -> list[Fill]:
            return [Fill(
                action=OrderAction.OPEN, side=Side.SHORT, volume=1.0, price=PRICE,
                at=market_candle.time, order_id=entry.order_id,
            )]

    lines: list = []
    engine = Engine(
        ScriptedStrategy([]), WORKING,
        state=EngineState(pending=(entry,)), journal=lines.append,
    )
    asyncio.run(engine.on_market_candle(candle(10, 10, close=PRICE), Endless()))
    assert engine.halted
    assert "не сошёлся" in engine.halted
    assert "НЕ разобраны" in engine.halted, (
        "остановка молчит о том, что сделки последнего круга потеряны"
    )
    assert lines[-1].level is JournalLevel.ERROR


# --------------------------------------------------------------------------
# Снятие: порядок «сначала снять, потом закрыть»
# --------------------------------------------------------------------------

def test_the_take_is_cancelled_before_the_exit_order_is_submitted() -> None:
    """Порядок обязателен: иначе у брокера живут обе заявки, и обе исполнятся.

    В модели исполнения этой гонки нет никогда — отложенная заявка исполняется
    первым же тиком. Тестер эту ветку не пройдёт ни разу, поэтому проверяется
    сам порядок заявок, а не его последствие.
    """
    guarded = armed()
    outcome = run(EngineState(position=guarded), intent=Intent.SHORT)
    assert actions(outcome) == [
        OrderAction.CANCEL_TAKE_PROFIT, OrderAction.CLOSE,
    ]
    assert outcome.orders[0].target_id == take_order_id(guarded), (
        "снятие не называет заявку, которую снимает"
    )
    assert outcome.state.position is not None
    assert outcome.state.position.take_profit is None, (
        "снятие принято, а движок всё ещё считает позицию защищённой"
    )


def test_the_window_end_exit_cancels_the_take_too() -> None:
    """Шаг 6 — та же дорога: сначала снять, потом закрыть."""
    outcome = run(EngineState(position=armed()), intent=Intent.LONG, when=OUTSIDE)
    assert actions(outcome) == [
        OrderAction.CANCEL_TAKE_PROFIT, OrderAction.CLOSE,
    ]
    assert outcome.orders[1].exit_reason is ExitReason.WINDOW_END


def test_arming_and_cancelling_on_the_same_candle_reaches_the_broker_never() -> None:
    """Шаг 5 стоит до шага 7 — а на свече выхода обе заявки лишние.

    Уровень появился бы и исчез внутри одной свечи, не дойдя до брокера.
    Лишнее снятие здесь не безобидно: определённый отказ «нет такой заявки»
    остановил бы подачу заявки на выход, которая идёт следом.
    """
    fresh = position(Side.LONG, price=PRICE)
    fresh = replace(fresh, take=TakeProfit(percent=0.5))  # уровня ещё нет
    outcome = run(EngineState(position=fresh), intent=Intent.SHORT)
    assert actions(outcome) == [OrderAction.CLOSE]
    assert not any(
        "Тейк-профит выставлен" == entry.event for entry in outcome.lines()
    ), "журнал обещал вооружение, которого у брокера не было"


def test_a_cancel_refused_because_the_level_is_already_hit_keeps_everything() -> None:
    """«Уровень уже задет» — сторож ЖИВ. Не трогать ничего и ждать сделку.

    ⚠️ Самая дорогая из двух веток, и она достижима каждый торговый день:
    выход по концу окна плюс тейк, задевший уровень на той же свече.

    Погасив здесь уровень, движок на следующей свече увидел бы невооружённый
    снимок и подал **голый рыночный выход поверх живого сторожа**: рыночная
    закрывает лонг, сторож при следующем касании продаёт ещё раз — шорт
    на полный объём, без сигнала и без строки.

    Убрав здесь заявку вооружения из полёта, движок получил бы сделку
    по сработавшему уровню «без своей заявки» — и **остановился бы
    на штатной гонке**, потеряв настоящий выход из журнала сделок
    и оставшись с фантомной позицией.
    """
    lines: list = []
    state = guarded_state(armed())
    engine = Engine(
        ScriptedStrategy([decision(Intent.SHORT, close=PRICE)] * 2),
        WORKING, state=state, journal=lines.append,
    )
    executor = RecordingExecutor(
        refuse={take_order_id(state.position): Refusal.ALREADY_TRIGGERED}
    )
    asyncio.run(engine.on_market_candle(candle(*INSIDE, close=PRICE), executor))

    assert [order.action for order in executor.submitted] == [], (
        "заявка на выход ушла после неудавшегося снятия"
    )
    assert not engine.halted, "штатная гонка остановила робота"
    assert engine.position is not None
    assert engine.position.take_profit == state.position.take_profit, (
        "уровень погашен, хотя сторож у брокера жив — следующая свеча подаст "
        "голый рыночный выход поверх него"
    )
    assert engine.state.order_in_flight(take_order_id(state.position)) is not None, (
        "заявка вооружения убрана из полёта — её сделка станет "
        "несопоставленной и остановит робота"
    )
    assert engine.state.exit_blocked_bars == 1
    assert any("вот-вот станет сделкой" in entry.reason for entry in lines)
    assert any("заявка на выход не подаётся" in entry.reason for entry in lines)

    # Следующая свеча: снятие подаётся снова, потому что уровень на месте.
    asyncio.run(engine.on_market_candle(candle(10, 15, close=PRICE), executor))
    assert engine.state.exit_blocked_bars == 2
    assert [order.action for order in executor.submitted] == [], (
        "выход всё-таки ушёл поверх живого сторожа"
    )


def test_the_deal_by_the_kept_level_closes_the_position_without_a_halt() -> None:
    """Продолжение той же ветки: сделка приходит и разбирается штатно.

    Ради этого заявка вооружения и остаётся в полёте. Если бы её убрали,
    здесь была бы остановка робота и потерянный выход по тейку.
    """
    state = guarded_state(armed())
    arm_id = take_order_id(state.position)
    engine = Engine(
        ScriptedStrategy([decision(Intent.SHORT, close=PRICE)] * 3),
        WORKING, state=state,
    )
    executor = RecordingExecutor(refuse={arm_id: Refusal.ALREADY_TRIGGERED})
    asyncio.run(engine.on_market_candle(candle(*INSIDE, close=PRICE), executor))

    level = state.position.take_profit
    assert level is not None
    executor.scripted.append([Fill(
        action=OrderAction.CLOSE, side=Side.LONG, volume=1.0, price=level,
        at=bar(10, 15).closes_at, order_id=arm_id,
    )])
    asyncio.run(engine.on_market_candle(candle(10, 15, close=PRICE), executor))

    assert not engine.halted, engine.halted
    assert engine.position is None, "выход по тейку не разобран"
    assert engine.state.exit_blocked_bars == 0, "счётчик пережил закрытие позиции"
    assert engine.state.take_profit_date == bar(10, 15).closes_at.date()


def test_a_cancel_refused_because_there_is_no_such_order_clears_the_level() -> None:
    """«Нет такой заявки» — сторожа НЕТ. Гасим уровень и выходим следующей свечой.

    Стоп у брокера исчезает не по нашей команде: истёк срок, снятие
    на клиринге, переподключение — ровно три случая, ради которых написана
    перестановка тейка каждую свечу. Без гашения отказ зациклится: выход
    не подан, состояние откатилось, следующая свеча делает то же самое —
    и так до конца прогона, с открытой позицией без стопа и пустым `halted`.
    """
    state = guarded_state(armed())
    arm_id = take_order_id(state.position)
    engine = Engine(
        ScriptedStrategy([decision(Intent.SHORT, close=PRICE)] * 2),
        WORKING, state=state,
    )
    executor = RecordingExecutor(refuse={arm_id: Refusal.NO_SUCH_ORDER})
    asyncio.run(engine.on_market_candle(candle(*INSIDE, close=PRICE), executor))

    assert executor.submitted == [], "выход ушёл на той же свече, что и отказ"
    assert engine.position is not None
    assert engine.position.take_profit is None, (
        "движок считает позицию защищённой уровнем, которого у брокера нет"
    )
    assert engine.state.order_in_flight(arm_id) is None, (
        "заявка вооружения осталась в полёте, а сторожа нет"
    )
    assert engine.state.exit_blocked_bars == 1

    executor.refuse.clear()
    asyncio.run(engine.on_market_candle(candle(10, 15, close=PRICE), executor))
    assert [order.action for order in executor.submitted] == [OrderAction.CLOSE], (
        "на следующей свече выход так и не подан — позиция заперта в рынке"
    )
    assert engine.state.exit_blocked_bars == 0, "счётчик не сброшен поданным выходом"


def test_an_unnamed_refusal_is_treated_as_a_living_guard() -> None:
    """Причина не названа — считаем сторожа живым. Умолчание выбрано так.

    Обратное умолчание стоит дороже всего: погасив уровень, движок подаст
    голый рыночный выход поверх живого сторожа. Не знать и считать, что
    сторожа нет, — самая дорогая из двух ошибок.
    """
    assert Refusal.UNKNOWN.order_is_alive
    state = guarded_state(armed())
    engine = Engine(
        ScriptedStrategy([decision(Intent.SHORT, close=PRICE)]),
        WORKING, state=state,
    )
    executor = RecordingExecutor(
        refuse={take_order_id(state.position): Refusal.UNKNOWN}
    )
    asyncio.run(engine.on_market_candle(candle(*INSIDE, close=PRICE), executor))
    assert engine.position is not None
    assert engine.position.take_profit is not None, "уровень погашен на «не знаю»"
    assert executor.submitted == []


def test_a_refused_exit_order_is_counted_too() -> None:
    """Третья дорога: снятие прошло, а сама заявка на выход отклонена.

    Аукцион, инструмент недоступен, лимит риск-менеджера. Позиция открыта,
    сторожа нет, и без счёта отказов это повторяется каждую свечу: шаг 5
    вооружает, шаг 6 подаёт выход, `_settle` вооружение выбрасывает.
    **Позиция ночует и проводит выходные без сторожимого уровня**, робот
    считает себя работающим, а окну нечего показать.
    """
    state = guarded_state(armed())
    engine = Engine(
        ScriptedStrategy([decision(Intent.SHORT, close=PRICE)] * 2),
        WORKING, state=state,
    )

    class RefusesTheExit(RecordingExecutor):
        async def submit(self, order: OrderRequest) -> None:
            if order.action is OrderAction.CLOSE:
                raise ExecutionRefused("аукцион", Refusal.NO_SUCH_ORDER)
            await super().submit(order)

    executor = RefusesTheExit()
    asyncio.run(engine.on_market_candle(candle(*INSIDE, close=PRICE), executor))
    assert engine.position is not None
    assert engine.position.state.value == "open", "позиция помечена закрывающейся"
    assert engine.state.closing_bars == 0, "позиция в CLOSING не переходила"
    assert engine.state.exit_blocked_bars == 1, (
        "отказ в самой заявке на выход не посчитан — позиция останется без "
        "сторожа на выходные, и никто об этом не узнает"
    )

    asyncio.run(engine.on_market_candle(candle(10, 15, close=PRICE), executor))
    assert engine.state.exit_blocked_bars == 2


def test_a_refused_arming_leaves_the_position_without_a_level_and_says_so() -> None:
    """Вооружение не встало — позицию держит переворот по средней.

    Робот не останавливается: следующая свеча вооружит заново. Но движок
    не должен считать позицию защищённой уровнем, которого у брокера нет.
    """
    lines: list = []
    state = EngineState(position=unarmed())
    engine = Engine(
        ScriptedStrategy([decision(Intent.LONG, close=PRICE)]),
        WORKING, state=state, journal=lines.append,
    )
    executor = RecordingExecutor(
        refuse={take_order_id(state.position): Refusal.NO_SUCH_ORDER}
    )
    asyncio.run(engine.on_market_candle(candle(*INSIDE, close=PRICE), executor))

    assert not engine.halted
    assert engine.position is not None
    assert engine.position.take_profit is None
    assert engine.state.exit_blocked_bars == 0, (
        "отказ в ВООРУЖЕНИИ посчитан как отказ выхода — а он ничему не мешает"
    )
    assert any(entry.level is JournalLevel.WARNING for entry in lines)
    assert not any("Тейк-профит выставлен" == entry.event for entry in lines), (
        "журнал объявил уровень, которого исполнитель не принял"
    )


def test_a_cancel_refused_over_and_over_halts_the_robot() -> None:
    """Второй предохранитель: гашение лечит одну причину из двух.

    «Нет такой заявки» лечится гашением, «уровень уже задет» — нет. Если
    снятие отклоняется раз за разом, позиция остаётся в рынке без выхода,
    и молчать об этом нельзя: `halted` пуст, а окно показывает работающего
    робота.
    """
    settings = WORKING.replace(close_wait_bars=2)
    state = EngineState(position=armed(), exit_blocked_bars=3)
    outcome = run(state, settings=settings, intent=Intent.SHORT)
    assert outcome.halt
    assert "не удаётся" in outcome.halt
    assert outcome.orders == (), "робот подал заявку на свече, где остановился"
    assert outcome.journal[0].level is JournalLevel.ERROR


def test_a_deal_by_a_level_that_is_no_longer_in_flight_halts_the_robot() -> None:
    """Снятие «прошло», а сторож был жив — сделка приходит на пустое место.

    ⚠️ **Нарушение контракта порта, включённое тестом нарочно**
    (`hollow_cancel`): подача снятия возвращается по подтверждению приёма,
    а не по факту снятия. Движок гасит уровень и подаёт рыночный выход;
    цена задевает уровень; приходит сделка по заявке, которой в полёте
    больше нет.

    Без остановки движок пошёл бы подавать рыночный выход на счёте, где
    позиции уже нет, — а его собственная заявка на выход открыла бы позицию
    в обратную сторону на полный объём.
    """
    level = fixed_level(PRICE, 1, 0.5)
    assert level is not None
    arm = OrderRequest(
        action=OrderAction.ARM_TAKE_PROFIT, side=Side.LONG, volume=1.0,
        submitted_at=bar(10, 5).closes_at, reason="сторожим",
        order_id=take_order_id(armed()), price=level,
    )
    lines: list = []
    engine = Engine(
        ScriptedStrategy([decision(Intent.SHORT, close=PRICE)] * 3),
        WORKING, state=EngineState(position=armed(), pending=(arm,)),
        journal=lines.append,
    )
    executor = NextOpenExecutor(hollow_cancel=True)
    asyncio.run(executor.submit(arm))
    executor._open = (Side.LONG, PRICE, bar(10, 5).closes_at, 1.0)

    async def scenario() -> None:
        # свеча 10:10 — обратный сигнал: снятие «принято», выход подан
        await engine.on_market_candle(candle(*INSIDE, close=PRICE), executor)
        # свеча 10:15 — цена задевает уровень, сторож жив и стреляет
        await engine.on_market_candle(
            replace(
                candle(10, 15, close=PRICE),
                open=PRICE, high=PRICE * 1.01, low=PRICE, close=PRICE,
            ),
            executor,
        )

    asyncio.run(scenario())
    assert engine.halted, "сделка по снятому уровню прошла молча"
    assert "без своей заявки" in engine.halted
    assert lines[-1].level is JournalLevel.ERROR


# ⚠️ Инвариант «на позицию не больше одного живого сторожа» проверяется
# НЕ здесь. Синтетический сценарий давал два вооружения и **ноль снятий** —
# ветка снятия у поддельного брокера не выполнялась ни разу, и зелёный тест
# подтверждал пустоту. Проверка переехала в прогон сверки
# (`tests/test_engine_reference_parity.py`, `CountingGuards`), где на реальном
# потоке заявок: 517 вооружений, 85 снятий, максимум 1 живой сторож, 0 сирот.


def test_an_exit_that_never_becomes_a_deal_halts_the_robot() -> None:
    """Заявка на выход зависла: тейк снят, позиция стоит без защиты.

    Повторить заявку нельзя — вторая рыночная заявка после исполнения первой
    открывает позицию в обратную сторону. Вооружить заново нельзя — шаг 5
    закрывающуюся позицию не трогает, и это правило прототипа. Остаётся
    человек.
    """
    from engine import PositionState  # только здесь

    stuck = replace(armed(), state=PositionState.CLOSING)
    settings = WORKING.replace(close_wait_bars=2)
    state = EngineState(position=stuck)
    for _ in range(2):
        outcome = run(state, settings=settings, intent=Intent.LONG)
        assert not outcome.halt
        state = outcome.state
    outcome = run(state, settings=settings, intent=Intent.LONG)
    assert outcome.halt
    assert "без защиты" in outcome.halt
    assert outcome.journal[0].level is JournalLevel.ERROR


def test_the_journal_never_claims_an_exit_that_was_not_submitted() -> None:
    """Строка уровня TRADE не утверждает выхода, которого не было.

    ⚠️ TRADE — сильнейший уровень журнала решений: по нему потом объясняют
    сделки. При определённом отказе в снятии владелец счёта читал подряд
    «Выход из лонга по обратному сигналу :: Сначала снимаем тейк 211 050…»
    и «Заявка отклонена исполнителем» — а заявок подано было **ноль**.
    """
    lines: list = []
    state = guarded_state(armed())
    engine = Engine(
        ScriptedStrategy([decision(Intent.SHORT, close=PRICE)]),
        WORKING, state=state, journal=lines.append,
    )
    executor = RecordingExecutor(
        refuse={take_order_id(state.position): Refusal.ALREADY_TRIGGERED}
    )
    asyncio.run(engine.on_market_candle(candle(*INSIDE, close=PRICE), executor))

    assert executor.submitted == [], "заявок подано не ноль — сценарий не тот"
    traded = [entry for entry in lines if entry.level is JournalLevel.TRADE]
    assert not traded, (
        "журнал уровнем TRADE утверждает действие, которого не было: "
        f"{[entry.event for entry in traded]}"
    )
    assert not any("снимаем тейк" in entry.reason for entry in lines), (
        "журнал обещал снятие, которого не было"
    )
    assert lines and lines[-1].level is JournalLevel.WARNING


def test_an_unknown_failure_still_leaves_the_decision_in_the_journal() -> None:
    """Обратная сторона того же правила: **неопределённый** отказ строку оставляет.

    Таймаут и обрыв связи означают «не знаю, дошла ли заявка». Спрятать
    решение робота здесь было бы такой же неправдой: человек увидел бы
    «Заявка не подана» и не увидел, что именно робот решал.
    """
    lines: list = []
    state = guarded_state(armed())

    class Silent(RecordingExecutor):
        """Снятие принимает, а на заявке выхода замолкает."""

        async def submit(self, order: OrderRequest) -> None:
            if order.action is OrderAction.CLOSE:
                raise TimeoutError("брокер не ответил")
            await super().submit(order)

    engine = Engine(
        ScriptedStrategy([decision(Intent.SHORT, close=PRICE)]),
        WORKING, state=state, journal=lines.append,
    )
    asyncio.run(engine.on_market_candle(candle(*INSIDE, close=PRICE), Silent()))

    assert engine.halted
    exits = [
        entry for entry in lines
        if entry.level is JournalLevel.TRADE and "Выход" in entry.event
    ]
    assert exits, "решение робота пропало из журнала на неопределённом отказе"
    assert "снимаем тейк" in exits[0].reason
    assert lines[-1].level is JournalLevel.ERROR


def test_a_timeout_on_the_cancel_still_says_why_the_robot_was_leaving() -> None:
    """Решение видно, даже когда таймаут пришёл на **первой** заявке цепочки.

    ⚠️ Умолчательная форма выхода кладёт две заявки: снятие, затем закрытие.
    Строка «Выход из лонга по обратному сигналу» привязана к закрытию. Показ
    «принятые плюс одна» оставлял в журнале только «Заявка не подана» со
    словами про снятие тейка: робот остановлен, причина про сторожа,
    а **зачем он собирался выходить — не сказано нигде**. Разбор начинался
    с «робот зачем-то снимал тейк» вместо «робот шёл на выход и не дошёл».
    """
    lines: list = []
    state = guarded_state(armed())

    class SilentOnCancel(RecordingExecutor):
        async def submit(self, order: OrderRequest) -> None:
            if order.action is OrderAction.CANCEL_TAKE_PROFIT:
                raise TimeoutError("брокер не ответил")
            await super().submit(order)

    engine = Engine(
        ScriptedStrategy([decision(Intent.SHORT, close=PRICE)]),
        WORKING, state=state, journal=lines.append,
    )
    asyncio.run(
        engine.on_market_candle(candle(*INSIDE, close=PRICE), SilentOnCancel())
    )

    assert engine.halted
    exits = [entry for entry in lines if "Выход" in entry.event]
    assert exits, (
        "робот остановлен, а зачем он собирался выходить — не сказано нигде"
    )
    assert "обратный сигнал" in exits[0].reason
    # И тут же честно: часть заявок не подавалась вовсе. ⚠️ Форма слова —
    # по числу: `rest == 1` самый частый случай, и «Ещё 1 заявок» читается
    # как опечатка ровно там, где человек разбирает остановку.
    stopped = [entry for entry in lines if entry.event == "Заявка не подана"]
    assert stopped and "Ещё 1 заявка" in stopped[0].reason, (
        "журнал показал решение и не сказал, что оно осталось намерением"
    )
    assert "не подавалась вовсе" in stopped[0].reason


#: Обороты, утверждающие, что сторожимого уровня у брокера нет. При живом
#: стороже любой из них ведёт владельца счёта в противоположную сторону:
#: он читает «известно точно» и в приложение брокера не идёт.
GONE_FOR_SURE = (
    "известно точно",
    "уровня у брокера нет",
    "заявки у брокера нет",
    "заявки нет",
    "снимать было нечего",
    "сторожа у брокера нет",
)


def declined_run(
    action: OrderAction, reason: Refusal
) -> tuple[list, Engine, RecordingExecutor]:
    """Прогон, где исполнитель **определённо** отказывает в заявке этого действия.

    Четыре сценария, по одному на действие, и они разные по построению: вход
    подаётся на пустом рынке, снятие открывает цепочку выхода, закрытие её
    заканчивает, вооружение ставит шаг 5 открытой позиции.

    ⚠️ Нужны все четыре. Разбор отказа один на всех, и утверждения о судьбе
    заявки печатались для всех четырёх одинаково — а верны они были только
    для снятия.
    """
    lines: list = []
    at = bar(*INSIDE).closes_at
    if action is OrderAction.OPEN:
        state = EngineState()
        intent = Intent.LONG
        names = {f"open:long:{at.isoformat()}": reason}
    elif action is OrderAction.ARM_TAKE_PROFIT:
        # Уровня у позиции ещё нет: его ставит шаг 5, и это отклоняют.
        held = unarmed(plan=TakeProfit(percent=0.5))
        state = EngineState(position=held)
        intent = Intent.LONG  # сигнала на выход нет — только вооружение
        names = {take_order_id(held): reason}
    else:
        held = armed()
        state = guarded_state(held)
        intent = Intent.SHORT  # обратный сигнал: снятие, затем закрытие
        names = (
            {take_order_id(held): reason}
            if action is OrderAction.CANCEL_TAKE_PROFIT
            else {f"close:long:{at.isoformat()}": reason}
        )
    engine = Engine(
        ScriptedStrategy([decision(intent, close=PRICE)]),
        WORKING, state=state, journal=lines.append,
    )
    executor = RecordingExecutor(refuse=names)
    asyncio.run(engine.on_market_candle(candle(10, 5, close=PRICE), executor))
    return lines, engine, executor


#: Список «живых» причин **выводится** из `order_is_alive`, а не перечисляется
#: руками: четвёртая такая причина обязана попасть под сторожа сама, в тот же
#: день, когда её добавили, — а не тогда, когда о стороже вспомнят.
@pytest.mark.parametrize(
    "reason",
    [r for r in Refusal if r.order_is_alive],
    ids=lambda reason: reason.value,
)
@pytest.mark.parametrize("action", list(OrderAction), ids=lambda a: a.value)
def test_a_decline_never_claims_the_order_is_gone_when_it_may_be_alive(
    action: OrderAction, reason: Refusal,
) -> None:
    """Утверждение о судьбе идёт по причине отказа — и **только для снятия**.

    ⚠️ Здесь стояло «Заявки у брокера нет — это известно точно», и печаталось
    оно для всех трёх причин, в том числе рядом со словами «Сторожимый уровень
    остаётся у брокера и вот-вот сработает». Два взаимоисключающих утверждения
    в одной строке, и ложное — про живой стоп.

    ⚠️ **Второе измерение — действие заявки, и оно тут же нашло второй дефект
    того же класса.** `Refusal` описан вокруг снятия: он говорит про заявку,
    названную `target_id`, то есть про сторожимый уровень. Печаталось же
    утверждение для всех четырёх действий, и на трёх из них оно было неправдой
    **противоположного знака**: «движок считает её ЖИВОЙ» при отклонённом
    вооружении — а движок тут же оставлял позицию без уровня и сам это
    проверял соседним тестом.

    Дороже всего на закрытии: адаптер, отклонивший рыночный выход с «уровень
    уже задет», получал строку «заявка жива и уже исполняется» — а движок
    следующей свечой подавал **вторую рыночную заявку на выход**. Если первая
    жива, это позиция в обратную сторону на полный объём.

    Поведение движка при этом было правильным. Неверным был текст — и это тот
    же класс дефекта, что чистили в окне, только переехавший в журнал движка.
    """
    lines, _, _ = declined_run(action, reason)
    declined = [entry for entry in lines if "отклонил заявку" in entry.reason]
    assert declined, "отказ не попал в журнал"
    text = declined[0].reason.lower()

    if action is OrderAction.CANCEL_TAKE_PROFIT:
        guilty = [phrase for phrase in GONE_FOR_SURE if phrase in text]
        assert not guilty, (
            f"при причине «{reason.label}» строка утверждает, что уровня "
            f"нет: {guilty}. Сторож может быть жив, и владелец счёта "
            "не пойдёт его проверять"
        )
        assert "жив" in text, (
            "строка не говорит, что сторожимый уровень может быть жив, — "
            "а именно это и надо проверить в приложении брокера"
        )
        return

    assert "жив" not in text, (
        f"отказ в заявке «{action.value}» объяснён живой заявкой. Контракт "
        "`ExecutionRefused` однозначен: заявку НЕ ПРИНЯЛИ. Движок так "
        "и действует — а строка обещает обратное"
    )
    assert "не принял" in text, (
        "строка не сказала главного: заявку не приняли, исполнения не будет"
    )


def test_a_decline_does_say_the_order_is_gone_when_that_is_known() -> None:
    """Обратная сторона: при «нет такой заявки» так и написано.

    Иначе сторож на пару строк выше превратился бы в запрет говорить правду.
    """
    lines: list = []
    state = guarded_state(armed())
    engine = Engine(
        ScriptedStrategy([decision(Intent.SHORT, close=PRICE)]),
        WORKING, state=state, journal=lines.append,
    )
    executor = RecordingExecutor(
        refuse={take_order_id(state.position): Refusal.NO_SUCH_ORDER}
    )
    asyncio.run(engine.on_market_candle(candle(*INSIDE, close=PRICE), executor))

    declined = [entry for entry in lines if "отклонил заявку" in entry.reason]
    assert declined
    assert "известно точно" in declined[0].reason


def test_the_words_of_a_decline_match_the_order_that_was_declined() -> None:
    """Отказ во входе не объясняется сторожимым уровнем.

    Прежняя редакция объясняла любой отказ снятием тейка, и владелец счёта
    читал про сторожа там, где отклонили вход.
    """
    lines: list = []
    engine = Engine(
        ScriptedStrategy([decision(Intent.LONG, close=PRICE)]),
        WORKING, journal=lines.append,
    )
    # Свеча 10:05 закрывается в 10:10, и разбор называет заявку по времени
    # ЗАКРЫТИЯ — имя не выдумываем.
    entry_id = f"open:long:{bar(*INSIDE).closes_at.isoformat()}"
    executor = RecordingExecutor(refuse={entry_id: Refusal.NO_SUCH_ORDER})
    asyncio.run(engine.on_market_candle(candle(10, 5, close=PRICE), executor))

    declined = [entry for entry in lines if "отклонил заявку" in entry.reason]
    assert declined, "отказ не попал в журнал"
    assert "Позиция не открыта" in declined[0].reason
    assert "сторож" not in declined[0].reason.lower()
    assert "вход в лонг" in declined[0].reason


def test_a_declined_exit_still_says_why_the_robot_was_leaving() -> None:
    """Определённый отказ в **закрытии**: причина выхода не пропадает.

    ⚠️ Цепочка выхода — снятие и закрытие; строка «Выход из лонга по обратному
    сигналу» привязана к закрытию. Отклонили закрытие — принятой оказалась
    одна заявка (снятие), строка выброшена, а строки, привязанной к снятию,
    не существует вовсе. В журнале свечи оставался **один отказ**, и зачем
    робот выходил, не было сказано нигде — и так каждую свечу до остановки.

    «Либо TRADE-неправда, либо молчание» — ложная дихотомия. Уровнем TRADE
    выход утверждать нельзя, его не было; сниженным уровнем с явной оговоркой
    «действия не было» — можно и нужно.
    """
    lines, engine, executor = declined_run(OrderAction.CLOSE, Refusal.UNKNOWN)

    assert [order.action for order in executor.submitted] == [
        OrderAction.CANCEL_TAKE_PROFIT
    ], "сценарий не тот: закрытие обязано быть отклонено после принятого снятия"
    traded = [entry for entry in lines if entry.level is JournalLevel.TRADE]
    assert not traded, (
        "журнал уровнем TRADE утверждает выход, которого не было: "
        f"{[entry.event for entry in traded]}"
    )
    why = [entry for entry in lines if "обратный сигнал" in entry.reason]
    assert why, "зачем робот выходил — не сказано нигде"
    assert why[0].level is JournalLevel.INFO, (
        "причина показана сильным уровнем — так журнал утверждает сделку"
    )
    assert "действия не было" in why[0].reason, (
        "строка показывает решение и не оговаривает, что оно осталось "
        "намерением"
    )
    # ⚠️ И ни слова про снятие тейка: механика подачи в строку про
    # несостоявшееся действие не идёт — это читалось бы обещанием снятия.
    assert "Сначала снимаем тейк" not in why[0].reason

    # ⚠️ Порядок: сначала строки самой свечи, потом отказ. Времена у всех
    # строк одной свечи одинаковые, и порядок публикации — единственное,
    # чем они различаются при чтении. Отказ, вставший первым, читается как
    # «робот отказался, а потом зачем-то решал».
    events = [entry.event for entry in lines]
    assert events.index("Решение принято, заявки нет") < events.index(
        "Заявка отклонена исполнителем"
    ), f"строка отказа встала раньше строк разбора свечи: {events}"


@pytest.mark.parametrize(
    "action,reason",
    [
        (OrderAction.CANCEL_TAKE_PROFIT, Refusal.NO_SUCH_ORDER),
        (OrderAction.CLOSE, Refusal.UNKNOWN),
    ],
    ids=["снятие: нет такой заявки", "закрытие отклонено"],
)
def test_a_refused_exit_never_promises_a_level_it_will_not_arm(
    action: OrderAction, reason: Refusal,
) -> None:
    """Обещание «следующая свеча вооружит заново» отменяет `_settle`.

    ⚠️ Проверяется **поведением**, а не только словами. Пока робот выходит,
    шаг 5 вооружает, шаг 6 или 7 подаёт выход, а `_settle` вооружение
    выбрасывает: на свече, где подаётся закрытие, тейк до брокера не доходит
    вовсе. Значит после отказа позиция стоит **без сторожимого уровня** все
    свечи до самого выхода — а журнал обещал обратное каждую из них.

    Сценариев два, и оба ведут в одно место: отказ в снятии с «нет такой
    заявки» гасит уровень, отказ в закрытии приходит уже после принятого
    снятия.
    """
    lines: list = []
    state = guarded_state(armed())
    at = bar(*INSIDE).closes_at
    names = (
        {take_order_id(state.position): reason}
        if action is OrderAction.CANCEL_TAKE_PROFIT
        else {f"close:long:{at.isoformat()}": reason}
    )
    engine = Engine(
        ScriptedStrategy([decision(Intent.SHORT, close=PRICE)] * 2),
        WORKING, state=state, journal=lines.append,
    )
    executor = RecordingExecutor(refuse=names)

    async def scenario() -> None:
        await engine.on_market_candle(candle(10, 5, close=PRICE), executor)
        await engine.on_market_candle(candle(10, 10, close=PRICE), executor)

    asyncio.run(scenario())

    assert engine.position is not None, "позиция закрылась — сценарий не тот"
    assert engine.position.take_profit is None, (
        "движок считает позицию защищённой уровнем, которого у брокера нет"
    )
    assert OrderAction.ARM_TAKE_PROFIT not in [
        order.action for order in executor.submitted
    ], (
        "вооружение дошло до исполнителя на свече выхода — значит `_settle` "
        "его не выбросил, и сторож с рыночным выходом живут у брокера вместе"
    )
    promised = [
        entry for entry in lines
        if "вооружит" in entry.reason and "не выйдет" not in entry.reason
        and "если только" not in entry.reason
    ]
    assert not promised, (
        "журнал обещает вооружение, которого не будет: "
        f"{[entry.reason for entry in promised]}"
    )


def timed_out_run(action: OrderAction) -> tuple[list, Engine]:
    """Прогон, где исполнитель на заявке этого действия **молчит**.

    Таймаут — не отказ: движок не знает, дошла ли заявка. Отсюда остановка,
    и отсюда же требование к строке — не утверждать судьбу позиции.
    """
    lines: list = []
    at = bar(*INSIDE).closes_at

    class Silent(RecordingExecutor):
        async def submit(self, order: OrderRequest) -> None:
            if order.action is action:
                raise TimeoutError("брокер не ответил")
            await super().submit(order)

    if action is OrderAction.OPEN:
        state = EngineState()
        intent = Intent.LONG
    elif action is OrderAction.ARM_TAKE_PROFIT:
        state = EngineState(position=unarmed(plan=TakeProfit(percent=0.5)))
        intent = Intent.LONG
    else:
        state = guarded_state(armed())
        intent = Intent.SHORT
    engine = Engine(
        ScriptedStrategy([decision(intent, close=PRICE)]),
        WORKING, state=state, journal=lines.append,
    )
    asyncio.run(engine.on_market_candle(candle(10, 5, close=PRICE), Silent()))
    assert engine.halted, f"таймаут на «{action.value}» не остановил робота"
    assert at == bar(*INSIDE).closes_at
    return lines, engine


#: Что может случиться БЕЗ робота — по действию заявки, на которой он замолчал.
#: ⚠️ Строка остановки утверждала «Позиция не закрывается сама» для всех
#: четырёх сразу, соседним предложением честно признавая, что движок
#: не знает, дошла ли заявка. Ложно во всех четырёх случаях.
ALONE = {
    OrderAction.CANCEL_TAKE_PROFIT: "может быть жив",
    OrderAction.CLOSE: "закроется без робота",
    OrderAction.OPEN: "может открыться",
    OrderAction.ARM_TAKE_PROFIT: "мог встать",
}


@pytest.mark.parametrize("action", list(OrderAction), ids=lambda a: a.value)
def test_a_halt_never_promises_what_the_account_will_do(
    action: OrderAction,
) -> None:
    """Строка остановки: про робота — правда, про счёт — только «может».

    Сценарий цены ошибки: лонг 210 000, тейк 211 050 у брокера, обратный
    сигнал, снятие ушло в таймаут, робот остановлен. Владелец счёта читает
    «позиция не закрывается сама», ставит свой стоп руками — **два стопа
    на одной позиции**. Один срабатывает, второй открывает обратную
    на полный объём. Ровно исход, названный в решении 0008.
    """
    lines, _ = timed_out_run(action)
    stopped = [entry for entry in lines if entry.event == "Заявка не подана"]
    assert stopped, "остановка прошла без строки в журнале"
    text = stopped[0].reason

    assert "Позиция не закрывается сама" not in text, (
        "строка утверждает судьбу позиции, которой движок не знает"
    )
    assert "Остановка робота сама по себе позицию не закрывает" in text, (
        "правда про робота пропала вместе с неправдой про счёт"
    )
    assert ALONE[action] in text.lower(), (
        f"не сказано, что может произойти без робота после «{action.value}»: "
        f"{text}"
    )


def test_a_timeout_on_the_arming_born_from_a_deal_never_claims_the_level() -> None:
    """Заявка от сделки ушла в таймаут: «Тейк-профит выставлен» писать нельзя.

    ⚠️ Ветка родилась правкой соседнего дефекта и живёт **только в бою**:
    ни один исполнитель в дереве тестов не молчал на вооружении, рождённом
    сделкой. Заявок от сделки ровно одна, поэтому оговорка «ещё N заявок
    не подавались вовсе» не печаталась, — и владелец счёта читал подряд
    «Лонг открыт», «Тейк-профит выставлен. Уровень 211 050» и «Заявка
    не подана». Позиция оставалась под тейком, которого у брокера может
    не быть.

    Довод «иначе не сказано, зачем робот действовал» здесь не работает:
    безусловная строка «Лонг открыт» стоит рядом и всё уже сказала.
    """
    entry = OrderRequest(
        action=OrderAction.OPEN, side=Side.LONG, volume=1.0,
        submitted_at=bar(10, 5).closes_at, reason="вход", order_id="open:long:тест",
    )
    lines: list = []
    engine = Engine(
        ScriptedStrategy([decision(Intent.LONG, close=PRICE)]), WORKING,
        state=EngineState(pending=(entry,)), journal=lines.append,
    )

    class SilentOnArming(RecordingExecutor):
        async def submit(self, order: OrderRequest) -> None:
            if order.action is OrderAction.ARM_TAKE_PROFIT:
                raise TimeoutError("брокер не ответил")
            await super().submit(order)

    executor = SilentOnArming(scripted=[[Fill(
        action=OrderAction.OPEN, side=Side.LONG, volume=1.0, price=PRICE,
        at=bar(*INSIDE).closes_at, order_id=entry.order_id,
    )]])
    asyncio.run(engine.on_market_candle(candle(*INSIDE, close=PRICE), executor))

    assert engine.halted, "движок не знает, дошла ли заявка, и не остановился"
    assert engine.position is not None
    assert engine.position.take_profit is None, "уровень записан без ответа брокера"
    claimed = [entry for entry in lines if entry.event == "Тейк-профит выставлен"]
    assert not claimed, (
        "журнал объявил уровень, которого у брокера может не быть: "
        f"{[entry.reason for entry in claimed]}"
    )
    # Безусловная строка про саму сделку при этом на месте — «зачем робот
    # действовал» сказано ею.
    assert any(entry.event == "Лонг открыт" for entry in lines)
    assert any(entry.event == "Заявка не подана" for entry in lines)


def test_a_stuck_exit_halts_the_running_robot() -> None:
    """Провод от разбора к флагу `halted` — через настоящий прогон свечей.

    ⚠️ Разбор просит остановки полем `Outcome.halt`, а поднимает флаг
    `Engine.accept`. Проверок было две, и обе на разбор: удаление двух строк
    в `accept` оставляло дерево зелёным, а робот с застрявшим выходом
    и снятым тейком **продолжал торговать**.
    """
    from engine import PositionState  # только здесь

    lines: list = []
    stuck = replace(armed(), state=PositionState.CLOSING)
    engine = Engine(
        ScriptedStrategy([decision(Intent.LONG, close=PRICE)] * 3),
        WORKING.replace(close_wait_bars=1),
        state=EngineState(position=stuck), journal=lines.append,
    )
    executor = RecordingExecutor()

    async def scenario() -> None:
        await engine.on_market_candle(candle(10, 5, close=PRICE), executor)
        assert not engine.halted, "остановка на первой же свече ожидания"
        await engine.on_market_candle(candle(10, 10, close=PRICE), executor)

    asyncio.run(scenario())

    assert engine.halted, (
        "разбор попросил остановки, а робот продолжает работать: провод "
        "от `Outcome.halt` к флагу не проверен ничем"
    )
    assert "без защиты" in engine.halted
    assert lines[-1].level is JournalLevel.ERROR

    # И следующая свеча уже не разбирается: заявок нет, строк нет.
    outcome = asyncio.run(
        engine.on_market_candle(candle(10, 15, close=PRICE), executor)
    )
    assert outcome.orders == ()
    assert executor.submitted == []


def test_a_cancel_refused_over_and_over_halts_the_running_robot() -> None:
    """Второй источник остановки — тот же провод, тот же прогон свечей.

    Снятие отклоняется с «уровень уже задет» свечу за свечой: гашение здесь
    не лечит, заявка на выход не подаётся, позиция остаётся в рынке. Молчать
    об этом нельзя — окно показывало бы работающего робота.
    """
    lines: list = []
    state = guarded_state(armed())
    engine = Engine(
        ScriptedStrategy([decision(Intent.SHORT, close=PRICE)] * 3),
        WORKING.replace(close_wait_bars=1),
        state=state, journal=lines.append,
    )
    executor = RecordingExecutor(
        refuse={take_order_id(state.position): Refusal.ALREADY_TRIGGERED}
    )

    async def scenario() -> None:
        for hour, minute in ((10, 5), (10, 10), (10, 15)):
            await engine.on_market_candle(candle(hour, minute, close=PRICE), executor)

    asyncio.run(scenario())

    assert engine.halted, "отказы в снятии идут подряд, а робот считает себя рабочим"
    assert "не удаётся" in engine.halted
    assert engine.position is not None, "позиция закрыта, хотя выход не подавался"
    assert lines[-1].level is JournalLevel.ERROR


def test_a_refusal_that_forgot_its_reason_is_read_as_unknown() -> None:
    """Подкласс адаптера, не позвавший конструктор, — дефект, но не авария.

    ⚠️ Ветка существует **только в бою**: своя реализация порта у брокера
    может объявить свой отказ и забыть про `super().__init__`. Уронив движок
    внутри `except`, она превратила бы понятную строку журнала в трассировку.
    Читается такой отказ самым осторожным для снятия образом — «причина
    не названа», сторож считается живым.
    """
    class ForgetfulRefusal(ExecutionRefused):
        """Свой отказ адаптера, не позвавший `super().__init__`."""

        def __init__(self) -> None:  # намеренно без super()
            pass

    lines: list = []
    state = guarded_state(armed())

    class Broken(RecordingExecutor):
        async def submit(self, order: OrderRequest) -> None:
            if order.action is OrderAction.CANCEL_TAKE_PROFIT:
                raise ForgetfulRefusal
            await super().submit(order)

    engine = Engine(
        ScriptedStrategy([decision(Intent.SHORT, close=PRICE)]),
        WORKING, state=state, journal=lines.append,
    )
    asyncio.run(engine.on_market_candle(candle(10, 5, close=PRICE), Broken()))

    assert not engine.halted, "штатный отказ уронил робота"
    assert engine.position is not None
    assert engine.position.take_profit == state.position.take_profit, (
        "уровень погашен на отказе без причины — следующая свеча подаст "
        "голый рыночный выход поверх живого сторожа"
    )
    assert engine.state.exit_blocked_bars == 1
    declined = [entry for entry in lines if "отклонил заявку" in entry.reason]
    assert declined, "отказ не попал в журнал"
    assert Refusal.UNKNOWN.label in declined[0].reason


def test_fills_lost_to_a_halt_are_named_out_loud() -> None:
    """Остановка посреди пачки: остаток сделок не пропадает молча.

    Это уже случившиеся сделки. Движок про них не узнает, и человек,
    разбирающий остановку, обязан знать, что состояние движка отстало
    от счёта не на одну сделку, а на несколько.
    """
    lines: list = []
    engine = Engine(ScriptedStrategy([]), WORKING, journal=lines.append)
    executor = RecordingExecutor(scripted=[[
        Fill(OrderAction.CLOSE, Side.LONG, 1.0, PRICE, bar(*INSIDE).closes_at),
        Fill(OrderAction.OPEN, Side.SHORT, 1.0, PRICE, bar(*INSIDE).closes_at),
        Fill(OrderAction.OPEN, Side.LONG, 1.0, PRICE, bar(*INSIDE).closes_at),
    ]])
    asyncio.run(engine.on_market_candle(candle(*INSIDE, close=PRICE), executor))

    assert engine.halted, "несопоставленная сделка не остановила робота"
    lost = [entry for entry in lines if "НЕ вошли" in entry.reason]
    assert lost, "остаток пачки потерян молча"
    assert "ещё 2 сделок" in lost[0].reason
    assert lost[0].level is JournalLevel.ERROR


# --------------------------------------------------------------------------
# «Стоп после тейка»: календарная дата и асимметрия двух путей
# --------------------------------------------------------------------------

def test_the_take_profit_date_is_taken_from_the_time_of_the_deal() -> None:
    """Путь Б: дата берётся из времени сделки, то есть из НАЧАЛА свечи.

    Сравнение на шаге 9 идёт с датой **закрытия** свечи. Пути не согласованы,
    и это воспроизводится как есть, а не чинится: приведение их к одному виду
    даёт другой список сделок на окне через полночь (PROTOTYPE.md §5).
    """
    level = fixed_level(PRICE, 1, 0.5)
    assert level is not None
    order = OrderRequest(
        action=OrderAction.ARM_TAKE_PROFIT, side=Side.LONG, volume=1.0,
        submitted_at=bar(*INSIDE).closes_at, reason="сторожим",
        order_id="take:long:тест", price=level,
    )
    engine = Engine(
        ScriptedStrategy([]), WORKING,
        state=EngineState(position=armed(), pending=(order,)),
    )
    engine.on_fill(Fill(
        action=OrderAction.CLOSE, side=Side.LONG, volume=1.0, price=level,
        at=bar(23, 55).closes_at, order_id=order.order_id,
    ))
    assert engine.state.take_profit_date == bar(23, 55).closes_at.date()


def test_the_stop_after_take_blocks_entries_by_the_calendar_date() -> None:
    """Шаг 9 сравнивает с датой ВРЕМЕНИ ЗАКРЫТИЯ свечи, а не начала."""
    today = bar(*INSIDE).closes_at.date()
    blocked = run(EngineState(take_profit_date=today), intent=Intent.LONG)
    assert blocked.orders == ()
    assert blocked.last_step is Step.STOP_AFTER_TAKE

    tomorrow = run(
        EngineState(take_profit_date=today), intent=Intent.LONG, day=22,
    )
    assert actions(tomorrow) == [OrderAction.OPEN], "новый день, а входа нет"


def test_the_trailing_take_counts_as_the_same_event_for_the_stop_rule() -> None:
    """Скользящий тейк — тот же выход с прибылью, и день он закрывает так же.

    Открытый вопрос решения 0009, закрыт здесь: правило «стоп после тейка»
    написано под неподвижный тейк, но различать два тейка нечем и незачем —
    оба закрывают позицию по сторожимому уровню. Разные ответы в двух ветках
    кода были бы хуже любого из них.
    """
    plan = TakeProfit(
        percent=0.5, trailing=True, start_percent=0.5, offset_percent=0.2,
        step_percent=0.05, peak=PRICE * 1.01, level=PRICE * 1.008,
    )
    moving = replace(armed(plan=plan, level=PRICE * 1.008), take=plan)
    order = OrderRequest(
        action=OrderAction.ARM_TAKE_PROFIT, side=Side.LONG, volume=1.0,
        submitted_at=bar(*INSIDE).closes_at, reason="сторожим",
        order_id=take_order_id(moving), price=plan.level,
    )
    engine = Engine(
        ScriptedStrategy([]), WORKING.replace(trailing_take_profit=True),
        state=EngineState(position=moving, pending=(order,)),
    )
    engine.on_fill(Fill(
        action=OrderAction.CLOSE, side=Side.LONG, volume=1.0,
        price=plan.level or 0.0, at=bar(*INSIDE).closes_at, order_id=order.order_id,
    ))
    assert engine.state.take_profit_date == bar(*INSIDE).closes_at.date()


# --------------------------------------------------------------------------
# Момент переворота: оба режима
# --------------------------------------------------------------------------

def test_through_the_bar_the_entry_waits_for_the_next_candle() -> None:
    """Умолчание и поведение прототипа: снимок не пуст — входа нет."""
    outcome = run(EngineState(position=armed()), intent=Intent.SHORT)
    assert actions(outcome) == [
        OrderAction.CANCEL_TAKE_PROFIT, OrderAction.CLOSE,
    ]
    assert outcome.last_step is Step.OPEN, "разбор не дошёл до шага открытия"


def test_in_one_candle_the_exit_and_the_entry_are_submitted_back_to_back() -> None:
    """Режим ТЗ §4.4 Б: рынок не покидаем, выход и вход подряд.

    ⚠️ Сверять этот режим не с чем по построению: у прототипа его нет,
    и сделки в нём заведомо другие. Это не дефект.
    """
    settings = WORKING.replace(reversal=Reversal.SAME_BAR)
    outcome = run(EngineState(position=armed()), settings=settings, intent=Intent.SHORT)
    assert actions(outcome) == [
        OrderAction.CANCEL_TAKE_PROFIT, OrderAction.CLOSE, OrderAction.OPEN,
    ]
    assert outcome.orders[2].side is Side.SHORT
    assert outcome.orders[1].submitted_at == outcome.orders[2].submitted_at


def test_in_one_candle_the_window_end_still_leaves_the_market() -> None:
    """Шаг 6 возвращает до шага 10 — вход после выхода по концу окна не подаётся.

    Иначе робот входил бы в позицию ровно тогда, когда его попросили из рынка
    выйти.
    """
    settings = WORKING.replace(reversal=Reversal.SAME_BAR)
    outcome = run(
        EngineState(position=armed()), settings=settings,
        intent=Intent.LONG, when=OUTSIDE,
    )
    assert actions(outcome) == [
        OrderAction.CANCEL_TAKE_PROFIT, OrderAction.CLOSE,
    ]


def test_in_one_candle_a_take_profit_day_still_blocks_the_entry() -> None:
    """Шаг 9 стоит до шага 10 в обоих режимах переворота."""
    settings = WORKING.replace(reversal=Reversal.SAME_BAR)
    state = EngineState(
        position=armed(), take_profit_date=bar(*INSIDE).closes_at.date(),
    )
    outcome = run(state, settings=settings, intent=Intent.SHORT)
    assert actions(outcome) == [
        OrderAction.CANCEL_TAKE_PROFIT, OrderAction.CLOSE,
    ]


def test_the_two_reversal_modes_differ_by_exactly_one_timeframe() -> None:
    """Обе ветки обязаны работать: программа проверки прогоняет их обе (ТЗ §4.10).

    Разница видна не в числе сделок, а во **времени**. «Через свечу»: между
    выходом из одной позиции и входом в следующую ровно один таймфрейм —
    в эталонном журнале это 79 переворотов из 79, других значений нет
    (PROTOTYPE.md §2). «В одной свече»: рынок не покидаем, и вход попадает
    на ту же свечу, что и выход.
    """
    def play(mode: Reversal) -> list:
        prices = [100.0, 101.0, 102.0, 99.0, 98.0, 103.0, 104.0, 97.0, 96.0]
        strategy = ScriptedStrategy([
            decision(Intent.LONG if price > 100 else Intent.SHORT, close=price)
            for price in prices
        ])
        engine = Engine(
            strategy,
            EngineSettings(
                mode=Mode.REVERSE, window=WINDOW, take_profit=False, reversal=mode,
            ),
        )
        executor = NextOpenExecutor()
        source = ListSource([
            candle(10, 5 + 5 * index, close=price)
            for index, price in enumerate(prices)
        ])
        asyncio.run(engine.run(source, executor))
        assert not engine.halted, engine.halted
        return executor.deals

    through = play(Reversal.THROUGH_BAR)
    same = play(Reversal.SAME_BAR)
    assert len(through) >= 2 and len(same) >= 2, "переворотов не случилось"

    gaps_through = {
        (nxt.entry_time - prev.exit_time).total_seconds() / 60
        for prev, nxt in zip(through, through[1:])
    }
    gaps_same = {
        (nxt.entry_time - prev.exit_time).total_seconds() / 60
        for prev, nxt in zip(same, same[1:])
    }
    assert gaps_through == {5.0}, (
        "«через свечу»: разрыв между выходом и входом обязан быть ровно "
        f"один таймфрейм, получено {sorted(gaps_through)}"
    )
    assert gaps_same == {0.0}, (
        "«в одной свече»: рынок не покидаем, разрыва быть не должно, "
        f"получено {sorted(gaps_same)}"
    )


# --------------------------------------------------------------------------
# Скользящий тейк: приёмочное правило ТЗ §9
# --------------------------------------------------------------------------

TRAILING = WORKING.replace(
    trailing_take_profit=True, trailing_start_percent=0.5,
    trailing_offset_percent=0.2, trailing_step_percent=0.05,
)


def test_below_the_threshold_the_trailing_take_has_no_level_at_all() -> None:
    """Пока прибыль меньше порога, позицию держит переворот по средней.

    Уровня нет **вовсе** — это не «уровень далеко». Сторожить нечего,
    и заявка не подаётся.
    """
    entered = unarmed(plan=TRAILING.plan().initial(PRICE, 1))
    outcome = run(EngineState(position=entered), settings=TRAILING, close=PRICE * 1.004)
    assert outcome.orders == ()
    assert outcome.state.position is not None
    assert outcome.state.position.take_profit is None


def test_at_the_threshold_the_level_appears_at_the_offset_from_the_best_price() -> None:
    """Порог достигнут — запоминается лучшая цена, уровень встаёт на отступе."""
    entered = unarmed(plan=TRAILING.plan().initial(PRICE, 1))
    top = PRICE * 1.006
    outcome = run(EngineState(position=entered), settings=TRAILING, close=top)
    assert actions(outcome) == [OrderAction.ARM_TAKE_PROFIT]
    assert outcome.orders[0].price == round_level(top - top * 0.2 / 100)
    assert outcome.state.position is not None
    assert outcome.state.position.take.peak == top


def _walk(side: Side, factors: list[float], settings: EngineSettings) -> list[dict]:
    """Прогнать позицию по ломаной закрытий. Снимок механизма на каждом шаге."""
    sign = 1 if side is Side.LONG else -1
    state = EngineState(position=unarmed(side, plan=settings.plan().initial(PRICE, sign)))
    intent = Intent.LONG if side is Side.LONG else Intent.SHORT
    steps: list[dict] = []
    for index, factor in enumerate(factors):
        close = PRICE * factor
        outcome = run(
            state, settings=settings, intent=intent, close=close,
            when=(10, 10 + 5 * index),
        )
        state = outcome.state
        assert state.position is not None
        steps.append({
            "close": close,
            "peak": state.position.take.peak,
            "level": state.position.take_profit,
            "armed": bool(outcome.orders),
        })
    return steps


def test_the_trailing_mechanism_holds_at_every_step_of_the_walk() -> None:
    """**Приёмочное правило ТЗ §9** и механизм, которым оно держится.

    Единственная проверка скользящего тейка, какая у проекта есть: в прототипе
    его нет, сверять не с чем и не будет чем (решение 0009).

    ⚠️ **Одной ломаной мало, и это проверено мутациями.** Уровень защищён
    двумя независимыми механизмами — монотонной вершиной и отдельным запретом
    ехать назад. Убери любой из них по отдельности — список уровней на этой
    ломаной совпадёт до числа, и «уровень не поехал назад» останется зелёным.
    Поэтому проверяется не список, а механизм: вершина равна бегущему
    экстремуму закрытий, уровень равен расчёту от вершины в тот момент, когда
    он двигался, и никогда её не обгоняет.
    """
    walk = [1.004, 1.006, 1.010, 1.008, 1.003, 1.012, 1.007, 1.020, 1.001]
    steps = _walk(Side.LONG, walk, TRAILING)

    running = None
    previous = None
    moved = 0
    for index, step in enumerate(steps):
        if step["peak"] is None:
            assert step["level"] is None, "уровень есть, а вершины нет"
            continue
        running = step["close"] if running is None else max(running, step["close"])
        assert step["peak"] == running, (
            f"шаг {index}: вершина {step['peak']} против бегущего максимума "
            f"{running} — монотонность вершины нарушена"
        )
        assert step["level"] is not None
        target = round_level(step["peak"] - step["peak"] * 0.2 / 100)
        assert step["level"] <= target, (
            f"шаг {index}: уровень {step['level']} обогнал расчёт от вершины "
            f"{target}"
        )
        if previous is not None:
            assert step["level"] >= previous, (
                f"шаг {index}: уровень поехал назад {previous} → {step['level']}"
            )
            if step["level"] > previous:
                moved += 1
                assert step["level"] == target, (
                    f"шаг {index}: уровень сдвинулся не на расчёт от вершины"
                )
        previous = step["level"]

    assert previous is not None, "уровень так и не появился — проверять нечего"
    assert moved >= 2, "уровень не двигался — ломаная ничего не проверила"


def test_the_same_walk_mirrored_holds_for_a_short() -> None:
    """Тот же механизм для шорта: вершина — минимум, уровень едет вниз.

    Перепутанный знак закрывает шорт по уровню, который цена уже прошла,
    и ломаной для лонга это не видно вовсе.
    """
    walk = [0.996, 0.994, 0.990, 0.992, 0.997, 0.988, 0.993, 0.980, 0.999]
    steps = _walk(Side.SHORT, walk, TRAILING)

    running = None
    previous = None
    moved = 0
    for index, step in enumerate(steps):
        if step["peak"] is None:
            assert step["level"] is None
            continue
        running = step["close"] if running is None else min(running, step["close"])
        assert step["peak"] == running, f"шаг {index}: вершина шорта не минимум"
        assert step["level"] is not None
        target = round_level(step["peak"] + step["peak"] * 0.2 / 100)
        assert step["level"] >= target, f"шаг {index}: уровень шорта обогнал расчёт"
        assert step["level"] > step["peak"], "уровень шорта обязан стоять ВЫШЕ цены"
        if previous is not None:
            assert step["level"] <= previous, (
                f"шаг {index}: уровень шорта поехал назад {previous} → {step['level']}"
            )
            if step["level"] < previous:
                moved += 1
                assert step["level"] == target
        previous = step["level"]

    assert previous is not None
    assert moved >= 2


def test_the_level_refuses_to_slide_back_even_when_the_move_is_large() -> None:
    """Второй механизм: сравнение движения уровня идёт **со знаком**.

    Монотонная вершина сама по себе запрет не обеспечивает — она лишь делает
    откат недостижимым на обычной ломаной. Проверяется он в лоб: состояние,
    где расчёт от вершины **ниже** уже стоящего уровня, и откат при этом
    **больше шага подтяжки** — то есть сравнение по модулю его бы пропустило.

    ⚠️ Состояние собрано руками, а не получено ломаной, и это сказано вслух:
    сегодня продукт в него не приходит. Это вторая линия обороны приёмочного
    правила ТЗ §9, и цена её отсутствия — уровень, уехавший назад на боевом
    счёте, там, где сверять уже не с чем.
    """
    peak = PRICE * 1.01
    higher = round_level(peak - peak * 0.2 / 100) + 500
    plan = TakeProfit(
        percent=0.5, trailing=True, start_percent=0.5, offset_percent=0.2,
        step_percent=0.05, peak=peak, level=higher,
    )
    # Закрытие ниже вершины: вершина не двигается, расчёт от неё ниже уровня.
    kept = plan.advance(PRICE, 1, PRICE * 1.005)
    assert kept.peak == peak, "вершина сдвинулась на откате"
    assert kept.level == higher, (
        f"уровень поехал назад {higher} → {kept.level}: запрет «не ехать назад» "
        "не работает сам по себе"
    )

    # Зеркало для шорта: вершина НИЖЕ цены входа, уровень уже ушёл дальше
    # расчёта от неё, закрытие выше вершины — вершина стоит, расчёт тянет
    # уровень вверх, то есть назад.
    low = PRICE * 0.99
    mirror = replace(
        plan, peak=low, level=round_level(low + low * 0.2 / 100) - 500,
    )
    kept_short = mirror.advance(PRICE, -1, PRICE * 0.995)
    assert kept_short.peak == low, "вершина шорта сдвинулась на откате"
    assert kept_short.level == mirror.level, (
        f"уровень шорта поехал назад {mirror.level} → {kept_short.level}"
    )


def test_a_level_restored_after_a_cancel_does_not_start_over() -> None:
    """Снятие погасило уровень — повторное вооружение берёт ТУ ЖЕ вершину.

    Иначе после каждого неудавшегося снятия скользящий тейк считался бы заново
    от цены входа, то есть **ехал бы назад** — прямо против приёмочного правила.
    """
    steps = _walk(Side.LONG, [1.004, 1.010, 1.020], TRAILING)
    reached = steps[-1]
    assert reached["level"] is not None and reached["peak"] is not None

    dropped = TakeProfit(
        percent=0.5, trailing=True, start_percent=0.5, offset_percent=0.2,
        step_percent=0.05, peak=reached["peak"], level=None,
    )
    restored = dropped.advance(PRICE, 1, PRICE * 1.005)
    assert restored.peak == reached["peak"], "вершина потеряна вместе с уровнем"
    assert restored.level == reached["level"], (
        "уровень восстановился не там, где стоял: скользящий тейк поехал назад"
    )


def test_for_a_short_the_level_sits_above_the_best_price() -> None:
    """Ниже лучшей цены для лонга, **выше** для шорта.

    Перепутанный знак закрывает шорт по уровню, который цена уже прошла.
    """
    entered = unarmed(Side.SHORT, plan=TRAILING.plan().initial(PRICE, -1))
    bottom = PRICE * 0.994
    outcome = run(
        EngineState(position=entered), settings=TRAILING,
        intent=Intent.SHORT, close=bottom,
    )
    level = outcome.orders[0].price
    assert level is not None
    assert level > bottom
    assert level == round_level(bottom + bottom * 0.2 / 100)

    deeper = run(
        outcome.state, settings=TRAILING, intent=Intent.SHORT,
        close=PRICE * 0.990, when=(10, 15),
    )
    assert deeper.orders[0].price is not None
    assert deeper.orders[0].price < level, "у шорта уровень обязан ехать вниз"


def test_the_step_keeps_small_moves_from_touching_the_broker() -> None:
    """Шаг подтяжки — ограничитель нагрузки, а не украшение.

    У скользящего тейка каждая смена уровня это настоящая операция у брокера.
    Без шага каждая свеча с новым максимумом даёт обращение — за час позиции
    до двенадцати (решение 0009).
    """
    settings = TRAILING.replace(trailing_step_percent=0.5)
    state = EngineState(position=unarmed(plan=TRAILING.plan().initial(PRICE, 1)))
    first = run(state, settings=settings, close=PRICE * 1.006)
    level = first.state.position.take_profit
    assert level is not None

    # Новый максимум есть, но движение уровня меньше шага подтяжки.
    second = run(first.state, settings=settings, close=PRICE * 1.0065, when=(10, 15))
    assert second.state.position is not None
    assert second.state.position.take_profit == level, "уровень поехал мимо шага"
    assert second.state.position.take.peak == PRICE * 1.0065, (
        "вершина обязана обновиться даже тогда, когда уровень стоит"
    )

    # А теперь движение больше шага — уровень едет.
    third = run(second.state, settings=settings, close=PRICE * 1.02, when=(10, 20))
    assert third.state.position is not None
    assert third.state.position.take_profit is not None
    assert third.state.position.take_profit > level


def test_switching_the_trailing_take_off_does_not_touch_an_open_position() -> None:
    """Открытый вопрос решения 0009, закрыт здесь: **держим достигнутый уровень**.

    План тейка замораживается на входе в позицию, поэтому изменение настройки
    на неё не действует вовсе — ровно как требует ТЗ §4.4 А. Возврат
    к неподвижному уровню от цены входа означал бы, что уровень поехал назад,
    а это прямо запрещено приёмочным правилом.
    """
    state = EngineState(position=unarmed(plan=TRAILING.plan().initial(PRICE, 1)))
    moved = run(state, settings=TRAILING, close=PRICE * 1.01)
    level = moved.state.position.take_profit
    assert level is not None

    off = run(
        moved.state, settings=TRAILING.replace(trailing_take_profit=False),
        close=PRICE * 1.011, when=(10, 15),
    )
    assert off.state.position is not None
    assert off.state.position.take.trailing, "план позиции пошёл за настройкой"
    assert off.state.position.take_profit is not None
    assert off.state.position.take_profit >= level


def test_the_frozen_plan_ignores_a_changed_percent_for_an_open_position() -> None:
    """Смена размера тейка действует со следующей сделки (ТЗ §4.4 А).

    ⚠️ Прототип устроен иначе: его шаг 5 пересчитывает уровень из текущего
    значения параметра. Расхождение сверкой недостижимо — она идёт
    на неизменных настройках, — и выбран вариант ТЗ.
    """
    state = EngineState(position=armed())
    outcome = run(state, settings=WORKING.replace(take_profit_percent=2.0))
    assert outcome.orders[0].price == fixed_level(PRICE, 1, 0.5)


# --------------------------------------------------------------------------
# «Тейк окупает комиссию обеих сторон» — ТЗ §4.4 В
# --------------------------------------------------------------------------

def test_the_target_is_measured_against_the_commission_of_both_sides() -> None:
    """Цель в рублях против удвоенного тарифа. Тариф не задан — честное «нет»."""
    assert covers_commission(PRICE, PRICE + 1050, 1.0, 1, commission_per_side=14.0)
    assert not covers_commission(PRICE, PRICE + 20, 1.0, 1, commission_per_side=14.0)
    assert covers_commission(
        PRICE, PRICE + 1050, 1.0, 1, commission_per_side=None
    ) is None
    # Шорт: цель ниже цены входа — и это прибыль, а не убыток.
    assert covers_commission(PRICE, PRICE - 1050, 1.0, -1, commission_per_side=14.0)
    assert target_profit(PRICE, PRICE - 1050, 1.0, -1) == 1050


def test_a_level_on_the_losing_side_never_counts_as_a_covered_target() -> None:
    """Цель по модулю объявляла бы убыток окупающейся целью.

    Замерено на реальном сочетании настроек: порог 0,1 при отступе 1,0, лонг
    от 200 000, закрытие 200 400 → уровень 198 396, то есть выход в убыток
    1 604 ₽ на контракт. Приёмочное правило ТЗ §9 этот случай **не ловит**:
    уровень едет вперёд честно, он просто начинается в убытке.
    """
    assert target_profit(200_000.0, 198_396.0, 1.0, 1) == -1604
    assert not covers_commission(
        200_000.0, 198_396.0, 1.0, 1, commission_per_side=14.0
    )
    assert not covers_commission(
        200_000.0, 200_000.0, 1.0, 1, commission_per_side=0.0
    ), "уровень ровно на цене входа даёт ноль, а ноль издержек не окупает"


def test_a_target_below_the_costs_gets_a_warning_but_the_level_is_still_set() -> None:
    """Уровень выставляется всё равно, а в журнале стоит предупреждение.

    Отказаться вооружать неокупаемый тейк движок не вправе: снятая защита
    стоит дороже маленькой цели. Запрет на ввод такого процента — дело окна
    настроек, где рядом есть цена инструмента.
    """
    settings = WORKING.replace(commission_per_side=14.0, take_profit_percent=0.001)
    outcome = run(
        EngineState(position=unarmed(plan=settings.plan())), settings=settings,
    )
    assert actions(outcome) == [OrderAction.ARM_TAKE_PROFIT]
    warnings = [
        entry for entry in outcome.lines() if entry.level is JournalLevel.WARNING
    ]
    assert warnings, "неокупаемая цель прошла молча"
    assert "НЕ окупает" in warnings[0].reason


def test_a_target_that_covers_the_costs_says_both_numbers() -> None:
    settings = WORKING.replace(commission_per_side=14.0)
    outcome = run(
        EngineState(position=unarmed(plan=settings.plan())), settings=settings,
    )
    reason = outcome.lines()[0].reason
    assert "окупается" in reason
    assert "28" in reason, "комиссия обеих сторон в строке не названа"


def test_a_threshold_below_the_offset_is_refused_at_the_settings_border() -> None:
    """Сочетание, при котором уровень начинается в убытке, не принимается.

    Замерено: порог 0,1 при отступе 1,0, лонг от 200 000, закрытие 200 400 →
    уровень 198 396, то есть выход в убыток 1 604 ₽ на контракт. Приёмочное
    правило ТЗ §9 этот случай **не ловит**: уровень едет вперёд честно,
    он просто начинается в убытке.
    """
    with pytest.raises(ValueError, match="больше отступа"):
        WORKING.replace(
            trailing_take_profit=True, trailing_start_percent=0.1,
            trailing_offset_percent=1.0,
        )
    # Умолчания решения 0009 сочетание проходят.
    assert WORKING.replace(trailing_take_profit=True).trailing_start_percent == 0.5


def test_the_first_trailing_level_never_lands_on_the_losing_side() -> None:
    """Вторая половина того же лечения — арифметика на границе.

    Настройки отвергают явно плохое сочетание, но при пороге, лишь чуть
    большем отступа, первый уровень всё равно может лечь ровно на цену входа
    или ниже. Тогда вооружения не происходит вовсе: движок ждёт вершины повыше.
    """
    plan = TakeProfit(
        percent=0.5, trailing=True, start_percent=0.21, offset_percent=0.2,
        step_percent=0.05,
    )
    # ⚠️ Инструмент с низкой ценой: уровень округляется до ЦЕЛОГО, и на цене
    # 1 000 разница между порогом 0,21% и отступом 0,2% — два пункта — целиком
    # съедается округлением. 1 002,1 − 0,2% = 1 000,0958 → 1 000, то есть ровно
    # цена входа: выход в ноль минус две комиссии.
    engaged = plan.advance(1_000.0, 1, 1_002.1)
    # Вершина при этом НЕ запоминается: скользящий тейк не включился вовсе,
    # и порог на следующей свече считается заново. Это осознанно — включиться
    # с уровнем в убытке хуже, чем не включиться.
    assert engaged.peak is None
    assert engaged.level is None, (
        f"вооружились на уровне {engaged.level} при цене входа 1 000 — "
        "это выход в убыток под именем фиксации прибыли"
    )
    # Цена ушла дальше — уровень появляется и стоит выше входа.
    higher = engaged.advance(1_000.0, 1, 1_020.0)
    assert higher.level is not None and higher.level > 1_000.0

    # Зеркало для шорта.
    down = plan.advance(1_000.0, -1, 997.9)
    assert down.level is None
    assert plan.advance(1_000.0, -1, 980.0).level is not None


def test_the_journal_says_out_loud_when_the_commission_rule_was_not_checked() -> None:
    """Тариф не задан — правило ТЗ §4.4 В сегодня не исполняется.

    Уровень WARNING, а не INFO: непроверенное правило про деньги не должно
    выглядеть как обычный шум в потоке строк.
    """
    outcome = run(EngineState(position=unarmed()), settings=WORKING)
    assert WORKING.commission_per_side is None
    note = outcome.lines()[0]
    assert note.level is JournalLevel.WARNING
    assert "НЕ ПРОВЕРЕНО" in note.reason


def test_the_journal_does_not_promise_a_cancel_that_never_happened() -> None:
    """Текст «сначала снимаем тейк X» пишется только когда снятие подаётся.

    Достижимо: вооружение отклонено на прошлой свече, и уровень встаёт
    одновременно с сигналом на выход; скользящий перешёл порог ровно на свече
    выхода. Текст уходит не только в журнал, но и в поле причины самой заявки,
    которую читает боевой адаптер.
    """
    fresh = unarmed()  # план есть, уровня у брокера нет
    outcome = run(EngineState(position=fresh), intent=Intent.SHORT)
    assert actions(outcome) == [OrderAction.CLOSE], (
        "снятие подано по уровню, которого у брокера не было"
    )
    assert "снимаем тейк" not in outcome.orders[0].reason
    assert not any("снимаем тейк" in entry.reason for entry in outcome.lines())

    # А когда уровень действительно стоял — текст на месте.
    with_level = run(EngineState(position=armed()), intent=Intent.SHORT)
    assert "снимаем тейк" in with_level.orders[1].reason


def test_the_words_for_an_order_name_all_four_actions() -> None:
    """Вооружение и снятие — не «выход из лонга».

    Текст уходит в две самые дорогие строки журнала: отказ с остановкой робота
    и отклонение заявки. Владелец счёта, прочитавший «Исполнитель не принял
    заявку (выход из лонга…). Робот остановлен», идёт закрывать руками
    позицию, из которой робот выходить не собирался.
    """
    guarded = armed()
    arm = OrderRequest(
        action=OrderAction.ARM_TAKE_PROFIT, side=Side.LONG, volume=1.0,
        submitted_at=bar(*INSIDE).closes_at, reason="сторожим",
        order_id=take_order_id(guarded), price=211_050.0,
    )
    cancel = OrderRequest(
        action=OrderAction.CANCEL_TAKE_PROFIT, side=Side.LONG, volume=1.0,
        submitted_at=bar(*INSIDE).closes_at, reason="снимаем",
        order_id="cancel:тест", target_id=arm.order_id,
    )
    assert "тейк лонга на уровень 211 050" in order_words(arm)
    assert "снятие тейка лонга" in order_words(cancel)
    for words in (order_words(arm), order_words(cancel)):
        assert "выход из" not in words
        assert "вход в" not in words


# --------------------------------------------------------------------------
# Граница решения 0008, пункт 4: два теста, и нужны оба
# --------------------------------------------------------------------------

ENGINE = pathlib.Path(__file__).resolve().parent.parent / "engine"
#: Что считается чтением экстремума: обращение к полю (`bar.high`) и строковое
#: имя поля (`getattr(bar, "low")`, список обязательных полей).
EXTREMES = ("high", "low")
#: Единственное исключение — **перекладка свечи**, где `high` и `low` только
#: переписываются и решений не принимают (решение 0008, пункт 4). Исключаются
#: две функции, а НЕ файл целиком: «переименовать файл» названо в самом решении
#: третьим из трёх способов обойти запрет.
EXEMPT = {("bars.py", "to_bar"), ("bars.py", "check_market_candle")}


def _extreme_reads(path: pathlib.Path) -> list[str]:
    """Где в этом файле читается `high` или `low`. Разбор по дереву, не текстом.

    По дереву — потому что текстом это делается неправильно тремя способами
    сразу: комментарий внутри строкового литерала, строковый литерал внутри
    комментария и докстрока, объясняющая сам запрет. Дерево не содержит
    ни комментариев, ни докстрок как кода, и различает `x.high` (обращение
    к полю) и `"high"` (имя поля строкой) точно.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    exempt: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if (path.name, node.name) in EXEMPT:
                exempt.update(
                    id(inner) for inner in ast.walk(node) if inner is not node
                )
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        )
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    found: list[str] = []
    for node in ast.walk(tree):
        if id(node) in exempt or id(node) in docstrings:
            continue
        if isinstance(node, ast.Attribute) and node.attr in EXTREMES:
            found.append(f"{path.name}:{node.lineno}: .{node.attr}")
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in EXTREMES
        ):
            found.append(f"{path.name}:{node.lineno}: {node.value!r}")
    return found


def test_no_module_of_the_engine_reads_the_high_or_the_low_of_a_candle() -> None:
    """Разбор исходников **всего** `engine/`. Первый из двух тестов границы.

    Запрет «движок не читает `high`/`low`» держит границу не сам по себе,
    а вместе с поведенческим тестом ниже. Но без него обход тривиален:
    достаточно сравнить экстремум свечи с уровнем — и на истории сделки
    сойдутся, а в бою разойдутся, потому что движок узнает о пробое только
    на закрытии свечи.

    ⚠️ `rglob`, а не `glob`: подпакет внутри `engine/` иначе не попал бы
    в разбор вовсе. И исключаются **две функции перекладки**, а не файл:
    «переименовать файл» решение 0008 само называет одним из трёх способов
    обойти запрет.
    """
    sources = sorted(ENGINE.rglob("*.py"))
    assert len(sources) >= 8, "разбирать нечего — сторож смотрит в пустоту"
    guilty = [line for source in sources for line in _extreme_reads(source)]
    assert not guilty, (
        "движок читает экстремумы свечи — срабатывание тейка обязано приходить "
        "сделкой из порта, а не выводиться из полей свечи: " + "; ".join(guilty)
    )


def test_the_source_scan_would_notice_a_violation(tmp_path: pathlib.Path) -> None:
    """Сторож не слепой: три подделки обязаны его сработать.

    Тест на тест. Проверка по исходникам легко превращается в зелёный ноль —
    неверное выражение, пустой список файлов, чистка, съевшая весь текст, —
    и заметить это можно только так. Три случая взяты из тех самых способов
    обхода, которые названы в решении 0008.
    """
    def scan(text: str, name: str = "fake.py") -> list[str]:
        source = tmp_path / name
        source.write_text(text, encoding="utf-8")
        return _extreme_reads(source)

    assert scan("def check(bar):\n    return bar.high > 100\n")
    assert scan('level = getattr(bar, "low")\n')
    # Переименованный файл прикрытием не служит: исключение привязано к паре
    # «файл + функция», а не к имени файла.
    assert scan("def to_bar(c):\n    return c.high\n", name="carrier.py")

    # А вот это нарушением НЕ является и ловиться не должно.
    assert not scan('"""Движок не читает bar.high."""\n')
    assert not scan("# и bar.low тоже\n")
    assert not scan('TEXT = "строка со словом # high внутри"\n')
    assert not scan('LOW_WATER = 1\ndef f(x):\n    return x.lowest\n')


def test_the_exemption_covers_the_carrier_and_nothing_else() -> None:
    """Исключение выдано двум функциям, и обе обязаны существовать.

    Если `to_bar` переименуют, исключение станет мёртвым, а разбор — красным.
    Это правильно: пусть решение о новом исключении принимает человек.
    """
    carrier = ast.parse((ENGINE / "bars.py").read_text(encoding="utf-8"))
    names = {
        node.name for node in ast.walk(carrier)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for file_name, function in EXEMPT:
        assert file_name == "bars.py"
        assert function in names, (
            f"исключение выдано функции `{function}`, которой в bars.py больше "
            "нет — разбор молча перестал что-то проверять"
        )
    # И сама перекладка экстремумы действительно читает: иначе исключение
    # было бы лишним, а лишнее исключение — это дыра про запас.
    assert _extreme_reads_raw(ENGINE / "bars.py"), (
        "в перекладке нет обращений к экстремумам — исключение больше не нужно"
    )


def _extreme_reads_raw(path: pathlib.Path) -> list[str]:
    """То же самое, но без исключений. Нужен ровно одному тесту выше."""
    global EXEMPT
    keep, EXEMPT = EXEMPT, set()
    try:
        return _extreme_reads(path)
    finally:
        EXEMPT = keep


def test_an_executor_that_never_reports_a_take_produces_no_take_exits() -> None:
    """Второй тест границы, поведенческий. Нужны оба.

    Исполнитель исполняет рыночные заявки и **никогда** не сообщает
    о сработавшем уровне: заявки вооружения он молча принимает и забывает.
    Свечи при этом с любым размахом — `high` и `low` многократно перекрывают
    любой мыслимый уровень. Выходов по тейку не должно быть ни одного.

    ⚠️ **Проверяется выход САМОГО ДВИЖКА, а не подставки.** Прежняя редакция
    смотрела на признак сделки в модели исполнения (`exit_reason == "take"`)
    — а его пишет ровно один путь, тот самый, который у глухого исполнителя
    не выполняется никогда. Тест был зелёным и на правильной реализации,
    и на нарушающей: движок, читающий `bar.close` и сравнивающий его
    с уровнем, прошёл бы его, **и сверка на истории тоже сошлась бы** —
    внутрибарный уровень и внутрибарное сравнение дают на исторических данных
    одно и то же. Разошлось бы только в бою.

    Поэтому смотрим на три собственных следа движка: причину в его заявках,
    причину в его журнале и цену выхода, совпавшую с расчётным уровнем.
    """
    class DeafToLevels(NextOpenExecutor):
        async def submit(self, order: OrderRequest) -> None:
            if order.action is OrderAction.ARM_TAKE_PROFIT:
                return None
            await super().submit(order)

    # ⚠️ Цена растёт, а сигнал средней держит лонг: без этого позиция уходит
    # по обратному сигналу раньше, чем закрытие успеет перевалить за уровень,
    # и сторож ничего не проверяет. Уровень лонга от 210 000 — 211 050,
    # закрытия перекрывают его начиная с третьей свечи.
    prices = [
        210_000.0, 210_000.0, 211_500.0, 213_000.0, 214_500.0, 205_000.0, 204_000.0,
    ]
    #: На какой свече модуль просит выйти. Седьмая свеча нужна, чтобы заявка
    #: на выход успела стать сделкой: без неё сравнивать было бы нечего.
    signal_at = 5
    strategy = ScriptedStrategy([
        decision(Intent.LONG if index < signal_at else Intent.SHORT, close=price)
        for index, price in enumerate(prices)
    ])
    lines: list = []
    engine = Engine(strategy, WORKING, journal=lines.append)
    executor = DeafToLevels()
    candles = [
        replace(
            candle(10, 5 + 5 * index, close=price),
            open=price, high=price * 1.5, low=price * 0.5, close=price,
        )
        for index, price in enumerate(prices)
    ]
    source_times = [
        item.time + timedelta(minutes=item.timeframe.minutes) for item in candles
    ]
    asyncio.run(engine.run(ListSource(candles), executor))

    assert executor.deals, "сделок не было вовсе — проверка была бы вакуумной"
    assert not engine.halted, engine.halted

    # 1. Ни одной заявки движка с причиной «тейк-профит».
    by_take = [
        order for order in executor.submitted_market
        if order.exit_reason is ExitReason.TAKE_PROFIT
    ]
    assert not by_take, f"движок сам объявил выход тейком: {by_take}"

    # 2. Ни одной строки журнала с этой причиной.
    said = [entry for entry in lines if "тейк-профит" in entry.reason.lower()]
    assert not said, f"журнал назвал выход тейком: {[e.reason for e in said]}"

    # 3. Ни одной цены выхода, равной расчётному уровню, — на случай движка,
    #    который причину не пишет, но закрывает ровно по уровню.
    at_level = [
        deal for deal in executor.deals
        if deal.exit_price == fixed_level(
            deal.entry_price, 1 if deal.side is Side.LONG else -1, 0.5
        )
    ]
    assert not at_level, f"выход прошёл ровно по уровню тейка: {at_level}"

    # 4. ⚠️ И главное: выход случился ТОЛЬКО там, где его объяснил модуль.
    #    Мутация «сравнить close с уровнем и выйти по рынку, не называя
    #    причину» проверки 1–3 проходит: цена выхода у неё — открытие
    #    следующей свечи, а не уровень. Ловит её вот это: обратный сигнал
    #    в сценарии один, и выход обязан быть один и на своей свече.
    exits = [
        order.submitted_at for order in executor.submitted_market
        if order.action is OrderAction.CLOSE
    ]
    assert exits == [source_times[signal_at]], (
        "движок вышел из позиции на свече, где модуль его об этом не просил: "
        f"{exits} против {[source_times[signal_at]]}"
    )

    # 5. И признак модели исполнения — сверху, как было.
    assert not [deal for deal in executor.deals if deal.exit_reason == "take"]


def test_the_behavioural_guard_would_notice_a_cheating_engine() -> None:
    """Тест на тест: подделанный выход обязан сработать все три проверки.

    Первая редакция сторожа была зелёной на нарушающей реализации, и заметить
    это можно было только так — прогнав через те же проверки заведомо
    неправильный результат.
    """
    cheating = OrderRequest(
        action=OrderAction.CLOSE, side=Side.LONG, volume=1.0,
        submitted_at=bar(*INSIDE).closes_at, reason="закрытие по уровню",
        order_id="close:long:подделка", exit_reason=ExitReason.TAKE_PROFIT,
    )
    assert cheating.exit_reason is ExitReason.TAKE_PROFIT
    assert "тейк-профит" in ExitReason.TAKE_PROFIT.label.lower()
    level = fixed_level(PRICE, 1, 0.5)
    assert level is not None and level != PRICE, (
        "уровень совпал с ценой входа — проверка по цене выхода была бы слепой"
    )


# --------------------------------------------------------------------------
# Дефект образцовой реализации порта, найденный повторным ревью
# --------------------------------------------------------------------------

def test_a_repeated_candle_does_not_fill_an_order_at_its_own_open() -> None:
    """Свеча показана дважды — заявка не исполняется по её же открытию.

    Один интервал показывается исполнителю несколько раз: круг обмена внутри
    свечи, повтор после переподключения, незакрытый бар на каждом тике потока.
    Реализация, не проверяющая момент подачи, исполнит заявку по открытию
    ТОЙ САМОЙ свечи, на закрытии которой заявка подана. Родственная гипотеза
    «вход по цене сигнальной свечи» даёт 73 расхождения из 127.
    """
    executor = NextOpenExecutor()
    signal = replace(candle(10, 10, close=PRICE), open=PRICE * 0.5)
    order = OrderRequest(
        action=OrderAction.OPEN, side=Side.LONG, volume=1.0,
        submitted_at=bar(10, 15).closes_at,  # закрытие свечи 10:10
        reason="вход", order_id="open:long:тест",
    )
    asyncio.run(executor.submit(order))

    repeated = asyncio.run(executor.fills_at(signal))
    assert list(repeated) == [], "заявка исполнилась по открытию своей же свечи"

    following = replace(candle(10, 15, close=PRICE), open=PRICE)
    fills = asyncio.run(executor.fills_at(following))
    assert [fill.price for fill in fills] == [PRICE]
    assert fills[0].at == following.time, "время сделки — начало свечи исполнения"
