"""Открытые данные биржи: адрес, разбор ответа, склейка страниц, повторы.

Ни один тест не ходит в сеть. Транспорт подставной, паузы заглушены —
проверяется поведение слоя, а не доступность iss.moex.ru сегодня утром.
"""

from __future__ import annotations

import json
import urllib.parse
from datetime import date, datetime, timedelta

import pytest

from market.candles import MSK, MINUTE
from market.iss import (
    FUTURES,
    SHARES,
    HttpxTransport,
    IssClient,
    IssHttpError,
    IssPagingError,
    IssPayloadError,
    IssStopped,
    IssTransportError,
    IssUnknownInstrument,
    candles_url,
    parse_borders,
    parse_candles,
    parse_security,
    securities_url,
)

from market_helpers import (
    MXU6_ROW,
    SECURITY_COLUMNS,
    FakeTransport,
    iss_body,
    minutes_from,
    msk,
    no_sleep,
    pages_handler,
    security_body,
)


def query(url: str) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))


# -- адрес ------------------------------------------------------------------


def test_candles_url_for_futures() -> None:
    url = candles_url("MXU6", market=FUTURES, date_from=date(2026, 8, 1), date_to=date(2026, 8, 26))
    assert url.startswith(
        "https://iss.moex.com/iss/engines/futures/markets/forts/boards/RFUD/securities/MXU6/candles.json?"
    )
    assert query(url) == {
        "from": "2026-08-01",
        "till": "2026-08-26",
        "interval": "1",
        "iss.meta": "off",
        "start": "0",
    }


def test_candles_url_for_shares_uses_another_board() -> None:
    url = candles_url("SBER", market=SHARES, date_from=date(2026, 8, 1), date_to=date(2026, 8, 1))
    assert "/engines/stock/markets/shares/boards/TQBR/securities/SBER/" in url


# -- разбор -----------------------------------------------------------------


def test_parse_candles_reads_prices_and_moscow_time() -> None:
    source = minutes_from(msk(2026, 8, 26, 10, 5), 3)
    candles = parse_candles(iss_body(source))

    assert [c.time for c in candles] == [c.time for c in source]
    assert candles[0].time.utcoffset().total_seconds() == 3 * 3600
    assert candles[0].timeframe == MINUTE
    assert (candles[0].open, candles[0].close, candles[0].volume) == (100.0, 100.0, 1.0)


def test_parse_candles_survives_bom() -> None:
    """Ответ с BOM разбирается, а не считается сбоем.

    В архивном загрузчике BOM валил разбор и чанк перезапрашивался; данные
    от этого не портились, но круг по сети был лишним. Здесь BOM снимается.
    """
    source = minutes_from(msk(2026, 8, 26, 10, 5), 2)
    candles = parse_candles(iss_body(source, bom=True))
    assert len(candles) == 2


def test_parse_candles_reads_columns_by_name_not_by_order() -> None:
    """Перестановка колонок в ответе не должна менять цены местами.

    Черновик архива читал строку по порядку полей. Это работает ровно до дня,
    когда ISS добавит колонку, — и тогда `high` тихо станет `low`.
    """
    source = [
        c.replace(open=1.0, high=9.0, low=0.5, close=2.0, volume=7.0)
        for c in minutes_from(msk(2026, 8, 26, 10, 5), 1)
    ]
    reversed_columns = ["end", "begin", "volume", "value", "low", "high", "close", "open"]
    (candle,) = parse_candles(iss_body(source, columns=reversed_columns))
    assert (candle.open, candle.high, candle.low, candle.close, candle.volume) == (1.0, 9.0, 0.5, 2.0, 7.0)


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"   ",
        b"<html>503</html>",
        b'{"candles": {"columns": ["open"], "data": [[1]]}}',
        b'{"nothing": 1}',
        b'{"candles": [1, 2, 3]}',
    ],
)
def test_broken_payload_is_an_error_not_empty_result(body: bytes) -> None:
    """Повреждённый ответ — исключение, а не пустой список.

    Пустой список означал бы «за этот период торгов не было», и недокачанный
    кусок выглядел бы как выходной день.
    """
    with pytest.raises(IssPayloadError):
        parse_candles(body)


def test_unparsable_time_is_an_error() -> None:
    body = json.dumps(
        {"candles": {"columns": ["open", "high", "low", "close", "volume", "begin", "end"],
                     "data": [[1, 1, 1, 1, 1, "вчера", "2026-08-26 10:05:59"]]}}
    ).encode()
    with pytest.raises(IssPayloadError, match="время"):
        parse_candles(body)


def test_a_response_without_the_end_column_is_refused() -> None:
    """Без `end` размер свечи проверить нечем — значит, разбирать нельзя.

    Колонка `end` — единственный признак длины свечи, который приходит
    в самих данных. Разбирать ответ без неё значило бы вернуться к проверке
    по параметру `interval`, то есть к проверке, которую вызывающий задаёт сам.
    """
    body = json.dumps(
        {"candles": {"columns": ["open", "high", "low", "close", "volume", "begin"],
                     "data": [[1, 1, 1, 1, 1, "2026-08-26 10:05:00"]]}}
    ).encode()
    with pytest.raises(IssPayloadError, match="end"):
        parse_candles(body)


def test_parse_borders() -> None:
    body = json.dumps(
        {"borders": {"columns": ["begin", "end", "interval"],
                     "data": [["2025-09-06 10:18:00", "2026-08-29 18:59:59", 1]]}}
    ).encode()
    (border,) = parse_borders(body)
    assert border.interval == 1
    assert border.begin == datetime(2025, 9, 6, 10, 18, tzinfo=MSK)
    assert border.days == 358


# -- склейка страниц --------------------------------------------------------


def test_paging_collects_all_pages_until_empty() -> None:
    first_one = minutes_from(msk(2026, 8, 26, 10, 0), 500)
    second_one = minutes_from(msk(2026, 8, 26, 18, 20), 500)
    third_one = minutes_from(msk(2026, 8, 27, 7, 0), 137)
    transport = FakeTransport(pages_handler([iss_body(first_one), iss_body(second_one), iss_body(third_one)]))

    result = IssClient(transport, pause=0, sleep=no_sleep).minutes(
        "MXU6", market=FUTURES, date_from=date(2026, 8, 26), date_to=date(2026, 8, 27)
    )

    assert len(result.candles) == 1137
    assert result.pages == 4  # три с данными плюс пустая, на которой обход встал
    assert [query(u)["start"] for u in transport.urls] == ["0", "500", "1000", "1137"]


def test_paging_does_not_stop_on_a_short_page() -> None:
    """Короткая страница — не признак конца.

    В архиве обход прекращался, когда страница короче 500 строк. Стоит серверу
    сменить размер страницы — и загрузка обрывается молча, с виду успешно.
    """
    short_one = minutes_from(msk(2026, 8, 26, 10, 0), 3)
    next_one = minutes_from(msk(2026, 8, 26, 11, 0), 4)
    transport = FakeTransport(pages_handler([iss_body(short_one), iss_body(next_one)]))

    result = IssClient(transport, pause=0, sleep=no_sleep).minutes(
        "MXU6", market=FUTURES, date_from=date(2026, 8, 26), date_to=date(2026, 8, 26)
    )
    assert len(result.candles) == 7


def test_duplicates_between_pages_are_counted_and_dropped() -> None:
    candles = minutes_from(msk(2026, 8, 26, 10, 0), 5)
    transport = FakeTransport(
        pages_handler([iss_body(candles), iss_body(candles[3:] + minutes_from(msk(2026, 8, 26, 11, 0), 2))])
    )

    result = IssClient(transport, pause=0, sleep=no_sleep).minutes(
        "MXU6", market=FUTURES, date_from=date(2026, 8, 26), date_to=date(2026, 8, 26)
    )
    assert result.duplicates == 2
    assert len(result.candles) == 7
    assert len({c.time for c in result.candles}) == 7


def test_result_is_sorted_even_if_server_is_not() -> None:
    candles = minutes_from(msk(2026, 8, 26, 10, 0), 4)
    transport = FakeTransport(pages_handler([iss_body(list(reversed(candles)))]))
    result = IssClient(transport, pause=0, sleep=no_sleep).minutes(
        "MXU6", market=FUTURES, date_from=date(2026, 8, 26), date_to=date(2026, 8, 26)
    )
    assert [c.time for c in result.candles] == sorted(c.time for c in candles)


def test_paging_that_does_not_advance_is_an_error() -> None:
    """Сервер, отдающий одну и ту же страницу, — отказ, а не вечный цикл.

    ⚠️ Страница здесь **одна и та же**: она начинается с той же минуты,
    что и прошлая. Это и значит «сервер не слышит смещение». Отличать её
    от хвоста, повторённого в конце данных, обязан `_went_forward` —
    сторож на второй случай стоит соседним тестом.
    """
    candles = minutes_from(msk(2026, 8, 26, 10, 0), 3)
    transport = FakeTransport(lambda _url: iss_body(candles))

    with pytest.raises(IssPagingError, match="одни и те же свечи"):
        IssClient(transport, pause=0, sleep=no_sleep).minutes(
            "MXU6", market=FUTURES, date_from=date(2026, 8, 26), date_to=date(2026, 8, 26)
        )


def test_the_end_of_the_data_is_a_success_not_a_failure() -> None:
    """Стережёт `B-031`: биржа повторила хвост — это конец, а не поломка.

    Что случилось у владельца счёта 06.09.2026. Число минут за период
    не делится на размер страницы ровно, смещение уходит за последнюю
    строку не на целую страницу, и биржа отдаёт **перекрывающийся хвост**:
    страница начинается позже прошлой, а новых минут в ней нет. Загрузка
    при этом прошла целиком — а окно показало красное «обход зациклился».

    ⚠️ Проверка «загрузка не отказала» тут не годится: она зеленеет и тогда,
    когда обход молча потерял половину свечей. Поэтому сверяется **состав**:
    все минуты собраны и ни одна не задвоилась.
    """
    candles = minutes_from(msk(2026, 8, 26, 10, 0), 8)
    pages = [
        iss_body(candles[:5]),      # первая страница
        iss_body(candles[3:]),      # хвост внахлёст: начало позже, нового — 3
        iss_body(candles[5:]),      # то же самое ещё раз: нового нет вовсе
    ]
    result = IssClient(FakeTransport(pages_handler(pages)), pause=0, sleep=no_sleep).minutes(
        "MXU6", market=FUTURES, date_from=date(2026, 8, 26), date_to=date(2026, 8, 26)
    )

    assert result.exhausted, "конец данных не назван концом данных"
    assert [c.time for c in result.candles] == [c.time for c in candles], (
        "обход кончился раньше, чем биржа отдала всё"
    )


def test_backwards_period_is_refused() -> None:
    transport = FakeTransport(pages_handler([]))
    with pytest.raises(ValueError, match="задом наперёд"):
        IssClient(transport, pause=0, sleep=no_sleep).minutes(
            "MXU6", market=FUTURES, date_from=date(2026, 8, 27), date_to=date(2026, 8, 26)
        )


# -- повторы ----------------------------------------------------------------


def test_transient_failure_is_retried_and_data_is_not_lost() -> None:
    """Сбой чанка лечится перезапросом — данные приходят целыми.

    Ровно то, что описано в архиве про BOM и обрывы: «дошло до 100 % —
    данные целые».
    """
    candles = minutes_from(msk(2026, 8, 26, 10, 0), 6)
    attempts_made = {"n": 0}

    def handler(url: str) -> bytes:
        if "start=0" in url:
            attempts_made["n"] += 1
            if attempts_made["n"] <= 2:
                raise IssTransportError("соединение сброшено")
            return iss_body(candles)
        return iss_body([])

    transport = FakeTransport(handler)
    client = IssClient(transport, pause=0, sleep=no_sleep, attempts=4)
    result = client.minutes("MXU6", market=FUTURES, date_from=date(2026, 8, 26), date_to=date(2026, 8, 26))

    assert len(result.candles) == 6
    assert result.retries == 2


def test_exhausted_attempts_raise_instead_of_returning_half() -> None:
    """Исчерпание попыток — исключение, а не половина истории как целая.

    Архивный загрузчик в этом месте возвращал накопленное. Недокачанный
    отрезок, отданный как полный, — это тихая потеря свечей.
    """
    first_one = minutes_from(msk(2026, 8, 26, 10, 0), 500)

    def handler(url: str) -> bytes:
        if "start=0" in url:
            return iss_body(first_one)
        raise IssTransportError("сеть пропала")

    client = IssClient(FakeTransport(handler), pause=0, sleep=no_sleep, attempts=3)
    with pytest.raises(IssTransportError, match="3 попыток"):
        client.minutes("MXU6", market=FUTURES, date_from=date(2026, 8, 26), date_to=date(2026, 8, 26))


def test_broken_payload_is_retried_too() -> None:
    """Обрезанное тело лечится тем же перезапросом, что и обрыв сокета."""
    candles = minutes_from(msk(2026, 8, 26, 10, 0), 4)
    attempts_made = {"n": 0}

    def handler(url: str) -> bytes:
        if "start=0" in url:
            attempts_made["n"] += 1
            return b'{"candles": {"colum' if attempts_made["n"] == 1 else iss_body(candles)
        return iss_body([])

    client = IssClient(FakeTransport(handler), pause=0, sleep=no_sleep)
    result = client.minutes("MXU6", market=FUTURES, date_from=date(2026, 8, 26), date_to=date(2026, 8, 26))
    assert len(result.candles) == 4
    assert result.retries == 1


def test_sleep_between_attempts_grows() -> None:
    """Пауза между попытками нарастает — как в архивном загрузчике."""
    pauses: list[float] = []

    def handler(_url: str) -> bytes:
        raise IssTransportError("нет связи")

    client = IssClient(
        FakeTransport(handler), pause=0, sleep=pauses.append, attempts=4
    )
    with pytest.raises(IssTransportError):
        client.minutes("MXU6", market=FUTURES, date_from=date(2026, 8, 26), date_to=date(2026, 8, 26))
    assert pauses == [1, 3, 5]


def test_client_refuses_zero_attempts() -> None:
    with pytest.raises(ValueError):
        IssClient(FakeTransport(pages_handler([])), attempts=0)


def test_unsorted_page_does_not_look_like_a_loop() -> None:
    """Неотсортированная страница не должна выглядеть зацикливанием.

    Порядок строк внутри страницы сервер не обещает. Если считать сдвиг
    по последней строке, загрузка целых данных упала бы на ровном месте.
    """
    first_one = minutes_from(msk(2026, 8, 26, 10, 0), 4)
    second_one = minutes_from(msk(2026, 8, 26, 11, 0), 4)
    transport = FakeTransport(
        pages_handler([iss_body(list(reversed(first_one))), iss_body(list(reversed(second_one)))])
    )
    result = IssClient(transport, pause=0, sleep=no_sleep).minutes(
        "MXU6", market=FUTURES, date_from=date(2026, 8, 26), date_to=date(2026, 8, 26)
    )
    assert len(result.candles) == 8


# -- с ISS берутся только минутки -------------------------------------------


@pytest.mark.parametrize("interval", [10, 60, 24, 7, 31])
def test_only_minute_candles_are_taken_from_iss(interval: int) -> None:
    """Любой интервал кроме минутного — отказ, а не свеча с выдуманной полнотой.

    Свеча, собранная **сервером**, не сообщает, сколько минут интервала в ней
    действительно было. Прежде здесь ставилось `filled_minutes` = длина
    интервала: скачанная десятиминутка объявлялась полной всегда. Своя же
    сборка (`build_bars`) считает полноту честно — и при настройке движка
    «пропускать неполные свечи» один и тот же отрезок давал **разный ряд
    баров** в зависимости от способа загрузки.
    """
    transport = FakeTransport(pages_handler([iss_body(minutes_from(msk(2026, 8, 26, 10, 0), 2))]))
    client = IssClient(transport, pause=0, sleep=no_sleep)
    with pytest.raises(ValueError, match="только минутные"):
        client.candles(
            "MXU6", market=FUTURES,
            date_from=date(2026, 8, 26), date_to=date(2026, 8, 26), interval=interval,
        )


def test_the_interval_is_checked_before_the_first_request() -> None:
    """Отказ случается на границе, а не после похода в сеть."""
    transport = FakeTransport(pages_handler([iss_body(minutes_from(msk(2026, 8, 26, 10, 0), 2))]))
    with pytest.raises(ValueError, match="только минутные"):
        IssClient(transport, pause=0, sleep=no_sleep).candles(
            "MXU6", market=FUTURES,
            date_from=date(2026, 8, 26), date_to=date(2026, 8, 26), interval=60,
        )
    assert transport.urls == [], "запрос всё-таки ушёл"


def test_parse_candles_refuses_a_non_minute_interval() -> None:
    """Чистая функция разбора отвергает то же самое — она публичная."""
    body = iss_body(minutes_from(msk(2026, 8, 26, 10, 0), 2))
    with pytest.raises(ValueError, match="только минутные"):
        parse_candles(body, interval=10)


def test_a_downloaded_candle_claims_only_the_completeness_it_knows() -> None:
    """Минутка сообщает «одна минута из одной» — и ничего сверх этого.

    Единственная полнота, которую этот источник знает достоверно.
    Полноту свечи любого другого размера считает `build_bars`, из минуток.
    """
    (candle,) = parse_candles(iss_body(minutes_from(msk(2026, 8, 26, 10, 5), 1)))
    assert candle.timeframe == MINUTE
    assert candle.filled_minutes == 1
    assert not candle.is_partial


@pytest.mark.parametrize("span", [599, 3599, 60])
def test_a_candle_longer_than_a_minute_is_refused_by_its_own_data(span: int) -> None:
    """Размер свечи проверяется по колонке `end`, а не по параметру вызова.

    Дыра, которую это закрывает: `candles_url(interval=10)` → транспорт →
    `parse_candles(body)` с умолчанием `interval=1`. Проверка по параметру
    молчала, `put_minutes` пропускала (она проверяет **тип**, а тип честно
    говорит «минутка»), и в базе оказывались десятиминутки под видом минуток.
    Здесь ответ приходит с честным `end` — и разбор отказывает, хотя
    `interval` в вызове минутный.
    """
    body = iss_body(minutes_from(msk(2026, 8, 26, 10, 0), 2), span_seconds=span)
    with pytest.raises(IssPayloadError, match="длиннее минуты"):
        parse_candles(body)


def test_candles_url_refuses_a_non_minute_interval() -> None:
    """Адрес чего-то кроме минуток слой не строит вовсе.

    Функция экспортирована из пакета, и без проверки здесь она была обходным
    путём мимо всех остальных: адрес собирался, транспорт ходил, разбор верил
    умолчанию.
    """
    with pytest.raises(ValueError, match="только минутные"):
        candles_url(
            "MXU6", market=FUTURES,
            date_from=date(2026, 8, 26), date_to=date(2026, 8, 26), interval=10,
        )


def test_minutes_refuses_a_foreign_interval_instead_of_dropping_it() -> None:
    """`minutes(interval=10)` — отказ, а не пять тысяч минуток молча.

    Здесь стояло `kwargs.pop("interval", None)`: вызывающий просил пятьсот
    десятиминуток, получал пять тысяч минуток и ни слова об этом. Правило
    переставало быть правилом ровно в методе, названном единственным
    разрешённым входом.
    """
    transport = FakeTransport(pages_handler([iss_body(minutes_from(msk(2026, 8, 26, 10, 0), 2))]))
    with pytest.raises(ValueError, match="только минутные"):
        IssClient(transport, pause=0, sleep=no_sleep).minutes(
            "MXU6", market=FUTURES,
            date_from=date(2026, 8, 26), date_to=date(2026, 8, 26), interval=10,
        )
    assert transport.urls == [], "запрос всё-таки ушёл"


def test_minutes_still_accepts_an_explicit_minute_interval() -> None:
    transport = FakeTransport(pages_handler([iss_body(minutes_from(msk(2026, 8, 26, 10, 0), 2))]))
    result = IssClient(transport, pause=0, sleep=no_sleep).minutes(
        "MXU6", market=FUTURES,
        date_from=date(2026, 8, 26), date_to=date(2026, 8, 26), interval=1,
    )
    assert len(result.candles) == 2


# -- код ответа: что повторять, а что нет ------------------------------------


def test_a_client_error_is_not_retried() -> None:
    """404 на опечатке в тикере — отказ сразу, а не четыре попытки с паузами.

    Повторы стоят 1 + 3 + 5 секунд на кусок. При годовой догрузке (13 кусков)
    это две минуты ожидания вместо мгновенного отказа с внятным текстом,
    и ни одна из попыток не может превратить «такого нет» в данные.
    """
    pauses: list[float] = []

    def handler(_url: str) -> bytes:
        raise IssHttpError(404)

    client = IssClient(FakeTransport(handler), pause=0, sleep=pauses.append, attempts=4)
    with pytest.raises(IssHttpError) as refusal:
        client.minutes("MXU6", market=FUTURES, date_from=date(2026, 8, 26), date_to=date(2026, 8, 26))

    assert refusal.value.status == 404
    assert "проверьте тикер" in str(refusal.value)
    assert pauses == [], "на безнадёжный код всё-таки ждали"


@pytest.mark.parametrize("status", [429, 408, 500, 503])
def test_a_busy_server_is_retried(status: int) -> None:
    """«Занято» повторяется: 429, 408 и все 5xx — временные, а не окончательные."""
    candles = minutes_from(msk(2026, 8, 26, 10, 0), 3)
    attempts_made = {"n": 0}

    def handler(url: str) -> bytes:
        if "start=0" in url:
            attempts_made["n"] += 1
            if attempts_made["n"] == 1:
                raise IssTransportError(f"сервер ISS ответил {status}")
            return iss_body(candles)
        return iss_body([])

    client = IssClient(FakeTransport(handler), pause=0, sleep=no_sleep, attempts=4)
    result = client.minutes("MXU6", market=FUTURES, date_from=date(2026, 8, 26), date_to=date(2026, 8, 26))
    assert len(result.candles) == 3
    assert result.retries == 1


@pytest.mark.parametrize(
    ("status", "permanent"),
    [(404, True), (400, True), (403, True), (429, False), (408, False), (503, False)],
)
def test_which_statuses_the_transport_calls_permanent(status: int, permanent: bool) -> None:
    """Разбиение кодов задано таблицей, а не «по ощущению».

    Проверяется сам транспорт: он решает, чем обернуть ответ, и от этого
    зависит, будет ли повтор.
    """
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="нет")

    transport_under_test = HttpxTransport(httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(IssHttpError if permanent else IssTransportError):
        transport_under_test.get("https://iss.moex.com/iss/x.json", timeout=1.0)


# -- остановка ---------------------------------------------------------------


def test_stop_breaks_the_paging_between_pages() -> None:
    """Просьба остановиться прерывает обход, а не ждёт его конца.

    Так закрывается окно, пока идёт годовая догрузка: без этого `close()`
    стоит в очереди за загрузкой и программа висит минутами.
    """
    client = IssClient(FakeTransport(lambda _u: iss_body([])), pause=0, sleep=no_sleep)
    client.stop()

    assert client.stopped
    with pytest.raises(IssStopped, match="остановлена"):
        client.minutes("MXU6", market=FUTURES, date_from=date(2026, 8, 26), date_to=date(2026, 8, 26))


def test_stop_in_the_middle_keeps_what_was_asked_before() -> None:
    """Остановка застаёт обход между страницами: запрошенное уже записано выше."""
    pages_served = {"n": 0}
    client = IssClient(FakeTransport(lambda _u: b""), pause=0, sleep=no_sleep)

    def handler(_url: str) -> bytes:
        pages_served["n"] += 1
        if pages_served["n"] == 2:
            client.stop()
        return iss_body(minutes_from(msk(2026, 8, 26, 10, 0) + timedelta(hours=pages_served["n"]), 3))

    client._transport = FakeTransport(handler)  # noqa: SLF001 - подмена транспорта в тесте
    with pytest.raises(IssStopped):
        client.minutes("MXU6", market=FUTURES, date_from=date(2026, 8, 26), date_to=date(2026, 8, 26))
    assert pages_served["n"] == 2, "остановка сработала не между страницами"


# -- дубль ловится по минуте, а не по моменту --------------------------------


def test_a_duplicate_with_seconds_is_caught_at_the_client_too() -> None:
    """Одна минута, пришедшая дважды с разными секундами, — один дубль.

    Правило «ключ минутки — минута» объявлено правилом всего слоя
    (`market.candles.floor_to_minute`). Здесь ключом был момент: `10:05:00`
    и `10:05:07` расходились по разным ключам, и одна и та же минута уезжала
    в хранилище дважды — там её ловило уже другое правило и другим счётчиком.
    """
    on_the_minute = minutes_from(msk(2026, 8, 26, 10, 0), 3)
    with_seconds = [c.replace(time=c.time.replace(second=7)) for c in on_the_minute]
    transport = FakeTransport(pages_handler([iss_body(on_the_minute), iss_body(with_seconds)]))

    result = IssClient(transport, pause=0, sleep=no_sleep).minutes(
        "MXU6", market=FUTURES, date_from=date(2026, 8, 26), date_to=date(2026, 8, 26)
    )
    assert result.duplicates == 3
    assert len(result.candles) == 3


# -- карточка инструмента (B-021) -------------------------------------------
#
# Замер 05.09.2026 одним живым запросом, по нему построены все тела ниже:
#   MXU6  MINSTEP 25,0   STEPPRICE 25,0       → 1,00 ₽ за пункт
#   RIU6  MINSTEP 10,0   STEPPRICE 17,37744   → 1,74
#   BRV6  MINSTEP 0,01   STEPPRICE 8,68872    → 868,87

# Заготовка карточки и её колонки лежат в `market_helpers`: тем же телом
# ответа пользуется `test_market_point.py`, и две копии одной заготовки
# в этом проекте уже расходились молча.


def test_the_url_of_the_instrument_card_has_no_board_in_the_path() -> None:
    """Режим торгов приходит колонкой, а не стоит в адресе.

    У акции карточек две — по одной на режим, — и адрес с зашитым режимом
    вернул бы одну из них молча. Выбор идёт по `BOARDID` в разборе.
    """
    url = securities_url("MXU6", market=FUTURES)
    assert url == (
        "https://iss.moex.com/iss/engines/futures/markets/forts"
        "/securities/MXU6.json?iss.meta=off"
    )
    assert "/boards/" not in url


def test_the_card_gives_the_cost_of_a_point_of_the_index_future() -> None:
    """У фьючерса на индекс МосБиржи пункт стоит ровно рубль.

    ⚠️ **Это сторож сверки с прототипом.** 127 сделок из 127 посчитаны
    при 1 ₽ за пункт; подстановка с биржи обязана давать ту же единицу,
    иначе поедут все деньги эталонного прогона.
    """
    spec = parse_security(security_body(MXU6_ROW), secid="MXU6", market=FUTURES)
    assert spec.ruble_per_point == pytest.approx(1.0)
    assert spec.price_step == pytest.approx(25.0)
    assert spec.step_price == pytest.approx(25.0)
    assert spec.board == "RFUD"


@pytest.mark.parametrize(
    ("step", "step_price", "expected"),
    [(25.0, 25.0, 1.0), (10.0, 17.37744, 1.737744), (0.01, 8.68872, 868.872)],
)
def test_the_cost_of_a_point_is_the_step_price_over_the_step(
    step: float, step_price: float, expected: float
) -> None:
    """Замер 05.09.2026 воспроизводится арифметикой слоя: индекс, РТС, Брент."""
    row = {**MXU6_ROW, "MINSTEP": step, "STEPPRICE": step_price}
    spec = parse_security(security_body(row), secid="X", market=FUTURES)
    assert spec.ruble_per_point == pytest.approx(expected)


def test_the_card_carries_the_exchange_fee_and_the_margin() -> None:
    """Биржевой сбор и ГО биржа отдаёт тем же запросом — их не надо искать руками.

    Сбор здесь **биржевой**, не тариф брокера: тариф считается от него
    и обычно больше. Умолчание проекта — 14 ₽ целиком, биржа 05.09.2026
    объявляет по `MXU6` 14,68 ₽ только со своей стороны.
    """
    spec = parse_security(security_body(MXU6_ROW), secid="MXU6", market=FUTURES)
    assert spec.exchange_fee == pytest.approx(14.68)
    assert spec.scalper_fee == pytest.approx(7.34)
    assert spec.initial_margin == pytest.approx(23124.62)
    assert spec.lot == pytest.approx(1.0)


def test_a_card_without_a_step_price_says_so_instead_of_answering_one() -> None:
    """Нет `STEPPRICE` — ответ `None`, а не подставленная единица.

    У акций этой колонки нет вовсе. Подставленная за биржу единица
    выглядела бы подтверждённой величиной, а стоит она по Бренту
    в 869 раз меньше правды.
    """
    row = {**MXU6_ROW, "STEPPRICE": None}
    spec = parse_security(security_body(row), secid="MXU6", market=FUTURES)
    assert spec.step_price is None
    assert spec.ruble_per_point is None


def test_an_unknown_instrument_is_named_and_not_retried() -> None:
    """Пустая таблица — отказ вслух, и **не** повторяемый.

    Неизвестный тикер приходит кодом 200 с пустой таблицей. Будь это
    `IssPayloadError`, клиент повторил бы его четыре раза с паузами 1, 3, 5
    секунд — девять секунд за опечатку, которая у фьючерса штатна:
    тикер меняется с каждой экспирацией.
    """
    transport = FakeTransport(lambda _url: security_body())
    client = IssClient(transport, attempts=4, pause=0, sleep=no_sleep)
    with pytest.raises(IssUnknownInstrument, match="MXZ9"):
        client.security("MXZ9", market=FUTURES)
    assert len(transport.urls) == 1, (
        f"опечатка в тикере стоила {len(transport.urls)} запросов вместо одного"
    )


def test_a_card_of_another_board_is_refused_not_taken() -> None:
    """Строка чужого режима торгов не берётся молча — это дефект `B-016`.

    У инструмента бывает несколько режимов, и у каждого свои шаг цены
    и стоимость шага. Взять первую строку значит показать деньги другого
    инструмента под именем нашего.
    """
    row = {**MXU6_ROW, "BOARDID": "SPBFUT"}
    with pytest.raises(IssUnknownInstrument, match="SPBFUT"):
        parse_security(security_body(row), secid="MXU6", market=FUTURES)


def test_the_right_board_is_picked_out_of_several() -> None:
    """Из нескольких строк берётся строка нужного режима, а не первая."""
    other = {**MXU6_ROW, "BOARDID": "SPBFUT", "MINSTEP": 1.0, "STEPPRICE": 100.0}
    spec = parse_security(
        security_body(other, MXU6_ROW), secid="MXU6", market=FUTURES
    )
    assert spec.board == "RFUD"
    assert spec.ruble_per_point == pytest.approx(1.0)


def test_a_card_without_a_price_step_is_refused() -> None:
    """Без `MINSTEP` считать стоимость пункта не от чего — отказ, а не ноль."""
    row = {**MXU6_ROW, "MINSTEP": None}
    with pytest.raises(IssPayloadError, match="шаг цены"):
        parse_security(security_body(row), secid="MXU6", market=FUTURES)


def test_the_columns_of_the_card_are_read_by_name_not_by_order() -> None:
    """Перестановка колонок ответ не портит: они читаются по именам.

    Чтение по порядку работает ровно до дня, когда ISS добавит колонку, —
    и тогда стоимость шага молча станет чем-нибудь другим.
    """
    shuffled = tuple(reversed(SECURITY_COLUMNS))
    spec = parse_security(
        security_body(MXU6_ROW, columns=shuffled), secid="MXU6", market=FUTURES
    )
    assert spec.ruble_per_point == pytest.approx(1.0)
    assert spec.exchange_fee == pytest.approx(14.68)


def test_the_client_asks_the_card_at_the_right_address() -> None:
    """Клиент ходит по адресу карточки и отдаёт разобранное."""
    transport = FakeTransport(lambda _url: security_body(MXU6_ROW))
    spec = IssClient(transport, pause=0, sleep=no_sleep).security(
        "MXU6", market=FUTURES
    )
    assert transport.urls == [securities_url("MXU6", market=FUTURES)]
    assert spec.secid == "MXU6"
