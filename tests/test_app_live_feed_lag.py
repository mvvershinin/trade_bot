"""Отстающий поток свечей замечается сам и переподключается (боевой прогон 13.11.2026).

Что стережётся, словами
-----------------------
1. **Снимки старые.** Снимки идут, но по минуте, закончившейся дольше порога
   назад, а снимков следующей минуты нет — строка в журнал и переподключение.
   Ровно случай 13.11.2026: минута приходила через 2–4 минуты, второй поток
   рядом получал её сразу.
2. **Снимков нет.** Ни одного своего снимка дольше порога — то же самое,
   и раньше, чем сработал бы признак 1.
3. **Нормальный поток сторож не трогает**: минута за минутой, без
   переподключений и без строк «отстаёт».
4. **Одна строка на минуту.** Повтор на той же минуте после переподключения
   (брокер подтвердил, что новее у него ничего нет) в журнал не идёт.
5. **Закрытая по расписанию биржа** — не «отстаёт»: строки нет, поток ждёт
   открытия.
6. **Запись в базу не останавливает чтение сокета**: пока минута пишется,
   снимки продолжают приниматься, минуты пишутся по порядку.
7. **Проводка**: порог — поле «Допустимое опоздание» из настроек порта,
   свежим чтением.

Сокета, брокера и настоящего ожидания нет: `candle_stream` подменён
сценарием заходов, часы и сон сторожа — подставные (`FakeClock`).
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import pathlib
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from app import live_feed
from app.live_feed import LiveFeed, lag_due
from broker.schedule import SessionKind, TradingStatus
from market import MSK, Candle, Source, WriteStats
from tests.test_app_live_feed import (
    FakePort,
    FakeSocket,
    FakeStream,
    FakeWorker,
    Heard,
    feed_of,
    link_of,
    settle,
    snapshot,
    wire,
)
from ui.models import Connection, Settings

UTC = timezone.utc
#: Порог сторожа в проверках — умолчание поля окна, 60 с.
AFTER = 60.0
LAG_EVENT = "Поток брокера отстаёт"


class FakeClock:
    """Часы и сон сторожа. Сон сдвигает часы не дальше шага и зовёт `on_tick`.

    `until` — конец сценария: дальше сон не идёт, сторож спит до отмены.
    Без него опустевший сценарий крутил бы подставное время вхолостую.
    """

    def __init__(self, start: datetime, *, until: datetime, step: float = 10.0) -> None:
        self.moment = start
        self.until = until
        self.step = step
        self.on_tick: Callable[[datetime], None] | None = None

    def now(self) -> datetime:
        return self.moment

    async def nap(self, seconds: float) -> None:
        if self.moment >= self.until:
            await asyncio.Event().wait()
        self.moment += timedelta(seconds=min(seconds, self.step))
        if self.on_tick is not None:
            self.on_tick(self.moment)
        for _ in range(5):  # дать чтению разобрать то, что пришло за шаг
            await asyncio.sleep(0)


def msk(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 11, 13, hour, minute, second, tzinfo=MSK)


def paced(monkeypatch, stream: FakeStream, worker, heard: Heard, clock: FakeClock) -> LiveFeed:
    feed = feed_of(monkeypatch, stream, worker, heard)
    feed.pace(lambda: AFTER, now=clock.now, nap=clock.nap)
    return feed


def lag_lines(heard: Heard) -> list[str]:
    return [text for text in heard.warnings() if text.startswith(LAG_EVENT)]


def run(feed: LiveFeed, until: Callable[[], bool], what: str) -> None:
    async def scenario() -> None:
        feed.start()
        try:
            await settle(until, what)
        finally:
            await feed.aclose()

    asyncio.run(scenario())


# ============================================================ срок, чистая функция


def test_the_due_moment_is_the_earlier_of_the_two_signs() -> None:
    """Снимков нет — от прихода последнего; снимки старые — от конца минуты."""
    minute, since = msk(11, 0), msk(11, 0, 5)
    assert lag_due(heard_at=msk(11, 0, 5), minute=minute, since=since, after=AFTER) == msk(
        11, 1, 5
    ), "молчание дольше порога не замечено"
    assert lag_due(heard_at=msk(11, 3), minute=minute, since=since, after=AFTER) == msk(
        11, 2
    ), "старые снимки продлевают поток бесконечно"
    assert lag_due(
        heard_at=msk(11, 5, 30), minute=minute, since=msk(11, 5, 30), after=AFTER
    ) == msk(11, 6, 30), "прошлая минута, присланная при подписке, рвёт свежий заход"


# ============================================================ поток со сторожем


def test_old_snapshots_still_arriving_are_lag_said_aloud_and_reconnected(
    monkeypatch,
) -> None:
    """Стережёт 1. Снимки идут каждые 10 с — но все по минуте 11:52.

    Мутация «только признак тишины»: снимки идут, тишины нет, сторож молчит —
    тест падает на ожидании переподключения.
    """
    old = msk(11, 52).astimezone(UTC)
    clock = FakeClock(msk(11, 53, 5), until=msk(11, 54, 30))
    first = FakeSocket([wire(snapshot(old, close=100.0, turnover=100.0))])
    fresh = FakeSocket([wire(snapshot(msk(11, 54).astimezone(UTC), close=101.0, turnover=101.0))])
    stream = FakeStream(first, fresh)
    turnover = iter(range(200, 10_000, 100))

    def stale_keeps_coming(_now: datetime) -> None:
        if len(stream.opened) == 1:
            first.push(wire(snapshot(old, close=100.0, turnover=float(next(turnover)))))

    clock.on_tick = stale_keeps_coming
    heard, worker = Heard(), FakeWorker()
    feed = paced(monkeypatch, stream, worker, heard, clock)

    run(feed, lambda: len(stream.opened) == 2 and fresh.delivered == 1, "переподключение")

    assert first.delivered >= 5, "старые снимки не шли — проверка вакуумна"
    assert lag_lines(heard) == [
        "Поток брокера отстаёт: последняя минута 11:52, сейчас 11:54:05 — переподключаюсь. "
        "Пропущенные минуты догрузятся у брокера. Если по инструменту просто нет сделок, "
        "это затишье, а не сбой."
    ], heard.said
    assert stream.events[:3] == ["open#1", "close#1", "open#2"], stream.events
    assert heard.leading == [msk(11, 52), msk(11, 54)], (
        "второй заход не объявил свою минуту — пропущенное не догрузится"
    )
    assert Connection.OFFLINE not in heard.connections, "поток снят, а не переподключён"


def test_silence_longer_than_the_limit_is_lag_and_said_once_per_minute(
    monkeypatch, caplog
) -> None:
    """Стережёт 2 и 4. Один снимок 11:00 — и тишина.

    Срабатывание в 11:01:05 (порог от прихода), а не в 11:02:00 (порог
    от конца минуты): мутация «только признак старых снимков» даёт другое
    «сейчас», и тест падает. Второй заход брокер открывает той же 11:00 —
    новее у него ничего нет, это затишье: второе срабатывание идёт
    в технический лог, а не второй строкой в журнал.
    """
    quiet = msk(11, 0).astimezone(UTC)
    clock = FakeClock(msk(11, 0, 5), until=msk(11, 2, 30))
    stream = FakeStream(
        FakeSocket([wire(snapshot(quiet, close=100.0, turnover=100.0))]),
        FakeSocket([wire(snapshot(quiet, close=100.0, turnover=100.0))]),
    )
    heard, worker = Heard(), FakeWorker()
    feed = paced(monkeypatch, stream, worker, heard, clock)

    with caplog.at_level(logging.WARNING, logger="app.live_feed"):
        run(feed, lambda: len(stream.opened) == 3, "два переподключения")

    [line] = lag_lines(heard)
    assert "последняя минута 11:00, сейчас 11:01:05" in line, line
    repeats = [r.getMessage() for r in caplog.records if "уже сказано" in r.getMessage()]
    assert repeats, "повтор на той же минуте не дошёл даже до технического лога"


def test_a_normal_stream_is_left_alone(monkeypatch) -> None:
    """Стережёт 3. Пять минут снимков текущей минуты — ни одного переподключения.

    Заодно порядок записи: минуты 10:00…10:04 легли в базу по очереди.
    """
    clock = FakeClock(msk(10, 0, 5), until=msk(10, 5, 5))
    socket = FakeSocket([wire(snapshot(msk(10, 0).astimezone(UTC), close=100.0, turnover=100.0))])
    stream = FakeStream(socket)
    counted: dict[datetime, float] = {msk(10, 0): 100.0}

    def current_minute_trades(now: datetime) -> None:
        minute = now.replace(second=0)
        counted[minute] = counted.get(minute, 0.0) + 100.0
        socket.push(wire(snapshot(minute.astimezone(UTC), close=100.0, turnover=counted[minute])))

    clock.on_tick = current_minute_trades
    heard, worker = Heard(), FakeWorker()
    feed = paced(monkeypatch, stream, worker, heard, clock)

    run(feed, lambda: len(worker.written) == 5 and clock.moment >= clock.until, "пять минут")

    assert len(stream.opened) == 1, f"здоровый поток переподключён: {stream.events}"
    assert lag_lines(heard) == [], heard.said
    written = [candle.time for _, [candle], _ in worker.written]
    assert written == [msk(10, minute) for minute in range(5)], written


class Hours:
    """Расписание: торги идут, пока `open`; дальше — закрыто, момент открытия неизвестен."""

    def __init__(self) -> None:
        self.open = True

    async def status(self, *, class_code: str) -> TradingStatus:
        return TradingStatus(
            class_code=class_code,
            session_type=None,
            session_type_id=None,
            kind=SessionKind.MAIN if self.open else SessionKind.BREAK,
            is_open=self.open,
            next_change=None,
        )

    async def daily(self, *, class_code: str, ticker: str) -> None:
        return None

    def status_trouble(self, *, class_code: str) -> None:
        return None


def test_a_market_closed_by_the_schedule_is_not_called_lag(monkeypatch) -> None:
    """Стережёт 5. Клиринг: снимки кончились, расписание говорит «закрыто».

    Строки «отстаёт» нет, поток уходит ждать открытия — «Биржа закрыта».
    Без этой развилки ложная тревога звучала бы трижды в день.
    """
    hours = Hours()
    clock = FakeClock(msk(14, 0, 5), until=msk(14, 3))
    stream = FakeStream(
        FakeSocket([wire(snapshot(msk(13, 59).astimezone(UTC), close=100.0, turnover=100.0))])
    )
    heard, worker = Heard(), FakeWorker()
    feed = paced(monkeypatch, stream, worker, heard, clock)
    feed.follow(hours)

    def clearing_begins(_now: datetime) -> None:
        hours.open = False

    clock.on_tick = clearing_begins
    run(feed, lambda: Connection.MARKET_CLOSED in heard.connections, "ожидание открытия")

    assert lag_lines(heard) == [], f"закрытая биржа названа отставанием: {heard.said}"
    assert [event for event, _, _ in heard.said].count("Биржа закрыта") == 1, heard.said


# ============================================================ запись не держит чтение


class SlowWorker(FakeWorker):
    """База, занятая чужой работой: запись ждёт, пока её не отпустят."""

    def __init__(self) -> None:
        super().__init__()
        self.gate: asyncio.Event | None = None

    async def put_minutes(
        self, symbol: str, candles: Iterable[Candle], source: Source
    ) -> WriteStats:
        assert self.gate is not None
        await self.gate.wait()
        return await super().put_minutes(symbol, candles, source)


def test_a_slow_write_does_not_stop_reading_the_socket(monkeypatch) -> None:
    """Стережёт 6. Запись минуты A висит — снимки B и C всё равно принимаются.

    Мутация «ждать записи в чтении» (как до 13.11.2026): чтение встаёт
    на первом снимке B, и тест падает на ожидании пяти снимков.
    """
    a, b, c = (msk(10, minute).astimezone(UTC) for minute in (1, 2, 3))
    socket = FakeSocket([
        wire(snapshot(a, close=100.0, turnover=100.0)),
        wire(snapshot(b, close=101.0, turnover=101.0)),
        wire(snapshot(b, close=102.0, turnover=204.0)),
        wire(snapshot(c, close=103.0, turnover=103.0)),
        wire(snapshot(c, close=104.0, turnover=208.0)),
    ])
    heard, worker = Heard(), SlowWorker()
    feed = feed_of(monkeypatch, FakeStream(socket), worker, heard)

    async def scenario() -> None:
        worker.gate = asyncio.Event()
        feed.start()
        try:
            await settle(lambda: socket.delivered == 5, "пять снимков при висящей записи")
            assert worker.written == [], "запись не висела — проверка вакуумна"
            assert [m.close for _, m in heard.growing] == [100.0], (
                "растущая минута нарисована раньше записи закончившейся: бар мигнёт"
            )
            worker.gate.set()
            await settle(lambda: len(worker.written) == 2, "запись A и B")
            await settle(lambda: len(heard.growing) == 2, "придержанная минута")
        finally:
            await feed.aclose()

    asyncio.run(scenario())
    assert [candle.time for _, [candle], _ in worker.written] == [
        msk(10, 1), msk(10, 2)
    ], "минуты записаны не по порядку"
    assert heard.closed == ["MXU6", "MXU6"]
    assert heard.growing[-1][1].close == 104.0, "после записи нарисована не последняя минута"


# ============================================================ проводка


def test_the_link_takes_the_limit_from_the_port_settings_fresh(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """Стережёт 7. Порог — `live_stale_after_sec` порта, и правка видна сразу.

    Поток собирается настоящим включением (`switch`), но не запускается:
    проверяется проводка, а не сокет.
    """
    built: list[LiveFeed] = []

    class Unstarted(LiveFeed):
        def start(self) -> None:
            built.append(self)

    monkeypatch.setattr(live_feed, "BrokerSession", lambda store: SimpleNamespace(
        stored=lambda: SimpleNamespace(permissions_were_loose=False, scope_notice=None),
    ))
    monkeypatch.setattr(live_feed, "TokenStore", lambda directory: directory)
    monkeypatch.setattr(live_feed, "LiveFeed", Unstarted)
    port: Any = FakePort()
    port.values = dataclasses.replace(Settings(), live_stale_after_sec=97)
    link = link_of(port, FakeWorker(), tmp_path / "userdata")
    assert link.switch(True) is None

    [feed] = built
    assert feed.lag_after == 97.0, "сторож взял не порог из настроек"
    port.values = dataclasses.replace(Settings(), live_stale_after_sec=30)
    assert feed.lag_after == 30.0, "правка порога не видна до пересборки"
