"""Свеча, таймфрейм и часовой пояс торгов.

Типы объявлены внутри слоя намеренно: `market/` не импортирует ни один слой
проекта (ARCHITECTURE.md §2).

⚠️ **Эта свеча в модуль стратегии НЕ проходит, и не должна.** Здесь стояло, что
соответствие типам `strategies/` структурное, по именам полей
`time / open / high / low / close / volume`. С 30.08.2026 это неверно.

Поле `time` — **начало** свечи. Свеча `strategies/` несёт `closes_at`, то есть
**время закрытия**, и поля `time` у неё нет: объект с таким полем модуль стратегии
отвергает явной проверкой. Величины различаются на таймфрейм, а имена не различались
никак — и утиная типизация подменяла одно другим молча. Ровно это уже сдвинуло
на один бар весь график с метками (разбор — в докстринге `ui/models.py`).

Перекладку делает `engine/` явной строкой. См. ARCHITECTURE.md §2.

Часовой пояс
------------
Всё торговое время — МСК. Здесь она задана **фиксированным смещением UTC+3**,
а не через `zoneinfo`, и это осознанный выбор:

* Москва не переводит часы с 26.10.2014, смещение неизменно уже двенадцать лет;
  данных до этой даты продукт не обрабатывает;
* `zoneinfo` на Windows требует пакета `tzdata`. В сборке Nuitka его отсутствие
  даёт `ZoneInfoNotFoundError` не при сборке, а у владельца счёта при первом
  запуске — и выглядит это как «программа не открывает базу свечей».

Смешение с UTC даёт сдвиг торгового окна на часы, поэтому все `datetime`
в слое — **tz-aware**. Наивный `datetime` в публичные функции не принимается.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

__all__ = [
    "MSK",
    "Candle",
    "Timeframe",
    "MINUTE",
    "M5",
    "ensure_msk",
    "floor_to_minute",
]

#: Московское время: фиксированный UTC+3 (см. пояснение в шапке модуля).
MSK = timezone(timedelta(hours=3), "MSK")


def ensure_msk(moment: datetime) -> datetime:
    """Привести момент к МСК. Наивное время — отказ, а не догадка.

    Догадка «наверное, это уже МСК» — самый дешёвый способ сдвинуть торговое
    окно на часы и не заметить: сделки останутся, просто станут другими.
    """
    if moment.tzinfo is None:
        raise ValueError(
            f"время без часового пояса: {moment!r}. Слой market/ работает только "
            "с tz-aware временем, иначе торговое окно молча сдвигается"
        )
    return moment.astimezone(MSK)


def floor_to_minute(moment: datetime) -> datetime:
    """Начало минуты, которой принадлежит момент. МСК, секунды отброшены.

    Ключ минутной свечи — **минута**, а не момент, которым её пометил источник.
    Биржа помечает минутку ровно `10:05:00`, поток брокера может прислать ту же
    минутку как `10:05:07`. Без приведения это два разных ключа: в базе две
    строки на одну минуту, а в собранном баре её объём сложится вдвое —
    правдоподобное число, которое потом не по чему найти.

    Отбрасывание, а не округление: свеча минуты 10:05 не может стать свечой
    минуты 10:06 от того, что источник отметил её на 47-й секунде.
    """
    return ensure_msk(moment).replace(second=0, microsecond=0)


@dataclass(frozen=True, slots=True, order=True)
class Timeframe:
    """Размер свечи в минутах и его имя.

    Допустимы только такие размеры, при которых границы свечей ложатся на сетку
    без остатка: делители часа (1…30 минут) и делители суток, кратные часу
    (60, 120, 180, 240, 360, 720, 1440). Размер вроде 7 минут отвергается:
    у него нет однозначной границы бара, и «начало» свечи начинает зависеть
    от того, с какой даты считать.
    """

    minutes: int
    name: str = ""

    def __post_init__(self) -> None:
        if self.minutes <= 0:
            raise ValueError(f"размер свечи должен быть положительным: {self.minutes}")
        if self.minutes <= 60:
            if 60 % self.minutes:
                raise ValueError(
                    f"{self.minutes} минут не делит час нацело — граница бара неоднозначна"
                )
        else:
            if self.minutes % 60 or 1440 % self.minutes:
                raise ValueError(
                    f"{self.minutes} минут не делит сутки нацело и не кратно часу — "
                    "граница бара неоднозначна"
                )
        if not self.name:
            object.__setattr__(self, "name", _default_name(self.minutes))

    @property
    def delta(self) -> timedelta:
        return timedelta(minutes=self.minutes)

    def __str__(self) -> str:  # pragma: no cover - тривиально
        return self.name


def _default_name(minutes: int) -> str:
    if minutes % 60 == 0 and minutes >= 60:
        return f"Hour{minutes // 60}"
    return f"Min{minutes}"


#: Имена как в архивном загрузчике (`reference/stand/moexdata.py`, `TF_MINUTES`) —
#: чтобы наборы данных и замеры прошлых прогонов читались без перевода.
MINUTE = Timeframe(1)
M5 = Timeframe(5)


@dataclass(frozen=True, slots=True)
class Candle:
    """Одна свеча.

    `time` — **начало** свечи. Время закрытия = `time + timeframe`.
    Движок считает торговое окно по времени **закрытия** (ARCHITECTURE.md §6),
    и оно берётся отсюда: `close_time`.

    Два разных признака неполноты, их нельзя путать:

    `filled_minutes` < `timeframe.minutes`
        Внутри интервала не нашлось части минуток. Для неликвидного времени
        это норма: биржа не отдаёт минуту, в которую не было сделок.
        Бар всё равно собран — из того, что есть, — но факт зафиксирован.

    `unsettled`
        Интервал бара ещё не закончился (или данные за него докачаны не до конца).
        Такой бар в решении участвовать не должен: он ещё изменится.
    """

    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    timeframe: Timeframe = MINUTE
    filled_minutes: int = 1
    unsettled: bool = False

    @property
    def close_time(self) -> datetime:
        """Время закрытия свечи: начало + таймфрейм."""
        return self.time + self.timeframe.delta

    @property
    def is_partial(self) -> bool:
        """В интервале не хватает минуток."""
        return self.filled_minutes < self.timeframe.minutes

    def replace(self, **changes: object) -> "Candle":
        return replace(self, **changes)  # type: ignore[arg-type]
