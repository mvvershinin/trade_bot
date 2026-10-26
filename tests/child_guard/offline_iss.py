"""Заглушка биржи для копии программы, запущенной тестом отдельным процессом.

`B-047`. Копия программы при старте сама догружает пропущенный отрезок
с MOEX ISS (`market/worker.py`, `IssClient()` по умолчанию), и отключить
это ключом нельзя. Тесту окна или снимка биржа не нужна: ему нужно,
чтобы программа поднялась и закрылась. Заглушка отвечает отказом
транспорта сразу — тем же `IssTransportError`, каким программа встречает
лежащую сеть, — и в сеть не идёт никто.

Включается переменной `TERMINAL_TEST_ISS_STUB=1`, которую тест ставит
ребёнку сам, **поимённо**: общая заглушка на все подпроцессы спрятала бы
следующий тест, которому биржа нужна по-настоящему. Подключает модуль
`sitecustomize.py` рядом.
"""

from __future__ import annotations

import market.iss

#: Переменная, которой тест просит заглушку для своего ребёнка.
ISS_STUB_ENV = "TERMINAL_TEST_ISS_STUB"

#: Что подменяется: единственное место слоя, открывающее сокет.
REPLACED = "HttpxTransport"


class OfflineTransport:
    """Транспорт, не открывающий сокетов: каждый запрос — отказ сети."""

    def __init__(self, client: object | None = None) -> None:
        del client  # подпись та же, что у `HttpxTransport`

    def get(self, url: str, *, timeout: float) -> bytes:
        del timeout
        raise market.iss.IssTransportError(
            f"заглушка прогона тестов: биржа не спрашивалась ({url})"
        )


def install() -> None:
    """Подменить транспорт по умолчанию. Позже `IssClient()` возьмёт этот."""
    setattr(market.iss, REPLACED, OfflineTransport)
