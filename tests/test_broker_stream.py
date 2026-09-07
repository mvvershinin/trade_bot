"""Поток свечей брокера: разбор сообщений и рукопожатие сокета.

В сеть тесты не ходят. `snapshot_of` — чистая функция над строкой, и каждый
случай разбора проверяется на сообщении, собранном руками по документации
(`.docs/broker-api/20-ws-last-candle.md`, сверенной с сервером 04.09.2026);
рукопожатие проверяется на подставном `websockets.connect`.

Главное, что стережётся, — **молчаливая подстановка нуля**. Свеча с нулевым
`low` выглядит как касание любого уровня выхода, и тейк «сработал» бы там,
где сделки не было. Поэтому неполное сообщение — отказ вслух с перечнем
пришедших полей, а не снимок с дыркой. Второе — **наивное время**: брокер шлёт
UTC, окно 10:05–11:00 московское, и момент без пояса, истолкованный как
местный, сдвинул бы свечу на три часа без единого сообщения.

Токен брокера здесь не участвует: заголовок рукопожатия проверяется
на приметной подделке (подделка-для-теста), которая токеном не является.

Вторая половина файла — про эту подделку и про то, куда она **не** попадает:
в переменные кадра при отказе рукопожатия и в журнал при подробном логе
библиотеки. Оба места нашлись прогоном 04.09.2026, оба закрыты в
`broker/stream.py::candle_stream`.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import traceback
from collections.abc import AsyncIterator, Awaitable, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Callable

import pytest
import websockets.exceptions
from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.http11 import Response

from broker import stream
from broker.errors import AuthFailed, BrokerError, NoConnection, ServerFailure
from broker.redaction import LOGGER_NAME, MASK
from broker.secret import Secret
from broker.stream import (
    MARKET_DATA_WS,
    SOCKET_LOGGER,
    CandleSnapshot,
    StreamRejected,
    candle_stream,
    snapshot_of,
)

#: Приметная подделка — не токен брокера. Латиница без пробелов, как требует `Secret`.
FAKE_WS_TOKEN = "FAKE-ws-handshake-0000-NOT-A-REAL-TOKEN"

#: Поля свечи, без любого из которых снимка быть не должно.
REQUIRED = (
    "ticker", "classCode", "timeFrame", "dateTime", "open", "high", "low", "close", "volume",
)

OPENED_AT = datetime(2026, 9, 4, 5, 38, tzinfo=timezone.utc)


def message(**changes: object) -> dict[str, object]:
    """Сообщение с данными свечи — плоское, как отдаёт сервер, а не PDF."""
    body: dict[str, object] = {
        "responseType": "CandleStick",
        "ticker": "MXU6",
        "classCode": "SPBFUT",
        "timeFrame": "M1",
        "dateTime": "2026-09-04T05:38:00.000Z",
        "open": 224_900.0,
        "high": 224_925.0,
        "low": 224_850.0,
        "close": 224_875.0,
        "volume": 30_579_800.0,
    }
    body.update(changes)
    return body


def wire(**changes: object) -> str:
    return json.dumps(message(**changes))


# --- годное сообщение ---


def test_a_candle_message_becomes_a_snapshot_with_every_field() -> None:
    snapshot = snapshot_of(wire())
    assert isinstance(snapshot, CandleSnapshot)
    assert (snapshot.ticker, snapshot.class_code, snapshot.timeframe) == ("MXU6", "SPBFUT", "M1")
    assert snapshot.opened_at == OPENED_AT
    assert snapshot.opened_at.tzinfo is not None, "пояс потерян при разборе"
    assert (snapshot.open, snapshot.high, snapshot.low, snapshot.close) == (
        224_900.0, 224_925.0, 224_850.0, 224_875.0,
    )
    assert snapshot.turnover == 30_579_800.0, "оборот обязан храниться как пришёл"
    # Сокет отдаёт и байты, и текст — разбор одинаков.
    assert snapshot_of(wire().encode("utf-8")) == snapshot


def test_an_explicit_offset_is_the_same_instant_not_a_wall_clock() -> None:
    """08:38+03:00 и 05:38Z — один момент; сравнение идёт по моменту, не по цифрам."""
    moscow = snapshot_of(wire(dateTime="2026-09-04T08:38:00+03:00"))
    assert moscow is not None
    assert moscow.opened_at == OPENED_AT


@pytest.mark.parametrize(
    "service",
    [
        {"responseType": "CandleStickSuccess", "ticker": "MXU6", "classCode": "SPBFUT"},
        {"responseType": "Pong"},
        {},
    ],
)
def test_a_service_message_is_not_a_snapshot(service: dict[str, object]) -> None:
    assert snapshot_of(json.dumps(service)) is None


# --- время без пояса ---


@pytest.mark.parametrize(
    "moment", ["2026-09-04T05:38:00.000", "2026-09-04T05:38:00", "2026-09-04 05:38"]
)
def test_a_moment_without_a_time_zone_is_refused_not_guessed(moment: str) -> None:
    """Наивное время — отказ вслух, а не молчаливое `astimezone` как местного."""
    with pytest.raises(StreamRejected) as caught:
        snapshot_of(wire(dateTime=moment))
    assert "пояс" in str(caught.value), str(caught.value)
    assert caught.value.retryable is False


# --- неполное или чужое сообщение ---


def _listed_fields(text: str) -> set[str]:
    """Имена полей, которые отказ перечислил как пришедшие."""
    _, _, tail = text.partition("Пришедшие поля:")
    assert tail, f"отказ не перечисляет пришедшие поля: {text}"
    return {name.strip(" .") for name in tail.split(",")}


@pytest.mark.parametrize("field", REQUIRED)
def test_a_missing_field_is_a_refusal_that_lists_what_came(field: str) -> None:
    """Нет поля — нет снимка. Ноль вместо `low` был бы касанием любого уровня."""
    body = message()
    del body[field]
    with pytest.raises(StreamRejected) as caught:
        snapshot_of(json.dumps(body))
    assert _listed_fields(str(caught.value)) == set(body)
    assert field not in _listed_fields(str(caught.value))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("low", None),
        ("low", "нет"),
        ("volume", [30_579_800.0]),
        ("close", {"value": 224_875.0}),
        ("dateTime", 123),
        ("dateTime", "вчера"),
    ],
)
def test_a_field_of_the_wrong_type_is_refused_not_coerced(field: str, value: object) -> None:
    with pytest.raises(StreamRejected):
        snapshot_of(wire(**{field: value}))


def test_a_refusal_speaks_in_plain_words_and_keeps_the_details_for_the_log() -> None:
    with pytest.raises(StreamRejected) as caught:
        snapshot_of(wire(low=None))
    error = caught.value
    assert "незнакомом виде" in error.human
    assert "Traceback" not in error.human


# --- негодный кадр ---


@pytest.mark.parametrize(
    ("frame", "what"),
    [
        ("ping", "текстовый кадр, не JSON вовсе"),
        ("", "пустой кадр"),
        ("{нет", "оборванный JSON"),
        ("[1, 2]", "годный JSON, но массив, а не объект"),
        ('"CandleStick"', "годный JSON, но строка"),
        ("17", "годный JSON, но число"),
        (b"\xff\xfe", "байты не в UTF-8"),
    ],
)
def test_a_frame_that_does_not_parse_is_a_refusal_worth_a_retry(
    frame: str | bytes, what: str
) -> None:
    """Битый кадр — отказ **с повтором**, а не исключение мимо слоя.

    До правки 05.09.2026 не-JSON выходил наружу `JSONDecodeError`, а годный
    JSON не-объект — `AttributeError` на `message.get`. Вызывающий
    (`app/live_feed._ride`) всё, что не `BrokerError`, считает бедой программы
    и снимает поток окончательно: один битый кадр стоил всех минут до конца
    сеанса. Теперь подписка переоткрывается, а пропущенное добирает догрузка.
    """
    with pytest.raises(StreamRejected) as caught:
        snapshot_of(frame)
    assert caught.value.retryable is True, f"{what}: повтор запрещён"
    assert "переоткроет подписку" in caught.value.human, what


def test_the_content_of_a_bad_frame_does_not_reach_the_owner() -> None:
    """Содержимое кадра — данные из сети: в `human` и `technical` им хода нет.

    Требование шапки `broker/errors.py`, и оно уже оплачено: тело ответа,
    дописанное в `technical`, однажды выплеснулось владельцу счёта в консоль
    страницей разметки вместо сообщения. Наружу идут вид кадра и его длина;
    само тело живёт в `details`, откуда выходит одной записью уровня DEBUG.
    """
    frame = '["приметная строка внутри кадра"]'
    with pytest.raises(StreamRejected) as caught:
        snapshot_of(frame)
    error = caught.value
    assert "приметная строка" not in error.human, "тело кадра ушло владельцу счёта"
    assert "приметная строка" not in error.technical, "тело кадра ушло в технический лог"
    assert "приметная строка" in error.details, (
        "тело не сохранено вовсе — разбирать такой отказ будет нечем"
    )
    assert str(len(frame)) in error.technical, (
        f"длина кадра не названа, разобрать отказ не по чему: {error.technical}"
    )


def test_a_list_of_errors_without_objects_is_still_a_refusal_not_a_crash() -> None:
    """`errors` из строк — отказ брокера, а не `AttributeError` на `.get`.

    Та же беда, что и с битым кадром, но на шаг позже: исключение не от слоя
    брокера снимает поток до конца сеанса.
    """
    body = {"errors": ["Instrument not found"]}
    with pytest.raises(StreamRejected) as caught:
        snapshot_of(json.dumps(body))
    assert "без объяснения" in caught.value.human
    assert "без кода" in caught.value.technical


# --- отказ брокера ---


def test_a_broker_refusal_carries_its_text_and_code() -> None:
    body = {
        "responseType": "CandleStick",
        "errors": [{"message": "Instrument not found", "code": "NOT_FOUND"}],
    }
    with pytest.raises(StreamRejected) as caught:
        snapshot_of(json.dumps(body))
    error = caught.value
    assert "Instrument not found" in error.human
    assert "NOT_FOUND" in error.technical
    assert error.retryable is False


def test_a_live_refusal_names_its_code_in_the_type_field() -> None:
    """Живой отказ 04.09.2026 пришёл с `type`, а не с `code`, как на сайте."""
    body = {"errors": [{"type": "REQUIRED_FIELD", "field": "subscribeType"}]}
    with pytest.raises(StreamRejected) as caught:
        snapshot_of(json.dumps(body))
    assert "REQUIRED_FIELD" in caught.value.technical
    assert "без объяснения" in caught.value.human


@pytest.mark.parametrize(
    ("code", "retryable"),
    [
        ("NO_DATE", True),
        ("NOT_FOUND", False),
        ("INCORRECT_JSON", False),
        ("BAD_REQUEST", False),
        ("UNAUTHORIZED", False),
    ],
)
def test_only_no_data_yet_is_worth_a_retry(code: str, retryable: bool) -> None:
    """`NO_DATE` — данных пока нет, повтор по паузе; остальное лечит человек."""
    body = {"errors": [{"message": "отказ", "code": code}]}
    with pytest.raises(StreamRejected) as caught:
        snapshot_of(json.dumps(body))
    assert caught.value.retryable is retryable


# --- подписка ---


async def _access_token() -> SimpleNamespace:
    return SimpleNamespace(secret=Secret(FAKE_WS_TOKEN))


def test_the_subscription_speaks_the_names_the_server_accepts(monkeypatch) -> None:
    """`subscribeType`, не `subscriberType` из PDF: сервер отвечает REQUIRED_FIELD.

    Сокет подменён; проверяется ровно то, что уходит в него первым сообщением,
    и заголовок, с которым он открывается.
    """
    sent: list[str] = []
    seen: dict[str, object] = {}

    class Socket:
        async def send(self, text: str) -> None:
            sent.append(text)

    @asynccontextmanager
    async def connect(
        url: str, *, additional_headers: dict[str, str], logger: logging.Logger
    ) -> AsyncIterator[Socket]:
        seen["url"] = url
        # Копия, а не сам словарь: настоящий словарь затирается на выходе
        # из контекста, и проверка ниже осталась бы без предмета.
        seen["headers"] = dict(additional_headers)
        yield Socket()

    monkeypatch.setattr(stream.websockets, "connect", connect)
    session: Any = SimpleNamespace(access_token=_access_token)

    async def scenario() -> None:
        async with candle_stream(session, ticker="MXU6", class_code="SPBFUT") as socket:
            assert isinstance(socket, Socket)

    asyncio.run(scenario())
    assert seen["url"] == MARKET_DATA_WS
    assert seen["headers"] == {"Authorization": "Bearer " + FAKE_WS_TOKEN}
    assert json.loads(sent[0]) == {
        "subscribeType": 0,
        "dataType": 1,
        "instruments": [{"classCode": "SPBFUT", "ticker": "MXU6"}],
        "timeFrame": "M1",
    }




# --- заголовок рукопожатия: куда токен не попадает ---


class _Socket:
    """Подставной сокет: принимает подписку и молчит."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


#: Подставной `websockets.connect`: принимает то же, что настоящий, и отдаёт
#: контекст с сокетом. Аргументы именованы как у библиотеки — подмена, которая
#: приняла бы вызов с любыми аргументами, не заметила бы их пропажи.
Connect = Callable[..., Any]

#: Что делает потребитель внутри открытого потока.
Body = Callable[[object], Awaitable[None]]


def _working(seen: dict[str, Any]) -> Connect:
    """Рукопожатие удалось. Всё, что получила библиотека, кладётся в `seen`."""

    @asynccontextmanager
    async def connect(
        url: str, *, additional_headers: dict[str, str], logger: logging.Logger
    ) -> AsyncIterator[_Socket]:
        seen["url"] = url
        seen["headers"] = additional_headers  # сам словарь, а не копия
        seen["at_handshake"] = dict(additional_headers)
        seen["logger"] = logger
        yield _Socket()

    return connect


class _Refusing:
    """Контекст, который отказывает на входе, — как рукопожатие, не состоявшееся.

    Классом, а не генератором с `@asynccontextmanager`: у настоящей библиотеки
    отказ поднимается из `__aenter__`, а генератор с недостижимым `yield`
    после `raise` — мёртвая строка, на которую справедливо ругаются проверки.
    """

    def __init__(self, error: BaseException) -> None:
        self._error = error

    async def __aenter__(self) -> _Socket:
        raise self._error

    async def __aexit__(self, *unused: object) -> bool:
        return False


def _failing(error: BaseException) -> Connect:
    """Подмена `connect`, роняющая рукопожатие заданным отказом."""

    def connect(
        url: str, *, additional_headers: dict[str, str], logger: logging.Logger
    ) -> _Refusing:
        return _Refusing(error)

    return connect


def _rejected(status: int) -> websockets.exceptions.InvalidStatus:
    """Отказ сервера кодом HTTP — так выглядит непринятый токен (401) и 5xx."""
    return websockets.exceptions.InvalidStatus(Response(status, "", Headers()))


async def _quiet(socket: object) -> None:
    return None


async def _database_broke(socket: object) -> None:
    raise RuntimeError("база не пишется: disk I/O error")


async def _window_closed(socket: object) -> None:
    raise asyncio.CancelledError()


def _stream_failure(
    monkeypatch: pytest.MonkeyPatch, connect: Connect, body: Body = _quiet
) -> BaseException:
    """Прогнать поток на подставном сокете и вернуть отказ, вышедший наружу."""
    monkeypatch.setattr(stream.websockets, "connect", connect)
    session: Any = SimpleNamespace(access_token=_access_token)

    async def scenario() -> BaseException:
        try:
            async with candle_stream(session, ticker="MXU6", class_code="SPBFUT") as socket:
                await body(socket)
        except BaseException as error:  # noqa: BLE001 — ловим любой, в том числе отмену
            return error
        raise AssertionError("отказ не вышел наружу — тест остался без предмета")

    return asyncio.run(scenario())


def _frames_with_token(error: BaseException) -> list[str]:
    """Строки переменных кадров трассировки, в которых видна подделка.

    `traceback.format_exception` переменные кадра не печатает — поэтому дыра
    этого класса невидима обычным прогоном и обычным логом. Печатают их
    `pytest -l`, отладчик и сборщики отчётов об ошибках.
    """
    rendered = "".join(
        traceback.TracebackException.from_exception(error, capture_locals=True).format()
    )
    return [line.strip() for line in rendered.splitlines() if FAKE_WS_TOKEN in line]


def test_the_handshake_header_is_wiped_when_the_socket_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Словарь заголовков затирается на выходе: библиотеке он ушёл, в кадре пусто.

    Прямая проверка того же, что ниже проверяется через трассировку. Здесь
    видно оба конца: при рукопожатии заголовок был настоящим, после выхода
    из контекста словарь пуст.
    """
    seen: dict[str, Any] = {}
    monkeypatch.setattr(stream.websockets, "connect", _working(seen))
    session: Any = SimpleNamespace(access_token=_access_token)

    async def scenario() -> None:
        async with candle_stream(session, ticker="MXU6", class_code="SPBFUT"):
            assert seen["headers"] == {"Authorization": "Bearer " + FAKE_WS_TOKEN}, (
                "внутри контекста заголовок обязан быть живым — иначе сокет не открылся бы"
            )

    asyncio.run(scenario())
    assert seen["at_handshake"] == {"Authorization": "Bearer " + FAKE_WS_TOKEN}
    assert seen["headers"] == {}, "словарь заголовков не затёрт после закрытия сокета"


@pytest.mark.parametrize(
    ("name", "connect", "body", "expected"),
    [
        ("рукопожатие 401: токен не принят", _failing(_rejected(401)), _quiet, AuthFailed),
        ("рукопожатие 503: неполадка у брокера", _failing(_rejected(503)), _quiet, ServerFailure),
        ("сети нет вовсе", _failing(OSError("нет маршрута до узла")), _quiet, NoConnection),
        (
            "обрыв сокета",
            _failing(websockets.exceptions.ConnectionClosedError(None, None)),
            _quiet,
            NoConnection,
        ),
        ("чужой RuntimeError у потребителя", _working({}), _database_broke, RuntimeError),
        ("отмена задачи при закрытии окна", _working({}), _window_closed, asyncio.CancelledError),
    ],
)
def test_no_handshake_token_in_frame_locals_of_any_failure(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    connect: Connect,
    body: Body,
    expected: type,
) -> None:
    """Ни в одной локальной переменной ни одного кадра нет значения токена.

    Найдено прогоном 04.09.2026: заголовок жил голой локальной переменной
    `authorization`, а оба отказа рукопожатия поднимаются **внутри кадра**
    `candle_stream` — кадр уезжал в трассировку вместе со строкой
    `Bearer <рабочий токен>`, сутками полного доступа к счёту.

    Главный случай здесь — **401**: самый вероятный первый отказ при включении
    потока (токен протух или отвергнут), и цикл переподключения в `app/`
    приводит к нему повторно, то есть утечка не разовая.

    Проверка общая, а не про конкретную переменную: следующая переменная
    с сырым значением обязана валить этот тест, не дожидаясь разбора.
    Два последних случая — отказ у потребителя — до правки были чисты
    и остаются здесь как сторож на будущее: кадр там живёт по другим
    правилам, и правка `candle_stream` способна это изменить.
    """
    error = _stream_failure(monkeypatch, connect, body)
    assert isinstance(error, expected), f"{name}: отказ подменился на {error!r}"
    offenders = _frames_with_token(error)
    assert not offenders, f"{name}: значение токена в переменных кадра:\n  " + "\n  ".join(
        offenders
    )


def test_the_frame_search_would_find_a_planted_token() -> None:
    """Канарейка: проверка выше не выродилась — подложенная утечка находится."""
    try:
        raise RuntimeError("отказ без токена в тексте")
    except RuntimeError as planted:
        authorization = "Bearer " + FAKE_WS_TOKEN  # живёт в кадре этого теста
        assert authorization
        assert _frames_with_token(planted), "поиск утечки не находит даже подложенную"


def test_the_refusal_of_a_handshake_says_nothing_about_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Тексты отказа — человеческий и технический — без подделки."""
    error = _stream_failure(monkeypatch, _failing(_rejected(401)))
    assert isinstance(error, BrokerError)
    assert FAKE_WS_TOKEN not in error.human
    assert FAKE_WS_TOKEN not in error.technical
    assert FAKE_WS_TOKEN not in str(error)


# --- подробный лог библиотеки ---


@contextmanager
def _captured_log(level: int = logging.DEBUG) -> Iterator[io.StringIO]:
    """Корневой обработчик, как его поставил бы `logging.basicConfig`."""
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    root = logging.getLogger()
    was = root.level
    root.addHandler(handler)
    root.setLevel(level)
    try:
        yield buffer
    finally:
        root.removeHandler(handler)
        root.setLevel(was)


def test_the_socket_gets_a_logger_from_our_own_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    """Библиотеке передаётся логгер под `broker`, а не её собственный.

    `websockets` печатает каждый заголовок рукопожатия обычным `debug`
    (`websockets/client.py:294-297`, исходник 17.0.1), и по умолчанию делает
    это на логгере `websockets.client` — ветке, которую слой брокера накрывать
    не вправе. Тогда чистка держалась бы на одной строке в сборке `app/`,
    и процесс, собранный мимо неё, писал бы токен в лог молча.
    """
    seen: dict[str, Any] = {}
    monkeypatch.setattr(stream.websockets, "connect", _working(seen))
    session: Any = SimpleNamespace(access_token=_access_token)

    async def scenario() -> None:
        async with candle_stream(session, ticker="MXU6", class_code="SPBFUT"):
            pass

    asyncio.run(scenario())
    logger = seen["logger"]
    assert isinstance(logger, logging.Logger), "логгер библиотеке не передан вовсе"
    assert logger.name == SOCKET_LOGGER
    assert logger.name.split(".")[0] == LOGGER_NAME, (
        "логгер сокета вне поддерева broker — чистка его не накроет"
    )


def test_a_handshake_line_on_that_logger_comes_out_masked() -> None:
    """Строка рукопожатия на логгере сокета — без подделки, с маской.

    Запись собрана ровно так, как её делает библиотека: `debug("> %s: %s", key,
    value)`. Уровень поднят до DEBUG на корне — то есть воспроизводится
    `logging.basicConfig(level=DEBUG)`, поставленный при разборе обрыва.
    """
    keeper = Secret(FAKE_WS_TOKEN)  # значение под охраной реестра, как в бою
    assert str(keeper) == MASK
    with _captured_log() as buffer:
        logging.getLogger(SOCKET_LOGGER).debug(
            "> %s: %s", "Authorization", "Bearer " + FAKE_WS_TOKEN
        )
    printed = buffer.getvalue()
    assert "Authorization" in printed, "запись не дошла до обработчика — проверка вакуумна"
    assert FAKE_WS_TOKEN not in printed, "подделка вышла в лог целиком"
    assert MASK in printed


def test_the_same_line_outside_our_tree_would_leak() -> None:
    """Канарейка: чистит не сам факт записи, а поддерево `broker`.

    Имя чужого дерева взято приметное, а не `websockets.client`: сборка `app/`
    ставит чистку и на него, и канарейка зависела бы от порядка тестов.
    Смысл тот же — вне накрытой ветки та же строка выходит целиком,
    значит проверка выше проверяет чистку, а не устройство `logging`.
    """
    with _captured_log() as buffer:
        logging.getLogger("not-broker.websockets.client").debug(
            "> %s: %s", "Authorization", "Bearer " + FAKE_WS_TOKEN
        )
    assert FAKE_WS_TOKEN in buffer.getvalue(), (
        "чужая ветка оказалась чистой сама по себе — проверка выше ничего не доказывает"
    )


def test_a_real_handshake_logs_its_header_under_our_logger_and_masked() -> None:
    """Живьём, на локальном сокете: настоящая библиотека, настоящее рукопожатие.

    Единственное место файла, где `websockets.connect` **не** подменён. Ловит
    то, чего подмена не поймает по построению: версию библиотеки, переставшую
    принимать `logger=`, и заголовки, напечатанные мимо переданного логгера.
    Брокера здесь нет — сервер поднимается на 127.0.0.1 в этом же процессе,
    токен подставной, наружу тест не ходит.

    Проверено 04.09.2026: без `logger=` та же строка уходит на `websockets.client`
    и печатается целиком, вместе с подделкой.
    """
    # Сервер печатает **полученные** заголовки на своём логгере. В продукте
    # сервера нет; здесь он часть стенда, и его записи из проверки убираются,
    # иначе тест ловил бы собственный стенд, а не слой.
    server_log = logging.getLogger("probe-server.websockets")
    server_log.propagate = False

    async def wait_until_closed(connection: ServerConnection) -> None:
        await connection.wait_closed()

    session: Any = SimpleNamespace(access_token=_access_token)

    async def scenario() -> str:
        async with serve(wait_until_closed, "127.0.0.1", 0, logger=server_log) as server:
            port = int(server.sockets[0].getsockname()[1])
            with _captured_log() as buffer:
                async with candle_stream(
                    session,
                    ticker="MXU6",
                    class_code="SPBFUT",
                    url=f"ws://127.0.0.1:{port}",
                ):
                    pass
            return buffer.getvalue()

    printed = asyncio.run(scenario())
    handshake = [line for line in printed.splitlines() if "Authorization" in line]
    assert handshake, (
        "строки рукопожатия в логе нет вовсе — библиотека перестала печатать "
        f"заголовки либо печатает их мимо {SOCKET_LOGGER}; проверка вакуумна"
    )
    assert FAKE_WS_TOKEN not in printed, f"подделка вышла в лог: {handshake}"
    assert any(MASK in line for line in handshake), handshake
