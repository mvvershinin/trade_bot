"""Что получилось из перебора: выбор подбора, устойчивость, поправки.

Здесь считаются **числа** отчёта и только они; как это выглядит на экране —
в `backtest.table`. Разделение не косметическое: сюда смотрят тесты, и
проверять «в таблице есть строка с таким текстом» вместо «выбор подбора занял
на проверке такое место» значит проверять вёрстку вместо смысла.

Три вещи, ради которых модуль существует
----------------------------------------
1. **Две колонки рядом.** У каждой строки есть подбор и проверка, и строка
   без второй колонки здесь не собирается — `Row` требует обе.
2. **Что выбрал бы подбор.** Лучшая на подборе строка называется отдельно
   вместе с тем, чем она обернулась на проверке и какое место там заняла.
   Это единственный способ показать цену подгонки в рублях.
3. **Устойчивость.** Рядом с каждой строкой — как показали себя её соседи
   по сетке. Значение, у которого соседи проваливаются, находкой не является,
   и таблица обязана это показывать, а не только максимум.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from statistics import pstdev

from backtest.execution import Costs
from backtest.overfitting import (
    ENOUGH_FOR_A_SHAPE,
    Shape,
    deflated_sharpe,
    neighbours,
    overfit_share,
    rank_of,
)
from backtest.split import Fold, Period
from backtest.sweep import Block, Money, Point, Trial

__all__ = [
    "BlockReport",
    "Choice",
    "Forward",
    "Row",
    "Step",
    "Study",
    "Verdict",
    "best_on",
    "block_report",
    "forward",
    "study",
]


@dataclass(frozen=True, slots=True)
class Verdict:
    """Устойчиво ли значение: как показали себя его соседи по сетке.

    `stable` — `None`, когда соседей нет вовсе (точка вне сетки: «весь день»,
    «без тейка», переключатель режима). `None` здесь означает «вопрос не задан»,
    а не «неустойчиво», и в таблице печатается прочерком, а не словом.
    """

    neighbours: int
    worst: float | None
    best: float | None
    stable: bool | None

    @property
    def word(self) -> str:
        """Вердикт словом для таблицы."""
        if self.stable is None:
            return "—"
        return "устойчиво" if self.stable else "шум"


@dataclass(frozen=True, slots=True)
class Row:
    """Строка таблицы: настройка, подбор, проверка, соседи.

    ⚠️ Обе колонки — **обязательные поля**. Строку с одним только подбором
    здесь не собрать, и это не украшение конструктора: правило ТЗ §4.10 Г
    держится тем, что вторую колонку нельзя не заполнить.
    """

    label: str
    tuning: Money
    checking: Money
    verdict: Verdict


@dataclass(frozen=True, slots=True)
class Choice:
    """Что выбрал бы подбор и что из этого вышло.

    `place` — место на проверке снизу вверх среди всех строк блока, `total` —
    сколько строк. «Место 12 из 58» читается прямо: подбор выбрал строку,
    которая на проверке оказалась хуже сорока шести других.
    """

    label: str
    tuning: Money
    checking: Money
    place: int
    total: int
    verdict: Verdict

    @property
    def cost(self) -> float | None:
        """Разница между подбором и проверкой в рублях. Цена подгонки в деньгах."""
        if self.tuning.net is None or self.checking.net is None:
            return None
        return self.checking.rubles - self.tuning.rubles


@dataclass(frozen=True, slots=True)
class BlockReport:
    """Один блок перебора: строки, выбор подбора, поправка Шарпа."""

    name: str
    title: str
    question: str
    rows: tuple[Row, ...]
    choice: Choice
    deflated: float | None


@dataclass(frozen=True, slots=True)
class Step:
    """Одно окно скользящей проверки: что выбрал подбор и сколько это дало."""

    fold: Fold
    label: str
    money: Money
    place: int
    total: int


@dataclass(frozen=True, slots=True)
class Forward:
    """Скользящая проверка вперёд целиком.

    `rubles` — сумма проверочных отрезков. Складывать их **можно**: окна
    сдвигаются шагом ровно в длину проверки, поэтому проверочные отрезки
    не пересекаются ни одним днём. Отрезки подбора пересекаются, и их сумма
    в отчёт не идёт ни разу.

    `baseline` — те же проверочные дни на умолчаниях программы. Без него
    число «столько дала бы переподстройка» не с чем сравнить.
    """

    steps: tuple[Step, ...]
    rubles: float
    baseline: float
    overfit: float | None


@dataclass(frozen=True, slots=True)
class Study:
    """Весь отчёт в числах."""

    symbol: str
    days: int
    since: object
    until: object
    split: Fold
    folds: tuple[Fold, ...]
    costs: Costs
    runs: int
    blocks: tuple[BlockReport, ...]
    forward: Forward
    error_of_total: float
    straddling: int
    outside: int
    notes: tuple[str, ...]


def best_on(trials: Sequence[Trial], period: Period) -> int:
    """Номер строки с наибольшими деньгами на отрезке.

    Одна функция на весь модуль намеренно: выбор победителя делается в двух
    местах — в блоке и в каждом окне скользящей проверки, — и написанный
    дважды он разойдётся молча. Разойтись он может ровно в одну сторону:
    один из двух начнёт смотреть в проверку.
    """
    return max(range(len(trials)), key=lambda index: trials[index].on(period).rubles)


def _verdicts(
    money: Sequence[Money], axes: Sequence[tuple[tuple[str, float], ...]]
) -> tuple[Verdict, ...]:
    """Вердикт устойчивости для каждой строки блока по её соседям на проверке."""
    kin = neighbours(axes)
    return tuple(
        _one_verdict(money[here], [money[there].rubles for there in mine])
        for here, mine in enumerate(kin)
    )


def _one_verdict(mine: Money, around: Sequence[float]) -> Verdict:
    """Точка устойчива, если и она, и все её соседи на проверке в плюсе."""
    if not around:
        return Verdict(neighbours=0, worst=None, best=None, stable=None)
    return Verdict(
        neighbours=len(around),
        worst=min(around),
        best=max(around),
        stable=mine.rubles > 0 and min(around) > 0,
    )


def block_report(block: Block, trials: Sequence[Trial], split: Fold, runs: int) -> BlockReport:
    """Собрать отчёт по блоку: строки в порядке сетки плюс выбор подбора.

    Порядок строк — **порядок сетки**, а не по убыванию прибыли. Сортировка
    по прибыли ставит читателя в позу «смотрю на максимум», ровно ту, против
    которой заведён весь этот отчёт; выбор подбора называется отдельной
    строкой ниже, и там же сказано, чем он обернулся.
    """
    tuning = [trial.on(split.tuning) for trial in trials]
    checking = [trial.on(split.checking) for trial in trials]
    verdicts = _verdicts(checking, [trial.point.axes for trial in trials])
    rows = tuple(
        Row(label=trial.point.label, tuning=tuning[at], checking=checking[at],
            verdict=verdicts[at])
        for at, trial in enumerate(trials)
    )
    at_best = best_on(trials, split.tuning)
    places = [item.rubles for item in checking]
    choice = Choice(
        label=trials[at_best].point.label,
        tuning=tuning[at_best], checking=checking[at_best],
        place=rank_of(places[at_best], places), total=len(places),
        verdict=verdicts[at_best],
    )
    return BlockReport(
        name=block.name, title=block.title, question=block.question,
        rows=rows, choice=choice,
        deflated=_deflated(trials, split.checking, at_best, runs),
    )


def _deflated(
    trials: Sequence[Trial], period: Period, at_best: int, runs: int
) -> float | None:
    """Поправка Шарпа на число испытаний для выбранной строки.

    Число испытаний берётся по **всему** перебору, а не по блоку: смотрели
    на всё, значит выбирали из всего. Взять размер блока значило бы уменьшить
    поправку ровно на то, что от неё прячут.
    """
    sharpes = [trial.form(period).sharpe for trial in trials]
    known = [value for value in sharpes if value is not None]
    if len(known) < ENOUGH_FOR_A_SHAPE:
        return None
    return deflated_sharpe(trials[at_best].form(period), pstdev(known), runs)


def _no_common_days(folds: Sequence[Fold]) -> None:
    """Проверочные отрезки окон не делят ни одного дня.

    ⚠️ Сторож стоит здесь, хотя `walk_forward` строит окна правильно.
    Причина простая: складывать деньги отрезков **можно только** пока они
    не пересекаются, а `forward` принимает любые окна — из другого расчёта,
    из ключа командной строки, из будущей схемы деления. Пересечение
    посчитало бы общий день дважды, итог вырос бы и выглядел бы правдоподобно.

    :raises ValueError: два окна делят день на проверке.
    """
    for earlier, later in zip(folds, folds[1:], strict=False):
        if earlier.checking.overlaps(later.checking):
            raise ValueError(
                f"окна {earlier.number} и {later.number} делят дни на проверке: "
                f"{earlier.checking} и {later.checking}. Их деньги нельзя "
                "складывать — общие дни посчитались бы дважды"
            )


def forward(
    trials: Sequence[Trial], folds: Sequence[Fold], plain: Point
) -> Forward:
    """Скользящая проверка вперёд: в каждом окне подбор выбирает заново.

    В каждом окне победитель подбора определяется **только** по сделкам
    отрезка подбора, а деньги считаются по сделкам отрезка проверки того же
    окна. Настройки следующего окна выбираются заново, но задним числом
    предыдущие результаты не переписываются.

    ⚠️ На стыке окон робот считается плоским: сделки, разорванные границей
    отрезка, не идут ни в одно окно. Иначе переподстройка получала бы
    в наследство позицию, открытую при других настройках.
    """
    _no_common_days(folds)
    steps: list[Step] = []
    places: list[tuple[int, int]] = []
    for fold in folds:
        at_best = best_on(trials, fold.tuning)
        money = [trial.on(fold.checking).rubles for trial in trials]
        place = rank_of(money[at_best], money)
        steps.append(Step(
            fold=fold, label=trials[at_best].point.label,
            money=trials[at_best].on(fold.checking), place=place, total=len(money),
        ))
        places.append((place, len(money)))
    at_plain = _index_of(trials, plain)
    return Forward(
        steps=tuple(steps),
        rubles=sum(step.money.rubles for step in steps),
        baseline=sum(trials[at_plain].on(fold.checking).rubles for fold in folds),
        overfit=overfit_share(places),
    )


def _index_of(trials: Sequence[Trial], point: Point) -> int:
    """Найти строку с этими настройками. Не нашлась — отказ, а не подстановка.

    Умолчания обязаны быть в переборе: без них колонка «для сравнения» брала бы
    число неизвестно откуда. Молчаливая замена на ближайшую строку дала бы
    правдоподобное сравнение с не тем.
    """
    for at, trial in enumerate(trials):
        if trial.point.engine == point.engine and trial.point.strategy == point.strategy:
            return at
    raise ValueError(
        f"в переборе нет строки с настройками «{point.label}». Сравнивать "
        "переподстройку не с чем: строка умолчаний обязана быть в сетке"
    )


def _one_answer_per_setting(trials: Sequence[Trial], period: Period) -> None:
    """Одни настройки — одни деньги, в каком бы блоке они ни встретились.

    Умолчания программы попадают в каждый из трёх блоков: в блоке окна это
    строка «10:05–11:00 (умолчание)», в блоке средней — «средняя 15,
    тейк 0,5 %», в блоке режимов — «через свечу · стоп на день». Это одни
    и те же настройки, и числа у них обязаны совпасть до рубля.

    Проверка стоит здесь, а не в тесте, потому что ловит она не ошибку кода,
    а **расхождение отчёта с самим собой**: если сборка сетки, разбор сделок
    или раскладка по отрезкам начнут зависеть от порядка строк, три числа
    разойдутся, и в таблице будет три разные правды про одни настройки.
    Тихо такое не проходит.

    :raises ValueError: одни настройки дали в разных блоках разные деньги.
    """
    seen: dict[tuple[object, object], tuple[str, float]] = {}
    for trial in trials:
        key = (trial.point.engine, trial.point.strategy)
        money = trial.on(period).rubles
        first = seen.setdefault(key, (trial.point.label, money))
        if first[1] != money:
            raise ValueError(
                f"одни и те же настройки дали разные деньги на проверке: "
                f"«{first[0]}» — {first[1]:.0f} ₽, «{trial.point.label}» — "
                f"{money:.0f} ₽. В таблице оказалось бы три разные правды "
                "про одну настройку"
            )


def study(  # noqa: PLR0913 — все шесть суть разные части одного отчёта, и склеивать их в объект-мешок значит прятать, что откуда взялось
    *,
    symbol: str,
    days: Sequence[object],
    split: Fold,
    folds: Sequence[Fold],
    costs: Costs,
    parts: Sequence[tuple[Block, tuple[Trial, ...]]],
    plain: Point,
    notes: Sequence[str] = (),
) -> Study:
    """Свести весь перебор в один отчёт.

    `parts` — блоки вместе с их прогонами. Скользящая проверка считается
    по **всем** прогонам разом: в бою настройки выбирают из всего, что
    смотрели, а не из одного блока.
    """
    everything = tuple(trial for _, trials in parts for trial in trials)
    _one_answer_per_setting(everything, split.checking)
    runs = len(everything)
    return Study(
        symbol=symbol, days=len(days), since=days[0], until=days[-1],
        split=split, folds=tuple(folds), costs=costs, runs=runs,
        blocks=tuple(block_report(block, trials, split, runs) for block, trials in parts),
        forward=forward(everything, folds, plain),
        error_of_total=_error(everything, split.checking, plain),
        straddling=max((trial.straddling for trial in everything), default=0),
        outside=max((trial.outside for trial in everything), default=0),
        notes=tuple(notes),
    )


def _error(trials: Sequence[Trial], period: Period, plain: Point) -> float:
    """Стандартная ошибка итога на умолчаниях: с чем сравнивать любую прибыль."""
    form: Shape = trials[_index_of(trials, plain)].form(period)
    return form.error_of_total
