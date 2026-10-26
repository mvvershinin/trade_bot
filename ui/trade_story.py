"""Сделки симуляции из базы в окне «Отчёты»: что окно просит и что получает.

Миниплан — `.docs/plans/reports-window-step2-paper-trades.md`, §4–5.

Живой ход симуляции пишет в базу сделки, заявки и решения (`app/paper_log.py`).
Окно «Отчёты» показывает их двумя отборами, и оба делаются **не здесь**:

* «как было» — сделки, которые робот решил закрыть вживую, и открытые
  вживую, но закрытые на догоне;
* «пересчёт с утра» — за каждый день сделки последнего прогона, шедшего
  в этот день.

Отбирает и раскладывает `app/paper_report.py`, перекладывает в эти типы
`app/convert.py`. Окно получает готовое: строки таблицы (`TradeRow`, тот же
тип, что у журнала «Сделки») и разбор каждой сделки (`TradeStory`).

Здесь же — то, что окну всё-таки приходится сделать самому, и это только
показ: строки разбора (`story_lines`) и отбор готового по выбранному
периоду (`unrecorded_runs`, `days_without_run`). Ни одной цены и ни одного
решения здесь не считается.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from ui.formatting import fmt_datetime, fmt_level, fmt_time
from ui.models import DecisionRow, TradeRow

__all__ = [
    "SOURCES",
    "OrderStep",
    "PaperReport",
    "PaperReportRequest",
    "PaperRunSpan",
    "PaperTrades",
    "Phase",
    "ReportSource",
    "TradeStory",
    "caught_up_trades",
    "day_list",
    "days_without_run",
    "silent_days",
    "story_lines",
    "unrecorded_runs",
]


class ReportSource(enum.Enum):
    """Какие сделки показывает окно «Отчёты»."""

    #: Прогон, показанный на графике и в журнале «Сделки» (шаг 1).
    SHOWN = "shown"
    #: Симуляция: сделки, закрытые вживую или открытые вживую и закрытые
    #: на догоне, по всем прогонам инструмента.
    AS_WAS = "as_was"
    #: Симуляция: за каждый день — последний прогон, шедший в этот день.
    RECOUNT = "recount"


#: Положения переключателя: источник, подпись, подсказка простым языком.
#: Таблица, а не три куска сборки: добавить положение — одна строка.
SOURCES: tuple[tuple[ReportSource, str, str], ...] = (
    (
        ReportSource.SHOWN,
        "Прогон на экране",
        "Сделки того прогона, что сейчас на графике и в журнале «Сделки».",
    ),
    (
        ReportSource.AS_WAS,
        "Симуляция: как было",
        "Сделки, которые робот на симуляции закрыл вживую или открыл вживую "
        "и закрыл на догоне, — по всем прогонам симуляции этого инструмента, "
        "все контракты одним рядом. Сделки, где ни вход, ни выход робот "
        "вживую не решал, сюда не входят.",
    ),
    (
        ReportSource.RECOUNT,
        "Симуляция: пересчёт с утра",
        "За каждый день — сделки последнего прогона симуляции, который шёл "
        "в этот день: как он посчитал весь день с утра. Так видно, что дали "
        "бы последние настройки дня, если бы стояли с начала торгов.",
    ),
)


@dataclass(frozen=True, slots=True)
class PaperReportRequest:
    """Просьба прочитать сделки симуляции из базы.

    `instrument` — код на экране. Пусто — порт берёт инструмент из настроек.
    Все контракты того же актива порт находит сам по таблице контрактов
    (ответ владельца счёта В3).
    """

    instrument: str = ""


class Phase(enum.Enum):
    """Когда робот принял решения о сделке: вживую, на догоне или в пересчёте.

    Решает фаза строк заявок входа и выхода (`market.DecisionPhase`): её
    ставит ход по тому, как пришёл бар, — не часы машины и не время сделки.
    Догон — разбор свечей, пропущенных до подключения: их закрытие робот
    не застал.
    """

    #: Вход и выход решены вживую.
    LIVE = "live"
    #: Позицию открыл пересчёт истории при старте прогона, закрыл живой ход
    #: (ответ владельца счёта В2).
    ENTRY_RECOUNTED = "entry_recounted"
    #: Позицию открыл разбор пропущенных свечей после подключения, закрыл
    #: живой ход.
    ENTRY_CAUGHT_UP = "entry_caught_up"
    #: Позицию открыл живой ход, закрыл разбор пропущенных свечей. В «как
    #: было» — строкой и в итоге (ответ владельца счёта 12.10.2026).
    EXIT_CAUGHT_UP = "exit_caught_up"
    #: Выход решил разбор пропущенных свечей, вход — он же или пересчёт.
    #: В «как было» не входит, окно называет такие сделки числом.
    CAUGHT_UP = "caught_up"
    #: Сделка целиком из пересчёта истории при старте прогона.
    RECOUNTED = "recounted"

    @property
    def label(self) -> str:
        """Пометка в таблице — коротко."""
        return _PHASE_LABELS[self]

    @property
    def words(self) -> str:
        """То же в разборе сделки — полной фразой."""
        return _PHASE_WORDS[self]


_PHASE_LABELS = {
    Phase.LIVE: "вживую",
    Phase.ENTRY_RECOUNTED: "вход из пересчёта",
    Phase.ENTRY_CAUGHT_UP: "вход на догоне",
    Phase.EXIT_CAUGHT_UP: "выход на догоне",
    Phase.CAUGHT_UP: "догон",
    Phase.RECOUNTED: "пересчёт",
}

_PHASE_WORDS = {
    Phase.LIVE: "Вход и выход робот решил вживую.",
    Phase.ENTRY_RECOUNTED: (
        "Вход из пересчёта: позицию открыл пересчёт истории при старте "
        "прогона, закрыл живой ход."
    ),
    Phase.ENTRY_CAUGHT_UP: (
        "Вход на догоне: позицию открыл разбор пропущенных свечей после "
        "подключения, закрыл живой ход."
    ),
    Phase.EXIT_CAUGHT_UP: (
        "Выход на догоне: позицию открыл живой ход, а выход решил разбор "
        "пропущенных свечей после подключения, а не живой ход — программа "
        "была закрыта или поток обрывался."
    ),
    Phase.CAUGHT_UP: (
        "Сделка из разбора пропущенных свечей после подключения: вживую "
        "робот её не закрывал."
    ),
    Phase.RECOUNTED: (
        "Сделка из пересчёта истории при старте прогона: вживую робот её "
        "не совершал."
    ),
}


@dataclass(frozen=True, slots=True)
class OrderStep:
    """Заявка в разборе сделки: когда решена, что, почему и при каких числах.

    Числа — те, что лежат в строке заявки (`market.OrderRecord`): закрытие
    и средняя бара, на котором заявка родилась, уровень тейка, предел выхода
    по времени (ответ владельца счёта В4). `None` — у заявки этого числа нет.
    """

    at: datetime
    what: str
    reason: str = ""
    close: float | None = None
    average: float | None = None
    level: float | None = None
    limit: float | None = None


@dataclass(frozen=True, slots=True)
class TradeStory:
    """Почему робот вошёл и вышел — для одной сделки симуляции.

    `entry` и `exit` — заявки входа и выхода; `None` — заявки в базе не нашлось,
    и разбор говорит об этом словами. `entry_rows` и `exit_rows` — строки
    журнала решений баров, на которых приняты решения. `settings` — снимок
    настроек прогона, тот, что записан при его открытии.
    """

    run_id: int
    run_started: datetime
    phase: Phase
    exit_reason: str
    entry: OrderStep | None = None
    exit: OrderStep | None = None
    entry_rows: tuple[DecisionRow, ...] = ()
    exit_rows: tuple[DecisionRow, ...] = ()
    settings: str = ""


@dataclass(frozen=True, slots=True)
class PaperTrades:
    """Один отбор: строки таблицы и разбор каждой — два ряда одной длины."""

    rows: tuple[TradeRow, ...] = ()
    stories: tuple[TradeStory, ...] = ()


@dataclass(frozen=True, slots=True)
class PaperRunSpan:
    """Прогон симуляции днями МСК: с какого по какой день он шёл.

    `recorded` — записывал ли прогон сделки (`market.PaperRun.mark`). Прогон
    без записи окно называет словами, а не нулём сделок.
    """

    run_id: int
    first_day: date
    last_day: date
    recorded: bool


@dataclass(frozen=True, slots=True)
class PaperReport:
    """Сделки симуляции инструмента из базы — оба отбора разом.

    `covered` — дни «пересчёта»: день и номер последнего прогона, шедшего
    в этот день. Дня в списке нет — прогона в этот день не было.

    `caught_up` — дни выхода (МСК) сделок, решённых на догоне без единого
    живого решения: в «как было» их нет, и окно называет их числом
    за период (`caught_up_trades`), а не молчит.

    `trouble` — чтение базы отказало: окно показывает эту фразу вместо
    таблицы (правило 13). `notes` — оговорки к прочитанному.
    """

    instrument: str = ""
    codes: tuple[str, ...] = ()
    runs: tuple[PaperRunSpan, ...] = ()
    as_was: PaperTrades = field(default_factory=PaperTrades)
    caught_up: tuple[date, ...] = ()
    recount: PaperTrades = field(default_factory=PaperTrades)
    covered: tuple[tuple[date, int], ...] = ()
    notes: tuple[str, ...] = ()
    trouble: str = ""


# ---------------------------------------------------------------- разбор


def _numbers(step: OrderStep) -> str:
    """Числа заявки одной строкой: только те, что есть."""
    parts = [
        f"{name} {fmt_level(value)}"
        for name, value in (
            ("закрытие бара", step.close),
            ("средняя", step.average),
            ("уровень тейка", step.level),
            ("предел выхода по времени", step.limit),
        )
        if value is not None
    ]
    return " · ".join(parts)


def _step_lines(
    title: str, step: OrderStep | None, missing: str, *, reason: bool = True
) -> list[str]:
    """Решение о входе или выходе: когда, что, почему и числа.

    `reason=False` — причина уже сказана своей строкой («Почему вошли»).
    """
    if step is None:
        return [f"   {title}: {missing}"]
    head = f"   {title}: {fmt_datetime(step.at)} МСК · {step.what}"
    numbers = _numbers(step)
    lines = [head + (f" · {numbers}" if numbers else "")]
    if reason and step.reason:
        lines.append(f"      {step.reason}")
    return lines


def _bar_lines(title: str, step: OrderStep | None, rows: tuple[DecisionRow, ...]) -> list[str]:
    """Строки журнала решений бара, на котором принято решение."""
    if step is None:
        return []
    head = f"   Журнал решений, бар {fmt_datetime(step.at)} МСК ({title}):"
    if not rows:
        return [head, "      строк журнала на этом баре в базе нет"]
    return [head] + [
        f"      {fmt_time(row.time)} {row.event}" + (f" — {row.reason}" if row.reason else "")
        for row in rows
    ]


def story_lines(story: TradeStory) -> list[str]:
    """Разбор сделки симуляции строками — под строками `trade_lines` шага 1.

    Отступ тот же, что у `trade_lines`: блок сделки читается одним куском.
    Чего нет в базе — сказано словами, а не пропущено (правило 13).
    """
    entry, exit_ = story.entry, story.exit
    why = entry.reason if entry is not None and entry.reason else "причина в базе не записана"
    lines = [
        f"   Прогон симуляции №{story.run_id}, начат "
        f"{fmt_datetime(story.run_started)} МСК",
        f"   {story.phase.words}",
        f"   Почему вошли: {why}",
    ]
    lines += _step_lines(
        "Решение о входе", entry,
        "заявки входа в базе нет — когда и при каких числах решён вход, не известно",
        reason=False,
    )
    lines += _step_lines(
        "Решение о выходе", exit_,
        "заявки выхода в базе нет — когда и при каких числах решён выход, не известно",
    )
    lines += _bar_lines("вход", entry, story.entry_rows)
    lines += _bar_lines("выход", exit_, story.exit_rows)
    settings = [f"      {line}" for line in story.settings.splitlines() if line.strip()]
    lines.append("   Настройки прогона:")
    lines += settings or ["      снимок настроек в базе пуст"]
    return lines


# ---------------------------------------------------------------- период


def _meets(first: date, last: date, since: date | None, until: date | None) -> bool:
    """Отрезок дней [first; last] задевает период. `None` — граница не задана."""
    return (since is None or last >= since) and (until is None or first <= until)


def unrecorded_runs(report: PaperReport, since: date | None, until: date | None) -> int:
    """Сколько прогонов периода сделок не записывали — их называют словами."""
    return sum(
        1 for run in report.runs
        if not run.recorded and _meets(run.first_day, run.last_day, since, until)
    )


def caught_up_trades(report: PaperReport, since: date | None, until: date | None) -> int:
    """Сколько сделок периода решено на догоне без живых решений — их нет в «как было»."""
    return sum(1 for day in report.caught_up if _meets(day, day, since, until))


def days_without_run(
    report: PaperReport, since: date | None, until: date | None, today: date
) -> list[date]:
    """Дни периода, в которые не шёл ни один прогон симуляции. Не позже сегодня.

    «Всё» (границ нет) — от первого дня прогонов до последнего. Прогонов
    нет вовсе — пусто: о пустой базе окно говорит отдельно.
    """
    covered = {day for day, _run in report.covered}
    if not covered:
        return []
    first = since if since is not None else min(covered)
    last = min(until if until is not None else max(covered), today)
    return [day for day in day_list(first, last) if day not in covered]


def silent_days(report: PaperReport, since: date | None, until: date | None) -> list[date]:
    """Дни периода, чей последний прогон сделки не записывал.

    «Пересчёт с утра» берёт день у последнего прогона дня; если это прогон
    сборки, не писавшей сделок, сделок дня нет — и это не «ноль сделок».
    """
    silent = {run.run_id for run in report.runs if not run.recorded}
    return [
        day for day, run_id in report.covered
        if run_id in silent and _meets(day, day, since, until)
    ]


def day_list(first: date, last: date) -> list[date]:
    """Дни от `first` до `last` включительно. `first` позже — пусто."""
    return [first + timedelta(days=n) for n in range((last - first).days + 1)]
