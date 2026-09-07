"""Файл в папке данных: атомарная запись, права как у соседа с токеном.

Правило одно на всю папку `userdata/`, и файлов в ней стало больше одного:
настройки (`app/settings_store.py`) и библиотека шаблонов
(`ui/templates.py`). Две записи «почти атомарно» разошлись бы молча, а цена
у обрыва записи одна и та же — набор, который человек подбирал вечером.

Почему модуль лежит в `ui/`
---------------------------
Из двух его читателей один — окно, а `ui/` не имеет права импортировать `app/`
(ARCHITECTURE.md §2). Тот же довод, что у `ui/settings_codec.py`
и у `market/paths.py`: общее место — самый нижний из слоёв, которые обоим
доступны. Ничего от интерфейса здесь нет: модуль умеет записать текст в файл.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Final

__all__ = ["FILE_MODE", "write_atomically"]

#: Права нового файла. Не шире, чем у соседа с токеном: секретов в настройках
#: нет, но файл создаётся в той же папке теми же руками, и одно правило
#: на папку проще держать, чем два.
FILE_MODE: Final[int] = 0o600


def write_atomically(path: Path, body: str) -> None:
    """Записать текст: временный файл рядом, потом подмена на месте.

    Обрыв питания в середине оставляет прежний файл целым — потерять набор
    настроек из-за выключенного света нельзя.

    Отказы файловой системы (`OSError`) **пробрасываются**: что о них сказать
    человеку, знает вызывающий — у настроек и у библиотеки шаблонов это разные
    фразы. Временный файл при любом исходе не остаётся.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".", suffix=".tmp")
    try:
        _restrict(handle)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _restrict(handle: int) -> None:
    """Сузить права нового файла, пока он ещё пуст.

    ⚠️ `os.fchmod` есть не везде: на Windows его нет вовсе (`Availability: Unix`,
    поддержка появилась только в Python 3.13, а программа собрана под 3.12),
    а на exFAT и NTFS с флешки он отвечает `EPERM` — прав POSIX там нет.
    Ни то ни другое не повод не сохранить файл: секрета в нём нет, а поставка
    портативная и живёт в том числе на флешке. Тот же спуск, что у файла
    токена (`broker/token_store.py`).
    """
    chmod = getattr(os, "fchmod", None)
    if chmod is None:
        return
    try:
        chmod(handle, FILE_MODE)
    except OSError:
        return
