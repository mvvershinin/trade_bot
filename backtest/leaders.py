"""Перебор на длинной истории и пятнадцать лидеров подбора.

Просьба владельца счёта 05.09.2026, дословно: «прогнать варианты, которые ты
сам создашь, выбрать год и период действия контракта, выбрать лидеров штук 15
и их сохранить как шаблоны, вывести свод и сохранить в файл».

Чем это отличается от `backtest/sweep.py`
-----------------------------------------
`sweep` отвечает на вопрос «что показывает перебор». Здесь его результат
доводится до конца пути: из сетки выбираются лидеры, каждый считается ещё
и на настоящем контракте, и всё это складывается в свод, который читает
человек. Сохранением шаблонов и записью прогонов занимается слой сборки —
у `backtest/` нет ни библиотеки шаблонов, ни журнала (ARCHITECTURE.md §2).

Ловушка, ради которой модуль написан осторожно
----------------------------------------------
«Пятнадцать лучших за период» — это ровно то, что все замеры проекта называют
выдумкой: победитель подбора оказывался на проверке ниже середины в 30 окнах
из 48, а переподстройка проиграла нетронутым умолчаниям 11 386 ₽. Поэтому:

* **лидеры выбираются только по отрезку подбора** (`leaders`), и посмотреть
  в проверку выбор не может по устройству — в функцию не подаётся ничего,
  кроме подбора;
* **проверка стоит рядом в каждой строке** свода;
* **валовая прибыль стоит отдельной колонкой рядом с чистой**. Замер
  05.09.2026 на 280 днях: валовая за год +32 ₽ при комиссии 33 964 ₽. При
  нулевой валовой перебор окна и тейка усиливать нечего, и пятнадцать
  «лидеров» окажутся пятнадцатью способами по-разному проиграть комиссии.

Почему сочетание описывается `Recipe`, а не готовыми настройками движка
----------------------------------------------------------------------
Сетку разворачивают **два слоя**: этот — в настройки движка, слой сборки —
в набор окна, который ложится в шаблон. Обратного перевода «настройки движка →
набор окна» в программе нет и заводить его нельзя (`D-051`), а две сетки,
написанные порознь, разошлись бы молча: шаблон обещал бы одно, а посчитано
было бы другое.

`Recipe` — простые величины: время, число, флаг. Ни словаря движка, ни словаря
окна. Каждый слой разворачивает её в своё, а согласие двух разворотов
проверяется тестом по **всей** сетке, а не обещанием.
"""

from __future__ import annotations

import asyncio
import math
import pathlib
import pickle
import sqlite3
import sys
import time as clock
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta

from backtest.execution import Costs
from backtest.history import Deal, HistoryRun, deal_results, replay
from backtest.overfitting import Shape, deflated_sharpe, overfit_share, rank_of
from backtest.split import (
    SWEEP_SPAN,
    Fold,
    Period,
    single_split,
    trading_days,
    walk_forward,
)
from backtest.sweep import (
    AVERAGE_PERIODS,
    GRID_STRATEGY_ID,
    MINUTES_IN_HOUR,
    TAKE_PERCENTS,
    WINDOW_DURATIONS,
    WINDOW_STARTS,
    Money,
    Point,
    grid_strategy,
    lay_out,
    refuse_foreign_strategy,
)
from backtest.table import plural
from engine import MSK, EngineSettings, Reversal, TradingWindow, in_moscow
from market.aggregate import build_bars
from market.candles import M5, Candle
from market.chain import daily_volume, legs_of_chain
from market.storage import CandleStore
from strategies import EmaReverseSettings, StrategySettings

__all__ = [
    "CHAIN",
    "ENOUGH_TRADES",
    "LEADERS_WANTED",
    "PACE",
    "Cut",
    "Ground",
    "Recipe",
    "Scored",
    "Stretch",
    "Study",
    "around",
    "bars_of",
    "days_of",
    "halves",
    "leaders",
    "point_of",
    "prepare",
    "recipes",
    "reference_recipes",
    "snapshot",
    "study",
    "summary_text",
    "trim",
]

#: Сколько лидеров просил владелец счёта. Число его, а не выведенное.
LEADERS_WANTED = 15

#: Сколько сделок обязано быть на подборе, чтобы набор вообще рассматривался.
#:
#: Без порога наверх всплывает набор с тремя сделками и одним везучим днём:
#: узкое окно в конце дня даёт единицы входов, и деньги у него случайны
#: по построению. Сорок — это примерно одна сделка на четыре дня, то есть
#: нижняя граница, при которой слово «статистика» ещё что-то значит. Число
#: названо здесь, печатается в своде и ничем не подпирается, кроме этого
#: довода: замера, который выбрал бы его точнее, у проекта нет.
ENOUGH_TRADES = 40

#: Цепочка контрактов, из которой собран ряд `@MX`. Нужна, чтобы найти дни
#: стыков: в такой день средняя считает ценовой разрыв соседних контрактов
#: ходом рынка, и сделки этого дня — артефакт склейки (`D-057`).
CHAIN = ("MXM5", "MXU5", "MXZ5", "MXH6", "MXM6", "MXU6")

#: Секунд на прогон одного сочетания по ряду `@MX` (64 407 пятиминуток).
#: Замер 06.09.2026 по шести углам сетки: от 8,16 до 9,56 с, и **ширина окна
#: почти не влияет** — время съедает обход ряда, а не число сделок.
#: Это опровергает прежнюю оценку `backtest/__main__.py::PACE`, снятую
#: на умолчаниях (`D-069`).
PACE = 8.8 / 64_407

#: Дольше этого перебор считается долгим, и на него спрашивают согласия.
LONG_ENOUGH_TO_ASK = 300.0

#: Секунд в минуте. Названо, чтобы `60` рядом с размером свечи не путалось.
A_MINUTE = 60


# ---------------------------------------------------------------------------
# Сочетание в простых величинах
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Recipe:
    """Одно сочетание настроек: время, числа, флаги. Ни одного словаря слоя.

    Разворачивается в настройки движка здесь (`point_of`) и в набор окна
    в слое сборки. Согласие двух разворотов проверяется тестом по всей сетке.

    `take_percent` живёт рядом с `take_on` и **не обнуляется** при выключенном
    тейке: так оба разворота ставят одно и то же число, и сравнить их можно
    целиком, а не по кускам.
    """

    label: str
    window_start: time
    window_end: time
    average_period: int
    take_percent: float
    take_on: bool = True
    same_bar_reversal: bool = False
    stop_after_take: bool = True
    #: Положение на осях сетки. Пусто — точка вне сетки, соседей нет.
    axes: tuple[tuple[str, float], ...] = ()


def point_of(
    recipe: Recipe, engine: EngineSettings, strategy: EmaReverseSettings
) -> Point:
    """Рецепт → сочетание для прогона. Всё остальное берётся у поданной основы."""
    return Point(
        label=recipe.label,
        engine=replace(
            engine,
            window=TradingWindow(start=recipe.window_start, end=recipe.window_end),
            take_profit=recipe.take_on,
            take_profit_percent=recipe.take_percent,
            reversal=Reversal.SAME_BAR if recipe.same_bar_reversal else Reversal.THROUGH_BAR,
            stop_after_take_profit=recipe.stop_after_take,
        ),
        strategy=replace(strategy, period=recipe.average_period),
        axes=recipe.axes,
    )


def _end_of(start: time, span: timedelta) -> time:
    """Конец окна: начало плюс длительность, внутри тех же суток."""
    total = int(start.hour * MINUTES_IN_HOUR + start.minute
                + span.total_seconds() / MINUTES_IN_HOUR)
    return time(total // MINUTES_IN_HOUR, total % MINUTES_IN_HOUR)


def windows() -> tuple[tuple[str, time, time, tuple[tuple[str, float], ...]], ...]:
    """Границы окна: сетка ТЗ §8, плюс весь день, плюс нынешнее умолчание.

    Строка «весь торговый день» обязательна (ТЗ §8): без неё нельзя отличить
    работающее сужение окна от подгонки. Умолчание 10:05–11:00 на сетке ТЗ
    не стоит (10:05 не кратно пятнадцати минутам), и добавлено отдельно —
    иначе таблица сравнивала бы варианты между собой, но не с тем, что стоит
    в программе сегодня.

    У обеих добавленных строк осей нет: они не на сетке, и соседства у них
    не бывает по природе.
    """
    made: list[tuple[str, time, time, tuple[tuple[str, float], ...]]] = [
        (
            f"{start:%H:%M}–{_end_of(start, span):%H:%M}",
            start,
            _end_of(start, span),
            (
                ("start", float(start.hour * MINUTES_IN_HOUR + start.minute)),
                ("duration", span.total_seconds() / MINUTES_IN_HOUR),
            ),
        )
        for start in WINDOW_STARTS
        for span in WINDOW_DURATIONS
    ]
    made.append(("весь день", time(0, 0), time(0, 0), ()))
    made.append(("10:05–11:00", time(10, 5), time(11, 0), ()))
    return tuple(made)


#: Момент переворота: подпись для имени шаблона, флаг и значение оси.
REVERSALS: tuple[tuple[str, bool, float], ...] = (
    ("через", False, 0.0),
    ("в одной", True, 1.0),
)

#: Поведение после тейка. Третьего варианта («сразу восстановить позицию»)
#: движком не выражено вовсе — это недобор относительно ТЗ §8, и он назван
#: в своде, а не замолчан.
AFTER_TAKE: tuple[tuple[str, bool, float], ...] = (
    ("стоп", True, 0.0),
    ("ждать", False, 1.0),
)


def recipes() -> tuple[Recipe, ...]:
    """Полное произведение обязательной программы ТЗ §8: все оси разом.

    Порядок сочетаний **детерминирован**, и от этого зависит правильность:
    рабочие процессы перебора собирают ту же сетку заново и обращаются к ней
    по номеру. Перестановка циклов молча сдвинула бы номера.
    """
    made: list[Recipe] = []
    for label, start, end, window_axes in windows():
        for period in AVERAGE_PERIODS:
            for take in TAKE_PERCENTS:
                for turn, same_bar, turn_axis in REVERSALS:
                    for after, stop, stop_axis in AFTER_TAKE:
                        # Точка вне сетки соседей не имеет: у «весь день»
                        # и у нынешнего умолчания соседства не бывает.
                        axes = () if not window_axes else window_axes + (
                            ("period", float(period)),
                            ("take", take),
                            ("reversal", turn_axis),
                            ("stop", stop_axis),
                        )
                        size = f"{take:.1f} %".replace(".", ",")
                        made.append(Recipe(
                            label=f"{label} · EMA{period} · {size} · {turn} · {after}",
                            window_start=start, window_end=end,
                            average_period=period, take_percent=take,
                            same_bar_reversal=same_bar, stop_after_take=stop,
                            axes=axes,
                        ))
    return tuple(made)


#: Два окна, которыми меряются опорные строки: нынешнее умолчание программы
#: и весь торговый день.
REFERENCE_WINDOWS: tuple[tuple[str, time, time], ...] = (
    ("10:05–11:00", time(10, 5), time(11, 0)),
    ("весь день", time(0, 0), time(0, 0)),
)

#: Тейк умолчания программы. За ним замеры и сверка с прототипом.
DEFAULT_TAKE = 0.5

#: Период средней умолчания программы.
DEFAULT_PERIOD = 15


def reference_recipes() -> tuple[tuple[Recipe, str], ...]:
    """Опорные наборы: с чем сравнивать лидеров.

    Умолчания программы на двух окнах и то же самое **без тейк-профита**.
    Последнее стоит здесь не для полноты: в DOMAIN.md §4 записано, что без
    тейк-профита стратегия убыточна на любом окне. Утверждение проверяемое,
    и раз перебор всё равно идёт — оно проверяется, а не пересказывается.
    """
    made: list[tuple[Recipe, str]] = []
    for label, start, end in REFERENCE_WINDOWS:
        made.append((
            Recipe(
                label=f"{label} · EMA{DEFAULT_PERIOD} · 0,5 % · через · стоп",
                window_start=start, window_end=end,
                average_period=DEFAULT_PERIOD, take_percent=DEFAULT_TAKE,
            ),
            f"{label}: умолчания программы",
        ))
        made.extend(
            (
                Recipe(
                    label=f"{label} · EMA{period} · без тейка",
                    window_start=start, window_end=end,
                    average_period=period, take_percent=DEFAULT_TAKE, take_on=False,
                ),
                f"{label}: EMA{period}, тейк выключен",
            )
            for period in AVERAGE_PERIODS
        )
    return tuple(made)


@dataclass(frozen=True, slots=True)
class Cut:
    """Деньги и форма одного отрезка. Валовая стоит рядом с чистой всегда.

    Отдельный тип, а не `backtest.sweep.Money`, ровно по одной причине:
    перебор идёт в отдельных процессах, и обратно едут только простые числа.
    Всё, что здесь есть, посчитано `backtest.history.summarise` и только ею —
    арифметика отчёта живёт в одном месте.

    ⚠️ `trades == 0` означает «сделок не было», и деньги при этом ноль
    по построению, а не «дало ноль рублей». Различать эти два случая читатель
    обязан по `trades`, а не по нулю в рублях.
    """

    trades: int = 0
    profitable: int = 0
    gross: float = 0.0
    commission: float = 0.0
    net: float = 0.0
    drawdown: float = 0.0
    reversals: int = 0
    profit_factor: float | None = None
    best: float = 0.0
    worst: float = 0.0
    #: Форма распределения результатов сделок: нужна поправке Шарпа
    #: и стандартной ошибке итога.
    spread: float = 0.0
    skew: float = 0.0
    kurtosis: float = 0.0
    mean: float = 0.0

    @classmethod
    def of(cls, deals: Sequence[Deal]) -> "Cut":
        """Сжать итог по списку сделок."""
        return cls.made(Money.of(deals), Shape.of(deal_results(deals)))

    @classmethod
    def made(cls, money: Money, form: Shape) -> "Cut":
        """Сжать уже посчитанные деньги и форму отрезка.

        Пара к `of`: `lay_out` раскладывает сделки по отрезкам и считает
        и то и другое один раз, а считать их второй раз по тем же сделкам
        значило бы платить за одно и то же дважды.
        """
        return cls(
            trades=money.trades,
            profitable=money.profitable,
            gross=money.gross,
            commission=money.commission or 0.0,
            net=money.rubles,
            drawdown=money.drawdown or 0.0,
            reversals=money.reversals,
            profit_factor=money.profit_factor,
            best=money.best or 0.0,
            worst=money.worst or 0.0,
            spread=form.spread,
            skew=form.skew,
            kurtosis=form.kurtosis,
            mean=form.mean,
        )

    @property
    def shape(self) -> Shape:
        """Форма распределения обратно объектом: её ждут поправки."""
        return Shape(count=self.trades, mean=self.mean, spread=self.spread,
                     skew=self.skew, kurtosis=self.kurtosis)

    @property
    def error_of_total(self) -> float:
        """Стандартная ошибка итога в рублях. Ставится рядом с прибылью всегда."""
        return self.shape.error_of_total


@dataclass(frozen=True, slots=True)
class Scored:
    """Одно сочетание, посчитанное на всех отрезках.

    `checking` рядом с `tuning` не для полноты: это железное правило проекта.
    Таблица результатов, где есть только колонка подбора, отклоняется.
    """

    number: int
    tuning: Cut
    checking: Cut
    whole: Cut
    #: Чистая прибыль по каждому проверочному окну скользящей проверки —
    #: в порядке окон. Нужна доле провалов (PBO).
    fold_checking: tuple[float, ...] = ()
    #: То же по отрезкам подбора этих окон: по ним ищется победитель окна.
    fold_tuning: tuple[float, ...] = ()
    #: Сколько сделок подбора вошло в день стыка контрактов.
    seam_trades: int = 0
    #: Робот остановился на прогоне: причина. Пусто — дошёл до конца.
    halted: str = ""


def snapshot(source: pathlib.Path, into: pathlib.Path) -> pathlib.Path:
    """Снять копию базы, **не открывая оригинал на запись**.

    Причин две, и вторая важнее первой: `market.storage.CandleStore`
    открывает базу на запись и выполняет миграции, а перебор идёт минутами,
    и программа в это время может дописывать свечи. Считать разные строки
    одной таблицы на разных данных нельзя.

    :raises SystemExit: базы нет или её не удалось прочитать. Отказ громкий
        и с путём: молчаливый пустой перебор выглядел бы как «сделок нет».
    """
    if not source.exists():
        raise SystemExit(f"базы со свечами нет: {source}")
    into.mkdir(parents=True, exist_ok=True)
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


def bars_of(copy: pathlib.Path, symbol: str) -> list[Candle]:
    """Пятиминутки инструмента из снимка базы, по возрастанию времени.

    Глубина не ограничивается ничем: берётся **вся** история ряда. Ключ
    `--days` у `python -m backtest` режет её до 400 дней молча (`D-056`),
    и владелец счёта, попросивший год, получал 280 дней из 312.
    """
    with CandleStore(copy) as store:
        minutes = store.minutes(symbol, since=None)
    if not minutes:
        raise SystemExit(
            f"в базе нет свечей по {symbol}. Загрузите историю прежде, "
            "чем перебирать настройки"
        )
    return list(build_bars(minutes, M5, on_duplicate="skip"))


def days_of(bars: Sequence[Candle]) -> tuple[date, ...]:
    """Торговые дни ряда: будние и с полной историей часов перебора."""
    since, until = SWEEP_SPAN
    counted: Counter[date] = Counter()
    for bar in bars:
        moment = in_moscow(bar.time)
        if since <= moment.time() < until:
            counted[moment.date()] += 1
    return trading_days(list(counted.items()))


def trim(bars: Sequence[Candle], days: Sequence[date]) -> list[Candle]:
    """Отрезать ряд по первому и последнему торговому дню.

    Свечи раньше первого торгового дня дают сделки, не попадающие ни
    в подбор, ни в проверку: они считаются, тратят время и не идут никуда.
    Цена решения названа вслух: средняя стартует холодной на первом дне
    подбора — одинаково для всех сочетаний и целиком внутри подбора.
    """
    first, last = days[0], days[-1]
    return [bar for bar in bars if first <= in_moscow(bar.time).date() <= last]


def seam_days(copy: pathlib.Path, chain: Sequence[str] = CHAIN) -> frozenset[date]:
    """Дни, в которые ряд переходит с одного контракта на следующий.

    В такой день средняя считает ценовой разрыв соседних контрактов ходом
    рынка: замер 05.09.2026 — разрыв до 3,20 % цены, сторона средней
    расходится с честной в 10 барах торгового окна из 11 (`D-057`). Сделка
    такого дня — артефакт склейки, и её долю в итоге лидера надо назвать.

    Рубежи **считаются заново тем же кодом**, которым ряд собирался
    (`market.chain`), а не разбираются из текста отчёта о сборке: разбор
    текста завёл бы вторую правду о том, где режется стык.
    """
    with CandleStore(copy) as store:
        volumes = [(symbol, daily_volume(store, symbol)) for symbol in chain]
    legs = legs_of_chain([pair for pair in volumes if pair[1]])
    return frozenset(leg.since for leg in legs[1:])


@dataclass(frozen=True, slots=True)
class Ground:
    """Условия перебора: ряд, отрезки, издержки. Одни на все сочетания.

    Разные условия у разных строк таблицы означали бы, что таблица сравнивает
    не настройки, а данные.
    """

    database: pathlib.Path
    symbol: str
    #: Умолчания движка и торгового модуля, на которые сетка кладёт свои
    #: отличия. Приходят от слоя сборки готовыми: набор окна и настройки
    #: движка обязаны совпасть по построению, а перевод между ними живёт
    #: в одном месте (`app/convert.py`).
    engine: EngineSettings
    strategy: EmaReverseSettings
    #: Имя алгоритма, чьи настройки лежат в `strategy`. Сетка написана
    #: под один алгоритм и объявляет это (`backtest/sweep.py`); чужой —
    #: отказ вслух, а не молчаливый подбор по чужим полям.
    strategy_id: str
    split: Fold
    folds: tuple[Fold, ...]
    seams: frozenset[date]
    costs: Costs
    #: Брать каждое N-е сочетание сетки. 1 — всю сетку. Больше единицы —
    #: быстрая проба на настоящих данных: путь расчёта тот же, а ждать
    #: не час. Живёт в условиях, а не в ключе командной строки, потому что
    #: рабочий процесс собирает сетку сам и обязан сократить её так же.
    every: int = 1
    #: Сколько процессов считает сетку. Один — считать в этом же.
    workers: int = 1
    #: Сколько лидеров брать с подбора.
    wanted: int = LEADERS_WANTED
    #: Настоящий торгуемый контракт: снимок базы, код и деление истории.
    #: Пусто — прогонов по контракту не будет вовсе.
    contract: str = ""
    contract_database: pathlib.Path | None = None
    contract_tuning_days: int = 36

    def __post_init__(self) -> None:
        """Условия согласованы с сеткой — или отказ вслух (ловушка 14).

        Проверка стоит в **модели данных**: через эти поля настройки
        алгоритма расходятся по всему замеру, включая рабочие процессы,
        и перехватывать их в каждой двери означало бы забыть одну.
        """
        refuse_foreign_strategy(self.strategy_id, self.strategy)


@dataclass(slots=True)
class _Desk:
    """Рабочее место процесса перебора: ряд свечей, сетка и условия.

    Заполняется один раз при запуске процесса. Ряд в 64 тысячи свечей нельзя
    пересылать на каждое сочетание, а сетку из 9 744 наборов нельзя
    пересылать вовсе: по проводу уезжает номер, а не набор.
    """

    bars: list[Candle] = field(default_factory=list)
    recipes: tuple[Recipe, ...] = ()
    ground: Ground | None = None
    periods: tuple[Period, ...] = ()


#: Единственное рабочее место процесса. Значение, а не набор глобальных
#: переменных: заполнить его наполовину нельзя, а `global` в четырёх строках
#: подряд читается как четыре разных состояния.
_DESK = _Desk()


def _periods_of(ground: Ground) -> tuple[Period, ...]:
    """Все отрезки, по которым раскладываются сделки, без повторов.

    Отрезок подбора одного деления и отрезок подбора первого окна — один
    и тот же по значению, и считать его дважды незачем.
    """
    named = [ground.split.tuning, ground.split.checking]
    for fold in ground.folds:
        named.extend((fold.tuning, fold.checking))
    return tuple(dict.fromkeys(named))


def sit_down(ground: Ground) -> None:
    """Занять рабочее место процесса: прочитать ряд и собрать сетку.

    Зовётся один раз на процесс. Публичная, потому что её же зовёт
    однопоточный прогон и тест: перебор без процессов обязан считать
    ровно то же самое.
    """
    bars = bars_of(ground.database, ground.symbol)
    _DESK.bars = trim(bars, days_of(bars))
    _DESK.recipes = recipes()[::ground.every]
    _DESK.ground = ground
    _DESK.periods = _periods_of(ground)


def measure(number: int) -> Scored:
    """Посчитать одно сочетание по его номеру в сетке.

    Прогон **один на всё сочетание**, а не по прогону на отрезок, и это
    не оптимизация, а вопрос правильности: движок работает непрерывно,
    средняя прогрета предыдущими днями, позиция переходит через границу.
    Отдельный прогон с первого дня проверки стартовал бы с холодной средней
    и давал бы сделки, которых у непрерывно работающего робота не было.

    Заглядывания вперёд это не создаёт: выбор настроек смотрит только
    в сделки, целиком лежащие в отрезке подбора (`Period.holds`).
    """
    ground = _DESK.ground
    if ground is None:
        raise RuntimeError(
            "процесс перебора не занял рабочее место: sit_down не зван. "
            "Считать нечего — ряда свечей и сетки в этом процессе нет"
        )
    return asyncio.run(
        score(_DESK.recipes[number], _DESK.bars, ground, _DESK.periods, number=number)
    )


async def score(
    one: Recipe,
    bars: Sequence[Candle],
    ground: Ground,
    periods: Sequence[Period],
    *,
    number: int = 0,
) -> Scored:
    """Посчитать одно сочетание на готовом ряде. Ни одного обращения к базе.

    Отделена от `measure` намеренно: тем же кодом считаются и сетка
    в рабочих процессах, и добавочные строки свода в главном. Две копии
    этого расчёта означали бы две колонки, посчитанные по-разному.
    """
    point = point_of(one, ground.engine, ground.strategy)
    run = await replay(
        bars,
        # Алгоритм собирает реестр по имени, под которое написана сетка
        # (`backtest/sweep.py::GRID_STRATEGY_ID`), а не этот файл по имени
        # класса: подбор, объявивший себя написанным под один алгоритм
        # и гоняющий другой, врал бы числами, а не падал.
        grid_strategy().build(point.strategy),
        replace(point.engine, commission_per_side=ground.costs.per_side),
        costs=ground.costs,
    )
    trial = lay_out(point, run.deals, periods, halted=run.halted)
    return Scored(
        number=number,
        tuning=Cut.made(trial.on(ground.split.tuning), trial.form(ground.split.tuning)),
        checking=Cut.made(
            trial.on(ground.split.checking), trial.form(ground.split.checking)
        ),
        whole=Cut.made(trial.total, Shape.of(deal_results(run.deals))),
        fold_checking=tuple(trial.on(fold.checking).rubles for fold in ground.folds),
        fold_tuning=tuple(trial.on(fold.tuning).rubles for fold in ground.folds),
        seam_trades=sum(
            1 for deal in run.deals if in_moscow(deal.entry_time).date() in ground.seams
        ),
        halted=run.halted,
    )


def run_sweep(
    ground: Ground, count: int, *, workers: int, told: Sequence[int] = ()
) -> tuple[Scored, ...]:
    """Прогнать всю сетку. Порядок результата — порядок сетки.

    Процессы, а не потоки: цикл движка держит интерпретатор, и вторая нить
    не ускорила бы ничего. Ряд читается каждым процессом самостоятельно
    из **одного и того же снимка базы** — то есть данные у всех строк
    таблицы одни, что и требуется от таблицы сравнения.

    :param told: номера, на которых печатать ход. Пусто — молча.
    """
    if workers <= 1:
        sit_down(ground)
        return tuple(measure(number) for number in range(count))
    found: list[Scored | None] = [None] * count
    with ProcessPoolExecutor(
        max_workers=workers, initializer=sit_down, initargs=(ground,)
    ) as pool:
        for done, one in enumerate(pool.map(measure, range(count), chunksize=8), start=1):
            found[one.number] = one
            if done in told:
                print(f"  посчитано {done} из {count}", file=sys.stderr)
    missing = [number for number, one in enumerate(found) if one is None]
    if missing:
        raise RuntimeError(
            f"перебор вернул не все сочетания: не хватает {len(missing)} "
            f"из {count}. Таблица была бы неполной и об этом не сказала бы"
        )
    return tuple(one for one in found if one is not None)


def leaders(
    scored: Sequence[Scored], *, wanted: int = LEADERS_WANTED, enough: int = ENOUGH_TRADES
) -> tuple[Scored, ...]:
    """Лучшие сочетания **по отрезку подбора**. Проверка сюда не входит.

    ⚠️ Здесь и живёт главная страховка задачи. Отбор смотрит в `tuning`
    и больше никуда: ни `checking`, ни `whole` в порядке не участвуют.
    Лидер, отобранный на полном отрезке, — это подгонка по построению,
    и таблица при этом выглядит ровно так же, как честная.

    Порог `enough` отсекает наборы с горсткой сделок: без него наверх
    всплывает сочетание с тремя сделками и одним везучим днём. Сделок меньше
    порога — набор не рассматривается вовсе, а не понижается в порядке:
    «мало данных» и «плохой результат» — разные вещи.

    Равные деньги разводятся числом сделок, а потом номером в сетке: порядок
    обязан быть один и тот же при каждом прогоне, иначе «пятнадцать лидеров»
    менялись бы от запуска к запуску.
    """
    fit = [one for one in scored if one.tuning.trades >= enough]
    fit.sort(key=lambda one: (-one.tuning.net, -one.tuning.trades, one.number))
    return tuple(fit[:wanted])


def around(
    axes: Sequence[tuple[tuple[str, float], ...]], here: int
) -> tuple[int, ...]:
    """Соседи точки по сетке: одна ось отличается на один шаг, прочие равны.

    То же определение, что у `backtest.overfitting.neighbours`, и согласие
    с ней проверяется тестом, а не обещанием. Отдельная функция нужна
    из-за размера: `neighbours` сравнивает каждую точку с каждой, и на 9 744
    точках это 95 миллионов сравнений. Здесь ход обратный — соседи ищутся
    по словарю положений, и цена не зависит от размера сетки.

    Точка без осей соседей не имеет: у «весь день» и у нынешнего умолчания
    соседства не бывает по природе, и выдумывать его нельзя.
    """
    mine = axes[here]
    if not mine:
        return ()
    steps: dict[str, list[float]] = {}
    for item in axes:
        for name, value in item:
            steps.setdefault(name, []).append(value)
    lines = {name: sorted(set(values)) for name, values in steps.items()}
    where = {item: number for number, item in enumerate(axes)}
    found: list[int] = []
    for spot, (name, value) in enumerate(mine):
        line = lines[name]
        at = line.index(value)
        for step in (-1, 1):
            if not 0 <= at + step < len(line):
                continue
            moved = list(mine)
            moved[spot] = (name, line[at + step])
            number = where.get(tuple(moved))
            if number is not None and number != here:
                found.append(number)
    return tuple(sorted(found))


@dataclass(frozen=True, slots=True)
class Stretch:
    """Отрезок ряда и прогон по нему: то, что ложится строкой в журнал."""

    period: Period
    bars: tuple[Candle, ...]
    run: HistoryRun


def halves(days: Sequence[date], *, tuning: int) -> tuple[Period, Period]:
    """Ряд пополам: первые `tuning` торговых дней и весь остаток.

    Границы берутся у `backtest.split`, а не считаются здесь: правило
    «подбор не заходит на проверку» стережёт `Fold`, и обойти его
    параметром нельзя.
    """
    split = single_split(days, tuning=tuning)
    return split.tuning, split.checking


def part(bars: Sequence[Candle], period: Period) -> tuple[Candle, ...]:
    """Свечи, попавшие в отрезок. Обе границы включительно."""
    return tuple(bar for bar in bars if period.contains(in_moscow(bar.time).date()))


async def legs_of(  # noqa: PLR0913 — шесть доводов: рецепт, ряд, отрезки, издержки и обе основы
    recipe: Recipe,
    bars: Sequence[Candle],
    periods: Sequence[Period],
    costs: Costs,
    *,
    engine: EngineSettings,
    strategy: EmaReverseSettings,
) -> tuple[Stretch, ...]:
    """Прогнать набор по каждому отрезку отдельно и вернуть отрезки с итогами.

    Отдельный прогон на отрезок, а не один прогон с раскладкой сделок, —
    и это разница по существу. В журнал ложится запись, которую владелец
    счёта обязан суметь повторить: он выберет этот период в программе
    и получит те же числа. Раскладка сделок общего прогона дала бы другие:
    средняя на границе была бы прогрета предыдущими днями.

    Цена названа вслух: **средняя стартует холодной на первом дне каждого
    отрезка**, и первые пятнадцать баров решений не дают.
    """
    point = point_of(recipe, engine, strategy)
    made: list[Stretch] = []
    for period in periods:
        inside = part(bars, period)
        run = await replay(
            inside, grid_strategy().build(point.strategy),
            replace(point.engine, commission_per_side=costs.per_side), costs=costs,
        )
        made.append(Stretch(period=period, bars=inside, run=run))
    return tuple(made)


def rubles(value: float) -> str:
    """Деньги для таблицы свода: знак обязателен, разряды отбиты."""
    return f"{value:+,.0f}".replace(",", " ").replace("-", "−")


def verdict(picked: Sequence[Scored]) -> str:
    """Ответ словами первой строкой: что дал отбор и есть ли чему верить.

    Пишется **до** таблицы и без обиняков. Владелец счёта спрашивал не «кто
    лучший», а «можно ли вообще выбрать настройки перебором»; таблица на этот
    вопрос не отвечает, а строка отвечает.
    """
    if not picked:
        return (
            "**Ни одно сочетание не прошло порог по числу сделок — лидеров нет.** "
            "Выбирать не из чего, и это ответ, а не сбой."
        )
    alive = sum(1 for one in picked if one.checking.net > 0)
    earning = sum(1 for one in picked if one.tuning.gross > 0)
    return (
        f"**Из {len(picked)} лидеров подбора проверку пережили {alive}.** "
        f"Валовая прибыль (до комиссии) положительна на подборе "
        f"у {earning} из {len(picked)}."
    )


def conditions_lines(study: Study, trials: int) -> list[str]:
    """Условия перебора: на чём, чем и сколько. Без них числа ничего не значат."""
    ground, days = study.ground, study.days
    penalty = f"ln(N) = {math.log(trials):.2f}" if trials > 1 else "—"
    span = study.span or (days[0], days[-1])
    return [
        f"* **Ряд:** `{ground.symbol}`, пятиминутки, база `{study.source}`, "
        "только на чтение.",
        f"* **В базе есть:** {span[0]:%d.%m.%Y} … {span[1]:%d.%m.%Y}, "
        f"{study.bars} пятиминуток. Глубина **не подрезалась**: взято всё.",
        f"* **Торговых дней взято:** {len(days)} "
        f"({days[0]:%d.%m.%Y} … {days[-1]:%d.%m.%Y}). Торговым считается будний "
        "день, у которого есть история часов перебора.",
        f"* **Настоящий контракт:** `{ground.contract or '—'}`, свечи из рабочей "
        "базы программы — чтобы записанный прогон повторялся из окна.",
        f"* **Подбор:** {ground.split.tuning} · **проверка:** {ground.split.checking}. "
        "Ни один день не попадает в обе части.",
        f"* **Скользящая проверка:** {len(ground.folds)} окон.",
        f"* **Комиссия:** {ground.costs.per_side:g} ₽ за контракт на сторону. "
        "Это умолчание проекта, а не тариф владельца счёта.",
        f"* **Проскальзывание перебора:** {ground.costs.slippage_steps:g} шагов "
        f"при шаге цены {ground.costs.price_step:g}.",
        f"* **Прогонов:** {trials}. Поправка на число испытаний растёт как {penalty}.",
        "* **Дни стыка контрактов:** "
        + ", ".join(f"{day:%d.%m.%Y}" for day in sorted(ground.seams))
        + ". В такой день средняя считает ценовой разрыв ходом рынка.",
    ]


#: Заголовки таблицы лидеров. Валовая стоит рядом с чистой: комиссия
#: в этом проекте — отдельная строка всегда (`CLAUDE.md`, правило 4),
#: а на длинной истории именно она и решает исход.
LEADER_COLUMNS = (
    "№", "Набор",
    "Подбор: валовая", "Подбор: комиссия", "Подбор: чистая",
    "Сделок", "Переворотов", "Просадка",
    "Проверка: валовая", "Проверка: чистая", "Место на проверке (1 — лучшее)",
    "Соседи на проверке", "Сделок в дни стыка",
)


@dataclass(frozen=True, slots=True)
class Seen:
    """Место набора среди всех и что дали его соседи — то же, но на проверке."""

    rank: int
    total: int
    neighbourhood: str


def leader_row(place: int, one: Scored, name: str, *, seen: Seen) -> str:
    """Одна строка таблицы лидеров."""
    cells = (
        str(place), name,
        rubles(one.tuning.gross), rubles(-one.tuning.commission), rubles(one.tuning.net),
        str(one.tuning.trades), str(one.tuning.reversals), rubles(-one.tuning.drawdown),
        rubles(one.checking.gross), rubles(one.checking.net),
        f"{seen.total + 1 - seen.rank} из {seen.total}",
        seen.neighbourhood,
        f"{one.seam_trades} из {one.whole.trades}",
    )
    return "| " + " | ".join(cells) + " |"


def neighbourhood(
    scored: Sequence[Scored], axes: Sequence[tuple[tuple[str, float], ...]], here: int
) -> str:
    """Что дали соседи набора **на проверке**: худший … лучший.

    Набор-пик между двумя провалами и набор на плато в таблице неразличимы,
    а стоят разного: первый — шум, второй — хоть что-то. Соседи — самый
    дешёвый и самый понятный признак того, какой из двух перед нами.
    """
    if not axes[here]:
        return "вне сетки: соседей не бывает"
    kin = around(axes, here)
    if not kin:
        return "не нашлось (сетка прорежена)"
    money = sorted(scored[number].checking.net for number in kin)
    return f"{rubles(money[0])} … {rubles(money[-1])} ({len(kin)})"


def fold_places(scored: Sequence[Scored], folds: int) -> list[tuple[int, int]]:
    """Места победителей подбора на проверке — по одному на окно.

    Прямая проверка того, ради чего заведено разделение: побеждает ли
    на проверке тот, кто победил на подборе. Пары «место, всего мест»
    уходят в `overfit_share`.
    """
    found: list[tuple[int, int]] = []
    for window in range(folds):
        best = max(scored, key=lambda one, at=window: one.fold_tuning[at])  # type: ignore[misc]  # значение по умолчанию связывает номер окна
        line = [one.fold_checking[window] for one in scored]
        found.append((rank_of(best.fold_checking[window], line), len(line)))
    return found


def sharpe_spread(scored: Sequence[Scored]) -> float:
    """Разброс Шарпа по всем испытаниям подбора: планка для поправки.

    Перебрав тысячу пустышек, лучшую из них получают всегда. Насколько
    высоко она заберётся — определяется именно этим разбросом.
    """
    line = [one.tuning.shape.sharpe for one in scored]
    values = [value for value in line if value is not None]
    if len(values) < 2:  # noqa: PLR2004 — разброса из одного числа не бывает
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))


@dataclass(frozen=True, slots=True)
class Extra:
    """Что посчитано про лидера сверх сетки.

    `slippage` — чистая прибыль **на проверке** при 0, 1 и 2 шагах
    проскальзывания. Считается тремя отдельными прогонами, а не пересчётом:
    проскальзывание двигает цену входа, движок считает от неё уровень тейка,
    и сдвинутый уровень задевается на другом баре (`D-061`). То есть меняются
    не только деньги, но и состав сделок.
    """

    slippage: tuple[float, float, float] = (0.0, 0.0, 0.0)
    contract_tuning: Cut = Cut()
    contract_checking: Cut = Cut()
    sessions: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class Study:
    """Весь замер: условия, сетка, лидеры и всё, что про них посчитано."""

    ground: Ground
    days: tuple[date, ...]
    recipes: tuple[Recipe, ...]
    scored: tuple[Scored, ...]
    picked: tuple[Scored, ...]
    named: tuple[tuple[str, Scored], ...] = ()
    extras: tuple[tuple[int, Extra], ...] = ()
    contract: tuple[str, tuple[date, ...], Period, Period] | None = None
    #: Имя файла базы, как его видит человек: снимок называется иначе.
    source: str = ""
    spent: float = 0.0
    #: Что записано на диск — строками для человека. Пусто, пока не записано.
    said: tuple[str, ...] = ()
    #: Кривая лидера №1 по дням: день и чистая за день. По проверочному
    #: прогону на настоящем контракте.
    curve: tuple[tuple[date, int, float], ...] = ()
    #: Разбивка лидера №1 по дням недели и по часам входа: вид, подпись,
    #: сделки, деньги. Считается по **проверочному** прогону на настоящем
    #: контракте — тому, чьё число стоит в окне шаблонов.
    breakdown: tuple[tuple[str, str, int, int, float], ...] = ()
    #: Сколько пятиминуток в ряде и какие календарные дни он занимает целиком.
    #: Нужны, чтобы свод сказал «запрошено / взято», а не только «взято»:
    #: `python -m backtest` молча режет историю до 400 дней (`D-056`).
    bars: int = 0
    span: tuple[date, date] | None = None

    @property
    def extra_of(self) -> dict[int, Extra]:
        """Добавочные числа по номеру сочетания в сетке."""
        return dict(self.extras)


def leaders_table(study: Study) -> list[str]:
    """Таблица лидеров: подбор и проверка рядом, валовая рядом с чистой."""
    axes = tuple(one.axes for one in study.recipes)
    line = [one.checking.net for one in study.scored]
    rows = [
        "| " + " | ".join(LEADER_COLUMNS) + " |",
        "|" + "---|" * len(LEADER_COLUMNS),
    ]
    for place, one in enumerate(study.picked, start=1):
        rows.append(leader_row(place, one, study.recipes[one.number].label, seen=Seen(
            rank=rank_of(one.checking.net, line),
            total=len(line),
            neighbourhood=neighbourhood(study.scored, axes, one.number),
        )))
    return rows


def named_table(study: Study) -> list[str]:
    """Опорные строки: с чем сравнивать лидеров.

    Строка «весь торговый день» обязательна (ТЗ §8): без неё нельзя отличить
    работающее сужение окна от подгонки. Если весь день даёт то же самое —
    узкое окно не дало ничего.
    """
    rows = [
        "| Строка | Подбор: валовая | Подбор: чистая | Сделок | "
        "Проверка: валовая | Проверка: чистая |",
        "|---|---|---|---|---|---|",
    ]
    for title, one in study.named:
        rows.append(
            f"| {title} | {rubles(one.tuning.gross)} | {rubles(one.tuning.net)} | "
            f"{one.tuning.trades} | {rubles(one.checking.gross)} | "
            f"{rubles(one.checking.net)} |"
        )
    return rows


def contract_table(study: Study) -> list[str]:
    """Что дали лидеры на настоящем контракте — том, которым торгуют."""
    if study.contract is None:
        return []
    symbol, days, tuning, checking = study.contract
    rows = [
        f"Ряд `{symbol}` из **рабочей базы программы**, {len(days)} "
        + plural(len(days), "торговый день", "торговых дня", "торговых дней")
        + ", "
        + f"{days[0]:%d.%m.%Y} … {days[-1]:%d.%m.%Y}. "
        f"Первый отрезок {tuning}, второй {checking}. Именно эти два прогона "
        "записаны в журнал и видны в окне «Шаблоны настроек».",
        "",
        "| № | Набор | Первый: валовая | Первый: чистая | Первый: сделок | "
        "Второй: валовая | Второй: чистая | Второй: сделок |",
        "|---|---|---|---|---|---|---|---|",
    ]
    found = study.extra_of
    for place, one in enumerate(study.picked, start=1):
        extra = found.get(one.number, Extra())
        rows.append(
            f"| {place} | {study.recipes[one.number].label} | "
            f"{rubles(extra.contract_tuning.gross)} | "
            f"{rubles(extra.contract_tuning.net)} | {extra.contract_tuning.trades} | "
            f"{rubles(extra.contract_checking.gross)} | "
            f"{rubles(extra.contract_checking.net)} | {extra.contract_checking.trades} |"
        )
    return rows


def slippage_table(study: Study) -> list[str]:
    """Каждый лидер в трёх столбцах: 0, 1 и 2 шага проскальзывания."""
    rows = [
        "| № | Набор | Проверка: 0 шагов | 1 шаг | 2 шага |",
        "|---|---|---|---|---|",
    ]
    found = study.extra_of
    for place, one in enumerate(study.picked, start=1):
        none, once, twice = found.get(one.number, Extra()).slippage
        rows.append(
            f"| {place} | {study.recipes[one.number].label} | {rubles(none)} | "
            f"{rubles(once)} | {rubles(twice)} |"
        )
    return rows


def gross_lines(study: Study) -> list[str]:
    """Главный вопрос: есть ли в сетке хоть одно сочетание с ненулевой валовой.

    Валовая — это прибыль **до комиссии**, то есть то, что вообще угадал
    сигнал. Замер 05.09.2026 на настройках владельца счёта дал за год
    валовую +32 ₽ на 1 213 сделках: до всяких издержек сигнал не угадал
    ничего. Если так по всей сетке — усиливать нечего, и перебор окна
    и тейка бессмыслен: пятнадцать «лидеров» окажутся пятнадцатью способами
    по-разному проиграть комиссии.

    Поэтому счёт идёт по всей сетке, а не по пятнадцати верхним строкам:
    верхние строки положительны по построению отбора.
    """
    total = len(study.scored)
    tuned = [one for one in study.scored if one.tuning.gross > 0]
    checked = [one for one in study.scored if one.checking.gross > 0]
    both = [one for one in tuned if one.checking.gross > 0]
    net_up = [one for one in study.scored if one.checking.net > 0]
    order = sorted(one.checking.gross for one in study.scored)
    middle = order[total // 2] if order else 0.0
    best = max(study.scored, key=lambda one: one.checking.gross)
    return [
        f"* **Валовая положительна на подборе:** {len(tuned)} сочетаний из {total}.",
        f"* **Валовая положительна на проверке:** {len(checked)} из {total}; "
        f"на обоих отрезках сразу — {len(both)}.",
        f"* **Чистая положительна на проверке:** {len(net_up)} из {total}. "
        "Разница с валовой и есть комиссия.",
        f"* **Медианная валовая на проверке:** {rubles(middle)} ₽.",
        f"* **Наибольшая валовая на проверке:** {rubles(best.checking.gross)} ₽ "
        f"при чистой {rubles(best.checking.net)} ₽ и {best.checking.trades} сделках "
        f"— «{study.recipes[best.number].label}».",
    ]


#: Дни недели по-русски. Ключ разбивки — как у `datetime.weekday()`.
WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница",
            "суббота", "воскресенье")


def breakdown_of(run: HistoryRun) -> tuple[tuple[str, str, int, int, float], ...]:
    """Разбивка прогона по дням недели и по часам входа.

    Требование ТЗ §4.10 Б, и оно же прямая проверка наблюдения заказчика
    про понедельник–вторник. Считается по **одному** набору, а не по девяти
    тысячам: строка «среда: 0 ₽» в таблице из девяти тысяч строк не значит
    ничего, а в отчёте об одном прогоне значит.

    ⚠️ Сделка относится к моменту **входа**, приведённому к МСК: разбивка
    отвечает на вопрос «когда входить», а не «когда зафиксировалась прибыль».
    """
    return tuple(
        (kind, label(item.key), item.trades, item.profitable, item.profit)
        for kind, line, label in (
            ("день недели", run.summary.by_weekday, lambda key: WEEKDAYS[key]),
            ("час", run.summary.by_hour, lambda key: f"{key:02d}:00"),
        )
        for item in line
    )


def curve_of(run: HistoryRun) -> tuple[tuple[date, int, float], ...]:
    """Кривая прогона по дням: день, сделок, чистая за день."""
    return tuple((one.day, one.trades, one.profit) for one in run.summary.by_day)


#: Сколько лучших и худших дней показывать. Три — чтобы было видно, держится
#: ли итог на одном дне; полный список из тридцати пяти строк тот же вопрос
#: не проясняет, а прячет.
DAYS_SHOWN = 3


def curve_lines(study: Study) -> list[str]:
    """Равномерный результат или один удачный день — прямой ответ.

    Итоговая цифра этот вопрос скрывает, и потому кривая по дням в отчёте
    обязательна (ТЗ §4.10 Б). Здесь не вся кривая, а то, ради чего её смотрят:
    сколько дней в минусе, какова медиана дня и не держится ли весь плюс
    на трёх лучших днях.
    """
    if not study.curve:
        return ["Кривой нет: прогонов по настоящему контракту не делалось."]
    money = sorted(profit for _day, _trades, profit in study.curve)
    total = sum(money)
    best = sorted(study.curve, key=lambda one: one[2], reverse=True)
    losing = sum(1 for value in money if value < 0)
    top = sum(value for value in money[-DAYS_SHOWN:])
    return [
        f"* **Дней со сделками:** {len(money)}, из них в минусе {losing}.",
        f"* **Медиана дня:** {rubles(money[len(money) // 2])} ₽; "
        f"лучший {rubles(money[-1])} ₽, худший {rubles(money[0])} ₽.",
        f"* **Три лучших дня дали {rubles(top)} ₽ при итоге {rubles(total)} ₽**"
        + (f" — это {top / total:.0%} итога." if total > 0
           else ". Итог не положителен, доли считать не от чего."),
        "* **Лучшие дни:** "
        + "; ".join(
            f"{day:%d.%m} {rubles(profit)} ₽ ({trades} сд.)"
            for day, trades, profit in best[:DAYS_SHOWN]
        ),
        "* **Худшие дни:** "
        + "; ".join(
            f"{day:%d.%m} {rubles(profit)} ₽ ({trades} сд.)"
            for day, trades, profit in best[-DAYS_SHOWN:]
        ),
    ]


def detail_lines(study: Study) -> list[str]:
    """Лидер №1 по всем показателям отчёта ТЗ §4.10 Б, тремя отрезками."""
    if not study.picked:
        return ["Лидеров нет — показывать нечего."]
    one = study.picked[0]
    extra = study.extra_of.get(one.number, Extra())
    ryad = study.ground.symbol
    contract = study.ground.contract or "контракт"
    rows = [
        f"| Показатель | Подбор `{ryad}` | Проверка `{ryad}` "
        f"| `{contract}`, второй отрезок |",
        "|---|---|---|---|",
    ]
    for title, take in _DETAIL_ROWS:
        rows.append(
            f"| {title} | {take(one.tuning)} | {take(one.checking)} "
            f"| {take(extra.contract_checking)} |"
        )
    return rows


def _share(cut: Cut) -> str:
    """Доля прибыльных сделок словами. Без сделок доли не бывает."""
    if cut.trades == 0:
        return "сделок нет"
    return f"{cut.profitable} из {cut.trades} ({cut.profitable / cut.trades:.0%})"


def _factor(cut: Cut) -> str:
    """Профит-фактор. Не определён — так и написано, а не «бесконечность»."""
    if cut.profit_factor is None:
        return "не определён"
    return f"{cut.profit_factor:.2f}".replace(".", ",")


def _average(cut: Cut) -> str:
    """Средняя сделка в рублях."""
    return "—" if cut.trades == 0 else f"{rubles(cut.net / cut.trades)} ₽"


#: Строки подробной таблицы лидера №1 — таблица, а не десяток строк подряд:
#: показатель, заведённый завтра, добавляется сюда одной строкой.
_DETAIL_ROWS: tuple[tuple[str, Callable[[Cut], str]], ...] = (
    ("Сделок", lambda cut: str(cut.trades)),
    ("Прибыльных", _share),
    ("Валовая, ₽", lambda cut: rubles(cut.gross)),
    ("Комиссия, ₽", lambda cut: rubles(-cut.commission)),
    ("Чистая, ₽", lambda cut: rubles(cut.net)),
    ("Профит-фактор", _factor),
    ("Максимальная просадка, ₽", lambda cut: rubles(-cut.drawdown)),
    ("Переворотов", lambda cut: str(cut.reversals)),
    ("Средняя сделка", _average),
    ("Лучшая сделка, ₽", lambda cut: rubles(cut.best)),
    ("Худшая сделка, ₽", lambda cut: rubles(cut.worst)),
    ("Стандартная ошибка итога, ₽", lambda cut: f"±{cut.error_of_total:,.0f}".replace(",", " ")),
)


def breakdown_table(study: Study) -> list[str]:
    """Разбивка лидера №1 по дням недели и по часам."""
    if not study.breakdown:
        return ["Разбивки нет: прогонов по настоящему контракту не делалось."]
    rows = [
        "| Что | Строка | Сделок | Прибыльных | Чистая |",
        "|---|---|---|---|---|",
    ]
    rows.extend(
        f"| {kind} | {label} | {trades} | {profitable} | {rubles(profit)} |"
        for kind, label, trades, profitable, profit in study.breakdown
    )
    return rows


def overfitting_lines(study: Study) -> list[str]:
    """Три ответа на вопрос «а не случайность ли лучший результат».

    Ни один из трёх не спасает короткую историю: на короткой выборке разброс
    итога сопоставим с самим итогом, и поправки честно это показывают,
    но не чинят. Вывод из красной поправки один — данных мало, а не «надо
    взять другую формулу».
    """
    # Берётся лидер №1 — тот набор, который человек и применил бы, — а не
    # безусловный максимум по подбору: у максимума может не быть и сорока
    # сделок, и поправка считалась бы для строки, которую никто не возьмёт.
    best = study.picked[0] if study.picked else max(
        study.scored, key=lambda one: one.tuning.net
    )
    spread = sharpe_spread(study.scored)
    deflated = deflated_sharpe(best.tuning.shape, spread, len(study.scored))
    places = fold_places(study.scored, len(study.ground.folds))
    share = overfit_share(places)
    below = sum(1 for place, total in places if place <= (total + 1) / 2)
    return [
        "* **Поправка Шарпа (DSR) для лидера №1:** "
        + ("не считается — сделок или разброса не хватает"
           if deflated is None else f"{deflated:.2f}")
        + f". Считана от фактических {len(study.scored)} испытаний.",
        f"* **Разброс Шарпа по испытаниям:** {spread:.4f} на сделку.",
        f"* **Доля провалов (PBO):** победитель подбора оказался на проверке "
        f"ниже середины — **окон {below} из {len(places)}**"
        + (f", то есть {share:.0%}." if share is not None else "."),
        "* **Стандартная ошибка итога проверки у лидера №1:** "
        + f"±{best.checking.error_of_total:,.0f} ₽".replace(",", " ")
        + f" при самом итоге {rubles(best.checking.net)} ₽. Итог, который меньше "
        "своей ошибки, от нуля неотличим.",
    ]


#: Оговорки, которые едут вместе со сводом. Оговорка, которой нельзя верить,
#: хуже отсутствующей: она обесценивает соседние.
CAVEATS = (
    "**Примеры — это показ того, как перебор устроен, а не совет, чем "
    "торговать.** Пятнадцать лидеров отобраны на прошлом; замеры 05–06.09.2026 "
    "дважды показали, что подбор проигрывает бездействию (решение 0051).",
    "Свод не говорит, какие настройки заработают в следующем месяце. Ни одна "
    "строка этого не значит и значить не может: проверочная половина — это тоже "
    "прошлое, просто не то, на котором выбирали.",
    "Сшитый ряд `@MX` годится только для замеров: цены на стыках контрактов "
    "не подгоняются, и торговать по нему нельзя (решение 0049). В шаблонах "
    "стоит настоящий контракт, а не ряд.",
    "Комиссия 14 ₽ за контракт на сторону — умолчание проекта, а не тариф "
    "владельца счёта. Биржевой сбор плавает; на другом тарифе все числа другие.",
    "Третьего варианта поведения после тейка («сразу восстановить позицию») "
    "движком не выражено вовсе — обязательная программа ТЗ §8 закрыта не "
    "полностью, и это недобор, а не полнота.",
    "Разбивки по дням недели и по часам посчитаны **по одному набору** — лидеру №1. "
    "По девяти тысячам их не считают: строка «среда: 0 ₽» в такой таблице "
    "не значит ничего.",
    "⚠️ Шаблон применяется целиком. У машинных наборов **пуст календарь** "
    "и **пусто «откуда взята стоимость пункта»**: перебор шёл без отметок "
    "нерабочих дней и биржу ни о чём не спрашивал. Применение такого шаблона "
    "снимет ваши отметки календаря и вернёт красную пометку «величина биржей "
    "не подтверждена». Это правда о том, как считано, а не недосмотр.",
    "У машинных наборов выключены скользящий тейк и фильтр против пилы: "
    "ни то ни другое в этой сетке не перебиралось.",
)


#: Разделы свода: заголовок, вводная фраза, кто строит содержимое.
#:
#: Таблица, а не сорок строк подряд внутри одной функции, и это не про длину.
#: Порядок разделов становится **данными**: раздел, заведённый завтра,
#: добавляется сюда одной строкой, а не вставляется в середину списка,
#: где его легко поставить не туда. Пустая вводная — раздел без пояснения.
SECTIONS: tuple[tuple[str, str, Callable[[Study], list[str]]], ...] = (
    (
        "Условия",
        "",
        lambda study: [
            *conditions_lines(study, len(study.scored)),
            f"* **Перебор занял:** {duration(study.spent)}.",
        ],
    ),
    (
        "Лидеры подбора и что они дали на проверке",
        "Лидеры выбраны **только по отрезку подбора**. Колонка «проверка» "
        "показывает, что из этого вышло на днях, которых выбор не видел.",
        leaders_table,
    ),
    ("Опорные строки", "", named_table),
    (
        "Есть ли в сетке хоть что-то, что угадывает",
        "Валовая прибыль — это то, что угадал сигнал **до комиссии**. "
        "Если она около нуля по всей сетке, усиливать нечего.",
        gross_lines,
    ),
    ("Подгонка", "", overfitting_lines),
    (
        "Проскальзывание",
        "Проскальзывание меняет не только цены, но и состав сделок (`D-061`).",
        slippage_table,
    ),
    ("Настоящий контракт", "", contract_table),
    (
        "Лидер №1 подробно",
        "Все показатели отчёта ТЗ §4.10 Б по одному набору. Просадка в рублях, "
        "а не в процентах: размер счёта программе никто не сообщал.",
        detail_lines,
    ),
    (
        "Равномерный ли результат",
        "Итоговая цифра этот вопрос скрывает. Кривая по дням — проверочный "
        "прогон лидера №1 на настоящем контракте.",
        curve_lines,
    ),
    (
        "Разбивка лидера №1 по дням недели и по часам",
        "Считана по **проверочному** прогону на настоящем контракте — тому, "
        "чьё число стоит в окне шаблонов. Сделка отнесена к моменту входа "
        "по МСК. Наблюдение про понедельник–вторник проверяется этой таблицей, "
        "а не подтверждается ею: на нескольких неделях разница между днями "
        "набирается из единиц сделок.",
        breakdown_table,
    ),
    ("Что записано", "", lambda study: [f"* {line}" for line in study.said]),
    (
        "Чего этот свод не закрывает",
        "",
        lambda study: [f"* {line}" for line in CAVEATS],  # noqa: ARG005 — подпись общая на все разделы
    ),
)


def headline(study: Study) -> list[str]:
    """Ответ словами до первой таблицы: три предложения и ни одного числа лишнего.

    Владелец счёта спрашивал не «кто лучший», а «можно ли вообще выбрать
    настройки перебором». На это отвечают три вещи: сколько лидеров пережило
    проверку, есть ли в сетке хоть что-то с положительной валовой на обоих
    отрезках, и во сколько обошлась комиссия.
    """
    both = sum(
        1 for one in study.scored if one.tuning.gross > 0 and one.checking.gross > 0
    )
    net_up = sum(1 for one in study.scored if one.checking.net > 0)
    return [
        verdict(study.picked),
        "",
        f"**По всей сетке из {len(study.scored)} сочетаний валовая прибыль "
        f"положительна на обоих отрезках у {both}"
        + (", то есть ни у одного.**" if both == 0
           else f", а чистая на проверке — у {net_up}.**")
        + (" Комиссия съедает то, что сигнал угадал."
           if net_up < both else " Разница между этими двумя числами и есть комиссия."),
    ]


def summary_text(study: Study) -> str:
    """Весь свод одним текстом. Первой строкой — ответ словами.

    Разделы берутся из таблицы `SECTIONS` и идут в её порядке: заголовок,
    вводная фраза, содержимое. Ответ словами стоит **до** первой таблицы
    намеренно — владелец счёта спрашивал не «кто лучший», а «можно ли вообще
    выбрать настройки перебором», и таблица на это не отвечает.
    """
    lines = [
        f"# Лидеры перебора: {len(study.picked)} "
        + plural(len(study.picked), "набор", "набора", "наборов")
        + f" — замер {datetime.now(tz=MSK):%d.%m.%Y}",
        "",
        *headline(study),
        "",
    ]
    for title, intro, build in SECTIONS:
        lines += [f"## {title}", ""]
        if intro:
            lines += [intro, ""]
        lines += [*build(study), ""]
    return "\n".join(lines)

def duration(seconds: float) -> str:
    """Время словами: минуты и секунды."""
    if seconds < A_MINUTE:
        return f"{seconds:.0f} с"
    return f"{int(seconds) // A_MINUTE} мин {int(seconds) % A_MINUTE:02d} с"


def confirm(trials: int, seconds: float, *, asked: bool, workers: int = 1) -> None:
    """Сказать, сколько будет прогонов, **до** запуска, и спросить, если долго.

    Требование ТЗ §4.10 В. Число печатается всегда; согласие спрашивается
    только тогда, когда перебор идёт дольше пяти минут — иначе вопрос
    превращается в клавишу, которую жмут не читая.

    ⚠️ Время называется **с учётом числа процессов**. Оценка «533 минуты»
    при одиннадцати работающих процессах — это не осторожность, а неверное
    число: человек по нему решает, ждать ему или уйти.
    """
    waiting = seconds / max(1, workers)
    print(
        f"Прогонов: {trials}. Ожидаемое время: {duration(waiting)} "
        f"({workers} процессов, всего работы {duration(seconds)}). "
        f"Чем прогонов больше, тем выше шанс, что лучший — случайность: "
        f"поправка растёт как ln(N) = {math.log(trials):.2f}",
        file=sys.stderr,
    )
    if waiting < LONG_ENOUGH_TO_ASK or asked:
        return
    if input("Это надолго. Продолжать? [д/N] ").strip().lower() not in {"д", "да", "y", "yes"}:
        raise SystemExit("перебор отменён")


def cache_mark(source: pathlib.Path, ground: Ground, count: int) -> str:
    """Подпись перебора: всё, от чего зависят его числа.

    Кэш, отданный при других условиях, — это таблица, посчитанная на одних
    данных и подписанная другими. Подпись сверяется перед чтением, и при
    расхождении кэш **не читается**, а не «дочитывается».
    """
    stat = source.stat()
    return "|".join(str(item) for item in (
        1, ground.symbol, source.name, stat.st_size, int(stat.st_mtime),
        ground.costs.per_side, ground.costs.price_step, ground.costs.slippage_steps,
        ground.every, count, str(ground.split), len(ground.folds),
    ))


def sweep_or_cache(  # noqa: PLR0913 — все пять величин про один вопрос: считать или взять готовое
    source: pathlib.Path,
    ground: Ground,
    count: int,
    *,
    bars: int,
    room: pathlib.Path | None = None,
    asked: bool = False,
) -> tuple[tuple[Scored, ...], float]:
    """Прогнать сетку или взять готовое из кэша, если условия те же.

    Перебор на длинной истории идёт около часа. Пересчитывать его ради
    правки в подписи столбца — это час на опечатку, поэтому числа
    откладываются рядом. Подпись условий сверяется: чужой кэш не читается.
    """
    mark = cache_mark(source, ground, count)
    if room is not None and room.exists():
        kept = pickle.loads(room.read_bytes())
        if kept.get("mark") == mark:
            print(f"Перебор взят из {room}: считать заново нечего.", file=sys.stderr)
            return tuple(kept["scored"]), float(kept["spent"])
        print(f"Кэш {room} посчитан при других условиях и не взят.", file=sys.stderr)
    confirm(count, count * PACE * bars, asked=asked, workers=ground.workers)
    started = clock.perf_counter()
    scored = run_sweep(ground, count, workers=ground.workers,
                       told=tuple(range(500, count + 1, 500)))
    spent = clock.perf_counter() - started
    print(f"Перебор занял {duration(spent)}.", file=sys.stderr)
    if room is not None:
        room.write_bytes(pickle.dumps({"mark": mark, "scored": scored, "spent": spent}))
        print(f"Перебор отложен в {room}.", file=sys.stderr)
    return scored, spent


async def slippage_of(
    one: Recipe, bars: Sequence[Candle], ground: Ground, periods: Sequence[Period]
) -> tuple[float, float, float]:
    """Чистая прибыль набора на проверке при 0, 1 и 2 шагах проскальзывания.

    Три прогона, а не пересчёт одного: проскальзывание двигает цену входа,
    и от неё движок считает уровень тейка — состав сделок меняется (`D-061`).
    """
    found: list[float] = []
    for steps in (0.0, 1.0, 2.0):
        costs = replace(ground.costs, slippage_steps=steps)
        made = await score(one, bars, replace(ground, costs=costs), periods)
        found.append(made.checking.net)
    return found[0], found[1], found[2]


async def benchmarks(
    ground: Ground, bars: Sequence[Candle], periods: Sequence[Period]
) -> tuple[tuple[str, Scored], ...]:
    """Опорные строки свода: умолчания, весь день и то же самое без тейка.

    Считаются **отдельными прогонами**, а не выбираются из готовой сетки.
    Дороже на шесть прогонов, зато строка «весь торговый день» стоит в своде
    всегда — а без неё нельзя отличить работающее сужение окна от подгонки
    (ТЗ §8), и её отсутствие никак не бросалось бы в глаза.
    """
    named: list[tuple[str, Scored]] = []
    for one, title in reference_recipes():
        named.append((title, await score(one, bars, ground, periods)))
    return tuple(named)


async def contract_pass(
    made: Study,
) -> tuple[Study, dict[int, tuple[Cut, Cut, tuple[Stretch, ...]]]]:
    """Прогнать лидеров на настоящем контракте и получить два отрезка каждому.

    Контракт, а не сшитый ряд, потому что шаблон применяют кнопкой: заявку
    по `@MX` подать нельзя (решение 0049). Дни контракта при этом лежат
    внутри **проверочной** половины `@MX`, то есть оба прогона независимы
    от того, на чём лидер выбран.

    ⚠️ Свечи контракта берутся из **рабочей базы программы**, а не из базы
    цепочки, и это не мелочь. Записанный прогон человек обязан суметь
    повторить: он выберет тот же период в окне и получит те же числа.
    Замер 06.09.2026 показал, что две базы на общем отрезке MXU6 расходятся —
    142 минутки есть только в цепочке, 20 только в рабочей, у 40 не совпало
    закрытие. Считай мы по цепочке, окно давало бы другие деньги, и объяснить
    это владельцу счёта было бы нечем.
    """
    ground = made.ground
    if not ground.contract or ground.contract_database is None:
        return made, {}
    bars = bars_of(ground.contract_database, ground.contract)
    days = days_of(bars)
    bars = trim(bars, days)
    tuning, checking = halves(days, tuning=ground.contract_tuning_days)
    found: dict[int, tuple[Cut, Cut, tuple[Stretch, ...]]] = {}
    for one in made.picked:
        legs = await legs_of(
            made.recipes[one.number], bars, (tuning, checking), ground.costs,
            engine=ground.engine, strategy=ground.strategy,
        )
        found[one.number] = (
            Cut.of(legs[0].run.deals), Cut.of(legs[1].run.deals), legs,
        )
    return replace(made, contract=(ground.contract, days, tuning, checking)), found


# ---------------------------------------------------------------------------
# Две двери наружу: собрать условия и посчитать замер целиком
# ---------------------------------------------------------------------------

def prepare(  # noqa: PLR0913 — условия замера: ряд, деление, издержки, контракт
    database: pathlib.Path,
    symbol: str,
    *,
    engine: EngineSettings,
    strategy: StrategySettings,
    days: Sequence[date],
    costs: Costs,
    strategy_id: str = GRID_STRATEGY_ID,
    tuning_share: float = 0.5,
    checking_days: int = 5,
    every: int = 1,
    workers: int = 1,
    wanted: int = LEADERS_WANTED,
    contract: str = "",
    contract_database: pathlib.Path | None = None,
    contract_tuning_days: int = 36,
) -> Ground:
    """Собрать условия замера. Деление истории считается здесь и один раз.

    Слой сборки границ отрезков не считает и считать не должен: правило
    «подбор не заходит на проверку» стережёт `backtest.split.Fold`, и обойти
    его параметром нельзя. Отсюда и форма двери — сборка подаёт «сколько дней
    на подбор в долях», а рубеж выбирает этот слой.

    :param engine: умолчания движка с включённым переворотом. Приходят
        от сборки готовыми, а не строятся здесь: набор окна и настройки
        движка обязаны совпасть по построению, и единственное место,
        где перевод живёт, — `app/convert.py`.
    :param strategy_id: имя выбранного торгового алгоритма. Умолчание —
        тот, под который написана сетка: у неё один автор и один адресат.
        Чужое имя — отказ вслух (`ForeignStrategy`), а не подбор по чужим
        полям, показанный как ваш.
    """
    tuning = max(1, round(len(days) * tuning_share))
    return Ground(
        database=database,
        symbol=symbol,
        engine=engine,
        # Сужение на границе: настройки приходят портом, а сетка умеет менять
        # поля **своего** алгоритма. Чужие — отказ вслух, а не подбор
        # по чужим полям, показанный как ваш.
        strategy=refuse_foreign_strategy(strategy_id, strategy),
        strategy_id=strategy_id,
        split=single_split(days, tuning=tuning),
        folds=walk_forward(days, tuning=tuning, checking=checking_days),
        seams=seam_days(database),
        costs=costs,
        every=every,
        workers=workers,
        wanted=wanted,
        contract=contract,
        contract_database=contract_database,
        contract_tuning_days=contract_tuning_days,
    )


async def study(
    ground: Ground,
    *,
    source: pathlib.Path,
    cache: pathlib.Path | None = None,
    asked: bool = False,
) -> tuple[Study, dict[int, tuple[Cut, Cut, tuple[Stretch, ...]]]]:
    """Весь замер целиком: перебор, лидеры, опорные строки, контракт.

    Одна дверь, а не десяток шагов, и это то же правило, по которому сборка
    зовёт движок целиком (`Engine`, `replay`), а не собирает обработку свечи
    из его шагов (`tests/test_app_boundaries.py`). Замер, собранный в слое
    сборки из кусков, стал бы второй линией расчёта и разошёлся бы с этой
    молча.

    Возвращает замер и прогоны лидеров по настоящему контракту — их сборке
    надо записать в журнал, и только их.

    :param source: настоящая база, с которой снят `ground.database`. Нужна
        подписи кэша: снимок каждый раз новый, а исходник тот же.
    """
    bars = bars_of(ground.database, ground.symbol)
    days = days_of(bars)
    bars = trim(bars, days)
    line = recipes()[::ground.every]
    scored, spent = sweep_or_cache(
        source, ground, len(line), bars=len(bars), room=cache, asked=asked,
    )
    made = Study(
        ground=ground, days=tuple(days), recipes=line, scored=scored,
        picked=leaders(scored, wanted=ground.wanted), spent=spent,
        bars=len(bars), source=source.name,
        span=(in_moscow(bars[0].time).date(), in_moscow(bars[-1].time).date()),
    )
    periods = _periods_of(ground)
    made = replace(made, named=await benchmarks(ground, bars, periods))
    made, legs = await contract_pass(made)
    top = made.picked[0].number if made.picked else None
    if top is not None and top in legs:
        last = legs[top][2][-1].run
        made = replace(made, breakdown=breakdown_of(last), curve=curve_of(last))
    return replace(made, extras=tuple([
        (one.number, Extra(
            slippage=await slippage_of(line[one.number], bars, ground, periods),
            contract_tuning=legs[one.number][0] if one.number in legs else Cut(),
            contract_checking=legs[one.number][1] if one.number in legs else Cut(),
        ))
        for one in made.picked
    ])), legs
