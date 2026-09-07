r"""Подставить версию релиза в `pyproject.toml`. Только для релизного CI.

Запуск:

    .venv/bin/python tools/set_version.py 0.2.0

Зачем отдельный файл, а не `sed` прямо в workflow
--------------------------------------------------
`.github/workflows/release.yml` бежит и на `ubuntu-latest`, и на
`windows-latest`. На Windows `actions/checkout` по умолчанию кладёт текстовые
файлы с CRLF (`core.autocrlf=true` в git раннера, в репозитории нет
`.gitattributes`, который бы это отключил). `sed -E 's/^.../.../'` с `$`
в конце шаблона на CRLF-строке не совпадает: после закрывающей кавычки
остаётся символ `\\r`, и `$` (конец строки перед `\\n`) до него не дотягивается.
Замена **молча не происходит** — `sed -i` не считает это ошибкой, код
возврата 0, а `pyproject.toml` остаётся со старой версией. Ровно тот класс
дефекта, который проект требует ловить громко (`CLAUDE.md`, правило 13).

Проверено 08.09.2026: этот файл прогнан и на LF-, и на CRLF-копии реального
`pyproject.toml` — замена происходит в обоих случаях, остальные байты файла
не меняются (файл читается и пишется с `newline=""`, переводы строк не
трогаются вообще, кроме как для гарантии наличия ровно одной подмены).
На настоящем `windows-latest`-раннере не проверено — первый тег покажет.
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"

#: Ровно то же самое, чем `.github/workflows/release.yml` уже проверил тег
#: (vX.Y.Z, три числа через точку). Проверено здесь тоже — этот файл может
#: вызываться и не из workflow, и полагаться на чужую проверку не стоит.
#:
#: ⚠️ Без `$` на конце: на CRLF-файле (Windows-раннер, см. докстринг модуля)
#: после закрывающей кавычки стоит `\r`, и `$` в MULTILINE-режиме матчит
#: только перед `\n` — с `$` в шаблоне подмена находила 0 совпадений на
#: каждом прогоне, тихо или громко в зависимости от проверки `count`, но
#: не работала. Без `$` шаблон матчит саму кавычку и останавливается —
#: то, что после неё (`\r` или ничего), в замену не входит и не портится.
#: Проверено 08.09.2026 на LF- и CRLF-копиях реального pyproject.toml.
_PATTERN = re.compile(r"^version = \"[^\"]*\"", re.MULTILINE)


def set_version(new_version: str) -> int:
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", new_version):
        print(f"«{new_version}» не похоже на X.Y.Z — останавливаюсь", file=sys.stderr)
        return 2

    # ⚠️ Не `Path.read_text()`/`write_text()`: на Python 3.12 у них нет
    # параметра `newline` (появился только в 3.13, а проект закреплён на
    # 3.12 — `requires-python` в pyproject.toml). Без него текстовый режим
    # молча переводит все переводы строк в `\n` при чтении и в системные
    # при записи — на Linux-раннере это ничего не портит, а на Windows-
    # раннере превратило бы CRLF-файл в LF, то есть переписало бы куда
    # больше байт, чем одну строку версии. Найдено прогоном 08.09.2026.
    with open(PYPROJECT, encoding="utf-8", newline="") as fh:
        text = fh.read()
    replacement = f'version = "{new_version}"'
    new_text, count = _PATTERN.subn(replacement, text, count=1)
    if count != 1:
        print(
            f"строка 'version = \"...\"' не найдена в {PYPROJECT} ровно один раз "
            f"(найдено {count}) — формат файла изменился, правь шаблон",
            file=sys.stderr,
        )
        return 2

    with open(PYPROJECT, "w", encoding="utf-8", newline="") as fh:
        fh.write(new_text)
    print(f"{PYPROJECT}: version -> {new_version}")
    return 0


_EXPECTED_ARGC = 2  # имя скрипта + версия

if __name__ == "__main__":
    if len(sys.argv) != _EXPECTED_ARGC:
        print("использование: set_version.py X.Y.Z", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(set_version(sys.argv[1]))
