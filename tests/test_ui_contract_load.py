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
import time as time_module
import urllib.parse
from collections.abc import Callable, Sequence
from datetime import date, datetime, time, timedelta

import pytest

from app.port import HistoryPort
from market import (
    MINUTE,
    MSK,
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
    """«Загрузить заново» для контракта снимает отметки рубежа и прогрева.

    Без него уже загруженные дни не перезапрашиваются. Дата из окна
    на отрезок не влияет: его задаёт рубеж.
    """
    chain = Chain(TODAY)
    roll = TODAY - timedelta(days=ROLL_BACK)
    asked: dict[str, list[tuple[date, date]]] = {}

    async def work(port: HistoryPort, heard: Heard) -> None:
        for name, request in (
            ("first", HistoryLoadRequest(symbol="MXZ6")),
            ("plain", HistoryLoadRequest(symbol="MXZ6")),
            ("anew", HistoryLoadRequest(symbol="MXZ6", replace=True,
                                        since=TODAY - timedelta(days=1))),
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
    assert covers(asked["anew"], roll), "«заново» не перезапросило период контракта"
    assert covers(asked["anew"], roll - timedelta(days=1)), "«заново» забыло прогрев"


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
    в таблицу выдуманные периоды. Таблица остаётся пустой.
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
        assert store.contracts() == []
