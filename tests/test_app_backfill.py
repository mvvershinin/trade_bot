"""Догрузка пропущенного: дыра закрывается, ночь дырой не считается.

Что здесь настоящее и что подделано
-----------------------------------
База — **настоящая** SQLite во временном файле: правило источника, ранги
и учёт дней живут в ней, и подделывать их значило бы проверять подделку.

Брокер — подделка `FakeHistory` с той же подписью, что у `broker.candles.
History` (сверяется отдельным сторожем): токен в разработке не участвует,
живого запроса здесь нет ни одного.

Биржа — **настоящий** `IssClient` поверх подставного транспорта. Разбор
ответа, склейка страниц, снятие дублей и политика повторов при этом работают
рабочим кодом; подделан ровно один слой — тот, который открывает сокет.

⚠️ Сторож `the_real_transport_is_disarmed` не декоративный. 05.09.2026 первая
же редакция отката на биржу увела этот файл **в настоящий интернет**:
`MarketWorker` без своего клиента собирает `IssClient()` с транспортом на httpx,
и тест молча скачал 55 настоящих минуток MXU6 за 19.06.2026. Прогон при этом
был зелёным.
Тест, ходящий в сеть, падает от чужого сбоя и зеленеет от чужой удачи —
поэтому теперь настоящий транспорт в этом файле обезврежен целиком, и любая
попытка выйти наружу падает громко.

Токен не участвует ни в каком виде — ни настоящий, ни выдуманный.

Форма ряда взята из наблюдения владельца счёта 04.09.2026: минуты до 13:07
записаны, час потерян на обрыве, минуты с 14:03 записаны потоком после
переподключения. На такой форме и ломается наивное «последняя минутка + 1».
"""

from __future__ import annotations

import asyncio
import inspect
import pathlib
from collections.abc import Callable, Coroutine, Sequence
from datetime import date, datetime, timedelta, timezone
from typing import TypeVar

import pytest
from market_helpers import FakeTransport, iss_body, no_sleep, pages_handler

from app.backfill import (
    EXCHANGE_WORDS,
    Backfiller,
    BrokerMinutes,
    ExchangeMinutes,
    covered_days,
    to_minute,
)
from broker.candles import HistoricalCandle, History, HistoryIncomplete
from broker.errors import NoConnection, NotFound
from market import (
    MSK,
    Candle,
    CandleStore,
    HttpxTransport,
    IssClient,
    IssHttpError,
    LoadReport,
    MarketWorker,
    Source,
    WriteStats,
    pick_new,
)
from ui.models import DecisionLevel

_T = TypeVar("_T")

MINUTE = timedelta(minutes=1)
TICKER = "MXU6"
CLASS = "SPBFUT"
#: Настоящий режим торгов МосБиржи, которому в `ISS_MARKETS` рынка не назначено.
#: Взят настоящий, а не выдуманный: выдуманный проверял бы «программа не знает
#: чепухи», а вопрос в другом — что она делает с **облигациями**, которые есть
#: и которыми не занимались.
BONDS = "TQOB"

#: Пятница 19.06.2026 — торговый день, и он **заведомо в прошлом** для любого
#: прогона. Даты не «сегодняшние» намеренно: граница догрузки режется ещё
#: и по часам программы (`Backfiller.run`), а `mark_days_requested` закрывает
#: только уже кончившийся день. На плавающей дате обе проверки то работали бы,
#: то нет — в зависимости от того, когда запустили прогон.
OPEN = datetime(2026, 6, 19, 13, 0, tzinfo=MSK)
#: Дыра владельца счёта: 13:08…14:02 включительно, 55 минут.
HOLE = OPEN + timedelta(minutes=8)
#: Первая минута, которую поток записал после переподключения.
BACK = OPEN + timedelta(minutes=63)  # 14:03
#: Сколько минут поток успел записать после переподключения. Двенадцать,
#: а не восемь: на этом нахлёсте меряется единица объёма (`RATIO_SAMPLE` = 10),
#: и на восьми замер молчал бы, а проверка про единицы стала бы вакуумной.
AFTER = 12
#: Минута первого снимка подписки — граница: с неё и правее ведёт поток.
EDGE = BACK + timedelta(minutes=AFTER)  # 14:15


def one_minute(moment: datetime, *, close: float = 100_000.0, volume: float = 7.0) -> Candle:
    return Candle(
        time=moment,
        open=close,
        high=close + 40.0,
        low=close - 40.0,
        close=close,
        volume=volume,
    )


def minute_row(start: datetime, count: int) -> list[Candle]:
    """Подряд идущие минутки — то, что отдаёт биржа за отрезок."""
    return [one_minute(start + MINUTE * i) for i in range(count)]


def broker_bar(
    moment: datetime,
    *,
    close: float = 100_000.0,
    contracts: float = 7.0,
    turnover: float | None = None,
    high: float | None = None,
    low: float | None = None,
) -> HistoricalCandle:
    """Свеча в том виде, в каком её отдаёт `broker/candles.py`: время с поясом.

    ⚠️ У брокера в `volume` лежит **оборот в рублях**, а не контракты
    (`market.volume`, замер 05.09.2026). Поэтому здесь называется число
    контрактов, а оборот считается из него: свеча с семёркой в колонке
    объёма — это свеча, которой у брокера не бывает, и проверка на ней
    проверяла бы несуществующий случай.

    `turnover` называется прямо там, где смысл проверки — именно в обороте:
    пришли контракты вместо оборота, множитель не тот, деление не сошлось.

    ⚠️ Оборот считается **по середине разброса**, и середина здесь написана
    руками, а не взята из `market.volume.bar_price`. Заготовка, считающая
    тем же кодом, который проверяют, соглашается с любой его поломкой:
    подмени `bar_price` на закрытие — и оборот подстроится, а прогон
    останется зелёным.
    """
    top = close + 40.0 if high is None else high
    bottom = close - 40.0 if low is None else low
    return HistoricalCandle(
        ticker=TICKER,
        class_code=CLASS,
        timeframe="M1",
        opened_at=moment,
        open=close,
        high=top,
        low=bottom,
        close=close,
        volume=contracts * (top + bottom) / 2 if turnover is None else turnover,
    )


def run(coroutine: Callable[[], Coroutine[object, object, _T]]) -> _T:
    """Один цикл событий на тест. Qt здесь не нужен: порт подделан."""
    return asyncio.run(coroutine())


class FakeHistory:
    """Подмена `broker.candles.History`: сценарий ответов и список запросов.

    ⚠️ Дублёр повторяет **смысл** оригинала, а не тот минимум, при котором
    тесты замолчат: запрошенный период запоминается целиком, потому что
    половина проверок здесь именно про него — «с какой минуты попросили»
    и есть ответ на вопрос, найдена ли дыра в середине ряда.
    """

    def __init__(self, *answers: object, expect_class: str = CLASS) -> None:
        self.answers = list(answers)
        self.asked: list[tuple[datetime, datetime]] = []
        #: Какой класс инструмента здесь считается своим. Поле, а не
        #: константа: один тест проверяет поведение при классе, которому
        #: не сопоставлен рынок биржи, и сторож «чужой инструмент» обязан
        #: остаться сторожем во всех остальных.
        self.expect_class = expect_class

    async def candles(
        self,
        *,
        ticker: str,
        class_code: str,
        start: datetime,
        end: datetime,
        timeframe: str = "M1",
    ) -> tuple[HistoricalCandle, ...]:
        self.asked.append((start, end))
        assert (ticker, class_code, timeframe) == (TICKER, self.expect_class, "M1"), (
            f"догрузка спросила чужой инструмент: {ticker} {class_code} {timeframe}"
        )
        answer = self.answers.pop(0) if self.answers else ()
        if isinstance(answer, BaseException):
            raise answer
        assert isinstance(answer, (list, tuple))
        return tuple(answer)


class FakePort:
    """Четыре вызова, которые догрузка делает наружу, — списками."""

    def __init__(self, *, instrument: str = TICKER) -> None:
        self.instrument = instrument
        self.said: list[tuple[str, str, DecisionLevel]] = []
        self.confirmed: list[tuple[str, datetime]] = []
        self.refreshed: list[str] = []
        #: Остановки робота: событие и причина. Пусто — робот не остановлен.
        self.halts: list[tuple[str, str]] = []
        #: Двигается ли граница на очередном подтверждении. Порт отвечает
        #: `False`, когда граница уже стоит правее (`HistoryPort._confirm`).
        self.moves = True

    def note(
        self, event: str, reason: str, level: DecisionLevel = DecisionLevel.INFO
    ) -> None:
        self.said.append((event, reason, level))

    def history_confirmed(self, symbol: str, until: datetime) -> bool:
        self.confirmed.append((symbol, until))
        return self.moves

    def refresh(self, why: str = "") -> None:
        self.refreshed.append(why)

    def halt(self, event: str, reason: str) -> None:
        self.halts.append((event, reason))

    def warnings(self) -> list[str]:
        return [
            reason for _, reason, level in self.said if level is DecisionLevel.WARNING
        ]

    def texts(self) -> str:
        return "\n".join(reason for _, reason, _ in self.said)


def base_with_hole(tmp_path: pathlib.Path) -> pathlib.Path:
    """База владельца счёта: восемь минут, час пустоты, восемь минут потока."""
    path = tmp_path / "candles.sqlite3"
    with CandleStore(path) as store:
        store.put_minutes(
            TICKER, [one_minute(OPEN + MINUTE * i) for i in range(8)], Source.ISS
        )
        store.put_minutes(
            TICKER, [one_minute(BACK + MINUTE * i) for i in range(AFTER)], Source.BROKER
        )
    return path


@pytest.fixture(autouse=True)
def the_real_transport_is_disarmed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Настоящий транспорт биржи обезврежен на весь файл.

    Сторож по факту, а не из осторожности: первая редакция отката молча
    скачала настоящие данные с `iss.moex.com` и прогон остался зелёным
    (разбор — в шапке модуля). Подставить транспорт «везде, где вспомнил» —
    это и есть тот способ, которым забывают в одном месте.

    ⚠️ Имя — не `no_internet`, и это `B-034`. Так фикстура и называлась,
    ровно как автоматический сторож `tests/conftest.py::no_internet`,
    и потому **отменяла его на весь файл**: сокет наружу переставал быть
    закрыт, а `conftest` при этом объявлял обход невозможным. Здесь
    обезврежен транспорт биржи — один слой, а не сеть целиком, — и имя
    говорит именно это. Сокетный сторож работает вторым рубежом.
    """

    def refuse(self: object, url: str, *, timeout: float) -> bytes:
        raise AssertionError(
            f"тест полез в настоящий интернет: {url}. Биржа в этом файле "
            "подставная — передайте `exchange=` в `backfilled`"
        )

    monkeypatch.setattr(HttpxTransport, "get", refuse)


def iss_denies(_url: str) -> bytes:
    """Биржа отказала. `IssHttpError` мимо повторов — ждать нечего."""
    raise IssHttpError(404, "проверка: биржи нет")


def iss_gives(candles: Sequence[Candle]) -> Callable[[str], bytes]:
    """Биржа отдаёт эти свечи одной страницей, дальше — пусто."""
    handler: Callable[[str], bytes] = pages_handler([iss_body(list(candles))])
    return handler


def exchange_client(handler: Callable[[str], bytes]) -> tuple[IssClient, FakeTransport]:
    """Настоящий `IssClient` поверх подставного транспорта, без пауз."""
    transport = FakeTransport(handler)
    return IssClient(transport, sleep=no_sleep, pause=0.0), transport


async def backfilled(
    path: pathlib.Path,
    history: FakeHistory,
    port: FakePort,
    *,
    boundary: datetime = EDGE,
    exchange: Callable[[str], bytes] = iss_denies,
    class_code: str = CLASS,
) -> tuple[list[Candle], LoadReport]:
    """Прогнать один заход догрузки и отдать минутки базы после него.

    ⚠️ Биржа по умолчанию **отказывает**. Умолчание «биржа отдаёт всё»
    сделало бы половину проверок этого файла бессмысленными: поломка в пути
    к брокеру молча закрывалась бы откатом, и прогон остался бы зелёным.
    Кому нужен работающий откат — называет его явно.
    """
    client, _ = exchange_client(exchange)
    async with MarketWorker(path, iss=client) as worker:
        filler = Backfiller(
            history,
            worker,
            port,
            ticker=TICKER,
            class_code=class_code,
        )
        report = await filler.run(boundary)
        return await worker.minutes(TICKER), report


# -- сторож дублёра ---------------------------------------------------------


def test_the_history_double_answers_exactly_what_the_real_one_promises() -> None:
    """Подделка брокера обязана иметь ту же подпись, что и настоящая.

    Дублёр, разошедшийся с оригиналом, делает зелёным прогон, в котором
    рабочий код упал бы: догрузка зовёт `candles` только по имени параметра,
    и переименование `start` в `since` в `broker/candles.py` здесь молча
    прошло бы.
    """
    real = inspect.signature(History.candles)
    double = inspect.signature(FakeHistory.candles)

    assert list(real.parameters) == list(double.parameters), (
        f"подпись разошлась: у оригинала {list(real.parameters)}, "
        f"у дублёра {list(double.parameters)}"
    )
    assert inspect.iscoroutinefunction(History.candles)
    assert inspect.iscoroutinefunction(FakeHistory.candles)


# -- дыра найдена и закрыта -------------------------------------------------


def test_the_hole_in_the_middle_is_asked_for_and_filled(tmp_path: pathlib.Path) -> None:
    """Пустой час закрывается, и запрос начинается с первой пропавшей минуты.

    Это ровно то, на что жаловался владелец счёта: минуты после дыры пишутся,
    а сама дыра остаётся. Хвостовая догрузка попросила бы с 14:11 и не нашла
    бы ничего.
    """
    history = FakeHistory([broker_bar(HOLE + MINUTE * i) for i in range(55)])
    port = FakePort()
    minutes, report = run(lambda: backfilled(base_with_hole(tmp_path), history, port))

    assert history.asked == [(HOLE, EDGE)], (
        f"спросили не тот период: {history.asked}; ждали с первой дыры {HOLE}"
    )
    assert len(minutes) == 8 + 55 + AFTER, f"дыра не закрыта: минуток в базе {len(minutes)}"
    assert report.inserted == 55
    assert "Догружено 55 минут за 19.06 13:08–14:03" in port.texts(), (
        f"в журнале нет человеческой строки о догрузке:\n{port.texts()}"
    )
    assert port.refreshed, (
        "график не перерисован: заполненная **середина** ряда через путь живой "
        "свечи на экран не попадает вовсе, и дыра осталась бы на графике"
    )


def test_the_filled_minutes_carry_the_broker_history_source(tmp_path: pathlib.Path) -> None:
    """Догруженное отличимо от потока и от биржи — колонкой источника.

    Объём догруженной минуты **выведен** делением оборота на цену,
    а не измерен (`market.volume`). Пока это так, такая минута обязана быть
    узнаваема: по колонке источника её заменяет первая же заливка с биржи,
    а без колонки выведенное и итоговое лежали бы вперемешку.
    """
    path = base_with_hole(tmp_path)
    history = FakeHistory([broker_bar(HOLE + MINUTE * i) for i in range(55)])
    run(lambda: backfilled(path, history, FakePort()))

    with CandleStore(path) as store:
        counts = store.source_counts(TICKER)

    assert counts == {"iss": 8, "broker": AFTER, "broker_history": 55}, (
        f"источники в базе перепутаны: {counts}"
    )


def test_the_confirmed_edge_moves_to_the_boundary_not_to_the_last_row(
    tmp_path: pathlib.Path,
) -> None:
    """Подтверждается граница потока, а не конец базы.

    В базе к этому моменту уже лежат минуты, записанные потоком в этом сеансе
    (14:03…14:10). Их полноту никто не проверял, и сдвиг границы до конца базы
    выдал бы поток за подтверждённые биржей данные.
    """
    history = FakeHistory([broker_bar(HOLE + MINUTE * i) for i in range(55)])
    port = FakePort()
    run(lambda: backfilled(base_with_hole(tmp_path), history, port))

    assert port.confirmed == [(TICKER, EDGE)], (
        f"подтверждено не по границу потока: {port.confirmed}"
    )


def test_an_empty_answer_still_confirms_the_stretch_and_says_it_plainly(
    tmp_path: pathlib.Path,
) -> None:
    """Спросили — брокер не дал: пустота доказана, и так и сказано.

    Это и есть ответ владельцу счёта на вопрос «мы потеряли или торгов
    не было». Различить их по самому ряду нельзя; различает **факт запроса**.
    """
    history = FakeHistory([])
    port = FakePort()
    minutes, _ = run(lambda: backfilled(base_with_hole(tmp_path), history, port))

    assert len(minutes) == 8 + AFTER, "из пустого ответа что-то записалось"
    assert port.confirmed == [(TICKER, EDGE)], (
        "пустой ответ не подтвердил отрезок — подпись «Данных нет» осталась бы "
        "недоказанной, и неполные бары продолжили бы выпадать из прогона"
    )
    said = port.texts()
    assert "13:08–14:03" in said and "не потеря программы" in said, (
        f"владельцу счёта не сказано, что пустота настоящая:\n{said}"
    )


def test_every_journal_line_of_the_backfill_names_the_instrument(
    tmp_path: pathlib.Path,
) -> None:
    """Событие догрузки называет тикер, иначе строка читается наоборот.

    Подписка следует за настройкой окна (`B-020`), и заход догрузки прежнего
    инструмента доживает своё уже после смены. Владелец счёта менял MXU6
    на RIU6 и читал «Догружено 47 минут, пропуск закрыт» — при том, что
    на экране RIU6 с незакрытой дырой. Строка правдива и понята будет
    наоборот.

    Проверяются **все** строки захода, а не одна: пропущенная строка —
    это ровно тот случай, ради которого проверка написана.
    """
    history = FakeHistory([])
    port = FakePort()
    run(lambda: backfilled(base_with_hole(tmp_path), history, port))

    assert port.said, "заход не сказал ничего — проверять нечего"
    unnamed = [event for event, _, _ in port.said if TICKER not in event]
    assert unnamed == [], f"события догрузки без инструмента: {unnamed}"


# -- ночь, выходные и слишком старая база -----------------------------------


def test_a_base_older_than_the_window_is_not_a_hole_and_the_broker_is_not_asked(
    tmp_path: pathlib.Path,
) -> None:
    """Ночь, выходные и «месяц не запускали» дырой обрыва не считаются.

    Иначе каждое подключение начиналось бы с запроса на четверо суток
    пустоты, а месячная дыра — с сотен запросов у брокера, у которого потолок
    1440 баров и ограничение частоты. Глубокая история берётся с биржи.
    """
    history = FakeHistory([broker_bar(HOLE)])
    port = FakePort()
    late = EDGE + timedelta(days=30)
    minutes, _ = run(
        lambda: backfilled(base_with_hole(tmp_path), history, port, boundary=late)
    )

    assert history.asked == [], (
        f"догрузка полезла к брокеру за месяцем истории: {history.asked}"
    )
    assert len(minutes) == 8 + AFTER, "в базу что-то записалось при отказе догрузки"
    assert port.confirmed == [], "граница доверия сдвинута через незакрытую дыру"
    assert any("с биржи" in reason for reason in port.warnings()), (
        f"не сказано, чем это лечится:\n{port.texts()}"
    )


def test_nothing_is_said_when_the_series_has_no_holes(tmp_path: pathlib.Path) -> None:
    """Дыр нет — ни запроса, ни строки в журнале.

    Строка «пропусков нет» на каждом подключении — это шум, за которым
    перестанут читать строки по делу.
    """
    path = tmp_path / "whole.sqlite3"
    with CandleStore(path) as store:
        store.put_minutes(
            TICKER, [one_minute(OPEN + MINUTE * i) for i in range(75)], Source.ISS
        )
    history = FakeHistory()
    port = FakePort()
    run(lambda: backfilled(path, history, port))

    assert history.asked == []
    assert port.said == [], f"журнал засорён при полном ряде: {port.said}"
    assert port.refreshed == [], "прогон запущен впустую"


def test_a_night_left_empty_is_not_reported_as_a_lost_stretch(
    tmp_path: pathlib.Path,
) -> None:
    """Ночь остаётся пустой и в журнал отдельной строкой не идёт.

    Ночной перерыв и выходные дают пропуск каждый календарный день. Названные
    поимённо, они топят те несколько строк, ради которых журнал и ведётся, —
    и владелец счёта перестаёт его читать. Отрезок при этом всё равно
    запрашивается: пустой ответ на него и есть доказательство «торгов не было».
    """
    path = tmp_path / "overnight.sqlite3"
    evening = datetime(2026, 6, 18, 23, 45, tzinfo=MSK)
    morning = datetime(2026, 6, 19, 10, 0, tzinfo=MSK)
    with CandleStore(path) as store:
        store.put_minutes(
            TICKER, [one_minute(evening + MINUTE * i) for i in range(5)], Source.ISS
        )
        store.put_minutes(
            TICKER, [one_minute(morning + MINUTE * i) for i in range(AFTER)], Source.BROKER
        )
    history = FakeHistory([])
    port = FakePort()
    run(
        lambda: backfilled(
            path, history, port, boundary=morning + MINUTE * AFTER
        )
    )

    assert history.asked, "ночной отрезок даже не запросили — пустота не доказана"
    assert not any("Пропуск остался пустым" == event for event, _, _ in port.said), (
        f"ночь названа потерянным пропуском:\n{port.texts()}"
    )


def test_the_rank_alone_forbids_the_backfill_to_touch_an_existing_row(
    tmp_path: pathlib.Path,
) -> None:
    """Второй рубеж проверяется отдельно от первого: один ранг, без отбора.

    Отбор до записи (`pick_new`) и ранг источника закрывают одно и то же
    двумя способами, и проверять их вместе значит не проверить ни одного:
    поломка отбора прошла бы молча, потому что её прикрыл бы ранг. Здесь
    в хранилище подаётся то, чего отбор не пропустил бы никогда.
    """
    path = tmp_path / "ranks.sqlite3"
    with CandleStore(path) as store:
        store.put_minutes(TICKER, [one_minute(OPEN, close=1.0)], Source.ISS)
        store.put_minutes(TICKER, [one_minute(BACK, close=2.0)], Source.BROKER)

        over_iss = store.put_minutes(
            TICKER, [one_minute(OPEN, close=999.0)], Source.BROKER_HISTORY
        )
        over_stream = store.put_minutes(
            TICKER, [one_minute(BACK, close=999.0)], Source.BROKER_HISTORY
        )
        prices = [candle.close for candle in store.minutes(TICKER)]

        # Обратное направление: биржа поверх догрузки — переписывает.
        store.put_minutes(TICKER, [one_minute(HOLE, close=5.0)], Source.BROKER_HISTORY)
        healed = store.put_minutes(TICKER, [one_minute(HOLE, close=6.0)], Source.ISS)
        after = {candle.time: candle.close for candle in store.minutes(TICKER)}

    assert prices == [1.0, 2.0], f"догрузка переписала существующие строки: {prices}"
    assert (over_iss.kept, over_stream.kept) == (1, 1), (
        f"хранилище не отбило запись рангом: {over_iss}, {over_stream}"
    )
    assert healed.updated == 1 and after[HOLE] == 6.0, (
        "биржа не переписала догруженную минуту — единица объёма осталась бы "
        "неисправленной навсегда"
    )


# -- повтор -----------------------------------------------------------------


def test_running_the_backfill_twice_leaves_the_same_data(tmp_path: pathlib.Path) -> None:
    """Запустили дважды — данных столько же, и второй заход ничего не пишет.

    Дёрганая связь даёт подключение за подключением, и каждое зовёт догрузку.
    Небезопасный повтор переписывал бы уже проверенные минуты худшими данными.
    """
    path = base_with_hole(tmp_path)
    answer = [broker_bar(HOLE + MINUTE * i) for i in range(55)]

    first_history = FakeHistory(list(answer))
    after_first, first = run(lambda: backfilled(path, first_history, FakePort()))
    second_history = FakeHistory(list(answer))
    after_second, second = run(lambda: backfilled(path, second_history, FakePort()))

    assert len(after_first) == len(after_second) == 8 + 55 + AFTER
    assert [candle.time for candle in after_first] == [
        candle.time for candle in after_second
    ]
    assert first.inserted == 55
    assert second.inserted == 0, f"повтор записал ещё {second.inserted} минут"
    assert second_history.asked == [], (
        f"второй заход снова полез к брокеру: {second_history.asked}"
    )


def test_the_backfill_never_rewrites_a_minute_the_stream_already_wrote(
    tmp_path: pathlib.Path,
) -> None:
    """Существующую строку догрузка не трогает — ни ценами, ни объёмом.

    Два рубежа проверяются разом: отбор до записи и ранг источника
    `broker_history` = 0, ниже потока. У потока объём посчитан по приращениям
    оборота внутри минуты и сверен с биржей до контракта, у HTTP-ответа он
    выведен из одного итогового оборота; обрезанный `close` сдвинул бы
    среднюю и дал бы переворот там, где его нет.
    """
    path = base_with_hole(tmp_path)
    fake = [
        broker_bar(BACK + MINUTE * i, close=1.0, contracts=999_999.0)
        for i in range(AFTER)
    ]
    history = FakeHistory([*[broker_bar(HOLE + MINUTE * i) for i in range(55)], *fake])
    minutes, report = run(lambda: backfilled(path, history, FakePort()))

    stream_minutes = [candle for candle in minutes if candle.time >= BACK]
    assert [candle.close for candle in stream_minutes] == [100_000.0] * AFTER, (
        "догрузка переписала минуты, которые ведёт поток"
    )
    assert report.kept == 0 and report.updated == 0, (
        f"строки дошли до записи и были отбиты только рангом: kept={report.kept}, "
        f"updated={report.updated} — отбор до записи не сработал"
    )


def test_the_growing_minute_never_reaches_the_base(tmp_path: pathlib.Path) -> None:
    """Текущая, ещё набирающаяся минута не пишется никогда.

    Признака «свеча закрыта» у брокера нет ни в HTTP-ответе, ни в потоке.
    Период, дотянутый до «сейчас», вернул бы недобранную минуту: её `close`
    не окончательный, а средняя считается по нему.
    """
    now = datetime.now(MSK).replace(second=0, microsecond=0)
    path = tmp_path / "now.sqlite3"
    start = now - timedelta(minutes=20)
    with CandleStore(path) as store:
        store.put_minutes(
            TICKER, [one_minute(start + MINUTE * i) for i in range(10)], Source.BROKER
        )
    history = FakeHistory(
        [broker_bar(start + MINUTE * i) for i in range(10, 21)]  # …включая минуту `now`
    )
    minutes, _ = run(lambda: backfilled(path, history, FakePort(), boundary=now))

    assert now not in [candle.time for candle in minutes], (
        "растущая минута записана: обрезанный `close` сдвинет среднюю"
    )
    assert minutes[-1].time == now - MINUTE


def test_a_boundary_from_the_future_is_cut_back_by_the_clock(
    tmp_path: pathlib.Path,
) -> None:
    """Граница из будущего ужимается по часам программы, а не принимается.

    Второй рубеж к первому: метка сервера, ушедшая вперёд, иначе втащила бы
    в базу растущую минуту через параметр, а не через ответ брокера.
    """
    now = datetime.now(MSK).replace(second=0, microsecond=0)
    path = tmp_path / "future.sqlite3"
    start = now - timedelta(minutes=20)
    with CandleStore(path) as store:
        store.put_minutes(
            TICKER, [one_minute(start + MINUTE * i) for i in range(10)], Source.BROKER
        )
    history = FakeHistory([broker_bar(start + MINUTE * i) for i in range(10, 30)])
    run(
        lambda: backfilled(
            path, history, FakePort(), boundary=now + timedelta(minutes=8)
        )
    )

    assert history.asked and history.asked[0][1] == now, (
        f"период запрошен по границу из будущего: {history.asked}"
    )


# -- оборванная догрузка ----------------------------------------------------


def test_a_broken_backfill_does_not_pass_half_a_series_for_a_whole_one(
    tmp_path: pathlib.Path,
) -> None:
    """Половина ряда записана, но дыра не объявлена закрытой.

    `covered_until` — последний **полученный** бар, а не конец запрошенного
    периода. Подтвердить по нему весь отрезок значило бы оставить дыру,
    о которой уже никто не узнает.
    """
    got = tuple(broker_bar(HOLE + MINUTE * i) for i in range(20))
    history = FakeHistory(
        HistoryIncomplete(got, cause=NoConnection("обрыв в проверке"))
    )
    port = FakePort()
    minutes, report = run(lambda: backfilled(base_with_hole(tmp_path), history, port))

    assert len(minutes) == 8 + 20 + AFTER, f"записалось не то, что пришло: {len(minutes)}"
    assert port.confirmed == [(TICKER, HOLE + MINUTE * 20)], (
        f"подтверждено дальше полученного: {port.confirmed}"
    )
    assert report.missing_minutes == 35, (
        f"остаток дыры не посчитан: {report.missing_minutes}"
    )
    assert any("оборвалась" in reason for reason in port.warnings()), (
        f"обрыв догрузки не назван вслух:\n{port.texts()}"
    )


def test_a_broken_backfill_marks_no_day_as_fully_known(tmp_path: pathlib.Path) -> None:
    """Оборванная догрузка не отмечает ни одного дня загруженным.

    Отметка необратима: `days_to_request` больше этот день не отдаст,
    и недокачанный отрезок останется дырой навсегда — при зелёном журнале.
    """
    path = tmp_path / "two-days.sqlite3"
    #: Ряд подобран так, чтобы **целый** день 18.06 лёг внутрь полученной
    #: части: иначе отмечать было бы нечего и при поломке, и проверка
    #: молчала бы (замер мутацией 04.09.2026 — ровно так и было).
    before = datetime(2026, 6, 17, 23, 50, tzinfo=MSK)
    inside = datetime(2026, 6, 18, 10, 0, tzinfo=MSK)
    with CandleStore(path) as store:
        store.put_minutes(TICKER, [one_minute(before)], Source.ISS)
        store.put_minutes(
            TICKER, [one_minute(BACK + MINUTE * i) for i in range(AFTER)], Source.BROKER
        )
    got = (
        *(broker_bar(inside + MINUTE * i) for i in range(20)),
        broker_bar(datetime(2026, 6, 19, 0, 30, tzinfo=MSK)),
    )
    history = FakeHistory(
        HistoryIncomplete(got, cause=NoConnection("обрыв в проверке"))
    )
    run(lambda: backfilled(path, history, FakePort()))

    with CandleStore(path) as store:
        assert store.settled_days(TICKER) == set(), (
            "день отмечен загруженным по оборванному ответу: `days_to_request` "
            "больше его не отдаст, и недокачанное останется дырой навсегда"
        )


def test_when_no_source_answers_the_hole_stays_open_and_unmarked(
    tmp_path: pathlib.Path,
) -> None:
    """Отказали оба источника: ни записи, ни подтверждения, ни отметки дня.

    Второй источник делает опасной ровно одну вещь — молчаливое «закрыто».
    Пока хоть кто-то ответил, дыра доказана; когда не ответил никто, дыра
    обязана остаться открытой во **всех** учётах сразу: в базе, в границе
    подтверждения и в отметке дней. Отметка необратима, и поставленная
    по неответу она закрыла бы день навсегда.
    """
    path = base_with_hole(tmp_path)
    history = FakeHistory(NoConnection("обрыв в проверке"))
    port = FakePort()
    minutes, report = run(lambda: backfilled(path, history, port))

    assert len(minutes) == 8 + AFTER
    assert port.confirmed == [], "отказ обоих источников подтвердил отрезок"
    assert port.refreshed == [], "прогон запущен после отказа"
    assert report.inserted == 0
    with CandleStore(path) as store:
        assert store.settled_days(TICKER) == set(), (
            "день отмечен загруженным, хотя свечей не дал никто"
        )
    said = port.warnings()
    assert any("догрузить не удалось" in reason for reason in said), (
        f"отказ не дошёл до журнала решений:\n{port.texts()}"
    )
    assert any("у брокера" in reason and "у биржи" in reason for reason in said), (
        f"в журнале не названы оба источника — непонятно, что уже перепробовано:"
        f"\n{port.texts()}"
    )


# -- учёт дней --------------------------------------------------------------


def test_a_day_asked_about_end_to_end_is_marked_and_a_half_day_is_not(
    tmp_path: pathlib.Path,
) -> None:
    """Отметку в учёте получает только день, про который спросили целиком.

    Отметка означает «за этот день спрашивали весь день», и `CandleStore.bars`
    после неё перестаёт звать хвостовой бар дня незакрытым. День, задетый
    краем запроса, такой отметки не заслуживает: про его вторую половину
    никто не спрашивал, а закрыть его навсегда учёт позволяет один раз.
    """
    path = tmp_path / "spanning.sqlite3"
    first = datetime(2026, 6, 17, 23, 50, tzinfo=MSK)
    with CandleStore(path) as store:
        store.put_minutes(TICKER, [one_minute(first)], Source.ISS)
        store.put_minutes(
            TICKER, [one_minute(BACK + MINUTE * i) for i in range(AFTER)], Source.BROKER
        )
    whole_day = datetime(2026, 6, 18, 10, 0, tzinfo=MSK)
    history = FakeHistory([broker_bar(whole_day + MINUTE * i) for i in range(5)])
    run(lambda: backfilled(path, history, FakePort()))

    with CandleStore(path) as store:
        marked = store.settled_days(TICKER)

    assert marked == {date(2026, 6, 18)}, (
        "отмечены не те дни: 17.06 задет краем запроса, 19.06 обрывается "
        f"на границе потока — {sorted(marked)}"
    )


def test_a_day_the_broker_gave_nothing_for_is_not_marked(tmp_path: pathlib.Path) -> None:
    """Пустой ответ за целый день загруженным его не делает.

    Выходной объясняет пустоту законно, но так же выглядит и сбой у брокера.
    Ошибка в пользу «загружено» необратима, ошибка в другую сторону стоит
    одного лишнего запроса.
    """
    path = tmp_path / "empty-day.sqlite3"
    first = datetime(2026, 6, 17, 23, 50, tzinfo=MSK)
    with CandleStore(path) as store:
        store.put_minutes(TICKER, [one_minute(first)], Source.ISS)
        store.put_minutes(
            TICKER, [one_minute(BACK + MINUTE * i) for i in range(AFTER)], Source.BROKER
        )
    run(lambda: backfilled(path, FakeHistory([]), FakePort()))

    with CandleStore(path) as store:
        assert store.settled_days(TICKER) == set(), (
            "день без единой свечи объявлен загруженным — дыра закрыта навсегда"
        )


def test_covered_days_takes_only_whole_days() -> None:
    """Календарный день попадает в отметку, только если лежит в запросе целиком."""
    since = datetime(2026, 6, 17, 23, 50, tzinfo=MSK)
    until = datetime(2026, 6, 20, 14, 11, tzinfo=MSK)

    assert covered_days(since, until) == [date(2026, 6, 18), date(2026, 6, 19)]
    assert covered_days(since, since + timedelta(minutes=5)) == []


# -- часовой пояс и единицы объёма ------------------------------------------


def test_a_utc_candle_from_the_broker_is_stored_as_the_same_moscow_instant(
    tmp_path: pathlib.Path,
) -> None:
    """Брокер шлёт UTC, база живёт в МСК — момент тот же, представление другое.

    Снятый пояс сдвинул бы свечи на три часа молча: торговое окно 10:05–11:00
    московское, и список сделок стал бы другим, ничего при этом не уронив.
    """
    utc_hole = HOLE.astimezone(timezone.utc)
    assert utc_hole.hour == 10, "проверка вакуумна: момент и так в МСК"

    history = FakeHistory([broker_bar(utc_hole)])
    minutes, _ = run(lambda: backfilled(base_with_hole(tmp_path), history, FakePort()))

    written = [candle.time for candle in minutes if candle.time == HOLE]
    assert written == [HOLE], f"минутка легла не туда: {[c.time for c in minutes]}"
    assert to_minute(broker_bar(utc_hole)).time.utcoffset() == timedelta(hours=3)


def test_the_turnover_of_the_broker_is_written_as_contracts(
    tmp_path: pathlib.Path,
) -> None:
    """Оборот брокера доходит до базы контрактами, а не рублями.

    Главная проверка `D-013`. Замер 05.09.2026 на базе владельца счёта:
    объём брокера ÷ наш = 2,199 × 10⁵ на 3858 общих минутах — то есть
    в колонке контрактов лежали рубли. Здесь то же самое в маленьком
    масштабе: 7 контрактов по 100 000 ₽ дают оборот 700 000, и записаться
    обязана семёрка.
    """
    path = base_with_hole(tmp_path)
    history = FakeHistory([broker_bar(HOLE + MINUTE * i) for i in range(55)])
    minutes, _ = run(lambda: backfilled(path, history, FakePort()))

    filled = [candle for candle in minutes if HOLE <= candle.time < BACK]
    assert len(filled) == 55, f"дыра не закрыта: {len(filled)}"
    assert {candle.volume for candle in filled} == {7.0}, (
        "объём догруженных минут не пересчитан из оборота: "
        f"{sorted({candle.volume for candle in filled})}. Ждали 7 контрактов "
        "(оборот 700 000 ₽ ÷ цена 100 000 ₽), а 700 000 в этой колонке — "
        "это рубли под именем контрактов"
    )


def test_the_turnover_is_divided_by_the_middle_of_the_range(
    tmp_path: pathlib.Path,
) -> None:
    """Оборот делится на середину разброса минуты, а не на её закрытие.

    Выбор сделан по замеру на 1000 настоящих минут (`market/volume.py`):
    середина ошибается вдвое с лишним меньше. На обычном баре этого файла
    разница не видна — там разброс симметричен и середина совпадает
    с закрытием, — поэтому здесь бар закрылся на минимуме.

    Числа подобраны так, чтобы **округление не спрятало разницу**: разброс
    обычный, 0,1 % (медиана по году — 0,048 %), середина 100 050 против
    закрытия 100 000, оборот 100 150 050 ₽. По середине выходит ровно 1001
    контракт, по закрытию — 1001,5005, то есть 1002. Один контракт на тысячу:
    столько и стоит выбор цены на настоящем баре.
    """
    path = base_with_hole(tmp_path)
    history = FakeHistory(
        [broker_bar(HOLE + MINUTE * i, turnover=100_150_050.0,
                    close=100_000.0, high=100_100.0, low=100_000.0)
         for i in range(55)]
    )
    minutes, _ = run(lambda: backfilled(path, history, FakePort()))

    filled = {candle.volume for candle in minutes if HOLE <= candle.time < BACK}
    assert filled == {1001.0}, (
        f"поделили не на середину разброса: {sorted(filled)}. 1002 означает "
        "деление на цену закрытия"
    )


def test_a_volume_that_is_not_a_turnover_is_refused_whole(
    tmp_path: pathlib.Path,
) -> None:
    """Пришли контракты вместо оборота — ответ отвергнут целиком, дыру закрыла биржа.

    Сторож против **обратной** ошибки: если брокер однажды начнёт слать
    контракты, деление на цену даст 0,00007 и записало бы нули — минуту
    без сделок там, где сделки были. Записывать такое нельзя, и рядом стоит
    биржа, у которой те же минуты есть.
    """
    path = base_with_hole(tmp_path)
    history = FakeHistory(
        [broker_bar(HOLE + MINUTE * i, turnover=7.0) for i in range(55)]
    )
    port = FakePort()
    minutes, report = run(
        lambda: backfilled(
            path, history, port, exchange=iss_gives(minute_row(HOLE, 55))
        )
    )

    with CandleStore(path) as store:
        counts = store.source_counts(TICKER)

    assert counts.get("broker_history", 0) == 0, (
        f"свечи с ненастоящим оборотом записаны: {counts}"
    )
    assert counts.get("iss", 0) == 8 + 55, f"дыру закрыла не биржа: {counts}"
    assert all(candle.volume > 0 for candle in minutes), (
        "в базе появилась минута с нулевым объёмом — это «сделок не было», "
        "а сделки были"
    )
    assert "меньше половины контракта" in report.note, (
        f"причина отказа не попала в отчёт загрузки: {report.note}"
    )


def test_a_divisor_that_did_not_match_is_said_aloud(tmp_path: pathlib.Path) -> None:
    """Пересчёт не сошёлся с нашим счётом — замер на нахлёсте говорит вслух.

    Случай, ради которого полоса узкая: у контракта на РТС рубль за пункт
    равен 1,73, и оборот там больше «цена × количество» ровно во столько же.
    Пересчёт поделит только на цену, получит 12 контрактов вместо 7 — разрыв
    не на пять порядков, а всего в 1,73 раза, и прежняя полоса (1/20 … 20)
    его не заметила бы.
    """
    path = base_with_hole(tmp_path)
    rts = 1.7317
    history = FakeHistory(
        [*[broker_bar(HOLE + MINUTE * i, turnover=7.0 * 100_000.0 * rts)
           for i in range(55)],
         *[broker_bar(BACK + MINUTE * i, turnover=7.0 * 100_000.0 * rts)
           for i in range(AFTER)]]
    )
    port = FakePort()
    run(lambda: backfilled(path, history, port))

    said = [reason for reason in port.warnings() if "отличается от нашего" in reason]
    assert said, f"расхождение в 1,7 раза не названо вслух:\n{port.texts()}"
    assert "в 1.71 раза" in said[0], (
        f"названо не то число: {said[0]}. Ждали 12 контрактов вместо 7 — "
        "округление до целого делает из 1,7317 ровно 12/7"
    )
    assert "делить надо было не на неё" in said[0], (
        f"сказано про расхождение, но не сказано, что делать: {said[0]}"
    )


def test_matching_volumes_do_not_raise_a_false_alarm(tmp_path: pathlib.Path) -> None:
    """Сошедшийся пересчёт предупреждения не даёт — иначе оно ничего не значит."""
    path = base_with_hole(tmp_path)
    history = FakeHistory(
        [*[broker_bar(HOLE + MINUTE * i) for i in range(55)],
         *[broker_bar(BACK + MINUTE * i) for i in range(AFTER)]]
    )
    port = FakePort()
    run(lambda: backfilled(path, history, port))

    assert not any("Объём" in event for event, _, _ in port.said), (
        f"ложная тревога о пересчёте объёма:\n{port.texts()}"
    )


# -- отчёт о загрузке -------------------------------------------------------


def test_the_load_is_written_to_the_journal_of_loads(tmp_path: pathlib.Path) -> None:
    """Догрузка оставляет запись в журнале загрузок, как и загрузка с биржи.

    Без чисел «сколько запрошено, пришло, записано» недокачанный отрезок
    неотличим от полного.
    """
    path = base_with_hole(tmp_path)
    history = FakeHistory([broker_bar(HOLE + MINUTE * i) for i in range(50)])
    run(lambda: backfilled(path, history, FakePort()))

    with CandleStore(path) as store:
        rows = store.load_log(TICKER)

    assert len(rows) == 1, f"записей о загрузке {len(rows)}, ждали одну"
    assert rows[0]["source"] == "broker_history"
    assert rows[0]["fetched"] == 50 and rows[0]["inserted"] == 50
    assert rows[0]["missing_minutes"] == 5, (
        f"остаток дыры не попал в журнал загрузок: {rows[0]['missing_minutes']}"
    )


# -- задача и её снятие -----------------------------------------------------


def test_a_second_start_does_not_queue_a_second_backfill(tmp_path: pathlib.Path) -> None:
    """Подключение за подключением не копит очередь запросов к брокеру.

    Следующая догрузка всё равно перечитает базу и возьмёт самое свежее
    состояние; очередь из них только упёрлась бы в ограничение частоты.
    """
    path = base_with_hole(tmp_path)

    async def scenario() -> int:
        async with MarketWorker(path) as worker:
            history = FakeHistory(
                [broker_bar(HOLE + MINUTE * i) for i in range(55)],
                [broker_bar(HOLE + MINUTE * i) for i in range(55)],
            )
            filler = Backfiller(
                history,
                worker,
                FakePort(),
                ticker=TICKER,
                class_code=CLASS,
            )
            filler.start(EDGE)
            filler.start(EDGE)
            await filler.wait()
            return len(history.asked)

    assert run(scenario) == 1, "второй `start` поставил вторую догрузку в очередь"


def test_a_program_error_inside_the_backfill_reaches_the_journal(
    tmp_path: pathlib.Path,
) -> None:
    """Поломка программы не роняет поток котировок и не молчит.

    Молчание здесь — это дыра в данных, о которой никто не узнает: следующая
    строка журнала будет про закрытие минуты, как будто всё в порядке.
    """
    path = base_with_hole(tmp_path)

    class Broken(FakeHistory):
        async def candles(self, **_: object) -> tuple[HistoricalCandle, ...]:
            raise TypeError("подпись разъехалась")

    async def scenario() -> list[str]:
        async with MarketWorker(path) as worker:
            port = FakePort()
            filler = Backfiller(
                Broken(),
                worker,
                port,
                ticker=TICKER,
                class_code=CLASS,
            )
            filler.start(EDGE)
            await filler.wait()
            return port.warnings()

    warnings = run(scenario)
    assert any("догрузить не вышло" in reason for reason in warnings), (
        f"поломка догрузки не дошла до журнала: {warnings}"
    )


def test_the_same_line_is_not_repeated_on_every_reconnect(tmp_path: pathlib.Path) -> None:
    """Клиринговый перерыв пуст всегда — говорить о нём каждый раз незачем.

    На дёрганой связи подключений много, и одна и та же строка залила бы
    журнал, за которым перестанут следить.
    """
    path = base_with_hole(tmp_path)

    async def scenario() -> list[str]:
        async with MarketWorker(path) as worker:
            port = FakePort()
            filler = Backfiller(
                FakeHistory([], []),
                worker,
                port,
                ticker=TICKER,
                class_code=CLASS,
            )
            await filler.run(EDGE)
            await filler.run(EDGE)
            return [reason for _, reason, _ in port.said]

    said = run(scenario)
    assert len(said) == len(set(said)), f"строка повторена дословно: {said}"


@pytest.mark.parametrize("shown", ["SiU6"])
def test_the_backfill_talks_about_its_own_instrument_only(
    tmp_path: pathlib.Path, shown: str
) -> None:
    """Подтверждение уходит с именем инструмента — решает порт, не догрузка.

    Граница доверия в порту одна на весь порт, а догрузка идёт по тикеру
    потока. Имя обязано доехать до порта, иначе тот не сможет отличить
    «подтвердили показанное» от «подтвердили чужое».
    """
    history = FakeHistory([broker_bar(HOLE + MINUTE * i) for i in range(55)])
    port = FakePort(instrument=shown)
    run(lambda: backfilled(base_with_hole(tmp_path), history, port))

    assert port.confirmed == [(TICKER, EDGE)], (
        f"инструмент не назван в подтверждении: {port.confirmed}"
    )


# -- откат на биржу ---------------------------------------------------------


def test_the_real_transport_cannot_leave_this_file(tmp_path: pathlib.Path) -> None:
    """Сторож `the_real_transport_is_disarmed` снимает транспорт — значит, он стоит.

    Проверка самой проверки, и она по факту: 05.09.2026 этот файл молча
    скачал настоящие минутки с `iss.moex.com` и остался зелёным. Сторож,
    который не сторожит, хуже отсутствующего — по нему принимают решения.
    """
    with pytest.raises(AssertionError, match="настоящий интернет"):
        HttpxTransport().get("https://iss.moex.com/iss/whatever.json", timeout=1.0)


def test_the_broker_is_asked_first_and_the_exchange_is_not_touched(
    tmp_path: pathlib.Path,
) -> None:
    """Брокер ответил — биржу не спрашивают вовсе.

    Основной источник остаётся основным: он досчитан до текущей минуты
    заведомо (то же подключение, что прислало снимок). И день, когда путь
    к брокеру заработает, не требует правки — выбор идёт по факту ответа.
    """
    path = base_with_hole(tmp_path)
    client, transport = exchange_client(iss_gives(minute_row(HOLE, 55)))
    history = FakeHistory([broker_bar(HOLE + MINUTE * i) for i in range(55)])

    async def scenario() -> tuple[list[Candle], list[str]]:
        async with MarketWorker(path, iss=client) as worker:
            filler = Backfiller(
                history, worker, FakePort(), ticker=TICKER, class_code=CLASS
            )
            assert isinstance(filler.sources[0], BrokerMinutes), (
                f"первым в цепочке не брокер: {filler.sources}"
            )
            assert isinstance(filler.sources[1], ExchangeMinutes)
            await filler.run(EDGE)
            return await worker.minutes(TICKER), transport.urls

    minutes, urls = run(scenario)
    assert len(minutes) == 8 + 55 + AFTER, "дыра не закрыта брокером"
    assert urls == [], f"биржу спросили при живом брокере: {urls}"
    with CandleStore(path) as store:
        assert store.source_counts(TICKER)["broker_history"] == 55, (
            "минуты брокера записались не под своим источником"
        )


def test_the_exchange_closes_the_hole_when_the_broker_refuses(
    tmp_path: pathlib.Path,
) -> None:
    """Брокер отказал — минуты приходят с биржи, и дыра закрывается.

    Отказ взят **не** «метода нет» (404), а обрывом связи: развилка,
    привязанная к коду ответа, была бы догадкой о чужом сервере и ломалась
    бы молча в тот день, когда шлюз ответит другим кодом.
    """
    path = base_with_hole(tmp_path)
    history = FakeHistory(NoConnection("обрыв в проверке"))
    port = FakePort()
    minutes, report = run(
        lambda: backfilled(path, history, port, exchange=iss_gives(minute_row(HOLE, 55)))
    )

    assert len(minutes) == 8 + 55 + AFTER, (
        f"биржа не закрыла дыру после отказа брокера: минуток {len(minutes)}"
    )
    assert report.inserted == 55 and report.source == "iss"
    assert "Догружено 55 минут за 19.06 13:08–14:03" in port.texts(), (
        f"в журнале нет человеческой строки о догрузке:\n{port.texts()}"
    )
    assert port.refreshed, "график не перерисован — дыра осталась бы на экране"


def test_the_method_missing_at_the_broker_falls_back_the_same_way(
    tmp_path: pathlib.Path,
) -> None:
    """404 у брокера — такой же отказ, как всякий другой, и лечится так же.

    Ровно то, что владелец счёта увидел 05.09.2026: сырой HTML вместо свечей.
    Теперь на этом месте график.
    """
    path = base_with_hole(tmp_path)
    history = FakeHistory(NotFound("candles-chart: HTTP 404"))
    port = FakePort()
    minutes, _ = run(
        lambda: backfilled(path, history, port, exchange=iss_gives(minute_row(HOLE, 55)))
    )

    assert len(minutes) == 8 + 55 + AFTER
    assert "404" not in port.texts() and "html" not in port.texts().lower(), (
        f"код отказа или разметка дошли до владельца счёта:\n{port.texts()}"
    )


def test_the_owner_is_told_where_the_minutes_came_from_once(
    tmp_path: pathlib.Path,
) -> None:
    """Одна строка: минуты взяты с биржи, потому что у брокера не вышло.

    Говорится **один раз за сеанс**: на дёрганой связи подключений много,
    и повтор одной строки залил бы журнал, за которым перестанут следить.
    """
    path = base_with_hole(tmp_path)

    async def scenario() -> list[tuple[str, str, DecisionLevel]]:
        client, _ = exchange_client(iss_gives(minute_row(HOLE, 55)))
        async with MarketWorker(path, iss=client) as worker:
            port = FakePort()
            filler = Backfiller(
                FakeHistory(NoConnection("раз"), NoConnection("два")),
                worker,
                port,
                ticker=TICKER,
                class_code=CLASS,
            )
            await filler.run(EDGE)
            await filler.run(EDGE)
            return port.said

    said = run(scenario)
    origins = [reason for _, reason, _ in said if reason == EXCHANGE_WORDS.origin]
    assert len(origins) == 1, f"строка «откуда» сказана {len(origins)} раз(а)"
    assert "у брокера получить их не удалось" in origins[0], origins[0]
    assert "Московской биржи" in origins[0], origins[0]


def test_the_exchange_answer_is_cut_back_to_the_hole(tmp_path: pathlib.Path) -> None:
    """Биржа отдаёт целые сутки — записывается только дыра.

    Без резки в базу легли бы минуты далеко за пределами запроса, строка
    «Догружено N минут за X–Y» назвала бы не тот отрезок, а счётчики свечей
    в учёте дней разошлись бы с теми, что посчитала загрузка истории.
    """
    path = base_with_hole(tmp_path)
    whole_day = minute_row(OPEN - timedelta(hours=3), 12 * 60)
    assert whole_day[0].time < HOLE and whole_day[-1].time > EDGE, (
        "проверка вакуумна: ответ биржи и так не шире дыры"
    )
    port = FakePort()
    minutes, report = run(
        lambda: backfilled(
            path, FakeHistory(NoConnection("обрыв")), port, exchange=iss_gives(whole_day)
        )
    )

    written = [candle.time for candle in minutes]
    assert min(written) == OPEN and max(written) == EDGE - MINUTE, (
        f"записано за пределами дыры: {min(written)} … {max(written)}"
    )
    assert report.inserted == 55, f"записано {report.inserted} минут вместо 55"


def test_a_hole_filled_from_the_exchange_is_proven_empty_the_same_way(
    tmp_path: pathlib.Path,
) -> None:
    """Различение «мы потеряли» / «торгов не было» переживает смену источника.

    Оба механизма обязаны сработать те же: отметка дня в учёте (переживает
    перезапуск) и граница подтверждения (сеанс). И строка журнала обязана
    назвать того, **кого спрашивали**: ответ стоит ровно столько, сколько
    стоит доверие к источнику.
    """
    path = tmp_path / "proven-empty.sqlite3"
    before = datetime(2026, 6, 17, 23, 50, tzinfo=MSK)
    #: Биржа отдаст 10:00…10:04 и 10:20…10:24 — между ними остаётся пропуск
    #: **внутри одного дня**, ровно такой, о котором журнал обязан сказать.
    #: Без него строка «Пропуск остался пустым» не появляется вовсе (ночь
    #: и выходные в журнал не идут), и проверка была бы вакуумной.
    whole_day = datetime(2026, 6, 18, 10, 0, tzinfo=MSK)
    with CandleStore(path) as store:
        store.put_minutes(TICKER, [one_minute(before)], Source.ISS)
        store.put_minutes(
            TICKER, [one_minute(BACK + MINUTE * i) for i in range(AFTER)], Source.BROKER
        )
    port = FakePort()
    run(
        lambda: backfilled(
            path,
            FakeHistory(NoConnection("обрыв")),
            port,
            exchange=iss_gives(
                minute_row(whole_day, 5) + minute_row(whole_day + MINUTE * 20, 5)
            ),
        )
    )

    with CandleStore(path) as store:
        marked = store.settled_days(TICKER)
    assert marked == {date(2026, 6, 18)}, (
        f"после отката на биржу день не отмечен в учёте: {sorted(marked)}"
    )
    assert port.confirmed == [(TICKER, EDGE)], (
        f"после отката на биржу граница доверия не сдвинута: {port.confirmed}"
    )
    said = port.texts()
    assert "Биржа не прислала свечей" in said and "не потеря программы" in said, (
        f"пустота приписана не тому, кого спрашивали:\n{said}"
    )
    assert "Брокер не прислал" not in said, f"сказано про брокера, спросили биржу:\n{said}"


def test_a_minute_written_from_the_exchange_outranks_the_stream(
    tmp_path: pathlib.Path,
) -> None:
    """Догруженное с биржи ложится рангом биржи, а не рангом догрузки.

    Выбор осознанный (решение 0029): ранг описывает **происхождение**
    данных, а не функцию, которая их записала. Понизить его значило бы
    разрешить потоку брокера переписывать итоговые данные самой биржи —
    поток от них производен, а не наоборот.
    """
    path = base_with_hole(tmp_path)
    run(
        lambda: backfilled(
            path,
            FakeHistory(NoConnection("обрыв")),
            FakePort(),
            exchange=iss_gives(minute_row(HOLE, 55)),
        )
    )

    with CandleStore(path) as store:
        assert store.source_counts(TICKER) == {"iss": 8 + 55, "broker": AFTER}, (
            f"источники в базе перепутаны: {store.source_counts(TICKER)}"
        )
        over = store.put_minutes(TICKER, [one_minute(HOLE, close=1.0)], Source.BROKER)
        prices = {candle.time: candle.close for candle in store.minutes(TICKER)}

    assert over.kept == 1 and prices[HOLE] != 1.0, (
        "поток брокера переписал минуту, взятую у самой биржи"
    )


def test_a_backfill_that_touched_an_existing_row_says_so_aloud(
    tmp_path: pathlib.Path,
) -> None:
    """Перезапись существующей минуты — предупреждение, а не число в отчёте.

    У свечей брокера второй рубеж есть (ранг 0, хранилище отобьёт запись),
    у свечей биржи его нет по решению 0029. Значит, поломка отбора `pick_new`
    прошла бы при откате **молча**: строка переписана, отчёт зелёный.
    Единственный способ узнать — сказать вслух.
    """
    path = base_with_hole(tmp_path)

    async def scenario() -> list[str]:
        async with MarketWorker(path) as worker:
            port = FakePort()
            filler = Backfiller(
                FakeHistory(), worker, port, ticker=TICKER, class_code=CLASS
            )
            filler._count_leftovers(  # noqa: SLF001 - сторож на внутреннем шаге
                pick_new([], present=[], boundary=EDGE),
                WriteStats(inserted=0, updated=3),
                LoadReport(symbol=TICKER, source="iss"),
                EDGE,
            )
            return port.warnings()

    warnings = run(scenario)
    assert any("переписала 3 минут" in reason for reason in warnings), (
        f"перезапись готовых минут прошла молча: {warnings}"
    )


def test_an_unknown_class_code_leaves_the_broker_alone_and_says_why(
    tmp_path: pathlib.Path,
) -> None:
    """Класс инструмента не сопоставлен рынку ISS — биржи в цепочке нет.

    Угадать рынок по коду нельзя: ошибка вернула бы свечи **другого
    инструмента**, и заметить это было бы нечем. Поэтому источник
    не собирается вовсе, а причина едет в журнал вместе с отказом.
    """
    path = base_with_hole(tmp_path)
    port = FakePort()
    minutes, _ = run(
        lambda: backfilled(
            path,
            FakeHistory(NoConnection("обрыв"), expect_class=BONDS),
            port,
            exchange=iss_gives(minute_row(HOLE, 55)),
            class_code=BONDS,
        )
    )

    assert len(minutes) == 8 + AFTER, "свечи взяты с рынка, который никто не назвал"
    assert any("не сопоставлен ни одному рынку" in reason for reason in port.warnings()), (
        f"молчаливо выпавший источник неотличим от отказавшего:\n{port.texts()}"
    )


def test_a_missing_exchange_is_not_announced_while_the_broker_works(
    tmp_path: pathlib.Path,
) -> None:
    """Биржи в цепочке нет, но брокер ответил — про биржу молчим.

    Отката не было: спрашивали как обычно и получили ответ. Строка «свечи
    взяты у брокера» на каждом подключении — шум, за которым перестанут
    читать строки по делу. Причина отсутствия биржи придержана до случая,
    когда она понадобится: когда не ответит никто.
    """
    path = base_with_hole(tmp_path)
    port = FakePort()
    minutes, _ = run(
        lambda: backfilled(
            path,
            FakeHistory(
                [broker_bar(HOLE + MINUTE * i) for i in range(55)], expect_class=BONDS
            ),
            port,
            class_code=BONDS,
        )
    )

    assert len(minutes) == 8 + 55 + AFTER, "брокер не закрыл дыру"
    assert not any(event == "Откуда взяты свечи" for event, _, _ in port.said), (
        f"сказано про источник там, где отката не было:\n{port.texts()}"
    )
    assert not any("не сопоставлен" in reason for _, reason, _ in port.said), (
        f"причина отсутствия биржи выведена при работающем брокере:\n{port.texts()}"
    )


def test_both_sources_answer_the_same_contract() -> None:
    """Оба источника обязаны отвечать одной подписью — иначе цепочки нет.

    Всё, что стоит после выбора источника (отбор, запись, учёт, журнал),
    написано **один раз** и про смену источника не знает. Держится это
    на совпадении подписи; разошлась она — и один из путей молча
    перестанет вызываться.
    """
    broker = inspect.signature(BrokerMinutes.minutes)
    exchange = inspect.signature(ExchangeMinutes.minutes)

    assert broker == exchange, f"подписи разошлись: {broker} против {exchange}"
    assert inspect.iscoroutinefunction(BrokerMinutes.minutes)
    assert inspect.iscoroutinefunction(ExchangeMinutes.minutes)
    assert BrokerMinutes.source.rank < ExchangeMinutes.source.rank, (
        "ранги источников догрузки сравнялись — правило конфликта стало "
        "зависеть от порядка вставки"
    )
