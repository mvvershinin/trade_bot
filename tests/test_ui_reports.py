"""Окно «Отчёты», шаг 1: итог за период и разбор сделок прогона на истории.

Что стережёт каждая проверка — в её докстринге словами. Вход — настоящий
путь сборки отчёта (`app.convert.run_report` по сделкам `backtest.Deal`),
а не строки, собранные руками под ожидание.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta

import pytest
from PySide6.QtCore import QDate
from PySide6.QtWidgets import QApplication, QLabel, QWidget

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
    NO_PERIOD_TRADES,
    NO_RUN,
    NO_TRADES,
    PERIODS,
    ReportsDialog,
    ShownRun,
    card_title,
    run_title,
    trades_in_period,
)
from ui.theme import current as current_theme

#: «Сегодня» окна: пятница 02.10 — «Месяц» по умолчанию октябрь, «Неделя» с 28.09.
TODAY = date(2026, 10, 2)


def _deal(
    n: int,
    entry: datetime,
    hold: timedelta,
    side,
    prices: tuple[float, float],
    *,
    commission: float | None = 28.0,
) -> Deal:
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
        commission=commission,
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


def _plain(text: str) -> str:
    """Текст окна с обычными пробелами: неразрывные — забота вёрстки, не смысла."""
    return text.replace("\xa0", " ").replace("\u202f", " ")


def _rub(text: str) -> float:
    """Рубли из текста окна: «−1 234,56 ₽» → −1234.56. Чтение экрана, не модели."""
    body = re.sub(r"\s", "", text).replace("₽", "").replace("−", "-").replace("+", "")
    return float(body.replace(",", "."))


def _card_lines(card) -> list[str]:
    """Что карточка показывает человеку: видимые непустые надписи по порядку."""
    return [
        _plain(label.text())
        for label in card.findChildren(QLabel)
        if label.isVisibleTo(card) and label.text()
    ]


def _column(dialog, title: str) -> list[float]:
    """Столбец таблицы окна числами — как его видит человек."""
    header = dialog.table.horizontalHeaderItem
    names = [header(index).text() for index in range(dialog.table.columnCount())]
    column = names.index(title)
    return [_rub(dialog.table.item(line, column).text()) for line in range(dialog.table.rowCount())]


def _value(dialog, name: str) -> str:
    """Число ячейки карточки — только если ячейка видна."""
    assert dialog.card.cells[name].isVisibleTo(dialog.card), f"ячейка «{name}» скрыта"
    return _plain(dialog.card.values[name].text())


def test_the_period_total_is_the_sum_of_its_rows(dialog) -> None:
    """С1. Карточка считает **выбранный период** — суммы тех строк, что стоят в таблице.

    Числа карточки читаются из её надписей, суммы — из ячеек таблицы, как их
    видит человек. Чистые «Месяца» и «Всего» обязаны различаться: иначе
    карточка, считающая весь прогон, прошла бы проверку.
    """
    nets: dict[str, float] = {}
    for name in ("Месяц", "Неделя", "Всё"):
        dialog.pick_period(name)
        assert dialog.table.rowCount(), f"«{name}»: проверка вакуумна, сделок в периоде нет"
        assert not dialog.card.isHidden(), f"«{name}»: карточки нет"
        net = sum(_column(dialog, "Результат, ₽"))
        fee = sum(_column(dialog, "Комиссия, ₽"))
        assert _rub(_value(dialog, "net")) == pytest.approx(net), name
        assert _rub(_value(dialog, "commission")) == pytest.approx(fee), name
        assert _rub(_value(dialog, "gross")) == pytest.approx(net + fee), name
        assert _value(dialog, "trades") == str(dialog.table.rowCount()), name
        assert _rub(_value(dialog, "average")) == pytest.approx(
            net / dialog.table.rowCount(), abs=0.005
        ), name
        nets[name] = net
    assert nets["Месяц"] != nets["Всё"], f"проверка вакуумна: периоды не различаются — {nets}"


def test_the_card_follows_dates_typed_by_hand(dialog) -> None:
    """С2. Даты, выставленные руками в полях, пересчитывают карточку.

    01.10 вышли сделки 3 (+422 ₽) и 4 (+72 ₽): чистый +494, комиссия 56,
    до комиссии 550 — посчитано руками по ценам фикстуры. До правки на экране
    «Всё» с другим итогом, иначе неизменная карточка прошла бы проверку.
    """
    dialog.pick_period("Всё")
    before = _value(dialog, "net")
    dialog.since_edit.setDate(QDate(2026, 10, 1))
    dialog.until_edit.setDate(QDate(2026, 10, 1))
    assert dialog.table.rowCount() == 2, "проверка вакуумна: в таблице не две сделки 01.10"
    assert _value(dialog, "net") == "+494,00 ₽" != before
    assert _value(dialog, "commission") == "56,00 ₽"
    assert _value(dialog, "gross") == "+550,00 ₽"
    assert _value(dialog, "trades") == "2"
    assert _rub(_value(dialog, "net")) == pytest.approx(sum(_column(dialog, "Результат, ₽")))
    assert _plain(dialog.card.title.text()).endswith("1 день, 01.10.2026")


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
    assert _value(dialog, "gross") == _plain(fmt_money(summary.gross_profit_rub, sign=True))
    assert _value(dialog, "commission") == _plain(fmt_money(summary.commission_rub))
    assert _value(dialog, "net") == _plain(fmt_money(summary.net_profit_rub, sign=True))
    assert _value(dialog, "trades") == str(summary.trades)
    counts = _plain(dialog.card.counts.text())
    assert counts == "прибыльных 3 · убыточных 2 · 60 % прибыльных", counts


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
        counts = _plain(made.card.counts.text())
        assert counts.startswith("прибыльных 1 · убыточных 1 · "), (
            f"сделка с нулевым результатом посчитана: {counts}"
        )
    finally:
        made.deleteLater()
        qapp.processEvents()

def _by_points(*points: int, commission: float | None = 28.0) -> list[Deal]:
    """Лонги 01.10 по часу друг за другом; результат каждой — заданные пункты.

    1 ₽ за пункт и объём 1: прибыль до комиссии сделки равна её пунктам,
    комиссия — `commission` (28 ₽ — две стороны по 14 ₽), `None` — тариф не задан.
    """
    start = datetime(2026, 10, 1, 10, tzinfo=MSK)
    return [
        _deal(
            n, start + timedelta(hours=n), timedelta(minutes=20), EngineSide.LONG,
            (226_000, 226_000 + value), commission=commission,
        )
        for n, value in enumerate(points, 1)
    ]


@pytest.fixture()
def open_report(
    qapp: QApplication,
) -> Iterator[Callable[[list[Deal] | ShownRun], ReportsDialog]]:
    """Окно «Отчёты» по заданным сделкам с нажатым «Всё»."""
    made: list[ReportsDialog] = []

    def open_(deals: list[Deal] | ShownRun) -> ReportsDialog:
        run = deals if isinstance(deals, ShownRun) else _shown(deals)
        dialog = ReportsDialog(run, today=lambda: TODAY)
        dialog.pick_period("Всё")
        made.append(dialog)
        return dialog

    yield open_
    for dialog in made:
        dialog.close()
        dialog.deleteLater()
    qapp.processEvents()


def test_the_commission_share_is_taken_from_the_profit_before_commission(
    qapp, open_report
) -> None:
    """С3. Доля комиссии — от прибыли **до** комиссии; «осталось» — остаток до 100.

    Пункты +300, +150, −100, комиссия 28 ₽ на сделку: до комиссии 350 ₽,
    комиссия 84 ₽, чистый 266 ₽. Руками: 84 / 350 = 24 % ушло, 76 % осталось.
    Доля от чистого дала бы 84 / 266 ≈ 32 %. Полоса читается шириной отрезков
    на экране, а не числами, из которых её строили.
    """
    dialog = open_report(_by_points(300, 150, -100))
    card = dialog.card
    assert _value(dialog, "gross") == "+350,00 ₽", "проверка вакуумна: не та валовая"
    assert _value(dialog, "commission") == "84,00 ₽", "проверка вакуумна: не та комиссия"
    assert card.share_box.isVisibleTo(card), "полосы нет"
    assert _plain(card.kept_label.text()) == "76 % осталось"
    assert _plain(card.fee_label.text()) == "24 % ушло на комиссию"
    # С4: строка «Комиссия X при прибыли Y» стоит **вместо** долей, а не рядом.
    lines = _card_lines(card)
    assert not any("при прибыли до комиссии" in line for line in lines), lines
    dialog.show()
    qapp.processEvents()
    kept, fee = card.kept_part.width(), card.fee_part.width()
    assert kept + fee > 100, f"полоса не разложена: {kept} + {fee}"
    assert kept / (kept + fee) == pytest.approx(0.76, abs=0.01), (kept, fee)


def test_the_two_shares_always_add_up_to_100(open_report) -> None:
    """С3. Проценты «осталось» и «ушло» в сумме ровно 100.

    +224 пункта, комиссия 28 ₽: ушло ровно 12,5 %. Руками, округлением как
    на бумаге: ушло 13 %, осталось 87 %. Две доли, округлённые порознь, дали
    бы 13 + 88 = 101.
    """
    card = open_report(_by_points(224)).card
    assert _plain(card.kept_label.text()) == "87 % осталось"
    assert _plain(card.fee_label.text()) == "13 % ушло на комиссию"


@pytest.mark.parametrize(
    ("points", "commission", "net", "labels"),
    [
        (2559, 2548.0, "+11,00 ₽", ("0,4 % осталось", "99,6 % ушло на комиссию")),
        (10_000, 28.0, "+9 972,00 ₽", ("99,7 % осталось", "0,3 % ушло на комиссию")),
        (
            100_000, 28.0, "+99 972,00 ₽",
            ("более 99,9 % осталось", "менее 0,1 % ушло на комиссию"),
        ),
        (
            100_000, 99_972.0, "+28,00 ₽",
            ("менее 0,1 % осталось", "более 99,9 % ушло на комиссию"),
        ),
        (300, 0.0, "+300,00 ₽", ("100 % осталось", "0 % ушло на комиссию")),
        (28, 28.0, "+0,00 ₽", ("0 % осталось", "100 % ушло на комиссию")),
    ],
    ids=[
        "ostatok-desyatye", "komissiya-desyatye", "komissiya-menee-desyatoy",
        "ostatok-menee-desyatoy", "komissiya-nol", "ostatok-nol",
    ],
)
def test_a_share_that_is_not_zero_is_never_written_as_zero(
    open_report, points, commission, net, labels
) -> None:
    """С3. Ненулевая доля не пишется «0 %»; точный ноль остаётся целым.

    Одна сделка, 1 ₽ за пункт: прибыль до комиссии равна пунктам. Руками:
    2548 / 2559 = 99,57 % — целыми «0 % осталось» при чистом +11 ₽, поэтому
    десятые: 0,4 и 99,6. 28 / 10 000 = 0,28 % → 0,3 и 99,7.
    28 / 100 000 = 0,028 % — и десятыми ноль, поэтому «менее 0,1» у меньшей
    доли и «более 99,9» у большей, с какой бы стороны меньшая ни стояла.
    Комиссия 0 и чистый 0 — точные нули: «0 %» и «100 %» целыми.
    """
    dialog = open_report(_by_points(points, commission=commission))
    card = dialog.card
    assert _value(dialog, "net") == net, "проверка вакуумна: не тот чистый"
    assert card.share_box.isVisibleTo(card), f"полосы нет: {_card_lines(card)}"
    assert (_plain(card.kept_label.text()), _plain(card.fee_label.text())) == labels


@pytest.mark.parametrize(
    ("points", "commission", "words"),
    [
        ((100, -220), 28.0, "Комиссия 56,00 ₽ при прибыли до комиссии −120,00 ₽"),
        ((100, -100), 28.0, "Комиссия 56,00 ₽ при прибыли до комиссии +0,00 ₽"),
        ((20, 30), 28.0, "Комиссия 56,00 ₽ при прибыли до комиссии +50,00 ₽"),
        ((100, -100), 0.0, "Комиссия 0,00 ₽ при прибыли до комиссии +0,00 ₽"),
    ],
    ids=[
        "valovaya-v-minuse", "valovaya-nol", "komissiya-bolshe-valovoy",
        "valovaya-nol-komissiya-nol",
    ],
)
def test_no_shares_where_they_are_not_shares(open_report, points, commission, words) -> None:
    """С4. Прибыль до комиссии ≤ 0 или комиссия больше неё — долей нет, только числа.

    «Ушло 112 %, осталось −12 %» — не доли. Вместо полосы — одна строка
    с комиссией и прибылью до комиссии со знаком; «%» в карточке остаётся
    только в счёте прибыльных.

    Последний случай — прибыль до комиссии ноль при нулевом тарифе:
    «комиссия больше прибыли» здесь ложно, и долей нет только по условию
    «прибыль до комиссии ≤ 0». Без него — деление на ноль, окно падает.
    """
    card = open_report(_by_points(*points, commission=commission)).card
    lines = _card_lines(card)
    assert not card.share_box.isVisibleTo(card), f"полоса показана: {lines}"
    assert words in lines, lines
    assert not any("осталось" in line or "ушло на комиссию" in line for line in lines), lines
    assert [line for line in lines if "%" in line] == [_plain(card.counts.text())], lines


def test_no_tariff_shows_no_net_number(open_report) -> None:
    """С5. Тариф комиссии не задан: чистого результата нет — словами.

    Не «0,00 ₽» и не прибыль до комиссии на месте чистого: ноль объявил бы
    период окупившим комиссию, валовая — выдала бы себя за результат.
    """
    card = open_report(_by_points(300, -100, commission=None)).card
    net = _plain(card.values["net"].text())
    assert card.cells["net"].isVisibleTo(card), "на месте чистого пусто — молчание"
    assert net.startswith("Чистого результата нет: тариф комиссии не задан"), net
    assert not re.search(r"\d", net), f"на месте чистого число: {net}"
    assert _plain(card.values["gross"].text()) == "+200,00 ₽"
    lines = _card_lines(card)
    assert not card.share_box.isVisibleTo(card), lines
    assert not card.cells["average"].isVisibleTo(card), lines
    assert not card.cells["commission"].isVisibleTo(card), lines
    assert not any(re.fullmatch(r"[+−]?0,00 ₽", line) for line in lines), lines
    assert sum("Чистого результата нет" in line for line in lines) == 1, lines


def test_the_card_header_names_instrument_days_and_dates(dialog) -> None:
    """С7. Заголовок карточки: инструмент, календарные дни включительно, даты.

    «Сегодня» — 1 день; 16.09–09.10 — 24 дня (границы включительно, разность
    дала бы 23); разные годы — год у обеих дат; «Всё» — даты первой
    и последней сделки прогона.
    """
    instrument = _report().instrument
    assert instrument, "проверка вакуумна: у прогона нет инструмента"

    def title() -> str:
        return _plain(dialog.card.title.text())

    dialog.pick_period("Сегодня")
    assert title() == f"{instrument} · 1 день, 02.10.2026"
    dialog.pick_period("Всё")
    assert title() == f"{instrument} · 12 дней, 21.09–02.10.2026"
    dialog.since_edit.setDate(QDate(2026, 9, 16))
    dialog.until_edit.setDate(QDate(2026, 10, 9))
    assert title() == f"{instrument} · 24 дня, 16.09–09.10.2026"
    dialog.since_edit.setDate(QDate(2025, 12, 28))
    dialog.until_edit.setDate(QDate(2026, 1, 9))
    assert title() == f"{instrument} · 13 дней, 28.12.2025–09.01.2026"
    day = date(2026, 10, 9)
    # Склонение — таблицей руками, а не правилом: правило проверки повторило бы ошибку кода.
    for days, words in (
        (2, "2 дня"), (4, "4 дня"), (5, "5 дней"), (11, "11 дней"), (14, "14 дней"),
        (20, "20 дней"), (21, "21 день"), (22, "22 дня"), (25, "25 дней"),
        (101, "101 день"), (111, "111 дней"), (112, "112 дней"),
    ):
        assert card_title("", day - timedelta(days=days - 1), day).startswith(f"— · {words}, ")


@pytest.mark.parametrize(
    ("points", "colour"), [((300,), "success"), ((-300,), "danger"), ((28,), "text")]
)
def test_the_net_colour_follows_its_sign(open_report, points, colour) -> None:
    """Чистый в плюсе — цвет успеха темы, в минусе — опасности, ноль — цвет текста."""
    dialog = open_report(_by_points(*points))
    expected = getattr(current_theme(), colour)
    assert f"color: {expected};" in dialog.card.values["net"].styleSheet()


#: Оценочные слова, которых в карточке быть не должно (просьба 09.10.2026).
#: Список не полон по построению: оценку словом вне него проверка пропустит.
_JUDGEMENTS = (
    "хорош", "плох", "много", "мало", "небольш", "обратит", "надёжн", "отличн",
    "слаб", "успешн", "довольно", "стоит ", "лучш", "хуж", "слишком", "высок",
    "низк", "значительн", "существенн", "выгодн", "приемлем", "нормальн", "опасн",
    "тревож", "внимани", "довер", "съеда", "рекоменд", "следует",
)


def _card_texts(card) -> list[str]:
    """Всё, что карточка говорит человеку: видимые надписи, её подсказка и подсказки детей."""
    tips = [card.toolTip(), *(widget.toolTip() for widget in card.findChildren(QWidget))]
    return _card_lines(card) + [_plain(tip) for tip in tips if tip]


def _with_open_position(deals: list[Deal]) -> ShownRun:
    """Прогон, где за сделками `deals` стоит ещё одна — без результата: позиция открыта."""
    run = _shown(deals)
    last = run.trades[-1]
    still_open = dataclasses.replace(
        last,
        entry_time=last.entry_time + timedelta(hours=1),
        exit_time=None,
        exit_price=None,
        profit_rub=None,
        profit_pct=None,
        commission_rub=None,
        profit_points=None,
        trade_id="still-open",
    )
    return dataclasses.replace(run, trades=(*run.trades, still_open))


def test_the_card_states_numbers_and_no_judgements(dialog, open_report) -> None:
    """В карточке и её подсказках — числа и факты, ни одной оценки результата.

    Обходятся все состояния, где у карточки свои слова: каждый период
    фикстуры, полоса долей, «долей нет», тариф не задан, итог не по всем
    сделкам, пустой период. Слово, показанное только в одном состоянии,
    обходом одних периодов фикстуры прошло бы мимо. Подсказка самой
    карточки — тоже текст для человека.
    """
    states: dict[str, list[str]] = {}
    for name, _ in PERIODS:
        dialog.pick_period(name)
        states[f"«{name}»"] = _card_texts(dialog.card)
    for state, run in (
        ("полоса", _by_points(300, -100)),
        ("долей нет", _by_points(20, 30)),
        ("тариф не задан", _by_points(300, commission=None)),
        ("итог не по всем", _with_open_position(_by_points(300, -100))),
    ):
        states[state] = _card_texts(open_report(run).card)
    dialog.since_edit.setDate(QDate(2026, 12, 1))
    dialog.until_edit.setDate(QDate(2026, 12, 2))
    states["пустой период"] = _card_texts(dialog.card)
    for state, marker in (
        ("полоса", "% ушло на комиссию"),
        ("долей нет", "при прибыли до комиссии"),
        ("тариф не задан", "Чистого результата нет"),
        ("итог не по всем", "Итог посчитан по 2 сделкам из 3"),
        ("пустой период", NO_PERIOD_TRADES),
    ):
        assert any(marker in text for text in states[state]), (
            f"проверка вакуумна: в состоянии «{state}» нет его слов — {states[state]}"
        )
    for state, texts in states.items():
        found = [word for word in _JUDGEMENTS for text in texts if word in text.lower()]
        assert not found, f"{state}: оценочные слова {found}"


def test_a_trade_without_a_result_is_a_row_but_not_a_share_of_the_average(open_report) -> None:
    """С2. Сделка без результата (позиция открыта): строка в «Сделок» есть, в среднем — нет.

    Пункты +300 и −100 при комиссии 28 ₽ и третья сделка без результата.
    Руками: чистый 272 − 128 = +144 ₽; сделок — три строки таблицы; средний
    чистый — на две сделки с результатом, +72 ₽, а не 144 / 3 = +48 ₽.
    С1 проверяет средний как «чистый / строк таблицы» — там это то же самое
    только потому, что результат известен у всех строк.
    Оговорка «по 2 из 3» на экране: итог по части сделок без неё читался бы
    как итог по всем (правило 13).
    """
    dialog = open_report(_with_open_position(_by_points(300, -100)))
    assert dialog.table.rowCount() == 3, "проверка вакуумна: сделки без результата нет в таблице"
    assert _value(dialog, "net") == "+144,00 ₽"
    assert _value(dialog, "trades") == "3"
    assert _value(dialog, "average") == "+72,00 ₽"
    # Доля прибыльных — тоже от сделок с результатом (подсказка ячейки): 1 из 2, не 1 из 3.
    counts = _plain(dialog.card.counts.text())
    assert counts == "прибыльных 1 · убыточных 1 · 50 % прибыльных", counts
    lines = _card_lines(dialog.card)
    assert (
        "Итог посчитан по 2 сделкам из 3: по остальным прогон результата не передал."
        in lines
    ), lines


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
    """С6. Пустой период — одной строкой в карточке, без нулей (правило 13).

    Стережёт: окно не показывает пустую таблицу и нули вместо ответа, не молчит
    (карточка на месте) и не говорит «сделок нет» дважды: подсказка ниже —
    только где сделки прогона.
    """
    made = ReportsDialog(_shown(), today=lambda: date(2026, 12, 15))
    try:
        assert made.shown_trades() == []
        assert not made.card.isHidden(), "пустой период — молча, карточки нет"
        lines = _card_lines(made.card)
        assert lines == ["MXZ6 · 15 дней, 01.12–15.12.2026", NO_PERIOD_TRADES], lines
        assert not any(re.search(r"\d ₽|\d+ %", line) for line in lines), lines
        hint = _plain(made.empty_label.text())
        assert not made.empty_label.isHidden()
        assert "«Всё»" in hint and "с 21.09.2026 по 02.10.2026, всего 5" in hint, hint
        assert "сделок нет" not in hint.lower(), f"одна фраза дважды на экране: {hint}"
        assert made.splitter.isHidden(), "пустая таблица осталась на экране"
    finally:
        made.deleteLater()


def test_no_run_yet_is_said_in_words(qapp) -> None:
    """Прогона не было или сделок в нём нет — окно говорит это, а не молчит."""
    for run, text in ((None, NO_RUN), (ShownRun(), NO_TRADES)):
        made = ReportsDialog(run, today=lambda: TODAY)
        assert made.empty_label.text() == text and not made.empty_label.isHidden()
        assert made.splitter.isHidden()
        assert made.card.isHidden(), "карточка итога без прогона или без сделок"
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


def test_the_reports_window_names_what_the_stitched_run_counted_on(window) -> None:
    """Стережёт B-069: строка «Считается на:» со снимка графика — в заголовке «Отчётов».

    Порт кладёт её в `ChartData.basis`; окно только показывает. Мутация:
    главное окно не передало строку в `ShownRun` — в «Отчётах» её нет.
    """
    basis = "Считается на: MXU6 18.06–16.09 (57 дн. со свечами; нет 07.09–16.09)"
    report = _report()
    start = datetime(2026, 9, 21, 10, 0, tzinfo=MSK)
    window.show_chart(ChartData(instrument="MXZ6", candles=_candles(start, 3), basis=basis))
    window.set_trades(report.trades, report.summary)
    window.journals.reports_button.click()
    assert basis in window.reports_dialog.title_label.text().splitlines()
    window.show_chart(ChartData(instrument="MXZ6", candles=_candles(start, 3)))
    window.set_trades(report.trades, report.summary)
    assert "Считается на" not in window.reports_dialog.title_label.text(), (
        "строка прогона по склейке осталась у прогона одним контрактом"
    )


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
