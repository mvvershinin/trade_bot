"""Пара движков «факт» и «прогноз» — путями `app/`, на подставном брокере (Э4 2в).

План `.docs/plans/E4-live-orders.md` §8 п. 10, §12 «Что ломает test-expert»;
решение 0066. Всё идёт так, как пойдёт у владельца счёта: порт
(`HistoryPort`), живой ход (`app/observe.py`), сборка боевой стороны
(`app/live_trading.py`), боевой исполнитель (`app/live_executor.py`)
на `tests/fake_broker.py`. Движок напрямую тест не зовёт ни разу.

**Идеальная биржа** — правила названы здесь, а не взяты у брокера (план §8,
«Равенство в п. 10 — проверка перевода заявок и событий, а не брокера»):

* текущая цена в минуту `j` — открытие минуты `j`: заявка, поданная, пока
  идёт минута `j` (закрытие бара, проход по событию сделки), исполняется по
  ней, если исполнима; рыночная — всегда;
* первая сделка минуты `j` видна потоку котировок с той же минуты:
  исполняя заявку, биржа отдаёт порту растущую минутку `j`
  (`HistoryPort.growing_minute`), как поток после первого снимка;
* рыночный выход, поданный сторожем трейлинга, — по уровню «факта»:
  касание судится по размаху, как у модели;
* лимитная, неисполнимая при подаче, ждёт и исполняется по своему пределу
  в первой минуте не раньше минуты подачи, чей размах её достаёт.

Ряд — минутки с прогревом по базе (две сессии), живая часть пишется
по одной минутке, как пишет поток (`Source.BROKER`), и о каждой порт
узнаёт `live_candle`.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import math
import os
import pathlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any

import pytest
import websockets

os.environ.setdefault("QT_API", "pyside6")  # до первого импорта qasync

from app.fact import FactDesk, FactPosition, FactRequest, LiveStatus, signed_position
from app.live_trading import LiveDesk, LiveDesks
from app.port import HistoryPort
from backtest import HistoryRun
from broker.secret import Secret
from broker.session import BrokerSession
from broker.throttle import Throttle
from broker.token_store import TokenStore
from broker.tokens import TokenScope
from engine import ExitReason, Fill
from market import MSK, Candle, CandleStore, MarketWorker, Source
from market.commands import (
    BrokerCommand,
    BrokerExecution,
    CommandAction,
    CommandSide,
    CommandStatus,
)
from market.journal import RunOrigin, SessionRecord, redact
from tests.exchange_card import exchange
from tests.fake_broker import CREATE, BookOrder, FakeBroker, _unavailable
from tests.test_broker_session import FAKE_REFRESH, START, Clock, no_sleep
from ui.models import ChartData, DecisionRow, Layer, MinuteBarLimit, Mode
from ui.models import RunOrigin as WindowOrigin
from ui.models import Settings as UiSettings

SYMBOL = "MXZ6"
BASE = 250_000.0
STEP = 25.0
DAY = datetime(2026, 6, 19, tzinfo=MSK)  # пятница
LIVE_FROM = DAY.replace(hour=9)
LIVE_TO = DAY.replace(hour=11, minute=20)

#: Настройки владельца счёта (`userdata/settings.json`, 01.11.2026): свеча
#: 15 минут, скользящий уровень, окно 09:30–11:00, выход по времени с пределом.
OWNER = UiSettings(
    instrument=SYMBOL,
    timeframe="15 минут",
    trailing_enabled=True,
    trailing_start_pct=0.07,
    trailing_offset_pct=0.05,
    trailing_step_pct=0.01,
    window_end=time(11, 0),
    commission_per_side_rub=14.0,
)

#: Те же настройки с неподвижным тейком: уровень вооружается сделкой входа,
#: и только он проверяет проход по событию сделки (план §3).
FIXED_TAKE = dataclasses.replace(OWNER, trailing_enabled=False)

#: Без уровня: перевороты и выход по концу окна заявкой с пределом.
NO_TAKE = dataclasses.replace(OWNER, trailing_enabled=False, take_profit_enabled=False)


# --------------------------------------------------------------- ряд


def _path(moment: datetime) -> float:
    """Закрытие минутки: ход цены, по которому решает алгоритм.

    До 09:30 — около `BASE`, средняя около него же. 09:45 — закрытие выше
    средней: вход в лонг. 10:00 — ниже: переворот в шорт. 10:15 — шорт
    в прибыли на 0,1 %: скользящий уровень встаёт. Дальше — медленно вниз.
    """
    minutes = (moment - LIVE_FROM) / timedelta(minutes=1)
    if minutes < 0:
        index = (moment - (DAY - timedelta(days=1))) / timedelta(minutes=1)
        return BASE + 60.0 * math.sin(index / 23.0)
    marks = ((0, -50.0), (30, -50.0), (44, 100.0), (59, -200.0), (74, -450.0),
             (89, -440.0), (120, -500.0), (141, -500.0))
    for (left, low), (right, high) in zip(marks, marks[1:], strict=False):
        if left <= minutes <= right:
            share = (minutes - left) / (right - left)
            return BASE + low + (high - low) * share
    return BASE - 500.0


#: Минутки с особым размахом: вершина у 09:45 — неподвижный тейк лонга
#: задет в первой минуте бара входа и нигде больше; 10:29 — касание
#: скользящего уровня шорта в **последней** минуте бара: закрытая свеча
#: обязана дойти до «факта» только после записи этой минутки.
LAST_MINUTE_TOUCH = LIVE_FROM + timedelta(minutes=89)
_SPIKES: dict[datetime, tuple[float, float]] = {
    LIVE_FROM + timedelta(minutes=45): (BASE + 100.0, BASE + 2100.0),
    LAST_MINUTE_TOUCH: (BASE - 450.0, BASE - 300.0),
}

#: Касание того же уровня в середине бара (10:20) — вместо последней минуты:
#: его видит только сторож на записанной минутке, а проверка на закрытии
#: бара продала бы позже и по другой цене.
MID_BAR_TOUCH = (LIVE_FROM + timedelta(minutes=80), (BASE - 450.0, BASE - 300.0))


def minute(moment: datetime, *, growing: bool = False) -> Candle:
    """Минутка ряда. Растущая — первая сделка минуты: всё по открытию."""
    opened = _path(moment - timedelta(minutes=1))
    closed = _path(moment)
    low, high = min(opened, closed) - 10.0, max(opened, closed) + 10.0
    if moment in _SPIKES:
        low, high = _SPIKES[moment]
    if growing:
        return Candle(
            time=moment, open=opened, high=opened, low=opened, close=opened,
            volume=1.0, unsettled=True,
        )
    return Candle(time=moment, open=opened, high=high, low=low, close=closed, volume=1.0)


def _history(path: pathlib.Path) -> None:
    """Прогрев в базе: прошлая сессия и утро до живой части."""
    eve = DAY - timedelta(days=1)
    sessions = (
        eve.replace(hour=7), eve.replace(hour=23, minute=50), DAY.replace(hour=7), LIVE_FROM
    )
    rows: list[Candle] = []
    for since, until in zip(sessions[::2], sessions[1::2], strict=True):
        moment = since
        while moment < until:
            rows.append(minute(moment))
            moment += timedelta(minutes=1)
    with CandleStore(path) as store:
        store.put_minutes(SYMBOL, rows, Source.ISS)


# --------------------------------------------------------------- биржа


@dataclass
class Exchange:
    """Идеальная биржа (правила — в шапке модуля) поверх книги подделки."""

    broker: FakeBroker
    #: Идущая минута и момент «сейчас» — общие часы теста.
    current: datetime = LIVE_FROM
    now: datetime = LIVE_FROM
    #: Последняя записанная минутка: по ней сторож трейлинга судил касание.
    written: Candle | None = None
    port: HistoryPort | None = None
    revealed: set[datetime] = field(default_factory=set)
    #: Лимитные, ждущие касания: ключ → минута подачи.
    resting: dict[str, datetime] = field(default_factory=dict)
    #: Проскальзывание рыночной заявки против открытия — в пунктах, против
    #: стороны заявки. Ноль — биржа идеальна; не ноль — тень и факт расходятся.
    slip: float = 0.0
    #: Уровень сторожит программа (скользящий): рыночный выход на касании —
    #: по уровню. Неподвижный тейк — лимитная у брокера, правило не для него.
    guarded: bool = False

    def __post_init__(self) -> None:
        self.broker.on_create = self.created
        self.broker.clock = lambda: self.now

    def reveal(self) -> None:
        """Первая сделка идущей минуты видна потоку — растущая минутка порту."""
        if self.current in self.revealed or self.port is None:
            return
        self.revealed.add(self.current)
        self.port.growing_minute(SYMBOL, minute(self.current, growing=True))

    def created(self, order: BookOrder) -> None:
        self.reveal()
        price = minute(self.current).open
        if order.order_type == "1":
            level = self._guard_level(order)
            worse = price + (self.slip if order.side == "1" else -self.slip)
            self.broker.execute(order.key, order.quantity, worse if level is None else level)
            return
        assert order.price is not None
        if self._fits(order, low=price, high=price):
            self.broker.execute(order.key, order.quantity, price)
            return
        self.resting[order.key] = self.current

    def trade(self, bar: Candle) -> None:
        """Минута `bar` прошла: ждущие лимитные, которых она достала, — по пределу."""
        for key, placed in list(self.resting.items()):
            book = self.broker.order(key)
            if book is None or not book.live:
                del self.resting[key]
                continue
            if placed <= bar.time and self._fits(book, low=bar.low, high=bar.high):
                assert book.price is not None
                self.broker.execute(key, book.quantity, book.price)
                del self.resting[key]

    def _guard_level(self, order: BookOrder) -> float | None:
        """Уровень «факта», если это его выход и последняя минутка уровень задела."""
        if not self.guarded or self.port is None or self.written is None:
            return None
        position = self.port.live_status().position
        if position is None or position.take_level is None:
            return None
        if order.side != ("2" if position.signed > 0 else "1"):
            return None
        level, bar = position.take_level, self.written
        touched = bar.low <= level if position.signed > 0 else bar.high >= level
        return level if touched else None

    @staticmethod
    def _fits(order: BookOrder, *, low: float, high: float) -> bool:
        assert order.price is not None
        return high >= order.price if order.side == "2" else low <= order.price


# --------------------------------------------------------------- стенд


@dataclass
class Bench:
    """Порт с потоком, боевой стороной на подделке и идеальной биржей."""

    port: HistoryPort
    broker: FakeBroker
    exchange: Exchange
    worker: MarketWorker
    notes: list[tuple[str, str]]
    desks: LiveDesks

    def events(self) -> list[str]:
        return [event for event, _ in self.notes]

    def said(self, event: str) -> str:
        return " ".join(reason for name, reason in self.notes if name == event)


def _token(userdata: pathlib.Path, scope: TokenScope) -> TokenStore:
    userdata.mkdir(exist_ok=True)
    store = TokenStore(userdata)
    store.save(Secret(FAKE_REFRESH), scope, issued_at=START - timedelta(days=1))
    return store


async def _bench(
    tmp_path: pathlib.Path,
    broker: FakeBroker,
    values: UiSettings,
    *,
    scope: TokenScope = TokenScope.TRADE,
    before: Callable[[MarketWorker], Awaitable[None]] | None = None,
) -> tuple[Bench, BrokerSession]:
    path = tmp_path / "candles.sqlite3"
    _history(path)
    worker = MarketWorker(path)
    await worker.open()
    if before is not None:
        await before(worker)
    ex = Exchange(broker, guarded=values.trailing_enabled)
    session = BrokerSession(
        _token(tmp_path / "userdata", scope), client=broker.client(), clock=Clock(),
        sleep=no_sleep, attempts=3, throttle=Throttle(rate=1000.0),
        order_throttle=Throttle(rate=1000.0),
    )
    port = HistoryPort(
        worker, values=values, days=0, sanitize=redact, clock=lambda: ex.now
    )
    ex.port = port
    notes: list[tuple[str, str]] = []
    port.decision_appended.connect(
        lambda row: notes.append((getattr(row, "event", ""), getattr(row, "reason", "")))
    )
    port.attach_exchange(exchange(STEP))
    port.attach_stream(lambda on: None)
    desks = LiveDesks(lambda: session, worker, class_code="SPBFUT", clock=lambda: ex.now)
    port.attach_live(desks)
    return Bench(port, broker, ex, worker, notes, desks), session


async def _close(bench: Bench, session: BrokerSession) -> None:
    await bench.port.aclose()
    await session.close()
    await bench.worker.close()


async def _connect(bench: Bench, *, live: bool = True) -> None:
    """Поток включён, прогон прошёл, бой попрошен — к 09:00."""
    bench.exchange.now = LIVE_FROM - timedelta(seconds=58)
    bench.port.stream(True)
    await bench.port.wait()
    if live:
        bench.port.set_live(True)
        await bench.port.wait()


async def _ride(bench: Bench, until: datetime = LIVE_TO, since: datetime = LIVE_FROM) -> None:
    """Живая часть: минутка за минуткой, как пишет поток. `since` — продолжить с этой."""
    ex, port = bench.exchange, bench.port
    moment = since
    while moment <= until:
        # Первый снимок минуты `moment` закрывает прошлую: она — в базу.
        ex.current, ex.now = moment, moment + timedelta(seconds=2)
        if moment > LIVE_FROM:
            ex.written = minute(moment - timedelta(minutes=1))
            await bench.worker.put_minutes(SYMBOL, [ex.written], Source.BROKER)
            port.live_candle(SYMBOL)
            await port.wait()
        ex.reveal()
        await port.wait()
        ex.now = moment + timedelta(seconds=30)
        ex.trade(minute(moment))
        await port.wait()
        moment += timedelta(minutes=1)


def _deals(run: HistoryRun | None) -> list[tuple[object, ...]]:
    """Сделки без денег брокера: у подделки комиссии нет, у модели — тариф."""
    assert run is not None
    return [
        (deal.side, deal.volume, deal.entry_time, deal.entry_price, deal.entry_order_id,
         deal.exit_time, deal.exit_price, deal.exit_order_id, deal.exit_reason)
        for deal in run.deals
    ]


def _fills(run: HistoryRun | None) -> list[tuple[object, ...]]:
    assert run is not None
    return [
        (one.action, one.side, one.volume, one.price, one.at, one.order_id)
        for one in run.fills
        if isinstance(one, Fill)
    ]


@dataclass
class _Spy:
    """Боевая сторона, которая запоминает запрос порта и собирает настоящую."""

    real: LiveDesks
    requests: list[FactRequest]

    def refusal(self) -> str | None:
        return self.real.refusal()

    async def open(self, request: FactRequest) -> FactDesk:
        self.requests.append(request)
        return await self.real.open(request)


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch) -> FakeBroker:
    fake = FakeBroker()
    monkeypatch.setattr(websockets, "connect", fake.connect)
    return fake


# --------------------------------------------------------------- равенство


@dataclass(frozen=True)
class Case:
    """Настройки, причины выхода, которые обязан дать «прогноз», и касание в середине бара."""

    values: UiSettings
    exits: frozenset[ExitReason]
    mid_bar: bool = False


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            Case(OWNER, frozenset({ExitReason.SIGNAL, ExitReason.TRAILING_TAKE})),
            id="owner-trailing-touch-in-the-last-minute",
        ),
        pytest.param(
            Case(OWNER, frozenset({ExitReason.SIGNAL, ExitReason.TRAILING_TAKE}), mid_bar=True),
            id="owner-trailing-touch-mid-bar",
        ),
        pytest.param(
            Case(FIXED_TAKE, frozenset({ExitReason.TAKE_PROFIT})), id="take-in-the-entry-bar"
        ),
        pytest.param(
            Case(NO_TAKE, frozenset({ExitReason.SIGNAL, ExitReason.WINDOW_END})),
            id="window-end-limit",
        ),
    ],
)
def test_fact_deals_equal_forecast_deals_on_an_ideal_broker(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch, case: Case
) -> None:
    """§8 п. 10: на идеальной бирже сделки «факта» = сделкам «прогноза» — путями `app/`.

    Стережёт перевод заявок и событий пары: один прогрев в «Выключен»,
    одно включение на одной свече, один цикл подачи свечей, проход по событию
    сделки. Ряд владельца (15 минут, трейлинг) — переворот и выход сторожем
    программы; ряд с неподвижным тейком сходится, только если сделка входа
    дошла до «факта» проходом по событию: тейк встаёт у брокера в первую
    минуту бара входа, где его и задевает цена (план §3). Без прохода тейк
    встанет минутой позже — касания уже не будет. Разный прогрев разводит
    средние, и вход «факта» уходит на другую свечу. Касание уровня в последней
    минуте бара сходится, только если закрытая свеча дошла до «факта» после
    записи этой минутки; касание в середине бара — только если сторож смотрит
    каждую записанную минутку.
    """
    if case.mid_bar:
        monkeypatch.delitem(_SPIKES, LAST_MINUTE_TOUCH)
        monkeypatch.setitem(_SPIKES, *MID_BAR_TOUCH)

    async def go() -> tuple[HistoryRun | None, HistoryRun | None, int, list[str]]:
        bench, session = await _bench(tmp_path, broker, case.values)
        try:
            await _connect(bench)
            await _ride(bench)
            status = bench.port.live_status()
            return status.fact, status.forecast, broker.position(SYMBOL), bench.events()
        finally:
            await _close(bench, session)

    fact, forecast, at_broker, events = loop.run_until_complete(go())
    assert "Бой включён" in events, events
    assert forecast is not None and forecast.deals, "у прогноза нет сделок — сравнивать нечего"
    assert {deal.exit_reason for deal in forecast.deals} == case.exits, _deals(forecast)
    assert _deals(fact) == _deals(forecast)
    assert _fills(fact) == _fills(forecast)
    # Один прогрев в «Выключен» и одно включение: журналы пары совпадают
    # событиями и временем, а не только сделками. Расходятся они только
    # словами о комиссии — у подделки её нет.
    assert fact is not None
    assert [(one.at, one.event) for one in fact.journal] == [
        (one.at, one.event) for one in forecast.journal
    ]
    assert fact is not None and not fact.halted, fact.halted
    held = fact.position
    signed = 0 if held is None else signed_position(held.side, held.volume)
    assert at_broker == signed, "позиция у брокера разошлась с «фактом»"


# --------------------------------------------------------------- отказы включения


def test_live_is_refused_with_a_position_at_the_broker(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """0066: бой включается только при нулевой позиции у брокера — иначе отказ словами.

    Позиция открыта мимо программы (руками в приложении брокера). Ни одной
    команды брокеру, бой не включён, ход остался наблюдением, причина —
    в журнале и в снимке боя: позиция названа словами.
    """

    async def go() -> tuple[Bench, list[tuple[str, str]]]:
        broker.outside = 2
        bench, session = await _bench(tmp_path, broker, OWNER)
        try:
            await _connect(bench)
            return bench, list(bench.notes)
        finally:
            await _close(bench, session)

    bench, notes = loop.run_until_complete(go())
    assert broker.commands == [], broker.commands
    refused = " ".join(reason for event, reason in notes if event == "Бой не включён")
    assert "лонг 2" in refused and "нулевой позиции" in refused, refused
    status = bench.port.live_status()
    assert not status.on and not status.wanted
    assert "лонг 2" in status.refused


def test_live_is_refused_with_an_unsettled_command(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """0066: незакрытая команда журнала, чью судьбу брокер не назвал, — бой не включается.

    Команда прошлого запуска записана и не закрыта; статус по ключу брокер
    не отдаёт (503). Новой заявки нет, причина словами — «судьба команды».
    """

    async def plant(worker: MarketWorker) -> None:
        def write(store: CandleStore) -> None:
            run = store.open_journal_session(
                SessionRecord(RunOrigin.LIVE, symbol=SYMBOL), now=DAY.replace(hour=8)
            )
            store.record_broker_command(run.id, BrokerCommand(
                client_order_id="00000000-0000-4000-8000-0000000000aa",
                action=CommandAction.CREATE, order_id="open:long:прошлый",
                symbol=SYMBOL, side=CommandSide.BUY, volume=1, price=None,
                account=broker.account,
            ))

        await worker.call(write)

    async def go() -> list[tuple[str, str]]:
        broker.status_answers.extend([_unavailable] * 5)
        bench, session = await _bench(tmp_path, broker, OWNER, before=plant)
        try:
            await _connect(bench)
            return list(bench.notes)
        finally:
            await _close(bench, session)

    notes = loop.run_until_complete(go())
    assert [kind for kind, _ in broker.commands] == [], broker.commands
    refused = " ".join(reason for event, reason in notes if event == "Бой не включён")
    assert "Судьбу команды" in refused, refused


def test_live_is_unavailable_on_a_read_only_token(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """§8 п. 17: токен для чтения — бой недоступен до любой сборки, причина словами.

    Снимок боя говорит «нельзя» и называет токен; нажатие отказывает тем же
    текстом; к брокеру не уходит ни одного запроса заявок.
    """

    async def go() -> tuple[LiveStatus, list[tuple[str, str]]]:
        bench, session = await _bench(tmp_path, broker, OWNER, scope=TokenScope.READ_ONLY)
        try:
            await _connect(bench, live=False)
            status = bench.port.live_status()
            bench.port.set_live(True)
            await bench.port.wait()
            return status, list(bench.notes)
        finally:
            await _close(bench, session)

    status, notes = loop.run_until_complete(go())
    assert not status.available
    assert any("только для чтения" in why for why in status.why_not)
    refused = " ".join(reason for event, reason in notes if event == "Бой не включён")
    assert "только для чтения" in refused, refused
    assert broker.commands == []


# --------------------------------------------------------------- пересборка и остановка


def test_a_rebuild_of_the_fact_with_an_open_position_is_held(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """С4: смена периода средней при открытой позиции «факта» его не пересобирает.

    Пересобранный «факт» родился бы без позиции, и настоящий лонг у брокера
    остался бы без робота. Стережёт: позиция у «факта» та же, лонг у брокера
    тот же, новых команд нет, строка «Пересборка боя отложена» названа
    с позицией словами. После выхода по концу окна (позиции нет, команд
    нет) та же смена применяется сама: бой включается заново.
    """

    async def go() -> tuple[object, object, int, int, list[str], str]:
        bench, session = await _bench(tmp_path, broker, NO_TAKE)
        try:
            await _connect(bench)
            await _ride(bench, LIVE_FROM + timedelta(minutes=50))
            before = bench.port.live_status().position
            sent = len(broker.commands)
            bench.port.apply_settings(dataclasses.replace(NO_TAKE, average_period=20))
            await bench.port.wait()
            after = bench.port.live_status()
            moved, at_broker = len(broker.commands) - sent, broker.position(SYMBOL)
            held = bench.said("Пересборка боя отложена")
            await _ride(bench)
            return before, after.position, moved, at_broker, bench.events(), held
        finally:
            await _close(bench, session)

    before, after, moved, at_broker, events, held = loop.run_until_complete(go())
    assert before is not None, "к 09:50 «факт» обязан быть в лонге — проверять нечего"
    assert after == before, "пересборка сбросила позицию «факта»"
    assert moved == 0
    assert at_broker == 1
    assert "лонг 1" in held, events
    assert events.count("Бой включён") == 2, "после выхода пересборка не случилась"


def test_an_executor_halt_reaches_the_robot_halt_in_its_words(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """Остановка боевого исполнителя — в общий путь остановки робота его словами.

    Сделка по нашему тикеру, которой программа не подавала (заявка руками
    в приложении брокера): исполнитель встаёт, и порт поднимает остановку
    вида «состояние счёта» с **текстом исполнителя** — в окне и в таблице
    `robot_halt` (переживает перезапуск). Новых заявок робот не подаёт.
    «Возобновить» пересобирает исполнитель **после сверки** (С3): чужой лонг
    у брокера остался — бой не включается, и это сказано словами.
    """

    async def go() -> tuple[str, list[str], int, str]:
        bench, session = await _bench(tmp_path, broker, NO_TAKE)
        try:
            await _connect(bench)
            broker.foreign_trade("1", 1, BASE)
            await asyncio.sleep(0.2)
            await bench.port.wait()
            await bench.port.wait()
            stored = [cause.reason for cause in await bench.worker.standing_halt()]
            sent = len([one for one in broker.commands if one[0] == "create"])
            halted = bench.port.halted
            bench.port.resume()
            await bench.port.wait()
            return halted, stored, sent, bench.said("Бой не включён")
        finally:
            await _close(bench, session)

    halted, stored, sent, after_resume = loop.run_until_complete(go())
    words = "прошла сделка, которой программа не подавала"
    assert words in halted, halted
    assert any(words in reason for reason in stored), stored
    assert sent == 0
    assert "лонг 1" in after_resume, after_resume


def test_after_a_restart_on_a_take_day_neither_engine_enters(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """Р7: тейк был сегодня в прошлом запуске — после «перезапуска» не входит никто из пары.

    Ни «факт», ни «прогноз».

    День берётся из таблицы исполнений счёта (вход и выход по тейку утром,
    прошлым запуском) и кладётся обоим движкам пары. Стережёт: ни одной
    заявки брокеру за всё окно, ни одной сделки «прогноза», строка «День
    до запуска принят» говорит про тейк.
    """
    keys = ("00000000-0000-4000-8000-0000000000b1", "00000000-0000-4000-8000-0000000000b2")

    async def plant(worker: MarketWorker) -> None:
        def write(store: CandleStore) -> None:
            run = store.open_journal_session(
                SessionRecord(RunOrigin.LIVE, symbol=SYMBOL), now=DAY.replace(hour=7)
            )
            morning = DAY.replace(hour=7, minute=10)
            for key, name, side, price, shift in (
                (keys[0], "open:long:утро", CommandSide.BUY, BASE, 0),
                (keys[1], f"take:long:{morning.isoformat()}", CommandSide.SELL, BASE + 300, 5),
            ):
                store.record_broker_command(run.id, BrokerCommand(
                    client_order_id=key, action=CommandAction.CREATE, order_id=name,
                    symbol=SYMBOL, side=side, volume=1, price=None, account=broker.account,
                ))
                store.record_broker_execution(BrokerExecution(
                    execution_id=f"EXEC-{key[-2:]}", client_order_id=key, volume=1,
                    price=price, at=morning + timedelta(minutes=shift), commission=14.0,
                ))
                store.set_broker_command_status(key, CommandStatus.FILLED, reason="")

        await worker.call(write)

    async def go() -> tuple[LiveStatus, int]:
        bench, session = await _bench(tmp_path, broker, NO_TAKE, before=plant)
        try:
            await _connect(bench)
            await _ride(bench)
            created = len([one for one in broker.commands if one[0] == "create"])
            return bench.port.live_status(), created
        finally:
            await _close(bench, session)

    status, created = loop.run_until_complete(go())
    assert created == 0, "после тейка прошлого запуска «факт» подал заявку"
    assert status.forecast is not None and not status.forecast.deals, _deals(status.forecast)
    for run in (status.fact, status.forecast):
        assert run is not None
        day = " ".join(one.reason for one in run.journal if one.event == "День до запуска принят")
        assert "тейк в этот день уже был" in day, day


def test_the_executor_waits_for_a_limit_exit_as_many_bars_as_the_engine(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """Одно число — одно место: свечи ожидания выхода с пределом у исполнителя — из движка.

    `LiveLimits.limit_exit_bars` берётся из `time_exit_wait_bars` настроек,
    прочие пределы — из окна (`convert.live_limits`). Разойдись они, остановку
    раньше назвал бы движок, и текст не сказал бы о живой заявке у брокера (С9).
    """
    values = dataclasses.replace(OWNER, time_exit_wait_bars=2, live_fill_wait_sec=7)
    requests: list[FactRequest] = []

    async def go() -> None:
        bench, session = await _bench(tmp_path, broker, values)
        bench.port.attach_live(_Spy(bench.desks, requests))
        try:
            await _connect(bench)
        finally:
            await _close(bench, session)

    loop.run_until_complete(go())
    [request] = requests
    assert request.limits.limit_exit_bars == 2
    assert request.limits.fill_wait == 7


def test_the_executor_notice_becomes_a_journal_line(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """`Wiring.notice` — строка журнала решений: «уровень пройден, выход ждёт связи».

    Исполнитель говорит человеку без остановки (С2); сказанное обязано
    дойти до окна, а не остаться строкой технического лога.
    """
    requests: list[FactRequest] = []
    words = "Цена прошла уровень трейлинга 249675, а выход по рынку подать нельзя"

    async def go() -> list[tuple[str, str]]:
        bench, session = await _bench(tmp_path, broker, OWNER)
        bench.port.attach_live(_Spy(bench.desks, requests))
        try:
            await _connect(bench)
            requests[0].notice(words)
            return list(bench.notes)
        finally:
            await _close(bench, session)

    notes = loop.run_until_complete(go())
    assert ("Боевой исполнитель", words) in notes, notes[-3:]


def test_the_forecast_layer_is_the_shadow_and_its_journal_is_marked_simulation(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """В бою слой «Прогноз» — ход теневого движка, слой «Факт» — сделки брокера (§8).

    Биржа исполняет рыночные с проскальзыванием 50 пунктов: тень и факт
    расходятся ценой. Стережёт: метки «Прогноза» на графике — исполнения
    тени (имя заявки и цена), метки «Факта» — исполнения брокера, и они
    различаются; строки журнала тени видны окну с меткой «симуляция»,
    строки «факта» — с меткой «боевой режим», каждая лента целиком.
    """
    charts: list[ChartData] = []
    journals: list[tuple[DecisionRow, ...]] = []

    async def go() -> LiveStatus:
        bench, session = await _bench(tmp_path, broker, NO_TAKE)
        bench.exchange.slip = 50.0
        bench.port.chart_replaced.connect(charts.append)
        bench.port.decisions_replaced.connect(journals.append)
        try:
            await _connect(bench)
            await _ride(bench)
            return bench.port.live_status()
        finally:
            await _close(bench, session)

    status = loop.run_until_complete(go())
    fact, forecast = status.fact, status.forecast
    assert fact is not None and forecast is not None and forecast.fills
    chart = charts[-1]
    # Время метки — бар исполнения: у тени и у брокера он один, а расчёт
    # самого «факта» (цена закрытия свечи решения) стоял бы на баре раньше.
    planned = {
        (one.pair_id, one.time, one.price) for one in chart.markers if one.layer is Layer.PLAN
    }
    done = {(one.pair_id, one.time, one.price) for one in chart.markers if one.layer is Layer.FACT}
    assert planned == {(one.order_id, one.at, one.price) for one in forecast.fills}
    assert done == {(one.order_id, one.at, one.price) for one in fact.fills}
    assert planned != done, "проскальзывание есть, а слои совпали"
    # Строки движков — в начале журнала, строки программы — следом (`note`).
    journal = journals[-1][: len(fact.journal) + len(forecast.journal)]
    assert [row.event for row in journal if row.origin is WindowOrigin.PAPER] == [
        one.event for one in forecast.journal
    ]
    assert [row.event for row in journal if row.origin is WindowOrigin.LIVE] == [
        one.event for one in fact.journal
    ]


def test_stop_in_live_turns_the_live_off_and_keeps_the_position(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """«Стоп» в бою выключает бой (решение владельца 01.11.2026, 0066).

    Прежде «Стоп» отвечал «боевого хода ещё нет», а бой шёл дальше — окно
    получало ложь. Стережёт: после «Стоп» бой выключен, ни одной команды
    брокеру, строка «Бой выключен» с позицией словами, позиция у брокера
    не закрыта, отказа с текстом `NO_LIVE` нет.
    """
    from app.port import NO_LIVE

    async def go() -> tuple[LiveStatus, int, list[tuple[str, str]]]:
        bench, session = await _bench(tmp_path, broker, NO_TAKE)
        try:
            await _connect(bench)
            await _ride(bench, LIVE_FROM + timedelta(minutes=50))
            assert bench.port.live_status().on, "бой не включился — проверять нечего"
            sent = len(broker.commands)
            bench.notes.clear()
            bench.port.stop()
            await bench.port.wait()
            return bench.port.live_status(), len(broker.commands) - sent, list(bench.notes)
        finally:
            await _close(bench, session)

    status, sent, notes = loop.run_until_complete(go())
    assert not status.on and not status.wanted
    assert sent == 0, "«Стоп» подал команду брокеру"
    off = " ".join(reason for event, reason in notes if event == "Бой выключен")
    assert "лонг 1" in off and "не закрывает" in off, notes
    assert broker.position(SYMBOL) == 1
    assert not any(NO_LIVE in reason for _, reason in notes), notes


def test_mode_off_keeps_guarding_the_frozen_trailing_level(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Режим «Выключен» в бою: застывший уровень трейлинга сторожится (0066, 01.11.2026).

    Шорт, скользящий уровень встал на закрытии 10:15; в 10:16 — «Выключен».
    Дальше цена идёт в пользу шорта (в рабочем режиме уровень подтянулся бы),
    а в 11:05 минутка задевает уровень, вставший до выключения. Стережёт:
    уровень после «Выключен» не двигается; на касании — ровно одна рыночная
    покупка по уровню; движок видит выход, позиции у «факта» и у брокера нет.
    """
    monkeypatch.delitem(_SPIKES, LAST_MINUTE_TOUCH)
    touch = LIVE_FROM + timedelta(minutes=125)
    monkeypatch.setitem(_SPIKES, touch, (BASE - 510.0, BASE - 300.0))
    off_at = LIVE_FROM + timedelta(minutes=76)

    async def go() -> tuple[object, object, list[dict[str, object]], LiveStatus]:
        bench, session = await _bench(tmp_path, broker, OWNER)
        try:
            await _connect(bench)
            await _ride(bench, off_at)
            armed = bench.port.live_status().position
            bench.port.set_mode(Mode.OFF)
            await bench.port.wait()
            sent = len(broker.requests)
            await _ride(bench, touch - timedelta(minutes=1), since=off_at + timedelta(minutes=1))
            frozen = bench.port.live_status().position
            await _ride(bench, since=touch)
            bodies = [
                json.loads(request.content) for request in broker.requests[sent:]
                if (request.method, request.url.path) == CREATE
            ]
            return armed, frozen, bodies, bench.port.live_status()
        finally:
            await _close(bench, session)

    armed, frozen, bodies, status = loop.run_until_complete(go())
    assert isinstance(armed, FactPosition) and armed.take_level is not None, armed
    assert armed.signed < 0, "к 10:16 «факт» обязан быть в шорте со скользящим уровнем"
    assert isinstance(frozen, FactPosition)
    assert frozen.take_level == armed.take_level, "уровень сдвинулся после «Выключен»"
    best_close = min(_path(LIVE_FROM + timedelta(minutes=m)) for m in (104, 119))
    # В рабочем режиме лучшая цена после выключения подтянула бы уровень
    # больше чем на шаг подтяжки — иначе проверка «не сдвинулся» пуста.
    assert armed.take_level - best_close > 125.0 + 25.0, best_close
    assert [(body["side"], body["orderType"]) for body in bodies] == [("1", "1")], bodies
    assert status.position is None, "движок не увидел выход по уровню"
    assert broker.position(SYMBOL) == 0
    assert status.fact is not None
    assert status.fact.deals[-1].exit_reason is ExitReason.TRAILING_TAKE
    assert status.fact.deals[-1].exit_price == armed.take_level


# ------------------------------------------- /risk 2в: БЛОК 1, БЛОК 2, R1, R4, R5

#: Тейк лонга у брокера живёт: вершина 09:45 убрана, уровень не задет.
ENTRY_SPIKE = LIVE_FROM + timedelta(minutes=45)


Takes = list[tuple[str, float | None]]


def _takes(broker: FakeBroker) -> Takes:
    """Живые лимитные заявки у брокера: (сторона, цена)."""
    return [(order.side, order.price) for order in broker.live() if order.order_type == "2"]


@pytest.mark.parametrize("how", ["switched-off-and-on", "halted-and-resumed"])
def test_reenabling_with_a_position_leaves_its_take_at_the_broker(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    """БЛОК 1: повторное включение боя при позиции — отказ **до** любой команды брокеру.

    Лонг с тейком у брокера; бой выключили (или он встал и его возобновили) —
    и снова включают. Позиция у брокера читается первой: не ноль — отказ,
    ни одного снятия, тейк жив у брокера; отказ называет позицию, говорит,
    что тейк не тронут, и «команд брокеру не было» — потому что их не было.
    """
    monkeypatch.delitem(_SPIKES, ENTRY_SPIKE)

    async def go() -> tuple[Takes, Takes, int, str]:
        bench, session = await _bench(tmp_path, broker, FIXED_TAKE)
        try:
            await _connect(bench)
            await _ride(bench, LIVE_FROM + timedelta(minutes=50))
            before = _takes(broker)
            if how == "switched-off-and-on":
                bench.port.set_live(False)
                await bench.port.wait()
                bench.port.set_live(True)
            else:
                bench.port.halt("Проба остановки", "Проба: робот остановлен извне боя.")
                await bench.port.wait()
                bench.port.resume()
            await bench.port.wait()
            cancels = sum(broker.keys_sent("cancel").values())
            return before, _takes(broker), cancels, bench.said("Бой не включён")
        finally:
            await _close(bench, session)

    before, after, cancels, refused = loop.run_until_complete(go())
    assert before, "тейка у брокера нет — проверять нечего"
    assert cancels == 0, "при позиции у брокера снята заявка"
    assert after == before, "тейк позиции снят"
    assert broker.position(SYMBOL) == 1
    assert "лонг 1" in refused and "тейк" in refused, refused
    assert "команд брокеру не было" in refused, refused


def test_mode_off_reaches_the_fact_while_a_rebuild_is_held(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """БЛОК 2: пересборка отложена (период средней при лонге), затем «Выключен» — сразу.

    Режим — настройка движка: к «факту» он идёт на ходу и при отложенной
    пересборке. Стережёт: после «Выключен» ни одной заявки брокеру, хотя
    в 10:00 рабочий режим перевернул бы лонг в шорт; позиция у «факта»
    и у брокера та же; сказано «Позиция без управления»; строка про
    отложенную пересборку не обещает того, чего нет.
    """

    async def go() -> tuple[int, object, list[str], str]:
        bench, session = await _bench(tmp_path, broker, NO_TAKE)
        try:
            await _connect(bench)
            await _ride(bench, LIVE_FROM + timedelta(minutes=50))
            bench.port.apply_settings(dataclasses.replace(NO_TAKE, average_period=20))
            await bench.port.wait()
            bench.port.set_mode(Mode.OFF)
            await bench.port.wait()
            sent = len(broker.commands)
            await _ride(
                bench, LIVE_FROM + timedelta(minutes=70), since=LIVE_FROM + timedelta(minutes=51)
            )
            return (len(broker.commands) - sent, bench.port.live_status().position,
                    bench.events(), bench.said("Пересборка боя отложена"))
        finally:
            await _close(bench, session)

    sent, position, events, held = loop.run_until_complete(go())
    assert sent == 0, "после «Выключен» «факт» подал заявку"
    assert isinstance(position, FactPosition) and position.signed == 1
    assert broker.position(SYMBOL) == 1
    assert "Позиция без управления" in events, events
    assert "Период средней: 15 → 20" in held and "уже действуют" in held, held


def test_apply_in_live_refuses_what_the_live_switch_refuses(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """R1: в бою «Применить» проверяет запреты включения — D-141 с порогом минуток.

    Бой без скользящего уровня на свече 15 минут при пороге проверки по
    минуткам 15: включить уровень на ходу — отказ под полем, бой идёт
    по прежним настройкам. Без проверки уровень заработал бы в бою, где
    сделки систематически расходятся с прогоном (D-141).
    """
    small = dataclasses.replace(FIXED_TAKE, minute_bar_limit=MinuteBarLimit.FIFTEEN)
    refused: list[tuple[str, str]] = []

    async def go() -> tuple[UiSettings, LiveStatus]:
        bench, session = await _bench(tmp_path, broker, small)
        bench.port.settings_refused.connect(
            lambda _values, reason, name: refused.append((reason, name))
        )
        try:
            await _connect(bench)
            await _ride(bench, LIVE_FROM + timedelta(minutes=16))
            bench.port.apply_settings(dataclasses.replace(small, trailing_enabled=True))
            await bench.port.wait()
            return bench.port.values, bench.port.live_status()
        finally:
            await _close(bench, session)

    values, status = loop.run_until_complete(go())
    assert status.on, "бой не включился — проверять нечего"
    assert refused and refused[-1][1] == "trailing_enabled", refused
    assert "D-141" in refused[-1][0] and "по прежним настройкам" in refused[-1][0], refused
    assert not values.trailing_enabled, "запрещённое в бою значение принято"


def test_after_a_halt_the_window_sees_the_halt_and_the_position_left_at_the_broker(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R4: бой встал при лонге — снимок боя называет остановку и позицию у брокера.

    После остановки боевая сторона закрыта, а позиция у брокера осталась
    с тейком. Стережёт: `halted` — причина словами, `position` — лонг 1
    с уровнем тейка; «Выключить бой» называет эту позицию, а не «позиции нет».
    """
    monkeypatch.delitem(_SPIKES, ENTRY_SPIKE)

    async def go() -> tuple[LiveStatus, str]:
        bench, session = await _bench(tmp_path, broker, FIXED_TAKE)
        try:
            await _connect(bench)
            await _ride(bench, LIVE_FROM + timedelta(minutes=50))
            bench.port.halt("Проба остановки", "Проба: робот остановлен извне боя.")
            await bench.port.wait()
            status = bench.port.live_status()
            bench.port.set_live(False)
            await bench.port.wait()
            return status, bench.said("Бой выключен")
        finally:
            await _close(bench, session)

    status, off = loop.run_until_complete(go())
    assert not status.on and status.wanted
    assert "Проба: робот остановлен извне боя." in status.halted, status.halted
    assert status.position is not None and status.position.signed == 1
    assert status.position.take_level is not None
    assert "лонг 1" in off, off


def test_a_pinned_backtest_is_refused_in_live_and_the_live_goes_on(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R5: прогон на закреплённом отрезке в бою — отказ словами «выключите бой», бой идёт.

    Прежде прогон молча закрывал боевую сторону: закреплённый отрезок
    закрывает живой ход. Стережёт: отказ назван, бой включён, тейк у брокера
    жив, команд брокеру нет.
    """
    from ui.models import BacktestRequest

    monkeypatch.delitem(_SPIKES, ENTRY_SPIKE)
    told: list[str] = []

    async def go() -> tuple[LiveStatus, Takes, int]:
        bench, session = await _bench(tmp_path, broker, FIXED_TAKE)
        bench.port.backtest_refused.connect(told.append)
        try:
            await _connect(bench)
            await _ride(bench, LIVE_FROM + timedelta(minutes=50))
            before = _takes(broker)
            sent = len(broker.commands)
            bench.port.run_backtest(BacktestRequest(
                settings=FIXED_TAKE, since=LIVE_FROM - timedelta(hours=20),
                until=LIVE_FROM - timedelta(hours=10),
            ))
            await bench.port.wait()
            assert _takes(broker) == before
            return bench.port.live_status(), before, len(broker.commands) - sent
        finally:
            await _close(bench, session)

    status, before, sent = loop.run_until_complete(go())
    assert told and "Выключите бой" in told[-1], told
    assert status.on, "прогон на отрезке прервал бой"
    assert before and sent == 0


# ------------------------------------------- повторный /risk 2в: N1, N2, N3


def test_a_held_rebuild_keeps_feeding_the_pair_its_own_candle_size(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """N1 (владелец: «отложить честно»): пока пересборка отложена, пара ест свечи своего хода.

    Лонг 1 на 15-минутках; «Размер свечи» сменили на 5 минут. Позиция держит
    пересборку, и до выхода оба движка получают 15-минутки — с ними собраны.
    Стережёт: ни одной заявки вне 15-минутного расписания (по 5-минуткам
    переворот случился бы в 09:50); переворот в 10:00, выход по концу окна
    в 11:00; строка отложенной пересборки называет, что ждёт; после выхода
    бой пересобран на 5-минутках.
    """
    times: list[datetime] = []
    charts: list[ChartData] = []

    async def go() -> tuple[list[str], str]:
        bench, session = await _bench(tmp_path, broker, NO_TAKE)
        plan = broker.on_create

        def spy(order: BookOrder) -> None:
            times.append(bench.exchange.now)
            assert plan is not None
            plan(order)

        broker.on_create = spy
        bench.port.chart_replaced.connect(charts.append)
        try:
            await _connect(bench)
            await _ride(bench, LIVE_FROM + timedelta(minutes=50))
            bench.port.apply_settings(dataclasses.replace(NO_TAKE, timeframe="5 минут"))
            await bench.port.wait()
            await _ride(bench, since=LIVE_FROM + timedelta(minutes=51))
            return bench.events(), bench.said("Пересборка боя отложена")
        finally:
            await _close(bench, session)

    events, held = loop.run_until_complete(go())
    assert times, "заявок не было — проверять нечего"
    assert all(moment.minute % 15 == 0 for moment in times), [f"{t:%H:%M:%S}" for t in times]
    assert [f"{moment:%H:%M}" for moment in times] == ["09:45", "10:00", "10:00", "11:00"]
    assert "Размер свечи: 15 минут → 5 минут" in held, held
    assert events.count("Бой включён") == 2, "после выхода пересборка не случилась"
    assert charts[-1].timeframe == "5 минут"


@pytest.mark.parametrize("how", ["live-off", "stream-off", "halt"])
def test_a_fill_not_yet_given_to_the_engine_is_the_position_left_at_the_broker(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    """N2: вход исполнен у брокера, движку ещё не отдан — бой закрыли; окно видит лонг 1.

    Позиция для окна и вопросов — у брокера, а не у движка: движок этого
    входа ещё не видел. Стережёт: после выключения, отключения потока или
    остановки снимок боя показывает лонг 1 с пометкой «на момент остановки»,
    а строка «Бой выключен» называет «лонг 1», а не «позиции нет».
    """
    monkeypatch.delitem(_SPIKES, ENTRY_SPIKE)
    # Растущей минутки нет: сделка входа ждёт первой минуты следующего бара.
    monkeypatch.setattr(Exchange, "reveal", lambda _self: None)

    async def go() -> tuple[object, LiveStatus, str]:
        bench, session = await _bench(tmp_path, broker, FIXED_TAKE)
        try:
            await _connect(bench)
            await _ride(bench, LIVE_FROM + timedelta(minutes=45))
            engine = bench.port.live_status().position
            if how == "live-off":
                bench.port.set_live(False)
            elif how == "stream-off":
                bench.port.stream(False)
            else:
                bench.port.halt("Проба остановки", "Проба: робот остановлен извне боя.")
            await bench.port.wait()
            return engine, bench.port.live_status(), bench.said("Бой выключен")
        finally:
            await _close(bench, session)

    engine, status, off = loop.run_until_complete(go())
    assert engine is None and broker.position(SYMBOL) == 1, "окно неотданного входа не открылось"
    assert status.position is not None and status.position.signed == 1, status.position
    assert "на момент остановки" in status.position_note, status.position_note
    if how != "halt":
        assert "лонг 1" in off, off


def test_after_close_everything_the_left_position_shows_no_take(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N3: «закрыть всё» сняло тейк, закрывающая не подана (К2) — уровня у позиции нет.

    За обрыв потока статусов у брокера прошла чужая покупка; после связи
    сверка видит лонг 2 против лонга 1, снимает тейк и встаёт. Стережёт:
    окно показывает лонг 2 по брокеру без уровня тейка, с пометкой «на момент
    остановки»; тейка у брокера нет.
    """
    from tests.test_app_live_ride import _fast_reconnect, _relink

    monkeypatch.delitem(_SPIKES, ENTRY_SPIKE)

    async def go() -> LiveStatus:
        bench, session = await _bench(tmp_path, broker, FIXED_TAKE)
        _fast_reconnect(bench, session)
        try:
            await _connect(bench)
            await _ride(bench, LIVE_FROM + timedelta(minutes=50))
            broker.muted = True
            broker.foreign_trade("1", 1, BASE, ticker=SYMBOL)
            await _relink(broker)
            broker.muted = False
            await _ride(
                bench, LIVE_FROM + timedelta(minutes=53), since=LIVE_FROM + timedelta(minutes=51)
            )
            return bench.port.live_status()
        finally:
            await _close(bench, session)

    status = loop.run_until_complete(go())
    assert status.halted, "сверка не остановила бой — проверять нечего"
    assert _takes(broker) == [], "тейк у брокера жив — сценарий не тот"
    assert status.position is not None and status.position.signed == 2, status.position
    assert status.position.take_level is None, "окно показывает тейк, которого у брокера нет"
    assert "на момент остановки" in status.position_note, status.position_note


def test_a_take_cancelled_at_the_broker_is_not_shown_on_the_left_position(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N3: тейк сняли не мы (биржа, руками) — бой встал; позиция та же, уровня у неё нет.

    Позиция у брокера совпадает с движком (лонг 1), а тейка у брокера уже
    нет. Стережёт: окно не показывает уровень, которого нет, — уровень
    остаётся у запомненной позиции, только пока заявка тейка жива в журнале.
    """
    monkeypatch.delitem(_SPIKES, ENTRY_SPIKE)

    async def go() -> LiveStatus:
        bench, session = await _bench(tmp_path, broker, FIXED_TAKE)
        try:
            await _connect(bench)
            await _ride(bench, LIVE_FROM + timedelta(minutes=50))
            [take] = [order for order in broker.live() if order.order_type == "2"]
            broker.cancel_outside(take.key)
            await asyncio.sleep(0.2)
            await bench.port.wait()
            await bench.port.wait()
            return bench.port.live_status()
        finally:
            await _close(bench, session)

    status = loop.run_until_complete(go())
    assert status.halted and not status.on, "снятие тейка не остановило бой — сценарий не тот"
    assert status.position is not None and status.position.signed == 1, status.position
    assert status.position.take_level is None, "окно показывает снятый тейк"
    assert "на момент остановки" in status.position_note, status.position_note


# ------------------------------------------- третий /risk 2в: Н1–Н4

SMALL = dataclasses.replace(FIXED_TAKE, minute_bar_limit=MinuteBarLimit.FIFTEEN)


@pytest.mark.parametrize("steps", ["at-once", "candle-then-trailing"])
def test_d141_is_checked_on_the_series_a_held_fact_still_runs(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch, steps: str
) -> None:
    """Н1: в бою при лонге «свеча 30 + скользящий уровень» — отказ: «факт» ещё идёт 15-минутками.

    Пересборка на 30 минут отложится позицией, и «факт» пошёл бы прежним
    рядом 15 минут со скользящим уровнем при пороге минуток 15 — ровно
    то, что запрещает D-141. Стережёт: отказ под полем `trailing_enabled`
    с прежним рядом в словах; уровень не включён; бой идёт.
    """
    monkeypatch.delitem(_SPIKES, ENTRY_SPIKE)
    refused: list[tuple[str, str]] = []

    async def go() -> tuple[UiSettings, LiveStatus]:
        bench, session = await _bench(tmp_path, broker, SMALL)
        bench.port.settings_refused.connect(
            lambda _values, reason, name: refused.append((reason, name))
        )
        try:
            await _connect(bench)
            await _ride(bench, LIVE_FROM + timedelta(minutes=50))
            longer = dataclasses.replace(SMALL, timeframe="30 минут")
            if steps == "candle-then-trailing":
                bench.port.apply_settings(longer)
                await bench.port.wait()
            bench.port.apply_settings(dataclasses.replace(longer, trailing_enabled=True))
            await bench.port.wait()
            return bench.port.values, bench.port.live_status()
        finally:
            await _close(bench, session)

    values, status = loop.run_until_complete(go())
    assert status.on and status.position is not None, "бой без позиции — пересборка не держится"
    assert refused and refused[-1][1] == "trailing_enabled", refused
    assert "D-141" in refused[-1][0] and "15 минут" in refused[-1][0], refused[-1][0]
    assert not values.trailing_enabled


class _Unread(RuntimeError):
    """Портфель у брокера не прочитался — подставная беда."""


@pytest.mark.parametrize("entered", [True, False])
def test_when_the_broker_cannot_be_read_the_window_still_hears_about_the_position(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch,
    entered: bool,
) -> None:
    """Н2: бой выключают, портфель не читается — позиция по роботу с неотданным, оговорка вслух.

    С неотданным входом окно видит лонг 1 (вход исполнен у брокера, движок
    его ещё не видел). Без позиции — всё равно оговорка «у брокера
    не прочитана» и строка-предупреждение, а не «Позиции у робота нет».
    """
    monkeypatch.delitem(_SPIKES, ENTRY_SPIKE)
    monkeypatch.setattr(Exchange, "reveal", lambda _self: None)

    async def unread(_self: object) -> tuple[int, float | None]:
        raise _Unread("проба: портфель не прочитан")

    async def go() -> tuple[LiveStatus, str]:
        bench, session = await _bench(tmp_path, broker, FIXED_TAKE)
        try:
            await _connect(bench)
            await _ride(bench, LIVE_FROM + timedelta(minutes=45 if entered else 30))
            monkeypatch.setattr(LiveDesk, "holding", unread)
            bench.port.set_live(False)
            await bench.port.wait()
            return bench.port.live_status(), bench.said("Бой выключен")
        finally:
            await _close(bench, session)

    status, off = loop.run_until_complete(go())
    assert "не прочитана" in status.position_note, status.position_note
    if entered:
        assert status.position is not None and status.position.signed == 1, status.position
        assert "лонг 1" in off, off
    else:
        assert status.position is None
        assert "не прочитана" in off and "Проверьте позицию" in off, off


def test_a_failed_close_of_the_live_side_still_tells_the_position(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Н3: закрытие боевой стороны упало — позиция и «Бой выключен» всё равно сказаны.

    Стережёт: строка о беде закрытия словами, одна строка «Бой выключен»
    с позицией, позиция в снимке боя; бой выключен.
    """
    monkeypatch.delitem(_SPIKES, ENTRY_SPIKE)

    async def broken(_self: object) -> None:
        raise _Unread("проба: закрытие стороны упало")

    async def go() -> tuple[LiveStatus, list[str], str, str]:
        bench, session = await _bench(tmp_path, broker, FIXED_TAKE)
        try:
            await _connect(bench)
            await _ride(bench, LIVE_FROM + timedelta(minutes=50))
            monkeypatch.setattr(LiveDesk, "aclose", broken)
            bench.port.set_live(False)
            await bench.port.wait()
            return (bench.port.live_status(), bench.events(), bench.said("Бой выключен"),
                    bench.said("Боевой исполнитель"))
        finally:
            await _close(bench, session)

    status, events, off, trouble = loop.run_until_complete(go())
    assert not status.on and not status.wanted
    assert "закрылась с ошибкой" in trouble, trouble
    assert events.count("Бой выключен") == 1 and "лонг 1" in off, off
    assert status.position is not None and status.position.signed == 1


def test_off_and_on_before_a_pass_keeps_the_live_and_leaves_no_stale_line(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Н4: «Выключить» и сразу «Включить» до прохода — бой идёт, строка «Бой выключен» не всплывает.

    Стережёт: бой включён тем же ходом, сказано «Выключение боя отменено»;
    позднее закрытие боя (остановка) не выдаёт отложенную «Бой выключен».
    """
    monkeypatch.delitem(_SPIKES, ENTRY_SPIKE)

    async def go() -> tuple[LiveStatus, list[str]]:
        bench, session = await _bench(tmp_path, broker, FIXED_TAKE)
        try:
            await _connect(bench)
            await _ride(bench, LIVE_FROM + timedelta(minutes=50))
            bench.port.set_live(False)
            bench.port.set_live(True)
            await bench.port.wait()
            status = bench.port.live_status()
            bench.port.halt("Проба остановки", "Проба: робот остановлен извне боя.")
            await bench.port.wait()
            return status, bench.events()
        finally:
            await _close(bench, session)

    status, events = loop.run_until_complete(go())
    assert status.on and status.wanted
    assert "Выключение боя отменено" in events, events
    assert "Бой выключен" not in events, events


def test_the_live_off_line_says_no_take_only_when_it_can_vouch(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """B-082, обратная сторона: брокер прочитан, позиция совпала, живых лимитных нет — «тейка нет».

    Сторож «неизвестно» не должен съесть правду: когда программа может
    поручиться, строка говорит это прямо.
    """

    async def go() -> str:
        bench, session = await _bench(tmp_path, broker, NO_TAKE)
        try:
            await _connect(bench)
            await _ride(bench, LIVE_FROM + timedelta(minutes=50))
            bench.port.set_live(False)
            await bench.port.wait()
            return bench.said("Бой выключен")
        finally:
            await _close(bench, session)

    off = loop.run_until_complete(go())
    assert "лонг 1" in off and "тейка у неё нет" in off, off


# ------------------------------------------- четвёртый /risk 2в: Н6–Н8, З2, З5


@pytest.mark.parametrize(
    "field", ["trailing_enabled", "volume"],
)
def test_off_apply_on_cannot_slip_a_forbidden_setting_into_the_live(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    """Н6: «Выключить» → «Применить» запрещённое → «Включить» до прохода — запрет держится.

    Боевая сторона ещё собрана, пока выключение не дошло до прохода: «Применить»
    обязано проверить запреты боя (D-141 с порогом минуток, нецелый объём),
    а «Включить» — отменить выключение только после тех же запретов.
    Стережёт: отказ под полем, бой идёт прежними настройками.
    """
    monkeypatch.delitem(_SPIKES, ENTRY_SPIKE)
    refused: list[str] = []

    async def go() -> tuple[UiSettings, LiveStatus, list[str]]:
        bench, session = await _bench(tmp_path, broker, SMALL)
        bench.port.settings_refused.connect(lambda _values, _reason, name: refused.append(name))
        try:
            await _connect(bench)
            await _ride(bench, LIVE_FROM + timedelta(minutes=50))
            bench.port.set_live(False)
            change: dict[str, Any] = {"trailing_enabled": True, "volume": 1.5}
            bench.port.apply_settings(dataclasses.replace(SMALL, **{field: change[field]}))
            bench.port.set_live(True)
            await bench.port.wait()
            return bench.port.values, bench.port.live_status(), bench.events()
        finally:
            await _close(bench, session)

    values, status, events = loop.run_until_complete(go())
    assert refused == [field], refused
    assert getattr(values, field) == getattr(SMALL, field)
    assert status.on and "Выключение боя отменено" in events, events


def test_history_load_is_refused_in_live_and_a_running_load_refuses_the_live(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Н8 (владелец: «недоступна в бою»): загрузка истории в бою — отказ словами, бой идёт.

    Загрузка занимает поток данных и откладывает проходы «факта»: переворот
    уходил бы к брокеру с опозданием на всю загрузку. Стережёт: в бою
    загрузка не начата («выключите бой»); идущая загрузка не даёт включить бой.
    """
    from ui.models import HistoryLoadRequest

    monkeypatch.delitem(_SPIKES, ENTRY_SPIKE)
    outcomes: list[object] = []

    async def go() -> tuple[LiveStatus, LiveStatus]:
        bench, session = await _bench(tmp_path, broker, FIXED_TAKE)
        bench.port.history_finished.connect(outcomes.append)
        try:
            await _connect(bench)
            await _ride(bench, LIVE_FROM + timedelta(minutes=50))
            bench.port.load_history(HistoryLoadRequest(symbol=SYMBOL))
            await bench.port.wait()
            during = bench.port.live_status()
            bench.port.set_live(False)
            await bench.port.wait()
            monkeypatch.setattr(HistoryPort, "_loading", lambda _self: True)
            return during, bench.port.live_status()
        finally:
            await _close(bench, session)

    during, off = loop.run_until_complete(go())
    assert outcomes and "выключите бой" in getattr(outcomes[-1], "trouble", ""), outcomes
    assert during.on, "загрузка прервала бой"
    assert not off.available and any("выключите бой" in why for why in off.why_not), off.why_not


def test_an_unread_portfolio_names_the_live_take_from_the_journal(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Н7: портфель не прочитан, тейк жив по журналу — окну готовые слова о нём (`take_note`).

    Уровня у позиции нет (за тейк, не спросив брокера, программа не ручается),
    но и «тейка нет» неправда: окно получает «по журналу команд у брокера жива
    лимитная заявка по 251375 — возможно, это тейк».
    """
    monkeypatch.delitem(_SPIKES, ENTRY_SPIKE)

    async def unread(_self: object) -> tuple[int, float | None]:
        raise _Unread("проба: портфель не прочитан")

    async def go() -> LiveStatus:
        bench, session = await _bench(tmp_path, broker, FIXED_TAKE)
        try:
            await _connect(bench)
            await _ride(bench, LIVE_FROM + timedelta(minutes=50))
            monkeypatch.setattr(LiveDesk, "holding", unread)
            bench.port.set_live(False)
            await bench.port.wait()
            return bench.port.live_status()
        finally:
            await _close(bench, session)

    status = loop.run_until_complete(go())
    assert status.position is not None and status.position.take_level is None
    assert "251375" in status.take_note and "возможно" in status.take_note, status.take_note
    assert "тейка у неё нет" not in status.take_note


def test_off_pressed_during_the_enable_check_does_not_turn_the_live_on(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """З2: «Включить» → идёт сверка → «Выключить» — пара не включается, строки по порядку.

    Стережёт: бой не включён, ни одной заявки, «Бой включён» не сказано,
    «Бой выключен» — после «Бой включается».
    """
    gate, reached = asyncio.Event(), asyncio.Event()
    real_open = LiveDesks.open

    async def slow_open(self: LiveDesks, request: FactRequest) -> LiveDesk:
        reached.set()
        await gate.wait()
        return await real_open(self, request)

    monkeypatch.setattr(LiveDesks, "open", slow_open)

    async def go() -> tuple[LiveStatus, list[str], int]:
        bench, session = await _bench(tmp_path, broker, FIXED_TAKE)
        try:
            await _connect(bench, live=False)
            await _ride(bench, LIVE_FROM + timedelta(minutes=40))
            bench.port.set_live(True)
            await asyncio.wait_for(reached.wait(), 5)
            bench.port.set_live(False)
            gate.set()
            await bench.port.wait()
            await _ride(
                bench, LIVE_FROM + timedelta(minutes=50), since=LIVE_FROM + timedelta(minutes=41)
            )
            return bench.port.live_status(), bench.events(), len(broker.orders)
        finally:
            await _close(bench, session)

    status, events, orders = loop.run_until_complete(go())
    assert not status.on and not status.wanted
    assert orders == 0
    assert "Бой включён" not in events, events
    assert events.index("Бой выключен") > events.index("Бой включается"), events


def test_a_refusal_with_a_fresh_position_updates_the_remembered_one(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """З5: бой выключен при лонге 1; за это время у брокера лонг 2 — новый отказ обновляет окно.

    Отказ включения прочитал у брокера свежую позицию: запомненная «лонг 1
    с тейком» уже неправда. Стережёт: окно — лонг 2 без уровня, с пометкой
    «при попытке включить бой».
    """
    monkeypatch.delitem(_SPIKES, ENTRY_SPIKE)

    async def go() -> LiveStatus:
        bench, session = await _bench(tmp_path, broker, FIXED_TAKE)
        try:
            await _connect(bench)
            await _ride(bench, LIVE_FROM + timedelta(minutes=50))
            bench.port.set_live(False)
            await bench.port.wait()
            broker.outside = 1
            bench.port.set_live(True)
            await bench.port.wait()
            return bench.port.live_status()
        finally:
            await _close(bench, session)

    status = loop.run_until_complete(go())
    assert "лонг 2" in status.refused, status.refused
    assert status.position is not None and status.position.signed == 2, status.position
    assert status.position.take_level is None
    assert "при попытке включить бой" in status.position_note, status.position_note
    # Слова о тейке — тоже не от прежней позиции: за лонг 2 тейк не известен.
    assert "неизвестно" in status.take_note and "жив —" not in status.take_note, status.take_note


def test_cancelling_an_off_passes_the_same_gates_as_turning_on(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Н6: отмена выключения — те же запреты, что включение (здесь — идущая загрузка истории).

    Стережёт: «Включить» после «Выключить» при запрете не отменяет выключение —
    отказ словами, бой выключается.
    """
    monkeypatch.delitem(_SPIKES, ENTRY_SPIKE)

    async def go() -> tuple[LiveStatus, list[str], str]:
        bench, session = await _bench(tmp_path, broker, FIXED_TAKE)
        try:
            await _connect(bench)
            await _ride(bench, LIVE_FROM + timedelta(minutes=50))
            bench.port.set_live(False)
            monkeypatch.setattr(HistoryPort, "_loading", lambda _self: True)
            bench.port.set_live(True)
            await bench.port.wait()
            return bench.port.live_status(), bench.events(), bench.said("Бой не включён")
        finally:
            await _close(bench, session)

    status, events, refused = loop.run_until_complete(go())
    assert not status.on and not status.wanted
    assert "Выключение боя отменено" not in events, events
    assert "выключите бой" in refused, refused
    # Выключение доведено до конца и сказано: отказ отмены не съел его строку.
    assert "Бой выключен" in events, events


# ------------------------------------------- пятый /risk 2в: Н9, Н10, З8


def _past_apply(_self: object, _settings: object) -> None:
    """Проверка «Применить» в бою снята — дорога в обход неё, к запасному рубежу."""


def test_a_forbidden_trailing_keeps_only_itself_and_mode_off_still_reaches_the_fact(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Н9: запрет на прежнем ряду держит прежним **только** скользящий уровень.

    Лонг на 15-минутках; мимо «Применить» пришли «свеча 30 + скользящий
    уровень» — пересборку держит позиция, на прежнем ряду D-141. Затем
    «Выключен». Стережёт: режим доходит до «факта» — в 10:00 переворота
    к брокеру нет; строка «Настройки не дошли до боя» называет только
    скользящий уровень, не режим.
    """
    monkeypatch.delitem(_SPIKES, ENTRY_SPIKE)

    async def go() -> tuple[int, list[str], list[str]]:
        bench, session = await _bench(tmp_path, broker, SMALL)
        try:
            await _connect(bench)
            await _ride(bench, LIVE_FROM + timedelta(minutes=46))
            monkeypatch.setattr(HistoryPort, "_fits_live", _past_apply)
            said = len(bench.notes)
            bench.port.apply_settings(
                dataclasses.replace(SMALL, timeframe="30 минут", trailing_enabled=True)
            )
            await bench.port.wait()
            bench.port.set_mode(Mode.OFF)
            await bench.port.wait()
            sent = len(broker.commands)
            await _ride(
                bench, LIVE_FROM + timedelta(minutes=62), since=LIVE_FROM + timedelta(minutes=47)
            )
            kept = [
                why for event, why in bench.notes[said:] if event == "Настройки не дошли до боя"
            ]
            return len(broker.commands) - sent, kept, bench.events()
        finally:
            await _close(bench, session)

    sent, kept, events = loop.run_until_complete(go())
    assert sent == 0, "«Выключен» не дошёл до «факта»: в 10:00 ушли заявки"
    assert kept, "что скользящий уровень не дошёл, не сказано"
    assert all("Скользящ" in why and "Режим" not in why for why in kept), kept
    assert "Позиция без управления" in events, events


def test_apply_in_live_checks_the_old_series_even_when_the_fact_is_flat(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """Н9: «Применить» в бою проверяет прежний ряд при **любой** смене ряда.

    «Факт» без позиции, но вход может исполниться, пока «Применить» думает,
    и пересборка отложится — «факт» пошёл бы прежним рядом со скользящим
    уровнем (D-141). Стережёт: отказ под полем с прежним рядом в словах.
    """
    refused: list[tuple[str, str]] = []

    async def go() -> UiSettings:
        bench, session = await _bench(tmp_path, broker, SMALL)
        bench.port.settings_refused.connect(
            lambda _values, reason, name: refused.append((reason, name))
        )
        try:
            await _connect(bench)
            await _ride(bench, LIVE_FROM + timedelta(minutes=20))
            assert bench.port.live_status().position is None
            bench.port.apply_settings(
                dataclasses.replace(SMALL, timeframe="30 минут", trailing_enabled=True)
            )
            await bench.port.wait()
            return bench.port.values
        finally:
            await _close(bench, session)

    values = loop.run_until_complete(go())
    assert refused and refused[-1][1] == "trailing_enabled", refused
    assert "прежним рядом" in refused[-1][0], refused[-1][0]
    assert not values.trailing_enabled and values.timeframe == "15 минут"


def test_a_refused_enable_does_not_vouch_for_a_take_from_the_time_of_off(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Н10: отказ включения со свежей позицией за тейк не ручается.

    Лонг с тейком; бой выключен; тейк сняли не мы; «Включить» — отказ
    (позиция не ноль). Стережёт: уровня у позиции нет, слова о тейке —
    «неизвестно — проверьте», а не «жив» с момента выключения.
    """
    monkeypatch.delitem(_SPIKES, ENTRY_SPIKE)

    async def go() -> LiveStatus:
        bench, session = await _bench(tmp_path, broker, FIXED_TAKE)
        try:
            await _connect(bench)
            await _ride(bench, LIVE_FROM + timedelta(minutes=50))
            bench.port.set_live(False)
            await bench.port.wait()
            for order in broker.live():
                broker.cancel_outside(order.key)
            bench.exchange.now = LIVE_FROM + timedelta(minutes=80)
            bench.port.set_live(True)
            await bench.port.wait()
            return bench.port.live_status()
        finally:
            await _close(bench, session)

    status = loop.run_until_complete(go())
    assert not status.on and status.position is not None and status.position.signed == 1
    assert status.position.take_level is None, "окно ручается за тейк с момента выключения"
    assert "неизвестно" in status.take_note and "жив —" not in status.take_note, status.take_note


def test_returning_the_settings_clears_the_held_line(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """З8: вернул прежние настройки — «пересборка отложена» в окне не держится.

    Стережёт: после возврата периода средней снимок боя говорит, что ждать
    нечего (`held` пуст).
    """

    async def go() -> tuple[str, str]:
        bench, session = await _bench(tmp_path, broker, NO_TAKE)
        try:
            await _connect(bench)
            await _ride(bench, LIVE_FROM + timedelta(minutes=50))
            bench.port.apply_settings(dataclasses.replace(NO_TAKE, average_period=20))
            await bench.port.wait()
            held = bench.port.live_status().held
            bench.port.apply_settings(NO_TAKE)
            await bench.port.wait()
            return held, bench.port.live_status().held
        finally:
            await _close(bench, session)

    held, after = loop.run_until_complete(go())
    assert "Период средней: 15 → 20" in held, held
    assert after == "", after
