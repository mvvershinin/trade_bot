"""Сборка мусора только в потоке окна (`B-057`).

Циклический сборщик питона срабатывает в том потоке, где выделение памяти
перевалило порог, — то есть и в потоке базы, и в потоке сети
(`market.worker`). Объект Qt с таймером, собранный там, таймер не снимает
(«Timers cannot be stopped from another thread»), и следующий
`QTimerInfoList::activateTimers` роняет процесс SIGSEGV. Замер 01.10.2026:
с отдельным потоком сети полный прогон тестов падал так 2 раза из 16,
до него — 0 из 12; сборка мусора в потоке окна перед тестом сняла падения.

Поэтому автоматическая сборка выключена, а пороги смотрит таймер окна
и собирает то же поколение, что собрал бы сам питон, — но здесь.
"""

from __future__ import annotations

import gc
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PySide6.QtCore import QObject, QTimer

#: Как часто смотреть пороги. Не торговое число: на решения робота
#: не влияет, только на то, как долго мусор ждёт уборки.
LOOK_MS = 1000


def collect_due() -> int:
    """Собрать поколение, чей порог перейдён. Возвращает собранное число."""
    counts = gc.get_count()
    thresholds = gc.get_threshold()
    if counts[0] <= thresholds[0]:
        return 0
    generation = 0
    if counts[1] > thresholds[1]:
        generation = 1
        if counts[2] > thresholds[2]:
            generation = 2
    return gc.collect(generation)


def collect_in_main_thread(parent: QObject, *, look_ms: int = LOOK_MS) -> QTimer:
    """Выключить автоматическую сборку и собирать по таймеру потока окна.

    Звать **в потоке окна**: таймер принадлежит потоку, где создан.
    Qt ввозится здесь, а не в шапке: `collect_due` нужен и прогону
    тестов, где Qt может не быть вовсе.
    """
    from PySide6.QtCore import QTimer  # noqa: PLC0415 — см. докстринг

    gc.disable()
    timer = QTimer(parent)
    timer.setInterval(look_ms)
    timer.timeout.connect(collect_due)
    timer.start()
    return timer
