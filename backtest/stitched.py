"""Прогон по склейке: каждый контракт — своим куском, стык — закрытием позиции.

Зачем это существует
--------------------
Решение 0061: на каждом отрезке времени считается контракт, который тогда
жил настоящей жизнью. Тестер гоняет период, а не тикер: июнь — сентябрь
по MXU6, дальше по MXZ6. Здесь — сам прогон по уже нарезанным кускам.
Кто режет период на куски и читает бары из базы — `app/stitched.py`.

Как устроен прогон
------------------
**Кусок — отдельный `replay` со свежим торговым модулем.** Не один прогон
по склеенному ряду. Иначе средняя старого контракта перетекла бы в новый
и скачок цены на стыке лёг бы в среднюю, а из неё — в решения.

**Прогрев — не новая ветка.** Перед баром куска подаются бары того же
контракта из прогрева, ровно `warmup_wanted` штук. Движок отбрасывает свечу,
пока их накоплено не больше периода средней (PROTOTYPE.md, «Прогрев
считается от начала поданного отрезка»). Значит, ровно N баров прогрева
целиком уходят в отброс, а первый бар куска — первый, на закрытии которого
возможно решение. Баров больше N — последние из них уже решали бы, поэтому
это отказ вслух. Меньше N — недогретая средняя не торгует, отброс съедает
начало куска. Это не ошибка, но и не молчание: строка в журнале и в `problems`.

**Стык закрывается здесь, а не в движке.** Рыночный выход модель
исполнения проводит по открытию следующей свечи, а следующей свечи того же
контракта в конце куска нет. Подать бар нового контракта — значит исполнить
выход по его цене, и скачок стыка лёг бы прямо в прибыль. Поэтому
открытая позиция закрывается по закрытию последнего бара куска, с комиссией
и тем же проскальзыванием, что у любой сделки. Движок об этом не знает:
у него добавилась только причина выхода «конец поданного отрезка»
(`ExitReason.SERIES_END`) — данные, а не ветка (ARCHITECTURE.md §1).

**Последний кусок не закрывается.** Его конец — конец выбранного периода,
а не стык. Позиция остаётся открытой, как у обычного `replay`, и прогон
одного контракта даёт ровно прежние сделки.

**Первый кусок стартует холодным**, без прогрева: так начинается любой
прогон прототипа (PROTOTYPE.md §«Прогрев»). Прогрев нужен только новому
контракту на стыке — это и решал владелец счёта.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time

from backtest.execution import Costs, Minutes
from backtest.history import Deal, HistoryRun, Summary, replay, summarise
from engine import (
    MSK,
    EngineSettings,
    ExitReason,
    JournalEntry,
    JournalLevel,
    MarketCandle,
    OrderAction,
    Position,
    Side,
    close_time,
)
from strategies import Strategy

__all__ = [
    "Piece",
    "PieceRun",
    "StitchedRun",
    "replay_pieces",
    "stitched_lines",
]


@dataclass(frozen=True, slots=True)
class Piece:
    """Кусок периода, занятый одним контрактом, с барами прогрева перед ним.

    `warmup` — закрытые бары **того же** контракта до начала куска.
    `warmup_wanted` — сколько их просили: период средней из настроек.
    """

    symbol: str
    since: date
    until: date
    warmup: tuple[MarketCandle, ...]
    body: tuple[MarketCandle, ...]
    warmup_wanted: int
    #: Минутки **этого** контракта под барами куска и порядок их обхода.
    #: `None` — сторож уровня по размаху бара, как до минуток. Чужой
    #: контракт сюда класть нельзя: цены соседнего контракта на стыке
    #: другие, и уровень задевался бы ценой, которой у этого не было.
    minutes: Minutes | None = None
    #: Шаг цены **этого** контракта по его карточке биржи. `None` — биржа
    #: о нём не сообщила, кусок идёт с шагом прогона.
    price_step: float | None = None
    #: Дни куска без единого бара, не отмеченные загруженными, — отрезками.
    #: Заполняет нарезка (`app/stitched.py`), прогон их не читает: это
    #: подпись «на чём считано», а не данные решения (B-069).
    missing: tuple[tuple[date, date], ...] = ()


@dataclass(frozen=True, slots=True)
class PieceRun:
    """Что дал один кусок: прогон движка и сделка закрытия на стыке."""

    piece: Piece
    run: HistoryRun
    #: Сделка, закрытая на стыке по последней цене контракта. `None` —
    #: к стыку позиции не было, либо это последний кусок.
    seam: Deal | None = None

    @property
    def deals(self) -> tuple[Deal, ...]:
        """Сделки куска по порядку; закрытие на стыке — последней."""
        return self.run.deals if self.seam is None else (*self.run.deals, self.seam)

    @property
    def summary(self) -> Summary:
        """Итог куска — по его сделкам, включая закрытие на стыке."""
        return summarise(self.deals)


@dataclass(frozen=True, slots=True)
class StitchedRun:
    """Итог прогона по склейке: общий и по каждому контракту."""

    pieces: tuple[PieceRun, ...] = ()
    deals: tuple[Deal, ...] = ()
    journal: tuple[JournalEntry, ...] = ()
    summary: Summary = Summary()
    #: Позиция на конец последнего куска — как у обычного прогона.
    position: Position | None = None
    halted: str = ""
    #: Всё, что прогон не смог сделать как просили: недогретые куски,
    #: пустые куски, дни без контракта. Каждая строка есть и в журнале.
    problems: tuple[str, ...] = ()
    average: tuple[tuple[datetime, float], ...] = ()


def _side_word(side: Side) -> str:
    return "лонг" if side is Side.LONG else "шорт"


def _money(value: float | None, *, sign: bool = True) -> str:
    """Рубли как в окне отчёта: «−9 762,00 ₽», у комиссии без знака."""
    if value is None:
        return "тарифа нет"
    text = f"{value:+,.2f}" if sign else f"{value:,.2f}"
    return text.replace(",", " ").replace(".", ",").replace("-", "−") + " ₽"


def _seam_deal(
    run: HistoryRun, last: MarketCandle, costs: Costs, settings: EngineSettings
) -> Deal | None:
    """Закрыть позицию, оставшуюся к концу куска, по закрытию последнего бара.

    Вход берётся из последней сделки прогона: при открытой позиции это
    обязательно её открытие. Иначе позиции и сделки разошлись, и считать
    деньги по такому прогону нельзя — отказ вслух.
    """
    position = run.position
    if position is None:
        return None
    opened = run.fills[-1] if run.fills else None
    if opened is None or opened.action is not OrderAction.OPEN or opened.side is not position.side:
        raise ValueError(
            "позиция к концу куска открыта, а последней сделкой прогона "
            "было не её открытие — закрыть её на стыке не по чему"
        )
    price = costs.fill_price(float(last.close), OrderAction.CLOSE, position.side)
    one_side = costs.commission(opened.volume)
    return Deal(
        side=position.side,
        volume=opened.volume,
        entry_time=opened.at,
        entry_price=opened.price,
        entry_order_id=opened.order_id,
        exit_time=close_time(last),
        exit_price=price,
        exit_order_id=f"{opened.order_id}-series-end",
        exit_reason=ExitReason.SERIES_END,
        commission=None if one_side is None else one_side * 2,
        ruble_per_point=settings.ruble_per_point,
    )


def _seam_entry(deal: Deal, old: str, new: str, until: date) -> JournalEntry:
    """Строка журнала о закрытии на стыке — человеческим языком."""
    count = f"{deal.volume:g}"
    return JournalEntry(
        at=deal.exit_time,
        event=(
            f"Смена контракта {old} → {new}: {_side_word(deal.side)} "
            f"{count} контр. закрыт"
        ),
        reason=(
            f"Период {old} кончился {until:%d.%m.%Y}, дальше считается {new}. "
            f"Позиция закрыта по последней цене {old} — {deal.exit_price:g}. "
            f"Результат {deal.points:+g} пункта, валовая {_money(deal.gross)}, "
            f"комиссия {_money(deal.commission, sign=False)} отдельно. Скачок цены между "
            "контрактами в результат не входит"
        ),
        level=JournalLevel.TRADE,
    )


def _warning(at: datetime, event: str, reason: str) -> JournalEntry:
    return JournalEntry(at=at, event=event, reason=reason, level=JournalLevel.WARNING)


def _check_warmup(piece: Piece) -> str | None:
    """Прогрев ровно N, меньше — пометка, больше — отказ.

    :raises ValueError: баров прогрева больше, чем просили: последние
        из них уже принимали бы решения, и в сделки попал бы прогрев.
    """
    got = len(piece.warmup)
    if got > piece.warmup_wanted:
        raise ValueError(
            f"{piece.symbol}: подано {got} баров прогрева при периоде "
            f"{piece.warmup_wanted} — лишние решали бы и дали бы сделки "
            "до начала куска"
        )
    if got == piece.warmup_wanted:
        return None
    return (
        f"{piece.symbol}: перед {piece.since:%d.%m.%Y} в базе {got} закрытых "
        f"баров прогрева из {piece.warmup_wanted} — первые "
        f"{piece.warmup_wanted - got} баров куска уйдут на прогрев и сделок не дадут"
    )


async def replay_pieces(
    pieces: Sequence[Piece],
    build: Callable[[], Strategy],
    settings: EngineSettings,
    costs: Costs,
    notes: Sequence[tuple[datetime, str]] = (),
) -> StitchedRun:
    """Прогнать куски подряд, каждый отдельно, и сложить итог.

    `build` собирает **свежий** торговый модуль на каждый кусок: средняя
    старого контракта в новый не переходит. `notes` — предупреждения
    вызывающего (например, дни периода без контракта): они ложатся
    в журнал и в `problems` наравне со своими.

    Шаг цены куска (`Piece.price_step`) — свойство контракта, а не прогона:
    кусок идёт со своим шагом и в движке (предел заявки на выход), и в
    издержках (проскальзывание в шагах). Не назван — шаг из `settings`
    и `costs`.
    """
    journal: list[JournalEntry] = [
        _warning(at, "Склейка неполная", text) for at, text in notes
    ]
    problems: list[str] = [text for _, text in notes]
    made: list[PieceRun] = []
    halted = ""
    for number, piece in enumerate(pieces):
        # Сосед на стыке — следующий НЕПУСТОЙ кусок: пустой не прогоняется,
        # перехода в него нет. Он же — следующий элемент `StitchedRun.pieces`,
        # и по нему подписывает стык окно (`app.stitched_view`). Непустых
        # дальше нет — стыка нет, позиция остаётся открытой, как у последнего.
        after = next((later.symbol for later in pieces[number + 1:] if later.body), "")
        if not piece.body:
            text = (
                f"{piece.symbol}: за {piece.since:%d.%m.%Y} … "
                f"{piece.until:%d.%m.%Y} в базе нет ни одной закрытой свечи — "
                "кусок не прогнан"
            )
            problems.append(text)
            journal.append(_warning(
                datetime.combine(piece.since, time(0, 0), MSK),
                "Кусок склейки пуст", text,
            ))
            continue
        short = _check_warmup(piece)
        if short is not None:
            problems.append(short)
            journal.append(_warning(close_time(piece.body[0]), "Прогрев неполный", short))
        own = piece.price_step
        mine, priced = (
            (settings, costs) if own is None or own <= 0
            else (replace(settings, price_step=own), replace(costs, price_step=own))
        )
        run = await replay(
            (*piece.warmup, *piece.body), build(), mine,
            costs=priced, minutes=piece.minutes,
        )
        following = "" if run.halted else after
        seam = None
        if following:
            seam = _seam_deal(run, piece.body[-1], priced, mine)
        journal.extend(run.journal)
        if seam is not None:
            journal.append(_seam_entry(seam, piece.symbol, following, piece.until))
        made.append(PieceRun(piece=piece, run=run, seam=seam))
        if run.halted:
            halted = run.halted
            break
    deals = tuple(deal for one in made for deal in one.deals)
    last = made[-1] if made else None
    return StitchedRun(
        pieces=tuple(made),
        deals=deals,
        journal=tuple(journal),
        summary=summarise(deals),
        position=None if last is None or last.seam is not None else last.run.position,
        halted=halted,
        problems=tuple(problems),
        average=tuple(point for one in made for point in one.run.average),
    )


def _summary_line(title: str, summary: Summary) -> str:
    """Итог строкой. Без сделок комиссии и чистой нет — «—», а не «тарифа нет».

    Сводка без сделок пуста при любом тарифе (`summarise`), и «тарифа нет»
    при заданных 14 ₽ за контракт было неправдой (29.09.2026).
    """
    if not summary.trades:
        return (
            f"{title}: сделок 0, валовая {_money(summary.gross_profit)}, "
            f"комиссия {EMPTY}, чистая {EMPTY}"
        )
    return (
        f"{title}: сделок {summary.trades}, валовая {_money(summary.gross_profit)}, "
        f"комиссия {_money(summary.commission, sign=False)}, чистая {_money(summary.net_profit)}"
    )


#: Число, которого нет, потому что нет сделок, — как в окне отчёта.
EMPTY = "—"


def stitched_lines(run: StitchedRun) -> list[str]:
    """Отчёт словами: общий итог, разбивка по контрактам, что пошло не так.

    Комиссия — всегда отдельной колонкой (CLAUDE.md, правило 4).
    """
    lines = [_summary_line("Итог по склейке", run.summary)]
    for one in run.pieces:
        piece = one.piece
        seam = "" if one.seam is None else ", позиция закрыта на стыке"
        lines.append(
            _summary_line(
                f"{piece.symbol} {piece.since:%d.%m.%Y} … {piece.until:%d.%m.%Y}",
                one.summary,
            )
            + f"; прогрев {len(piece.warmup)} из {piece.warmup_wanted}{seam}"
        )
    lines.extend(f"⚠ {text}" for text in run.problems)
    if run.halted:
        lines.append(f"⚠ Прогон остановлен: {run.halted}")
    return lines
