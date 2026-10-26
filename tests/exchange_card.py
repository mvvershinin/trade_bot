"""Подставная карточка инструмента биржи — шаг цены и стоимость пункта.

Шаг цены программа берёт у биржи сама (решение владельца счёта 05.10.2026):
порт, собранный без ответа биржи, шага не знает, и заявка «с предельной
ценой» у него идёт «по рынку». Проверкам, которым нужен шаг, он подаётся
отсюда — тем же путём, что в программе (`HistoryPort.attach_exchange`).
В сеть эти проверки не ходят.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from market import PointValue
from market.iss import InstrumentSpec


def card(symbol: str, step: float, rubles: float = 1.0) -> PointValue:
    """Ответ биржи: карточка с шагом `step` и `rubles` рублей в пункте."""
    spec = InstrumentSpec(
        secid=symbol, board="RFUD", price_step=step, step_price=step * rubles,
        lot=1.0, initial_margin=None, exchange_fee=None, scalper_fee=None,
        quoted_at="2026-10-05 07:00:01",
    )
    told = f"биржей — карточка {symbol} (RFUD): шаг цены {step:g}"
    return PointValue(symbol, rubles, told, spec)


def exchange(step: float, rubles: float = 1.0) -> Callable[[str], Awaitable[PointValue]]:
    """Подставная биржа: на любой тикер — карточка с этим шагом."""

    async def ask(symbol: str) -> PointValue:
        return card(symbol, step, rubles)

    return ask
