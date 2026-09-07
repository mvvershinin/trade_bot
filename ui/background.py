"""Ничего не подвешивает окно.

Программа одновременно держит поток котировок, рисует график и отвечает на мышь.
Замерший интерфейс во время обрыва связи неотличим от зависшей программы —
владелец счёта в этот момент не знает, торгует робот или нет, и первое, что он
сделает, — снимет процесс. Вместе с открытой позицией.

Правила (ARCHITECTURE.md §5):

* **Сеть — не здесь и не в слоте.** Запросы к брокеру и к бирже живут в `broker/`
  и `market/` и выполняются асинхронно. В `ui/` сетевых вызовов нет вообще;
  это проверяется тестом `tests/test_ui_no_trading_logic.py`.
* **Тяжёлый перебор — не здесь.** Оптимизатор перебирает сотни комбинаций;
  ему нужны процессы (`backtest/`), а не поток. Поток спасает от подвисания
  окна, но не от одного занятого ядра.
* **Здесь — короткие местные задачи**, которые всё же длиннее 100 мс: чтение
  журнала с диска, запись выгрузки, разбор большого CSV.

Связь потока с окном — только через сигналы. Прямой вызов метода виджета
из рабочего потока даёт падение, которое воспроизводится раз в неделю
и не ловится ничем.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QApplication


class _Signals(QObject):
    done = Signal(object)
    failed = Signal(str)


class Worker(QRunnable):
    """Одна задача для пула потоков.

    Исключение не роняет программу и не теряется: оно приходит в `failed`
    человеческой строкой. Проглоченная ошибка в фоне выглядит как «кнопка
    не работает» и разбирается часами.
    """

    def __init__(self, work: Callable[[], Any]) -> None:
        super().__init__()
        self._work = work
        self.signals = _Signals()

    def run(self) -> None:  # вызывается в рабочем потоке
        try:
            result = self._work()
        except Exception as error:  # noqa: BLE001 — сообщение уходит пользователю
            self.signals.failed.emit(str(error))
            return
        self.signals.done.emit(result)


def run_in_worker(
    work: Callable[[], Any],
    on_done: Callable[[Any], None] | None = None,
    on_error: Callable[[str], None] | None = None,
    pool: QThreadPool | None = None,
) -> Worker:
    """Выполнить задачу вне потока интерфейса, ответ вернуть сигналом.

    Возвращает `Worker` — его нужно держать, пока задача не закончилась:
    сборщик мусора Python не знает про очередь Qt и уносит объект вместе
    с сигналами.
    """
    worker = Worker(work)
    if on_done is not None:
        worker.signals.done.connect(on_done)
    if on_error is not None:
        worker.signals.failed.connect(on_error)
    (pool or QThreadPool.globalInstance()).start(worker)
    return worker


@contextmanager
def busy_cursor():
    """Курсор ожидания на время короткой, но заметной операции (~100 мс и дольше)."""
    app = QApplication.instance()
    if app is None:
        yield
        return
    app.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))
    try:
        yield
    finally:
        app.restoreOverrideCursor()
