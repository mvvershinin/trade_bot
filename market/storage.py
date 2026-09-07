"""Хранилище свечей: одна встроенная база в одном файле.

SQLite, никакого сервера. Резервная копия = копия папки `userdata/`,
перенос на другой компьютер = копирование папки (решение 0003).
В той же базе живут журнал сделок и журнал решений — типы записи и чтения
в `market.journal`, правила хранения в разделе «Журналы» ниже.

Что хранится
------------
**Только минутные свечи.** Собранные таймфреймы не хранятся вообще — и это
не экономия места, а защита: пока в базе нет пересобранных свечей, из них
физически нельзя пересобрать следующий таймфрейм и накопить ошибку округления
границ. Любой таймфрейм строится на чтении, из минуток.

Правило источника правды
------------------------
Одна минута инструмента — **одна** строка. Если минутка пришла и из открытых
данных биржи, и от брокера, побеждает **биржа**:

================  ====  ======================================================
источник          ранг  почему так
================  ====  ======================================================
`iss`             2     итоговые данные самой биржи
`broker`          1     поток реального времени: свеча может прийти до конца
                        минуты, объём может дописаться позже. Контракты
                        считаются по приращениям оборота и сверены с биржей
                        до контракта (`app/live_feed.py`, замер 04.09.2026)
`broker_history`  0     догрузка пропущенного HTTP-запросом к брокеру
                        (`app/backfill.py`). Ранг ниже всех, потому что
                        **объём здесь выведен, а не измерен**: брокер отдаёт
                        оборот в рублях, и контракты получаются делением
                        на середину разброса минуты (`market.volume`, замер
                        05.09.2026 на 81 914 минутах: точно в 81,7 % минут,
                        медианная погрешность 0,05 %). Следствие ранга не декоративное:
                        догрузка физически не может переписать ни одну
                        существующую строку, а первая же заливка тех же
                        минут с биржи заменит её
================  ====  ======================================================

* ранг новой записи **выше** — перезаписывает;
* ранг **равный** — перезаписывает (повторная загрузка того же источника
  обновляет данные, это и есть починка испорченного отрезка);
* ранг **ниже** — запись отвергается, данные в базе не меняются, факт попадает
  в отчёт полем `kept`.

Отсюда же берётся пункт приёмки «стыка между историей и реальными свечами
не видно»: свечи брокера ложатся в ту же таблицу, а следующая загрузка с биржи
молча заменяет их итоговыми. Порядок вставки на результат не влияет.

⚠️ Строка «свеча может прийти до конца минуты» описывает **снимок минуты
нарастающим итогом**, а не сделку. На вход хранилища идут свечи; повтор одной
минуты внутри пачки заменяется целиком, последним снимком (см. `put_minutes`).
Семантика потока БКС по документации брокера на 31.08.2026 не подтверждена —
это открытый долг задачи Э1-5, и до его закрытия поток тиков сюда подавать
нельзя.

Время
-----
`ts` — **начало минуты** в секундах эпохи (UTC-шкала). Хранить строку локального
времени нельзя: сортировка по тексту и сравнение периодов начнут зависеть
от формата. Наружу время отдаётся всегда tz-aware в МСК.

Секунды у ключа отбрасываются (`market.candles.floor_to_minute`). Биржа метит
минутку ровно `10:05:00`, поток брокера может прислать ту же минутку как
`10:05:07` — без приведения это разные ключи, две строки на одну минуту
и удвоенный объём в собранном баре.

Журналы
-------
Журнал сделок и журнал решений лежат в этой же базе и подчиняются трём
правилам, которые дороже кода:

* **строка принадлежит прогону.** У прогона (`journal_session`) есть
  происхождение — боевой режим, симуляция на боевом потоке или прогон
  по истории. Строка ссылается на прогон обязательным ключом, прочитать её
  без происхождения нельзя — решение 0011 в `.docs/decisions/`;

* **строка журнала не правится, не исчезает поодиночке и не удваивается.**
  Правило держится сторожами в самой базе (`_GUARDS`), а не соглашением
  между разработчиками. Закрыты все пути, которыми запись можно подменить:

  ==========================  ===============================================
  путь                        чем закрыт
  ==========================  ===============================================
  `UPDATE` строки             `journal_*_is_append_only`
  `DELETE` строки             `journal_*_dies_with_its_run` — строка умирает
                              только вместе со своим прогоном (каскад)
  `INSERT OR REPLACE`         тот же сторож плюс `PRAGMA recursive_triggers`:
                              REPLACE в SQLite это DELETE + INSERT
  правка условий прогона      `journal_session_conditions_are_final` — все
                              восемь полей, а не только происхождение
  правка заметки о конце      `journal_session_final_note_comes_with_the_close`
                              — заметка пишется тем же действием, что
                              закрывает прогон, и другого пути к ней нет
                              ни у закрытого прогона, ни у открытого
  дозапись в закрытый прогон  `journal_*_joins_an_open_run` — законченное
                              свидетельство не дополняется задним числом
  повтор той же сделки        `journal_trade_is_written_once`: повтор
                              `write_trades` после таймаута удваивал прибыль
                              за день, не переписав ни одной строки
  ==========================  ===============================================

  ⚠️ Сторож на повторе сделки — **последний** рубеж, а не первый.
  `write_trades` узнаёт уже записанную сделку сам и пропускает её, чтобы
  не потерять соседнюю новую из той же пачки; условие у проверки и у сторожа
  общее (`_TRADE_IDENTITY`). Сторож остаётся для всех остальных путей —
  прежде всего для правки базы снаружи.

  Сверх того: прогон, который нельзя повторить, не удаляется вовсе —
  и боевой, и симуляция на боевом потоке (`journal_session_evidence_is_kept`,
  список происхождений выведен из `RunOrigin.evidence`). Время конца прогона
  пишется один раз. Сторожа **пересоздаются при каждом открытии** —
  обезвреженный снаружи сторож с тем же именем не переживает открытие.

  ⚠️ Чего это НЕ даёт. Защиты от того, у кого есть права на файл базы:
  он снимет сторожа, испортит данные и уйдёт. Возвращать данные нечем —
  от этого защищает только резервная копия папки;

* **чтение идёт по одному прогону.** Без явного `session_id`, `origin`
  или `all_runs` `decisions` и `trades` отдают последний прогон, а не всю
  базу вперемешку, и говорят про обрезку по лимиту (`JournalPage`).
  Отчёт за день, в котором боевые сделки смешаны с прогоном по истории, —
  это выбор объёма по чужому числу;

* **текст чистится от секретов на записи, а не на чтении.** Причина решения
  может прийти из отказа брокера, а в теле ответа сервера бывает токен.
  Чистка на чтении оставила бы его на диске и в резервной копии. Через чистку
  идёт **весь** текст, который слой пишет: обе таблицы журнала, прогон
  и отчёт о загрузке. Список «эти колонки чистые» не ведётся — он один раз
  уже оказался неверным сразу в семи местах.

Поток
-----
⚠️ **Соединение принадлежит потоку, который его создал.** `sqlite3.connect`
здесь без `check_same_thread=False`, и это выбор по замеру, а не недосмотр:
флаг снимает проверку принадлежности и **оставляет** гонку транзакций (голый
`BEGIN` в `_transaction`), то есть меняет понятный отказ на редкий. Сквозной
вход в базу — `market.worker`, однопоточный фасад: он создаёт `CandleStore`
внутри своего потока и наружу отдаёт ожидаемые (`await`) методы.
[Решение 0005](../.docs/decisions/0005-concurrency-model.md), раздел следствий.
"""

from __future__ import annotations

import pathlib
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from itertools import groupby
from types import TracebackType
from typing import Any

from market.aggregate import build_bars
from market.candles import MINUTE, MSK, Candle, Timeframe, ensure_msk, floor_to_minute
from market.gaps import Gap, count_missing_minutes, find_gaps
from market.journal import (
    BACKTEST_SESSIONS_KEPT,
    DecisionLevel,
    DecisionRecord,
    HaltKind,
    HaltRecord,
    JournalPage,
    JournalSession,
    JournalStats,
    PruneReport,
    RunOrigin,
    SessionRecord,
    StoredDecision,
    StoredHalt,
    StoredTrade,
    TradeRecord,
    TradeSide,
    redact,
)
from market.paths import default_db_path, ensure_userdata_dir
from market.reports import LoadReport
from market.synthetic import (
    is_synthetic,
    refuse_synthetic_in_working_base,
)

__all__ = ["Source", "Coverage", "WriteStats", "CandleStore", "SCHEMA_VERSION"]

#: Версия схемы. Меняется, когда меняется **смысл** записанного, а не только
#: набор колонок.
#:
#: 1 → 2 (31.08.2026). Ключ `ts` стал началом **минуты**: раньше это был момент,
#: которым свечу пометил источник, и поток брокера мог положить `10:05:07`
#: отдельной строкой рядом с биржевой `10:05:00`. Такая база при чтении даёт
#: две записи на одну минуту, и `bars()` бросает `ValueError` на весь запрос —
#: график пуст, источник свечей падает. Миграция `_migrate_minute_keys`
#: приводит ключи к минуте по тому же правилу источника правды.
#: Тогда же заведена колонка `data_load.collapsed`.
#:
#: 2 → 3 (03.09.2026). В базе поселились журнал сделок и журнал решений
#: (`journal_session`, `journal_trade`, `journal_decision`) вместе со сторожами,
#: запрещающими переписывать строку задним числом и удалять боевой прогон.
#: Данные схемы 2 миграция не трогает: свечей это не касается, и переносить
#: нечего — журналов до неё не существовало вовсе.
#:
#: 3 → 4 (03.09.2026). У прогона появилась **вторая** заметка: `note` —
#: условие открытия, `finish_note` — чем прогон кончился. Раньше вторая
#: писалась поверх первой, и условие открытия исчезало. Тогда же `CHECK`
#: на происхождение перестал быть написанным вручную списком и выводится
#: из `RunOrigin`; на базах схемы 3 он поправим только пересборкой таблицы,
#: и `_rebuild_journal_session` её делает.
#:
#: 4 → 5 (05.09.2026). У колонки `volume` появился **смысл**: контракты,
#: у любого источника. До этой версии догрузка у брокера писала туда оборот
#: в рублях как пришёл (`D-013`), то есть в одной колонке лежали величины,
#: различающиеся впятеро больше, чем на пять порядков — 2,199 × 10⁵ по замеру
#: на базе владельца счёта. Пересчитать записанное миграция не может: делить
#: надо на цену **и** на множитель «рублей за пункт», а множитель живёт
#: на бирже, и ходить в сеть при открытии базы нельзя. Поэтому строки
#: догрузки снимаются (`_drop_provisional_minutes`) — они по построению
#: временные, ранг 0, и следующий заход кладёт их заново уже в контрактах.
#:
#: 5 → 6 (07.09.2026). В базе поселилась **остановка робота** (`robot_halt`):
#: до этой версии она жила в памяти порта и стиралась перезапуском программы
#: (`D-043`). Переносить нечего — таблицы не существовало вовсе, и
#: `CREATE TABLE IF NOT EXISTS` заводит её на базе любой прежней схемы.
#:
#: ⚠️ Версия всё равно поднята, и не ради колонок. Сборка со схемой 5 открыла
#: бы базу со стоящей остановкой и **не увидела** бы её: таблицу она
#: не читает, робот пошёл бы торговать. Поднятая версия превращает это
#: в честный отказ при открытии вместо тихого обхода предохранителя.
SCHEMA_VERSION = 6

#: Начиная с этой версии ключ `ts` — гарантированно начало минуты. База
#: младше её проходит `_migrate_minute_keys`; база от неё и старше — нет,
#: и это не оптимизация: полный перебор `minute_candle` при каждом открытии
#: базы стоил бы прохода по всем свечам ради заведомо пустого результата.
_SCHEMA_WITH_MINUTE_KEYS = 2

#: Начиная с этой версии у прогона две заметки, а `CHECK` на происхождение
#: выведен из `RunOrigin`. База младше проходит `_rebuild_journal_session`:
#: `CREATE TABLE IF NOT EXISTS` существующую таблицу не трогает, и ни новая
#: колонка, ни исправленный `CHECK` до неё сами не доедут.
_SCHEMA_WITH_TWO_NOTES = 4

#: Начиная с этой версии в `volume` у всех источников контракты. База младше
#: проходит `_drop_provisional_minutes`: в ней могли остаться минуты догрузки
#: с оборотом в рублях, и отличить их от честных контрактов внутри строки
#: нечем — только по источнику.
_SCHEMA_WITH_CONTRACT_VOLUME = 5

#: Таблицы журнала. Пересборка прогонов (`_rebuild_journal_session`) обязана
#: вернуть каждую строку каждой из них — это и проверяется счётом до и после.
_JOURNAL_TABLES: tuple[str, ...] = ("journal_session", "journal_decision", "journal_trade")

#: Счётчик номеров прогонов в снимке журнала (`_journal_snapshot`): строка
#: `sqlite_sequence` таблицы прогонов. Пересборка обязана вернуть и его —
#: иначе номер удалённого прогона выдаётся заново (`_carry_the_run_counter`).
_RUN_COUNTER = "sqlite_sequence.journal_session"

_MINUTE = timedelta(minutes=1)


class Source(str, Enum):
    """Откуда пришла свеча. Ранг задаёт правило разрешения конфликта.

    Разбор каждого ранга — в шапке модуля. Здесь важно одно: значение члена
    попадает в колонку `source` как есть, и `CHECK` на неё в схеме нет, —
    поэтому новый источник не требует переезда базы, а старая база с новым
    кодом читается без единой правки.
    """

    ISS = "iss"
    BROKER = "broker"
    BROKER_HISTORY = "broker_history"
    STITCHED = "stitched"

    @property
    def rank(self) -> int:
        return _RANKS[self]


#: Ранги перечислены **таблицей на все члены** и сверяются с ней тестом:
#: источник, забытый здесь, падал бы `KeyError` не при добавлении, а при первой
#: записи из него — то есть в бою.
_RANKS: dict[Source, int] = {
    Source.ISS: 2,
    Source.BROKER: 1,
    Source.BROKER_HISTORY: 0,
    # Ряд, собранный нами из ближних контрактов (`market.chain`). Ранг выше
    # биржи не ради старшинства: под синтетическим кодом (`@…`) у биржи нет
    # и не может быть ни одной свечи, конфликтовать не с чем. Ранг назван
    # отдельным, чтобы собранная минутка **не** выглядела в базе биржевой:
    # различить их иначе нечем, а это разные вещи по происхождению.
    Source.STITCHED: 3,
}


@dataclass(frozen=True, slots=True)
class Coverage:
    """Что по инструменту уже есть в базе."""

    symbol: str
    first: datetime | None
    last: datetime | None
    count: int

    @property
    def empty(self) -> bool:
        return self.count == 0


@dataclass(frozen=True, slots=True)
class WriteStats:
    """Итог одной записи пачки минуток."""

    inserted: int = 0
    updated: int = 0
    kept: int = 0
    #: Сколько свечей пачки схлопнулось в уже занятую минуту. Ноль — норма;
    #: другое число означает, что источник прислал одну минуту несколько раз
    #: (например, поток брокера с секундами: `10:05:00` и `10:05:07`).
    #: В базу они лягут одной строкой — **побеждает последняя в пачке**, —
    #: и это верно ровно до тех пор, пока источник шлёт снимки минуты
    #: нарастающим итогом. Число обязано быть видно вызывающему: схлопнутая
    #: молча пачка выглядит как нормальная запись, а объём в базе при этом
    #: не сумма, а последнее значение (см. `put_minutes`).
    collapsed: int = 0

    @property
    def written(self) -> int:
        return self.inserted + self.updated


def _sql_values(values: Iterable[str]) -> str:
    """Значения перечисления списком для SQL: `'live', 'paper', 'backtest'`.

    Кавычки одинарные — других строковых в SQLite нет. Экранирование здесь
    не нужно и намеренно не делается: на вход идут **только** значения наших
    же перечислений, а не чужой текст. Значение с кавычкой внутри сломает
    схему на первом открытии базы, то есть будет замечено сразу, а не станет
    щелью в чужих руках.
    """
    return ", ".join(f"'{value}'" for value in values)


#: Допустимые значения трёх колонок журнала — **выводятся** из перечислений,
#: а не переписаны в SQL строкой. Причина та же, что у `_EVIDENCE_ORIGINS`
#: ниже: четвёртое происхождение, пятый уровень или третья сторона попадут
#: в `CHECK` сами. Написанный руками список молча остался бы прежним, и база
#: начала бы отвергать значение, которое код считает законным.
_ORIGIN_VALUES: tuple[str, ...] = tuple(origin.value for origin in RunOrigin)
_LEVEL_VALUES: tuple[str, ...] = tuple(level.value for level in DecisionLevel)
_SIDE_VALUES: tuple[str, ...] = tuple(side.value for side in TradeSide)
_HALT_KIND_VALUES: tuple[str, ...] = tuple(kind.value for kind in HaltKind)

#: Таблица прогонов — **одним** определением на два места: `_SCHEMA` создаёт её
#: на новой базе, `_rebuild_journal_session` пересобирает по нему же старую.
#: Два текста одной таблицы разошлись бы молча, и разошлись бы именно там,
#: где расхождение не видно, — в `CHECK`.
_SESSION_TABLE = """
CREATE TABLE IF NOT EXISTS {name} (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    origin      TEXT    NOT NULL CHECK (origin IN ({origins})),
    started_at  INTEGER NOT NULL,
    finished_at INTEGER,
    symbol      TEXT    NOT NULL DEFAULT '',
    timeframe   TEXT    NOT NULL DEFAULT '',
    strategy    TEXT    NOT NULL DEFAULT '',
    settings    TEXT    NOT NULL DEFAULT '',
    app_version TEXT    NOT NULL DEFAULT '',
    note        TEXT    NOT NULL DEFAULT '',
    finish_note TEXT    NOT NULL DEFAULT ''
)"""

#: Колонки прогона — **один** список на всё, что их перечисляет: чтение
#: (`_SESSION_SELECT` и `_session_of`), заморозка (`_FROZEN_SESSION_COLUMNS`)
#: и запись (`open_journal_session`). Порядок — тот же, что в `_SESSION_TABLE`
#: выше, и это сверяется тестом по `PRAGMA table_info`.
#:
#: ⚠️ Списков было шесть, и стерёг их комментарий. Цена расхождения названа
#: там же: у колонок прогона одинаковые типы, поэтому перепутанные местами
#: `note` и `finish_note` не уронили бы ничего, кроме смысла, — прогон
#: прочитался бы с заметкой о конце вместо условия открытия, и заметил бы
#: это человек, а не программа.
_SESSION_COLUMNS: tuple[str, ...] = (
    "id", "origin", "started_at", "finished_at", "symbol", "timeframe",
    "strategy", "settings", "app_version", "note", "finish_note",
)

#: Текстовые колонки прогона: те, что приходят снаружи и идут через чистку
#: от секретов. Порядок здесь — порядок полей `SessionRecord`, и имена
#: совпадают с ними намеренно: `open_journal_session` берёт значения
#: по имени, а не по счёту.
_SESSION_TEXT_COLUMNS: tuple[str, ...] = (
    "symbol", "timeframe", "strategy", "settings", "app_version", "note",
)

#: Колонки прогона, которые пишутся при **закрытии**, и только там. Всё
#: остальное, кроме номера, — условия прогона и заморожено (см.
#: `_FROZEN_SESSION_COLUMNS` ниже, он выводится вычитанием).
_CLOSING_SESSION_COLUMNS: tuple[str, ...] = ("finished_at", "finish_note")

#: Колонки прогона, существовавшие в схеме 3. По ним идёт перенос при
#: пересборке таблицы: `finish_note` у старых прогонов взять неоткуда,
#: и она остаётся пустой — прогон закрыли молча, так и записано.
#:
#: ⚠️ Этот список **не выводится** из `_SESSION_COLUMNS` и не должен: он
#: описывает чужую, уже неизменяемую схему. Выведенный, он поехал бы вслед
#: за кодом, и перенос со схемы 3 начал бы читать колонку, которой в базе
#: схемы 3 нет, — то есть миграция сломалась бы на настоящей старой базе,
#: а тесты остались бы зелёными.
_SESSION_COLUMNS_BEFORE_TWO_NOTES: tuple[str, ...] = (
    "id", "origin", "started_at", "finished_at", "symbol", "timeframe",
    "strategy", "settings", "app_version", "note",
)

#: Естественный ключ сделки: одна сделка — одна строка в прогоне. Список
#: один на три места, и они обязаны совпадать: индекс `journal_trade_identity`,
#: сторож `journal_trade_is_written_once` и проверка перед вставкой
#: в `write_trades`. Разойдясь, проверка и сторож дали бы худшее из двух —
#: проверка пропустила бы запись, а сторож отверг бы её вместе со всей пачкой.
#:
#: Почему ключ именно такой, а не по именам заявок — см. комментарий
#: у сторожа.
_TRADE_IDENTITY: tuple[str, ...] = (
    "session_id", "symbol", "side", "entry_ts", "exit_ts",
)

#: «Та же сделка» глазами сторожа: лежащая строка против вставляемой.
_TRADE_IDENTITY_MATCH = "\n              AND ".join(
    f"{name} = new.{name}" for name in _TRADE_IDENTITY
)
#: «Та же сделка» глазами `write_trades`: лежащая строка против поданных
#: значений. Значения подставляются **в том же порядке**, потому что берутся
#: из того же списка (`_trade_values`).
_TRADE_IDENTITY_WHERE = " AND ".join(f"{name} = ?" for name in _TRADE_IDENTITY)


def _session_table(name: str) -> str:
    """Текст `CREATE TABLE` прогонов под нужным именем и с текущим `CHECK`."""
    return _SESSION_TABLE.format(name=name, origins=_sql_values(_ORIGIN_VALUES))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS minute_candle (
    symbol      TEXT    NOT NULL,
    ts          INTEGER NOT NULL,
    open        REAL    NOT NULL,
    high        REAL    NOT NULL,
    low         REAL    NOT NULL,
    close       REAL    NOT NULL,
    volume      REAL    NOT NULL,
    source      TEXT    NOT NULL,
    source_rank INTEGER NOT NULL,
    PRIMARY KEY (symbol, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS data_day (
    symbol       TEXT    NOT NULL,
    day          TEXT    NOT NULL,
    requested_at INTEGER NOT NULL,
    candles      INTEGER NOT NULL,
    settled      INTEGER NOT NULL,
    PRIMARY KEY (symbol, day)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS data_load (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    at              INTEGER NOT NULL,
    symbol          TEXT    NOT NULL,
    source          TEXT    NOT NULL,
    requested_from  TEXT,
    requested_to    TEXT,
    fetched         INTEGER NOT NULL DEFAULT 0,
    inserted        INTEGER NOT NULL DEFAULT 0,
    updated         INTEGER NOT NULL DEFAULT 0,
    kept            INTEGER NOT NULL DEFAULT 0,
    duplicates      INTEGER NOT NULL DEFAULT 0,
    collapsed       INTEGER NOT NULL DEFAULT 0,
    missing_minutes INTEGER NOT NULL DEFAULT 0,
    gap_count       INTEGER NOT NULL DEFAULT 0,
    overnight_gaps  INTEGER NOT NULL DEFAULT 0,
    pages           INTEGER NOT NULL DEFAULT 0,
    requests        INTEGER NOT NULL DEFAULT 0,
    retries         INTEGER NOT NULL DEFAULT 0,
    first_ts        INTEGER,
    last_ts         INTEGER,
    summary         TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS data_gap (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT    NOT NULL,
    start_ts     INTEGER NOT NULL,
    end_ts       INTEGER NOT NULL,
    minutes      INTEGER NOT NULL,
    crosses_date INTEGER NOT NULL,
    detected_at  INTEGER NOT NULL,
    load_id      INTEGER
);

CREATE UNIQUE INDEX IF NOT EXISTS data_gap_unique
    ON data_gap (symbol, start_ts, end_ts);

{session_table};

CREATE TABLE IF NOT EXISTS journal_decision (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES journal_session (id) ON DELETE CASCADE,
    at         INTEGER NOT NULL,
    event      TEXT    NOT NULL,
    reason     TEXT    NOT NULL,
    level      TEXT    NOT NULL CHECK (level IN ({levels}))
);

CREATE INDEX IF NOT EXISTS journal_decision_of_session
    ON journal_decision (session_id, id);

CREATE TABLE IF NOT EXISTS journal_trade (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER NOT NULL REFERENCES journal_session (id) ON DELETE CASCADE,
    symbol          TEXT    NOT NULL,
    side            TEXT    NOT NULL CHECK (side IN ({sides})),
    volume          REAL    NOT NULL,
    entry_ts        INTEGER NOT NULL,
    entry_price     REAL    NOT NULL,
    entry_order_id  TEXT    NOT NULL DEFAULT '',
    exit_ts         INTEGER NOT NULL,
    exit_price      REAL    NOT NULL,
    exit_order_id   TEXT    NOT NULL DEFAULT '',
    exit_reason     TEXT    NOT NULL DEFAULT '',
    gross           REAL    NOT NULL,
    commission      REAL,
    net             REAL,
    ruble_per_point REAL    NOT NULL DEFAULT 1.0,
    written_at      INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS journal_trade_of_session
    ON journal_trade (session_id, id);

-- Опора сторожа, стерегущего неповторимость сделки
-- (`journal_trade_is_written_once` в `_GUARDS` ниже). Сторож спрашивает базу
-- «такая сделка уже записана?» на каждой вставке, и без индекса этот вопрос
-- читал бы все сделки прогона: на прогоне по истории 266 сделок это
-- 266 проходов вместо 266 поисков.
--
-- Индекс **не** уникальный, и это выбор. `CREATE UNIQUE INDEX` на базе,
-- где дубль уже лежит, отказывает при открытии — то есть превращает базу
-- со свечами, журналами и всей историей в нечитаемую целиком, ради правила,
-- которое касается будущих записей. Сторож же останавливает новый дубль
-- и не трогает старый.
CREATE INDEX IF NOT EXISTS journal_trade_identity
    ON journal_trade ({trade_identity});

-- Остановка робота. **Строка на причину, а не строка на остановку**: причин
-- бывает несколько, они не отменяют друг друга и снимаются по одной, самой
-- ранней (`app/port.py::_Halt`, `D-086`). Склеенная в одну строку остановка
-- теряла бы всё, кроме первой причины, и теряла бы молча.
--
-- Порядок появления и порядок снятия — один и тот же, и задан он `id`.
-- Отдельной колонки под порядок нет намеренно: два способа сказать одно
-- и то же разошлись бы, а разошлись бы они в снятии не той причины.
--
-- ⚠️ Сторожа на этой таблице нет, и это не упущение. Журнал — свидетельство,
-- его строку переписывать нельзя; остановка — **текущее состояние**, её
-- строки заводятся и снимаются по делу. Запрет на `UPDATE` не охранял бы
-- ничего: то же самое делается разрешённой парой `DELETE` + `INSERT`.
--
-- Таблица не ссылается на прогон (`journal_session`) намеренно: остановка
-- переживает не только живой ход, но и сам прогон — она снимается рукой
-- человека, а не концом прогона (решение 0045).
CREATE TABLE IF NOT EXISTS robot_halt (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    kind      TEXT    NOT NULL CHECK (kind IN ({halt_kinds})),
    event     TEXT    NOT NULL DEFAULT '',
    reason    TEXT    NOT NULL,
    raised_at INTEGER NOT NULL
);

-- Одна и та же причина второй строкой не ложится. Правило то же, что
-- в памяти порта (`_Halt.add`): опрос счёта повторяет заход каждые полминуты,
-- и без этого база копила бы по сто двадцать одинаковых причин в час.
CREATE UNIQUE INDEX IF NOT EXISTS robot_halt_reason_is_one
    ON robot_halt (reason);

-- Сторожа журнала вынесены в `_GUARDS` (ниже по файлу) и **пересоздаются
-- при каждом открытии базы**: тело сторожа принадлежит коду, а не файлу.
-- `CREATE TRIGGER IF NOT EXISTS` здесь стоял бы напрасно — обезвреженный
-- сторож с тем же именем пережил бы открытие, и это проверено пробой.
""".format(
    session_table=_session_table("journal_session"),
    levels=_sql_values(_LEVEL_VALUES),
    sides=_sql_values(_SIDE_VALUES),
    halt_kinds=_sql_values(_HALT_KIND_VALUES),
    trade_identity=", ".join(_TRADE_IDENTITY),
)


#: Поля прогона, которые не меняются после его открытия. **Выводятся
#: вычитанием**, а не перечисляются: всё, кроме номера и двух колонок
#: закрытия, — условие прогона. Двенадцатая колонка, заведённая завтра,
#: окажется замороженной сама, и это верное умолчание: незамеченная щель
#: в охране дороже лишнего отказа на правке.
#:
#: ⚠️ `note` попал сюда не сразу. Он был единственным правимым текстом
#: прогона: `finish_journal_session(note=…)` писал `SET note = ?` поверх
#: заметки, с которой прогон открыли. Прогон, открытый с «автозапуск,
#: догрузка 3 дня», после Ctrl+C нёс «закрыто по Ctrl+C» — условие открытия
#: исчезало, и восстановить его было нечем: строки журнала неизменяемы,
#: а заметка одна.
#:
#: ⚠️ Заметок стало две, и правимой не осталось ни одной — но это стало
#: правдой только после правки 03.09.2026. До неё `finish_note`
#: у **открытого** прогона правился обычным `UPDATE` сколько угодно раз;
#: см. сторожа `journal_session_final_note_comes_with_the_close`.
_FROZEN_SESSION_COLUMNS: tuple[str, ...] = tuple(
    name
    for name in _SESSION_COLUMNS
    if name != "id" and name not in _CLOSING_SESSION_COLUMNS
)

# Два списка происхождений — две половины одного правила, и оба **выводятся**
# из `RunOrigin.evidence`, а не переписаны сюда строками. Иначе четвёртое
# происхождение однажды заведут, а сюда добавить забудут — и оно окажется
# либо без сторожа, либо без чистки, в зависимости от того, куда не дописали.
#
# Что нельзя повторить — то не удаляется (сторож в базе, ниже).
_EVIDENCE_ORIGINS: tuple[str, ...] = tuple(
    origin.value for origin in RunOrigin if origin.evidence
)
# Что можно повторить командой — то чистка вправе трогать.
_PRUNABLE_ORIGINS: tuple[str, ...] = tuple(
    origin.value for origin in RunOrigin if not origin.evidence
)

#: Сторожа журнала: имя → тело. Пересоздаются при **каждом** открытии базы
#: (`_install_guards`), поэтому тело всегда то, которое написано здесь.
#:
#: ⚠️ Что эти сторожа делают и чего они не делают.
#:
#: **Делают.** Останавливают правку журнала изнутри программы и снаружи —
#: из `sqlite3`, из просмотрщика баз, из чужого скрипта, — пока сторож на
#: месте. Ловят и `INSERT OR REPLACE`, который в SQLite есть DELETE + INSERT
#: (для этого включён `PRAGMA recursive_triggers`, см. `CandleStore.__init__`).
#:
#: **Не делают.** Не защищают от того, у кого есть права на сам файл: сторожа
#: можно снять `DROP TRIGGER`, испортить данные и уйти. Пересоздание при
#: открытии вернёт сторожа, но не данные. Это защита от нашей же ошибки
#: и от неосторожной правки руками, а не от злоумышленника с доступом к диску;
#: от него защищает резервная копия папки, а не база.
_GUARDS: dict[str, str] = {
    # Строка журнала не переписывается задним числом. Никакая: правило не знает
    # происхождения, потому что переписанная строка прогона по истории — это
    # тоже неправда, просто дешевле.
    "journal_decision_is_append_only": """
        CREATE TRIGGER journal_decision_is_append_only
        BEFORE UPDATE ON journal_decision
        BEGIN
            SELECT RAISE(
                ABORT,
                'строку журнала решений переписать нельзя: журнал — свидетельство'
            );
        END
    """,
    "journal_trade_is_append_only": """
        CREATE TRIGGER journal_trade_is_append_only
        BEFORE UPDATE ON journal_trade
        BEGIN
            SELECT RAISE(
                ABORT,
                'строку журнала сделок переписать нельзя: журнал — свидетельство'
            );
        END
    """,
    # Строка живёт ровно столько, сколько её прогон. Условие `WHEN EXISTS`
    # разделяет два удаления, которые для SQLite выглядят одинаково:
    # прямое `DELETE FROM journal_decision` (прогон на месте — отказ)
    # и каскад от удаления прогона (прогона уже нет — пропускаем).
    # Проверено пробой: каскад проходит, прямое удаление и REPLACE — нет.
    "journal_decision_dies_with_its_run": """
        CREATE TRIGGER journal_decision_dies_with_its_run
        BEFORE DELETE ON journal_decision
        WHEN EXISTS (SELECT 1 FROM journal_session WHERE id = old.session_id)
        BEGIN
            SELECT RAISE(
                ABORT,
                'строку журнала решений удалить нельзя: она живёт, пока жив её прогон'
            );
        END
    """,
    "journal_trade_dies_with_its_run": """
        CREATE TRIGGER journal_trade_dies_with_its_run
        BEFORE DELETE ON journal_trade
        WHEN EXISTS (SELECT 1 FROM journal_session WHERE id = old.session_id)
        BEGIN
            SELECT RAISE(
                ABORT,
                'сделку из журнала удалить нельзя: она живёт, пока жив её прогон'
            );
        END
    """,
    # Условия прогона задаются при открытии и дальше не меняются. Иначе отчёт
    # о боевом дне можно было бы задним числом объявить прогоном по истории,
    # переписать инструмент или подменить снимок настроек.
    "journal_session_conditions_are_final": """
        CREATE TRIGGER journal_session_conditions_are_final
        BEFORE UPDATE OF {frozen} ON journal_session
        BEGIN
            SELECT RAISE(
                ABORT,
                'условия прогона не меняются после его открытия — ни одно из {count} полей'
            );
        END
    """.format(
        frozen=", ".join(_FROZEN_SESSION_COLUMNS),
        # Число в сообщении — счётом, а не словом. Слово «семи» уже один раз
        # осталось прежним, когда полей стало восемь.
        count=len(_FROZEN_SESSION_COLUMNS),
    ),
    # Время конца пишется один раз. Второе закрытие того же прогона — это
    # либо ошибка в номере, либо попытка задним числом объявить аварийное
    # завершение штатным.
    "journal_session_finishes_once": """
        CREATE TRIGGER journal_session_finishes_once
        BEFORE UPDATE OF finished_at ON journal_session
        WHEN old.finished_at IS NOT NULL
        BEGIN
            SELECT RAISE(
                ABORT,
                'прогон уже закрыт: время его конца не переписывается'
            );
        END
    """,
    # Заметка о завершении пишется тем же действием, что закрывает прогон,
    # и другого пути к ней нет. Без этого сторожа она осталась бы дырой,
    # через которую текст прогона правится задним числом: `finished_at`
    # уже под охраной, условия открытия заморожены, а `finish_note`
    # правился бы обычным `UPDATE` сколько угодно раз.
    #
    # ⚠️ Условие из двух половин, и вторая появилась после ревью 03.09.2026.
    # С одним лишь `old.finished_at IS NOT NULL` сторож стерёг **закрытый**
    # прогон и пропускал `UPDATE journal_session SET finish_note = '…'`
    # у открытого — сколько угодно раз. Дальше программу убивали, и в журнале
    # оставался прогон, который «не закончился» (`finished_at IS NULL`)
    # и одновременно несёт «закрыто штатно». Поверить пришлось бы одному
    # из двух, а какому — из журнала не видно.
    #
    # `new.finished_at IS NULL` пропускает ровно тот `UPDATE`, который
    # закрывает прогон: там `finished_at` получает время, то есть
    # перестаёт быть пустым. Правка одной заметки закрытия не меняет
    # (`new.finished_at` = `old.finished_at` = NULL) и отвергается.
    # Проверено пробой на всех четырёх сочетаниях.
    "journal_session_final_note_comes_with_the_close": """
        CREATE TRIGGER journal_session_final_note_comes_with_the_close
        BEFORE UPDATE OF finish_note ON journal_session
        WHEN old.finished_at IS NOT NULL OR new.finished_at IS NULL
        BEGIN
            SELECT RAISE(
                ABORT,
                'заметка о завершении пишется один раз, вместе с закрытием прогона'
            );
        END
    """,
    # Дозапись идёт только в **открытый** прогон. Закрытый — законченное
    # свидетельство: строка, приписанная к нему задним числом, ничем
    # не отличается от переписанной, только следов от неё ещё меньше.
    #
    # ⚠️ Чего сторож не делает: он не отличает нашу запись от чужой, пока
    # прогон открыт. Открытый прогон обязан принимать строки — в этом его
    # работа, и правило «дописывать может только движок» на уровне базы
    # не выражается вовсе.
    #
    # Прогона с таким номером может не быть вовсе: тогда подзапрос даёт NULL,
    # условие ложно, и вставку отвергает внешний ключ — с ошибкой про него,
    # а не про закрытый прогон.
    "journal_decision_joins_an_open_run": """
        CREATE TRIGGER journal_decision_joins_an_open_run
        BEFORE INSERT ON journal_decision
        WHEN (
            SELECT finished_at FROM journal_session WHERE id = new.session_id
        ) IS NOT NULL
        BEGIN
            SELECT RAISE(
                ABORT,
                'прогон закрыт: строку журнала решений в него больше не дописать'
            );
        END
    """,
    "journal_trade_joins_an_open_run": """
        CREATE TRIGGER journal_trade_joins_an_open_run
        BEFORE INSERT ON journal_trade
        WHEN (
            SELECT finished_at FROM journal_session WHERE id = new.session_id
        ) IS NOT NULL
        BEGIN
            SELECT RAISE(
                ABORT,
                'прогон закрыт: сделку в него больше не дописать'
            );
        END
    """,
    # Одна сделка — одна строка. Повтор `write_trades` после таймаута (сеть
    # промолчала, вызывающий послал пачку заново) превращал четыре сделки
    # боевого дня в восемь. Журнал при этом формально не переписан — сторожа
    # на `UPDATE` целы, — а «прибыль за день» удвоена.
    #
    # Ключ — **естественный**: прогон, инструмент, сторона, время входа
    # и время выхода. Не `(session_id, entry_order_id, exit_order_id)`:
    # у прогона по истории имена заявок пусты все до одного, и такой ключ
    # схлопнул бы все его сделки в одну — то есть отверг бы вторую же
    # законную запись. Две разные сделки одного прогона совпасть и по входу,
    # и по выходу не могут: позиция в этой системе одна
    # (`backtest.execution`, `self._open: OpenLeg | None`), а значит
    # интервалы сделок не пересекаются.
    "journal_trade_is_written_once": """
        CREATE TRIGGER journal_trade_is_written_once
        BEFORE INSERT ON journal_trade
        WHEN EXISTS (
            SELECT 1 FROM journal_trade
            WHERE {identity}
        )
        BEGIN
            SELECT RAISE(
                ABORT,
                'эта сделка в прогоне уже записана: тот же вход и тот же выход'
            );
        END
    """.format(identity=_TRADE_IDENTITY_MATCH),
    # ⚠️ Второго сторожа — по паре имён заявок — здесь **нет намеренно**,
    # и это не забывчивость: он был написан и снят пробой. Пара имён ловит
    # больше (повтор, собранный заново из ответа брокера, где времена
    # пересчитаны), но ошибается в сторону отказа: любой источник, кладущий
    # в имя заявки заглушку — постоянную строку, «неизвестно», номер попытки, —
    # получает отказ на второй же законной сделке дня, и вся пачка
    # откатывается. Потерянная боевая сделка дороже записанной дважды:
    # дубль виден в отчёте и снимается, пропажа не видна вовсе.
    #
    # Неповторимый прогон не удаляется ничем: ни чисткой, ни прямым `DELETE`,
    # ни `forget_journal_sessions`. Список происхождений **выводится**
    # из `RunOrigin.evidence`, а не написан здесь строкой: правило «нельзя
    # удалять то, что нельзя повторить» живёт в одном месте, иначе четвёртое
    # происхождение однажды заведут, а сюда добавить забудут.
    #
    # ⚠️ Прежде сторож стоял только на боевом прогоне (`origin = 'live'`),
    # хотя `evidence` объявляет неповторимой и симуляцию на боевом потоке.
    # Симуляцию защищала только чистка — то есть код, а код переписывают.
    "journal_session_evidence_is_kept": """
        CREATE TRIGGER journal_session_evidence_is_kept
        BEFORE DELETE ON journal_session
        WHEN old.origin IN ({evidence})
        BEGIN
            SELECT RAISE(
                ABORT,
                'прогон не удаляется: повторить его нельзя — настоящие деньги или живой рынок'
            );
        END
    """.format(evidence=_sql_values(_EVIDENCE_ORIGINS)),
}

#: Сторожа, которых больше нет под этими именами. Перечислены, чтобы снять их
#: с баз, открытых прежней сборкой: `_install_guards` пересоздаёт только тех,
#: кто есть в `_GUARDS`, и снятый с довольствия триггер остался бы в файле
#: навсегда — невидимым для кода, непроверяемым и уже неверным.
#:
#: `journal_session_live_is_kept` заменён на `journal_session_evidence_is_kept`:
#: тот стерёг одно происхождение из двух неповторимых.
_RETIRED_GUARDS: tuple[str, ...] = ("journal_session_live_is_kept",)

#: Прежние тела наших же сторожей: имя → тело, которое ставила прошлая сборка.
#: Служат ровно одному — **не поднимать тревогу на собственной правке**.
#: `replaced_guards` отвечает на вопрос «базу правили мимо программы?»,
#: и подмена, случившаяся от обновления программы, обесценила бы ответ:
#: поле было бы полным при первом открытии каждой старой базы.
#:
#: Тело всё равно заменяется на нынешнее — прощается тревога, а не сторож.
#: Цена известна и принята: тот, кто вернёт в базу **прежнее** тело сторожа,
#: останется неназванным. Прежнее тело — наше и слабее нынешнего, а нынешнее
#: встанет на место при первом же открытии, так что выигрыш от такой подмены
#: живёт до перезапуска программы.
_SUPERSEDED_GUARDS: dict[str, tuple[str, ...]] = {
    # Семь замороженных полей вместо восьми: `note` встал под охрану вместе
    # со второй заметкой (схема 4), а число в сообщении было словом.
    # Тело — дословно из базы, созданной сборкой `4337561`; перенос строки
    # в списке колонок для SQL ничего не меняет, сравнение (`_same_sql`)
    # к пробелам нечувствительно.
    "journal_session_conditions_are_final": (
        """
        CREATE TRIGGER journal_session_conditions_are_final
        BEFORE UPDATE OF origin, started_at, symbol, timeframe, strategy,
                         settings, app_version ON journal_session
        BEGIN
            SELECT RAISE(
                ABORT,
                'условия прогона не меняются после его открытия — ни одно из семи полей'
            );
        END
    """,
    ),
    # Стерёг только закрытый прогон; у открытого заметка о завершении
    # правилась обычным `UPDATE` (ревью 03.09.2026).
    "journal_session_final_note_comes_with_the_close": (
        """
        CREATE TRIGGER journal_session_final_note_comes_with_the_close
        BEFORE UPDATE OF finish_note ON journal_session
        WHEN old.finished_at IS NOT NULL
        BEGIN
            SELECT RAISE(
                ABORT,
                'прогон уже закрыт: заметка о завершении пишется один раз, вместе с закрытием'
            );
        END
    """,
    ),
}


def _same_sql(stored: str | None, wanted: str) -> bool:
    """Одно ли это тело сторожа. Сравнение без оглядки на пробелы и переносы.

    `sqlite_master` хранит текст `CREATE TRIGGER` ровно таким, каким его подали,
    а подаём мы его с отступами исходника. Сравнивать посимвольно значило бы
    пересоздавать всех сторожей при каждом открытии и никогда не узнать, что
    один из них правда подменён.
    """
    if stored is None:
        return False
    return " ".join(stored.split()) == " ".join(wanted.split())


def _ours_before(name: str, stored: str) -> bool:
    """Это тело сторожа ставила прошлая сборка программы?

    Отделяет собственную правку от подмены снаружи. Без этого поле
    `replaced_guards` заполнялось бы при первом открытии каждой базы,
    созданной прошлой сборкой, — то есть перестало бы значить что-либо
    ровно там, где на него смотрят.
    """
    return any(_same_sql(stored, body) for body in _SUPERSEDED_GUARDS.get(name, ()))


def _to_ts(moment: datetime) -> int:
    return int(ensure_msk(moment).timestamp())


def _minute_ts(moment: datetime) -> int:
    """Ключ минутной свечи: секунды отброшены (см. «Время» в шапке модуля)."""
    return int(floor_to_minute(moment).timestamp())


def _from_ts(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc).astimezone(MSK)


#: Прогоны по истории, кроме последних `?` штук. Подзапрос, а не список
#: номеров: чистка бывает длинной, а `IN (?, ?, …)` на тысячу прогонов упрётся
#: в предел числа параметров SQLite.
_OLD_BACKTEST_SESSIONS = (
    "SELECT id FROM journal_session "
    f"WHERE origin IN ({', '.join('?' * len(_PRUNABLE_ORIGINS))}) "
    "ORDER BY id DESC LIMIT -1 OFFSET ?"
)


def _halt_of(row: tuple[Any, ...]) -> StoredHalt:
    """Собрать причину остановки из строки таблицы. Порядок задан запросом."""
    return StoredHalt(
        id=int(row[0]),
        kind=HaltKind(str(row[1])),
        event=str(row[2]),
        reason=str(row[3]),
        raised_at=_from_ts(int(row[4])),
    )


def _session_of(row: tuple[Any, ...]) -> JournalSession:
    """Собрать прогон из строки таблицы. Порядок колонок задан запросом.

    Тип строки — `tuple[Any, ...]`, потому что ровно это отдаёт `sqlite3`:
    колонка типизирована схемой базы, а не питоном. Приведение к нужному типу
    делается здесь явными `int`/`str`, и это единственное место в слое, где
    порядок колонок журнала знает не запрос, а код.
    """
    return JournalSession(
        id=int(row[0]),
        origin=RunOrigin(str(row[1])),
        started_at=_from_ts(int(row[2])),
        finished_at=None if row[3] is None else _from_ts(int(row[3])),
        symbol=str(row[4]),
        timeframe=str(row[5]),
        strategy=str(row[6]),
        settings=str(row[7]),
        app_version=str(row[8]),
        note=str(row[9]),
        finish_note=str(row[10]),
    )


#: Колонки прогона для чтения — в том же порядке, которого ждёт `_session_of`.
#: Собирается из `_SESSION_COLUMNS`, а не пишется отдельной строкой: два
#: списка одних и тех же колонок разошлись бы молча.
_SESSION_SELECT = ", ".join(_SESSION_COLUMNS)


def _limit_probe(limit: int) -> int:
    """Сколько строк запросить, чтобы узнать, была ли обрезка. На одну больше.

    Лишняя строка — единственный способ отличить «строк ровно столько»
    от «строк больше, остальные отрезаны», не считая их вторым запросом.
    Она читается и выбрасывается: цена — одна строка на чтение.

    :raises ValueError: лимит меньше единицы. `LIMIT 0` вернул бы пустую
        выдачу, а `LIMIT -1` в SQLite означает «без предела» — два разных
        молчаливых ответа на одну опечатку.
    """
    if limit < 1:
        raise ValueError(
            f"лимит выдачи журнала {limit} меньше единицы: "
            "чтение без строк — не чтение, а ноль в SQLite означает не то же, "
            "что минус один"
        )
    return limit + 1


def _journal_filter(
    *,
    session_id: int | None,
    origin: RunOrigin | None,
    since: datetime | None,
    until: datetime | None,
    column: str,
) -> tuple[list[str], list[object]]:
    """Условия отбора строк журнала и их значения.

    Границы по времени полуоткрыты — `[since, until)`, как у минуток: так
    соседние периоды стыкуются без пересечения и без пропуска одной строки
    на границе.
    """
    where: list[str] = []
    args: list[object] = []
    if session_id is not None:
        where.append("s.id = ?")
        args.append(session_id)
    if origin is not None:
        where.append("s.origin = ?")
        args.append(origin.value)
    if since is not None:
        where.append(f"{column} >= ?")
        args.append(_to_ts(since))
    if until is not None:
        where.append(f"{column} < ?")
        args.append(_to_ts(until))
    return where, args


class CandleStore:
    """Единственный файл-база со свечами.

    Открывается как контекстный менеджер либо закрывается явно.
    Файл создаётся сам при первом открытии — установщика у продукта нет
    (решение 0001), база обязана появляться на пустом месте.

    ⚠️ **Соединение принадлежит потоку, который создал этот объект.**
    Обращение из другого потока — `sqlite3.ProgrammingError` с прямым текстом
    про поток, и это лучший из возможных отказов: он случается на первом же
    вызове и называет причину. `check_same_thread=False` отвергнут замером
    (решение 0005): он снимает проверку принадлежности и оставляет гонку
    транзакций из `_transaction`, то есть меняет понятный отказ на редкий
    `cannot start a transaction within a transaction`.

    Из окна и из движка база открывается не напрямую, а через
    `market.worker.MarketWorker` — однопоточный фасад, который создаёт
    `CandleStore` внутри своего потока.
    """

    def __init__(
        self,
        path: pathlib.Path | str,
        *,
        sanitize: Callable[[str], str] | None = None,
    ) -> None:
        """Открыть базу. Файл создаётся сам, если его ещё нет.

        :param sanitize: чистка текста, уходящего в журналы и в отчёт
            о загрузке. Умолчание — `market.journal.redact`: она знает только
            формы токена (`Bearer …`, поля `*_token`, голый JWT) и не знает
            живых значений. `app/` обязан передать сюда
            `broker.redaction.scrub` — у него есть оба рубежа.

            Через чистку идёт **весь** текст, который слой кладёт на диск:
            все текстовые колонки обоих журналов, включая обе заметки прогона,
            и строка отчёта о загрузке.
            Список «сюда снаружи ничего не приходит» вести нельзя — он устареет
            молча: `LoadReport.note` уже собирается из текста исключения
            (`market.sync`), а `entry_order_id` заполнится ответом брокера
            на Э1-5. Единственные нетронутые поля — числа и время.
        """
        self.path = pathlib.Path(path)
        #: Сколько строк починила миграция ключей при открытии. Ноль — норма.
        #: Число не прячется: база, в которой минутка лежала с секундами,
        #: до починки роняла чтение баров целиком.
        self.migrated_minutes = 0
        #: Сколько минут догрузки сняла миграция 4 → 5. Ноль — норма: путь
        #: догрузки у брокера заработал 05.09.2026, и в большинстве баз таких
        #: строк нет вовсе. Число не прячется: снятая минута — это дыра
        #: до следующего захода догрузки, и о ней надо сказать.
        self.dropped_minutes = 0
        #: Что убрала чистка журнала при открытии последнего прогона.
        #: Пустой отчёт — норма; см. `open_journal_session`.
        self.pruned_journal = PruneReport()
        #: Номера строк, которые последний `write_trades` **не** записал:
        #: такие сделки в журнале уже были. Пусто — норма. Не пусто означает,
        #: что пачку подали повторно, и число здесь — единственный след этого:
        #: длина ответа `write_trades` не меняется, в ней и повторы тоже.
        self.repeated_trades: tuple[int, ...] = ()
        self._sanitize = sanitize if sanitize is not None else redact
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), isolation_level=None)
        # ⚠️ Отказ ниже — «база новее программы» в `_check_version`, отказ
        # пересборки в `_rebuild_journal_session` — оставлял соединение
        # открытым: объект не создан, `close()` не позовут никогда. На Windows
        # открытый дескриптор не даёт переименовать и удалить файл базы —
        # то есть мешает ровно той ручной починке, которая после отказа
        # остаётся единственной (ревью 03.09.2026).
        try:
            self._db.execute("PRAGMA foreign_keys = ON")
            # WAL + FULL: свечи и журналы пишутся раз в минуту, скорость здесь
            # ничего не решает, а «записи не теряются при перезапуске» — пункт
            # приёмки. Дешёвая долговечность против дорогого расследования.
            if str(self.path) != ":memory:":
                self._db.execute("PRAGMA journal_mode = WAL")
            self._db.execute("PRAGMA synchronous = FULL")
            # ⚠️ Обязателен для сторожей журнала, а не для удобства.
            # `INSERT OR REPLACE` в SQLite — это DELETE + INSERT, и без этого
            # флага неявный DELETE **не поднимает** `BEFORE DELETE`: строка
            # журнала подменяется молча. Проверено пробой в обе стороны.
            self._db.execute("PRAGMA recursive_triggers = ON")
            self._db.executescript(_SCHEMA)
            # ⚠️ Миграции идут **до** установки сторожей, а не после. Пересборка
            # таблицы прогонов (3 → 4) сносит вместе со старой таблицей и её
            # триггеры; поставленные до неё сторожа исчезли бы молча, и база
            # осталась бы открытой без охраны до следующего запуска.
            taken_down = self._check_version()
            #: Сторожа, чьё тело в базе разошлось с телом в коде и было заменено
            #: при открытии. Пусто — норма. Не пусто означает, что базу правили
            #: мимо программы: сторожа вернули, данные — нет.
            self.replaced_guards: tuple[str, ...] = self._install_guards(taken_down)
        except BaseException:
            self._db.close()
            raise

    # -- служебное ---------------------------------------------------------

    @classmethod
    def open_default(cls) -> "CandleStore":
        """База в папке `userdata/` рядом с программой (решение 0003)."""
        ensure_userdata_dir()
        return cls(default_db_path())

    def _install_guards(self, taken_down: Mapping[str, str]) -> tuple[str, ...]:
        """Поставить сторожей журнала заново. Возвращает имена расходившихся.

        Пересоздание, а не `CREATE TRIGGER IF NOT EXISTS`: тело сторожа
        принадлежит коду, а не файлу. Проверено пробой — сторож, подменённый
        снаружи на пустой (`BEGIN SELECT 1; END`) и оставленный под тем же
        именем, переживал открытие программой, и журнал после этого правился
        обычным `UPDATE`.

        Что это **не** даёт: защиты от того, у кого есть права на файл. Он
        снимет сторожа, испортит данные и уйдёт; пересоздание вернёт сторожа,
        но не данные. Единственная защита от такого — резервная копия папки.

        :param taken_down: сторожа, снятые миграцией **до** этого вызова,
            имя → тело, каким оно лежало в файле. Пересборка таблицы
            прогонов снимает пятерых из семи сторожей схемы 3 раньше, чем
            их прочитает этот метод, — и подменённый снаружи сторож исчезал
            бы прежде, чем его заметят: `replaced_guards` пустел ровно на том
            открытии, где подмена свежая (ревью 03.09.2026). Снятое тело
            судится тем же разбором, что и лежащее в базе. Пересоздаётся
            сторож при этом всегда: в файле его уже нет.
        """
        found = {
            str(name): str(body or "")
            for name, body in self._db.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"
            )
        }
        # Сторожа, отменённые новой сборкой, снимаются молча и в
        # `replaced_guards` не попадают: это наша собственная смена схемы,
        # а поле `replaced_guards` отвечает на другой вопрос — «базу правили
        # мимо программы?». Тревога от собственной миграции обесценила бы его.
        for retired in _RETIRED_GUARDS:
            if retired in found:
                self._db.execute(f"DROP TRIGGER {retired}")
        # Тело, каким сторожа застало открытие: лежащее в базе, а если
        # миграция его уже сняла — снятое.
        at_open = {**taken_down, **found}
        replaced: list[str] = []
        for name, body in _GUARDS.items():
            stored = found.get(name)
            if stored is not None and _same_sql(stored, body):
                continue
            before = at_open.get(name)
            if (
                before is not None
                and not _same_sql(before, body)
                and not _ours_before(name, before)
            ):
                replaced.append(name)
            self._db.execute(f"DROP TRIGGER IF EXISTS {name}")
            self._db.execute(body)
        return tuple(replaced)

    def _check_version(self) -> dict[str, str]:
        """Довести схему базы до текущей. Возвращает сторожей, снятых миграцией.

        Снятые сторожа (имя → тело из файла) идут в `_install_guards`: без
        них подмена, сделанная до обновления программы, исчезала бы вместе
        с пересборкой таблицы и в `replaced_guards` не попадала.
        """
        version = int(self._db.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"база {self.path} создана более новой версией программы "
                f"(схема {version} против {SCHEMA_VERSION}). Открывать её нельзя: "
                "старый код перезапишет то, чего не понимает"
            )
        if version == SCHEMA_VERSION:
            return {}
        # Миграции идут цепочкой, каждая от своей отметки. Раньше здесь стояло
        # безусловное «сделать всё»: пока версия была одна, это совпадало
        # с правильным поведением, а со второй миграцией начало означать
        # полный проход по `minute_candle` при каждом открытии базы схемы 2.
        #
        # Ноль — либо пустая база, либо созданная до появления версии; в обоих
        # случаях миграция ключей безвредна: на чистой базе она не находит строк.
        if version < _SCHEMA_WITH_MINUTE_KEYS:
            self._add_missing_columns()
            self.migrated_minutes = self._migrate_minute_keys()
        # 2 → 3: журналы. Таблицы и сторожа созданы `_SCHEMA` выше — переносить
        # нечего, до этой версии журналов в базе не существовало.
        taken_down: dict[str, str] = {}
        if version < _SCHEMA_WITH_TWO_NOTES:
            taken_down = self._rebuild_journal_session()
        # 4 → 5: в `volume` теперь контракты у всех источников. Ноль здесь —
        # обычное дело: догрузка у брокера успела записать что-то лишь в тех
        # базах, где она вообще отработала (путь заработал 05.09.2026).
        if version < _SCHEMA_WITH_CONTRACT_VOLUME:
            self.dropped_minutes = self._drop_provisional_minutes()
        self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        return taken_down

    def _add_missing_columns(self) -> None:
        """Дописать колонки журнала, появившиеся позже самой таблицы.

        `CREATE TABLE IF NOT EXISTS` существующую таблицу не трогает: в базе
        прежней сборки колонки просто нет, и запись отчёта падает на вставке.
        """
        existing = {
            str(row[1])
            for row in self._db.execute("PRAGMA table_info(data_load)")
        }
        for column in ("collapsed", "overnight_gaps"):
            if column not in existing:
                self._db.execute(
                    f"ALTER TABLE data_load ADD COLUMN {column} "
                    "INTEGER NOT NULL DEFAULT 0"
                )

    def _drop_provisional_minutes(self) -> int:
        """Снять минуты догрузки у брокера: в них оборот, а не контракты.

        Почему снять, а не пересчитать
        ------------------------------
        Пересчёт — это деление на цену **и** на множитель «рублей
        за пункт». Цены в строке есть, множителя нет: он живёт на бирже
        и плавает (решение 0041), а ходить в сеть при открытии базы нельзя —
        программа не откроется без интернета. Пересчёт «по цене и единице»
        был бы верен для фьючерса на индекс и молча неверен для РТС и Брента,
        то есть завёл бы вторую тихую ошибку вместо первой.

        Почему это безопасно
        --------------------
        Ранг `broker_history` — ноль: такая строка по построению временная,
        она стоит на месте дыры до первой заливки тех же минут с биржи.
        Снятая строка возвращает дыру, а дыру догрузка ищет заново при каждом
        подключении (`market.backfill.holes`) и закрывает — теперь уже
        контрактами.

        ⚠️ Чего это НЕ закрывает: дыра старше окна догрузки (`MAX_SPAN`,
        четверо суток) сама не закроется — её закрывает только загрузка
        истории с биржи. На 05.09.2026 таких строк быть не может: путь
        догрузки у брокера впервые ответил в этот же день.
        """
        cursor = self._db.execute(
            "DELETE FROM minute_candle WHERE source = ?",
            (Source.BROKER_HISTORY.value,),
        )
        # `rowcount` у SQLite после `DELETE` — число снятых строк; −1 означает
        # «неизвестно» и в норме здесь не встречается. Ноль наружу честнее
        # минус единицы: строка «снято −1 минут» не значит ничего.
        return max(cursor.rowcount, 0)

    def _rebuild_journal_session(self) -> dict[str, str]:
        """Пересобрать таблицу прогонов: вторая заметка и `CHECK` из кода.

        Возвращает сторожей, снятых перед пересборкой (имя → тело из файла),
        для `_install_guards`. Пустой словарь — пересборка не понадобилась.

        Зачем пересборка, а не `ALTER TABLE ADD COLUMN`
        ----------------------------------------------
        Колонку `finish_note` дописать можно, а `CHECK (origin IN …)` —
        нельзя: в SQLite ограничение таблицы не правится ничем, кроме
        пересборки. На базе схемы 3 список происхождений остался бы
        написанным вручную — ровно тем, от чего уходили: четвёртое
        происхождение завели бы в `RunOrigin`, оба следствия подхватили бы
        его сами, а база молча отвергала бы вставку.

        Порядок — 12 шагов из документации SQLite, и первый из них важнее
        остальных: **внешние ключи выключаются до начала**. С включёнными
        `DROP TABLE journal_session` выполняет неявный `DELETE FROM`,
        а у строк журнала стоит `ON DELETE CASCADE` — то есть миграция
        унесла бы весь журнал решений и сделок. `PRAGMA foreign_keys`
        внутри транзакции не действует, поэтому переключение снаружи.

        Триггеры старой таблицы исчезают вместе с ней; сторожа ставятся
        заново после миграции (`__init__`), а тела, снятые перед ней,
        передаются туда же. Иначе `replaced_guards` — единственный след
        правки базы снаружи — пустел бы ровно на том открытии, где подмена
        свежая: пересборка сносила сторожа раньше, чем его успевали
        прочитать (ревью 03.09.2026). Собственная смена тела за подмену
        не выдаётся: прежние тела перечислены в `_SUPERSEDED_GUARDS`.

        Пересборка **ничего не меняет в журнале**: ни числа строк в трёх
        таблицах, ни счётчика номеров прогонов. Снимок берётся до и после
        (`_journal_snapshot`, `_refuse_if_the_journal_changed`); расхождение —
        отказ и откат всей транзакции. Счётчик при этом приходится переносить
        руками (`_carry_the_run_counter`): `DROP TABLE` уносит его вместе
        со старой таблицей.

        ⚠️ **Чужие сторожа снимаются до пересборки, и без этого миграция
        не работала вовсе.** Найдено ревью 03.09.2026, воспроизведено
        на настоящей базе от коммита `4337561`, а не на фикстуре.

        `DROP TABLE journal_session` уносит триггеры **самой** таблицы.
        Но два сторожа стоят на ДРУГИХ таблицах и ссылаются на эту в своём
        `WHEN`: `journal_decision_dies_with_its_run`
        и `journal_trade_dies_with_its_run`. Они остаются и указывают
        на несуществующую таблицу. Следующей строкой идёт
        `ALTER TABLE … RENAME`, а SQLite начиная с 3.25 при переименовании
        **переразбирает все триггеры схемы**, чтобы переписать в них ссылки.
        Разбор упирается в отсутствующую таблицу::

            sqlite3.OperationalError: error in trigger
            journal_decision_dies_with_its_run: no such table: main.journal_session

        Исключение поднимается из `CandleStore.__init__`, то есть **программа
        не открывает свою базу вообще, при каждом запуске**. Данные целы,
        но взять их нечем. Это ровно тот исход, ради недопущения которого
        выше отвергнут `CREATE UNIQUE INDEX`, — и та же ошибка, повторённая
        собственной миграцией.

        Снимаются все триггеры, чьё тело ссылается на `journal_session`,
        а не список по именам: имена меняются (`_RETIRED_GUARDS` тому
        свидетель), а условие «ссылается на пересобираемую таблицу» —
        нет. Все они пересоздаются после миграции в `_install_guards`.
        """
        columns = {
            str(row[1])
            for row in self._db.execute("PRAGMA table_info(journal_session)")
        }
        if "finish_note" in columns:
            return {}
        kept = ", ".join(_SESSION_COLUMNS_BEFORE_TWO_NOTES)
        self._db.execute("PRAGMA foreign_keys = OFF")
        try:
            with self._transaction():
                before = self._journal_snapshot()
                taken_down = self._drop_guards_referring_to_session()
                self._db.execute(_session_table("journal_session_new"))
                self._db.execute(
                    f"INSERT INTO journal_session_new ({kept}) "
                    f"SELECT {kept} FROM journal_session"
                )
                self._db.execute("DROP TABLE journal_session")
                self._db.execute(
                    "ALTER TABLE journal_session_new RENAME TO journal_session"
                )
                self._carry_the_run_counter(before[_RUN_COUNTER])
                self._refuse_if_the_journal_changed(before)
        finally:
            self._db.execute("PRAGMA foreign_keys = ON")
        return taken_down

    def _journal_snapshot(self) -> dict[str, int]:
        """Число строк в таблицах журнала и счётчик номеров прогонов.

        Опора проверки пересборки: до и после обязано совпасть всё. Счётчик
        без строки в `sqlite_sequence` (в таблицу ещё не писали) считается
        нулём — столько же ставит и сама SQLite при первой вставке.
        """
        snapshot = {
            table: int(self._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in _JOURNAL_TABLES
        }
        snapshot[_RUN_COUNTER] = int(
            self._db.execute(
                "SELECT COALESCE("
                "(SELECT seq FROM sqlite_sequence WHERE name = 'journal_session'), 0)"
            ).fetchone()[0]
        )
        return snapshot

    def _carry_the_run_counter(self, seq: int) -> None:
        """Вернуть счётчик номеров прогонов, который `DROP TABLE` унёс.

        `AUTOINCREMENT` существует ровно затем, чтобы номер **никогда**
        не выдавался повторно, и держится на строке `sqlite_sequence`.
        `DROP TABLE` эту строку удаляет, а `INSERT … SELECT` в новую таблицу
        ставит счётчик по наибольшему **живому** номеру. Проверено пробой
        (SQLite 3.45): счётчик 3 при прогонах [1, 2] после пересборки
        становился 2, и следующий прогон получал номер удалённого. Состояние
        «счётчик выше наибольшего номера» — обычное: чистка прогонов
        по истории удаляет старые по замыслу. Для журнала-свидетельства два
        разных «прогона №3» — двусмысленность в каждой внешней ссылке
        на номер: в выгруженном отчёте, в заметке, в вопросе в поддержку.

        Строка пишется заново, а не правится: у `sqlite_sequence` нет
        ограничения уникальности, `UPDATE` по отсутствующей строке промолчал
        бы, а вторая строка с тем же именем сделала бы счётчик неопределённым.
        Правка `sqlite_sequence` обычными `DELETE` и `INSERT` документирована
        SQLite. Что перенос удался, проверяет тот же снимок, что и строки
        (`_refuse_if_the_journal_changed`).
        """
        self._db.execute("DELETE FROM sqlite_sequence WHERE name = 'journal_session'")
        self._db.execute(
            "INSERT INTO sqlite_sequence (name, seq) VALUES ('journal_session', ?)",
            (seq,),
        )

    def _refuse_if_the_journal_changed(self, before: Mapping[str, int]) -> None:
        """Отказать, если снимок журнала после пересборки не совпал со снимком до.

        Почему счёт строк, а не `PRAGMA foreign_key_check` (шаг 10 процедуры
        из документации SQLite). Он здесь **не ловит ничего**, и это найдено
        ревью 03.09.2026 прогоном, а не рассуждением. Перенос идёт
        `INSERT … SELECT` с сохранением номеров, и висячую ссылку миграция
        создать не способна. А беда, ради которой проверка ставилась, сирот
        не оставляет вовсе: если выключение внешних ключей не подействовало,
        `DROP TABLE` уносит журнал **каскадом** — строки удаляются, а не
        остаются без прогона. Сирот ноль, проверка проходит, транзакция
        фиксируется, журнал уничтожен. Мутация `OFF` → `ON`: проверка
        молчала, падал только тест.

        Сработать `foreign_key_check` мог только на **чужих** сиротах:
        владелец удалил прогон по истории сторонним просмотрщиком, где
        внешние ключи по умолчанию выключены, и строки прогона остались.
        Такая база читается нормально — чтение идёт через `JOIN`, сироты
        невидимы, — а после обновления программы не открылась бы **никогда**,
        при каждом запуске, без пути починки. Текст отказа при этом врал бы:
        сироты лежали там до миграции.

        Снимок свободен от обоих изъянов: он сравнивает базу с ней же самой
        до и после, ловит и каскад, и потерянный прогон, и сбитый счётчик
        номеров, а к прошлому базы безразличен. Отказ поднимается **внутри**
        транзакции: миграция откатывается целиком, база остаётся схемы 3
        и открывается прежней сборкой. Требование `/risk` от 03.09.2026 —
        проверка после пересборки — выполняется этим, а не шагом 10.
        """
        after = self._journal_snapshot()
        changed = [
            f"{key} {before[key]} → {after[key]}"
            for key in before
            if after[key] != before[key]
        ]
        if changed:
            raise RuntimeError(
                "пересборка таблицы прогонов изменила бы журнал, "
                "поэтому отменена: " + ", ".join(changed)
            )

    def _drop_guards_referring_to_session(self) -> dict[str, str]:
        """Снять сторожей, чьё тело ссылается на пересобираемую таблицу.

        Возвращает снятых, имя → тело из файла: по нему `_install_guards`
        судит, не подменяли ли сторожа снаружи, — сам он снятого уже
        не увидит. Отдельным методом, а не строкой внутри миграции: у операции
        своя причина и своя цена, и обе объяснены в `_rebuild_journal_session`.
        """
        taken_down = {
            str(name): str(body or "")
            for name, body in self._db.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' "
                "AND sql LIKE '%journal_session%'"
            )
        }
        for name in taken_down:
            self._db.execute(f'DROP TRIGGER IF EXISTS "{name}"')
        return taken_down

    def _migrate_minute_keys(self) -> int:
        """Свести ключи со секундами к началу минуты. Возвращает число строк.

        Схема 1 хранила `ts` таким, каким его прислал источник. Поток брокера
        мог положить `10:05:07` рядом с биржевой `10:05:00` — две строки на одну
        минуту. При чтении такая пара роняет `bars()` целиком (`on_duplicate`
        в сборке — отказ), то есть график пуст и источник свечей не работает.

        **Правило выбора при слипании то же, что при записи:** выше ранг
        источника; при равном ранге побеждает более поздняя исходная отметка.
        Порядок строк в таблице на результат не влияет.
        """
        rows = self._db.execute(
            "SELECT symbol, ts, open, high, low, close, volume, source, source_rank "
            "FROM minute_candle WHERE ts % 60 <> 0"
        ).fetchall()
        if not rows:
            return 0

        best: dict[tuple[str, int], tuple[object, ...]] = {}
        for row in rows:
            symbol, ts = str(row[0]), int(row[1])
            key = (symbol, ts - ts % 60)
            current = best.get(key)
            if current is None or (int(row[8]), ts) >= (int(current[8]), int(current[1])):
                best[key] = row

        with self._transaction():
            self._db.execute("DELETE FROM minute_candle WHERE ts % 60 <> 0")
            self._db.executemany(
                """
                INSERT INTO minute_candle
                    (symbol, ts, open, high, low, close, volume, source, source_rank)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, ts) DO UPDATE SET
                    open = excluded.open,
                    high = excluded.high,
                    low = excluded.low,
                    close = excluded.close,
                    volume = excluded.volume,
                    source = excluded.source,
                    source_rank = excluded.source_rank
                WHERE excluded.source_rank >= minute_candle.source_rank
                """,
                [
                    (symbol, minute, row[2], row[3], row[4], row[5], row[6], row[7], row[8])
                    for (symbol, minute), row in best.items()
                ],
            )
        return len(rows)

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """Одна транзакция на пачку записей.

        Соединение открыто в режиме автокоммита, и без явной транзакции
        каждая строка `executemany` фиксируется отдельно — при
        `synchronous = FULL` это отдельный сброс на диск на каждую свечу.
        Замер: 20 000 минуток вставлялись 22 секунды вместо десятых долей.

        ⚠️ Голый `BEGIN` требует **единственного владельца соединения**.
        Транзакция здесь принадлежит соединению, а не вызову: два потока
        на одном соединении дают `OperationalError: cannot start a transaction
        within a transaction` — замерено (решение 0005). Владелец обеспечен
        не соглашением, а тем, что соединение создаётся внутри потока данных
        (`market.worker`) и наружу не отдаётся.

        ⚠️ `COMMIT` стоит **внутри** `try`. Снаружи он оставлял соединение
        внутри транзакции при своём собственном отказе (диск полон, база
        занята): следующий `_transaction()` получал `cannot start a transaction
        within a transaction` — сообщение, объявленное признаком нарушения
        потоковой модели. Расследование уходило в потоки, а причина была
        на диске.
        """
        self._db.execute("BEGIN")
        try:
            yield
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "CandleStore":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- запись ------------------------------------------------------------

    def put_minutes(self, symbol: str, candles: Iterable[Candle], source: Source) -> WriteStats:
        """Записать минутные свечи. Дубли и конфликт источников — по правилу выше.

        Одна минута — один ключ, **независимо от секунд** в отметке источника:
        `10:05:00` от биржи и `10:05:07` из потока брокера это одна минутка.

        Что происходит с повтором внутри пачки
        --------------------------------------
        **Побеждает последняя свеча минуты. Целиком — вместе с `high`, `low`
        и объёмом.** Ничего не складывается и не пересчитывается.

        Это правильно ровно для одного вида входа: **снимков минуты нарастающим
        итогом**, когда каждый следующий снимок уже содержит всё, что было
        в минуте раньше. Так шлёт биржа (одна минутка на минуту) и так обычно
        устроен поток свечей у брокера.

        ⚠️ Если источник шлёт **сделки** (тики), правило неверно: четыре тика
        минуты с объёмом 1 дадут в базе объём 1 вместо 4, а `high`/`low` станут
        экстремумами последнего тика, а не минуты. Тейк проверяется касанием
        `high >= уровень`, и по испорченному `high` он не сработает в прогоне —
        зато сработает в бою. Семантика потока БКС на 31.08.2026 не подтверждена
        по документации брокера; долг записан в `ROADMAP` (задача Э1-5).
        Пока она не подтверждена, сюда подаются **свечи**, а не тики.

        Сколько свечей схлопнулось, видно в `WriteStats.collapsed` и в строке
        журнала загрузки.

        :raises ValueError: свеча не минутная либо собранный нами ряд (`@…`)
            кладут в рабочую базу программы — см. `market.synthetic`.
        """
        refuse_synthetic_in_working_base(symbol, self.path)
        batch: dict[int, Candle] = {}
        received = 0
        for candle in candles:
            if candle.timeframe.minutes != 1:
                raise ValueError(
                    f"в хранилище кладутся только минутные свечи, а не {candle.timeframe.name}: "
                    "собранные таймфреймы не хранятся принципиально"
                )
            received += 1
            batch[_minute_ts(candle.time)] = candle
        collapsed = received - len(batch)
        if not batch:
            return WriteStats()

        rank = source.rank
        low, high = min(batch), max(batch)
        existing: dict[int, int] = {
            int(ts): int(existing_rank)
            for ts, existing_rank in self._db.execute(
                "SELECT ts, source_rank FROM minute_candle "
                "WHERE symbol = ? AND ts BETWEEN ? AND ?",
                (symbol, low, high),
            )
        }

        inserted = updated = kept = 0
        rows: list[tuple[object, ...]] = []
        for ts, candle in batch.items():
            previous = existing.get(ts)
            if previous is None:
                inserted += 1
            elif rank >= previous:
                updated += 1
            else:
                kept += 1
                continue
            rows.append(
                (
                    symbol,
                    ts,
                    candle.open,
                    candle.high,
                    candle.low,
                    candle.close,
                    candle.volume,
                    source.value,
                    rank,
                )
            )

        with self._transaction():
            self._db.executemany(
                """
                INSERT INTO minute_candle
                    (symbol, ts, open, high, low, close, volume, source, source_rank)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, ts) DO UPDATE SET
                    open = excluded.open,
                    high = excluded.high,
                    low = excluded.low,
                    close = excluded.close,
                    volume = excluded.volume,
                    source = excluded.source,
                    source_rank = excluded.source_rank
                WHERE excluded.source_rank >= minute_candle.source_rank
                """,
                rows,
            )
        return WriteStats(
            inserted=inserted, updated=updated, kept=kept, collapsed=collapsed
        )

    # -- учёт запрошенных дней ---------------------------------------------

    def mark_days_requested(
        self,
        symbol: str,
        days: Iterable[date],
        *,
        counts: dict[date, int] | None = None,
        now: datetime,
    ) -> None:
        """Отметить, что за эти дни у источника уже спрашивали.

        Отмечается именно **факт запроса**, а не наличие свечей. Разница
        принципиальная: у выходного дня свечей нет и не будет, и без такой
        отметки программа переспрашивала бы про каждый выходной при каждом
        запуске — за год это сотни бесполезных запросов к бирже.

        День помечается `settled = 1`, только если он уже закончился по МСК.
        День, запрошенный посреди торгов, закрытым не считается и будет
        перезапрошен: иначе половина сегодняшней сессии осталась бы недокачанной
        навсегда, и в базе это выглядело бы как обычный вечерний перерыв.
        """
        today = ensure_msk(now).date()
        counts = counts or {}
        at = _to_ts(now)
        rows = [
            (symbol, day.isoformat(), at, int(counts.get(day, 0)), int(day < today))
            for day in days
        ]
        if not rows:
            return
        with self._transaction():
            self._db.executemany(
                """
                INSERT INTO data_day (symbol, day, requested_at, candles, settled)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(symbol, day) DO UPDATE SET
                    requested_at = excluded.requested_at,
                    candles = excluded.candles,
                    settled = excluded.settled
                """,
                rows,
            )

    def settled_days(self, symbol: str) -> set[date]:
        """Дни, за которые уже спрашивали и которые уже закончились."""
        return {
            date.fromisoformat(str(day))
            for (day,) in self._db.execute(
                "SELECT day FROM data_day WHERE symbol = ? AND settled = 1",
                (symbol,),
            )
        }

    def forget_day_marks(self, symbol: str, since: date, until: date) -> int:
        """Забыть отметки «за эти дни у источника уже спрашивали». Сколько сняло.

        Нужно ровно для одного случая: человек просит **загрузить отрезок
        заново**. Без снятия отметок такая просьба выполнялась бы молча
        наполовину — `days_to_request` выкинул бы из запроса всё, за что уже
        спрашивали, то есть почти весь отрезок, и загрузка «заново» не сделала
        бы ничего.

        ⚠️ **Свечи не трогаются ни одной.** Стирается только учёт запросов
        (`data_day`), а он говорит «спрашивали», а не «в базе всё есть». Худшее
        последствие снятой отметки — лишние запросы к бирже; потерять данные
        этим нельзя. Пути удаления настоящих свечей у хранилища нет вовсе
        (`forget_minutes` работает только по собранным нами рядам), и заводить
        его ради этой кнопки не потребовалось.

        :raises ValueError: период задом наперёд.
        """
        if until < since:
            raise ValueError(f"период задом наперёд: {since} .. {until}")
        with self._transaction():
            cursor = self._db.execute(
                "DELETE FROM data_day WHERE symbol = ? AND day BETWEEN ? AND ?",
                (symbol, since.isoformat(), until.isoformat()),
            )
        return int(cursor.rowcount)

    def days_to_request(self, symbol: str, since: date, until: date) -> list[date]:
        """Какие дни периода ещё не закрыты запросом — по возрастанию."""
        if until < since:
            raise ValueError(f"период задом наперёд: {since} .. {until}")
        known = self.settled_days(symbol)
        span = (until - since).days + 1
        return [day for i in range(span) if (day := since + timedelta(days=i)) not in known]

    # -- чтение ------------------------------------------------------------

    def symbols(self) -> list[str]:
        return [row[0] for row in self._db.execute(
            "SELECT DISTINCT symbol FROM minute_candle ORDER BY symbol"
        )]

    def coverage(self, symbol: str) -> Coverage:
        """Первая и последняя минутка инструмента и сколько их всего."""
        row = self._db.execute(
            "SELECT MIN(ts), MAX(ts), COUNT(*) FROM minute_candle WHERE symbol = ?",
            (symbol,),
        ).fetchone()
        first, last, count = row
        return Coverage(
            symbol=symbol,
            first=_from_ts(int(first)) if first is not None else None,
            last=_from_ts(int(last)) if last is not None else None,
            count=int(count),
        )

    def minutes(
        self,
        symbol: str,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[Candle]:
        """Минутные свечи в полуинтервале `[since, until)`, по возрастанию времени.

        Границы полуоткрыты намеренно: так соседние периоды стыкуются
        без пересечения и без пропуска ровно одной свечи на границе.
        """
        sql = ["SELECT ts, open, high, low, close, volume FROM minute_candle WHERE symbol = ?"]
        args: list[object] = [symbol]
        if since is not None:
            sql.append("AND ts >= ?")
            args.append(_to_ts(since))
        if until is not None:
            sql.append("AND ts < ?")
            args.append(_to_ts(until))
        sql.append("ORDER BY ts")
        return [
            Candle(
                time=_from_ts(int(ts)),
                open=float(o),
                high=float(h),
                low=float(l),
                close=float(c),
                volume=float(v),
                timeframe=MINUTE,
                filled_minutes=1,
            )
            for ts, o, h, l, c, v in self._db.execute(" ".join(sql), args)
        ]

    def minute_times(
        self,
        symbol: str,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[datetime]:
        """Только **времена** минуток периода, по возрастанию.

        Отдельно от `minutes()` ради отчёта о загрузке: он считает по временам
        и пропуски, и разрывы, и границы ряда — а собирал ради этого 74 тысячи
        объектов `Candle` со всеми ценами. Здесь один столбец.
        """
        sql = ["SELECT ts FROM minute_candle WHERE symbol = ?"]
        args: list[object] = [symbol]
        if since is not None:
            sql.append("AND ts >= ?")
            args.append(_to_ts(since))
        if until is not None:
            sql.append("AND ts < ?")
            args.append(_to_ts(until))
        sql.append("ORDER BY ts")
        return [_from_ts(int(ts)) for (ts,) in self._db.execute(" ".join(sql), args)]

    def has_minutes_after(self, symbol: str, moment: datetime) -> bool:
        """Есть ли в базе хоть одна минутка не раньше `moment`.

        Один индексный поиск, не счёт. Нужен догрузке: если данные следующего
        дня уже лежат в базе, обрыв выдачи **внутри** предыдущего дня исключён —
        ISS отдаёт свечи по возрастанию времени, и до следующего дня обход
        дошёл бы только через весь предыдущий.
        """
        row = self._db.execute(
            "SELECT 1 FROM minute_candle WHERE symbol = ? AND ts >= ? LIMIT 1",
            (symbol, _to_ts(moment)),
        ).fetchone()
        return row is not None

    def minute_volumes(
        self,
        symbol: str,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[tuple[datetime, float]]:
        """Времена и объёмы минуток периода, по возрастанию.

        Отдельно от `minutes()` ради подсчёта дневных объёмов по цепочке
        контрактов (`market.chain`): рубеж считается по объёму, и собирать
        ради него шесть контрактов по 80 тысяч `Candle` со всеми ценами —
        полмиллиона объектов на одно сравнение двух чисел в день.
        """
        sql = ["SELECT ts, volume FROM minute_candle WHERE symbol = ?"]
        args: list[object] = [symbol]
        if since is not None:
            sql.append("AND ts >= ?")
            args.append(_to_ts(since))
        if until is not None:
            sql.append("AND ts < ?")
            args.append(_to_ts(until))
        sql.append("ORDER BY ts")
        return [
            (_from_ts(int(ts)), float(volume))
            for ts, volume in self._db.execute(" ".join(sql), args)
        ]

    def forget_minutes(self, symbol: str) -> int:
        """Стереть все минутки собранного нами ряда. Отдаёт, сколько сняло.

        **Стирается только синтетический ряд** (`@…`), и это не оговорка,
        а единственная причина, по которой метод вообще существует. Пересборка
        со сменившимся рубежом обязана снять прошлый ряд целиком: иначе за
        границей новых отрезков остался бы хвост прошлой сборки, и в ряду
        оказались бы дни двух контрактов сразу.

        Настоящие свечи не стираются никогда и ничем: у продукта нет ни одного
        случая, где это нужно, а метод «удалить свечи инструмента» — это
        тихая потеря истории на одну опечатку в коде.

        :raises ValueError: код не синтетический.
        """
        if not is_synthetic(symbol):
            raise ValueError(
                f"«{symbol}» — инструмент биржи, и его свечи не стираются. "
                "Стереть можно только собранный нами ряд (код с «@»): "
                "загруженную историю вернуть будет неоткуда"
            )
        with self._transaction():
            cursor = self._db.execute(
                "DELETE FROM minute_candle WHERE symbol = ?", (symbol,)
            )
        return int(cursor.rowcount)

    def trading_days(self, symbol: str) -> list[date]:
        """Дни, в которых есть хотя бы одна минутка — по возрастанию.

        Именно это и есть «сколько торговых дней истории у нас реально есть».
        Считается по данным, а не по календарю: календаря у слоя нет.
        """
        return [
            date.fromisoformat(str(day))
            for (day,) in self._db.execute(
                # unixepoch → localtime не годится: он возьмёт зону машины,
                # а торговый день считается по МСК. Смещение задаётся явно.
                "SELECT DISTINCT date(ts + 3 * 3600, 'unixepoch') FROM minute_candle "
                "WHERE symbol = ? ORDER BY 1",
                (symbol,),
            )
        ]

    def source_counts(
        self,
        symbol: str,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> dict[str, int]:
        """Сколько минуток какого источника лежит в периоде — для журнала."""
        sql = ["SELECT source, COUNT(*) FROM minute_candle WHERE symbol = ?"]
        args: list[object] = [symbol]
        if since is not None:
            sql.append("AND ts >= ?")
            args.append(_to_ts(since))
        if until is not None:
            sql.append("AND ts < ?")
            args.append(_to_ts(until))
        sql.append("GROUP BY source")
        return {str(name): int(count) for name, count in self._db.execute(" ".join(sql), args)}

    def bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        since: datetime | None = None,
        until: datetime | None = None,
        *,
        drop_unsettled: bool = False,
        known_until: datetime | None = None,
    ) -> list[Candle]:
        """Свечи таймфрейма — собираются на чтении, из минуток.

        Откуда берётся «докуда мы знаем минутки»
        ----------------------------------------
        Из **учёта загруженного** (`data_day`), а не из самих данных, и это
        разные вещи. Раньше здесь стояло `known_until = последняя минутка + 1`
        на весь запрошенный период. Такой признак верен только для хвоста ряда:
        после дыры в середине истории последняя минутка лежит далеко за дырой,
        и огрызок бара **на краю дыры** объявлялся закрытым. Он входил в среднюю
        как полноценная свеча, сверка с прототипом расходилась на одной свече —
        и искать это пошли бы в стратегии.

        Бар не пересекает границу суток ни при одном допустимом таймфрейме
        (делители часа и кратные часу делители суток), поэтому знание считается
        **по дням**:

        * день **отмечен загруженным** (`settled_days`: за него спрашивали
          и он уже закончился) — знаем весь день, до его конца. Отсутствие
          минуток внутри дня в этом случае означает «сделок не было», а не
          «не докачали»;
        * день **не отмечен** (не спрашивали, ответ не дошёл, или день ещё
          идёт) — знаем ровно по последнюю имеющуюся минутку этого дня.
          Бар, чей интервал заканчивается позже, помечается незакрытым.

        Отсюда свойство, которое и требовалось: **признак закрытости бара
        никогда не сильнее, чем учёт загрузки.** В базе, наполненной мимо учёта
        (например, только потоком брокера), признак строже прежнего, а не мягче:
        хвостовой бар каждого дня остаётся незакрытым, пока догрузка день
        не подтвердит. «Данные кончились» и «торги кончились» по самому ряду
        неразличимы, и разрешать это сомнение в свою пользу нельзя.

        Когда правила по дням мало: параметр `known_until`
        -------------------------------------------------
        Правило по дням умеет сказать «дальше не знаем», но не умеет сказать
        «знаем до вот этого момента». Для дня **без** отметки в `data_day` оно
        всегда останавливается на последней имеющейся минутке — и это бьёт
        по двум местам сразу:

        * **в бою.** Сегодняшний день не отмечен никогда. Бар 10:55 (закрытие
          11:00 — граница торгового окна) становится закрытым только когда
          придёт минутка 11:00 или позже. На неликвидном инструменте 26 %
          минутных слотов пусты: сделок в 11:00…11:07 может не быть,
          и принудительный выход подаётся с опозданием, а при
          `drop_unsettled=True` бара просто нет. На вечернем клиринге
          эффект детерминированный: бар с закрытием 18:50 незакрыт все
          пятнадцать минут перерыва;
        * **в тестере и сверке.** Правая граница отрезка начинает зависеть
          от того, была ли сделка в последние две минуты.

        Поэтому источник, который **знает** границу полноты, называет её сам:
        боевой — потому что подписан на поток и получает подтверждения,
        тестер — потому что сам задал отрезок. Знание только **усиливается**:
        берётся `max` от правила по данным и от заявленного момента,
        обрезанного концом суток. Соврать в слабую сторону параметр не может,
        а `None` — сегодняшнее поведение слово в слово.

        ⚠️ `since` и `until` режут **минутки**, а не бары. Если `since`
        не лежит на сетке таймфрейма, первый бар соберётся из огрызка
        интервала — он будет помечен `is_partial`, но существовать будет.
        Границы задаёт вызывающий: обрезать их здесь молча значило бы
        отдавать не тот период, который запросили.

        :param known_until:
            момент, до которого вызывающий **знает**, что минутки полны.
            Действует только на дни без отметки в `data_day`: отметка о
            загрузке и так означает знание до конца суток.
        """
        minutes = self.minutes(symbol, since, until)
        if not minutes:
            return []
        settled = self.settled_days(symbol)
        limit = ensure_msk(until) if until is not None else None
        claimed = ensure_msk(known_until) if known_until is not None else None

        bars: list[Candle] = []
        for day, group in groupby(minutes, key=lambda candle: candle.time.date()):
            of_day = list(group)
            day_end = datetime.combine(
                day + timedelta(days=1), datetime.min.time(), MSK
            )
            if day in settled:
                known = day_end
            else:
                known = of_day[-1].time + _MINUTE
                if claimed is not None:
                    known = max(known, min(day_end, claimed))
            if limit is not None:
                known = min(known, limit)
            bars.extend(
                build_bars(
                    of_day,
                    timeframe,
                    known_until=known,
                    drop_unsettled=drop_unsettled,
                    on_duplicate="error",
                )
            )
        return bars

    def gaps(
        self,
        symbol: str,
        since: datetime | None = None,
        until: datetime | None = None,
        *,
        min_minutes: int = 1,
        crossing_date: bool | None = None,
    ) -> list[Gap]:
        """Разрывы в минутном ряду по данным базы.

        :param crossing_date: `None` — все; `False` — только внутри одного дня;
            `True` — только перешагнувшие сутки. Ночь и выходные дают разрыв
            каждый календарный день, и в общей куче они топят те несколько,
            ради которых счёт и ведётся.
        """
        return find_gaps(
            self.minute_times(symbol, since, until),
            min_minutes=min_minutes,
            crossing_date=crossing_date,
        )

    def missing_minutes(
        self,
        symbol: str,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> int:
        return count_missing_minutes(self.minute_times(symbol, since, until))

    # -- журнал ------------------------------------------------------------

    def write_load_report(self, report: LoadReport, *, now: datetime | None = None) -> int:
        """Записать отчёт о загрузке и найденные разрывы. Возвращает id записи.

        В `data_gap` попадает ровно то, что лежит в `report.gaps`. Догрузка
        кладёт туда **разрывы внутри дня** (`market.sync`): ночной перерыв
        и выходные дают разрыв каждый календарный день, и в журнале они топят
        те несколько, ради которых журнал и ведётся. Их число уходит
        отдельной колонкой `overnight_gaps`.
        """
        at = _to_ts(now or datetime.now(MSK))
        with self._transaction():
            cursor = self._db.execute(
                """
                INSERT INTO data_load
                    (at, symbol, source, requested_from, requested_to, fetched,
                     inserted, updated, kept, duplicates, collapsed,
                     missing_minutes, gap_count, overnight_gaps,
                     pages, requests, retries, first_ts, last_ts, summary)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    at,
                    # Инструмент и источник — тоже текст, и тоже с улицы:
                    # инструмент вводит владелец счёта. Список «эти колонки
                    # чистые» здесь однажды уже оказался неверным.
                    self._clean(report.symbol),
                    self._clean(report.source),
                    report.requested_from.isoformat() if report.requested_from else None,
                    report.requested_to.isoformat() if report.requested_to else None,
                    report.fetched,
                    report.inserted,
                    report.updated,
                    report.kept,
                    report.duplicates,
                    report.collapsed,
                    report.missing_minutes,
                    len(report.gaps),
                    report.overnight_gaps,
                    report.pages,
                    report.requests,
                    report.retries,
                    _to_ts(report.first_time) if report.first_time else None,
                    _to_ts(report.last_time) if report.last_time else None,
                    # ⚠️ Не «свой текст из чисел и дат», как здесь считалось:
                    # `LoadReport.note` собирается в `market.sync` из
                    # `f"{type(error).__name__}: {error}"` и вклеивается
                    # в `summary()`. Сегодня источник один и он без токена,
                    # но `sync_minutes` уже принимает `source` — путь написан
                    # под приход брокерского источника.
                    self._clean(report.summary()),
                ),
            )
            load_id = int(cursor.lastrowid or 0)
            self._db.executemany(
                """
                INSERT OR IGNORE INTO data_gap
                    (symbol, start_ts, end_ts, minutes, crosses_date, detected_at, load_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        self._clean(report.symbol),
                        _to_ts(gap.start),
                        _to_ts(gap.end),
                        gap.minutes,
                        int(gap.crosses_date),
                        at,
                        load_id,
                    )
                    for gap in report.gaps
                ],
            )
        return load_id

    def load_log(self, symbol: str | None = None, limit: int = 50) -> list[dict[str, object]]:
        """Последние записи журнала загрузок, свежие первыми."""
        sql = "SELECT * FROM data_load"
        args: list[object] = []
        if symbol is not None:
            sql += " WHERE symbol = ?"
            args.append(symbol)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        cursor = self._db.execute(sql, args)
        names: Sequence[str] = [column[0] for column in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    def gap_log(
        self,
        symbol: str | None = None,
        limit: int = 200,
        *,
        crossing_date: bool | None = None,
    ) -> list[Gap]:
        """Разрывы из журнала — **свежие первыми**, как и записи о загрузках.

        Здесь стояло `ORDER BY start_ts` по возрастанию. При лимите 200
        и трёх сотнях записей читатель получал двести **самых старых** —
        ночные перерывы прошлого сентября, — а дыра внутри торгового окна,
        ради которой порог разрыва и опускали, в выдачу не попадала вовсе.
        Рядом `load_log` всё это время был отсортирован правильно.

        :param crossing_date: `None` — все; `False` — только внутри одного дня;
            `True` — только перешагнувшие сутки. Догрузка ночные разрывы
            в журнал не пишет, но базы прежней сборки их содержат.
        """
        sql = "SELECT start_ts, end_ts, minutes, crosses_date FROM data_gap"
        where: list[str] = []
        args: list[object] = []
        if symbol is not None:
            where.append("symbol = ?")
            args.append(symbol)
        if crossing_date is not None:
            where.append("crosses_date = ?")
            args.append(int(crossing_date))
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY start_ts DESC LIMIT ?"
        args.append(limit)
        return [
            Gap(
                start=_from_ts(int(start)),
                end=_from_ts(int(end)),
                minutes=int(minutes),
                crosses_date=bool(crosses),
            )
            for start, end, minutes, crosses in self._db.execute(sql, args)
        ]

    # -- журналы сделок и решений -------------------------------------------

    def open_journal_session(
        self,
        run: SessionRecord,
        *,
        now: datetime | None = None,
        keep_backtest_sessions: int = BACKTEST_SESSIONS_KEPT,
    ) -> JournalSession:
        """Начать прогон и получить его. Строки журналов ссылаются на него.

        Происхождение (`run.origin`) задаётся здесь и больше не меняется
        никогда: смену отвергает сторож в самой базе. Прогон не бывает
        наполовину боевым, поэтому признак стоит у прогона, а не у каждой
        строки (решение 0011).

        Сразу после создания прогона чистятся **старые прогоны по истории**:
        их хранится последние `keep_backtest_sessions` штук. Боевой журнал
        и симуляция на боевом потоке не чистятся вовсе — они неповторимы.
        Что именно удалено, лежит в `self.pruned_journal`: молча выбрасывать
        записи нельзя, даже воспроизводимые.

        :raises ValueError: `keep_backtest_sessions` меньше единицы —
            это удалило бы и только что открытый прогон.
        """
        if keep_backtest_sessions < 1:
            raise ValueError(
                f"хранить {keep_backtest_sessions} прогонов по истории нельзя: "
                "чистка удалила бы и тот, который сейчас открывается. "
                "Чтобы убрать все, зовите prune_journal(0) отдельно"
            )
        started = ensure_msk(now) if now is not None else datetime.now(MSK)
        # Чистятся ВСЕ текстовые поля, а не только те, куда «может» прийти
        # чужой текст. Список исключений устаревает молча: инструмент
        # владелец счёта вводит руками и вставить туда способен что угодно,
        # а `strategy` и `app_version` завтра начнут собираться из чужих строк.
        #
        # Список полей — один (`_SESSION_TEXT_COLUMNS`), и имена в нём те же,
        # что у `SessionRecord`: поле, заведённое завтра, попадёт и в чистку,
        # и в запись само. Перечисленное руками попало бы туда, куда дописали,
        # а полнота списка теперь проверяется тестом.
        values: dict[str, object] = {
            "origin": run.origin.value,
            "started_at": _to_ts(started),
        }
        for name in _SESSION_TEXT_COLUMNS:
            values[name] = self._clean(str(getattr(run, name)))
        # Имена колонок в запрос подставляются из словаря выше — из нашего
        # же кода, а не из чужого текста. Значения идут параметрами, как
        # везде: подставлять их в текст запроса нельзя (`settings` и `note`
        # приходят от человека).
        columns = ", ".join(values)
        with self._transaction():
            cursor = self._db.execute(
                f"INSERT INTO journal_session ({columns}) "
                f"VALUES ({', '.join('?' * len(values))})",
                tuple(values.values()),
            )
            session_id = int(cursor.lastrowid or 0)
            # ⚠️ Чистка — В ТОЙ ЖЕ транзакции, что и вставка. Раньше она шла
            # после коммита, и её отказ (диск полон, база занята) оставлял
            # прогон без строк и без `finished_at` — то есть неотличимый
            # от аварийно завершённого. Ложный факт ровно там, где человек
            # ищет, что случилось.
            self.pruned_journal = self._prune_journal(keep_backtest_sessions)
            # Прогон читается обратно, а не собирается здесь вторым набором
            # полей. Собранный руками, он разошёлся бы с записанным молча:
            # у колонок прогона одинаковые типы. Прочитанный — это то,
            # что в базе, и путь чтения тот же самый (`_session_of`).
            (row,) = self._db.execute(
                f"SELECT {_SESSION_SELECT} FROM journal_session WHERE id = ?",
                (session_id,),
            )
        return _session_of(row)

    def finish_journal_session(
        self, session_id: int, *, now: datetime | None = None, note: str | None = None
    ) -> bool:
        """Отметить, что прогон закончился. Отвечает, случилось ли закрытие.

        Незакрытый прогон (`finished_at is None`) — это не дефект записи,
        а факт: программу закрыли аварийно, и строки журнала при этом
        остались на месте. Такой прогон видно в списке, и это правильнее,
        чем проставить ему время закрытия задним числом.

        ⚠️ **Неизвестный номер — отказ, а не тихий ноль строк.** Прежде вызов
        с чужим номером проходил молча, и настоящий прогон оставался
        незакрытым навсегда: в журнале он читается как «программу закрыли
        аварийно», то есть ошибка в номере превращается в ложный факт
        о торговом дне.

        Повторное закрытие уже закрытого прогона — не отказ, а `False`:
        завершение программы бывает вызвано дважды, и падать на этом нельзя.
        Время конца при этом **не** переписывается — это сторожит и база.

        ⚠️ **`True` означает «закрыл я», а не «прогон закрыт».** Ответ берётся
        из числа задетых строк, а не из проверки перед `UPDATE`. Между
        проверкой и записью прогон успевает закрыть кто-то ещё — второе
        подключение к тому же файлу или правка снаружи, — и тогда `UPDATE`
        не задевает ни одной строки: в базе стоит чужое время конца и чужая
        заметка, а поданная сюда выброшена. Прежде вызывающему в этом случае
        отвечали `True`, то есть говорили, что записана его.

        ⚠️ `note` ложится в **отдельную** колонку `finish_note` и заметку,
        с которой прогон открыли, не трогает. Прежде здесь стояло
        `SET note = ?`: прогон, открытый с «автозапуск, догрузка 3 дня»,
        после Ctrl+C нёс только «закрыто по Ctrl+C», и условие открытия
        было утрачено безвозвратно. Заметок стало две, и `note` с этой
        правкой переехал в замороженные поля прогона.

        ⚠️ **Закрытие идёт последним действием прогона.** После него дозапись
        отвергает сторож (`journal_*_joins_an_open_run`): закрытый прогон —
        законченное свидетельство. Строку «работа окончена» пишите **до**
        этого вызова, иначе получите отказ на завершении программы.

        :param note: чем прогон кончился. `None` — закрыть молча; так
            и записывается, пустой строкой, а не выдумкой про штатный конец.
        :raises LookupError: прогона с таким номером в журнале нет.
        """
        moment = ensure_msk(now) if now is not None else datetime.now(MSK)
        row = self._db.execute(
            "SELECT finished_at FROM journal_session WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise LookupError(
                f"прогона {session_id} в журнале нет, закрывать нечего. "
                "Номер прогона берётся у open_journal_session и живёт "
                "до конца работы: чужой номер здесь означает, что настоящий "
                "прогон останется незакрытым и будет прочитан как аварийный"
            )
        if row[0] is not None:
            return False
        with self._transaction():
            cursor = self._db.execute(
                "UPDATE journal_session SET finished_at = ?, finish_note = ? "
                "WHERE id = ? AND finished_at IS NULL",
                (_to_ts(moment), self._clean(note or ""), session_id),
            )
            # `AND finished_at IS NULL` — не украшение запроса, а условие
            # гонки: оно не даёт переписать чужое закрытие. Ноль задетых
            # строк означает, что прогон закрыли между `SELECT` и `UPDATE`.
            closed = cursor.rowcount == 1
        return closed

    def write_decision(self, session_id: int, record: DecisionRecord) -> int:
        """Записать одну строку журнала решений. Возвращает её id.

        Вход для **боевого** режима: движок отдаёт строку по мере событий,
        и каждая ложится своей транзакцией. При `synchronous = FULL` это
        отдельный сброс на диск на строку — здесь это ровно то, что нужно:
        строк за торговый день десятки, а «запись не теряется при аварийном
        завершении» — пункт приёмки.
        """
        (written,) = self.write_decisions(session_id, (record,))
        return written

    def write_decisions(
        self, session_id: int, records: Iterable[DecisionRecord]
    ) -> list[int]:
        """Записать пачку строк журнала решений. Возвращает их id по порядку.

        Вход для **прогона по истории**: тысячи строк одной транзакцией,
        один сброс на диск вместо тысячи.

        Порядок строк сохраняется и держится на `id`, а не на времени:
        у десяти строк одной свечи время закрытия одно и то же, и сортировка
        по нему перемешала бы их. Секунды у времени хранятся, доли секунды —
        нет; порядку это не мешает по той же причине.

        ⚠️ Сторожа на повтор здесь **нет**, в отличие от сделок. Естественного
        ключа у строки решения не существует: две одинаковые строки в одну
        секунду законны (один и тот же отказ, названный дважды), и правило
        «второй такой не бывает» отвергало бы правду. Цена известна: повтор
        пачки после таймаута удвоит строки журнала решений. Это шум в чтении,
        а не удвоенная прибыль за день, и отвергнутая правдивая строка дороже.

        :raises sqlite3.IntegrityError: прогон уже закрыт — дозапись
            в законченное свидетельство отвергает сторож.
        """
        written: list[int] = []
        with self._transaction():
            for record in records:
                cursor = self._db.execute(
                    """
                    INSERT INTO journal_decision (session_id, at, event, reason, level)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        _to_ts(record.at),
                        self._clean(record.event),
                        self._clean(record.reason),
                        DecisionLevel(record.level).value,
                    ),
                )
                written.append(int(cursor.lastrowid or 0))
        return written

    def _trade_values(
        self, session_id: int, record: TradeRecord, written_at: int
    ) -> dict[str, object]:
        """Строка сделки по колонкам: имя → значение, готовое к записи.

        Один словарь на три дела: вставка, поиск уже записанной такой же
        и сверка её содержимого. Порядок колонок нигде не участвует — только
        имена, поэтому переставить местами `entry_price` и `exit_price`
        незаметно нельзя.
        """
        return {
            "session_id": session_id,
            "symbol": self._clean(record.symbol),
            "side": TradeSide(record.side).value,
            "volume": float(record.volume),
            "entry_ts": _to_ts(record.entry_time),
            "entry_price": float(record.entry_price),
            # ⚠️ Имя заявки на Э1-5 придёт из ответа брокера, то есть станет
            # чужим текстом. Чистить его позже, когда это случится, — значит
            # не почистить.
            "entry_order_id": self._clean(record.entry_order_id),
            "exit_ts": _to_ts(record.exit_time),
            "exit_price": float(record.exit_price),
            "exit_order_id": self._clean(record.exit_order_id),
            "exit_reason": self._clean(record.exit_reason),
            "gross": float(record.gross),
            "commission": None if record.commission is None else float(record.commission),
            "net": None if record.net is None else float(record.net),
            "ruble_per_point": float(record.ruble_per_point),
            "written_at": written_at,
        }

    def _same_trade_already(self, values: dict[str, object]) -> int | None:
        """Номер строки, в которой эта сделка уже записана. `None` — новой нет.

        Ищется по естественному ключу (`_TRADE_IDENTITY`) — тому же, которым
        сторож `journal_trade_is_written_once` отвергает вторую такую же
        строку. Список колонок один на оба места, поэтому «нашлась» здесь
        и «отвергнет» там — это одно и то же условие.

        ⚠️ Совпадения ключа мало: сверяются **все** остальные колонки, кроме
        времени записи. Ключ грубый по построению (прогон, инструмент,
        сторона, вход, выход), и две строки с одним ключом, но разной ценой —
        это не повтор, а две разные сделки, из которых вторая молча пропала бы.

        :raises ValueError: ключ совпал, содержимое — нет.
        """
        columns = ", ".join(values)
        row = self._db.execute(
            f"SELECT id, {columns} FROM journal_trade WHERE {_TRADE_IDENTITY_WHERE}",
            tuple(values[name] for name in _TRADE_IDENTITY),
        ).fetchone()
        if row is None:
            return None
        stored = dict(zip(values, row[1:], strict=True))
        # Время записи сравнивать нельзя: у повтора оно своё, и совпасть
        # с прежним не должно.
        differs = tuple(
            name
            for name, value in values.items()
            if name != "written_at" and stored[name] != value
        )
        if differs:
            raise ValueError(
                f"сделка {values['symbol']} в прогоне {values['session_id']} уже "
                f"записана строкой {int(row[0])}, но поданная отличается от неё "
                f"полями: {', '.join(differs)}. Вход и выход у них общие, значит "
                "это не повтор пачки, а две разные сделки с одним ключом — "
                "записать вторую поверх первой значило бы потерять одну из них"
            )
        return int(row[0])

    def write_trades(self, session_id: int, records: Iterable[TradeRecord]) -> list[int]:
        """Записать закрытые сделки. Возвращает номер строки на каждую поданную.

        В журнал сделок попадает **закрытая** сделка: у неё есть и вход,
        и выход. Открытая позиция сделкой не является — про неё говорит
        журнал решений строкой «позиция открыта», и она же восстанавливается
        сверкой со счётом после перезапуска.

        ⚠️ **Уже записанная сделка пропускается, соседняя новая — пишется.**
        Правка ревью 03.09.2026; прежде повтор пачки отвергался сторожем
        целиком, вместе с новыми сделками, которые в ней были. Цена прежнего
        поведения считалась в боевом дне: `write_trades` поднимал отказ,
        и единственная запись о сделке на настоящие деньги не появлялась
        вовсе. Отвергнутый дубль виден в отчёте и снимается, пропавшая
        сделка не видна никак.

        Пропуск не молчаливый и не на глазок:

        * повтором считается строка, у которой совпал естественный ключ
          **и** все остальные колонки, кроме времени записи. Совпал ключ,
          а цена нет — это две разные сделки, и вызов отказывает
          (`ValueError`), а не выбирает за владельца счёта, какую потерять;
        * номера пропущенных лежат в `self.repeated_trades` — пачка,
          записавшаяся наполовину, не должна выглядеть как обычная;
        * в ответе на каждую поданную сделку стоит номер её строки —
          у повтора прежний. После возврата **каждая** поданная сделка лежит
          в журнале, и ровно одной строкой.

        Пачка идёт одной транзакцией, поэтому «половина записалась» не бывает
        и здесь: либо все, либо ни одной.

        ⚠️ Сторож `journal_trade_is_written_once` остался и не подменён этой
        проверкой. Проверка вежливая — она стоит на нашем пути; сторож стоит
        на всех, включая правку базы снаружи, и условие у них общее
        (`_TRADE_IDENTITY`).

        ⚠️ **Закрытый прогон отвергается по-прежнему, и сделка пропадает.**
        Это не лечится здесь: куда девать сделку, пришедшую после закрытия
        прогона, — решение вызывающего (открыть новый прогон), а хранилище
        не вправе ни дописать её в законченное свидетельство, ни промолчать.
        Пачка, целиком состоящая из уже записанных сделок, проходит и в
        закрытый прогон: писать нечего.

        :raises ValueError: у поданной сделки ключ совпал с записанной,
            а содержимое разошлось.
        :raises sqlite3.IntegrityError: прогон уже закрыт.
        """
        at = _to_ts(datetime.now(MSK))
        # Отчёт о повторах обнуляется до работы, а не после: отказ посреди
        # пачки не должен оставить в поле след прошлого вызова.
        self.repeated_trades = ()
        written: list[int] = []
        repeats: list[int] = []
        with self._transaction():
            for record in records:
                values = self._trade_values(session_id, record, at)
                already = self._same_trade_already(values)
                if already is not None:
                    repeats.append(already)
                    written.append(already)
                    continue
                columns = ", ".join(values)
                cursor = self._db.execute(
                    f"INSERT INTO journal_trade ({columns}) "
                    f"VALUES ({', '.join('?' * len(values))})",
                    tuple(values.values()),
                )
                written.append(int(cursor.lastrowid or 0))
        self.repeated_trades = tuple(repeats)
        return written

    # -- чтение журналов ----------------------------------------------------

    def journal_sessions(
        self, *, origin: RunOrigin | None = None, limit: int = 50
    ) -> JournalPage[JournalSession]:
        """Прогоны, **свежие первыми**. `origin` сужает до одного вида.

        ⚠️ Обрезка по лимиту **названа** (`JournalPage.truncated`), а не
        оставлена на догадку. Прежде отсюда возвращался список: при пятидесяти
        прогонах в базе и пятидесяти в выдаче пятьдесят первый исчезал молча,
        и список выглядел ровно как полный. Тот же дефект уже закрыт
        у `decisions` и у `trades`, и цена у него та же: выбор по неполному
        числу.

        Читателю, которому полнота не нужна, менять нечего: `JournalPage` —
        последовательность, и длина, перебор, распаковка и обращение
        по номеру работают как у списка.
        """
        sql = [f"SELECT {_SESSION_SELECT}", "FROM journal_session"]
        args: list[object] = []
        if origin is not None:
            sql.append("WHERE origin = ?")
            args.append(origin.value)
        sql.append("ORDER BY id DESC LIMIT ?")
        args.append(_limit_probe(limit))
        rows = list(self._db.execute(" ".join(sql), args))
        truncated = len(rows) > limit
        del rows[limit:]
        return JournalPage(
            rows=tuple(_session_of(row) for row in rows),
            limit=limit,
            truncated=truncated,
        )

    def journal_session(self, session_id: int) -> JournalSession | None:
        """Один прогон по его номеру. `None` — такого прогона нет."""
        row = self._db.execute(
            f"SELECT {_SESSION_SELECT} FROM journal_session WHERE id = ?",
            (session_id,),
        ).fetchone()
        return None if row is None else _session_of(row)

    def _one_run(
        self, *, session_id: int | None, origin: RunOrigin | None, all_runs: bool
    ) -> int | None:
        """К какому прогону сузить чтение. `None` — сужения по прогону нет.

        Правило одно на оба журнала:

        * назван прогон — он и читается;
        * названо происхождение — читаются все прогоны этого происхождения;
          происхождения при этом не смешиваются, а прогоны одного вида
          смешиваются намеренно: боевой день, разорванный перезапуском
          программы, — это по-прежнему один отчёт;
        * сказано `all_runs` — читается всё, что есть, и смешение объявлено
          вслух самим вызовом;
        * не названо ничего — читается **последний** прогон. Умолчанием
          здесь была вся база вперемешку: владелец счёта видел сорок сделок
          вместо четырёх настоящих и от этого числа выбирал объём.

        :raises ValueError: назван и прогон, и `all_runs` — просьба
            противоречива, и любое её толкование было бы догадкой.
        """
        if all_runs and session_id is not None:
            raise ValueError(
                f"нельзя просить сразу прогон {session_id} и все прогоны: "
                "уберите all_runs либо session_id"
            )
        if session_id is not None:
            return session_id
        if origin is not None or all_runs:
            return None
        latest = self._db.execute("SELECT MAX(id) FROM journal_session").fetchone()[0]
        return None if latest is None else int(latest)

    def decisions(
        self,
        *,
        session_id: int | None = None,
        origin: RunOrigin | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 5000,
        all_runs: bool = False,
    ) -> JournalPage[StoredDecision]:
        """Строки журнала решений — **по порядку записи**, старые первыми.

        ⚠️ Без `session_id`, `origin` и `all_runs` отдаётся **последний
        прогон**, а не вся база: журнал показывается по одному прогону за раз.
        Смешать боевые строки с прогоном по истории можно только назвав
        `all_runs` — разбор в `_one_run` и в решении 0011.

        ⚠️ Ограничение `limit` отсекает **старые** строки, а не свежие:
        выдача берётся с конца и разворачивается. Иначе читатель с лимитом
        получал бы первые пятьсот строк прошлогоднего прогона, а то, ради
        чего он открыл журнал, в выдачу не попадало бы вовсе — ровно этот
        дефект уже был у `gap_log`. Про саму обрезку выдача говорит:
        `JournalPage.truncated`.

        Происхождение приходит вместе со строкой и полем без умолчания:
        прочитать строку, не узнав, боевая она или из прогона, нельзя.
        """
        chosen = self._one_run(
            session_id=session_id, origin=origin, all_runs=all_runs
        )
        sql = [
            "SELECT d.id, d.session_id, s.origin, d.at, d.event, d.reason, d.level",
            "FROM journal_decision AS d",
            "JOIN journal_session AS s ON s.id = d.session_id",
        ]
        where, args = _journal_filter(
            session_id=chosen, origin=origin, since=since, until=until, column="d.at"
        )
        if where:
            sql.append("WHERE " + " AND ".join(where))
        sql.append("ORDER BY d.id DESC LIMIT ?")
        args.append(_limit_probe(limit))
        rows = list(self._db.execute(" ".join(sql), args))
        truncated = len(rows) > limit
        del rows[limit:]
        rows.reverse()
        return JournalPage(
            rows=tuple(
                StoredDecision(
                    id=int(row[0]),
                    session_id=int(row[1]),
                    origin=RunOrigin(str(row[2])),
                    at=_from_ts(int(row[3])),
                    event=str(row[4]),
                    reason=str(row[5]),
                    level=DecisionLevel(str(row[6])),
                )
                for row in rows
            ),
            limit=limit,
            truncated=truncated,
            session_id=chosen,
        )

    def trades(
        self,
        *,
        session_id: int | None = None,
        origin: RunOrigin | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 5000,
        all_runs: bool = False,
    ) -> JournalPage[StoredTrade]:
        """Закрытые сделки — по порядку записи, старые первыми.

        Отбор по времени идёт по **выходу**: сделка попала в период, если
        в нём она закрылась. Иначе сделка, открытая в пятницу и закрытая
        в понедельник, попадала бы в отчёт за пятницу результатом, которого
        в пятницу ещё не было.

        Сужение до прогона и признак обрезки — как у `decisions`: отчёт
        за день, собранный из сделок двух происхождений вперемешку, и есть
        та цена, ради которой это правило написано.
        """
        chosen = self._one_run(
            session_id=session_id, origin=origin, all_runs=all_runs
        )
        sql = [
            "SELECT t.id, t.session_id, s.origin, t.symbol, t.side, t.volume,",
            "       t.entry_ts, t.entry_price, t.entry_order_id,",
            "       t.exit_ts, t.exit_price, t.exit_order_id, t.exit_reason,",
            "       t.gross, t.commission, t.net, t.ruble_per_point, t.written_at",
            "FROM journal_trade AS t",
            "JOIN journal_session AS s ON s.id = t.session_id",
        ]
        where, args = _journal_filter(
            session_id=chosen,
            origin=origin,
            since=since,
            until=until,
            column="t.exit_ts",
        )
        if where:
            sql.append("WHERE " + " AND ".join(where))
        sql.append("ORDER BY t.id DESC LIMIT ?")
        args.append(_limit_probe(limit))
        rows = list(self._db.execute(" ".join(sql), args))
        truncated = len(rows) > limit
        del rows[limit:]
        rows.reverse()
        return JournalPage(
            rows=tuple(
                StoredTrade(
                    id=int(row[0]),
                    session_id=int(row[1]),
                    origin=RunOrigin(str(row[2])),
                    symbol=str(row[3]),
                    side=TradeSide(str(row[4])),
                    volume=float(row[5]),
                    entry_time=_from_ts(int(row[6])),
                    entry_price=float(row[7]),
                    entry_order_id=str(row[8]),
                    exit_time=_from_ts(int(row[9])),
                    exit_price=float(row[10]),
                    exit_order_id=str(row[11]),
                    exit_reason=str(row[12]),
                    gross=float(row[13]),
                    commission=None if row[14] is None else float(row[14]),
                    net=None if row[15] is None else float(row[15]),
                    ruble_per_point=float(row[16]),
                    written_at=_from_ts(int(row[17])),
                )
                for row in rows
            ),
            limit=limit,
            truncated=truncated,
            session_id=chosen,
        )

    # -- остановка робота ---------------------------------------------------

    def standing_halt(self) -> tuple[StoredHalt, ...]:
        """Причины остановки, стоящие сейчас, **в порядке появления**.

        Пустой кортеж означает «робот не остановлен», и другого способа
        сказать это здесь нет: остановка без причины — не остановка,
        а невидимый запрет.

        ⚠️ Порядок здесь не для красоты. Снимается **самая ранняя** причина
        (`app/port.py::HistoryPort.resume`, `D-086`): разбираются с того,
        с чего всё началось. `ORDER BY id DESC` снимал бы не ту причину,
        и заметить это можно было бы только по тому, какая надпись осталась
        в окне.
        """
        return tuple(
            _halt_of(row)
            for row in self._db.execute(
                "SELECT id, kind, event, reason, raised_at "
                "FROM robot_halt ORDER BY id"
            )
        )

    def raise_halt(
        self, cause: HaltRecord, *, now: datetime | None = None
    ) -> StoredHalt | None:
        """Поднять причину остановки. `None` — такая уже стоит.

        Правило повтора то же, что в памяти порта (`_Halt.add`), и это
        не совпадение: опрос счёта повторяет заход каждые полминуты и после
        остановки его не прекращает. Сто двадцать одинаковых строк в час
        похоронили бы под собой всё остальное — и в журнале, и здесь.

        Сравнение идёт по **очищенному** тексту, а не по поданному: на диск
        ложится очищенный, и сравнивать надо то, что там лежит. Чистка
        идемпотентна (`market.journal.redact`), поэтому причина, прочитанная
        из базы и поданная обратно, узнаётся как та же самая.

        ⚠️ Текст проходит **ту же** чистку, что и журнал (`_clean`). Причина
        остановки приходит из отказа брокера и уезжает на диск, в резервную
        копию и в файл, который владелец счёта пришлёт в переписку.

        :param now: когда причина поднята. `None` — сейчас, по московским
            часам. Довод существует ради проверок: время «вчера в 11:20»
            иначе не изобразить.
        """
        raised = ensure_msk(now) if now is not None else datetime.now(MSK)
        reason = self._clean(cause.reason)
        with self._transaction():
            standing = self._db.execute(
                "SELECT id FROM robot_halt WHERE reason = ?", (reason,)
            ).fetchone()
            if standing is not None:
                return None
            cursor = self._db.execute(
                "INSERT INTO robot_halt (kind, event, reason, raised_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    cause.kind.value,
                    self._clean(cause.event),
                    reason,
                    _to_ts(raised),
                ),
            )
            # Причина читается обратно, а не собирается здесь вторым набором
            # полей: собранная руками, она разошлась бы с записанной молча.
            # Тот же приём, что у `open_journal_session`.
            (row,) = self._db.execute(
                "SELECT id, kind, event, reason, raised_at FROM robot_halt "
                "WHERE id = ?",
                (int(cursor.lastrowid or 0),),
            )
        return _halt_of(row)

    def lift_halt(self, reason: str) -> bool:
        """Снять названную причину. `False` — такой причины не стояло.

        Снимается **одна названная**, а не всё разом, и вызывающего это
        касается напрямую: причины бывают про разные беды. Дневной лимит
        убытка — про деньги, «исход команды брокеру неизвестен» — про
        позицию, которой программа не знает. Снять их одним движением
        значило бы позволить владельцу счёта согласиться с убытком
        и **заодно**, не читая, снять требование сходить к брокеру
        (`D-086`, аудит 06.09.2026).

        Метода «снять всё» здесь нет намеренно. Он понадобился бы ровно
        одному вызывающему — тому, который ломает правило выше.
        """
        with self._transaction():
            cursor = self._db.execute(
                "DELETE FROM robot_halt WHERE reason = ?", (self._clean(reason),)
            )
        return int(cursor.rowcount or 0) > 0

    # -- объём и чистка -----------------------------------------------------

    def journal_stats(self) -> JournalStats:
        """Сколько в базе журнала: прогонов, строк, сделок и байт.

        Размер — всей базы целиком, а не одного журнала: отдельного числа
        SQLite без расширения `dbstat` не даёт, а придумывать оценку вместо
        измерения здесь незачем.
        """
        sessions = int(
            self._db.execute("SELECT COUNT(*) FROM journal_session").fetchone()[0]
        )
        decisions = int(
            self._db.execute("SELECT COUNT(*) FROM journal_decision").fetchone()[0]
        )
        trades = int(
            self._db.execute("SELECT COUNT(*) FROM journal_trade").fetchone()[0]
        )
        by_origin = tuple(
            (RunOrigin(str(name)), int(count))
            for name, count in self._db.execute(
                "SELECT origin, COUNT(*) FROM journal_session GROUP BY origin ORDER BY origin"
            )
        )
        pages = int(self._db.execute("PRAGMA page_count").fetchone()[0])
        page_size = int(self._db.execute("PRAGMA page_size").fetchone()[0])
        return JournalStats(
            sessions=sessions,
            decisions=decisions,
            trades=trades,
            sessions_by_origin=by_origin,
            database_bytes=pages * page_size,
        )

    def prune_journal(
        self, *, keep_backtest_sessions: int = BACKTEST_SESSIONS_KEPT
    ) -> PruneReport:
        """Убрать старые прогоны **по истории**. Возвращает, сколько убрано.

        Правило одно и оно в трёх словах: **чистится только то, что можно
        повторить**. Прогон по истории повторяется командой — те же свечи
        и те же настройки дадут тот же журнал. Боевой прогон и симуляция
        на боевом потоке видели рынок, которого в базе не осталось, и
        не чистятся вовсе; боевой вдобавок нельзя удалить и напрямую —
        отвергнет сторож в базе.

        :param keep_backtest_sessions: сколько последних прогонов по истории
            оставить. `0` — убрать все.
        :raises ValueError: число отрицательное.
        """
        if keep_backtest_sessions < 0:
            raise ValueError(
                f"хранить {keep_backtest_sessions} прогонов нельзя: число отрицательное"
            )
        with self._transaction():
            return self._prune_journal(keep_backtest_sessions)

    def _prune_journal(self, keep_backtest_sessions: int) -> PruneReport:
        """Чистка **внутри уже открытой** транзакции.

        Отдельный метод, потому что `_transaction` держит транзакцию
        соединения, а не вызова: голый `BEGIN` внутри `BEGIN` даёт
        `cannot start a transaction within a transaction`. Открытие прогона
        обязано чистить в своей транзакции — см. `open_journal_session`.
        """
        victims = _OLD_BACKTEST_SESSIONS
        args = (*_PRUNABLE_ORIGINS, keep_backtest_sessions)
        decisions = int(
            self._db.execute(
                f"SELECT COUNT(*) FROM journal_decision WHERE session_id IN ({victims})",
                args,
            ).fetchone()[0]
        )
        trades = int(
            self._db.execute(
                f"SELECT COUNT(*) FROM journal_trade WHERE session_id IN ({victims})",
                args,
            ).fetchone()[0]
        )
        cursor = self._db.execute(
            f"DELETE FROM journal_session WHERE id IN ({victims})", args
        )
        sessions = int(cursor.rowcount or 0)
        return PruneReport(
            sessions=sessions,
            decisions=decisions,
            trades=trades,
            kept=keep_backtest_sessions,
        )

    def forget_journal_sessions(self, session_ids: Iterable[int]) -> PruneReport:
        """Убрать названные прогоны целиком, со всеми их строками.

        Явное удаление по просьбе человека, а не чистка по расписанию.
        Боевой прогон не удалится и отсюда: сторож в базе отвергнет `DELETE`
        и вся операция откатится — частичного удаления не бывает.

        :raises sqlite3.IntegrityError: среди названных есть боевой прогон.
        """
        wanted = list(dict.fromkeys(int(one) for one in session_ids))
        if not wanted:
            return PruneReport()
        marks = ", ".join("?" * len(wanted))
        decisions = int(
            self._db.execute(
                f"SELECT COUNT(*) FROM journal_decision WHERE session_id IN ({marks})",
                wanted,
            ).fetchone()[0]
        )
        trades = int(
            self._db.execute(
                f"SELECT COUNT(*) FROM journal_trade WHERE session_id IN ({marks})",
                wanted,
            ).fetchone()[0]
        )
        with self._transaction():
            cursor = self._db.execute(
                f"DELETE FROM journal_session WHERE id IN ({marks})", wanted
            )
            sessions = int(cursor.rowcount or 0)
        return PruneReport(sessions=sessions, decisions=decisions, trades=trades)

    def _clean(self, text: str) -> str:
        """Текст, годный для журнала: без токена, ни целиком, ни частично.

        Чистка идёт **на записи**. На чтении она была бы бесполезной: значение
        уже лежало бы на диске, в резервной копии и в файле, который владелец
        счёта пришлёт в переписку.
        """
        return self._sanitize(text)
