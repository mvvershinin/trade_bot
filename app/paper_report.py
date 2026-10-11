"""Сделки симуляции из базы: «как было» и «пересчёт с утра». Только отбор строк.

Миниплан — `.docs/plans/reports-window-step2-paper-trades.md`, §3–4.

Чего здесь нет — и это главное
------------------------------
Торговой логики. Движок один (правило 1): прогон симуляции пересчитывает
историю при старте и дальше идёт вживую **тем же** движком. Оба отбора
берут готовые строки из базы и различаются только тем, **какие** строки
показать. Ни одна сделка здесь не считается заново, ни одна цена
не выводится.

Связь сделки с её строками (§3)
-------------------------------
Сделка ссылается на заявки по имени: `entry_order_id` и `exit_order_id`.

* **Заявка входа** — последняя поданная заявка с именем входа не позже входа.
  Исполнение идёт по открытию бара, следующего за решением, поэтому время
  заявки — момент решения, а время сделки — исполнения.
* **Заявка выхода** — последняя поданная заявка с именем выхода не позже
  выхода. У выхода по уровню это вооружение тейка: оно повторяется на каждом
  баре под тем же именем, и последнее до выхода — тот уровень, что стоял.
* **Касание уровня** — строка `level_hit` с именем выхода. Время у неё —
  закрытие бара касания, и оно **позже** времени сделки: сделка помечена
  моментом внутри бара. Поэтому касание ищется не «до выхода», а ближайшее
  к нему среди касаний этой позиции.
* **Строки журнала решений** — все строки бара решения, по `(прогон, время)`.
  Привязки по имени заявки у строк журнала нет (правка `engine/`, вне шага).

Решение выхода — касание, если оно было, иначе заявка выхода; решение
входа — заявка входа. По ним и только по ним сделка делится на «вживую»,
«догон» и «пересчёт»: у каждой строки заявки своя фаза (`DecisionPhase`),
поставленная ходом по тому, как пришёл бар. Ни время сделки, ни время
решения в этом не участвуют: граница во времени объявляла живым утро,
разобранное одной пачкой догрузки после выходных (находка ревью).
Строки решения нет — пересчёт: безопасная сторона.

Отборы (§4)
-----------
* **«Как было»** — сделки, решение выхода которых живое. Вход из пересчёта —
  пометка «вход из пересчёта» (ответ владельца В2), вход на догоне —
  «вход на догоне». Сделка, открытая вживую и закрытая на догоне, — тоже
  строка с пометкой «выход на догоне» и в итоге (ответ владельца 12.10).
  Сделка без единого живого решения, закрытая на догоне, в таблицу не идёт,
  а называется окну числом (`caught_up`, правило 13). Куда идёт сделка
  каждой фазы — таблица `_AS_WAS` на все фазы: ни одна не выпадает молча.
* **«Пересчёт с утра»** — для дня D (МСК) сделки с выходом в D у последнего
  прогона, **шедшего в D**: начат не позже конца D, кончился не раньше
  начала D. Конец — время закрытия; у прогона, закрытого аварийно, —
  наибольшее время его строк, а без строк — время старта. Последний —
  по старту, равные — по номеру. Дни до старта прогона он не берёт: их
  пересчёт хранится свидетельством и связкой для «входа из пересчёта».

Дни считаются по МСК: машина владельца стоит в Новосибирске (+7), база
отдаёт моменты в МСК, а прогон, начатый в 00:30 МСК, — прогон своего дня,
а не вчерашнего (в UTC это ещё вчера).
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime

from app import convert
from market import (
    DecisionPhase,
    DecisionRecord,
    JournalSession,
    OrderKind,
    OrderRecord,
    PaperRun,
    StoredTrade,
    TradeRecord,
    ensure_msk,
)
from ui.trade_story import (
    PaperReport,
    PaperRunSpan,
    PaperTrades,
    Phase,
    TradeStory,
    day_list,
)

__all__ = [
    "Lot",
    "Picked",
    "RunBook",
    "RunRows",
    "as_was",
    "caught_up",
    "decided",
    "lot_of",
    "links",
    "msk_day",
    "needed",
    "phase_of",
    "phases",
    "last_runs",
    "recount",
    "report",
    "run_days",
    "story_of",
]


def msk_day(moment: datetime) -> date:
    """День момента по МСК. Наивное время — отказ (`market.ensure_msk`)."""
    return ensure_msk(moment).date()


# ------------------------------------------------------------------ связь


@dataclass(frozen=True, slots=True)
class RunBook:
    """Строки одного прогона, разложенные для разбора: заявки по имени, решения по бару.

    Собирается одинаково из памяти хода и из базы (`RunBook.of`): на входе
    записи (`OrderRecord`, `DecisionRecord`), а не прочитанные строки, —
    поэтому разбор сделки из базы обязан совпасть с разбором из памяти
    строка в строку.
    """

    session: JournalSession
    orders: Mapping[str, tuple[OrderRecord, ...]] = field(default_factory=dict)
    bars: Mapping[datetime, tuple[DecisionRecord, ...]] = field(default_factory=dict)

    @classmethod
    def of(
        cls,
        session: JournalSession,
        orders: Iterable[OrderRecord],
        decisions: Iterable[DecisionRecord] = (),
    ) -> RunBook:
        """Разложить строки прогона. Порядок внутри имени и бара — порядок записи."""
        named: dict[str, list[OrderRecord]] = {}
        for order in orders:
            named.setdefault(order.order_id, []).append(order)
        bars: dict[datetime, list[DecisionRecord]] = {}
        for row in decisions:
            bars.setdefault(row.at, []).append(row)
        return cls(
            session=session,
            orders={name: tuple(rows) for name, rows in named.items()},
            bars={at: tuple(rows) for at, rows in bars.items()},
        )


def _last_submitted(rows: Sequence[OrderRecord], until: datetime) -> OrderRecord | None:
    """Последняя поданная заявка не позже `until`. Строки — в порядке записи."""
    found = None
    for row in rows:
        if row.kind is OrderKind.SUBMITTED and row.at <= until:
            found = row
    return found


def _hit(rows: Sequence[OrderRecord], trade: TradeRecord) -> OrderRecord | None:
    """Касание уровня этой позиции: среди касаний после входа — ближайшее к выходу."""
    hits = [
        row for row in rows if row.kind is OrderKind.LEVEL_HIT and row.at >= trade.entry_time
    ]
    if not hits:
        return None
    return min(hits, key=lambda row: abs((row.at - trade.exit_time).total_seconds()))


def links(trade: TradeRecord, book: RunBook) -> convert.TradeLinks:
    """Строки прогона, принадлежащие сделке (шапка модуля, «Связь»)."""
    entry = _last_submitted(book.orders.get(trade.entry_order_id, ()), trade.entry_time)
    named_exit = book.orders.get(trade.exit_order_id, ())
    exit_order = _last_submitted(named_exit, trade.exit_time)
    hit = _hit(named_exit, trade)
    entry_at, exit_at = decided(trade, entry, exit_order, hit)
    return convert.TradeLinks(
        entry=entry,
        exit_order=exit_order,
        hit=hit,
        entry_rows=book.bars.get(entry_at, ()) if entry is not None else (),
        exit_rows=book.bars.get(exit_at, ()) if hit or exit_order else (),
    )


def decided(
    trade: TradeRecord,
    entry: OrderRecord | None,
    exit_order: OrderRecord | None,
    hit: OrderRecord | None,
) -> tuple[datetime, datetime]:
    """Моменты решений входа и выхода. Строки нет — время самой сделки.

    Время сделки вместо решения — запасной ответ, а не правило: заявок
    без отметки прогона не бывает, а прогон с отметкой пишет все. Разбор
    такой сделки говорит словами, что заявки в базе нет.
    """
    entry_at = entry.at if entry is not None else trade.entry_time
    if hit is not None:
        return entry_at, hit.at
    return entry_at, exit_order.at if exit_order is not None else trade.exit_time


_LIVE, _CATCH_UP, _RECOUNT = DecisionPhase.LIVE, DecisionPhase.CATCH_UP, DecisionPhase.RECOUNT

#: Фаза сделки по фазам решений входа и выхода — **таблицей на все девять
#: сочетаний** (правило 9), полноту сверяет тест. Три сочетания с выходом
#: раньше входа по ленте не бывают (пересчёт идёт первым) и отданы
#: пересчёту — безопасной стороне: в «как было» они не попадут.
_PHASES: dict[tuple[DecisionPhase, DecisionPhase], Phase] = {
    (_LIVE, _LIVE): Phase.LIVE,
    (_RECOUNT, _LIVE): Phase.ENTRY_RECOUNTED,
    (_CATCH_UP, _LIVE): Phase.ENTRY_CAUGHT_UP,
    (_LIVE, _CATCH_UP): Phase.EXIT_CAUGHT_UP,
    (_RECOUNT, _CATCH_UP): Phase.CAUGHT_UP,
    (_CATCH_UP, _CATCH_UP): Phase.CAUGHT_UP,
    (_RECOUNT, _RECOUNT): Phase.RECOUNTED,
    (_CATCH_UP, _RECOUNT): Phase.RECOUNTED,
    (_LIVE, _RECOUNT): Phase.RECOUNTED,
}


def phases(found: convert.TradeLinks) -> tuple[DecisionPhase, DecisionPhase]:
    """Фазы решений входа и выхода — у строк заявок. Строки нет — пересчёт.

    Пересчёт вместо пропавшей строки — безопасная сторона, а не правило:
    заявок без отметки прогона не бывает, а прогон с отметкой пишет все.
    """
    entry = found.entry.phase if found.entry is not None else _RECOUNT
    decision = found.hit or found.exit_order
    return entry, decision.phase if decision is not None else _RECOUNT


def phase_of(entry: DecisionPhase, exit_: DecisionPhase) -> Phase:
    """Фаза сделки по фазам её решений (`_PHASES`)."""
    return _PHASES[(entry, exit_)]


def story_of(trade: TradeRecord, book: RunBook) -> TradeStory:
    """Разбор одной сделки — **одна функция** для памяти хода и для базы."""
    found = links(trade, book)
    return convert.trade_story(
        trade, found, session=book.session, phase=phase_of(*phases(found))
    )


# ------------------------------------------------------------------ отборы


@dataclass(frozen=True, slots=True)
class RunRows:
    """Прочитанное по одному прогону симуляции: прогон, его сделки и заявки.

    У прогона без отметки (`run.mark is None`) строк нет и не читаются.
    """

    run: PaperRun
    trades: tuple[StoredTrade, ...] = ()
    orders: tuple[OrderRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class Picked:
    """Сделка, попавшая в отбор, — с прогоном и тем, вживую ли она решена."""

    run: PaperRun
    trade: StoredTrade
    phase: Phase


def _book(rows: RunRows) -> RunBook:
    return RunBook.of(rows.run.session, rows.orders)


def _decided(runs: Sequence[RunRows]) -> list[Picked]:
    """Каждая сделка записавших прогонов — с фазой по фазам её решений."""
    found: list[Picked] = []
    for rows in runs:
        if rows.run.mark is None:
            continue
        book = _book(rows)
        for trade in rows.trades:
            found.append(Picked(rows.run, trade, phase_of(*phases(links(trade.record, book)))))
    return found


def _in_order(picked: Iterable[Picked]) -> list[Picked]:
    """По выходу, затем по входу и номеру прогона — порядок таблицы."""
    return sorted(
        picked,
        key=lambda one: (one.trade.exit_time, one.trade.entry_time, one.run.session.id),
    )


class Lot(enum.Enum):
    """Куда сделка идёт в «как было»."""

    #: Строкой в таблицу и в итог.
    SHOWN = "shown"
    #: Числом над таблицей: ни одного живого решения, выход решён на догоне.
    COUNTED = "counted"
    #: Никуда: сделка пересчёта, об этом говорит подпись окна.
    RECOUNTED = "recounted"


#: Фаза сделки → её место в «как было» — **на все фазы** (правило 9), полноту
#: сверяет тест: новая фаза без места падает `KeyError` на первой сделке,
#: а не пропадает из окна молча (правило 13). Ответы владельца: В2 (11.10) —
#: вход из пересчёта в таблице; 12.10 — выход на догоне в таблице и в итоге,
#: сделка целиком на догоне — числом.
_AS_WAS: dict[Phase, Lot] = {
    Phase.LIVE: Lot.SHOWN,
    Phase.ENTRY_RECOUNTED: Lot.SHOWN,
    Phase.ENTRY_CAUGHT_UP: Lot.SHOWN,
    Phase.EXIT_CAUGHT_UP: Lot.SHOWN,
    Phase.CAUGHT_UP: Lot.COUNTED,
    Phase.RECOUNTED: Lot.RECOUNTED,
}


def lot_of(phase: Phase) -> Lot:
    """Место сделки этой фазы в «как было» (`_AS_WAS`)."""
    return _AS_WAS[phase]


def _lot(runs: Sequence[RunRows], lot: Lot) -> list[Picked]:
    return _in_order(one for one in _decided(runs) if lot_of(one.phase) is lot)


def as_was(runs: Sequence[RunRows]) -> list[Picked]:
    """«Как было»: сделки, у которых живое решение выхода или живой вход при выходе на догоне."""
    return _lot(runs, Lot.SHOWN)


def caught_up(runs: Sequence[RunRows]) -> list[Picked]:
    """Решённые на догоне без единого живого решения: в «как было» их нет, окно называет число.

    Так бывает, когда программа была закрыта или поток обрывался: и вход,
    и выход решил пересчёт истории или разбор пропущенных свечей после
    подключения, а выход — именно разбор. Молчать о них нельзя (правило 13).
    """
    return _lot(runs, Lot.COUNTED)


def run_days(run: PaperRun) -> tuple[date, date]:
    """С какого по какой день МСК шёл прогон.

    Конец — время закрытия; у аварийного прогона — наибольшее время его строк,
    без строк — старт. Строки пересчёта старше старта, поэтому конец
    не раньше старта.
    """
    session = run.session
    end = session.finished_at or run.last_at or session.started_at
    return msk_day(session.started_at), msk_day(max(end, session.started_at))


def last_runs(runs: Iterable[PaperRun]) -> dict[date, PaperRun]:
    """День МСК → последний прогон, шедший в этот день. По старту, равные — по номеру."""
    chosen: dict[date, PaperRun] = {}
    for run in sorted(runs, key=lambda one: (one.session.started_at, one.session.id)):
        first, last = run_days(run)
        for day in day_list(first, last):
            chosen[day] = run
    return chosen


def recount(runs: Sequence[RunRows]) -> tuple[list[Picked], dict[date, PaperRun]]:
    """«Пересчёт с утра»: за день — сделки с выходом в этот день у его последнего прогона.

    Второе в ответе — какие дни чьи: по нему окно называет дни без прогона.
    """
    days = last_runs(rows.run for rows in runs)
    picked: list[Picked] = []
    for one in _decided(runs):
        owner = days.get(msk_day(one.trade.exit_time))
        if owner is not None and owner.session.id == one.run.session.id:
            picked.append(one)
    return _in_order(picked), days


# ------------------------------------------------------------------ ответ окну


def _trades(picked: Sequence[Picked], books: Mapping[int, RunBook]) -> PaperTrades:
    """Отбор → строки таблицы и разборы. Разбор — по книге прогона с решениями."""
    return PaperTrades(
        rows=tuple(convert.row_of_stored_trade(one.trade) for one in picked),
        stories=tuple(
            story_of(one.trade.record, books[one.run.session.id]) for one in picked
        ),
    )


def needed(runs: Sequence[RunRows]) -> list[int]:
    """Номера прогонов, чьи сделки попали хоть в один отбор, — им нужен журнал решений.

    Журнал читается только им: у прогона с месяцем пересчёта в журнале
    тысячи строк, а в отбор от него может не попасть ни одной сделки.
    """
    picked = [*as_was(runs), *recount(runs)[0]]
    return sorted({one.run.session.id for one in picked})


def report(
    instrument: str,
    runs: Sequence[RunRows],
    books: Mapping[int, RunBook],
    notes: Sequence[str] = (),
) -> PaperReport:
    """Ответ окну: оба отбора с разборами, прогоны днями и оговорки.

    `books` — книги прогонов **с журналом решений**, по номеру прогона;
    нужны все из `needed(runs)`.
    """
    picked, days = recount(runs)
    spans = [(rows.run, run_days(rows.run)) for rows in runs]
    return PaperReport(
        instrument=instrument,
        codes=tuple(sorted({rows.run.session.symbol for rows in runs})),
        runs=tuple(
            PaperRunSpan(
                run_id=run.session.id,
                first_day=first,
                last_day=last,
                recorded=run.mark is not None,
            )
            for run, (first, last) in spans
        ),
        as_was=_trades(as_was(runs), books),
        caught_up=tuple(msk_day(one.trade.exit_time) for one in caught_up(runs)),
        recount=_trades(picked, books),
        covered=tuple(sorted((day, run.session.id) for day, run in days.items())),
        notes=tuple(notes),
    )
