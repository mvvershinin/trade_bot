"""Путь от кнопки до отчёта: диалог прогона, окно отчёта, проводка в порту.

Что здесь проверяется и почему именно это
------------------------------------------
Этап Э1 закрывается тем, что владелец счёта **мышкой** получает отчёт
о прогоне на истории. Ломается этот путь молча, и мест, где он ломается,
шесть — каждому здесь свой сторож:

1. в список инструментов попал собранный нами ряд `@MX`, по которому торговать
   нельзя (решение 0049);
2. прошлые бары уехали в живой ход движка и испортили его среднюю;
3. отчёт показал деньги без оговорки про нулевое проскальзывание;
4. «сколько дней» посчитано календарём, а не торгами;
5. отмена не отменяет;
6. закреплённый отрезок «залип» молча — живых свечей нет, а сказать об этом
   некому.

Торговых утверждений здесь нет ни одного: что робот принял верное решение,
проверяют `engine/` и сверка с прототипом.
"""

from __future__ import annotations

import asyncio
import math
import os
import pathlib
from datetime import datetime, timedelta

import pytest

os.environ.setdefault("QT_API", "pyside6")  # до первого импорта qasync

from app.port import HistoryPort
from market import MSK, Candle, CandleStore, MarketWorker, Source, Timeframe
from market.journal import redact
from ui.models import (
    BacktestOptions,
    BacktestReport,
    BacktestRequest,
    InstrumentInfo,
    RunAssumption,
    Settings,
    TradesSummary,
)
from ui.ports import TerminalPort

DAY = datetime(2026, 6, 19, 7, 0, tzinfo=MSK)  # пятница, до открытия окна


def _minutes(count: int, start: datetime) -> list[Candle]:
    """Минутки с колебанием вокруг ровной цены: без движения сделок не будет."""
    out = []
    for index in range(count):
        price = 100000.0 + 300.0 * math.sin(index / 9.0)
        out.append(Candle(
            time=start + timedelta(minutes=index),
            open=price, high=price + 40.0, low=price - 40.0, close=price,
            volume=1.0, timeframe=Timeframe(1), filled_minutes=1,
        ))
    return out


#: На какие дни от `DAY` в базе есть свечи. Три дня, и **с разрывом**:
#: 19, 20 и 24 июня. Разрыв здесь не для красоты — он отделяет «сколько дней
#: с торгами» от «сколько дней в отрезке». На трёх днях подряд оба счёта дают
#: тройку, и подмена одного другим прошла бы молча.
TRADING_DAYS = (0, 1, 5)


@pytest.fixture
def database(tmp_path: pathlib.Path) -> pathlib.Path:
    """Три торговых дня по одному инструменту, разнесённые по календарю."""
    path = tmp_path / "candles.sqlite3"
    with CandleStore(path) as store:
        for shift in TRADING_DAYS:
            store.put_minutes(
                "MXU6", _minutes(300, DAY + timedelta(days=shift)), Source.ISS
            )
    return path


class _Heard:
    """Что порт отправил в окно по части прогона."""

    def __init__(self, port: HistoryPort) -> None:
        self.options: list[BacktestOptions] = []
        self.reports: list[BacktestReport] = []
        self.states: list[object] = []
        self.failures: list[str] = []
        self.notes: list[object] = []
        self.charts: list[object] = []
        port.chart_replaced.connect(self.charts.append)
        port.backtest_options_ready.connect(self.options.append)
        port.backtest_finished.connect(self.reports.append)
        port.state_changed.connect(self.states.append)
        port.failed.connect(self.failures.append)
        port.decision_appended.connect(self.notes.append)


def _request(
    *,
    settings: Settings | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> BacktestRequest:
    """Просьба о прогоне по всему, что лежит в базе фикстуры.

    Доводы названы поимённо, а не собраны словарём: словарь пришлось бы
    подавлять проверке типов, а подавление в оснастке — это то же самое,
    что не проверять оснастку вовсе.
    """
    return BacktestRequest(
        settings=settings if settings is not None
        else Settings(instrument="MXU6", timeframe="5 минут"),
        since=since if since is not None else DAY.replace(hour=0, minute=0),
        until=until if until is not None else DAY + timedelta(days=7),
        settings_source="текущие настройки",
    )


def with_port(loop, database: pathlib.Path, work):
    """Открыть базу, отдать порт работе, закрыть всё."""

    async def go():
        worker = MarketWorker(database)
        await worker.open()
        port = HistoryPort(worker, values=Settings(), sanitize=redact, days=0)
        heard = _Heard(port)
        try:
            await work(port, heard)
        finally:
            await port.aclose()
            await worker.close()
        return port, heard

    return loop.run_until_complete(go())


# ------------------------------------------------- список инструментов


def test_the_instrument_list_offers_what_the_base_has(loop, database) -> None:
    """В списке — код из базы и границы его истории.

    Границы нужны диалогу не для красоты: по ним он прижимает календари,
    и без них человек выбрал бы даты, за которые данных нет, а объяснять
    это пришлось бы пустым отчётом.
    """

    async def work(port, heard):
        port.request_backtest_options()
        await port.wait()
        assert heard.options, "порт не ответил списком инструментов"
        options = heard.options[-1]
        assert not options.trouble, options.trouble
        found = {item.symbol: item for item in options.instruments}
        assert "MXU6" in found, f"инструмента из базы нет в списке: {list(found)}"
        one = found["MXU6"]
        assert one.first is not None and one.last is not None, (
            "границы истории не названы: диалогу нечем прижать календари"
        )
        assert one.first < one.last
        assert one.minutes > 0, "число минуток не передано"

    with_port(loop, database, work)


def test_a_stitched_series_never_appears_in_the_instrument_list(
    loop, tmp_path
) -> None:
    """Ряд `@MX` в выборе инструмента не показывается никогда.

    Цена ошибки названа в решении 0049: цены на стыках такого ряда
    не подгоняются, кода `@MX` на бирже нет, и заявка по нему невозможна.
    Ряд, попавший в список выбора, рано или поздно будет выбран.

    ⚠️ Ряд кладётся в базу **в обход** `CandleStore.put_minutes`: тот сам
    отказывается писать синтетику в рабочую базу. Здесь проверяется второй
    рубеж — список выбора, — и он обязан держать даже базу, испорченную
    руками.
    """
    import sqlite3

    path = tmp_path / "candles.sqlite3"
    with CandleStore(path) as store:
        store.put_minutes("MXU6", _minutes(60, DAY), Source.ISS)
    with sqlite3.connect(path) as db:
        # Колонки перечисляются явно: `SELECT *` со звёздочкой сломался бы
        # на первой же новой колонке хранилища, и сторож упал бы не на том,
        # что стережёт.
        db.execute(
            "INSERT INTO minute_candle "
            "(symbol, ts, open, high, low, close, volume, source, source_rank) "
            "SELECT '@MX', ts, open, high, low, close, volume, source, source_rank "
            "FROM minute_candle WHERE symbol = 'MXU6'"
        )

    async def work(port, heard):
        port.request_backtest_options()
        await port.wait()
        await asyncio.sleep(0)
        assert heard.options, "порт не ответил списком инструментов"
        codes = [item.symbol for item in heard.options[-1].instruments]
        assert "@MX" not in codes, (
            "собранный ряд попал в выбор инструмента: по коду, которого нет "
            f"на бирже, будет отправлена заявка. Список: {codes}"
        )
        assert "MXU6" in codes, "настоящий инструмент из списка пропал вместе с рядом"

    with_port(loop, path, work)


# ------------------------------------------------------------- прогон


def test_a_run_reaches_the_report_with_money_split_three_ways(loop, database) -> None:
    """Отчёт приходит и несёт валовую, комиссию и чистую **порознь**.

    Валовая прибыль в этом проекте результатом не является (`DOMAIN.md` §5):
    реверсная система на пятиминутках делает много переворотов, и комиссия —
    первый кандидат съесть весь плюс. Одно число вместо трёх скрыло бы это.
    """

    async def work(port, heard):
        port.run_backtest(_request())
        await port.wait()
        assert heard.reports, f"отчёта нет; отказы: {heard.failures}"
        report = heard.reports[-1]
        assert report.instrument == "MXU6"
        assert report.bars > 0, "прогон не увидел ни одной свечи"
        assert report.summary is not None
        assert report.summary.gross_profit_rub is not None
        assert report.summary.commission_rub is not None
        assert report.summary.net_profit_rub is not None
        assert report.trades, "сделок нет — проверять нечего"

    with_port(loop, database, work)


def test_the_report_counts_trading_days_not_calendar_ones(loop, database) -> None:
    """Торговых дней столько, сколько дней с торгами, а не дней в отрезке.

    В базе три дня с торгами, разнесённые по календарю на шесть суток
    (`TRADING_DAYS`). Разница не косметическая: прибыль «за год» и «за 280
    торговых дней» человек делит на разное.
    """

    async def work(port, heard):
        port.run_backtest(_request())
        await port.wait()
        report = heard.reports[-1]
        assert report.trading_days == len(TRADING_DAYS), (
            "число торговых дней посчитано не по свечам: в базе "
            f"{len(TRADING_DAYS)} дня с торгами, отчёт говорит "
            f"{report.trading_days}"
        )
        assert report.first_bar is not None and report.last_bar is not None
        calendar = (report.last_bar.date() - report.first_bar.date()).days + 1
        assert calendar > report.trading_days, (
            "выборка выбрана неудачно: календарных дней в ней столько же, "
            "сколько торговых, и подмена одного другим этим тестом не ловится"
        )

    with_port(loop, database, work)


def test_the_report_says_what_is_missing_from_the_money(loop, database) -> None:
    """Рядом с деньгами едет оговорка, и в ней названо проскальзывание.

    Умолчание проскальзывания — ноль, поэтому итог систематически лучше
    настоящего счёта. Замер 05.09.2026: один шаг забирает половину итога,
    два уводят в минус. Число без этой оговорки обманывает владельца счёта
    деньгами.
    """

    async def work(port, heard):
        port.run_backtest(_request())
        await port.wait()
        report = heard.reports[-1]
        assert report.assumptions, "список допущений пуст — отчёт выдан за выписку"
        beside = [item for item in report.assumptions if item.beside_number]
        assert beside, "ни одно допущение не помечено «стоять рядом с числом»"
        whole = "\n".join(item.text for item in report.assumptions)
        assert "роскальзывани" in whole, (
            "в допущениях не сказано про проскальзывание, а оно нулевое"
        )
        assert report.summary is not None
        assert "расчёт, а не выписка" in report.summary.headline

    with_port(loop, database, work)


def test_the_report_names_both_the_asked_span_and_the_found_one(loop, database) -> None:
    """Границы «просили» и «нашлось» — разные поля и обе заполнены."""

    async def work(port, heard):
        port.run_backtest(_request())
        await port.wait()
        report = heard.reports[-1]
        assert report.asked_since is not None and report.asked_until is not None
        assert report.first_bar is not None and report.last_bar is not None
        assert report.asked_since <= report.first_bar
        assert report.last_bar <= report.asked_until

    with_port(loop, database, work)


def test_the_pinned_span_is_announced_in_the_state(loop, database) -> None:
    """Закреплённый отрезок виден в снимке состояния — иначе он «залипнет» молча.

    Пока отрезок стоит, живые свечи на график не приходят вовсе. Застывший
    график при живом потоке неотличим от потерянной связи, и разбираться
    в этом владелец счёта будет с секундомером.
    """

    async def work(port, heard):
        port.run_backtest(_request())
        await port.wait()
        assert heard.states[-1].history_span, (
            "закреплённый отрезок не назван в снимке состояния: плашке "
            "в окне неоткуда взяться"
        )
        port.show_recent()
        await port.wait()
        assert not heard.states[-1].history_span, (
            "отрезок снят, а снимок состояния всё ещё говорит о нём"
        )

    with_port(loop, database, work)


def test_a_pinned_span_keeps_the_engine_off_the_live_path(loop, database) -> None:
    """Пока отрезок закреплён, живой ход движка не собирается.

    Бары июня, поданные наблюдателю, легли бы в его среднюю: она перестала бы
    быть той, что была бы у робота в бою, и ни одно решение после этого
    не значило бы ничего.
    """

    async def work(port, heard):
        port._watch.observing = True  # noqa: SLF001 — то же делает кнопка связи
        port.run_backtest(_request())
        await port.wait()
        assert port._watch.observer is None, (  # noqa: SLF001 — состояние порта
            "при закреплённом отрезке собрался живой ход движка: прошлые бары "
            "уехали в его среднюю"
        )
        assert heard.reports, "отчёта нет"

    with_port(loop, database, work)


def test_a_pinned_span_stops_the_live_candle_from_being_drawn(loop, database) -> None:
    """Живая свеча на закреплённый отрезок не рисуется."""

    async def work(port, heard):
        assert port._shown(_frame_of(port), "MXU6") is not None, (  # noqa: SLF001
            "до прогона живая свеча не рисовалась вовсе — проверять нечего"
        )
        port.run_backtest(_request())
        await port.wait()
        assert port._shown(_frame_of(port), "MXU6") is None, (  # noqa: SLF001
            "живая свеча рисуется поверх закреплённого отрезка истории"
        )
        port.show_recent()
        await port.wait()
        assert port._shown(_frame_of(port), "MXU6") is not None, (  # noqa: SLF001
            "отрезок сняли, а живая свеча так и не вернулась"
        )

    with_port(loop, database, work)


def _frame_of(port: HistoryPort):
    """Снимок настроек прохода — тем же способом, что берёт его сам порт."""
    from app.port import _Frame

    return _Frame(port._values, port._engine_settings)  # noqa: SLF001 — поля порта


def test_a_cancelled_run_really_stops(loop, database) -> None:
    """Отмена **останавливает счёт**, а не только прячет отчёт.

    Проверяется наблюдаемое последствие: график не перерисован. Одного
    «отчёта нет» мало — снять просьбу об отчёте и дать прогону досчитать
    до конца выглядело бы отсюда так же, а на деле означало бы кнопку
    «Отменить», которая ничего не отменяет.
    """

    async def work(port, heard):
        port.run_backtest(_request())
        await asyncio.sleep(0)
        drawn = len(heard.charts)
        port.cancel_backtest()
        await port.wait()
        assert not heard.reports, "отменённый прогон всё равно выдал отчёт"
        assert len(heard.charts) == drawn, (
            "отменённый прогон досчитал и перерисовал график: отмена не отменяет"
        )
        assert port._task is not None and port._task.cancelled(), (  # noqa: SLF001
            "задача прогона не снята — счёт продолжается после «Отменить»"
        )
        said = [row.event for row in heard.notes]
        assert "Прогон отменён" in said, (
            f"об отмене не сказано ни строки: {said}"
        )

    with_port(loop, database, work)


def test_a_second_run_is_refused_while_the_first_is_running(loop, database) -> None:
    """Два прогона разом дали бы в окно два разных ответа на один вопрос.

    ⚠️ Проверяется не сам отказ, а **что в нём сказано**. Прежняя редакция
    искала слова «Прогон уже идёт» и была зелена при отказе, который человеку
    ничего не объясняет: `B-025` — это ровно про такой случай, только доведённый
    до конца (задача застряла навсегда, и окно молча отклоняло всё подряд).
    Отказ обязан назвать, **чем** порт занят и **сколько** уже.
    """

    async def work(port, heard):
        port.run_backtest(_request())
        await asyncio.sleep(0)
        port.run_backtest(_request())
        assert heard.failures, "второй прогон приняли молча"
        said = heard.failures[-1]
        assert "прогон робота по истории" in said, (
            f"отказ не назвал, чем программа занята: {said!r}"
        )
        assert "идёт уже" in said and " с." in said, (
            f"отказ не сказал, сколько работа уже идёт: {said!r}"
        )
        await port.wait()

    with_port(loop, database, work)


def test_bad_settings_move_neither_the_span_nor_the_settings(loop, database) -> None:
    """Негодные настройки отвергаются **до** записи отрезка.

    Иначе неудачная попытка оставила бы за собой закреплённый отрезок при
    прежних настройках: график уехал бы в прошлое от того, что не сработало.
    """

    async def work(port, heard):
        before = port._values  # noqa: SLF001 — сравнение состояния порта
        port.run_backtest(_request(settings=Settings(timeframe="полчасика")))
        await port.wait()
        assert not heard.reports, "прогон с негодными настройками выдал отчёт"
        assert port._values == before, "отвергнутые настройки всё же применились"  # noqa: SLF001
        assert port._back.span is None, "отрезок закрепился от неудачной попытки"  # noqa: SLF001
        assert any("не начат" in row.event for row in heard.notes), (
            f"отказ не попал в журнал решений: {[r.event for r in heard.notes]}"
        )
        assert any("не знает" in text for text in heard.failures), (
            f"человеку не сказали, что именно не годится: {heard.failures}"
        )

    with_port(loop, database, work)


def test_a_run_over_an_empty_span_says_so_and_promises_no_report(
    loop, database
) -> None:
    """Свечей на отрезок нет — отчёта нет, и об этом сказано, а не промолчано."""

    async def work(port, heard):
        empty = DAY - timedelta(days=400)
        port.run_backtest(_request(since=empty, until=empty + timedelta(days=1)))
        await port.wait()
        assert not heard.reports, "отчёт собран на отрезке без единой свечи"
        assert heard.failures, "про пустой отрезок не сказано ни слова"

    with_port(loop, database, work)


# ------------------------------------------------------------ окно диалога


def test_the_dialog_builds_a_request_from_what_was_picked(qapp) -> None:
    """Диалог складывает выбор в просьбу: инструмент внутри настроек, даты — МСК."""
    from ui.backtest_dialog import CURRENT_SETTINGS, BacktestDialog

    options = BacktestOptions(instruments=(
        InstrumentInfo("MXU6", DAY, DAY + timedelta(days=30), 4000),
    ))
    dialog = BacktestDialog(options, Settings(instrument="ЧУЖОЙ"))
    try:
        request = dialog.request()
        assert request is not None
        assert request.settings.instrument == "MXU6", (
            "инструмент не подставлен в настройки: прогон пойдёт не по тому "
            "ряду, что показан в окне настроек"
        )
        assert request.settings_source == CURRENT_SETTINGS
        assert request.since.tzinfo is not None and request.until.tzinfo is not None
        assert request.since < request.until
        assert request.until.hour == 23, (
            "последний день отрезка обрезан полуночью: торговый день потерян"
        )
    finally:
        dialog.deleteLater()


def test_the_dialog_will_not_offer_dates_outside_the_base(qapp) -> None:
    """Даты вне того, что есть в базе, выбрать нельзя."""
    from ui.backtest_dialog import BacktestDialog

    first, last = DAY, DAY + timedelta(days=10)
    options = BacktestOptions(instruments=(InstrumentInfo("MXU6", first, last, 100),))
    dialog = BacktestDialog(options, Settings())
    try:
        assert dialog.since.minimumDate().toPython() == first.date()
        assert dialog.until.maximumDate().toPython() == last.date()
    finally:
        dialog.deleteLater()


def test_the_dialog_says_when_there_is_nothing_to_run_on(qapp) -> None:
    """Пустая база — слова, а не пустой список и погасшая кнопка без причины."""
    from ui.backtest_dialog import BacktestDialog

    dialog = BacktestDialog(BacktestOptions(), Settings())
    try:
        assert dialog.request() is None
        assert not dialog.run_button.isEnabled()
        assert "не" in dialog.summary.text().lower(), (
            f"окно молчит о том, почему нельзя прогнать: {dialog.summary.text()!r}"
        )
    finally:
        dialog.deleteLater()


def test_the_dialog_warns_that_a_run_changes_the_settings(qapp) -> None:
    """Про смену настроек и про застывший график сказано в самом окне.

    Не в докстринге и не в документации: последствие, которого человек
    не ждёт, обязано стоять перед кнопкой, которую он нажимает.
    """
    from ui.backtest_dialog import BacktestDialog

    options = BacktestOptions(instruments=(
        InstrumentInfo("MXU6", DAY, DAY + timedelta(days=5), 100),
    ))
    dialog = BacktestDialog(options, Settings())
    try:
        texts = " ".join(
            child.text() for child in dialog.findChildren(type(dialog.summary))
        )
        assert "Параметры робота" in texts, "не сказано, что настройки сменятся"
        assert "Живые свечи" in texts, "не сказано, что живые свечи перестанут идти"
    finally:
        dialog.deleteLater()


# ------------------------------------------------------------- окно отчёта


#: Итог, с которым собирается образцовый отчёт. Числа взяты правдоподобные,
#: но подгонять их не подо что: проверяется, что они **показаны**, а не какие
#: они.
_SUMMARY = TradesSummary(
    trades=4, profitable_share=0.5, net_profit_rub=1000.0,
    gross_profit_rub=1112.0, commission_rub=112.0, max_drawdown_rub=50.0,
    headline="⚠️ Это расчёт, а не выписка со счёта.",
)

#: Допущение с замеренным числом внутри абзаца. Число нужно тесту: по нему
#: видно, показан ли абзац целиком или только заголовок.
_ASSUMPTION = RunAssumption(
    name="Проскальзывание не учтено",
    short="Цена каждой сделки взята расчётная.",
    text="Один шаг проскальзывания забрал 5 514 ₽ из 9 908 ₽ итога.",
    beside_number=True,
)


def _report(
    *,
    summary: TradesSummary | None = _SUMMARY,
    assumptions: tuple[RunAssumption, ...] = (_ASSUMPTION,),
) -> BacktestReport:
    """Образцовый отчёт для окна. Меняются только те два поля, что нужны тестам."""
    return BacktestReport(
        instrument="MXU6",
        timeframe="5 минут",
        asked_since=DAY,
        asked_until=DAY + timedelta(days=3),
        first_bar=DAY + timedelta(minutes=5),
        last_bar=DAY + timedelta(days=2, hours=4),
        bars=180,
        trading_days=3,
        settings_source="текущие настройки",
        settings_text="Настройки движка\n  Период средней: 15",
        summary=summary,
        assumptions=assumptions,
    )


def test_the_report_window_shows_the_three_money_lines(qapp) -> None:
    """Валовая, комиссия и чистая — тремя подписями, а не одной «прибылью»."""
    from PySide6.QtWidgets import QLabel

    from ui.backtest_report import BacktestReportDialog

    window = BacktestReportDialog(_report())
    try:
        texts = [child.text() for child in window.findChildren(QLabel)]
        joined = " ".join(texts)
        for word in ("Валовая", "Комиссия", "Чистая прибыль"):
            assert any(word in text for text in texts), (
                f"в окне отчёта нет строки «{word}»: {joined[:400]}"
            )
    finally:
        window.deleteLater()


def test_the_report_window_says_the_span_and_the_trading_days(qapp) -> None:
    """На каком отрезке считано и сколько в нём торговых дней — на экране."""
    from ui.backtest_report import period_lines

    lines = " ".join(period_lines(_report()))
    assert "Просили" in lines and "Посчитано" in lines
    assert "Торговых дней: 3" in lines, (
        f"число торговых дней не показано: {lines}"
    )


def test_the_report_window_shows_every_assumption_in_full(qapp) -> None:
    """Допущения показаны абзацами целиком, а не одним заголовком."""
    from PySide6.QtWidgets import QLabel

    from ui.backtest_report import BacktestReportDialog

    window = BacktestReportDialog(_report())
    try:
        texts = " ".join(child.text() for child in window.findChildren(QLabel))
        assert "5 514" in texts, (
            "полный текст допущения не показан: замер про проскальзывание "
            "остался в докстринге"
        )
        assert "расчёт, а не выписка" in texts, "оговорка рядом с числом пропала"
    finally:
        window.deleteLater()


def test_the_report_window_survives_a_run_without_a_summary(qapp) -> None:
    """Отчёт без сводки открывается и говорит об этом, а не падает."""
    from ui.backtest_report import BacktestReportDialog

    window = BacktestReportDialog(_report(summary=None, assumptions=()))
    try:
        assert window.isEnabled()
    finally:
        window.deleteLater()


# ------------------------------------------------------- проводка в окне


class _Quiet(TerminalPort):
    """Порт, который запоминает команды окна вместо того, чтобы их выполнять."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    def request_backtest_options(self) -> None:
        self.calls.append("options")

    def run_backtest(self, request) -> None:
        self.calls.append("run")

    def cancel_backtest(self) -> None:
        self.calls.append("cancel")

    def show_recent(self) -> None:
        self.calls.append("recent")


@pytest.fixture
def window(qapp):
    """Главное окно на порту-молчуне. Часы остановлены: тесту они не нужны."""
    from market import redact
    from ui.main_window import MainWindow

    made = MainWindow(port=_Quiet(), sanitize=redact)
    made._timer.stop()  # noqa: SLF001 — часы окна тесту не нужны
    made.show()
    qapp.processEvents()
    yield made
    made.close()
    made.deleteLater()
    qapp.processEvents()


def test_the_window_warns_while_a_history_span_is_pinned(window, qapp) -> None:
    """Пока отрезок закреплён, в окне висит плашка и есть чем её снять.

    Живых свечей на закреплённый отрезок не приходит вовсе. Застывший график
    при живом потоке котировок читается как потерянная связь — и разбираться
    в этом владелец счёта будет с секундомером в руках.
    """
    from ui.models import RobotState

    assert not window.history_banner.isVisible(), "плашка висит без прогона"
    assert not window.recent_action.isVisible(), "выход есть, а выходить не из чего"

    window.apply_state(RobotState(history_span="Показан прогон на истории: июнь."))
    qapp.processEvents()
    assert window.history_banner.isVisible(), (
        "закреплённый отрезок ничем не показан: график застыл молча"
    )
    assert "прогон на истории" in window.history_banner.text()
    assert window.recent_action.isVisible(), "снять отрезок нечем"

    window.apply_state(RobotState())
    qapp.processEvents()
    assert not window.history_banner.isVisible()
    assert not window.recent_action.isVisible()


def test_returning_to_current_data_reaches_the_port(window) -> None:
    """Кнопка возврата отдаёт команду порту, а не делает что-то в окне."""
    window.recent_action.trigger()
    assert window.port.calls == ["recent"]


def test_the_cross_on_the_banner_unpins_the_span_instead_of_hiding_it(
    window, qapp
) -> None:
    """Стережёт `B-032`: крестик снимает закрепление, а не прячет плашку.

    Просьба владельца счёта 06.09.2026: «как эту залупу убрать? сделай там
    крестик что ли». Крестик, который просто прячет плашку, — это ложь:
    живые свечи на закреплённый отрезок по-прежнему не приходят, а сказать
    об этом стало нечем.

    ⚠️ Проверка «плашка спряталась» не годится ни при какой мутации: она
    зеленеет ровно на той ошибке, которую здесь ловят (`hide()` вместо
    действия). Поэтому сверяется **команда порту**.
    """
    from ui.models import RobotState

    window.apply_state(RobotState(history_span="Показан прогон на истории: июнь."))
    qapp.processEvents()
    cross = window.history_banner.close_button
    assert cross is not None, "крестика на плашке нет — убрать её нечем"

    cross.click()
    assert window.port.calls == ["recent"], (
        "крестик не снял закреплённый отрезок: плашка исчезла, а живых свечей "
        "как не было, так и нет"
    )


def test_the_cross_does_not_eat_the_text_of_the_banner(window, qapp) -> None:
    """Стережёт то, что уже сломалось при первой попытке: обрезанный текст.

    Крестик, положенный в раскладку **внутрь** `QLabel`, отбирает у надписи
    её собственный размер: плашка начинает мерить себя по кнопке. Высота
    упала с 68 точек до 42, и вторая строка предупреждения обрезалась
    на середине. Поймано снимком экрана, а не рассуждением.

    ⚠️ Проверка «крестик виден» этого не ловит: он-то как раз виден.
    """
    from ui.models import RobotState

    # ⚠️ Окно сужается намеренно. На широком окне текст ложится в одну
    # строку и вертикального запаса хватает всем — сломанная плашка выглядит
    # целой. Беда видна там, где надписи нужно две строки, а места впритык.
    window.resize(1200, 700)
    qapp.processEvents()
    window.apply_state(RobotState(history_span=(
        "Показан прогон на истории: 08.06.2026 10:05 — 05.09.2026 18:59 МСК, "
        "настройки: текущие настройки. Живые свечи на этот отрезок не приходят."
    )))
    qapp.processEvents()
    banner = window.history_banner

    # Мерка — такая же плашка **без** крестика, с тем же текстом и той же
    # шириной. Спрашивать высоту у самой плашки бесполезно: раскладка внутри
    # `QLabel` уменьшает и её ответ тоже, и проверка сходится сама с собой.
    from ui.main_window import _Banner

    plain = _Banner()
    plain.show_text(banner.text(), window.theme.warning)
    plain.resize(banner.width(), plain.heightForWidth(banner.width()))
    needed = plain.heightForWidth(banner.width())
    plain.deleteLater()

    assert banner.height() >= needed, (
        f"плашка {banner.height()} точек при нужных {needed}: крестик отобрал "
        "у надписи её размер, и предупреждение читается наполовину"
    )


def test_no_cross_on_the_banner_that_must_not_be_dismissed(window) -> None:
    """Стережёт обратное: плашку остановки крестиком не смахнуть.

    Робот, вставший по дневному лимиту убытка, возобновляется только явно
    (решение 0045, `D-043`). Крестик на этой плашке означал бы, что
    сообщение о вставшем роботе можно закрыть и забыть.
    """
    for name in ("halt_banner", "token_banner", "mode_banner"):
        banner = getattr(window, name)
        assert banner.close_button is None, (
            f"на плашке «{name}» появился крестик: её сообщение можно смахнуть, "
            "а снять её состояние — нечем"
        )


def test_the_run_item_asks_the_port_for_the_instrument_list(window) -> None:
    """«Прогон на истории…» не читает базу сам: он просит порт."""
    window.open_backtest()
    assert window.port.calls == ["options"], (
        "пункт меню либо ничего не спросил, либо полез в базу из окна"
    )


def test_the_progress_bar_appears_and_its_cancel_reaches_the_port(window, qapp) -> None:
    """Полоска показывается, отмена уходит движку, конец операции её убирает.

    Полоску закрывает **конец длинной операции**, а не приход отчёта: отчёта
    может не быть вовсе (свечей на отрезок не нашлось, настройки отвергнуты),
    и полоска, закрываемая только отчётом, осталась бы на экране навсегда.
    """
    window._start_progress()  # noqa: SLF001 — то же делает `show_backtest_dialog`
    qapp.processEvents()
    assert window._progress is not None  # noqa: SLF001
    window._on_progress(37, "Прогон по истории")  # noqa: SLF001
    assert window._progress.value() == 37  # noqa: SLF001

    window._progress.canceled.emit()  # noqa: SLF001 — нажатие «Отменить»
    qapp.processEvents()
    assert "cancel" in window.port.calls, "отмена не дошла до движка"

    window._on_busy(False, "")  # noqa: SLF001 — длинная операция кончилась
    assert window._progress is None, "полоска осталась на экране после прогона"  # noqa: SLF001


def test_closing_the_progress_bar_does_not_cancel_anything(window, qapp) -> None:
    """Закрытие полоски по концу прогона отменой не является.

    ⚠️ `QProgressDialog.close()` испускает `canceled`. Без явного отключения
    сигнала окно отправляло бы движку отмену **уже законченного** прогона —
    а следующий прогон, начатый сразу после, снялся бы этой отменой.
    """
    window._start_progress()  # noqa: SLF001
    qapp.processEvents()
    window._on_busy(False, "")  # noqa: SLF001 — прогон кончился штатно
    qapp.processEvents()
    assert "cancel" not in window.port.calls, (
        "закрытие полоски отправило движку отмену законченного прогона"
    )


def test_the_report_window_opens_from_the_port_signal(window, qapp) -> None:
    """Отчёт приходит сигналом порта и открывается окном, а не теряется."""
    window.show_backtest_report(_report())
    qapp.processEvents()
    assert window.report_dialog.isVisible(), "окно отчёта не открылось"
    window.report_dialog.close()
