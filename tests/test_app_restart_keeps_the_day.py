"""D-138: перезапуск программы днём не открывает второй вход за дату.

Стережётся поведение **живого хода** (`app/observe.py::LiveObserver`), а не
движка: после перезапуска наблюдатель собирается заново с пустым
`EngineState`, и признак «день закрыт» обязан вернуться сам. Возвращает его
повторный проход по ряду из базы: ряд живого хода начинается с полуночи
(`HistoryPort._candles`, граница `since` округляется вниз до начала дня),
поэтому утренний выход проходит через движок ещё раз и снова пишет
`last_exit_date` и `take_profit_date`.

⚠️ Журнал сделок в базе этого не заменяет: `journal_trade` живым ходом
не пишется вовсе (`D-027`), восстанавливать оттуда сегодня нечего.

⚠️ Вливать восстановленное состояние в новый ход **до** повторного прохода
нельзя: движок закрыл бы день уже на утренней свече, утренняя сделка исчезла
бы из показа, и окно разошлось бы с тем, что было. Третий тест ниже — та же
беда с другой стороны: ряд, начатый после утреннего выхода, день теряет.

Что стерегут тесты:

* «один вход в день»: ход, собранный заново на том же ряде, даёт те же сделки,
  что ход, шедший с утра, и не больше одного входа за дату;
* «стоп после тейка»: то же — после тейка в эту дату новых входов нет;
* граница: ряд без утреннего выхода вход даёт — то есть первые два теста
  зелены из-за повторного прохода, а не потому, что входов не бывает вовсе.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

from app.observe import LiveObserver
from backtest import Costs
from engine import EngineSettings, ExitReason, Mode
from engine.window import in_moscow
from market import Candle
from strategies import MaReverseAlways, MaReverseAlwaysSettings
from tests.test_app_observe import Said, bars, settings

#: Ряд одного дня. На нём утром есть вход и выход, а после выхода средняя
#: пересекается снова — вход был бы, не будь день закрыт.
SERIES = bars(160)
#: Где «программу закрыли»: после утреннего выхода, до следующего сигнала.
RESTART_AT = 90

ONE_ENTRY = replace(settings(), mode=Mode.ONE_ENTRY_A_DAY, take_profit=False)
STOP_AFTER_TAKE = replace(
    settings(), mode=Mode.REVERSE, take_profit_percent=0.2, stop_after_take_profit=True,
)


def _observer(engine: EngineSettings) -> LiveObserver:
    return LiveObserver(
        strategy=MaReverseAlways(MaReverseAlwaysSettings()),
        settings=engine,
        say=Said(),
        costs=Costs(commission_per_side=14.0),
    )


def _entries(engine: EngineSettings, *offers: list[Candle]) -> list[tuple[str, str, str]]:
    """Прогнать один ход по отрезкам. Сделки: вход, выход, причина — по МСК."""

    async def go() -> list[tuple[str, str, str]]:
        observer = _observer(engine)
        run = None
        for offer in offers:
            run = await observer.feed(offer)
        assert run is not None
        return [
            (
                f"{in_moscow(deal.entry_time):%d %H:%M}",
                f"{in_moscow(deal.exit_time):%H:%M}",
                deal.exit_reason.name,
            )
            for deal in run.deals
        ]

    return asyncio.run(go())


def _before_restart(engine: EngineSettings) -> list[tuple[str, str, str]]:
    """Ход, шедший с утра, — бар за баром до перезапуска."""
    return _entries(engine, *(SERIES[:k] for k in range(20, RESTART_AT + 1)))


def test_one_entry_a_day_survives_a_restart() -> None:
    """После выхода сегодня новый ход на той же базе второй раз не входит."""
    morning = _before_restart(ONE_ENTRY)
    assert len(morning) == 1, f"на ряде нет утреннего выхода: {morning}"
    restarted = _entries(ONE_ENTRY, SERIES)
    assert restarted == morning, (
        f"после перезапуска сделок стало {restarted}, до него было {morning}: "
        "день, закрытый выходом, после перезапуска открылся снова"
    )


def test_stop_after_take_survives_a_restart() -> None:
    """После тейка сегодня новый ход на той же базе в эту дату не входит."""
    morning = _before_restart(STOP_AFTER_TAKE)
    assert morning and morning[-1][2] == ExitReason.TAKE_PROFIT.name, (
        f"на ряде нет утреннего тейка: {morning}"
    )
    restarted = _entries(STOP_AFTER_TAKE, SERIES)
    assert restarted == morning, (
        f"после перезапуска сделок стало {restarted}, до него было {morning}: "
        "правило «стоп после тейка» после перезапуска забылось"
    )


def test_a_series_without_the_morning_exit_enters() -> None:
    """Граница: ряд, начатый после утреннего выхода, день не помнит и входит.

    Без этого теста два первых зелены и на ряде, где входа не бывает вовсе.
    """
    assert _entries(ONE_ENTRY, SERIES[RESTART_AT:]), (
        "ряд после утреннего выхода не дал ни одной сделки — первые тесты "
        "стерегут пустоту, а не память дня"
    )
