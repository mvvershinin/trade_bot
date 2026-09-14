"""Алгоритм №2: пересечение свечи со средней. Правило по частям.

Что здесь проверяется и почему именно это
-----------------------------------------
Порт (прогрев, `skip_bar`, повтор свечи, ход времени назад, `reset`) уже
проверен набором, параметризованным по реестру
(`tests/test_strategies_contract.py`), а описание правила словами —
исполняется через сам модуль (`tests/test_strategies_description.py`).
Оба набора алгоритм №2 получил даром, и повторять их здесь незачем.

Остаётся то, чего не проверяет ни один общий набор: **само правило**,
три части которого можно испортить порознь и ни разу не уронить прогон.

1. **Средняя берётся у предыдущей свечи.** Подмена на среднюю, уже
   включившую закрытие текущей, даёт другой список сделок и ни одного
   падения: обе величины существуют, обе числа, разница в доли процента.
2. **Пересечение меряется размахом, а не телом.** Тень, дотянувшаяся
   до линии, — это сигнал. Свеча, стоящая выше линии целиком, — не сигнал,
   и это главное отличие от алгоритма №1, который на такой свече говорит
   «хочу быть в лонге» **на каждой** свече подряд.
3. **Сторону задаёт наклон средней, а не место закрытия.** У
   экспоненциальной средней эти два способа совпадают, и подмена одного
   другим на умолчаниях невидима. Различает их простая средняя — там
   и стоит опыт.

⚠️ Изоляция (правило 14 `CLAUDE.md`): каждая проверка сама строит ряд,
который ей нужен, и ничего не берёт у соседей. Файл проверен запуском
по одному тесту, а не только целиком.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from strategies import (
    AverageKind,
    Bar,
    EmaReverse,
    EmaReverseSettings,
    Intent,
    MaCrossing,
)

STEP = timedelta(minutes=5)
START = datetime(2026, 6, 19, 10, 0)

#: Цена прогрева. Ровная нарочно: на ровном ряде средняя равна этой цене
#: и у экспоненциальной, и у простой — значит «средняя предыдущей свечи»
#: известна проверке точно, а не приблизительно.
FLAT = 100.0


def feed(module, prices: list[tuple[float, float, float, float]], *, at=START):
    """Подать ряд свечей `(open, high, low, close)` и вернуть все решения."""
    out = []
    moment = at
    for one_open, high, low, close in prices:
        moment += STEP
        out.append(module.on_closed_bar(Bar(moment, one_open, high, low, close)))
    return out


def flat_bars(count: int) -> list[tuple[float, float, float, float]]:
    """Ровный ряд: свеча без размаха, цена не меняется."""
    return [(FLAT, FLAT, FLAT, FLAT)] * count


def warmed(settings: EmaReverseSettings | None = None) -> MaCrossing:
    """Прогретый модуль на ровной цене: средняя предыдущей свечи равна `FLAT`."""
    settings = settings or EmaReverseSettings()
    module = MaCrossing(settings)
    feed(module, flat_bars(settings.period))
    assert module.average == FLAT, "ровный ряд дал среднюю не на цене"
    return module


def last_at(settings: EmaReverseSettings | None = None) -> datetime:
    """Время последней свечи прогрева — чтобы следующая шла вперёд."""
    settings = settings or EmaReverseSettings()
    return START + STEP * settings.period


# --------------------------------------------------------------------------
# Тени: пересечение меряется размахом свечи
# --------------------------------------------------------------------------

def test_a_shadow_that_reaches_the_line_is_a_crossing() -> None:
    """Тело выше линии, нижняя тень до неё достала — сигнал есть.

    Это ровно то, чего алгоритм №1 не умеет: он читает только закрытие.
    Мутация, которую проверка ловит: сравнивать вместо `low`/`high`
    открытие и закрытие — тень перестанет считаться, сигнал исчезнет.
    """
    module = warmed()
    decision, = feed(
        module, [(100.5, 101.0, 99.5, 100.5)], at=last_at()
    )
    assert decision.intent is Intent.LONG, decision.reason
    assert "задела" in decision.reason


def test_a_candle_that_stays_above_the_line_is_not_a_crossing() -> None:
    """Свеча целиком над линией — молчание, хотя закрытие выше средней.

    Главное отличие от алгоритма №1, и оно проверяется рядом с ним:
    на той же свече №1 говорит «хочу быть в лонге», №2 молчит. Разойдись
    они здесь — и «второй алгоритм» оказался бы первым под другим именем.
    """
    bar = (101.0, 101.5, 100.5, 101.0)
    crossing, = feed(warmed(), [bar], at=last_at())
    assert crossing.intent is Intent.NONE, crossing.reason
    assert "прошла мимо" in crossing.reason

    reverse = EmaReverse(EmaReverseSettings())
    feed(reverse, flat_bars(EmaReverseSettings().period))
    state, = feed(reverse, [bar], at=last_at())
    assert state.intent is Intent.LONG, (
        "алгоритм №1 перестал отвечать состоянием — сравнивать больше нечего"
    )


def test_the_line_exactly_at_the_low_still_counts() -> None:
    """Минимум ровно на линии — пересечение засчитано.

    Неравенства здесь нестрогие, в отличие от алгоритма №1 (`close > EMA`),
    и это выбор владельца счёта: «минимум ≤ средняя ≤ максимум». Строгие
    неравенства отняли бы ровно те свечи, которые линию и коснулись.
    """
    decision, = feed(warmed(), [(100.5, 101.0, FLAT, 100.5)], at=last_at())
    assert decision.intent is Intent.LONG, decision.reason


# --------------------------------------------------------------------------
# Средняя — предыдущей свечи
# --------------------------------------------------------------------------

def test_the_line_compared_with_is_the_one_from_the_previous_candle() -> None:
    """Сравнение идёт с линией ДО текущей свечи, а не после неё.

    Опыт устроен так, что две трактовки дают **разные** ответы: минимум
    свечи лежит между старым и новым значением средней. Со средней
    предыдущей свечи пересечения нет (минимум выше линии), со средней,
    включившей эту свечу, — есть.

    Мутация, которую это ловит: снять среднюю после `push`, а не до.
    """
    settings = EmaReverseSettings(period=3)
    module = warmed(settings)
    # Закрытие 130 поднимает EMA(3) со 100 до 115: k = 2/(3+1) = 0,5.
    low = 107.0
    decision, = feed(module, [(130.0, 131.0, low, 130.0)], at=last_at(settings))
    assert module.average == 115.0, "проверка построена на другой арифметике"
    assert low > FLAT and low < 115.0, "опыт перестал различать две трактовки"
    assert decision.intent is Intent.NONE, (
        "модуль сравнил размах свечи со средней, уже включившей эту свечу — "
        f"это другое правило: {decision.reason}"
    )


# --------------------------------------------------------------------------
# Сторона — по наклону средней
# --------------------------------------------------------------------------

def test_the_side_comes_from_the_slope_not_from_the_close() -> None:
    """Закрытие выше линии, а линия падает — шорт.

    Единственный опыт, который различает «сторона по наклону» и «сторона
    по закрытию»: у экспоненциальной средней они совпадают всегда
    (`E(i) − E(i−1) = k·(close − E(i−1))`), поэтому взята простая.

    Ряд: 110, 100, 90 — простая средняя за три свечи равна 100. Следующее
    закрытие 101 выше неё, но из окна выпадает 110, и средняя падает
    до 97. Алгоритм №1 на этой свече говорит «лонг», №2 — «шорт».
    """
    settings = EmaReverseSettings(period=3, kind=AverageKind.SMA)
    module = MaCrossing(settings)
    feed(module, [(110.0, 110.0, 110.0, 110.0),
                  (100.0, 100.0, 100.0, 100.0),
                  (90.0, 90.0, 90.0, 90.0)])
    assert module.average == 100.0, "затравка простой средней изменилась"

    decision, = feed(
        module, [(99.0, 102.0, 99.0, 101.0)], at=START + STEP * 3
    )
    assert module.average == pytest.approx(97.0), "проверка построена на другом"
    assert decision.intent is Intent.SHORT, (
        "сторону задало закрытие, а не наклон средней: " + decision.reason
    )
    assert "вниз" in decision.reason

    reverse = EmaReverse(settings)
    feed(reverse, [(110.0, 110.0, 110.0, 110.0),
                   (100.0, 100.0, 100.0, 100.0),
                   (90.0, 90.0, 90.0, 90.0)])
    other, = feed(reverse, [(99.0, 102.0, 99.0, 101.0)], at=START + STEP * 3)
    assert other.intent is Intent.LONG, (
        "алгоритм №1 перестал решать по закрытию — опыт больше не различает"
    )


def test_a_line_that_did_not_move_gives_no_signal() -> None:
    """Свеча задела линию, а линия не сдвинулась — ни входа, ни выхода.

    Достижимо на ровной цене: ночная сессия, остановленный инструмент.
    Выбор зашит и назван в шапке модуля; настройка «Закрытие ровно
    на средней» здесь ни при чём — она про другое событие.
    """
    decision, = feed(warmed(), [(FLAT, 101.0, 99.0, FLAT)], at=last_at())
    assert decision.intent is Intent.NONE, decision.reason
    assert "не изменилась" in decision.reason


@pytest.mark.parametrize(
    "on_equal_value",
    ["long", "short"],
)
def test_the_setting_about_the_close_on_the_line_is_not_read(
    on_equal_value: str,
) -> None:
    """«Закрытие ровно на средней» этот алгоритм не читает — ни одним значением.

    Настройка общая с алгоритмом №1 (класс настроек один), и молчаливое
    её применение здесь было бы торговлей по правилу, которого в описании
    нет. Проверяется на той самой свече, где она сработала бы у соседа:
    закрытие ровно на линии.
    """
    from strategies import OnPriceEqualsAverage

    settings = EmaReverseSettings(on_equal=OnPriceEqualsAverage(on_equal_value))
    decision, = feed(warmed(settings), [(FLAT, 101.0, 99.0, FLAT)], at=last_at())
    assert decision.intent is Intent.NONE, (
        "настройка про закрытие на средней подействовала: " + decision.reason
    )


def test_the_confirmation_setting_is_not_read() -> None:
    """«Подтверждение сигнала» этот алгоритм не читает: сигнал — одна свеча.

    У алгоритма №1 та же настройка задерживает намерение до N свечей
    подряд по одну сторону. Здесь одиночное пересечение обязано дать
    сигнал сразу — иначе поле работало бы наполовину и молча.
    """
    settings = EmaReverseSettings(confirm_bars=6)
    decision, = feed(
        warmed(settings), [(100.5, 101.0, 99.5, 100.5)], at=last_at(settings)
    )
    assert decision.intent is Intent.LONG, decision.reason


# --------------------------------------------------------------------------
# Полоса вокруг средней
# --------------------------------------------------------------------------

def test_the_band_demands_the_whole_crossing_not_a_touch() -> None:
    """С полосой свеча обязана перекрыть её целиком, иначе сигнала нет."""
    settings = EmaReverseSettings(threshold_percent=1.0)  # полоса ±1 пункт от 100
    shallow, = feed(
        warmed(settings), [(100.5, 100.5, 99.5, 100.5)], at=last_at(settings)
    )
    assert shallow.intent is Intent.NONE, shallow.reason

    deep, = feed(
        warmed(settings), [(100.5, 101.5, 98.5, 100.5)], at=last_at(settings)
    )
    assert deep.intent is Intent.LONG, deep.reason
    assert "полоса" in deep.reason


def test_without_a_band_the_thinnest_touch_is_enough() -> None:
    """Порог 0 — дословное «минимум ≤ средняя ≤ максимум», без запаса."""
    decision, = feed(
        warmed(), [(100.001, 100.002, 99.999, 100.001)], at=last_at()
    )
    assert decision.intent is Intent.LONG, decision.reason


# --------------------------------------------------------------------------
# Прогрев и событийность
# --------------------------------------------------------------------------

def test_the_first_decision_lands_on_the_bar_after_the_period() -> None:
    """Первое решение — на свече номер `период + 1`, как у алгоритма №1.

    Средняя предыдущей свечи лишней свечи не требует: линия появляется
    на свече номер `период`, значит на следующей есть обе. Число названо
    человеку в описании и исполняется там же
    (`FactKind.FIRST_DECISION_BAR`); здесь оно проверено напрямую.
    """
    settings = EmaReverseSettings(period=15)
    module = MaCrossing(settings)
    decisions = feed(module, flat_bars(settings.period + 1))
    numbers = [
        number for number, decision in enumerate(decisions, start=1)
        if not decision.skip_bar
    ]
    assert numbers == [16], f"свечи с решением: {numbers}"


def test_the_module_answers_with_an_event_not_with_a_state() -> None:
    """Пересечение одно — сигнал один, дальше молчание. У №1 — сигнал на каждой.

    Это то самое следствие, которое обязан знать владелец счёта: позиция
    держится до следующего пересечения, промежуточных подтверждений нет.
    """
    after = [(101.0 + step, 101.5 + step, 100.6 + step, 101.0 + step)
             for step in range(5)]
    series = [(100.5, 101.0, 99.5, 100.5), *after]

    crossing = feed(warmed(), series, at=last_at())
    assert [one.intent for one in crossing] == [
        Intent.LONG, Intent.NONE, Intent.NONE, Intent.NONE, Intent.NONE,
        Intent.NONE,
    ], [one.reason for one in crossing]

    reverse = EmaReverse(EmaReverseSettings())
    feed(reverse, flat_bars(EmaReverseSettings().period))
    state = feed(reverse, series, at=last_at())
    assert [one.intent for one in state] == [Intent.LONG] * 6, (
        "алгоритм №1 перестал отвечать состоянием — сравнение потеряло смысл"
    )


# --------------------------------------------------------------------------
# Непригодная свеча: отказ вслух, а не тихое «прошла мимо»
# --------------------------------------------------------------------------

@pytest.mark.parametrize("field", ["high", "low"])
def test_a_non_number_in_the_range_is_refused_out_loud(field: str) -> None:
    """`nan` в максимуме или минимуме останавливает модуль, а не гасит сигнал.

    Молчаливый исход здесь особенно коварен: сравнения с `nan` ложны,
    поэтому свеча просто «не задевала» бы линию — робот перестал бы
    торговать, а в журнале стояло бы честное «прошла мимо» на каждой свече
    (правило 13 `CLAUDE.md`).
    """
    prices = {"open": 100.5, "high": 101.0, "low": 99.5, "close": 100.5}
    prices[field] = float("nan")
    with pytest.raises(ValueError, match="не число"):
        warmed().on_closed_bar(Bar(last_at() + STEP, **prices))


def test_a_candle_without_a_range_is_refused_out_loud() -> None:
    """Свеча без максимума и минимума — отказ, а не `AttributeError` внутри.

    `check_bar` про размах не знает: алгоритму №1 он не нужен. Значит
    отказ обязан быть свой и называть, почему без него нельзя.
    """
    class OnlyClose:
        closes_at = last_at() + STEP
        open = 100.5
        close = 100.5

    with pytest.raises(TypeError, match="размах"):
        warmed().on_closed_bar(OnlyClose())  # type: ignore[arg-type]


def test_a_candle_with_the_low_above_the_high_is_refused() -> None:
    """Минимум выше максимума — порча ряда, а не свеча."""
    with pytest.raises(ValueError, match="минимум"):
        warmed().on_closed_bar(
            Bar(last_at() + STEP, 100.5, 99.0, 101.0, 100.5)
        )


# --------------------------------------------------------------------------
# Смена настроек на ходу
# --------------------------------------------------------------------------

def test_changing_the_period_recomputes_the_line_over_the_whole_series() -> None:
    """Новый период пересчитывает среднюю по всей истории, а не только вперёд.

    У экспоненциальной средней прошлое входит в значение: продолженный ряд
    не совпал бы с посчитанным с нуля, и после смены периода робот решал бы
    по линии, которой на графике нет.
    """
    grown = MaCrossing(EmaReverseSettings(period=15))
    series = [(100.0 + step, 101.0 + step, 99.0 + step, 100.0 + step)
              for step in range(40)]
    feed(grown, series)
    lines = grown.apply(EmaReverseSettings(period=5))
    assert lines == ["Период средней: 15 → 5"]

    scratch = MaCrossing(EmaReverseSettings(period=5))
    feed(scratch, series)
    assert grown.average == pytest.approx(scratch.average), (
        "после смены периода средняя не пересчитана по всей истории"
    )
