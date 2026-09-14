"""Сторона касания сторожимого уровня: `B-046`, уровень позади рынка.

Что здесь стережётся
--------------------
Заявка вооружения несёт **уровень и сторону позиции**. До 10.09.2026 она
не несла того, **с какой стороны до уровня доходят**, и это додумывал
исполнитель: «у лонга уровень выше входа, значит до него дорастают снизу».
Для неподвижного тейка догадка верна. Для скользящего — нет: он стоит
на отступе **позади** лучшей достигнутой цены, то есть это стоп, и до него
откатываются с обратной стороны. Условие «`high` дотянулся вверх» для уровня,
лежащего ниже рынка, истинно всегда — уровень срабатывал на первой же свече
после вооружения.

Замер 17.06–04.09.2026: 28 вооружений скользящего тейка, **0 подтяжек**, жизнь
уровня **0 свечей у всех 28**, и **4 выхода из 28 по цене, которой в свече
не было**. У неподвижного тейка — 0 таких из 32.

Три разных ловца, и они не заменяют друг друга
-----------------------------------------------
1. **Поведение пары движок+исполнитель** — главный. Неверная сторона касания
   не ловится ни сверкой с прототипом (в ней трейлинга нет), ни сторожем
   «цена, которой не было» (24 сработки из 28 были внутри размаха своей
   свечи), ни проверкой контракта (она видит заполненное поле и не знает,
   верное ли). Ловится только тем, что уровень **не срабатывает** на свече,
   до него не дошедшей.
2. **Сторож «цена, которой не было»** — шире этой задачи: он про любой
   будущий дефект модели исполнения, а не про трейлинг.
3. **Контракт** — единственное, что достаётся боевому адаптеру: без стороны
   касания заявка не собирается вовсе.

⚠️ **Заявка вооружения здесь собирается производственным `_arm_order`.**
Оснастка, выводящая сторону касания из стороны позиции, вернула бы старое
правило внутрь тестов и сделала бы их слепыми ровно к той ошибке, ради
которой они написаны. Собранная руками заявка встречается ниже дважды,
и оба раза это **нарочная подделка** — проверка сторожа и проверка контракта.
"""

from __future__ import annotations

import asyncio
import dataclasses
from dataclasses import replace
from datetime import datetime

import pytest

from backtest import Costs, ExecutionModel, HistoryExecutor
from engine import (
    ExitReason,
    Fill,
    LevelTouch,
    OrderAction,
    OrderRequest,
    Position,
    Side,
    TakeProfit,
)
from engine.contracts import FIELD_RULES
from engine.pipeline import _arm_order
from tests.engine_helpers import MSK, candle

#: Цена входа синтетических сценариев. Круглая: числа здесь не про арифметику
#: уровня, а про то, срабатывает он или нет.
ENTRY = 100.0


def moment(hour: int, minute: int) -> datetime:
    """Начало пятиминутки этого дня МСК."""
    return datetime(2026, 6, 19, hour, minute, tzinfo=MSK)


def market(hour: int, minute: int, *, open_: float, high: float, low: float, close: float):
    """Свеча с заданным размахом. Имя `open_` — `open` занято встроенной."""
    return replace(
        candle(hour, minute), open=open_, high=high, low=low, close=close,
    )


def run(model: ExecutionModel, *steps) -> list[list[Fill]]:
    """Проиграть сценарий: заявки подаются, свечи показываются.

    Возвращает ответ порта на каждую показанную свечу, по одному на свечу.
    """
    answers: list[list[Fill]] = []

    async def scenario() -> None:
        for step in steps:
            if isinstance(step, OrderRequest):
                await model.submit(step)
            else:
                answers.append(list(await model.fills_at(step)))

    asyncio.run(scenario())
    return answers


def entering(side: Side, at: datetime) -> OrderRequest:
    """Рыночный вход. Нужен, чтобы у сторожа была **своя нога**."""
    return OrderRequest(
        action=OrderAction.OPEN, side=side, volume=1.0, submitted_at=at,
        reason="вход", order_id=f"open:{side.value}",
    )


def holding(side: Side, level: float, *, trailing: bool) -> Position:
    """Открытая позиция с уже посчитанным уровнем.

    Параметры плана заморожены на входе (ТЗ §4.4 А), поэтому здесь важен
    ровно один: **скользящий уровень или неподвижный**. Из него и стороны
    позиции движок выводит сторону касания.
    """
    plan = TakeProfit(
        percent=0.5, trailing=trailing, start_percent=0.2, offset_percent=0.1,
        step_percent=0.01, level=level, peak=ENTRY if trailing else None,
    )
    return Position(
        side=side, volume=1.0, entry_price=ENTRY,
        entry_time=moment(10, 10), take=plan,
    )


# ---------------------------------------------------------------------------
# Главный ловец: поведение пары движок + исполнитель
# ---------------------------------------------------------------------------

def test_a_trailing_level_under_a_long_waits_until_the_price_falls_to_it() -> None:
    """Скользящий уровень лонга стоит ПОЗАДИ рынка, и рост его не задевает.

    Ровно этот случай и был `B-046`: уровень 99 при цене 100, свеча сходила
    вверх до 101 — по старому правилу «у лонга сторожим `high`» уровень
    срабатывал сразу, хотя цена к нему даже не приближалась.
    """
    model = ExecutionModel()
    run(
        model,
        entering(Side.LONG, moment(10, 10)),
        market(10, 10, open_=ENTRY, high=ENTRY, low=ENTRY, close=ENTRY),
    )
    order = _arm_order(holding(Side.LONG, 99.0, trailing=True), moment(10, 15), 99.0)
    assert order.touch is LevelTouch.FALL, "движок назвал не ту сторону касания"

    away, reached = run(
        model,
        order,
        market(10, 15, open_=ENTRY, high=101.0, low=99.5, close=100.5),
        market(10, 20, open_=ENTRY, high=100.5, low=98.5, close=99.0),
    )
    assert away == [], (
        "уровень позади рынка сработал на движении ВВЕРХ: цена до него "
        "не доходила, низ свечи 99,5 выше уровня 99"
    )
    assert [fill.price for fill in reached] == [99.0]


def test_a_trailing_level_over_a_short_waits_until_the_price_rises_to_it() -> None:
    """Симметрия: скользящий уровень шорта стоит ВЫШЕ рынка, и падение его не задевает.

    Старое правило «у шорта сторожим `low`» здесь срабатывало точно так же
    сразу: низ любой свечи ниже уровня, лежащего над рынком.
    """
    model = ExecutionModel()
    run(
        model,
        entering(Side.SHORT, moment(10, 10)),
        market(10, 10, open_=ENTRY, high=ENTRY, low=ENTRY, close=ENTRY),
    )
    order = _arm_order(holding(Side.SHORT, 101.0, trailing=True), moment(10, 15), 101.0)
    assert order.touch is LevelTouch.RISE, "движок назвал не ту сторону касания"

    away, reached = run(
        model,
        order,
        market(10, 15, open_=ENTRY, high=100.5, low=99.0, close=99.5),
        market(10, 20, open_=ENTRY, high=101.5, low=99.5, close=101.0),
    )
    assert away == [], (
        "уровень впереди рынка сработал на движении ВНИЗ: цена до него "
        "не доходила, верх свечи 100,5 ниже уровня 101"
    )
    assert [fill.price for fill in reached] == [101.0]


@pytest.mark.parametrize(
    ("side", "level", "touch", "misses", "hits"),
    [
        (Side.LONG, 100.5, LevelTouch.RISE, (100.0, 100.4, 99.0), (100.0, 101.0, 99.5)),
        (Side.SHORT, 99.5, LevelTouch.FALL, (100.0, 101.0, 99.6), (100.0, 101.0, 99.0)),
    ],
    ids=["long", "short"],
)
def test_a_fixed_level_fires_on_the_first_bar_that_reaches_it(
    side: Side,
    level: float,
    touch: LevelTouch,
    misses: tuple[float, float, float],
    hits: tuple[float, float, float],
) -> None:
    """Неподвижный тейк ведёт себя как прежде — и это половина правки.

    Правка меняет поведение только скользящего уровня. Если бы она задела
    неподвижный, разошлась бы сверка с прототипом: 42 выхода из 127 идут
    по уровню, и сверяются **цены выхода**.
    """
    model = ExecutionModel()
    run(
        model,
        entering(side, moment(10, 10)),
        market(10, 10, open_=ENTRY, high=ENTRY, low=ENTRY, close=ENTRY),
    )
    order = _arm_order(holding(side, level, trailing=False), moment(10, 15), level)
    assert order.touch is touch

    away, reached = run(
        model,
        order,
        market(10, 15, open_=misses[0], high=misses[1], low=misses[2], close=misses[0]),
        market(10, 20, open_=hits[0], high=hits[1], low=hits[2], close=hits[0]),
    )
    assert away == [], "уровень сработал на свече, которая до него не дошла"
    assert [fill.price for fill in reached] == [level]


def test_the_shadow_executor_guards_a_trailing_level_the_same_way() -> None:
    """Наблюдение на живом потоке — та же модель исполнения, и это проверяется.

    `ShadowExecutor` наследует `HistoryExecutor` и своих правил исполнения
    не имеет ни одного. Свойство важно само по себе: расхождение наблюдения
    с прогоном по истории обязано означать расхождение **данных**, а не двух
    разных моделей. Значит и дефект `B-046` жил в наблюдении тоже.
    """
    from app.observe import ShadowExecutor

    model = ShadowExecutor()
    run(
        model,
        entering(Side.LONG, moment(10, 10)),
        market(10, 10, open_=ENTRY, high=ENTRY, low=ENTRY, close=ENTRY),
    )
    away, reached = run(
        model,
        _arm_order(holding(Side.LONG, 99.0, trailing=True), moment(10, 15), 99.0),
        market(10, 15, open_=ENTRY, high=101.0, low=99.5, close=100.5),
        market(10, 20, open_=ENTRY, high=100.5, low=98.5, close=99.0),
    )
    assert away == [], "в наблюдении уровень позади рынка сработал на росте"
    assert [fill.price for fill in reached] == [99.0]
    assert model.deals[-1].exit_reason is ExitReason.TAKE_PROFIT


# ---------------------------------------------------------------------------
# Сторож «цена, которой не было». Вход — цена на ПРЕДЫДУЩЕМ баре
# ---------------------------------------------------------------------------
#
# Три теста ниже показывают одну и ту же свечу — размах и открытие совпадают
# до цифры. Различает их **только прошлый бар**, и это не оформительский приём,
# а суть сторожа: проверка, собранная из тех же величин, что и проверяемое
# правило, зеленеет вместе с ним. Первая редакция сторожа была тождественна
# условию срабатывания и не могла упасть никогда.

#: Замер 25.06.2026, 10:15: выход записан по 226 437 при размахе свечи
#: 226 550 … 227 000. Цены, которой в свече не было.
MEASURED_LEVEL = 226_437.0


def gapping(hour: int, minute: int):
    """Свеча замера: вся выше уровня 226 437, открытие внутри своего размаха."""
    return market(
        hour, minute, open_=226_700.0, high=227_000.0, low=226_550.0, close=226_900.0,
    )


def gapped_down(hour: int, minute: int):
    """Свеча разрыва вниз: вся ниже уровня 226 437, открытие 226 300."""
    return market(
        hour, minute, open_=226_300.0, high=226_350.0, low=226_200.0, close=226_250.0,
    )


def approaching(close: float):
    """Предыдущий бар с заданным закрытием — единственный вход сторожа."""
    return market(10, 10, open_=close, high=close, low=close, close=close)


def _armed_over_the_measured_candle(
    model: ExecutionModel, previous: float, touch: LevelTouch
) -> OrderRequest:
    """Открыть ногу, показать прошлый бар и вооружить уровень замера."""
    order = _arm_order(
        holding(Side.LONG, MEASURED_LEVEL, trailing=True), moment(10, 15), MEASURED_LEVEL,
    )
    if touch is not order.touch:
        # ⚠️ Подделка нарочная: ровно это делал исполнитель до 10.09.2026 —
        # выводил `RISE` из того, что позиция лонговая.
        order = replace(order, touch=touch)
    run(model, entering(Side.LONG, moment(10, 10)), approaching(previous))
    return order


def test_a_deal_at_a_price_the_candle_never_had_is_a_defect_not_a_gap() -> None:
    """Уровень вне размаха своей свечи, разрыва не было — прогон обязан упасть.

    Дословная просьба владельца счёта: «цена сделки обязана лежать внутри
    размаха своей свечи». Здесь она нарушена: сделка записана по 226 437,
    а свеча ходила от 226 550 до 227 000. Прошлый бар закрылся **выше**
    уровня — значит рынок к нему сверху даже не подходил, и объяснить
    запись разрывом нечем.

    ⚠️ Проверка идёт **напрямую по `ExecutionModel.fills_at`**, а не через
    `Engine`: тот ловит любое исключение исполнителя и превращает его
    в остановку с фразой «Сделки исполнителя не получены», которая описывает
    не то, что случилось (`B-048`).
    """
    model = ExecutionModel()
    order = _armed_over_the_measured_candle(model, 226_800.0, LevelTouch.RISE)
    with pytest.raises(AssertionError, match="цене, которой в свече не было"):
        run(model, order, gapping(10, 15))


def test_the_same_candle_after_a_real_gap_is_not_a_defect() -> None:
    """Та же свеча, тот же уровень — но прошлый бар был НИЖЕ уровня.

    Рынок стоял под уровнем и открылся уже над ним: это настоящий разрыв,
    и прототип исполнял такое ровно по уровню. Сторож обязан молчать —
    иначе он объявляет дефектом рыночное событие.

    ⚠️ Свеча здесь **та же самая**, что в тесте выше, до цифры. Различает
    их только закрытие прошлого бара — величина, которой в условии
    срабатывания нет вовсе.
    """
    model = ExecutionModel()
    order = _armed_over_the_measured_candle(model, 226_400.0, LevelTouch.RISE)
    filled, = run(model, order, gapping(10, 15))
    assert [fill.price for fill in filled] == [MEASURED_LEVEL]
    assert model.gaps.rise == 1, "разрыв через уровень впереди рынка не посчитан"


def test_the_guard_stays_silent_when_there_is_no_previous_bar_to_judge_by() -> None:
    """Прошлого бара нет — судить не о чем, и сторож молчит.

    Случай не выдуманный: уровень, вооружённый **сделкой открытия**, помечен
    временем сделки и сторожится уже на своей же свече. Если эта свеча —
    самая первая, показанная исполнителю, прошлого бара у него нет вовсе.
    Слепое пятно названо вслух, а не закрыто догадкой: судить о разрыве
    не по чему, и объявлять дефектом нечего.
    """
    model = ExecutionModel()
    first = gapping(10, 10)
    opened, = run(model, entering(Side.LONG, moment(10, 10)), first)
    assert [fill.action for fill in opened] == [OrderAction.OPEN]

    order = replace(
        _arm_order(
            holding(Side.LONG, MEASURED_LEVEL, trailing=True),
            moment(10, 10), MEASURED_LEVEL,
        ),
        touch=LevelTouch.RISE,
    )
    filled, = run(model, order, first)
    assert [fill.price for fill in filled] == [MEASURED_LEVEL], (
        "сторож упал на первой же свече, о прошлом которой ему ничего не известно"
    )


def test_a_repeated_candle_does_not_become_its_own_previous_bar() -> None:
    """Один интервал показан дважды — прошлым баром он себе не становится.

    Контракт порта разрешает показывать интервал несколько раз: круг обмена
    внутри свечи, повтор после переподключения, незакрытый бар на каждом тике.
    Исполнитель, считающий прошлым баром **прошлый вызов**, после второго
    показа судил бы о разрыве по цене этой же свечи. Здесь это видно прямо:
    настоящий прошлый бар закрылся на 226 800, выше уровня, — разрыв вниз
    налицо; закрытие самой свечи 226 250 лежит ниже уровня, и по нему разрыва
    нет. Сторож обязан молчать.
    """
    model = ExecutionModel()
    order = _armed_over_the_measured_candle(model, 226_800.0, LevelTouch.FALL)
    same = gapped_down(10, 15)
    empty, = run(model, same)
    assert empty == [], "заявка вооружения ещё не подана, сделок быть не может"
    filled, = run(model, order, same)
    assert [fill.price for fill in filled] == [MEASURED_LEVEL]
    assert model.gaps.fall == 1


def test_slippage_carries_the_deal_out_of_the_range_and_the_guard_stays_silent() -> None:
    """Сторож стоит на уровне срабатывания, а не на цене сделки.

    Проскальзывание законно выносит цену сделки за размах свечи: у брокера
    сработавший стоп — рыночная заявка. Сторож, поставленный на цену сделки,
    падал бы на любом ненулевом проскальзывании.
    """
    model = ExecutionModel(costs=Costs(price_step=1.0, slippage_steps=2.0))
    run(
        model,
        entering(Side.LONG, moment(10, 10)),
        market(10, 10, open_=ENTRY, high=ENTRY, low=ENTRY, close=ENTRY),
    )
    filled, = run(
        model,
        _arm_order(holding(Side.LONG, 100.5, trailing=False), moment(10, 15), 100.5),
        market(10, 15, open_=100.2, high=101.0, low=100.0, close=100.8),
    )
    assert [fill.price for fill in filled] == [98.5], "проскальзывание не применилось"
    assert model.gaps.rise == 0 and model.gaps.fall == 0, (
        "цена сделки вне размаха принята за разрыв: сторож смотрит не на уровень"
    )


# ---------------------------------------------------------------------------
# Разрывы считаются раздельно по стороне касания
# ---------------------------------------------------------------------------

def test_a_gap_down_through_a_level_behind_the_market_is_counted_apart() -> None:
    """Разрыв вниз через стоп считается ОТДЕЛЬНО, и рядом стоят пункты.

    Знак ошибки разрыва противоположен по сторонам касания. Разрыв через
    уровень **впереди** рынка занижает результат — ошибка в пользу
    осторожности. Разрыв через уровень **позади** рынка его завышает: сделка
    записана по 226 437, а торги открылись на 226 300, и разницу отчёт
    приписал себе. Один общий счётчик сложил бы два знака в одно число
    и спрятал вторую половину — ту, которая врёт в нашу пользу.
    """
    model = ExecutionModel()
    order = _armed_over_the_measured_candle(model, 226_800.0, LevelTouch.FALL)
    assert order.touch is LevelTouch.FALL, "у скользящего уровня лонга касание сверху"

    filled, = run(model, order, gapped_down(10, 15))
    assert [fill.price for fill in filled] == [MEASURED_LEVEL]
    assert model.gaps.fall == 1
    assert model.gaps.rise == 0, "разрыв вниз посчитан в половине разрывов вверх"
    assert model.gaps.fall_points == pytest.approx(137.0), (
        "пункты, приписанные отчёту на разрыве, посчитаны неверно: "
        "уровень 226 437 минус открытие 226 300 на объём 1"
    )


def test_the_points_of_a_gap_become_rubles_by_the_price_of_a_point() -> None:
    """Пункты в рубли переводит тот, кто знает цену пункта.

    Базовая модель исполнения курса пункта не знает и знать не должна:
    у неё нет ни инструмента, ни настроек. Разделение обязанностей проверяется
    числом, а не описанием.
    """
    model = HistoryExecutor(ruble_per_point=2.0)
    order = _armed_over_the_measured_candle(model, 226_800.0, LevelTouch.FALL)
    run(model, order, gapped_down(10, 15))
    assert model.gaps.fall_points == pytest.approx(137.0)
    assert model.fall_gap_rubles == pytest.approx(274.0)


# ---------------------------------------------------------------------------
# Контракт: без стороны касания заявка не собирается
# ---------------------------------------------------------------------------

def test_arming_a_level_without_a_touch_side_is_refused() -> None:
    """Единственное, что достаётся боевому адаптеру, — контракт.

    Тестом боевой путь не ловится: `OrderExecutor` не написан. Ловится
    он тем, что заявка без стороны касания не собирается вовсе, и отказ
    называет цену ошибки — у брокера это стоп-заявка не в ту сторону.
    """
    with pytest.raises(ValueError, match="без стороны касания"):
        OrderRequest(
            action=OrderAction.ARM_TAKE_PROFIT, side=Side.LONG, volume=1.0,
            submitted_at=moment(10, 15), reason="сторожим",
            order_id="take:long:тест", price=100.5,
        )


@pytest.mark.parametrize(
    "action",
    [OrderAction.OPEN, OrderAction.CLOSE, OrderAction.CANCEL_TAKE_PROFIT],
    ids=lambda action: action.value,
)
def test_an_order_without_a_level_may_not_carry_a_touch_side(action: OrderAction) -> None:
    """Сторожимого уровня нет — касаться нечего.

    Поле, разрешённое где попало, через месяц заполняют «на всякий случай»,
    и первый же читатель принимает его за осмысленное.
    """
    rest: dict[str, object] = {"touch": LevelTouch.RISE}
    if action is OrderAction.CLOSE:
        rest["exit_reason"] = ExitReason.SIGNAL
    if action is OrderAction.CANCEL_TAKE_PROFIT:
        rest["target_id"] = "take:long:тест"
    with pytest.raises(ValueError, match="задана сторона касания"):
        OrderRequest(
            action=action, side=Side.LONG, volume=1.0,
            submitted_at=moment(10, 15), reason="проверка",
            order_id="тест", **rest,  # type: ignore[arg-type]  # поля различаются по действию
        )


def test_every_optional_field_of_an_order_has_a_row_in_the_table() -> None:
    """Таблица правил полна — иначе новое поле молча не проверяется.

    Ровно ради этого правила поля и сведены в таблицу: рукописная цепочка
    `if` требует вспомнить два места, проверку и текст отказа, и забытое
    не падает никогда. Признак «поле необязательное» — значение по умолчанию:
    обязательные поля заявки есть у всех четырёх действий.
    """
    optional = {
        item.name for item in dataclasses.fields(OrderRequest)
        if item.default is not dataclasses.MISSING
        or item.default_factory is not dataclasses.MISSING
    }
    ruled = {rule.field for rule in FIELD_RULES}
    assert optional, "у заявки не нашлось ни одного необязательного поля — разбор сломан"
    assert optional - ruled == set(), (
        "поле заявки без строки в таблице правил: "
        f"{sorted(optional - ruled)}. Оно не проверяется ничем"
    )
    assert ruled - optional == set(), (
        f"в таблице правил названо несуществующее поле: {sorted(ruled - optional)}"
    )
