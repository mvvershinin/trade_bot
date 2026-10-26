"""Запрет сети для копий программы, запущенных тестом отдельным процессом.

`B-047`. Сторож `no_internet` в `tests/conftest.py` стоит подменой внутри
процесса pytest, и ребёнок, поднятый через `subprocess`, её не наследует:
копия программы ходила на iss.moex.com по-настоящему, а тест оставался
зелёным. Этот файл подкладывает ребёнку ту же подмену.

Как доезжает: `tests/conftest.py` дописывает этот каталог в начало
`PYTHONPATH` ребёнка, а модуль с именем `sitecustomize` интерпретатор
импортирует сам при старте (модуль `site`). Включается переменной
`TERMINAL_TEST_OFFLINE_REPORT` — путём файла отчёта; без неё файл
ничего не делает, так что случайный запуск вне прогона не пострадает.

⚠️ Отказ — обычный `OSError`, а не `BaseException`, как у сторожа
в процессе pytest. Ребёнок — это программа с рабочими потоками: отказ
мимо `except Exception` убил бы поток работника данных, копия повисла бы,
и вместо внятного красного теста был бы таймаут на 180 секунд. Громкость
обеспечивает не отказ, а **отчёт**: каждая попытка дописывается в файл,
и `tests/conftest.py` роняет тест, если файл не пуст.

Имя файла задано интерпретатором (`sitecustomize`), поэтому латиница
здесь не выбор, а условие работы.
"""

from __future__ import annotations

import os
import socket
import sys
from typing import Any

_REPORT = os.environ.get("TERMINAL_TEST_OFFLINE_REPORT")

#: Та же машина — не интернет. Копия списка из `tests/conftest.py::LOOPBACK`:
#: импортировать conftest в ребёнке нельзя, он тянет pytest.
_LOCAL = frozenset({
    "",
    "0.0.0.0",
    "::",
    "::1",
    "127.0.0.1",
    "localhost",
    "localhost.localdomain",
    socket.gethostname(),
})


def _local(host: object) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("utf-8", "replace")
    if not isinstance(host, str):
        return False
    return host in _LOCAL or host.startswith("127.")


def _host_of(address: object) -> object:
    if isinstance(address, (str, bytes)):
        return None  # AF_UNIX: путь в файловой системе, а не сеть
    if isinstance(address, (tuple, list)) and address:
        return address[0]
    return address


def _refuse(report: str, where: str, target: object) -> OSError:
    line = f"pid={os.getpid()} {where} {target!r} argv={sys.argv!r}\n"
    try:
        with open(report, "a", encoding="utf-8") as sink:
            sink.write(line)
    except OSError:
        pass  # отчёт не записался — отказ ниже всё равно будет
    sys.stderr.write(f"ПРОГОН ТЕСТОВ: копия программы пошла в сеть: {line}")
    return OSError(f"сеть закрыта прогоном тестов (B-047): {where} {target!r}")


def _install(report: str) -> None:
    connect: Any = socket.socket.connect
    connect_ex: Any = socket.socket.connect_ex
    getaddrinfo: Any = socket.getaddrinfo

    def guarded_connect(self: socket.socket, address: object) -> None:
        if not _local(_host_of(address)):
            raise _refuse(report, "socket.connect", address)
        connect(self, address)

    def guarded_connect_ex(self: socket.socket, address: object) -> object:
        if not _local(_host_of(address)):
            raise _refuse(report, "socket.connect_ex", address)
        return connect_ex(self, address)

    def guarded_getaddrinfo(host: object, *rest: object, **named: object) -> object:
        if not _local(host):
            raise _refuse(report, "socket.getaddrinfo", host)
        return getaddrinfo(host, *rest, **named)

    # Таблица подмен, а не три присвоения: присвоение методу класса
    # проверка типов не пропускает, а глушить её ради подмены незачем.
    replacements: tuple[tuple[object, str, object], ...] = (
        (socket.socket, "connect", guarded_connect),
        (socket.socket, "connect_ex", guarded_connect_ex),
        (socket, "getaddrinfo", guarded_getaddrinfo),
    )
    for owner, name, value in replacements:
        setattr(owner, name, value)

if _REPORT:
    _install(_REPORT)

# Заглушка биржи — отдельно и по просьбе теста: см. `offline_iss.py`.
if os.environ.get("TERMINAL_TEST_ISS_STUB") == "1":
    # Через `importlib`: модуль лежит рядом и виден ребёнку по `PYTHONPATH`,
    # а проверке типов — нет, и глушить её ради одной строки незачем.
    import importlib

    importlib.import_module("offline_iss").install()
