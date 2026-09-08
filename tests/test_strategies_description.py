"""Описание правила словами: три проверки, и первая его **исполняет**.

Владелец счёта, глядя в окно настроек, видит «Период средней» и «Порог
пересечения» — и нигде не написано, что программа с ними делает. Абзац
в окне отвечает на этот вопрос, и весь смысл работы в том, что абзац
не написан руками, а собран из таблицы утверждений самого модуля.

Отсюда три проверки, и они разного веса.

**А — описание исполняется.** Каждое утверждение таблицы прогоняется
**через сам модуль**: строится свеча, удовлетворяющая заявленному отношению,
подаётся прогретому модулю, полученное намерение сверяется с заявленным.
Это единственная из трёх, которая ловит **ложь**: мутация `close > average`
→ `close < average` в `EmaReverse._side` роняет её.

**Б — ни одно поле настроек не забыто.** Для каждого поля меняется значение,
и описание обязано измениться. ⚠️ Ловит **умолчание**, а не ложь: поле
названо — хорошо, названо неверно — проверка зелёная. Устроена по образцу
`app/convert.py::_strategy_gap()`, где так же ловится поле, заведённое завтра
и забытое в таблице.

**В — граница честности.** Описание рассказывает, **как принимается
решение**, и ничего не обещает. ⚠️ Это **дымовой извещатель, а не
доказательство**: список ловит слова, которые мы придумали, а не обещание,
которого не придумали. Оно стоит три строки и один раз уже окупится —
но принимать его за гарантию нельзя.

⚠️ **Чего эти проверки не покрывают вовсе.** Обрамляющие фразы (`lead`
и `notes`) машиной не проверяются: «средняя считается по ценам закрытия»
под форму «отношение → намерение» не подходит и остаётся на совести ревью.
И ни одна из трёх не поймает модуль, который врёт про свои настройки, —
объявляет период 15, а считает по 20.

⚠️ **Период средней 1 недостижим для утверждений «выше» и «ниже», и это
не дефект описания.** При периоде 1 средняя равна закрытию всегда — ни одно
закрытие не бывает выше своей же средней. Утверждение остаётся истинным
как импликация и становится невыполнимым; поле окна начинается с 5
(`ui/settings_dialog.py`), так что до владельца счёта случай не доходит.
"""

from __future__ import annotations

import dataclasses
import enum
from datetime import datetime, timedelta
from typing import Any

import pytest

from strategies import (
    Bar,
    Claim,
    Description,
    Intent,
    Strategy,
    StrategyEntry,
    registry,
)

#: Цена прогрева. Любая — важно, что постоянная: на ряде из одинаковых
#: закрытий средняя равна этому же числу и у экспоненциальной, и у простой,
#: значит утверждение «ровно на средней» строится точно, а не приблизительно.
WARMUP_PRICE = 285_000.0

#: Свечи пятиминутные — как в бою. Модулю важен только порядок времени.
STEP = timedelta(minutes=5)
START = datetime(2026, 6, 19, 9, 0)

#: Сколько свечей прогрева готов подать проверяющий, прежде чем сдаться.
#: Верхняя граница периода в окне — 300; тысяча оставляет запас и не даёт
#: сломанному модулю крутить прогон бесконечно.
WARMUP_LIMIT = 1000

#: Слова, которых в описании правила быть не должно.
#:
#: Каждое — обещание результата, а не рассказ о решении. «Ищет разворот
#: тренда» неправда: модуль сравнивает закрытие со средней, и всё. Замер
#: 06.09.2026 показал, чего стоят обещания — из 9 744 сочетаний чистую
#: прибыль на независимой половине года дали 112.
#:
#: ⚠️ Результатов на истории в описании тоже нет, и это не умолчание:
#: описание рассказывает **правило**, отчёт о результатах живёт отдельно
#: (`.docs/quality/FINDINGS.md`). Смешать их значит превратить объяснение
#: в рекламу или в приговор.
FORBIDDEN: tuple[str, ...] = (
    "прибыл",
    "доход",
    "зарабат",
    "выгод",
    "надёжн",
    "надежн",
    "гарант",
    "разворот тренда",
    "сигнал на покупку",
    "сигнал на продажу",
    "лучше работает",
    "рекоменд",
)


# --------------------------------------------------------------------------
# Как строятся наборы настроек, на которых гоняются все три проверки
# --------------------------------------------------------------------------


def another(value: object) -> object:
    """Другое значение того же типа. Универсально, без знания о модуле.

    Нужно двум проверкам сразу: Б берёт по одному полю и требует, чтобы
    описание изменилось, А гоняет утверждения на получившихся наборах.
    Знание «чем заменить период» здесь было бы знанием про модуль №1,
    а проверки обязаны достаться модулю №2 даром.

    ⚠️ `bool` разбирается **раньше** `int`: в Python `True` — целое, и без
    этой строки выключатель превращался бы в число 6.
    """
    if isinstance(value, bool):
        return not value
    if isinstance(value, enum.Enum):
        others = [item for item in type(value) if item is not value]
        assert others, f"у перечисления {type(value).__name__} одно значение"
        return others[0]
    if isinstance(value, int):
        return value + 5
    if isinstance(value, float):
        return value + 0.04
    raise AssertionError(
        f"проверка не знает, чем заменить значение {value!r} типа "
        f"{type(value).__name__} — допишите правило в `another`"
    )


def defaults_of(entry: StrategyEntry) -> Any:
    """Умолчания модуля как датакласс.

    Тип ответа — `Any` в одном месте и намеренно. Реестр отдаёт настройки
    как `object`: он не знает, какой класс у модуля, и `Any` в его подписи
    означал бы «проверок больше нет» у всех вызывающих сразу. Здесь же
    датакласс нужен по существу — проверки ходят по его полям, — и признание
    этого одной строкой честнее россыпи подавлений по файлу.
    """
    return entry.defaults()


def variants(entry: StrategyEntry) -> list[tuple[str, object]]:
    """Умолчания модуля и по одному набору на каждое его поле.

    Каждый набор отличается от умолчаний **ровно одним** полем: так проверка
    Б говорит, какое именно поле забыто, а не «что-то не сошлось».
    """
    defaults = defaults_of(entry)
    built: list[tuple[str, object]] = [("умолчания", defaults)]
    for field in dataclasses.fields(defaults):
        value = another(getattr(defaults, field.name))
        built.append((field.name, dataclasses.replace(defaults, **{field.name: value})))
    return built


def entry_and_variant() -> list[tuple[StrategyEntry, str, object]]:
    """Все записи реестра, помноженные на их наборы настроек."""
    return [
        (entry, name, settings)
        for entry in registry.entries()
        for name, settings in variants(entry)
    ]


CASES = entry_and_variant()
CASE_IDS = [f"{entry.id}-{name}" for entry, name, _ in CASES]


# --------------------------------------------------------------------------
# Исполнение утверждения: общий механизм проверки А
# --------------------------------------------------------------------------


def warm_up(module: Strategy) -> tuple[float, datetime]:
    """Прогреть модуль постоянной ценой. Отдаёт среднюю и время последней свечи.

    Сколько свечей нужно на прогрев, знает модуль, а не проверка: подаём,
    пока он сам не скажет `warmed_up`. Условие прогрева у модуля №1 —
    «свечей больше периода», у модуля №2 оно может быть любым.
    """
    moment = START
    for _ in range(WARMUP_LIMIT):
        moment += STEP
        decision = module.on_closed_bar(
            Bar(moment, WARMUP_PRICE, WARMUP_PRICE, WARMUP_PRICE, WARMUP_PRICE)
        )
        if decision.warmed_up and decision.average is not None:
            return float(decision.average), moment
    raise AssertionError(
        f"модуль не прогрелся за {WARMUP_LIMIT} свечей — описание исполнить нечем"
    )


def execute(entry: StrategyEntry, settings: object, claim: Claim) -> Intent:
    """Исполнить одно утверждение и вернуть намерение, которое вышло.

    Свечи строятся по `claim.probe` от **текущей** средней на каждом шаге:
    модуль включает поданное закрытие в среднюю **до** сравнения, поэтому
    образец, посчитанный один раз в начале, к последней свече мог бы уже
    не удовлетворять отношению.
    """
    module = entry.build(settings)
    average, moment = warm_up(module)
    intent = Intent.NONE
    for step in range(max(claim.bars, 1)):
        close = claim.probe(average)
        moment += STEP
        decision = module.on_closed_bar(Bar(moment, close, close, close, close))
        assert decision.average is not None, (
            f"средняя пропала на свече {step + 1} утверждения «{claim.detail}»"
        )
        average = float(decision.average)
        intent = decision.intent
    return intent


# --------------------------------------------------------------------------
# А — описание исполняется
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("entry", "name", "settings"), CASES, ids=CASE_IDS)
def test_every_claim_of_the_description_is_true_of_the_module(
    entry: StrategyEntry, name: str, settings: object
) -> None:
    """Каждое утверждение описания проверяется **самим модулем**.

    Это не «текст совпал с шаблоном», а «текст соответствует поведению».
    Пока направление живёт в тексте, соврать может текст, и поймать это
    нечем. Как только направление живёт в таблице, а текст собирается из неё,
    соврать может только таблица — а таблицу исполняет машина.

    Мутация, обязанная ронять эту проверку: `close > average + edge` →
    `close < average + edge` в `strategies/ema_reverse.py::_side`.
    """
    description = entry.description(settings)
    assert description.claims, (
        f"у модуля {entry.id} ({name}) описание без единого утверждения — "
        "исполнять нечего, и проверка была бы вакуумной"
    )
    for claim in description.claims:
        got = execute(entry, settings, claim)
        assert got is claim.intent, (
            f"описание модуля {entry.id} ({name}) врёт: обещано «{claim.detail} "
            f"— {claim.intent.in_words}», а модуль на такой свече ответил "
            f"«{got.in_words}»"
        )


def test_the_execution_check_is_not_blind() -> None:
    """Канарейка проверки А: описание с перевёрнутым намерением обязано падать.

    Без неё «исполнение утверждений» зеленело бы и на проверке, которая
    ничего не подаёт модулю. Здесь настоящему модулю подсовывается описание,
    в котором лонг и шорт поменяны местами, — и оно обязано быть поймано.
    """
    entry = registry.default_entry()
    settings = entry.defaults()
    honest = entry.description(settings)
    caught: list[str] = []
    for claim in honest.claims:
        upside_down = dataclasses.replace(claim, intent=_flipped(claim.intent))
        if execute(entry, settings, upside_down) is not upside_down.intent:
            caught.append(claim.detail)
    # Два, а не три: `NONE` переворачивать нечем, и оно остаётся собой.
    assert len(caught) == 2, (
        "перевёрнутые утверждения не пойманы: проверка А не исполняет "
        f"описание. Поймано: {caught}"
    )


def _flipped(intent: Intent) -> Intent:
    """Намерение наизнанку. `NONE` остаётся собой: переворачивать нечего."""
    return {
        Intent.LONG: Intent.SHORT,
        Intent.SHORT: Intent.LONG,
        Intent.NONE: Intent.NONE,
    }[intent]


# --------------------------------------------------------------------------
# Б — ни одно поле настроек не забыто
# --------------------------------------------------------------------------


@pytest.mark.parametrize("entry", registry.entries(), ids=lambda item: item.id)
def test_a_change_of_any_setting_changes_the_description(
    entry: StrategyEntry,
) -> None:
    """Поменяли любое поле настроек — описание обязано измениться.

    Ловит поле, заведённое завтра и забытое в описании: человек поставил бы
    порог 0,2 % и читал бы абзац, рассказывающий про поведение без порога.

    ⚠️ Ловит **умолчание, а не ложь**. Поле названо неверно — проверка
    зелёная; на это работает проверка А, и только для полей, влияющих
    на утверждения.
    """
    defaults = defaults_of(entry)
    was = entry.description(defaults).full()
    silent: list[str] = []
    for field in dataclasses.fields(defaults):
        changed = dataclasses.replace(
            defaults, **{field.name: another(getattr(defaults, field.name))}
        )
        if entry.description(changed).full() == was:
            silent.append(field.name)
    assert not silent, (
        f"описание модуля {entry.id} не заметило смены полей: {silent}. "
        "Человек поменял настройку и читает абзац про прежнее поведение"
    )


def test_the_completeness_check_is_not_blind() -> None:
    """Канарейка проверки Б: описание, не читающее настроек, обязано падать.

    Проверка «текст изменился» зеленела бы на любом описании, где хоть
    что-то зависит от настроек. Здесь описание не зависит от них вовсе,
    и все поля обязаны попасть в список забытых.
    """
    defaults = defaults_of(registry.default_entry())
    fields = [field.name for field in dataclasses.fields(defaults)]
    deaf = _entry_with(lambda _settings: Description(
        title="Глухой модуль",
        lead="Описание, которое не смотрит на настройки.",
        claims=(
            Claim(
                relation="закрытие выше средней",
                detail="закрытие выше средней",
                intent=Intent.LONG,
                probe=lambda average: average + 1.0,
            ),
        ),
    ))
    was = deaf.description(defaults).full()
    silent = [
        field
        for field in fields
        if deaf.description(
            dataclasses.replace(defaults, **{field: another(getattr(defaults, field))})
        ).full() == was
    ]
    assert silent == fields, (
        "проверка полноты не заметила описания, глухого ко всем настройкам: "
        f"поймано {silent} из {fields}"
    )


# --------------------------------------------------------------------------
# В — граница честности
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("entry", "name", "settings"), CASES, ids=CASE_IDS)
def test_the_description_promises_nothing(
    entry: StrategyEntry, name: str, settings: object
) -> None:
    """В описании нет слов-обещаний: оно рассказывает решение, а не результат.

    ⚠️ **Дымовой извещатель, а не доказательство.** Ловит слова из списка
    `FORBIDDEN`, то есть те, которые мы придумали, — а не обещание, которого
    не придумали. Принимать зелёный результат этой проверки за «описание
    честное» нельзя.
    """
    description = entry.description(settings)
    for rendering, text in (
        ("абзацами", description.full()),
        ("одной строкой", description.headline()),
        ("без чисел", description.brief()),
    ):
        lowered = text.lower()
        found = [word for word in FORBIDDEN if word in lowered]
        assert not found, (
            f"описание модуля {entry.id} ({name}, {rendering}) обещает "
            f"результат словами {found}. Модуль сравнивает закрытие "
            "со средней — и всё"
        )


def test_the_honesty_check_is_not_blind() -> None:
    """Канарейка проверки В: обещание в тексте обязано быть поймано."""
    boastful = "Надёжный сигнал на покупку: приносит доход и прибыль."
    lowered = boastful.lower()
    found = [word for word in FORBIDDEN if word in lowered]
    # Четыре обещания в одной фразе: «надёжн», «сигнал на покупку», «доход»,
    # «прибыл». Меньше — значит список дырявый в самом очевидном месте.
    assert len(found) >= 4, (
        f"список запрещённых слов не поймал явную рекламу, нашёл только {found}"
    )


# --------------------------------------------------------------------------
# Отрисовка без чисел: та, что видна в списке выбора до всякого «Применить»
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("entry", "name", "settings"), CASES, ids=CASE_IDS)
def test_the_numberless_rendering_carries_no_numbers(
    entry: StrategyEntry, name: str, settings: object
) -> None:
    """`brief()` показывается до применения настроек — чисел в нём быть не может.

    Число в этой отрисовке было бы числом из **чужого** набора: список
    выбора модуля показывается тогда, когда настройки ещё не приняты.
    """
    text = entry.description(settings).brief()
    digits = sorted({sign for sign in text if sign.isdigit()})
    assert not digits, (
        f"в отрисовке без чисел модуля {entry.id} ({name}) есть цифры "
        f"{digits}: «{text}»"
    )


@pytest.mark.parametrize("entry", registry.entries(), ids=lambda item: item.id)
def test_the_summary_of_the_registry_comes_from_the_module(
    entry: StrategyEntry,
) -> None:
    """Подпись модуля в реестре — то же самое, что говорит о себе сам модуль.

    До этой задачи `summary` была рукописной строкой рядом с записью:
    заглушка, которая расходится с кодом при первой правке модуля и молчит.
    """
    assert entry.summary == entry.description(entry.defaults()).brief()
    assert entry.title in entry.summary, (
        f"подпись модуля {entry.id} не называет его самого"
    )


@pytest.mark.parametrize(("entry", "name", "settings"), CASES, ids=CASE_IDS)
def test_the_full_rendering_names_the_module_and_its_claims(
    entry: StrategyEntry, name: str, settings: object
) -> None:
    """Полное описание несёт все утверждения таблицы и ничего не теряет.

    Проверяется связь таблицы с текстом: утверждение, выпавшее из отрисовки,
    исполняется проверкой А и при этом человеку не показывается — то есть
    правило работает, а человек о нём не знает.
    """
    description = entry.description(settings)
    text = description.full()
    for claim in description.claims:
        # Сравнение в нижнем регистре: отрисовка абзацами начинает строку
        # утверждения с прописной, а в таблице оно лежит строчным — оно же
        # встаёт в середину строки в отрисовке для журнала.
        assert claim.detail.lower() in text.lower(), (
            f"утверждение «{claim.detail}» есть в таблице модуля {entry.id} "
            f"({name}), но не показано человеку"
        )
        assert claim.intent.in_words in text, (
            f"у утверждения «{claim.detail}» не показано намерение"
        )


# --------------------------------------------------------------------------
# Подделки для канареек: запись реестра мимо самого реестра
# --------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class StandInSettings:
    """Настройки подставного модуля. Своя форма, не форма модуля №1."""

    period: int = 3


def _entry_with(describe) -> StrategyEntry:
    """Запись реестра с подставленным описанием — **локальная**, мимо таблицы.

    Настоящий реестр здесь не трогается: подделка, дописанная в общую
    таблицу, досталась бы соседним тестам (`D-078`).
    """
    return StrategyEntry(
        id="stand_in",
        title="Подставной модуль",
        settings_type=registry.default_entry().settings_type,
        factory=registry.default_entry().factory,
        fields=registry.default_entry().fields,
        describe=describe,
    )


@pytest.fixture(autouse=True)
def the_registry_is_left_alone():
    """Ни один тест этого файла не меняет состав настоящего реестра."""
    before = registry.known_ids()
    yield
    assert registry.known_ids() == before, (
        "тест дописал или убрал торговый модуль в общей таблице — "
        f"было {before}, стало {registry.known_ids()}"
    )
