r"""Собрать из результата Nuitka то, что отдаётся владельцу счёта — Linux.

Запуск после `tools/build.py` из корня рабочей копии:

    .venv/bin/python tools/linux_package.py

Что делает
----------
1. Копирует `build/main.dist` в `dist/Terminal` — то же имя папки, что и
   на Windows (`tools/windows_package.py`), для единообразия.
2. **Снимает `userdata/`**, если она осталась от прогонов на машине сборки:
   это папка с настройками и, возможно, с файлом токена — попасть в артефакт
   поставки она не имеет права ни при каких условиях.

   ⚠️ До 08.09.2026 (задача релизного CI) этого шага под Linux не было
   вовсе: `tools/build.py` собирает `build/main.dist`, и ничего не мешало
   этой папке доехать до архива, если на машине сборки хоть раз запускали
   Terminal. Windows этот шаг уже имел (`windows_package.py`), Linux — нет.
3. Кладёт рядом короткий `README.txt`: что это, как запустить, где искать
   полный список системных библиотек (он в `BUILD.md`, не дублируется
   здесь — дублирующийся текст в двух местах расходится при следующей правке).
4. Архивирует `dist/Terminal` в `dist/Terminal-{версия}-linux-x86_64.tar.gz`.

   ⚠️ **tar, не zip.** У zip нет единого гарантированного способа сохранить
   unix-бит «исполняемый» при распаковке на Linux — pip есть площадки, где
   он это делает, есть где нет. У tar бит `+x` — часть формата, сохраняется
   всегда. Без него пользователь после распаковки получил бы «Permission
   denied» на `./Terminal` и не понял бы, почему: та же ловушка, которую
   решение 0002 требует не создавать пользователю без объяснения.

Версия — из `TERMINAL_VERSION` (тот же механизм, что в `tools/build.py`
и `tools/windows_package.py`), по умолчанию "0.1.0" для локальной сборки
без тега.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import sys
import tarfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE = ROOT / "build" / "main.dist"
TARGET = ROOT / "dist" / "Terminal"

#: Тот же механизм и то же умолчание, что в `tools/build.py::APP_VERSION`.
APP_VERSION = os.environ.get("TERMINAL_VERSION", "0.1.0")

README = """\
«Терминал» — программа автоматической торговли на Московской бирже
==================================================================

Версия {version}. Устанавливать саму программу не нужно — распаковать
и запустить. Но для окна нужно несколько системных библиотек Qt, которых
в архиве нет (не тащим в поставку копию системных .so — они уже есть
на любом Linux с рабочим столом, кроме совсем голых серверных установок).


Как запустить
-------------

1. Распакуйте архив целиком, куда угодно. Папку "Terminal" разделять
   нельзя — бинарь ищет свои файлы рядом с собой.

2. В этой папке:

       chmod +x Terminal    (обычно не нужно — tar сохраняет права; на всякий случай)
       ./Terminal


Если не хватает системной библиотеки
--------------------------------------

Окно не откроется, а в терминале будет видно сообщение Qt о том, какого
файла не хватает — например:

    qt.qpa.plugin: Could not load the Qt platform plugin "xcb" ...

Список библиотек, команда установки под Debian/Ubuntu и Fedora/Arch,
и как этот список измерен — в документации проекта, файл BUILD.md,
раздел «Запустить готовую программу на Linux»:

    https://github.com/mvvershinin/trade_bot/blob/main/BUILD.md


Куда программа пишет свои данные
---------------------------------

В папку "userdata" рядом с собой — создаётся при первом запуске. Чтобы
перенести программу на другой компьютер вместе со всей историей — скопируйте
папку "Terminal" целиком. Чтобы удалить программу — удалите папку. В систему
(в $HOME/.config, в systemd, куда-то ещё) программа не пишет ничего.


Одна копия на папку данных
----------------------------

Вторая копия, запущенная на той же папке, откажется стартовать и скажет
об этом словами — это защита от двойных заявок на один сигнал по счёту,
а не недоработка.
""".format(version=APP_VERSION)


def package() -> int:
    if not SOURCE.is_dir():
        print(f"нет {SOURCE} — сначала tools/build.py", file=sys.stderr)
        return 2

    if TARGET.exists():
        shutil.rmtree(TARGET)
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SOURCE, TARGET, symlinks=True)

    # Следы прогонов на машине сборки — там настройки и, возможно, токен.
    leftovers = TARGET / "userdata"
    if leftovers.exists():
        shutil.rmtree(leftovers)
        print("снята userdata/ от прогонов на машине сборки")

    (TARGET / "README.txt").write_text(README, encoding="utf-8")

    binary = TARGET / "Terminal"
    if binary.is_file():
        binary.chmod(binary.stat().st_mode | 0o111)

    archive = ROOT / "dist" / f"Terminal-{APP_VERSION}-linux-x86_64.tar.gz"
    if archive.exists():
        archive.unlink()
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(TARGET, arcname="Terminal")

    files = [p for p in TARGET.rglob("*") if p.is_file()]
    total = sum(p.stat().st_size for p in files)
    print(f"{TARGET}: {len(files)} файлов, {total} Б = {total / 1024 / 1024:.0f} МБ")
    print(f"{archive}: {archive.stat().st_size / 1024 / 1024:.0f} МБ")
    return 0


if __name__ == "__main__":
    raise SystemExit(package())
