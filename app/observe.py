"""Наблюдение: движок работает на живом ряду и показывает решения. Заявок нет.

Что это и чем отличается от того, что было
------------------------------------------
До Э3 движок **переигрывал базу**: живая минута записана — и по всей истории
заново гонялся `backtest.replay`. Состояние движка выбрасывалось на каждом баре
и выводилось заново. Здесь ход **один и непрерывный**: движок создаётся один
раз, получает прогрев из базы, а дальше — живые бары тем же рядом, по одному,
по мере закрытия.

Разница не оформительская. Непрерывный ход — единственное, на чём видно, ведёт
ли себя стратегия в бою так же, как на истории: у него есть состояние, которое
**переживает** бар, — заявка в полёте, вооружённый уровень, остановка робота.
Переигрывание базы всё это выводило заново и потому не могло разойтись
с историей никогда, то есть не проверяло ничего.

Три части модуля
----------------
* `LiveSource` — порт «источник свечей» поверх ряда, который растёт.
  Прогрев и живое идут **одной** лентой, стык стережётся здесь;
* `ShadowExecutor` — порт «исполнитель заявок», который **никуда не ходит**:
  та же модель исполнения, что в прогоне по истории, плюс счёт поданного;
* `LiveObserver` — склейка: движок, ход по ленте и снимок результата
  в том же виде (`HistoryRun`), в каком его показывает окно.

⚠️ Заявки к брокеру отсюда не уходят, и это доказано устройством
------------------------------------------------------------------
Не проверкой «а сейчас можно?», а тем, что **брокера здесь нет**. Модуль
не импортирует `broker/` ни одним именем, исполнитель — потомок модели
исполнения на истории, и другого исполнителя живому ходу передать нельзя:
он создаётся внутри. Путь к боевому размещению из этого модуля не проложен
никакой, и сторожит это тест по импортам (`tests/test_app_observe.py`).

Отсюда же следует, что видит владелец счёта: **где был бы вход, где выход,
почему пропустил** — сделки считает та же модель, что в прогоне по истории,
с той же комиссией и тем же правилом «заявка исполняется по открытию
следующей свечи».

Почему бары приходят снаружи, а не читаются здесь
-------------------------------------------------
`LiveSource.offer` принимает **готовый** список баров. Читает базу и решает,
каким барам можно верить как закрытым, порт (`HistoryPort._candles`
и `_trusted`) — и он же рисует их на графике. Если бы источник читал базу сам,
у вопроса «какие бары видит движок» появилось бы два ответа, расходящихся
молча: на графике один ряд, в решениях другой. Ровно от этого написан
`ARCHITECTURE.md` §1.

Цена решения названа вслух: бар, который порт отбросил как неполный, движку
не показывается, и если позже он станет доверенным (догрузка подтвердила
полноту), робот уже ушёл вперёд. Такая свеча в среднюю не войдёт. Молчанием
это не станет — `Seam.late` называет её временем и уровнем «предупреждение».
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from backtest import (
    BREATHE_EVERY,
    Costs,
    HistoryExecutor,
    HistoryRun,
    Plan,
    summarise,
)
from engine import (
    Engine,
    EngineSettings,
    JournalEntry,
    OrderRequest,
    close_time,
    in_moscow,
)
from market import DEFAULT_WARMUP_BARS
from market import Candle as MarketCandle
from strategies import Strategy
from ui.models import DecisionLevel

__all__ = [
    "RECHECK_BARS",
    "LiveObserver",
    "LiveSource",
    "Seam",
    "ShadowExecutor",
]

log = logging.getLogger(__name__)

#: Сколько последних отданных баров помнить, чтобы заметить, что бар изменился
#: задним числом. Шестнадцать пятиминуток — это 80 минут: столько закрывает
#: догрузка после обрыва средней длины (`app/backfill.py`). Глубже помнить
#: незачем — не потому, что там ничего не меняется, а потому, что запоминать
#: пришлось бы весь ряд; чего это не закрывает, сказано в миниплане Э3 §4.
RECHECK_BARS = 16

#: Сколько ждать, пока ход дойдёт до конца при закрытии. По истечении задача
#: снимается: висящий ход не должен держать завершение программы. Пять секунд —
#: столько же ждёт закрытия сокета живой поток (`app/live_feed.py::CLOSE_WAIT`),
#: и по той же причине: дольше человек считает программу зависшей.
CLOSE_WAIT = 5.0

#: Сколько баров нужно средней, чтобы её значение перестало зависеть от начала
#: ряда. Число выведено, а не назначено: `market.warmup_bars` — 52 бара для
#: EMA(15). Меньше — решения принимаются по средней, которая ещё помнит,
#: с какой свечи начали.
WARMUP_BARS = DEFAULT_WARMUP_BARS


# --------------------------------------------------------------------- стык


@dataclass(frozen=True, slots=True)
class Seam:
    """Что случилось на стыке: сколько баров взято и что с остальными.

    Значение, а не четыре числа россыпью: отчёт целиком уходит в журнал одной
    разборкой (`LiveObserver._say`), и потерять из него поле, дописав пятое
    место обработки, так нельзя.

    `taken` — баров принято в ленту. `repeated` — уже отданных, пропущено
    молча: повтор ряда — штатное событие, порт отдаёт весь отрезок целиком
    на каждом проходе.

    Остальные три поля — **события для журнала**, а не служебные счётчики:

    `late` — бары, которых робот не видел, а лента ушла дальше. Так бывает,
    когда порт отбросил бар как неполный (`HistoryPort._trusted`), а позже
    догрузка подтвердила его полноту: бар стал доверенным, но робот уже
    решает по следующим. В среднюю он не войдёт никогда.

    `changed` — бары, отданные движку, содержимое которых потом изменилось.
    Решение по такому бару принято по неполным данным.

    `gaps` — пропуски в живой части ленты: между соседними барами больше
    одного таймфрейма. Различить «сделок не было» и «связи не было» по самому
    ряду нельзя, поэтому строка журнала называет обе причины.
    """

    taken: int = 0
    repeated: int = 0
    late: tuple[datetime, ...] = ()
    changed: tuple[datetime, ...] = ()
    gaps: tuple[tuple[datetime, datetime], ...] = ()

    @property
    def quiet(self) -> bool:
        """Ничего, о чём стоит говорить, не случилось."""
        return not (self.late or self.changed or self.gaps)


def _mark(candle: MarketCandle) -> tuple[float, int]:
    """Отпечаток бара: по чему видно, что он изменился задним числом.

    `close` и число собранных минуток. Не весь бар: `open` у бара не меняется
    никогда (первая минутка), а `high`/`low` двигаются вместе с `close`.
    Две величины ловят оба вида изменения — дописанную минутку внутри
    интервала и другую цену последней минутки.
    """
    return (candle.close, candle.filled_minutes)


class LiveSource:
    """Порт «источник свечей» поверх ряда, который растёт. Стык стережётся тут.

    Контракт порта (`engine/ports.py`) требует ровно двух вещей, и обе держит
    этот класс:

    * **свечи по возрастанию времени.** Ход времени назад движок не гасит —
      это порча ряда, и торговый модуль останавливает её исключением
      (`engine/bars.py`, шапка). В живом ходе такое исключение убило бы
      наблюдение посреди дня, поэтому бар с временем не больше отданного
      в ленту не попадает вовсе;
    * **без пропусков внутри поданного отрезка.** Пропуски здесь возможны
      и неустранимы (`Seam.late`, `Seam.gaps`), поэтому каждый называется
      вслух, а не проглатывается.

    Повтор бара источник **гасит сам**, хотя мог бы не гасить: движок отбрасывает
    повтор последней свечи (`engine/bars.py::intake`). Полагаться на это нельзя —
    движок сравнивает только с **последней принятой** свечой, а порт отдаёт
    сюда весь отрезок целиком, то есть повторяются сотни баров подряд. Второй
    из них ушёл бы движку как ход назад.

    Незакрытые бары в ленту не берутся (решение 0033). Контракт это разрешает
    («отдавать может»), а живому ходу это даёт то же поведение, что и прогону
    по истории: тейк срабатывает на закрытии бара, в котором цена задела
    уровень, а не по незакрытому бару с временем закрытия в будущем.

    :param breathe_every: через сколько отданных баров лента возвращает
        управление циклу событий. Прогрев — это тысячи баров подряд в одной
        корутине; без передышки окно замирает на всё время прогрева
        (та же причина, что у `backtest.HistorySource`).
    """

    def __init__(self, *, breathe_every: int = BREATHE_EVERY) -> None:
        if breathe_every <= 0:
            raise ValueError(
                f"передышка через {breathe_every} свечей невозможна: "
                "число должно быть положительным"
            )
        self._breathe_every = breathe_every
        self._buffer: deque[MarketCandle] = deque()
        self._last: datetime | None = None
        self._seen: dict[datetime, tuple[float, int]] = {}
        self._order: deque[datetime] = deque()
        #: Сколько баров прошло через ленту всего. Нужен прогреву: он считает
        #: бары, чтобы сказать, хватило ли их средней.
        self.delivered = 0

    @property
    def last(self) -> datetime | None:
        """Начало последнего бара, взятого в ленту. `None` — не было ни одного."""
        return self._last

    def offer(self, candles: Sequence[MarketCandle]) -> Seam:
        """Предложить ленте отрезок ряда. Взято будет только новое.

        Отрезок приходит **целиком**, а не приростом: порт читает базу за весь
        период на каждом проходе, и просить его помнить, что он уже отдавал,
        значило бы завести вторую память о том же — расходящуюся молча.

        :return: что взято и о чём надо сказать в журнал.
        """
        taken: list[MarketCandle] = []
        repeated = 0
        late: list[datetime] = []
        changed: list[datetime] = []
        for candle in candles:
            if candle.unsettled:
                continue
            moment = in_moscow(candle.time)
            if self._last is not None and moment <= self._last:
                known = self._seen.get(moment)
                if known is None:
                    if self._order and moment > self._order[0]:
                        late.append(moment)
                    else:
                        repeated += 1
                elif known != _mark(candle):
                    changed.append(moment)
                    self._seen[moment] = _mark(candle)
                else:
                    repeated += 1
                continue
            taken.append(candle)
            self._last = moment
            self._remember(moment, candle)
        gaps = self._gaps(taken)
        self._buffer.extend(taken)
        return Seam(
            taken=len(taken),
            repeated=repeated,
            late=tuple(late),
            changed=tuple(changed),
            gaps=gaps,
        )

    @property
    def pending(self) -> int:
        """Сколько баров лежит в ленте и ещё не отдано."""
        return len(self._buffer)

    async def candles(self) -> AsyncGenerator[MarketCandle, None]:
        """Свечи по одной — всё, что пришло к этому моменту, и обратно.

        ⚠️ **Лента отдаёт пришедшее и кончается, а не ждёт следующего бара
        внутри себя.** Это отличие от боевого источника, каким его описывает
        `engine/ports.py` («в бою поток не кончается сам»), и оно намеренное:
        ждать здесь означало бы держать разбор в отдельной задаче, а её —
        синхронизировать с окном признаком «всё разобрано».

        Так и было сделано в первой редакции, и цена оказалась не теоретической:
        связка «фоновая задача + `asyncio.Event` + брошенный асинхронный
        генератор» роняла процесс в SIGSEGV — причём не здесь, а в чужом файле
        проверок и с плавающим местом падения. Разбор того же рода — в решении
        0005: несколько циклов событий и `asyncio` поверх Qt в одном процессе.

        Живость от этого не теряется, она **переезжает к вызывающему**: новый
        бар приносит порт (`HistoryPort.live_candle` на закрытии бара), и он же
        зовёт разбор заново. Состояние движка при этом не сбрасывается — он
        создан один раз и живёт между вызовами, а это и есть суть Э3.
        """
        given = 0
        while self._buffer:
            given += 1
            yield self._buffer.popleft()
            if given % self._breathe_every == 0:
                # Передышка: управление возвращается циклу событий, окно
                # успевает перерисоваться. Прогрев — это тысячи баров подряд.
                await asyncio.sleep(0)

    def _remember(self, moment: datetime, candle: MarketCandle) -> None:
        """Отпечаток отданного бара — в память глубиной `RECHECK_BARS`."""
        self._seen[moment] = _mark(candle)
        self._order.append(moment)
        while len(self._order) > RECHECK_BARS:
            self._seen.pop(self._order.popleft(), None)

    def _gaps(
        self, taken: Sequence[MarketCandle]
    ) -> tuple[tuple[datetime, datetime], ...]:
        """Разрывы между соседними взятыми барами — парами «после, до».

        Прогрев здесь не разбирается: в истории разрыв между баром 23:45
        и баром 10:00 следующего дня — это ночь, и говорить о ней нечего.
        Живая часть ленты идёт подряд, поэтому разрыв в ней означает либо
        отсутствие сделок, либо отсутствие связи, и различить их по ряду
        нельзя (`HistoryPort._trusted`).
        """
        if self.delivered == 0:
            # Первый отрезок — прогрев. Его разрывы это ночи и выходные.
            return ()
        found: list[tuple[datetime, datetime]] = []
        previous = self._before(taken)
        for candle in taken:
            if previous is not None:
                step = timedelta(minutes=candle.timeframe.minutes)
                if candle.time - previous > step:
                    found.append((previous, in_moscow(candle.time)))
            previous = in_moscow(candle.time)
        return tuple(found)

    def _before(self, taken: Sequence[MarketCandle]) -> datetime | None:
        """Начало бара, стоявшего в ленте перед этим отрезком."""
        if not taken:
            return None
        first = in_moscow(taken[0].time)
        for moment in reversed(self._order):
            if moment < first:
                return moment
        return None


# --------------------------------------------------------------- исполнитель


class ShadowExecutor(HistoryExecutor):
    """Исполнитель, который **никуда не отправляет заявку**. Тень, а не брокер.

    Модель исполнения — та же самая, что в прогоне по истории
    (`backtest.execution.ExecutionModel`), и это главное свойство, а не
    экономия: расхождение между наблюдением и прогоном по истории обязано
    означать расхождение **данных**, а не двух разных моделей исполнения.
    Своя модель здесь дала бы разницу, которую нельзя было бы отнести ни
    к чему.

    Отсюда и цена, названная вслух: сделки этого исполнителя — **расчётные**.
    Вход считается по открытию следующего бара, тейк — по уровню внутри бара,
    проскальзывание берётся из издержек. Настоящая цена у брокера будет другой,
    и наложение расчёта на факт — это Э4, а не этот этап.

    `submitted` — сколько заявок подано. Поле нужно не работе, а доказательству:
    тест «ни одна заявка не ушла к брокеру» обязан отличать «заявок не было
    вовсе» от «заявки были и остались здесь». Без счётчика зелёным был бы
    и наблюдатель, который вообще не работает.
    """

    def __init__(self, *, costs: Costs | None = None, ruble_per_point: float = 1.0) -> None:
        super().__init__(costs=costs, ruble_per_point=ruble_per_point)
        self.submitted = 0

    async def submit(self, order: OrderRequest) -> None:
        """Принять заявку. Дальше этого объекта она не уходит никуда."""
        self.submitted += 1
        await super().submit(order)


# ---------------------------------------------------------------- наблюдение


@dataclass(slots=True)
class _Tally:
    """Накопленное за ход: то, из чего собирается снимок для окна.

    Поля повторяют локальные переменные `backtest.replay` — намеренно
    и до последней: снимок обязан быть **тем же** `HistoryRun`, иначе окно
    показывало бы живой ход иначе, чем прогон по истории, и сравнить их
    глазами стало бы нельзя. Что эти два пути дают одинаковый результат
    на одном ряде — проверяется тестом, а не обещается.
    """

    plans: list[Plan] = field(default_factory=list)
    average: list[tuple[datetime, float]] = field(default_factory=list)
    journal: list[JournalEntry] = field(default_factory=list)
    bars: int = 0


class LiveObserver:
    """Один непрерывный ход движка по ленте. Показывает решения, не торгует.

    Живёт от включения потока котировок до его выключения либо до смены
    настроек. **Смена настроек пересобирает наблюдателя целиком**, и это
    не лень: движок, которому поменяли период средней посреди ряда, идёт
    дальше по средней, посчитанной наполовину по старому периоду, — такой ход
    не соответствует ни старым настройкам, ни новым, а выглядит как настоящий.
    Пересборка означает новый прогрев из базы и новый ряд с той же границы,
    то есть ровно то, что владелец счёта и ожидает увидеть, меняя настройку.

    :param strategy: торговый модуль, уже настроенный. Наблюдатель его
        не настраивает и не сбрасывает: ряд модуля — часть хода.
    :param settings: настройки движка на весь ход.
    :param say: строка в журнал решений — событие, причина, важность.
    :param costs: издержки исполнения. `None` — тариф из настроек движка
        и нулевое проскальзывание, как в прогоне по истории.
    :param around: чем обернуть ход, чтобы он попал в журнал прогонов.
        `None` — не записывать. Обёртка **одна на весь ход**, а не на проход:
        живой ход это один прогон, а не двенадцать в час.

    Порога прогрева среди доводов нет намеренно: он не настройка, а следствие
    периода средней, и меняется вместе с ним (`WARMUP_BARS`). Довод здесь
    означал бы, что вызывающий вправе назвать своё число, — и первое же
    отличное от `market.warmup_bars` сделало бы предупреждение о коротком
    прогреве бессмысленным.
    """

    def __init__(
        self,
        *,
        strategy: Strategy,
        settings: EngineSettings,
        say: Callable[[str, str, DecisionLevel], None],
        costs: Costs | None = None,
        around: Callable[[], AbstractAsyncContextManager[object]] | None = None,
    ) -> None:
        self._strategy = strategy
        self._settings = settings
        self._say = say
        self._around = around
        self._costs = costs or Costs(commission_per_side=settings.commission_per_side)
        self._tally = _Tally()
        self._engine = Engine(strategy, settings, journal=self._tally.journal.append)
        self._executor = ShadowExecutor(
            costs=self._costs, ruble_per_point=settings.ruble_per_point
        )
        self._source = LiveSource()
        #: Открытая запись в журнале прогонов и то, чем её закрывают. `None` —
        #: записывать не просили либо ход уже закрыт.
        self._record: AbstractAsyncContextManager[object] | None = None
        self._entry: object | None = None
        #: Почему ход прекратился не по воле движка. Пусто — не прекращался.
        self._failure = ""
        self._stopped = False
        self._warmed = False

    # -------------------------------------------------------------- команды

    async def start(self) -> None:
        """Открыть запись о ходе в журнале прогонов. Повторный вызов молчит.

        Отдельно от `feed`, потому что запись открывается **один раз на весь
        ход**, а `feed` зовётся на каждом закрытом баре. Без `around` метод
        не делает ничего и существует ради одинакового порядка сборки.
        """
        if self._around is None or self._record is not None or self._stopped:
            return
        self._record = self._around()
        self._entry = await self._record.__aenter__()

    async def feed(self, candles: Sequence[MarketCandle]) -> HistoryRun:
        """Показать ленте отрезок ряда и разобрать его движком до конца.

        Возврат — снимок хода, в который **уже вошёл** последний бар отрезка.
        Иначе окно рисовало бы бар, по которому решение ещё не принято, и метка
        входа появлялась бы на баре позже свечи — ровно то расхождение, которое
        этап и должен ловить.

        ⚠️ Разбор идёт **здесь**, в вызывающей корутине, а не в фоновой задаче.
        Первая редакция держала задачу и синхронизировала её с окном признаком
        «всё разобрано»; цена оказалась не теоретической — разбор в шапке
        `LiveSource.candles`.

        Ход, уже прекращённый (закрыт либо упал), новых баров не разбирает
        и отдаёт последний снимок: молча продолжить после падения нельзя,
        а притворяться, что бары учтены, — тем более.
        """
        seam = self._source.offer(candles)
        self._say_seam(seam)
        if not self._stopped and not self._failure:
            await self._ride()
        self._say_warmup()
        return self.run

    async def aclose(self) -> None:
        """Прекратить ход и дописать итог в журнал прогонов.

        ⚠️ Запись закрывается **итогом**, а не отменой. Разница видна в базе:
        `RunLog.around` на отмене намеренно не закрывает запись, и прогон без
        времени конца читается как «программу закрыли на середине». Так и должно
        выглядеть аварийное завершение, но **не** выключение потока кнопкой:
        там ход кончился по команде, и его итог обязан быть в базе.
        """
        self._stopped = True
        record, self._record = self._record, None
        if record is None:
            return
        if self._entry is not None:
            _note_result(self._entry, self.run)
        self._entry = None
        try:
            await record.__aexit__(None, None, None)
        except asyncio.CancelledError:
            raise
        except Exception:  # запись важна, но ход и окно важнее (`RunLog`)
            log.exception("запись живого хода не закрыта")

    # -------------------------------------------------------------- чтение

    @property
    def run(self) -> HistoryRun:
        """Снимок хода — ровно в том виде, в каком его показывает окно.

        Собирается заново на каждый вызов, а не копится: `plans` обязаны быть
        отсортированы вместе с уровнями исполнителя, а те появляются по ходу.
        Порядок сборки повторяет `backtest.replay` до строки.
        """
        plans = [*self._tally.plans, *self._executor.level_plans]
        plans.sort(key=lambda plan: plan.decided_at)
        return HistoryRun(
            deals=tuple(self._executor.deals),
            plans=tuple(plans),
            fills=tuple(self._executor.fills),
            average=tuple(self._tally.average),
            journal=tuple(self._tally.journal),
            summary=summarise(self._executor.deals),
            position=self._engine.position,
            halted=self._engine.halted or self._failure,
            bars=self._tally.bars,
            costs=self._costs,
        )

    @property
    def submitted(self) -> int:
        """Сколько заявок подано исполнителю. К брокеру не ушла ни одна."""
        return self._executor.submitted

    # ---------------------------------------------------------- внутреннее

    async def _ride(self) -> None:
        """Разобрать то, что лежит в ленте. Беду говорит вслух и запоминает.

        ⚠️ Отказ **не пробрасывается** вызывающему. Разбор зовёт порт из своего
        прохода, и исключение оттуда стало бы отказом всего прохода — окно
        осталось бы без графика из-за беды в одном лишь роботе. Поэтому беда
        превращается в остановку хода: она видна в снимке (`halted`), сказана
        строкой уровня ERROR и больше баров не разбирает.
        """
        try:
            await self._walk()
        except asyncio.CancelledError:
            raise
        except Exception as error:  # трассировка уходит в технический лог
            log.exception("живой ход движка прекращён ошибкой программы")
            self._failure = (
                f"Живой ход движка прекращён: {type(error).__name__}: {error}. "
                "Решения по новым свечам больше не принимаются. Подробность — "
                "в выводе программы в консоли."
            )
            self._say("Живой ход прекращён", self._failure, DecisionLevel.ERROR)

    async def _walk(self) -> None:
        """Цикл по ленте. Написан здесь по той же причине, что в `replay`.

        `Engine.run` наружу не отдаёт двух вещей, нужных окну: заявок этой
        свечи (слой «прогноз» на графике) и значения средней у торгового
        модуля (линия). Ни то ни другое не пересчитывается — иначе на экране
        одна линия, а в решениях другая.
        """
        stream = self._source.candles()
        try:
            async for candle in stream:
                self._tally.bars += 1
                self._source.delivered += 1
                outcome = await self._engine.on_market_candle(candle, self._executor)
                for order in outcome.orders:
                    if not order.action.is_market:
                        continue
                    self._tally.plans.append(Plan(
                        order_id=order.order_id,
                        decided_at=order.submitted_at,
                        price=candle.close,
                        side=order.side,
                        action=order.action,
                        exit_reason=order.exit_reason,
                    ))
                value = getattr(self._strategy, "average", None)
                if value is not None and not outcome.skipped:
                    self._tally.average.append((close_time(candle), float(value)))
                if self._engine.halted:
                    return
        finally:
            # ⚠️ Лента закрывается **явно**. `async for` брошенный генератор
            # не закрывает: его добирает сборщик мусора, и `aclose` уезжает
            # на тот цикл событий, в котором генератор создан, — а он к тому
            # времени может быть уже закрыт. В программе это «Task was
            # destroyed but it is pending» при завершении, в прогоне тестов —
            # чужая обвязка, падающая на разборке нашей.
            await stream.aclose()

    # ------------------------------------------------------------- рассказ

    def _say_seam(self, seam: Seam) -> None:
        """Три события стыка — в журнал. Молчаливой потери свечи не бывает."""
        if seam.late:
            self._say(
                "Свеча пришла поздно",
                f"{_times(seam.late)} — эти бары стали доверенными после того, "
                "как робот ушёл вперёд по ряду. В среднюю они не войдут: "
                "движок обязан получать свечи по возрастанию времени, иначе "
                "ряд торгового модуля испорчен. Так бывает, когда неполный бар "
                "подтвердила догрузка уже после решения по следующему.",
                DecisionLevel.WARNING,
            )
        if seam.changed:
            self._say(
                "Свеча изменилась задним числом",
                f"{_times(seam.changed)} — бары, по которым робот уже принял "
                "решение, дозаполнились позже (догрузка пропущенных минут). "
                "Решение по ним принято по неполным данным и переигрываться "
                "не будет.",
                DecisionLevel.WARNING,
            )
        for after, before in seam.gaps:
            self._say(
                "Разрыв в ряду свечей",
                f"Между {_moment(after)} и {_moment(before)} баров нет. "
                "Либо в это время не было сделок, либо не было связи — "
                "по самому ряду это неразличимо. Средняя посчитана без них.",
                DecisionLevel.WARNING,
            )

    def _say_warmup(self) -> None:
        """Сказать про прогрев — один раз, после первого отрезка."""
        if self._warmed or self._tally.bars == 0:
            return
        self._warmed = True
        if self._tally.bars >= WARMUP_BARS:
            self._say(
                "Движок работает на живом ряду",
                f"Прогрев: {self._tally.bars} закрытых баров из базы, нужно "
                f"не меньше {WARMUP_BARS}. Дальше решения принимаются по мере "
                "закрытия баров. Заявки к брокеру не подаются: это наблюдение, "
                "сделки в журнале — расчётные.",
                DecisionLevel.INFO,
            )
            return
        self._say(
            "Прогрев короче нужного",
            f"В ряду {self._tally.bars} закрытых баров, средней нужно "
            f"не меньше {WARMUP_BARS}, чтобы её значение перестало зависеть "
            "от начала ряда. Решения принимаются, но по средней, которая ещё "
            "помнит, с какой свечи начали: тот же ряд с более ранней границы "
            "дал бы другой список сделок.",
            DecisionLevel.WARNING,
        )


def _note_result(entry: object, run: HistoryRun) -> None:
    """Итог хода — в открытую запись журнала прогонов.

    Через `setattr`, потому что тип записи принадлежит `app/runs.py`, а брать
    его сюда значило бы связать наблюдение с журналом прогонов навсегда:
    обёртка необязательна, и без неё ход работает целиком.
    """
    from app.runs import result_note  # noqa: PLC0415 — связь только при записи

    entry.note = result_note(run)  # type: ignore[attr-defined]


def _times(moments: Sequence[datetime]) -> str:
    """Времена баров человеку: не больше пяти, дальше — «и ещё N»."""
    shown = ", ".join(_moment(moment) for moment in moments[:5])
    rest = len(moments) - 5
    return f"{shown} и ещё {rest}" if rest > 0 else shown


def _moment(moment: datetime) -> str:
    return f"{in_moscow(moment):%d.%m %H:%M}"
