"""Свечи → сигнал → заявка → позиция; окна, паузы, тейк, лимиты, сверка.

Движок владеет всем, что превращает **намерение** торгового модуля
в **позицию**: торговым окном, днями недели, режимом работы, моментом
переворота, тейк-профитом, расчётом объёма, лимитами и журналом решений.
Всё это общее для всех модулей и в каждом из них не дублируется
(ARCHITECTURE.md §3).

Три правила, нарушение которых не падает, а меняет список сделок
-----------------------------------------------------------------
**Движок не знает, откуда пришла свеча.** Ни одного `if режим == тестер`.
Свечи приходят через порт, заявки уходят через порт, и у портов нет признака,
по которому боевой путь отличается от прогона по истории. Нарушение оставляет
тесты зелёными и возвращает продукт к состоянию «тестер врёт, доказать нечем»
(ARCHITECTURE.md §1).

**Порядок обработки свечи значим.** Десять шагов PROTOTYPE.md §2, в точном
порядке. Ключевое: снимок открытых позиций берётся **до** закрытия — отсюда
«переворот через свечу». Перестановка шагов не роняет ничего, она просто даёт
другие сделки.

**Торговое окно считается по времени ЗАКРЫТИЯ свечи, неравенства строгие.**
Свеча, закрывшаяся ровно в 10:05, в окно 10:05–11:00 не попадает.

Что где лежит
-------------
=====================  ====================================================
`engine/window.py`     торговое окно и торговый день, московское время
`engine/settings.py`   общие настройки: режим, окно, выходные, объём, тейк
`engine/take_profit.py` уровень тейка: неподвижный и скользящий
`engine/contracts.py`  значения: позиция, заявка, сделка, строка журнала
`engine/bars.py`       перекладка свечи слоя данных в свечу модуля
`engine/guards.py`     предохранители по деньгам: числа и вердикты
`engine/pipeline.py`   десять шагов — чистая синхронная функция
`engine/ports.py`      источник свечей и исполнитель заявок
`engine/runner.py`     склейка: синхронное ядро и асинхронный адаптер
=====================  ====================================================

Заявка подаётся ДО того, как движок себе что-то запишет
--------------------------------------------------------
Разбор свечи ничего не фиксирует: он возвращает заявки и **два** состояния —
на случай, что исполнитель их примет, и на случай, что нет. Состояние и журнал
закрепляются по ответу исполнителя (`Engine.analyse` и `Engine.accept`).
Обратный порядок даёт при отказе брокера позицию, закрытую в журнале
и открытую на счёте. Отказ останавливает робота явно (`Engine.halted`)
и пишет строку уровня ERROR — а не роняет цикл свечей.

Тейк-профит: уровень считает движок, сторожит исполнитель
----------------------------------------------------------
Движок вычисляет уровень и подаёт заявку «сторожи X»; факт срабатывания
приходит обратно **сделкой**, сославшейся на эту же заявку. Ни из какого поля
свечи он не выводится и ни из какого совпадения цен не угадывается
(решение 0008). Отсюда два запрета, действующих на весь слой:

* `high` и `low` в `engine/` не читаются нигде, кроме перекладки свечи;
* причина выхода берётся из **своей заявки**, а не из полей сделки.

Три предохранителя по деньгам
------------------------------
Потолок объёма, дневной лимит убытка и запас свободных средств живут
в `engine/guards.py` (числа) и в `engine/pipeline.py` (слова журнала).
Умолчание у всех трёх — **выключено**: у прототипа предохранителей нет,
и любое их срабатывание в сверке дало бы другой список сделок.

Деньги счёта движок **не спрашивает** — их кладут внутрь (`Engine.on_funds`).
Ссылки на поставщика у движка нет, и различить боевой портфель от прогона
по истории он не может, как и в случае со свечами (ARCHITECTURE.md §1).

Чего в движке пока нет
----------------------
Третьего варианта поведения после тейка — «сразу восстановить позицию»
(ТЗ §4.4 В): однозначного прочтения у него нет, разбор в `engine/settings.py`.
Интервалов пауз, предела числа сделок за день (решение 0021),
сверки позиции после обрыва связи — этап 2.
"""

from engine.bars import Intake, check_market_candle, close_time, intake, to_bar
from engine.contracts import (
    AccountFunds,
    DayMoney,
    EngineState,
    ExitReason,
    Fill,
    JournalEntry,
    JournalLevel,
    OrderAction,
    OrderRequest,
    Position,
    PositionState,
    Side,
    Step,
)
from engine.guards import (
    FUNDS_MAX_AGE,
    DayResult,
    Sizing,
    Unchecked,
    day_result,
    entry_size,
    funds_stale,
    unchecked_guards,
    usable_funds,
)
from engine.pipeline import (
    FillOutcome,
    Outcome,
    after_refused_cancel,
    after_refused_exit,
    after_submit,
    apply_fill,
    exit_signal,
    money,
    order_words,
    percent,
    process_closed_candle,
    take_order_id,
)
from engine.ports import (
    CandleSource,
    ExecutionRefused,
    MarketCandle,
    OrderExecutor,
    Refusal,
    Timeframe,
)
from engine.runner import Engine
from engine.settings import (
    MAX_CLOSE_WAIT_BARS,
    EngineSettings,
    Mode,
    PartialCandles,
    Reversal,
)
from engine.take_profit import (
    TakeProfit,
    covers_commission,
    fixed_level,
    round_level,
    target_profit,
)
from engine.window import (
    MSK,
    DayMarks,
    DayRule,
    DayVerdict,
    TradingWindow,
    day_verdict,
    in_moscow,
    is_trading_day,
    minutes_of_day,
    verdict_of_day,
)

__all__ = [
    # окно и время
    "MSK",
    "TradingWindow",
    "in_moscow",
    "is_trading_day",
    "minutes_of_day",
    # календарь нерабочих дней (решение 0038)
    "DayMarks",
    "DayRule",
    "DayVerdict",
    "day_verdict",
    "verdict_of_day",
    # настройки
    "EngineSettings",
    "MAX_CLOSE_WAIT_BARS",
    "Mode",
    "Reversal",
    "PartialCandles",
    # значения
    "Side",
    "Position",
    "PositionState",
    "OrderAction",
    "OrderRequest",
    "ExitReason",
    "Fill",
    "JournalEntry",
    "JournalLevel",
    "Step",
    "EngineState",
    "AccountFunds",
    "DayMoney",
    # предохранители по деньгам
    "FUNDS_MAX_AGE",
    "DayResult",
    "Sizing",
    "Unchecked",
    "day_result",
    "entry_size",
    "funds_stale",
    "unchecked_guards",
    "usable_funds",
    # тейк-профит
    "TakeProfit",
    "round_level",
    "fixed_level",
    "target_profit",
    "covers_commission",
    "take_order_id",
    # свечи
    "Intake",
    "intake",
    "to_bar",
    "close_time",
    "check_market_candle",
    # разбор
    "Outcome",
    "FillOutcome",
    "process_closed_candle",
    "after_submit",
    "after_refused_cancel",
    "after_refused_exit",
    "apply_fill",
    "exit_signal",
    "money",
    "order_words",
    "percent",
    # порты и склейка
    "CandleSource",
    "OrderExecutor",
    "ExecutionRefused",
    "Refusal",
    "MarketCandle",
    "Timeframe",
    "Engine",
]
