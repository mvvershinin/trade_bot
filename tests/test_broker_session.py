"""Подключение: обмен токена, автообновление, отказы, предохранители.

В сеть тесты не ходят: транспорт `httpx` подменён на подставные ответы.
Настоящий токен не участвует.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import pathlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx
import pytest

from broker.clock import Verdict as ClockVerdict
from broker.connection import Backoff, ConnectionState
from broker.errors import (
    AuthFailed,
    BadRequest,
    BrokerError,
    NoConnection,
    NotFound,
    OutcomeUnknown,
    ReadOnlyToken,
    ServerFailure,
    Throttled,
    Timeout,
    TokenExpired,
    UnexpectedAnswer,
    WrongAddress,
)
from broker.secret import Secret
from broker.session import (
    ADDRESS_SENSITIVE,
    ALLOWED_READ_PATHS,
    AUTH_EXCHANGE,
    AUTH_URL,
    CANDLES_PATH,
    DAILY_SCHEDULE_PATH,
    INSTRUMENTS_BY_TICKERS_PATH,
    LIMITS_PATH,
    PORTFOLIO_PATH,
    READ_ENDPOINTS,
    REPEATABLE,
    TRADING_STATUS_PATH,
    BrokerSession,
)
from broker.throttle import Throttle
from broker.token_store import TokenStore
from broker.tokens import ACCESS_LIFETIME, TokenScope

FAKE_REFRESH = "FAKE-session-refresh-0000-NOT-A-REAL-TOKEN"
FAKE_ACCESS = "FAKE-session-access-1111-NOT-A-REAL-TOKEN"

START = datetime(2026, 8, 30, 9, 0, tzinfo=timezone.utc)


class Clock:
    """Часы, которыми управляет тест."""

    def __init__(self, start: datetime = START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **shift: float) -> None:
        self.now += timedelta(**shift)


async def no_sleep(seconds: float) -> None:
    return None


def good_auth(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Ответ сервиса авторизации в том виде, как его описывает документация."""
    body = {
        "expires_in": int(ACCESS_LIFETIME.total_seconds()),
        "refresh_expires_in": 90 * 24 * 3600,
        "token_type": "bearer",
        "scope": "openid profile",
        "session_state": "8b2d0e0e-0000-0000-0000-000000000000",
        "not-before-policy": 0,
        "access_token": FAKE_ACCESS,
    }
    if extra:
        body.update(extra)
    return body


@pytest.fixture
def store(tmp_path: pathlib.Path) -> TokenStore:
    directory = tmp_path / "userdata"
    directory.mkdir()
    keeper = TokenStore(directory)
    keeper.save(Secret(FAKE_REFRESH), TokenScope.TRADE, issued_at=START - timedelta(days=1))
    return keeper


@pytest.fixture
def read_only_store(tmp_path: pathlib.Path) -> TokenStore:
    directory = tmp_path / "userdata"
    directory.mkdir()
    keeper = TokenStore(directory)
    keeper.save(Secret(FAKE_REFRESH), TokenScope.READ_ONLY, issued_at=START - timedelta(days=1))
    return keeper


class Recorder:
    """Подставной транспорт: помнит запросы, отвечает по сценарию."""

    def __init__(self, *replies: Callable[[httpx.Request], httpx.Response]) -> None:
        self.replies = list(replies)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self.replies) - 1)
        return self.replies[index](request)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url="https://example.invalid", transport=httpx.MockTransport(self)
        )


def json_reply(status: int, body: Any) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(status, json=body)


def raising(error: Exception) -> Callable[[httpx.Request], httpx.Response]:
    def reply(request: httpx.Request) -> httpx.Response:
        raise error

    return reply


def session(store: TokenStore, recorder: Recorder, clock: Clock, **kwargs: Any) -> BrokerSession:
    kwargs.setdefault("attempts", 3)
    return BrokerSession(
        store,
        client=recorder.client(),
        clock=clock,
        sleep=no_sleep,
        throttle=Throttle(rate=1000.0),
        **kwargs,
    )


def run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


# --- обмен токена ---


def test_exchange_sends_what_the_documentation_requires(store: TokenStore) -> None:
    """Три поля формы: `client_id`, `grant_type` и значение токена."""
    recorder = Recorder(json_reply(200, good_auth()))
    clock = Clock()

    async def scenario() -> Any:
        async with session(store, recorder, clock) as broker:
            return await broker.access_token()

    access = run(scenario())
    assert access.secret == Secret(FAKE_ACCESS)
    assert access.expires_at == START + ACCESS_LIFETIME
    assert access.scope is TokenScope.TRADE
    assert access.scope_reported == "openid profile"

    request = recorder.requests[0]
    assert str(request.url) == AUTH_URL
    body = request.content.decode()
    assert "client_id=trade-api-write" in body
    assert "grant_type=refresh_token" in body
    assert FAKE_REFRESH in body, "токен обязан уйти брокеру — иначе обмена нет"


def test_read_only_token_uses_the_read_client_id(read_only_store: TokenStore) -> None:
    recorder = Recorder(json_reply(200, good_auth()))
    clock = Clock()

    async def scenario() -> None:
        async with session(read_only_store, recorder, clock) as broker:
            await broker.access_token()

    run(scenario())
    assert "client_id=trade-api-read" in recorder.requests[0].content.decode()


def test_access_token_is_reused_until_it_goes_stale(store: TokenStore) -> None:
    """Обмен не повторяется на каждый запрос: это прямой путь в лимит частоты."""
    recorder = Recorder(json_reply(200, good_auth()), json_reply(200, {"ok": True}))
    clock = Clock()

    async def scenario() -> int:
        async with session(store, recorder, clock) as broker:
            await broker.read(PORTFOLIO_PATH)
            clock.advance(hours=1)
            await broker.read(PORTFOLIO_PATH)
            return sum(1 for request in recorder.requests if str(request.url) == AUTH_URL)

    assert run(scenario()) == 1


def test_access_token_is_refreshed_before_expiry(store: TokenStore) -> None:
    """Обновление заранее — чтобы поток не встал посреди торгового окна."""
    recorder = Recorder(json_reply(200, good_auth()))
    clock = Clock()

    async def scenario() -> int:
        async with session(store, recorder, clock) as broker:
            await broker.access_token()
            clock.advance(hours=23, minutes=45)  # до конца суток меньше запаса
            await broker.access_token()
            return sum(1 for request in recorder.requests if str(request.url) == AUTH_URL)

    assert run(scenario()) == 2


def test_concurrent_callers_cause_one_exchange(store: TokenStore) -> None:
    """Десять одновременных запросов не дают десяти обменов."""
    recorder = Recorder(json_reply(200, good_auth()))
    clock = Clock()

    async def scenario() -> int:
        async with session(store, recorder, clock) as broker:
            await asyncio.gather(*(broker.access_token() for _ in range(10)))
            return len(recorder.requests)

    assert run(scenario()) == 1


def test_expired_refresh_token_is_not_even_sent(store: TokenStore) -> None:
    """Просроченный токен не отправляется: брокеру нечего отвечать."""
    recorder = Recorder(json_reply(200, good_auth()))
    clock = Clock(START + timedelta(days=91))

    async def scenario() -> BrokerError:
        async with session(store, recorder, clock) as broker:
            with pytest.raises(TokenExpired) as caught:
                await broker.access_token()
            return caught.value

    error = run(scenario())
    assert recorder.requests == [], "запрос ушёл на заведомо мёртвом токене"
    assert "истёк" in error.human
    assert "Токены API" in error.human


def test_answer_without_a_working_token_is_an_error(store: TokenStore) -> None:
    recorder = Recorder(json_reply(200, {"token_type": "bearer", "expires_in": 86400}))
    clock = Clock()

    async def scenario() -> None:
        async with session(store, recorder, clock) as broker:
            with pytest.raises(UnexpectedAnswer):
                await broker.access_token()

    run(scenario())


def test_answer_that_is_not_json(store: TokenStore) -> None:
    recorder = Recorder(lambda request: httpx.Response(200, text="<html>сервис на профилактике"))
    clock = Clock()

    async def scenario() -> None:
        async with session(store, recorder, clock) as broker:
            with pytest.raises(UnexpectedAnswer) as caught:
                await broker.access_token()
            assert "не в формате JSON" in caught.value.human

    run(scenario())


# --- права ---


def test_live_mode_is_refused_on_a_read_only_token(read_only_store: TokenStore) -> None:
    """Пункт приёмки: боевой режим на токене «только чтение» не включается."""
    recorder = Recorder(json_reply(200, good_auth()))
    clock = Clock()

    async def scenario() -> BrokerError:
        async with session(read_only_store, recorder, clock) as broker:
            assert broker.can_trade() is False
            with pytest.raises(ReadOnlyToken) as caught:
                broker.require_trade_rights()
            return caught.value

    error = run(scenario())
    assert "только для чтения" in error.human
    assert "не включается" in error.human
    assert FAKE_REFRESH not in error.human + error.technical


def test_trade_token_passes_the_gate(store: TokenStore) -> None:
    recorder = Recorder(json_reply(200, good_auth()))
    clock = Clock()

    async def scenario() -> None:
        async with session(store, recorder, clock) as broker:
            assert broker.can_trade() is True
            broker.require_trade_rights()

    run(scenario())


# --- предохранитель против заявок ---


#: Правдоподобные написания адреса заявки. Прежний чёрный список подстрок
#: пропускал девять из них: единственное число без слэша на конце
#: ("/api/v1/order") не совпадало ни с одним куском. Список нарочно шире
#: того, что сторож знает: смысл проверки в том, что незнакомый адрес
#: отвергается по умолчанию, а не в том, что он заранее перечислен.
@pytest.mark.parametrize(
    "path",
    [
        "/trade-api-bff-order/api/v1/order",
        "/trade-api-order-service/api/v1/order",
        "/trade-api-bff-order/api/v1/stop-order",
        "/trade-api-trading/api/v1/submit",
        "/api/v1/deal",
        "/api/v1/trade",
        "/api/v1/OrDeR",
        "/trade-api-bff-operations/api/v1/orders",
        "/x/orders/12/cancel",
        "/trade-api-bff-portfolio/api/v1/portfolio/../order",
    ],
)
def test_order_paths_are_refused(store: TokenStore, path: str) -> None:
    """Этап 1 к торговым адресам не обращается — и это проверка, а не обещание."""
    recorder = Recorder(json_reply(200, good_auth()))
    clock = Clock()

    async def scenario() -> None:
        async with session(store, recorder, clock) as broker:
            with pytest.raises(RuntimeError) as caught:
                await broker.read(path)
            assert "этап 2" in str(caught.value)

    run(scenario())
    assert recorder.requests == [], "запрос к торговому адресу всё-таки ушёл"


def test_no_order_functions_exist() -> None:
    """В слое нет ни одного метода, похожего на подачу заявки."""
    import broker

    forbidden = ("order", "заявк", "buy", "sell", "cancel", "submit")
    offenders = [
        name
        for name in dir(broker)
        if not name.startswith("_") and any(word in name.lower() for word in forbidden)
    ]
    assert not offenders, f"в фасаде слоя появилось торговое имя: {offenders}"
    assert READ_ENDPOINTS, "белый список обращений опустел"
    # Четвёртым адресом 04.09.2026 добавлены исторические свечи (`CANDLES_PATH`):
    # догрузка пропущенного после обрыва, баг B-006. Запрос читающий.
    # Пятым и шестым 05.09.2026 — расписание торгов на день и статус торгов
    # (`DAILY_SCHEDULE_PATH`, `TRADING_STATUS_PATH`): программа не знала,
    # работает ли биржа, и ночью пробовала подключиться до утра. Оба читающие.
    assert len(READ_ENDPOINTS) == 6, (
        "в белый список добавили обращение: это осознанное действие, и его "
        "надо подтвердить здесь. ⚠️ Повтор при этом НЕ включается: таблица "
        "`REPEATABLE` отдельная и выписана руками. Если новое обращение "
        "действительно ничего не меняет — внесите его туда же и в "
        "`EXPECTED_REPEATABLE`; если меняет — не вносите никуда, кроме "
        "белого списка"
    )
    assert len(ALLOWED_READ_PATHS) == len(READ_ENDPOINTS), (
        "у одного адреса появилось два глагола. Само по себе это законно "
        "(у БКС так живут подача заявки и чтение её статуса), но проверьте "
        "оба: список адресов теряет глагол и такую пару не различает"
    )


def test_absolute_url_is_refused(store: TokenStore) -> None:
    """Полный адрес уводит запрос мимо base_url — то есть мимо подставного хоста.

    httpx при абсолютном URL игнорирует `base_url`. Строка с полным адресом,
    попавшая в вызов `read()`, ушла бы на боевой хост брокера в обход всех
    настроек транспорта, включая тестовые.
    """
    recorder = Recorder(json_reply(200, good_auth()))
    clock = Clock()
    host = "be." + "broker" + ".ru"

    async def scenario() -> None:
        async with session(store, recorder, clock) as broker:
            for path in (
                f"https://{host}{PORTFOLIO_PATH}",
                f"//{host}{LIMITS_PATH}",
                f"http://127.0.0.1{LIMITS_PATH}",
            ):
                with pytest.raises(RuntimeError) as caught:
                    await broker.read(path)
                assert "мимо настроек транспорта" in str(caught.value)

    run(scenario())
    assert recorder.requests == [], "запрос по полному адресу всё-таки ушёл"


# --- отказы ---


@pytest.mark.parametrize(
    ("status", "expected", "must_say"),
    [
        (401, AuthFailed, "не принял токен"),
        (403, AuthFailed, "не принял токен"),
        (404, NotFound, "не нашёл"),
        (400, BadRequest, "ошибка программы"),
        (429, Throttled, "слишком много запросов"),
        (500, ServerFailure, "на его стороне"),
        (503, ServerFailure, "на его стороне"),
    ],
)
def test_each_http_failure_has_its_own_human_message(
    store: TokenStore, status: int, expected: type, must_say: str
) -> None:
    recorder = Recorder(json_reply(status, {"error": "какой-то код"}))
    clock = Clock()

    async def scenario() -> BrokerError:
        async with session(store, recorder, clock, attempts=2) as broker:
            with pytest.raises(BrokerError) as caught:
                await broker.access_token()
            return caught.value

    error = run(scenario())
    assert isinstance(error, expected), f"{status} → {type(error).__name__}"
    assert must_say in error.human, error.human
    assert str(status) in error.technical
    assert str(status) not in error.human, "код ответа попал в текст для человека"


def test_connection_loss_is_reported_as_such(store: TokenStore) -> None:
    recorder = Recorder(
        raising(httpx.ConnectError("нет сети", request=httpx.Request("POST", AUTH_URL)))
    )
    clock = Clock()

    async def scenario() -> BrokerError:
        async with session(store, recorder, clock, attempts=2) as broker:
            with pytest.raises(NoConnection) as caught:
                await broker.access_token()
            return caught.value

    error = run(scenario())
    assert "Нет связи" in error.human
    assert "не принимает решений" in error.human
    assert error.retryable is True


def test_timeout_says_how_long_we_waited(store: TokenStore) -> None:
    recorder = Recorder(
        raising(httpx.ReadTimeout("долго", request=httpx.Request("POST", AUTH_URL)))
    )
    clock = Clock()

    async def scenario() -> BrokerError:
        async with session(store, recorder, clock, attempts=1, timeout=7.0) as broker:
            with pytest.raises(Timeout) as caught:
                await broker.access_token()
            return caught.value

    error = run(scenario())
    assert "7 с" in error.human, error.human


# --- повторы ---


def test_retry_helps_when_the_server_recovers(store: TokenStore) -> None:
    """Отказ 500 повторяется, и второй ответ принимается."""
    recorder = Recorder(json_reply(500, {"error": "INTERNAL_SERVER_ERROR"}), json_reply(200, good_auth()))
    clock = Clock()

    async def scenario() -> Any:
        async with session(store, recorder, clock, attempts=3) as broker:
            return await broker.access_token()

    access = run(scenario())
    assert access.secret == Secret(FAKE_ACCESS)
    assert len(recorder.requests) == 2


def test_permanent_failure_is_not_retried(store: TokenStore) -> None:
    """400 повторять бессмысленно: ответ не изменится, а лимит израсходуется."""
    recorder = Recorder(json_reply(400, {"error": "VALIDATION_ERROR"}))
    clock = Clock()

    async def scenario() -> None:
        async with session(store, recorder, clock, attempts=3) as broker:
            with pytest.raises(BadRequest):
                await broker.access_token()

    run(scenario())
    assert len(recorder.requests) == 1


def test_retries_stop_at_the_limit(store: TokenStore) -> None:
    recorder = Recorder(json_reply(500, {"error": "INTERNAL_SERVER_ERROR"}))
    clock = Clock()

    async def scenario() -> None:
        async with session(store, recorder, clock, attempts=3) as broker:
            with pytest.raises(ServerFailure):
                await broker.access_token()

    run(scenario())
    assert len(recorder.requests) == 3


#: Каталог снимка документации БКС — **независимый от кода источник правды**
#: про адреса брокера. Снят руками владельца счёта 04.09.2026 со страниц
#: `trade-api.bcs.ru`, лежит в git и правкой `broker/session.py` не двигается.
BROKER_API_DOCS = pathlib.Path(__file__).resolve().parent.parent / ".docs" / "broker-api"

#: Заголовок страницы снимка: строка с глаголом, следом строка вида
#: `## https://be.broker.ru/<сервис> /api/v1/<путь>`.
DOC_VERB = re.compile(r"^(GET|POST|PUT|PATCH|DELETE)$")
DOC_ADDRESS = re.compile(r"^##\s+https://be\.broker\.ru(/[\w-]+)\s+(/\S+)$")
#: Тот же сервис, названный в прозе страницы вторым разом. Нужен канарейкой:
#: разбор, поймавший мусор, обязан на нём споткнуться, а не выдать пустоту.
DOC_BASE = re.compile(r"^Base URL `https://be\.broker\.ru(/[\w-]+)`")


def endpoint_from_docs(page: str) -> tuple[str, str]:
    """Пара «глагол и адрес» со страницы снимка документации БКС.

    Разбирается заголовок страницы, а сервис сверяется со строкой `Base URL`
    в её прозе — то есть с **другим полем той же страницы**. Без этой сверки
    сломавшийся разбор вернул бы правдоподобную чепуху, и храповик,
    построенный на ней, стал бы зелёным навсегда.
    """
    lines = (BROKER_API_DOCS / page).read_text(encoding="utf-8").splitlines()
    found: list[tuple[str, str]] = []
    service = ""
    for number, line in enumerate(lines):
        base = DOC_BASE.match(line.strip())
        if base is not None:
            service = base.group(1)
        if not DOC_VERB.match(line.strip()) or number + 1 >= len(lines):
            continue
        address = DOC_ADDRESS.match(lines[number + 1].strip())
        if address is not None:
            found.append((line.strip(), address.group(1) + address.group(2)))
    assert len(found) == 1, f"{page}: заголовков с адресом не один, а {len(found)}"
    verb, path = found[0]
    assert service and path.startswith(service + "/"), (
        f"{page}: сервис в заголовке ({path}) разошёлся с прозой ({service}) — "
        "страница снимка изменилась, храповик надо перечитать глазами"
    )
    return verb, path


#: Настоящие **торговые** адреса БКС: подача, изменение и снятие заявки.
#: Пара «глагол и адрес» переписана со страниц снимка, а не сочинена — прежде
#: здесь стояли два несуществующих написания с UUID в пути (находка Н-4),
#: и храповик на них не стерёг ничего. Что литерал совпадает со снимком,
#: проверяет `test_the_trading_addresses_are_the_ones_the_documentation_names`.
ORDER_ENDPOINTS = (
    ("POST", "/trade-api-bff-operations/api/v1/orders"),  # 13-create-order.md
    ("POST", "/trade-api-bff-operations/api/v1/orders/edit"),  # 14-update-order.md
    ("POST", "/trade-api-bff-order-details/api/v1/orders/cancel"),  # 12-cancel-order.md
)

#: Страницы снимка, откуда взята каждая строка `ORDER_ENDPOINTS`. Порядок тот же.
ORDER_PAGES = ("13-create-order.md", "14-update-order.md", "12-cancel-order.md")

#: **Читающие** адреса вокруг заявок. Здесь они затем, что похожи на торговые
#: до неразличимости: `GET .../api/v1/orders` — тот же адрес, что у подачи,
#: только глагол другой; `POST .../orders/search` — тот же глагол и то же
#: слово `orders`, что у изменения заявки. Если храповик разводит эти пары
#: с `ORDER_ENDPOINTS`, он разводит чтение и торговлю, а не ищет подстроку.
READING_AROUND_ORDERS = (
    ("GET", "/trade-api-bff-operations/api/v1/orders"),  # 11-get-order-by-id.md
    ("POST", "/trade-api-market-data-connector/api/v1/orders/search"),  # 15
    ("POST", "/trade-api-bff-operations/api/v1/trades/search"),  # 16
)
READING_PAGES = ("11-get-order-by-id.md", "15-orders-search.md", "16-trades-search.md")

#: Первый торговый адрес — подача заявки. Именем, а не индексом: `ORDER_ENDPOINTS[0]`
#: в пяти местах не говорит читателю, что именно там подаётся.
CREATE_ORDER = ORDER_ENDPOINTS[0]


async def through_transport(
    broker: BrokerSession, endpoint: tuple[str, str], send: Callable[[], Any]
) -> None:
    """Отправить запрос транспортом слоя, минуя `read()`.

    Так его позовёт подача заявки на этапе 4: `read()` к торговым адресам
    не пускает вовсе (`_guard`), а транспорт под ним — общий. Обращение
    к закрытому методу собрано в одну точку нарочно: оно должно быть
    на виду в одном месте, а не в пяти.

    Глагол берётся из пары, а не зашит: повтор решает пара целиком, и тест,
    умеющий послать только `POST`, эту половину проверить не может.
    """
    method, path = endpoint
    await broker._with_retries(method, path, send)


def lost_answer(calls: list[int]) -> Callable[[], Any]:
    """Отправка, у которой ответ не пришёл. Каждый вызов метит `calls`."""

    async def sending() -> httpx.Response:
        calls.append(1)
        raise httpx.ReadTimeout("нет ответа", request=httpx.Request("POST", AUTH_URL))

    return sending


@pytest.mark.parametrize("endpoint", ORDER_ENDPOINTS)
def test_a_write_request_is_sent_once_and_never_twice(
    store: TokenStore, endpoint: tuple[str, str]
) -> None:
    """Дубль заявки не подаётся. `B-002`, и это про деньги на счёте.

    Проверяется **не** «функция вернула отказ», а что второй отправки
    не было: таймаут означает, что не пришёл ответ, а не что заявка
    не дошла. Повтор здесь — вторая заявка на срочном рынке: удвоенный
    объём, удвоенное обеспечение и убыток, которого никто не планировал.

    Мутация, на которую тест обязан упасть: разрешить повтор пишущему
    запросу (внести адрес в `REPEATABLE` с глаголом `POST` или вернуть
    транспорту рычаг «повторяй»).

    Ловится **только дубль**, а вид отказа нарочно не проверяется: чем
    именно транспорт называет неизвестный исход, стережёт соседний тест.
    Иначе одна мутация роняла бы оба, и было бы не видно, что чему.
    """
    recorder = Recorder(json_reply(200, good_auth()))
    clock = Clock()
    calls: list[int] = []
    sending = lost_answer(calls)

    async def scenario() -> None:
        async with session(store, recorder, clock, attempts=5) as broker:
            with pytest.raises(BrokerError):
                await through_transport(broker, endpoint, sending)

    run(scenario())
    assert calls == [1], (
        f"дубль: запрос, меняющий состояние счёта, отправлен {len(calls)} раз "
        "вместо одного"
    )


def test_the_transport_has_no_switch_that_turns_repeating_on() -> None:
    """У `_with_retries` нет аргумента, которым повтор можно попросить.

    Храповик на будущее. До 06.09.2026 разрешение ставил вызывающий флагом
    `repeatable=True`; умолчание было безопасным, но рычаг оставался, и
    дёрнуть его предстояло тому, кто придёт сюда с подачей заявки — по
    образцу соседней строки, где так написано у чтения. Рычага быть
    не должно: повтор решает таблица `REPEATABLE`.
    """
    parameters = list(inspect.signature(BrokerSession._with_retries).parameters)
    assert parameters == ["self", "method", "where", "send"], (
        "у транспорта появился лишний аргумент. Если это разрешение повторять "
        "— его дёрнет тот, кто подаёт заявку, и на счёте окажется вторая "
        "заявка (B-002). Повтор задаётся таблицей REPEATABLE, а не вызовом"
    )


#: Что имеет право повторяться — выписано здесь **отдельно от продуктового
#: кода и целиком**, парами. Это половина храповика над `REPEATABLE`.
#:
#: Прежний сторож строил ожидаемое из тех же `READ_METHODS` и того же белого
#: списка, из которых собиралась сама таблица, — и потому был истинным
#: по построению (находка Н-2: мутация `READ_METHODS += ("PUT", "DELETE")`
#: дала 3489 passed, 0 failed). Список, переписанный руками, тавтологией быть
#: не может: он не меняется, когда меняется код, и любое изменение состава
#: таблицы обязано быть подтверждено здесь вторым человеком.
EXPECTED_REPEATABLE = {
    ("POST", AUTH_EXCHANGE),
    ("GET", PORTFOLIO_PATH),
    ("GET", LIMITS_PATH),
    ("POST", INSTRUMENTS_BY_TICKERS_PATH),
    ("GET", CANDLES_PATH),
    ("GET", DAILY_SCHEDULE_PATH),
    ("GET", TRADING_STATUS_PATH),
}


def test_the_repeat_table_holds_exactly_what_is_written_out_here() -> None:
    """Что в таблице повторов — то и повторяется. Больше ничего.

    Храповик про **состав** таблицы, а не про поведение одного вызова:
    обращение, попавшее сюда по недосмотру, включает повтор молча.

    Сверяется с `EXPECTED_REPEATABLE` — списком, выписанным руками рядом.
    Почему это независимо от источника: `REPEATABLE` в продуктовом коде
    больше ниоткуда не выводится, а этот список не выводится из неё. Два
    независимых перечисления совпадают только тогда, когда их обновили оба,
    то есть когда изменение заметил человек. Прежняя проверка строила
    ожидаемое из тех же данных, что и таблицу, и молчала на любой мутации
    источника.

    Вторая половина — включение в белый список чтения. Она односторонняя
    нарочно: `REPEATABLE` не обязана покрывать `READ_ENDPOINTS`, но не имеет
    права выходить за него — повторяться может лишь то, что и запрашивать-то
    слою разрешено.

    ⚠️ **Здесь стояло «забытая строка стоит лишнего отказа на обрыве,
    и только». Это перестало быть правдой** (`D-086`, 06.09.2026): забытая
    строка при потере ответа поднимает `OutcomeUnknown`, а он останавливает
    робота остановкой, которую снимает человек. Само требование к этой
    проверке не меняется — оно про состав таблицы, — но покрытие
    `READ_ENDPOINTS` теперь стережёт отдельный храповик
    (`tests/test_app_unknown_outcome_halts.py`).
    """
    assert set(REPEATABLE) == EXPECTED_REPEATABLE, (
        "состав таблицы повторов изменился. Это решение про деньги: повтор "
        "разрешён только тому, от чьей второй отправки не меняется ничего. "
        f"лишнее {set(REPEATABLE) - EXPECTED_REPEATABLE}, "
        f"потерянное {EXPECTED_REPEATABLE - set(REPEATABLE)}"
    )
    beyond = set(REPEATABLE) - {("POST", AUTH_EXCHANGE)} - set(READ_ENDPOINTS)
    assert not beyond, (
        f"повторяется то, что слою не разрешено даже спрашивать: {beyond}. "
        "Таблица повторов вправе быть уже белого списка чтения, но не шире"
    )


@pytest.mark.parametrize("endpoint", ORDER_ENDPOINTS)
def test_no_trading_address_of_the_broker_is_readable_or_repeatable(
    endpoint: tuple[str, str],
) -> None:
    """Ни подача, ни изменение, ни снятие заявки сюда не попали.

    Это **независимый от нашего кода** сторож: пары взяты со страниц снимка
    документации БКС (проверяет `test_the_trading_addresses_are_the_ones_the_
    documentation_names`), а не из `broker/session.py`. Сломать источник
    таблицы повторов и остаться зелёным на этом нельзя: адреса брокера
    от наших правок не меняются.

    Мутация, на которую тест обязан упасть: внести торговый адрес в белый
    список чтения (например `POST .../orders/edit`, «изменение заявки»)
    и поднять счётчик соседнего храповика — ровно так, как предписывает
    его собственное сообщение. Изменение у БКС выполняется **через отмену
    исходной заявки и создание новой** (`14-update-order.md`), то есть
    повторённое изменение — это вторая заявка на рынке.
    """
    assert endpoint not in REPEATABLE, (
        f"торговый адрес {endpoint} внесён в таблицу повторов — это `B-002` "
        "обратно: потерянный ответ обернётся второй заявкой на рынке"
    )
    assert endpoint not in READ_ENDPOINTS, (
        f"торговый адрес {endpoint} внесён в белый список чтения. Заявки — "
        "этап 2, и заводятся они отдельной задачей с отдельным ревью"
    )


def test_the_layer_has_no_such_thing_as_a_reading_verb() -> None:
    """Понятия «читающий глагол» в слое нет, и вернуться оно не должно.

    Храповик той же формы, что `test_the_transport_has_no_switch_that_turns_
    repeating_on`: он стережёт **отсутствие** вещи, из-за которой уже был
    дефект. Здесь это константа `READ_METHODS` — «глаголы, которыми слой
    читает». Из неё декартовым произведением собиралась таблица повторов,
    и потому каждый адрес белого списка получал оба глагола разом (Н-1),
    а сторож состава сверял таблицу с той же константой и молчал на её
    расширении (Н-2, мутация `READ_METHODS += ("PUT", "DELETE")`:
    3489 passed, 0 failed).

    Понятие ложно по существу, а не по реализации. `POST` — это чтение
    у справочника инструментов и подача заявки по адресу `.../api/v1/orders`;
    `GET` по **тому же** адресу отдаёт статус заявки. Читает не глагол,
    читает пара.

    Заодно проверяется, что слой на этапе 1 не умеет ни одного изменяющего
    глагола: `PUT`, `PATCH` и `DELETE` не нужны ни одному читающему методу
    БКС, и появление любого из них — повод для разговора, а не для правки
    списка.
    """
    import broker.session as layer

    assert not hasattr(layer, "READ_METHODS"), (
        "в слое снова заведён список «читающих глаголов». Читающего глагола "
        "не существует: POST бывает и чтением справочника, и подачей заявки. "
        "Допуск и повтор задаются парой «глагол и адрес», а не глаголом"
    )
    verbs = {verb for verb, _ in READ_ENDPOINTS}
    assert verbs <= {"GET", "POST"}, (
        f"слой научился глаголу {verbs - {'GET', 'POST'}}. На этапе 1 он "
        "только читает, а изменяющий глагол читающему методу БКС не нужен "
        "ни одному"
    )


def test_the_trading_addresses_are_the_ones_the_documentation_names() -> None:
    """Храповик стережёт настоящие адреса брокера, а не правдоподобные.

    Находка Н-4: два адреса из трёх были сочинены (`.../orders/{uuid}` и
    `.../orders/{uuid}/cancel`); UUID попал в них из **тела** запроса-примера.
    Сторож на несуществующем адресе не стережёт ничего — брокер такого
    запроса не получит никогда.

    Литерал сверяется со снимком документации: `.docs/broker-api/`, страницы
    12, 13 и 14. Снимок — не наш код, его правит владелец счёта по сайту
    брокера. Разошлись — читать глазами: либо у брокера поменялся адрес,
    либо кто-то поправил литерал, не заглянув в документацию.

    Читающие адреса вокруг заявок сверяются тем же движением и по той же
    причине: `GET .../api/v1/orders` живёт по **тому же адресу**, что подача,
    и разница между ними — ровно глагол.
    """
    assert BROKER_API_DOCS.is_dir(), (
        f"снимка документации нет по пути {BROKER_API_DOCS}: сверять храповик "
        "не с чем, и он превращается в список из головы"
    )
    for page, endpoint in zip(ORDER_PAGES, ORDER_ENDPOINTS, strict=True):
        assert endpoint_from_docs(page) == endpoint, (
            f"{page}: адрес в документации разошёлся с храповиком "
            f"({endpoint_from_docs(page)} против {endpoint})"
        )
    for page, endpoint in zip(READING_PAGES, READING_AROUND_ORDERS, strict=True):
        assert endpoint_from_docs(page) == endpoint, (
            f"{page}: адрес в документации разошёлся с храповиком "
            f"({endpoint_from_docs(page)} против {endpoint})"
        )
    assert ORDER_ENDPOINTS[0][1] == READING_AROUND_ORDERS[0][1], (
        "подача заявки и чтение её статуса разъехались по адресам — значит "
        "исчезла причина, по которой ключ таблицы повторов состоит из пары. "
        "Проверить по документации и переписать объяснение у `REPEATABLE`"
    )


@pytest.mark.parametrize(
    ("method", "path", "sent", "what"),
    [
        ("GET", PORTFOLIO_PATH, 3, "чтение портфеля"),
        ("POST", PORTFOLIO_PATH, 1, "POST по адресу портфеля"),
        ("POST", INSTRUMENTS_BY_TICKERS_PATH, 3, "справочник инструментов"),
        ("GET", INSTRUMENTS_BY_TICKERS_PATH, 1, "GET по адресу справочника"),
    ],
)
def test_the_verb_decides_whether_a_lost_answer_is_repeated(
    store: TokenStore, method: str, path: str, sent: int, what: str
) -> None:
    """Один адрес, два глагола — два разных решения про повтор.

    Здесь проверяется само обещание, записанное у `REPEATABLE`: ключ состоит
    из пары потому, что у БКС по одному адресу `GET` отдаёт статус заявки,
    а `POST` её создаёт. Обещание было ложным (находка Н-1): таблица
    собиралась декартовым произведением, и **каждый** адрес белого списка
    получал оба глагола разом.

    Мутация, на которую тест обязан упасть: вернуть построение таблицы
    произведением «адреса × глаголы». Взяты два настоящих адреса, у которых
    читающий глагол разный: портфель читается `GET`, справочник инструментов
    по тикерам — `POST` с телом. Значит проверяется именно глагол, а не
    привычка звать всё через `GET`.
    """
    recorder = Recorder(json_reply(200, good_auth()))
    clock = Clock()
    calls: list[int] = []
    sending = lost_answer(calls)

    async def scenario() -> None:
        async with session(store, recorder, clock, attempts=3) as broker:
            with pytest.raises(BrokerError):
                await through_transport(broker, (method, path), sending)

    run(scenario())
    assert len(calls) == sent, (
        f"{what}: отправок {len(calls)}, а должно быть {sent}. Повтор решает "
        "пара «глагол и адрес», а не адрес отдельно"
    )


@pytest.mark.parametrize(
    ("method", "path", "allowed"),
    [
        ("POST", PORTFOLIO_PATH, "GET"),
        ("GET", INSTRUMENTS_BY_TICKERS_PATH, "POST"),
        ("DELETE", LIMITS_PATH, "GET"),
        ("PUT", CANDLES_PATH, "GET"),
        ("PATCH", TRADING_STATUS_PATH, "GET"),
    ],
)
def test_read_refuses_a_verb_this_address_does_not_take(
    store: TokenStore, method: str, path: str, allowed: str
) -> None:
    """`read()` сторожит пару, а не путь. Находка Н-3.

    Прежде глагол не проверялся ничем: `read(PORTFOLIO_PATH, method="DELETE")`
    уходил в сеть. Сегодня по этим адресам ничего не меняется, но связка
    «любой глагол + знакомый адрес» — это дорога к подаче заявки через метод,
    который называется `read`.

    Проверяется не только отказ, но и что запрос **не ушёл**: сторож,
    поднимающий исключение после отправки, защищает только журнал.
    """
    recorder = Recorder(json_reply(200, good_auth()))
    clock = Clock()

    async def scenario() -> str:
        async with session(store, recorder, clock) as broker:
            with pytest.raises(RuntimeError) as caught:
                await broker.read(path, method=method)
            return str(caught.value)

    said = run(scenario())
    assert recorder.requests == [], f"запрос глаголом {method} всё-таки ушёл"
    assert allowed in said and method in said, (
        f"отказ не называет ни того глагола, что нужен, ни того, что пришёл: {said}"
    )


def test_read_sends_the_very_verb_it_has_checked(store: TokenStore) -> None:
    """Проверенный глагол и отправленный глагол — одна строка.

    Регистр глагола смысла не несёт, поэтому он приводится к верхнему —
    но **до** сторожа, и дальше идёт именно приведённый. Мутация, на которую
    тест обязан упасть: сторожить `method.upper()`, а отправлять `method`.
    Это тот же дефект, что Н-5, только про глагол: сверяется одно,
    отправляется другое.

    С путём так поступать нельзя, и это не непоследовательность: строка
    запроса несёт смысл, а регистр глагола — нет.
    """
    recorder = Recorder(json_reply(200, good_auth()), json_reply(200, {"ok": True}))
    clock = Clock()

    async def scenario() -> None:
        async with session(store, recorder, clock) as broker:
            await broker.read(PORTFOLIO_PATH, method="get")

    run(scenario())
    assert recorder.requests[-1].method == "GET", (
        f"в сеть ушёл глагол {recorder.requests[-1].method}, а сторож смотрел "
        "на приведённый к верхнему регистру"
    )
    assert str(recorder.requests[-1].url).endswith(PORTFOLIO_PATH)


@pytest.mark.parametrize(
    "path",
    [
        PORTFOLIO_PATH + "?action=cancel&id=7",
        PORTFOLIO_PATH + "/",
        PORTFOLIO_PATH + "///",
    ],
)
def test_read_sends_only_the_string_it_has_checked(
    store: TokenStore, path: str
) -> None:
    """Сверяется и отправляется одна и та же строка. Находка Н-5.

    Прежде сторож приводил путь к «голому» виду, а в сеть уходил исходный:
    `read(PORTFOLIO_PATH + "?action=cancel&id=7")` проходил проверку и
    отправлялся как есть. У БКС действие задаётся глаголом и телом, так что
    цены сегодня у этого нет, — но правило «проверяем не то, что отправляем»
    негодно само по себе, и стоит его исправление одной ветки.

    Приглаживать путь нельзя: тогда программа молча отправила бы не то, что
    её просили. Поэтому расхождение называется вслух отказом.
    """
    recorder = Recorder(json_reply(200, good_auth()))
    clock = Clock()

    async def scenario() -> str:
        async with session(store, recorder, clock) as broker:
            with pytest.raises(RuntimeError) as caught:
                await broker.read(path)
            return str(caught.value)

    said = run(scenario())
    assert recorder.requests == [], f"адрес {path} ушёл в сеть непроверенным"
    assert "params" in said, f"отказ не подсказывает, как передать параметры: {said}"


def test_a_lost_answer_on_a_write_says_the_outcome_is_unknown(
    store: TokenStore,
) -> None:
    """Про неизвестный исход программа говорит словами, а не молчит.

    Правило 13 `CLAUDE.md`: отказ, не доехавший до человека, равен поломке.
    Обычный `Timeout` здесь сказал бы владельцу счёта прямо обратное —
    «попробуем ещё раз», — а повтора не будет и заявка, возможно, уже живёт
    у брокера. Мутация, на которую тест обязан упасть: поднять на пишущем
    запросе обычный таймаут вместо `OutcomeUnknown`.
    """
    recorder = Recorder(json_reply(200, good_auth()))
    clock = Clock()
    sending = lost_answer([])

    async def scenario() -> OutcomeUnknown:
        async with session(store, recorder, clock, attempts=3) as broker:
            with pytest.raises(OutcomeUnknown) as caught:
                await through_transport(broker, CREATE_ORDER, sending)
            return caught.value

    error = run(scenario())
    said = error.human
    assert "Неизвестно" in said, said
    assert "не повторяет" in said, said
    assert "Проверьте" in said, said
    assert "попробуем" not in said.lower(), f"обещан повтор, которого не будет: {said}"
    assert error.retryable is False, "исход неизвестен — повторять нельзя"
    assert "неизвестен" in error.technical, error.technical
    for leak in ("http", "://", "orders", "api/v1"):
        assert leak not in said.lower(), f"в текст человеку попало техническое: {said}"


_ASKED = httpx.Request("POST", AUTH_URL)


@pytest.mark.parametrize(
    "reply, cause",
    [
        (raising(httpx.ReadTimeout("нет ответа", request=_ASKED)), "таймаут"),
        (raising(httpx.ConnectError("нет сети", request=_ASKED)), "нет связи"),
        (json_reply(500, {"type": "INTERNAL_SERVER_ERROR"}), "5xx"),
        (json_reply(429, {"type": "RESOURCE_EXHAUSTED"}), "отказ по частоте"),
    ],
)
def test_any_inconclusive_answer_to_a_write_is_an_unknown_outcome(
    store: TokenStore, reply: Callable[[httpx.Request], httpx.Response], cause: str
) -> None:
    """Неокончательный ответ на пишущий запрос — всегда «исход неизвестен».

    Документация БКС про 5xx и 429 не утверждает, что заявка **не** принята:
    таблицы «Описание исключительных ситуаций» описывают код ответа, а не
    судьбу заявки. Перестраховка стоит одного запроса статуса, недостача —
    денег на счёте.
    """
    recorder = Recorder(json_reply(200, good_auth()))
    clock = Clock()
    calls: list[int] = []

    async def sending() -> httpx.Response:
        calls.append(1)
        return reply(httpx.Request("POST", AUTH_URL))

    async def scenario() -> None:
        async with session(store, recorder, clock, attempts=4) as broker:
            with pytest.raises(OutcomeUnknown):
                await through_transport(broker, CREATE_ORDER, sending)

    run(scenario())
    assert calls == [1], f"{cause}: пишущий запрос был отправлен ещё раз"


def test_a_refusal_the_broker_stated_is_not_dressed_up_as_unknown(
    store: TokenStore,
) -> None:
    """Брокер сказал «заявка отвергнута» — значит она отвергнута, и точка.

    Обратная сторона предыдущего теста: пугать владельца счёта неизвестным
    исходом там, где брокер дал окончательный ответ, — такой же дефект.
    400 и 401 означают, что заявка не принята.
    """
    recorder = Recorder(json_reply(200, good_auth()))
    clock = Clock()

    async def scenario() -> None:
        async with session(store, recorder, clock, attempts=3) as broker:
            for status, expected in ((400, BadRequest), (401, AuthFailed)):

                async def sending(code: int = status) -> httpx.Response:
                    return httpx.Response(code, json={"type": "VALIDATION_ERROR"})

                with pytest.raises(expected):
                    await through_transport(broker, CREATE_ORDER, sending)

    run(scenario())


def test_a_read_that_never_answers_is_a_timeout_and_not_an_unknown_outcome(
    store: TokenStore,
) -> None:
    """Чтение остаётся чтением: три попытки и обычный таймаут в конце.

    Сторож против перестраховки: если «исход неизвестен» начнёт приходить
    на опрос портфеля, владелец счёта будет получать пугающий текст про
    заявки на каждом обрыве связи, а обрывы у него частые.
    """
    recorder = Recorder(
        json_reply(200, good_auth()),
        raising(httpx.ReadTimeout("долго", request=httpx.Request("GET", AUTH_URL))),
    )
    clock = Clock()

    async def scenario() -> None:
        async with session(store, recorder, clock, attempts=3) as broker:
            with pytest.raises(Timeout):
                await broker.read(PORTFOLIO_PATH)

    run(scenario())
    assert len(recorder.requests) == 4, "чтение повторялось не трижды"


def test_reading_is_repeated_because_reading_changes_nothing(store: TokenStore) -> None:
    """Чтение повторяется по-прежнему: обрыв связи не должен ронять опрос."""
    recorder = Recorder(
        json_reply(200, good_auth()),
        raising(httpx.ReadTimeout("долго", request=httpx.Request("GET", AUTH_URL))),
        json_reply(200, {"positions": []}),
    )
    clock = Clock()

    async def scenario() -> Any:
        async with session(store, recorder, clock, attempts=3) as broker:
            return await broker.read(PORTFOLIO_PATH)

    assert run(scenario()) == {"positions": []}
    assert len(recorder.requests) == 3, "повтор чтения после таймаута не состоялся"


def test_token_exchange_survives_a_lost_answer(store: TokenStore) -> None:
    """Обмен токена повторяется: он не меняет ничего ни на счёте, ни у брокера."""
    recorder = Recorder(
        raising(httpx.ConnectError("нет сети", request=httpx.Request("POST", AUTH_URL))),
        json_reply(200, good_auth()),
    )
    clock = Clock()

    async def scenario() -> Any:
        async with session(store, recorder, clock, attempts=3) as broker:
            return await broker.access_token()

    assert run(scenario()).secret == Secret(FAKE_ACCESS)
    assert len(recorder.requests) == 2


def test_stale_access_token_is_dropped_on_401(store: TokenStore) -> None:
    """Токен, удалённый в кабинете, убивает и рабочий — обмен повторяется."""
    recorder = Recorder(
        json_reply(200, good_auth()),
        json_reply(401, {"error": "UNAUTHORIZED"}),
        json_reply(200, good_auth()),
    )
    clock = Clock()

    async def scenario() -> Any:
        async with session(store, recorder, clock, attempts=1) as broker:
            await broker.access_token()
            with pytest.raises(AuthFailed):
                await broker.read(PORTFOLIO_PATH)
            # Рабочий токен сброшен: следующий запрос идёт через новый обмен.
            return await broker.access_token()

    access = run(scenario())
    assert access.secret == Secret(FAKE_ACCESS)
    auth_calls = [request for request in recorder.requests if str(request.url) == AUTH_URL]
    assert len(auth_calls) == 2


# --- состояние связи ---


def test_outage_is_recorded_and_closed(store: TokenStore) -> None:
    """Обрыв виден в состоянии, а восстановление его закрывает."""
    recorder = Recorder(
        raising(httpx.ConnectError("нет сети", request=httpx.Request("POST", AUTH_URL))),
        raising(httpx.ConnectError("нет сети", request=httpx.Request("POST", AUTH_URL))),
        json_reply(200, good_auth()),
    )
    clock = Clock()

    async def scenario() -> ConnectionState:
        async with session(store, recorder, clock, attempts=3) as broker:
            await broker.access_token()
            return broker.connection

    state = run(scenario())
    assert state.online is True
    assert len(state.history) == 1
    assert state.history[0].ended_at is not None


def test_header_carries_the_working_token(store: TokenStore) -> None:
    """Рабочий токен уходит в заголовке — иначе брокер не ответит."""
    recorder = Recorder(json_reply(200, good_auth()), json_reply(200, {"ok": True}))
    clock = Clock()

    async def scenario() -> None:
        async with session(store, recorder, clock) as broker:
            await broker.read(LIMITS_PATH)

    run(scenario())
    data_request = recorder.requests[-1]
    assert data_request.headers["Authorization"] == f"Bearer {FAKE_ACCESS}"
    assert data_request.url.path == LIMITS_PATH


def test_rate_limit_is_not_reported_as_a_lost_connection(store: TokenStore) -> None:
    """429 — это не обрыв. Красная панель «нет связи» сказала бы неправду.

    Связь при отказе по частоте есть, брокер отвечает. Счётчик времени
    без связи в этом случае не запускается, но отказ в журнал попадает.
    """
    recorder = Recorder(json_reply(429, {"error": "too many"}))
    clock = Clock()

    async def scenario() -> ConnectionState:
        async with session(store, recorder, clock, attempts=2) as broker:
            with pytest.raises(Throttled):
                await broker.access_token()
            return broker.connection

    state = run(scenario())
    assert state.online is True, "отказ по частоте засчитан как обрыв связи"
    assert state.history == []


def test_server_failure_is_reported_as_a_lost_connection(store: TokenStore) -> None:
    """5xx — обрыв по сути: данных нет, решать не на чем."""
    recorder = Recorder(json_reply(500, {"error": "INTERNAL_SERVER_ERROR"}))
    clock = Clock()

    async def scenario() -> ConnectionState:
        async with session(store, recorder, clock, attempts=2) as broker:
            with pytest.raises(ServerFailure):
                await broker.access_token()
            return broker.connection

    state = run(scenario())
    assert state.online is False
    assert state.current is not None


def test_importing_the_layer_installs_the_log_filter() -> None:
    """Затирание включается импортом слоя, а не созданием подключения.

    `TokenStore` пишет в журнал раньше, чем появляется `BrokerSession`.
    Если бы фильтр ставился только в конструкторе сессии, гарантия
    «токен не попадает в лог» зависела бы от порядка вызовов.
    """
    import logging

    import broker
    from broker.redaction import FOREIGN_LOGGERS, LOGGER_NAME, RedactingFilter

    filters = logging.getLogger(LOGGER_NAME).filters
    installed = [item for item in filters if isinstance(item, RedactingFilter)]
    assert len(installed) == 1, f"на логгере {LOGGER_NAME} фильтров: {len(installed)}"

    # И ровно своё: `httpx` и `httpcore` — общие ветки процесса, их настраивает
    # `app/` вместе с техническим логом (ARCHITECTURE.md §2). Слой, который
    # правит записи чужого слоя на импорте, ломает их молча и издалека.
    for name in FOREIGN_LOGGERS:
        strangers = [
            item
            for item in logging.getLogger(name).filters
            if isinstance(item, RedactingFilter)
        ]
        assert not strangers, f"слой брокера настроил чужой логгер {name}"
    assert broker.MASK


# --- нарастающая пауза ---


def test_backoff_grows_and_stops_at_the_cap() -> None:
    steady = Backoff(base=1.0, growth=2.0, maximum=60.0, jitter=0.0, random_source=lambda: 0.5)
    assert steady.delay(1) == 1.0
    assert steady.delay(2) == 2.0
    assert steady.delay(3) == 4.0
    assert steady.delay(10) == 60.0
    with pytest.raises(ValueError):
        steady.delay(0)


def test_backoff_spreads_the_attempts() -> None:
    """Без разброса все клиенты после общего сбоя приходят одной волной."""
    low = Backoff(base=10.0, growth=1.0, jitter=0.25, random_source=lambda: 0.0)
    high = Backoff(base=10.0, growth=1.0, jitter=0.25, random_source=lambda: 1.0)
    assert low.delay(1) == pytest.approx(7.5)
    assert high.delay(1) == pytest.approx(12.5)


# --- ограничитель частоты ---


def test_throttle_waits_when_the_bucket_is_empty() -> None:
    """Опрос в цикле без паузы упирается в ограничитель, а не в брокера."""
    time_now = [0.0]
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        time_now[0] += seconds

    throttle = Throttle(rate=2.0, burst=2.0, clock=lambda: time_now[0], sleep=sleep)

    async def scenario() -> None:
        for _ in range(5):
            await throttle.acquire()

    run(scenario())
    assert len(slept) == 3, slept
    assert all(pause > 0 for pause in slept)


# --- тело ответа брокера не доходит до человека (05.09.2026) ---


#: Ровно то, что владелец счёта увидел на экране 05.09.2026 вместо сообщения.
#: Отдаёт это не брокер, а его шлюз (`Angie` — их nginx): до сервиса запрос
#: не дошёл вовсе, потому что в адресе свечей стояло неверное имя сервиса.
GATEWAY_404 = (
    "<html><head><title>404 Not Found</title></head>"
    "<body><center><h1>404 Not Found</h1></center>"
    "<hr><center>Angie</center></body></html>"
)

#: Куски тела, ни один из которых не имеет права оказаться в тексте для человека.
BODY_PIECES = ("<html", "<h1>", "</center>", "Angie", "<title>")


def text_reply(status: int, text: str = GATEWAY_404) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(status, text=text)


@pytest.mark.parametrize("endpoint", sorted(READ_ENDPOINTS))
@pytest.mark.parametrize("status", [400, 401, 404, 429, 500])
def test_the_answer_body_reaches_no_human_text(
    store: TokenStore, endpoint: tuple[str, str], status: int
) -> None:
    """Тело ответа не попадает ни в одну строку, которую читает владелец счёта.

    Проверяются все четыре читаемые человеком поверхности отказа, и `technical`
    в их числе не по перестраховке: `app/backfill` кладёт `failure.technical`
    в пояснение отчёта о загрузке, которое живёт в базе и уходит в выгрузку.

    Все четыре адреса белого списка и пять кодов ответа, а не только свечи
    и не только 404: причина той страницы на экране была своя — неверное имя
    сервиса в адресе, — но собирал текст отказа общий `errors.from_status`,
    и ошибиться адресом можно снова.
    """
    method, path = endpoint
    recorder = Recorder(json_reply(200, good_auth()), text_reply(status))
    clock = Clock()

    async def scenario() -> BrokerError:
        async with session(store, recorder, clock, attempts=1) as broker:
            with pytest.raises(BrokerError) as caught:
                await broker.read(path, method=method)
            return caught.value

    error = run(scenario())
    surfaces = {
        "human": error.human,
        "str": str(error),
        "repr": repr(error),
        "technical": error.technical,
    }
    for where, line in surfaces.items():
        for piece in BODY_PIECES:
            assert piece not in line, f"тело ответа доехало до `{where}`: {line}"
    assert "404 Not Found" in error.details, "тело потеряно и для технического лога"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        # Два кода, а не один: у `_note_failure` две ветки, и обрыв связи
        # (5xx) идёт другой дорогой, чем обычный отказ (4xx). Проверка
        # на одном коде оставила бы вторую ветку без присмотра — и именно
        # эту дыру нашёл прогон мутаций 05.09.2026.
        (400, BadRequest),
        (500, ServerFailure),
    ],
)
def test_the_layer_logs_technical_only_and_never_the_human_text(
    store: TokenStore,
    caplog: pytest.LogCaptureFixture,
    status: int,
    expected: type[BrokerError],
) -> None:
    """Слой брокера в лог человеческого текста не пишет — ни в каком виде.

    Разговор с владельцем счёта — не дело брокера: человеческий текст уезжает
    наверх вместе с отказом, а говорит его окно через порт и журнал решений.
    В лог слой пишет техническое: код, адрес, тип отказа, тело ответа.

    Правило записано 05.09.2026 ценой двух показов подряд. Сперва владелец
    счёта получил в консоли тело ответа, потом — шесть предложений
    человеческого текста оттуда же.
    """
    recorder = Recorder(json_reply(200, good_auth()), text_reply(status))
    clock = Clock()

    async def scenario() -> BrokerError:
        async with session(store, recorder, clock, attempts=1) as broker:
            with pytest.raises(expected) as caught:
                await broker.read(PORTFOLIO_PATH)
            return caught.value

    with caplog.at_level(logging.DEBUG, logger="broker"):
        error = run(scenario())

    written = "\n".join(record.getMessage() for record in caplog.records)
    assert error.human, "у отказа нет человеческого текста вовсе"
    assert error.human not in written, f"человеческий текст ушёл в лог:\n{written}"
    opening = error.human.split(".")[0]
    assert opening not in written, f"начало человеческого текста в логе:\n{written}"

    loud = [record for record in caplog.records if record.levelno >= logging.WARNING]
    quiet = [record for record in caplog.records if record.levelno < logging.WARNING]
    assert any(f"HTTP {status}" in record.getMessage() for record in loud), (
        "отказ не записан техническим текстом — разбирать его будет нечем"
    )
    for record in caplog.records:
        for piece in BODY_PIECES:
            if piece in record.getMessage():
                assert record.levelno < logging.WARNING, (
                    f"тело ответа записано уровнем {record.levelname}: оно длинное "
                    "и нужно редко, ему место на DEBUG"
                )
    assert any("404 Not Found" in record.getMessage() for record in quiet), (
        "тело ответа не записано вовсе — разбирать отказ будет нечем"
    )


def test_the_layer_prints_nothing_to_the_console(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Слой ничего не печатает в консоль, даже когда лог никуда не настроен.

    ⚠️ Печатал. `RedactingSink.emit` намеренно перекидывал записи слоя
    в `logging.lastResort` — аварийный вывод Python в stderr, — чтобы журнал
    был хоть где-то виден до появления технического лога (Э1-18). Через
    эту дорогу владелец счёта и получил на экран сперва разметку страницы
    404 от шлюза брокера, а потом человеческий текст отказа. Консоль —
    не место разговора с ним: его сообщения живут в журнале решений в окне.

    Обработчики корневого логгера снимаются нарочно: под pytest их ставит
    сам прогон, а с ними `lastResort` не срабатывает никогда — и проверка
    была бы зелёной при любой реализации.
    """
    import contextlib
    import io

    from broker.redaction import log

    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    stream = io.StringIO()
    root.handlers = []
    root.setLevel(logging.DEBUG)
    try:
        with contextlib.redirect_stderr(stream):
            log().warning("проверочная строка технического лога")
            log().error("проверочная строка отказа")
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)

    assert stream.getvalue() == "", (
        "слой напечатал в консоль:\n" + stream.getvalue()
    )


# --- неверный адрес: 404 от шлюза, а не от брокера (05.09.2026) ---


def test_a_wrong_address_is_not_a_crash_and_speaks_in_plain_words(
    store: TokenStore,
) -> None:
    """404 страницей шлюза — отдельный отказ с коротким текстом, а не авария.

    Владелец счёта читает **две строки**: что перестало работать и что из
    этого следует. Ни кода ответа, ни слова «HTTP», ни куска адреса в тексте
    нет. Длина проверяется тоже: первая редакция была на шесть предложений,
    и владелец счёта назвал её стеной — подробности, которые он не может
    применить, топят в себе то немногое, что применить можно.
    """
    recorder = Recorder(json_reply(200, good_auth()), text_reply(404))
    clock = Clock()

    async def scenario() -> BrokerError:
        async with session(store, recorder, clock, attempts=1) as broker:
            with pytest.raises(WrongAddress) as caught:
                await broker.read(CANDLES_PATH)
            return caught.value

    error = run(scenario())
    assert error.repeated is False
    assert error.retryable is False, "адрес зашит в код и повтором не исправится"
    assert "Догрузка свечей" in error.human, "не сказано, что перестало работать"
    assert "с биржи" in error.human, "не сказано, что из этого следует"
    for piece in ("404", "HTTP", "candles-chart", "nginx", "шлюз"):
        assert piece not in error.human, f"в тексте для человека техника: {error.human}"
    assert len(error.human) <= 200, (
        f"текст владельцу счёта разросся до {len(error.human)} символов: "
        f"{error.human}"
    )
    assert error.human.count(".") <= 2, f"больше двух предложений: {error.human}"
    assert "404" in error.technical, "код ответа потерян для технического лога"


def test_a_wrong_address_is_asked_about_once_per_session(
    store: TokenStore, caplog: pytest.LogCaptureFixture
) -> None:
    """Второй и дальнейшие разы — без запроса к шлюзу и без строки в журнале.

    Догрузка запускается при **каждом** подключении, а обрывы у владельца
    счёта частые: без пометки на сеанс он получил бы эту строку десятки раз
    за день, а шлюз — десятки заведомо мёртвых запросов.
    """
    recorder = Recorder(json_reply(200, good_auth()), text_reply(404))
    clock = Clock()
    seen: list[WrongAddress] = []

    async def scenario() -> BrokerSession:
        async with session(store, recorder, clock, attempts=1) as broker:
            for _ in range(5):
                with pytest.raises(WrongAddress) as caught:
                    await broker.read(CANDLES_PATH)
                seen.append(caught.value)
            return broker

    with caplog.at_level(logging.DEBUG, logger="broker"):
        broker = run(scenario())

    asked = [item for item in recorder.requests if item.url.path == CANDLES_PATH]
    assert len(asked) == 1, f"по неверному адресу шлюз опрошен {len(asked)} раз"
    assert [item.repeated for item in seen] == [False, True, True, True, True]
    assert broker.address_is_wrong(CANDLES_PATH) is True
    assert broker.address_is_wrong(PORTFOLIO_PATH) is False, "помечен не тот адрес"

    said = [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ]
    assert len(said) == 1, f"о неверном адресе сказано {len(said)} раз: {said}"


def test_two_requests_racing_into_the_same_wrong_address_say_it_once(
    store: TokenStore, caplog: pytest.LogCaptureFixture
) -> None:
    """Два одновременных запроса по одному неверному адресу — одна строка.

    Короткое замыкание в `read` второй запрос не остановит: он ушёл раньше,
    чем вернулся первый ответ, — и это единственный путь, которым повторный
    отказ доходит до записи в журнал. Путь не выдуманный: догрузка и запрос
    состояния счёта ходят через одну сессию и стартуют вместе при подключении.

    Ответ задерживается **до прихода обоих запросов** нарочно. Подставной
    транспорт отвечает мгновенно, поэтому без задержки `asyncio.gather`
    выполняет два чтения подряд, а не одновременно, — гонки не случается,
    и проверка становится украшением. Поймано прогоном мутаций 05.09.2026:
    без задержки снятие защиты в `_note_failure` не роняло ни одного теста.
    """
    clock = Clock()
    both_in_flight = asyncio.Event()
    arrived: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        arrived.append(request)
        if request.url.path != CANDLES_PATH:
            return httpx.Response(200, json=good_auth())
        if len([item for item in arrived if item.url.path == CANDLES_PATH]) >= 2:
            both_in_flight.set()
        await both_in_flight.wait()
        return httpx.Response(404, text=GATEWAY_404)

    async def scenario() -> list[object]:
        client = httpx.AsyncClient(
            base_url="https://example.invalid", transport=httpx.MockTransport(handler)
        )
        async with BrokerSession(
            store,
            client=client,
            clock=clock,
            sleep=no_sleep,
            throttle=Throttle(rate=1000.0),
            attempts=1,
        ) as broker:
            await broker.access_token()  # обмен отдельно: гонку ловим на чтении
            return list(
                await asyncio.gather(
                    broker.read(CANDLES_PATH),
                    broker.read(CANDLES_PATH),
                    return_exceptions=True,
                )
            )

    with caplog.at_level(logging.DEBUG, logger="broker"):
        outcome = run(scenario())

    assert len([item for item in arrived if item.url.path == CANDLES_PATH]) == 2, (
        "гонки не случилось: запросы ушли по очереди, проверять нечего"
    )
    assert all(isinstance(item, WrongAddress) for item in outcome), outcome
    repeats = [item.repeated for item in outcome if isinstance(item, WrongAddress)]
    assert sorted(repeats) == [False, True], (
        "оба отказа помечены одинаково: пометка «уже говорили» не сработала"
    )
    said = [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ]
    assert len(said) == 1, f"о неверном адресе сказано {len(said)} раз: {said}"


def test_a_broker_404_with_its_own_code_is_an_ordinary_refusal(store: TokenStore) -> None:
    """404 с кодом отказа брокера — обычный `NotFound`, а не «неверный адрес».

    ⚠️ **404 у метода свечей двузначен**: официальный PDF называет
    `404 NOT_FOUND` штатным ответом «данные не найдены» — например, за период
    без торгов. Спутать его с неверным адресом дорого: рабочий метод был бы
    погашен до перезапуска программы, и догрузка молча прекратилась бы.
    """
    recorder = Recorder(
        json_reply(200, good_auth()),
        json_reply(404, {"errorCode": "NOT_FOUND", "message": "нет данных"}),
    )
    clock = Clock()

    async def scenario() -> tuple[BrokerError, BrokerSession]:
        async with session(store, recorder, clock, attempts=1) as broker:
            with pytest.raises(NotFound) as caught:
                await broker.read(CANDLES_PATH)
            with pytest.raises(NotFound):
                await broker.read(CANDLES_PATH)
            return caught.value, broker

    error, broker = run(scenario())
    assert not isinstance(error, WrongAddress)
    assert broker.address_is_wrong(CANDLES_PATH) is False
    asked = [item for item in recorder.requests if item.url.path == CANDLES_PATH]
    assert len(asked) == 2, "штатный отказ погасил рабочий метод на весь сеанс"
    assert "NOT_FOUND" in error.technical, "код отказа брокера потерян для разбора"


def test_the_candles_address_names_the_market_data_service() -> None:
    """Свечи живут на том же сервисе, что и поток котировок, — и это проверка.

    ⚠️ Стоило ровно одного живого запуска: в адресе стояло имя сервиса
    справочника инструментов (`trade-api-information-service`), шлюз брокера
    ответил 404 страницей своего nginx, и владелец счёта получил её на экран.
    Верное имя — `trade-api-market-data-connector`, из официального PDF; тот же
    сервис несёт вебсокет котировок, и свечи — тоже рыночные данные.

    ⛔ Живьём верный адрес не проверялся: токена в разработке нет.
    """
    from broker.stream import MARKET_DATA_WS

    service = "/trade-api-market-data-connector/"
    assert CANDLES_PATH.startswith(service), CANDLES_PATH
    assert service in MARKET_DATA_WS, (
        "адрес свечей и адрес потока разъехались по сервисам: одно из двух "
        "имён неверно, и узнается это снова на живом запуске"
    )
    assert CANDLES_PATH.endswith("/api/v1/candles-chart"), CANDLES_PATH
    assert INSTRUMENTS_BY_TICKERS_PATH.startswith("/trade-api-information-service/"), (
        "справочник инструментов переехал: этот адрес проверен живьём "
        "(403 без токена — маршрут есть), менять его без замера нельзя"
    )


def test_the_address_sensitive_table_names_only_paths_we_may_call() -> None:
    """Таблица двузначных адресов и белый список адресов не разъехались.

    ⚠️ Адрес свечей из белого списка **не убран намеренно**: метод не пропал,
    у нас было неверно записано имя сервиса. Разбор и источник верного адреса —
    `.docs/broker-api/07-historical-candles.md`.
    """
    assert set(ADDRESS_SENSITIVE) <= ALLOWED_READ_PATHS, (
        "в таблице адрес, к которому слой обращаться не вправе: 404 по нему "
        "никогда не придёт, и запись мертва"
    )
    assert CANDLES_PATH in ALLOWED_READ_PATHS
    assert CANDLES_PATH in ADDRESS_SENSITIVE
    # Обход по всей таблице, а не по одной записи. Прежде здесь стоял один
    # `CANDLES_PATH`, и добавленные 05.09.2026 два адреса расписания прошли бы
    # мимо проверки текстов молча — а тексты эти читает владелец счёта.
    for path, texts in ADDRESS_SENSITIVE.items():
        assert len(texts) == 2, f"у адреса {path} не две строки: {texts}"
        for line in texts:
            assert line.strip(), f"пустая строка для человека у адреса {path}"
            for piece in ("404", "HTTP", "candles", "nginx", "/api/", "trade-api"):
                assert piece not in line, f"техника в тексте для человека: {line}"


# --- сверка часов по заголовку `Date` (05.09.2026, `D-045`) ---


def dated(moment: datetime) -> Callable[[httpx.Request], httpx.Response]:
    """Ответ 200 с заголовком `Date`, как его шлёт сервер."""
    stamp = moment.astimezone(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    return lambda request: httpx.Response(200, json={"ok": True}, headers={"Date": stamp})


def test_every_answer_measures_the_clock_without_a_single_extra_request(
    store: TokenStore,
) -> None:
    """Часы сверяются заголовком `Date` обычного ответа: лишних запросов нет.

    Отдельного метода «который час» в API БКС нет, а `Date` приходит с каждым
    ответом. Значит замер стоит ноль запросов и ноль жетонов ограничителя.
    """
    clock = Clock()
    recorder = Recorder(
        json_reply(200, good_auth()), dated(START + timedelta(seconds=45))
    )

    async def scenario() -> BrokerSession:
        broker = session(store, recorder, clock)
        await broker.read(LIMITS_PATH)
        await broker.close()
        return broker

    broker = run(scenario())
    assert len(recorder.requests) == 2, (
        "запросов больше, чем обмен токена плюс чтение: сверка часов стоит "
        "лишний запрос, а обещано, что она бесплатна"
    )
    assert recorder.requests[1].url.path == LIMITS_PATH
    # Замер лежит в сессии, наружу не возвращается: говорить о нём — дело `app/`.
    reading = broker.clock.last
    assert reading is not None, "заголовок `Date` пришёл, а замера нет"
    assert reading.skew == timedelta(seconds=45)


def test_the_session_sees_a_machine_clock_that_lags(store: TokenStore) -> None:
    """Отставшие часы машины замечены и названы числом со стороной."""
    clock = Clock()
    recorder = Recorder(
        json_reply(200, good_auth()), dated(START + timedelta(seconds=90))
    )

    async def scenario() -> BrokerSession:
        broker = session(store, recorder, clock)
        await broker.read(LIMITS_PATH)
        await broker.close()
        return broker

    broker = run(scenario())
    assert broker.clock.verdict() is ClockVerdict.BROKEN
    told = broker.clock.complaint()
    assert told is not None
    assert "отстают" in told[0], told[0]
    assert "1 мин 30 с" in told[1], told[1]


def test_an_answer_without_a_date_leaves_the_clock_unchecked(store: TokenStore) -> None:
    """Ответ без `Date` — «не сверяли», а не «часы верны»."""
    clock = Clock()
    recorder = Recorder(json_reply(200, good_auth()), json_reply(200, {"ok": True}))

    async def scenario() -> BrokerSession:
        broker = session(store, recorder, clock)
        await broker.read(LIMITS_PATH)
        await broker.close()
        return broker

    broker = run(scenario())
    assert broker.clock.verdict() is ClockVerdict.UNKNOWN
    assert broker.clock.complaint() is None


def test_the_clock_reading_never_carries_a_request_header(store: TokenStore) -> None:
    """В замер попадает время ответа и ничего больше: заголовки запроса — секрет.

    Замер стоит **после** `finally`, стирающего `Authorization` из кадра.
    Тест сторожит именно это: замер, переехавший внутрь `try`, продлил бы
    жизнь строки `Bearer <рабочий токен>` в трассировке.
    """
    # Приватный метод здесь и есть предмет проверки: порядок «стереть
    # заголовок, потом мерить» снаружи не виден никак.
    source = inspect.getsource(BrokerSession._authorized)  # noqa: SLF001
    clearing = source.index("headers.clear()")
    measuring = source.index("self.clock.observed")
    assert clearing < measuring, (
        "сверка часов делается до стирания заголовка: секрет живёт в кадре дольше"
    )
