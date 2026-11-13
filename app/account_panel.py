"""Строка «Счёт у брокера» — по ответу брокера, а не по памяти программы (решение 0070).

Владелец счёта 13.11.2026: «я вообще не понимаю, в каком состоянии счёт».
Окно показывало позицию «факта» — то, что **программа думает** о счёте, —
и после ручного действия или остановки это расходилось с брокером.

Здесь слова собираются только из того, что брокер ответил одним заходом
опроса (`app/live_feed.py::FundsWatch`): позиция по инструменту из портфеля
(строка `T0`: сторона, объём, `balancePrice`, `currentPrice`), живые заявки
по инструменту из списка заявок, рубли из строки `moneyLimit` RUB `T0`.
Ни сделки движка, ни журнал команд сюда не приходят — сравнивать не с чем,
и подменить брокера нечем.

Единственное число не от брокера — стоимость пункта (биржа, настройки), ею
разница цен переводится в рубли. Нет её — результат в пунктах.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from broker.account import AccountSnapshot, Position
from broker.errors import BrokerError
from broker.orders import ListedOrder, ListedOrderStatus, OrderSide, OrdersPage
from ui.formatting import fmt_level, fmt_money, fmt_number, fmt_time, fmt_volume
from ui.models import AccountLine

__all__ = ["AccountPanel", "AccountSeen", "HEAD"]

#: Начало строки: чьи это данные.
HEAD = "Счёт у брокера"

#: Сторона сделки словами — для заявок списка.
_DEAL_SIDE: dict[OrderSide, str] = {OrderSide.BUY: "покупка", OrderSide.SELL: "продажа"}
#: Какой стороной закрывается позиция: лонг — продажей, шорт — покупкой.
_CLOSING: dict[bool, OrderSide] = {True: OrderSide.SELL, False: OrderSide.BUY}


@dataclass(frozen=True, slots=True)
class AccountSeen:
    """Один заход опроса счёта: что брокер ответил — или почему не ответил.

    :param ticker: инструмент окна, по нему позиция и заявки.
    :param snapshot: снимок счёта; `None` — не прочитан, причина в `trouble`.
    :param orders: живые заявки по инструменту; `None` — список не прочитан,
        причина в `orders_trouble`.
    """

    ticker: str
    snapshot: AccountSnapshot | None
    trouble: str = ""
    orders: OrdersPage | None = None
    orders_trouble: str = ""


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
            trouble=bool(seen.orders_trouble),
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


def _orders_words(seen: AccountSeen, snapshot: AccountSnapshot) -> str:
    """Тейк позиции и прочие живые заявки по инструменту — по списку брокера."""
    if seen.orders is None:
        return f"заявки не прочитаны — {seen.orders_trouble or 'брокер не ответил'}"
    live = [
        order for order in seen.orders.records
        if order.status is ListedOrderStatus.ACTIVE
        and (order.ticker is None or order.ticker.upper() == seen.ticker.upper())
    ]
    try:
        position = snapshot.position(seen.ticker)
    except BrokerError:
        position = None
    if position is not None and position.open:
        closing = _CLOSING[position.quantity > 0]
        takes = [order for order in live if order.side is closing]
        rest = [order for order in live if order.side is not closing]
        said = (
            "тейк " + ", ".join(fmt_level(order.price) for order in takes) + " стоит"
            if takes else "тейка у брокера нет"
        )
    else:
        rest, said = live, "" if live else "живых заявок нет"
    if rest:
        said = "; ".join(filter(None, [said, "заявки: " + ", ".join(map(_order_words, rest))]))
    more = (seen.orders.total_pages or 1) > 1
    return said + (" (показана первая страница)" if more else "")


def _order_words(order: ListedOrder) -> str:
    side = "?" if order.side is None else _DEAL_SIDE[order.side]
    volume = order.remained_quantity or order.order_quantity
    price = "" if order.price is None else f" по {fmt_level(order.price)}"
    return f"{side} {fmt_volume(volume) if volume else '?'}{price}"


def _cash_words(cash: float | None) -> str:
    return "деньги не прочитаны" if cash is None else f"деньги {fmt_money(cash, 0)}"
