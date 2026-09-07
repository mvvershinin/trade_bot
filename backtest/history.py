"""Прогон движка по истории: источник свечей, модель исполнения, сделки, итог.

Здесь живут **тестовые реализации двух портов движка** и подсчёт того, что
из прогона получилось. Движок про этот модуль не знает и знать не может:
у портов нет ни признака «это прогон», ни метода, которого не было бы
у боевой реализации (ARCHITECTURE.md §1).

======================  ==============================  ============================
Порт                    Боевая реализация               Здесь
======================  ==============================  ============================
Источник свечей         поток брокера + догрузка        список свечей из базы
Исполнитель заявок      заявки в API брокера            модель исполнения прототипа
======================  ==============================  ============================

Где живут правила исполнения
-----------------------------
**Не здесь.** Все до одного — в `backtest/execution.py`: исполнение по открытию
следующей свечи, сторож уровня внутри бара, проверка «заявка не исполняется
раньше, чем подана», комиссия и проскальзывание. До Э1-9 они были написаны
дважды — тут и в тестовой оснастке движка, — и это ровно та болезнь, о которой
предупреждает решение 0008. `HistoryExecutor` теперь наследник общей модели
и добавляет к ней только запись результата: закрытую сделку с деньгами
и расчётную цену выхода по сработавшему уровню.

Что здесь остаётся
------------------
Подсчёт того, что из прогона получилось: сделки, кривая доходности, разбивки,
профит-фактор, пары «прогноз — факт» и сам цикл прогона.

⚠️ **Модуль написан на Э1-18** (сведение программы) — раньше, чем Э1-9, потому
что без реализации обоих портов движок по истории не гоняется вовсе, а без
прогона окно нечем наполнить. Оптимизатора, независимого периода и таблицы
результатов здесь по-прежнему нет: это Э1-11, Э1-13 и Э1-19.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime

from backtest.assumptions import Assumption, assumptions, headline
from backtest.execution import (
    Costs,
    ExecutionModel,
    OpenLeg,
    commissions_disagree,
    selling,
)
from engine import (
    Engine,
    EngineSettings,
    ExitReason,
    Fill,
    JournalEntry,
    MarketCandle,
    OrderAction,
    OrderRequest,
    Position,
    Refusal,
    Side,
    close_time,
    in_moscow,
)
from strategies import Strategy

__all__ = [
    "Assumption",
    "Deal",
    "Plan",
    "Pair",
    "DayResult",
    "Bucket",
    "Summary",
    "HistoryRun",
    "HistorySource",
    "HistoryExecutor",
    "reversal_entries",
    "pairs",
    "summarise",
    "deal_results",
    "replay",
]

#: Через сколько свечей прогон отдаёт управление циклу событий. Число не про
#: скорость, а про живость окна: прогон идёт в том же цикле, что и интерфейс
#: (решение 0005), и без передышки окно замирает на всё время прогона.
BREATHE_EVERY = 250


# ---------------------------------------------------------------------------
# Что получается из прогона
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Deal:
    """Закрытая сделка: вход, выход и деньги.

    `commission` — комиссия **обеих сторон** в рублях или `None`, если тариф
    не задан. `None` — это не ноль: подставленный ноль объявил бы любую сделку
    окупившей комиссию, а реверсная система на пятиминутках делает много
    переворотов (DOMAIN.md §5).
    """

    side: Side
    volume: float
    entry_time: datetime
    entry_price: float
    entry_order_id: str
    exit_time: datetime
    exit_price: float
    exit_order_id: str
    exit_reason: ExitReason
    commission: float | None = None
    ruble_per_point: float = 1.0

    @property
    def points(self) -> float:
        """Движение цены в пунктах в пользу позиции."""
        sign = 1.0 if self.side is Side.LONG else -1.0
        return (self.exit_price - self.entry_price) * sign

    @property
    def gross(self) -> float:
        """Валовая прибыль в рублях. Результатом в этом проекте не считается."""
        return self.points * self.volume * self.ruble_per_point

    @property
    def net(self) -> float | None:
        """Чистая прибыль. `None`, пока тариф комиссии не задан."""
        return None if self.commission is None else self.gross - self.commission

    @property
    def percent(self) -> float:
        """Движение цены в процентах от цены входа. Комиссия сюда не входит."""
        return self.points / self.entry_price * 100.0 if self.entry_price else 0.0


@dataclass(frozen=True, slots=True)
class Plan:
    """Что робот **рассчитывал**: заявка и цена на момент решения.

    Слой «прогноз» на графике (DOMAIN.md §8) строится отсюда, и цена берётся
    по-разному у двух видов заявок — потому что «расчётная цена» у них разная:

    * **рыночная заявка** — закрытие свечи, на которой принято решение. Другой
      цены у робота в этот момент нет: решение принимается на закрытии свечи,
      а исполнится оно по открытию следующей. Разница между этими двумя ценами
      и есть то, что показывает наложение слоёв;
    * **сторожимый уровень** (тейк) — сам уровень. Заранее известна цена,
      но не время: уровень ждёт, пока его коснутся. Временем берётся закрытие
      той свечи, на которой уровень задели, поэтому по времени такая пара
      всегда совпадает, а по цене расходится ровно настолько, насколько
      брокер исполнил не по уровню.

    ⚠️ `decided_at` **всегда время закрытия свечи** — в соглашении движка.
    У сделки (`Fill.at`) соглашение другое: это момент внутри бара. Ось
    графика размечена третьим образом — открытиями свечей. Перевод обоих
    времён на ось делает `app/convert.py` (`chart_time` для `decided_at`,
    `bar_open` для `Fill.at`), и он обязателен, иначе слои уезжают на бар.

    Сравнение с исполнением по имени заявки — точное, без догадок: `order_id`
    ставит движок, и то же имя приходит обратно в сделке.
    """

    order_id: str
    decided_at: datetime
    price: float
    side: Side
    action: OrderAction
    exit_reason: ExitReason | None = None


@dataclass(frozen=True, slots=True)
class DayResult:
    """Один торговый день кривой доходности (ТЗ §4.10 Б).

    `profit` — результат дня, `cumulative` — накопленный итог с начала периода.
    Оба нужны вместе, и ровно поэтому кривая вообще есть в отчёте: итоговая
    цифра прибыли одинакова у ровного роста и у двух удачных дней среди
    убыточных, а эти два случая — разные стратегии.

    День берётся по времени **входа** в сделку, в МСК. Почему вход, а не
    выход, — в докстринге `Summary`.
    """

    day: date
    trades: int
    profit: float
    cumulative: float


@dataclass(frozen=True, slots=True)
class Bucket:
    """Строка разбивки: сколько сделок и сколько денег пришлось на ключ.

    `key` — день недели (`0` понедельник … `6` воскресенье, как у
    `datetime.weekday()` и как в `engine.is_trading_day`) либо час суток
    `0…23` по МСК. Что именно, задаёт поле `Summary`, в котором строка лежит:
    `by_weekday` или `by_hour`.

    `profitable` — сколько из `trades` закрылись в плюс. Сделка ровно в ноль
    прибыльной не считается.
    """

    key: int
    trades: int
    profitable: int
    profit: float


@dataclass(frozen=True, slots=True)
class Summary:
    """Итог за период по ТЗ §4.10 Б. Числа считаются здесь и только здесь.

    Денежная база — одна на весь итог
    ----------------------------------
    Всё, что считается от результата сделки — просадка, доля прибыльных,
    профит-фактор, средняя, лучшая и худшая сделка, кривая по дням и обе
    разбивки, — берётся по **чистой** прибыли, если тариф комиссии задан,
    и по валовой, если нет. Какая база в этом итоге, видно по `net_profit`:
    пусто — значит тариф не задан и посчитано по валовой.

    Смешивать базы внутри одного отчёта нельзя. Валовая прибыль в этом проекте
    результатом не является (DOMAIN.md §5): реверсная система на пятиминутках
    делает много переворотов, и комиссия — первый кандидат съесть весь плюс.

    Просадка — наибольшее падение накопленной кривой от достигнутой вершины.

    Считаются только **закрытые** сделки. Позиция, оставшаяся открытой на конце
    периода, не входит сюда ни одним рублём: её бумажная прибыль живёт
    в `HistoryRun.position` и складывать её с реализованной нельзя. Сводная
    статистика прототипа их складывает — и получает цифру, которой на счёте
    нет, потому что позиция ещё может закрыться как угодно.

    Переворот — **выход по обратному сигналу, за которым следующей сделкой
    идёт вход в противоположную сторону**. Число переворотов обязательно
    в отчёте: именно они съедают прибыль комиссией (DOMAIN.md §5).

    Профит-фактор
    -------------
    Сумма прибыльных сделок, делённая на сумму убыточных **по модулю**. Сделка
    ровно в ноль не попадает ни в числитель, ни в знаменатель.

    ⚠️ **При нуле убытков он не определён и равен `None`, а не «бесконечности».**
    Делить не на что — это не «стратегия бесконечно хороша», а «убыточных
    сделок в выборке не встретилось», то есть выборка мала. Подстановка
    бесконечности (или заведомо большого числа) поднимала бы такой прогон
    на первое место в таблице перебора — ровно там, где таблица и заводится
    против подгонки: три сделки подряд в плюс результатом не являются.

    ⚠️ **С профит-фактором прототипа не сравнивается, пока задан тариф.**
    PF 1,50 из DOMAIN.md §4 посчитан по **валовой** прибыли: комиссию тестер
    прототипа в журнал не переносил, её считали сбоку. Наш профит-фактор при
    заданном тарифе всегда меньше. Сойтись эти два числа могут только при
    пустом тарифе, когда обе стороны считают одно и то же.

    Разбивки: по времени ВХОДА, по Москве
    --------------------------------------
    `by_day`, `by_weekday` и `by_hour` относят сделку целиком к моменту её
    **входа**, приведённому к МСК. Это выбор, а не единственная возможность,
    и человек, читающий отчёт, обязан знать, какую именно картину он видит:
    разбивка по времени **выхода** дала бы другую. Сделка, открытая в 10:55
    и закрытая в 11:00 по концу окна, по входу лежит в часе 10, а по выходу —
    в часе 11.

    Вход выбран потому, что в этот момент принято решение робота: разбивка
    отвечает на вопрос «когда входить», а не «когда зафиксировалась прибыль».
    Наблюдение заказчика про понедельник–вторник (ТЗ §4.10 Б) проверяется
    этой же разбивкой — но проверяется, а не подтверждается: на выборке
    в несколько недель разница между днями недели набирается из единиц сделок.

    Строки без сделок в разбивки не попадают: `by_weekday` бывает короче семи
    строк, `by_hour` — короче двадцати четырёх. «Ноль сделок» и «ноль рублей» —
    разные вещи, а строка «среда: 0 ₽» читается как результат.

    Три разбивки разносят одни и те же сделки, поэтому суммы `profit` по
    `by_day`, по `by_weekday` и по `by_hour` сходятся между собой и с
    `net_profit` — или с `gross_profit`, если тариф не задан. Сходятся
    с точностью двоичного сложения: слагаемые те же, а порядок разный,
    и сравнивать эти суммы на равенство «в лоб» нельзя.
    """

    trades: int = 0
    profitable: int = 0
    profitable_share: float | None = None
    gross_profit: float = 0.0
    commission: float | None = None
    net_profit: float | None = None
    max_drawdown: float | None = None
    reversals: int = 0
    profit_factor: float | None = None
    average_trade: float | None = None
    best_trade: float | None = None
    worst_trade: float | None = None
    by_day: tuple[DayResult, ...] = ()
    by_weekday: tuple[Bucket, ...] = ()
    by_hour: tuple[Bucket, ...] = ()


@dataclass(frozen=True, slots=True)
class HistoryRun:
    """Всё, что дал прогон. Ни одного поля, посчитанного дважды.

    `costs` — издержки, на которых прогон считался: тариф и проскальзывание.
    Лежат в результате, а не только в настройках, ровно потому, что отчёт
    обязан называть их вслух: «прибыль 2 923 ₽» без строки «комиссия 3 556 ₽
    при тарифе 14 ₽ за контракт на сторону» читается как результат стратегии,
    а является результатом стратегии на конкретном тарифе. Два прогона
    с разными тарифами дают разные деньги при **одном и том же списке сделок**
    (PROTOTYPE.md §6), и различить их по самим сделкам нельзя.
    """

    deals: tuple[Deal, ...] = ()
    plans: tuple[Plan, ...] = ()
    fills: tuple[Fill, ...] = ()
    average: tuple[tuple[datetime, float], ...] = ()
    journal: tuple[JournalEntry, ...] = ()
    summary: Summary = Summary()
    position: Position | None = None
    halted: str = ""
    bars: int = 0
    costs: Costs = Costs()

    @property
    def assumptions(self) -> tuple[Assumption, ...]:
        """Чем этот итог отличается от настоящего счёта. Пустым не бывает.

        Свойство, а не поле, и это единственное, что удерживает оговорку
        рядом с числом. Полем её можно было бы не заполнить, заполнить
        от другого прогона или потерять по дороге в окно; свойство считается
        из этого самого объекта в момент вопроса и разойтись с его деньгами
        не может. Разбор каждой оговорки — в `backtest/assumptions.py`.
        """
        return assumptions(self)

    @property
    def headline(self) -> str:
        """Оговорка рядом с итогом: три строки и счёт остальных.

        Показывается везде, где показана прибыль этого прогона. Полный
        список — `assumptions`, он длиннее и место ему в выгрузке.
        """
        return headline(self)


# ---------------------------------------------------------------------------
# Порт «источник свечей»
# ---------------------------------------------------------------------------

class HistorySource:
    """Свечи из готового списка. Движок не знает, что список из базы.

    ⚠️ `breathe_every` — не оптимизация. Прогон живёт в цикле событий окна;
    без явной передачи управления окно не перерисуется и не ответит на мышь
    до последней свечи. Двадцать тысяч свечей — это заметно.
    """

    def __init__(
        self,
        candles: Sequence[object],
        *,
        on_progress: Callable[[int, int], None] | None = None,
        breathe_every: int = BREATHE_EVERY,
    ) -> None:
        if breathe_every <= 0:
            raise ValueError(
                f"передышка через {breathe_every} свечей невозможна: "
                "число должно быть положительным"
            )
        self._candles = list(candles)
        self._on_progress = on_progress
        self._breathe_every = breathe_every

    async def candles(self) -> AsyncIterator[object]:
        total = len(self._candles)
        for number, candle in enumerate(self._candles, start=1):
            yield candle
            if number % self._breathe_every == 0:
                self._report(number, total)
                # Передышка: управление возвращается в цикл событий Qt, окно
                # успевает перерисоваться. `sleep(0)` у qasync — это таймер Qt
                # на 0 мс, то есть один оборот цикла событий.
                await asyncio.sleep(0)
        self._report(total, total)

    def _report(self, done: int, total: int) -> None:
        if self._on_progress is not None:
            self._on_progress(done, total)


# ---------------------------------------------------------------------------
# Порт «исполнитель заявок»
# ---------------------------------------------------------------------------

class HistoryExecutor(ExecutionModel):
    """Модель исполнения прототипа плюс запись того, что из неё получилось.

    Правила исполнения — все до одного — в `backtest.execution.ExecutionModel`
    и только там. Здесь к ним добавлены две вещи, нужные отчёту и никак
    не влияющие на сделки: закрытая сделка с деньгами (`deals`) и расчётная
    цена выхода по сработавшему уровню (`level_plans`).

    Отказов в прогоне по истории не выдаётся ни одного: отказ брокера —
    это боевой путь, и выдумывать его на записанных свечах значит проверять
    выдуманное. Выразить их модель тем не менее умеет — иначе ветка обработки
    отказа существовала бы только в бою (решение 0008, пункт 3).

    ⚠️ `commission_per_side` оставлен отдельным параметром намеренно: ровно
    так тариф задан в настройках движка сегодня, и разложить его на биржевой
    сбор и тариф брокера (ТЗ §4.4 Е) без карточки инструмента нельзя. Кто
    разбивку знает — передаёт `costs=Costs(tariff=Tariff(...))`.

    ⚠️ **Названные вместе, они обязаны совпасть**, иначе вызов отвергается
    обеими цифрами. До 03.09.2026 второй параметр молча затирал комиссию
    внутри издержек: `HistoryExecutor(costs=Costs(commission_per_side=14.0,
    price_step=5.0, slippage_steps=1.0), commission_per_side=1.0)` проходил
    и считал отчёт по 1 ₽. Сторож внутри `Costs` этого не ловил — он сравнивает
    разбивку с общей цифрой, а здесь разбивки не было вовсе. На эталонных
    127 сделках такой отчёт показывает комиссию 254 ₽ вместо 3 556 ₽
    и «чистую прибыль» 6 225 ₽ вместо 2 923 ₽ — при непустой колонке, то есть
    неотличимо от правды. Это вторая дверь к той же ошибке, что закрыта
    в `replay` (`_one_commission`), и через публичный вход она обходила его
    параметром.

    Незаполненную половину дозаполнить по-прежнему можно: `Costs`,
    называющий только проскальзывание, плюс комиссия отдельным числом — это
    одна цифра, а не две.
    """

    def __init__(
        self,
        *,
        costs: Costs | None = None,
        commission_per_side: float | None = None,
        ruble_per_point: float = 1.0,
        refuse: dict[str, Refusal] | None = None,
        hollow_cancel: bool = False,
    ) -> None:
        if costs is None:
            costs = Costs(commission_per_side=commission_per_side)
        elif commissions_disagree(costs.per_side, commission_per_side):
            raise ValueError(
                "комиссия названа дважды и по-разному в одном вызове: "
                f"издержки дают {_tariff(costs.per_side)}, отдельным "
                f"параметром указано {_tariff(commission_per_side)}. Молча "
                "побеждала бы вторая цифра, и отчёт посчитал бы комиссию "
                "и чистую прибыль по ней — при непустой колонке, то есть "
                "неотличимо от правды. Задайте комиссию одним способом: "
                "либо внутри Costs, либо этим параметром"
            )
        elif commission_per_side is not None and costs.per_side is None:
            # Издержки назвали только проскальзывание — комиссия дозаполняется.
            # Это одна цифра, а не две: затирать здесь нечего.
            costs = replace(costs, commission_per_side=commission_per_side)
        super().__init__(costs=costs, refuse=refuse, hollow_cancel=hollow_cancel)
        self.ruble_per_point = ruble_per_point
        self.deals: list[Deal] = []
        #: Расчётная цена выхода по сработавшему уровню. Записывается здесь,
        #: потому что заявка на вооружение рождается из сделки открытия
        #: и до вызывающего не доходит: он видит только заявки разбора свечи.
        self.level_plans: list[Plan] = []

    @property
    def commission_per_side(self) -> float | None:
        """Комиссия одной стороны за контракт. `None` — тариф не задан, а не ноль."""
        return self.costs.per_side

    # -- запись результата --------------------------------------------------

    def _record(
        self,
        leg: OpenLeg,
        price: float,
        at: datetime,
        order: OrderRequest,
        order_id: str,
    ) -> None:
        """Оформить закрытую сделку: обе ноги, причина выхода и комиссия.

        Причина берётся из **заявки**, по которой прошёл выход: у рыночного
        выхода она своя, у сработавшего уровня это заявка вооружения, и другой
        причины у неё быть не может. Сравнением цены сделки с уровнем причина
        не выводится ни здесь, ни где-либо ещё (решение 0008, пункт 4).

        Комиссия сделки — **обе стороны**: вход и выход. `None` не превращается
        в ноль ни на одном шаге.
        """
        reason = (
            ExitReason.TAKE_PROFIT
            if order.action is OrderAction.ARM_TAKE_PROFIT
            else order.exit_reason or ExitReason.SIGNAL
        )
        one_side = self.costs.commission(leg.volume)
        self.deals.append(Deal(
            side=leg.side, volume=leg.volume,
            entry_time=leg.at, entry_price=leg.price, entry_order_id=leg.order_id,
            exit_time=at, exit_price=price, exit_order_id=order_id,
            exit_reason=reason,
            commission=None if one_side is None else one_side * 2,
            ruble_per_point=self.ruble_per_point,
        ))

    def _level_hit(
        self, order: OrderRequest, level: float, candle: MarketCandle
    ) -> None:
        """Уровень задет — записать, где робот собирался выйти.

        ⚠️ Время расчёта — **закрытие** бара, а не его начало. У расчёта время
        всегда в этом соглашении (заявку движок подаёт на закрытии свечи),
        а у сделки — в своём: у неё `at` это момент внутри бара. Смешать два
        соглашения — значит показать метку на соседней свече.

        Цена расчёта — сам **уровень**, а не цена сделки: расчёт и есть то,
        на что робот рассчитывал, и разницу между ним и исполнением показывает
        наложение слоёв (DOMAIN.md §8). При заданном проскальзывании эти два
        числа расходятся — ровно на его величину, и это видно в отчёте.
        """
        self.level_plans.append(Plan(
            order_id=order.order_id, decided_at=close_time(candle), price=level,
            side=order.side, action=OrderAction.CLOSE,
            exit_reason=ExitReason.TAKE_PROFIT,
        ))


# ---------------------------------------------------------------------------
# Итог за период
# ---------------------------------------------------------------------------

def reversal_entries(deals: Sequence[Deal]) -> frozenset[str]:
    """Имена заявок на вход, которыми позиция **перевернулась**.

    Определение одно на всю программу — и на число в отчёте, и на метку
    на графике: **вход в противоположную сторону следом за выходом
    по обратному сигналу**. Две разные проверки в двух местах разошлись бы
    молча, и в отчёте было бы одно число, а на графике другое.

    Выход по концу окна и выход по тейку переворотом не считаются: после них
    робот выходит из рынка, а не меняет сторону.
    """
    names: set[str] = set()
    for previous, following in zip(deals, deals[1:]):
        if previous.exit_reason is ExitReason.SIGNAL and following.side is not previous.side:
            names.add(following.entry_order_id)
    return frozenset(names)


@dataclass(frozen=True, slots=True)
class Pair:
    """Пара «прогноз — факт» по одной заявке (DOMAIN.md §8).

    `favour_points` — насколько исполнение оказалось **в пользу позиции**
    по сравнению с расчётом. Знак считается по смыслу операции, а не по
    вычитанию цен: покупке выгодна цена пониже, продаже — повыше.

    ======================  ================================================
    Операция                Лучше расчёта, когда
    ======================  ================================================
    вход в лонг (покупка)   исполнили дешевле
    вход в шорт (продажа)   исполнили дороже
    выход из лонга (продажа) исполнили дороже
    выход из шорта (покупка) исполнили дешевле
    ======================  ================================================

    `plan is None` — сделка, которой расчёт не предполагал: разбирается как
    дефект. `fill is None` — расчёт был, сделки не случилось: в журнале
    обязана быть причина.
    """

    order_id: str
    plan: Plan | None = None
    fill: Fill | None = None
    favour_points: float | None = None
    favour_rubles: float | None = None


def pairs(run: "HistoryRun", *, ruble_per_point: float = 1.0) -> tuple[Pair, ...]:
    """Свести расчёт и исполнение по имени заявки. Догадок нет ни одной."""
    by_plan = {plan.order_id: plan for plan in run.plans}
    by_fill = {fill.order_id: fill for fill in run.fills}
    result: list[Pair] = []
    for order_id in list(by_plan) + [name for name in by_fill if name not in by_plan]:
        plan, fill = by_plan.get(order_id), by_fill.get(order_id)
        points = rubles = None
        if plan is not None and fill is not None:
            # Знак — по смыслу операции, и таблица «покупка или продажа»
            # в проекте одна (`backtest.execution.selling`): вторая копия
            # разошлась бы знаком, а знак здесь означает «в пользу позиции»
            # или «против неё».
            sold = selling(plan.action, plan.side)
            points = (fill.price - plan.price) if sold else (plan.price - fill.price)
            rubles = points * fill.volume * ruble_per_point
        result.append(Pair(order_id, plan, fill, points, rubles))
    result.sort(key=_pair_moment)
    return tuple(result)


def _pair_moment(pair: Pair) -> datetime:
    """Когда пару показывать: по сделке, а если её не было — по расчёту."""
    if pair.fill is not None:
        return pair.fill.at
    assert pair.plan is not None, "пара без расчёта и без сделки не строится"
    return pair.plan.decided_at


def _result(deal: Deal, *, net_basis: bool) -> float:
    """Результат одной сделки на той денежной базе, которую задал вызывающий.

    `net_basis` — не догадка, а следствие уже сделанной проверки «тариф
    известен у всех сделок выборки». Пустой `Deal.net` при `net_basis=True`
    поэтому невозможен; возврат валовой на этот случай — страховка от чужой
    правки, а не подстановка нуля вместо неизвестной комиссии.
    """
    if not net_basis:
        return deal.gross
    return deal.gross if deal.net is None else deal.net


def _daily_curve(
    moments: Sequence[datetime], results: Sequence[float]
) -> tuple[DayResult, ...]:
    """Кривая доходности по торговым дням: результат дня и накопленный итог.

    Дни идут по возрастанию даты, а не в порядке списка сделок: накопленная
    сумма, посчитанная по неотсортированному списку, кривой не является.
    """
    per_day: dict[date, list[float]] = {}
    for moment, value in zip(moments, results, strict=True):
        per_day.setdefault(moment.date(), []).append(value)

    curve: list[DayResult] = []
    cumulative = 0.0
    for day in sorted(per_day):
        values = per_day[day]
        profit = sum(values)
        cumulative += profit
        curve.append(DayResult(
            day=day, trades=len(values), profit=profit, cumulative=cumulative,
        ))
    return tuple(curve)


def _bucketed(keys: Sequence[int], results: Sequence[float]) -> tuple[Bucket, ...]:
    """Разбивка по целому ключу — дню недели или часу. Пустых строк нет."""
    per_key: dict[int, list[float]] = {}
    for key, value in zip(keys, results, strict=True):
        per_key.setdefault(key, []).append(value)
    return tuple(
        Bucket(
            key=key,
            trades=len(values),
            profitable=sum(1 for value in values if value > 0),
            profit=sum(values),
        )
        for key, values in sorted(per_key.items())
    )


def deal_results(deals: Sequence[Deal]) -> tuple[float, ...]:
    """Результат каждой сделки на **одной** денежной базе, в порядке списка.

    База выбирается ровно так же, как в `summarise`: чистая, если тариф
    известен у всех сделок выборки, и валовая, если хоть у одной его нет.
    Смешивать базы внутри одной выборки нельзя (докстринг `Summary`).

    Функция нужна тем, кто считает по сделкам не деньги, а форму распределения
    — разброс, асимметрию, эксцесс (`backtest.overfitting.Shape`). Без неё
    правило выбора базы пришлось бы повторить на стороне вызывающего,
    и разойтись эти два правила разошлись бы молча: в отчёте одна прибыль,
    в поправке на подгонку — другая.
    """
    known = [deal.commission for deal in deals if deal.commission is not None]
    net_basis = bool(deals) and len(known) == len(deals)
    return tuple(_result(deal, net_basis=net_basis) for deal in deals)


def summarise(deals: Sequence[Deal]) -> Summary:
    """Показатели по списку сделок. Определения — в докстринге `Summary`.

    Пустой список даёт пустой итог, а не нули: у `Summary()` везде, где число
    без сделок смысла не имеет, стоит `None`. Ноль в отчёте читается как
    результат, а «сделок не было» — не результат.

    Время входа приводится к МСК явно, через `engine.in_moscow`. Наивный
    момент (без часового пояса) отвергается им с исключением, и это намеренно:
    сдвиг на три часа не роняет расчёт, он молча раскладывает сделки по другим
    часам — и разбивка по часам показывает не то время, которое подписано.
    """
    if not deals:
        return Summary()

    gross = sum(deal.gross for deal in deals)
    known = [deal.commission for deal in deals if deal.commission is not None]
    commission = sum(known) if len(known) == len(deals) else None
    net = None if commission is None else gross - commission

    # Всё, что ниже, — по тем деньгам, которые мы действительно знаем.
    results = [_result(deal, net_basis=commission is not None) for deal in deals]

    running = peak = 0.0
    drawdown = 0.0
    for value in results:
        running += value
        peak = max(peak, running)
        drawdown = max(drawdown, peak - running)

    profitable = sum(1 for value in results if value > 0)
    won = sum(value for value in results if value > 0)
    lost = -sum(value for value in results if value < 0)
    entries = [in_moscow(deal.entry_time) for deal in deals]
    return Summary(
        trades=len(deals),
        profitable=profitable,
        profitable_share=profitable / len(deals),
        gross_profit=gross,
        commission=commission,
        net_profit=net,
        max_drawdown=drawdown,
        reversals=len(reversal_entries(deals)),
        # Ноль убытков — «делить не на что», а не «бесконечно хорошо».
        profit_factor=won / lost if lost > 0 else None,
        average_trade=sum(results) / len(results),
        best_trade=max(results),
        worst_trade=min(results),
        by_day=_daily_curve(entries, results),
        by_weekday=_bucketed([moment.weekday() for moment in entries], results),
        by_hour=_bucketed([moment.hour for moment in entries], results),
    )


# ---------------------------------------------------------------------------
# Сам прогон
# ---------------------------------------------------------------------------

def _one_commission(costs: Costs, settings: EngineSettings) -> None:
    """Комиссия в одном прогоне одна. Две разные — отказ с обеими цифрами.

    ⚠️ **Это не сверка ради порядка, а сторож против двух цифр комиссии
    в одном отчёте.** Число живёт в двух местах, и каждое отвечает за своё:

    * `settings.commission_per_side` читает **движок** — по нему он решает,
      окупает ли цель тейка комиссию обеих сторон (ТЗ §4.4 В), и пишет
      об этом строку журнала;
    * `costs.per_side` читает **отчёт** — по нему считаются комиссия
      и чистая прибыль.

    Расходятся они молча и по-разному в обе стороны. Тариф задан только
    отчёту — журнал на каждой позиции пишет «окупает ли цель комиссию —
    НЕ ПРОВЕРЕНО, тариф движку не задан», а итог того же прогона называет
    комиссию и чистую прибыль числами. Тариф задан только движку — наоборот:
    журнал уверенно судит окупаемость, а отчёт показывает валовую базу.
    Разные числа в обоих — движок судит по 100 ₽ за сделку, отчёт считает
    по 28 ₽; сделки при этом одни и те же, и расхождение не видно нигде.

    Тот же запрет стоит внутри `Costs` (`Costs.__post_init__`) и на входе
    в `HistoryExecutor`; через границу прогона он обязан работать так же,
    иначе обходится параметром.

    ⚠️ Здесь он **строже**, чем `backtest.execution.commissions_disagree`:
    там `None` значит «не названа» и дозаполняется, а тут `None` у движка
    против числа у отчёта — уже расхождение. Причина в том, что цифры
    отвечают за разное: «не названа» у движка означает «окупаемость цели
    не проверена», и рядом с посчитанной комиссией в отчёте это неправда.
    """
    engine_side = settings.commission_per_side
    report_side = costs.per_side
    same = (
        engine_side is None
        if report_side is None
        else engine_side is not None and math.isclose(engine_side, report_side)
    )
    if same:
        return
    raise ValueError(
        "две разные цифры комиссии в одном прогоне: у движка "
        f"{_tariff(engine_side)}, у издержек отчёта {_tariff(report_side)}. "
        "Движок по своей цифре судит, окупает ли тейк комиссию обеих сторон, "
        "а отчёт по своей считает чистую прибыль: сделки выйдут одни и те же, "
        "а деньги и предупреждения — разные, и понять по отчёту, которая "
        "из двух цифр настоящая, будет нельзя. Задайте тариф в обоих местах "
        "или ни в одном"
    )


def _tariff(value: float | None) -> str:
    """Тариф словами. `None` — «тарифа нет», а не ноль (DOMAIN.md §5)."""
    return "тарифа нет" if value is None else f"{value} ₽ за контракт на сторону"


async def replay(  # noqa: PLR0913 — шестой параметр это издержки, и убрать их некуда: настройки движка проскальзывания не знают, а живость окна задают два оставшихся
    candles: Sequence[object],
    strategy: Strategy,
    settings: EngineSettings,
    *,
    costs: Costs | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    breathe_every: int = BREATHE_EVERY,
) -> HistoryRun:
    """Прогнать движок по готовому ряду свечей и собрать результат.

    Цикл написан здесь, а не взят из `Engine.run`, ровно по одной причине:
    после каждой свечи нужны две вещи, которых `run` наружу не отдаёт, —
    заявки этой свечи (слой «прогноз») и значение средней у торгового модуля
    (линия на графике). Ни то ни другое не пересчитывается: заявки берутся
    из разбора движка, средняя — из самого модуля.

    `costs` — издержки исполнения. Не заданы — берутся из настроек движка,
    то есть тариф одним числом за контракт на сторону и **нулевое
    проскальзывание**. Ровно так считал прототип, и только на этих издержках
    сверка с ним сходится по построению: проскальзывание больше нуля меняет
    цену каждой сделки и, значит, все деньги прогона.

    ⚠️ Издержки на **список** сделок не влияют — ни комиссия, ни
    проскальзывание в решения движка не входят (PROTOTYPE.md §6). Поэтому два
    прогона с разными издержками дают одни и те же сделки и разные деньги,
    и различить их можно только по `HistoryRun.costs`.

    ⚠️ **Комиссия отчёта и комиссия движка обязаны совпадать**, иначе прогон
    отвергается вслух (`_one_commission`). Проскальзывание — величина только
    отчёта, у движка её нет вовсе, и сверять её не с чем.

    :raises ValueError: издержки называют одну комиссию, настройки движка —
        другую.
    """
    entries: list[JournalEntry] = []
    engine = Engine(strategy, settings, journal=entries.append)
    if costs is None:
        costs = Costs(commission_per_side=settings.commission_per_side)
    else:
        _one_commission(costs, settings)
    executor = HistoryExecutor(
        costs=costs,
        ruble_per_point=settings.ruble_per_point,
    )
    source = HistorySource(
        candles, on_progress=on_progress, breathe_every=breathe_every
    )

    plans: list[Plan] = []
    average: list[tuple[datetime, float]] = []
    seen = 0

    async for candle in source.candles():
        seen += 1
        outcome = await engine.on_market_candle(candle, executor)
        for order in outcome.orders:
            if not order.action.is_market:
                continue
            plans.append(Plan(
                order_id=order.order_id,
                decided_at=order.submitted_at,
                # Цена, по которой робот считал: закрытие свечи решения.
                price=candle.close,  # type: ignore[attr-defined]
                side=order.side,
                action=order.action,
                exit_reason=order.exit_reason,
            ))
        # Средняя берётся у торгового модуля, а не считается заново: иначе
        # на экране одна линия, а в решениях робота другая (ui/models.py).
        value = getattr(strategy, "average", None)
        if value is not None and not outcome.skipped:
            average.append((close_time(candle), float(value)))
        if engine.halted:
            break

    # Расчёт по рыночным заявкам и расчёт по сработавшим уровням — один слой:
    # для владельца счёта это одинаково «где робот собирался выйти».
    plans.extend(executor.level_plans)
    plans.sort(key=lambda plan: plan.decided_at)

    return HistoryRun(
        deals=tuple(executor.deals),
        plans=tuple(plans),
        fills=tuple(executor.fills),
        average=tuple(average),
        journal=tuple(entries),
        summary=summarise(executor.deals),
        position=engine.position,
        halted=engine.halted,
        bars=seen,
        costs=costs,
    )
