"""Где лежит то, что программа создаёт сама.

[Решение 0003](../.docs/decisions/0003-runtime-data-location.md): всё —
база свечей, журналы, технический лог, файл с токеном — в одной папке
`userdata/` рядом с программой. Папка целиком закрыта якорным правилом
`/userdata/` в `.gitignore`.

Наивное «рядом с исполняемым файлом» в поставке не работает, и отказ тихий:

* **AppImage** монтирует свой образ только на чтение и запускает программу
  изнутри монтирования. `__file__` и `sys.executable` указывают внутрь него,
  `mkdir` даст «read-only file system». Якорь — переменная `APPIMAGE`
  с абсолютным путём самого файла `.AppImage`. `APPDIR` — это монтирование,
  брать его нельзя.
* **Портатив Nuitka** — модули слинкованы в бинарь, `__file__` каталога
  поставки не даёт. Якорь — каталог `sys.executable`.
* **Из исходников** — корень рабочей копии.

Модуль живёт в `market/`, а не в `app/`, по направлению зависимостей
(ARCHITECTURE.md §2): `market/` не имеет права импортировать `app/`,
а `app/` импортирует `market/` свободно и переиспользует это же место.
"""

from __future__ import annotations

import os
import pathlib
import sys
from typing import TextIO

__all__ = [
    "DB_FILE_NAME",
    "TEST_RUN_ENV",
    "USERDATA_ENV",
    "default_db_path",
    "ensure_userdata_dir",
    "ignored_override",
    "say_ignored_override",
    "userdata_dir",
]

#: Одна база на всё: свечи, журнал сделок, журнал решений (ARCHITECTURE.md §7).
DB_FILE_NAME = "candles.sqlite3"

#: Переменная окружения, переносящая папку данных целиком: база, журналы,
#: технический лог, отметка единственной копии. Пустое значение — как нет.
#:
#: Заведена под `B-047`: прогон тестов запускает копии программы отдельным
#: процессом, и подмены внутри pytest до них не доезжают — без переменной
#: ребёнок писал технический лог в `userdata/logs/` владельца счёта.
#: Окружение наследуется, довод командной строки — нет. Выставляет её
#: `tests/conftest.py` только **ребёнку**: сам процесс pytest читает
#: настоящую базу для сверки с эталоном, и переносить её нельзя.
USERDATA_ENV = "TERMINAL_USERDATA"

#: Признак тестового прогона: без него `USERDATA_ENV` не действует.
#:
#: ⚠️ Ревью 27.09.2026, находка 9. Одна переменная действовала и в поставке:
#: случайно оставленная в окружении, она увела бы программу в другую папку —
#: к базе **без стоящей остановки** робота (`robot_halt`) и к другому замку
#: «одна копия». Теперь папка переносится, только когда выставлены обе
#: переменные и программа запущена из исходников. Выставляет обе
#: `tests/conftest.py::child_environment`, и больше никто.
TEST_RUN_ENV = "TERMINAL_TEST_RUN"


def _is_frozen() -> bool:
    """Программа запущена из собранной поставки, а не из исходников."""
    return bool(getattr(sys, "frozen", False)) or "__compiled__" in globals()


def _test_override() -> str:
    """Папка из `USERDATA_ENV`, если ей можно верить; иначе пусто.

    Верить можно только в тестовом прогоне из исходников: признак
    `TEST_RUN_ENV` равен «1», поставка не собранная и не AppImage.
    """
    named = os.environ.get(USERDATA_ENV, "")
    if not named or os.environ.get(TEST_RUN_ENV) != "1":
        return ""
    if _is_frozen() or os.environ.get("APPIMAGE"):
        return ""
    return named


def ignored_override(database: pathlib.Path | None = None) -> str:
    """Фраза для журнала, если `USERDATA_ENV` выставлена, но не действует.

    Пусто — переменной нет либо она действует (тестовый прогон). У `market/`
    журнала нет: строку пишет `app/` при запуске (правило 13 — отказ,
    не доехавший до человека, равен поломке).

    :param database: база, с которой работает этот запуск, если её назвали
        ключом (`--db`, `--database`). Лежит она не в папке данных — фраза
        называет её, а не папку: «база берётся из папки» была бы неправдой
        ровно там, где человек ищет, куда делись его данные.
    """
    named = os.environ.get(USERDATA_ENV, "")
    if not named or _test_override():
        return ""
    standard = _standard_dir()
    where = (
        f"база, журналы и остановка робота берутся из {standard}."
        if database is None or database.resolve().parent == standard
        else f"папка данных программы — {standard}, а база этого запуска "
             f"названа ключом: {database}."
    )
    return (
        f"В окружении задана переменная {USERDATA_ENV}={named}. Она нужна "
        f"только прогону тестов и в обычном запуске не действует: {where}"
    )


def say_ignored_override(
    database: pathlib.Path | None = None, *, out: TextIO | None = None
) -> str:
    """Сказать `ignored_override` в поток ошибок консольного запуска. `D-124`.

    Для точек входа без окна — `--fetch`, `python3 -m app.fetch`,
    `python -m backtest`: у них нет журнала решений, и до 28.09.2026
    переменная игнорировалась в них молча. Окно говорит ту же фразу
    своим журналом (`app/main.py`).

    В stderr, а не в stdout: выдачу `backtest` перенаправляют в файл
    таблицей, и чужая строка в ней испортила бы таблицу.

    Возвращает сказанное — пусто, если говорить нечего.
    """
    line = ignored_override(database)
    if line:
        (out or sys.stderr).write(line + "\n")
    return line


def userdata_dir() -> pathlib.Path:
    """Папка `userdata/` для текущего способа запуска.

    Переменная `USERDATA_ENV` главнее способа запуска **только в тестовом
    прогоне** (`TEST_RUN_ENV`, `_test_override`). Иначе она не действует,
    а сказать об этом — `ignored_override`.
    """
    named = _test_override()
    if named:
        return pathlib.Path(named).resolve()
    return _standard_dir()


def _standard_dir() -> pathlib.Path:
    """Папка данных по способу запуска, без тестовой подмены."""
    appimage = os.environ.get("APPIMAGE")
    if appimage:
        return pathlib.Path(appimage).resolve().parent / "userdata"
    if _is_frozen():
        return pathlib.Path(sys.executable).resolve().parent / "userdata"
    # Из исходников: корень рабочей копии — родитель пакета `market`.
    return pathlib.Path(__file__).resolve().parent.parent / "userdata"


def default_db_path() -> pathlib.Path:
    """Путь к единственному файлу-базе."""
    return userdata_dir() / DB_FILE_NAME


def ensure_userdata_dir(path: pathlib.Path | None = None) -> pathlib.Path:
    """Создать папку и убедиться, что в неё можно писать.

    Проба на запись обязательна: если путь уедет на файловую систему только
    для чтения или во временную, отказ будет не падением, а молчаливой
    потерей — база свечей и журнал сделок исчезнут при выходе.

    Видимый запасной вариант (строка в журнале и сообщение в окне) —
    ответственность `app/` при старте: у `market/` нет ни окна, ни журнала
    запуска. Здесь — только честный отказ вместо тихой записи в никуда.
    """
    path = path or userdata_dir()
    path.mkdir(parents=True, exist_ok=True)
    probe = path / ".write-probe"
    try:
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as error:
        raise OSError(
            f"в папку {path} нельзя писать ({error}). База свечей и журналы "
            "не сохранятся — запускать программу в таком виде нельзя"
        ) from error
    return path
