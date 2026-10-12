"""Порт: первая записанная минута бара при ждущей заявке — один проход (решение 0065).

Отдельный файл от `tests/test_live_fill_at_first_minute.py` по той же причине,
что `tests/test_app_observe_port.py`: здесь нужен цикл событий поверх Qt,
наблюдателю он не нужен.

Ряд: минутки в базе до начала бара T, дальше — по минуте, как пишет живой
поток (`Source.BROKER`), и `live_candle`, как зовёт его поток. Какой бар T
взять, решает разведка тем же путём (живой ход по всей базе): у неё
исполнение по открытию T есть — значит у хода, обрезанного перед T,
заявка на закрытии T-1 ждёт. Числа под результат не подбирались.
"""

from __future__ import annotations

import dataclasses
import math
import os
import pathlib
from collections.abc import Callable
from datetime import datetime, timedelta

import pytest

os.environ.setdefault("QT_API", "pyside6")  # до первого импорта qasync

from app.port import HistoryPort
from backtest import HistoryRun
from engine import ExitReason
from market import MSK, Candle, CandleStore, MarketWorker, Source, Timeframe
from market.journal import redact
from ui.models import BacktestRequest, ChartData, Layer, MarkerKind
from ui.models import Settings as UiSettings

SYMBOL = UiSettings().instrument
DAY = datetime(2026, 6, 19, 7, 0, tzinfo=MSK)
COUNT = 600
FED = "Незакрытые свечи не в прогоне"


def minute(index: int) -> Candle:
    """Минутка ряда — та же формула, что у базы `tests/test_app_observe_port.py`."""
    price = 100_000.0 + 300.0 * math.sin(index / 9.0)
    return Candle(
        time=DAY + timedelta(minutes=index), open=price, high=price + 40.0,
        low=price - 40.0, close=price, volume=1.0, timeframe=Timeframe(1),
        filled_minutes=1,
    )


def database(
    path: pathlib.Path, until: datetime, source: Callable[[int], Candle] = minute
) -> pathlib.Path:
    """База с минутками ряда до `until` (не включая) — история до потока."""
    with CandleStore(path) as store:
        store.put_minutes(
            SYMBOL, [source(i) for i in range(COUNT) if source(i).time < until], Source.ISS
        )
    return path


@dataclasses.dataclass
class Seen:
    """Что порт показал за шаг: проходов, снимок прогона, строки журнала."""

    passes: int
    run: HistoryRun
    events: list[str]


def _seen(port: HistoryPort, charts: list[object], events: list[str]) -> Seen:
    run = port._run  # noqa: SLF001 — снимок, показанный окну этим проходом
    assert run is not None, "порт не показал ни одного прогона"
    return Seen(len(charts), run, list(events))


async def _session(
    path: pathlib.Path, steps: list[int], source: Callable[[int], Candle] = minute
) -> list[Seen]:
    """Порт с наблюдением; первый шаг — исходный проход, дальше — минуты потока.

    `steps` — номера минут ряда `source`, которые поток пишет по одной
    и о которых зовёт `live_candle`.
    """
    worker = MarketWorker(path)
    await worker.open()
    port = HistoryPort(worker, values=UiSettings(), days=0, sanitize=redact)
    charts: list[object] = []
    events: list[str] = []
    port.chart_replaced.connect(charts.append)
    port.decision_appended.connect(lambda row: events.append(getattr(row, "event", "")))
    port.attach_stream(lambda on: None)
    seen: list[Seen] = []
    try:
        port.stream(True)
        await port.wait()
        seen.append(_seen(port, charts, events))
        for index in steps:
            charts.clear()
            events.clear()
            await worker.put_minutes(SYMBOL, [source(index)], Source.BROKER)
            port.live_candle(SYMBOL)
            await port.wait()
            seen.append(_seen(port, charts, events))
        return seen
    finally:
        await port.aclose()
        await worker.close()


@pytest.fixture
def scouted(loop, tmp_path: pathlib.Path) -> HistoryRun:
    """Разведка: живой ход по всей базе — где исполнения по открытию бара."""
    path = database(tmp_path / "all.sqlite3", DAY + timedelta(days=1))
    seen: list[Seen] = loop.run_until_complete(_session(path, []))
    run = seen[0].run
    assert run.fills, "разведка не дала ни одной сделки"
    return run


def _bar_with_fill(run: HistoryRun) -> int:
    """Номер минуты начала бара, по открытию которого есть исполнение. Не первые."""
    late = DAY + timedelta(hours=2)
    # Исполнение по открытию — у рыночной заявки, решённой на закрытии T-1:
    # время решения равно началу T. У тейка оно другое (закрытие бара касания).
    by_open = {(plan.order_id, plan.decided_at) for plan in run.plans}
    moment = next(
        fill.at for fill in run.fills
        if fill.at >= late and (fill.order_id, fill.at) in by_open
    )
    return int((moment - DAY) / timedelta(minutes=1))


def _bar_without_fill(run: HistoryRun) -> int:
    """Начало бара после прогрева, по открытию которого исполнений нет."""
    filled = {fill.at for fill in run.fills}
    start = DAY + timedelta(hours=2)
    moment = next(
        start + timedelta(minutes=5 * k) for k in range(60)
        if start + timedelta(minutes=5 * k) not in filled
    )
    return int((moment - DAY) / timedelta(minutes=1))


def test_the_first_minute_of_a_bar_fills_the_waiting_order_in_one_pass(
    loop, tmp_path: pathlib.Path, scouted: HistoryRun
) -> None:
    """Стережёт: первая минута бара при ждущей заявке — проход, сделка по открытию.

    До минуты заявка ждёт (`run.waiting`). Первая минута T — ровно один
    проход, и в нём исполнение со временем начала T по открытию первой
    минуты; ждущих больше нет. Строки «Незакрытые свечи не в прогоне»
    рядом с этой сделкой нет: бар не участвует в решениях, но участвует
    в исполнении, и строка прочлась бы как «заявки не было». Вторая минута
    того же бара прохода не гоняет. Ломается, если порт не подаёт
    незакрытый бар (`_forming_of`, `_advance`), если первая минута не ставит
    проход (`_first_minute`) либо ставит его на каждой минуте.
    """
    start = _bar_with_fill(scouted)
    path = database(tmp_path / "live.sqlite3", DAY + timedelta(minutes=start))
    before, first, second = loop.run_until_complete(_session(path, [start, start + 1]))

    opened = DAY + timedelta(minutes=start)
    assert before.run.waiting, "на закрытии T-1 заявка не подана — проверять нечего"
    assert first.passes == 1, f"первая минута дала {first.passes} проходов, а не один"
    fills = [fill for fill in first.run.fills if fill.at == opened]
    assert fills, "заявка не исполнена по первой минуте бара"
    assert {fill.order_id for fill in fills} == set(before.run.waiting)
    assert all(fill.price == minute(start).open for fill in fills), (
        f"исполнено не по открытию первой минуты {minute(start).open}: "
        f"{[fill.price for fill in fills]}"
    )
    assert first.run.waiting == (), f"после первой минуты ждут {first.run.waiting}"
    assert FED not in first.events, (
        "рядом со сделкой по открытию бара строка «такой бар не участвует»"
    )
    assert second.passes == 0, f"вторая минута того же бара дала {second.passes} проходов"


def test_the_first_minute_without_a_waiting_order_runs_nothing(
    loop, tmp_path: pathlib.Path, scouted: HistoryRun
) -> None:
    """Стережёт: заявки нет — первая минута бара только рисует, прохода нет.

    Ломается, если проход ставится на каждой первой минуте (лишний прогон
    раз в бар) или на каждой минуте вообще (`B-009`).
    """
    start = _bar_without_fill(scouted)
    path = database(tmp_path / "idle.sqlite3", DAY + timedelta(minutes=start))
    before, first = loop.run_until_complete(_session(path, [start]))
    assert not before.run.waiting, "заявка ждёт — случай не тот"
    assert first.passes == 0, f"первая минута без заявки дала {first.passes} проходов"


def test_a_bar_whose_first_minute_is_missing_does_not_fill_by_a_foreign_open(
    loop, tmp_path: pathlib.Path, scouted: HistoryRun
) -> None:
    """Стережёт: бар с потерянной первой минутой заявку не исполняет.

    Пришла вторая минута, первой нет: открытие такого бара — чужая цена.
    Заявка ждёт закрытия, как до решения 0065 (`_forming_of`, правило
    полноты). Заявка так и ждёт, но проход на бар один: третья минута
    прохода не гоняет. Ломается, если порт подаёт незакрытый бар с дырой
    или ставит проход на каждой минуте, пока заявка ждёт.
    """
    start = _bar_with_fill(scouted)
    path = database(tmp_path / "hole.sqlite3", DAY + timedelta(minutes=start))
    before, second, third = loop.run_until_complete(
        _session(path, [start + 1, start + 2])
    )
    opened = DAY + timedelta(minutes=start)
    assert before.run.waiting, "заявка не ждёт — случай не тот"
    assert not [fill for fill in second.run.fills if fill.at == opened], (
        "заявка исполнена по открытию бара без первой минуты"
    )
    assert second.run.waiting == before.run.waiting
    assert third.passes == 0, f"ещё минута того же бара дала {third.passes} проходов"


def _undone(chart: ChartData) -> dict[datetime, MarkerKind]:
    """Метки прогноза без исполнения на графике: «заявка подана» и ⊗, по времени."""
    return {
        marker.time: marker.kind for marker in chart.markers
        if marker.layer is Layer.PLAN
        and marker.kind in (MarkerKind.WAITING, MarkerKind.MISSED)
    }


async def _stopped(
    path: pathlib.Path,
) -> tuple[dict[datetime, MarkerKind], dict[datetime, MarkerKind]]:
    """Метки без исполнения до остановки робота и после — на одном и том же ряду."""
    worker = MarketWorker(path)
    await worker.open()
    port = HistoryPort(worker, values=UiSettings(), days=0, sanitize=redact)
    charts: list[ChartData] = []
    port.chart_replaced.connect(charts.append)
    port.attach_stream(lambda on: None)
    try:
        port.stream(True)
        await port.wait()
        assert charts, "порт не показал графика"
        before = _undone(charts[-1])
        charts.clear()
        port.halt("Робот остановлен", "Исход заявки у брокера неизвестен.")
        await port.wait()
        assert port.halted, "робот не остановлен — проверять нечего"
        assert charts, "после остановки график не перерисован"
        return before, _undone(charts[-1])
    finally:
        await port.aclose()
        await worker.close()


def test_a_stopped_robot_draws_no_waiting_order(
    loop, tmp_path: pathlib.Path, scouted: HistoryRun
) -> None:
    """Стережёт: у остановленного робота заявка не «подана» — она ⊗.

    Заявка ждала открытия бара, и окно рисовало часы «заявка подана».
    Робот остановлен причиной не из движка (`HistoryPort.halt`: исход
    заявки у брокера неизвестен; так же — остановка, восстановленная
    после перезапуска): живой ход закрыт, показан прогон по истории,
    а у его движка `halted` пуст и заявка в `waiting`. Исполнять её
    некому, и часы с подсказкой «исполнится по открытию следующей свечи»
    обещали бы сделку, которой не будет (решение 0065, миниплан §2 п. 12).
    Ломается, если «ждёт» решается по одному `run.halted`, без остановки
    порта.
    """
    start = _bar_with_fill(scouted)
    path = database(tmp_path / "stopped.sqlite3", DAY + timedelta(minutes=start))
    before, after = loop.run_until_complete(_stopped(path))
    waited = [moment for moment, kind in before.items() if kind is MarkerKind.WAITING]
    assert waited, "до остановки заявка не ждала — проверять нечего"
    for moment in waited:
        assert after.get(moment) is MarkerKind.MISSED, (
            f"у остановленного робота решение {moment:%H:%M} нарисовано "
            f"«{after[moment].label if moment in after else 'без метки'}», а не ⊗"
        )


#: Бары ряда `stepped` по номерам первых минут: T-1 10:00, T 10:05, T+1 10:10.
BEFORE, BAR_T, AFTER = 180, 185, 190


def stepped(index: int) -> Candle:
    """Ряд ступенями: ровно до T-1, T-1 выше, T ниже на 1 %, T+1 снова выше.

    Ровный ряд держит закрытие равным средней — входа нет (неравенства
    строгие), и первый сигнал «лонг» приходится ровно на закрытие T-1
    (10:05, внутри окна 09:30–11:30). Вход по открытию T — 100 000, тейк
    100 500. Закрытие T-1 (101 000) за тейком, и вся T+1 (101 000 ± 40)
    за тейком тоже: судить касание по закрытию T-1 — «разрыва не было».
    """
    price = 101_000.0 if BEFORE <= index < BAR_T or index >= AFTER else 100_000.0
    return Candle(
        time=DAY + timedelta(minutes=index), open=price, high=price + 40.0,
        low=price - 40.0, close=price, volume=1.0, timeframe=Timeframe(1),
        filled_minutes=1,
    )


def test_a_lost_last_minute_and_a_partial_next_bar_do_not_stop_the_robot(
    loop, tmp_path: pathlib.Path
) -> None:
    """Стережёт: потерянная последняя минута T и незакрытая T+1 не останавливают робота.

    Вход исполнен по первой минуте T. Последней минуты T нет — закрытую T
    порт выбрасывает как неполную (`_trusted`) и замечает это только на
    первой минуте T+1, тем же проходом показывая T+1 незакрытой
    (`_forming_of`). Закрытая T+1 целиком за тейком: прошлый интервал
    показан только огрызком, касание — разрыв, выход по уровню 100 500.
    Ломается, если показ незакрытой T+1 затирает у исполнителя память о
    незакрытой T: касание судится по закрытию T-1 → «Ошибка внутри
    исполнителя», робот остановлен.
    """
    path = database(tmp_path / "lost.sqlite3", DAY + timedelta(minutes=BAR_T), stepped)
    steps = [BAR_T, BAR_T + 1, BAR_T + 2, BAR_T + 3, *range(AFTER, AFTER + 5)]
    seen = loop.run_until_complete(_session(path, steps, stepped))
    before, first, *_, next_open = seen[: 1 + steps.index(AFTER) + 1]
    last = seen[-1]

    assert before.run.waiting, "на закрытии T-1 заявка не подана — проверять нечего"
    opened = DAY + timedelta(minutes=BAR_T)
    assert [fill.price for fill in first.run.fills if fill.at == opened] == [100_000.0], (
        "вход не исполнен по первой минуте T"
    )
    assert next_open.passes == 1, (
        f"первая минута T+1 дала {next_open.passes} проходов: незакрытая T+1 "
        "не показана — случай не тот"
    )
    assert not last.run.halted, f"робот остановлен: {last.run.halted}"
    assert len(last.run.deals) == 1, last.run.deals
    (deal,) = last.run.deals
    assert (deal.entry_time, deal.entry_price) == (opened, 100_000.0)
    assert deal.exit_time == DAY + timedelta(minutes=AFTER)
    assert deal.exit_price == pytest.approx(100_500.0), "выход не по уровню тейка"
    assert deal.exit_reason is ExitReason.TAKE_PROFIT


async def _pinned(path: pathlib.Path) -> tuple[HistoryRun, ChartData]:
    """Прогон тестера на отрезке, который кончается баром T-1 с поданной заявкой.

    Сначала — прогон на отрезке во весь ряд: где исполнение по открытию
    бара T (`_bar_with_fill`). Затем — тот же отрезок, обрезанный последней
    минутой бара T-1: бары после него в базе есть, в прогон они не входят.
    Оба прогона — тестер, разведка тем же путём, что и проверка.
    """
    worker = MarketWorker(path)
    await worker.open()
    port = HistoryPort(worker, values=UiSettings(), days=0, sanitize=redact)
    charts: list[ChartData] = []
    port.chart_replaced.connect(charts.append)
    try:
        await port.wait()
        port.run_backtest(BacktestRequest(
            settings=UiSettings(), since=DAY, until=DAY + timedelta(minutes=COUNT - 1),
        ))
        await port.wait()
        start = _bar_with_fill(_seen(port, [], []).run)
        charts.clear()
        # Конец отрезка — первая минута T, включительно: бар T в прогон
        # не войдёт (незакрыт по границе отрезка), бар T-1 — закрыт. Последняя
        # минута T-1 как конец не годится: граница полноты легла бы на неё,
        # и незакрытым объявился бы сам T-1.
        port.run_backtest(BacktestRequest(
            settings=UiSettings(), since=DAY, until=DAY + timedelta(minutes=start),
        ))
        await port.wait()
        assert charts, "прогон на обрезанном отрезке не показал графика"
        return _seen(port, [], []).run, charts[-1]
    finally:
        await port.aclose()
        await worker.close()


def test_a_pinned_span_draws_no_waiting_order(loop, tmp_path: pathlib.Path) -> None:
    """Стережёт: на закреплённом отрезке ждущая заявка — ⊗ «прогон кончился», не часы.

    Находка `/review` 12.10.2026. Прогон тестера кончился на баре T-1,
    заявка подана на его закрытии и в `run.waiting`. Бар T в базе есть,
    но в закреплённый отрезок не входит, и исполнения не будет: часы
    с подсказкой «исполнится по открытию следующей свечи» — неправда.
    Ломается, если порт не передаёт признак закреплённого отрезка
    (`_show` → `_pending_of(pinned=…)` → `pending=Unfilled.CUT`) или
    `convert` его не слушает.
    """
    path = database(tmp_path / "pinned.sqlite3", DAY + timedelta(days=1))
    run, chart = loop.run_until_complete(_pinned(path))
    assert run.waiting, "на последней свече отрезка заявка не подана — проверять нечего"
    cut = [
        marker for marker in chart.markers
        if marker.layer is Layer.PLAN and marker.pair_id in run.waiting
    ]
    assert cut, "у ждущей заявки на графике нет метки прогноза"
    for marker in cut:
        assert marker.kind is MarkerKind.MISSED, (
            f"ждущая заявка на конце отрезка нарисована «{marker.kind.label}», а не ⊗"
        )
        assert "прогон кончился до исполнения" in (marker.tooltip or ""), (
            f"подсказка не говорит, что прогон кончился: «{marker.tooltip}»"
        )
    waiting = [marker for marker in chart.markers if marker.kind is MarkerKind.WAITING]
    assert not waiting, f"на закреплённом отрезке нарисованы часы: {waiting}"


async def _live(path: pathlib.Path) -> tuple[HistoryRun, ChartData]:
    """Живой ход по базе: снимок прогона и последний показанный график."""
    worker = MarketWorker(path)
    await worker.open()
    port = HistoryPort(worker, values=UiSettings(), days=0, sanitize=redact)
    charts: list[ChartData] = []
    port.chart_replaced.connect(charts.append)
    port.attach_stream(lambda on: None)
    try:
        port.stream(True)
        await port.wait()
        assert charts, "порт не показал графика"
        return _seen(port, [], []).run, charts[-1]
    finally:
        await port.aclose()
        await worker.close()


def test_a_live_run_draws_the_waiting_order_as_clocks(
    loop, tmp_path: pathlib.Path, scouted: HistoryRun
) -> None:
    """Стережёт: в живом ходе ждущая заявка — часы «заявка подана», а не ⊗.

    Пара к `test_a_pinned_span_draws_no_waiting_order`: признак отрезка
    гасит часы только на закреплённом отрезке тестера. В живом ходе
    отрезка нет, заявка подана на закрытии последнего бара и исполнится
    по открытию следующего — ровно то, ради чего часы заведены (просьба
    владельца счёта 12.10.2026, решение 0065). Ломается, если порт
    говорит «отрезок» и без отрезка (`_show` → `_pending_of`, `Unfilled.CUT`):
    ⊗ «прогон кончился» на живом графике — та же ложь «заявки не было»,
    только с другой подписью.
    """
    start = _bar_with_fill(scouted)
    path = database(tmp_path / "live.sqlite3", DAY + timedelta(minutes=start))
    run, chart = loop.run_until_complete(_live(path))
    assert run.waiting, "на закрытии последнего бара заявка не подана — проверять нечего"
    drawn = {
        marker.pair_id: marker for marker in chart.markers
        if marker.layer is Layer.PLAN and marker.pair_id in run.waiting
    }
    assert set(drawn) == set(run.waiting), (
        f"у ждущих заявок {sorted(set(run.waiting) - set(drawn))} нет метки прогноза"
    )
    for order_id, marker in drawn.items():
        assert marker.kind is MarkerKind.WAITING, (
            f"в живом ходе ждущая заявка {order_id} нарисована "
            f"«{marker.kind.label}», а не часами «заявка подана»"
        )
        assert "исполнится по открытию следующей свечи" in (marker.tooltip or ""), (
            f"подсказка ждущей заявки {order_id} не говорит, когда она "
            f"исполнится: «{marker.tooltip}»"
        )
