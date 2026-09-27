"""Таблица контрактов в окне: какой контракт действующий, загрузка по нему, стыки.

Зачем отдельно от `app/port.py`
-------------------------------
Решение 0061: на каждом отрезке времени считается и показывается контракт,
который тогда жил настоящей жизнью; хвост нового контракта до его рубежа
не качается. Порту здесь достаются три тонкие вещи — загрузка «по контракту»,
сведения «действующий контракт» и стыки на графике, — а разбор таблицы
и фразы для человека живут тут. Порт и так больше любого другого файла
проекта, а это разные обязанности: чтение таблицы не знает про прогон.

Что здесь НЕ делается
---------------------
* **Не меняется торгуемый тикер.** Решение 0016: переход в бою ручной.
  `ContractNotice` — только сведения; кнопку перехода окно проводит через
  обычное подтверждение правки настроек.
* **Не режется прогон.** Движок по-прежнему идёт по одному контракту —
  тому, что стоит в настройках. Прогон по склейке с закрытием на стыке —
  фаза Ф3. Прежние контракты на графике — **только свечи**, без сделок.
* **Цены на стыке не подгоняются** (0049 §2): скачок виден как есть.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta

from market import (
    MSK,
    CandleStore,
    ContractError,
    ContractLoad,
    ContractRow,
    MarketWorker,
    Timeframe,
    asset_of,
    current_contract,
    pieces,
    rows_of_asset,
)
from market import (
    Candle as MarketCandle,
)
from ui.models import ChartData, ContractNotice, ContractSeam, HistoryLoadOutcome

__all__ = [
    "ChartContext",
    "chart_context",
    "load_lead",
    "load_outcome",
    "notice_of",
    "not_started",
    "split_shown",
    "trim_before",
]

#: Самая ранняя дата, с которой спрашиваются куски, когда глубина показа —
#: «вся история». Не настройка: это «раньше любого контракта в таблице».
_EARLIEST = date(2000, 1, 1)


def notice_of(store: CandleStore, configured: str, *, today: date) -> ContractNotice:
    """Действующий контракт по таблице — и расхождение с настройкой.

    Зовётся **в потоке данных** (`MarketWorker.call`). Отказ таблицы — это
    сведения, а не беда: пустая таблица на свежей папке значит «у биржи
    ещё не спрашивали», и сказать это надо спокойно.
    """
    try:
        asset = asset_of(configured.strip())
    except ContractError:
        # Код не квартальный фьючерс: цепочки у него нет, сверять не с чем.
        return ContractNotice(configured=configured)
    if not rows_of_asset(store.contracts(), asset):
        return ContractNotice(
            configured=configured,
            trouble=f"Цепочки {asset} в таблице контрактов нет: какой контракт "
                    "действующий, программа узнает у биржи при загрузке истории "
                    "квартального фьючерса.",
        )
    try:
        row = current_contract(store, today=today, asset=asset)
    except ContractError as error:
        return ContractNotice(
            configured=configured, trouble=_sentence(str(error)), urgent=True
        )
    return ContractNotice(
        configured=configured,
        current=row.symbol,
        current_since=row.active_from,
        last_trade_day=row.last_trade_day,
    )


def not_started(symbol: str, rows: list[ContractRow]) -> str:
    """Почему загрузка по контракту не начнётся — либо пусто, если начнётся.

    Контракт без начала периода — ещё не стал ближним (либо первый в цепочке):
    хвост до рубежа решение 0061 качать запрещает, значит, качать нечего.
    """
    row = next((one for one in rows if one.symbol == symbol), None)
    if row is not None and row.active_from is not None:
        return ""
    current = next((one.symbol for one in rows if one.current), "")
    also = (
        f" Действующий сейчас по данным биржи — {current}: поставьте его "
        "в поле «Инструмент» и загрузите историю по нему."
        if current and current != symbol else ""
    )
    return (
        f"У {symbol} по дневным объёмам биржи ещё нет рубежа — он не стал "
        "ближним контрактом, либо уже ушёл из цепочки. Минуты до рубежа "
        f"не качаются (решение 0061), поэтому загружать нечего.{also}"
    )


def load_lead(row: ContractRow, warmup_bars: int, today: date) -> str:
    """Что именно начали грузить — строкой в журнал, до обращения к бирже."""
    assert row.active_from is not None  # проверено `not_started`
    return (
        f"{row.symbol}: с рубежа {row.active_from:%d.%m.%Y} по {today:%d.%m.%Y} МСК "
        f"и {warmup_bars} баров прогрева средней перед рубежом. Минуты "
        "до рубежа, кроме прогрева, не качаются: тогда ближним был другой "
        "контракт. Свечи берутся у биржи; токен брокера не нужен."
    )


def load_outcome(load: ContractLoad, seconds: float) -> HistoryLoadOutcome:
    """Итог загрузки контракта → фразы для окна. Беду называет слой данных."""
    trouble = load.problem
    fetched = sum(report.fetched for report in load.reports)
    inserted = sum(report.inserted for report in load.reports)
    updated = sum(report.updated for report in load.reports)
    kept = sum(report.kept for report in load.reports)
    detail = (
        f"Пришло с биржи {fetched} свечей: {inserted} новых, {updated} обновлено"
        + (f", {kept} отвергнуто" if kept else "")
        + f". Период контракта — с {load.active_from:%d.%m.%Y}; для прогрева "
        f"средней нужно {load.warmup_bars} закрытых баров, перед рубежом их "
        f"{load.warm_bars} (с {load.since:%d.%m.%Y}). Заняло {seconds:.0f} с."
    )
    return HistoryLoadOutcome(
        symbol=load.symbol,
        ok=trouble is None,
        headline=(
            f"История {load.symbol} загружена с рубежа"
            if trouble is None else f"История {load.symbol} загружена не вся"
        ),
        detail=detail,
        trouble=trouble or "",
    )


@dataclass(frozen=True, slots=True)
class ChartContext:
    """Чем дополнить показ одного контракта: прежние контракты и стыки.

    `earlier` — свечи прежних контрактов, каждый в своём периоде, по порядку.
    `since` — начало периода показанного контракта: раньше него его свечи
    на графике не показываются (там жил другой контракт). `None` — таблица
    про этот контракт ничего не знает, показ как прежде.
    """

    earlier: tuple[MarketCandle, ...] = ()
    seams: tuple[tuple[datetime | None, str, str], ...] = ()
    since: datetime | None = None


async def chart_context(
    worker: MarketWorker,
    symbol: str,
    timeframe: Timeframe,
    since: datetime | None,
    until: datetime,
) -> ChartContext:
    """Куски показа по таблице контрактов — только чтение, без прогона.

    Показываются куски **до** периода контракта из настроек. Куски после
    него не показываются: робот настроен на этот контракт, и дорисовать
    к нему следующий значило бы показать не то, чем он торгует; расхождение
    называет плашка «действующий контракт».
    """
    first = since.astimezone(MSK).date() if since is not None else _EARLIEST
    last = until.astimezone(MSK).date()
    try:
        asset = asset_of(symbol)
        legs = await worker.call(lambda store: pieces(store, first, last, asset=asset))
    except ContractError:
        return ChartContext()
    mine = next((index for index, leg in enumerate(legs) if leg.symbol == symbol), None)
    if mine is None:
        return ChartContext()
    shown = legs[: mine + 1]
    earlier: list[MarketCandle] = []
    seams: list[tuple[datetime | None, str, str]] = []
    previous = ""
    for leg in shown[:-1]:
        bars = await worker.bars(
            leg.symbol, timeframe,
            since=_midnight(leg.since), until=_midnight(leg.until + timedelta(days=1)),
            drop_unsettled=True,
        )
        if bars:
            if previous:
                seams.append((bars[0].time, leg.symbol, previous))
            earlier.extend(bars)
            previous = leg.symbol
    if previous:
        # Время стыка с показанным контрактом — его первая показанная свеча;
        # её знает только тот, кто режет его ряд (`split_shown`).
        seams.append((None, symbol, previous))
    return ChartContext(
        earlier=tuple(earlier),
        seams=tuple(seams),
        since=_midnight(shown[-1].since),
    )


def split_shown(
    context: ChartContext, candles: list[MarketCandle]
) -> tuple[list[MarketCandle], tuple[ContractSeam, ...]]:
    """Свечи для графика и стыки: прежние контракты, затем свой с его рубежа.

    Свечи своего контракта до его рубежа (прогрев, хвост) на график
    не идут: на этом отрезке показан контракт, который тогда был ближним.
    Если после обрезки не осталось ничего — показ как прежде, без обрезки:
    пустой график хуже, чем лишний день.
    """
    own = candles
    if context.since is not None:
        cut = [candle for candle in candles if candle.time >= context.since]
        own = cut or candles
    seams = tuple(
        ContractSeam(
            time=moment if moment is not None else own[0].time,
            symbol=name,
            previous=before,
        )
        for moment, name, before in context.seams
        if moment is not None or own
    )
    return [*context.earlier, *own], seams


def trim_before(since: datetime | None, data: ChartData) -> ChartData:
    """Снять с графика метки прогона раньше рубежа показанного контракта.

    До рубежа на графике свечи другого контракта; метка сделки, линия
    средней или затенение оттуда легли бы на чужие цены. Прогон при этом
    не меняется — ни сделки, ни журнал: это показ, а не счёт.
    """
    if since is None:
        return data
    return replace(
        data,
        average=tuple(point for point in data.average if point.time >= since),
        markers=tuple(marker for marker in data.markers if marker.time >= since),
        paths=tuple(path for path in data.paths if path.entry_time >= since),
        shades=tuple(
            replace(shade, start=max(shade.start, since))
            for shade in data.shades if shade.end > since
        ),
    )


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time(0, 0), MSK)


def _sentence(text: str) -> str:
    """Фраза слоя данных → предложение: с заглавной и с точкой."""
    text = text.strip()
    if not text:
        return text
    text = text[0].upper() + text[1:]
    return text if text.endswith(".") else text + "."
