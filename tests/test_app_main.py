"""Программа запускается и завершается. Проверка отдельным процессом.

Почему процесс, а не вызов `app.main.main()` в тесте: `main()` создаёт
`QApplication` и цикл qasync, а в прогоне тестов приложение Qt уже одно
на всю сессию — второе даёт падение. Отдельный процесс проверяет ровно то,
что делает владелец счёта: набирает команду и смотрит, открылось ли окно.

Четыре вещи, которые иначе не проверяются ничем:

* программа **завершается сама**, кодом 0. Зависание на выходе — известная
  болезнь связки Qt + asyncio (решение 0005), и ловится она только запуском;
* окно **открывается без базы** и говорит об этом фразой, а не трассировкой;
* **негодная база** объясняется фразой, а не трассировкой;
* снимок экрана делается на машине без экрана — тем же способом, каким
  снимки попадают в отчёты.

⚠️ Запуск с `--shot` до закрытия окна не доходит: он сохраняет картинку
и возвращается, не входя ни в `await closed.wait()`, ни в закрытие окна Qt.
А болезнь незавершения из решения 0005 живёт именно там. Поэтому один запуск
идёт **без** снимка, а окно в нём закрывает таймер — то же самое, что крестик
мышкой.
"""

from __future__ import annotations

import hashlib
import math
import os
import pathlib
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

from market import MSK, Candle, CandleStore, Source, Timeframe

REPO = pathlib.Path(__file__).resolve().parent.parent
#: Запуск Qt в отдельном процессе не мгновенный; на медленной машине сборки
#: полторы секунды легко превращаются в десять.
TIMEOUT = 180


def _database(path: pathlib.Path) -> pathlib.Path:
    """База с настоящей формой данных: минутки, из которых собираются бары."""
    start = datetime(2026, 6, 19, 7, 0, tzinfo=MSK)  # пятница
    minutes = [
        Candle(
            time=start + timedelta(minutes=index),
            open=100000.0 + 300.0 * math.sin(index / 9.0),
            high=100400.0 + 300.0 * math.sin(index / 9.0),
            low=99600.0 + 300.0 * math.sin(index / 9.0),
            close=100000.0 + 300.0 * math.sin(index / 9.0),
            volume=1.0, timeframe=Timeframe(1), filled_minutes=1,
        )
        for index in range(300)
    ]
    with CandleStore(path) as store:
        store.put_minutes("MXU6", minutes, Source.ISS)
    return path


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return _launch(["-m", "app.main"], arguments)


def _launch(
    entry_point: list[str], arguments, extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ) | (extra or {})
    environment["PYTHONPATH"] = str(REPO)
    # ⚠️ Не `pop`, а заведомо негодный плагин, и это **усиление** проверки.
    # Режим снимка не подхватывает платформу из окружения, а **присваивает**
    # её (`app/main.py`, ветка `--shot`) — именно затем, чтобы перебить
    # оставшееся от другого запуска. `pop` этого не стерёг: пустая переменная
    # и присвоенная дают один и тот же снимок, и пропажу присвоения он
    # пережил бы молча. С негодным плагином Qt умирает насмерть — снимок
    # получится только если присвоение на месте.
    #
    # И второе: `pop` отдавал ребёнку настоящий `DISPLAY`, то есть окно
    # на столе владельца счёта (`B-033`). Негодный плагин окна не откроет
    # ни на какой машине.
    environment["QT_QPA_PLATFORM"] = "there-is-no-such-plugin"
    # Биржа ребёнку не нужна, а при старте он сам догружает историю (B-047):
    # заглушка отвечает отказом сети, не открывая сокета
    # (`tests/child_guard/offline_iss.py`).
    environment["TERMINAL_TEST_ISS_STUB"] = "1"
    return subprocess.run(
        [sys.executable, *entry_point, *arguments],
        cwd=REPO, env=environment, capture_output=True, text=True,
        timeout=TIMEOUT, check=False,
    )


#: Та же программа, тем же `main()`, но окно закрывается само через секунду.
#: Подмена — ровно одна строка: `MainWindow.show`. Ни порядок запуска,
#: ни завершение, ни цикл событий не трогаются, потому что проверяются
#: именно они.
CLOSING_DRIVER = '''
import os
import sys

os.environ["QT_QPA_PLATFORM"] = "offscreen"   # машина сборки без экрана
os.environ["QT_API"] = "pyside6"              # до первого импорта qasync

from PySide6.QtCore import QTimer

import ui.main_window

_original_show = ui.main_window.MainWindow.show


def show(self):
    _original_show(self)
    QTimer.singleShot(1500, self.close)       # то же, что крестик мышкой


ui.main_window.MainWindow.show = show

from app.main import main

raise SystemExit(main(sys.argv[1:]))
'''


@pytest.mark.slow
def test_the_program_starts_and_takes_a_snapshot(tmp_path: pathlib.Path) -> None:
    shot = tmp_path / "view.png"
    outcome = _run(
        "--db", str(_database(tmp_path / "candles.sqlite3")),
        "--shot", str(shot),
    )
    assert outcome.returncode == 0, f"программа вышла с кодом {outcome.returncode}\n{outcome.stderr}"
    assert shot.exists(), outcome.stdout + outcome.stderr
    assert shot.stat().st_size > 5000, "снимок подозрительно мал — окно пустое?"
    assert "снимок сохранён" in outcome.stdout


@pytest.mark.slow
def test_the_window_still_opens_without_a_database(tmp_path: pathlib.Path) -> None:
    """Отсутствие базы — сообщение в окне, а не падение при старте."""
    shot = tmp_path / "view.png"
    outcome = _run(
        "--db", str(tmp_path / "no-such.sqlite3"),
        "--shot", str(shot),
    )
    assert outcome.returncode == 0, outcome.stderr
    assert shot.exists()
    assert "Traceback" not in outcome.stderr, outcome.stderr


@pytest.mark.slow
def test_closing_the_window_ends_the_program(tmp_path: pathlib.Path) -> None:
    """Программа выходит сама, кодом 0, и выходит целиком.

    Это единственный запуск, который доходит до `await closed.wait()`
    и до закрытия окна Qt — там живёт болезнь незавершения из решения 0005:
    цикл, остановленный на середине завершения, оставляет базу открытой,
    а процесс висящим. С `--shot` этот путь не исполняется вовсе.

    Зависание ловится таймаутом `subprocess.run`: `TimeoutExpired` роняет
    тест, а не превращается в зелёный прогон.
    """
    driver = tmp_path / "закрыть-окно.py"
    driver.write_text(CLOSING_DRIVER, encoding="utf-8")
    outcome = _launch(
        [str(driver)],
        ["--db", str(_database(tmp_path / "candles.sqlite3"))],
    )
    assert outcome.returncode == 0, (
        f"программа вышла с кодом {outcome.returncode}\n{outcome.stdout}\n{outcome.stderr}"
    )
    assert "Traceback" not in outcome.stderr, outcome.stderr


def test_an_unreadable_database_is_explained_to_a_person(tmp_path: pathlib.Path) -> None:
    """Файл, который не база: фраза и код возврата, а не трассировка.

    Так выглядит выбранный не тот файл, обрывок загрузки и база от более
    новой сборки. Прежде открытие базы стояло ДО `try` в главной корутине:
    владелец счёта получал трассировку, а поток данных оставался незакрытым
    с открытым соединением SQLite.
    """
    not_a_database = tmp_path / "not-a-database.sqlite3"
    not_a_database.write_bytes("это не sqlite, а просто файл\n".encode("utf-8") * 32)
    outcome = _run("--db", str(not_a_database), "--shot", str(tmp_path / "view.png"))
    assert outcome.returncode != 0, "негодная база принята молча"
    assert "Traceback" not in outcome.stderr, outcome.stderr
    assert "not-a-database.sqlite3" in outcome.stderr, outcome.stderr
    assert "база" in outcome.stderr.lower()


def test_a_bad_argument_is_explained_to_a_person() -> None:
    outcome = _run("--days", "-3")
    assert outcome.returncode != 0
    assert "0 означает" in outcome.stderr, outcome.stderr


def test_a_bad_moment_is_explained_to_a_person() -> None:
    outcome = _run("--until", "вчера")
    assert outcome.returncode != 0
    assert "28.08.2026" in outcome.stderr, outcome.stderr


# --------------------------------------------------------------- палитра окна

def test_the_window_is_dark_unless_asked_otherwise() -> None:
    """Тёмная палитра — умолчание, а не ключ.

    Просьба владельца счёта 06.09.2026: «надо тёмную тему по умолчанию
    сделать». До этого окно брало тему системы, и на собранной программе
    под Windows она **вышла разной на двух запусках подряд** (замер сборщика
    06.09.2026): системная тема читается по яркости фона палитры, а палитра
    на старте успевает прийти не всегда.

    Сторож стережёт именно умолчание. Проверка «`--light` даёт светлое» его
    не заменяет: она осталась бы зелёной, если бы умолчание вернулось
    к системной теме.
    """
    from app.main import _arguments, wants_dark

    assert wants_dark(_arguments([])) is True, "без ключей окно обязано быть тёмным"
    assert wants_dark(_arguments(["--dark"])) is True
    assert wants_dark(_arguments(["--light"])) is False


def test_the_two_palette_keys_refuse_to_stand_together() -> None:
    """`--light --dark` в одной строке — отказ, а не молчаливая победа одного.

    Верного прочтения у пары нет: победа последнего зависит от порядка,
    которого человек не держит в голове, а молчаливая победа одного даёт
    окно, которого не просили.
    """
    from app.main import _arguments

    with pytest.raises(SystemExit):
        _arguments(["--light", "--dark"])


def test_the_dark_palette_shows_that_a_control_is_switched_off(qapp) -> None:
    """Выключенная кнопка обязана **выглядеть** выключенной, а не только быть.

    Замер 09.09.2026, найдено при правке окна выбора алгоритма. `QPalette.
    setColor(role, цвет)` красит **все** группы разом, включая `Disabled`:
    текст выключенной кнопки выходил того же цвета, что и живой, и отличалась
    она одной рамкой — 314 различающихся точек из 3350 против 1039 в системной
    палитре. Кнопка, выглядящая живой и молчащая на нажатие, — это молчание
    по правилу 13 `CLAUDE.md`, и касается это не одной кнопки: так же
    выглядели «Старт» на токене только для чтения и любой другой запрет.

    ⚠️ Проверяется **возвращённая** палитра, а не палитра приложения:
    приложение Qt в прогоне одно на всю сессию, и подмена его палитры
    покрасила бы не тем соседа по процессу.

    ⚠️ Порог снизу тоже нужен. Выключенное — не значит нечитаемое: человек
    обязан прочесть, чего именно ему не дают. 4,5:1 — обычный порог WCAG
    для текста.

    Мутация, обязанная ронять проверку: убрать цикл по `ColorGroup.Disabled`
    из `_dark_palette`.
    """
    from PySide6.QtGui import QPalette

    from app.main import _dark_palette
    from ui.theme import contrast

    palette = _dark_palette()
    for role in (
        QPalette.ColorRole.ButtonText,
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
    ):
        live = palette.color(QPalette.ColorGroup.Active, role)
        off = palette.color(QPalette.ColorGroup.Disabled, role)
        assert off != live, (
            f"выключенный {role.name} того же цвета, что живой: запрет не виден"
        )
        paper = palette.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Button)
        faded = contrast(off.name(), paper.name())
        assert faded >= 4.5, (
            f"выключенный {role.name} нечитаем: {faded:.1f}:1 против порога 4,5"
        )
        assert faded < contrast(live.name(), paper.name()), (
            f"выключенный {role.name} не тусклее живого — отличить их нельзя"
        )


def test_the_shot_keys_refuse_to_work_without_a_shot() -> None:
    """`--shot-of` и `--shot-tab` без `--shot` — отказ вслух, а не «ничего».

    Ключ, который молча ничего не делает, — это полчаса поиска причины,
    по которой снимок «не изменился». Правило 13 `CLAUDE.md`.
    """
    from app.main import _arguments

    with pytest.raises(SystemExit):
        _arguments(["--shot-of", "settings"])
    with pytest.raises(SystemExit):
        _arguments(["--shot-tab", "Сигнал"])
    # Канарейка: вместе с `--shot` те же ключи обязаны разбираться молча.
    args = _arguments(["--shot", "x.png", "--shot-of", "algorithm"])
    assert args.shot_of == "algorithm"


#: Та же программа, тем же `main()`, с тремя подменами — и ни одна из них
#: не трогает проверяемую проводку, они только **слушают** её.
#:
#: 1. Биржа отвечает о стоимости пункта сразу и числом. Это не упрощение,
#:    а худший для сторожа случай: ответ идёт через `_point_taken` →
#:    `apply_settings`, а `apply_settings` попутно рассылает каталог. Так
#:    каталог доезжает до окна ещё до щелчка «Настройки» — той самой
#:    попутной дорогой, на которой прежний сторож зеленел при снятой проводке.
#: 2. `HistoryPort.request_settings` обёрнут: пока он исполняется, поднят флаг.
#: 3. `MainWindow._on_algorithms` обёрнут: каждый приехавший непустой каталог
#:    записывается как «по просьбе» или «попутно» — по флагу.
#:
#: Обёртки зовут подлинные методы, поэтому поведение программы то же самое.
ALGORITHM_DRIVER = '''
import os
import sys

os.environ["QT_API"] = "pyside6"              # до первого импорта qasync

import app.port
import market.worker
import ui.main_window
from market.point import PointValue

arrived = {"asked": 0, "by_the_way": 0}
state = {"asking": False}


async def point_value(self, symbol, **_):
    return PointValue(symbol=symbol, rubles=1.74, told="подставлено проверкой")


_original_request = app.port.HistoryPort.request_settings


def request_settings(self):
    state["asking"] = True
    try:
        _original_request(self)
    finally:
        state["asking"] = False


_original_on_algorithms = ui.main_window.MainWindow._on_algorithms


def on_algorithms(self, options):
    if options:
        arrived["asked" if state["asking"] else "by_the_way"] += 1
    _original_on_algorithms(self, options)


market.worker.MarketWorker.point_value = point_value
app.port.HistoryPort.request_settings = request_settings
ui.main_window.MainWindow._on_algorithms = on_algorithms

from app.main import main

code = main(sys.argv[1:])
print(f"catalogue asked={arrived['asked']} by_the_way={arrived['by_the_way']}")
raise SystemExit(code)
'''


@pytest.mark.slow
def test_the_program_opens_its_own_algorithm_window_and_it_is_not_empty(
    tmp_path: pathlib.Path,
) -> None:
    """Каталог алгоритмов доезжает до окна выбора в **настоящей** программе.

    ⚠️ Это проверка проводки, а не картинки, и она про правило 12 `CLAUDE.md`.
    Прежние снимки этого окна делались `tools/demo.py`, где каталог подаётся
    диалогу прямо в конструктор: настоящая дорога — порт, сигнал, окно —
    не проверялась ничем, и владелец счёта 09.09.2026 получил пустой список.

    Окно здесь открывает сама программа, а окно выбора — **щелчок по той
    самой кнопке** (`app/main.py::_shot_of_dialog`). Кнопка выключена, когда
    каталог не доехал, и тогда снимка нет, а программа выходит кодом 1.

    ⚠️ **Существующего снимка мало, и это замерено (`B-042`).** Каталог
    доезжает до окна и попутной дорогой: ответ биржи о стоимости пункта
    кладётся через `apply_settings`, а тот рассылает каталог заодно. Прежняя
    проверка держалась на выдуманном тикере `XXZ9`, у которого ответа нет,
    и при снятой проводке зеленела на настоящем тикере и при попутной дороге
    через `_point_trouble` — обе мутации 28.09.2026 дали **1 passed**.

    Поэтому здесь попутная дорога открыта **нарочно** (биржа отвечает числом,
    `ALGORITHM_DRIVER`), а стережётся сам вызов: хотя бы один каталог обязан
    приехать в окно **внутри** `request_settings`, то есть по просьбе,
    отправленной открытием настроек. Отдельно проверено, что попутная дорога
    в этом запуске действительно была: иначе проверка не отличала бы
    проводку от случая, против которого поставлена.

    Мутации, обязанные ронять проверку: снять `self.port.request_settings()`
    из `MainWindow.open_settings` (при открытой попутной дороге); то же
    вместе с `apply_settings` в `_point_trouble`.
    """
    shot = tmp_path / "algorithm.png"
    driver = tmp_path / "open_algorithm_window.py"
    driver.write_text(ALGORITHM_DRIVER, encoding="utf-8")
    outcome = _launch(
        [str(driver)],
        [
            "--db", str(_database(tmp_path / "candles.sqlite3")),
            "--symbol", "MXU6",
            "--shot", str(shot), "--shot-of", "algorithm",
        ],
    )
    said = outcome.stdout + outcome.stderr
    assert outcome.returncode == 0, (
        "программа не смогла открыть своё же окно выбора алгоритма:\n" + said
    )
    assert shot.exists(), said
    assert shot.stat().st_size > 3000, "снимок подозрительно мал — окно пустое?"
    report = re.search(r"catalogue asked=(\d+) by_the_way=(\d+)", outcome.stdout)
    assert report, (
        "подменная обвязка не отчиталась — проверка не знает, как ехал каталог:\n"
        + said
    )
    asked, by_the_way = int(report[1]), int(report[2])
    assert by_the_way > 0, (
        "попутная дорога (ответ биржи → apply_settings) каталога не привезла — "
        "проверка не воспроизводит случай, против которого поставлена:\n" + said
    )
    assert asked > 0, (
        "каталог приехал в окно только попутно, а не по просьбе открытия "
        "настроек: проводка `open_settings` → `request_settings` снята, "
        "и окно показывает то, что осталось от чужого события:\n" + said
    )


# ------------------------------------------------ собранная поставка

def test_a_built_program_does_not_advise_a_python_that_is_not_there() -> None:
    """Совет называет то, чем программу позвали, а не интерпретатор.

    Замер 06.09.2026 на готовой сборке, два разных промаха подряд:

    1. Nuitka в режиме `--standalone` **не ставит `sys.frozen`** — код,
       опиравшийся только на него, считал сборку исходниками и советовал
       `python3 -m app.main --fetch MXU6`, которого на машине владельца
       счёта нет и не будет (`B-024`);
    2. у собранной программы **`sys.executable` называется `python`**,
       и совет вышел «python --fetch MXU6» — команда, которой тоже нет.
       Имя надо брать у `sys.argv[0]`: это то, чем позвали на самом деле.

    Оба промаха видны только на готовом файле, ни один тест их не показывал.
    """
    from app import invocation

    real, real_argv = sys.executable, list(sys.argv)
    try:
        # Собранная программа: имя из argv[0], а не из интерпретатора.
        sys.argv = ["/opt/Terminal/Terminal", "--runs"]
        assert _command_when_frozen(invocation) == "Terminal --fetch MXU6"

        # Признак по имени файла: наш бинарь — да, интерпретатор — нет.
        sys.executable = "/opt/Terminal/Terminal"
        assert invocation.frozen() is True
        sys.executable = "/usr/bin/python3"
        assert invocation.frozen() is False
    finally:
        sys.executable, sys.argv = real, real_argv

    # Из рабочей копии совет обязан остаться прежним.
    assert invocation.command("--fetch", "MXU6") == "python3 -m app.main --fetch MXU6"
    assert "кнопкой" in invocation.how_to_fetch("MXU6")


def _command_when_frozen(invocation: object) -> str:
    """`command()` при собранной программе — с подменённым признаком.

    Признак `__compiled__` подделать нельзя: его ставит сам сборщик модулю.
    Поэтому подменяется `frozen`, а проверяется то, что ниже него, — какое
    имя попадает в совет.
    """
    real = invocation.frozen  # type: ignore[attr-defined]
    invocation.frozen = lambda: True  # type: ignore[attr-defined]
    try:
        return invocation.command("--fetch", "MXU6")  # type: ignore[attr-defined]
    finally:
        invocation.frozen = real  # type: ignore[attr-defined]


def test_the_program_speaks_russian_without_a_locale() -> None:
    """Вывод переводится в utf-8 до первой напечатанной строки.

    Замер 06.09.2026: собранная программа без переменной `LANG` падала
    на своей же первой строке с `UnicodeEncodeError: 'ascii' codec`.
    Python берёт кодировку вывода у локали, а весь текст программы русский.
    Запуск из ярлыка, из планировщика и внутри контейнера идёт с пустым
    окружением сплошь и рядом.

    ⚠️ Проверяется **тот самый путь**, который падал, а не отдельная функция:
    первая редакция этого сторожа звала `_speak_utf8()` руками и оставалась
    зелёной, когда вызов убирали из `main` — то есть стерегла не то, ради
    чего поставлена. Поймано мутацией 06.09.2026.
    """
    import io

    from app.main import main

    ascii_out = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
    ascii_err = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
    real_out, real_err = sys.stdout, sys.stderr
    missing = pathlib.Path(tempfile.mkdtemp()) / "нет-такой-базы.sqlite3"
    try:
        sys.stdout, sys.stderr = ascii_out, ascii_err
        code = main(["--inspect", "MXU6", "--db", str(missing)])
    finally:
        sys.stdout, sys.stderr = real_out, real_err

    assert code == 1
    ascii_out.flush()
    printed = ascii_out.buffer.getvalue().decode("utf-8")  # type: ignore[attr-defined]
    assert "Базы свечей нет" in printed, printed


def test_the_templates_window_reads_runs_from_the_database_given_by_key(
    tmp_path: pathlib.Path, monkeypatch, qapp
) -> None:
    """Стережёт `D-052`: ключ `--db` доходит до окна шаблонов.

    Створку двери (`ui/backend.py`) ставит `app/runs.py` при импорте, и базу
    она берёт умолчанием. Без проводки в `_run` статистика шаблонов при
    `--db FILE` читалась бы из другой базы, и человек решил бы, что набор
    не гонялся.

    Сборка идёт настоящим `_run` и обрывается сразу после проводки: папка
    данных «не готова». Дальше окна и базы проверке ничего не нужно.

    Мутация: убрать вызов `_templates_read_runs_from` из `_database_ready` (или отдать
    створке `default_db_path()`) — прогоны читаются не из поданной базы,
    красная.
    """
    import argparse

    import app.main as main_module
    from ui import backend
    from ui.models import Settings

    database = tmp_path / "по-ключу" / "candles.sqlite3"
    before = backend.current()
    monkeypatch.setattr(main_module, "_userdata_ready", lambda _folder: False)
    try:
        # Без цикла событий: до обрыва `_run` не ждёт ничего, и корутина
        # кончается первым же шагом. `asyncio.run` здесь снял бы цикл,
        # поставленный соседним тестом того же процесса.
        work = main_module._run(None, argparse.Namespace(), database)  # noqa: SLF001 — сборка окна целиком, как её зовёт `main`
        with pytest.raises(StopIteration) as finished:
            work.send(None)
        assert finished.value.value == 1, (
            "сборка не оборвалась там, где проверка её обрывает"
        )
        door = backend.current()
        assert door is not None, "створки нет вовсе"
        said = door.runs([Settings()]).trouble
    finally:
        backend.use(before)
    assert str(database) in said, (
        f"окно шаблонов спрашивает прогоны не у базы из ключа --db: {said!r}"
    )


# ------------------------------------------- B-050: окно с настройками порта

#: Программа с файлом настроек, где выбран вариант, которого робот не умеет.
#: Через полторы секунды после показа окна обвязка делает то, что сделал бы
#: человек, не открывавший настроек: сохраняет текущие как шаблон, просит
#: записи настроек в файл (`request_settings` — эхо, которое слушает запись)
#: и запускает тестер с текущими. Подмен в поведении программы нет: обёрнут
#: только `_refuse`, чтобы отказы было видно, и `QInputDialog.getText`,
#: чтобы имя шаблона ввелось без клавиатуры.
RESTORE_DRIVER = '''
import hashlib
import os
import pathlib
import sys
from datetime import datetime

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_API"] = "pyside6"

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QInputDialog

import app.port
import ui.main_window
from market import MSK
from ui.models import BacktestRequest
from ui.templates import Library
from ui.templates_dialog import TemplatesDialog

refusals = []
_original_refuse = app.port.HistoryPort._refuse


def refuse(self, event, reason):
    refusals.append(f"{event}: {reason}")
    _original_refuse(self, event, reason)


app.port.HistoryPort._refuse = refuse
QInputDialog.getText = staticmethod(lambda *_a, **_k: ("проверка", True))

settings_file = pathlib.Path(os.environ["RESTORE_SETTINGS_FILE"])
library_dir = pathlib.Path(os.environ["RESTORE_LIBRARY_DIR"])
_original_show = ui.main_window.MainWindow.show


def act(window):
    print(f"window after_take_profit={window._settings.after_take_profit.value}")
    dialog = TemplatesDialog(Library(library_dir), window._settings, window)
    dialog.save_current()
    saved = [one for one in Library(library_dir).read().templates if one.name == "проверка"]
    print("template after_take_profit="
          + (saved[0].values.after_take_profit.value if saved else "не сохранён"))
    window.port.request_settings()
    print("file md5=" + hashlib.md5(settings_file.read_bytes()).hexdigest())
    window.port.run_backtest(BacktestRequest(
        settings=window._settings,
        since=datetime(2026, 6, 19, 0, 0, tzinfo=MSK),
        until=datetime(2026, 6, 19, 23, 59, tzinfo=MSK),
        settings_source="текущие настройки",
    ))
    print("tester refused=" + str(any("Настройки не годятся" in one for one in refusals)))
    window.close()


def show(self):
    _original_show(self)
    QTimer.singleShot(1500, lambda: act(self))


ui.main_window.MainWindow.show = show

from app.main import main

raise SystemExit(main(sys.argv[1:]))
'''


@pytest.mark.slow
def test_the_window_starts_with_what_the_port_accepted(tmp_path: pathlib.Path) -> None:
    """`B-050`: окно собирается с настройками порта, а не файла.

    Поведение: файл настроек несёт «Сразу восстановить позицию», которого
    робот не умеет; порт подменяет его умолчанием. Окно, собранное `main`,
    с первой секунды держит подменённое: тестер с текущими настройками
    не отказывает «Настройки не годятся», шаблон «текущих» сохраняется без
    непринимаемого варианта. Файл владельца при этом не переписан ни байтом,
    хотя запись в него была запрошена (`request_settings` → эхо → запись
    через `for_file`) — иначе равенство байтов ничего бы не доказывало.

    ⚠️ Мутация: `app/main.py` собирает `MainWindow(settings=values)` — окно
    держит `restore`, тестер отказывает, шаблон несёт `restore`.
    """
    from app.settings_store import SettingsStore
    from ui.models import AfterTakeProfit, Settings

    database = _database(tmp_path / "candles.sqlite3")
    store = SettingsStore(tmp_path)
    assert not store.save(
        Settings(instrument="MXU6", after_take_profit=AfterTakeProfit.RESTORE_AT_ONCE)
    )
    before = hashlib.md5(store.path.read_bytes()).hexdigest()
    library = tmp_path / "library"
    library.mkdir()
    driver = tmp_path / "restore_driver.py"
    driver.write_text(RESTORE_DRIVER, encoding="utf-8")
    environment = {
        "RESTORE_SETTINGS_FILE": str(store.path),
        "RESTORE_LIBRARY_DIR": str(library),
    }
    outcome = _launch([str(driver)], ["--db", str(database)], extra=environment)
    said = outcome.stdout + outcome.stderr
    assert outcome.returncode == 0, said
    assert "window after_take_profit=stop" in outcome.stdout, (
        "окно собрано с непринимаемым вариантом из файла, а не с подменой порта:\n" + said
    )
    assert "template after_take_profit=stop" in outcome.stdout, said
    assert "tester refused=False" in outcome.stdout, (
        "тестер отказал текущим настройкам окна:\n" + said
    )
    assert f"file md5={before}" in outcome.stdout, (
        "файл владельца переписан подменой, хотя «Применить» никто не нажимал:\n" + said
    )
