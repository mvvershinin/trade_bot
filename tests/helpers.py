"""Общие подставки и уборка для тестов интерфейса.

Здесь же живут два помощника по очереди Qt — `settle_qt` и
`finish_what_is_left`. Они лежат в одном месте, а не копиями по файлам,
и это следствие ревью 06.09.2026: `settle_qt` был дословно продублирован
в двух файлах, а нужен в семи. Копия, которую поправили в одном месте
из двух, — это защита, про которую все думают, что она стоит везде.
"""

from __future__ import annotations

from ui.models import HistoryLoadRequest, Mode, Settings
from ui.ports import TerminalPort


class RecordingPort(TerminalPort):
    """Порт, который запоминает команды вместо того, чтобы их выполнять."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, object]] = []

    def start(self) -> None:
        self.calls.append(("start", None))

    def stop(self) -> None:
        self.calls.append(("stop", None))

    def resume(self) -> None:
        self.calls.append(("resume", None))

    def set_mode(self, mode: Mode) -> None:
        self.calls.append(("set_mode", mode))

    def apply_settings(self, settings: Settings) -> None:
        self.calls.append(("apply_settings", settings))

    def close_position(self) -> None:
        self.calls.append(("close_position", None))

    def stream(self, on: bool) -> None:
        self.calls.append(("stream", on))

    def request_history_facts(self, symbol: str) -> None:
        self.calls.append(("request_history_facts", symbol))

    def load_history(self, request: HistoryLoadRequest) -> None:
        self.calls.append(("load_history", request))

    def cancel_history_load(self) -> None:
        self.calls.append(("cancel_history_load", None))

    def show_recent(self) -> None:
        self.calls.append(("show_recent", None))


def settle_qt(qapp) -> None:
    """Догасить очередь Qt, **включая отложенные удаления**.

    `processEvents()` сам по себе `deleteLater()` не исполняет: событие
    `DeferredDelete` доставляется при выходе из цикла событий либо по прямой
    просьбе, и просьба здесь как раз стоит. Разница проверена замером —
    `tests/test_ui_history_load.py::
    test_a_bare_process_events_leaves_a_deleted_window_alive`.

    ⚠️ Строка выведена из поломки, а не из опрятности. Показанный виджет
    после `deleteLater(); processEvents()` **жив и видим** — он уезжает
    в следующий тест и разрушается уже в его цикле событий. Для теста,
    который цикл событий крутит (а под `qasync` это цикл самого приложения),
    разрушение последнего показанного окна означает `lastWindowClosed` →
    `quit()` → выход из цикла на середине корутины. Падает при этом
    **не тот тест, который сломан**.

    ⚠️ Живёт здесь, а не в файле тестов, и это следствие ревью 06.09.2026:
    до него помощник был дословно скопирован в двух файлах, а нужен
    в семи. Копия, которую поправили в одном месте из двух, — это защита,
    про которую все думают, что она стоит везде.
    """
    from PySide6.QtCore import QCoreApplication, QEvent

    qapp.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()


def carry_out_deferred_deletions() -> None:
    """Исполнить отложенные удаления Qt, если Qt в этом процессе вообще есть.

    Половина `settle_qt`, и намеренно только половина. `settle_qt` крутит
    ещё и `processEvents()` — то есть исполняет **чужие** отложенные дела:
    таймеры, перерисовку, доставленные сигналы. Внутри теста это нужно
    и там оно стоит; после теста — это работа, которую никто не просил,
    и на 3450 тестах она обошлась бы дороже пользы.

    ⚠️ Проверка `sys.modules` — не микрооптимизация, а условие
    соразмерности. Помощник зовётся после **каждого** теста прогона,
    а Qt нужен трёмстам из трёх с половиной тысяч. Прямой импорт
    `PySide6` втащил бы Qt в воркер, где его нет и быть не должно:
    это и время старта, и лишняя платформа в процессе, который её
    не выбирал. Пока `PySide6.QtWidgets` не ввезён кем-то ещё,
    здесь ровно один поиск по словарю.

    Почему это вообще нужно: `deleteLater()` только **кладёт событие
    в очередь**. `processEvents()` его не разбирает — `DeferredDelete`
    доставляется при выходе из цикла событий либо по прямой просьбе,
    и просьба здесь как раз стоит. Без неё «убранный» виджет жив,
    получает все глобальные события Qt (смену палитры, смену стиля)
    и уезжает в следующий тест. Замер 06.09.2026, последовательный
    прогон целиком: **117 035 живых виджетов** в пике против нуля
    с этой строкой (`D-083`).
    """
    import sys

    widgets = sys.modules.get("PySide6.QtWidgets")
    if widgets is None:
        return
    if widgets.QApplication.instance() is None:
        return

    from PySide6.QtCore import QCoreApplication, QEvent

    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def live_widget_count() -> int:
    """Сколько виджетов Qt живо в этом процессе прямо сейчас.

    Ноль означает и «Qt в процессе нет», и «Qt есть, виджетов нет»:
    для сторожей `D-083` это одно и то же — копиться нечему.
    """
    import sys

    widgets = sys.modules.get("PySide6.QtWidgets")
    if widgets is None:
        return 0
    if widgets.QApplication.instance() is None:
        return 0
    return len(widgets.QApplication.allWidgets())


def finish_what_is_left(made) -> None:
    """Досрочно закончить задачи, оставшиеся от упавшего теста.

    Молча бросить их нельзя: сборщик мусора закроет корутину позже,
    в чужом тесте, и красным станет он. Отмена идёт **в этом же цикле**,
    поэтому `finally` внутри корутин отрабатывают по-настоящему —
    порты закрываются, поток данных останавливается.

    Ожидание ограничено по времени: задача, не желающая отменяться, —
    это отдельная беда, и вешать на ней весь прогон незачем.
    """
    import asyncio

    left = [task for task in asyncio.all_tasks(made) if not task.done()]
    if not left:
        return
    for task in left:
        task.cancel()
    made.run_until_complete(asyncio.wait(left, timeout=2))
