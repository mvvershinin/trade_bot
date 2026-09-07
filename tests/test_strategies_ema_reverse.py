"""Торговый модуль №1: намерение на закрытой свече и границы, где оно меняется.

Что проверяется и почему именно это (QUALITY.md §5, PROTOTYPE.md §3):

* **Прогрев.** Условие прототипа нестрогое: свечей `<=` периода — сигнала нет.
  Замена на строгое сдвигает весь список сделок на одну свечу и выглядит как
  «почти совпало».
* **Строгие неравенства и закрытие ровно на средней.** Случай на эталонном
  отрезке не встретился ни разу (0 из 10 878), подкрепить цифрами нельзя —
  поэтому все три значения настройки проверяются тестом, а не рассуждением.
* **Отсутствие предыстории.** Тот же ряд, поданный с более ранней границы,
  даёт другой список сделок целиком.
* **Смена настроек на ходу** пересчитывает среднюю по истории, а не только вперёд.
* **Модуль не смотрит на время.** Торговое окно, выходные и паузы — правила
  движка; модуль, который начнёт решать по часам, будет вторым, расходящимся
  с движком источником правды.

Данные синтетические и посчитаны руками. Период 3 взят там, где важна проверяемая
в уме арифметика, период 15 — там, где воспроизводится сцена из PROTOTYPE.md.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from strategies import (
    AverageKind,
    Bar,
    EmaReverse,
    EmaReverseSettings,
    Intent,
    OnPriceEqualsAverage,
)

MSK = timezone(timedelta(hours=3), "MSK")
STEP = timedelta(minutes=5)
DAY = datetime(2026, 6, 19, 10, 0, tzinfo=MSK)


def bars(closes, start: datetime = DAY, step: timedelta = STEP) -> list[Bar]:
    """Свечи с заданными закрытиями. `closes_at` — время ЗАКРЫТИЯ свечи."""
    return [
        Bar(
            closes_at=start + step * (index + 1),
            open=float(close), high=float(close), low=float(close),
            close=float(close), volume=1.0,
        )
        for index, close in enumerate(closes)
    ]


def run(closes, **settings):
    """Прогнать ряд закрытий через свежий модуль и вернуть решения."""
    strategy = EmaReverse(EmaReverseSettings(**settings))
    return [strategy.on_closed_bar(bar) for bar in bars(closes)]


def intents(closes, **settings) -> list[Intent]:
    return [decision.intent for decision in run(closes, **settings)]


# --------------------------------------------------------------------------
# Прогрев: свечей <= периода — сигнала нет
# --------------------------------------------------------------------------

def test_no_signal_until_bars_exceed_the_period() -> None:
    """Свеча №период — ещё нет, №период+1 — уже да. Неравенство нестрогое."""
    decisions = run([10, 11, 12, 13, 14], period=3)

    assert [decision.warmed_up for decision in decisions] == [
        False, False, False, True, True,
    ]
    assert [decision.intent for decision in decisions[:3]] == [Intent.NONE] * 3
    assert decisions[3].intent is not Intent.NONE, (
        "свеча №(период+1) — первая, на которой сигнал возможен"
    )


def test_average_exists_one_bar_before_the_first_signal() -> None:
    """Разрыв в одну свечу: средняя уже есть, сигнал ещё запрещён.

    Это и есть место, где строгое неравенство вместо нестрогого выглядит
    безобидно: значение средней посчитано, соблазн им воспользоваться прямой.
    """
    decisions = run([10, 11, 12, 13, 14], period=3)

    third = decisions[2]
    assert third.average == 11.0, "затравка на третьей свече посчитана"
    assert third.intent is Intent.NONE, "но сигнала на ней быть не должно"
    assert third.warmed_up is False


def test_warmup_boundaries_period_and_next_two(  # период, период+1, период+2
) -> None:
    """Границы прогрева перечислены поимённо, чтобы сдвиг на свечу был виден."""
    period = 3
    decisions = run([10, 11, 12, 13, 14, 15], period=period)

    at_period = decisions[period - 1]
    after = decisions[period]
    after_next = decisions[period + 1]

    assert (at_period.bars, at_period.warmed_up) == (period, False)
    assert (after.bars, after.warmed_up) == (period + 1, True)
    assert (after_next.bars, after_next.warmed_up) == (period + 2, True)


def test_first_signal_repeats_the_scene_from_the_prototype_document() -> None:
    """Первая свеча 08:55, период 15 → первый сигнал на закрытии в 10:15.

    Сцена из PROTOTYPE.md §3, сходящаяся с журналом прототипа: сигнал возможен
    на закрытии 16-й свечи, а самая ранняя сделка — на открытии 17-й.
    """
    start = datetime(2026, 6, 19, 8, 55, tzinfo=MSK)
    closes = list(range(100, 100 + 20))
    strategy = EmaReverse(EmaReverseSettings(period=15))

    decisions = [strategy.on_closed_bar(bar) for bar in bars(closes, start=start)]
    first = next(
        (bar, decision)
        for bar, decision in zip(bars(closes, start=start), decisions)
        if decision.warmed_up
    )

    bar, decision = first
    assert bar.closes_at == datetime(2026, 6, 19, 10, 15, tzinfo=MSK)
    assert decision.bars == 16
    assert decision.intent is Intent.LONG


# --------------------------------------------------------------------------
# Строгие неравенства
# --------------------------------------------------------------------------

def test_close_above_average_is_long_and_below_is_short() -> None:
    """Ручной расчёт: EMA(3) = 11, дальше закрытие уводится в обе стороны."""
    above = run([10, 11, 12, 30], period=3)[-1]
    assert above.average == 11.0 + 0.5 * (30 - 11.0)
    assert above.intent is Intent.LONG

    below = run([10, 11, 12, 1], period=3)[-1]
    assert below.average == 11.0 + 0.5 * (1 - 11.0)
    assert below.intent is Intent.SHORT


def test_a_whole_series_gives_the_expected_intents() -> None:
    """Ряд целиком: намерения совпадают с посчитанными руками средними.

        затравка = (1+2+3)/3 = 2
        E(8)  = 2   + 0,5*(8−2)    = 5      → 8  > 5     лонг
        E(14) = 5   + 0,5*(14−5)   = 9,5    → 14 > 9,5   лонг
        E(10) = 9,5 + 0,5*(10−9,5) = 9,75   → 10 > 9,75  лонг
    """
    decisions = run([1, 2, 3, 8, 14, 10], period=3)
    assert [decision.average for decision in decisions] == [
        None, None, 2.0, 5.0, 9.5, 9.75,
    ]
    assert [decision.intent for decision in decisions] == [
        Intent.NONE, Intent.NONE, Intent.NONE, Intent.LONG, Intent.LONG, Intent.LONG,
    ]


# --------------------------------------------------------------------------
# Закрытие ровно на средней — три значения настройки
# --------------------------------------------------------------------------

# Закрытие, равное предыдущему значению EMA, оставляет EMA на месте:
# E(i) = E(i−1) + k*(close − E(i−1)), и при close == E(i−1) слагаемое ноль.
# Затравка (10+11+12)/3 = 11 представима в двоичной плавающей точке точно,
# поэтому равенство здесь настоящее, а не «почти».
EQUAL_ON_EMA = [10, 11, 12, 11]
# Для простой средней: (12 + 10 + 11) / 3 = 11 — окно даёт ровно закрытие.
EQUAL_ON_SMA = [10, 12, 10, 11]


@pytest.mark.parametrize(
    "kind, closes",
    [(AverageKind.EMA, EQUAL_ON_EMA), (AverageKind.SMA, EQUAL_ON_SMA)],
)
def test_the_equality_case_is_really_reached(kind: AverageKind, closes: list[int]) -> None:
    """Сначала докажем, что равенство действительно наступило.

    Без этой проверки три теста ниже могли бы проверять обычный сигнал
    и остаться зелёными при любой настройке.
    """
    decision = run(closes, period=3, kind=kind)[-1]
    assert decision.warmed_up is True
    assert decision.close == decision.average == 11.0


@pytest.mark.parametrize(
    "kind, closes",
    [(AverageKind.EMA, EQUAL_ON_EMA), (AverageKind.SMA, EQUAL_ON_SMA)],
)
def test_equal_like_prototype_gives_no_signal(kind: AverageKind, closes: list[int]) -> None:
    """Умолчание — как у прототипа: ни входа, ни выхода (PROTOTYPE.md §3)."""
    decision = run(
        closes, period=3, kind=kind, on_equal=OnPriceEqualsAverage.LIKE_PROTOTYPE
    )[-1]
    assert decision.intent is Intent.NONE
    assert "ровно" in decision.reason


@pytest.mark.parametrize(
    "kind, closes",
    [(AverageKind.EMA, EQUAL_ON_EMA), (AverageKind.SMA, EQUAL_ON_SMA)],
)
def test_equal_can_be_read_as_long(kind: AverageKind, closes: list[int]) -> None:
    decision = run(
        closes, period=3, kind=kind, on_equal=OnPriceEqualsAverage.TREAT_AS_LONG
    )[-1]
    assert decision.intent is Intent.LONG
    assert "настройке" in decision.reason


@pytest.mark.parametrize(
    "kind, closes",
    [(AverageKind.EMA, EQUAL_ON_EMA), (AverageKind.SMA, EQUAL_ON_SMA)],
)
def test_equal_can_be_read_as_short(kind: AverageKind, closes: list[int]) -> None:
    decision = run(
        closes, period=3, kind=kind, on_equal=OnPriceEqualsAverage.TREAT_AS_SHORT
    )[-1]
    assert decision.intent is Intent.SHORT
    assert "настройке" in decision.reason


def test_the_equality_setting_does_not_touch_ordinary_bars() -> None:
    """Настройка меняет только случай равенства, остальной ряд — нет."""
    closes = [1, 2, 3, 8, 14, 10, 4, 20]
    base = intents(closes, period=3)
    for setting in OnPriceEqualsAverage:
        assert intents(closes, period=3, on_equal=setting) == base


# --------------------------------------------------------------------------
# EMA против SMA
# --------------------------------------------------------------------------

def test_ema_and_sma_can_disagree_on_the_same_bar() -> None:
    """Один ряд, разные типы средней — разные намерения.

        EMA(3): … 9,5 → 9,75  и закрытие 10 выше   → лонг
        SMA(3): (8+14+10)/3 = 10,67, закрытие ниже → шорт

    Тест держит переключатель настоящим: если тип средней перестанет читаться,
    оба прогона дадут одно и то же и тест станет красным.
    """
    closes = [1, 2, 3, 8, 14, 10]
    ema = run(closes, period=3, kind=AverageKind.EMA)[-1]
    sma = run(closes, period=3, kind=AverageKind.SMA)[-1]

    assert ema.average == 9.75 and ema.intent is Intent.LONG
    assert sma.average == pytest.approx(32 / 3) and sma.intent is Intent.SHORT


def test_default_kind_is_ema() -> None:
    """Умолчание — экспоненциальная средняя (решение 0004)."""
    settings = EmaReverseSettings()
    assert settings.kind is AverageKind.EMA
    assert settings.period == 15
    assert settings.on_equal is OnPriceEqualsAverage.LIKE_PROTOTYPE
    assert settings.label == "EMA(15)"


# --------------------------------------------------------------------------
# Прогрев от начала поданного ряда, предыстории нет
# --------------------------------------------------------------------------

def test_the_same_tail_with_earlier_history_decides_differently() -> None:
    """Тот же отрезок с более ранней границей — другие решения (PROTOTYPE.md §3).

    К общей дате средняя приходит уже прогретой, поэтому при сверке отрезок
    обязан совпадать до свечи, а не «примерно тот же период».
    """
    tail = [10, 12, 9, 13, 8, 12, 10]
    prefix = [30, 28, 26, 24]

    alone = intents(tail, period=3)
    with_history = intents(prefix + tail, period=3)[len(prefix):]

    assert alone != with_history
    assert alone[:3] == [Intent.NONE, Intent.NONE, Intent.NONE], "прогрев с нуля"
    assert Intent.NONE not in with_history, "с предысторией прогрев уже позади"


def test_reset_starts_the_warmup_again() -> None:
    series = bars([10, 11, 12, 13, 14])
    strategy = EmaReverse(EmaReverseSettings(period=3))
    for bar in series[:4]:
        strategy.on_closed_bar(bar)
    assert strategy.bars == 4

    strategy.reset()
    assert strategy.bars == 0
    assert strategy.average is None
    assert strategy.on_closed_bar(series[4]).warmed_up is False


# --------------------------------------------------------------------------
# Модуль не смотрит на время
# --------------------------------------------------------------------------

def test_intents_do_not_depend_on_when_the_bars_closed() -> None:
    """Те же закрытия в выходные, ночью и в другом году — те же намерения.

    Торговое окно, дни недели и паузы — правила движка (ARCHITECTURE.md §3).
    Средняя при этом считается по **всем** поданным свечам, включая выходные
    и вечернюю сессию: окно гасит торговлю, а не расчёт (PROTOTYPE.md §3).
    Модуль не может нарушить это правило, даже если захочет: он не знает,
    какой день на дворе.
    """
    closes = [1, 2, 3, 8, 14, 10, 4, 20]

    workday = intents(closes)

    strategy = EmaReverse()
    weekend = [
        strategy.on_closed_bar(bar).intent
        for bar in bars(
            closes,
            start=datetime(2026, 6, 20, 23, 45, tzinfo=MSK),  # ночь с субботы
            step=timedelta(minutes=30),
        )
    ]

    assert workday == weekend


# --------------------------------------------------------------------------
# Настройки на ходу
# --------------------------------------------------------------------------

def test_the_same_bar_twice_is_skipped_not_thrown() -> None:
    """Повтор свечи — штатное событие потока: решение с `skip_bar`, не исключение.

    Брокер присылает последнюю закрытую свечу ещё раз после переподключения.
    Принятая молча, она входит в среднюю дважды и сдвигает прогрев — другой
    список сделок без единого падения по дороге. Но и исключение здесь вредно:
    движок либо уронил бы цикл обработки (робот стоит с открытой позицией
    и больше не получает сигналов), либо обернул бы вызов широким `except`.
    """
    series = bars([10, 11, 12, 13])
    strategy = EmaReverse(EmaReverseSettings(period=3))
    for bar in series:
        strategy.on_closed_bar(bar)

    again = strategy.on_closed_bar(series[-1])
    assert again.skip_bar is True
    assert again.intent is Intent.NONE
    assert "повторно" in again.reason
    assert again.warmed_up is True, "прогрев давно позади — это не прогрев"

    assert strategy.bars == 4, "повторная свеча не должна попадать в историю"
    assert strategy.average == 12.0


def test_a_bar_from_the_past_is_refused() -> None:
    """Ход времени назад — порча ряда, а не штатное событие. Отказ."""
    series = bars([10, 11, 12, 13])
    strategy = EmaReverse(EmaReverseSettings(period=3))
    for bar in series:
        strategy.on_closed_bar(bar)

    with pytest.raises(ValueError, match="вперёд по времени"):
        strategy.on_closed_bar(series[0])
    assert strategy.bars == 4
    assert strategy.average == 12.0


def test_after_reset_the_series_may_start_from_the_beginning_again() -> None:
    """Новый отрезок начинается с `reset()` — и время снова идёт с начала."""
    series = bars([10, 11, 12, 13])
    strategy = EmaReverse(EmaReverseSettings(period=3))
    for bar in series:
        strategy.on_closed_bar(bar)
    strategy.reset()
    assert strategy.on_closed_bar(series[0]).bars == 1


def test_mixing_aware_and_naive_bar_times_is_reported_clearly() -> None:
    """Смесь времени с поясом и без — понятный отказ, а не `TypeError` из недр."""
    strategy = EmaReverse(EmaReverseSettings(period=3))
    strategy.on_closed_bar(bars([10])[0])
    naive = Bar(
        closes_at=datetime(2026, 6, 19, 11, 0),  # без пояса
        open=1.0, high=1.0, low=1.0, close=11.0,
    )
    with pytest.raises(TypeError, match="часовым"):
        strategy.on_closed_bar(naive)


def test_changing_the_period_recomputes_the_average_over_history() -> None:
    """Смена периода пересчитывает индикатор на истории, а не только вперёд.

    Иначе экспоненциальная средняя осталась бы с памятью прежнего периода,
    и робот считал бы не то, что показывает график после смены настройки.
    """
    closes = [10, 12, 9, 13, 8, 12, 10, 11, 15, 14]
    tail = 17

    changed = EmaReverse(EmaReverseSettings(period=3))
    for bar in bars(closes):
        changed.on_closed_bar(bar)
    lines = changed.apply(EmaReverseSettings(period=5))
    after_change = changed.on_closed_bar(bars(closes + [tail])[-1])

    fresh = EmaReverse(EmaReverseSettings(period=5))
    expected = [fresh.on_closed_bar(bar) for bar in bars(closes + [tail])][-1]

    assert lines == ["Период средней: 3 → 5"]
    assert after_change.average == expected.average
    assert after_change.intent is expected.intent
    assert after_change.bars == expected.bars


def test_changing_the_kind_recomputes_too() -> None:
    closes = [1, 2, 3, 8, 14, 10]
    changed = EmaReverse(EmaReverseSettings(period=3, kind=AverageKind.EMA))
    for bar in bars(closes[:-1]):
        changed.on_closed_bar(bar)
    changed.apply(EmaReverseSettings(period=3, kind=AverageKind.SMA))
    last = changed.on_closed_bar(bars(closes)[-1])

    assert last.average == pytest.approx(32 / 3)
    assert last.intent is Intent.SHORT


def test_settings_change_is_reported_for_the_journal() -> None:
    """Изменение настройки пишется с прежним и новым значением (ТЗ §4.4 А)."""
    strategy = EmaReverse(EmaReverseSettings(period=15))
    lines = strategy.apply(
        EmaReverseSettings(
            period=20,
            kind=AverageKind.SMA,
            on_equal=OnPriceEqualsAverage.TREAT_AS_LONG,
        )
    )
    assert len(lines) == 3
    assert lines[0] == "Период средней: 15 → 20"
    assert "Экспоненциальная (EMA) → Простая (SMA)" in lines[1]
    assert "Считать сигналом на лонг" in lines[2]
    assert strategy.settings.period == 20


def test_applying_the_same_settings_says_nothing() -> None:
    """Нечего писать в журнал — значит и строки нет."""
    strategy = EmaReverse(EmaReverseSettings(period=15))
    assert strategy.apply(EmaReverseSettings(period=15)) == []


def test_applying_settings_does_not_lose_the_history() -> None:
    """Смена настройки не сбрасывает прогрев: свечи остаются накопленными."""
    series = bars([10, 11, 12, 13, 14])
    strategy = EmaReverse(EmaReverseSettings(period=3))
    for bar in series[:4]:
        strategy.on_closed_bar(bar)
    strategy.apply(EmaReverseSettings(period=3, kind=AverageKind.SMA))
    assert strategy.bars == 4
    assert strategy.on_closed_bar(series[4]).warmed_up is True


@pytest.mark.parametrize("period", [0, -5])
def test_settings_refuse_a_period_below_one(period: int) -> None:
    with pytest.raises(ValueError, match="не меньше 1"):
        EmaReverseSettings(period=period)


def test_settings_refuse_a_foreign_enum() -> None:
    """Значение не из списка молча осталось бы умолчанием — как у прототипа.

    Прототип принимает параметр неверного типа без ошибки и продолжает работать
    на своём умолчании: сверка при этом идёт не с той конфигурацией, которую
    задавали (PROTOTYPE.md §1). Здесь это отказ.
    """
    with pytest.raises(TypeError, match="тип средней"):
        EmaReverseSettings(kind="ema")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="ровно на средней"):
        EmaReverseSettings(on_equal="skip")  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Причина человеческим языком
# --------------------------------------------------------------------------

def test_every_decision_carries_a_human_reason() -> None:
    """Действие без строки в журнале — дефект, даже если оно верное (ТЗ §4.6)."""
    decisions = run([1, 2, 3, 8, 14, 10], period=3)
    for decision in decisions:
        assert decision.reason
        assert "=" not in decision.reason, "причина, а не отладочный дамп состояния"
        assert "None" not in decision.reason


def test_reason_names_the_average_and_the_side() -> None:
    warmup, *_, last = run([1, 2, 3, 8, 14, 10], period=3)
    assert warmup.reason == "Прогрев EMA(3): накоплено свечей 1 из 4 — сигналов нет"
    assert last.reason == "Закрытие 10 выше EMA(3) 9,75"


def test_reason_formats_prices_the_way_the_owner_reads_them() -> None:
    """Пробел между тысячами, запятая в дробной части — как в отчётах."""
    # затравка (285 000 + 284 800 + 285 400) / 3 = 285 066,666667
    # E = 285 066,666667 + 0,5 * (286 150 − 285 066,666667) = 285 608,333333
    decision = run([285_000, 284_800, 285_400, 286_150], period=3)[-1]
    assert decision.reason == "Закрытие 286 150 выше EMA(3) 285 608,333333"


def test_two_runs_of_the_same_series_agree() -> None:
    """Модуль детерминирован: тот же ряд — те же решения."""
    closes = [1, 2, 3, 8, 14, 10, 4, 20, 19, 18]
    assert intents(closes, period=3) == intents(closes, period=3)


def test_every_setting_is_reported_when_it_changes() -> None:
    """Каждое поле настроек обязано попадать в журнал при изменении.

    Проверка не про красоту журнала: `apply()` решает по этому же списку,
    надо ли пересчитывать среднюю. Поле, забытое в `changes_from`, означало бы
    молча не пересчитанный индикатор — робот считает по старому периоду,
    а в окне стоит новый.
    """
    base = EmaReverseSettings()
    others: dict[str, object] = {
        "period": 20,
        "kind": AverageKind.SMA,
        "on_equal": OnPriceEqualsAverage.TREAT_AS_SHORT,
        "threshold_percent": 0.05,
        "confirm_bars": 3,
    }
    assert set(others) == set(EmaReverseSettings.__dataclass_fields__), (
        "у настроек модуля появилось поле, не покрытое этой проверкой"
    )
    for field, value in others.items():
        changed = base.replace(**{field: value})
        assert changed.changes_from(base), f"изменение «{field}» не попало в журнал"
        assert base.changes_from(changed), "и в обратную сторону тоже"


def test_a_longer_period_puts_the_module_back_into_warmup() -> None:
    """Период вырос, свечей стало «мало» — сигналов снова нет.

    Поведение неочевидное, поэтому закреплено: свечей у модуля 5, новый период
    10, и до 11-й свечи он молчит. Альтернатива — считать прогрев пройденным
    один раз навсегда — дала бы сигнал по недосчитанной средней.
    """
    series = bars([10, 11, 12, 13, 14, 15])
    strategy = EmaReverse(EmaReverseSettings(period=3))
    for bar in series[:4]:
        strategy.on_closed_bar(bar)
    assert strategy.on_closed_bar(series[4]).warmed_up is True

    strategy.apply(EmaReverseSettings(period=10))
    back_in_warmup = strategy.on_closed_bar(series[5])
    assert back_in_warmup.bars == 6
    assert back_in_warmup.warmed_up is False
    assert back_in_warmup.intent is Intent.NONE
    assert back_in_warmup.average is None


# --------------------------------------------------------------------------
# Свеча в обработку не идёт: шаг 2 прототипа
# --------------------------------------------------------------------------

def test_warmup_bars_are_marked_as_not_to_be_processed() -> None:
    """На прогреве движок обязан выйти из обработки свечи целиком.

    Шаг 2 `PROTOTYPE.md` §2 выходит **до** шага 5 (переставить тейк) и **до**
    шага 6 (закрытие по концу окна). Прочитать прогрев как обычное «сигнала
    нет» — значит на первых `period` свечах переставлять тейки и закрывать
    позицию по концу окна там, где прототип не делает ни того, ни другого.

    На эталонном прогоне разницы не видно: прогрев 19.06 приходится
    на 08:55–10:10, позиции ещё нет, шаги 5 и 6 пустые. Сверка с прототипом
    эту ошибку не поймает — поэтому она проверяется здесь.
    """
    decisions = run([10, 11, 12, 13, 14], period=3)

    assert [decision.skip_bar for decision in decisions] == [
        True, True, True, False, False,
    ]
    for decision in decisions[:3]:
        assert decision.intent is Intent.NONE
        assert "Прогрев" in decision.reason


def test_an_ordinary_bar_is_processed_even_when_the_intent_is_none() -> None:
    """«Ни входа, ни выхода» — не то же самое, что «свечу не обрабатывать».

    При закрытии ровно на средней движок продолжает обработку: переставляет
    тейк, закрывает по концу окна. Разницу между двумя видами `NONE` несёт
    `skip_bar`, и её нельзя вывести из намерения.
    """
    decision = run(EQUAL_ON_EMA, period=3)[-1]
    assert decision.intent is Intent.NONE
    assert decision.skip_bar is False
    assert decision.warmed_up is True


def test_changing_only_the_equality_setting_leaves_the_average_alone() -> None:
    """Поведение при равенстве в среднюю не входит — пересчитывать нечего."""
    strategy = EmaReverse(EmaReverseSettings(period=3))
    for bar in bars([10, 11, 12, 13]):
        strategy.on_closed_bar(bar)
    before = strategy.average

    lines = strategy.apply(
        EmaReverseSettings(period=3, on_equal=OnPriceEqualsAverage.TREAT_AS_LONG)
    )
    assert lines and "ровно на средней" in lines[0]
    assert strategy.average == before
    assert strategy.bars == 4


# --------------------------------------------------------------------------
# Ровная цена: класс расхождения, которого не видно на эталонном наборе
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "price, period", [(0.03, 15), (0.11, 5), (0.03, 9), (0.11, 20), (0.03, 30)]
)
@pytest.mark.parametrize("kind", list(AverageKind))
def test_a_flat_price_never_produces_a_signal(
    price: float, period: int, kind: AverageKind
) -> None:
    """Цена стоит на месте — сигнала нет ни одного. Ни у прототипа, ни у нас.

    Класс расхождения, которого нет на эталонном наборе и который сверка
    поэтому не ловит. Прототип считает в десятичном типе, где 0,03
    представима точно: средняя равна цене, неравенства строгие, ответ —
    «ни входа, ни выхода». В двоичной арифметике затравка промахивается
    мимо цены на 1 ULP и застревает там навсегда: до исправления это давало
    25 сигналов из 40 свечей.

    Достижимость: для целых котировок (весь срочный рынок) — 0 %,
    для сеток 0,01 и мельче — 5–15 % цен. Мёртвый флэт в начале ряда бывает:
    ночная сессия, остановленная бумага, первые свечи набора с одной ценой.
    """
    decisions = run([price] * (period + 25), period=period, kind=kind)
    signals = [
        decision for decision in decisions if decision.intent is not Intent.NONE
    ]
    assert not signals, (
        f"{kind.short_label}({period}) на ровной цене {price} дала "
        f"{len(signals)} сигналов из {len(decisions)}"
    )
    assert decisions[-1].average == price


# --------------------------------------------------------------------------
# Фильтр против пилы: порог пересечения и подтверждение сигнала
# --------------------------------------------------------------------------
# Просьба владельца счёта 05.09.2026. Оба фильтра выключены умолчанием, и
# **на умолчании стоит сверка с прототипом** — 127 сделок из 127. Поэтому
# первые две проверки здесь мутационные: они сравнивают решения с фильтром,
# выставленным в «выключено», и без него вовсе — построчно, вместе с текстом
# причины. Тест, который проверял бы только `intent`, пропустил бы разъехавшийся
# журнал; тест, который сравнивал бы итог, пропустил бы обоих.

#: Ряд с пилой: цена дёргается вокруг средней, потом уходит. Числа подобраны
#: так, чтобы были и слабые пересечения, и сильные, — иначе фильтр нечем
#: отличить от глушилки.
SAWTOOTH = [
    100, 101, 102, 103, 102, 103, 102, 103, 102, 103,
    104, 108, 112, 111, 112, 110, 105, 100, 99, 103,
    102, 103, 102, 101, 100, 104, 108, 107, 108, 106,
]


def told(closes, **settings) -> list[tuple[Intent, str]]:
    """Решения ряда как пары «намерение, причина» — для построчного сравнения."""
    return [(item.intent, item.reason) for item in run(closes, **settings)]


def test_the_defaults_leave_the_prototypes_bare_comparison() -> None:
    """Умолчание обоих фильтров — «выключено». На нём стоит сверка 127 из 127."""
    settings = EmaReverseSettings()
    assert settings.threshold_percent == 0.0
    assert settings.confirm_bars == 1


def test_a_zero_threshold_does_not_move_a_single_decision() -> None:
    """Порог 0 — сегодняшнее поведение дословно, вместе с текстом причины."""
    assert told(SAWTOOTH, period=3, threshold_percent=0.0) == told(SAWTOOTH, period=3)


def test_a_confirmation_of_one_bar_does_not_move_a_single_decision() -> None:
    """Подтверждение в одну свечу — сегодняшнее поведение дословно."""
    assert told(SAWTOOTH, period=3, confirm_bars=1) == told(SAWTOOTH, period=3)


def test_the_threshold_cuts_the_weak_crossing_and_keeps_the_strong_one() -> None:
    """Порог отсекает слабое пересечение и **не трогает** сильное.

    Без этой пары фильтр неотличим от глушилки: тест, где всё гасится,
    прошёл бы и на `return Intent.NONE`.
    """
    plain = intents(SAWTOOTH, period=3)
    filtered = intents(SAWTOOTH, period=3, threshold_percent=1.0)

    muted = [
        index for index, (was, now) in enumerate(zip(plain, filtered, strict=True))
        if was is not now
    ]
    kept = [
        index for index, (was, now) in enumerate(zip(plain, filtered, strict=True))
        if was is now and was is not Intent.NONE
    ]
    assert muted, "порог 1% не погасил ни одного сигнала — фильтр не работает"
    assert kept, "порог 1% погасил все сигналы — это глушилка, а не фильтр"
    assert all(filtered[index] is Intent.NONE for index in muted), (
        "погашенный сигнал обязан становиться «ничего», а не переворачиваться"
    )


def test_the_threshold_mutes_the_exit_as_well_as_the_entry() -> None:
    """Слабый **обратный** сигнал тоже гасится — позиция переживает пилу.

    Следствие, о котором обязан знать владелец счёта: реверсная система
    выходит по обратному сигналу, и погашенный обратный сигнал означает,
    что робот остаётся в позиции.
    """
    # Затравка EMA(3) по 100, 101, 102 равна 101. Дальше:
    #   110 → EMA 105,5,   отклонение +4,3 %  — сильный лонг;
    #   105 → EMA 105,25,  отклонение −0,24 % — слабый обратный сигнал;
    #    90 → EMA 97,625,  отклонение −7,8 %  — сильный обратный.
    closes = [100, 101, 102, 110, 105, 90]
    assert intents(closes, period=3)[3:] == [Intent.LONG, Intent.SHORT, Intent.SHORT]
    assert intents(closes, period=3, threshold_percent=0.5)[3:] == [
        Intent.LONG, Intent.NONE, Intent.SHORT,
    ]


def test_the_edge_of_the_band_is_outside_it_like_the_prototypes_inequality() -> None:
    """Закрытие **ровно** на краю полосы сигналом не считается: неравенство строгое.

    Порог здесь неправдоподобно велик — 50 % — и это осознанно: край полосы
    считается от средней, а средняя включает саму эту свечу, поэтому «закрытие
    ровно на краю» существует лишь при подобранных числах. При 50 % они
    круглые и в двоичной арифметике точные: EMA(3) по 100, 100, 100, 300
    равна 200, край полосы — ровно 300. Подбирать «жизненный» порог значило бы
    сравнивать с точностью до младшего разряда и проверять округление,
    а не неравенство.
    """
    assert intents([100, 100, 100, 300], period=3, threshold_percent=50.0)[-1] is (
        Intent.NONE
    )
    # Соседняя свеча: EMA 200,5, край 300,75, закрытие 301 — уже сигнал.
    assert intents([100, 100, 100, 301], period=3, threshold_percent=50.0)[-1] is (
        Intent.LONG
    )


def test_the_threshold_outranks_the_setting_about_price_equal_to_the_average() -> None:
    """Внутри полосы настройка «считать сигналом на лонг» не читается.

    Порог — правило более сильное: включивший его просил не входить на слабом
    пересечении, а равенство лежит в самой середине мёртвой зоны.
    """
    decisions = run(
        [100, 100, 100, 100], period=3, threshold_percent=0.5,
        on_equal=OnPriceEqualsAverage.TREAT_AS_LONG,
    )
    assert decisions[-1].intent is Intent.NONE
    assert "порог" in decisions[-1].reason
    # А без порога та же настройка работает как раньше.
    assert run(
        [100, 100, 100, 100], period=3,
        on_equal=OnPriceEqualsAverage.TREAT_AS_LONG,
    )[-1].intent is Intent.LONG


#: Ряд для подтверждения сигнала. Стороны у него такие (период 3, точки —
#: прогрев и первая свеча после него): ``...LLLSSSLLLLL``. Три ровных участка
#: подряд — на них видно и как счётчик набирается, и как он сбрасывается
#: при перевороте стороны.
RUNS = [100, 101, 102, 110, 108, 107, 106, 90, 95, 97, 99, 120, 121, 122]


def sides(closes, **settings) -> str:
    """Стороны решений одной строкой: ``L``, ``S``, точка — «ничего»."""
    return "".join(
        {Intent.LONG: "L", Intent.SHORT: "S", Intent.NONE: "."}[item]
        for item in intents(closes, **settings)
    )


def test_confirmation_delays_the_signal_until_the_bars_agree() -> None:
    """Сигнал появляется на N-й свече подряд по одну сторону, не раньше.

    Ожидания выписаны руками от строки сторон ``...LLLSSSLLLLL``: при N=2
    гасится первая свеча каждого участка, при N=3 — две первых, при N=4
    участков длиной 4 всего один.
    """
    assert sides(RUNS, period=3) == "...LLLSSSLLLLL"
    assert sides(RUNS, period=3, confirm_bars=2) == "....LL.SS.LLLL"
    assert sides(RUNS, period=3, confirm_bars=3) == ".....L..S..LLL"
    assert sides(RUNS, period=3, confirm_bars=4) == "............LL"


def test_confirmation_does_not_count_the_warmup_bar_that_seeded_the_average() -> None:
    """Свеча затравки в подтверждение не идёт, хотя средняя на ней уже есть.

    Она стоит ровно на границе прогрева: средняя посчитана, а сигнал ещё
    запрещён (`свечей <= периода`). Засчитав её, модуль подтверждал бы первый
    сигнал ряда свечой, на которой сам действовать отказался, — и первый
    сигнал выходил бы на свечу раньше всех последующих.
    """
    # Закрытие 102 на свече затравки лежит выше EMA(3) = 101, то есть сторона
    # у неё есть. Если бы она считалась, при N=2 сигнал встал бы на индекс 3.
    assert sides(RUNS, period=3, confirm_bars=2)[3] == "."
    assert sides(RUNS, period=3, confirm_bars=2)[4] == "L"


def test_changing_the_period_recounts_the_confirmation_over_the_history() -> None:
    """Новый период пересчитывает и подтверждение, а не только среднюю.

    Счётчик, оставшийся от прежнего периода, выглядел бы как «фильтр иногда
    не срабатывает»: сигнал подтверждён свечами, которые с новым периодом
    лежат по другую сторону средней.
    """
    closes = RUNS + [118, 117, 116, 130, 131, 132]
    switch = 10
    later = slice(switch, None)

    from_scratch = sides(closes, period=5, confirm_bars=3)
    unchanged = sides(closes, period=3, confirm_bars=3)

    strategy = EmaReverse(EmaReverseSettings(period=3, confirm_bars=3))
    series = bars(closes)
    for bar in series[:switch]:
        strategy.on_closed_bar(bar)
    strategy.apply(EmaReverseSettings(period=5, confirm_bars=3))
    after = "".join(
        {Intent.LONG: "L", Intent.SHORT: "S", Intent.NONE: "."}[
            strategy.on_closed_bar(bar).intent
        ]
        for bar in series[switch:]
    )

    assert after == from_scratch[later], (
        "после смены периода модуль обязан вести себя как посчитанный с нуля"
    )
    assert after != unchanged[later], (
        "смена периода на этом ряде ничего не изменила — проверка вакуумна"
    )


@pytest.mark.parametrize(
    "value, error",
    [
        (-0.1, ValueError), (float("nan"), ValueError), (float("inf"), ValueError),
        (True, TypeError), ("0.5", TypeError), (None, TypeError),
    ],
)
def test_a_threshold_that_makes_no_sense_is_refused_out_loud(
    value: object, error: type[Exception]
) -> None:
    """Порог, на котором расчёт теряет смысл, отвергается на входе.

    `nan` отдельно: сравнения с ним ложны, поэтому полоса из `nan` проглотила
    бы **каждое** закрытие — робот молча перестал бы торговать вовсе, и
    в журнале не было бы ни строки о причине.
    """
    with pytest.raises(error):
        EmaReverseSettings(threshold_percent=value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "value, error",
    [(0, ValueError), (-3, ValueError), (True, TypeError), (2.0, TypeError)],
)
def test_a_confirmation_that_confirms_nothing_is_refused_out_loud(
    value: object, error: type[Exception]
) -> None:
    """Ноль свечей подтверждения — настройка, которая выглядит работающей и не работает."""
    with pytest.raises(error):
        EmaReverseSettings(confirm_bars=value)  # type: ignore[arg-type]


def test_a_muted_signal_says_in_the_journal_which_filter_muted_it() -> None:
    """Погашенный сигнал обязан назвать причину человеческим языком.

    Действие робота без строки в журнале — дефект; бездействие робота
    без строки — тот же дефект. Владелец счёта включил фильтр и обязан видеть,
    что именно он отсёк.
    """
    by_band = run([100, 101, 102, 110, 105, 90], period=3, threshold_percent=0.5)[4]
    assert by_band.intent is Intent.NONE
    assert "внутри полосы" in by_band.reason and "0,5%" in by_band.reason

    by_confirm = run(RUNS, period=3, confirm_bars=3)[3]
    assert by_confirm.intent is Intent.NONE
    assert "не подтверждён" in by_confirm.reason
    assert "1 свеча" in by_confirm.reason and "3 свечи" in by_confirm.reason


def test_a_signal_that_passed_the_filter_says_so_too() -> None:
    """Прошедший фильтр сигнал называет порог и число свечей — иначе не проверить."""
    passed = run(RUNS, period=3, threshold_percent=0.1, confirm_bars=3)[5]
    assert passed.intent is Intent.LONG
    assert "порог 0,1% пройден" in passed.reason
    assert "3 свечи подряд по эту сторону" in passed.reason


def test_the_filters_stay_silent_in_the_journal_while_they_are_off() -> None:
    """Выключенный фильтр в строке журнала не появляется вовсе.

    Иначе владелец счёта читает про фильтр, которого нет, а сравнение
    журналов до и после правки даёт расхождение на каждой свече.
    """
    line = run(RUNS, period=3)[3].reason
    assert "порог" not in line and "подряд" not in line
    assert line == "Закрытие 110 выше EMA(3) 105,5"


def test_the_filter_changes_are_written_with_the_old_and_the_new_value() -> None:
    """Смена порога и подтверждения попадает в журнал с обоими значениями (ТЗ §4.4 А)."""
    base = EmaReverseSettings()
    changed = base.replace(threshold_percent=0.05, confirm_bars=3)
    assert changed.changes_from(base) == [
        "Порог пересечения: 0% → 0,05%",
        "Подтверждение сигнала: 1 свеча → 3 свечи",
    ]
    assert base.changes_from(changed) == [
        "Порог пересечения: 0,05% → 0%",
        "Подтверждение сигнала: 3 свечи → 1 свеча",
    ]
