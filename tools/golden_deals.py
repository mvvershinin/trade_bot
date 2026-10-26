"""Проверка-образец: зафиксированные данные и настройки — зафиксированный список сделок.

Замена сверки с прототипом после решения владельца счёта 05.10.2026: единственный
алгоритм программы — «Реверс с постоянной позицией» (`MaReverseAlways`), а сверка
шла по первому. Сверка ловила любую правку, незаметно меняющую сделки; этот
образец ловит то же самое на втором алгоритме. Оракулом он **не является**:
он доказывает «ничего не поменялось», а не «правильно». Снят 05.10.2026 с движка,
на котором сверка с прототипом прошла 25 из 25.

Данные и образцы лежат в `reference/` — вне git. Нет файла — тест пропускается.

Запуск::

    .venv/bin/python -m tools.golden_deals                      сравнить, ничего не писать
    .venv/bin/python -m tools.golden_deals --rewrite --reason "…"   переписать образец

Переписывание — **только этой командой** и только с причиной: она ложится строкой
в `reference/golden/JOURNAL.md`. Тест образец не переписывает никогда.

⚠️ Все настройки названы здесь явно, ни одна не берётся из умолчаний
`EngineSettings`, `Costs`, `Minutes` и `MaReverseAlwaysSettings`: умолчания
меняются, а образец обязан стоять. Что ни одно поле не забыто, проверяет
`missing_fields()` — новое поле у любого из четырёх классов роняет тест вслух.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import dataclasses
import difflib
import hashlib
import pathlib
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone
from typing import Any

import backtest.history as history_module
from backtest import Costs, HistoryExecutor, MinuteOrder, Minutes, replay
from engine import (
    EngineSettings,
    Mode,
    OrderAction,
    OrderRequest,
    PartialCandles,
    Reversal,
    TimeExitOrder,
    TradingWindow,
)
from engine.window import NO_MARKS
from market.aggregate import build_bars
from market.candles import M5, MINUTE, Candle
from strategies import AverageKind, MaReverseAlways, MaReverseAlwaysSettings

REPO = pathlib.Path(__file__).resolve().parent.parent
GOLDEN = REPO / "reference" / "golden"
JOURNAL = GOLDEN / "JOURNAL.md"
MXU6_BARS = REPO / "reference/stand/install/data/Set_MoexFuturesMxu6/MXU6/Min5/MXU6.txt"
MXU6_MINUTES = REPO / "reference" / "minutes" / "MXU6.csv"
MXZ6_MINUTES = REPO / "reference" / "minutes" / "MXZ6.csv"

#: Московское время фиксированным сдвигом: машина живёт в +7, и местное время
#: здесь не годится ни для разбора данных, ни для строки журнала.
MSK = timezone(timedelta(hours=3), "MSK")
#: Отрезок эталонного датасета — тот же, что у сверки с прототипом.
MXU6_FROM = datetime(2026, 6, 19, tzinfo=MSK)
MXU6_TO = datetime(2026, 8, 26, 23, 59, tzinfo=MSK)
#: Полей в строке датасета до объёма: дата, время и четыре цены.
BAR_FIELDS = 6

#: Комиссия заказчика, ответ 15.09.2026 (решение 0060).
COMMISSION = 17.0
#: Шаг цены фьючерса на индекс МосБиржи, пунктов. Задан биржей.
PRICE_STEP = 25.0

WINDOWS = {
    "0930-1130": TradingWindow(time(9, 30), time(11, 30)),
    "0950-1100": TradingWindow(time(9, 50), time(11, 0)),
}


@dataclasses.dataclass(frozen=True, slots=True)
class Case:
    """Одна точка образца: инструмент, вариант настроек, окно."""

    symbol: str
    variant: str
    window: str

    @property
    def name(self) -> str:
        """Имя точки латиницей: id теста и имя файла образца."""
        return f"{self.symbol.lower()}-{self.variant}-{self.window}"

    @property
    def path(self) -> pathlib.Path:
        """Файл образца точки."""
        return GOLDEN / f"{self.name}.txt"

    @property
    def walks_minutes(self) -> bool:
        """Идёт ли точка по минуткам: только вариант (б), скользящий уровень."""
        return self.variant == "trailing"


CASES: tuple[Case, ...] = tuple(
    Case(symbol, variant, window)
    for symbol in ("MXU6", "MXZ6")
    for variant in ("fixed", "trailing")
    for window in WINDOWS
)

# ---------------------------------------------------------------------------
# Настройки — все поля явно
# ---------------------------------------------------------------------------

STRATEGY_VALUES: dict[str, Any] = {"period": 15, "kind": AverageKind.EMA}

#: Вариант (а): неподвижный тейк, переворот в той же свече, выход по концу окна
#: заявкой с предельной ценой 10 шагов × 25.
FIXED_VALUES: dict[str, Any] = {
    "mode": Mode.REVERSE,
    "close_on_time_end": True,
    "trade_in_weekend": False,
    "volume": 1.0,
    "reversal": Reversal.SAME_BAR,
    "take_profit": True,
    "take_profit_percent": 0.5,
    "trailing_take_profit": False,
    "trailing_start_percent": 0.20,
    "trailing_offset_percent": 0.10,
    "trailing_step_percent": 0.05,
    "stop_after_take_profit": True,
    "min_exit_profit_sides": 0.0,
    "partial_candles": PartialCandles.ACCEPT,
    "commission_per_side": COMMISSION,
    "ruble_per_point": 1.0,
    "close_wait_bars": 3,
    "calendar": NO_MARKS,
    "exchange_days": NO_MARKS,
    "time_exit_order": TimeExitOrder.LIMIT,
    "time_exit_limit_steps": 10,
    "time_exit_wait_bars": 1,
    "price_step": PRICE_STEP,
}

#: Вариант (б): то же со скользящим уровнем 0,20 / 0,10 / 0,05 и минутками.
VARIANTS: dict[str, dict[str, Any]] = {
    "fixed": FIXED_VALUES,
    "trailing": {**FIXED_VALUES, "trailing_take_profit": True},
}

COSTS_VALUES: dict[str, Any] = {
    "tariff": None,
    "commission_per_side": COMMISSION,
    "price_step": PRICE_STEP,
    "slippage_steps": 0.0,
}

MINUTE_ORDER = MinuteOrder.NEAR_FIRST


def missing_fields() -> list[str]:
    """Поля классов настроек, не названные здесь явно. Пусто — всё названо."""
    named = {
        EngineSettings: set(FIXED_VALUES) | {"window"},
        Costs: set(COSTS_VALUES),
        MaReverseAlwaysSettings: set(STRATEGY_VALUES),
        Minutes: {"candles", "order"},
    }
    gaps: list[str] = []
    for kind, names in named.items():
        own = {field.name for field in dataclasses.fields(kind)}
        gaps += [f"{kind.__name__}.{name}" for name in sorted(own ^ names)]
    return gaps


def engine_settings(case: Case) -> EngineSettings:
    return EngineSettings(window=WINDOWS[case.window], **VARIANTS[case.variant])


# ---------------------------------------------------------------------------
# Данные
# ---------------------------------------------------------------------------


def _mxu6_bars() -> list[Candle]:
    """Эталонный датасет. Строка `ГГГГММДД,ЧЧММСС,o,h,l,c,v,0`, время — начало свечи.

    Ряд не сортируется: порча порядка обязана ронять прогон, как в бою.
    """
    rows: list[Candle] = []
    for line in MXU6_BARS.read_text(encoding="utf-8").splitlines():
        parts = line.strip().split(",")
        if len(parts) < BAR_FIELDS:
            continue
        moment = datetime.strptime(parts[0] + parts[1], "%Y%m%d%H%M%S").replace(tzinfo=MSK)
        if MXU6_FROM <= moment <= MXU6_TO:
            rows.append(Candle(
                time=moment, open=float(parts[2]), high=float(parts[3]),
                low=float(parts[4]), close=float(parts[5]),
                volume=float(parts[6]) if len(parts) > BAR_FIELDS else 0.0,
                timeframe=M5, filled_minutes=5,
            ))
    return rows


def _minutes(path: pathlib.Path) -> list[Candle]:
    with path.open(newline="", encoding="utf-8") as source:
        return [
            Candle(
                time=datetime.fromtimestamp(int(row["ts"]), MSK),
                open=float(row["open"]), high=float(row["high"]),
                low=float(row["low"]), close=float(row["close"]),
                volume=float(row["volume"]), timeframe=MINUTE, filled_minutes=1,
            )
            for row in csv.DictReader(source)
        ]


def inputs(case: Case) -> list[pathlib.Path]:
    """Файлы, от которых зависит точка. Их sha256 стоит в шапке образца."""
    if case.symbol == "MXZ6":
        return [MXZ6_MINUTES]
    return [MXU6_BARS, MXU6_MINUTES] if case.walks_minutes else [MXU6_BARS]


def missing_inputs() -> list[pathlib.Path]:
    return [path for path in (MXU6_BARS, MXU6_MINUTES, MXZ6_MINUTES) if not path.is_file()]


def series(case: Case) -> tuple[list[Candle], Minutes | None]:
    """Пятиминутки точки и минутки под ними — только для варианта (б)."""
    if case.symbol == "MXZ6":
        minutes = _minutes(MXZ6_MINUTES)
        bars = build_bars(minutes, M5)
    else:
        bars = _mxu6_bars()
        minutes = [
            minute for minute in _minutes(MXU6_MINUTES)
            if MXU6_FROM <= minute.time <= MXU6_TO + timedelta(minutes=5)
        ] if case.walks_minutes else []
    fed = Minutes(candles=minutes, order=MINUTE_ORDER) if case.walks_minutes else None
    return bars, fed


# ---------------------------------------------------------------------------
# Прогон
# ---------------------------------------------------------------------------


@contextmanager
def counted_arms() -> Iterator[list[HistoryExecutor]]:
    """Подменить исполнитель `replay` считающим вооружения; вернуть на место."""
    made: list[HistoryExecutor] = []

    class Counting(HistoryExecutor):
        arms = 0

        def __init__(self, *, costs: Costs, ruble_per_point: float) -> None:
            super().__init__(costs=costs, ruble_per_point=ruble_per_point)
            made.append(self)

        async def submit(self, order: OrderRequest) -> None:
            if order.action is OrderAction.ARM_TAKE_PROFIT:
                self.arms += 1
            await super().submit(order)

    # Подмена через словарь модуля, а не присваиванием атрибута: имя класса
    # для проверки типов неизменяемо, а глушить её здесь значило бы добавить
    # подавление ради инструмента. Возврат — в `finally`, на любом исходе.
    names = vars(history_module)
    original = names["HistoryExecutor"]
    names["HistoryExecutor"] = Counting
    try:
        yield made
    finally:
        names["HistoryExecutor"] = original


def _money(value: float | None) -> str:
    return "нет" if value is None else f"{value:.2f}"


def render(case: Case) -> str:
    """Прогнать точку и записать результат текстом образца."""
    bars, fed = series(case)
    settings = engine_settings(case)
    strategy = MaReverseAlways(MaReverseAlwaysSettings(**STRATEGY_VALUES))
    with counted_arms() as made:
        run = asyncio.run(replay(
            bars, strategy, settings, costs=Costs(**COSTS_VALUES), minutes=fed,
        ))
    executor = made[-1]
    lines = [f"# образец сделок {case.name}"]
    lines += [f"data {path.relative_to(REPO)} sha256 {sha256(path)}" for path in inputs(case)]
    lines += [
        f"settings {settings!r}",
        f"halted {run.halted or '-'}",
        f"bars {run.bars} bars_without_minutes {run.bars_without_minutes} "
        f"minute_order {run.minute_order.value if run.minute_order else '-'}",
        f"arms {getattr(executor, 'arms', 0)}",
        f"limit_exits {run.limit_exits}",
        f"gross {_money(run.summary.gross_profit)} commission {_money(run.summary.commission)} "
        f"net {_money(run.summary.net_profit)}",
        f"position {run.position!r}",
        f"deals {len(run.deals)}",
        "entry_time\tside\tentry_price\texit_time\texit_price\tvolume\texit_reason\tcommission",
    ]
    lines += [
        "\t".join((
            deal.entry_time.astimezone(MSK).isoformat(), deal.side.value, repr(deal.entry_price),
            deal.exit_time.astimezone(MSK).isoformat(), repr(deal.exit_price), repr(deal.volume),
            deal.exit_reason.value, _money(deal.commission),
        ))
        for deal in run.deals
    ]
    return "\n".join(lines) + "\n"


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Сравнение
# ---------------------------------------------------------------------------


def deal_count(text: str) -> int:
    """Число сделок из строки `deals N` образца; нет строки — ноль."""
    for line in text.splitlines():
        if line.startswith("deals "):
            return int(line.split()[1])
    return 0


def header(text: str, key: str) -> str:
    """Значение строки шапки `key …` образца; нет строки — пусто."""
    for line in text.splitlines():
        if line.startswith(key + " "):
            return line[len(key) + 1:]
    return ""


def stale_data(expected: str, case: Case) -> list[str]:
    """Строки `data` образца, не совпавшие с файлами на диске."""
    recorded = [line for line in expected.splitlines() if line.startswith("data ")]
    actual = [f"data {path.relative_to(REPO)} sha256 {sha256(path)}" for path in inputs(case)]
    return [line for line in recorded if line not in actual] + [
        line for line in actual if line not in recorded
    ]


def difference(expected: str, actual: str, limit: int = 20) -> str:
    """Первые различающиеся строки с номерами. Пусто — совпало."""
    old, new = expected.splitlines(), actual.splitlines()
    out: list[str] = []
    matcher = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        out += [f"  образец {n + 1:>5}: {old[n]}" for n in range(i1, i2)]
        out += [f"  прогон  {n + 1:>5}: {new[n]}" for n in range(j1, j2)]
        if len(out) >= limit:
            break
    return "\n".join(out[:limit])


# ---------------------------------------------------------------------------
# Команда
# ---------------------------------------------------------------------------


def _journal(reason: str, written: Sequence[tuple[Case, str]]) -> None:
    stamp = datetime.now(MSK).strftime("%d.%m.%Y %H:%M МСК")
    details = "; ".join(
        f"{case.name}: {deal_count(text)} сделок, "
        f"sha256 {hashlib.sha256(text.encode()).hexdigest()[:16]}"
        for case, text in written
    )
    if not JOURNAL.exists():
        JOURNAL.write_text(
            "# Журнал образца сделок (`tools/golden_deals.py`)\n\n", encoding="utf-8"
        )
    with JOURNAL.open("a", encoding="utf-8") as sink:
        sink.write(f"- {stamp} — образец переписан, причина: {reason}. {details}\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Проверка-образец сделок алгоритма №2.")
    parser.add_argument("--rewrite", action="store_true", help="переписать образец")
    parser.add_argument("--reason", default="", help="причина переписывания, обязательна")
    args = parser.parse_args(argv)
    if args.rewrite and not args.reason.strip():
        print("образец не переписан: нужна причина, --reason \"…\"", file=sys.stderr)
        return 2
    if missing_fields() or missing_inputs():
        print(
            f"не названы поля: {missing_fields()}; нет данных: {missing_inputs()}",
            file=sys.stderr,
        )
        return 2
    rendered = [(case, render(case)) for case in CASES]
    if args.rewrite:
        GOLDEN.mkdir(parents=True, exist_ok=True)
        for case, text in rendered:
            case.path.write_text(text, encoding="utf-8")
        _journal(args.reason.strip(), rendered)
        print(f"образец переписан: {GOLDEN}")
        return 0
    failed = 0
    for case, text in rendered:
        expected = case.path.read_text(encoding="utf-8") if case.path.is_file() else ""
        diff = difference(expected, text)
        print(f"{case.name}: {'совпало' if not diff else 'РАЗОШЛОСЬ'}")
        if diff:
            failed += 1
            print(diff)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
