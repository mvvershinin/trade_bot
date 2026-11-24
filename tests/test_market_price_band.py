"""Планка биржи `HIGHLIMIT`/`LOWLIMIT`: разбор ответа ISS и поход к бирже на одну попытку.

Решение 0067: выход по концу окна ставится по планке биржи, а не ответила
биржа — по проценту от закрытия. Здесь стерегутся три обещания слоя данных:

* из настоящего ответа ISS берутся границы строки `BOARDID=RFUD`, а негодная
  граница — пустая, ноль, не число, нижняя не ниже верхней — превращается
  в причину словами, а не в исключение и не в число;
* к бирже идёт **одна** попытка с таймаутом вызывающего — по той же сети,
  что задана фасаду, а не по новой;
* фасад ждёт не дольше таймаута, даже когда биржа висит или поток сети
  занят чужим походом, а не начатый к сроку запрос к бирже не уходит.

Сеть подставная: настоящий ответ лежит в `tests/fixtures/`.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from market.candles import MSK
from market.iss import (
    FUTURES,
    IssClient,
    IssTransportError,
    PriceBand,
    ask_price_band,
    parse_price_band,
    securities_url,
)
from market.worker import MarketWorker, WorkerClosed

#: Настоящий ответ `securities_url("MXZ6", market=FUTURES)`, снятый 04.11.2026
#: около 19:00 МСК (`marketdata.SYSTIME` 18:59:33). 04.11 — праздник (День
#: народного единства), и это была **праздничная сессия**: торги шли
#: (14 335 сделок за день), а данные по ГО помечены 10:00:04 (`IMTIME`).
#: Своего времени у границ в ответе нет (`quality/FINDINGS.md`, 04.11.2026).
REAL_ANSWER = pathlib.Path(__file__).parent / "fixtures" / "iss-MXZ6-securities-2026-11-04.json"

#: Что в нём стоит. Записано глазами из файла, а не вычислено разбором:
#: сверка разбора с самим собой зеленела бы на любой ошибке.
REAL_HIGH = 252_550.0
REAL_LOW = 237_850.0

ANSWERED = datetime(2026, 11, 4, 18, 59, 40, tzinfo=MSK)


def real_document() -> dict[str, object]:
    document: dict[str, object] = json.loads(REAL_ANSWER.read_bytes().decode("utf-8-sig"))
    return document


def with_row(**changes: object) -> bytes:
    """Настоящий ответ с правкой строки RFUD. `...` вместо значения — убрать колонку."""
    document = real_document()
    block = document["securities"]
    assert isinstance(block, dict)
    columns: list[str] = list(block["columns"])
    row = dict(zip(columns, block["data"][0], strict=True))
    for name, value in changes.items():
        if value is Ellipsis:
            row.pop(name)
        else:
            row[name] = value
    block["columns"] = list(row)
    block["data"] = [list(row.values())]
    return json.dumps(document).encode()


def band_of(payload: bytes) -> PriceBand:
    return parse_price_band(payload, secid="MXZ6", market=FUTURES, answered_at=ANSWERED)


class Recording:
    """Транспорт, запоминающий адрес и таймаут каждого запроса. Сети нет."""

    def __init__(self, serve: Callable[[str], bytes]) -> None:
        self._serve = serve
        self.asked: list[tuple[str, float]] = []

    def get(self, url: str, *, timeout: float) -> bytes:
        self.asked.append((url, timeout))
        return self._serve(url)


def real(_url: str) -> bytes:
    return REAL_ANSWER.read_bytes()


def fails_then_answers() -> Callable[[str], bytes]:
    """Первый запрос — обрыв сети, второй — настоящий ответ."""
    calls: list[str] = []

    def serve(url: str) -> bytes:
        calls.append(url)
        if len(calls) == 1:
            raise IssTransportError("соединение сброшено")
        return real(url)

    return serve


def never_sleep(seconds: float) -> None:
    raise AssertionError(f"пауза повтора {seconds} с — попытка должна быть одна")


# -- разбор настоящего ответа -----------------------------------------------


def test_the_real_answer_gives_the_band_of_the_contract() -> None:
    band = band_of(REAL_ANSWER.read_bytes())

    assert band.usable
    assert (band.secid, band.high, band.low) == ("MXZ6", REAL_HIGH, REAL_LOW)
    assert band.answered_at == ANSWERED
    assert "от 237850 до 252550" in band.told, band.told
    assert "RFUD" in band.told and "18:59:40 МСК" in band.told, band.told


def test_the_band_comes_from_the_rfud_row_even_when_it_is_not_first() -> None:
    document = real_document()
    block = document["securities"]
    assert isinstance(block, dict)
    columns: list[str] = block["columns"]
    foreign = list(block["data"][0])
    foreign[columns.index("BOARDID")] = "RFUQ"
    foreign[columns.index("HIGHLIMIT")] = 999_999.0
    foreign[columns.index("LOWLIMIT")] = 1.0
    block["data"].insert(0, foreign)

    band = band_of(json.dumps(document).encode())

    assert (band.high, band.low) == (REAL_HIGH, REAL_LOW)


BAD_BOUNDS = [
    pytest.param(None, "пуста", id="null"),
    pytest.param("", "пуста", id="empty-string"),
    pytest.param(0, "не больше нуля", id="zero"),
    pytest.param(-25.0, "не больше нуля", id="negative"),
    pytest.param("abc", "не число", id="text"),
    pytest.param("252550", "не число", id="digits-as-text"),
    pytest.param(True, "не число", id="bool"),
    pytest.param(float("nan"), "не конечное число", id="nan"),
    pytest.param(float("inf"), "не конечное число", id="inf"),
    pytest.param(Ellipsis, "колонки в ответе нет", id="no-column"),
]


@pytest.mark.parametrize(("column", "side"), [("HIGHLIMIT", "верхняя"), ("LOWLIMIT", "нижняя")])
@pytest.mark.parametrize(("raw", "words"), BAD_BOUNDS)
def test_a_bad_bound_is_refused_in_words_not_raised_and_not_used(
    column: str, side: str, raw: object, words: str
) -> None:
    band = band_of(with_row(**{column: raw}))

    assert not band.usable
    assert (band.high, band.low) == (None, None), "половина планки отдана как годная"
    assert column in band.told and side in band.told and words in band.told, band.told
    assert band.answered_at == ANSWERED, "ответ пришёл — момент его прихода известен"


@pytest.mark.parametrize(
    ("high", "low"),
    [
        pytest.param(240_000.0, 240_000.0, id="equal"),
        pytest.param(REAL_LOW, REAL_HIGH, id="swapped"),
    ],
)
def test_a_band_with_the_low_not_below_the_high_is_refused(high: float, low: float) -> None:
    band = band_of(with_row(HIGHLIMIT=high, LOWLIMIT=low))

    assert not band.usable
    assert (band.high, band.low) == (None, None)
    assert "LOWLIMIT ≥ HIGHLIMIT" in band.told, band.told


def test_the_moment_of_the_answer_is_told_in_moscow_time() -> None:
    in_utc = datetime(2026, 11, 4, 15, 59, 40, tzinfo=UTC)

    band = parse_price_band(
        REAL_ANSWER.read_bytes(), secid="MXZ6", market=FUTURES, answered_at=in_utc
    )

    assert band.answered_at is not None and band.answered_at.utcoffset() == MSK.utcoffset(None)
    assert "18:59:40 МСК" in band.told, band.told
    with pytest.raises(ValueError, match="без часового пояса"):
        parse_price_band(
            REAL_ANSWER.read_bytes(), secid="MXZ6", market=FUTURES,
            answered_at=ANSWERED.replace(tzinfo=None),
        )


# -- клиент на одну попытку ---------------------------------------------------


def test_the_single_try_client_keeps_the_network_and_takes_the_timeout() -> None:
    transport = Recording(real)
    once = IssClient(transport, attempts=4, timeout=40.0).single_try(timeout=2.5)

    band = ask_price_band(once, "MXZ6", market=FUTURES, clock=lambda: ANSWERED)

    assert band.usable
    assert transport.asked == [(securities_url("MXZ6", market=FUTURES), 2.5)]


def test_a_failure_is_not_retried_and_comes_back_as_words() -> None:
    transport = Recording(fails_then_answers())
    once = IssClient(transport, attempts=4, sleep=never_sleep).single_try(timeout=2.0)

    band = ask_price_band(once, "MXZ6", market=FUTURES)

    assert len(transport.asked) == 1, f"запросов: {len(transport.asked)}"
    assert not band.usable and band.answered_at is None
    assert "MXZ6" in band.told and "соединение сброшено" in band.told, band.told


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"<html>busy</html>", id="not-json"),
        pytest.param(json.dumps({"securities": {"columns": ["SECID"], "data": []}}).encode(),
                     id="unknown-contract"),
        pytest.param(with_row(BOARDID="RFUQ"), id="no-rfud-row"),
    ],
)
def test_an_answer_that_is_not_a_band_is_words_not_an_exception(body: bytes) -> None:
    once = IssClient(Recording(lambda _url: body)).single_try(timeout=2.0)

    band = ask_price_band(once, "MXZ6", market=FUTURES)

    assert not band.usable and (band.high, band.low) == (None, None)
    assert band.told.startswith("биржа не дала планку MXZ6: "), band.told


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("nan"), float("inf")])
def test_a_timeout_that_is_not_a_positive_number_is_refused(timeout: float) -> None:
    transport = Recording(real)

    with pytest.raises(ValueError, match="таймаут"):
        IssClient(transport).single_try(timeout=timeout)
    assert transport.asked == []


# -- фасад потока данных ------------------------------------------------------


def worker_on(
    tmp_path: pathlib.Path,
    transport: Recording,
    *,
    attempts: int = 1,
    sleep: Callable[[float], None] = never_sleep,
) -> MarketWorker:
    client = IssClient(transport, attempts=attempts, sleep=sleep)
    return MarketWorker(tmp_path / "candles.sqlite3", iss=client)


def test_the_worker_asks_the_band_over_the_facade_network_once(tmp_path: pathlib.Path) -> None:
    """Сеть фасада — та, по которой идёт планка; политика повторов — нет.

    Фасадный клиент здесь на четыре попытки с паузами: так собран клиент
    по умолчанию. Запрос планки обязан уйти по его транспорту ровно один раз
    и с таймаутом вызывающего.
    """
    transport = Recording(fails_then_answers())

    async def scenario() -> tuple[PriceBand, PriceBand]:
        async with worker_on(tmp_path, transport, attempts=4, sleep=never_sleep) as worker:
            first = await worker.price_band("MXZ6", timeout=3.0)
            second = await worker.price_band("MXZ6", timeout=3.0)
            return first, second

    before = datetime.now(MSK)
    first, second = asyncio.run(scenario())

    assert not first.usable and "соединение сброшено" in first.told, first.told
    assert second.usable and (second.high, second.low) == (REAL_HIGH, REAL_LOW)
    assert transport.asked == [(securities_url("MXZ6", market=FUTURES), 3.0)] * 2
    assert second.answered_at is not None
    assert second.answered_at.utcoffset() == MSK.utcoffset(None)
    assert before <= second.answered_at <= datetime.now(MSK)


def test_a_hanging_exchange_is_waited_no_longer_than_the_timeout(tmp_path: pathlib.Path) -> None:
    released = threading.Event()

    def hang(url: str) -> bytes:
        released.wait(10)
        return real(url)

    async def scenario() -> tuple[PriceBand, float]:
        async with worker_on(tmp_path, Recording(hang), attempts=1) as worker:
            started = time.monotonic()
            try:
                band = await worker.price_band("MXZ6", timeout=0.3)
                return band, time.monotonic() - started
            finally:
                released.set()

    band, waited = asyncio.run(scenario())

    assert waited < 1.5, f"ждали {waited:.2f} с при таймауте 0,3 с"
    assert not band.usable and band.answered_at is None
    assert band.told == "биржа не ответила про планку MXZ6 за 0,3 с", band.told


def test_a_band_queued_behind_a_hanging_trip_is_not_sent_late(tmp_path: pathlib.Path) -> None:
    """Поток сети занят чужим походом — планка ждёт срок и к бирже уже не идёт.

    Запрос, ушедший после срока, занял бы поток сети ещё на таймаут и
    принёс бы ответ, которого никто не ждёт.
    """
    asked = threading.Event()
    released = threading.Event()

    def first_hangs(url: str) -> bytes:
        if not asked.is_set():
            asked.set()
            released.wait(10)
        return real(url)

    transport = Recording(first_hangs)

    async def scenario() -> PriceBand:
        async with worker_on(tmp_path, transport, attempts=1) as worker:
            busy = asyncio.ensure_future(worker.point_value("MXZ6"))
            while not asked.is_set():
                await asyncio.sleep(0.01)
            try:
                band = await worker.price_band("MXZ6", timeout=0.2)
            finally:
                released.set()
            await busy
            # Очередь потока сети одна: эта карточка пройдёт после снятой планки.
            await worker.instrument_spec("MXZ6", market=FUTURES)
            return band

    band = asyncio.run(scenario())

    assert not band.usable and "за 0,2 с" in band.told, band.told
    assert len(transport.asked) == 2, f"к бирже ушло запросов: {len(transport.asked)}"


def test_a_closed_worker_refuses_the_band(tmp_path: pathlib.Path) -> None:
    transport = Recording(real)

    async def scenario() -> None:
        worker = worker_on(tmp_path, transport)
        await worker.close()
        await worker.price_band("MXZ6", timeout=1.0)

    with pytest.raises(WorkerClosed):
        asyncio.run(scenario())
    assert transport.asked == []
