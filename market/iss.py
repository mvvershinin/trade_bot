"""Открытые данные Московской биржи (ISS): минутные свечи и глубина истории.

Бесплатно, без токена, без регистрации — этим ISS и ценен: глубокая история
у брокера недоступна (лимит 1440 свечей на запрос, DOMAIN.md §6).

Откуда взято
------------
Постраничная загрузка и политика повторов перенесены из архивного загрузчика
`reference/stand/moexdata.py` (`fetch_minutes`): смещение `start`, пауза между
страницами, четыре попытки с нарастающей задержкой `1 + попытка * 2` секунды.

Три места сознательно сделаны иначе, и каждое — про тихую потерю данных:

1. **BOM.** В архиве ответ с BOM валил разбор, чанк перезапрашивался и на
   следующей попытке обычно приходил чистым. Здесь тело декодируется
   `utf-8-sig`, то есть BOM просто снимается: тот же результат без лишнего
   круга по сети. Повторы остались — для настоящих сбоев.
2. **Условие остановки.** В архиве обход прекращался, когда страница короче
   500 строк. Если сервер сменит размер страницы, такое условие оборвёт
   загрузку молча — с виду успешной. Здесь обход идёт до **пустой** страницы,
   ценой одного лишнего запроса на инструмент.
3. **Отказ вместо пустого результата.** В архиве исчерпание попыток
   возвращало то, что успело накачаться (`return out`). Половина истории,
   отданная как целая, — это ровно та тихая потеря свечей, которая в проекте
   запрещена. Здесь исчерпание попыток — исключение.

Разбор ответа отделён от сети намеренно: `parse_candles` — чистая функция,
и все проверки разбора идут без единого сетевого запроса.

Отсюда берутся **только минутные** свечи
----------------------------------------
ISS умеет отдавать и десятиминутные, и часовые свечи. Слой их не берёт,
и это не упрощение, а требование ARCHITECTURE.md §1.

Свеча, собранная **сервером**, не может честно сообщить свою полноту: сколько
минут интервала в ней действительно было, ISS не пишет. Раньше здесь стояло
`filled_minutes = длина интервала` — то есть скачанная свеча объявлялась полной
всегда, безусловно. Свеча того же интервала, собранная нами из минуток
(`market.aggregate`), полноту считает честно. При настройке движка «пропускать
неполные свечи» один и тот же отрезок давал **разный ряд баров** в зависимости
от того, каким путём он получен: расходилась не цена исполнения, а сам список
свечей — ровно то, что главное правило обязано делать невозможным.

Молчать дешевле, чем врать: клиент отдаёт минутки, любой другой размер свечи
собирается из них `build_bars`. Тот же интервал, та же цена, честная полнота.
Ничего при этом не теряется — минутная история ISS уходит вглубь не меньше
любой другой (DOMAIN.md §6, замер 30.08.2026).

Ограничение, про которое надо помнить
-------------------------------------
Клиент **синхронный**, и таким остаётся ([решение 0005](../.docs/decisions/0005-concurrency-model.md)):
переписывать на `httpx.AsyncClient` политику повторов, перенесённую из архивного
загрузчика построчно, — риск тихого расхождения там, где потеря свечей запрещена;
и записи 5 000 минуток в SQLite это всё равно не касается, она синхронна в любом
случае. Синхронность изолируется **выделенным потоком данных**: сквозной вход —
`market.worker`, там же живёт соединение с базой. Прямой вызов из цикла событий
останавливает поток котировок на всё время загрузки (замер: `time.sleep(0.3)`
в корутине оставляет от 15 тиков таймера один).
"""

from __future__ import annotations

import json
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol, TypeVar, runtime_checkable

from market.candles import MINUTE, MSK, Candle, floor_to_minute

__all__ = [
    "ISS_BASE",
    "Market",
    "MARKETS",
    "SHARES",
    "FUTURES",
    "Transport",
    "HttpxTransport",
    "IssError",
    "IssTransportError",
    "IssPayloadError",
    "IssPagingError",
    "IssUnknownInstrument",
    "IssHttpError",
    "IssStopped",
    "RETRYABLE_STATUSES",
    "CandleBorder",
    "FetchResult",
    "IssClient",
    "MINUTE_INTERVAL",
    "check_interval",
    "candles_url",
    "borders_url",
    "securities_url",
    "parse_candles",
    "parse_borders",
    "parse_security",
    "InstrumentSpec",
]

_T = TypeVar("_T")

ISS_BASE = "https://iss.moex.com/iss"

#: Размер страницы ISS для свечей. Используется только как ожидание,
#: остановка обхода на него не завязана — см. шапку модуля.
ISS_PAGE_SIZE = 500


@dataclass(frozen=True, slots=True)
class Market:
    """Где на ISS искать инструмент: движок, рынок, режим торгов."""

    engine: str
    market: str
    board: str
    title: str = ""


SHARES = Market("stock", "shares", "TQBR", "акции")
FUTURES = Market("futures", "forts", "RFUD", "срочный рынок")
MARKETS: dict[str, Market] = {"shares": SHARES, "futures": FUTURES}


class IssError(Exception):
    """Общая ошибка работы с ISS."""


class IssTransportError(IssError):
    """Сеть не отдала ответ."""


class IssPayloadError(IssError):
    """Ответ пришёл, но это не то, что ожидалось."""


class IssPagingError(IssError):
    """Обход страниц не сходится — данные отдавать нельзя."""


class IssUnknownInstrument(IssError):
    """Биржа такого инструмента не знает. Повторять нечего.

    **Не** наследник `IssPayloadError`, и это главное в этом классе.
    Неизвестный тикер приходит кодом 200 с пустой таблицей, а `_fetch`
    повторяет `IssPayloadError` четыре раза с паузами 1, 3, 5 секунд —
    девять секунд ожидания за опечатку в тикере, после которых причина
    ещё и завёрнута в «не загрузилось за 4 попыток». Тикер фьючерса
    меняется с каждой экспирацией, то есть опечатка здесь — штатное
    событие, а не редкость.
    """


class IssHttpError(IssError):
    """Сервер ответил кодом, который повторять бессмысленно.

    Отдельно от `IssTransportError` намеренно. Опечатка в тикере или снятый
    с торгов инструмент — это `404`, и повторять его четыре раза с паузами
    1, 3, 5 секунд значит платить девять секунд за каждый кусок: при годовой
    догрузке (13 кусков) — две минуты ожидания вместо немедленного отказа
    с внятным текстом.

    Повторяемые коды перечислены в `RETRYABLE_STATUSES`: они означают
    «сейчас занято», а не «такого нет».
    """

    def __init__(self, status: int, detail: str = "") -> None:
        self.status = status
        super().__init__(
            f"сервер ISS ответил {status}"
            + (f": {detail}" if detail else "")
            + (
                ". Повторять нечего — проверьте тикер и рынок"
                if 400 <= status < 500
                else ""
            )
        )


class IssStopped(IssError):
    """Загрузку попросили остановиться — она прервалась между страницами.

    Не сбой: так закрывается программа, пока идёт долгая догрузка. Скачанные
    и записанные куски остаются в базе, отчёт о загрузке пишется как при любом
    другом обрыве.
    """


#: Коды ответа, которые означают «занято, попробуйте ещё раз», а не «такого нет».
#: Всё остальное из 4xx повторять бессмысленно; 5xx повторяются целиком.
RETRYABLE_STATUSES = frozenset({408, 425, 429})


@runtime_checkable
class Transport(Protocol):
    """Как слой ходит в сеть. Ровно одна операция — чтобы её было легко подменить."""

    def get(self, url: str, *, timeout: float) -> bytes: ...


class HttpxTransport:
    """Транспорт на httpx. Единственное место слоя, которое открывает сокет."""

    def __init__(self, client: object | None = None) -> None:
        self._client = client

    def get(self, url: str, *, timeout: float) -> bytes:
        import httpx  # локальный импорт: без сети слой должен импортироваться

        try:
            if self._client is None:
                response = httpx.get(url, timeout=timeout, follow_redirects=True)
            else:
                response = self._client.get(url, timeout=timeout)  # type: ignore[attr-defined]
            response.raise_for_status()
            return bytes(response.content)
        except httpx.HTTPStatusError as error:
            status = int(error.response.status_code)
            # «Занято» повторяем, «такого нет» — нет: см. `IssHttpError`.
            if status in RETRYABLE_STATUSES or status >= 500:
                raise IssTransportError(f"сервер ISS ответил {status}") from error
            raise IssHttpError(status) from error
        except Exception as error:  # транспорт заворачивается целиком
            raise IssTransportError(str(error)) from error


def candles_url(
    secid: str,
    *,
    market: Market,
    date_from: date,
    date_to: date,
    interval: int = 1,
    start: int = 0,
    base: str = ISS_BASE,
) -> str:
    """Адрес страницы свечей. Чистая функция — проверяется без сети.

    :raises ValueError: интервал не минутный (`check_interval`).

    Проверка стоит и здесь, а не только в `IssClient.candles`. Функция
    экспортирована из пакета, и собрать ею адрес десятиминуток, сходить
    транспортом и позвать `parse_candles(body)` с умолчанием — путь мимо
    всех проверок, которым уже пользовался служебный скрипт в `tools/`.
    Слой берёт с ISS **только минутки** (шапка модуля), и адрес чего-то
    другого он не строит.
    """
    check_interval(interval)
    path = (
        f"{base}/engines/{market.engine}/markets/{market.market}"
        f"/boards/{market.board}/securities/{urllib.parse.quote(secid)}/candles.json"
    )
    query = urllib.parse.urlencode(
        {
            "from": date_from.isoformat(),
            "till": date_to.isoformat(),
            "interval": interval,
            "iss.meta": "off",
            "start": start,
        }
    )
    return f"{path}?{query}"


def borders_url(secid: str, *, market: Market, base: str = ISS_BASE) -> str:
    """Адрес справки о доступной глубине истории по каждому интервалу."""
    return (
        f"{base}/engines/{market.engine}/markets/{market.market}"
        f"/boards/{market.board}/securities/{urllib.parse.quote(secid)}"
        "/candleborders.json?iss.meta=off"
    )


def securities_url(secid: str, *, market: Market, base: str = ISS_BASE) -> str:
    """Адрес карточки инструмента: шаг цены, стоимость шага, лот, ГО, сбор.

    ⚠️ Адрес **без** `/boards/`, в отличие от свечей. Режим торгов приходит
    колонкой `BOARDID`, и по ней же строка выбирается: у одного тикера строк
    бывает несколько (у акции их две), и взять первую — тот самый дефект,
    который заведён на разбор справочника брокера (`B-016`).
    """
    return (
        f"{base}/engines/{market.engine}/markets/{market.market}"
        f"/securities/{urllib.parse.quote(secid)}.json?iss.meta=off"
    )


def _decode(payload: bytes) -> object:
    """Разобрать тело ответа. BOM снимается, а не считается сбоем."""
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise IssPayloadError(f"тело ответа не UTF-8: {error}") from error
    if not text.strip():
        raise IssPayloadError("пустое тело ответа")
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise IssPayloadError(f"тело ответа не JSON: {error}") from error


def _block(document: object, name: str) -> tuple[list[str], list[list[object]]]:
    if not isinstance(document, dict) or name not in document:
        raise IssPayloadError(f"в ответе нет блока {name!r}")
    block = document[name]
    if not isinstance(block, dict) or "columns" not in block or "data" not in block:
        raise IssPayloadError(f"блок {name!r} не похож на таблицу ISS")
    columns = block["columns"]
    rows = block["data"]
    if not isinstance(columns, list) or not isinstance(rows, list):
        raise IssPayloadError(f"блок {name!r} повреждён")
    return [str(c) for c in columns], rows


def parse_candles(payload: bytes, *, interval: int = 1) -> list[Candle]:
    """**Минутные** свечи из ответа ISS. Колонки читаются по именам, а не по порядку.

    Чтение по порядку («первая — open, вторая — close») — то, что делал
    черновик архива. Оно работает ровно до дня, когда ISS добавит колонку.

    Размер свечи проверяется **по самим данным**, а не по тому, что назвал
    вызывающий. В ответе ISS есть колонка `end` — конец интервала свечи;
    у минутки это `10:05:59`, у десятиминутки `10:09:59`. Проверка по параметру
    `interval` обходилась одной строкой: собрать адрес с `interval=10`, сходить
    транспортом и позвать `parse_candles(body)` с умолчанием. Дальше
    десятиминутки с `timeframe=MINUTE` проходили и в `put_minutes` — она
    проверяет **тип**, а тип честно говорит «минутка». В базе оказывались
    десятиминутки под видом минуток. Проверку по данным обойти нечем.

    :param interval: только `1`. Почему любой другой — отказ, а не свеча
        с выдуманной полнотой, разобрано в шапке модуля.
    :raises ValueError: интервал не минутный.
    :raises IssPayloadError: в ответе нет колонки `end` либо интервал свечи
        в самих данных длиннее минуты.
    """
    check_interval(interval)
    document = _decode(payload)
    columns, rows = _block(document, "candles")
    try:
        index = {
            name: columns.index(name)
            for name in ("open", "high", "low", "close", "volume", "begin", "end")
        }
    except ValueError as error:
        raise IssPayloadError(f"в ответе нет обязательной колонки: {error}") from error

    candles: list[Candle] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < len(columns):
            raise IssPayloadError(f"строка свечи короче заголовка: {row!r}")
        try:
            begin = datetime.strptime(str(row[index["begin"]]), "%Y-%m-%d %H:%M:%S")
        except ValueError as error:
            raise IssPayloadError(f"не разобрано время свечи {row[index['begin']]!r}") from error
        try:
            end = datetime.strptime(str(row[index["end"]]), "%Y-%m-%d %H:%M:%S")
        except ValueError as error:
            raise IssPayloadError(f"не разобран конец свечи {row[index['end']]!r}") from error
        if (end - begin).total_seconds() >= 60:
            raise IssPayloadError(
                f"свеча {begin:%Y-%m-%d %H:%M} длиннее минуты: {begin} … {end}. "
                "Слой берёт с биржи только минутные свечи; свеча, собранная сервером, "
                "не сообщает свою полноту, и объявить её полной — значит "
                "получить разный ряд баров для одного отрезка"
            )
        try:
            candles.append(
                Candle(
                    time=begin.replace(tzinfo=MSK),
                    open=float(row[index["open"]]),
                    high=float(row[index["high"]]),
                    low=float(row[index["low"]]),
                    close=float(row[index["close"]]),
                    volume=float(row[index["volume"]]),
                    # Минутка покрывает ровно свою минуту — единственная
                    # полнота, которую этот источник знает достоверно.
                    timeframe=MINUTE,
                    filled_minutes=1,
                )
            )
        except (TypeError, ValueError) as error:
            raise IssPayloadError(f"не разобраны цены свечи {row!r}: {error}") from error
    return candles


#: Единственный интервал ISS, который берёт слой. Обоснование — в шапке модуля.
MINUTE_INTERVAL = 1


def check_interval(interval: int) -> None:
    """Слой берёт с ISS только минутки — или отказ вслух.

    Отдельная функция, а не проверка внутри разбора: `IssClient` зовёт её
    **до** первого запроса, чтобы отказ случился на границе, а не после
    похода в сеть.

    :raises ValueError: запрошен не минутный интервал.
    """
    if interval != MINUTE_INTERVAL:
        raise ValueError(
            f"с ISS берутся только минутные свечи, запрошен интервал {interval}. "
            "Свеча, собранная сервером, не сообщает свою полноту: сколько минут "
            "интервала в ней было, в ответе не написано. Объявить её полной — "
            "значит получить разный ряд баров для одного отрезка в зависимости "
            "от способа загрузки. Нужен другой размер свечи — качайте минутные "
            "и собирайте market.aggregate.build_bars"
        )


@dataclass(frozen=True, slots=True)
class CandleBorder:
    """Доступная глубина истории по одному интервалу — как её объявляет ISS."""

    interval: int
    begin: datetime
    end: datetime

    @property
    def days(self) -> int:
        return (self.end.date() - self.begin.date()).days + 1


def parse_borders(payload: bytes) -> list[CandleBorder]:
    """Границы доступной истории по интервалам.

    ⚠️ Это **заявление сервера**, а не проверка. Реальную глубину показывает
    только скачивание: см. `market.depth`.
    """
    document = _decode(payload)
    columns, rows = _block(document, "borders")
    try:
        index = {name: columns.index(name) for name in ("begin", "end", "interval")}
    except ValueError as error:
        raise IssPayloadError(f"в ответе нет обязательной колонки: {error}") from error

    borders: list[CandleBorder] = []
    for row in rows:
        try:
            begin = datetime.strptime(str(row[index["begin"]]), "%Y-%m-%d %H:%M:%S")
            end = datetime.strptime(str(row[index["end"]]), "%Y-%m-%d %H:%M:%S")
        except (ValueError, IndexError) as error:
            raise IssPayloadError(f"не разобрана граница {row!r}") from error
        borders.append(
            CandleBorder(
                interval=int(row[index["interval"]]),
                begin=begin.replace(tzinfo=MSK),
                end=end.replace(tzinfo=MSK),
            )
        )
    return borders


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    """Карточка инструмента с биржи: то, что биржа объявляет о нём сама.

    Ради **стоимости шага**. Она превращает движение цены в рубли, и
    у каждого контракта она своя: у фьючерса на индекс МосБиржи это ровно
    1 ₽ за пункт, у РТС — 1,74, у Брента — 869. Умолчание 1,0 верно для
    одного контракта из шести и молча врёт для остальных: сделки те же,
    деньги другие (`B-021`).

    ⚠️ **Величина плавающая.** У РТС и Брента стоимость шага считается через
    курс доллара и пересчитывается каждый день. Снять один раз и запомнить
    навсегда — значит завести вторую тихую ошибку вместо первой; отсюда
    `quoted_at`: момент, которым биржа пометила свои котировочные поля.

    ⚠️ **Ни одного торгового решения тут нет.** Это свойство инструмента,
    как шаг цены и лот. Движок получает готовое число и не знает, откуда оно
    (ARCHITECTURE.md §1).

    Поля, которых у инструмента может не быть, — `None`, а не ноль и не
    единица. У акций `STEPPRICE` нет вовсе: цена акции и так в рублях,
    и подставленная за биржу единица выглядела бы подтверждённой величиной.
    """

    secid: str
    board: str
    #: `MINSTEP` — наименьшее движение цены.
    price_step: float
    #: `STEPPRICE` — сколько рублей приносит одно такое движение.
    step_price: float | None
    #: `LOTVOLUME` у срочного рынка, `LOTSIZE` у акций.
    lot: float | None
    #: `INITIALMARGIN` — гарантийное обеспечение биржи под один контракт.
    initial_margin: float | None
    #: `BUYSELLFEE` — **биржевой** сбор за контракт на сторону. Это не тариф
    #: брокера: тариф считается от него и обычно больше. Замер 05.09.2026:
    #: у `MXU6` биржа объявляет 14,68 ₽, а умолчание проекта — 14 ₽ целиком,
    #: то есть уже неверно (`DOMAIN.md` §5 брал сбор плюс рубль брокеру).
    exchange_fee: float | None
    #: `SCALPERFEE` — сбор за операцию, закрытую внутри той же сессии.
    #: У `MXU6` это 7,34 ₽ против 14,68 ₽. Реверсная система на пятиминутках
    #: закрывается внутри сессии почти всегда, поэтому величина к делу
    #: относится напрямую — но выбор тарифа делает владелец счёта, а не мы.
    scalper_fee: float | None
    #: `IMTIME` — момент, которым биржа пометила котировочные поля, строкой
    #: как пришло. Разбирать её не во что: пояс в ответе не указан.
    quoted_at: str

    @property
    def ruble_per_point(self) -> float | None:
        """Рублей в одном пункте цены — или `None`, если биржа не сказала.

        `None` — честный ответ, а не повод подставить единицу. Единица
        подставленная и единица подтверждённая выглядят одинаково, а стоят
        разного: по `BRV6` разница в 869 раз.
        """
        if self.step_price is None or self.price_step <= 0:
            return None
        return self.step_price / self.price_step


def _optional_float(row: dict[str, object], name: str) -> float | None:
    """Число из строки ISS — или `None`, если колонки нет либо она пуста.

    Пустая колонка у ISS приходит как `null`, а не как ноль, и превращать
    её в ноль нельзя: ноль ГО и ноль сбора выглядят посчитанными.
    """
    value = row.get(name)
    if value is None or value == "":
        return None
    try:
        return float(value)  # type: ignore[arg-type]  # тип строки ISS заранее неизвестен
    except (TypeError, ValueError) as error:
        raise IssPayloadError(f"колонка {name!r} не число: {value!r}") from error


def _first_of(row: dict[str, object], *names: str) -> float | None:
    """Первая заполненная колонка из перечисленных. Ноль — заполненная."""
    for name in names:
        value = _optional_float(row, name)
        if value is not None:
            return value
    return None


def parse_security(payload: bytes, *, secid: str, market: Market) -> InstrumentSpec:
    """Карточка инструмента из ответа ISS. Чистая функция — проверяется без сети.

    Строка выбирается **по режиму торгов**, а не первая попавшаяся: у одного
    тикера строк бывает несколько. Не нашлась строка нужного режима — отказ
    вслух: молча взятая чужая строка означала бы стоимость шага другого
    инструмента под именем нашего.

    :raises IssPayloadError: инструмента нет, режим не найден, нет `MINSTEP`
        либо шаг цены не положительный.
    """
    document = _decode(payload)
    columns, rows = _block(document, "securities")
    if not rows:
        raise IssUnknownInstrument(
            f"биржа не знает инструмента {secid!r} на рынке «{market.title}». "
            "Проверьте тикер: у фьючерса он меняется с каждой экспирацией"
        )
    records = [
        {name: row[index] for index, name in enumerate(columns) if index < len(row)}
        for row in rows
        if isinstance(row, list)
    ]
    matching = [row for row in records if str(row.get("BOARDID", "")) == market.board]
    if not matching:
        seen = ", ".join(sorted({str(row.get("BOARDID", "?")) for row in records}))
        raise IssUnknownInstrument(
            f"у инструмента {secid!r} нет режима торгов {market.board!r}; "
            f"биржа вернула: {seen}. Брать чужой режим нельзя — у него "
            "могут быть свои шаг цены и стоимость шага"
        )
    row = matching[0]
    step = _optional_float(row, "MINSTEP")
    if step is None or step <= 0:
        raise IssPayloadError(
            f"биржа не назвала шаг цены инструмента {secid!r} "
            f"(MINSTEP = {row.get('MINSTEP')!r}) — считать стоимость пункта не от чего"
        )
    return InstrumentSpec(
        secid=str(row.get("SECID", secid)),
        board=str(row.get("BOARDID", market.board)),
        price_step=step,
        step_price=_optional_float(row, "STEPPRICE"),
        # У срочного рынка колонка называется `LOTVOLUME`, у акций `LOTSIZE`.
        # Порядок важен: у акции есть только вторая, и первая вернёт `None`.
        lot=_first_of(row, "LOTVOLUME", "LOTSIZE"),
        initial_margin=_optional_float(row, "INITIALMARGIN"),
        exchange_fee=_optional_float(row, "BUYSELLFEE"),
        scalper_fee=_optional_float(row, "SCALPERFEE"),
        # ⚠️ `or ""`, а не умолчание `get`: у ISS пустая колонка приходит
        # как `null`, и `str(None)` дал бы строку «None» — её владелец счёта
        # прочитал бы в окне как «карточка помечена None».
        quoted_at=str(row.get("IMTIME") or ""),
    )


def _went_forward(previous_first: datetime | None, first_time: datetime) -> bool:
    """Ушла ли страница вперёд по сравнению с прошлой.

    Сравниваются **первые** минуты страниц, а не последние, и в этом всё
    различие (`B-031`): хвост, повторённый в конце обхода, начинается позже
    прошлой страницы, а страница застрявшего сервера — не позже.
    """
    return previous_first is None or first_time > previous_first


def _absorb(
    page: list[Candle], seen: dict[datetime, Candle], result: "FetchResult"
) -> bool:
    """Забрать из страницы новые минуты. `False` — нового в ней не было.

    `False` означает конец данных: смещение ушло вперёд (это проверено
    до вызова), а биржа вернула то, что уже отдавала, — значит за этот
    период у неё больше ничего нет. Обход на этом кончается **успехом**.
    """
    fresh = 0
    for candle in page:
        key = floor_to_minute(candle.time)
        if key in seen:
            result.duplicates += 1
            continue
        seen[key] = candle
        fresh += 1
    if fresh:
        return True
    result.exhausted = True
    return False


def _stuck(secid: str, collected: int, last_time: datetime) -> str:
    """Отказ «биржа стоит на месте» — словами, а не смещением страницы.

    ⚠️ Отличие от конца данных названо здесь, потому что спутать их легко,
    и один раз уже спутали.

    * **Конец данных.** Смещение ушло вперёд, страница пришла, но все её
      минуты уже собраны: биржа повторила хвост, потому что дальше у неё
      за этот период ничего нет. Так кончается загрузка, у которой число
      минут не делится на размер страницы ровно, — то есть почти любая.
    * **Обход стоит.** Смещение растёт, а биржа отдаёт страницу, которая
      начинается не позже прошлой: `start` она не слышит вовсе. Такой обход
      не кончится никогда, и остановить его надо отказом.

    Прежняя редакция различала их по **последней** минуте страницы и потому
    не различала вовсе: у обоих исходов время «не сдвинулось». Владелец счёта
    получил красное окно «обход зациклился» на успешно загруженной истории.
    """
    return (
        f"{secid}: биржа отдаёт одни и те же свечи и дальше не идёт. "
        f"Загружено {collected} шт., последняя за {last_time:%d.%m.%Y %H:%M} МСК. "
        "Это сбой на стороне биржи, а не в данных: скачанное записано, "
        "повторите загрузку позже."
    )


@dataclass(slots=True)
class FetchResult:
    """Что получилось при загрузке, в числах — для отчёта в журнал.

    `candles` заполняется **в конце** обхода, а не после каждой страницы:
    пересборка и сортировка списка на каждой из четырёх сотен страниц
    превращали загрузку года минуток в квадратичную. Для показа хода работы
    есть `loaded` и `last_seen` — они обновляются постранично.
    """

    candles: list[Candle] = field(default_factory=list)
    loaded: int = 0
    last_seen: datetime | None = None
    pages: int = 0
    requests: int = 0
    retries: int = 0
    duplicates: int = 0
    #: Обход кончился тем, что биржа начала повторять уже отданное, —
    #: то есть за запрошенный период у неё больше ничего нет. Это **успех**,
    #: а не беда: поле нужно отчёту в журнал, а не отказу.
    exhausted: bool = False

    @property
    def first_time(self) -> datetime | None:
        return self.candles[0].time if self.candles else None

    @property
    def last_time(self) -> datetime | None:
        return self.candles[-1].time if self.candles else None


class IssClient:
    """Постраничная загрузка свечей с ISS.

    Транспорт передаётся снаружи: в тестах это подставная функция, в бою —
    `HttpxTransport`. Из-за этого весь разбор, вся склейка страниц и вся
    политика повторов проверяются без сети.
    """

    def __init__(
        self,
        transport: Transport | None = None,
        *,
        attempts: int = 4,
        pause: float = 0.15,
        timeout: float = 40.0,
        sleep: Callable[[float], None] = time.sleep,
        base: str = ISS_BASE,
        max_pages: int = 20_000,
    ) -> None:
        if attempts < 1:
            raise ValueError("попыток должно быть хотя бы одна")
        self._transport = transport if transport is not None else HttpxTransport()
        self._attempts = attempts
        self._pause = pause
        self._timeout = timeout
        self._sleep = sleep
        self._base = base
        self._max_pages = max_pages
        self._stopped = False

    def stop(self) -> None:
        """Попросить загрузку остановиться на ближайшей границе страницы.

        Зовётся **из другого потока** — иначе смысла нет: загрузка занимает
        поток данных целиком. Присваивание флага атомарно, ждать ответа
        не нужно. Проверка стоит между страницами и перед паузой повтора:
        реже — значит закрытие окна ждёт минутами, чаще — некуда, внутри
        одного запроса прерывать нечего.

        Отменить уже отправленный запрос это не может: питон не прерывает
        чужой поток. Обещание ровно одно — не начинать следующую страницу.
        """
        self._stopped = True

    @property
    def stopped(self) -> bool:
        return self._stopped

    def _check_stop(self, secid: str) -> None:
        if self._stopped:
            raise IssStopped(
                f"{secid}: загрузка остановлена по просьбе — скачанное записано, "
                "остальное догрузится при следующем запуске"
            )

    def _fetch(self, url: str, parse: Callable[[bytes], _T], result: FetchResult) -> _T:
        """Запрос и разбор под одной политикой повторов.

        Разбор внутри повтора намеренно: ответ с BOM или обрезанное тело —
        это сбой доставки, а не порча данных на бирже, и лечится он тем же
        перезапросом, что и обрыв сокета. Пауза растёт: 1, 3, 5 секунд —
        как в архивном загрузчике.

        `IssHttpError` мимо повторов проходит намеренно: 404 на опечатке
        в тикере не станет данными от четырёх попыток, а стоит девять секунд
        на каждый кусок.
        """
        last: Exception | None = None
        for attempt in range(self._attempts):
            result.requests += 1
            try:
                return parse(self._transport.get(url, timeout=self._timeout))
            except (IssTransportError, IssPayloadError) as error:
                last = error
                result.retries += 1
                if attempt + 1 < self._attempts:
                    self._sleep(1 + attempt * 2)
        raise IssTransportError(
            f"не загрузилось за {self._attempts} попыток: {last}"
        ) from last

    def candles(
        self,
        secid: str,
        *,
        market: Market,
        date_from: date,
        date_to: date,
        interval: int = 1,
        progress: Callable[[FetchResult], None] | None = None,
    ) -> FetchResult:
        """Все **минутные** свечи инструмента за период. Обход — до пустой страницы.

        :raises ValueError: период задом наперёд либо интервал не минутный
            (проверяется **до** первого запроса, см. `check_interval`).
        """
        check_interval(interval)
        if date_to < date_from:
            raise ValueError(f"период задом наперёд: {date_from} .. {date_to}")

        result = FetchResult()
        # Ключ — **минута**, а не момент, которым свечу пометил сервер:
        # то же правило, что и в хранилище и в сборке
        # (`market.candles.floor_to_minute`). Иначе один и тот же дубль
        # ловится в трёх местах слоя по трём разным ключам.
        seen: dict[datetime, Candle] = {}
        start = 0
        previous_first: datetime | None = None

        while True:
            self._check_stop(secid)
            if result.pages >= self._max_pages:
                raise IssPagingError(
                    f"{secid}: биржа отдала уже {self._max_pages} страниц свечей "
                    "и не кончается. Загрузка остановлена, чтобы не длиться "
                    "бесконечно; скачанное записано. Повторите её позже — "
                    "продолжится с этого места."
                )
            url = candles_url(
                secid,
                market=market,
                date_from=date_from,
                date_to=date_to,
                interval=interval,
                start=start,
                base=self._base,
            )
            page = self._fetch(
                url, lambda body: parse_candles(body, interval=interval), result
            )
            result.pages += 1
            if not page:
                break

            # Крайние минуты страницы. Минимум и максимум, а не первая
            # и последняя строка: порядок внутри страницы сервер не обещает,
            # и на неотсортированной странице любая проверка сдвига сработала
            # бы ложно — загрузка падала бы на целых данных.
            first_time = min(candle.time for candle in page)
            last_time = max(candle.time for candle in page)
            # ⚠️ Два исхода, до 06.09.2026 сваленные в один (`B-031`): обход,
            # стоящий на месте, и обход, дошедший до конца данных. Оба
            # выглядят как «время не сдвинулось», а значат противоположное.
            # Разбор — в `_stuck` и `_absorb`.
            if not _went_forward(previous_first, first_time):
                raise IssPagingError(_stuck(secid, len(seen), last_time))
            previous_first = first_time
            if not _absorb(page, seen, result):
                break

            start += len(page)
            result.loaded = len(seen)
            result.last_seen = last_time
            if progress is not None:
                progress(result)
            if self._pause:
                self._sleep(self._pause)

        result.candles = [seen[t] for t in sorted(seen)]
        result.loaded = len(result.candles)
        return result

    def minutes(self, secid: str, **kwargs: object) -> FetchResult:
        """Минутные свечи — единственное, что слой берёт с ISS.

        Пятиминутки ISS не отдаёт вовсе, а десятиминутки и часовые слой
        не берёт сам: собранная сервером свеча не сообщает свою полноту
        (шапка модуля). Любой размер свечи собирает
        `market.aggregate.build_bars` (DOMAIN.md §6).

        Чужой `interval` здесь **отвергается**, а не отбрасывается. Здесь
        стояло `kwargs.pop("interval", None)`: `minutes(..., interval=10)`
        молча возвращал пять тысяч минуток там, где просили пятьсот
        десятиминуток. Правило переставало быть правилом ровно в методе,
        названном единственным разрешённым входом.

        :raises ValueError: запрошен не минутный интервал.
        """
        interval = kwargs.pop("interval", MINUTE_INTERVAL)
        check_interval(interval)  # type: ignore[arg-type]
        return self.candles(secid, interval=MINUTE_INTERVAL, **kwargs)  # type: ignore[arg-type]

    def borders(self, secid: str, *, market: Market) -> list[CandleBorder]:
        """Что сервер заявляет о доступной глубине по каждому интервалу."""
        return self._fetch(
            borders_url(secid, market=market, base=self._base), parse_borders, FetchResult()
        )

    def security(self, secid: str, *, market: Market) -> InstrumentSpec:
        """Карточка инструмента: шаг цены, стоимость шага, лот, ГО, сбор.

        Один запрос, без обхода страниц: карточка — одна строка. Политика
        повторов общая с загрузкой свечей, включая то, что `404` и опечатка
        в тикере не повторяются (`IssHttpError`).

        ⚠️ Неизвестный тикер приходит **двумястами** с пустой таблицей,
        а не `404`. Пустоту ловит `parse_security` и называет причину:
        иначе «инструмента нет» выглядело бы как сбой сети и повторялось
        бы четыре раза с паузами.
        """
        return self._fetch(
            securities_url(secid, market=market, base=self._base),
            lambda body: parse_security(body, secid=secid, market=market),
            FetchResult(),
        )
