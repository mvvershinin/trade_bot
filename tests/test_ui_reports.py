"""Окно «Отчёты», шаг 1: итог за период и разбор сделок прогона на истории.

Что стережёт каждая проверка — в её докстринге словами. Вход — настоящий
путь сборки отчёта (`app.convert.run_report` по сделкам `backtest.Deal`),
а не строки, собранные руками под ожидание.
"""

from __future__ import annotations

import dataclasses
import re
from datetime import UTC, date, datetime, timedelta

import pytest
from PySide6.QtWidgets import QLabel

from app import convert
from backtest import Deal
from backtest.execution import Costs
from backtest.history import HistoryRun, summarise
from engine import ExitReason
from engine import Side as EngineSide
from market import MSK, redact
from tests.helpers import RecordingPort
from ui.chart.painter_surface import PainterChartSurface
from ui.chart_panel import digest_lines
from ui.formatting import fmt_money
from ui.main_window import MainWindow
from ui.models import BacktestRequest, Candle, ChartData, ChartSpan, Mode, RunOrigin, Settings
from ui.reports_dialog import (
    NO_RUN,
    NO_TRADES,
    ReportsDialog,
    ShownRun,
    run_title,
    trades_in_period,
)

#: «Сегодня» окна: пятница 02.10 — «Месяц» по умолчанию октябрь, «Неделя» с 28.09.
TODAY = date(2026, 10, 2)


def _deal(n: int, entry: datetime, hold: timedelta, side, prices: tuple[float, float]) -> Deal:
    entry_price, exit_price = prices
    return Deal(
        side=side,
        volume=1,
        entry_time=entry,
        entry_price=entry_price,
        entry_order_id=f"in-{n}",
        exit_time=entry + hold,
        exit_price=exit_price,
        exit_order_id=f"out-{n}",
        exit_reason=ExitReason.SIGNAL,
        commission=28.0,
        ruble_per_point=1.0,
    )


def _deals() -> list[Deal]:
    """Пять сделок: две в сентябре, одна через полночь 30.09 → 01.10, две в октябре."""
    at = lambda d, h, m=0: datetime(2026, d[0], d[1], h, m, tzinfo=MSK)  # noqa: E731 — короткая запись дат
    return [
        _deal(1, at((9, 21), 10), timedelta(minutes=40), EngineSide.LONG, (226_000, 226_300)),
        _deal(2, at((9, 29), 11), timedelta(minutes=20), EngineSide.SHORT, (226_300, 226_450)),
        _deal(3, at((9, 30), 23), timedelta(hours=11), EngineSide.LONG, (226_450, 226_900)),
        _deal(4, at((10, 1), 10), timedelta(minutes=35), EngineSide.SHORT, (226_900, 226_800)),
        _deal(5, at((10, 2), 10, 30), timedelta(minutes=15), EngineSide.LONG, (226_800, 226_790)),
    ]


def _report(deals: list[Deal] | None = None):
    deals = _deals() if deals is None else deals
    run = HistoryRun(
        deals=tuple(deals), summary=summarise(deals), costs=Costs(commission_per_side=14.0)
    )
    request = BacktestRequest(
        settings=Settings(),
        since=datetime(2026, 9, 1, tzinfo=MSK),
        until=datetime(2026, 10, 5, tzinfo=MSK),
    )
    return convert.run_report(run, [], request, settings_text="", mode=Mode.REVERSE)


def _shown(deals: list[Deal] | None = None) -> ShownRun:
    """Прогон на экране так, как его собирает главное окно из сигналов порта."""
    report = _report(deals)
    return ShownRun(
        trades=report.trades,
        summary=report.summary,
        instrument=report.instrument,
        first=report.trades[0].entry_time if report.trades else None,
        last=report.trades[-1].exit_time if report.trades else None,
    )


@pytest.fixture()
def dialog(qapp):
    made = ReportsDialog(_shown(), today=lambda: TODAY)
    yield made
    made.deleteLater()
    qapp.processEvents()


def _money(label: QLabel) -> str:
    return label.text().split(": ", 1)[1]


def test_the_period_total_is_the_sum_of_its_rows(dialog) -> None:
    """Итог за период равен сумме показанных строк — на каждой кнопке периода.

    Стережёт: итог считается по тем же сделкам, что стоят в таблице, а не по
    всему прогону и не по соседнему периоду.
    """
    for name in ("Месяц", "Неделя", "Всё"):
        dialog.pick_period(name)
        shown = dialog.shown_trades()
        assert shown, f"«{name}»: проверка вакуумна, сделок в периоде нет"
        assert dialog.table.rowCount() == len(shown)
        net = sum(row.profit_rub for row in shown)
        fee = sum(row.commission_rub for row in shown)
        assert _money(dialog.net_label) == fmt_money(net, sign=True), name
        assert _money(dialog.commission_label) == fmt_money(fee), name
        assert _money(dialog.gross_label) == fmt_money(net + fee, sign=True), name


def test_all_agrees_with_the_run_total_on_screen(window) -> None:
    """«Всё» — те же числа, что итог прогона под журналом сделок.

    Вход — то, что порт шлёт окну (`trades_replaced`: строки и итог из
    `app.convert`), сверка — с итогом, который журнал держит у себя.
    """
    report = _report()
    window.set_trades(report.trades, report.summary)
    window.journals.reports_button.click()
    dialog = window.reports_dialog
    dialog.pick_period("Всё")
    summary = window.journals.summary()
    assert summary is not None and summary.trades == 5
    assert _money(dialog.gross_label) == fmt_money(summary.gross_profit_rub, sign=True)
    assert _money(dialog.commission_label) == fmt_money(summary.commission_rub)
    assert _money(dialog.net_label) == fmt_money(summary.net_profit_rub, sign=True)
    assert f"Сделок: {summary.trades} " in dialog.counts_label.text()
    assert "прибыльных: 3 · убыточных: 2" in dialog.counts_label.text()


def test_the_period_takes_a_trade_by_its_exit_day(dialog) -> None:
    """Правило периода: сделка относится к дню **выхода** по МСК.

    Сделка 3 вошла 30.09 в 23:00 и вышла 01.10 в 10:00 — её деньги октябрьские.
    Отбор по дню входа отнёс бы её к сентябрю, и итог месяца разошёлся бы
    со счётом.
    """
    dialog.pick_period("Месяц")
    entries = [row.entry_time.date() for row in dialog.shown_trades()]
    assert date(2026, 9, 30) in entries, "сделка через полночь не попала в месяц выхода"
    assert len(entries) == 3, entries
    rows = _report().trades
    assert [r.trade_id for r in trades_in_period(rows, date(2026, 9, 30), date(2026, 9, 30))] == []


def test_the_exit_day_is_taken_in_moscow_time_and_not_in_the_clock_of_the_data() -> None:
    """Сделка, вышедшая в 00:30 МСК 01.10, — октябрьская, даже если время пришло в UTC.

    В UTC это 30.09 21:30: день, взятый без перевода в МСК, отнёс бы её
    к сентябрю. Время сделок в прогоне не обязано быть московским —
    фикстура `_deals` задаёт его в МСК и перевода не проверяет.

    Мутация, обязанная ронять проверку: убрать `to_msk` из `trade_day`.
    """
    late = _deal(
        1, datetime(2026, 9, 30, 21, 0, tzinfo=UTC), timedelta(minutes=30),
        EngineSide.LONG, (226_000, 226_300),
    )
    rows = _report([late]).trades
    october = trades_in_period(rows, date(2026, 10, 1), date(2026, 10, 1))
    assert [row.trade_id for row in october] == [rows[0].trade_id], (
        "выход в 00:30 МСК не попал в день 01.10"
    )
    assert trades_in_period(rows, date(2026, 9, 30), date(2026, 9, 30)) == [], (
        "день выхода взят по UTC, а не по Москве"
    )


def test_a_trade_with_a_zero_result_is_neither_a_winner_nor_a_loser(qapp) -> None:
    """Счёт прибыльных и убыточных — по тем же деньгам, что итог: ноль — ни то, ни другое.

    Сделка 1 зарабатывает ровно комиссию (28 пунктов при 1 ₽ за пункт и 28 ₽
    комиссии): чистый результат ноль. Сделки 2 и 3 — прибыль и убыток.

    Мутация, обязанная ронять проверку: `value >= 0` вместо `value > 0`
    в `period_totals`.
    """
    at = datetime(2026, 10, 1, 10, tzinfo=MSK)
    minutes = timedelta(minutes=20)
    deals = [
        _deal(1, at, minutes, EngineSide.LONG, (226_000, 226_028)),
        _deal(2, at + timedelta(hours=1), minutes, EngineSide.LONG, (226_000, 226_300)),
        _deal(3, at + timedelta(hours=2), minutes, EngineSide.LONG, (226_300, 226_000)),
    ]
    shown = _shown(deals)
    assert [row.profit_rub for row in shown.trades] == [0.0, 272.0, -328.0], (
        f"проверка вакуумна: нулевой сделки нет — {[row.profit_rub for row in shown.trades]}"
    )
    made = ReportsDialog(shown, today=lambda: TODAY)
    try:
        made.pick_period("Всё")
        assert "прибыльных: 1 · убыточных: 1" in made.counts_label.text(), (
            f"сделка с нулевым результатом посчитана: {made.counts_label.text()}"
        )
    finally:
        made.deleteLater()
        qapp.processEvents()

def test_the_explanation_is_the_chart_digest_text(dialog) -> None:
    """Разбор выбранной строки — строка в строку блок «Разбора сделок» по ПКМ.

    Блок сравнивается без номера: номер у сделки в окне — её место
    в периоде, на графике — место в участке. Сравнение — с блоком **той же**
    сделки, а не «с любым»: разбор первой строки под каждой выбранной
    совпадал бы с каким-нибудь блоком и проходил.

    Мутации, обязанные ронять проверку: другие слова в `explanation_lines`;
    `self._shown[0]` вместо `self._shown[line]` в `_explain_selected`.
    """
    dialog.pick_period("Всё")
    rows = _report().trades
    assert [row.trade_id for row in dialog.shown_trades()] == [row.trade_id for row in rows]
    hour = timedelta(hours=1)
    span = ChartSpan(rows[0].entry_time - hour, rows[-1].exit_time + hour)
    blocks = _trade_blocks(digest_lines(span, rows))
    assert len(blocks) == len(rows), "проверка вакуумна: разбор графика без сделок"
    assert len({tuple(block) for block in blocks}) == len(blocks), (
        "проверка вакуумна: блоки разных сделок одинаковы"
    )
    for line in range(dialog.table.rowCount()):
        dialog.table.selectRow(line)
        text = dialog.explanation.toPlainText()
        assert text, f"строка {line + 1}: разбор пуст"
        own = _trade_blocks(digest_lines(span, [rows[line]]))
        assert own and _unnumbered(text.splitlines()) == own[0], (
            f"разбор строки {line + 1} не совпал с блоком этой сделки на графике:\n{text}"
        )


def _unnumbered(lines: list[str]) -> list[str]:
    """Без номера в первой строке и с обычным пробелом вместо неразрывного."""
    plain = [line.replace("\xa0", " ") for line in lines]
    return [re.sub(r"^\d+\. ", "", plain[0]), *plain[1:]] if plain else plain


def _trade_blocks(lines: list[str]) -> list[list[str]]:
    """Блоки сделок разбора: строка «N. …» и идущие за ней строки с отступом."""
    blocks: list[list[str]] = []
    inside = False
    for line in lines:
        if re.match(r"^\d+\. ", line):
            blocks.append([line])
            inside = True
        elif inside and line.startswith("   "):
            blocks[-1].append(line)
        else:
            inside = False  # раздел кончился: следующий отступ — уже не сделка
    return [_unnumbered(block) for block in blocks]


def test_an_empty_period_is_said_in_words(qapp) -> None:
    """Пустой период — словами, таблица убрана (правило 13, мутация молчания).

    Стережёт: окно не показывает пустую таблицу и нули вместо ответа.
    """
    made = ReportsDialog(_shown(), today=lambda: date(2026, 12, 15))
    assert made.shown_trades() == []
    assert not made.empty_label.isHidden()
    assert "сделок нет" in made.empty_label.text()
    assert "«Всё»" in made.empty_label.text()
    assert made.splitter.isHidden(), "пустая таблица осталась на экране"
    assert made.net_label.isHidden(), "показан итог периода без сделок"
    made.deleteLater()


def test_no_run_yet_is_said_in_words(qapp) -> None:
    """Прогона не было или сделок в нём нет — окно говорит это, а не молчит."""
    for run, text in ((None, NO_RUN), (ShownRun(), NO_TRADES)):
        made = ReportsDialog(run, today=lambda: TODAY)
        assert made.empty_label.text() == text and not made.empty_label.isHidden()
        assert made.splitter.isHidden()
        made.deleteLater()


def test_the_header_names_the_run(dialog) -> None:
    """Заголовок говорит, что это прогон на истории, называет инструмент и даты."""
    title = dialog.title_label.text()
    assert title.startswith("Отчёт по прогону на истории, ")
    assert _report().instrument in title
    assert "21.09.2026 — 02.10.2026 МСК" in title
    run = _shown()
    mixed = dataclasses.replace(
        run,
        trades=(*run.trades, dataclasses.replace(run.trades[-1], origin=RunOrigin.PAPER)),
    )
    assert run_title(mixed).startswith("Отчёт по прогону на экране, "), run_title(mixed)


def test_the_chart_goes_to_a_trade_it_shows(qapp) -> None:
    """Двойной щелчок: график встаёт на сделку; сделки вне свечей — «нет»."""
    surface = PainterChartSurface()
    start = datetime(2026, 10, 1, 10, 0, tzinfo=MSK)
    candles = _candles(start, 300)
    surface.show_chart(ChartData(candles=candles))
    assert surface.is_following()
    assert surface.show_moment(candles[20].opens_at, candles[25].opens_at)
    assert not surface.is_following(), "слежение не снято: первая свеча утащит экран"
    late = start + timedelta(days=30)
    assert not surface.show_moment(late, late + timedelta(minutes=30))
    surface.deleteLater()


def test_the_main_window_button_opens_the_report_of_the_run_on_screen(window) -> None:
    """Кнопка «Отчёты…» у журналов открывает окно; новый прогон его обновляет."""
    window.journals.reports_button.click()
    dialog = window.reports_dialog
    assert dialog is not None
    assert dialog.empty_label.text() == NO_RUN
    report = _report()
    start = datetime(2026, 9, 21, 10, 0, tzinfo=MSK)
    window.show_chart(ChartData(instrument="MXZ6", candles=_candles(start, 3)))
    window.set_trades(report.trades, report.summary)
    assert dialog.title_label.text().startswith("Отчёт по прогону на истории, MXZ6, 21.09.2026")
    dialog.pick_period("Всё")
    assert dialog.table.rowCount() == 5


def _candles(start: datetime, count: int) -> tuple[Candle, ...]:
    return tuple(
        Candle(opens_at=start + timedelta(minutes=5 * n), open=1, high=2, low=0.5, close=1.5)
        for n in range(count)
    )


@pytest.fixture()
def window(qapp):
    made = MainWindow(port=RecordingPort(), sanitize=redact)
    yield made
    made._timer.stop()  # noqa: SLF001 — часы окна наружу не выведены
    made.close()
    made.deleteLater()
    qapp.processEvents()
