"""Сборка мусора только в потоке окна (`B-057`, `app/collector.py`)."""

from __future__ import annotations

import gc
import threading
import weakref

from PySide6.QtCore import QObject

from app.collector import collect_due, collect_in_main_thread


class _Cycle:
    """Мусор, который уходит только циклической сборкой."""

    def __init__(self) -> None:
        self.me = self


def test_a_foreign_thread_does_not_collect_qt_garbage(qapp) -> None:
    """Цикл с объектом Qt, брошенный в окне, не собирается в чужом потоке.

    Чужой поток здесь выделяет памяти далеко за порог сборщика: при
    включённой автоматической сборке цикл был бы собран там, и `~QObject`
    пошёл бы не в своём потоке — ровно то, что роняло прогон SIGSEGV.
    """
    gc.enable()  # прогон тестов её выключил сам — проверяется вызов
    timer = collect_in_main_thread(qapp, look_ms=60_000)
    try:
        assert not gc.isenabled(), "автоматическая сборка осталась включённой"
        cycle = _Cycle()
        cycle.qt = QObject()  # type: ignore[attr-defined]
        gone = weakref.ref(cycle)
        del cycle

        def churn() -> None:
            junk = [_Cycle() for _ in range(gc.get_threshold()[0] * 20)]
            del junk

        worker = threading.Thread(target=churn)
        worker.start()
        worker.join()
        assert gone() is not None, "цикл с объектом Qt собран в чужом потоке"

        collect_due()
        assert gone() is None, "сборка по порогам в потоке окна цикл не собрала"
    finally:
        gc.disable()
        timer.stop()
        timer.deleteLater()
