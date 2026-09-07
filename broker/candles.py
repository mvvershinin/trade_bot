"""Исторические свечи брокера: период → список свечей, кусками по 1440 баров.

Зачем это здесь
---------------
Живой поток (`broker/stream.py`) пишет только вперёд: канал «последняя свеча»
шлёт текущую минуту и ничего больше. Всё, что прошло мимо за время обрыва
связи, из него не достать никогда. Дыра, оставшаяся за обрывом, закрывается
только этим запросом — другого способа у программы нет (решение 0022: машина
рабочая, а не постоянно включённая, значит убрать саму причину обрывов нечем).

Что делает и чего не делает
---------------------------
Делает: берёт у брокера свечи за названный период и отдаёт их такими, какими
прислал брокер. Разбивает период на куски, потому что брокер отдаёт не больше
1440 баров за запрос.

Не делает: не ищет дыр, не решает, что и когда догружать, ничего не пишет
в базу и не склеивает историю с живым потоком. Это политика источника, ей
место в `app/` и `market/`; здесь только разговор с брокером
(`ARCHITECTURE.md` §2).

Как этим пользоваться
---------------------
::

    from broker.candles import History, HistoryIncomplete

    try:
        bars = await History(session).candles(
            ticker="MXU6", class_code="SPBFUT", start=..., end=...
        )
    except HistoryIncomplete as broken:
        bars = broken.candles      # то, что успело прийти; остальное — не пришло

Времена на входе — **с поясом**, любым: наивные отвергаются. Свечи на выходе
идут по возрастанию времени открытия, без повторов.

Часовой пояс
------------
Брокер и принимает, и отдаёт **UTC** (`.docs/broker-api/01-special-info.md`:
«все поля с типом dateTime передаются в формате UTC»), примеры на странице
свечей — со суффиксом `Z`. Наружу отдаётся момент **с поясом**, ровно как
у потока (`broker/stream.CandleSnapshot.opened_at`), перевод в МСК —
забота `app/`, где для этого есть `market.candles.ensure_msk`.

Наивное время не приводится, а отвергается — и на входе, и на выходе.
Момент без пояса, истолкованный как местный, сдвинул бы свечу на три часа
молча: ничего не упадёт, свечи просто лягут не туда.

Документация
------------
Выписка — `.docs/broker-api/07-historical-candles.md`. Источник адреса
и параметров — **официальный PDF** `https://cdn.bcs.ru/static/bcs/files/trade-api-docs.pdf`,
раздел «Исторические свечи»: контейнер `bars`, поля `time` (время открытия
свечи), `open`, `high`, `low`, `close`, `volume`; параметры `classCode`,
`ticker`, `startDate`, `endDate`, `timeFrame` (`M1 M5 M15 M30 H1 H4 D W MN`),
время ISO 8601 с `Z`; потолок 1440 баров. Одностраничная документация
на сайте таблицу с хостом и схему ответа наружу не отдаёт.

⚠️ 05.09.2026: имя сервиса в адресе было неверным
--------------------------------------------------
Первый живой запуск у владельца счёта вернул `404` — страницу nginx брокера,
а не отказ его сервиса, потому что в `CANDLES_PATH` стояло имя
`trade-api-information-service` (сервис справочника инструментов). Верное
имя — `trade-api-market-data-connector`, тот же сервис, на котором живёт
вебсокет котировок. Исправлено, разбор — в комментарии к `CANDLES_PATH`.

⛔ **Живьём верный адрес не проверялся: токена в разработке нет.** То, что
метод отвечает, — утверждение PDF, а не наш замер. Первую живую проверку
делает владелец счёта.

⚠️ **404 у этого метода двузначен.** По официальному PDF `404 NOT_FOUND` —
штатный ответ «данные не найдены» (например, за период без торгов), и тот же
404 приходит на неверный адрес. Разводит их `session._wrong_address` по телу
ответа; повторно за сеанс по неверному адресу слой не ходит.

⚠️ Живым запросом к брокеру этот модуль не проверялся ни разу. Не проверено:

* **в чём измерен `volume`** — в контрактах, как у MOEX ISS, или в обороте
  валюты, как в потоке WebSocket того же брокера (замер 04.09.2026: поток
  даёт цену × количество, разрыв с ISS на пять порядков). Поэтому поле
  названо `volume`, как у брокера, а не `turnover`, как в потоке: имя не
  утверждает того, чего мы не знаем;
* приходит ли при пустом периоде `"bars": []` или `"bars": null` — второе
  здесь считается сменой формата и отвергается вслух;
* включает ли брокер границу `endDate` в период. Разбивка написана так,
  чтобы ответ на этот вопрос ни на что не влиял (см. `split_period`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final, Mapping

from broker.errors import BrokerError, UnexpectedAnswer
from broker.parsing import aware_moment, number, pick, text
from broker.session import CANDLES_PATH, BrokerSession

#: Потолок брокера: больше баров за один запрос он не отдаёт. Число названо
#: и на странице документации, и в официальном PDF.
#:
#: ⚠️ Имя отказа при превышении — `CANDLE_LIMIT_EXCEEDED` — взято
#: с одностраничной документации сайта; в официальном PDF оно **не названо**
#: (там пять кодов: `UNAUTHORIZED`, `NOT_FOUND`, `BAD_REQUEST`,
#: `VALIDATION_ERROR`, `INTERNAL_SERVER_ERROR`) и живым запросом
#: не проверялось. На этом имени в коде ничего не держится: разбивка
#: `split_period` не даёт превысить потолок, а отказ, если он всё же придёт,
#: разберётся общей таблицей `errors.from_status` по HTTP-коду. Имя оставлено
#: здесь как след источника, а не как условие.
MAX_BARS: Final[int] = 1440

#: Длительность одного бара по каждому значению `timeFrame`. Таблица, а не
#: цепочка `if`: по ней же проверяется, что таймфрейм вообще существует,
#: и она же задаёт размер куска при разбивке.
#:
#: ⚠️ `MN` — месяц, а он не одинаковой длины. Взято 28 суток: это заведомо
#: **меньше** любого месяца, значит кусок получится короче нужного, и предел
#: в 1440 баров будет соблюдён с запасом. Ошибка в другую сторону стоила бы
#: отказа брокера, ошибка в эту — лишнего запроса раз в сто двадцать лет.
STEPS: Final[Mapping[str, timedelta]] = {
    "M1": timedelta(minutes=1),
    "M5": timedelta(minutes=5),
    "M15": timedelta(minutes=15),
    "M30": timedelta(minutes=30),
    "H1": timedelta(hours=1),
    "H4": timedelta(hours=4),
    "D": timedelta(days=1),
    "W": timedelta(weeks=1),
    "MN": timedelta(days=28),
}

#: Таймфрейм по умолчанию: программа держит минутную базу и собирает из неё
#: свечи нужного размера сама (`market/`), поэтому догружает тоже минутки.
MINUTE: Final[str] = "M1"

#: Сколько запросов подряд слой готов сделать за одну догрузку.
#:
#: Это не предел брокера, а предохранитель от нашей же ошибки: период
#: «с 2015 года» на минутках — это две с половиной тысячи запросов подряд,
#: и упрёмся мы не в свечи, а в ограничение частоты (10 RPS, `29-restrictions.md`)
#: в торговое время. Шестьдесят четыре куска минуток — это 64 суток, для
#: догрузки после обрыва запас многократный. Первичная заливка истории идёт
#: не отсюда, а из MOEX ISS (`market/`), где потолка на запрос нет.
#:
#: Проверка делается **до** первого запроса: половина ряда хуже, чем отказ.
MAX_REQUESTS: Final[int] = 64


@dataclass(frozen=True, slots=True)
class HistoricalCandle:
    """Одна историческая свеча — как её прислал брокер.

    `opened_at` — **время открытия** свечи (документация называет поле `time`
    и подписывает его «Время открытия свечи»), с часовым поясом. То же, что
    у `CandleSnapshot.opened_at` из потока, и то же, что `ts` в нашей базе.
    ⚠️ У движка торговое окно считается по времени **закрытия**
    (`ARCHITECTURE.md` §2) — перевод делает не этот слой.

    `volume` — то, что брокер прислал в поле `volume`. В чём оно измерено,
    живым запросом не проверено; см. шапку модуля.

    ⚠️ Последняя свеча ряда может оказаться **незаконченной**: если период
    дотянут до текущего момента, брокер отдаст и текущую, ещё растущую минуту.
    Отличить её по содержимому нельзя — признака «свеча закрыта» в API нет
    вообще, ни здесь, ни в потоке. Решает вызывающий, по часам.
    """

    ticker: str
    class_code: str
    timeframe: str
    opened_at: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class HistoryIncomplete(BrokerError):
    """Догрузка оборвалась посреди периода: часть свечей есть, остальных нет.

    Отдельный отказ, а не молчаливый возврат половины ряда. Полученное лежит
    в `candles`, до какого момента ряд доведён — в `covered_until`, причина
    обрыва — в `cause`. Вызывающий вправе записать полученное и повторить
    остаток, но сделать это он должен **осознанно**: половина ряда, отданная
    как целый, — это дыра в данных, которую никто уже не заметит.
    """

    def __init__(
        self,
        candles: tuple[HistoricalCandle, ...],
        *,
        cause: BrokerError,
    ) -> None:
        self.candles = candles
        self.cause = cause
        self.covered_until = candles[-1].opened_at if candles else None
        self.retryable = cause.retryable
        covered = (
            self.covered_until.isoformat() if self.covered_until is not None else "ничего"
        )
        super().__init__(
            "Догрузка истории оборвалась на середине: часть свечей получена, "
            "остальные нет. Недостающие программа не выдумывает — пока догрузку "
            f"не повторят, в данных останется пропуск. Причина: {cause.human}",
            f"догрузка свечей: получено {len(candles)} шт. по {covered}; "
            f"причина остановки: {cause.technical or cause.human}",
            # Тело ответа переносится из причины: обёртка не должна его терять,
            # иначе технический лог по обрыву посреди догрузки останется без
            # единственной подробности, которую прислал брокер.
            details=cause.details,
        )


def step_of(timeframe: str) -> timedelta:
    """Длительность одного бара. Незнакомый таймфрейм — отказ, а не догадка.

    Проверять приходится здесь: без длительности бара нечем считать разбивку
    по потолку в 1440 баров. Опечатка вида `1m` вместо `M1` иначе доехала бы
    до брокера и вернулась отказом 400 уже в торговое время.
    """
    step = STEPS.get(timeframe)
    if step is None:
        known = ", ".join(STEPS)
        raise ValueError(f"неизвестный таймфрейм {timeframe!r}; брокер знает: {known}")
    return step


def split_period(
    start: datetime, end: datetime, *, timeframe: str = MINUTE
) -> tuple[tuple[datetime, datetime], ...]:
    """Разбить период на куски, в каждом из которых не больше 1440 баров.

    Длина куска — ``step * (MAX_BARS - 1)``, а не ``step * MAX_BARS``: в отрезке
    с **обеими** включёнными границами такой длины ровно 1440 баров, а не 1441.
    Так разбивка остаётся верной независимо от того, включает брокер границу
    `endDate` в период или нет, — а этого мы живым запросом не проверяли.

    Соседние куски стыкуются **внахлёст в один момент**: следующий начинается
    ровно там, где кончился предыдущий. Иначе бар, попавший между границами
    кусков (сетка биржи не обязана совпадать с нашими границами), потерялся бы
    молча — то есть задача про дыры в данных сама бы дыру и сделала.
    Совпавший бар отсеется при склейке, дубликата в ответе не будет.
    """
    step = step_of(timeframe)
    if end < start:
        raise ValueError(
            f"конец периода {end.isoformat()} раньше начала {start.isoformat()}"
        )

    span = step * (MAX_BARS - 1)
    chunks: list[tuple[datetime, datetime]] = []
    chunk_start = start
    while True:
        chunk_end = chunk_start + span
        if chunk_end >= end:
            chunks.append((chunk_start, end))
            return tuple(chunks)
        chunks.append((chunk_start, chunk_end))
        chunk_start = chunk_end


def parse_candles(
    payload: object, *, ticker: str, class_code: str, timeframe: str
) -> tuple[HistoricalCandle, ...]:
    """Ответ брокера → свечи. Недостающее поле — отказ вслух, а не ноль.

    Молчаливая подстановка нуля здесь недопустима ровно по той же причине,
    что и в потоке: свеча с нулевым `low` выглядит как касание любого уровня
    выхода, и тейк «сработал» бы там, где сделки не было.

    Пустой список баров отказом **не** считается: выходной день, пауза
    в торгах и период до появления инструмента — это законное «данных нет».
    А вот отсутствие самого поля `bars` — считается: это смена формата.
    """
    if not isinstance(payload, Mapping):
        raise UnexpectedAnswer(
            "ответ на запрос свечей — не объект",
            "свечи: тело ответа не является объектом JSON",
        )

    answered = text(pick(payload, ("ticker",)))
    if answered is not None and answered.upper() != ticker.strip().upper():
        raise UnexpectedAnswer(
            "брокер прислал свечи другого инструмента",
            f"свечи: запрошен {ticker}, в ответе {answered}",
        )

    bars = pick(payload, ("bars",))
    if not isinstance(bars, list):
        known = ", ".join(sorted(str(key) for key in payload)) or "пусто"
        raise UnexpectedAnswer(
            "в ответе на запрос свечей нет списка баров",
            f"свечи: поля `bars` нет или оно не список; пришли поля: {known}",
        )

    return tuple(
        _candle(bar, ticker=ticker, class_code=class_code, timeframe=timeframe)
        for bar in bars
    )


#: Поля бара, которые обязаны прийти числом. Перечислены один раз: список
#: недостающих полей строится по этой же таблице, а не вторым перечислением
#: руками — иначе добавленное поле молча выпадет из проверки.
_NUMBERS: Final[tuple[str, ...]] = ("open", "high", "low", "close", "volume")


def _candle(
    bar: object, *, ticker: str, class_code: str, timeframe: str
) -> HistoricalCandle:
    """Один бар ответа → свеча. Всё, чего не хватает, называется поимённо."""
    if not isinstance(bar, Mapping):
        raise UnexpectedAnswer(
            "среди баров пришёл не объект",
            f"свечи: элемент списка баров имеет тип {type(bar).__name__}",
        )

    # `time` — имя из документации. `dateTime` читается тоже: тем же именем
    # зовётся время свечи в потоке того же брокера, а расхождение выписки
    # с сервером в именах полей на этом API уже случалось (`broker/stream.py`).
    when = pick(bar, ("time", "dateTime"))
    values = {name: number(pick(bar, (name,))) for name in _NUMBERS}
    missing = [name for name, value in values.items() if value is None]
    if when is None:
        missing.insert(0, "time")
    if missing:
        known = ", ".join(sorted(str(key) for key in bar)) or "пусто"
        raise UnexpectedAnswer(
            "в свече от брокера не хватает полей: " + ", ".join(missing),
            f"свечи: бар без полей {', '.join(missing)}; пришли поля: {known}",
        )

    try:
        opened_at = aware_moment(when)
    except ValueError as error:
        raise UnexpectedAnswer(
            f"время свечи от брокера разобрать нельзя: {error}",
            f"свечи: поле времени {when!r}",
        ) from error

    return HistoricalCandle(
        ticker=ticker,
        class_code=class_code,
        timeframe=timeframe,
        opened_at=opened_at,
        open=_taken(values, "open"),
        high=_taken(values, "high"),
        low=_taken(values, "low"),
        close=_taken(values, "close"),
        volume=_taken(values, "volume"),
    )


def _taken(values: Mapping[str, float | None], name: str) -> float:
    """Число из разобранной таблицы полей. `None` сюда уже не доходит."""
    value = values[name]
    if value is None:  # pragma: no cover — отсеяно проверкой `missing` выше
        raise UnexpectedAnswer(f"поле {name} свечи пусто", f"свечи: поле {name} пусто")
    return value


def _zoned(moment: datetime, what: str) -> datetime:
    """Момент с поясом — любым. Наивное время — отказ, а не догадка.

    Перевода в UTC здесь **нет** намеренно, хотя он напрашивается. Перевод
    сделан ровно в одном месте — в `_stamp`, там же, где ставится буква `Z`.
    Пока таких мест два, ошибка в одном из них не видна ни одной проверке:
    второе исправит её и промолчит (проверено мутацией 04.09.2026 — при двух
    местах порча одного из них не роняет ни один тест). Одно место — одна
    проверка, и она падает, когда его сломали.

    Отвергать наивное время всё равно надо, и раньше перевода: `astimezone`
    истолковал бы момент без пояса как **местный**, а на московской машине
    это молчаливый сдвиг на три часа.
    """
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(
            f"{what}: время {moment.isoformat()} без часового пояса. "
            "Брокер работает в UTC, программа — в МСК; время без пояса "
            "сдвинуло бы свечи на три часа, ничего при этом не уронив"
        )
    return moment


def _stamp(moment: datetime) -> str:
    """Момент → строка запроса, как в примерах документации: `2025-11-14T07:00:00Z`.

    Перевод в UTC делается **здесь**, и только здесь. Суффикс `Z` —
    утверждение «это UTC»; приписанный к московскому времени, он врёт брокеру
    ровно на три часа, и ни одна проверка такого не заметит: запрос пройдёт,
    свечи придут, они будут не те. Буква и содержимое ставятся одной строкой,
    поэтому разъехаться им негде.
    """
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _whole_second(moment: datetime, *, up: bool) -> datetime:
    """Убрать доли секунды: формат запроса их не несёт.

    Начало периода округляется вниз, конец — вверх. Округление конца вниз
    отрезало бы последний бар в тех случаях, когда конец периода взят
    по часам программы, а не по сетке биржи.
    """
    if moment.microsecond == 0:
        return moment
    whole = moment.replace(microsecond=0)
    return whole + timedelta(seconds=1) if up else whole


class History:
    """Исторические свечи брокера. Только чтение; заявок этот класс не подаёт.

    Кэша нет намеренно: единственный потребитель — догрузка пропущенного
    после обрыва, и она спрашивает ровно то, чего у неё нет.
    """

    def __init__(self, session: BrokerSession, *, max_requests: int = MAX_REQUESTS) -> None:
        self._session = session
        self._max_requests = max(1, max_requests)

    async def candles(
        self,
        *,
        ticker: str,
        class_code: str,
        start: datetime,
        end: datetime,
        timeframe: str = MINUTE,
    ) -> tuple[HistoricalCandle, ...]:
        """Свечи за период, по возрастанию времени открытия, без повторов.

        Период любой длины: он сам разбивается на куски по 1440 баров
        (`split_period`), и каждый кусок — отдельный запрос к брокеру.
        Между запросами работает общий ограничитель частоты сессии, а каждый
        отдельный запрос переживает обрыв повтором — оба механизма уже
        встроены в `BrokerSession.read`.

        Отказ **посреди** догрузки поднимается как `HistoryIncomplete`
        с уже полученной частью внутри: половина ряда, отданная как целый,
        оставила бы в данных дыру, о которой никто не узнает. Отказ на первом
        же куске, когда получать ещё нечего, поднимается как есть.

        `ValueError` — это ошибка вызывающего, а не отказ брокера: время без
        пояса, незнакомый таймфрейм, конец периода раньше начала, слишком
        много запросов. Все четыре проверяются **до** обращения к сети.
        """
        first = _whole_second(_zoned(start, "начало периода"), up=False)
        last = _whole_second(_zoned(end, "конец периода"), up=True)
        chunks = split_period(first, last, timeframe=timeframe)
        if len(chunks) > self._max_requests:
            raise ValueError(
                f"период потребовал бы {len(chunks)} запросов к брокеру при "
                f"пределе {self._max_requests}. Догрузка после обрыва столько "
                "не просит; глубокая история берётся не у брокера, а с биржи"
            )

        collected: dict[datetime, HistoricalCandle] = {}
        for chunk_start, chunk_end in chunks:
            try:
                arrived = await self._chunk(
                    ticker=ticker,
                    class_code=class_code,
                    start=chunk_start,
                    end=chunk_end,
                    timeframe=timeframe,
                )
            except BrokerError as failure:
                if not collected:
                    raise
                raise HistoryIncomplete(_ordered(collected), cause=failure) from failure
            for candle in arrived:
                collected[candle.opened_at] = candle

        return _ordered(collected)

    async def _chunk(
        self,
        *,
        ticker: str,
        class_code: str,
        start: datetime,
        end: datetime,
        timeframe: str,
    ) -> tuple[HistoricalCandle, ...]:
        """Один запрос: не больше 1440 баров, имена параметров — из документации."""
        payload = await self._session.read(
            CANDLES_PATH,
            params={
                "classCode": class_code,
                "ticker": ticker,
                "startDate": _stamp(start),
                "endDate": _stamp(end),
                "timeFrame": timeframe,
            },
        )
        return parse_candles(
            payload, ticker=ticker, class_code=class_code, timeframe=timeframe
        )


def _ordered(collected: Mapping[datetime, HistoricalCandle]) -> tuple[HistoricalCandle, ...]:
    """Свечи по возрастанию времени открытия.

    Ключ словаря — момент открытия, поэтому один и тот же бар, пришедший
    в двух соседних кусках (границы кусков нахлёстываются на один момент),
    попадает в ответ один раз.
    """
    return tuple(collected[key] for key in sorted(collected))
