"""Фаза решения у строк живого хода: пересчёт, догон и живое различаются по ленте.

Каждый тест стережёт **одно** поведение, и оно названо первой строкой
докстринга. Предмет — пометка фазы в `app/observe.py` и отбор «как было»
в `app/paper_report.py`, прочитанный из базы заново.

Вход подставной (правило 14): ряд синтетический (`tests.test_app_observe.bars`),
база во временной папке; строки отбора — руками, по образцу
`tests/test_app_paper_report.py`. Ожидаемое выводится **из устройства
ленты** — какие сделки и строки появились после какого прохода, — а не тем
же правилом отбора: сверка правила с самим собой зеленела бы вместе с ним.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import itertools
import pathlib
import re
from collections.abc import Callable, Coroutine, Sequence
from datetime import datetime, timedelta
from typing import cast

import pytest

from app import convert, paper_report
from app.observe import LiveObserver, RunRecording
from app.paper_report import RunRows, as_was, caught_up
from app.runs import RunLog
from backtest import BREATHE_EVERY, Costs, Deal
from engine import ExitReason
from market import (
    MSK,
    CandleStore,
    DecisionPhase,
    MarketWorker,
    RunOrigin,
    SessionRecord,
    StoredTrade,
)
from market import Candle as MarketCandle
from strategies import MaReverseAlways, MaReverseAlwaysSettings
from tests.test_app_observe import DEADLINE, Said, bars, settings
from tests.test_app_paper_log import SYMBOL
from tests.test_app_paper_report import DAY, Rows, at, books, dialog_with, paper_run
from ui.models import DecisionLevel
from ui.reports_dialog import ReportsDialog
from ui.trade_story import Phase, ReportSource, story_lines

RECOUNT, CATCH_UP, LIVE = DecisionPhase.RECOUNT, DecisionPhase.CATCH_UP, DecisionPhase.LIVE

#: Пятница, 09.10.2026: программу закрыли в 18:00.
FRIDAY = datetime(2026, 10, 9, 13, 0, tzinfo=MSK)
#: Понедельник, 12.10.2026: открыли в 12:00 и нажали «Подключиться».
MONDAY = datetime(2026, 10, 12, 7, 0, tzinfo=MSK)
#: Пятница 13:00–17:55, понедельник 07:00–13:55.
WEEKEND = [*bars(60, start=FRIDAY), *bars(84, start=MONDAY)]
#: Первый проход — пятница целиком (пересчёт при сборке хода).
FIRST = 60
#: Второй проход — догрузка за выходные одной пачкой: понедельник до 12:00.
CAUGHT_UP = 120

LEVELS = {ExitReason.TAKE_PROFIT, ExitReason.TRAILING_TAKE}


@pytest.fixture(scope="module")
def loop():
    """Свой цикл asyncio на файл — по той же причине, что в `test_app_observe`."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def drive(loop):
    def run[T](work: Callable[[], Coroutine[object, object, T]]) -> T:
        return cast("T", loop.run_until_complete(asyncio.wait_for(work(), DEADLINE)))

    return run


def observer(worker: MarketWorker) -> LiveObserver:
    """Живой ход с записью в базу. Стоп после тейка снят.

    Со стопом после первого тейка дня робот стоит до вечера, и у живой
    части ряда не было бы ни одной сделки — сверять было бы нечего.
    """
    said = Said()
    runs = RunLog(
        worker,
        warn=lambda event, reason: said(event, reason, DecisionLevel.WARNING),
        note=lambda event, reason: said(event, reason, DecisionLevel.INFO),
    )
    return LiveObserver(
        strategy=MaReverseAlways(MaReverseAlwaysSettings()),
        settings=dataclasses.replace(settings(), stop_after_take_profit=False),
        say=said,
        costs=Costs(commission_per_side=14.0),
        recording=RunRecording(
            around=lambda: runs.around(SessionRecord(RunOrigin.PAPER, symbol=SYMBOL)),
            store=worker,
            symbol=SYMBOL,
        ),
    )


def feed_in_passes(
    drive, path: pathlib.Path, series: Sequence[MarketCandle], passes: Sequence[int]
) -> list[tuple[Deal, ...]]:
    """Живой ход: проход за проходом, отрезок ряда целиком, как у порта.

    `passes` — сколько баров ряда видит каждый проход. Ответ — сделки хода
    после каждого прохода. Программа «падает» без закрытия хода: всё нужное
    уже в базе, проход пишет свои строки сам.
    """

    async def go() -> list[tuple[Deal, ...]]:
        async with MarketWorker(path) as worker:
            watcher = observer(worker)
            await watcher.start()
            seen: list[tuple[Deal, ...]] = []
            for count in passes:
                run = await watcher.feed(series[:count])
                seen.append(run.deals)
            return seen

    seen: list[tuple[Deal, ...]] = drive(go)
    return seen


def fresh(seen: Sequence[tuple[Deal, ...]], index: int) -> tuple[Deal, ...]:
    """Сделки, впервые появившиеся после прохода `index`."""
    before = len(seen[index - 1]) if index else 0
    return seen[index][before:]


def read_rows(path: pathlib.Path) -> RunRows:
    """Прогон, его сделки и заявки — прочитаны заново, как их читает порт."""
    with CandleStore(path) as store:
        (run,) = store.paper_runs([SYMBOL])
        number = run.session.id
        trades = store.trades(session_id=number, limit=100_000).rows
        orders = store.orders(session_id=number, limit=100_000).rows
    return RunRows(run, tuple(trades), tuple(order.record for order in orders))


def key(trade) -> tuple[object, ...]:
    return (trade.entry_time, trade.exit_time, trade.entry_price, trade.exit_price)


def single_bar_deals(seen: Sequence[tuple[Deal, ...]], passes: Sequence[int]) -> list[Deal]:
    """Сделки, появившиеся после проходов в один новый бар, — прожитые вживую."""
    found: list[Deal] = []
    for index in range(1, len(passes)):
        if passes[index] - passes[index - 1] == 1:
            found.extend(fresh(seen, index))
    return found


def test_a_weekend_caught_up_in_one_pass_is_not_shown_as_was(drive, tmp_path: pathlib.Path) -> None:
    """Пятница → понедельник: сделки утра, разобранного одной пачкой догрузки, не «как было».

    Первый проход — пятница до 18:00, второй — догрузка выходных одной пачкой
    до понедельника 12:00, дальше по бару. «Как было» — ровно сделки,
    появившиеся после проходов в один бар. Мутация: граница живого — конец
    первого прохода (как до правки), — утро понедельника объявлено живым.
    """
    path = tmp_path / "weekend.sqlite3"
    passes = [FIRST, CAUGHT_UP, *range(CAUGHT_UP + 1, len(WEEKEND) + 1)]
    seen = feed_in_passes(drive, path, WEEKEND, passes)
    caught_up = fresh(seen, 1)
    last_of_batch = WEEKEND[CAUGHT_UP - 1].time
    assert caught_up, "пачка догрузки без сделок — сверять нечего"
    assert not any(
        deal.exit_reason in LEVELS and deal.exit_time >= last_of_batch for deal in caught_up
    ), "вход теста не тот: тейк на последнем баре пачки — решение вживую"
    live = single_bar_deals(seen, passes)
    assert live, "живая часть без сделок — сверять нечего"
    shown = [key(one.trade) for one in as_was([read_rows(path)])]
    assert shown == [key(convert.trade_record(deal, SYMBOL)) for deal in live], (
        f"«как было» показывает сделки догона: {len(shown)} против {len(live)} живых"
    )


def test_an_empty_first_pass_does_not_make_the_recount_live(drive, tmp_path: pathlib.Path) -> None:
    """Первый проход без единого доверенного бара не объявляет пересчёт живым.

    Порт отдал отрезок, где все бары неполные (`unsettled`), — ленте брать
    нечего. Следующий проход приносит весь ряд разом: это пересчёт, и
    «как было» — только сделки проходов в один бар. Мутация: отметка
    первого прохода `(None, None)` читается как «всё вживую».
    """
    path = tmp_path / "empty-first.sqlite3"
    series = bars(140, start=MONDAY)
    unsettled = [dataclasses.replace(candle, unsettled=True) for candle in series]
    first = 60

    async def go() -> list[tuple[Deal, ...]]:
        async with MarketWorker(path) as worker:
            watcher = observer(worker)
            await watcher.start()
            await watcher.feed(unsettled[:first])
            seen: list[tuple[Deal, ...]] = []
            for count in range(first, len(series) + 1):
                run = await watcher.feed(series[:count])
                seen.append(run.deals)
            return seen

    seen = drive(go)
    passes = list(range(first, len(series) + 1))
    assert seen[0], "пересчёт без сделок — сверять нечего"
    live = single_bar_deals(seen, passes)
    assert live, "живая часть без сделок — сверять нечего"
    shown = [key(one.trade) for one in as_was([read_rows(path)])]
    assert shown == [key(convert.trade_record(deal, SYMBOL)) for deal in live], (
        f"«как было» показывает пересчёт: {len(shown)} против {len(live)} живых"
    )


# ------------------------------------------------------------------ фаза строк в базе


def phases_by_pass(
    drive, path: pathlib.Path, feeds: Sequence[Sequence[MarketCandle]]
) -> list[list[DecisionPhase]]:
    """Фазы строк заявок, прочитанных из базы, — по проходам: что легло после каждого.

    `feeds` — отрезок ряда каждого прохода, целиком, как у порта. Проход
    пишет свои строки в конце себя (`PaperLog.flush`), поэтому прирост числа
    строк в базе после прохода — ровно строки этого прохода.
    """

    async def go() -> tuple[list[int], list[DecisionPhase]]:
        async with MarketWorker(path) as worker:
            watcher = observer(worker)
            await watcher.start()
            counts: list[int] = []
            for candles in feeds:
                await watcher.feed(candles)
                (run,) = await worker.paper_runs([SYMBOL])
                counts.append(len(await worker.orders(session_id=run.session.id, limit=100_000)))
            stored = await worker.orders(session_id=run.session.id, limit=100_000)
            return counts, [one.phase for one in stored]

    counts, found = cast("tuple[list[int], list[DecisionPhase]]", drive(go))
    edges = [0, *counts]
    return [found[start:end] for start, end in itertools.pairwise(edges)]


def test_bars_fed_one_by_one_after_the_recount_are_live(drive, tmp_path: pathlib.Path) -> None:
    """Обычный живой ход: строки первого прохода — пересчёт, строки проходов в один бар — живые.

    Мутации: фаза не доезжает до базы (всё читается умолчанием — пересчётом),
    живой бар объявлен догоном, пересчёт кончается не первым проходом.
    """
    series = bars(140, start=MONDAY)
    feeds = [series[:count] for count in range(FIRST, len(series) + 1)]
    by_pass = phases_by_pass(drive, tmp_path / "live.sqlite3", feeds)
    recounted, live = by_pass[0], [phase for rows in by_pass[1:] for phase in rows]
    assert recounted and live, "пересчёт или живая часть без строк — сверять нечего"
    assert set(recounted) == {RECOUNT}, recounted
    assert set(live) == {LIVE}, live


def test_a_catch_up_batch_is_catch_up_but_for_its_last_bar(drive, tmp_path: pathlib.Path) -> None:
    """Пачка догрузки: строки всех её баров, кроме последнего, — догон; строки последнего — живые.

    Сколько строк родил последний бар пачки, говорит второй ход по тому же
    ряду, которому последний бар отдан отдельным проходом: движок один,
    строки те же. Мутации: последний бар пачки — догон (живого после
    выходных не остаётся вовсе), вся пачка живая, вся пачка — пересчёт.
    """
    whole = phases_by_pass(
        drive, tmp_path / "whole.sqlite3", [WEEKEND[:FIRST], WEEKEND[:CAUGHT_UP]]
    )
    split = phases_by_pass(
        drive,
        tmp_path / "split.sqlite3",
        [WEEKEND[:FIRST], WEEKEND[: CAUGHT_UP - 1], WEEKEND[:CAUGHT_UP]],
    )
    before_last, on_last = len(split[1]), len(split[2])
    assert before_last and on_last, "у пачки или у её последнего бара нет строк — сверять нечего"
    assert whole[1] == [CATCH_UP] * before_last + [LIVE] * on_last


def test_the_pass_after_an_empty_first_one_is_the_recount(drive, tmp_path: pathlib.Path) -> None:
    """Пустой первый проход пересчёт не кончает: строки следующего прохода — пересчёт.

    Первый проход — одни неполные бары, ленте брать нечего; следующий
    приносит ряд целиком. Мутация: пересчёт кончается и пустым проходом —
    история читается догоном, а её последний бар живым.
    """
    series = bars(140, start=MONDAY)
    unsettled = [dataclasses.replace(candle, unsettled=True) for candle in series[:FIRST]]
    feeds = [unsettled, *(series[:count] for count in range(FIRST, len(series) + 1))]
    by_pass = phases_by_pass(drive, tmp_path / "empty.sqlite3", feeds)
    live = [phase for rows in by_pass[2:] for phase in rows]
    assert by_pass[0] == [], "пустой проход родил строки — вход не тот"
    assert by_pass[1] and live, "пересчёт или живая часть без строк — сверять нечего"
    assert set(by_pass[1]) == {RECOUNT}, by_pass[1]
    assert set(live) == {LIVE}, live


def test_a_first_pass_cut_short_leaves_no_live_row(drive, tmp_path: pathlib.Path) -> None:
    """«Отменить» посреди первого прохода: всё разобранное — пересчёт, остаток дописан закрытием.

    Первый проход — ровно `BREATHE_EVERY` баров: лента отдаёт управление
    после последнего из них, и проход снимается, разобрав всё, но не дойдя
    до своего конца. Строк в базе до закрытия хода нет — их пишет `aclose`.
    Мутации: пересчёт кончается в начале прохода либо при отмене — бары
    первого прохода читаются догоном и живыми; последний бар ленты живой
    без оглядки на пересчёт.
    """
    path = tmp_path / "cut.sqlite3"
    series = bars(BREATHE_EVERY, start=MONDAY)

    async def go() -> tuple[bool, int]:
        async with MarketWorker(path) as worker:
            watcher = observer(worker)
            await watcher.start()
            task = asyncio.ensure_future(watcher.feed(series))
            while watcher.run.bars < BREATHE_EVERY and not task.done():
                await asyncio.sleep(0)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            taken = watcher.run.bars
            await watcher.aclose()
            return task.cancelled(), taken

    cancelled, taken = cast("tuple[bool, int]", drive(go))
    assert cancelled and taken == BREATHE_EVERY, "проход не снят посреди себя — вход не тот"
    with CandleStore(path) as store:
        (run,) = store.paper_runs([SYMBOL])
        stored = store.orders(session_id=run.session.id, limit=100_000)
    assert run.mark is not None and stored, "закрытие хода не дописало остаток — сверять нечего"
    assert {one.phase for one in stored} == {RECOUNT}
    assert as_was([read_rows(path)]) == []


def test_the_pass_after_a_first_pass_cut_short_is_still_the_recount(
    drive, tmp_path: pathlib.Path
) -> None:
    """«Отменить» посреди первого прохода, ход живёт дальше: недобранный остаток — пересчёт.

    Первый проход несёт больше `BREATHE_EVERY` баров и снимается на передышке:
    часть истории осталась в ленте. Ход не закрыт — порт держит его и зовёт
    следующий проход, где этот остаток разбирается вместе с одним новым
    баром. Это всё ещё история, а не живое: строки обоих проходов —
    пересчёт, живые — только строки проходов в один бар после него.
    Мутация: пересчёт кончается и снятым проходом (`finally` вокруг разбора) —
    остаток истории читается догоном, его последний бар живым.
    """
    path = tmp_path / "cut-then-on.sqlite3"
    cut = BREATHE_EVERY + 50
    series = bars(cut + 20, start=MONDAY)

    async def go() -> tuple[bool, int, int, int, list[DecisionPhase]]:
        async with MarketWorker(path) as worker:
            watcher = observer(worker)
            await watcher.start()
            task = asyncio.ensure_future(watcher.feed(series[:cut]))
            while watcher.run.bars < BREATHE_EVERY and not task.done():
                await asyncio.sleep(0)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            taken, plans_at_cut = watcher.run.bars, len(watcher.run.plans)
            rest = await watcher.feed(series[: cut + 1])
            (paper,) = await worker.paper_runs([SYMBOL])
            history = len(await worker.orders(session_id=paper.session.id, limit=100_000))
            for count in range(cut + 2, len(series) + 1):
                await watcher.feed(series[:count])
            stored = await worker.orders(session_id=paper.session.id, limit=100_000)
            return (
                task.cancelled(), taken, len(rest.plans) - plans_at_cut, history,
                [one.phase for one in stored],
            )

    cancelled, taken, decided_on_rest, history, found = cast(
        "tuple[bool, int, int, int, list[DecisionPhase]]", drive(go)
    )
    assert cancelled and taken == BREATHE_EVERY, "проход не снят посреди себя — вход не тот"
    assert decided_on_rest, "остаток истории без решений — сверять нечего"
    recounted, live = found[:history], found[history:]
    assert live, "живая часть без строк — сверять нечего"
    assert set(recounted) == {RECOUNT}, (
        f"остаток снятого первого прохода не пересчёт: {sorted(set(recounted))}"
    )
    assert set(live) == {LIVE}, live


# ------------------------------------------------------------------ отбор по фазам


def test_every_pair_of_decision_phases_has_a_trade_phase() -> None:
    """Таблица фаз сделки полна: у каждого сочетания фаз входа и выхода есть ответ и подпись.

    Мутация: сочетание выпало из таблицы — разбор сделки с ним падает
    `KeyError` в окне, а не на сборке.
    """
    missing = []
    for pair in itertools.product(DecisionPhase, DecisionPhase):
        try:
            paper_report.phase_of(*pair)
        except KeyError:
            missing.append(pair)
    assert missing == [], f"сочетаний без фазы сделки: {missing}"
    assert all(phase.label and phase.words for phase in Phase)
    homeless = []
    for phase in Phase:
        try:
            paper_report.lot_of(phase)
        except KeyError:
            homeless.append(phase)
    assert homeless == [], f"фаз без места в «как было»: {homeless}"


def _monday_after_a_drop(moment: datetime) -> DecisionPhase:
    """Пересчёт до 09:55, вживую 10:00–10:30, обрыв; догон 10:35–11:55, дальше вживую."""
    if moment <= at(DAY, 9, 55):
        return RECOUNT
    if at(DAY, 10, 35) <= moment < at(DAY, 12):
        return CATCH_UP
    return LIVE


def test_an_entry_on_the_catch_up_closed_live_is_shown_as_was_marked_so(qapp) -> None:
    """Вход на догоне, выход вживую — в «как было», с пометкой «вход на догоне» в таблице и разборе.

    Мутации: «как было» берёт только живой вход и вход из пересчёта —
    сделки нет; сочетание «догон → живое» подписано как вход из пересчёта.
    """
    rows = Rows(paper_run(4, at(DAY, 9)), _monday_after_a_drop)
    trade = rows.market(at(DAY, 11), at(DAY, 12, 30))
    assert [(one.trade.id, one.phase) for one in as_was([rows.read()])] == [
        (trade.id, Phase.ENTRY_CAUGHT_UP)
    ]
    report = paper_report.report(SYMBOL, [rows.read()], books([rows.read()]))
    words = story_lines(report.as_was.stories[0])
    assert any("Вход на догоне" in line for line in words), words
    made = dialog_with(report, ReportSource.AS_WAS)
    try:
        assert made.table.rowCount() == 1
        cell = made.table.item(0, 10)
        assert cell is not None and "вход на догоне" in cell.text(), cell
    finally:
        made.deleteLater()


def _priced(rows: Rows, trade: StoredTrade, gross: float) -> StoredTrade:
    """Та же сделка с другим результатом: итог карточки различает, какие сделки в нём."""
    priced = dataclasses.replace(trade, gross=gross, net=gross - FEE)
    rows.trades[rows.trades.index(trade)] = priced
    return priced


#: Комиссия сделки `Rows`: 14 ₽ на сторону.
FEE = 28.0


def _money(text: str) -> float:
    """Рубли из надписи окна: «−1 234,56 ₽» → −1234.56. Чтение экрана, не модели."""
    body = re.sub(r"\s", "", text).replace("₽", "").replace("−", "-").replace("+", "")
    return float(body.replace(",", "."))


def _card(made: ReportsDialog) -> tuple[float, float, int]:
    """Итог карточки, как его видит человек: чистый, комиссия, число сделок."""
    assert not made.card.isHidden(), "карточки итога нет"
    values = made.card.values
    return (
        _money(values["net"].text()),
        _money(values["commission"].text()),
        int(values["trades"].text()),
    )


def _row_ids(made: ReportsDialog) -> list[str]:
    """Сделки таблицы по имени заявки входа — в её порядке."""
    shown = made.shown_trades()
    assert made.table.rowCount() == len(shown), "в таблице не те строки, что в отборе окна"
    return [row.trade_id for row in shown]


def _caught_up_count(scope: str) -> int:
    """Число из строки о сделках, решённых на догоне. Строки нет — ноль."""
    found = re.search(r"Сделок, решённых на догоне \([^)]*\): (\d+)\.", scope)
    return int(found.group(1)) if found else 0


def _scope_of_a_live_trade_alone(source: ReportSource) -> str:
    """Надпись над таблицей у того же прогона, где одна сделка — живая 12:05 → 12:30.

    Сверочная: сделки, которых надпись касаться не должна, её не меняют —
    какими бы словами ни была написана лишняя строка.
    """
    alone = Rows(paper_run(4, at(DAY, 9)), _monday_after_a_drop)
    alone.market(at(DAY, 12, 5), at(DAY, 12, 30))
    report = paper_report.report(SYMBOL, [alone.read()], books([alone.read()]))
    made = dialog_with(report, source)
    try:
        return made.scope_label.text()
    finally:
        made.deleteLater()


def test_a_trade_opened_live_and_closed_on_the_catch_up_is_a_row_and_in_the_total(qapp) -> None:
    """Вход вживую, выход на догоне: строка «как было» с пометкой «выход на догоне» и в итоге.

    Ожидаемое — по ленте: позиции 10:05 и 10:10 открыты в живые полчаса
    до обрыва и закрыты на догоне, 12:05 — вживую целиком. Итог — сумма
    результатов этих трёх сделок, как они поданы на вход, а не строк окна.
    Мутации: такие сделки не идут в таблицу (прежнее правило); идут, но
    карточка считает итог без них; пометка — «вживую»; разбор не объясняет
    пометку; над таблицей вернулась строка с их числом — любыми словами:
    надпись сверяется с надписью того же прогона, где есть только живая сделка.
    """
    rows = Rows(paper_run(4, at(DAY, 9)), _monday_after_a_drop)
    first = rows.market(at(DAY, 10, 5), at(DAY, 10, 40))
    second = _priced(rows, rows.market(at(DAY, 10, 10), at(DAY, 11)), -50.0)
    live = _priced(rows, rows.market(at(DAY, 12, 5), at(DAY, 12, 30)), 300.0)
    expected = [first, second, live]
    report = paper_report.report(SYMBOL, [rows.read()], books([rows.read()]))
    made = dialog_with(report, ReportSource.AS_WAS)
    try:
        assert _row_ids(made) == [one.entry_order_id for one in expected]
        marks = [cell.text() for n in range(3) if (cell := made.table.item(n, 10)) is not None]
        assert marks == ["№4 · выход на догоне", "№4 · выход на догоне", "№4 · вживую"], marks
        assert _card(made) == pytest.approx(
            (sum(one.net or 0.0 for one in expected), FEE * len(expected), len(expected))
        )
        words = story_lines(made.shown_stories()[0])
        assert any(
            "Выход на догоне" in line and "разбор пропущенных свечей" in line for line in words
        ), words
        scope = made.scope_label.text()
    finally:
        made.deleteLater()
    assert scope == _scope_of_a_live_trade_alone(ReportSource.AS_WAS), (
        "над таблицей сказано о сделках с выходом на догоне, а они уже строки таблицы"
    )


def test_trades_decided_on_the_catch_up_alone_are_named_by_number_not_shown(qapp) -> None:
    """Ни одного живого решения, выход на догоне: числом над таблицей, не в таблице и не в итоге.

    По ленте: 10:45 → 11:30 — целиком на догоне, 09:50 → 10:50 — вход
    пересчётом, выход на догоне; 12:05 → 12:30 — вживую. Мутации: строки
    с числом нет (правило 13 — сторож молчания); такие сделки в таблице
    или в итоге; сделка «пересчёт → догон» не в числе.
    """
    rows = Rows(paper_run(4, at(DAY, 9)), _monday_after_a_drop)
    caught = [
        _priced(rows, rows.market(at(DAY, 10, 45), at(DAY, 11, 30)), 500.0),
        _priced(rows, rows.market(at(DAY, 9, 50), at(DAY, 10, 50)), -400.0),
    ]
    live = rows.market(at(DAY, 12, 5), at(DAY, 12, 30))
    assert [one.trade.id for one in caught_up([rows.read()])] == [
        one.id for one in sorted(caught, key=lambda one: one.exit_time)
    ]
    report = paper_report.report(SYMBOL, [rows.read()], books([rows.read()]))
    made = dialog_with(report, ReportSource.AS_WAS)
    try:
        assert _row_ids(made) == [live.entry_order_id]
        assert _card(made) == pytest.approx((live.net, FEE, 1))
        scope = made.scope_label.text()
        assert (
            f"Сделок, решённых на догоне (программа была закрыта или поток обрывался): "
            f"{len(caught)}." in scope
        ), scope
    finally:
        made.deleteLater()


def test_every_trade_of_a_run_is_a_row_the_number_or_the_recount(qapp) -> None:
    """Разбиение «как было» полное: сделок прогона = строк таблицы + число догона + пересчёт.

    Семь сделок — все сочетания фаз, возможные на ленте «пересчёт до 09:55,
    вживую, обрыв, догон, вживую». Пересчёт — сделки, решённые целиком
    до 09:55, его окно называет подписью. Строки таблицы и число читаются
    с экрана. Мутации: сочетание фаз выпало из всех частей (сделка пропала
    молча) или попало в две.
    """
    rows = Rows(paper_run(4, at(DAY, 9)), _monday_after_a_drop)
    rows.market(at(DAY, 9), at(DAY, 9, 30))
    shown = [
        rows.market(at(DAY, 9, 40), at(DAY, 10, 15)),
        rows.market(at(DAY, 10, 5), at(DAY, 10, 20)),
        rows.market(at(DAY, 10, 10), at(DAY, 10, 40)),
        rows.market(at(DAY, 11), at(DAY, 12, 30)),
    ]
    rows.market(at(DAY, 9, 45), at(DAY, 10, 50))
    rows.market(at(DAY, 10, 45), at(DAY, 11, 30))
    recounted = [one for one in rows.trades if one.exit_time <= at(DAY, 9, 55)]
    report = paper_report.report(SYMBOL, [rows.read()], books([rows.read()]))
    made = dialog_with(report, ReportSource.AS_WAS)
    try:
        assert sorted(_row_ids(made)) == sorted(one.entry_order_id for one in shown)
        number = _caught_up_count(made.scope_label.text())
        assert made.table.rowCount() + number + len(recounted) == len(rows.trades), (
            made.table.rowCount(), number, len(recounted)
        )
        assert number == 2, made.scope_label.text()
    finally:
        made.deleteLater()


def _every_day_a_drop(moment: datetime) -> DecisionPhase:
    """Тот же обрыв, что `_monday_after_a_drop`, — в любой день: фаза по часам и минутам."""
    return _monday_after_a_drop(at(DAY, moment.hour, moment.minute))


def test_the_catch_up_number_is_of_the_period_shown_not_of_all_time(qapp) -> None:
    """Число сделок, решённых на догоне, — за выбранный период, как таблица под ним.

    По ленте: 10:45 → 11:30 целиком на догоне — 07.10 и 08.10, 08.10 ещё
    живая 12:05 → 12:30. «Всё» — две сделки догона, «Сегодня» (08.10) —
    одна: строка над таблицей дня говорит только о сделках этого дня. Мутация:
    число берётся за всё время — над днём названа сделка, которой в нём нет.
    """
    rows = Rows(paper_run(4, at(DAY, 9)), _every_day_a_drop)
    rows.market(at(DAY, 10, 45), at(DAY, 11, 30))
    next_day = DAY + timedelta(days=1)
    rows.market(at(next_day, 10, 45), at(next_day, 11, 30))
    live = rows.market(at(next_day, 12, 5), at(next_day, 12, 30))
    report = paper_report.report(SYMBOL, [rows.read()], books([rows.read()]))
    made = dialog_with(report, ReportSource.AS_WAS, today=next_day)
    try:
        assert _caught_up_count(made.scope_label.text()) == 2, made.scope_label.text()
        made.pick_period("Сегодня")
        assert made.period() == (next_day, next_day), "период не тот, что выбран"
        assert _row_ids(made) == [live.entry_order_id]
        assert _caught_up_count(made.scope_label.text()) == 1, made.scope_label.text()
    finally:
        made.deleteLater()


def test_the_recount_does_not_say_its_catch_up_trades_are_left_out(qapp) -> None:
    """«Пересчёт с утра» показывает сделки догона строками — и не пишет над таблицей, что их нет.

    Строка «Сделок, решённых на догоне …: N. В таблицу и итог они не вошли»
    — про «как было». В пересчёте те же сделки стоят в таблице, и такая
    строка над ней была бы неправдой. Надпись сверяется с надписью того же
    прогона, где есть только живая сделка. Мутация: строка выводится
    в любом положении переключателя.
    """
    rows = Rows(paper_run(4, at(DAY, 9)), _monday_after_a_drop)
    caught = [
        rows.market(at(DAY, 10, 45), at(DAY, 11, 30)),
        rows.market(at(DAY, 9, 50), at(DAY, 10, 50)),
    ]
    live = rows.market(at(DAY, 12, 5), at(DAY, 12, 30))
    report = paper_report.report(SYMBOL, [rows.read()], books([rows.read()]))
    as_was_view = dialog_with(report, ReportSource.AS_WAS)
    try:
        said = _caught_up_count(as_was_view.scope_label.text())
    finally:
        as_was_view.deleteLater()
    assert said == len(caught), "вход теста не тот: в «как было» сделок догона не названо"
    made = dialog_with(report, ReportSource.RECOUNT)
    try:
        assert sorted(_row_ids(made)) == sorted(one.entry_order_id for one in [*caught, live])
        scope = made.scope_label.text()
    finally:
        made.deleteLater()
    assert scope == _scope_of_a_live_trade_alone(ReportSource.RECOUNT), scope


def test_a_trade_closed_on_the_catch_up_is_not_marked_live_in_the_recount(qapp) -> None:
    """«Пересчёт с утра»: сделка, закрытая на догоне, помечена догоном — не «вживую», не пересчётом.

    Таблица пересчёта берёт сделки при любой фазе, и пометка — единственное,
    что говорит, застал ли робот закрытие вживую. Три входа, все с выходом
    на догоне: вживую, на догоне и из пересчёта. Мутации: сочетание
    с выходом на догоне подписано «вживую» либо «пересчёт».
    """
    rows = Rows(paper_run(4, at(DAY, 9)), _monday_after_a_drop)
    caught_up = [
        rows.market(at(DAY, 10, 5), at(DAY, 10, 40)),
        rows.market(at(DAY, 9, 50), at(DAY, 10, 50)),
        rows.market(at(DAY, 10, 45), at(DAY, 11, 30)),
    ]
    report = paper_report.report(SYMBOL, [rows.read()], books([rows.read()]))
    made = dialog_with(report, ReportSource.RECOUNT)
    try:
        assert made.table.rowCount() == len(caught_up), "не все сделки догона в пересчёте"
        marks = []
        for line in range(made.table.rowCount()):
            cell = made.table.item(line, 10)
            assert cell is not None
            marks.append(cell.text())
        assert all("догон" in mark and "вживую" not in mark for mark in marks), marks
    finally:
        made.deleteLater()
