"""Минутки прогону — только скользящему уровню на малой свече (B-058).

Проверяется `app/minutes.py::minute_plan` вместе с прогоном по истории:
на свече крупнее порога исполнитель, получив минутки, отдаёт движку
наблюдение на каждую их точку, и прогон упирается в предел кругов одного
бара (`engine/runner.py::_MAX_ROUNDS`). Правило подачи снимает это, а
о скользящем уровне без минуток говорит строка допущения.

Что стережёт каждый тест — сказано в его докстринге фразой «Стережёт:».
"""

from __future__ import annotations

import asyncio
import csv
import pathlib
from collections.abc import Sequence
from datetime import datetime, time, timedelta, timezone

import pytest

from app import convert
from app.minutes import MinutePlan, minute_plan
from backtest import CoarseBar, HistoryRun, Minutes, replay
from market.aggregate import build_bars
from market.candles import MINUTE, MSK, Candle, Timeframe
from ui.models import Mode, Settings

REPO = pathlib.Path(__file__).resolve().parent.parent
#: Минутки MXZ6 из локальной базы — вне git (`reference/minutes/README.md`).
MXZ6 = REPO / "reference" / "minutes" / "MXZ6.csv"

#: Весь день: окно с равными границами (`TradingWindow`). Больше баров
#: с открытой позицией — больше мест, где прогон мог бы остановиться.
WHOLE_DAY = {"window_start": time(0, 0), "window_end": time(0, 0)}

COARSE_LINE = "скользящий уровень в прогоне двигается только на закрытии свечи"


def _run(values: Settings, minutes: Sequence[Candle], *, plan: MinutePlan) -> HistoryRun:
    """Прогон как в окне: настройки движка и модуль — из тех же `values`."""
    timeframe = convert.timeframe_of(values.timeframe)
    bars = build_bars(minutes, timeframe)
    module = convert.strategy_settings(values)
    return plan.mark(asyncio.run(replay(
        bars,
        convert.chosen_algorithm(values).build(module),
        convert.engine_settings(values, Mode.REVERSE),
        costs=convert.run_costs(values),
        minutes=None if plan.order is None else Minutes(minutes, plan.order),
    )))


def _coarse_said(run: HistoryRun) -> list[str]:
    return [item.short for item in run.assumptions if COARSE_LINE in item.short]


# ---------------------------------------------------------------------------
# Синтетика: длинная свеча, ровный рост, позиция открыта
# ---------------------------------------------------------------------------

def _rising_minutes(hours: int) -> list[Candle]:
    """Минутки ровного роста: каждая выше предыдущей, две точки обхода на минутку."""
    start = datetime(2026, 9, 21, 10, 0, tzinfo=MSK)
    made: list[Candle] = []
    for index in range(hours * 60):
        low = 100_000.0 + index * 5.0
        made.append(Candle(
            time=start + timedelta(minutes=index),
            open=low, high=low + 5.0, low=low, close=low + 5.0,
            volume=1.0, timeframe=MINUTE, filled_minutes=1,
        ))
    return made


def test_a_coarse_bar_with_a_trailing_level_runs_to_the_end_and_says_why() -> None:
    """Стережёт: свеча 60 мин > порога 5, скользящий уровень — без остановки, строка есть.

    Сначала тот же вход с минутками, поданными силой, обязан остановиться
    по пределу кругов. Иначе вход слишком короткий, и тест проверял бы
    только текст. Мутация «условие по порогу снято» подаёт минутки
    и роняет первую проверку после этого. Мутация «пометка потеряна»
    (`MinutePlan.mark`) роняет проверку строки.
    """
    values = Settings().replace(
        trailing_enabled=True, timeframe="1 час", average_period=2, **WHOLE_DAY
    )
    minutes = _rising_minutes(12)

    forced = _run(values, minutes, plan=MinutePlan(convert.minute_order_of(values)))
    assert forced.halted, "минутки силой — а прогон не остановился: вход не проверяет B-058"

    plan = minute_plan(values, convert.timeframe_of(values.timeframe))
    run = _run(values, minutes, plan=plan)

    assert plan.order is None, "свече крупнее порога поданы минутки"
    assert not run.halted, f"прогон остановлен: {run.halted}"
    assert run.position is not None or run.deals, "позиции не было — проверять нечего"
    assert run.coarse_bar == CoarseBar(60, 5)
    assert _coarse_said(run) == [
        "Свеча 60 мин крупнее порога 5 мин: скользящий уровень в прогоне "
        "двигается только на закрытии свечи."
    ], f"о скользящем уровне без минуток отчёт молчит: {[a.short for a in run.assumptions]}"


@pytest.mark.parametrize(
    ("trailing", "timeframe", "order_given"),
    [
        (False, "5 минут", False),
        (False, "1 минута", False),
        (True, "5 минут", True),
        (True, "1 минута", True),
        (True, "15 минут", False),
        (True, "30 минут", False),
    ],
)
def test_minutes_go_only_to_a_trailing_level_on_a_bar_within_the_limit(
    trailing: bool, timeframe: str, order_given: bool
) -> None:
    """Стережёт: правило подачи — скользящий уровень И свеча не крупнее порога (включительно).

    Порог по умолчанию — 5 минут. Неподвижному тейку минуток нет ни на какой
    свече и пометки о них тоже: сделки неподвижного тейка минутки не меняют.
    """
    values = Settings().replace(trailing_enabled=trailing, timeframe=timeframe)
    tf = convert.timeframe_of(timeframe)
    plan = minute_plan(values, tf)
    assert (plan.order is not None) is order_given
    coarse = trailing and not order_given
    assert plan.coarse == (CoarseBar(tf.minutes, 5) if coarse else None)


def test_the_limit_comes_from_the_settings() -> None:
    """Стережёт: порог берётся из настроек окна, а не константой (правило 16)."""
    values = Settings().replace(trailing_enabled=True, timeframe="15 минут")
    limit = type(values.minute_bar_limit)
    assert minute_plan(values, Timeframe(15)).order is None
    wide = values.replace(minute_bar_limit=limit.FIFTEEN)
    assert minute_plan(wide, Timeframe(15)).order is not None


# ---------------------------------------------------------------------------
# Настоящие минутки MXZ6: 30 и 60 минут доходят до конца
# ---------------------------------------------------------------------------

def _mxz6() -> list[Candle]:
    with MXZ6.open(newline="") as source:
        return [
            Candle(
                time=datetime.fromtimestamp(int(row["ts"]), timezone.utc).astimezone(MSK),
                open=float(row["open"]), high=float(row["high"]),
                low=float(row["low"]), close=float(row["close"]),
                volume=float(row["volume"]), timeframe=MINUTE, filled_minutes=1,
            )
            for row in csv.DictReader(source)
        ]


@pytest.mark.skipif(not MXZ6.exists(), reason="минуток MXZ6 нет: reference/ вне git")
@pytest.mark.parametrize("timeframe", ["30 минут", "1 час"])
@pytest.mark.parametrize("trailing", [False, True], ids=["fixed-take", "trailing"])
def test_mxz6_on_coarse_bars_runs_to_the_end(timeframe: str, trailing: bool) -> None:
    """Стережёт (B-058): MXZ6 на 30 и 60 минутах — прогон без остановки, сделки есть.

    До правила подачи прогон останавливался на первом баре с открытой
    позицией и при неподвижном тейке. Мутация «условие по порогу снято»
    роняет вариант со скользящим уровнем, «условие по скользящему уровню
    снято» — вариант с неподвижным тейком.
    """
    values = Settings().replace(
        trailing_enabled=trailing, timeframe=timeframe, **WHOLE_DAY
    )
    tf = convert.timeframe_of(timeframe)
    run = _run(values, _mxz6(), plan=minute_plan(values, tf))

    assert not run.halted, f"прогон остановлен: {run.halted}"
    assert run.deals, "ни одной сделки — проверять нечего"
    assert run.minute_order is None
    assert bool(_coarse_said(run)) is trailing
