"""Сделки симуляции из базы в «Отчётах»: «как было», «пересчёт с утра», разбор.

Каждый тест стережёт **одно** поведение, и оно названо первой строкой
докстринга. Предметы — отбор (`app/paper_report.py`), чтение порта
(`HistoryPort.request_paper_report`) и показ в окне (`ui/reports_dialog.py`).

Вход подставной (правило 14): прогоны и строки отбора собраны руками
по образцу настоящих — имена заявок, касание уровня позже времени сделки,
исполнение по открытию бара, — база во временной папке, живой ход —
синтетической лентой баров (`tests/test_app_paper_log.py`).
"""

from __future__ import annotations

import dataclasses
import pathlib
import sqlite3
import time as clock
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta
from datetime import time as day_time

from PySide6.QtCore import QTimer

from app import convert, paper_report
from app.paper_report import RunBook, RunRows, as_was, recount, story_of
from app.port import HistoryPort
from market import (
    MSK,
    CandleStore,
    ContractRow,
    DecisionPhase,
    JournalSession,
    MarketWorker,
    OrderKind,
    OrderRecord,
    PaperRows,
    PaperRun,
    PaperWritten,
    RunOrigin,
    RunRowsMark,
    SessionRecord,
    StoredTrade,
    TradeRecord,
    TradeSide,
    redact,
)
from market import (
    RunOrigin as StoredOrigin,
)
from tests.helpers import RecordingPort
from tests.test_app_observe import Said, bars
from tests.test_app_paper_log import SYMBOL, observer
from ui.main_window import MainWindow
from ui.models import Candle, ChartData, Settings
from ui.reports_dialog import PAPER_WAIT, ReportsDialog
from ui.trade_story import (
    PaperReport,
    PaperReportRequest,
    Phase,
    ReportSource,
    story_lines,
)

#: Среда: день двух прогонов. Накануне — вторник, назавтра — четверг.
DAY = date(2026, 10, 7)
BAR = timedelta(minutes=5)


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, day_time(hour, minute), tzinfo=MSK)


# ------------------------------------------------------------------ сборка входа


def paper_run(
    number: int,
    started: datetime,
    *,
    finished: datetime | None = None,
    live_from: datetime | None = None,
    last_at: datetime | None = None,
    marked: bool = True,
) -> PaperRun:
    """Прогон симуляции, как его отдаёт `CandleStore.paper_runs`."""
    session = JournalSession(
        id=number, origin=StoredOrigin.PAPER, started_at=started, finished_at=finished,
        symbol=SYMBOL, timeframe="5m", settings=f"Период средней: {10 + number}",
    )
    mark = RunRowsMark(kept_since=started - timedelta(days=3), live_from=live_from)
    return PaperRun(session=session, mark=mark if marked else None, last_at=last_at)


class Rows:
    """Сделки и заявки одного прогона — по образцу того, что пишет живой ход.

    Фазу строкам ставит `phase_at` по моменту строки. Умолчание — так, как
    ход ставит её без догона: всё не позже закрытия последнего бара первого
    прохода (`RunRowsMark.live_from`) — пересчёт, позже — живое.
    """

    def __init__(
        self, run: PaperRun, phase_at: Callable[[datetime], DecisionPhase] | None = None
    ) -> None:
        self.run = run
        self.phase_at = phase_at or self._without_catch_up
        self.trades: list[StoredTrade] = []
        self.orders: list[OrderRecord] = []

    def _without_catch_up(self, moment: datetime) -> DecisionPhase:
        border = None if self.run.mark is None else self.run.mark.live_from
        if border is not None and moment <= border:
            return DecisionPhase.RECOUNT
        return DecisionPhase.LIVE

    def _trade(self, name: str, entry: datetime, exit_: datetime, exit_name: str) -> StoredTrade:
        number = self.run.session.id * 100 + len(self.trades)
        trade = StoredTrade(
            id=number, session_id=self.run.session.id, origin=StoredOrigin.PAPER,
            symbol=SYMBOL, side=TradeSide.LONG, volume=1.0,
            entry_time=entry, entry_price=100_000.0 + number,
            exit_time=exit_, exit_price=100_100.0 + number,
            exit_reason="обратный сигнал средней", gross=100.0, written_at=exit_,
            entry_order_id=name, exit_order_id=exit_name, commission=28.0, net=72.0,
        )
        self.trades.append(trade)
        return trade

    def market(self, entry: datetime, exit_: datetime) -> StoredTrade:
        """Вход и выход рыночными: решение на закрытии бара, исполнение — по открытию следующего.

        Время исполнения совпадает со временем решения: открытие следующего
        бара — это закрытие бара решения.
        """
        name = f"open:long:{entry.isoformat()}"
        out = f"close:long:{exit_.isoformat()}"
        self.orders += [
            OrderRecord(name, entry, OrderKind.SUBMITTED, "open", TradeSide.LONG, 1.0,
                        reason="Закрытие выше средней → лонг", close_price=100_000.0,
                        average=99_900.0),
            OrderRecord(out, exit_, OrderKind.SUBMITTED, "close", TradeSide.LONG, 1.0,
                        reason="Закрытие ниже средней — обратный сигнал",
                        exit_reason="signal", close_price=100_100.0, average=100_150.0),
        ]
        return self._trade(name, entry, exit_, out)

    def take(self, entry: datetime, hit: datetime) -> StoredTrade:
        """Выход по тейку: касание — закрытие бара, сделка — момент внутри бара.

        Вооружение повторяется на каждом баре под одним именем, как у движка.
        """
        name = f"open:long:{entry.isoformat()}"
        take = f"take:long:{entry.isoformat()}"
        self.orders.append(
            OrderRecord(name, entry, OrderKind.SUBMITTED, "open", TradeSide.LONG, 1.0,
                        reason="Закрытие выше средней → лонг", close_price=100_000.0,
                        average=99_900.0)
        )
        moment = entry
        while moment < hit:
            self.orders.append(OrderRecord(
                take, moment, OrderKind.SUBMITTED, "arm_take_profit", TradeSide.LONG, 1.0,
                reason="Тейк лонга сторожится на уровне 100 500",
                exit_reason="take_profit", level=100_500.0,
            ))
            moment += BAR
        self.orders.append(OrderRecord(
            take, hit, OrderKind.LEVEL_HIT, "close", TradeSide.LONG, 1.0,
            exit_reason="take_profit", close_price=100_520.0, average=100_200.0,
            level=100_500.0,
        ))
        return self._trade(name, entry, hit - BAR, take)

    def overnight(self, decided: datetime, filled: datetime, exit_: datetime) -> StoredTrade:
        """Вход решён на закрытии вечернего бара, исполнен открытием утреннего.

        Между решением и исполнением ночь: время сделки на часы позже
        времени заявки. Выход — рыночный, как у `market`.
        """
        name = f"open:long:{decided.isoformat()}"
        out = f"close:long:{exit_.isoformat()}"
        self.orders += [
            OrderRecord(name, decided, OrderKind.SUBMITTED, "open", TradeSide.LONG, 1.0,
                        reason="Закрытие выше средней → лонг", close_price=100_000.0),
            OrderRecord(out, exit_, OrderKind.SUBMITTED, "close", TradeSide.LONG, 1.0,
                        reason="Закрытие ниже средней — обратный сигнал",
                        exit_reason="signal", close_price=100_100.0),
        ]
        return self._trade(name, filled, exit_, out)

    def trailing(self, entry: datetime, levels: Iterable[float]) -> StoredTrade:
        """Выход по скользящему уровню: вооружение каждый бар, уровень едет за ценой.

        Последний уровень — тот, что задет: касание на закрытии бара после
        последнего вооружения, сделка — моментом внутри этого бара.
        """
        name = f"open:long:{entry.isoformat()}"
        take = f"take:long:{entry.isoformat()}"
        self.orders.append(
            OrderRecord(name, entry, OrderKind.SUBMITTED, "open", TradeSide.LONG, 1.0,
                        reason="Закрытие выше средней → лонг", close_price=100_000.0)
        )
        moment = entry
        level = 0.0
        for level in levels:
            self.orders.append(OrderRecord(
                take, moment, OrderKind.SUBMITTED, "arm_take_profit", TradeSide.LONG, 1.0,
                reason=f"Скользящий уровень лонга сторожится на {level:.0f}",
                exit_reason="trailing_take", level=level,
            ))
            moment += BAR
        self.orders.append(OrderRecord(
            take, moment, OrderKind.LEVEL_HIT, "close", TradeSide.LONG, 1.0,
            exit_reason="trailing_take", close_price=level - 10.0, level=level,
        ))
        return self._trade(name, entry, moment - BAR, take)

    def read(self) -> RunRows:
        orders = (dataclasses.replace(one, phase=self.phase_at(one.at)) for one in self.orders)
        return RunRows(self.run, tuple(self.trades), tuple(orders))


def ids(picked: Iterable[paper_report.Picked]) -> list[int]:
    return [one.trade.id for one in picked]


def books(runs: Iterable[RunRows]) -> dict[int, RunBook]:
    return {
        rows.run.session.id: RunBook.of(rows.run.session, rows.orders)
        for rows in runs
    }


def two_runs_of_one_day() -> tuple[Rows, Rows]:
    """Утро шёл прогон 1; в полдень «Применить» — прогон 2 пересчитал утро заново.

    Сделки утра у прогона 2 — другие (другие настройки), и решены они
    пересчётом при старте: его первый проход кончился баром, закрытым в 11:55.
    """
    first = Rows(paper_run(1, at(DAY, 9), finished=at(DAY, 12), live_from=at(DAY, 8, 55)))
    first.market(at(DAY - timedelta(days=1), 15), at(DAY - timedelta(days=1), 16))
    first.market(at(DAY, 9, 30), at(DAY, 10))
    first.market(at(DAY, 10, 30), at(DAY, 11))
    second = Rows(paper_run(2, at(DAY, 12), live_from=at(DAY, 11, 55), last_at=at(DAY, 14)))
    second.market(at(DAY, 9, 35), at(DAY, 10, 5))
    second.market(at(DAY, 10, 35), at(DAY, 11, 5))
    second.market(at(DAY, 13), at(DAY, 13, 30))
    return first, second


# ------------------------------------------------------------------ «как было»


def test_as_was_does_not_show_the_morning_a_later_run_recounted() -> None:
    """«Как было» — только сделки, решённые вживую; утро, пересчитанное вторым прогоном, — нет.

    Мутация: отбор без условия «решение выхода живое» — в таблицу
    приходят сделки 9:35 и 10:35 второго прогона рядом с настоящими 9:30
    и 10:30, и утро читается дважды.
    """
    first, second = two_runs_of_one_day()
    picked = as_was([first.read(), second.read()])
    assert ids(picked) == [first.trades[1].id, first.trades[2].id, second.trades[2].id]
    assert all(one.phase is Phase.LIVE for one in picked)


def test_a_take_hit_after_the_border_is_live_though_its_fill_time_is_not() -> None:
    """Выход по тейку решён касанием: фаза выхода — у касания, а не у вооружения.

    Касание — строка живого бара (12:00), последнее вооружение — строка
    последнего бара пересчёта (11:55), сделка помечена моментом внутри бара
    (11:55). Мутация: фаза выхода по заявке выхода, а не по касанию, —
    сделка уходит в пересчёт и пропадает из «как было».
    """
    rows = Rows(paper_run(3, at(DAY, 11, 50), live_from=at(DAY, 11, 55)))
    trade = rows.take(at(DAY, 11, 0), at(DAY, 12, 0))
    assert trade.exit_time <= at(DAY, 11, 55) < at(DAY, 12, 0), "вход теста не тот"
    picked = as_was([rows.read()])
    assert ids(picked) == [trade.id]
    story = story_of(trade.record, books([rows.read()])[3])
    assert story.exit is not None and story.exit.level == 100_500.0
    assert story.exit.at == at(DAY, 12, 0)


def test_an_entry_decided_at_the_border_is_marked_as_from_the_recount() -> None:
    """Вход, решённый пересчётом (и на последнем его баре), — пометка «вход из пересчёта» (В2).

    Мутация: фаза сделки только по фазе выхода — вход из пересчёта
    объявлен живым, пометки нет.
    """
    rows = Rows(paper_run(4, at(DAY, 12), live_from=at(DAY, 11, 55)))
    at_border = rows.market(at(DAY, 11, 55), at(DAY, 12, 30))
    before = rows.market(at(DAY, 11, 0), at(DAY, 12, 40))
    live = rows.market(at(DAY, 12, 0), at(DAY, 12, 50))
    phases = {one.trade.id: one.phase for one in as_was([rows.read()])}
    assert phases == {
        at_border.id: Phase.ENTRY_RECOUNTED,
        before.id: Phase.ENTRY_RECOUNTED,
        live.id: Phase.LIVE,
    }
    report = paper_report.report(SYMBOL, [rows.read()], books([rows.read()]))
    words = story_lines(report.as_was.stories[0])
    assert any("Вход из пересчёта" in line for line in words), words


def test_an_exit_decided_at_the_border_is_from_the_recount_and_not_shown_as_was() -> None:
    """Выход, решённый на последнем баре пересчёта, — пересчёт: в «как было» сделки нет.

    Мутация: «как было» берёт сделку, у которой живое хоть одно решение
    (вход или выход), — сделка пересчёта показана как прожитая.
    """
    rows = Rows(paper_run(4, at(DAY, 12), live_from=at(DAY, 11, 55)))
    at_border = rows.market(at(DAY, 11, 0), at(DAY, 11, 55))
    after = rows.market(at(DAY, 11, 5), at(DAY, 12, 0))
    assert at_border.exit_time == at(DAY, 11, 55), "вход теста не тот"
    assert ids(as_was([rows.read()])) == [after.id], "выход на границе показан как прожитый"


def test_an_entry_decided_at_the_border_and_filled_overnight_is_from_the_recount() -> None:
    """Вход решён вечером пересчётом, исполнен утром — всё равно «вход из пересчёта».

    Время сделки — открытие утреннего бара, на часы позже решения. Мутация:
    фаза входа по строке, ближайшей ко времени сделки (утренняя заявка
    выхода — живая), а не по заявке входа, — вход объявлен живым и теряет
    пометку (ответ владельца В2).
    """
    eve = DAY - timedelta(days=1)
    rows = Rows(paper_run(4, at(eve, 23, 55), live_from=at(eve, 23, 50)))
    trade = rows.overnight(at(eve, 23, 50), at(DAY, 9, 0), at(DAY, 10, 0))
    assert trade.entry_time > at(eve, 23, 50), "вход теста не тот: исполнение не после границы"
    (picked,) = as_was([rows.read()])
    assert picked.trade.id == trade.id
    assert picked.phase is Phase.ENTRY_RECOUNTED


def test_the_as_was_table_marks_an_entry_from_the_recount_in_words(qapp) -> None:
    """В таблице «как было» сделка со входом из пересчёта помечена «вход из пересчёта».

    Пометка в таблице — то, что видно без разбора (ответ владельца В2).
    Мутация: подпись фазы в таблице совпадает с подписью живой сделки.
    """
    rows = Rows(paper_run(4, at(DAY, 12), live_from=at(DAY, 11, 55)))
    rows.market(at(DAY, 11, 0), at(DAY, 12, 30))
    rows.market(at(DAY, 12, 0), at(DAY, 12, 50))
    report = paper_report.report(SYMBOL, [rows.read()], books([rows.read()]))
    made = dialog_with(report, ReportSource.AS_WAS)
    try:
        assert made.table.rowCount() == 2
        marks = [cell.text() for n in range(2) if (cell := made.table.item(n, 10)) is not None]
        assert len(marks) == 2, marks
        assert "вход из пересчёта" in marks[0], marks
        assert "вход из пересчёта" not in marks[1], marks
    finally:
        made.deleteLater()


# ------------------------------------------------------------------ разбор


def test_the_story_names_the_settings_of_its_run() -> None:
    """Разбор сделки называет снимок настроек своего прогона, а не «снимок пуст».

    Снимок — у строки прогона в базе; два прогона одного дня различаются
    именно им. Мутация: разбор собирается без снимка — окно пишет
    «снимок настроек в базе пуст» при лежащем в базе снимке.
    """
    rows = Rows(paper_run(3, at(DAY, 12), live_from=at(DAY, 11, 55)))
    trade = rows.market(at(DAY, 12, 0), at(DAY, 12, 30))
    words = story_lines(story_of(trade.record, books([rows.read()])[3]))
    assert "      Период средней: 13" in words, words
    assert not any("снимок настроек в базе пуст" in line for line in words), words


def test_the_exit_story_tells_the_level_that_stood_last_not_the_first_one() -> None:
    """У выхода по скользящему уровню разбор называет последнее вооружение до выхода.

    Вооружение повторяется каждый бар под одним именем, и уровень едет
    за ценой. Мутация: заявкой выхода берётся первая строка с этим именем —
    разбор называет уровень, стоявший при входе, а не задетый.
    """
    rows = Rows(paper_run(3, at(DAY, 11, 0), live_from=at(DAY, 10, 55)))
    trade = rows.trailing(at(DAY, 11, 0), (100_500.0, 100_520.0, 100_560.0))
    story = story_of(trade.record, books([rows.read()])[3])
    assert story.exit is not None
    assert story.exit.reason == "Скользящий уровень лонга сторожится на 100560", story.exit
    assert story.exit.level == 100_560.0


# ------------------------------------------------------------------ «пересчёт с утра»


def test_the_recount_takes_the_last_run_that_went_on_each_day() -> None:
    """За день — сделки последнего прогона, шедшего в этот день; чужие дни он не берёт.

    * день D: шли прогоны 1 и 2 — берётся 2 вместе с его пересчётом утра;
    * накануне D прогоны не шли — пересчёт истории прогона 2 этот день не берёт;
    * D+1: два прогона стартовали в одну минуту — берётся больший номер;
    * D+3: прогон, закрытый аварийно накануне в 22:00, шёл до последней
      своей строки в 02:00 — день его.

    Мутации: «последний» по номеру без времени старта, конец аварийного
    прогона по старту, пересчёт берёт дни до старта прогона.
    """
    first, second = two_runs_of_one_day()
    early = Rows(paper_run(5, at(DAY + timedelta(days=1), 9), live_from=at(DAY, 23),
                           finished=at(DAY + timedelta(days=1), 18)))
    early.market(at(DAY + timedelta(days=1), 10), at(DAY + timedelta(days=1), 11))
    twin = Rows(paper_run(6, at(DAY + timedelta(days=1), 9), live_from=at(DAY, 23),
                          finished=at(DAY + timedelta(days=1), 18)))
    twin_trade = twin.market(at(DAY + timedelta(days=1), 10, 5), at(DAY + timedelta(days=1), 11, 5))
    twin.market(at(DAY, 10), at(DAY, 10, 30))  # пересчёт дня D — день прогона 2
    crashed = Rows(paper_run(7, at(DAY + timedelta(days=2), 22), live_from=at(DAY, 23),
                             last_at=at(DAY + timedelta(days=3), 2)))
    night = crashed.market(at(DAY + timedelta(days=3), 1), at(DAY + timedelta(days=3), 1, 30))
    # Старший номер стартовал раньше всех: «последний» — по старту, не по номеру.
    oldest = Rows(paper_run(8, at(DAY, 8), finished=at(DAY, 8, 30), live_from=at(DAY, 7)))
    oldest.market(at(DAY, 9, 40), at(DAY, 10, 10))

    runs = [first.read(), second.read(), early.read(), twin.read(), crashed.read(), oldest.read()]
    picked, days = recount(runs)
    assert ids(picked) == [*ids_of(second.trades), twin_trade.id, night.id]
    assert {day: run.session.id for day, run in days.items()} == {
        DAY: 2,
        DAY + timedelta(days=1): 6,
        DAY + timedelta(days=2): 7,
        DAY + timedelta(days=3): 7,
    }


def ids_of(trades: Iterable[StoredTrade]) -> list[int]:
    return [trade.id for trade in trades]


def test_a_closed_run_went_on_until_its_close_even_without_rows_that_day() -> None:
    """Закрытый прогон шёл до времени закрытия, хотя строк в последний день у него нет.

    Прогон закрыли в 00:30 МСК — баров ночью нет, последняя строка
    вчерашняя. День закрытия всё равно его: прогон шёл в этот день.
    Мутация: конец закрытого прогона по последней строке — день закрытия
    в «пересчёте с утра» выглядит днём без прогона.
    """
    run = paper_run(10, at(DAY, 18), finished=at(DAY + timedelta(days=1), 0, 30),
                    live_from=at(DAY, 17, 55), last_at=at(DAY, 23, 45))
    assert paper_report.run_days(run) == (DAY, DAY + timedelta(days=1))


def test_a_run_started_at_00_30_msk_is_a_run_of_its_own_day(monkeypatch) -> None:
    """Дни — по МСК, а не по UTC и не по часам машины в Новосибирске (+7).

    Прогон 2 начат в 00:30 МСК дня D — в UTC это ещё D−1. Прогон 3 начат
    в 21:30 МСК дня D−1 — в Новосибирске это уже D. День D−1 принадлежит
    прогону 3, а не прогону 2; день D — прогону 2. Мутации: день по UTC,
    день по поясу машины — оба отдают день не тому прогону.
    """
    monkeypatch.setenv("TZ", "Asia/Novosibirsk")
    clock.tzset()
    try:
        eve = DAY - timedelta(days=1)
        evening = Rows(paper_run(3, at(eve, 21, 30).astimezone(UTC), live_from=at(eve, 21),
                                 finished=at(eve, 23, 50).astimezone(UTC)))
        kept = evening.market(at(eve, 22), at(eve, 22, 30))
        night = Rows(paper_run(2, at(DAY, 0, 30).astimezone(UTC), live_from=at(eve, 23, 50),
                               finished=at(DAY, 9).astimezone(UTC)))
        night.market(at(eve, 22, 5), at(eve, 22, 35))  # пересчёт вечера D−1
        picked, days = recount([night.read(), evening.read()])
        assert ids(picked) == [kept.id]
        assert {day: run.session.id for day, run in days.items()} == {eve: 3, DAY: 2}
    finally:
        monkeypatch.undo()
        clock.tzset()


# ------------------------------------------------------------------ окно


def dialog_with(report: PaperReport, source: ReportSource, today: date = DAY) -> ReportsDialog:
    made = ReportsDialog(None, today=lambda: today, ask=lambda: made.set_paper(report))
    made.pick_source(source)
    made.pick_period("Всё")
    return made


def test_runs_that_did_not_record_are_named_not_counted_as_zero(qapp) -> None:
    """Прогон без отметки — словами «сделки не записывались», а не «0 сделок» и не пустотой.

    Мутация: прогон без отметки считается записавшим — окно говорит
    «вживую не закрыто ни одной», и прогоны старой сборки выглядят
    прогонами без сделок.
    """
    silent = RunRows(paper_run(9, at(DAY, 9), finished=at(DAY, 18), marked=False))
    alone = paper_report.report(SYMBOL, [silent], {})
    made = dialog_with(alone, ReportSource.AS_WAS)
    try:
        said = made.empty_label.text()
        assert not made.empty_label.isHidden() and made.splitter.isHidden()
        assert "1 прогон симуляции" in said and "не записывал сделки" in said, said
        first, second = two_runs_of_one_day()
        runs = [silent, first.read(), second.read()]
        mixed = paper_report.report(SYMBOL, runs, books(runs))
        made.set_paper(mixed)
        assert made.table.rowCount() == 3
        assert "1 прогон симуляции за период: сделки не записывались" in made.scope_label.text()
    finally:
        made.deleteLater()


def test_a_day_without_a_run_is_said_in_words_in_the_recount(qapp) -> None:
    """«Пересчёт с утра»: день периода без прогона назван — «прогона не было», а не пропущен.

    Мутация: дни без прогона не называются — неделя с одним прогоном
    выглядит неделей, где робот считал каждый день.
    """
    first, second = two_runs_of_one_day()
    runs = [first.read(), second.read()]
    report = paper_report.report(SYMBOL, runs, books(runs))
    made = dialog_with(report, ReportSource.RECOUNT, today=DAY + timedelta(days=1))
    try:
        made.since_edit.setDate(made.since_edit.date().addDays(-1))
        made.until_edit.setDate(made.until_edit.date().addDays(1))
        scope = made.scope_label.text()
        assert "Прогона симуляции не было: 06.10, 08.10." in scope, scope
        assert made.table.rowCount() == 3
        marks = {cell.text() for n in range(3) if (cell := made.table.item(n, 10)) is not None}
        assert marks == {"№2 · пересчёт", "№2 · вживую"}
    finally:
        made.deleteLater()


def test_a_day_whose_last_run_did_not_record_is_named_in_the_recount(qapp) -> None:
    """«Пересчёт с утра»: день, чей последний прогон сделки не писал, назван — а не «сделок нет».

    День D: утром шёл записывающий прогон, вечером — прогон старой сборки.
    День его, сделок дня нет, и окно говорит почему. Мутация: дни прогонов
    без записи не называются — окно пишет «ни одна сделка не закрылась».
    """
    first, _second = two_runs_of_one_day()
    old = RunRows(paper_run(9, at(DAY, 18), finished=at(DAY, 18, 30), marked=False))
    runs = [first.read(), old]
    report = paper_report.report(SYMBOL, runs, books(runs))
    made = dialog_with(report, ReportSource.RECOUNT)
    try:
        assert made.table.rowCount() == 0
        assert "не писала" in made.empty_label.text(), made.empty_label.text()
        assert "Последний прогон дня сделки не записывал — сделок этих дней нет: 07.10." in (
            made.scope_label.text()
        ), made.scope_label.text()
    finally:
        made.deleteLater()


# ------------------------------------------------------------------ живой ход и база


class Keeping(MarketWorker):
    """Поток данных, который запоминает пачки строк хода — память хода как она есть."""

    def __init__(self, path: pathlib.Path) -> None:
        super().__init__(path)
        self.kept: list[PaperRows] = []

    async def write_paper_rows(self, session_id, rows, *, on_written=None) -> PaperWritten:
        self.kept.append(rows)
        return await super().write_paper_rows(session_id, rows, on_written=on_written)


#: Ряд живого хода: вторник с 07:00 и ночь на среду. Во вторник две сделки
#: (после тейка робот до конца дня стоит), ночью ещё две.
RIDE = bars(300, start=datetime(2026, 6, 16, 7, 0, tzinfo=MSK))
#: Первый проход — до 10:20 вторника: первая сделка в нём закрыта (пересчёт),
#: вторая открыта и закроется вживую (вход из пересчёта), ночные — живые.
#: Короткий прогрев `tests/test_app_paper_log.py` пересчёта без сделок не дал бы.
RIDE_WARM = 40


def live_ride(loop, path: pathlib.Path) -> tuple[list[TradeRecord], int, list[PaperRows]]:
    """Живой ход: первый проход одним отрезком, дальше по бару; программа «падает» без закрытия.

    Отдаёт сделки хода, сколько из них дал первый проход (пересчёт),
    и пачки строк, ушедшие в базу, — память хода.
    """
    worker = Keeping(path)

    async def go():
        await worker.open()
        try:
            watcher = observer(worker, Said())
            await watcher.start()
            first = await watcher.feed(RIDE[:RIDE_WARM])
            run = first
            for count in range(RIDE_WARM + 1, len(RIDE) + 1):
                run = await watcher.feed(RIDE[:count])
            return run, len(first.deals)
        finally:
            await worker.close()

    run, recounted = loop.run_until_complete(go())
    return [convert.trade_record(deal, SYMBOL) for deal in run.deals], recounted, worker.kept


def read_report(
    loop, path: pathlib.Path, worker: MarketWorker | None = None, instrument: str = SYMBOL
) -> PaperReport:
    """Новый порт на той же базе — как после перезапуска — и просьба «Отчётов»."""
    worker = worker or MarketWorker(path)

    async def go() -> list[PaperReport]:
        await worker.open()
        port = HistoryPort(worker, values=Settings(), days=0, sanitize=redact)
        seen: list[PaperReport] = []
        port.paper_report_ready.connect(seen.append)
        try:
            port.request_paper_report(PaperReportRequest(instrument))
            await port.wait()
        finally:
            await port.aclose()
            await worker.close()
        return seen

    seen: list[PaperReport] = loop.run_until_complete(go())
    assert len(seen) == 1, f"ответов порта {len(seen)}, а просьба одна"
    return seen[0]


def key(row) -> tuple[object, ...]:
    return (row.entry_time, row.exit_time, row.entry_price, row.exit_price, row.volume)


def test_a_paper_trade_is_read_from_the_base_after_a_restart_and_shown(
    loop, qapp, tmp_path: pathlib.Path
) -> None:
    """Сделка живого хода переживает перезапуск: новый порт читает её из базы, окно показывает.

    Ход «упал» без закрытия; новый порт на той же базе. Ждём в «как было»
    ровно сделки, закрытые после первого прохода, — счёт по проходам хода,
    а не по правилу отбора. Мутации: порт не читает базу либо окно
    не показывает присланное — таблица пуста.
    """
    path = tmp_path / "restart.sqlite3"
    trades, recounted, _kept = live_ride(loop, path)
    live = trades[recounted:]
    assert live and recounted, "ход без живых сделок или без пересчёта — сверять нечего"
    report = read_report(loop, path)
    assert not report.trouble, report.trouble
    assert [key(row) for row in report.as_was.rows] == [key(row) for row in live]
    made = dialog_with(report, ReportSource.AS_WAS, today=RIDE[0].time.date())
    try:
        assert made.table.rowCount() == len(live)
        made.table.selectRow(0)
        text = made.explanation.toPlainText()
        assert "Прогон симуляции №" in text and "Почему вошли:" in text, text
    finally:
        made.deleteLater()


def test_the_story_read_from_the_base_is_the_story_kept_in_memory(
    loop, tmp_path: pathlib.Path
) -> None:
    """Разбор сделки из базы — строка в строку разбор из памяти хода. Функция одна.

    Память — пачки, отданные ходом на запись (`app/paper_log.py`); база —
    те же строки, прочитанные заново. Мутации: заявки или решения
    читаются не в порядке записи, поле заявки теряется при чтении —
    разбор хоть одной сделки расходится.
    """
    path = tmp_path / "same.sqlite3"
    trades, _recounted, kept = live_ride(loop, path)
    with CandleStore(path) as store:
        (run,) = store.paper_runs([SYMBOL])
        number = run.session.id
        base = RunBook.of(
            run.session,
            [one.record for one in store.orders(session_id=number, limit=100_000)],
            [one.record for one in store.decisions(session_id=number, limit=100_000)],
        )
        stored = [one.record for one in store.trades(session_id=number, limit=100_000)]
    memory = RunBook.of(
        run.session,
        [order for rows in kept for order in rows.orders],
        [row for rows in kept for row in rows.decisions],
    )
    assert stored == trades, "сделки в базе не те, что у хода"
    for trade in trades:
        from_memory = story_lines(story_of(trade, memory))
        assert story_lines(story_of(trade, base)) == from_memory
    stories = [story_of(trade, base) for trade in trades]
    assert all(one.entry is not None and one.exit is not None for one in stories), (
        "проверка вакуумна: у сделки не нашлось заявки"
    )
    assert any(one.exit is not None and one.exit.level is not None for one in stories)
    assert all(one.entry_rows and one.exit_rows for one in stories)
    assert {one.phase for one in stories} == {
        Phase.LIVE, Phase.ENTRY_RECOUNTED, Phase.RECOUNTED
    }, "не все три случая хода без догона в ряду"


def memory_book(path: pathlib.Path, kept: list[PaperRows]) -> RunBook:
    """Книга прогона из памяти хода — пачек, отданных им на запись."""
    with CandleStore(path) as store:
        (run,) = store.paper_runs([SYMBOL])
    return RunBook.of(
        run.session,
        [order for rows in kept for order in rows.orders],
        [row for rows in kept for row in rows.decisions],
    )


def test_the_story_the_window_gets_from_the_base_is_the_story_kept_in_memory(
    loop, tmp_path: pathlib.Path
) -> None:
    """Разбор, который порт после перезапуска отдаёт окну, — строка в строку разбор из памяти.

    Сверяется путь окна целиком: порт читает прогоны, сделки, заявки
    и журнал решений и сам собирает книгу прогона. Мутации: порт собирает
    книгу без журнала решений или без заявок — разбор говорит «в базе нет»
    там, где ход всё записал.
    """
    path = tmp_path / "window.sqlite3"
    trades, recounted, kept = live_ride(loop, path)
    report = read_report(loop, path)
    memory = memory_book(path, kept)
    live = trades[recounted:]
    assert len(report.as_was.stories) == len(live) > 0, report.as_was.rows
    for trade, story in zip(live, report.as_was.stories, strict=True):
        assert story_lines(story) == story_lines(story_of(trade, memory))


def test_the_entry_reason_in_the_story_is_the_one_in_the_journal_of_the_entry_bar(
    loop, tmp_path: pathlib.Path
) -> None:
    """«Почему вошли» — та же причина, что в журнале решений бара входа.

    Причина приходит в базу двумя путями: заявкой входа и строкой журнала
    решений. Сверка с журналом ловит то, чего сверка «база против памяти»
    не видит: заявку, потерявшую причину ещё до записи, — тогда пусто
    и там и там. Мутация: перекладка заявки движка в строку базы теряет
    причину — разбор пишет «причина в базе не записана».
    """
    path = tmp_path / "reason.sqlite3"
    live_ride(loop, path)
    stories = read_report(loop, path).as_was.stories
    assert stories, "в «как было» ни одной сделки — сверять нечего"
    for story in stories:
        assert story.entry is not None and story.entry.reason, story
        journal = {row.reason for row in story.entry_rows}
        assert story.entry.reason in journal, (story.entry.reason, journal)


class Refusing(MarketWorker):
    """Поток данных, у которого чтение прогонов симуляции отказывает."""

    async def paper_runs(self, symbols):
        raise sqlite3.OperationalError("database is locked")


def test_a_refused_read_is_said_in_the_window_and_not_shown_as_no_trades(
    loop, qapp, tmp_path: pathlib.Path
) -> None:
    """Отказ чтения базы — фразой в окне вместо таблицы, а не пустым «прогонов нет».

    Мутация молчания: порт глотает отказ и шлёт пустой отчёт — окно
    говорит «прогонов симуляции в базе нет», то есть неправду.
    """
    path = tmp_path / "locked.sqlite3"
    report = read_report(loop, path, Refusing(path))
    assert "прочитать не удалось" in report.trouble and "database is locked" in report.trouble
    made = dialog_with(report, ReportSource.RECOUNT)
    try:
        assert made.empty_label.text() == report.trouble
        assert made.splitter.isHidden() and made.card.isHidden()
    finally:
        made.deleteLater()


def test_the_report_takes_every_contract_of_the_instrument_and_no_other(
    loop, tmp_path: pathlib.Path
) -> None:
    """Прогоны — все контракты актива на экране (В3), чужой актив и прогоны по истории — нет.

    MXH6 цепочкой вокруг MXZ6 не находится — только по таблице контрактов.
    Мутации: отбор по одному коду на экране; таблица контрактов не читается.
    """
    path = tmp_path / "codes.sqlite3"
    with CandleStore(path) as store:
        store.put_contracts([ContractRow("MXH6", last_trade_day=date(2026, 3, 19))])
        for symbol in ("MXH6", "MXU6", "MXZ6", "RIZ6"):
            session = store.open_journal_session(
                SessionRecord(RunOrigin.PAPER, symbol=symbol), now=at(DAY, 9)
            )
            store.write_paper_rows(session.id, PaperRows(mark=RunRowsMark(None, None)))
        store.open_journal_session(SessionRecord(RunOrigin.BACKTEST, symbol="MXZ6"))
    report = read_report(loop, path, instrument="MXZ6")
    assert report.codes == ("MXH6", "MXU6", "MXZ6"), report.codes
    assert len(report.runs) == 3 and all(run.recorded for run in report.runs)


# ------------------------------------------------------------------ проводка в окне


class AskingPort(RecordingPort):
    """Порт, который запоминает просьбу и не отвечает, пока тест не ответит сам."""

    def request_paper_report(self, request: PaperReportRequest) -> None:
        self.calls.append(("request_paper_report", request))


def test_the_reports_button_reaches_the_base_and_the_answer_reaches_the_table(qapp) -> None:
    """От кнопки «Отчёты…» через переключатель — просьба порту, ответ порта — в таблицу.

    Пока ответа нет, окно говорит «читаю», а не показывает пустую таблицу.
    Мутации: главное окно не передаёт `ask` или не подключает
    `paper_report_ready` — просьбы нет или таблица пуста.
    """
    port = AskingPort()
    window = MainWindow(port=port, sanitize=redact)
    try:
        start = at(DAY, 10)
        candles = tuple(
            Candle(opens_at=start + BAR * n, open=1, high=2, low=0.5, close=1.5)
            for n in range(3)
        )
        window.show_chart(ChartData(instrument="MXZ6", candles=candles))
        window.journals.reports_button.click()
        dialog = window.reports_dialog
        assert dialog is not None and dialog.source() is ReportSource.SHOWN
        dialog.source_buttons[ReportSource.AS_WAS].click()
        assert port.calls[-1] == ("request_paper_report", PaperReportRequest("MXZ6"))
        assert dialog.empty_label.text() == PAPER_WAIT and dialog.splitter.isHidden()
        first, second = two_runs_of_one_day()
        runs = [first.read(), second.read()]
        port.paper_report_ready.emit(paper_report.report("MXZ6", runs, books(runs)))
        dialog.pick_period("Всё")
        assert dialog.table.rowCount() == 3
    finally:
        for timer in window.findChildren(QTimer):
            timer.stop()
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_a_window_without_the_trading_part_says_so_instead_of_waiting(qapp) -> None:
    """Порт без торговой части отвечает отказом словами — окно не ждёт вечно."""
    window = MainWindow(port=RecordingPort(), sanitize=redact)
    try:
        window.journals.reports_button.click()
        dialog = window.reports_dialog
        assert dialog is not None
        dialog.pick_source(ReportSource.RECOUNT)
        assert "окно открыто без торговой части" in dialog.empty_label.text()
    finally:
        for timer in window.findChildren(QTimer):
            timer.stop()
        window.close()
        window.deleteLater()
        qapp.processEvents()
