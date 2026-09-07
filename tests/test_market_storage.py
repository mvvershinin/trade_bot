"""Хранилище: дубли, правило источника правды, переживание перезапуска,
разрывы, журнал загрузок.

Два пункта приёмки держатся на этих проверках:
«История загрузилась за период без разрывов и дублей» и «Реальные свечи
дописались к историческим, стыка не видно».
"""

from __future__ import annotations

import pathlib
import sqlite3
from datetime import date, datetime, timedelta

import pytest

from market.candles import M5, Candle
from market.reports import LoadReport
from market.storage import SCHEMA_VERSION, CandleStore, Source

from market_helpers import minute, minutes_from, msk


@pytest.fixture
def store(tmp_path: pathlib.Path):
    with CandleStore(tmp_path / "userdata" / "candles.sqlite3") as opened:
        yield opened


# -- файл и схема ------------------------------------------------------------


def test_database_is_created_on_first_open(tmp_path: pathlib.Path) -> None:
    """Установщика у продукта нет — база обязана появляться на пустом месте."""
    path = tmp_path / "userdata" / "candles.sqlite3"
    assert not path.exists()
    with CandleStore(path):
        pass
    assert path.is_file()


def test_schema_version_is_stamped(store: CandleStore) -> None:
    (version,) = store._db.execute("PRAGMA user_version").fetchone()
    assert version == SCHEMA_VERSION


def test_newer_schema_is_refused(tmp_path: pathlib.Path) -> None:
    """База от более новой версии не открывается.

    Иначе старый код перезапишет то, чего не понимает, — и обнаружится это
    на журнале сделок, а не на свечах.
    """
    path = tmp_path / "candles.sqlite3"
    with CandleStore(path):
        pass
    connection = sqlite3.connect(path)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    connection.close()

    with pytest.raises(RuntimeError, match="более новой версией"):
        CandleStore(path)


# -- миграция схемы 1 → 2 ----------------------------------------------------


def first_schema_database(path: pathlib.Path, rows: list[tuple]) -> None:
    """База, какой её оставляла схема 1: `ts` — момент источника, не минута."""
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE minute_candle (
            symbol TEXT NOT NULL, ts INTEGER NOT NULL,
            open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
            close REAL NOT NULL, volume REAL NOT NULL,
            source TEXT NOT NULL, source_rank INTEGER NOT NULL,
            PRIMARY KEY (symbol, ts)
        ) WITHOUT ROWID
        """
    )
    connection.executemany(
        "INSERT INTO minute_candle VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
    )
    connection.execute("PRAGMA user_version = 1")
    connection.commit()
    connection.close()


def ts(moment: datetime) -> int:
    return int(moment.timestamp())


def test_a_minute_stored_with_seconds_is_repaired_on_open(tmp_path: pathlib.Path) -> None:
    """База прежней сборки чинится при открытии, а не роняет чтение баров.

    Смысл ключа `ts` изменился: был момент источника, стал минута. База,
    в которой минутка лежит как `10:05:07` рядом с `10:05:00`, даёт при чтении
    две записи на одну минуту, и `bars()` бросает `ValueError` на весь запрос:
    график пуст, источник свечей падает. Практический риск невелик — но
    невелик он ровно до первой такой базы на диске.
    """
    path = tmp_path / "old-schema.sqlite3"
    the_minute = msk(2026, 8, 26, 10, 5)
    first_schema_database(
        path,
        [
            ("MXU6", ts(the_minute), 100, 100, 100, 100, 5, "iss", 2),
            ("MXU6", ts(the_minute) + 7, 101, 101, 101, 101, 9, "broker", 1),
        ],
    )

    with CandleStore(path) as store:
        assert store.migrated_minutes == 1
        (candle,) = store.minutes("MXU6")
        assert candle.time == the_minute
        # Правило выбора то же, что при записи: биржа выше брокера.
        assert (candle.open, candle.volume) == (100, 5)
        (bar,) = store.bars("MXU6", M5)
        assert bar.volume == 5, "объём одной минуты сложился вдвое"
        (version,) = store._db.execute("PRAGMA user_version").fetchone()
        assert version == SCHEMA_VERSION


def test_migration_keeps_the_later_snapshot_when_ranks_are_equal(
    tmp_path: pathlib.Path,
) -> None:
    """При равном ранге побеждает более поздняя исходная отметка.

    Правило записано, а не отдано на волю порядка строк в таблице.
    """
    path = tmp_path / "old-schema.sqlite3"
    the_minute = msk(2026, 8, 26, 10, 5)
    first_schema_database(
        path,
        [
            ("MXU6", ts(the_minute) + 40, 140, 140, 140, 140, 4, "broker", 1),
            ("MXU6", ts(the_minute) + 10, 110, 110, 110, 110, 1, "broker", 1),
        ],
    )

    with CandleStore(path) as store:
        assert store.migrated_minutes == 2
        (candle,) = store.minutes("MXU6")
        assert (candle.open, candle.volume) == (140, 4)


def test_a_clean_database_needs_no_migration(store: CandleStore) -> None:
    assert store.migrated_minutes == 0
    assert store.dropped_minutes == 0


# -- миграция схемы 4 → 5 ----------------------------------------------------


def fourth_schema_database(path: pathlib.Path, rows: list[tuple]) -> None:
    """База схемы 4: та же таблица свечей, но в `volume` у догрузки рубли.

    Схема ставится **рабочим кодом**, а версия откатывается: писать `CREATE
    TABLE` руками значило бы проверять миграцию на выдуманной таблице, а не
    на той, что лежит у владельца счёта.
    """
    with CandleStore(path) as ready:
        ready._db.executemany(
            "INSERT OR REPLACE INTO minute_candle VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        ready._db.execute("PRAGMA user_version = 4")


def test_the_minutes_with_money_in_the_volume_are_dropped_on_open(
    tmp_path: pathlib.Path,
) -> None:
    """Минуты догрузки, записанные до пересчёта, снимаются при открытии базы.

    В них лежит оборот в рублях под именем контрактов (`D-013`, замер
    05.09.2026: 2,199 × 10⁵ на 3858 минутах). Пересчитать их миграция не может
    — множитель «рублей за пункт» живёт на бирже, — а оставить не вправе:
    первый, кто сложит колонку `volume`, получит чушь. Ранг ноль делает
    такую строку временной по построению, и догрузка кладёт её заново.
    """
    path = tmp_path / "schema-four.sqlite3"
    first = msk(2026, 9, 4, 20, 44)
    fourth_schema_database(
        path,
        [
            ("MXU6", ts(first), 226175, 226350, 226175, 226325, 64_264_575,
             "broker_history", 0),
            ("MXU6", ts(first) + 60, 226250, 226325, 226150, 226200, 43_442_500,
             "broker_history", 0),
            ("MXU6", ts(first) + 120, 226200, 226225, 226150, 226225, 72,
             "broker", 1),
            ("MXU6", ts(first) + 180, 226150, 226200, 226125, 226150, 24,
             "iss", 2),
        ],
    )

    with CandleStore(path) as store:
        assert store.dropped_minutes == 2, (
            f"снято не столько строк догрузки: {store.dropped_minutes}"
        )
        assert store.source_counts("MXU6") == {"broker": 1, "iss": 1}, (
            "миграция задела не только догрузку: "
            f"{store.source_counts('MXU6')}"
        )
        volumes = sorted(candle.volume for candle in store.minutes("MXU6"))
        assert volumes == [24.0, 72.0], (
            f"в базе остались рубли под именем контрактов: {volumes}"
        )
        (version,) = store._db.execute("PRAGMA user_version").fetchone()
        assert version == SCHEMA_VERSION


def test_the_drop_of_provisional_minutes_happens_once(tmp_path: pathlib.Path) -> None:
    """Второе открытие уже пересчитанные минуты не трогает.

    Иначе миграция сносила бы честные контракты догрузки при каждом запуске:
    после неё те же минуты кладутся заново тем же источником и тем же рангом,
    и отличить их от прежних внутри строки нечем.
    """
    path = tmp_path / "twice.sqlite3"
    moment = msk(2026, 9, 4, 20, 44)
    fourth_schema_database(
        path, [("MXU6", ts(moment), 1.0, 1.0, 1.0, 1.0, 64_264_575, "broker_history", 0)]
    )
    with CandleStore(path) as store:
        assert store.dropped_minutes == 1
        store.put_minutes("MXU6", [minute(moment, volume=284)], Source.BROKER_HISTORY)

    with CandleStore(path) as again:
        assert again.dropped_minutes == 0, "миграция сработала второй раз"
        assert [candle.volume for candle in again.minutes("MXU6")] == [284.0], (
            "миграция снесла минуту, записанную уже в контрактах"
        )


def test_the_journal_of_an_old_database_gets_the_new_columns(
    tmp_path: pathlib.Path,
) -> None:
    """`CREATE TABLE IF NOT EXISTS` старую таблицу не трогает — колонки дописываются.

    Иначе первая же запись отчёта в базе прежней сборки падает на вставке.
    """
    path = tmp_path / "old-schema.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE data_load (
            id INTEGER PRIMARY KEY AUTOINCREMENT, at INTEGER NOT NULL,
            symbol TEXT NOT NULL, source TEXT NOT NULL,
            requested_from TEXT, requested_to TEXT,
            fetched INTEGER NOT NULL DEFAULT 0, inserted INTEGER NOT NULL DEFAULT 0,
            updated INTEGER NOT NULL DEFAULT 0, kept INTEGER NOT NULL DEFAULT 0,
            duplicates INTEGER NOT NULL DEFAULT 0,
            missing_minutes INTEGER NOT NULL DEFAULT 0,
            gap_count INTEGER NOT NULL DEFAULT 0,
            pages INTEGER NOT NULL DEFAULT 0, requests INTEGER NOT NULL DEFAULT 0,
            retries INTEGER NOT NULL DEFAULT 0,
            first_ts INTEGER, last_ts INTEGER, summary TEXT NOT NULL DEFAULT ''
        )
        """
    )
    connection.execute("PRAGMA user_version = 1")
    connection.commit()
    connection.close()

    with CandleStore(path) as store:
        store.write_load_report(
            LoadReport(symbol="MXU6", source="broker", collapsed=4, overnight_gaps=7),
            now=msk(2026, 8, 26, 20, 0),
        )
        (entry,) = store.load_log("MXU6")
        assert entry["collapsed"] == 4
        assert entry["overnight_gaps"] == 7


# -- запись и чтение ---------------------------------------------------------


def test_round_trip_keeps_moscow_time_and_prices(store: CandleStore) -> None:
    candles = minutes_from(msk(2026, 8, 26, 10, 0), 3)
    store.put_minutes("MXU6", candles, Source.ISS)

    read_back = store.minutes("MXU6")
    assert [c.time for c in read_back] == [c.time for c in candles]
    assert all(c.time.utcoffset() == timedelta(hours=3) for c in read_back)
    assert [c.open for c in read_back] == [c.open for c in candles]


def test_reading_window_is_half_open(store: CandleStore) -> None:
    """`[начало, конец)`: соседние периоды стыкуются без пересечения и без дыры."""
    store.put_minutes("MXU6", minutes_from(msk(2026, 8, 26, 10, 0), 10), Source.ISS)

    first = store.minutes("MXU6", msk(2026, 8, 26, 10, 0), msk(2026, 8, 26, 10, 5))
    second = store.minutes("MXU6", msk(2026, 8, 26, 10, 5), msk(2026, 8, 26, 10, 10))

    assert len(first) == 5 and len(second) == 5
    assert {c.time for c in first} & {c.time for c in second} == set()
    assert len(first) + len(second) == 10


def test_same_minute_written_twice_gives_one_row(store: CandleStore) -> None:
    candles = minutes_from(msk(2026, 8, 26, 10, 0), 5)
    store.put_minutes("MXU6", candles, Source.ISS)
    written = store.put_minutes("MXU6", candles, Source.ISS)

    assert store.coverage("MXU6").count == 5
    assert written.inserted == 0
    assert written.updated == 5


def test_duplicate_inside_one_batch_collapses(store: CandleStore) -> None:
    one = minute(msk(2026, 8, 26, 10, 0), open=100)
    other = minute(msk(2026, 8, 26, 10, 0), open=200)
    written = store.put_minutes("MXU6", [one, other], Source.ISS)

    assert written.inserted == 1
    (candle,) = store.minutes("MXU6")
    assert candle.open == 200  # в рамках одного источника побеждает более поздняя


def test_the_same_minute_with_seconds_is_one_row(store: CandleStore) -> None:
    """Минутка от биржи и та же минутка из потока — одна строка, не две.

    В базе `10:05:00` от биржи, из потока приходит `10:05:07`. Без приведения
    к началу минуты это разные ключи: две строки на одну минуту, и объём в баре
    сложится вдвое. Найти это потом не по чему — число выглядит правдоподобно.
    """
    from_exchange = minute(msk(2026, 8, 26, 10, 5), open=100, volume=7)
    from_stream = from_exchange.replace(
        time=msk(2026, 8, 26, 10, 5).replace(second=7), open=101, volume=9
    )
    store.put_minutes("MXU6", [from_exchange], Source.ISS)
    written = store.put_minutes("MXU6", [from_stream], Source.BROKER)

    (candle,) = store.minutes("MXU6")
    assert candle.time == msk(2026, 8, 26, 10, 5)
    assert (candle.open, candle.volume) == (100, 7), "свеча брокера перебила биржевую"
    assert written.kept == 1, "запись с секундами не сравнилась с имеющейся минуткой"
    assert store.coverage("MXU6").count == 1

    (bar,) = store.bars("MXU6", M5)
    assert bar.volume == 7, "объём одной минуты сложился вдвое"
    assert bar.filled_minutes == 1


def test_a_batch_that_collapses_says_so(store: CandleStore) -> None:
    """Схлопывание пачки в одну минуту — число, а не молчание.

    Ноль здесь норма. Другое число означает, что источник прислал одну минуту
    несколько раз. В базу они лягут одной строкой — и это правильно, — но
    вызывающий обязан это увидеть.
    """
    first_moment = msk(2026, 8, 26, 10, 5)
    snapshots = [
        minute(first_moment.replace(second=s), open=100.0 + s, volume=1) for s in (0, 15, 30, 45)
    ]
    written = store.put_minutes("MXU6", snapshots, Source.BROKER)

    assert written.inserted == 1
    assert written.collapsed == 3
    assert store.coverage("MXU6").count == 1


def test_the_last_snapshot_of_a_minute_wins_whole(store: CandleStore) -> None:
    """Побеждает **последний снимок минуты — целиком**, ничего не складывается.

    Правило верно ровно для снимков нарастающим итогом: каждый следующий уже
    содержит всё, что было в минуте раньше. Для потока **сделок** оно неверно:
    четыре сделки по объёму 1 дадут объём 1 вместо 4, а `high`/`low` станут
    экстремумами последней сделки, а не минуты. По испорченному `high` тейк
    в прогоне не сработает — а в бою сработает.

    Тест держит именно **какая** свеча выживает: без него схлопывание
    проверялось только числом, и подмена смысла прошла бы молча. Семантика
    потока БКС — открытый долг Э1-5, до его закрытия сюда подаются свечи.
    """
    first_moment = msk(2026, 8, 26, 10, 5)
    snapshots = [
        minute(first_moment.replace(second=0), open=100, high=105, low=99, close=104, volume=1),
        minute(first_moment.replace(second=20), open=100, high=110, low=98, close=109, volume=2),
        minute(first_moment.replace(second=40), open=100, high=107, low=97, close=103, volume=3),
    ]
    store.put_minutes("MXU6", snapshots, Source.BROKER)

    (candle,) = store.minutes("MXU6")
    assert (candle.open, candle.high, candle.low, candle.close) == (100, 107, 97, 103)
    assert candle.volume == 3, "объём сложился, а не заменился последним снимком"


def test_which_snapshot_wins_does_not_depend_on_the_order_of_the_batch(
    store: CandleStore,
) -> None:
    """«Последний в пачке» — это порядок пачки, а не порядок отметок времени.

    Правило записано ровно так и проверяется ровно так: перевёрнутая пачка
    даёт другой результат, и это не случайность, а следствие правила.
    """
    first_moment = msk(2026, 8, 26, 10, 5)
    snapshots = [
        minute(first_moment.replace(second=0), open=100, volume=1),
        minute(first_moment.replace(second=40), open=140, volume=3),
    ]
    store.put_minutes("MXU6", list(reversed(snapshots)), Source.BROKER)
    (candle,) = store.minutes("MXU6")
    assert candle.open == 100, "в базе не последняя свеча пачки"


def test_a_clean_batch_collapses_nothing(store: CandleStore) -> None:
    written = store.put_minutes(
        "MXU6", minutes_from(msk(2026, 8, 26, 10, 0), 5), Source.ISS
    )
    assert written.collapsed == 0


def test_only_minute_candles_are_stored(store: CandleStore) -> None:
    """Собранные таймфреймы не хранятся принципиально."""
    five_minute_bar = Candle(
        time=msk(2026, 8, 26, 10, 5), open=1, high=1, low=1, close=1, volume=1,
        timeframe=M5, filled_minutes=5,
    )
    with pytest.raises(ValueError, match="только минутные"):
        store.put_minutes("MXU6", [five_minute_bar], Source.ISS)


# -- правило источника правды ------------------------------------------------


def test_exchange_data_overwrites_broker_data(store: CandleStore) -> None:
    """Свеча брокера заменяется биржевой — стыка не видно.

    Поток брокера предварительный: свеча может прийти до конца минуты,
    объём может дописаться позже. Итоговые данные у биржи.
    """
    moment = msk(2026, 8, 26, 10, 0)
    store.put_minutes("MXU6", [minute(moment, open=100, volume=5)], Source.BROKER)
    written = store.put_minutes("MXU6", [minute(moment, open=101, volume=9)], Source.ISS)

    (candle,) = store.minutes("MXU6")
    assert (candle.open, candle.volume) == (101, 9)
    assert written.updated == 1
    assert store.source_counts("MXU6") == {"iss": 1}


def test_broker_data_does_not_overwrite_exchange_data(store: CandleStore) -> None:
    """Обратный порядок вставки даёт тот же результат.

    Правило выбора записано и не зависит от того, что пришло позже.
    """
    moment = msk(2026, 8, 26, 10, 0)
    store.put_minutes("MXU6", [minute(moment, open=101, volume=9)], Source.ISS)
    written = store.put_minutes("MXU6", [minute(moment, open=100, volume=5)], Source.BROKER)

    (candle,) = store.minutes("MXU6")
    assert (candle.open, candle.volume) == (101, 9)
    assert written.kept == 1
    assert store.source_counts("MXU6") == {"iss": 1}


def test_no_visible_seam_between_history_and_live(store: CandleStore) -> None:
    """Историю и реальные свечи читатель не различает: один ряд без разрывов.

    Пункт приёмки «стыка между историей и реальными свечами не видно».
    """
    store.put_minutes("MXU6", minutes_from(msk(2026, 8, 26, 10, 0), 5), Source.ISS)
    store.put_minutes("MXU6", minutes_from(msk(2026, 8, 26, 10, 5), 5), Source.BROKER)

    stored = store.minutes("MXU6")
    moments = [c.time for c in stored]
    assert len(stored) == 10
    assert moments == sorted(moments)
    assert store.missing_minutes("MXU6") == 0
    assert store.gaps("MXU6") == []
    assert store.source_counts("MXU6") == {"iss": 5, "broker": 5}


# -- перезапуск --------------------------------------------------------------


def test_records_survive_restart(tmp_path: pathlib.Path) -> None:
    """Записи не теряются при перезапуске программы — это пункт приёмки."""
    path = tmp_path / "candles.sqlite3"
    with CandleStore(path) as first_one:
        first_one.put_minutes("MXU6", minutes_from(msk(2026, 8, 26, 10, 0), 7), Source.ISS)
        first_one.write_load_report(
            LoadReport(symbol="MXU6", source="iss",
                       requested_from=date(2026, 8, 26), requested_to=date(2026, 8, 26)),
            now=msk(2026, 8, 26, 12, 0),
        )

    with CandleStore(path) as second_one:
        assert second_one.coverage("MXU6").count == 7
        assert len(second_one.load_log("MXU6")) == 1


# -- сборка на чтении --------------------------------------------------------


def test_bars_are_built_from_minutes_on_read(store: CandleStore) -> None:
    store.put_minutes("MXU6", minutes_from(msk(2026, 8, 26, 10, 0), 10), Source.ISS)
    bars = store.bars("MXU6", M5)
    assert [b.time.strftime("%H:%M") for b in bars] == ["10:00", "10:05"]
    assert all(b.filled_minutes == 5 for b in bars)


def test_tail_bar_is_unsettled_while_the_interval_is_not_over(store: CandleStore) -> None:
    """Хвостовой бар помечается незакрытым по самим данным.

    Мы знаем минутки по последнюю имеющуюся и ничего не знаем после неё:
    бар, чей интервал заканчивается позже, ещё изменится при догрузке.
    """
    store.put_minutes("MXU6", minutes_from(msk(2026, 8, 26, 10, 0), 7), Source.ISS)
    bars = store.bars("MXU6", M5)
    assert [(b.time.strftime("%H:%M"), b.unsettled) for b in bars] == [
        ("10:00", False),
        ("10:05", True),
    ]
    assert [b.time.strftime("%H:%M") for b in store.bars("MXU6", M5, drop_unsettled=True)] == ["10:00"]


def test_bars_of_empty_symbol(store: CandleStore) -> None:
    assert store.bars("НЕТ ТАКОГО", M5) == []
    assert store.coverage("НЕТ ТАКОГО").empty


# -- закрытость бара считается по учёту загрузки -----------------------------


def previous_day_with_a_hole(store: CandleStore) -> None:
    """Позавчера докачалось до 10:07, вчера — целиком.

    Так выглядит оборванная выдача сервера: день записан наполовину,
    а следующие дни легли целиком. «Последняя минутка в базе» после этого
    лежит далеко за дырой.
    """
    store.put_minutes("MXU6", minutes_from(msk(2026, 8, 24, 10, 0), 8), Source.ISS)
    store.put_minutes("MXU6", minutes_from(msk(2026, 8, 25, 10, 0), 10), Source.ISS)


def test_a_bar_at_the_edge_of_a_hole_is_not_called_settled(store: CandleStore) -> None:
    """Огрызок бара на краю дыры незакрыт — потому что день не отмечен загруженным.

    Прежде `known_until` брался из данных, на весь период сразу: последняя
    минутка лежала во вчерашнем дне, и бар 10:05 позавчерашнего дня, собранный
    из **трёх** минуток вместо пяти, объявлялся закрытым. Он входил в среднюю
    как полноценная свеча; сверка с прототипом расходилась на одной свече,
    и искать это пошли бы в стратегии.
    """
    previous_day_with_a_hole(store)
    # Отмечен загруженным только вчерашний день: позавчерашний оборвался.
    store.mark_days_requested("MXU6", [date(2026, 8, 25)], now=msk(2026, 8, 26, 9, 0))

    bars = {(b.time.date(), b.time.strftime("%H:%M")): b for b in store.bars("MXU6", M5)}
    stub_bar = bars[(date(2026, 8, 24), "10:05")]

    assert stub_bar.filled_minutes == 3
    assert stub_bar.unsettled, "бар на краю дыры объявлен закрытым"
    assert bars[(date(2026, 8, 24), "10:00")].unsettled is False
    # Вчерашний день отмечен загруженным — его бары закрыты все.
    assert not bars[(date(2026, 8, 25), "10:05")].unsettled


def test_the_same_bar_is_settled_once_the_day_is_accounted_for(store: CandleStore) -> None:
    """Тот же ряд, но день подтверждён загрузкой — бар закрыт.

    Неполнота при этом остаётся: минуток в нём по-прежнему три из пяти.
    «Сделок не было» и «не докачали» — разные вещи, и различает их учёт,
    а не сами данные.
    """
    previous_day_with_a_hole(store)
    store.mark_days_requested(
        "MXU6", [date(2026, 8, 24), date(2026, 8, 25)], now=msk(2026, 8, 26, 9, 0)
    )

    (the_bar,) = [
        b for b in store.bars("MXU6", M5)
        if b.time == msk(2026, 8, 24, 10, 5)
    ]
    assert the_bar.filled_minutes == 3
    assert the_bar.is_partial
    assert not the_bar.unsettled


def test_dropping_unsettled_removes_exactly_the_edge_of_the_hole(store: CandleStore) -> None:
    """`drop_unsettled` выбрасывает огрызок у дыры, а не хвост всего ряда."""
    previous_day_with_a_hole(store)
    store.mark_days_requested("MXU6", [date(2026, 8, 25)], now=msk(2026, 8, 26, 9, 0))

    settled = [
        (b.time.date().isoformat(), b.time.strftime("%H:%M"))
        for b in store.bars("MXU6", M5, drop_unsettled=True)
    ]
    assert settled == [
        ("2026-08-24", "10:00"),
        ("2026-08-25", "10:00"),
        ("2026-08-25", "10:05"),
    ]


def test_without_any_accounting_knowledge_ends_at_the_last_minute_of_each_day(
    store: CandleStore,
) -> None:
    """Про день ничего не записано — знание кончается на его последней минутке.

    Признак закрытости никогда не сильнее учёта: без учёта он строже, а не
    мягче. Здесь это видно на позавчерашнем огрызке — он незакрыт **и без**
    отметок; вчерашний день заканчивается ровно на границе бара, и его
    последний бар закрыт по самим данным.
    """
    previous_day_with_a_hole(store)  # ни одного `mark_days_requested`

    unsettled_bars = [
        (b.time.date().isoformat(), b.time.strftime("%H:%M"))
        for b in store.bars("MXU6", M5)
        if b.unsettled
    ]
    assert unsettled_bars == [("2026-08-24", "10:05")]


def test_the_tail_of_a_day_needs_accounting_to_become_settled(store: CandleStore) -> None:
    """День, оборвавшийся посреди бара, закрывается только отметкой о загрузке.

    Без отметки последний бар дня остаётся незакрытым навсегда — и это
    правильно: «данные кончились» и «торги кончились» по самому ряду
    не различаются. Отметка о том, что день запрошен и закончился, различает.
    """
    store.put_minutes("MXU6", minutes_from(msk(2026, 8, 24, 10, 0), 8), Source.ISS)
    store.put_minutes("MXU6", minutes_from(msk(2026, 8, 25, 10, 0), 10), Source.ISS)

    without_known_until = {b.time.strftime("%d %H:%M"): b.unsettled for b in store.bars("MXU6", M5)}
    assert without_known_until["24 10:05"] is True

    store.mark_days_requested("MXU6", [date(2026, 8, 24)], now=msk(2026, 8, 26, 9, 0))
    with_known_until = {b.time.strftime("%d %H:%M"): b.unsettled for b in store.bars("MXU6", M5)}
    assert with_known_until["24 10:05"] is False


# -- «данные полны до T»: то, чего учёт по дням сказать не умеет --------------


def test_a_caller_who_knows_the_boundary_can_say_so(store: CandleStore) -> None:
    """Источник называет момент, до которого данные заведомо полны.

    Боевой путь: сегодняшний день не отмечен загруженным никогда. Бар 10:55
    закрывается в 11:00 — это граница торгового окна, на которой срабатывает
    «закрывать в конце окна». По правилу «последняя имеющаяся минутка + 1» он
    станет закрытым только когда придёт минутка 11:00 или позже; на MXU6 пусты
    26 % минутных слотов, и ждать её можно минутами. Принудительный выход
    подаётся с опозданием и исполняется по `open` не той свечи.
    """
    store.put_minutes("MXU6", minutes_from(msk(2026, 8, 26, 10, 50), 8), Source.ISS)
    last_bar = msk(2026, 8, 26, 10, 55)

    without_knowledge = {b.time: b.unsettled for b in store.bars("MXU6", M5)}
    assert without_knowledge[last_bar] is True

    with_knowledge = {
        b.time: b.unsettled
        for b in store.bars("MXU6", M5, known_until=msk(2026, 8, 26, 11, 0))
    }
    assert with_knowledge[last_bar] is False


def test_the_claimed_boundary_cannot_weaken_what_the_data_shows(
    store: CandleStore,
) -> None:
    """Заявление только усиливает знание, ослабить его нечем.

    Иначе параметр стал бы способом объявить закрытый бар незакрытым —
    ошибка в другую сторону, но такая же тихая. Здесь данные идут до 10:09,
    то есть бар 10:05 закрыт по самим данным; заявление «знаю до 10:02»
    ничего в этом не меняет.
    """
    store.put_minutes("MXU6", minutes_from(msk(2026, 8, 26, 10, 0), 10), Source.ISS)
    too_early = msk(2026, 8, 26, 10, 2)

    no_argument = {b.time: b.unsettled for b in store.bars("MXU6", M5)}
    with_declaration = {
        b.time: b.unsettled for b in store.bars("MXU6", M5, known_until=too_early)
    }
    assert no_argument == with_declaration
    assert with_declaration[msk(2026, 8, 26, 10, 5)] is False


def test_the_claimed_boundary_does_not_leak_into_the_next_day(store: CandleStore) -> None:
    """Заявление режется концом суток: бар другого дня им не закрыть.

    Знание ведётся по дням, и `known_until = завтра 23:00` не означает,
    что позавчерашняя дыра докачана.
    """
    store.put_minutes("MXU6", minutes_from(msk(2026, 8, 24, 10, 0), 8), Source.ISS)
    store.put_minutes("MXU6", minutes_from(msk(2026, 8, 25, 10, 0), 10), Source.ISS)

    settledness = {
        (b.time.day, b.time.strftime("%H:%M")): b.unsettled
        for b in store.bars("MXU6", M5, known_until=msk(2026, 8, 24, 23, 0))
    }
    assert settledness[(24, "10:05")] is False, "заявленная граница не сработала"
    # 25-е заявлением не покрыто — работает правило по данным.
    assert settledness[(25, "10:05")] is False


def test_the_tester_can_close_the_right_edge_of_its_range(store: CandleStore) -> None:
    """Правая граница отрезка перестаёт зависеть от ликвидности двух минут.

    `bars(since=A, until=B)` без заявления кончался на последней имеющейся
    минутке: сделка в 10:57 — и бар 10:55 незакрыт, а при `drop_unsettled`
    его нет вовсе. Тестер границу отрезка знает — он её и задал.
    """
    candles = minutes_from(msk(2026, 8, 26, 10, 50), 5) + [
        minute(msk(2026, 8, 26, 10, 55)),
        minute(msk(2026, 8, 26, 10, 57)),
    ]
    store.put_minutes("MXU6", candles, Source.ISS)
    last_moment = msk(2026, 8, 26, 11, 0)

    without_knowledge = [b.time for b in store.bars("MXU6", M5, until=last_moment, drop_unsettled=True)]
    assert msk(2026, 8, 26, 10, 55) not in without_knowledge

    with_knowledge = [
        b.time
        for b in store.bars("MXU6", M5, until=last_moment, drop_unsettled=True, known_until=last_moment)
    ]
    assert msk(2026, 8, 26, 10, 55) in with_knowledge


def test_accounting_still_beats_a_missing_claim(store: CandleStore) -> None:
    """`known_until=None` — сегодняшнее поведение слово в слово."""
    previous_day_with_a_hole(store)
    store.mark_days_requested("MXU6", [date(2026, 8, 25)], now=msk(2026, 8, 26, 9, 0))

    no_argument = [(b.time, b.unsettled) for b in store.bars("MXU6", M5)]
    with_none = [(b.time, b.unsettled) for b in store.bars("MXU6", M5, known_until=None)]
    assert no_argument == with_none


def test_bars_stay_sorted_across_days(store: CandleStore) -> None:
    """Сборка идёт по дням — порядок и состав ряда от этого не меняются."""
    previous_day_with_a_hole(store)
    bars = store.bars("MXU6", M5)
    assert [b.time for b in bars] == sorted(b.time for b in bars)
    assert len(bars) == 4


# -- разрывы -----------------------------------------------------------------


def test_gaps_are_found_and_counted(store: CandleStore) -> None:
    candles = minutes_from(msk(2026, 8, 26, 10, 0), 3) + minutes_from(msk(2026, 8, 26, 10, 10), 3)
    store.put_minutes("MXU6", candles, Source.ISS)

    assert store.missing_minutes("MXU6") == 7
    (gap_found,) = store.gaps("MXU6", min_minutes=5)
    assert (gap_found.start, gap_found.end, gap_found.minutes) == (
        msk(2026, 8, 26, 10, 3), msk(2026, 8, 26, 10, 9), 7,
    )
    assert not gap_found.crosses_date


def test_gap_across_midnight_is_marked(store: CandleStore) -> None:
    """Ночной перерыв в торгах помечается, чтобы не выглядеть потерей данных."""
    candles = [minute(msk(2026, 8, 26, 23, 49)), minute(msk(2026, 8, 27, 7, 0))]
    store.put_minutes("MXU6", candles, Source.ISS)
    (gap_found,) = store.gaps("MXU6", min_minutes=30)
    assert gap_found.crosses_date
    assert gap_found.minutes == 430


def test_gaps_can_be_asked_for_one_kind_only(store: CandleStore) -> None:
    """Разрывы внутри дня и переходы через сутки отбираются порознь.

    Ночь и выходные дают разрыв каждый календарный день: в общей куче они
    топят те несколько, ради которых счёт и ведётся.
    """
    store.put_minutes(
        "MXU6",
        [
            minute(msk(2026, 8, 26, 10, 0)),
            minute(msk(2026, 8, 26, 10, 30)),
            minute(msk(2026, 8, 27, 7, 0)),
        ],
        Source.ISS,
    )
    assert len(store.gaps("MXU6", min_minutes=5)) == 2
    (inside_the_day,) = store.gaps("MXU6", min_minutes=5, crossing_date=False)
    assert inside_the_day.minutes == 29
    (across_midnight,) = store.gaps("MXU6", min_minutes=5, crossing_date=True)
    assert across_midnight.crosses_date


# -- журнал ------------------------------------------------------------------


def test_load_report_and_gaps_go_to_the_journal(store: CandleStore) -> None:
    from market.gaps import Gap

    report = LoadReport(
        symbol="MXU6", source="iss",
        requested_from=date(2026, 8, 26), requested_to=date(2026, 8, 26),
        fetched=100, inserted=90, updated=5, kept=5, duplicates=2, missing_minutes=13,
        gaps=[Gap(msk(2026, 8, 26, 14, 0), msk(2026, 8, 26, 14, 44), 45, False)],
        pages=3, requests=4, retries=1,
    )
    number = store.write_load_report(report, now=msk(2026, 8, 26, 20, 0))

    (entry,) = store.load_log("MXU6")
    assert entry["id"] == number
    assert entry["fetched"] == 100 and entry["kept"] == 5 and entry["gap_count"] == 1
    assert "отвергнуто" in str(entry["summary"])
    assert len(store.gap_log("MXU6")) == 1


def test_the_gap_journal_shows_the_freshest_first(store: CandleStore) -> None:
    """Свежие разрывы первыми, как и записи о загрузках.

    Здесь стояло `ORDER BY start_ts` по возрастанию. При лимите 200 и трёх
    сотнях записей читатель получал двести **самых старых** — ночные перерывы
    прошлого сентября, — а дыра внутри торгового окна, ради которой порог
    и опускали, в выдачу не попадала вовсе. Рядом `load_log` всё это время
    был отсортирован правильно.
    """
    from market.gaps import Gap

    logged_gaps = [
        Gap(msk(2025, 9, 1, 10, 0), msk(2025, 9, 1, 10, 30), 31, False),
        Gap(msk(2026, 3, 1, 10, 0), msk(2026, 3, 1, 10, 30), 31, False),
        Gap(msk(2026, 8, 26, 10, 12), msk(2026, 8, 26, 10, 39), 28, False),
    ]
    store.write_load_report(
        LoadReport(symbol="MXU6", source="iss", gaps=logged_gaps), now=msk(2026, 8, 26, 20, 0)
    )

    (newest,) = store.gap_log("MXU6", limit=1)
    assert newest.start == msk(2026, 8, 26, 10, 12), "в выдачу попал самый старый"
    assert [g.start for g in store.gap_log("MXU6")] == [
        msk(2026, 8, 26, 10, 12), msk(2026, 3, 1, 10, 0), msk(2025, 9, 1, 10, 0),
    ]


def test_the_gap_journal_can_leave_out_the_night(store: CandleStore) -> None:
    """Разрывы через сутки отбираются отдельно — их в выдаче можно не держать."""
    from market.gaps import Gap

    store.write_load_report(
        LoadReport(
            symbol="MXU6", source="iss",
            gaps=[
                Gap(msk(2026, 8, 26, 10, 12), msk(2026, 8, 26, 10, 39), 28, False),
                Gap(msk(2026, 8, 26, 23, 50), msk(2026, 8, 27, 6, 59), 430, True),
            ],
        ),
        now=msk(2026, 8, 27, 20, 0),
    )

    assert len(store.gap_log("MXU6")) == 2
    (inside_the_day,) = store.gap_log("MXU6", crossing_date=False)
    assert inside_the_day.minutes == 28
    (across_midnight,) = store.gap_log("MXU6", crossing_date=True)
    assert across_midnight.minutes == 430


def test_same_gap_is_not_written_twice(store: CandleStore) -> None:
    """Повторная догрузка не плодит копии одного разрыва в журнале."""
    from market.gaps import Gap

    gap_found = Gap(msk(2026, 8, 26, 14, 0), msk(2026, 8, 26, 14, 44), 45, False)
    report = LoadReport(symbol="MXU6", source="iss", gaps=[gap_found])
    store.write_load_report(report, now=msk(2026, 8, 26, 20, 0))
    store.write_load_report(report, now=msk(2026, 8, 27, 20, 0))

    assert len(store.gap_log("MXU6")) == 1
    assert len(store.load_log("MXU6")) == 2


# -- транзакция --------------------------------------------------------------


def test_a_failed_commit_leaves_no_open_transaction(store: CandleStore) -> None:
    """Отказ самого `COMMIT` не оставляет соединение внутри транзакции.

    `COMMIT` стоял **вне** `try`: при его собственном отказе (диск полон, база
    занята) откат не делался, и следующая запись получала
    `cannot start a transaction within a transaction` — сообщение, объявленное
    признаком нарушения потоковой модели. Расследование уходило в потоки,
    а причина была на диске.
    """
    class CommitBreaker:
        """Соединение, у которого первый `COMMIT` не проходит."""

        def __init__(self, real_db) -> None:
            self._real_db = real_db
            self.break_next_commit = True

        def execute(self, sql, *args):
            if sql == "COMMIT" and self.break_next_commit:
                self.break_next_commit = False
                raise sqlite3.OperationalError("database or disk is full")
            return self._real_db.execute(sql, *args)

        def __getattr__(self, attribute):
            return getattr(self._real_db, attribute)

    real_db = store._db
    substitute = CommitBreaker(real_db)
    store._db = substitute  # type: ignore[assignment]

    with pytest.raises(sqlite3.OperationalError, match="disk is full"):
        store.put_minutes("MXU6", minutes_from(msk(2026, 8, 26, 10, 0), 3), Source.ISS)

    # Соединение снова годно: следующая запись проходит, а не падает
    # на «cannot start a transaction within a transaction».
    written = store.put_minutes("MXU6", minutes_from(msk(2026, 8, 26, 10, 0), 3), Source.ISS)
    assert written.inserted == 3
    store._db = real_db


# -- чтение для отчёта -------------------------------------------------------


def test_minute_times_reads_one_column_and_the_same_moments(store: CandleStore) -> None:
    """Отчёту нужны времена, а не свечи: 74 тысячи `Candle` ради двух чисел.

    Значения при этом обязаны совпадать со свечами до момента.
    """
    store.put_minutes("MXU6", minutes_from(msk(2026, 8, 26, 10, 0), 10), Source.ISS)
    bounds = (msk(2026, 8, 26, 10, 2), msk(2026, 8, 26, 10, 7))
    assert store.minute_times("MXU6", *bounds) == [
        c.time for c in store.minutes("MXU6", *bounds)
    ]


def test_has_minutes_after_is_a_search_not_a_count(store: CandleStore) -> None:
    store.put_minutes("MXU6", minutes_from(msk(2026, 8, 26, 10, 0), 3), Source.ISS)
    assert store.has_minutes_after("MXU6", msk(2026, 8, 26, 10, 2))
    assert not store.has_minutes_after("MXU6", msk(2026, 8, 26, 10, 3))
    assert not store.has_minutes_after("НЕТ ТАКОГО", msk(2026, 8, 26, 10, 0))


# -- учёт запрошенных дней ---------------------------------------------------


def test_trading_days_are_counted_by_moscow_date(store: CandleStore) -> None:
    """Торговый день считается по МСК, а не по зоне машины.

    На машине с зоной восточнее Москвы вечерняя свеча 26 августа по UTC
    относится к 25-му. Ошибка здесь портит сам замер глубины истории.
    """
    store.put_minutes(
        "MXU6",
        [minute(msk(2026, 8, 26, 0, 30)), minute(msk(2026, 8, 26, 23, 30)), minute(msk(2026, 8, 27, 7, 0))],
        Source.ISS,
    )
    assert store.trading_days("MXU6") == [date(2026, 8, 26), date(2026, 8, 27)]


def test_finished_days_are_not_requested_twice(store: CandleStore) -> None:
    """Закончившийся запрошенный день больше не перезапрашивается.

    Даже если свечей в нём нет: у выходного их не будет никогда, а без этой
    памяти программа спрашивала бы про каждый выходной при каждом запуске.
    """
    store.mark_days_requested(
        "MXU6",
        [date(2026, 8, 22), date(2026, 8, 23)],  # выходные, свечей нет
        counts={},
        now=msk(2026, 8, 26, 12, 0),
    )
    left = store.days_to_request("MXU6", date(2026, 8, 22), date(2026, 8, 24))
    assert left == [date(2026, 8, 24)]


def test_today_is_always_requested_again(store: CandleStore) -> None:
    """День, запрошенный посреди торгов, закрытым не считается.

    Иначе половина сегодняшней сессии осталась бы недокачанной навсегда
    и выглядела бы как обычный вечерний перерыв.
    """
    today = date(2026, 8, 26)
    store.mark_days_requested("MXU6", [today], counts={today: 300}, now=msk(2026, 8, 26, 12, 0))
    assert store.days_to_request("MXU6", today, today) == [today]

    # На следующий день он уже закрыт — но только если его перезапросили.
    store.mark_days_requested("MXU6", [today], counts={today: 800}, now=msk(2026, 8, 27, 9, 0))
    assert store.days_to_request("MXU6", today, today) == []


def test_days_to_request_refuses_backwards_period(store: CandleStore) -> None:
    with pytest.raises(ValueError, match="задом наперёд"):
        store.days_to_request("MXU6", date(2026, 8, 27), date(2026, 8, 26))
