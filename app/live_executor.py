"""Боевой исполнитель заявок: порт `engine.ports.OrderExecutor` поверх брокера БКС.

Э4, шаг 2, под-шаг 2б (`.docs/plans/E4-live-orders.md` §12; постановка —
решение 0066). Пишущий путь, поток статусов и журнал команд — шаг 1
(`broker/session.py`, `broker/order_stream.py`, `market/commands.py`). Здесь —
как из них складываются «принято», «исполнено» и «торговать дальше нельзя».

Что делает
----------
* **Перед каждой заявкой** (план §2.2): заявка свежая, токен по-прежнему
  торговый (файл перечитывается, `D-175`), поток статусов жив, у брокера та
  позиция, которую считает движок (выход — та же сторона и объём, вход —
  позиции нет). Несвежая заявка и мёртвый поток — `ExecutionRefused`: заявки
  нет, сигнал пропущен (`DOMAIN.md` §7, план §8 №7, №15). Расхождение позиции —
  «закрыть всё» (решение 0020) и `ExecutionHalted`.
* **Рыночных заявок у брокера нет** (решение 0067). Вход, выход, переворот,
  выход по касанию трейлинга, остаток тейка и «закрыть всё» — лимитная
  «исполнить целиком или снять» (FOK) по планке биржи, без планки — по цене
  «текущая ± процент» (владелец 10.11.2026), и собирает её одно место — `_fok`.
  «Текущая» (Р-2 плана координатора 04.11.2026): у входа и выхода движка —
  закрытие свечи сигнала, то есть свечи последнего
  закрытого `fills_at` (заявка свечи T подаётся после `fills_at(T)`,
  `engine/ports.py`); у касания трейлинга — уровень касания; у остатка тейка
  и «закрыть всё» — закрытие последней записанной минутки, и только свежей:
  минутка старше порога свежести считается отсутствующей.
* **Заявка немедленного исполнения** ждёт конца — `2`, `4` или `8` (0066,
  §2.6). Исполнения одной заявки складываются в **одну** сделку движку:
  объём — сумма, цена — средняя; частичного движок не видит (Р6). Снята
  целиком — остановка словами статуса с хвостом «FOK» (Р-6); частичная без
  конца за предел — страховка: остаток снимается, `ExecutionHalted`.
* **Неподвижный тейк** — лимитная заявка у брокера по цене из заявки движка,
  как есть: к шагу её приводит движок (Р1). Снятие ждёт `4`. Снята не нами —
  остановка. Исполнилась частично к снятию — остаток закрывается FOK,
  движку одна сделка на весь объём и `ALREADY_TRIGGERED`.
* **Скользящий тейк** — сторож здесь, в программе (Р2). Касание судится по
  high/low **записанных минуток**, уровнем, поданным не позже начала минутки, —
  зеркало `submitted_at > ready_at` модели (`backtest/execution.py::_fire`).
  На касании — FOK от уровня касания; движку — сделка по закрытой свече
  касания. Касание вовремя и FOK снята — остановка (Р-6). Касание, выход
  по которому подан позже порога свежести (догруженная минутка или выход
  ждал связи), и FOK снята целиком — не остановка: движку событие
  `LevelUnfilled` по закрытой свече, решает он (0067, Р-10). До отдачи
  события сторож остаётся и отвечает `ALREADY_TRIGGERED`.
* **Механизм тейка выбирается по паре (сторона позиции, касание) из заявки**,
  а не по режиму и не по галочке: «лонг, снизу» и «шорт, сверху» — уровень
  впереди рынка, лимитная у брокера; две другие пары — уровень позади рынка,
  то есть стоп, и лимитной его выразить нельзя: она исполнилась бы сразу
  (план §2.3). Запрет держит таблица `_AT_BROKER`, а не окно.
* **Выход с пределом** — по концу окна (Р3) и рождённый событием
  `LevelUnfilled` — лимитная до конца дня по планке биржи (`HIGHLIMIT`
  покупка, `LOWLIMIT` продажа, к сетке шага внутрь коридора), если она пришла
  за `band_wait` и лежит по нужную сторону от закрытия свечи сигнала; иначе —
  предел движка (Р-4). Какая цена подана и откуда — своей строкой человеку.
  Не исполнилась за `limit_exit_bars` закрытых свечей — остановка, текст
  называет живую заявку у брокера, её цену и ключ (С9).
* **Неясный исход** — таймаут, обрыв, 5xx, 429, отмена задачи посреди подачи
  (`D-177`) — остановка, а не «не подана»: команда записана, заявка у брокера
  может жить. Разбирается сверкой (`settle_open_commands`, `app/reconcile.py`).

Чего не делает
--------------
* Не решает, когда подавать, и цену уровня не меняет: это движок.
* Не спрашивает брокера на каждом `fills_at` (порт требует дешёвого вызова,
  `engine/ports.py`): сделки приходят потоком статусов, `fills_at` отдаёт
  накопленное. Минутки сторожа читаются из базы — и только пока он вооружён.
* С окном не связан и `app/observe.py` его не импортирует: исполнитель
  приходит к ходу параметром, собирает его `app/live_trading.py` (2в).

Ожидания и пороги — `LiveLimits` (`app/fact.py`: значения без брокера,
их собирает окно); `limit_exit_bars` кладёт сборка из движка (2в).
Живьём не проверено ничего: токена с правами торговли в разработке нет,
всё — по снимку документации и подставному брокеру `tests/fake_broker.py`.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
import re
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final, NamedTuple, Protocol
from uuid import UUID

from app.fact import LiveLimits, position_words, signed_position
from app.halt import halt_reason
from broker.account import read_holdings
from broker.errors import (
    AuthFailed,
    BadRequest,
    BrokerError,
    NoConnection,
    NotFound,
    OutcomeUnknown,
    ServerFailure,
    Throttled,
    Timeout,
    UnexpectedAnswer,
    WrongAddress,
)
from broker.order_stream import order_events
from broker.orders import (
    FINAL_STATUSES,
    STATUS_WORDS,
    ListedOrder,
    ListedOrderStatus,
    NewOrder,
    OrderCommand,
    OrderEvent,
    OrderReport,
    OrderSide,
    OrdersQuery,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from broker.session import PORTFOLIO_PATH, BrokerSession
from broker.tokens import now_utc
from engine import close_time, in_moscow, limit_beyond
from engine.contracts import (
    Fill,
    LevelTouch,
    LevelUnfilled,
    OrderAction,
    OrderRequest,
    PriceSeen,
    Side,
)
from engine.ports import ExecutionHalted, ExecutionRefused, MarketCandle, Refusal
from engine.take_profit import to_grid
from market import DecisionLevel, MarketWorker, PriceBand
from market.commands import (
    BrokerCommand,
    BrokerExecution,
    CommandAction,
    CommandSide,
    CommandStatus,
    StoredCommand,
)

__all__ = [
    "CommandBook",
    "EventStream",
    "Instrument",
    "LiveExecutor",
    "LiveLimits",
    "StoreBook",
    "Wiring",
    "hand_words",
    "position_words",
    "signed_position",
]

log = logging.getLogger(__name__)

#: Пары (сторона позиции, касание), у которых уровень **впереди** рынка:
#: неподвижный тейк лонга и шорта. Только они становятся лимитной заявкой
#: у брокера (Р1). Остальные две — стоп позади рынка: лимитная продажа ниже
#: рынка исполнилась бы сразу, то есть стала бы немедленным выходом
#: (план §2.3, `engine/contracts.py::LevelTouch`). Их сторожит программа (Р2).
_AT_BROKER: Final[frozenset[tuple[Side, LevelTouch]]] = frozenset(
    {(Side.LONG, LevelTouch.RISE), (Side.SHORT, LevelTouch.FALL)}
)

#: Сторона сделки, открывающей и закрывающей позицию движка.
_OPENING: Final[dict[Side, OrderSide]] = {Side.LONG: OrderSide.BUY, Side.SHORT: OrderSide.SELL}
_CLOSING: Final[dict[Side, OrderSide]] = {Side.LONG: OrderSide.SELL, Side.SHORT: OrderSide.BUY}
_JOURNAL_SIDE: Final[dict[OrderSide, CommandSide]] = {
    OrderSide.BUY: CommandSide.BUY,
    OrderSide.SELL: CommandSide.SELL,
}
_GENITIVE: Final[dict[Side, str]] = {Side.LONG: "лонга", Side.SHORT: "шорта"}

#: Отказы связи **до** записи журнала: команда не ушла, и связь вернётся.
#: Заявки нет точно — это определённый отказ, сигнал пропущен.
_TRANSIENT: Final = (NoConnection, Timeout, ServerFailure, Throttled)

#: Отказы брокера **после** записи, после которых заявки у него нет точно.
_REFUSED_AFTER_RECORD: Final = (BadRequest, AuthFailed, NotFound, WrongAddress)

#: Сбои записи в базу: диск, блокировка, сторож, закрытый поток данных.
_JOURNAL_FAILURES: Final = (sqlite3.Error, ValueError, LookupError, RuntimeError, OSError)

#: Конец заявки у брокера → статус строки журнала команд.
_FINAL_IN_JOURNAL: Final[dict[OrderStatus, CommandStatus]] = {
    OrderStatus.FILLED: CommandStatus.FILLED,
    OrderStatus.CANCELLED: CommandStatus.CANCELLED,
    OrderStatus.REJECTED: CommandStatus.REJECTED,
}

#: Хвост каждой остановки: что делать человеку.
_HALT_TAIL: Final[str] = (
    "Робот остановлен. Прежде чем продолжать, проверьте позицию и заявки у брокера."
)

#: Чужой текст (причина отказа биржи) — без управляющих знаков и не длиннее этого.
_FOREIGN_TEXT_LIMIT: Final[int] = 200

#: Связь с потоком статусов считается восстановленной, если она продержалась
#: столько секунд (З1, З9 `/risk`): тогда пауза переподключения снова первая.
#: Связь короче — продолжение прежнего обрыва, и пауза растёт до потолка,
#: прошла ли сверка после неё или нет: сброс по сверке в шторм обрывов водил
#: паузу по кругу 1 → 2 → 5 с, не давая ей дойти до потолка (З9).
#:
#: ⚠️ Число техническое, не торговое: не цена, не объём и не окно, а «когда
#: обрыв считать новым, а не продолжением прежнего». 10 с выбраны так: при
#: обрывах раз в 50 с поток лежит ~2 % времени (пауза 1 с); при шторме, где
#: каждая связь короче 10 с, пауза доходит до потолка, и сверок с настоящим
#: чтением портфеля — не больше одной на паузу. В окно не выведено; если
#: владелец захочет крутить — одно поле `LiveLimits` (`app/fact.py`).
_LINK_SETTLE: Final[float] = 10.0
_CONTROL: Final[re.Pattern[str]] = re.compile(r"[\x00-\x1f\x7f]+")

#: Насколько назад искать последнюю записанную минутку — «текущую» у «закрыть
#: всё» и остатка тейка. Число только для слов: минутка старше порога
#: свежести заявке не годится всё равно, а сутки назад — чтобы назвать её
#: возраст, а не «минуток нет». На цену заявки не влияет.
_MINUTE_LOOKBACK: Final[timedelta] = timedelta(days=1)

#: Сбои источника планки биржи: поток данных закрыт (`WorkerClosed` —
#: `RuntimeError`), негодный таймаут, ошибка сети мимо разбора ISS. Любой из
#: них значит одно — подать предел движка и сказать почему (Р-4).
_BAND_FAILURES: Final = (RuntimeError, ValueError, LookupError, OSError)


# --------------------------------------------------------------- настройки


@dataclass(frozen=True, slots=True)
class Instrument:
    """Где и чем торгует исполнитель.

    :param account: счёт (`agreementId`), которому подаются команды. Пишется
        в каждую строку журнала: в `userdata/` бывает два токена на два счёта,
        и день счёта отбирает свои исполнения по нему (`market/day.py`).
    :param bar_minutes: размер свечи — для свежести заявки (§2.2 п. 1).
    :param price_step: шаг цены инструмента — сетка цены каждой заявки FOK
        и планки биржи (0067). Кладёт сборка из движка
        (`EngineSettings.price_step`), отдельного запроса шага нет. Не больше
        нуля — отказ сразу: цену без шага не положить на сетку, а бой при
        шаге ноль и так запрещён (`app/port.py`).
    """

    ticker: str
    class_code: str
    bar_minutes: int
    account: str
    price_step: float

    def __post_init__(self) -> None:
        step = self.price_step
        if isinstance(step, bool) or not isinstance(step, (int, float)) or not step > 0:
            raise ValueError(f"шаг цены инструмента — число больше нуля: {step!r}")


class _JournalDown(Exception):
    """Строка журнала команд не записалась: команда к брокеру не уходит (С5).

    Свой тип, а не сбой базы как есть: сессия пропускает исключение записи
    наружу без разбора, и отличить «база не приняла» от прочего можно только
    меткой, поставленной в самой записи. Текст — имя типа сбоя, без его слов:
    в них бывают значения строки.
    """


# --------------------------------------------------------------- журнал


class CommandBook(Protocol):
    """Журнал команд брокеру глазами исполнителя. Каждый вызов — после фиксации."""

    async def record(self, command: BrokerCommand) -> None:
        """Записать команду **до** отправки. Исключение — команда не уходит."""
        ...

    async def set_status(self, key: str, status: CommandStatus, reason: str) -> None:
        """Сменить статус команды."""
        ...

    async def record_execution(self, execution: BrokerExecution) -> None:
        """Записать исполнение; повтор `executionId` второй строки не даёт."""
        ...

    async def open_commands(self) -> tuple[StoredCommand, ...]:
        """Незакрытые команды всех прогонов."""
        ...


class StoreBook:
    """Журнал команд в базе — **через поток базы**, а не в цикле событий (план §11).

    `session_id` — прогон «факта», подающий команды (2в).
    """

    def __init__(self, worker: MarketWorker, session_id: int) -> None:
        self._worker = worker
        self._session_id = session_id

    async def record(self, command: BrokerCommand) -> None:
        """Строка команды до отправки; возврат — после `COMMIT`."""
        session = self._session_id
        await self._worker.call(lambda store: store.record_broker_command(session, command))

    async def set_status(self, key: str, status: CommandStatus, reason: str) -> None:
        """Новый статус команды; тот же статус не пишется второй раз."""
        await self._worker.call(
            lambda store: store.set_broker_command_status(key, status, reason=reason)
        )

    async def record_execution(self, execution: BrokerExecution) -> None:
        """Исполнение в таблицу исполнений."""
        await self._worker.call(lambda store: store.record_broker_execution(execution))

    async def open_commands(self) -> tuple[StoredCommand, ...]:
        """Незакрытые команды журнала."""
        return await self._worker.call(lambda store: store.open_broker_commands())


# --------------------------------------------------------------- проводка


class EventStream(Protocol):
    """Открытый поток статусов заявок (`broker.order_stream.OrderEvents`)."""

    def __aiter__(self) -> AsyncIterator[OrderEvent]:
        """События до обрыва; обрыв — `NoConnection`."""
        ...

    async def alive(self) -> float:
        """Ping-pong сейчас; мёртв — `NoConnection`."""
        ...


#: Минутки инструмента начиная с момента (`time >= since`), по возрастанию.
#: 2в: `lambda since: worker.minutes(ticker, since)`.
MinuteSource = Callable[[datetime], Awaitable[Sequence[MarketCandle]]]

#: Как открыть поток статусов. По умолчанию — `order_events(session)`.
EventsOpener = Callable[[], AbstractAsyncContextManager[EventStream]]

#: Планка биржи по инструменту исполнителя (Р-4). Сборка (`app/live_trading.py`):
#: `lambda: worker.price_band(ticker, timeout=limits.band_wait)`. Исполнитель
#: ждёт её не дольше `LiveLimits.band_wait` сам, обещанию источника не веря.
BandSource = Callable[[], Awaitable[PriceBand]]


@dataclass(frozen=True, slots=True)
class Wiring:
    """С кем исполнитель говорит: брокер, журнал, минутки, часы.

    :param on_news: зовётся, когда у исполнителя появилось, что отдать движку
        (сделка или остановка). 2в ставит по нему проход движка — иначе сделка
        входа дойдёт до движка только на первой минутке следующей свечи,
        и позиция всё это время стоит без тейка (план §3).
    :param sleep: пауза между попытками открыть поток статусов.
    """

    session: BrokerSession
    book: CommandBook
    minutes: MinuteSource
    events: EventsOpener | None = None
    clock: Callable[[], datetime] = now_utc
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    on_news: Callable[[], None] | None = None
    #: Сообщение человеку **без** остановки: 2в выводит его в журнал решений
    #: и окно с уровнем строки — WARNING у предупреждения, INFO у штатной
    #: строки о поданной заявке (0067). Не подключено — остаётся строка
    #: технического лога.
    notice: Callable[[str, DecisionLevel], None] | None = None
    #: Планка биржи — цена каждой FOK и выхода с пределом (0067, Р-4; владелец
    #: 10.11.2026). `None` — источника нет: FOK «текущая ± %», выход с пределом —
    #: предел движка, и строка об этом говорит.
    band: BandSource | None = None


# --------------------------------------------------------------- состояние


class _Answer(enum.Enum):
    """Что пишущий путь услышал на команду."""

    NONE = "none"
    ACCEPTED = "accepted"
    REFUSED = "refused"
    UNKNOWN = "unknown"


@dataclass(eq=False)
class _Cancel:
    """Запрос на снятие: свой ключ и что на него ответил брокер."""

    key: UUID
    answer: _Answer = _Answer.NONE
    journaled: CommandStatus | None = None


@dataclass(eq=False)
class _Ticket:
    """Одна наша заявка у брокера и всё, что о ней известно.

    `name` — имя заявки **движка**, под которым её сделка уйдёт в `Fill`:
    у тейка и его остатка оно одно, у брокера ключей два (план §2.9).
    """

    key: UUID
    name: str
    what: str
    order: NewOrder
    answer: _Answer = _Answer.NONE
    status: OrderStatus | None = None
    executed: int = 0
    average: float | None = None
    reject: str | None = None
    first_at: datetime | None = None
    missing: bool = False
    cancelling: bool = False
    settled: bool = False
    #: Заявка подана этим исполнителем ради сделки движка. `False` — взята
    #: из журнала прошлого запуска: её исполнение движку не уходит никогда.
    bound: bool = True
    #: Сколько исполнено к концу сверки — исполнение сверх этого у заявки
    #: из журнала есть сделка без движка (С1).
    known_volume: int = 0
    #: О каком состоянии заявки (статус, исполнено) сверка уже сказала
    #: человеку: то же состояние на следующем переоткрытии не повторяется (Н5).
    told_state: tuple[OrderStatus | None, int] | None = None
    note: str = ""
    journaled: CommandStatus | None = None
    #: «Текущая» цена при подаче (`_Priced.current`) — для строки об исполнении.
    current: float | None = None
    #: Заявка закрывает позицию: снятая FOK оставляет её открытой без тейка (6.2).
    closing: bool = False
    #: Снятая целиком — не остановка: решает движок (`LevelUnfilled`, Р-10).
    #: Только у выхода по касанию трейлинга, поданного позже порога свежести.
    spared: bool = False
    executions: dict[str, BrokerExecution] = field(default_factory=dict)
    cancels: list[_Cancel] = field(default_factory=list)

    @property
    def final(self) -> bool:
        return self.status in FINAL_STATUSES

    @property
    def killed(self) -> bool:
        """Кончилась, не исполнившись ни на контракт: снята (`4`) или отклонена (`8`).

        У FOK это «не исполнилась целиком». Какой из двух статусов даёт БКС,
        неизвестно (план координатора §9), поэтому оба — одно.
        """
        return (
            self.status in (OrderStatus.CANCELLED, OrderStatus.REJECTED)
            and self.done_volume == 0
        )

    @property
    def done_volume(self) -> int:
        """Исполнено контрактов: по брокеру, а у «исполнена» без количества — весь объём."""
        if self.executed:
            return self.executed
        return self.order.quantity if self.status is OrderStatus.FILLED else 0

    def mean_price(self) -> float | None:
        """Средняя цена исполненного: от брокера, иначе по своим исполнениям."""
        if self.average is not None:
            return self.average
        volume = sum(one.volume for one in self.executions.values())
        if not volume:
            return None
        return sum(one.volume * one.price for one in self.executions.values()) / volume


@dataclass(eq=False)
class _Deal:
    """Сделка для движка: одна на заявку движка, сколько бы исполнений ни было.

    `level` — по уровню (тейк, сторож): отдаётся только по закрытой свече.
    `moment` — у немедленной (FOK) момент подачи (`submitted_at`): сделка не раньше
    свечи, начавшейся не раньше него, — как в модели; у уровня — момент
    касания: сделка по закрытой свече, в интервал которой он попал.
    """

    name: str
    action: OrderAction
    side: Side
    volume: int
    level: bool
    moment: datetime
    tickets: list[_Ticket] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return (
            bool(self.tickets)
            and all(ticket.final for ticket in self.tickets)
            and sum(ticket.done_volume for ticket in self.tickets) == self.volume
        )

    def price(self) -> float | None:
        """Средняя по всем исполнениям всех заявок сделки."""
        total = 0.0
        for ticket in self.tickets:
            volume = ticket.done_volume
            if not volume:
                continue
            price = ticket.mean_price()
            if price is None:
                return None
            total += price * volume
        return total / self.volume

    def commission(self) -> float | None:
        """Сумма комиссий всех исполнений сделки (Р6).

        Тогда день из таблицы исполнений сходится с днём движка. Хоть у одного
        комиссия не названа — `None`, а не сумма известных: неполная сумма
        выглядела бы точной.

        ⚠️ За исполнение брокер шлёт `commission` или итогом по заявке — нет
        в снимке (план §9). Если итогом, сумма завышена — проверка первого дня.
        """
        executions = [one for ticket in self.tickets for one in ticket.executions.values()]
        if not executions or any(one.commission is None for one in executions):
            return None
        return sum(one.commission or 0.0 for one in executions)


@dataclass(eq=False)
class _Take:
    """Сторожимый уровень под именем заявки `ARM` движка.

    `ticket` есть — уровень стоит лимитной заявкой у брокера; нет — сторожит
    программа по минуткам с `checked_from`. `deal` — уровень сработал.
    """

    order: OrderRequest
    ticket: _Ticket | None = None
    checked_from: datetime | None = None
    deal: _Deal | None = None
    told: bool = False

    @property
    def fired(self) -> bool:
        """Сработал: сделка по нему уже есть или заявка у брокера исполнилась."""
        if self.deal is not None:
            return True
        ticket = self.ticket
        return ticket is not None and ticket.final and ticket.done_volume > 0


#: Ответ источника планки (`_fetch_band`): планка — или `None` и почему её нет.
_BandAnswer = tuple[PriceBand | None, str]


class _Priced(NamedTuple):
    """Заявка к подаче и её цена словами — «по цене X: откуда она» (0067).

    Ходят парой: цена без слов дала бы строку журнала «подана» без цены,
    слова без заявки — цену, которой у брокера нет.
    """

    order: NewOrder
    words: str
    #: «Текущая» цена, от которой судят исполнение (владелец 10.11.2026):
    #: строка «исполнена по X, текущая была Y» — у каждой исполненной заявки,
    #: где «текущая» известна. `None` — не судится (тейк, несвежая минутка).
    current: float | None = None


@dataclass(eq=False)
class _Unfilled:
    """Событие «уровень не исполнился», ещё не отданное движку, и его сторож.

    Сторож остаётся в `_takes`, пока событие не отдано: вооружение и снятие
    этого имени отвечают `ALREADY_TRIGGERED`, и старый уровень не встанет
    заново до того, как движок сбросит план (гейт Р-10, требование к 2.1).
    """

    take: _Take
    event: LevelUnfilled


@dataclass(eq=False)
class _LimitExit:
    """Выход по времени с пределом, ждущий исполнения (Р3): сколько свечей прошло."""

    order: OrderRequest
    deal: _Deal
    ticket: _Ticket
    bars: set[datetime] = field(default_factory=set)
    reported: bool = False


# --------------------------------------------------------------- слова


def _order_words(order: OrderRequest) -> str:
    """Заявка движка словами — для текстов отказов и остановок."""
    if order.action is OrderAction.OPEN:
        return f"вход в {order.side.label.lower()}"
    if order.action is OrderAction.CLOSE:
        limit = "" if order.limit_price is None else f" с пределом {order.limit_price:g}"
        return f"выход из {_GENITIVE[order.side]}{limit}"
    if order.action is OrderAction.ARM_TAKE_PROFIT:
        return f"уровень {_GENITIVE[order.side]} {order.price or 0:g}"
    return f"снятие уровня {order.target_id or ''}".rstrip()


#: Сторона команды журнала словами — для списка заявок робота в окне.
_HAND_SIDE: Final[dict[CommandSide, str]] = {
    CommandSide.BUY: "покупка",
    CommandSide.SELL: "продажа",
}


class _HandCancel(NamedTuple):
    """Итог снятия одной заявки руками: слова, «у брокера её нет», сдвиг позиции."""

    text: str
    ended: bool
    #: Исполненное по заявке за время снятия — со знаком: покупка плюс, продажа минус.
    shift: int = 0


#: Сторона строки списка заявок брокера (15) — сторона команды журнала.
_LISTED_SIDE: Final[dict[OrderSide, CommandSide]] = {
    OrderSide.BUY: CommandSide.BUY,
    OrderSide.SELL: CommandSide.SELL,
}


def _listed_side(order: ListedOrder) -> CommandSide | None:
    """Сторона строки списка заявок брокера — как в журнале; не названа — `None`."""
    return None if order.side is None else _LISTED_SIDE[order.side]


def _listed_words(order: ListedOrder) -> str:
    """Живая заявка из списка брокера словами: сторона, остаток, цена."""
    side = _listed_side(order)
    volume = order.remained_quantity or order.order_quantity or "?"
    price = "" if order.price is None else f" по {order.price:g}"
    return f"{'?' if side is None else _HAND_SIDE[side]} {volume}{price}"


def hand_words(row: StoredCommand) -> str:
    """Заявка робота из журнала словами: сторона, объём, инструмент, цена, чья.

    Одни слова на список в окне, вопрос перед снятием и строку итога:
    человек подтверждает ровно то, что потом прочитает в журнале.
    """
    command = row.command
    side = "?" if command.side is None else _HAND_SIDE[command.side]
    price = "по рынку" if command.price is None else f"по {command.price:g}"
    placed = in_moscow(row.at).strftime("%d.%m %H:%M")
    return (
        f"{side} {command.volume or '?'} {command.symbol} {price}; заявка робота "
        f"«{command.order_id}», записана {placed} МСК"
    )


def _bars_words(count: int) -> str:
    """Число свечей с русским окончанием: «1 свечу», «3 свечи», «5 свечей»."""
    last, tens = count % 10, count % 100 // 10
    if tens != 1 and last == 1:
        return f"{count} свечу"
    if tens != 1 and last in (2, 3, 4):
        return f"{count} свечи"
    return f"{count} свечей"


def _foreign_text(text: str | None) -> str:
    """Текст из сети в нашу строку: без управляющих знаков, коротко."""
    if not text:
        return ""
    clean = _CONTROL.sub(" ", text).strip()
    return clean[:_FOREIGN_TEXT_LIMIT]


def _msk(moment: datetime) -> str:
    return f"{in_moscow(moment):%H:%M}"


def _age_words(age: timedelta) -> str:
    """Возраст минутки словами: «7 мин», «2 ч 5 мин»."""
    hours, minutes = divmod(max(int(age.total_seconds() // 60), 0), 60)
    return f"{hours} ч {minutes} мин" if hours else f"{minutes} мин"


def _already(order: OrderRequest) -> ExecutionRefused:
    return ExecutionRefused(
        f"Уровень {order.price or 0:g} уже сработал: исход выхода по нему придёт следующим. "
        "Второй выход не подаётся.",
        Refusal.ALREADY_TRIGGERED,
    )


def _touched(order: OrderRequest, minute: MarketCandle) -> bool:
    """Задет ли уровень этой минуткой — по стороне касания из заявки, не по позиции."""
    level = order.price
    if level is None:
        return False
    if order.touch is LevelTouch.RISE:
        return minute.high >= level
    return minute.low <= level


def _fok_tail(ticket: _Ticket) -> str:
    """Хвост слов статуса у FOK, кончившейся без исполнения (Р-6, правки 6.1 и 6.2).

    Пусто у всего остального: у дневной лимитной и у FOK с исполнением
    (такой не бывает — тогда слова статуса уже назвали исполненное).
    У выхода — позиция открыта, и тейка у неё нет: перед выходом по сигналу
    движок тейк снял, а сторож трейлинга снят вместе с касанием (6.2).
    """
    order = ticket.order
    if order.time_in_force is not TimeInForce.FILL_OR_KILL or not ticket.killed:
        return ""
    left = (
        " Позиция у брокера открыта без тейка — закройте её сами в приложении брокера."
        if ticket.closing
        else ""
    )
    return (
        f" Это заявка «исполнить целиком или снять» (FOK) по {order.price or 0:g}: целиком "
        "она не исполнилась, и позиция у брокера та, что была до неё."
        f"{left}"
    )


def _journal_status(ticket: _Ticket) -> CommandStatus | None:
    """Статус строки журнала по тому, что известно о заявке сейчас; `None` — писать нечего."""
    if ticket.status in _FINAL_IN_JOURNAL:
        return _FINAL_IN_JOURNAL[ticket.status]
    if ticket.answer is _Answer.REFUSED:
        return CommandStatus.REFUSED
    if ticket.status is not None or ticket.answer is _Answer.ACCEPTED:
        return CommandStatus.ACCEPTED
    if ticket.answer is _Answer.UNKNOWN:
        return CommandStatus.UNKNOWN
    return None


def _cancel_status(cancel: _Cancel, target: _Ticket) -> CommandStatus | None:
    """Статус строки отмены: «исполнена» — исходная успела, «снята» — снялась."""
    if cancel.answer is _Answer.REFUSED:
        return CommandStatus.REFUSED
    if target.status is OrderStatus.FILLED:
        return CommandStatus.FILLED
    if target.final:
        return CommandStatus.CANCELLED
    return {
        _Answer.ACCEPTED: CommandStatus.ACCEPTED,
        _Answer.UNKNOWN: CommandStatus.UNKNOWN,
    }.get(cancel.answer)


def _own_price(
    report: OrderReport, last: int, before: int, before_mean: float | None
) -> float | None:
    """Цена одного исполнения. Отдельного поля нет — есть средняя по заявке (план §2.10).

    Цена выводится из двух средних: `(средняя·всего − прежняя·прежде) / это`.
    Если накопленное не сходится с этим событием (потерянное событие, повтор
    не по порядку), — средняя как есть: неточная цена строки лучше выдуманной.
    """
    mean = report.average_price
    total = report.executed_quantity
    if mean is None:
        return report.price
    if total is not None and before + last == total and (before == 0 or before_mean is not None):
        return round((mean * total - (before_mean or 0.0) * before) / last, 6)
    return mean


# --------------------------------------------------------------- исполнитель


class LiveExecutor:
    """Боевая реализация порта исполнителя. Правила — в шапке модуля.

    Жизнь: `async with LiveExecutor(...)` — открыть поток статусов (с
    переподключением) и запись журнала в фоне; выход — закрыть. Подача
    последовательная: `submit`, `fills_at`, `watch` и сверка идут под одним
    замком, а поток статусов читается отдельной задачей и не ждёт никого
    («не блокируйте обработку входящих», `broker-api/28`).
    """

    def __init__(
        self, wiring: Wiring, instrument: Instrument, limits: LiveLimits | None = None
    ) -> None:
        self._session = wiring.session
        self._book = wiring.book
        self._minutes = wiring.minutes
        self._open_events: EventsOpener = wiring.events or (
            lambda: order_events(wiring.session)
        )
        self._clock = wiring.clock
        self._sleep = wiring.sleep
        self._on_news = wiring.on_news
        self._notice = wiring.notice
        self._band = wiring.band
        self._instrument = instrument
        self._limits = limits or LiveLimits()
        self._lock = asyncio.Lock()
        self._pulse = asyncio.Event()
        self._writes: asyncio.Queue[Callable[[], Awaitable[None]]] = asyncio.Queue()
        self._tasks: list[asyncio.Task[None]] = []
        self._stream: EventStream | None = None
        self._tickets: dict[str, _Ticket] = {}
        self._cancel_keys: dict[str, _Ticket] = {}
        self._takes: dict[str, _Take] = {}
        self._deals: list[_Deal] = []
        self._limit_exits: list[_LimitExit] = []
        self._unfilled: list[_Unfilled] = []
        #: Свеча последнего `fills_at` — только закрытая: её закрытие —
        #: «текущая» у заявок движка этой свечи (Р-2, путь А гейта 0.2).
        self._signal: MarketCandle | None = None
        self._halt: str | None = None
        self._flattened = False
        #: Позиция по нашим сведениям: последнее чтение портфеля, совпавшее
        #: с ожиданием движка, плюс каждое исполнение потока после него (наши
        #: и чужие). `None` — ни одного совпавшего чтения ещё не было. По ней
        #: «закрыть всё» проверяет, не отстал ли портфель (К2 `/risk` 01.11.2026).
        self._ledger: int | None = None
        #: Последняя сверка журнала не закончилась: судьба команды неизвестна.
        self._unsettled = False
        #: Подача после остановки разрешена: идёт сверка из 2в или «закрыть всё».
        #: Только эти два пути, и только пока они идут (`_permitted`, `B-077`).
        self._permit = False
        #: Сколько раз поток статусов открывался: по росту 2в узнаёт «связь
        #: вернулась» и сверяет позицию до следующей заявки (0017, 0020).
        self._links = 0

    # -- жизнь -----------------------------------------------------------------

    async def __aenter__(self) -> LiveExecutor:
        await self.start()
        return self

    async def __aexit__(self, *unused: object) -> None:
        await self.close()

    async def start(self) -> None:
        """Запустить запись журнала и поток статусов (с переподключением)."""
        if self._tasks:
            raise RuntimeError("боевой исполнитель уже запущен")
        self._tasks = [
            asyncio.create_task(self._write_loop(), name="live-executor-journal"),
            asyncio.create_task(self._supervise(), name="live-executor-stream"),
        ]

    async def close(self) -> None:
        """Закрыть поток и дописать журнал. Повторный вызов безвреден."""
        tasks, self._tasks = self._tasks, []
        if not tasks:
            return
        writer, supervisor = tasks
        supervisor.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await supervisor
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._writes.join(), timeout=self._limits.event_wait)
        left = self._writes.qsize()
        if left:
            log.error(
                "журнал команд брокеру не дописан при закрытии: %s записей не легли в базу — "
                "статусы и исполнения последних заявок сверьте с брокером",
                left,
            )
        writer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await writer

    @property
    def connected(self) -> bool:
        """Поток статусов открыт."""
        return self._stream is not None

    @property
    def halted(self) -> str | None:
        """Почему торговать нельзя — словами; `None` — можно."""
        return self._halt

    @property
    def undelivered(self) -> tuple[str, ...]:
        """Сделки исполнителя, ещё не отданные движку, — именами заявок движка.

        Вход исполнился у брокера, а движок узнает о нём только с показом бара
        исполнения: в это окно позиция у брокера уже есть, а у движка ещё нет
        (`/risk` 2в, замечание к С4).
        """
        return tuple(deal.name for deal in self._deals)

    @property
    def links(self) -> int:
        """Сколько раз поток статусов открылся. Рост после первого — связь вернулась."""
        return self._links

    async def wait_connected(self, timeout: float) -> bool:
        """Дождаться открытого потока статусов; `False` — не открылся за `timeout`."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self._stream is None:
            left = deadline - loop.time()
            if left <= 0:
                return False
            pulse = self._pulse
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(pulse.wait(), left)
        return True

    # -- порт ------------------------------------------------------------------

    async def submit(self, order: OrderRequest) -> None:
        """Подать заявку. Возврат — по контракту порта (`engine/ports.py`)."""
        async with self._lock:
            self._raise_if_halted()
            await self._sweep()
            self._raise_if_halted()
            # Действие движка → обработчик: таблица, а не четыре ветки (правило 9).
            action = {
                OrderAction.OPEN: self._open,
                OrderAction.CLOSE: self._close,
                OrderAction.ARM_TAKE_PROFIT: self._arm,
                OrderAction.CANCEL_TAKE_PROFIT: self._cancel,
            }[order.action]
            try:
                await action(order)
            except ExecutionRefused:
                await self._flush()
                raise
            except ExecutionHalted as halted:
                await self._flush()
                raise self._stop(halted.human, None) from None
            await self._flush()

    async def fills_at(self, candle: MarketCandle) -> Sequence[Fill | PriceSeen | LevelUnfilled]:
        """Накопленные сделки и события. Остановка уносит с собой всё (0066, §2.6).

        Закрытая свеча запоминается: её закрытие — «текущая» заявок, которые
        движок подаст по ней следом (`engine/ports.py`, «Когда подаются
        заявки свечи»). Незакрытая не запоминается: по ней движок не решает.
        """
        async with self._lock:
            if not getattr(candle, "unsettled", False):
                self._signal = candle
            await self._sweep()
            await self._flush()
            self._count_limit_exits(candle)
            if self._halt is not None:
                raise self._stop(self._halt, candle)
            return self._ready(candle)

    async def watch(self) -> None:
        """Записана минутка: проверить сторожей трейлинга. 2в зовёт на каждой минутке.

        Без этого вызова касание всё равно найдётся — на ближайшем `submit`
        или `fills_at`, то есть не позже закрытия свечи, но уже с опозданием.
        """
        async with self._lock:
            await self._sweep()
            await self._flush()

    # -- сверка ----------------------------------------------------------------

    async def broker_position(self) -> int:
        """Позиция у брокера по инструменту, свежим запросом. Отказ — `BrokerError`."""
        async with self._lock:
            return await self._position_now()

    def _undelivered(self) -> int:
        """На сколько контрактов (со знаком) позицию сдвинуло исполненное, чего движок не видел.

        Сверка после связи сравнивает брокера с движком, а движок узнаёт
        о сделке, только когда заявка кончилась (Р6). Учитывается **всё
        исполненное**: законченные, но не отданные сделки (тейк за обрыв)
        и исполненная часть незаконченных — тейка на 2, исполненного на 1,
        выхода с пределом, сработавшего сторожа (`B-080`). Без этого сверка
        объявила бы расхождение и закрыла бы всё на штатном частичном тейке.

        Зовётся только под замком исполнителя (`compare_position`): сторож,
        продавший между подсчётом и чтением портфеля, дал бы ложное
        «закрыть всё» (R2 `/risk` 01.11.2026).
        """
        self._collect_takes()
        shift = 0
        counted: set[int] = set()
        for deal in self._deals:
            done = deal.volume if deal.complete else sum(t.done_volume for t in deal.tickets)
            counted.update(id(ticket) for ticket in deal.tickets)
            shift += self._shift(deal.action, deal.side, done)
        for take in self._takes.values():
            ticket = take.ticket
            if ticket is not None and id(ticket) not in counted:
                shift += self._shift(OrderAction.CLOSE, take.order.side, ticket.done_volume)
        return shift

    @staticmethod
    def _shift(action: OrderAction, side: Side, volume: int) -> int:
        """Сдвиг позиции сделкой движка: вход прибавляет сторону, выход вычитает."""
        signed = signed_position(side, volume)
        return signed if action is OrderAction.OPEN else -signed

    def tell(self, text: str) -> None:
        """Сказать человеку без остановки — через `Wiring.notice` и строкой WARNING."""
        self._tell(text)

    @property
    def unsettled(self) -> bool:
        """Последняя сверка журнала не выяснила судьбу хоть одной команды."""
        return self._unsettled

    async def check_position(self, expected: int) -> int:
        """Позиция у брокера свежим запросом; совпала с `expected` — основа книги позиции."""
        async with self._lock:
            actual = await self._position_now()
            if actual == expected:
                self._ledger = actual
            return actual

    async def compare_position(self, expected: int) -> tuple[int, int, str | None]:
        """Сверка позиции после связи — ОДНОЙ операцией под замком (R2, решение 0020).

        Позиция робота — `expected` движка плюс исполненное, чего движок ещё
        не видел (`_undelivered`), и считается она в тот же миг, что читается
        портфель: сторож трейлинга, продавший по догруженной минутке, не
        вклинится между подсчётом и чтением. Совпало — основа позиции по
        сделкам. Не совпало — «закрыть всё» тут же, под тем же замком.

        :returns: позиция у брокера, позиция робота, текст остановки или `None`.
        :raises BrokerError: портфель не прочитан.
        """
        async with self._lock:
            robot = expected + self._undelivered()
            actual = await self._position_now()
            if actual == robot:
                self._ledger = actual
                return actual, robot, None
            why = (
                f"Сверка с брокером: у брокера {position_words(actual)}, "
                f"у робота {position_words(robot)}."
            )
            with self._permitted():
                return actual, robot, await self._flatten(why)

    async def close_everything(self, why: str) -> str:
        """Решение 0020 (0066): снять заявки, закрыть позицию заявкой FOK, остановиться.

        Закрывающая заявка — одна за жизнь исполнителя: после поданной второй
        вызов ничего не подаёт. Попытка, после которой заявка не ушла (цены
        нет, позиция не прочиталась), не считается.

        Остановка ставится **до** закрывающей заявки. Возврат — текст для журнала.
        """
        async with self._lock:
            with self._permitted():
                return await self._flatten(why)

    async def settle_open_commands(self) -> tuple[str, ...]:
        """Решение 0017: незакрытые команды журнала — статусом по ключу; живые — снять.

        Зовётся при старте и после связи (2в, `app/reconcile.py`). Строка
        `recorded` — не «не подана» (`D-177`): её судьбу решает брокер.
        """
        async with self._lock:
            # Команды другого счёта (второй токен в `userdata/`) сверяет не этот
            # сеанс: спросить о них брокера этим токеном значит спросить не тот счёт.
            account = self._instrument.account
            stored = [
                row for row in await self._book.open_commands() if row.command.account == account
            ]
            notes: list[str] = []
            self._unsettled = False
            with self._permitted():
                for row in stored:
                    if row.command.action is CommandAction.CREATE:
                        note = await self._settle_create(row)
                        if note is not None:
                            notes.append(note)
            for row in stored:
                if row.command.action is CommandAction.CANCEL:
                    self._settle_cancel(row)
            await self._flush()
            return tuple(notes)

    # -- руками владельца (задача владельца 13.11.2026) ------------------------

    async def cancel_by_hand(self, key: str, *, stop: bool) -> str:
        """«Снять заявку» из окна: одна незакрытая заявка робота этого счёта.

        Снимается только команда создания из журнала команд этого счёта:
        ручных заявок владельца в журнале нет, и программа их не видит.
        Подача после остановки разрешена (`_permitted`): ради этого кнопка
        и заведена — робот стоит, заявка висит.

        :param stop: исполнитель ведёт бой. Заявку, которую движок считает
            живой, снимает не он, — робот останавливается **до** снятия,
            тем же приёмом, что «закрыть всё» (`_flatten`): остановка без
            новости, новость — после, когда снятие кончилось.
        :returns: итог словами — в журнал решений.
        """
        async with self._lock:
            rows = {row.command.client_order_id: row for row in await self._robot_rows(None)}
            row = rows.get(key)
            if row is None:
                return (
                    "Этой заявки нет среди незакрытых заявок робота по этому счёту — "
                    "снимать нечего: она уже кончилась или подана не роботом."
                )
            if stop:
                self._hand_halt()
            with self._permitted():
                text = (await self._hand_cancel(row)).text
            await self._flush()
        if stop:
            self._news()
        return text

    async def close_by_hand(self, *, stop: bool, awaiting: int | None = None) -> tuple[str, bool]:
        """«Закрыть позицию» из окна: позиция, заявки робота по инструменту, закрытие FOK.

        Как «закрыть всё» (0020), но по команде владельца и при остановленном
        роботе тоже. Порядок и отличия — `_hand_close`.

        :param awaiting: закрывающая заявка уже подана этой кнопкой, и портфель
            обязан показать эту позицию; пока показывает другую — второе
            нажатие отказывает словами (`/risk` 13.11.2026, Р1). `None` — не ждём.
        :returns: итог словами и «закрывающая заявка ушла к брокеру».
        """
        async with self._lock:
            if stop:
                self._hand_halt()
            with self._permitted():
                known = set(self._tickets)
                text = await self._hand_close(awaiting)
                sent = any(
                    one.closing and one.answer is not _Answer.REFUSED
                    for key, one in self._tickets.items() if key not in known
                )
            await self._flush()
        if stop:
            self._news()
        return text, sent

    async def _hand_close(self, awaiting: int | None) -> str:
        """Ручное закрытие по шагам; каждый шаг против денег (`/risk` 13.11.2026).

        1. **Позиция — первой** (Р3): не прочиталась — ничего не трогать,
           иначе позиция осталась бы без тейка.
        2. Закрытие уже подано, а портфель ещё не показал его (`awaiting`) —
           отказ словами, ничего не трогать (Р1): второе закрытие по отставшему
           портфелю — шорт.
        3. Снять заявки робота **из журнала команд** (у исполнителя на одну
           команду памяти нет). Не снялась хоть одна — не закрывать: тейк,
           оставшийся у брокера, открыл бы позицию снова.
        4. Исполнившееся при снятии (`executedQuantity` по статусу) — в позицию
           по сделкам (`_ledger`): закрывающая заявка подаётся, только если
           два чтения портфеля показывают **остаток**; ноль — не подаётся (Р1).
        5. Живые заявки у брокера по инструменту, которых нет в журнале робота
           (ручные заявки владельца), — назвать, не снимая (Р2).

        «Уже пыталась» (`_flattened`) здесь не держит: второе нажатие — решение
        человека, а не программа, стреляющая закрытиями (С2).
        """
        try:
            before = await self._position_now()
        except BrokerError as error:
            return (
                f"Позицию у брокера прочитать не удалось ({error.human}): заявки робота "
                "не тронуты, закрывающая заявка не подана. Повторите позже."
            )
        if awaiting is not None and before != awaiting:
            return (
                "Закрытие уже подано, ждём портфель: брокер ещё показывает "
                f"{position_words(before)}, после закрытия ждём {position_words(awaiting)}. "
                "Заявки не тронуты, вторая закрывающая не подана."
            )
        notes: list[str] = []
        alive, shift = False, 0
        handled: list[StoredCommand] = []
        for row in await self._robot_rows(self._instrument.ticker):
            done = await self._hand_cancel(row)
            notes.append(done.text)
            alive = alive or not done.ended
            shift += done.shift
            handled.append(row)
        # Тейк, исполнившийся при снятии, — сделка: движок узнает о ней
        # вместе с остановкой (`_stop`), как у `_flatten`.
        self._collect_takes()
        self._takes.clear()
        if alive:
            notes.append(
                "Позиция не закрывается: заявка робота осталась у брокера и после "
                "закрытия исполнилась бы сама, открыв позицию заново. Снимите её "
                "в приложении брокера и нажмите «Закрыть позицию» ещё раз."
            )
        else:
            if shift:
                notes.append(
                    "При снятии заявки робота исполнились: позиция по сделкам — "
                    f"{position_words(before + shift)}; закрывается только остаток."
                )
            # Позиция по сделкам — свежее чтение плюс исполненное при снятии:
            # `_close_position` подаёт заявку, только если оба чтения портфеля
            # с ней согласны (отставший портфель — отказ словами, а не шорт).
            self._ledger = before + shift
            notes.append(await self._close_position())
        notes.append(await self._foreign_orders(handled))
        return " ".join(note for note in notes if note)

    async def _foreign_orders(self, handled: Sequence[StoredCommand]) -> str:
        """Живые заявки у брокера по инструменту, не найденные в журнале робота (Р2).

        Снимать их программа не вправе — решение владельца: только заявки
        робота. Но промолчать о них после «позиция закрыта» нельзя: такая
        заявка исполнится и откроет позицию снова. Связать строку списка
        с журналом можно только по стороне, цене и объёму: биржевого номера
        в журнале нет, поэтому и слова осторожные — «не найдены в журнале».
        Снятые сейчас заявки робота (`handled`) тоже свои: список брокера
        может ещё показывать их живыми.
        """
        ticker = self._instrument.ticker
        try:
            page = await self._session.search_orders(OrdersQuery(
                statuses=(ListedOrderStatus.ACTIVE,), tickers=(ticker,),
            ))
        except BrokerError as error:
            return (
                f"Список заявок у брокера не прочитан ({error.human}) — проверьте в "
                f"приложении брокера, нет ли по {ticker} живых заявок не робота."
            )
        ours = {
            (row.command.side, row.command.price, row.command.volume)
            for row in [*handled, *await self._robot_rows(ticker)]
        }
        foreign = [
            one for one in page.records
            if one.status is ListedOrderStatus.ACTIVE and one.ticker in (None, ticker)
            and (_listed_side(one), one.price, one.order_quantity) not in ours
        ]
        if not foreign:
            return ""
        listed = "; ".join(_listed_words(one) for one in foreign)
        more = "" if (page.total_pages or 1) <= 1 else " (показана первая страница списка)"
        return (
            f"У брокера по {ticker} живы заявки, не найденные в журнале робота: {listed}"
            f"{more} — они могут открыть позицию снова. Программа их не снимает."
        )

    async def _robot_rows(self, ticker: str | None) -> list[StoredCommand]:
        """Незакрытые команды создания этого счёта — заявки робота; `ticker` — по инструменту."""
        account = self._instrument.account
        return [
            row for row in await self._book.open_commands()
            if row.command.account == account
            and row.command.action is CommandAction.CREATE
            and (ticker is None or row.command.symbol == ticker)
        ]

    def _hand_halt(self) -> None:
        """Робот стоит, пока владелец действует руками. Прежняя причина остаётся."""
        if self._halt is None:
            self._halt = (
                "Владелец снимает заявки или закрывает позицию робота руками из окна. "
                f"{_HALT_TAIL}"
            )
            log.warning("боевой исполнитель остановлен: ручное действие владельца")

    async def _hand_cancel(self, row: StoredCommand) -> _HandCancel:
        """Снять одну заявку робота: слова, «у брокера её больше нет», сдвиг позиции.

        Сдвиг — исполненное по заявке с первого ответа о ней до конца снятия
        (`executedQuantity`): тейк, исполнившийся при снятии, — продажа,
        и закрывать после него надо остаток (`/risk` 13.11.2026, Р1).
        Исполненное до первого ответа уже в чтении позиции перед снятием.

        Статус по ключу — только чтобы не снимать кончившуюся. Брокер про
        статус не ответил — снятие уходит всё равно: подтвердит его поток
        (`_cancel_keys`), а неподтверждённое `_cancel_ticket` и так считает
        отказом (ревью 13.11.2026, находка 3).

        ⚠️ 404 по ключу у команды, ответа на подачу которой программа
        не получила (`recorded`, `unknown`), — не «заявки нет» (С1,
        `BrokerSession.order_status`): «у брокера её больше нет» здесь
        не утверждается, и позиция за ней не закрывается (находка 2, 0069).
        """
        ticket = self._tickets.get(row.command.client_order_id) or self._adopt(row)
        what = hand_words(row)
        answered = await self._ask(ticket)
        first = ticket.done_volume
        if answered and ticket.missing and ticket.status is None:
            if row.status in (CommandStatus.RECORDED, CommandStatus.UNKNOWN):
                return _HandCancel(
                    f"Заявку ({what}) брокер по ключу не находит, но ответа на её подачу "
                    "программа не получила, а «не найдена» сразу после потерянного ответа "
                    "не доказывает, что заявки нет. Снятие не подтверждено — проверьте "
                    "заявку в приложении брокера.",
                    ended=False,
                )
            ticket.answer = _Answer.REFUSED
            ticket.note = "снятие руками: брокер заявки с этим ключом не знает"
            self._sync(ticket)
            return _HandCancel(
                f"Заявки ({what}) у брокера нет — снимать нечего; в журнале команд она "
                "закрыта как не поданная.",
                ended=True,
            )
        if ticket.status is not None and ticket.final:
            return _HandCancel(
                f"Заявка ({what}) у брокера уже {STATUS_WORDS[ticket.status]}.", ended=True
            )
        try:
            await self._cancel_ticket(ticket)
        except (ExecutionRefused, ExecutionHalted) as error:
            text, ended = f"Заявку ({what}) снять не удалось: {error}", False
        else:
            done = "снята"
            if ticket.done_volume > first:
                done = f"исполнилась на {ticket.done_volume - first} при снятии, остаток снят"
            if ticket.status is OrderStatus.FILLED:
                done = "успела исполниться до снятия"
            text, ended = f"Заявка ({what}) {done}.", True
        filled = ticket.done_volume - first
        return _HandCancel(text, ended=ended, shift=signed_position(
            Side.LONG if ticket.order.side is OrderSide.BUY else Side.SHORT, filled
        ) if filled else 0)

    # -- действия --------------------------------------------------------------

    async def _open(self, order: OrderRequest) -> None:
        volume = self._contracts(order)
        current = self._signal_close(order)
        # Планка — до проверки позиции (`/risk` 10.11.2026, находка 1): портфель
        # читается последним перед подачей, а не за `band_wait` до неё.
        band = await self._fetch_band()
        await self._admit(order, expected=0)
        priced = self._fok(_OPENING[order.side], volume, current, "закрытие свечи сигнала", band)
        ticket = await self._send(order.order_id, _order_words(order), priced)
        self._deals.append(_Deal(
            order.order_id, OrderAction.OPEN, order.side, volume,
            level=False, moment=order.submitted_at, tickets=[ticket],
        ))
        await self._fill_market(ticket)

    async def _close(self, order: OrderRequest) -> None:
        """Выход движка: без предела — FOK от закрытия свечи сигнала; с пределом — Р-4."""
        volume = self._contracts(order)
        current = self._signal_close(order)
        if order.limit_price is not None:
            await self._close_limited(order, volume, current)
            return
        band = await self._fetch_band()  # до проверки позиции — как у `_open`
        await self._admit(order, expected=signed_position(order.side, volume))
        priced = self._fok(_CLOSING[order.side], volume, current, "закрытие свечи сигнала", band)
        ticket = await self._send(order.order_id, _order_words(order), priced, closing=True)
        self._deals.append(_Deal(
            order.order_id, OrderAction.CLOSE, order.side, volume,
            level=False, moment=order.submitted_at, tickets=[ticket],
        ))
        await self._fill_market(ticket)

    async def _close_limited(self, order: OrderRequest, volume: int, close: float) -> None:
        """Выход с пределом (по концу окна, после неисполненного уровня): DAY, Р-4.

        Планка спрашивается **до** проверки позиции: портфель читается
        последним перед подачей, а не за `band_wait` до неё.
        """
        price, words = await self._exit_price(order, close)
        await self._admit(order, expected=signed_position(order.side, volume))
        priced = _Priced(NewOrder(
            side=_CLOSING[order.side],
            order_type=OrderType.LIMIT,
            quantity=volume,
            ticker=self._instrument.ticker,
            class_code=self._instrument.class_code,
            price=price,
        ), words, close)
        reason = "" if order.exit_reason is None else f" — {order.exit_reason.label}"
        what = f"выход из {_GENITIVE[order.side]}{reason}"
        ticket = await self._send(order.order_id, what, priced, closing=True)
        deal = _Deal(
            order.order_id, OrderAction.CLOSE, order.side, volume,
            level=False, moment=order.submitted_at, tickets=[ticket],
        )
        self._deals.append(deal)
        await self._accepted(ticket)
        self._limit_exits.append(_LimitExit(order, deal, ticket))

    async def _arm(self, order: OrderRequest) -> None:
        """Вооружение — состояние: тот же уровень тем же механизмом — пустая операция."""
        take = self._takes.get(order.order_id)
        at_broker = (order.side, order.touch) in _AT_BROKER
        if take is not None:
            if take.fired:
                raise _already(take.order)
            if take.ticket is None and not at_broker:
                # Сторож двигается: новый уровень судит минутки с момента подачи.
                self._refuse_stale(order)
                take.order = order
                return
            same = take.ticket is not None and at_broker and take.order.price == order.price
            if same and take.ticket is not None and not take.ticket.final:
                # Тот же уровень и заявка у брокера жива. Кончившаяся без
                # исполнения — не «то же состояние»: после `B-079` так бывает
                # только при снятии не нами, а оно уже остановило робота.
                return
            # Проверки — ДО снятия прежнего: снятый уровень плюс отказ в новом
            # оставили бы позицию без тейка, а движок считал бы прежний живым.
            self._refuse_stale(order)
            await self._require_stream()
            await self._tear_down(take)
            del self._takes[order.order_id]
        if at_broker:
            await self._place_take(order)
            return
        self._refuse_stale(order)
        self._takes[order.order_id] = _Take(order, checked_from=order.submitted_at)

    async def _cancel(self, order: OrderRequest) -> None:
        """Возврат — сторожа больше нет. Сработал — `ALREADY_TRIGGERED`, а не возврат."""
        name = order.target_id or ""
        take = self._takes.get(name)
        if take is None:
            raise ExecutionRefused(
                f"Снимать нечего: уровня {name} у исполнителя нет.", Refusal.NO_SUCH_ORDER
            )
        if take.deal is not None:
            raise _already(take.order)
        if take.ticket is not None:
            self._refuse_stale(order)
            self._require_rights()
            await self._require_stream()
            await self._tear_down(take)
        del self._takes[name]

    async def _place_take(self, order: OrderRequest) -> None:
        """Неподвижный тейк — лимитная у брокера по цене заявки, как есть (Р1)."""
        volume = self._contracts(order)
        await self._admit(order, expected=None)
        request = NewOrder(
            side=_CLOSING[order.side],
            order_type=OrderType.LIMIT,
            quantity=volume,
            ticker=self._instrument.ticker,
            class_code=self._instrument.class_code,
            price=order.price,
        )
        words = f"по цене {order.price or 0:g}: уровень тейка движка, до конца дня"
        ticket = await self._send(
            order.order_id, f"тейк {_GENITIVE[order.side]}", _Priced(request, words)
        )
        self._takes[order.order_id] = _Take(order, ticket)
        await self._accepted(ticket)

    async def _tear_down(self, take: _Take) -> None:
        """Снять прежний механизм уровня. Исполнилось хоть что-то — довести, `ALREADY_TRIGGERED`."""
        ticket = take.ticket
        if ticket is None:
            return
        await self._cancel_ticket(ticket)
        if ticket.done_volume == 0:
            return
        await self._finish_take(take)
        raise _already(take.order)

    async def _finish_take(self, take: _Take) -> None:
        """Тейк исполнился (хотя бы частично): сделка движку одна, остаток — выходом (Р1, Р6)."""
        ticket = take.ticket
        assert ticket is not None, "довести можно только тейк у брокера"
        order = take.order
        volume = int(order.volume)
        if take.deal is None:
            take.deal = _Deal(
                order.order_id, OrderAction.CLOSE, order.side, volume,
                level=True, moment=ticket.first_at or self._clock(), tickets=[ticket],
            )
            self._deals.append(take.deal)
        rest = volume - ticket.done_volume
        if rest <= 0:
            return
        try:
            band = await self._fetch_band()  # до проверки позиции — как у `_open`
            await self._require_position(signed_position(order.side, rest), order)
            what = f"закрытие остатка тейка {_GENITIVE[order.side]}"
            priced = await self._fok_now(_CLOSING[order.side], rest, what, band)
            closing = await self._send(order.order_id, what, priced, closing=True)
            take.deal.tickets.append(closing)
            await self._fill_market(closing)
        except ExecutionRefused as refusal:
            raise self._halted_by(
                f"Тейк {_GENITIVE[order.side]} исполнился частично ({ticket.done_volume} "
                f"из {volume}), а остаток закрыть не удалось: {refusal} {_HALT_TAIL}"
            ) from None

    # -- проверки перед заявкой (план §2.2) ------------------------------------

    async def _admit(self, order: OrderRequest, *, expected: int | None) -> None:
        """Цепочка: свежесть → права → поток → позиция. Порядок — от дешёвого к запросу."""
        self._refuse_stale(order)
        self._require_rights()
        await self._require_stream()
        if expected is not None:
            await self._require_position(expected, order)

    def _freshness(self) -> timedelta:
        """Насколько решение может быть старше «сейчас»: свеча плюс порог (§2.2 п. 1)."""
        return timedelta(minutes=self._instrument.bar_minutes, seconds=self._limits.stale_after)

    def _refuse_stale(self, order: OrderRequest) -> None:
        now = self._clock()
        oldest = now - self._freshness()
        if order.submitted_at < oldest:
            raise ExecutionRefused(
                f"Заявка ({_order_words(order)}) решена в {_msk(order.submitted_at)}, а сейчас "
                f"{_msk(now)}: решение несвежее — похоже на догрузку после обрыва связи. "
                "К брокеру не подаётся."
            )

    def _require_rights(self) -> None:
        """Токен перечитывается из файла перед командой (`D-175`)."""
        try:
            self._session.require_trade_rights(reread=True)
        except BrokerError as error:
            raise self._halted_by(f"Заявка не подана: {error.human} {_HALT_TAIL}") from None

    async def _require_stream(self) -> None:
        stream = self._stream
        if stream is not None:
            try:
                await stream.alive()
            except BrokerError as error:
                log.warning("поток статусов не ответил на ping: %s", error.technical)
            else:
                return
        raise ExecutionRefused(
            "Нет связи с потоком статусов заявок: без него программа не узнает о сделке. "
            "Заявка не подана, сигнал пропущен."
        )

    async def _require_position(self, expected: int, order: OrderRequest) -> None:
        """Позиция у брокера свежим запросом. Расхождение — закрыть всё и остановиться."""
        actual = await self._read_position()
        if actual != expected:
            await asyncio.sleep(self._limits.position_settle)
            actual = await self._read_position()
        if actual == expected:
            self._ledger = actual
            return
        why = (
            f"Перед заявкой ({_order_words(order)}) у брокера {position_words(actual)}, "
            f"а робот считает {position_words(expected)}."
        )
        raise ExecutionHalted(await self._flatten(why))

    async def _read_position(self) -> int:
        try:
            return await self._position_now()
        except _TRANSIENT as error:
            raise ExecutionRefused(
                f"Позицию у брокера прочитать не удалось: {error.human} Заявка не подана."
            ) from None
        except BrokerError as error:
            raise self._halted_by(
                f"Позицию у брокера прочитать нельзя: {error.human} {_HALT_TAIL}"
            ) from None

    async def _position_now(self) -> int:
        payload = await self._session.read(PORTFOLIO_PATH)
        ticker = self._instrument.ticker
        # По одному тикеру (broker-expert 13.11.2026, B-096): строка срока
        # портфеля, а не сумма сроков; отказ — только по своему тикеру.
        mine = read_holdings(payload).position(ticker)
        quantity = mine.quantity if mine is not None else 0.0
        if not float(quantity).is_integer():
            raise UnexpectedAnswer(
                "позиция у брокера пришла дробным числом контрактов",
                f"портфель: {ticker} quantity {quantity!r}",
            )
        return int(quantity)

    # -- команды брокеру -------------------------------------------------------

    def _fok(
        self, side: OrderSide, volume: int, current: float, source: str, band: _BandAnswer
    ) -> _Priced:
        """Заявка FOK по планке биржи (владелец 10.11.2026, 0067).

        Покупка — по `HIGHLIMIT`, продажа — по `LOWLIMIT`, к сетке шага внутрь
        коридора. «Текущая ± %» за планкой биржа не примет: в трендовый день от
        расчётной цены до «+2 %» больше, чем до края коридора, и FOK
        отклонялась бы как раз на выходе из убыточной позиции (ревью
        10.11.2026, находка 1). Планка — первый вариант заказчика (0067,
        «по максимально возможной цене, заданной биржей»).

        Планки нет, она не пришла за `band_wait` или не охватывает «текущую» —
        FOK «текущая ± %» (`_fok_percent`), и слова называют, почему не планка.

        :param current: «текущая» цена — откуда она, говорит `source` (Р-2).
        :param band: ответ `_fetch_band`, полученный **до** проверки позиции
            (`/risk` 10.11.2026, находка 1): ожидание планки не встаёт между
            последним чтением портфеля и подачей.
        :raises ExecutionRefused: цену не посчитать — заявка не подана.
        """
        selling = side is OrderSide.SELL
        found, trouble = band
        if found is not None:
            edge, trouble = self._band_edge(found, current, selling=selling)
            if edge is not None:
                name, column = ("нижней", "LOWLIMIT") if selling else ("верхней", "HIGHLIMIT")
                words = (
                    f"по цене {edge:g}: по {name} планке биржи ({column}, {found.told}; "
                    f"текущая {current:g} — {source}), FOK"
                )
                return _Priced(self._fok_at(side, volume, edge), words, current)
        return self._fok_percent(
            side, volume, current, source, f"планка биржи не годится: {trouble}"
        )

    def _fok_percent(
        self, side: OrderSide, volume: int, current: float, source: str, why: str
    ) -> _Priced:
        """Заявка FOK «текущая ± %» (0067): цена от «текущей» и процента пределов.

        Запасной путь `_fok`: планки биржи нет. Цена — `engine.limit_beyond`:
        одно правило округления с пределом движка (Р-3), покупка — к сетке
        вверх, продажа — вниз, не ближе к рынку, чем процент. Саму заявку
        собирает `_fok_at`.

        :param current: «текущая» цена — откуда она, говорит `source` (Р-2).
        :param why: почему не по планке — хвостом слов.
        :returns: заявка и её цена словами — «по цене X: текущая Y (откуда) ± p %».
        :raises ExecutionRefused: цену не посчитать (текущая не больше нуля) —
            заявка не подана.
        """
        percent = self._limits.fok_percent
        buying = side is OrderSide.BUY
        try:
            price = limit_beyond(current, percent, self._instrument.price_step, buying=buying)
        except ValueError as error:
            raise ExecutionRefused(
                f"Цену заявки не посчитать ({error}): заявка не подана."
            ) from None
        sign = "+" if buying else "−"
        words = (
            f"по цене {price:g}: текущая {current:g} ({source}) {sign} {percent:g} %, FOK — "
            f"{why}"
        )
        return _Priced(self._fok_at(side, volume, price), words, current)

    def _fok_at(self, side: OrderSide, volume: int, price: float) -> NewOrder:
        """Единственный сборщик заявки немедленного исполнения: лимитная FOK по цене `price`.

        Все шесть мест, где прежде уходила рыночная заявка, — вход, выход
        движка, остаток тейка, выход по касанию трейлинга, «закрыть всё» —
        собирают заявку только здесь, и рыночной взяться неоткуда (гейт 0.2,
        «удалить `_market`»). Цену приносит вызывающий: планка биржи (`_fok`,
        `_fok_now`) или «текущая ± %» без планки (`_fok_percent`).
        """
        return NewOrder(
            side=side,
            order_type=OrderType.LIMIT,
            quantity=volume,
            ticker=self._instrument.ticker,
            class_code=self._instrument.class_code,
            price=price,
            time_in_force=TimeInForce.FILL_OR_KILL,
        )

    def _signal_close(self, order: OrderRequest) -> float:
        """«Текущая» заявки движка — закрытие свечи сигнала (Р-2, путь А гейта 0.2).

        Свеча сигнала — свеча последнего закрытого `fills_at`: заявки свечи T
        движок подаёт после `fills_at(T)`, и их `submitted_at` — закрытие T
        (`engine/ports.py`). Не сошлось — цены нет, и заявка не подаётся:
        цена от чужой свечи — это вход по цене, которой уже нет.

        :raises ExecutionRefused: заявка несвежая или свечи сигнала нет.
        """
        self._refuse_stale(order)
        signal = self._signal
        closes = None if signal is None else close_time(signal)
        if signal is None or closes != order.submitted_at:
            seen = (
                "закрытой свечи у исполнителя ещё не было"
                if closes is None
                else f"последняя закрытая свеча у исполнителя закрылась в {_msk(closes)}"
            )
            raise ExecutionRefused(
                f"Нет цены свечи сигнала: заявка ({_order_words(order)}) решена по свече, "
                f"закрывшейся в {_msk(order.submitted_at)}, а {seen}. Заявка не подана."
            )
        return float(signal.close)

    async def _fok_now(
        self, side: OrderSide, volume: int, what: str, band: _BandAnswer
    ) -> _Priced:
        """FOK «сейчас» — у «закрыть всё» и остатка тейка (0067, «Последняя минутка несвежая»).

        * Свежая минутка — `_fok` от её закрытия: по планке, нет планки — ± процент.
        * Несвежая (старше `_freshness`; после обрыва сверка идёт раньше
          догрузки минуток, гейт 0.2, 2.a) — FOK по планке биржи, к сетке
          внутрь коридора: от цены, которой уже нет, FOK снялась бы, а планка —
          граница, внутри которой биржа исполнит. Сторона от закрытия здесь
          не проверяется: несвежее закрытие — не та мера, которой судить
          коридор биржи.
        * Планки нет — FOK от несвежей минутки ± процент, в словах её возраст.
          Подать лучше, чем не подать (`/risk` 04.11.2026 22:35, находка 1):
          снятая FOK оставляет ту же позицию, что неподанная, и те же слова.

        :raises ExecutionRefused: ни минутки за сутки, ни годной планки —
            цены нет вовсе, заявка не подана.
        """
        now = self._clock()
        last, missing = await self._last_minute()
        if last is not None and last.time >= now - self._freshness():
            return self._fok(
                side, volume, float(last.close), f"закрытие минутки {_msk(last.time)}", band
            )
        stale = (
            missing if last is None
            else f"последняя минутка {_msk(last.time)} несвежая — ей {_age_words(now - last.time)}"
        )
        selling = side is OrderSide.SELL
        found, trouble = band  # получен до чтения позиции (`/risk` 10.11.2026, 1)
        if found is not None:
            edge, trouble = self._band_edge(found, None, selling=selling)
            if edge is not None:
                name, column = ("нижней", "LOWLIMIT") if selling else ("верхней", "HIGHLIMIT")
                words = (
                    f"по цене {edge:g}: по {name} планке биржи ({column}, {found.told}), FOK — "
                    f"{stale}"
                )
                return _Priced(self._fok_at(side, volume, edge), words)
        if last is None:
            raise ExecutionRefused(
                f"Цены для заявки ({what}) нет: {missing}, а планка биржи не годится — "
                f"{trouble}. Заявка не подана."
            )
        priced = self._fok_percent(
            side, volume, float(last.close),
            f"закрытие минутки {_msk(last.time)}, несвежей — ей {_age_words(now - last.time)}",
            f"планки нет: {trouble}",
        )
        # Цена несвежей минутки — цена, которой уже нет: исполнение по ней
        # не судится, строки «текущая была» нет (0067, владелец 10.11.2026).
        return priced._replace(current=None)

    async def _last_minute(self) -> tuple[MarketCandle | None, str]:
        """Последняя записанная минутка за сутки — или почему её нет, словами."""
        try:
            minutes = await self._minutes(self._clock() - _MINUTE_LOOKBACK)
        except _JOURNAL_FAILURES as error:
            return None, f"минутки не прочитались ({type(error).__name__})"
        if not minutes:
            return None, "за последние сутки не записано ни одной минутки"
        return max(minutes, key=lambda one: one.time), ""

    async def _exit_price(self, order: OrderRequest, close: float) -> tuple[float, str]:
        """Цена выхода с пределом (Р-4): планка биржи, иначе предел движка. И слова: откуда.

        Планка годна, если пришла за `band_wait`, обе границы названы и
        `LOWLIMIT < закрытие < HIGHLIMIT`. Продажа — по `LOWLIMIT`, покупка —
        по `HIGHLIMIT`, к сетке шага **внутрь** коридора (правка 4.3 гейта 0.2):
        округление от рынка вывело бы цену за коридор, и биржа отказала бы
        на каждом выходе. Иначе — предел движка как есть.

        :returns: цена и слова «по цене X: откуда она».
        """
        limit = order.limit_price
        assert limit is not None, "цена выхода с пределом — только у заявки с пределом"
        selling = _CLOSING[order.side] is OrderSide.SELL
        band, trouble = await self._fetch_band()
        if band is not None:
            edge, trouble = self._band_edge(band, close, selling=selling)
            if edge is not None:
                name, column = ("нижней", "LOWLIMIT") if selling else ("верхней", "HIGHLIMIT")
                return edge, (
                    f"по цене {edge:g}: по {name} планке биржи ({column}, {band.told}; "
                    f"закрытие свечи сигнала {close:g}), до конца дня; предел движка "
                    f"{limit:g} не подан"
                )
        return limit, (
            f"по цене {limit:g}: предел движка, до конца дня — планка биржи не годится: "
            f"{trouble}"
        )

    async def _fetch_band(self) -> tuple[PriceBand | None, str]:
        """Планка от источника — не дольше `band_wait`, обещанию источника не веря."""
        source = self._band
        if source is None:
            return None, "источник планки не подключён"
        waited = self._limits.band_wait
        try:
            return await asyncio.wait_for(source(), waited), ""
        except TimeoutError:
            return None, f"биржа не ответила за {waited:g} с"
        except _BAND_FAILURES as error:
            log.warning("планка биржи не получена: %s", type(error).__name__)
            return None, f"планку спросить не удалось ({type(error).__name__})"

    def _band_edge(
        self, band: PriceBand, close: float | None, *, selling: bool
    ) -> tuple[float | None, str]:
        """Граница планки на сетке шага внутрь коридора — или почему она не годится.

        `close` — закрытие, которое коридор обязан охватывать; `None` — сторона
        не проверяется (несвежая минутка, `_fok_now`), только сама планка.
        """
        low, high = band.low, band.high
        if not band.usable or low is None or high is None:
            return None, band.told
        if close is not None and not low < close < high:
            return None, (
                f"планка {low:g}–{high:g} не охватывает текущую цену {close:g}"
            )
        step = Decimal(repr(float(self._instrument.price_step)))
        raw = low if selling else high
        edge = float(to_grid(Decimal(repr(float(raw))), step, up=selling))
        reference = close if close is not None else (high if selling else low)
        if (selling and edge >= reference) or (not selling and edge <= reference):
            return None, (
                f"граница {raw:g}, положенная на сетку шага внутрь коридора, дошла до "
                f"{'закрытия свечи сигнала' if close is not None else 'другой границы'} "
                f"{reference:g}"
            )
        return edge, ""

    def _contracts(self, order: OrderRequest) -> int:
        volume = order.volume
        if not float(volume).is_integer() or volume < 1:
            raise self._halted_by(
                f"Объём {volume:g} — не целое число контрактов, боевой заявки такой не бывает. "
                f"{_HALT_TAIL}"
            )
        return int(volume)

    async def _record(self, command: BrokerCommand) -> None:
        """Строка журнала до отправки. Сбой базы — `_JournalDown`, команда не уходит."""
        try:
            await self._book.record(command)
        except _JOURNAL_FAILURES as error:
            log.error("журнал команд не записал %s: %s", command.client_order_id, error)
            raise _JournalDown(type(error).__name__) from None

    def _command_row(self, key: UUID, name: str, command: OrderCommand) -> BrokerCommand:
        """Команда сессии → строка журнала: что увидит брокер, а не намерение движка."""
        if isinstance(command, NewOrder):
            return BrokerCommand(
                client_order_id=str(key),
                action=CommandAction.CREATE,
                order_id=name,
                symbol=command.ticker,
                side=_JOURNAL_SIDE[command.side],
                volume=command.quantity,
                price=command.price,
                account=self._instrument.account,
            )
        return BrokerCommand(
            client_order_id=str(key),
            action=CommandAction.CANCEL,
            order_id=name,
            symbol=self._instrument.ticker,
            target_client_order_id=str(command.target),
            account=self._instrument.account,
        )

    async def _send(
        self,
        name: str,
        what: str,
        priced: _Priced,
        *,
        closing: bool = False,
        spared: bool = False,
    ) -> _Ticket:
        """Создать заявку: строка журнала до POST (С5), ответ HTTP → наш язык (§2.4).

        Заявка регистрируется **в записи**, до POST: события потока по ней
        приходят раньше ответа 200, и найти их хозяина обязан уже первый.
        Поэтому и `closing`, и `spared` ставятся здесь, а не после возврата:
        конец заявки может прийти потоком раньше.

        :param what: что за заявка словами — «вход в лонг», «тейк лонга».
        :param priced: заявка и её цена словами — «по цене X: текущая Y
            (откуда) ± p %». Слова обязательны: в журнал решений на **каждую** боевую заявку
            уходит строка «подана по цене…» (решение владельца 04.11.2026,
            0067), а в тексты отказов и остановок — цена поданной, а не предел
            движка (правка 4.2 гейта 0.2).
        """
        self._gate()
        made: list[_Ticket] = []
        request, words = priced.order, priced.words
        named = f"{what}, {words}"

        async def record(key: UUID, command: OrderCommand) -> None:
            await self._record(self._command_row(key, name, command))
            ticket = _Ticket(
                key=key, name=name, what=named, order=request, closing=closing, spared=spared,
                current=priced.current,
            )
            self._tickets[str(key)] = ticket
            made.append(ticket)

        try:
            await self._session.create_order(request, record=record)
        except asyncio.CancelledError:
            if made:
                self._unclear(made[0], "подача прервана отменой задачи после записи журнала")
            raise
        except BrokerError as error:
            raise self._refusal_of(error, made[0] if made else None, what) from None
        except _JournalDown as down:
            raise self._halted_by(
                f"Журнал команд не записал заявку ({what}), к брокеру она не ушла: "
                f"{down}. {_HALT_TAIL}"
            ) from None
        ticket = made[0]
        ticket.answer = _Answer.ACCEPTED
        self._sync(ticket)
        self._say(f"Заявка ({what}) подана {words}.")
        return ticket

    def _refusal_of(self, error: BrokerError, ticket: _Ticket | None, what: str) -> Exception:
        """Отказ пишущего пути → отказ или остановка порта (план §2.4)."""
        if ticket is None:
            if isinstance(error, _TRANSIENT):
                return ExecutionRefused(f"Заявка ({what}) не подана: {error.human}")
            return self._halted_by(f"Заявка ({what}) не подана: {error.human} {_HALT_TAIL}")
        if isinstance(error, _REFUSED_AFTER_RECORD):
            ticket.answer = _Answer.REFUSED
            ticket.note = error.technical
            self._sync(ticket)
            if isinstance(error, BadRequest):
                return ExecutionRefused(f"Брокер отклонил заявку ({what}): {error.human}")
            return self._halted_by(f"Брокер отклонил заявку ({what}): {error.human} {_HALT_TAIL}")
        unknown = error if isinstance(error, OutcomeUnknown) else OutcomeUnknown(error.technical)
        self._unclear(ticket, unknown.technical)
        return self._halted_by(halt_reason(unknown))

    def _unclear(self, ticket: _Ticket, note: str) -> None:
        """Исход команды неизвестен: строка — `unknown`, робот — стоит (`D-086`, `D-177`)."""
        ticket.answer = _Answer.UNKNOWN
        ticket.note = note
        self._sync(ticket)
        self._set_halt(halt_reason(OutcomeUnknown(note)))

    async def _send_cancel(self, ticket: _Ticket) -> None:
        """Запрос на снятие. Возврат — запрос принят **или** отклонён после записи.

        Снятой заявку делает только событие `4` по исходной (§2.7); отказ
        на запрос (400, 404) разбирается статусом исходной у вызывающего.
        """
        self._gate(cancelling=ticket)
        made: list[_Cancel] = []

        async def record(key: UUID, command: OrderCommand) -> None:
            await self._record(self._command_row(key, ticket.name, command))
            cancel = _Cancel(key)
            ticket.cancels.append(cancel)
            self._cancel_keys[str(key)] = ticket
            made.append(cancel)

        ticket.cancelling = True
        try:
            await self._session.cancel_order(ticket.key, record=record)
        except asyncio.CancelledError:
            if made:
                made[0].answer = _Answer.UNKNOWN
                self._sync(ticket)
                self._set_halt(halt_reason(OutcomeUnknown("снятие прервано отменой задачи")))
            raise
        except BrokerError as error:
            self._cancel_refused(error, ticket, made[0] if made else None)
            return
        except _JournalDown as down:
            ticket.cancelling = False
            raise self._halted_by(
                f"Журнал команд не записал снятие ({ticket.what}), к брокеру оно не ушло: "
                f"{down}. {_HALT_TAIL}"
            ) from None
        made[0].answer = _Answer.ACCEPTED
        self._sync(ticket)

    def _cancel_refused(self, error: BrokerError, ticket: _Ticket, cancel: _Cancel | None) -> None:
        """Отказ на снятие: до записи — не подано; 4xx — разбор статусом; прочее — стоп."""
        if cancel is None:
            ticket.cancelling = False
            if isinstance(error, _TRANSIENT):
                raise ExecutionRefused(f"Снятие ({ticket.what}) не подано: {error.human}") from None
            raise self._halted_by(
                f"Снятие ({ticket.what}) не подано: {error.human} {_HALT_TAIL}"
            ) from None
        if isinstance(error, (BadRequest, NotFound)):
            cancel.answer = _Answer.REFUSED
            self._sync(ticket)
            return
        cancel.answer = _Answer.UNKNOWN
        self._sync(ticket)
        unknown = error if isinstance(error, OutcomeUnknown) else OutcomeUnknown(error.technical)
        raise self._halted_by(halt_reason(unknown)) from None

    async def _cancel_ticket(self, ticket: _Ticket) -> None:
        """Снять и дождаться конца заявки. Не дождались — остановка: заявка жива."""
        if ticket.final:
            return
        await self._send_cancel(ticket)
        if await self._wait(ticket, _is_final, self._limits.cancel_wait):
            return
        await self._ask(ticket)
        if _is_final(ticket):
            return
        raise self._halted_by(
            f"Снятие заявки ({ticket.what}) не подтверждено за {self._limits.cancel_wait:g} с: "
            f"заявка у брокера, возможно, жива (ключ {ticket.key}). {_HALT_TAIL}"
        )

    async def _cancel_rest(self, ticket: _Ticket) -> bool:
        """Снять остаток своей заявки немедленного исполнения. `True` — заявка кончилась.

        Любой отказ здесь — не «не подано» (заявка у брокера есть), а «остаток,
        возможно, жив»: вызывающий называет это словами с ключом заявки.
        """
        try:
            await self._cancel_ticket(ticket)
        except (ExecutionRefused, ExecutionHalted) as error:
            log.warning("остаток заявки %s не снят: %s", ticket.key, error)
            return False
        return _is_final(ticket)

    # -- ожидание --------------------------------------------------------------

    async def _wait(self, ticket: _Ticket, done: Callable[[_Ticket], bool], limit: float) -> bool:
        """Ждать `done` не дольше `limit`. Ни одного события за `event_wait` — статус по ключу."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + limit
        asked = False
        while not done(ticket):
            left = deadline - loop.time()
            if left <= 0:
                return False
            quiet = not asked and ticket.status is None
            pulse = self._pulse
            try:
                await asyncio.wait_for(
                    pulse.wait(), min(left, self._limits.event_wait) if quiet else left
                )
            except TimeoutError:
                if quiet:
                    asked = True
                    await self._ask(ticket)
        return True

    async def _ask(self, ticket: _Ticket) -> bool:
        """Статус по ключу (§2.2 п. 8): всё, что брокер помнит о заявке, — в билет.

        `False` — брокер не ответил (сбой, связь); 404 — ответ: заявки нет.
        """
        try:
            state = await self._session.order_status(ticket.key)
        except BrokerError as error:
            log.warning("статус заявки %s не получен: %s", ticket.key, error.technical)
            return False
        if state is None:
            ticket.missing = True
            return True
        for report in state.reports:
            self._apply(ticket, report)
        if not ticket.final:
            ticket.status = state.latest.status
        self._after_change(ticket)
        return True

    async def _fill_market(self, ticket: _Ticket) -> bool:
        """Заявка немедленного исполнения (FOK): ждать `2`, `4` или `8`.

        `True` — исполнена целиком. `False` бывает только у заявки, чьё снятие
        целиком решает движок (`spared`, Р-10): кончилась без единого
        исполнения. Любой другой конец — остановка словами статуса (Р-6).
        Частичная без конца к пределу — страховка (0067: у FOK частичного
        остатка нет): остаток снимается, стоп (Р6).
        """
        if not await self._wait(ticket, _is_final, self._limits.fill_wait):
            await self._ask(ticket)
        if ticket.status is OrderStatus.FILLED:
            return True
        if ticket.spared and ticket.killed:
            return False
        if ticket.final or ticket.missing:
            raise self._halted_by(self._end_words(ticket))
        cancelled = await self._cancel_rest(ticket)
        if _is_filled(ticket):
            return True
        if cancelled and ticket.killed:
            # Ответа не дождались, сняли сами, ни контракта не исполнено:
            # позиция у брокера та же — не «частичная» (ревью 10.11.2026, 3).
            if ticket.spared:
                return False
            raise self._halted_by(self._unanswered_words(ticket))
        raise self._halted_by(self._partial_words(ticket, cancelled=cancelled))

    def _unanswered_words(self, ticket: _Ticket) -> str:
        """FOK без ответа за `fill_wait`, снятая программой, ни на контракт не исполнена."""
        return (
            f"Заявка ({ticket.what}, ключ {ticket.key}) не дошла до конца за "
            f"{self._limits.fill_wait:g} с и снята программой, не исполнившись ни на "
            "контракт: позиция у брокера та, что была до заявки."
            f"{_fok_tail(ticket)} {_HALT_TAIL}"
        )

    def _partial_words(self, ticket: _Ticket, *, cancelled: bool) -> str:
        """Частичная без конца: ключ, «исполнено N из M», судьба остатка (решение 01.11.2026).

        Исполненная часть движку сделкой не уходит — это нарушило бы Р6 (движок
        останавливается на сделке не на объём своей заявки), — поэтому она
        названа здесь словами: объём и цена.
        """
        done = ticket.done_volume
        price = ticket.mean_price()
        at = "" if price is None or not done else f" по {price:g}"
        rest = (
            "остаток снят"
            if cancelled
            else "остаток, возможно, жив у брокера — снимите его в приложении брокера"
        )
        return (
            f"Заявка ({ticket.what}, ключ {ticket.key}) исполнена на {done} из "
            f"{ticket.order.quantity}{at} и не дошла до конца за "
            f"{self._limits.fill_wait:g} с; {rest}. Исполненная часть движку не отдана "
            "(решение 0066): позиция у брокера не та, что у робота. "
            f"{_HALT_TAIL}"
        )

    async def _accepted(self, ticket: _Ticket) -> None:
        """Лимитная: дождаться, что биржа её знает (`0`, `1`, `2`). `8` и тишина — стоп."""
        if not await self._wait(ticket, _has_status, self._limits.fill_wait):
            await self._ask(ticket)
        if ticket.status is None or ticket.missing or ticket.status in (
            OrderStatus.REJECTED,
            OrderStatus.CANCELLED,
        ):
            raise self._halted_by(self._end_words(ticket))

    def _end_words(self, ticket: _Ticket) -> str:
        """Почему заявка кончилась не исполнением — словами (правило 13).

        У FOK, кончившейся без исполнения, к словам статуса — хвост
        `_fok_tail` (правка 6.1 гейта 0.2): у `8` остаются слова биржи —
        «цена вне лимитов» общими словами о снятой FOK пряталась бы.
        """
        if ticket.status is OrderStatus.REJECTED:
            reason = _foreign_text(ticket.reject) or "причину биржа не назвала"
            return (
                f"Биржа отклонила заявку ({ticket.what}) уже после приёма брокером: "
                f"«{reason}».{_fok_tail(ticket)} {_HALT_TAIL}"
            )
        if ticket.status is OrderStatus.CANCELLED:
            part = f", успев исполниться на {ticket.done_volume}" if ticket.done_volume else ""
            return (
                f"Заявка ({ticket.what}) снята не программой — биржей, брокером или руками "
                f"в приложении брокера{part}.{_fok_tail(ticket)} {_HALT_TAIL}"
            )
        if ticket.missing:
            return (
                f"Брокер принял заявку ({ticket.what}), а по её ключу {ticket.key} "
                f"не находит. {_HALT_TAIL}"
            )
        return (
            f"О заявке ({ticket.what}) брокер ничего не сообщил, и статус по ключу "
            f"{ticket.key} не получен. {_HALT_TAIL}"
        )

    # -- сторож трейлинга (Р2) -------------------------------------------------

    async def _sweep(self) -> None:
        """Все сторожа программы — по минуткам, записанным с прошлого раза.

        Сработавший, чей выход ещё не подан (связи не было), подаётся здесь же:
        это первый проход сторожа после связи.
        """
        for take in list(self._takes.values()):
            if take.ticket is not None:
                continue
            if take.deal is None:
                await self._sweep_one(take)
            elif not take.deal.tickets:
                await self._send_fire(take)

    async def _sweep_one(self, take: _Take) -> None:
        order = take.order
        since = take.checked_from or order.submitted_at
        try:
            minutes = await self._minutes(since)
        except _JOURNAL_FAILURES as error:
            self._fault(f"минутки для сторожа уровня не прочитались: {type(error).__name__}")
            return
        for minute in minutes:
            if minute.time < order.submitted_at:
                continue
            # Последняя минутка может ещё расти — она смотрится и в следующий раз.
            take.checked_from = minute.time
            if _touched(order, minute):
                await self._fire(take, minute.time)
                return

    async def _fire(self, take: _Take, moment: datetime) -> None:
        """Уровень задет: выход FOK ровно один раз; сделка движку — по свече касания.

        Касание в догруженной минутке (после обрыва) — тот же выход, сразу;
        исполнился — робот работает дальше (решение владельца 01.11.2026,
        0066), снят целиком — решает движок (`_send_fire`, 0067). С этой
        минуты уровень сработал: снятие и вооружение отвечают
        `ALREADY_TRIGGERED`, второго выхода не будет, даже если догрузка
        пришлёт те же минутки ещё раз.
        """
        order = take.order
        take.deal = _Deal(
            order.order_id, OrderAction.CLOSE, order.side, int(order.volume),
            level=True, moment=moment,
        )
        self._deals.append(take.deal)
        late = moment < self._clock() - self._freshness()
        log.info(
            "сторож %s: уровень %s задет в %s%s", order.order_id, order.price, moment,
            " (догруженная минутка)" if late else "",
        )
        await self._send_fire(take)

    async def _send_fire(self, take: _Take) -> None:
        """Подать выход сработавшего сторожа. Нет связи — не стоп: подаётся после связи.

        Отказ, после которого заявки у брокера точно нет и связь вернётся
        (поток мёртв, портфель не прочитался, команда не ушла), оставляет выход
        ждущим: его подаст следующий проход сторожа (`watch`, `fills_at`,
        `submit`). Отказ брокера на уже записанную команду и любая остановка —
        стоп: повтор той же заявки дал бы тот же отказ.

        Цена — FOK от уровня касания (0067). Граница «после обрыва» — порог
        свежести (`_freshness`) от минутки касания **в момент подачи**: так
        ловятся оба случая Р-2 — минутка догружена после обрыва и выход ждал
        связи. Касание вовремя и FOK снята — остановка (Р-6); после обрыва —
        событие движку (`_unfilled_level`, Р-10).
        """
        order = take.order
        deal = take.deal
        assert deal is not None, "подавать выход можно только сработавшему сторожу"
        if self._blocked():
            return
        if not self.connected:
            self._tell_waiting(take, "связи с потоком статусов заявок нет")
            return
        level = order.price or 0.0
        late = deal.moment < self._clock() - self._freshness()
        recorded = len(self._tickets)
        try:
            band = await self._fetch_band()  # до проверки позиции — как у `_open`
            await self._require_stream()
            await self._require_position(signed_position(order.side, deal.volume), order)
            priced = self._fok(_CLOSING[order.side], deal.volume, level, "уровень касания", band)
            ticket = await self._send(
                order.order_id,
                f"выход из {_GENITIVE[order.side]} по трейлингу {level:g}",
                priced,
                closing=True,
                spared=late,
            )
            deal.tickets.append(ticket)
            if not await self._fill_market(ticket):
                self._unfilled_level(take, ticket)
        except ExecutionRefused as refusal:
            if len(self._tickets) == recorded:
                self._tell_waiting(take, str(refusal))
                return
            self._set_halt(
                f"Цена дошла до уровня трейлинга {order.price or 0:g} в {_msk(deal.moment)}, а "
                f"выход брокер не принял: {refusal} {_HALT_TAIL}"
            )
        except ExecutionHalted as halted:
            self._set_halt(halted.human)

    def _tell_waiting(self, take: _Take, why: str) -> None:
        """Уровень пройден, выход ждёт: робот не стоит, но человек обязан знать (С2)."""
        if take.told or take.deal is None:
            return
        take.told = True
        order = take.order
        held = position_words(signed_position(order.side, take.deal.volume))
        self._tell(
            f"Цена прошла уровень трейлинга {order.price or 0:g} ({held}) "
            f"в {_msk(take.deal.moment)}, а выход подать нельзя: {why}. Выход ждёт "
            "связи и будет подан сразу после неё. Робот не остановлен; пока выхода нет, "
            "позиция открыта без защиты."
        )

    def _unfilled_level(self, take: _Take, ticket: _Ticket) -> None:
        """Выход после обрыва снят целиком: не остановка, а событие движку (0067, Р-10).

        Сделки касания не было — она уходит из отдачи. Сторож остаётся
        в `_takes` с отметкой «сработал»: вооружение и снятие этого имени
        отвечают `ALREADY_TRIGGERED`, пока событие не отдано (`_release`),
        иначе движок поставил бы старый уровень заново — и снова касание.
        Решение — держать, закрыть, закрыть и не входить — за движком
        (`engine.pipeline.apply_unfilled`); строка человеку — здесь.
        """
        deal = take.deal
        assert deal is not None, "событие — только у сработавшего сторожа"
        if deal in self._deals:
            self._deals.remove(deal)
        order = take.order
        held = position_words(signed_position(order.side, deal.volume))
        if ticket.status is OrderStatus.REJECTED:
            why = "биржа её отклонила"
        elif ticket.cancels:
            # Ответа не было за `fill_wait` — сняла программа (`/risk` 10.11.2026, 4).
            why = f"ответа о ней не было за {self._limits.fill_wait:g} с, и программа её сняла"
        else:
            why = "биржа её сняла"
        said = _foreign_text(ticket.reject)
        if said:
            why = f"{why}: «{said}»"
        reason = (
            f"Цена прошла уровень трейлинга {order.price or 0:g} ({held}) в "
            f"{_msk(deal.moment)}, а выход по нему подан только в {_msk(self._clock())} — "
            f"после обрыва связи. Выход ({ticket.what}) целиком не исполнился — {why}. "
            "Позиция у брокера та же, сторожа у неё больше нет; что делать дальше — "
            "решает робот по настройке «Заходить заново при обрыве связи» на ближайшей "
            "закрытой свече."
        )
        self._unfilled.append(_Unfilled(take, LevelUnfilled(order.order_id, deal.moment, reason)))
        self._tell(reason)
        self._news()

    # -- отдача сделок ---------------------------------------------------------

    def _ready(self, candle: MarketCandle) -> list[Fill | LevelUnfilled]:
        """Порядок отдачи (план §3): сначала по открытию; по незакрытой — только они.

        Сделки по уровню и события «уровень не исполнился» — только по закрытой
        свече и не в одном ответе с исполнениями по открытию (`engine/ports.py`,
        `fills_at`; Н4 гейта Р-10).
        """
        self._collect_takes()
        market = [
            deal for deal in self._deals
            if not deal.level and deal.complete and candle.time >= deal.moment
        ]
        if market:
            return list(self._deliver(market, candle))
        if getattr(candle, "unsettled", False):
            return []
        closes = close_time(candle)
        level = [
            deal for deal in self._deals if deal.level and deal.complete and closes > deal.moment
        ]
        return [*self._deliver(level, candle), *self._events_at(closes)]

    def _events_at(self, closes: datetime) -> list[LevelUnfilled]:
        """События, которые отдаются свечой, закрывшейся в `closes`.

        Только свечой, решения по которой ещё подаются брокеру: движок
        на событии рождает выход со временем закрытия этой свечи, и по свече
        догрузки (старше порога свежести) он был бы отвергнут как несвежий —
        позиция осталась бы без выхода. Событие ждёт первой свежей закрытой
        свечи; сторож всё это время отвечает `ALREADY_TRIGGERED`.
        """
        if closes < self._clock() - self._freshness():
            return []
        return self._release([one for one in self._unfilled if closes > one.event.at])

    def _release(self, due: list[_Unfilled]) -> list[LevelUnfilled]:
        """Отдать события: с ними уходит и сторож — дальше уровень за движком (Р-10)."""
        for one in due:
            self._unfilled.remove(one)
            name = one.take.order.order_id
            if self._takes.get(name) is one.take:
                del self._takes[name]
        return [one.event for one in due]

    def _collect_takes(self) -> None:
        """Тейк у брокера исполнился целиком сам — сделка по уровню."""
        for take in self._takes.values():
            ticket = take.ticket
            if ticket is None or take.deal is not None or ticket.status is not OrderStatus.FILLED:
                continue
            take.deal = _Deal(
                take.order.order_id, OrderAction.CLOSE, take.order.side, int(take.order.volume),
                level=True, moment=ticket.first_at or self._clock(), tickets=[ticket],
            )
            self._deals.append(take.deal)

    def _deliver(self, deals: list[_Deal], candle: MarketCandle | None) -> list[Fill]:
        fills: list[Fill] = []
        for deal in deals:
            self._deals.remove(deal)
            price = deal.price()
            if price is None:
                self._add_halt(
                    f"Сделка по заявке {deal.name} ({deal.action.value}, "
                    f"{_GENITIVE[deal.side]}, {deal.volume}) у брокера прошла, а брокер "
                    "не назвал цену исполнения: движку она не отдана, позиция у робота "
                    "не та, что на счёте. "
                    f"{_HALT_TAIL}"
                )
                continue
            fills.append(Fill(
                action=deal.action, side=deal.side, volume=float(deal.volume), price=price,
                at=self._bar_of(deal, candle), order_id=deal.name,
                commission=deal.commission(),
            ))
            take = self._takes.get(deal.name)
            if take is not None and take.deal is deal:
                del self._takes[deal.name]
        return fills

    def _stop(self, text: str, candle: MarketCandle | None) -> ExecutionHalted:
        """Остановка для движка: с ней — все исполненные и не отданные сделки.

        Без правил очерёдности фаз: свечей после остановки не будет, а сделка
        на счёте есть — движок обязан о ней узнать (`engine/ports.py`,
        `ExecutionHalted`). Исполненные по открытию — первыми, как в обычной
        отдаче. Неотданные события «уровень не исполнился» — тоже с ней,
        в `unfilled`: позиция у брокера открыта без сторожа, и движок обязан
        назвать это строкой журнала (`engine/ports.py`, `ExecutionHalted`).
        """
        self._collect_takes()
        ready = [deal for deal in self._deals if deal.complete]
        ready.sort(key=lambda deal: deal.level)
        fills = self._deliver(ready, candle)
        return ExecutionHalted(text, fills, unfilled=self._release(list(self._unfilled)))

    def _bar_of(self, deal: _Deal, candle: MarketCandle | None) -> datetime:
        """Начало свечи сделки: у FOK — свеча отдачи, как в модели; у уровня — свеча касания.

        Без свечи (остановка из `submit`) немедленная берёт момент подачи — это
        граница свечи исполнения, — а уровень считается от полуночи по Москве.
        """
        if not deal.level:
            if candle is not None and candle.time >= deal.moment:
                return candle.time
            return deal.moment
        if candle is not None:
            anchor = candle.time
            step = timedelta(minutes=candle.timeframe.minutes)
        else:
            anchor = in_moscow(deal.moment).replace(hour=0, minute=0, second=0, microsecond=0)
            step = timedelta(minutes=self._instrument.bar_minutes)
        return anchor + ((deal.moment - anchor) // step) * step

    def _count_limit_exits(self, candle: MarketCandle) -> None:
        """Выход по времени с пределом: не исполнился за N закрытых свечей — стоп (Р3, С9)."""
        if getattr(candle, "unsettled", False):
            return
        for waiting in list(self._limit_exits):
            if waiting.deal.complete or waiting.ticket.final:
                self._limit_exits.remove(waiting)
                continue
            if candle.time >= waiting.order.submitted_at:
                waiting.bars.add(candle.time)
            if len(waiting.bars) >= self._limits.limit_exit_bars and not waiting.reported:
                waiting.reported = True
                self._set_halt(self._limit_exit_words(waiting))

    @staticmethod
    def _limit_exit_words(waiting: _LimitExit) -> str:
        """Живая заявка у брокера — её цена и откуда она, а не предел движка (правка 4.2)."""
        ticket = waiting.ticket
        part = (
            f" Исполнено {ticket.done_volume} из {ticket.order.quantity}."
            if ticket.done_volume else ""
        )
        return (
            f"Заявка ({ticket.what}) не исполнилась за "
            f"{_bars_words(len(waiting.bars))}.{part} Она жива у брокера (ключ "
            f"{ticket.key}): сначала снимите её в приложении брокера, иначе она "
            f"исполнится без робота. {_HALT_TAIL}"
        )

    # -- закрыть всё (решение 0020, 0066) --------------------------------------

    async def _flatten(self, why: str) -> str:
        if self._halt is not None and self._blocked():
            # Робот уже стоит по другой причине, и человек, возможно, уже
            # действует руками: само по себе не закрываем (`B-077`). Закрыть
            # всё после остановки — только явной сверкой 2в (`close_everything`).
            return self._halt
        if self._flattened:
            text = (
                f"{why} Закрыть всё программа уже пыталась — второй раз не закрывает. "
                f"{_HALT_TAIL}"
            )
            self._halt = text
            return text
        # «Уже пыталась» ставит `_close_position`, и только когда закрывающая
        # заявка ушла к брокеру (`/risk` 04.11.2026 22:35, находка 1): попытка
        # без заявки — позиция не прочиталась, цены нет — не гасит кнопку
        # «Закрыть всё» до конца жизни исполнителя.
        # Остановка — ДО закрывающей заявки: ни одна заявка движка больше не уйдёт.
        self._halt = f"{why} Робот остановлен, позиция закрывается."
        with self._permitted():
            notes = await self._cancel_all()
            # Тейк, исполнившийся при снятии, — сделка, и движок о ней узнает
            # вместе с остановкой (`_stop`): забыть его вместе со сторожами нельзя.
            self._collect_takes()
            self._takes.clear()
            notes.append(await self._close_position())
        text = " ".join(
            [why, *notes, "Робот остановлен до вашего решения (решение 0020)."]
        )
        self._halt = text
        await self._flush()
        log.warning("закрыть всё: %s", text)
        self._news()
        return text

    async def _cancel_all(self) -> list[str]:
        notes: list[str] = []
        for ticket in list(self._tickets.values()):
            if ticket.final or ticket.answer is _Answer.REFUSED:
                continue
            try:
                await self._cancel_ticket(ticket)
            except (ExecutionRefused, ExecutionHalted) as error:
                notes.append(f"Заявку ({ticket.what}) снять не удалось: {error}")
                continue
            done = "успела исполниться" if ticket.status is OrderStatus.FILLED else "снята"
            notes.append(f"Заявка ({ticket.what}) {done}.")
        return notes

    async def _close_position(self) -> str:
        """Одна заявка FOK на всё, что у брокера, — только по согласным сведениям (С2, К2).

        Цена — `_fok_now`: от свежей минутки, иначе по планке биржи, иначе
        от несвежей минутки с её возрастом в словах.

        Заявка ушла к брокеру (записана и не отвергнута им точно) — второго
        «закрыть всё» за жизнь исполнителя не будет (`_flattened`): программа,
        стреляющая закрывающими заявками на каждой сверке, хуже остановленной
        (С2). Не ушла — следующая попытка разрешена.

        Объём — из двух чтений портфеля подряд, и оба обязаны совпасть между
        собой и с позицией по исполнениям потока (`_ledger`), если она известна.
        Портфель брокера отстаёт от потока сделок (насколько — первый боевой
        день), и продажа по отставшему чтению — шорт без сигнала. Не сошлось —
        заявка не подаётся, человек получает обе цифры.
        """
        # Планка — до чтений позиции (`/risk` 10.11.2026, находка 1): иначе
        # объём закрытия — из чтения за `band_wait` до подачи, и сделка,
        # сделанная руками за это время, превратила бы закрытие в шорт.
        band = await self._fetch_band()
        try:
            first = await self._position_now()
            await asyncio.sleep(self._limits.position_settle)
            position = await self._position_now()
        except BrokerError as error:
            return (
                f"Позицию у брокера прочитать не удалось ({error.human}) — закрывающая "
                "заявка не подана, закройте позицию сами."
            )
        doubt = self._disagreement(first, position)
        if doubt is not None:
            return doubt
        if position == 0:
            self._forget_unfilled()
            return "Позиции у брокера нет — закрывать нечего."
        side = OrderSide.SELL if position > 0 else OrderSide.BUY
        what = f"закрыть всё: {position_words(position)}"
        known = set(self._tickets)
        try:
            priced = await self._fok_now(side, abs(position), what, band)
            ticket = await self._send(
                f"flatten:{self._clock().isoformat()}", what, priced, closing=True
            )
            await self._fill_market(ticket)
        except (ExecutionRefused, ExecutionHalted) as error:
            return (
                f"Позицию ({position_words(position)}) закрыть не удалось: {error} "
                "Закройте её сами в приложении брокера."
            )
        finally:
            self._flattened = self._flattened or any(
                one.answer is not _Answer.REFUSED
                for key, one in self._tickets.items() if key not in known
            )
        return await self._closed_words(position, ticket)

    def _forget_unfilled(self) -> None:
        """Позиции у брокера нет — неотданные «уровень не исполнился» забыть.

        Событие несёт движку «позиция открыта и без сторожа», и с остановкой
        оно встало бы строкой ERROR «закройте её» рядом со словами «закрыта
        заявкой FOK» (ревью 10.11.2026, находка 2). Сторожа сняты в `_flatten`.
        """
        self._unfilled.clear()

    def _disagreement(self, first: int, second: int) -> str | None:
        """Почему объём закрытия не назван надёжно; `None` — два чтения и сделки согласны."""
        known = self._ledger
        if first == second and (known is None or known == second):
            return None
        by_deals = "неизвестна" if known is None else position_words(known)
        return (
            f"Позиция у брокера {position_words(second)} (чтением раньше — "
            f"{position_words(first)}), по сделкам программы — {by_deals}: не сходится, "
            "закрывающая заявка не подана. Проверьте позицию в приложении брокера "
            "и закройте её сами."
        )

    async def _closed_words(self, position: int, ticket: _Ticket) -> str:
        """«Закрыта» — только по исполненной заявке и нулевой позиции после неё."""
        price = ticket.mean_price()
        at = "" if price is None else f" по {price:g}"
        try:
            after = await self._position_now()
            if after != 0:
                await asyncio.sleep(self._limits.position_settle)
                after = await self._position_now()
        except BrokerError as error:
            return (
                f"Закрывающая заявка ({position_words(position)}) исполнена{at}, а позицию "
                f"после неё прочитать не удалось ({error.human}) — проверьте её сами."
            )
        known = self._ledger
        if after == 0 and (known is None or known == 0):
            self._forget_unfilled()
            return (
                f"Позиция у брокера ({position_words(position)}) закрыта заявкой FOK{at}; "
                "после закрытия позиции нет."
            )
        by_deals = "неизвестна" if known is None else position_words(known)
        return (
            f"Закрывающая заявка ({position_words(position)}) исполнена{at}, но у брокера "
            f"{position_words(after)}, по сделкам программы — {by_deals}. Проверьте "
            "позицию в приложении брокера."
        )

    # -- сверка журнала (решение 0017) -----------------------------------------

    async def _settle_create(self, row: StoredCommand) -> str | None:
        """Одна команда создания: судьба — по брокеру. Не выяснилась — остановка (С1).

        Заявку, которую исполнитель **сам ведёт** в этом запуске и считает
        живой (тейк у брокера, выход по времени с пределом), сверка не снимает
        (`B-079`): решение 0017 — про заявки с неизвестной судьбой и заявки
        прошлого запуска, а не про стоящий тейк. Для неё — только статус.
        """
        known = self._tickets.get(row.command.client_order_id)
        if known is not None and self._managed(known):
            return await self._refresh(known)
        ticket = known or self._adopt(row)
        try:
            return await self._settle_ticket(ticket)
        finally:
            # Что исполнено к концу сверки, учтено позицией; сверх этого — сделка без движка.
            ticket.known_volume = ticket.done_volume

    def _managed(self, ticket: _Ticket) -> bool:
        """Заявка этого запуска, которую исполнитель ведёт как живую: тейк или выход с пределом."""
        if ticket.answer is not _Answer.ACCEPTED or ticket.final or not ticket.bound:
            return False
        return any(take.ticket is ticket for take in self._takes.values()) or any(
            waiting.ticket is ticket for waiting in self._limit_exits
        )

    async def _refresh(self, ticket: _Ticket) -> str | None:
        """Статус своей живой заявки после связи — добор пропущенного, без снятия (`B-079`).

        Исполнилась за обрыв — сделка уйдёт движку обычным путём (`fills_at`);
        снята не нами — остановка словами (`_ended`); жива — ничего, а частично
        исполненная названа частично исполненной (`B-080`).
        """
        if not await self._ask(ticket):
            return self._unfinished(
                f"Сверка не закончена: статус заявки ({ticket.what}, ключ {ticket.key}) "
                f"брокер не сообщил, а программа ведёт её как живую. {_HALT_TAIL}"
            )
        if ticket.missing:
            return self._unfinished(
                f"Брокер не находит заявку ({ticket.what}, ключ {ticket.key}), которую "
                f"программа ведёт как живую. {_HALT_TAIL}"
            )
        if ticket.status is OrderStatus.FILLED:
            return (
                f"Заявка ({ticket.what}) исполнилась, пока связи не было: сделка уйдёт "
                "движку."
            )
        if ticket.final:
            return self._end_words(ticket)
        # Живая заявка: сказать один раз на состояние, а не на каждое
        # переоткрытие потока (Н5) — при частых обрывах это строка в секунду.
        state = (ticket.status, ticket.done_volume)
        if ticket.told_state == state:
            return None
        ticket.told_state = state
        part = (
            f" исполнена на {ticket.done_volume} из {ticket.order.quantity}, остаток"
            if ticket.done_volume
            else ""
        )
        return (
            f"Заявка ({ticket.what}) у брокера{part} жива — после связи её не трогали; "
            "движок получит одну сделку, когда заявка кончится."
        )

    async def _settle_ticket(self, ticket: _Ticket) -> str:
        await self._ask(ticket)
        if ticket.missing and ticket.status is None:
            ticket.answer = _Answer.REFUSED
            ticket.note = "сверка: брокер заявки с этим ключом не знает"
            self._sync(ticket)
            return f"Заявка ({ticket.what}) у брокера не найдена — считается не поданной."
        if ticket.status is None:
            return self._unfinished(
                f"Сверка не закончена: о заявке ({ticket.what}, ключ {ticket.key}) брокер "
                "не ответил. Она может быть жива и исполниться без робота. "
                f"{_HALT_TAIL}"
            )
        if ticket.final:
            return f"Заявка ({ticket.what}) у брокера {STATUS_WORDS[ticket.status]}."
        try:
            await self._cancel_ticket(ticket)
        except (ExecutionRefused, ExecutionHalted) as error:
            return self._unfinished(
                f"Сверка не закончена: заявку ({ticket.what}, ключ {ticket.key}) снять не "
                f"удалось ({error}). Она, возможно, жива у брокера — снимите её в "
                f"приложении брокера. {_HALT_TAIL}"
            )
        return f"Заявка ({ticket.what}) была жива у брокера и снята (решение 0017)."

    def _unfinished(self, text: str) -> str:
        """Сверка не выяснила судьбу команды: робот стоит, сверка не прошла (С1)."""
        self._unsettled = True
        return self._add_halt(text)

    def _adopt(self, row: StoredCommand) -> _Ticket:
        """Команда прошлого запуска → заявка, о которой исполнитель знает."""
        command = row.command
        side = OrderSide.BUY if command.side is CommandSide.BUY else OrderSide.SELL
        request = NewOrder(
            side=side,
            order_type=OrderType.MARKET if command.price is None else OrderType.LIMIT,
            quantity=command.volume or 1,
            ticker=command.symbol,
            class_code=self._instrument.class_code,
            price=command.price,
        )
        recorded = row.status in (CommandStatus.RECORDED, CommandStatus.UNKNOWN)
        ticket = _Ticket(
            key=UUID(command.client_order_id),
            name=command.order_id,
            what=f"из журнала: {command.order_id}",
            order=request,
            answer=_Answer.UNKNOWN if recorded else _Answer.ACCEPTED,
            journaled=row.status,
            bound=False,
        )
        self._tickets[command.client_order_id] = ticket
        return ticket

    def _settle_cancel(self, row: StoredCommand) -> None:
        target = self._tickets.get(row.command.target_client_order_id or "")
        if target is None or not target.final:
            return
        key = row.command.client_order_id
        if all(str(one.key) != key for one in target.cancels):
            target.cancels.append(_Cancel(UUID(key), _Answer.ACCEPTED, journaled=row.status))
        self._sync(target)

    # -- поток статусов --------------------------------------------------------

    async def _supervise(self) -> None:
        """Держать поток открытым. Обрыв — пауза по нарастающей и снова (`DOMAIN.md` §7)."""
        loop = asyncio.get_running_loop()
        failures = 0
        while True:
            opened_at: float | None = None
            try:
                async with self._open_events() as stream:
                    await self._recover()
                    self._stream = stream
                    self._links += 1
                    opened_at = loop.time()
                    self._notify()
                    waiting = any(
                        take.deal is not None and not take.deal.tickets
                        for take in self._takes.values()
                    )
                    if self._links > 1 or waiting:
                        # Связь вернулась: сверка (0017, 0020) — сразу, проходом
                        # по событию, а не на закрытии бара через 15 минут (Z1
                        # `/risk`): принудительное закрытие брокером за обрыв
                        # иначе узнали бы с опозданием до бара, а окно всё это
                        # время писало бы «заявки уходят брокеру». Тот же
                        # проход подаёт выход сторожа, ждавший связи (`watch`).
                        self._news()
                    async for event in stream:
                        self._on_event(event)
            except (BrokerError, OSError, RuntimeError) as error:
                log.warning("поток статусов заявок прервался: %s", _technical(error))
            finally:
                self._stream = None
                self._notify()
            # Пауза сбрасывается на первую, только если связь продержалась
            # `_LINK_SETTLE` (З1, З9). Иначе растёт до потолка: обрыв сразу после
            # открытия — продолжение прежнего, и сверок с чтением портфеля не
            # больше одной на паузу. Порог свежести (60 с) здесь не годился:
            # при обрывах раз в 50 с поток лежал бы треть времени.
            if opened_at is not None and loop.time() - opened_at >= _LINK_SETTLE:
                failures = 0
            pauses = self._limits.reconnect_pauses
            await self._sleep(pauses[min(failures, len(pauses) - 1)])
            failures += 1

    async def _recover(self) -> None:
        """После (пере)подключения: события за обрыв потеряны — добрать статусом (§5)."""
        for ticket in list(self._tickets.values()):
            if not ticket.final and ticket.answer is not _Answer.REFUSED:
                await self._ask(ticket)

    def _on_event(self, event: OrderEvent) -> None:
        ticket = self._ticket_of(event)
        if ticket is None:
            self._foreign(event)
            return
        self._apply(ticket, event.report)
        self._after_change(ticket)

    def _ticket_of(self, event: OrderEvent) -> _Ticket | None:
        """Хозяин события — по обоим ключам: какое поле несёт наш, нет в снимке (§2.9)."""
        for key in (event.client_order_id, event.original_client_order_id):
            if key is None:
                continue
            found = self._tickets.get(key) or self._cancel_keys.get(key)
            if found is not None:
                return found
        return None

    def _apply(self, ticket: _Ticket, report: OrderReport) -> None:
        """Сообщение о заявке → билет. Повтор исполнения (тот же `executionId`) — не второе."""
        execution_id = report.execution_id
        fresh = execution_id is not None and execution_id not in ticket.executions
        if report.is_execution and execution_id is not None and fresh:
            try:
                execution = self._execution_of(ticket, report, execution_id)
            except ValueError as error:
                self._fault(f"исполнение заявки {ticket.key} не разобрано: {error}")
            else:
                ticket.executions[execution_id] = execution
                ticket.first_at = ticket.first_at or execution.at
                self._count(ticket.order.side, execution.volume)
                self._writes.put_nowait(lambda: self._book.record_execution(execution))
        total = report.executed_quantity
        if total is None:
            total = sum(one.volume for one in ticket.executions.values())
        if total >= ticket.executed:
            ticket.executed = total
            if report.average_price is not None:
                ticket.average = report.average_price
        if report.reject_reason:
            ticket.reject = report.reject_reason
        if not ticket.final:
            ticket.status = report.status

    def _execution_of(
        self, ticket: _Ticket, report: OrderReport, execution_id: str
    ) -> BrokerExecution:
        last = report.last_quantity
        if last is None and report.executed_quantity is not None:
            last = report.executed_quantity - ticket.executed
        if last is None or last < 1:
            raise ValueError(f"исполнение {execution_id}: объём не назван")
        price = _own_price(report, last, ticket.executed, ticket.average)
        if price is None:
            raise ValueError(f"исполнение {execution_id}: цена не названа")
        return BrokerExecution(
            execution_id=execution_id,
            client_order_id=str(ticket.key),
            volume=last,
            price=price,
            at=report.transaction_time or self._clock(),
            commission=report.commission,
        )

    def _count(self, side: OrderSide, volume: int) -> None:
        """Исполнение — в позицию по нашим сведениям (покупка плюс, продажа минус)."""
        if self._ledger is not None:
            self._ledger += volume if side is OrderSide.BUY else -volume

    def _after_change(self, ticket: _Ticket) -> None:
        if not ticket.bound and ticket.done_volume > ticket.known_volume and not self._permit:
            # Заявка прошлого запуска исполнилась уже после сверки: сделки
            # движку по ней нет и не будет, позиция у брокера другая (С1).
            ticket.known_volume = ticket.done_volume
            self._add_halt(
                f"Заявка из журнала ({ticket.what}, ключ {ticket.key}) исполнилась на "
                f"{ticket.done_volume} — сделки движку по ней нет, позиция у брокера "
                f"не та, что у робота. {_HALT_TAIL}"
            )
        if ticket.final and not ticket.settled:
            ticket.settled = True
            self._ended(ticket)
        self._sync(ticket)
        self._notify()

    def _fill_words(self, ticket: _Ticket) -> str | None:
        """«Исполнена по X: текущая была Y, хуже/лучше текущей на N (p %)» (владелец 10.11.2026).

        FOK по планке биржи исполняется по лучшим ценам стакана, но на пустом
        стакане — до края коридора (`/risk` 10.11.2026, находка 2): цена
        исполнения против «текущей» видна строкой у каждой заявки, а не только
        ценой сделки. Покупка хуже — дороже текущей, продажа хуже — дешевле.
        `None` — «текущая» неизвестна или цены исполнения нет.
        """
        current = ticket.current
        price = ticket.mean_price()
        if current is None or price is None or current <= 0:
            return None
        points = round(price - current, 6)
        percent = points / current * 100
        if points == 0:
            how = "по текущей"
        else:
            worse = points > 0 if ticket.order.side is OrderSide.BUY else points < 0
            how = (
                f"{'хуже' if worse else 'лучше'} текущей на {abs(points):g} "
                f"({abs(percent):.2f} %)"
            )
        return (
            f"Заявка ({ticket.what}) исполнена по {price:g}: текущая была {current:g}, {how}."
        )

    def _ended(self, ticket: _Ticket) -> None:
        """Заявка кончилась. Исполнена — новость; снята не нами или отклонена — стоп.

        Кроме FOK, чьё снятие целиком решает движок (`spared`, Р-10): её конец
        разбирает ждущий её `_send_fire`, и остановки здесь нет.
        """
        if ticket.status is OrderStatus.FILLED:
            filled = self._fill_words(ticket)
            if filled is not None:
                self._say(filled)
            self._news()
            return
        if ticket.status is OrderStatus.CANCELLED and ticket.cancelling:
            return
        if ticket.spared and ticket.killed:
            return
        self._set_halt(self._end_words(ticket))

    def _foreign(self, event: OrderEvent) -> None:
        """Событие не по нашей заявке. Сделка по нашему инструменту — стоп (§8 №9)."""
        report = event.report
        ticker = self._instrument.ticker
        if report.ticker is not None and report.ticker != ticker:
            log.info("событие по чужому инструменту пропущено")
            return
        if not report.is_execution:
            log.info("событие по заявке, поданной не программой: %s", report.status.value)
            return
        volume = report.last_quantity or report.executed_quantity or 0
        price = report.average_price or report.price
        verb = "покупка" if report.side is OrderSide.BUY else "продажа"
        if price is not None and volume and report.execution_id:
            execution = BrokerExecution(
                execution_id=report.execution_id,
                client_order_id=event.key or f"exchange:{report.exchange_order_id or '-'}",
                volume=volume,
                price=price,
                at=report.transaction_time or self._clock(),
                commission=report.commission,
            )
            self._writes.put_nowait(lambda: self._book.record_execution(execution))
        if report.side is not None and volume:
            self._count(report.side, volume)
        at = "" if price is None else f" по {price:g}"
        self._set_halt(
            f"По {ticker} прошла сделка, которой программа не подавала: {verb} {volume}{at}. "
            "Похоже, заявка подана руками из приложения брокера — позиция у брокера "
            f"разошлась с роботом. {_HALT_TAIL}"
        )
        self._notify()

    # -- журнал и остановка ----------------------------------------------------

    def _sync(self, ticket: _Ticket) -> None:
        """Статусы строк журнала — в очередь потока базы; пишется то, что верно к записи."""
        self._writes.put_nowait(lambda: self._journal_sync(ticket))

    async def _journal_sync(self, ticket: _Ticket) -> None:
        wanted = _journal_status(ticket)
        if wanted is not None and wanted is not ticket.journaled:
            reason = ticket.note or _foreign_text(ticket.reject)
            await self._book.set_status(str(ticket.key), wanted, reason)
            ticket.journaled = wanted
        for cancel in ticket.cancels:
            status = _cancel_status(cancel, ticket)
            if status is not None and status is not cancel.journaled:
                await self._book.set_status(str(cancel.key), status, "")
                cancel.journaled = status

    async def _write_loop(self) -> None:
        while True:
            job = await self._writes.get()
            try:
                await job()
            except _JOURNAL_FAILURES as error:
                self._fault(f"журнал команд брокеру не принял запись: {type(error).__name__}")
                log.error("запись журнала команд: %s", error)
            finally:
                self._writes.task_done()

    async def _flush(self) -> None:
        """Дождаться записи журнала: сделка уходит движку после строк своих исполнений."""
        if self._tasks:
            await self._writes.join()

    def _notify(self) -> None:
        pulse, self._pulse = self._pulse, asyncio.Event()
        pulse.set()

    def _news(self) -> None:
        if self._on_news is not None:
            asyncio.get_running_loop().call_soon(self._on_news)

    def _set_halt(self, text: str) -> None:
        if self._halt is None:
            self._halt = text
            log.warning("боевой исполнитель остановлен: %s", text)
            self._news()

    def _halted_by(self, text: str) -> ExecutionHalted:
        """Остановка с этой причиной. Робот уже стоял по другой — назвать обе."""
        return ExecutionHalted(self._add_halt(text))

    def _add_halt(self, text: str) -> str:
        """Причина остановки; если робот уже стоял по другой — обе. Возврат — итоговый текст."""
        if self._halt is not None and text not in self._halt:
            text = f"{text} Раньше этого: {self._halt}"
            self._halt = text
            log.warning("боевой исполнитель: вторая причина остановки: %s", text)
        self._set_halt(text)
        return text

    def _tell(self, text: str) -> None:
        """Человеку — без остановки (С2): в журнал решений через 2в и в технический лог."""
        log.warning("боевой исполнитель: %s", text)
        self._notify_human(text, DecisionLevel.WARNING)

    def _say(self, text: str) -> None:
        """Штатная строка человеку — тем же путём в журнал решений; в техническом логе — INFO.

        Каждая боевая заявка (решение владельца 04.11.2026, 0067): по какой цене
        подана и откуда цена. Не предупреждение — потому и уровень лога ниже.
        """
        log.info("боевой исполнитель: %s", text)
        self._notify_human(text, DecisionLevel.INFO)

    def _notify_human(self, text: str, level: DecisionLevel) -> None:
        if self._notice is not None:
            asyncio.get_running_loop().call_soon(self._notice, text, level)

    def _fault(self, text: str) -> None:
        log.error("боевой исполнитель: %s", text)
        self._set_halt(f"Сбой боевого исполнителя: {text}. {_HALT_TAIL}")

    def _blocked(self) -> bool:
        """Подача запрещена: робот остановлен, и это не сверка и не «закрыть всё»."""
        return self._halt is not None and not self._permit

    def _gate(self, *, cancelling: _Ticket | None = None) -> None:
        """Единственная проверка перед командой брокеру (`B-077`).

        Через неё идут все создания (`_send`) и снятия (`_send_cancel`): после
        остановки к брокеру не уходит ничего — ни из `submit`, ни из `watch`
        и `fills_at`, ни ждущий выход сторожа. Человек, прочитавший «робот
        остановлен», действует руками, и любая заявка программы следом —
        вторая сделка поверх его. Исключения два: пути, разрешённые
        `_permitted` (сверка и «закрыть всё» из 2в), и **снятие** своей
        заявки немедленного исполнения (`cancelling`) — FOK или рыночной из
        журнала прошлых версий: остатка, который иначе исполнится без робота
        (решение владельца 01.11.2026; Р-7 и правка 7 гейта 0.2). Снятие
        позицию не создаёт; новых заявок это разрешение не открывает.

        ⚠️ Исключение — здесь, а не в `_blocked`: `_blocked` зовут ждущий
        выход сторожа и «закрыть всё», и ослабленный он разрешил бы после
        остановки новые заявки поверх ручных действий человека (`B-077`).
        """
        if cancelling is not None and _immediate(cancelling.order):
            return
        if self._halt is not None and self._blocked():
            raise ExecutionHalted(self._halt)

    @contextlib.contextmanager
    def _permitted(self) -> Iterator[None]:
        """Разрешить подачу после остановки на время сверки 2в или «закрыть всё»."""
        before, self._permit = self._permit, True
        try:
            yield
        finally:
            self._permit = before

    def _raise_if_halted(self) -> None:
        if self._halt is not None:
            raise self._stop(self._halt, None)


def _technical(error: BaseException) -> str:
    """Что писать в технический лог: у отказа брокера — его техническую часть, иначе тип."""
    return error.technical if isinstance(error, BrokerError) else type(error).__name__


def _immediate(order: NewOrder) -> bool:
    """Заявка немедленного исполнения: FOK или рыночная (Р-7)."""
    return order.time_in_force is TimeInForce.FILL_OR_KILL or order.order_type is OrderType.MARKET


def _is_final(ticket: _Ticket) -> bool:
    return ticket.final


def _has_status(ticket: _Ticket) -> bool:
    return ticket.status is not None


def _is_filled(ticket: _Ticket) -> bool:
    return ticket.status is OrderStatus.FILLED
