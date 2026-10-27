"""Один сигнал — не больше одной живой заявки у брокера, при любом исходе команды.

Э4, шаг 1, фаза 3 (`.docs/plans/E4-live-orders.md` §8 №1, №2, №3, №4, №6, №7
на уровне `broker/`; §11 «Что стерегут тесты»). Брокер — подделка
`tests/fake_broker.py`: одна книга заявок на HTTP и на потоке статусов,
и о чём снимок молчит, она решает в худшую для нас сторону (повтор ключа —
вторая заявка; ответ потерян — заявка всё равно создана).

«Сигнал» здесь — один вызов команды сессии. Что следующая свеча не подаёт
второй вызов, держит исполнитель в `app/` (шаг 2) журналом и запросом
статуса; этот файл доказывает, что у него для этого всё есть: ключ записан
до отправки, по нему заявка находится, и сам слой не шлёт второго.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from collections.abc import Coroutine
from datetime import timedelta
from typing import TypeVar
from uuid import UUID

import httpx
import pytest
import websockets

from broker.errors import BadRequest, BrokerError, NoConnection, OutcomeUnknown
from broker.order_stream import OrderEvents, order_events
from broker.orders import (
    CommandAccepted,
    NewOrder,
    OrderEvent,
    OrderSide,
    OrderState,
    OrderStatus,
    OrderType,
)
from broker.secret import Secret
from broker.session import BrokerSession
from broker.throttle import Throttle
from broker.token_store import TokenStore
from broker.tokens import TokenScope
from market.commands import BrokerExecution
from market.storage import CandleStore
from tests.fake_broker import ANSWERS, CREATE, Answer, FakeBroker, Journal
from tests.test_broker_session import FAKE_ACCESS, FAKE_REFRESH, START, Clock

MARKET_BUY = NewOrder(
    side=OrderSide.BUY,
    order_type=OrderType.MARKET,
    quantity=1,
    ticker="MXZ6",
    class_code="SPBFUT",
)
#: Тейк лонга: лимитная продажа выше рынка (план §2.3).
TAKE = NewOrder(
    side=OrderSide.SELL,
    order_type=OrderType.LIMIT,
    quantity=1,
    ticker="MXZ6",
    class_code="SPBFUT",
    price=231_450.0,
)

#: Попыток с запасом: повтор, если бы он был разрешён, успел бы случиться.
ATTEMPTS = 5

_Result = TypeVar("_Result")


def run(coroutine: Coroutine[object, object, _Result]) -> _Result:
    return asyncio.run(coroutine)


class Sleeps:
    """Пауза между повторами: помнит, сколько просили ждать, и не ждёт."""

    def __init__(self) -> None:
        self.asked: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.asked.append(seconds)


def open_session(
    store: TokenStore, broker: FakeBroker, sleep: Sleeps | None = None
) -> BrokerSession:
    return BrokerSession(
        store,
        client=broker.client(),
        clock=Clock(),
        sleep=sleep or Sleeps(),
        attempts=ATTEMPTS,
        throttle=Throttle(rate=1000.0),
        order_throttle=Throttle(rate=1000.0),
    )


@pytest.fixture
def store(tmp_path: pathlib.Path) -> TokenStore:
    directory = tmp_path / "userdata"
    directory.mkdir()
    keeper = TokenStore(directory)
    keeper.save(Secret(FAKE_REFRESH), TokenScope.TRADE, issued_at=START - timedelta(days=1))
    return keeper


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch) -> FakeBroker:
    """Подделка на обоих каналах: HTTP — через клиент сессии, поток — подменой `connect`."""
    fake = FakeBroker()
    monkeypatch.setattr(websockets, "connect", fake.connect)
    return fake


async def next_event(events: OrderEvents) -> OrderEvent:
    """Следующее событие; не пришло за секунду — провал, а не вечное ожидание."""
    event: OrderEvent = await asyncio.wait_for(anext(events), timeout=1.0)
    return event


# --- создание: один сигнал при любом исходе ---

#: Что получает вызывающий на каждый исход из `ANSWERS`. Ожидание — по плану
#: §2.4: 200 — принята; 400 и 422 — заявки нет точно; всё прочее — исход
#: неизвестен и остановка, а не повтор и не «отказ».
EXPECTED: dict[str, type[BrokerError] | None] = {
    "принята, ответ 200": None,
    "принята, ответ потерян по таймауту": OutcomeUnknown,
    "принята, связь оборвана до ответа": OutcomeUnknown,
    "принята, ответ 500": OutcomeUnknown,
    "принята, ответ 429": OutcomeUnknown,
    "не принята, 429": OutcomeUnknown,
    "не принята, 503": OutcomeUnknown,
    "не принята, нет подключения": OutcomeUnknown,
    "не принята, 400": BadRequest,
    "не принята, 422": BadRequest,
}


def test_every_answer_of_the_fake_has_an_expectation() -> None:
    """Таблица ожиданий покрывает все исходы подделки: новый исход без ожидания не прячется."""
    assert set(EXPECTED) == set(ANSWERS)


@pytest.mark.parametrize("answer", sorted(ANSWERS))
def test_one_signal_is_at_most_one_live_order_whatever_the_broker_answers(
    store: TokenStore, broker: FakeBroker, answer: str
) -> None:
    """Одна команда создания — один POST, не больше одной живой заявки, и она находится по журналу.

    Стережёт §8 №1, №2, №6 на уровне слоя: дубль начинается с повтора POST
    или с ключа, которого нет в журнале. Худший брокер создаёт заявку и теряет
    ответ — тогда заявка есть, а программа узнаёт о ней только запросом
    статуса по ключу журнала. Мутации: пишущая пара в `REPEATABLE`; «исход
    неизвестен» → обычный отказ; ключ записи ≠ ключ POST; запись после POST.
    """
    broker.create_answers.append(ANSWERS[answer])
    journal = Journal(broker)

    async def scenario() -> tuple[CommandAccepted | BaseException, OrderState | None]:
        async with open_session(store, broker) as session:
            try:
                outcome: CommandAccepted | BaseException = await session.create_order(
                    MARKET_BUY, record=journal
                )
            except BrokerError as error:
                outcome = error
            # То, что сделает исполнитель после любого исхода: спросит по ключу журнала.
            key = journal.entries[0][0] if journal.entries else None
            found = None if key is None else await session.order_status(key)
            return outcome, found

    outcome, found = run(scenario())
    expected = EXPECTED[answer]
    if expected is None:
        assert isinstance(outcome, CommandAccepted), f"{answer}: вышло {outcome!r}"
    else:
        assert type(outcome) is expected, (
            f"{answer}: {type(outcome).__name__} вместо {expected.__name__}"
        )

    assert len(broker.commands) == 1, f"{answer}: команд ушло {len(broker.commands)} — дубль"
    assert journal.sent_before_entry == [0], f"{answer}: команда ушла раньше записи журнала"
    [(key, _request)] = journal.entries
    assert broker.peak_live <= 1, f"{answer}: у брокера было {broker.peak_live} живых заявок"
    booked = broker.order(str(key))
    if ANSWERS[answer].applies:
        assert booked is not None and booked.live, (
            f"{answer}: заявка, созданная брокером, не находится по ключу журнала"
        )
        assert found is not None and found.latest.status is OrderStatus.NEW, (
            f"{answer}: статус по ключу журнала не нашёл живую заявку"
        )
    else:
        assert broker.orders == [] and found is None


def test_the_fake_books_a_repeated_key_as_a_second_order(broker: FakeBroker) -> None:
    """Подделка честно худшая: повтор ключа у неё — вторая живая заявка.

    Без этого тест выше молчал бы о повторе с тем же ключом: брокер,
    склеивающий повторы, спрятал бы дубль. Проверяется сама подделка.
    """
    body = MARKET_BUY.body(UUID(int=7))
    for _ in range(2):
        broker(httpx.Request(CREATE[0], "https://example.invalid" + CREATE[1], json=body))
    assert len(broker.live()) == 2
    assert broker.keys_sent("create") == {str(UUID(int=7)): 2}


# --- частичное исполнение ---


def test_a_partial_fill_arrives_as_two_executions_of_one_order_and_is_stored_once(
    store: TokenStore, broker: FakeBroker, tmp_path: pathlib.Path
) -> None:
    """Объём 2, исполнения 1 + 1: два события одной заявки, ни одного нового POST.

    Повтор последнего события в журнал исполнений второй строки не даёт
    (план §8 №3, §2.10). Ключ у всех событий — наш, из `originalClientOrderId`
    (пример 24). Мутации: ключ только из `clientOrderId`; исполнение без
    `executionId` пропускается; повтор `executionId` — вторая строка.
    """
    order = NewOrder(
        side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=2,
        ticker="MXZ6", class_code="SPBFUT",
    )
    journal = Journal(broker)

    async def scenario() -> tuple[CommandAccepted, list[OrderEvent]]:
        async with open_session(store, broker) as session, order_events(session) as events:
            accepted = await session.create_order(order, record=journal)
            seen = [await next_event(events)]
            key = str(accepted.key)
            broker.execute(key, 1, 231_000.0)
            seen.append(await next_event(events))
            broker.execute(key, 1, 231_050.0)
            seen.append(await next_event(events))
            broker.replay_last()
            seen.append(await next_event(events))
            return accepted, seen

    accepted, seen = run(scenario())
    assert [event.key for event in seen] == [str(accepted.key)] * 4, "событие потеряло наш ключ"
    assert [event.report.status for event in seen] == [
        OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, OrderStatus.FILLED,
    ]
    executions = [event.report for event in seen if event.report.is_execution]
    assert [report.last_quantity for report in executions] == [1, 1, 1]
    assert len({report.execution_id for report in executions}) == 2
    assert len(broker.commands) == 1 and broker.peak_live == 1 and broker.live() == []

    with CandleStore(tmp_path / "executions.sqlite3") as journal_db:
        written = [
            journal_db.record_broker_execution(
                BrokerExecution(
                    execution_id=str(report.execution_id),
                    client_order_id=str(accepted.key),
                    volume=int(report.last_quantity or 0),
                    price=float(report.average_price or 0.0),
                    at=report.transaction_time or START,
                )
            )
            for report in executions
        ]
        stored = journal_db.broker_executions(str(accepted.key))
    assert written == [True, True, False], "повтор события лёг второй строкой"
    assert sum(one.volume for one in stored) == 2


# --- гонка отмены с исполнением ---

#: Как брокер отвечает на отмену заявки, которая уже исполнилась. Что он
#: отвечает на самом деле — нет в снимке (12, «Зачем»): перебираются все
#: ответы, и ни один не даёт права слать второй запрос.
CANCEL_AFTER_FILL: dict[str, tuple[Answer, type[BrokerError] | None]] = {
    "200 «запрос принят»": (ANSWERS["принята, ответ 200"], None),
    "422": (ANSWERS["не принята, 422"], BadRequest),
    "500": (ANSWERS["принята, ответ 500"], OutcomeUnknown),
    "таймаут": (ANSWERS["принята, ответ потерян по таймауту"], OutcomeUnknown),
    "429": (ANSWERS["не принята, 429"], OutcomeUnknown),
}


@pytest.mark.parametrize("answer", sorted(CANCEL_AFTER_FILL))
def test_a_cancel_that_loses_the_race_to_a_fill_is_one_request_and_the_fill_stays_visible(
    store: TokenStore, broker: FakeBroker, answer: str
) -> None:
    """Тейк исполнился между решением и снятием: одна отмена, без повтора, исполнение видно.

    200 на отмену — «запрос принят», а не «снята»: вызывающий узнаёт правду
    статусом по ключу, и там «исполнена» (план §2.7, §8 №4). Живых заявок
    у брокера не больше одной в любой момент. Мутации: отмена в `REPEATABLE`;
    «исход неизвестен» → отказ; ключ отменяемой заявки вместо своего.
    """
    reply, expected = CANCEL_AFTER_FILL[answer]
    broker.cancel_answers.append(reply)
    journal = Journal(broker)

    async def scenario() -> tuple[
        CommandAccepted, CommandAccepted | BaseException, OrderState | None
    ]:
        async with open_session(store, broker) as session:
            take = await session.create_order(TAKE, record=journal)
            broker.execute(str(take.key), 1, 231_450.0)
            try:
                outcome: CommandAccepted | BaseException = await session.cancel_order(
                    take.key, record=journal
                )
            except BrokerError as error:
                outcome = error
            return take, outcome, await session.order_status(take.key)

    take, outcome, state = run(scenario())
    if expected is None:
        assert isinstance(outcome, CommandAccepted), f"{answer}: вышло {outcome!r}"
        assert outcome.key != take.key, "у отмены ключ отменяемой заявки, а не свой"
    else:
        assert type(outcome) is expected, f"{answer}: {type(outcome).__name__}"
    assert [kind for kind, _ in broker.commands] == ["create", "cancel"], (
        f"{answer}: ушло {broker.commands} — повтор команды"
    )
    assert broker.peak_live <= 1 and broker.live() == []
    assert state is not None and state.latest.status is OrderStatus.FILLED
    assert all(json.loads(frame)["data"]["orderStatus"] != "4" for frame in broker.frames), (
        "подделка отменила исполненную заявку — гонка не воспроизведена"
    )


def test_a_cancel_of_a_live_order_is_answered_by_event_four_on_the_original(
    store: TokenStore, broker: FakeBroker
) -> None:
    """Обратный случай гонки: заявка жива — отмена снимает её, событие `4` по исходной.

    Проверяет, что подделка в тесте выше молчит о `4` из-за исполнения,
    а не потому что отменять не умеет; и что `orderId` отмены — наш ключ.
    """
    journal = Journal(broker)

    async def scenario() -> tuple[CommandAccepted, list[OrderEvent]]:
        async with open_session(store, broker) as session, order_events(session) as events:
            take = await session.create_order(TAKE, record=journal)
            await session.cancel_order(take.key, record=journal)
            return take, [await next_event(events), await next_event(events)]

    take, seen = run(scenario())
    assert [event.report.status for event in seen] == [OrderStatus.NEW, OrderStatus.CANCELLED]
    assert {event.key for event in seen} == {str(take.key)}
    assert broker.live() == [] and len(broker.commands) == 2


# --- 429 на чтении статуса ---


def test_a_429_on_the_status_query_is_waited_out_and_asked_again(
    store: TokenStore, broker: FakeBroker
) -> None:
    """429 на `GET` статуса — пауза и повтор чтения; на команде — ни одного повтора.

    План §8 №6: создание — остановка, `GET` — повтор с паузой. Без повтора
    статуса исполнитель, потерявший ответ на подаче, остался бы без ответа
    на единственный вопрос, который ему можно задать. Мутации: статус вне
    `REPEATABLE`; повтор без паузы.
    """
    sleeps = Sleeps()
    journal = Journal(broker)
    broker.create_answers.append(ANSWERS["принята, ответ 429"])
    throttled = ANSWERS["не принята, 429"].reply
    broker.status_answers.extend([throttled, throttled])

    async def scenario() -> OrderState | None:
        async with open_session(store, broker, sleeps) as session:
            with pytest.raises(OutcomeUnknown):
                await session.create_order(MARKET_BUY, record=journal)
            assert sleeps.asked == [], "между отправкой команды и отказом была пауза — повтор?"
            return await session.order_status(journal.entries[0][0])

    state = run(scenario())
    asked = [r for r in broker.requests if r.method == "GET"]
    assert len(asked) == 3, f"статус спрошен {len(asked)} раз вместо трёх"
    assert len(sleeps.asked) == 2 and all(pause > 0 for pause in sleeps.asked)
    assert state is not None and state.latest.status is OrderStatus.NEW
    assert len(broker.commands) == 1 and broker.peak_live == 1


# --- обрыв потока статусов ---


def test_a_dropped_stream_reaches_the_consumer_and_the_lost_fill_is_found_by_key(
    store: TokenStore, broker: FakeBroker, tmp_path: pathlib.Path
) -> None:
    """Обрыв — `NoConnection` потребителю; исполнение за обрыв находится статусом по ключу.

    План §8 №7, §5: повтора событий после переподключения нет — новый сокет
    молчит о прошлом, а исполнение, пришедшее в пустоту, добирается `GET`
    статуса; повтор того же `executionId` из потока второй строки не даёт.
    Слой сам ничего не подаёт заново. Заголовок рукопожатия стёрт у обоих
    подключений. Мутации: итерация кончается молча на обрыве; переподключение
    внутри; `headers.clear()` убран.
    """
    journal = Journal(broker)

    async def scenario() -> tuple[CommandAccepted, BaseException, OrderState | None, int]:
        async with open_session(store, broker) as session:
            async with order_events(session) as events:
                accepted = await session.create_order(MARKET_BUY, record=journal)
                await next_event(events)
                broker.drop_stream()
                try:
                    async for _ in events:
                        pass
                except NoConnection as error:
                    dropped: BaseException = error
                else:
                    raise AssertionError("поток кончился молча: обрыв не дошёл до потребителя")
            broker.execute(str(accepted.key), 1, 230_900.0)
            async with order_events(session) as events:
                pending = broker.sockets[-1].inbox.qsize()
            return accepted, dropped, await session.order_status(accepted.key), pending

    accepted, dropped, state, pending = run(scenario())
    assert isinstance(dropped, NoConnection)
    assert len(broker.lost) == 1, "исполнение за обрыв не ушло в пустоту — сценарий не тот"
    assert pending == 0, "после переподключения подделка повторила прошлое — сценарий не тот"
    assert state is not None and state.latest.status is OrderStatus.FILLED
    assert state.latest.execution_id is not None
    assert len(broker.commands) == 1, "после обрыва слой подал команду заново"
    assert len(broker.handshakes) == 2, "слой переподключился сам или не подключился вовсе"
    for shake in broker.handshakes:
        assert shake.at_handshake == {"Authorization": "Bearer " + FAKE_ACCESS}
        assert shake.headers == {}, "заголовок с рабочим токеном не стёрт после подключения"

    lost = json.loads(broker.lost[0])["data"]
    with CandleStore(tmp_path / "executions.sqlite3") as journal_db:
        from_status = state.latest
        written = [
            journal_db.record_broker_execution(
                BrokerExecution(
                    execution_id=str(execution_id),
                    client_order_id=str(accepted.key),
                    volume=1,
                    price=230_900.0,
                    at=from_status.transaction_time or START,
                )
            )
            for execution_id in (from_status.execution_id, lost["executionId"])
        ]
    assert written == [True, False], "одно исполнение из статуса и из потока легло дважды"

