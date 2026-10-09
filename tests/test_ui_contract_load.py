"""Кнопка «Загрузить историю» для квартального фьючерса — по контракту (решение 0061).

Что стережётся, словами
-----------------------
1. **Хвост до рубежа не качается.** Кнопка по коду `MXZ6` уточняет рубежи
   у биржи по дневным объёмам и просит минуты только с рубежа плюс день
   прогрева перед ним. День из «хвоста» (минуты MXZ6 до рубежа, когда
   ближним был MXU6) не запрашивается ни разу.
2. **Ход и конец доходят до окна** (правило 13): полоска хода получает
   сигналы, конец приходит всегда — и на успехе, и на отказе.
3. **Отказ не молчит.** Контракт без рубежа (ещё не ближний) — конец
   с причиной словами и ни одного запроса минут.
4. **Часы — порта, а не машины** (`B-049`): три разных «сегодня», правая
   граница запроса — ровно названный день.
5. **Программа тикер не меняет** (решение 0016): после загрузки окно
   узнаёт действующий контракт, журнал говорит о расхождении, а настройка
   инструмента остаётся прежней.
6. **График по периодам**: до рубежа показан MXU6, после — MXZ6, на стыке
   метка; свечи прогрева MXZ6 до рубежа на график не идут.

Сеть — заглушкой: биржа отвечает по адресу запроса (`Chain.answer`).
"""

from __future__ import annotations

import asyncio
import json
import math
import pathlib
import threading
import time as time_module
import urllib.parse
from collections.abc import Callable, Sequence
from datetime import date, datetime, time, timedelta

import pytest

from app.port import HistoryPort
from market import (
    MINUTE,
    MSK,
    WARMUP_LOOK_BACK_DAYS,
    CandleStore,
    HttpxTransport,
    IssClient,
    MarketWorker,
    Source,
    chain_around,
)
from market import (
    Candle as MarketCandle,
)
from market.journal import redact
from ui.models import (
    BacktestOptions,
    BacktestReport,
    BacktestRequest,
    ChartData,
    ContractNotice,
    DecisionLevel,
    DecisionRow,
    HistoryLoadOutcome,
    HistoryLoadRequest,
    Settings,
    TradeRow,
)

TODAY = date(2026, 9, 23)

#: Рубеж MXZ6 и MXU6 в подставной цепочке — за шесть дней до «сегодня».
ROLL_BACK = 6


@pytest.fixture(autouse=True)
def the_real_transport_is_disarmed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Настоящий транспорт биржи обезврежен на весь файл (см. `B-034`)."""

    def refuse(self: object, url: str, *, timeout: float) -> bytes:
        raise AssertionError(f"тест полез в настоящий интернет: {url}")

    monkeypatch.setattr(HttpxTransport, "get", refuse)


def _volumes(today: date) -> dict[str, dict[date, float]]:
    """Дневные объёмы цепочки MXM6 → MXU6 → MXZ6 → MXH7.

    Рубеж MXU6 — `today - 40`, рубеж MXZ6 — `today - ROLL_BACK`; MXH7
    ещё не обогнал MXZ6, значит, действующий — MXZ6.
    """
    table: dict[str, dict[date, float]] = {
        code: {} for code in ("MXM6", "MXU6", "MXZ6", "MXH7", "MXM7")
    }
    for back in range(60, -1, -1):
        day = today - timedelta(days=back)
        table["MXM6"][day] = 1000.0 if back > 40 else 1.0
        table["MXU6"][day] = 10.0 if back > 40 else (1000.0 if back > ROLL_BACK else 10.0)
        table["MXZ6"][day] = 5.0 if back > ROLL_BACK else 2000.0
        table["MXH7"][day] = 1.0
        table["MXM7"][day] = 1.0
    return table


def _daily_body(days: dict[date, float]) -> bytes:
    rows = [
        [1, 1, 1, 1, 0, volume, f"{day} 00:00:00", f"{day} 23:59:59"]
        for day, volume in sorted(days.items())
    ]
    columns = ["open", "close", "high", "low", "value", "volume", "begin", "end"]
    return json.dumps({"candles": {"columns": columns, "data": rows}}).encode()


def _description_body(last: date | None) -> bytes:
    """Описание кода; `None` — биржа кода не знает, описание пусто."""
    rows = (
        [["SECID", "Код", "X", "string"], ["LSTTRADE", "Последний день", f"{last}", "date"]]
        if last is not None else []
    )
    columns = ["name", "title", "value", "type"]
    return json.dumps({"description": {"columns": columns, "data": rows}}).encode()


def _minute_body(day: date, count: int, price: float, *, wave: float = 0.0) -> bytes:
    """Минуты одной сессии подставной биржи.

    `wave` — размах качелей цены, период 40 минут: на ровной цене средняя
    ни с чем не пересекается, и сделок нет вовсе.
    """
    # Качели начинаются с 09:00: прогрев средней кончается к 10:15, и до
    # конца торгового окна остаётся больше часа на сделки.
    start = datetime.combine(day, time(9 if wave else 10, 0), MSK)
    prices = [price + wave * math.sin(2 * math.pi * i / 40) for i in range(count)]
    rows = [
        [one, one, one + 1, one - 1, 0, 1,
         f"{start + timedelta(minutes=i):%Y-%m-%d %H:%M:%S}",
         f"{start + timedelta(minutes=i, seconds=59):%Y-%m-%d %H:%M:%S}"]
        for i, one in enumerate(prices)
    ]
    columns = ["open", "close", "high", "low", "value", "volume", "begin", "end"]
    return json.dumps({"candles": {"columns": columns, "data": rows}}).encode()


class _Transport:
    """Подставной транспорт: ответ по адресу и список адресов, что спросили.

    Свой, а не общий из `market_helpers`: тот же десяток строк, зато файл
    проверяется типами целиком (`mypy` не видит каталог проверок).
    """

    def __init__(self, handler: Callable[[str], bytes]) -> None:
        self._handler = handler
        self.urls: list[str] = []

    def get(self, url: str, *, timeout: float) -> bytes:
        self.urls.append(url)
        return self._handler(url)


def _no_sleep(_seconds: float) -> None:
    return None


class Chain:
    """Подставная биржа: описание, дневные объёмы и минуты — по адресу запроса.

    Минуты MXZ6 есть **каждый день** последних двух месяцев, включая хвост
    до рубежа: если загрузка о нём попросит, биржа отдаст — и проверка это
    увидит в `minute_spans`, а не в пустом ответе.
    """

    def __init__(self, today: date, *, delay: float = 0.0, wave: float = 0.0) -> None:
        self.today = today
        self.delay = delay
        self.wave = wave
        self.volumes = _volumes(today)
        self.transport = _Transport(self.answer)
        self.client = IssClient(self.transport, sleep=_no_sleep, pause=0.0)

    def answer(self, url: str) -> bytes:
        parsed = urllib.parse.urlparse(url)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        if query.get("iss.only") == "description":
            code = parsed.path.rsplit("/", 1)[-1].removesuffix(".json")
            # Месячного MXX6 биржа не знает — цепочка MX квартальная.
            return _description_body(
                self.today + timedelta(days=80) if code in self.volumes else None
            )
        if "interval" not in query:
            return b"{}"
        code = parsed.path.rsplit("/", 2)[-2]
        if query["interval"] == "24":
            return _daily_body(self.volumes[code] if query.get("start", "0") == "0" else {})
        if self.delay:
            time_module.sleep(self.delay)
        if query.get("start", "0") != "0":
            return _minute_body(self.today, 0, 0.0)
        since = date.fromisoformat(query["from"])
        till = date.fromisoformat(query["till"])
        if since != till:
            # Загрузка с рубежа: по одной сессии на каждый день отрезка.
            rows: list[list[object]] = []
            day = since
            while day <= till:
                rows += json.loads(
                    _minute_body(day, 120, 500.0, wave=self.wave)
                )["candles"]["data"]
                day += timedelta(days=1)
            columns = ["open", "close", "high", "low", "value", "volume", "begin", "end"]
            return json.dumps({"candles": {"columns": columns, "data": rows}}).encode()
        return _minute_body(since, 120, 500.0, wave=self.wave)

    def minute_spans(self) -> list[tuple[date, date]]:
        spans = []
        for url in self.transport.urls:
            query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
            if query.get("interval") == "1":
                spans.append((date.fromisoformat(query["from"]), date.fromisoformat(query["till"])))
        return spans


def _noon(day: date):
    moment = datetime.combine(day, time(12, 0), MSK)
    return lambda: moment


class Heard:
    def __init__(self, port: HistoryPort) -> None:
        self.progress: list[tuple[int, str]] = []
        self.finished: list[HistoryLoadOutcome] = []
        self.notes: list[DecisionRow] = []
        self.contracts: list[ContractNotice] = []
        self.charts: list[ChartData] = []
        port.history_progress.connect(lambda p, what: self.progress.append((p, what)))
        port.history_finished.connect(self.finished.append)
        port.decision_appended.connect(self.notes.append)
        port.contract_checked.connect(self.contracts.append)
        port.chart_replaced.connect(self.charts.append)
        self.trades: list[tuple[TradeRow, ...]] = []
        self.reports: list[BacktestReport] = []
        port.trades_replaced.connect(lambda rows, _summary: self.trades.append(rows))
        port.backtest_finished.connect(self.reports.append)


async def _settle(heard: Heard, rounds: int = 400) -> None:
    """Дождаться конца загрузки и сведений о контракте — по сигналам окна."""
    for _ in range(rounds):
        if heard.finished and heard.contracts:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("загрузка не закончилась за отведённое время")


def _run(loop, database: pathlib.Path, chain: Chain, work, *, instrument: str = "MXU6"):
    async def main():
        worker = MarketWorker(database, iss=chain.client)
        if database.exists():
            await worker.open()
        port = HistoryPort(worker, values=Settings(instrument=instrument), days=0,
                           sanitize=redact, clock=_noon(chain.today))
        heard = Heard(port)
        try:
            await work(port, heard)
        finally:
            await port.aclose()
            await worker.close()
        return port, heard

    return loop.run_until_complete(main())


def _load(symbol: str):
    async def work(port: HistoryPort, heard: Heard) -> None:
        port.load_history(HistoryLoadRequest(symbol=symbol, days=90))
        await _settle(heard)

    return work


# ---------------------------------------------------------------------------


def test_the_button_loads_the_contract_from_its_roll_and_not_the_tail(loop, tmp_path) -> None:
    """Стережёт 1 и 2: пустая папка → кнопка → MXZ6 с рубежа, без хвоста.

    Мутация, которую тест ловит: вернуть кнопку на загрузку «90 дней» —
    тогда уйдёт запрос с левой границей за два месяца до рубежа.
    """
    database = tmp_path / "userdata" / "candles.sqlite3"
    database.parent.mkdir()
    chain = Chain(TODAY)

    _, heard = _run(loop, database, chain, _load("MXZ6"))

    roll = TODAY - timedelta(days=ROLL_BACK)
    spans = chain.minute_spans()
    assert spans, "к бирже не ушло ни одного запроса минут — проверка вакуумна"
    earliest = min(since for since, _ in spans)
    assert earliest >= roll - timedelta(days=1), (
        f"загрузка попросила хвост до рубежа: с {earliest}, рубеж {roll}"
    )
    assert max(till for _, till in spans) == TODAY
    assert heard.finished, "конец загрузки в окно не пришёл"
    assert heard.finished[-1].ok, heard.finished[-1].trouble
    assert heard.progress, "ход загрузки в окно не пришёл"
    with CandleStore(database) as store:
        first = store.coverage("MXZ6").first
        assert first is not None and first.date() == roll - timedelta(days=1), first


@pytest.mark.parametrize(
    "today", [date(2024, 1, 15), TODAY, date(2031, 6, 2)],
    ids=["years-back", "written", "years-ahead"],
)
def test_the_contract_load_takes_today_from_the_ports_clock(loop, tmp_path, today) -> None:
    """Стережёт 4 (`B-049`): правая граница — «сегодня» из часов порта."""
    chain = Chain(today)

    _, heard = _run(loop, tmp_path / f"{today:%Y%m%d}.sqlite3", chain, _load("MXZ6"))

    assert heard.finished and heard.finished[-1].ok, heard.finished[-1].trouble
    assert max(till for _, till in chain.minute_spans()) == today
    stamped = {row.time.date() for row in heard.notes}
    assert stamped == {today}, f"журнал датирован не часами порта: {sorted(stamped)}"


def test_a_code_the_exchange_does_not_know_at_the_chain_edge_is_said_in_the_journal(
    loop, tmp_path
) -> None:
    """`B-054`, правило 13: дальний код цепочки бирже не известен — строка в журнале.

    Цепочка MXZ6 — MXM6, MXU6, MXZ6, MXH7; биржа не знает MXH7 (ещё не
    вышел). Слой данных выпускает его без отказа, загрузка идёт по
    остальным — и человек обязан узнать, что по MXH7 ничего не уточнено.

    Мутация, которую тест ловит: порт не читает `said` у ответа
    уточнения (`_refresh_chain` без цикла `note`) — загрузка удачна,
    журнал молчит.
    """
    from ui.models import DecisionLevel

    chain = Chain(TODAY)
    del chain.volumes["MXH7"]

    _, heard = _run(loop, tmp_path / "base.sqlite3", chain, _load("MXZ6"))

    assert heard.finished and heard.finished[-1].ok, heard.finished[-1].trouble
    warned = [
        row for row in heard.notes
        if row.level is DecisionLevel.WARNING and "MXH7" in row.reason
    ]
    assert warned, (
        "биржа не знает MXH7, а журнал окна об этом молчит: "
        f"{[(row.event, row.reason) for row in heard.notes]}"
    )


def test_a_contract_without_a_roll_is_refused_aloud_and_loads_nothing(loop, tmp_path) -> None:
    """Стережёт 3: рубежа нет — отказ словами, ни одного запроса минут.

    MXH7 ещё не обогнал MXZ6. Молчание здесь выглядело бы как «загрузилось,
    но пусто», а отказ «недокачанные объёмы» — как поломка биржи.
    """
    chain = Chain(TODAY)

    _, heard = _run(loop, tmp_path / "base.sqlite3", chain, _load("MXH7"))

    assert chain.minute_spans() == [], "минуты запрошены, хотя рубежа нет"
    assert heard.finished, "отказ до окна не дошёл"
    outcome = heard.finished[-1]
    assert not outcome.ok
    assert outcome.headline == "Загрузка не начата", outcome
    assert "не стал ближним" in outcome.trouble, outcome.trouble
    assert "Действующий сейчас по данным биржи — MXZ6" in outcome.trouble, outcome.trouble


def test_after_the_load_the_window_hears_the_current_contract_and_the_ticker_stays(
    loop, tmp_path
) -> None:
    """Стережёт 5: окно узнаёт действующий MXZ6, настройка остаётся MXU6."""
    chain = Chain(TODAY)

    _, heard = _run(loop, tmp_path / "base.sqlite3", chain, _load("MXZ6"))

    assert heard.contracts, "сведения о действующем контракте в окно не пришли"
    notice = heard.contracts[-1]
    assert notice.current == "MXZ6" and notice.mismatch, notice
    assert notice.configured == "MXU6", "программа сама сменила торгуемый тикер"
    said = [row for row in heard.notes if row.event == "Действующий контракт сменился"]
    assert said and "MXZ6" in said[-1].reason, "журнал о расхождении промолчал"


def test_the_chart_shows_each_period_with_its_own_contract_and_marks_the_seam(
    loop, tmp_path
) -> None:
    """Стережёт 6: MXU6 до рубежа, MXZ6 после, метка на стыке, скачок не сглажен.

    Цены контрактов разведены (100 и 500), поэтому по цене свечи видно,
    чей это кусок. Мутации: показ без прежних контрактов (нет свечей по 100),
    показ прогрева MXZ6 до рубежа (свеча 500 раньше рубежа), потеря метки.
    """
    database = tmp_path / "base.sqlite3"
    roll = TODAY - timedelta(days=ROLL_BACK)
    with CandleStore(database) as store:
        for back in (9, 8, 7):
            day = TODAY - timedelta(days=back)
            start = datetime.combine(day, time(10, 0), MSK)
            store.put_minutes(
                "MXU6",
                [
                    MarketCandle(time=start + timedelta(minutes=i), open=100.0, high=100.0,
                                 low=100.0, close=100.0, volume=1.0, timeframe=MINUTE,
                                 filled_minutes=1)
                    for i in range(120)
                ],
                Source.ISS,
            )
            store.mark_days_requested("MXU6", [day], counts={day: 120},
                                      now=datetime.combine(TODAY, time(12), MSK))
    chain = Chain(TODAY)

    async def work(port: HistoryPort, heard: Heard) -> None:
        port.load_history(HistoryLoadRequest(symbol="MXZ6"))
        await _settle(heard)
        port.refresh("проверка")
        await port.wait()

    _, heard = _run(loop, database, chain, work, instrument="MXZ6")

    full = [chart for chart in heard.charts if chart.candles]
    assert full, "график так и не получил свечей"
    chart = full[-1]
    border = datetime.combine(roll, time(0), MSK)
    before = [candle for candle in chart.candles if candle.opens_at < border]
    after = [candle for candle in chart.candles if candle.opens_at >= border]
    assert before and all(candle.open == 100.0 for candle in before), (
        "до рубежа показан не MXU6 (или не показан вовсе)"
    )
    assert after and all(candle.open == 500.0 for candle in after), "после рубежа не MXZ6"
    assert [(seam.previous, seam.symbol) for seam in chart.seams] == [("MXU6", "MXZ6")]
    assert chart.seams[0].time == after[0].opens_at, "метка стыка не на первой свече MXZ6"
    assert all(marker.time >= border for marker in chart.markers), (
        "метки прогона MXZ6 легли на свечи MXU6"
    )
    zs = [point for point in chart.average if point.time >= border]
    assert zs, "линии средней MXZ6 нет вовсе"
    assert all(
        point.time >= border or point.value < 200.0 for point in chart.average
    ), "средняя MXZ6 по барам прогрева легла на свечи MXU6"


@pytest.mark.parametrize(
    ("code", "chain"),
    [
        ("MXZ6", ["MXM6", "MXU6", "MXZ6", "MXH7"]),
        ("MXH0", ["MXU9", "MXZ9", "MXH0", "MXM0"]),
    ],
)
def test_chain_around_walks_the_quarters_across_the_year(code, chain) -> None:
    """Соседи по кварталам, с переходом через год и через десятилетие."""
    assert chain_around(code) == chain


def test_a_monthly_code_has_no_quarter_chain() -> None:
    """Код не квартального фьючерса — отказ, а не выдуманные соседи."""
    from market import ContractError

    with pytest.raises(ContractError):
        chain_around("BRX6")


def test_run_marks_before_the_roll_are_not_drawn_over_the_old_contract() -> None:
    """До рубежа на графике чужой контракт: метки прогона оттуда снимаются.

    Прогон и журнал сделок не меняются — снимается только рисунок.
    """
    from app.contract_view import trim_before
    from ui.models import LinePoint, Marker, MarkerKind, Shade, ShadeKind, Side, TradePath

    border = datetime.combine(TODAY, time(0), MSK)
    early, late = border - timedelta(hours=2), border + timedelta(hours=11)
    data = ChartData(
        average=(LinePoint(early, 1.0), LinePoint(late, 2.0)),
        markers=(
            Marker(time=early, price=1.0, kind=next(iter(MarkerKind))),
            Marker(time=late, price=1.0, kind=next(iter(MarkerKind))),
        ),
        paths=(
            TradePath(entry_time=early, entry_price=1.0, exit_time=late,
                      exit_price=2.0, side=next(iter(Side))),
        ),
        shades=(Shade(start=early, end=late, kind=ShadeKind.PAUSE),),
    )

    got = trim_before(border, data)

    assert [point.time for point in got.average] == [late]
    assert [marker.time for marker in got.markers] == [late]
    assert got.paths == ()
    assert [(shade.start, shade.end) for shade in got.shades] == [(border, late)]
    assert trim_before(None, data) == data


def test_closing_the_program_during_a_load_starts_no_new_work(loop, tmp_path) -> None:
    """Закрытие посреди загрузки не заводит чтение таблицы после себя.

    Мутация, которую тест ловит: сверка контракта в `finally` загрузки —
    тогда отмена при выходе заводит новую задачу уже после того, как
    закрытие собрало свои, и та пишет в окно, которого нет.
    """
    chain = Chain(TODAY, delay=0.05)

    async def work(port: HistoryPort, heard: Heard) -> None:
        port.load_history(HistoryLoadRequest(symbol="MXZ6"))
        for _ in range(400):
            if chain.minute_spans():
                break
            await asyncio.sleep(0.01)
        assert chain.minute_spans(), "загрузка минут не началась — проверять нечего"
        await port.aclose()
        told = len(heard.contracts)
        await asyncio.sleep(0.5)
        assert len(heard.contracts) == told, "после закрытия в окно пришли сведения"

    _run(loop, tmp_path / "base.sqlite3", chain, work)


def test_loading_anew_re_requests_the_contract_period_and_plain_load_does_not(
    loop, tmp_path
) -> None:
    """«Загрузить заново» для контракта снимает отметки с даты из окна.

    Без него уже загруженные дни не перезапрашиваются. Дата раньше рубежа
    с прогревом прижимается к нему; дата позже рубежа рубеж не трогает.
    """
    chain = Chain(TODAY)
    roll = TODAY - timedelta(days=ROLL_BACK)
    asked: dict[str, list[tuple[date, date]]] = {}

    async def work(port: HistoryPort, heard: Heard) -> None:
        for name, request in (
            ("first", HistoryLoadRequest(symbol="MXZ6")),
            ("plain", HistoryLoadRequest(symbol="MXZ6")),
            ("recent", HistoryLoadRequest(symbol="MXZ6", replace=True,
                                          since=TODAY - timedelta(days=1))),
            ("anew", HistoryLoadRequest(symbol="MXZ6", replace=True,
                                        since=TODAY - timedelta(days=3650))),
        ):
            before = len(chain.minute_spans())
            heard.finished.clear()
            heard.contracts.clear()
            port.load_history(request)
            await _settle(heard)
            asked[name] = chain.minute_spans()[before:]

    _run(loop, tmp_path / "base.sqlite3", chain, work)

    def covers(spans: list[tuple[date, date]], day: date) -> bool:
        return any(since <= day <= till for since, till in spans)

    assert covers(asked["first"], roll)
    assert not covers(asked["plain"], roll), "догрузка перезапросила загруженный рубеж"
    assert not covers(asked["recent"], roll), "«заново с даты» не учло дату из окна"
    assert covers(asked["recent"], TODAY - timedelta(days=1)), (
        "«заново с даты» не перезапросило выбранный день"
    )
    assert covers(asked["anew"], roll), "«заново» не перезапросило период контракта"
    assert covers(asked["anew"], roll - timedelta(days=1)), "«заново» забыло прогрев"


def test_loading_anew_from_an_old_date_keeps_marks_before_the_roll(loop, tmp_path) -> None:
    """Дата «заново» раньше рубежа прижимается к рубежу с прогревом.

    Отметки дней раньше этого края загрузка по контракту не восстанавливает:
    снятые, они остались бы снятыми, и день, за который уже спрашивали,
    числился бы недокачанным.
    """
    chain = Chain(TODAY)
    roll = TODAY - timedelta(days=ROLL_BACK)
    old = roll - timedelta(days=WARMUP_LOOK_BACK_DAYS + 5)
    database = tmp_path / "base.sqlite3"
    with CandleStore(database) as store:
        store.mark_days_requested("MXZ6", [old], now=_noon(TODAY)())

    async def work(port: HistoryPort, heard: Heard) -> None:
        for request in (
            HistoryLoadRequest(symbol="MXZ6"),
            HistoryLoadRequest(symbol="MXZ6", replace=True,
                               since=TODAY - timedelta(days=3650)),
        ):
            heard.finished.clear()
            heard.contracts.clear()
            port.load_history(request)
            await _settle(heard)

    _run(loop, database, chain, work, instrument="MXZ6")
    with CandleStore(database) as store:
        assert old in store.settled_days("MXZ6"), (
            "«заново» с давней даты сняло отметки раньше рубежа с прогревом"
        )


def test_a_run_asked_during_the_load_waits_for_it_and_says_so(loop, tmp_path) -> None:
    """«Применить» посреди загрузки минут: прогон не идёт по недогруженной базе.

    Поток сети отдельно от базы (01.10.2026), и прогон прошёл бы между
    страницами загрузки — сделки по части периода без пометки. Он
    откладывается, человеку сказано почему, и идёт сам после загрузки.
    """
    chain = Chain(TODAY, delay=0.05)

    async def work(port: HistoryPort, heard: Heard) -> None:
        port.load_history(HistoryLoadRequest(symbol="MXZ6"))
        for _ in range(400):
            if port._history.filling:  # noqa: SLF001
                break
            await asyncio.sleep(0.01)
        assert port._history.filling, "загрузка минут не началась"  # noqa: SLF001
        port.refresh("проверка")
        assert port._task is None or port._task.done(), (  # noqa: SLF001
            "прогон пошёл посреди загрузки минут"
        )
        assert any("Прогон пойдёт сам" in note.reason for note in heard.notes), (
            "отложенный прогон не назван человеку"
        )
        await _settle(heard)
        charts = len(heard.charts)
        for _ in range(400):
            if len(heard.charts) > charts or (
                port._task is not None and not port._task.done()  # noqa: SLF001
            ):
                return
            await asyncio.sleep(0.01)
        raise AssertionError("отложенный прогон не пошёл после загрузки")

    _run(loop, tmp_path / "base.sqlite3", chain, work, instrument="MXZ6")


def test_a_hanging_exchange_does_not_hold_the_contract_check(loop, tmp_path) -> None:
    """Биржа повисла на уточнении контрактов — сверка по базе всё равно отвечает.

    Случай 01.10.2026, свежая сборка под Windows: уточнение у биржи заняло
    поток базы, и «чтение таблицы контрактов», прогон и перерисовка встали
    за ним с плашкой «не отвечает». Сверка читает только базу и ждать
    биржу не обязана.
    """
    chain = Chain(TODAY)
    held = threading.Event()
    asked = threading.Event()
    answer = chain.answer

    def hanging(url: str) -> bytes:
        asked.set()
        held.wait(10)
        return answer(url)

    chain.transport = _Transport(hanging)
    chain.client = IssClient(chain.transport, sleep=_no_sleep, pause=0.0)
    database = tmp_path / "base.sqlite3"
    CandleStore(database).close()

    async def work(port: HistoryPort, heard: Heard) -> None:
        try:
            port.sync_contracts("проверка")
            for _ in range(200):
                if asked.is_set():
                    break
                await asyncio.sleep(0.01)
            assert asked.is_set(), "уточнение у биржи не началось"
            assert "уточнение контрактов у биржи" in port._busy_with("load"), (  # noqa: SLF001
                "уточнение контрактов названо человеку чужим именем"
            )
            port.check_contract()
            for _ in range(200):
                if heard.contracts:
                    return
                await asyncio.sleep(0.01)
            raise AssertionError("сверка контракта ждала повисшую биржу")
        finally:
            held.set()

    _run(loop, database, chain, work, instrument="MXZ6")


def _seed_mxu6(database: pathlib.Path, backs: Sequence[int], *, wave: float = 0.0) -> None:
    """Минуты MXU6 за названные дни назад — цена около 100, как у старого контракта."""
    with CandleStore(database) as store:
        for back in backs:
            day = TODAY - timedelta(days=back)
            start = datetime.combine(day, time(10, 0), MSK)
            store.put_minutes(
                "MXU6",
                [
                    MarketCandle(
                        time=start + timedelta(minutes=i),
                        open=100.0 + wave * math.sin(2 * math.pi * i / 40),
                        high=101.0 + wave, low=99.0 - wave,
                        close=100.0 + wave * math.sin(2 * math.pi * i / 40),
                        volume=1.0, timeframe=MINUTE, filled_minutes=1,
                    )
                    for i in range(120)
                ],
                Source.ISS,
            )
            store.mark_days_requested("MXU6", [day], counts={day: 120},
                                      now=datetime.combine(TODAY, time(12), MSK))


def test_the_main_window_run_does_not_trade_on_the_warmup_day(loop, tmp_path) -> None:
    """Стережёт `B-053`: прогон главного окна по MXZ6 идёт по склейке.

    День перед рубежом (прогрев MXZ6) в сделки не входит: ни одна сделка
    таблицы не открыта в этот день. Цена качается, поэтому сделки есть
    вообще — иначе проверка была бы пустой. Мутация: прогон по всем барам
    MXZ6 без склейки — сделки дня прогрева возвращаются.
    """
    database = tmp_path / "base.sqlite3"
    _seed_mxu6(database, (9, 8, 7))
    chain = Chain(TODAY, wave=30.0)
    warm = TODAY - timedelta(days=ROLL_BACK + 1)

    async def work(port: HistoryPort, heard: Heard) -> None:
        port.load_history(HistoryLoadRequest(symbol="MXZ6"))
        await _settle(heard)
        heard.trades.clear()
        port.refresh("проверка")
        await port.wait()

    _, heard = _run(loop, database, chain, work, instrument="MXZ6")

    assert heard.trades, "таблица сделок не пришла"
    rows = heard.trades[-1]
    assert rows, "сделок нет вовсе — проверка пуста"
    days = sorted({row.entry_time.astimezone(MSK).date() for row in rows})
    assert warm not in days, f"сделка в день прогрева {warm}: {days}"


def test_the_tester_runs_a_period_across_the_seam(loop, tmp_path) -> None:
    """Тестер гонит период, а не тикер: MXU6, стык, MXZ6 — и строка стыка.

    Отчёт называет оба контракта, разбивку итога по каждому и смену
    контракта. Мутация: тестер по тикеру из настроек — MXU6 в отчёте нет.
    """
    database = tmp_path / "base.sqlite3"
    _seed_mxu6(database, (9, 8, 7))
    chain = Chain(TODAY)
    since = datetime.combine(TODAY - timedelta(days=9), time(0), MSK)
    until = datetime.combine(TODAY, time(23, 59), MSK)

    async def work(port: HistoryPort, heard: Heard) -> None:
        port.load_history(HistoryLoadRequest(symbol="MXZ6"))
        await _settle(heard)
        await port.wait()
        port.run_backtest(BacktestRequest(
            settings=Settings(instrument="MXZ6"), since=since, until=until,
        ))
        await port.wait()

    _, heard = _run(loop, database, chain, work, instrument="MXZ6")

    assert heard.reports, "отчёт прогона не пришёл"
    report = heard.reports[-1]
    assert report.instrument == "MXU6 → MXZ6", report.instrument
    text = "\n".join(report.contracts)
    assert "MXU6" in text and "MXZ6" in text and "комиссия" in text, text
    assert "Смена контракта MXU6 → MXZ6" in text, text
    full = [chart for chart in heard.charts if chart.candles]
    assert [(seam.previous, seam.symbol) for seam in full[-1].seams] == [("MXU6", "MXZ6")]


def test_the_seam_deal_is_labelled_as_a_contract_change() -> None:
    """Сделка, закрытая на стыке, подписана «смена контракта MXU6 → MXZ6».

    Движок ставит ей причину «конец поданного отрезка» — это данные;
    что конец — стык контрактов, знает только нарезка. Прочие строки
    не трогаются.
    """
    from app.stitched_view import relabel, seam_reason
    from backtest import Deal, HistoryRun
    from backtest.stitched import Piece, PieceRun, StitchedRun
    from engine import ExitReason, Side
    from ui.models import Side as WindowSide

    at = datetime.combine(TODAY, time(10), MSK)

    def row(reason: str) -> TradeRow:
        return TradeRow(
            entry_time=at, exit_time=at, side=next(iter(WindowSide)), volume=1.0,
            entry_price=1.0, exit_price=1.0, exit_reason=reason, profit_rub=None,
            profit_pct=None, commission_rub=None,
        )

    def piece(symbol: str) -> Piece:
        return Piece(symbol=symbol, since=TODAY, until=TODAY, warmup=(), body=(),
                     warmup_wanted=0)

    deal = Deal(
        side=Side.LONG, volume=1.0, entry_time=at, entry_price=1.0,
        entry_order_id="open", exit_time=at, exit_price=1.0, exit_order_id="close",
        exit_reason=ExitReason.SERIES_END, commission=None, ruble_per_point=1.0,
    )
    first = PieceRun(piece=piece("MXU6"), run=HistoryRun(deals=(deal, deal)), seam=deal)
    second = PieceRun(piece=piece("MXZ6"), run=HistoryRun(deals=(deal,)))
    stitched = StitchedRun(pieces=(first, second))

    labels = seam_reason(stitched)
    rows = relabel([row("a"), row("b"), row("конец поданного отрезка"), row("c")], labels)

    assert [one.exit_reason for one in rows] == [
        "a", "b", "смена контракта MXU6 → MXZ6", "c",
    ]


def test_the_tester_does_not_cut_another_asset_by_the_mx_chain(loop, tmp_path) -> None:
    """Ревью 27.09.2026, сценарий А: в настройках RIZ6, в таблице только MX.

    До правки тестер резал период кусками MXU6 → MXZ6 и считал их
    стоимостью пункта RIZ6. Теперь — строка «склейка не собрана» с именем
    актива и прогон одним рядом из настроек; кусков MX в отчёте нет.
    """
    database = tmp_path / "base.sqlite3"
    _seed_mxu6(database, (9, 8, 7))
    chain = Chain(TODAY)
    since = datetime.combine(TODAY - timedelta(days=9), time(0), MSK)
    until = datetime.combine(TODAY, time(23, 59), MSK)

    offered: list[BacktestOptions] = []

    async def work(port: HistoryPort, heard: Heard) -> None:
        port.backtest_options_ready.connect(offered.append)
        port.load_history(HistoryLoadRequest(symbol="MXZ6"))
        await _settle(heard)
        await port.wait()
        port.request_backtest_options()
        await port.wait()
        port.run_backtest(BacktestRequest(
            settings=Settings(instrument="RIZ6"), since=since, until=until,
        ))
        await port.wait()

    _, heard = _run(loop, database, chain, work, instrument="RIZ6")

    # Периоды тестера — только цепочки актива из настроек: MX для RIZ6 не предлагается.
    assert offered, "выбор тестера не пришёл"
    assert all(not one.periods for one in offered), offered

    refused = [row for row in heard.notes if row.event == "Склейка по контрактам не собрана"]
    assert refused, "прогон RIZ6 без своей цепочки прошёл молча"
    assert "RI" in refused[-1].reason, refused[-1].reason
    assert all("MX" not in report.instrument for report in heard.reports), [
        report.instrument for report in heard.reports
    ]


def test_a_monthly_asset_falls_back_to_the_day_load_aloud(loop, tmp_path) -> None:
    """BR: биржа знает BRX6 — по контракту не грузим, говорим и грузим по дням.

    Квартальная цепочка BRM6…BRH7 для нефти ложна: рубежи по ней записали бы
    в таблицу выдуманные периоды. Квартальных строк в таблице нет; есть одна —
    названный биржей месячный код, след месячности актива (`D-125`).
    """
    database = tmp_path / "base.sqlite3"
    chain = Chain(TODAY)
    for month in "FGJKNQVX":  # месячные коды нефти биржа знает, как BRX6 вживую
        chain.volumes[f"BR{month}6"] = chain.volumes[f"BR{month}7"] = {}

    _, heard = _run(loop, database, chain, _load("BRZ6"), instrument="BRZ6")

    said = [row for row in heard.notes if row.event == "Загрузка по контракту не начата"]
    assert said and "месячные" in said[-1].reason, " | ".join(
        f"{row.event}: {row.reason[:90]}" for row in heard.notes
    )
    spans = chain.minute_spans()
    assert spans, "загрузка по дням не пошла"
    assert min(first for first, _ in spans) == TODAY - timedelta(days=89)
    with CandleStore(database) as store:
        rows = store.contracts()
    assert [row.symbol for row in rows] == ["BRK6"], rows
    assert all(row.active_from is None and row.active_to is None for row in rows), rows


# ---------------------------------------------------------------------------
# B-069: загрузка периода — каждым контрактом своими днями, затем сверка
# ---------------------------------------------------------------------------

#: «Сегодня» владельца счёта 09.10.2026 и рубежи 2026 года, как в его таблице.
OWNER_TODAY = date(2026, 10, 9)
ROLLS = {"MXM6": date(2026, 3, 19), "MXU6": date(2026, 6, 18), "MXZ6": date(2026, 9, 17)}


class Quarters(Chain):
    """Подставная биржа с настоящими кварталами: MXH6 → MXM6 → MXU6 → MXZ6 → MXH7.

    Ближний по дневным объёмам — по рубежам `ROLLS`; до своего рубежа
    контракт торгуется слабо, после следующего — не торгуется вовсе (истёк).
    Минуты — каждый день, кроме `holes`: дней, за которые биржа торговала
    (объём есть), а минут по коду не отдаёт.
    """

    def __init__(self, today: date, *, holes: dict[str, set[date]] | None = None) -> None:
        super().__init__(today)
        self.holes = holes or {}
        codes = ("MXH6", "MXM6", "MXU6", "MXZ6", "MXH7")
        self.volumes = {code: {} for code in codes}
        day = date(2025, 9, 1)
        while day <= today:
            front = max(
                (code for code, roll in ROLLS.items() if roll <= day), default="MXH6",
                key=lambda code: ROLLS[code],
            )
            for code in codes[codes.index(front):]:
                self.volumes[code][day] = 1000.0 if code == front else 10.0
            day += timedelta(days=1)

    def answer(self, url: str) -> bytes:
        body = super().answer(url)
        parsed = urllib.parse.urlparse(url)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        code = parsed.path.rsplit("/", 2)[-2]
        if query.get("interval") != "1" or not self.holes.get(code):
            return body
        page = json.loads(body)
        page["candles"]["data"] = [
            row for row in page["candles"]["data"]
            if date.fromisoformat(row[6][:10]) not in self.holes[code]
        ]
        return json.dumps(page).encode()

    def spans_of(self, code: str) -> list[tuple[date, date]]:
        """Отрезки минут, которые просили по коду."""
        spans = []
        for url in self.transport.urls:
            parsed = urllib.parse.urlparse(url)
            query = dict(urllib.parse.parse_qsl(parsed.query))
            if query.get("interval") == "1" and parsed.path.rsplit("/", 2)[-2] == code:
                spans.append((date.fromisoformat(query["from"]),
                              date.fromisoformat(query["till"])))
        return spans


def _load_since(symbol: str, since: date):
    async def work(port: HistoryPort, heard: Heard) -> None:
        port.load_history(HistoryLoadRequest(symbol=symbol, days=90, since=since))
        await _settle(heard)

    return work


def test_a_period_load_takes_every_contract_in_its_own_days(loop, tmp_path) -> None:
    """Стережёт B-069 (A): период 17.06–сегодня — MXM6, MXU6, MXZ6 каждый своими днями.

    Слова владельца счёта 09.10.2026: «выбрали период с 17 июня — и там
    актуальный фьючерс до конца его действия… Далее грузим следующий».
    По рубежам 0049 17.06 — ещё MXM6, MXU6 — 18.06–16.09, MXZ6 — с 17.09.
    Мутация: грузить только код из настроек (MXZ6) — минут MXU6 и MXM6
    не просили.
    """
    database = tmp_path / "base.sqlite3"
    chain = Quarters(OWNER_TODAY)

    _, heard = _run(loop, database, chain, _load_since("MXZ6", date(2026, 6, 17)),
                    instrument="MXZ6")

    outcome = heard.finished[-1]
    assert outcome.ok, outcome.trouble
    for code, first, last in (
        ("MXM6", date(2026, 6, 17), date(2026, 6, 17)),
        ("MXU6", date(2026, 6, 18), date(2026, 9, 16)),
        ("MXZ6", date(2026, 9, 17), OWNER_TODAY),
    ):
        spans = chain.spans_of(code)
        assert spans, f"минуты {code} не просили вовсе"
        asked = {since + timedelta(days=n) for since, till in spans
                 for n in range((till - since).days + 1)}
        left = [day for day in (first + timedelta(days=n) for n in range((last - first).days + 1))
                if day not in asked]
        assert not left, f"{code}: дни своего куска не просили: {left[:3]}…"
        assert max(till for _, till in spans) == last, f"{code} грузился за чужие дни: {spans}"
        assert min(since for since, _ in spans) >= first - timedelta(days=WARMUP_LOOK_BACK_DAYS), (
            f"{code} грузил хвост до своего куска: {spans}"
        )
    assert chain.spans_of("MXH6") == [], "MXH6 на отрезке не ближний — грузить его нечего"
    assert outcome.detail.startswith(
        "Загружено: MXM6 17.06 (1 из 1 торговых дней биржи) · MXU6 18.06–16.09 (91 из 91) · "
        "MXZ6 17.09–09.10 (23 из 23)."
    ), outcome.detail


def _asked_days(chain: Quarters, code: str) -> list[date]:
    """Дни, за которые минуты кода спросили у биржи. Страницы одного отрезка — один раз."""
    return sorted({since + timedelta(days=n) for since, till in chain.spans_of(code)
                   for n in range((till - since).days + 1)})


def test_a_repeated_load_does_not_ask_the_exchange_for_days_it_already_has(
    loop, tmp_path,
) -> None:
    """Стережёт B-069 (D): «Догрузить недостающее» второй раз не качает лежащие дни.

    Слова владельца счёта 09.10.2026: «почему каждый раз 20 тысяч свечей?».
    Тот же период с 17.06 грузится дважды, без «заново». Во второй раз биржу
    не спрашивают ни об одном дне, отмеченном после первого (`settled`
    в `data_day`); по MXZ6 спрашивают ровно сегодняшний, ещё не закрытый день.
    Итог называет число запрошенных дней — и оно совпадает с тем, что
    подставная биржа реально получила.

    Мутации: снимать отметки без «заново»; не смотреть на отметки при выборе
    дней; считать в итоге просимый отрезок вместо запрошенного; не сказать.
    ⚠️ По прошлым кускам второй раз спрашивается их последний день (MXM6 17.06,
    MXU6 16.09): после него минут этого кода в базе нет, и `_settle_tails`
    его не закрывает. Отмеченным он не был — проверке это не противоречит;
    перезапрос — долг `D-149`.
    """
    database = tmp_path / "base.sqlite3"
    first_chain = Quarters(OWNER_TODAY)
    _, first = _run(loop, database, first_chain, _load_since("MXZ6", date(2026, 6, 17)),
                    instrument="MXZ6")
    assert first.finished[-1].ok, first.finished[-1].trouble
    assert "Календарных дней запрошено у биржи: 118 из 118." in first.finished[-1].detail, (
        first.finished[-1].detail
    )
    codes = ("MXM6", "MXU6", "MXZ6")
    with CandleStore(database) as store:
        settled = {code: store.settled_days(code) for code in codes}
    assert all(settled.values()), f"первая загрузка ничего не отметила: {settled}"

    chain = Quarters(OWNER_TODAY)
    _, heard = _run(loop, database, chain, _load_since("MXZ6", date(2026, 6, 17)),
                    instrument="MXZ6")

    outcome = heard.finished[-1]
    assert outcome.ok, outcome.trouble
    asked = {code: _asked_days(chain, code) for code in codes}
    for code in codes:
        again = sorted(set(asked[code]) & settled[code])
        assert not again, f"{code}: второй раз спросили уже отмеченные дни: {again[:5]}…"
    assert asked["MXZ6"] == [OWNER_TODAY], f"MXZ6 спросили не только сегодня: {asked['MXZ6']}"
    total = sum(len(days) for days in asked.values())
    said = f"Календарных дней запрошено у биржи: {total} из 118, остальные уже были в базе."
    assert said in outcome.detail, outcome.detail


def test_after_the_load_the_days_the_exchange_traded_are_checked(loop, tmp_path) -> None:
    """Стережёт B-069 (B1): биржа торговала 07.09–16.09, минут MXU6 в базе нет — громко.

    Итог `ok=False`, строка с диапазоном в беде итога и в журнале
    предупреждением, отметки «спрашивали» с этих дней сняты — следующая
    «Догрузить недостающее» спросит их снова. Мутации: не сверять, сверить
    и не сказать, не снять отметки.
    """
    database = tmp_path / "base.sqlite3"
    hole = {date(2026, 9, 7) + timedelta(days=n) for n in range(10)}
    chain = Quarters(OWNER_TODAY, holes={"MXU6": hole})

    _, heard = _run(loop, database, chain, _load_since("MXZ6", date(2026, 6, 17)),
                    instrument="MXZ6")

    outcome = heard.finished[-1]
    assert not outcome.ok, "пропуск 07.09–16.09 прошёл как успех"
    line = "MXU6: нет свечей за 07.09–16.09 — сделки за эти дни не посчитаны."
    assert line in outcome.trouble, outcome.trouble
    assert "MXU6 18.06–16.09 (81 из 91)" in outcome.detail, outcome.detail
    warned = [row for row in heard.notes
              if row.event == "Загрузка истории" and line in row.reason]
    assert warned and warned[-1].level is DecisionLevel.WARNING, "журнал о пропуске молчит"
    with CandleStore(database) as store:
        settled = store.settled_days("MXU6")
    assert not settled & hole, f"отметки с пропуска не сняты: {sorted(settled & hole)}"
    assert date(2026, 9, 4) in settled, "сняты отметки и с дней, где свечи есть"


def test_the_tester_says_the_hole_and_the_basis_in_its_report(loop, tmp_path) -> None:
    """Стережёт B-069 (B3 и «Считается на:»): дыра — ⚠ в отчёте, строка — в шапке и графике.

    MXU6 со свечами 14.09 и 16.09, 15.09 без свечей и без отметки. Отчёт
    о прогоне называет дыру в разбивке «По контрактам», строку «Считается на:»
    несёт и отчёт, и снимок графика (оттуда — «Отчёты»). Мутация «посчитали,
    но не отдали»: строка собрана, но в отчёт не положена.
    """
    database = tmp_path / "base.sqlite3"
    _seed_mxu6(database, (9, 7))
    chain = Chain(TODAY)
    since = datetime.combine(TODAY - timedelta(days=9), time(0), MSK)
    until = datetime.combine(TODAY, time(23, 59), MSK)

    async def work(port: HistoryPort, heard: Heard) -> None:
        port.load_history(HistoryLoadRequest(symbol="MXZ6"))
        await _settle(heard)
        await port.wait()
        port.run_backtest(BacktestRequest(
            settings=Settings(instrument="MXZ6"), since=since, until=until,
        ))
        await port.wait()

    _, heard = _run(loop, database, chain, work, instrument="MXZ6")

    report = heard.reports[-1]
    hole = f"MXU6: нет свечей за {TODAY - timedelta(days=8):%d.%m}"
    assert any(line.startswith(f"⚠ {hole}") for line in report.contracts), report.contracts
    basis = (
        f"Считается на: MXU6 {TODAY - timedelta(days=9):%d.%m}–"
        f"{TODAY - timedelta(days=7):%d.%m} (2 дн. со свечами; нет "
        f"{TODAY - timedelta(days=8):%d.%m}) · MXZ6 {TODAY - timedelta(days=6):%d.%m}–"
        f"{TODAY:%d.%m} (5 дн. со свечами)"
    )
    # 5, а не 7: подставная биржа отдаёт минуты и в субботу с воскресеньем,
    # а дни считаются торговые (пн–пт), как в сверке загрузки с биржей.
    assert report.basis == basis, report.basis
    full = [chart for chart in heard.charts if chart.candles]
    assert full[-1].basis == basis, "строка не дошла до снимка графика — «Отчёты» её не покажут"


class _BrokenOnMXZ6(Quarters):
    """Биржа, рвущая связь на минутах MXZ6."""

    def answer(self, url: str) -> bytes:
        if "/MXZ6/" in url and "interval=1&" in url:
            raise OSError("связь оборвалась")
        return super().answer(url)


def test_a_break_in_the_middle_names_the_pieces_loaded_and_not(loop, tmp_path) -> None:
    """Обрыв посреди периода: скачанное остаётся, итог называет, что загружено, что нет.

    Биржа рвёт связь на минутах MXZ6 — третьем куске. MXM6 и MXU6 уже
    в базе, и итог говорит это словами, а не «Загрузка не удалась» без
    подробностей.
    """
    database = tmp_path / "base.sqlite3"
    chain = _BrokenOnMXZ6(OWNER_TODAY)

    _, heard = _run(loop, database, chain, _load_since("MXZ6", date(2026, 6, 17)),
                    instrument="MXZ6")

    outcome = heard.finished[-1]
    assert not outcome.ok
    assert "Загружено: MXM6 17.06, MXU6 18.06–16.09." in outcome.trouble, outcome.trouble
    assert "Не загружено: MXZ6 17.09–09.10." in outcome.trouble, outcome.trouble
    with CandleStore(database) as store:
        assert date(2026, 9, 16) in store.trading_days("MXU6"), "скачанное до обрыва пропало"


#: Куски периода с 17.06 по рубежам 0049 — каждый своими днями.
PERIOD_PIECES = (
    ("MXM6", date(2026, 6, 17), date(2026, 6, 17)),
    ("MXU6", date(2026, 6, 18), date(2026, 9, 16)),
    ("MXZ6", date(2026, 9, 17), OWNER_TODAY),
)


def _days_of(first: date, last: date) -> list[date]:
    return [first + timedelta(days=n) for n in range((last - first).days + 1)]


def test_loading_anew_over_a_period_asks_every_contract_its_days_again(loop, tmp_path) -> None:
    """Стережёт B-069 (D, обратная сторона): «Загрузить заново» перекачивает каждый кусок.

    Период с 17.06 уже загружен и отмечен. «Заново» с той же даты обязано
    снова спросить у биржи все дни MXM6, MXU6 и MXZ6 — полная перекачка
    бывает только так. Мутация: снять отметки одного куска (кода из
    настроек) — дни MXU6 второй раз не спрошены, и «заново» молча
    оказывается догрузкой.
    """
    database = tmp_path / "base.sqlite3"
    _, first = _run(loop, database, Quarters(OWNER_TODAY),
                    _load_since("MXZ6", date(2026, 6, 17)), instrument="MXZ6")
    assert first.finished[-1].ok, first.finished[-1].trouble
    with CandleStore(database) as store:
        # Без отметок второй раз спросили бы всё и без «заново» — проверка пуста.
        assert store.settled_days("MXU6"), "первая загрузка ничего не отметила"

    chain = Quarters(OWNER_TODAY)

    async def anew(port: HistoryPort, heard: Heard) -> None:
        port.load_history(HistoryLoadRequest(
            symbol="MXZ6", days=90, since=date(2026, 6, 17), replace=True,
        ))
        await _settle(heard)

    _, heard = _run(loop, database, chain, anew, instrument="MXZ6")

    assert heard.finished[-1].ok, heard.finished[-1].trouble
    for code, first_day, last_day in PERIOD_PIECES:
        asked = set(_asked_days(chain, code))
        left = [day for day in _days_of(first_day, last_day) if day not in asked]
        assert not left, f"«заново» не перезапросило {code}: {left[:3]}…"


def test_every_piece_of_the_period_gets_the_warmup_of_the_average_period(
    loop, tmp_path,
) -> None:
    """Стережёт B-069 (A, «плюс прогрев»): перед каждым куском — прогрев на период средней.

    Подставная биржа отдаёт 120 минут в день — 24 пятиминутки. Период 30
    из настроек в один день не помещается: перед началом каждого куска
    нужны два дня **его же** контракта. Мутация: порт передаёт загрузке
    прогрев не из настроек (один бар) — берётся один день, средняя на стыке
    начинает недогретой, а итог молчит: недогрев меряется тем же неверным
    числом.
    """
    database = tmp_path / "base.sqlite3"
    chain = Quarters(OWNER_TODAY)

    async def work(port: HistoryPort, heard: Heard) -> None:
        port.apply_settings(Settings(instrument="MXZ6", average_period=30))
        await port.wait()
        port.load_history(HistoryLoadRequest(symbol="MXZ6", days=90, since=date(2026, 6, 17)))
        await _settle(heard)

    _, heard = _run(loop, database, chain, work, instrument="MXZ6")

    assert heard.finished[-1].ok, heard.finished[-1].trouble
    for code, start, _ in PERIOD_PIECES:
        asked = set(_asked_days(chain, code))
        warm = {start - timedelta(days=1), start - timedelta(days=2)}
        assert warm <= asked, (
            f"{code}: прогрев перед {start:%d.%m} неполный — не спрошены "
            f"{sorted(warm - asked)}"
        )


class _WithoutMXH6(Quarters):
    """Биржа, не знающая MXH6: начало периода MXM6 датировать нечем, 17.06 без контракта."""

    def __init__(self, today: date) -> None:
        super().__init__(today)
        del self.volumes["MXH6"]

    def answer(self, url: str) -> bytes:
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        if "/MXH6/" in url and query.get("interval") == "24":
            return _daily_body({})
        return super().answer(url)


def test_days_of_the_period_without_a_contract_are_said_after_the_load(
    loop, tmp_path,
) -> None:
    """Стережёт B-069 (B, правило 13): день периода без контракта в таблице — громко.

    MXH6 биржа не знает, начало MXM6 неизвестно, и 17.06 не грузится ничем.
    Итог обязан это сказать (`ok=False`, строка в беде, журнал —
    предупреждением), а не отчитаться «История загружена» по 18.06–09.10.
    Мутация молчания: дни без контракта не отданы в итог.
    """
    database = tmp_path / "base.sqlite3"
    chain = _WithoutMXH6(OWNER_TODAY)

    _, heard = _run(loop, database, chain, _load_since("MXZ6", date(2026, 6, 17)),
                    instrument="MXZ6")

    outcome = heard.finished[-1]
    line = ("17.06.2026 … 17.06.2026: в таблице контрактов нет ближнего на эти дни — "
            "они не загружены.")
    assert not outcome.ok, f"17.06 без контракта прошло как успех: {outcome.detail}"
    assert line in outcome.trouble, outcome.trouble
    warned = [row for row in heard.notes
              if row.event == "Загрузка истории" and line in row.reason]
    assert warned and warned[-1].level is DecisionLevel.WARNING, "журнал о дне без контракта молчит"


class _CheckRefused(Quarters):
    """Биржа, у которой дневные свечи перестают отвечать, когда пошли минуты."""

    def __init__(self, today: date) -> None:
        super().__init__(today)
        self.minutes_asked = False

    def answer(self, url: str) -> bytes:
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        if query.get("interval") == "1":
            self.minutes_asked = True
        elif query.get("interval") == "24" and self.minutes_asked:
            raise OSError("связь оборвалась")
        return super().answer(url)


def test_a_check_the_exchange_refused_is_said_and_the_load_is_not_clean(
    loop, tmp_path,
) -> None:
    """Стережёт B-069 (B1, правило 13): сверка не сделана — так и сказано, `ok=False`.

    Минуты всех кусков в базе, а дневные свечи для сверки биржа не отдала.
    Пропуски не проверены, и «История загружена» без оговорки была бы
    неправдой. Мутация молчания: отказ сверки проглочен.
    """
    database = tmp_path / "base.sqlite3"
    chain = _CheckRefused(OWNER_TODAY)

    _, heard = _run(loop, database, chain, _load_since("MXZ6", date(2026, 6, 17)),
                    instrument="MXZ6")

    outcome = heard.finished[-1]
    assert chain.minutes_asked, "до минут дело не дошло — сверку проверить нечем"
    assert not outcome.ok, f"несделанная сверка прошла как успех: {outcome.detail}"
    assert "Сверка дней с биржей не сделана" in outcome.trouble, outcome.trouble
    assert "Пропуски в базе не проверены" in outcome.trouble, outcome.trouble
    with CandleStore(database) as store:
        assert date(2026, 9, 16) in store.trading_days("MXU6"), "минуты пропали вместе со сверкой"
