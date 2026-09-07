"""Прогон по истории: запись результата, итог за период, пары «прогноз — факт».

⚠️ **Правил исполнения здесь нет.** Они переехали на Э1-9 в
`backtest/execution.py`, и проверяются в `tests/test_backtest_execution.py`:
исполнение по открытию следующей свечи, сторож уровня внутри бара, проверка
«заявка не исполняется раньше, чем подана», комиссия и проскальзывание.

Здесь — то, что делает `backtest/history.py`: закрытая сделка с деньгами,
расчётная цена выхода по сработавшему уровню, показатели за период и сведение
расчёта с фактом. Ошибка в них меняет не список сделок, а **числа в отчёте**,
и заметить её сверкой с прототипом нельзя: сверка сравнивает сделки.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from engine import (
    DayMarks,
    EngineSettings,
    ExitReason,
    Mode,
    OrderAction,
    OrderRequest,
    Side,
    TradingWindow,
)
from strategies import EmaReverse, EmaReverseSettings

from backtest import (
    Costs,
    Deal,
    HistoryExecutor,
    HistorySource,
    Tariff,
    pairs,
    replay,
    reversal_entries,
    summarise,
)
from tests.engine_helpers import MSK, STEP, candle


def _order(action: OrderAction, side: Side, at: datetime, **rest) -> OrderRequest:
    return OrderRequest(
        action=action, side=side, volume=1.0, submitted_at=at,
        reason="тест", order_id=f"{action.value}:{at.isoformat()}", **rest,
    )


def _deal(
    side: Side = Side.LONG,
    entry: float = 100_000.0,
    exit_: float = 100_100.0,
    reason: ExitReason = ExitReason.SIGNAL,
    commission: float | None = None,
    entry_id: str = "open:1",
    at: datetime | None = None,
) -> Deal:
    at = at or datetime(2026, 6, 19, 10, 10, tzinfo=MSK)
    return Deal(
        side=side, volume=1.0,
        entry_time=at, entry_price=entry, entry_order_id=entry_id,
        exit_time=at + STEP, exit_price=exit_, exit_order_id="close:1",
        exit_reason=reason, commission=commission,
    )


def _three_deals(commission: float | None = 28.0) -> list[Deal]:
    """Три сделки, посчитанные на бумаге. Все числа ниже — из этого набора.

    Ходы цены: +300, −100, +100 пунктов при цене входа 100 000 и одном
    контракте, комиссия 28 ₽ на сделку (обе стороны). Значит:

    ==========  ==========  =========  =====================================
    Вход (МСК)  Валовая ₽   Чистая ₽   День
    ==========  ==========  =========  =====================================
    22.06 10:10      +300       +272   понедельник
    23.06 11:15      −100       −128   вторник
    23.06 10:40      +100        +72   вторник
    ==========  ==========  =========  =====================================

    Итого валовых +300, комиссии 84, чистых +216. Профит-фактор чистыми
    (272 + 72) / 128 = 2,6875; валовыми (300 + 100) / 100 = 4,0.

    Порядок сделок в списке намеренно не совпадает с порядком времени:
    кривая по дням обязана строиться по датам, а не по месту в списке.
    """
    return [
        _deal(exit_=100_300.0, commission=commission, entry_id="open:1",
              at=datetime(2026, 6, 22, 10, 10, tzinfo=MSK)),
        _deal(exit_=99_900.0, commission=commission, entry_id="open:2",
              at=datetime(2026, 6, 23, 11, 15, tzinfo=MSK)),
        _deal(exit_=100_100.0, commission=commission, entry_id="open:3",
              at=datetime(2026, 6, 23, 10, 40, tzinfo=MSK)),
    ]


# ------------------------------------------------------------ запись результата
#
# ⚠️ Правила исполнения проверяются НЕ здесь. Они переехали на Э1-9
# в `backtest/execution.py`, и их тесты — в `tests/test_backtest_execution.py`.
# Здесь остаётся то, что `HistoryExecutor` к ним добавляет: закрытая сделка
# с деньгами и расчётная цена выхода по сработавшему уровню.

def test_a_closed_position_becomes_a_deal_with_both_legs() -> None:
    """Вход и выход сходятся в одну запись, и обе ноги названы своими заявками."""
    executor = HistoryExecutor(commission_per_side=14.0)
    entry = _order(OrderAction.OPEN, Side.LONG, datetime(2026, 6, 19, 10, 10, tzinfo=MSK))
    exit_ = _order(
        OrderAction.CLOSE, Side.LONG, datetime(2026, 6, 19, 10, 15, tzinfo=MSK),
        exit_reason=ExitReason.WINDOW_END,
    )

    async def go():
        await executor.submit(entry)
        await executor.fills_at(replace(candle(10, 10), open=100_000.0))
        await executor.submit(exit_)
        await executor.fills_at(replace(candle(10, 15), open=100_100.0))

    asyncio.run(go())
    deal = executor.deals[0]
    assert (deal.entry_order_id, deal.exit_order_id) == (entry.order_id, exit_.order_id)
    assert deal.exit_reason is ExitReason.WINDOW_END, (
        "причина взята не из заявки движка"
    )
    assert deal.gross == pytest.approx(100.0)
    assert deal.commission == pytest.approx(28.0), "комиссия сделки — обе стороны"
    assert deal.net == pytest.approx(72.0)


def test_a_triggered_level_is_written_down_as_a_calculation_too() -> None:
    """Где робот собирался выйти по тейку — отдельная строка слоя «прогноз».

    Заявка вооружения рождается из сделки открытия и до вызывающего не доходит:
    он видит только заявки разбора свечи. Значит записать расчёт может лишь
    сама модель.

    ⚠️ Время расчёта — **закрытие** бара, а не его начало: у расчёта время
    всегда в этом соглашении, у сделки — в своём.
    """
    executor = HistoryExecutor()
    placed_at = datetime(2026, 6, 19, 10, 10, tzinfo=MSK)

    async def go():
        await executor.submit(_order(OrderAction.OPEN, Side.LONG, placed_at))
        await executor.fills_at(replace(candle(10, 10), open=100.0))
        await executor.submit(_order(
            OrderAction.ARM_TAKE_PROFIT, Side.LONG, placed_at, price=100.5,
        ))
        return await executor.fills_at(
            replace(candle(10, 15), open=100.2, high=101.0, low=99.0, close=100.2)
        )

    fills = asyncio.run(go())
    assert [fill.price for fill in fills] == [100.5]
    assert executor.deals[-1].exit_reason is ExitReason.TAKE_PROFIT
    plan = executor.level_plans[-1]
    assert plan.price == 100.5 and plan.exit_reason is ExitReason.TAKE_PROFIT
    assert plan.decided_at == datetime(2026, 6, 19, 10, 20, tzinfo=MSK), (
        "у расчёта время — закрытие бара, а не его начало"
    )


def test_without_a_tariff_the_commission_is_not_substituted_with_zero() -> None:
    """`None` — «тариф не задан», а не «комиссии нет» (DOMAIN.md §5)."""
    without_tariff = HistoryExecutor()
    with_tariff = HistoryExecutor(commission_per_side=14.0)
    placed_at = datetime(2026, 6, 19, 10, 10, tzinfo=MSK)

    async def go(executor):
        await executor.submit(_order(OrderAction.OPEN, Side.LONG, placed_at))
        return await executor.fills_at(candle(10, 10, close=100.0))

    assert asyncio.run(go(without_tariff))[0].commission is None
    assert without_tariff.commission_per_side is None
    assert asyncio.run(go(with_tariff))[0].commission == 14.0
    assert with_tariff.commission_per_side == 14.0


def test_the_tariff_may_be_given_as_a_breakdown_instead_of_one_figure() -> None:
    """ТЗ §4.4 Е: биржевой сбор из карточки инструмента плюс 1 ₽ брокеру."""
    executor = HistoryExecutor(costs=Costs(tariff=Tariff(exchange_fee=13.0)))
    assert executor.commission_per_side == pytest.approx(14.0)


def test_a_second_commission_named_in_the_same_call_is_refused() -> None:
    """Издержки и отдельный параметр назвали разные цифры — выбрать нельзя.

    Вторая дверь к той же ошибке, что закрыта в `replay`. До 03.09.2026
    параметр молча затирал комиссию внутри издержек: сторож `Costs` сравнивает
    разбивку с общей цифрой, а разбивки здесь нет вовсе, и `replace` просто
    ставил 1 ₽ поверх 14 ₽. `HistoryExecutor` — публичный вход `backtest`,
    то есть обход сторожа `replay` был доступен снаружи.

    Цена — 254 ₽ комиссии вместо 3 556 ₽ на эталонных 127 сделках, «чистая
    прибыль» 6 225 ₽ вместо 2 923 ₽. Колонка комиссии при этом непустая,
    то есть отчёт неотличим от правды.
    """
    with pytest.raises(ValueError, match="названа дважды") as by_number:
        HistoryExecutor(
            costs=Costs(commission_per_side=14.0, price_step=5.0, slippage_steps=1.0),
            commission_per_side=1.0,
        )
    assert "14.0" in str(by_number.value) and "1.0 " in str(by_number.value), (
        "в отказе нет обеих цифр, а без них расхождение искать негде"
    )

    with pytest.raises(ValueError, match="названа дважды"):
        HistoryExecutor(
            costs=Costs(tariff=Tariff(exchange_fee=13.0)), commission_per_side=1.0
        )


def test_the_separate_number_fills_in_costs_that_name_only_the_slippage() -> None:
    """Незаполненную половину дозаполнить можно: это одна цифра, а не две.

    Проскальзывание — величина издержек, комиссия задана отдельным числом:
    затирать нечего, и отказ здесь был бы придиркой. Совпадение цифр,
    записанных по-разному, — тоже не расхождение: сверяются деньги, а не буквы.
    """
    filled = HistoryExecutor(
        costs=Costs(price_step=5.0, slippage_steps=1.0), commission_per_side=14.0
    )
    assert filled.commission_per_side == pytest.approx(14.0)
    assert filled.costs.slippage == pytest.approx(5.0), "издержки потеряли шаг цены"

    agreeing = HistoryExecutor(
        costs=Costs(tariff=Tariff(exchange_fee=13.0)), commission_per_side=14.0
    )
    assert agreeing.commission_per_side == pytest.approx(14.0)


# ------------------------------------------------------------------- источник

def test_the_source_yields_every_candle_and_counts_progress() -> None:
    series = [candle(10, 5 * i % 60) for i in range(10)]
    progress_reports: list[tuple[int, int]] = []
    source = HistorySource(series, on_progress=lambda d, t: progress_reports.append((d, t)),
                           breathe_every=4)

    async def go():
        return [item async for item in source.candles()]

    assert len(asyncio.run(go())) == 10
    assert progress_reports[-1] == (10, 10)
    assert progress_reports[0] == (4, 10), "передышка обязана быть, иначе окно замрёт"


def test_the_breather_must_be_positive() -> None:
    with pytest.raises(ValueError):
        HistorySource([], breathe_every=0)


# ---------------------------------------------------------------------- итог

def test_the_summary_counts_gross_net_and_drawdown() -> None:
    closed_deals = [
        _deal(exit_=100_100.0, commission=28.0),   # +100 валовых, +72 чистых
        _deal(exit_=99_900.0, commission=28.0),    # −100 валовых, −128 чистых
    ]
    summary = summarise(closed_deals)
    assert summary.trades == 2
    assert summary.gross_profit == 0.0
    assert summary.commission == 56.0
    assert summary.net_profit == -56.0
    assert summary.profitable == 1
    assert summary.profitable_share == 0.5
    assert summary.max_drawdown == 128.0


def test_without_a_tariff_the_net_profit_is_not_computed() -> None:
    summary = summarise([_deal(), _deal(exit_=99_900.0, commission=28.0)])
    assert summary.commission is None, "часть сделок без тарифа — итог не полон"
    assert summary.net_profit is None
    assert summary.gross_profit == 0.0


def test_a_reversal_is_a_change_of_side_on_the_opposite_signal() -> None:
    closed_deals = [
        _deal(side=Side.LONG, reason=ExitReason.SIGNAL, entry_id="open:1"),
        _deal(side=Side.SHORT, reason=ExitReason.WINDOW_END, entry_id="open:2"),
        _deal(side=Side.LONG, reason=ExitReason.SIGNAL, entry_id="open:3"),
    ]
    assert reversal_entries(closed_deals) == frozenset({"open:2"})
    assert summarise(closed_deals).reversals == 1
    # Выход по концу окна переворотом не считается: робот вышел из рынка.
    assert "open:3" not in reversal_entries(closed_deals)


def test_an_empty_list_of_deals_gives_an_empty_summary() -> None:
    """Пустой итог — `None` там, где числа нет, и пустые разбивки.

    Ноль в отчёте читается как результат, а «сделок не было» результатом
    не является. Падать на пустом списке нельзя тем более: прогон, в котором
    робот не сделал ни одной сделки, — обычный исход, а не отказ.
    """
    summary = summarise([])
    assert summary.trades == 0 and summary.net_profit is None
    assert summary.profit_factor is None
    assert summary.average_trade is None
    assert summary.best_trade is None and summary.worst_trade is None
    assert summary.by_day == () and summary.by_weekday == () and summary.by_hour == ()


# --------------------------------------------- показатели отчёта, ТЗ §4.10 Б

def test_the_profit_factor_is_the_wins_divided_by_the_losses() -> None:
    """Считано на бумаге: (272 + 72) / 128 = 2,6875.

    Чистыми, а не валовыми, потому что тариф задан. Те же сделки без тарифа
    дают (300 + 100) / 100 = 4,0 — разница ровно в комиссии. Отсюда же
    следует, что с PF прототипа (1,50 в DOMAIN.md §4) наше число сравнивать
    нельзя: там оно посчитано по валовой.
    """
    assert summarise(_three_deals()).profit_factor == pytest.approx(344 / 128)
    assert summarise(_three_deals(commission=None)).profit_factor == pytest.approx(4.0)


def test_without_a_single_loss_the_profit_factor_is_undefined() -> None:
    """Ноль убытков — «делить не на что», а не «бесконечно хорошо».

    Подставленная бесконечность вывела бы такой прогон на первое место
    в таблице перебора, а три сделки подряд в плюс результатом не являются.
    """
    only_wins = [_deal(exit_=100_300.0, commission=28.0)]
    assert summarise(only_wins).profit_factor is None
    assert summarise(only_wins).trades == 1, "сама сделка при этом посчитана"


def test_a_deal_exactly_at_zero_counts_in_neither_sum() -> None:
    """Сделка ровно в ноль: ни в числитель профит-фактора, ни в знаменатель."""
    without_zero = summarise(_three_deals())
    # +28 валовых при комиссии 28 ₽ — это ровно ноль чистыми.
    with_zero = summarise([
        *_three_deals(),
        _deal(exit_=100_028.0, commission=28.0, entry_id="open:4"),
    ])
    assert with_zero.profit_factor == without_zero.profit_factor
    assert with_zero.profitable == without_zero.profitable
    assert with_zero.trades == without_zero.trades + 1


def test_the_average_best_and_worst_trade_are_counted_on_the_known_money() -> None:
    """+272, −128, +72 чистыми: средняя 72, лучшая 272, худшая −128."""
    with_tariff = summarise(_three_deals())
    assert with_tariff.average_trade == pytest.approx(72.0)
    assert with_tariff.best_trade == pytest.approx(272.0)
    assert with_tariff.worst_trade == pytest.approx(-128.0)
    # Без тарифа те же сделки считаются по валовой: +300, −100, +100.
    without_tariff = summarise(_three_deals(commission=None))
    assert without_tariff.average_trade == pytest.approx(100.0)
    assert without_tariff.best_trade == pytest.approx(300.0)
    assert without_tariff.worst_trade == pytest.approx(-100.0)


def test_the_daily_curve_shows_the_day_and_the_running_total() -> None:
    """Понедельник +272; вторник −128 и +72, то есть −56. Накопленно 272 и 216."""
    summary = summarise(_three_deals())
    curve = summary.by_day
    assert [(point.day, point.trades) for point in curve] == [
        (date(2026, 6, 22), 1), (date(2026, 6, 23), 2),
    ]
    assert [point.profit for point in curve] == pytest.approx([272.0, -56.0])
    assert [point.cumulative for point in curve] == pytest.approx([272.0, 216.0])
    assert curve[-1].cumulative == pytest.approx(summary.net_profit)


def test_the_breakdowns_split_by_the_weekday_and_the_hour_of_entry() -> None:
    """Понедельник и вторник; часы 10 и 11 — по времени ВХОДА."""
    summary = summarise(_three_deals())
    assert [(row.key, row.trades, row.profitable) for row in summary.by_weekday] == [
        (0, 1, 1),   # понедельник 22.06: одна сделка, прибыльная
        (1, 2, 1),   # вторник 23.06: две сделки, прибыльная одна
    ]
    assert [row.profit for row in summary.by_weekday] == pytest.approx([272.0, -56.0])
    assert [(row.key, row.trades, row.profitable) for row in summary.by_hour] == [
        (10, 2, 2), (11, 1, 0),
    ]
    assert [row.profit for row in summary.by_hour] == pytest.approx([344.0, -128.0])


def test_hours_and_weekdays_without_deals_do_not_appear() -> None:
    """Строка «среда: 0 ₽» читалась бы как результат, а сделок в среду не было."""
    summary = summarise(_three_deals())
    assert len(summary.by_weekday) == 2, "семь строк — это пять выдуманных"
    assert len(summary.by_hour) == 2, "двадцать четыре строки — двадцать две выдуманных"


def test_the_breakdown_takes_the_entry_moment_in_moscow() -> None:
    """Час и день недели — по МСК и по времени входа, а не выхода.

    Сделка открыта 22.06 в 22:10 UTC. По Москве это уже 23.06, 01:10, вторник.
    Оставленный UTC положил бы её в понедельник, в час 22 — и разбивка
    показала бы не то время, которое подписано.
    """
    entered = datetime(2026, 6, 22, 22, 10, tzinfo=timezone.utc)
    summary = summarise([_deal(commission=28.0, at=entered)])
    assert summary.by_hour[0].key == 1
    assert summary.by_weekday[0].key == 1
    assert summary.by_day[0].day == date(2026, 6, 23)


def test_a_naive_entry_time_is_refused_instead_of_shifting_the_hours() -> None:
    """Момент без пояса не роняет расчёт сам — он молча меняет ответ."""
    naive = datetime(2026, 6, 22, 10, 10)
    with pytest.raises(ValueError, match="часового пояса"):
        summarise([_deal(commission=28.0, at=naive)])


def test_the_three_breakdowns_add_up_to_the_same_money() -> None:
    """Одни и те же сделки разнесены трижды — суммы обязаны сойтись."""
    summary = summarise(_three_deals())
    for rows in (summary.by_day, summary.by_weekday, summary.by_hour):
        assert sum(row.profit for row in rows) == pytest.approx(summary.net_profit)
        assert sum(row.trades for row in rows) == summary.trades
    assert sum(row.profitable for row in summary.by_weekday) == summary.profitable
    assert sum(row.profitable for row in summary.by_hour) == summary.profitable


# ------------------------------------------------------- прогон и пары слоёв

def _bars(count: int = 200) -> list:
    """Ряд с колебанием — чтобы средняя пересекалась и решения были."""
    import math

    series = []
    first_moment = datetime(2026, 6, 19, 7, 0, tzinfo=MSK)
    for index in range(count):
        level = 100000.0 + 300.0 * math.sin(index / 4.0)
        series.append(replace(
            candle(7, 0, close=level),
            time=first_moment + index * STEP,
            high=level + 50.0, low=level - 50.0,
        ))
    return series


def _run(settings: EngineSettings, *, costs: Costs | None = None):
    return asyncio.run(replay(
        _bars(), EmaReverse(EmaReverseSettings(period=15)), settings, costs=costs
    ))


def test_a_run_yields_deals_a_journal_and_an_average() -> None:
    outcome = _run(EngineSettings(mode=Mode.REVERSE, window=TradingWindow()))
    assert outcome.bars == 200
    assert outcome.deals, "на колеблющемся ряде робот обязан что-то сделать"
    assert outcome.journal, "журнал решений пуст"
    assert len(outcome.average) > 100
    assert outcome.halted == ""


def test_a_run_repeats_exactly() -> None:
    """Детерминированность — условие сверки с прототипом."""
    first_run = _run(EngineSettings(mode=Mode.REVERSE))
    second_run = _run(EngineSettings(mode=Mode.REVERSE))
    assert first_run.deals == second_run.deals
    assert [(e.at, e.event) for e in first_run.journal] == [
        (e.at, e.event) for e in second_run.journal
    ]


def test_every_deal_found_its_calculation() -> None:
    """Факт без прогноза разбирается как дефект (DOMAIN.md §8).

    В прогоне по истории таких быть не должно ни одного: каждая сделка
    родилась из заявки движка, а у заявки есть расчётная цена.
    """
    outcome = _run(EngineSettings(mode=Mode.REVERSE))
    matched = pairs(outcome)
    assert matched
    assert [pair for pair in matched if pair.plan is None] == []
    assert len(matched) == len(outcome.fills)


def test_the_sign_of_a_mismatch_follows_the_meaning_of_the_operation() -> None:
    """Покупке выгодна цена пониже, продаже — повыше."""
    outcome = _run(EngineSettings(mode=Mode.REVERSE))
    for pair in pairs(outcome):
        if pair.plan is None or pair.fill is None:
            continue
        is_a_sale = (
            (pair.plan.action is OrderAction.OPEN and pair.plan.side is Side.SHORT)
            or (pair.plan.action is OrderAction.CLOSE and pair.plan.side is Side.LONG)
        )
        expected = (
            pair.fill.price - pair.plan.price if is_a_sale
            else pair.plan.price - pair.fill.price
        )
        assert pair.favour_points == pytest.approx(expected)


def test_a_run_fills_every_indicator_of_the_report() -> None:
    """Показатели ТЗ §4.10 Б есть после обычного прогона, а не только в юните."""
    summary = _run(EngineSettings(mode=Mode.REVERSE, window=TradingWindow())).summary
    assert summary.trades > 0
    assert summary.by_day, "кривой по дням нет"
    assert summary.by_hour and summary.by_weekday, "разбивок нет"
    assert summary.average_trade is not None
    assert summary.best_trade is not None and summary.worst_trade is not None
    assert sum(point.trades for point in summary.by_day) == summary.trades


def test_a_switched_off_robot_makes_no_deals() -> None:
    outcome = _run(EngineSettings(mode=Mode.OFF))
    assert outcome.deals == ()
    assert outcome.journal, "молчание тоже объясняется строкой"


# --------------------------------------------------------------------- издержки

def test_the_run_says_on_which_costs_it_was_counted() -> None:
    """Тариф лежит в результате, а не только в настройках.

    «Прибыль столько-то» без строки «комиссия столько-то при таком-то тарифе»
    читается как результат стратегии, а является результатом стратегии
    на конкретном тарифе.
    """
    outcome = _run(
        EngineSettings(
            mode=Mode.REVERSE, window=TradingWindow(), commission_per_side=14.0
        ),
        costs=Costs(tariff=Tariff(exchange_fee=13.0)),
    )
    assert outcome.costs.per_side == pytest.approx(14.0)
    assert outcome.costs.tariff is not None
    assert outcome.costs.tariff.broker_fee == pytest.approx(1.0)


def test_the_tariff_comes_from_the_settings_when_the_costs_are_not_given() -> None:
    """Не задали издержки — берётся тариф движка и нулевое проскальзывание."""
    outcome = _run(
        EngineSettings(mode=Mode.REVERSE, window=TradingWindow(), commission_per_side=14.0)
    )
    assert outcome.costs.per_side == pytest.approx(14.0)
    assert outcome.costs.slippage == 0.0


def test_the_commission_changes_the_money_and_not_a_single_deal() -> None:
    """Комиссия — единственная величина, которую можно менять, не меняя сделок.

    Прототип её не читает вообще: ни объём, ни условия входа и выхода от неё
    не зависят (PROTOTYPE.md §6). Следствие для сверки: сделки совпали,
    а деньги нет — расходится комиссия; сделки разошлись — комиссия ни при чём.

    ⚠️ Тариф меняется **в обоих местах сразу** — и у движка, и у издержек
    отчёта, — потому что порознь их менять `replay` больше не даёт: две разные
    цифры комиссии в одном прогоне отвергаются вслух. Значит проверяется
    заодно и цифра движка: `commission_per_side` читает только строка журнала
    про уровень, и на список сделок она не влияет.
    """
    free = _run(EngineSettings(mode=Mode.REVERSE, window=TradingWindow()))
    charged = _run(
        EngineSettings(
            mode=Mode.REVERSE, window=TradingWindow(), commission_per_side=14.0
        ),
        costs=Costs(commission_per_side=14.0),
    )

    assert [(deal.entry_time, deal.exit_time, deal.exit_price) for deal in free.deals] == [
        (deal.entry_time, deal.exit_time, deal.exit_price) for deal in charged.deals
    ], "комиссия изменила список сделок — значит она попала в решения"
    assert free.summary.gross_profit == pytest.approx(charged.summary.gross_profit)
    assert free.summary.commission is None and free.summary.net_profit is None
    assert charged.summary.commission == pytest.approx(28.0 * charged.summary.trades)
    assert charged.summary.net_profit == pytest.approx(
        charged.summary.gross_profit - charged.summary.commission
    )


def test_two_different_commissions_in_one_run_are_refused_with_both_figures() -> None:
    """Комиссия живёт в двух местах, и разойтись им прогон не даёт.

    Движок по `settings.commission_per_side` судит, окупает ли цель тейка
    комиссию обеих сторон (ТЗ §4.4 В), и пишет об этом строку журнала. Отчёт
    по `costs.per_side` считает комиссию и чистую прибыль. Внутри `Costs`
    запрет на две цифры уже стоял, а через границу прогона то же самое
    проходило молча.

    Обе стороны расхождения одинаково опасны:

    * тариф только у отчёта — итог называет комиссию и чистую прибыль
      числами, а журнал того же прогона на каждой позиции пишет «окупает ли
      цель комиссию — НЕ ПРОВЕРЕНО, тариф движку не задан»;
    * разные числа — движок судит по 100 ₽ за сделку, отчёт считает по 28 ₽,
      сделки при этом одни и те же, и по отчёту не понять, которая настоящая.

    Отказ обязан назвать **обе** цифры: без них искать, где именно разошлось,
    придётся по коду.
    """
    window = TradingWindow()
    with pytest.raises(ValueError, match="две разные цифры комиссии") as only_report:
        _run(
            EngineSettings(mode=Mode.REVERSE, window=window),
            costs=Costs(commission_per_side=14.0),
        )
    assert "тарифа нет" in str(only_report.value)
    assert "14.0" in str(only_report.value)

    with pytest.raises(ValueError, match="две разные цифры комиссии") as only_engine:
        _run(
            EngineSettings(
                mode=Mode.REVERSE, window=window, commission_per_side=14.0
            ),
            costs=Costs(price_step=5.0, slippage_steps=1.0),
        )
    assert "тарифа нет" in str(only_engine.value)

    with pytest.raises(ValueError, match="две разные цифры комиссии") as both:
        _run(
            EngineSettings(
                mode=Mode.REVERSE, window=window, commission_per_side=50.0
            ),
            costs=Costs(commission_per_side=14.0),
        )
    assert "50.0" in str(both.value) and "14.0" in str(both.value), (
        "в отказе нет обеих цифр, а без них расхождение искать негде"
    )


def test_the_same_commission_in_both_places_passes_however_it_is_written() -> None:
    """Совпали — прогон идёт, даже если записаны они по-разному.

    Разбивка ТЗ §4.4 Е (13 ₽ биржа + 1 ₽ брокер) и «14 ₽ за контракт
    на сторону» — одно и то же число, и требовать одинаковой ЗАПИСИ было бы
    придиркой: сверяются деньги, а не буквы.
    """
    outcome = _run(
        EngineSettings(
            mode=Mode.REVERSE, window=TradingWindow(), commission_per_side=14.0
        ),
        costs=Costs(tariff=Tariff(exchange_fee=13.0)),
    )
    assert outcome.costs.per_side == pytest.approx(14.0)


def test_an_explicit_zero_commission_runs_and_shows_itself_in_the_report() -> None:
    """Явный ноль проходит насквозь — и виден в отчёте, а не притворяется.

    Решение записано у поля `EngineSettings.commission_per_side` и разобрано
    ещё раз 03.09.2026: `None` означает «тариф не задан», ноль — «комиссии
    нет». Слиться им негде: колонка комиссии показывает 0 ₽, чистая прибыль
    равна валовой, и оба числа названы, а не пусты. Из окна ноль недостижим —
    `ui/settings_dialog.py` переводит «меньше либо равно нулю» в `None`.

    ⚠️ Прогон здесь обязан **пройти**, а не упасть. Запрети `Costs` явный
    ноль — и настройки, которые движок считает законными, стали бы
    непрогоняемыми по истории: `replay` требует, чтобы цифра движка и цифра
    отчёта совпадали, а подставить вместо нуля `None` значило бы посчитать
    отчёт не по тем настройкам, которые дали.
    """
    outcome = _run(
        EngineSettings(
            mode=Mode.REVERSE, window=TradingWindow(), commission_per_side=0.0
        )
    )
    assert outcome.deals, "прогон не сделал ни сделки — сравнивать нечего"
    assert outcome.costs.per_side == 0.0
    assert outcome.summary.commission == 0.0, "ноль комиссии стал пустотой"
    assert outcome.summary.net_profit == pytest.approx(outcome.summary.gross_profit)
    assert outcome.summary.net_profit is not None, (
        "чистая прибыль не посчитана — ноль прочитан как «тариф неизвестен»"
    )


def test_slippage_changes_the_money_and_shows_up_as_a_worse_fill() -> None:
    """Проскальзывание видно в наложении расчёта на факт — и всегда в минус.

    ⚠️ **Список сделок оно меняет тоже**, и этим отличается от комиссии:
    цена входа сдвигается, от неё считается уровень тейка, и дальше расходится
    всё. Поэтому сверка с прототипом гонится только на нулевом проскальзывании.
    """
    settings = EngineSettings(mode=Mode.REVERSE, window=TradingWindow())
    exact = _run(settings)
    slipping = _run(settings, costs=Costs(price_step=5.0, slippage_steps=1.0))

    assert slipping.costs.slippage == pytest.approx(5.0)
    favours = [
        pair.favour_points for pair in pairs(slipping)
        if pair.favour_points is not None
    ]
    assert favours, "пар «расчёт — факт» не нашлось, проверка была бы вакуумной"
    assert all(value <= 0 for value in favours), (
        "проскальзывание оказалось в пользу позиции — знак перепутан"
    )
    assert slipping.summary.gross_profit < exact.summary.gross_profit, (
        "издержка исполнения не дошла до результата"
    )


def test_a_marked_day_makes_the_history_run_silent() -> None:
    """Отметка календаря действует и на прогоне по истории — сквозной сторож.

    Требование задачи `F-002`: иначе проверка на истории перестаёт отвечать
    на вопрос «что было бы». Проверяется на **том же ряде**, что и обычный
    прогон: сделки есть без отметки и исчезают с ней — то есть меняет их
    именно она, а не другой ряд.

    Весь ряд `_bars()` лежит внутри 19.06.2026, поэтому одной отметки хватает.
    """
    window = TradingWindow()
    ordinary = _run(EngineSettings(mode=Mode.REVERSE, window=window))
    assert ordinary.deals, "на колеблющемся ряде без отметок сделки обязаны быть"

    marked = _run(EngineSettings(
        mode=Mode.REVERSE,
        window=window,
        calendar=DayMarks.of({date(2026, 6, 19): False}),
    ))
    assert not marked.deals, "помеченный день не остановил прогон по истории"
    assert any(
        "пометили этот день нерабочим" in entry.reason for entry in marked.journal
    ), "в журнале прогона не сказано, почему робот молчал"
