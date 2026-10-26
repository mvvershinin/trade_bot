"""Лента «Отладка»: что робот делает прямо сейчас — строками, для панели окна.

Просьба владельца счёта 12.10.2026: «сворачиваемое окно — отладка, где будет
логирование всех действий: приём свечи, принятие решения, сигналы, всё, что
имеет значение». Миниплан с поправками гейта — `.docs/plans/debug-panel.md`.

Транспорт — `logging`, второй ленты нет
---------------------------------------
Строки, которых сегодня нет нигде (минута пошла и закрылась, бар и разбор
движка по нему, заявки и исполнения симуляции), пишутся на логгер
`app.panel`. Он **не доходит до корня** (`propagate=False`), поэтому в файл
технического лога не попадает ничего нового: прирост `terminal.log` — ноль
строк в день, и расчёт ротации в шапке `app/logs.py` остаётся верным.

Обработчик ленты (`PanelHandler`) висит и на `app.panel`, и на корне. С корня
в ленту приходит технический лог уровня INFO и выше сам, а из чужих
библиотек (`httpx`, `httpcore`, `websockets`) — только WARNING и выше: их
строки уровня INFO — это каждый HTTP-запрос. Строка, у которой есть близнец
в журнале решений (один отказ — и в лог, и человеку), помечается
`extra={"panel": False}`: иначе один отказ стоял бы в ленте дважды, красным
и жёлтым.

⚠️ `propagate=False` ставится **вместе** с `NullHandler`, при импорте модуля.
Логгер без обработчика и без выхода к корню отправил бы запись в
`logging.lastResort`, то есть в консоль сырым текстом, до чистки секретов.

Граница потоков
---------------
`emit` зовётся из любого потока — цикл событий, поток данных. Он кладёт
готовую строку с номером в `deque(maxlen)` под своим замком. Окно забирает
строки с номерами больше последнего виденного (`since`) по таймеру и только
пока панель открыта. Сигналов на каждую строку нет.

Строка ленты не роняет живой путь
---------------------------------
Каждый построитель строки обёрнут одним приёмом (`_guarded`): исключение
внутри него не уходит в чтение сокета, в разбор бара или в дорисовку графика.
Пойманный отказ не глотается молча (правило 13): первый — одна строка
WARNING в файл и в ленту, следующие только считаются, и счёт виден в шапке
панели вместе с причиной первой потери. Отказ самого обработчика
(`handleError`) только считается: писать о нём в лог изнутри обработчика —
путь к рекурсии. Его причина видна в шапке, а право первого отказа
построителя на строку в журнале он не занимает (`_Losses`).

Токен (правило 7): `emit` прогоняет готовый текст и первую строку исключения
через `broker.redaction.scrub` — ту же функцию, что у файла лога. Второй
рубеж — «Скопировать» в окне, через чистку окна.
"""

from __future__ import annotations

import logging
import sys
import threading
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum
from functools import partial, wraps
from itertools import islice
from typing import TYPE_CHECKING, Final

from app import convert
from broker.redaction import FOREIGN_LOGGERS, scrub
from engine import JournalLevel, OrderAction, Step, close_time
from market import DecisionPhase, OrderKind
from ui.formatting import MSK, fmt_datetime, fmt_level, fmt_money, fmt_time, fmt_volume
from ui.models import DebugBatch, DebugLine, DecisionLevel

if TYPE_CHECKING:
    from backtest import Deal, HistoryRun
    from engine import Fill, JournalEntry, Outcome
    from market import Candle, OrderRecord

__all__ = [
    "BUFFER",
    "LOSSES",
    "PANEL_LOGGER",
    "PanelHandler",
    "install",
    "installed",
    "uninstall",
]

#: Логгер строк, которых нет больше нигде. До корня не доходит.
PANEL_LOGGER: Final[str] = "app.panel"

#: Сколько строк держит лента. Вживую ожидается 2–3 строки в минуту: начало
#: и закрытие минуты, раз в пять минут бар и его решения. Сеанс 830 минут —
#: 2100–2500 строк, то есть в буфер помещаются два торговых дня. Память —
#: не больше ~5 МБ. Число не торговое (правило 16 его не касается).
BUFFER: Final[int] = 5000

#: Откуда строка — словом в колонке ленты. Строки техлога несут имя логгера.
SOURCE_STREAM: Final[str] = "поток"
SOURCE_BASE: Final[str] = "база"
SOURCE_PORT: Final[str] = "порт"
SOURCE_ENGINE: Final[str] = "движок"
SOURCE_ORDERS: Final[str] = "заявки"
SOURCE_JOURNAL: Final[str] = "журнал"

log = logging.getLogger(__name__)

_panel = logging.getLogger(PANEL_LOGGER)
_panel.setLevel(logging.INFO)
if not any(type(handler) is logging.NullHandler for handler in _panel.handlers):
    _panel.addHandler(logging.NullHandler())
_panel.propagate = False


# ------------------------------------------------------------- счёт потерь


class _Losses:
    """Сколько строк ленты не собрано, причина первой — и сказан ли отказ построителя.

    Два «первых» здесь разные и друг друга не занимают. `first` — причина
    первой потери любого рода: её показывает шапка панели рядом с числом.
    Признак `_told` — сказан ли в технический журнал отказ **построителя**
    строки (`_lose`). Отказ самого обработчика (`PanelHandler.handleError`)
    в журнал не пишется (запись оттуда вернулась бы в тот же обработчик)
    и права построителя на строку в журнале не отнимает: иначе одна битая
    запись чужого логгера заглушила бы все отказы построителей до конца
    работы, и в журнале не осталось бы ничего.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.count = 0
        self.first = ""
        self._told = False

    def add(self, why: str, *, aloud: bool = False) -> bool:
        """Учесть потерю. `True` — это первый отказ с `aloud`, и его надо сказать."""
        with self._lock:
            self.count += 1
            if self.count == 1:
                self.first = why
            if not aloud or self._told:
                return False
            self._told = True
            return True

    def state(self) -> tuple[int, str]:
        """Счёт и причина первой потери — одним снимком под замком."""
        with self._lock:
            return self.count, self.first

    def reset(self) -> None:
        """Обнулить счёт — при снятии ленты (`uninstall`)."""
        with self._lock:
            self.count = 0
            self.first = ""
            self._told = False


#: Один счёт на процесс: строки ленты собираются из разных потоков, а окно
#: показывает одно число. Обнуляет его `uninstall` — для тестов (правило 14).
LOSSES: Final[_Losses] = _Losses()


def _said(error: BaseException) -> str:
    """Тип и текст исключения, очищенные. Сам `str` чужого исключения вправе упасть."""
    try:
        text = str(error)
    except Exception:  # noqa: BLE001 — текст недоступен, тип остаётся
        text = "описание недоступно"
    head = text.strip().splitlines()[:1]
    return scrub(f"{type(error).__name__}: {head[0]}" if head else type(error).__name__)


def _lose(where: str, error: Exception) -> None:
    """Строка не собрана: первый отказ построителя — вслух одной строкой, остальные — счётом.

    Вслух — даже если до него уже были потери у обработчика (`handleError`):
    у них свой счёт «первого», и строку в журнале они не занимают.
    """
    why = _said(error)
    if LOSSES.add(f"{where}: {why}", aloud=True):
        log.warning(
            "строка ленты «Отладка» не собрана (%s): %s. Робот работает дальше; "
            "следующие такие отказы только считаются — число в шапке панели",
            where,
            why,
        )


def _guarded[**P](build: Callable[P, None]) -> Callable[P, None]:
    """Построитель строки, который не бросает. Приём один на все построители.

    Шаблонный метод вместо `try` в каждом: забытая защита у одного построителя
    уронила бы ровно тот путь, ради наблюдения за которым лента заведена, —
    чтение сокета (`LiveFeed._ride` снял бы поток до ручного «Подключиться»),
    дорисовку (решение по бару отложилось бы на пять минут) или ход движка.
    """

    @wraps(build)
    def safe(*args: P.args, **kwargs: P.kwargs) -> None:
        try:
            build(*args, **kwargs)
        except Exception as error:  # noqa: BLE001 — строка ленты не вправе ронять живой путь; счёт в `_lose`
            _lose(build.__name__, error)

    return safe


def _emit(source: str, level: int, template: str, *args: object, trade: bool = False) -> None:
    """Запись на логгер ленты. Аргументы ленивые: подставляет их обработчик."""
    _panel.log(level, template, *args, extra={"source": source, "trade": trade})


# ------------------------------------------------------------- обработчик


class PanelHandler(logging.Handler):
    """Буфер ленты: последние `capacity` строк с номерами. Пишут все, читает окно."""

    def __init__(self, capacity: int = BUFFER) -> None:
        # Уровень — явно INFO: при DEBUG в ленту пошли бы кадры сокета.
        super().__init__(level=logging.INFO)
        self._guard = threading.Lock()
        self._lines: deque[DebugLine] = deque(maxlen=capacity)
        self._next = 1

    def emit(self, record: logging.LogRecord) -> None:  # имя метода задано logging
        """Положить строку в буфер. Не бросает: отказ уходит в `handleError`."""
        if not _wanted(record):
            return
        try:
            text, level, source = _parts(record)
        except Exception:  # noqa: BLE001 — отказ уходит в `handleError` и считается
            self.handleError(record)
            return
        moment = datetime.fromtimestamp(record.created, MSK)
        with self._guard:
            self._lines.append(DebugLine(
                seq=self._next, time=moment, level=level, source=source, text=text
            ))
            self._next += 1

    def handleError(  # noqa: N802 — имя метода задано logging.Handler
        self, record: logging.LogRecord
    ) -> None:
        """Счесть отказ и **ничего не выводить** — как `TechnicalLog.handleError`.

        Штатный обработчик печатает в консоль `record.msg` и `record.args`
        до чистки. Из записи берётся только имя логгера. В журнал отсюда
        не пишется: запись вернулась бы в этот же обработчик. Причина видна
        человеку в шапке панели (`LOSSES.first` → `DebugBatch.cause`).
        """
        error = sys.exc_info()[1]
        why = _said(error) if error is not None else "отказ без исключения"
        LOSSES.add(f"{why} (запись логгера {record.name})")

    def since(self, after: int, limit: int) -> DebugBatch:
        """Строки с номером больше `after`, не больше `limit`, старшие первыми.

        `evicted` — сколько строк после `after` буфер успел вытеснить, пока
        их никто не читал: окно ставит на этом месте отметку.
        """
        lost, cause = LOSSES.state()
        with self._guard:
            if not self._lines:
                return DebugBatch(lost=lost, cause=cause)
            first = self._lines[0].seq
            start = max(after + 1, first) - first
            taken = tuple(islice(self._lines, start, start + max(limit, 0)))
        return DebugBatch(
            lines=taken, evicted=max(0, first - after - 1), lost=lost, cause=cause
        )


def _wanted(record: logging.LogRecord) -> bool:
    """Идёт ли запись в ленту: не помечена «мимо ленты» и не болтовня чужих библиотек."""
    if getattr(record, "panel", True) is False:
        return False
    foreign = record.name.partition(".")[0] in FOREIGN_LOGGERS
    return not (foreign and record.levelno < logging.WARNING)


def _parts(record: logging.LogRecord) -> tuple[str, DecisionLevel, str]:
    """Текст, важность и источник строки. Текст — одной строкой и очищенный."""
    text = record.getMessage()
    error = record.exc_info[1] if record.exc_info else None
    if error is not None:
        text = f"{text} [{_said(error)}]"
    # Одной строкой: перевод строки в ленте ломал бы ряд списка. Неразрывные
    # пробелы в числах (`ui.formatting`) остаются как есть.
    text = " ".join(part.strip() for part in scrub(text).splitlines() if part.strip())
    source = str(getattr(record, "source", "") or record.name)
    if record.levelno >= logging.ERROR:
        level = DecisionLevel.ERROR
    elif record.levelno >= logging.WARNING:
        level = DecisionLevel.WARNING
    else:
        level = DecisionLevel.TRADE if getattr(record, "trade", False) else DecisionLevel.INFO
    return text, level, source


def installed() -> PanelHandler | None:
    """Обработчик ленты, если он стоит на корне."""
    for handler in logging.getLogger().handlers:
        if isinstance(handler, PanelHandler):
            return handler
    return None


def install(capacity: int = BUFFER) -> PanelHandler:
    """Поставить ленту. Повтор возвращает тот же обработчик — дублей строк нет.

    Зовётся сразу за первым `setup_logging` (`app/main.py`). Повторный
    `setup_logging` ленту не снимает: `close_logging` трогает только файл.
    """
    handler = installed()
    if handler is None:
        handler = PanelHandler(capacity)
        logging.getLogger().addHandler(handler)
    if handler not in _panel.handlers:
        _panel.addHandler(handler)
    return handler


def uninstall() -> None:
    """Снять ленту с корня и с `app.panel`, обнулить счёт потерь. Для тестов."""
    for logger in (logging.getLogger(), _panel):
        for handler in list(logger.handlers):
            if isinstance(handler, PanelHandler):
                logger.removeHandler(handler)
                handler.close()
    LOSSES.reset()


# ------------------------------------------------------------- построители

#: Важность строки журнала программы → уровень записи. Сделка — INFO
#: с пометкой: в ленте она красится цветом сделки, а не предупреждения.
LOG_LEVELS: Final[dict[DecisionLevel, int]] = {
    DecisionLevel.INFO: logging.INFO,
    DecisionLevel.TRADE: logging.INFO,
    DecisionLevel.WARNING: logging.WARNING,
    DecisionLevel.ERROR: logging.ERROR,
}

#: Важность строки журнала движка → уровень записи.
JOURNAL_LEVELS: Final[dict[JournalLevel, int]] = {
    JournalLevel.INFO: logging.INFO,
    JournalLevel.TRADE: logging.INFO,
    JournalLevel.WARNING: logging.WARNING,
    JournalLevel.ERROR: logging.ERROR,
}

#: Докуда дошёл разбор бара — словами. Последний пройденный шаг
#: (`Outcome.steps`, PROTOTYPE.md §2) говорит, где разбор кончился.
#: Полноту таблицы проверяет тест.
STEP_WORDS: Final[dict[Step, str]] = {
    Step.MODE_OFF: "режим «Выключен», решений нет",
    Step.WARMUP: "свеча в разбор не пошла (прогрев средней или пропуск)",
    Step.SNAPSHOT: "снят снимок позиции",
    Step.CLOSE_TIME: "посчитано время закрытия",
    Step.REARM_TAKE_PROFIT: "переставлен тейк",
    Step.WINDOW: "вне торгового окна или нерабочий день — закрытие и выход",
    Step.CLOSE_BY_SIGNAL: "закрытие по сигналу средней",
    Step.CLOSE_ONLY: "режим «Только закрытие», входов нет",
    Step.STOP_AFTER_TAKE: "вход на сегодня закрыт (стоп после тейка или вход уже был)",
    Step.OPEN: "логика открытия",
}

#: Как пришёл бар — словами. Полноту проверяет тест.
PHASE_WORDS: Final[dict[DecisionPhase, str]] = {
    DecisionPhase.RECOUNT: "пересчёт",
    DecisionPhase.CATCH_UP: "догон",
    DecisionPhase.LIVE: "вживую",
}

#: Что сделало исполнение — словами. Сделкой становится только рыночная
#: заявка (`engine.Fill`); полноту по рыночным действиям проверяет тест.
FILL_WORDS: Final[dict[OrderAction, str]] = {
    OrderAction.OPEN: "вход в {side}",
    OrderAction.CLOSE: "выход из {side}а",
}


@_guarded
def tell(source: str, template: str, *args: object, level: int = logging.INFO) -> None:
    """Простая строка ленты. Аргументы — для `%`, подставляются лениво."""
    _emit(source, level, template, *args)


@_guarded
def note(event: str, reason: str, level: DecisionLevel) -> None:
    """Строка журнала программы (`HistoryPort.note`) — её единственный выход в ленту."""
    _emit(
        SOURCE_JOURNAL, LOG_LEVELS[level], "%s — %s", event, reason,
        trade=level is DecisionLevel.TRADE,
    )


@_guarded
def minute_started(ticker: str, minute: Candle) -> None:
    """Первый принятый снимок минуты. Дальше эта минута в ленте молчит до закрытия."""
    _emit(
        SOURCE_STREAM, logging.INFO, "%s: минута %s пошла, первый снимок — цена %s",
        ticker, fmt_time(minute.time), fmt_level(minute.close),
    )


@_guarded
def minute_closed(ticker: str, minute: Candle, refused: int) -> None:
    """Минута закончилась и записана в базу: цены и контракты по приращениям."""
    _emit(
        SOURCE_STREAM, logging.INFO,
        "%s: минута %s закрыта и записана в базу — О %s В %s Н %s З %s, %s контр.%s",
        ticker, fmt_time(minute.time), fmt_level(minute.open), fmt_level(minute.high),
        fmt_level(minute.low), fmt_level(minute.close), fmt_volume(minute.volume),
        f", отвергнуто снимков: {refused}" if refused else "",
    )


@_guarded
def bars_read(symbol: str, timeframe: str, candles: Sequence[Candle]) -> None:
    """Что прогон взял из базы: сколько закрытых баров и с какого по какое время."""
    if not candles:
        _emit(SOURCE_BASE, logging.INFO, "из базы %s %s: закрытых баров нет", symbol, timeframe)
        return
    _emit(
        SOURCE_BASE, logging.INFO, "из базы %s %s: %d закрытых баров с %s по %s",
        symbol, timeframe, len(candles),
        fmt_datetime(candles[0].time), fmt_datetime(candles[-1].time),
    )


@_guarded
def bar_due(symbol: str, timeframe: str, closed: datetime) -> None:
    """Порт увидел, что минуты бара собраны, и поставил пересчёт. Цен здесь нет.

    Цены закрытого бара говорит только ход движка (`happened`): бар,
    прочитанный здесь, движок мог и не получить — порт отбрасывает неполный
    бар (`HistoryPort._trusted`), и лента объявила бы закрытым то, чего
    движок не видел.
    """
    _emit(
        SOURCE_PORT, logging.INFO, "%s %s: минуты бара до %s собраны — пересчёт поставлен",
        symbol, timeframe, fmt_time(closed),
    )


@dataclass(frozen=True, slots=True)
class BarSeen:
    """Бар, который получил движок, и что он с ним сделал, — для строки бара."""

    candle: Candle
    outcome: Outcome
    average: float | None
    phase: DecisionPhase


class Turn(IntEnum):
    """Очередь событий одного мгновения: правило при равном времени (`happened`).

    Время события — его собственное поле: у исполнения и сделки — начало бара,
    по открытию которого прошла заявка (`Fill.at`), у слов движка — их `at`,
    у заявки — момент подачи, у строки бара — его закрытие. Касание уровня
    ставится в начало своего бара, к исполнению, которое оно вызвало: у строки
    для базы его время — закрытие (время расчёта, `OrderKind`), и по нему
    касание встало бы после собственной сделки.

    На открытии: исполнение → касание → исполнение по касанию → сделка →
    слова движка о них → заявка, рождённая сделкой (вооружение тейка).
    На закрытии: строка бара → слова движка → новые заявки.
    """

    FILL = 0
    TOUCH = 1
    TOUCH_FILL = 2
    DEAL = 3
    BAR = 4
    WORDS = 5
    ORDER = 6


@_guarded
def happened(
    *,
    fills: Sequence[Fill] = (),
    deals: Sequence[Deal] = (),
    words: Sequence[JournalEntry] = (),
    records: Sequence[OrderRecord] = (),
    bar: BarSeen | None = None,
) -> None:
    """Всё случившееся с прошлой строки ленты — по порядку событий, а не сбора.

    Одна точка на оба пути хода: незакрытый показ (решение 0065 — исполнение
    по открытию за минуты до закрытия бара) и закрытый бар. Порядок задают
    время события и очередь при равенстве (`Turn`); сортировка устойчивая,
    так что строки одного вида идут в порядке, в каком их дал ход.

    Слова «сигнал», «вход» берутся только из журнала движка (`words`);
    строка бара несёт числа и поля `Outcome`. Своих выводов лента не делает:
    вторая правда о решении хуже отсутствующей.

    ⚠️ Названный край: вход и касание его тейка в одном баре (3 сделки из 127,
    PROTOTYPE.md §5) — слова движка о входе встают после исполнения по тейку,
    а заявка вооружения после касания: у слов и у вооружения время одно —
    начало бара, а с исполнением их связывает только порядок записи.
    """
    touched = {r.order_id for r in records if r.kind is OrderKind.LEVEL_HIT}
    queue: list[tuple[datetime, Turn, Callable[[], None]]] = []
    for fill in fills:
        turn = Turn.TOUCH_FILL if fill.order_id in touched else Turn.FILL
        queue.append((fill.at, turn, partial(_fill_line, fill)))
    queue.extend((deal.exit_time, Turn.DEAL, partial(_deal_line, deal)) for deal in deals)
    queue.extend((entry.at, Turn.WORDS, partial(_word_line, entry)) for entry in words)
    for record in records:
        if record.kind is OrderKind.LEVEL_HIT:
            inside = record.at if bar is None else bar.candle.time
            queue.append((inside, Turn.TOUCH, partial(_order_line, record)))
        else:
            queue.append((record.at, Turn.ORDER, partial(_order_line, record)))
    if bar is not None:
        queue.append((close_time(bar.candle), Turn.BAR, partial(_bar_line, bar)))
    queue.sort(key=lambda item: (item[0], item[1]))
    for _, _, tell_line in queue:
        tell_line()


def _bar_line(bar: BarSeen) -> None:
    candle, outcome = bar.candle, bar.outcome
    head = (
        f"бар {fmt_time(candle.time)}–{fmt_time(close_time(candle))} "
        f"({candle.timeframe.minutes} мин), {PHASE_WORDS[bar.phase]}: О {fmt_level(candle.open)} "
        f"В {fmt_level(candle.high)} Н {fmt_level(candle.low)} З {fmt_level(candle.close)}"
    )
    if outcome.skipped:
        _emit(SOURCE_ENGINE, logging.INFO, "%s; в разбор не принят — %s", head, outcome.skipped)
        return
    window = {None: "окно не проверялось", True: "в окне", False: "вне окна"}
    step = outcome.steps[-1] if outcome.steps else None
    _emit(
        SOURCE_ENGINE, logging.INFO, "%s; средняя %s; %s; разбор дошёл до шага %s — %s",
        head, fmt_level(bar.average), window[outcome.in_window],
        int(step) if step is not None else "—",
        STEP_WORDS[step] if step is not None else "шагов нет",
    )


def _word_line(entry: JournalEntry) -> None:
    _emit(
        SOURCE_ENGINE, JOURNAL_LEVELS[entry.level], "%s — %s", entry.event, entry.reason,
        trade=entry.level is JournalLevel.TRADE,
    )


def _fill_line(fill: Fill) -> None:
    words = FILL_WORDS.get(fill.action, f"исполнение ({fill.action.value})")
    _emit(
        SOURCE_ORDERS, logging.INFO, "исполнено: %s, %s контр. по %s в %s%s",
        words.format(side=fill.side.label.lower()), fmt_volume(fill.volume),
        fmt_level(fill.price), fmt_time(fill.at),
        "" if fill.commission is None else f", комиссия {fmt_money(fill.commission)}",
        trade=True,
    )


def _deal_line(deal: Deal) -> None:
    net = deal.net
    _emit(
        SOURCE_ORDERS, logging.INFO,
        "сделка закрыта (%s): %s %s контр., %s → %s, чистыми %s",
        deal.exit_reason.label, deal.side.label.lower(), fmt_volume(deal.volume),
        fmt_level(deal.entry_price), fmt_level(deal.exit_price),
        "— (тариф комиссии не задан)" if net is None else fmt_money(net, sign=True),
        trade=True,
    )


def _order_line(record: OrderRecord) -> None:
    _emit(
        SOURCE_ORDERS, logging.INFO, "%s: %s контр.%s%s; закрытие бара %s, средняя %s%s",
        convert.order_words(record), fmt_volume(record.volume),
        "" if record.level is None else f", уровень {fmt_level(record.level)}",
        "" if record.limit_price is None else f", предел {fmt_level(record.limit_price)}",
        fmt_level(record.close_price), fmt_level(record.average),
        f"; причина: {record.reason}" if record.reason else "",
        trade=True,
    )


@_guarded
def recount_done(
    run: HistoryRun,
    first: datetime | None,
    last: datetime | None,
    orders: int,
    seconds: float,
) -> None:
    """Итог пересчёта одной строкой — вместо строки на каждый из тысяч баров."""
    _emit(
        SOURCE_ENGINE, logging.INFO,
        "пересчёт по базе окончен: %d баров с %s по %s, строк журнала %d, заявок %d, "
        "сделок %d, за %.1f с. Дальше бары разбираются по мере закрытия",
        run.bars, fmt_datetime(first), fmt_datetime(last), len(run.journal), orders,
        run.summary.trades, seconds,
    )


@_guarded
def run_closed(run: HistoryRun) -> None:
    """Ход движка закрыт: выключили поток, сменили настройку, закрыли программу."""
    _emit(
        SOURCE_ENGINE, logging.INFO, "живой ход движка закрыт: баров %d, сделок %d%s",
        run.bars, run.summary.trades, f"; остановлен: {run.halted}" if run.halted else "",
    )
