"""Ручные действия владельца из окна: снять заявку робота, закрыть позицию.

Задача владельца 13.11.2026 после первого боевого дня: робот встал с лонгом
и тейком, снять тейк и закрыть позицию из программы было нечем. Сторожа:

* снимаются **только заявки робота** — команды журнала этого счёта; ручная
  заявка владельца у брокера и заявка другого счёта в журнале в список
  не попадают и не снимаются ни кнопкой «Снять заявку», ни «Закрыть позицию»;
* «Закрыть позицию» работает при **остановленном** роботе: боевая сторона
  уже закрыта, а тейк снимается и позиция закрывается;
* строка открытой позиции в таблице «Сделки» есть, пока позиция открыта,
  и исчезает, когда позиция закрылась.

Стенд — `tests/test_app_live_pair.py` (порт, пара, подставной брокер):
своя база и свой `userdata` во временном каталоге, сети нет.
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Awaitable, Callable
from datetime import timedelta

import httpx
import pytest
import websockets

from broker.session import PORTFOLIO_PATH, BrokerSession
from market import CandleStore, MarketWorker
from market.commands import BrokerCommand, CommandAction, CommandSide
from market.journal import RunOrigin, SessionRecord
from tests.fake_broker import CANCEL, CREATE, FakeBroker, _unavailable
from tests.test_app_live_pair import (
    _SPIKES,
    DAY,
    ENTRY_SPIKE,
    FIXED_TAKE,
    LIVE_FROM,
    SYMBOL,
    Bench,
    _bench,
    _close,
    _connect,
    _ride,
)
from ui.models import RobotOrders, TradeRow

#: Тейк прошлого запуска — в журнале команд этого счёта и живой у брокера.
ROBOT_TAKE = "00000000-0000-4000-8000-00000000a001"
#: Заявка, поданная владельцем в приложении брокера: у брокера есть, в журнале нет.
OWNER_ORDER = "00000000-0000-4000-8000-00000000b001"
#: Заявка другого счёта (второй токен в `userdata/`): в журнале есть, счёт чужой.
OTHER_ACCOUNT_ORDER = "00000000-0000-4000-8000-00000000c001"


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch) -> FakeBroker:
    fake = FakeBroker()
    monkeypatch.setattr(websockets, "connect", fake.connect)
    return fake


def _journal(broker: FakeBroker) -> Callable[[MarketWorker], Awaitable[None]]:
    """Журнал прошлого запуска: тейк робота этого счёта и заявка чужого счёта."""

    async def plant(worker: MarketWorker) -> None:
        def write(base: CandleStore) -> None:
            run = base.open_journal_session(
                SessionRecord(RunOrigin.LIVE, symbol=SYMBOL), now=DAY.replace(hour=8)
            )
            for key, account in (
                (ROBOT_TAKE, broker.account), (OTHER_ACCOUNT_ORDER, "OTHER-ACCOUNT"),
            ):
                base.record_broker_command(run.id, BrokerCommand(
                    client_order_id=key, action=CommandAction.CREATE,
                    order_id="take:long:прошлый", symbol=SYMBOL, side=CommandSide.SELL,
                    volume=1, price=251_000.0, account=account,
                ))

        await worker.call(write)

    return plant


async def _at_the_broker(broker: FakeBroker, key: str, price: float) -> None:
    """Лимитная продажа в книге подделки — тем путём, каким её подал бы кто угодно."""
    async with broker.client() as client:
        await client.post(CREATE[1], json={
            "clientOrderId": key, "side": "2", "orderType": "2", "orderQuantity": 1,
            "ticker": SYMBOL, "classCode": "SPBFUT", "price": price,
        })
    order = broker.order(key)
    assert order is not None and order.live, f"подделка не приняла заявку {key}"


def test_only_robot_orders_are_listed_and_cancelled(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """Стережёт: в списке и под снятием — только команды журнала этого счёта.

    Перезапуск после боевого дня: бой выключен, у брокера лонг 1, тейк робота
    прошлого запуска, ручная заявка владельца; в журнале ещё заявка другого
    счёта. «Снять заявку» видит один тейк; просьба снять ручную заявку
    владельца отказывает словами и брокеру не уходит; «Закрыть позицию»
    снимает тейк, закрывает лонг и ручную заявку не трогает.
    """

    async def go() -> tuple[list[RobotOrders], str, str]:
        bench, session = await _bench(
            tmp_path, broker, FIXED_TAKE, before=_journal(broker)
        )
        try:
            await _at_the_broker(broker, ROBOT_TAKE, 251_000.0)
            await _at_the_broker(broker, OWNER_ORDER, 252_000.0)
            broker.foreign_trade("1", 1, 250_000.0, ticker=SYMBOL)
            await _connect(bench, live=False)
            answers: list[RobotOrders] = []
            bench.port.robot_orders_ready.connect(answers.append)
            bench.port.request_robot_orders()
            await bench.port.wait()
            bench.port.cancel_robot_order(OWNER_ORDER)
            await bench.port.wait()
            refused = bench.said("Снятие заявки руками")
            assert broker.keys_sent("cancel") == {}, "снятие чужой заявки ушло брокеру"
            bench.port.close_position()
            await bench.port.wait()
            return answers, refused, bench.said("Закрытие позиции руками")
        finally:
            await _close(bench, session)

    answers, refused, closed = loop.run_until_complete(go())
    assert len(answers) == 1 and not answers[0].trouble, answers
    assert [order.key for order in answers[0].orders] == [ROBOT_TAKE], answers
    assert "нет среди незакрытых заявок робота" in refused, refused
    owner = broker.order(OWNER_ORDER)
    assert owner is not None and owner.live, "ручная заявка владельца снята программой"
    take = broker.order(ROBOT_TAKE)
    assert take is not None and not take.live, f"тейк робота жив у брокера: {closed}"
    assert broker.position(SYMBOL) == 0, closed
    assert "закрыта" in closed, closed


def test_close_position_works_when_the_robot_is_halted(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Стережёт: «Закрыть позицию» при остановленном роботе снимает тейк и закрывает позицию.

    Бой вошёл в лонг и поставил тейк у брокера; робот остановлен — боевая
    сторона закрыта, исполнителя боя нет. Кнопка обязана сработать всё
    равно: тейк снят, позиция у брокера ноль, итог — строкой журнала.
    """
    monkeypatch.delitem(_SPIKES, ENTRY_SPIKE)

    async def go() -> tuple[str, bool]:
        bench, session = await _bench(tmp_path, broker, FIXED_TAKE)
        try:
            await _connect(bench)
            await _ride(bench, LIVE_FROM + timedelta(minutes=50))
            assert broker.position(SYMBOL) == 1, "бой не вошёл — проверять нечего"
            assert broker.live(), "тейка у брокера нет — проверять нечего"
            bench.port.halt("Проба остановки", "Проба: робот остановлен.")
            await bench.port.wait()
            status = bench.port.live_status()
            assert status.halted and not status.on, "робот не остановлен — проверять нечего"
            bench.port.close_position()
            await bench.port.wait()
            return bench.said("Закрытие позиции руками"), bench.port.live_status().on
        finally:
            await _close(bench, session)

    said, on = loop.run_until_complete(go())
    assert not broker.live(), f"у брокера осталась живая заявка: {said}"
    assert broker.position(SYMBOL) == 0, said
    assert "закрыта" in said, said
    assert not on


def _creates(broker: FakeBroker) -> int:
    """Сколько заявок подано брокеру на создание."""
    return sum(1 for kind, _ in broker.commands if kind == "create")


def _open_rows(rows: tuple[TradeRow, ...]) -> list[TradeRow]:
    return [row for row in rows if row.exit_time is None]


def test_open_position_row_is_shown_and_goes_away_when_the_position_closes(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """Стережёт: строка открытой позиции в «Сделках» есть при позиции и пропадает с выходом.

    Ход боя по стенду: после входа в таблице одна строка без выхода — лонг,
    объём и цена входа те же, что у позиции робота, результат плавающий;
    после выхода строки без выхода нет, а сделка — среди закрытых.
    """

    async def go() -> list[tuple[tuple[TradeRow, ...], bool]]:
        bench, session = await _bench(tmp_path, broker, FIXED_TAKE)
        seen: list[tuple[tuple[TradeRow, ...], bool]] = []
        try:
            bench.port.trades_replaced.connect(lambda rows, _summary: seen.append(
                (tuple(rows), bench.port.live_status().position is not None)
            ))
            await _connect(bench)
            await _ride(bench)
            return seen
        finally:
            await _close(bench, session)

    seen = loop.run_until_complete(go())
    holding = [rows for rows, held in seen if held]
    assert holding, "позиция робота ни разу не открылась — проверять нечего"
    for rows in holding:
        open_rows = _open_rows(rows)
        assert len(open_rows) == 1, rows
        row = open_rows[0]
        assert row.exit_price is None and row.commission_rub is None, row
        assert row.profit_rub is not None and "плавающий" in row.exit_reason, row
    after = [rows for rows, held in seen[seen.index((holding[0], True)):] if not held]
    assert after, "позиция ни разу не закрылась — проверять нечего"
    assert all(not _open_rows(rows) for rows in after), after[-1]
    assert any(rows for rows in after), "после выхода в таблице нет закрытой сделки"


def _journal_only(broker: FakeBroker) -> Callable[[MarketWorker], Awaitable[None]]:
    """Команда робота в журнале со статусом «записана»: ответа на подачу не было."""

    async def plant(worker: MarketWorker) -> None:
        def write(base: CandleStore) -> None:
            run = base.open_journal_session(
                SessionRecord(RunOrigin.LIVE, symbol=SYMBOL), now=DAY.replace(hour=8)
            )
            base.record_broker_command(run.id, BrokerCommand(
                client_order_id=ROBOT_TAKE, action=CommandAction.CREATE,
                order_id="take:long:прошлый", symbol=SYMBOL, side=CommandSide.SELL,
                volume=1, price=251_000.0, account=broker.account,
            ))

        await worker.call(write)

    return plant


def test_a_not_found_order_without_an_answer_keeps_the_position_open(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """Стережёт С1 в ручном пути: 404 по ключу у команды без ответа — не «заявки нет».

    Ответа на подачу тейка программа не получила (строка журнала «записана»),
    брокер по ключу его не находит. Это не доказывает, что заявки нет: она
    могла ещё не появиться. «Закрыть позицию» позицию не закрывает и говорит,
    почему (ревью 13.11.2026, находка 2; решение 0069).
    """

    async def go() -> str:
        bench, session = await _bench(
            tmp_path, broker, FIXED_TAKE, before=_journal_only(broker)
        )
        try:
            broker.foreign_trade("1", 1, 250_000.0, ticker=SYMBOL)
            await _connect(bench, live=False)
            bench.port.close_position()
            await bench.port.wait()
            return bench.said("Закрытие позиции руками")
        finally:
            await _close(bench, session)

    said = loop.run_until_complete(go())
    assert broker.position(SYMBOL) == 1, f"позиция закрыта при неподтверждённом снятии: {said}"
    assert "не подтверждено" in said and "Позиция не закрывается" in said, said


def test_a_silent_status_does_not_stop_the_cancel(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """Стережёт: брокер не ответил про статус — снятие всё равно уходит, позиция закрыта.

    Подтверждение снятия приходит потоком статусов, а не запросом статуса
    (ревью 13.11.2026, находка 3).
    """

    async def go() -> str:
        bench, session = await _bench(
            tmp_path, broker, FIXED_TAKE, before=_journal(broker)
        )
        try:
            await _at_the_broker(broker, ROBOT_TAKE, 251_000.0)
            broker.foreign_trade("1", 1, 250_000.0, ticker=SYMBOL)
            await _connect(bench, live=False)
            broker.status_answers = [_unavailable] * 3
            bench.port.close_position()
            await bench.port.wait()
            return bench.said("Закрытие позиции руками")
        finally:
            await _close(bench, session)

    said = loop.run_until_complete(go())
    assert any(kind == "cancel" for kind, _ in broker.commands), f"снятие не ушло: {said}"
    take = broker.order(ROBOT_TAKE)
    assert take is not None and not take.live, said
    assert broker.position(SYMBOL) == 0, said


class _StalePortfolio:
    """Портфель, который после `after` честных ответов застывает на первом (`/risk` Р1).

    Отставший портфель живого брокера: сделка прошла, а портфель ещё
    показывает позицию до неё.
    """

    def __init__(self, broker: FakeBroker, *, after: int) -> None:
        self.previous = broker.replace_route(("GET", PORTFOLIO_PATH), self)
        self.after = after
        self.first: object = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.after <= 0 and self.first is not None:
            return httpx.Response(200, json=self.first)
        self.after -= 1
        answer = self.previous(request)
        if self.first is None:
            self.first = json.loads(answer.content)
        return answer


class _FailingPortfolio:
    """Портфель отвечает первые `good` раз, дальше — 503 (`/risk` Р3)."""

    def __init__(self, broker: FakeBroker, good: int) -> None:
        self.previous = broker.replace_route(("GET", PORTFOLIO_PATH), self)
        self.good = good

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.good > 0:
            self.good -= 1
            return self.previous(request)
        return httpx.Response(503, text="Service Unavailable")


async def _restart(tmp_path: pathlib.Path, broker: FakeBroker) -> tuple[Bench, BrokerSession]:
    """Перезапуск после боевого дня: бой выключен, лонг 1 и тейк робота у брокера."""
    bench, session = await _bench(tmp_path, broker, FIXED_TAKE, before=_journal(broker))
    await _at_the_broker(broker, ROBOT_TAKE, 251_000.0)
    broker.foreign_trade("1", 1, 250_000.0, ticker=SYMBOL)
    await _connect(bench, live=False)
    return bench, session


def test_an_unread_position_leaves_the_take_alone(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """Стережёт Р3: позиция не прочиталась — тейк не снимается, заявок нет, сказано почему.

    Снять тейк до чтения позиции значило бы при сбое чтения оставить лонг
    без тейка и без закрытия (`/risk` 13.11.2026).
    """

    async def go() -> str:
        bench, session = await _restart(tmp_path, broker)
        try:
            # Первое чтение — номер счёта для исполнителя на одну команду.
            _FailingPortfolio(broker, good=1)
            bench.port.close_position()
            await bench.port.wait()
            return bench.said("Закрытие позиции руками")
        finally:
            await _close(bench, session)

    said = loop.run_until_complete(go())
    assert not any(kind == "cancel" for kind, _ in broker.commands), said
    take = broker.order(ROBOT_TAKE)
    assert take is not None and take.live, f"тейк снят при непрочитанной позиции: {said}"
    assert broker.position(SYMBOL) == 1, said
    assert "прочитать не удалось" in said and "не тронуты" in said, said


def test_a_take_filled_while_cancelling_is_not_sold_again(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """Стережёт Р1: тейк исполнился при снятии, портфель отстал — второй продажи нет.

    Позиция по сделкам после исполнения тейка — ноль, а портфель ещё
    показывает лонг 1: закрывающая по нему продала бы второй раз (шорт 1).
    """

    async def go() -> tuple[str, int]:
        bench, session = await _restart(tmp_path, broker)
        try:
            routes: list[Callable[[httpx.Request], httpx.Response]] = []

            def fill_then_cancel(request: httpx.Request) -> httpx.Response:
                order = broker.order(ROBOT_TAKE)
                if order is not None and order.live:
                    broker.execute(ROBOT_TAKE, 1, 251_000.0)
                return routes[0](request)

            routes.append(broker.replace_route(CANCEL, fill_then_cancel))
            # Номер счёта и позиция перед снятием — свежие, дальше портфель застыл.
            _StalePortfolio(broker, after=2)
            sent = _creates(broker)
            bench.port.close_position()
            await bench.port.wait()
            return bench.said("Закрытие позиции руками"), _creates(broker) - sent
        finally:
            await _close(bench, session)

    said, sent = loop.run_until_complete(go())
    assert sent == 0, f"после исполнения тейка подана вторая продажа: {said}"
    assert broker.position(SYMBOL) == 0, said
    assert "закрывающая заявка не подана" in said, said


def test_a_second_close_while_the_portfolio_lags_is_refused_in_words(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """Стережёт Р1: пока идёт — кнопки погашены; второе нажатие по отставшему портфелю — отказ.

    Закрывающая ушла и исполнилась, портфель ещё показывает лонг 1. Второе
    «Закрыть позицию» отвечает «закрытие уже подано, ждём портфель» и заявок
    не подаёт.
    """

    async def go() -> tuple[str, str, str, int]:
        bench, session = await _restart(tmp_path, broker)
        try:
            stale = _StalePortfolio(broker, after=10_000)
            bench.port.close_position()
            busy = bench.port.live_status().hand_refused
            await bench.port.wait()
            first = bench.said("Закрытие позиции руками")
            stale.after = 0  # портфель застыл на первом ответе — «лонг 1»
            sent = _creates(broker)
            bench.port.close_position()
            await bench.port.wait()
            second = bench.said("Закрытие позиции руками")[len(first):]
            return busy, first, second, _creates(broker) - sent
        finally:
            await _close(bench, session)

    busy, first, second, sent = loop.run_until_complete(go())
    assert busy, "пока ручное действие идёт, кнопки не погашены"
    assert "закрыта" in first, first
    assert broker.position(SYMBOL) == 0, second
    assert sent == 0, f"второе нажатие подало ещё одну заявку: {second}"
    assert "Закрытие уже подано, ждём портфель" in second, second


def test_close_names_live_orders_that_are_not_the_robots(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """Стережёт Р2: живая ручная заявка владельца по инструменту названа в итоге, не снята."""

    async def go() -> str:
        bench, session = await _restart(tmp_path, broker)
        try:
            await _at_the_broker(broker, OWNER_ORDER, 252_000.0)
            bench.port.close_position()
            await bench.port.wait()
            return bench.said("Закрытие позиции руками")
        finally:
            await _close(bench, session)

    said = loop.run_until_complete(go())
    owner = broker.order(OWNER_ORDER)
    assert owner is not None and owner.live, "ручная заявка владельца снята"
    assert "не найденные в журнале робота" in said and "252000" in said, said
