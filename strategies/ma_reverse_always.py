"""Торговый модуль: «Реверс с постоянной позицией». Единственный в программе.

Решение владельца счёта 05.10.2026: «Реверс по скользящей средней» убран,
этот алгоритм — единственный (`.docs/decisions/0063-only-reverse-with-constant-position.md`).
Расчёт, живший в `strategies/ema_reverse.py`, перенесён сюда с прибитыми
значениями, которые у этого алгоритма и раньше не настраивались: полосы
вокруг средней нет, подтверждения сигнала нет, закрытие ровно на средней —
ни входа, ни выхода. Что перенос сделок не сдвинул, стережёт проверка-образец
`tests/test_golden_deals.py`.

Правило
-------

::

    close  >  средняя   → лонг
    close  <  средняя   → шорт
    close ==  средняя   → ни входа, ни выхода: сторона не создаётся
                          и не отменяется, и открытое остаётся открытым

Заказанное отличие «в торговое время всегда есть позиция» слой стратегий
выговорить не может и не должен: сторона рынка, момент переворота и защита
прибыли живут в `engine/` (ARCHITECTURE.md §3). Переворот в одной свече —
умолчание общих настроек (`ui/models.py::Settings.reversal_moment`).

Чего в этом модуле нет и не будет
---------------------------------
Объёма, денег, комиссии, ГО, торгового окна, дней недели, тейк-профита,
режима работы, момента переворота, брокера и различения «бой или прогон
по истории».
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Final

from strategies.average import AverageKind, MovingAverage
from strategies.contracts import (
    Bar,
    Claim,
    Decision,
    Description,
    Fact,
    FactKind,
    Intent,
    Sample,
    check_bar,
)
from strategies.wording import bars as _bars
from strategies.wording import number as _number

__all__ = [
    "MaReverseAlwaysSettings",
    "MaReverseAlways",
    "DEFAULT_PERIOD",
    "describe",
]

#: Период средней у прототипа. В программу проверки входят ещё 9 и 20 (DOMAIN.md §4).
DEFAULT_PERIOD = 15


@dataclass(frozen=True, slots=True)
class MaReverseAlwaysSettings:
    """Настройки алгоритма: период и тип средней, и больше ничего.

    Диапазон периода 5–300 из ТЗ здесь **не** проверяется: это ограничение
    поля ввода, а не правила расчёта. Модуль отвергает только то, на чём
    расчёт теряет смысл, — период меньше единицы и нецелый.
    """

    period: int = DEFAULT_PERIOD
    kind: AverageKind = AverageKind.EMA

    def __post_init__(self) -> None:
        if isinstance(self.period, bool) or not isinstance(self.period, int):
            raise TypeError(f"период средней — целое число, получено {self.period!r}")
        if self.period < 1:
            raise ValueError(
                f"период средней должен быть не меньше 1, получено {self.period}"
            )
        if not isinstance(self.kind, AverageKind):
            raise TypeError(f"тип средней — AverageKind, получено {self.kind!r}")

    @property
    def label(self) -> str:
        """Подпись средней для журнала и графика: `EMA(15)`."""
        return f"{self.kind.short_label}({self.period})"

    def changes_from(self, previous: "MaReverseAlwaysSettings") -> list[str]:
        """Что изменилось — строками для журнала решений, с прежним и новым.

        ТЗ §4.4 А требует писать изменение настройки с обоими значениями.
        """
        lines: list[str] = []
        if previous.period != self.period:
            lines.append(f"Период средней: {previous.period} → {self.period}")
        if previous.kind is not self.kind:
            lines.append(f"Тип средней: {previous.kind.label} → {self.kind.label}")
        return lines

    def replace(self, **changes: object) -> "MaReverseAlwaysSettings":
        """Копия с изменёнными полями — проверка значений срабатывает заново."""
        return replace(self, **changes)  # type: ignore[arg-type]


class MaReverseAlways:
    """Реверс с постоянной позицией: закрытие выше средней — лонг, ниже — шорт.

    Модуль накапливает закрытия поданного ряда и на каждой закрытой свече
    отдаёт намерение. Он **stateful** по одной причине: прогрев и значение
    средней зависят от того, сколько свечей подано с начала ряда, а предыстории
    у него нет (PROTOTYPE.md §3).

    `apply()` **пересчитывает среднюю по всей накопленной истории**, а не только
    вперёд: у экспоненциальной средней прошлое входит в значение, и продолженный
    ряд не совпал бы с посчитанным с нуля.
    """

    __slots__ = ("_settings", "_closes", "_average", "_last_bar_at")

    #: Название для окна и журнала. Решение владельца счёта 14.09.2026.
    title = "Реверс с постоянной позицией"

    def __init__(self, settings: MaReverseAlwaysSettings | None = None) -> None:
        self._settings = MaReverseAlwaysSettings() if settings is None else settings
        self._own(self._settings)
        self._closes: list[float] = []
        self._average = MovingAverage(self._settings.period, self._settings.kind)
        self._last_bar_at: object | None = None

    @property
    def settings(self) -> MaReverseAlwaysSettings:
        """Настройки, по которым модуль считает сейчас."""
        return self._settings

    @property
    def bars(self) -> int:
        """Сколько закрытых свечей подано с начала ряда."""
        return len(self._closes)

    @property
    def average(self) -> float | None:
        """Текущее значение средней или `None`, пока идёт затравка."""
        return self._average.value

    def reset(self) -> None:
        """Забыть ряд: прогрев начнётся заново, как на новом отрезке."""
        self._closes.clear()
        self._average.reset()
        self._last_bar_at = None

    def _own(self, settings: object) -> MaReverseAlwaysSettings:
        """Чужие настройки — отказ вслух, а не тихое согласие."""
        if not isinstance(settings, MaReverseAlwaysSettings):
            raise TypeError(
                f"торговому модулю «{self.title}» поданы настройки "
                f"{type(settings).__name__}, а он работает с "
                f"{MaReverseAlwaysSettings.__name__}. Настройки чужого "
                "алгоритма — это торговля с параметрами, которых никто "
                "не выбирал"
            )
        return settings

    def apply(self, settings: object) -> list[str]:
        """Новые настройки. Возвращает строки для журнала: прежнее → новое.

        Пустой список означает, что ничего не изменилось. Тип довода —
        `object`, проверка значением: порт `Strategy` принимает любые
        настройки, а этот модуль — только свои.
        """
        chosen = self._own(settings)
        previous = self._settings
        changes = chosen.changes_from(previous)
        self._settings = chosen
        if chosen.period != previous.period or chosen.kind is not previous.kind:
            self._average = MovingAverage(chosen.period, chosen.kind)
            for close in self._closes:
                self._average.push(close)
        return changes

    def _repeat_or_backwards(self, bar: Bar) -> Decision | None:
        """Разбор времени свечи: повтор — штатный, ход назад — порча.

        **Повтор последней свечи — штатное поведение потока**: после
        переподключения брокер присылает её ещё раз. Принятая молча, она
        входит в среднюю дважды и сдвигает прогрев. Поэтому — решение
        с `skip_bar`. Ход времени назад — порча ряда, и она останавливает
        прогон. Наивное время принимается: модулю нужен только порядок.
        """
        previous = self._last_bar_at
        if previous is None:
            return None
        try:
            forward = bar.closes_at > previous  # type: ignore[operator]
            repeat = bar.closes_at == previous
        except TypeError as error:
            raise TypeError(
                f"время свечи {bar.closes_at!r} не сравнивается с временем "
                f"предыдущей {previous!r}: скорее всего смешаны свечи с часовым "
                "поясом и без него"
            ) from error
        if forward:
            return None
        if repeat:
            return Decision(
                intent=Intent.NONE,
                reason=(
                    f"Свеча за {_moment(previous)} подана повторно — уже "
                    "учтена, в обработку не идёт"
                ),
                close=float(bar.close),
                average=self._average.value,
                bars=len(self._closes),
                warmed_up=self._is_warm(len(self._closes)),
                skip_bar=True,
            )
        raise ValueError(_sequence_message(previous, bar.closes_at))

    def _is_warm(self, bars: int) -> bool:
        """Прогрев пройден: свечей **больше** периода.

        Дословное условие прототипа наоборот: `свечей <= периода` — сигнала нет.
        Неравенство нестрогое; строгое сдвинуло бы все сделки на свечу.
        """
        return bars > self._settings.period

    def on_closed_bar(self, bar: Bar) -> Decision:
        """Намерение на закрытии свечи.

        Закрытие попадает в среднюю **до** сравнения — сравнивается значение,
        включающее эту же свечу (PROTOTYPE.md §3). Решение с `skip_bar`
        означает «свеча в обработку не идёт»: так помечены прогрев и повтор.

        :raises TypeError: свеча не той формы — разбор в `check_bar`.
        :raises ValueError: нечисловое закрытие или ход времени назад.
        """
        check_bar(bar)
        repeated = self._repeat_or_backwards(bar)
        if repeated is not None:
            return repeated
        close = float(bar.close)
        self._closes.append(close)
        self._last_bar_at = bar.closes_at
        average = self._average.push(close)

        bars = len(self._closes)
        settings = self._settings
        label = settings.label

        if not self._is_warm(bars):
            return Decision(
                intent=Intent.NONE,
                reason=(
                    f"Прогрев {label}: накоплено свечей {bars} из "
                    f"{settings.period + 1} — сигналов нет"
                ),
                close=close,
                average=average,
                bars=bars,
                warmed_up=False,
                skip_bar=True,      # шаг 2 прототипа: выход из обработки свечи
            )

        if average is None:  # недостижимо: свечей больше периода ⇒ затравка была
            raise RuntimeError(
                f"средней нет при {bars} свечах и периоде {settings.period} — "
                "состояние модуля повреждено"
            )

        intent = _side(close, average)
        if intent is Intent.NONE:
            reason = f"Закрытие {_number(close)} ровно на {label} — {_ON_EQUAL_TAIL}"
        else:
            reason = (
                f"Закрытие {_number(close)} {_WHERE[intent]} {label} "
                f"{_number(average)}"
            )
        return Decision(
            intent=intent,
            reason=reason,
            close=close,
            average=average,
            bars=bars,
            warmed_up=True,
        )


def _side(close: float, average: float) -> Intent:
    """Дословные неравенства прототипа: `close > средняя` / `close < средняя`."""
    if close > average:
        return Intent.LONG
    if close < average:
        return Intent.SHORT
    return Intent.NONE


def _moment(value: object) -> str:
    """Время свечи для строки журнала: `19.06.2026 10:15`, если это дата."""
    try:
        return f"{value:%d.%m.%Y %H:%M}"
    except (TypeError, ValueError):
        return repr(value)


def _sequence_message(previous: object, current: object) -> str:
    return (
        f"свеча {current!r} подана после {previous!r} — ряд обязан идти вперёд "
        "по времени. Та же свеча дважды тихо испортила бы среднюю и прогрев: "
        "список сделок стал бы другим при зелёных тестах. Новый отрезок "
        "начинается с reset()"
    )


#: Хвост строки журнала на закрытии ровно на средней.
_ON_EQUAL_TAIL: Final[str] = "ни входа, ни выхода, как у прототипа"

#: Стороны: подпись, знак и намерение. Отсюда берут подпись **оба** читателя —
#: описание правила и строка журнала решений (`B-040`).
_SIDES: Final[tuple[tuple[str, float, Intent], ...]] = (
    ("выше", 1.0, Intent.LONG),
    ("ниже", -1.0, Intent.SHORT),
)

#: Намерение → подпись стороны. Собирается из `_SIDES`, а не пишется рядом.
_WHERE: Final[dict[Intent, str]] = {
    intent: where for where, _sign, intent in _SIDES
}

#: Какую долю средней закрытие-образец отходит от неё. Образец сперва входит
#: в среднюю, и средняя шагает навстречу — не больше двух третей расстояния
#: при периоде ≥ 2; два процента средней это закрывают.
_PROBE_LIFT: Final[float] = 0.02

#: Как средняя называется словами — для отрисовки **без чисел** (`B-039`):
#: подпись в списке обязана зависеть от настроек.
_KIND_IN_WORDS: Final[dict[AverageKind, str]] = {
    AverageKind.EMA: "экспоненциальной средней",
    AverageKind.SMA: "простой средней",
}


def _probe(sign: float) -> Callable[[float], Sample]:
    """Свеча, которая заведомо по нужную сторону средней. По значению средней.

    Образец **без теней** (`Sample.flat`): максимум и минимум свечи в правило
    модуля не входят вовсе.
    """

    def probe(average: float) -> Sample:
        beyond = max(abs(average) * _PROBE_LIFT, _PROBE_LIFT)
        return Sample.flat(average + sign * beyond)

    return probe


# ---------------------------------------------------------------------------
# Факты о самом модуле: ответ, который проверка получает исполнением (`B-041`)
# ---------------------------------------------------------------------------

_PRICE_SOURCE: Final[str] = "по ценам закрытия"
_RESTART_ANSWER: Final[str] = "отсчёт начинается заново"
_REVERSAL_ANSWER: Final[str] = "выходом служит противоположный сигнал"
_OVERRIDE_ANSWER: Final[str] = "может отменить любое намерение"
_FILTER_OFF: Final[str] = "выключен"


def _price_source_fact() -> Fact:
    """По каким ценам свечи считается средняя (`FactKind.PRICE_SOURCE`)."""
    return Fact(
        kind=FactKind.PRICE_SOURCE,
        answer=_PRICE_SOURCE,
        text=(
            f"Средняя считается {_PRICE_SOURCE}: максимум и минимум свечи "
            "в неё не входят вовсе. Сдвинулось закрытие — сдвинется и средняя; "
            "длинная тень сама по себе не меняет ничего."
        ),
    )


def _reversal_fact() -> Fact:
    """Отдельного сигнала «выйти» нет — выходом служит противоположный."""
    return Fact(
        kind=FactKind.REVERSAL,
        answer=_REVERSAL_ANSWER,
        text=(
            "Отдельного сигнала «выйти» у этого алгоритма нет: система "
            f"реверсная, {_REVERSAL_ANSWER} — поэтому робот всё время держит "
            "сторону рынка, а роль стоп-лосса играет переворот."
        ),
    )


def _warmup_fact(settings: MaReverseAlwaysSettings) -> Fact:
    """Почему в начале отрезка решений нет. Номер свечи — ответ, а не текст."""
    first = settings.period + 1
    return Fact(
        kind=FactKind.FIRST_DECISION_BAR,
        answer=str(first),
        text=(
            f"В начале робот молчит. Чтобы посчитать среднюю за "
            f"{_bars(settings.period)}, ему надо сперва эти "
            f"{_bars(settings.period)} увидеть; пока их меньше, сравнивать "
            "закрытие не с чем — ни входа, ни выхода не будет вовсе, что бы "
            f"ни творилось на графике. Первое решение возможно на свече "
            f"номер {first}."
        ),
    )


def _restart_fact() -> Fact:
    """Отсчёт свечей идёт от начала разбираемого отрезка (`FactKind.RESTART`)."""
    return Fact(
        kind=FactKind.RESTART,
        answer=_RESTART_ANSWER,
        text=(
            "Считаются свечи от начала того отрезка, который робот разбирает, "
            f"а не от начала торгов: поменяли глубину показа — "
            f"{_RESTART_ANSWER}, и первые свечи снова остаются без решений."
        ),
    )


def _filter_fact() -> Fact:
    """Фильтра против пилы нет (`FactKind.SAW_FILTER`): ответ «выключен»."""
    return Fact(
        kind=FactKind.SAW_FILTER,
        answer=_FILTER_OFF,
        text=(
            f"Фильтр против пилы {_FILTER_OFF}, и включить его здесь нечем: "
            "ни полосы вокруг средней, ни подтверждения сигнала у этого "
            "алгоритма нет вовсе. Сигналом считается любое пересечение "
            "средней, каким бы мелким оно ни было."
        ),
    )


def _boundary_fact() -> Fact:
    """Общие настройки программы способны отменить любое намерение модуля.

    Исполняется проверкой движка: слой стратегий про движок не знает.
    """
    return Fact(
        kind=FactKind.ENGINE_MAY_OVERRIDE,
        answer=_OVERRIDE_ANSWER,
        text=(
            "Это всё, что решает алгоритм. Войдёт ли робот на самом деле — "
            "решают общие настройки: торговое окно, режим работы, тейк-профит, "
            f"объём и предохранители. Любая из них {_OVERRIDE_ANSWER} выше. "
            "Момент переворота — тоже общая настройка, и она здесь главная: "
            "именно она решает, подаются ли выход и обратный вход подряд или "
            "робот ждёт следующую свечу."
        ),
    )


def _lead(settings: MaReverseAlwaysSettings) -> str:
    """Вступление. Проза — кроме направляющих слов, взятых из `_SIDES`."""
    where = " или ".join(word for word, _sign, _intent in _SIDES)
    return (
        f"Робот смотрит на одно: где свеча закрылась — {where} своей "
        "скользящей средней. Скользящая средняя — это сглаженная цена: "
        "на каждой новой свече она считается заново и тянется за рынком, "
        "но медленнее его, а чем больше период, тем спокойнее линия. "
        f"Сейчас она такая: {settings.kind.label}, период "
        f"{settings.period} свечей; дальше в тексте она обозначена "
        f"{settings.label}."
    )


def describe(settings: MaReverseAlwaysSettings) -> Description:
    """Как модуль принимает решение — словами, с нынешними числами.

    Собирается из таблицы утверждений и таблицы фактов, а не пишется руками:
    утверждения исполняются свечой-образцом, факты — своим опытом
    (`tests/test_strategies_description.py`).
    """
    kind = _KIND_IN_WORDS[settings.kind]
    claims = tuple(
        Claim(
            relation=f"закрытие {where} {kind}",
            detail=f"закрытие {where} {settings.label}",
            intent=intent,
            probe=_probe(sign),
            bars=1,
        )
        for where, sign, intent in _SIDES
    )
    middle = Claim(
        relation=f"закрытие ровно на {kind}",
        detail=f"закрытие ровно на {settings.label}",
        intent=Intent.NONE,
        probe=Sample.flat,
    )
    return Description(
        title=MaReverseAlways.title,
        lead=_lead(settings),
        claims=(*claims, middle),
        facts=(
            _price_source_fact(),
            _reversal_fact(),
            _warmup_fact(settings),
            _restart_fact(),
            _filter_fact(),
            _boundary_fact(),
        ),
    )
