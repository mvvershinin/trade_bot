"""Причина «прогона не будет» — только причина прогона, а не чужой отказ.

Что стережётся, словами
-----------------------
Пока обещан отчёт о прогоне, `_refuse` запоминает причину, которой окно
объяснит «Отчёта о прогоне не будет». До правки 29.09.2026 запоминался
**любой** отказ этого времени: нажатое «Подключиться» без собранного
подключения, загрузка «занято». Окно называло чужую причину.

1. Посторонний отказ при обещанном отчёте в окно прогона не попадает.
2. Отказ самого прогона (свечей нет) — попадает: правка не глушит всё.

Сеть не участвует: база пустая, подключение не собрано.
"""

from __future__ import annotations

import asyncio
import pathlib
from collections.abc import Awaitable, Callable
from datetime import datetime

from app.port import HistoryPort
from market import MSK, MarketWorker, redact
from ui.models import BacktestRequest, Settings

FOREIGN = "не собрано"  # из отказа «Подключиться» без подключения
GENERIC = "последней строкой в журнале решений"  # текст, когда причины нет


class Probe(HistoryPort):
    """Порт с двумя ручками: обещать отчёт и сдаться, не прогоняя."""

    def promise_report(self, request: BacktestRequest) -> None:
        self._back.pending = request

    def give_up(self) -> None:
        self._forget_report()


def _request() -> BacktestRequest:
    return BacktestRequest(
        settings=Settings(),
        since=datetime(2026, 6, 19, tzinfo=MSK),
        until=datetime(2026, 6, 20, tzinfo=MSK),
    )


def _drive(
    loop: asyncio.AbstractEventLoop,
    tmp_path: pathlib.Path,
    work: Callable[[Probe], Awaitable[None]],
) -> list[str]:
    async def go() -> list[str]:
        worker = MarketWorker(tmp_path / "candles.sqlite3")
        port = Probe(worker, values=Settings(), days=0, sanitize=redact)
        told: list[str] = []
        port.backtest_refused.connect(told.append)
        try:
            await work(port)
        finally:
            await port.aclose()
            await worker.close()
        return told

    return loop.run_until_complete(go())


def test_a_foreign_refusal_is_not_named_as_the_reason_of_the_run(loop, tmp_path) -> None:
    """Стережёт 1. Мутация: запоминать в `_refuse` без `run` — причиной станет чужое."""

    async def work(port: Probe) -> None:
        port.promise_report(_request())
        port.stream(True)  # подключение не собрано: отказ вслух, но не прогона
        port.give_up()

    told = _drive(loop, tmp_path, work)

    assert told, "окну не сказано, что отчёта не будет — проверка вакуумна"
    assert FOREIGN not in told[-1], f"причиной прогона назван чужой отказ: «{told[-1]}»"
    assert GENERIC in told[-1], told[-1]


def test_the_runs_own_refusal_still_reaches_the_window(loop, tmp_path) -> None:
    """Стережёт 2. Мутация: снять `run=True` у «Свечей нет» — окно получит общий текст."""

    async def work(port: Probe) -> None:
        port.run_backtest(_request())
        port.stream(True)  # чужой отказ пришёл, пока прогон ещё не начал читать
        await port.wait()

    told = _drive(loop, tmp_path, work)

    assert told, "прогон на пустой базе не сказал окну, что отчёта не будет"
    assert GENERIC not in told[-1], f"причина прогона потеряна: «{told[-1]}»"
    assert FOREIGN not in told[-1], f"причиной прогона назван чужой отказ: «{told[-1]}»"
