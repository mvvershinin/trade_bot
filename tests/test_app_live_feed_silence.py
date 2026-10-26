"""Брокер открыл поток и молчит — это говорится вслух (правило 13, 29.09.2026).

Что стережётся, словами
-----------------------
1. **Молчание брокера не молчит у нас.** Сокет открыт, снимков нет дольше
   `FIRST_SNAPSHOT_WAIT` — в журнал уходит строка с кодом и сроком,
   и поток переподключается сам, а не ждёт вечно. Замер: журнал владельца
   счёта 29.09.2026 14:38:46 — «сокет открыт, ждём первый снимок» по
   истёкшему MXU6, и дальше ни одной строки.
2. **Срок — только на первый снимок.** Поток, получивший снимок и потом
   затихший (нет сделок), этим отказом не рвётся.

Сокета и брокера нет: `candle_stream` подменён сценарием заходов.
"""

from __future__ import annotations

import asyncio
import pathlib

from app import live_feed
from market import MarketWorker
from tests.test_app_live_feed import (
    FakeSocket,
    FakeStream,
    Heard,
    feed_of,
    probe_snapshots,
    settle,
    wire,
)


def test_a_silent_broker_is_said_aloud_and_the_stream_reconnects(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """Стережёт 1: молчащий брокер — строка в журнал и переподключение.

    Мутация молчания: срок первого снимка убран или отказ по нему
    не доходит до журнала — строки нет, тест падает.
    """
    monkeypatch.setattr(live_feed, "FIRST_SNAPSHOT_WAIT", 0.05)
    heard, worker = Heard(), MarketWorker(tmp_path / "live.sqlite3")
    stream = FakeStream(FakeSocket(), FakeSocket())
    feed = feed_of(monkeypatch, stream, worker, heard)

    async def scenario() -> None:
        await worker.open()
        try:
            feed.start()
            await settle(lambda: len(stream.opened) >= 2, "повторное подключение")
        finally:
            feed.request_stop()
            await feed.aclose()
            await worker.close()

    asyncio.run(scenario())
    said = [text for text in heard.warnings() if "ни одного снимка" in text]
    assert said, f"молчание брокера не сказано в журнал: {heard.said}"
    assert "MXU6" in said[0] and "торгуется" in said[0], said[0]
    assert len(said) == 1, f"одна и та же строка повторена: {said}"


def test_a_stream_that_got_a_snapshot_is_not_torn_by_the_first_snapshot_limit(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """Стережёт 2: после первого снимка тишина рынка — не отказ."""
    monkeypatch.setattr(live_feed, "FIRST_SNAPSHOT_WAIT", 0.05)
    heard, worker = Heard(), MarketWorker(tmp_path / "live.sqlite3")
    socket = FakeSocket([wire(probe_snapshots()[0])])
    stream = FakeStream(socket)
    feed = feed_of(monkeypatch, stream, worker, heard)

    async def scenario() -> None:
        await worker.open()
        try:
            feed.start()
            await settle(lambda: socket.delivered == 1, "первый снимок")
            await asyncio.sleep(0.2)  # вчетверо дольше срока первого снимка
        finally:
            feed.request_stop()
            await feed.aclose()
            await worker.close()

    asyncio.run(scenario())
    assert len(stream.opened) == 1, f"поток со снимком переподключён: {stream.events}"
    assert not [text for text in heard.warnings() if "ни одного снимка" in text], heard.said


def test_the_first_snapshot_limit_is_a_minute_not_forever() -> None:
    """Стережёт 1, срок: молчание брокера говорится не позже, чем через минуту.

    Остальные тесты файла подменяют срок на 0,05 с — поднятие его до часа
    они не замечают, а человек узнал бы о молчащем брокере через час.
    Нижняя граница — замер `B-007`: брокер присылает первый снимок
    за 3–17 с, срок короче рвал бы здоровый поток.
    Мутация, которую тест ловит (проверено 29.09.2026): срок 600 с.
    """
    assert 17 < live_feed.FIRST_SNAPSHOT_WAIT <= 60, live_feed.FIRST_SNAPSHOT_WAIT
