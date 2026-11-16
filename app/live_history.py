"""Боевые сделки всех заходов — в «Сделки» и на график одним списком (B-100).

Зачем
-----
Каждый заход боя («Включить бой», «Возобновить», остановка) — новый прогон
в журнале, а движок «факта» нового прогона прогревается с нуля в режиме
«Выключен» и прежних сделок не знает. Пока окно показывало только сделки
движка, после любой остановки таблица «Сделки», строка итога и метки графика
пустели — 13.11.2026 при четырёх настоящих сделках за день.

Здесь закрытые боевые сделки из базы (все прогоны `origin = live`, без отбора
по периоду графика — просьба владельца счёта) складываются со сделками
текущего захода. Строки таблицы, итог и метки графика собираются из **одного**
списка: два источника для таблицы и для графика разошлись бы молча.

Прежние заходы отличимы одним приёмом в обоих местах — приглушены
(`TradeRow.earlier_run`, `Marker.earlier_run`, `TradePath.earlier_run`).

Без дублей
----------
Сделка текущего захода лежит и в памяти движка, и в базе: её пишет
`app/paper_log.py` в конце каждого прохода. Из базы берётся только та, которой
нет в памяти, по естественному ключу журнала (`market.storage._TRADE_IDENTITY`
без номера прогона): инструмент, сторона, вход и выход **с точностью до
секунды** — с той, с какой их хранит база. Сравнение до микросекунды не нашло бы
ни одного совпадения, и каждая сделка текущего захода встала бы дважды —
вместе с удвоенной прибылью в итоге.

Время сделки и скользящий у брокера
-----------------------------------
Движок знает о сделке свечу (`engine.Fill.at` — начало бара), брокер — секунду.
В строку таблицы идёт секунда исполнения по журналу команд (`broker_command`
↔ `broker_execution`, связь — имя заявки движка в прогоне): условная, исполненная
в 10:38:03 под свечой 10:35, иначе читалась «10:35 → 10:35» (бой 16.11.2026).
Исполнения нет в журнале — время свечи, как было. Ключ «та же сделка» и база
остаются при времени свечи: подмена — только в показе.

С `D-196` скользящий тейк в бою ведёт брокер условной заявкой. Такой выход
подписан «скользящий тейк у брокера» (`convert.BROKER_TRAILING`), а «прогноз»
(тень боя) ведёт уровень сам, по минуткам и с шагом, — и после выхода брокера
расходится с боем штатно, а не дефектом (`at_broker`, B-104).

Решений здесь нет: числа сделок — записанные, итог считает `backtest.summarise`
тем же счётом, что у любого прогона.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime

from app import convert
from backtest import Deal, HistoryRun, reversal_entries, summarise
from engine import ExitReason, OrderAction, in_moscow
from engine import Side as EngineSide
from market import OrderFillTimes, StoredTrade, Timeframe, floor_to_minute
from ui.models import (
    Marker,
    MarkerKind,
    RunOrigin,
    Side,
    TradePath,
    TradeRow,
    TradesSummary,
)

__all__ = ["Earlier", "Executed", "LiveTrades", "at_broker", "compose"]

#: Причина выхода по подписи: база хранит подпись (`convert.trade_record`).
#: Нужна только счёту переворотов (`backtest.reversal_entries`); в строку
#: таблицы идёт записанная подпись как есть.
_REASON_OF_LABEL: dict[str, ExitReason] = {reason.label: reason for reason in ExitReason}

TOTAL_TITLE = "Итог за всю историю боя"
CURRENT_TITLE = "Текущий заход боя"


@dataclass(frozen=True, slots=True)
class Earlier:
    """Что прочитано из базы: закрытые боевые сделки всех прогонов, старые первыми.

    `trouble` — почему не прочитаны, словами; пусто — прочитаны. Отказ
    не выдаётся за «сделок не было»: он едет в подпись итога (правило 13).
    """

    trades: tuple[StoredTrade, ...] = ()
    trouble: str = ""


@dataclass(frozen=True, slots=True)
class Executed:
    """Исполнения заявок у брокера по журналу команд: (прогон, имя заявки) → времена.

    Пусто — не прочитаны или исполнений нет; тогда в строках время свечи.
    `current` — номер прогона текущего захода боя: сделки из памяти движка
    номера прогона не несут, и их исполнения ищутся под ним.
    """

    times: Mapping[tuple[int, str], OrderFillTimes] = field(default_factory=dict)
    current: int | None = None

    @classmethod
    def of(cls, rows: tuple[OrderFillTimes, ...]) -> Executed:
        """Строки журнала команд → поиск по (прогон, имя заявки)."""
        return cls({(row.session_id, row.order_id): row for row in rows})

    def get(self, session: int | None, name: str) -> OrderFillTimes | None:
        """Времена заявки `name` прогона `session`; `None` — исполнений не записано."""
        return None if session is None or not name else self.times.get((session, name))


def rested(times: OrderFillTimes) -> bool:
    """Выход стоял у брокера заранее — значит, уровень вёл брокер, а не программа.

    Условная (`D-196`) ставится сразу после входа и исполняется позже, в другой
    минуте. Выход программы по касанию (запасной путь без условной и всё, что
    было до `D-196`) подаётся по закрытию минутки и исполняется в ней же, за
    секунды. Ошибка признака — в безопасную сторону: условная, сработавшая
    в минуту постановки, останется подписанной «скользящий уровень», без
    «у брокера».
    """
    return times.placed_at < floor_to_minute(times.last_at)


def _reason(label: str, exit_times: OrderFillTimes | None) -> str:
    """Подпись причины: скользящий, который вёл брокер, называется так (B-104)."""
    if label == ExitReason.TRAILING_TAKE.label and exit_times is not None and rested(exit_times):
        return convert.BROKER_TRAILING
    return label


def _timed(
    row: TradeRow, executed: Executed, session: int | None, entry_name: str, exit_name: str
) -> TradeRow:
    """Строка с временем исполнения у брокера вместо свечи движка, если оно известно."""
    entry = executed.get(session, entry_name)
    exit_ = executed.get(session, exit_name)
    if entry is None and exit_ is None:
        return row
    return replace(
        row,
        entry_time=entry.first_at if entry is not None else row.entry_time,
        exit_time=exit_.last_at if exit_ is not None else row.exit_time,
        exit_reason=_reason(row.exit_reason, exit_),
    )


def at_broker(run: HistoryRun, executed: Executed) -> convert.AtBroker:
    """Что брокер сообщил о сделках текущего захода — для меток «Факта» (B-104).

    Время исполнения по имени заявки и имена выходов, уровень которых вёл
    брокер условной (`rested`). По ним метка и подсказка не называют дефектом
    расхождение с «прогнозом», который ведёт скользящий сам.
    """
    session = executed.current
    times: dict[str, datetime] = {}
    trailing: set[str] = set()
    for fill in run.fills:
        found = executed.get(session, fill.order_id)
        if found is None:
            continue
        times[fill.order_id] = found.first_at if fill.action is OrderAction.OPEN else found.last_at
    for deal in run.deals:
        found = executed.get(session, deal.exit_order_id)
        if deal.exit_reason is ExitReason.TRAILING_TAKE and found is not None and rested(found):
            trailing.add(deal.exit_order_id)
    return convert.AtBroker(executed=times, trailing=frozenset(trailing))


@dataclass(frozen=True, slots=True)
class LiveTrades:
    """Что показать в бою: строки, итог и метки прежних заходов — из одного списка."""

    rows: tuple[TradeRow, ...]
    summary: TradesSummary
    markers: tuple[Marker, ...]
    paths: tuple[TradePath, ...]


def compose(
    run: HistoryRun,
    stored: Earlier,
    *,
    symbol: str,
    timeframe: Timeframe,
    since: datetime | None,
    executed: Executed | None = None,
) -> LiveTrades:
    """Сделки боя: прежние заходы из базы плюс текущий заход из памяти движка.

    `run` — ход «факта» текущего захода; `stored` — прочитанное из базы;
    `symbol` — инструмент текущего захода и графика: по нему ключ «та же
    сделка» и метки (сделки другого контракта — только строками таблицы);
    `since` — начало текущего захода, `None` — неизвестно; `executed` —
    исполнения у брокера (время сделки в строке), `None` — не прочитаны.
    """
    executed = executed if executed is not None else Executed()
    seen = {
        _key(symbol, deal.side.value, deal.entry_time, deal.exit_time) for deal in run.deals
    }
    earlier = tuple(
        trade for trade in stored.trades
        if _key(trade.symbol, trade.side.value, trade.entry_time, trade.exit_time) not in seen
    )
    earlier_deals = tuple(_deal_of(trade) for trade in earlier)
    merged = earlier_deals + run.deals
    reversals = reversal_entries(merged)
    total = convert.trades_summary(replace(run, deals=merged, summary=summarise(merged)))
    current = convert.trades_summary(run)
    markers: list[Marker] = []
    paths: list[TradePath] = []
    for trade, deal in zip(earlier, earlier_deals, strict=True):
        if trade.symbol != symbol:
            # На графике другого контракта метка легла бы на чужие цены;
            # в таблице сделка остаётся.
            continue
        marks, path = _marks(trade, deal, reversals, timeframe, executed)
        markers.extend(marks)
        paths.append(path)
    return LiveTrades(
        rows=tuple(
            _timed(
                replace(convert.row_of_stored_trade(trade), earlier_run=True), executed,
                trade.session_id, trade.entry_order_id, trade.exit_order_id,
            )
            for trade in earlier
        ) + tuple(
            _timed(
                convert.trade_row(deal, origin=RunOrigin.LIVE), executed, executed.current,
                deal.entry_order_id, deal.exit_order_id,
            )
            for deal in run.deals
        ),
        summary=replace(
            total,
            title=_total_title(stored.trouble),
            current=replace(current, title=_current_title(since)),
        ),
        markers=tuple(markers),
        paths=tuple(paths),
    )


def _key(
    symbol: str, side: str, entry_time: datetime, exit_time: datetime
) -> tuple[str, str, int, int]:
    """«Та же сделка»: инструмент, сторона, вход и выход до секунды — как в базе.

    Секунды отбрасываются тем же счётом, что у `market.storage._to_ts`.
    """
    return symbol, side, int(entry_time.timestamp()), int(exit_time.timestamp())


def _deal_of(trade: StoredTrade) -> Deal:
    """Сделка базы → сделка модели: для итога и переворотов, не для строки таблицы.

    Имена заявок — с номером прогона: у движка каждого захода счёт имён свой,
    и без номера вход одного захода совпал бы по имени с входом другого
    в `reversal_entries`. Неизвестная подпись причины (сменилась между
    версиями) — «команда человека»: переворотом такой выход не считается.
    """
    run = trade.session_id
    return Deal(
        side=EngineSide(trade.side.value),
        volume=trade.volume,
        entry_time=trade.entry_time,
        entry_price=trade.entry_price,
        entry_order_id=f"{run}:{trade.entry_order_id}",
        exit_time=trade.exit_time,
        exit_price=trade.exit_price,
        exit_order_id=f"{run}:{trade.exit_order_id}",
        exit_reason=_REASON_OF_LABEL.get(trade.exit_reason, ExitReason.MANUAL),
        commission=trade.commission,
        ruble_per_point=trade.ruble_per_point,
    )


def _marks(
    trade: StoredTrade, deal: Deal, reversals: frozenset[str], timeframe: Timeframe,
    executed: Executed,
) -> tuple[tuple[Marker, Marker], TradePath]:
    """Метки входа и выхода и линия сделки прежнего захода — слоем «Факт», приглушённо.

    Прогноза у прежнего захода на графике нет (его тень не сохраняется),
    поэтому метка не «сделка вне расчёта»: это была бы неправда о сделке.
    """
    if deal.entry_order_id in reversals:
        entry_kind = MarkerKind.REVERSAL
    else:
        entry_kind = (
            MarkerKind.ENTRY_LONG if deal.side is EngineSide.LONG else MarkerKind.ENTRY_SHORT
        )
    reason = _reason(trade.exit_reason, executed.get(trade.session_id, trade.exit_order_id))
    note = (
        f"Прежний заход боя (прогон №{trade.session_id}): {trade.side.label.lower()}, "
        f"выход — {reason}. Прогноза к прежним заходам на графике нет."
    )
    entry_at = convert.bar_open(deal.entry_time, timeframe)
    exit_at = convert.bar_open(deal.exit_time, timeframe)
    marks = (
        Marker(time=entry_at, price=deal.entry_price, kind=entry_kind, tooltip=note,
               earlier_run=True),
        Marker(time=exit_at, price=deal.exit_price, kind=MarkerKind.EXIT, tooltip=note,
               earlier_run=True),
    )
    profit = deal.net if deal.net is not None else deal.gross
    path = TradePath(
        entry_time=entry_at, entry_price=deal.entry_price,
        exit_time=exit_at, exit_price=deal.exit_price,
        side=Side(deal.side.value), profitable=profit > 0, earlier_run=True,
    )
    return marks, path


def _total_title(trouble: str) -> str:
    """Подпись итога всей истории. Отказ чтения базы — словами, а не пустотой."""
    if trouble:
        return (
            f"{TOTAL_TITLE}: сделки прежних заходов из базы не прочитаны — {trouble} "
            "В итоге только текущий заход"
        )
    return f"{TOTAL_TITLE} (прежние заходы — приглушённые строки)"


def _current_title(since: datetime | None) -> str:
    """Подпись итога текущего захода: с какого момента он идёт, по МСК."""
    if since is None:
        return CURRENT_TITLE
    return f"{CURRENT_TITLE} с {in_moscow(since):%H:%M %d.%m} МСК"
