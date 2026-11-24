"""Инструмент замера спреда и хода цены: разбор кадров, расчёт по пятиминутке, запись.

Сеть подставная: сокет — заглушка вместо `candle_stream`, токена в тестах нет.
Стерегут три поведения:

* кадры каналов по примерам документации разбираются, незнакомые — нет;
* расчёт по пятиминутке даёт названные числа на выписанном руками стакане,
  а пятиминутка без связи или вне торгов пропускается с причиной;
* в сокет уходят только подписки на рыночные данные, обрыв переподключается.
"""

from __future__ import annotations

import asyncio
import io
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, cast

import pytest

from broker import BrokerSession
from broker.errors import NoConnection
from market.candles import MSK
from tools import spread_drift as sd

STEP = 25.0
DAY = date(2026, 10, 27)
CLOSE = datetime(2026, 10, 27, 10, 35, tzinfo=MSK)
T = CLOSE.timestamp()


def book(at: float, bid: float, ask: float) -> dict[str, Any]:
    return {"k": "book", "t": at, "x": at - 0.2, "b": [[bid, 3.0]], "a": [[ask, 4.0]]}


def recorded() -> list[dict[str, Any]]:
    """Связь с 10:30 до 10:40; стакан шагает; закрытие минуты 10:34 — 1000."""
    return [
        {"k": "start", "t": T - 300, "ticker": "MXZ6", "class_code": "SPBFUT", "step": STEP,
         "day": DAY.isoformat(), "start": "10:30:00", "end": "10:35:00"},
        {"k": "up", "t": T - 300},
        {"k": "candle", "t": T + 2.0, "x": T - 60, "c": 990.0, "tf": "M1"},
        {"k": "candle", "t": T + 4.0, "x": T - 60, "c": 1000.0, "tf": "M1"},
        {"k": "candle", "t": T + 7.5, "x": T, "c": 1025.0, "tf": "M1"},
        {"k": "quote", "t": T - 10, "x": T - 10, "st": 17, "bid": 975.0, "ask": 1025.0},
        book(T - 1, 975.0, 1025.0),       # на закрытии: спред 2 шага, середина 1000
        book(T + 3, 1000.0, 1050.0),      # +1 шаг
        book(T + 8, 1025.0, 1075.0),      # +2 шага
        book(T + 15, 950.0, 1000.0),      # −1 шаг, продажа исполнима по 0 отступу? нет
        {"k": "down", "t": T + 300},
    ]


def test_compact_reads_documented_frames() -> None:
    frames: dict[str, dict[str, Any]] = {
        "OrderBook": {"responseType": "OrderBook", "dateTime": "2024-10-30T09:01:00.000Z",
                      "bidVolume": "59851", "bids": [{"price": 244.30, "quantity": 100},
                                                     {"price": 244.25, "quantity": 90}],
                      "asks": [{"price": 244.40, "quantity": 50},
                               {"price": 244.35, "quantity": 120}]},
        "Quotes": {"responseType": "Quotes", "dateTime": "2024-11-01T10:00:00.000Z",
                   "securityTradingStatus": 17, "bid": 305.86, "offer": 305.96, "last": 305.97},
        "LastTrades": {"responseType": "LastTrades", "price": 305.89, "quantity": 2,
                       "dateTime": "2025-11-20T06:29:41.000Z"},
        "CandleStick": {"responseType": "CandleStick", "close": 10.0, "timeFrame": "M1",
                        "dateTime": "2025-11-20T06:29:00Z"},
    }
    events = {kind: sd.compact(frame, 1.0, 5) or {} for kind, frame in frames.items()}
    assert events["OrderBook"]["a"][0] == [244.35, 120.0], "лучшая продажа — наименьшая цена"
    assert events["OrderBook"]["x"] == datetime(2024, 10, 30, 9, 1, tzinfo=UTC).timestamp()
    assert events["Quotes"] == {"k": "quote", "t": 1.0, "x": events["Quotes"]["x"], "bid": 305.86,
                                "ask": 305.96, "last": 305.97, "st": 17}
    assert events["LastTrades"]["p"] == 305.89
    assert events["CandleStick"]["c"] == 10.0
    assert sd.compact({"responseType": "Something"}, 1.0, 5) is None
    assert sd.compact({"responseType": "QuotesSuccess"}, 1.0, 5) == {
        "k": "ack", "t": 1.0, "what": "QuotesSuccess"}


def test_measure_counts_spread_moves_and_fills_in_steps() -> None:
    tape = sd.build_tape(recorded(), "local")
    close = sd.measure(tape, CLOSE, step=STEP, place_delay=0.0)
    assert close.skipped == ""
    assert close.spread == 2.0
    assert close.moves == {5: 1.0, 10: 2.0, 20: -1.0}
    assert close.excursion == 2.0
    # Цена сигнала — последний снимок минуты 10:34, а не первый.
    assert close.buy == {0: 1.0, 5: 2.0, 10: 3.0, 20: 0.0}
    assert close.sell == {0: 1.0, 5: 0.0, 10: -1.0, 20: 2.0}
    assert close.fill_buy[0] == 15.0, "продажа дошла до 1000 только на 15-й секунде"
    assert close.fill_buy[1] == 0.0
    assert close.fill_sell[0] == 3.0
    assert close.candle_delay == 7.5
    assert close.top_qty == (3.0, 4.0)


def test_measure_skips_close_without_connection_or_open_trading() -> None:
    events = recorded()
    cut = [e for e in events if e["k"] != "down"] + [{"k": "down", "t": T + 10}]
    assert sd.measure(sd.build_tape(cut, "local"), CLOSE, step=STEP, place_delay=0).skipped == (
        "связи не было"
    )
    halted = [{**e, "st": 2} if e["k"] == "quote" else e for e in events]
    skipped = sd.measure(sd.build_tape(halted, "local"), CLOSE, step=STEP, place_delay=0).skipped
    assert skipped == "торги не открыты (статус 2)"


def test_render_reports_every_close_of_window() -> None:
    header = sd.Header("MXZ6", "SPBFUT", DAY, time(10, 25), time(10, 35), STEP)
    text = sd.render(recorded(), header, step=STEP, clock="local", place_delay=0.0)
    assert "Пятиминуток в окне: 2, измерено: 1, пропущено: связи не было — 1." in text
    assert "| 10:35 | 2,0 | +1,0 | +2,0 | −1,0 | 2,0 | +3,0 | −1,0 | 7,5 |" in text
    assert sd.closes_in_window(DAY, time(9, 30), time(11, 30))[0] == datetime(
        2026, 10, 27, 9, 35, tzinfo=MSK
    )


class FakeSocket:
    def __init__(self, frames: list[str]) -> None:
        self.sent: list[dict[str, Any]] = []
        self._frames = frames

    async def send(self, text: str) -> None:
        self.sent.append(json.loads(text))

    def __aiter__(self) -> AsyncIterator[str]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[str]:
        for frame in self._frames:
            yield frame
        raise NoConnection("связь пропала")


def test_record_sends_only_market_data_subscriptions_and_reconnects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sockets: list[FakeSocket] = []

    @asynccontextmanager
    async def fake_stream(_session: object, **_kwargs: object) -> AsyncIterator[FakeSocket]:
        socket = FakeSocket([json.dumps({"responseType": "Quotes", "bid": 1, "offer": 2})])
        sockets.append(socket)
        yield socket

    monkeypatch.setattr(sd, "candle_stream", fake_stream)
    out = io.StringIO()
    recorder = sd.Recorder(out, 5, say=lambda _line: None)
    until = datetime.now(UTC) + timedelta(seconds=0.3)

    class Quick:
        def delay(self, _attempt: int) -> float:
            return 0.05

    monkeypatch.setattr(sd, "Backoff", Quick)
    session = cast(BrokerSession, object())
    asyncio.run(
        sd.record_until(session, recorder, until=until, ticker="MXZ6", class_code="SPBFUT")
    )
    assert len(sockets) >= 2, "обрыв не переподключился"
    sent = [request for socket in sockets for request in socket.sent]
    assert {request["dataType"] for request in sent} == {sd.BOOK, sd.QUOTES, sd.TRADES}
    assert {request["subscribeType"] for request in sent} == {sd.SUBSCRIBE}
    kinds = [json.loads(line)["k"] for line in out.getvalue().splitlines()]
    assert kinds.count("up") == len(sockets) and "quote" in kinds and kinds[-1] == "down"
