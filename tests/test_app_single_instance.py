"""Две копии «Терминала» на одной папке данных не работают одновременно.

[Решение 0039](../.docs/decisions/0039-one-copy-per-data-folder.md).

Что здесь стережётся, по одной строке на проверку, — в докстринге каждой.
Три вещи, ради которых файл написан:

1. **вторая копия не поднимается** — иначе две копии пишут в одну базу свечей
   и одни настройки, а в бою подают две заявки на один сигнал;
2. **замок после аварии не держит вечно** — программу убили, машина
   выключилась, а следующий запуск обязан пройти без ручной уборки;
3. **человек видит фразу, а не код** — «Errno 11» владельцу счёта
   не говорит ничего.

⚠️ Проверено только на Linux: машина разработки одна. Ветка Windows
(`msvcrt.locking`) исполняется здесь ноль раз — её проверяет `mypy
--platform win32` и прогон на чистой машине под Windows, которого не было.
"""

from __future__ import annotations

import errno
import os
import pathlib
import subprocess
import sys
import textwrap

import pytest

from app.main import _say_already_running, main
from app.single_instance import LOCK_FILE_NAME, OneCopy, Purpose
from market import CandleStore

REPO = pathlib.Path(__file__).resolve().parent.parent

#: Запуск отдельного процесса с Qt на медленной машине сборки не мгновенный.
TIMEOUT = 180


@pytest.fixture()
def folder(tmp_path: pathlib.Path) -> pathlib.Path:
    """Папка данных вроде `userdata/`. Настоящую трогать нельзя ни одним тестом."""
    place = tmp_path / "userdata"
    place.mkdir()
    return place


def _launch(source: str, *arguments: str) -> subprocess.Popen[str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPO)
    # Свой же интерпретатор и свой же текст программы — не пользовательский ввод.
    return subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(source), *arguments],
        cwd=REPO, env=environment, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def _break_the_syscall(monkeypatch: pytest.MonkeyPatch, error: OSError) -> None:
    """Заставить системный вызов замка отказать заданным кодом.

    Патчится тот модуль, который на этой ОС и работает: разница между
    Windows и POSIX здесь та же, что в `app/single_instance.py::_grab`.
    """
    def boom(*_: object) -> None:
        raise error

    if sys.platform == "win32":
        import msvcrt  # модуля нет на POSIX, наверху он уронил бы файл целиком

        monkeypatch.setattr(msvcrt, "locking", boom)
    else:
        import fcntl  # модуля нет на Windows, наверху он уронил бы файл целиком

        monkeypatch.setattr(fcntl, "flock", boom)


#: Код отказа «занято» у той системы, на которой идёт прогон.
BUSY_ERRNO = errno.EACCES if sys.platform == "win32" else errno.EWOULDBLOCK


# ------------------------------------------------------------------ сам замок

def test_the_second_copy_does_not_get_the_lock(folder: pathlib.Path) -> None:
    """Стережёт главное: занятую папку вторая копия не получает."""
    first = OneCopy(folder)
    assert first.take().taken, "первая копия замок не взяла — проверка вакуумна"
    second = OneCopy(folder)
    try:
        verdict = second.take()
        assert not verdict.taken, (
            "вторая копия получила ту же папку: две программы пишут в одну базу "
            "свечей и одни настройки"
        )
    finally:
        first.release()


def test_a_real_second_process_does_not_get_the_lock(folder: pathlib.Path) -> None:
    """Стережёт то же самое настоящим вторым процессом, а не вторым объектом."""
    holder = OneCopy(folder)
    assert holder.take().taken
    try:
        child = _launch(
            """
            import pathlib, sys
            from app.single_instance import OneCopy
            print(OneCopy(pathlib.Path(sys.argv[1])).take().taken, flush=True)
            """,
            str(folder),
        )
        out, _ = child.communicate(timeout=TIMEOUT)
        assert out.strip() == "False", (
            f"второй процесс взял занятую папку, ответ: {out!r}"
        )
    finally:
        holder.release()


def test_two_folders_do_not_collide(tmp_path: pathlib.Path) -> None:
    """Стережёт область замка: две разные папки данных — две независимые копии."""
    one, two = tmp_path / "a", tmp_path / "b"
    first, second = OneCopy(one), OneCopy(two)
    try:
        assert first.take().taken
        assert second.take().taken, (
            "замок взят шире папки данных: программа с --db на другую базу "
            "перестала запускаться рядом"
        )
    finally:
        first.release()
        second.release()


# ------------------------------------------------------- аварийное завершение

def test_the_lock_dies_with_the_process(folder: pathlib.Path) -> None:
    """Стережёт главную трудность: программу убили — замок не остался.

    Убийство именно `kill()`: ни `finally`, ни `atexit`, ни обработчика
    сигнала — то же, что выключение питания с точки зрения процесса.
    """
    child = _launch(
        """
        import pathlib, sys, time
        from app.single_instance import OneCopy
        held = OneCopy(pathlib.Path(sys.argv[1])).take()
        print(held.taken, flush=True)
        time.sleep(600)
        """,
        str(folder),
    )
    assert child.stdout is not None
    assert child.stdout.readline().strip() == "True", "дочерний процесс замок не взял"
    child.kill()
    child.wait(timeout=TIMEOUT)
    for pipe in (child.stdout, child.stderr):
        if pipe is not None:
            pipe.close()

    heir = OneCopy(folder)
    try:
        assert heir.take().taken, (
            "после убийства владельца папка осталась занятой навсегда: "
            "программа больше не запустится, пока человек не найдёт файл замка"
        )
    finally:
        heir.release()


def test_a_leftover_file_holds_nothing(folder: pathlib.Path) -> None:
    """Стережёт отличие от PID-файла: файл с живым номером процесса не запирает.

    Ровно этот случай ломает очевидную реализацию: после перезагрузки под
    записанным номером живёт чужая программа, проверка «жив?» отвечает «да»,
    и «Терминал» не запускается никогда.
    """
    (folder / LOCK_FILE_NAME).write_text(
        f"pid={os.getpid()}\nstarted=01.01.2026 10:00:00\n", encoding="utf-8"
    )
    copy = OneCopy(folder)
    try:
        assert copy.take().taken, (
            "оставшийся файл прочитан как «занято»: это PID-файл, а не замок"
        )
    finally:
        copy.release()


def test_the_file_stays_after_release(folder: pathlib.Path) -> None:
    """Стережёт отказ от удаления файла: оно создало бы гонку двух копий."""
    copy = OneCopy(folder)
    copy.take()
    copy.release()
    assert (folder / LOCK_FILE_NAME).exists(), (
        "файл замка удалён при выходе — между удалением и созданием другая "
        "копия успевает открыть тот же файл"
    )
    again = OneCopy(folder)
    try:
        assert again.take().taken, "после освобождения папка не берётся заново"
    finally:
        again.release()


def test_taking_twice_is_not_an_error(folder: pathlib.Path) -> None:
    """Стережёт от самоблокировки: повторный вызов у того же объекта не отказ."""
    copy = OneCopy(folder)
    try:
        assert copy.take().taken
        assert copy.take().taken, "копия отказала сама себе"
    finally:
        copy.release()


# ------------------------------------------------- разбор кодов отказа системы

def test_busy_is_a_refusal(folder: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Стережёт разбор кода «занято»: он обязан приводить к отказу."""
    _break_the_syscall(monkeypatch, OSError(BUSY_ERRNO, "занято"))
    assert not OneCopy(folder).take().taken


def test_a_folder_that_cannot_hold_a_lock_lets_the_program_start(
    folder: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Стережёт вторую половину разбора: «система так не умеет» — не «занято».

    Часть сетевых монтирований вызова не поддерживает. Спутать это с «занято»
    значило бы запретить работу на такой машине вовсе; смолчать — оставить
    человека без предохранителя и без слова об этом.
    """
    _break_the_syscall(monkeypatch, OSError(errno.ENOSYS, "нет такого вызова"))
    verdict = OneCopy(folder).take()
    assert verdict.taken, "программа не запустилась там, где замка просто нет"
    assert verdict.trouble, "замка нет, и об этом никто не сказал ни слова"
    assert "не позволяет поставить замок" in verdict.trouble


# ------------------------------------------------------- что видит человек

def test_the_refusal_is_a_phrase_not_a_code(folder: pathlib.Path) -> None:
    """Стережёт текст отказа: фраза владельцу счёта, а не код ошибки."""
    first = OneCopy(folder)
    first.take()
    try:
        trouble = OneCopy(folder).take(Purpose.WINDOW).trouble
    finally:
        first.release()
    assert "«Терминал» уже запущен" in trouble
    assert "панели задач" in trouble, "человеку не сказано, где искать открытое окно"
    assert "две заявки на один сигнал" in trouble, "не сказано, почему так сделано"
    for forbidden in ("Errno", "errno", "Traceback", "OSError", "EWOULDBLOCK", "EACCES"):
        assert forbidden not in trouble, f"в тексте человеку код «{forbidden}»"


def test_the_command_is_told_to_close_the_window(folder: pathlib.Path) -> None:
    """Стережёт второй совет: у консольной команды он другой, чем у окна."""
    first = OneCopy(folder)
    first.take()
    try:
        trouble = OneCopy(folder).take(Purpose.COMMAND).trouble
    finally:
        first.release()
    assert "Закройте окно" in trouble
    assert "панели задач" not in trouble, (
        "команде из консоли советуют переключиться на окно вместо того, "
        "чтобы его закрыть"
    )


def test_the_refusal_names_when_it_started(folder: pathlib.Path) -> None:
    """Стережёт пользу от записки: в отказе есть время запуска первой копии."""
    first = OneCopy(folder)
    first.take()
    try:
        trouble = OneCopy(folder).take().trouble
    finally:
        first.release()
    assert "Запущена:" in trouble, "не сказано, с какого времени программа открыта"
    assert str(folder) in trouble, "не названа папка данных, из-за которой отказ"


def test_a_broken_note_costs_one_line_and_no_more(folder: pathlib.Path) -> None:
    """Стережёт независимость фразы от записки: испорченный файл её не отменяет."""
    first = OneCopy(folder)
    first.take()
    (folder / LOCK_FILE_NAME).write_bytes(b"\x00\xff\xfe not a note at all")
    try:
        verdict = OneCopy(folder).take()
    finally:
        first.release()
    assert not verdict.taken
    assert "«Терминал» уже запущен" in verdict.trouble
    assert "Запущена:" not in verdict.trouble


def test_a_note_full_of_junk_reaches_no_one(folder: pathlib.Path) -> None:
    """Стережёт чистку записки: мусор из файла не доезжает до глаз человека.

    Прежняя проверка этого не ловила и была вакуумной: она клала в файл
    двоичный мусор **без** строки `started=`, то есть разбирать было нечего.
    Здесь строка есть, и в ней управляющие символы и длина в километр —
    ровно то, что даёт оборванная запись или чужой файл.
    """
    first = OneCopy(folder)
    first.take()
    (folder / LOCK_FILE_NAME).write_text(
        "started=\x1b[31m" + "ы" * 500 + "\x07\n", encoding="utf-8"
    )
    try:
        trouble = OneCopy(folder).take().trouble
    finally:
        first.release()
    assert not trouble.startswith("Запущена"), trouble
    for control in ("\x1b", "\x07"):
        assert control not in trouble, "управляющие символы из файла ушли в окно"
    assert len(trouble) < 600, f"мусор из файла раздул текст до {len(trouble)} знаков"


def test_a_long_note_is_cut_short(folder: pathlib.Path) -> None:
    """Стережёт потолок длины: печатный, но бесконечный мусор тоже обрезается."""
    first = OneCopy(folder)
    first.take()
    (folder / LOCK_FILE_NAME).write_text("started=" + "ы" * 500 + "\n", encoding="utf-8")
    try:
        trouble = OneCopy(folder).take().trouble
    finally:
        first.release()
    assert "ы" * 100 not in trouble, "значение из записки вставлено целиком, без потолка"


def test_the_note_says_the_file_holds_nothing(folder: pathlib.Path) -> None:
    """Стережёт записку: нашедший файл человек читает, что удалять его можно."""
    copy = OneCopy(folder)
    copy.take()
    try:
        text = (folder / LOCK_FILE_NAME).read_text(encoding="utf-8")
    finally:
        copy.release()
    assert f"pid={os.getpid()}" in text
    assert "started=" in text
    assert "можно удалить" in text, "человеку не сказано, что файл сам ничего не держит"


# ------------------------------------------ ключи командной строки (app/main.py)

def _no_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    """Не давать прогону теста писать в боевой `userdata/logs/`."""
    monkeypatch.setattr("app.logs.setup_logging", lambda *_, **__: None)


def _empty_database(folder: pathlib.Path) -> pathlib.Path:
    path = folder / "candles.sqlite3"
    with CandleStore(path):
        pass
    return path


def test_runs_reads_while_the_program_is_open(
    folder: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Стережёт `--runs`: он только читает, и открытое окно ему не помеха.

    Запретить его значило бы наказать человека за предохранитель: спросить
    «что я вчера гонял», не закрывая программу, — обычное дело.
    """
    _no_logging(monkeypatch)
    database = _empty_database(folder)
    held = OneCopy(folder)
    assert held.take().taken
    try:
        main(["--runs", "--db", str(database)])
    finally:
        held.release()
    assert "уже запущен" not in capsys.readouterr().err


def test_inspect_reads_while_the_program_is_open(
    folder: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Стережёт `--inspect`: тоже только чтение, тоже без замка."""
    _no_logging(monkeypatch)
    database = _empty_database(folder)
    held = OneCopy(folder)
    assert held.take().taken
    try:
        main(["--inspect", "MXU6", "--db", str(database)])
    finally:
        held.release()
    assert "уже запущен" not in capsys.readouterr().err


def test_fetch_refuses_while_the_program_is_open(
    folder: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Стережёт `--fetch`: он пишет в базу, значит под замком.

    Загрузка кладёт в базу тысячи свечей разом; живой поток котировок будет
    её ждать и получит «database is locked» — таймаут `sqlite3` пять секунд.
    """
    _no_logging(monkeypatch)

    def never(*_: object, **__: object) -> int:
        raise AssertionError("загрузка пошла в базу при открытой программе")

    monkeypatch.setattr("app.fetch.run_from_arguments", never)
    database = _empty_database(folder)
    held = OneCopy(folder)
    assert held.take().taken
    try:
        code = main(["--fetch", "MXU6", "--db", str(database)])
    finally:
        held.release()
    assert code == 1, "загрузка при открытой программе завершилась успехом"
    trouble = capsys.readouterr().err
    assert "«Терминал» уже запущен" in trouble
    assert "Закройте окно" in trouble, "человеку не сказано, что делать"


def test_fetch_works_when_nothing_is_open(
    folder: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Стережёт от «предохранитель, который отказывает всегда»: свободная папка грузится."""
    _no_logging(monkeypatch)
    called: list[str] = []
    monkeypatch.setattr(
        "app.fetch.run_from_arguments",
        lambda *_, **__: called.append("да") or 0,  # type: ignore[func-returns-value]
    )
    database = _empty_database(folder)
    assert main(["--fetch", "MXU6", "--db", str(database)]) == 0
    assert called, "загрузка не пошла и при свободной папке — замок отказывает всегда"


def test_fetch_gives_the_folder_back(
    folder: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Стережёт освобождение: после загрузки папка не остаётся занятой навсегда."""
    _no_logging(monkeypatch)
    monkeypatch.setattr("app.fetch.run_from_arguments", lambda *_, **__: 0)
    database = _empty_database(folder)
    main(["--fetch", "MXU6", "--db", str(database)])
    after = OneCopy(folder)
    try:
        assert after.take().taken, "загрузка не отдала папку обратно"
    finally:
        after.release()


# --------------------------------------------------- ветка окна, целым запуском

def _program(source: str | None, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Запуск настоящей программы отдельным процессом.

    `source` — драйвер, подменяющий одну вещь и зовущий тот же `main()`;
    `None` — обычный `python3 -m app.main`.
    """
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPO)
    # ⚠️ Негодный плагин вместо `pop` — тот же довод, что в
    # `tests/test_app_main.py::_launch`. Присвоение `offscreen` в ветке
    # `--shot` обязано перебить то, что было в окружении; `pop` этого
    # не проверял, а ребёнку отдавал настоящий экран (`B-033`).
    environment["QT_QPA_PLATFORM"] = "there-is-no-such-plugin"
    entry = ["-c", textwrap.dedent(source)] if source else ["-m", "app.main"]
    return subprocess.run(
        [sys.executable, *entry, *arguments],
        cwd=REPO, env=environment, capture_output=True, text=True,
        timeout=TIMEOUT, check=False,
    )


def test_the_window_refuses_before_it_touches_the_database(folder: pathlib.Path) -> None:
    """Стережёт ветку окна: вторая копия выходит фразой и базы не открывает.

    Настоящий второй процесс, настоящий `main()`. Проверяется и то, что база
    не создана: вторая копия обязана уйти, ничего в папке не тронув.
    """
    database = folder / "candles.sqlite3"
    held = OneCopy(folder)
    assert held.take().taken
    try:
        outcome = _program(
            None, "--db", str(database), "--shot", str(folder / "view.png"),
        )
    finally:
        held.release()
    assert outcome.returncode == 1, (
        f"вторая копия запустилась, код {outcome.returncode}\n{outcome.stdout}"
    )
    assert "«Терминал» уже запущен" in outcome.stderr, outcome.stderr
    assert "панели задач" in outcome.stderr, outcome.stderr
    assert "Traceback" not in outcome.stderr, outcome.stderr
    assert not database.exists(), "вторая копия успела создать базу свечей"


@pytest.mark.slow
def test_the_refusal_survives_a_qt_that_cannot_start(folder: pathlib.Path) -> None:
    """Стережёт `B-028`: отказ печатается **до** первого касания Qt.

    Владелец счёта 06.09.2026 запустил вторую копию из консоли и получил
    **ничего**: ни окна, ни строки, ни ответа на Ctrl+C. Замок сработал
    верно — молчала программа. Причина в порядке: замок брался после
    `QApplication`, и любая беда Qt съедала отказ целиком.

    ⚠️ Ломается тем, чем это и ломалось: Qt, который не может подняться.
    Плагин платформы назван несуществующим, и Qt на этом падает насмерть
    (`qFatal`, перехватить нечем). Фраза обязана быть напечатана **до**
    падения. Проверка «вторая копия не запустилась» это не заменяет:
    она зеленеет и на молчаливом падении, то есть стережёт не то.
    """
    database = folder / "candles.sqlite3"
    held = OneCopy(folder)
    assert held.take().taken, "первая копия замок не взяла — проверка вакуумна"
    try:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(REPO)
        environment["QT_QPA_PLATFORM"] = "there-is-no-such-plugin"
        outcome = subprocess.run(
            [sys.executable, "-m", "app.main", "--db", str(database)],
            cwd=REPO, env=environment, capture_output=True, text=True,
            timeout=TIMEOUT, check=False,
        )
    finally:
        held.release()
    assert "«Терминал» уже запущен" in outcome.stderr, (
        "вторая копия промолчала: Qt не поднялся, и отказ пропал вместе с ним\n"
        f"stdout: {outcome.stdout}\nstderr: {outcome.stderr}"
    )
    assert not database.exists(), "вторая копия успела создать базу свечей"


def test_the_refusal_names_the_process_that_holds_the_folder(folder: pathlib.Path) -> None:
    """Стережёт вторую половину `B-028`: сказано, **кто** держит папку.

    Совет «переключитесь на открытое окно» вёл в никуда: держателем была
    копия, зависшая тремя часами раньше, и окна у неё не было. Номер
    процесса — единственное, чем её снять.
    """
    first = OneCopy(folder)
    assert first.take().taken
    try:
        trouble = OneCopy(folder).take(Purpose.WINDOW).trouble
    finally:
        first.release()
    assert str(os.getpid()) in trouble, (
        "в отказе нет номера процесса-держателя: зависшую копию снять нечем"
    )
    assert "Номер процесса" in trouble, "номер назван, но не сказано, что это"


#: Драйвер: замок «взят», но с оговоркой — так выглядит папка на сетевом диске,
#: где системного вызова нет. Строка обязана дойти до журнала решений, а не
#: остаться в консоли: консоли при запуске двойным щелчком нет.
LOUD_NOTE = "ПАПКА-ЗАМОК-НЕ-ДЕРЖИТ"
NOTE_DRIVER = f'''
import sys

import app.single_instance as single
from app.single_instance import Purpose, Verdict

single.OneCopy.take = lambda self, purpose=Purpose.WINDOW: Verdict(True, "{LOUD_NOTE}")

from app.port import HistoryPort

_note = HistoryPort.note


def loud(self, event, reason, *rest):
    print("ЖУРНАЛ:", event, "|", reason, flush=True)
    return _note(self, event, reason, *rest)


HistoryPort.note = loud

from app.main import main

raise SystemExit(main(sys.argv[1:]))
'''


@pytest.mark.slow
def test_the_journal_is_told_when_the_folder_cannot_hold_a_lock(
    folder: pathlib.Path,
) -> None:
    """Стережёт доезд оговорки до журнала решений, а не только до консоли."""
    outcome = _program(
        NOTE_DRIVER,
        "--db", str(folder / "candles.sqlite3"),
        "--shot", str(folder / "view.png"),
    )
    assert outcome.returncode == 0, outcome.stderr
    lines = [line for line in outcome.stdout.splitlines() if line.startswith("ЖУРНАЛ:")]
    assert any("Один экземпляр программы" in line and LOUD_NOTE in line for line in lines), (
        "оговорка «замок не поставлен» до журнала решений не доехала:\n"
        + "\n".join(lines)
    )


# ----------------------------------------------- отказ показывается окном

class _FakeBox:
    """Подстановка вместо `QMessageBox`: запоминает, что ей сказали показать."""

    shown: list[str] = []

    class Icon:
        Information = object()

    def setIcon(self, _: object) -> None: ...  # noqa: N802 — имя задано Qt

    def setWindowTitle(self, _: str) -> None: ...  # noqa: N802 — имя задано Qt

    def setText(self, text: str) -> None:  # noqa: N802 — имя задано Qt
        _FakeBox.shown.append(text)

    def exec(self) -> int:
        return 0


def test_the_refusal_is_shown_by_a_window_not_only_by_the_console(
    qapp, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Стережёт главный путь показа: при запуске двойным щелчком консоли нет.

    Без окна вторая копия для владельца счёта просто не запускается —
    молча, без единого слова. Это и есть «молчание», которого быть не должно.

    ⚠️ **Переменная здесь — вход развилки, а не приказ Qt** (`B-033`).
    Проверяется решение `_say_already_running`: платформа не из
    `SCREENLESS_PLATFORMS` — значит человек за экраном и ему нужно окно.
    Само окно подставное (`_FakeBox`), настоящее Qt тут не при чём.

    До 06.09.2026 разницы никто не делал, и она стоила двух бед сразу.
    `_show_already_running` зовёт `QApplication.instance() or QApplication(...)`:
    на пустом процессе он поднимал **настоящее приложение xcb**, платформа
    у живого `QApplication` не меняется до конца процесса, и все тесты
    `tests/test_ui_*.py`, доставшиеся тому же воркеру, начинали рисовать
    окна на столе владельца счёта. На машине без монитора тот же вызов
    не поднимал приложение, а убивал воркер: `Fatal Python error: Aborted`.

    Отсюда `qapp`: приложение готовит себе **сам тест**, и готовит
    безэкранное. `QApplication.instance()` возвращает его, второго
    не создаётся, развилка проверяется та же самая.
    """
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox", _FakeBox)
    monkeypatch.setenv("QT_QPA_PLATFORM", "xcb")  # вход развилки, см. докстринг
    assert qapp.platformName() == "offscreen", (
        f"приложение прогона поднято на {qapp.platformName()!r}: проверка "
        "развилки не должна тянуть за собой настоящий экран"
    )
    _FakeBox.shown.clear()
    _say_already_running("«Терминал» уже запущен. Смотрите в панели задач.")
    assert _FakeBox.shown == ["«Терминал» уже запущен. Смотрите в панели задач."], (
        "окно с отказом не показано: при запуске значком человек не увидит ничего"
    )
    assert "уже запущен" in capsys.readouterr().err, "в консоль тоже не сказано"


def test_no_window_is_built_on_a_machine_without_a_screen(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Стережёт от зависания: модальное окно в режиме без экрана ждало бы вечно.

    ⚠️ Подстановка **запоминает**, а не бросает. Первая редакция бросала
    `AssertionError` — и мутация «убрать проверку экрана» её пережила:
    `_say_already_running` гасит любое исключение Qt намеренно, и брошенное
    из подстановки гасилось вместе с ними. Проверка выглядела проверкой
    и не проверяла ничего.
    """
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox", _FakeBox)
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    _FakeBox.shown.clear()
    _say_already_running("«Терминал» уже запущен.")
    assert not _FakeBox.shown, (
        "окно строится на машине без экрана: модальное окно `offscreen` "
        "ждёт нажатия вечно — прогон повиснет"
    )
    assert "уже запущен" in capsys.readouterr().err, "не сказано даже в консоль"
