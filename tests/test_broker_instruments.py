"""Справочник инструментов: форма ответа живого сервера, дата экспирации.

В сеть тесты не ходят: транспорт `httpx` подменён. Настоящий токен
не участвует.

⚠️ **Главное, что здесь стережётся.** Живой запрос 04.09.2026 вернул
**голый массив**, а и PDF, и сайт описывают объект с полем `instruments`.
Разбор был написан по документации и на живом ответе молча отдавал пустоту:
справочник — единственный путь, которым в программу попадают шаг цены,
стоимость шага и дата экспирации, и «ничего не нашлось» читалось бы
как «нет такого тикера».
"""

from __future__ import annotations

import asyncio
import pathlib
from datetime import datetime, timedelta, timezone
from typing import Any, Coroutine, TypeVar

import httpx
import pytest

from broker.errors import NotFound, UnexpectedAnswer
from broker.instruments import Instrument, Instruments, parse_instruments
from broker.secret import Secret
from broker.session import AUTH_URL, INSTRUMENTS_BY_TICKERS_PATH, BrokerSession
from broker.throttle import Throttle
from broker.token_store import TokenStore
from broker.tokens import ACCESS_LIFETIME, TokenScope, now_utc

FAKE_REFRESH = "FAKE-instruments-refresh-0000-NOT-A-REAL-TOKEN"
FAKE_ACCESS = "FAKE-instruments-access-1111-NOT-A-REAL-TOKEN"

START = datetime(2026, 9, 5, 6, 0, tzinfo=timezone.utc)

_Result = TypeVar("_Result")

#: Ответ живого сервера 04.09.2026 — дословно из
#: `.docs/broker-api/19-instruments.md`, раздел «Проверено живым запросом».
#: Обёртки нет: это **массив**, а не объект с полем `instruments`.
LIVE_ANSWER: list[dict[str, Any]] = [
    {
        "ticker": "MXU6",
        "boards": [{"classCode": "SPBFUT", "exchange": "MOEX"}],
        "shortName": "MIX-9.26",
        "displayName": "MIX-9.26",
        "type": "Фьючерсы",
        "instrumentType": "FUTURES",
        "tradingCurrency": "RUB",
        "minimumStep": 25.0,
        "lotSize": 1.0,
        "settleCode": "T+n",
        "settlementDate": "2026-09-07T00:00:00.000Z",
        "maturityDate": "20260917",
        "isCanShort": True,
        "baseAsset": "Индекс МосБиржи",
        "currencyStepPrice": "RUB",
    }
]

#: Тот же инструмент в форме, которую описывают PDF и сайт.
DOCUMENTED_ANSWER: dict[str, Any] = {
    "instruments": [dict(LIVE_ANSWER[0], maturityDate="2026-09-17T18:50:00.000Z")]
}


class Broker:
    """Подставной брокер: авторизация даром, справочник — заданным ответом."""

    def __init__(self, answer: object, status: int = 200) -> None:
        self.answer = answer
        self.status = status
        self.asked: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == AUTH_URL:
            return httpx.Response(
                200,
                json={
                    "expires_in": int(ACCESS_LIFETIME.total_seconds()),
                    "token_type": "bearer",
                    "access_token": FAKE_ACCESS,
                },
            )
        self.asked.append(request)
        return httpx.Response(self.status, json=self.answer)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url="https://example.invalid", transport=httpx.MockTransport(self)
        )


@pytest.fixture
def store(tmp_path: pathlib.Path) -> TokenStore:
    directory = tmp_path / "userdata"
    directory.mkdir()
    keeper = TokenStore(directory)
    keeper.save(Secret(FAKE_REFRESH), TokenScope.READ_ONLY, issued_at=START - timedelta(days=1))
    return keeper


async def no_sleep(seconds: float) -> None:
    return None


def run(work: Coroutine[Any, Any, _Result]) -> _Result:
    return asyncio.run(work)


def session(store: TokenStore, broker: Broker) -> BrokerSession:
    return BrokerSession(
        store,
        client=broker.client(),
        clock=lambda: START,
        sleep=no_sleep,
        throttle=Throttle(rate=1000.0),
        attempts=1,
    )


# --- форма ответа ---


def test_the_shape_the_live_server_sent_is_read() -> None:
    """Голый массив разбирается: именно так ответил брокер 04.09.2026.

    До 05.09.2026 разбор возвращал на нём пустой кортеж, и справочник
    не работал ни разу за всё время существования слоя.
    """
    found = parse_instruments(LIVE_ANSWER)
    assert len(found) == 1
    card = found[0]
    assert card.ticker == "MXU6"
    assert card.class_code == "SPBFUT"
    assert card.lot == 1.0
    assert card.price_step == 25.0


def test_the_shape_the_documentation_describes_is_still_read() -> None:
    """Объект с полем `instruments` читается по-прежнему: так пишут PDF и сайт."""
    found = parse_instruments(DOCUMENTED_ANSWER)
    assert len(found) == 1 and found[0].ticker == "MXU6"


@pytest.mark.parametrize(
    "payload",
    [
        {"data": [{"ticker": "MXU6"}]},
        {"items": []},
        {},
        "MXU6",
        5,
        None,
    ],
)
def test_a_third_shape_is_refused_out_loud_and_not_taken_for_emptiness(payload: object) -> None:
    """Незнакомая форма — отказ вслух, а не пустой ответ.

    Пустота здесь и есть беда: «инструментов не найдено» читается как
    «нет такого тикера», а на самом деле брокер ответил не так, как мы ждём.
    Именно так пропажа и пряталась год.
    """
    with pytest.raises(UnexpectedAnswer):
        parse_instruments(payload)


@pytest.mark.parametrize("payload", [[], {"instruments": []}])
def test_an_empty_list_is_a_legal_answer(payload: object) -> None:
    """Пустой список — законное «не найдено», в обеих формах."""
    assert parse_instruments(payload) == ()


def test_a_stranger_among_the_instruments_is_skipped() -> None:
    """Не объект в списке пропускается: соседние карточки не теряются."""
    found = parse_instruments([*LIVE_ANSWER, "мусор", 7, {"нет тикера": 1}])
    assert len(found) == 1 and found[0].ticker == "MXU6"


# --- дата экспирации ---


def test_the_expiry_of_the_live_contract_is_read_from_maturity_date() -> None:
    """`maturityDate` живого ответа — 17.09.2026, дата экспирации MIX-9.26.

    Поле для предупреждения «скоро истекает контракт» есть и заполнено.
    Подключает его `app/`; слой брокера только отдаёт значение.
    """
    card = parse_instruments(LIVE_ANSWER)[0]
    assert card.maturity is not None
    assert (card.maturity.year, card.maturity.month, card.maturity.day) == (2026, 9, 17)


@pytest.mark.parametrize("answer", [LIVE_ANSWER, DOCUMENTED_ANSWER])
def test_the_expiry_can_always_be_compared_with_the_current_moment(answer: object) -> None:
    """Дата экспирации всегда **с поясом** — иначе «сколько дней осталось» падает.

    Живой сервер прислал `"20260917"` — дату без пояса, схема обещает
    `date-time` с поясом. Смешанное поле уронило бы вычитание из `now_utc()`
    с `TypeError` ровно на том контракте, который сейчас в работе,
    и работало бы на выдуманном из тестов.
    """
    card = parse_instruments(answer)[0]
    assert card.maturity is not None
    assert card.maturity.tzinfo is not None
    assert isinstance(card.maturity - now_utc(), timedelta)


def test_a_maturity_that_cannot_be_read_is_nothing_and_not_a_date() -> None:
    """Нечитаемое значение даёт `None`, а не сегодняшнее число."""
    card = parse_instruments([dict(LIVE_ANSWER[0], maturityDate="никогда")])[0]
    assert card.maturity is None


# --- поля карточки ---


def test_the_latin_type_wins_over_the_russian_one() -> None:
    """Живой сервер прислал `type` по-русски; берётся латинский `instrumentType`."""
    card = parse_instruments(LIVE_ANSWER)[0]
    assert card.instrument_type == "FUTURES"


def test_the_class_code_comes_out_of_the_boards_array() -> None:
    """Код класса лежит в массиве бордов, а не полем верхнего уровня."""
    assert parse_instruments(LIVE_ANSWER)[0].class_code == "SPBFUT"


def test_the_live_answer_has_no_step_price_and_the_card_says_so() -> None:
    """В записанном живом ответе `stepPrice` нет — карточка это признаёт.

    ⚠️ Значит «рублей в пункте» из справочника сегодня не берётся.
    Единицу вместо неизвестной стоимости шага подставлять нельзя: это
    ошибка результата в разы. Проверено ли это на полном живом ответе —
    неизвестно: записанный в `19-instruments.md` ответ мог быть сокращён.
    """
    card = parse_instruments(LIVE_ANSWER)[0]
    assert card.step_price is None
    assert card.complete_for_trading is False
    assert "стоимость шага цены" in card.missing()


# --- через подключение ---


def test_the_reference_goes_through_the_session_on_the_live_shape(store: TokenStore) -> None:
    """Сквозной проход: запрос уходит по документированному адресу и разбирается.

    Проверка того, что живая форма ответа доезжает до вызывающего, а не
    только до `parse_instruments`.
    """
    broker = Broker(LIVE_ANSWER)

    async def scenario() -> Instrument:
        async with session(store, broker) as connection:
            return await Instruments(connection).one("MXU6")

    card = run(scenario())
    assert card.ticker == "MXU6"
    assert broker.asked[0].url.path == INSTRUMENTS_BY_TICKERS_PATH
    assert broker.asked[0].method == "POST"


def test_a_ticker_that_did_not_come_back_is_a_refusal(store: TokenStore) -> None:
    """Тикера нет в ответе — отказ `NotFound`, а не чужая карточка."""
    broker = Broker(LIVE_ANSWER)

    async def scenario() -> None:
        async with session(store, broker) as connection:
            with pytest.raises(NotFound):
                await Instruments(connection).one("RIU6")

    run(scenario())


def test_an_unknown_shape_from_the_wire_becomes_a_layer_refusal(store: TokenStore) -> None:
    """Незнакомая форма из сети выходит отказом слоя, а не пустым справочником."""
    broker = Broker({"data": []})

    async def scenario() -> None:
        async with session(store, broker) as connection:
            with pytest.raises(UnexpectedAnswer):
                await Instruments(connection).by_tickers(["MXU6"])

    run(scenario())


def test_an_empty_request_never_reaches_the_broker(store: TokenStore) -> None:
    """Пустой список тикеров запросом не становится: квота слоя не тратится."""
    broker = Broker(LIVE_ANSWER)

    async def scenario() -> tuple[Instrument, ...]:
        async with session(store, broker) as connection:
            return await Instruments(connection).by_tickers([" ", ""])

    assert run(scenario()) == ()
    assert broker.asked == []


def test_the_refusal_text_does_not_show_the_answer_body(store: TokenStore) -> None:
    """В тексте для человека нет ни имён полей, ни содержимого ответа."""
    broker = Broker({"data": [{"secretish": "МОЙ-ТОКЕН"}]})

    async def scenario() -> UnexpectedAnswer:
        async with session(store, broker) as connection:
            with pytest.raises(UnexpectedAnswer) as caught:
                await Instruments(connection).by_tickers(["MXU6"])
            return caught.value

    error = run(scenario())
    assert "МОЙ-ТОКЕН" not in error.human
    assert "МОЙ-ТОКЕН" not in str(error)
    assert "МОЙ-ТОКЕН" not in repr(error)
