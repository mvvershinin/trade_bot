"""`B-106`, `B-108`, `B-109`: «ждать нового сигнала, но первый убыток в окне — стоп».

Ответы владельца счёта 16.11.2026: убыток считается **чистыми**, с комиссией
обеих сторон; засчитываются только сделки, закрытые внутри торгового окна —
выход по концу окна в убыток стопом не считается. После боя 2 16.11:
«стоп должен был быть сразу после закрытия лонга» — убыточный по оценке
выход при перевороте «в той же свече» идёт без входа (`B-108`); и «по условиям
у нас должно быть снова до первого убытка» в новом окне — стоп действует
до конца окна, а не даты (`B-109`). Решение 0072.

Сценарии идут через `Engine` и производственную модель исполнения
(`NextOpenExecutor` — это `backtest.ExecutionModel`), то есть тем же путём,
что прогон по истории; бой отличается только исполнителем за портом.

Что стерегут тесты — по одному поведению на тест:

* убыточная по сигналу сделка внутри окна закрывает окно «через свечу»:
  новых входов нет, в журнале строка с суммой чистыми;
* сделка «+пункты, но минус после комиссии» стоп **даёт**; та же сделка
  без комиссии — не даёт (убыток считается чистыми, а не по пунктам);
* убыточный выход по концу окна стопа не даёт: окно расширили — вход есть;
* переворот «в той же свече» с убыточной оценкой — только выход, без входа;
* переворот «в той же свече» с прибыльной оценкой — переворот есть;
* оценка прибыльная, сделка выхода убыточная — переворот ушёл, стоп после
  сделки: перевёрнутая позиция доводится до выхода, дальше входов нет;
* стоп в окне A, окно сдвинуто на B — вход в B есть;
* стоп сегодня — завтра в том же окне входы есть;
* окно через полночь — одно окно на две даты, следующее — новое;
* строка о закрытии по тейку при этом варианте не говорит про «стоп после тейка».
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import date, datetime, time

from backtest import Costs
from engine import Engine, EngineSettings, Mode, Reversal, Side, TradingWindow
from engine.window import MSK, WindowDay
from strategies import Intent
from tests.engine_helpers import NextOpenExecutor, ScriptedStrategy, candle, decision

PRICE = 210_000.0
WINDOW = TradingWindow(time(10, 5), time(11, 0))
#: «Ждать нового сигнала» плюс стоп по первому убытку — как его отдаёт окно.
SETTINGS = EngineSettings(
    mode=Mode.REVERSE, window=WINDOW, reversal=Reversal.THROUGH_BAR,
    take_profit=False, stop_after_take_profit=False, stop_after_loss=True,
)
#: Переворот «в той же свече»: оценке нужна ставка комиссии из настроек.
SAME_BAR = SETTINGS.replace(reversal=Reversal.SAME_BAR, commission_per_side=17.0)
#: Вход на 10:05, исполнение 10:10, обратный сигнал 10:15, выход 10:20 по цене
#: четвёртой свечи, дальше сигналы в обе стороны — каждый дал бы вход.
INTENTS = [
    Intent.LONG, Intent.LONG, Intent.SHORT, Intent.SHORT,
    Intent.SHORT, Intent.LONG, Intent.LONG, Intent.SHORT,
]
EXIT_BAR = 3
#: Свеча решения о выходе: её закрытие — оценка при перевороте «в той же свече».
SIGNAL_BAR = 2
#: Окно, на которое владелец счёта сдвигает торговлю после стопа (`B-109`).
SHIFTED = TradingWindow(time(10, 20), time(11, 30))
#: Понедельник после пятницы 19.06.2026 — следующий торговый день.
NEXT_DAY = 22


def _run(
    settings: EngineSettings,
    prices: Mapping[int, float],
    *,
    commission: float = 17.0,
    changes: Mapping[int, EngineSettings] | None = None,
    days: tuple[int, ...] = (19,),
):
    """Свечи 10:05, 10:10, … по `INTENTS` на каждый из `days`; цена — `prices[номер]`.

    Номер свечи сквозной через дни. Цена не названа — `PRICE`. `changes` —
    после свечи с этим номером новые настройки через `apply_settings`, как это
    сделал бы владелец счёта в окне настроек.
    """
    script = [decision(intent, close=PRICE) for _day in days for intent in INTENTS]
    strategy = ScriptedStrategy(script)
    lines: list = []
    engine = Engine(strategy, settings, journal=lines.append)
    executor = NextOpenExecutor(costs=Costs(commission_per_side=commission))

    async def scenario() -> None:
        number = 0
        for day in days:
            for offset in range(len(INTENTS)):
                price = prices.get(number, PRICE)
                bar = candle(10, 5 + 5 * offset, close=price, day=day)
                await engine.on_market_candle(bar, executor)
                if changes and number in changes:
                    engine.apply_settings(changes[number])
                number += 1

    asyncio.run(scenario())
    return engine, executor, lines


def _day(settings: EngineSettings, exit_price: float, **rest):
    """Один день, свеча `EXIT_BAR` (исполнение выхода «через свечу») — на `exit_price`."""
    return _run(settings, {EXIT_BAR: exit_price}, **rest)


def _stop_lines(lines: list) -> list:
    return [line for line in lines if "первый убыток в окне — стоп" in line.reason]


def _ahead_lines(lines: list) -> list:
    return [line for line in lines if line.event.startswith("Переворота не будет")]


def test_a_losing_signal_exit_inside_the_window_closes_the_window() -> None:
    """Лонг закрыт обратным сигналом на 100 пунктов ниже входа — дальше входов нет.

    Переворот «через свечу»: вход подаётся только на следующей свече, после
    сделки выхода, и стоп ставит фактический результат. Без правила следующие
    сигналы дали бы шорт и ещё сделки. Строка журнала называет сумму чистыми:
    −100 пунктов и 34 ₽ комиссии — и окно, до конца которого входов нет.
    """
    engine, executor, lines = _day(SETTINGS, PRICE - 100)

    assert len(executor.deals) == 1, executor.deals
    assert engine.state.position is None
    assert engine.state.entry_in_flight() is None, "после убытка подан вход"
    said = _stop_lines(lines)
    assert said, [line.event for line in lines]
    assert "134" in said[0].reason, said[0].reason
    assert "10:05–11:00" in said[0].reason, said[0].reason


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
    строка стопа встала бы на время выхода, а метка окна осталась бы в памяти.
    """
    narrow = SETTINGS.replace(window=TradingWindow(time(10, 5), time(10, 20)))
    engine, executor, lines = _day(narrow, PRICE - 100, changes={EXIT_BAR: SETTINGS})

    first = executor.deals[0]
    assert first.exit_reason == "window_end", executor.deals
    assert not [line for line in _stop_lines(lines) if line.at == first.exit_time], (
        "выход по концу окна засчитан правилу «первый убыток в окне»"
    )
    assert len(executor.deals) > 1 or engine.state.position is not None, (
        "после выхода по концу окна в расширенном окне входов нет"
    )


def test_same_bar_reversal_with_a_losing_estimate_exits_without_entry() -> None:
    """`B-108`: обратный сигнал, закрытие на 100 ниже входа — подан только выход.

    Бой 2 16.11: лонг закрыт в убыток, и в той же свече открылся шорт.
    Теперь на закрытии свечи решение — по оценке (−100 пунктов − 34 ₽
    комиссии): вход в обратную сторону не подаётся, окно закрыто. Без оценки
    шорт открылся бы вместе с выходом и дал бы вторую сделку.
    """
    engine, executor, lines = _run(SAME_BAR, {SIGNAL_BAR: PRICE - 100, EXIT_BAR: PRICE - 100})

    assert len(executor.deals) == 1, executor.deals
    assert engine.state.position is None, "переворот в убыток подан"
    assert engine.state.entry_in_flight() is None, "после убыточной оценки подан вход"
    said = _ahead_lines(lines)
    assert said, [line.event for line in lines]
    assert "−134" in said[0].reason or "-134" in said[0].reason, said[0].reason


def test_same_bar_reversal_with_a_profitable_estimate_reverses() -> None:
    """`B-108`, обратная сторона: закрытие на 100 выше входа — переворот в той же свече.

    Оценка +100 − 34 ₽ не в минусе: выход и вход в шорт подаются вместе,
    шорт открывается в момент закрытия лонга. Правило не должно отнимать
    переворот у прибыльного выхода.
    """
    _engine, executor, lines = _run(
        SAME_BAR, {number: PRICE + 100 for number in range(SIGNAL_BAR, len(INTENTS))}
    )

    assert len(executor.deals) >= 2, executor.deals
    first, second = executor.deals[0], executor.deals[1]
    assert second.side is Side.SHORT, executor.deals
    assert second.entry_time == first.exit_time, "шорт открыт не в свече выхода из лонга"
    assert not [line for line in _ahead_lines(lines) if line.at <= first.exit_time], (
        "прибыльная оценка остановила переворот"
    )


def test_profitable_estimate_but_losing_fill_stops_after_the_deal() -> None:
    """Ограничение решения 0072: оценка +100, сделка выхода −100 — переворот уже ушёл.

    Закрытие свечи решения выше входа, а выход исполнился ниже (проскальзывание
    на открытии следующей). Шорт переворота открыт и доводится до выхода
    по обратному сигналу; стоп ставит фактический результат лонга, и после
    шорта входов нет.
    """
    prices = {SIGNAL_BAR: PRICE + 100}
    prices.update({number: PRICE - 100 for number in range(EXIT_BAR, len(INTENTS))})
    engine, executor, lines = _run(SAME_BAR, prices)

    assert [deal.side for deal in executor.deals] == [Side.LONG, Side.SHORT], executor.deals
    assert engine.state.position is None
    assert engine.state.entry_in_flight() is None, "после фактического убытка подан вход"
    assert _stop_lines(lines), [line.event for line in lines]


def test_a_stop_in_window_a_lets_entries_into_shifted_window_b() -> None:
    """`B-109`: стоп в окне 10:05–11:00, окно сдвинули на 10:20–11:30 — вход есть.

    Бой 2 16.11: после стопа в 12:05 владелец счёта сдвинул окно 11:45–12:20
    на 12:10–12:40 и ждал входов — «должно быть снова до первого убытка».
    Окно, изменённое в настройках, — новое окно, счёт заново. Без сдвига
    (первый тест) после того же убытка входов нет.
    """
    engine, executor, _lines = _day(
        SETTINGS, PRICE - 100, changes={EXIT_BAR + 1: SETTINGS.replace(window=SHIFTED)}
    )

    assert len(executor.deals) > 1 or engine.state.position is not None, (
        "после сдвига окна в новом окне входов нет"
    )


def test_a_stop_today_does_not_close_tomorrow() -> None:
    """Стоп в пятницу в окне 10:05–11:00 — в понедельник в том же окне входы есть.

    Метка — окно **на своей дате**: те же границы на другой дате — другое
    окно. Пятница после стопа без входов, понедельник с ними.
    """
    engine, executor, _lines = _run(SETTINGS, {EXIT_BAR: PRICE - 100}, days=(19, NEXT_DAY))

    friday = [deal for deal in executor.deals if deal.entry_time.day == 19]
    monday = [deal for deal in executor.deals if deal.entry_time.day == NEXT_DAY]
    assert len(friday) == 1, executor.deals
    assert monday or engine.state.position is not None, "на следующий день входов нет"


def test_a_window_over_midnight_is_one_window_on_two_dates() -> None:
    """Окно 23:00–01:00: 23:30 и 00:30 следующей даты — одно окно, 23:30 следующей — новое.

    Стоп до конца окна, а не даты: в 00:30 окно, открытое накануне, ещё идёт,
    и счёт по нему не начинается заново оттого, что сменилась дата. Граница
    строгая, как у `contains`: 01:00 — уже вне окна.
    """
    night = TradingWindow(time(23, 0), time(1, 0))
    evening = night.running_at(datetime(2026, 6, 19, 23, 30, tzinfo=MSK))
    after_midnight = night.running_at(datetime(2026, 6, 20, 0, 30, tzinfo=MSK))
    next_evening = night.running_at(datetime(2026, 6, 20, 23, 30, tzinfo=MSK))

    assert evening == WindowDay(opened=date(2026, 6, 19), window=night), evening
    assert after_midnight == evening, after_midnight
    assert next_evening is not None and next_evening != evening, next_evening
    assert night.running_at(datetime(2026, 6, 20, 1, 0, tzinfo=MSK)) is None


def test_take_profit_close_line_does_not_speak_of_stop_after_take() -> None:
    """Строка «Лонг закрыт» по тейку при этом варианте не упоминает «стоп после тейка».

    Бой 2 16.11: «Шорт закрыт … если включено правило «стоп после тейка»…»
    при выбранном «первый убыток — стоп» — про чужую настройку, сбивает.
    Контроль: при включённом «стопе после тейка» та же сделка его называет —
    иначе тест мог бы молчать оттого, что тейка не было вовсе.
    """
    taking = SETTINGS.replace(take_profit=True, take_profit_percent=0.5)
    above_take = {number: PRICE * 1.006 for number in range(SIGNAL_BAR, len(INTENTS))}

    def close_lines(settings: EngineSettings) -> list:
        _engine, _executor, lines = _run(settings, above_take)
        return [line for line in lines if line.event == "Лонг закрыт"]

    variant = close_lines(taking)
    control = close_lines(taking.replace(stop_after_take_profit=True, stop_after_loss=False))

    assert variant, "тейк не сработал — тесту нечего проверять"
    assert "стоп после тейка" not in variant[0].reason, variant[0].reason
    assert control and "стоп после тейка" in control[0].reason, control
