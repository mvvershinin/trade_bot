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

__all__ = ["userdata_dir", "default_db_path", "ensure_userdata_dir", "DB_FILE_NAME"]

#: Одна база на всё: свечи, журнал сделок, журнал решений (ARCHITECTURE.md §7).
DB_FILE_NAME = "candles.sqlite3"


def _is_frozen() -> bool:
    """Программа запущена из собранной поставки, а не из исходников."""
    return bool(getattr(sys, "frozen", False)) or "__compiled__" in globals()


def userdata_dir() -> pathlib.Path:
    """Папка `userdata/` для текущего способа запуска."""
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
