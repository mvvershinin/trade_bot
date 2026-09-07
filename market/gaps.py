"""Разрывы в минутном ряду.

Что здесь считается разрывом и почему именно так
------------------------------------------------
Биржа **не отдаёт** минуту, в которую не было ни одной сделки. Поэтому
«пропущенная минутка» и «потерянная минутка» — разные вещи, и различить их
по самому ряду нельзя: у слоя `market/` нет торгового календаря и он не должен
его выдумывать.

Отсюда правило: слой сообщает **факты**, а не выводы.

* считается **каждая** отсутствующая минута между первой и последней имеющейся —
  общее число попадает в отчёт о загрузке целиком, ничего не округляется
  и не отбрасывается;
* подряд идущие отсутствующие минуты собираются в отрезки; отрезок длиной
  от `min_minutes` попадает в журнал отдельной записью;
* у каждого отрезка отмечается, пересекает ли он смену календарной даты —
  ночной перерыв в торгах выглядит как разрыв на десять часов, и без этой
  пометки журнал забивается им каждый день.

Вывод «это был перерыв в торгах, а не потеря данных» делает человек или
верхний слой с календарём. Здесь его нет.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from market.candles import ensure_msk

__all__ = ["Gap", "find_gaps", "count_missing_minutes"]

_MINUTE = timedelta(minutes=1)


@dataclass(frozen=True, slots=True)
class Gap:
    """Непрерывный отрезок отсутствующих минуток.

    `start` — первая отсутствующая минута, `end` — последняя отсутствующая
    (включительно). Границы разрыва — это именно пропуск, а не соседние
    имеющиеся свечи: иначе при чтении журнала непонятно, что искать.
    """

    start: datetime
    end: datetime
    minutes: int
    crosses_date: bool

    def __str__(self) -> str:
        where = "смена даты" if self.crosses_date else "внутри одного дня"
        return (
            f"пропуск {self.start:%Y-%m-%d %H:%M} … {self.end:%Y-%m-%d %H:%M} "
            f"({self.minutes} мин, {where})"
        )


def _moments(times: Iterable[datetime]) -> list[datetime]:
    return sorted({ensure_msk(t).replace(second=0, microsecond=0) for t in times})


def count_missing_minutes(times: Iterable[datetime]) -> int:
    """Сколько минуток отсутствует между первой и последней имеющейся."""
    moments = _moments(times)
    if len(moments) < 2:
        return 0
    span = int((moments[-1] - moments[0]).total_seconds() // 60) + 1
    return span - len(moments)


def find_gaps(
    times: Iterable[datetime],
    *,
    min_minutes: int = 1,
    crossing_date: bool | None = None,
) -> list[Gap]:
    """Отрезки отсутствующих минуток длиной от `min_minutes`.

    `min_minutes=1` (умолчание) находит все пропуски, включая одиночные.
    Отчёт о загрузке использует умолчание для подсчёта и порог побольше —
    для записей в журнал.

    :param crossing_date:
        отбор по смене календарной даты. `None` — отдать все;
        `False` — только разрывы **внутри одного дня**; `True` — только
        перешагнувшие сутки.

        Отбор нужен не для красоты. Ночной перерыв и выходные дают разрыв
        каждый календарный день: год минуток — это под три сотни разрывов
        «через сутки», и в общем счёте они топят те несколько, ради которых
        счёт и ведётся. Строка «разрывов от порога: 350» не сообщает ничего.
    """
    if min_minutes < 1:
        raise ValueError("порог разрыва меньше одной минуты не имеет смысла")
    moments: Sequence[datetime] = _moments(times)
    gaps: list[Gap] = []
    for previous, following in zip(moments, moments[1:]):
        missing = int((following - previous).total_seconds() // 60) - 1
        if missing < min_minutes:
            continue
        start = previous + _MINUTE
        end = following - _MINUTE
        crosses = start.date() != end.date() or previous.date() != following.date()
        if crossing_date is not None and crosses is not crossing_date:
            continue
        gaps.append(
            Gap(start=start, end=end, minutes=missing, crosses_date=crosses)
        )
    return gaps
