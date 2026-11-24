"""Спред и ход цены после закрытия пятиминутки — запись с живого потока брокера.

Зачем
-----
Вход для миниплана Э4 (`.docs/plans/E4-live-orders.md`, развилки Р1 и Р3,
вопросы №7 и №26 в `OPEN-QUESTIONS.md`): какой отступ предельной цены нужен,
чтобы лимитная заявка по открытию следующей свечи исполнилась, за сколько
секунд, и насколько цена рыночной заявки отойдёт от цены сигнала.

Что делает
----------
Только **читает** рыночные данные: стакан, котировки, обезличенные сделки
и минутные свечи — один сокет, четыре подписки (`.docs/broker-api/20`–`23`).
Заявок не подаёт и подать не может: сокет рыночных данных заявок не принимает,
а HTTP-сессия брокера пускает только чтение (`BrokerSession._guard`).
Единственный HTTP-запрос — справочник инструмента, за шагом цены.

Запускает владелец счёта сам, своим токеном (правило 8 `CLAUDE.md`: в разработке
боевой токен не участвует). Из корня рабочей копии::

    .venv/bin/python -m tools.spread_drift

Инструмент — из настроек программы, окно — 09:30–11:30 МСК (ключи `--start`,
`--end`). Запуск раньше окна ждёт его начала; позже — пишет остаток окна.
Поток ложится в `userdata/spread-drift/spread-drift-<дата>.jsonl`, по концу
окна — отчёт в `.docs/quality/spread-drift-<дата>.md`. Отчёт пересобирается
из записи без связи::

    .venv/bin/python -m tools.spread_drift report userdata/spread-drift/<файл>.jsonl

Токен
-----
Подключение — `broker.stream.candle_stream`: заголовок авторизации собирает
и стирает слой брокера, логгер рукопожатия — под чисткой секретов. В запись
идут поля рыночных сообщений; образцы сырых кадров дополнительно проходят
`broker.redaction.scrub`. Отказы слоя печатаются полем `human` (уже вычищено),
неожиданные — типом и вычищенным текстом, без трассировки.

Что считается
-------------
* **Цена сигнала** — закрытие последней минуты пятиминутки, как его видит
  движок (последний пришедший снимок минутной свечи). Нет свечи — последняя
  сделка до закрытия.
* **Спред** — лучшая продажа минус лучшая покупка в момент закрытия, в шагах.
* **Ход** — середина стакана через 5/10/20 с минус середина на закрытии.
* **Покупка / продажа по рынку через d с** — лучшая продажа минус цена сигнала
  (для продажи — цена сигнала минус лучшая покупка). Плюс — хуже сигнала.
* **Лимитная от цены сигнала с отступом k шагов** — заявка, выставленная
  в момент закрытия (плюс `--place-delay`): через сколько секунд лучшая
  встречная цена дошла до предельной. Объём на уровне и очередь не учтены:
  это время, когда заявка **стала бы исполнимой**, а не гарантия исполнения.
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import json
import math
import statistics
import sys
import time as wallclock
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Final, TextIO

from app.settings_store import SettingsStore
from broker import BrokerError, BrokerSession, Instruments, TokenStore
from broker.connection import Backoff
from broker.redaction import scrub
from broker.stream import SUBSCRIBE, candle_stream
from market.candles import MSK
from market.paths import userdata_dir

ROOT: Final[Path] = Path(__file__).resolve().parent.parent

#: Значения `dataType` того же сокета (`.docs/broker-api/21`–`23`).
BOOK: Final[int] = 0
TRADES: Final[int] = 2
QUOTES: Final[int] = 3

#: Через сколько секунд после закрытия меряется ход и цена рыночной заявки.
OFFSETS: Final[tuple[int, ...]] = (5, 10, 20)
HORIZON: Final[int] = max(OFFSETS)
#: Отступы лимитной заявки от цены сигнала, в шагах цены.
LIMIT_STEPS: Final[tuple[int, ...]] = (0, 1, 2, 3, 5)
#: Сколько сырых кадров каждого вида сохранить образцом — на случай, если
#: сервер шлёт поля не так, как написано в документации (уже бывало).
SAMPLES_PER_KIND: Final[int] = 3
#: Код «торги открыты» в `securityTradingStatus` (`.docs/broker-api/22`).
TRADING_OPEN: Final[str] = "17"
CANDLE: Final[timedelta] = timedelta(minutes=5)
#: Уровень стакана парой «цена, количество» — длина пары.
PAIR: Final[int] = 2

Event = dict[str, Any]
Say = Callable[[str], None]


# ------------------------------------------------------------------ кадры


def _num(value: object) -> float | None:
    """Число из поля брокера. Строка тоже: `bidVolume` в примере — строка."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "."))
        except ValueError:
            return None
    return None


def _epoch(text: object) -> float | None:
    """`dateTime` брокера → секунды UTC. Без пояса — `None`, не догадка."""
    if not isinstance(text, str):
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment.timestamp() if moment.tzinfo is not None else None


def _levels(raw: object, *, descending: bool, depth: int) -> list[list[float]]:
    """Уровни стакана: `[{price, quantity}]` по документации, пары — тоже."""
    levels: list[list[float]] = []
    for item in raw if isinstance(raw, list) else ():
        if isinstance(item, dict):
            price, quantity = _num(item.get("price")), _num(item.get("quantity"))
        elif isinstance(item, list | tuple) and len(item) >= PAIR:
            price, quantity = _num(item[0]), _num(item[1])
        else:
            continue
        if price is not None and price > 0:
            levels.append([price, quantity or 0.0])
    levels.sort(key=lambda level: level[0], reverse=descending)
    return levels[:depth]


def _book_event(message: dict[str, Any], base: Event, depth: int) -> Event | None:
    bids = _levels(message.get("bids"), descending=True, depth=depth)
    asks = _levels(message.get("asks"), descending=False, depth=depth)
    return {"k": "book", **base, "b": bids, "a": asks} if bids and asks else None


def _quote_event(message: dict[str, Any], base: Event, _depth: int) -> Event | None:
    return {
        "k": "quote",
        **base,
        "bid": _num(message.get("bid")),
        "ask": _num(message.get("offer")),
        "last": _num(message.get("last")),
        "st": message.get("securityTradingStatus"),
    }


def _trade_event(message: dict[str, Any], base: Event, _depth: int) -> Event | None:
    price = _num(message.get("price"))
    if price is None:
        return None
    return {"k": "trade", **base, "p": price, "q": _num(message.get("quantity"))}


def _candle_event(message: dict[str, Any], base: Event, _depth: int) -> Event | None:
    close = _num(message.get("close"))
    if base["x"] is None or close is None:
        return None
    return {"k": "candle", **base, "c": close, "tf": message.get("timeFrame")}


#: Таблица по `responseType`, а не поиск по полям: у четырёх каналов разные
#: имена одних и тех же вещей (`offer` у котировок, `asks` у стакана).
PARSERS: Final[dict[str, Callable[[dict[str, Any], Event, int], Event | None]]] = {
    "OrderBook": _book_event,
    "Quotes": _quote_event,
    "LastTrades": _trade_event,
    "CandleStick": _candle_event,
}


def compact(message: dict[str, Any], received: float, depth: int) -> Event | None:
    """Сообщение брокера → строка записи. `None` — вид не разобран."""
    kind = message.get("responseType")
    if not isinstance(kind, str):
        return None
    if kind.endswith("Success"):
        return {"k": "ack", "t": received, "what": kind}
    parser = PARSERS.get(kind)
    if parser is None:
        return None
    return parser(message, {"t": received, "x": _epoch(message.get("dateTime"))}, depth)


# ------------------------------------------------------------------ запись


@dataclass
class Recorder:
    """Пишет кадры строками JSON и раз в минуту говорит, что поток жив."""

    out: TextIO
    depth: int
    say: Say = print
    now: Callable[[], float] = wallclock.time
    counts: Counter[str] = field(default_factory=Counter)
    frames_since_up: int = 0
    _samples: Counter[str] = field(default_factory=Counter)
    _minute: int | None = None

    def write(self, event: Event) -> None:
        """Одна строка JSON на событие."""
        self.out.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")

    def mark(self, kind: str, why: str = "") -> None:
        """Связь появилась (`up`) или пропала (`down`) — граница годных данных."""
        if kind == "up":
            self.frames_since_up = 0
        self.write({"k": kind, "t": self.now(), "why": why})
        self.out.flush()

    def frame(self, raw: str | bytes) -> None:
        """Кадр сокета → строка записи; незнакомый вид — образцом и счётом."""
        received = self.now()
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        self.frames_since_up += 1
        try:
            message = json.loads(text)
        except ValueError:
            message = None
        if not isinstance(message, dict):
            self._sample("не JSON", text, received)
            return
        if message.get("errors"):
            self._refusal(message, received)
            return
        kind = str(message.get("responseType", "без вида"))
        event = compact(message, received, self.depth)
        if event is None:
            self.counts[f"{kind} (не разобрано)"] += 1
        else:
            self.counts[event["k"]] += 1
            self.write(event)
        self._sample(kind, text, received)
        self._tick(received)

    def _sample(self, kind: str, text: str, received: float) -> None:
        if self._samples[kind] < SAMPLES_PER_KIND:
            self._samples[kind] += 1
            self.write({"k": "raw", "t": received, "kind": kind, "text": scrub(text[:4000])})

    def _refusal(self, message: dict[str, Any], received: float) -> None:
        errors = message.get("errors")
        head = errors[0] if isinstance(errors, list) and errors else {}
        first = head if isinstance(head, dict) else {}
        code = scrub(first.get("code") or first.get("type") or "без кода")
        text = scrub(first.get("message") or "без объяснения")
        self.write({"k": "error", "t": received, "code": code, "msg": text})
        self.say(f"Брокер отказал в подписке: {text} ({code})")

    def _tick(self, received: float) -> None:
        minute = int(received // 60)
        if self._minute is None:
            self._minute = minute
        if minute == self._minute:
            return
        stamp = datetime.fromtimestamp(self._minute * 60, MSK).strftime("%H:%M")
        seen = ", ".join(f"{name} {count}" for name, count in sorted(self.counts.items()))
        self.say(f"{stamp} МСК: связь есть; сообщений за минуту: {seen or 'нет'}")
        self.counts.clear()
        self._minute = minute
        self.out.flush()


async def ride(
    session: BrokerSession, recorder: Recorder, *, ticker: str, class_code: str
) -> None:
    """Одно подключение: минутные свечи через слой брокера, плюс три подписки.

    Свечи подписывает `candle_stream` — он же собирает и стирает заголовок
    авторизации. Остальные три подписки уходят в тот же сокет запросами без
    токена: сокет уже авторизован рукопожатием.
    """
    instruments = [{"ticker": ticker, "classCode": class_code}]
    requests = (
        {"subscribeType": SUBSCRIBE, "dataType": BOOK, "depth": recorder.depth},
        {"subscribeType": SUBSCRIBE, "dataType": QUOTES},
        {"subscribeType": SUBSCRIBE, "dataType": TRADES},
    )
    stream = candle_stream(session, ticker=ticker, class_code=class_code, timeframe="M1")
    async with stream as socket:
        recorder.mark("up")
        for request in requests:
            await socket.send(json.dumps({**request, "instruments": instruments}))
        async for raw in socket:
            recorder.frame(raw)


async def record_until(
    session: BrokerSession,
    recorder: Recorder,
    *,
    until: datetime,
    ticker: str,
    class_code: str,
) -> None:
    """Писать до `until`, переподключаясь с нарастающей паузой.

    Отказ, который повтором не лечится (токен не принят, инструмента нет),
    поднимается наружу: молча крутить переподключение до конца окна — значит
    отдать владельцу счёта пустой файл без объяснения.
    """
    backoff, attempt = Backoff(), 0
    while (left := (until - datetime.now(UTC)).total_seconds()) > 0:
        try:
            async with asyncio.timeout(left):
                await ride(session, recorder, ticker=ticker, class_code=class_code)
            recorder.mark("down", "брокер закрыл соединение")
        except TimeoutError:
            recorder.mark("down", "конец записи")
            return
        except BrokerError as error:
            recorder.mark("down", error.human)
            if not error.retryable:
                raise
            recorder.say(f"Связь с брокером прервалась: {error.human}")
        attempt = 0 if recorder.frames_since_up else attempt + 1
        pause = min(backoff.delay(attempt), max(0.0, (until - datetime.now(UTC)).total_seconds()))
        recorder.say(f"Переподключение через {pause:.0f} с")
        await asyncio.sleep(pause)


# ------------------------------------------------------------------ разбор


@dataclass(frozen=True)
class Book:
    """Лучшие цены по времени: ступенчатая функция «что было в стакане»."""

    times: list[float]
    bids: list[float]
    asks: list[float]
    bid_qty: list[float]
    ask_qty: list[float]
    source: str

    def at(self, moment: float) -> int | None:
        """Номер состояния, действовавшего в момент `moment`."""
        index = bisect.bisect_right(self.times, moment) - 1
        return index if index >= 0 else None

    def mid(self, index: int) -> float:
        """Середина между лучшими ценами."""
        return (self.bids[index] + self.asks[index]) / 2


def _when(event: Event, clock: str) -> float | None:
    moment = event.get("x") if clock == "broker" else event.get("t")
    return float(moment) if isinstance(moment, int | float) else None


def build_book(events: Sequence[Event], clock: str) -> Book:
    """Стакан, если он есть в записи; иначе лучшие цены из котировок."""
    rows: list[tuple[float, float, float, float, float]] = []
    source = "стакан" if any(event["k"] == "book" for event in events) else "котировки"
    for event in events:
        moment = _when(event, clock)
        if moment is None:
            continue
        if source == "стакан" and event["k"] == "book":
            (bid, bid_qty), (ask, ask_qty) = event["b"][0], event["a"][0]
            rows.append((moment, bid, ask, bid_qty, ask_qty))
        elif source == "котировки" and event["k"] == "quote" and event["bid"] and event["ask"]:
            rows.append((moment, event["bid"], event["ask"], math.nan, math.nan))
    rows.sort(key=lambda row: row[0])
    return Book(
        [row[0] for row in rows],
        [row[1] for row in rows],
        [row[2] for row in rows],
        [row[3] for row in rows],
        [row[4] for row in rows],
        source,
    )


def connected_spans(events: Sequence[Event]) -> list[tuple[float, float]]:
    """Отрезки, когда сокет был открыт. Новый запуск закрывает прежний отрезок."""
    spans: list[tuple[float, float]] = []
    opened: float | None = None
    last: float | None = None
    for event in events:
        moment = float(event["t"])
        if event["k"] in ("up", "down", "start") and opened is not None:
            spans.append((opened, moment if event["k"] != "start" else last or moment))
            opened = None
        if event["k"] == "up":
            opened = moment
        last = moment
    if opened is not None and last is not None:
        spans.append((opened, last))
    return spans


@dataclass(frozen=True)
class Close:
    """Одна пятиминутка: что было на закрытии и в первые секунды после."""

    moment: datetime
    skipped: str = ""
    spread: float = math.nan
    moves: dict[int, float] = field(default_factory=dict)
    excursion: float = math.nan
    buy: dict[int, float] = field(default_factory=dict)
    sell: dict[int, float] = field(default_factory=dict)
    fill_buy: dict[int, float | None] = field(default_factory=dict)
    fill_sell: dict[int, float | None] = field(default_factory=dict)
    candle_delay: float | None = None
    top_qty: tuple[float, float] = (math.nan, math.nan)


@dataclass(frozen=True)
class Tape:
    """Всё, что нужно для разбора, собранное из записи один раз."""

    book: Book
    spans: list[tuple[float, float]]
    candle_close: dict[int, float]
    candle_first: dict[int, float]
    trades: list[tuple[float, float]]
    statuses: list[tuple[float, str]]


def build_tape(events: Sequence[Event], clock: str) -> Tape:
    candle_close: dict[int, tuple[float, float]] = {}
    candle_first: dict[int, float] = {}
    trades: list[tuple[float, float]] = []
    statuses: list[tuple[float, str]] = []
    for event in events:
        if event["k"] == "candle" and event.get("tf") in (None, "M1"):
            minute, received = round(event["x"]), float(event["t"])
            if minute not in candle_close or candle_close[minute][0] <= received:
                candle_close[minute] = (received, event["c"])
            candle_first[minute] = min(candle_first.get(minute, received), received)
            continue
        moment = _when(event, clock)
        if moment is None:
            continue
        if event["k"] == "trade":
            trades.append((moment, event["p"]))
        elif event["k"] == "quote" and event.get("st") is not None:
            statuses.append((moment, str(event["st"])))
    return Tape(
        build_book(events, clock),
        connected_spans(events),
        {minute: close for minute, (_, close) in candle_close.items()},
        candle_first,
        sorted(trades),
        sorted(statuses),
    )


def _last_before[V](rows: Sequence[tuple[float, V]], moment: float, since: float) -> V | None:
    """Последнее значение не позже `moment`, но не раньше `since` (начала связи)."""
    index = bisect.bisect_right(rows, moment, key=lambda row: row[0]) - 1
    return rows[index][1] if index >= 0 and rows[index][0] >= since else None


def _state(book: Book, moment: float, fallback: int) -> int:
    index = book.at(moment)
    return fallback if index is None else index


def _first_reach(book: Book, start: float, end: float, *, side: str, limit: float) -> float | None:
    """Первый момент в `[start, end]`, когда встречная цена дошла до предельной."""
    index = book.at(start)
    index = 0 if index is None else index
    while index < len(book.times) and book.times[index] <= end:
        price = book.asks[index] if side == "buy" else book.bids[index]
        if (price <= limit) if side == "buy" else (price >= limit):
            return max(book.times[index], start)
        index += 1
    return None


def measure(tape: Tape, close_at: datetime, *, step: float, place_delay: float) -> Close:
    """Одна пятиминутка. Нет связи, стакана или цены сигнала — пропуск с причиной."""
    moment = close_at.timestamp()
    span = next((s for s in tape.spans if s[0] <= moment - 5 and moment + HORIZON <= s[1]), None)
    if span is None:
        return Close(close_at, "связи не было")
    status = _last_before(tape.statuses, moment, span[0])
    if status is not None and status != TRADING_OPEN:
        return Close(close_at, f"торги не открыты (статус {status})")
    book, now = tape.book, tape.book.at(moment)
    if now is None or book.times[now] < span[0]:
        return Close(close_at, "стакана ещё нет")
    minute = round(moment) - 60
    signal = tape.candle_close.get(minute) or _last_before(tape.trades, moment, span[0])
    if signal is None:
        return Close(close_at, "нет цены сигнала")
    states = {d: _state(book, moment + d, now) for d in (0, *OFFSETS)}
    last = _state(book, moment + HORIZON, now)
    mid0 = book.mid(now)
    eps = step * 1e-6
    placed = moment + place_delay
    first = tape.candle_first.get(round(moment))
    return Close(
        close_at,
        spread=(book.asks[now] - book.bids[now]) / step,
        moves={d: (book.mid(states[d]) - mid0) / step for d in OFFSETS},
        excursion=max(abs(book.mid(i) - mid0) for i in range(now, last + 1)) / step,
        buy={d: (book.asks[states[d]] - signal) / step for d in states},
        sell={d: (signal - book.bids[states[d]]) / step for d in states},
        fill_buy={
            k: _reach_after(book, placed, moment, "buy", signal + k * step + eps)
            for k in LIMIT_STEPS
        },
        fill_sell={
            k: _reach_after(book, placed, moment, "sell", signal - k * step - eps)
            for k in LIMIT_STEPS
        },
        candle_delay=None if first is None else first - moment,
        top_qty=(book.bid_qty[now], book.ask_qty[now]),
    )


def _reach_after(
    book: Book, placed: float, moment: float, side: str, limit: float
) -> float | None:
    reached = _first_reach(book, placed, moment + HORIZON, side=side, limit=limit)
    return None if reached is None else reached - moment


def closes_in_window(day: date, start: time, end: time) -> list[datetime]:
    """Закрытия пятиминуток окна: строго позже начала, не позже конца."""
    moment = datetime.combine(day, start, MSK) + CANDLE
    last = datetime.combine(day, end, MSK)
    found: list[datetime] = []
    while moment <= last:
        found.append(moment)
        moment += CANDLE
    return found


# ------------------------------------------------------------------ отчёт


def _ru(value: float | None, digits: int = 1, signed: bool = False) -> str:
    if value is None or math.isnan(value):
        return "—"
    text = f"{value:+.{digits}f}" if signed else f"{value:.{digits}f}"
    return text.replace(".", ",").replace("-", "−")


def percentile(values: Sequence[float], share: float) -> float:
    """Ближайший ранг: значение, не больше которого `share` выборки."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(share * len(ordered)) - 1)]


def _stat_row(name: str, values: Iterable[float | None]) -> str:
    clean = [v for v in values if v is not None and not math.isnan(v)]
    if not clean:
        return f"| {name} | — | — | 0 |"
    middle, high = _ru(statistics.median(clean)), _ru(percentile(clean, 0.9))
    return f"| {name} | {middle} | {high} | {len(clean)} |"


def _clock_lag(events: Sequence[Event]) -> float | None:
    lags = [e["t"] - e["x"] for e in events
            if e["k"] in ("quote", "trade", "book") and e.get("x")]
    return statistics.median(lags) if lags else None


def _summary(done: Sequence[Close]) -> list[str]:
    lines = [
        "| величина, шагов цены (свеча — секунд) | медиана | 90 % | свечей |",
        "|---|---|---|---|",
        _stat_row("спред на закрытии", (c.spread for c in done)),
    ]
    for d in OFFSETS:
        moves = (abs(c.moves[d]) for c in done)
        lines.append(_stat_row(f"ход середины через {d} с, по модулю", moves))
    excursions = (c.excursion for c in done)
    lines.append(_stat_row(f"наибольший отход середины за {HORIZON} с", excursions))
    for side, name in (("buy", "покупка"), ("sell", "продажа")):
        for d in (0, *OFFSETS):
            when = "на закрытии" if d == 0 else f"через {d} с"
            label = f"{name} по рынку {when}, от цены сигнала"
            lines.append(_stat_row(label, (getattr(c, side)[d] for c in done)))
    delays = (c.candle_delay for c in done)
    lines.append(_stat_row("первая минута следующей свечи пришла через, с", delays))
    lines.append(_stat_row("объём на лучшей покупке, контрактов", (c.top_qty[0] for c in done)))
    lines.append(_stat_row("объём на лучшей продаже, контрактов", (c.top_qty[1] for c in done)))
    return lines


def _limits(done: Sequence[Close], place_delay: float) -> list[str]:
    lines = [
        f"Заявка выставлена через {_ru(place_delay)} с после закрытия; ждём {HORIZON} с.",
        "",
        "| отступ от цены сигнала, шагов | покупка: стала исполнимой | медиана, с | "
        "продажа: стала исполнимой | медиана, с |",
        "|---|---|---|---|---|",
    ]
    for k in LIMIT_STEPS:
        cells = []
        for fills in ([c.fill_buy[k] for c in done], [c.fill_sell[k] for c in done]):
            reached = [s for s in fills if s is not None]
            middle = _ru(statistics.median(reached)) if reached else "—"
            cells += [f"{len(reached)} из {len(fills)}", middle]
        lines.append(f"| {k} | " + " | ".join(cells) + " |")
    return lines


def _per_close(closes: Sequence[Close]) -> list[str]:
    head = " | ".join(f"ход +{d} с" for d in OFFSETS)
    lines = [
        f"| закрытие МСК | спред | {head} | отход за {HORIZON} с "
        "| покупка +10 с | продажа +10 с | свеча, с |",
        "|---|" + "---|" * (len(OFFSETS) + 6),
    ]
    for c in closes:
        stamp = c.moment.astimezone(MSK).strftime("%H:%M")
        if c.skipped:
            lines.append(f"| {stamp} | пропуск: {c.skipped} |" + " |" * (len(OFFSETS) + 5))
            continue
        moves = " | ".join(_ru(c.moves[d], signed=True) for d in OFFSETS)
        lines.append(
            f"| {stamp} | {_ru(c.spread)} | {moves} | {_ru(c.excursion)} | "
            f"{_ru(c.buy[10], signed=True)} | {_ru(c.sell[10], signed=True)} | "
            f"{_ru(c.candle_delay)} |"
        )
    return lines


@dataclass(frozen=True)
class Header:
    """Строка начала записи: инструмент, день, окно, шаг цены."""

    ticker: str
    class_code: str
    day: date
    start: time
    end: time
    step: float | None


def read_events(path: Path) -> list[Event]:
    """Запись `.jsonl` → список событий."""
    events: list[Event] = []
    with path.open(encoding="utf-8") as source:
        for line in source:
            if line.strip():
                events.append(json.loads(line))
    return events


def header_of(events: Sequence[Event], path: Path) -> Header:
    """Первая строка начала записи. Её нет — файл не этого инструмента."""
    first = next((e for e in events if e["k"] == "start"), None)
    if first is None:
        raise SystemExit(f"В записи {path} нет строки начала — это не файл этого инструмента")
    return Header(
        first["ticker"],
        first["class_code"],
        date.fromisoformat(first["day"]),
        time.fromisoformat(first["start"]),
        time.fromisoformat(first["end"]),
        first.get("step"),
    )


def render(
    events: Sequence[Event], header: Header, *, step: float, clock: str, place_delay: float
) -> str:
    """Отчёт markdown: итог, лимитная от цены сигнала, таблица по пятиминуткам."""
    tape = build_tape(events, clock)
    closes = [
        measure(tape, moment, step=step, place_delay=place_delay)
        for moment in closes_in_window(header.day, header.start, header.end)
    ]
    done = [c for c in closes if not c.skipped]
    skipped = Counter(c.skipped for c in closes if c.skipped)
    lag = _clock_lag(events)
    axis = "время брокера (`dateTime`)" if clock == "broker" else "часы машины в момент приёма"
    spans = tape.spans
    began = _msk(spans[0][0]) if spans else "—"
    ended = _msk(spans[-1][1]) if spans else "—"
    why = "; ".join(f"{reason} — {count}" for reason, count in skipped.items()) or "нет"
    lines = [
        f"# Спред и ход цены после закрытия пятиминутки — {header.ticker}, {header.day:%d.%m.%Y}",
        "",
        f"Записано `tools/spread_drift.py` с живого потока брокера. {header.class_code}, "
        f"шаг цены {_ru(step, 2)}, окно {header.start:%H:%M}–{header.end:%H:%M} МСК. "
        f"Лучшие цены — {tape.book.source}. Ось времени — {axis}; приём позже метки брокера "
        f"на {_ru(lag, 2)} с (медиана).",
        "",
        f"Связь: с {began} до {ended} МСК, разрывов {max(0, len(spans) - 1)}. "
        f"Пятиминуток в окне: {len(closes)}, измерено: {len(done)}, пропущено: {why}.",
        "",
        "Цена сигнала — закрытие последней минуты пятиминутки. "
        "Плюс у рыночной цены — хуже сигнала.",
        "",
        "## Итог",
        "",
        *_summary(done),
        "",
        "## Лимитная от цены сигнала",
        "",
        *_limits(done, place_delay),
        "",
        "«Стала исполнимой» — встречная лучшая цена дошла до предельной; "
        "объём на уровне не учтён.",
        "",
        "## По пятиминуткам",
        "",
        *_per_close(closes),
        "",
    ]
    return "\n".join(lines)


def _msk(moment: float) -> str:
    return datetime.fromtimestamp(moment, MSK).strftime("%H:%M:%S")


def report_path(day: date, beside: Path) -> Path:
    """`.docs/quality/` рабочей копии; нет её — рядом с записью."""
    quality = ROOT / ".docs" / "quality"
    folder = quality if quality.is_dir() else beside.parent
    return folder / f"spread-drift-{day.isoformat()}.md"


def write_report(
    path: Path, *, step: float | None, clock: str, place_delay: float, out: Path | None
) -> Path:
    """Прочитать запись, построить отчёт, положить файлом. Возвращает путь."""
    events = read_events(path)
    header = header_of(events, path)
    step = step or header.step
    if not step:
        raise SystemExit("Шаг цены неизвестен: справочник брокера не ответил. Задайте --step")
    target = out or report_path(header.day, path)
    text = render(events, header, step=step, clock=clock, place_delay=place_delay)
    target.write_text(text, encoding="utf-8")
    return target


# ------------------------------------------------------------------ запуск


async def _price_step(session: BrokerSession, ticker: str, say: Say) -> float | None:
    try:
        card = await Instruments(session).one(ticker)
    except BrokerError as error:
        say(f"Шаг цены из справочника брокера не получен: {error.human}")
        return None
    return card.price_step


async def run_record(args: argparse.Namespace, say: Say = print) -> int:
    """Дождаться окна, писать поток до его конца (+20 с), построить отчёт."""
    userdata = userdata_dir()
    ticker = (args.ticker or SettingsStore(userdata).load().values.instrument).strip()
    day = datetime.now(MSK).date()
    begin = datetime.combine(day, args.start, MSK) - timedelta(minutes=1)
    until = datetime.combine(day, args.end, MSK) + timedelta(seconds=HORIZON + 10)
    if datetime.now(UTC) >= until:
        say(
            f"Окно {args.start:%H:%M}–{args.end:%H:%M} МСК сегодня уже закончилось. "
            "Запустите в торговый день до его конца."
        )
        return 2
    named = args.token_file
    store = TokenStore(userdata, filename=named) if named else TokenStore(userdata)
    target = recording_path(userdata, day)
    target.parent.mkdir(parents=True, exist_ok=True)
    async with BrokerSession(store) as session:
        scope = "торговля и чтение" if session.can_trade() else "только чтение"
        say(f"Токен: {scope}. Инструмент только читает поток, заявок не подаёт.")
        step = await _price_step(session, ticker, say)
        say(f"{ticker} {args.class_code}, шаг цены {step}. Запись: {target}")
        if (wait := (begin - datetime.now(UTC)).total_seconds()) > 0:
            say(f"Жду {begin.astimezone(MSK):%H:%M} МСК — за минуту до окна.")
            await asyncio.sleep(wait)
        with target.open("a", encoding="utf-8") as out:
            recorder = Recorder(out, args.depth, say)
            recorder.write(
                {
                    "k": "start",
                    "t": wallclock.time(),
                    "ticker": ticker,
                    "class_code": args.class_code,
                    "step": step,
                    "depth": args.depth,
                    "day": day.isoformat(),
                    "start": args.start.isoformat(),
                    "end": args.end.isoformat(),
                }
            )
            say(f"Запись идёт до {until.astimezone(MSK):%H:%M:%S} МСК. Раньше — Ctrl+C.")
            try:
                await record_until(
                    session, recorder, until=until, ticker=ticker, class_code=args.class_code
                )
            finally:
                out.flush()
    report = write_report(
        target, step=step, clock="local", place_delay=args.place_delay, out=None
    )
    say(f"Отчёт: {report}")
    return 0


def _arguments(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Спред и ход цены после закрытия пятиминутки.")
    add = parser.add_argument
    add("command", nargs="?", choices=("record", "report"), default="record")
    add("file", nargs="?", type=Path, help="запись .jsonl — для report")
    add("--ticker", help="тикер; по умолчанию — инструмент из настроек программы")
    add("--class-code", default="SPBFUT")
    add("--start", type=time.fromisoformat, default=time(9, 30), help="начало окна, МСК")
    add("--end", type=time.fromisoformat, default=time(11, 30), help="конец окна, МСК")
    add("--depth", type=int, default=5, help="глубина стакана, 1–20")
    add("--token-file", help="имя файла токена в папке userdata, если не основной")
    add("--step", type=float, help="шаг цены — для report, если в записи его нет")
    add("--clock", choices=("local", "broker"), default="local", help="ось времени отчёта")
    add("--place-delay", type=float, default=0.0, help="секунд от закрытия до лимитной")
    add("--out", type=Path, help="куда положить отчёт")
    return parser.parse_args(argv)


def recording_path(userdata: Path, day: date) -> Path:
    """Файл записи дня: дозапись при повторном запуске в тот же день."""
    return userdata / "spread-drift" / f"spread-drift-{day.isoformat()}.jsonl"


def _report(args: argparse.Namespace, path: Path) -> int:
    report = write_report(
        path, step=args.step, clock=args.clock, place_delay=args.place_delay, out=args.out
    )
    print(f"Отчёт: {report}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Запись (по умолчанию) или отчёт по готовой записи.

    Ctrl+C посреди записи не теряет записанного: отчёт строится по тому, что
    успело лечь в файл. Отказ слоя брокера печатается его полем `human`,
    уже вычищенным от секретов.
    """
    args = _arguments(sys.argv[1:] if argv is None else argv)
    if args.command == "report":
        if args.file is None:
            raise SystemExit("Для report нужен файл записи .jsonl")
        return _report(args, args.file)
    try:
        return asyncio.run(run_record(args))
    except KeyboardInterrupt:
        print("Запись остановлена.")
        path = recording_path(userdata_dir(), datetime.now(MSK).date())
        return _report(args, path) if path.exists() else 130
    except BrokerError as error:
        print(f"Брокер: {error.human}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
