"""Модель исполнения для прогона по истории: правила прототипа и издержки.

Проверяется `backtest/execution.py` — реализация порта «исполнитель заявок».
Ошибка в любом её правиле меняет **все** сделки сразу, а не одну:

* заявка исполняется по `open` следующей свечи;
* уровень тейка сторожится **внутри** бара и исполняется ровно по уровню —
  единственное событие прототипа не на закрытии свечи (PROTOTYPE.md §5);
* заявка не исполняется свечой, начавшейся раньше её подачи;
* уровень **движется** вместе с вооружением, а не застывает на первом.

Издержки проверяются здесь же, потому что за них отвечает та же модель:
комиссия никогда не подставляется нулём, проскальзывание всегда **против**
позиции, а по умолчанию его ноль — как считал прототип.

⚠️ Числа прототипа этот файл не воспроизводит и воспроизводить не должен: его
дело — правила по отдельности. Сверка сделка в сделку живёт
в `tests/test_engine_reference_parity.py`, и одно другое не заменяет.
"""

from __future__ import annotations

import asyncio
import pathlib
import re
from dataclasses import replace
from datetime import datetime

import pytest

from backtest import (
    BROKER_FEE_PER_CONTRACT,
    Costs,
    ExecutionModel,
    HistoryExecutor,
    OpenLeg,
    Tariff,
    selling,
)
from engine import (
    ExecutionRefused,
    ExitReason,
    Fill,
    MarketCandle,
    OrderAction,
    OrderRequest,
    Refusal,
    Side,
)
from tests.engine_helpers import MSK, candle

#: Закрытие свечи 10:05 — момент, которым движок помечает заявку этой свечи.
DECIDED_AT = datetime(2026, 6, 19, 10, 10, tzinfo=MSK)


def order(
    action: OrderAction,
    side: Side,
    at: datetime = DECIDED_AT,
    **rest: object,
) -> OrderRequest:
    """Заявка с обязательными полями. Имя выводится из содержания, как у движка."""
    if action is OrderAction.CLOSE:
        rest.setdefault("exit_reason", ExitReason.SIGNAL)
    return OrderRequest(
        action=action, side=side, volume=1.0, submitted_at=at,
        reason="проверка", order_id=f"{action.value}:{side.value}:{at.isoformat()}",
        **rest,  # type: ignore[arg-type]  # поля заявки различаются по действию
    )


def arm(level: float, side: Side = Side.LONG, at: datetime = DECIDED_AT) -> OrderRequest:
    """Вооружение уровня. Имя **устойчиво**: оно не зависит от момента подачи.

    Так его вычисляет движок (`engine.take_order_id` — из позиции), и ровно
    на этом стоит правило «вооружить — это состояние»: повторное вооружение
    называет тот же уровень и заменяет прежний.
    """
    return OrderRequest(
        action=OrderAction.ARM_TAKE_PROFIT, side=side, volume=1.0,
        submitted_at=at, reason="сторожим уровень",
        order_id=f"take:{side.value}", price=level,
    )


def run(model: ExecutionModel, *steps: OrderRequest | MarketCandle) -> list[list[Fill]]:
    """Проиграть сценарий: заявки подаются, свечи показываются.

    Возвращает список ответов порта на каждую показанную свечу — по одному
    на свечу, в том же порядке.
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


# ---------------------------------------------------------------------------
# Правила исполнения: рыночная заявка
# ---------------------------------------------------------------------------

def test_a_market_order_fills_at_the_next_candles_open() -> None:
    """Заявка, поданная на закрытии свечи, исполняется по `open` следующей."""
    model = ExecutionModel()
    same, following = run(
        model,
        order(OrderAction.OPEN, Side.LONG),
        candle(10, 5, close=100.0),                       # своя свеча
        replace(candle(10, 10), open=105.0, close=106.0),  # следующая
    )
    assert same == [], "заявка исполнилась свечой, начавшейся раньше её подачи"
    assert [fill.price for fill in following] == [105.0]
    assert following[0].at == datetime(2026, 6, 19, 10, 10, tzinfo=MSK), (
        "время сделки — начало свечи исполнения"
    )


def test_a_repeated_candle_does_not_fill_an_order_at_its_own_open() -> None:
    """Свеча показана дважды — заявка не исполняется по её же открытию.

    Один интервал показывается исполнителю несколько раз: круг обмена внутри
    свечи, повтор после переподключения, незакрытый бар на каждом тике потока.
    Реализация, не проверяющая момент подачи, исполнит заявку по открытию
    ТОЙ САМОЙ свечи, на закрытии которой заявка подана. Родственная гипотеза
    «вход по цене сигнальной свечи» даёт 73 расхождения из 127.
    """
    model = ExecutionModel()
    signal = replace(candle(10, 5), open=50.0, close=100.0)
    first, second, next_bar = run(
        model,
        order(OrderAction.OPEN, Side.LONG),
        signal,
        signal,                                # тот же интервал ещё раз
        replace(candle(10, 10), open=100.0),
    )
    assert first == [] and second == [], "заявка исполнилась по открытию своей же свечи"
    assert [fill.price for fill in next_bar] == [100.0]


def test_the_answers_are_empty_once_there_is_nothing_left() -> None:
    """Пустой ответ — условие остановки круга, а не признак поломки."""
    model = ExecutionModel()
    first, second = run(
        model,
        order(OrderAction.OPEN, Side.LONG),
        replace(candle(10, 10), open=100.0),
        replace(candle(10, 10), open=100.0),
    )
    assert len(first) == 1
    assert second == [], "заявка исполнилась дважды на одной свече"


# ---------------------------------------------------------------------------
# Правила исполнения: сторожимый уровень
# ---------------------------------------------------------------------------

def test_the_level_is_guarded_inside_the_bar_and_fills_exactly_at_the_level() -> None:
    """Внутрибарное событие: цена задела уровень, выход ровно по нему.

    Гипотеза «тейк исполняется по `close` свечи» даёт 42 расхождения из 127 —
    ровно по числу тейков.
    """
    model = ExecutionModel()
    run(
        model,
        order(OrderAction.OPEN, Side.LONG),
        replace(candle(10, 10), open=100.0),
        arm(100.5),
    )
    touched, = run(model, replace(candle(10, 15), open=100.2, high=101.0, low=99.0, close=100.2))
    assert [fill.price for fill in touched] == [100.5]
    assert touched[0].action is OrderAction.CLOSE


def test_the_level_is_hit_by_a_touch_not_by_a_break() -> None:
    """Достижение считается по касанию: `high >= уровень`, `low <= уровень`.

    ⚠️ Эталонным датасетом это не проверяется: свечи, у которой экстремум ровно
    равен уровню, на отрезке нет ни одной, и строгое неравенство даёт те же
    127 сделок. Выбрано по смыслу стоп-заявки, а не по данным, — и потому
    проверяется здесь синтетикой, а не там.
    """
    model = ExecutionModel()
    run(
        model,
        order(OrderAction.OPEN, Side.LONG),
        replace(candle(10, 10), open=100.0),
        arm(100.5),
    )
    exactly, = run(model, replace(candle(10, 15), open=100.0, high=100.5, low=100.0))
    assert [fill.price for fill in exactly] == [100.5], "касание ровно в уровень не сработало"


def test_a_short_level_is_guarded_by_the_low() -> None:
    """У шорта уровень ниже входа и сторожится по `low`."""
    model = ExecutionModel()
    run(
        model,
        order(OrderAction.OPEN, Side.SHORT),
        replace(candle(10, 10), open=100.0),
        arm(99.5, Side.SHORT),
    )
    above, below = run(
        model,
        replace(candle(10, 15), open=100.0, high=101.0, low=99.6),
        replace(candle(10, 20), open=100.0, high=101.0, low=99.4),
    )
    assert above == [], "шорт закрылся движением вверх"
    assert [fill.price for fill in below] == [99.5]


def test_a_level_armed_by_the_opening_fill_guards_that_very_bar() -> None:
    """Тейк вооружён уже на свече открытия — три сделки из 127 живут этим.

    Заявка вооружения, рождённая **сделкой**, помечена временем сделки, то есть
    началом свечи. Значит проверка «интервал начался не раньше подачи» её
    пропускает, и вход с выходом умещаются в один бар. Запрет срабатывания
    на своей свече даёт ровно 3 расхождения из 127.
    """
    model = ExecutionModel()
    entry_bar = replace(candle(10, 10), open=100.0, high=101.0, low=99.0)
    opened, = run(model, order(OrderAction.OPEN, Side.LONG), entry_bar)
    assert [fill.action for fill in opened] == [OrderAction.OPEN]

    # Движок вооружает уровень заявкой со временем СДЕЛКИ — начало свечи.
    same_bar, = run(model, arm(100.5, at=entry_bar.time), entry_bar)
    assert [fill.price for fill in same_bar] == [100.5], (
        "уровень, вооружённый сделкой открытия, не сработал на своей же свече"
    )


def test_the_opening_fills_come_before_the_intrabar_events() -> None:
    """Внутри круга сначала исполнения по открытию, внутрибарные — следующим.

    Иначе уровень, вооружённый сделкой открытия, проверялся бы раньше, чем
    движок успел его назвать.

    ⚠️ Уровень здесь вооружён **раньше**, чем появилась позиция, и потому
    сиротский: с 03.09.2026 своей ногой уровень обзаводится только в момент
    вооружения (`backtest.Guard`). Иначе этот сценарий не строится вовсе —
    нога рождается тем самым исполнением по открытию, которое проверяется.
    Утверждение от этого не слабеет: порядок кругов виден по тому, что
    в первом круге пришла сделка ОТКРЫТИЯ, а не задетый уровень. Сорвись
    порядок — первым пришёл бы `CLOSE`, и тест бы упал.

    Тот же порядок на **связанном** стороже проверяет соседний тест
    «уровень, вооружённый сделкой открытия, сторожит свою же свечу»:
    там вооружение подаётся между кругами, ровно как это делает движок.
    """
    model = ExecutionModel()
    run(model, arm(100.5, at=datetime(2026, 6, 19, 10, 5, tzinfo=MSK)))
    bar = replace(candle(10, 10), open=100.0, high=101.0, low=99.0)
    first, second = run(model, order(OrderAction.OPEN, Side.LONG), bar, bar)
    assert [fill.action for fill in first] == [OrderAction.OPEN], (
        "внутрибарное событие обогнало исполнение по открытию"
    )
    assert [fill.action for fill in second] == [OrderAction.CLOSE]
    assert [fill.order_id for fill in model.orphans] == ["take:long"], (
        "уровень усыновил ногу, которой не сторожил"
    )


def test_a_cancel_removes_the_guard() -> None:
    """Снятие убирает сторожа, и уровень больше не стреляет."""
    model = ExecutionModel()
    armed = arm(100.5)
    cancel = OrderRequest(
        action=OrderAction.CANCEL_TAKE_PROFIT, side=Side.LONG, volume=1.0,
        submitted_at=DECIDED_AT, reason="снимаем", order_id="cancel:1",
        target_id=armed.order_id,
    )
    after, = run(model, armed, cancel, replace(candle(10, 15), open=100.0, high=101.0))
    assert after == [], "снятый сторож всё ещё срабатывает"
    assert model.armed == {}


def test_a_level_that_outlived_its_position_is_reported_not_swallowed() -> None:
    """Сработал уровень, а позиции под ним нет: факт записан, сделка отдана.

    У брокера такая заявка не «ничего не делает» — она ОТКРЫВАЕТ позицию
    в обратную сторону на полный объём. Проглотить её молча значило бы спрятать
    самую дорогую ошибку контракта.
    """
    model = ExecutionModel()
    stray, = run(
        model,
        arm(100.5),
        replace(candle(10, 15), open=100.0, high=101.0, low=99.0),
    )
    assert len(stray) == 1
    assert len(model.orphans) == 1, "сирота не записана"
    assert model.orphans[0].order_id == "take:long"


def test_a_level_does_not_close_a_leg_it_was_never_armed_over() -> None:
    """Переворот в одной свече: старый уровень застаёт УЖЕ НОВУЮ ногу.

    Сочетание `hollow_cancel` (снятие ответило по приёму, сторож жив)
    и переворота **в одной свече**. Круг 1 отдаёт выход и вход сразу, и
    к кругу 2 у исполнителя открыта нога в обратную сторону. Условие «позиции
    нет» такой уровень не ловит: позиция есть, просто чужая. Он закрывал бы
    шорт по уровню, посчитанному от цены входа в лонг, с причиной «тейк» —
    сделка, которой не было, и ни строчки о том, что что-то не так.

    ⚠️ Проверяется здесь **сторож исполнителя**, а не поведение движка: движок
    на этой сделке останавливается по своей причине («заявки среди поданных
    нет»), а вот в отчёт прогона до правки уходила выдуманная сделка.
    """
    long_leg = order(OrderAction.OPEN, Side.LONG)
    model = ExecutionModel(hollow_cancel=True)
    run(
        model,
        long_leg,
        replace(candle(10, 10), open=100.0, high=100.2, low=99.8),
        arm(100.5, at=datetime(2026, 6, 19, 10, 10, tzinfo=MSK)),
    )

    # Закрытие свечи 10:15: снятие «принято» (сторож остался жив), выход
    # и вход в обратную сторону подаются подряд — переворот в одной свече.
    reversed_at = datetime(2026, 6, 19, 10, 20, tzinfo=MSK)
    turn = replace(candle(10, 20), open=100.0, high=101.0, low=99.7)
    market, level = run(
        model,
        OrderRequest(
            action=OrderAction.CANCEL_TAKE_PROFIT, side=Side.LONG, volume=1.0,
            submitted_at=reversed_at, reason="снимаем перед выходом",
            order_id="cancel:1", target_id="take:long",
        ),
        order(OrderAction.CLOSE, Side.LONG, reversed_at),
        order(OrderAction.OPEN, Side.SHORT, reversed_at),
        turn,
        turn,
    )

    assert [fill.action for fill in market] == [OrderAction.CLOSE, OrderAction.OPEN]
    assert [fill.order_id for fill in level] == ["take:long"], (
        "переживший сторож не выстрелил — сценарий развалился, а не прошёл"
    )
    assert [fill.order_id for fill in model.orphans] == ["take:long"], (
        "уровень закрыл чужую ногу и не попал в сироты"
    )


def test_a_level_armed_over_nothing_does_not_adopt_a_later_leg() -> None:
    """Вооружились, когда позиции не было, — своей ноги у уровня нет никогда.

    До 03.09.2026 «нога неизвестна» читалось как «подойдёт та, что есть»,
    и поблажка заводилась ради оснастки тестов движка. Через неё уровень,
    выставленный над пустым местом, закрывал первую же подвернувшуюся
    позицию — с причиной «тейк» и по цене из чужого расчёта.

    Проверяется здесь **три** следствия сразу, и каждое стоит отдельно:
    сделка отдана движку и записана сиротой; открытая нога цела — её закрывает
    следующий рыночный выход, а не уровень; в слой «прогноз» ничего
    не записано.
    """
    model = HistoryExecutor()
    run(
        model,
        arm(100.5, at=datetime(2026, 6, 19, 10, 5, tzinfo=MSK)),
        order(OrderAction.OPEN, Side.LONG),
        replace(candle(10, 10), open=100.0, high=100.2, low=99.8),
    )
    stray, = run(model, replace(candle(10, 15), open=100.0, high=101.0, low=99.0))

    assert [fill.order_id for fill in stray] == ["take:long"], (
        "уровень не выстрелил — сценарий развалился, а не прошёл"
    )
    assert [fill.order_id for fill in model.orphans] == ["take:long"]
    assert model.deals == [], "сирота записала сделку, которой не было"
    assert model.level_plans == [], (
        "осиротевший уровень нарисовал метку тейка: `backtest.pairs` соберёт "
        "по ней пару «расчёт — факт» для сделки, которой не было"
    )

    # Нога цела: её закрывает рыночный выход, и вот он-то сделкой становится.
    exit_at = datetime(2026, 6, 19, 10, 15, tzinfo=MSK)
    run(model, order(OrderAction.CLOSE, Side.LONG, exit_at),
        replace(candle(10, 20), open=100.0))
    assert [deal.exit_reason for deal in model.deals] == [ExitReason.SIGNAL], (
        "сирота съела открытую ногу — выходить оказалось нечем"
    )


def test_a_genuine_level_still_draws_its_mark_on_the_forecast_layer() -> None:
    """Обратная половина проверки выше: настоящий уровень метку рисует.

    Без неё «`level_plans` пуст» проходило бы и на модели, которая не пишет
    расчёт никогда.
    """
    model = HistoryExecutor()
    run(
        model,
        order(OrderAction.OPEN, Side.LONG),
        replace(candle(10, 10), open=100.0, high=100.2, low=99.8),
        arm(100.5),
        replace(candle(10, 15), open=100.0, high=101.0, low=99.0),
    )
    assert [plan.price for plan in model.level_plans] == [100.5]
    assert [deal.exit_reason for deal in model.deals] == [ExitReason.TAKE_PROFIT]
    assert model.orphans == []


def test_an_entry_on_top_of_an_entry_is_refused_like_an_exit_without_one() -> None:
    """Две заявки на вход подряд — дефект, и он падает, а не переписывает ногу.

    Выход без входа падает `assert` с 2026-08; вход поверх входа до правки
    молча заменял открытую ногу. Итог: ОДНА сделка вместо двух и потерянный
    вход, о котором не сказала бы ни одна строка отчёта.
    """
    model = ExecutionModel()
    later = datetime(2026, 6, 19, 10, 15, tzinfo=MSK)
    with pytest.raises(AssertionError, match="вход поверх входа"):
        run(
            model,
            order(OrderAction.OPEN, Side.LONG),
            replace(candle(10, 10), open=100.0),
            order(OrderAction.OPEN, Side.SHORT, later),
            replace(candle(10, 15), open=101.0),
        )


# ---------------------------------------------------------------------------
# Уровень движущийся, а не только неподвижный (решение 0009)
# ---------------------------------------------------------------------------

def test_re_arming_replaces_the_level_instead_of_adding_a_second_guard() -> None:
    """«Вооружить» — состояние: на позицию не больше одного сторожимого уровня.

    Адаптер, понявший вооружение как «поставить стоп-заявку», оставил бы
    у брокера за час позиции дюжину стопов; одиннадцать из них пережили бы
    позицию и стали заявками на открытие в обратную сторону.
    """
    model = ExecutionModel()
    run(model, arm(100.5), arm(100.9), arm(101.3))
    assert list(model.armed) == ["take:long"], f"живых сторожей больше одного: {model.armed}"
    assert model.armed["take:long"].order.price == 101.3, (
        "сторожится не последний названный уровень"
    )


def test_the_guard_follows_the_level_that_moved_and_forgets_the_old_one() -> None:
    """Скользящий тейк: уровень поехал — сторожится новый, старый не стреляет.

    Никакой отдельной ветки «скользящий» в модели нет и быть не должно: она
    знает про уровень ровно то, что назвал движок, а едет он или стоит — дело
    движка (решение 0009).
    """
    model = ExecutionModel()
    run(
        model,
        order(OrderAction.OPEN, Side.LONG),
        replace(candle(10, 10), open=100.0),
        arm(100.5, at=datetime(2026, 6, 19, 10, 10, tzinfo=MSK)),
    )
    # Уровень уехал вверх на закрытии свечи 10:10 — сторожится с 10:15.
    moved_at = datetime(2026, 6, 19, 10, 15, tzinfo=MSK)
    at_the_old_level, at_the_new_level = run(
        model,
        arm(101.0, at=moved_at),
        replace(candle(10, 15), open=100.4, high=100.7, low=100.2),  # задет СТАРЫЙ уровень
        replace(candle(10, 20), open=100.6, high=101.2, low=100.4),  # задет НОВЫЙ
    )
    assert at_the_old_level == [], "сторож выстрелил по уровню, который уже уехал"
    assert [fill.price for fill in at_the_new_level] == [101.0]


def test_a_level_that_has_just_moved_does_not_fire_on_its_own_bar() -> None:
    """Новый уровень назван на закрытии свечи — сторожится со следующей.

    Иначе получилось бы решение по цене, не известной на момент решения:
    уровень поехал по итогам этого бара, а сработал бы внутри него же.
    """
    model = ExecutionModel()
    bar = replace(candle(10, 15), open=100.0, high=101.5, low=99.0)
    closes_at = datetime(2026, 6, 19, 10, 20, tzinfo=MSK)
    run(
        model,
        order(OrderAction.OPEN, Side.LONG),
        replace(candle(10, 10), open=100.0),
    )
    on_its_own_bar, on_the_next_bar = run(
        model,
        arm(101.0, at=closes_at),
        bar,
        replace(candle(10, 20), open=100.6, high=101.2, low=100.4),
    )
    assert on_its_own_bar == [], "уровень сработал на свече, по итогам которой его назвали"
    assert [fill.price for fill in on_the_next_bar] == [101.0]


# ---------------------------------------------------------------------------
# Проскальзывание
# ---------------------------------------------------------------------------

def test_by_default_there_is_no_slippage_at_all() -> None:
    """Умолчание — ноль. Ровно так считал прототип, и только так сходится сверка."""
    assert Costs().slippage == 0.0
    model = ExecutionModel()
    filled, = run(
        model,
        order(OrderAction.OPEN, Side.LONG),
        replace(candle(10, 10), open=100.0),
    )
    assert filled[0].price == 100.0


@pytest.mark.parametrize(
    ("action", "side", "expected"),
    [
        (OrderAction.OPEN, Side.LONG, 100.25),    # покупка — дороже
        (OrderAction.OPEN, Side.SHORT, 99.75),    # продажа — дешевле
        (OrderAction.CLOSE, Side.LONG, 99.75),    # выход из лонга — продажа
        (OrderAction.CLOSE, Side.SHORT, 100.25),  # выход из шорта — покупка
    ],
)
def test_slippage_always_moves_the_price_against_the_position(
    action: OrderAction, side: Side, expected: float
) -> None:
    """Знак — по смыслу операции. Обратный превратил бы издержку в подарок."""
    costs = Costs(price_step=0.25, slippage_steps=1.0)
    assert costs.fill_price(100.0, action, side) == pytest.approx(expected)


def test_the_market_fill_moves_by_the_slippage() -> None:
    """Проскальзывание доходит до цены сделки, а не остаётся настройкой."""
    model = ExecutionModel(costs=Costs(price_step=0.25, slippage_steps=2.0))
    filled, = run(
        model,
        order(OrderAction.OPEN, Side.LONG),
        replace(candle(10, 10), open=100.0),
    )
    assert filled[0].price == pytest.approx(100.5)


def test_a_triggered_level_slips_too_and_the_trigger_stays_on_the_level() -> None:
    """Сработавший тейк исполняется хуже уровня, но срабатывает **по уровню**.

    У брокера сработавший стоп — рыночная заявка, и исполняется она как любая
    другая. Освободить от проскальзывания именно тейк значило бы занизить
    издержки ровно там, где их больше всего: 42 выхода из 127 идут по уровню.

    ⚠️ Порог срабатывания при этом остаётся на уровне, а не на сдвинутой цене:
    иначе проскальзывание меняло бы не только деньги, но и **список сделок**.
    """
    model = ExecutionModel(costs=Costs(price_step=0.25, slippage_steps=1.0))
    run(
        model,
        order(OrderAction.OPEN, Side.LONG),
        replace(candle(10, 10), open=100.0),
        arm(100.5),
    )
    touched, = run(model, replace(candle(10, 15), open=100.0, high=100.5, low=100.0))
    assert [fill.price for fill in touched] == [pytest.approx(100.25)], (
        "уровень исполнился без проскальзывания"
    )


def test_slippage_in_steps_without_a_price_step_is_refused() -> None:
    """Настройка, которая выглядит заданной, а работает как ноль, не принимается."""
    with pytest.raises(ValueError, match="шаг цены"):
        Costs(slippage_steps=1.0)


def test_slippage_that_takes_the_price_to_zero_is_refused() -> None:
    """Цена ноль или минус — не цена. Такое число разошлось бы по всему отчёту."""
    costs = Costs(price_step=100.0, slippage_steps=2.0)
    with pytest.raises(ValueError, match="в ноль"):
        costs.fill_price(100.0, OrderAction.CLOSE, Side.LONG)


def test_the_direction_of_an_operation_is_decided_in_one_place() -> None:
    """Таблица «покупка или продажа» одна на проект: знак нельзя развести."""
    assert selling(OrderAction.OPEN, Side.SHORT)
    assert selling(OrderAction.CLOSE, Side.LONG)
    assert not selling(OrderAction.OPEN, Side.LONG)
    assert not selling(OrderAction.CLOSE, Side.SHORT)


def test_the_volume_does_not_change_the_slippage_and_that_is_said_out_loud() -> None:
    """Зависимости издержек от объёма в модели нет — и это названо, а не спрятано.

    ТЗ §4.4 Ж говорит, что с ростом объёма заявка забирает несколько уровней
    стакана. Глубина стакана внутри торгового окна не измерена, порог перехода
    на лимитные заявки помечен «к обсуждению», оракула нет ни одного —
    и выдуманная зависимость давала бы числа, за которыми не стоит замера.
    На больших объёмах модель издержки **занижает**, и это записано в её
    докстринге, а не обнаружится на счёте.
    """
    costs = Costs(price_step=0.25, slippage_steps=1.0)
    one = costs.fill_price(100.0, OrderAction.OPEN, Side.LONG)
    assert one == costs.fill_price(100.0, OrderAction.OPEN, Side.LONG)
    assert "занижает" in (Costs.__doc__ or ""), (
        "ограничение модели издержек перестало быть названным вслух"
    )


# ---------------------------------------------------------------------------
# Комиссия
# ---------------------------------------------------------------------------

def test_an_unknown_tariff_is_not_substituted_with_zero() -> None:
    """`None` — это «неизвестно», а не «ноль» (DOMAIN.md §5).

    Подставленный ноль объявил бы любую сделку окупившей комиссию,
    а реверсная система на пятиминутках делает много переворотов.
    """
    assert Costs().per_side is None
    assert Costs().commission(1.0) is None
    model = ExecutionModel()
    filled, = run(
        model,
        order(OrderAction.OPEN, Side.LONG),
        replace(candle(10, 10), open=100.0),
    )
    assert filled[0].commission is None


def test_the_tariff_is_the_exchange_fee_plus_the_brokers_rouble() -> None:
    """ТЗ §4.4 Е: биржевой сбор из карточки инструмента плюс 1 ₽ брокеру."""
    assert BROKER_FEE_PER_CONTRACT == 1.0
    assert Tariff(exchange_fee=13.0).broker_fee == 1.0
    assert Tariff(exchange_fee=13.0).per_contract == pytest.approx(14.0)


def test_the_breakdown_and_the_single_figure_mean_the_same_money() -> None:
    """Опорная цифра замеров — 14 ₽ за контракт на сторону (PROTOTYPE.md §6)."""
    by_parts = Costs(tariff=Tariff(exchange_fee=13.0))
    by_total = Costs(commission_per_side=14.0)
    assert by_parts.per_side == by_total.per_side == pytest.approx(14.0)
    assert by_parts.commission(2.0) == by_total.commission(2.0) == pytest.approx(28.0)


def test_a_zero_component_of_the_tariff_is_refused() -> None:
    """Ноль внутри тарифа — это молчаливый ноль, а не «комиссия такая».

    Карточку инструмента МосБиржи сегодня не читает никто, и `exchange_fee`
    остался бы нулём сам собой. `Tariff(exchange_fee=0.0)` даёт 1 ₽
    за контракт: на эталонных 127 сделках это комиссия 254 ₽ вместо 3 556 ₽
    и «чистая прибыль» 6 225 ₽ вместо 2 923 ₽ — завышение на 3 302 ₽
    **при непустой колонке комиссии**, то есть неотличимое от правды.
    `Tariff(0.0, 0.0)` дал бы 0 ₽ и чистую, равную валовой.

    «Тарифа нет» выражается отсутствием издержек, а не нулём внутри них:
    `Costs()` про это и говорит.
    """
    with pytest.raises(ValueError, match="нулём не задаётся"):
        Tariff(exchange_fee=0.0)
    with pytest.raises(ValueError, match="нулём не задаётся"):
        Tariff(exchange_fee=13.0, broker_fee=0.0)
    assert Costs().per_side is None, "неизвестный тариф выражается отсутствием издержек"
    # Разница цифр, ради которой запрет и стоит: 127 сделок, обе стороны.
    assert Costs(tariff=Tariff(exchange_fee=13.0)).commission(1.0) == pytest.approx(14.0)
    assert Costs(commission_per_side=1.0).commission(1.0) == pytest.approx(1.0)


def test_two_different_commissions_in_one_report_are_refused() -> None:
    """Разбивка и общая цифра разошлись — выбрать одну молча нельзя."""
    with pytest.raises(ValueError, match="задана дважды"):
        Costs(tariff=Tariff(exchange_fee=13.0), commission_per_side=20.0)


def test_an_explicit_zero_commission_is_allowed_where_a_zero_part_is_not() -> None:
    """Односторонний запрет назван вслух, а не оставлен на догадку.

    Вопрос ревью 03.09.2026: почему `Tariff(0.0, 0.0)` отвергается, а
    `Costs(commission_per_side=0.0)` проходит. Разница в том, что зануляется.
    Ноль **слагаемого** прячется внутри суммы и выходит наружу правдоподобным
    числом: 1 ₽ за контракт, 254 ₽ на 127 сделках. Ноль **всей** комиссии
    спрятаться не может — он и есть отчёт с колонкой «0 ₽» и чистой, равной
    валовой; умолчания у поля нет, `None` означает «не названа».

    Запретить его вдобавок было бы противоречиво: `EngineSettings` явный ноль
    разрешает намеренно, а `replay` требует, чтобы цифры движка и отчёта
    совпадали, — прогнать законные настройки по истории стало бы нельзя.
    """
    zero = Costs(commission_per_side=0.0)
    assert zero.per_side == 0.0 and zero.commission(2.0) == 0.0
    assert Costs().per_side is None, "«не названа» и «ноль» слились в одно"
    with pytest.raises(ValueError, match="нулём не задаётся"):
        Tariff(exchange_fee=0.0, broker_fee=0.0)


def test_a_commission_that_is_not_a_number_is_refused_at_the_border() -> None:
    """`nan` не падает, а расползается по отчёту: ловится здесь, а не в отчёте."""
    with pytest.raises(ValueError, match="не число"):
        Costs(commission_per_side=float("nan"))
    with pytest.raises(ValueError, match="не может быть таким"):
        Costs(commission_per_side=-1.0)
    with pytest.raises(TypeError, match="число"):
        Costs(commission_per_side="14")  # type: ignore[arg-type]  # проверяется отказ на нечисле


def test_the_commission_reaches_both_the_fill_and_the_deal() -> None:
    """У сделки — одна сторона, у закрытой позиции — обе."""
    model = HistoryExecutor(commission_per_side=14.0)
    run(
        model,
        order(OrderAction.OPEN, Side.LONG),
        replace(candle(10, 10), open=100.0),
        order(OrderAction.CLOSE, Side.LONG, datetime(2026, 6, 19, 10, 15, tzinfo=MSK)),
        replace(candle(10, 15), open=101.0),
    )
    assert [fill.commission for fill in model.fills] == [14.0, 14.0]
    assert model.deals[0].commission == pytest.approx(28.0)
    assert model.deals[0].gross == pytest.approx(1.0)
    assert model.deals[0].net == pytest.approx(-27.0), (
        "валовая прибыль в этом проекте результатом не считается"
    )


def test_the_commission_scales_with_the_volume() -> None:
    """Тариф задан за контракт, значит на двух контрактах он вдвое больше."""
    assert Costs(commission_per_side=14.0).commission(2.0) == pytest.approx(28.0)


# ---------------------------------------------------------------------------
# Отказы обязаны быть выразимы моделью (решение 0008, пункт 3)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("reason", list(Refusal))
def test_a_refusal_carries_its_reason_as_a_value(reason: Refusal) -> None:
    """«Нет такой заявки» и «уровень уже задет» требуют противоположных действий."""
    armed = arm(100.5)
    model = ExecutionModel(refuse={armed.order_id: reason})
    with pytest.raises(ExecutionRefused) as refused:
        asyncio.run(model.submit(armed))
    assert refused.value.reason is reason
    assert model.armed == {}, "отклонённая заявка всё-таки встала сторожем"


def test_a_hollow_cancel_leaves_the_guard_alive() -> None:
    """Снятие, ответившее по подтверждению приёма, — самый дорогой отказ порта.

    Возврат из подачи снятия обязан означать «уровень больше не сторожится».
    Реализация, отвечающая по приёму, оставляет сторожа живым: движок сразу
    за снятием подаёт рыночный выход, цена задевает уровень, исполняются обе
    заявки — и получается позиция в обратную сторону на полный объём.
    Ветка обязана быть выразима моделью, иначе она существует только в бою.
    """
    armed = arm(100.5)
    model = ExecutionModel(hollow_cancel=True)
    run(model, armed, OrderRequest(
        action=OrderAction.CANCEL_TAKE_PROFIT, side=Side.LONG, volume=1.0,
        submitted_at=DECIDED_AT, reason="снимаем", order_id="cancel:1",
        target_id=armed.order_id,
    ))
    assert model.armed, "нарушение контракта, включённое нарочно, не воспроизвелось"


def test_a_cancel_is_matched_by_the_order_it_cancels() -> None:
    """Отказ в снятии называется именем снимаемой заявки, а не своим.

    Собственное имя снятия выводится из момента подачи, и записать его
    в сценарий теста заранее нельзя.
    """
    armed = arm(100.5)
    model = ExecutionModel(refuse={armed.order_id: Refusal.ALREADY_TRIGGERED})
    cancel = OrderRequest(
        action=OrderAction.CANCEL_TAKE_PROFIT, side=Side.LONG, volume=1.0,
        submitted_at=DECIDED_AT, reason="снимаем", order_id="cancel:1",
        target_id=armed.order_id,
    )
    with pytest.raises(ExecutionRefused) as refused:
        asyncio.run(model.submit(cancel))
    assert refused.value.reason is Refusal.ALREADY_TRIGGERED


# ---------------------------------------------------------------------------
# Сторож переезда: правил исполнения в оснастке движка больше нет
# ---------------------------------------------------------------------------

#: Признаки правил исполнения в исходнике: сравнение с экстремумами бара
#: и сравнение с моментом подачи заявки.
EXECUTION_RULES = (
    re.compile(r"\.high\b"),
    re.compile(r"\.low\b"),
    re.compile(r"submitted_at"),
    re.compile(r"\.open\b"),
)


def test_the_engine_test_harness_has_no_execution_rules_of_its_own() -> None:
    """Сторож уровня **переехал**, а не был скопирован (решение 0008).

    Пока копий было две — в `backtest/` и в оснастке движка, — сверка
    с прототипом сходилась на той, которую зовёт, а вторая могла разойтись
    с ней молча: ни один тест не сравнивал их между собой.

    Проверяется разбором исходника, а не поведением: поведение у копии
    совпадает по построению, ровно в этом и беда.
    """
    source = pathlib.Path(__file__).with_name("engine_helpers.py").read_text(
        encoding="utf-8"
    )
    body = "\n".join(
        line for line in source.splitlines()
        if not line.lstrip().startswith(("#", "*"))
    )
    found = [rule.pattern for rule in EXECUTION_RULES if rule.search(body)]
    assert not found, (
        "в оснастке тестов движка снова появились правила исполнения: "
        f"{found}. Их место — backtest/execution.py, и только там"
    )


def test_the_engine_test_harness_calls_the_production_model() -> None:
    """Оснастка наследует производственную модель, а не повторяет её."""
    from tests.engine_helpers import NextOpenExecutor

    assert issubclass(NextOpenExecutor, ExecutionModel)
    assert issubclass(HistoryExecutor, ExecutionModel)


def test_the_harness_binds_a_level_to_the_position_placed_right_after_it() -> None:
    """Оснастка связывает сторожа с ногой сама — поблажки в модели больше нет.

    Сценарии тестов движка ставят позицию готовым объектом, и порядок
    «вооружились, а позицию поставили следом» встречается. Пока модель читала
    «нога неизвестна» как «подойдёт та, что есть», такой сторож после
    переворота закрывал **чужую** ногу с причиной «тейк»: выдуманная сделка
    ровно в той оснастке, которой проверяют движок.

    Связывание живёт в оснастке — то есть в коде, существующем ради тестов.
    Уже связанного сторожа постановка новой позиции не трогает: на этом стоит
    распознавание сироты при перевороте, и вторая половина теста — про это.
    """
    from tests.engine_helpers import NextOpenExecutor

    model = NextOpenExecutor(hollow_cancel=True)
    armed = arm(100.5)
    asyncio.run(model.submit(armed))
    guarded = model.place(Side.LONG, 100.0, DECIDED_AT)
    assert model.armed[armed.order_id].leg is guarded, (
        "сторож остался без своей ноги, и закроет он первую попавшуюся"
    )

    # Переворот: под уровнем уже другая нога, и своей она ему не становится.
    model.place(Side.SHORT, 100.0, DECIDED_AT)
    stray, = run(model, replace(candle(10, 15), open=100.0, high=101.0, low=99.0))
    assert [fill.order_id for fill in stray] == ["take:long"], (
        "уровень не выстрелил — сценарий развалился, а не прошёл"
    )
    assert [fill.order_id for fill in model.orphans] == ["take:long"], (
        "уровень закрыл чужую ногу и назвал это тейком"
    )
    assert model.deals == [], "в сделки оснастки ушло закрытие, которого не было"


def test_the_harness_does_not_bind_a_level_to_a_leg_opened_by_a_fill() -> None:
    """Связку называет сценарий, а не производственное присваивание.

    ⚠️ Первая редакция правки 03.09.2026 вешала связывание на сеттер `_open`.
    Через него пишет открытую ногу **сама производственная модель**, когда
    исполняет заявку на вход, — и на этой оснастке уровень, вооружённый над
    пустым местом, усыновлял первую же ногу от сделки открытия. Получался
    зелёный тест на выдуманном тейке в том самом классе, которым проверяют
    движок и от которого наследует счётчик сторожей в сверке.

    Здесь проверяется сценарий ровно этой формы: вооружились раньше, чем
    появилась позиция, а позицию создала настоящая сделка открытия. Уровень
    обязан остаться сиротой — как и на голой `ExecutionModel`.
    """
    from tests.engine_helpers import NextOpenExecutor

    model = NextOpenExecutor()
    bar = replace(candle(10, 10), open=100.0, high=101.0, low=99.0)
    first, second = run(
        model,
        arm(100.5, at=datetime(2026, 6, 19, 10, 5, tzinfo=MSK)),
        order(OrderAction.OPEN, Side.LONG),
        bar,
        bar,
    )
    assert [fill.action for fill in first] == [OrderAction.OPEN]
    assert [fill.order_id for fill in second] == ["take:long"], (
        "уровень не выстрелил — сценарий развалился, а не прошёл"
    )
    assert [fill.order_id for fill in model.orphans] == ["take:long"], (
        "уровень усыновил ногу от сделки открытия — оснастка снова разрешает "
        "выдуманный тейк"
    )
    assert model.deals == [], "в сделки оснастки ушло закрытие, которого не было"


def test_the_open_leg_keeps_both_the_price_and_the_order_it_came_from() -> None:
    """Вход помнит и цену, и имя заявки: пару сделки собирать больше нечем."""
    leg = OpenLeg(side=Side.LONG, price=100.0, at=DECIDED_AT, volume=1.0, order_id="open:1")
    assert leg.order_id == "open:1"
