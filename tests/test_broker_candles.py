"""Исторические свечи брокера: разбивка по 1440, пояс, отказ посреди догрузки.

В сеть тесты не ходят: транспорт `httpx` подменён. Настоящий токен
не участвует — только приметная подделка, заведомо не похожая на выданную
брокером.

Что здесь стережётся, по важности:

1. **Разбивка по потолку 1440.** Брокер отказывает на запросе шире потолка.
   Дыра в сутки на минутках упирается в него ровно, то есть промах здесь —
   это не «неоптимально», а «догрузка не работает в основном случае».
2. **Пояс.** Брокер принимает и отдаёт UTC, программа живёт в МСК. Ошибка
   на три часа ничего не роняет: свечи просто лягут не туда.
3. **Отказ посреди догрузки.** Связь рвётся — это основной сценарий отказа
   (`DOMAIN.md` §7). Половина ряда, отданная как целый, оставит в данных дыру,
   о которой никто не узнает.
4. **Никаких молчаливых нулей.** Свеча с нулевым `low` выглядит как касание
   любого уровня выхода.
"""

from __future__ import annotations

import asyncio
import pathlib
import traceback
from collections.abc import Coroutine
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, TypeVar

import httpx
import pytest

from broker.candles import (
    MAX_BARS,
    MAX_REQUESTS,
    MINUTE,
    STEPS,
    HistoricalCandle,
    History,
    HistoryIncomplete,
    parse_candles,
    split_period,
    step_of,
)
from broker.errors import BadRequest, BrokerError, NoConnection, NotFound, UnexpectedAnswer
from broker.secret import Secret
from broker.session import CANDLES_PATH, BrokerSession
from broker.throttle import Throttle
from broker.token_store import TokenStore
from broker.tokens import ACCESS_LIFETIME, TokenScope

#: Приметные подделки. Ни одна не является токеном брокера.
FAKE_REFRESH = "FAKE-candles-refresh-0000-NOT-A-REAL-TOKEN"
FAKE_ACCESS = "FAKE-candles-access-1111-NOT-A-REAL-TOKEN"

#: Московское время. В `broker/` его нет и быть не должно — но вызывающий
#: слой живёт в нём, и запрос к брокеру приходит именно оттуда.
MSK = timezone(timedelta(hours=3))
UTC = timezone.utc

TICKER = "MXU6"
CLASS_CODE = "SPBFUT"

START = datetime(2026, 9, 4, 9, 0, tzinfo=UTC)


class Clock:
    """Часы, которыми управляет тест."""

    def __init__(self, start: datetime = START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


async def no_sleep(seconds: float) -> None:
    return None


#: Что вернула догрузка. Именованный тип, а не `Any`: подставка, которая
#: молча вернула не то, — это ровно тот отказ, который тесты и ловят.
Result = TypeVar("Result")


def run(coroutine: Coroutine[Any, Any, Result]) -> Result:
    return asyncio.run(coroutine)


@pytest.fixture
def store(tmp_path: pathlib.Path) -> TokenStore:
    directory = tmp_path / "userdata"
    directory.mkdir()
    keeper = TokenStore(directory)
    keeper.save(Secret(FAKE_REFRESH), TokenScope.READ_ONLY, issued_at=START - timedelta(days=1))
    return keeper


# --- подставной брокер ---


def auth_body() -> dict[str, Any]:
    return {
        "expires_in": int(ACCESS_LIFETIME.total_seconds()),
        "token_type": "bearer",
        "scope": "openid profile",
        "access_token": FAKE_ACCESS,
    }


class Broker:
    """Подставной брокер: обмен токена плюс сценарий ответов на свечи.

    Ответы задаются по одному на запрос свечей; когда сценарий кончился,
    повторяется последний. Так пишется и «всё время отказывает», и «первый
    кусок пришёл, второй оборвался».
    """

    def __init__(self, *answers: Callable[[httpx.Request], httpx.Response]) -> None:
        self.answers = list(answers)
        self.asked: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and "openid-connect" in request.url.path:
            return httpx.Response(200, json=auth_body())
        self.asked.append(request)
        index = min(len(self.asked) - 1, len(self.answers) - 1)
        return self.answers[index](request)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url="https://example.invalid", transport=httpx.MockTransport(self)
        )

    def params(self, index: int = 0) -> dict[str, str]:
        return dict(self.asked[index].url.params)

    def periods(self) -> list[tuple[str, str]]:
        """Периоды всех ушедших запросов: начало и конец, как в строке запроса."""
        asked = [dict(ask.url.params) for ask in self.asked]
        return [(one["startDate"], one["endDate"]) for one in asked]


def bar(moment: str, **changes: object) -> dict[str, Any]:
    """Один бар в том виде, как его описывает документация."""
    body: dict[str, Any] = {
        "time": moment,
        "open": 224_900.0,
        "close": 224_875.0,
        "high": 224_925.0,
        "low": 224_850.0,
        "volume": 137.0,
    }
    body.update(changes)
    return body


def answer(
    *bars: dict[str, Any], **envelope: object
) -> Callable[[httpx.Request], httpx.Response]:
    body: dict[str, Any] = {
        "ticker": TICKER,
        "classCode": CLASS_CODE,
        "timeFrame": MINUTE,
        "bars": list(bars),
    }
    body.update(envelope)
    return lambda request: httpx.Response(200, json=body)


def raw(body: object) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(200, json=body)


def refusal(status: int) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(status, json={"type": "SOMETHING"})


def broken(error: BaseException) -> Callable[[httpx.Request], httpx.Response]:
    """Транспорт, который бросает вместо ответа.

    Тип аргумента — `BaseException`, а не `Exception`, намеренно: отмена
    задачи (`CancelledError`) — не `Exception`, а самый частый нештатный
    отказ посреди запроса, потому что приходит при закрытии окна.
    """

    def reply(request: httpx.Request) -> httpx.Response:
        raise error

    return reply


#: Период по умолчанию — тот самый пропавший час владельца счёта из B-006,
#: 13:07–14:05 МСК. Один запрос: 58 минуток в потолок укладываются.
DEFAULT_START = datetime(2026, 9, 4, 16, 7, tzinfo=MSK)
DEFAULT_END = datetime(2026, 9, 4, 17, 5, tzinfo=MSK)


def fetch(
    store: TokenStore,
    broker: Broker,
    *,
    start: datetime = DEFAULT_START,
    end: datetime = DEFAULT_END,
    timeframe: str = MINUTE,
    attempts: int = 1,
    max_requests: int = MAX_REQUESTS,
) -> tuple[HistoricalCandle, ...]:
    """Догрузка за период. По умолчанию одна попытка: сценарий ответов не плывёт."""

    async def scenario() -> tuple[HistoricalCandle, ...]:
        session = BrokerSession(
            store,
            client=broker.client(),
            clock=Clock(),
            sleep=no_sleep,
            throttle=Throttle(rate=10_000.0),
            attempts=attempts,
        )
        async with session:
            return await History(session, max_requests=max_requests).candles(
                ticker=TICKER,
                class_code=CLASS_CODE,
                start=start,
                end=end,
                timeframe=timeframe,
            )

    return run(scenario())


# --- разбивка по потолку 1440 ---


def test_a_period_of_exactly_the_ceiling_is_one_request() -> None:
    """1440 минуток — ровно потолок, значит один запрос, а не два.

    Число из документации: «максимальное количество баров в одном запросе —
    1440», для `M1` это ровно сутки. Разбивка, дробящая уже влезающее,
    удваивает нагрузку на пустом месте.
    """
    start = datetime(2026, 9, 3, 0, 0, tzinfo=UTC)
    end = start + timedelta(minutes=MAX_BARS - 1)
    assert split_period(start, end, timeframe="M1") == ((start, end),)


def test_one_bar_over_the_ceiling_becomes_two_requests() -> None:
    """1441-я минутка обязана уехать во второй запрос: 1441 брокер отвергает."""
    start = datetime(2026, 9, 3, 0, 0, tzinfo=UTC)
    end = start + timedelta(minutes=MAX_BARS)
    chunks = split_period(start, end, timeframe="M1")
    assert len(chunks) == 2, chunks
    assert chunks[0][0] == start
    assert chunks[-1][1] == end


@pytest.mark.parametrize("timeframe", sorted(STEPS))
@pytest.mark.parametrize("bars", [1, 2, MAX_BARS - 1, MAX_BARS, MAX_BARS + 1, 5 * MAX_BARS])
def test_no_chunk_can_hold_more_than_the_ceiling(timeframe: str, bars: int) -> None:
    """Ни один кусок не вмещает больше 1440 баров — на всех таймфреймах.

    Считается так же, как считал бы брокер: сколько баров сетки помещается
    в отрезок с обеими включёнными границами.
    """
    step = step_of(timeframe)
    start = datetime(2026, 9, 3, 0, 0, tzinfo=UTC)
    for chunk_start, chunk_end in split_period(
        start, start + step * bars, timeframe=timeframe
    ):
        inside = (chunk_end - chunk_start) // step + 1
        assert inside <= MAX_BARS, (
            f"{timeframe}: кусок на {inside} баров, брокер отдаёт {MAX_BARS}"
        )


@pytest.mark.parametrize("timeframe", sorted(STEPS))
def test_chunks_cover_the_whole_period_without_a_seam(timeframe: str) -> None:
    """Куски покрывают период подряд: начало следующего = конец предыдущего.

    Зазор между кусками потерял бы бар молча — то есть задача про дыры
    в данных сама сделала бы дыру.
    """
    step = step_of(timeframe)
    start = datetime(2026, 9, 3, 0, 0, tzinfo=UTC)
    end = start + step * (3 * MAX_BARS + 7)
    chunks = split_period(start, end, timeframe=timeframe)
    assert chunks[0][0] == start
    assert chunks[-1][1] == end
    for before, after in zip(chunks, chunks[1:], strict=False):
        assert before[1] == after[0], f"между кусками разрыв: {before} и {after}"


def test_a_gap_of_one_day_of_minutes_is_split_into_requests(store: TokenStore) -> None:
    """Сутки минуток — тот самый случай, ради которого разбивка и написана."""
    broker = Broker(answer())
    fetch(
        store,
        broker,
        start=datetime(2026, 9, 3, 10, 0, tzinfo=MSK),
        end=datetime(2026, 9, 4, 10, 0, tzinfo=MSK),
    )
    assert len(broker.asked) == 2, broker.periods()


def test_a_period_longer_than_the_limit_is_refused_before_any_request(
    store: TokenStore,
) -> None:
    """Слишком длинный период — отказ до сети, а не полсотни запросов подряд."""
    broker = Broker(answer())
    with pytest.raises(ValueError) as caught:
        fetch(
            store,
            broker,
            start=datetime(2020, 1, 1, tzinfo=MSK),
            end=datetime(2026, 1, 1, tzinfo=MSK),
        )
    assert "запросов" in str(caught.value)
    assert broker.asked == [], "запрос всё-таки ушёл"


def test_the_request_limit_is_a_setting_not_a_wall(store: TokenStore) -> None:
    """Предел запросов настраивается: он наш предохранитель, а не предел брокера."""
    broker = Broker(answer())
    fetch(
        store,
        broker,
        start=datetime(2026, 9, 1, 10, 0, tzinfo=MSK),
        end=datetime(2026, 9, 4, 10, 0, tzinfo=MSK),
        max_requests=4,
    )
    assert len(broker.asked) == 4, broker.periods()


def test_an_unknown_timeframe_is_refused_before_any_request(store: TokenStore) -> None:
    """Опечатка в таймфрейме ловится у нас, а не отказом брокера в торговое время."""
    broker = Broker(answer())
    with pytest.raises(ValueError) as caught:
        fetch(store, broker, timeframe="1m")
    assert "таймфрейм" in str(caught.value)
    assert broker.asked == []


def test_an_end_before_the_start_is_refused() -> None:
    start = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
    with pytest.raises(ValueError):
        split_period(start, start - timedelta(minutes=1), timeframe=MINUTE)


# --- часовой пояс ---


def test_moscow_time_leaves_as_utc(store: TokenStore) -> None:
    """16:07 МСК уходит брокеру как 13:07Z. Промах здесь — час не тот, молча."""
    broker = Broker(answer())
    fetch(
        store,
        broker,
        start=datetime(2026, 9, 4, 16, 7, tzinfo=MSK),
        end=datetime(2026, 9, 4, 17, 5, tzinfo=MSK),
    )
    assert broker.periods() == [("2026-09-04T13:07:00Z", "2026-09-04T14:05:00Z")]


def test_the_same_instant_in_any_zone_gives_the_same_request(store: TokenStore) -> None:
    """13:07Z и 16:07+03:00 — один момент; запрос обязан выйти одинаковый."""
    in_utc = Broker(answer())
    fetch(
        store,
        in_utc,
        start=datetime(2026, 9, 4, 13, 7, tzinfo=UTC),
        end=datetime(2026, 9, 4, 14, 5, tzinfo=UTC),
    )
    in_moscow = Broker(answer())
    fetch(
        store,
        in_moscow,
        start=datetime(2026, 9, 4, 16, 7, tzinfo=MSK),
        end=datetime(2026, 9, 4, 17, 5, tzinfo=MSK),
    )
    assert in_utc.periods() == in_moscow.periods()


@pytest.mark.parametrize("which", ["start", "end"])
def test_a_naive_moment_is_refused_not_guessed(store: TokenStore, which: str) -> None:
    """Время без пояса — отказ. Догадка «наверное, МСК» стоит трёх часов."""
    broker = Broker(answer())
    bounds: dict[str, Any] = {
        "start": datetime(2026, 9, 4, 16, 7, tzinfo=MSK),
        "end": datetime(2026, 9, 4, 17, 5, tzinfo=MSK),
    }
    bounds[which] = bounds[which].replace(tzinfo=None)
    with pytest.raises(ValueError) as caught:
        fetch(store, broker, **bounds)
    assert "пояс" in str(caught.value)
    assert broker.asked == [], "запрос с наивным временем всё-таки ушёл"


def test_fractions_of_a_second_do_not_cut_off_the_last_bar(store: TokenStore) -> None:
    """Конец периода округляется вверх: иначе последняя свеча теряется.

    Конец берётся по часам программы, а не по сетке биржи, и доли секунды
    в нём — обычное дело.
    """
    broker = Broker(answer())
    fetch(
        store,
        broker,
        start=datetime(2026, 9, 4, 16, 7, 0, 500_000, tzinfo=MSK),
        end=datetime(2026, 9, 4, 17, 5, 0, 500_000, tzinfo=MSK),
    )
    assert broker.periods() == [("2026-09-04T13:07:00Z", "2026-09-04T14:05:01Z")]


def test_the_moment_of_a_candle_keeps_its_zone(store: TokenStore) -> None:
    """Время свечи выходит из слоя с поясом — как у потока, не наивным."""
    broker = Broker(answer(bar("2026-09-04T13:07:00Z")))
    got = fetch(store, broker)
    assert got[0].opened_at == datetime(2026, 9, 4, 13, 7, tzinfo=UTC)
    assert got[0].opened_at.tzinfo is not None, "пояс потерян при разборе"


def test_an_explicit_offset_is_the_same_instant_as_z() -> None:
    """16:07+03:00 и 13:07Z — один момент; сравнение по моменту, не по цифрам."""
    with_z = parse_candles(
        {"bars": [bar("2026-09-04T13:07:00Z")]},
        ticker=TICKER,
        class_code=CLASS_CODE,
        timeframe=MINUTE,
    )
    with_offset = parse_candles(
        {"bars": [bar("2026-09-04T16:07:00+03:00")]},
        ticker=TICKER,
        class_code=CLASS_CODE,
        timeframe=MINUTE,
    )
    assert with_z[0].opened_at == with_offset[0].opened_at


@pytest.mark.parametrize(
    "moment", ["2026-09-04T13:07:00.000", "2026-09-04T13:07:00", "2026-09-04 13:07"]
)
def test_a_candle_without_a_time_zone_is_refused(moment: str) -> None:
    """Наивное время в ответе — отказ вслух, а не молчаливый сдвиг на три часа."""
    with pytest.raises(UnexpectedAnswer) as caught:
        parse_candles(
            {"bars": [bar(moment)]},
            ticker=TICKER,
            class_code=CLASS_CODE,
            timeframe=MINUTE,
        )
    assert "пояс" in str(caught.value), str(caught.value)


# --- разбор ответа ---


def test_a_full_bar_becomes_a_candle(store: TokenStore) -> None:
    broker = Broker(answer(bar("2026-09-04T13:07:00Z")))
    got = fetch(store, broker)
    assert got == (
        HistoricalCandle(
            ticker=TICKER,
            class_code=CLASS_CODE,
            timeframe=MINUTE,
            opened_at=datetime(2026, 9, 4, 13, 7, tzinfo=UTC),
            open=224_900.0,
            high=224_925.0,
            low=224_850.0,
            close=224_875.0,
            volume=137.0,
        ),
    )


@pytest.mark.parametrize("field", ["time", "open", "high", "low", "close", "volume"])
def test_a_missing_field_is_refused_not_zeroed(field: str) -> None:
    """Ни одного молчаливого нуля: свеча с нулевым `low` — ложное срабатывание тейка."""
    incomplete = bar("2026-09-04T13:07:00Z")
    del incomplete[field]
    with pytest.raises(UnexpectedAnswer) as caught:
        parse_candles(
            {"bars": [incomplete]}, ticker=TICKER, class_code=CLASS_CODE, timeframe=MINUTE
        )
    assert field in str(caught.value), str(caught.value)


def test_a_zero_volume_is_a_value_not_a_gap() -> None:
    """Минута без сделок — законный ноль в объёме, а не недостающее поле."""
    got = parse_candles(
        {"bars": [bar("2026-09-04T13:07:00Z", volume=0)]},
        ticker=TICKER,
        class_code=CLASS_CODE,
        timeframe=MINUTE,
    )
    assert got[0].volume == 0.0


def test_an_empty_list_of_bars_is_not_a_failure(store: TokenStore) -> None:
    """Выходной, пауза в торгах, период до появления инструмента — данных нет."""
    broker = Broker(answer())
    assert fetch(store, broker) == ()


@pytest.mark.parametrize("body", [{"ticker": TICKER}, {"bars": None}, {"bars": {}}, []])
def test_a_missing_list_of_bars_is_a_change_of_format(body: object) -> None:
    """Нет самого списка баров — это смена формата, а не «данных нет»."""
    with pytest.raises(UnexpectedAnswer):
        parse_candles(body, ticker=TICKER, class_code=CLASS_CODE, timeframe=MINUTE)


def test_candles_of_another_instrument_are_refused() -> None:
    """Брокер ответил про другой инструмент — чужие цены в базу не пойдут."""
    with pytest.raises(UnexpectedAnswer) as caught:
        parse_candles(
            {"ticker": "SBER", "bars": [bar("2026-09-04T13:07:00Z")]},
            ticker=TICKER,
            class_code=CLASS_CODE,
            timeframe=MINUTE,
        )
    assert "другого инструмента" in str(caught.value)


def test_the_field_of_time_is_read_in_both_spellings() -> None:
    """`time` из документации и `dateTime` из потока того же брокера.

    Расхождение выписки с сервером в именах полей на этом API уже случалось:
    `subscribeType` против `subscriberType` (`broker/stream.py`).
    """
    moved = bar("2026-09-04T13:07:00Z")
    moved["dateTime"] = moved.pop("time")
    got = parse_candles(
        {"bars": [moved]}, ticker=TICKER, class_code=CLASS_CODE, timeframe=MINUTE
    )
    assert got[0].opened_at == datetime(2026, 9, 4, 13, 7, tzinfo=UTC)


def test_a_bar_that_is_not_an_object_is_refused() -> None:
    with pytest.raises(UnexpectedAnswer):
        parse_candles(
            {"bars": ["2026-09-04T13:07:00Z"]},
            ticker=TICKER,
            class_code=CLASS_CODE,
            timeframe=MINUTE,
        )


# --- склейка кусков ---


def test_candles_come_back_in_order_of_time(store: TokenStore) -> None:
    """Порядок — по возрастанию времени открытия, что бы ни прислал брокер."""
    broker = Broker(
        answer(
            bar("2026-09-04T13:09:00Z"),
            bar("2026-09-04T13:07:00Z"),
            bar("2026-09-04T13:08:00Z"),
        )
    )
    got = fetch(store, broker)
    assert [candle.opened_at.minute for candle in got] == [7, 8, 9]


def test_the_bar_on_the_seam_is_not_doubled(store: TokenStore) -> None:
    """Куски стыкуются внахлёст на один момент — бар шва обязан прийти один раз.

    Дубликат в ряду — это удвоенный объём в собранном баре: правдоподобное
    число, которое потом не по чему найти.
    """
    seam = "2026-09-04T13:07:00Z"
    broker = Broker(
        answer(bar(seam, close=1.0)),
        answer(bar(seam, close=2.0), bar("2026-09-04T13:08:00Z")),
    )
    got = fetch(
        store,
        broker,
        start=datetime(2026, 9, 3, 10, 0, tzinfo=MSK),
        end=datetime(2026, 9, 4, 10, 0, tzinfo=MSK),
    )
    assert len(broker.asked) == 2, "тест перестал проверять шов: кусок остался один"
    assert [candle.opened_at for candle in got] == [
        datetime(2026, 9, 4, 13, 7, tzinfo=UTC),
        datetime(2026, 9, 4, 13, 8, tzinfo=UTC),
    ]


# --- запрос ---


def test_the_request_carries_the_documented_parameters(store: TokenStore) -> None:
    """Имена параметров — из документации, а не из головы."""
    broker = Broker(answer())
    fetch(store, broker)
    assert broker.params() == {
        "classCode": CLASS_CODE,
        "ticker": TICKER,
        "startDate": "2026-09-04T13:07:00Z",
        "endDate": "2026-09-04T14:05:00Z",
        "timeFrame": "M1",
    }
    assert broker.asked[0].method == "GET"
    assert broker.asked[0].url.path == CANDLES_PATH


def test_the_address_of_candles_passes_the_guard(store: TokenStore) -> None:
    """Адрес свечей внесён в белый список осознанно, иначе `read` роняет рантайм."""
    broker = Broker(answer())
    fetch(store, broker)
    assert broker.asked, "сторож адресов не пустил читающий запрос свечей"


# --- отказ посреди догрузки ---


def test_a_failure_on_the_first_chunk_comes_out_as_it_is(store: TokenStore) -> None:
    """Получать ещё нечего — наружу идёт исходный отказ, без обёртки."""
    broker = Broker(broken(httpx.ConnectError("нет сети")))
    with pytest.raises(NoConnection):
        fetch(store, broker)


def test_a_failure_in_the_middle_does_not_pass_for_a_whole_series(
    store: TokenStore,
) -> None:
    """Обрыв на втором куске: наружу — отказ с полученной частью, а не «вот ряд».

    Половина ряда, отданная как целый, оставит в данных дыру, о которой
    никто не узнает. Это тот самый баг B-006, только наоборот.
    """
    broker = Broker(
        answer(bar("2026-09-03T07:00:00Z"), bar("2026-09-03T07:01:00Z")),
        broken(httpx.ConnectError("связь пропала")),
    )
    with pytest.raises(HistoryIncomplete) as caught:
        fetch(
            store,
            broker,
            start=datetime(2026, 9, 3, 10, 0, tzinfo=MSK),
            end=datetime(2026, 9, 4, 10, 0, tzinfo=MSK),
        )
    failure = caught.value
    assert len(failure.candles) == 2
    assert failure.covered_until == datetime(2026, 9, 3, 7, 1, tzinfo=UTC)
    assert isinstance(failure.cause, NoConnection)
    assert failure.retryable is True, "обрыв связи лечится повтором"
    assert "оборвалась" in str(failure)


@pytest.mark.parametrize(
    ("status", "cause", "retryable"),
    [(400, BadRequest, False), (404, NotFound, False), (500, None, True)],
)
def test_the_reason_of_the_break_is_carried_through(
    store: TokenStore, status: int, cause: type[BrokerError] | None, retryable: bool
) -> None:
    """Причина обрыва догрузки не теряется: по ней решают, повторять ли.

    Отказ 400 — это и есть `CANDLE_LIMIT_EXCEEDED`, отказ шире потолка.
    Повтором он не лечится, и `retryable` обязан это сказать.
    """
    broker = Broker(
        answer(bar("2026-09-03T07:00:00Z")),
        refusal(status),
    )
    with pytest.raises(HistoryIncomplete) as caught:
        fetch(
            store,
            broker,
            start=datetime(2026, 9, 3, 10, 0, tzinfo=MSK),
            end=datetime(2026, 9, 4, 10, 0, tzinfo=MSK),
        )
    assert caught.value.retryable is retryable
    if cause is not None:
        assert isinstance(caught.value.cause, cause)


def test_a_broken_answer_in_the_middle_is_not_a_half_series(store: TokenStore) -> None:
    """Смена формата на втором куске — тоже не повод отдать половину."""
    broker = Broker(
        answer(bar("2026-09-03T07:00:00Z")),
        raw({"ticker": TICKER}),
    )
    with pytest.raises(HistoryIncomplete) as caught:
        fetch(
            store,
            broker,
            start=datetime(2026, 9, 3, 10, 0, tzinfo=MSK),
            end=datetime(2026, 9, 4, 10, 0, tzinfo=MSK),
        )
    assert isinstance(caught.value.cause, UnexpectedAnswer)
    assert caught.value.retryable is False


def test_a_hiccup_on_one_chunk_is_survived_by_a_retry(store: TokenStore) -> None:
    """Один отказ — не обрыв догрузки: читающий запрос повторяется сам.

    Повторяет `BrokerSession._with_retries`; здесь проверяется, что догрузка
    этим пользуется, а не считает первую же неудачу концом.
    """
    tries: list[int] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        tries.append(1)
        if len(tries) == 1:
            raise httpx.ConnectError("моргнула связь")
        return httpx.Response(
            200,
            json={"ticker": TICKER, "bars": [bar("2026-09-04T13:07:00Z")]},
        )

    broker = Broker(flaky)
    got = fetch(store, broker, attempts=3)
    assert len(tries) == 2, "повтора не было"
    assert len(got) == 1


# --- токен ---


def frames_with_secret(error: BaseException) -> list[str]:
    """Строки переменных кадров трассировки, в которых видна подделка.

    `traceback.format_exception` переменные кадра не печатает, поэтому дыра
    этого класса невидима и обычным прогоном, и обычным логом. Печатают их
    `pytest -l`, отладчик и сборщики отчётов об ошибках.
    """
    rendered = "".join(
        traceback.TracebackException.from_exception(error, capture_locals=True).format()
    )
    return [
        line.strip()
        for line in rendered.splitlines()
        if FAKE_ACCESS in line or FAKE_REFRESH in line
    ]


def test_no_token_in_the_failure_of_a_backfill(store: TokenStore) -> None:
    """Обрыв догрузки не выносит наружу ни рабочий токен, ни токен кабинета.

    Смотрятся оба текста отказа — человеческий и технический — и трассировка
    целиком, вместе с переменными кадров.

    ⚠️ **Чего этот тест не проверяет, вопреки прежней записи в нём.** Кадра
    `BrokerSession._authorized` — того самого, где лежит словарь со строкой
    `Authorization: Bearer …`, — в этой трассировке нет и быть не может.
    `_with_retries` складывает отказ в переменную, а поднимает его строкой
    `raise last` **вне** блока `except`: у `HistoryIncomplete` трассировка
    начинается заново, кадры httpx-отказа в неё не попадают, и `__context__`
    тоже пуст. Проверено мутацией 04.09.2026: `_authorized` без
    `finally: headers.clear()` оставлял этот тест зелёным.

    Кадр с заголовком живёт на другом пути — когда отказ **не** httpx-овский
    и проходит `_authorized` насквозь. Его стережёт соседний
    `test_a_non_httpx_failure_mid_backfill_leaves_no_header_in_frame_locals`,
    а в слое сессии — `test_no_access_token_in_frame_locals_of_a_non_httpx_failure`
    (`tests/test_broker_no_leak.py`).
    """
    broker = Broker(
        answer(bar("2026-09-03T07:00:00Z")),
        broken(httpx.ConnectError("связь пропала")),
    )
    try:
        fetch(
            store,
            broker,
            start=datetime(2026, 9, 3, 10, 0, tzinfo=MSK),
            end=datetime(2026, 9, 4, 10, 0, tzinfo=MSK),
        )
    except HistoryIncomplete as broken_download:
        # Имя переезжает наружу нарочно: `except X as имя` стирает имя
        # на выходе из блока, и проверка ниже осталась бы без предмета.
        failure = broken_download
        printed = "".join(
            traceback.TracebackException.from_exception(
                broken_download, capture_locals=True
            ).format()
        )
    else:
        pytest.fail("догрузка не оборвалась — тест перестал проверять то, ради чего стоит")

    for secret in (FAKE_ACCESS, FAKE_REFRESH):
        assert secret not in printed, "токен вышел из слоя в трассировке"
    assert FAKE_ACCESS not in str(failure)
    assert FAKE_ACCESS not in failure.technical


@pytest.mark.parametrize(
    ("name", "failure"),
    [
        ("отмена задачи при закрытии окна", asyncio.CancelledError()),
        ("нехватка памяти", MemoryError()),
        ("чужой RuntimeError", RuntimeError("что-то пошло не так внутри")),
    ],
)
def test_a_non_httpx_failure_mid_backfill_leaves_no_header_in_frame_locals(
    store: TokenStore, name: str, failure: BaseException
) -> None:
    """Отказ не от httpx проходит `_authorized` насквозь — и без заголовка.

    Это тот путь догрузки, на котором кадр с `Authorization: Bearer …`
    в трассировке **есть**: `_with_retries` ловит только исключения httpx,
    всё прочее летит через кадр наружу и уносит кадр с собой.

    Главный случай — **отмена задачи**: владелец счёта закрывает окно или жмёт
    «Стоп», пока догружаются пропущенные свечи после обрыва, и asyncio штатно
    печатает непойманный отказ задачи через `loop.call_exception_handler`.
    То есть речь не о редком отказе, а об обычном выходе из программы.
    """
    broker = Broker(broken(failure))
    try:
        fetch(store, broker)
    except BaseException as error:  # noqa: BLE001 — ловим любой, в том числе отмену
        caught: BaseException = error
    else:
        pytest.fail("подставленный отказ не долетел — тест остался без предмета")

    assert type(caught) is type(failure), f"{name}: отказ подменился на {caught!r}"
    offenders = frames_with_secret(caught)
    assert not offenders, f"{name}: значение токена в переменных кадра:\n  " + "\n  ".join(
        offenders
    )


def test_the_search_for_a_leak_would_find_a_planted_one() -> None:
    """Проверка выше не выродилась: подложенная утечка обязана находиться."""
    try:
        raise RuntimeError(f"заголовок: Bearer {FAKE_ACCESS}")
    except RuntimeError as planted:
        printed = "".join(
            traceback.TracebackException.from_exception(
                planted, capture_locals=True
            ).format()
        )
    assert FAKE_ACCESS in printed, "поиск утечки не находит даже подложенную"
