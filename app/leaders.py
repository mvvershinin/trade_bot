"""Лидеры перебора становятся шаблонами настроек: проводка, а не расчёт.

Просьба владельца счёта 05.09.2026, дословно: «прогнать варианты, которые ты
сам создашь, выбрать год и период действия контракта, выбрать лидеров штук 15
и их сохранить как шаблоны, вывести свод и сохранить в файл его».

Что здесь есть и чего здесь нет
--------------------------------
Здесь **нет расчёта**. Перебор, отбор лидеров, поправки на подгонку и свод
живут в `backtest/leaders.py` — это торговый слой, и числа приходят оттуда
готовыми (ARCHITECTURE.md §2). Здесь три вещи, которых у `backtest/` быть
не может, потому что оно не знает ни окна, ни библиотеки шаблонов, ни журнала:

1. **разворот рецепта в набор окна** (`settings_of`) — то, что ляжет в шаблон;
2. **запись файла примеров** в папку `examples/` поставки;
3. **запись прогонов** в журнал — по отдельной просьбе, ключом `--record-runs`.

Куда кладутся примеры и почему не в рабочий файл
------------------------------------------------
Решение 0051 («примеры едут своей папкой»). Слова владельца счёта
06.09.2026: «сделать папку "примеры" и сделать там
в окне с шаблонами экспорт и импорт — это будет более зрелое решение».

**Рабочий файл `userdata/settings-templates.json` этот модуль не открывает
вовсе.** Пятнадцать машинных наборов вперемешку с его собственными через месяц
не разделить, а следующая поставка не смогла бы обновить примеры, не споря
с его правками. Формат файла примеров — тот же, что у библиотеки, чтобы кнопка
«Импортировать…» читала его без переводчика.

⚠️ **Имя примера обязано нести, на каком инструменте и отрезке он отобран.**
Без этого пример читается как рекомендация, а подбор в этом проекте дважды
проиграл бездействию. Режимы переворота и поведения после тейка в имя
не помещаются (шестьдесят знаков) и видны во вкладке «Что в наборе».

Почему сетка описана рецептом, а не набором окна
------------------------------------------------
Сочетание описано в `backtest.leaders.Recipe` простыми величинами — время,
число, флаг. Разворачивают её **два слоя**: торговый — в настройки движка,
этот — в набор окна. Обратного перевода «настройки движка → набор окна»
в программе нет и заводить его нельзя (`D-051`).

Согласие двух разворотов не обещается, а проверяется: тест сверяет
`convert.engine_settings(settings_of(recipe))` с `point_of(recipe)`
по **всей** сетке из 9 744 сочетаний. Разойдись они — шаблон обещал бы одно,
а посчитано было бы другое, и заметить это было бы нечем.

⚠️ Денежных полей в шаблон не пишется ни одного: `ui/templates.py`
отказывается от этого намеренно, чтобы не завести вторую правду о деньгах.
Цифры живут в записанных прогонах и в своде.

⚠️ В шаблон кладётся **настоящий контракт**, а не сшитый ряд `@MX`: заявку
по нему подать нельзя (решение 0049), а шаблон применяют кнопкой.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from app import convert
from app import runs as runs_log
from backtest.execution import Costs
from backtest.leaders import (
    LEADERS_WANTED,
    Recipe,
    Stretch,
    Study,
    bars_of,
    days_of,
    prepare,
    snapshot,
    study,
    summary_text,
)
from backtest.sweep import ForeignStrategy
from market.journal import RunOrigin
from market.paths import default_db_path
from market.storage import CandleStore
from ui.formatting import MSK
from ui.models import AfterTakeProfit, Mode, ReversalMoment, Settings
from ui.templates import EXAMPLES_DIR_NAME, TEMPLATES_FILE_NAME, Library, Template
from ui.version import version

__all__ = [
    "MACHINE_MARK",
    "NAME_ROOM",
    "main",
    "record",
    "settings_of",
    "template_name",
    "template_of",
]

#: Корень поставки: отсюда отсчитывается папка примеров. Тот же приём, что
#: у `market.paths.userdata_dir`, — от каталога пакета вверх.
REPO_ROOT = Path(__file__).resolve().parent.parent

#: Имя файла примеров. То же, что у библиотеки: формат один, и кнопка
#: «Импортировать…» читает его без переводчика (решение 0051).
EXAMPLES_FILE_NAME = TEMPLATES_FILE_NAME

#: С чего начинается имя примера. Пометка остаётся и после импорта: владелец
#: счёта обязан видеть в своём списке, что этот набор пришёл из поставки,
#: а не подобран им самим.
MACHINE_MARK = "Пример"

#: Предел длины имени шаблона (`ui.templates.NAME_LIMIT`). Повторён здесь
#: не для удобства: имя собирается по кускам, и сторож обязан ловить рост
#: любого куска, а не общий предел где-то в другом модуле.
NAME_ROOM = 60


# ---------------------------------------------------------------------------
# Рецепт → набор окна
# ---------------------------------------------------------------------------

def settings_of(recipe: Recipe, base: Settings) -> Settings:
    """Рецепт сетки → набор настроек окна. Всё прочее берётся у основы.

    ⚠️ Это **не перевод из настроек движка**, а второй разворот одного
    и того же рецепта. Разница существенная: перевод пришлось бы писать
    задом наперёд по таблице `app/convert.py`, а таблица не взаимно
    однозначна (`D-051`). Здесь же оба разворота идут из одного источника,
    и согласие проверяется тестом по всей сетке.
    """
    return base.replace(
        window_start=recipe.window_start,
        window_end=recipe.window_end,
        average_period=recipe.average_period,
        take_profit_enabled=recipe.take_on,
        take_profit_pct=recipe.take_percent,
        reversal_moment=(
            ReversalMoment.SAME_BAR if recipe.same_bar_reversal
            else ReversalMoment.NEXT_BAR
        ),
        after_take_profit=(
            AfterTakeProfit.STOP_FOR_THE_DAY if recipe.stop_after_take
            else AfterTakeProfit.WAIT_FOR_SIGNAL
        ),
    )


# ---------------------------------------------------------------------------
# Шаблоны: набор ложится в библиотеку, деньги — нет
# ---------------------------------------------------------------------------

def template_name(number: int, recipe: Recipe, *, origin: str) -> str:
    """Имя примера: пометка, номер, **происхождение** и чем набор отличается.

    Происхождение обязательно (решение 0051): без «на чём отобран» пример
    читается как рекомендация, а подбор в этом проекте дважды проиграл
    бездействию. Номер разводит сочетания, у которых совпал бы остаток.

    ⚠️ Длина стережётся: `ui.templates` обрезает имя до 60 знаков при чтении,
    и обрезанное имя потеряло бы как раз хвост. Режимы переворота и поведения
    после тейка в шестьдесят знаков не помещаются и живут во вкладке
    «Что в наборе».
    """
    size = f"{recipe.take_percent:.1f} %".replace(".", ",")
    window = (
        "весь день" if recipe.window_start == recipe.window_end
        else f"{recipe.window_start:%H:%M}–{recipe.window_end:%H:%M}"
    )
    name = (
        f"{MACHINE_MARK} {number:02d} · {origin} · {window} · "
        f"EMA{recipe.average_period} · {size}"
    )
    if len(name) > NAME_ROOM:
        raise ValueError(
            f"имя примера длиной {len(name)} знаков не поместится в {NAME_ROOM}: "
            f"«{name}». Библиотека обрежет его и потеряет хвост"
        )
    return name


def origin_of(symbol: str, period: object) -> str:
    """Откуда пример: инструмент и отрезок подбора, коротко.

    Коротко — потому что имя целиком помещается в шестьдесят знаков, а без
    происхождения имя не имеет права существовать вовсе. Месяц и год: точнее
    в отведённое место не влезает, а «19.06.2025–14.02.2026» съело бы окно
    и среднюю.
    """
    since = getattr(period, "since", None)
    until = getattr(period, "until", None)
    if since is None or until is None:
        return symbol
    return f"{symbol} {since:%m.%y}–{until:%m.%y}"


def template_of(  # noqa: PLR0913 — набор, происхождение и время: меньше не выходит
    number: int,
    recipe: Recipe,
    base: Settings,
    *,
    saved_at: datetime,
    origin: str,
    full_origin: str = "",
) -> Template:
    """Сочетание → пример настроек. **Ни одного денежного поля.**

    Прибыль, просадка и число сделок в шаблон не кладутся, и это не
    забывчивость: `ui/templates.py` отказывается от этого намеренно — цифры
    берутся из записанных прогонов, а считать их отдельно значило бы завести
    вторую правду о деньгах. Соблазн велик именно здесь: свод уже посчитан.

    Происхождение кладётся **дважды и намеренно**: коротко в имя, которое
    видно в строке списка, и полностью в поле `Template.origin`. Имя обязано
    нести его само (решение 0051), потому что поле показывается не везде
    и в файл записывается сборкой, которая про него уже знает, — а имя
    переживает и старую сборку, и пересылку файла на другую машину.
    """
    return Template(
        name=template_name(number, recipe, origin=origin),
        values=settings_of(recipe, base),
        saved_at=saved_at,
        origin=full_origin or origin,
    )


# ---------------------------------------------------------------------------
# Журнал прогонов: два отрезка на лидера, проверка записывается последней
# ---------------------------------------------------------------------------

def record(
    store: CandleStore, values: Settings, made: Sequence[Stretch], *, timeframe: str
) -> tuple[int, ...]:
    """Записать прогоны набора в журнал. **Порядок записи значим.**

    ⚠️ Отрезки пишутся в том порядке, в котором поданы, и последний из них
    окно шаблонов показывает строкой таблицы (`ui/templates_dialog.py`:
    `runs[0]`, а прогоны идут свежими первыми). Поэтому проверочный отрезок
    подаётся последним: в списке шаблонов обязано стоять число с проверки,
    а не с подбора. Поменяй порядок — и окно покажет пятнадцать зелёных
    чисел подбора, то есть ровно то, против чего заведено разделение.

    Возвращает номера записанных прогонов, в порядке записи.
    """
    conditions = runs_log.RunConditions(
        origin=RunOrigin.BACKTEST,
        symbol=convert.instrument_of(values.instrument),
        timeframe=timeframe,
        engine=convert.engine_settings(values, Mode.REVERSE),
        strategy=convert.strategy_settings(values),
        algorithm=convert.chosen_algorithm(values),
        app_version=version(),
    )
    written: list[int] = []
    for one in made:
        session = store.open_journal_session(
            conditions.record(one.bars), keep_backtest_sessions=runs_log.RUNS_KEPT
        )
        store.finish_journal_session(session.id, note=runs_log.result_note(one.run))
        written.append(session.id)
    return tuple(written)


# ---------------------------------------------------------------------------
# Запуск: python3 -m app.leaders
# ---------------------------------------------------------------------------

def arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    """Разобрать ключи командной строки."""
    parser = argparse.ArgumentParser(
        prog="python -m app.leaders",
        description="Перебор на длинной истории; лидеры подбора ложатся шаблонами.",
    )
    parser.add_argument("--db", default="userdata/chain.sqlite3",
                        help="база со свечами ряда перебора, только на чтение")
    parser.add_argument("--symbol", default="@MX",
                        help="ряд, на котором идёт перебор и выбираются лидеры")
    parser.add_argument("--contract", default="MXU6",
                        help="торгуемый контракт: он попадёт в шаблон и в журнал")
    parser.add_argument("--contract-db", default="",
                        help="база со свечами контракта; без ключа — база программы")
    parser.add_argument("--runs-db", default="",
                        help="база, куда пишутся прогоны; без ключа — база программы")
    parser.add_argument("--examples-dir", default="",
                        help="папка примеров; без ключа — examples/ рядом с программой")
    parser.add_argument("--record-runs", action="store_true",
                        help="записать два прогона каждого примера в журнал программы")
    parser.add_argument("--commission", type=float, default=14.0,
                        help="комиссия в рублях за контракт на сторону")
    parser.add_argument("--price-step", type=float, default=25.0,
                        help="шаг цены инструмента")
    parser.add_argument("--slippage", type=float, default=0.0,
                        help="проскальзывание перебора в шагах цены")
    parser.add_argument("--tuning-share", type=float, default=0.5,
                        help="какая доля торговых дней уходит на подбор")
    parser.add_argument("--checking-days", type=int, default=5,
                        help="торговых дней в одном окне скользящей проверки")
    parser.add_argument("--contract-tuning-days", type=int, default=36,
                        help="торговых дней на подбор в записываемых прогонах контракта")
    parser.add_argument("--leaders", type=int, default=LEADERS_WANTED,
                        help="сколько лидеров брать")
    parser.add_argument("--workers", type=int, default=11, help="сколько процессов считают")
    parser.add_argument("--every", type=int, default=1,
                        help="брать каждое N-е сочетание: быстрая проба вместо всей сетки")
    parser.add_argument("--out", default="", help="куда записать свод")
    parser.add_argument("--cache", default="",
                        help="файл с результатом перебора: есть — читается, нет — пишется")
    parser.add_argument("--apply", action="store_true",
                        help="записать шаблоны и прогоны; без ключа — только свод")
    parser.add_argument("--yes", action="store_true",
                        help="не спрашивать подтверждения на длинный перебор")
    return parser.parse_args(argv)


def base_settings(args: argparse.Namespace) -> Settings:
    """Набор, от которого считаются отличия сетки.

    Умолчания проекта **не меняются**: за ними замеры и решения. Здесь
    только то, чего в умолчаниях нет, — торгуемый контракт и издержки.
    """
    return Settings(
        instrument=args.contract,
        commission_per_side_rub=args.commission,
        price_step=args.price_step,
        slippage_steps=args.slippage,
    )


def examples_path(args: argparse.Namespace) -> Path:
    """Куда лечь файлу примеров.

    Папка примеров живёт **рядом с программой**, а не в `userdata/`: она
    едет с поставкой и обновляется вместе с ней, а файл владельца счёта
    остаётся его собственностью (решение 0051).
    """
    if args.examples_dir:
        return Path(args.examples_dir) / EXAMPLES_FILE_NAME
    return REPO_ROOT / EXAMPLES_DIR_NAME / EXAMPLES_FILE_NAME


def apply_results(
    made: Study,
    legs: Mapping[int, tuple[object, object, tuple[Stretch, ...]]],
    args: argparse.Namespace,
) -> list[str]:
    """Записать файл примеров и, если просили, прогоны. Строки — для человека.

    ⚠️ **Рабочий файл шаблонов владельца счёта здесь не открывается вовсе.**
    Пятнадцать машинных наборов вперемешку с его собственными через месяц
    не разделить, а следующая поставка не смогла бы обновить примеры,
    не споря с его правками (решение 0051).

    ⚠️ Журнал прогонов трогается **только по ключу** `--record-runs`. Без
    него примеры после импорта честно покажут «не запускался»: на истории
    владельца счёта они не гонялись, и число, взятое из чужого прогона,
    читалось бы как обещание.
    """
    base = base_settings(args)
    origin, full = _origins(made, args)
    stamp = datetime.now(tz=MSK)
    fresh = tuple(
        template_of(place, made.recipes[one.number], base,
                    saved_at=stamp, origin=origin, full_origin=full)
        for place, one in enumerate(made.picked, start=1)
    )
    target = examples_path(args)
    target.parent.mkdir(parents=True, exist_ok=True)
    library = Library(target.parent)
    trouble = library.write(fresh)
    said = [
        f"Примеров записано: {len(fresh)} в {library.path}. Рабочий файл "
        "шаблонов не тронут: примеры едут своей папкой (решение 0051).",
        f"Происхождение каждого примера: {full}.",
    ]
    if trouble:
        said.append(trouble)
    said.extend(_recorded(made, legs, args, base))
    return said


def _origins(made: Study, args: argparse.Namespace) -> tuple[str, str]:
    """Происхождение примера: коротко для имени и полностью для поля.

    Коротко — потому что имя целиком обязано уместиться в шестьдесят знаков.
    Полностью — потому что поле `Template.origin` показывается там, где место
    есть, и обязано называть **обе** половины истории: и на чём выбирали,
    и на чём проверяли.
    """
    tuning = made.ground.split.tuning
    checking = made.ground.split.checking
    return (
        origin_of(args.symbol, tuning),
        f"отобран перебором {len(made.scored)} сочетаний на ряду {args.symbol}: "
        f"подбор {tuning}, проверка {checking}. Это пример устройства перебора, "
        f"а не совет: на проверке из {len(made.picked)} лидеров выжили "
        f"{sum(1 for one in made.picked if one.checking.net > 0)}",
    )


def _recorded(
    made: Study,
    legs: Mapping[int, tuple[object, object, tuple[Stretch, ...]]],
    args: argparse.Namespace,
    base: Settings,
) -> list[str]:
    """Прогоны примеров в журнал — только по ключу `--record-runs`.

    ⚠️ Порядок внутри примера значим: подбор пишется первым, проверка второй.
    Окно шаблонов показывает строкой **последний** записанный прогон, и там
    обязано стоять число с проверки, а не с подбора.
    """
    if not args.record_runs:
        return [
            "Прогоны в журнал не писались: ключа `--record-runs` не было. "
            "После импорта примеры покажут «не запускался» — это правда, "
            "на вашей истории они не гонялись. Числа — в этом своде."
        ]
    missing = [one.number for one in made.picked if one.number not in legs]
    said: list[str] = []
    if missing:
        said.append(
            f"⚠️ У {len(missing)} примеров из {len(made.picked)} прогонов "
            "по настоящему контракту нет: они не считались."
        )
    database = Path(args.runs_db) if args.runs_db else default_db_path()
    written: list[int] = []
    with CandleStore(database) as store:
        for one in made.picked:
            if one.number not in legs:
                continue
            values = settings_of(made.recipes[one.number], base)
            written.extend(
                record(store, values, legs[one.number][2], timeframe=values.timeframe)
            )
    said.append(
        f"Прогонов записано: {len(written)} в {database}, номера "
        f"{written[0]}…{written[-1]}." if written else "Прогонов не записано."
    )
    return said


async def work(args: argparse.Namespace) -> int:
    """Весь замер: снимки баз, расчёт в торговом слое, свод и запись."""
    base = base_settings(args)
    source = Path(args.db)
    with tempfile.TemporaryDirectory(prefix="terminal-leaders-") as room:
        copy = snapshot(source, Path(room))
        bars = bars_of(copy, args.symbol)
        contract_db = snapshot(
            Path(args.contract_db) if args.contract_db else default_db_path(),
            Path(room) / "contract",
        )
        ground = prepare(
            copy, args.symbol,
            engine=convert.engine_settings(base, Mode.REVERSE),
            strategy=convert.strategy_settings(base),
            strategy_id=base.strategy_id,
            days=days_of(bars),
            costs=Costs(
                commission_per_side=args.commission,
                price_step=args.price_step,
                slippage_steps=args.slippage,
            ),
            tuning_share=args.tuning_share, checking_days=args.checking_days,
            every=max(1, args.every), workers=args.workers, wanted=args.leaders,
            contract=args.contract, contract_database=contract_db,
            contract_tuning_days=args.contract_tuning_days,
        )
        made, legs = await study(
            ground, source=source,
            cache=Path(args.cache) if args.cache else None, asked=args.yes,
        )
        said = apply_results(made, legs, args) if args.apply else [
            "Ключ `--apply` не задан: шаблоны и прогоны не записаны."
        ]
        text = summary_text(replace(made, said=tuple(said)))
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
            print(f"Свод записан: {args.out}", file=sys.stderr)
        else:
            print(text)
        for line in said:
            print(line, file=sys.stderr)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Точка входа. Возвращает код возврата процесса.

    ⚠️ Отказ перебора — **фраза и код возврата**, а не трассировка. Сетка
    написана под один торговый алгоритм и объявляет это сама
    (`backtest/sweep.py::GRID_STRATEGY_ID`); выбран другой — подбирать
    нечего, и сказать это надо словами. Трассировка в консоль здесь была бы
    тем самым молчанием, которое дороже поломки (`CLAUDE.md` №13).
    """
    try:
        return asyncio.run(work(arguments(argv)))
    except ForeignStrategy as refusal:
        print(f"Перебор не запущен: {refusal}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
