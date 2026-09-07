"""Догрузка пропущенного: при следующем запуске программа сама добирает историю.

Пункт приёмки: «После перезапуска пропущенный отрезок догрузился сам».
Сети здесь нет — транспорт подставной, и это позволяет проверить именно
поведение догрузки, а не доступность сервера.
"""

from __future__ import annotations

import pathlib
import urllib.parse
from datetime import date, datetime, timedelta

import pytest

from market.candles import M5, MSK
from market.iss import FUTURES, IssClient, IssTransportError
from market.reports import LoadReport
from market.storage import CandleStore, Source
from market.sync import (
    EMPTY_CHUNK_TRUSTED_DAYS,
    contiguous_ranges,
    split_range,
    sync_minutes,
)

from market_helpers import iss_body, minute, msk


@pytest.fixture
def store(tmp_path: pathlib.Path):
    with CandleStore(tmp_path / "candles.sqlite3") as opened:
        yield opened


def session_of(day: date, *, minutes: int = 5, at_hour: int = 10) -> list:
    """Короткая «торговая сессия» из подряд идущих минуток."""
    since = datetime(day.year, day.month, day.day, at_hour, 0, tzinfo=MSK)
    return [minute(since + timedelta(minutes=i), open=100.0 + i) for i in range(minutes)]


class Server:
    """Подставной ISS: отдаёт заготовленные свечи по датам запроса."""

    def __init__(self, by_day: dict[date, list]) -> None:
        self.by_day = by_day
        self.requests: list[tuple[date, date]] = []

    def get(self, url: str, *, timeout: float) -> bytes:
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        since = date.fromisoformat(params["from"])
        until = date.fromisoformat(params["till"])
        offset = int(params["start"])
        if offset == 0:
            self.requests.append((since, until))
        candles = [
            candle
            for day, day_candles in sorted(self.by_day.items())
            if since <= day <= until
            for candle in day_candles
        ]
        return iss_body(candles[offset:])


def client(server: Server) -> IssClient:
    return IssClient(server, pause=0, sleep=lambda _s: None)


# -- нарезка -----------------------------------------------------------------


def test_contiguous_ranges_groups_days() -> None:
    days = [date(2026, 8, 1), date(2026, 8, 2), date(2026, 8, 5)]
    assert contiguous_ranges(days) == [
        (date(2026, 8, 1), date(2026, 8, 2)),
        (date(2026, 8, 5), date(2026, 8, 5)),
    ]


def test_split_range_limits_chunk_length() -> None:
    chunks = split_range(date(2026, 8, 1), date(2026, 8, 10), 4)
    assert chunks == [
        (date(2026, 8, 1), date(2026, 8, 4)),
        (date(2026, 8, 5), date(2026, 8, 8)),
        (date(2026, 8, 9), date(2026, 8, 10)),
    ]


# -- первая загрузка и повторный запуск --------------------------------------


def test_first_run_downloads_and_second_run_asks_only_the_tail(store: CandleStore) -> None:
    """Повторный запуск не качает заново закрытое — кроме последнего дня.

    Последний день, за который пришли данные, подтвердить нечем: обрыв выдачи
    **внутри** дня и честный конец истории по ответу сервера не различаются
    (см. `_settle_tails`). Поэтому он и только он перезапрашивается — один день
    вместо всего периода.
    """
    server = Server({date(2026, 8, 24): session_of(date(2026, 8, 24)),
                     date(2026, 8, 25): session_of(date(2026, 8, 25))})
    now_moment = msk(2026, 8, 26, 9, 0)

    first = sync_minutes(store, client(server), "MXU6", market=FUTURES,
                          since=date(2026, 8, 24), until=date(2026, 8, 25), now=now_moment)
    assert first.inserted == 10
    assert server.requests == [(date(2026, 8, 24), date(2026, 8, 25))]
    assert store.settled_days("MXU6") == {date(2026, 8, 24)}

    server.requests.clear()
    second = sync_minutes(store, client(server), "MXU6", market=FUTURES,
                          since=date(2026, 8, 24), until=date(2026, 8, 25), now=now_moment)
    assert server.requests == [(date(2026, 8, 25), date(2026, 8, 25))]
    assert second.inserted == 0, "уже лежащие минутки скачались как новые"
    assert store.coverage("MXU6").count == 10


def test_a_day_confirmed_by_the_next_day_is_not_asked_again(store: CandleStore) -> None:
    """Пришли данные следующего дня — предыдущий подтверждён и закрыт.

    ISS отдаёт свечи по возрастанию времени: до 25 августа обход добрался бы
    только через весь 24-й. Значит, обрыв внутри 24-го исключён, и переспрашивать
    его незачем.
    """
    server = Server({date(2026, 8, 24): session_of(date(2026, 8, 24)),
                     date(2026, 8, 25): session_of(date(2026, 8, 25))})
    now_moment = msk(2026, 8, 26, 9, 0)
    sync_minutes(store, client(server), "MXU6", market=FUTURES,
                 since=date(2026, 8, 24), until=date(2026, 8, 25), now=now_moment)

    server.requests.clear()
    sync_minutes(store, client(server), "MXU6", market=FUTURES,
                 since=date(2026, 8, 24), until=date(2026, 8, 24), now=now_moment)
    assert server.requests == [], "подтверждённый день переспрашивают заново"


def test_chunks_confirm_each_other_so_only_the_very_last_day_stays_open(
    store: CandleStore,
) -> None:
    """Год кусками: неподтверждённым остаётся один день на всю загрузку.

    Кусок кончается там, где кончились его данные, — и подтвердить его хвост
    может только следующий кусок. Если разбирать хвосты сразу, каждый кусок
    оставлял бы по неподтверждённому дню: у годовой догрузки их было бы
    тринадцать. Разбор отложен до конца обхода.
    """
    days_asked = [date(2026, 8, 1) + timedelta(days=i) for i in range(12)]
    server = Server({d: session_of(d) for d in days_asked})
    now_moment = msk(2026, 9, 1, 9, 0)

    summary_report = sync_minutes(store, client(server), "MXU6", market=FUTURES,
                         since=days_asked[0], until=days_asked[-1], now=now_moment, chunk_days=4)

    assert len(summary_report.ranges) == 3, "куски нарезались не так, как задумано в тесте"
    assert summary_report.incomplete == [days_asked[-1]]
    assert store.settled_days("MXU6") == set(days_asked[:-1])


def test_missing_tail_is_picked_up_on_next_start(store: CandleStore) -> None:
    """Пропущенный отрезок догружается сам, вручную ничего не нужно."""
    days = {d: session_of(d) for d in (date(2026, 8, 24), date(2026, 8, 25), date(2026, 8, 26))}
    server = Server(days)

    sync_minutes(store, client(server), "MXU6", market=FUTURES,
                 since=date(2026, 8, 24), until=date(2026, 8, 24), now=msk(2026, 8, 25, 9, 0))
    assert store.coverage("MXU6").count == 5

    report = sync_minutes(store, client(server), "MXU6", market=FUTURES,
                         since=date(2026, 8, 24), until=date(2026, 8, 26), now=msk(2026, 8, 27, 9, 0))

    # 24-е перезапрашивается вместе с хвостом: подтвердить его прошлый раз
    # было нечем, данных следующего дня в базе не было.
    assert server.requests[-1] == (date(2026, 8, 24), date(2026, 8, 26))
    assert report.inserted == 10 and report.updated == 5
    assert store.coverage("MXU6").count == 15
    assert store.settled_days("MXU6") == {date(2026, 8, 24), date(2026, 8, 25)}


def test_hole_in_the_middle_is_filled(store: CandleStore) -> None:
    """Дыра между двумя загруженными кусками не остаётся навсегда.

    «Качать после последней свечи» её бы не увидело никогда: последняя свеча
    есть, а середины нет.
    """
    days = {d: session_of(d) for d in (date(2026, 8, 10), date(2026, 8, 11), date(2026, 8, 12))}
    server = Server(days)
    now_moment = msk(2026, 8, 20, 9, 0)

    sync_minutes(store, client(server), "MXU6", market=FUTURES,
                 since=date(2026, 8, 10), until=date(2026, 8, 10), now=now_moment)
    sync_minutes(store, client(server), "MXU6", market=FUTURES,
                 since=date(2026, 8, 12), until=date(2026, 8, 12), now=now_moment)
    assert store.coverage("MXU6").count == 10
    # Ни один из двух дней не подтверждён: каждый был хвостом своей загрузки,
    # а данных **после** себя в момент загрузки не имел.
    assert store.settled_days("MXU6") == set()

    sync_minutes(store, client(server), "MXU6", market=FUTURES,
                 since=date(2026, 8, 10), until=date(2026, 8, 12), now=now_moment)

    assert server.requests[-1] == (date(2026, 8, 10), date(2026, 8, 12))
    assert store.trading_days("MXU6") == [date(2026, 8, 10), date(2026, 8, 11), date(2026, 8, 12)]
    # Середина закрыта, и теперь подтверждены оба прошлых дня.
    assert store.settled_days("MXU6") == {date(2026, 8, 10), date(2026, 8, 11)}


def test_day_interrupted_mid_session_is_completed_next_time(store: CandleStore) -> None:
    """Программу закрыли посреди сессии — остаток дня догрузится завтра."""
    day = date(2026, 8, 26)
    server = Server({day: session_of(day, minutes=3)})
    sync_minutes(store, client(server), "MXU6", market=FUTURES,
                 since=day, until=day, now=msk(2026, 8, 26, 10, 3))
    assert store.coverage("MXU6").count == 3

    server.by_day[day] = session_of(day, minutes=9)
    report = sync_minutes(store, client(server), "MXU6", market=FUTURES,
                         since=day, until=day, now=msk(2026, 8, 27, 9, 0))

    assert report.inserted == 6
    assert report.updated == 3
    assert store.coverage("MXU6").count == 9


def test_weekend_without_candles_is_not_asked_twice(store: CandleStore) -> None:
    """У выходного свечей нет и не будет — переспрашивать про него незачем."""
    server = Server({})
    now_moment = msk(2026, 8, 26, 9, 0)
    sync_minutes(store, client(server), "MXU6", market=FUTURES,
                 since=date(2026, 8, 22), until=date(2026, 8, 23), now=now_moment)
    report = sync_minutes(store, client(server), "MXU6", market=FUTURES,
                         since=date(2026, 8, 22), until=date(2026, 8, 23), now=now_moment)

    assert len(server.requests) == 1
    assert report.fetched == 0
    assert store.coverage("MXU6").empty


# -- отчёт -------------------------------------------------------------------


def test_report_lands_in_the_journal_with_numbers(store: CandleStore) -> None:
    day = date(2026, 8, 26)
    server = Server({day: session_of(day, minutes=5)})
    report = sync_minutes(store, client(server), "MXU6", market=FUTURES,
                         since=day, until=day, now=msk(2026, 8, 27, 9, 0))

    assert report.fetched == 5 and report.inserted == 5 and report.missing_minutes == 0
    (entry,) = store.load_log("MXU6")
    assert entry["symbol"] == "MXU6"
    assert entry["fetched"] == 5
    assert entry["requested_from"] == "2026-08-26"
    assert "MXU6" in str(entry["summary"])


def test_report_counts_missing_minutes_and_long_gaps(store: CandleStore) -> None:
    """Общее число отсутствующих минуток — в отчёте целиком, ничего не теряется.

    Отдельными записями в журнал идут только разрывы от порога, иначе журнал
    забивается штатными минутами без сделок.
    """
    day = date(2026, 8, 26)
    morning = session_of(day, minutes=3, at_hour=10)
    evening = session_of(day, minutes=3, at_hour=19)
    server = Server({day: morning + evening})

    report = sync_minutes(store, client(server), "MXU6", market=FUTURES,
                         since=day, until=day, now=msk(2026, 8, 27, 9, 0),
                         gap_threshold_minutes=30)

    assert report.missing_minutes == 537
    assert len(report.gaps) == 1
    assert report.gaps[0].minutes == 537
    assert "минут без свечи: 537" in report.summary()
    assert len(store.gap_log("MXU6")) == 1


def runs_of_minutes(day: date, runs: list[tuple[tuple[int, int], int]]) -> list:
    """Подряд идущие минутки кусками: `((час, минута), сколько)`."""
    candles = []
    for (at_hour, at_minute), how_many in runs:
        first_moment = datetime(day.year, day.month, day.day, at_hour, at_minute, tzinfo=MSK)
        candles += [minute(first_moment + timedelta(minutes=i), open=100.0 + i) for i in range(how_many)]
    return candles


def session_with_breaks(day: date) -> list:
    """День с тремя пропусками: 28 минут, 5 минут (клиринг) и 15 (вечерний).

    28 минут — это пять пятиминутных баров из одиннадцати в торговом окне
    10:05–11:00. Пять и пятнадцать — длина штатных перерывов биржи.

    10:00–10:11, **дыра 10:12–10:39**, 10:40–10:59, перерыв 11:00–11:04,
    11:05–11:09, перерыв 11:10–11:24, 11:25–11:29.
    """
    return runs_of_minutes(day, [((10, 0), 12), ((10, 40), 20), ((11, 5), 5), ((11, 25), 5)])


def test_a_hole_inside_the_trading_window_reaches_the_journal(store: CandleStore) -> None:
    """Пропуск 10:12–10:39 попадает в журнал — вместе со всем, что стоит бара.

    Порог стоял на 30 минутах и прятал ровно этот случай: дыра в полокна
    (пять пятиминутных баров из одиннадцати) не давала в журнале ни строки.
    Потом он стоял на 16 — «первое значение выше вечернего клиринга»; это
    свойство держалось только на непрерывных синтетических рядах, а на живых
    данных (26 % пустых минутных слотов) пятнадцатиминутный клиринг регулярно
    выглядит шестнадцатиминутным.

    Теперь порог мерится **длиной бара рабочего таймфрейма**: пять минут —
    один потерянный бар, десять — два. Штатные перерывы биржи при этом видны
    тоже, и это принято сознательно: отличить их от дыры без торгового
    календаря нельзя, а календаря у слоя данных нет.
    """
    day = date(2026, 8, 26)
    server = Server({day: session_with_breaks(day)})
    report = sync_minutes(store, client(server), "MXU6", market=FUTURES,
                          since=day, until=day, now=msk(2026, 8, 27, 9, 0))

    assert report.missing_minutes == 48, "общее число пропущенных минут не сошлось"
    logged_gaps = [(g.start.strftime("%H:%M"), g.end.strftime("%H:%M"), g.minutes)
                  for g in report.gaps]
    assert logged_gaps == [
        ("10:12", "10:39", 28),
        ("11:00", "11:04", 5),
        ("11:10", "11:24", 15),
    ]
    assert len(store.gap_log("MXU6")) == 3


def test_a_ten_minute_hole_is_two_lost_decisions_and_it_shows(store: CandleStore) -> None:
    """Пропуск в 10 минут — два пятиминутных бара, которых у движка нет.

    Прежний порог 16 минут прятал его целиком: «короче вечернего клиринга,
    значит не интересно». Интересна не длина перерыва биржи, а число решений,
    которых не будет.
    """
    day = date(2026, 8, 26)
    server = Server({day: runs_of_minutes(day, [((10, 0), 10), ((10, 20), 10)])})
    report = sync_minutes(store, client(server), "MXU6", market=FUTURES,
                          since=day, until=day, now=msk(2026, 8, 27, 9, 0))

    assert [(g.start.strftime("%H:%M"), g.minutes) for g in report.gaps] == [("10:10", 10)]

    at_the_old_threshold = sync_minutes(store, client(Server({day: runs_of_minutes(day, [((10, 0), 10), ((10, 20), 10)])})),
                           "MXU6", market=FUTURES, since=day, until=day,
                           now=msk(2026, 8, 27, 9, 0), gap_threshold_minutes=16,
                           write_report=False)
    assert at_the_old_threshold.gaps == [], "при пороге 16 та же дыра не давала ни строки"


def test_the_old_threshold_would_have_hidden_that_hole(store: CandleStore) -> None:
    """Тот же ряд при прежнем пороге 30 минут — в журнале ни строки.

    Тест держит найденное: если порог когда-нибудь вернут наверх, здесь
    станет видно, что именно этим прячется.
    """
    day = date(2026, 8, 26)
    server = Server({day: session_with_breaks(day)})
    report = sync_minutes(store, client(server), "MXU6", market=FUTURES,
                          since=day, until=day, now=msk(2026, 8, 27, 9, 0),
                          gap_threshold_minutes=30)

    assert report.missing_minutes == 48
    assert report.gaps == []


def test_minutes_marked_with_seconds_are_one_minute_all_the_way_through(
    store: CandleStore,
) -> None:
    """Одна минута с разными секундами — одна строка и один счётчик.

    Ключ минутки — минута, и это правило всего слоя: `IssClient` считает такой
    повтор дублем страницы ещё до записи, поэтому до `collapsed` дело
    не доходит. `collapsed` остаётся для источника, который в клиент ISS
    не заходит вовсе, — потока брокера (Э1-5).
    """
    day = date(2026, 8, 26)
    first_moment = datetime(day.year, day.month, day.day, 10, 0, tzinfo=MSK)
    server = Server({day: [minute(first_moment.replace(second=s), open=100.0 + s) for s in (0, 20, 40)]})

    report = sync_minutes(store, client(server), "MXU6", market=FUTURES,
                          since=day, until=day, now=msk(2026, 8, 27, 9, 0))

    assert store.coverage("MXU6").count == 1
    assert report.duplicates == 2
    assert report.collapsed == 0


def test_the_collapsed_counter_reaches_the_journal_as_a_column() -> None:
    """`collapsed` — колонка журнала, а не слово внутри русской фразы.

    Счётчик целостности, живущий только в тексте записи, нельзя ни
    отсортировать, ни просуммировать по журналу.
    """
    report = LoadReport(symbol="MXU6", source="broker", collapsed=3)
    assert "схлопнулось" in report.summary()
    assert "осталась последняя" in report.summary()


def test_routine_breaks_are_visible_too_and_that_is_deliberate(store: CandleStore) -> None:
    """Штатные перерывы биржи теперь в журнале видны — и это решение, а не сбой.

    Прежний порог 16 минут обещал «штатные перерывы не попадают». Обещание
    держалось только на непрерывных синтетических рядах: на MXU6 пусты 26 %
    минутных слотов, и вечерний клиринг регулярно выглядит длиннее пятнадцати
    минут. Обещание, которое не выполняется на живых данных, хуже отсутствующего.

    Отличить перерыв торгов от потери данных без календаря нельзя, а календаря
    у слоя нет (`market.gaps`: слой сообщает факты, а не выводы).
    """
    day = date(2026, 8, 26)
    # 10:00–10:19, перерыв 10:20–10:24 (клиринг, 5 минут),
    # 10:25–10:44, перерыв 10:45–10:59 (вечерний, 15 минут), 11:00–11:14.
    candles = runs_of_minutes(day, [((10, 0), 20), ((10, 25), 20), ((11, 0), 15)])
    server = Server({day: candles})

    report = sync_minutes(store, client(server), "MXU6", market=FUTURES,
                          since=day, until=day, now=msk(2026, 8, 27, 9, 0))

    assert report.missing_minutes == 20, "перерывы 5 и 15 минут"
    assert [g.minutes for g in report.gaps] == [5, 15]
    assert all(not g.crosses_date for g in report.gaps)


def test_night_and_weekend_do_not_land_in_the_gap_journal(store: CandleStore) -> None:
    """Ночь и выходные считаются отдельным числом, а не записями в журнале.

    `find_gaps` зовётся на **всём** запрошенном периоде, а не на догруженном
    куске: год MXU6 дал бы под три сотни разрывов «через сутки». Строка
    «разрывов от порога: 350» не сообщает ничего, а дыра внутри торгового окна
    в такой куче не находится.
    """
    days_asked = [date(2026, 8, 24), date(2026, 8, 25), date(2026, 8, 26)]
    server = Server({d: session_of(d) for d in days_asked})
    report = sync_minutes(store, client(server), "MXU6", market=FUTURES,
                          since=days_asked[0], until=days_asked[-1], now=msk(2026, 8, 27, 9, 0))

    assert report.gaps == [], "ночной перерыв уехал в журнал разрывов"
    assert report.overnight_gaps == 2, "переходы через сутки не посчитаны"
    assert store.gap_log("MXU6") == []
    assert "переходов через сутки: 2" in report.summary()
    assert str(store.load_log("MXU6")[0]["overnight_gaps"]) == "2"


def test_broker_candles_are_not_overwritten_by_older_history(store: CandleStore) -> None:
    """История с биржи ложится поверх свечей брокера, а не рядом.

    Пункт приёмки «стыка между историей и реальными свечами не видно»
    проверяется здесь на живом пути: сначала пишет поток, потом догрузка.
    """
    day = date(2026, 8, 26)
    store.put_minutes("MXU6", session_of(day, minutes=5), Source.BROKER)
    server = Server({day: [c.replace(open=999.0) for c in session_of(day, minutes=5)]})

    report = sync_minutes(store, client(server), "MXU6", market=FUTURES,
                         since=day, until=day, now=msk(2026, 8, 27, 9, 0))

    assert report.updated == 5 and report.kept == 0
    assert store.source_counts("MXU6") == {"iss": 5}
    assert all(c.open == 999.0 for c in store.minutes("MXU6"))
    assert store.missing_minutes("MXU6") == 0


def test_backwards_period_is_refused(store: CandleStore) -> None:
    with pytest.raises(ValueError, match="задом наперёд"):
        sync_minutes(store, client(Server({})), "MXU6", market=FUTURES,
                     since=date(2026, 8, 27), until=date(2026, 8, 26), now=msk(2026, 8, 27, 9, 0))


# -- обрыв посреди загрузки --------------------------------------------------


class FailingServer(Server):
    """Сервер, который перестаёт отвечать начиная с указанной даты."""

    def __init__(self, by_day: dict[date, list], fail_with: date) -> None:
        super().__init__(by_day)
        self.fail_with = fail_with

    def get(self, url: str, *, timeout: float) -> bytes:
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        if date.fromisoformat(params["from"]) >= self.fail_with:
            raise IssTransportError("сеть пропала")
        return super().get(url, timeout=timeout)


def test_interrupted_load_is_journalled_and_resumes_from_the_break(store: CandleStore) -> None:
    """Оборванная загрузка попадает в журнал, а скачанное не пропадает.

    Без записи в журнал в базе просто оказалось бы меньше свечей, чем нужно,
    и ни одной строки о том, почему. Повторный запуск обязан продолжить
    с места обрыва, а не с начала.
    """
    days = {d: session_of(d) for d in (date(2026, 8, 10), date(2026, 8, 11), date(2026, 8, 12))}
    server = FailingServer(days, fail_with=date(2026, 8, 12))
    now_moment = msk(2026, 8, 20, 9, 0)

    with pytest.raises(IssTransportError):
        sync_minutes(store, client(server), "MXU6", market=FUTURES,
                     since=date(2026, 8, 10), until=date(2026, 8, 12),
                     now=now_moment, chunk_days=1)

    (entry,) = store.load_log("MXU6")
    assert "оборвалась на куске 3 из 3" in str(entry["summary"])
    assert store.coverage("MXU6").count == 10  # два дня успели записаться

    whole_period = Server(days)
    report = sync_minutes(store, client(whole_period), "MXU6", market=FUTURES,
                         since=date(2026, 8, 10), until=date(2026, 8, 12),
                         now=now_moment, chunk_days=1)
    # 11-е перезапрашивается тоже: оно было хвостом прошлой (оборванной)
    # загрузки, и подтвердить его тогда было нечем.
    assert whole_period.requests == [(date(2026, 8, 11), date(2026, 8, 11)),
                              (date(2026, 8, 12), date(2026, 8, 12))]
    assert report.inserted == 5
    assert store.coverage("MXU6").count == 15


def test_cut_off_answer_does_not_mark_missing_days_as_loaded(store) -> None:
    """Сервер оборвал выдачу — недошедшие дни не считаются загруженными.

    Обход страниц заканчивается на первой пустой, и пустая страница от сбоя
    сервера неотличима от честного конца данных. Прежде весь кусок помечался
    загруженным целиком: недокачанные дни навсегда выпадали из `days_to_request`,
    и дыра в истории оставалась невидимой — прогон зелёный, а параметры
    подбираются на неполных данных.
    """
    server = Server({
        date(2026, 6, 1): session_of(date(2026, 6, 1)),
        date(2026, 6, 2): session_of(date(2026, 6, 2)),
        # 3–5 июня сервер не отдал: обрыв внутри выдачи
    })
    now_moment = msk(2026, 6, 20, 12, 0)

    report = sync_minutes(
        store, client(server), "MXU6", market=FUTURES,
        since=date(2026, 6, 1), until=date(2026, 6, 5), source=Source.ISS, now=now_moment,
    )

    assert report.incomplete == [
        date(2026, 6, 2), date(2026, 6, 3), date(2026, 6, 4), date(2026, 6, 5),
    ]
    assert report.note and "не полностью" in report.note

    # Главное: следующий запуск обязан переспросить недошедшие дни.
    server.requests.clear()
    sync_minutes(
        store, client(server), "MXU6", market=FUTURES,
        since=date(2026, 6, 1), until=date(2026, 6, 5), source=Source.ISS, now=now_moment,
    )
    assert server.requests, "недокачанные дни больше не переспрашиваются — дыра навсегда"


def test_the_day_where_the_answer_broke_off_is_asked_again(store) -> None:
    """День, на котором выдача оборвалась, не считается загруженным.

    Обход страниц кончается на первой пустой — то есть обрыв случается
    **внутри** дня, а не между днями. Если объявить этот день загруженным,
    `CandleStore.bars` поверит учёту и выдаст последний бар дня закрытым,
    хотя это огрызок: ровно та свеча, из-за которой сверка расходится
    на одной строке.
    """
    server = Server({
        date(2026, 6, 1): session_of(date(2026, 6, 1)),
        # выдача оборвалась посреди бара 10:05: пришли только 10:05 и 10:06
        date(2026, 6, 2): session_of(date(2026, 6, 2), minutes=7),
        # 3–5 июня сервер не отдал вовсе
    })
    now_moment = msk(2026, 6, 20, 12, 0)

    summary_report = sync_minutes(store, client(server), "MXU6", market=FUTURES,
                         since=date(2026, 6, 1), until=date(2026, 6, 5), now=now_moment)

    assert "2026-06-02" in summary_report.note and "оборвалась" in summary_report.note
    assert store.settled_days("MXU6") == {date(2026, 6, 1)}
    assert date(2026, 6, 2) in store.days_to_request("MXU6", date(2026, 6, 1), date(2026, 6, 5))

    # И огрызок бара на краю обрыва не объявлен закрытым.
    last_bar = store.bars("MXU6", M5)[-1]
    assert (last_bar.time.date(), last_bar.filled_minutes) == (date(2026, 6, 2), 2)
    assert last_bar.unsettled

    # ⚠️ Граница осторожности: обрыв ровно на границе бара так не ловится.
    # Последний бар при этом полон — не хватает следующих, а они просто
    # не существуют. Дыру закрывает перезапрос дня, а не признак свечи.


def test_a_break_at_the_end_of_the_chunk_is_not_confirmed_either(store) -> None:
    """Обрыв в **последнем** дне куска больше не выдаётся за загруженный день.

    Тот самый сценарий: догрузка 01.06 — 26.08 кусками по 30 дней, последний
    кусок кончается 26.08, сервер отдаёт данные до 26.08 10:06 и на следующей
    странице возвращает пустоту. Дней после 26.08 в куске нет, `cut` пуст —
    и прежний учёт помечал 26.08 закрытым.

    Дальше `bars()` брал `known_until = 27.08 00:00`, и последний бар дня,
    собранный из двух минуток вместо пяти, получал `unsettled=False`. Он входил
    в среднюю полноправной свечой, прогрев сдвигался, сверка расходилась
    на одной строке — и искать это пошли бы в стратегии.
    """
    server = Server({
        date(2026, 6, 1): session_of(date(2026, 6, 1)),
        # 2 июня: выдача оборвалась посреди бара 10:05 — пришли 10:05 и 10:06
        date(2026, 6, 2): session_of(date(2026, 6, 2), minutes=7),
    })
    now_moment = msk(2026, 6, 20, 12, 0)

    summary_report = sync_minutes(store, client(server), "MXU6", market=FUTURES,
                         since=date(2026, 6, 1), until=date(2026, 6, 2), now=now_moment)

    assert store.settled_days("MXU6") == {date(2026, 6, 1)}
    assert summary_report.incomplete == [date(2026, 6, 2)]
    assert "подтвердить его нечем" in summary_report.note

    stub_bar = store.bars("MXU6", M5)[-1]
    assert (stub_bar.time.date(), stub_bar.filled_minutes) == (date(2026, 6, 2), 2)
    assert stub_bar.unsettled, "бар из двух минуток объявлен закрытым"


def test_the_last_day_becomes_settled_once_the_next_day_arrives(store) -> None:
    """Осторожность не превращается в вечный перезапрос.

    Как только приходят данные следующего дня, предыдущий подтверждается
    и больше не запрашивается. В боевом ходу неподтверждённым остаётся
    сегодняшний день — он и так перезапрашивается всегда.
    """
    server = Server({
        date(2026, 6, 1): session_of(date(2026, 6, 1)),
        date(2026, 6, 2): session_of(date(2026, 6, 2)),
    })
    now_moment = msk(2026, 6, 20, 12, 0)

    sync_minutes(store, client(server), "MXU6", market=FUTURES,
                 since=date(2026, 6, 1), until=date(2026, 6, 1), now=now_moment)
    assert store.settled_days("MXU6") == set(), "подтверждать 1 июня было нечем"

    summary_report = sync_minutes(store, client(server), "MXU6", market=FUTURES,
                         since=date(2026, 6, 1), until=date(2026, 6, 2), now=now_moment)
    assert store.settled_days("MXU6") == {date(2026, 6, 1)}
    assert summary_report.incomplete == [date(2026, 6, 2)]


def test_today_at_the_end_of_the_range_needs_no_extra_care(store) -> None:
    """Хвост в сегодняшнем дне разбирает `settled`, а не осторожность.

    Боевой ход: период кончается сегодняшним днём. Он не отмечается закрытым
    и так, поэтому в `incomplete` попадать ему незачем — иначе журнал каждый
    запуск сообщал бы о «недошедшем дне», которого никто не терял.
    """
    server = Server({
        date(2026, 6, 1): session_of(date(2026, 6, 1)),
        date(2026, 6, 2): session_of(date(2026, 6, 2)),
    })
    summary_report = sync_minutes(store, client(server), "MXU6", market=FUTURES,
                         since=date(2026, 6, 1), until=date(2026, 6, 2),
                         now=msk(2026, 6, 2, 12, 0))

    assert summary_report.incomplete == []
    assert store.settled_days("MXU6") == {date(2026, 6, 1)}
    assert store.days_to_request("MXU6", date(2026, 6, 1), date(2026, 6, 2)) == [date(2026, 6, 2)]


# -- кусок, на который сервер не ответил ничем -------------------------------


def test_a_long_empty_chunk_is_not_declared_loaded(store) -> None:
    """Тридцать дней без единой свечи — это не «выходные», а «ответа не было».

    ISS отвечает `200` с пустым `data` при обслуживании, при смене режима
    торгов и при запросе инструмента, которого на доске нет. Политика повторов
    это не ловит: повторяются сбои, а пустая страница — законный конец обхода.

    Прежде все дни куска уходили в `data_day` с `settled = 1`, `incomplete`
    оставался пуст, `note` пуст, `missing_minutes` ноль — и следующий запуск
    говорил «всё уже загружено, запросов не потребовалось». Восстановление
    оставалось только через удаление базы.
    """
    server = Server({})
    now_moment = msk(2026, 7, 1, 12, 0)
    since, until = date(2026, 6, 1), date(2026, 6, 30)

    summary_report = sync_minutes(store, client(server), "MXU6", market=FUTURES,
                         since=since, until=until, now=now_moment)

    assert len(summary_report.incomplete) == 30
    assert summary_report.note and "не дал ни одной свечи" in summary_report.note
    assert store.settled_days("MXU6") == set()

    server.requests.clear()
    sync_minutes(store, client(server), "MXU6", market=FUTURES,
                 since=since, until=until, now=now_moment)
    assert server.requests, "пустой месяц больше не переспрашивают — дыра навсегда"


def test_a_short_empty_chunk_is_still_trusted(store) -> None:
    """Кусок в пределах самых длинных каникул пустым быть вправе.

    Новогодние каникулы — около десяти дней. Всё, что короче, пустым быть
    может законно, и переспрашивать про каждый такой день при каждом запуске
    значит слать сотни бесполезных запросов в год.
    """
    server = Server({})
    now_moment = msk(2026, 1, 20, 12, 0)
    since = date(2026, 1, 1)
    until = since + timedelta(days=EMPTY_CHUNK_TRUSTED_DAYS - 1)

    summary_report = sync_minutes(store, client(server), "MXU6", market=FUTURES,
                         since=since, until=until, now=now_moment)

    assert summary_report.incomplete == []
    assert len(store.settled_days("MXU6")) == EMPTY_CHUNK_TRUSTED_DAYS

    server.requests.clear()
    sync_minutes(store, client(server), "MXU6", market=FUTURES,
                 since=since, until=until, now=now_moment)
    assert server.requests == [], "закрытые каникулы переспрашивают заново"


def test_the_border_between_trusted_and_suspicious_is_one_day(store) -> None:
    """Граница ровно там, где написана: на дне длиннее порога доверие кончается."""
    now_moment = msk(2026, 1, 20, 12, 0)
    since = date(2026, 1, 1)
    beyond_the_trusted_horizon = since + timedelta(days=EMPTY_CHUNK_TRUSTED_DAYS)

    summary_report = sync_minutes(store, client(Server({})), "MXU6", market=FUTURES,
                         since=since, until=beyond_the_trusted_horizon, now=now_moment)
    assert len(summary_report.incomplete) == EMPTY_CHUNK_TRUSTED_DAYS + 1
