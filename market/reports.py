"""Отчёт о загрузке — то, что уходит в журнал после каждой докачки.

Отдельный модуль, чтобы хранилище и догрузка не импортировали друг друга.

Отчёт существует ради одного требования: **тихой потери свечи быть не должно**.
Пока числа «сколько запрошено, сколько пришло, сколько записано, сколько
отвергнуто, сколько минут отсутствует» не выписаны рядом, недокачанная история
выглядит точно так же, как полная.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from market.gaps import Gap

__all__ = ["LoadReport"]


@dataclass(slots=True)
class LoadReport:
    """Что произошло при одной загрузке."""

    symbol: str
    source: str
    requested_from: date | None = None
    requested_to: date | None = None
    ranges: list[tuple[date, date]] = field(default_factory=list)

    fetched: int = 0        # свечей пришло с сервера (после снятия дублей страниц)
    inserted: int = 0       # записано новых
    updated: int = 0        # перезаписано поверх данных источника не выше рангом
    kept: int = 0           # отвергнуто: в базе данные источника выше рангом
    duplicates: int = 0     # одна и та же минутка пришла в разных страницах
    #: Сколько свечей схлопнулось в уже занятую минуту при записи. У ISS это
    #: всегда ноль: сервер метит минутку ровно, `10:05:00`. Не ноль означает,
    #: что источник прислал одну минуту несколько раз — например, с секундами.
    #: Своя колонка в журнале (`data_load.collapsed`) есть: счётчик целостности,
    #: живущий только внутри русской фразы, нельзя ни отсортировать,
    #: ни просуммировать по журналу.
    collapsed: int = 0

    missing_minutes: int = 0
    #: Разрывы **внутри одного дня** от порога. Только они идут в журнал
    #: отдельными записями и только они считаются в `data_load.gap_count`.
    gaps: list[Gap] = field(default_factory=list)
    #: Сколько разрывов перешагнуло календарную дату: ночь, выходные,
    #: праздники. Их число, а не они сами: год минуток даёт под три сотни
    #: таких разрывов, и в общем счёте они топят те несколько, ради которых
    #: счёт и ведётся. Отдельной колонкой `data_load.overnight_gaps`.
    overnight_gaps: int = 0

    #: Дни, за которые ответ не дошёл: сервер оборвал выдачу раньше конца
    #: запрошенного куска. Загруженными они НЕ помечены и будут перезапрошены.
    #: Отдельно от `gaps` намеренно: разрыв внутри дня и недошедший день —
    #: разные события, и второе означает, что истории может не хватать
    #: там, где по журналу всё в порядке.
    incomplete: list[date] = field(default_factory=list)

    pages: int = 0
    requests: int = 0
    retries: int = 0

    first_time: datetime | None = None
    last_time: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    note: str = ""

    @property
    def written(self) -> int:
        return self.inserted + self.updated

    def summary(self) -> str:
        """Одна строка для журнала — человеческим языком, а не кодом."""
        period = (
            f"{self.requested_from} … {self.requested_to}"
            if self.requested_from and self.requested_to
            else "период не задан"
        )
        parts = [
            f"{self.symbol} ({self.source}): {period}",
            f"пришло {self.fetched}",
            f"записано {self.inserted} новых, {self.updated} обновлено",
        ]
        if self.kept:
            parts.append(f"{self.kept} отвергнуто — в базе данные надёжнее")
        if self.duplicates:
            parts.append(f"{self.duplicates} дублей со страниц отброшено")
        if self.collapsed:
            parts.append(
                f"{self.collapsed} свечей схлопнулось в уже занятую минуту — "
                "источник прислал одну минуту несколько раз, в базе осталась "
                "последняя"
            )
        parts.append(f"минут без свечи: {self.missing_minutes}")
        if self.gaps:
            parts.append(f"разрывов внутри дня от порога: {len(self.gaps)}")
        if self.overnight_gaps:
            parts.append(f"переходов через сутки: {self.overnight_gaps}")
        if self.retries:
            parts.append(f"перезапросов: {self.retries}")
        if self.note:
            parts.append(self.note)
        return "; ".join(parts)
