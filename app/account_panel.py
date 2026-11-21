"""Строка «Счёт у брокера» — по ответу брокера, а не по памяти программы (решение 0070).

Владелец счёта 13.11.2026: «я вообще не понимаю, в каком состоянии счёт».
Окно показывало позицию «факта» — то, что **программа думает** о счёте, —
и после ручного действия или остановки это расходилось с брокером.

Здесь слова собираются только из того, что брокер ответил одним заходом
опроса (`app/live_feed.py::FundsWatch`): позиция по инструменту из портфеля
(строка `T0`: сторона, объём, `balancePrice`, `currentPrice`), живые заявки
по инструменту из списка заявок, рубли из строки `moneyLimit` RUB `T0`.
Сделки движка сюда не приходят — подменить брокера нечем.

Журнал команд даёт **только ключи** заявок робота (`B-105`): живую условную
робота 16.11.2026 статус по ключу (11) показал, а проверочное чтение списка
(15) — нет. Жива ли заявка, отвечает брокер по ключу (`robot_live_orders`);
журнал «стоит» не утверждает никогда.

Единственное число не от брокера — стоимость пункта (биржа, настройки), ею
разница цен переводится в рубли. Нет её — результат в пунктах.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import NamedTuple
from uuid import UUID

from broker.account import AccountSnapshot, Position
from broker.errors import BrokerError
from broker.orders import (
    FINAL_STATUSES,
    ListedOrderStatus,
    OrderSide,
    OrdersPage,
    OrderState,
    OrderType,
)
from market.commands import CommandAction, CommandSide, StoredCommand
from ui.formatting import fmt_level, fmt_money, fmt_number, fmt_time, fmt_volume
from ui.models import AccountLine

__all__ = ["HEAD", "AccountPanel", "AccountSeen", "RobotOrder", "robot_live_orders"]

#: Начало строки: чьи это данные.
HEAD = "Счёт у брокера"

#: Сторона сделки словами — для заявок списка.
_DEAL_SIDE: dict[OrderSide, str] = {OrderSide.BUY: "покупка", OrderSide.SELL: "продажа"}
#: Какой стороной закрывается позиция: лонг — продажей, шорт — покупкой.
_CLOSING: dict[bool, OrderSide] = {True: OrderSide.SELL, False: OrderSide.BUY}
#: Сторона команды журнала — сторона заявки брокера.
_COMMAND_SIDE: dict[CommandSide, OrderSide] = {
    CommandSide.BUY: OrderSide.BUY,
    CommandSide.SELL: OrderSide.SELL,
}


@dataclass(frozen=True, slots=True)
class RobotOrder:
    """Живая заявка робота: ключ — из журнала команд, «жива» — ответ брокера по ключу (11).

    :param level: цена из журнала: у лимитной — цена заявки, у условной —
        цена включения (так её пишет исполнитель, `D-197` п. 5).
    :param conditional: брокер назвал её условной (`orderType` `5`).
    """

    key: str
    side: OrderSide | None
    volume: int | None
    level: float | None
    conditional: bool


@dataclass(frozen=True, slots=True)
class AccountSeen:
    """Один заход опроса счёта: что брокер ответил — или почему не ответил.

    :param ticker: инструмент окна, по нему позиция и заявки.
    :param snapshot: снимок счёта; `None` — не прочитан, причина в `trouble`.
    :param orders: живые заявки по инструменту; `None` — список не прочитан,
        причина в `orders_trouble`.
    :param robot: живые заявки робота по ключам журнала (`robot_live_orders`);
        `robot_trouble` — про какие спросить не удалось.
    """

    ticker: str
    snapshot: AccountSnapshot | None
    trouble: str = ""
    orders: OrdersPage | None = None
    orders_trouble: str = ""
    robot: tuple[RobotOrder, ...] = ()
    robot_trouble: str = ""


class AccountPanel:
    """Слова строки по заходам опроса. Помнит последнее удачное чтение.

    Отказ чтения не стирает строку: «не прочитан в 15:28:07» плюс прошлое
    удачное со своим временем. Пустая строка вместо отказа читалась бы как
    «счёт пуст» (правило 13).
    """

    def __init__(self) -> None:
        #: Последняя удачная строка (без хвоста «на ЧЧ:ММ:СС») и её время.
        self._good: tuple[str, datetime] | None = None

    def line(
        self, seen: AccountSeen, *, at: datetime, ruble_per_point: float | None
    ) -> AccountLine:
        """Строка окна по заходу. `at` — когда заход кончился (часы порта)."""
        snapshot = seen.snapshot
        if snapshot is None:
            return self._failed(seen.trouble or "брокер не ответил", at)
        parts = [
            _position_words(seen.ticker, snapshot, ruble_per_point),
            _orders_words(seen, snapshot),
            _cash_words(snapshot.cash_rub),
        ]
        body = " · ".join(part for part in parts if part)
        self._good = (body, snapshot.taken_at)
        return AccountLine(
            text=f"{HEAD}: {body} · на {fmt_time(snapshot.taken_at, seconds=True)} МСК",
            tooltip=_TOOLTIP,
            trouble=bool(seen.orders_trouble or seen.robot_trouble),
        )

    def _failed(self, trouble: str, at: datetime) -> AccountLine:
        head = f"{HEAD}: не прочитан в {fmt_time(at, seconds=True)} МСК"
        if self._good is None:
            return AccountLine(text=f"{head} — {trouble}", tooltip=trouble, trouble=True)
        body, when = self._good
        return AccountLine(
            text=f"{head}; последнее удачное — на {fmt_time(when, seconds=True)}: {body}",
            tooltip=f"Почему не прочитан: {trouble}",
            trouble=True,
        )


_TOOLTIP = (
    "Что ответил брокер: позиция по инструменту (средняя цена — балансовая цена "
    "брокера), живые заявки по инструменту и рубли на счёте (срок T0).\n"
    "Условную заявку робота (скользящий тейк) брокер может не показать в списке "
    "заявок — тогда она прочитана по её ключу из журнала команд: «по ключу».\n"
    "Не память программы: перечитывается каждые 30 секунд и сразу после каждой "
    "команды брокеру. Результат — (текущая цена − средняя) × объём × стоимость пункта."
)


def _position_words(
    ticker: str, snapshot: AccountSnapshot, ruble_per_point: float | None
) -> str:
    """«MXZ6: шорт 1 по 249 650, сейчас 249 675 (−25 ₽)» или «MXZ6: позиции нет»."""
    try:
        position = snapshot.position(ticker)
    except BrokerError as error:
        return f"{ticker}: позиция не прочитана — {error.human}"
    if position is None or not position.open:
        return f"{ticker}: позиции нет"
    long = position.quantity > 0
    words = f"{ticker}: {'лонг' if long else 'шорт'} {fmt_volume(abs(position.quantity))}"
    if position.average_price:
        words += f" по {fmt_level(position.average_price)}"
    if position.current_price:
        words += f", сейчас {fmt_level(position.current_price)}"
        result = _result(position, ruble_per_point)
        if result:
            words += f" ({result})"
    return words


def _result(position: Position, ruble_per_point: float | None) -> str:
    """Результат позиции по ценам брокера: рубли, если известна стоимость пункта."""
    if not position.average_price or not position.current_price:
        return ""
    points = (position.current_price - position.average_price) * position.quantity
    if ruble_per_point:
        return fmt_money(points * ruble_per_point, 0, sign=True)
    body = fmt_number(abs(points), 0 if float(points).is_integer() else 2)
    return f"{'−' if points < 0 else '+'}{body} пт"


class _Shown(NamedTuple):
    """Живая заявка в строке — из списка брокера или по ключу робота."""

    side: OrderSide | None
    volume: float | None
    level: float | None
    conditional: bool
    #: Прочитана по ключу робота, а в списке брокера её нет (`B-105`).
    by_key: bool


def _live(seen: AccountSeen) -> list[_Shown]:
    """Список брокера плюс заявки робота, которых в нём нет (по стороне и уровню)."""
    listed = [] if seen.orders is None else [
        _Shown(
            order.side, order.remained_quantity or order.order_quantity, order.level,
            order.conditional, by_key=False,
        )
        for order in seen.orders.records
        if order.status is ListedOrderStatus.ACTIVE
        and (order.ticker is None or order.ticker.upper() == seen.ticker.upper())
    ]
    known = {(one.side, one.level) for one in listed}
    return listed + [
        _Shown(one.side, one.volume, one.level, one.conditional, by_key=True)
        for one in seen.robot
        if (one.side, one.level) not in known
    ]


def _orders_words(seen: AccountSeen, snapshot: AccountSnapshot) -> str:
    """Тейк позиции и прочие живые заявки по инструменту — по ответу брокера.

    «Тейка нет» и «живых заявок нет» говорятся, только если прочитаны и список,
    и статусы заявок робота: иначе это неправда о том, чего не видели.
    """
    live = _live(seen)
    certain = seen.orders is not None and not seen.robot_trouble
    parts: list[str] = []
    if seen.orders is None:
        parts.append(f"заявки не прочитаны — {seen.orders_trouble or 'брокер не ответил'}")
    try:
        position = snapshot.position(seen.ticker)
    except BrokerError:
        position = None
    if position is not None and position.open:
        closing = _CLOSING[position.quantity > 0]
        takes = [order for order in live if order.side is closing]
        rest = [order for order in live if order.side is not closing]
        if takes:
            parts.append("; ".join(map(_take_words, takes)))
        elif certain:
            parts.append("тейка у брокера нет")
    else:
        rest = live
        if not live and certain:
            parts.append("живых заявок нет")
    if rest:
        parts.append("заявки: " + ", ".join(map(_order_words, rest)))
    if seen.robot_trouble:
        parts.append(seen.robot_trouble)
    more = seen.orders is not None and (seen.orders.total_pages or 1) > 1
    return "; ".join(parts) + (" (показана первая страница)" if more else "")


def _take_words(order: _Shown) -> str:
    """«тейк 249 400 стоит» или «тейк стоит условной заявкой, включение 248 150»."""
    if not order.conditional:
        return f"тейк {fmt_level(order.level)} стоит{_by_key(order)}"
    return f"тейк стоит условной заявкой, включение {fmt_level(order.level)}{_by_key(order)}"


def _order_words(order: _Shown) -> str:
    side = "?" if order.side is None else _DEAL_SIDE[order.side]
    volume = fmt_volume(order.volume) if order.volume else "?"
    if order.conditional:
        price = f" условная, включение {fmt_level(order.level)}"
    else:
        price = "" if order.level is None else f" по {fmt_level(order.level)}"
    return f"{side} {volume}{price}{_by_key(order)}"


def _by_key(order: _Shown) -> str:
    return " (заявка робота, по ключу)" if order.by_key else ""


def _cash_words(cash: float | None) -> str:
    return "деньги не прочитаны" if cash is None else f"деньги {fmt_money(cash, 0)}"


async def robot_live_orders(
    commands: Callable[[], Awaitable[Sequence[StoredCommand]]],
    status: Callable[[UUID], Awaitable[OrderState | None]],
    *,
    ticker: str,
    account: str | None,
) -> tuple[tuple[RobotOrder, ...], str]:
    """Живые заявки робота по инструменту: ключи — журнал команд, жизнь — брокер (11).

    `B-105`: живую условную робота 16.11.2026 статус по ключу показал, а
    проверочное чтение списка заявок (15) — нет. Строка счёта поэтому
    спрашивает о каждой незакрытой заявке робота по её ключу. Брокер
    ответил «жива» — заявка в строке; кончилась или 404 — нет: журнал
    «стоит» не утверждает.

    :param commands: незакрытые команды журнала (`open_broker_commands`).
    :param status: статус по ключу (`Account.order_state`).
    :param account: номер договора снимка; `None` — не назван, ключи всех
        счетов (чужой ключ брокер этого токена не найдёт — 404).
    :returns: заявки и почему не про все удалось спросить (пусто — про все).
        Отказ чтения журнала — наружу: его называет опрос счёта.
    """
    found: list[RobotOrder] = []
    trouble = ""
    for row in await commands():
        command = row.command
        if command.action is not CommandAction.CREATE:
            continue
        if command.symbol.upper() != ticker.upper() or (account and command.account != account):
            continue
        try:
            state = await status(UUID(command.client_order_id))
        except BrokerError as error:
            trouble = f"статус заявки робота не прочитан — {error.human}"
            continue
        if state is None or state.latest.status in FINAL_STATUSES:
            continue
        found.append(RobotOrder(
            key=command.client_order_id,
            side=None if command.side is None else _COMMAND_SIDE[command.side],
            volume=command.volume,
            level=command.price,
            conditional=any(
                report.order_type is OrderType.CONDITIONAL for report in state.reports
            ),
        ))
    return tuple(found), trouble
