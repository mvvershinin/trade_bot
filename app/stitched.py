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
То же — будние дни **внутри** куска без единого бара (B-069: MXU6
05.09–16.09 прошёл без сделок и без слова), кроме отмеченных загруженными:
отметка без свечей — биржа в тот день не торговала.
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
from market.contracts import WARMUP_LOOK_BACK_DAYS, ContractError, asset_of, pieces, runs_of
from market.storage import CandleStore
from ui.models import Settings

__all__ = ["StitchRequest", "basis_line", "load_pieces", "run_stitched", "span_text", "uncovered"]


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
    #: Сегодня по Москве: день, который ещё идёт, пропуском не считается.
    #: `None` — по часам машины.
    today: date | None = None


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


#: Номер субботы в `date.weekday()`: суббота и воскресенье пропуском
#: не считаются. Календарь, а не настройка.
_SATURDAY = 5


def span_text(first: date, last: date) -> str:
    """Отрезок дат коротко: «07.09–16.09», один день — «07.09»."""
    return f"{first:%d.%m}" if first == last else f"{first:%d.%m}–{last:%d.%m}"


def _missing(
    leg: Leg, body: Sequence[Candle], settled: set[date], today: date
) -> list[tuple[date, date]]:
    """Будние дни куска до сегодня без единого бара и без отметки — отрезками.

    Отметка `data_day` без свечей — биржа в этот день не торговала (праздник):
    тревоги нет. Выходные не смотрятся: торги выходного дня без свечей
    здесь не ловятся.
    """
    have = {bar.time.astimezone(MSK).date() for bar in body}
    weekdays: list[date] = []
    day = leg.since
    while day <= leg.until and day < today:
        if day.weekday() < _SATURDAY:
            weekdays.append(day)
        day += timedelta(days=1)
    return runs_of(
        weekdays, (one for one in weekdays if one not in have and one not in settled)
    )


def basis_line(made: Sequence[Piece]) -> str:
    """«Считается на: MXU6 18.06–16.09 (57 дн. со свечами; нет 07.09–16.09) · …».

    На каких контрактах и днях посчитан прогон по склейке — одной строкой.
    «Из N торговых дней биржи» здесь нет: прогон к бирже не ходит, и N ему
    взять неоткуда. Пусто — кусков нет.

    Дни считаются будние (пн–пт), как торговые дни биржи: свечи торгов
    выходного дня биржа относит к понедельнику, и сверка загрузки с её
    дневными свечами считает так же. Путь человека 09.10.2026: MXU6
    18.06–16.09 — 83 календарных дня со свечами против 65 торговых дней
    биржи; два разных числа на один кусок читались бы как пропажа.
    """
    parts = []
    for piece in made:
        days = len({
            day for bar in piece.body
            if (day := bar.time.astimezone(MSK).date()).weekday() < _SATURDAY
        })
        gaps = ", ".join(span_text(first, last) for first, last in piece.missing)
        parts.append(
            f"{piece.symbol} {span_text(piece.since, piece.until)} ({days} дн. со свечами"
            + (f"; нет {gaps}" if gaps else "") + ")"
        )
    return f"Считается на: {' · '.join(parts)}" if parts else ""


def load_pieces(
    store: CandleStore, request: StitchRequest
) -> tuple[list[Piece], list[tuple[datetime, str]]]:
    """Куски с барами и прогревом — плюс предупреждения о днях без свечей.

    :raises ContractError: актив не назван, его цепочки в таблице нет либо
        таблица противоречит сама себе.
    """
    if not request.asset:
        raise ContractError(
            "актив для нарезки по контрактам не назван: чью цепочку брать, "
            "неизвестно"
        )
    legs = pieces(store, request.since, request.until, asset=request.asset)
    today = request.today or datetime.now(MSK).date()
    made: list[Piece] = []
    holes: list[tuple[datetime, str]] = []
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
        missing = _missing(leg, body, store.settled_days(leg.symbol), today)
        made.append(Piece(
            symbol=leg.symbol, since=leg.since, until=leg.until,
            warmup=warmup, body=tuple(body), warmup_wanted=wanted,
            minutes=minutes, missing=tuple(missing),
        ))
        if not body:
            continue  # пустой кусок называет прогон (`replay_pieces`)
        holes.extend(
            (
                _moment(first),
                f"{leg.symbol}: нет свечей за {span_text(first, last)} — сделки "
                "за эти дни не посчитаны. Загрузите историю с "
                f"{first:%d.%m.%Y}: «Загрузить историю…» в меню «Программа»",
            )
            for first, last in missing
        )
    notes = [
        (
            _moment(start),
            f"{start:%d.%m.%Y} … {end:%d.%m.%Y}: в таблице контрактов нет "
            "ближнего на эти дни — они в прогон не вошли",
        )
        for start, end in uncovered(legs, request.since, request.until)
    ]
    return made, sorted([*notes, *holes], key=lambda note: note[0])


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
