"""Торговое окно и торговый день — по времени ЗАКРЫТИЯ свечи, строго внутри.

Одно правило, из-за которого этот файл существует отдельно
-----------------------------------------------------------
::

    now = час*60 + минута   от времени ЗАКРЫТИЯ свечи
    start == end  → окно не задано, торгуем весь день
    start <  end  → now > start && now < end
    start >  end  → now > start || now < end      (окно через полночь)

Неравенства **строгие**. Свеча, закрывшаяся ровно в 10:05, в окно 10:05–11:00
**не попадает**: первая рабочая пятиминутка — закрывшаяся в 10:10. Ошибка
на один знак здесь не падает, она просто даёт другой список сделок
(PROTOTYPE.md §4, ARCHITECTURE.md §6).

Секунды отбрасываются, и это не небрежность
-------------------------------------------
`now` собирается из **часа и минуты**, секунды и микросекунды в счёт не идут —
дословно как у прототипа. Свеча, закрывшаяся в 10:05:30, даёт `now == 605`,
то есть ровно границу, и в окно 10:05–11:00 не попадает. Сравнение объектов
времени целиком дало бы `10:05:30 > 10:05:00` и пустило бы её внутрь —
это уже другой список сделок. То же и для границ: секунды в `start` и `end`
не читаются.

Почему часовой пояс объявлен здесь заново
-----------------------------------------
`engine/` не импортирует ни один слой, кроме `strategies/` (ARCHITECTURE.md §2),
а МСК живёт в `market/`. Дублирование намеренное и стоит дешевле нарушения
направления зависимостей: смещение Москвы не менялось с 26.10.2014, данных
до этой даты продукт не обрабатывает.

Наивное время (без пояса) — отказ, а не догадка. «Наверное, это уже МСК» —
самый дешёвый способ сдвинуть торговое окно на три часа и не заметить:
сделки останутся, просто станут другими.

Второе, что здесь живёт: какой день торговый
--------------------------------------------
До 05.09.2026 ответ был один — «будни да, выходные нет», и он неверен дважды:
на рабочую субботу робот не выходил, а в праздничный понедельник выходил.
Теперь ответ считается **таблицей правил** `_DAY_RULES`, и её порядок и есть
содержание решения 0038:

1. биржа сказала «не работает» — нет, и отметка «торгуем» этого не отменяет;
2. владелец счёта пометил «не торгуем» — нет;
3. владелец счёта пометил «торгуем» — да, хоть это и суббота;
4. биржа сказала «рабочий день» — да, хоть это и суббота (перенос);
5. суббота или воскресенье при выключенной торговле в выходные — нет;
6. остальное — да.

Отсюда главное свойство, которое легко потерять перестановкой двух строк:
**биржа только запрещает.** Заставить торговать может владелец счёта отметкой
или обычное правило будней, но не она. Порядок сверяется тестом с документом
(`.docs/plans/F-002-calendar-of-non-working-days.md`).

Оба набора отметок пусты по умолчанию, и это в точности прототип: на пустом
календаре стоит сверка, 127 сделок из 127.
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

__all__ = [
    "MSK",
    "DayMarks",
    "NO_MARKS",
    "DayRule",
    "DayVerdict",
    "TradingWindow",
    "minutes_of_day",
    "in_moscow",
    "is_trading_day",
    "day_verdict",
    "verdict_of_day",
]

#: Московское время: фиксированный UTC+3 (см. пояснение в шапке модуля).
MSK = timezone(timedelta(hours=3), "MSK")


def in_moscow(moment: datetime) -> datetime:
    """Момент в МСК. Наивное время не принимается.

    :raises ValueError: время без часового пояса.
    """
    if not isinstance(moment, datetime):
        raise TypeError(f"ожидается datetime, получено {moment!r}")
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError(
            f"время без часового пояса: {moment!r}. Торговое окно считается "
            "по московскому времени; наивный момент молча сдвинул бы окно "
            "на разницу поясов, и список сделок стал бы другим"
        )
    return moment.astimezone(MSK)


def minutes_of_day(value: time | datetime) -> int:
    """Минуты от полуночи: `час*60 + минута`. Секунды отбрасываются.

    Дословно `Minutes(hour, minute)` прототипа. Отбрасывание секунд —
    часть правила, а не упрощение: см. шапку модуля.
    """
    return value.hour * 60 + value.minute


#: Названия дней недели для фраз журнала. Порядок — `datetime.weekday()`.
_WEEKDAYS = (
    "понедельник", "вторник", "среда", "четверг", "пятница",
    "суббота", "воскресенье",
)


@dataclass(frozen=True, slots=True)
class DayMarks:
    """Ответы про **конкретные календарные дни**: дата → торговый день или нет.

    Одна форма на два разных источника, и это не экономия: складывать волю
    владельца счёта и факт про биржу в один набор нельзя (решение 0038),
    поэтому наборов два, а форма у них общая. Кто чей — видно на месте
    хранения (`EngineSettings.calendar` и `EngineSettings.exchange_days`),
    а не по содержимому.

    Неизменяемо и хешируемо: лежит внутри `EngineSettings`, у которого
    `frozen=True`, а значит есть и `__hash__`. Словарь внутри отнял бы его
    молча — до первого `hash(settings)`.

    ⚠️ `datetime` — подкласс `date`, и `datetime(2026, 6, 12, 10, 5)` в наборе
    **никогда** не совпадёт с `date(2026, 6, 12)`: отметка выглядела бы
    поставленной и не работала. Поэтому момент времени здесь — громкий отказ,
    а не приведение к дате: приводить пришлось бы в чьём-то поясе, а «в чьём»
    — это ровно тот вопрос, из-за которого торговое окно уезжает на три часа.
    """

    #: Пары «дата → торгуем ли», отсортированные по дате и без повторов.
    #: Нормализуются в `__post_init__`: два набора с одними и теми же днями
    #: в разном порядке обязаны быть равны, иначе `changes_from` напишет
    #: в журнал изменение, которого не было.
    items: tuple[tuple[date, bool], ...] = ()

    def __post_init__(self) -> None:
        seen: dict[date, bool] = {}
        for item in self.items:
            if not isinstance(item, tuple) or len(item) != _PAIR:
                raise TypeError(
                    f"отметка календаря — пара «дата, торгуем ли», получено {item!r}"
                )
            day, trading = item
            if isinstance(day, datetime) or not isinstance(day, date):
                raise TypeError(
                    f"день календаря — datetime.date, получено {day!r}. Момент "
                    "времени сюда не годится: он не совпадёт ни с одной датой, "
                    "и отметка молча перестанет работать"
                )
            if not isinstance(trading, bool):
                raise TypeError(
                    f"отметка дня {day:%d.%m.%Y} — «да» или «нет», "
                    f"получено {trading!r}"
                )
            if day in seen and seen[day] != trading:
                raise ValueError(
                    f"день {day:%d.%m.%Y} помечен дважды и по-разному. "
                    "Какой из двух ответов верен, программа не знает"
                )
            seen[day] = trading
        object.__setattr__(self, "items", tuple(sorted(seen.items())))

    @classmethod
    def of(cls, marks: Mapping[date, bool] | Iterable[tuple[date, bool]]) -> DayMarks:
        """Набор из словаря или из пар. Порядок и повторы приводит `__post_init__`."""
        pairs = marks.items() if isinstance(marks, Mapping) else marks
        return cls(tuple(pairs))

    def answer(self, day: date) -> bool | None:
        """Что сказано про этот день. `None` — про него не сказано ничего."""
        for marked, trading in self.items:
            if marked == day:
                return trading
        return None

    def as_dict(self) -> dict[date, bool]:
        """Копия словарём — для окна и для перекладки. Сам набор неизменяем."""
        return dict(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def __bool__(self) -> bool:
        return bool(self.items)

    @property
    def label(self) -> str:
        """Для журнала: «нет» или перечисление дней, длинное — с числом.

        Полный список из тридцати дат в строке «Настройки изменены» никто
        не прочтёт, поэтому длинный набор печатается началом и числом. Число
        обязательно: «и ещё» без него означало бы, что изменение видно
        не полностью и насколько — неизвестно.
        """
        if not self.items:
            return "нет"
        shown = [
            f"{day:%d.%m.%Y} {'торгуем' if trading else 'не торгуем'}"
            for day, trading in self.items[:_SHOWN_DAYS]
        ]
        if len(self.items) <= _SHOWN_DAYS:
            return ", ".join(shown)
        rest = len(self.items) - _SHOWN_DAYS
        return ", ".join(shown) + f" и ещё {rest} (всего {len(self.items)})"


#: Сколько дней печатается в строке журнала целиком.
_SHOWN_DAYS = 4

#: Длина пары «дата → торгуем ли». Отдельным именем этого требует линтер,
#: и он прав: голая двойка в проверке длины ничего не объясняет.
_PAIR = 2

#: Первый выходной день недели по `datetime.weekday()` — суббота.
_FIRST_WEEKEND_DAY = 5

#: Пустой набор отметок: «про дни ничего не сказано». Отдельным именем,
#: потому что значение по умолчанию у довода вычисляется один раз, и общий
#: неизменяемый набор здесь честнее нового объекта на каждый вызов.
NO_MARKS = DayMarks()


class DayRule(enum.Enum):
    """Правило, по которому решено про день. Значение — ключ строки журнала.

    Ключ попадает в `writer.quiet(f"outside-{...}")`, то есть решает, будет ли
    новая строка в журнале решений или повтор прежней. Поэтому «биржа закрыта»
    и «вы пометили день нерабочим» — разные ключи: одинаковый склеил бы два
    разных события в одно.
    """

    EXCHANGE_CLOSED = "exchange_closed"
    OWNER_OFF = "owner_off"
    OWNER_ON = "owner_on"
    EXCHANGE_OPEN = "exchange_open"
    WEEKEND = "weekend"
    ORDINARY = "ordinary"


@dataclass(frozen=True, slots=True)
class DayVerdict:
    """Торговый ли это день, по какому правилу и как сказать это человеку."""

    day: date
    trading: bool
    rule: DayRule
    why: str


@dataclass(frozen=True, slots=True)
class _Day:
    """Всё, что известно про день, — довод правил ниже."""

    day: date
    trade_in_weekend: bool
    #: Что сказал владелец счёта. `None` — ничего не говорил.
    owner: bool | None
    #: Что сказала биржа. `None` — не знаем, и это НЕ «закрыто»
    #: (`broker/schedule.py`, решение 2).
    exchange: bool | None

    @property
    def named(self) -> str:
        """«12.06.2026, пятница» — начало каждой фразы про этот день."""
        return f"{self.day:%d.%m.%Y}, {_WEEKDAYS[self.day.weekday()]}"

    @property
    def weekend(self) -> bool:
        """Суббота или воскресенье по календарю — без учёта чьих-либо отметок."""
        return self.day.weekday() >= _FIRST_WEEKEND_DAY


def _exchange_closed(day: _Day) -> str | None:
    if day.exchange is not False:
        return None
    if day.owner is True:
        # Решение 0038, следствие 3: расхождение объясняется словами.
        # Молчаливое подчинение бирже читается как поломка программы.
        return (
            f"{day.named}: биржа в этот день не работает. Вы пометили день "
            "торговым, но отметка сильнее биржи не бывает — заявку принимать "
            "некому"
        )
    return f"{day.named}: биржа в этот день не работает"


def _owner_off(day: _Day) -> str | None:
    if day.owner is not False:
        return None
    if day.exchange is True:
        return (
            f"{day.named}: вы пометили этот день нерабочим. Биржа работает, "
            "но ваше решение сильнее — робот не торгует"
        )
    return f"{day.named}: вы пометили этот день нерабочим — робот не торгует"


def _owner_on(day: _Day) -> str | None:
    if day.owner is not True:
        return None
    if day.weekend:
        return f"{day.named}: обычно выходной, но вы пометили день торговым"
    return f"{day.named}: вы пометили этот день торговым"


def _exchange_open(day: _Day) -> str | None:
    if day.exchange is not True:
        return None
    if day.weekend:
        return f"{day.named}: биржа объявила день рабочим — перенос выходного"
    return f"{day.named}: биржа объявила день рабочим"


def _weekend(day: _Day) -> str | None:
    if day.trade_in_weekend or not day.weekend:
        return None
    # ⚠️ Фраза дословно та же, что была до появления календаря: на ней стоят
    # строки журнала и сторожа, а менять текст заодно с поведением — верный
    # способ не понять потом, что именно изменилось.
    return (
        f"{day.day:%d.%m.%Y} — {_WEEKDAYS[day.day.weekday()]}, "
        "торговля в выходные выключена"
    )


def _ordinary(day: _Day) -> str | None:
    return f"{day.named} — обычный торговый день"


#: **Порядок правил — это данные, а не последовательность `if`.** Первое
#: подошедшее решает; каждое отвечает фразой или `None` («правило не про этот
#: день»). Порядок проверяется тестом по документу
#: (`.docs/plans/F-002-calendar-of-non-working-days.md`), потому что он и есть
#: содержание решения 0038: биржа запрещает сильнее всех и **только
#: запрещает**, заставить торговать она не может — заставляет либо владелец
#: счёта отметкой, либо обычное правило будней.
_DAY_RULES: tuple[tuple[DayRule, bool, Callable[[_Day], str | None]], ...] = (
    (DayRule.EXCHANGE_CLOSED, False, _exchange_closed),
    (DayRule.OWNER_OFF, False, _owner_off),
    (DayRule.OWNER_ON, True, _owner_on),
    (DayRule.EXCHANGE_OPEN, True, _exchange_open),
    (DayRule.WEEKEND, False, _weekend),
    (DayRule.ORDINARY, True, _ordinary),
)


def verdict_of_day(
    day: date,
    *,
    trade_in_weekend: bool,
    calendar: DayMarks = NO_MARKS,
    exchange: DayMarks = NO_MARKS,
) -> DayVerdict:
    """Решение про календарный день: торгуем ли, по какому правилу и почему.

    :param calendar: отметки владельца счёта — его воля, снимается им же.
    :param exchange: что сказала биржа. **Только запрещает**: «рабочий день»
        отсюда отменяет выходной по календарю, но не отменяет отметку
        владельца счёта «не торгуем» (решение 0038, следствие 4).

    Оба набора пусты по умолчанию — и это ровно поведение прототипа:
    суббота с воскресеньем при выключенной торговле в выходные, все прочие
    дни торговые. На этом умолчании стоит сверка, 127 сделок из 127.
    """
    if isinstance(day, datetime) or not isinstance(day, date):
        raise TypeError(
            f"день — datetime.date, получено {day!r}. Момент времени приводит "
            "к дате `day_verdict`, и приводит его в МСК"
        )
    context = _Day(
        day=day,
        trade_in_weekend=trade_in_weekend,
        owner=calendar.answer(day),
        exchange=exchange.answer(day),
    )
    for rule, trading, speak in _DAY_RULES:
        why = speak(context)
        if why is not None:
            return DayVerdict(day=day, trading=trading, rule=rule, why=why)
    raise AssertionError(  # pragma: no cover — последнее правило отвечает всегда
        "ни одно правило дня не подошло: последнее обязано отвечать всегда"
    )


def day_verdict(
    closes_at: datetime,
    *,
    trade_in_weekend: bool,
    calendar: DayMarks = NO_MARKS,
    exchange: DayMarks = NO_MARKS,
) -> DayVerdict:
    """То же, но от свечи. День берётся от времени ЗАКРЫТИЯ, приведённого к МСК.

    ⚠️ Приведение обязательно. Голый `.date()` дал бы день того пояса,
    в котором момент пришёл: свеча, закрывшаяся 12.06 в 00:30 МСК, в UTC
    приходится на 11.06 — и отметка календаря сработала бы на сутки раньше
    или не сработала вовсе.
    """
    return verdict_of_day(
        in_moscow(closes_at).date(),
        trade_in_weekend=trade_in_weekend,
        calendar=calendar,
        exchange=exchange,
    )


def is_trading_day(
    closes_at: datetime,
    *,
    trade_in_weekend: bool,
    calendar: DayMarks = NO_MARKS,
    exchange: DayMarks = NO_MARKS,
) -> bool:
    """Торговый ли день у свечи. День берётся от времени ЗАКРЫТИЯ, в МСК.

    Короткий ответ там, где причина не нужна; причина — у `day_verdict`,
    и считается она тем же самым набором правил. Второй реализации правила
    торгового дня в программе нет.

    Прототип умеет только «выходные или нет», и при пустых наборах отметок
    эта функция ведёт себя ровно так же (PROTOTYPE.md §4). Производственный
    календарь у него не учитывается вовсе — то есть на рабочую субботу он
    не выходит, а в праздничный понедельник выходит. Оба случая чинятся
    отметками, а не догадкой: список праздников, вкомпилированный в программу,
    устарел бы молча (решение 0038, следствие 5).
    """
    return day_verdict(
        closes_at,
        trade_in_weekend=trade_in_weekend,
        calendar=calendar,
        exchange=exchange,
    ).trading


@dataclass(frozen=True, slots=True)
class TradingWindow:
    """Часы внутри дня, когда робот может открывать позиции.

    Умолчание — из ТЗ и DOMAIN.md §4 (10:05–11:00 МСК), а не из прототипа
    (у того 09:30–11:30). Цифры ТЗ — результат подбора **на том же отрезке**,
    на котором проверялись; это подгонка, и она проговорена вслух
    (PROTOTYPE.md §1). При сверке с прототипом окно задаётся явно с обеих
    сторон, умолчания не используются.
    """

    start: time = time(10, 5)
    end: time = time(11, 0)

    def __post_init__(self) -> None:
        for name in ("start", "end"):
            value = getattr(self, name)
            if not isinstance(value, time):
                raise TypeError(
                    f"граница торгового окна `{name}` — datetime.time, "
                    f"получено {value!r}"
                )
            if value.tzinfo is not None:
                raise ValueError(
                    f"граница торгового окна `{name}` задана с часовым поясом "
                    f"({value!r}). Окно — это часы внутри московского дня, "
                    "а не момент времени"
                )

    @property
    def whole_day(self) -> bool:
        """Окно не задано: начало равно концу — торгуем весь день."""
        return minutes_of_day(self.start) == minutes_of_day(self.end)

    @property
    def over_midnight(self) -> bool:
        """Окно переходит через полночь: начало позже конца."""
        return minutes_of_day(self.start) > minutes_of_day(self.end)

    def contains(self, closes_at: datetime) -> bool:
        """Свеча, закрывшаяся в этот момент, попадает в окно.

        Три случая границ и строгие неравенства — дословно PROTOTYPE.md §4.
        """
        now = minutes_of_day(in_moscow(closes_at))
        start = minutes_of_day(self.start)
        end = minutes_of_day(self.end)
        if start == end:
            return True
        if start < end:
            return now > start and now < end
        return now > start or now < end

    @property
    def label(self) -> str:
        """Подпись для журнала: `10:05–11:00` или «весь день»."""
        if self.whole_day:
            return "весь день"
        return f"{self.start:%H:%M}–{self.end:%H:%M}"
