"""Отказ вооружения «уже исполняется» (`D-142`, решение 0066 Р2).

Вооружение — состояние под одним именем. Если сторож с этим именем уже
сработал и выход по нему исполняется, исполнитель новый уровень не ставит
и отказывает определённо, с причиной `Refusal.ALREADY_TRIGGERED`. Движок
обязан: оставить прежний уровень и заявку вооружения в полёте (иначе сделка
по сторожу окажется «без своей заявки» и остановит робота), рыночный выход
не подавать и написать в журнале про исполняющийся выход — а не общую строку
вооружения «позиция осталась без уровня, следующая свеча вооружит заново»,
которая зовёт к выходу поверх живого сторожа.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, time

import pytest

from engine import (
    Engine,
    EngineSettings,
    EngineState,
    ExitReason,
    Fill,
    JournalEntry,
    Mode,
    OrderAction,
    OrderRequest,
    Refusal,
    Side,
    TakeProfit,
    TradingWindow,
    take_order_id,
)
from engine.pipeline import _arm_order
from engine.runner import _declined_words
from strategies import Intent
from tests.engine_helpers import (
    MSK,
    RecordingExecutor,
    ScriptedStrategy,
    bar,
    candle,
    decision,
    position,
)

WINDOW = TradingWindow(time(10, 5), time(11, 0))
WORKING = EngineSettings(mode=Mode.REVERSE, window=WINDOW)
PRICE = 210_000.0
LEVEL = 211_050.0  # 210 000 + 0,5%: шаг 5 назовёт тот же уровень


def _guarded() -> EngineState:
    """Лонг со сторожимым уровнем и его заявкой вооружения в полёте."""
    held = replace(
        position(Side.LONG, price=PRICE), take=TakeProfit(percent=0.5, level=LEVEL)
    )
    return EngineState(
        position=held, pending=(_arm_order(held, bar(10, 5).closes_at, LEVEL),)
    )


def _refused_rearm() -> tuple[Engine, RecordingExecutor, list[JournalEntry], str]:
    """Шаг 5 переставляет тейк, исполнитель отвечает «уже исполняется»."""
    state = _guarded()
    assert state.position is not None
    name = take_order_id(state.position)
    lines: list[JournalEntry] = []
    engine = Engine(
        ScriptedStrategy([decision(Intent.LONG, close=PRICE)] * 2), WORKING,
        state=state, journal=lines.append,
    )
    executor = RecordingExecutor(refuse={name: Refusal.ALREADY_TRIGGERED})
    asyncio.run(engine.on_market_candle(candle(10, 5, close=PRICE), executor))
    return engine, executor, lines, name


def test_a_rearm_refused_as_already_triggered_keeps_the_level_and_sends_no_exit() -> None:
    """Уровень жив, заявка вооружения в полёте, рыночного выхода нет, робот работает.

    Стережёт состояние движка после (ARM, ALREADY_TRIGGERED): уровень позиции
    прежний, заявка вооружения с именем сторожа осталась в полёте, ни снятия,
    ни выхода исполнителю не ушло, остановки нет.
    """
    engine, executor, _, name = _refused_rearm()

    assert engine.position is not None
    assert engine.position.take.level == LEVEL, (
        "уровень погашен — следующий выход пойдёт поверх сторожа"
    )
    assert engine.state.order_in_flight(name) is not None, (
        "заявка вооружения убрана из полёта — сделка по сторожу придёт «без своей заявки»"
    )
    assert executor.submitted == [], "движок подал что-то поверх исполняющегося сторожа"
    assert engine.halted == ""


def test_the_journal_names_the_executing_exit_not_a_guardless_position() -> None:
    """Строка отказа — про сработавший сторож и исполняющийся выход.

    Стережёт текст: в нём есть «уже сработал» и «исполняется», и нет общей
    строки вооружения — «без сторожимого уровня» и «вооружит заново».
    """
    _, _, lines, _ = _refused_rearm()
    declined = [entry for entry in lines if entry.event == "Заявка отклонена исполнителем"]
    assert len(declined) == 1, "отказ не попал в журнал"
    text = declined[0].reason
    assert "уже сработал" in text
    assert "исполняется" in text
    assert "рыночный выход не подаётся" in text
    for wrong in ("без сторожимого уровня", "вооружит заново"):
        assert wrong not in text, f"строка обещает «{wrong}» поверх исполняющегося выхода"


def test_the_deal_of_the_triggered_guard_is_matched_on_the_next_candle() -> None:
    """Сделка сработавшего сторожа приходит следом — и находит свою заявку.

    Стережёт смысл «оставить заявку в полёте»: сделка по уровню закрывает
    позицию как выход по тейку, без строки «Сделка без своей заявки»
    и без остановки.
    """
    engine, executor, lines, name = _refused_rearm()
    executor.refuse.clear()
    executor.scripted.append([Fill(
        action=OrderAction.CLOSE, side=Side.LONG, volume=1.0, price=LEVEL,
        at=candle(10, 10).time, order_id=name,
    )])
    asyncio.run(engine.on_market_candle(candle(10, 10, close=PRICE), executor))

    assert engine.position is None
    assert engine.halted == ""
    assert "Сделка без своей заявки" not in [entry.event for entry in lines]
    assert engine.state.take_profit_date == datetime(2026, 6, 19, tzinfo=MSK).date()


@pytest.mark.parametrize("reason", list(Refusal), ids=lambda r: r.value)
@pytest.mark.parametrize("action", list(OrderAction), ids=lambda a: a.value)
def test_every_action_with_every_reason_has_its_words(
    action: OrderAction, reason: Refusal
) -> None:
    """Таблица строк отказа полна: каждое действие с каждой причиной находит слова.

    Стережёт таблицу `engine.runner._DECLINED`: выпавшая строка уронила бы
    движок внутри обработчика отказа вместо строки в журнале.
    """
    known, means = _declined_words(_order_of(action), reason)
    assert known and means


def _order_of(action: OrderAction) -> OrderRequest:
    """Годная заявка этого действия — каждая со своими обязательными полями."""
    at = datetime(2026, 6, 19, 10, 10, tzinfo=MSK)
    held = replace(position(Side.LONG, price=PRICE), take=TakeProfit(percent=0.5))
    if action is OrderAction.ARM_TAKE_PROFIT:
        return _arm_order(held, at, LEVEL)
    base = OrderRequest(
        action=OrderAction.OPEN, side=Side.LONG, volume=1.0, submitted_at=at,
        reason="тест", order_id=f"{action.value}:тест",
    )
    if action is OrderAction.CLOSE:
        return replace(base, action=action, exit_reason=ExitReason.SIGNAL)
    if action is OrderAction.CANCEL_TAKE_PROFIT:
        return replace(base, action=action, target_id=take_order_id(held))
    return base
