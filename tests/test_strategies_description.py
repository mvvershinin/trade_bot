"""Описание правила словами: проверки, которые его **исполняют**.

Владелец счёта, глядя в окно настроек, видит «Период средней» и «Порог
пересечения» — и нигде не написано, что программа с ними делает. Абзац
в окне отвечает на этот вопрос, и весь смысл работы в том, что абзац
не написан руками, а собран из таблиц самого модуля.

⚠️ **Замер 09.09.2026, с которого начата эта редакция.** Механизм проверили
мутациями, и название оказалось сильнее сути: исполнялось 67 знаков из 1562
(4,3 %), вместе со случайно пришпиленной строкой — 88 (5,6 %). Молча прошли
шесть правдоподобных подмен: «средняя считается по максимумам», «первое
решение на свече 22», «система не реверсная», выключенный фильтр, названный
включённым. И главное — беззащитными были **подписи самих утверждений**:
поменять местами «выше» и «ниже», не трогая знак сравнения и намерение, —
и проверка А оставалась зелёной, потому что образец строится по знаку,
а модуль на той же свече отвечает тем же (`B-040`, `B-041`).

Отсюда нынешний состав.

**А — утверждения исполняются, вместе со словом.** Каждое утверждение
прогоняется **через сам модуль**: строится свеча, удовлетворяющая заявленному
отношению, подаётся прогретому модулю, намерение сверяется с заявленным.
Плюс три вещи, которых не было: направляющее слово сверяется с образцом
по независимому словарю, причина решения обязана назвать сторону тем же
словом, а вступление — назвать все направляющие слова таблицы.

**Г — факты о модуле исполняются.** По каким ценам считается правило,
на какой свече возможно первое решение, начинается ли отсчёт заново после
смены отрезка, включён ли фильтр против пилы, реверсная ли система. У каждого
факта есть **машинный ответ**, проверка получает свой ответ исполнением
модуля и сверяет; текст обязан этот ответ содержать.

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

⚠️ **Чего эти проверки не покрывают, названо числом, а не словом «почти
всё».** Исполняются ответы фактов и подписи утверждений; объяснения вокруг
них — проза, и в долю проверяемого она не входит. Свежий замер и способ
счёта — в отчёте `.docs/strategy/`. Прикидывать покрытие рассуждением здесь
нельзя: именно так 09.09.2026 автор назвал беззащитными 1398 знаков и не
назвал 76, а дыра оказалась в этих 76.

⚠️ Ни одна проверка не поймает модуль, который врёт про свои настройки, —
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
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import pytest

from strategies import (
    Bar,
    Claim,
    Decision,
    Description,
    Fact,
    FactKind,
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

#: Слова, которыми описание называет место закрытия, и что каждое из них
#: обещает про образец: `+1` — образец **выше** средней, `−1` — ниже,
#: `0` — на ней самой либо внутри мёртвой полосы вокруг неё.
#:
#: ⚠️ Этот словарь — **независимая** сторона проверки, и в этом весь смысл.
#: Пока слово брали из той же таблицы модуля, что и знак, подмена «выше» ↔
#: «ниже» не роняла ничего: образец строится по знаку, намерение стоит рядом,
#: модуль на той же свече отвечает тем же. В окне вставало бы «Закрытие ниже
#: EMA(15) — робот хочет быть в лонге» при зелёном прогоне (`B-040`).
#:
#: ⚠️ Слова здесь на русском, потому что проверяется **текст для человека**;
#: имена в коде по-прежнему латиницей (`CLAUDE.md` §6).
WHERE_WORDS: tuple[tuple[str, int], ...] = (
    ("выше", 1),
    ("ниже", -1),
    ("ровно на", 0),
    ("внутри полосы", 0),
)

#: Насколько мелким бывает «сколь угодно мелкое» пересечение средней в опыте
#: с фильтром против пилы. Доля от цены, а не пункты: полоса задаётся
#: процентом. 1e−6 от 285 000 — это 0,285, на четыре порядка выше шума
#: `float` и на два порядка ниже самой узкой полосы, какую можно поставить
#: в окне.
HAIR_CROSSING = 1e-6

#: Насколько двигают цену в опыте «по каким ценам считается средняя».
#: Два процента: заведомо больше любой погрешности и заведомо видно.
PRICE_NUDGE = 0.02

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


def execute(entry: StrategyEntry, settings: object, claim: Claim) -> Decision:
    """Исполнить одно утверждение и вернуть решение модуля целиком.

    Свечи строятся по `claim.probe` от **текущей** средней на каждом шаге:
    модуль включает поданное закрытие в среднюю **до** сравнения, поэтому
    образец, посчитанный один раз в начале, к последней свече мог бы уже
    не удовлетворять отношению.

    ⚠️ Возвращается решение, а не одно намерение: проверке нужна ещё
    и **причина** — строка журнала обязана называть сторону тем же словом,
    что описание. Два рукописных перечисления «выше/ниже» рядом уже стояли,
    и разошлись бы они молча (`B-040`).
    """
    module = entry.build(settings)
    average, moment = warm_up(module)
    decision: Decision | None = None
    for step in range(max(claim.bars, 1)):
        close = claim.probe(average)
        moment += STEP
        decision = module.on_closed_bar(Bar(moment, close, close, close, close))
        assert decision.average is not None, (
            f"средняя пропала на свече {step + 1} утверждения «{claim.detail}»"
        )
        average = float(decision.average)
    assert decision is not None, "утверждение не исполнено ни одной свечой"
    return decision


def says_word(text: str, word: str) -> bool:
    """Слово стоит в тексте **целиком**, а не куском другого слова.

    Без границ «ниже» нашлось бы внутри «снижение», и проверка направляющего
    слова зеленела бы на тексте, который этого слова не говорит.
    """
    return re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text) is not None


def where_sign(text: str, *, about: str) -> int:
    """Что текст обещает про место закрытия: `+1`, `−1` или `0`.

    Слово обязано быть ровно одно по смыслу: текст, не сказавший ни одного
    известного слова, ничего человеку не обещает — и подмена «ровно на» →
    «далеко от» прошла бы молча. Текст, сказавший сразу два разных, обещает
    противоположное сам себе.
    """
    found = sorted({sign for word, sign in WHERE_WORDS if says_word(text, word)})
    vocabulary = ", ".join(f"«{word}»" for word, _sign in WHERE_WORDS)
    assert found, (
        f"{about}: «{text}» не называет место закрытия ни одним известным "
        f"словом. Проверка знает {vocabulary}; если модуль говорит иначе — "
        "допишите слово в `WHERE_WORDS` вместе со знаком, а не оставляйте "
        "фразу без проверки"
    )
    assert len(found) == 1, (
        f"{about}: «{text}» называет сразу несколько мест закрытия {found} — "
        "человеку обещано взаимоисключающее"
    )
    return found[0]


def probe_sign(claim: Claim, average: float) -> int:
    """Где на самом деле лежит образец утверждения относительно средней."""
    close = claim.probe(average)
    if close > average:
        return 1
    if close < average:
        return -1
    return 0


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
        got = execute(entry, settings, claim).intent
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
        if execute(entry, settings, upside_down).intent is not upside_down.intent:
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


@pytest.mark.parametrize(("entry", "name", "settings"), CASES, ids=CASE_IDS)
def test_the_direction_word_of_every_claim_matches_its_probe(
    entry: StrategyEntry, name: str, settings: object
) -> None:
    """Слово «выше»/«ниже» сверяется с образцом, а не с самим собой.

    Это та дыра, из-за которой заведён `B-040`. Проверка А исполняет образец
    и намерение, но слово в тексте до 09.09.2026 не трогала: поменяй местами
    подписи в таблице модуля, не трогая знак сравнения, — образец тот же,
    намерение то же, прогон зелёный, а в окне, в журнале и в снимке прогона
    стоит «Закрытие **ниже** EMA(15) — робот хочет быть **в лонге**».

    Цепочка замыкается здесь: слово ↔ образец (эта проверка), образец ↔
    намерение (проверка А). Слово, образец и намерение больше не могут
    разойтись поодиночке.

    Мутации, обязанные ронять проверку: поменять местами подписи в
    `strategies/ema_reverse.py::_SIDES`; поменять слова в `_relation`;
    заменить подпись равенства «ровно на» на «далеко от».
    """
    module = entry.build(settings)
    average, _moment = warm_up(module)
    for claim in entry.description(settings).claims:
        real = probe_sign(claim, average)
        for rendering, text in (
            ("без чисел", claim.relation), ("с числами", claim.detail)
        ):
            about = f"модуль {entry.id} ({name}), утверждение {rendering}"
            assert where_sign(text, about=about) == real, (
                f"{about}: текст «{text}» говорит про одно место закрытия, "
                f"а образец утверждения лежит по другую сторону средней "
                f"({claim.probe(average)} при средней {average}). Человек "
                "читает слово, а робот сравнивает по знаку"
            )


@pytest.mark.parametrize(("entry", "name", "settings"), CASES, ids=CASE_IDS)
def test_the_reason_names_the_side_with_the_same_word_as_the_description(
    entry: StrategyEntry, name: str, settings: object
) -> None:
    """Журнал решений и описание правила называют сторону одним словом.

    ⚠️ Рядом с таблицей описания стояло **второе рукописное перечисление**
    того же соответствия — строка журнала (`_confirm`). Поправили бы слово
    в одном месте: в окне «выше», в журнале «ниже», на одной и той же свече.
    Поймать это можно было бы только сравнением руками (`B-040`).

    Мутация, обязанная ронять проверку: вернуть в `_confirm` рукописное
    `"выше" if side is Intent.LONG else "ниже"` с переставленными словами.
    """
    for claim in entry.description(settings).claims:
        decision = execute(entry, settings, claim)
        about = f"модуль {entry.id} ({name}), утверждение «{claim.detail}»"
        promised = where_sign(claim.detail, about=about)
        assert decision.reason, f"{about}: решение пришло без причины"
        assert where_sign(decision.reason, about=f"{about}, причина") == promised, (
            f"{about}: описание обещает одно место закрытия, а журнал решений "
            f"называет другое — «{decision.reason}»"
        )


@pytest.mark.parametrize(("entry", "name", "settings"), CASES, ids=CASE_IDS)
def test_the_lead_names_every_direction_the_module_knows(
    entry: StrategyEntry, name: str, settings: object
) -> None:
    """Вступление называет все направления таблицы — оба, а не одно.

    Вступление — проза, и целиком машиной оно не проверяется. Но одно
    требование к нему исполнимо и стоит дёшево: назвать те же направляющие
    слова, что стоят в таблице утверждений. Мутация 09.09.2026 сделала
    из фразы «где свеча закрылась — выше своей средней или ниже» фразу
    «закрылась ниже своей средней», и не упало ничего (`B-041`).

    ⚠️ Проверяются только направления. Слова про середину («ровно на»,
    «внутри полосы») вступление называть не обязано: оно рассказывает,
    на что робот смотрит, а не перечисляет все исходы.
    """
    description = entry.description(settings)
    module = entry.build(settings)
    average, _moment = warm_up(module)
    directions = {
        word
        for claim in description.claims
        for word, sign in WHERE_WORDS
        if sign != 0 and says_word(claim.relation, word)
        and sign == probe_sign(claim, average)
    }
    assert directions, (
        f"у модуля {entry.id} ({name}) в таблице нет ни одного направления — "
        "проверять вступление не на чем"
    )
    silent = sorted(word for word in directions if not says_word(description.lead, word))
    assert not silent, (
        f"вступление модуля {entry.id} ({name}) не называет направления "
        f"{silent}: «{description.lead}»"
    )


# --------------------------------------------------------------------------
# Г — факты о модуле исполняются
# --------------------------------------------------------------------------
#
# `Claim` отвечает на вопрос «где закрытие → что решает модуль». Факт отвечает
# на другой: **какой это модуль**. По каким ценам считается правило, на какой
# свече возможно первое решение, начинается ли отсчёт заново после смены
# отрезка, включён ли фильтр против пилы, реверсная ли система.
#
# До 09.09.2026 всё это было прозой в `notes`, и мутации прошли молча все
# до одной: «средняя считается по максимумам», «первое решение на свече 22»,
# «система не реверсная», выключенный фильтр, названный включённым. Человек
# читает такую фразу как обещание о поведении и крутит по ней настройки,
# по которым идут деньги (`B-041`).
#
# Ниже — **словари проверки**, и они независимы от модуля. Модуль объявляет
# свой ответ, проверка получает свой исполнением и сверяет. Совпасть они
# могут только если модуль сказал правду.


#: Ответ факта «по каким ценам считается правило» → какую цену свечи трогать
#: в опыте. Словарь маленький намеренно: модуль, ответивший словом не отсюда,
#: обязан уронить прогон, а не проскочить как «неизвестно».
PRICE_FIELDS: dict[str, str] = {
    "по ценам закрытия": "close",
    "по ценам открытия": "open",
    "по максимумам": "high",
    "по минимумам": "low",
}

#: Все цены свечи, которыми проверка умеет двигать.
PRICE_NAMES: tuple[str, ...] = ("open", "high", "low", "close")

#: Ответ факта «отсчёт после смены отрезка» → что обязан дать `reset()`.
RESTART_ANSWERS: dict[bool, str] = {
    True: "отсчёт начинается заново",
    False: "отсчёт продолжается с того же места",
}

#: Ответ факта «фильтр против пилы» → что обязано выйти из опыта со сколь
#: угодно мелким пересечением средней.
FILTER_ANSWERS: dict[bool, str] = {
    True: "включён",
    False: "выключен",
}

#: Ответ факта реверсности → что обязано стоять в таблице утверждений.
#:
#: ⚠️ Фразы, а не «да/нет», и это не вкус: подмена «система **не** реверсная,
#: сигнал выйти есть» оставила бы «да» на месте. Фразу же она уносит целиком,
#: и текст остаётся без ответа.
REVERSAL_ANSWERS: dict[bool, str] = {
    True: "выходом служит противоположный сигнал",
    False: "у алгоритма есть отдельный сигнал «выйти»",
}


def first_decision_bar(module: Strategy) -> int:
    """Номер свечи ряда (с единицы), которая первой пошла в обработку.

    «Пошла в обработку» — это `skip_bar` ложно, то есть шаг 2 прототипа
    не выкинул свечу целиком. Именно это число описание и называет человеку:
    «первое решение возможно на свече номер N».
    """
    moment = START
    for number in range(1, WARMUP_LIMIT + 1):
        moment += STEP
        decision = module.on_closed_bar(
            Bar(moment, WARMUP_PRICE, WARMUP_PRICE, WARMUP_PRICE, WARMUP_PRICE)
        )
        if not decision.skip_bar:
            return number
    raise AssertionError(
        f"модуль не принял ни одного решения за {WARMUP_LIMIT} свечей"
    )


def average_after_nudging(
    entry: StrategyEntry, settings: object, moved: set[str]
) -> tuple[float, float]:
    """Средняя до и после свечи, у которой сдвинуты названные цены.

    Прогрев идёт постоянной ценой, поэтому «до» — это ровно она. Свеча
    подаётся одна: две подряд смешали бы вклад двух сдвигов.
    """
    module = entry.build(settings)
    before, moment = warm_up(module)
    prices = {
        name: WARMUP_PRICE * (1 + PRICE_NUDGE) if name in moved else WARMUP_PRICE
        for name in PRICE_NAMES
    }
    decision = module.on_closed_bar(Bar(moment + STEP, **prices))
    assert decision.average is not None, "средняя пропала на подстроенной свече"
    return before, float(decision.average)


def check_price_source(entry: StrategyEntry, settings: object, fact: Fact) -> None:
    """По названной цене средняя двигается, по остальным — нет.

    Два опыта, а не один. Один проверял бы «средняя вообще шевелится»,
    и утверждение «считается по максимумам» прошло бы: закрытие сдвинули
    вместе со всем остальным. Отрицание здесь и есть содержание фразы.
    """
    field = PRICE_FIELDS.get(fact.answer)
    known = ", ".join(f"«{word}»" for word in PRICE_FIELDS)
    assert field is not None, (
        f"модуль {entry.id} говорит, что правило считается «{fact.answer}», "
        f"а проверка знает только {known}. Допишите цену в `PRICE_FIELDS` "
        "вместе с полем свечи — иначе фраза остаётся без проверки"
    )
    named_alone, moved = average_after_nudging(entry, settings, {field})
    assert abs(moved - named_alone) > WARMUP_PRICE * PRICE_NUDGE * 1e-3, (
        f"модуль {entry.id} говорит, что средняя считается {fact.answer}, "
        f"а сдвиг этой цены на {PRICE_NUDGE:.0%} её не тронул вовсе "
        f"({named_alone} → {moved})"
    )
    others = set(PRICE_NAMES) - {field}
    rest_alone, still = average_after_nudging(entry, settings, others)
    assert abs(still - rest_alone) <= WARMUP_PRICE * 1e-9, (
        f"модуль {entry.id} говорит, что средняя считается {fact.answer}, "
        f"а сдвиг остальных цен свечи ({', '.join(sorted(others))}) её "
        f"поменял ({rest_alone} → {still})"
    )


def check_first_decision_bar(
    entry: StrategyEntry, settings: object, fact: Fact
) -> None:
    """Номер первой свечи с решением — тот самый, что назван человеку."""
    got = first_decision_bar(entry.build(settings))
    assert fact.answer == str(got), (
        f"модуль {entry.id} обещает первое решение на свече номер "
        f"{fact.answer}, а принял его на свече номер {got}. Человек ждал бы "
        "меток на графике не там, где они появятся"
    )


def check_restart(entry: StrategyEntry, settings: object, fact: Fact) -> None:
    """После смены разбираемого отрезка отсчёт свечей начинается заново."""
    from_scratch = first_decision_bar(entry.build(settings))
    used = entry.build(settings)
    warm_up(used)
    used.reset()
    after_reset = first_decision_bar(used)
    restarts = after_reset == from_scratch
    assert fact.answer == RESTART_ANSWERS[restarts], (
        f"модуль {entry.id} говорит «{fact.answer}», а после смены отрезка "
        f"первое решение пришло на свече {after_reset} против {from_scratch} "
        "на свежем модуле"
    )


def check_saw_filter(entry: StrategyEntry, settings: object, fact: Fact) -> None:
    """Сколь угодно мелкое пересечение средней: сигнал или тишина.

    Опыт ровно тот, который обещан текстом при выключенном фильтре:
    «сигналом считается любое пересечение средней, каким бы мелким оно
    ни было». Включённый фильтр обязан на такой свече промолчать.
    """
    module = entry.build(settings)
    average, moment = warm_up(module)
    close = average * (1 + HAIR_CROSSING)
    decision = module.on_closed_bar(Bar(moment + STEP, close, close, close, close))
    filtered = decision.intent is Intent.NONE
    assert fact.answer == FILTER_ANSWERS[filtered], (
        f"модуль {entry.id} говорит, что фильтр против пилы {fact.answer}, "
        f"а на пересечении средней в {HAIR_CROSSING:.0e} доли цены ответил "
        f"«{decision.intent.in_words}» — это поведение фильтра "
        f"«{FILTER_ANSWERS[filtered]}»"
    )


def check_reversal(entry: StrategyEntry, settings: object, fact: Fact) -> None:
    """Оба направления достижимы — значит выходом служит обратный сигнал.

    Опирается на проверку А: каждое из этих направлений она исполнила
    на настоящем модуле. Отдельного решения «выйти» у модуля нет
    по построению — он отдаёт намерение, а вход это, выход или переворот,
    решает движок.
    """
    sides = {claim.intent for claim in entry.description(settings).claims}
    both = Intent.LONG in sides and Intent.SHORT in sides
    assert fact.answer == REVERSAL_ANSWERS[both], (
        f"модуль {entry.id} говорит «{fact.answer}», а в его таблице "
        f"утверждений намерения {sorted(item.name for item in sides)}"
    )


#: Кто исполняет какой вид факта. Таблица, а не цепочка `if`: по ней же
#: сверяется полнота — вид, никем не исполняемый, роняет прогон.
FACT_CHECKS: dict[FactKind, Callable[[StrategyEntry, object, Fact], None]] = {
    FactKind.PRICE_SOURCE: check_price_source,
    FactKind.FIRST_DECISION_BAR: check_first_decision_bar,
    FactKind.RESTART: check_restart,
    FactKind.SAW_FILTER: check_saw_filter,
    FactKind.REVERSAL: check_reversal,
}

#: Виды, которые модуль исполнить не может, — и кто исполняет их вместо него.
#:
#: ⚠️ Единственный такой вид сегодня — утверждение про общие настройки
#: программы: это про `engine/`, а слой стратегий про движок не знает
#: (ARCHITECTURE.md §2). Передача записана здесь и сверяется списком, иначе
#: «исполняется где-то там» означало бы «не исполняется нигде».
DELEGATED_FACTS: dict[FactKind, str] = {
    FactKind.ENGINE_MAY_OVERRIDE: (
        "tests/test_engine_pipeline.py::"
        "test_the_engine_cancels_an_intent_as_the_module_promises"
    ),
}

#: Заведомо неверный ответ на каждый вид — для канарейки.
WRONG_ANSWERS: dict[FactKind, str] = {
    FactKind.PRICE_SOURCE: "по максимумам",
    FactKind.FIRST_DECISION_BAR: "999",
    FactKind.RESTART: RESTART_ANSWERS[False],
    FactKind.SAW_FILTER: FILTER_ANSWERS[True],
    FactKind.REVERSAL: REVERSAL_ANSWERS[False],
}


def test_every_kind_of_fact_is_executed_by_someone() -> None:
    """Вид факта либо исполняется здесь, либо явно передан — третьего нет.

    Без этой проверки в `FactKind` можно было бы завести вид, никем
    не исполняемый, и получить графу «факт», которая ничего не значит, —
    ровно ту болезнь, ради которой класс `Fact` и заведён.
    """
    executed = set(FACT_CHECKS)
    delegated = set(DELEGATED_FACTS)
    assert not (executed & delegated), (
        f"вид факта числится и здесь, и переданным: {executed & delegated}"
    )
    missing = sorted(item.name for item in FactKind if item not in executed | delegated)
    assert not missing, (
        f"виды фактов не исполняет никто: {missing}. Допишите процедуру "
        "в `FACT_CHECKS` либо передачу в `DELEGATED_FACTS` с именем теста"
    )


@pytest.mark.parametrize(("entry", "name", "settings"), CASES, ids=CASE_IDS)
def test_every_module_declares_every_kind_of_fact(
    entry: StrategyEntry, name: str, settings: object
) -> None:
    """Модуль объявляет все виды фактов — умолчать нельзя ни об одном.

    Промолчавший модуль не «не имеет этого свойства»: он имеет его и не
    называет. Человек тогда читает описание, в котором про фильтр против
    пилы или про начало отсчёта не сказано ничего, — и достраивает сам.
    """
    declared = {fact.kind for fact in entry.description(settings).facts}
    missing = sorted(item.name for item in FactKind if item not in declared)
    assert not missing, (
        f"модуль {entry.id} ({name}) не объявил факты {missing}"
    )


@pytest.mark.parametrize(("entry", "name", "settings"), CASES, ids=CASE_IDS)
def test_the_text_of_every_fact_carries_its_answer(
    entry: StrategyEntry, name: str, settings: object
) -> None:
    """Абзац факта содержит свой машинный ответ целым словом.

    Это вторая половина замка. Первая — ответ сверяется с исполнением;
    вторая — текст обязан этот ответ говорить. Подменили текст: ответа в нём
    нет, падает здесь. Подменили ответ: он разошёлся с исполнением, падает
    в проверке ниже. Согласованно поменять оба — значит поменять поведение
    модуля, а это уже другая работа.
    """
    for fact in entry.description(settings).facts:
        assert says_word(fact.text, fact.answer), (
            f"факт {fact.kind.name} модуля {entry.id} ({name}) отвечает "
            f"«{fact.answer}», а в его тексте этого нет: «{fact.text}»"
        )


@pytest.mark.parametrize(("entry", "name", "settings"), CASES, ids=CASE_IDS)
def test_every_fact_is_true_of_the_module(
    entry: StrategyEntry, name: str, settings: object
) -> None:
    """Ответ каждого факта проверяется исполнением самого модуля.

    Мутации, обязанные ронять проверку: «средняя считается по максимумам»;
    «первое решение возможно на свече номер период + 7»; выключенный фильтр,
    названный включённым; «система не реверсная, сигнал выйти есть».
    Все четыре 09.09.2026 проходили молча.
    """
    for fact in entry.description(settings).facts:
        check = FACT_CHECKS.get(fact.kind)
        if check is None:
            assert fact.kind in DELEGATED_FACTS, (
                f"факт {fact.kind.name} не исполняет никто"
            )
            continue
        check(entry, settings, fact)


def test_the_fact_check_is_not_blind() -> None:
    """Канарейка проверки Г: подменённый ответ обязан быть пойман.

    Без неё «факты исполняются» зеленело бы и на процедуре, которая ничего
    не подаёт модулю. Здесь настоящему модулю подсовывается факт с заведомо
    неверным ответом — и каждая процедура обязана это заметить.
    """
    entry = registry.default_entry()
    settings = entry.defaults()
    missed: list[str] = []
    for fact in entry.description(settings).facts:
        check = FACT_CHECKS.get(fact.kind)
        if check is None:
            continue
        wrong = dataclasses.replace(fact, answer=WRONG_ANSWERS[fact.kind])
        assert wrong.answer != fact.answer, (
            f"заведомо неверный ответ для {fact.kind.name} совпал с настоящим"
        )
        try:
            check(entry, settings, wrong)
        except AssertionError:
            continue
        missed.append(fact.kind.name)
    assert not missed, (
        f"процедуры не заметили подменённого ответа: {missed} — эти факты "
        "числятся исполняемыми, а не исполняются"
    )


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

    ⚠️ Настройки называются явно, и это правка `B-039`. Здесь стояло свойство
    без аргумента, считавшее правило по **умолчаниям** модуля: чисел
    в отрисовке нет, но формулировки от настроек зависят — включённый порог
    превращает «закрытие выше средней» в «закрытие выше полосы вокруг
    средней». Владелец счёта, включивший фильтр против пилы, читал правило
    без фильтра.
    """
    settings = entry.defaults()
    assert entry.summary(settings) == entry.description(settings).brief()
    assert entry.title in entry.summary(settings), (
        f"подпись модуля {entry.id} не называет его самого"
    )


@pytest.mark.parametrize("entry", registry.entries(), ids=lambda item: item.id)
def test_the_summary_of_the_registry_follows_the_settings_it_is_given(
    entry: StrategyEntry,
) -> None:
    """Подпись без чисел всё равно **зависит** от настроек — и обязана.

    Довод, на котором стоял дефект: «`brief()` цифр не содержит, поэтому
    от настроек не зависит». Цифр в ней действительно нет, а формулировки
    меняются — от порога, от подтверждения сигнала и от поведения
    на равенстве. Проверка требует, чтобы хоть одно поле настроек эту
    формулировку меняло: иначе `summary(settings)` был бы честным по подписи
    и умолчанием по сути (`B-039`).
    """
    defaults = defaults_of(entry)
    was = entry.summary(defaults)
    changed = [
        field.name
        for field in dataclasses.fields(defaults)
        if entry.summary(
            dataclasses.replace(
                defaults, **{field.name: another(getattr(defaults, field.name))}
            )
        ) != was
    ]
    assert changed, (
        f"подпись модуля {entry.id} одинакова при любых настройках — значит "
        "её можно считать по умолчаниям, и `B-039` вернётся"
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
