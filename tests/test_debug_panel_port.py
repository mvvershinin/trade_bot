"""Лента «Отладка» со стороны порта: журнал программы, закрытый бар, отказ.

Отдельный файл по той же причине, что `test_app_observe_port.py`: здесь
нужен цикл поверх Qt, собранный так же, как в `app/main.py`.

Стерегутся три вещи:

* отказ доходит до ленты **одной** строкой уровня WARNING — не пропадает
  (мутация молчания: убрать выход в ленту из `HistoryPort.note`) и не стоит
  вторым, красным близнецом из технического лога;
* закрытый бар порт объявляет строкой без цен, и только после того, как
  попросил пересчёт;
* сломанная строка ленты в `_draw_live` не откладывает решение по бару:
  пересчёт идёт, дорисовка не падает.
"""

from __future__ import annotations

import dataclasses
import logging
import math
import os
import pathlib
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

import pytest

os.environ.setdefault("QT_API", "pyside6")  # до первого импорта qasync

from app import debug_feed
from app.debug_feed import PanelHandler
from app.port import HistoryPort
from market import MSK, Candle, CandleStore, MarketWorker, Source, Timeframe
from market.journal import redact
from ui.models import DecisionLevel
from ui.models import Settings as UiSettings

SYMBOL = UiSettings().instrument
DAY = datetime(2026, 6, 19, 7, 0, tzinfo=MSK)  # пятница, до открытия окна
READING = "Читаю свечи из базы…"


def minutes(count: int, start: datetime = DAY) -> list[Candle]:
    """Минутки с колебанием — тот же ряд, что у `test_app_observe_port.py`."""
    out: list[Candle] = []
    for index in range(count):
        price = 100000.0 + 300.0 * math.sin(index / 9.0)
        out.append(Candle(
            time=start + timedelta(minutes=index),
            open=price, high=price + 40.0, low=price - 40.0, close=price,
            volume=1.0, timeframe=Timeframe(1), filled_minutes=1,
        ))
    return out


@pytest.fixture
def database(tmp_path: pathlib.Path) -> pathlib.Path:
    """База с минутками 07:00–16:59: прогрев есть, последний бар закрыт в 17:00."""
    path = tmp_path / "live.sqlite3"
    with CandleStore(path) as store:
        store.put_minutes(SYMBOL, minutes(600), Source.ISS)
    return path


@pytest.fixture
def panel() -> PanelHandler:
    return debug_feed.install()


#: Сценарий на порту: порт, поток данных, список начатых проходов чтения базы.
Scenario = Callable[[HistoryPort, MarketWorker, list[str]], Awaitable[None]]


def on_port(
    loop, database: pathlib.Path, scenario: Scenario, events: list[str] | None = None
) -> list[str]:
    """Порт на базе; `scenario` делает своё. Возврат — начатые проходы чтения базы.

    `events` — куда сложить события журнала программы, сказанные портом.
    """
    reads: list[str] = []

    def heard(on: bool, what: str) -> None:
        if on and what == READING:
            reads.append(what)

    async def go() -> None:
        worker = MarketWorker(database)
        await worker.open()
        port = HistoryPort(worker, values=UiSettings(), days=0, sanitize=redact)
        port.busy_changed.connect(heard)
        if events is not None:
            port.decision_appended.connect(lambda row: events.append(row.event))
        try:
            await scenario(port, worker, reads)
        finally:
            await port.aclose()
            await worker.close()

    loop.run_until_complete(go())
    return reads


def test_a_refusal_reaches_the_panel_as_one_warning_line(
    loop, database, monkeypatch, panel
) -> None:
    """Прогон упал: в ленте одна строка «Прогон не удался» уровня WARNING из журнала.

    Мутации, которые это роняют: убрать `debug_feed.note` из `HistoryPort.note`
    (молчание), отдать отказ уровнем INFO, снять `extra={"panel": False}`
    у `log.exception` в `_pass` (вторая, красная строка о том же).
    """

    async def scenario(port: HistoryPort, _worker: MarketWorker, _reads: list[str]) -> None:
        async def broken(*_args: object) -> None:
            raise RuntimeError("чтение базы сломано нарочно")

        monkeypatch.setattr(port, "_series", broken)
        port.refresh("проверка")
        await port.wait()

    on_port(loop, database, scenario)
    about = [
        line for line in panel.since(0, 10_000).lines
        if "прогон по истории не удался" in line.text.lower() or "Прогон не удался" in line.text
    ]
    assert len(about) == 1, [line.text for line in about]
    assert about[0].level is DecisionLevel.WARNING
    assert about[0].source == debug_feed.SOURCE_JOURNAL
    assert "чтение базы сломано нарочно" in about[0].text


def _close_one_more_bar(
    loop, database: pathlib.Path, events: list[str] | None = None
) -> tuple[int, int]:
    """Поток включён, прогрев прошёл; дописаны минуты 17:00–17:04, порт узнал о минуте.

    Возврат: проходов чтения базы до новой минуты и всего. `events` — куда
    сложить события журнала программы.
    """
    before: list[int] = []

    async def scenario(port: HistoryPort, worker: MarketWorker, reads: list[str]) -> None:
        port.attach_stream(lambda on: None)
        port.stream(True)
        await port.wait()
        before.append(len(reads))
        fresh = [
            dataclasses.replace(candle, time=DAY + timedelta(minutes=600 + index))
            for index, candle in enumerate(minutes(5))
        ]
        await worker.put_minutes(SYMBOL, fresh, Source.BROKER)
        port.live_candle(SYMBOL)
        await port.wait()

    reads = on_port(loop, database, scenario, events)
    return before[0], len(reads)


def test_a_closed_bar_is_announced_after_the_recount_is_asked(loop, database, panel) -> None:
    """Порт объявил закрытый бар строкой без цен; следом прогон прочитал его из базы."""
    logging.getLogger().setLevel(logging.INFO)  # вернёт `pristine_logging`
    before, after = _close_one_more_bar(loop, database)
    assert after == before + 1, "закрытый бар не дал пересчёта"
    texts = [line.text for line in panel.since(0, 100_000).lines]
    due = [text for text in texts if "минуты бара до 17:05 собраны — пересчёт поставлен" in text]
    assert len(due) == 1, due
    assert " О " not in due[0], "порт назвал цены бара, которого движок мог не видеть"
    read = [text for text in texts if text.startswith(f"из базы {SYMBOL} ")]
    assert read and read[-1].endswith("по 19.06.2026 17:00"), read[-1:]


def test_a_broken_panel_line_does_not_delay_the_bar_decision(
    loop, database, monkeypatch, caplog, panel
) -> None:
    """Строка ленты сломана: пересчёт по закрытому бару всё равно идёт, дорисовка цела.

    Мутация «снять защиту» роняет: исключение из `bar_due` уходит в
    `_live_candle` («живая свеча не дорисована»), а стоя до `refresh` —
    откладывает решение по бару до следующего закрытия. Исключение из
    `bars_read` роняет весь проход («Прогон не удался»): чтение базы
    начато, а решения по бару нет — счёт чтений этого не видит.
    """

    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("строка ленты сломана нарочно")

    monkeypatch.setattr(debug_feed, "_emit", boom)
    events: list[str] = []
    before, after = _close_one_more_bar(loop, database, events)
    assert after == before + 1, "сломанная строка ленты отложила пересчёт"
    assert "Прогон не удался" not in events, "сломанная строка ленты уронила проход"
    assert not [r for r in caplog.records if "не дорисована" in r.getMessage()]
    assert debug_feed.LOSSES.count > 0
