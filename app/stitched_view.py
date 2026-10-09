"""Прогон окна по склейке контрактов: что подать движку и как показать итог.

Решение 0061: на каждом отрезке времени считается контракт, который тогда
был ближним. Прогон по кускам, прогрев ровно на период средней и закрытие
позиции на стыке уже сделаны — `app/stitched.py` и `backtest/stitched.py`.
Здесь — переходник к окну:

* **какие куски брать.** Главное окно показывает контракт из настроек
  и те, что были перед ним; следующие — нет (робот настроен на свой код,
  расхождение называет плашка «действующий контракт»). Тестер берёт все
  куски выбранного периода: он выбирает период, а не тикер;
* **последний кусок главного окна** — те бары, что уже прочитал и признал
  закрытыми порт (`HistoryPort._candles`): у них своя проверка неполных
  баров живого потока, и второй её копии здесь быть не должно;
* **итог — одним `HistoryRun`**: всё показываемое окном (график, журналы,
  отчёт) уже умеет его читать, и второй вид итога означал бы второй путь
  до каждого из них;
* **подпись стыка.** Движок закрывает позицию на стыке с причиной «конец
  поданного отрезка» (`ExitReason.SERIES_END`, данные, а не ветка). Что
  этот конец — смена контракта, знает только тот, кто резал период; поэтому
  подпись меняется здесь, по месту сделки в ряду, а не в `engine/`.

Бары прогрева (для MXZ6 — конец 16.09) движку подаются, но в сделки
не входят (`backtest.stitched`) и на график не идут: там показан MXU6.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from typing import cast

from app import convert
from app.minutes import minute_plan
from app.stitched import StitchRequest, basis_line, load_pieces
from backtest import HistoryRun
from backtest.execution import Costs, LimitExits
from backtest.stitched import Piece, StitchedRun, replay_pieces, stitched_lines
from engine import EngineSettings
from market import MSK, MarketWorker, Timeframe
from market import Candle as MarketCandle
from ui.models import ContractSeam, Settings, TradeRow

__all__ = [
    "Stitch",
    "StitchedRun",
    "as_history",
    "lines",
    "live_series",
    "period",
    "plan",
    "relabel",
    "request_of",
    "run",
    "seam_reason",
    "warmup_of",
]

#: Самая ранняя дата периода, когда глубина показа — «вся история».
_EARLIEST = date(2000, 1, 1)


@dataclass(frozen=True, slots=True)
class Stitch:
    """Нарезанный период: куски, предупреждения и что из них показать."""

    pieces: tuple[Piece, ...]
    notes: tuple[tuple[datetime, str], ...] = ()

    @property
    def candles(self) -> list[MarketCandle]:
        """Бары для графика и отчёта: тела кусков подряд, без прогрева.

        `Piece` хранит их протоколом движка, а читались они из базы
        (`CandleStore.bars`) — то есть это свечи слоя данных; `cast` только
        возвращает им их собственный тип, ничего не преобразуя.
        """
        return [cast(MarketCandle, bar) for piece in self.pieces for bar in piece.body]

    @property
    def seams(self) -> tuple[ContractSeam, ...]:
        """Стыки: первая свеча каждого следующего непустого куска."""
        made: list[ContractSeam] = []
        previous = ""
        for piece in self.pieces:
            if not piece.body:
                continue
            if previous:
                made.append(ContractSeam(
                    time=piece.body[0].time, symbol=piece.symbol, previous=previous,
                ))
            previous = piece.symbol
        return tuple(made)

    @property
    def symbols(self) -> str:
        """Контракты периода словами: «MXU6 → MXZ6»."""
        return " → ".join(piece.symbol for piece in self.pieces if piece.body)

    @property
    def basis(self) -> str:
        """«Считается на: …» — контракты, дни со свечами и пропуски (B-069)."""
        return basis_line(self.pieces)


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time(0, 0), MSK)


async def plan(
    worker: MarketWorker,
    request: StitchRequest,
    *,
    symbol: str | None = None,
    own: Sequence[MarketCandle] = (),
) -> Stitch | None:
    """Нарезать период по таблице контрактов. `None` — резать не по чему.

    `symbol` задан — это главное окно: куски после контракта из настроек
    отбрасываются, а тело его куска заменяется барами `own`, уже
    проверенными портом. Контракта в кусках нет — `None`: показ идёт
    одним контрактом, как до решения 0061.
    """
    made, notes = await worker.call(lambda store: load_pieces(store, request))
    if symbol is not None:
        index = next(
            (number for number, piece in enumerate(made) if piece.symbol == symbol), None
        )
        if index is None:
            return None
        made = made[: index + 1]
        start = _midnight(made[-1].since)
        body = tuple(bar for bar in own if bar.time >= start)
        made[-1] = replace(made[-1], body=body)
    if not any(piece.body for piece in made):
        return None
    return Stitch(pieces=tuple(made), notes=tuple(notes))


def period(since: datetime | None, until: datetime) -> tuple[date, date]:
    """Даты периода по моментам окна. Нет левой границы — «вся история»."""
    first = since.astimezone(MSK).date() if since is not None else _EARLIEST
    return first, until.astimezone(MSK).date()


def request_of(
    values: Settings, timeframe: Timeframe, since: date, until: date, *, asset: str
) -> StitchRequest:
    """Условия нарезки. Прогрев — период средней из настроек (правило 16).

    `asset` обязателен: чья цепочка режет период, называет вызывающий
    (`market.contracts.asset_of`), а не угадывает таблица.
    """
    return StitchRequest(
        since=since, until=until, timeframe=timeframe,
        warmup_bars=warmup_of(values), asset=asset,
        # Подавать ли минутки — правило B-058, одно с окном (`app/minutes.py`).
        minute_order=minute_plan(values, timeframe).order,
    )


def warmup_of(values: Settings) -> int:
    """Сколько баров прогрева перед рубежом контракта — период средней.

    Одно число на склейку и на живой ход: второе правило прогрева дало бы
    разные сделки на одном контракте с потоком и без (решение 0061).
    """
    return max(values.average_period, 1)


def live_series(
    candles: Sequence[MarketCandle], since: date, warmup: int
) -> list[MarketCandle]:
    """Ряд живого хода по контракту: ровно `warmup` баров до рубежа и всё после.

    То же правило, что у куска склейки (`backtest.stitched`): движок
    отбрасывает первые бары, пока их не больше периода средней, поэтому
    ровно `warmup` баров до рубежа уходят в прогрев и сделок не дают.
    Лишние бары до рубежа (день прогрева качается целиком) решали бы
    и дали бы сделки контрактом вне его периода.
    """
    start = _midnight(since)
    before = [bar for bar in candles if bar.time < start]
    after = [bar for bar in candles if bar.time >= start]
    return [*before[len(before) - min(warmup, len(before)):], *after]


async def run(
    stitch: Stitch,
    values: Settings,
    engine: EngineSettings,
    *,
    steps: Mapping[str, float] | None = None,
) -> StitchedRun:
    """Прогнать куски. Торговый модуль — свежий на каждый кусок.

    `steps` — шаг цены каждого контракта по его карточке биржи; контракт
    без шага идёт с шагом из `values` (`replay_pieces`).
    """
    module = convert.strategy_settings(values)
    algorithm = convert.chosen_algorithm(values)
    known = steps or {}
    pieces = tuple(
        replace(piece, price_step=known[piece.symbol]) if piece.symbol in known else piece
        for piece in stitch.pieces
    )
    return await replay_pieces(
        pieces, lambda: algorithm.build(module), engine,
        convert.run_costs(values), stitch.notes,
    )


def as_history(stitched: StitchedRun, costs: Costs) -> HistoryRun:
    """Итог по кускам — одним `HistoryRun`, который окно уже умеет показывать."""
    return HistoryRun(
        deals=stitched.deals,
        plans=tuple(plan for one in stitched.pieces for plan in one.run.plans),
        fills=tuple(fill for one in stitched.pieces for fill in one.run.fills),
        # Точки средней на барах прогрева не показываются: там на графике
        # прежний контракт, и линия нового легла бы на чужие цены.
        average=tuple(
            point
            for one in stitched.pieces
            for point in one.run.average
            if point[0] >= _midnight(one.piece.since)
        ),
        journal=stitched.journal,
        summary=stitched.summary,
        position=stitched.position,
        halted=stitched.halted,
        bars=sum(len(one.piece.body) for one in stitched.pieces),
        costs=costs,
        # Порядок у кусков один — из того же набора окна (`request_of`).
        # Бары без минуток — суммой по кускам: потерянный здесь счёт
        # означал бы отчёт склейки, молчащий о свечах по размаху.
        minute_order=next(
            (one.run.minute_order for one in stitched.pieces
             if one.run.minute_order is not None),
            None,
        ),
        bars_without_minutes=sum(one.run.bars_without_minutes for one in stitched.pieces),
        # Выходы по времени с предельной ценой — суммой по кускам. Потерянный
        # здесь счёт убрал бы из допущений склейки неисполненную заявку,
        # остановившую робота (правило 13).
        limit_exits=sum((one.run.limit_exits for one in stitched.pieces), LimitExits()),
    )


def _neighbours(stitched: StitchedRun) -> list[str]:
    """На каждый кусок — контракт, в который после него перешёл прогон; «» — не перешёл.

    Одно правило соседа для подписи сделки и для отчёта — и то же, что
    у строки журнала о стыке (`backtest.stitched.replay_pieces`): в
    `StitchedRun.pieces` только прогнанные куски, пустой туда не попадает,
    значит следующий элемент — это следующий непустой кусок.
    """
    runs = stitched.pieces
    return [one.piece.symbol for one in runs[1:]] + [""] * bool(runs)


def seam_reason(stitched: StitchedRun) -> dict[int, str]:
    """Место сделки в `deals` → подпись «смена контракта MXU6 → MXZ6»."""
    labels: dict[int, str] = {}
    place = 0
    for one, following in zip(stitched.pieces, _neighbours(stitched), strict=True):
        place += len(one.run.deals)
        if one.seam is not None:
            labels[place] = f"смена контракта {one.piece.symbol} → {following}"
            place += 1
    return labels


def relabel(rows: Sequence[TradeRow], labels: dict[int, str]) -> tuple[TradeRow, ...]:
    """Строки сделок с подписью стыка вместо «конец поданного отрезка»."""
    return tuple(
        replace(row, exit_reason=labels[place]) if place in labels else row
        for place, row in enumerate(rows)
    )


def lines(stitched: StitchedRun) -> tuple[str, ...]:
    """Итог словами: общий, по контрактам, затем строка на каждый стык.

    Строка закрытия на стыке — та же, что в журнале решений
    (`backtest.stitched._seam_entry`), а не второй её пересказ. Стык без
    позиции тоже назван: иначе по отчёту не видно, где сменился контракт.
    """
    made = list(stitched_lines(stitched))
    closed = [
        f"{entry.event}. {entry.reason}."
        for entry in stitched.journal
        if entry.event.startswith("Смена контракта")
    ]
    for number, (old, following) in enumerate(
        zip(stitched.pieces, _neighbours(stitched), strict=True)
    ):
        if old.seam is None and following:
            new = stitched.pieces[number + 1]
            made.append(
                f"Смена контракта {old.piece.symbol} → {following} "
                f"с {new.piece.since:%d.%m.%Y}: к стыку позиции не было."
            )
    return (*made, *closed)
