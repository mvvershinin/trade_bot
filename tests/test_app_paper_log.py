"""Живой ход симуляции пишет свои строки в базу: без потерь и без дублей.

Каждый тест стережёт **одно** поведение записи (`app/paper_log.py`
и её проводку в `app/observe.py`), и оно названо первой строкой докстринга.
Ряд — синтетический, база — во временной папке, отказы базы — подставным
потоком данных (правило 14). Сверка идёт с тем, что ход показал бы окну
(`LiveObserver.run`, `submitted`), а не с числами, выведенными из самой записи.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import pathlib
import sqlite3
import threading
from collections.abc import Callable, Coroutine
from typing import cast

import pytest

from app import convert
from app.observe import LiveObserver, RunRecording
from app.runs import RunLog
from backtest import BREATHE_EVERY, Costs, HistoryRun
from engine import ExitReason, close_time
from market import (
    CandleStore,
    MarketWorker,
    OrderKind,
    PaperRows,
    PaperWritten,
    RunOrigin,
    RunRowsMark,
    SessionRecord,
    TradeRecord,
)
from strategies import MaReverseAlways, MaReverseAlwaysSettings
from tests.test_app_observe import DEADLINE, Said, bars, settings
from ui.models import DecisionLevel

SYMBOL = "MXZ6"
#: Ряд: двадцать баров первого прохода, дальше по одному живому — как
#: на закрытии бара у порта, который отдаёт отрезок целиком на каждом проходе.
#: Первый проход короткий намеренно: сделки этого ряда приходятся на 08:20–10:25,
#: и при длинном первом проходе живая часть осталась бы без единой строки —
#: писать по ходу было бы нечего, и проверки записи по ходу зеленели бы впустую.
SERIES = bars(120)
WARM = 20

FAILED = "Сделки симуляции не записаны в базу"
RECOVERED = "Запись симуляции восстановлена"
LOST = "Сделки симуляции потеряны"


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


class FlakyWorker(MarketWorker):
    """Поток данных, у которого запись строк хода отказывает заданное число раз."""

    def __init__(self, path: pathlib.Path, *, failures: int = 0) -> None:
        super().__init__(path)
        self.failures = failures
        self.attempts = 0

    async def write_paper_rows(
        self,
        session_id: int,
        rows: PaperRows,
        *,
        on_written: Callable[[PaperWritten], None] | None = None,
    ) -> PaperWritten:
        self.attempts += 1
        if self.failures:
            self.failures -= 1
            raise sqlite3.OperationalError("database is locked")
        return await super().write_paper_rows(session_id, rows, on_written=on_written)


class SlowWorker(MarketWorker):
    """Поток данных, у которого запись фиксируется и застревает **в потоке базы**.

    Застревание — после фиксации и до сдвига курсоров: ровно то окно, в котором
    снятый проход оставлял записанную пачку неучтённой.
    """

    def __init__(self, path: pathlib.Path) -> None:
        super().__init__(path)
        self.slow = False
        self.entered = threading.Event()
        self.gate = threading.Event()

    async def write_paper_rows(
        self,
        session_id: int,
        rows: PaperRows,
        *,
        on_written: Callable[[PaperWritten], None] | None = None,
    ) -> PaperWritten:
        slow = self.slow

        def hold(written: PaperWritten) -> None:
            if slow:
                self.entered.set()
                self.gate.wait(DEADLINE)
            if on_written is not None:
                on_written(written)

        return await super().write_paper_rows(session_id, rows, on_written=hold)


@dataclasses.dataclass
class Stored:
    """Что лежит в базе по прогону — прочитано заново, мимо хода."""

    decisions: int
    submitted: int
    level_hits: int
    trades: list[TradeRecord]
    finished: bool
    finish_note: str
    mark: RunRowsMark | None


def stored(path: pathlib.Path) -> Stored:
    with CandleStore(path) as store:
        (run,) = store.paper_runs([SYMBOL])
        session = run.session.id
        orders = store.orders(session_id=session)
        return Stored(
            decisions=len(store.decisions(session_id=session)),
            submitted=sum(1 for one in orders if one.kind is OrderKind.SUBMITTED),
            level_hits=sum(1 for one in orders if one.kind is OrderKind.LEVEL_HIT),
            trades=[one.record for one in store.trades(session_id=session)],
            finished=run.session.finished_at is not None,
            finish_note=run.session.finish_note,
            mark=run.mark,
        )


def observer(worker: MarketWorker, said: Said) -> LiveObserver:
    runs = RunLog(
        worker,
        warn=lambda event, reason: said(event, reason, DecisionLevel.WARNING),
        note=lambda event, reason: said(event, reason, DecisionLevel.INFO),
    )
    return LiveObserver(
        strategy=MaReverseAlways(MaReverseAlwaysSettings()),
        settings=settings(),
        say=said,
        costs=Costs(commission_per_side=14.0),
        recording=RunRecording(
            around=lambda: runs.around(SessionRecord(RunOrigin.PAPER, symbol=SYMBOL)),
            store=worker,
            symbol=SYMBOL,
        ),
    )


async def ride(watcher: LiveObserver, until: int = len(SERIES)) -> HistoryRun:
    """Прогрев одним отрезком, дальше по бару — как порт на закрытии бара."""
    await watcher.start()
    run = await watcher.feed(SERIES[:WARM])
    for count in range(WARM + 1, until + 1):
        run = await watcher.feed(SERIES[:count])
    return run


def expected_trades(run: HistoryRun) -> list[TradeRecord]:
    return [convert.trade_record(deal, SYMBOL) for deal in run.deals]


def level_exits(run: HistoryRun) -> int:
    """Касаний уровня столько же, сколько сделок, закрытых по уровню."""
    levels = {ExitReason.TAKE_PROFIT, ExitReason.TRAILING_TAKE}
    return sum(1 for deal in run.deals if deal.exit_reason in levels)


def assert_whole(found: Stored, run: HistoryRun, watcher: LiveObserver) -> None:
    """В базе ровно то, что ход показал окну: ни строки меньше, ни дубля."""
    assert run.deals, "ряд не дал ни одной сделки — сверять нечего"
    assert any(deal.exit_time > SERIES[WARM].time for deal in run.deals), (
        "живая часть ряда без сделок — запись по ходу не проверена"
    )
    assert found.trades == expected_trades(run), "сделки в базе не те, что в окне"
    assert found.decisions == len(run.journal), (
        f"строк решений в базе {found.decisions}, у хода {len(run.journal)}"
    )
    assert found.submitted == watcher.submitted, (
        f"заявок в базе {found.submitted}, подано {watcher.submitted}"
    )
    assert found.level_hits == level_exits(run)


# ------------------------------------------------------------------ сохранность


def test_the_rows_of_a_live_run_survive_a_crash(drive, tmp_path: pathlib.Path) -> None:
    """Сделки, заявки и решения лежат в базе по ходу, а не только после закрытия.

    Программу закрыли аварийно — `aclose` не позван. Новое открытие базы
    находит всё, что ход показал окну, и отметку с границами пересчёта.
    """
    path = tmp_path / "crash.sqlite3"

    async def go() -> tuple[HistoryRun, LiveObserver]:
        worker = MarketWorker(path)
        await worker.open()
        watcher = observer(worker, Said())
        try:
            return await ride(watcher), watcher
        finally:
            await worker.close()

    run, watcher = drive(go)
    found = stored(path)
    assert_whole(found, run, watcher)
    assert not found.finished, "ход без закрытия записан закрытым"


def test_the_mark_holds_the_bounds_of_the_first_pass(drive, tmp_path: pathlib.Path) -> None:
    """Отметка — начало первого бара и закрытие последнего бара первого прохода.

    Границы берутся из данных, а не из часов машины. Это сведение о первом
    проходе, а не граница живого: вживую ли решена строка, говорит её фаза
    (`tests/test_app_paper_phase.py`).
    """
    path = tmp_path / "mark.sqlite3"

    async def go() -> None:
        async with MarketWorker(path) as worker:
            await ride(observer(worker, Said()), until=WARM + 3)

    drive(go)
    found = stored(path).mark
    assert found is not None, "отметки нет — ход читался бы как незаписанный"
    assert found.kept_since == SERIES[0].time
    assert found.live_from == close_time(SERIES[WARM - 1])


def test_the_mark_is_written_on_the_first_pass_even_without_a_row(
    drive, tmp_path: pathlib.Path
) -> None:
    """Первый проход ставит отметку и тогда, когда строк нет ни одной.

    Иначе «ход без сделок» и «сделки не записывались» в базе неразличимы.
    """
    path = tmp_path / "empty.sqlite3"
    unsettled = dataclasses.replace(SERIES[0], unsettled=True)

    async def go() -> None:
        async with MarketWorker(path) as worker:
            watcher = observer(worker, Said())
            await watcher.start()
            await watcher.feed([unsettled])

    drive(go)
    found = stored(path)
    assert found.mark is not None, "первый проход без строк не оставил отметки"
    assert (found.decisions, found.submitted, found.trades) == (0, 0, [])


def test_a_run_closed_before_its_first_pass_reports_no_loss(
    drive, tmp_path: pathlib.Path
) -> None:
    """Ход, закрытый до конца первого прохода, потерь не объявляет и отметку ставит.

    Граница живой части у него — то, что он успел разобрать: живых решений
    не было, и ERROR «строки потеряны» здесь был бы ложной тревогой.
    """
    path = tmp_path / "early.sqlite3"
    said = Said()

    async def go() -> None:
        async with MarketWorker(path) as worker:
            watcher = observer(worker, said)
            await watcher.start()
            await watcher.aclose()

    drive(go)
    found = stored(path)
    assert said.about(LOST) == [], said.events()
    assert found.finished and found.mark is not None


def test_the_rest_is_written_before_the_run_is_closed(drive, tmp_path: pathlib.Path) -> None:
    """Остаток, не легший по ходу, дописывается в `aclose` до закрытия прогона.

    Закрытый прогон дозаписи не принимает, так что обратный порядок терял
    бы остаток при каждом выключении потока.
    """
    path = tmp_path / "rest.sqlite3"

    async def go() -> tuple[HistoryRun, LiveObserver]:
        worker = FlakyWorker(path)
        await worker.open()
        watcher = observer(worker, Said())
        try:
            await watcher.start()
            await watcher.feed(SERIES[:WARM])
            worker.failures = 10**6  # дальше база отказывает до самого закрытия
            for count in range(WARM + 1, len(SERIES) + 1):
                await watcher.feed(SERIES[:count])
            run = watcher.run
            worker.failures = 0  # а на закрытии принимает
            await watcher.aclose()
            return run, watcher
        finally:
            await worker.close()

    run, watcher = drive(go)
    found = stored(path)
    assert found.finished, "ход закрыт, а прогон остался открытым"
    assert_whole(found, run, watcher)


def test_two_passes_in_a_row_write_no_duplicates(drive, tmp_path: pathlib.Path) -> None:
    """Повтор отрезка и проход без новых баров не дописывают строк второй раз."""
    path = tmp_path / "twice.sqlite3"

    async def go() -> tuple[HistoryRun, LiveObserver]:
        async with MarketWorker(path) as worker:
            watcher = observer(worker, Said())
            run = await ride(watcher)
            run = await watcher.feed(SERIES)
            run = await watcher.feed(SERIES)
            await watcher.aclose()
            return run, watcher

    run, watcher = drive(go)
    assert_whole(stored(path), run, watcher)


def test_a_cancelled_pass_does_not_get_its_batch_written_twice(
    drive, tmp_path: pathlib.Path
) -> None:
    """Проход снят посреди записи — закрытие хода её дожидается и не повторяет.

    Отменённый `await` работу в потоке базы не отменяет: пачка ложится,
    а снятый проход уже не узнает об этом. Закрытие хода, посчитавшее
    остаток раньше конца этой записи, записало бы ту же пачку второй раз —
    строки решений и заявки естественного ключа не имеют.
    """
    path = tmp_path / "cancel.sqlite3"

    async def go() -> tuple[HistoryRun, LiveObserver]:
        worker = SlowWorker(path)
        await worker.open()
        watcher = observer(worker, Said())
        try:
            await watcher.start()
            await watcher.feed(SERIES[:WARM])
            worker.slow = True
            passing = asyncio.ensure_future(watcher.feed(SERIES))
            while not worker.entered.is_set():
                await asyncio.sleep(0.01)
            passing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await passing
            run = watcher.run
            asyncio.get_running_loop().call_later(0.2, worker.gate.set)
            await watcher.aclose()
            return run, watcher
        finally:
            worker.gate.set()
            await worker.close()

    run, watcher = drive(go)
    found = stored(path)
    assert found.finished
    assert_whole(found, run, watcher)


def test_a_pass_and_the_closing_waiting_on_one_write_write_the_rest_once(
    drive, tmp_path: pathlib.Path
) -> None:
    """Проход и закрытие хода, ждущие одну запись в полёте, остаток пишут один раз.

    Снятый проход оставил запись в потоке базы; следующий проход и закрытие
    хода ждут её оба. Дождавшись, первый отправляет остаток, а второй
    без замка посчитал бы тот же остаток по ещё не сдвинутым курсорам
    и отправил второй раз — решения и заявки естественного ключа не имеют.
    Порядок пробуждения задан циклом событий: кто раньше встал ждать,
    тот раньше и проснулся.
    """
    path = tmp_path / "two-waiters.sqlite3"

    async def go() -> tuple[HistoryRun, LiveObserver, int]:
        worker = SlowWorker(path)
        await worker.open()
        watcher = observer(worker, Said())
        try:
            await watcher.start()
            await watcher.feed(SERIES[:WARM])
            worker.slow = True
            cancelled = asyncio.ensure_future(watcher.feed(SERIES[: WARM + 2]))
            while not worker.entered.is_set():
                await asyncio.sleep(0.01)
            cancelled.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled
            in_flight = len(watcher.run.journal)
            passing = asyncio.ensure_future(watcher.feed(SERIES))
            while watcher.run.bars < len(SERIES):
                await asyncio.sleep(0)
            closing = asyncio.ensure_future(watcher.aclose())
            await asyncio.sleep(0)
            worker.gate.set()
            await asyncio.gather(passing, closing)
            return watcher.run, watcher, in_flight
        finally:
            worker.gate.set()
            await worker.close()

    run, watcher, in_flight = drive(go)
    assert len(run.journal) > in_flight, "после записи в полёте строк не было — остатка нет"
    found = stored(path)
    assert found.finished
    assert_whole(found, run, watcher)


def test_a_run_closed_in_the_middle_of_a_pass_reads_no_more_bars(
    drive, tmp_path: pathlib.Path
) -> None:
    """Ход, закрытый посреди прохода, следующих баров не разбирает: окно и база совпадают.

    Закрытие хода считает остаток и закрывает прогон, пока проход стоит
    на передышке ленты (`BREATHE_EVERY`). Проход, продолживший разбор после
    этого, показал бы окну решения и сделки, которых в базе нет и уже
    не будет: закрытый прогон дозаписи не принимает.
    """
    path = tmp_path / "stopped.sqlite3"
    series = bars(BREATHE_EVERY + 60)

    async def go() -> tuple[HistoryRun, LiveObserver]:
        async with MarketWorker(path) as worker:
            watcher = observer(worker, Said())
            await watcher.start()
            passing = asyncio.ensure_future(watcher.feed(series))
            closing = asyncio.ensure_future(watcher.aclose())
            await asyncio.gather(passing, closing)
            return watcher.run, watcher

    run, watcher = drive(go)
    assert run.bars == BREATHE_EVERY, (
        f"после закрытия хода разобрано баров {run.bars - BREATHE_EVERY}"
    )
    found = stored(path)
    assert found.finished
    assert found.decisions == len(run.journal), "в окне решения, которых нет в базе"
    assert found.trades == expected_trades(run)
    assert found.submitted == watcher.submitted


def test_every_take_level_lies_in_the_base_with_its_arming_and_its_hit(
    drive, tmp_path: pathlib.Path
) -> None:
    """Уровень тейка записан у вооружения и у касания, и это один и тот же уровень.

    Уровни приходят двумя путями: у вооружения — из заявки движка, у касания —
    из расчёта исполнителя. Разбор сделки показывает уровень касания, а
    вооружение хранит, где тейк стоял на каждом баре (ответ владельца В4);
    потерянный при записи уровень вооружения разбор сделки не выдал бы.
    """
    path = tmp_path / "levels.sqlite3"

    async def go() -> None:
        async with MarketWorker(path) as worker:
            await ride(observer(worker, Said()))

    drive(go)
    with CandleStore(path) as store:
        (run,) = store.paper_runs([SYMBOL])
        orders = [one.record for one in store.orders(session_id=run.session.id)]
    hits = [one for one in orders if one.kind is OrderKind.LEVEL_HIT]
    assert hits, "в ряду ни одного касания уровня — сверять нечего"
    for hit in hits:
        armed = [
            one for one in orders
            if one.order_id == hit.order_id and one.kind is OrderKind.SUBMITTED
            and one.at <= hit.at
        ]
        assert armed, f"касание {hit.order_id} без вооружения в базе"
        assert hit.level is not None and armed[-1].level == hit.level, (
            f"{hit.order_id}: уровень вооружения {armed[-1].level}, касания {hit.level}"
        )


# ------------------------------------------------------------------ отказы вслух


def test_a_refused_write_is_said_once_per_series_and_so_is_the_recovery(
    drive, tmp_path: pathlib.Path
) -> None:
    """Серия отказов — одна строка WARNING; восстановление — одна строка; дублей нет."""
    path = tmp_path / "flaky.sqlite3"
    said = Said()

    async def go() -> tuple[HistoryRun, LiveObserver, int]:
        worker = FlakyWorker(path, failures=4)
        await worker.open()
        watcher = observer(worker, said)
        try:
            run = await ride(watcher)
            await watcher.aclose()
            return run, watcher, worker.attempts
        finally:
            await worker.close()

    run, watcher, attempts = drive(go)
    assert attempts >= 5, "запись не повторялась — серии отказов не было"
    failed = said.about(FAILED)
    assert len(failed) == 1, f"об отказах сказано {len(failed)} раз вместо одного"
    assert failed[0][2] is DecisionLevel.WARNING
    assert "database is locked" in failed[0][1], "причина отказа не названа"
    assert len(said.about(RECOVERED)) == 1, "о восстановлении записи не сказано"
    assert_whole(stored(path), run, watcher)


def test_a_refused_write_never_halts_the_robot(drive, tmp_path: pathlib.Path) -> None:
    """Отказ записи не останавливает робота: запись — свидетельство, не торговля."""
    path = tmp_path / "refused.sqlite3"

    async def go() -> HistoryRun:
        worker = FlakyWorker(path, failures=10**6)
        await worker.open()
        watcher = observer(worker, Said())
        try:
            return await ride(watcher)
        finally:
            await worker.close()

    run = drive(go)
    assert run.halted == "", f"отказ записи остановил робота: {run.halted}"
    assert run.deals, "ход перестал торговать после отказа записи"


def test_a_loss_at_closing_is_said_logged_and_noted(
    drive, tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Потеря при закрытии хода — ERROR в окно, в технический лог и в итог прогона.

    Окно к этому моменту обычно уже закрыто, поэтому строки в журнале окна
    мало: её обязан повторить технический лог, а заметка о конце прогона —
    назвать число потерянного.
    """
    path = tmp_path / "lost.sqlite3"
    said = Said()

    async def go() -> HistoryRun:
        worker = FlakyWorker(path, failures=10**6)
        await worker.open()
        watcher = observer(worker, said)
        try:
            run = await ride(watcher)
            await watcher.aclose()
            return run
        finally:
            await worker.close()

    with caplog.at_level(logging.ERROR, logger="app.paper_log"):
        run = drive(go)
    lost = said.about(LOST)
    assert len(lost) == 1 and lost[0][2] is DecisionLevel.ERROR, said.events()
    assert f"сделок {len(run.deals)}" in lost[0][1], "число потерянных сделок не названо"
    assert any(
        record.levelno == logging.ERROR and "потеряны" in record.getMessage()
        for record in caplog.records
    ), "потеря не повторена в техническом логе"
    with CandleStore(path) as store:
        (paper,) = store.paper_runs([SYMBOL])
    assert paper.mark is None
    note = paper.session.finish_note
    assert "потеряны" in note, "потеря не названа в итоге прогона"
    # Число — из хода, а не из фразы отказа: через месяц по заметке ищут,
    # сколько сделок не хватает в базе, и слово «потеряны» без числа
    # на этот вопрос не отвечает.
    assert f"сделок {len(run.deals)}" in note, f"в итоге прогона не названо число: {note}"


def test_a_run_that_did_not_open_writes_no_rows_and_says_so_in_its_own_words(
    drive, tmp_path: pathlib.Path
) -> None:
    """Прогон симуляции не открылся — строк нет, и сказано это про симуляцию.

    Общая фраза «восстановить настройки будет нечем» не называла бы главного:
    сделки этого хода после перезапуска не открыть.
    """
    path = tmp_path / "closed.sqlite3"
    said = Said()

    class NoRuns(MarketWorker):
        async def open_journal_session(self, *args: object, **kwargs: object):
            raise sqlite3.OperationalError("disk I/O error")

    async def go() -> int:
        worker = NoRuns(path)
        await worker.open()
        watcher = observer(worker, said)
        try:
            await ride(watcher, until=WARM + 2)
            await watcher.aclose()
            stats = await worker.journal_stats()
            return stats.decisions + stats.orders + stats.trades + stats.run_rows
        finally:
            await worker.close()

    assert drive(go) == 0
    events = said.events()
    assert "Симуляция не записывается" in events, events
    assert "Прогон не записан" not in events
