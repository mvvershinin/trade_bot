"""Дневное состояние счёта: счёт на утро, результат дня, даты тейка и выхода.

Зачем
-----
Решение 0066, Р7: перезапуск программы в тот же день не обнуляет день.
Движок держит четыре дневные величины — счёт на утро и результат дня
(`engine.DayMoney`), дату последнего тейка и дату последнего выхода
(`EngineState.take_profit_date`, `last_exit_date`), — и все четыре живут
в памяти. Без восстановления перезапуск после утреннего тейка снимает
«стоп после тейка»: робот входит второй раз в день, когда входить нельзя.
Значения отдаются движку при старте **значением**, как `on_funds`; правило
«стоп после тейка» остаётся в движке.

Хранится одно, остальное считается
----------------------------------
Отдельной таблицы дня **нет**, и это выбор, а не экономия.

* **Два источника правды для одной сделки.** Исполнение ложится одной
  транзакцией (`CandleStore.record_broker_execution`), строка дня легла бы
  другой. Падение между ними оставляет день без сделки, и после перезапуска
  «стоп после тейка» молчит — ровно тот дефект, ради которого Р7.
* **Сделки «закрыть всё» идут мимо движка** (0066, сверка 0020). С таблицей
  дня их обязан не забыть дописать каждый путь, подающий заявку; расчёт
  по исполнениям берёт их по построению — у них такие же строки.
* **Исполнения — факт биржи под сторожами**: не правятся и не удаляются.
  День, посчитанный из них, после перезапуска тот же по построению.

Цена — проход по исполнениям счёта на каждый запрос. Строк единицы в день,
а спрашивают день при старте и после «Возобновить», не на каждой свече.

Вывести не из чего только **счёт на утро**: это снимок портфеля у брокера,
а не сделка. Он хранится — первый снимок даты, один раз (`OpeningFunds`,
таблица `account_opening`).

Счёт ведётся так же, как его ведёт движок
-----------------------------------------
Движок (`engine/pipeline.py`, `_day_after`) прибавляет к результату даты
**сделки** (по времени сделки, МСК):

* у входа — минус комиссия входа;
* у выхода — пункты × рублей за пункт минус комиссия выхода.

Здесь то же самое, по событиям (`DayEvent`). Источников два, путь один:

* **бой** — исполнения брокера (`execution_events`): сторона и тикер берутся
  из команды, по которой исполнение пришло, вход и выход различает проход
  «первым вошёл — первым вышел» по каждому тикеру. Исполнения одной заявки
  движок видит одной сделкой со средней ценой (Р6); сумма по исполнениям
  совпадает с ней, потому что результат линеен по объёму;
* **симуляция** — журнал сделок её прогона (`trade_events`): у сделки вход
  и выход уже разделены, валовый результат записан, комиссия обеих сторон
  делится пополам — у модели исполнения она одна на сторону при равном
  объёме (`backtest.execution`, `costs.commission(order.volume)`).

Комиссия `None` — не известна. Движок считает её нулём
(`fill.commission or 0.0`), и здесь так же, но число таких событий даты
отдаётся отдельно (`TradingDay.unknown_commissions`): результат без комиссии
в этом проекте результатом не считается (правило 4), и окно обязано это
сказать.

Чего не делает
--------------
* Не знает имён заявок движка. Тейк от прочего выхода отличает
  предикат `is_take`, его передаёт `app/`: имя `take:…` — соглашение
  `engine/`, импортировать который слою данных нельзя (ARCHITECTURE.md §2).
* Исполнение без команды (заявка подана руками из приложения брокера)
  не учитывается: стороны у него нет, и вход ли это или выход, не сказать.
  Такие исполнения даты названы поимённо (`TradingDay.foreign`) — не пусто
  означает, что результат дня неполон.
* Не открывает позицию, которой нет в исполнениях. Позиция, открытая
  до появления журнала команд, для прохода — чужая.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime

from market.candles import ensure_msk
from market.commands import BrokerExecution, CommandSide
from market.journal import TradeRecord

__all__ = [
    "AttributedExecution",
    "DayEvent",
    "OpeningFunds",
    "TradingDay",
    "day_of",
    "execution_events",
    "trade_events",
]


def _finite(value: float, what: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError(f"{what}: {value!r} — не конечное число")


@dataclass(frozen=True, slots=True)
class OpeningFunds:
    """Первый снимок счёта календарной даты — «счёт на утро».

    Повторяет `engine.AccountFunds` полем в поле, плюс счёт: деньги разных
    счетов не смешиваются (в `userdata/` бывает два токена, находка `/risk`).

    ⚠️ «Первый снимок даты» — не всегда утро: программу могли запустить
    в середине дня. Время снимка хранится, чтобы окно называло его,
    а не слово «утро» (`engine.DayMoney`).

    :param account: номер договора у брокера (`agreementId`).
    :param at: момент снимка; дата — его дата по МСК.
    :param margin_per_contract: ГО под контракт; `None` — не сообщено,
        это не ноль.
    """

    account: str
    at: datetime
    equity: float
    free: float
    margin_per_contract: float | None = None

    def __post_init__(self) -> None:
        if not self.account.strip():
            raise ValueError("снимок счёта без номера счёта: чей это день, не сказать")
        ensure_msk(self.at)
        _finite(self.equity, "размер счёта")
        _finite(self.free, "свободные средства")
        if self.margin_per_contract is not None:
            _finite(self.margin_per_contract, "ГО под контракт")
            if self.margin_per_contract <= 0:
                raise ValueError(
                    f"ГО под контракт {self.margin_per_contract!r}: ноль означал бы, "
                    "что свободных средств хватает на любой объём"
                )

    @property
    def day(self) -> date:
        """Календарная дата снимка по МСК."""
        return ensure_msk(self.at).date()


@dataclass(frozen=True, slots=True)
class DayEvent:
    """Сделка глазами дня: когда, сколько зафиксировано, какая комиссия.

    :param gross: рубли, зафиксированные этим событием. У входа ноль.
    :param commission: комиссия события; `None` — не известна.
    :param exit: событие закрыло позицию (целиком или частью).
    :param take: это выход по уровню тейка (`is_take` по имени заявки).
    """

    at: datetime
    gross: float
    commission: float | None
    exit: bool
    take: bool


@dataclass(frozen=True, slots=True)
class TradingDay:
    """День счёта, восстановленный из базы: то, что движок держал в памяти.

    :param account: чей это день; `None` — день прогона симуляции, у которого
        счёт не назвали (`CandleStore.journal_day`).
    :param opening: первый снимок счёта этой даты; `None` — снимка не было.
    :param gross: валовый результат сделок, закрытых в эту дату, в рублях.
    :param commission: известная комиссия сделок этой даты, обеих сторон.
    :param unknown_commissions: сколько сделок даты без комиссии. Не ноль —
        `realized` выше настоящего, и окно обязано это сказать.
    :param take_profit_date: дата последнего выхода по тейку не позже `day`.
    :param last_exit_date: дата последнего выхода не позже `day`, включая
        «закрыть всё».
    :param foreign: исполнения даты без команды — не учтены. Не пусто —
        результат дня неполон.
    """

    account: str | None
    day: date
    opening: OpeningFunds | None
    gross: float
    commission: float
    unknown_commissions: int
    take_profit_date: date | None
    last_exit_date: date | None
    foreign: tuple[str, ...] = ()

    @property
    def realized(self) -> float:
        """Зафиксированный результат даты, комиссия вычтена (`DayMoney.realized`)."""
        return self.gross - self.commission


def day_of(
    events: Iterable[DayEvent],
    *,
    account: str | None,
    day: date,
    opening: OpeningFunds | None = None,
    foreign: tuple[str, ...] = (),
) -> TradingDay:
    """Собрать день из событий. Общий путь боя и симуляции.

    События позже `day` не учитываются вовсе: день, спрошенный задним числом,
    не знает завтрашнего тейка.
    """
    gross = commission = 0.0
    unknown = 0
    took: date | None = None
    left: date | None = None
    for event in events:
        when = ensure_msk(event.at).date()
        if when > day:
            continue
        if event.exit:
            left = when if left is None else max(left, when)
        if event.take:
            took = when if took is None else max(took, when)
        if when != day:
            continue
        gross += event.gross
        if event.commission is None:
            unknown += 1
        else:
            commission += event.commission
    return TradingDay(
        account=account,
        day=day,
        opening=opening,
        gross=gross,
        commission=commission,
        unknown_commissions=unknown,
        take_profit_date=took,
        last_exit_date=left,
        foreign=foreign,
    )



@dataclass(frozen=True, slots=True)
class AttributedExecution:
    """Исполнение вместе с тем, что о нём знает журнал команд.

    :param side: сторона сделки из команды, по которой исполнение пришло.
    :param symbol: тикер команды.
    :param order_id: имя заявки у движка; по нему `is_take` узнаёт тейк.
        У «закрыть всё» — имя, данное ей `app/`, и тейком оно не будет.
    """

    execution: BrokerExecution
    side: CommandSide
    symbol: str
    order_id: str


@dataclass(slots=True)
class _Lot:
    """Открытая часть позиции: сторона (+1 лонг, −1 шорт), объём, цена входа."""

    sign: int
    volume: int
    price: float


def _close_against(book: list[_Lot], sign: int, volume: int, price: float) -> tuple[int, float]:
    """Закрыть встречные части позиции. Возвращает (закрытый объём, пункты × объём).

    «Первым вошёл — первым вышел»: у движка позиция одна, и порядок частей
    на результат не влияет, а у позиции из нескольких входов он хотя бы
    определён.
    """
    closed = 0
    points = 0.0
    while volume and book and book[0].sign != sign:
        lot = book[0]
        matched = min(volume, lot.volume)
        points += (price - lot.price) * lot.sign * matched
        closed += matched
        volume -= matched
        lot.volume -= matched
        if not lot.volume:
            book.pop(0)
    return closed, points


def execution_events(
    rows: Iterable[AttributedExecution],
    *,
    ruble_per_point: Mapping[str, float],
    is_take: Callable[[str], bool],
) -> tuple[DayEvent, ...]:
    """Исполнения счёта → события дня. Порядок строк — порядок исполнений.

    Вход и выход различает проход по каждому тикеру: исполнение против
    открытой позиции — выход (на закрытый объём), остаток — вход. Переворот
    одним исполнением даёт одно событие с признаком выхода.

    :param ruble_per_point: рублей за пункт по тикеру. Нужен только тикеру,
        у которого был выход; нет его — отказ, а не единица: результат
        в пунктах, выданный за рубли, ошибается в десятки раз.
    :raises ValueError: у тикера с выходом не назван вес пункта.
    """
    books: dict[str, list[_Lot]] = {}
    events: list[DayEvent] = []
    for row in rows:
        fill = row.execution
        sign = 1 if CommandSide(row.side) is CommandSide.BUY else -1
        book = books.setdefault(row.symbol, [])
        closed, points = _close_against(book, sign, fill.volume, fill.price)
        if fill.volume > closed:
            book.append(_Lot(sign, fill.volume - closed, fill.price))
        gross = 0.0
        if closed:
            weight = ruble_per_point.get(row.symbol)
            if weight is None:
                raise ValueError(
                    f"исполнение {fill.execution_id} закрыло позицию по {row.symbol}, "
                    "а рублей за пункт этого тикера не назвали: результат дня "
                    "в пунктах, выданный за рубли, был бы неверен"
                )
            gross = points * weight
        events.append(DayEvent(
            at=fill.at,
            gross=gross,
            commission=fill.commission,
            exit=closed > 0,
            take=closed > 0 and is_take(row.order_id),
        ))
    return tuple(events)


def trade_events(
    trades: Iterable[TradeRecord], *, is_take: Callable[[str], bool]
) -> tuple[DayEvent, ...]:
    """Сделки журнала прогона → события дня: вход и выход каждой.

    Валовый результат берётся записанным, а не пересчитывается: журнал —
    свидетельство того, что было посчитано тем весом пункта
    (`market.journal.TradeRecord`). Комиссия обеих сторон делится пополам
    (шапка модуля); `None` остаётся `None` у обеих.
    """
    events: list[DayEvent] = []
    for trade in trades:
        half = None if trade.commission is None else trade.commission / 2
        events.append(DayEvent(trade.entry_time, 0.0, half, exit=False, take=False))
        events.append(DayEvent(
            trade.exit_time, trade.gross, half, exit=True, take=is_take(trade.exit_order_id)
        ))
    return tuple(events)
