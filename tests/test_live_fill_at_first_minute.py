"""Живой ход: заявка исполняется по первой записанной минуте бара (решение 0065).

Владелец счёта 12.10.2026: заявка, поданная на закрытии T-1, обязана стать
сделкой по открытию T сразу после первой минуты T, а не на закрытии T; пока
минуты нет — заявка «ждёт» (`HistoryRun.waiting`), а не «не исполнена».

Здесь — наблюдатель (`LiveObserver`) и его лента (`LiveSource`) без порта
и без Qt; проход порта на первой минуте — `tests/test_live_fill_at_first_minute_port.py`.
Ряд синтетический (`tests/test_app_observe.bars`), база — во временной
папке (правило 14). Сверка идёт с прогоном по истории на тех же барах
и с ходом без незакрытых показов, а не с числами, выведенными из самой
правки.
"""

from __future__ import annotations

import asyncio
import dataclasses
import pathlib
from collections.abc import Callable, Coroutine
from datetime import date, datetime
from typing import cast

import pytest

from app.observe import LiveObserver, LiveSource
from app.stitched_view import as_history
from backtest import Costs, Deal, HistoryRun, replay
from backtest.stitched import Piece, PieceRun, StitchedRun
from engine import ExitReason, Side, close_time
from market import Candle, MarketWorker, OrderRecord, RunRowsMark, TradeRecord
from strategies import MaReverseAlways, MaReverseAlwaysSettings
from tests.test_app_observe import DEADLINE, Said, bars, settings
from tests.test_app_paper_log import SERIES, SYMBOL, WARM, expected_trades
from tests.test_app_paper_log import observer as recorded_observer
from ui.models import DecisionLevel

REOPENED = "Открытие свечи изменилось после сделки"


def first_minute(bar: Candle, *, opened: float | None = None, minutes: int = 1) -> Candle:
    """Бар, собранный из первых минут интервала: незакрытый, открытие — бара.

    Огрызок: размах и закрытие — открытие (одна точка), как у минуты без
    движения. `opened` — открытие показа, если тест сдвигает его нарочно.
    """
    price = bar.open if opened is None else opened
    return dataclasses.replace(
        bar, open=price, high=price, low=price, close=price,
        filled_minutes=minutes, unsettled=True,
    )


@pytest.fixture(scope="module")
def loop():
    """Свой цикл asyncio на файл — по той же причине, что в `test_app_observe`."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def drive(loop):
    def run[T](work: Callable[[], Coroutine[object, object, T]]) -> T:
        return cast("T", loop.run_until_complete(asyncio.wait_for(work(), DEADLINE * 3)))

    return run


def plain_observer(said: Said | None = None) -> LiveObserver:
    return LiveObserver(
        strategy=MaReverseAlways(MaReverseAlwaysSettings()),
        settings=settings(),
        say=said or Said(),
        costs=Costs(commission_per_side=14.0),
    )


@dataclasses.dataclass
class Ride:
    """Что ход показал и что легло в базу — для сверки двух ходов."""

    run: HistoryRun
    trades: list[TradeRecord]
    orders: list[OrderRecord]
    decisions: int
    waited: int = 0
    #: Исполнения по первой минуте — по действию заявки: вход и выход.
    actions: list[str] = dataclasses.field(default_factory=list)


async def _rows(
    worker: MarketWorker,
) -> tuple[list[TradeRecord], list[OrderRecord], int]:
    (paper,) = await worker.paper_runs([SYMBOL])
    session = paper.session.id
    trades = [one.record for one in await worker.trades(session_id=session)]
    orders = [one.record for one in await worker.orders(session_id=session)]
    decisions = len(await worker.decisions(session_id=session))
    return trades, orders, decisions


async def _ride(path: pathlib.Path, *, partials: bool) -> Ride:
    """Прогрев одним отрезком, дальше по бару; `partials` — с первыми минутами.

    Закрытые бары приходят проходами трёх порядков, по кругу, — одинаково
    с первыми минутами и без них, иначе строки базы различались бы фазой
    («догон» у старшего из двух баров одного прохода, `LiveObserver._phase`),
    а не первыми минутами:

    * проход на закрытии T-1 отдельно, затем проход на первой минуте T;
    * первой минуты T проход не видел (потеряна или проход не успел) —
      T придёт только закрытой;
    * два закрытых бара, включая ту T, и первая минута следующего бара —
      **одним** проходом (проход на закрытии опоздал до первой минуты).

    Третий порядок и есть тот, где лента обязана сначала разобрать закрытые
    бары и только потом показать незакрытый: иначе заявка, поданная
    на закрытии T-1, исполнится по открытию T+1 вместо открытия T, а младший
    закрытый бар получит фазу «догон». Каждый проход с первой минутой
    сверяется тут же: по открытию бара исполнены ровно заявки, решённые
    на закрытии прошлого бара, ждущих нет, сделки и строки уже в базе.
    """
    worker = MarketWorker(path)
    await worker.open()
    watcher = recorded_observer(worker, Said())
    ride = Ride(HistoryRun(), [], [], 0)
    try:
        await watcher.start()
        run = await watcher.feed(SERIES[:WARM])
        for count in range(WARM, len(SERIES)):
            bar = SERIES[count]
            order = count % 3
            if order == 0:
                # Первой минуты этого бара проход не видел: бар придёт только
                # закрытым — вместе с первой минутой следующего.
                continue
            if not partials:
                run = await watcher.feed(SERIES[:count])
                continue
            if order == 2:
                run = await watcher.feed(SERIES[:count])
                ride.waited += bool(run.waiting)
            run = await watcher.feed(SERIES[:count], first_minute(bar))
            # До закрытого бара `count` сделка со временем его начала может
            # быть только исполнением по открытию — первой минутой. Исполнены
            # обязаны быть ровно заявки, решённые на закрытии прошлого бара.
            decided = {plan.order_id for plan in run.plans if plan.decided_at == bar.time}
            fresh = [fill for fill in run.fills if fill.at == bar.time]
            assert {fill.order_id for fill in fresh} == decided, (
                f"по открытию бара {bar.time:%H:%M} ждали {sorted(decided)}, "
                f"исполнено первой минутой {[fill.order_id for fill in fresh]}"
            )
            for fill in fresh:
                assert (fill.at, fill.price) == (bar.time, bar.open), (
                    f"заявка {fill.order_id} исполнена {fill.at} по {fill.price}, "
                    f"а не по открытию бара {bar.time} ({bar.open})"
                )
            assert not run.waiting, f"после первой минуты ждут {run.waiting}"
            ride.actions.extend(fill.action.value for fill in fresh)
            if fresh:
                trades, _, decisions = await _rows(worker)
                assert trades == expected_trades(run), (
                    "сделка по первой минуте не легла в базу тем же проходом"
                )
                assert decisions == len(run.journal), (
                    f"строк решений в базе {decisions}, у хода {len(run.journal)}"
                )
        # Последний бар ряда — его закрытие ещё не приходило.
        run = await watcher.feed(SERIES)
        ride.run = run
        ride.trades, ride.orders, ride.decisions = await _rows(worker)
        return ride
    finally:
        await watcher.aclose()
        await worker.close()


def test_a_waiting_order_fills_at_the_first_minute_like_the_history_says(
    drive, tmp_path: pathlib.Path
) -> None:
    """Стережёт: вход и выход — после первой минуты следующей свечи, по её открытию.

    Каждая ждавшая заявка исполнена первой же минутой бара по его открытию,
    со временем начала бара; закрытая сделка и строки решений лежат в базе
    уже после этого прохода. Итог хода равен прогону по истории на тех же
    барах, а строки базы — ходу без первых минут: фаза строк (`LIVE`, а не
    догон), закрытие и средняя у строки вооружения — те же. Ломается, если
    порт или лента перестанут подавать незакрытый бар (сделка ждёт закрытия),
    если строки заявок будут писаться по огрызку, если первая минута
    попадёт в ленту баром, если лента покажет незакрытый бар раньше
    закрытых баров того же прохода (`LiveObserver._ride`).
    """

    async def go() -> tuple[Ride, Ride, HistoryRun]:
        shown = await _ride(tmp_path / "shown.sqlite3", partials=True)
        closed = await _ride(tmp_path / "closed.sqlite3", partials=False)
        history = await replay(
            SERIES, MaReverseAlways(MaReverseAlwaysSettings()), settings(),
            costs=Costs(commission_per_side=14.0),
        )
        return shown, closed, history

    shown, closed, history = drive(go)
    assert shown.waited, "ни разу не ждала заявка — нечего было исполнять"
    assert {"open", "close"} <= set(shown.actions), (
        f"по первой минуте исполнены только {set(shown.actions)} — вход и выход "
        "обязаны быть оба"
    )
    assert shown.run.deals == history.deals, "сделки разошлись с прогоном по истории"
    assert shown.run.fills == history.fills, "исполнения разошлись с прогоном по истории"
    assert shown.run.journal == history.journal, "журнал разошёлся с прогоном по истории"
    assert shown.run.plans == history.plans, "заявки разошлись с прогоном по истории"
    assert shown.run.bars == history.bars, "первая минута посчитана баром"
    assert shown.trades == closed.trades, "сделки в базе разошлись с ходом без первых минут"
    assert shown.orders == closed.orders, (
        "строки заявок разошлись с ходом без первых минут: фаза, закрытие или средняя"
    )
    assert shown.decisions == closed.decisions


def test_repeated_first_minutes_add_neither_deals_nor_rows(
    drive, tmp_path: pathlib.Path
) -> None:
    """Стережёт: повторный показ того же незакрытого бара ничего не прибавляет.

    Четыре показа одного бара (1…4 минуты, открытие то же): после первого
    ни исполнений, ни сделок, ни строк журнала, ни строк базы больше
    не становится. Ломается, если исполнение по открытию повторяется
    на каждом показе (двойная сделка) или строки пишутся заново.
    """
    path = tmp_path / "repeat.sqlite3"

    async def go() -> list[tuple[int, int, int, int, int, int]]:
        worker = MarketWorker(path)
        await worker.open()
        watcher = recorded_observer(worker, Said())
        counts = []
        try:
            await watcher.start()
            run = await watcher.feed(SERIES[:WARM])
            count = WARM
            while not run.waiting:
                count += 1
                assert count < len(SERIES), "в ряду не нашлось ждущей заявки"
                run = await watcher.feed(SERIES[:count])
            for minutes in range(1, 5):
                run = await watcher.feed(
                    SERIES[:count], first_minute(SERIES[count], minutes=minutes)
                )
                trades, orders, decisions = await _rows(worker)
                counts.append((
                    len(run.fills), len(run.deals), len(run.journal),
                    len(trades), len(orders), decisions,
                ))
            return counts
        finally:
            await watcher.aclose()
            await worker.close()

    counts = drive(go)
    assert len(set(counts)) == 1, f"повторные показы прибавили строки: {counts}"


def test_a_first_minute_bar_is_not_a_bar_of_the_run(drive, tmp_path: pathlib.Path) -> None:
    """Стережёт: незакрытый бар в ленту не берётся и баром хода не считается.

    Бары, средняя и граница «строки записаны» (`live_from` — закрытие
    последнего закрытого бара) — те же, что без него. Ломается, если
    первая минута пойдёт в ленту: средняя получит точку по огрызку,
    а граница уйдёт в будущее.
    """
    series = SERIES[:60]
    path = tmp_path / "bars.sqlite3"

    async def go() -> tuple[HistoryRun, HistoryRun, RunRowsMark | None]:
        plain = plain_observer()
        await plain.start()
        without = await plain.feed(series)
        await plain.aclose()
        worker = MarketWorker(path)
        await worker.open()
        watcher = recorded_observer(worker, Said())
        try:
            await watcher.start()
            shown = await watcher.feed(series, first_minute(SERIES[60]))
            (paper,) = await worker.paper_runs([SYMBOL])
            return without, shown, paper.mark
        finally:
            await watcher.aclose()
            await worker.close()

    without, shown, mark = drive(go)
    assert shown.bars == 60, f"незакрытый бар посчитан баром: {shown.bars}"
    assert shown.average == without.average, "средняя получила точку по огрызку"
    assert mark is not None, "отметка «строки записаны» не поставлена"
    assert mark.live_from == close_time(series[-1]), (
        f"граница «строки записаны» ушла: {mark.live_from}"
    )


# ------------------------------------------------------------ лента и забор


def test_a_closed_bar_behind_a_shown_first_minute_is_named_late() -> None:
    """Стережёт: после показа незакрытой T закрытый бар раньше T в ленту не идёт.

    Он позади исполнителя — по открытию T уже исполнено, — и порча хода
    времени назад движку недопустима. Бар называется поздним (`Seam.late`,
    строка WARNING), закрытая T при этом берётся. Ломается, если забора
    нет: T-1 ушла бы движку после исполнения по открытию T.
    """
    series = bars(20)
    source = LiveSource()
    source.offer(series[:10])
    source.offer(series[:10], first_minute(series[11]))
    assert source.take_forming() is not None, "незакрытый бар не принят"
    seam = source.offer(series[:12])
    assert seam.late == (series[10].time,), f"бар позади показанного не назван: {seam.late}"
    assert seam.taken == 1, f"взято {seam.taken} баров, а должна быть только закрытая T"
    assert source.take_forming() is None, "незакрытый бар отдан второй раз"


def test_a_stale_first_minute_is_not_shown_again() -> None:
    """Стережёт: незакрытый бар, чей интервал уже взят закрытым, не показывается.

    Ломается, если лента отдаст движку огрызок бара, который он уже разобрал
    закрытым: исполнитель увидел бы свечу из прошлого.
    """
    series = bars(20)
    source = LiveSource()
    source.offer(series[:12])
    source.offer(series[:12], first_minute(series[11]))
    assert source.take_forming() is None


def test_the_open_of_a_first_minute_is_checked_against_its_closed_bar() -> None:
    """Стережёт: лента сверяет открытие показа с открытием закрытого бара.

    Ломается, если сверки нет: расхождение не дойдёт до журнала.
    """
    series = bars(20)
    source = LiveSource()
    source.offer(series[:11])
    source.offer(series[:11], first_minute(series[11], opened=series[11].open + 7.0))
    seam = source.offer(series[:12])
    assert seam.reopened == ((series[11].time, series[11].open + 7.0, series[11].open),)
    assert not seam.quiet


async def _to_waiting(watcher: LiveObserver, series: list[Candle], start: int) -> int:
    """Идти по бару, пока на закрытии не появится ждущая заявка. Вернуть бар T."""
    run = await watcher.feed(series[:start])
    count = start
    while not run.waiting:
        count += 1
        assert count < len(series), "в ряду не нашлось ждущей заявки"
        run = await watcher.feed(series[:count])
    return count


def test_a_fill_at_an_open_that_changed_later_is_said(drive) -> None:
    """Стережёт: исполнено по открытию показа, а у закрытого бара оно другое — строка WARNING.

    Минуту начала переписала догрузка (`D-149`): цена сделки осталась прежней,
    и об этом обязана сказать строка журнала. Без исполнения по такому
    показу строки нет: говорить не о чем. Ломается молчанием — строку убрали
    или перестали сверять открытие (правило 13).
    """
    series = bars(120)

    async def go() -> tuple[Said, Said, float]:
        said = Said()
        watcher = plain_observer(said)
        await watcher.start()
        count = await _to_waiting(watcher, series, 20)
        moved = series[count].open + 7.0
        run = await watcher.feed(series[:count], first_minute(series[count], opened=moved))
        price = next(fill.price for fill in run.fills if fill.at == series[count].time)
        await watcher.feed(series[: count + 1])
        await watcher.aclose()

        quiet = Said()
        other = plain_observer(quiet)
        await other.start()
        idle = 60
        run = await other.feed(series[:idle])
        while run.waiting:
            idle += 1
            assert idle < len(series), "в ряду не нашлось бара без ждущей заявки"
            run = await other.feed(series[:idle])
        await other.feed(series[:idle], first_minute(series[idle], opened=series[idle].open + 7.0))
        await other.feed(series[: idle + 1])
        await other.aclose()
        return said, quiet, price - moved

    said, quiet, shift = drive(go)
    assert shift == 0.0, "исполнено не по открытию показа"
    rows = said.about(REOPENED)
    assert len(rows) == 1, f"строк о сменившемся открытии {len(rows)}: {said.events()}"
    assert rows[0][2] is DecisionLevel.WARNING
    assert not quiet.about(REOPENED), "строка без исполнения — шум"


def test_a_closed_run_awaits_nothing(drive) -> None:
    """Стережёт: закрытый ход не просит прохода на первой минуте.

    Заявка ждёт — `awaits_open` истинно; ход закрыт — ложно, и метка
    «ждёт» ему не положена. Ломается, если признак смотрит только
    на очередь исполнителя.
    """
    series = bars(120)

    async def go() -> tuple[bool, bool]:
        watcher = plain_observer()
        await watcher.start()
        await _to_waiting(watcher, series, 20)
        waiting = watcher.awaits_open
        await watcher.aclose()
        return waiting, watcher.awaits_open

    waiting, closed = drive(go)
    assert waiting, "заявка ждёт, а ход прохода не просит"
    assert not closed, "закрытый ход всё ещё ждёт открытия бара"


# --------------------------------------------- ждущие заявки во всех снимках


def test_the_live_run_names_the_order_that_waits_for_the_open(drive) -> None:
    """Стережёт: `LiveObserver.run` несёт имя заявки, ждущей первой минуты.

    Заявка подана на закрытии T-1, минуты T нет — её имя в `run.waiting`;
    первая минута T пришла — имени нет. Ломается, если поле не заполнено:
    окно нарисует ⊗ на заявке, которая просто ждёт.
    """
    series = bars(120)

    async def go() -> tuple[HistoryRun, HistoryRun, set[str]]:
        watcher = plain_observer()
        await watcher.start()
        count = await _to_waiting(watcher, series, 20)
        before = watcher.run
        decided = {plan.order_id for plan in before.plans if plan.decided_at == series[count].time}
        after = await watcher.feed(series[:count], first_minute(series[count]))
        await watcher.aclose()
        return before, after, decided

    before, after, decided = drive(go)
    assert decided, "на закрытии T-1 не подано ни одной заявки"
    assert set(before.waiting) == decided, f"ждут {before.waiting}, подано {decided}"
    assert after.waiting == (), f"после первой минуты всё ещё ждут {after.waiting}"


def test_the_history_replay_names_the_order_left_waiting(drive) -> None:
    """Стережёт: `replay` несёт заявку, поданную на закрытии последнего бара.

    Ломается, если поле не заполнено: на последнем решении прогона окно
    нарисует ⊗ вместо «ждёт».
    """
    series = bars(120)

    async def go() -> tuple[HistoryRun, set[str]]:
        full = await replay(series, MaReverseAlways(MaReverseAlwaysSettings()), settings())
        moment = next(
            plan.decided_at for plan in full.plans if plan.decided_at > series[20].time
        )
        cut = next(index for index, bar in enumerate(series) if close_time(bar) == moment) + 1
        run = await replay(series[:cut], MaReverseAlways(MaReverseAlwaysSettings()), settings())
        last = {plan.order_id for plan in run.plans if plan.decided_at == moment}
        return run, last

    run, last = drive(go)
    assert last, "на последнем баре нет решения — сверять нечего"
    assert set(run.waiting) == last, f"ждут {run.waiting}, подано на последнем баре {last}"


def _piece(symbol: str) -> Piece:
    return Piece(
        symbol=symbol, since=date(2026, 6, 19), until=date(2026, 6, 19),
        warmup=(), body=(), warmup_wanted=0,
    )


def _seam_deal() -> Deal:
    moment = datetime(2026, 6, 19, 18, 45, tzinfo=bars(1)[0].time.tzinfo)
    return Deal(
        side=Side.LONG, volume=1.0,
        entry_time=moment, entry_price=100.0, entry_order_id="open",
        exit_time=moment, exit_price=100.0, exit_order_id="seam",
        exit_reason=ExitReason.SIGNAL,
    )


def test_the_stitched_run_names_only_the_last_piece_waiting() -> None:
    """Стережёт: склейка несёт ждущие заявки последнего куска — и только его.

    Последний кусок без стыка — его заявки ждут; кусок со стыком — позиция
    закрыта на стыке, ждать нечего; заявки прежних кусков не ждут никогда.
    Ломается, если `as_history` поле не заполняет либо складывает куски.
    """
    first = PieceRun(_piece("MXU6"), HistoryRun(waiting=("old",)), seam=_seam_deal())
    last = PieceRun(_piece("MXZ6"), HistoryRun(waiting=("new",)))
    costs = Costs(commission_per_side=14.0)
    assert as_history(StitchedRun(pieces=(first, last)), costs).waiting == ("new",)
    closed = dataclasses.replace(last, seam=_seam_deal())
    assert as_history(StitchedRun(pieces=(first, closed)), costs).waiting == ()
    assert as_history(StitchedRun(), costs).waiting == ()
