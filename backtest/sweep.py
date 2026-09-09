"""Перебор сочетаний настроек с разделением подбора и проверки.

Что это и чего это не делает
----------------------------
Модуль **не подбирает боевые настройки**. Он показывает, что дал перебор
и насколько этому можно верить: каждое сочетание считается на отрезке подбора
и на отрезке проверки, и вторая колонка стоит рядом с первой всегда
(ТЗ §4.10 Г). Умолчания проекта модуль не трогает: за ними замеры и решения.

Почему перебор разбит на блоки, а не считается одним перекрёстным списком
-------------------------------------------------------------------------
Полное произведение обязательной программы ТЗ §8 — 9 744 сочетания. На 71
торговом дне это около 66 проверочных сделок; лучшее из 9 744 испытаний
на такой выборке — почти наверняка шум, и никакая поправка этого не чинит,
она только называет цену. Число испытаний входит в поправку Шарпа напрямую
(`backtest.overfitting`), поэтому перебор урезан до трёх блоков:

======================  ==========================================  ==========
Блок                    На какой вопрос отвечает                    Сочетаний
======================  ==========================================  ==========
`window`                даёт ли сужение окна хоть что-нибудь         58
`average_and_take`      период средней и размер тейка вместе         45
`modes`                 момент переворота и поведение после тейка    4
======================  ==========================================  ==========

Внутри блока перебор полный, между блоками — нет: остальное держится
на умолчаниях. Цена названа вслух: **взаимодействия между блоками так
не видны**. Скажем, узкое окно может оказаться лучшим при тейке 0,3 %
и худшим при 1,2 %, и блочный перебор этого не покажет. Полное произведение
считается ключом `--full`, число прогонов при этом показывается до запуска.

Период средней и тейк перебираются **вместе** намеренно: они связаны через
частоту сигналов. Длинная средняя даёт редкие сигналы и длинные ходы, короткая
— частые и мелкие; размер тейка, окупающий комиссию, у них разный. Разнести
эти две оси значило бы мерить одну при заведомо неподходящем значении другой.

Что здесь **не** перебирается и почему
--------------------------------------
* **фильтр против пилы** (порог и подтверждение) — отдельная ось, и она уже
  меряна 05.09.2026 с оговорками; включать её в ту же сетку значит умножить
  число испытаний вчетверо ради параметра, у которого соседние значения дают
  +10 340 и −889 ₽. Мерится отдельно и после;
* **третий вариант поведения после тейка** («сразу восстановить позицию») —
  движком не выражен вовсе (`engine/settings.py`), перебирать нечего. Это
  недобор относительно ТЗ §8, и он назван в отчёте, а не замолчан;
* **проскальзывание** — не настройка стратегии, а условие исполнения. Задаётся
  один раз на весь перебор и печатается в шапке: сравнивать строки, посчитанные
  при разном проскальзывании, нельзя.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Final

from backtest.execution import Costs
from backtest.history import Deal, deal_results, replay, summarise
from backtest.overfitting import Shape
from backtest.split import Period
from engine import EngineSettings, Mode, Reversal, TradingWindow, in_moscow
from strategies import EmaReverseSettings, registry

if TYPE_CHECKING:
    from backtest.history import Summary

__all__ = [
    "BLOCKS",
    "GRID_STRATEGY_ID",
    "ForeignStrategy",
    "refuse_foreign_strategy",
    "TAKE_PERCENTS",
    "WINDOW_DURATIONS",
    "WINDOW_STARTS",
    "AVERAGE_PERIODS",
    "MINUTES_IN_HOUR",
    "Block",
    "Ground",
    "Money",
    "Point",
    "Trial",
    "blocks",
    "full_cross",
    "lay_out",
    "sweep",
]

#: Под какой торговый алгоритм написана эта сетка. **Литерал, а не
#: `registry.DEFAULT_ID`**: умолчание однажды сменят, и сетка тогда молча
#: объявила бы себя написанной под алгоритм, полей которого она не знает.
#:
#: Сетка перебирает `period` настроек алгоритма и поля движка (окно, тейк,
#: момент переворота). Первое — имя поля **конкретного** алгоритма: у второго
#: алгоритма период средней может называться иначе, значить иное или
#: отсутствовать вовсе. Отсюда и объявление: сетка знает, чья она.
GRID_STRATEGY_ID: Final[str] = "ema_reverse"


class ForeignStrategy(ValueError):
    """Перебор запрошен для алгоритма, под который сетка не написана.

    Отдельный класс, а не `ValueError` вообще: наверху его переводят
    в человеческий отказ, и ловить его широким `except ValueError` вместе
    с любым другим негодным числом нельзя.
    """


def refuse_foreign_strategy(
    strategy_id: str, settings: object
) -> EmaReverseSettings:
    """Сетка написана под этот алгоритм — или отказ вслух. Ловушка 14.

    ⚠️ **Молчание здесь стоит дороже всего в этом файле.** Перебор без этой
    проверки честно перебрал бы поля алгоритма №1 при выбранном втором
    и показал бы результат как «ваши лидеры»: числа настоящие, таблица
    красивая, к выбранному правилу отношения не имеет. Владелец счёта
    поставил бы по ней настройки — и торговал бы по подбору, сделанному
    для другого правила.

    Проверяется **имя**, а не класс настроек, и это не придирка: два
    алгоритма вправе делить один класс настроек (`D-098`), а сетка после
    перебора собирает **модуль** — по имени `GRID_STRATEGY_ID`. Совпадение
    классов при разных именах означало бы перебор настроек одного алгоритма
    с прогоном другого.

    Возвращает **те же** настройки, суженные до типа, который сетка умеет
    менять: сужение на границе, а не `cast` внутри. Слой сборки поднимается
    выше портом (`StrategySettings`) и класс алгоритма назвать не может —
    называет его тот, кто под этот класс написан.

    :raises ForeignStrategy: выбран не тот алгоритм либо настройки не его.
    """
    entry = registry.find(GRID_STRATEGY_ID)
    if strategy_id != entry.id:
        raise ForeignStrategy(
            f"сетка перебора написана под торговый алгоритм «{entry.title}» "
            f"({entry.id}), а выбран «{strategy_id}». Перебор не запущен: "
            "он перебирал бы поля чужого алгоритма и показал бы результат "
            "как ваш. Подбора параметров для этого алгоритма в программе "
            "пока нет — выберите «{title}» либо подбирайте вручную.".format(
                title=entry.title
            )
        )
    if not isinstance(settings, EmaReverseSettings):
        raise ForeignStrategy(
            f"сетка перебора написана под настройки алгоритма «{entry.title}» "
            f"({EmaReverseSettings.__name__}), а поданы "
            f"{type(settings).__name__}. Перебор не запущен."
        )
    return settings


def grid_strategy() -> registry.StrategyEntry:
    """Запись алгоритма, под который написана сетка. Отсутствие — отказ вслух."""
    return registry.find(GRID_STRATEGY_ID)


#: Период средней — три цифры ТЗ §8, рядом.
AVERAGE_PERIODS = (9, 15, 20)

#: Тейк-профит 0,2…1,5 % шагом 0,1 (ТЗ §8). Умолчание 0,5 % на этой сетке есть.
TAKE_PERCENTS = tuple(round(0.2 + 0.1 * step, 1) for step in range(14))

#: Минут в часе. Названо, чтобы `60` в расчёте границ окна не путалось
#: с длительностью в минутах, которая стоит рядом.
MINUTES_IN_HOUR = 60


def _at(minutes: int) -> time:
    """Время суток по числу минут от полуночи."""
    return time(minutes // MINUTES_IN_HOUR, minutes % MINUTES_IN_HOUR)


#: Начало торгового окна: 09:30…11:00 шагом 15 минут (ТЗ §8).
WINDOW_STARTS = tuple(_at(9 * MINUTES_IN_HOUR + 30 + 15 * step) for step in range(7))

#: Длительность окна: 30 минут … 4 часа. Шаг ТЗ не называет; взят получасовой —
#: при пятнадцатиминутном сочетаний стало бы вдвое больше без нового вопроса,
#: на который они отвечают.
WINDOW_DURATIONS = tuple(timedelta(minutes=30 * step) for step in range(1, 9))


# ---------------------------------------------------------------------------
# Итог отрезка в сжатом виде
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Money:
    """Деньги отрезка: то, что помещается в строку таблицы.

    Считается через `backtest.history.summarise` и только через неё: арифметика
    отчёта живёт в одном месте, здесь из неё берётся срез без кривых
    и разбивок. Кривая по дням и разбивки по часам нужны при открытии одной
    строки, а не в таблице из ста строк, и хранить их для каждого сочетания
    значило бы держать в памяти сотни тысяч мелких объектов.

    `net` — `None`, когда тариф не задан **или** сделок не было. Различает эти
    два случая `trades`. Ноль вместо «сделок не было» в таблицу не ставится:
    «0 ₽» читается как результат, а отсутствие сделок результатом не является.
    """

    trades: int
    profitable: int
    gross: float
    commission: float | None
    net: float | None
    drawdown: float | None
    reversals: int
    profit_factor: float | None
    best: float | None
    worst: float | None

    @classmethod
    def of(cls, deals: Sequence[Deal]) -> "Money":
        """Сжать итог по списку сделок."""
        return cls.from_summary(summarise(deals))

    @classmethod
    def from_summary(cls, summary: "Summary") -> "Money":
        """Сжать готовый итог: те же числа, без кривых и разбивок."""
        return cls(
            trades=summary.trades,
            profitable=summary.profitable,
            gross=summary.gross_profit,
            commission=summary.commission,
            net=summary.net_profit,
            drawdown=summary.max_drawdown,
            reversals=summary.reversals,
            profit_factor=summary.profit_factor,
            best=summary.best_trade,
            worst=summary.worst_trade,
        )

    @property
    def rubles(self) -> float:
        """Деньги отрезка числом. Отрезок без сделок даёт ровно ноль рублей.

        ⚠️ Отличается от `net` намеренно: `net` разделяет «сделок не было»
        и «сделки были, тариф не задан», а складывать проверочные отрезки надо
        числом. Пустой отрезок вносит в сумму ноль — это правда, а не догадка.
        """
        if self.trades == 0:
            return 0.0
        if self.net is None:
            raise ValueError(
                "деньги отрезка спрошены числом, а чистой прибыли нет: тариф "
                "комиссии не задан. Валовая прибыль результатом в этом проекте "
                "не является (DOMAIN.md §5) — задайте тариф"
            )
        return self.net


# ---------------------------------------------------------------------------
# Точка сетки
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Point:
    """Одно сочетание настроек: как называется, что подставить, где стоит в сетке.

    `axes` — положение точки на осях своего блока: имя оси и число. По ним
    находятся **соседи** (`backtest.overfitting.neighbours`), а соседи — это
    единственный способ отличить находку от шума на сетке: значение, у которого
    соседи проваливаются, находкой не является. Пустые `axes` означают «точка
    вне сетки, соседей нет» — так помечены «весь день», «без тейка»
    и переключатели режимов, у которых соседства не бывает по природе.
    """

    label: str
    engine: EngineSettings
    strategy: EmaReverseSettings
    axes: tuple[tuple[str, float], ...] = ()

    @property
    def on_grid(self) -> bool:
        """У точки есть соседи: она стоит на сетке, а не рядом с ней."""
        return bool(self.axes)


@dataclass(frozen=True, slots=True)
class Block:
    """Названная группа сочетаний и вопрос, на который она отвечает."""

    name: str
    title: str
    question: str
    points: tuple[Point, ...]


@dataclass(frozen=True, slots=True)
class Ground:
    """Условия, одинаковые для всего перебора: умолчания, тариф, издержки.

    Умолчания движка модуль не меняет — их задаёт `EngineSettings()`.
    Здесь только то, чего у движка нет: издержки отчёта и режим работы,
    без которого сделок не будет вовсе.
    """

    engine: EngineSettings
    strategy: EmaReverseSettings = field(default_factory=EmaReverseSettings)
    costs: Costs = field(default_factory=Costs)
    #: Имя алгоритма, чьи настройки лежат в `strategy`. Отдельным полем,
    #: а не выведенное из класса настроек: два алгоритма вправе делить один
    #: класс настроек, а сетка после перебора собирает **модуль** по имени.
    strategy_id: str = GRID_STRATEGY_ID

    def __post_init__(self) -> None:
        """Условия перебора согласованы с сеткой — или отказ вслух.

        Проверка стоит в **модели данных**, а не в проводке: через это поле
        настройки алгоритма ходят по всему перебору, и перехватывать их
        в каждой двери означало бы забыть одну.
        """
        refuse_foreign_strategy(self.strategy_id, self.strategy)


# ---------------------------------------------------------------------------
# Блоки перебора
# ---------------------------------------------------------------------------

def _window_points(ground: Ground) -> tuple[Point, ...]:
    """Границы окна: сетка ТЗ, плюс весь день, плюс нынешнее умолчание.

    **Строка «весь торговый день» обязательна** (ТЗ §8): без неё нельзя
    отличить работающее сужение окна от подгонки. Если весь день даёт то же
    самое — узкое окно не дало ничего.

    Нынешнее умолчание 10:05–11:00 на сетке ТЗ **не стоит**: 10:05 не кратно
    пятнадцати минутам, 55 минут не кратны получасу. Оно добавлено отдельной
    строкой, иначе таблица сравнивала бы варианты между собой, но не с тем,
    что стоит в программе сегодня.
    """
    points = [
        Point(
            label=f"{start:%H:%M}–{_end_of(start, span):%H:%M}",
            engine=replace(
                ground.engine,
                window=TradingWindow(start=start, end=_end_of(start, span)),
            ),
            strategy=ground.strategy,
            axes=(("start", _minutes(start)),
                  ("duration", span.total_seconds() / MINUTES_IN_HOUR)),
        )
        for start in WINDOW_STARTS
        for span in WINDOW_DURATIONS
    ]
    points.append(Point(
        label="весь торговый день",
        engine=replace(ground.engine, window=TradingWindow(time(0, 0), time(0, 0))),
        strategy=ground.strategy,
    ))
    points.append(Point(
        label="10:05–11:00 (умолчание)",
        engine=replace(ground.engine, window=TradingWindow()),
        strategy=ground.strategy,
    ))
    return tuple(points)


def _average_and_take_points(ground: Ground) -> tuple[Point, ...]:
    """Период средней × размер тейка, плюс «без тейка» на каждом периоде.

    «Без тейка» стоит здесь не для полноты: в DOMAIN.md §4 записано, что без
    тейк-профита стратегия убыточна на любом окне. Утверждение проверяемое,
    и раз перебор всё равно идёт — оно проверяется, а не пересказывается.
    """
    points = [
        Point(
            label=f"средняя {period}, тейк {take:.1f} %".replace(".", ","),
            engine=replace(ground.engine, take_profit=True, take_profit_percent=take),
            strategy=replace(ground.strategy, period=period),
            axes=(("period", float(period)), ("take", take)),
        )
        for period in AVERAGE_PERIODS
        for take in TAKE_PERCENTS
    ]
    points.extend(
        Point(
            label=f"средняя {period}, без тейка",
            engine=replace(ground.engine, take_profit=False),
            strategy=replace(ground.strategy, period=period),
        )
        for period in AVERAGE_PERIODS
    )
    return tuple(points)


def _mode_points(ground: Ground) -> tuple[Point, ...]:
    """Момент переворота × поведение после тейка. Соседства у переключателей нет.

    ⚠️ Третьего варианта поведения после тейка — «сразу восстановить позицию» —
    в движке нет, и подменять его вторым нельзя: это другие сделки. Программа
    ТЗ §8 требует трёх; здесь их два, и это недобор, а не полнота.
    """
    return tuple(
        Point(
            label=f"{_reversal_word(reversal)} · {_after_take_word(stop)}",
            engine=replace(ground.engine, reversal=reversal, stop_after_take_profit=stop),
            strategy=ground.strategy,
        )
        for reversal in (Reversal.THROUGH_BAR, Reversal.SAME_BAR)
        for stop in (True, False)
    )


#: Блоки перебора: имя, заголовок, вопрос, строитель. Таблица, а не три вызова
#: подряд, — добавление блока здесь ровно одна строка, и порядок блоков
#: становится данными, которые видит и отчёт, и тест.
BLOCKS: tuple[tuple[str, str, str, Callable[[Ground], tuple[Point, ...]]], ...] = (
    (
        "window",
        "Границы торгового окна",
        "даёт ли сужение окна что-нибудь по сравнению со всем днём",
        _window_points,
    ),
    (
        "average_and_take",
        "Период средней и тейк-профит",
        "какой размер тейка окупает комиссию при какой средней",
        _average_and_take_points,
    ),
    (
        "modes",
        "Момент переворота и поведение после тейка",
        "меняет ли режим переворота итог настолько, чтобы это было видно",
        _mode_points,
    ),
)


def blocks(ground: Ground) -> tuple[Block, ...]:
    """Собрать все блоки перебора на заданных условиях."""
    return tuple(
        Block(name=name, title=title, question=question, points=build(ground))
        for name, title, question, build in BLOCKS
    )


def full_cross(ground: Ground) -> tuple[Point, ...]:
    """Полное произведение обязательной программы: все оси разом.

    ⚠️ Считается по прямой просьбе и с показанным числом прогонов. Полезен
    он ровно одним — показывает взаимодействия, которых блоки не видят;
    вреден тем, что число испытаний растёт в девяносто раз, а история
    от этого длиннее не становится.
    """
    windows = [
        (f"{start:%H:%M}–{_end_of(start, span):%H:%M}",
         TradingWindow(start=start, end=_end_of(start, span)))
        for start in WINDOW_STARTS
        for span in WINDOW_DURATIONS
    ]
    windows.append(("весь торговый день", TradingWindow(time(0, 0), time(0, 0))))
    windows.append(("10:05–11:00", TradingWindow()))
    return tuple(
        Point(
            label=f"{name} · средняя {period} · тейк {take:.1f} % · "
                  f"{_reversal_word(reversal)} · {_after_take_word(stop)}".replace(".", ","),
            engine=replace(
                ground.engine, window=window, take_profit=True, take_profit_percent=take,
                reversal=reversal, stop_after_take_profit=stop,
            ),
            strategy=replace(ground.strategy, period=period),
            axes=(("period", float(period)), ("take", take)),
        )
        for name, window in windows
        for period in AVERAGE_PERIODS
        for take in TAKE_PERCENTS
        for reversal in (Reversal.THROUGH_BAR, Reversal.SAME_BAR)
        for stop in (True, False)
    )


# ---------------------------------------------------------------------------
# Прогон сетки
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Trial:
    """Один прогон сетки: деньги по каждому названному отрезку.

    Прогон **один на всё сочетание**, а не по прогону на отрезок, и это
    не оптимизация, а вопрос правильности. Движок работает непрерывно: средняя
    прогрета предыдущими днями, позиция переходит через границу. Отдельный
    прогон с первого дня проверки стартовал бы с холодной средней и давал бы
    сделки, которых у непрерывно работающего робота не было.

    Заглядывания вперёд это не создаёт: **выбор** настроек смотрит только
    в сделки, целиком лежащие в отрезке подбора, а состояние движка на границе
    определяется прошлыми данными, а не будущими.

    `straddling` — сделки, разорванные границей отрезка: вошли внутри, вышли
    снаружи. Они не идут ни в подбор, ни в проверку. `outside` — сделки
    в дни, не попавшие ни в один названный отрезок. Оба числа печатаются:
    ненулевые, они означают, что сумма колонок меньше итога прогона,
    и молчать об этом нельзя.
    """

    point: Point
    money: dict[Period, Money]
    shape: dict[Period, Shape]
    total: Money
    straddling: int
    outside: int
    halted: str

    def on(self, period: Period) -> Money:
        """Деньги названного отрезка.

        :raises KeyError: отрезок не назывался при прогоне. Молчаливый пустой
            итог здесь означал бы «на этом отрезке ничего не заработано»,
            то есть подменил бы ошибку вызова правдоподобным числом.
        """
        return self.money[period]

    def form(self, period: Period) -> Shape:
        """Форма распределения сделок отрезка: разброс, асимметрия, эксцесс.

        Нужна поправке Шарпа и стандартной ошибке итога. Хранится рядом
        с деньгами, а не считается заново по сделкам, потому что сделки
        после прогона не хранятся: сто сочетаний по полторы сотни сделок
        ещё поместились бы в память, девять тысяч — уже нет.
        """
        return self.shape[period]


async def sweep(
    candles: Sequence[object],
    points: Sequence[Point],
    periods: Sequence[Period],
    *,
    costs: Costs,
    on_progress: Callable[[int, int, Point], None] | None = None,
) -> tuple[Trial, ...]:
    """Прогнать каждое сочетание по всему ряду и разложить сделки по отрезкам.

    :param candles: весь ряд свечей, один и тот же для всех сочетаний.
        Разные ряды у разных строк таблицы означали бы, что таблица сравнивает
        не настройки, а данные.
    :param points: сочетания. Их число показывается вызывающим **до** запуска
        (ТЗ §4.10 В) и сохраняется рядом с результатом: без него нельзя
        оценить, насколько лучший результат отличается от шума.
    :param periods: отрезки, по которым раскладываются деньги. Пересекаться
        им не запрещено (у скользящего окна отрезки подбора пересекаются),
        но складывать пересекающиеся нельзя, и делает это не модуль,
        а тот, кто строит таблицу.
    :param costs: издержки, одни на весь перебор.
    """
    trials: list[Trial] = []
    for number, point in enumerate(points, start=1):
        run = await replay(
            candles,
            # Алгоритм собирает **реестр** по имени, под которое написана
            # сетка, а не сборка по имени класса: перебор, объявивший себя
            # написанным под один алгоритм и гоняющий другой, — это ровно
            # тот молчаливый обман, против которого стоит `ForeignStrategy`.
            grid_strategy().build(point.strategy),
            replace(point.engine, commission_per_side=costs.per_side),
            costs=costs,
        )
        trials.append(lay_out(point, run.deals, periods, halted=run.halted))
        if on_progress is not None:
            on_progress(number, len(points), point)
    return tuple(trials)


def lay_out(
    point: Point, deals: Sequence[Deal], periods: Sequence[Period], *, halted: str = ""
) -> Trial:
    """Разложить сделки прогона по отрезкам, посчитав потери на границах.

    Публичная, а не внутренняя, ровно потому, что здесь принимается решение
    «в какую колонку пойдут деньги этой сделки», и проверять его надо прямо,
    а не через прогон движка на подобранных данных. Сделка, разорванная
    границей, встречается редко, и наткнуться на неё в синтетическом ряду
    можно только случайно — то есть проверка была бы случайной тоже.
    """
    dated = [(deal, _day(deal.entry_time), _day(deal.exit_time)) for deal in deals]
    inside = {
        period: [deal for deal, entry, gone in dated if period.holds(entry, gone)]
        for period in periods
    }
    money = {period: Money.of(part) for period, part in inside.items()}
    shape = {period: Shape.of(deal_results(part)) for period, part in inside.items()}
    straddling = sum(
        1 for _, entry, gone in dated
        if any(period.contains(entry) for period in periods) and entry != gone
        and not any(period.holds(entry, gone) for period in periods)
    )
    outside = sum(
        1 for _, entry, _ in dated
        if not any(period.contains(entry) for period in periods)
    )
    return Trial(
        point=point,
        money=money,
        shape=shape,
        total=Money.of([deal for deal, _, _ in dated]),
        straddling=straddling,
        outside=outside,
        halted=halted,
    )


def _day(moment: datetime) -> date:
    """День сделки по Москве. Наивный момент отвергается `in_moscow` с исключением.

    Приведение к Москве обязательно и делается явно: сдвиг на три часа
    не роняет расчёт, он молча кладёт сделку в соседний день — то есть
    в соседний отрезок, а на границе подбора и проверки это ровно та ошибка,
    против которой заведено разделение.
    """
    return in_moscow(moment).date()


def _minutes(moment: time) -> float:
    """Минут от начала суток."""
    return moment.hour * MINUTES_IN_HOUR + moment.minute


def _end_of(start: time, span: timedelta) -> time:
    """Конец окна: начало плюс длительность, внутри тех же суток.

    Через полночь эта сетка не переходит: самое позднее начало 11:00 плюс
    четыре часа — 15:00. Отдельная ветка «за полночь» не пишется, потому что
    случая, в котором она сработала бы, в программе перебора нет.
    """
    total = int(_minutes(start) + span.total_seconds() / MINUTES_IN_HOUR)
    if total >= 24 * MINUTES_IN_HOUR:
        raise ValueError(
            f"окно с началом {start:%H:%M} и длительностью {span} выходит за сутки: "
            "сетка перебора через полночь не идёт"
        )
    return _at(total)


def _reversal_word(reversal: Reversal) -> str:
    """Момент переворота по-русски."""
    return "через свечу" if reversal is Reversal.THROUGH_BAR else "в одной свече"


def _after_take_word(stop: bool) -> str:
    """Поведение после тейка по-русски."""
    return "стоп на день" if stop else "ждать сигнала"


def trading_mode(engine: EngineSettings) -> EngineSettings:
    """Настройки с включённым переворотом: выключенный робот сделок не делает.

    Умолчание движка — `Mode.OFF`, и это правильное умолчание для боя:
    программа, поднявшаяся сама по себе, торговать не начинает. Для перебора
    оно означало бы сто строк с нулём сделок, поэтому режим задаётся здесь
    явно и в одном месте.
    """
    return replace(engine, mode=Mode.REVERSE)


def trials_shown(count: int) -> str:
    """Сколько прогонов будет — фразой, которую печатают **до** запуска.

    Требование ТЗ §4.10 В. Число сохраняется вместе с результатом: без него
    нельзя сказать, насколько лучший результат отличается от шума, а поправка
    Шарпа на число испытаний считается прямо от него.
    """
    if count <= 0:
        raise ValueError(f"перебор из {count} прогонов не имеет смысла")
    penalty = math.log(count) if count > 1 else 0.0
    return (
        f"прогонов: {count}. Чем их больше, тем выше шанс, что лучший — "
        f"случайность: поправка на число испытаний растёт как ln(N) = {penalty:.2f}"
    )
