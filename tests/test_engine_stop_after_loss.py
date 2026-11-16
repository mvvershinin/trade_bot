"""`B-106`: «ждать нового сигнала, но первый убыток за день — стоп до конца дня».

Ответы владельца счёта 16.11.2026: убыток считается **чистыми**, с комиссией
обеих сторон; засчитываются только сделки, закрытые внутри торгового окна —
выход по концу окна в убыток стопом не считается. Решение 0072.

Сценарии идут через `Engine` и производственную модель исполнения
(`NextOpenExecutor` — это `backtest.ExecutionModel`), то есть тем же путём,
что прогон по истории; бой отличается только исполнителем за портом.

Что стерегут тесты — по одному поведению на тест:

* убыточная по сигналу сделка внутри окна закрывает день: новых входов нет,
  в журнале строка с суммой чистыми;
* сделка «+пункты, но минус после комиссии» стоп **даёт**; та же сделка
  без комиссии — не даёт (убыток считается чистыми, а не по пунктам);
* убыточный выход по концу окна стопа не даёт: окно расширили — вход есть.
"""

from __future__ import annotations

import asyncio
from datetime import time

from backtest import Costs
from engine import Engine, EngineSettings, Mode, Reversal, TradingWindow
from strategies import Intent
from tests.engine_helpers import NextOpenExecutor, ScriptedStrategy, candle, decision

PRICE = 210_000.0
WINDOW = TradingWindow(time(10, 5), time(11, 0))
#: «Ждать нового сигнала» плюс стоп по первому убытку — как его отдаёт окно.
SETTINGS = EngineSettings(
    mode=Mode.REVERSE, window=WINDOW, reversal=Reversal.THROUGH_BAR,
    take_profit=False, stop_after_take_profit=False, stop_after_loss=True,
)
#: Вход на 10:05, исполнение 10:10, обратный сигнал 10:15, выход 10:20 по цене
#: четвёртой свечи, дальше сигналы в обе стороны — каждый дал бы вход.
INTENTS = [
    Intent.LONG, Intent.LONG, Intent.SHORT, Intent.SHORT,
    Intent.SHORT, Intent.LONG, Intent.LONG, Intent.SHORT,
]
EXIT_BAR = 3


def _day(
    settings: EngineSettings,
    exit_price: float,
    *,
    commission: float = 17.0,
    widen_after: int | None = None,
):
    """Свечи 10:05, 10:10, … по `INTENTS`; свеча `EXIT_BAR` стоит на `exit_price`.

    `widen_after` — после этой свечи окно расширяется до 11:00 через
    `apply_settings`, как это сделал бы владелец счёта в окне настроек.
    """
    strategy = ScriptedStrategy([decision(intent, close=PRICE) for intent in INTENTS])
    lines: list = []
    engine = Engine(strategy, settings, journal=lines.append)
    executor = NextOpenExecutor(costs=Costs(commission_per_side=commission))

    async def scenario() -> None:
        for number in range(len(INTENTS)):
            price = exit_price if number == EXIT_BAR else PRICE
            await engine.on_market_candle(candle(10, 5 + 5 * number, close=price), executor)
            if number == widen_after:
                engine.apply_settings(settings.replace(window=WINDOW))

    asyncio.run(scenario())
    return engine, executor, lines


def _stop_lines(lines: list) -> list:
    return [line for line in lines if "первый убыток за день — стоп" in line.reason]


def test_a_losing_signal_exit_inside_the_window_closes_the_day() -> None:
    """Лонг закрыт обратным сигналом на 100 пунктов ниже входа — дальше входов нет.

    Без правила свеча выхода дала бы шорт, а следующие сигналы — ещё сделки.
    Строка журнала называет сумму чистыми: −100 пунктов и 34 ₽ комиссии.
    """
    engine, executor, lines = _day(SETTINGS, PRICE - 100)

    assert len(executor.deals) == 1, executor.deals
    assert engine.state.position is None
    assert engine.state.entry_in_flight() is None, "после убытка подан вход"
    said = _stop_lines(lines)
    assert said, [line.event for line in lines]
    assert "134" in said[0].reason, said[0].reason


def test_plus_points_but_minus_after_commission_is_a_loss() -> None:
    """+20 пунктов, комиссия 2 × 17 ₽ — минус 14 ₽ чистыми: стоп есть.

    Та же сделка без комиссии — плюс 20 ₽, и робот входит по следующему
    сигналу: стоп даёт именно комиссия, а не пункты.
    """
    _engine, charged, lines = _day(SETTINGS, PRICE + 20, commission=17.0)
    _engine, free, _lines = _day(SETTINGS, PRICE + 20, commission=0.0)

    assert len(charged.deals) == 1, charged.deals
    assert _stop_lines(lines), [line.event for line in lines]
    assert len(free.deals) > 1, "без комиссии сделка в плюсе, а входов после неё нет"


def test_a_losing_exit_at_the_window_end_gives_no_stop() -> None:
    """Окно 10:05–10:20: лонг закрыт по концу окна в убыток. Окно расширили — вход есть.

    Выход по концу окна — уход с рынка по расписанию, а не проигранная
    сделка (ответ владельца счёта 16.11.2026). Засчитай его правило —
    расширенное окно осталось бы пустым до конца дня.
    """
    narrow = SETTINGS.replace(window=TradingWindow(time(10, 5), time(10, 20)))
    engine, executor, lines = _day(narrow, PRICE - 100, widen_after=EXIT_BAR)

    first = executor.deals[0]
    assert first.exit_reason == "window_end", executor.deals
    assert not [line for line in _stop_lines(lines) if line.at == first.exit_time], (
        "выход по концу окна засчитан правилу «первый убыток за день»"
    )
    assert len(executor.deals) > 1 or engine.state.position is not None, (
        "после выхода по концу окна в расширенном окне входов нет"
    )
