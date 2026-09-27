"""Таблица контрактов: периоды по рубежу, архив, загрузка без хвоста, куски.

Решение 0061. Сеть везде подставная (`FakeTransport`), базы — во временной
папке. Дневные объёмы пары MXU6/MXZ6 — настоящие числа ISS, снятые
27.09.2026 живым запросом: вход приходит снаружи правила рубежа, а не
выводится из него.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import sqlite3
import urllib.parse
from collections.abc import Iterator, Mapping
from datetime import date, datetime, timedelta

import pytest
from market_helpers import FakeTransport, minute, msk, no_sleep

from app.fetch import main as fetch_main
from market.candles import MSK, Candle
from market.chain import Leg, legs_of_chain
from market.contracts import (
    ChainNotQuarterly,
    ContractError,
    ContractRequest,
    Expiry,
    adopt_chain,
    current_contract,
    expiry_verdict,
    load_contract_minutes,
    periods_of_chain,
    pieces,
    refresh_contracts,
)
from market.iss import FUTURES, IssClient, IssPayloadError, parse_daily_volumes
from market.storage import SCHEMA_VERSION, CandleStore, ContractRow, Source

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=MSK)

#: Дневные объёмы ISS, 27.09.2026. 16.09 MXU6 ещё впереди, 17.09 — уже нет.
MXU6_DAYS = {
    date(2026, 9, 8): 469502, date(2026, 9, 9): 376898, date(2026, 9, 10): 360499,
    date(2026, 9, 11): 499833, date(2026, 9, 14): 469662, date(2026, 9, 15): 340836,
    date(2026, 9, 16): 278346, date(2026, 9, 17): 132031,
}
MXZ6_DAYS = {
    date(2026, 9, 8): 35053, date(2026, 9, 9): 36715, date(2026, 9, 10): 33573,
    date(2026, 9, 11): 47245, date(2026, 9, 14): 108498, date(2026, 9, 15): 114420,
    date(2026, 9, 16): 188522, date(2026, 9, 17): 385057, date(2026, 9, 18): 326860,
    date(2026, 9, 21): 298099, date(2026, 9, 22): 401663, date(2026, 9, 23): 389772,
    date(2026, 9, 24): 341841, date(2026, 9, 25): 306728,
}
LAST_DAYS = {"MXU6": "2026-09-17", "MXZ6": "2026-12-17"}


def daily_body(days: Mapping[date, float]) -> bytes:
    rows = [
        [1, 1, 1, 1, 0, volume, f"{day} 00:00:00", f"{day} 23:59:59"]
        for day, volume in sorted(days.items())
    ]
    columns = ["open", "close", "high", "low", "value", "volume", "begin", "end"]
    return json.dumps({"candles": {"columns": columns, "data": rows}}).encode()


def description_body(last: str | None) -> bytes:
    rows = [["SECID", "Код", "X", "string"]]
    if last is not None:
        rows.append(["LSTTRADE", "Последний день обращения", last, "date"])
    columns = ["name", "title", "value", "type"]
    return json.dumps({"description": {"columns": columns, "data": rows}}).encode()


def session(day: date, count: int) -> list[Candle]:
    """`count` минуток подряд с 10:00 МСК этого дня."""
    start = datetime.combine(day, datetime.min.time(), MSK) + timedelta(hours=10)
    return [minute(start + timedelta(minutes=i), volume=1.0) for i in range(count)]


def minute_body(candles: list[Candle]) -> bytes:
    rows = [
        [c.open, c.close, c.high, c.low, 0, c.volume,
         f"{c.time:%Y-%m-%d %H:%M:%S}", f"{c.time + timedelta(seconds=59):%Y-%m-%d %H:%M:%S}"]
        for c in candles
    ]
    columns = ["open", "close", "high", "low", "value", "volume", "begin", "end"]
    return json.dumps({"candles": {"columns": columns, "data": rows}}).encode()


class Exchange:
    """Подставная биржа: описание, дневные объёмы и минуты по торговым дням.

    Описание есть только у кодов из `LAST_DAYS` (и из `known`): прочие биржа
    «не знает» — пустое описание, как живой ответ ISS по MXX6. Дневные
    объёмы режутся по датам запроса, как у настоящей биржи.
    """

    def __init__(
        self,
        sessions: Mapping[date, int] | None = None,
        *,
        daily: Mapping[str, Mapping[date, float]] | None = None,
        known: Mapping[str, str] | None = None,
    ) -> None:
        self.sessions = dict(sessions or {})
        self.daily = dict(daily or {"MXU6": MXU6_DAYS, "MXZ6": MXZ6_DAYS})
        self.known = {**LAST_DAYS, **(known or {})}
        self.transport = FakeTransport(self.answer)
        self.client = IssClient(self.transport, sleep=no_sleep, pause=0.0)

    def answer(self, url: str) -> bytes:
        parsed = urllib.parse.urlparse(url)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        secid = parsed.path.rsplit("/", 2)[-1].removesuffix(".json")
        if query.get("iss.only") == "description":
            if secid not in self.known:
                return json.dumps({"description": {
                    "columns": ["name", "title", "value", "type"], "data": [],
                }}).encode()
            return description_body(self.known[secid])
        secid = parsed.path.rsplit("/", 2)[-2]
        if query["interval"] == "24":
            first = date.fromisoformat(query["from"])
            till = date.fromisoformat(query["till"])
            days = {
                day: volume for day, volume in self.daily[secid].items()
                if first <= day <= till
            }
            return daily_body(days if query["start"] == "0" else {})
        if query["start"] != "0":
            return minute_body([])
        since = date.fromisoformat(query["from"])
        till = date.fromisoformat(query["till"])
        candles = [
            candle
            for day, count in sorted(self.sessions.items())
            if since <= day <= till
            for candle in session(day, count)
        ]
        return minute_body(candles)

    def minute_requests(self) -> list[tuple[date, date]]:
        spans = []
        for url in self.transport.urls:
            query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
            if query.get("interval") == "1":
                spans.append((date.fromisoformat(query["from"]),
                              date.fromisoformat(query["till"])))
        return spans


@pytest.fixture
def store(tmp_path: pathlib.Path) -> Iterator[CandleStore]:
    with CandleStore(tmp_path / "work.sqlite3") as opened:
        yield opened


# -- схема ------------------------------------------------------------------

def test_base_of_schema_6_gains_the_contract_table_and_keeps_its_minutes(
    tmp_path: pathlib.Path,
) -> None:
    """Миграция 6 → 7: таблица появляется, версия поднимается, свечи на месте."""
    path = tmp_path / "old.sqlite3"
    with CandleStore(path) as old:
        old.put_minutes("MXU6", session(date(2026, 9, 1), 30), Source.ISS)
    raw = sqlite3.connect(path)
    raw.execute("DROP TABLE contract")
    raw.execute("PRAGMA user_version = 6")
    raw.commit()
    raw.close()

    with CandleStore(path) as reopened:
        assert len(reopened.minutes("MXU6")) == 30
        reopened.put_contracts([ContractRow("MXZ6", active_from=date(2026, 9, 17))], now=NOW)
        assert [row.symbol for row in reopened.contracts()] == ["MXZ6"]
    raw = sqlite3.connect(path)
    (version,) = raw.execute("PRAGMA user_version").fetchone()
    raw.close()
    assert version == SCHEMA_VERSION == 7


def test_archived_flag_cannot_disagree_with_a_closed_period(store: CandleStore) -> None:
    """`archived` выводится из конца периода, и база не даёт им разойтись."""
    store.put_contracts(
        [ContractRow("MXU6", active_from=date(2026, 6, 8), active_to=date(2026, 9, 16))],
        now=NOW,
    )
    raw = sqlite3.connect(store.path)
    try:
        (archived,) = raw.execute(
            "SELECT archived FROM contract WHERE symbol = 'MXU6'"
        ).fetchone()
        assert archived == 1
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute("UPDATE contract SET archived = 0 WHERE symbol = 'MXU6'")
    finally:
        raw.close()


# -- рубеж и архив ------------------------------------------------------------

def test_period_starts_are_the_roll_days_of_the_stitched_series() -> None:
    """Правило рубежа одно: начала периодов совпадают с отрезками `legs_of_chain`."""
    a = {date(2026, 1, d): 100.0 for d in range(1, 29)}
    b = {date(2026, 1, d): (10.0 if d < 15 else 300.0) for d in range(1, 29)}
    b |= {date(2026, 2, d): 300.0 for d in range(1, 28)}
    c = {date(2026, 2, d): (10.0 if d < 20 else 900.0) for d in range(1, 28)}
    chain = [("A", a), ("B", b), ("C", c)]

    rows = periods_of_chain(chain)
    legs = legs_of_chain(chain)

    assert [row.active_from for row in rows[1:]] == [leg.since for leg in legs]
    assert rows[0].active_from is None
    assert rows[1].active_to == legs[1].since - timedelta(days=1)
    assert rows[0].archived and rows[1].archived and rows[2].current


def test_mxz6_takes_over_on_17_september_and_mxu6_is_archived(store: CandleStore) -> None:
    """Настоящие дневные объёмы: MXU6 закрыт 16.09 и архивный, MXZ6 действующий с 17.09."""
    exchange = Exchange()
    rows = refresh_contracts(store, exchange.client, ["MXU6", "MXZ6"], market=FUTURES, now=NOW)

    by_symbol = {row.symbol: row for row in rows}
    assert by_symbol["MXU6"].active_to == date(2026, 9, 16)
    assert by_symbol["MXU6"].archived
    assert by_symbol["MXU6"].last_trade_day == date(2026, 9, 17)
    assert by_symbol["MXZ6"].active_from == date(2026, 9, 17)
    assert by_symbol["MXZ6"].current
    assert current_contract(store, today=NOW.date(), asset="MX").symbol == "MXZ6"


def test_short_refresh_keeps_the_start_written_by_the_whole_chain(store: CandleStore) -> None:
    """Уточнение по паре не стирает начало MXU6, известное из переноса цепочки."""
    store.put_contracts([ContractRow("MXU6", active_from=date(2026, 6, 8))], now=NOW)
    refresh_contracts(store, Exchange().client, ["MXU6", "MXZ6"], market=FUTURES, now=NOW)

    (mxu6,) = [row for row in store.contracts() if row.symbol == "MXU6"]
    assert mxu6.active_from == date(2026, 6, 8)
    assert mxu6.active_to == date(2026, 9, 16)


def test_expired_contract_is_not_given_out_as_current(store: CandleStore) -> None:
    """Открытый период у истёкшего контракта — отказ, а не торговля истёкшим кодом."""
    store.put_contracts(
        [ContractRow("MXU6", last_trade_day=date(2026, 9, 17), active_from=date(2026, 6, 8))],
        now=NOW,
    )
    with pytest.raises(ContractError, match="MXU6"):
        current_contract(store, today=NOW.date(), asset="MX")


def test_two_open_periods_are_refused(store: CandleStore) -> None:
    """Действующий — ровно один; два открытых периода — противоречие вслух."""
    store.put_contracts(
        [ContractRow("MXU6", active_from=date(2026, 6, 8)),
         ContractRow("MXZ6", active_from=date(2026, 9, 17))],
        now=NOW,
    )
    with pytest.raises(ContractError, match="несколько"):
        current_contract(store, today=NOW.date(), asset="MX")


# -- загрузка без хвоста ---------------------------------------------------

def _dated_mxz6(store: CandleStore, since: date) -> None:
    store.put_contracts([ContractRow("MXZ6", active_from=since)], now=NOW)


def test_load_does_not_fetch_the_tail_before_the_warmup_day(store: CandleStore) -> None:
    """С рубежа 17.09 и один день прогрева 16.09 — ни одного запроса раньше."""
    days = {date(2026, 9, d): 120 for d in (10, 11, 14, 15, 16, 17, 18)}
    exchange = Exchange(days)
    _dated_mxz6(store, date(2026, 9, 17))

    load = load_contract_minutes(
        store, exchange.client,
        ContractRequest("MXZ6", FUTURES, until=date(2026, 9, 18), warmup_bars=15, now=NOW),
    )

    assert min(since for since, _ in exchange.minute_requests()) == date(2026, 9, 16)
    assert store.trading_days("MXZ6") == [date(2026, 9, 16), date(2026, 9, 17), date(2026, 9, 18)]
    assert load.since == date(2026, 9, 16)
    assert load.warm_bars >= 15
    assert load.problem is None


def test_warmup_length_comes_from_the_caller_and_skips_the_weekend(
    store: CandleStore,
) -> None:
    """Прогрев длиннее одной сессии тянет ровно ещё один торговый день, выходные пропущены."""
    days = {date(2026, 9, d): 60 for d in (16, 17, 18, 21, 22)}
    exchange = Exchange(days)
    _dated_mxz6(store, date(2026, 9, 21))  # понедельник

    load = load_contract_minutes(
        store, exchange.client,
        ContractRequest("MXZ6", FUTURES, until=date(2026, 9, 22), warmup_bars=20, now=NOW),
    )

    # Сессия 60 минут = 12 пятиминуток: 20 баров — это пятница и четверг.
    assert load.since == date(2026, 9, 17)
    assert min(since for since, _ in exchange.minute_requests()) == date(2026, 9, 17)
    assert date(2026, 9, 16) not in store.trading_days("MXZ6")
    assert load.warm_bars == 24


def test_contract_without_a_start_is_not_loaded(store: CandleStore) -> None:
    """Нет начала периода — нет загрузки: откуда качать без хвоста, неизвестно."""
    exchange = Exchange({date(2026, 9, 17): 60})
    store.put_contracts([ContractRow("MXH7")], now=NOW)
    with pytest.raises(ContractError, match="MXH7"):
        load_contract_minutes(
            store, exchange.client,
            ContractRequest("MXH7", FUTURES, until=date(2026, 9, 22), warmup_bars=15, now=NOW),
        )
    assert exchange.transport.urls == []


# -- куски по периоду -------------------------------------------------------

def test_pieces_cut_the_period_at_the_mxu6_mxz6_seam(store: CandleStore) -> None:
    """Каждый день отрезка — у своего ближнего контракта, встык, без налезания."""
    store.put_contracts(
        [ContractRow("MXM6", active_from=date(2026, 3, 19), active_to=date(2026, 6, 7)),
         ContractRow("MXU6", active_from=date(2026, 6, 8), active_to=date(2026, 9, 16)),
         ContractRow("MXZ6", active_from=date(2026, 9, 17))],
        now=NOW,
    )
    assert pieces(store, date(2026, 9, 1), date(2026, 9, 25), asset="MX") == [
        Leg("MXU6", date(2026, 9, 1), date(2026, 9, 16)),
        Leg("MXZ6", date(2026, 9, 17), date(2026, 9, 25)),
    ]
    assert pieces(store, date(2026, 9, 17), date(2026, 9, 25), asset="MX") == [
        Leg("MXZ6", date(2026, 9, 17), date(2026, 9, 25)),
    ]
    assert pieces(store, date(2026, 7, 1), date(2026, 9, 16), asset="MX") == [
        Leg("MXU6", date(2026, 7, 1), date(2026, 9, 16)),
    ]


def test_overlapping_periods_are_refused(store: CandleStore) -> None:
    """Один день не принадлежит двум контрактам: налезание — отказ."""
    store.put_contracts(
        [ContractRow("MXU6", active_from=date(2026, 6, 8), active_to=date(2026, 9, 17)),
         ContractRow("MXZ6", active_from=date(2026, 9, 17))],
        now=NOW,
    )
    with pytest.raises(ContractError, match="налезают"):
        pieces(store, date(2026, 9, 1), date(2026, 9, 25), asset="MX")


# -- перенос цепочки --------------------------------------------------------

def _chain_source(path: pathlib.Path) -> None:
    """База цепочки: MXH6 уступает MXM6 12.01, плюс сшитый ряд, который не переносится."""
    with CandleStore(path) as chain:
        for day in range(5, 16):
            moment = date(2026, 1, day)
            if moment.weekday() >= 5:
                continue
            old, new = (50, 5) if day < 12 else (5, 50)
            chain.put_minutes("MXH6", [minute(msk(2026, 1, day, 10), volume=old)], Source.ISS)
            chain.put_minutes("MXM6", [minute(msk(2026, 1, day, 10), volume=new)], Source.ISS)
            chain.mark_days_requested("MXH6", [moment], now=NOW)
            chain.mark_days_requested("MXM6", [moment], now=NOW)
        chain.put_minutes("@X", [minute(msk(2026, 1, 5, 10))], Source.STITCHED)
    raw = sqlite3.connect(path)
    raw.execute("PRAGMA user_version = 5")
    raw.commit()
    raw.close()


def test_adopt_moves_minutes_and_day_marks_without_touching_the_source(
    tmp_path: pathlib.Path, store: CandleStore,
) -> None:
    """Перенос кладёт минуты и отметки дней, считает периоды, источник не меняется."""
    source = tmp_path / "chain.sqlite3"
    _chain_source(source)
    before = hashlib.sha256(source.read_bytes()).hexdigest()

    report = adopt_chain(source, store, ["MXH6", "MXM6"], now=NOW)

    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert report.minutes == {"MXH6": 9, "MXM6": 9}
    assert len(store.settled_days("MXM6")) == 9
    assert "@X" not in store.symbols()
    rows = {row.symbol: row for row in report.contracts}
    assert rows["MXH6"].active_to == date(2026, 1, 11)
    assert rows["MXM6"].active_from == date(2026, 1, 12)
    assert rows["MXM6"].current


def test_adopt_refuses_the_stitched_series(tmp_path: pathlib.Path, store: CandleStore) -> None:
    """Сшитый ряд — не контракт, в рабочую базу его не переносят."""
    source = tmp_path / "chain.sqlite3"
    _chain_source(source)
    with pytest.raises(ContractError, match="@X"):
        adopt_chain(source, store, ["MXH6", "@X"], now=NOW)


def test_adopt_command_fills_the_working_base(tmp_path: pathlib.Path) -> None:
    """Команда `python3 -m app.fetch --adopt` доходит до переноса, без сети."""
    source, target = tmp_path / "chain.sqlite3", tmp_path / "candles.sqlite3"
    _chain_source(source)
    code = fetch_main(["--legs", "MXH6,MXM6", "--adopt", "--no-fetch",
                       "--db", str(source), "--target", str(target)])
    assert code == 0
    with CandleStore(target) as working:
        rows = {row.symbol: row for row in working.contracts()}
        assert rows["MXM6"].active_from == date(2026, 1, 12)
        assert len(working.minutes("MXH6")) == 9
        # Без биржи срок обращения не проверен — действующим его не отдают.
        with pytest.raises(ContractError, match="не проверен"):
            current_contract(working, today=date(2026, 1, 20), asset="MX")


def test_open_contract_with_unknown_last_day_is_not_given_out(store: CandleStore) -> None:
    """Срок обращения не проверен — отказ: так выглядит истёкший MXU6 после переноса."""
    store.put_contracts([ContractRow("MXU6", active_from=date(2026, 6, 8))], now=NOW)
    with pytest.raises(ContractError, match="не проверен"):
        current_contract(store, today=NOW.date(), asset="MX")


# -- разбор ISS -------------------------------------------------------------

def test_a_shortened_trading_day_is_still_a_daily_volume() -> None:
    """Укороченный день — дневная свеча, а не отказ всей загрузки.

    Живой ответ ISS 27.09.2026 по MXH6: `2025-03-11 00:00:00 … 19:30:22`.
    Отказ на нём ронял кнопку «Загрузить историю» для любого MX-кода:
    MXH6 входит в цепочку вокруг MXU6.
    """
    columns = ["open", "close", "high", "low", "value", "volume", "begin", "end"]
    body = json.dumps({"candles": {"columns": columns, "data": [
        [1, 1, 1, 1, 0, 42, "2025-03-11 00:00:00", "2025-03-11 19:30:22"],
    ]}}).encode()
    assert parse_daily_volumes(body) == {date(2025, 3, 11): 42.0}


def test_daily_volumes_refuse_rows_shorter_than_a_day() -> None:
    """Минутки под видом дневных не становятся дневным объёмом."""
    body = minute_body([minute(msk(2026, 9, 17, 10), volume=7)])
    with pytest.raises(IssPayloadError, match="короче суток"):
        parse_daily_volumes(body)


def test_last_trade_day_comes_from_the_description() -> None:
    """Последний день обращения — из описания, которое отвечает и по истёкшим."""
    client = IssClient(FakeTransport(lambda _url: description_body("2026-09-17")),
                       sleep=no_sleep)
    assert client.last_trade_day("MXU6") == date(2026, 9, 17)


# -- цепочки разных активов в одной таблице (ревью 27.09.2026, находка 1) ----

#: Цепочка MX после загрузки MXZ6: MXU6 архивный, MXZ6 действующий.
MX_ROWS = [
    ContractRow("MXU6", last_trade_day=date(2026, 9, 17),
                active_from=date(2026, 6, 18), active_to=date(2026, 9, 16)),
    ContractRow("MXZ6", last_trade_day=date(2026, 12, 17), active_from=date(2026, 9, 17)),
]


def test_second_asset_in_the_table_does_not_break_the_first(store: CandleStore) -> None:
    """Сценарий Б: загрузка RI пишет свою цепочку рядом — склейка и действующий MX целы.

    До правки `pieces` читал всю таблицу: «периоды MXU6 и RIU6 налезают»,
    а `current_contract` — «действующих несколько».
    """
    store.put_contracts(MX_ROWS, now=NOW)
    exchange = Exchange(
        daily={"RIU6": MXU6_DAYS, "RIZ6": MXZ6_DAYS},
        known={"RIU6": "2026-09-17", "RIZ6": "2026-12-17"},
    )
    refresh_contracts(store, exchange.client, ["RIU6", "RIZ6"], market=FUTURES, now=NOW)

    assert pieces(store, date(2026, 9, 1), date(2026, 9, 25), asset="MX") == [
        Leg("MXU6", date(2026, 9, 1), date(2026, 9, 16)),
        Leg("MXZ6", date(2026, 9, 17), date(2026, 9, 25)),
    ]
    assert [leg.symbol for leg in pieces(
        store, date(2026, 9, 1), date(2026, 9, 25), asset="RI"
    )] == ["RIZ6"]
    assert current_contract(store, today=NOW.date(), asset="MX").symbol == "MXZ6"
    assert current_contract(store, today=NOW.date(), asset="RI").symbol == "RIZ6"


def test_asset_without_its_chain_is_refused_not_cut_by_another(store: CandleStore) -> None:
    """Сценарий А: в таблице только MX — нарезка RI отказывает вслух, кусков MX не отдаёт."""
    store.put_contracts(MX_ROWS, now=NOW)
    with pytest.raises(ContractError, match="нет цепочки RI"):
        pieces(store, date(2026, 9, 1), date(2026, 9, 25), asset="RI")
    with pytest.raises(ContractError, match="RI"):
        current_contract(store, today=NOW.date(), asset="RI")


def test_monthly_asset_is_refused_before_anything_is_written(store: CandleStore) -> None:
    """BR: биржа знает BRQ6 — квартальная цепочка BRU6 → BRZ6 ложна, таблица не тронута."""
    exchange = Exchange(
        daily={"BRU6": MXU6_DAYS, "BRZ6": MXZ6_DAYS},
        known={"BRU6": "2026-08-31", "BRQ6": "2026-07-31", "BRZ6": "2026-11-30"},
    )
    with pytest.raises(ChainNotQuarterly, match="BRQ6"):
        refresh_contracts(store, exchange.client, ["BRU6", "BRZ6"], market=FUTURES, now=NOW)
    assert store.contracts() == []


def test_monthly_check_asks_the_month_before_the_first_code(store: CandleStore) -> None:
    """Дальний месячный код биржа могла ещё не перечислить — смотрится прошлый.

    Биржа знает только BRK6 (перед BRM6): этого хватает для отказа.
    """
    exchange = Exchange(
        daily={"BRM6": MXU6_DAYS, "BRU6": MXZ6_DAYS, "BRZ6": MXZ6_DAYS, "BRH7": MXZ6_DAYS},
        known={"BRK6": "2026-04-30"},
    )
    with pytest.raises(ChainNotQuarterly, match="BRK6"):
        refresh_contracts(
            store, exchange.client, ["BRM6", "BRU6", "BRZ6", "BRH7"], market=FUTURES, now=NOW
        )
    assert store.contracts() == []


def test_quarterly_asset_passes_the_monthly_check(store: CandleStore) -> None:
    """MX: промежуточного MXQ6 биржа не знает — цепочка подтверждена, строки записаны."""
    exchange = Exchange()
    refresh_contracts(store, exchange.client, ["MXU6", "MXZ6"], market=FUTURES, now=NOW)
    asked = [url for url in exchange.transport.urls if "/securities/MXQ6.json" in url]
    assert len(asked) == 1
    assert {row.symbol for row in store.contracts()} == {"MXU6", "MXZ6"}


# -- рубеж не ставится по незакрытому дню (находка 4) -------------------------

def test_todays_unfinished_day_does_not_set_a_roll(store: CandleStore) -> None:
    """Утром MXH7 обогнал MXZ6 по неполной свече — рубежа нет, MXZ6 остаётся действующим.

    По сегодняшней свече рубеж встал бы на 27.09, и `put_contracts` назавтра
    его бы не снял: старый ближний так и числился бы архивным.
    """
    old = {date(2026, 9, day): 100.0 for day in (21, 22, 23, 24, 25, 26, 27)}
    new = {date(2026, 9, day): 10.0 for day in (21, 22, 23, 24, 25, 26)}
    new[date(2026, 9, 27)] = 500.0
    exchange = Exchange(daily={"MXZ6": old, "MXH7": new}, known={"MXH7": "2027-03-18"})

    rows = refresh_contracts(store, exchange.client, ["MXZ6", "MXH7"], market=FUTURES, now=NOW)

    by_symbol = {row.symbol: row for row in rows}
    assert by_symbol["MXZ6"].active_to is None
    assert by_symbol["MXH7"].active_from is None


# -- перенос без биржи: архивный — архивный и без срока (находка 5) -----------

def test_adopted_archived_leg_is_refused_without_a_last_trade_day(
    tmp_path: pathlib.Path,
) -> None:
    """После `--adopt --no-fetch` у MXH6 нет срока, но период закрыт — это ARCHIVED, не UNKNOWN."""
    source, target = tmp_path / "chain.sqlite3", tmp_path / "candles.sqlite3"
    _chain_source(source)
    assert fetch_main(["--legs", "MXH6,MXM6", "--adopt", "--no-fetch",
                       "--db", str(source), "--target", str(target)]) == 0
    with CandleStore(target) as working:
        rows = working.contracts()
    (mxh6,) = [row for row in rows if row.symbol == "MXH6"]
    assert mxh6.last_trade_day is None and mxh6.active_to is not None

    verdict = expiry_verdict(rows, "MXH6", today=date(2026, 1, 20), halt_days=1)

    assert verdict.kind is Expiry.ARCHIVED
    assert verdict.kind.refused
