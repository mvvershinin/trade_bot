"""Прогон тестера по периоду, а не по тикеру: куски по контрактам из базы.

Решение 0061. Период режется по таблице `contract` (`market.contracts.pieces`):
на каждом отрезке времени — контракт, который тогда был ближним. Каждый
кусок читается из базы своим тикером; перед каждым, кроме первого,
читается ровно столько закрытых баров того же контракта, сколько период
средней (правило 16 — число из настроек, а не константа). Прогон
и закрытие позиции на стыке — `backtest.stitched`.

Здесь нет ни одного правила торговли: только «какие бары, в каком порядке».

⚠️ Дни периода, не покрытые ни одним контрактом, `pieces` выкидывает
по построению. Молча это не остаётся: каждая такая дыра — строка
предупреждения в журнале прогона и в `StitchedRun.problems` (правило 13).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from app import convert
from app.minutes import minute_plan, minutes_between
from backtest import MinuteOrder
from backtest.stitched import Piece, StitchedRun, replay_pieces
from engine import EngineSettings
from market.candles import MSK, Candle, Timeframe
from market.chain import Leg
from market.contracts import WARMUP_LOOK_BACK_DAYS, ContractError, asset_of, pieces
from market.storage import CandleStore
from ui.models import Settings

__all__ = ["StitchRequest", "load_pieces", "run_stitched", "uncovered"]


@dataclass(frozen=True, slots=True)
class StitchRequest:
    """Что и откуда нарезать. Условия идут вместе, порознь смысла не имеют."""

    since: date
    until: date
    timeframe: Timeframe
    #: Сколько баров прогрева перед каждым куском, кроме первого, —
    #: период средней из настроек. Умолчания нет намеренно.
    warmup_bars: int
    look_back_days: int = WARMUP_LOOK_BACK_DAYS
    #: Базовый актив, чья цепочка режет период (`market.contracts.asset_of`).
    #: Пусто — не назван, и нарезка отказывает вслух: взять «любую» цепочку
    #: таблицы значило бы прогнать RIZ6 кусками MX.
    asset: str = ""
    #: Порядок обхода цен минутки. Задан — каждый кусок получает минутки
    #: **своего** контракта (`Piece.minutes`), и уровни внутри бара
    #: проверяются по ним; `None` — по размаху бара, как до минуток.
    minute_order: MinuteOrder | None = None


def _moment(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time(), MSK)


def uncovered(legs: Sequence[Leg], since: date, until: date) -> list[tuple[date, date]]:
    """Отрезки периода, на которых нет ни одного контракта. Обе границы включительно."""
    holes: list[tuple[date, date]] = []
    cursor = since
    for leg in legs:
        if leg.since > until:
            break
        if leg.since > cursor:
            holes.append((cursor, leg.since - timedelta(days=1)))
        cursor = max(cursor, leg.until + timedelta(days=1))
    if cursor <= until:
        holes.append((cursor, until))
    return holes


def load_pieces(
    store: CandleStore, request: StitchRequest
) -> tuple[list[Piece], list[tuple[datetime, str]]]:
    """Куски с барами и прогревом — плюс предупреждения о днях без контракта.

    :raises ContractError: актив не назван, его цепочки в таблице нет либо
        таблица противоречит сама себе.
    """
    if not request.asset:
        raise ContractError(
            "актив для нарезки по контрактам не назван: чью цепочку брать, "
            "неизвестно"
        )
    legs = pieces(store, request.since, request.until, asset=request.asset)
    made: list[Piece] = []
    for number, leg in enumerate(legs):
        warmup: tuple[Candle, ...] = ()
        wanted = 0 if number == 0 else request.warmup_bars
        if wanted:
            before = store.bars(
                leg.symbol, request.timeframe,
                since=_moment(leg.since - timedelta(days=request.look_back_days)),
                until=_moment(leg.since),
                drop_unsettled=True,
            )
            warmup = tuple(before[-wanted:])
        body = store.bars(
            leg.symbol, request.timeframe,
            since=_moment(leg.since),
            until=_moment(leg.until + timedelta(days=1)),
            drop_unsettled=True,
        )
        # Минутки — по границам куска, а не по прочитанным барам: тело
        # последнего куска главное окно заменяет своими барами
        # (`stitched_view.plan`), и они обязаны лечь под те же минутки.
        # Начало — с первого бара прогрева: иначе прогрев целиком ушёл бы
        # в `bars_without_minutes`.
        minutes = None
        if request.minute_order is not None:
            minutes = minutes_between(
                store, leg.symbol,
                warmup[0].time if warmup else _moment(leg.since),
                _moment(leg.until + timedelta(days=1)),
                request.minute_order,
            )
        made.append(Piece(
            symbol=leg.symbol, since=leg.since, until=leg.until,
            warmup=warmup, body=tuple(body), warmup_wanted=wanted,
            minutes=minutes,
        ))
    notes = [
        (
            _moment(start),
            f"{start:%d.%m.%Y} … {end:%d.%m.%Y}: в таблице контрактов нет "
            "ближнего на эти дни — они в прогон не вошли",
        )
        for start, end in uncovered(legs, request.since, request.until)
    ]
    return made, notes


async def run_stitched(
    store: CandleStore,
    since: date,
    until: date,
    values: Settings,
    engine: EngineSettings,
) -> StitchedRun:
    """Прогон тестера по склейке за период — точка входа для окна (Ф4).

    Торговый модуль собирается заново на каждый кусок из тех же настроек:
    средняя одного контракта в другой не переходит.
    """
    module = convert.strategy_settings(values)
    algorithm = convert.chosen_algorithm(values)
    timeframe = convert.timeframe_of(values.timeframe)
    request = StitchRequest(
        since=since, until=until,
        timeframe=timeframe,
        warmup_bars=values.average_period,
        asset=asset_of(values.instrument.strip()),
        minute_order=minute_plan(values, timeframe).order,
    )
    made, notes = load_pieces(store, request)
    return await replay_pieces(
        made, lambda: algorithm.build(module), engine, convert.run_costs(values), notes,
    )
