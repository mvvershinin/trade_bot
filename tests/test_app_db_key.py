"""Ключ `--db` переносит всю папку данных запуска, а не только базу.

Четыре поведения, каждое стережёт свой ход дела:

* **D-041** — технический лог уезжает в `database.parent`, если папку данных
  назвали ключом `--db`, а не в `userdata/` по умолчанию. Два вызова
  `setup_logging()` в `app/main.py` — до разбора окна и внутри сборки окна, —
  и оба обязаны получить один и тот же адрес, иначе у одного запуска
  получаются две разные папки данных одновременно (решение 0003).
* **D-041, без ключа** — обратная сторона того же фикса. `named_userdata`
  подставляется явно, только когда `--db` действительно назвали; без ключа
  он обязан остаться `None`, чтобы `setup_logging`/`_templates_read_runs_from`
  сами спросили `userdata_dir()` — то место, которое вправе подменить прогон
  тестов (`tests/conftest.py::logs_go_to_a_temporary_folder`). Безусловная
  подстановка `database.parent` обошла бы эту подмену стороной и писала бы
  лог в боевую папку `userdata/` при каждом прямом вызове `main()` без `--db`.
* **D-129** — библиотека шаблонов (`ui/backend.py::Backend.userdata`) следует
  той же логике, тем же условием: `app/main.py::_templates_read_runs_from`
  перевешивает её на `database.parent` вместе с чтением прогонов (D-052),
  и тоже только когда `--db` назвали.
* **D-072** — снимок окна (`--shot`) не гонит вложенный цикл событий Qt
  внутри уже исполняющегося шага корутины: `application.processEvents()`
  убран из `_snapshot`, потому что вложенный цикл — та же мина, что и
  `B-026` (`tests/test_ui_history_load.py`), только с другим триггером.
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib

from PySide6.QtCore import QEventLoop, QTimer

from app.logs import LogSetup
from app.main import (
    _named_userdata,
    _Shot,
    _snapshot,
    _templates_read_runs_from,
    _wire_settings_and_log,
    main,
)
from app.settings_store import Loaded
from market.journal import redact
from ui import backend
from ui.models import DecisionLevel, Settings

# ===========================================================================
# D-041/D-129 — вычисление общей развилки, от которого зависят оба
# ===========================================================================

def test_named_userdata_is_none_without_the_db_key() -> None:
    """`_named_userdata` — единственное место развилки, и обе стороны тут.

    `main()` и `_run()` зовут одну и ту же функцию вместо двух копий строки:
    разойдись они — регрессия из `test_without_the_db_key_...` вернулась бы
    в одном месте и осталась незамеченной в другом.

    Мутация: убрать условие (`if args.database else None`) — тест на пустом
    `database` покраснеет.
    """
    assert _named_userdata(argparse.Namespace(database=None)) is None
    named = pathlib.Path("/where/candles.sqlite3")
    assert _named_userdata(argparse.Namespace(database=str(named))) == named.parent

# ===========================================================================
# D-041 — технический лог следует за `--db`
# ===========================================================================

def test_the_log_follows_the_db_key_not_the_default_userdata(tmp_path: pathlib.Path) -> None:
    """Первый вызов `setup_logging()` в `main()` — до разбора окна.

    База ещё не существует, `--runs` честно откажет («Базы свечей нет»),
    но лог обязан появиться рядом с ней, а не в `userdata/` по умолчанию
    (которую фикстура `logs_go_to_a_temporary_folder` подменяет на общую
    для всей сессии папку — заметно другую, чем `tmp_path` этого теста).

    Мутация: убрать `userdata=database.parent` из первого вызова
    `setup_logging()` в `app.main.main` — лог уедет в общую папку сессии,
    и `tmp_path / "logs" / "terminal.log"` не появится.
    """
    database = tmp_path / "candles.sqlite3"
    main(["--db", str(database), "--runs", "1"])
    log_file = tmp_path / "logs" / "terminal.log"
    assert log_file.exists(), (
        f"лог не последовал за --db: в {tmp_path} лежит {list(tmp_path.iterdir())}"
    )


def test_without_the_db_key_the_log_still_honours_the_userdata_patch(
    monkeypatch, tmp_path: pathlib.Path
) -> None:
    """Без `--db` лог идёт через `userdata_dir()`, а не мимо него.

    Оборотная сторона предыдущего теста и находка ревью 28.09.2026:
    `main()` сам считает `database` через `market.default_db_path()`
    (используя непропатченный `market.paths.userdata_dir`), а фикстура
    `logs_go_to_a_temporary_folder` подменяет только `app.logs.userdata_dir` —
    другую, локальную привязку имени. Подать `database.parent` в `userdata`
    безусловно значило бы каждый раз обходить эту подмену стороной; здесь два
    пути разведены нарочно, чтобы поймать именно расхождение.

    Мутация: передавать `userdata=named_userdata` не только когда `--db`
    назван, а всегда, вычисляя `named_userdata` из `default_db_path()` —
    лог уедет в `other_default / "logs"`, а не в `patched / "logs"`.
    """
    other_default = tmp_path / "other-default"
    other_default.mkdir()
    patched = tmp_path / "patched"

    monkeypatch.setattr("market.default_db_path", lambda: other_default / "candles.sqlite3")
    monkeypatch.setattr("app.logs.userdata_dir", lambda: patched)

    main(["--runs", "1"])

    assert (patched / "logs" / "terminal.log").exists(), (
        "лог не последовал за подменённым userdata_dir(): "
        f"{list(patched.iterdir()) if patched.exists() else 'папки нет'}"
    )
    assert not (other_default / "logs").exists(), (
        "лог ушёл в database.parent мимо тестовой подмены userdata_dir()"
    )


def test_the_window_wiring_forwards_the_named_userdata_to_the_log(
    monkeypatch, tmp_path: pathlib.Path
) -> None:
    """Второй вызов, внутри сборки окна (`_wire_settings_and_log`), тот же адрес.

    Первую правку легко сделать только в `main()` и забыть про
    `_wire_settings_and_log` — окно вызывается позже и само переносит лог
    по настройке `values.log_directory`, умолчание у которой пусто, и без
    явного `userdata` он опять уехал бы в папку по умолчанию.

    Подставные порт и хранилище настроек: тест проверяет **проводку**,
    не поведение `HistoryPort` или `SettingsStore` — им место в своих файлах.

    Мутация: убрать `userdata=named_userdata` из вызова `setup_logging()`
    внутри `_wire_settings_and_log` — `captured["userdata"]` останется `None`,
    хотя передан не пустой `named_userdata`.
    """
    captured: dict[str, object] = {}

    def fake_setup_logging(directory=None, *, userdata=None, **_ignored):
        captured["directory"] = directory
        captured["userdata"] = userdata
        return LogSetup(
            path=None, directory=None, asked=None,
            permissions_enforced=False, trouble="",
        )

    monkeypatch.setattr("app.logs.setup_logging", fake_setup_logging)

    class _FakeSignal:
        def connect(self, _slot: object) -> None:
            return None

    class _FakePort:
        def __init__(self) -> None:
            self.notes: list[tuple[str, str]] = []
            self.settings_applied = _FakeSignal()

        def note(self, title: str, text: str, level: object = None) -> None:
            self.notes.append((title, text))

    class _FakeStore:
        path = tmp_path / "settings.json"

    named_userdata = tmp_path / "named-userdata"
    _wire_settings_and_log(
        _FakePort(),  # type: ignore[arg-type]
        _FakeStore(),  # type: ignore[arg-type]
        Loaded(values=Settings()),
        Settings(),
        named_userdata,
        level=DecisionLevel.WARNING,
    )
    assert captured["userdata"] == named_userdata


# ===========================================================================
# D-129 — библиотека шаблонов следует за `--db`
# ===========================================================================

def test_the_templates_library_follows_the_db_key_too(tmp_path: pathlib.Path) -> None:
    """`_templates_read_runs_from` перевешивает `userdata`, а не только `runs`.

    Мутация: убрать `changes["userdata"] = ...` из `_templates_read_runs_from`
    — `door.userdata()` останется той же заглушкой, что стояла до вызова.
    """
    original = backend.current()
    try:
        backend.use(backend.Backend(
            changes=lambda _before, _after: None,  # type: ignore[arg-type,return-value]
            runs=lambda _sets: None,  # type: ignore[arg-type,return-value]
            snapshot=lambda _values: "",
            userdata=lambda: pathlib.Path("/should-not-be-used"),
        ))
        database = tmp_path / "candles.sqlite3"
        _templates_read_runs_from(database, database.parent)
        door = backend.current()
        assert door is not None
        assert door.userdata() == database.parent
    finally:
        backend.use(original)


def test_without_the_db_key_the_templates_library_keeps_its_own_userdata(
    tmp_path: pathlib.Path,
) -> None:
    """Без `--db` `_templates_read_runs_from` не трогает `userdata` двери вовсе.

    Оборотная сторона предыдущего теста: `_library` (`app/runs.py`) сама
    зовёт `userdata_dir()` при каждом обращении — то место, которое вправе
    подменить прогон тестов. Подставить `database.parent` безусловно значило
    бы обходить эту подмену стороной при каждом запуске окна без `--db`.

    Мутация: подставлять `userdata=lambda: database.parent` всегда, не только
    когда `named_userdata` не пуст, — `door.userdata` перестанет быть
    исходным объектом `original_userdata`.
    """
    original = backend.current()
    try:
        original_userdata = object()
        backend.use(backend.Backend(
            changes=lambda _before, _after: None,  # type: ignore[arg-type,return-value]
            runs=lambda _sets: None,  # type: ignore[arg-type,return-value]
            snapshot=lambda _values: "",
            userdata=original_userdata,  # type: ignore[arg-type]
        ))
        database = tmp_path / "candles.sqlite3"
        _templates_read_runs_from(database, None)
        door = backend.current()
        assert door is not None
        assert door.userdata is original_userdata
    finally:
        backend.use(original)


# ===========================================================================
# D-072 — снимок не гонит вложенный цикл событий Qt изнутри корутины
# ===========================================================================

class _StubHistoryPort:
    """Только то, что `_snapshot` спрашивает у порта: дождаться простоя."""

    async def wait(self) -> None:
        return None


class _NestedLoopTrigger:
    """Подставной обработчик: поднимает **вложенный** цикл событий Qt.

    Ровно то же самое делает модальное окно (`B-026`,
    `tests/test_ui_history_load.py::_NestedLoopDialog`) — под `qasync`
    цикл Qt и есть цикл asyncio, и во вложенном цикле шагают соседние задачи.
    """

    def __init__(self) -> None:
        self.opened = 0

    def fire(self) -> None:
        self.opened += 1
        inner = QEventLoop()
        QTimer.singleShot(30, inner.quit)
        inner.exec()


def test_a_neighbour_task_survives_the_snapshots_qt_queue(
    loop, qapp, monkeypatch, tmp_path: pathlib.Path
) -> None:
    """`_snapshot` не рвёт соседнюю задачу вложенным циклом Qt.

    Три звена, то же семейство, что и `B-026`: во время снимка что-то в очереди
    Qt поднимает вложенный `QEventLoop` (здесь — подставной триггер вместо
    настоящего модального окна, чтобы не зависеть от того, что там нажато);
    вложенный цикл даёт слово соседней задаче; если `_snapshot` в этот момент
    сама была текущей задачей шага (`application.processEvents()` изнутри
    корутины), asyncio роняет соседа `RuntimeError: Cannot enter into task…`.

    Мутация: вернуть `application.processEvents()` в цикл `_snapshot` —
    тест обязан покраснеть (соседняя задача не дойдёт до конца, цикл событий
    сообщит об отказе).
    """
    from ui.main_window import MainWindow
    from ui.ports import TerminalPort

    window = MainWindow(port=TerminalPort(), sanitize=redact)
    window._timer.stop()  # noqa: SLF001 — та же связанность, что в test_ui_history_load.py

    trigger = _NestedLoopTrigger()
    trouble: list[dict] = []
    loop.set_exception_handler(lambda _loop, context: trouble.append(context))

    async def busy() -> str:
        for _ in range(30):
            await asyncio.sleep(0.005)
        return "дошла до конца"

    async def main_coroutine() -> tuple[asyncio.Task, int]:
        neighbour = asyncio.ensure_future(busy())
        await asyncio.sleep(0.01)  # дать соседке начать
        QTimer.singleShot(0, trigger.fire)  # сработает во время снимка
        shot = _Shot(tmp_path / "snap.png", "window", "")
        outcome = await _snapshot(qapp, window, _StubHistoryPort(), shot)  # type: ignore[arg-type]
        await asyncio.sleep(0.3)  # дать соседке дойти до конца
        return neighbour, outcome

    try:
        neighbour, outcome = loop.run_until_complete(main_coroutine())
        assert trigger.opened == 1, (
            "вложенный цикл так и не сработал — тест проверяет не то, что написано"
        )
        assert outcome == 0
        assert not trouble, (
            "цикл событий сообщил об отказе во время снимка: "
            + "; ".join(str(item.get("message")) for item in trouble)
        )
        assert neighbour.done(), (
            "соседняя задача не дошла до конца: её шаг сорвался внутри "
            "вложенного цикла Qt, поднятого во время снимка"
        )
        assert neighbour.result() == "дошла до конца"
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()
