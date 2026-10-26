"""Подставные данные окна сходятся с собственным контрактом (B-018).

`TradeRow.profit_rub` — результат сделки уже **после** комиссии
(`app/convert.py::trade_row`). Итог `synthetic.summary()` и дневной результат
`synthetic.state()` обязаны складываться из тех же строк `synthetic.trades()`
по тому же правилу. Иначе любой сторож, сверяющий сумму строк с итогом,
уйдёт искать в коде ошибку, которой там нет.
"""

from __future__ import annotations

import pytest

from tests import synthetic


def test_net_result_of_the_summary_is_the_sum_of_trade_results() -> None:
    """Чистая прибыль итога — сумма `profit_rub` строк: строки уже чистые."""
    rows = synthetic.trades()
    total = synthetic.summary()
    assert total.net_profit_rub == pytest.approx(sum(row.profit_rub or 0.0 for row in rows))


def test_gross_result_is_net_plus_nonzero_commission_of_the_same_trades() -> None:
    """Валовая = чистая + комиссия тех же сделок, и комиссия ненулевая."""
    rows = synthetic.trades()
    total = synthetic.summary()
    commission = sum(row.commission_rub or 0.0 for row in rows)
    assert commission > 0
    assert total.commission_rub == pytest.approx(commission)
    assert total.net_profit_rub is not None
    assert total.gross_profit_rub == pytest.approx(total.net_profit_rub + commission)


def test_profit_factor_is_counted_from_net_trade_results() -> None:
    """Профит-фактор — прибыльные чистыми против убыточных чистыми."""
    results = [row.profit_rub or 0.0 for row in synthetic.trades()]
    won = sum(value for value in results if value > 0)
    lost = -sum(value for value in results if value < 0)
    assert synthetic.summary().profit_factor == pytest.approx(won / lost)


def test_day_result_of_the_state_matches_the_trades_of_the_day() -> None:
    """Дневной результат в состоянии — тот же чистый итог и та же комиссия."""
    total = synthetic.summary()
    day = synthetic.state()
    assert day.day_profit_rub == pytest.approx(total.net_profit_rub)
    assert day.day_commission_rub == pytest.approx(total.commission_rub)
    assert day.day_trades == total.trades
