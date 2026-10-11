"""Строки прогона симуляции: заявки и отметка «строки записаны».

Зачем это отдельно от `market.journal`
--------------------------------------
Журнал решений и журнал сделок пишет любой прогон. Заявки и отметка — только
прогон, который **нельзя повторить** (`RunOrigin.evidence`): живой ход
на боевом потоке видел рынок, которого в базе может не остаться, и его
сделки после перезапуска взять больше негде. Прогон по истории повторяется
командой, и его строки не пишутся вовсе — на этом стоит `app.runs.RUNS_KEPT`.

Почему заявки, а не только сделки
---------------------------------
Сделка говорит «вошёл тогда-то, вышел тогда-то». Разбор сделки отвечает
на другие вопросы: **почему** вошёл, на каком уровне стоял тейк, когда
робот решил выйти. Ответы живут в заявках: причина — у заявки входа,
уровень — у вооружения тейка и у касания, решение выхода — у заявки выхода
либо у касания. Время решения у заявки своё и с временем сделки
не совпадает: решение принимается на закрытии бара, исполнение — на открытии
следующего, а через ночь между ними часы.

Вживую ли решено — **фаза у строки заявки**, а не граница во времени
------------------------------------------------------------------
Каждая заявка несёт фазу бара, на разборе которого родилась
(`DecisionPhase`): пересчёт истории при сборке хода, догон пропущенных
свечей после подключения или живой бар. Фазу ставит ход (`app/observe.py`)
по тому, **как бар пришёл**, — ни часы машины, ни время бара в ней
не участвуют.

Границей во времени это сделать нельзя, и это проверено находкой ревью,
а не рассуждением: граница «конец первого прохода» объявляла живым всё
утро понедельника, разобранное одной пачкой догрузки после выходных,
а пустой первый проход (все бары неполные) — весь последующий пересчёт.
Пачка догрузки и живой бар лежат по одну сторону любой границы во времени;
различает их только то, что бар пачки — не последний в ней.

Отметки нет — строк этого прогона нет вовсе: так у прогонов, сделанных
до сборки, которая их пишет. «Ноль сделок» и «сделки не записывались» —
разные ответы, и различает их только отметка.

Типы здесь, а не в `engine/`, по той же причине, что и в `market.journal`:
слой данных не импортирует ни один слой проекта (ARCHITECTURE.md §2),
перекладку делает `app/convert.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from market.journal import (
    DecisionRecord,
    JournalSession,
    RunOrigin,
    TradeRecord,
    TradeSide,
)

__all__ = [
    "DecisionPhase",
    "OrderKind",
    "OrderRecord",
    "PaperRows",
    "PaperRun",
    "PaperWritten",
    "RunRowsMark",
    "StoredOrder",
]


class OrderKind(str, Enum):
    """Что случилось с заявкой: подана движком или задет её уровень.

    Касание уровня — **решение выхода**, а не исполнение: время у строки —
    закрытие бара, на котором уровень задет, цена — сам уровень. Сделка
    по нему исполняется отдельно и несёт своё время и свою цену.
    """

    SUBMITTED = "submitted"
    LEVEL_HIT = "level_hit"


class DecisionPhase(str, Enum):
    """На каком разборе родилась строка заявки: пересчёт, догон или живой бар.

    * `RECOUNT` — бары первого прохода хода, давшего движку хоть один бар
      и дошедшего до конца: пересчёт истории при сборке хода;
    * `CATCH_UP` — в любом следующем проходе все бары пачки, кроме
      последнего: свечи, пропущенные, пока программа не была подключена;
    * `LIVE` — последний бар пачки следующего прохода: решение принято,
      как только бар закрылся.

    Умолчание — `RECOUNT`, безопасная сторона: строка, о которой не сказано,
    что она живая, в «как было» не попадает. Оно же у строк отладочных сборок,
    писавших заявки без фазы (`market/storage.py`, колонка `phase`).
    """

    RECOUNT = "recount"
    CATCH_UP = "catch_up"
    LIVE = "live"


@dataclass(frozen=True, slots=True)
class OrderRecord:
    """Заявка прогона — то, что подаётся на запись.

    `action` — действие заявки машинным значением движка (`open`, `close`,
    `arm_take_profit`, `cancel_take_profit`). Слой данных их не перечисляет:
    перечисление принадлежит `engine/`, и список здесь разошёлся бы с ним
    молча. Поэтому колонка текстовая и проходит чистку, как любой текст.

    `exit_reason` — тоже машинное значение (`take_profit`, `trailing_take`),
    а не подпись: подпись меняется вместе с окном, значение — нет.

    Числа необязательны: `None` — у заявки этого числа нет, а не ноль.
    `close_price` и `average` — закрытие и средняя бара, на разборе которого
    заявка родилась; `level` — уровень тейка, неподвижного или скользящего;
    `limit_price` — предел выхода по времени.

    `phase` — на каком разборе родилась строка (`DecisionPhase`).
    """

    order_id: str
    at: datetime
    kind: OrderKind
    action: str
    side: TradeSide
    volume: float
    reason: str = ""
    exit_reason: str = ""
    target_id: str = ""
    close_price: float | None = None
    average: float | None = None
    level: float | None = None
    limit_price: float | None = None
    phase: DecisionPhase = DecisionPhase.RECOUNT


@dataclass(frozen=True, slots=True)
class StoredOrder:
    """Заявка, прочитанная из базы. `origin` обязателен (решение 0011 п. 4)."""

    id: int
    session_id: int
    origin: RunOrigin
    order_id: str
    at: datetime
    kind: OrderKind
    action: str
    side: TradeSide
    volume: float
    reason: str
    exit_reason: str
    target_id: str
    close_price: float | None
    average: float | None
    level: float | None
    limit_price: float | None
    phase: DecisionPhase

    @property
    def record(self) -> OrderRecord:
        """Та же заявка без признаков хранения — для сравнения с поданной."""
        return OrderRecord(
            order_id=self.order_id,
            at=self.at,
            kind=self.kind,
            action=self.action,
            side=self.side,
            volume=self.volume,
            reason=self.reason,
            exit_reason=self.exit_reason,
            target_id=self.target_id,
            close_price=self.close_price,
            average=self.average,
            level=self.level,
            limit_price=self.limit_price,
            phase=self.phase,
        )


@dataclass(frozen=True, slots=True)
class RunRowsMark:
    """Отметка «строки прогона записаны» и границы первого прохода хода.

    `kept_since` — начало первого бара, разобранного первым проходом:
    с него хранится всё, что прогон пересчитал. `live_from` — закрытие
    последнего бара того же прохода. Оба `None` — первому проходу движку
    не дали ни одного бара.

    ⚠️ `live_from` — сведение о первом проходе, а **не граница живого**:
    вживую ли решена заявка, говорит её фаза (`DecisionPhase`). Колонка
    оставлена, а не снята: таблица уже стоит в базах, снятие колонки —
    переезд таблицы ради поля, которое никому не мешает.
    """

    kept_since: datetime | None
    live_from: datetime | None


@dataclass(frozen=True, slots=True)
class PaperRows:
    """Пачка строк прогона. Пишется одной транзакцией или не пишется вовсе.

    `mark` подаётся с первой пачкой и больше никогда: отметка одна
    на прогон. Строки без отметки в базе отвергаются — иначе прогон
    со строками читался бы как «сделки не записывались».
    """

    mark: RunRowsMark | None = None
    decisions: tuple[DecisionRecord, ...] = ()
    orders: tuple[OrderRecord, ...] = ()
    trades: tuple[TradeRecord, ...] = ()

    @property
    def empty(self) -> bool:
        """Писать нечего: ни отметки, ни строк."""
        return self.mark is None and not (self.decisions or self.orders or self.trades)


@dataclass(frozen=True, slots=True)
class PaperWritten:
    """Сколько легло из пачки. Отсеянное названо отдельно, а не спрятано.

    `repeated_trades` — сделки пачки, уже лежавшие в журнале: второй строкой
    они не пишутся (`CandleStore.write_trades`). Не ноль означает, что пачку
    или её часть подали повторно.
    """

    decisions: int = 0
    orders: int = 0
    trades: int = 0
    repeated_trades: int = 0
    marked: bool = False

    @property
    def rows(self) -> int:
        """Сколько строк записано всего, без отметки."""
        return self.decisions + self.orders + self.trades


@dataclass(frozen=True, slots=True)
class PaperRun:
    """Прогон симуляции вместе с отметкой о строках.

    `mark is None` — строк этого прогона в базе нет: прогон сделан сборкой,
    которая их не писала, или отказала запись первой пачки. Отличать это
    от «прогон без сделок» обязан читающий: окно называет такой прогон
    словами, а не нулём.

    `last_at` — наибольшее время его строк (решений и заявок). Нужно прогону
    без времени конца: программу закрыли аварийно, и до какого момента он
    шёл, говорят только его строки. `None` — строк нет.
    """

    session: JournalSession
    mark: RunRowsMark | None
    last_at: datetime | None
