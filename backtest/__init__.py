"""Прогон по истории, модель исполнения, метрики, независимый период.

`execution` — реализация порта «исполнитель заявок»: цена сделки, издержки,
сторож уровня тейка внутри бара. Единственное место в проекте, где записаны
правила исполнения прототипа.

`history` — источник свечей из готового ряда, цикл прогона и подсчёт того,
что из прогона получилось.

`assumptions` — чем показанный итог отличается от настоящего счёта. Оговорка
считается из самого прогона и висит свойством на `HistoryRun`, поэтому едет
вместе с числом: показать деньги прогона и не иметь под рукой оговорку
к ним нельзя.

⚠️ Оптимизатора, независимого периода и таблицы результатов здесь нет:
это Э1-11, Э1-13 и Э1-19.
"""

from backtest.assumptions import (
    BESIDE_THE_NUMBER_LIMIT,
    TUNED_FROM,
    TUNED_UNTIL,
    Assumption,
    assumptions,
    headline,
)
from backtest.execution import (
    BROKER_FEE_PER_CONTRACT,
    CoarseBar,
    Costs,
    ExecutionModel,
    Guard,
    LevelGaps,
    MinuteOrder,
    Minutes,
    OpenLeg,
    Tariff,
    minute_points,
    selling,
)
from backtest.history import (
    BREATHE_EVERY,
    Bucket,
    DayResult,
    Deal,
    HistoryExecutor,
    HistoryRun,
    HistorySource,
    Pair,
    Plan,
    Summary,
    deal_results,
    pairs,
    replay,
    reversal_entries,
    summarise,
)

__all__ = [
    "BESIDE_THE_NUMBER_LIMIT",
    "BREATHE_EVERY",
    "BROKER_FEE_PER_CONTRACT",
    "TUNED_FROM",
    "TUNED_UNTIL",
    "Assumption",
    "Bucket",
    "Costs",
    "DayResult",
    "Deal",
    "ExecutionModel",
    "CoarseBar",
    "Guard",
    "HistoryExecutor",
    "HistoryRun",
    "HistorySource",
    "LevelGaps",
    "MinuteOrder",
    "Minutes",
    "OpenLeg",
    "Pair",
    "Plan",
    "Summary",
    "Tariff",
    "assumptions",
    "deal_results",
    "headline",
    "minute_points",
    "pairs",
    "replay",
    "reversal_entries",
    "selling",
    "summarise",
]
