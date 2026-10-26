"""`D-080`, решение 0064: программа там, где нет данных, говорит об этом на экране.

Три слоя проверки, и каждый ловит своё:

* чистая функция (`app/userdata_notice.py`) — что считается данными, а что нет;
* окно (`MainWindow.show_missing_data`) — плашка без крестика, цвет темы,
  перекраска при смене темы;
* **настоящая сборка** — копия программы отдельным процессом на временной
  папке. Только она видит порядок в `_run`: снимок до чтения настроек,
  доезд текста до плашки, журнала решений и технического лога. Чистая
  функция и окно по отдельности зелены и на программе, которая их не зовёт.
"""

from __future__ import annotations

import builtins
import io
import os
import pathlib
import subprocess
import sys
import textwrap
from collections.abc import Callable

import pytest

from app.settings_store import SETTINGS_FILE_NAME, SettingsStore
from app.userdata_notice import missing_data_notice
from broker.token_store import TOKEN_FILE_NAME
from market import CandleStore

REPO = pathlib.Path(__file__).resolve().parent.parent
TIMEOUT = 180

#: Подделка содержимого файла ключа. Ключом не является, и ни в плашку,
#: ни в журнал, ни в лог попадать не должна (правило 7): признак смотрит
#: только, лежит ли файл.
KEY_MARKER = "KEY-MARKER-d080-never-shown"


def _key_pieces() -> list[str]:
    """Каждый кусок маркера по восемь знаков: «ни ключа, ни его части» (правило 7).

    Проверка целого маркера зеленеет на утечке среза — первых знаков ключа
    в строке лога.
    """
    return [KEY_MARKER[start : start + 8] for start in range(len(KEY_MARKER) - 7)]


def _base(path: pathlib.Path) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with CandleStore(path):
        pass
    assert path.exists(), "база не завелась — проверка «только база» была бы пустой"
    return path


def _settings(folder: pathlib.Path) -> None:
    from ui.models import Settings

    folder.mkdir(parents=True, exist_ok=True)
    assert not SettingsStore(folder).save(Settings()), "настройки не записались"


def _broken_settings(folder: pathlib.Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / SETTINGS_FILE_NAME).write_text("{это не json", encoding="utf-8")


def _key(folder: pathlib.Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / TOKEN_FILE_NAME).write_text(
        f'{{"refresh_token": "{KEY_MARKER}"}}', encoding="utf-8"
    )


# ------------------------------------------------------------ чистая функция


def test_an_empty_folder_names_itself_and_where_to_copy(tmp_path: pathlib.Path) -> None:
    """Стережёт текст: абсолютная папка данных и папка, куда класть `userdata`."""
    folder = tmp_path / "Terminal" / "userdata"
    folder.mkdir(parents=True)
    text = missing_data_notice(folder / "candles.sqlite3")
    assert text.startswith("Данных нет"), text
    assert f"в папке {folder.resolve()} нет" in text, text
    assert f"сюда: {folder.resolve().parent} —" in text, text
    assert "перенесите папку userdata" in text, text
    assert "скопируйте" not in text, "копия с ключом обходит замок «одна копия» (решение 0039)"
    assert "первый запуск" in text, "на новой машине текст обязан успокаивать, а не пугать"


def test_what_the_program_makes_itself_is_not_data(tmp_path: pathlib.Path) -> None:
    """Лог, замок «одна копия» и проба записи заводятся раньше — и данными не считаются."""
    folder = tmp_path / "userdata"
    (folder / "logs").mkdir(parents=True)
    (folder / "logs" / "terminal.log").write_text("строка", encoding="utf-8")
    (folder / "instance.lock").write_text("", encoding="utf-8")
    (folder / ".write-probe").write_bytes(b"")
    assert missing_data_notice(folder / "candles.sqlite3"), (
        "служебные файлы приняты за данные: плашки нет там, где данных нет"
    )


@pytest.mark.parametrize("lay", [_settings, _broken_settings, _key], ids=lambda f: f.__name__)
def test_any_one_of_the_three_is_data(tmp_path: pathlib.Path, lay) -> None:
    """Хоть один файл из трёх — данные есть, плашки нет."""
    folder = tmp_path / "userdata"
    lay(folder)
    assert missing_data_notice(folder / "candles.sqlite3") == ""


def test_the_base_is_the_one_named_not_the_default_name(tmp_path: pathlib.Path) -> None:
    """Стережёт `database.exists()`: при `--db` имя базы своё, и она — данные."""
    database = _base(tmp_path / "work" / "other.sqlite3")
    assert missing_data_notice(database) == "", (
        "названная ключом база не признана данными — проверяется имя по умолчанию"
    )


def test_a_relative_path_is_made_absolute(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Путь из текста копируют руками: относительный отправил бы не туда."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "userdata").mkdir()
    text = missing_data_notice(pathlib.Path("userdata") / "candles.sqlite3")
    assert f"в папке {(tmp_path / 'userdata').resolve()} нет" in text, text
    assert f"сюда: {tmp_path.resolve()} —" in text, text


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0,
    reason="права файла: на Windows нет chmod, у root они не действуют",
)
def test_the_key_file_is_not_opened(tmp_path: pathlib.Path) -> None:
    """Правило 7: признак смотрит, лежит ли файл ключа, и не читает его.

    Файл без права чтения: открытие дало бы `PermissionError`.
    """
    folder = tmp_path / "userdata"
    _key(folder)
    key = folder / TOKEN_FILE_NAME
    key.chmod(0)
    try:
        assert missing_data_notice(folder / "candles.sqlite3") == ""
    finally:
        key.chmod(0o600)


def test_the_key_file_is_never_opened_even_quietly(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Правило 7: ни одной попытки открыть файл ключа — и проглоченной тоже.

    Права файла (тест выше) ловят чтение, только пока отказ долетает
    до теста: чтение под `try/except OSError` проходит их молча, а кусок
    ключа уходит в лог. Здесь открытие записывается до того, как случится.
    """
    folder = tmp_path / "userdata"
    _key(folder)
    key = os.path.realpath(folder / TOKEN_FILE_NAME)
    attempts: list[str] = []

    def spy(real: Callable[..., object], name: str) -> Callable[..., object]:
        def opener(file: object, *args: object, **kwargs: object) -> object:
            if isinstance(file, str | os.PathLike) and os.path.realpath(file) == key:
                attempts.append(name)
            return real(file, *args, **kwargs)

        return opener

    monkeypatch.setattr(io, "open", spy(io.open, "io.open"))
    monkeypatch.setattr(builtins, "open", spy(builtins.open, "open"))
    monkeypatch.setattr(os, "open", spy(os.open, "os.open"))
    assert missing_data_notice(folder / "candles.sqlite3") == ""
    assert not attempts, f"файл ключа открывали ({attempts}): признак обязан только смотреть"


# ------------------------------------------------------------------- окно


@pytest.fixture()
def window(qapp):
    from market import redact
    from ui.main_window import MainWindow
    from ui.models import RobotState

    win = MainWindow(sanitize=redact)  # порт по умолчанию: окну хватает
    win.resize(1280, 860)
    yield win
    win._timer.stop()  # noqa: SLF001 — часы окна, иначе тикают после теста
    win.apply_state(RobotState())
    win.close()
    win.deleteLater()
    qapp.processEvents()


def test_the_window_shows_the_text_as_given_and_without_a_cross(window) -> None:
    """Плашка показывает готовый текст и снять её нечем: висит весь сеанс."""
    from ui.theme import LIGHT

    assert not window.data_banner.isVisibleTo(window), "плашка видна до всякого текста"
    window.show_missing_data("Данных нет: проверка <b>разметки</b>")
    assert window.data_banner.isVisibleTo(window)
    assert window.data_banner.text() == "Данных нет: проверка <b>разметки</b>"
    assert window.data_banner.close_button is None, "у плашки «данных нет» появился крестик"
    assert LIGHT.warning in window.data_banner.styleSheet(), window.data_banner.styleSheet()
    window.show_missing_data("")
    assert not window.data_banner.isVisibleTo(window)


def test_the_banner_stands_below_the_halt_and_above_the_chart(window) -> None:
    """Место в раскладке — по срочности: ниже остановки робота, выше графика."""
    layout = window.centralWidget().layout()
    halt = layout.indexOf(window.halt_banner)
    data = layout.indexOf(window.data_banner)
    chart = layout.indexOf(window.chart.parentWidget())
    assert data >= 0, "плашка «данных нет» не поставлена в окно"
    assert halt < data < chart, (halt, data, chart)


def test_the_banner_follows_the_theme(window, qapp) -> None:
    """Смена темы перекрашивает плашку: она не входит в состояние робота."""
    from PySide6.QtGui import QColor, QPalette

    from ui.theme import DARK

    window.show_missing_data("Данных нет: проверка темы")
    original = QPalette(qapp.palette())
    try:
        dark = QPalette(original)
        dark.setColor(QPalette.ColorRole.Window, QColor("#15181d"))
        qapp.setPalette(dark)
        qapp.processEvents()
        assert window.theme.dark, "смена темы не перехвачена"
        assert DARK.warning in window.data_banner.styleSheet(), (
            "плашка «данных нет» осталась в цветах прежней темы"
        )
    finally:
        qapp.setPalette(original)
        qapp.processEvents()


# --------------------------------------------------- настоящая сборка программы

#: Та же программа тем же `main()`. Подмены только смотрят: `MainWindow.show`
#: печатает плашку после показа, `HistoryPort.note` — строку журнала.
DRIVER = '''
import sys

import ui.main_window
from app.port import HistoryPort

_note = HistoryPort.note


def loud(self, event, reason, *rest):
    print("ЖУРНАЛ:", event, "|", reason, flush=True)
    return _note(self, event, reason, *rest)


HistoryPort.note = loud
_show = ui.main_window.MainWindow.show


def show(self):
    _show(self)
    banner = self.data_banner
    print("ПЛАШКА:", banner.isVisible(), "|", banner.text(), flush=True)


ui.main_window.MainWindow.show = show

from app.main import main

raise SystemExit(main(sys.argv[1:]))
'''


def _program(database: pathlib.Path) -> tuple[str, str]:
    """Программа на базе `database` со снимком; (что сказала плашка, весь вывод)."""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPO)
    environment["TERMINAL_TEST_ISS_STUB"] = "1"  # биржа — отказом сети, без сокета
    environment["QT_QPA_PLATFORM"] = "there-is-no-such-plugin"  # `--shot` обязан перебить
    outcome = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(DRIVER),
         "--db", str(database), "--shot", str(database.parent.parent / "view.png")],
        cwd=REPO, env=environment, capture_output=True, text=True,
        timeout=TIMEOUT, check=False,
    )
    said = outcome.stdout + outcome.stderr
    assert outcome.returncode == 0, said
    banners = [line for line in outcome.stdout.splitlines() if line.startswith("ПЛАШКА:")]
    assert len(banners) == 1, f"окно показано не один раз:\n{said}"
    return banners[0], said


def _journal(said: str) -> list[str]:
    return [line for line in said.splitlines() if line.startswith("ЖУРНАЛ: Папка данных |")]


def _technical_log(folder: pathlib.Path) -> str:
    return "".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in sorted((folder / "logs").glob("*.log*"))
    )


@pytest.mark.slow
def test_the_program_says_so_on_an_empty_folder(tmp_path: pathlib.Path) -> None:
    """Пустая папка (лог и замок не в счёт) — плашка видна, строка в журнале и в логе."""
    folder = tmp_path / "Terminal" / "userdata"
    (folder / "logs").mkdir(parents=True)
    (folder / "instance.lock").write_text("", encoding="utf-8")
    banner, said = _program(folder / "candles.sqlite3")
    assert banner.startswith("ПЛАШКА: True | Данных нет"), f"плашки нет:\n{said}"
    assert str(folder.resolve()) in banner, banner
    assert f"сюда: {folder.resolve().parent} —" in banner, banner
    assert any("Данных нет" in line for line in _journal(said)), (
        f"строки «Папка данных» в журнале решений нет:\n{said}"
    )
    assert "Данных нет" in _technical_log(folder), "строки в техническом логе нет"


@pytest.mark.slow
@pytest.mark.parametrize(
    "lay",
    [
        lambda folder: _base(folder / "candles.sqlite3"),
        _settings,
        _key,
        _broken_settings,
    ],
    ids=["base", "settings", "key", "broken_settings"],
)
def test_the_program_is_quiet_when_any_data_is_there(tmp_path: pathlib.Path, lay) -> None:
    """Один из трёх файлов — плашки и строки нет.

    `broken_settings` стережёт порядок в `_run`: чтение настроек
    переименовывает испорченный файл, и снимок после него сказал бы
    «данных нет» человеку, у которого они есть.
    `key` заодно стережёт правило 7: ни содержимое файла ключа, ни кусок
    его не появляется ни в выводе, ни в журнале, ни в техническом логе.
    """
    folder = tmp_path / "Terminal" / "userdata"
    lay(folder)
    banner, said = _program(folder / "candles.sqlite3")
    assert banner == "ПЛАШКА: False | ", f"плашка на папке с данными:\n{said}"
    assert not [line for line in _journal(said) if "Данных нет" in line], said
    log = _technical_log(folder)
    shown = [piece for piece in _key_pieces() if piece in said]
    logged = [piece for piece in _key_pieces() if piece in log]
    assert not shown, f"кусок файла ключа попал в вывод программы: {shown}"
    assert not logged, f"кусок файла ключа попал в технический лог: {logged}"


@pytest.mark.slow
def test_the_program_checks_the_folder_given_by_key(tmp_path: pathlib.Path) -> None:
    """`--db` в папке не с именем `userdata` — проверяется и называется она."""
    folder = tmp_path / "elsewhere"
    folder.mkdir()
    banner, said = _program(folder / "base.sqlite3")
    assert banner.startswith("ПЛАШКА: True | Данных нет"), f"плашки нет:\n{said}"
    assert f"в папке {folder.resolve()} нет" in banner, banner
    assert f"сюда: {tmp_path.resolve()} —" in banner, banner
