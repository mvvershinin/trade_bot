"""Боевой исполнитель против подставного брокера: Э4, шаг 2, под-шаг 2б.

План `.docs/plans/E4-live-orders.md` §8 (№1, 2, 6, 7, 9, 13, 14, 15, 17) и §12
«Что ломает test-expert»; постановка — решение 0066. Брокер — `tests/fake_broker.py`:
одна книга заявок на HTTP и на потоке статусов, исход каждой заявки задаёт тест.
Журнал команд — настоящая база во временной папке, запись через поток базы
(`StoreBook` поверх `MarketWorker`), как в продукте.

Каждый тест называет **поведение**, которое стережёт, в своём докстринге —
его и ломает мутацией test-expert. Счёт ведётся по книге подделки (что ушло
к брокеру и что у него живо), а не по вызовам исполнителя.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import pathlib
import sqlite3
from collections import Counter
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta
from typing import TypeVar

import httpx
import pytest
import websockets

from app import live_executor
from app.live_executor import Instrument, LiveExecutor, LiveLimits, StoreBook, Wiring
from app.reconcile import reconcile
from broker.secret import Secret
from broker.session import PORTFOLIO_PATH, BrokerSession
from broker.throttle import Throttle
from broker.token_store import TokenStore
from broker.tokens import TokenScope
from engine import Engine, EngineSettings, Mode, TradingWindow, is_take_order
from engine.contracts import ExitReason, Fill, LevelTouch, OrderAction, OrderRequest, Side
from engine.ports import ExecutionHalted, ExecutionRefused, Refusal
from market import MSK, MarketWorker
from market.candles import M5, MINUTE, Candle
from market.commands import (
    BrokerCommand,
    BrokerExecution,
    CommandAction,
    CommandSide,
    CommandStatus,
    StoredCommand,
)
from market.journal import RunOrigin, SessionRecord
from strategies import Intent
from tests.engine_helpers import ScriptedStrategy, candle, decision
from tests.fake_broker import (
    ACCEPT,
    ANSWERS,
    CANCEL,
    CREATE,
    STATUS,
    Answer,
    BookOrder,
    FakeBroker,
)
from tests.test_broker_session import FAKE_ACCESS, FAKE_REFRESH, START, Clock, no_sleep

TICKER = "MXZ6"
ACCOUNT = "FAKE-ACCOUNT-1"
#: Свеча решения закрывается в 10:00 — заявка подана в 10:00, исполняется в свече 10:00.
BAR0 = datetime(2026, 10, 27, 10, 0, tzinfo=MSK)
BAR1 = BAR0 + timedelta(minutes=5)
BAR2 = BAR1 + timedelta(minutes=5)
NOW = BAR0 + timedelta(seconds=5)

#: Пределы в доли секунды: тест не ждёт боевых десятков секунд.
FAST = LiveLimits(
    stale_after=60.0,
    event_wait=0.2,
    fill_wait=0.6,
    cancel_wait=0.4,
    limit_exit_bars=2,
    position_settle=0.0,
    reconnect_pauses=(0.01,),
)

_Result = TypeVar("_Result")


def run(coroutine: Coroutine[object, object, _Result]) -> _Result:
    return asyncio.run(coroutine)


# --- заявки движка ---


def take_name(side: Side = Side.LONG) -> str:
    return f"take:{side.value}:{BAR0.isoformat()}"


def entry(side: Side = Side.LONG, volume: int = 1, at: datetime = BAR0) -> OrderRequest:
    return OrderRequest(
        action=OrderAction.OPEN, side=side, volume=volume, submitted_at=at,
        reason="сигнал", order_id=f"open:{side.value}:{at.isoformat()}",
    )


def leave(
    side: Side = Side.LONG, volume: int = 1, at: datetime = BAR1, limit: float | None = None
) -> OrderRequest:
    return OrderRequest(
        action=OrderAction.CLOSE, side=side, volume=volume, submitted_at=at,
        reason="сигнал", order_id=f"close:{side.value}:{at.isoformat()}",
        exit_reason=ExitReason.SIGNAL if limit is None else ExitReason.WINDOW_END,
        limit_price=limit,
    )


def arm(
    level: float, touch: LevelTouch, side: Side = Side.LONG, volume: int = 1,
    at: datetime = BAR0,
) -> OrderRequest:
    fixed = (side, touch) in ((Side.LONG, LevelTouch.RISE), (Side.SHORT, LevelTouch.FALL))
    return OrderRequest(
        action=OrderAction.ARM_TAKE_PROFIT, side=side, volume=volume, submitted_at=at,
        reason="уровень", order_id=take_name(side), price=level, touch=touch,
        exit_reason=ExitReason.TAKE_PROFIT if fixed else ExitReason.TRAILING_TAKE,
    )


def disarm(side: Side = Side.LONG, volume: int = 1, at: datetime = BAR1) -> OrderRequest:
    return OrderRequest(
        action=OrderAction.CANCEL_TAKE_PROFIT, side=side, volume=volume, submitted_at=at,
        reason="выход", order_id=f"cancel:{take_name(side)}:{at.isoformat()}",
        target_id=take_name(side),
    )


def bar(start: datetime, *, unsettled: bool = False) -> Candle:
    return Candle(
        time=start, open=100.0, high=120.0, low=80.0, close=100.0, volume=10.0,
        timeframe=M5, filled_minutes=5, unsettled=unsettled,
    )


def minute(start: datetime, low: float, high: float = 101.0) -> Candle:
    return Candle(
        time=start, open=100.0, high=high, low=low, close=100.0, volume=1.0, timeframe=MINUTE
    )


# --- биржа подделки ---


Plan = Callable[[FakeBroker, BookOrder], None]


def filled(*pieces: tuple[int, float]) -> Plan:
    """Биржа исполняет заявку кусками `(контрактов, цена)` сразу после приёма."""

    def act(broker: FakeBroker, order: BookOrder) -> None:
        for quantity, price in pieces:
            broker.execute(order.key, quantity, price)

    return act


def resting(broker: FakeBroker, order: BookOrder) -> None:
    """Биржа приняла и не исполняет: заявка живёт в книге."""


def rejected(reason: str) -> Plan:
    def act(broker: FakeBroker, order: BookOrder) -> None:
        broker.reject(order.key, reason)

    return act


class Exchange:
    """Что биржа делает с каждой следующей заявкой — планы по порядку; нет плана — живёт."""

    def __init__(self, broker: FakeBroker) -> None:
        self.broker = broker
        self.plans: list[Plan] = []
        broker.on_create = self

    def __call__(self, order: BookOrder) -> None:
        if self.plans:
            self.plans.pop(0)(self.broker, order)


# --- оснастка ---


class Moment:
    """Часы исполнителя, которыми управляет тест."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class JournalProbe(StoreBook):
    """Журнал в базе плюс: сколько команд ушло к брокеру к моменту каждой записи."""

    def __init__(self, worker: MarketWorker, session_id: int, broker: FakeBroker) -> None:
        super().__init__(worker, session_id)
        self.worker = worker
        self.session_id = session_id
        self.broker = broker
        self.sent_before: list[int] = []
        self.written: list[BrokerCommand] = []
        self.fail = False
        #: Смена статуса падает — сбой записи уже после отправки команды.
        self.fail_status = False

    async def record(self, command: BrokerCommand) -> None:
        self.sent_before.append(len(self.broker.commands))
        if self.fail:
            raise sqlite3.OperationalError("database is locked")
        await super().record(command)
        self.written.append(command)

    async def set_status(self, key: str, status: CommandStatus, reason: str) -> None:
        if self.fail_status:
            raise sqlite3.OperationalError("disk I/O error")
        await super().set_status(key, status, reason)

    async def rows(self) -> list[StoredCommand]:
        keys = [command.client_order_id for command in self.written]

        def read(store: object) -> list[StoredCommand]:
            found = [store.broker_command(key) for key in keys]  # type: ignore[attr-defined]
            return [row for row in found if row is not None]

        return await self.worker.call(read)

    async def executions(self, key: str) -> tuple[BrokerExecution, ...]:
        return await self.worker.call(lambda store: store.broker_executions(key))


@dataclass
class Rig:
    broker: FakeBroker
    exchange: Exchange
    executor: LiveExecutor
    book: JournalProbe
    clock: Moment
    minutes: list[Candle] = field(default_factory=list)
    #: Не пусто — чтение минуток для сторожа падает этим исключением.
    minute_faults: list[Exception] = field(default_factory=list)
    #: Что исполнитель сказал человеку без остановки (`Wiring.notice`).
    notices: list[str] = field(default_factory=list)
    session: BrokerSession | None = None

    def creates(self) -> list[dict[str, object]]:
        """Тела всех созданий заявок, ушедших к брокеру, по порядку."""
        return [
            json.loads(request.content)
            for request in self.broker.requests
            if (request.method, request.url.path) == CREATE
        ]

    def sells(self) -> list[dict[str, object]]:
        return [body for body in self.creates() if body["side"] == "2"]


@pytest.fixture
def store(tmp_path: pathlib.Path) -> TokenStore:
    directory = tmp_path / "userdata"
    directory.mkdir()
    keeper = TokenStore(directory)
    keeper.save(Secret(FAKE_REFRESH), TokenScope.TRADE, issued_at=START - timedelta(days=1))
    return keeper


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch) -> FakeBroker:
    fake = FakeBroker()
    monkeypatch.setattr(websockets, "connect", fake.connect)
    return fake


@contextlib.asynccontextmanager
async def make_rig(
    tmp_path: pathlib.Path,
    store: TokenStore,
    broker: FakeBroker,
    *,
    client: httpx.AsyncClient | None = None,
    sleep: Callable[[float], Coroutine[object, object, None]] | None = None,
    on_news: Callable[[], None] | None = None,
    limits: LiveLimits = FAST,
) -> AsyncIterator[Rig]:
    worker = MarketWorker(tmp_path / "candles.sqlite3")
    await worker.open()
    run_id = await worker.call(
        lambda base: base.open_journal_session(
            SessionRecord(RunOrigin.LIVE, symbol=TICKER), now=NOW
        ).id
    )
    book = JournalProbe(worker, run_id, broker)
    session = BrokerSession(
        store,
        client=client or broker.client(),
        clock=Clock(),
        sleep=no_sleep,
        attempts=3,
        throttle=Throttle(rate=1000.0),
        order_throttle=Throttle(rate=1000.0),
    )
    minutes: list[Candle] = []
    minute_faults: list[Exception] = []

    async def source(since: datetime) -> list[Candle]:
        if minute_faults:
            raise minute_faults[0]
        return [one for one in minutes if one.time >= since]

    clock = Moment(NOW)
    notices: list[str] = []
    wiring = Wiring(
        session, book, source, clock=clock, sleep=sleep or asyncio.sleep,
        notice=notices.append, on_news=on_news,
    )
    executor = LiveExecutor(wiring, Instrument(TICKER, "SPBFUT", 5, ACCOUNT), limits)
    exchange = Exchange(broker)
    await executor.start()
    try:
        assert await executor.wait_connected(2.0), "поток статусов не открылся"
        yield Rig(
            broker, exchange, executor, book, clock, minutes, minute_faults, notices, session
        )
    finally:
        await executor.close()
        await session.close()
        await worker.close()


async def long_open(rig: Rig, price: float = 100.0, volume: int = 1) -> None:
    """Лонг открыт и отдан движку: вход исполнен целиком по `price`."""
    rig.exchange.plans.append(filled((volume, price)))
    await rig.executor.submit(entry(volume=volume))
    fill = only_fill(await rig.executor.fills_at(bar(BAR0, unsettled=True)))
    assert (fill.action, fill.volume) == (OrderAction.OPEN, volume)


async def heard() -> None:
    """Дать потоку статусов прочитать то, что подделка уже послала: читает отдельная задача."""
    await asyncio.sleep(0.05)


def only_fill(fills: object) -> Fill:
    assert isinstance(fills, list) and len(fills) == 1, f"ожидалась одна сделка: {fills!r}"
    item = fills[0]
    assert isinstance(item, Fill)
    return item


# --- рыночные заявки: одна сделка на заявку (Р6) ---


def test_three_executions_of_one_order_are_one_deal_at_the_average_price(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Три исполнения одной заявки — движок видит ОДНУ сделку на весь объём со средней ценой.

    Ломается: отдать первое исполнение, взять цену последнего, отдать до
    полного исполнения. Строки исполнений лежат в таблице исполнений все три,
    с ценой каждого, — из них потом считается день (Р7).
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            rig.exchange.plans.append(filled((1, 100.0), (1, 101.0), (1, 105.0)))
            await rig.executor.submit(entry(volume=3))
            fill = only_fill(await rig.executor.fills_at(bar(BAR0, unsettled=True)))
            assert (fill.action, fill.side, fill.volume) == (OrderAction.OPEN, Side.LONG, 3.0)
            assert fill.price == pytest.approx(102.0)
            assert fill.order_id == entry().order_id
            assert fill.at == BAR0
            key = rig.book.written[0].client_order_id
            prices = sorted(one.price for one in await rig.book.executions(key))
            assert prices == pytest.approx([100.0, 101.0, 105.0])
            [row] = await rig.book.rows()
            assert row.status is CommandStatus.FILLED
            assert row.command.account == ACCOUNT
            assert await rig.executor.fills_at(bar(BAR0)) == []

    run(scenario())


def test_a_market_deal_is_not_given_by_a_candle_older_than_its_order(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Рыночная сделка — не раньше свечи, начавшейся не раньше подачи (как в модели).

    Вход подан в 10:00 — свеча 09:55 о нём не знает. Отдать сделку ею
    значило бы прожить позицию на свечу дольше, чем прогон.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            rig.exchange.plans.append(filled((1, 100.0)))
            await rig.executor.submit(entry())
            assert await rig.executor.fills_at(bar(BAR0 - timedelta(minutes=5))) == []
            fill = only_fill(await rig.executor.fills_at(bar(BAR0, unsettled=True)))
            assert fill.at == BAR0

    run(scenario())


def test_partial_execution_without_end_cancels_the_rest_and_halts(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Частичное без конца — остаток снят, остановка; частичная сделка движку НЕ отдаётся."""

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            rig.exchange.plans.append(filled((1, 100.0)))
            with pytest.raises(ExecutionHalted) as halted:
                await rig.executor.submit(entry(volume=2))
            assert "1 из 2" in halted.value.human
            assert halted.value.fills == ()
            assert rig.broker.live() == [], "остаток у брокера не снят"
            with pytest.raises(ExecutionHalted):
                await rig.executor.fills_at(bar(BAR0, unsettled=True))

    run(scenario())


def test_new_then_rejected_halts_with_the_exchange_words(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Статус `0`, затем `8` — остановка, и в причине — слова биржи (мутация молчания)."""

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            rig.exchange.plans.append(rejected("Недостаточно обеспечения по счёту"))
            with pytest.raises(ExecutionHalted) as halted:
                await rig.executor.submit(entry())
            assert "Недостаточно обеспечения по счёту" in halted.value.human
            assert "отклонила" in halted.value.human
            [row] = await rig.book.rows()
            assert row.status is CommandStatus.REJECTED
            with pytest.raises(ExecutionHalted):
                await rig.executor.submit(entry(at=BAR1))
            assert len(rig.creates()) == 1, "после остановки ушла ещё заявка"

    run(scenario())


# --- проверки перед заявкой (§2.2) ---


def test_journal_failure_sends_nothing(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Запись журнала упала — к брокеру ноль запросов создания, робот стоит (С5)."""

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            rig.book.fail = True
            with pytest.raises(ExecutionHalted) as halted:
                await rig.executor.submit(entry())
            assert "Журнал команд не записал" in halted.value.human
            assert rig.creates() == []
            assert rig.broker.commands == []

    run(scenario())


def test_stale_order_is_not_sent(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """§8 №15: заявка старше «сейчас − свеча − порог» к брокеру не уходит; это отказ, не стоп."""

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            rig.clock.now = BAR0 + timedelta(minutes=5, seconds=61)
            with pytest.raises(ExecutionRefused) as refused:
                await rig.executor.submit(entry())
            assert "несвежее" in str(refused.value)
            assert rig.creates() == []
            assert rig.executor.halted is None

    run(scenario())


def test_read_only_token_put_in_the_file_halts_before_any_command(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """§8 №17, `D-175`: токен в файле заменён на «только чтение» — следующая заявка не уходит.

    Сеанс прочитал торговый токен при старте; право перечитывается перед
    командой, а не берётся из прочитанного однажды.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            store.save(Secret(FAKE_REFRESH), TokenScope.READ_ONLY, issued_at=START)
            with pytest.raises(ExecutionHalted) as halted:
                await rig.executor.submit(entry())
            assert "только для чтения" in halted.value.human
            assert rig.broker.commands == []

    run(scenario())


def test_position_closed_by_hand_stops_the_reversal(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """§8 №13: у брокера позиции нет, движок считает лонг — ни выхода, ни входа, остановка.

    Без запроса портфеля выход продал бы контракт, которого нет, а вход —
    ещё один: шорт 2 вместо 1.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            rig.broker.outside = -1  # закрыт руками в приложении брокера
            rig.clock.now = BAR1 + timedelta(seconds=5)
            with pytest.raises(ExecutionHalted) as halted:
                await rig.executor.submit(leave())
            assert "у брокера позиции нет" in halted.value.human
            with pytest.raises(ExecutionHalted):
                await rig.executor.submit(entry(Side.SHORT, at=BAR1))
            assert len(rig.creates()) == 1, "после расхождения ушла заявка"
            assert rig.broker.position(TICKER) == 0

    run(scenario())


def test_position_mismatch_seen_by_the_stream_closes_everything_once(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Расхождение, которое программа видела сама, — снять заявки, закрыть ОДИН раз, стоять.

    Контракт докуплен руками, и сделка пришла потоком: робот встал (чужая
    сделка), сверка 2в закрывает всё. У брокера лонг 2, по сделкам программы
    тоже 2 — объём назван согласно, продажа на 2 ровно одна, «закрыта»
    сказано по факту. Повторная сверка и любая заявка ничего не подают.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(110.0, LevelTouch.RISE))
            rig.broker.foreign_trade("1", 1, 100.0)
            await heard()
            assert rig.executor.halted is not None
            rig.exchange.plans.append(filled((2, 99.0)))
            result = await reconcile(rig.executor, expected=1)
            assert result.halted is not None and "закрыта по рынку" in result.halted
            assert rig.broker.live() == [], "тейк у брокера не снят"
            assert rig.broker.position(TICKER) == 0
            closing = [body for body in rig.sells() if body["orderType"] == "1"]
            assert [body["orderQuantity"] for body in closing] == [2]
            again = await reconcile(rig.executor, expected=1)
            assert again.halted is not None
            with pytest.raises(ExecutionHalted):
                await rig.executor.submit(entry(at=BAR1))
            assert [body["orderQuantity"] for body in rig.sells() if body["orderType"] == "1"] == [
                2
            ], "закрыть всё подано второй раз"

    run(scenario())


def test_position_mismatch_unseen_by_the_stream_is_not_closed(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """К2: портфель и сделки программы расходятся — закрывающей заявки нет, обе цифры названы.

    Лонг 2 у брокера, по сделкам программы лонг 1 (контракт докуплен мимо
    потока, или портфель отстал): какое из двух верно, программа не знает,
    и продажа на 2 по отставшему портфелю была бы шортом. Заявки сняты.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(110.0, LevelTouch.RISE))
            rig.broker.outside = 1
            result = await reconcile(rig.executor, expected=1)
            assert result.halted is not None
            assert "лонг 2" in result.halted and "по сделкам программы — лонг 1" in result.halted
            assert "не подана" in result.halted
            assert rig.broker.live() == [], "тейк у брокера не снят"
            assert [body for body in rig.sells() if body["orderType"] == "1"] == []
            assert rig.broker.position(TICKER) == 2

    run(scenario())


def test_a_failed_close_all_is_not_repeated_by_the_next_reconcile(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """«Закрыть всё» — один раз за жизнь исполнителя, даже если первая попытка не закрыла.

    Биржа отклонила закрывающую продажу, у брокера по-прежнему лонг 2.
    Следующая сверка снова видит расхождение — и не подаёт вторую продажу:
    программа, стреляющая рыночными заявками на каждой сверке, хуже
    остановленной (С2). Текст говорит, что закрывать придётся человеку.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            rig.broker.foreign_trade("1", 1, 100.0)
            await heard()
            rig.exchange.plans.append(rejected("Инструмент на дискретном аукционе"))
            first = await reconcile(rig.executor, expected=1)
            assert first.halted is not None and "закрыть не удалось" in first.halted
            assert rig.broker.position(TICKER) == 2
            again = await reconcile(rig.executor, expected=1)
            assert again.halted is not None and "уже пыталась" in again.halted
            market_sells = [body for body in rig.sells() if body["orderType"] == "1"]
            assert len(market_sells) == 1, "закрыть всё подано второй раз"

    run(scenario())


def test_a_mismatch_found_before_an_order_cancels_the_take_at_the_broker(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Расхождение найдено проверкой перед заявкой — тейк у брокера снят.

    Не сверка при старте (там живые заявки снимает разбор журнала),
    а `_require_position` посреди торговли: у брокера лонг 2 (контракт
    докуплен руками мимо потока), робот выходит из лонга 1. Тейк,
    оставленный жить, исполнился бы без робота. Позиция не закрывается:
    портфель (2) и сделки программы (1) расходятся (К2 `/risk` 01.11.2026).
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(110.0, LevelTouch.RISE))
            rig.broker.outside = 1
            rig.exchange.plans.append(filled((2, 99.0)))
            rig.clock.now = BAR1 + timedelta(seconds=5)
            with pytest.raises(ExecutionHalted) as halted:
                await rig.executor.submit(leave())
            assert "лонг 2" in halted.value.human
            assert rig.broker.live() == [], "тейк у брокера пережил «закрыть всё»"
            assert rig.broker.position(TICKER) == 2
            market_sells = [body for body in rig.sells() if body["orderType"] == "1"]
            assert market_sells == [], "закрыто по портфелю, не согласному со сделками"

    run(scenario())


def test_a_close_all_names_the_take_it_could_not_cancel(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """«Закрыть всё» не смогло снять тейк — текст остановки это говорит.

    Портфель (лонг 2) и сделки программы (лонг 1) расходятся, поэтому позиция
    не закрывается (К2), а лимитный тейк у брокера, возможно, жив: исполнившись,
    он продаст без робота. Промолчать об этом — оставить человека с уверенностью,
    что заявок у брокера нет (правило 13). Случай «позиция закрыта, тейк жив» —
    `test_a_close_all_that_closed_still_names_the_take_it_could_not_cancel`.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(110.0, LevelTouch.RISE))
            rig.broker.outside = 1
            rig.broker.cancel_answers.append(Answer(False, ACCEPT.reply))
            rig.exchange.plans.append(filled((2, 99.0)))
            rig.clock.now = BAR1 + timedelta(seconds=5)
            with pytest.raises(ExecutionHalted) as halted:
                await rig.executor.submit(leave())
            assert "Заявку (тейк лонга) снять не удалось" in halted.value.human
            assert "не подана" in halted.value.human
            assert rig.broker.position(TICKER) == 2

    run(scenario())


def test_a_lagging_portfolio_is_read_again_before_closing_everything(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Портфель, отставший от потока сделок, читается второй раз — и «закрыть всё» не нужно.

    Первое чтение ещё не видит купленного контракта, второе видит. Объявить
    расхождение по первому значит продать дважды: «закрыть всё» плюс выход,
    которого позиция и так ждала.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            route = ("GET", PORTFOLIO_PATH)
            reads: list[int] = []

            def lagging(request: httpx.Request) -> httpx.Response:
                reads.append(1)
                if len(reads) > 1:
                    return honest(request)
                rig.broker.outside -= 1
                try:
                    return honest(request)
                finally:
                    rig.broker.outside += 1

            honest = rig.broker.replace_route(route, lagging)
            rig.exchange.plans.append(filled((1, 104.0)))
            rig.clock.now = BAR1 + timedelta(seconds=5)
            await rig.executor.submit(leave())
            assert len(reads) == 2, "портфель не перечитан"
            assert rig.executor.halted is None, "отставший портфель закрыл всё"
            assert [body["orderQuantity"] for body in rig.sells()] == [1]
            assert rig.broker.position(TICKER) == 0

    run(scenario())


# --- неподвижный тейк у брокера (Р1) ---


def test_fixed_take_is_one_limit_at_the_engine_price_and_rearm_sends_nothing(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Вооружение — состояние: одна лимитная по цене заявки как есть.

    Повтор того же уровня — ноль запросов к брокеру.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(110.0, LevelTouch.RISE))
            await rig.executor.submit(arm(110.0, LevelTouch.RISE, at=BAR1))
            limits = [body for body in rig.creates() if body["orderType"] == "2"]
            assert [(body["side"], body["price"]) for body in limits] == [("2", 110.0)]
            assert len(rig.broker.live()) == 1
            rig.broker.execute(str(limits[0]["clientOrderId"]), 1, 110.0)
            await heard()
            assert await rig.executor.fills_at(bar(BAR0, unsettled=True)) == []
            fill = only_fill(await rig.executor.fills_at(bar(BAR0)))
            assert (fill.action, fill.order_id, fill.price, fill.at) == (
                OrderAction.CLOSE, take_name(), 110.0, BAR0,
            )

    run(scenario())


def test_take_cancel_returns_only_after_the_cancelled_status(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Возврат из снятия = сторожа у брокера больше нет (статус `4`), а не «запрос принят»."""

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(110.0, LevelTouch.RISE))
            rig.clock.now = BAR1 + timedelta(seconds=5)
            await rig.executor.submit(disarm())
            assert rig.broker.live() == []
            assert [kind for kind, _ in rig.broker.commands].count("cancel") == 1
            rig.exchange.plans.append(filled((1, 104.0)))
            await rig.executor.submit(leave())
            fill = only_fill(await rig.executor.fills_at(bar(BAR1, unsettled=True)))
            assert (fill.action, fill.price) == (OrderAction.CLOSE, 104.0)

    run(scenario())


def test_take_cancelled_by_someone_else_halts(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Тейк снят не нами — остановка со словами, а не позиция без тейка молча."""

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(110.0, LevelTouch.RISE))
            take_key = str(rig.creates()[-1]["clientOrderId"])
            rig.broker.cancel_outside(take_key)
            await heard()
            with pytest.raises(ExecutionHalted) as halted:
                await rig.executor.fills_at(bar(BAR0))
            assert "снята не программой" in halted.value.human

    run(scenario())


def test_partly_filled_take_on_cancel_closes_the_rest_as_one_deal(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Тейк исполнен на 1 из 2 к снятию: остаток — рыночным выходом, движку ОДНА сделка на 2.

    Снятие отвечает `ALREADY_TRIGGERED`: движок рыночный выход не подаёт и ждёт
    сделку по уровню. Позиция у брокера после этого — ноль, не шорт.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig, volume=2)
            await rig.executor.submit(arm(110.0, LevelTouch.RISE, volume=2))
            take_key = str(rig.creates()[-1]["clientOrderId"])
            rig.broker.execute(take_key, 1, 110.0)
            await heard()
            rig.exchange.plans.append(filled((1, 108.0)))
            rig.clock.now = BAR1 + timedelta(seconds=5)
            with pytest.raises(ExecutionRefused) as refused:
                await rig.executor.submit(disarm(volume=2))
            assert refused.value.reason is Refusal.ALREADY_TRIGGERED
            fill = only_fill(await rig.executor.fills_at(bar(BAR0)))
            assert (fill.action, fill.volume) == (OrderAction.CLOSE, 2.0)
            assert fill.order_id == take_name()
            assert fill.price == pytest.approx(109.0)
            assert rig.broker.position(TICKER) == 0
            journal_names = {command.order_id for command in rig.book.written}
            assert take_name() in journal_names

    run(scenario())


def test_a_partly_filled_take_whose_rest_cannot_be_closed_halts_with_words(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Тейк исполнен на 1 из 2, остаток брокер закрыть не дал (400) — остановка словами.

    Без неё движок получил бы «уровень уже сработал» и ждал бы сделки
    на 2, которой не будет никогда, а на счёте висел бы контракт без тейка
    и без выхода — и робот считал бы себя работающим.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig, volume=2)
            await rig.executor.submit(arm(110.0, LevelTouch.RISE, volume=2))
            take_key = str(rig.creates()[-1]["clientOrderId"])
            rig.broker.execute(take_key, 1, 110.0)
            await heard()
            rig.broker.create_answers.append(ANSWERS["не принята, 400"])
            rig.clock.now = BAR1 + timedelta(seconds=5)
            with pytest.raises(ExecutionHalted) as halted:
                await rig.executor.submit(disarm(volume=2))
            assert "1 из 2" in halted.value.human
            assert "остаток закрыть не удалось" in halted.value.human
            assert rig.executor.halted is not None
            assert rig.broker.position(TICKER) == 1

    run(scenario())


def test_a_take_rejected_right_after_acceptance_halts_its_own_submit(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Лимитный тейк: `0`, сразу `8` — вооружение кончается остановкой со словами биржи.

    Возврат из вооружения движок читает как «уровень стоит у брокера»
    и пишет «Тейк-профит выставлен». Отклонённый биржей тейк, вернувший
    «принято», оставил бы в журнале уровень, которого нет нигде.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            rig.exchange.plans.append(rejected("Цена вне лимитов торговой сессии"))
            with pytest.raises(ExecutionHalted) as halted:
                await rig.executor.submit(arm(110.0, LevelTouch.RISE))
            assert "Цена вне лимитов торговой сессии" in halted.value.human

    run(scenario())


def test_a_journal_status_write_failure_after_sending_halts_out_loud(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Статус команды не записался (диск) — робот стоит, причина названа; сделка не теряется.

    Команда ушла и исполнилась, а журнал этого не знает: сверка после
    перезапуска сочла бы живую историю незакрытой. Молча продолжать нельзя
    (правило 13): следующий вопрос о сделках — остановка со словами про
    журнал, и исполненный вход уходит движку вместе с ней.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            rig.book.fail_status = True
            rig.exchange.plans.append(filled((1, 100.0)))
            await rig.executor.submit(entry())
            assert rig.executor.halted is not None, "сбой записи статуса проглочен"
            with pytest.raises(ExecutionHalted) as halted:
                await rig.executor.fills_at(bar(BAR0, unsettled=True))
            assert "журнал команд брокеру не принял запись" in halted.value.human
            assert [fill.action for fill in halted.value.fills] == [OrderAction.OPEN]

    run(scenario())


def test_unreadable_minutes_stop_the_trailing_guard_out_loud(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Минутки для сторожа не прочитались — остановка со словами, а не сторож вслепую.

    Сторож, не видящий минуток, не увидит и касания: позиция стоит без стопа,
    а робот считает её защищённой.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(95.0, LevelTouch.FALL))
            rig.minute_faults.append(sqlite3.OperationalError("database is locked"))
            await rig.executor.watch()
            with pytest.raises(ExecutionHalted) as halted:
                await rig.executor.fills_at(bar(BAR0))
            assert "минутки для сторожа уровня не прочитались" in halted.value.human

    run(scenario())


def test_a_portfolio_that_cannot_be_read_halts_before_any_command(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Портфель пришёл негодным (дробная позиция) — остановка, а не «сигнал пропущен».

    Это не обрыв: связь есть, а ответ брокера программа не понимает. Пропуск
    сигнала с продолжением работы повторял бы ту же заявку на каждой свече
    по позиции, которой программа не знает.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            route = ("GET", PORTFOLIO_PATH)

            def odd(request: httpx.Request) -> httpx.Response:
                return httpx.Response(200, json=[
                    {"type": "moneyLimit", "ticker": "RUB", "quantity": 1_000_000.0},
                    {"type": "futuresHolding", "ticker": TICKER, "board": "SPBFUT",
                     "quantity": 1.5},
                ])

            rig.broker.replace_route(route, odd)
            with pytest.raises(ExecutionHalted) as halted:
                await rig.executor.submit(entry())
            assert "прочитать нельзя" in halted.value.human
            assert rig.executor.halted is not None
            assert rig.creates() == []

    run(scenario())


def test_a_fractional_volume_halts_without_a_command(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Объём 1,5 контракта — остановка со словами, к брокеру не уходит ничего.

    Боевой заявки на дробный объём не бывает; округлить молча значит подать
    не тот объём, который считает движок.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            fractional = OrderRequest(
                action=OrderAction.OPEN, side=Side.LONG, volume=1.5, submitted_at=BAR0,
                reason="сигнал", order_id=f"open:long:{BAR0.isoformat()}",
            )
            with pytest.raises(ExecutionHalted) as halted:
                await rig.executor.submit(fractional)
            assert "не целое число контрактов" in halted.value.human
            assert rig.creates() == []

    run(scenario())


def test_a_halt_on_submit_carries_the_take_deal_that_filled_meanwhile(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Остановка на подаче уносит с собой сделку, исполнившуюся до неё (0066, §2.6).

    Тейк у брокера исполнился, движок о нём ещё не спросил; снятие этого
    тейка упирается в токен «только чтение». Сделка на счёте есть — движок
    обязан получить её вместе с остановкой, иначе позиция в окне так и
    останется открытой.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(110.0, LevelTouch.RISE))
            take_key = str(rig.creates()[-1]["clientOrderId"])
            rig.broker.execute(take_key, 1, 110.0)
            await heard()
            store.save(Secret(FAKE_REFRESH), TokenScope.READ_ONLY, issued_at=START)
            rig.clock.now = BAR1 + timedelta(seconds=5)
            with pytest.raises(ExecutionHalted) as halted:
                await rig.executor.submit(disarm())
            assert [(fill.order_id, fill.price) for fill in halted.value.fills] == [
                (take_name(), 110.0)
            ], "исполненный тейк не ушёл движку вместе с остановкой"

    run(scenario())


def test_an_order_refused_after_its_record_is_closed_in_the_journal(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """400 на создание — сигнал пропущен, и строка журнала закрыта как «отклонена».

    Строка, оставшаяся «записана», для сверки после перезапуска — команда
    с неизвестной судьбой (`D-177`): программа пошла бы спрашивать о ней
    брокера и разбирать то, чего не было.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            rig.broker.create_answers.append(ANSWERS["не принята, 400"])
            with pytest.raises(ExecutionRefused):
                await rig.executor.submit(entry())
            assert rig.executor.halted is None
            [row] = await rig.book.rows()
            assert row.status is CommandStatus.REFUSED

    run(scenario())


def test_a_reconcile_that_cannot_read_the_position_does_not_pass(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Позиция у брокера не прочиталась — сверка НЕ прошла, а не «сошлась по умолчанию».

    «Возобновить» допускается только через сверку (С3): сверка, прошедшая
    без ответа брокера, пустила бы торговлю по позиции, которой никто
    не видел.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            route = ("GET", PORTFOLIO_PATH)
            rig.broker.replace_route(
                route, lambda request: httpx.Response(503, text="Service Unavailable")
            )
            result = await reconcile(rig.executor, expected=0)
            assert result.broker is None
            assert result.halted is not None and "не прошла" in result.halted

    run(scenario())


def test_the_reconcile_does_not_ask_about_another_account_commands(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Незакрытая команда другого счёта этим сеансом не сверяется — брокера о ней не спрашивают.

    В `userdata/` бывает два токена на два счёта. Спросить о чужой заявке
    этим токеном — получить «не найдена» и закрыть в журнале живую заявку
    другого счёта как «не поданную».
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            foreign_key = "00000000-0000-4000-8000-0000000000aa"
            session_id = rig.book.session_id
            await rig.book.worker.call(lambda base: base.record_broker_command(
                session_id,
                BrokerCommand(
                    client_order_id=foreign_key, action=CommandAction.CREATE,
                    order_id="open:long:чужой", symbol=TICKER, side=CommandSide.BUY,
                    volume=1, account="FAKE-ACCOUNT-2",
                ),
                now=NOW,
            ))
            notes = await rig.executor.settle_open_commands()
            asked = [
                request for request in rig.broker.requests
                if (request.method, request.url.path) == STATUS
            ]
            assert asked == [], "брокера спросили о заявке чужого счёта"
            assert notes == ()
            row = await rig.book.worker.call(lambda base: base.broker_command(foreign_key))
            assert row is not None and row.status is CommandStatus.RECORDED

    run(scenario())


def _take_frame(rig: Rig, **changes: object) -> str:
    """Кадр потока по нашему тейку (снят с подделки) с подменёнными полями."""
    frame = json.loads(rig.broker.frames[-1])
    frame["data"].update(changes)
    return json.dumps(frame)


def test_an_execution_without_its_volume_halts_out_loud(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Исполнение по нашей заявке без объёма — остановка со словами, а не пропуск.

    Сделка на счёте есть, а сколько контрактов — не сказано. Пропустить её
    молча значит считать тейк живым, когда он, возможно, уже исполняется.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(110.0, LevelTouch.RISE))
            frame = json.loads(_take_frame(
                rig, executionType="1", orderStatus="1", executionId="FAKE-EXEC-NOQTY"
            ))
            frame["data"].pop("executedQuantity", None)
            rig.broker.send_frame(json.dumps(frame))
            await heard()
            with pytest.raises(ExecutionHalted) as halted:
                await rig.executor.fills_at(bar(BAR0))
            assert "не разобрано" in halted.value.human

    run(scenario())


def test_a_filled_take_without_a_price_halts_out_loud(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Тейк «исполнен», а цены брокер не назвал — сделка не выдумывается, робот стоит.

    Отдать движку сделку без цены нельзя, выбросить молча — тоже: позиция
    на счёте закрыта, а в окне открыта. Первый вопрос о сделках ничего
    не отдаёт и ставит остановку, следующий — остановка со словами.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(110.0, LevelTouch.RISE))
            rig.broker.send_frame(_take_frame(rig, orderStatus="2", executedQuantity=1))
            await heard()
            assert await rig.executor.fills_at(bar(BAR0)) == []
            assert rig.executor.halted is not None, "сделка без цены выброшена молча"
            with pytest.raises(ExecutionHalted) as halted:
                await rig.executor.fills_at(bar(BAR1))
            assert "не назвал цену" in halted.value.human

    run(scenario())


# --- шорт: те же механизмы с другой стороны ---


def test_a_short_fixed_take_is_a_limit_buy_at_the_broker(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Неподвижный тейк шорта (`FALL`) — лимитная покупка у брокера по цене заявки.

    Пара (шорт, снизу) — уровень впереди рынка (`_AT_BROKER`). Сторож
    в программе вместо неё сделал бы из тейка рыночный выход по касанию:
    другая цена исполнения, чем у прогона, и тейк, живущий только пока
    программа открыта.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            rig.exchange.plans.append(filled((1, 100.0)))
            await rig.executor.submit(entry(Side.SHORT))
            only_fill(await rig.executor.fills_at(bar(BAR0, unsettled=True)))
            await rig.executor.submit(arm(90.0, LevelTouch.FALL, side=Side.SHORT))
            limits = [body for body in rig.creates() if body["orderType"] == "2"]
            assert [(body["side"], body["price"]) for body in limits] == [("1", 90.0)]
            assert len(rig.broker.live()) == 1

    run(scenario())


def test_a_rise_touch_of_a_short_buys_exactly_once(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Касание `RISE` у шорта (максимум минутки дошёл до уровня) — одна рыночная покупка.

    Зеркало лонга: стоп шорта позади рынка лимитной не выразить, его сторожит
    программа. Максимум ровно на уровне — касание, как у модели (`high >= level`);
    минимум сторону касания не решает.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            rig.exchange.plans.append(filled((1, 100.0)))
            await rig.executor.submit(entry(Side.SHORT))
            only_fill(await rig.executor.fills_at(bar(BAR0, unsettled=True)))
            await rig.executor.submit(arm(105.0, LevelTouch.RISE, side=Side.SHORT))
            assert rig.broker.live() == [], "стоп шорта ушёл к брокеру лимитной заявкой"
            rig.minutes.append(minute(BAR0 + timedelta(minutes=1), low=99.0, high=104.0))
            await rig.executor.watch()
            assert [body for body in rig.creates() if body["side"] == "1"] == [], (
                "минутка ниже уровня дала покупку"
            )
            rig.minutes.append(minute(BAR0 + timedelta(minutes=2), low=104.0, high=105.0))
            rig.exchange.plans.append(filled((1, 105.5)))
            await rig.executor.watch()
            await rig.executor.watch()
            buys = [body for body in rig.creates() if body["side"] == "1"]
            assert [(body["orderType"], body["orderQuantity"]) for body in buys] == [("1", 1)]
            fill = only_fill(await rig.executor.fills_at(bar(BAR0)))
            assert (fill.action, fill.order_id, fill.price) == (
                OrderAction.CLOSE, take_name(Side.SHORT), 105.5,
            )
            assert rig.broker.position(TICKER) == 0

    run(scenario())


# --- трейлинг: сторож в программе (Р2) ---


def test_fall_touch_of_a_long_sells_exactly_once(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Касание `FALL` у лонга — ровно одна рыночная продажа; повторный снимок второй не даёт.

    Уровень у брокера не стоит вовсе (лимитной стоп не выразить), продажа
    записана в журнал под именем заявки тейка движка — по нему день узнаёт тейк.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(95.0, LevelTouch.FALL))
            assert rig.broker.live() == [], "стоп ушёл к брокеру лимитной заявкой"
            rig.minutes.append(minute(BAR0 + timedelta(minutes=1), low=96.0))
            await rig.executor.watch()
            assert rig.sells() == []
            rig.minutes.append(minute(BAR0 + timedelta(minutes=2), low=94.5))
            rig.exchange.plans.append(filled((1, 94.0)))
            await rig.executor.watch()
            rig.minutes[-1] = minute(BAR0 + timedelta(minutes=2), low=93.0)
            rig.minutes.append(minute(BAR0 + timedelta(minutes=3), low=92.0))
            await rig.executor.watch()
            assert [(body["orderType"], body["orderQuantity"]) for body in rig.sells()] == [
                ("1", 1)
            ]
            fill = only_fill(await rig.executor.fills_at(bar(BAR0)))
            assert (fill.action, fill.order_id, fill.price, fill.at) == (
                OrderAction.CLOSE, take_name(), 94.0, BAR0,
            )
            assert rig.book.written[-1].order_id == take_name()
            assert rig.broker.position(TICKER) == 0

    run(scenario())


def test_touch_in_the_last_seconds_then_cancel_is_already_triggered_and_no_short(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Касание в последней минутке T, записанной уже после прохода по T.

    Снятие на T+1 — `ALREADY_TRIGGERED`; выход движка к брокеру не уходит;
    шорта нет; сделка — по свече касания T, а не T+1.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(95.0, LevelTouch.FALL))
            rig.minutes.extend(
                minute(BAR0 + timedelta(minutes=step), low=97.0) for step in range(4)
            )
            assert await rig.executor.fills_at(bar(BAR0)) == []
            rig.minutes.append(minute(BAR0 + timedelta(minutes=4), low=94.0))
            rig.exchange.plans.append(filled((1, 94.5)))
            rig.clock.now = BAR1 + timedelta(seconds=8)
            with pytest.raises(ExecutionRefused) as refused:
                await rig.executor.submit(disarm())
            assert refused.value.reason is Refusal.ALREADY_TRIGGERED
            assert len(rig.sells()) == 1
            assert await rig.executor.fills_at(bar(BAR1, unsettled=True)) == []
            fill = only_fill(await rig.executor.fills_at(bar(BAR1)))
            assert (fill.order_id, fill.at, fill.price) == (take_name(), BAR0, 94.5)
            with pytest.raises(ExecutionHalted):
                await rig.executor.submit(leave())
            assert len(rig.sells()) == 1, "выход движка ушёл к брокеру поверх сработавшего сторожа"
            assert rig.broker.position(TICKER) == 0

    run(scenario())


def test_trailing_arm_after_its_touch_is_already_triggered(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """`D-142`: вооружение сработавшего сторожа — `ALREADY_TRIGGERED`, новый уровень не ставится."""

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(95.0, LevelTouch.FALL))
            rig.minutes.append(minute(BAR0 + timedelta(minutes=1), low=94.0))
            rig.exchange.plans.append(filled((1, 94.0)))
            with pytest.raises(ExecutionRefused) as refused:
                await rig.executor.submit(arm(96.0, LevelTouch.FALL, at=BAR1))
            assert refused.value.reason is Refusal.ALREADY_TRIGGERED
            assert len(rig.sells()) == 1

    run(scenario())


def test_touch_in_backfilled_minutes_sells_once_after_the_link_returns(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Касание в догруженной после обрыва минутке — выход по рынку после связи, робот работает.

    Решение владельца 01.11.2026 (0066): пока потока статусов нет, выход
    не подаётся и робот не встаёт; после связи — ровно одна рыночная продажа
    под именем тейка `take:…`, строка журнала до POST. Движок получает выход
    свечой касания, а не свечой связи. Повтор догрузки тех же минуток второй
    продажи не даёт.
    """

    async def scenario() -> None:
        gate = asyncio.Event()

        async def gated(seconds: float) -> None:
            await gate.wait()

        async with make_rig(tmp_path, store, broker, sleep=gated) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(95.0, LevelTouch.FALL))
            rig.broker.drop_stream()
            for _ in range(100):
                if not rig.executor.connected:
                    break
                await asyncio.sleep(0.01)
            assert not rig.executor.connected
            rig.clock.now = BAR0 + timedelta(minutes=20)
            backfill = [minute(BAR0 + timedelta(minutes=1), low=97.0),
                        minute(BAR0 + timedelta(minutes=2), low=94.0)]
            rig.minutes.extend(backfill)
            await rig.executor.watch()
            assert rig.sells() == [], "выход подан без потока статусов"
            assert rig.executor.halted is None, "касание без связи остановило робота"
            sent = len(rig.book.sent_before)
            rig.exchange.plans.append(filled((1, 93.0)))
            gate.set()
            assert await rig.executor.wait_connected(2.0)
            await rig.executor.watch()
            assert [(body["orderType"], body["orderQuantity"]) for body in rig.sells()] == [
                ("1", 1)
            ]
            assert rig.book.written[-1].order_id == take_name()
            assert rig.book.sent_before[sent:] == [len(rig.broker.commands) - 1]
            rig.minutes.extend(backfill)
            await rig.executor.watch()
            fill = only_fill(await rig.executor.fills_at(bar(BAR0 + timedelta(minutes=15))))
            assert (fill.action, fill.order_id, fill.price, fill.at) == (
                OrderAction.CLOSE, take_name(), 93.0, BAR0,
            )
            assert len(rig.sells()) == 1, "повтор догрузки дал вторую продажу"
            assert rig.executor.halted is None
            assert rig.broker.position(TICKER) == 0

    run(scenario())


# --- выход по времени с пределом (Р3) ---


def test_limit_time_exit_not_filled_halts_naming_the_live_order(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Лимитный выход не исполнился за N свечей — остановка, текст называет живую заявку (С9)."""

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            rig.clock.now = BAR1 + timedelta(seconds=5)
            await rig.executor.submit(leave(limit=99.0))
            [limit] = [body for body in rig.creates() if body["orderType"] == "2"]
            assert limit["price"] == 99.0
            assert await rig.executor.fills_at(bar(BAR1)) == []
            with pytest.raises(ExecutionHalted) as halted:
                await rig.executor.fills_at(bar(BAR2))
            assert str(limit["clientOrderId"]) in halted.value.human
            assert "снимите" in halted.value.human
            assert len(rig.broker.live()) == 1, "живую заявку исполнитель снял сам"

    run(scenario())


# --- неясный исход (§8 №1, №2, №6, №14; D-177) ---


def test_lost_answer_halts_and_nothing_is_sent_twice(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """§8 №1: ответ потерян после приёма — остановка; ключ ушёл один раз; строка — до POST."""

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            rig.broker.create_answers.append(ANSWERS["принята, ответ потерян по таймауту"])
            with pytest.raises(ExecutionHalted) as halted:
                await rig.executor.submit(entry())
            assert "Неизвестно" in halted.value.human
            with pytest.raises(ExecutionHalted):
                await rig.executor.submit(entry(at=BAR1))
            assert list(rig.broker.keys_sent("create").values()) == [1]
            assert rig.book.sent_before == [0], "строка журнала записана после POST"
            [row] = await rig.book.rows()
            assert row.status in (CommandStatus.UNKNOWN, CommandStatus.ACCEPTED)

    run(scenario())


def test_after_lost_answer_reconcile_cancels_the_live_order(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """§8 №2, решение 0017: после связи живая заявка снимается, позиция сошлась — не закрываем."""

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            rig.broker.create_answers.append(ANSWERS["принята, ответ потерян по таймауту"])
            rig.exchange.plans.append(resting)
            with pytest.raises(ExecutionHalted) as earlier:
                await rig.executor.submit(entry())
            result = await reconcile(rig.executor, expected=0)
            assert (result.broker, result.expected) == (0, 0)
            assert result.halted == earlier.value.human, "сверка скрыла, что исполнитель стоит"
            assert rig.sells() == [], "сошлось — а закрыть всё всё равно подано"
            assert any("снята" in note for note in result.notes)
            assert rig.broker.live() == []
            rows = {row.command.action: row.status for row in await rig.book.rows()}
            assert rows == {
                CommandAction.CREATE: CommandStatus.CANCELLED,
                CommandAction.CANCEL: CommandStatus.CANCELLED,
            }

    run(scenario())


def test_after_lost_answer_an_executed_order_is_flattened(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """§8 №2, решение 0020: исполнилась без ответа — у брокера лонг, у робота ноль: закрыть."""

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            rig.broker.create_answers.append(ANSWERS["принята, связь оборвана до ответа"])
            rig.exchange.plans.extend([filled((1, 100.0)), filled((1, 98.0))])
            with pytest.raises(ExecutionHalted):
                await rig.executor.submit(entry())
            result = await reconcile(rig.executor, expected=0)
            assert result.halted is not None
            assert rig.broker.position(TICKER) == 0
            assert len(rig.sells()) == 1

    run(scenario())


def test_a_close_all_lands_in_the_day_under_its_real_name_and_is_not_a_take(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """«Закрыть всё» исполнителя входит в день счёта из базы — и тейком не считается.

    Решение 0066: сделки «закрыть всё» идут мимо движка, и день после
    перезапуска (Р7) знает о них только из таблицы исполнений. Проверяется
    сквозной путь с настоящим именем команды, которое даёт исполнитель, а
    не придуманное в тесте: вход 1 по 100 (ответ потерян), сверка закрывает
    по 98 — день видит выход сегодня и минус 2 пункта × 10 ₽.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            rig.broker.create_answers.append(ANSWERS["принята, связь оборвана до ответа"])
            rig.exchange.plans.extend([filled((1, 100.0)), filled((1, 98.0))])
            with pytest.raises(ExecutionHalted):
                await rig.executor.submit(entry())
            result = await reconcile(rig.executor, expected=0)
            assert result.halted is not None
            day = await rig.book.worker.call(
                lambda base: base.executions_day(
                    ACCOUNT, BAR0.date(), ruble_per_point={TICKER: 10.0},
                    is_take=is_take_order,
                )
            )
            assert (day.gross, day.last_exit_date) == (-20.0, BAR0.date())
            assert day.take_profit_date is None, "«закрыть всё» засчитано тейком"
            assert day.foreign == ()

    run(scenario())


@pytest.mark.parametrize("answer", ["принята, ответ 429", "не принята, 429"])
def test_429_on_create_halts_without_a_repeat(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker, answer: str
) -> None:
    """§8 №6: 429 на создании — остановка, повтора нет (создала ли 429 заявку — нет в снимке)."""

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            rig.broker.create_answers.append(ANSWERS[answer])
            with pytest.raises(ExecutionHalted):
                await rig.executor.submit(entry())
            assert len(rig.creates()) == 1

    run(scenario())


def test_accepted_without_event_is_found_by_status_after_a_429(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """§8 №14 и №6: 200 без события — статус по ключу (429 на нём — повтор), нашлась — принята."""

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            rig.broker.muted = True
            rig.broker.status_answers.extend([ANSWERS["не принята, 429"].reply, None])
            rig.exchange.plans.append(filled((1, 100.0)))
            await rig.executor.submit(entry())
            fill = only_fill(await rig.executor.fills_at(bar(BAR0, unsettled=True)))
            assert (fill.action, fill.price) == (OrderAction.OPEN, 100.0)
            assert len(rig.creates()) == 1

    run(scenario())


def test_accepted_but_not_found_by_status_halts(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """§8 №14: 200, событий нет, по ключу брокер заявку не находит — остановка, не отказ."""

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            rig.broker.muted = True

            def missing(request: httpx.Request, key: str) -> httpx.Response:
                return httpx.Response(404, json={"type": "NOT_FOUND", "traceId": "fake"})

            rig.broker.status_answers.extend([missing, missing])
            with pytest.raises(ExecutionHalted) as halted:
                await rig.executor.submit(entry())
            assert "не находит" in halted.value.human

    run(scenario())


def test_task_cancelled_after_record_reads_as_unknown_not_as_not_sent(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """`D-177`: отмена задачи между записью и ответом — робот стоит; сверка решает по брокеру."""

    async def scenario() -> None:
        entered, release = asyncio.Event(), asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            # Держится только ПЕРВОЕ создание: второе, если исполнитель его
            # пропустит после остановки, обязано уронить тест, а не повесить
            # его навсегда (мутация «подача не смотрит на остановку»).
            if (request.method, request.url.path) == CREATE and not entered.is_set():
                entered.set()
                await release.wait()
            return broker(request)

        client = httpx.AsyncClient(
            base_url="https://example.invalid", transport=httpx.MockTransport(handler)
        )
        async with make_rig(tmp_path, store, broker, client=client) as rig:
            task = asyncio.create_task(rig.executor.submit(entry()))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert rig.executor.halted is not None
            with pytest.raises(ExecutionHalted):
                await rig.executor.submit(entry(at=BAR1))
            notes = await rig.executor.settle_open_commands()
            assert any("не найдена" in note for note in notes)
            [row] = await rig.book.rows()
            assert row.status is CommandStatus.REFUSED
        await client.aclose()

    run(scenario())


# --- поток статусов (§8 №7) и чужая сделка (§8 №9) ---


def test_dead_stream_refuses_and_lost_events_are_recovered_once(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """§8 №7: сокет закрыт — заявка не подана (отказ); после связи добрано, без дублей."""

    async def scenario() -> None:
        gate = asyncio.Event()

        async def gated(seconds: float) -> None:
            await gate.wait()

        async with make_rig(tmp_path, store, broker, sleep=gated) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(110.0, LevelTouch.RISE))
            take_key = str(rig.creates()[-1]["clientOrderId"])
            sent = len(rig.broker.commands)
            rig.broker.drop_stream()
            for _ in range(100):
                if not rig.executor.connected:
                    break
                await asyncio.sleep(0.01)
            assert not rig.executor.connected
            rig.broker.execute(take_key, 1, 110.0)
            assert rig.broker.lost, "событие не потерялось — проверка пуста"
            rig.clock.now = BAR1 + timedelta(seconds=5)
            with pytest.raises(ExecutionRefused):
                await rig.executor.submit(leave())
            assert len(rig.broker.commands) == sent
            gate.set()
            assert await rig.executor.wait_connected(2.0)
            fill = only_fill(await rig.executor.fills_at(bar(BAR0)))
            assert (fill.order_id, fill.price) == (take_name(), 110.0)
            rig.broker.replay_last()
            await heard()
            assert await rig.executor.fills_at(bar(BAR1)) == []
            assert len(await rig.book.executions(take_key)) == 1

    run(scenario())


def test_foreign_trade_on_our_ticker_halts(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """§8 №9: сделка по нашему тикеру с незнакомым ключом — остановка.

    Движку она не отдаётся ни с чужим именем, ни с пустым.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            key = rig.broker.foreign_trade("2", 1, 101.0)
            await heard()
            with pytest.raises(ExecutionHalted) as halted:
                await rig.executor.fills_at(bar(BAR0))
            assert "не подавала" in halted.value.human
            assert halted.value.fills == ()
            assert len(await rig.book.executions(key)) == 1

    run(scenario())


# --- токен ---


def test_the_token_is_in_no_text(
    tmp_path: pathlib.Path,
    store: TokenStore,
    broker: FakeBroker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Токен не попадает ни в текст остановки, ни в журнал команд, ни в технический лог."""
    texts: list[str] = []

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            rig.broker.create_answers.append(ANSWERS["принята, ответ 500"])
            with pytest.raises(ExecutionHalted) as halted:
                await rig.executor.submit(entry())
            texts.extend([halted.value.human, repr(halted.value), str(halted.value)])
            texts.extend(row.reason for row in await rig.book.rows())
            texts.append(rig.executor.halted or "")

    with caplog.at_level(logging.DEBUG):
        run(scenario())
    texts.append(caplog.text)
    for secret in (FAKE_REFRESH, FAKE_ACCESS):
        assert all(secret not in text for text in texts), "токен в тексте"


# --- движок целиком на боевом исполнителе ---


#: Движок в бою по окну 10:05–11:00, без тейка: проверяется проводка порта, а не тейк.
WORKING = EngineSettings(
    mode=Mode.REVERSE, window=TradingWindow(time(10, 5), time(11, 0)), take_profit=False
)


def _engine_rig_clock(rig: Rig) -> None:
    """Часы исполнителя — на закрытие свечи 10:05 из `tests/engine_helpers.py` (19.06.2026)."""
    rig.clock.now = datetime(2026, 6, 19, 10, 10, 5, tzinfo=MSK)


def test_the_engine_opens_a_long_through_the_live_executor(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Движок сам подаёт вход в исполнитель и получает сделку: порт проведён, а не только описан.

    Сделка входа — по цене брокера, на свече после решения; у брокера лонг 1.
    """
    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            _engine_rig_clock(rig)
            engine = Engine(ScriptedStrategy([decision(Intent.LONG, close=210_000.0)]), WORKING)
            rig.exchange.plans.append(filled((1, 210_025.0)))
            await engine.on_market_candle(candle(10, 5, close=210_000.0), rig.executor)
            await engine.on_market_candle(candle(10, 10, unsettled=True), rig.executor)
            assert not engine.halted, engine.halted
            position = engine.position
            assert position is not None and position.side is Side.LONG
            assert position.entry_price == 210_025.0
            assert rig.broker.position(TICKER) == 1

    run(scenario())


def test_the_engine_names_an_exchange_rejection_in_the_executor_words(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Статус `0`, затем `8` под движком: робот стоит, причина — слова биржи, позиции нет."""
    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            _engine_rig_clock(rig)
            engine = Engine(ScriptedStrategy([decision(Intent.LONG, close=210_000.0)]), WORKING)
            rig.exchange.plans.append(rejected("Инструмент на дискретном аукционе"))
            await engine.on_market_candle(candle(10, 5, close=210_000.0), rig.executor)
            assert "Инструмент на дискретном аукционе" in engine.halted
            assert engine.position is None

    run(scenario())


# --- проверки test-expert: поведение, которое тесты выше не ломало ---


async def _wait_disconnected(rig: Rig) -> None:
    """Дождаться, что исполнитель заметил обрыв потока статусов."""
    for _ in range(100):
        if not rig.executor.connected:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("обрыв потока статусов не замечен")


def test_a_stale_order_costs_not_a_single_request(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """§2.2 п. 1: несвежая заявка отказывается ДО любого запроса — и портфель не спрашивается.

    Догрузка после обрыва прогоняет через «факт» десятки старых баров;
    запрос портфеля на каждый — это лимит частоты, потраченный на заявки,
    которые всё равно не уйдут. Стережёт порядок проверок: свежесть первой.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            rig.clock.now = BAR0 + timedelta(minutes=5, seconds=61)
            before = len(rig.broker.requests)
            with pytest.raises(ExecutionRefused):
                await rig.executor.submit(entry())
            assert rig.broker.requests[before:] == [], "несвежая заявка потратила запрос"
            assert rig.executor.halted is None

    run(scenario())


def test_a_dead_stream_costs_no_request_and_the_robot_trades_after_the_link(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Поток статусов мёртв — сигнал пропущен без единого запроса, робот не встаёт.

    Решение владельца 01.11.2026 (0066): обрыв потока — «пропустить сигнал,
    работать дальше». Стережёт три вещи: отказ, а не остановка; ни одного
    HTTP-запроса (портфель тоже не спрашивается — проверка потока раньше);
    после связи следующая заявка уходит и исполняется.
    """

    async def scenario() -> None:
        gate = asyncio.Event()

        async def gated(seconds: float) -> None:
            await gate.wait()

        async with make_rig(tmp_path, store, broker, sleep=gated) as rig:
            await long_open(rig)
            rig.broker.drop_stream()
            await _wait_disconnected(rig)
            rig.clock.now = BAR1 + timedelta(seconds=5)
            before = len(rig.broker.requests)
            with pytest.raises(ExecutionRefused) as refused:
                await rig.executor.submit(leave())
            assert "сигнал пропущен" in str(refused.value)
            assert rig.broker.requests[before:] == [], "заявка без потока потратила запрос"
            assert rig.executor.halted is None, "обрыв потока остановил робота"
            gate.set()
            assert await rig.executor.wait_connected(2.0)
            rig.exchange.plans.append(filled((1, 103.0)))
            await rig.executor.submit(leave())
            fill = only_fill(await rig.executor.fills_at(bar(BAR1, unsettled=True)))
            assert (fill.action, fill.price) == (OrderAction.CLOSE, 103.0)
            assert rig.broker.position(TICKER) == 0

    run(scenario())


def test_a_moved_trailing_level_is_not_judged_by_minutes_before_it(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Сдвинутый уровень судит только минутки, начавшиеся не раньше его подачи.

    Зеркало модели (`submitted_at > ready_at`): уровень 97, поданный в 10:05,
    не знает минутки 10:01 с минимумом 96 — она была при уровне 95. Судить
    её новым уровнем значит продать по касанию, которого при этом уровне
    не было, и разойтись с прогоном.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(95.0, LevelTouch.FALL))
            rig.minutes.append(minute(BAR0 + timedelta(minutes=1), low=96.0))
            await rig.executor.watch()
            rig.clock.now = BAR1 + timedelta(seconds=5)
            await rig.executor.submit(arm(97.0, LevelTouch.FALL, at=BAR1))
            rig.exchange.plans.append(filled((1, 96.5)))
            await rig.executor.watch()
            assert rig.sells() == [], "новый уровень сработал по минутке до своей подачи"
            rig.minutes.append(minute(BAR1, low=96.5))
            await rig.executor.watch()
            assert [body["orderQuantity"] for body in rig.sells()] == [1]

    run(scenario())


def test_a_moved_trailing_level_fires_at_the_new_level(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Перевооружение скользящего уровня меняет то, чем судит сторож.

    Уровень подтянут с 95 до 97; минутка с минимумом 96,5 задевает новый
    и не задевает прежний. Сторож, оставшийся на 95, держал бы позицию
    без того уровня, который считает движок и судит прогон.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(95.0, LevelTouch.FALL))
            rig.clock.now = BAR1 + timedelta(seconds=5)
            await rig.executor.submit(arm(97.0, LevelTouch.FALL, at=BAR1))
            rig.exchange.plans.append(filled((1, 96.5)))
            rig.minutes.append(minute(BAR1 + timedelta(minutes=1), low=96.5))
            await rig.executor.watch()
            assert [body["orderQuantity"] for body in rig.sells()] == [1], (
                "сторож судит прежним уровнем"
            )
            fill = only_fill(await rig.executor.fills_at(bar(BAR1)))
            assert (fill.order_id, fill.at, fill.price) == (take_name(), BAR1, 96.5)

    run(scenario())


def test_a_trailing_exit_not_sent_for_a_passing_reason_is_sent_later_without_a_halt(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Касание есть, а портфель не прочитался (503) — выход ждёт, робот не встаёт.

    Команда к брокеру не ушла и связь вернётся: это не «брокер отказал»,
    а «подать позже». Следующий проход сторожа подаёт ровно одну продажу,
    и движок получает её свечой касания.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(95.0, LevelTouch.FALL))
            route = ("GET", PORTFOLIO_PATH)
            broken = [True]

            def flaky(request: httpx.Request) -> httpx.Response:
                if broken[0]:
                    return httpx.Response(503, text="Service Unavailable")
                return honest(request)

            honest = rig.broker.replace_route(route, flaky)
            rig.minutes.append(minute(BAR0 + timedelta(minutes=1), low=94.0))
            rig.exchange.plans.append(filled((1, 94.0)))
            await rig.executor.watch()
            assert rig.sells() == []
            assert rig.executor.halted is None, "временный отказ портфеля остановил робота"
            broken[0] = False
            await rig.executor.watch()
            assert [body["orderQuantity"] for body in rig.sells()] == [1]
            fill = only_fill(await rig.executor.fills_at(bar(BAR0)))
            assert (fill.order_id, fill.at, fill.price) == (take_name(), BAR0, 94.0)

    run(scenario())


def test_a_trailing_exit_refused_by_the_broker_halts_with_words(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Выход сработавшего сторожа брокер отклонил (400) — остановка, и сказано почему.

    Команда записана и ушла, брокер её отверг: повтор дал бы тот же отказ,
    а позиция стоит без стопа. Молча ждать «следующего прохода» здесь нельзя
    (правило 13): робот стоит, текст называет уровень трейлинга.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(95.0, LevelTouch.FALL))
            rig.broker.create_answers.append(ANSWERS["не принята, 400"])
            rig.minutes.append(minute(BAR0 + timedelta(minutes=1), low=94.0))
            await rig.executor.watch()
            assert rig.executor.halted is not None, "отказ брокера в выходе по стопу проглочен"
            with pytest.raises(ExecutionHalted) as halted:
                await rig.executor.fills_at(bar(BAR0))
            assert "трейлинга 95" in halted.value.human
            assert "не принял" in halted.value.human

    run(scenario())


def test_a_touch_in_the_minute_after_the_close_belongs_to_the_next_candle(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Касание в минутке 10:05 — сделка свечи 10:05, а не закрытой свечи 10:00.

    Минутка, начавшаяся ровно в момент закрытия свечи, ей не принадлежит
    (граница по закрытию, неравенство строгое). Отдать её сделку по свече
    10:00 значит выйти на свечу раньше прогона.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(95.0, LevelTouch.FALL))
            rig.minutes.append(minute(BAR1, low=94.0))
            rig.exchange.plans.append(filled((1, 94.0)))
            await rig.executor.watch()
            assert len(rig.sells()) == 1
            assert await rig.executor.fills_at(bar(BAR0)) == [], (
                "сделка минутки 10:05 отдана закрытой свечой 10:00"
            )
            fill = only_fill(await rig.executor.fills_at(bar(BAR1)))
            assert fill.at == BAR1

    run(scenario())


def test_a_stale_trailing_arm_is_refused_and_guards_nothing(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Несвежее вооружение скользящего уровня — отказ, сторож не встаёт.

    Догрузка после обрыва прогоняет через «факт» старые бары; уровень,
    решённый на одном из них, сторожить нельзя — движок узнаёт об отказе
    и пишет, что позиция без уровня, а не считает её защищённой.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            rig.clock.now = BAR0 + timedelta(minutes=5, seconds=61)
            with pytest.raises(ExecutionRefused) as refused:
                await rig.executor.submit(arm(95.0, LevelTouch.FALL))
            assert "несвежее" in str(refused.value)
            rig.minutes.append(minute(BAR1 + timedelta(minutes=1), low=94.0))
            await rig.executor.watch()
            assert rig.sells() == [], "отклонённый уровень всё равно сторожится"

    run(scenario())


def test_a_refused_trailing_exit_is_not_sent_again_after_the_halt(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Выход сторожа брокер отклонил, робот стоит — второй продажи не уходит никогда.

    `_send_fire` о себе говорит: «повтор той же заявки дал бы тот же отказ».
    Сейчас повтор есть: отклонённая заявка не попадает в сделку сторожа,
    и следующий `fills_at` или `watch` подаёт рыночную продажу заново — уже
    после остановки. Человек, прочитавший «робот остановлен, проверьте
    позицию», продаёт руками, программа продаёт следом: шорт.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(95.0, LevelTouch.FALL))
            rig.broker.create_answers.append(ANSWERS["не принята, 400"])
            rig.minutes.append(minute(BAR0 + timedelta(minutes=1), low=94.0))
            await rig.executor.watch()
            assert rig.executor.halted is not None
            with pytest.raises(ExecutionHalted):
                await rig.executor.fills_at(bar(BAR0))
            await rig.executor.watch()
            assert len(rig.sells()) == 1, "после остановки ушла ещё одна продажа"

    run(scenario())


def test_a_trailing_guard_sends_nothing_once_the_robot_is_halted(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Робот остановлен (чужая сделка) — касание сторожа заявки к брокеру не подаёт.

    Остановка — «ни одна заявка больше не уйдёт» (`_flatten`), и движок
    пишет человеку: «остановка не должна приводить к сделке». Сторож,
    продолжающий продавать через `watch`, делает обе фразы неправдой.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(95.0, LevelTouch.FALL))
            rig.broker.foreign_trade("1", 1, 101.0)
            await heard()
            assert rig.executor.halted is not None
            rig.minutes.append(minute(BAR0 + timedelta(minutes=1), low=94.0))
            rig.exchange.plans.append(filled((1, 94.0)))
            await rig.executor.watch()
            assert rig.sells() == [], "остановленный робот продал по сторожу"

    run(scenario())


def test_a_minute_low_exactly_at_the_level_is_a_touch(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Минимум минутки, равный уровню, — касание, как у модели (`low <= level`).

    Сторож со строгим неравенством пропустил бы ровно то касание, на котором
    прогон выходит, и бой разошёлся бы с прогоном на каждом таком баре.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(95.0, LevelTouch.FALL))
            rig.minutes.append(minute(BAR0 + timedelta(minutes=1), low=95.0))
            rig.exchange.plans.append(filled((1, 95.0)))
            await rig.executor.watch()
            assert [body["orderQuantity"] for body in rig.sells()] == [1], (
                "касание ровно по уровню не засчитано"
            )

    run(scenario())


def test_a_minute_that_grew_after_the_check_is_judged_again(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Последняя минутка, дописанная после проверки, смотрится ещё раз.

    В 10:01:30 минутка 10:01 знает минимум 96, к 10:02 — уже 94. Сторож,
    пропустивший её как проверенную, не увидел бы касания вовсе: следующая
    минутка выше уровня, и позиция осталась бы без сработавшего стопа.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(95.0, LevelTouch.FALL))
            rig.minutes.append(minute(BAR0 + timedelta(minutes=1), low=96.0))
            await rig.executor.watch()
            assert rig.sells() == []
            rig.minutes[-1] = minute(BAR0 + timedelta(minutes=1), low=94.0)
            rig.minutes.append(minute(BAR0 + timedelta(minutes=2), low=97.0))
            rig.exchange.plans.append(filled((1, 94.5)))
            await rig.executor.watch()
            assert [body["orderQuantity"] for body in rig.sells()] == [1], (
                "доросшая минутка не проверена второй раз"
            )

    run(scenario())


def test_the_take_cancel_returns_only_once_the_exchange_says_cancelled(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Брокер ответил на снятие 200, а `4` приходит позже — снятие ждёт его.

    Ответ 200 значит «запрос принят», а не «заявки нет». Вернуть снятие
    раньше `4` значит пустить рыночный выход поверх живого тейка: тейк
    исполнится следом и откроет шорт на полный объём.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(110.0, LevelTouch.RISE))
            take_key = str(rig.creates()[-1]["clientOrderId"])
            rig.broker.cancel_answers.append(Answer(False, ACCEPT.reply))
            asyncio.get_running_loop().call_later(0.1, rig.broker.cancel_outside, take_key)
            rig.clock.now = BAR1 + timedelta(seconds=5)
            await rig.executor.submit(disarm())
            assert rig.broker.live() == [], "снятие вернулось, пока тейк жив у брокера"
            assert rig.executor.halted is None

    run(scenario())


def test_a_take_cancel_never_confirmed_halts_and_sends_no_exit(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Снятие принято, а `4` не пришло и по ключу заявка жива — остановка, выхода нет.

    Стережёт молчание: «не дождались» не равно «снято». Текст называет ключ
    живой заявки, и ни одной продажи после этого не уходит.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(110.0, LevelTouch.RISE))
            take_key = str(rig.creates()[-1]["clientOrderId"])
            rig.broker.cancel_answers.append(Answer(False, ACCEPT.reply))
            rig.clock.now = BAR1 + timedelta(seconds=5)
            with pytest.raises(ExecutionHalted) as halted:
                await rig.executor.submit(disarm())
            assert "не подтверждено" in halted.value.human
            assert take_key in halted.value.human
            with pytest.raises(ExecutionHalted):
                await rig.executor.submit(leave())
            assert [body["orderType"] for body in rig.sells()] == ["2"], "ушёл выход"

    run(scenario())


def test_a_take_level_off_the_whole_grid_goes_to_the_broker_as_is(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Цену тейка к шагу приводит движок, исполнитель её не трогает (0066, Р1).

    Уровень 110,25 — узел сетки шага 0,25. Исполнитель, округливший его
    «к целому», поставил бы у брокера не тот уровень, которым прогон судит
    касание.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(110.25, LevelTouch.RISE))
            limits = [body for body in rig.creates() if body["orderType"] == "2"]
            assert [body["price"] for body in limits] == [110.25]

    run(scenario())


# --- /risk 01.11.2026: S1–S5 как сторожа правильного поведения ---


def _partial_then_foreign(fake: FakeBroker, order: BookOrder) -> None:
    """Вход на 2 исполнен на 1, и тут же по тикеру прошла чужая сделка: робот встал."""
    fake.execute(order.key, 1, 100.0)
    fake.foreign_trade("1", 1, 100.0)


def test_halt_while_a_market_order_fills_cancels_the_rest_and_names_it(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """К1 (S1): остановка пришла посреди рыночной заявки — остаток всё равно снят и назван.

    Снятие своей рыночной заявки — единственная команда, разрешённая после
    остановки. Текст: ключ заявки, «исполнено 1 из 2», «остаток снят» и обе
    причины. Исполненная часть движку не отдаётся (Р6) — названа словами.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            rig.exchange.plans.append(_partial_then_foreign)
            with pytest.raises(ExecutionHalted) as halted:
                await rig.executor.submit(entry(volume=2))
            key = rig.book.written[0].client_order_id
            text = halted.value.human
            assert key in text and "1 из 2" in text and "остаток снят" in text
            assert "не подавала" in text, "первая причина остановки потеряна"
            assert rig.broker.live() == [], "остаток рыночной заявки жив"
            assert list(rig.broker.keys_sent("cancel").values()) == [1]
            assert halted.value.fills == ()
            assert len(rig.creates()) == 1, "после остановки ушла новая заявка"

    run(scenario())


def test_a_rest_that_could_not_be_cancelled_is_named_alive(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """К1: снять остаток не вышло — текст говорит «жив, снимите в приложении брокера» и ключ."""

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            rig.exchange.plans.append(_partial_then_foreign)
            rig.broker.cancel_answers.append(Answer(False, ACCEPT.reply))
            with pytest.raises(ExecutionHalted) as halted:
                await rig.executor.submit(entry(volume=2))
            key = rig.book.written[0].client_order_id
            text = halted.value.human
            assert key in text and "1 из 2" in text
            assert "снимите его в приложении брокера" in text
            assert len(rig.broker.live()) == 1

    run(scenario())


def test_close_all_does_not_oversell_when_the_take_fills_during_its_cancel(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """К2 (S2): тейк исполнился при снятии, портфель отстал — «закрыть всё» не продаёт лишнего.

    Два чтения портфеля разошлись (лонг 2, затем лонг 1), по сделкам
    программы позиции нет: объём не назван надёжно, закрывающая заявка
    не подана, человек получает все цифры. Исполненный тейк уходит движку
    вместе с остановкой.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(110.0, LevelTouch.RISE))
            [take] = rig.broker.live()
            rig.broker.outside = 1
            lag = {"once": False}

            def racing_cancel(request: httpx.Request) -> httpx.Response:
                if take.live:
                    rig.broker.execute(take.key, 1, 110.0)
                    lag["once"] = True
                return honest_cancel(request)

            def lagging(request: httpx.Request) -> httpx.Response:
                if lag["once"]:
                    lag["once"] = False
                    rig.broker.outside += 1
                    try:
                        return honest_portfolio(request)
                    finally:
                        rig.broker.outside -= 1
                return honest_portfolio(request)

            honest_cancel = rig.broker.replace_route(CANCEL, racing_cancel)
            honest_portfolio = rig.broker.replace_route(("GET", PORTFOLIO_PATH), lagging)
            rig.clock.now = BAR1 + timedelta(seconds=5)
            with pytest.raises(ExecutionHalted) as halted:
                await rig.executor.submit(leave())
            assert rig.broker.position(TICKER) == 1, "закрыть всё продало по отставшему портфелю"
            assert [body for body in rig.sells() if body["orderType"] == "1"] == []
            assert "не сходится" in halted.value.human and "не подана" in halted.value.human
            assert "закрыта" not in halted.value.human
            fill = only_fill(list(halted.value.fills))
            assert (fill.order_id, fill.price) == (take_name(), 110.0)

    run(scenario())


def test_an_unresolved_command_fails_reconcile_and_its_later_fill_halts(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """С1 (S3): о команде журнала брокер не ответил — сверка не прошла; её исполнение — стоп.

    Перезапуск: новый исполнитель на той же базе. Статус по ключу отвечает
    503 — «сверка не закончена» названа остановкой со словами, позиция не
    сравнивается. Исполнение этой заявки потом — не тишина, а остановка:
    сделки движку по ней нет.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            rig.broker.create_answers.append(ANSWERS["принята, ответ потерян по таймауту"])
            rig.exchange.plans.append(resting)
            with pytest.raises(ExecutionHalted):
                await rig.executor.submit(entry())
            key = rig.book.written[0].client_order_id
            await rig.executor.close()
            assert rig.session is not None

            async def no_minutes(since: datetime) -> list[Candle]:
                return []

            def unavailable(request: httpx.Request, key: str) -> httpx.Response:
                return httpx.Response(503, json={"type": "UNAVAILABLE"})

            fresh = LiveExecutor(
                Wiring(rig.session, rig.book, no_minutes, clock=rig.clock),
                Instrument(TICKER, "SPBFUT", 5, ACCOUNT),
                FAST,
            )
            await fresh.start()
            try:
                assert await fresh.wait_connected(2.0)
                rig.broker.status_answers.extend([unavailable] * 10)
                result = await reconcile(fresh, expected=0)
                rig.broker.status_answers.clear()
                assert result.halted is not None and "Сверка не закончена" in result.halted
                assert key in result.halted
                assert result.broker is None, "позиция сравнивалась при неразобранной команде"
                rig.broker.execute(key, 1, 100.0)
                await heard()
                with pytest.raises(ExecutionHalted) as halted:
                    await fresh.fills_at(bar(BAR1))
                assert "исполнилась" in halted.value.human and "из журнала" in halted.value.human
            finally:
                await fresh.close()

    run(scenario())


def test_a_trailing_touch_without_the_stream_is_told_to_the_human(
    tmp_path: pathlib.Path,
    store: TokenStore,
    broker: FakeBroker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """С2 (S4): уровень пройден, выход ждёт связи — робот не встаёт, но человеку сказано.

    Одно сообщение на касание (через `Wiring.notice` и строкой WARNING),
    а не на каждый проход. После связи — одна продажа.
    """

    async def scenario() -> None:
        gate = asyncio.Event()

        async def gated(seconds: float) -> None:
            await gate.wait()

        async with make_rig(tmp_path, store, broker, sleep=gated) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(95.0, LevelTouch.FALL))
            rig.broker.drop_stream()
            for _ in range(100):
                if not rig.executor.connected:
                    break
                await asyncio.sleep(0.01)
            rig.minutes.append(minute(BAR0 + timedelta(minutes=1), low=94.0))
            for number in range(1, 6):
                rig.clock.now = BAR0 + timedelta(minutes=5 * number, seconds=5)
                start = BAR0 + timedelta(minutes=5 * (number - 1))
                assert await rig.executor.fills_at(bar(start)) == []
            await heard()
            assert rig.executor.halted is None
            assert len(rig.notices) == 1, rig.notices
            assert "уровень трейлинга 95" in rig.notices[0]
            assert "ждёт" in rig.notices[0] and "связи" in rig.notices[0]
            assert any(
                record.levelno == logging.WARNING and "уровень трейлинга 95" in record.getMessage()
                for record in caplog.records
            )
            rig.exchange.plans.append(filled((1, 94.0)))
            gate.set()
            assert await rig.executor.wait_connected(2.0)
            await rig.executor.watch()
            assert len(rig.sells()) == 1

    with caplog.at_level(logging.WARNING):
        run(scenario())


def test_a_reversal_with_a_portfolio_lagging_past_settle_does_not_open_a_short(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """К2 (S5): выход исполнен, портфель отстаёт дольше `position_settle` — второй продажи нет.

    Вход в шорт видит в портфеле прежний лонг: робот встаёт, но «закрыть
    всё» не продаёт — чтения портфеля и сделки программы расходятся.
    Позиция у брокера — ноль, а не шорт.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            lag = {"reads": 0}

            def lagging(request: httpx.Request) -> httpx.Response:
                if lag["reads"] > 0:
                    lag["reads"] -= 1
                    rig.broker.outside += 1
                    try:
                        return honest_portfolio(request)
                    finally:
                        rig.broker.outside -= 1
                return honest_portfolio(request)

            honest_portfolio = rig.broker.replace_route(("GET", PORTFOLIO_PATH), lagging)

            def close_then_lag(fake: FakeBroker, order: BookOrder) -> None:
                fake.execute(order.key, 1, 104.0)
                lag["reads"] = 3

            rig.exchange.plans.extend([close_then_lag, filled((1, 104.0))])
            rig.clock.now = BAR1 + timedelta(seconds=5)
            await rig.executor.submit(leave())
            with pytest.raises(ExecutionHalted) as halted:
                await rig.executor.submit(entry(side=Side.SHORT, at=BAR1))
            assert rig.broker.position(TICKER) == 0, "переворот с отставшим портфелем дал шорт"
            assert len(rig.sells()) == 1
            assert "не сходится" in halted.value.human

    run(scenario())


# --- test-expert 01.11.2026: К1, К2, С1, С2 после правок /risk — сторожа выживших мутаций ---


def _on_first_ping(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, act: Callable[[], object]
) -> None:
    """Первый ping потока (проверка «поток жив» перед командой) сначала делает `act`.

    И даёт потоку прочитать то, что `act` прислал: событие приходит посреди
    подачи — после проверки остановки в начале `submit`, до команды брокеру.
    """
    socket = rig.broker.sockets[-1]
    honest = socket.ping
    done: list[bool] = []

    async def racing() -> asyncio.Future[float]:
        if not done:
            done.append(True)
            act()
            await heard()
        return await honest()

    monkeypatch.setattr(socket, "ping", racing)


def _market_sells(rig: Rig) -> list[object]:
    """Объёмы рыночных продаж, ушедших к брокеру, по порядку."""
    return [body["orderQuantity"] for body in rig.sells() if body["orderType"] == "1"]


def test_a_halt_arriving_while_a_take_is_being_cancelled_sends_no_cancel(
    tmp_path: pathlib.Path,
    store: TokenStore,
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """К1: после остановки снимается только остаток своей рыночной заявки — не тейк.

    Чужая сделка приходит посреди снятия тейка, уже после проверки остановки
    в начале `submit`. Снятие лимитного тейка — не остаток рыночной: к брокеру
    оно не уходит (`B-077`), тейк жив, человеку названа причина остановки.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(110.0, LevelTouch.RISE))
            _on_first_ping(rig, monkeypatch, lambda: rig.broker.foreign_trade("1", 1, 100.0))
            rig.clock.now = BAR1 + timedelta(seconds=5)
            with pytest.raises(ExecutionHalted) as halted:
                await rig.executor.submit(disarm())
            assert "не подавала" in halted.value.human
            sent = [kind for kind, _ in rig.broker.commands if kind == "cancel"]
            assert sent == [], "после остановки к брокеру ушло снятие тейка"
            assert [one.order_type for one in rig.broker.live()] == ["2"]

    run(scenario())


def test_a_halt_arriving_while_an_exit_is_checked_sends_no_order(
    tmp_path: pathlib.Path,
    store: TokenStore,
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """К1: остановка пришла посреди проверок выхода — продажа к брокеру не уходит.

    Тейк сняли руками в приложении брокера, пока выход проходил проверки:
    позиция при этом та же, проверка позиции пропускает. Последняя преграда —
    проверка перед самой командой (`B-077`): без неё продажа ушла бы поверх
    действий человека, прочитавшего «робот остановлен».
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(110.0, LevelTouch.RISE))
            [take] = rig.broker.live()
            _on_first_ping(rig, monkeypatch, lambda: rig.broker.cancel_outside(take.key))
            rig.clock.now = BAR1 + timedelta(seconds=5)
            with pytest.raises(ExecutionHalted) as halted:
                await rig.executor.submit(leave())
            assert "снята не программой" in halted.value.human
            assert _market_sells(rig) == [], "после остановки ушла продажа"
            assert rig.broker.position(TICKER) == 1

    run(scenario())


def test_close_all_without_known_deals_does_not_sell_on_two_disagreeing_reads(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """К2: позиция по сделкам ещё неизвестна — закрывают только два согласных чтения.

    Сверка при старте: совпавшего чтения не было, сверять портфель не с чем,
    кроме него самого. Между двумя чтениями перед закрытием позиция выросла
    (лонг 1, затем лонг 2): продажа по любому из них — догадка. Заявки нет,
    текст называет обе цифры и «по сделкам программы — неизвестна».
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            rig.broker.outside = 1
            reads: list[int] = []

            def growing(request: httpx.Request) -> httpx.Response:
                reads.append(1)
                if len(reads) == 3:
                    rig.broker.outside = 2  # докуплено руками между чтениями
                return honest(request)

            honest = rig.broker.replace_route(("GET", PORTFOLIO_PATH), growing)
            rig.exchange.plans.append(filled((1, 99.0)))
            result = await reconcile(rig.executor, expected=0)
            text = result.halted or ""
            assert "лонг 2" in text and "чтением раньше — лонг 1" in text
            assert "по сделкам программы — неизвестна" in text and "не подана" in text
            assert rig.creates() == [], "закрыто по одному чтению портфеля"
            assert rig.broker.position(TICKER) == 2

    run(scenario())


def test_a_close_all_that_leaves_a_position_does_not_say_closed(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """К2: закрывающая исполнилась, а позиция после неё не ноль — «закрыта» не пишется.

    Объём согласен (лонг 2 в портфеле и по сделкам), продажа на 2 исполнена,
    но пока она шла, контракт докупили руками. «Закрыта» — только по нулевой
    позиции после закрывающей: текст называет исполнение и то, что осталось.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            rig.broker.foreign_trade("1", 1, 100.0)
            await heard()

            def close_while_bought_by_hand(fake: FakeBroker, order: BookOrder) -> None:
                fake.execute(order.key, 2, 99.0)
                fake.outside += 1

            rig.exchange.plans.append(close_while_bought_by_hand)
            result = await reconcile(rig.executor, expected=1)
            text = result.halted or ""
            assert _market_sells(rig) == [2]
            assert "закрыта" not in text, "«закрыта» при оставшейся позиции"
            assert "исполнена по 99" in text and "у брокера лонг 1" in text
            assert rig.broker.position(TICKER) == 1

    run(scenario())


def test_a_close_all_whose_check_after_fails_does_not_say_closed(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """К2: закрывающая исполнилась, а позицию после неё прочитать нельзя — «проверьте сами».

    Нулевой позиции никто не видел: «закрыта» было бы утверждением без
    проверки. Отказ чтения — слова в тексте остановки, а не исключение
    из сверки.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            rig.broker.foreign_trade("1", 1, 100.0)
            await heard()
            broken = [False]

            def portfolio(request: httpx.Request) -> httpx.Response:
                if broken[0]:
                    return httpx.Response(503, text="Service Unavailable")
                return honest(request)

            honest = rig.broker.replace_route(("GET", PORTFOLIO_PATH), portfolio)

            def close_then_portfolio_breaks(fake: FakeBroker, order: BookOrder) -> None:
                fake.execute(order.key, 2, 99.0)
                broken[0] = True

            rig.exchange.plans.append(close_then_portfolio_breaks)
            result = await reconcile(rig.executor, expected=1)
            text = result.halted or ""
            assert "закрыта" not in text, "«закрыта» без чтения позиции после закрытия"
            assert "исполнена по 99" in text and "прочитать не удалось" in text
            assert "проверьте её сами" in text
            assert _market_sells(rig) == [2]

    run(scenario())


def test_a_close_all_that_cannot_read_the_portfolio_tells_to_close_by_hand(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """К2: расхождение найдено, а портфель перед закрытием не читается — заявки нет, сказано.

    Проверка перед выходом прочитала лонг 2 при лонге 1 у робота, «закрыть
    всё» читает портфель ещё раз — отказ. Объёма нет: закрывающая не подаётся,
    и человеку это сказано словами, а не пустой строкой.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            rig.broker.outside = 1
            reads: list[int] = []

            def failing_after_two(request: httpx.Request) -> httpx.Response:
                reads.append(1)
                if len(reads) > 2:
                    return httpx.Response(503, text="Service Unavailable")
                return honest(request)

            honest = rig.broker.replace_route(("GET", PORTFOLIO_PATH), failing_after_two)
            rig.clock.now = BAR1 + timedelta(seconds=5)
            with pytest.raises(ExecutionHalted) as halted:
                await rig.executor.submit(leave())
            text = halted.value.human
            assert "прочитать не удалось" in text and "закройте позицию сами" in text
            assert _market_sells(rig) == []
            assert rig.broker.position(TICKER) == 2

    run(scenario())


def test_a_journal_order_that_cannot_be_cancelled_fails_reconcile(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """С1: живую заявку журнала снять не вышло — сверка не прошла, позиция не сравнивается.

    Ответ на вход потерян, заявка жива у брокера. Сверка её снимает — снятие
    не подтвердилось: заявка, возможно, жива и исполнится без робота. Это
    остановка с ключом и «снимите в приложении брокера», а не строка в заметках.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            rig.broker.create_answers.append(ANSWERS["принята, ответ потерян по таймауту"])
            rig.exchange.plans.append(resting)
            with pytest.raises(ExecutionHalted):
                await rig.executor.submit(entry())
            key = rig.book.written[0].client_order_id
            rig.broker.cancel_answers.append(Answer(False, ACCEPT.reply))
            result = await reconcile(rig.executor, expected=0)
            text = result.halted or ""
            assert "Сверка не закончена" in text and key in text
            assert "снимите её в приложении брокера" in text
            assert result.broker is None, "позиция сравнивалась при неснятой заявке"
            alive = rig.broker.order(key)
            assert alive is not None and alive.live

    run(scenario())


def test_an_execution_known_at_reconcile_does_not_halt_when_the_stream_repeats_it(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """С1, обратная сторона: исполнение, учтённое сверкой, — не «сделка без движка».

    Заявка прошлого запуска исполнилась, пока программа была закрыта; сверка
    нашла её исполненной, позиция сошлась. Поток вправе прислать то же событие
    ещё раз (план §2.10): это не новое исполнение, робот не встаёт.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            rig.broker.create_answers.append(ANSWERS["принята, ответ потерян по таймауту"])
            rig.exchange.plans.append(resting)
            with pytest.raises(ExecutionHalted):
                await rig.executor.submit(entry())
            key = rig.book.written[0].client_order_id
            await rig.executor.close()
            rig.broker.execute(key, 1, 100.0)  # программа закрыта: событие уходит в пустоту
            assert rig.session is not None

            async def no_minutes(since: datetime) -> list[Candle]:
                return []

            fresh = LiveExecutor(
                Wiring(rig.session, rig.book, no_minutes, clock=rig.clock),
                Instrument(TICKER, "SPBFUT", 5, ACCOUNT),
                FAST,
            )
            await fresh.start()
            try:
                assert await fresh.wait_connected(2.0)
                result = await reconcile(fresh, expected=1)
                assert result.halted is None, result.halted
                rig.broker.replay_last()
                await heard()
                assert fresh.halted is None, fresh.halted
                assert await fresh.fills_at(bar(BAR1)) == []
            finally:
                await fresh.close()

    run(scenario())


def test_a_trailing_exit_waiting_for_the_portfolio_is_told_once(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """С2: выход сторожа ждёт из-за временного отказа портфеля — человеку сказано один раз.

    Робот не встаёт (отказ до команды, связь вернётся), но позиция без защиты:
    молчать нельзя (правило 13). Сообщение одно, сколько бы проходов ни ждали,
    и в нём уровень и причина.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(95.0, LevelTouch.FALL))

            def unavailable(request: httpx.Request) -> httpx.Response:
                return httpx.Response(503, text="Service Unavailable")

            rig.broker.replace_route(("GET", PORTFOLIO_PATH), unavailable)
            rig.minutes.append(minute(BAR0 + timedelta(minutes=1), low=94.0))
            await rig.executor.watch()
            await rig.executor.watch()
            await heard()
            assert rig.executor.halted is None
            assert rig.sells() == []
            assert len(rig.notices) == 1, rig.notices
            assert "уровень трейлинга 95" in rig.notices[0]
            assert "прочитать не удалось" in rig.notices[0]

    run(scenario())


def test_the_link_back_calls_for_a_pass_when_a_trailing_exit_waits(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """С2: связь вернулась, выход сторожа ждёт — исполнитель зовёт проход (`Wiring.on_news`).

    Без зова выход ушёл бы только с ближайшей минуткой, а позиция всё это
    время без защиты. Проход по зову подаёт ровно одну продажу.
    """

    async def scenario() -> None:
        gate = asyncio.Event()
        called = asyncio.Event()

        async def gated(seconds: float) -> None:
            await gate.wait()

        async with make_rig(tmp_path, store, broker, sleep=gated, on_news=called.set) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(95.0, LevelTouch.FALL))
            rig.broker.drop_stream()
            await _wait_disconnected(rig)
            rig.minutes.append(minute(BAR0 + timedelta(minutes=1), low=94.0))
            await rig.executor.watch()
            await heard()
            assert rig.sells() == [] and len(rig.notices) == 1
            called.clear()
            rig.exchange.plans.append(filled((1, 94.0)))
            gate.set()
            await asyncio.wait_for(called.wait(), 2.0)
            await rig.executor.watch()
            assert _market_sells(rig) == [1]

    run(scenario())


def test_rearming_the_level_after_a_reconcile_keeps_the_one_take(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """После сверки тот же уровень — то же состояние: тейк у брокера один и тот же.

    До `B-079` сверка снимала свой живой тейк, и этот тест стерёг, что
    повторное вооружение ставит его заново. Теперь сверка тейк не трогает
    (только статус), и стережётся обратное: повторное вооружение того же
    уровня не ставит второй лимитной заявки, а исполнение единственного
    тейка доходит движку одной сделкой.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(110.0, LevelTouch.RISE))
            result = await reconcile(rig.executor, expected=1)
            assert result.halted is None, result.halted
            [take] = rig.broker.live()
            rig.clock.now = BAR1 + timedelta(seconds=5)
            await rig.executor.submit(arm(110.0, LevelTouch.RISE, at=BAR1))
            limits = [body for body in rig.creates() if body["orderType"] == "2"]
            assert [(body["side"], body["price"]) for body in limits] == [("2", 110.0)]
            assert [order.key for order in rig.broker.live()] == [take.key]
            rig.broker.execute(take.key, 1, 110.0)
            await heard()
            fill = only_fill(await rig.executor.fills_at(bar(BAR1)))
            assert (fill.order_id, fill.price) == (take_name(), 110.0)

    run(scenario())


def test_a_close_all_does_not_say_closed_while_the_portfolio_lags_a_seen_trade(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """К2: после закрывающей портфель показывает ноль, а поток видел ещё покупку — не «закрыта».

    Портфель отстаёт от потока сделок (посылка К2): пока шла продажа на 2,
    по тикеру прошла покупка 1, поток её показал, портфель ещё нет. Ноль
    в портфеле здесь — отставание, а не закрытая позиция: текст называет
    обе цифры.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            rig.broker.foreign_trade("1", 1, 100.0)
            await heard()

            def close_while_a_lagging_buy(fake: FakeBroker, order: BookOrder) -> None:
                fake.execute(order.key, 2, 99.0)
                fake.foreign_trade("1", 1, 100.0)
                fake.outside -= 1  # портфель этой покупки ещё не показывает

            rig.exchange.plans.append(close_while_a_lagging_buy)
            result = await reconcile(rig.executor, expected=1)
            text = result.halted or ""
            assert _market_sells(rig) == [2]
            assert "закрыта" not in text, "«закрыта» по отставшему портфелю"
            assert "у брокера позиции нет" in text and "по сделкам программы — лонг 1" in text

    run(scenario())


def test_a_close_all_that_closed_still_names_the_take_it_could_not_cancel(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """«Закрыть всё» закрыло позицию, а тейк снять не смогло — текст говорит и то, и другое.

    Опасное сочетание: «закрыта по рынку» читается как «на счёте пусто», а
    лимитный тейк, возможно, жив и, исполнившись, откроет шорт без робота.
    Робот считает лонг 1, а портфель и сделки потока согласно говорят лонг 2:
    объём закрытия подтверждён (К2), продажа на 2 прошла.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig, volume=2)
            await rig.executor.submit(arm(110.0, LevelTouch.RISE, volume=2))
            rig.broker.cancel_answers.append(Answer(False, ACCEPT.reply))
            rig.exchange.plans.append(filled((2, 99.0)))
            rig.clock.now = BAR1 + timedelta(seconds=5)
            with pytest.raises(ExecutionHalted) as halted:
                await rig.executor.submit(leave(volume=1))
            text = halted.value.human
            assert "закрыта по рынку" in text
            assert "Заявку (тейк лонга) снять не удалось" in text
            assert [one.order_type for one in rig.broker.live()] == ["2"]
            assert rig.broker.position(TICKER) == 0

    run(scenario())


# --- B-079: сверка после связи не снимает свой живой тейк ---


async def _reconnect(rig: Rig, gate: asyncio.Event, while_down: Callable[[], None]) -> None:
    """Оборвать поток статусов, сделать `while_down` без связи и переоткрыть поток."""
    rig.broker.drop_stream()
    for _ in range(100):
        if not rig.executor.connected:
            break
        await asyncio.sleep(0.01)
    assert not rig.executor.connected, "поток не оборвался — проверка пуста"
    while_down()
    gate.set()
    assert await rig.executor.wait_connected(2.0), "поток не переоткрылся"


def test_reconcile_after_a_reconnect_keeps_the_live_take_at_the_broker(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """B-079: после связи сверка тейк у брокера НЕ снимает — только спрашивает его статус.

    Лонг 1 с тейком 110 у брокера, поток статусов оборван и переоткрыт,
    позиция та же. Команд снятия ноль, тейк жив, сверка прошла, заметка
    сверки дошла до человека.
    """

    async def scenario() -> None:
        gate = asyncio.Event()

        async def gated(seconds: float) -> None:
            await gate.wait()

        async with make_rig(tmp_path, store, broker, sleep=gated) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(110.0, LevelTouch.RISE))
            [take] = rig.broker.live()
            await _reconnect(rig, gate, lambda: None)
            result = await reconcile(rig.executor, expected=1)
            assert result.halted is None, result.halted
            assert rig.broker.keys_sent("cancel") == Counter(), "после связи тейк снят"
            assert [order.key for order in rig.broker.live()] == [take.key]
            await heard()
            assert any("жива" in note for note in rig.notices), rig.notices

    run(scenario())


def test_a_take_filled_during_the_outage_reaches_the_engine_once(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """B-079: тейк исполнился без потока — после связи одна сделка движку, не «закрыть всё».

    Событие исполнения потеряно (сокета нет); после связи статус по ключу
    его добирает. Сверка учитывает ещё не отданную сделку: у брокера ноль,
    у робота лонг 1 минус тейк — сошлось, снятий и продаж по рынку нет.
    Движок получает выход по тейку ровно один раз.
    """

    async def scenario() -> None:
        gate = asyncio.Event()

        async def gated(seconds: float) -> None:
            await gate.wait()

        async with make_rig(tmp_path, store, broker, sleep=gated) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(110.0, LevelTouch.RISE))
            [take] = rig.broker.live()
            await _reconnect(rig, gate, lambda: rig.broker.execute(take.key, 1, 110.0))
            assert rig.broker.lost, "исполнение не потерялось — проверка пуста"
            result = await reconcile(rig.executor, expected=1)
            assert result.halted is None, result.halted
            assert rig.broker.keys_sent("cancel") == Counter()
            assert [body for body in rig.sells() if body["orderType"] == "1"] == []
            fill = only_fill(await rig.executor.fills_at(bar(BAR0)))
            assert (fill.action, fill.order_id) == (OrderAction.CLOSE, take_name())
            assert fill.price == 110.0
            assert await rig.executor.fills_at(bar(BAR1)) == []
            assert rig.broker.position(TICKER) == 0

    run(scenario())


# --- B-079: проход test-expert по правке — ветки сверки своей живой заявки ---


class _StatusDown:
    """Статус по ключу у брокера не отвечает (`replace_route(STATUS, ...)`): 503 или 404.

    Считает вызовы: проверка, где статус «не получен», без единого вопроса
    к брокеру пуста.
    """

    def __init__(self, code: int) -> None:
        self.code = code
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.code == 404:
            return httpx.Response(404, json={"type": "NOT_FOUND", "traceId": "fake-trace"})
        return httpx.Response(self.code, text="Service Unavailable")


def _gated() -> tuple[asyncio.Event, Callable[[float], Coroutine[object, object, None]]]:
    """Пауза переподключения, которую отпускает тест: поток стоит, пока он не скажет."""
    gate = asyncio.Event()

    async def sleep(seconds: float) -> None:
        await gate.wait()

    return gate, sleep


def test_reconcile_after_a_reconnect_keeps_the_limit_exit_waiting_at_the_broker(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """B-079: выход по времени с пределом, ждущий исполнения, сверка после связи не снимает.

    Лонг 1, выход с пределом 99 стоит у брокера (Р3), поток статусов оборван
    и переоткрыт. Снятий ноль, заявка жива, сверка прошла; её исполнение
    доходит до движка одной сделкой по 99.
    """

    async def scenario() -> None:
        gate, sleep = _gated()
        async with make_rig(tmp_path, store, broker, sleep=sleep) as rig:
            await long_open(rig)
            rig.clock.now = BAR1 + timedelta(seconds=5)
            await rig.executor.submit(leave(limit=99.0))
            [waiting] = rig.broker.live()
            await _reconnect(rig, gate, lambda: None)
            result = await reconcile(rig.executor, expected=1)
            assert result.halted is None, result.halted
            assert rig.broker.keys_sent("cancel") == Counter(), "после связи выход с пределом снят"
            assert [order.key for order in rig.broker.live()] == [waiting.key]
            rig.broker.execute(waiting.key, 1, 99.0)
            await heard()
            fill = only_fill(await rig.executor.fills_at(bar(BAR1)))
            assert (fill.action, fill.price) == (OrderAction.CLOSE, 99.0)

    run(scenario())


def test_reconcile_that_gets_no_status_of_the_live_take_does_not_pass_and_names_its_key(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """B-079: статус своего тейка на сверке не получен — «Сверка не закончена» с ключом.

    Тейк у брокера, поток переоткрыт, а на сверке брокер статус не отдаёт
    (503). Судьба тейка неизвестна: позиция не сравнивается и не закрывается,
    тейк не снимается, робот стоит словами с ключом заявки — и эти слова
    дошли до человека заметкой сверки.
    """

    async def scenario() -> None:
        gate, sleep = _gated()
        async with make_rig(tmp_path, store, broker, sleep=sleep) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(110.0, LevelTouch.RISE))
            [take] = rig.broker.live()
            await _reconnect(rig, gate, lambda: None)
            down = _StatusDown(503)
            rig.broker.replace_route(STATUS, down)
            result = await reconcile(rig.executor, expected=1)
            assert down.calls, "статус на сверке не спрашивался — проверка пуста"
            text = result.halted or ""
            assert "Сверка не закончена" in text and take.key in text, text
            assert rig.executor.unsettled
            assert result.broker is None, "позиция сравнивалась при невыясненном тейке"
            assert rig.broker.keys_sent("cancel") == Counter()
            assert _market_sells(rig) == []
            await heard()
            assert any("Сверка не закончена" in note and take.key in note for note in rig.notices)

    run(scenario())


def test_reconcile_when_the_broker_no_longer_finds_the_live_take_does_not_pass(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """B-079: брокер по ключу не находит тейк, который программа ведёт, — сверка не прошла.

    404 на заявку, принятую и подтверждённую статусом, — не «не подана»:
    где тейк, неизвестно. Робот стоит словами с ключом, позиция не
    сравнивается, «закрыть всё» нет.
    """

    async def scenario() -> None:
        gate, sleep = _gated()
        async with make_rig(tmp_path, store, broker, sleep=sleep) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(110.0, LevelTouch.RISE))
            [take] = rig.broker.live()
            await _reconnect(rig, gate, lambda: None)
            down = _StatusDown(404)
            rig.broker.replace_route(STATUS, down)
            result = await reconcile(rig.executor, expected=1)
            assert down.calls, "статус на сверке не спрашивался — проверка пуста"
            text = result.halted or ""
            assert "не находит" in text and take.key in text, text
            assert rig.executor.unsettled
            assert result.broker is None
            assert rig.broker.keys_sent("cancel") == Counter()
            assert _market_sells(rig) == []

    run(scenario())


def test_a_take_filled_in_the_outage_and_missed_on_the_link_reaches_the_engine_once(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """B-079: тейк исполнился без потока, а при переподключении статус не получен.

    Добирает исполнение уже сверка: заметка «исполнилась, пока связи не было»,
    сверка прошла (у брокера ноль, у робота лонг 1 минус тейк), снятий
    и продаж по рынку нет, движок получает выход по тейку ровно один раз.
    """

    async def scenario() -> None:
        gate, sleep = _gated()
        async with make_rig(tmp_path, store, broker, sleep=sleep) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(110.0, LevelTouch.RISE))
            [take] = rig.broker.live()
            down = _StatusDown(503)
            book = rig.broker.replace_route(STATUS, down)
            await _reconnect(rig, gate, lambda: rig.broker.execute(take.key, 1, 110.0))
            rig.broker.replace_route(STATUS, book)
            assert rig.broker.lost and down.calls, "исполнение добрано при связи — проверка пуста"
            result = await reconcile(rig.executor, expected=1)
            assert result.halted is None, result.halted
            assert rig.broker.keys_sent("cancel") == Counter()
            assert _market_sells(rig) == []
            await heard()
            assert any("исполнилась, пока связи не было" in note for note in rig.notices), (
                rig.notices
            )
            fill = only_fill(await rig.executor.fills_at(bar(BAR0)))
            assert (fill.order_id, fill.price) == (take_name(), 110.0)
            assert await rig.executor.fills_at(bar(BAR1)) == []

    run(scenario())


@pytest.mark.parametrize(
    "asked_on_link", [True, False], ids=["статус при связи получен", "статус при связи не получен"]
)
def test_a_take_cancelled_outside_during_the_outage_halts_in_words_after_the_link(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker, asked_on_link: bool
) -> None:
    """B-079: тейк сняли не мы, пока связи не было, — после связи остановка словами.

    Событие снятия потеряно; статус по ключу добирает его при переподключении
    или, если тогда брокер не ответил, на сверке. Робот стоит с причиной
    «снята не программой»; ни одна заметка сверки не называет тейк живым.
    """

    async def scenario() -> None:
        gate, sleep = _gated()
        async with make_rig(tmp_path, store, broker, sleep=sleep) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(110.0, LevelTouch.RISE))
            [take] = rig.broker.live()
            down = _StatusDown(503)
            book = None if asked_on_link else rig.broker.replace_route(STATUS, down)
            await _reconnect(rig, gate, lambda: rig.broker.cancel_outside(take.key))
            if book is not None:
                rig.broker.replace_route(STATUS, book)
                assert down.calls, "статус при связи не спрашивался — проверка пуста"
            assert rig.broker.lost, "снятие пришло потоком — проверка пуста"
            result = await reconcile(rig.executor, expected=1)
            text = result.halted or ""
            assert "снята не программой" in text, text
            await heard()
            assert not [note for note in rig.notices if "жива" in note], rig.notices

    run(scenario())


def test_a_trailing_exit_still_waiting_for_the_link_is_not_counted_by_the_reconcile(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """B-079: сторож сработал без связи, выход ещё не подан — сверка не считает его сделкой.

    Лонг 1, уровень трейлинга 95 у программы, поток оборван, минутка 94 его
    задела: выход ждёт связи (С2). После связи у брокера всё ещё лонг 1 —
    сверка проходит без «закрыть всё»; следующий проход сторожа подаёт один
    выход, движку — одна сделка по уровню.
    """

    async def scenario() -> None:
        gate, sleep = _gated()
        async with make_rig(tmp_path, store, broker, sleep=sleep) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(95.0, LevelTouch.FALL))
            rig.broker.drop_stream()
            await _wait_disconnected(rig)
            rig.minutes.append(minute(BAR0 + timedelta(minutes=1), low=94.0))
            await rig.executor.watch()
            assert _market_sells(rig) == [], "выход подан без связи — проверка пуста"
            gate.set()
            assert await rig.executor.wait_connected(2.0), "поток не переоткрылся"
            result = await reconcile(rig.executor, expected=1)
            assert result.halted is None, result.halted
            assert _market_sells(rig) == [], "сверка закрыла позицию вместо выхода сторожа"
            rig.exchange.plans.append(filled((1, 94.0)))
            await rig.executor.watch()
            assert _market_sells(rig) == [1]
            fill = only_fill(await rig.executor.fills_at(bar(BAR0)))
            assert (fill.action, fill.order_id) == (OrderAction.CLOSE, take_name())

    run(scenario())


def test_an_entry_not_yet_given_to_the_engine_is_counted_by_the_reconcile(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """B-079: вход исполнился, движку ещё не отдан, — сверка считает его позицией робота.

    Вход по рынку исполнен, сделка ждёт свечи (`fills_at` ещё не звался),
    и связь переоткрылась. Движок знает «позиции нет», у брокера лонг 1:
    без поправки сверка закрыла бы всё на штатной сделке. Ожидаемое: сверка
    прошла, продаж нет, движок получает вход одной сделкой.
    """

    async def scenario() -> None:
        gate, sleep = _gated()
        async with make_rig(tmp_path, store, broker, sleep=sleep) as rig:
            rig.exchange.plans.append(filled((1, 100.0)))
            await rig.executor.submit(entry())
            await _reconnect(rig, gate, lambda: None)
            result = await reconcile(rig.executor, expected=0)
            assert result.halted is None, result.halted
            assert rig.sells() == []
            fill = only_fill(await rig.executor.fills_at(bar(BAR0, unsettled=True)))
            assert (fill.action, fill.volume) == (OrderAction.OPEN, 1.0)

    run(scenario())


def test_a_take_partly_filled_before_the_link_returned_is_not_a_mismatch_to_close(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Тейк на 2 исполнен на 1, остаток жив у брокера; связь переоткрылась — «закрыть всё» нет.

    Частичное исполнение тейка штатное (Р1, Р6): движок узнает о нём одной
    сделкой, когда тейк кончится. Позиция робота для сверки — лонг 2 движка
    минус контракт, уже исполненный тейком, то есть лонг 1, как у брокера.
    До правки `B-080` сверка объявляла расхождение, снимала тейк и закрывала
    позицию по рынку.
    """

    async def scenario() -> None:
        gate, sleep = _gated()
        async with make_rig(tmp_path, store, broker, sleep=sleep) as rig:
            await long_open(rig, volume=2)
            await rig.executor.submit(arm(110.0, LevelTouch.RISE, volume=2))
            [take] = rig.broker.live()
            rig.broker.execute(take.key, 1, 110.0)
            await heard()
            await _reconnect(rig, gate, lambda: None)
            result = await reconcile(rig.executor, expected=2)
            assert result.halted is None, result.halted
            assert _market_sells(rig) == []
            assert [order.key for order in rig.broker.live()] == [take.key]

    run(scenario())


def test_rearming_the_same_level_after_the_broker_cancelled_the_take_is_a_halt(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Тейк снят не нами (клиринг, срок, руками), движок вооружает тот же уровень — стоп.

    Ответ «уже стоит» на мёртвый тейк оставил бы позицию без защиты молча.
    Ожидаемое: остановка с причиной «снята не программой», второй лимитной
    заявки нет — робот стоит, решает человек (`B-077`).
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(110.0, LevelTouch.RISE))
            [take] = rig.broker.live()
            rig.broker.cancel_outside(take.key)
            await heard()
            rig.clock.now = BAR1 + timedelta(seconds=5)
            with pytest.raises(ExecutionHalted) as halted:
                await rig.executor.submit(arm(110.0, LevelTouch.RISE, at=BAR1))
            assert "снята не программой" in halted.value.human
            limits = [body for body in rig.creates() if body["orderType"] == "2"]
            assert len(limits) == 1, "снятый брокером тейк поставлен заново после остановки"

    run(scenario())


def test_reconcile_of_an_unconfirmed_take_the_broker_does_not_find_does_not_pass(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """B-079: тейк без единого статуса, а после связи брокер его по ключу не находит.

    Тейк принят ответом 200, но ни события, ни статуса по нему не было —
    робот уже стоит «брокер ничего не сообщил» (§2.2 п. 8). После связи
    статус — 404. Где заявка, неизвестно: сверка не прошла, позиция
    не сравнивается, в причине остановки — «не находит» и ключ.
    """

    async def scenario() -> None:
        gate, sleep = _gated()
        async with make_rig(tmp_path, store, broker, sleep=sleep) as rig:
            await long_open(rig)
            rig.broker.muted = True
            rig.broker.replace_route(STATUS, _StatusDown(503))
            with pytest.raises(ExecutionHalted):
                await rig.executor.submit(arm(110.0, LevelTouch.RISE))
            [take] = rig.broker.live()
            rig.broker.muted = False
            await _reconnect(rig, gate, lambda: None)
            down = _StatusDown(404)
            rig.broker.replace_route(STATUS, down)
            result = await reconcile(rig.executor, expected=1)
            assert down.calls, "статус на сверке не спрашивался — проверка пуста"
            text = result.halted or ""
            assert "не находит" in text and take.key in text, text
            assert rig.executor.unsettled
            assert result.broker is None, "позиция сравнивалась при невыясненном тейке"
            assert rig.broker.keys_sent("cancel") == Counter()

    run(scenario())


# --- R2 и B-080: сверка после связи и неотданное движку ---


def test_a_trailing_exit_during_the_relink_reconcile_is_not_a_mismatch(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """R2: сторож продаёт по догруженной минутке в ту же минуту, что идёт сверка после связи.

    Сверка и `watch` идут одновременно. Позиция робота с неотданной сделкой
    и портфель считаются одной операцией под замком исполнителя, поэтому
    продажа сторожа не вклинивается между ними: «закрыть всё» нет, робот
    не стоит (решение владельца «выйти и работать дальше», 0066), продажа
    одна, позиция у брокера ноль, движок получает выход по трейлингу.
    """

    async def scenario() -> None:
        gate, sleep = _gated()
        async with make_rig(tmp_path, store, broker, sleep=sleep) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(95.0, LevelTouch.FALL))

            def outage() -> None:
                rig.minutes.append(minute(BAR0 + timedelta(minutes=1), low=94.0))

            await _reconnect(rig, gate, outage)
            rig.exchange.plans.append(filled((1, 94.0)))
            result, _ = await asyncio.gather(
                reconcile(rig.executor, expected=1), rig.executor.watch()
            )
            await heard()
            assert result.halted is None, result.halted
            assert rig.executor.halted is None, rig.executor.halted
            assert _market_sells(rig) == [1]
            assert rig.broker.position(TICKER) == 0
            fill = only_fill(await rig.executor.fills_at(bar(BAR0)))
            assert (fill.action, fill.order_id, fill.price) == (
                OrderAction.CLOSE, take_name(), 94.0,
            )

    run(scenario())


def test_a_limit_exit_partly_filled_before_the_link_returned_is_not_a_mismatch_to_close(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """B-080: выход с пределом на 2 исполнен на 1 за обрыв — сверка без «закрыть всё».

    Позиция робота для сверки — лонг 2 движка минус исполненная часть
    выхода, то есть лонг 1, как у брокера. Заявка не снимается, рыночных
    продаж нет, заметка сверки называет частичное исполнение, а не «жива».
    """

    async def scenario() -> None:
        gate, sleep = _gated()
        async with make_rig(tmp_path, store, broker, sleep=sleep) as rig:
            await long_open(rig, volume=2)
            rig.clock.now = BAR1 + timedelta(seconds=5)
            await rig.executor.submit(leave(volume=2, limit=99.0))
            [limit] = rig.broker.live()
            await _reconnect(rig, gate, lambda: rig.broker.execute(limit.key, 1, 99.0))
            result = await reconcile(rig.executor, expected=2)
            assert result.halted is None, result.halted
            assert (result.broker, result.expected) == (1, 1)
            assert _market_sells(rig) == []
            assert rig.broker.keys_sent("cancel") == Counter()
            assert [order.key for order in rig.broker.live()] == [limit.key]
            await heard()
            assert any("исполнена на 1 из 2" in note for note in rig.notices), rig.notices

    run(scenario())


def test_a_reopened_status_stream_asks_for_a_pass_at_once(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Z1: поток статусов переоткрылся — исполнитель сразу просит проход (сверку), не ждёт бара.

    Первое открытие прохода не просит: сверку при старте делает сборка.
    Каждое следующее — просит (`Wiring.on_news`), даже без ждущего выхода
    сторожа, и порт запускает сверку в момент связи, а не на закрытии бара.
    """

    async def scenario() -> None:
        gate, sleep = _gated()
        news: list[int] = []
        async with make_rig(
            tmp_path, store, broker, sleep=sleep, on_news=lambda: news.append(1)
        ) as rig:
            await heard()
            assert news == [], "первое открытие потока просит проход"
            await _reconnect(rig, gate, lambda: None)
            await heard()
            assert news, "связь вернулась, а проход (сверку) никто не попросил"

    run(scenario())


# --- Н5: частые обрывы — ни строки, ни чтения портфеля раз в секунду ---


async def _drop_and_wait(rig: Rig) -> None:
    """Оборвать поток статусов и дождаться, пока исполнитель откроет его снова.

    Ждётся рост счёта открытий, а не «поток закрыт»: с нулевой паузой
    исполнитель переоткрывает поток быстрее, чем тест успевает увидеть обрыв.
    """
    links = rig.executor.links
    rig.broker.drop_stream()
    for _ in range(400):
        if rig.executor.links > links and rig.executor.connected:
            return
        await asyncio.sleep(0.005)
    raise AssertionError("поток не переоткрылся")


def _reads(rig: Rig, route: tuple[str, str]) -> int:
    """Сколько запросов к подделке ушло по этому адресу."""
    return sum(
        1 for request in rig.broker.requests if (request.method, request.url.path) == route
    )


def test_five_reconnects_with_a_live_take_say_alive_once_and_reconcile_each_time(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Н5, З1: пять обрывов подряд, тейк жив — заметка «жива» одна, сверка — каждый раз.

    Заметка о живой заявке говорится при смене её состояния, а не на каждое
    переоткрытие. Сверка не пропускается и не подделывается: статус тейка
    спрошен и портфель прочитан на каждом переоткрытии — «прошлая сверка была
    недавно» правдой о брокере не является. Робот не стоит, тейк у брокера
    тот же, снятий ноль.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            await rig.executor.submit(arm(110.0, LevelTouch.RISE))
            [take] = rig.broker.live()
            portfolio, status = _reads(rig, ("GET", PORTFOLIO_PATH)), _reads(rig, STATUS)
            for _ in range(5):
                await _drop_and_wait(rig)
                result = await reconcile(rig.executor, expected=1)
                assert result.halted is None, result.halted
            await heard()
            assert _reads(rig, STATUS) - status >= 5, "сверка пропущена на переоткрытии"
            read = _reads(rig, ("GET", PORTFOLIO_PATH)) - portfolio
            assert read == 5, "сверка без чтения портфеля"
            alive = [note for note in rig.notices if "жива" in note]
            assert len(alive) == 1, alive
            assert [order.key for order in rig.broker.live()] == [take.key]
            assert rig.broker.keys_sent("cancel") == Counter()

    run(scenario())


def test_a_link_that_neither_held_nor_was_reconciled_grows_the_pause(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """З1: обрыв сразу после открытия, без сверки — продолжение прежнего, пауза растёт.

    Три таких обрыва — паузы 0, 0,02, 0,04 с. Связь, продержавшаяся
    `_LINK_SETTLE` (здесь 0,3 с), — следующая пауза снова первая.
    """
    monkeypatch.setattr(live_executor, "_LINK_SETTLE", 0.3)
    limits = replace(FAST, reconnect_pauses=(0.0, 0.02, 0.04))

    async def scenario() -> list[float]:
        asked: list[float] = []

        async def sleep(seconds: float) -> None:
            asked.append(seconds)
            await asyncio.sleep(seconds)

        async with make_rig(tmp_path, store, broker, sleep=sleep, limits=limits) as rig:
            for _ in range(3):
                await _drop_and_wait(rig)
            await asyncio.sleep(0.4)
            await _drop_and_wait(rig)
            return asked

    asked = run(scenario())
    assert asked == [0.0, 0.02, 0.04, 0.0], asked


def test_reconnects_that_held_keep_the_first_pause(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """З1: обрывы раз в 50 с (здесь — связь держится дольше `_LINK_SETTLE`) — пауза не растёт.

    Каждая связь продержалась порог, сверка после неё прошла: пауза каждый
    раз первая (0 с), поток не лежит треть времени, как при сбросе по порогу
    свежести. Портфель прочитан каждой сверкой — по разу на переоткрытие.
    """
    monkeypatch.setattr(live_executor, "_LINK_SETTLE", 0.1)
    limits = replace(FAST, reconnect_pauses=(0.0, 0.5, 1.0))

    async def scenario() -> tuple[list[float], int]:
        asked: list[float] = []

        async def sleep(seconds: float) -> None:
            asked.append(seconds)
            await asyncio.sleep(seconds)

        async with make_rig(tmp_path, store, broker, sleep=sleep, limits=limits) as rig:
            await long_open(rig)
            portfolio = _reads(rig, ("GET", PORTFOLIO_PATH))
            for _ in range(4):
                await asyncio.sleep(0.15)
                await _drop_and_wait(rig)
                result = await reconcile(rig.executor, expected=1)
                assert result.halted is None, result.halted
            return asked, _reads(rig, ("GET", PORTFOLIO_PATH)) - portfolio

    asked, reads = run(scenario())
    assert asked == [0.0] * 4, f"пауза растёт при связи, державшейся порог: {asked}"
    assert reads == 4, reads


def test_a_storm_of_reconnects_grows_the_pause_even_when_each_reconcile_passes(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """З1, Н5: обрывы раз в секунду (здесь — чаще `_LINK_SETTLE`) — пауза растёт до потолка.

    Сверка после каждого обрыва проходит, но сброс паузы по ней — не чаще
    раза в порог: иначе шторм обрывов давал бы сверку с чтением портфеля
    на каждом. Пауза растёт до потолка, чтений портфеля — ровно по одному
    на сверку, ни одного лишнего.
    """
    monkeypatch.setattr(live_executor, "_LINK_SETTLE", 5.0)
    limits = replace(FAST, reconnect_pauses=(0.0, 0.02, 0.04))

    async def scenario() -> tuple[list[float], int]:
        asked: list[float] = []

        async def sleep(seconds: float) -> None:
            asked.append(seconds)
            await asyncio.sleep(seconds)

        async with make_rig(tmp_path, store, broker, sleep=sleep, limits=limits) as rig:
            await long_open(rig)
            portfolio = _reads(rig, ("GET", PORTFOLIO_PATH))
            for _ in range(5):
                await _drop_and_wait(rig)
                result = await reconcile(rig.executor, expected=1)
                assert result.halted is None, result.halted
            return asked, _reads(rig, ("GET", PORTFOLIO_PATH)) - portfolio

    asked, reads = run(scenario())
    # Первый обрыв — пауза первая (счёт с нуля), второй — сброс по прошедшей
    # сверке (прошлого сброса не было), дальше сброс не чаще порога: рост.
    assert asked == [0.0, 0.0, 0.02, 0.04, 0.04], f"пауза в шторм обрывов не растёт: {asked}"
    assert reads == 5, reads


async def _outage(rig: Rig, gate: asyncio.Event, while_down: Callable[[], None]) -> str | None:
    """Обрыв, `while_down` без связи, переоткрытие и сверка. Возврат — причина остановки.

    Пауза закрывается снова: следующий обрыв опять ждёт теста.
    """
    await _reconnect(rig, gate, while_down)
    gate.clear()
    result = await reconcile(rig.executor, expected=3)
    await heard()
    return result.halted


def test_a_live_take_that_changed_between_reconnects_is_told_again(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Н5: заметка «жива» — одна на **состояние** заявки, а не одна на всю жизнь заявки.

    Тейк на 3: за первый обрыв исполнен на 1, за второй — на 2, за третий
    ничего. Статус «исполнена частично» у брокера одинаков у первых двух —
    отличает их только исполненное. Стережёт: две заметки с «1 из 3»
    и «2 из 3», третьей нет; робот не стоит.
    """

    async def scenario() -> list[str]:
        gate, sleep = _gated()
        async with make_rig(tmp_path, store, broker, sleep=sleep) as rig:
            await long_open(rig, volume=3)
            await rig.executor.submit(arm(110.0, LevelTouch.RISE, volume=3))
            [take] = rig.broker.live()
            for done in (1, 1, 0):

                def fill(done: int = done) -> None:
                    if done:
                        rig.broker.execute(take.key, done, 110.0)

                halted = await _outage(rig, gate, fill)
                assert halted is None, halted
            return [note for note in rig.notices if "жива" in note]

    alive = run(scenario())
    assert len(alive) == 2, alive
    assert "1 из 3" in alive[0] and "2 из 3" in alive[1], alive


def test_a_take_cancelled_outside_after_it_was_told_alive_still_halts(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """Н5: «уже сказано, что жива» не отменяет сверку — снятый не нами тейк останавливает робота.

    Первый обрыв — тейк жив, заметка сказана. За второй тейк сняли
    в приложении брокера. Стережёт: после второй связи робот стоит словами
    «снята не программой».
    """

    async def scenario() -> tuple[str | None, str | None]:
        gate, sleep = _gated()
        async with make_rig(tmp_path, store, broker, sleep=sleep) as rig:
            await long_open(rig, volume=3)
            await rig.executor.submit(arm(110.0, LevelTouch.RISE, volume=3))
            [take] = rig.broker.live()
            first = await _outage(rig, gate, lambda: None)
            assert [note for note in rig.notices if "жива" in note], "заметки «жива» не было"
            return first, await _outage(rig, gate, lambda: rig.broker.cancel_outside(take.key))

    first, second = run(scenario())
    assert first is None, first
    assert second is not None and "снята не программой" in second, second
