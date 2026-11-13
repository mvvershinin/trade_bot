"""Живые ответы БКС первого боевого дня, 13.11.2026: сторожа по ним.

Вход — ответы настоящего брокера на счёте владельца, снятые координатором
(`B-094`, `B-095`, `B-096`, `B-090`). Номера счёта, договора и кода клиента
обезличены. Адреса здесь — **со слов живого сервера**, а не из
`broker/session.py`: сверять код с самим собой нечем (сторожу нужен вход
извне).

Что стережётся, словами:

* снятие заявки уходит туда, где живой брокер ответил 200 (`B-094`);
* список заявок — туда, где живой брокер ответил 200, с датами
  в миллисекундах, и разбирается по живым полям: `side` и `orderStatus`
  числами, биржевой `orderId`, номер заявки без потери разрядов (`B-095`);
* статус по ключу с `data` объектом — ответ о заявке, а не отказ «нет
  сообщений» (09:55:13, «200 без data»);
* портфель с позицией строкой на каждый срок `term` — один контракт, а не
  четыре: в окне, в сводке счёта и в размере счёта (`B-096`, `B-090`);
* отказ по чужому активу (бумага без строки `T0`, чужой фьючерс с разными
  сроками) не роняет чтение своего тикера, номера счёта и снимка счёта;
* у фьючерса количество на разных сроках расходится — громкий отказ со
  сроками и числами, а не тихий `T0`; у бумаг сроки законно различаются.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest

from app.live_trading import LiveDesk, Seat
from broker.account import (
    Account,
    AccountSnapshot,
    parse_equity,
    parse_money,
    parse_positions,
    portfolio_account,
    read_holdings,
)
from broker.errors import UnexpectedAnswer
from broker.orders import (
    CommandAccepted,
    ExecutionType,
    ListedOrderStatus,
    OrderCommand,
    OrderSide,
    OrdersPage,
    OrdersQuery,
    OrderState,
    OrderStatus,
    parse_order_state,
)
from broker.secret import Secret
from broker.session import PORTFOLIO_PATH, BrokerSession
from broker.token_store import TokenStore
from broker.tokens import TokenScope
from tests.test_broker_orders import Wire, open_session, run
from tests.test_broker_session import FAKE_REFRESH, GATEWAY_404, START

#: Адреса, на которых живой брокер ответил 200 (13.11.2026). Литералы — из
#: записи живого прогона, а не из кода и не из снимка сайта.
LIVE_CANCEL = ("POST", "/trade-api-bff-operations/api/v1/orders/cancel")
LIVE_ORDERS_SEARCH = ("POST", "/trade-api-bff-order-details/api/v1/orders/search")

#: Что живой список принял (`yyyy-MM-dd'T'HH:mm:ss.SSSX`); прочее — 422.
LIVE_DATE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")

#: Ответ живого статуса по ключу: продажа FOK исполнена (13.11.2026, 07:03:58 UTC).
#: `data` — **объект**. Ключ и код клиента обезличены.
LIVE_STATUS_FILLED: dict[str, Any] = {
    "clientOrderId": "be77b937-aeb2-4d95-a8b6-3e7bdcb1b3b2",
    "data": {
        "messageType": "8", "orderStatus": "2", "executionType": "11",
        "orderQuantity": 1, "executedQuantity": 1, "lastQuantity": 1,
        "remainedQuantity": 0, "ticker": "MXZ6", "classCode": "SPBFUT", "side": "2",
        "orderType": "2", "averagePrice": 250800,
        "orderId": "261113-SPBFUT-1984994564062306578",
        "executionId": "SPBFUT-2MdIMRrCN2z-S-1-1-N", "price": 237450, "currency": "RUB",
        "clientCode": "CLIENT-1", "transactionTime": "2026-11-13T07:03:58.584Z",
        "tradeDate": "2026-11-13", "orderNumber": "1984994564062306578",
        "accruedCoupon": 0, "executionValue": 250800, "commission": 16.58,
        "securityExchange": "SPBFUT", "rejectReason": "", "timeInForce": "2",
    },
    "originalClientOrderId": "",
}

#: Строки живого списка заявок за 13.11.2026 — поля дословно из ответа,
#: значения по записи прогона: продажа FOK исполнена, тейк снят, покупка
#: FOK исполнена. `side` и `orderStatus` — числа.
LIVE_ORDER_ROWS: list[dict[str, Any]] = [
    {
        "averagePrice": 250800.0, "calculationVolume": 250800.0, "classCode": "SPBFUT",
        "clientCode": "CLIENT-1", "contractSum": 250800.0, "executedQuantity": 1.0,
        "executedQuantityLots": 1.0, "executedValue": 250800.0,
        "executionDateTime": "2026-11-13T07:03:58.584Z", "expiryDate": None,
        "linkedOrder": None, "offsetType": None,
        "orderDateTime": "2026-11-13T07:03:58.281Z", "orderId": "261113-SPBFUT-1984994564062306578",
        "orderNum": 1984994564062306578, "orderQuantity": 1.0, "orderQuantityLots": 1.0,
        "orderStatus": 2, "orderType": 2, "price": 237450.0, "rejectReason": "",
        "remainedQuantity": 0.0, "remainedQuantityLots": 0.0, "settlementCurrency": "RUB",
        "side": 2, "spreadType": None, "stopOrder": None, "ticker": "MXZ6",
        "tradeDate": "2026-11-13", "updateDateTime": "2026-11-13T07:03:58.584Z",
    },
    {
        "averagePrice": 0.0, "classCode": "SPBFUT", "clientCode": "CLIENT-1",
        "executedQuantity": 0.0, "orderDateTime": "2026-11-13T07:04:01.000Z",
        "orderId": "261113-SPBFUT-1984994564062258802", "orderNum": "1984994564062258802",
        "orderQuantity": 1.0, "orderStatus": 1, "orderType": 2, "price": 252300.0,
        "remainedQuantity": 1.0, "side": 2, "ticker": "MXZ6",
        "updateDateTime": "2026-11-13T07:00:30.000Z",
    },
    {
        "averagePrice": 251025.0, "classCode": "SPBFUT", "clientCode": "CLIENT-1",
        "executedQuantity": 1.0, "orderDateTime": "2026-11-13T07:00:20.000Z",
        "orderId": "261113-SPBFUT-1984994564062400001", "orderNum": 1984994564062400001,
        "orderQuantity": 1.0, "orderStatus": 2, "orderType": 2, "price": 264900.0,
        "remainedQuantity": 0.0, "side": 1, "ticker": "MXZ6",
        "updateDateTime": "2026-11-13T07:00:20.300Z",
    },
]


def holding_row(ticker: str, term: str, quantity: float, **extra: object) -> dict[str, Any]:
    """Строка живого портфеля 13.11.2026 (`futuresHolding`), обезличенная."""
    return {
        "type": "futuresHolding", "ticker": ticker, "board": "SPBFUT", "term": term,
        "quantity": quantity, "locked": 0.0, "balancePrice": 250800.0,
        "currentPrice": 251000.0, "unrealizedPL": 200.0, "minimumStep": 25.0,
        "lockedForFutures": 30000.0, "expireDate": "2026-12-17",
        "agreementId": "AGREEMENT-1", "account": "ACCOUNT-1", **extra,
    }


TERMS = ("T0", "T1", "T2", "T365")


def live_portfolio(contracts: float) -> list[dict[str, Any]]:
    """Живой вид портфеля: деньги, бумаги и фьючерс — каждый строкой на каждый срок."""
    rows: list[dict[str, Any]] = []
    for term in TERMS:
        rows.append({
            "type": "moneyLimit", "ticker": "RUB", "currency": "RUB", "term": term,
            "quantity": 500000.0, "currentValueRub": 500000.0,
            "agreementId": "AGREEMENT-1", "account": "ACCOUNT-1",
        })
        rows.append({
            "type": "depoLimit", "ticker": "LKOH", "board": "TQBR", "term": term,
            "quantity": 10.0, "currentValueRub": 70000.0,
            "agreementId": "AGREEMENT-1", "account": "ACCOUNT-1",
        })
        rows.append(holding_row("MXZ6", term, contracts, currentValueRub=0.0))
    return rows


@pytest.fixture
def store(tmp_path: Path) -> TokenStore:
    directory = tmp_path / "userdata"
    directory.mkdir()
    keeper = TokenStore(directory)
    keeper.save(Secret(FAKE_REFRESH), TokenScope.TRADE, issued_at=START - timedelta(days=1))
    return keeper


def gateway_404() -> httpx.Response:
    """Ответ шлюза на неверный адрес — как 13.11: страница HTML, не JSON."""
    return httpx.Response(404, text=GATEWAY_404, headers={"content-type": "text/html"})


@dataclass
class Record:
    """Журнал команд вместо `app/`: только помнит, что записано."""

    entries: list[tuple[UUID, OrderCommand]] = field(default_factory=list)

    async def __call__(self, key: UUID, request: OrderCommand) -> None:
        self.entries.append((key, request))


# --- B-094: снятие заявки ---


def test_a_cancel_goes_where_the_live_broker_answered_ok(store: TokenStore) -> None:
    """Стережёт: снятие заявки уходит на адрес, где живой брокер 13.11 ответил 200.

    На адресе снимка сайта шлюз отвечал 404 страницей HTML, тейк перед
    переворотом не снимался, и исполнитель остановился с позицией и тейком
    у брокера. Здесь любой другой адрес — та же страница шлюза.
    """

    def reply(request: httpx.Request) -> httpx.Response:
        if (request.method, request.url.path) != LIVE_CANCEL:
            return gateway_404()
        key = json.loads(request.content)["clientOrderId"]
        return httpx.Response(200, json={"clientOrderId": key, "status": "OK"})

    wire = Wire(reply)
    journal = Record()

    async def scenario() -> CommandAccepted:
        async with open_session(store, wire) as broker:
            return await broker.cancel_order(uuid4(), record=journal)

    accepted = run(scenario())
    assert len(wire.to(LIVE_CANCEL)) == 1, "снятие ушло не туда, где живой брокер отвечает"
    assert accepted.key == journal.entries[0][0]


# --- B-095: список заявок ---


def test_the_orders_list_is_asked_where_and_how_the_live_broker_answers(
    store: TokenStore,
) -> None:
    """Стережёт: адрес списка, даты с миллисекундами и разбор живых строк.

    Подставной брокер ведёт себя как живой 13.11: чужой адрес — 404 HTML,
    дата без миллисекунд — 422 `INVALID_FORMAT`, иначе — три строки дня.
    Строки читаются по живым полям: `side` и `orderStatus` числами,
    биржевой `orderId`, `orderNum` — все 19 цифр (через `float` последние
    разряды молча округлились бы до чужого номера).
    """

    def reply(request: httpx.Request) -> httpx.Response:
        if (request.method, request.url.path) != LIVE_ORDERS_SEARCH:
            return gateway_404()
        body = json.loads(request.content)
        for name in ("startDateTime", "endDateTime"):
            if name in body and not LIVE_DATE.fullmatch(body[name]):
                return httpx.Response(422, json={"type": "VALIDATION_ERROR"})
        return httpx.Response(
            200, json={"records": LIVE_ORDER_ROWS, "totalRecords": 3, "totalPages": 1}
        )

    wire = Wire(reply)
    query = OrdersQuery(
        start=datetime(2026, 11, 12, 19, 0, tzinfo=timezone.utc),
        end=datetime(2026, 11, 13, 18, 59, 59, 999999, tzinfo=timezone.utc),
        tickers=("MXZ6",),
        size=100,
    )

    async def scenario() -> OrdersPage:
        async with open_session(store, wire) as broker:
            return await broker.search_orders(query)

    page = run(scenario())
    sent = json.loads(wire.to(LIVE_ORDERS_SEARCH)[0].content)
    assert sent["startDateTime"] == "2026-11-12T19:00:00.000Z"
    assert sent["endDateTime"] == "2026-11-13T18:59:59.999Z"
    assert [row.status for row in page.records] == [
        ListedOrderStatus.EXECUTED, ListedOrderStatus.REMOVED, ListedOrderStatus.EXECUTED,
    ]
    assert [row.side for row in page.records] == [OrderSide.SELL, OrderSide.SELL, OrderSide.BUY]
    assert page.records[0].exchange_order_id == "261113-SPBFUT-1984994564062306578"
    assert [row.order_num for row in page.records] == [
        1984994564062306578, 1984994564062258802, 1984994564062400001,
    ]
    assert page.records[0].average_price == 250800.0
    assert page.records[1].executed_quantity == 0


@pytest.mark.parametrize("key", ["records", "content", "data"])
def test_the_orders_list_rows_are_found_under_any_known_key(
    store: TokenStore, key: str
) -> None:
    """Стережёт: массив строк читается под `records`, `content` или `data`.

    Ключ массива в живом ответе не записан; пустая страница вместо
    непрочитанной значила бы «у брокера ничего не висит», и живой тейк
    был бы забыт. Без всех трёх ключей — отказ (сторож в `test_broker_orders`).
    """
    wire = Wire(lambda request: httpx.Response(200, json={key: LIVE_ORDER_ROWS[:1]}))

    async def scenario() -> OrdersPage:
        async with open_session(store, wire) as broker:
            return await broker.search_orders(OrdersQuery())

    page = run(scenario())
    assert len(page.records) == 1
    assert page.records[0].status is ListedOrderStatus.EXECUTED


# --- статус по ключу: «200 без data» ---


def test_a_live_status_answer_with_data_as_an_object_is_a_report(store: TokenStore) -> None:
    """Стережёт: `data` объектом (живой вид 13.11) — сообщение о заявке, не отказ.

    До правки объект отвергался тем же текстом, что отсутствие поля, — «нет
    сообщений о заявке (data)», и 13.11 в 09:55 это прочитали как «200 без
    `data`». Из ответа обязаны дойти состояние, исполненное, средняя цена
    и комиссия брокера.
    """
    wire = Wire(lambda request: httpx.Response(200, json=LIVE_STATUS_FILLED))

    async def scenario() -> OrderState | None:
        async with open_session(store, wire) as broker:
            return await broker.order_status(UUID(LIVE_STATUS_FILLED["clientOrderId"]))

    state = run(scenario())
    assert state is not None
    assert len(state.reports) == 1
    latest = state.latest
    assert latest.status is OrderStatus.FILLED
    assert latest.execution_type is ExecutionType.TRADE
    assert latest.executed_quantity == 1
    assert latest.average_price == 250800.0
    assert latest.commission == 16.58
    assert latest.exchange_order_id == "261113-SPBFUT-1984994564062306578"


def test_a_live_cancelled_take_reads_as_cancelled(store: TokenStore) -> None:
    """Стережёт: снятый тейк (живьём `orderStatus` 4, `executionType` 0) — «отменена»."""
    answer = copy.deepcopy(LIVE_STATUS_FILLED)
    answer["data"].update(
        orderStatus="4", executionType="0", executedQuantity=0, lastQuantity=0,
        remainedQuantity=1, executionId="", averagePrice=0, price=252300,
    )
    wire = Wire(lambda request: httpx.Response(200, json=answer))

    async def scenario() -> OrderState | None:
        async with open_session(store, wire) as broker:
            return await broker.order_status(uuid4())

    state = run(scenario())
    assert state is not None
    assert state.latest.status is OrderStatus.CANCELLED


def test_a_status_answer_without_data_says_so_and_not_what_an_object_would() -> None:
    """Стережёт: «поля нет» и «поле не той формы» — разные технические тексты.

    Один текст на оба случая 13.11 увёл разбор к выводу «брокер не прислал
    `data`», когда брокер прислал её объектом. Отсутствие остаётся отказом:
    страница 11 называет `data` обязательным, «заявки нет» — это 404.
    """
    with pytest.raises(UnexpectedAnswer) as absent:
        parse_order_state({"clientOrderId": "x", "originalClientOrderId": ""})
    with pytest.raises(UnexpectedAnswer) as odd:
        parse_order_state({"clientOrderId": "x", "data": 7})
    assert "нет поля data" in absent.value.technical
    assert "нет поля data" not in odd.value.technical


# --- B-096, B-090: позиция строкой на каждый срок ---


def test_one_contract_on_four_terms_is_one_contract() -> None:
    """Стережёт: четыре строки сроков `T0`–`T365` по одному контракту — лонг 1.

    Сложение строк по тикеру дало в окне «лонг 4» при одном контракте
    у брокера. Сверка и исполнитель складывают строки так же, поэтому правило
    живёт в разборе: строк одного актива на выходе одна.
    """
    positions, account = parse_positions(live_portfolio(1.0))
    futures = [one for one in positions if one.ticker == "MXZ6"]
    assert len(futures) == 1
    assert futures[0].quantity == 1.0
    assert sum(one.quantity for one in positions if one.ticker == "MXZ6") == 1.0
    assert [one.ticker for one in positions].count("LKOH") == 1
    assert account == "AGREEMENT-1"


def test_a_closed_position_on_four_terms_is_flat() -> None:
    """Стережёт: после закрытия все сроки `0.0` — позиции нет, а не отказ."""
    positions, _ = parse_positions(live_portfolio(0.0))
    futures = [one for one in positions if one.ticker == "MXZ6"]
    assert len(futures) == 1
    assert not futures[0].open


def test_the_account_summary_names_each_holding_once() -> None:
    """Стережёт: строка «Брокер ответил про счёт» не повторяет бумагу по срокам (`B-090`)."""
    positions, account = parse_positions(live_portfolio(1.0))
    snapshot = AccountSnapshot(
        taken_at=START, account=account, money=parse_money({}), positions=positions
    )
    summary = snapshot.human_summary()
    assert summary.count("MXZ6") == 1, summary
    assert summary.count("LKOH") == 1, summary
    assert "MXZ6: лонг, 1 шт." in summary, summary


def test_the_account_size_counts_each_holding_once() -> None:
    """Стережёт: размер счёта — по строкам `T0`, а не вчетверо.

    От размера счёта считается дневной лимит убытка: вчетверо больший
    размер — вчетверо более слабый предохранитель.
    """
    assert parse_equity(live_portfolio(1.0)) == 500000.0 + 70000.0 + 0.0


@pytest.mark.parametrize(
    "rows",
    [
        [holding_row("MXZ6", term, 1.0, currentValueRub=900.0) for term in ("T1", "T2", "T365")],
        [holding_row("MXZ6", "T0", 1.0, currentValueRub=900.0)] * 2,
        [{k: v for k, v in holding_row("MXZ6", "T0", 1.0, currentValueRub=900.0).items()
          if k != "term"}] * 2,
    ],
    ids=["сроки-без-T0", "два-T0", "две-строки-без-срока"],
)
def test_a_holding_without_one_current_row_is_refused(rows: list[dict[str, Any]]) -> None:
    """Стережёт: нет ровно одной строки `T0` — отказ словами, а не «позиции нет».

    Пропуск актива читается как «позиции нет» — вход поверх открытой;
    сложение — снова «лонг 4». Отказ называет причину человеку.
    """
    with pytest.raises(UnexpectedAnswer) as caught:
        parse_positions(rows)
    assert "срока расчётов" in caught.value.human
    assert parse_equity(rows) is None


def test_the_window_reads_one_contract_from_the_live_portfolio() -> None:
    """Стережёт: «позиция у брокера» боевой стороны окна — 1, а не 4 (`B-096`).

    `LiveDesk.holding` — то, что окно показало 13.11 «лонг 4». Сессия здесь —
    подставная: отдаёт живой портфель, больше ничего не умеет.
    """

    class Portfolio:
        async def read(self, path: str) -> object:
            return live_portfolio(1.0)

    seat = Seat(session=cast(BrokerSession, Portfolio()), ticker="MXZ6", account="AGREEMENT-1")
    desk = LiveDesk(
        cast(Any, SimpleNamespace(links=0)), book=cast(Any, None), seat=seat, day=None, notes=()
    )
    signed, average = run(desk.holding())
    assert signed == 1
    assert average == 250800.0


# --- ревью 13.11.2026: отказ по активу, а не по портфелю; сроки фьючерса ---


def without_current_row(rows: list[dict[str, Any]], ticker: str) -> list[dict[str, Any]]:
    """Тот же портфель, но у `ticker` нет строки `T0` — бумага, купленная сегодня."""
    return [row for row in rows if not (row["ticker"] == ticker and row["term"] == "T0")]


def with_terms(ticker: str, amounts: dict[str, float]) -> list[dict[str, Any]]:
    """Фьючерс строками сроков с заданными количествами."""
    return [holding_row(ticker, term, amount) for term, amount in amounts.items()]


#: Чужие активы, позицию которых прочитать нельзя: бумага без строки `T0`
#: и чужой фьючерс, у которого сроки разошлись.
FOREIGN_BREAKERS = {
    "бумага-без-T0": without_current_row(live_portfolio(1.0), "LKOH"),
    "чужой-фьючерс-сроки-разошлись": live_portfolio(1.0)
    + with_terms("SiZ6", {"T0": 0.0, "T1": 1.0, "T2": 1.0, "T365": 1.0}),
}


class PortfolioSession:
    """Подставная сессия: отдаёт портфель и пустые лимиты, больше ничего не умеет."""

    def __init__(self, portfolio: object) -> None:
        self.portfolio = portfolio

    async def read(self, path: str) -> object:
        return self.portfolio if path == PORTFOLIO_PATH else {}

    def stored(self) -> SimpleNamespace:
        return SimpleNamespace(account=None)


def desk_on(portfolio: object) -> LiveDesk:
    """Боевая сторона окна, у которой есть только сессия с этим портфелем."""
    seat = Seat(
        session=cast(BrokerSession, PortfolioSession(portfolio)), ticker="MXZ6",
        account="AGREEMENT-1",
    )
    return LiveDesk(
        cast(Any, SimpleNamespace(links=0)), book=cast(Any, None), seat=seat, day=None, notes=()
    )


@pytest.mark.parametrize("name", sorted(FOREIGN_BREAKERS))
def test_a_foreign_unreadable_asset_does_not_block_reading_ours(name: str) -> None:
    """Стережёт: отказ по чужому активу не роняет позицию своего тикера и номер счёта.

    Ревью 13.11.2026: отказ на весь портфель останавливал робота перед
    заявкой и гасил обе ручные кнопки из-за чужой облигации. Отказ получает
    только тот, кто спросит непрочитанный тикер.
    """
    portfolio = FOREIGN_BREAKERS[name]
    holdings = read_holdings(portfolio)
    ours = holdings.position("MXZ6")
    assert ours is not None
    assert ours.quantity == 1.0
    assert len(holdings.unreadable) == 1
    with pytest.raises(UnexpectedAnswer):
        holdings.position(holdings.unreadable[0].ticker)
    assert portfolio_account(portfolio) == "AGREEMENT-1"
    assert run(desk_on(portfolio).holding()) == (1, 250800.0)


def test_parse_positions_still_refuses_the_whole_portfolio_on_any_unreadable_asset() -> None:
    """Стережёт: «все позиции» без непрочитанной не отдаются — её отсутствие читалось бы «нет»."""
    with pytest.raises(UnexpectedAnswer):
        parse_positions(FOREIGN_BREAKERS["бумага-без-T0"])


def test_the_account_snapshot_survives_a_foreign_unreadable_asset() -> None:
    """Стережёт: снимок счёта обновляется при чужом непрочитанном активе и называет его.

    Размер счёта — на срок `T0`: бумага без строки `T0` в этот срез ещё
    не пришла и в сумму не входит; отказа из-за неё нет.
    """
    portfolio = FOREIGN_BREAKERS["бумага-без-T0"]
    account = Account(cast(BrokerSession, PortfolioSession(portfolio)))
    snapshot = run(account.snapshot())
    assert snapshot.account == "AGREEMENT-1"
    ours = snapshot.position("MXZ6")
    assert ours is not None
    assert ours.quantity == 1.0
    assert "не прочитаны позиции: LKOH" in snapshot.human_summary()
    assert snapshot.equity_rub == 500000.0 + 0.0
    assert snapshot.margin_per_contract("LKOH") is None


def test_futures_terms_that_disagree_stop_loudly_with_terms_and_numbers() -> None:
    """Стережёт: у фьючерса T0 0 при T1/T365 1 — громкий отказ со сроками и числами.

    Вечерняя сессия FORTS относится к следующему торговому дню: после
    вечернего входа `T0` может ещё стоять на нуле. Тихий `T0` — это «позиции
    нет», снятый тейк и вход поверх открытой позиции.
    """
    portfolio = [
        row for row in live_portfolio(1.0) if row["ticker"] != "MXZ6"
    ] + with_terms("MXZ6", {"T0": 0.0, "T1": 1.0, "T2": 1.0, "T365": 1.0})
    with pytest.raises(UnexpectedAnswer) as caught:
        read_holdings(portfolio).position("MXZ6")
    words = caught.value.human
    for shown in ("MXZ6", "T0 — 0", "T1 — 1", "T365 — 1"):
        assert shown in words, words
    with pytest.raises(UnexpectedAnswer):
        run(desk_on(portfolio).holding())


def test_stock_terms_may_disagree_and_the_current_term_is_read() -> None:
    """Стережёт: у бумаг сроки законно различаются — читается `T0`, отказа нет."""
    rows = [
        {"type": "depoLimit", "ticker": "LKOH", "board": "TQBR", "term": term,
         "quantity": amount, "agreementId": "AGREEMENT-1", "account": "ACCOUNT-1"}
        for term, amount in (("T0", 10.0), ("T1", 15.0), ("T2", 15.0), ("T365", 15.0))
    ]
    position = read_holdings(rows).position("LKOH")
    assert position is not None
    assert position.quantity == 10.0


def test_a_paper_without_current_row_does_not_stop_the_executor_reading_ours() -> None:
    """Стережёт: бумага без `T0` на договоре не мешает исполнителю прочитать MXZ6.

    Чтение позиции перед заявкой (`_position_now`) — у исполнителя свой путь
    к портфелю; отказ по всему портфелю из-за чужой бумаги останавливал бы
    робота перед каждой заявкой. Своя непрочитанная позиция — отказ.
    """
    from app.live_executor import LiveExecutor

    # Сама функция чтения, без сборки исполнителя: ей нужны сессия и тикер.
    position_now = vars(LiveExecutor)["_position_now"]

    def reader(portfolio: object) -> LiveExecutor:
        return cast(LiveExecutor, SimpleNamespace(
            _session=PortfolioSession(portfolio), _instrument=SimpleNamespace(ticker="MXZ6")
        ))

    portfolio = FOREIGN_BREAKERS["бумага-без-T0"]
    assert run(position_now(reader(portfolio))) == 1
    ours_broken = [row for row in live_portfolio(1.0) if row["ticker"] != "MXZ6"] + with_terms(
        "MXZ6", {"T0": 0.0, "T1": 1.0, "T2": 1.0, "T365": 1.0}
    )
    with pytest.raises(UnexpectedAnswer):
        run(position_now(reader(ours_broken)))
