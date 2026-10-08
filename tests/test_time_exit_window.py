"""Выход по концу окна с предельной ценой: окно → движок, отказы, требование.

Э5, Ф3 задачи З8 (`.docs/plans/E5-hard-close-at-window-end.md`). Что стережётся,
словами — по одному поведению на проверку:

* три поля окна (форма заявки, отступ в шагах, ожидание) и общий шаг цены
  доезжают до `EngineSettings` **из окна**, а не из прежних настроек;
* заявка с предельной ценой, пока биржа не сообщила шаг цены, — не отказ:
  шаг программа берёт у биржи сама (решение 05.10.2026), движок до тех пор
  идёт «по рынку», и это видно (правило 13) — плашкой и строкой в окне
  настроек; проверки этого — `tests/test_step_from_exchange.py`
  и `tests/test_owner_file_without_step.py`;
* алгоритм №2 требует предельной цены и не принимает «по рынку», а прежнее
  его требование — переворот в одной свече — не задето;
* затенение «вне окна» при снятой галочке и предельной цене обещает выход:
  он и правда будет (`_quiet` читает `closes_by_time`).
"""

from __future__ import annotations

import os
from datetime import time

import pytest

os.environ.setdefault("QT_API", "pyside6")  # до первого импорта qasync


import engine
from app import convert
from engine import EngineSettings, TimeExitOrder, TradingWindow
from tests.test_app_algorithm_demands import ALWAYS
from tests.test_app_convert import _series
from tests.test_app_port_startup import SYMBOL, _Heard, database  # noqa: F401 — фикстура
from tests.test_ui_settings import make_dialog  # noqa: F401 — фикстура
from ui.models import (
    MAX_TIME_EXIT_WAIT_BARS,
    Mode,
    ReversalMoment,
    Settings,
    TimeExitKind,
)

#: Окно, где всё выставлено не как у прежних настроек движка (`_PREVIOUS`):
#: значение, взятое не оттуда, видно сразу.
_WINDOW = Settings(
    time_exit_order=TimeExitKind.LIMIT,
    time_exit_limit_steps=7,
    time_exit_wait_bars=3,
    price_step=25.0,
)

#: Прежние настройки движка — другие во всех четырёх полях.
_PREVIOUS = EngineSettings(
    time_exit_order=TimeExitOrder.MARKET,
    time_exit_limit_steps=10,
    time_exit_wait_bars=1,
    price_step=5.0,
)


# ------------------------------------------------------- окно → движок


def test_the_window_values_reach_the_engine_and_not_the_previous_ones() -> None:
    """Стережёт: форма, отступ, ожидание и шаг цены берутся из окна.

    ⚠️ Мутация: вернуть три поля в `convert._ENGINE_FROM_BASE` (как было
    до Ф3) — движок получит значения `_PREVIOUS`, проверка падает.
    """
    made = convert.engine_settings(_WINDOW, Mode.REVERSE, _PREVIOUS)
    got = (
        made.time_exit_order, made.time_exit_limit_steps,
        made.time_exit_wait_bars, made.price_step,
    )
    assert got == (TimeExitOrder.LIMIT, 7, 3, 25.0), (
        f"до движка доехало не то, что в окне: {got}"
    )
    market = convert.engine_settings(
        _WINDOW.replace(time_exit_order=TimeExitKind.MARKET), Mode.REVERSE,
        EngineSettings(time_exit_order=TimeExitOrder.LIMIT, price_step=5.0),
    )
    assert market.time_exit_order is TimeExitOrder.MARKET, (
        "«по рынку» из окна не дошло: форма взята у прежних настроек"
    )


def test_the_wait_limit_of_the_window_is_the_engine_limit() -> None:
    """Стережёт: граница поля «Ждать исполнения» равна границе движка.

    Окно `engine/` не импортирует и держит число у себя. Разойдись они —
    окно предлагало бы ожидание, которое движок отвергнет, или не давало бы
    допустимого.
    """
    assert MAX_TIME_EXIT_WAIT_BARS == engine.MAX_CLOSE_WAIT_BARS


# --------------------------------------------- форма заявки в окне


def test_the_limit_form_disables_the_switch_that_it_overrides(make_dialog) -> None:  # noqa: F811 — фикстура
    """Стережёт: при предельной цене галочка не врёт, при рыночной гаснут числа.

    При предельной цене позиция закрывается всегда (`closes_by_time`):
    снятая галочка и строка «закрытие по времени выключено» были бы
    неправдой на экране.
    """
    dialog = make_dialog(Settings(
        time_exit_order=TimeExitKind.LIMIT, price_step=25.0, close_on_time_end=False,
    ))
    assert not dialog.close_on_time_end.isEnabled()
    assert dialog.window_close_note.text() == "", dialog.window_close_note.text()
    assert dialog.time_exit_limit_steps.isEnabled()
    market = make_dialog(Settings(
        close_on_time_end=False, time_exit_order=TimeExitKind.MARKET,
    ))
    assert market.close_on_time_end.isEnabled()
    assert "выключено" in market.window_close_note.text()
    assert not market.time_exit_limit_steps.isEnabled()
    assert not market.time_exit_wait_bars.isEnabled()


# ------------------------------------------------- требование алгоритма №2

_ALGORITHM_TWO = Settings(
    strategy_id=ALWAYS, reversal_moment=ReversalMoment.SAME_BAR,
    time_exit_order=TimeExitKind.LIMIT, price_step=25.0,
)


def test_the_second_algorithm_sets_the_limit_exit_out_loud() -> None:
    """Стережёт: выход «по рынку» выставляется в «с предельной ценой», вслух.

    Решение 0063: требование выставляется, а не даёт отказ.
    ⚠️ Мутация: убрать требование `time_exit_order` из `_ALWAYS_DEMANDS` —
    подмены нет, проверка падает.
    """
    assert convert.settled(_ALGORITHM_TWO) == (_ALGORITHM_TWO, ())  # канарейка
    values, said = convert.settled(
        _ALGORITHM_TWO.replace(time_exit_order=TimeExitKind.MARKET)
    )
    assert values.time_exit_order is TimeExitKind.LIMIT
    assert "С предельной ценой" in " ".join(said), said


def test_the_reversal_demand_of_the_second_algorithm_still_holds() -> None:
    """Стережёт: новое требование не задело прежнее — переворот в одной свече.

    Набор нарушает **только** момент переворота: предельная цена стоит.
    """
    values, said = convert.settled(
        _ALGORITHM_TWO.replace(reversal_moment=ReversalMoment.NEXT_BAR)
    )
    assert values == _ALGORITHM_TWO
    assert len(said) == 1 and "В той же свече" in said[0], said


# ---------------------------------------------------------------- `_quiet`


def test_the_shade_promises_an_exit_under_the_limit_form_without_the_switch() -> None:
    """Стережёт: подпись затенения читает `closes_by_time`, а не одну галочку.

    ⚠️ Мутация: вернуть в `_quiet` `settings.close_on_time_end` — при снятой
    галочке и предельной цене полоса скажет «выключено», а позиция
    закроется. Проверка падает.
    """
    candles = _series(9, 45, 20)
    window = TradingWindow(start=time(10, 5), end=time(11, 0))
    limit = EngineSettings(
        window=window, close_on_time_end=False,
        time_exit_order=TimeExitOrder.LIMIT, price_step=25.0,
    )
    market = EngineSettings(window=window, close_on_time_end=False)
    assert "закрывается по концу окна" in convert.shades_of(candles, limit)[0].note
    assert "закрывается по концу окна" not in convert.shades_of(candles, market)[0].note


def test_a_file_without_the_exit_form_is_not_said_to_hold_the_market_one(tmp_path) -> None:
    """Стережёт: файл прежней сборки без поля формы не «стоял на рынке».

    Файл владельца 04.10.2026 записан до Ф3: ключа `time_exit_order` в нём
    нет. С 05.10.2026 умолчание поля — «с предельной ценой» (решение 0063),
    и строка обязана сказать «взято по умолчанию», а не «стояло «По рынку»»
    — второго человек не выбирал.
    """
    import json

    from app.settings_store import SettingsStore

    store = SettingsStore(tmp_path)
    assert store.save(_ALGORITHM_TWO) == ""
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    del raw["settings"]["time_exit_order"]
    store.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    read = SettingsStore(tmp_path).load()
    assert read.values.time_exit_order is TimeExitKind.LIMIT
    said = " ".join(read.troubles)
    assert "стояло «По рынку»" not in said, said
    assert "Заявка на выход по концу окна" in " ".join(read.notes), read.notes


@pytest.mark.parametrize("changes", [
    {"time_exit_wait_bars": 0},
    {"time_exit_limit_steps": -3, "average_period": 21},
    {"time_exit_wait_bars": 13, "time_exit_order": TimeExitKind.LIMIT},
])
def test_a_number_out_of_range_in_the_file_does_not_stop_the_start(changes) -> None:
    """Стережёт: негодное число из файла — подмена вслух, а не программа без окна.

    С Ф3 отступ и ожидание приходят из файла; окно таких не даст (границы
    полей), файл, правленный руками, — даст. Движок отвергает их голым
    `ValueError`, а сборка порта прежде ловила только `SettingsRefused`.

    ⚠️ Мутации: убрать `except (ValueError, TypeError)` в `_startup_settings`
    — исключение, проверка падает; вернуть пустую строку — подмена молчит.
    """
    from app.port import _startup_settings

    values, engine, said = _startup_settings(Settings(**changes), Mode.REVERSE)
    assert 1 <= engine.time_exit_wait_bars <= MAX_TIME_EXIT_WAIT_BARS
    assert engine.time_exit_limit_steps >= 0
    assert "не принимает" in said, f"подмена числа из файла прошла молча: «{said}»"
    assert values.average_period == changes.get("average_period", Settings().average_period), (
        "вместе с негодным числом пропало годное соседнее"
    )
