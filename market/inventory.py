"""Что лежит в базе по инструменту — картина по дням, без догадок.

Зачем это существует
--------------------
Вопрос владельца счёта 05.09.2026: «почему я вижу данные только с августа,
хотя контракт живёт с июня». Ответить на него было нечем: программа
показывает график и молчит о том, что у неё есть. Разбор пошёл в сторону
«загрузчик недокачал», и на проверку этой догадки ушло время — а данные
оказались целыми.

Замер, ради которого модуль и написан (MXU6, 82 339 минуток, сверка
с ISS 05.09.2026: 74 дня, ноль расхождений сверх сегодняшнего дня):

    2025-10   медиана 7 минут в день
    2026-04   медиана 101
    2026-06   медиана 710
    2026-08   медиана 1009

Это не дыры в загрузке. Это природа срочного рынка: пока контракт дальний,
по нему почти не торгуют, и минутка в день — **полные** данные за такой день.

Чем здесь меряется «полный день»
--------------------------------
⚠️ **Не абсолютным числом минут.** Порог вроде «меньше 500 минут — значит
недокачали» на этих данных ложен для двух третей ряда и, если завести его
в правило отметки загруженных дней, обрекает программу вечно перезапрашивать
214 дней при каждом запуске.

Меряется **долей самого плотного дня ряда**. Максимум по инструменту —
единственная величина о длине сессии, которую слой данных знает **из самих
данных**: торгового календаря у `market/` нет и быть не должно (`market.gaps`).
Для MXU6 это 1011 минут. День, набравший половину от неё и больше, назван
плотным; всё, что меньше и не ноль, — редким; ноль — пустым.

Слова выбраны так, чтобы не обещать лишнего: «плотный» — это про количество
сделок, а не про то, что биржа в этот день работала полную сессию.
Ни одного вывода о торговом календаре здесь не делается.
"""

from __future__ import annotations

import collections
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from market.candles import MSK, ensure_msk
from market.storage import CandleStore

__all__ = [
    "DENSE_SHARE",
    "DayFill",
    "Inventory",
    "MonthFill",
    "take_inventory",
]

#: Какую долю самого плотного дня ряда день обязан набрать, чтобы считаться
#: плотным. Половина — граница, а не измеренная величина: у MXU6 распределение
#: разрывное (медиана месяца скачет с 188 на 710 при переходе контракта
#: в ближние), и любой порог от 0.2 до 0.8 даёт тот же ответ. Число названо
#: явно, чтобы его можно было оспорить замером, а не менять по вкусу.
DENSE_SHARE = 0.5


@dataclass(frozen=True, slots=True)
class DayFill:
    """Один день ряда: сколько минуток и что о нём знает учёт загрузки."""

    day: date
    minutes: int
    #: День отмечен загруженным: за него спрашивали и он уже закончился.
    #: Неотмеченный будет перезапрошен при следующей загрузке.
    settled: bool

    def share(self, busiest: int) -> float:
        """Доля от самого плотного дня ряда. Ноль, если ряд пуст."""
        return self.minutes / busiest if busiest else 0.0


@dataclass(frozen=True, slots=True)
class MonthFill:
    """Месяц одной строкой — чтобы год истории читался с экрана."""

    month: str
    days: int
    dense: int
    sparse: int
    empty: int
    median_minutes: int


@dataclass(frozen=True, slots=True)
class Inventory:
    """Полная опись того, что есть по инструменту."""

    symbol: str
    days: tuple[DayFill, ...]
    #: Самый плотный день ряда — мера, относительно которой считается доля.
    busiest: int
    busiest_day: date | None
    #: Дни отрезка, которые следующая загрузка перезапросит.
    to_request: tuple[date, ...]

    @property
    def minutes(self) -> int:
        """Сколько всего минуток лежит по инструменту."""
        return sum(day.minutes for day in self.days)

    @property
    def first(self) -> date | None:
        """Первый день ряда. `None` — данных нет вовсе."""
        return self.days[0].day if self.days else None

    @property
    def last(self) -> date | None:
        """Последний день ряда. `None` — данных нет вовсе."""
        return self.days[-1].day if self.days else None

    @property
    def dense(self) -> tuple[DayFill, ...]:
        """Дни, набравшие долю `DENSE_SHARE` от самого плотного."""
        edge = self.busiest * DENSE_SHARE
        return tuple(day for day in self.days if day.minutes >= edge and day.minutes)

    @property
    def sparse(self) -> tuple[DayFill, ...]:
        """Дни, где сделки были, но их мало."""
        edge = self.busiest * DENSE_SHARE
        return tuple(day for day in self.days if 0 < day.minutes < edge)

    @property
    def empty(self) -> tuple[DayFill, ...]:
        """Дни без единой сделки: выходные, праздники, контракт вне торгов."""
        return tuple(day for day in self.days if not day.minutes)

    @property
    def first_dense(self) -> date | None:
        """С какого дня ряд становится пригодным для торговли.

        Ровно тот ответ, которого не было на вопрос «почему видно только
        с августа»: до этой даты данные есть, но их единицы минут в день.
        """
        dense = self.dense
        return dense[0].day if dense else None

    def by_month(self) -> tuple[MonthFill, ...]:
        """Ряд помесячно. Год минуток иначе не поместится на экран."""
        buckets: dict[str, list[DayFill]] = collections.defaultdict(list)
        for day in self.days:
            buckets[day.day.strftime("%Y-%m")].append(day)
        edge = self.busiest * DENSE_SHARE
        rows = []
        for month, fills in sorted(buckets.items()):
            counts = sorted(fill.minutes for fill in fills)
            rows.append(
                MonthFill(
                    month=month,
                    days=len(fills),
                    dense=sum(1 for n in counts if n and n >= edge),
                    sparse=sum(1 for n in counts if 0 < n < edge),
                    empty=sum(1 for n in counts if not n),
                    median_minutes=counts[len(counts) // 2],
                )
            )
        return tuple(rows)


def take_inventory(store: CandleStore, symbol: str) -> Inventory:
    """Собрать опись по данным базы. Только чтение, ничего не меняется.

    Дни берутся **сплошным календарём** от первой минутки до последней,
    а не по тем дням, где свечи есть: день без единой минутки внутри ряда —
    это самостоятельный факт (выходной, праздник, остановка торгов), и молча
    выкидывать его из описи значит показывать историю плотнее, чем она есть.
    """
    times = store.minute_times(symbol)
    counts: collections.Counter[date] = collections.Counter(
        ensure_msk(moment).date() for moment in times
    )
    if not counts:
        return Inventory(
            symbol=symbol, days=(), busiest=0, busiest_day=None, to_request=()
        )
    first, last = min(counts), max(counts)
    settled = store.settled_days(symbol)
    span = (last - first).days + 1
    days = tuple(
        DayFill(day=day, minutes=counts.get(day, 0), settled=day in settled)
        for day in (first + timedelta(days=i) for i in range(span))
    )
    peak = max(counts.values())
    # При равенстве — **самый ранний** такой день: он отвечает на вопрос
    # «с каких пор ряд такой плотный», а последний из равных не отвечает ни
    # на что. Правило названо, потому что без него порядок задаёт `Counter`.
    busiest_day = min(day for day in counts if counts[day] == peak)
    return Inventory(
        symbol=symbol,
        days=days,
        busiest=peak,
        busiest_day=busiest_day,
        to_request=tuple(store.days_to_request(symbol, first, last)),
    )


def observed_session(store: CandleStore, symbol: str, day: date) -> int:
    """Сколько минуток лежит в базе за этот день. Ноль — ни одной.

    Отдельная функция, а не поле описи: спросить про один день дешевле,
    чем собирать весь ряд, и этим пользуется проверка загрузки.
    """
    start = datetime.combine(day, datetime.min.time(), MSK)
    return len(store.minute_times(symbol, start, start + timedelta(days=1)))
