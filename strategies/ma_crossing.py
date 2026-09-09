"""Торговый модуль №2: сигнал в момент пересечения свечи со средней.

⚠️ **Это проверочный алгоритм, а не замена первому.** Заведён по просьбе
владельца счёта 09.09.2026 со слов брокера: «сигнал срабатывает при
пересечении свечи и скользящей, он так работает». Умолчанием остаётся
модуль №1 — прямое решение владельца счёта.

Правило целиком
---------------

::

    средняя      берётся у ПРЕДЫДУЩЕЙ свечи — та, что была посчитана
                 до подачи текущей
    пересечение  минимум <= средняя <= максимум текущей свечи
                 (тени считаются наравне с телом)
    сторона      средняя выросла  → лонг
                 средняя упала    → шорт
                 средняя не изменилась → ни входа, ни выхода

Три отличия от модуля №1, каждое меняет поведение
-------------------------------------------------
**1. Ответ событием, а не состоянием.** Модуль №1 отвечает на каждой свече:
«закрытие выше средней — хочу быть в лонге», и так пока цена по эту сторону.
Здесь намерение появляется **только на свече пересечения**, на всех
остальных — `NONE`, то есть «ни входа, ни выхода».

⚠️ Следствие, о котором обязан знать владелец счёта: **позиция держится
до следующего пересечения**, промежуточных подтверждений нет. И второе,
менее очевидное: в режиме «переворот через свечу» (умолчание) выход
подаётся на свече сигнала, а вход в обратную сторону — на следующей,
по намерению **следующей** свечи. У модуля №1 оно там обычно то же самое,
и переворот получается; здесь на следующей свече намерения нет, и
переворот вырождается в **простой выход**. Робот на этом алгоритме уходит
в деньги и ждёт следующего пересечения. Режим «в одной свече» переворот
сохраняет. Движок при этом верен себе и правки не требует: `NONE` он
и обязан читать как «ни входа, ни выхода» (PROTOTYPE.md §3).

**2. Тени участвуют.** Сравнивается не закрытие, а **размах** свечи: если
средняя лежит между минимумом и максимумом, свеча её задела — даже когда
тело целиком осталось по одну сторону. Модуль №1 максимума и минимума
не читает вовсе.

**3. Сторону задаёт наклон средней, а не место цены.** Куда пошла линия,
туда и намерение. Закрытие в выборе стороны не участвует **напрямую** —
оно участвует только через среднюю.

⚠️ Для экспоненциальной средней эти два способа совпадают:
`E(i) = E(i−1) + k·(close − E(i−1))`, поэтому «средняя выросла» — это в
точности «закрытие выше средней предыдущей свечи». Для простой средней
не совпадают: там знак задаёт разность нового закрытия и выпавшего из окна.
Реализовано **буквально по наклону** — так сказано владельцем счёта, и так
правило одинаково работает при обоих типах средней.

Две развилки, закрытые здесь явно
---------------------------------
**Какой парой меряется наклон.** Взята пара «средняя этой свечи против
средней предыдущей», то есть два последних значения линии. Довод: решение
принимается на закрытии свечи, закрытие законно известно, и это ровно тот
наклон, который человек видит глазом на графике — последний отрезок линии.
Законна и вторая пара («средняя предыдущей против средней позапрошлой»):
она не читает текущее закрытие вовсе и даёт более медленный сигнал. Её
не взяли потому, что тогда решение на свече не зависело бы от самой свечи
ничем, кроме теней.

**Средняя не изменилась.** Сигнала нет. Это тот же выбор, что шестое
исключение прототипа у модуля №1 (`close == EMA` → ни входа, ни выхода),
и здесь он **зашит**, а не вынесен в настройку: настройка модуля №1
называется «Закрытие ровно на средней» и говорит про другое событие.
Случай достижим на ровной цене — ночная сессия, остановленный инструмент.

Чего этот модуль из общих настроек не читает
--------------------------------------------
Настройки у него те же, что у модуля №1 (один класс), и **два поля из пяти
он не читает**:

* «Закрытие ровно на средней» (`on_equal`) — он закрытие со средней
  не сравнивает вовсе;
* «Подтверждение сигнала, свечей» (`confirm_bars`) — «N закрытий подряд
  по одну сторону» для события одной свечи смысла не имеет, а придумывать
  ему второе значение значило бы завести третье правило, которого никто
  не просил.

Оба названы вслух в описании правила словами — молча игнорировать поле,
которое человек крутит в окне, нельзя (правило 13 `CLAUDE.md`). Это же
и находка для задачи о раздельном хранении настроек: набор полей у двух
алгоритмов **не совпадает**, хотя класс настроек общий.

Чего в этом модуле нет и не будет
---------------------------------
Объёма, денег, комиссии, ГО, торгового окна, дней недели, тейк-профита,
режима работы, момента переворота, брокера и различения «бой или прогон
по истории». Всё это — `engine/`.
"""

from __future__ import annotations

from collections.abc import Callable
from math import isfinite
from typing import Final

from strategies.average import MovingAverage
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
from strategies.ema_reverse import EmaReverseSettings, _bars, _number, _percent

# ⚠️ Три оформителя чисел (`_number`, `_percent`, `_bars`) выше взяты у модуля
# №1, а не переписаны здесь. Копия рядом — это два оформления одной цены,
# которые разъедутся в первой же правке, и разъедутся молча: строку журнала
# читает человек, а не проверка. Общий модуль оформления для слоя был бы
# честнее импорта закрытых имён, но это перенос кода из модуля №1, а его
# в этой работе не трогают дальше необходимого (`BACKLOG.md`, `D-106`).

__all__ = ["MaCrossing", "describe"]


class MaCrossing:
    """Пересечение свечи со средней: сторона — по наклону средней.

    Модуль накапливает закрытия поданного ряда и на каждой закрытой свече
    отдаёт намерение. Он **stateful** по той же причине, что модуль №1:
    прогрев и значение средней зависят от того, сколько свечей подано
    с начала ряда, а предыстории у него нет (PROTOTYPE.md §3).

    ⚠️ Настройки — общий с модулем №1 класс (`EmaReverseSettings`), и это
    не лень: владелец счёта просил «все условия те же». Два поля из пяти
    этот модуль не читает — разбор в шапке файла.
    """

    __slots__ = ("_settings", "_closes", "_average", "_averages", "_last_bar_at")

    #: Название для окна и журнала. Слова владельца счёта 09.09.2026:
    #: «в списке назови тестовый скользящий — проверим».
    title = "Тестовый скользящий"

    def __init__(self, settings: EmaReverseSettings | None = None) -> None:
        self._settings = EmaReverseSettings() if settings is None else settings
        self._closes: list[float] = []
        self._average = MovingAverage(self._settings.period, self._settings.kind)
        self._averages: list[float | None] = []
        self._last_bar_at: object | None = None

    @property
    def settings(self) -> EmaReverseSettings:
        """Нынешние настройки алгоритма — те, по которым он и решает."""
        return self._settings

    @property
    def bars(self) -> int:
        """Сколько закрытых свечей подано с начала ряда."""
        return len(self._closes)

    @property
    def average(self) -> float | None:
        """Текущее значение средней или `None`, пока идёт затравка.

        Читается снаружи для линии на графике: рисуется то же число, по
        которому робот принял решение, а не пересчитанное заново
        (`backtest/history.py::replay`).
        """
        return self._average.value

    def reset(self) -> None:
        """Забыть ряд: прогрев начнётся заново, как на новом отрезке."""
        self._closes.clear()
        self._averages.clear()
        self._average.reset()
        self._last_bar_at = None

    def apply(self, settings: EmaReverseSettings) -> list[str]:
        """Новые настройки. Возвращает строки для журнала: прежнее → новое.

        Пересчёт средней по всей накопленной истории — как у модуля №1:
        у экспоненциальной средней прошлое входит в значение, и продолженный
        ряд не совпал бы с посчитанным с нуля.
        """
        previous = self._settings
        changes = settings.changes_from(previous)
        self._settings = settings
        if settings.period != previous.period or settings.kind is not previous.kind:
            self._average = MovingAverage(settings.period, settings.kind)
            self._averages = [self._average.push(close) for close in self._closes]
        return changes

    def _is_warm(self, bars: int) -> bool:
        """Прогрев пройден: свечей **больше** периода.

        То же условие, что у модуля №1, и оно здесь достаточно: на свече
        номер `период + 1` средняя предыдущей свечи уже существует —
        затравка появляется на свече номер `период`. Лишней свечи этому
        правилу не нужно, и объявлять её значило бы отодвинуть первое
        решение без причины.
        """
        return bars > self._settings.period

    def _repeat_or_backwards(self, bar: Bar) -> Decision | None:
        """Повтор последней свечи — штатный, ход времени назад — порча ряда.

        Разбор целиком — у модуля №1 (`ema_reverse.py`): после
        переподключения брокер присылает последнюю свечу ещё раз, принятая
        молча она входит в среднюю дважды и сдвигает прогрев.
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
                f"предыдущей {previous!r}: скорее всего смешаны свечи "
                "с часовым поясом и без него"
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
        raise ValueError(
            f"свеча {bar.closes_at!r} подана после {previous!r} — ряд обязан "
            "идти вперёд по времени. Та же свеча дважды тихо испортила бы "
            "среднюю и прогрев: список сделок стал бы другим при зелёных "
            "тестах. Новый отрезок начинается с reset()"
        )

    def on_closed_bar(self, bar: Bar) -> Decision:
        """Намерение на закрытии свечи.

        Порядок значим: средняя предыдущей свечи снимается **до** того, как
        закрытие текущей вошло в расчёт. Снять её после — значит сравнивать
        размах свечи с линией, которая эту же свечу уже учла, то есть
        с другим правилом.

        :raises TypeError: свеча не той формы — разбор в `check_bar`
            и в `_span`.
        :raises ValueError: нечисловое закрытие, нечисловые максимум или
            минимум, ход времени назад.
        """
        check_bar(bar)
        low, high = _span(bar)
        repeated = self._repeat_or_backwards(bar)
        if repeated is not None:
            return repeated
        before = self._average.value
        close = float(bar.close)
        self._closes.append(close)
        self._last_bar_at = bar.closes_at
        average = self._average.push(close)
        self._averages.append(average)

        bars = len(self._closes)
        settings = self._settings
        if not self._is_warm(bars):
            return Decision(
                intent=Intent.NONE,
                reason=(
                    f"Прогрев {settings.label}: накоплено свечей {bars} из "
                    f"{settings.period + 1} — сигналов нет"
                ),
                close=close,
                average=average,
                bars=bars,
                warmed_up=False,
                skip_bar=True,      # шаг 2 прототипа: выход из обработки свечи
            )
        if average is None or before is None:  # недостижимо: прогрев позади
            raise RuntimeError(
                f"средней нет при {bars} свечах и периоде {settings.period} — "
                "состояние модуля повреждено"
            )
        intent, reason = self._verdict(before, average, low, high)
        return Decision(
            intent=intent,
            reason=reason,
            close=close,
            average=average,
            bars=bars,
            warmed_up=True,
        )

    def _verdict(
        self, before: float, average: float, low: float, high: float
    ) -> tuple[Intent, str]:
        """Намерение и строка журнала. Сперва пересечение, потом наклон.

        Порядок ответа на «почему» выбран так, потому что событие здесь —
        пересечение: не задела средней — про наклон говорить не о чем.
        На само намерение порядок не влияет, оба условия обязательны.
        """
        label = self._settings.label
        edge = _edge(self._settings, before)
        where = (
            f"{label} {_number(before)} прошлой свечи "
            f"(минимум {_number(low)}, максимум {_number(high)})"
        )
        if not (low <= before - edge and high >= before + edge):
            return Intent.NONE, (
                f"Свеча прошла мимо {where}{_band_tail(self._settings)} — "
                "ни входа, ни выхода"
            )
        touched = f"Свеча задела {where}{_band_tail(self._settings)}"
        for word, sign, intent in _SLOPES:
            if (average - before) * sign > 0:
                return intent, (
                    f"{touched}, средняя пошла {word}: "
                    f"{_number(before)} → {_number(average)}"
                )
        return Intent.NONE, (
            f"{touched}, но средняя не изменилась — ни входа, ни выхода"
        )


def _span(bar: object) -> tuple[float, float]:
    """Минимум и максимум свечи — или отказ вслух. Молчание дороже.

    Этот модуль читает размах свечи, а `check_bar` проверяет только время
    и закрытие: ему хватает того, что читает модуль №1. Отсюда своя
    проверка, и она обязана быть **громкой**. `nan` в максимуме или
    минимуме не падает: любое сравнение с ним ложно, поэтому свеча просто
    никогда не «задевала» бы среднюю — робот молча перестал бы торговать,
    и в журнале стояло бы честное «прошла мимо» на каждой свече.
    """
    values: list[float] = []
    for name in ("low", "high"):
        if not hasattr(bar, name):
            raise TypeError(
                f"это не свеча торгового модуля: нет поля {name}. Алгоритм "
                "«Тестовый скользящий» ищет пересечение по размаху свечи, "
                "а не по закрытию, и без максимума с минимумом работать "
                "не может"
            )
        raw = getattr(bar, name)
        try:
            value = float(raw)
        except (TypeError, ValueError) as error:
            raise TypeError(f"{name} свечи не число: {raw!r}") from error
        if not isfinite(value):
            raise ValueError(
                f"{name} свечи не число: {value!r}. Сравнения с ним ложны, "
                "поэтому свеча никогда не задевала бы среднюю: робот молча "
                "перестал бы торговать, а в журнале стояло бы «прошла мимо»"
            )
        values.append(value)
    low, high = values
    if low > high:
        raise ValueError(
            f"у свечи минимум {low} больше максимума {high} — ряд повреждён"
        )
    return low, high


def _edge(settings: EmaReverseSettings, average: float) -> float:
    """Полусирина полосы вокруг средней в ценах. Ноль — полосы нет.

    ⚠️ Порог здесь значит то же, что и у модуля №1 («Порог пересечения, %»),
    но применяется к размаху свечи: свеча обязана перекрыть полосу
    **целиком** — минимум ниже нижней границы, максимум выше верхней.
    Касание края полосы пересечением не считается. При нулевом пороге
    остаётся дословное `минимум <= средняя <= максимум`.
    """
    return average * (settings.threshold_percent / 100.0)


def _band_tail(settings: EmaReverseSettings) -> str:
    """Хвост строки журнала про полосу. Пусто — полосы нет."""
    if settings.threshold_percent <= 0:
        return ""
    return f", полоса {_percent(settings.threshold_percent)}"


def _moment(value: object) -> str:
    """Время свечи для строки журнала: `19.06.2026 10:15`, если это дата."""
    try:
        return f"{value:%d.%m.%Y %H:%M}"
    except (TypeError, ValueError):
        return repr(value)


#: Наклон средней: слово для человека, знак разности и намерение.
#:
#: Таблица, а не два `if`, и по той же причине, что `_SIDES` у модуля №1:
#: то же соответствие нужно описанию правила словами. Написанное дважды,
#: оно разъехалось бы молча — в окне одно, в решениях робота другое.
#: Слово сверяется с образцом машиной: «вверх» обязано означать среднюю,
#: которая после свечи-образца выросла (`tests/test_strategies_description.py`).
_SLOPES: Final[tuple[tuple[str, float, Intent], ...]] = (
    ("вверх", 1.0, Intent.LONG),
    ("вниз", -1.0, Intent.SHORT),
)

#: Во сколько раз образец отходит от границы полосы и какую долю средней
#: берёт сверх неё. Числа те же, что у модуля №1, и по той же причине:
#: образец обязан оставаться за полосой после того, как средняя шагнёт
#: ему навстречу. Здесь запас даже больше нужного — пересечение меряется
#: по средней **предыдущей** свечи, которая от подачи образца не двигается
#: вовсе; шаг средней важен только для наклона, а у него знак от размера
#: шага не зависит.
_PROBE_REACH: Final[float] = 4.0
_PROBE_LIFT: Final[float] = 0.02


def _reach(settings: EmaReverseSettings, average: float) -> float:
    """Насколько образец отходит от средней. Одна арифметика на все образцы."""
    edge = abs(_edge(settings, average))
    return edge + max(_PROBE_REACH * edge, abs(average) * _PROBE_LIFT, _PROBE_LIFT)


def _crossing_probe(
    settings: EmaReverseSettings, sign: float
) -> Callable[[float], Sample]:
    """Свеча, которая заведомо задела среднюю и увела её в нужную сторону.

    Тело целиком по одну сторону средней, а противоположная тень её
    перекрывает: так один образец исполняет сразу два утверждения модуля —
    «тени считаются» и «сторону задаёт наклон». Свеча, у которой среднюю
    пересекает тело, дала бы то же намерение и не отличала бы этот модуль
    от модуля №1.
    """

    def probe(average: float) -> Sample:
        span = _reach(settings, average)
        return Sample(
            open=average + sign * span / 2,
            high=average + span,
            low=average - span,
            close=average + sign * span,
        )

    return probe


def _aside_probe(settings: EmaReverseSettings) -> Callable[[float], Sample]:
    """Свеча целиком выше средней: линию не задела ни телом, ни тенью."""

    def probe(average: float) -> Sample:
        span = _reach(settings, average)
        return Sample(
            open=average + 2 * span,
            high=average + 3 * span,
            low=average + span,
            close=average + 2 * span,
        )

    return probe


def _still_probe(settings: EmaReverseSettings) -> Callable[[float], Sample]:
    """Свеча задела среднюю, а закрылась ровно на ней: линия не двинулась."""

    def probe(average: float) -> Sample:
        span = _reach(settings, average)
        return Sample(
            open=average, high=average + span, low=average - span, close=average
        )

    return probe


#: Хвост, которым полоса дописывается к месту пересечения. Пусто — полосы нет.
_WITH_BAND: Final[str] = "вместе с полосой вокруг неё"


def _crossed_place(settings: EmaReverseSettings) -> str:
    """Что задела свеча — без чисел, винительный падеж («задела ЧТО»)."""
    if settings.threshold_percent > 0:
        return f"среднюю {_WITH_BAND}"
    return "среднюю"


def _aside_place(settings: EmaReverseSettings) -> str:
    """Мимо чего прошла свеча — без чисел, родительный падеж («мимо ЧЕГО»).

    Два падежа, а не один на оба утверждения: «свеча прошла мимо среднюю»
    человек читает как опечатку и перестаёт верить остальному тексту.
    Пары «слово — падеж» здесь всего две, и обе стоят рядом.
    """
    if settings.threshold_percent > 0:
        return f"средней {_WITH_BAND}"
    return "средней"


def _band_detail(settings: EmaReverseSettings) -> str:
    """То же место с нынешними числами. Падеж один: строка начинается с подписи."""
    label = f"{settings.label} прошлой свечи"
    if settings.threshold_percent > 0:
        return f"{label} вместе с полосой {_percent(settings.threshold_percent)}"
    return label


def _crossing_claims(settings: EmaReverseSettings) -> tuple[Claim, ...]:
    """Два направленных утверждения: задела и увела линию вверх или вниз."""
    return tuple(
        Claim(
            relation=(
                f"свеча задела {_crossed_place(settings)}, "
                f"и средняя пошла {word}"
            ),
            detail=f"свеча задела {_band_detail(settings)}, и средняя пошла {word}",
            intent=intent,
            probe=_crossing_probe(settings, sign),
        )
        for word, sign, intent in _SLOPES
    )


def _quiet_claims(settings: EmaReverseSettings) -> tuple[Claim, ...]:
    """Два утверждения про молчание: не задела и задела при стоящей линии.

    Первое — главное отличие от модуля №1, и потому оно объявлено
    утверждением, а не фразой: свеча может стоять высоко над средней
    и не значить ничего.
    """
    aside = "свеча прошла мимо"
    return (
        Claim(
            relation=f"{aside} {_aside_place(settings)}",
            detail=f"{aside} {_band_detail(settings)}",
            intent=Intent.NONE,
            probe=_aside_probe(settings),
        ),
        Claim(
            relation=(
                f"свеча задела {_crossed_place(settings)}, "
                "но средняя не изменилась"
            ),
            detail=(
                f"свеча задела {_band_detail(settings)}, "
                "но средняя не изменилась"
            ),
            intent=Intent.NONE,
            probe=_still_probe(settings),
        ),
    )


#: Ответы фактов. Слова из закрытого словаря проверки: она по ним выбирает
#: опыт, а слово не из словаря роняет прогон, а не проходит как «неизвестно»
#: (`tests/test_strategies_description.py`).
_PRICE_SOURCE: Final[str] = "по ценам закрытия"
_RESTART_ANSWER: Final[str] = "отсчёт начинается заново"
_REVERSAL_ANSWER: Final[str] = "выходом служит противоположный сигнал"
_OVERRIDE_ANSWER: Final[str] = "может отменить любое намерение"
_FILTER_OFF: Final[str] = "выключен"
_FILTER_ON: Final[str] = "включён"


def _price_source_fact() -> Fact:
    """По каким ценам считается сама средняя — и чем меряется пересечение.

    Два разных вопроса в одном абзаце намеренно: средняя считается только
    по закрытиям (это исполняется опытом со сдвигом цен), а пересечение
    ищется по максимуму и минимуму. Умолчать про второе нельзя — именно
    тени и отличают этот алгоритм от первого.
    """
    return Fact(
        kind=FactKind.PRICE_SOURCE,
        answer=_PRICE_SOURCE,
        text=(
            f"Сама средняя считается {_PRICE_SOURCE} — максимум и минимум "
            "свечи в неё не входят. А вот пересечение робот ищет как раз "
            "по максимуму и минимуму: свеча считается задевшей линию, если "
            "линия прошлой свечи лежит между ними. Тень засчитывается "
            "наравне с телом."
        ),
    )


def _reversal_fact() -> Fact:
    """Отдельного сигнала «выйти» нет, но ждать его можно долго."""
    return Fact(
        kind=FactKind.REVERSAL,
        answer=_REVERSAL_ANSWER,
        text=(
            "Отдельного сигнала «выйти» у этого алгоритма нет: "
            f"{_REVERSAL_ANSWER}. Но сигнал здесь — событие одной свечи, "
            "а не состояние: пока новое пересечение не случилось, робот "
            "молчит, и открытая позиция стоит как есть. Между двумя "
            "пересечениями может пройти много свечей."
        ),
    )


def _warmup_fact(settings: EmaReverseSettings) -> Fact:
    """Почему в начале отрезка решений нет и с какой свечи они возможны.

    ⚠️ Число — **ответ**, а не украшение текста: проверка подаёт свежему
    модулю ряд и смотрит, какая свеча первой пошла в обработку
    (`FactKind.FIRST_DECISION_BAR`). Оно то же, что у модуля №1, хотя
    правилу нужна ещё и средняя предыдущей свечи: линия появляется
    на свече номер `период`, поэтому на свече `период + 1` есть обе.
    """
    first = settings.period + 1
    return Fact(
        kind=FactKind.FIRST_DECISION_BAR,
        answer=str(first),
        text=(
            "В начале робот молчит. Чтобы посчитать среднюю за "
            f"{_bars(settings.period)}, ему надо сперва эти "
            f"{_bars(settings.period)} увидеть, а сравнивать размах свечи "
            "не с чем, пока линии нет. Первое решение возможно на свече "
            f"номер {first}."
        ),
    )


def _restart_fact() -> Fact:
    """Отсчёт свечей идёт от начала разбираемого отрезка, а не от начала торгов."""
    return Fact(
        kind=FactKind.RESTART,
        answer=_RESTART_ANSWER,
        text=(
            "Считаются свечи от начала того отрезка, который робот "
            f"разбирает, а не от начала торгов: поменяли глубину показа — "
            f"{_RESTART_ANSWER}, и первые свечи снова остаются без решений."
        ),
    )


def _filter_fact(settings: EmaReverseSettings) -> Fact:
    """Состояние фильтра против пилы — и прямая речь про то, что не читается.

    ⚠️ Ответ считается **только по полосе**: подтверждение сигнала этот
    алгоритм не читает вовсе, и объявить фильтр включённым из-за него
    значило бы соврать проверке, которая ставит опыт — подаёт сколь угодно
    мелкое пересечение и смотрит на ответ (`FactKind.SAW_FILTER`).
    """
    band = _percent(settings.threshold_percent)
    confirm = _bars(settings.confirm_bars)
    unread = (
        f"Подтверждение сигнала ({confirm}) этот алгоритм не читает: "
        "у него сигнал — событие одной свечи, а не несколько закрытий "
        "подряд по одну сторону."
    )
    if settings.threshold_percent <= 0:
        return Fact(
            kind=FactKind.SAW_FILTER,
            answer=_FILTER_OFF,
            text=(
                f"Фильтр против пилы {_FILTER_OFF}: полоса {band}. Задела "
                f"свеча линию хотя бы на копейку — это уже пересечение. "
                f"{unread}"
            ),
        )
    return Fact(
        kind=FactKind.SAW_FILTER,
        answer=_FILTER_ON,
        text=(
            f"Фильтр против пилы {_FILTER_ON}: полоса {band} вокруг средней. "
            "Свеча обязана перекрыть её целиком — минимумом ниже нижней "
            "границы и максимумом выше верхней; кто перекрыл меньше, тот "
            f"для робота линии не задел. {unread}"
        ),
    )


def _boundary_fact() -> Fact:
    """Общие настройки программы способны отменить любое намерение модуля."""
    return Fact(
        kind=FactKind.ENGINE_MAY_OVERRIDE,
        answer=_OVERRIDE_ANSWER,
        text=(
            "Это всё, что решает алгоритм. Войдёт ли робот на самом деле — "
            "решают общие настройки: торговое окно, режим работы, "
            "тейк-профит, объём и предохранители. Любая из них "
            f"{_OVERRIDE_ANSWER} выше."
        ),
    )


def _lead(settings: EmaReverseSettings) -> str:
    """Вступление. ⚠️ Проза — кроме слов таблицы, взятых из неё же.

    Слова «вверх» и «вниз» сюда не вписаны, а взяты из `_SLOPES`: проверка
    требует от вступления назвать все направления таблицы, а второе
    перечисление разъехалось бы с первым молча.
    """
    where = " или ".join(word for word, _sign, _intent in _SLOPES)
    return (
        "Робот смотрит на две вещи: задела ли свеча линию средней или "
        "прошла мимо неё — и куда эта линия после свечи пошла, "
        f"{where}. Средняя берётся у предыдущей свечи, а задеть линию "
        "свеча может и тенью: сравнивается весь её размах, от минимума "
        f"до максимума. Сейчас линия такая: {settings.kind.label}, период "
        f"{settings.period} свечей; дальше в тексте она обозначена "
        f"{settings.label}."
    )


#: Оговорка про поле, которого этот алгоритм не читает. ⚠️ Проза: машиной
#: не проверяется, но обязана быть — поле стоит в окне, человек его крутит,
#: и молчание об этом было бы тем самым молчаливым дефектом (правило 13).
_UNREAD_ON_EQUAL: Final[str] = (
    "Настройку «Закрытие ровно на средней» ({label}) этот алгоритм "
    "не читает: он вообще не сравнивает закрытие с линией — сторону "
    "задаёт наклон самой линии."
)


def describe(settings: EmaReverseSettings) -> Description:
    """Как модуль принимает решение — словами, с нынешними числами.

    Собирается из таблиц, а не пишется руками: разбор приёма и цена отказа
    от него — в `strategies/contracts.py`, классы `Claim` и `Fact`.
    """
    return Description(
        title=MaCrossing.title,
        lead=_lead(settings),
        claims=(*_crossing_claims(settings), *_quiet_claims(settings)),
        facts=(
            _price_source_fact(),
            _reversal_fact(),
            _warmup_fact(settings),
            _restart_fact(),
            _filter_fact(settings),
            _boundary_fact(),
        ),
        notes=(_UNREAD_ON_EQUAL.format(label=settings.on_equal.label),),
    )
