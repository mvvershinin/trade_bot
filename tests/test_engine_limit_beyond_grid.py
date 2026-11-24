"""Предельная цена на сетке: цена, лежащая ровно на сетке, не сдвигается (решение 0067)."""

from __future__ import annotations

import pytest

from engine import limit_beyond


@pytest.mark.parametrize(
    ("price", "percent", "step", "buying", "expected"),
    [
        (100.0, 0.1, 0.1, True, 100.1),
        (100.0, 0.1, 0.1, False, 99.9),
        (1000.0, 0.5, 0.5, True, 1005.0),
        (1000.0, 0.5, 0.5, False, 995.0),
    ],
    ids=["покупка 0,1", "продажа 0,1", "покупка 0,5", "продажа 0,5"],
)
def test_a_limit_that_lies_exactly_on_the_grid_is_not_pushed_a_step_by_a_binary_tail(
    price: float, percent: float, step: float, buying: bool, expected: float
) -> None:
    """Цена ± процент ровно на сетке остаётся на ней: счёт идёт от десятичного вида чисел.

    Двоичный хвост у `0,1` сдвигал бы предел на лишний шаг от рынка (100,1 → 100,2),
    то есть заявка стояла бы дальше, чем названо процентом. Мутация: `Decimal(float)`
    вместо `Decimal(repr(float))`.
    """
    assert limit_beyond(price, percent, step, buying=buying) == pytest.approx(expected, abs=1e-9)
