"""Дыры Р-10, найденные мутацией (test-expert, 05.11.2026)."""

from __future__ import annotations

import pytest

from engine import EngineState
from strategies import Intent
from tests.test_engine_level_unfilled import (
    ARM,
    EVENT,
    HELD,
    SETTINGS,
    _bar_t,
    _engine,
    _run,
    _Scripted,
)


@pytest.mark.parametrize(
    ("hold", "close"),
    [(True, 992.0), (False, 992.0), (False, 1004.0)],
    ids=["в убытке, держать", "в убытке, закрыть", "не в убытке"],
)
def test_after_the_event_the_exit_path_of_the_position_is_not_counted_broken(
    hold: bool, close: float
) -> None:
    """Р-10 п. 2: во всех трёх ветках счётчик отказов выхода обнулён.

    Событие — новое начало для позиции (трейлинг заново): оставшийся от
    прежних отказов счётчик `exit_blocked_bars` держал бы уровень внутри бара
    выключенным до конца позиции (`D-140`) — молча. Мутация: убрать
    `exit_blocked_bars=0` из разбора события.
    """
    executor = _Scripted(answers=[[EVENT]])
    state = EngineState(position=HELD, pending=(ARM,), exit_blocked_bars=1)
    engine, _ = _engine(Intent.LONG, SETTINGS.replace(hold_after_unfilled_level=hold), state)
    _run(engine, executor, _bar_t(close))
    assert engine.halted == ""
    assert engine.state.exit_blocked_bars == 0, "счётчик отказов выхода пережил событие"
