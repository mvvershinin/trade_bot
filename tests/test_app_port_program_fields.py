"""D-117: числа программы доезжают до снимка прогона из окна.

`ProgramFields` (остановка перед экспирацией и глубина загрузки) заведён
в `app/runs.py`; здесь стережётся **проводка**: каждое место, где порт
пишет прогон или собирает отчёт, передаёт числа из окна. Три места —
три проверки, и каждая ломается своей мутацией: убрать `program=`
из `_replay` (прогон по истории), из записи живого хода и из отчёта тестера.

Числа — не умолчания (3 и 17 против 1 и 90): с умолчаниями проверка
зеленела бы и при числах, взятых не из окна.

Изоляция: своя база во временном каталоге, свой порт, сети нет; подключение
к брокеру — заглушка. Каждую проверку можно гонять поодиночке.
"""

from __future__ import annotations

import math
import os
import pathlib
from datetime import date, datetime, timedelta

import pytest

os.environ.setdefault("QT_API", "pyside6")  # до первого импорта qasync

from app.port import HistoryPort
from market import MSK, Candle, CandleStore, ContractRow, MarketWorker, RunOrigin, Source, Timeframe
from market.journal import redact
from ui.models import BacktestRequest, Settings

DAY = datetime(2026, 10, 5, 7, 0, tzinfo=MSK)  # понедельник, до открытия окна
SYMBOL = "MXZ6"
HALT, DEPTH = 3, 17
EXPECTED = (
    f"Остановка перед экспирацией, дней: {HALT}",
    f"Глубина загрузки истории, дней: {DEPTH}",
)


def _values() -> Settings:
    return Settings(instrument=SYMBOL, expiry_halt_days=HALT, history_depth_days=DEPTH)


@pytest.fixture
def database(tmp_path: pathlib.Path) -> pathlib.Path:
    path = tmp_path / "program.sqlite3"
    minutes = [
        Candle(
            time=DAY + timedelta(minutes=index),
            open=100000.0 + 300.0 * math.sin(index / 9.0),
            high=100040.0 + 300.0 * math.sin(index / 9.0),
            low=99960.0 + 300.0 * math.sin(index / 9.0),
            close=100000.0 + 300.0 * math.sin(index / 9.0),
            volume=1.0, timeframe=Timeframe(1), filled_minutes=1,
        )
        for index in range(300)
    ]
    with CandleStore(path) as store:
        store.put_minutes(SYMBOL, minutes, Source.ISS)
        store.put_contracts(
            [ContractRow(SYMBOL, last_trade_day=date(2026, 12, 17),
                         active_from=date(2026, 9, 17))],
            now=DAY,
        )
    return path


def _with_port(loop, database: pathlib.Path, work):
    async def go():
        worker = MarketWorker(database, sanitize=redact)
        await worker.open()
        port = HistoryPort(worker, values=_values(), days=0, sanitize=redact,
                           clock=lambda: DAY + timedelta(hours=5))
        port.attach_stream(lambda on: None)
        try:
            return await work(port)
        finally:
            await port.aclose()
            await worker.close()

    return loop.run_until_complete(go())


def _sessions(database: pathlib.Path):
    with CandleStore(database) as store:
        return list(store.journal_sessions(limit=99).rows)


def _carries(text: str) -> bool:
    return all(line in text for line in EXPECTED)


def test_a_history_run_records_the_program_numbers(loop, database) -> None:
    """Прогон по истории из окна пишет в снимок числа программы из окна."""

    async def work(port):
        port.refresh("тест")
        await port.wait()

    _with_port(loop, database, work)
    runs = [one for one in _sessions(database) if one.origin is RunOrigin.BACKTEST]
    assert runs, "прогон по истории не записан"
    assert all(_carries(one.settings) for one in runs), runs[0].settings


def test_a_live_ride_records_the_program_numbers(loop, database) -> None:
    """Живой ход пишет в снимок числа программы из окна."""

    async def work(port):
        port.stream(True)
        await port.wait()
        port.stream(False)
        await port.wait()

    _with_port(loop, database, work)
    live = [one for one in _sessions(database) if one.origin is not RunOrigin.BACKTEST]
    assert live, "живой ход не записан — проверять нечего"
    assert all(_carries(one.settings) for one in live), live[0].settings


def test_the_tester_report_carries_the_program_numbers(loop, database) -> None:
    """Отчёт тестера показывает числа программы в тексте настроек."""
    reports: list = []

    async def work(port):
        port.backtest_finished.connect(reports.append)
        port.run_backtest(BacktestRequest(
            settings=_values(),
            since=DAY.replace(hour=0),
            until=DAY + timedelta(days=1),
            settings_source="текущие настройки",
        ))
        await port.wait()

    _with_port(loop, database, work)
    assert reports, "отчёта нет"
    assert _carries(reports[-1].settings_text), reports[-1].settings_text
