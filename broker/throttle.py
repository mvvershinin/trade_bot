"""Ограничитель частоты запросов к брокеру.

`DOMAIN.md` §6 и приложение А ТЗ называют предел «10 запросов в секунду
на большинство сервисов». **Найдено в документации брокера и подтверждено
04.09.2026** — раздел «Ограничения», выписка
`.docs/broker-api/29-restrictions.md`: 10 RPS на портфель, лимиты,
справочник, рыночные данные, заявки и статус заявки; **3 RPS** на неторговые
операции; 200 000 заявок в сутки. Превышение — ответ **429** с кодом
`RESOURCE_EXHAUSTED`, и документация прямо советует повтор с нарастающей
паузой. (До 05.09.2026 здесь стояло, что этого в документации нет.)

Ограничитель настраиваемый, а его значение по умолчанию — заведомо
осторожное: раскладка предела между потоком и разовыми запросами
в документации не описана.

Для нашей задачи (один инструмент, свечи от минуты) запас многократный.
Ограничитель нужен не ради предела, а ради строки из роли: «опрос портфеля
в цикле без паузы — прямой путь в лимит». Цикл без паузы пишется случайно,
а обнаруживается отказом брокера в торговое время.
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, Final

#: Столько запросов в секунду программа себе позволяет. Половина заявленного
#: предела: поток котировок и разовые запросы делят одну квоту, а точная
#: раскладка предела по сервисам неизвестна.
DEFAULT_RATE: Final[float] = 5.0


class Throttle:
    """Ведро с жетонами: не больше `rate` обращений в секунду в среднем.

    Часы и сон внедряются — иначе проверка ограничителя превращается
    в тест, который ждёт настоящую секунду и падает на нагруженной машине.
    """

    def __init__(
        self,
        rate: float = DEFAULT_RATE,
        *,
        burst: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if rate <= 0:
            raise ValueError("частота должна быть больше нуля")
        self._rate = rate
        self._capacity = burst if burst is not None else max(1.0, rate)
        self._tokens = self._capacity
        self._clock = clock
        self._sleep = sleep
        self._updated = clock()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Дождаться права сделать один запрос."""
        async with self._lock:
            while True:
                now = self._clock()
                self._tokens = min(
                    self._capacity, self._tokens + (now - self._updated) * self._rate
                )
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await self._sleep((1.0 - self._tokens) / self._rate)
