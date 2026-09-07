"""Разделение истории: подбор и проверка не пересекаются ни одним днём.

Каждый тест стережёт ровно одну вещь, и она названа в имени. Проверялось
мутацией: каждая проверка ниже была прогнана против испорченного кода,
и каждая покраснела **по своей причине**, а не «вообще упало».
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from backtest.split import (
    CHECKING_DAYS,
    DENSE_ENOUGH,
    TUNING_DAYS,
    Fold,
    Period,
    single_split,
    trading_days,
    walk_forward,
)

MONDAY = date(2026, 6, 15)


def workdays(count: int, since: date = MONDAY) -> list[date]:
    """Ряд будних дней подряд, без выходных."""
    days: list[date] = []
    day = since
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


# -- Period: сделка на стыке не принадлежит никому ---------------------------

def test_a_deal_that_left_the_period_does_not_belong_to_it():
    """Стережёт: вход внутри, выход снаружи — отрезок такую сделку не считает."""
    period = Period(MONDAY, MONDAY + timedelta(days=4))
    assert period.holds(MONDAY, MONDAY + timedelta(days=1))
    assert not period.holds(MONDAY + timedelta(days=4), MONDAY + timedelta(days=5))


def test_a_deal_entered_before_the_period_does_not_belong_to_it():
    """Стережёт: вход раньше отрезка не втаскивает сделку внутрь по выходу."""
    period = Period(MONDAY + timedelta(days=1), MONDAY + timedelta(days=4))
    assert not period.holds(MONDAY, MONDAY + timedelta(days=2))


def test_a_period_that_ends_before_it_starts_is_refused():
    """Стережёт: перевёрнутый отрезок не построить — он молча ничего не содержал бы."""
    with pytest.raises(ValueError, match="кончается раньше"):
        Period(MONDAY + timedelta(days=1), MONDAY)


def test_periods_sharing_a_single_day_are_seen_as_overlapping():
    """Стережёт: `overlaps` видит общий день, включая единственный общий."""
    left = Period(MONDAY, MONDAY + timedelta(days=4))
    assert left.overlaps(Period(MONDAY + timedelta(days=4), MONDAY + timedelta(days=8)))
    assert not left.overlaps(Period(MONDAY + timedelta(days=5), MONDAY + timedelta(days=8)))


# -- Fold: построить окно с протечкой нельзя ---------------------------------

def test_a_fold_whose_tuning_shares_a_day_with_checking_cannot_be_built():
    """Стережёт главное правило: день подбора не может быть днём проверки."""
    with pytest.raises(ValueError, match="заходит на проверку"):
        Fold(
            number=1,
            tuning=Period(MONDAY, MONDAY + timedelta(days=4)),
            checking=Period(MONDAY + timedelta(days=4), MONDAY + timedelta(days=8)),
        )


def test_a_fold_that_checks_before_it_tunes_cannot_be_built():
    """Стережёт: проверка «в прошлом» — это подбор задним числом, а не проверка."""
    with pytest.raises(ValueError, match="заходит на проверку"):
        Fold(
            number=1,
            tuning=Period(MONDAY + timedelta(days=7), MONDAY + timedelta(days=11)),
            checking=Period(MONDAY, MONDAY + timedelta(days=4)),
        )


def test_a_fold_with_separated_periods_is_accepted():
    """Стережёт: законное окно сторож не отвергает — иначе он не сторож, а запрет."""
    fold = Fold(
        number=1,
        tuning=Period(MONDAY, MONDAY + timedelta(days=4)),
        checking=Period(MONDAY + timedelta(days=5), MONDAY + timedelta(days=9)),
    )
    assert fold.tuning.until < fold.checking.since


# -- walk_forward ------------------------------------------------------------

def test_the_checking_periods_of_the_folds_never_overlap():
    """Стережёт: деньги окон складывать можно только если их дни не общие."""
    folds = walk_forward(workdays(71))
    for earlier, later in zip(folds, folds[1:], strict=False):
        assert not earlier.checking.overlaps(later.checking), (
            f"{earlier.checking} и {later.checking} делят день: "
            "их суммирование посчитало бы его дважды"
        )


def test_every_fold_checks_strictly_after_its_own_tuning():
    """Стережёт: в каждом окне проверка строго позже подбора, а не «в целом позже»."""
    for fold in walk_forward(workdays(71)):
        assert fold.tuning.until < fold.checking.since


def test_the_checking_days_cover_the_rest_of_the_history_exactly_once():
    """Стережёт: ни один день после подбора не пропущен и ни один не взят дважды."""
    days = workdays(71)
    folds = walk_forward(days)
    taken = [day for fold in folds for day in days if fold.checking.contains(day)]
    assert taken == days[TUNING_DAYS:], "проверка обязана пройти по каждому дню ровно раз"
    assert len(taken) == len(set(taken))


def test_the_fold_step_equals_the_length_of_the_checking_period():
    """Стережёт: окно едет ровно на длину проверки, иначе дни пересекутся или выпадут."""
    days = workdays(71)
    folds = walk_forward(days)
    starts = [days.index(fold.tuning.since) for fold in folds]
    assert starts == [number * CHECKING_DAYS for number in range(len(folds))]


def test_every_checking_period_is_one_trading_week():
    """Стережёт довод из решения: пять дней проверки — понедельник…пятница по разу."""
    days = workdays(71)
    for fold in walk_forward(days):
        inside = [day for day in days if fold.checking.contains(day)]
        assert sorted(day.weekday() for day in inside) == [0, 1, 2, 3, 4]


def test_too_few_days_are_refused_instead_of_giving_no_folds():
    """Стережёт: коротких данных не хватило — отказ, а не молчаливые ноль окон."""
    with pytest.raises(ValueError, match="Делить нечего"):
        walk_forward(workdays(TUNING_DAYS + CHECKING_DAYS - 1))


def test_a_fold_of_zero_length_is_refused():
    """Стережёт: окно нулевой длины — это отсутствие проверки под её именем."""
    with pytest.raises(ValueError, match="положительными"):
        walk_forward(workdays(71), checking=0)


def test_the_folds_are_numbered_from_one_in_order():
    """Стережёт: номер окна в отчёте совпадает с его местом в ряду."""
    folds = walk_forward(workdays(71))
    assert [fold.number for fold in folds] == list(range(1, len(folds) + 1))


# -- single_split ------------------------------------------------------------

def test_the_single_split_and_the_first_fold_share_one_boundary():
    """Стережёт: во всём отчёте одна граница между подбором и проверкой."""
    days = workdays(71)
    assert single_split(days).tuning == walk_forward(days)[0].tuning


def test_the_single_split_gives_the_whole_remainder_to_checking():
    """Стережёт: проверка одного деления кончается последним днём истории."""
    days = workdays(71)
    split = single_split(days)
    assert split.checking.until == days[-1]
    assert split.checking.since == days[TUNING_DAYS]


def test_a_split_that_leaves_no_day_for_checking_is_refused():
    """Стережёт: подбор на всей истории — это отсутствие проверки."""
    with pytest.raises(ValueError, match="не остаётся ни одного дня"):
        single_split(workdays(TUNING_DAYS))


# -- trading_days ------------------------------------------------------------

def test_a_weekend_with_data_is_not_a_trading_day():
    """Стережёт: суббота с данными не занимает место в окне — робот в неё не торгует."""
    saturday = date(2026, 6, 20)
    assert saturday.weekday() == 5
    assert trading_days([(saturday, 200)]) == ()


def test_a_day_with_an_incomplete_history_is_not_a_trading_day():
    """Стережёт: огрызок дня дал бы одним границам окна сделки, другим пустоту."""
    assert trading_days([(MONDAY, DENSE_ENOUGH - 1)]) == ()
    assert trading_days([(MONDAY, DENSE_ENOUGH)]) == (MONDAY,)


def test_the_trading_days_come_back_in_ascending_order():
    """Стережёт: порядок дней задаёт границы окон, и он не зависит от порядка входа."""
    days = workdays(3)
    assert trading_days([(day, 100) for day in reversed(days)]) == tuple(days)
