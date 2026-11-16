"""Бой: «Сделки», итог и метки графика — по всем боевым заходам из базы (B-100).

13.11.2026 в бою вкладка «Сделки» стояла пустой при четырёх боевых сделках
за день: каждый заход боя — новый прогон, движок «факта» прогрет с нуля,
а таблица показывала только его сделки. Здесь стережётся, что прежние заходы
приходят из базы в таблицу и на график одним списком, без дублей с памятью
движка, а отказ чтения базы назван словами.
"""

from __future__ import annotations

import pathlib
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
import websockets
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor

from app import convert
from app.live_history import Earlier, Executed, compose
from backtest import Deal, HistoryRun, summarise
from engine import ExitReason
from engine import Side as EngineSide
from market import (
    CandleStore,
    MarketWorker,
    OrderFillTimes,
    StoredTrade,
    TradeRecord,
    TradeSide,
)
from market.journal import RunOrigin, SessionRecord
from tests.fake_broker import FakeBroker
from tests.test_app_live_pair import DAY, FIXED_TAKE, SYMBOL, _bench, _close, _connect
from ui.journals import JournalTabs
from ui.models import ChartData, Layer, TradeRow, TradesSummary
from ui.models import RunOrigin as WindowOrigin
from ui.theme import current as current_theme

#: Две прежние боевые сделки, каждая своим прогоном: вход, выход, сторона, цены.
EARLIER = (
    (DAY.replace(hour=7, minute=15), DAY.replace(hour=7, minute=30),
     TradeSide.LONG, 250_000.0, 250_100.0),
    (DAY.replace(hour=8), DAY.replace(hour=8, minute=20),
     TradeSide.SHORT, 250_200.0, 250_050.0),
)


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch) -> FakeBroker:
    fake = FakeBroker()
    monkeypatch.setattr(websockets, "connect", fake.connect)
    return fake


async def _earlier_runs(worker: MarketWorker) -> None:
    """Два закрытых боевых прогона прошлых заходов, по одной сделке в каждом."""

    def write(base: CandleStore) -> None:
        for number, (entry, exit_, side, price_in, price_out) in enumerate(EARLIER):
            run = base.open_journal_session(
                SessionRecord(RunOrigin.LIVE, symbol=SYMBOL), now=entry - timedelta(minutes=5)
            )
            sign = 1.0 if side is TradeSide.LONG else -1.0
            base.write_trades(run.id, [TradeRecord(
                symbol=SYMBOL, side=side, volume=1.0,
                entry_time=entry, entry_price=price_in,
                exit_time=exit_, exit_price=price_out,
                exit_reason=ExitReason.TAKE_PROFIT.label,
                gross=(price_out - price_in) * sign,
                entry_order_id=f"open-{number}", exit_order_id=f"take-{number}",
            )])
            base.finish_journal_session(run.id, now=exit_ + timedelta(minutes=1))

    await worker.call(write)


def _stored(number: int, session: int, entry: datetime, exit_: datetime) -> StoredTrade:
    return StoredTrade(
        id=number, session_id=session, origin=RunOrigin.LIVE, symbol=SYMBOL,
        side=TradeSide.LONG, volume=1.0, entry_time=entry, entry_price=250_000.0,
        exit_time=exit_, exit_price=250_100.0, exit_reason=ExitReason.TAKE_PROFIT.label,
        gross=100.0, written_at=exit_, entry_order_id="open-1", exit_order_id="take-1",
    )


def test_a_deal_in_memory_and_in_the_base_is_one_row() -> None:
    """Стережёт: сделка текущего захода из памяти и та же из базы — одна строка.

    База хранит время до секунды, память движка — до микросекунды: ключ «та же
    сделка» обязан совпасть, иначе сделка встала бы дважды и удвоила итог.
    Две сделки прежних заходов при этом остаются — приглушёнными строками.
    """
    entry = DAY.replace(hour=10, minute=0, second=1, microsecond=420_000)
    exit_ = DAY.replace(hour=10, minute=15, second=3, microsecond=900_000)
    deal = Deal(
        side=EngineSide.LONG, volume=1.0, entry_time=entry, entry_price=250_000.0,
        entry_order_id="open-1", exit_time=exit_, exit_price=250_100.0,
        exit_order_id="take-1", exit_reason=ExitReason.TAKE_PROFIT,
    )
    stored = (
        _stored(1, 381, DAY.replace(hour=7), DAY.replace(hour=7, minute=30)),
        _stored(2, 384, DAY.replace(hour=8), DAY.replace(hour=8, minute=30)),
        _stored(3, 395, entry.replace(microsecond=0), exit_.replace(microsecond=0)),
    )

    shown = compose(
        HistoryRun(deals=(deal,), summary=summarise((deal,))), Earlier(stored), symbol=SYMBOL,
        timeframe=convert.timeframe_of("15 минут"), since=DAY.replace(hour=9, minute=50),
    )

    assert [row.earlier_run for row in shown.rows] == [True, True, False], shown.rows
    assert [row.entry_time.hour for row in shown.rows] == [7, 8, 10], shown.rows
    assert all(row.origin is WindowOrigin.LIVE for row in shown.rows), shown.rows
    assert shown.summary.trades == 3, shown.summary
    assert shown.summary.current is not None and shown.summary.current.trades == 1
    assert "09:50 19.06" in shown.summary.current.title, shown.summary.current.title
    assert len([one for one in shown.markers if one.earlier_run]) == 4, shown.markers


def test_live_trades_show_every_earlier_run_in_the_table_and_on_the_chart(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """Стережёт: в бою «Сделки» и график показывают сделки прежних боевых прогонов из базы.

    Две закрытые боевые сессии в базе, по сделке в каждой; бой только включён,
    текущий заход пуст. В таблице обе сделки приглушёнными строками боевого
    режима, итог всей истории — две сделки, итог захода — ноль; на графике
    у каждой сделки метки входа и выхода слоя «Факт» и линия — приглушённые.
    """

    async def go() -> tuple[list[tuple[tuple[TradeRow, ...], TradesSummary]], list[ChartData]]:
        bench, session = await _bench(tmp_path, broker, FIXED_TAKE, before=_earlier_runs)
        tables: list[tuple[tuple[TradeRow, ...], TradesSummary]] = []
        charts: list[ChartData] = []
        bench.port.trades_replaced.connect(
            lambda rows, summary: tables.append((tuple(rows), summary))
        )
        bench.port.chart_replaced.connect(charts.append)
        try:
            await _connect(bench)
            return tables, charts
        finally:
            await _close(bench, session)

    tables, charts = loop.run_until_complete(go())
    assert tables and charts, "окно не получило ни таблицы, ни графика"
    rows, summary = tables[-1]
    closed = [row for row in rows if row.exit_time is not None]
    assert [(row.entry_time, row.exit_time, row.earlier_run) for row in closed] == [
        (entry, exit_, True) for entry, exit_, *_ in EARLIER
    ], closed
    assert all(row.origin is WindowOrigin.LIVE for row in closed), closed
    assert summary.trades == len(EARLIER), summary
    assert summary.current is not None and summary.current.trades == 0, summary.current
    earlier_marks = [one for one in charts[-1].markers if one.earlier_run]
    assert all(one.layer is Layer.FACT for one in earlier_marks), earlier_marks
    assert sorted(one.price for one in earlier_marks) == sorted(
        price for *_, price_in, price_out in EARLIER for price in (price_in, price_out)
    ), earlier_marks
    assert len([one for one in charts[-1].paths if one.earlier_run]) == len(EARLIER)


def test_an_unread_base_is_named_in_the_window(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Стережёт: отказ чтения базы — причина словами в журнале и в подписи итога.

    Пустая таблица без слова читалась бы как «сделок не было» — ровно то
    молчание, из-за которого B-100 заметили только по выписке брокера.
    """

    async def refuse(**_: object) -> None:
        raise OSError("disk I/O error")

    async def go() -> tuple[list[TradesSummary], str]:
        bench, session = await _bench(tmp_path, broker, FIXED_TAKE, before=_earlier_runs)
        summaries: list[TradesSummary] = []
        bench.port.trades_replaced.connect(lambda _rows, summary: summaries.append(summary))
        monkeypatch.setattr(bench.worker, "trades", refuse)
        try:
            await _connect(bench)
            return summaries, bench.said("Сделки прежних заходов боя не прочитаны")
        finally:
            await _close(bench, session)

    summaries, said = loop.run_until_complete(go())
    assert said, "отказ чтения базы не сказан в журнале решений"
    assert summaries and "не прочитаны" in summaries[-1].title, summaries[-1:]


def test_earlier_rows_are_dimmed_and_both_totals_are_shown(qapp) -> None:
    """Стережёт: строка прежнего захода приглушена, итогов под таблицей два.

    Приглушена вся строка, включая результат: яркими остаются только сделки
    текущего захода. Под таблицей — итог всей истории боя и итог захода.
    """
    shown = compose(
        HistoryRun(), Earlier((_stored(1, 381, DAY.replace(hour=7), DAY.replace(hour=8)),)),
        symbol=SYMBOL, timeframe=convert.timeframe_of("15 минут"),
        since=DAY.replace(hour=9),
    )
    widget = JournalTabs()
    try:
        widget.set_trades(shown.rows, shown.summary)
        model = widget.trades_model
        colours = {
            model.data(model.index(0, column), Qt.ItemDataRole.ForegroundRole).name()
            for column in range(model.columnCount())
        }
        assert colours == {QColor(current_theme().text_dim).name()}, colours
        text = widget.summary_label.text()
        assert "Итог за всю историю боя" in text and "Текущий заход боя с 09:00" in text, text
    finally:
        widget.deleteLater()


def _times(session: int, name: str, placed: datetime, filled: datetime) -> OrderFillTimes:
    return OrderFillTimes(
        session_id=session, order_id=name, placed_at=placed, first_at=filled, last_at=filled
    )


def test_live_rows_show_the_broker_execution_time_not_the_engine_candle() -> None:
    """Стережёт: время входа и выхода в «Сделках» боя — секунда исполнения у брокера.

    Бой 16.11.2026: условная исполнена в 10:38:03 под свечой 10:35, вход —
    в 10:35:05, а строка читалась «10:35 → 10:35». Сделка, чьих исполнений
    в журнале команд нет, остаётся со временем свечи — честно, а не пусто.
    """
    candle_in, candle_out = DAY.replace(hour=10, minute=35), DAY.replace(hour=10, minute=35)
    deal = Deal(
        side=EngineSide.SHORT, volume=1.0, entry_time=candle_in, entry_price=247_425.0,
        entry_order_id="open-7", exit_time=candle_out, exit_price=247_200.0,
        exit_order_id="take-7", exit_reason=ExitReason.TRAILING_TAKE,
    )
    earlier = _stored(1, 381, DAY.replace(hour=7), DAY.replace(hour=7, minute=30))
    executed = replace(Executed.of((
        _times(471, "open-7", candle_in.replace(second=4), candle_in.replace(second=5)),
        _times(
            471, "take-7", candle_in.replace(second=6), DAY.replace(hour=10, minute=38, second=3)
        ),
    )), current=471)

    shown = compose(
        HistoryRun(deals=(deal,), summary=summarise((deal,))), Earlier((earlier,)),
        symbol=SYMBOL, timeframe=convert.timeframe_of("5 минут"), since=None,
        executed=executed,
    )

    old, live = shown.rows
    assert (old.entry_time, old.exit_time) == (earlier.entry_time, earlier.exit_time), old
    assert live.entry_time == candle_in.replace(second=5), live
    assert live.exit_time == DAY.replace(hour=10, minute=38, second=3), live


@pytest.mark.parametrize(
    ("placed_second", "filled_minute", "label"),
    [
        # Условная поставлена при входе и исполнена минутами позже — вёл брокер.
        (6, 38, convert.BROKER_TRAILING),
        # Выход программы по касанию: подан и исполнен в одной минуте (до D-196).
        (1, 35, ExitReason.TRAILING_TAKE.label),
    ],
)
def test_a_trailing_exit_led_by_the_broker_is_named_so(
    placed_second: int, filled_minute: int, label: str
) -> None:
    """Стережёт: выход по условной у брокера подписан «скользящий тейк у брокера» (B-104).

    Движок подписывает любой скользящий выход «скользящий уровень» — он не знает,
    кто вёл уровень. Знает журнал команд: условная стоит у брокера с входа
    и исполняется в другой минуте; выход программы подаётся по касанию
    и исполняется сразу. Второй случай подписи «у брокера» не получает.
    """
    stored = replace(
        _stored(1, 441, DAY.replace(hour=10, minute=35), DAY.replace(hour=10, minute=35)),
        exit_reason=ExitReason.TRAILING_TAKE.label,
    )
    executed = Executed.of((
        _times(441, "take-1", DAY.replace(hour=10, minute=35, second=placed_second),
               DAY.replace(hour=10, minute=filled_minute, second=3)),
    ))

    shown = compose(
        HistoryRun(), Earlier((stored,)), symbol=SYMBOL,
        timeframe=convert.timeframe_of("5 минут"), since=None, executed=executed,
    )

    [row] = shown.rows
    assert row.exit_reason == label, row
    assert all(f"выход — {label}." in marker.tooltip for marker in shown.markers), shown.markers
