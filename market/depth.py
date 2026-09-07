"""Замер доступной глубины истории (задача Э1-3).

Зачем это отдельная задача, а не строчка в документации
------------------------------------------------------
Пятиминутки собираются из минуток — иначе никак, ISS пятиминутный интервал
не отдаёт вовсе. Значит глубина проверки стратегии на пятиминутках равна
глубине **минутной** истории. Умолчания продукта (тейк 0,5 %, окно 10:05–11:00)
подобраны на июне–августе 2026 и на том же отрезке проверены — это подгонка,
признанная в ТЗ дважды. Пункт приёмки требует показать результат на периоде,
в подборе не участвовавшем. Если минутной истории короче, чем нужно, закрывать
этот пункт **нечем**, и узнать об этом надо до Э1-13, а не после.

Что здесь считается замером
---------------------------
Два разных числа, и путать их нельзя:

`declared`
    что биржа **заявляет** через `candleborders`. Один запрос, но это
    обещание сервера, а не проверка.

`measured`
    что **реально** скачалось и легло в базу: первая и последняя минутка,
    число торговых дней, число минуток. Только это и есть замер.

Расхождение между ними — сам по себе результат: заявленная граница может
оказаться шире фактической.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from market.iss import CandleBorder, IssClient, Market
from market.storage import CandleStore

__all__ = ["Depth", "declared_depth", "measured_depth"]


@dataclass(frozen=True, slots=True)
class Depth:
    """Фактическая глубина минутной истории по данным базы."""

    symbol: str
    first: datetime | None
    last: datetime | None
    minutes: int
    trading_days: int
    days: list[date]

    @property
    def calendar_days(self) -> int:
        if self.first is None or self.last is None:
            return 0
        return (self.last.date() - self.first.date()).days + 1

    def __str__(self) -> str:
        if self.first is None or self.last is None:
            return f"{self.symbol}: минутной истории нет"
        return (
            f"{self.symbol}: {self.first:%Y-%m-%d %H:%M} … {self.last:%Y-%m-%d %H:%M} МСК, "
            f"{self.trading_days} торговых дней, {self.calendar_days} календарных, "
            f"{self.minutes} свечей"
        )


def declared_depth(client: IssClient, symbol: str, market: Market) -> dict[int, CandleBorder]:
    """Что сервер заявляет по каждому интервалу. Обещание, а не проверка."""
    return {border.interval: border for border in client.borders(symbol, market=market)}


def measured_depth(store: CandleStore, symbol: str) -> Depth:
    """Что реально скачалось и лежит в базе."""
    coverage = store.coverage(symbol)
    days = store.trading_days(symbol)
    return Depth(
        symbol=symbol,
        first=coverage.first,
        last=coverage.last,
        minutes=coverage.count,
        trading_days=len(days),
        days=days,
    )
