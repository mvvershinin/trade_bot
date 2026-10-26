"""Алгоритм №2 «Реверс с постоянной позицией»: то же правило, свои настройки.

Главная проверка файла — **поток намерений совпадает с первым алгоритмом
свеча в свечу**. Она и есть доказательство того, что второй реализации
правила не завелось: сверкой с прототипом (127 сделок из 127) покрыт один
расчёт, и копия рядом разошлась бы с ним молча — на строгости сравнения,
на свече прогрева, на засеве средней.

Остальное — то, что у второго алгоритма **своё**: название, узкий класс
настроек и три прибитых значения, которых в окне у него нет.

⚠️ Равенство `close == средняя` строится **точно**, а не «почти»: у
экспоненциальной средней закрытие, равное её нынешнему значению, оставляет
значение прежним до последнего разряда (`E + k·(E − E) == E`), и равенство
выходит настоящим. У простой средней такого построения нет: закрытие
`сумма(n−1 прошлых)/(n−1)` даёт точное равенство только в двух случаях
из трёх — округление двоичной дроби. Поэтому равенство проверяется
на экспоненциальной средней, то есть на умолчании.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta

import pytest

from strategies import registry
from strategies.average import AverageKind, MovingAverage
from strategies.contracts import Bar, Decision, Intent
from strategies.ma_reverse_always import (
    DEFAULT_PERIOD,
    MaReverseAlways,
    MaReverseAlwaysSettings,
    describe,
)

#: Время первой свечи ряда и шаг. Смысла у них здесь нет никакого: модуль
#: смотрит на время только ради порядка свечей (PROTOTYPE.md §3).
START = datetime(2026, 6, 19, 9, 0)
STEP = timedelta(minutes=5)

#: Цена прогрева. Постоянная: при ней средняя равна цене до последнего
#: разряда, и первая же решённая свеча ряда оказывается свечой равенства.
WARMUP_PRICE = 100.0


def bar_at(number: int, close: float) -> Bar:
    """Свеча-ступенька без теней: правило читает только закрытие."""
    return Bar(
        closes_at=START + STEP * number,
        open=close,
        high=close,
        low=close,
        close=close,
    )


def feed(module: MaReverseAlways, closes: list[float]) -> list[Decision]:
    """Подать ряд закрытий и собрать решения по одному на свечу."""
    return [
        module.on_closed_bar(bar_at(number, close))
        for number, close in enumerate(closes)
    ]


def series_with_equality(period: int = DEFAULT_PERIOD) -> tuple[list[float], list[int]]:
    """Ряд закрытий и номера свечей, где закрытие **ровно** на средней.

    Ряд проходит через всё, что модуль умеет: прогрев, равенство до первой
    стороны, ход вверх, равенство при лонге, ход вниз, равенство при шорте
    и снова ход вверх. Значение средней считается зеркальной `MovingAverage`
    с теми же настройками — той же арифметикой, что и внутри модуля.
    """
    mirror = MovingAverage(period, AverageKind.EMA)
    closes: list[float] = []
    equal_at: list[int] = []

    def push(close: float) -> None:
        closes.append(close)
        mirror.push(close)

    def push_equal() -> None:
        value = mirror.value
        assert value is not None, "средней ещё нет — равенство строить не из чего"
        equal_at.append(len(closes))
        push(value)

    for _ in range(period):
        push(WARMUP_PRICE)
    push_equal()                       # первая решённая свеча: стороны ещё нет
    for step in range(1, 11):
        push(WARMUP_PRICE + step)      # ход вверх
    push_equal()                       # равенство при установившемся лонге
    for step in range(1, 21):
        push(WARMUP_PRICE + 10 - step)  # ход вниз
    push_equal()                       # равенство при установившемся шорте
    for step in range(1, 11):
        push(WARMUP_PRICE - 10 + step)
    return closes, equal_at


# --------------------------------------------------------------------------
# Главное: второй реализации правила нет
# --------------------------------------------------------------------------


def test_the_series_really_contains_what_the_checks_below_need() -> None:
    """Ряд для опытов даёт и обе стороны, и точное равенство. Иначе он пуст.

    Проверка не про алгоритм, а про **вход** остальных проверок: ряд,
    на котором равенство не сложилось или сторона не установилась, делает
    их вакуумными, и молча.
    """
    closes, equal_at = series_with_equality()
    decisions = feed(MaReverseAlways(), closes)
    assert {decision.intent for decision in decisions} == set(Intent), (
        "в ряде нет всех трёх намерений — опыты ниже проверяют не всё"
    )
    assert len(equal_at) == 3, f"свечей равенства {len(equal_at)}, а нужно три"
    for number in equal_at:
        decision = decisions[number]
        assert decision.average is not None
        assert decision.close == decision.average, (
            f"на свече {number + 1} закрытие {decision.close} не равно средней "
            f"{decision.average} — равенство построено неточно, и проверка "
            "равенства стала бы вакуумной"
        )


# --------------------------------------------------------------------------
# А2. Закрытие ровно на средней
# --------------------------------------------------------------------------


def test_equality_neither_creates_a_side_nor_revokes_it() -> None:
    """Закрытие ровно на средней: ни входа, ни выхода — сторона не трогается.

    Это и есть «равенство держит прежнюю сторону» из спецификации §3.3:
    `NONE` движок читает как «ни входа, ни выхода» (PROTOTYPE.md §3), то есть
    открытое не закрывается и не переворачивается, а не открытое не открывается.

    Мутация, обязанная ронять проверку: `PINNED_ON_EQUAL` → `TREAT_AS_LONG`
    или `TREAT_AS_SHORT`. Тогда равенство начало бы **создавать** сторону,
    и робот входил бы там, где документ заказчика входа не обещает.
    """
    closes, equal_at = series_with_equality()
    decisions = feed(MaReverseAlways(), closes)
    before = [decisions[number - 1].intent for number in equal_at]
    assert before == [Intent.NONE, Intent.LONG, Intent.SHORT], (
        f"ряд не дал нужных предшествующих сторон: {before}"
    )
    for number in equal_at:
        decision = decisions[number]
        assert decision.intent is Intent.NONE, (
            f"на свече равенства {number + 1} алгоритм ответил "
            f"«{decision.intent.in_words}» — равенство создало сторону"
        )
        assert not decision.skip_bar, (
            f"свеча равенства {number + 1} помечена «в обработку не идёт»: "
            "движок вышел бы до перестановки тейка и до закрытия по концу окна"
        )
        assert "ровно на" in decision.reason, (
            f"причина решения на свече равенства не называет равенство: "
            f"«{decision.reason}»"
        )


def test_the_first_decided_bar_may_be_an_equality_and_invents_no_side() -> None:
    """Стороны ещё не было — равенство её не выдумывает.

    Ряд прогревается постоянной ценой, поэтому первая же свеча, которая
    пошла в обработку, оказывается свечой равенства. Ответ обязан быть
    «ни входа, ни выхода»: выдуманная сторона означала бы вход по цене,
    ничем не подкреплённой.
    """
    decisions = feed(MaReverseAlways(), [WARMUP_PRICE] * (DEFAULT_PERIOD + 3))
    decided = [one for one in decisions if not one.skip_bar]
    assert decided, "ни одна свеча не пошла в обработку — проверять нечего"
    assert all(one.intent is Intent.NONE for one in decided), (
        "на ровном ряде алгоритм выдумал сторону: "
        f"{[one.intent.name for one in decided]}"
    )


# --------------------------------------------------------------------------
# А3. Прогрев — строка в журнал, а не молчание
# --------------------------------------------------------------------------


def test_warmup_says_why_there_are_no_signals() -> None:
    """Каждая свеча прогрева возвращает причину словами, а не пустоту.

    Причину печатает в журнал движок (`engine/pipeline.py`, шаг 2), и взять
    её ему больше неоткуда. Молчание здесь — самостоятельный дефект
    (`CLAUDE.md` №13): человек видел бы график без единой метки и не знал бы,
    почему.

    Мутация, обязанная ронять проверку: вернуть решение прогрева с пустой
    причиной либо без `skip_bar`.
    """
    period = 9
    decisions = feed(
        MaReverseAlways(MaReverseAlwaysSettings(period=period)),
        [WARMUP_PRICE] * (period + 2),
    )
    warming = decisions[:period]
    assert len(warming) == period
    for number, decision in enumerate(warming, start=1):
        assert decision.skip_bar, (
            f"свеча прогрева {number} пошла в обработку целиком — движок "
            "переставил бы тейк и закрыл бы по концу окна на прогреве"
        )
        assert not decision.warmed_up
        assert decision.reason.strip(), (
            f"свеча прогрева {number} вернулась без причины — движку нечего "
            "написать в журнал"
        )
        assert f"EMA({period})" in decision.reason, (
            f"причина прогрева не называет среднюю: «{decision.reason}»"
        )
    assert not decisions[period].skip_bar, (
        "первая свеча после прогрева обязана пойти в обработку: сигнал "
        "возможен со свечи номер период + 1"
    )


# --------------------------------------------------------------------------
# А1. Настройки узкие, три значения прибиты
# --------------------------------------------------------------------------


def test_the_settings_are_only_the_period_and_the_kind() -> None:
    """У алгоритма ровно два поля настроек — и ни одного лишнего.

    Порог пересечения, подтверждение сигнала и поведение на равенстве
    документ заказчика не знает; поля, которых алгоритм не читает, уже дали
    долг `D-107`.
    """
    names = tuple(
        field.name for field in dataclasses.fields(MaReverseAlwaysSettings)
    )
    assert names == ("period", "kind"), (
        f"поля настроек алгоритма №2 изменились: {names}"
    )


def test_a_broken_period_is_refused_by_the_narrow_settings_too() -> None:
    """Узкие настройки проверяются так же строго, как расширенные.

    Своей проверки у них нет намеренно — её делают расширенные настройки.
    Проверка на то и стоит, что «нет своей» не превратилось в «нет никакой».
    """
    with pytest.raises(ValueError, match="не меньше 1"):
        MaReverseAlwaysSettings(period=0)
    with pytest.raises(TypeError, match="целое число"):
        MaReverseAlwaysSettings(period=15.5)  # type: ignore[arg-type]


@dataclasses.dataclass(frozen=True)
class ForeignSettings:
    """Настройки чужого алгоритма: те же два поля, другой класс."""

    period: int = 20
    kind: AverageKind = AverageKind.EMA


def test_foreign_settings_are_refused_out_loud() -> None:
    """Чужие настройки на ходу — отказ, а не тихое согласие.

    Стережёт: `apply` с настройками не своего класса бросает `TypeError`
    и оставляет прежние настройки.
    """
    module = MaReverseAlways()
    before = module.settings
    with pytest.raises(TypeError, match="никто не выбирал"):
        module.apply(ForeignSettings())
    assert module.settings == before, "настройки поменялись вопреки отказу"


def test_a_new_period_is_applied_and_told_to_the_journal() -> None:
    """Смена периода на ходу: строка «было → стало» и пересчёт по истории.

    ТЗ §4.4 А — изменение пишется с прежним и новым значением; `PROTOTYPE.md`
    §3 — смена периода пересчитывает среднюю по всей накопленной истории,
    а не только вперёд.
    """
    closes, _equal_at = series_with_equality()
    module = MaReverseAlways()
    feed(module, closes)
    said = module.apply(MaReverseAlwaysSettings(period=20))
    assert said == ["Период средней: 15 → 20"], said
    fresh = MaReverseAlways(MaReverseAlwaysSettings(period=20))
    feed(fresh, closes)
    assert module.average == fresh.average, (
        "средняя после смены периода посчитана только вперёд: она не совпала "
        "с посчитанной по тому же ряду с нуля"
    )


# --------------------------------------------------------------------------
# А5. Описание правила словами
# --------------------------------------------------------------------------


def test_the_description_is_titled_by_the_module_itself() -> None:
    """Название в описании — то же, что говорит о себе модуль.

    Голое переиспользование первого алгоритма писало бы сюда «Реверс
    по скользящей средней» — при том, что в окне выбрана другая строка.
    """
    said = describe(MaReverseAlwaysSettings())
    assert said.title == MaReverseAlways.title == "Реверс с постоянной позицией"
    # Название несут обе короткие отрисовки: подпись в списке выбора
    # и строка журнала решений. В отрисовку абзацами оно не входит вовсе —
    # там его печатает окно.
    assert said.brief().startswith(said.title)
    assert said.headline().startswith(said.title)


def test_the_numberless_rendering_follows_the_kind_of_average() -> None:
    """Отрисовка без чисел меняется от типа средней — иначе она глуха.

    У этого алгоритма настроек две, и период в отрисовку без чисел не попадает
    по определению. Останься здесь родительское «средней», подпись в списке
    выбора была бы одинаковой при любых настройках — то есть её можно было бы
    считать по умолчаниям, и `B-039` вернулся бы.
    """
    exponential = describe(MaReverseAlwaysSettings(kind=AverageKind.EMA)).brief()
    simple = describe(MaReverseAlwaysSettings(kind=AverageKind.SMA)).brief()
    assert "экспоненциальной средней" in exponential
    assert "простой средней" in simple
    assert exponential != simple


def test_every_kind_of_average_has_its_words() -> None:
    """Тип средней, заведённый завтра, обязан получить слова сегодня.

    Без этой проверки новый тип уронил бы `KeyError` в списке выбора
    алгоритма — там, где человек только смотрит, а не торгует.
    """
    for kind in AverageKind:
        said = describe(MaReverseAlwaysSettings(kind=kind)).brief()
        assert "средней" in said, f"тип средней {kind.name} остался без слов"


def test_the_description_does_not_offer_settings_the_algorithm_has_not_got() -> None:
    """Описание не называет чисел порога и подтверждения: полей таких нет.

    У первого алгоритма факт про фильтр против пилы называет оба числа.
    Пересказанный дословно, он отправил бы человека искать в окне поля,
    которых у этого алгоритма нет.
    """
    said = describe(MaReverseAlwaysSettings()).full()
    assert "полоса 0%" not in said
    assert "подтверждение 1 свеча" not in said
    assert "включить его здесь нечем" in said


def test_the_description_names_the_setting_that_makes_the_difference() -> None:
    """Момент переворота назван — иначе название алгоритма нечем объяснить.

    Правило у двух алгоритмов одно; отличается настройка движка. Описание
    обязано сказать это вслух, а не оставить человека с названием, смысл
    которого не следует ниоткуда.
    """
    said = describe(MaReverseAlwaysSettings()).full()
    assert "Момент переворота" in said


# --------------------------------------------------------------------------
# А4. Запись в реестре
# --------------------------------------------------------------------------


def test_the_registry_builds_this_algorithm_by_its_own_name() -> None:
    """Запись реестра собирает именно этот алгоритм и его узкие настройки."""
    entry = registry.find("ma_reverse_always")
    assert entry.title == MaReverseAlways.title
    assert entry.settings_type is MaReverseAlwaysSettings
    built = entry.build(entry.defaults())
    assert isinstance(built, MaReverseAlways)
    assert built.title == MaReverseAlways.title


def test_the_field_table_of_this_algorithm_names_two_fields() -> None:
    """Своя таблица полей, а не общая: трёх полей первого у него нет.

    Общая таблица на два разных класса настроек означала бы три имени,
    которым у настроек ничего не соответствует, — выбранное в окне уходило
    бы в никуда.
    """
    entry = registry.find("ma_reverse_always")
    assert tuple(one.name for one in entry.fields) == ("period", "kind")
    assert tuple(one.outer for one in entry.fields) == (
        "average_period",
        "average_kind",
    )
    assert entry.settings_gap() == ()
    assert entry.stray_fields() == ()
