"""Расписание торгов: разбор ответа, частота запросов, поведение при отказе.

В сеть тесты не ходят: транспорт `httpx` подменён. Настоящий токен
не участвует — значение фикстуры помечено `FAKE-…-NOT-A-REAL-TOKEN`.

⛔ Живого запроса к брокеру не было ни одного. Ответы в этом файле собраны
по официальному PDF и по выпискам `.docs/broker-api/05-daily-schedule.md`
и `06-trading-status.md`, то есть проверяют наш разбор документации,
а не поведение брокера.
"""

from __future__ import annotations

import asyncio
import io
import logging
import pathlib
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Coroutine, Iterator, TypeVar

import httpx
import pytest

from broker.errors import BrokerError, UnexpectedAnswer, WrongAddress
from broker.redaction import LOGGER_NAME
from broker.schedule import (
    SCHEDULE_MAX_AGE,
    SESSION_KINDS,
    STATUS_MAX_AGE,
    STATUS_MIN_AGE,
    UNKNOWN_MAX_AGE,
    DailySchedule,
    Schedule,
    SessionKind,
    TradingStatus,
    kind_of,
    lifetime_of,
    parse_daily,
    parse_status,
)
from broker.secret import Secret
from broker.session import (
    ADDRESS_SENSITIVE,
    ALLOWED_READ_PATHS,
    AUTH_URL,
    DAILY_SCHEDULE_PATH,
    TRADING_STATUS_PATH,
    BrokerSession,
)
from broker.throttle import Throttle
from broker.token_store import TokenStore
from broker.tokens import ACCESS_LIFETIME, TokenScope

FAKE_REFRESH = "FAKE-schedule-refresh-0000-NOT-A-REAL-TOKEN"
FAKE_ACCESS = "FAKE-schedule-access-1111-NOT-A-REAL-TOKEN"

START = datetime(2026, 9, 5, 6, 0, tzinfo=timezone.utc)

CLASS = "SPBFUT"
TICKER = "MXU6"

#: Что вернул сценарий теста. `run` не теряет тип, иначе проверка
#: `is None` уходила бы в `Any` и молчала бы на любой ошибке типов.
_Result = TypeVar("_Result")

#: Ответ статуса торгов в том виде, как его описывает документация сайта.
STATUS_ANSWER: dict[str, Any] = {
    "tradingSessionTypeId": 3,
    "tradingSessionType": "Торговый период",
    "tradingSessionStatus": "OPEN",
    "nextSessionDate": "2026-09-05T11:00:00.000Z",
}

#: Ответ дневного расписания. Отрезки перечислены теми же шестью типами,
#: которые называет документация, — включая технический перерыв.
DAILY_ANSWER: dict[str, Any] = {
    "isWorkDay": True,
    "dailySchedule": [
        {
            "startDate": "09:50:00",
            "endDate": "10:00:00",
            "tradingSessionType": "Аукцион открытия",
            "tradingSessionStatus": "OPEN",
        },
        {
            "startDate": "10:00:00",
            "endDate": "14:00:00",
            "tradingSessionType": "Торговый период",
            "tradingSessionStatus": "OPEN",
        },
        {
            "startDate": "14:00:00",
            "endDate": "14:05:00",
            "tradingSessionType": "Технический перерыв QUIK",
            "tradingSessionStatus": "CLOSE",
        },
        {
            "startDate": "18:45:00",
            "endDate": "19:00:00",
            "tradingSessionType": "Аукцион закрытия",
            "tradingSessionStatus": "CLOSE",
        },
        {
            "startDate": "19:00:00",
            "endDate": "23:50:00",
            "tradingSessionType": "Вечерняя торговая сессия",
            "tradingSessionStatus": "OPEN",
        },
        {
            "startDate": "23:50:00",
            "endDate": "09:50:00",
            "tradingSessionType": "Не рабочее время/Выходной/Праздник",
            "tradingSessionStatus": "CLOSE",
        },
    ],
}


class Clock:
    """Часы, которыми управляет тест."""

    def __init__(self, start: datetime = START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, shift: timedelta) -> None:
        self.now += shift


async def no_sleep(seconds: float) -> None:
    return None


def good_auth() -> dict[str, Any]:
    return {
        "expires_in": int(ACCESS_LIFETIME.total_seconds()),
        "token_type": "bearer",
        "access_token": FAKE_ACCESS,
    }


@pytest.fixture
def store(tmp_path: pathlib.Path) -> TokenStore:
    directory = tmp_path / "userdata"
    directory.mkdir()
    keeper = TokenStore(directory)
    keeper.save(Secret(FAKE_REFRESH), TokenScope.READ_ONLY, issued_at=START - timedelta(days=1))
    return keeper


@pytest.fixture
def journal() -> Iterator[io.StringIO]:
    """Записи слоя брокера — в буфер теста, а не в консоль."""
    buffer = io.StringIO()
    sink = logging.StreamHandler(buffer)
    sink.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logger = logging.getLogger(LOGGER_NAME)
    logger.addHandler(sink)
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        yield buffer
    finally:
        logger.removeHandler(sink)
        logger.setLevel(previous)


class Broker:
    """Подставной брокер: авторизация даром, ответы — по адресу.

    Ответы задаются списком на адрес: первый запрос получает первый ответ,
    последний повторяется, пока спрашивают. Так пишутся сценарии «сначала
    отказ, потом ответ» без счётчиков в самом тесте.

    ⚠️ Обработчик **асинхронный**, и это не украшение. `MockTransport`
    вызывает синхронный обработчик, ни разу не уступив управление циклу,
    поэтому десять одновременных вопросов исполнялись бы по очереди,
    и проверка замка была бы зелёной при снятом замке.
    """

    def __init__(self) -> None:
        self.asked: list[httpx.Request] = []
        self._replies: dict[str, list[Callable[[httpx.Request], httpx.Response]]] = {}

    def answer(self, path: str, *replies: Callable[[httpx.Request], httpx.Response]) -> None:
        self._replies[path] = list(replies)

    def count(self, path: str) -> int:
        return len([item for item in self.asked if item.url.path == path])

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        """Ответ на запрос. Асинхронный намеренно — см. докстринг класса."""
        if str(request.url) == AUTH_URL:
            return httpx.Response(200, json=good_auth())
        self.asked.append(request)
        # Уступаем управление до того, как ответить. Синхронный обработчик
        # `MockTransport` до конца отрабатывает первый же запрос, ни разу
        # не отдав управление циклу, — и десять «одновременных» вопросов
        # исполняются по очереди. Проверка замка при этом зеленела бы
        # и при снятом замке (проверено мутацией 05.09.2026).
        await asyncio.sleep(0)
        prepared = self._replies.get(request.url.path)
        if not prepared:
            raise AssertionError(f"тест не задал ответа на {request.url.path}")
        index = min(self.count(request.url.path) - 1, len(prepared) - 1)
        return prepared[index](request)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url="https://example.invalid", transport=httpx.MockTransport(self)
        )


def json_reply(status: int, body: object) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(status, json=body)


def text_reply(status: int, body: str) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(status, text=body)


def session(store: TokenStore, broker: Broker, clock: Clock) -> BrokerSession:
    """Подключение к подставному брокеру. Повторов нет: отказ виден сразу."""
    return BrokerSession(
        store,
        client=broker.client(),
        clock=clock,
        sleep=no_sleep,
        throttle=Throttle(rate=1000.0),
        attempts=1,
    )


def run(work: Coroutine[Any, Any, _Result]) -> _Result:
    return asyncio.run(work)


def ask_status(store: TokenStore, broker: Broker, clock: Clock) -> TradingStatus | None:
    async def scenario() -> TradingStatus | None:
        async with session(store, broker, clock) as connection:
            return await Schedule(connection, clock=clock).status(class_code=CLASS)

    return run(scenario())


# --- разбор ответа: статус торгов ---


def test_status_reads_every_field_the_documentation_names() -> None:
    """Все четыре поля ответа доезжают до `TradingStatus`."""
    status = parse_status(STATUS_ANSWER, class_code=CLASS)
    assert status.class_code == CLASS
    assert status.session_type == "Торговый период"
    assert status.session_type_id == 3
    assert status.kind is SessionKind.MAIN
    assert status.is_open is True
    assert status.next_change == datetime(2026, 9, 5, 11, 0, tzinfo=timezone.utc)


def test_status_id_survives_the_casing_of_the_pdf() -> None:
    """`tradingSessionTypeID` из PDF читается наравне с `…Id` с сайта."""
    answer = {"tradingSessionTypeID": 7, "tradingSessionStatus": "CLOSE"}
    assert parse_status(answer, class_code=CLASS).session_type_id == 7


@pytest.mark.parametrize(
    ("value", "expected"),
    [("OPEN", True), ("open", True), ("CLOSE", False), (True, True), (False, False)],
)
def test_status_understands_both_spellings_of_openness(value: object, expected: bool) -> None:
    """`OPEN`/`CLOSE` и логическое значение — одно и то же да/нет.

    Логическое принимается потому, что PDF подписывает значения как
    «OPEN (true)» и «CLOSE (false)».
    """
    answer = {"tradingSessionStatus": value}
    assert parse_status(answer, class_code=CLASS).is_open is expected


@pytest.mark.parametrize("value", ["ПАУЗА", "", "unknown", 3, None])
def test_an_unknown_status_word_is_not_taken_for_closed(value: object) -> None:
    """Непонятое слово даёт «не знаем», а не «биржа закрыта».

    Разница дорогая: `False` остановил бы торговлю на весь день из-за
    опечатки в чужом сервисе, `None` означает «ведём себя как раньше».
    """
    answer = {"tradingSessionStatus": value}
    assert parse_status(answer, class_code=CLASS).is_open is None


def test_a_moment_without_a_zone_is_dropped_not_guessed() -> None:
    """`nextSessionDate` без пояса отбрасывается: догадка о зоне стоит трёх часов.

    Пример на сайте кончается на `Z`, пример в PDF — нет. Наивный момент,
    истолкованный как местный, сдвинул бы срок на три часа молча.
    """
    naive = dict(STATUS_ANSWER, nextSessionDate="2021-01-19T09:41:03")
    assert parse_status(naive, class_code=CLASS).next_change is None
    aware = dict(STATUS_ANSWER, nextSessionDate="2021-01-19T09:41:03Z")
    assert parse_status(aware, class_code=CLASS).next_change is not None


def test_an_empty_status_answer_is_unknown_but_not_a_failure() -> None:
    """Объект без полей — это «брокер ничего не сказал», а не смена формата."""
    status = parse_status({}, class_code=CLASS)
    assert (status.is_open, status.kind, status.next_change) == (
        None,
        SessionKind.UNKNOWN,
        None,
    )


@pytest.mark.parametrize("payload", [[], "OPEN", None, 5])
def test_a_status_answer_that_is_not_an_object_is_refused(payload: object) -> None:
    """Ответ не объектом — смена формата, и она называется вслух."""
    with pytest.raises(UnexpectedAnswer):
        parse_status(payload, class_code=CLASS)


# --- разбор ответа: расписание на день ---


@pytest.mark.parametrize("name", ["isWorkDay", "isWorkingDay"])
def test_both_spellings_of_the_work_day_flag_are_read(name: str) -> None:
    """Сайт зовёт поле `isWorkDay`, PDF — `isWorkingDay`; читаются оба.

    Какое приходит на самом деле, живым запросом не проверено. Прочитать
    одно из двух значило бы с вероятностью в половину не узнать про выходной.
    """
    answer = {name: True}
    assert parse_daily(answer, class_code=CLASS, ticker=TICKER).is_work_day is True


@pytest.mark.parametrize(("value", "expected"), [(False, False), ("TRUE", True), ("false", False)])
def test_the_work_day_flag_reads_a_string_too(value: object, expected: bool) -> None:
    """PDF подписывает признак как «Логический (TRUE/FALSE)» — строка тоже да/нет."""
    answer = {"isWorkDay": value}
    assert parse_daily(answer, class_code=CLASS, ticker=TICKER).is_work_day is expected


@pytest.mark.parametrize("answer", [{}, {"isWorkDay": "может быть"}, {"isWorkDay": 1}])
def test_a_missing_work_day_flag_is_unknown_not_a_holiday(answer: object) -> None:
    """Поля нет или оно не логическое — «не знаем», а не «биржа не работает»."""
    assert parse_daily(answer, class_code=CLASS, ticker=TICKER).is_work_day is None


def test_the_daily_answer_gives_the_intraday_break() -> None:
    """Перерыв внутри дня в ответе есть — и он отделён от торговых отрезков.

    Это ответ на вопрос №17: программа принципиально способна отличить
    «свечей нет по расписанию» от «свечей нет, потому что мы их потеряли».
    """
    schedule = parse_daily(DAILY_ANSWER, class_code=CLASS, ticker=TICKER)
    assert len(schedule.sessions) == 6
    assert len(schedule.breaks) == 1
    pause = schedule.breaks[0]
    assert pause.session_type == "Технический перерыв QUIK"
    assert (pause.starts_at, pause.ends_at) == ("14:00:00", "14:05:00")
    assert pause.is_open is False


def test_session_times_stay_text_and_are_never_turned_into_a_moment() -> None:
    """Времена отрезков остаются строками: часовой пояс у них не установлен.

    Превращение `14:00:00` в момент на оси — угадывание зоны, и ошибка
    сдвинула бы перерыв на три часа, ничего при этом не уронив.
    """
    schedule = parse_daily(DAILY_ANSWER, class_code=CLASS, ticker=TICKER)
    for slot in schedule.sessions:
        assert isinstance(slot.starts_at, str), slot
        assert isinstance(slot.ends_at, str), slot


def test_a_day_without_a_schedule_list_is_not_a_failure() -> None:
    """Выходной приходит без отрезков — это законный ответ, а не смена формата.

    В отличие от `bars` у свечей: там список — единственное содержимое
    ответа, здесь рядом живёт признак рабочего дня.
    """
    schedule = parse_daily({"isWorkDay": False}, class_code=CLASS, ticker=TICKER)
    assert schedule.sessions == ()
    assert schedule.is_work_day is False


def test_a_stranger_among_the_slots_is_skipped_not_crashed() -> None:
    """Не объект в списке отрезков пропускается: соседние отрезки не теряются."""
    answer = {"dailySchedule": [DAILY_ANSWER["dailySchedule"][2], "мусор", 7]}
    schedule = parse_daily(answer, class_code=CLASS, ticker=TICKER)
    assert len(schedule.sessions) == 1
    assert schedule.sessions[0].kind is SessionKind.BREAK


@pytest.mark.parametrize("payload", [[], "OPEN", None])
def test_a_daily_answer_that_is_not_an_object_is_refused(payload: object) -> None:
    """Ответ не объектом — смена формата, и она называется вслух."""
    with pytest.raises(UnexpectedAnswer):
        parse_daily(payload, class_code=CLASS, ticker=TICKER)


# --- таблица типов сессии ---


def test_the_table_covers_every_type_the_documentation_lists() -> None:
    """Все шесть названных документацией типов различены, и ни один не потерян.

    Таблица, а не цепочка `if`: проверяется её полнота, а не отдельные ветки.
    Опечатка в ключе иначе прошла бы молча — тип стал бы `UNKNOWN`.
    """
    documented = {
        "Технический перерыв QUIK": SessionKind.BREAK,
        "Аукцион открытия": SessionKind.OPENING_AUCTION,
        "Не рабочее время/Выходной/Праздник": SessionKind.CLOSED,
        "Вечерняя торговая сессия": SessionKind.EVENING,
        "Торговый период": SessionKind.MAIN,
        "Аукцион закрытия": SessionKind.CLOSING_AUCTION,
    }
    assert {kind_of(name) for name in documented} == set(documented.values())
    for name, kind in documented.items():
        assert kind_of(name) is kind, name
    assert len(SESSION_KINDS) == len(documented)


@pytest.mark.parametrize(
    "written",
    ["  Торговый   период ", "ТОРГОВЫЙ ПЕРИОД", "торговый период"],
)
def test_the_type_is_matched_past_case_and_spaces(written: str) -> None:
    """Регистр и лишние пробелы в названии сессии ничего не решают."""
    assert kind_of(written) is SessionKind.MAIN


@pytest.mark.parametrize("written", ["Клиринг", "Перерыв", "", None])
def test_an_unknown_type_is_unknown_and_not_a_break(written: str | None) -> None:
    """Незнакомый тип не подгоняется под перерыв по куску строки.

    Совпадение точное. «Перерыв» без остального названия — не тот тип,
    про который мы что-то знаем, и врать про торговое время нельзя.
    """
    assert kind_of(written) is SessionKind.UNKNOWN


def test_an_unknown_type_keeps_the_original_words() -> None:
    """Незнакомое название сохраняется целиком: разбирать его будет человек."""
    answer = {"dailySchedule": [{"tradingSessionType": "Клиринг вечерний"}]}
    slot = parse_daily(answer, class_code=CLASS, ticker=TICKER).sessions[0]
    assert slot.kind is SessionKind.UNKNOWN
    assert slot.session_type == "Клиринг вечерний"


# --- адреса ---


def test_both_addresses_are_in_the_white_list_of_reading() -> None:
    """Оба адреса расписания — читающие и названы в белом списке поимённо."""
    assert DAILY_SCHEDULE_PATH in ALLOWED_READ_PATHS
    assert TRADING_STATUS_PATH in ALLOWED_READ_PATHS


def test_both_addresses_name_the_information_service() -> None:
    """Имя сервиса у расписания — то, что назвал PDF, и это выбор, а не копия.

    ⚠️ Наша выписка `05-daily-schedule.md` называет `trade-api-bff-portfolio`.
    Взят PDF: соседний метод того же контроллера (`status`) стоит
    на `trade-api-information-service` в обоих источниках, и в самом PDF
    `trade-api-bff-portfolio` встречается один раз — у портфеля.
    Ошибка в имени сервиса уже стоила дня 05.09.2026 на свечах.

    ⛔ Живым запросом не проверено ни одно из двух имён.
    """
    service = "/trade-api-information-service/"
    assert DAILY_SCHEDULE_PATH.startswith(service), DAILY_SCHEDULE_PATH
    assert TRADING_STATUS_PATH.startswith(service), TRADING_STATUS_PATH
    assert DAILY_SCHEDULE_PATH.endswith("/api/v1/trading-schedule/daily-schedule")
    assert TRADING_STATUS_PATH.endswith("/api/v1/trading-schedule/status")


def test_both_addresses_are_marked_two_faced_for_404() -> None:
    """404 по расписанию двузначен, и слой это знает заранее, а не после запуска.

    Имя сервиса выбрано из двух документов и живьём не проверено. Если оно
    не то, шлюз ответит своей страницей; запись в таблице делает так, что
    владелец счёта услышит об этом один раз, а не на каждом переподключении.
    """
    assert DAILY_SCHEDULE_PATH in ADDRESS_SENSITIVE
    assert TRADING_STATUS_PATH in ADDRESS_SENSITIVE


# --- запрос ---


def test_the_status_request_carries_the_documented_parameter(store: TokenStore) -> None:
    """Запрос статуса — GET по документированному адресу с одним `classCode`."""
    broker = Broker()
    broker.answer(TRADING_STATUS_PATH, json_reply(200, STATUS_ANSWER))
    clock = Clock()

    status = ask_status(store, broker, clock)

    assert status is not None and status.is_open is True
    assert broker.count(TRADING_STATUS_PATH) == 1
    request = broker.asked[0]
    assert request.method == "GET"
    assert request.url.params["classCode"] == CLASS


def test_the_daily_request_carries_class_and_ticker(store: TokenStore) -> None:
    """Запрос расписания — GET с `classCode` и `ticker`, как требует документация."""
    broker = Broker()
    broker.answer(DAILY_SCHEDULE_PATH, json_reply(200, DAILY_ANSWER))
    clock = Clock()

    async def scenario() -> DailySchedule | None:
        async with session(store, broker, clock) as connection:
            return await Schedule(connection, clock=clock).daily(
                class_code=CLASS, ticker=TICKER
            )

    schedule = run(scenario())
    assert schedule is not None and schedule.is_work_day is True
    request = broker.asked[0]
    assert request.method == "GET"
    assert request.url.params["classCode"] == CLASS
    assert request.url.params["ticker"] == TICKER


@pytest.mark.parametrize("empty", ["", "   "])
def test_an_empty_class_code_never_reaches_the_broker(store: TokenStore, empty: str) -> None:
    """Пустой параметр — ошибка вызывающего, и она ловится до сети.

    Иначе он доехал бы до брокера, вернулся отказом уже в торговое время
    и потратил запрос из общей квоты слоя.
    """
    broker = Broker()
    clock = Clock()

    async def scenario() -> None:
        async with session(store, broker, clock) as connection:
            hours = Schedule(connection, clock=clock)
            with pytest.raises(ValueError):
                await hours.status(class_code=empty)
            with pytest.raises(ValueError):
                await hours.daily(class_code=CLASS, ticker=empty)

    run(scenario())
    assert broker.asked == [], "запрос с пустым параметром всё-таки ушёл"


# --- частота запросов ---


def test_the_same_question_twice_costs_one_request(store: TokenStore) -> None:
    """Второй вопрос в пределах срока годности запроса не делает.

    Ограничитель частоты у слоя общий, и догрузка истории уже умеет занять
    очередь надолго: спрашивать расписание перед каждым действием нельзя.
    """
    broker = Broker()
    broker.answer(DAILY_SCHEDULE_PATH, json_reply(200, DAILY_ANSWER))
    clock = Clock()

    async def scenario() -> None:
        async with session(store, broker, clock) as connection:
            hours = Schedule(connection, clock=clock)
            first = await hours.daily(class_code=CLASS, ticker=TICKER)
            clock.advance(SCHEDULE_MAX_AGE - timedelta(seconds=1))
            second = await hours.daily(class_code=CLASS, ticker=TICKER)
            assert first == second

    run(scenario())
    assert broker.count(DAILY_SCHEDULE_PATH) == 1


def test_the_day_schedule_is_asked_again_when_it_goes_stale(store: TokenStore) -> None:
    """Расписание не живёт сутки: смену рабочего дня программа замечает сама."""
    broker = Broker()
    broker.answer(
        DAILY_SCHEDULE_PATH,
        json_reply(200, DAILY_ANSWER),
        json_reply(200, {"isWorkDay": False}),
    )
    clock = Clock()

    async def scenario() -> DailySchedule | None:
        async with session(store, broker, clock) as connection:
            hours = Schedule(connection, clock=clock)
            await hours.daily(class_code=CLASS, ticker=TICKER)
            clock.advance(SCHEDULE_MAX_AGE + timedelta(seconds=1))
            return await hours.daily(class_code=CLASS, ticker=TICKER)

    schedule = run(scenario())
    assert broker.count(DAILY_SCHEDULE_PATH) == 2
    assert schedule is not None and schedule.is_work_day is False


def test_next_session_date_shortens_the_life_of_the_answer(store: TokenStore) -> None:
    """Смена состояния через минуту — и ответ живёт минуту, а не потолок.

    Ради этого метод статуса и дешевле полного расписания: он говорит,
    до какого момента спрашивать незачем.
    """
    broker = Broker()
    soon = (START + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    broker.answer(TRADING_STATUS_PATH, json_reply(200, dict(STATUS_ANSWER, nextSessionDate=soon)))
    clock = Clock()

    async def scenario() -> None:
        async with session(store, broker, clock) as connection:
            hours = Schedule(connection, clock=clock)
            await hours.status(class_code=CLASS)
            clock.advance(timedelta(seconds=50))
            await hours.status(class_code=CLASS)
            assert broker.count(TRADING_STATUS_PATH) == 1, "спросили раньше времени"
            clock.advance(timedelta(seconds=11))
            await hours.status(class_code=CLASS)

    run(scenario())
    assert broker.count(TRADING_STATUS_PATH) == 2


def test_a_far_next_session_date_does_not_outlive_the_ceiling(store: TokenStore) -> None:
    """Смена состояния через восемь часов не даёт спать восемь часов.

    Потолок — защита от неверного `nextSessionDate`: момент, ошибочно
    отнесённый вперёд, стоил бы пропущенного открытия торгов.
    """
    broker = Broker()
    far = (START + timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
    broker.answer(TRADING_STATUS_PATH, json_reply(200, dict(STATUS_ANSWER, nextSessionDate=far)))
    clock = Clock()

    async def scenario() -> None:
        async with session(store, broker, clock) as connection:
            hours = Schedule(connection, clock=clock)
            await hours.status(class_code=CLASS)
            clock.advance(STATUS_MAX_AGE + timedelta(seconds=1))
            await hours.status(class_code=CLASS)

    run(scenario())
    assert broker.count(TRADING_STATUS_PATH) == 2


def test_a_next_session_date_in_the_past_does_not_cost_a_request_per_call(
    store: TokenStore,
) -> None:
    """Момент в прошлом не превращает кэш в запрос на каждый вызов."""
    broker = Broker()
    past = (START - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    broker.answer(TRADING_STATUS_PATH, json_reply(200, dict(STATUS_ANSWER, nextSessionDate=past)))
    clock = Clock()

    async def scenario() -> None:
        async with session(store, broker, clock) as connection:
            hours = Schedule(connection, clock=clock)
            for _ in range(5):
                await hours.status(class_code=CLASS)
                clock.advance(timedelta(seconds=2))

    run(scenario())
    assert broker.count(TRADING_STATUS_PATH) == 1


@pytest.mark.parametrize(
    ("next_change", "expected"),
    [
        (None, STATUS_MAX_AGE),
        (START - timedelta(hours=1), STATUS_MIN_AGE),
        (START + timedelta(seconds=5), STATUS_MIN_AGE),
        (START + timedelta(minutes=1), timedelta(minutes=1)),
        (START + timedelta(hours=8), STATUS_MAX_AGE),
    ],
)
def test_the_lifetime_table_holds_both_edges(
    next_change: datetime | None, expected: timedelta
) -> None:
    """Срок жизни статуса: пол, потолок и всё, что между ними."""
    status = TradingStatus(
        class_code=CLASS,
        session_type=None,
        session_type_id=None,
        kind=SessionKind.UNKNOWN,
        is_open=True,
        next_change=next_change,
    )
    assert lifetime_of(status, START) == expected


def test_the_lifetime_of_not_knowing_is_the_shortest() -> None:
    """«Не знаем» живёт меньше любого ответа: незнание ничего не блокирует."""
    assert lifetime_of(None, START) == UNKNOWN_MAX_AGE
    assert UNKNOWN_MAX_AGE < SCHEDULE_MAX_AGE
    assert UNKNOWN_MAX_AGE <= STATUS_MAX_AGE


def test_ten_simultaneous_questions_cost_one_request(store: TokenStore) -> None:
    """Десять одновременных вопросов — один запрос, а не десять.

    Без замка первый же старт программы упёрся бы в ограничитель частоты
    на ровном месте: несколько частей `app/` спрашивают расписание разом.
    """
    broker = Broker()
    broker.answer(TRADING_STATUS_PATH, json_reply(200, STATUS_ANSWER))
    clock = Clock()

    async def scenario() -> list[TradingStatus | None]:
        async with session(store, broker, clock) as connection:
            hours = Schedule(connection, clock=clock)
            return list(
                await asyncio.gather(*(hours.status(class_code=CLASS) for _ in range(10)))
            )

    answers = run(scenario())
    assert broker.count(TRADING_STATUS_PATH) == 1
    assert all(item is not None and item.is_open is True for item in answers)


def test_two_instruments_do_not_share_one_answer(store: TokenStore) -> None:
    """Расписание помнится по инструменту: второй тикер получает свой ответ."""
    broker = Broker()
    broker.answer(
        DAILY_SCHEDULE_PATH,
        json_reply(200, DAILY_ANSWER),
        json_reply(200, {"isWorkDay": False}),
    )
    clock = Clock()

    async def scenario() -> tuple[DailySchedule | None, DailySchedule | None]:
        async with session(store, broker, clock) as connection:
            hours = Schedule(connection, clock=clock)
            first = await hours.daily(class_code=CLASS, ticker=TICKER)
            second = await hours.daily(class_code=CLASS, ticker="RIU6")
            return first, second

    first, second = run(scenario())
    assert broker.count(DAILY_SCHEDULE_PATH) == 2
    assert first is not None and first.is_work_day is True
    assert second is not None and second.is_work_day is False


def test_forgetting_makes_the_next_question_reach_the_broker(store: TokenStore) -> None:
    """`forget()` действительно чистит память, а не только выглядит так."""
    broker = Broker()
    broker.answer(DAILY_SCHEDULE_PATH, json_reply(200, DAILY_ANSWER))
    clock = Clock()

    async def scenario() -> None:
        async with session(store, broker, clock) as connection:
            hours = Schedule(connection, clock=clock)
            await hours.daily(class_code=CLASS, ticker=TICKER)
            hours.forget()
            await hours.daily(class_code=CLASS, ticker=TICKER)

    run(scenario())
    assert broker.count(DAILY_SCHEDULE_PATH) == 2


def test_the_policy_numbers_are_the_ones_we_chose() -> None:
    """Сроки жизни — те, что выбраны и объяснены, а не любые.

    Тесты частоты запросов написаны через эти же имена и вместе с ними
    едут. Числа названы здесь, чтобы правка политики была видна: срок жизни
    расписания — это то, чем оплачивается общий ограничитель частоты слоя.
    """
    assert STATUS_MAX_AGE == timedelta(minutes=5)
    assert STATUS_MIN_AGE == timedelta(seconds=15)
    assert SCHEDULE_MAX_AGE == timedelta(minutes=30)
    assert UNKNOWN_MAX_AGE == timedelta(seconds=30)
    assert STATUS_MIN_AGE < STATUS_MAX_AGE < SCHEDULE_MAX_AGE


def test_status_and_the_day_schedule_do_not_share_one_memory(store: TokenStore) -> None:
    """Два вопроса помнятся порознь: ответ на один не выдаётся за другой.

    Общий ключ памяти вернул бы на вопрос про расписание сохранённый статус
    торгов — то есть не тот объект, и вызывающий получил бы его молча.
    """
    broker = Broker()
    broker.answer(TRADING_STATUS_PATH, json_reply(200, STATUS_ANSWER))
    broker.answer(DAILY_SCHEDULE_PATH, json_reply(200, DAILY_ANSWER))
    clock = Clock()

    async def scenario() -> tuple[object, object]:
        async with session(store, broker, clock) as connection:
            hours = Schedule(connection, clock=clock)
            return (
                await hours.status(class_code=CLASS),
                await hours.daily(class_code=CLASS, ticker=TICKER),
            )

    status, schedule = run(scenario())
    assert isinstance(status, TradingStatus), status
    assert isinstance(schedule, DailySchedule), schedule
    assert broker.count(TRADING_STATUS_PATH) == 1
    assert broker.count(DAILY_SCHEDULE_PATH) == 1


def test_a_defect_in_our_own_code_is_not_swallowed_as_not_knowing(store: TokenStore) -> None:
    """Постороннее исключение проходит насквозь: «не знаем» — только про брокера.

    `except Exception` вместо `except BrokerError` превратил бы дефект
    нашего же кода в тихое «расписание неизвестно», и искать его было бы
    негде: программа продолжала бы работать как раньше.
    """
    broker = Broker()

    def defect(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("дефект нашего кода")

    broker.answer(TRADING_STATUS_PATH, defect)
    clock = Clock()

    async def scenario() -> None:
        async with session(store, broker, clock) as connection:
            await Schedule(connection, clock=clock).status(class_code=CLASS)

    with pytest.raises(RuntimeError):
        run(scenario())


# --- отказ брокера ---


@pytest.mark.parametrize("status_code", [400, 401, 429, 500, 503])
def test_a_refusal_means_not_knowing_and_not_a_closed_exchange(
    store: TokenStore, status_code: int
) -> None:
    """Любой отказ даёт «не знаем», а не «биржа закрыта», и наружу не выходит.

    Программа не встаёт из-за незнания расписания: `None` означает
    «вести себя как раньше».
    """
    broker = Broker()
    broker.answer(TRADING_STATUS_PATH, json_reply(status_code, {"code": "SOME_ERROR"}))
    clock = Clock()

    assert ask_status(store, broker, clock) is None


def test_a_refusal_is_not_repeated_on_every_call(store: TokenStore) -> None:
    """После отказа вопрос не повторяется на каждый вызов.

    Вызывающий спрашивает из цикла переподключения — раз в минуту и чаще.
    Без короткой памяти на «не знаем» это был бы запрос на каждую попытку.
    """
    broker = Broker()
    broker.answer(TRADING_STATUS_PATH, json_reply(500, {"code": "INTERNAL_SERVER_ERROR"}))
    clock = Clock()

    async def scenario() -> None:
        async with session(store, broker, clock) as connection:
            hours = Schedule(connection, clock=clock)
            for _ in range(4):
                assert await hours.status(class_code=CLASS) is None
                clock.advance(timedelta(seconds=5))

    run(scenario())
    assert broker.count(TRADING_STATUS_PATH) == 1


def test_after_not_knowing_goes_stale_the_question_is_asked_again(store: TokenStore) -> None:
    """Короткая память на «не знаем» именно короткая: связь вернулась — спросили."""
    broker = Broker()
    broker.answer(
        TRADING_STATUS_PATH,
        json_reply(500, {"code": "INTERNAL_SERVER_ERROR"}),
        json_reply(200, STATUS_ANSWER),
    )
    clock = Clock()

    async def scenario() -> TradingStatus | None:
        async with session(store, broker, clock) as connection:
            hours = Schedule(connection, clock=clock)
            assert await hours.status(class_code=CLASS) is None
            clock.advance(UNKNOWN_MAX_AGE + timedelta(seconds=1))
            return await hours.status(class_code=CLASS)

    status = run(scenario())
    assert broker.count(TRADING_STATUS_PATH) == 2
    assert status is not None and status.is_open is True


def test_a_broken_answer_is_not_knowing_too(store: TokenStore) -> None:
    """200 с телом не того вида — тоже «не знаем», а не падение вызывающего."""
    broker = Broker()
    broker.answer(TRADING_STATUS_PATH, json_reply(200, ["не объект"]))
    clock = Clock()

    assert ask_status(store, broker, clock) is None


def test_a_gateway_404_stops_the_asking_for_the_whole_session(store: TokenStore) -> None:
    """Шлюз не знает адреса — слой перестаёт ходить туда до перезапуска.

    Имя сервиса выбрано из двух расходящихся документов. Если оно неверно,
    ответит nginx брокера, а не его сервис; повторять этот запрос всю ночь
    незачем.
    """
    broker = Broker()
    broker.answer(
        TRADING_STATUS_PATH,
        text_reply(404, "<html><head><title>404 Not Found</title></head></html>"),
    )
    clock = Clock()

    async def scenario() -> None:
        async with session(store, broker, clock) as connection:
            hours = Schedule(connection, clock=clock)
            assert await hours.status(class_code=CLASS) is None
            clock.advance(UNKNOWN_MAX_AGE + timedelta(seconds=1))
            assert await hours.status(class_code=CLASS) is None
            assert connection.address_is_wrong(TRADING_STATUS_PATH) is True

    run(scenario())
    assert broker.count(TRADING_STATUS_PATH) == 1, "по несуществующему адресу сходили дважды"


def test_a_cancelled_call_is_not_swallowed_as_not_knowing(store: TokenStore) -> None:
    """Отмена задачи проходит насквозь: «не знаем» — только про отказ брокера.

    `except Exception` вместо `except BrokerError` превратил бы закрытие
    окна и дефект нашего же кода в тихое «расписание неизвестно».
    """
    broker = Broker()

    def cancel(request: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError

    broker.answer(TRADING_STATUS_PATH, cancel)
    clock = Clock()

    async def scenario() -> None:
        async with session(store, broker, clock) as connection:
            await Schedule(connection, clock=clock).status(class_code=CLASS)

    with pytest.raises(asyncio.CancelledError):
        run(scenario())


def test_the_refusal_is_named_in_the_log_once_and_then_quietly(
    store: TokenStore, journal: io.StringIO
) -> None:
    """Отказ — одно предупреждение, дальше отладочные записи.

    Ночь без связи иначе оставила бы в техническом логе тысячи одинаковых
    строк, среди которых не видно ничего другого.
    """
    broker = Broker()
    broker.answer(TRADING_STATUS_PATH, json_reply(500, {"code": "INTERNAL_SERVER_ERROR"}))
    clock = Clock()

    async def scenario() -> None:
        async with session(store, broker, clock) as connection:
            hours = Schedule(connection, clock=clock)
            for _ in range(3):
                await hours.status(class_code=CLASS)
                clock.advance(UNKNOWN_MAX_AGE + timedelta(seconds=1))

    run(scenario())
    lines = [line for line in journal.getvalue().splitlines() if "расписание торгов" in line]
    assert len([line for line in lines if line.startswith("WARNING")]) == 1, lines
    assert len([line for line in lines if line.startswith("DEBUG")]) == 2, lines


def test_the_human_text_of_the_refusal_stays_out_of_the_log(
    store: TokenStore, journal: io.StringIO
) -> None:
    """В технический лог идёт техническое. С владельцем счёта говорит `app/`.

    Правило записано 05.09.2026 ценой двух показов подряд: человеческий текст
    отказа доехал до консоли владельца счёта через записи слоя.
    """
    broker = Broker()
    broker.answer(
        TRADING_STATUS_PATH,
        text_reply(404, "<html><head><title>404 Not Found</title></head></html>"),
    )
    clock = Clock()
    human = ADDRESS_SENSITIVE[TRADING_STATUS_PATH][0]

    assert ask_status(store, broker, clock) is None
    # Только записи этого модуля. Тело ответа в логе есть — его кладёт туда
    # отдельной записью уровня DEBUG сама сессия (`_note_technical`), и это
    # её осознанное поведение. Проверяется, что запись расписания не несёт
    # ни человеческого текста, ни тела.
    mine = [line for line in journal.getvalue().splitlines() if "расписание торгов" in line]
    assert mine, journal.getvalue()
    for line in mine:
        assert human not in line, line
        assert "<html" not in line, line


def test_the_refusal_that_reaches_the_layer_is_a_broker_error(store: TokenStore) -> None:
    """Отказ шлюза остаётся отказом слоя, а не голым исключением httpx.

    Проверка того, что «не знаем» построено на `BrokerError`: поймай слой
    что-то другое, ветка отказа была бы мертва, а тесты выше — зелены.
    """
    broker = Broker()
    broker.answer(
        TRADING_STATUS_PATH,
        text_reply(404, "<html><head><title>404 Not Found</title></head></html>"),
    )
    clock = Clock()

    async def scenario() -> BrokerError:
        async with session(store, broker, clock) as connection:
            with pytest.raises(BrokerError) as caught:
                await connection.read(TRADING_STATUS_PATH, params={"classCode": CLASS})
            return caught.value

    error = run(scenario())
    assert isinstance(error, WrongAddress)


# --- короткий ответ ---


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (json_reply(200, dict(STATUS_ANSWER, tradingSessionStatus="OPEN")), True),
        (json_reply(200, dict(STATUS_ANSWER, tradingSessionStatus="CLOSE")), False),
        (json_reply(200, {}), None),
        (json_reply(500, {"code": "INTERNAL_SERVER_ERROR"}), None),
    ],
)
def test_is_open_has_three_answers_and_none_of_them_is_a_lie(
    store: TokenStore, answer: Callable[[httpx.Request], httpx.Response], expected: bool | None
) -> None:
    """`is_open` отвечает да, нет или «не знаю» — и третье не притворяется вторым."""
    broker = Broker()
    broker.answer(TRADING_STATUS_PATH, answer)
    clock = Clock()

    async def scenario() -> bool | None:
        async with session(store, broker, clock) as connection:
            return await Schedule(connection, clock=clock).is_open(class_code=CLASS)

    assert run(scenario()) is expected


# --- токен ---


def test_the_token_does_not_reach_the_log_of_this_module(
    store: TokenStore, journal: io.StringIO
) -> None:
    """Ни одна запись расписания не выносит значение токена.

    Общие сторожа стоят в `tests/test_broker_no_leak.py`; здесь проверяется
    та ветка, которой там нет, — отказ по запросу расписания.
    """
    broker = Broker()
    broker.answer(TRADING_STATUS_PATH, json_reply(401, {"code": "UNAUTHORIZED"}))
    clock = Clock()

    assert ask_status(store, broker, clock) is None
    written = journal.getvalue()
    assert FAKE_ACCESS not in written, written
    assert FAKE_REFRESH not in written, written
    assert FAKE_ACCESS[:8] not in written, written


# --- причину отказа можно спросить (`D-062`) ---


def test_the_reason_the_status_is_unknown_can_be_asked_for(store: TokenStore) -> None:
    """`None` от `status` перестал быть безымянным: отказ отдаётся объектом.

    Это и есть `D-062`. Пока «спросить не удалось» и «торги идут» приезжали
    одним и тем же `None`, ошибка в адресе расписания провалилась бы молча —
    а прецедент свежий (`B-010`, неверное имя сервиса свечей).
    """
    broker = Broker()
    broker.answer(TRADING_STATUS_PATH, json_reply(500, {"code": "INTERNAL_SERVER_ERROR"}))
    clock = Clock()

    async def scenario() -> BrokerError | None:
        async with session(store, broker, clock) as connection:
            hours = Schedule(connection, clock=clock)
            assert await hours.status(class_code=CLASS) is None
            return hours.status_trouble(class_code=CLASS)

    failure = run(scenario())
    assert failure is not None, "расписание промолчало о том, почему промолчало"
    assert failure.retryable is True, "неполадка у брокера объявлена неисправимой"
    assert "неполадка" in failure.human, failure.human


def test_a_wrong_address_is_told_apart_from_a_lost_connection(store: TokenStore) -> None:
    """Шлюз не знает адреса — отказ отличается **типом**, а не только текстом.

    Ради этого различения долг и заведён: «нет связи» пройдёт само,
    а неверный адрес не пройдёт никогда, и слова человеку нужны разные.
    """
    broker = Broker()
    broker.answer(
        TRADING_STATUS_PATH,
        text_reply(404, "<html><head><title>404 Not Found</title></head></html>"),
    )
    clock = Clock()

    async def scenario() -> BrokerError | None:
        async with session(store, broker, clock) as connection:
            hours = Schedule(connection, clock=clock)
            assert await hours.status(class_code=CLASS) is None
            return hours.status_trouble(class_code=CLASS)

    failure = run(scenario())
    assert isinstance(failure, WrongAddress), f"тип отказа: {type(failure).__name__}"
    assert failure.retryable is False


def test_the_reason_outlives_the_request_and_serves_the_remembered_answer(
    store: TokenStore,
) -> None:
    """Причина годится и тогда, когда «не знаем» пришло из памяти, а не из сети.

    Вызывающий спрашивает расписание перед каждой попыткой подключения,
    и почти все ответы ему отдаёт память (`UNKNOWN_MAX_AGE`). Причина,
    жившая бы только до конца запроса, была бы ему видна один раз из ста.
    """
    broker = Broker()
    broker.answer(TRADING_STATUS_PATH, json_reply(400, {"code": "VALIDATION_ERROR"}))
    clock = Clock()

    async def scenario() -> BrokerError | None:
        async with session(store, broker, clock) as connection:
            hours = Schedule(connection, clock=clock)
            for _ in range(4):
                assert await hours.status(class_code=CLASS) is None
                clock.advance(timedelta(seconds=1))
            return hours.status_trouble(class_code=CLASS)

    failure = run(scenario())
    assert broker.count(TRADING_STATUS_PATH) == 1, "память «не знаем» перестала работать"
    assert failure is not None and failure.retryable is False


def test_a_good_answer_wipes_the_previous_reason(store: TokenStore) -> None:
    """Связь вернулась — причины больше нет. Иначе она пережила бы починку."""
    broker = Broker()
    broker.answer(
        TRADING_STATUS_PATH,
        json_reply(500, {"code": "INTERNAL_SERVER_ERROR"}),
        json_reply(200, STATUS_ANSWER),
    )
    clock = Clock()

    async def scenario() -> BrokerError | None:
        async with session(store, broker, clock) as connection:
            hours = Schedule(connection, clock=clock)
            assert await hours.status(class_code=CLASS) is None
            assert hours.status_trouble(class_code=CLASS) is not None
            clock.advance(UNKNOWN_MAX_AGE + timedelta(seconds=1))
            assert await hours.status(class_code=CLASS) is not None
            return hours.status_trouble(class_code=CLASS)

    assert run(scenario()) is None


def test_the_reason_of_the_day_schedule_is_not_mistaken_for_the_status_one(
    store: TokenStore,
) -> None:
    """Отказ дневного расписания не выдаётся за отказ статуса.

    Ключи вопросов разные, и это проверяется, потому что сойдись они —
    `status_trouble` начал бы отвечать про чужой вопрос, а поток сказал бы
    владельцу счёта не про то.
    """
    broker = Broker()
    broker.answer(TRADING_STATUS_PATH, json_reply(200, STATUS_ANSWER))
    broker.answer(DAILY_SCHEDULE_PATH, json_reply(500, {"code": "INTERNAL_SERVER_ERROR"}))
    clock = Clock()

    async def scenario() -> BrokerError | None:
        async with session(store, broker, clock) as connection:
            hours = Schedule(connection, clock=clock)
            assert await hours.daily(class_code=CLASS, ticker=TICKER) is None
            assert await hours.status(class_code=CLASS) is not None
            return hours.status_trouble(class_code=CLASS)

    assert run(scenario()) is None


def test_asking_for_a_reason_costs_no_request(store: TokenStore) -> None:
    """`status_trouble` в сеть не ходит: ответ уже лежит в памяти.

    Иначе вызывающий, спрашивающий причину перед каждой попыткой,
    удвоил бы расход общей квоты запросов ради строки в журнале.
    """
    broker = Broker()
    broker.answer(TRADING_STATUS_PATH, json_reply(500, {"code": "INTERNAL_SERVER_ERROR"}))
    clock = Clock()

    async def scenario() -> None:
        async with session(store, broker, clock) as connection:
            hours = Schedule(connection, clock=clock)
            assert await hours.status(class_code=CLASS) is None
            for _ in range(5):
                assert hours.status_trouble(class_code=CLASS) is not None

    run(scenario())
    assert broker.count(TRADING_STATUS_PATH) == 1


def test_forgetting_drops_the_reason_along_with_the_answer(store: TokenStore) -> None:
    """`forget` чистит и причину: иначе она пережила бы смену инструмента."""
    broker = Broker()
    broker.answer(TRADING_STATUS_PATH, json_reply(500, {"code": "INTERNAL_SERVER_ERROR"}))
    clock = Clock()

    async def scenario() -> BrokerError | None:
        async with session(store, broker, clock) as connection:
            hours = Schedule(connection, clock=clock)
            assert await hours.status(class_code=CLASS) is None
            hours.forget()
            return hours.status_trouble(class_code=CLASS)

    assert run(scenario()) is None


def test_the_reason_carries_no_token(store: TokenStore) -> None:
    """Отказ, отданный наружу, не выносит значения токена ни одним полем.

    Он уезжает в журнал решений, то есть на экран и в выгрузку, — а `human`
    и `technical` собираются из тела ответа брокера (правило 7).
    """
    broker = Broker()
    broker.answer(
        TRADING_STATUS_PATH,
        text_reply(401, f'{{"code": "UNAUTHORIZED", "message": "{FAKE_ACCESS}"}}'),
    )
    clock = Clock()

    async def scenario() -> BrokerError | None:
        async with session(store, broker, clock) as connection:
            hours = Schedule(connection, clock=clock)
            assert await hours.status(class_code=CLASS) is None
            return hours.status_trouble(class_code=CLASS)

    failure = run(scenario())
    assert failure is not None
    for field in (failure.human, failure.technical, failure.details, str(failure)):
        assert FAKE_ACCESS not in field, field
        assert FAKE_ACCESS[:8] not in field, field


def test_the_reason_is_found_however_the_class_code_is_spelled(store: TokenStore) -> None:
    """Пробелы вокруг кода класса не разводят вопрос и его причину.

    Запрос подрезает код перед отправкой, и ключ причины подрезает его же.
    Разойдись они — `status_trouble` отвечал бы `None` на живой отказ,
    то есть молчание вернулось бы тем же путём, но уже с виду починенным.
    """
    broker = Broker()
    broker.answer(TRADING_STATUS_PATH, json_reply(500, {"code": "INTERNAL_SERVER_ERROR"}))
    clock = Clock()

    async def scenario() -> tuple[BrokerError | None, BrokerError | None]:
        async with session(store, broker, clock) as connection:
            hours = Schedule(connection, clock=clock)
            assert await hours.status(class_code=f"  {CLASS}  ") is None
            return (
                hours.status_trouble(class_code=CLASS),
                hours.status_trouble(class_code=f"  {CLASS}  "),
            )

    trimmed, padded = run(scenario())
    assert trimmed is not None, "причина не нашлась по подрезанному коду класса"
    assert padded is not None, "причина не нашлась по коду класса с пробелами"
