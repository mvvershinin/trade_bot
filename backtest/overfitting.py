"""Подгонка: как её меряют. Соседи по сетке, поправка Шарпа, доля провалов.

Зачем этот модуль
-----------------
Перебор всегда что-нибудь находит. Вопрос не в том, нашлось ли лучшее
сочетание — оно нашлось по построению, — а в том, отличается ли оно от шума.
Здесь три ответа на этот вопрос, и они отвечают на разное:

===========================  ==============================================
Соседи по сетке              значение, у которого соседи проваливаются, —
                             шум, а не находка. Самый дешёвый и самый
                             понятный владельцу счёта признак
Поправка Шарпа (DSR)         лучший из N испытаний обязан быть лучше,
                             чем **ожидаемый** лучший из N случайных.
                             Учитывает число испытаний, асимметрию,
                             эксцесс и длину истории
Доля провалов (PBO)          как часто лучший на подборе оказывался хуже
                             среднего на проверке. Считается по окнам
                             скользящей проверки
===========================  ==============================================

⚠️ **Ни одна из трёх не спасает короткую историю.** На 71 торговом дне
и ~140 сделках разброс итога сопоставим с самим итогом; поправки честно
это показывают, но не чинят. Вывод из красной поправки один: данных мало,
а не «надо взять другую формулу».

Модуль намеренно не знает ни про `Point`, ни про `Trial`: он работает
с числами и осями. Иначе `sweep` и `overfitting` ссылались бы друг на друга,
а разорвать такой круг потом дороже, чем не заводить.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import NormalDist

__all__ = [
    "ENOUGH_FOR_A_SHAPE",
    "EULER",
    "Shape",
    "deflated_sharpe",
    "expected_best_sharpe",
    "neighbours",
    "overfit_share",
    "rank_of",
]

#: Постоянная Эйлера–Маскерони. Входит в оценку ожидаемого максимума Шарпа
#: по N независимым испытаниям (Bailey, López de Prado, 2014).
EULER = 0.5772156649015329

#: Меньше двух значений — формы нет: разброс неопределён, и подстановка нуля
#: обратила бы Шарп в бесконечность, то есть подняла бы такую строку на первое
#: место в таблице перебора. Ровно там, где таблица заведена против подгонки.
ENOUGH_FOR_A_SHAPE = 2

_NORMAL = NormalDist()


@dataclass(frozen=True, slots=True)
class Shape:
    """Форма распределения результатов сделок: чем итог отличается от нормального.

    Поправка Шарпа считается не от одного числа прибыли, а от формы: серия
    из мелких плюсов и одного огромного минуса и серия ровных плюсов дают
    одинаковую среднюю и разный смысл. Асимметрия и эксцесс — ровно про это.

    `kurtosis` — **обычный**, а не избыточный: у нормального распределения
    он равен 3. Формула поправки написана под обычный, и подстановка
    избыточного тихо сдвинула бы результат.
    """

    count: int
    mean: float
    spread: float
    skew: float
    kurtosis: float

    @classmethod
    def of(cls, results: Sequence[float]) -> "Shape":
        """Форма по списку результатов сделок в рублях.

        Меньше двух значений — формы нет: `spread` неопределён, и подставлять
        вместо него ноль нельзя, иначе Шарп обратился бы в бесконечность.
        """
        count = len(results)
        if count < ENOUGH_FOR_A_SHAPE:
            return cls(count=count, mean=results[0] if results else 0.0,
                       spread=0.0, skew=0.0, kurtosis=0.0)
        mean = sum(results) / count
        second = sum((value - mean) ** 2 for value in results) / count
        spread = math.sqrt(second)
        if spread == 0.0:
            return cls(count=count, mean=mean, spread=0.0, skew=0.0, kurtosis=0.0)
        third = sum((value - mean) ** 3 for value in results) / count
        fourth = sum((value - mean) ** 4 for value in results) / count
        return cls(
            count=count, mean=mean, spread=spread,
            skew=third / spread ** 3, kurtosis=fourth / spread ** 4,
        )

    @property
    def sharpe(self) -> float | None:
        """Шарп на сделку: средняя, делённая на разброс. `None` — считать не из чего."""
        if self.count < ENOUGH_FOR_A_SHAPE or self.spread == 0.0:
            return None
        return self.mean / self.spread

    @property
    def error_of_total(self) -> float:
        """Стандартная ошибка **итога** в рублях: σ · √n.

        Число, которое надо ставить рядом с прибылью всегда. Замер 05.09.2026
        на умолчаниях: итог 2 089 ₽ при ошибке 8 104 ₽ — то есть 0,26 ошибки
        от нуля. Без этой цифры «плюс две тысячи» читается как результат.
        """
        return self.spread * math.sqrt(self.count)


def expected_best_sharpe(spread_of_sharpes: float, trials: int) -> float:
    """Какой Шарп покажет лучший из `trials` испытаний, если ни одно не работает.

    Оценка максимума `trials` независимых нормальных величин с разбросом
    `spread_of_sharpes` (Bailey, López de Prado, 2014). Это та планка, которую
    лучший результат обязан перепрыгнуть, чтобы считаться результатом:
    перебрав тысячу пустышек, лучшую из них получают всегда.
    """
    if trials < ENOUGH_FOR_A_SHAPE or spread_of_sharpes <= 0.0:
        return 0.0
    first = _NORMAL.inv_cdf(1.0 - 1.0 / trials)
    second = _NORMAL.inv_cdf(1.0 - 1.0 / (trials * math.e))
    return spread_of_sharpes * ((1.0 - EULER) * first + EULER * second)


def deflated_sharpe(best: Shape, spread_of_sharpes: float, trials: int) -> float | None:
    """Вероятность, что лучший результат не случаен. Ближе к 1 — лучше.

    Поправка Шарпа на четыре вещи разом: число испытаний, асимметрию, эксцесс
    и длину истории. Возвращает `None`, когда считать не из чего: сделок
    меньше двух, разброс нулевой либо знаменатель поправки вышел
    неположительным (так бывает при сильной асимметрии на короткой выборке —
    это отказ считать, а не ноль).

    ⚠️ Порога «сколько хорошо» здесь нет намеренно. В литературе принято 0,95,
    но при 66 проверочных сделках эта величина сама меряется с огромной
    погрешностью, и превращать её в светофор значило бы заменить одну
    самоуверенную цифру другой.
    """
    sharpe = best.sharpe
    if sharpe is None:
        return None
    planned = expected_best_sharpe(spread_of_sharpes, trials)
    under = 1.0 - best.skew * sharpe + (best.kurtosis - 1.0) / 4.0 * sharpe ** 2
    if under <= 0.0 or best.count < ENOUGH_FOR_A_SHAPE:
        return None
    return _NORMAL.cdf((sharpe - planned) * math.sqrt(best.count - 1) / math.sqrt(under))


def neighbours(
    axes: Sequence[tuple[tuple[str, float], ...]]
) -> tuple[tuple[int, ...], ...]:
    """Кто чей сосед по сетке: одна ось отличается на один шаг, остальные равны.

    Возвращает по кортежу номеров на каждую точку, в том же порядке, что вход.
    Точка без осей соседей не имеет: у «весь торговый день» и у переключателя
    режима соседства не бывает по природе, и выдумывать его нельзя.

    Соседство считается по **фактически встретившимся** значениям оси,
    а не по формуле шага: сетка бывает неравномерной, а «сосед» должен
    означать «следующее из того, что мы считали».
    """
    positions = [dict(item) for item in axes]
    steps = _steps(positions)
    found: list[tuple[int, ...]] = []
    for here, mine in enumerate(positions):
        if not mine:
            found.append(())
            continue
        found.append(tuple(
            there for there, other in enumerate(positions)
            if there != here and _one_step_apart(mine, other, steps)
        ))
    return tuple(found)


def _steps(positions: Sequence[dict[str, float]]) -> dict[str, list[float]]:
    """Какие значения встретились на каждой оси, по возрастанию."""
    seen: dict[str, set[float]] = {}
    for item in positions:
        for name, value in item.items():
            seen.setdefault(name, set()).add(value)
    return {name: sorted(values) for name, values in seen.items()}


def _one_step_apart(
    mine: dict[str, float], other: dict[str, float], steps: dict[str, list[float]]
) -> bool:
    """Точки различаются ровно одной осью и ровно на один шаг по ней."""
    if mine.keys() != other.keys():
        return False
    differ = [name for name in mine if mine[name] != other[name]]
    if len(differ) != 1:
        return False
    name = differ[0]
    line = steps[name]
    return abs(line.index(mine[name]) - line.index(other[name])) == 1


def rank_of(value: float, values: Sequence[float]) -> int:
    """Место значения в ряду, снизу вверх: 1 — худшее.

    Равные значения получают одинаковое место — место худшего из равных.
    Иначе порядок строк в таблице влиял бы на долю провалов.
    """
    return 1 + sum(1 for other in values if other < value)


def overfit_share(places: Sequence[tuple[int, int]]) -> float | None:
    """Доля окон, где лучший на подборе оказался ниже середины на проверке.

    Это упрощённая оценка PBO (probability of backtest overfitting): для
    каждого окна берётся место, которое занял на проверке победитель подбора,
    и считается доля окон, где место оказалось ниже медианы.

    `places` — пары «место, всего мест». Пустой список даёт `None`: доли
    от нуля окон не бывает, а ноль читался бы как «подгонки нет».

    ⚠️ При семи окнах эта доля принимает восемь значений, от 0 до 1 шагом
    примерно 0,14. Различать по ней 0,29 и 0,43 нельзя, и в отчёт она идёт
    словами «в K окнах из N», а не одним процентом.
    """
    if not places:
        return None
    below = sum(1 for place, total in places if place <= (total + 1) / 2)
    return below / len(places)
