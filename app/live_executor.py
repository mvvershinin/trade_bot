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
* **Рыночные вход и выход** ждут конца заявки — `2` или `8` (0066, §2.6).
  Исполнения одной заявки складываются в **одну** сделку движку: объём —
  сумма, цена — средняя; частичного движок не видит (Р6). Не дошла до конца
  за предел — остаток снимается, `ExecutionHalted`.
* **Неподвижный тейк** — лимитная заявка у брокера по цене из заявки движка,
  как есть: к шагу её приводит движок (Р1). Снятие ждёт `4`. Снята не нами —
  остановка. Исполнилась частично к снятию — остаток закрывается рыночным
  выходом, движку одна сделка на весь объём и `ALREADY_TRIGGERED`.
* **Скользящий тейк** — сторож здесь, в программе (Р2). Касание судится по
  high/low **записанных минуток**, уровнем, поданным не позже начала минутки, —
  зеркало `submitted_at > ready_at` модели (`backtest/execution.py::_fire`).
  На касании — рыночный выход; движку — сделка по закрытой свече касания.
  Касание в минутке, догруженной после обрыва, — тот же выход сразу после
  связи, робот не встаёт (решение владельца 01.11.2026, 0066).
* **Механизм тейка выбирается по паре (сторона позиции, касание) из заявки**,
  а не по режиму и не по галочке: «лонг, снизу» и «шорт, сверху» — уровень
  впереди рынка, лимитная у брокера; две другие пары — уровень позади рынка,
  то есть стоп, и лимитной его выразить нельзя: она исполнилась бы сразу
  (план §2.3). Запрет держит таблица `_AT_BROKER`, а не окно.
* **Выход по времени с пределом** (Р3) — лимитная; не исполнилась за
  `limit_exit_bars` закрытых свечей — остановка, текст называет живую заявку
  у брокера и её ключ (С9).
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
from typing import Final, Protocol
from uuid import UUID

from app.fact import LiveLimits, position_words, signed_position
from app.halt import halt_reason
from broker.account import parse_positions
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
    NewOrder,
    OrderCommand,
    OrderEvent,
    OrderReport,
    OrderSide,
    OrderStatus,
    OrderType,
)
from broker.session import PORTFOLIO_PATH, BrokerSession
from broker.tokens import now_utc
from engine import close_time, in_moscow
from engine.contracts import Fill, LevelTouch, OrderAction, OrderRequest, PriceSeen, Side
from engine.ports import ExecutionHalted, ExecutionRefused, MarketCandle, Refusal
from market import MarketWorker
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


# --------------------------------------------------------------- настройки


@dataclass(frozen=True, slots=True)
class Instrument:
    """Где и чем торгует исполнитель.

    :param account: счёт (`agreementId`), которому подаются команды. Пишется
        в каждую строку журнала: в `userdata/` бывает два токена на два счёта,
        и день счёта отбирает свои исполнения по нему (`market/day.py`).
    :param bar_minutes: размер свечи — для свежести заявки (§2.2 п. 1).
    """

    ticker: str
    class_code: str
    bar_minutes: int
    account: str


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
    #: и окно. Не подключено — остаётся строка WARNING технического лога.
    notice: Callable[[str], None] | None = None


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
    executions: dict[str, BrokerExecution] = field(default_factory=dict)
    cancels: list[_Cancel] = field(default_factory=list)

    @property
    def final(self) -> bool:
        return self.status in FINAL_STATUSES

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
    `moment` — у рыночной момент подачи (`submitted_at`): сделка не раньше
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


def _already(order: OrderRequest) -> ExecutionRefused:
    return ExecutionRefused(
        f"Уровень {order.price or 0:g} уже сработал: сделка по нему придёт следующей. "
        "Выход по рынку не подаётся.",
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

    async def fills_at(self, candle: MarketCandle) -> Sequence[Fill | PriceSeen]:
        """Накопленные сделки. Остановка уносит с собой всё исполненное (0066, §2.6)."""
        async with self._lock:
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
        """Решение 0020 (0066): снять заявки, закрыть позицию по рынку, остановиться.

        Один раз за жизнь исполнителя: второй вызов ничего не подаёт.
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

    # -- действия --------------------------------------------------------------

    async def _open(self, order: OrderRequest) -> None:
        volume = self._contracts(order)
        await self._admit(order, expected=0)
        ticket = await self._send(
            order.order_id, _order_words(order), self._market(_OPENING[order.side], volume)
        )
        self._deals.append(_Deal(
            order.order_id, OrderAction.OPEN, order.side, volume,
            level=False, moment=order.submitted_at, tickets=[ticket],
        ))
        await self._fill_market(ticket)

    async def _close(self, order: OrderRequest) -> None:
        volume = self._contracts(order)
        await self._admit(order, expected=signed_position(order.side, volume))
        limit = order.limit_price
        request = NewOrder(
            side=_CLOSING[order.side],
            order_type=OrderType.MARKET if limit is None else OrderType.LIMIT,
            quantity=volume,
            ticker=self._instrument.ticker,
            class_code=self._instrument.class_code,
            price=limit,
        )
        ticket = await self._send(order.order_id, _order_words(order), request)
        deal = _Deal(
            order.order_id, OrderAction.CLOSE, order.side, volume,
            level=False, moment=order.submitted_at, tickets=[ticket],
        )
        self._deals.append(deal)
        if limit is None:
            await self._fill_market(ticket)
            return
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
        ticket = await self._send(order.order_id, f"тейк {_GENITIVE[order.side]}", request)
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
            await self._require_position(signed_position(order.side, rest), order)
            closing = await self._send(
                order.order_id,
                f"закрытие остатка тейка {_GENITIVE[order.side]}",
                self._market(_CLOSING[order.side], rest),
            )
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
        positions, _ = parse_positions(payload)
        ticker = self._instrument.ticker
        quantity = sum(one.quantity for one in positions if one.ticker == ticker)
        if not float(quantity).is_integer():
            raise UnexpectedAnswer(
                "позиция у брокера пришла дробным числом контрактов",
                f"портфель: {ticker} quantity {quantity!r}",
            )
        return int(quantity)

    # -- команды брокеру -------------------------------------------------------

    def _market(self, side: OrderSide, volume: int) -> NewOrder:
        return NewOrder(
            side=side,
            order_type=OrderType.MARKET,
            quantity=volume,
            ticker=self._instrument.ticker,
            class_code=self._instrument.class_code,
        )

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

    async def _send(self, name: str, what: str, request: NewOrder) -> _Ticket:
        """Создать заявку: строка журнала до POST (С5), ответ HTTP → наш язык (§2.4).

        Заявка регистрируется **в записи**, до POST: события потока по ней
        приходят раньше ответа 200, и найти их хозяина обязан уже первый.
        """
        self._gate()
        made: list[_Ticket] = []

        async def record(key: UUID, command: OrderCommand) -> None:
            await self._record(self._command_row(key, name, command))
            ticket = _Ticket(key=key, name=name, what=what, order=request)
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
        """Снять остаток своей рыночной заявки. `True` — заявка кончилась (снята или исполнена).

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

    async def _fill_market(self, ticket: _Ticket) -> None:
        """Рыночная: ждать `2` или `8`. Без конца к пределу — снять остаток и стоп (Р6)."""
        if not await self._wait(ticket, _is_final, self._limits.fill_wait):
            await self._ask(ticket)
        if ticket.status is OrderStatus.FILLED:
            return
        if ticket.final or ticket.missing:
            raise self._halted_by(self._end_words(ticket))
        cancelled = await self._cancel_rest(ticket)
        if _is_filled(ticket):
            return
        raise self._halted_by(self._partial_words(ticket, cancelled=cancelled))

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
        """Почему заявка кончилась не исполнением — словами (правило 13)."""
        if ticket.status is OrderStatus.REJECTED:
            reason = _foreign_text(ticket.reject) or "причину биржа не назвала"
            return (
                f"Биржа отклонила заявку ({ticket.what}) уже после приёма брокером: "
                f"«{reason}». {_HALT_TAIL}"
            )
        if ticket.status is OrderStatus.CANCELLED:
            part = f", успев исполниться на {ticket.done_volume}" if ticket.done_volume else ""
            return (
                f"Заявка ({ticket.what}) снята не программой — биржей, брокером или руками "
                f"в приложении брокера{part}. {_HALT_TAIL}"
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
        """Уровень задет: рыночный выход ровно один раз; сделка движку — по свече касания.

        Касание в догруженной минутке (после обрыва) — тот же выход, сразу,
        и робот работает дальше (решение владельца 01.11.2026, 0066). С этой
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
        """
        order = take.order
        deal = take.deal
        assert deal is not None, "подавать выход можно только сработавшему сторожу"
        if self._blocked():
            return
        if not self.connected:
            self._tell_waiting(take, "связи с потоком статусов заявок нет")
            return
        recorded = len(self._tickets)
        try:
            await self._require_stream()
            await self._require_position(signed_position(order.side, deal.volume), order)
            ticket = await self._send(
                order.order_id,
                f"выход из {_GENITIVE[order.side]} по трейлингу {order.price or 0:g}",
                self._market(_CLOSING[order.side], deal.volume),
            )
            deal.tickets.append(ticket)
            await self._fill_market(ticket)
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
            f"в {_msk(take.deal.moment)}, а выход по рынку подать нельзя: {why}. Выход ждёт "
            "связи и будет подан сразу после неё. Робот не остановлен; пока выхода нет, "
            "позиция открыта без защиты."
        )

    # -- отдача сделок ---------------------------------------------------------

    def _ready(self, candle: MarketCandle) -> list[Fill]:
        """Порядок отдачи (план §3): сначала рыночные; по незакрытой — только они."""
        self._collect_takes()
        market = [
            deal for deal in self._deals
            if not deal.level and deal.complete and candle.time >= deal.moment
        ]
        if market:
            return self._deliver(market, candle)
        if getattr(candle, "unsettled", False):
            return []
        closes = close_time(candle)
        level = [
            deal for deal in self._deals if deal.level and deal.complete and closes > deal.moment
        ]
        return self._deliver(level, candle)

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
        `ExecutionHalted`). Рыночные — первыми, как в обычной отдаче.
        """
        self._collect_takes()
        ready = [deal for deal in self._deals if deal.complete]
        ready.sort(key=lambda deal: deal.level)
        return ExecutionHalted(text, self._deliver(ready, candle))

    def _bar_of(self, deal: _Deal, candle: MarketCandle | None) -> datetime:
        """Начало свечи сделки: у рыночной — свеча отдачи, как в модели; у уровня — свеча касания.

        Без свечи (остановка из `submit`) рыночная берёт момент подачи — это
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
        ticket = waiting.ticket
        part = (
            f" Исполнено {ticket.done_volume} из {ticket.order.quantity}."
            if ticket.done_volume else ""
        )
        return (
            f"Заявка на {_order_words(waiting.order)} не исполнилась за "
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
        self._flattened = True
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
        """Один рыночный ордер на всё, что у брокера, — только по согласным сведениям (С2, К2).

        Объём — из двух чтений портфеля подряд, и оба обязаны совпасть между
        собой и с позицией по исполнениям потока (`_ledger`), если она известна.
        Портфель брокера отстаёт от потока сделок (насколько — первый боевой
        день), и продажа по отставшему чтению — шорт без сигнала. Не сошлось —
        заявка не подаётся, человек получает обе цифры.
        """
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
            return "Позиции у брокера нет — закрывать нечего."
        side = OrderSide.SELL if position > 0 else OrderSide.BUY
        what = f"закрыть всё: {position_words(position)}"
        try:
            ticket = await self._send(
                f"flatten:{self._clock().isoformat()}", what, self._market(side, abs(position))
            )
            await self._fill_market(ticket)
        except (ExecutionRefused, ExecutionHalted) as error:
            return (
                f"Позицию ({position_words(position)}) закрыть не удалось: {error} "
                "Закройте её сами в приложении брокера."
            )
        return await self._closed_words(position, ticket)

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
            return (
                f"Позиция у брокера ({position_words(position)}) закрыта по рынку{at}; "
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

    def _ended(self, ticket: _Ticket) -> None:
        """Заявка кончилась. Исполнена — новость; снята не нами или отклонена — стоп."""
        if ticket.status is OrderStatus.FILLED:
            self._news()
            return
        if ticket.status is OrderStatus.CANCELLED and ticket.cancelling:
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
        if self._notice is not None:
            asyncio.get_running_loop().call_soon(self._notice, text)

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
        рыночной заявки (`cancelling`) — остатка, который иначе исполнится
        без робота (решение владельца 01.11.2026). Снятие позицию не создаёт;
        новых заявок это разрешение не открывает.
        """
        if cancelling is not None and cancelling.order.order_type is OrderType.MARKET:
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


def _is_final(ticket: _Ticket) -> bool:
    return ticket.final


def _has_status(ticket: _Ticket) -> bool:
    return ticket.status is not None


def _is_filled(ticket: _Ticket) -> bool:
    return ticket.status is OrderStatus.FILLED
