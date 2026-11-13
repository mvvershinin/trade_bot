"""Строка «Счёт у брокера» и тексты про скользящий тейк в бою (решение 0070).

Минимум по просьбе владельца счёта: два сторожа, остальное — `BACKLOG.md`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from app.account_panel import AccountPanel, AccountSeen
from app.live_feed import FundsWatch, FundsWatchers
from app.port import _settings_blocks
from broker.account import Account
from broker.orders import ListedOrder, ListedOrderStatus, OrderSide, OrdersPage, OrdersQuery
from broker.session import PORTFOLIO_PATH
from ui.models import MinuteBarLimit, Settings
from ui.settings_dialog import trailing_in_live

TERMS = ("T0", "T1", "T2", "T365")


def broker_portfolio() -> list[dict[str, Any]]:
    """Портфель живого вида 13.11.2026: каждый актив строкой на каждый срок.

    У брокера шорт 1 MXZ6 по 249 650, сейчас 249 675; на счёте 136 770 ₽.
    """
    rows: list[dict[str, Any]] = []
    for term in TERMS:
        rows.append({
            "type": "moneyLimit", "ticker": "RUB", "currency": "RUB", "term": term,
            "quantity": 136770.0, "agreementId": "AGREEMENT-1", "account": "ACCOUNT-1",
        })
        rows.append({
            "type": "futuresHolding", "ticker": "MXZ6", "board": "SPBFUT", "term": term,
            "quantity": -1.0, "balancePrice": 249650.0, "currentPrice": 249675.0,
            "minimumStep": 25.0, "agreementId": "AGREEMENT-1", "account": "ACCOUNT-1",
        })
    return rows


def take_order() -> ListedOrder:
    """Живая лимитная покупка 1 по 249 400 — тейк шорта, как его видит список брокера."""
    return ListedOrder(
        status=ListedOrderStatus.ACTIVE, order_num=1, exchange_order_id="1",
        ticker="MXZ6", class_code="SPBFUT", side=OrderSide.BUY, order_type=2,
        order_quantity=1, executed_quantity=0, remained_quantity=1, price=249400.0,
        average_price=None, take_price=None, stop_price=None, reject_reason=None,
        ordered_at=None, updated_at=None,
    )


class BrokerSession:
    """Брокер: портфель, лимиты без рублей, список заявок. Помнит запросы списка."""

    def __init__(self) -> None:
        self.queries: list[OrdersQuery] = []

    async def read(self, path: str, **_: object) -> object:
        return broker_portfolio() if path == PORTFOLIO_PATH else {"moneyLimits": []}

    async def search_orders(self, query: OrdersQuery) -> OrdersPage:
        self.queries.append(query)
        return OrdersPage(records=(take_order(),), total_records=1, total_pages=1)

    def stored(self) -> SimpleNamespace:
        return SimpleNamespace(account="ACCOUNT-1")


def test_the_account_line_tells_position_and_take_from_the_broker() -> None:
    """Стережёт: строка «Счёт у брокера» — позиция, тейк и деньги из ответа брокера.

    Подставлены портфель (шорт 1 по 249 650, сейчас 249 675, 136 770 ₽) и список
    заявок (покупка 1 по 249 400). Заход — настоящий `FundsWatch` с настоящим
    `Account`; строку собирает `AccountPanel`. Сделок и позиции программы у неё
    на входе нет вовсе: строка обязана сказать шорт и тейк брокера, а не «лонг».
    """
    session: Any = BrokerSession()
    seen: list[AccountSeen] = []
    watch = FundsWatch(Account(session), ticker="MXZ6", watchers=FundsWatchers(
        funds=lambda *_: None, say=lambda *_: None, halt=lambda *_: None,
        halted=lambda: False, account=seen.append,
    ))

    async def go() -> None:
        watch.start()
        for _ in range(200):
            if seen:
                break
            await asyncio.sleep(0.005)
        await watch.aclose()

    asyncio.run(go())

    assert seen, "заход опроса не отдал прочитанное строке счёта"
    line = AccountPanel().line(seen[0], at=datetime.now(UTC), ruble_per_point=1.0)
    text = line.text.replace("\xa0", " ")  # тысячи — неразрывным пробелом
    for part in (
        "Счёт у брокера: MXZ6: шорт 1 по 249 650, сейчас 249 675 (−25 ₽)",
        "тейк 249 400 стоит",
        "деньги 136 770 ₽",
        " МСК",
    ):
        assert part in text, f"{part!r} нет в «{text}»"
    assert "лонг" not in text, text
    assert [(q.statuses, q.tickers) for q in session.queries] == [
        ((ListedOrderStatus.ACTIVE,), ("MXZ6",))
    ], "заявки спрошены не живые или не по инструменту"


def test_trailing_in_live_is_not_forbidden_and_the_words_name_the_broker() -> None:
    """Стережёт снятие D-141 (`D-196`, владелец 13.11.2026): скользящий тейк в бою разрешён.

    В бою его ведёт брокер условной заявкой, и запрет «свеча не крупнее порога
    минуток» больше не о чем. Подпись в настройках говорит, кто ведёт уровень.
    """
    trailing = Settings(
        timeframe="15 минут", trailing_enabled=True, take_profit_enabled=True,
        minute_bar_limit=MinuteBarLimit.FIFTEEN, price_step=25.0,
    )
    assert _settings_blocks(trailing, trailing) == (), "скользящий уровень в бою снова запрещён"
    assert "брокер" in trailing_in_live(), trailing_in_live()
