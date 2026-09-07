"""Живые свечи в базу: счёт контрактов, запись закрытой минуты, связь, звено.

Сокета и брокера здесь нет: `candle_stream` подменяется сценарием заходов,
сессия — заглушкой. Часть проверок идёт на **настоящей** базе во временном
файле — там, где важно, что минута появляется в базе только после конца
минуты и что слой данных по-прежнему отвергает время без пояса.

Числа объёма — по §4.5 миниплана Э1-5: запись пробы 04.09.2026 даёт ровно
136 контрактов девятью приращениями 1 + 29 + 4 + 3 + 51 + 10 + 6 + 1 + 31.
Самой записи в репозитории нет — в `.docs/broker-api/20-ws-last-candle.md`
названы только первый и последний оборот, 224 900 и 30 579 800, — поэтому
девять снимков собраны руками так, чтобы сойтись с ними.

Токен брокера здесь не участвует ни в каком виде; в проверке подробного лога
стоит приметная подделка (подделка-для-теста), которая токеном не является.
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import inspect
import io
import json
import logging
import pathlib
import textwrap
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Final, cast

import pytest

from app import live_feed
from app.live_feed import LiveFeed, LiveLink, MinuteTally, Watchers, tally_after, to_minute
from app.main import _arguments, _live_feed
from broker.account import AccountSnapshot, Money
from broker.account import Position as AccountPosition
from broker.account import Side as AccountSide
from broker.clock import ClockWatch
from broker.errors import (
    BrokerError,
    NoConnection,
    NotFound,
    ServerFailure,
    TokenFileError,
    WrongAddress,
)
from broker.margin import MarginPerContract, MarginSource
from broker.redaction import FOREIGN_LOGGERS, MASK, RedactingFilter, RedactingSink
from broker.schedule import DailySchedule, Schedule, SessionKind, TradingStatus
from broker.session import ADDRESS_SENSITIVE, TRADING_STATUS_PATH, BrokerSession
from broker.stream import CandleSnapshot, StreamRejected
from market import MSK, Candle, CandleStore, MarketWorker, Source, WriteStats
from market.iss import Market
from market.point import PointValue
from ui.models import Connection, DecisionLevel, Settings

#: Приметная подделка — не токен брокера.
FAKE_WS_TOKEN = "FAKE-ws-verbose-log-1111-NOT-A-REAL-TOKEN"

UTC = timezone.utc
MINUTE_A = datetime(2026, 9, 4, 5, 38, tzinfo=UTC)  # 08:38 МСК
MINUTE_B = MINUTE_A + timedelta(minutes=1)
MINUTE_C = MINUTE_B + timedelta(minutes=1)

#: Девять слагаемых записи пробы (§4.5) и цены, по которым они наторгованы.
#: Две цены 224 900 подобраны так, чтобы итог сошёлся с записью: 30 579 800.
PROBE_INCREMENTS = (1, 29, 4, 3, 51, 10, 6, 1, 31)
PROBE_CLOSES = (224_900.0, 224_850.0, 224_850.0, 224_900.0, *([224_850.0] * 5))
PROBE_TOTAL = 30_579_800.0


def snapshot(
    minute: datetime, *, close: float, turnover: float, low: float | None = None
) -> CandleSnapshot:
    return CandleSnapshot(
        ticker="MXU6",
        class_code="SPBFUT",
        timeframe="M1",
        opened_at=minute,
        open=close,
        high=close,
        low=close if low is None else low,
        close=close,
        turnover=turnover,
    )


def wire(one: CandleSnapshot) -> str:
    """Снимок в том виде, в каком его шлёт сокет: плоский JSON, время с `Z`."""
    return json.dumps(
        {
            "responseType": "CandleStick",
            "ticker": one.ticker,
            "classCode": one.class_code,
            "timeFrame": one.timeframe,
            "dateTime": one.opened_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "open": one.open,
            "high": one.high,
            "low": one.low,
            "close": one.close,
            "volume": one.turnover,
        }
    )


def probe_snapshots() -> list[CandleSnapshot]:
    result: list[CandleSnapshot] = []
    turnover = 0.0
    for count, close in zip(PROBE_INCREMENTS, PROBE_CLOSES, strict=True):
        turnover += count * close
        result.append(snapshot(MINUTE_A, close=close, turnover=turnover))
    assert turnover == PROBE_TOTAL, "фикстура разошлась с записью пробы"
    return result


def fold(snapshots: Iterable[CandleSnapshot]) -> MinuteTally:
    tally: MinuteTally | None = None
    for one in snapshots:
        tally = tally_after(tally, one)
    assert tally is not None
    return tally


# ======================================================================= объём


def test_the_probe_minute_counts_exactly_136_contracts_by_increments() -> None:
    """Девять приращений записи пробы дают ровно 136 (миниплан Э1-5, §4.5).

    Проверяется не только итог, но и каждое слагаемое: после каждого снимка
    счёт вырастает ровно на названное число, а не «примерно».
    """
    running: list[int] = []
    tally: MinuteTally | None = None
    for one in probe_snapshots():
        tally = tally_after(tally, one)
        running.append(tally.contracts)
    assert tally is not None
    assert tally.contracts == 136 == sum(PROBE_INCREMENTS)
    assert running == [sum(PROBE_INCREMENTS[: index + 1]) for index in range(9)]
    assert tally.turnover == PROBE_TOTAL
    assert (tally.refused, tally.refusal, tally.minute) == (0, None, MINUTE_A)


def test_increments_are_not_the_same_as_dividing_the_total_by_the_last_price() -> None:
    """Пример §4.5: разброс 0,3 % и 40 000 контрактов — деление итога врёт на ~120.

    Первое приращение наторговано по 225 000, последнее — по 224 325 (на 0,3 %
    ниже). По приращениям минута даёт ровно 40 001. Итог, делённый на последнюю
    цену, — 40 121: сто двадцать контрактов, которых не было. Тест держит оба
    числа, чтобы подмена формулы на деление не прошла зелёной.
    """
    bulk = snapshot(MINUTE_A, close=225_000.0, turnover=40_000 * 225_000.0)
    last = snapshot(MINUTE_A, close=224_325.0, turnover=bulk.turnover + 224_325.0)
    tally = fold([bulk, last])
    assert tally.contracts == 40_001
    by_division = round(last.turnover / last.close)
    assert by_division == 40_121, "фикстура перестала различать два способа счёта"
    assert tally.contracts != by_division


def test_a_new_minute_starts_the_count_from_zero() -> None:
    """Первый снимок новой минуты — весь её оборот одним приращением (§4.5)."""
    previous = fold(probe_snapshots())
    first = tally_after(previous, snapshot(MINUTE_B, close=224_850.0, turnover=9 * 224_850.0))
    assert first.minute == MINUTE_B
    assert first.contracts == 9
    assert first.turnover == 9 * 224_850.0
    assert (first.refusal, first.refused) == (None, 0)


def test_a_fractional_increment_is_rounded_not_truncated() -> None:
    assert fold([snapshot(MINUTE_A, close=100.0, turnover=160.0)]).contracts == 2


def test_a_shrinking_turnover_is_refused_and_does_not_move_the_base() -> None:
    accepted = fold([snapshot(MINUTE_A, close=100.0, turnover=1_000.0)])
    refused = tally_after(accepted, snapshot(MINUTE_A, close=100.0, turnover=900.0))
    assert refused.refusal is not None
    assert "оборот уменьшился" in refused.refusal
    assert refused.refused == 1
    assert refused.contracts == accepted.contracts == 10
    assert refused.turnover == 1_000.0, "отвергнутый снимок сдвинул базу приращения"
    # Следующий годный снимок считается от прежней базы; минута остаётся сомнительной.
    healed = tally_after(refused, snapshot(MINUTE_A, close=100.0, turnover=1_100.0))
    assert (healed.contracts, healed.refusal, healed.refused) == (11, None, 1)


@pytest.mark.parametrize("close", [0.0, -1.0])
def test_a_non_positive_close_is_refused_aloud(close: float) -> None:
    accepted = fold([snapshot(MINUTE_A, close=100.0, turnover=1_000.0)])
    refused = tally_after(accepted, snapshot(MINUTE_A, close=close, turnover=2_000.0))
    assert refused.refusal is not None
    assert "не положительна" in refused.refusal
    assert (refused.contracts, refused.turnover, refused.refused) == (10, 1_000.0, 1)


# ================================================================ часовой пояс


def test_the_minute_keeps_its_time_zone() -> None:
    one = probe_snapshots()[-1]
    minute = to_minute(one, 136)
    assert minute.time.tzinfo is not None, "пояс снят — UTC брокера станет местным временем"
    assert minute.time == one.opened_at
    assert minute.volume == 136.0
    assert (minute.open, minute.high, minute.low, minute.close) == (
        one.open, one.high, one.low, one.close,
    )
    assert minute.timeframe.minutes == 1
    assert minute.unsettled is False


def test_the_data_layer_still_refuses_a_naive_minute(tmp_path: pathlib.Path) -> None:
    """Сторож пояса в слое данных на месте: без него три часа терялись бы молча."""
    naive = Candle(
        time=MINUTE_A.replace(tzinfo=None), open=1.0, high=1.0, low=1.0, close=1.0, volume=1.0
    )
    with (
        CandleStore(tmp_path / "guard.sqlite3") as store,
        pytest.raises(ValueError, match="без часового пояса"),
    ):
        store.put_minutes("MXU6", [naive], Source.BROKER)


def test_a_broker_minute_lands_in_the_base_at_the_same_instant(tmp_path: pathlib.Path) -> None:
    """05:38 UTC от брокера — это 08:38 МСК в базе, а не 05:38 МСК."""
    one = probe_snapshots()[-1]
    with CandleStore(tmp_path / "live.sqlite3") as store:
        store.put_minutes("MXU6", [to_minute(one, 136)], Source.BROKER)
        [stored] = store.minutes("MXU6")
    assert stored.time == one.opened_at
    assert stored.time.astimezone(MSK).strftime("%H:%M") == "08:38"
    assert stored.volume == 136.0


# ====================================================================== поток


class FakeSocket:
    """Сокет из очереди: сообщения по одному, до сигнала конца или отмены задачи."""

    END = object()

    def __init__(self, messages: Iterable[str] = (), *, then_close: bool = False) -> None:
        self.queue: asyncio.Queue[object] = asyncio.Queue()
        for message in messages:
            self.queue.put_nowait(message)
        if then_close:
            self.queue.put_nowait(self.END)
        self.delivered = 0

    def push(self, message: str) -> None:
        self.queue.put_nowait(message)

    def __aiter__(self) -> FakeSocket:
        return self

    async def __anext__(self) -> str:
        item = await self.queue.get()
        if item is self.END:
            raise StopAsyncIteration
        self.delivered += 1
        return str(item)


class FakeStream:
    """Подмена `candle_stream`: сценарий заходов — сокет либо отказ на входе.

    Когда сценарий исчерпан, заход получает пустой сокет, который молчит
    до отмены: так выглядит живое соединение без сделок, и так поток
    не уходит в бесконечный цикл повторов с нулевой паузой.
    """

    def __init__(self, *sessions: FakeSocket | BaseException) -> None:
        self.sessions = list(sessions)
        self.opened: list[FakeSocket] = []
        self.events: list[str] = []
        self.requests: list[tuple[str, str]] = []
        self.slow_close = 0.0

    @asynccontextmanager
    async def __call__(
        self, session: object, *, ticker: str, class_code: str, timeframe: str = "M1", url: str = ""
    ) -> AsyncIterator[FakeSocket]:
        self.requests.append((ticker, class_code))
        outcome = self.sessions.pop(0) if self.sessions else FakeSocket()
        if isinstance(outcome, BaseException):
            raise outcome
        self.opened.append(outcome)
        self.events.append(f"open#{len(self.opened)}")
        try:
            yield outcome
        finally:
            if self.slow_close:
                await asyncio.sleep(self.slow_close)
            self.events.append(f"close#{len(self.opened)}")


class Heard:
    """Получатели `Watchers` — списками, как они звучали."""

    def __init__(self) -> None:
        self.connections: list[Connection] = []
        self.closed: list[str] = []
        self.said: list[tuple[str, str, DecisionLevel]] = []
        #: Минута первого снимка каждого захода — то, с чего начинается
        #: догрузка пропущенного (`app/backfill.py`).
        self.leading: list[datetime] = []
        #: Растущая минута на каждом принятом снимке: инструмент и минутка.
        #: То, чего до `B-007` не было вовсе, — данные приходили и терялись.
        self.growing: list[tuple[str, Candle]] = []
        #: Остановки робота: событие и причина. Пусто — робот не остановлен.
        self.halts: list[tuple[str, str]] = []

    def watchers(self) -> Watchers:
        return Watchers(
            connection=self.connections.append,
            closed_minute=self.closed.append,
            growing_minute=lambda symbol, minute: self.growing.append((symbol, minute)),
            say=lambda event, reason, level: self.said.append((event, reason, level)),
            connected=self.leading.append,
            halt=lambda event, reason: self.halts.append((event, reason)),
        )

    def warnings(self) -> list[str]:
        return [reason for _, reason, level in self.said if level is DecisionLevel.WARNING]


class FakeWorker:
    """Поток данных без базы — с тем же жизненным циклом, что у `MarketWorker`.

    ⚠️ Дублёр повторяет **смысл** оригинала, а не тот минимум, при котором
    тесты замолчат. Оригинал (`market/worker.py`) создаёт соединение с базой
    внутри своего потока при `open()`, и до этого писать некуда: `put_minutes`
    отказывает вслух. Ровно из-за такого отказа `LiveFeed._listen` открывает
    базу сам — на чистой машине первый же `put_minutes` падал, и поток считал
    это обрывом связи.

    Дублёр, знавший один `put_minutes`, на `worker.opened` ронял поток
    `AttributeError`, и девять тестов ждали условия по три секунды и падали
    на `settle` — `B-001`. Поэтому повторяются четыре правила оригинала:

    * `opened` — правда только между `open()` и `close()`;
    * второе `open()` отказывает: это были бы два соединения к одной базе;
    * `open()` после `close()` отказывает: поток не возобновляется;
    * `put_minutes` до `open()` и после `close()` отказывает — иначе дублёр
      прощал бы запись мимо базы, ради которой ленивое открытие и появилось.

    Тексты отказов сокращены намеренно: здесь важен сам отказ, а не его
    редакция. Куски, по которым отказ узнаётся, оставлены те же — «не запущен»
    и «уже открыт», ровно они стерегут оригинал в `tests/test_market_worker.py`.
    """

    def __init__(self) -> None:
        self.written: list[tuple[str, list[Candle], Source]] = []
        #: Сколько раз базу открывали. Больше одного за сеанс означало бы,
        #: что поток переоткрывает её на каждом переподключении.
        self.opens = 0
        self._open = False
        self._closed = False
        #: Тикеры, по которым спрашивали карточку с биржи, по порядку.
        self.asked_point_value: list[str] = []
        #: Что отвечать на карточку: тикер → ответ биржи.
        self.point_values: dict[str, PointValue] = {}

    @property
    def opened(self) -> bool:
        return self._open

    async def open(self) -> FakeWorker:
        if self._closed:
            raise RuntimeError("поток данных остановлен, база закрыта")
        if self._open:
            raise RuntimeError("поток данных уже открыт: второе соединение с базой")
        self._open = True
        self.opens += 1
        return self

    async def close(self) -> None:
        self._closed = True
        self._open = False

    async def put_minutes(
        self, symbol: str, candles: Iterable[Candle], source: Source
    ) -> WriteStats:
        if not self._open:
            raise RuntimeError("поток данных не запущен: сначала `await worker.open()`")
        batch = list(candles)
        self.written.append((symbol, batch, source))
        return WriteStats(inserted=len(batch))

    async def point_value(
        self,
        symbol: str,
        *,
        markets: Iterable[Market] = (),
        client: object | None = None,
    ) -> PointValue:
        """Карточка инструмента с биржи. База для этого оригиналу не нужна.

        ⚠️ Отказ по закрытому потоку повторён намеренно: оригинал роняет
        `RuntimeError` на остановленном пуле, и звено обязано это пережить —
        опрос счёта не имеет права упасть из-за карточки инструмента.
        """
        if self._closed:
            raise RuntimeError("поток данных остановлен, база закрыта")
        self.asked_point_value.append(symbol)
        return self.point_values.get(
            symbol, PointValue(symbol, None, "биржа не отвечала: дублёр")
        )


def _worker_calls() -> set[str]:
    """Что живой поток просит у потока данных — разбором `app/live_feed.py`.

    Разбор, а не поиск по подстроке: имя берётся из выражения `self._worker.X`
    целиком, поэтому ни слово в комментарии, ни своё поле `_worker` в списке
    не окажутся.
    """
    tree = ast.parse(inspect.getsource(live_feed))
    return {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "_worker"
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "self"
    }


def _session_calls(owner: type = LiveFeed) -> set[str]:
    """Что класс просит у сессии брокера — разбором, а не поиском по подстроке.

    Тот же приём, что у `_worker_calls`, и по той же причине: имя берётся
    из выражения `self._session.X` целиком.

    ⚠️ Разбирается **один класс**, а не модуль. Сессию держат двое —
    `LiveFeed` и `LiveLink`, — и дублёры у них разные: у первого пустой
    объект из `feed_of`, у второго `FakeSession` в тестах звена. Разбор
    по всему модулю требовал бы от каждого дублёра уметь всё за обоих.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(owner)))
    return {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "_session"
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "self"
    }


def _session_attributes() -> set[str]:
    """Что у `BrokerSession` есть: методы класса и поля, заводимые в `__init__`.

    Одного `hasattr` по классу мало: `connection` и `clock` — поля экземпляра,
    и по классу их не видно. Строить настоящую сессию ради проверки имени
    значило бы тянуть в тест хранилище токена.
    """
    fields = {
        node.targets[0].attr
        for node in ast.walk(
            # `textwrap.dedent` обязателен: исходник метода приходит
            # с отступом класса, и `ast.parse` на нём падает.
            ast.parse(textwrap.dedent(inspect.getsource(BrokerSession.__init__)))
        )
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Attribute)
        and isinstance(node.targets[0].value, ast.Name)
        and node.targets[0].value.id == "self"
    }
    return fields | set(dir(BrokerSession))


def test_the_fake_session_answers_everything_the_feed_asks_of_the_real_one(
    monkeypatch, tmp_path: pathlib.Path
) -> None:
    """Подставная сессия отвечает на всё, что звено спрашивает у `BrokerSession`.

    Тот же сторож, что у потока данных, и поставлен по такому же промаху,
    случившемуся 05.09.2026: поток стал спрашивать `session.clock` на каждой
    закрытой минуте, а подставная сессия была пустым `SimpleNamespace`.
    Выходил `AttributeError` внутри задачи — поток снимался как «ошибка
    программы», а тесты этого не видели: минута пишется **до** вызова,
    и в базе всё выглядело правильно.
    """
    asked = _session_calls()
    assert asked, "разбор перестал находить обращения к сессии брокера"
    heard, worker = Heard(), FakeWorker()
    feed = feed_of(monkeypatch, FakeStream(FakeSocket([])), worker, heard)
    double = feed._session  # noqa: SLF001  # это и есть предмет проверки
    real = _session_attributes()
    for name in sorted(asked):
        assert name in real, (
            f"звено просит у сессии `{name}`, которого у `BrokerSession` нет"
        )
        assert hasattr(double, name), (
            f"подставная сессия не умеет `{name}`: задача упадёт "
            "`AttributeError`, а тест увидит «не дождались»"
        )


def test_the_worker_double_answers_everything_the_feed_asks_of_the_real_one() -> None:
    """Дублёр отвечает на всё, что поток спрашивает у настоящего `MarketWorker`.

    Сторож поставлен по `B-001`, и вот чем он там был бы полезен. Рабочий код
    оброс ленивым открытием базы (`worker.opened`, `worker.open()`), дублёр
    остался с одним `put_minutes` — и девять тестов сломались молча: поток
    падал `AttributeError` внутри задачи, а падение выглядело как «не дождались»
    по таймауту `settle` через три секунды. Здесь то же расхождение видно
    строкой с именем метода, и видно сразу.

    Сверяется не только наличие: свойство обязано остаться свойством,
    ожидаемый метод — ожидаемым, а список параметров — тем же. Дублёр,
    у которого `opened` стал методом, врал бы про `if not worker.opened`
    ровно наоборот: пустой метод — истина всегда.
    """
    asked = _worker_calls()
    assert asked >= {"opened", "open", "put_minutes"}, (
        f"разбор перестал находить обращения к потоку данных: {sorted(asked)}"
    )
    for name in sorted(asked):
        real = inspect.getattr_static(MarketWorker, name, None)
        assert real is not None, f"поток просит у потока данных `{name}`, которого нет"
        double = inspect.getattr_static(FakeWorker, name, None)
        assert double is not None, (
            f"дублёр не умеет `{name}`, а поток это просит: тесты упадут "
            f"по таймауту `settle`, а не с именем причины (`B-001`)"
        )
        assert isinstance(real, property) == isinstance(double, property), (
            f"`{name}`: у оригинала и дублёра разный вид — свойство против метода"
        )
        if isinstance(real, property):
            continue
        assert inspect.iscoroutinefunction(real) == inspect.iscoroutinefunction(double), (
            f"`{name}`: один ожидаемый (`async`), другой нет"
        )
        assert list(inspect.signature(real).parameters) == list(
            inspect.signature(double).parameters
        ), f"`{name}`: у дублёра другие параметры, чем у `MarketWorker`"


async def settle(condition: Callable[[], bool], what: str, seconds: float = 3.0) -> None:
    """Дать циклу событий дойти до условия; не дошёл — падение с именем условия."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while not condition():
        assert loop.time() < deadline, f"не дождались: {what}"
        await asyncio.sleep(0.001)


def feed_of(
    monkeypatch,
    stream: FakeStream,
    worker,
    heard: Heard,
    clock: ClockWatch | None = None,
) -> LiveFeed:
    """Поток с подставной сессией.

    ⚠️ У подставной сессии обязан быть **настоящий** `ClockWatch`: поток
    спрашивает его на каждой закрытой минуте (`_tell_clock`, `D-045`).
    Пустой `SimpleNamespace` давал `AttributeError` внутри задачи, и поток
    снимался как «ошибка программы» — а тесты, смотревшие только в базу,
    этого не видели: минута пишется до вызова. Поймано 05.09.2026.
    """
    monkeypatch.setattr(live_feed, "candle_stream", stream)
    monkeypatch.setattr(live_feed, "RETRY_FIRST", 0.0)
    monkeypatch.setattr(live_feed, "RETRY_CAP", 0.0)
    session: Any = SimpleNamespace(clock=clock or ClockWatch())
    return LiveFeed(
        session, worker, ticker="MXU6", class_code="SPBFUT", watchers=heard.watchers()
    )


def test_a_running_minute_is_not_in_the_base_until_the_next_one_begins(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """Лечение 🔴 ворот `/validate`: формирующаяся минута не пишется вовсе.

    Три снимка минуты A — в базе пусто. Первый снимок минуты B — в базе
    минута A с её последним снимком и контрактами по приращениям, минуты B нет.
    Остановка потока последнюю минуту не дописывает: следующего снимка не будет.
    """
    heard, worker = Heard(), MarketWorker(tmp_path / "live.sqlite3")
    socket = FakeSocket([wire(one) for one in probe_snapshots()[:3]])
    feed = feed_of(monkeypatch, FakeStream(socket), worker, heard)

    async def scenario() -> tuple[list[Candle], list[Candle]]:
        await worker.open()
        try:
            feed.start()
            await settle(lambda: socket.delivered == 3, "три снимка минуты A")
            await asyncio.sleep(0.02)  # дать неверной записи шанс случиться
            assert await worker.minutes("MXU6") == [], "формирующаяся минута попала в базу"
            socket.push(wire(snapshot(MINUTE_B, close=224_850.0, turnover=224_850.0)))
            await settle(lambda: heard.closed == ["MXU6"], "запись минуты A")
            stored = await worker.minutes("MXU6")
            feed.request_stop()
            await feed.aclose()
            return stored, await worker.minutes("MXU6")
        finally:
            await feed.aclose()
            await worker.close()

    stored, after_stop = asyncio.run(scenario())
    assert after_stop == stored, "остановка дописала минуту, конца которой не видели"
    [minute] = stored
    assert minute.time == MINUTE_A
    assert minute.volume == float(sum(PROBE_INCREMENTS[:3]))
    assert minute.close == PROBE_CLOSES[2]
    assert "Сомнительная минута" not in [event for event, _, _ in heard.said]


# ================================ сверка часов машины с брокером (`D-045`)


def clock_off_by(seconds: float) -> ClockWatch:
    """Часы, разошедшиеся с брокером на столько секунд. Плюс — наши отстают."""
    watch = ClockWatch()
    server = datetime(2026, 9, 5, 7, 30, tzinfo=UTC) + timedelta(seconds=seconds)
    watch.observed(
        server.strftime("%a, %d %b %Y %H:%M:%S GMT"),
        sent_at=datetime(2026, 9, 5, 7, 30, tzinfo=UTC),
        received_at=datetime(2026, 9, 5, 7, 30, tzinfo=UTC),
    )
    return watch


def run_a_closed_minute(monkeypatch, tmp_path: pathlib.Path, clock: ClockWatch) -> Heard:
    """Прогнать поток до закрытия одной минуты. Возвращает всё, что он сказал."""
    heard, worker = Heard(), MarketWorker(tmp_path / "live.sqlite3")
    socket = FakeSocket([wire(one) for one in probe_snapshots()[:1]])
    feed = feed_of(monkeypatch, FakeStream(socket), worker, heard, clock=clock)

    async def scenario() -> None:
        await worker.open()
        try:
            feed.start()
            await settle(lambda: socket.delivered == 1, "снимок минуты A")
            socket.push(wire(snapshot(MINUTE_B, close=224_850.0, turnover=224_850.0)))
            await settle(lambda: heard.closed == ["MXU6"], "запись минуты A")
        finally:
            await feed.aclose()
            await worker.close()

    asyncio.run(scenario())
    return heard


def test_a_machine_clock_out_of_step_is_said_out_loud(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """`D-045`. Разошедшиеся часы машины замечены и сказаны в журнал решений.

    Без этого часы, отставшие на две минуты, останавливают вход на весь день,
    а причина остаётся загадкой: снимок счёта протухает раньше, чем приходит.
    """
    heard = run_a_closed_minute(monkeypatch, tmp_path, clock_off_by(120))
    about = [row for row in heard.said if "Часы компьютера" in row[0]]
    assert about, f"про часы не сказано ни строки: {[row[0] for row in heard.said]}"
    event, reason, level = about[0]
    assert "отстают" in event, event
    assert "2 мин" in reason, reason
    assert level is DecisionLevel.ERROR, "остановка торговли подана как мелочь"
    assert "не подкручивает" in reason, "не сказано, что программа время не правит"


def test_a_machine_clock_in_step_says_nothing(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """Исправные часы в журнал не пишут: записи «всё как обычно» ему не нужны."""
    heard = run_a_closed_minute(monkeypatch, tmp_path, clock_off_by(1))
    assert not [row for row in heard.said if "Часы" in row[0]], heard.said


def test_an_unchecked_clock_is_not_reported_as_correct(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """Ни одного ответа с временем — молчание, а не «часы верны»."""
    heard = run_a_closed_minute(monkeypatch, tmp_path, ClockWatch())
    assert not [row for row in heard.said if "Часы" in row[0]], heard.said


def test_the_clock_complaint_does_not_take_the_stream_down(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """Сверка часов не роняет поток: минута записана, отказа связи нет.

    Сторож на конкретный промах 05.09.2026: `_tell_clock` падал
    `AttributeError` на подставной сессии, поток снимался как «ошибка
    программы», а тесты, смотревшие только в базу, этого не замечали —
    минута пишется до вызова.
    """
    heard = run_a_closed_minute(monkeypatch, tmp_path, clock_off_by(120))
    assert heard.closed == ["MXU6"], "минута не записана"
    assert not [
        row for row in heard.said if "Ошибка программы" in row[1]
    ], heard.said


# ============================================ растущая минута: рисунок, `B-007`


def test_every_snapshot_of_a_running_minute_goes_out_to_be_drawn(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """`B-007`. Каждый снимок идущей минуты уходит рисоваться — и ни один в базу.

    Слова владельца счёта: «рисует как-то с запаздыванием», «свечка
    не колеблется при получении сигнала, как я видел на бирже». Данные
    для колебания приходили и выбрасывались: рисовался только последний
    снимок и только после конца минуты.

    Сторож держит **обе** половины правки сразу, и порознь они бесполезны:
    снимок обязан уйти рисоваться (иначе свеча снова замрёт) и обязан
    не попасть в базу (иначе бар объявится закрытым раньше времени — это
    🔴 ворот `/validate` и `B-006` заодно).

    Проверяется и **содержимое** минутки, а не только число вызовов: объём
    растёт приращениями записи пробы, цена — та, что в снимке, признак
    `unsettled` стоит. Без сверки содержимого сюда прошла бы отправка одной
    и той же неизменной минутки девять раз — то есть свеча, которая
    «обновляется» и не меняется.
    """
    heard, worker = Heard(), MarketWorker(tmp_path / "live.sqlite3")
    probe = probe_snapshots()[:3]
    socket = FakeSocket([wire(one) for one in probe])
    feed = feed_of(monkeypatch, FakeStream(socket), worker, heard)

    async def scenario() -> list[Candle]:
        await worker.open()
        try:
            feed.start()
            await settle(lambda: len(heard.growing) == 3, "три растущие минутки")
            await asyncio.sleep(0.02)  # дать неверной записи шанс случиться
            assert await worker.minutes("MXU6") == [], (
                "растущая минута попала в базу: бар объявится закрытым, пока "
                "его последняя минута ещё набирается"
            )
            assert heard.closed == [], "минута объявлена законченной, не закончившись"
            return [minute for _, minute in heard.growing]
        finally:
            await feed.aclose()
            await worker.close()

    drawn = asyncio.run(scenario())

    assert {symbol for symbol, _ in heard.growing} == {"MXU6"}
    assert [minute.volume for minute in drawn] == [1.0, 30.0, 34.0], (
        "растущая свеча не растёт: объём по снимкам должен идти приращениями "
        f"записи пробы 1, 29, 4, а пришло {[m.volume for m in drawn]}"
    )
    assert [minute.close for minute in drawn] == list(PROBE_CLOSES[:3]), (
        "в рисунок ушла не цена снимка"
    )
    assert all(minute.time == MINUTE_A.astimezone(MSK) for minute in drawn), (
        "растущие минутки помечены разным временем — на графике их будет три"
    )
    assert all(minute.unsettled for minute in drawn), (
        "растущая минутка не помечена незакрытой: собранный с ней бар "
        "объявится закрытым, и по нему пойдёт прогон"
    )


def test_a_refused_snapshot_is_not_drawn_at_all(monkeypatch) -> None:
    """Отвергнутый снимок не рисуется — но и не молчит.

    Третий вопрос `B-007`: рисовать сомнительное нельзя (цена на графике
    неотличима от настоящей, а по графику смотрят глазами), молчать тоже
    нельзя. Поэтому отвергнутый снимок до рисунка не доходит, а первый
    отказ минуты уходит строкой в журнал — и в строке сказано, что свеча
    замерла, а не что программа зависла.
    """
    heard, worker = Heard(), FakeWorker()
    good = snapshot(MINUTE_A, close=100.0, turnover=1_000.0)  # 10 контрактов
    shrunk = snapshot(MINUTE_A, close=99.0, turnover=900.0)  # оборот убыл
    zero = snapshot(MINUTE_A, close=0.0, turnover=2_000.0)  # цена не положительна
    socket = FakeSocket([wire(good), wire(shrunk), wire(zero)])
    feed = feed_of(monkeypatch, FakeStream(socket), worker, heard)

    async def scenario() -> None:
        feed.start()
        try:
            await settle(lambda: socket.delivered == 3, "три снимка минуты A")
            await asyncio.sleep(0.02)  # дать лишнему рисунку шанс случиться
        finally:
            await feed.aclose()

    asyncio.run(scenario())

    assert [minute.close for _, minute in heard.growing] == [100.0], (
        "на график ушёл отвергнутый снимок: сомнительная цена на графике "
        f"неотличима от настоящей. Нарисовано {[m.close for _, m in heard.growing]}"
    )
    [doubt] = [reason for event, reason, _ in heard.said if event == "Сомнительная минута"]
    assert "растущая свеча стоит на прошлом принятом снимке" in doubt, (
        f"замершая свеча не объяснена человеку: {doubt!r}"
    )


def test_the_link_hands_a_growing_minute_to_the_port_without_a_run(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """Проводка `B-007`: растущая минута доходит до порта и ничего не запускает.

    Сторож на **шве**, парный к `test_a_closed_minute_asks_for_a_drawing…`.
    Проверяются оба конца: что делает звено и что поток зовёт именно его.
    Без второй половины переименование обработчика оставило бы проверку
    зелёной, а свечу — снова замершей.
    """
    monkeypatch.setattr(live_feed, "BrokerSession", lambda store: SimpleNamespace(
        stored=lambda: SimpleNamespace(permissions_were_loose=False),
    ))
    monkeypatch.setattr(live_feed, "TokenStore", lambda directory: directory)
    port: Any = FakePort()
    link = link_of(port, FakeWorker(), tmp_path / "userdata")
    minute = to_minute(snapshot(MINUTE_A, close=100.0, turnover=1_000.0), 10, unsettled=True)

    link._grow("MXU6", minute)  # noqa: SLF001 — предмет проверки и есть этот шов

    assert port.growing == [("MXU6", minute)], "растущая минута до порта не дошла"
    assert port.refreshed == [], (
        "растущая минута гонит прогон по всей истории: движок решал бы "
        "по бару, который ещё набирается"
    )
    assert port.drawn == [], (
        "растущая минута выдаёт себя за записанную: порт пойдёт читать её "
        "из базы, где её нет и не будет"
    )

    feed = link._build()  # noqa: SLF001 — то же
    assert feed._watch.growing_minute == link._grow, (  # noqa: SLF001 — то же
        "поток зовёт по растущей минуте не тот обработчик — проверка выше "
        "проверяет мёртвый код"
    )


def test_a_refused_snapshot_is_neither_written_nor_counted_and_is_said_once(monkeypatch) -> None:
    heard, worker = Heard(), FakeWorker()
    good = snapshot(MINUTE_A, close=100.0, turnover=1_000.0)  # 10 контрактов
    shrunk = snapshot(MINUTE_A, close=99.0, turnover=900.0)  # оборот убыл
    shrunk_again = snapshot(MINUTE_A, close=98.0, turnover=800.0)
    next_minute = snapshot(MINUTE_B, close=100.0, turnover=100.0)
    socket = FakeSocket([wire(good), wire(shrunk), wire(shrunk_again), wire(next_minute)])
    feed = feed_of(monkeypatch, FakeStream(socket), worker, heard)

    async def scenario() -> None:
        feed.start()
        try:
            await settle(lambda: len(worker.written) == 1, "запись минуты A")
        finally:
            await feed.aclose()

    asyncio.run(scenario())
    [(symbol, [candle], source)] = worker.written
    assert (symbol, source) == ("MXU6", Source.BROKER)
    assert candle.close == 100.0, "в базу ушёл отвергнутый снимок"
    assert candle.volume == 10.0, "отвергнутый снимок добавил контрактов"
    doubts = [reason for event, reason, _ in heard.said if event == "Сомнительная минута"]
    assert len(doubts) == 1, doubts
    assert "оборот уменьшился" in doubts[0]
    assert doubts[0] in heard.warnings()


def test_a_minute_with_nothing_accepted_is_not_written(monkeypatch) -> None:
    heard, worker = Heard(), FakeWorker()
    socket = FakeSocket(
        [
            wire(snapshot(MINUTE_A, close=0.0, turnover=1_000.0)),
            wire(snapshot(MINUTE_B, close=100.0, turnover=100.0)),
            wire(snapshot(MINUTE_C, close=100.0, turnover=100.0)),
        ]
    )
    feed = feed_of(monkeypatch, FakeStream(socket), worker, heard)

    async def scenario() -> None:
        feed.start()
        try:
            await settle(lambda: len(worker.written) == 1, "запись первой годной минуты")
        finally:
            await feed.aclose()

    asyncio.run(scenario())
    [(_, [candle], _)] = worker.written
    assert candle.time == MINUTE_B, "минута без единого принятого снимка попала в базу"
    # Текст сверяется слово в слово: его читает владелец счёта, и обещать
    # в нём догрузку, которой в этой версии нет, нельзя (шапка модуля).
    # Названы все четыре цены, а не одна `close`: свеча с нулевым `low`
    # выглядит касанием любого уровня выхода (`_shape_problem`).
    # Про график сказано отдельной фразой: отвергнутый снимок не рисуется
    # (`B-007`), и человек, глядящий на замершую свечу, обязан прочесть,
    # почему она замерла, а не гадать, что программа зависла.
    assert [reason for event, reason, _ in heard.said if event == "Сомнительная минута"] == [
        "Минута 08:38 МСК: цена не положительна: 0, 0, 0, 0. Снимок отброшен "
        "и на график не пошёл: растущая свеча стоит на прошлом принятом снимке. "
        "Объём и цены этой минуты могут быть неточными; догрузки с биржи "
        "в этой версии нет."
    ]


def test_the_link_is_reconnecting_between_attempts_and_offline_only_when_taken_down(
    monkeypatch,
) -> None:
    """`OFFLINE` — только снятый поток; пауза между попытками — `RECONNECTING`."""
    heard, worker = Heard(), FakeWorker()
    first = FakeSocket([wire(snapshot(MINUTE_A, close=100.0, turnover=100.0))], then_close=True)
    second = FakeSocket([wire(snapshot(MINUTE_B, close=100.0, turnover=100.0))])
    stream = FakeStream(first, second)
    feed = feed_of(monkeypatch, stream, worker, heard)

    async def scenario() -> None:
        feed.start()
        try:
            await settle(
                lambda: heard.connections.count(Connection.ONLINE) == 2, "вторая связь"
            )
            assert heard.connections == [
                Connection.RECONNECTING,
                Connection.ONLINE,
                Connection.RECONNECTING,
                Connection.ONLINE,
            ]
            feed.request_stop()
        finally:
            await feed.aclose()

    asyncio.run(scenario())
    assert heard.connections[-1] is Connection.OFFLINE
    assert Connection.OFFLINE not in heard.connections[:-1]
    assert heard.warnings() == ["Брокер закрыл поток котировок."]
    assert ("Связь с брокером", "Отключено по команде из окна.", DecisionLevel.INFO) in heard.said
    assert worker.written == [], "минута, закончившаяся за время обрыва, не пишется"


def test_a_refusal_that_a_retry_cannot_fix_takes_the_stream_down(monkeypatch) -> None:
    heard, worker = Heard(), FakeWorker()
    refusal = StreamRejected("Инструмент не найден у брокера", "NOT_FOUND", retryable=False)
    stream = FakeStream(refusal)
    feed = feed_of(monkeypatch, stream, worker, heard)

    async def scenario() -> None:
        feed.start()
        try:
            await settle(lambda: Connection.OFFLINE in heard.connections, "поток снят")
            await asyncio.sleep(0.02)  # повтору дать шанс случиться
        finally:
            await feed.aclose()

    asyncio.run(scenario())
    assert heard.connections == [Connection.RECONNECTING, Connection.OFFLINE]
    assert len(stream.requests) == 1, "отказ, которому повтор не поможет, повторили"
    assert heard.warnings()[0] == "Инструмент не найден у брокера"
    assert "повтор здесь не поможет" in heard.warnings()[1]
    assert "«Подключиться»" in heard.warnings()[1]


@pytest.mark.parametrize(
    ("failure", "complaint"),
    [
        (
            StreamRejected("Данных по инструменту пока нет", "NO_DATE", retryable=True),
            "Данных по инструменту пока нет",
        ),
        (
            # Сеть наружу исключением транспорта не выходит: `OSError`
            # и `WebSocketException` слой `broker/` переводит в `NoConnection`
            # сам (`broker/stream.py`). Сырой `OSError` здесь означал бы
            # не обрыв, а беду программы, — тест ниже.
            NoConnection("поток свечей: OSError: connection reset"),
            "Нет связи с брокером. Проверьте подключение к интернету. "
            "Пока связи нет, робот не принимает решений и заявок не подаёт.",
        ),
    ],
)
def test_an_outage_is_retried_and_its_reason_is_said_once(
    monkeypatch, failure: BaseException, complaint: str
) -> None:
    """Обрыв повторяется, и причина говорится один раз, а не каждую попытку.

    В журнал уходит **человеческая** половина отказа: владелец счёта читает
    «Нет связи с брокером», а не `OSError: connection reset`. Техническая
    половина живёт в `technical` и до журнала решений не доходит (а в лог —
    доходит, тест ниже про это).

    ⚠️ Ожидание состояний изменено 05.09.2026 вместе с `LinkPhase`. Прежде
    здесь стояло `[RECONNECTING] * 3 + [ONLINE]` — по одной отправке
    на попытку. Теперь одно и то же состояние второй раз в окно не уходит
    (`LiveFeed._phase`): панель зеркалит снимок, и повтор означал бы лишний
    снимок из порта на каждую из тысячи ночных попыток. Число попыток
    проверяется тем, чем и проверялось по делу, — `stream.requests`.
    """
    heard, worker = Heard(), FakeWorker()
    socket = FakeSocket([wire(snapshot(MINUTE_A, close=100.0, turnover=100.0))])
    stream = FakeStream(failure, failure, socket)
    feed = feed_of(monkeypatch, stream, worker, heard)

    async def scenario() -> None:
        feed.start()
        try:
            await settle(lambda: Connection.ONLINE in heard.connections, "связь после двух отказов")
        finally:
            await feed.aclose()

    asyncio.run(scenario())
    assert heard.connections == [Connection.RECONNECTING, Connection.ONLINE]
    assert Connection.OFFLINE not in heard.connections, (
        "обрыв, которому повтор помогает, показан как снятый поток"
    )
    assert heard.warnings() == [complaint], "одна причина — одна строка"
    assert len(stream.requests) == 3
    assert worker.opens == 1, "база переоткрывается на каждой попытке подключения"


def test_a_program_error_takes_the_stream_down_instead_of_retrying_forever(
    monkeypatch, caplog
) -> None:
    """Не сеть — не обрыв: беда программы снимает поток, а не крутит повторы.

    Сеть и рукопожатие слой `broker/` переводит в `BrokerError` сам, поэтому
    всё, что дошло сюда другим, — беда программы или машины: база не открылась,
    диск, разбор. Ворота `/review` 04.09.2026 нашли обратное поведение: такая
    беда считалась обрывом, повторы шли по кругу до конца сеанса, и на чистой
    машине без базы поток не записал ни одной минуты и молчал.

    Человеку — фраза в журнале и снятый поток; разработчику — трассировка
    в техническом логе, потому что по фразе место падения не найти.
    """
    heard, worker = Heard(), FakeWorker()
    stream = FakeStream(RuntimeError("база не открылась"), FakeSocket())
    feed = feed_of(monkeypatch, stream, worker, heard)

    async def scenario() -> None:
        feed.start()
        try:
            await settle(lambda: Connection.OFFLINE in heard.connections, "поток снят")
            await asyncio.sleep(0.02)  # повтору дать шанс случиться
        finally:
            await feed.aclose()

    with caplog.at_level(logging.ERROR, logger="app.live_feed"):
        asyncio.run(scenario())
    assert len(stream.requests) == 1, "беду программы повторили как обрыв связи"
    assert heard.connections == [Connection.RECONNECTING, Connection.OFFLINE]
    assert heard.warnings()[0] == (
        "Ошибка программы, а не связи: RuntimeError: база не открылась. "
        "Подробность — в выводе программы в консоли."
    )
    assert "повтор здесь не поможет" in heard.warnings()[1]
    traces = [record for record in caplog.records if record.exc_info is not None]
    assert len(traces) == 1, "трассировка не ушла в технический лог"


def test_a_frame_that_does_not_parse_reopens_the_socket_instead_of_ending_the_day(
    monkeypatch, caplog
) -> None:
    """Один битый кадр стоит переподключения, а не всех минут до вечера.

    Находка ревью 05.09.2026. `snapshot_of` разбирал кадр `json.loads` без
    оговорок: не-JSON выходил `JSONDecodeError`, а годный JSON не-объект —
    `AttributeError`. Ни то ни другое не `BrokerError`, поэтому `_ride`
    считал это бедой программы и снимал поток **окончательно** — дальше
    нужен человек с кнопкой. Дыры такого размера догрузка закрывает при
    следующем подключении, но его уже не было.

    Проверяется весь путь, а не только разбор: битый кадр → переоткрытая
    подписка → минуты второго захода идут как ни в чём не бывало.
    """
    heard, worker = Heard(), FakeWorker()
    broken = FakeSocket(["ping"])
    alive = FakeSocket([wire(one) for one in probe_snapshots()[:3]])
    stream = FakeStream(broken, alive)
    feed = feed_of(monkeypatch, stream, worker, heard)

    async def scenario() -> None:
        feed.start()
        try:
            await settle(lambda: len(heard.growing) >= 3, "снимки второго захода")
        finally:
            await feed.aclose()

    with caplog.at_level(logging.ERROR, logger="app.live_feed"):
        asyncio.run(scenario())

    assert Connection.OFFLINE not in heard.connections, (
        f"поток снят до конца сеанса из-за одного битого кадра: {heard.connections}"
    )
    assert len(stream.requests) == 2, (
        f"подписка не переоткрыта, заходов было {len(stream.requests)}"
    )
    assert broken.delivered == 1, "битый кадр до разбора не дошёл — проверка вакуумна"
    assert any("переоткроет подписку" in reason for reason in heard.warnings()), (
        f"о битом кадре не сказано ни строки: {heard.warnings()}"
    )
    assert not [record for record in caplog.records if record.exc_info is not None], (
        "битый кадр записан как беда программы: трассировка в техническом логе"
    )
    assert Connection.ONLINE in heard.connections, (
        "второй заход не дошёл до связи — минуты после битого кадра потеряны"
    )


def test_the_feed_opens_the_base_itself_and_only_once(monkeypatch) -> None:
    """Поток открывает базу сам, при подключении, и ровно один раз.

    ⚠️ Оба конца, и оба стоят денег. При запуске на чистой машине `app/main.py`
    базу не открывает — пустой файл превратил бы «базы нет» в «в базе нет
    свечей», — и без открытия здесь первый же `put_minutes` падал бы, а поток
    считал это обрывом связи. Второе открытие поток данных запрещает вслух
    (`MarketWorker.open`: два соединения к одной базе), поэтому переподключение
    обязано открытие пропустить: иначе первый же обрыв снял бы поток насовсем.

    Минута доезжает до записи через ту же неоткрытую снаружи базу: без ленивого
    открытия дублёр отказал бы ровно так же, как настоящий поток данных.
    """
    heard, worker = Heard(), FakeWorker()
    outage = NoConnection("поток свечей: OSError: connection reset")
    minute = FakeSocket([wire(one) for one in (
        snapshot(MINUTE_A, close=100.0, turnover=100.0),
        snapshot(MINUTE_B, close=100.0, turnover=100.0),
    )])
    feed = feed_of(monkeypatch, FakeStream(outage, minute), worker, heard)
    before = worker.opened

    async def scenario() -> None:
        feed.start()
        try:
            await settle(lambda: len(worker.written) == 1, "запись минуты после обрыва")
        finally:
            await feed.aclose()

    asyncio.run(scenario())
    assert (before, worker.opened) == (False, True), (
        "базу открывает не поток: до включения она обязана быть закрыта, после — открытой"
    )
    assert worker.opens == 1, "база открыта заново после обрыва — поток данных откажет"
    [(symbol, [candle], source)] = worker.written
    assert (symbol, candle.time, source) == ("MXU6", MINUTE_A, Source.BROKER)


def test_start_on_a_running_stream_does_not_open_a_second_socket(monkeypatch) -> None:
    heard, worker = Heard(), FakeWorker()
    stream = FakeStream(FakeSocket([wire(snapshot(MINUTE_A, close=100.0, turnover=100.0))]))
    feed = feed_of(monkeypatch, stream, worker, heard)

    async def scenario() -> None:
        feed.start()
        feed.start()
        try:
            await settle(lambda: Connection.ONLINE in heard.connections, "связь")
            feed.start()
            await asyncio.sleep(0.02)
        finally:
            await feed.aclose()

    asyncio.run(scenario())
    assert len(stream.requests) == 1


def test_a_restart_right_after_stop_waits_for_the_old_socket_to_close(monkeypatch) -> None:
    """Прежний сокет закрыт раньше нового: у брокера лимит соединений (§4.6)."""
    heard, worker = Heard(), FakeWorker()
    first = FakeSocket([wire(snapshot(MINUTE_A, close=100.0, turnover=100.0))])
    second = FakeSocket([wire(snapshot(MINUTE_B, close=100.0, turnover=100.0))])
    stream = FakeStream(first, second)
    stream.slow_close = 0.05
    feed = feed_of(monkeypatch, stream, worker, heard)

    async def scenario() -> None:
        feed.start()
        try:
            await settle(lambda: heard.connections.count(Connection.ONLINE) == 1, "первая связь")
            feed.request_stop()
            feed.start()  # сразу, пока прежний сокет ещё закрывается
            await settle(lambda: heard.connections.count(Connection.ONLINE) == 2, "вторая связь")
        finally:
            await feed.aclose()

    asyncio.run(scenario())
    assert stream.events == ["open#1", "close#1", "open#2", "close#2"]


# ====================================================================== звено


class FakePort:
    """Ровно то, что звено просит у порта: строка журнала, снимок, перерисовка."""

    def __init__(self) -> None:
        self.connection = Connection.UNKNOWN
        self.notes: list[tuple[str, str, DecisionLevel]] = []
        self.restated = 0
        self.refreshed: list[str] = []
        self.drawn: list[str] = []
        #: Растущие минуты. Настоящий порт по ним **рисует и только рисует**:
        #: ни записи, ни прогона (`HistoryPort.growing_minute`).
        self.growing: list[tuple[str, Candle]] = []
        self.control: Callable[[bool], str | None] | None = None
        self.retarget: Callable[[str], None] | None = None
        self.commands: list[bool] = []
        #: Минуты первого снимка, объявленные звеном. Настоящий порт кладёт
        #: их в границу доверия по инструменту (`HistoryPort.stream_leads`).
        self.leads: list[tuple[str, datetime]] = []
        #: Причины отказов, пришедшие от звена. Настоящий порт делает из них
        #: строку журнала и состояние «нет связи» (`tests/test_app_port.py`).
        self.refusals: list[str] = []
        #: Остановки робота, пришедшие от звена. Настоящий порт кладёт причину
        #: в `_Halt` — туда же, куда ложится остановка движка (`HistoryPort.halt`).
        self.halts: list[tuple[str, str]] = []

    def note(self, event: str, reason: str, level: DecisionLevel = DecisionLevel.INFO) -> None:
        self.notes.append((event, reason, level))

    def halt(self, event: str, reason: str) -> None:
        self.halts.append((event, reason))

    def restate(self) -> None:
        self.restated += 1

    def refresh(self, why: str = "") -> None:
        self.refreshed.append(why)

    def live_candle(self, symbol: str) -> None:
        self.drawn.append(symbol)

    def growing_minute(self, symbol: str, minute: Candle) -> None:
        self.growing.append((symbol, minute))

    def attach_stream(
        self,
        control: Callable[[bool], str | None],
        *,
        retarget: Callable[[str], None] | None = None,
    ) -> None:
        self.control = control
        self.retarget = retarget

    def stream_leads(self, symbol: str, minute: datetime) -> None:
        self.leads.append((symbol, minute))

    def stream(self, on: bool) -> None:
        """Как настоящий порт: команду передал, причину отказа наружу **не** вернул.

        `HistoryPort.stream` возвращает `None` всегда, а причину превращает
        в строку журнала, состояние «нет связи» и снимок для окна. Дублёр,
        возвращавший причину, обещал бы вызывающему то, чего настоящий порт
        не отдаёт, — и умалчивал бы, что с причиной вообще что-то делают.
        """
        self.commands.append(on)
        assert self.control is not None, "поток просят до того, как назначен обработчик"
        refusal = self.control(on)
        if refusal is not None:
            self.refusals.append(refusal)


def link_of(port, worker, userdata: pathlib.Path) -> LiveLink:
    return LiveLink(port, worker, userdata=userdata, ticker="MXU6", class_code="SPBFUT")


def no_token() -> TokenFileError:
    return TokenFileError("Токен брокера ещё не введён.", technical="нет файла — подделка")


def token_store_spy(monkeypatch, *, refusal: TokenFileError | None = None) -> list[pathlib.Path]:
    """Подменить `TokenStore` соглядатаем: пишет обращения, при `refusal` — отказывает."""
    touched: list[pathlib.Path] = []

    class Spy:
        def __init__(self, directory: pathlib.Path) -> None:
            touched.append(pathlib.Path(directory))
            if refusal is not None:
                raise refusal

    monkeypatch.setattr(live_feed, "TokenStore", Spy)
    return touched


def test_building_the_link_does_not_touch_the_token(tmp_path: pathlib.Path, monkeypatch) -> None:
    """Токен не трогается, пока связь не попросили. Каталог назван неверно нарочно."""
    touched = token_store_spy(monkeypatch, refusal=no_token())
    port: Any = FakePort()
    link = link_of(port, FakeWorker(), tmp_path / "elsewhere")
    port.attach_stream(link.switch)
    assert link.switch(False) is None
    assert touched == [], "файл токена тронут до того, как связь попросили"
    assert link.switch(True) == "Токен брокера ещё не введён."
    assert touched == [tmp_path / "elsewhere"]


@pytest.mark.parametrize(
    ("directory", "expected"),
    [("elsewhere", "названа неверно"), ("userdata", "ещё не введён")],
)
def test_a_failed_build_is_a_reason_not_a_traceback(
    tmp_path: pathlib.Path, caplog, directory: str, expected: str
) -> None:
    """Настоящий `TokenStore`: не тот каталог и отсутствующий файл — фразой."""
    userdata = tmp_path / directory
    userdata.mkdir()
    port: Any = FakePort()
    link = link_of(port, FakeWorker(), userdata)
    with caplog.at_level(logging.WARNING, logger="app.live_feed"):
        reason = link.switch(True)
    assert reason is not None
    assert expected in reason, reason
    assert [record.levelno for record in caplog.records] == [logging.WARNING]
    assert "не собрано" in caplog.records[0].getMessage()
    # Второе нажатие пробует снова: токен, положенный в папку при работающей
    # программе, подхватывается без перезапуска.
    assert link.switch(True) == reason
    asyncio.run(link.aclose())


def test_switching_on_again_reuses_the_session_and_restarts_the_stream(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """Выключили и включили снова — пересобирается задача, а не сессия и поток.

    Считаются оба построения, и не для симметрии. Сессия — это транспорт
    `httpx`, который закрывает только `aclose` и только у последней: лишняя
    текла бы до конца сеанса. Лишний `LiveFeed` унёс бы с собой список задач,
    снятых кнопкой, — и новый сокет открылся бы, не дождавшись закрытия
    прежнего, при лимите брокера в двадцать соединений (§4.6).
    """
    sessions: list[Any] = []
    built: list[LiveFeed] = []

    class FakeSession:
        def __init__(self, store: object) -> None:
            sessions.append(self)
            self.closed = 0

        def stored(self) -> SimpleNamespace:
            return SimpleNamespace(permissions_were_loose=False)

        async def close(self) -> None:
            self.closed += 1

    def counting(
        session, worker, *, ticker: str, class_code: str, watchers: Watchers
    ) -> LiveFeed:
        """Подпись — как у настоящего `LiveFeed`: лишний аргумент виден сразу."""
        built.append(
            LiveFeed(session, worker, ticker=ticker, class_code=class_code, watchers=watchers)
        )
        return built[-1]

    monkeypatch.setattr(live_feed, "BrokerSession", FakeSession)
    monkeypatch.setattr(live_feed, "TokenStore", lambda directory: directory)
    monkeypatch.setattr(live_feed, "LiveFeed", counting)
    stream = FakeStream(
        FakeSocket([wire(snapshot(MINUTE_A, close=100.0, turnover=100.0))]),
        FakeSocket([wire(snapshot(MINUTE_B, close=100.0, turnover=100.0))]),
    )
    monkeypatch.setattr(live_feed, "candle_stream", stream)
    monkeypatch.setattr(live_feed, "RETRY_FIRST", 0.0)
    port: Any = FakePort()
    link = link_of(port, FakeWorker(), tmp_path / "userdata")

    async def scenario() -> None:
        assert link.switch(True) is None
        assert link.switch(True) is None
        await settle(lambda: port.connection is Connection.ONLINE, "первая связь")
        assert link.switch(False) is None
        assert port.connection is Connection.OFFLINE
        assert link.switch(True) is None
        await settle(
            lambda: len(stream.opened) == 2 and port.connection is Connection.ONLINE,
            "вторая связь",
        )
        await link.aclose()

    asyncio.run(scenario())
    assert len(sessions) == 1, "второе включение построило вторую сессию"
    assert len(built) == 1, "второе включение пересобрало поток, а не перезапустило"
    assert sessions[0].closed == 1
    # RECONNECTING, ONLINE, OFFLINE, RECONNECTING, ONLINE — и ни одного повтора.
    assert port.restated == 5


def test_the_feed_names_the_first_minute_of_every_session_exactly_once(
    monkeypatch,
) -> None:
    """Минута первого снимка объявляется один раз за заход — и заново после обрыва.

    С неё и правее ряд ведёт поток, всё левее — забота догрузки пропущенного
    (`app/backfill.py`). Объявленная дважды, она поставила бы вторую догрузку;
    не объявленная после переподключения — оставила бы дыру обрыва навсегда,
    ровно то, на что жаловался владелец счёта.
    """
    heard = Heard()
    stream = FakeStream(
        FakeSocket(
            [
                wire(snapshot(MINUTE_A, close=100.0, turnover=100.0)),
                wire(snapshot(MINUTE_A, close=100.0, turnover=200.0)),
            ],
            then_close=True,
        ),
        FakeSocket([wire(snapshot(MINUTE_C, close=100.0, turnover=100.0))]),
    )
    feed = feed_of(monkeypatch, stream, FakeWorker(), heard)

    async def scenario() -> None:
        feed.start()
        await settle(lambda: len(heard.leading) == 2, "две объявленные минуты")
        await feed.aclose()

    asyncio.run(scenario())

    assert heard.leading == [
        MINUTE_A.astimezone(MSK),
        MINUTE_C.astimezone(MSK),
    ], f"объявлены не те минуты: {heard.leading}"


def test_the_link_starts_the_backfill_on_every_connection(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """Связь установлена — догрузка пропущенного запущена. Проводка, а не намерение.

    Сторож стоит по названной причине: `market/backfill.py` и `app/backfill.py`
    могут быть покрыты насквозь и при этом **не быть подключены ни к чему**.
    Прогон остался бы зелёным, а дыра у владельца счёта — на месте.
    """
    started: list[datetime] = []
    closed: list[str] = []

    class SpyBackfiller:
        def __init__(self, history, worker, port, *, ticker: str, class_code: str) -> None:
            self.ticker = ticker
            self.class_code = class_code

        def start(self, boundary: datetime) -> None:
            started.append(boundary)

        async def aclose(self) -> None:
            closed.append("догрузка")

    class FakeSession:
        def __init__(self, store: object) -> None:
            pass

        def stored(self) -> SimpleNamespace:
            return SimpleNamespace(permissions_were_loose=False)

        async def close(self) -> None:
            closed.append("сессия")

    class WatchedFeed(LiveFeed):
        """Настоящий поток; отмечает только момент своего снятия.

        Нужен ради **порядка** завершения: без отметки закрытие потока
        не видно вовсе, и перестановка «сначала поток, потом догрузка»
        прошла бы молча (замер мутацией 04.09.2026 — ровно так и было).
        """

        async def aclose(self) -> None:
            closed.append("поток")
            await super().aclose()

    monkeypatch.setattr(live_feed, "BrokerSession", FakeSession)
    monkeypatch.setattr(live_feed, "TokenStore", lambda directory: directory)
    monkeypatch.setattr(live_feed, "Backfiller", SpyBackfiller)
    monkeypatch.setattr(live_feed, "LiveFeed", WatchedFeed)
    monkeypatch.setattr(live_feed, "History", lambda session: session)
    stream = FakeStream(
        FakeSocket([wire(snapshot(MINUTE_A, close=100.0, turnover=100.0))])
    )
    monkeypatch.setattr(live_feed, "candle_stream", stream)
    monkeypatch.setattr(live_feed, "RETRY_FIRST", 0.0)
    port: Any = FakePort()
    link = link_of(port, FakeWorker(), tmp_path / "userdata")

    async def scenario() -> None:
        assert link.switch(True) is None
        await settle(lambda: bool(started), "догрузка запущена")
        await link.aclose()

    asyncio.run(scenario())

    assert started == [MINUTE_A.astimezone(MSK)], (
        f"догрузка получила не ту границу: {started}"
    )
    assert closed == ["догрузка", "поток", "сессия"], (
        "порядок завершения нарушен: догрузка пишет в базу и ходит в сеть через "
        "ту же сессию, поэтому снимается первой, а сессия закрывается последней. "
        f"Получено {closed}"
    )


def test_repeated_failed_presses_build_one_session(tmp_path: pathlib.Path, monkeypatch) -> None:
    """Токен не введён, кнопку жмут снова и снова — сессия одна, а не по одной на нажатие.

    `_build` зовётся, пока поток не построен, то есть при каждом неудачном
    нажатии. Лишняя сессия — свой транспорт, который закрывает только `aclose`
    и только у последней; остальные текли бы до конца сеанса.
    """
    sessions: list[Any] = []

    class RefusingSession:
        def __init__(self, store: object) -> None:
            sessions.append(self)
            self.closed = 0

        def stored(self) -> SimpleNamespace:
            raise no_token()

        async def close(self) -> None:
            self.closed += 1

    monkeypatch.setattr(live_feed, "BrokerSession", RefusingSession)
    monkeypatch.setattr(live_feed, "TokenStore", lambda directory: directory)
    port: Any = FakePort()
    link = link_of(port, FakeWorker(), tmp_path / "userdata")
    answers = [link.switch(True), link.switch(True), link.switch(True)]
    assert answers == ["Токен брокера ещё не введён."] * 3
    assert len(sessions) == 1, "на каждое нажатие построена новая сессия"
    asyncio.run(link.aclose())
    assert sessions[0].closed == 1


def test_loose_token_file_permissions_are_reported_to_the_journal(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    class LooseSession:
        def __init__(self, store: object) -> None:
            self.store = store

        def stored(self) -> SimpleNamespace:
            return SimpleNamespace(permissions_were_loose=True)

        async def close(self) -> None:
            return None

    monkeypatch.setattr(live_feed, "BrokerSession", LooseSession)
    monkeypatch.setattr(live_feed, "TokenStore", lambda directory: directory)
    monkeypatch.setattr(live_feed, "candle_stream", FakeStream())
    port: Any = FakePort()
    link = link_of(port, FakeWorker(), tmp_path / "userdata")

    async def scenario() -> None:
        assert link.switch(True) is None
        await link.aclose()

    asyncio.run(scenario())
    warnings = [(event, level) for event, _, level in port.notes if "токеном" in event]
    assert warnings == [("Права файла с токеном", DecisionLevel.WARNING)]


# ====================================================== сборка: `_live_feed`


def _scrubbing_of(name: str) -> list[logging.Filter | logging.Handler]:
    logger = logging.getLogger(name)
    return [
        *(item for item in logger.filters if isinstance(item, RedactingFilter)),
        *(item for item in logger.handlers if isinstance(item, RedactingSink)),
    ]


def _detach(name: str, items: Iterable[logging.Filter | logging.Handler]) -> None:
    logger = logging.getLogger(name)
    for item in items:
        if isinstance(item, logging.Handler):
            logger.removeHandler(item)
        else:
            logger.removeFilter(item)


def _attach(name: str, items: Iterable[logging.Filter | logging.Handler]) -> None:
    logger = logging.getLogger(name)
    for item in items:
        if isinstance(item, logging.Handler):
            logger.addHandler(item)
        else:
            logger.addFilter(item)


@pytest.fixture
def foreign_loggers_clean() -> Iterator[None]:
    """Чужие ветки логгеров — без чистки до теста и как были после.

    Иначе тест зеленел бы от чистки, которую поставил сосед, а сосед,
    стерегущий нетронутые чужие ветки, краснел бы от нашей.
    """
    names = sorted({*FOREIGN_LOGGERS, "websockets", "websockets.client"})
    before = {name: _scrubbing_of(name) for name in names}
    for name in names:
        _detach(name, before[name])
    yield
    for name in names:
        _detach(name, _scrubbing_of(name))
        _attach(name, before[name])


def handshake_line_as_logged() -> str:
    """Строка рукопожатия `websockets` на DEBUG — как её увидел бы файл лога.

    `websockets/client.py:297` печатает каждый заголовок рукопожатия вызовом
    `self.logger.debug("> %s: %s", key, value)`, и среди них — `Authorization`.
    """
    buffer = io.StringIO()
    sink = logging.StreamHandler(buffer)
    sink.setFormatter(logging.Formatter("%(name)s %(message)s"))
    root = logging.getLogger()
    root.addHandler(sink)
    previous = root.level
    root.setLevel(logging.DEBUG)
    try:
        logging.getLogger("websockets.client").debug(
            "> %s: %s", "Authorization", "Bearer " + FAKE_WS_TOKEN
        )
    finally:
        root.removeHandler(sink)
        root.setLevel(previous)
    return buffer.getvalue()


def test_the_program_without_the_stream_flag_never_builds_the_token_store(
    tmp_path: pathlib.Path, monkeypatch, foreign_loggers_clean: None
) -> None:
    touched = token_store_spy(monkeypatch, refusal=no_token())
    port: Any = FakePort()
    worker: Any = FakeWorker()
    link = _live_feed(port, worker, tmp_path / "userdata", Settings(), _arguments([]))
    assert touched == [], "запуск без --stream тронул файл токена"
    assert port.control == link.switch, "кнопка окна не проведена к звену"
    assert port.commands == []
    assert port.refusals == []


def test_the_stream_flag_presses_the_same_button_as_the_window(
    tmp_path: pathlib.Path, monkeypatch, foreign_loggers_clean: None
) -> None:
    touched = token_store_spy(monkeypatch, refusal=no_token())
    port: Any = FakePort()
    worker: Any = FakeWorker()
    _live_feed(port, worker, tmp_path / "userdata", Settings(), _arguments(["--stream"]))
    assert port.commands == [True]
    assert touched == [tmp_path / "userdata"]
    assert port.refusals == ["Токен брокера ещё не введён."], (
        "отказ по ключу --stream не доехал до порта: сказать о нём в журнал некому"
    )


def test_a_verbose_log_does_not_carry_the_handshake_token(
    tmp_path: pathlib.Path, monkeypatch, foreign_loggers_clean: None
) -> None:
    """Подробный лог `websockets` — без токена, потому что сборка поставила чистку.

    Канарейка сначала: до сборки та же строка уходит как есть — иначе зелёный
    результат ничего не говорил бы о чистке.
    """
    token_store_spy(monkeypatch, refusal=no_token())
    exposed = handshake_line_as_logged()
    assert FAKE_WS_TOKEN in exposed, "канарейка: без чистки подделка обязана быть видна"

    port: Any = FakePort()
    worker: Any = FakeWorker()
    _live_feed(port, worker, tmp_path / "userdata", Settings(), _arguments([]))

    written = handshake_line_as_logged()
    assert written.strip(), "запись вообще не дошла до обработчика"
    assert FAKE_WS_TOKEN not in written, written
    assert FAKE_WS_TOKEN[:12] not in written, written
    assert MASK in written, written


def test_a_closed_minute_asks_for_a_drawing_not_for_a_whole_run(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """Записанная минута просит у порта дорисовку, а не прогон по всей истории.

    Стережёт `B-009` на **шве**, а не внутри порта. Прежде здесь стоял
    `port.refresh(...)`: прогон стратегии по всей истории на каждой живой
    минуте, заканчивавшийся полной заменой набора свечей, — приближение,
    выставленное руками, сбрасывалось раз в минуту, и наблюдать за появлением
    свечи глазами было нельзя.

    Проверяются оба конца шва: что зовёт `_redraw` и что поток зовёт именно
    `_redraw`. Без второй половины переименование обработчика оставило бы
    проверку зелёной, а поток — без перерисовки вовсе.
    """
    monkeypatch.setattr(live_feed, "BrokerSession", lambda store: SimpleNamespace(
        stored=lambda: SimpleNamespace(permissions_were_loose=False),
    ))
    monkeypatch.setattr(live_feed, "TokenStore", lambda directory: directory)
    port: Any = FakePort()
    link = link_of(port, FakeWorker(), tmp_path / "userdata")

    link._redraw("MXU6")  # noqa: SLF001 — предмет проверки и есть этот шов
    assert port.drawn == ["MXU6"], "минута не дошла до дорисовки графика"
    assert port.refreshed == [], (
        "живая минута всё ещё гонит прогон по всей истории: масштаб графика "
        "сбрасывается на каждой свече"
    )

    feed = link._build()  # noqa: SLF001 — то же
    assert feed._watch.closed_minute == link._redraw, (  # noqa: SLF001 — то же
        "поток зовёт по закрытой минуте не тот обработчик — проверка выше "
        "проверяет мёртвый код"
    )


# ================================================= расписание торгов (`B-022`)


def status_of(
    is_open: bool | None, next_change: datetime | None = None
) -> TradingStatus:
    """Ответ «идут ли торги» — ровно тот, что строит `broker.schedule.parse_status`."""
    return TradingStatus(
        class_code="SPBFUT",
        session_type=None,
        session_type_id=None,
        kind=SessionKind.UNKNOWN,
        is_open=is_open,
        next_change=next_change,
    )


class FakeHours:
    """Расписание из сценария: ответы по одному, последний повторяется.

    Повтор последнего — не удобство, а форма живого случая: биржа, закрытая
    в субботу, закрыта и через пятнадцать минут, и через час. Сценарий
    из двух ответов «закрыто, открыто» так читается как «дождались открытия».
    """

    def __init__(
        self,
        *answers: TradingStatus | None,
        work_day: bool | None = None,
        boom: BaseException | None = None,
        trouble: BrokerError | None = None,
    ) -> None:
        self.answers = list(answers) or [None]
        self.work_day = work_day
        self.boom = boom
        #: Почему `status` отвечает «не знаем». `None` — причины нет
        #: (так ведёт себя расписание, у которого отказа не было).
        self.trouble = trouble
        self.asked: list[str] = []
        self.asked_daily: list[tuple[str, str]] = []
        self.asked_trouble: list[str] = []

    async def status(self, *, class_code: str) -> TradingStatus | None:
        self.asked.append(class_code)
        if self.boom is not None:
            raise self.boom
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]

    def status_trouble(self, *, class_code: str) -> BrokerError | None:
        """Причина «не знаем» — как у `Schedule`: из памяти, без запроса."""
        self.asked_trouble.append(class_code)
        return self.trouble

    async def daily(self, *, class_code: str, ticker: str) -> DailySchedule | None:
        self.asked_daily.append((class_code, ticker))
        if self.work_day is None:
            return None
        return DailySchedule(
            class_code=class_code, ticker=ticker,
            is_work_day=self.work_day, sessions=(),
        )


def quiet_session(monkeypatch) -> None:
    """Сессия-заглушка: файл токена цел, закрытие ожидаемое (`await`).

    ⚠️ `close` именно ожидаемый. Заглушка с обычным `close` роняла звено
    на `aclose` — «object NoneType can't be used in await», — и падение
    выглядело бы дефектом правки, а не дублёра.
    """

    class Session:
        def __init__(self, store: object) -> None:
            pass

        def stored(self) -> SimpleNamespace:
            return SimpleNamespace(permissions_were_loose=False)

        async def close(self) -> None:
            pass

    monkeypatch.setattr(live_feed, "BrokerSession", Session)


def hourly_feed(monkeypatch, stream: FakeStream, worker, heard: Heard, hours) -> LiveFeed:
    """Поток с расписанием и без пауз: и повтор, и ожидание открытия — мгновенны."""
    feed = feed_of(monkeypatch, stream, worker, heard)
    monkeypatch.setattr(live_feed, "CLOSED_WAIT_MAX", 0.001)
    monkeypatch.setattr(live_feed, "CLOSED_WAIT_MIN", 0.0)
    feed.follow(hours)
    return feed


#: Что поток просит у расписания и ожидаемый ли это вызов. Таблица, а не
#: список имён: `status_trouble` **не** корутина, и проверка «все ожидаемые»
#: на нём бы упала, а вычеркнуть его из проверки значило бы не сверять
#: подпись именно у того вызова, который добавлен последним (`D-062`).
HOURS_CALLS: Final[dict[str, bool]] = {
    "status": True,
    "daily": True,
    "status_trouble": False,
}


def test_the_trading_hours_protocol_matches_the_real_schedule() -> None:
    """Протокол потока совпадает с `broker.schedule.Schedule` подпись в подпись.

    Тот же сторож, что у дублёра потока данных (`B-001`): протокол,
    разошедшийся с оригиналом, превратил бы рабочий вызов в `AttributeError`
    внутри задачи, а поток счёл бы это бедой программы и снялся. Проверка
    смотрит на настоящий класс, а не на дублёр, — дублёр можно поправить
    под ошибку, оригинал нет.
    """
    assert set(HOURS_CALLS) == {
        name for name in vars(live_feed.TradingHours) if not name.startswith("_")
    }, "таблица вопросов разошлась с протоколом"
    for name, awaited in HOURS_CALLS.items():
        declared = getattr(live_feed.TradingHours, name)
        real = getattr(Schedule, name, None)
        assert real is not None, f"поток просит у расписания `{name}`, которого нет"
        assert inspect.iscoroutinefunction(real) is awaited, (
            f"`{name}` у расписания ожидаемый не так, как ждёт поток"
        )
        assert inspect.signature(declared).parameters == inspect.signature(real).parameters, (
            f"`{name}`: подпись протокола разошлась с `Schedule`"
        )


def test_the_fake_schedule_answers_everything_the_protocol_asks() -> None:
    """Дублёр расписания отвечает на все три вопроса протокола.

    Без этого дублёр, отставший от протокола, ронял бы `AttributeError`
    внутри задачи потока — а поток ловит там всё подряд и считает бедой
    программы. Прогон остался бы зелёным, проверяя не то (`B-001`).
    """
    for name in HOURS_CALLS:
        assert hasattr(FakeHours, name), f"дублёр не знает вопроса `{name}`"


@pytest.mark.parametrize(
    ("work_day", "opens_at", "expected"),
    [
        (
            False,
            datetime(2026, 9, 7, 9, 0, tzinfo=MSK),
            "Биржа закрыта, суббота: нерабочий день. Торги начнутся "
            "в понедельник в 09:00.",
        ),
        (
            True,
            datetime(2026, 9, 5, 10, 0, tzinfo=MSK),
            "Биржа закрыта, суббота: сейчас не торговое время. Торги начнутся "
            "сегодня в 10:00.",
        ),
        (
            None,
            datetime(2026, 9, 6, 9, 30, tzinfo=MSK),
            "Биржа закрыта, суббота. Торги начнутся завтра в 09:30.",
        ),
        (None, None, "Биржа закрыта, суббота. Когда начнутся торги, брокер не сказал."),
        (
            None,
            datetime(2026, 9, 14, 9, 0, tzinfo=MSK),
            "Биржа закрыта, суббота. Торги начнутся 14.09 в 09:00.",
        ),
    ],
)
def test_a_closed_exchange_is_explained_in_words_not_left_to_guessing(
    work_day: bool | None, opens_at: datetime | None, expected: str
) -> None:
    """Фраза про закрытую биржу называет день и момент открытия — из расписания.

    Ровно то, чего не хватило владельцу счёта субботним утром: полчаса
    «восстанавливаем связь» вместо одной строки про выходной. Момент открытия
    берётся из `nextSessionDate`; не назвал брокер — не называем и мы,
    свой календарь торгов заводить нельзя.

    ⚠️ Суббота 05.09.2026 взята нарочно: она же и была тем самым днём.
    """
    saturday = datetime(2026, 9, 5, 9, 30, tzinfo=MSK)
    words = live_feed.closed_words(saturday, opens_at=opens_at, work_day=work_day)
    assert words.startswith(expected), f"сказано иначе: {words}"
    assert "не будем" in words, "не сказано, что подключаться не будем"


@pytest.mark.parametrize(
    ("day", "named"),
    [
        (datetime(2026, 9, 7, 21, 0, tzinfo=MSK), "понедельник"),
        (datetime(2026, 9, 8, 21, 0, tzinfo=MSK), "вторник"),
        (datetime(2026, 9, 9, 21, 0, tzinfo=MSK), "среда"),
        (datetime(2026, 9, 10, 21, 0, tzinfo=MSK), "четверг"),
        (datetime(2026, 9, 11, 21, 0, tzinfo=MSK), "пятница"),
        (datetime(2026, 9, 12, 9, 30, tzinfo=MSK), "суббота"),
        (datetime(2026, 9, 13, 9, 30, tzinfo=MSK), "воскресенье"),
    ],
)
def test_the_day_named_to_the_owner_is_the_day_it_actually_is(
    day: datetime, named: str
) -> None:
    """Фраза называет **сегодняшний** день недели, а не тот, что был в живом случае.

    Дыра, найденная мутацией 05.09.2026: все проверки фразы звали её
    с субботой, потому что суббота и была тем самым днём. Замена
    `WEEKDAYS[today.weekday()]` на зашитое слово «суббота» оставляла прогон
    зелёным — то есть день недели не проверялся вовсе.

    Цена ошибки не косметическая. Владелец счёта запускает программу вечером
    в среду, читает «Биржа закрыта, суббота» — и делает ровно тот же вывод,
    что и в живом случае: программа врёт, ей верить нельзя. Проверять надо
    все семь дней: любая одна опора допускает зашитую строку.

    ⚠️ Будни взяты в 21:00, выходные в 09:30 — время нарочно нерабочее,
    чтобы фраза была уместной в каждом дне.
    """
    words = live_feed.closed_words(day, opens_at=None, work_day=None)
    assert words.startswith(f"Биржа закрыта, {named}."), f"сказано иначе: {words}"


def test_the_day_is_named_in_moscow_time_not_in_the_machine_zone() -> None:
    """Полночь по Москве — уже другой день, и день берётся московский.

    22:30 UTC воскресенья — это 01:30 понедельника в Москве. Машина владельца
    счёта может стоять в любом поясе; торговое время в этом проекте только
    московское, и день недели обязан считаться по нему.
    """
    words = live_feed.closed_words(
        datetime(2026, 9, 13, 22, 30, tzinfo=UTC), opens_at=None, work_day=None
    )
    assert words.startswith("Биржа закрыта, понедельник."), words


def test_the_words_take_the_moment_from_the_schedule_zone_not_from_ours() -> None:
    """Момент открытия приходит в UTC — в слова он идёт **московским**.

    Три часа разницы здесь стоят целого дня: 21:30 UTC воскресенья — это уже
    00:30 понедельника в Москве, и без перевода фраза назвала бы не тот день.
    """
    saturday = datetime(2026, 9, 5, 9, 30, tzinfo=MSK)
    words = live_feed.closed_words(
        saturday,
        opens_at=datetime(2026, 9, 6, 21, 30, tzinfo=UTC),
        work_day=None,
    )
    assert "в понедельник в 00:30" in words, words


@pytest.mark.parametrize(
    ("opens_in", "expected"),
    [(None, 900.0), (10_000.0, 900.0), (120.0, 120.0), (-30.0, 15.0)],
)
def test_waiting_for_the_opening_stays_between_the_floor_and_the_ceiling(
    opens_in: float | None, expected: float
) -> None:
    """Ждём до открытия, но не дольше потолка и не короче пола.

    Потолок нужен потому, что `nextSessionDate` приходит в PDF без часового
    пояса: один сон до понедельника по ошибочному моменту стоил бы
    пропущенного открытия торгов. Пол — потому что момент в прошлом иначе
    означал бы повтор вовсе без паузы.
    """
    now = datetime(2026, 9, 5, 9, 30, tzinfo=MSK)
    opens_at = None if opens_in is None else now + timedelta(seconds=opens_in)
    assert live_feed.closed_wait(now, opens_at) == expected


def test_a_closed_exchange_is_not_hammered_with_connection_attempts(monkeypatch) -> None:
    """Биржа закрыта — сокет не открывается ни разу, и сказано почему.

    Это `B-022` целиком: субботним утром цикл повторов давал бы около тысячи
    попыток за выходные, а окно всё это время говорило «восстанавливаем
    связь». Проверяется, что расписание спрошено не раз (то есть ожидание
    действительно идёт), а подключений не было **ни одного**.

    ⚠️ И третье, добавленное 05.09.2026: панель говорит **«Биржа закрыта»**.
    Ради панели всё и затевалось — владелец счёта смотрел полчаса именно
    на неё, а не в журнал. Состояние уходит в окно **один раз** на всё
    ожидание: три проверки расписания подряд не должны мигать надписью.
    """
    heard, worker = Heard(), FakeWorker()
    stream = FakeStream()
    hours = FakeHours(status_of(False), work_day=False)
    feed = hourly_feed(monkeypatch, stream, worker, heard, hours)

    async def scenario() -> None:
        feed.start()
        try:
            await settle(lambda: len(hours.asked) >= 3, "расписание спрошено трижды")
        finally:
            await feed.aclose()

    asyncio.run(scenario())
    assert stream.requests == [], "поток долбится в закрытую биржу"
    assert stream.opened == [], "сокет открыт при закрытой бирже"
    said = [(event, reason) for event, reason, _ in heard.said if event == "Биржа закрыта"]
    assert len(said) == 1, f"строка про закрытую биржу сказана {len(said)} раз(а)"
    assert "нерабочий день" in said[0][1], said[0][1]
    assert heard.connections == [Connection.RECONNECTING, Connection.MARKET_CLOSED], (
        f"состояние связи при закрытой бирже: {heard.connections}"
    )
    assert heard.connections[-1].label == "Биржа закрыта", (
        "панель не сказала владельцу счёта, что торгов нет"
    )
    assert worker.written == []


def test_the_stream_connects_as_soon_as_the_exchange_opens(monkeypatch) -> None:
    """Дождались открытия — подключаемся, и ровно один раз.

    Обратная половина проверки выше: без неё «не долбиться» прошло бы
    и у потока, который не подключается никогда.

    ⚠️ Порядок состояний тут и есть содержание: «Восстанавливаем связь» →
    «Биржа закрыта» → «Восстанавливаем связь» → «Связь есть». Две проверки
    расписания подряд дают **одно** «Биржа закрыта», а не два: панель
    не мигает, пока ничего не изменилось.
    """
    heard, worker = Heard(), FakeWorker()
    stream = FakeStream(FakeSocket([wire(snapshot(MINUTE_A, close=100.0, turnover=100.0))]))
    hours = FakeHours(status_of(False), status_of(False), status_of(True))
    feed = hourly_feed(monkeypatch, stream, worker, heard, hours)

    async def scenario() -> None:
        feed.start()
        try:
            await settle(lambda: Connection.ONLINE in heard.connections, "связь после открытия")
        finally:
            await feed.aclose()

    asyncio.run(scenario())
    assert len(stream.requests) == 1, f"попыток подключения: {len(stream.requests)}"
    assert len(hours.asked) == 3, f"расписание спрошено {len(hours.asked)} раз(а)"
    assert heard.connections == [
        Connection.RECONNECTING,
        Connection.MARKET_CLOSED,
        Connection.RECONNECTING,
        Connection.ONLINE,
    ], f"панель показала: {[state.label for state in heard.connections]}"


@pytest.mark.parametrize(
    ("hours", "why"),
    [
        (FakeHours(None), "брокер не ответил"),
        (FakeHours(status_of(None)), "прислал незнакомое слово"),
        (FakeHours(boom=RuntimeError("расписание сломалось — подделка")), "беда программы"),
    ],
)
def test_an_unknown_schedule_behaves_exactly_as_it_did_before(
    monkeypatch, hours: FakeHours, why: str
) -> None:
    """«Не знаем» — это не «закрыто»: поток подключается, как до `B-022`.

    Пропущенный торговый день дороже сотни лишних попыток, поэтому все три
    вида незнания ведут себя одинаково — так, как поток вёл себя, когда
    расписания у него не было вовсе.
    """
    heard, worker = Heard(), FakeWorker()
    stream = FakeStream(FakeSocket([wire(snapshot(MINUTE_A, close=100.0, turnover=100.0))]))
    feed = hourly_feed(monkeypatch, stream, worker, heard, hours)

    async def scenario() -> None:
        feed.start()
        try:
            await settle(lambda: Connection.ONLINE in heard.connections, f"связь: {why}")
        finally:
            await feed.aclose()

    asyncio.run(scenario())
    assert len(stream.requests) == 1
    assert [event for event, _, _ in heard.said if event == "Биржа закрыта"] == [], (
        "незнание расписания выдано за закрытую биржу"
    )


def test_a_stream_without_a_schedule_at_all_still_connects(monkeypatch) -> None:
    """Расписание потоку не дали — он работает как до `B-022`, а не встаёт.

    Умолчание `follow` — единственный способ проверять поток без сети,
    и оно обязано быть безопасным: `None` означает «расписания нет»,
    а не «биржа закрыта».
    """
    heard, worker = Heard(), FakeWorker()
    stream = FakeStream(FakeSocket([wire(snapshot(MINUTE_A, close=100.0, turnover=100.0))]))
    feed = feed_of(monkeypatch, stream, worker, heard)

    async def scenario() -> None:
        feed.start()
        try:
            await settle(lambda: Connection.ONLINE in heard.connections, "связь без расписания")
        finally:
            await feed.aclose()

    asyncio.run(scenario())
    assert len(stream.requests) == 1


# ================== незнание расписания доезжает до человека (`D-062`)
#
# `B-022` закрыт тем, что закрытую биржу поток называет словами. У этого
# осталась цена: **всё остальное** он по-прежнему проглатывал. Отказ уходил
# в технический лог, `_shut` превращал его в «не знаем», поток подключался
# как раньше — и владелец счёта не узнавал ни о чём. Ошибись мы адресом
# расписания, как ошиблись адресом свечей (`B-010`), — субботнее утро
# вернулось бы один в один, и вернулось бы молча.
#
# Проверяется здесь ровно это: строка **доезжает** до журнала решений,
# и доезжает **один раз за сеанс**, а не по разу на каждую из 192 попыток.


def doubt_lines(heard: Heard) -> list[tuple[str, DecisionLevel]]:
    """Строки журнала решений про расписание: текст и громкость."""
    return [
        (reason, level)
        for event, reason, level in heard.said
        if event == live_feed.SCHEDULE_EVENT
    ]


def wrong_address_trouble() -> WrongAddress:
    """Отказ «шлюз не знает адреса» — с текстами из настоящей таблицы слоя.

    Тексты берутся из `session.ADDRESS_SENSITIVE`, а не пишутся здесь заново:
    проверка, сверяющая выдуманную фразу с выдуманной, не заметила бы, что
    владельцу счёта говорят не то.
    """
    what, consequence = ADDRESS_SENSITIVE[TRADING_STATUS_PATH]
    return WrongAddress(what, consequence, "подделка: шлюз не знает адреса")


def test_a_schedule_that_could_not_be_asked_is_said_to_the_owner(monkeypatch) -> None:
    """Расписание получить не удалось — это сказано в журнал решений.

    Сторож **молчания**, а не отказа: проверка «расписание не получено»
    зеленела бы и тогда, когда программа промолчала, потому что молчание
    и есть сегодняшнее поведение. Поэтому смотрим не на поток и не на лог,
    а на то, что владелец счёта прочитает: строку журнала.

    ⚠️ Вторая половина проверки не менее важна: поведение потока прежнее.
    «Не знаем» — это не «закрыто», и подключение состоялось.
    """
    heard, worker = Heard(), FakeWorker()
    stream = FakeStream(FakeSocket([wire(snapshot(MINUTE_A, close=100.0, turnover=100.0))]))
    trouble = NoConnection("подделка: сети нет")
    feed = hourly_feed(monkeypatch, stream, worker, heard, FakeHours(None, trouble=trouble))

    async def scenario() -> None:
        feed.start()
        try:
            await settle(lambda: Connection.ONLINE in heard.connections, "связь")
        finally:
            await feed.aclose()

    asyncio.run(scenario())
    said = doubt_lines(heard)
    assert len(said) == 1, f"про незнание расписания сказано {len(said)} раз(а)"
    line, level = said[0]
    assert "Расписание торгов у брокера получить не удалось" in line, line
    assert "проверить нечем" in line, line
    assert trouble.human in line, "причину отказа владельцу счёта не назвали"
    assert level is DecisionLevel.INFO, f"громкость: {level.label}"
    assert len(stream.requests) == 1, "поведение потока изменилось: он не подключился"


def test_one_line_for_the_whole_session_however_many_attempts(monkeypatch) -> None:
    """Двадцать отказов подряд — **одна** строка в журнале.

    Расписание спрашивается перед каждой попыткой подключения, а за выходные
    их 192. Журнал из 192 одинаковых строк — то же молчание, только громкое:
    в нём не видно ничего, и владелец счёта перестаёт его читать.
    """
    heard, worker = Heard(), FakeWorker()
    stream = FakeStream(*[NoConnection(f"подделка {number}") for number in range(25)])
    hours = FakeHours(None, trouble=ServerFailure("подделка: неполадка у брокера"))
    feed = hourly_feed(monkeypatch, stream, worker, heard, hours)

    async def scenario() -> None:
        feed.start()
        try:
            await settle(lambda: len(hours.asked) >= 20, "двадцать попыток подключения")
        finally:
            await feed.aclose()

    asyncio.run(scenario())
    said = doubt_lines(heard)
    assert len(hours.asked) >= 20, f"расписание спрошено {len(hours.asked)} раз(а)"
    assert len(said) == 1, f"на {len(hours.asked)} попыток пришлось {len(said)} строк(и)"


def test_a_wrong_address_is_told_in_words_of_its_own(monkeypatch) -> None:
    """Адрес неверен — слова другие и громче, чем при пропавшей связи.

    Это третий случай долга и единственный, который отличим наверняка:
    на неверное имя сервиса отвечает шлюз брокера, и по телу ответа видно,
    что кода отказа в нём нет (`session._wrong_address`). Отличать его
    обязательно: пропавшая связь пройдёт сама, а зашитый в программу адрес
    не исправится никогда.
    """
    heard, worker = Heard(), FakeWorker()
    stream = FakeStream(FakeSocket([wire(snapshot(MINUTE_A, close=100.0, turnover=100.0))]))
    trouble = wrong_address_trouble()
    feed = hourly_feed(monkeypatch, stream, worker, heard, FakeHours(None, trouble=trouble))

    async def scenario() -> None:
        feed.start()
        try:
            await settle(lambda: Connection.ONLINE in heard.connections, "связь")
        finally:
            await feed.aclose()

    asyncio.run(scenario())
    [(line, level)] = doubt_lines(heard)
    assert "ошибка в адресе на нашей стороне" in line, line
    assert "Само это не пройдёт" in line, line
    assert level is DecisionLevel.WARNING, f"громкость: {level.label}"
    plain, _ = live_feed.doubt_words(
        live_feed.ScheduleDoubt.UNREACHABLE, NoConnection("подделка")
    )
    assert line != plain, "неверный адрес сказан теми же словами, что пропавшая связь"


def test_a_refusal_no_retry_will_cure_is_said_apart_from_a_lost_link(monkeypatch) -> None:
    """Отказ, которому повтор не поможет, назван так, а не «нет связи».

    Сюда попадает и неверное имя **метода** внутри существующего сервиса:
    брокер ответит обычным 404 с кодом, и от «данных по такому классу нет»
    это неотличимо ничем. Поэтому слова годятся для обоих случаев и зовут
    проверить и программу, и настройку инструмента.
    """
    heard, worker = Heard(), FakeWorker()
    stream = FakeStream(FakeSocket([wire(snapshot(MINUTE_A, close=100.0, turnover=100.0))]))
    feed = hourly_feed(
        monkeypatch, stream, worker, heard,
        FakeHours(None, trouble=NotFound("подделка: не найдено")),
    )

    async def scenario() -> None:
        feed.start()
        try:
            await settle(lambda: Connection.ONLINE in heard.connections, "связь")
        finally:
            await feed.aclose()

    asyncio.run(scenario())
    [(line, level)] = doubt_lines(heard)
    assert "повтор не поможет" in line, line
    assert level is DecisionLevel.WARNING, f"громкость: {level.label}"


def test_a_broker_that_answered_without_saying_gets_its_own_line(monkeypatch) -> None:
    """Брокер ответил, но про торги не сказал, — это тоже незнание, и оно видно.

    Соседнее молчание того же вида: `is_open is None` означает, что поля
    в ответе нет или значение в нём незнакомое. Поток при этом подключается
    как раньше — и до этой правки не говорил об этом ничего.

    Громкость намеренно тихая: документация помечает все поля ответа
    необязательными, живого запроса не было ни одного, и объявлять
    предупреждением то, что может оказаться штатным ночным ответом, значит
    приучить владельца счёта пролистывать предупреждения.
    """
    heard, worker = Heard(), FakeWorker()
    stream = FakeStream(FakeSocket([wire(snapshot(MINUTE_A, close=100.0, turnover=100.0))]))
    feed = hourly_feed(monkeypatch, stream, worker, heard, FakeHours(status_of(None)))

    async def scenario() -> None:
        feed.start()
        try:
            await settle(lambda: Connection.ONLINE in heard.connections, "связь")
        finally:
            await feed.aclose()

    asyncio.run(scenario())
    [(line, level)] = doubt_lines(heard)
    assert "не сказал, идут ли они" in line, line
    assert level is DecisionLevel.INFO, f"громкость: {level.label}"
    assert len(stream.requests) == 1, "поведение потока изменилось"


def test_a_schedule_that_breaks_says_it_is_our_own_defect(monkeypatch) -> None:
    """Расписание уронило исключение — это названо ошибкой программы, а не связи.

    Прежде такая беда уходила только в технический лог: `log.exception`
    и тишина. Технический лог владелец счёта не читает и читать не обязан.
    """
    heard, worker = Heard(), FakeWorker()
    stream = FakeStream(FakeSocket([wire(snapshot(MINUTE_A, close=100.0, turnover=100.0))]))
    feed = hourly_feed(
        monkeypatch, stream, worker, heard,
        FakeHours(boom=RuntimeError("расписание сломалось — подделка")),
    )

    async def scenario() -> None:
        feed.start()
        try:
            await settle(lambda: Connection.ONLINE in heard.connections, "связь")
        finally:
            await feed.aclose()

    asyncio.run(scenario())
    [(line, level)] = doubt_lines(heard)
    assert "ошибка в самой программе" in line, line
    assert level is DecisionLevel.WARNING, f"громкость: {level.label}"


def test_a_closed_exchange_is_knowledge_and_not_a_doubt(monkeypatch) -> None:
    """Брокер сказал «закрыто» — это знание, и строки про незнание нет.

    Различение из задачи: «биржа закрыта» уже показывается и панелью,
    и журналом. Приписать к нему ещё и «проверить нечем» значило бы
    сказать владельцу счёта две противоположные вещи разом.
    """
    heard, worker = Heard(), FakeWorker()
    stream = FakeStream()
    hours = FakeHours(status_of(False), work_day=False)
    feed = hourly_feed(monkeypatch, stream, worker, heard, hours)

    async def scenario() -> None:
        feed.start()
        try:
            await settle(lambda: len(hours.asked) >= 3, "расписание спрошено трижды")
        finally:
            await feed.aclose()

    asyncio.run(scenario())
    assert doubt_lines(heard) == [], f"закрытая биржа выдана за незнание: {doubt_lines(heard)}"
    assert [event for event, _, _ in heard.said if event == "Биржа закрыта"] == ["Биржа закрыта"]


def test_a_stream_without_a_schedule_says_nothing_about_it(monkeypatch) -> None:
    """Расписания потоку не дали — про незнание он молчит, и это верно.

    Умолчание `follow` — это прогон без сети, а не событие для владельца
    счёта. Строка «расписание получить не удалось» там означала бы,
    что программа жалуется на то, чего у неё не просили.
    """
    heard, worker = Heard(), FakeWorker()
    stream = FakeStream(FakeSocket([wire(snapshot(MINUTE_A, close=100.0, turnover=100.0))]))
    feed = feed_of(monkeypatch, stream, worker, heard)

    async def scenario() -> None:
        feed.start()
        try:
            await settle(lambda: Connection.ONLINE in heard.connections, "связь")
        finally:
            await feed.aclose()

    asyncio.run(scenario())
    assert doubt_lines(heard) == []


# --- разбор незнания и слова к нему: чистые функции ---


def test_every_kind_of_doubt_has_words_and_a_loudness() -> None:
    """Слова заведены на все виды незнания, и лишних в таблице нет.

    Таблица полей против перечисления руками: вид без строки уронил бы поток
    `KeyError` внутри задачи, а поток счёл бы это бедой программы и снялся —
    то есть отсутствие текста стоило бы связи.
    """
    assert set(live_feed.DOUBTS) == set(live_feed.ScheduleDoubt)
    for doubt, words in live_feed.DOUBTS.items():
        assert words.tail, f"у вида «{doubt.value}» нет следствия"


def test_every_kind_of_doubt_promises_the_stream_keeps_working() -> None:
    """Каждая фраза говорит, что поток подключается как раньше.

    Это главная половина сообщения: владелец счёта обязан понять, что робот
    **не встал**. Проверяется готовая фраза, а не хвост таблицы: у неверного
    адреса ту же мысль говорит сам отказ слоя брокера («…и работает как
    раньше»), и повторять её вторым предложением значило бы сказать одно
    и то же дважды — ровно та стена, на которую владелец счёта уже жаловался.
    """
    for doubt in live_feed.ScheduleDoubt:
        failure = (
            wrong_address_trouble()
            if doubt is live_feed.ScheduleDoubt.WRONG_ADDRESS
            else None
        )
        line, _ = live_feed.doubt_words(doubt, failure)
        assert "как обычно" in line or "как раньше" in line, (
            f"вид «{doubt.value}» не сказал, что поток работает как раньше: {line}"
        )


@pytest.mark.parametrize(
    ("status", "failure", "expected"),
    [
        (status_of(True), None, None),
        (status_of(False), None, None),
        (status_of(None), None, live_feed.ScheduleDoubt.SILENT),
        (None, None, live_feed.ScheduleDoubt.UNREACHABLE),
        (None, NoConnection("подделка"), live_feed.ScheduleDoubt.UNREACHABLE),
        (None, ServerFailure("подделка"), live_feed.ScheduleDoubt.UNREACHABLE),
        (None, NotFound("подделка"), live_feed.ScheduleDoubt.REFUSED),
    ],
)
def test_the_doubt_is_named_by_what_came_back(
    status: TradingStatus | None,
    failure: BrokerError | None,
    expected: object,
) -> None:
    """Что пришло → чего мы не знаем. Включая «знаем» на закрытой бирже."""
    assert live_feed.schedule_doubt(status, failure) is expected


def test_a_wrong_address_outranks_the_fact_that_it_is_a_refusal() -> None:
    """Неверный адрес разбирается по типу отказа, а не по признаку повтора.

    Отдельной строкой, а не среди прочих: `WrongAddress` — тоже отказ,
    которому повтор не помогает, и порядок проверок здесь и есть содержание.
    Сойди он в `REFUSED` — владелец счёта не узнал бы, что дело в адресе.
    """
    doubt = live_feed.schedule_doubt(None, wrong_address_trouble())
    assert doubt is live_feed.ScheduleDoubt.WRONG_ADDRESS


def test_the_words_carry_the_reason_written_by_the_broker_layer() -> None:
    """Середину фразы пишет слой брокера, и второго текста про то же нет."""
    trouble = NoConnection("подделка")
    line, _ = live_feed.doubt_words(live_feed.ScheduleDoubt.UNREACHABLE, trouble)
    assert trouble.human in line, line
    assert "  " not in line, f"двойной пробел в строке журнала: {line}"
    assert not line.startswith(" ") and not line.endswith(" "), repr(line)


def test_a_missing_reason_leaves_no_hole_in_the_phrase() -> None:
    """Причины нет — фраза целая, а не с дырой посередине.

    Так выглядит `BROKEN`: причину туда никто не передаёт, и шаблон
    с пустым местом дал бы владельцу счёта строку с двойным пробелом.
    """
    line, level = live_feed.doubt_words(live_feed.ScheduleDoubt.BROKEN, None)
    assert "  " not in line, f"двойной пробел в строке журнала: {line}"
    assert level is DecisionLevel.WARNING


def test_the_link_hands_the_feed_the_real_trading_schedule(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """Сборка отдаёт потоку настоящее расписание, а не забывает его отдать.

    Проводка, а не намерение: `broker/schedule.py` может быть покрыт насквозь
    и при этом не быть подключён ни к чему. Прогон остался бы зелёным,
    а поток — долбящимся в закрытую биржу.
    """
    given: list[object] = []

    class WatchedFeed(LiveFeed):
        def follow(self, hours) -> None:
            given.append(hours)
            super().follow(hours)

    monkeypatch.setattr(live_feed, "BrokerSession", lambda store: SimpleNamespace(
        stored=lambda: SimpleNamespace(permissions_were_loose=False),
    ))
    monkeypatch.setattr(live_feed, "TokenStore", lambda directory: directory)
    monkeypatch.setattr(live_feed, "LiveFeed", WatchedFeed)
    monkeypatch.setattr(live_feed, "History", lambda session: session)
    port: Any = FakePort()
    link = link_of(port, FakeWorker(), tmp_path / "userdata")

    feed = link._build()  # noqa: SLF001 — предмет проверки и есть эта сборка
    assert len(given) == 1, "потоку не отдали расписание торгов"
    assert isinstance(given[0], Schedule), f"отдали не расписание: {given[0]!r}"
    assert isinstance(feed, WatchedFeed)


def test_every_phase_of_the_link_has_a_row_in_the_table() -> None:
    """У каждой фазы есть строка в `PHASES`, и наоборот. Иначе `KeyError` в бою.

    Таблица заменила ветвление по состояниям, и её полнота — условие правки:
    новая фаза без строки уронила бы поток `KeyError` внутри задачи, а старая
    строка без фазы означала бы мёртвый вид, про который никто не помнит.
    """
    assert set(live_feed.PHASES) == set(live_feed.LinkPhase)
    assert {look.shown for look in live_feed.PHASES.values()} <= set(Connection), (
        "фаза складывается в состояние, которого окно не знает"
    )
    assert all(look.words for look in live_feed.PHASES.values()), (
        "фаза без слов: в техническом логе её будет не отличить"
    )
    # ⚠️ Проверка не «сколько разных состояний получилось», а **какая фаза
    # каким состоянием показывается**. Прежняя редакция сверяла множество
    # целиком и осталась бы зелёной, верни кто-нибудь закрытую биржу
    # в «Восстанавливаем связь» — то есть ровно к `B-022`.
    closed = live_feed.PHASES[live_feed.LinkPhase.MARKET_CLOSED].shown
    assert closed is Connection.MARKET_CLOSED, (
        f"закрытая биржа показывается как {closed.label!r}: "
        "владелец счёта уже терял на этом полчаса"
    )
    assert live_feed.PHASES[live_feed.LinkPhase.ONLINE].shown is Connection.ONLINE
    assert live_feed.PHASES[live_feed.LinkPhase.DOWN].shown is Connection.OFFLINE
    # Три ожидания, которые окну показываются одинаково, — это решение,
    # а не недосмотр: для человека все три означают «связи нет, добываем».
    waiting = (
        live_feed.LinkPhase.CONNECTING,
        live_feed.LinkPhase.AWAITING_FIRST,
        live_feed.LinkPhase.PAUSED,
    )
    assert all(live_feed.PHASES[phase].shown is Connection.RECONNECTING for phase in waiting)


def test_the_reason_of_an_outage_reaches_the_technical_log_with_both_halves(
    monkeypatch, caplog
) -> None:
    """Причина обрыва — в техническом логе, и человеческая, и техническая.

    05.09.2026 причина уходила только в журнал окна, и разобрать обрыв
    по логу было нельзя — а лог заведён ровно для этого. Проверяются обе
    половины: без `technical` в логе остаётся «Нет связи с брокером»,
    по которому не видно ни адреса, ни кода.
    """
    heard, worker = Heard(), FakeWorker()
    failure = NoConnection("поток свечей: OSError: connection reset — подделка")
    stream = FakeStream(
        failure, FakeSocket([wire(snapshot(MINUTE_A, close=100.0, turnover=100.0))])
    )
    feed = feed_of(monkeypatch, stream, worker, heard)

    async def scenario() -> None:
        feed.start()
        try:
            await settle(lambda: Connection.ONLINE in heard.connections, "связь после отказа")
        finally:
            await feed.aclose()

    with caplog.at_level(logging.WARNING, logger="app.live_feed"):
        asyncio.run(scenario())

    written = "\n".join(record.getMessage() for record in caplog.records)
    assert "Нет связи с брокером" in written, written
    assert "OSError: connection reset — подделка" in written, (
        f"техническая половина отказа до лога не дошла: {written}"
    )
    assert "MXU6" in written, "в логе не назван инструмент, по которому оборвалось"


def test_each_kind_of_waiting_leaves_its_own_line_in_the_technical_log(
    monkeypatch, caplog
) -> None:
    """Четыре ожидания различимы в логе: где висит — у брокера или у нас.

    Это пункт 3 задания `B-022`. До него «восстанавливаем связь» покрывало
    всё сразу: сокет ещё не открыт; сокет открыт, а первого снимка нет;
    попытка провалилась и идёт пауза; биржа закрыта. Первое и второе —
    разные адресаты жалобы, третье — ожидание по нашему решению, четвёртое
    вообще не беда.

    ⚠️ Проверяется именно **лог**, а не панель: панели три ожидания
    показываются одинаково нарочно (`PHASES`), и разводит их только он.

    ⚠️ Дыра, ради которой сторож и написан (мутация 05.09.2026): удаление
    любой из четырёх строк `_phase(...)` из `_ride`, `_wait_out` и `_listen`,
    как и удаление самой записи в лог, оставляло прогон зелёным. То есть
    разведение состояний в коде было, а сторожа у него не было.

    Сценарий один и проходит все четыре: закрыто → открылось → сокет упал →
    пауза → сокет открыт, снимок пришёл.
    """
    heard, worker = Heard(), FakeWorker()
    failure = NoConnection("поток свечей: OSError: reset — подделка")
    stream = FakeStream(
        failure, FakeSocket([wire(snapshot(MINUTE_A, close=100.0, turnover=100.0))])
    )
    hours = FakeHours(status_of(False), status_of(True))
    feed = hourly_feed(monkeypatch, stream, worker, heard, hours)
    monkeypatch.setattr(live_feed, "RETRY_FIRST", 0.0)

    async def scenario() -> None:
        feed.start()
        try:
            await settle(lambda: Connection.ONLINE in heard.connections, "связь после ожидания")
        finally:
            await feed.aclose()

    with caplog.at_level(logging.INFO, logger="app.live_feed"):
        asyncio.run(scenario())

    written = "\n".join(record.getMessage() for record in caplog.records)
    for phase in (
        live_feed.LinkPhase.MARKET_CLOSED,
        live_feed.LinkPhase.CONNECTING,
        live_feed.LinkPhase.PAUSED,
        live_feed.LinkPhase.AWAITING_FIRST,
        live_feed.LinkPhase.ONLINE,
    ):
        words = live_feed.PHASES[phase].words
        assert words in written, (
            f"фаза «{words}» в техническом логе не названа: "
            f"разобрать по нему обрыв нельзя.\n{written}"
        )


def test_the_pause_between_attempts_starts_over_after_the_exchange_opens(
    monkeypatch,
) -> None:
    """Ночь ожидания не превращается в минуту паузы на первой утренней ошибке.

    Пауза повторов растёт вдвое до минуты, и после ожидания открытия она
    обязана начинаться заново: биржа открылась — первая попытка идёт сразу.
    Иначе накопленное за прошлые обрывы ожидание сложится с ожиданием
    расписания, и торговое утро начнётся с лишней минуты молчания.

    Обещание записано в докстринге `_ride`, но мутацией 05.09.2026
    выяснилось, что снятие `pause = RETRY_FIRST` не роняло ни одного теста.

    Сценарий: два обрыва подряд (пауза выросла) → биржа закрылась → открылась
    → снова обрыв. Последняя пауза обязана быть первой, а не четвёртой.

    ⚠️ Паузы здесь **настоящей длины, но не ожидаются**: `asyncio.sleep`
    подменён и только записывает, сколько его просили ждать. Обычный
    `feed_of` ставит `RETRY_FIRST = 0`, и на нулях удвоение неразличимо —
    первая редакция этого сторожа так и прошла мимо мутации.
    """
    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def remember(seconds: float) -> None:
        # Одного довода хватает: и `_ride`, и `_wait_out`, и `settle` зовут
        # `sleep` ровно так. Подмена «на все случаи» тут была бы враньём
        # про то, чем пользуется проверяемый код.
        slept.append(seconds)
        await real_sleep(0)

    heard, worker = Heard(), FakeWorker()
    boom = NoConnection("поток свечей: OSError: reset — подделка")
    stream = FakeStream(
        boom, boom, boom, FakeSocket([wire(snapshot(MINUTE_A, close=100.0, turnover=100.0))])
    )
    # Закрыто ровно на третьем вопросе: два обрыва до него, один после.
    hours = FakeHours(status_of(True), status_of(True), status_of(False), status_of(True))
    feed = hourly_feed(monkeypatch, stream, worker, heard, hours)
    monkeypatch.setattr(live_feed, "RETRY_FIRST", 0.5)
    monkeypatch.setattr(live_feed, "RETRY_CAP", 60.0)
    monkeypatch.setattr(live_feed.asyncio, "sleep", remember)

    async def scenario() -> None:
        feed.start()
        try:
            await settle(lambda: Connection.ONLINE in heard.connections, "связь после ожидания")
        finally:
            await feed.aclose()

    asyncio.run(scenario())
    # Отсев по длине, а не по значению: ожидание открытия и опрос `settle`
    # укорочены до тысячных, паузы повторов — десятые и больше.
    pauses = [seconds for seconds in slept if seconds >= 0.1]
    assert pauses[:2] == [0.5, 1.0], f"пауза повторов растёт не вдвое: {pauses}"
    assert len(pauses) >= 3, f"третьего обрыва не случилось, проверять нечего: {pauses}"
    assert pauses[2] == 0.5, (
        f"после ожидания открытия пауза не начата заново: {pauses}"
    )


# ============================================ смена инструмента (`B-020`)


def test_a_change_of_instrument_reaches_the_running_stream(monkeypatch) -> None:
    """Инструмент сменили — подписка переоткрылась на новом, а не осталась старой.

    До 05.09.2026 тикер задавался один раз при сборке: поток продолжал писать
    **старый** инструмент, а снимки нового отбрасывал бы как чужие — молча.

    Проверяется и порядок: прежний сокет закрыт **до** открытия нового.
    Лимит брокера — двадцать соединений, и полумёртвые у сервера считаются.
    """
    heard, worker = Heard(), FakeWorker()
    stream = FakeStream(
        FakeSocket([wire(snapshot(MINUTE_A, close=100.0, turnover=100.0))]),
        FakeSocket([wire(
            dataclasses.replace(
                snapshot(MINUTE_B, close=100.0, turnover=100.0), ticker="SiU6"
            )
        )]),
    )
    feed = feed_of(monkeypatch, stream, worker, heard)

    async def scenario() -> None:
        feed.start()
        try:
            await settle(lambda: heard.connections.count(Connection.ONLINE) == 1, "связь")
            feed.retarget("SiU6")
            await settle(lambda: len(stream.requests) == 2, "подписка на новый инструмент")
            await settle(lambda: heard.connections.count(Connection.ONLINE) == 2, "связь-2")
        finally:
            await feed.aclose()

    asyncio.run(scenario())
    assert stream.requests == [("MXU6", "SPBFUT"), ("SiU6", "SPBFUT")]
    assert stream.events[:3] == ["open#1", "close#1", "open#2"], (
        f"новый сокет открыт до закрытия прежнего: {stream.events}"
    )
    assert [event for event, _, _ in heard.said].count("Инструмент подписки изменён") == 1
    assert [symbol for symbol, _ in heard.growing][-1] == "SiU6", (
        "растущая свеча всё ещё идёт под прежним инструментом"
    )


def test_the_same_instrument_does_not_restart_the_stream(monkeypatch) -> None:
    """Инструмент тот же — подписка не трогается вовсе.

    Порт зовёт `retarget` на **каждое** применение настроек, в том числе
    когда меняли размер свечи. Переподключение на каждое «Применить» стоило
    бы обрыва ряда и дыры в минутках на ровном месте.
    """
    heard, worker = Heard(), FakeWorker()
    stream = FakeStream(FakeSocket([wire(snapshot(MINUTE_A, close=100.0, turnover=100.0))]))
    feed = feed_of(monkeypatch, stream, worker, heard)

    async def scenario() -> None:
        feed.start()
        try:
            await settle(lambda: Connection.ONLINE in heard.connections, "связь")
            feed.retarget("  MXU6 ")
            feed.retarget("")
            await asyncio.sleep(0.02)  # переподключению дать шанс случиться
        finally:
            await feed.aclose()

    asyncio.run(scenario())
    assert len(stream.requests) == 1, "подписка переоткрыта без смены инструмента"
    assert "Инструмент подписки изменён" not in [event for event, _, _ in heard.said]


def test_the_link_retargets_the_feed_and_rebuilds_the_backfill(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """Звено ведёт за настройкой и подписку, и догрузку — обе на новый тикер.

    Догрузка привязана к инструменту своими источниками (`Backfiller.sources`
    строятся из тикера), и оставленная прежней она догружала бы дыры чужого
    ряда, пока поток пишет новый.
    """
    built: list[str] = []
    stopped: list[str] = []

    class SpyBackfiller:
        def __init__(self, history, worker, port, *, ticker: str, class_code: str) -> None:
            self.ticker = ticker
            built.append(ticker)

        def start(self, boundary: datetime) -> None:
            pass

        def request_stop(self) -> None:
            stopped.append(self.ticker)

        async def aclose(self) -> None:
            pass

    retargeted: list[str] = []

    class WatchedFeed(LiveFeed):
        def retarget(self, ticker: str) -> None:
            retargeted.append(ticker)
            super().retarget(ticker)

    quiet_session(monkeypatch)
    monkeypatch.setattr(live_feed, "TokenStore", lambda directory: directory)
    monkeypatch.setattr(live_feed, "Backfiller", SpyBackfiller)
    monkeypatch.setattr(live_feed, "LiveFeed", WatchedFeed)
    monkeypatch.setattr(live_feed, "History", lambda session: session)
    monkeypatch.setattr(live_feed, "candle_stream", FakeStream())
    port: Any = FakePort()
    link = link_of(port, FakeWorker(), tmp_path / "userdata")

    async def scenario() -> None:
        assert link.switch(True) is None
        link.retarget("SiU6")
        await link.aclose()

    asyncio.run(scenario())
    assert built == ["MXU6", "SiU6"], f"догрузка осталась на прежнем инструменте: {built}"
    assert retargeted == ["SiU6"], f"подписка за настройкой не пошла: {retargeted}"
    assert stopped == ["MXU6"], (
        "догрузка прежнего инструмента не снята: она держит жетоны общего "
        f"ограничителя частоты и пишет в журнал про чужой тикер — {stopped}"
    )


def test_retargeting_before_the_first_connection_only_moves_the_ticker(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """Инструмент сменили до первого включения — подписка начнётся уже с нового.

    Токен при этом не трогается: смена инструмента в окне не повод читать
    файл токена, а до включения его читать нельзя вовсе (`LiveLink`).
    """
    touched = token_store_spy(monkeypatch)
    stream = FakeStream()
    monkeypatch.setattr(live_feed, "candle_stream", stream)
    quiet_session(monkeypatch)
    monkeypatch.setattr(live_feed, "History", lambda session: session)
    monkeypatch.setattr(live_feed, "RETRY_FIRST", 0.0)
    port: Any = FakePort()
    link = link_of(port, FakeWorker(), tmp_path / "userdata")

    async def scenario() -> None:
        link.retarget("SiU6")
        assert touched == [], "смена инструмента прочитала файл токена"
        assert link.switch(True) is None
        await settle(lambda: bool(stream.requests), "подписка открыта")
        await link.aclose()

    asyncio.run(scenario())
    assert stream.requests == [("SiU6", "SPBFUT")], (
        f"подписка ушла на прежний инструмент: {stream.requests}"
    )


def test_the_first_minute_of_a_session_reaches_the_port_as_well_as_the_backfill(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """Минута первого снимка идёт и в догрузку, и в порт — границей доверия.

    Второй получатель появился вместе с `B-020`: поток теперь способен
    дописать в базу по инструменту, на который порт ещё не смотрел, и тогда
    `coverage.last` показывает на минуту потока, а границей служить не может
    (`HistoryPort._first_look`). Без этой проводки настоящая история такого
    инструмента осталась бы неподтверждённой — громко, но напрасно.
    """
    started: list[datetime] = []

    class SpyBackfiller:
        def __init__(self, history, worker, port, *, ticker: str, class_code: str) -> None:
            pass

        def start(self, boundary: datetime) -> None:
            started.append(boundary)

        async def aclose(self) -> None:
            pass

    quiet_session(monkeypatch)
    monkeypatch.setattr(live_feed, "TokenStore", lambda directory: directory)
    monkeypatch.setattr(live_feed, "Backfiller", SpyBackfiller)
    monkeypatch.setattr(live_feed, "History", lambda session: session)
    stream = FakeStream(FakeSocket([wire(snapshot(MINUTE_A, close=100.0, turnover=100.0))]))
    monkeypatch.setattr(live_feed, "candle_stream", stream)
    monkeypatch.setattr(live_feed, "RETRY_FIRST", 0.0)
    port: Any = FakePort()
    link = link_of(port, FakeWorker(), tmp_path / "userdata")

    async def scenario() -> None:
        assert link.switch(True) is None
        await settle(lambda: bool(started), "догрузка запущена")
        await link.aclose()

    asyncio.run(scenario())
    assert port.leads == [("MXU6", MINUTE_A.astimezone(MSK))], (
        f"порт не узнал минуту первого снимка: {port.leads}"
    )
    assert started == [MINUTE_A.astimezone(MSK)]


def test_the_assembly_wires_both_the_switch_and_the_retarget(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """Сборка отдаёт порту обе команды связи: включение и смену инструмента.

    Раздельная проводка означала бы случай «поток включается, но за сменой
    инструмента в окне не идёт» — это и есть `B-020`. Проверяются обе,
    и что вторая ведёт именно в звено, а не в заглушку.
    """
    token_store_spy(monkeypatch)
    port: Any = FakePort()
    worker: Any = FakeWorker()
    link = _live_feed(port, worker, tmp_path / "userdata", Settings(), _arguments([]))

    assert port.control == link.switch, "включение потока не проведено"
    assert port.retarget == link.retarget, (
        "смена инструмента до звена не доходит: поток останется на инструменте, "
        "выбранном при сборке"
    )


def _connection_callers() -> set[str]:
    """Методы `LiveFeed`, которые сами дёргают `self._watch.connection(...)`.

    Разбор, а не поиск по подстроке: имя берётся из выражения
    `self._watch.connection(...)` целиком, поэтому ни слово в докстринге,
    ни `Watchers.connection` в другом классе сюда не попадут.
    """
    tree = ast.parse(inspect.getsource(live_feed))
    found: set[str] = set()

    def walk(node: ast.AST, owner: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                walk(child, child.name)
                continue
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr == "connection"
                and isinstance(child.func.value, ast.Attribute)
                and child.func.value.attr == "_watch"
            ):
                found.add(owner)
            walk(child, owner)

    walk(tree, "<модуль>")
    return found


def test_the_state_of_the_link_leaves_the_feed_through_one_door_only() -> None:
    """Состояние связи уходит в окно только из `_phase`, и больше ниоткуда.

    Не вкус: `_phase` — единственное место, где фаза попадает в технический
    лог. Отправка состояния мимо него означала бы состояние, которого в логе
    нет, — то есть обрыв, который нечем разобрать, ровно то, на чём споткнулись
    05.09.2026. Плюс мимо `_phase` теряется правило «одно и то же второй раз
    не отправляем».
    """
    assert _connection_callers() == {"_phase"}, (
        "состояние связи отправляется мимо `_phase`: "
        f"{sorted(_connection_callers())}"
    )


# --------------------------------------------------------------------------
# Опрос состояния счёта: деньги брокера — в движок (решения 0037 и 0040)
# --------------------------------------------------------------------------

def account_snapshot(
    *,
    free: float | None = 248_500.0,
    equity: float | None = 210_850.0,
    locked_for_futures: float | None = 41_250.0,
    quantity: float = 3.0,
    at: datetime | None = None,
) -> AccountSnapshot:
    """Ответ брокера про счёт — в том виде, в каком его строит `broker/account.py`."""
    return AccountSnapshot(
        taken_at=at or datetime(2026, 9, 4, 7, 0, tzinfo=UTC),
        account="9876543",
        money=Money(
            free_rub=free,
            money_total=free,
            money_locked=0.0 if free is not None else None,
            collateral_positions=41_250.0,
            collateral_orders=0.0,
            futures_limit=180_000.0,
            variation_margin=0.0,
            missing=() if free is not None else ("свободные средства",),
        ),
        positions=(
            AccountPosition(
                ticker="MXU6", class_code="SPBFUT", side=AccountSide.of(quantity),
                quantity=quantity, average_price=285_400.0, current_price=286_100.0,
                lot=1.0, price_step=25.0, currency="SUR", expires_on=None,
                locked_for_futures=locked_for_futures,
            ),
        ),
        equity_rub=equity,
    )


class FakeAccount:
    """Счёт брокера: сценарий ответов, дальше — последний ответ по кругу."""

    def __init__(self, *answers: AccountSnapshot | BaseException) -> None:
        self.answers = list(answers)
        self.asked = 0

    async def snapshot(self) -> AccountSnapshot:
        self.asked += 1
        outcome = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def watch_of(
    account,
    *,
    ticker: str = "MXU6",
    every: float = 0.001,
    exchange_margin: Callable[[], Any] | None = None,
    halts: list[tuple[str, str]] | None = None,
):
    """Опрос счёта с мгновенным темпом и двумя списками: деньги и строки журнала.

    `halts` — куда складывать остановки робота. Довод, а не четвёртое
    возвращаемое значение: остановка нужна одной проверке из тридцати,
    а распаковку по три поменяли бы все.
    """
    money: list[Any] = []
    said: list[tuple[str, str, DecisionLevel]] = []
    halts = [] if halts is None else halts
    watch = live_feed.FundsWatch(
        cast(Any, account),
        ticker=ticker,
        watchers=live_feed.FundsWatchers(
            funds=money.append,
            say=lambda event, reason, level: said.append((event, reason, level)),
            halt=lambda event, reason: halts.append((event, reason)),
            # Подставной порт: остановлен ровно тогда, когда `halt` уже
            # звали. Так же ведёт себя настоящий (`HistoryPort.halted`
            # читает то, что положил `halt`), и без этого проверки жалобы
            # про счёт зеленели бы на постоянном «робот работает».
            halted=lambda: bool(halts),
        ),
        every=timedelta(seconds=every),
        exchange_margin=cast(Any, exchange_margin),
    )
    return watch, money, said


#: Замер 05.09.2026: столько биржа объявляет по `MXU6` (`INITIALMARGIN`).
EXCHANGE_MARGIN = 23_124.62
#: Он же по `GZU6` — в пятнадцать раз меньше. Ради этой разницы книга ГО
#: и забывается при смене инструмента.
GAZPROM_MARGIN = 1_567.22


def exchange_margin_source(*answers: float | None | BaseException, **by_ticker: float):
    """Подставная биржа: ответ по тикеру, иначе сценарий по порядку вызовов.

    Тикеры лежат в атрибуте `calls` — по нему видно и **сколько** раз ходили
    на биржу, и **за каким** инструментом.
    """
    calls: list[str] = []

    async def source(ticker: str) -> float | None:
        calls.append(ticker)
        if ticker in by_ticker:
            return by_ticker[ticker]
        outcome = answers[min(len(calls) - 1, len(answers) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    source.calls = calls  # type: ignore[attr-defined]  # счётчик для теста
    return source


async def until(condition: Callable[[], bool], limit: float = 2.0) -> None:
    """Дождаться условия, а не «поспать и надеяться»."""
    deadline = asyncio.get_running_loop().time() + limit
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("условие не наступило за отведённое время")
        await asyncio.sleep(0)


def test_a_complete_snapshot_becomes_the_money_the_engine_gets() -> None:
    """Перевод снимка счёта в деньги движка: три числа, ни одно не выдумано."""
    money = live_feed.funds_of(account_snapshot(), "MXU6")
    assert money is not None
    assert money.equity == 210_850.0
    assert money.free == 248_500.0
    assert money.margin_per_contract == 13_750.0, "ГО контракта не выведено из позиции"
    assert money.at == datetime(2026, 9, 4, 7, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("kwargs", "what"),
    [
        ({"free": None}, "свободные средства"),
        ({"equity": None}, "размер счёта"),
    ],
    ids=["нет свободных средств", "нет размера счёта"],
)
def test_a_gap_in_the_snapshot_yields_no_money_at_all(kwargs: dict, what: str) -> None:
    """Пропуск не заменяется нулём: ноль в этих полях — не «нет», а неправда.

    Ноль в «свободных средствах» читается как «денег нет» — ложная тревога.
    Ноль в «размере счёта» даёт дневной лимит убытка, равный нулю, то есть
    остановку робота на первом же убытке.
    """
    snapshot = account_snapshot(**kwargs)
    assert live_feed.funds_of(snapshot, "MXU6") is None
    assert what in snapshot.funds_missing()


# --- ГО под контракт до первого входа (`D-046`, 05.09.2026) ---


def test_the_circle_is_broken_the_engine_gets_margin_with_no_position() -> None:
    """Позиции нет, а ГО в движок уходит: замкнутый круг `D-046` разорван.

    Круг был такой: ГО контракта брокер сообщает только через открытую
    позицию, а открыть позицию без ГО предохранитель запаса средств не даёт.
    """
    account = FakeAccount(account_snapshot(quantity=0.0))
    watch, money, said = watch_of(
        account, exchange_margin=exchange_margin_source(EXCHANGE_MARGIN)
    )

    async def scenario() -> None:
        watch.start()
        # ⚠️ Ждём **непустое ГО**, а не первый снимок. За биржевым числом
        # ходит отдельная задача: ждать биржу в цикле опроса нельзя — четыре
        # попытки по сорок секунд задержали бы укладку денег дольше срока
        # их годности (`FundsWatch._margin_now`). Значит число приходит
        # со следующего захода, и тест обязан ждать именно его.
        await until(lambda: any(one.margin_per_contract is not None for one in money))
        await watch.aclose()

    asyncio.run(scenario())
    assert money[-1].margin_per_contract == EXCHANGE_MARGIN * live_feed_markup(), (
        "ГО до первого входа снова неизвестно — круг не разорван"
    )
    assert money[0].margin_per_contract is None, (
        "первый снимок пришёл с ГО: значит опрос всё-таки ждал биржу"
    )


def live_feed_markup() -> float:
    """Надбавка брокера над биржевым ГО — из слоя брокера, а не переписанная сюда."""
    from broker.margin import BROKER_MARKUP

    return BROKER_MARKUP


def test_the_open_position_beats_whatever_margin_is_handed_in() -> None:
    """Ответ брокера по открытой позиции сильнее переданного числа — прямо в `funds_of`.

    Проверяется сама развилка, а не её след в опросе: через `FundsWatch` оба
    пути дают одно и то же число, и перевёрнутый порядок там незаметен
    (поймано мутацией 05.09.2026). Здесь переданное число заведомо другое.
    """
    handed = MarginPerContract(
        rubles=99_999.0,
        source=MarginSource.EXCHANGE_ESTIMATE,
        told="подделка: оценка",
        measured_at=datetime(2026, 9, 4, 7, 0, tzinfo=UTC),
    )
    money = live_feed.funds_of(account_snapshot(), "MXU6", margin=handed)
    assert money is not None
    assert money.margin_per_contract == 13_750.0, (
        "переданное число вытеснило ответ брокера по открытой позиции"
    )


def test_the_handed_margin_is_used_only_when_the_broker_said_nothing() -> None:
    """Позиции нет — берётся переданное число: ровно ради этого оно и передаётся."""
    handed = MarginPerContract(
        rubles=99_999.0,
        source=MarginSource.EXCHANGE_ESTIMATE,
        told="подделка: оценка",
        measured_at=datetime(2026, 9, 4, 7, 0, tzinfo=UTC),
    )
    money = live_feed.funds_of(account_snapshot(quantity=0.0), "MXU6", margin=handed)
    assert money is not None
    assert money.margin_per_contract == 99_999.0


def test_the_estimate_is_announced_as_an_estimate_in_the_journal() -> None:
    """Оценка названа оценкой вслух: движку уходит просто число, отличить нечем.

    `AccountFunds.margin_per_contract` — `float`, и ответ брокера от нашей
    прикидки в нём неразличим. Строка журнала — единственное место, где
    владелец счёта узнаёт, что объём заявки посчитан по оценке.
    """
    account = FakeAccount(account_snapshot(quantity=0.0))
    watch, money, said = watch_of(
        account, exchange_margin=exchange_margin_source(EXCHANGE_MARGIN)
    )

    async def scenario() -> None:
        watch.start()
        await until(
            lambda: any(
                row[0] == "Гарантийное обеспечение под контракт" for row in said
            )
        )
        await watch.aclose()

    asyncio.run(scenario())
    about = [row for row in said if row[0] == "Гарантийное обеспечение под контракт"]
    assert about, f"про ГО не сказано ни строки: {said}"
    event, reason, level = about[0]
    assert "ОЦЕНКА" in reason, reason
    assert level is DecisionLevel.WARNING, "оценка подана как обычное событие"


def test_an_open_position_is_not_replaced_by_the_estimate() -> None:
    """Есть позиция — берётся ответ брокера, а не оценка: она грубее вдвое."""
    account = FakeAccount(account_snapshot())
    watch, money, said = watch_of(
        account, exchange_margin=exchange_margin_source(EXCHANGE_MARGIN)
    )

    async def scenario() -> None:
        watch.start()
        await until(lambda: bool(money))
        await watch.aclose()

    asyncio.run(scenario())
    assert money[0].margin_per_contract == 13_750.0, "оценка вытеснила ответ брокера"
    about = [row for row in said if row[0] == "Гарантийное обеспечение под контракт"]
    assert about and "ОЦЕНКА" not in about[0][1], about


def test_the_exchange_is_not_asked_on_every_snapshot() -> None:
    """За карточкой на биржу ходят не чаще `EXCHANGE_MARGIN_GAP`, а не раз в снимок.

    ГО меняется на клиринге, а опрос счёта идёт раз в полминуты: запрос
    на каждый снимок был бы сотней лишних обращений за торговый день.
    """
    account = FakeAccount(account_snapshot(quantity=0.0))
    source = exchange_margin_source(EXCHANGE_MARGIN)
    watch, money, _ = watch_of(account, exchange_margin=source)

    async def scenario() -> None:
        watch.start()
        await until(lambda: account.asked >= 5)
        await watch.aclose()

    asyncio.run(scenario())
    assert account.asked >= 5
    assert len(source.calls) == 1, (
        f"на биржу сходили {len(source.calls)} раз при {account.asked} снимках"
    )


def test_a_silent_exchange_does_not_drop_the_account_snapshot(caplog) -> None:
    """Биржа не ответила — деньги уходят в движок, а отказ виден в логе.

    Карточка инструмента нужнее ГО, но не нужнее размера счёта: от него
    считается дневной лимит убытка. Отказ при этом обязан попасть
    в технический лог с трассировкой: задача живёт сама по себе, и молча
    упавшая оставила бы строку asyncio, которую никто не читает.
    """
    account = FakeAccount(account_snapshot(quantity=0.0))
    source = exchange_margin_source(RuntimeError("биржа молчит — подделка"))
    watch, money, _ = watch_of(account, exchange_margin=source)

    async def scenario() -> None:
        watch.start()
        await until(lambda: len(source.calls) >= 1 and bool(money))
        await watch.aclose()

    with caplog.at_level(logging.ERROR, logger="app.live_feed"):
        asyncio.run(scenario())
    assert money[0].equity == 210_850.0
    assert money[0].margin_per_contract is None, "ГО взято из воздуха"
    assert any(
        "биржевое ГО" in record.message for record in caplog.records
    ), f"отказ биржи не попал в технический лог: {[r.message for r in caplog.records]}"


def test_changing_the_instrument_forgets_the_margin_of_the_previous_one() -> None:
    """Сменили тикер — ГО прежнего контракта забыто: у `MXU6` и `GZU6` разница в 15 раз.

    Оставленная память посчитала бы объём заявки по чужому обеспечению —
    молча, и в пятнадцать раз мимо.
    """
    account = FakeAccount(account_snapshot())
    # Первый ответ — биржевое ГО `MXU6`, второй — `GZU6` (замер 05.09.2026).
    # Числа разные намеренно: по итоговому видно, чьё именно ГО в книге.
    source = exchange_margin_source(MXU6=EXCHANGE_MARGIN, GZU6=GAZPROM_MARGIN)
    watch, money, _ = watch_of(account, exchange_margin=source)
    markup = live_feed_markup()

    def margins() -> list[float | None]:
        return [one.margin_per_contract for one in money]

    async def scenario() -> None:
        watch.start()
        # Пока тикер `MXU6`, ГО берётся из его открытой позиции — ответ брокера.
        await until(lambda: 13_750.0 in margins())
        watch.retarget("GZU6")
        await until(lambda: GAZPROM_MARGIN * markup in margins())
        await watch.aclose()

    asyncio.run(scenario())
    seen = margins()
    assert 13_750.0 in seen, "первые снимки — по позиции MXU6"
    assert seen[-1] == GAZPROM_MARGIN * markup, seen[-1]
    # Забыты **обе** памяти. Брокерская: иначе после смены тикера осталось бы
    # 13 750 ₽ от позиции `MXU6` (`MarginSource.BROKER_RECENT`). Биржевая:
    # иначе осталось бы ГО `MXU6` с надбавкой, и на биржу бы не пошли —
    # число моложе `EXCHANGE_MARGIN_GAP`.
    assert EXCHANGE_MARGIN * markup not in seen[-3:], (
        "в книге нового инструмента осталось биржевое ГО прежнего"
    )


def test_a_late_answer_about_the_previous_instrument_is_thrown_away() -> None:
    """Ответ биржи по прежнему тикеру в книгу нового не кладётся.

    Гонка: задача за биржевым ГО живёт своей жизнью, инструмент меняется
    из окна. Снятие задачи в `retarget` гонку сужает, но не закрывает —
    ответ мог уже прийти. Поэтому задача сверяет тикер, и проверяется здесь
    именно сверка: через открытый путь до неё не добраться, снятие успевает
    раньше.
    """
    account = FakeAccount(account_snapshot(quantity=0.0))
    watch, money, _ = watch_of(
        account, exchange_margin=exchange_margin_source(EXCHANGE_MARGIN)
    )

    async def scenario() -> None:
        watch.retarget("GZU6")
        # Ответ по прежнему тикеру, пришедший уже после смены инструмента.
        await watch._ask_exchange_margin(  # noqa: SLF001  # предмет проверки
            datetime(2026, 9, 4, 7, 0, tzinfo=UTC), "MXU6"
        )

    asyncio.run(scenario())
    stale = live_feed.funds_of(
        account_snapshot(quantity=0.0),
        "GZU6",
        margin=watch._margin.best(  # noqa: SLF001  # предмет проверки
            datetime(2026, 9, 4, 7, 0, tzinfo=UTC)
        ),
    )
    assert stale is not None
    assert stale.margin_per_contract is None, (
        "ГО прежнего контракта легло в книгу нового: разница до пятнадцати раз"
    )


def test_an_unknown_margin_still_goes_into_the_engine() -> None:
    """ГО под контракт неизвестно — деньги всё равно кладут.

    До первого входа за день позиции нет, а по HTTP ГО выводится только
    из неё. Отказ класть деньги здесь означал бы, что дневной лимит убытка
    не работает никогда: он считается от размера счёта, который в снимке есть.
    """
    money = live_feed.funds_of(account_snapshot(quantity=0.0), "MXU6")
    assert money is not None
    assert money.margin_per_contract is None
    assert money.equity == 210_850.0


def test_the_watch_asks_the_portfolio_and_hands_the_money_over() -> None:
    """Опрос спрашивает счёт и кладёт деньги получателю — тому, кто зовёт движок."""
    account = FakeAccount(account_snapshot())
    watch, money, said = watch_of(account)

    async def scenario() -> None:
        watch.start()
        await until(lambda: bool(money))
        await watch.aclose()

    asyncio.run(scenario())
    assert money[0].equity == 210_850.0
    assert any("Состояние счёта получено" == event for event, _, _ in said)


def test_a_silent_portfolio_gives_the_engine_nothing_and_says_why() -> None:
    """Брокер молчит — в движок не уходит ничего, и сказано, чем это кончится.

    Решение 0037: отказ во входе обязан быть **виден и объяснён**, а не быть
    молчаливым бездействием. Проверяется не только сам факт строки, но и то,
    что в ней названо последствие: владелец счёта, прочитавший «портфель
    не ответил», иначе будет ждать сделок, которых не будет.
    """
    account = FakeAccount(NoConnection("нет сети — подделка"))
    watch, money, said = watch_of(account)

    async def scenario() -> None:
        watch.start()
        await until(lambda: bool(said))
        await watch.aclose()

    asyncio.run(scenario())
    assert money == [], "деньги ушли в движок при молчащем портфеле"
    event, reason, level = said[0]
    assert event == "Состояние счёта не получено"
    assert level is DecisionLevel.WARNING
    assert "новых позиций" in reason and "не открывает" in reason, reason
    assert "Открытая позиция" in reason, (
        "не сказано, что выход и тейк продолжают работать: владелец счёта "
        "решит, что робот бросил позицию"
    )


def test_the_same_refusal_is_not_repeated_line_after_line() -> None:
    """Портфель молчит час — это одна строка журнала, а не сто двадцать."""
    account = FakeAccount(NoConnection("нет сети — подделка"))
    watch, _, said = watch_of(account)

    async def scenario() -> None:
        watch.start()
        await until(lambda: account.asked >= 5)
        await watch.aclose()

    asyncio.run(scenario())
    assert len(said) == 1, f"повтор одной и той же жалобы: {said}"


def test_a_portfolio_that_answers_again_is_announced() -> None:
    """Портфель заговорил снова — сказано вслух: без этого не понять, можно ли торговать."""
    account = FakeAccount(NoConnection("нет сети — подделка"), account_snapshot())
    watch, money, said = watch_of(account)

    async def scenario() -> None:
        watch.start()
        await until(lambda: bool(money))
        await watch.aclose()

    asyncio.run(scenario())
    events = [event for event, _, _ in said]
    assert events == [
        "Состояние счёта не получено",
        "Состояние счёта получено",
        # Порядок строк — это порядок чтения. Про обеспечение говорится
        # **после** того, как снимок признан годным: строка про ГО впереди
        # жалобы объясняла бы владельцу счёта не то, из-за чего робот не входит.
        "Гарантийное обеспечение под контракт",
    ], events


def test_an_incomplete_snapshot_is_not_half_a_snapshot() -> None:
    """Половина ответа в движок не уходит: она читается как целое."""
    account = FakeAccount(account_snapshot(equity=None))
    watch, money, said = watch_of(account)

    async def scenario() -> None:
        watch.start()
        await until(lambda: bool(said))
        await watch.aclose()

    asyncio.run(scenario())
    assert money == []
    assert "размер счёта" in said[0][1], said[0][1]


def test_a_broken_parser_is_logged_as_ours_not_blamed_on_the_broker(caplog) -> None:
    """Поломка нашей программы не притворяется отказом брокера.

    «Портфель не ответил» вместо дефекта разбора — это дефект, который никто
    не найдёт: он выглядит как обычная сетевая неприятность.
    """
    account = FakeAccount(ZeroDivisionError("подделка: дефект разбора"))
    watch, money, said = watch_of(account)

    async def scenario() -> None:
        watch.start()
        await until(lambda: bool(said))
        await watch.aclose()

    with caplog.at_level(logging.ERROR, logger="app.live_feed"):
        asyncio.run(scenario())
    assert money == []
    assert "Ошибка программы" in said[0][1]
    assert any("ошибкой программы" in record.message for record in caplog.records), (
        "трассировка не попала в технический лог"
    )


def test_the_poll_keeps_going_after_a_refusal() -> None:
    """Отказ не обрывает опрос: связь вернётся, а цикл должен её дождаться."""
    account = FakeAccount(NoConnection("нет сети — подделка"))
    watch, _, _ = watch_of(account)

    async def scenario() -> None:
        watch.start()
        await until(lambda: account.asked >= 3)
        await watch.aclose()

    asyncio.run(scenario())
    assert account.asked >= 3


class SpyWatch:
    """Опрос счёта-соглядатай: пишет, когда его запускали и останавливали."""

    def __init__(self, account, *, ticker, watchers, **rest) -> None:
        self.ticker = ticker
        self.log: list[str] = []

    def start(self) -> None:
        self.log.append("start")

    def request_stop(self) -> None:
        self.log.append("stop")

    def retarget(self, ticker: str) -> None:
        self.ticker = ticker
        self.log.append(f"retarget:{ticker}")

    async def aclose(self) -> None:
        self.log.append("aclose")


def link_with_funds(monkeypatch, port, stream: FakeStream, tmp_path: pathlib.Path):
    """Звено боевой сборки: настоящий поток, соглядатаи вместо догрузки и счёта."""
    watches: list[SpyWatch] = []

    def build(account, **rest) -> SpyWatch:
        spy = SpyWatch(account, **rest)
        watches.append(spy)
        return spy

    quiet_session(monkeypatch)
    monkeypatch.setattr(live_feed, "TokenStore", lambda directory: directory)
    monkeypatch.setattr(live_feed, "Backfiller", lambda *a, **k: SimpleNamespace(
        start=lambda boundary: None, request_stop=lambda: None,
        aclose=_noop_aclose,
    ))
    monkeypatch.setattr(live_feed, "History", lambda session: session)
    monkeypatch.setattr(live_feed, "Account", lambda session, exchange=None: session)
    monkeypatch.setattr(live_feed, "FundsWatch", build)
    monkeypatch.setattr(live_feed, "candle_stream", stream)
    monkeypatch.setattr(live_feed, "RETRY_FIRST", 0.0)
    monkeypatch.setattr(live_feed, "RETRY_CAP", 0.0)
    link = link_of(port, FakeWorker(), tmp_path / "userdata")
    link.deliver_funds_to(lambda funds: None)
    return link, watches


def test_the_link_polls_the_account_from_the_moment_the_stream_is_online(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """Опрос счёта живёт ровно столько, сколько открыт поток.

    Связь появилась — счёт перечитывается немедленно (шаг 2 порядка
    восстановления, `DOMAIN.md` §7). Связь пропала — спрашивать некого,
    и цикл только жёг бы жетоны общего ограничителя частоты.

    ⚠️ Ход идёт через **настоящий** поток и настоящий снимок, а не через
    прямой вызов `_show_connection`: проверяется, что состояние `ONLINE`
    вообще доходит до опроса. Подставить состояние руками значило бы
    проверить свою же подстановку.
    """
    socket = FakeSocket([wire(snapshot(MINUTE_A, close=100.0, turnover=1000.0))])
    stream = FakeStream(socket)
    port: Any = FakePort()
    link, watches = link_with_funds(monkeypatch, port, stream, tmp_path)

    async def scenario() -> None:
        assert link.switch(True) is None
        await settle(lambda: bool(watches) and "start" in watches[0].log,
                     "опрос счёта запущен по появлению связи")
        link.switch(False)
        await settle(lambda: "stop" in watches[0].log, "опрос счёта снят с потоком")
        await link.aclose()

    asyncio.run(scenario())
    log = watches[0].log
    assert log.count("start") == 1, f"опрос счёта запускался не один раз: {log}"
    # ⚠️ Перед `start` стоит `stop`, и это не дефект: поток начинает заход
    # состоянием «восстанавливаем связь», а опрос при любом состоянии, кроме
    # `ONLINE`, обязан быть снят. Снятие несуществующей задачи ничего не стоит.
    assert log.index("start") < len(log) - 1, f"после запуска опрос не снят: {log}"
    assert log[log.index("start") + 1] == "stop", log


def test_a_broken_link_stops_the_account_poll_without_any_button(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """Связь оборвалась сама — опрос счёта снят, кнопку никто не нажимал.

    ⚠️ Отдельно от снятия по кнопке, и это не дубль: кнопка снимает опрос
    своей строкой в `switch`, а обрыв — только состоянием связи. Проверка,
    гоняющая кнопку, зелена и у звена, которое на обрыв не реагирует вовсе,
    — то есть у звена, которое всю ночь спрашивает портфель через мёртвое
    соединение.
    """
    socket = FakeSocket(
        [wire(snapshot(MINUTE_A, close=100.0, turnover=1000.0))], then_close=True
    )
    stream = FakeStream(socket)
    port: Any = FakePort()
    link, watches = link_with_funds(monkeypatch, port, stream, tmp_path)

    async def scenario() -> None:
        assert link.switch(True) is None
        await settle(
            lambda: bool(watches) and "start" in watches[0].log,
            "опрос счёта запущен по появлению связи",
        )
        await settle(
            lambda: "stop" in watches[0].log[watches[0].log.index("start") + 1:],
            "опрос счёта снят обрывом связи",
        )
        await link.aclose()

    asyncio.run(scenario())


def test_the_account_poll_is_queued_before_the_backfill(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """Запрос счёта встаёт в очередь ограничителя раньше догрузки пропущенного.

    Ограничитель частоты у слоя один, и догрузка умеет занять его надолго.
    Приоритетов у ведра жетонов нет, поэтому единственное, чем можно помочь, —
    порядок постановки: связь `ONLINE` приходит раньше объявления границы
    потока, а границей начинается догрузка.

    ⚠️ Это порядок, а не гарантия, и так же сказано в коде. Задержанный
    очередью снимок приходит старее, но остаётся годным (`engine.FUNDS_MAX_AGE`).
    """
    order: list[str] = []
    socket = FakeSocket([wire(snapshot(MINUTE_A, close=100.0, turnover=1000.0))])
    stream = FakeStream(socket)
    port: Any = FakePort()

    class OrderedWatch(SpyWatch):
        def start(self) -> None:
            order.append("счёт")
            super().start()

    quiet_session(monkeypatch)
    monkeypatch.setattr(live_feed, "TokenStore", lambda directory: directory)
    monkeypatch.setattr(live_feed, "Backfiller", lambda *a, **k: SimpleNamespace(
        start=lambda boundary: order.append("догрузка"),
        request_stop=lambda: None, aclose=_noop_aclose,
    ))
    monkeypatch.setattr(live_feed, "History", lambda session: session)
    monkeypatch.setattr(live_feed, "Account", lambda session, exchange=None: session)
    monkeypatch.setattr(live_feed, "FundsWatch", OrderedWatch)
    monkeypatch.setattr(live_feed, "candle_stream", stream)
    link = link_of(port, FakeWorker(), tmp_path / "userdata")
    link.deliver_funds_to(lambda funds: None)

    async def scenario() -> None:
        assert link.switch(True) is None
        await settle(lambda: "догрузка" in order, "догрузка началась")
        await link.aclose()

    asyncio.run(scenario())
    assert order == ["счёт", "догрузка"], order


def test_changing_the_instrument_moves_the_account_poll_too(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """Сменили инструмент — ГО под контракт считается уже по его позиции.

    Оставленный прежним тикер дал бы ГО чужого контракта в проверке
    свободных средств: у MXU6 и RIU6 обеспечение разное, и объём заявки
    посчитался бы не от того числа.
    """
    stream = FakeStream()
    port: Any = FakePort()
    link, watches = link_with_funds(monkeypatch, port, stream, tmp_path)

    async def scenario() -> None:
        assert link.switch(True) is None
        link.retarget("RIU6")
        await link.aclose()

    asyncio.run(scenario())
    assert watches[0].ticker == "RIU6", "опрос счёта остался на прежнем инструменте"


def test_without_a_receiver_the_link_never_asks_about_the_account(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """Получателя денег не назвали — портфель не спрашивают вовсе.

    ⚠️ Это граница главного правила, а не экономия запросов. Прогон
    по истории и наблюдение на живом потоке размера счёта не имеют и иметь
    не должны: движок, которому в одном режиме сообщают счёт, а в другом
    нет, начинает вести себя по-разному на одной и той же свече
    (решение 0037, следствие 1).
    """
    built: list[str] = []

    socket = FakeSocket([wire(snapshot(MINUTE_A, close=100.0, turnover=1000.0))])
    quiet_session(monkeypatch)
    monkeypatch.setattr(live_feed, "TokenStore", lambda directory: directory)
    monkeypatch.setattr(live_feed, "Backfiller", lambda *a, **k: SimpleNamespace(
        start=lambda boundary: built.append("догрузка"), request_stop=lambda: None,
        aclose=_noop_aclose,
    ))
    monkeypatch.setattr(live_feed, "History", lambda session: session)
    monkeypatch.setattr(
        live_feed, "Account", lambda session, exchange=None: built.append("счёт")
    )
    monkeypatch.setattr(live_feed, "candle_stream", FakeStream(socket))
    port: Any = FakePort()
    link = link_of(port, FakeWorker(), tmp_path / "userdata")

    async def scenario() -> None:
        assert link.switch(True) is None
        # Ждём **связи**, а не таймера: без этого тест был бы зелёным и у звена,
        # которое просто не успело дойти до опроса счёта.
        await settle(lambda: "догрузка" in built, "поток дошёл до связи")
        await link.aclose()

    asyncio.run(scenario())
    assert "счёт" not in built, "портфель спросили без боевой сборки"


async def _noop_aclose() -> None:
    pass
