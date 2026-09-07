"""Перебор настроек: выбор смотрит только в подбор, колонки не складываются.

Каждый тест стережёт одну вещь, названную в его докстринге. Мутационный
замер — в отчёте методолога: каждая проверка прогонялась против испорченного
кода и краснела по своей причине.

⚠️ Здесь нет ни одного теста «прогон дал столько-то рублей». Числа прогона
зависят от данных, а данные тут синтетические; проверяется **устройство**
отчёта: что во что попадает, что откуда выбирается и чего не складывается.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date, datetime, time, timedelta

import pytest

from backtest.execution import Costs
from backtest.history import Deal
from backtest.overfitting import Shape, deflated_sharpe, neighbours, overfit_share, rank_of
from backtest.report import Row, Verdict, block_report, forward, study
from backtest.split import Fold, Period
from backtest.sweep import (
    AVERAGE_PERIODS,
    TAKE_PERCENTS,
    WINDOW_DURATIONS,
    WINDOW_STARTS,
    Block,
    Ground,
    Money,
    Point,
    Trial,
    blocks,
    full_cross,
    lay_out,
    sweep,
    trading_mode,
    trials_shown,
)
from backtest.table import THOUSANDS, both_columns_filled, money, plural, render
from engine import EngineSettings, ExitReason, Side, TradingWindow
from strategies import EmaReverseSettings
from tests.engine_helpers import MSK, candle

TUNING = Period(date(2026, 6, 1), date(2026, 6, 30))
CHECKING = Period(date(2026, 7, 1), date(2026, 7, 31))
LATER = Period(date(2026, 8, 1), date(2026, 8, 31))


def a_deal(day: date, net: float, *, exit_day: date | None = None) -> Deal:
    """Сделка с заранее известным результатом: вход 100, выход 100 + деньги."""
    entry = datetime.combine(day, time(10, 10), tzinfo=MSK)
    gone = datetime.combine(exit_day or day, time(10, 30), tzinfo=MSK)
    return Deal(
        side=Side.LONG, volume=1.0,
        entry_time=entry, entry_price=100.0, entry_order_id="in",
        exit_time=gone, exit_price=100.0 + net, exit_order_id="out",
        exit_reason=ExitReason.SIGNAL, commission=0.0, ruble_per_point=1.0,
    )


def a_point(label: str, *, take: float = 0.5, period: int = 15) -> Point:
    """Точка сетки с различимыми настройками."""
    return Point(
        label=label,
        engine=trading_mode(EngineSettings(commission_per_side=14.0, take_profit_percent=take)),
        strategy=EmaReverseSettings(period=period),
        axes=(("take", take),),
    )


def a_trial(point: Point, **rubles: float) -> Trial:
    """Прогон с заданными деньгами по отрезкам: ключи — «tuning», «checking», «later»."""
    named = {"tuning": TUNING, "checking": CHECKING, "later": LATER}
    # Все три отрезка заполняются всегда, даже неназванные: `Trial.on`
    # на неназванный отрезок отвечает отказом, и оснастка не должна ловить
    # его вместо проверяемого поведения.
    parts = {
        period: [a_deal(period.since, rubles[name])] if rubles.get(name) else []
        for name, period in named.items()
    }
    return Trial(
        point=point,
        money={period: Money.of(deals) for period, deals in parts.items()},
        shape={period: Shape.of([deal.gross for deal in deals])
               for period, deals in parts.items()},
        total=Money.of([deal for deals in parts.values() for deal in deals]),
        straddling=0, outside=0, halted="",
    )


# -- выбор смотрит ТОЛЬКО в подбор -------------------------------------------

def test_the_choice_is_made_on_the_tuning_column_alone() -> None:
    """Стережёт протечку: победитель выбирается по подбору, а не по проверке."""
    winner, loser = a_point("выиграл подбор", take=0.4), a_point("выиграл проверку", take=0.6)
    trials = (a_trial(winner, tuning=1000.0, checking=-500.0),
              a_trial(loser, tuning=-100.0, checking=9000.0))
    report = block_report(_block(trials), trials, Fold(1, TUNING, CHECKING), runs=2)
    assert report.choice.label == "выиграл подбор"


def test_the_choice_carries_the_checking_money_of_the_chosen_row() -> None:
    """Стережёт подмену: рядом с выбором стоит ЕГО проверка, а не чужая лучшая."""
    winner, loser = a_point("выиграл подбор", take=0.4), a_point("выиграл проверку", take=0.6)
    trials = (a_trial(winner, tuning=1000.0, checking=-500.0),
              a_trial(loser, tuning=-100.0, checking=9000.0))
    report = block_report(_block(trials), trials, Fold(1, TUNING, CHECKING), runs=2)
    assert report.choice.checking.rubles == -500.0
    assert report.choice.cost == -1500.0


def test_the_choice_reports_its_place_on_the_checking_column() -> None:
    """Стережёт: место победителя считается на проверке — иначе оно всегда первое."""
    winner, loser = a_point("выиграл подбор", take=0.4), a_point("выиграл проверку", take=0.6)
    trials = (a_trial(winner, tuning=1000.0, checking=-500.0),
              a_trial(loser, tuning=-100.0, checking=9000.0))
    report = block_report(_block(trials), trials, Fold(1, TUNING, CHECKING), runs=2)
    assert (report.choice.place, report.choice.total) == (1, 2)


def test_every_row_carries_both_columns() -> None:
    """Стережёт правило ТЗ §4.10 Г на уровне чисел: у строки всегда обе колонки."""
    trials = (a_trial(a_point("раз", take=0.4), tuning=10.0, checking=20.0),)
    report = block_report(_block(trials), trials, Fold(1, TUNING, CHECKING), runs=1)
    assert all(isinstance(row, Row) and row.tuning and row.checking for row in report.rows)


def test_the_rows_keep_the_order_of_the_grid() -> None:
    """Стережёт: строки идут по сетке, а не по убыванию прибыли."""
    trials = tuple(
        a_trial(a_point(f"точка {number}", take=0.2 + number / 10), tuning=float(number))
        for number in range(4)
    )
    report = block_report(_block(trials), trials, Fold(1, TUNING, CHECKING), runs=4)
    assert [row.label for row in report.rows] == [f"точка {number}" for number in range(4)]


# -- скользящая проверка -----------------------------------------------------

def _two_folds() -> tuple[Fold, Fold]:
    return (Fold(1, TUNING, CHECKING), Fold(2, CHECKING, LATER))


def test_each_fold_picks_by_its_own_tuning_period() -> None:
    """Стережёт протечку между окнами: второе окно выбирает по своему подбору."""
    first = a_point("лучший в первом окне", take=0.4)
    second = a_point("лучший во втором окне", take=0.6)
    trials = (a_trial(first, tuning=900.0, checking=10.0, later=-40.0),
              a_trial(second, tuning=-900.0, checking=800.0, later=70.0))
    result = forward(trials, _two_folds(), plain=first)
    assert [step.label for step in result.steps] == [first.label, second.label]


def test_the_forward_money_comes_from_the_checking_periods_only() -> None:
    """Стережёт: итог переподстройки складывает проверки, а подборы — никогда."""
    first = a_point("лучший в первом окне", take=0.4)
    second = a_point("лучший во втором окне", take=0.6)
    trials = (a_trial(first, tuning=900.0, checking=10.0, later=-40.0),
              a_trial(second, tuning=-900.0, checking=800.0, later=70.0))
    result = forward(trials, _two_folds(), plain=first)
    assert result.rubles == 10.0 + 70.0


def test_the_forward_baseline_is_the_defaults_on_the_same_days() -> None:
    """Стережёт: колонка сравнения считает умолчания на тех же проверочных днях."""
    first = a_point("лучший в первом окне", take=0.4)
    second = a_point("лучший во втором окне", take=0.6)
    trials = (a_trial(first, tuning=900.0, checking=10.0, later=-40.0),
              a_trial(second, tuning=-900.0, checking=800.0, later=70.0))
    result = forward(trials, _two_folds(), plain=first)
    assert result.baseline == 10.0 + (-40.0)


def test_folds_sharing_a_checking_day_are_refused() -> None:
    """Стережёт сложение разных периодов: общий день посчитался бы дважды."""
    point = a_point("одна", take=0.4)
    trials = (a_trial(point, tuning=1.0, checking=2.0, later=3.0),)
    overlapping = (
        Fold(1, TUNING, Period(CHECKING.since, LATER.since)),
        Fold(2, Period(date(2026, 7, 2), date(2026, 7, 20)), LATER),
    )
    with pytest.raises(ValueError, match="делят дни на проверке"):
        forward(trials, overlapping, plain=point)


def test_the_defaults_must_be_present_in_the_grid() -> None:
    """Стережёт: сравнивать переподстройку не с чем — отказ, а не чужая строка."""
    trials = (a_trial(a_point("не умолчания", take=0.9), tuning=1.0, checking=2.0, later=3.0),)
    with pytest.raises(ValueError, match="строка умолчаний обязана быть"):
        forward(trials, _two_folds(), plain=a_point("умолчания", take=0.5))


def test_the_share_of_windows_below_the_middle_is_counted() -> None:
    """Стережёт долю провалов: место победителя ниже середины считается провалом."""
    assert overfit_share([(1, 10), (10, 10)]) == 0.5
    assert overfit_share([]) is None


# -- деньги отрезка ----------------------------------------------------------

def test_a_period_without_deals_is_worth_exactly_zero() -> None:
    """Стережёт: пустой отрезок вносит в сумму ноль, а в таблицу — «сделок нет»."""
    empty = Money.of([])
    assert empty.trades == 0
    assert empty.net is None
    assert empty.rubles == 0.0


def test_money_without_a_tariff_refuses_to_become_a_number() -> None:
    """Стережёт подстановку нуля вместо неизвестной комиссии (DOMAIN.md §5)."""
    without = Money.of([replace(a_deal(TUNING.since, 100.0), commission=None)])
    with pytest.raises(ValueError, match="тариф комиссии не задан"):
        _ = without.rubles


def test_a_deal_torn_by_the_boundary_goes_into_neither_column() -> None:
    """Стережёт заглядывание вперёд: вход в подборе, выход в проверке — никуда."""
    torn = a_deal(TUNING.until, 500.0, exit_day=CHECKING.since)
    assert not TUNING.holds(TUNING.until, CHECKING.since)
    assert not CHECKING.holds(TUNING.until, CHECKING.since)
    assert Money.of([torn]).trades == 1


# -- соседи по сетке ---------------------------------------------------------

def test_a_point_outside_the_grid_has_no_neighbours() -> None:
    """Стережёт: «весь день» и переключатель режима соседей не выдумывают."""
    assert neighbours([(), (("take", 0.5),)])[0] == ()


def test_only_one_step_along_one_axis_makes_a_neighbour() -> None:
    """Стережёт: сосед — это шаг по одной оси, а не любое другое значение."""
    axes = [
        (("start", 570.0), ("duration", 30.0)),
        (("start", 570.0), ("duration", 60.0)),
        (("start", 570.0), ("duration", 90.0)),
        (("start", 585.0), ("duration", 60.0)),
    ]
    found = neighbours(axes)
    assert found[0] == (1,)
    assert set(found[1]) == {0, 2, 3}
    assert found[2] == (1,)


def test_points_on_different_axes_are_never_neighbours() -> None:
    """Стережёт: точки разных блоков не соседи, даже если числа совпали."""
    assert neighbours([(("take", 0.5),), (("period", 0.5),)]) == ((), ())


def test_a_profitable_point_with_a_losing_neighbour_is_noise() -> None:
    """Стережёт главную оговорку задания: соседи провалились — это шум."""
    good, bad = a_point("хорошая", take=0.4), a_point("плохая", take=0.5)
    trials = (a_trial(good, tuning=1.0, checking=5000.0),
              a_trial(bad, tuning=0.0, checking=-5000.0))
    report = block_report(_block(trials), trials, Fold(1, TUNING, CHECKING), runs=2)
    assert report.rows[0].verdict.stable is False
    assert report.rows[0].verdict.word == "шум"


def test_a_point_is_stable_only_when_every_neighbour_is_in_profit() -> None:
    """Стережёт: устойчиво — это и точка, и все её соседи в плюсе."""
    trials = tuple(
        a_trial(a_point(f"точка {number}", take=0.2 + number / 10),
                tuning=1.0, checking=1000.0)
        for number in range(3)
    )
    report = block_report(_block(trials), trials, Fold(1, TUNING, CHECKING), runs=3)
    assert [row.verdict.stable for row in report.rows] == [True, True, True]


def test_a_verdict_without_neighbours_is_a_dash_not_a_word() -> None:
    """Стережёт: «соседей нет» не превращается в «неустойчиво»."""
    assert Verdict(neighbours=0, worst=None, best=None, stable=None).word == "—"


# -- сетка перебора ----------------------------------------------------------

def _ground() -> Ground:
    return Ground(
        engine=trading_mode(EngineSettings(commission_per_side=14.0)),
        costs=Costs(commission_per_side=14.0),
    )


def _block(trials: tuple[Trial, ...]) -> Block:
    return Block(name="проба", title="Проба", question="проба",
                 points=tuple(trial.point for trial in trials))


def test_the_window_block_carries_the_whole_trading_day_row() -> None:
    """Стережёт требование ТЗ §8: без «всего дня» сужение окна не с чем сравнить."""
    window = next(block for block in blocks(_ground()) if block.name == "window")
    whole = [point for point in window.points if point.label == "весь торговый день"]
    assert len(whole) == 1
    assert whole[0].engine.window.whole_day


def test_the_window_block_carries_the_current_default_row() -> None:
    """Стережёт: сетка ТЗ не содержит 10:05–11:00, и без строки не с чем сравнить."""
    window = next(block for block in blocks(_ground()) if block.name == "window")
    labels = [point.label for point in window.points]
    assert "10:05–11:00 (умолчание)" in labels


def test_the_grid_follows_the_mandatory_programme() -> None:
    """Стережёт состав программы ТЗ §8: три средних, тейк 0,2…1,5 шагом 0,1."""
    assert AVERAGE_PERIODS == (9, 15, 20)
    assert TAKE_PERCENTS[0] == 0.2
    assert TAKE_PERCENTS[-1] == 1.5
    assert len(TAKE_PERCENTS) == 14
    assert WINDOW_STARTS[0] == time(9, 30)
    assert WINDOW_STARTS[-1] == time(11, 0)
    assert WINDOW_DURATIONS[0] == timedelta(minutes=30)
    assert WINDOW_DURATIONS[-1] == timedelta(hours=4)


def test_the_take_block_checks_the_claim_that_no_take_loses_money() -> None:
    """Стережёт: «без тейка» есть в сетке — утверждение DOMAIN.md §4 проверяемо."""
    take = next(block for block in blocks(_ground()) if block.name == "average_and_take")
    off = [point for point in take.points if not point.engine.take_profit]
    assert len(off) == len(AVERAGE_PERIODS)


def test_the_defaults_row_repeats_in_every_block() -> None:
    """Стережёт сверку блоков: умолчания есть везде, и потому сравнимы."""
    ground = _ground()
    for block in blocks(ground):
        same = [
            point for point in block.points
            if point.engine == ground.engine and point.strategy == ground.strategy
        ]
        assert len(same) == 1, f"в блоке {block.name} умолчаний {len(same)}"


def test_the_full_cross_is_the_product_of_every_axis() -> None:
    """Стережёт число прогонов: полное произведение считается, а не оценивается."""
    windows = len(WINDOW_STARTS) * len(WINDOW_DURATIONS) + 2
    assert len(full_cross(_ground())) == windows * 3 * 14 * 2 * 2


def test_the_number_of_runs_is_said_before_the_run() -> None:
    """Стережёт требование ТЗ §4.10 В: число прогонов называется до запуска."""
    said = trials_shown(107)
    assert "107" in said
    with pytest.raises(ValueError, match="не имеет смысла"):
        trials_shown(0)


def test_the_same_settings_in_two_blocks_must_give_the_same_money() -> None:
    """Стережёт расхождение отчёта с собой: одна настройка — одно число."""
    point = a_point("умолчания", take=0.5)
    honest = a_trial(point, tuning=1.0, checking=2.0, later=3.0)
    lying = a_trial(point, tuning=1.0, checking=99.0, later=3.0)
    with pytest.raises(ValueError, match="разные деньги на проверке"):
        study(
            symbol="MXU6", days=[TUNING.since, LATER.until],
            split=Fold(1, TUNING, CHECKING), folds=[Fold(1, TUNING, CHECKING)],
            costs=Costs(commission_per_side=14.0),
            parts=[(_block((honest,)), (honest,)), (_block((lying,)), (lying,))],
            plain=point,
        )


# -- поправки ----------------------------------------------------------------

def test_more_trials_lower_the_deflated_sharpe() -> None:
    """Стережёт смысл поправки: чем больше перебрано, тем меньше веры лучшему."""
    shape = Shape.of([120.0, -80.0, 200.0, -30.0, 90.0, 15.0, -60.0, 140.0])
    few = deflated_sharpe(shape, 0.1, 10)
    many = deflated_sharpe(shape, 0.1, 10_000)
    assert few is not None and many is not None
    assert many < few


def test_the_standard_error_of_the_total_grows_with_the_square_root() -> None:
    """Стережёт цифру, которую ставят рядом с прибылью: σ·√n, а не σ."""
    shape = Shape.of([100.0, -100.0] * 8)
    assert shape.error_of_total == pytest.approx(shape.spread * 4.0)


def test_a_single_deal_has_no_shape() -> None:
    """Стережёт: разброс по одной сделке не считается, Шарп не становится бесконечным."""
    assert Shape.of([500.0]).sharpe is None


def test_equal_results_share_the_place_of_the_worst() -> None:
    """Стережёт долю провалов: порядок строк не влияет на место."""
    assert rank_of(5.0, [5.0, 5.0, 9.0]) == 1


# -- вёрстка -----------------------------------------------------------------

def test_the_rendered_table_keeps_both_columns_in_every_row() -> None:
    """Стережёт: спрятать колонку «проверка» из готовой таблицы невозможно."""
    point = a_point("умолчания", take=0.5)
    trials = (a_trial(point, tuning=1.0, checking=2.0, later=3.0),)
    text = render(study(
        symbol="MXU6", days=[TUNING.since, LATER.until],
        split=Fold(1, TUNING, CHECKING), folds=[Fold(1, TUNING, CHECKING)],
        costs=Costs(commission_per_side=14.0),
        parts=[(_block(trials), trials)], plain=point,
    ))
    assert both_columns_filled(text)
    assert "ПРОВЕРКА" in text


def test_a_hidden_checking_column_is_caught() -> None:
    """Стережёт саму проверку: выброшенная колонка обязана быть замечена."""
    assert not both_columns_filled("  строка │ 1 │  │ 2")
    assert not both_columns_filled("  строка │ 1 │ 2")


def test_money_shows_a_dash_for_the_unknown_and_never_a_zero() -> None:
    """Стережёт: «нет числа» и «ноль рублей» в отчёте выглядят по-разному."""
    assert money(None) == "—"
    assert money(0.0) == "+0"
    assert money(-1234.0) == f"−1{THOUSANDS}234"
    assert " " not in money(-1234.0), "разделитель разрядов обязан быть неразрывным"


def test_the_words_agree_with_the_numbers() -> None:
    """Стережёт русский текст отчёта: «1 сделка», «2 сделки», «5 сделок»."""
    assert plural(1, "сделка", "сделки", "сделок") == "сделка"
    assert plural(3, "сделка", "сделки", "сделок") == "сделки"
    assert plural(11, "сделка", "сделки", "сделок") == "сделок"


# -- сквозной прогон ---------------------------------------------------------

def _series(days: int = 6) -> list[object]:
    """Ряд пятиминуток на несколько дней: цена ходит вверх-вниз, сигналы есть."""
    bars: list[object] = []
    for day in range(days):
        for step in range(72):
            price = 100.0 + 6.0 * ((step // 6) % 2) + step % 5
            bars.append(candle(9 + step // 12, (step % 12) * 5,
                               close=price, day=15 + day, month=6))
    return bars


def test_the_sweep_lays_the_deals_out_by_period() -> None:
    """Стережёт раскладку: сделки прогона попадают в свои отрезки и никуда больше."""
    ground = _ground()
    points = (Point(label="проба", engine=ground.engine, strategy=ground.strategy),)
    first = Period(date(2026, 6, 15), date(2026, 6, 17))
    second = Period(date(2026, 6, 18), date(2026, 6, 20))
    trials = asyncio.run(sweep(_series(), points, (first, second), costs=ground.costs))
    trial = trials[0]
    assert trial.total.trades == trial.on(first).trades + trial.on(second).trades
    assert trial.straddling == 0
    assert trial.outside == 0


def test_asking_an_unnamed_period_is_a_refusal_not_an_empty_result() -> None:
    """Стережёт: неназванный отрезок — ошибка вызова, а не «ничего не заработано»."""
    ground = _ground()
    points = (Point(label="проба", engine=ground.engine, strategy=ground.strategy),)
    named = Period(date(2026, 6, 15), date(2026, 6, 20))
    trials = asyncio.run(sweep(_series(), points, (named,), costs=ground.costs))
    with pytest.raises(KeyError):
        trials[0].on(Period(date(2026, 7, 1), date(2026, 7, 2)))


def test_a_window_that_would_cross_midnight_is_refused() -> None:
    """Стережёт сетку окон: через полночь она не идёт, и молча не переносится."""
    ground = replace(_ground(), engine=trading_mode(
        EngineSettings(commission_per_side=14.0, window=TradingWindow(time(23, 0), time(23, 30)))))
    assert all(
        point.engine.window.start <= point.engine.window.end
        or point.engine.window.whole_day
        for block in blocks(ground) for point in block.points
    )


def test_a_deal_torn_by_the_boundary_lands_in_no_column_at_all() -> None:
    """Стережёт раскладку: разорванная сделка не идёт ни в подбор, ни в проверку."""
    torn = a_deal(TUNING.until, 5000.0, exit_day=CHECKING.since)
    inside = a_deal(TUNING.since, 100.0)
    trial = lay_out(a_point("проба"), (inside, torn), (TUNING, CHECKING))
    assert trial.on(TUNING).rubles == 100.0
    assert trial.on(CHECKING).trades == 0
    assert trial.straddling == 1
    assert trial.total.trades == 2


def test_a_deal_outside_every_period_is_counted_apart() -> None:
    """Стережёт: сделка вне названных отрезков видна числом, а не тонет молча."""
    trial = lay_out(a_point("проба"), (a_deal(LATER.since, 700.0),), (TUNING, CHECKING))
    assert trial.outside == 1
    assert trial.on(TUNING).trades == 0
    assert trial.on(CHECKING).trades == 0


def test_a_row_outside_the_grid_gets_no_verdict_instead_of_a_bad_one() -> None:
    """Стережёт: у «всего дня» соседей нет, и это не приговор «шум»."""
    off = Point(label="весь день", engine=a_point("сетка").engine,
                strategy=EmaReverseSettings(period=20))
    on_grid = a_point("на сетке", take=0.4)
    trials = (a_trial(off, tuning=1.0, checking=-9000.0),
              a_trial(on_grid, tuning=2.0, checking=3000.0))
    report = block_report(_block(trials), trials, Fold(1, TUNING, CHECKING), runs=2)
    assert report.rows[0].verdict.stable is None
    assert report.rows[0].verdict.word == "—"
    assert report.rows[1].verdict.stable is None
