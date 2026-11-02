"""Боевая сторона пары (`app/live_trading.py::LiveDesk`): сверка после связи и занятость.

Замечания `/risk` 2в (01.11.2026). Исполнитель здесь — дублёр с теми полями,
которые `LiveDesk` читает: проверяется правило самой стороны, а не брокер
(брокер — в `tests/test_app_live_pair.py` и `tests/test_app_live_executor.py`).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast
from uuid import UUID

import pytest

from app import live_trading
from app.fact import FactRefused, FactRequest
from app.live_trading import LiveDesk, LiveDesks, Seat
from app.reconcile import Reconciliation
from broker.errors import BrokerError, NoConnection
from market import MSK
from market.commands import (
    BrokerCommand,
    CommandAction,
    CommandSide,
    CommandStatus,
    StoredCommand,
)
from tests.test_app_live_executor import FAST


@dataclass
class _Executor:
    links: int = 1
    connected: bool = True
    undelivered: tuple[str, ...] = ()


@dataclass
class _Book:
    rows: list[object] = field(default_factory=list)

    async def open_commands(self) -> tuple[object, ...]:
        return tuple(self.rows)


def _desk(executor: _Executor) -> LiveDesk:
    seat = Seat(cast(Any, None), "MXZ6", "FAKE-ACCOUNT-1")
    return LiveDesk(cast(Any, executor), book=cast(Any, _Book()), seat=seat, day=None, notes=())


def test_a_failed_reconcile_after_the_link_is_tried_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """Связь вернулась, сверка не прошла — следующий проход сверяет снова.

    Счётчик связи запоминается **после** удачной сверки: запомненный до неё,
    он дал бы следующей заявке уйти без сверки, будто связь не рвалась.
    """
    answers = ["Позицию у брокера прочитать не удалось.", None]
    calls: list[int] = []

    async def fake(executor: object, expected: int) -> Reconciliation:
        calls.append(expected)
        return Reconciliation((), None, expected, answers.pop(0))

    monkeypatch.setattr(live_trading, "reconcile", fake)
    executor = _Executor()
    desk = _desk(executor)
    executor.links = 2

    async def go() -> list[str | None]:
        return [await desk.relinked(1), await desk.relinked(1), await desk.relinked(1)]

    said = asyncio.run(go())
    assert said == ["Позицию у брокера прочитать не удалось.", None, None]
    assert calls == [1, 1], "сверка не повторилась после сбоя или повторилась после успеха"


def test_a_deal_not_yet_given_to_the_engine_keeps_the_fact_busy() -> None:
    """Вход исполнился у брокера, движок его ещё не видел — «факт» занят (С4).

    В это окно позиция у брокера уже есть, а у движка нет: пересборка
    родила бы движок без позиции, которая у брокера уже открыта.
    """
    desk = _desk(_Executor(undelivered=("open:long:2026-06-19T09:45:00+03:00",)))
    busy = asyncio.run(desk.unclosed())
    assert busy and "open:long" in busy[0] and "не отдана" in busy[0], busy


def _take_row(number: int, account: str, price: float | None) -> StoredCommand:
    """Незакрытая команда журнала: продажа 1 — лимитная по `price`, без цены — рыночная."""
    command = BrokerCommand(
        client_order_id=str(UUID(int=number)), action=CommandAction.CREATE,
        order_id="take:long", symbol="MXZ6", side=CommandSide.SELL, volume=1,
        price=price, account=account,
    )
    moment = datetime(2026, 6, 19, 9, 45, tzinfo=MSK)
    return StoredCommand(number, 1, command, moment, CommandStatus.RECORDED, moment, "")


def test_a_resting_level_is_a_limit_order_of_this_account_only() -> None:
    """N3: уровень у запомненной позиции — по живой лимитной **этого** счёта.

    В `userdata/` бывает два токена на два счёта, и одна стратегия на двух
    счетах ставит тейк по одному уровню: живая лимитная чужого счёта
    показала бы у этой позиции тейк, снятый у неё самой. Рыночная —
    не уровень вовсе.
    """
    rows: list[object] = [
        _take_row(1, "FAKE-ACCOUNT-1", 251_375.0),
        _take_row(2, "FAKE-ACCOUNT-2", 251_400.0),
        _take_row(3, "FAKE-ACCOUNT-1", None),
    ]
    seat = Seat(cast(Any, None), "MXZ6", "FAKE-ACCOUNT-1")
    desk = LiveDesk(
        cast(Any, _Executor()), book=cast(Any, _Book(rows)), seat=seat, day=None, notes=()
    )
    assert asyncio.run(desk.resting_levels()) == (251_375.0,)


# --------------------------------------------------- B-081: позиция в отказе включения


#: Ответ на чтение позиции у брокера: число со знаком или отказ брокера.
Read = int | BrokerError

#: Портфель не прочитан — подставной отказ брокера.
UNREAD = NoConnection("проба: портфель не прочитан")


def _answer(answer: Read) -> int:
    if isinstance(answer, BrokerError):
        raise answer
    return answer


@dataclass
class _Preparing:
    """Исполнитель на сборке: поток, чтения позиции по порядку, итог разбора журнала."""

    reads: list[Read]
    connected: bool = True
    notes: tuple[str, ...] = ()
    unsettled: bool = False
    halted: str | None = None
    #: Чтения позиции для отказа после разбора журнала (`broker_position`), по порядку.
    fresh: list[Read] = field(default_factory=list)

    async def wait_connected(self, timeout: float) -> bool:
        return self.connected

    async def check_position(self, expected: int) -> int:
        return _answer(self.reads.pop(0))

    async def broker_position(self) -> int:
        return _answer(self.fresh.pop(0))

    async def settle_open_commands(self) -> tuple[str, ...]:
        return self.notes

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None


@dataclass
class _Session:
    """Сессия брокера на сборке: права на торговлю есть, портфель называет счёт."""

    async def read(self, path: str) -> object:
        return {}

    def require_trade_rights(self, *, reread: bool) -> None:
        return None


SNIPPED = ("Заявка (из журнала: take:long:прошлый) была жива у брокера и снята (решение 0017).",)


@pytest.mark.parametrize(
    ("executor", "rows", "position"),
    [
        pytest.param(_Preparing([], connected=False), [], None, id="stream-not-open"),
        pytest.param(_Preparing([UNREAD]), [], None, id="first-read-failed"),
        pytest.param(_Preparing([1]), [], 1, id="first-read-long"),
        pytest.param(_Preparing([0], unsettled=True), [], 0, id="fate-unknown"),
        pytest.param(_Preparing([0]), [_take_row(1, "FAKE-ACCOUNT-1", 251_375.0)], 0,
                     id="unclosed-command"),
        pytest.param(_Preparing([0], halted="Исполнитель стоит."), [], 0, id="executor-halted"),
        pytest.param(_Preparing([0, UNREAD]), [], 0, id="second-read-failed-nothing-done"),
        pytest.param(_Preparing([0, UNREAD], notes=SNIPPED), [], None,
                     id="second-read-failed-after-a-cancel"),
        pytest.param(_Preparing([0, -1], notes=SNIPPED), [], -1, id="second-read-short"),
        # Разбор журнала снял заявку, и отказ пришёл раньше второго чтения:
        # ноль первого чтения уже не свеж — позиция читается заново (З5).
        pytest.param(_Preparing([0], notes=SNIPPED, unsettled=True, fresh=[-1]), [], -1,
                     id="fate-unknown-after-a-cancel"),
        pytest.param(_Preparing([0], notes=SNIPPED, halted="Исполнитель стоит.", fresh=[UNREAD]),
                     [], None, id="halted-after-a-cancel-fresh-read-failed"),
    ],
)
def test_a_refusal_to_turn_live_on_carries_the_position_only_when_it_was_freshly_read(
    monkeypatch: pytest.MonkeyPatch, executor: _Preparing, rows: list[object],
    position: int | None,
) -> None:
    """B-081: позиция в отказе — свежее чтение у брокера, иначе `None`.

    Порт снимает позицию, запомненную с прошлого боя, только по нулю
    в отказе (`HistoryPort._open_fact`). Ноль без чтения — окно теряет лонг,
    оставшийся у брокера без робота, и закрытие окна о нём не спрашивает.
    Ноль, прочитанный **до** разбора журнала, который потом снял или нашёл
    исполненной заявку прошлого запуска, — уже не свежий.
    """
    monkeypatch.setattr(live_trading, "LiveExecutor", lambda *_args: executor)
    monkeypatch.setattr(live_trading, "StoreBook", lambda *_args: _Book(rows))
    monkeypatch.setattr(live_trading, "parse_positions", lambda _payload: ((), "FAKE-ACCOUNT-1"))
    desks = LiveDesks(lambda: cast(Any, _Session()), cast(Any, None), class_code="SPBFUT")
    request = FactRequest(
        ticker="MXZ6", bar_minutes=15, ruble_per_point=1.0, limits=FAST, session_id=1,
        on_news=lambda: None, notice=lambda _text: None,
    )

    async def go() -> FactRefused:
        with pytest.raises(FactRefused) as caught:
            await desks.open(request)
        return caught.value

    refusal = asyncio.run(go())
    assert refusal.position == position, (
        f"отказ «{refusal.human}» несёт позицию {refusal.position}, а должен {position}"
    )
