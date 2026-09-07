"""Перебор лидеров: выбор смотрит только в подбор, сетка полна и повторима.

Каждый тест стережёт одну вещь, названную в его докстринге, и каждый прогнан
против испорченного кода — мутационная проба лежит в отчёте методолога.

⚠️ Здесь нет ни одного теста «перебор дал столько-то рублей». Числа зависят
от данных; проверяется **устройство**: что откуда выбирается и что во что
складывается.
"""

from __future__ import annotations

from datetime import date, time, timedelta

import pytest

from backtest.leaders import (
    ENOUGH_TRADES,
    Cut,
    Recipe,
    Scored,
    around,
    halves,
    leaders,
    recipes,
    reference_recipes,
    verdict,
)
from backtest.overfitting import neighbours


def a_cut(net: float, *, trades: int = 100, gross: float = 0.0) -> Cut:
    """Итог отрезка с заданными деньгами. Прочее в проверке не участвует."""
    return Cut(trades=trades, gross=gross or net, net=net, spread=1.0, mean=net / 100)


def a_scored(number: int, *, tuning: float, checking: float, whole: float | None = None,
             trades: int = 100) -> Scored:
    """Сочетание с различимыми деньгами по каждому отрезку."""
    return Scored(
        number=number,
        tuning=a_cut(tuning, trades=trades),
        checking=a_cut(checking, trades=trades),
        whole=a_cut(tuning + checking if whole is None else whole, trades=trades),
    )


# --------------------------------------------------------------- выбор лидеров

def test_leaders_are_picked_on_the_tuning_period_only():
    """Порядок лидеров задаёт подбор, а не проверка и не весь отрезок.

    Главная страховка задачи: лидер, отобранный на полном отрезке, — подгонка
    по построению, а таблица при этом выглядит ровно так же, как честная.
    Здесь три сочетания, у которых порядок по подбору обратен порядку
    по проверке и по всему отрезку.
    """
    line = [
        a_scored(0, tuning=100.0, checking=-900.0, whole=-800.0),
        a_scored(1, tuning=50.0, checking=500.0, whole=550.0),
        a_scored(2, tuning=10.0, checking=900.0, whole=910.0),
    ]
    assert [one.number for one in leaders(line, wanted=3)] == [0, 1, 2]


def test_a_set_with_too_few_trades_is_not_a_leader():
    """Порог по числу сделок отсекает набор с горсткой везучих входов.

    Без порога наверх всплывает сочетание с тремя сделками и одним удачным
    днём: узкое окно в конце дня даёт единицы входов, и деньги у него
    случайны по построению.
    """
    lucky = a_scored(0, tuning=10_000.0, checking=0.0, trades=ENOUGH_TRADES - 1)
    plain = a_scored(1, tuning=100.0, checking=0.0, trades=ENOUGH_TRADES)
    assert [one.number for one in leaders([lucky, plain], wanted=5)] == [1]


def test_leaders_do_not_change_between_runs():
    """При равных деньгах порядок разводится числом сделок и номером в сетке.

    Иначе «пятнадцать лидеров» менялись бы от запуска к запуску, и свод
    нельзя было бы повторить.
    """
    line = [
        a_scored(2, tuning=100.0, checking=0.0, trades=100),
        a_scored(0, tuning=100.0, checking=0.0, trades=100),
        a_scored(1, tuning=100.0, checking=0.0, trades=200),
    ]
    assert [one.number for one in leaders(line, wanted=3)] == [1, 0, 2]
    assert [one.number for one in leaders(list(reversed(line)), wanted=3)] == [1, 0, 2]


def test_a_set_with_no_trades_at_all_never_wins():
    """Отрезок без сделок даёт ноль рублей, и ноль не должен побеждать минус."""
    empty = a_scored(0, tuning=0.0, checking=0.0, trades=0)
    losing = a_scored(1, tuning=-100.0, checking=0.0, trades=ENOUGH_TRADES)
    assert [one.number for one in leaders([empty, losing], wanted=5)] == [1]


# ---------------------------------------------------------------------- сетка

def test_the_grid_is_the_full_programme_of_the_specification():
    """Сетка — полное произведение обязательной программы ТЗ §8.

    58 окон (56 по сетке, плюс весь день, плюс нынешнее умолчание) на три
    периода средней, четырнадцать размеров тейка и четыре сочетания режимов.
    """
    made = recipes()
    assert len(made) == 58 * 3 * 14 * 4
    assert len({one.label for one in made}) == len(made)


def test_the_grid_is_built_in_the_same_order_every_time():
    """Порядок сетки детерминирован: рабочий процесс обращается к ней по номеру."""
    assert [one.label for one in recipes()] == [one.label for one in recipes()]


def test_the_whole_day_row_is_in_the_grid():
    """Строка «весь торговый день» обязательна: без неё сужение окна не с чем сравнить."""
    whole = [one for one in recipes() if one.window_start == one.window_end]
    assert whole
    assert all(one.axes == () for one in whole)


def test_reference_rows_switch_the_take_profit_off():
    """Опорные строки «без тейка» действительно выключают тейк, а не ставят ноль.

    Ноль вместо выключателя — другое поведение: движок с нулевым тейком
    вооружает цель на самой цене входа, а с выключенным не вооружает вовсе.
    """
    without = [one for one, title in reference_recipes() if "тейк выключен" in title]
    assert without
    assert all(not one.take_on for one in without)
    assert all(one.take_percent > 0 for one in without)


# ------------------------------------------------------------------- соседи

def test_around_agrees_with_the_shared_definition_of_a_neighbour():
    """Соседство здесь то же, что в `backtest.overfitting.neighbours`.

    Отдельная функция написана из-за размера: `neighbours` сравнивает каждую
    точку с каждой, и на 9 744 точках это 95 миллионов сравнений. Согласие
    двух определений проверяется, а не обещается.
    """
    axes = tuple(
        (("take", take), ("period", float(period)))
        for take in (0.2, 0.3, 0.4)
        for period in (9, 15, 20)
    ) + ((),)
    theirs = neighbours(axes)
    for here in range(len(axes)):
        assert around(axes, here) == tuple(sorted(theirs[here])), here


def test_a_point_outside_the_grid_has_no_neighbours():
    """У «весь день» и у нынешнего умолчания соседства не бывает по природе."""
    assert around(((), (("take", 0.2),)), 0) == ()


# ------------------------------------------------------------- деление истории

def test_halves_never_let_the_tuning_touch_the_checking():
    """Ни один день не попадает и в подбор, и в проверку."""
    days = tuple(date(2026, 6, 1) + timedelta(days=step) for step in range(40))
    tuning, checking = halves(days, tuning=20)
    assert tuning.until < checking.since


def test_halves_refuse_when_nothing_is_left_to_check():
    """Подбор во всю историю — отказ: проверять было бы нечего."""
    days = tuple(date(2026, 6, 1) + timedelta(days=step) for step in range(10))
    with pytest.raises(ValueError, match="не остаётся ни одного дня"):
        halves(days, tuning=10)


# ------------------------------------------------------------------ свод

def test_the_verdict_answers_in_words_before_any_table():
    """Первая строка свода отвечает словами, а не оставляет читателю таблицу."""
    line = [
        a_scored(0, tuning=100.0, checking=50.0),
        a_scored(1, tuning=90.0, checking=-50.0),
    ]
    assert "пережили 1" in verdict(line)


def test_the_verdict_says_so_when_there_are_no_leaders():
    """Пустой отбор — это ответ, а не сбой, и он называется словами."""
    assert "лидеров нет" in verdict(())


def test_a_recipe_carries_the_take_size_even_when_the_take_is_off():
    """Размер тейка живёт рядом с выключателем и при выключенном тейке.

    Так оба разворота рецепта — в настройки движка и в набор окна — ставят
    одно и то же число, и сравнить их можно целиком, а не по кускам.
    """
    off = Recipe(label="проба", window_start=time(10, 5), window_end=time(11, 0),
                 average_period=15, take_percent=0.5, take_on=False)
    assert off.take_percent == 0.5
    assert not off.take_on
