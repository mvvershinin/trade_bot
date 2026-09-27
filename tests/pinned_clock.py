"""Подставные часы для одного модуля: «сейчас» называет проверка, а не машина.

Зачем (`D-112`, `B-049`): проверка, чьи данные лежат вокруг названного дня,
а рабочий код считает отрезок от системных часов, зеленеет в день написания
и краснеет сама, когда настоящее сегодня уезжает от подставных данных.

Как: имя `datetime` **в одном модуле** подменяется объектом, у которого
`now()` отдаёт названный момент. Кроме `now` и `min` у него нет ничего —
намеренно: новое обращение модуля к `datetime.что-то` упадёт громко
`AttributeError`, а не пройдёт мимо подмены к часам машины.

⚠️ Не подкласс `datetime`, и `now()` отдаёт **обычный** `datetime`.
Замер `quality/calendar-rot-2026-09-14.md` §2: подкласс, подставленный
вместо класса, ломает сравнения типов и `dataclass` — 485 падений
на нулевом сдвиге. Здесь в коде не появляется ни одного экземпляра чужого
типа.
"""

from __future__ import annotations

from datetime import datetime, tzinfo


class PinnedClock:
    """Замена имени `datetime` в модуле: `now()` отдаёт `moment`."""

    #: Нужна `app.backfill.covered_days` (`datetime.min.time()`).
    min = datetime.min

    def __init__(self, moment: datetime) -> None:
        if moment.tzinfo is None:
            raise ValueError("подставной момент обязан быть с поясом")
        self.moment = moment

    def now(self, tz: tzinfo | None = None) -> datetime:
        """Тот же контракт, что у `datetime.now`: без пояса — местное наивное."""
        if tz is None:
            return self.moment.astimezone().replace(tzinfo=None)
        return self.moment.astimezone(tz)

