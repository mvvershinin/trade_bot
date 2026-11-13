"""Скользящий тейк у брокера условной заявкой (`D-196`): вход, выход, исполнение.

Решение владельца 13.11.2026: скользящий тейк в бою всегда у брокера, подтяжки
роботом нет. Движок с первой минуты позиции подаёт вооружение с `trail_offset`
(цена включения = вход ± порог), исполнитель ставит по нему условную заявку
(13, `orderType` `5`), а дальнейшие вооружения этого имени к брокеру не несёт.

Оснастка — `tests/test_app_live_executor.py`: подставной брокер
(`tests/fake_broker.py`), журнал команд в настоящей базе во временной папке.
Три сторожа — минимум по слову владельца; остальное — `BACKLOG.md`, D-196.
"""

from __future__ import annotations

import pathlib
from datetime import datetime, timedelta

import pytest
import websockets

from broker.secret import Secret
from broker.token_store import TokenStore
from broker.tokens import TokenScope
from engine.contracts import ExitReason, LevelTouch, OrderAction, OrderRequest, Side
from tests.fake_broker import FakeBroker
from tests.test_app_live_executor import (
    BAR0,
    BAR1,
    FAST,
    Rig,
    arm,
    bar,
    disarm,
    filled,
    heard,
    leave,
    long_open,
    make_rig,
    only_fill,
    run,
    submit_on_signal,
    take_name,
)
from tests.test_broker_session import FAKE_REFRESH, START

#: Цена включения: вход 100 + порог 0,5 %, на сетке шага 0,5 — как кладёт движок.
ACTIVATION = 100.5
#: Отступ уровня от лучшей цены, проценты.
OFFSET = 0.2


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


def conditional(at: datetime = BAR0) -> OrderRequest:
    """Вооружение «скользящий тейк ведёт брокер» — как его подаёт движок до порога."""
    return OrderRequest(
        action=OrderAction.ARM_TAKE_PROFIT, side=Side.LONG, volume=1, submitted_at=at,
        reason="условная", order_id=take_name(), price=ACTIVATION,
        touch=LevelTouch.RISE, exit_reason=ExitReason.TRAILING_TAKE, trail_offset=OFFSET,
    )


def conditional_bodies(rig: Rig) -> list[dict[str, object]]:
    """Тела созданий условных заявок, ушедших к брокеру."""
    return [body for body in rig.creates() if body["orderType"] == "5"]


def test_an_entry_puts_one_conditional_at_the_broker_and_the_robot_does_not_move_it(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """(а) Стережёт: после входа у брокера одна условная закрывающей стороны.

    Тип `5`, цена включения, отступ и спред в процентах, исполнение лимитное —
    поле в поле как в живой пробе 13.11.2026; без `price` и `timeInForce`.
    Повтор условной на следующей свече и уровень программы после порога
    к брокеру не идут: подтяжки роботом нет (решение владельца 13.11.2026).
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            await rig.executor.submit(conditional())
            await rig.executor.submit(conditional(at=BAR1))
            await rig.executor.submit(arm(100.3, LevelTouch.FALL, at=BAR1))
            [body] = conditional_bodies(rig)
            assert {
                name: body.get(name)
                for name in (
                    "side", "orderQuantity", "takePrice", "offset", "offsetType",
                    "executionTakeProfitType", "spread", "spreadType",
                )
            } == {
                "side": "2", "orderQuantity": 1, "takePrice": ACTIVATION, "offset": OFFSET,
                "offsetType": 1, "executionTakeProfitType": 2,
                "spread": FAST.trailing_spread_percent, "spreadType": 1,
            }
            assert "price" not in body and "timeInForce" not in body, body
            assert len(rig.creates()) == 2, f"кроме входа и условной ушло ещё: {rig.creates()}"
            assert [kind for kind, _ in rig.broker.commands].count("cancel") == 0
            assert rig.executor.halted is None, rig.executor.halted

    run(scenario())


def test_a_signal_exit_cancels_the_conditional_before_the_exit_order(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """(б) Стережёт порядок: условная снята (статус `4`) раньше, чем ушла заявка выхода.

    Иначе у брокера живут обе: выход закрывает лонг, условная продаёт ещё раз —
    шорт без сигнала.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            await rig.executor.submit(conditional())
            [body] = conditional_bodies(rig)
            rig.clock.now = BAR1 + timedelta(seconds=5)
            await rig.executor.submit(disarm())
            placed = rig.broker.order(str(body["clientOrderId"]))
            assert placed is not None and placed.status == "4", "условная не снята"
            rig.exchange.plans.append(filled((1, 104.0)))
            await submit_on_signal(rig, leave())
            fill = only_fill(await rig.executor.fills_at(bar(BAR1, unsettled=True)))
            assert (fill.action, fill.price) == (OrderAction.CLOSE, 104.0)
            assert [kind for kind, _ in rig.broker.commands] == [
                "create", "create", "cancel", "create",
            ], rig.broker.commands
            assert rig.broker.position() == 0

    run(scenario())


def test_the_conditional_filled_by_its_key_is_the_take_deal_and_the_robot_goes_on(
    tmp_path: pathlib.Path, store: TokenStore, broker: FakeBroker
) -> None:
    """(в) Стережёт: исполнение условной по её ключу — сделка выхода по уровню движку.

    Как в живой пробе: `orderStatus` `2`, `executionType` `11` по ключу условной.
    Сделка — под именем тейка, по средней цене брокера, по закрытой свече;
    робот не стоит, новых заявок не подаёт.
    """

    async def scenario() -> None:
        async with make_rig(tmp_path, store, broker) as rig:
            await long_open(rig)
            await rig.executor.submit(conditional())
            [body] = conditional_bodies(rig)
            rig.broker.execute(str(body["clientOrderId"]), 1, 100.8, execution_type="11")
            await heard()
            assert await rig.executor.fills_at(bar(BAR0, unsettled=True)) == []
            fill = only_fill(await rig.executor.fills_at(bar(BAR0)))
            assert (fill.action, fill.order_id, fill.price, fill.at) == (
                OrderAction.CLOSE, take_name(), 100.8, BAR0,
            )
            assert rig.executor.halted is None, rig.executor.halted
            assert rig.broker.position() == 0
            assert len(rig.creates()) == 2, rig.creates()

    run(scenario())
