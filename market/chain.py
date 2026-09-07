"""Цепочка ближних контрактов: где режется стык и как из неё собирается ряд.

Зачем это существует
--------------------
Внутридневная история одного фьючерса — это жизнь одного контракта. У MXU6
это 71 торговый день, и на такой длине перебор настроек не отличает варианты
друг от друга: поправка Шарпа 0,00 / 0,09 / 0,10, стандартная ошибка итога
±5 968 ₽ больше любого числа в таблице (решение 0048). Владельцу счёта
показывают таблицу, про которую честно сказать нечего.

Контракты при этом сменяют друг друга непрерывно: пока один доживает, следующий
уже торгуется. Замер 05.09.2026 по дневным свечам биржи (объём больше 10 000
контрактов): MXU5 04.06–18.09.2025, MXZ5 10.09–18.12.2025, MXH6 03.12.2025–
19.03.2026, MXM6 10.03–18.06.2026, MXU6 08.06–07.09.2026. Пятнадцать месяцев
без единой дыры. Здесь эта цепочка превращается в один ряд.

⚠️ Собранный ряд — **не инструмент биржи**. Заявку по нему подать нельзя,
и два сторожа этого не дают: `market.synthetic`.

Где режется стык
----------------
**Рубеж — первый торговый день нового контракта после последнего дня, в который
старый ещё торговался активнее нового.** Объём в контрактах, по дневным свечам.

Правило выведено замером, и в нём намеренно нет ни одного порога.

* «за N дней до экспирации» — это календарь, а не данные. Замер 05.09.2026:
  в декабре 2025 MXZ5 упал с 20 215 минут в месяц до 12 589, а MXH6 вырос
  с 8 041 до 18 724 — перелом сидит внутри месяца, и в каждой цепочке он свой;
* наивное «новый обогнал старого» срабатывает **за восемь месяцев** до
  экспирации: 14.01.2025 у MXU5 было 2 контракта за день, у MXZ5 — 3.
  Отсюда «последний день, когда старый был впереди», а не «первый, когда его
  обогнали»: одиночный ранний обгон рубежа не создаёт, потому что после него
  старый снова впереди — и не один день, а восемь месяцев;
* порог доли («новый набрал 30 %») пришлось бы выбирать, а выбранное число
  потом защищать. Здесь выбирать нечего: сравниваются два объёма.

Замер по дням, а не по месяцам — и это не придирка. У контракта, который
экспирируется 18-го числа, в этом месяце 14 торговых дней против 22 у соседа:
месячный итог сравнивает разные знаменатели и показывает перелом там, где его
ещё нет. Сентябрь 2025: помесячно MXU5 13 125 против MXZ5 18 727 (будто перелом
уже был), по дням MXU5 впереди вплоть до самой экспирации.

Что получилось на цепочке MXM5 → MXU5 → MXZ5 → MXH6 → MXM6 → MXU6
(замер 05.09.2026, дневные свечи ISS): рубежи попали ровно на дни экспирации
уходящего контракта, и в каждом из четырёх стыков новый контракт в этот день
даёт от 63 до 73 % общего объёма пары.

Первый контракт цепочки в ряд **не входит**. Он нужен, чтобы датировать начало
второго тем же замером, а не догадкой: иначе ряд начинался бы там, где кому-то
показалось, что контракт «уже ликвиден».

Ценовой разрыв на стыке
-----------------------
Соседние контракты стоят по-разному, и разрыв здесь **меряется, а не правится**:
`Seam.gap` — разница цен двух контрактов **в одну и ту же минуту**, последнюю
общую перед рубежом. Не «первая цена нового минус последняя цена старого»:
та величина смешала бы разницу контрактов с ночным ходом рынка.

**Цены не подгоняются ни на один пункт** (ни разностью, ни отношением).
Довод — соразмерность: обратная подгонка чинит локальный дефект (четыре дня
из трёхсот сорока пяти) глобальной ценой — она трогает **все** дни и делает
каждую цену ряда числом, по которому никто не торговал. Разбор с числами —
решение 0049.

Что здесь НЕ делается
---------------------
* **Не решается, что делать с позицией через экспирацию в бою.** Ряд нужен
  для проверки на истории; перенос — открытый вопрос №8.
* **Не собираются таймфреймы.** В цель кладутся минутки, как везде
  (`market.storage`), а бары строятся на чтении.
* **Не трогается рабочая база.** Источник открывается тем же `CandleStore`,
  цель — отдельный файл; сторож `market.synthetic` не даст собранному ряду
  лечь в `candles.sqlite3`.
"""

from __future__ import annotations

import collections
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from market.candles import MSK, Candle
from market.reports import LoadReport
from market.storage import CandleStore, Source
from market.synthetic import is_synthetic

#: Короче этого цепочка не имеет смысла: первый контракт в ряд не входит,
#: он только датирует начало второго тем же замером, что и все остальные
#: рубежи. Число названо, чтобы «2» в проверке не читалось как размер пары.
SHORTEST_CHAIN = 2

__all__ = [
    "SHORTEST_CHAIN",
    "Leg",
    "Seam",
    "StitchReport",
    "daily_volume",
    "roll_day",
    "legs_of_chain",
    "measure_seam",
    "stitch",
]


@dataclass(frozen=True, slots=True)
class Leg:
    """Отрезок ряда, занятый одним контрактом. Обе границы включительно."""

    symbol: str
    since: date
    until: date

    def __post_init__(self) -> None:
        if self.until < self.since:
            raise ValueError(
                f"отрезок {self.symbol} кончается раньше, чем начинается: "
                f"{self.since} … {self.until}"
            )

    @property
    def days(self) -> int:
        """Сколько календарных дней занимает отрезок."""
        return (self.until - self.since).days + 1


@dataclass(frozen=True, slots=True)
class Seam:
    """Стык двух контрактов: где режется и насколько они разошлись в цене.

    Цены взяты **в одну и ту же минуту** — последнюю, в которую торговались
    оба. Иначе в разрыв попал бы ночной ход рынка, и число говорило бы
    не о том, о чём его спрашивают.
    """

    day: date
    leaving: str
    arriving: str
    #: Минута, на которой мерился разрыв. `None` — общей минуты не нашлось,
    #: и тогда о разрыве сказать нечего: это не «разрыва нет».
    at: datetime | None
    leaving_price: float | None
    arriving_price: float | None

    @property
    def gap(self) -> float | None:
        """Насколько новый контракт дороже уходящего, в пунктах."""
        if self.arriving_price is None or self.leaving_price is None:
            return None
        return self.arriving_price - self.leaving_price

    @property
    def gap_percent(self) -> float | None:
        """То же в процентах от цены уходящего контракта."""
        gap = self.gap
        if gap is None or not self.leaving_price:
            return None
        return gap / self.leaving_price * 100.0


@dataclass(slots=True)
class StitchReport:
    """Что вышло из одной сборки ряда."""

    symbol: str
    legs: list[Leg] = field(default_factory=list)
    seams: list[Seam] = field(default_factory=list)
    #: Сколько минуток снято перед сборкой: прошлый ряд стирается целиком.
    forgotten: int = 0
    #: Сколько минуток прочитано у контрактов и сколько записано в ряд.
    taken: int = 0
    written: int = 0
    #: Сколько минуток каждый контракт отдал в ряд.
    by_leg: dict[str, int] = field(default_factory=dict)
    first: datetime | None = None
    last: datetime | None = None

    def summary(self) -> str:
        """Одна строка для журнала — человеческим языком."""
        span = (
            f"{self.first:%d.%m.%Y} … {self.last:%d.%m.%Y}"
            if self.first and self.last
            else "ряд пуст"
        )
        parts = [
            f"{self.symbol}: {span}",
            f"контрактов {len(self.legs)}",
            f"свечей {self.written}",
        ]
        if self.forgotten:
            parts.append(f"прошлый ряд снят: {self.forgotten}")
        for seam in self.seams:
            gap = seam.gap_percent
            said = f"{gap:+.2f} %" if gap is not None else "разрыв не измерен"
            parts.append(f"{seam.leaving}→{seam.arriving} {seam.day:%d.%m.%Y} {said}")
        return "; ".join(parts)


def daily_volume(store: CandleStore, symbol: str) -> dict[date, float]:
    """Объём в контрактах по дням — из минуток, лежащих в базе.

    Дни без сделок в словарь не попадают вовсе: «нуля» и «биржа не работала»
    слой данных не различает и различать не должен (`market.gaps`).
    """
    totals: dict[date, float] = collections.defaultdict(float)
    for moment, volume in store.minute_volumes(symbol):
        totals[moment.astimezone(MSK).date()] += volume
    return {day: total for day, total in totals.items() if total > 0}


def roll_day(
    leaving: Mapping[date, float], arriving: Mapping[date, float]
) -> date | None:
    """День, с которого ряд переходит со старого контракта на новый.

    **Первый торговый день нового контракта после последнего дня, в который
    старый ещё торговался активнее нового.** Разбор правила — в шапке модуля.

    `None` означает «рубежа в этих данных нет»: новый контракт не торговался
    после того дня, когда старый был впереди. Так выглядит недокачанная
    история нового контракта, и молчаливо резать её посередине нельзя.
    """
    both = sorted(set(leaving) | set(arriving))
    traded = [
        day for day in both if leaving.get(day, 0.0) + arriving.get(day, 0.0) > 0
    ]
    if not traded:
        return None
    old_ahead = [
        day for day in traded if arriving.get(day, 0.0) <= leaving.get(day, 0.0)
    ]
    if not old_ahead:
        # Новый впереди с самого первого дня со сделками: старый не был
        # активнее ни разу, и ряд начинается там же, где начинаются данные.
        return traded[0]
    last_ahead = max(old_ahead)
    later = sorted(
        day for day, volume in arriving.items() if volume > 0 and day > last_ahead
    )
    return later[0] if later else None


def legs_of_chain(volumes: Sequence[tuple[str, Mapping[date, float]]]) -> list[Leg]:
    """Разбить цепочку контрактов на отрезки ряда — по рубежам, без дыр.

    На вход — контракты **в порядке экспирации**, каждый со своими дневными
    объёмами. Первый в ряд не входит: он датирует начало второго.

    Отрезки идут встык: конец предыдущего — день перед началом следующего.
    Последний кончается последним днём своих данных.

    :raises ValueError: контрактов меньше двух, рубеж не нашёлся, рубежи идут
        не по возрастанию либо у последнего контракта нет данных после рубежа.
    """
    if len(volumes) < SHORTEST_CHAIN:
        raise ValueError(
            "цепочка короче двух контрактов: первый нужен только чтобы "
            "датировать начало второго, и в ряд он не входит"
        )
    starts: list[date] = []
    for (old_symbol, old), (new_symbol, new) in zip(volumes, volumes[1:], strict=False):
        day = roll_day(old, new)
        if day is None:
            raise ValueError(
                f"рубеж между «{old_symbol}» и «{new_symbol}» не нашёлся: "
                f"после последнего дня, когда «{old_symbol}» был активнее, "
                f"у «{new_symbol}» нет ни одного дня со сделками. Похоже "
                "на недокачанную историю — сшивать такое нельзя"
            )
        if starts and day <= starts[-1]:
            raise ValueError(
                f"рубеж «{new_symbol}» ({day}) не позже рубежа предыдущего "
                f"контракта ({starts[-1]}): цепочка перечислена не в порядке "
                "экспирации либо у контрактов перепутаны объёмы"
            )
        starts.append(day)
    legs: list[Leg] = []
    for index, (symbol, own) in enumerate(volumes[1:]):
        since = starts[index]
        if index + 1 < len(starts):
            until = starts[index + 1] - timedelta(days=1)
        else:
            traded = [day for day, volume in own.items() if volume > 0]
            if not traded or max(traded) < since:
                raise ValueError(
                    f"у последнего контракта «{symbol}» нет сделок после "
                    f"рубежа {since}: ряд оборвался бы на пустом месте"
                )
            until = max(traded)
        legs.append(Leg(symbol=symbol, since=since, until=until))
    return legs


def measure_seam(
    store: CandleStore, leaving: str, arriving: str, day: date, *, look_back: int = 7
) -> Seam:
    """Насколько два контракта разошлись в цене к рубежу.

    Берётся **последняя минута, в которую торговались оба** — не позже дня
    перед рубежом. Смотрим назад не дальше `look_back` календарных дней:
    выходные и праздники встречаются, а месяц назад разница контрактов уже
    другая и о стыке ничего не говорит.
    """
    until = datetime.combine(day, datetime.min.time(), MSK)
    since = until - timedelta(days=look_back)
    old = {candle.time: candle.close for candle in store.minutes(leaving, since, until)}
    new = {candle.time: candle.close for candle in store.minutes(arriving, since, until)}
    common = sorted(set(old) & set(new))
    if not common:
        return Seam(day, leaving, arriving, None, None, None)
    at = common[-1]
    return Seam(day, leaving, arriving, at, old[at], new[at])


def stitch(
    source: CandleStore,
    target: CandleStore,
    symbol: str,
    legs: Sequence[Leg],
    *,
    now: datetime | None = None,
) -> StitchReport:
    """Собрать ряд `symbol` из отрезков и записать его в `target`.

    Прошлый ряд снимается **целиком** перед сборкой. Иначе смена рубежа
    оставила бы хвост прошлой сборки за пределами новых отрезков, и в ряду
    оказались бы дни двух контрактов сразу — ровно та беда, от которой
    сшивка и защищает.

    Повтор сборки на тех же данных поэтому даёт тот же ряд: то же число
    минуток, те же рубежи, ноль лишних строк.

    :raises ValueError: код ряда не синтетический, отрезков нет либо они
        налезают друг на друга.
    """
    if not is_synthetic(symbol):
        raise ValueError(
            f"«{symbol}» — код инструмента биржи, и собранный ряд под таким "
            "именем неотличим от настоящих свечей. Имя ряда начинается с «@»"
        )
    if not legs:
        raise ValueError("сшивать нечего: список отрезков пуст")
    for earlier, later in zip(legs, legs[1:], strict=False):
        if later.since <= earlier.until:
            raise ValueError(
                f"отрезки «{earlier.symbol}» и «{later.symbol}» налезают друг "
                f"на друга: {earlier.until} и {later.since}. Одна минута ряда "
                "не может принадлежать двум контрактам"
            )
    report = StitchReport(symbol=symbol, legs=list(legs))
    report.forgotten = target.forget_minutes(symbol)
    for leg in legs:
        since = datetime.combine(leg.since, datetime.min.time(), MSK)
        until = datetime.combine(leg.until + timedelta(days=1), datetime.min.time(), MSK)
        minutes: list[Candle] = list(source.minutes(leg.symbol, since, until))
        stats = target.put_minutes(symbol, minutes, Source.STITCHED)
        report.taken += len(minutes)
        report.written += stats.written
        report.by_leg[leg.symbol] = len(minutes)
    for earlier, later in zip(legs, legs[1:], strict=False):
        report.seams.append(
            measure_seam(source, earlier.symbol, later.symbol, later.since)
        )
    coverage = target.coverage(symbol)
    report.first, report.last = coverage.first, coverage.last
    target.write_load_report(_as_load_report(report), now=now)
    return report


def _as_load_report(report: StitchReport) -> LoadReport:
    """Отчёт о сборке — в тот же журнал загрузок, тем же видом записи.

    Сборка — это загрузка ряда, и прятать её от журнала нельзя: без строки
    в журнале сшитый ряд неотличим от загруженного с биржи, а рубежи в нём
    не видны вовсе.
    """
    return LoadReport(
        symbol=report.symbol,
        source=Source.STITCHED.value,
        requested_from=report.legs[0].since if report.legs else None,
        requested_to=report.legs[-1].until if report.legs else None,
        ranges=[(leg.since, leg.until) for leg in report.legs],
        fetched=report.taken,
        inserted=report.written,
        first_time=report.first,
        last_time=report.last,
        note=report.summary(),
    )
