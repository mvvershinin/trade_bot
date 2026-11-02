"""Бой на ходу: отложенная пересборка, беда сторожа, «Выключен», сверка после связи (Э4 2в).

План `.docs/plans/E4-live-orders.md` §7, §12; решения 0017, 0020, 0066.
Что стережётся здесь, словами:

* незакрытая команда журнала откладывает пересборку «факта» так же, как
  открытая позиция (С4), — и после её закрытия пересборка идёт сама;
* сторож трейлинга не посмотрел минутку — строка журнала, а не тишина
  (правило 13);
* режим «Выключен» при позиции «факта» — строка «Позиция без управления»
  с позицией словами;
* связь с потоком статусов вернулась, а у брокера за обрыв появилась
  сделка, которой программа не видела: сверка на ближайшем проходе
  останавливает робота с обеими позициями словами, **до** первой заявки;
* связь вернулась без расхождения — неподвижный тейк у брокера остаётся
  (`B-079`: сверка спрашивает его статус, а не снимает);
* линии сделок в бою: «Факт» — сделки брокера, «Прогноз» — сделки тени;
* «заявки уходят брокеру» в снимке окна — пока собран исполнитель, а не
  пока бой просили (`D-172`).

Стенд — тот же, что у пары (`tests/test_app_live_pair.py`). Боевая сторона —
настоящая (`app/live_trading.py`); упавший сторож — беда, которой подделка
брокера не умеет, — подсаживается обёрткой `_Desks`, и только он.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import pathlib
from dataclasses import dataclass
from datetime import timedelta

import pytest
import websockets

os.environ.setdefault("QT_API", "pyside6")  # до первого импорта qasync

from app.fact import FactDesk, FactRequest
from app.live_trading import LiveDesks
from backtest import HistoryRun
from broker.session import BrokerSession
from engine import DayRecord, OrderExecutor
from market import CandleStore, MarketWorker
from market.commands import BrokerCommand, CommandAction, CommandSide, CommandStatus
from market.journal import RunOrigin, SessionRecord
from tests.fake_broker import FakeBroker
from tests.test_app_live_pair import (
    _SPIKES,
    BASE,
    FIXED_TAKE,
    LAST_MINUTE_TOUCH,
    LIVE_FROM,
    NO_TAKE,
    OWNER,
    SYMBOL,
    Bench,
    _bench,
    _close,
    _connect,
    _ride,
)
from ui.models import ChartData, Layer, Mode, RobotState

#: Ключи команд без исхода — выдуманные, в форме UUID, как у брокера: своего
#: счёта и чужого (в `userdata/` бывает два токена на два счёта).
HANGING = "00000000-0000-4000-8000-0000000000e1"
FOREIGN = "00000000-0000-4000-8000-0000000000e2"


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch) -> FakeBroker:
    fake = FakeBroker()
    monkeypatch.setattr(websockets, "connect", fake.connect)
    return fake


@dataclass
class _Desk:
    """Настоящая боевая сторона, у которой сторож трейлинга падает на каждой минутке."""

    real: FactDesk

    @property
    def executor(self) -> OrderExecutor:
        return self.real.executor

    @property
    def day(self) -> DayRecord | None:
        return self.real.day

    @property
    def notes(self) -> tuple[str, ...]:
        return self.real.notes

    @property
    def halted(self) -> str | None:
        return self.real.halted

    async def watch(self) -> None:
        raise RuntimeError("минутки сторожа не прочитались на проверке")

    async def relinked(self, expected: int) -> str | None:
        return await self.real.relinked(expected)

    async def unclosed(self) -> tuple[str, ...]:
        return await self.real.unclosed()

    @property
    def undelivered(self) -> tuple[str, ...]:
        return self.real.undelivered

    async def holding(self) -> tuple[int, float | None]:
        return await self.real.holding()

    async def resting_levels(self) -> tuple[float, ...]:
        return await self.real.resting_levels()

    async def aclose(self) -> None:
        await self.real.aclose()


@dataclass
class _Desks:
    real: LiveDesks

    def refusal(self) -> str | None:
        return self.real.refusal()

    async def open(self, request: FactRequest) -> FactDesk:
        return _Desk(await self.real.open(request))


def _said(notes: list[tuple[str, str]], event: str) -> str:
    return " ".join(reason for name, reason in notes if name == event)


def test_an_unclosed_command_holds_the_rebuild_until_it_is_closed(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """С4: незакрытая команда при нулевой позиции — пересборка «факта» отложена, словами.

    Новый движок родился бы, не зная исхода команды, — а сверить её некому.
    Команда — строка журнала команд этого счёта без исхода (записана,
    ответа нет); рядом — такая же команда **другого** счёта, её этот бой
    не ведёт. Стережёт: смена периода средней до входа (позиции нет) бой
    не пересобирает, строка «Пересборка боя отложена» называет одну команду —
    свою; команда закрылась — пересборка на ближайшем проходе сама.
    """

    async def plant(worker: MarketWorker) -> None:
        def write(store: CandleStore) -> None:
            run = store.open_journal_session(
                SessionRecord(RunOrigin.LIVE, symbol=SYMBOL), now=LIVE_FROM
            )
            for key, account in ((HANGING, broker.account), (FOREIGN, "FAKE-ACCOUNT-2")):
                store.record_broker_command(run.id, BrokerCommand(
                    client_order_id=key, action=CommandAction.CREATE, order_id="open:long:висит",
                    symbol=SYMBOL, side=CommandSide.BUY, volume=1, price=None, account=account,
                ))

        await worker.call(write)

    async def close(worker: MarketWorker) -> None:
        def write(store: CandleStore) -> None:
            store.set_broker_command_status(HANGING, CommandStatus.REFUSED, reason="проверка")

        await worker.call(write)

    async def go() -> tuple[str, int, int, list[str]]:
        bench, session = await _bench(tmp_path, broker, NO_TAKE)
        try:
            await _connect(bench)
            await _ride(bench, LIVE_FROM + timedelta(minutes=20))
            assert bench.port.live_status().position is None, "к 09:20 позиции быть не должно"
            await plant(bench.worker)
            bench.port.apply_settings(dataclasses.replace(NO_TAKE, average_period=20))
            await bench.port.wait()
            held = _said(bench.notes, "Пересборка боя отложена")
            during = bench.events().count("Бой включён")
            await close(bench.worker)
            await _ride(
                bench, LIVE_FROM + timedelta(minutes=31), since=LIVE_FROM + timedelta(minutes=21)
            )
            return held, during, bench.events().count("Бой включён"), bench.events()
        finally:
            await _close(bench, session)

    held, during, after, events = loop.run_until_complete(go())
    assert "незакрытых 1" in held and HANGING in held, events
    assert FOREIGN not in held, "пересборку держит команда чужого счёта"
    assert during == 1, "«факт» пересобран при незакрытой команде"
    assert after == 2, "команда закрылась, а пересборка так и не случилась"


def test_a_failed_trailing_watch_is_said_in_the_journal(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """Правило 13: сторож трейлинга не посмотрел записанную минутку — строка, а не тишина.

    Касание в такой минуте найдётся только на закрытии свечи, с опозданием
    и по другой цене; человек обязан знать, что сторож слеп.
    """

    async def go() -> list[tuple[str, str]]:
        bench, session = await _bench(tmp_path, broker, OWNER)
        bench.port.attach_live(_Desks(bench.desks))
        try:
            await _connect(bench)
            assert bench.port.live_status().on, "бой не включился — проверять нечего"
            await _ride(bench, LIVE_FROM + timedelta(minutes=2))
            return list(bench.notes)
        finally:
            await _close(bench, session)

    notes = loop.run_until_complete(go())
    said = _said(notes, "Сторож трейлинга")
    assert "Проверка уровня по записанной минутке не удалась" in said, notes[-4:]


def test_mode_off_with_a_fact_position_says_the_position_is_left_unmanaged(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """«Выключен» при позиции «факта» — строка «Позиция без управления» с позицией словами.

    Выключение позицию не закрывает намеренно; тихое выключение
    с оставленной у брокера позицией — дефект (правило 13).
    """
    monkeypatch.delitem(_SPIKES, LAST_MINUTE_TOUCH)

    async def go() -> tuple[object, list[tuple[str, str]]]:
        bench, session = await _bench(tmp_path, broker, OWNER)
        try:
            await _connect(bench)
            await _ride(bench, LIVE_FROM + timedelta(minutes=76))
            position = bench.port.live_status().position
            bench.notes.clear()
            bench.port.set_mode(Mode.OFF)
            await bench.port.wait()
            return position, list(bench.notes)
        finally:
            await _close(bench, session)

    position, notes = loop.run_until_complete(go())
    assert position is not None, "к 10:16 «факт» обязан быть в позиции — проверять нечего"
    said = _said(notes, "Позиция без управления")
    assert "шорт 1" in said and "не закрывает" in said, notes


def test_the_window_state_says_orders_go_only_while_the_executor_is_assembled(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """`D-172`: «заявки уходят брокеру» в снимке окна — по собранному исполнителю, не по просьбе.

    Бой включён — `running` и не `simulation`. Исполнитель встал (сделка мимо
    программы), ход пары закрыт, а просьба боя осталась: снимок окна обязан
    сказать «симуляция», иначе окно показывало бы идущий бой у робота,
    который заявок не подаёт.
    """
    states: list[RobotState] = []

    async def go() -> tuple[RobotState, RobotState, str]:
        bench, session = await _bench(tmp_path, broker, NO_TAKE)
        bench.port.state_changed.connect(states.append)
        try:
            await _connect(bench)
            on = states[-1]
            broker.foreign_trade("1", 1, BASE)
            await asyncio.sleep(0.2)
            await bench.port.wait()
            await bench.port.wait()
            return on, states[-1], bench.port.halted
        finally:
            await _close(bench, session)

    on, halted_state, halted = loop.run_until_complete(go())
    assert on.running and not on.simulation, on
    assert halted, "исполнитель не встал — проверять нечего"
    assert not halted_state.running and halted_state.simulation, halted_state


def test_the_fact_lines_are_broker_deals_and_the_forecast_lines_shadow_deals(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """Линии сделок на графике в бою: «Факт» — сделки брокера, «Прогноз» — сделки тени.

    Биржа исполняет рыночные с проскальзыванием 50 пунктов, и сделки пары
    расходятся ценой. Линия «Факт», проведённая по сделке тени, показала бы
    расчёт под видом исполнения у брокера (`DOMAIN.md` §8, `D-172`).
    """
    charts: list[ChartData] = []

    async def go() -> tuple[HistoryRun | None, HistoryRun | None]:
        bench, session = await _bench(tmp_path, broker, NO_TAKE)
        bench.exchange.slip = 50.0
        bench.port.chart_replaced.connect(charts.append)
        try:
            await _connect(bench)
            await _ride(bench)
            status = bench.port.live_status()
            return status.fact, status.forecast
        finally:
            await _close(bench, session)

    fact, forecast = loop.run_until_complete(go())
    assert fact is not None and forecast is not None and fact.deals and forecast.deals
    paths = charts[-1].paths
    done = sorted((one.entry_price, one.exit_price) for one in paths if one.layer is Layer.FACT)
    planned = sorted((one.entry_price, one.exit_price) for one in paths if one.layer is Layer.PLAN)
    assert done == sorted((deal.entry_price, deal.exit_price) for deal in fact.deals), done
    assert planned == sorted((deal.entry_price, deal.exit_price) for deal in forecast.deals)
    assert done != planned, "проскальзывание есть, а линии слоёв совпали"


def _fast_reconnect(bench: Bench, session: BrokerSession) -> None:
    """Боевая сторона стенда без пауз переподключения: обрыв лечится сразу."""
    bench.port.attach_live(LiveDesks(
        lambda: session, bench.worker, class_code="SPBFUT",
        clock=lambda: bench.exchange.now, sleep=lambda _pause: asyncio.sleep(0),
    ))


async def _relink(broker: FakeBroker) -> int:
    """Оборвать поток статусов и дождаться, пока исполнитель откроет его снова."""
    sockets = len(broker.sockets)
    broker.drop_stream()
    for _ in range(100):
        await asyncio.sleep(0.01)
        if len(broker.sockets) > sockets:
            break
    return len(broker.sockets) - sockets


def test_after_the_status_stream_returns_a_missed_trade_halts_before_any_order(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """0017, 0020: связь вернулась — сверка на ближайшем проходе, расхождение — стоп словами.

    Лонг 1 с тейком у брокера. Поток статусов обрывается, за обрыв у брокера
    проходит покупка, которой программа не видела (кадр потерян). Поток
    переоткрывается. Стережёт: на ближайшем проходе робот остановлен
    с обеими позициями словами («у брокера лонг 2, у робота лонг 1»),
    остановка — в `robot_halt`; ни одной новой заявки после обрыва —
    переворот на 10:00 к брокеру не ушёл.
    """
    monkeypatch.delitem(_SPIKES, LIVE_FROM + timedelta(minutes=45))

    async def go() -> tuple[str, list[str], int, int]:
        bench, session = await _bench(tmp_path, broker, FIXED_TAKE)
        _fast_reconnect(bench, session)
        try:
            await _connect(bench)
            await _ride(bench, LIVE_FROM + timedelta(minutes=50))
            held = bench.port.live_status().position
            assert held is not None and held.signed == 1, "к 09:50 «факт» обязан быть в лонге"
            sent = len(broker.commands)
            broker.muted = True
            broker.foreign_trade("1", 1, BASE, ticker=SYMBOL)
            reopened = await _relink(broker)
            broker.muted = False
            await _ride(
                bench, LIVE_FROM + timedelta(minutes=61), since=LIVE_FROM + timedelta(minutes=51)
            )
            stored = [cause.reason for cause in await bench.worker.standing_halt()]
            created = [key for kind, key in broker.commands[sent:] if kind == "create"]
            return bench.port.halted, stored, reopened, len(created)
        finally:
            await _close(bench, session)

    halted, stored, reopened, created = loop.run_until_complete(go())
    assert reopened >= 1, "поток статусов не переоткрылся — проверять нечего"
    words = "Сверка с брокером: у брокера лонг 2, у робота лонг 1"
    assert words in halted, halted
    assert any(words in reason for reason in stored), stored
    assert created == 0, "после связи заявка ушла к брокеру до сверки"


def test_the_fixed_take_at_the_broker_survives_a_status_stream_reconnect(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Р1: неподвижный тейк — лимитная у брокера; обрыв потока статусов его не снимает.

    Решение 0017 снимает заявку, **чья судьба неизвестна** на обрыве; принятый
    и живой тейк робота к ним не относится. Лонг 1 с тейком у брокера, поток
    статусов обрывается и переоткрывается, позиция у брокера та же. Стережёт:
    ближайший проход (сверка после связи) тейк у брокера не снимает — ни одной
    отмены, заявка жива; уровень, который окно показывает у позиции «факта»,
    у брокера есть.
    """
    monkeypatch.delitem(_SPIKES, LIVE_FROM + timedelta(minutes=45))

    async def go() -> tuple[list[str], list[str], object]:
        bench, session = await _bench(tmp_path, broker, FIXED_TAKE)
        _fast_reconnect(bench, session)
        try:
            await _connect(bench)
            await _ride(bench, LIVE_FROM + timedelta(minutes=50))
            takes = [order.key for order in broker.live() if order.order_type == "2"]
            assert len(takes) == 1, "к 09:50 у брокера обязан стоять тейк лонга"
            sent = len(broker.commands)
            assert await _relink(broker) >= 1, "поток статусов не переоткрылся"
            await _ride(
                bench, LIVE_FROM + timedelta(minutes=52), since=LIVE_FROM + timedelta(minutes=51)
            )
            bench.port.refresh("проверка: проход после связи")
            await bench.port.wait()
            cancels = [key for kind, key in broker.commands[sent:] if kind == "cancel"]
            alive = [order.key for order in broker.live() if order.key in takes]
            return cancels, alive, bench.port.live_status().position
        finally:
            await _close(bench, session)

    cancels, alive, position = loop.run_until_complete(go())
    assert position is not None, "позиция «факта» пропала — проверять нечего"
    assert cancels == [], "после связи тейк у брокера снят"
    assert alive, "тейка у брокера нет, а окно показывает уровень позиции"
