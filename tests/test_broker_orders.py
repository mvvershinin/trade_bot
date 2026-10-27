"""Пишущий путь сессии и разбор заявок: Э4, шаг 1, фаза 1а.

План: `.docs/plans/E4-live-orders.md` §11, «Что стерегут тесты». В сеть тесты
не ходят: транспорт `httpx` подменён (`httpx.MockTransport`), адрес —
`example.invalid`. Настоящий токен не участвует.

Где можно, ожидаемое берётся **не из продуктового кода**, а со страниц снимка
документации `.docs/broker-api/`: коды состояний, пример события, имена
полей и адреса. Сторож, сверяющий код с самим собой, зеленеет вместе с ним.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from collections.abc import Callable, Coroutine
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, TypeVar
from uuid import UUID, uuid4

import httpx
import pytest

from broker.errors import (
    AuthFailed,
    BadRequest,
    BrokerError,
    NotFound,
    OutcomeUnknown,
    ReadOnlyToken,
    Timeout,
    UnexpectedAnswer,
    WrongAddress,
)
from broker.orders import (
    STATUS_WORDS,
    CancelOrder,
    CommandAccepted,
    ExecutionType,
    ListedOrderStatus,
    NewOrder,
    OrderCommand,
    OrderSide,
    OrdersPage,
    OrdersQuery,
    OrderState,
    OrderStatus,
    OrderType,
    TimeInForce,
    TradesPage,
    TradesQuery,
    parse_order_event,
    parse_report,
)
from broker.secret import Secret
from broker.session import (
    AUTH_URL,
    PORTFOLIO_PATH,
    READ_ENDPOINTS,
    REPEATABLE,
    WRITE_ENDPOINTS,
    BrokerSession,
)
from broker.throttle import Throttle
from broker.token_store import TokenStore
from broker.tokens import TokenScope
from tests.test_broker_session import (
    BROKER_API_DOCS,
    FAKE_REFRESH,
    GATEWAY_404,
    ORDER_ENDPOINTS,
    READING_AROUND_ORDERS,
    START,
    Clock,
    endpoint_from_docs,
    good_auth,
    no_sleep,
)

# Пары «глагол и адрес» — **со страниц снимка**, а не из `broker/session.py`.
# Что литералы совпадают со снимком, проверяет
# `tests/test_broker_session.py::test_the_trading_addresses_are_the_ones_the_documentation_names`.
CREATE = ORDER_ENDPOINTS[0]  # 13-create-order.md
EDIT = ORDER_ENDPOINTS[1]  # 14-update-order.md
CANCEL = ORDER_ENDPOINTS[2]  # 12-cancel-order.md
STATUS = READING_AROUND_ORDERS[0]  # 11-get-order-by-id.md
ORDERS_SEARCH = READING_AROUND_ORDERS[1]  # 15-orders-search.md
TRADES_SEARCH = READING_AROUND_ORDERS[2]  # 16-trades-search.md
COMMANDS = {CREATE, CANCEL}

MARKET_BUY = NewOrder(
    side=OrderSide.BUY,
    order_type=OrderType.MARKET,
    quantity=1,
    ticker="MXZ6",
    class_code="SPBFUT",
)
LIMIT_SELL = NewOrder(
    side=OrderSide.SELL,
    order_type=OrderType.LIMIT,
    quantity=2,
    ticker="MXZ6",
    class_code="SPBFUT",
    price=231450.0,
)

Reply = Callable[[httpx.Request], httpx.Response]


def report(**changes: object) -> dict[str, object]:
    """Сообщение о заявке в форме страниц 11 и 24: новая заявка без исполнения."""
    data: dict[str, object] = {
        "messageType": "8",
        "orderStatus": "0",
        "executionType": "0",
        "orderQuantity": 1,
        "executedQuantity": 0,
        "remainedQuantity": 1,
        "ticker": "MXZ6",
        "classCode": "SPBFUT",
        "side": "1",
        "orderType": "1",
        "transactionTime": "2026-10-27T07:00:00.000Z",
    }
    data.update(changes)
    return data


def answer_like_a_broker(request: httpx.Request) -> httpx.Response:
    """Подставной брокер без книги заявок: команда принята, статус — «новая»."""
    endpoint = (request.method, request.url.path)
    if endpoint in COMMANDS:
        sent = json.loads(request.content)
        return httpx.Response(200, json={"clientOrderId": sent["clientOrderId"], "status": "OK"})
    if endpoint == STATUS:
        key = request.url.params["orderId"]
        return httpx.Response(200, json={"clientOrderId": key, "data": [report()]})
    if endpoint in (ORDERS_SEARCH, TRADES_SEARCH):
        return httpx.Response(200, json={"records": [], "totalRecords": 0, "totalPages": 0})
    return httpx.Response(200, json={"ok": True})


class Wire:
    """Подставной транспорт: обмен токена отвечает всегда, остальное — `reply`."""

    def __init__(self, reply: Reply = answer_like_a_broker) -> None:
        self.reply = reply
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url) == AUTH_URL:
            return httpx.Response(200, json=good_auth())
        return self.reply(request)

    def commands(self) -> list[httpx.Request]:
        """Ушедшие команды — по парам из документации, а не из кода."""
        return [r for r in self.requests if (r.method, r.url.path) in COMMANDS]

    def to(self, endpoint: tuple[str, str]) -> list[httpx.Request]:
        return [r for r in self.requests if (r.method, r.url.path) == endpoint]


class Journal:
    """Журнал команд вместо `app/`: помнит записи и сколько команд ушло к моменту записи."""

    def __init__(self, wire: Wire, fail: Callable[[], BaseException] | None = None) -> None:
        self.wire = wire
        self.fail = fail
        self.calls = 0
        self.entries: list[tuple[UUID, OrderCommand]] = []
        self.sent_before_entry: list[int] = []

    async def __call__(self, key: UUID, request: OrderCommand) -> None:
        self.calls += 1
        self.sent_before_entry.append(len(self.wire.commands()))
        if self.fail is not None:
            raise self.fail()
        self.entries.append((key, request))


def open_session(
    store: TokenStore,
    wire: Wire,
    *,
    attempts: int = 3,
    throttle: Throttle | None = None,
) -> BrokerSession:
    return BrokerSession(
        store,
        client=httpx.AsyncClient(
            base_url="https://example.invalid", transport=httpx.MockTransport(wire)
        ),
        clock=Clock(),
        sleep=no_sleep,
        attempts=attempts,
        throttle=throttle or Throttle(rate=1000.0),
        order_throttle=Throttle(rate=1000.0),
    )


_Result = TypeVar("_Result")


def run(coroutine: Coroutine[object, object, _Result]) -> _Result:
    return asyncio.run(coroutine)


@pytest.fixture
def store(tmp_path) -> TokenStore:
    directory = tmp_path / "userdata"
    directory.mkdir()
    keeper = TokenStore(directory)
    keeper.save(Secret(FAKE_REFRESH), TokenScope.TRADE, issued_at=START - timedelta(days=1))
    return keeper


@pytest.fixture
def read_only_store(tmp_path) -> TokenStore:
    directory = tmp_path / "userdata"
    directory.mkdir()
    keeper = TokenStore(directory)
    keeper.save(Secret(FAKE_REFRESH), TokenScope.READ_ONLY, issued_at=START - timedelta(days=1))
    return keeper


#: Две команды одним видом: имя для отчёта и вызов на сессии.
COMMAND_CALLS = {
    "создание": lambda broker, record: broker.create_order(MARKET_BUY, record=record),
    "отмена": lambda broker, record: broker.cancel_order(uuid4(), record=record),
}


# --- журнал до отправки (С5) ---


def test_the_key_is_recorded_before_the_order_goes_out_and_is_the_key_sent(
    store: TokenStore,
) -> None:
    """Запись журнала идёт **до** POST, и в теле POST тот же UUID, что в записи.

    Стережёт контракт С5: ключ без записи не отправляется, а записанный ключ —
    ровно тот, по которому потом спрашивают статус. Мутации: запись после
    POST; новый `uuid4` в теле вместо записанного.
    """
    wire = Wire()
    journal = Journal(wire)

    async def scenario() -> CommandAccepted:
        async with open_session(store, wire) as broker:
            return await broker.create_order(MARKET_BUY, record=journal)

    accepted = run(scenario())
    assert journal.sent_before_entry == [0], "команда ушла к брокеру раньше записи журнала"
    assert len(journal.entries) == 1
    key, request = journal.entries[0]
    assert request is MARKET_BUY, "в журнал записана не та команда"
    sent = wire.commands()
    assert len(sent) == 1
    assert json.loads(sent[0].content)["clientOrderId"] == str(key), (
        "в теле POST не тот ключ, что записан в журнал: статус по записи не найдётся"
    )
    assert accepted.key == key and accepted.request is MARKET_BUY


def test_a_cancel_has_its_own_key_and_names_the_order_by_ours(store: TokenStore) -> None:
    """У отмены свой ключ запроса; исходная заявка — в `orderId`, по нашему UUID.

    Стережёт страницу 12: `clientOrderId` — UUID **запроса на отмену**, `orderId`
    — ключ исходной заявки, `orderIdType` `1`. Мутации: ключ исходной в
    `clientOrderId`; запись в журнал не того ключа, что ушёл.
    """
    wire = Wire()
    journal = Journal(wire)
    target = uuid4()

    async def scenario() -> CommandAccepted:
        async with open_session(store, wire) as broker:
            return await broker.cancel_order(target, record=journal)

    accepted = run(scenario())
    assert journal.sent_before_entry == [0]
    key, request = journal.entries[0]
    assert request == CancelOrder(target)
    body = json.loads(wire.commands()[0].content)
    assert body == {"orderIdType": "1", "orderId": str(target), "clientOrderId": str(key)}
    assert key != target, "ключ запроса на отмену совпал с ключом отменяемой заявки"
    assert accepted.key == key
    assert wire.to(CANCEL), "отмена ушла не по адресу страницы 12"


#: Сбои записи журнала. Заводятся по требованию: готовое исключение, поднятое
#: однажды, держит трассировку до конца прогона (см. `NON_HTTPX_FAILURES`
#: в `tests/test_broker_no_leak.py`).
JOURNAL_FAILURES: dict[str, Callable[[], BaseException]] = {
    "база не записала": lambda: OSError("диск переполнен"),
    "задачу отменили посреди записи": asyncio.CancelledError,
}


@pytest.mark.parametrize("command", sorted(COMMAND_CALLS))
@pytest.mark.parametrize("failure", sorted(JOURNAL_FAILURES))
def test_a_failed_journal_entry_sends_nothing(
    store: TokenStore, command: str, failure: str
) -> None:
    """Запись журнала упала — у транспорта ноль запросов, отказ записи идёт наружу.

    Рабочий токен получен заранее, поэтому «ноль» буквальный: после отказа
    записи к брокеру не уходит ничего. Мутации: глотать исключение записи;
    отправлять, не дождавшись записи; записывать после отправки.
    """
    wire = Wire()
    make = JOURNAL_FAILURES[failure]
    journal = Journal(wire, fail=make)

    async def scenario() -> int:
        async with open_session(store, wire) as broker:
            await broker.access_token()
            before = len(wire.requests)
            with pytest.raises(type(make())):
                await COMMAND_CALLS[command](broker, journal)
            return before

    before = run(scenario())
    assert journal.calls == 1, "журнал не спрошен — проверять нечего"
    assert len(wire.requests) == before, (
        f"{command}, {failure}: после отказа записи к брокеру ушло "
        f"{len(wire.requests) - before} запросов"
    )
    assert wire.commands() == []


# --- исход неизвестен: ровно одна команда ---

_ASKED = httpx.Request("POST", "https://example.invalid/")


def _raising(error: Callable[[], Exception]) -> Reply:
    def reply(request: httpx.Request) -> httpx.Response:
        raise error()

    return reply


INCONCLUSIVE: dict[str, Reply] = {
    "таймаут": _raising(lambda: httpx.ReadTimeout("нет ответа", request=_ASKED)),
    "обрыв": _raising(lambda: httpx.ConnectError("нет сети", request=_ASKED)),
    "500": lambda request: httpx.Response(500, json={"type": "INTERNAL_SERVER_ERROR"}),
    "503": lambda request: httpx.Response(503, text="Service Unavailable"),
    "429": lambda request: httpx.Response(429, json={"type": "RESOURCE_EXHAUSTED"}),
}


@pytest.mark.parametrize("command", sorted(COMMAND_CALLS))
@pytest.mark.parametrize("cause", sorted(INCONCLUSIVE))
def test_an_inconclusive_answer_is_one_command_and_an_unknown_outcome(
    store: TokenStore, command: str, cause: str
) -> None:
    """Таймаут, обрыв, 5xx, 429 на команде — ровно один POST и `OutcomeUnknown`.

    Попыток пять: повтор, если бы он был разрешён, случился бы. Мутации:
    пишущая пара в `REPEATABLE`; `Timeout`/`Throttled` вместо «исход
    неизвестен» (его текст обещает повтор, которого не будет).
    """
    wire = Wire(INCONCLUSIVE[cause])
    journal = Journal(wire)

    async def scenario() -> None:
        async with open_session(store, wire, attempts=5) as broker:
            with pytest.raises(OutcomeUnknown):
                await COMMAND_CALLS[command](broker, journal)

    run(scenario())
    assert len(wire.commands()) == 1, (
        f"{command}, {cause}: команда отправлена {len(wire.commands())} раз — дубль заявки"
    )
    assert len(journal.entries) == 1, "команда ушла без записи в журнал"


ANSWERS_THAT_DO_NOT_CONFIRM: dict[str, Reply] = {
    "чужой ключ": lambda request: httpx.Response(
        200, json={"clientOrderId": str(uuid4()), "status": "OK"}
    ),
    "status не OK": lambda request: httpx.Response(
        200,
        json={"clientOrderId": json.loads(request.content)["clientOrderId"], "status": "ERROR"},
    ),
    "без ключа": lambda request: httpx.Response(200, json={"status": "OK"}),
    "не JSON": lambda request: httpx.Response(200, text="<html>ok</html>"),
    "список вместо объекта": lambda request: httpx.Response(200, json=["OK"]),
}


@pytest.mark.parametrize("answer", sorted(ANSWERS_THAT_DO_NOT_CONFIRM))
def test_an_answer_that_does_not_confirm_this_key_is_an_unknown_outcome(
    store: TokenStore, answer: str
) -> None:
    """200, не подтверждающий приём **этого** ключа, — исход неизвестен, не отказ.

    Ответ 200 пришёл: команда могла быть принята. Сказать «отказ» значит
    разрешить вызывающему подать вторую. Мутации: вернуть `CommandAccepted`
    без сверки ключа; поднять `UnexpectedAnswer` («брокер ответил не так»).
    """
    wire = Wire(ANSWERS_THAT_DO_NOT_CONFIRM[answer])

    async def scenario() -> None:
        async with open_session(store, wire, attempts=5) as broker:
            with pytest.raises(OutcomeUnknown):
                await broker.create_order(MARKET_BUY, record=Journal(wire))

    run(scenario())
    assert len(wire.commands()) == 1


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (400, {"type": "VALIDATION_ERROR"}, BadRequest),
        (401, {"type": "UNAUTHORIZED"}, AuthFailed),
        (404, {"type": "NOT_FOUND"}, NotFound),
    ],
)
def test_a_refusal_the_broker_stated_stays_a_refusal(
    store: TokenStore, status: int, body: dict[str, str], expected: type[BrokerError]
) -> None:
    """400, 401, 404 с кодом брокера — окончательный отказ: заявки нет точно.

    Обратная сторона «исход неизвестен»: пугать остановкой там, где брокер
    сказал «нет», — тот же дефект. 404 у **подачи** — не «нет заявки»:
    «такого нет» читается только у статуса по ключу. Мутации: всякий отказ
    команды — `OutcomeUnknown`; 404 подачи — пустой ответ.
    """
    wire = Wire(lambda request: httpx.Response(status, json=body))

    async def scenario() -> BaseException:
        async with open_session(store, wire, attempts=5) as broker:
            with pytest.raises(BrokerError) as caught:
                await broker.create_order(MARKET_BUY, record=Journal(wire))
            return caught.value

    error = run(scenario())
    assert type(error) is expected, f"{status}: {type(error).__name__} вместо {expected.__name__}"
    assert len(wire.commands()) == 1


# --- права токена ---


@pytest.mark.parametrize("command", sorted(COMMAND_CALLS))
def test_a_read_only_token_sends_no_command_and_records_nothing(
    read_only_store: TokenStore, command: str
) -> None:
    """Токен «только чтение» — `ReadOnlyToken` до единого запроса и до записи.

    Стережёт вызов `require_trade_rights` перед каждой командой (до 27.10.2026
    его не звал никто, `broker/session.py`). Мутации: убрать проверку; звать
    её после записи журнала или после обмена токена.
    """
    wire = Wire()
    journal = Journal(wire)

    async def scenario() -> None:
        async with open_session(read_only_store, wire) as broker:
            with pytest.raises(ReadOnlyToken):
                await COMMAND_CALLS[command](broker, journal)

    run(scenario())
    assert wire.requests == [], f"на токене для чтения ушло {len(wire.requests)} запросов"
    assert journal.calls == 0, "команда на токене для чтения записана в журнал"


# --- своя очередь частоты (С6) ---


class Gate(Throttle):
    """Ведро чтения, которое тест закрывает намертво: догрузка заняла очередь."""

    def __init__(self) -> None:
        super().__init__(rate=1000.0)
        self.shut = False

    async def acquire(self) -> None:
        if self.shut:
            await asyncio.Event().wait()
        await super().acquire()


def test_a_command_does_not_queue_behind_the_reading_bucket(store: TokenStore) -> None:
    """Ведро чтения стоит — команда и статус по ключу всё равно уходят.

    Находка С6: в общей очереди 5/с заявка ждала бы догрузку десятки секунд.
    Сначала доказывается, что ведро чтения действительно стоит (чтение
    портфеля не завершается), иначе проверка пустая. Мутация: брать для
    команды общее ведро.
    """
    wire = Wire()
    gate = Gate()

    async def scenario() -> tuple[bool, CommandAccepted, OrderState | None]:
        async with open_session(store, wire, throttle=gate) as broker:
            await broker.access_token()
            gate.shut = True
            reading = asyncio.create_task(broker.read(PORTFOLIO_PATH))
            for _ in range(5):
                await asyncio.sleep(0)
            accepted = await asyncio.wait_for(
                broker.create_order(MARKET_BUY, record=Journal(wire)), timeout=2.0
            )
            status = await asyncio.wait_for(broker.order_status(accepted.key), timeout=2.0)
            stuck = not reading.done()
            reading.cancel()
            with pytest.raises(asyncio.CancelledError):
                await reading
            return stuck, accepted, status

    stuck, accepted, status = run(scenario())
    assert stuck, "ведро чтения не стояло: проверять было нечего"
    assert len(wire.commands()) == 1
    assert status is not None
    assert wire.to(("GET", PORTFOLIO_PATH)) == [], (
        "чтение прошло сквозь закрытое ведро — подставное ведро не работает"
    )


# --- read() команды не пропускает ---


@pytest.mark.parametrize("endpoint", sorted(COMMANDS | {EDIT}))
def test_read_does_not_let_a_command_through(
    store: TokenStore, endpoint: tuple[str, str]
) -> None:
    """`read()` пишущую пару не отправляет — ни подачу, ни отмену, ни изменение.

    Пары — со страниц 12, 13, 14. Проверяется и отказ, и что ничего не ушло.
    Мутация: пишущая пара в `READ_ENDPOINTS`.
    """
    method, path = endpoint
    wire = Wire()

    async def scenario() -> str:
        async with open_session(store, wire) as broker:
            with pytest.raises(RuntimeError) as caught:
                await broker.read(path, method=method)
            return str(caught.value)

    said = run(scenario())
    assert wire.requests == [], f"{method} {path} ушёл через read()"
    assert path in said, f"отказ не называет адрес: {said}"


# --- статус по ключу ---


def test_order_status_asks_by_our_key_and_reads_the_latest_message(
    store: TokenStore,
) -> None:
    """Статус — `GET` с `orderIdType=1` и нашим UUID; последнее — по времени, не по месту.

    Порядок массива `data` документация не описывает (план §9). Мутация:
    брать последний элемент массива.
    """
    key = uuid4()
    later = report(
        orderStatus="2",
        executionType="11",
        executionId="E-2",
        executedQuantity=1,
        lastQuantity=1,
        remainedQuantity=0,
        averagePrice=231000.0,
        transactionTime="2026-10-27T07:00:05.000Z",
    )
    earlier = report(transactionTime="2026-10-27T07:00:01.000Z")

    def reply(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"clientOrderId": str(key), "data": [later, earlier]})

    wire = Wire(reply)

    async def scenario() -> OrderState | None:
        async with open_session(store, wire) as broker:
            return await broker.order_status(key)

    state = run(scenario())
    asked = wire.to(STATUS)
    assert len(asked) == 1
    assert dict(asked[0].url.params) == {"orderIdType": "1", "orderId": str(key)}
    assert state is not None
    assert state.latest.status is OrderStatus.FILLED, "последним взят не самый поздний"
    assert state.latest.execution_id == "E-2"


def test_a_404_from_the_broker_means_no_such_order(store: TokenStore) -> None:
    """404 с кодом брокера `NOT_FOUND` у статуса — «заявки нет», значением `None`.

    И адрес после этого не гасится: второй вопрос снова идёт к брокеру.
    Мутации: 404 статуса — отказ; 404 статуса гасит адрес как неверный.
    """
    wire = Wire(lambda request: httpx.Response(404, json={"type": "NOT_FOUND", "traceId": "t"}))

    async def scenario() -> tuple[OrderState | None, OrderState | None, bool]:
        async with open_session(store, wire) as broker:
            first = await broker.order_status(uuid4())
            second = await broker.order_status(uuid4())
            return first, second, broker.address_is_wrong(STATUS[1])

    first, second, gone = run(scenario())
    assert first is None and second is None
    assert len(wire.to(STATUS)) == 2, "после ответа «нет заявки» адрес перестали спрашивать"
    assert gone is False


@pytest.mark.parametrize(
    "body",
    [
        {"type": "NOT_FOUND", "errors": [{"type": "USER_NOT_FOUND"}]},
        {"code": "USER_NOT_FOUND", "type": "NOT_FOUND"},
        {"type": "FORBIDDEN"},
    ],
)
def test_a_404_that_is_not_about_the_order_is_a_refusal(
    store: TokenStore, body: dict[str, object]
) -> None:
    """404 с `USER_NOT_FOUND` или другим кодом — не «заявки нет», а отказ.

    «Брокер не знает счёта», прочитанное как «заявки нет», разрешило бы
    вызывающему подать заявку заново. Мутация: любой 404 с телом брокера —
    «нет заявки».
    """
    wire = Wire(lambda request: httpx.Response(404, json=body))

    async def scenario() -> None:
        async with open_session(store, wire, attempts=1) as broker:
            with pytest.raises(NotFound):
                await broker.order_status(uuid4())

    run(scenario())


def test_a_404_from_the_gateway_on_status_is_a_wrong_address(store: TokenStore) -> None:
    """404 страницей шлюза у статуса — `WrongAddress`, как у чтения свечей.

    Мутация: проверять «нет заявки» раньше ответа шлюза — страница без кода
    брокера стала бы «заявки нет».
    """
    wire = Wire(lambda request: httpx.Response(404, text=GATEWAY_404))

    async def scenario() -> bool:
        async with open_session(store, wire, attempts=1) as broker:
            with pytest.raises(WrongAddress):
                await broker.order_status(uuid4())
            return broker.address_is_wrong(STATUS[1])

    assert run(scenario()) is True


def test_a_command_is_not_sent_where_the_gateway_disowned_the_address(
    store: TokenStore,
) -> None:
    """Шлюз не знает адреса заявок — подача по нему не уходит и не пишется в журнал.

    Подача и статус живут по одному адресу (11, 13): ответ шлюза на статус
    значит, что и подача туда не дойдёт. Мутация: пишущий путь без проверки
    погашенного адреса.
    """
    wire = Wire(lambda request: httpx.Response(404, text=GATEWAY_404))
    journal = Journal(wire)

    async def scenario() -> WrongAddress:
        async with open_session(store, wire, attempts=1) as broker:
            with pytest.raises(WrongAddress):
                await broker.order_status(uuid4())
            with pytest.raises(WrongAddress) as caught:
                await broker.create_order(MARKET_BUY, record=journal)
            return caught.value

    error = run(scenario())
    assert error.repeated is True
    assert wire.commands() == []
    assert journal.calls == 0


def test_a_lost_status_answer_is_repeated_and_is_not_an_unknown_outcome(
    store: TokenStore,
) -> None:
    """Статус по ключу — чтение: повторяется и кончается `Timeout`, не «исход неизвестен».

    Мутация: забыть статус в `REPEATABLE` — потеря ответа на чтении
    остановила бы робота ручной остановкой (`D-086`).
    """
    wire = Wire(_raising(lambda: httpx.ReadTimeout("долго", request=_ASKED)))

    async def scenario() -> None:
        async with open_session(store, wire, attempts=3) as broker:
            with pytest.raises(Timeout):
                await broker.order_status(uuid4())

    run(scenario())
    assert len(wire.to(STATUS)) == 3


# --- разбор: коды по таблице ---


def documented_codes(page: str, field: str) -> set[str]:
    """Коды поля со страницы снимка: строка поля и строки-продолжения под ней."""
    lines = (BROKER_API_DOCS / page).read_text(encoding="utf-8").splitlines()
    for number, line in enumerate(lines):
        if f"**{field}**" not in line and f"`{field}`" not in line:
            continue
        block = [line]
        for follower in lines[number + 1 :]:
            if not follower.startswith("  "):
                break
            block.append(follower)
        codes = set(re.findall(r"`(\d+)`", " ".join(block)))
        if codes:
            return codes
    raise AssertionError(f"{page}: кодов поля {field} не нашлось — разбор снимка сломан")


@pytest.mark.parametrize("page", ["11-get-order-by-id.md", "24-ws-order-status.md"])
def test_every_documented_code_is_known_and_nothing_else(page: str) -> None:
    """Таблицы кодов совпадают с документацией: ни пропуска, ни выдумки.

    Вход — страницы снимка, не наш код. Пропущенный код сделал бы настоящий
    ответ брокера «отказом разбора», лишний — разрешил бы недокументированный.
    """
    assert {status.value for status in OrderStatus} == documented_codes(page, "orderStatus")
    assert {kind.value for kind in ExecutionType} == documented_codes(page, "executionType")


def test_every_status_has_words_for_the_owner() -> None:
    """Каждое состояние заявки названо словами: журнал читает человек, не код."""
    assert set(STATUS_WORDS) == set(OrderStatus)
    assert all(words.strip() for words in STATUS_WORDS.values())


@pytest.mark.parametrize("code", ["3", "7", "14", "NEW", "", 2.0, True, None])
def test_an_unknown_order_status_is_refused_not_guessed(code: object) -> None:
    """Незнакомый, пустой или не тот по виду `orderStatus` — отказ разбора.

    «Отклонена», прочитанная как «Новая», — вход, которого движок ждёт вечно.
    Мутации: незнакомый код → `None` или ближайший знакомый; отсутствующий
    статус → «новая».
    """
    data = report()
    if code is None:
        del data["orderStatus"]
    else:
        data["orderStatus"] = code
    with pytest.raises(UnexpectedAnswer):
        parse_report(data)


def test_an_unknown_status_in_the_status_answer_is_refused(store: TokenStore) -> None:
    """Тот же отказ доходит через `order_status`, а не теряется по дороге."""
    wire = Wire(
        lambda request: httpx.Response(
            200, json={"clientOrderId": "x", "data": [report(orderStatus="7")]}
        )
    )

    async def scenario() -> None:
        async with open_session(store, wire) as broker:
            with pytest.raises(UnexpectedAnswer):
                await broker.order_status(uuid4())

    run(scenario())


@pytest.mark.parametrize(
    "payload",
    [
        {"clientOrderId": "x", "data": []},
        {"clientOrderId": "x"},
        {"clientOrderId": "x", "data": {"orderStatus": "0"}},
    ],
    ids=["пустой-data", "без-data", "data-не-массив"],
)
def test_a_status_answer_without_messages_is_refused_not_read_as_no_order(
    store: TokenStore, payload: dict[str, object]
) -> None:
    """200 статуса без сообщений о заявке — отказ разбора, а не `None`.

    `None` у `order_status` значит «заявки у брокера нет», и исполнитель
    вправе подать её снова: «нет заявки» — только 404 `NOT_FOUND` (11).
    Мутация, выжившая до этого теста (test-expert, 27.10.2026): пустой
    `data` → `None`.
    """
    wire = Wire(lambda request: httpx.Response(200, json=payload))

    async def scenario() -> None:
        async with open_session(store, wire) as broker:
            with pytest.raises(UnexpectedAnswer):
                await broker.order_status(uuid4())

    run(scenario())


def documented_event() -> dict[str, object]:
    """Пример события со страницы 24 — как он там напечатан."""
    page = (BROKER_API_DOCS / "24-ws-order-status.md").read_text(encoding="utf-8")
    found = re.search(r"```json\n(.*?)\n```", page, re.DOTALL)
    assert found is not None, "в странице 24 нет примера события"
    parsed = json.loads(found.group(1))
    assert isinstance(parsed, dict)
    return parsed


def test_the_documented_event_parses_with_the_key_from_the_original_id() -> None:
    """Пример страницы 24: `clientOrderId` пуст — ключ из `originalClientOrderId`.

    Пример — исполнение с `executionType` `2`, а не `11`: оно обязано читаться
    исполнением, иначе сделка теряется. Мутации: ключ только из
    `clientOrderId`; исполнение — только `11`.
    """
    message = documented_event()
    event = parse_order_event(message)
    assert event.client_order_id is None
    assert event.key == message["originalClientOrderId"]
    assert event.report.is_execution, "исполнение из примера документации не узнано"
    assert event.report.execution_id == "TQBR-Z3fE7c-S-1-1-N"
    assert event.report.status is OrderStatus.FILLED
    assert (event.report.last_quantity, event.report.executed_quantity) == (50, 100)
    assert event.report.average_price == 244.5
    assert event.report.commission == 12.3
    assert event.report.transaction_time == datetime(2024, 10, 30, 9, 1, tzinfo=timezone.utc)


def test_a_filled_client_order_id_wins_over_the_original() -> None:
    """Оба ключа заполнены — ключ из `clientOrderId`, оба сохранены для сверки."""
    ours, theirs = uuid4(), uuid4()
    event = parse_order_event(
        {
            "clientOrderId": str(ours).upper(),
            "originalClientOrderId": str(theirs),
            "data": report(),
        }
    )
    assert event.key == str(ours), "ключ не приведён к виду, в каком его пишет программа"
    assert event.original_client_order_id == str(theirs)


@pytest.mark.parametrize("kind", ["1", "2", "11"])
def test_an_execution_without_its_id_is_refused(kind: str) -> None:
    """Исполнение без `executionId` — отказ: дубли сделок нечем отсечь (план §2.10).

    Мутация: пропускать исполнение с пустым `executionId`.
    """
    with pytest.raises(UnexpectedAnswer):
        parse_report(report(executionType=kind, orderStatus="2"))
    parsed = parse_report(report(executionType=kind, orderStatus="2", executionId="E-1"))
    assert parsed.is_execution


@pytest.mark.parametrize("kind", ["0", "4", "8", "12"])
def test_a_status_change_needs_no_execution_id(kind: str) -> None:
    """Смена статуса без `executionId` — обычное событие, а не исполнение."""
    parsed = parse_report(report(executionType=kind))
    assert not parsed.is_execution
    assert parsed.execution_id is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("orderQuantity", 1.5),
        ("orderQuantity", "abc"),
        ("executedQuantity", "nan"),
        ("averagePrice", "abc"),
        ("averagePrice", math.inf),
        ("commission", True),
        ("transactionTime", "2026-10-27T07:00:00"),
        ("side", "3"),
        ("orderType", "9"),
        # Код не строкой и не целым — тоже испорчен, а не пуст (test-expert
        # 27.10.2026: мутация «такой код → `None`» прежде выживала — у
        # обязательного `orderStatus` её прятал отказ «нет поля»).
        ("side", True),
        ("orderType", 2.5),
    ],
)
def test_a_spoiled_value_is_refused_not_emptied(field: str, value: object) -> None:
    """Поле есть, но испорчено — отказ, а не `None`: пустое вместо испорченного — подмена.

    Наивное время тоже испорчено: пояс не угадывается (`parsing.aware_moment`).
    """
    with pytest.raises(UnexpectedAnswer):
        parse_report(report(**{field: value}))


def test_a_missing_value_is_empty_not_zero() -> None:
    """Поля нет — `None`, а не ноль: «комиссии нет» не равно «комиссия ноль»."""
    data = report()
    for field in ("orderQuantity", "executedQuantity", "side", "orderType", "transactionTime"):
        del data[field]
    parsed = parse_report(data)
    assert parsed.commission is None and parsed.average_price is None
    assert parsed.order_quantity is None and parsed.executed_quantity is None
    assert parsed.side is None and parsed.order_type is None
    assert parsed.transaction_time is None


# --- тело команды ---


def documented_fields(page: str) -> set[str]:
    """Имена полей тела запроса — первая колонка таблицы страницы снимка."""
    text = (BROKER_API_DOCS / page).read_text(encoding="utf-8")
    return set(re.findall(r"^\| \**`(\w+)`\** \|", text, re.MULTILINE))


def test_the_create_body_uses_only_documented_fields() -> None:
    """Тело создания: поля из таблицы страницы 13 и коды из её же описаний."""
    key = uuid4()
    market = MARKET_BUY.body(key)
    limit = LIMIT_SELL.body(key)
    allowed = documented_fields("13-create-order.md")
    assert {"clientOrderId", "orderQuantity", "classCode", "price"} <= allowed, (
        "разбор таблицы страницы 13 сломан"
    )
    assert set(market) <= allowed and set(limit) <= allowed
    assert market == {
        "clientOrderId": str(key),
        "side": "1",
        "orderType": "1",
        "timeInForce": "1",
        "orderQuantity": 1,
        "ticker": "MXZ6",
        "classCode": "SPBFUT",
    }, "у рыночной заявки лишнее или недостающее поле"
    assert (limit["side"], limit["orderType"], limit["price"]) == ("2", "2", 231450.0)


def test_the_cancel_body_uses_only_documented_fields() -> None:
    """Тело отмены: ровно три поля страницы 12."""
    assert set(CancelOrder(uuid4()).body(uuid4())) == documented_fields("12-cancel-order.md")


@pytest.mark.parametrize(
    "changes",
    [
        {"price": 1.0},
        {"order_type": OrderType.LIMIT},
        {"order_type": OrderType.LIMIT, "price": 0.0},
        {"order_type": OrderType.LIMIT, "price": math.nan},
        {"order_type": OrderType.LIMIT, "price": math.inf},
        {"quantity": 0},
        {"quantity": -1},
        {"quantity": 1.5},
        {"quantity": True},
        {"order_type": OrderType.CONDITIONAL, "price": 1.0},
        {"time_in_force": TimeInForce.GOOD_TILL_DATE},
        {"ticker": ""},
        {"class_code": " SPBFUT"},
    ],
)
def test_an_order_that_cannot_be_sent_is_refused_before_the_wire(
    changes: dict[str, Any],
) -> None:
    """Заявка, которую брокер не примет или которую мы не вправе подать, — `ValueError`.

    Дробный объём, цена у рыночной, условная заявка — отказ до сети, а не на
    подаче (план §6). Мутация: убрать любое звено цепочки проверок.
    """
    with pytest.raises(ValueError, match="заявка не годится"):
        replace(MARKET_BUY, **changes)


# --- списки заявок и сделок ---


def test_the_orders_search_sends_its_filter_and_reads_the_rows(store: TokenStore) -> None:
    """Список заявок: `POST` с фильтром в теле, страница — в строке запроса (15).

    Сторона и состояние в теле — числа, время — UTC с `Z`. Мутация:
    состояние строкой; страница в теле.
    """
    rows = [
        {"orderNum": 7, "orderId": "261027-SPBFUT-7", "orderStatus": 3, "side": 2,
         "orderType": 2, "orderQuantity": 1.0, "price": 231450.0, "ticker": "MXZ6"},
    ]

    def reply(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"records": rows, "totalRecords": 1, "totalPages": 1})

    wire = Wire(reply)
    since = datetime(2026, 10, 27, 10, 0, tzinfo=timezone(timedelta(hours=3)))
    query = OrdersQuery(
        start=since, side=OrderSide.SELL, statuses=(ListedOrderStatus.ACTIVE,),
        tickers=("MXZ6",), size=100,
    )

    async def scenario() -> OrdersPage:
        async with open_session(store, wire) as broker:
            return await broker.search_orders(query)

    page = run(scenario())
    asked = wire.to(ORDERS_SEARCH)
    assert len(asked) == 1
    assert dict(asked[0].url.params) == {"page": "0", "size": "100"}
    assert json.loads(asked[0].content) == {
        "startDateTime": "2026-10-27T07:00:00Z",
        "side": 2,
        "orderStatus": [3],
        "tickers": ["MXZ6"],
    }
    assert page.records[0].status is ListedOrderStatus.ACTIVE
    assert page.records[0].side is OrderSide.SELL
    assert page.records[0].order_quantity == 1


@pytest.mark.parametrize("row", [{"orderStatus": 4}, {"orderStatus": "Активна"}, {}])
def test_an_unknown_state_in_the_orders_list_is_refused(
    store: TokenStore, row: dict[str, object]
) -> None:
    """Состояние строки списка вне `1`, `2`, `3` (15) или без него — отказ разбора.

    Строка без состояния, прочитанная как «Активна», заставила бы снимать
    заявку, которой нет; как «Снята» — забыть живой тейк. Мутация: умолчание
    вместо отказа.
    """
    wire = Wire(lambda request: httpx.Response(200, json={"records": [row]}))

    async def scenario() -> None:
        async with open_session(store, wire) as broker:
            with pytest.raises(UnexpectedAnswer):
                await broker.search_orders(OrdersQuery())

    run(scenario())


#: Два списка одним видом: имя для отчёта и вызов на сессии.
LIST_CALLS = {
    "заявки": lambda broker: broker.search_orders(OrdersQuery()),
    "сделки": lambda broker: broker.search_trades(TradesQuery()),
}


@pytest.mark.parametrize("listing", sorted(LIST_CALLS))
@pytest.mark.parametrize(
    "payload",
    [{"totalRecords": 0}, {"records": None}, {"records": {"orderNum": 1}}, []],
    ids=["без-records", "records-null", "records-объект", "массив-вместо-объекта"],
)
def test_a_list_answer_without_its_rows_is_refused_not_read_as_empty(
    store: TokenStore, listing: str, payload: object
) -> None:
    """Ответ списка без массива `records` — отказ, а не пустая страница.

    Пустая страница — «у брокера нет ни заявок, ни сделок»: сверка после
    обрыва (план §5) забыла бы живой тейк. Мутация, выжившая до этого теста
    (test-expert, 27.10.2026): нет `records` → пустой список.
    """
    wire = Wire(lambda request: httpx.Response(200, json=payload))

    async def scenario() -> None:
        async with open_session(store, wire) as broker:
            with pytest.raises(UnexpectedAnswer):
                await LIST_CALLS[listing](broker)

    run(scenario())


def test_the_trades_search_sends_the_side_as_text(store: TokenStore) -> None:
    """Список сделок: сторона в теле — строка (16), не число, как в списке заявок."""
    rows = [{"tradeNum": 11, "orderNum": 7, "side": "1", "tradeQuantity": 1.0,
             "price": 231000.0, "go": 25000.0, "tradeDateTime": "2026-10-27T07:00:05Z"}]
    wire = Wire(lambda request: httpx.Response(200, json={"records": rows}))

    async def scenario() -> TradesPage:
        async with open_session(store, wire) as broker:
            return await broker.search_trades(TradesQuery(side=OrderSide.BUY))

    page = run(scenario())
    assert json.loads(wire.to(TRADES_SEARCH)[0].content) == {"side": "1"}
    record = page.records[0]
    assert (record.trade_num, record.quantity, record.margin) == (11, 1, 25000.0)


@pytest.mark.parametrize(
    "changes",
    [
        {"start": datetime(2026, 10, 27, 10, 0)},  # без пояса нарочно
        {"page": -1},
        {"size": 0},
        {"size": 101},
    ],
)
def test_a_list_query_refuses_naive_time_and_bad_paging(changes: dict[str, Any]) -> None:
    """Наивное время и страница вне 0…, размер вне 1…100 (15, 16) — отказ до сети."""
    with pytest.raises(ValueError):
        OrdersQuery(**changes)
    with pytest.raises(ValueError):
        TradesQuery(**changes)


# --- пишущий путь: только подача и отмена ---


@pytest.mark.parametrize(
    "endpoint",
    [EDIT, ("POST", PORTFOLIO_PATH), ("POST", "https://example.invalid" + CREATE[1])],
    ids=["изменение-заявки", "чтение-портфеля", "полный-адрес-подачи"],
)
def test_the_write_path_refuses_anything_but_create_and_cancel(
    store: TokenStore, endpoint: tuple[str, str]
) -> None:
    """Пишущий путь по адресу вне `WRITE_ENDPOINTS` — отказ до сети и до журнала.

    Сторож стоит на случай, когда шаг 2 заведёт новую команду по образцу
    соседней (`/orders/edit`, план §2.8, — «не заводить»). Публичные методы
    его не задевают: адрес у них зашит, поэтому вызов внутренний. Мутация,
    выжившая до этого теста (test-expert, 27.10.2026): сторож пропускает всё.
    """
    _, path = endpoint
    wire = Wire()
    journal = Journal(wire)

    async def scenario() -> str:
        async with open_session(store, wire) as broker:
            with pytest.raises(RuntimeError) as caught:
                # Внутренний вызов: публичные методы передают только свои адреса,
                # и до сторожа иначе не дотянуться.
                await vars(BrokerSession)["_command"](broker, path, MARKET_BUY, journal)
            return str(caught.value)

    said = run(scenario())
    assert wire.requests == [], f"по {path} ушло {len(wire.requests)} запросов"
    assert journal.calls == 0, "команда по чужому адресу записана в журнал"
    assert path in said, f"отказ не называет адрес: {said}"


# --- списки допуска против документации ---


def test_the_write_list_is_create_and_cancel_of_the_documentation() -> None:
    """Пишущий список — ровно подача (13) и отмена (12); изменения (14) нет нигде.

    Вход — страницы снимка. Мутации: добавить изменение заявки; переписать
    адрес отмены на сервис операций.
    """
    assert set(WRITE_ENDPOINTS) == {
        endpoint_from_docs("13-create-order.md"),
        endpoint_from_docs("12-cancel-order.md"),
    }
    edit = endpoint_from_docs("14-update-order.md")
    assert edit not in WRITE_ENDPOINTS and edit not in READ_ENDPOINTS


@pytest.mark.parametrize(
    "page", ["11-get-order-by-id.md", "15-orders-search.md", "16-trades-search.md"]
)
def test_reading_around_orders_is_allowed_and_repeated(page: str) -> None:
    """Статус и оба списка (11, 15, 16) — в чтении и в повторах; команд — ни в одном."""
    endpoint = endpoint_from_docs(page)
    assert endpoint in READ_ENDPOINTS
    assert endpoint in REPEATABLE
    assert not set(WRITE_ENDPOINTS) & set(READ_ENDPOINTS)
