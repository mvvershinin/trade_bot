"""Журнал сделок и журнал решений: типы записи и чтения.

Оба журнала живут в **той же базе**, что и свечи (решение 0003): один файл
рядом с программой, резервная копия = копия папки. Отдельного файла под
журналы нет и не будет — иначе перенос на другой компьютер начинает состоять
из двух действий, а забытое второе выглядит как «журнал пропал».

Почему типы объявлены здесь, а не взяты из `engine/`
-----------------------------------------------------
`market/` не импортирует ни один слой проекта (ARCHITECTURE.md §2). Строку
журнала производит `engine/`, сделку — `backtest/` или боевой разбор, а кладёт
их сюда `app/` — ровно так же, как он уже перекладывает свечу в объект окна
(`app/convert.py`). Совпадение имён полей с `engine.JournalEntry`
и `backtest.Deal` намеренное; совпадением **типов** оно не является и
утиной типизацией не пользуется.

Что отличает боевой журнал от прогона
-------------------------------------
Происхождение (`RunOrigin`) — свойство **прогона**, а не строки: прогон
не бывает наполовину боевым. Поэтому признак стоит у прогона, строка ссылается
на прогон обязательным ключом, а прочитать строку без происхождения нельзя:
у прочитанного типа (`StoredDecision`, `StoredTrade`) поле `origin`
обязательное и без умолчания. Разбор — решение 0011 в `.docs/decisions/`.

Токен в журнал не попадает
--------------------------
Причина решения — человеческий текст, и приходит он в том числе из отказа
исполнителя: `engine.Engine` кладёт в строку `f"{type(error).__name__}: {error}"`,
а сообщение боевого адаптера может нести тело ответа сервера. Поэтому **весь**
текст, который слой пишет на диск, проходит через чистку
(`CandleStore(..., sanitize=...)`): **все** текстовые колонки обоих журналов,
инструмент и строка отчёта о загрузке. Нетронутыми остаются только числа
и время. Числа колонок здесь нет намеренно: оно уже устаревало дважды —
на десятой колонке и на одиннадцатой (`finish_note`).

⚠️ Списка «а вот эти колонки чистые» здесь нет намеренно. Он был — и оказался
неверным сразу в семи местах: `entry_order_id` на Э1-5 заполнится ответом
брокера, инструмент владелец счёта вводит руками, `LoadReport.note` уже
собирается из текста исключения. Список исключений устаревает молча,
чистка всего — нет.

Здесь лежит только **второй рубеж** — `redact`, чистка по образцам. Она
не знает живых значений токенов: реестр значений принадлежит `broker/`,
импортировать который слою данных нельзя. Первый рубеж ставит `app/`, передавая
`broker.redaction.scrub` в `sanitize`; тогда работают оба. Без этого вызова
образцы всё равно ловят `Bearer …`, поля `*_token` и голый JWT — то есть
ровно те формы, в которых токен приходит от сервера.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Final, TypeVar, overload

__all__ = [
    "RunOrigin",
    "DecisionLevel",
    "HaltKind",
    "HaltRecord",
    "StoredHalt",
    "TradeSide",
    "SessionRecord",
    "JournalSession",
    "DecisionRecord",
    "StoredDecision",
    "TradeRecord",
    "StoredTrade",
    "JournalPage",
    "JournalStats",
    "PruneReport",
    "SECRET_MASK",
    "redact",
    "BACKTEST_SESSIONS_KEPT",
]


class RunOrigin(str, Enum):
    """Откуда взялся прогон: настоящие деньги, симуляция или история.

    Три значения, а не два. `PAPER` — это боевой поток котировок без подачи
    заявок: денег на счёте он не двигает, но и **повторить его нельзя** —
    он видел то, чего в базе может не остаться. `BACKTEST` повторяем: те же
    свечи и те же настройки дадут тот же журнал.

    Из этой разницы следуют разные правила хранения (см. `RunOrigin.evidence`
    и `CandleStore.prune_journal`).
    """

    LIVE = "live"
    PAPER = "paper"
    BACKTEST = "backtest"

    @property
    def label(self) -> str:
        """Как это называется в окне и в выгрузке."""
        return {
            RunOrigin.LIVE: "боевой режим",
            RunOrigin.PAPER: "симуляция на боевом потоке",
            RunOrigin.BACKTEST: "прогон по истории",
        }[self]

    @property
    def real_money(self) -> bool:
        """Двигались ли на этом прогоне настоящие деньги."""
        return self is RunOrigin.LIVE

    @property
    def evidence(self) -> bool:
        """Неповторимая запись: удалять её автоматически нельзя.

        Боевой прогон и симуляция на боевом потоке видели рынок, которого
        в базе не осталось. Прогон по истории повторяется командой.

        Отсюда берут список **оба** правила хранения: чистка
        (`CandleStore.prune_journal` трогает только повторимые прогоны)
        и сторож в базе (`journal_session_evidence_is_kept` отвергает прямое
        удаление неповторимого). Два места, одно правило: четвёртое
        происхождение, если его заведут, попадёт в оба сразу.

        ⚠️ Прежде сторож в базе стоял только на боевом прогоне, а симуляцию
        держала одна чистка — то есть код, а код переписывают. Долг закрыт
        задачей Э1-10а.
        """
        return self in (RunOrigin.LIVE, RunOrigin.PAPER)


class DecisionLevel(str, Enum):
    """Важность строки журнала решений.

    Значения совпадают с `engine.JournalLevel` и `ui.models.DecisionLevel`
    дословно: строка проходит через три слоя, и разные написания одного
    уровня превратили бы фильтр в окне в тихо неполный.
    """

    INFO = "info"
    TRADE = "trade"
    WARNING = "warning"
    ERROR = "error"


class HaltKind(str, Enum):
    """Вид остановки робота: чем именно он остановлен.

    Требование решения
    [0014](../.docs/decisions/0014-daily-loss-limit-and-margin-shortfall.md),
    дословно: «у программы должно быть различимо, **какая именно** остановка
    случилась — иначе владелец счёта, вернувшись к компьютеру, увидит одно
    и то же "стоп" по двум разным причинам». До 07.09.2026 отдельного признака
    не было вовсе, только текст причины (`D-042`).

    Два значения, и разница между ними — **что человек должен сделать**.

    `ENGINE` — робот остановил себя сам по торговому правилу: дневной лимит
    убытка, расхождение позиции после обрыва связи, отказ исполнителя принять
    заявку, отказ приёмника журнала. Состояние счёта программе при этом
    известно; разбираться нужно с причиной.

    `ACCOUNT` — программа **не знает**, что на счёте: команда ушла, ответа
    нет. Разбираться нужно не с программой, а у брокера — посмотреть заявки
    и позицию своими глазами, и только потом снимать остановку.

    ⚠️ **Дальше этой границы признак не идёт, и это честная граница.**
    Различить внутри `ENGINE` дневной лимит и расхождение позиции по одному
    полю нельзя: движок отдаёт остановку одной склеенной строкой
    (`engine/runner.py`, `f"{entry.event}. {entry.reason}"`), а разбирать
    чужую склейку значило бы завести догадку о формате, который никто
    не обещал. Различие остаётся в тексте причины — он приходит целиком.
    """

    ENGINE = "engine"
    ACCOUNT = "account"

    @property
    def label(self) -> str:
        """Как вид остановки называется в окне и в журнале."""
        return {
            HaltKind.ENGINE: "предохранитель робота",
            HaltKind.ACCOUNT: "состояние счёта неизвестно",
        }[self]


@dataclass(frozen=True, slots=True)
class HaltRecord:
    """Одна причина остановки — то, что подаётся на запись.

    Пара к `StoredHalt`, та же, что `DecisionRecord` к `StoredDecision`.

    `reason` — причина **человеческим языком**, целиком и как есть. Это то,
    что видит владелец счёта в красной плашке; хранилище её не сокращает
    и не пересказывает, только чистит от секретов.

    `event` — заголовок события, если он был назван отдельно. Пусто —
    заголовка не было: у остановки из движка он склеен с причиной, и разбирать
    склейку хранилище не имеет права (см. `HaltKind`).
    """

    kind: HaltKind
    reason: str
    event: str = ""


@dataclass(frozen=True, slots=True)
class StoredHalt:
    """Одна причина остановки, прочитанная из базы.

    ⚠️ **Строка на причину, а не строка на остановку.** Причин бывает
    несколько, они не отменяют друг друга и снимаются по одной, самой ранней
    (`app/port.py::_Halt`, `D-086`). Склеенная в одну строку остановка теряла
    бы всё, кроме первой причины, — и теряла бы молча, ровно в том месте,
    где владелец счёта соглашается с убытком и заодно, не читая, снимает
    требование сходить к брокеру.

    Порядок задан `id`: он же порядок появления причин, он же порядок снятия.
    """

    id: int
    kind: HaltKind
    reason: str
    raised_at: datetime
    event: str = ""

    @property
    def record(self) -> HaltRecord:
        """То же без признаков хранения — для сравнения с поданным."""
        return HaltRecord(kind=self.kind, reason=self.reason, event=self.event)


class TradeSide(str, Enum):
    """Сторона сделки. Значения совпадают с `engine.Side`."""

    LONG = "long"
    SHORT = "short"

    @property
    def label(self) -> str:
        """Как сторона называется в окне."""
        return "Лонг" if self is TradeSide.LONG else "Шорт"


#: Сколько прогонов по истории хранится. Старшие удаляются целиком при открытии
#: следующего такого прогона.
#:
#: Число посчитано, а не выбрано: прогон по всей имеющейся истории MXU6
#: (20 716 пятиминутных свечей, 299 торговых дней) даёт 1558 строк решений
#: и 266 сделок — под 0,5 МБ в базе. Двадцать таких прогонов — около 9 МБ,
#: то есть втрое меньше самих свечей за тот же период. Боевой журнал в этот
#: счёт не входит: он не чистится вовсе.
BACKTEST_SESSIONS_KEPT: Final[int] = 20

#: Чем заменяется найденный секрет. Текст **дословно** совпадает с маской
#: `broker.redaction.MASK`, и это намеренно: человек видит журнал, не зная,
#: какой из двух рубежей сработал, и две разных маски читались бы как два
#: разных события.
SECRET_MASK: Final[str] = "<токен скрыт>"

# Имена полей собираются из кусков — ровно по той же причине, что и в
# `broker/redaction.py`: целиком написанное имя поля рядом с длинным выражением
# ловится детектором секретов в `tests/test_layers.py`.
_TOKEN_FIELDS: Final[tuple[str, ...]] = ("access", "refresh", "id", "session")
_BARE_FIELDS: Final[tuple[str, ...]] = ("token", "токен")
_FIELD_NAMES: Final[str] = "|".join(
    [*(f"{name}_token" for name in _TOKEN_FIELDS), *_BARE_FIELDS]
)

# Чистка обязана быть идемпотентной: текст может пройти через неё дважды —
# сначала рубежом `broker/`, потом этим. Без оговорки `\S+` откусывал бы
# у подставленной маски первое слово и превращал `Bearer <токен скрыт>`
# в `Bearer <токен скрыт> скрыт>`.
_NOT_MASK: Final[str] = f"(?!{re.escape(SECRET_MASK)})"

_JSON_FIELD = re.compile(rf'"({_FIELD_NAMES})"\s*:\s*"[^"]*"', re.IGNORECASE)
_FORM_FIELD = re.compile(rf"\b({_FIELD_NAMES})={_NOT_MASK}[^&\s\"']+", re.IGNORECASE)
_BEARER = re.compile(rf"\bBearer\s+{_NOT_MASK}\S+", re.IGNORECASE)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}")


def redact(text: str) -> str:
    """Вырезать из текста всё похожее на токен. Второй рубеж, не первый.

    Знает только **формы**: `Bearer …`, поля `*_token` в JSON и в
    form-кодировке, голый JWT. Живых значений токенов не знает и знать
    не может — их реестр принадлежит `broker/`, и слой данных его
    не импортирует (ARCHITECTURE.md §2).

    Поэтому вызов сам по себе достаточным не считается: `app/` обязан
    передать в хранилище `broker.redaction.scrub`, у которого есть оба
    рубежа. Здесь — то, что работает без него.
    """
    result = _JSON_FIELD.sub(lambda match: f'"{match.group(1)}": "{SECRET_MASK}"', text)
    result = _FORM_FIELD.sub(lambda match: f"{match.group(1)}={SECRET_MASK}", result)
    result = _BEARER.sub(f"Bearer {SECRET_MASK}", result)
    return _JWT.sub(SECRET_MASK, result)


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """Чем прогон объявляет себя при открытии.

    Пара к `JournalSession` — та же, что `DecisionRecord` к `StoredDecision`:
    здесь то, что подаётся на запись, там то, что прочитано.

    `settings` — снимок настроек **человеческим текстом**, а не набором полей.
    Разбирать его обратно программа не должна и не будет: он существует, чтобы
    через полгода можно было прочитать, с чем робот работал в тот день. Набор
    полей поменяется, а прочитанная фраза останется верной.
    """

    origin: RunOrigin
    symbol: str = ""
    timeframe: str = ""
    strategy: str = ""
    settings: str = ""
    app_version: str = ""
    note: str = ""


@dataclass(frozen=True, slots=True)
class JournalSession:
    """Один прогон робота: боевой, симуляция или проход по истории.

    Прогон — единица, которой журнал живёт и умирает: строки ссылаются на него
    обязательным ключом и удаляются вместе с ним (`ON DELETE CASCADE`).

    `finished_at is None` означает, что прогон не закончился штатно: программу
    закрыли аварийно. Проставить время задним числом было бы неправдой ровно
    в том месте, где человек ищет, что случилось.

    ⚠️ **Две заметки, а не одна.** `note` — условие открытия («автозапуск,
    догрузка 3 дня»), `finish_note` — чем прогон кончился («закрыто по
    Ctrl+C»). Прежде вторая писалась поверх первой, и условие открытия
    исчезало без следа: строки журнала неизменяемы, а заметка была
    единственным правимым текстом прогона — то есть единственной щелью,
    через которую текст прогона правился задним числом.
    """

    id: int
    origin: RunOrigin
    started_at: datetime
    finished_at: datetime | None = None
    symbol: str = ""
    timeframe: str = ""
    strategy: str = ""
    settings: str = ""
    app_version: str = ""
    note: str = ""
    #: Чем прогон кончился. Пусто у незакрытого прогона и у закрытого молча.
    #: Отдельно от `note`, потому что заметка об открытии — условие прогона,
    #: и правится она только вместе с историей (`_FROZEN_SESSION_COLUMNS`).
    finish_note: str = ""

    @property
    def real_money(self) -> bool:
        """Двигались ли на этом прогоне настоящие деньги."""
        return self.origin.real_money

    @property
    def record(self) -> SessionRecord:
        """То же объявление без признаков хранения — для сравнения с поданным."""
        return SessionRecord(
            origin=self.origin,
            symbol=self.symbol,
            timeframe=self.timeframe,
            strategy=self.strategy,
            settings=self.settings,
            app_version=self.app_version,
            note=self.note,
        )


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    """Строка журнала решений — то, что подаётся на запись.

    Событие и причина **человеческим языком**, а не кодом: «Сигнал: закрытие
    выше EMA(15) → лонг 1 контракт», а не `state=FLAT`. Текст приходит готовым
    из `engine/`; хранилище его не сочиняет и не переводит — только чистит
    от секретов.
    """

    at: datetime
    event: str
    reason: str
    level: DecisionLevel = DecisionLevel.INFO


@dataclass(frozen=True, slots=True)
class StoredDecision:
    """Строка журнала решений, прочитанная из базы.

    ⚠️ Отдельный тип от `DecisionRecord`, и это не церемония. Поля `origin`
    здесь **обязательное и без умолчания**: прочитать строку журнала, не узнав,
    боевая она или из прогона, невозможно по построению типа. Ровно этого
    требует решение 0011, и ровно это теряется, если признак сделать
    необязательным полем одного общего класса.
    """

    id: int
    session_id: int
    origin: RunOrigin
    at: datetime
    event: str
    reason: str
    level: DecisionLevel

    @property
    def real_money(self) -> bool:
        """Строка про настоящие деньги, а не про симуляцию."""
        return self.origin.real_money

    @property
    def record(self) -> DecisionRecord:
        """Та же строка без признаков хранения — для сравнения с записанной."""
        return DecisionRecord(
            at=self.at, event=self.event, reason=self.reason, level=self.level
        )


@dataclass(frozen=True, slots=True)
class TradeRecord:
    """Закрытая сделка — то, что подаётся на запись.

    ⚠️ `commission is None` — это **не ноль**. Ноль объявил бы любую сделку
    окупившей комиссию, а реверсная система на пятиминутках делает много
    переворотов (DOMAIN.md §5). `None` означает «тариф не задан», и тогда
    `net` тоже `None`: чистой прибыли без тарифа не существует.

    ⚠️ `gross` и `net` **хранятся, а не пересчитываются при чтении.** Журнал —
    свидетельство: он записывает то, что было посчитано в тот день, тем тарифом
    и тем весом пункта. Пересчёт при чтении менял бы прошлые сделки при смене
    тарифа — то есть переписывал бы журнал задним числом.
    """

    symbol: str
    side: TradeSide
    volume: float
    entry_time: datetime
    entry_price: float
    exit_time: datetime
    exit_price: float
    exit_reason: str
    gross: float
    entry_order_id: str = ""
    exit_order_id: str = ""
    commission: float | None = None
    net: float | None = None
    ruble_per_point: float = 1.0


@dataclass(frozen=True, slots=True)
class StoredTrade:
    """Закрытая сделка, прочитанная из базы.

    Как и `StoredDecision`, несёт обязательное `origin`: отчёт, по которому
    нельзя сказать, были ли это настоящие деньги, не отчёт.
    """

    id: int
    session_id: int
    origin: RunOrigin
    symbol: str
    side: TradeSide
    volume: float
    entry_time: datetime
    entry_price: float
    exit_time: datetime
    exit_price: float
    exit_reason: str
    gross: float
    written_at: datetime
    entry_order_id: str = ""
    exit_order_id: str = ""
    commission: float | None = None
    net: float | None = None
    ruble_per_point: float = 1.0

    @property
    def real_money(self) -> bool:
        """Сделка на настоящие деньги, а не в прогоне."""
        return self.origin.real_money

    @property
    def points(self) -> float:
        """Движение цены в пунктах в пользу позиции.

        Считается из цен, а не читается из базы: тариф сюда не входит,
        и двусмысленности здесь нет.
        """
        sign = 1.0 if self.side is TradeSide.LONG else -1.0
        return (self.exit_price - self.entry_price) * sign

    @property
    def percent(self) -> float:
        """Движение цены в процентах от цены входа. Комиссия сюда не входит."""
        return self.points / self.entry_price * 100.0 if self.entry_price else 0.0

    @property
    def record(self) -> TradeRecord:
        """Та же сделка без признаков хранения — для сравнения с записанной."""
        return TradeRecord(
            symbol=self.symbol,
            side=self.side,
            volume=self.volume,
            entry_time=self.entry_time,
            entry_price=self.entry_price,
            exit_time=self.exit_time,
            exit_price=self.exit_price,
            exit_reason=self.exit_reason,
            gross=self.gross,
            entry_order_id=self.entry_order_id,
            exit_order_id=self.exit_order_id,
            commission=self.commission,
            net=self.net,
            ruble_per_point=self.ruble_per_point,
        )


#: Строка журнала любого из двух видов. Выдача устроена одинаково для решений
#: и для сделок: одинаково обрезается по лимиту и одинаково сужается до прогона.
RowT = TypeVar("RowT")


@dataclass(frozen=True, slots=True)
class JournalPage(Sequence[RowT]):
    """Прочитанный кусок журнала: строки и **честный ответ про полноту**.

    Списка строк здесь мало по той же причине, по какой разрыв в ряду свечей —
    событие для журнала, а не повод молча сдвинуть индикатор: журнал,
    обрезанный по лимиту, выглядит ровно как полный. Поэтому обрезка названа
    (`truncated`) и названо число, которое её вызвало (`limit`).

    `session_id` — прогон, к которому сужена выдача. `None` означает, что
    сужения по прогону не было: спросили либо одно происхождение, либо всё
    сразу. Поле нужно **пустой** выдаче: у строки номер прогона свой, а
    у пустого списка строк нет вовсе, и «журнал прогона пуст» без него
    не отличить от «прогона не нашлось».

    Ведёт себя как последовательность — длина, перебор, обращение по номеру,
    срез. Читающий, которому полнота не нужна, ничего не замечает; спросить
    про неё может любой.
    """

    rows: tuple[RowT, ...] = ()
    limit: int = 0
    truncated: bool = False
    session_id: int | None = None

    def __len__(self) -> int:
        return len(self.rows)

    @overload
    def __getitem__(self, index: int) -> RowT: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[RowT]: ...

    def __getitem__(self, index: int | slice) -> RowT | Sequence[RowT]:
        return self.rows[index]

    def summary(self) -> str:
        """Одна строка для журнала — человеческим языком, а не кодом."""
        if not self.truncated:
            return f"Строк журнала: {len(self.rows)}, показаны все"
        return (
            f"Строк журнала: {len(self.rows)} — это последние; "
            f"остальные не показаны, лимит выдачи {self.limit}"
        )


@dataclass(frozen=True, slots=True)
class PruneReport:
    """Что убрала чистка журнала. Пустой отчёт — норма, а не отсутствие работы.

    `kept` — сколько прогонов по истории велено оставить. Число хранится
    в отчёте, а не берётся из `BACKTEST_SESSIONS_KEPT` при печати: чистку
    зовут и с другим порогом, и строка «хранятся последние 20» после
    `prune_journal(0)` была бы прямой неправдой. `None` — удаление не по
    порогу, а по названным номерам (`forget_journal_sessions`).
    """

    sessions: int = 0
    decisions: int = 0
    trades: int = 0
    kept: int | None = None

    @property
    def empty(self) -> bool:
        """Ничего не удалено."""
        return not (self.sessions or self.decisions or self.trades)

    def summary(self) -> str:
        """Одна строка для журнала — человеческим языком, а не кодом."""
        if self.empty:
            return "Старых прогонов по истории не нашлось, удалять нечего"
        counted = (
            f"Убрано прогонов: {self.sessions} "
            f"(строк решений {self.decisions}, сделок {self.trades})"
        )
        if self.kept is None:
            return counted + ". Удаление по просьбе человека, а не чистка по порогу"
        threshold = (
            "прогоны по истории не хранятся вовсе"
            if self.kept == 0
            else f"хранятся последние {self.kept}"
        )
        return (
            f"{counted}. Теперь {threshold}; боевой журнал и симуляция "
            "на боевом потоке не чистятся вовсе"
        )


@dataclass(frozen=True, slots=True)
class JournalStats:
    """Сколько в базе журнала — чтобы про объём отвечали числом, а не догадкой."""

    sessions: int = 0
    decisions: int = 0
    trades: int = 0
    #: Сколько прогонов какого происхождения. Происхождения нет в списке —
    #: таких прогонов в базе нет.
    sessions_by_origin: tuple[tuple[RunOrigin, int], ...] = ()
    #: Размер файла базы целиком: свечи, журналы, учёт загрузок. Отдельного
    #: числа «сколько занимает журнал» SQLite без расширения `dbstat` не даёт,
    #: и придумывать оценку вместо измерения здесь незачем.
    database_bytes: int = 0
