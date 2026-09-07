"""Запуск перебора из командной строки: `python -m backtest`.

Почему отдельная точка входа, а не ключ у `app/main.py`
-------------------------------------------------------
Перебор не поднимает окно, не берёт замок папки данных и не трогает токен.
Ему нужны свечи и процессорное время; всё остальное в `app/` — обвязка окна,
и тянуть её ради таблицы значило бы связать таблицу с интерфейсом. Слой
`backtest/` про окно не знает (ARCHITECTURE.md §2), и запуск его собственной
командой это правило сохраняет, а ключ у `app/main.py` — нарушил бы.

Что делается с базой владельца счёта
-------------------------------------
**Только чтение, и это не обещание, а устройство.** База открывается
соединением `mode=ro` и немедленно копируется в отдельный файл; всё остальное
работает с копией. Причин две, и вторая важнее первой:

1. `market.storage.CandleStore` открывает базу на запись и при открытии
   выполняет миграции. Настоящую базу ему не показывают вовсе;
2. **перебор идёт минутами, а программа в это время может дописывать свечи.**
   Считать разные строки одной таблицы на разных данных нельзя — снимок
   делает ряд одинаковым для всех сочетаний по построению.

Копия удаляется после прогона. Ключ `--keep-copy` её оставляет — для случая,
когда таблицу надо будет пересчитать на тех же данных.
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import shutil
import sqlite3
import sys
import tempfile
import time as clock
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta

from backtest.execution import Costs
from backtest.report import study
from backtest.split import (
    CHECKING_DAYS,
    SWEEP_SPAN,
    TUNING_DAYS,
    Fold,
    Period,
    single_split,
    trading_days,
    walk_forward,
)
from backtest.sweep import Block, Ground, Point, blocks, full_cross, sweep, trading_mode
from backtest.table import render
from engine import EngineSettings, in_moscow
from market.aggregate import build_bars
from market.candles import M5, Candle
from market.storage import CandleStore

__all__ = ["main"]

#: Тариф по умолчанию — цифра проекта (DOMAIN.md §5, CLAUDE.md). Не «примерно
#: столько»: все замеры прототипа считаны на ней, и менять её на ходу значит
#: сравнивать тарифы вместо сделок.
COMMISSION = 14.0

#: Дольше этого перебор считается долгим, и на него спрашивают согласия.
#: Пять минут — не круглое число, а граница внимания: до неё ответ «да» ещё
#: осмысленный, после — вопрос превращается в клавишу, которую жмут не читая.
LONG_ENOUGH_TO_ASK = 300.0

#: Секунд в минуте. Названо, чтобы `60` в расчёте времени не читалось
#: как размер свечи: в этом проекте таких шестидесяток две.
A_MINUTE = 60

#: Сколько времени назад брать историю, если не сказано иное. Больше, чем есть
#: в базе, — намеренно: лишнее отсеет отбор торговых дней, а не эта цифра.
DEPTH = timedelta(days=400)


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    """Разобрать ключи командной строки."""
    parser = argparse.ArgumentParser(
        prog="python -m backtest",
        description="Перебор настроек с раздельными подбором и проверкой.",
    )
    parser.add_argument("--symbol", default="MXU6", help="инструмент, по умолчанию MXU6")
    parser.add_argument("--db", default="userdata/candles.sqlite3", help="база со свечами")
    parser.add_argument("--commission", type=float, default=COMMISSION,
                        help="комиссия в рублях за контракт на сторону")
    parser.add_argument("--slippage", type=float, default=0.0,
                        help="проскальзывание в шагах цены")
    parser.add_argument("--price-step", type=float, default=0.0,
                        help="шаг цены инструмента, обязателен при непустом проскальзывании")
    parser.add_argument("--tuning-days", type=int, default=TUNING_DAYS,
                        help="торговых дней на подбор в одном окне")
    parser.add_argument("--checking-days", type=int, default=CHECKING_DAYS,
                        help="торговых дней на проверку в одном окне")
    parser.add_argument("--full", action="store_true",
                        help="полное произведение всех осей вместо трёх блоков")
    parser.add_argument("--out", help="куда записать таблицу; без ключа — на экран")
    parser.add_argument("--keep-copy", action="store_true",
                        help="не удалять снимок базы после прогона")
    parser.add_argument("--yes", action="store_true",
                        help="не спрашивать подтверждения на длинный перебор")
    return parser.parse_args(argv)


def _snapshot(source: pathlib.Path, into: pathlib.Path) -> pathlib.Path:
    """Снять копию базы, не открывая оригинал на запись.

    :raises SystemExit: базы нет или её не удалось прочитать. Отказ громкий
        и с путём: молчаливый пустой перебор выглядел бы как «сделок нет».
    """
    if not source.exists():
        raise SystemExit(f"базы со свечами нет: {source}")
    copy = into / "candles-snapshot.sqlite3"
    try:
        origin = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    except sqlite3.Error as trouble:
        raise SystemExit(f"база {source} не открылась на чтение: {trouble}") from trouble
    with origin:
        target = sqlite3.connect(copy)
        with target:
            origin.backup(target)
        target.close()
    origin.close()
    return copy


def _bars(copy: pathlib.Path, symbol: str) -> list[Candle]:
    """Пятиминутки инструмента из снимка базы, по возрастанию времени."""
    with CandleStore(copy) as store:
        since = datetime.now(tz=in_moscow(datetime.now().astimezone()).tzinfo) - DEPTH
        minutes = store.minutes(symbol, since=since)
    if not minutes:
        raise SystemExit(
            f"в базе нет свечей по {symbol} за последние {DEPTH.days} дней. "
            "Загрузите историю прежде, чем перебирать настройки"
        )
    return list(build_bars(minutes, M5, on_duplicate="skip"))


def _days_of(bars: Sequence[Candle]) -> tuple[date, ...]:
    """Торговые дни ряда: будние и с полной историей часов перебора."""
    since, until = SWEEP_SPAN
    counted: Counter[date] = Counter()
    for bar in bars:
        moment = in_moscow(bar.time)
        if since <= moment.time() < until:
            counted[moment.date()] += 1
    return trading_days(list(counted.items()))


def _ground(args: argparse.Namespace) -> Ground:
    """Условия перебора: умолчания движка, включённый переворот, издержки."""
    costs = Costs(
        commission_per_side=args.commission,
        price_step=args.price_step,
        slippage_steps=args.slippage,
    )
    engine = trading_mode(EngineSettings(commission_per_side=args.commission))
    return Ground(engine=engine, costs=costs)


def _plan(ground: Ground, *, whole: bool) -> tuple[Block, ...]:
    """Что перебирать: три блока либо полное произведение одним блоком."""
    if not whole:
        return blocks(ground)
    points = full_cross(ground)
    return (Block(
        name="full",
        title="Полное произведение обязательной программы",
        question="все оси разом; взаимодействия видны, число испытаний огромно",
        points=points,
    ),)


def _confirm(runs: int, seconds: float, *, asked: bool) -> None:
    """Сказать, сколько будет прогонов, **до** запуска, и спросить, если долго.

    Требование ТЗ §4.10 В. Число печатается всегда; согласие спрашивается
    только тогда, когда перебор идёт дольше пяти минут — иначе вопрос
    превращается в клавишу, которую жмут не читая.
    """
    print(f"Прогонов: {runs}. Ожидаемое время: {_duration(seconds)}.", file=sys.stderr)
    if seconds < LONG_ENOUGH_TO_ASK or asked:
        return
    answer = input("Это надолго. Продолжать? [д/N] ").strip().lower()
    if answer not in {"д", "да", "y", "yes"}:
        raise SystemExit("перебор отменён")


def _duration(seconds: float) -> str:
    """Время словами: минуты и секунды."""
    if seconds < A_MINUTE:
        return f"{seconds:.0f} с"
    return f"{int(seconds) // A_MINUTE} мин {int(seconds) % A_MINUTE:02d} с"


def _plain(ground: Ground) -> Point:
    """Точка умолчаний: с ней сравнивается всё остальное."""
    return Point(label="умолчания программы", engine=ground.engine, strategy=ground.strategy)


async def _work(args: argparse.Namespace) -> str:
    """Весь прогон: снимок, свечи, дни, сетка, перебор, таблица."""
    with tempfile.TemporaryDirectory(prefix="terminal-sweep-") as room:
        copy = _snapshot(pathlib.Path(args.db), pathlib.Path(room))
        bars = _bars(copy, args.symbol)
        if args.keep_copy:
            shutil.copy2(copy, pathlib.Path.cwd() / copy.name)
        return await _table(args, bars)


def _trim(bars: Sequence[Candle], days: Sequence[date]) -> list[Candle]:
    """Отрезать ряд по первому и последнему торговому дню.

    ⚠️ Не оптимизация, хотя и ускоряет. Свечи, лежащие раньше первого
    торгового дня, дают сделки, которые не попадают ни в подбор, ни
    в проверку: они считаются, тратят время и не идут никуда. Хуже того,
    в базе перед плотным отрезком лежат месяцы редких дней — по три десятка
    минуток в сутки, — и средняя прогревалась бы на них.

    Цена решения названа вслух: **средняя стартует холодной на первом дне
    подбора**. Это одинаково для всех сочетаний, лежит целиком внутри подбора
    и на колонку «проверка» не влияет.
    """
    first, last = days[0], days[-1]
    return [
        bar for bar in bars
        if first <= in_moscow(bar.time).date() <= last
    ]


async def _table(args: argparse.Namespace, bars: Sequence[Candle]) -> str:
    """Построить таблицу по готовому ряду свечей."""
    days = _days_of(bars)
    bars = _trim(bars, days)
    split = single_split(days, tuning=args.tuning_days)
    folds = walk_forward(days, tuning=args.tuning_days, checking=args.checking_days)
    ground = _ground(args)
    plan = _plan(ground, whole=args.full)
    runs = sum(len(block.points) for block in plan)
    _confirm(runs, runs * _pace(bars), asked=args.yes)

    periods = _periods(split, folds)
    started = clock.perf_counter()
    parts = []
    for block in plan:
        trials = await sweep(bars, block.points, periods, costs=ground.costs,
                             on_progress=_progress(block.title))
        parts.append((block, trials))
    spent = clock.perf_counter() - started
    print(f"\nПрогон занял {_duration(spent)}.", file=sys.stderr)
    return render(study(
        symbol=args.symbol, days=days, split=split, folds=folds, costs=ground.costs,
        parts=parts, plain=_plain(ground), notes=_notes(runs, spent, len(bars)),
    ))


def _periods(split: Fold, folds: Sequence[Fold]) -> tuple[Period, ...]:
    """Все отрезки, по которым раскладываются сделки, без повторов.

    Отрезок подбора одного деления и отрезок подбора первого окна — один
    и тот же объект по значению, и считать его дважды незачем.
    """
    named = [split.tuning, split.checking]
    for fold in folds:
        named.extend((fold.tuning, fold.checking))
    return tuple(dict.fromkeys(named))


#: Замер 05.09.2026 на этом дереве: 15 891 бар, 107 прогонов за 89 секунд
#: в один поток на Intel i5-13420H. Пересчитывается пропорционально числу
#: баров — цикл движка по ним линеен, и это проверено вторым замером
#: (полное произведение, 96 случайных сочетаний, 0,915 с на прогон).
PACE = 0.83 / 15891


def _pace(bars: Sequence[Candle]) -> float:
    """Сколько секунд занимает один прогон на этом ряде. Замер, а не догадка."""
    return PACE * len(bars)


def _progress(title: str) -> Callable[[int, int, Point], None]:
    """Строка хода перебора в поток ошибок: таблица идёт в вывод, ход — рядом."""
    def told(done: int, total: int, point: Point) -> None:
        del point
        if done % 10 == 0 or done == total:
            print(f"  {title}: {done}/{total}", end="\r", file=sys.stderr)
    return told


def _notes(runs: int, spent: float, bars: int) -> tuple[str, ...]:
    """Оговорки, которые едут вместе с таблицей.

    ⚠️ Последняя оговорка переписана 05.09.2026 (`D-059`). До этого дня она
    говорила «глубина внутридневной истории у биржи около двух месяцев, и
    длиннее период не станет без другого источника данных». После сшивки
    ближних контрактов (решение 0049) это неправда: ряд `@MX` даёт 280
    торговых дней. Строка отговаривала от того, что уже сделано, — а оговорка,
    которой нельзя верить, хуже отсутствующей: она обесценивает соседние
    четыре, которые верны.
    """
    return (
        f"Перебрано {runs} сочетаний за {_duration(spent)} на {bars} пятиминутках. "
        "Чем больше сочетаний, тем выше шанс, что лучшее — случайность.",
        "Умолчания программы (тейк 0,5 %, окно 10:05–11:00) подобраны на том же "
        "отрезке, на котором проверялись. Эта таблица их не меняет.",
        "Третьего варианта поведения после тейка («сразу восстановить позицию») "
        "в движке нет — обязательная программа ТЗ §8 закрыта не полностью.",
        "Проскальзывание задаётся ключом и по умолчанию нулевое: результат "
        "систематически лучше настоящего счёта.",
        "Длина истории зависит от того, по чему считали. Один фьючерс живёт "
        "около 70 ликвидных дней, и на такой длине проверка ничего не "
        "различает. Минутные свечи биржа отдаёт за всю жизнь каждого контракта, "
        "поэтому сшитый ряд ближних контрактов даёт 280 торговых дней: "
        "«python3 -m app.fetch --stitch @MX», затем «python -m backtest "
        "--symbol @MX --db userdata/chain.sqlite3» (решение 0049).",
        "Сшитый ряд годится только для замеров: цены на стыках контрактов "
        "не подгоняются, и торговать по нему нельзя. Торговое окно дня стыка "
        "испорчено целиком — средняя считает скачок цены ходом рынка.",
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Точка входа. Возвращает код возврата процесса."""
    args = _arguments(argv)
    text = asyncio.run(_work(args))
    if args.out:
        pathlib.Path(args.out).write_text(text, encoding="utf-8")
        print(f"Таблица записана: {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
