"""Поток статусов заявок: Э4, шаг 1, фаза 2 (`broker/order_stream.py`).

В сеть тесты не ходят. Рукопожатие подменено (`websockets.connect`), кроме
одного теста, где настоящая библиотека говорит с молчащим сервером на
127.0.0.1 в этом же процессе. Токены подставные.

Ожидаемое, где можно, — со страниц снимка `.docs/broker-api/` (24, 28):
адрес сокета, пример события, период ping сервера.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import math
import re
import time
import traceback
from collections.abc import Callable, Coroutine
from datetime import datetime, timezone
from typing import Any, TypeVar

import pytest
import websockets
from websockets.datastructures import Headers
from websockets.http11 import Response

from broker.errors import (
    AuthFailed,
    BrokerError,
    NoConnection,
    ServerFailure,
    UnexpectedAnswer,
)
from broker.order_stream import (
    ORDERS_WS,
    SILENCE_LIMIT,
    OrderEvents,
    OrderStreamRefused,
    order_events,
)
from broker.orders import OrderEvent
from broker.redaction import LOGGER_NAME
from broker.secret import Secret
from broker.tokens import AccessToken, TokenScope
from tests.test_broker_session import BROKER_API_DOCS

FAKE_FIRST = "FAKE-orders-ws-1111-2222-NOT-A-REAL-TOKEN"
FAKE_SECOND = "FAKE-orders-ws-3333-4444-NOT-A-REAL-TOKEN"
EXPIRES = datetime(2026, 10, 28, 7, 0, tzinfo=timezone.utc)

_Result = TypeVar("_Result")


def run(coroutine: Coroutine[object, object, _Result]) -> _Result:
    return asyncio.run(coroutine)


class Desk:
    """Сессия-заглушка: на каждый вызов — следующий рабочий токен из списка."""

    def __init__(self, *values: str) -> None:
        self.values = list(values) or [FAKE_FIRST]
        self.handed: list[str] = []
        self.forgotten = 0

    async def access_token(self) -> AccessToken:
        value = self.values[min(len(self.handed), len(self.values) - 1)]
        self.handed.append(value)
        return AccessToken(secret=Secret(value), expires_at=EXPIRES, scope=TokenScope.TRADE)

    def forget_access_token(self) -> None:
        self.forgotten += 1


class FakeSocket:
    """Сокет-заглушка: отдаёт кадры, потом поднимает `end` или молчит вечно."""

    def __init__(
        self,
        *frames: str | bytes,
        end: Callable[[], BaseException] | None = None,
        pong: bool = True,
    ) -> None:
        self.frames = list(frames)
        self.end = end
        self.pong = pong
        self.reads = 0

    async def recv(self) -> str | bytes:
        self.reads += 1
        if self.frames:
            return self.frames.pop(0)
        if self.end is not None:
            raise self.end()
        await asyncio.Event().wait()
        raise AssertionError("молчащий сокет проснулся")

    async def ping(self) -> asyncio.Future[float]:
        answer: asyncio.Future[float] = asyncio.get_running_loop().create_future()
        if self.pong:
            answer.set_result(0.01)
        return answer


Outcome = Callable[[], "FakeSocket | BaseException"]


class _Entered:
    """Контекст рукопожатия: отдаёт сокет или отказывает на входе, как библиотека."""

    def __init__(self, outcome: FakeSocket | BaseException) -> None:
        self.outcome = outcome

    async def __aenter__(self) -> FakeSocket:
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    async def __aexit__(self, *unused: object) -> bool:
        return False


class Handshakes:
    """Подставной `websockets.connect`. Аргументы — как у библиотеки, поимённо.

    Каждый исход заводится по требованию: готовое исключение, поднятое
    дважды, копит трассировки, а в кадрах слоя живут заголовки.
    """

    def __init__(self, *outcomes: Outcome) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        url: str,
        *,
        additional_headers: dict[str, str],
        logger: logging.Logger,
        ping_interval: float,
        ping_timeout: float,
        close_timeout: float,
    ) -> _Entered:
        self.calls.append(
            {
                "url": url,
                "headers": additional_headers,  # сам словарь: проверяется стирание
                "at_handshake": dict(additional_headers),
                "logger": logger,
                "keepalive": (ping_interval, ping_timeout, close_timeout),
            }
        )
        index = min(len(self.calls) - 1, len(self.outcomes) - 1)
        return _Entered(self.outcomes[index]())


def rejected(status: int) -> websockets.exceptions.InvalidStatus:
    """Отказ рукопожатия HTTP-кодом."""
    return websockets.exceptions.InvalidStatus(Response(status, "", Headers()))


def dropped() -> websockets.exceptions.ConnectionClosedError:
    return websockets.exceptions.ConnectionClosedError(None, None)


def closed_by_broker() -> websockets.exceptions.ConnectionClosedOK:
    return websockets.exceptions.ConnectionClosedOK(None, None)


def event_frame(**data: object) -> str:
    """Событие в форме страницы 24: новая заявка без исполнения."""
    body: dict[str, object] = {"messageType": "8", "orderStatus": "0", "executionType": "0"}
    body.update(data)
    return json.dumps(
        {
            "originalClientOrderId": "",
            "clientOrderId": "517661df-d051-461d-9389-988abf24de4d",
            "data": body,
        }
    )


@pytest.fixture
def handshakes(monkeypatch: pytest.MonkeyPatch) -> Callable[..., Handshakes]:
    """Поставить подставное рукопожатие с заданными исходами."""

    def install(*outcomes: Outcome) -> Handshakes:
        fake = Handshakes(*outcomes)
        monkeypatch.setattr(websockets, "connect", fake)
        return fake

    return install


async def first_event(desk: Desk, silence_limit: float = SILENCE_LIMIT) -> OrderEvent:
    async with order_events(desk, silence_limit=silence_limit) as events:
        return await anext(events)


# --- адрес и рукопожатие ---


def test_the_socket_is_the_documented_one_and_asks_for_nothing(handshakes) -> None:
    """Адрес — со страницы 24, токен — в заголовке, подписки не посылается.

    «Подписки нет — сервер шлёт события сам» (24): подставной сокет не умеет
    `send`, и посланная подписка уронила бы тест. Логгер рукопожатия — под
    `broker`, иначе чистка его не накроет. Мутации: другой адрес; логгер
    библиотеки по умолчанию.
    """
    page = (BROKER_API_DOCS / "24-ws-order-status.md").read_text(encoding="utf-8")
    documented = re.findall(r"^(wss://\S+)$", page, re.MULTILINE)
    assert documented == [ORDERS_WS], f"адрес сокета разошёлся со страницей 24: {documented}"
    fake = handshakes(lambda: FakeSocket(event_frame()))
    run(first_event(Desk()))
    call = fake.calls[0]
    assert call["url"] == ORDERS_WS
    assert call["at_handshake"] == {"Authorization": "Bearer " + FAKE_FIRST}
    assert call["logger"].name.split(".")[0] == LOGGER_NAME


async def _cancel_while_reading(desk: Desk) -> None:
    async with order_events(desk) as events:
        reading = asyncio.ensure_future(anext(events))
        await asyncio.sleep(0)
        reading.cancel()
        await reading


#: Пути выхода из потока: исход рукопожатия и что делает потребитель.
WAYS_OUT: dict[str, tuple[Outcome, Callable[[Desk], Coroutine[Any, Any, object]], type]] = {
    "штатный выход": (lambda: FakeSocket(event_frame()), first_event, type(None)),
    "рукопожатие 401": (lambda: rejected(401), first_event, AuthFailed),
    "рукопожатие 503": (lambda: rejected(503), first_event, ServerFailure),
    "сети нет": (lambda: OSError("нет маршрута до узла"), first_event, NoConnection),
    "обрыв посреди чтения": (lambda: FakeSocket(end=dropped), first_event, NoConnection),
    "отмена задачи посреди чтения": (
        FakeSocket,
        _cancel_while_reading,
        asyncio.CancelledError,
    ),
}


def _frames_with(error: BaseException, secret: str) -> list[str]:
    rendered = "".join(
        traceback.TracebackException.from_exception(error, capture_locals=True).format()
    )
    return [line.strip() for line in rendered.splitlines() if secret in line]


@pytest.mark.parametrize("way", sorted(WAYS_OUT))
def test_the_header_is_wiped_on_every_way_out(handshakes, way: str) -> None:
    """Словарь заголовков пуст после выхода — на всех путях; в кадрах токена нет.

    При рукопожатии заголовок был настоящим (иначе проверять нечего).
    Мутация: убрать `headers.clear()` из `finally` в
    `broker/stream.py::authorized_socket`.
    """
    outcome, body, expected = WAYS_OUT[way]
    fake = handshakes(outcome)

    async def scenario() -> BaseException | None:
        try:
            await body(Desk())
        except BaseException as error:  # noqa: BLE001 — ловим любой, и отмену тоже
            return error
        return None

    error = run(scenario())
    assert isinstance(error, expected), f"{way}: вышло {error!r}"
    call = fake.calls[0]
    assert call["at_handshake"] == {"Authorization": "Bearer " + FAKE_FIRST}
    assert call["headers"] == {}, f"{way}: заголовок с токеном не стёрт"
    if error is not None:
        assert not _frames_with(error, FAKE_FIRST), f"{way}: токен в переменных кадра"


# --- события ---


def documented_event() -> str:
    """Пример события со страницы 24 — кадр, как он там напечатан."""
    page = (BROKER_API_DOCS / "24-ws-order-status.md").read_text(encoding="utf-8")
    found = re.search(r"```json\n(.*?)\n```", page, re.DOTALL)
    assert found is not None, "в странице 24 нет примера события"
    return found.group(1)


def test_the_documented_event_comes_through_with_the_key_of_the_original(handshakes) -> None:
    """Пример 24: `clientOrderId` пуст — ключ из `originalClientOrderId`; это исполнение.

    Мутация: разбирать кадр мимо `parse_order_event` или брать ключ только
    из `clientOrderId`.
    """
    frame = documented_event()
    handshakes(lambda: FakeSocket(frame))
    event = run(first_event(Desk()))
    assert event.key == json.loads(frame)["originalClientOrderId"]
    assert event.report.is_execution
    assert event.report.execution_id == "TQBR-Z3fE7c-S-1-1-N"


def test_an_execution_without_an_id_is_a_refusal_not_a_skip(handshakes) -> None:
    """Исполнение без `executionId` — отказ на нём, а не следующее событие.

    Пропуск значил бы потерянную сделку. Мутация: ловить отказ разбора
    и читать дальше.
    """
    bad = event_frame(orderStatus="2", executionType="11", lastQuantity=1)
    handshakes(lambda: FakeSocket(bad, event_frame()))
    with pytest.raises(UnexpectedAnswer):
        run(first_event(Desk()))


NOT_EVENTS: dict[str, tuple[str | bytes, type[BrokerError]]] = {
    "текст ping": ("ping", UnexpectedAnswer),
    "байты не UTF-8": (b"\xff\xfe\x00", UnexpectedAnswer),
    "массив": ("[1, 2]", UnexpectedAnswer),
    "объект без data": ('{"clientOrderId": "x"}', UnexpectedAnswer),
    "незнакомый статус": (event_frame(orderStatus="7"), UnexpectedAnswer),
    "незнакомый вид события": (event_frame(executionType="14"), UnexpectedAnswer),
    "отказ брокера": ('{"errors": [{"type": "UNAUTHORIZED"}]}', OrderStreamRefused),
}


@pytest.mark.parametrize("kind", sorted(NOT_EVENTS))
def test_a_frame_that_is_not_an_event_is_refused(handshakes, kind: str) -> None:
    """Незнакомый кадр или код — отказ разбора, не пропуск и не подмена.

    Служебных кадров на странице 24 нет: подписки нет, ответа на неё тоже.
    Мутация: незнакомый кадр → пропустить.
    """
    frame, expected = NOT_EVENTS[kind]
    handshakes(lambda: FakeSocket(frame, event_frame()))
    with pytest.raises(expected) as caught:
        run(first_event(Desk()))
    assert "ping" not in caught.value.human, "содержимое кадра доехало до текста человеку"


# --- обрыв и молчание ---

DROPS: dict[str, Callable[[], BaseException]] = {
    "обрыв": dropped,
    "брокер закрыл штатно": closed_by_broker,
    "сеть": lambda: OSError("соединение сброшено"),
}


@pytest.mark.parametrize("drop", sorted(DROPS))
def test_a_drop_reaches_the_consumer_and_is_not_redialled(handshakes, drop: str) -> None:
    """Обрыв — `NoConnection` потребителю; второго подключения слой не делает.

    Штатное закрытие брокером — тоже обрыв: итерация не кончается молча
    (`async for` вышел бы из цикла без слова, а сделки дальше терялись бы).
    Мутации: переподключаться внутри; закончить итерацию на `ConnectionClosedOK`.
    """
    fake = handshakes(lambda: FakeSocket(event_frame(), end=DROPS[drop]))

    async def scenario() -> list[OrderEvent]:
        seen: list[OrderEvent] = []
        async with order_events(Desk()) as events:
            async for event in events:
                seen.append(event)
        return seen

    with pytest.raises(NoConnection):
        run(scenario())
    assert len(fake.calls) == 1, f"{drop}: слой подключился {len(fake.calls)} раз"


GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


async def _silent_server(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Сервер, который принял рукопожатие и замолчал: ни кадра, ни pong."""
    head = await reader.readuntil(b"\r\n\r\n")
    key = next(
        line.split(":", 1)[1].strip()
        for line in head.decode().split("\r\n")
        if line.lower().startswith("sec-websocket-key:")
    )
    accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
    writer.write(
        "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
        f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n".encode()
    )
    await writer.drain()
    await asyncio.sleep(30)


def test_a_silent_socket_is_cut_at_the_limit() -> None:
    """Настоящая библиотека, молчащий сервер: `NoConnection` к пределу, а не вечное ожидание.

    Единственный тест файла без подмены `websockets.connect`: проверяется,
    что keepalive библиотеки действительно получает предел. Сервер на
    127.0.0.1 в этом же процессе, токен подставной. Мутации: не передать
    keepalive (умолчания библиотеки — 50 с); не передать `close_timeout`
    (+10 с к пределу).
    """
    limit = 0.3

    async def scenario() -> float:
        server = await asyncio.start_server(_silent_server, "127.0.0.1", 0)
        port = int(server.sockets[0].getsockname()[1])
        started = time.monotonic()
        try:
            async with order_events(
                Desk(), url=f"ws://127.0.0.1:{port}", silence_limit=limit
            ) as events:
                with pytest.raises(NoConnection):
                    await asyncio.wait_for(anext(events), timeout=5.0)
        finally:
            server.close()
        return time.monotonic() - started

    elapsed = run(scenario())
    assert elapsed < 3.0, f"молчащий сокет порван за {elapsed:.1f} с при пределе {limit} с"


def test_the_default_limit_is_the_server_ping_period() -> None:
    """Предел по умолчанию — период ping сервера со страницы 28, а не число из головы."""
    page = (BROKER_API_DOCS / "28-ping-pong.md").read_text(encoding="utf-8")
    found = re.search(r"ping каждые (\d+) секунд", page)
    assert found is not None, "на странице 28 не нашёлся период ping"
    assert float(found.group(1)) == SILENCE_LIMIT


def test_the_keepalive_fits_inside_the_limit(handshakes) -> None:
    """Пауза, ожидание pong и закрытие вместе не длиннее предела."""
    fake = handshakes(lambda: FakeSocket(event_frame()))
    run(first_event(Desk(), silence_limit=12.0))
    interval, timeout, closing = fake.calls[0]["keepalive"]
    assert interval > 0 and timeout > 0 and closing > 0
    assert interval + timeout + closing <= 12.0 + 1e-9


@pytest.mark.parametrize("limit", [0.0, -1.0, math.nan, math.inf])
def test_a_meaningless_limit_is_refused_before_the_handshake(handshakes, limit: float) -> None:
    """Предел 0, отрицательный, NaN, бесконечность — `ValueError` без подключения.

    Ожидание ограничено: без отказа подставной сокет молчит вечно, и прогон
    висел бы, а не падал (test-expert, 27.10.2026: мутация «предел молча
    заменён умолчанием» вешала весь набор).
    """
    fake = handshakes(FakeSocket)
    desk = Desk()
    with pytest.raises(ValueError):
        run(asyncio.wait_for(first_event(desk, silence_limit=limit), timeout=2.0))
    assert fake.calls == [] and desk.handed == []


def test_alive_is_a_ping_answered_by_a_pong(handshakes) -> None:
    """`alive()` — ping и pong сейчас; нет pong за треть предела — `NoConnection`.

    Мутация: `alive()` без ожидания pong.
    """

    async def probe(socket: FakeSocket) -> float:
        handshakes(lambda: socket)
        async with order_events(Desk(), silence_limit=0.3) as events:
            return await events.alive()

    assert run(probe(FakeSocket())) > 0
    started = time.monotonic()
    with pytest.raises(NoConnection):
        run(probe(FakeSocket(pong=False)))
    assert time.monotonic() - started < 2.0


# --- токен и число сокетов ---


def test_each_connection_takes_the_token_current_at_that_moment(handshakes) -> None:
    """Каждое подключение берёт рабочий токен заново — не однажды на старте.

    Мутация: запомнить токен при первом подключении.
    """
    fake = handshakes(lambda: FakeSocket(event_frame()))
    desk = Desk(FAKE_FIRST, FAKE_SECOND)

    async def twice() -> None:
        for _ in range(2):
            await first_event(desk)

    run(twice())
    sent = [call["at_handshake"]["Authorization"] for call in fake.calls]
    assert sent == ["Bearer " + FAKE_FIRST, "Bearer " + FAKE_SECOND]


def test_a_rejected_handshake_forgets_the_working_token(handshakes) -> None:
    """401 в рукопожатии — `AuthFailed` и рабочий токен забыт; 503 — не забыт.

    Иначе следующее подключение снова понесло бы отвергнутый токен.
    Мутация: не звать `forget_access_token`; звать его на любом отказе.
    """
    handshakes(lambda: rejected(401))
    refused = Desk()
    with pytest.raises(AuthFailed):
        run(first_event(refused))
    assert refused.forgotten == 1

    handshakes(lambda: rejected(503))
    failing = Desk()
    with pytest.raises(ServerFailure):
        run(first_event(failing))
    assert failing.forgotten == 0


def test_the_token_expiry_reaches_the_consumer(handshakes) -> None:
    """Срок рабочего токена этого подключения виден потребителю (план §10, С10)."""
    handshakes(FakeSocket)

    async def scenario() -> datetime:
        async with order_events(Desk()) as events:
            return events.token_expires_at

    assert run(scenario()) == EXPIRES


def test_a_second_socket_on_one_session_is_refused(handshakes) -> None:
    """Пока поток открыт, второй по той же сессии — отказ без подключения.

    После закрытия — снова можно, и после отказа рукопожатия тоже: отметка
    снимается на всех путях. Другая сессия — своя. Мутации: не проверять
    открытый; не снимать отметку на отказе.
    """
    fake = handshakes(FakeSocket)
    desk, other = Desk(), Desk()

    async def scenario() -> None:
        async with order_events(desk) as events:
            assert isinstance(events, OrderEvents)
            with pytest.raises(RuntimeError, match="уже открыт"):
                async with order_events(desk):
                    pass
            async with order_events(other):
                pass
        async with order_events(desk):
            pass

    run(scenario())
    assert len(fake.calls) == 3, f"подключений {len(fake.calls)}: второе ушло к брокеру"

    handshakes(lambda: rejected(401))
    with pytest.raises(AuthFailed):
        run(first_event(desk))
    handshakes(lambda: FakeSocket(event_frame()))
    run(first_event(desk))
