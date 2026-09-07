"""Загрузка глубокой истории по требованию человека — вход в `sync_minutes`.

Зачем отдельный модуль
----------------------
Механизм загрузки был, входа не было: `sync_minutes` звался только из тестов.
Владелец счёта, сменивший инструмент в настройках, видел пустой график и
ничего не мог с этим сделать — своих свечей у нового инструмента в базе нет.
Здесь собрано то, чего не хватало между «есть `sync_minutes`» и «человек
набрал команду, получил свечи и увидел график»:

* **спросить, существует ли инструмент, до того как качать.** Опечатка в коде
  не даёт `404`: ISS отвечает `200` с пустым `data` — проверено живым запросом
  05.09.2026, `MXZZZ9` вернул `{"candles": {"columns": [...], "data": []}}`.
  По самой выдаче свечей опечатка неотличима от выходных. Отличает её
  `candleborders`: у несуществующего кода блок `borders` **пуст**, у живого —
  строка с интервалом 1 и настоящими границами минутной истории;
* **честная глубина.** Тот же ответ говорит, с какой даты минутки вообще есть.
  Запрос за пределы этой границы — это запрос в пустоту, и лучше сказать
  об этом до загрузки, а не после;
* **прогрев.** Средняя на свежем ряду начинается с нуля. Ряд, в котором баров
  меньше, чем нужно средней, — это не «мало данных», это другие сделки
  в первый же день (замер прототипа: тот же набор данных с более ранней
  границы даёт другой список сделок целиком).

Чего модуль **не** делает
-------------------------
Не решает, с какой даты правильно начинать ряд: это вопрос к владельцу счёта,
а не к программе. Умолчание здесь — только умолчание, и оно названо вслух
в `DEFAULT_DEPTH_DAYS`.

Не склеивает инструменты. Ряд одного кода кончается там, где кончается сам
инструмент, и следующим не продолжается — это конец инструмента, а не разрыв
данных (открытый вопрос №8).

Не пишет ничего мимо `sync_minutes`: отчёт о загрузке, отметки загруженных
дней и правило конфликта источников остаются там, где были. Источник —
`Source.ISS`, ранг 2, выше потока брокера: биржа при конфликте побеждает.
Повторный запуск поэтому не плодит дублей: день, за который уже спрашивали,
в запрос не попадёт вовсе, а минутка того же ранга ляжет поверх себя же.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from market.candles import M5, MSK, Timeframe
from market.iss import (
    MINUTE_INTERVAL,
    CandleBorder,
    FetchResult,
    IssClient,
    IssHttpError,
    IssPagingError,
    IssPayloadError,
    IssTransportError,
    Market,
)
from market.reports import LoadReport
from market.storage import CandleStore, Source
from market.sync import sync_minutes

__all__ = [
    "DEFAULT_DEPTH_DAYS",
    "DEFAULT_WARMUP_BARS",
    "WARMUP_PERIOD",
    "WARMUP_RESIDUAL",
    "HistoryOutcome",
    "HistoryRequest",
    "load_history",
    "minute_border",
    "warmup_bars",
]

#: Сколько календарных дней грузится, если человек не назвал глубину.
#:
#: Девяносто — число, названное владельцем счёта 05.09.2026: «глубину
#: в настройках и по умолчанию 90 дней». Снизу оно заведомо больше прогрева
#: средней (`warmup_bars`) даже при неполных сессиях; сверху — примерно жизнь
#: одного фьючерсного контракта в ближней позиции (65…77 торговых дней, замер
#: 05.09.2026), то есть столько, сколько по одному коду вообще бывает плотных
#: минуток (DOMAIN.md §6).
#:
#: ⚠️ **Число повторено, а не импортировано** — тот же приём и та же причина,
#: что у `WARMUP_PERIOD` ниже: `market/` не зависит ни от одного слоя проекта
#: (ARCHITECTURE.md §2), а второй его владелец — поле окна
#: `ui.models.Settings.history_depth_days`. Связь двух чисел держит тест
#: `tests/test_market_history.py`: разойдутся — прогон покраснеет, а не
#: промолчит. До 06.09.2026 здесь стояло 30 при 90 в настройках показа
#: и 90 в диалоге прогона — три числа глубины, не совпадавшие ни одно
#: с другим (`D-068`).
#:
#: ⚠️ **Дату начала ряда стоит выбрать один раз и больше не менять.**
#: Замер прототипа: тот же набор данных, поданный с более ранней границы,
#: даёт на общем участке другой список сделок целиком. Меняя границу
#: от запуска к запуску, сверить прогон на истории с живым роботом нечем.
DEFAULT_DEPTH_DAYS = 90

#: Какая доля начального значения средней ещё считается «не влияет».
#: Тысячная — порядок, при котором сдвиг средней меньше шага цены на любой
#: разумной цене инструмента, то есть до решения он не доходит.
WARMUP_RESIDUAL = 0.001

#: Период средней, под который считается прогрев.
#:
#: ⚠️ Число принадлежит модулю стратегии (`strategies.ema_reverse.DEFAULT_PERIOD`),
#: и оно здесь **повторено, а не импортировано**: `market/` не зависит ни от
#: одного слоя проекта (ARCHITECTURE.md §2), и сборка тоже не имеет права брать
#: его у торгового слоя — на этом стоит сторож `tests/test_app_boundaries.py`.
#: Связь двух чисел держит тест `test_market_history.py`: разойдутся — прогон
#: покраснеет, а не промолчит.
WARMUP_PERIOD = 15


def warmup_bars(period: int, *, residual: float = WARMUP_RESIDUAL) -> int:
    """Сколько баров нужно средней, чтобы её значение не зависело от начала ряда.

    Два разных требования, и большее из них побеждает.

    **Сигнал вообще возможен** начиная с `period + 1` бара: условие прогрева
    в модуле стратегии — «свечей больше периода» (`strategies.ema_reverse`,
    `_is_warm`). При периоде 15 это 16 баров, и до них решений нет никаких.

    **Значение средней перестаёт зависеть от начала ряда** позже. У
    экспоненциальной средней вес новой свечи `2/(период+1)`; при периоде 15
    это ровно 1/8, и от начальной величины через `n` баров остаётся
    `(1 - 1/8)^n`. Тысячная доля достигается на 52-м баре: `0.875 ** 52`
    равно 0.00097, а `0.875 ** 51` — 0.00111.

    Пятьдесят две пятиминутки — это 260 минут, меньше трети торговой сессии
    срочного рынка. То есть требование скромное: оно ловит не «мало истории»,
    а «истории почти нет» — новый контракт, у которого в базе один огрызок дня.

    :raises ValueError: период меньше единицы либо доля вне (0, 1).
    """
    if period < 1:
        raise ValueError(f"период средней не может быть меньше 1: {period}")
    if not 0.0 < residual < 1.0:
        raise ValueError(f"доля остатка должна лежать строго между 0 и 1: {residual}")
    factor = 2.0 / (period + 1)
    if factor >= 1.0:
        # Период 1: вес новой свечи — единица, памяти у средней нет вовсе,
        # и логарифм нуля здесь не считается, а не «почти ноль».
        return period + 1
    decayed = math.ceil(math.log(residual) / math.log(1.0 - factor))
    return max(decayed, period + 1)


#: Прогрев под умолчания продукта: период 15, пятиминутки. Пятьдесят два бара.
DEFAULT_WARMUP_BARS = warmup_bars(WARMUP_PERIOD)


@dataclass(frozen=True, slots=True)
class HistoryRequest:
    """Что человек попросил загрузить.

    `since` = `None` означает «с начала минутной истории, которую заявляет
    биржа». Без ответа биржи такой запрос выполнить нечем — и это отказ,
    а не догадка: подставить сюда «ну, год назад» значит молча загрузить
    не то, что просили.
    """

    symbol: str
    market: Market
    until: date
    since: date | None = None
    #: Сколько закрытых баров нужно средней. Умолчание — под период 15
    #: (`WARMUP_PERIOD`); вызывающий вправе назвать своё.
    warmup: int = DEFAULT_WARMUP_BARS
    timeframe: Timeframe = M5
    #: Момент, относительно которого решается, какие дни уже закончились.
    #: `None` — сейчас. Задаётся в тестах, чтобы прогон не зависел от даты.
    now: datetime | None = None


@dataclass(frozen=True, slots=True)
class HistoryOutcome:
    """Что вышло из одной загрузки — вместе с разбором, что пошло не так."""

    request: HistoryRequest
    #: Инструмент бирже известен: в `candleborders` есть строка про минутки.
    known: bool
    #: Что биржа заявляет о минутной истории. `None` — не знает либо не ответила.
    border: CandleBorder | None
    #: Почему не удалось спросить про глубину. Пусто — спросили и получили ответ.
    #: Непустое НЕ означает «инструмента нет»: загрузка при этом идёт как обычно.
    border_error: str
    #: Отчёт `sync_minutes`. `None` — до загрузки дело не дошло.
    report: LoadReport | None
    #: Отрезок, который в итоге запрашивали (после подрезки под глубину биржи).
    since: date | None
    until: date | None
    #: Сколько **закрытых** баров рабочего таймфрейма собирается из того,
    #: что теперь лежит в базе за этот отрезок.
    bars: int
    seconds: float

    @property
    def symbol(self) -> str:
        """Код инструмента — тот, что просили."""
        return self.request.symbol

    @property
    def warm(self) -> bool:
        """Баров хватает, чтобы средняя не зависела от начала ряда."""
        return self.bars >= self.request.warmup

    @property
    def clamped(self) -> bool:
        """Запрошенный отрезок пришлось подрезать под глубину биржи."""
        if self.border is None or self.since is None or self.until is None:
            return False
        if self.request.since is None or self.since > self.request.since:
            return True
        return self.until < self.request.until

    def problem(self) -> str | None:
        """Что не так — человеческой фразой. `None` — всё в порядке.

        Цепочка обязанностей: звенья перечислены в `_TROUBLES`, **порядок
        в списке и есть правило**. Первое сработавшее звено отвечает, дальше
        не идём. Порядок не произвольный: неизвестный инструмент обязан
        объясняться раньше, чем «свечей не пришло», иначе человек с опечаткой
        в коде получит совет про выходные дни.
        """
        for trouble in _TROUBLES:
            said = trouble(self)
            if said is not None:
                return said
        return None


def _unknown_instrument(outcome: HistoryOutcome) -> str | None:
    """Биржа не знает такого кода на этом рынке."""
    if outcome.known:
        return None
    request = outcome.request
    return (
        f"Биржа не знает инструмента «{request.symbol}» на рынке "
        f"«{request.market.title}»: минутной истории по нему нет вовсе.\n"
        "Так выглядят две разные беды, и обе лечатся по-разному:\n"
        "  • опечатка в коде контракта — сверьте код в терминале брокера "
        "или на сайте МосБиржи;\n"
        f"  • код с другого рынка — сейчас спрашивали «{request.market.title}», "
        "акции грузятся ключом --fetch-market shares.\n"
        "Ничего не загружено, база не тронута."
    )


def _no_depth_to_start_from(outcome: HistoryOutcome) -> str | None:
    """Просили «всю историю», а биржа не сказала, с какой даты она есть."""
    if outcome.request.since is not None or outcome.border is not None:
        return None
    return (
        f"Не удалось спросить биржу, с какой даты есть свечи по "
        f"«{outcome.symbol}»: {outcome.border_error}.\n"
        "Без этой даты «вся история» — это запрос неизвестно откуда, "
        "и он не отправлен.\n"
        "Назовите глубину в днях или дату начала: в окне это поле "
        "«Скачивать историю за, дней» в настройках, из консоли — ключи "
        "--fetch-days 30 и --fetch-since 01.08.2026."
    )


def _outside_history(outcome: HistoryOutcome) -> str | None:
    """Запрошенный отрезок целиком за пределами истории инструмента."""
    if outcome.report is not None or outcome.border is None:
        return None
    return (
        f"Запрошенный период целиком вне минутной истории «{outcome.symbol}».\n"
        f"Биржа отдаёт свечи с {outcome.border.begin:%d.%m.%Y} "
        f"по {outcome.border.end:%d.%m.%Y}.\n"
        "Ничего не загружено, база не тронута."
    )


def _nothing_came(outcome: HistoryOutcome) -> str | None:
    """Инструмент есть, период в пределах истории, а свечей ноль."""
    report = outcome.report
    if report is None or report.fetched:
        return None
    if outcome.since is None or outcome.until is None:  # до загрузки не дошло
        return None
    depth = ""
    if outcome.border is not None:
        depth = (
            f"Свечи по нему биржа отдаёт с {outcome.border.begin:%d.%m.%Y} "
            f"по {outcome.border.end:%d.%m.%Y}.\n"
        )
    return (
        f"За период {outcome.since:%d.%m.%Y} … {outcome.until:%d.%m.%Y} "
        f"биржа не отдала по «{outcome.symbol}» ни одной свечи.\n"
        f"{depth}"
        "Инструмент бирже известен — значит дело не в опечатке. Похоже на "
        "выходные и праздники целиком либо на дни, когда контракт ещё "
        "не торговался.\n"
        "Возьмите период пошире: в окне это поле «Скачивать историю за, "
        "дней» в настройках, из консоли — ключ --fetch-days 30."
    )


def _cold_average(outcome: HistoryOutcome) -> str | None:
    """Данные загрузились, но средней их не хватит."""
    if outcome.warm:
        return None
    return (
        f"Загружено, но для прогрева средней этого мало: собирается "
        f"{outcome.bars} закрытых баров {outcome.request.timeframe.name}, "
        f"а нужно не меньше {outcome.request.warmup}.\n"
        "На таком ряду средняя ещё помнит своё начальное значение, и первые "
        "сделки будут не те, что на полной истории.\n"
        "Возьмите период глубже: в окне это поле «Скачивать историю за, "
        "дней» в настройках, из консоли — ключ --fetch-days 30 или больше."
    )


#: Звенья разбора «что не так». Порядок — правило, а не оформление: см.
#: `HistoryOutcome.problem`. Отдельным списком, а не цепочкой `if` в методе,
#: чтобы порядок можно было проверить тестом, а не вычитать глазами.
_TROUBLES: tuple[Callable[[HistoryOutcome], str | None], ...] = (
    _unknown_instrument,
    _no_depth_to_start_from,
    _outside_history,
    _nothing_came,
    _cold_average,
)


#: Отказы справки о глубине, которые загрузку не отменяют. Список поимённый,
#: а не `IssError` целиком: в общего предка входит `IssStopped` — «человек
#: закрыл программу», — и глотать его значит продолжить качать после просьбы
#: остановиться.
_DEPTH_TROUBLES = (IssTransportError, IssHttpError, IssPayloadError, IssPagingError)


def minute_border(
    client: IssClient, symbol: str, *, market: Market
) -> tuple[CandleBorder | None, str]:
    """Что биржа заявляет о **минутной** истории инструмента.

    Возвращает границу и причину, по которой её нет. Различать обязательно:

    * граница есть — инструмент живой, известна честная глубина;
    * границы нет, причина пуста — сервер ответил, и в ответе пусто.
      Такого кода на этом рынке **нет**;
    * границы нет, причина названа — спросить не удалось (сеть, `5xx`).
      Это не приговор инструменту: загрузка идёт как обычно, просто без
      подрезки под глубину и без раннего отказа на опечатке.

    Третий случай выделен намеренно. Справка о глубине — удобство, и делать
    её обязательным условием загрузки значит поставить всю догрузку истории
    в зависимость от доступности ещё одной ручки ISS.
    """
    try:
        borders = client.borders(symbol, market=market)
    except _DEPTH_TROUBLES as error:
        return None, f"{type(error).__name__}: {error}"
    for border in borders:
        if border.interval == MINUTE_INTERVAL:
            return border, ""
    return None, ""


def load_history(
    store: CandleStore,
    client: IssClient,
    request: HistoryRequest,
    *,
    progress: Callable[[FetchResult], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> HistoryOutcome:
    """Спросить глубину, загрузить период, посчитать бары. Синхронно.

    Порядок шагов — не оформление, а экономия и честность отказа:

    1. **сначала справка о глубине.** Один дешёвый запрос. На опечатке в коде
       он отменяет тридцать дней бесполезной загрузки, а человеку даёт ответ
       про опечатку вместо ответа про выходные;
    2. **подрезка под глубину.** Просить у биржи то, чего у неё нет, незачем:
       такие дни отметятся «спрашивали, пусто» и больше не перезапросятся;
    3. **загрузка** — обычным `sync_minutes`, со своим отчётом в журнал;
    4. **пересчёт баров по базе, а не по ответу.** Считается то, что теперь
       лежит в хранилище: ответ мог наполовину лечь поверх уже имеющегося,
       а прогрев считается по ряду, а не по приросту.

    Загрузка занимает поток вызывающего целиком: `IssClient` синхронный.
    Из окна и движка это зовётся через `market.worker.MarketWorker`;
    из командной строки цикла событий нет вовсе, и поток здесь один.
    """
    started = clock()
    border, border_error = minute_border(client, request.symbol, market=request.market)

    def outcome(
        report: LoadReport | None, since: date | None, until: date | None, bars: int
    ) -> HistoryOutcome:
        return HistoryOutcome(
            request=request,
            # «Не знаем» — только когда спросили и получили пустой ответ.
            # Не сумели спросить — это про связь, а не про инструмент.
            known=border is not None or bool(border_error),
            border=border, border_error=border_error, report=report,
            since=since, until=until, bars=bars, seconds=clock() - started,
        )

    nothing = outcome(None, None, None, 0)
    if border is None and not border_error:
        return nothing            # спросили — такого кода на этом рынке нет
    since = request.since
    if since is None:
        if border is None:
            return nothing        # «вся история» без известной даты начала
        since = border.begin.date()
    until = request.until
    if border is not None:
        since, until = max(since, border.begin.date()), min(until, border.end.date())
    if until < since:
        return nothing            # период целиком вне истории инструмента

    report = sync_minutes(
        store, client, request.symbol, market=request.market,
        since=since, until=until, source=Source.ISS, now=request.now,
        progress=progress,
    )
    return outcome(report, since, until, _closed_bars(store, request, since, until))


def _closed_bars(
    store: CandleStore, request: HistoryRequest, since: date, until: date
) -> int:
    """Сколько закрытых баров рабочего таймфрейма собирается за отрезок.

    Незакрытые отброшены намеренно: движок в решении их не использует,
    и считать их за прогрев значит обещать прогрев, которого нет.
    """
    bars = store.bars(
        request.symbol,
        request.timeframe,
        since=datetime.combine(since, datetime.min.time(), MSK),
        until=datetime.combine(until + timedelta(days=1), datetime.min.time(), MSK),
        drop_unsettled=True,
    )
    return len(bars)
