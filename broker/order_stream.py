"""Поток статусов заявок (`.docs/broker-api/24-ws-order-status.md`).

Что делает и чего не делает
---------------------------
Держит один сокет сервиса заявок и отдаёт наружу события о заявках
(`broker.orders.OrderEvent`) — по одному, в том порядке, в каком их прислал
брокер. Подписки нет: сервер шлёт события сам после подключения (24).
Сопоставления с журналом команд, сборки сделок и решений здесь нет: это
боевой исполнитель в `app/` (план Э4 §3, шаг 2).

**Обрыв — всегда отказ, и переподключения внутри нет.** Итерация по потоку
не кончается молча никогда: и обрыв, и штатное закрытие сокета брокером
приходят потребителю `NoConnection`. Пока сокета нет, события о сделках
теряются — повтора после переподключения в документации нет (план §5), —
и добирать их статусом по ключу и списком сделок обязан вызывающий: ему
для этого нужен журнал команд, которого у слоя брокера нет и быть не должно.
Тихое переподключение здесь спрятало бы именно ту дыру, которую надо добрать.

**Молчание событий — норма, молчание сокета — обрыв.** Заявок нет — событий
нет, хоть весь день, поэтому таймер «давно не было кадра» здесь неверен.
Живость проверяется протоколом: сокет посылает ping и ждёт pong
(`websockets`, keepalive), а сервер со своей стороны шлёт ping каждые 30 с
(`28-ping-pong.md`). Не пришёл pong — сокет рвётся, потребитель получает
`NoConnection` не позже `silence_limit` секунд от последнего признака жизни.

⚠️ «Не блокируйте обработку входящих сообщений» (28): потребитель обязан
читать поток непрерывно. Очередь входящих у библиотеки ограничена, и пока
её не разбирают, сокет перестаёт читать сеть — вместе с ping сервера.
Запись в базу — потоком базы, не между двумя чтениями (план §3).

Живьём не проверялось ничего: токена с правами торговли в разработке нет.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import AsyncIterator, Awaitable, Mapping
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Final, Protocol

import websockets

from broker.errors import (
    AuthFailed,
    BrokerError,
    NoConnection,
    UnexpectedAnswer,
    body_details,
    code_of,
)
from broker.orders import OrderEvent, parse_order_event
from broker.redaction import LOGGER_NAME
from broker.stream import authorized_socket
from broker.tokens import AccessToken

#: Сокет статусов заявок — дословно со страницы 24.
ORDERS_WS: Final[str] = "wss://ws.broker.ru/trade-api-bff-operations/api/v1/orders/events/ws"

#: Логгер рукопожатия — в нашем поддереве, по той же причине, что
#: `broker/stream.py::SOCKET_LOGGER`: библиотека печатает заголовки, а среди
#: них рабочий токен.
SOCKET_LOGGER: Final[str] = f"{LOGGER_NAME}.order_stream.socket"

#: Как поток называется в технических текстах отказов.
WHERE: Final[str] = "поток статусов заявок"

#: За сколько секунд мёртвый сокет обязан быть признан мёртвым.
#:
#: **Откуда число.** Сервер шлёт ping каждые 30 с (`28-ping-pong.md`), и план
#: Э4 §2.2 п. 2 требует того же от исполнителя: «поток статусов подключён
#: и жив: ping не реже 30 с». Предел делится на три равные части — пауза
#: между нашими ping, ожидание pong, ожидание закрытия, — так что худший
#: случай укладывается ровно в предел (проба 27.10.2026 на локальном
#: молчащем сервере: без `close_timeout` к пределу добавлялись умолчательные
#: 10 с закрытия). Наш ping раз в 10 с — 0,1 сообщения в секунду при пределе
#: брокера 10 (`29-restrictions.md`).
#:
#: Число техническое, а не торговое: робот по нему не решает, где входить,
#: оно только отвечает, когда честно сказать «связи нет». Параметр
#: `order_events`, а не константа внутри: тесты рвут сокет за доли секунды.
SILENCE_LIMIT: Final[float] = 30.0

#: Открытые потоки: `id` сессии. Предел брокера — 4 соединения на сервис
#: заявок (29), но нам нужно ровно одно: второй сокет означает, что прежний
#: не закрыт при переподключении, и после десятка обрывов программа упёрлась
#: бы в предел в момент, когда связь только что вернулась (29, «Зачем»).
#: По `id`, а не слабой ссылкой: сессия-заглушка в тестах ссылок не держит.
_OPEN: set[int] = set()


class TokenDesk(Protocol):
    """Что потоку нужно от сессии: рабочий токен и способ его забыть."""

    async def access_token(self) -> AccessToken:
        """Действующий рабочий токен."""
        ...

    def forget_access_token(self) -> None:
        """Забыть рабочий токен: следующее обращение обменяет новый."""
        ...


class _Socket(Protocol):
    """Что потоку нужно от сокета `websockets`: чтение кадра и ping."""

    async def recv(self) -> str | bytes:
        """Очередной кадр; обрыв — исключение библиотеки."""
        ...

    async def ping(self) -> Awaitable[float]:
        """Отправить ping; результат — ожидание pong с задержкой в секундах."""
        ...


class OrderStreamRefused(BrokerError):
    """Брокер прислал в поток заявок отказ (`errors`), а не событие."""

    def __init__(self, code: str, details: str = "") -> None:
        super().__init__(
            "Брокер прислал в поток заявок отказ вместо события. Поток закрыт: "
            "о сделках программа узнаёт только из него.",
            f"{WHERE}: отказ брокера, код {code}",
            details=details,
        )


def event_of(raw: str | bytes) -> OrderEvent:
    """Кадр сокета → событие. Незнакомый кадр — отказ разбора, а не пропуск.

    Служебных кадров на странице 24 нет вовсе: подписки нет, ответа на неё
    тоже. Значит любой кадр, не ставший событием, — непонятый канал, и
    пропустить его значит, возможно, пропустить сделку. Сырой кадр уходит
    только в `details` (технический лог уровня DEBUG), в тексты — его вид.
    """
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    try:
        message = json.loads(raw)
    except (ValueError, TypeError):
        error = UnexpectedAnswer(
            "кадр потока заявок не разбирается как сообщение",
            f"{WHERE}: кадр не JSON ({type(raw).__name__}, длина {len(raw)})",
        )
        error.details = body_details(text)
        raise error from None
    if isinstance(message, Mapping) and message.get("errors"):
        raise OrderStreamRefused(code_of(text) or "без кода", details=text)
    try:
        return parse_order_event(message)
    except UnexpectedAnswer as error:
        error.details = body_details(text)
        raise


class OrderEvents:
    """Открытый поток: `async for event in events` — события до обрыва.

    Итерация не кончается сама: обрыв и закрытие сокета брокером — оба
    `NoConnection` из очередного шага. Негодный кадр — `UnexpectedAnswer`
    или `OrderStreamRefused`.
    """

    def __init__(self, socket: _Socket, token_expires_at: datetime, probe: float) -> None:
        self._socket = socket
        self._probe = probe
        #: Срок рабочего токена, с которым открыт сокет. Что брокер делает
        #: с сокетом, когда срок выходит, — нет в снимке (план §10, С10);
        #: вызывающий вправе переподключиться заранее.
        self.token_expires_at = token_expires_at

    def __aiter__(self) -> OrderEvents:
        return self

    async def __anext__(self) -> OrderEvent:
        try:
            raw = await self._socket.recv()
        except (websockets.exceptions.WebSocketException, OSError) as error:
            # Сюда попадает и `ConnectionClosedOK` — штатное закрытие брокером.
            # Для потока сделок это такой же обрыв: дальше событий не будет.
            raise NoConnection(f"{WHERE}: {type(error).__name__}: {error}") from None
        return event_of(raw)

    async def alive(self) -> float:
        """Сокет жив **сейчас**: наш ping, ответный pong. Возвращает задержку, с.

        Для проверки перед заявкой (план §2.2 п. 2): keepalive говорит
        «жив был не дольше предела назад», а здесь — «жив в этот момент».
        Pong не пришёл за треть предела — `NoConnection`.
        """
        try:
            pong = await self._socket.ping()
            async with asyncio.timeout(self._probe):
                return float(await pong)
        except TimeoutError:
            raise NoConnection(f"{WHERE}: pong не пришёл за {self._probe:.1f} с") from None
        except (websockets.exceptions.WebSocketException, OSError) as error:
            raise NoConnection(f"{WHERE}: {type(error).__name__}: {error}") from None


def _keepalive(limit: float) -> dict[str, float]:
    """Предел молчания → настройки keepalive библиотеки: три равные доли."""
    if not math.isfinite(limit) or limit <= 0:
        raise ValueError(f"предел молчания потока заявок — положительное число, а не {limit}")
    share = limit / 3
    return {"ping_interval": share, "ping_timeout": share, "close_timeout": share}


@asynccontextmanager
async def order_events(
    session: TokenDesk,
    *,
    url: str = ORDERS_WS,
    silence_limit: float = SILENCE_LIMIT,
) -> AsyncIterator[OrderEvents]:
    """Открытый поток статусов заявок. Читать — `async for` по нему.

    Контекст, а не генератор событий, по той же причине, что `candle_stream`:
    сокет открывает и закрывает потребитель в своей задаче.

    * **Токен — на каждое подключение свой:** `access_token()` зовётся здесь,
      при входе, а не однажды при старте. Отказ рукопожатия 401 забывает
      рабочий токен в сессии: следующее подключение обменяет новый.
    * **Сокет один на сессию.** Второй вход при открытом первом — отказ
      без подключения (`RuntimeError`, дефект программы).
    * Сеть и рукопожатие — отказы слоя (`broker/stream.py::authorized_socket`),
      заголовок с токеном стирается на всех путях выхода.
    """
    options = _keepalive(silence_limit)
    key = id(session)
    if key in _OPEN:
        raise RuntimeError(
            "поток статусов заявок по этой сессии уже открыт: второй сокет "
            "значит, что прежний не закрыт при переподключении"
        )
    _OPEN.add(key)
    try:
        token = await session.access_token()
        try:
            async with authorized_socket(
                token, url, logger_name=SOCKET_LOGGER, where=WHERE, options=options
            ) as socket:
                yield OrderEvents(socket, token.expires_at, options["ping_timeout"])
        except AuthFailed:
            session.forget_access_token()
            raise
    finally:
        _OPEN.discard(key)
