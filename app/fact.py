"""«Факт» пары движков глазами наблюдения и порта — без единого имени `broker/`.

Э4, шаг 2, под-шаг 2в (`.docs/plans/E4-live-orders.md` §7, §12; решение 0066).
Пара — это два движка одного хода: «факт» с боевым исполнителем и теневой
«прогноз» с `ShadowExecutor`. Ход и порт (`app/observe.py`, `app/port.py`)
обязаны оставаться неспособными подать заявку **по устройству**: брокера они
не импортируют ни одним именем, и это стережёт `tests/test_app_observe.py`.
Боевой исполнитель поэтому приходит к ним **параметром** — значением порта
`engine.OrderExecutor`, — а собирает его `app/live_trading.py`, которому
брокер положен.

Здесь — шов между двумя сторонами:

* `LiveLimits` — пределы ожидания боевого исполнителя. Значения, а не правило:
  их собирает окно (`app/convert.py::live_limits`), и сборке окна незачем
  тянуть за собой пишущий путь ради класса из семи чисел;
* `FactDesks` и `FactDesk` — что порту нужно знать о боевой стороне: можно ли
  включить бой, как собрать исполнитель, остановлен ли он, есть ли у брокера
  незакрытые команды;
* `day_record` — перекладка дня из базы (`market.TradingDay`) в значение
  движка (`engine.DayRecord`, решение 0066, Р7). Перекладка, а не пересчёт:
  таблица полей и тест её полноты;
* `LiveStatus` — снимок боя для окна: доступен ли и почему нет, позиция
  «факта», сделки «факта» и «прогноза» отдельно, с меткой происхождения
  данными (`D-172`), а не ветвлением по режиму в окне.
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol

from backtest import HistoryRun
from engine import AccountFunds, DayRecord, OrderExecutor, Side
from market import OpeningFunds, TradingDay
from ui.models import RunOrigin

__all__ = [
    "DAY_FIELDS",
    "DAY_LEFT_OUT",
    "FUNDS_FIELDS",
    "FactDesk",
    "FactDesks",
    "FactPosition",
    "FactRefused",
    "FactRequest",
    "LiveLimits",
    "LiveStatus",
    "day_record",
    "position_words",
    "signed_position",
]


# --------------------------------------------------------------- пределы


def _finite(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


@dataclass(frozen=True, slots=True)
class LiveLimits:
    """Пределы ожидания боевого исполнителя, в секундах и свечах.

    Правило 16: каждое число — поле окна (`app/convert.py::live_limits`).
    Живьём не замерено ни одно: сколько брокер молчит после заявки,
    неизвестно (план §10, «Что не закрыто»).

    ⚠️ Действуют **со следующей сборки исполнителя**: исполнитель берёт их
    при создании и на ходу не перечитывает. Сборка — включение боя,
    «Возобновить» и пересборка хода (`app/port.py`).

    :param stale_after: заявка, решённая раньше «сейчас − размер свечи − это»,
        к брокеру не подаётся (план §2.2 п. 1): догрузка после обрыва
        прогнала бы через «факт» старые бары с настоящими заявками.
    :param event_wait: сколько ждать первого события по заявке, прежде чем
        спросить статус по ключу (§2.2 п. 7–8).
    :param fill_wait: сколько ждать конца рыночной заявки (`2` или `8`).
        Частичная без конца к этому сроку — остаток снимается (0066, Р6).
    :param cancel_wait: сколько ждать `4` после запроса на снятие.
    :param limit_exit_bars: сколько закрытых свечей ждать исполнения выхода
        по времени с пределом (Р3). Одно число — одно место: кладёт его
        сборка исполнителя из `EngineSettings.time_exit_wait_bars`
        (`app/port.py::_fact_limits`). Иначе остановку раньше назовёт движок,
        и текст не скажет о живой заявке у брокера (С9).
    :param position_settle: пауза перед вторым чтением позиции, прежде чем
        объявить расхождение: портфель брокера может отставать от потока
        сделок, и «закрыть всё» по отставшему портфелю продало бы дважды.
    :param reconnect_pauses: паузы между попытками открыть поток статусов —
        по нарастающей, последняя повторяется.
    """

    stale_after: float = 60.0
    event_wait: float = 10.0
    fill_wait: float = 30.0
    cancel_wait: float = 10.0
    limit_exit_bars: int = 3
    position_settle: float = 1.0
    reconnect_pauses: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 30.0)

    def __post_init__(self) -> None:
        waits = (self.stale_after, self.event_wait, self.fill_wait, self.cancel_wait)
        pauses = (self.position_settle, *self.reconnect_pauses)
        if any(not _finite(value) or value <= 0 for value in waits):
            raise ValueError(f"пределы ожидания — положительные секунды: {waits}")
        if not self.reconnect_pauses or any(not _finite(value) or value < 0 for value in pauses):
            raise ValueError(f"паузы — неотрицательные секунды, хотя бы одна: {pauses}")
        bars = self.limit_exit_bars
        if isinstance(bars, bool) or not isinstance(bars, int) or bars < 1:
            raise ValueError(f"свечей ожидания выхода с пределом — целое от 1: {bars!r}")


# --------------------------------------------------------------- позиция


def signed_position(side: Side | None, volume: float) -> int:
    """Позиция движка числом со знаком: лонг — плюс, шорт — минус, нет — ноль."""
    if side is None or not volume:
        return 0
    return int(volume) if side is Side.LONG else -int(volume)


def position_words(signed: int) -> str:
    """Позиция словами для журнала: «лонг 2», «шорт 1», «позиции нет»."""
    if signed > 0:
        return f"лонг {signed}"
    if signed < 0:
        return f"шорт {-signed}"
    return "позиции нет"


# --------------------------------------------------------------- боевая сторона


class FactRefused(Exception):
    """Бой не включается — и почему, словами владельцу счёта.

    Не остановка робота: позиция у брокера не тронута. Порт говорит причину
    строкой журнала и оставляет наблюдение.

    :param notes: что сверка успела сделать с командами прошлых запусков
        (снятие живой заявки — тоже команда брокеру) — каждое своей фразой.
        Порт говорит их строками журнала и при отказе: молча снятая заявка
        хуже отказа.
    :param position: позиция у брокера со знаком, если её успели прочитать;
        `None` — не прочитана.
    """

    def __init__(
        self, human: str, *, notes: tuple[str, ...] = (), position: int | None = None
    ) -> None:
        super().__init__(human)
        self.human = human
        self.notes = notes
        self.position = position


@dataclass(frozen=True, slots=True)
class FactRequest:
    """Что порт отдаёт при сборке боевой стороны.

    :param session_id: прогон «факта» в журнале прогонов (`RunOrigin.LIVE`):
        каждая команда брокеру записывается под ним (`StoreBook`).
    :param on_news: у исполнителя появилось, что отдать движку, — порт ставит
        проход через единственный слот прогона (план §3).
    :param notice: сообщение человеку без остановки — строкой журнала решений.
    :param ruble_per_point: вес пункта — для дня по исполнениям (Р7).
    """

    ticker: str
    bar_minutes: int
    ruble_per_point: float
    limits: LiveLimits
    session_id: int
    on_news: Callable[[], None]
    notice: Callable[[str], None]


class FactDesk(Protocol):
    """Собранная боевая сторона: исполнитель «факта» и вопросы к брокеру."""

    @property
    def executor(self) -> OrderExecutor:
        """Исполнитель «факта» — значение порта движка."""
        ...

    @property
    def day(self) -> DayRecord | None:
        """День до запуска (Р7) — обоим движкам пары. `None` — день пуст."""
        ...

    @property
    def notes(self) -> tuple[str, ...]:
        """Что сделала сверка при сборке с командами прошлых запусков — словами."""
        ...

    @property
    def halted(self) -> str | None:
        """Почему исполнитель торговать не будет — его словами; `None` — будет."""
        ...

    @property
    def undelivered(self) -> tuple[str, ...]:
        """Имена заявок, чьи сделки у брокера есть, а движку ещё не отданы."""
        ...

    async def watch(self) -> None:
        """Записана минутка: сторожа трейлинга по ней."""
        ...

    async def relinked(self, expected: int) -> str | None:
        """После обрыва потока статусов — сверка (0017, 0020). Возврат — причина остановки."""
        ...

    async def unclosed(self) -> tuple[str, ...]:
        """Незакрытые команды журнала этого счёта — словами; пусто — нет ни одной."""
        ...

    async def holding(self) -> tuple[int, float | None]:
        """Позиция у брокера свежим чтением: со знаком и средняя цена; отказ — исключение."""
        ...

    async def resting_levels(self) -> tuple[float, ...]:
        """Цены лимитных заявок, незакрытых в журнале, — что живо у брокера."""
        ...

    async def aclose(self) -> None:
        """Закрыть исполнитель: поток статусов и запись журнала."""
        ...


class FactDesks(Protocol):
    """Чем порт собирает боевую сторону. Ставит сборка (`app/main.py`)."""

    def refusal(self) -> str | None:
        """Нельзя ли включить бой, не спрашивая брокера (нет связи, токен для чтения)."""
        ...

    def open(self, request: FactRequest) -> Awaitable[FactDesk]:
        """Собрать исполнитель после сверки. Отказ — `FactRefused` с причиной."""
        ...


# --------------------------------------------------------------- день до запуска


def _funds(opening: OpeningFunds | None) -> AccountFunds | None:
    """Счёт на утро слоя данных → деньги счёта движка. Поле в поле, без счёта."""
    if opening is None:
        return None
    return AccountFunds(**{name: getattr(opening, name) for name in FUNDS_FIELDS})


#: Поля `AccountFunds`, перекладываемые из `OpeningFunds` один к одному.
#: Номер счёта не перекладывается: день уже отобран по счёту при чтении.
FUNDS_FIELDS: Final[tuple[str, ...]] = ("at", "equity", "free", "margin_per_contract")

#: Поле `DayRecord` ← как его взять из `TradingDay`. Таблица, а не перечисление
#: руками (правило 9): тест сверяет её с полями обоих классов, и поле, забытое
#: здесь, — красный тест, а не молча пустое значение у движка после перезапуска.
DAY_FIELDS: Final[Mapping[str, Callable[[TradingDay], object]]] = {
    "day": lambda day: day.day,
    "opening": lambda day: _funds(day.opening),
    "realized": lambda day: day.realized,
    "take_profit_date": lambda day: day.take_profit_date,
    "last_exit_date": lambda day: day.last_exit_date,
    "unknown_commissions": lambda day: day.unknown_commissions,
    "foreign": lambda day: day.foreign,
}

#: Поля `TradingDay`, которых движок не получает, — с причиной.
DAY_LEFT_OUT: Final[Mapping[str, str]] = {
    "account": "чей день — отбор по счёту сделан при чтении из базы",
    "gross": "входит в `realized` вместе с комиссией",
    "commission": "входит в `realized` вместе с валовым",
}


def day_record(day: TradingDay) -> DayRecord:
    """День из базы → значение движка (`Engine.on_day`). Перекладка по таблице."""
    # `Any`: имена полей приходят из таблицы, и тип каждого сверяет не mypy,
    # а `DayRecord.__post_init__` и тест полноты таблицы.
    values: dict[str, Any] = {name: take(day) for name, take in DAY_FIELDS.items()}
    return DayRecord(**values)


# --------------------------------------------------------------- снимок для окна


@dataclass(frozen=True, slots=True)
class FactPosition:
    """Позиция «факта» — то, что робот считает открытым у брокера.

    :param take_level: уровень, который сейчас сторожится: лимитная заявка
        у брокера (неподвижный тейк) или сторож программы (трейлинг).
        `None` — уровня нет.
    """

    side: Side
    volume: float
    entry_price: float
    take_level: float | None = None

    @property
    def signed(self) -> int:
        """Со знаком: лонг — плюс, шорт — минус."""
        return signed_position(self.side, self.volume)


@dataclass(frozen=True, slots=True)
class LiveStatus:
    """Бой глазами окна. Собирает порт (`HistoryPort.live_status`).

    :param available: можно ли сейчас нажать «Включить бой».
    :param why_not: почему нельзя — каждая причина своей фразой. Пусто при
        `available`. Причины, которые знает только брокер (позиция у него,
        незакрытые команды), появляются после попытки включения.
    :param wanted: бой просили включить (кнопка нажата и не отжата).
    :param on: бой включён — «факт» собран с боевым исполнителем.
    :param armed: прогрев пройден, решения «факта» настоящие.
    :param held: пересборка «факта» отложена — почему; пусто — не отложена.
    :param refused: почему бой не включился в последний раз — словами
        (позиция у брокера, незакрытые команды, токен); пусто — не отказывал.
    :param halted: почему бой стоит — причина остановки робота словами, пока
        бой просили; пусто — не стоит. Окно показывает её независимо от `on`:
        после остановки боевая сторона закрыта (`on` ложно), а бой просили.
    :param position: позиция «факта». У собранной пары — её; после остановки,
        выключения или отключения потока — та, что осталась у брокера: прочитана
        у него при закрытии боевой стороны (движок мог не знать исполненного
        входа, N2), уровень — только если тейк ещё жив у брокера (N3).
        `None` — позиции нет или не было боя.
    :param position_note: пусто — позиция свежая (живой «факт»). Иначе — почему
        ей нельзя верить как текущей: «на момент остановки HH:MM МСК —
        у брокера могло измениться» (и что именно неизвестно). Окно показывает
        её рядом с позицией в шапке и в вопросах.
    :param take_note: что известно о тейке позиции, оставшейся у брокера, —
        готовыми словами, те же, что в строке «Бой выключен» (`/risk` 2в Н7,
        B-082): «тейк у брокера по X жив — снимите его, если закрываете позицию
        руками», «по журналу команд у брокера жива лимитная заявка по X —
        возможно, это тейк; проверьте…», «тейка у неё нет» (только когда
        за это можно поручиться), «жив ли тейк у брокера, неизвестно —
        проверьте…». Пусто — у живой позиции «факта»: тогда тейк — `take_level`.
    :param fact: ход «факта» — его сделки, заявки и журнал.
    :param forecast: ход «прогноза» — расчётные сделки той же пары.
    :param fact_origin: метка данных «факта» — боевой режим (`ui.models.RunOrigin`,
        как у строк журнала): подпись «заявка у брокера» окно берёт отсюда, а не
        из своего режима (`D-172`).
    :param forecast_origin: метка данных «прогноза» — симуляция.
    """

    available: bool
    why_not: tuple[str, ...] = ()
    wanted: bool = False
    on: bool = False
    armed: bool = False
    held: str = ""
    refused: str = ""
    halted: str = ""
    position: FactPosition | None = None
    position_note: str = ""
    take_note: str = ""
    fact: HistoryRun | None = None
    forecast: HistoryRun | None = None
    fact_origin: RunOrigin = RunOrigin.LIVE
    forecast_origin: RunOrigin = RunOrigin.PAPER
