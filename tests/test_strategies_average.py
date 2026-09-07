"""Скользящая средняя: затравка, окно расчёта и границы появления значения.

Здесь проверяется арифметика, на которой стоит весь список сделок. Три места,
где ошибка не падает, а просто даёт другие сделки:

1. **Затравка.** Простое среднее первых `period` закрытий, а не первое закрытие.
   На эталонном отрезке подмена даёт 128 сделок вместо 127 и другое время выхода
   уже на первой сделке (PROTOTYPE.md §3). Дальше оба ряда сходятся — поэтому
   ошибка выглядит как «разъехалось начало прогрева» и легко списывается.
2. **Момент появления значения.** Средняя появляется на `period`-й свече.
   Сигнал при этом ещё запрещён — разрыв в одну свечу проверяется в тестах
   торгового модуля.
3. **Значение считается с учётом поданного закрытия**, а не по предыдущему.

Числа в тестах маленькие и посчитаны руками: период 3 выбран для того, чтобы
ожидаемое значение можно было проверить в уме, а не пересчитать тем же кодом,
который проверяется.
"""

from __future__ import annotations

import random
from decimal import Decimal, getcontext
from math import fsum

import pytest

from strategies import AverageKind, MovingAverage, average_series


def test_ema_seed_is_the_simple_mean_of_the_first_period_closes() -> None:
    """Затравка — среднее первых трёх закрытий, дальше рекуррентная формула.

    Ручной расчёт для периода 3 (коэффициент 2/(3+1) = 0,5):

        затравка   = (10 + 11 + 12) / 3 = 11
        E(13) = 11   + 0,5 * (13 − 11)   = 12
        E(14) = 12   + 0,5 * (14 − 12)   = 13
    """
    assert average_series([10, 11, 12, 13, 14], 3, AverageKind.EMA) == [
        None, None, 11.0, 12.0, 13.0,
    ]


def test_seeding_with_the_first_close_gives_a_different_series() -> None:
    """Затравка первым закрытием — другая средняя. Ровно та подмена из §3.

    Тест держит различие видимым: если однажды кто-то «упростит» затравку,
    красным станет он, а не сверка с прототипом через неделю.
    """
    ours = average_series([10, 11, 12, 13, 14], 3, AverageKind.EMA)

    wrong: list[float] = []
    value = 10.0  # затравка первым закрытием
    wrong.append(value)
    for close in (11, 12, 13, 14):
        value += 0.5 * (close - value)
        wrong.append(value)

    assert wrong == [10.0, 10.5, 11.25, 12.125, 13.0625]
    assert ours[2:] != wrong[2:], (
        "две затравки дали одинаковый ряд — значит проверять больше нечего "
        "и тест перестал что-либо ловить"
    )


def test_sma_is_the_mean_of_the_last_period_closes() -> None:
    """Простая средняя: окно едет, значения считаются руками.

        (10 + 11 + 12) / 3 = 11
        (11 + 12 + 13) / 3 = 12
        (12 + 13 + 14) / 3 = 13
    """
    assert average_series([10, 11, 12, 13, 14], 3, AverageKind.SMA) == [
        None, None, 11.0, 12.0, 13.0,
    ]


def test_both_kinds_agree_on_the_seed_bar_and_diverge_after() -> None:
    """На затравочной свече EMA и SMA равны — дальше расходятся."""
    closes = [1, 2, 3, 8, 14, 10]
    ema = average_series(closes, 3, AverageKind.EMA)
    sma = average_series(closes, 3, AverageKind.SMA)

    assert ema[2] == sma[2] == 2.0
    assert ema[3:] == [5.0, 9.5, 9.75]
    assert sma[3] == pytest.approx(13 / 3)
    assert sma[5] == pytest.approx(32 / 3)
    assert ema[5] != sma[5]


def test_value_appears_exactly_on_the_period_th_close() -> None:
    """До `period`-й свечи значения нет — и это `None`, а не ноль.

    Ноль здесь был бы ценой, и сравнение «закрытие выше средней» стало бы
    истинным на каждой свече прогрева.
    """
    average = MovingAverage(4, AverageKind.EMA)
    assert average.value is None

    for close in (10, 11, 12):
        assert average.push(close) is None
        assert average.value is None

    assert average.push(13) == pytest.approx(11.5)
    assert average.count == 4


def test_value_includes_the_close_just_pushed() -> None:
    """Сравнивается значение, включающее эту же свечу, а не предыдущее."""
    average = MovingAverage(3, AverageKind.SMA)
    average.push(10)
    average.push(11)
    average.push(12)
    before = average.value
    after = average.push(120)
    assert after != before
    assert after == pytest.approx((11 + 12 + 120) / 3)


def test_period_one_makes_the_average_equal_to_the_close() -> None:
    """Крайний случай не должен падать: период 1 — это само закрытие."""
    assert average_series([10, 20, 30], 1, AverageKind.EMA) == [10.0, 20.0, 30.0]
    assert average_series([10, 20, 30], 1, AverageKind.SMA) == [10.0, 20.0, 30.0]


def test_series_matches_push_by_push() -> None:
    """Разом посчитанный ряд равен посчитанному свеча за свечой.

    Иначе линия средней на графике и решения робота считались бы двумя
    разными способами — и однажды разошлись бы.
    """
    closes = [285_000, 284_800, 285_400, 286_100, 285_950, 284_200, 283_900]
    stepwise = MovingAverage(3, AverageKind.EMA)
    assert [stepwise.push(close) for close in closes] == average_series(closes, 3)


def test_the_average_of_a_flat_price_is_that_price_exactly() -> None:
    """Ровная цена — ровно она же, даже если в двоичной она непредставима.

    Не косметика. Если все `period` закрытий затравки равны, а цена в double
    непредставима (0,03, 0,11 — обычная цена акции), то `сумма / period`
    промахивается мимо неё на 1 ULP. Дальше цена стоит, поправка
    `α·(close − E)` округляется в ноль — и средняя застревает мимо цены
    **навсегда**, давая сигнал на каждой свече там, где прототип молчит.
    """
    for price, period in [(0.03, 15), (0.11, 5), (0.03, 9), (0.11, 20), (0.03, 30)]:
        naive = fsum([price] * period) / period
        assert naive != price, (
            f"цена {price} представима точно — на ней проверять нечего, "
            "тест выродился"
        )
        for kind in AverageKind:
            average = MovingAverage(period, kind)
            for _ in range(period + 25):
                value = average.push(price)
            assert value == price, (
                f"{kind.short_label}({period}) на ровной цене {price} застряла "
                f"на {value!r} — расхождение с прототипом на каждой свече флэта"
            )


def test_ema_stays_within_a_few_ulp_of_exact_arithmetic() -> None:
    """Замер дрейфа: 10 000 свечей против точного расчёта в `Decimal`.

    Мы считаем в двоичной плавающей точке, прототип — в десятичной. Сравнение
    строгое, поэтому вопрос «на сколько расходится» — не академический.
    Здесь он измеряется на синтетике, а не на архивном наборе: замер обязан
    работать и в свежем клоне, где `reference/` нет.

    Порог 1e-14 относительной ошибки (около 50 ULP) выбран с большим запасом
    к измеренным 2,1 ULP: тест ловит сломанный накопитель, а не колеблется
    от версии интерпретатора.
    """
    rng = random.Random(20260619)
    price, closes = 285_000.0, []
    for _ in range(10_000):
        price += rng.uniform(-300, 300)
        closes.append(round(price, 1))

    getcontext().prec = 60
    alpha = Decimal(2) / Decimal(16)
    exact: Decimal | None = None
    worst = Decimal(0)
    average = MovingAverage(15, AverageKind.EMA)

    for index, close in enumerate(closes):
        if exact is None:
            if index >= 14:
                exact = sum(Decimal(str(x)) for x in closes[:15]) / 15
        else:
            exact = exact + alpha * (Decimal(str(close)) - exact)
        ours = average.push(close)
        if exact is not None and ours is not None:
            worst = max(worst, abs(Decimal(ours) - exact) / abs(exact))

    assert worst > 0, "расхождения нет вовсе — замер выродился"
    assert worst < Decimal("1e-14"), f"дрейф двоичной средней вырос до {worst}"


def test_replay_recomputes_the_whole_history() -> None:
    """Пересчёт по истории равен расчёту с нуля — важно при смене периода."""
    closes = [10, 12, 9, 13, 8, 12, 10, 11, 15, 14]
    replayed = MovingAverage(5, AverageKind.EMA)
    replayed.push(999)  # мусор от прежней жизни объекта
    replayed.replay(closes)

    fresh = MovingAverage(5, AverageKind.EMA)
    for close in closes:
        fresh.push(close)

    assert replayed.value == fresh.value
    assert replayed.count == fresh.count == len(closes)


def test_reset_forgets_everything() -> None:
    average = MovingAverage(3)
    for close in (10, 11, 12, 13):
        average.push(close)
    average.reset()
    assert average.value is None
    assert average.count == 0
    assert average.push(50) is None


@pytest.mark.parametrize("period", [0, -1, -300])
def test_period_below_one_is_refused(period: int) -> None:
    with pytest.raises(ValueError, match="не меньше 1"):
        MovingAverage(period)


@pytest.mark.parametrize("period", [2.5, "15", None, True])
def test_non_integer_period_is_refused(period: object) -> None:
    """Дробный период молча превратил бы формулу в другую.

    `True` в списке намеренно: булево значение — подкласс `int`, и без явной
    проверки период стал бы единицей, а средняя — ценой закрытия.
    """
    with pytest.raises(TypeError, match="целое число"):
        MovingAverage(period)  # type: ignore[arg-type]


@pytest.mark.parametrize("close", [float("nan"), float("inf"), float("-inf")])
def test_a_non_number_close_is_refused(close: float) -> None:
    """Отчёт обязан спотыкаться о `nan` там же, где спотыкается робот.

    Торговый путь отвергает `nan` в `check_bar`. Если второй публичный вход
    в ту же арифметику этой защиты не имеет, на одних и тех же порченых данных
    робот останавливается вслух, а прогон по истории печатает пустую колонку.
    """
    with pytest.raises(ValueError, match="не число"):
        MovingAverage(3).push(close)
    with pytest.raises(ValueError, match="не число"):
        average_series([10, 11, close, 13], 3)


def test_a_foreign_average_kind_is_refused() -> None:
    """Одноимённое перечисление другого слоя молча дало бы экспоненциальную.

    Ветка выбора — сравнение по идентичности, а значения строк у слоя окна
    те же самые («ema», «sma») и это закреплено отдельным тестом. Значит
    подстановка проходит без ошибки и без эффекта: владелец счёта выбрал
    простую среднюю, робот считает экспоненциальную, и увидеть это можно
    только сравнением графика с журналом решений.
    """
    import ui.models

    assert ui.models.AverageKind.SMA.value == AverageKind.SMA.value, (
        "значения разошлись — этот тест перестал воспроизводить ловушку"
    )
    with pytest.raises(TypeError, match="тип средней"):
        MovingAverage(3, ui.models.AverageKind.SMA)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="тип средней"):
        average_series([1, 2, 3], 3, "sma")  # type: ignore[arg-type]
