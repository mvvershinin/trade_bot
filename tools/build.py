"""Сборка портативной поставки: Nuitka standalone. Решение 0001.

Запуск из корня рабочей копии:

    .venv/bin/python tools/build.py            # обычная сборка
    .venv/bin/python tools/build.py --dry-run  # только показать команду

Что здесь намеренно НЕ делается
-------------------------------
* `--onefile` не используется ни при каких условиях: он самораспаковывается
  во временную папку и возвращает часть проблемы с эвристиками антивирусов
  (решение 0001). Только `--standalone`.
* UPX не применяется нигде (решение 0002, п. 2).
* Имя исполняемого файла закреплено (`Terminal`) и от релиза к релизу
  не меняется: меняющееся имя выглядит для репутационных механизмов
  как новая неизвестная программа (решение 0002, п. 3).

QtWebEngine
-----------
Из программы удалён 09.09.2026 вместе с веб-графиком (решение 0056), и ключа
`--with-web` больше нет. Имена его модулей остались в `QT_UNUSED` ниже —
не как след, а по той же причине, что и остальные сорок: PySide6 везёт весь Qt,
и всё, чего программа не импортирует, надо назвать вслух, иначе Nuitka положит
его в поставку. Цена этих четырёх строк измерена: `QtWebEngineCore` тянет
за собой 298 МБ (`.docs/packaging/build-recon-2026-09-05-1624.md` §2.3).

Linux
-----
Нужен `patchelf`: без него Nuitka отказывается в standalone-режиме сразу.
Ставится в то же виртуальное окружение колесом с PyPI, в систему
ничего не попадает.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import platform
import shutil
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENTRY = ROOT / "app" / "main.py"
OUT = ROOT / "build"

#: Папка примеров наборов настроек (решение 0051). Едет в поставку рядом
#: с исполняемым файлом и оттуда же читается: `ui.templates.examples_dir`
#: берёт каталог `sys.executable`. Не попала в сборку — кнопка «взять
#: из примеров» показывает пустоту, и отказ этот **тихий**: программа
#: работает, примеров просто нет. Поэтому отсутствие папки в дереве —
#: повод сказать вслух, а не собрать молча (см. `main`).
EXAMPLES = "examples"

#: Имя исполняемого файла. Латиницей и неизменно — см. шапку модуля.
NAME = "Terminal"

#: Число, которое едет в свойства файла на Windows (`--file-version`,
#: `--product-version` ниже) — это диалог «Свойства → Подробно» в Проводнике,
#: и только он. Релизный workflow (`.github/workflows/release.yml`) вычисляет
#: его из тега (`v0.2.0` → `0.2.0`) и передаёт через переменную окружения;
#: локальная сборка без тега получает то же число, что записано в
#: `pyproject.toml` на этот день.
#:
#: ⚠️ Это НЕ версия, которую видит владелец счёта в заголовке окна и в
#: журнале решений (`ui.version.version()`, шапка окна, «О программе»).
#: Та версия печётся Nuitka В МОМЕНТ КОМПИЛЯЦИИ: `ui/version.py` вызывает
#: `importlib.metadata.version("terminal")` с литеральной строкой, и Nuitka
#: 4.1.3 умеет распознать такой вызов и подставить результат константой,
#: если на машине сборки в этот момент установлен пакет "terminal" нужной
#: версии — то есть ответ зависит от того, что лежит в `pyproject.toml`
#: и что успел проиндексировать `uv sync`/`pip install -e .` ДО запуска
#: Nuitka, а не от ключей командной строки самой Nuitka. Проверено замером
#: 08.09.2026 на отдельном пробнике (три сборки, версии 9.9.9 → 9.9.10 через
#: прямую правку METADATA → 9.9.11 через переустановку): буквальный литерал
#: печётся, версия из переменной — нет, `metadata.distribution(...)` вместо
#: `metadata.version(...)` — тоже нет. Отсюда обе половины фикса: этот файл
#: меняет только то, что видно в свойствах .exe, а окно и журнал правит
#: релизный workflow — правкой `pyproject.toml` и повторной установкой
#: пакета перед сборкой, с проверкой результата тем же вызовом. Разбор —
#: `.docs/packaging/packaging-expert-release-2026-09-08-*.md`.
APP_VERSION = os.environ.get("TERMINAL_VERSION", "0.1.0")

#: Модули Qt, которых в программе нет. Замер импортов по дереву 05.09.2026:
#: используются только QtWidgets, QtCore, QtGui. Всё остальное — вес
#: без применения. С 09.09.2026 сюда безусловно входит и семейство WebEngine:
#: исключений из этого списка больше нет ни при каких ключах.
QT_UNUSED = (
    "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQml", "PySide6.QtCharts",
    "PySide6.QtDataVisualization", "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput", "PySide6.Qt3DLogic", "PySide6.QtBluetooth", "PySide6.QtNfc",
    "PySide6.QtPositioning", "PySide6.QtSensors", "PySide6.QtSerialPort", "PySide6.QtSerialBus",
    "PySide6.QtTest", "PySide6.QtSql", "PySide6.QtDesigner", "PySide6.QtHelp",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtScxml", "PySide6.QtSpatialAudio",
    "PySide6.QtStateMachine", "PySide6.QtTextToSpeech", "PySide6.QtRemoteObjects",
    "PySide6.QtWebSockets", "PySide6.QtWebChannel", "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick", "PySide6.QtWebView",
    "PySide6.QtHttpServer", "PySide6.QtGraphs", "PySide6.QtLocation", "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets", "PySide6.QtUiTools", "PySide6.QtSvgWidgets",
)

#: Пакеты, чьи импорты Nuitka разобрать НЕ МОЖЕТ и молчит об этом.
#:
#: `websockets` 17.x поднимает подмодули отложенно — через `__getattr__`
#: модуля и `importlib.import_module` с именем, которое собирается в строку
#: (`websockets/imports.py`). Nuitka видит только `websockets`,
#: `websockets.imports` и `websockets.version`, а `websockets.asyncio.client`
#: в сборку не попадает. Отказ **не при сборке, а у владельца счёта**
#: при нажатии «Подключиться»: замер 05.09.2026 на собранном пробнике —
#: `websockets.connect` → `ModuleNotFoundError: No module named
#: 'websockets.asyncio'`, `websockets.exceptions` → `AttributeError`.
#: Из исходников оба работают. Поток котировок (Э1-5) без этой строки
#: в поставке мёртв целиком.
LAZY_PACKAGES = ("websockets",)

#: Оснастка разработчика. В поставке ей делать нечего, но она стоит
#: в том же окружении и утягивается по цепочке импортов.
DEV_ONLY = ("pytest", "_pytest", "mypy", "ruff", "nuitka", "setuptools", "pip", "tkinter")


def command(*, jobs: int) -> list[str]:
    """Полная команда сборки. Собрана здесь, чтобы её можно было прочитать."""
    argv = [
        sys.executable, "-m", "nuitka",
        "--standalone",                    # НЕ --onefile, решение 0001
        "--enable-plugin=pyside6",
        "--assume-yes-for-downloads",
        # ⚠️ Ключ снимает предохранитель Nuitka: обращение к исключённому
        # модулю (`find_spec`, `import` в try/except) у неё по умолчанию
        # ПОДНИМАЕТ ImportError «actively excluded from Nuitka compilation»
        # вместо обычного `None`/`ModuleNotFoundError`, и такое исключение
        # уносит главное окно. Замер 05.09.2026: без ключа программа падала
        # на `ui/chart/factory.py` пустым экраном — веб-график спрашивал
        # у системы импорта, есть ли QtWebEngine.
        #
        # Тот вызов ушёл 09.09.2026 вместе с веб-графиком, и известного
        # потребителя у ключа сегодня нет. Ключ **оставлен**: он ничего
        # не добавляет в поставку и ничего не весит, а снимать его —
        # значит менять поведение собранной программы при исключённом
        # модуле, и проверяется это только настоящей сборкой с запуском,
        # а не прогоном тестов. Отдельной задачей, с замером (`D-104`).
        "--no-deployment-flag=excluded-module-usage",
        f"--output-dir={OUT}",
        f"--output-filename={NAME}",
        f"--jobs={jobs}",
        "--company-name=Terminal",
        "--product-name=Terminal",
        f"--file-version={APP_VERSION}",
        f"--product-version={APP_VERSION}",
        f"--report={OUT / 'report.xml'}",
        "--remove-output",
    ]
    argv += [f"--include-package={name}" for name in LAZY_PACKAGES]
    argv += [f"--nofollow-import-to={name}" for name in (*QT_UNUSED, *DEV_ONLY)]
    if platform.system() == "Windows":
        argv += _windows_flags()
    # Ключ ставится, только если папка есть: Nuitka на несуществующий каталог
    # данных отказывается собирать вовсе, а ронять сборку из-за примеров
    # неправильно — программа без них полноценна. Пропажу ловит тест, а не
    # молчание: `tests/test_ui_templates.py`.
    if (ROOT / EXAMPLES).is_dir():
        argv.append(f"--include-data-dir={EXAMPLES}={EXAMPLES}")
    argv.append(str(ENTRY.relative_to(ROOT)))
    return argv


def _windows_flags() -> list[str]:
    """Ключи, которые нужны только под Windows.

    `--mingw64` — требование решения 0001: сборка через MSVC линкуется
    с `vcruntime140.dll`/`msvcp140.dll`, а их на чистой Windows нет —
    они приезжают с «Распространяемым пакетом Visual C++», который
    надо ставить. Обещание «ничего не устанавливается» тогда перестаёт
    быть правдой. MinGW-w64 линкуется с `msvcrt.dll`, а он часть системы
    с девяностых.

    ⚠️ Ключ снимает зависимость **нашего** кода, но не чужого:
    `python312.dll` собран Microsoft'ом через MSVC и `vcruntime140.dll`
    требует всё равно. Nuitka кладёт его в папку поставки рядом —
    это проверяется замером зависимостей, а не верой.

    `--windows-console-mode=attach` — окно консоли не создаётся при запуске
    мышкой, но если программу позвали из консоли (`--fetch`, `--runs`),
    вывод идёт в неё. Вариант `disable` убил бы вывод консольных команд,
    `force` показывал бы владельцу счёта чёрное окно рядом с программой.
    """
    flags = ["--mingw64", "--windows-console-mode=attach"]
    # Иконка собирается кодом (`tools/make_icon.py`), а не лежит в дереве
    # двоичным файлом. Генерация здесь, а не «если файл есть»: молчаливая
    # сборка без иконки — это значок неизвестного приложения на рабочем
    # столе владельца счёта, и заметят это уже после выпуска.
    icon = ROOT / "tools" / "terminal.ico"
    if not icon.is_file():
        # Отдельным процессом, а не импортом: `tools/` пакетом не является,
        # и `import make_icon` разрешается по-разному в зависимости от того,
        # чем позвали `build.py`. Генератор дешёвый, вызывается раз на сборку.
        # ⚠️ Режим UTF-8 подпроцессу — обязателен, а не для красоты.
        # Замер 08.09.2026 на теге v0.1.0: `make_icon.py` печатает размер
        # строкой с русской «Б», Python на Windows отдаёт stdout в кодировке
        # консоли (там cp1252), и генератор иконки падал с UnicodeEncodeError,
        # роняя сборку за 26 секунд до начала компиляции. В CI это же включено
        # на уровне задания, здесь — чтобы сборка работала и у человека,
        # запустившего `build.py` руками на своей Windows.
        environment = dict(os.environ, PYTHONUTF8="1")
        subprocess.check_call(
            [sys.executable, str(ROOT / "tools" / "make_icon.py")],
            env=environment,
        )
    flags.append(f"--windows-icon-from-ico={icon}")
    return flags


def _patchelf() -> str | None:
    """`patchelf` рядом с интерпретатором окружения или в PATH.

    Первое важнее: колесо с PyPI кладёт его в `.venv/bin`, а `.venv/bin`
    в PATH оболочки обычно нет — программу запускают полным путём.
    """
    near = pathlib.Path(sys.executable).parent / "patchelf"
    if near.is_file():
        return str(near)
    return shutil.which("patchelf")


def _environment(tool: str | None) -> dict[str, str] | None:
    """Окружение для Nuitka: `patchelf` обязан быть **в PATH**, а не просто быть.

    ⚠️ Проверка выше находила `patchelf` в `.venv/bin` и пропускала сборку,
    а Nuitka искала его своим `shutil.which` — то есть в PATH, где `.venv/bin`
    обычно нет: программу запускают полным путём, не активируя окружение.
    Проверка зеленела, сборка падала строкой «requires patchelf to be
    installed». Замер 06.09.2026: сборка под Linux упала за 1 секунду ровно
    так, при установленном `patchelf==0.19.1.0` в том же окружении.

    Это был дефект самой проверки: она отвечала на вопрос «есть ли файл»,
    а нужен был «найдёт ли его тот, кто будет искать».
    """
    if tool is None:
        return None
    env = dict(os.environ)
    folder = str(pathlib.Path(tool).parent)
    env["PATH"] = folder + os.pathsep + env.get("PATH", "")
    return env


def main() -> int:
    parser = argparse.ArgumentParser(description="Портативная сборка «Терминала»")
    parser.add_argument("--jobs", type=int, default=4,
                        help="потоков компиляции C (по умолчанию 4)")
    parser.add_argument("--dry-run", action="store_true", help="показать команду и выйти")
    args = parser.parse_args()

    tool = _patchelf() if platform.system() == "Linux" else None
    if platform.system() == "Linux" and tool is None:
        print("patchelf не найден: Nuitka откажется собирать standalone. "
              "Поставить в окружение: pip install patchelf==0.19.1.0", file=sys.stderr)
        return 2
    if not (ROOT / EXAMPLES).is_dir():
        # Предупреждение, а не отказ: без примеров программа работает.
        # Но собрать поставку, где кнопка «взять из примеров» показывает
        # пустоту, и не сказать об этом — значит выпустить тихую пропажу.
        print(f"⚠️ нет папки {EXAMPLES}/ — примеры наборов настроек в поставку "
              "не попадут", file=sys.stderr)

    argv = command(jobs=args.jobs)
    print(" ".join(argv), flush=True)
    if args.dry_run:
        return 0

    started = time.monotonic()
    code = subprocess.call(argv, cwd=ROOT, env=_environment(tool))
    print(f"\nсборка заняла {time.monotonic() - started:.0f} с, код возврата {code}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
