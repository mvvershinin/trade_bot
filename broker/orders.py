"""Заявки у БКС: что программа посылает брокеру и что слышит о заявке в ответ.

Здесь только **типы и разбор**, без сети. Отправляет `broker/session.py`
(`create_order`, `cancel_order`, `order_status`, `search_orders`,
`search_trades`); поток статусов заявок (шаг 2 плана Э4, страница 24 снимка)
разбирает свои события функцией `parse_order_event` отсюда же. Ничто
в продукте этот модуль пока не вызывает: боевой исполнитель в `app/` —
шаг 2 (`.docs/plans/E4-live-orders.md` §11).

Источник каждого кода и поля — снимок документации `.docs/broker-api/`:
11 (статус по ключу), 12 (отмена), 13 (создание), 15 (список заявок),
16 (список сделок), 24 (поток статусов). С сайтом не сверено, **живьём
не проверено ни одно поле**: токена с правами торговли в разработке нет.

Правила разбора — общие для слоя (`broker/parsing.py`) плюс два своих.

1. **Код — по таблице; незнакомый код — отказ разбора, а не подмена.**
   Состояние заявки, вид события, сторона и тип заявки приходят кодами.
   Код, которого нет в таблице, не становится ни «неизвестно», ни ближайшим
   знакомым: «Отклонена», прочитанная как «Новая», — это вход, которого
   движок будет ждать вечно (план §2.6). Отказ останавливает разбор вслух.
2. **Испорченное число — отказ, а не пустое поле.** Поля нет — `None`;
   поле есть, но в нём не число, дробь в количестве или `NaN` — отказ.
   Пустое значение вместо испорченного — та же подмена.
"""

from __future__ import annotations

import enum
import math
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final, TypeVar
from uuid import UUID

from broker.errors import UnexpectedAnswer
from broker.parsing import aware_moment, number, pick, text

# --- коды ---


class OrderSide(enum.Enum):
    """Сторона **сделки** (13, 11, 24): покупка или продажа.

    Не сторона позиции: выход из лонга — продажа. Перевод стороны позиции
    движка в сторону сделки — дело исполнителя в `app/` (план §2.3).
    """

    BUY = "1"
    SELL = "2"


class OrderType(enum.Enum):
    """Тип заявки: коды `1`–`5` создания (13) и статуса по ключу (11)."""

    MARKET = "1"
    LIMIT = "2"
    ALGORITHMIC_GTD = "3"
    ICEBERG = "4"
    CONDITIONAL = "5"


#: Что программа вправе подать. Права торгового токена (`broker-api/00`)
#: называют рыночные и лимитные заявки; условная описана в 13, но годится ли
#: она нашему токену, выясняется только на живом счёте (план §2.3, Р1, Р2).
SUBMITTABLE_TYPES: Final[frozenset[OrderType]] = frozenset(
    {OrderType.MARKET, OrderType.LIMIT}
)


class TimeInForce(enum.Enum):
    """Срок заявки (13). `GOOD_TILL_DATE` — только алгоритмическим, мы их не подаём."""

    DAY = "1"
    FILL_OR_KILL = "2"
    GOOD_TILL_DATE = "3"


#: Сроки, допустимые рыночной и лимитной заявке — дословно по 13.
SUBMITTABLE_TERMS: Final[frozenset[TimeInForce]] = frozenset(
    {TimeInForce.DAY, TimeInForce.FILL_OR_KILL}
)


class OrderStatus(enum.Enum):
    """Состояние заявки `orderStatus` — одна таблица у страниц 11 и 24.

    Кода `3` и `7` в таблице нет, и это не пропуск: их нет в документации.
    Пришли — отказ разбора (шапка модуля, правило 1).
    """

    NEW = "0"
    PARTIALLY_FILLED = "1"
    FILLED = "2"
    CANCELLED = "4"
    REPLACED = "5"
    PENDING_CANCEL = "6"
    REJECTED = "8"
    PENDING_REPLACE = "9"
    PENDING_NEW = "10"


#: Состояние заявки словами — для журнала, который читает владелец счёта.
#: Тексты — из таблицы документации (11, 24), строчными.
STATUS_WORDS: Final[Mapping[OrderStatus, str]] = {
    OrderStatus.NEW: "новая",
    OrderStatus.PARTIALLY_FILLED: "частично исполнена",
    OrderStatus.FILLED: "полностью исполнена",
    OrderStatus.CANCELLED: "отменена",
    OrderStatus.REPLACED: "заменена",
    OrderStatus.PENDING_CANCEL: "отменяется",
    OrderStatus.REJECTED: "отклонена",
    OrderStatus.PENDING_REPLACE: "заменяется",
    OrderStatus.PENDING_NEW: "ожидает подтверждения",
}

#: После этих состояний с заявкой у брокера уже ничего не случится.
#: «Заменена» сюда **не входит**: страница 12 называет её отменяемой, то есть
#: у БКС это состояние живой заявки, а не прежней.
FINAL_STATUSES: Final[frozenset[OrderStatus]] = frozenset(
    {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED}
)

#: Что брокер разрешает отменить — дословно по 12: `0`, `1`, `5`, `10`.
CANCELLABLE_STATUSES: Final[frozenset[OrderStatus]] = frozenset(
    {
        OrderStatus.NEW,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.REPLACED,
        OrderStatus.PENDING_NEW,
    }
)


class ExecutionType(enum.Enum):
    """Вид события `executionType` — одна таблица у страниц 11 и 24."""

    NEW = "0"
    PARTIAL_FILL = "1"
    FILL = "2"
    CANCELLED = "4"
    REPLACED = "5"
    PENDING_CANCEL = "6"
    REJECTED = "8"
    SUSPENDED = "9"
    PENDING_NEW = "10"
    TRADE = "11"
    ORDER_STATUS = "12"
    RESTATED = "13"


#: События, несущие исполнение. ⚠️ **Не только `11` «Сделка».** План (§2.5)
#: называет фактом исполнения один код `11`, но единственный пример события
#: в документации (24) — исполнение с `executionType` `2` «Исполнена»:
#: `lastQuantity` 50, `executionId` заполнен. Счесть его сменой статуса —
#: значит потерять сделку: на счёте позиция, движок считает себя вне рынка.
#: Взяты все три кода; дубль одного исполнения под двумя кодами отсекает
#: уникальный `executionId` в журнале исполнений (план §2.10). Какие коды
#: приходят на самом деле — проверка первого боевого дня.
EXECUTIONS: Final[frozenset[ExecutionType]] = frozenset(
    {ExecutionType.PARTIAL_FILL, ExecutionType.FILL, ExecutionType.TRADE}
)


class ListedOrderStatus(enum.Enum):
    """Состояние заявки в **списке** заявок (15) — своя таблица, не `OrderStatus`.

    Коды совпадают цифрами и расходятся смыслом: `2` здесь «Исполнена», как
    и в 11, а `1` — «Снята», тогда как в 11 это «Частично исполнена».
    """

    REMOVED = "1"
    EXECUTED = "2"
    ACTIVE = "3"


#: `orderIdType` отмены (12) и статуса (11): заявка названа нашим UUID.
#: Второе значение — биржевой номер — программа не посылает.
ORDER_ID_BY_CLIENT: Final[str] = "1"

#: `status` ответа 200 на создание и отмену (13, 12). Других значений
#: документация не называет; любое другое — исход неизвестен.
ACCEPTED_STATUS: Final[str] = "OK"

#: Размер страницы списков (15, 16): умолчание и потолок документации.
DEFAULT_PAGE_SIZE: Final[int] = 50
MAX_PAGE_SIZE: Final[int] = 100


# --- команды брокеру ---


@dataclass(frozen=True, slots=True)
class NewOrder:
    """Новая заявка (13). Ключ `clientOrderId` сюда не входит — его даёт сессия.

    Ключ создаёт пишущий путь (`BrokerSession.create_order`) непосредственно
    перед записью в журнал команд: тот, кто собирает заявку, не может ни
    подставить старый ключ, ни отправить заявку без записи (план §11, С5).
    """

    side: OrderSide
    order_type: OrderType
    quantity: int
    ticker: str
    class_code: str
    #: Цена лимитной заявки как есть. На сетку шага её кладёт вызывающий:
    #: что брокер делает с ценой вне сетки — нет в снимке (план §2.3, Р4).
    price: float | None = None
    time_in_force: TimeInForce = TimeInForce.DAY

    def __post_init__(self) -> None:
        for broken, why in _NEW_ORDER_CHECKS:
            if broken(self):
                raise ValueError(f"заявка не годится к подаче: {why}")

    def body(self, key: UUID) -> dict[str, object]:
        """Тело `POST` создания (13) с ключом, который дала сессия."""
        body: dict[str, object] = {
            "clientOrderId": str(key),
            "side": self.side.value,
            "orderType": self.order_type.value,
            "timeInForce": self.time_in_force.value,
            "orderQuantity": self.quantity,
            "ticker": self.ticker,
            "classCode": self.class_code,
        }
        if self.price is not None:
            body["price"] = self.price
        return body


def _not_text(value: object) -> bool:
    return not isinstance(value, str) or not value or value != value.strip()


def _bad_price(order: NewOrder) -> bool:
    if order.order_type is OrderType.MARKET:
        return order.price is not None
    price = order.price
    return (
        price is None
        or isinstance(price, bool)
        or not math.isfinite(price)
        or price <= 0
    )


#: Проверки новой заявки — **цепочка звеньев «условие → отказ»** списком,
#: а не шестью `if` подряд. Порядок — данные: первое сработавшее звено
#: называет причину. Это ошибка вызывающего, а не брокера, поэтому отказ —
#: `ValueError` до сети, а не отказ брокера после неё (план §6, «объём боевой
#: заявки — целое ≥ 1»: дробный — отказ до запуска, а не на подаче).
_NEW_ORDER_CHECKS: Final[tuple[tuple[Callable[[NewOrder], bool], str], ...]] = (
    (
        lambda order: order.order_type not in SUBMITTABLE_TYPES,
        "программа подаёт только рыночные и лимитные заявки",
    ),
    (
        lambda order: order.time_in_force not in SUBMITTABLE_TERMS,
        "срок «до даты» брокер принимает только у алгоритмических заявок",
    ),
    (
        lambda order: isinstance(order.quantity, bool)
        or not isinstance(order.quantity, int)
        or order.quantity < 1,
        "количество — целое число не меньше единицы",
    ),
    (lambda order: _not_text(order.ticker), "тикер пуст или с пробелами по краям"),
    (lambda order: _not_text(order.class_code), "код класса пуст или с пробелами по краям"),
    (
        _bad_price,
        "у рыночной заявки цены нет, у лимитной — конечное число больше нуля",
    ),
)


@dataclass(frozen=True, slots=True)
class CancelOrder:
    """Отмена заявки (12) по **нашему** ключу исходной заявки.

    ⚠️ У отмены свой `clientOrderId` — UUID самого запроса на отмену, его даёт
    сессия. Ключ исходной заявки уходит в `orderId`. Два UUID в одном теле
    с похожими именами — место, где путаница стоит отмены не той заявки.
    """

    target: UUID

    def body(self, key: UUID) -> dict[str, object]:
        """Тело `POST` отмены (12): `key` — ключ запроса, `target` — заявки."""
        return {
            "orderIdType": ORDER_ID_BY_CLIENT,
            "orderId": str(self.target),
            "clientOrderId": str(key),
        }


#: Команда, меняющая счёт: то, что пишется в журнал до отправки.
OrderCommand = NewOrder | CancelOrder

#: Запись журнала команд, которую передаёт `app/` (план §11, контракт С5).
#: Вызывается с ключом и командой **до** отправки; успешный возврат значит
#: «зафиксировано». Исключение — команда не уходит, исключение идёт наружу.
#: Асинхронная: запись в базу идёт потоком базы, а не в цикле событий.
RecordCommand = Callable[[UUID, OrderCommand], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class CommandAccepted:
    """Брокер ответил 200 и вернул наш ключ: команда **принята брокером**.

    Не «исполнена» и не «снята»: у создания это «принята, ждите биржу»,
    у отмены — «запрос на отмену принят» (13, 12). Факт приходит потоком
    статусов (24) или запросом статуса по ключу (11) — план §0, п. 1.
    """

    key: UUID
    request: OrderCommand


# --- что брокер говорит о заявке ---


@dataclass(frozen=True, slots=True)
class OrderReport:
    """Одно сообщение о заявке: элемент `data` статуса (11) и события потока (24)."""

    status: OrderStatus
    execution_type: ExecutionType
    #: Обязателен у исполнения (`EXECUTIONS`): по нему журнал отсекает
    #: повтор события (план §2.10). У прочих событий бывает пустым.
    execution_id: str | None
    #: `orderId` — биржевой номер заявки, а не наш ключ.
    exchange_order_id: str | None
    order_number: str | None
    ticker: str | None
    class_code: str | None
    side: OrderSide | None
    order_type: OrderType | None
    order_quantity: int | None
    executed_quantity: int | None
    last_quantity: int | None
    remained_quantity: int | None
    price: float | None
    average_price: float | None
    execution_value: float | None
    #: За исполнение или итогом, с биржевым сбором или без — нет в снимке.
    #: Не пришла — `None`, а не ноль (план §2.10).
    commission: float | None
    transaction_time: datetime | None
    trade_date: str | None
    reject_reason: str | None

    @property
    def is_execution(self) -> bool:
        """Несёт ли сообщение исполнение (см. `EXECUTIONS`)."""
        return self.execution_type in EXECUTIONS


@dataclass(frozen=True, slots=True)
class OrderEvent:
    """Событие потока статусов (24): ключи заявки и одно сообщение о ней."""

    client_order_id: str | None
    original_client_order_id: str | None
    report: OrderReport

    @property
    def key(self) -> str | None:
        """Ключ заявки: `clientOrderId`, а если он пуст — `originalClientOrderId`.

        Единственный пример документации (24) — исполнение, у которого
        `clientOrderId` пуст, а заполнен `originalClientOrderId`. Какое поле
        несёт наш ключ у прочих событий — нет в снимке (план §2.9), поэтому
        оба значения хранятся и сверять следует по обоим. `None` — у события
        нет ни одного ключа: заявка подана не через этот API.
        """
        return self.client_order_id or self.original_client_order_id


@dataclass(frozen=True, slots=True)
class OrderState:
    """Ответ статуса по ключу (11): все сообщения о заявке, что прислал брокер."""

    client_order_id: str | None
    original_client_order_id: str | None
    reports: tuple[OrderReport, ...]

    @property
    def latest(self) -> OrderReport:
        """Последнее сообщение — **по времени транзакции**, а не по месту в массиве.

        Порядок массива `data` документация не описывает (план §9). Сообщения
        без времени считаются старше любого с временем; при равенстве
        побеждает стоящее дальше в массиве.
        """
        oldest = datetime.min.replace(tzinfo=timezone.utc)
        ranked = sorted(
            enumerate(self.reports),
            key=lambda pair: (
                pair[1].transaction_time is not None,
                pair[1].transaction_time or oldest,
                pair[0],
            ),
        )
        return ranked[-1][1]


@dataclass(frozen=True, slots=True)
class ListedOrder:
    """Строка списка заявок (15). Нашего ключа в ней нет — только биржевой."""

    status: ListedOrderStatus
    order_num: int | None
    exchange_order_id: str | None
    ticker: str | None
    class_code: str | None
    side: OrderSide | None
    #: Код типа из **своего** перечня страницы 15 (`1`–`7`, `10`, `11`),
    #: числом как есть: у тейк-профита там три кода, у стоп-лосса свой.
    order_type: int | None
    order_quantity: int | None
    executed_quantity: int | None
    remained_quantity: int | None
    price: float | None
    average_price: float | None
    take_price: float | None
    stop_price: float | None
    reject_reason: str | None
    ordered_at: datetime | None
    updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class TradeRecord:
    """Строка списка сделок (16)."""

    trade_num: int | None
    order_num: int | None
    ticker: str | None
    class_code: str | None
    side: OrderSide | None
    traded_at: datetime | None
    price: float | None
    quantity: int | None
    volume: float | None
    #: `go` — гарантийное обеспечение по сделке.
    margin: float | None


@dataclass(frozen=True, slots=True)
class OrdersPage:
    """Страница списка заявок (15)."""

    records: tuple[ListedOrder, ...]
    total_records: int | None
    total_pages: int | None


@dataclass(frozen=True, slots=True)
class TradesPage:
    """Страница списка сделок (16)."""

    records: tuple[TradeRecord, ...]
    total_records: int | None
    total_pages: int | None


# --- запросы списков ---


def _utc_text(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _check_window(start: datetime | None, end: datetime | None, page: int, size: int) -> None:
    """Общие проверки обоих списков. Наивное время — отказ: пояс не угадывается."""
    for moment in (start, end):
        if moment is not None and moment.utcoffset() is None:
            raise ValueError("время запроса списка без часового пояса")
    if page < 0 or not 1 <= size <= MAX_PAGE_SIZE:
        raise ValueError(f"страница {page}, размер {size}: размер от 1 до {MAX_PAGE_SIZE}")


@dataclass(frozen=True, slots=True)
class OrdersQuery:
    """Фильтр списка заявок (15). Пустое поле фильтра в тело не идёт."""

    start: datetime | None = None
    end: datetime | None = None
    side: OrderSide | None = None
    statuses: tuple[ListedOrderStatus, ...] = ()
    tickers: tuple[str, ...] = ()
    class_codes: tuple[str, ...] = ()
    page: int = 0
    size: int = DEFAULT_PAGE_SIZE

    def __post_init__(self) -> None:
        _check_window(self.start, self.end, self.page, self.size)

    def params(self) -> dict[str, int]:
        """Строка запроса: страница и её размер."""
        return {"page": self.page, "size": self.size}

    def body(self) -> dict[str, object]:
        """Тело запроса. Сторона и состояние здесь — **числа** (15)."""
        body: dict[str, object] = {}
        if self.start is not None:
            body["startDateTime"] = _utc_text(self.start)
        if self.end is not None:
            body["endDateTime"] = _utc_text(self.end)
        if self.side is not None:
            body["side"] = int(self.side.value)
        if self.statuses:
            body["orderStatus"] = [int(status.value) for status in self.statuses]
        if self.tickers:
            body["tickers"] = list(self.tickers)
        if self.class_codes:
            body["classCodes"] = list(self.class_codes)
        return body


@dataclass(frozen=True, slots=True)
class TradesQuery:
    """Фильтр списка сделок (16). Пустое поле фильтра в тело не идёт."""

    start: datetime | None = None
    end: datetime | None = None
    side: OrderSide | None = None
    tickers: tuple[str, ...] = ()
    class_codes: tuple[str, ...] = ()
    page: int = 0
    size: int = DEFAULT_PAGE_SIZE

    def __post_init__(self) -> None:
        _check_window(self.start, self.end, self.page, self.size)

    def params(self) -> dict[str, int]:
        """Строка запроса: страница и её размер."""
        return {"page": self.page, "size": self.size}

    def body(self) -> dict[str, object]:
        """Тело запроса. Сторона здесь — **строка**, не число, как в 15 (16)."""
        body: dict[str, object] = {}
        if self.start is not None:
            body["startDateTime"] = _utc_text(self.start)
        if self.end is not None:
            body["endDateTime"] = _utc_text(self.end)
        if self.side is not None:
            body["side"] = self.side.value
        if self.tickers:
            body["tickers"] = list(self.tickers)
        if self.class_codes:
            body["classCodes"] = list(self.class_codes)
        return body


# --- разбор ---

#: Как выглядит код, который можно показать в техническом тексте отказа.
#: Остальное описывается видом и длиной, без единого символа из сети.
_SHOWABLE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_.\-]{1,16}")

_Code = TypeVar("_Code", bound=enum.Enum)


def _shown(value: object) -> str:
    raw = str(value)
    if _SHOWABLE.fullmatch(raw):
        return repr(raw)
    return f"{type(value).__name__}, {len(raw)} симв."


def _unreadable(technical: str) -> UnexpectedAnswer:
    return UnexpectedAnswer(
        "непонятное сообщение о заявке", f"заявка: {technical}"
    )


def _code(source: Mapping[str, object], field: str, table: type[_Code]) -> _Code | None:
    """Код по таблице. Поля нет — `None`; код незнакомый — отказ, а не подмена."""
    raw = pick(source, (field,))
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    code = raw.strip() if isinstance(raw, str) else raw
    if isinstance(code, bool) or not isinstance(code, (str, int)):
        raise _unreadable(f"{field}: код не строкой и не целым, {_shown(code)}")
    try:
        return table(str(code))
    except ValueError:
        raise _unreadable(f"{field}: незнакомый код {_shown(code)}") from None


def _required_code(source: Mapping[str, object], field: str, table: type[_Code]) -> _Code:
    """Код, без которого сообщение не понять: нет поля — тоже отказ."""
    found = _code(source, field, table)
    if found is None:
        raise _unreadable(f"нет поля {field}")
    return found


def _amount(source: Mapping[str, object], field: str) -> float | None:
    raw = pick(source, (field,))
    if raw is None:
        return None
    value = number(raw)
    if value is None or not math.isfinite(value):
        raise _unreadable(f"{field}: не число, {_shown(raw)}")
    return value


def _whole(source: Mapping[str, object], field: str) -> int | None:
    value = _amount(source, field)
    if value is None:
        return None
    if not value.is_integer():
        raise _unreadable(f"{field}: дробное количество {value}")
    return int(value)


def _moment(source: Mapping[str, object], field: str) -> datetime | None:
    raw = pick(source, (field,))
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    try:
        return aware_moment(raw)
    except ValueError:
        raise _unreadable(f"{field}: не время с поясом, {_shown(raw)}") from None


def _key(value: object) -> str | None:
    """Ключ заявки в каноническом виде UUID; не UUID — как пришёл."""
    found = text(value)
    if found is None:
        return None
    try:
        return str(UUID(found))
    except ValueError:
        return found


def _mapping(value: object, what: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _unreadable(f"{what} не объект JSON")
    return value


def parse_report(data: object) -> OrderReport:
    """Сообщение о заявке (`data` в 11 и 24). Отказ — `UnexpectedAnswer`."""
    source = _mapping(data, "сообщение о заявке")
    status = _required_code(source, "orderStatus", OrderStatus)
    kind = _required_code(source, "executionType", ExecutionType)
    execution_id = text(pick(source, ("executionId",)))
    if kind in EXECUTIONS and execution_id is None:
        raise _unreadable(f"исполнение (executionType {kind.value}) без executionId")
    return OrderReport(
        status=status,
        execution_type=kind,
        execution_id=execution_id,
        exchange_order_id=text(pick(source, ("orderId",))),
        order_number=text(pick(source, ("orderNumber",))),
        ticker=text(pick(source, ("ticker",))),
        class_code=text(pick(source, ("classCode",))),
        side=_code(source, "side", OrderSide),
        order_type=_code(source, "orderType", OrderType),
        order_quantity=_whole(source, "orderQuantity"),
        executed_quantity=_whole(source, "executedQuantity"),
        last_quantity=_whole(source, "lastQuantity"),
        remained_quantity=_whole(source, "remainedQuantity"),
        price=_amount(source, "price"),
        average_price=_amount(source, "averagePrice"),
        execution_value=_amount(source, "executionValue"),
        commission=_amount(source, "commission"),
        transaction_time=_moment(source, "transactionTime"),
        trade_date=text(pick(source, ("tradeDate",))),
        reject_reason=text(pick(source, ("rejectReason",))),
    )


def parse_order_event(message: object) -> OrderEvent:
    """Событие потока статусов (24). Отказ — `UnexpectedAnswer`."""
    source = _mapping(message, "событие о заявке")
    return OrderEvent(
        client_order_id=_key(pick(source, ("clientOrderId",))),
        original_client_order_id=_key(pick(source, ("originalClientOrderId",))),
        report=parse_report(source.get("data")),
    )


def parse_order_state(payload: object) -> OrderState:
    """Ответ статуса по ключу (11). Пустой `data` — отказ: «нет заявки» — это 404."""
    source = _mapping(payload, "ответ статуса заявки")
    data = source.get("data")
    if not isinstance(data, list) or not data:
        raise _unreadable("в ответе статуса нет сообщений о заявке (data)")
    return OrderState(
        client_order_id=_key(pick(source, ("clientOrderId",))),
        original_client_order_id=_key(pick(source, ("originalClientOrderId",))),
        reports=tuple(parse_report(item) for item in data),
    )


def acceptance_doubt(payload: object, key: UUID) -> str | None:
    """Почему ответ 200 **не** подтверждает приём именно этой команды; `None` — подтверждает.

    Три звена подряд — от формы ответа к его содержанию; таблицей не сделаны,
    потому что каждое читает своё поле своим способом. Ответ, не прошедший
    любое из них, — не отказ: 200 уже пришёл, и команда могла быть принята.
    Сессия называет это «исход неизвестен» (`OutcomeUnknown`).
    """
    if not isinstance(payload, Mapping):
        return "тело ответа 200 не объект JSON"
    status = text(pick(payload, ("status",)))
    if status != ACCEPTED_STATUS:
        return f"status ответа {_shown(status)} вместо {ACCEPTED_STATUS}"
    if _key(pick(payload, ("clientOrderId",))) != str(key):
        return "clientOrderId ответа не совпадает с отправленным"
    return None


def _records(payload: object) -> tuple[list[Mapping[str, object]], int | None, int | None]:
    source = _mapping(payload, "ответ списка")
    records = source.get("records")
    if not isinstance(records, list):
        raise _unreadable("в ответе списка нет массива records")
    return (
        [_mapping(item, "строка списка") for item in records],
        _whole(source, "totalRecords"),
        _whole(source, "totalPages"),
    )


def parse_orders_page(payload: object) -> OrdersPage:
    """Страница списка заявок (15). Незнакомое состояние строки — отказ."""
    rows, total_records, total_pages = _records(payload)
    records = tuple(
        ListedOrder(
            status=_required_code(row, "orderStatus", ListedOrderStatus),
            order_num=_whole(row, "orderNum"),
            exchange_order_id=text(pick(row, ("orderId",))),
            ticker=text(pick(row, ("ticker",))),
            class_code=text(pick(row, ("classCode",))),
            side=_code(row, "side", OrderSide),
            order_type=_whole(row, "orderType"),
            order_quantity=_whole(row, "orderQuantity"),
            executed_quantity=_whole(row, "executedQuantity"),
            remained_quantity=_whole(row, "remainedQuantity"),
            price=_amount(row, "price"),
            average_price=_amount(row, "averagePrice"),
            take_price=_amount(row, "takePrice"),
            stop_price=_amount(row, "stopPrice"),
            reject_reason=text(pick(row, ("rejectReason",))),
            ordered_at=_moment(row, "orderDateTime"),
            updated_at=_moment(row, "updateDateTime"),
        )
        for row in rows
    )
    return OrdersPage(records, total_records, total_pages)


def parse_trades_page(payload: object) -> TradesPage:
    """Страница списка сделок (16)."""
    rows, total_records, total_pages = _records(payload)
    records = tuple(
        TradeRecord(
            trade_num=_whole(row, "tradeNum"),
            order_num=_whole(row, "orderNum"),
            ticker=text(pick(row, ("ticker",))),
            class_code=text(pick(row, ("classCode",))),
            side=_code(row, "side", OrderSide),
            traded_at=_moment(row, "tradeDateTime"),
            price=_amount(row, "price"),
            quantity=_whole(row, "tradeQuantity"),
            volume=_amount(row, "volume"),
            margin=_amount(row, "go"),
        )
        for row in rows
    )
    return TradesPage(records, total_records, total_pages)
