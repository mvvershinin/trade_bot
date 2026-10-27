"""Подставной брокер: одна книга заявок на HTTP и на потоке статусов.

Э4, шаг 1, фаза 3 (`.docs/plans/E4-live-orders.md` §8, §11). В сеть не ходит:
HTTP — `httpx.MockTransport` (вызов объекта — обработчик запроса), поток
статусов — подмена `websockets.connect` (`FakeBroker.connect`). Адрес —
`example.invalid`, токены — подставные из `tests/test_broker_session.py`.

**Что подделка знает о брокере — со страниц снимка `.docs/broker-api/`,
а не из `broker/`:** пары «глагол и адрес» (11, 12, 13 — через
`ORDER_ENDPOINTS` и `READING_AROUND_ORDERS`, которые сверяются со снимком
в `tests/test_broker_session.py`), форма ответа 200 (12, 13), форма события
(24, пример страницы), коды состояний (11, 24). Подделка, собранная из таблиц
продукта, зеленела бы вместе с ними.

**Чего снимок не говорит, подделка решает в худшую для нас сторону:**

* повтор `clientOrderId` при создании — у брокера **вторая заявка**:
  что сервер делает с повтором ключа, документация не описывает (13);
* 5xx, 429, таймаут и обрыв **после** приёма — заявка у брокера есть,
  а ответа нет (`Answer.applies`);
* отмена уже исполненной заявки — 200 «запрос принят» и ни одного
  события `4`: что брокер отвечает на самом деле, в снимке нет (12, «Зачем»).

Книга одна на оба канала. Заявка, принятая по HTTP, даёт событие «Новая»
в потоке; исполнение, которое задаёт тест (`execute`), — событие и новое
состояние для `GET` статуса. Пока открытого сокета нет, события теряются
(`lost`): повтора после переподключения в документации нет (план §5).

Пример (сессия — `BrokerSession` с `client=broker.client()`)::

    broker = FakeBroker()
    broker.create_answers.append(ANSWERS["принята, ответ потерян по таймауту"])
    monkeypatch.setattr(websockets, "connect", broker.connect)
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from uuid import UUID

import httpx
import websockets

from broker.session import AUTH_URL
from tests.test_broker_session import ORDER_ENDPOINTS, READING_AROUND_ORDERS, good_auth

#: Пары — со страниц снимка (через проверенные литералы `tests/test_broker_session.py`).
CREATE = ORDER_ENDPOINTS[0]  # 13-create-order.md
CANCEL = ORDER_ENDPOINTS[2]  # 12-cancel-order.md
STATUS = READING_AROUND_ORDERS[0]  # 11-get-order-by-id.md

#: После этих состояний с заявкой ничего не случится (11, 24): «Полностью
#: исполнена», «Отменена», «Отклонена». Остальные коды — живая заявка.
FINAL = frozenset({"2", "4", "8"})

#: Что брокер разрешает отменить — дословно по 12: `0`, `1`, `5`, `10`.
CANCELLABLE = frozenset({"0", "1", "5", "10"})

#: Начало часов подделки: время транзакции у каждого события своё и растёт.
_EPOCH = datetime(2026, 10, 27, 7, 0, tzinfo=timezone.utc)


# --- как брокер отвечает на команду ---


def _accepted(request: httpx.Request, key: str) -> httpx.Response:
    """200 по форме 12 и 13: ключ запроса и `status` `OK`."""
    return httpx.Response(200, json={"clientOrderId": key, "status": "OK"})


def _timed_out(request: httpx.Request, key: str) -> httpx.Response:
    raise httpx.ReadTimeout("ответ не пришёл", request=request)


def _cut(request: httpx.Request, key: str) -> httpx.Response:
    raise httpx.RemoteProtocolError("сервер закрыл соединение без ответа", request=request)


def _not_reached(request: httpx.Request, key: str) -> httpx.Response:
    raise httpx.ConnectTimeout("до брокера не дошло", request=request)


def _refusal(status: int, kind: str) -> Callable[[httpx.Request, str], httpx.Response]:
    """Общая форма отказа (03, конец): `type` — код брокера."""

    def reply(request: httpx.Request, key: str) -> httpx.Response:
        return httpx.Response(
            status, json={"type": kind, "traceId": "fake-trace", "errors": []}
        )

    return reply


def _unavailable(request: httpx.Request, key: str) -> httpx.Response:
    return httpx.Response(503, text="Service Unavailable")


@dataclass(frozen=True)
class Answer:
    """Исход одной команды: выполнил ли её брокер и что услышала программа.

    Две половины разведены нарочно: «ответ потерян» не значит «команды
    не было». Именно это расхождение и стерегут тесты дубля.
    """

    #: Брокер выполнил команду: создал заявку или снял её.
    applies: bool
    #: Что ушло программе: ответ или исключение транспорта.
    reply: Callable[[httpx.Request, str], httpx.Response]


#: Все исходы команды, которые разбирает план (§2.4, §8 №1, №2, №6).
#: Таблица, а не ветки: тест перебирает её целиком.
ANSWERS: dict[str, Answer] = {
    "принята, ответ 200": Answer(True, _accepted),
    "принята, ответ потерян по таймауту": Answer(True, _timed_out),
    "принята, связь оборвана до ответа": Answer(True, _cut),
    "принята, ответ 500": Answer(True, _refusal(500, "INTERNAL_SERVER_ERROR")),
    "принята, ответ 429": Answer(True, _refusal(429, "RESOURCE_EXHAUSTED")),
    "не принята, 429": Answer(False, _refusal(429, "RESOURCE_EXHAUSTED")),
    "не принята, 503": Answer(False, _unavailable),
    "не принята, нет подключения": Answer(False, _not_reached),
    "не принята, 400": Answer(False, _refusal(400, "VALIDATION_ERROR")),
    "не принята, 422": Answer(False, _refusal(422, "VALIDATION_ERROR")),
}

ACCEPT = ANSWERS["принята, ответ 200"]


# --- книга ---


@dataclass
class BookOrder:
    """Заявка в книге подделки — то, что «лежит у брокера»."""

    key: str
    side: str
    order_type: str
    quantity: int
    ticker: str
    class_code: str
    price: float | None
    exchange_id: str
    status: str = "0"
    executed: int = 0
    turnover: float = 0.0
    reports: list[dict[str, object]] = field(default_factory=list)

    @property
    def live(self) -> bool:
        return self.status not in FINAL


class FakeOrderSocket:
    """Сокет потока статусов: кадры книги в очереди, обрыв — исключение из `recv`."""

    def __init__(self) -> None:
        self.inbox: asyncio.Queue[str | BaseException] = asyncio.Queue()
        self.open = True

    async def recv(self) -> str:
        item = await self.inbox.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def ping(self) -> asyncio.Future[float]:
        pong: asyncio.Future[float] = asyncio.get_running_loop().create_future()
        pong.set_result(0.01)
        return pong


@dataclass
class Handshake:
    """Одно рукопожатие: сам словарь заголовков (стирание) и снимок на входе."""

    url: str
    headers: dict[str, str]
    at_handshake: dict[str, str]


class _Dial:
    """Контекст `websockets.connect`: отдаёт сокет и закрывает его на выходе."""

    def __init__(self, broker: FakeBroker) -> None:
        self._broker = broker
        self._socket: FakeOrderSocket | None = None

    async def __aenter__(self) -> FakeOrderSocket:
        self._socket = FakeOrderSocket()
        self._broker.sockets.append(self._socket)
        return self._socket

    async def __aexit__(self, *unused: object) -> bool:
        if self._socket is not None:
            self._socket.open = False
        return False


class FakeBroker:
    """Одна книга заявок на HTTP и на потоке статусов. Порядок исходов задаёт тест.

    `create_answers`, `cancel_answers`, `status_answers` — очереди исходов:
    каждый запрос берёт следующий, пустая очередь — «принята, ответ 200».
    """

    def __init__(self) -> None:
        self.orders: list[BookOrder] = []
        self.requests: list[httpx.Request] = []
        #: Ушедшие команды по порядку: (`create`/`cancel`, ключ запроса).
        self.commands: list[tuple[str, str]] = []
        self.create_answers: list[Answer] = []
        self.cancel_answers: list[Answer] = []
        #: Исходы `GET` статуса: `None` — ответить по книге.
        self.status_answers: list[Callable[[httpx.Request, str], httpx.Response] | None] = []
        self.sockets: list[FakeOrderSocket] = []
        self.handshakes: list[Handshake] = []
        #: Все кадры, что книга послала в поток, и те, что ушли в пустоту.
        self.frames: list[str] = []
        self.lost: list[str] = []
        #: Наибольшее число живых заявок за всё время — «в любой момент» плана §8 №4.
        self.peak_live = 0
        self._numbers = itertools.count(1)
        self._routes: dict[tuple[str, str], Callable[[httpx.Request], httpx.Response]] = {
            CREATE: self._create,
            CANCEL: self._cancel,
            STATUS: self._status,
        }

    # --- HTTP ---

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        # Обмен токена — не предмет подделки: отвечает всегда.
        if str(request.url) == AUTH_URL:
            return httpx.Response(200, json=good_auth())
        route = self._routes.get((request.method, request.url.path))
        if route is None:
            raise AssertionError(
                f"подделка не знает {request.method} {request.url.path}: "
                "запрос мимо книги заявок"
            )
        return route(request)

    def client(self) -> httpx.AsyncClient:
        """Клиент сессии с этой подделкой вместо сети."""
        return httpx.AsyncClient(
            base_url="https://example.invalid", transport=httpx.MockTransport(self)
        )

    def _create(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        key = str(body["clientOrderId"])
        self.commands.append(("create", key))
        answer = self.create_answers.pop(0) if self.create_answers else ACCEPT
        if answer.applies:
            # Худший случай: повтор ключа — вторая заявка, а не тот же ответ.
            order = BookOrder(
                key=key,
                side=str(body["side"]),
                order_type=str(body["orderType"]),
                quantity=int(body["orderQuantity"]),
                ticker=str(body["ticker"]),
                class_code=str(body["classCode"]),
                price=body.get("price"),
                exchange_id=f"FAKE-EXCH-{next(self._numbers)}",
            )
            self.orders.append(order)
            self._note_live()
            self._emit(order, execution_type="0")
        return answer.reply(request, key)

    def _cancel(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        key = str(body["clientOrderId"])
        self.commands.append(("cancel", key))
        answer = self.cancel_answers.pop(0) if self.cancel_answers else ACCEPT
        target = self.order(str(body["orderId"]))
        if answer.applies and target is not None and target.status in CANCELLABLE:
            target.status = "4"
            self._emit(target, execution_type="4")
        # Исполненная до отмены — тишина: события `4` по ней не будет никогда.
        return answer.reply(request, key)

    def _status(self, request: httpx.Request) -> httpx.Response:
        key = request.url.params["orderId"]
        scripted = self.status_answers.pop(0) if self.status_answers else None
        if scripted is not None:
            return scripted(request, key)
        order = self.order(key)
        if order is None:
            return httpx.Response(404, json={"type": "NOT_FOUND", "traceId": "fake-trace"})
        return httpx.Response(
            200,
            json={"clientOrderId": key, "originalClientOrderId": key, "data": order.reports},
        )

    # --- поток статусов ---

    def connect(
        self,
        url: str,
        *,
        additional_headers: dict[str, str],
        logger: logging.Logger,
        **options: float,
    ) -> _Dial:
        """Подмена `websockets.connect`: аргументы — как у библиотеки, поимённо."""
        self.handshakes.append(
            Handshake(url=url, headers=additional_headers, at_handshake=dict(additional_headers))
        )
        return _Dial(self)

    def drop_stream(self) -> None:
        """Оборвать все открытые сокеты: следующее чтение — обрыв библиотеки."""
        for socket in self.sockets:
            if socket.open:
                socket.open = False
                socket.inbox.put_nowait(websockets.exceptions.ConnectionClosedError(None, None))

    def replay_last(self) -> None:
        """Прислать последнее событие ещё раз: поток вправе повторять (план §2.10)."""
        self._deliver(self.frames[-1])

    # --- что делает тест ---

    def execute(self, key: str, quantity: int, price: float) -> None:
        """Биржа исполнила `quantity` контрактов заявки `key` по `price`."""
        order = self.order(key)
        if order is None or not order.live:
            raise AssertionError(f"исполнять нечего: заявка {key} не жива в книге")
        if quantity < 1 or order.executed + quantity > order.quantity:
            raise AssertionError(f"исполнение {quantity} не помещается в заявку {key}")
        order.executed += quantity
        order.turnover += quantity * price
        full = order.executed == order.quantity
        order.status = "2" if full else "1"
        self._emit(
            order,
            execution_type="2" if full else "1",
            execution={"executionId": f"FAKE-EXEC-{next(self._numbers)}", "lastQuantity": quantity},
        )
        self._note_live()

    # --- что видит тест ---

    def order(self, key: str) -> BookOrder | None:
        """Первая заявка книги с этим ключом. Ключ сверяется как UUID, не как строка."""
        wanted = _canonical(key)
        return next((one for one in self.orders if _canonical(one.key) == wanted), None)

    def live(self) -> list[BookOrder]:
        return [one for one in self.orders if one.live]

    def keys_sent(self, what: str) -> Counter[str]:
        """Сколько раз ушёл каждый ключ команды вида `what` (`create`/`cancel`)."""
        return Counter(key for kind, key in self.commands if kind == what)

    # --- изнутри ---

    def _note_live(self) -> None:
        self.peak_live = max(self.peak_live, len(self.live()))

    def _emit(
        self,
        order: BookOrder,
        *,
        execution_type: str,
        execution: dict[str, object] | None = None,
    ) -> None:
        """Сообщение о заявке (11, 24): в историю статуса и кадром в поток."""
        moment = _EPOCH + timedelta(seconds=len(self.frames) + 1)
        data: dict[str, object] = {
            "messageType": "8",
            "orderStatus": order.status,
            "executionType": execution_type,
            "orderId": order.exchange_id,
            "ticker": order.ticker,
            "classCode": order.class_code,
            "side": order.side,
            "orderType": order.order_type,
            "orderQuantity": order.quantity,
            "executedQuantity": order.executed,
            "remainedQuantity": order.quantity - order.executed,
            "transactionTime": moment.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        }
        if order.price is not None:
            data["price"] = order.price
        if execution is not None:
            data.update(execution)
            data["averagePrice"] = order.turnover / order.executed
        order.reports.append(data)
        # Форма кадра — пример страницы 24: ключ в `originalClientOrderId`,
        # `clientOrderId` пуст.
        frame = json.dumps({"originalClientOrderId": order.key, "clientOrderId": "", "data": data})
        self.frames.append(frame)
        self._deliver(frame)

    def _deliver(self, frame: str) -> None:
        listening = [socket for socket in self.sockets if socket.open]
        if not listening:
            self.lost.append(frame)
        for socket in listening:
            socket.inbox.put_nowait(frame)


def _canonical(key: str) -> str:
    try:
        return str(UUID(key))
    except ValueError:
        return key


class Journal:
    """Журнал команд вместо `app/`: что записано и сколько команд ушло к моменту записи."""

    def __init__(self, broker: FakeBroker) -> None:
        self.broker = broker
        self.entries: list[tuple[UUID, object]] = []
        self.sent_before_entry: list[int] = []

    async def __call__(self, key: UUID, request: object) -> None:
        self.sent_before_entry.append(len(self.broker.commands))
        self.entries.append((key, request))
