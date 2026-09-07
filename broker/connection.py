"""Состояние связи и нарастающая пауза между попытками.

Заказчик: «обрыв связи — очень важно!!! интернет прерывается часто»
(ТЗ §4.9, `DOMAIN.md` §7). Здесь — та часть, которая принадлежит слою
`broker/`: отметить, когда связь пропала и когда вернулась, посчитать
длительность обрыва и выдать текст **человеческим языком** для панели
состояния и журнала решений.

Чего здесь нет и не будет: сверки фактической позиции с расчётной. Шаги 3–5
из `DOMAIN.md` §7 — работа движка, и относятся они к этапу 2.

**Чего программа сделать не может.** Пока компьютера нет в сети, робот
не торгует. Никакой настройкой это не лечится. Переподключение сокращает
простой, но не отменяет его — и говорить об этом надо прямо.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Final

from broker.errors import BrokerError

#: Первая пауза перед повтором.
BASE_DELAY: Final[float] = 1.0
#: Во сколько раз растёт пауза с каждой неудачей.
GROWTH: Final[float] = 2.0
#: Потолок паузы. Дальше расти незачем: минута без связи — это уже
#: двенадцать пропущенных пятиминутных свечей за час, а не «почти сразу».
MAX_DELAY: Final[float] = 60.0
#: Доля случайного разброса. Без него все переподключения после общего сбоя
#: у брокера приходят одной волной ровно в одну секунду.
JITTER: Final[float] = 0.25


class Backoff:
    """Нарастающая пауза между попытками переподключения.

    Источник случайности внедряется, иначе поведение нельзя проверить тестом.
    """

    def __init__(
        self,
        *,
        base: float = BASE_DELAY,
        growth: float = GROWTH,
        maximum: float = MAX_DELAY,
        jitter: float = JITTER,
        random_source: Callable[[], float] = random.random,
    ) -> None:
        self._base = base
        self._growth = growth
        self._maximum = maximum
        self._jitter = jitter
        self._random = random_source

    def delay(self, attempt: int) -> float:
        """Пауза перед попыткой номер `attempt` (первая неудача — attempt = 1)."""
        if attempt < 1:
            raise ValueError("номер попытки начинается с единицы")
        raw = self._base * (self._growth ** (attempt - 1))
        raw = min(raw, self._maximum)
        spread = raw * self._jitter
        return max(0.0, raw - spread + 2 * spread * self._random())


def _ru_duration(span: timedelta) -> str:
    """«2 минуты 35 секунд» — так, как это скажет человек."""
    total = int(max(0.0, span.total_seconds()))
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)

    def plural(count: int, one: str, few: str, many: str) -> str:
        tail = count % 100
        if 11 <= tail <= 14:
            return f"{count} {many}"
        match count % 10:
            case 1:
                return f"{count} {one}"
            case 2 | 3 | 4:
                return f"{count} {few}"
            case _:
                return f"{count} {many}"

    parts: list[str] = []
    if hours:
        parts.append(plural(hours, "час", "часа", "часов"))
    if minutes:
        parts.append(plural(minutes, "минута", "минуты", "минут"))
    if seconds or not parts:
        parts.append(plural(seconds, "секунда", "секунды", "секунд"))
    return " ".join(parts)


@dataclass(slots=True)
class Outage:
    """Один обрыв: когда начался, когда кончился, сколько длился."""

    started_at: datetime
    reason: str
    ended_at: datetime | None = None

    def duration(self, now: datetime) -> timedelta:
        return (self.ended_at or now) - self.started_at


@dataclass(slots=True)
class ConnectionState:
    """Есть ли связь, сколько её нет и что об этом написать в журнал.

    Хранится история обрывов: ТЗ §4.9 требует, чтобы «всё время обрыва»
    попадало в журнал — когда пропала, когда восстановилась, сколько длилась.
    """

    online: bool = True
    current: Outage | None = None
    history: list[Outage] = field(default_factory=list)
    #: Сколько попыток переподключения сделано в текущем обрыве.
    attempts: int = 0

    def went_offline(self, now: datetime, error: BrokerError) -> str | None:
        """Отметить пропажу связи. Возвращает строку журнала или `None`.

        `None` — если связи не было и до этого: повторная неудача не заводит
        новый обрыв и не пишет вторую строку в журнал. Иначе журнал решений
        за час без интернета состоял бы из одной этой записи.
        """
        self.attempts += 1
        if not self.online:
            return None
        self.online = False
        self.current = Outage(started_at=now, reason=error.human)
        return (
            f"Связь с брокером пропала в {now.astimezone():%H:%M:%S}. "
            f"{error.human} Робот не принимает решений, пока связи нет: "
            "сигналы по устаревшим данным не подаются."
        )

    def came_online(self, now: datetime) -> str | None:
        """Отметить восстановление. Возвращает строку журнала или `None`."""
        self.attempts = 0
        if self.online:
            return None
        self.online = True
        outage = self.current
        self.current = None
        if outage is None:
            return f"Связь с брокером есть, {now.astimezone():%H:%M:%S}."
        outage.ended_at = now
        self.history.append(outage)
        return (
            f"Связь с брокером восстановлена в {now.astimezone():%H:%M:%S}. "
            f"Без связи: {_ru_duration(outage.duration(now))}. "
            "Пропущенные свечи будут догружены, фактическая позиция "
            "запрошена у брокера."
        )

    def offline_for(self, now: datetime) -> timedelta | None:
        """Сколько времени нет связи. `None`, если связь есть."""
        if self.online or self.current is None:
            return None
        return self.current.duration(now)

    def describe(self, now: datetime) -> str:
        """Строка для панели состояния."""
        if self.online:
            return "Связь с брокером есть"
        span = self.offline_for(now) or timedelta(0)
        return f"Нет связи с брокером: {_ru_duration(span)}"
