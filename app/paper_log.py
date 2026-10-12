"""Строки живого хода симуляции — в базу: пачками, без потерь и без дублей.

Зачем
-----
Живой ход писал в базу одну строку прогона (`D-027`): сделки, заявки
и решения жили в памяти окна и пропадали с перезапуском или «Применить».
Здесь они ложатся в базу в конце каждого прохода (`LiveObserver.feed`)
и остатком при закрытии хода (`LiveObserver.aclose`). Миниплан —
`.docs/plans/reports-window-step2-paper-trades.md`.

Единица работы с курсорами (правило 9 проекта)
----------------------------------------------
Ход копит три списка: строки журнала движка, заявки с касаниями уровней
и закрытые сделки. У каждого свой курсор — сколько из него уже в базе —
плюс признак «отметка записана». Пачка — то, что за курсорами; она уходит
одной транзакцией (`CandleStore.write_paper_rows`), и курсоры сдвигаются
только после её фиксации. Отказ записи курсоров не трогает: следующий проход
повторит ту же пачку вместе с новым.

⚠️ Одна запись в полёте, и это не осторожность, а правка дефекта гейта
-----------------------------------------------------------------------
Отменённый `await` не отменяет работу в потоке базы (`market/worker.py`,
шапка). А проход и закрытие хода — разные задачи (`app/port.py::_observe`
ставит закрытие задачей, `aclose` снимает проход). Без порядка выходило так:
проход отправил пачку и снят на ожидании; пачка в потоке легла, а курсоры
не сдвинуты; закрытие хода считает пачку заново и пишет её второй раз.
Решения и заявки естественного ключа не имеют, база дубль не узнает.

Поэтому три вещи сразу:

* **замок записи** — пачку считает и отправляет один писатель за раз;
* **запись в полёте дожидается** — следующий писатель сперва ждёт её
  (`asyncio.shield`: ожидание можно отменить, саму запись — нет);
* **курсоры сдвигаются в потоке базы**, сразу после фиксации транзакции
  (`MarketWorker.write_paper_rows(on_written=…)`, приём `_open_store`):
  успех записи не теряется, даже если ждать её стало некому.

Отказ записи не роняет ход и не останавливает робота
----------------------------------------------------
Запись — свидетельство о ходе, а не часть торговли: график, решения и
сделки в окне от неё не зависят. Поэтому отказ не уходит в `run.halted`
и не прекращает ход. Но и не молчит (правило 13): одна строка WARNING
на серию отказов, строка о восстановлении, а отказ при закрытии хода —
ERROR с числом потерянного, то же в технический лог (окно к этому моменту
уже закрыто, `app/main.py`) и в заметку о конце прогона.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from app import convert
from backtest import Deal
from engine import JournalEntry
from market import MarketWorker, OrderRecord, PaperRows, PaperWritten, RunRowsMark
from ui.models import DecisionLevel

__all__ = ["PaperLog", "PaperSource"]

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PaperSource:
    """Что копит ход: три списка **по ссылке** и инструмент для сделок.

    Списки живые — ход дописывает в них на каждом баре, писатель только
    читает срезы за курсорами. Копия здесь означала бы вторую память о тех же
    строках, расходящуюся с первой молча.
    """

    journal: Sequence[JournalEntry]
    orders: Sequence[OrderRecord]
    deals: Sequence[Deal]
    symbol: str


@dataclass(slots=True)
class _Cursor:
    """Сколько из каждого списка уже в базе, и легла ли отметка."""

    journal: int = 0
    orders: int = 0
    deals: int = 0
    marked: bool = False


@dataclass(frozen=True, slots=True)
class _Batch:
    """Пачка и положение курсоров после её фиксации."""

    rows: PaperRows
    journal: int
    orders: int
    deals: int


class PaperLog:
    """Писатель строк одного прогона симуляции. Создаётся, когда прогон открыт.

    :param worker: поток данных; база пишется только через него.
    :param session_id: номер открытого прогона (`RunEntry.id`).
    :param source: что копит ход (`PaperSource`).
    :param say: строка в журнал решений окна — событие, причина, важность.
    """

    def __init__(
        self,
        worker: MarketWorker,
        session_id: int,
        source: PaperSource,
        say: Callable[[str, str, DecisionLevel], None],
    ) -> None:
        self._worker = worker
        self._session_id = session_id
        self._source = source
        self._say = say
        self._cursor = _Cursor()
        self._lock = asyncio.Lock()
        self._flight: asyncio.Task[None] | None = None
        #: Текст последнего отказа. Пусто — отказов не было.
        self._error = ""
        #: Идёт ли серия отказов: о ней уже сказано, и до восстановления
        #: повторять нельзя — иначе строка на каждом баре похоронит под собой
        #: решения робота.
        self._failing = False
        self._closed = False

    async def flush(self, mark: RunRowsMark | None) -> None:
        """Дописать накопленное. Отказ сказан строкой, наружу не поднимается.

        `mark` — отметка хода: границы пересчёта (`RunRowsMark`). `None` —
        первый проход ещё не кончился, и строки ждут: без отметки база их
        не примет.
        """
        async with self._lock:
            await self._landed()
            if self._closed:
                return
            batch = self._batch(mark)
            if batch is None:
                return
            self._flight = asyncio.ensure_future(self._send(batch, closing=False))
            await asyncio.shield(self._flight)

    async def close(self, mark: RunRowsMark | None) -> str:
        """Дописать остаток и больше не писать. Отвечает, что потеряно.

        Пустой ответ — всё на месте. Иначе — фраза о потере: она уходит
        в заметку о конце прогона, строкой ERROR в окно и в технический лог.
        """
        async with self._lock:
            await self._landed()
            batch = self._batch(mark)
            if batch is not None:
                self._flight = asyncio.ensure_future(self._send(batch, closing=True))
                await asyncio.shield(self._flight)
            self._closed = True
            return self._say_loss(mark)

    # ---------------------------------------------------------- внутреннее

    async def _landed(self) -> None:
        """Дождаться записи в полёте. Ожидание отменить можно, запись — нет."""
        flight = self._flight
        if flight is not None and not flight.done():
            await asyncio.shield(flight)

    def _batch(self, mark: RunRowsMark | None) -> _Batch | None:
        """Пачка за курсорами. `None` — писать нечего или рано."""
        cursor, source = self._cursor, self._source
        fresh_mark = None if cursor.marked else mark
        if not cursor.marked and fresh_mark is None:
            return None
        journal = source.journal[cursor.journal:]
        orders = source.orders[cursor.orders:]
        deals = source.deals[cursor.deals:]
        rows = PaperRows(
            mark=fresh_mark,
            decisions=tuple(convert.decision_record(entry) for entry in journal),
            orders=tuple(orders),
            trades=tuple(convert.trade_record(deal, source.symbol) for deal in deals),
        )
        if rows.empty:
            return None
        return _Batch(
            rows=rows,
            journal=cursor.journal + len(journal),
            orders=cursor.orders + len(orders),
            deals=cursor.deals + len(deals),
        )

    def _advance(self, batch: _Batch) -> None:
        """Сдвинуть курсоры за записанную пачку. **Зовётся в потоке базы.**

        Сразу после фиксации транзакции и до ответа ждущему: так успех
        записи переживает отмену ожидания (`MarketWorker.write_paper_rows`).
        Читает курсоры только писатель под замком и только после того, как
        запись в полёте кончилась, — гонки с потоком окна нет.
        """
        self._cursor.journal = batch.journal
        self._cursor.orders = batch.orders
        self._cursor.deals = batch.deals
        if batch.rows.mark is not None:
            self._cursor.marked = True

    async def _send(self, batch: _Batch, *, closing: bool) -> None:
        """Одна пачка — одна транзакция. Отказ запоминается и говорится."""
        try:
            written = await self._worker.write_paper_rows(
                self._session_id, batch.rows, on_written=lambda _done: self._advance(batch)
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log.warning(  # мимо ленты «Отладка»: в ней — строка журнала из `_say_failure`
                "строки симуляции не записаны в базу", exc_info=True, extra={"panel": False}
            )
            self._error = f"{type(error).__name__}: {error}"
            if not closing:
                self._say_failure()
            return
        self._say_recovery(written)

    def _say_failure(self) -> None:
        """Отказ — одной строкой на серию, а не на каждый бар."""
        if self._failing:
            return
        self._failing = True
        self._say(
            "Сделки симуляции не записаны в базу",
            f"Запись отказала: {self._error}. Ход идёт, сделки и решения "
            "видны в окне; запись повторится на следующем баре вместе "
            "с новыми строками. Пока база отказывает, после перезапуска "
            "программы этих сделок не открыть.",
            DecisionLevel.WARNING,
        )

    def _say_recovery(self, written: PaperWritten) -> None:
        """Серия отказов кончилась — сказать, что дописано всё."""
        if not self._failing:
            return
        self._failing = False
        self._say(
            "Запись симуляции восстановлена",
            f"Дописано строк: {written.rows}, из них сделок {written.trades}. "
            "Пропусков нет: строки, не принятые базой раньше, легли этой пачкой.",
            DecisionLevel.INFO,
        )

    def _say_loss(self, mark: RunRowsMark | None) -> str:
        """Что не легло к закрытию хода — вслух, в лог и в заметку прогона."""
        cursor, source = self._cursor, self._source
        deals = len(source.deals) - cursor.deals
        rows = len(source.journal) - cursor.journal + len(source.orders) - cursor.orders
        unmarked = mark is not None and not cursor.marked
        if not (deals or rows or unmarked):
            return ""
        cause = self._error or "причина неизвестна"
        text = (
            f"Строки симуляции потеряны при закрытии хода: сделок {deals}, "
            f"строк журнала и заявок {rows} не записаны в базу ({cause}). "
            "В окне до закрытия они были видны; после перезапуска их не открыть."
        )
        self._say("Сделки симуляции потеряны", text, DecisionLevel.ERROR)
        log.error("%s", text, extra={"panel": False})  # в ленте — строка журнала выше
        return text
