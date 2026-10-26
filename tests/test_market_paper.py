"""Строки прогона симуляции в базе: заявки, отметка, пачка одной транзакцией.

Каждый тест стережёт **одно** поведение слоя данных, и оно названо первой
строкой его докстринга. Вход — база во временной папке; ни сеть, ни рабочая
база владельца счёта здесь не участвуют (правило 14).
"""

from __future__ import annotations

import asyncio
import dataclasses
import pathlib
import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime, timedelta

import pytest

from market import MarketWorker
from market.journal import (
    SECRET_MASK,
    DecisionLevel,
    DecisionRecord,
    RunOrigin,
    SessionRecord,
    TradeRecord,
    TradeSide,
)
from market.paper import (
    DecisionPhase,
    OrderKind,
    OrderRecord,
    PaperRows,
    PaperWritten,
    RunRowsMark,
)
from market.storage import (
    _GUARDS,
    _ORDER_CHECKED_COLUMNS,
    _ORDER_TEXT_COLUMNS,
    SCHEMA_VERSION,
    CandleStore,
    _same_sql,
)
from tests.market_helpers import msk
from tests.test_market_journal import FAKE_JWT, past_the_front_door

AT = msk(2026, 10, 9, 10, 5)
MARK = RunRowsMark(kept_since=msk(2026, 10, 6, 9, 0), live_from=AT)


@pytest.fixture
def store(tmp_path: pathlib.Path):
    with CandleStore(tmp_path / "paper.sqlite3") as opened:
        yield opened


def paper_run(store: CandleStore, symbol: str = "MXZ6", *, at: datetime = AT) -> int:
    return store.open_journal_session(
        SessionRecord(RunOrigin.PAPER, symbol=symbol), now=at
    ).id


def order(at: datetime = AT) -> OrderRecord:
    return OrderRecord(
        order_id="open:long:1",
        at=at,
        kind=OrderKind.SUBMITTED,
        action="open",
        side=TradeSide.LONG,
        volume=1.0,
        reason="Закрытие выше EMA(15)",
        close_price=100.0,
        average=99.5,
    )


def deal(entry: datetime = AT, *, price: float = 100.0) -> TradeRecord:
    return TradeRecord(
        symbol="MXZ6",
        side=TradeSide.LONG,
        volume=1.0,
        entry_time=entry,
        entry_price=price,
        exit_time=entry + timedelta(minutes=5),
        exit_price=price + 1.0,
        exit_reason="Тейк-профит",
        gross=1.0,
        entry_order_id="open:long:1",
        exit_order_id="take:long:1",
        commission=34.0,
        net=-33.0,
    )


def rows(*, mark: RunRowsMark | None = MARK) -> PaperRows:
    return PaperRows(
        mark=mark,
        decisions=(
            DecisionRecord(AT, "Вход в лонг", "Закрытие выше EMA(15)", DecisionLevel.TRADE),
        ),
        orders=(
            order(),
            dataclasses.replace(
                order(),
                order_id="take:long:1",
                at=AT + timedelta(minutes=5),
                kind=OrderKind.LEVEL_HIT,
                action="close",
                reason="",
                exit_reason="take_profit",
                level=100.5,
            ),
        ),
        trades=(deal(),),
    )


def counted(store: CandleStore, table: str) -> int:
    ((count,),) = past_the_front_door(store, f"SELECT COUNT(*) FROM {table}")
    return int(count)


# ------------------------------------------------------------- запись и чтение


def test_a_batch_lands_whole_and_reads_back_with_its_run_and_origin(store) -> None:
    """Пачка ложится целиком; заявка читается с прогоном и происхождением.

    Происхождение у прочитанной заявки обязательное (решение 0011 п. 4):
    разбор сделки симуляции, принятый за боевой, — выбор объёма по чужому
    числу.
    """
    session = paper_run(store)
    written = store.write_paper_rows(session, rows())

    assert written == PaperWritten(decisions=1, orders=2, trades=1, marked=True)
    page = store.orders(session_id=session)
    assert [stored.record for stored in page] == list(rows().orders), (
        "заявки прочитаны не теми, что записаны"
    )
    assert {stored.origin for stored in page} == {RunOrigin.PAPER}
    assert {stored.session_id for stored in page} == {session}
    assert [stored.record for stored in store.trades(session_id=session)] == [deal()]
    assert len(store.decisions(session_id=session)) == 1


def test_an_order_with_every_field_filled_reads_back_as_written(store) -> None:
    """Заявка, у которой заполнено каждое поле, читается из базы той же до поля.

    У заявки входа половина полей пуста, и сверка на ней не видит потери
    предела выхода по времени или имени цели. Здесь пустых нет — проверено
    тут же по списку полей, так что поле, заведённое завтра, без значения
    в этой пробе тест уронит.
    """
    full = OrderRecord(
        order_id="time:short:1",
        at=AT,
        kind=OrderKind.SUBMITTED,
        action="close",
        side=TradeSide.SHORT,
        volume=3.0,
        reason="Выход по времени с пределом",
        exit_reason="time_exit",
        target_id="open:short:1",
        close_price=100.0,
        average=99.5,
        level=101.0,
        limit_price=98.5,
        phase=DecisionPhase.LIVE,
    )
    empty = [
        one.name for one in dataclasses.fields(OrderRecord)
        if getattr(full, one.name) == one.default
    ]
    assert empty == [], f"проба не заполняет поля {empty}"
    session = paper_run(store)
    store.write_paper_rows(session, PaperRows(mark=MARK, orders=(full,)))

    (stored,) = store.orders(session_id=session)
    assert stored.record == full


def test_a_repeated_batch_writes_no_second_trade_and_says_how_many_it_sifted(store) -> None:
    """Повтор пачки: сделка второй строкой не ложится, отсеянное названо числом."""
    session = paper_run(store)
    store.write_paper_rows(session, rows())
    again = store.write_paper_rows(
        session, PaperRows(trades=(deal(), deal(AT + timedelta(hours=1))))
    )

    assert again.trades == 1 and again.repeated_trades == 1, again
    assert counted(store, "journal_trade") == 2, "повтор сделки лёг второй строкой"


def test_a_batch_is_all_or_nothing(store) -> None:
    """Отказ на одной строке откатывает всю пачку, отметку тоже.

    Половина пачки в базе означала бы, что вызывающий не знает, что
    повторять: курсоры сдвигаются только после фиксации.
    """
    session = paper_run(store)
    store.write_paper_rows(session, PaperRows(mark=MARK))
    other_session = paper_run(store)
    clash = PaperRows(
        mark=MARK,
        decisions=rows().decisions,
        orders=rows().orders,
        trades=(deal(), deal(price=200.0)),  # тот же ключ, другая цена
    )

    with pytest.raises(ValueError, match="две разные сделки"):
        store.write_paper_rows(other_session, clash)
    for table in ("journal_decision", "journal_order", "journal_trade"):
        assert counted(store, table) == 0, f"{table}: пачка легла наполовину"
    assert counted(store, "journal_run_rows") == 1, "отметка отказавшей пачки осталась"


def test_rows_without_a_mark_are_refused_and_a_mark_alone_is_written(store) -> None:
    """Строки без отметки отвергаются; отметка без строк пишется.

    Нет отметки — значит «сделки не записывались». Прогон со строками
    и без отметки читался бы как прогон без строк, а прогон без сделок
    без отметки — как незаписанный.
    """
    session = paper_run(store)
    with pytest.raises(ValueError, match="без отметки"):
        store.write_paper_rows(session, rows(mark=None))

    alone = store.write_paper_rows(session, PaperRows(mark=MARK))
    assert alone == PaperWritten(marked=True)
    (run,) = store.paper_runs(["MXZ6"])
    assert run.mark == MARK, "отметка без строк не легла"


def test_the_same_mark_twice_passes_and_another_one_is_refused(store) -> None:
    """Отметка одна на прогон: повтор той же проходит, другая — отказ."""
    session = paper_run(store)
    store.write_paper_rows(session, PaperRows(mark=MARK))

    assert store.write_paper_rows(session, PaperRows(mark=MARK)).marked is False
    moved = RunRowsMark(kept_since=MARK.kept_since, live_from=AT + timedelta(minutes=5))
    with pytest.raises(ValueError, match="другими"):
        store.write_paper_rows(session, PaperRows(mark=moved))


def test_a_run_over_history_gets_no_rows(store) -> None:
    """Прогон по истории строк не пишет — на этом стоит хранение двухсот прогонов."""
    session = store.open_journal_session(SessionRecord(RunOrigin.BACKTEST), now=AT).id
    with pytest.raises(ValueError, match="прогон по истории"):
        store.write_paper_rows(session, rows())
    assert counted(store, "journal_run_rows") == 0


def test_the_stats_count_the_new_tables(store) -> None:
    """`journal_stats` считает заявки и отметки — опора сторожа `RUNS_KEPT`."""
    store.write_paper_rows(paper_run(store), rows())
    stats = store.journal_stats()
    assert (stats.orders, stats.run_rows) == (2, 1)


# ------------------------------------------------------------- прогоны по инструменту


def test_paper_runs_are_read_by_the_codes_of_the_instrument_oldest_first(store) -> None:
    """Прогоны симуляции — только названных кодов, старые первыми, с отметкой.

    Прогон без отметки отдаётся с `mark is None`, а не пропадает: окно
    обязано назвать его «сделки не записывались», а не показать ноль.
    """
    late = paper_run(store, "MXZ6", at=AT + timedelta(hours=2))
    early = paper_run(store, "MXU6", at=AT)
    paper_run(store, "RIZ6", at=AT + timedelta(hours=1))
    store.open_journal_session(SessionRecord(RunOrigin.BACKTEST, symbol="MXZ6"), now=AT)
    store.write_paper_rows(late, rows())

    found = store.paper_runs(["MXZ6", "MXU6"])

    assert [run.session.id for run in found] == [early, late]
    assert found[0].mark is None and found[0].last_at is None
    assert found[1].mark == MARK
    assert found[1].last_at == AT + timedelta(minutes=5), "конец по строкам не тот"


def test_the_end_of_a_run_by_its_rows_counts_the_journal_too(store) -> None:
    """Конец прогона по строкам — самая поздняя строка любого вида, журнала решений тоже.

    После тейка робот до вечера стоит без заявок, а журнал пишет строки
    каждый бар. Прогон, закрытый аварийно вечером, шёл до последней строки
    журнала, а не до последней заявки. Мутация: конец — только по заявкам.
    """
    session = paper_run(store)
    later = AT + timedelta(hours=6)
    store.write_paper_rows(session, PaperRows(
        mark=MARK,
        decisions=(
            DecisionRecord(later, "Сделок сегодня больше не будет", "стоп после тейка",
                           DecisionLevel.INFO),
        ),
        orders=(order(AT),),
    ))

    (run,) = store.paper_runs(["MXZ6"])
    assert run.last_at == later


# ------------------------------------------------------------- сторожа


ROW_TABLES = ("journal_order", "journal_run_rows")


@pytest.mark.parametrize("table", ROW_TABLES)
def test_a_paper_row_cannot_be_rewritten_or_deleted(store, table: str) -> None:
    """Строку прогона симуляции не переписать и не удалить мимо прогона."""
    store.write_paper_rows(paper_run(store), rows())
    with pytest.raises(sqlite3.IntegrityError, match="переписать нельзя"):
        past_the_front_door(store, f"UPDATE {table} SET session_id = session_id")
    with pytest.raises(sqlite3.IntegrityError, match="удалить нельзя"):
        past_the_front_door(store, f"DELETE FROM {table}")


def test_a_closed_run_takes_no_more_paper_rows(store) -> None:
    """Закрытый прогон — законченное свидетельство: строк в него не дописать."""
    session = paper_run(store)
    store.finish_journal_session(session, now=AT + timedelta(hours=1))
    with pytest.raises(sqlite3.IntegrityError, match="больше не дописать"):
        store.write_paper_rows(session, PaperRows(mark=MARK))
    with pytest.raises(sqlite3.IntegrityError, match="больше не дописать"):
        past_the_front_door(
            store,
            "INSERT INTO journal_order (session_id, order_id, at, kind, action, side, volume) "
            "VALUES (?, 'x', 0, 'submitted', 'open', 'long', 1)",
            session,
        )


# ------------------------------------------------------------- секреты


def test_every_text_column_of_an_order_is_in_the_cleaning_list(store) -> None:
    """Список чистки совпадает с текстовыми колонками самой таблицы.

    Колонка, заведённая завтра и не дописанная в список, легла бы на диск
    нечищеной. Колонки под `CHECK` из наших перечислений чужого текста
    не принимают и чистки не требуют.
    """
    text = {
        str(row[1])
        for row in past_the_front_door(store, "PRAGMA table_info(journal_order)")
        if str(row[2]).upper() == "TEXT"
    }
    assert set(_ORDER_TEXT_COLUMNS) == text - set(_ORDER_CHECKED_COLUMNS)


#: Текстовые поля заявки и как испачкать каждое — подделки собраны заранее,
#: как `DIRTY_TRADES` в `tests/test_market_journal.py`, и по той же причине.
DIRTY_ORDERS: tuple[tuple[str, Callable[[OrderRecord], OrderRecord]], ...] = (
    ("order_id", lambda base: dataclasses.replace(base, order_id=FAKE_JWT)),
    ("action", lambda base: dataclasses.replace(base, action=FAKE_JWT)),
    ("reason", lambda base: dataclasses.replace(base, reason=FAKE_JWT)),
    ("exit_reason", lambda base: dataclasses.replace(base, exit_reason=FAKE_JWT)),
    ("target_id", lambda base: dataclasses.replace(base, target_id=FAKE_JWT)),
)


def test_every_text_column_of_an_order_has_a_dirty_probe() -> None:
    """Проба чистки есть у каждой текстовой колонки заявки, а не у части."""
    assert [name for name, _ in DIRTY_ORDERS] == list(_ORDER_TEXT_COLUMNS)


@pytest.mark.parametrize(("column", "spoil"), DIRTY_ORDERS, ids=[n for n, _ in DIRTY_ORDERS])
def test_no_text_field_of_an_order_reaches_the_disk_dirty(
    store, column: str, spoil: Callable[[OrderRecord], OrderRecord]
) -> None:
    """Имя заявки и причина придут из ответа брокера — чистится всё текстовое."""
    store.write_paper_rows(paper_run(store), PaperRows(mark=MARK, orders=(spoil(order()),)))
    ((value,),) = past_the_front_door(store, f"SELECT {column} FROM journal_order")
    assert "eyJGQUtF" not in str(value), f"{column} уехал на диск с токеном: {value}"
    assert SECRET_MASK in str(value)


# ------------------------------------------------------------- старая база


#: Сторожа строк журнала решений и сделок сборки 0.2.4 — дословно из
#: `market/storage.py` выпуска `0c33641`, где они написаны шестью телами руками.
#: Теперь их собирает таблица `_ROW_TABLES`, и собранное обязано совпасть
#: с этим текстом: иначе каждая база владельца при открытии объявит своих
#: сторожей подменёнными снаружи (`replaced_guards`).
#:
#: ⚠️ Текст — вход **извне** проверяемого правила. Взятый из нынешнего `_GUARDS`,
#: он совпадал бы с собой при любой правке таблицы: мутация, сменившая слово
#: в отказе дозаписи сделки, проходила такую сверку зелёной.
_PREVIOUS_ROW_GUARDS = """
CREATE TRIGGER journal_decision_is_append_only
        BEFORE UPDATE ON journal_decision
        BEGIN
            SELECT RAISE(
                ABORT,
                'строку журнала решений переписать нельзя: журнал — свидетельство'
            );
        END;

CREATE TRIGGER journal_trade_is_append_only
        BEFORE UPDATE ON journal_trade
        BEGIN
            SELECT RAISE(
                ABORT,
                'строку журнала сделок переписать нельзя: журнал — свидетельство'
            );
        END;

CREATE TRIGGER journal_decision_dies_with_its_run
        BEFORE DELETE ON journal_decision
        WHEN EXISTS (SELECT 1 FROM journal_session WHERE id = old.session_id)
        BEGIN
            SELECT RAISE(
                ABORT,
                'строку журнала решений удалить нельзя: она живёт, пока жив её прогон'
            );
        END;

CREATE TRIGGER journal_trade_dies_with_its_run
        BEFORE DELETE ON journal_trade
        WHEN EXISTS (SELECT 1 FROM journal_session WHERE id = old.session_id)
        BEGIN
            SELECT RAISE(
                ABORT,
                'сделку из журнала удалить нельзя: она живёт, пока жив её прогон'
            );
        END;

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
        END;

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
        END;
"""

_PREVIOUS_ROW_GUARD_NAMES = (
    "journal_decision_is_append_only",
    "journal_trade_is_append_only",
    "journal_decision_dies_with_its_run",
    "journal_trade_dies_with_its_run",
    "journal_decision_joins_an_open_run",
    "journal_trade_joins_an_open_run",
)


def _strip_to_the_previous_build(path: pathlib.Path) -> None:
    """База, какой её оставила сборка 0.2.4: без двух новых таблиц и их сторожей.

    Сторожа журнала решений и сделок ставятся текстом сборки 0.2.4
    (`_PREVIOUS_ROW_GUARDS`), а не тем, что сегодня собрал код.
    """
    connection = sqlite3.connect(path)
    try:
        for (name,) in list(connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' "
            "AND (name LIKE 'journal_order%' OR name LIKE 'journal_run_rows%')"
        )):
            connection.execute(f"DROP TRIGGER {name}")
        connection.execute("DROP TABLE journal_order")
        connection.execute("DROP TABLE journal_run_rows")
        for name in _PREVIOUS_ROW_GUARD_NAMES:
            connection.execute(f"DROP TRIGGER {name}")
        connection.commit()
        connection.executescript(_PREVIOUS_ROW_GUARDS)
        connection.commit()
    finally:
        connection.close()


def test_a_database_of_the_previous_build_keeps_its_journal_and_raises_no_alarm(
    tmp_path: pathlib.Path,
) -> None:
    """База прежней сборки: строки целы, сторожа не объявлены подменёнными.

    Сторожа журнала решений и сделок теперь собираются таблицей; тело,
    разошедшееся с лежащим в базе хоть словом, при открытии читалось бы
    как правка базы мимо программы (`replaced_guards`). Номер схемы
    прежний — откат на 0.2.4 открывает ту же базу.
    """
    path = tmp_path / "previous.sqlite3"
    with CandleStore(path) as old:
        live = old.open_journal_session(SessionRecord(RunOrigin.LIVE), now=AT).id
        old.write_decisions(live, rows().decisions)
        old.write_trades(live, [deal()])
    _strip_to_the_previous_build(path)

    with CandleStore(path) as store:
        assert store.replaced_guards == ()
        assert len(store.decisions(session_id=live)) == 1
        assert [one.record for one in store.trades(session_id=live)] == [deal()]
        found = {
            str(name): str(body)
            for name, body in past_the_front_door(
                store, "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"
            )
        }
        stale = [name for name, body in _GUARDS.items() if not _same_sql(found.get(name), body)]
        assert stale == []
        ((version,),) = past_the_front_door(store, "PRAGMA user_version")
        assert version == SCHEMA_VERSION == 7, "номер схемы поднят — откат на 0.2.4 потерян"
        store.write_paper_rows(paper_run(store), rows())


#: Таблица заявок, какой её заводила отладочная сборка до фазы, — дословно
#: из `market/storage.py` до правки 12.10.2026, со значениями `CHECK`
#: строками. Текст — вход **извне**: взятый из нынешней схемы, он нёс бы
#: ту самую колонку, чьё отсутствие здесь и нужно.
_ORDER_TABLE_WITHOUT_PHASE = """
CREATE TABLE journal_order (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES journal_session (id) ON DELETE CASCADE,
    order_id    TEXT    NOT NULL,
    at          INTEGER NOT NULL,
    kind        TEXT    NOT NULL CHECK (kind IN ('submitted', 'level_hit')),
    action      TEXT    NOT NULL,
    side        TEXT    NOT NULL CHECK (side IN ('long', 'short')),
    volume      REAL    NOT NULL,
    reason      TEXT    NOT NULL DEFAULT '',
    exit_reason TEXT    NOT NULL DEFAULT '',
    target_id   TEXT    NOT NULL DEFAULT '',
    close_price REAL,
    average     REAL,
    level       REAL,
    limit_price REAL
);

CREATE INDEX journal_order_of_session
    ON journal_order (session_id, id);
"""


def _order_table_without_phase(path: pathlib.Path, session: int) -> None:
    """Таблица заявок прежнего вида: без фазы, со строкой и со сторожами, как лежали.

    Сторожа снимаются и ставятся обратно **теми телами, что были в файле**:
    открытие обязано застать их на месте и не объявить подменёнными.
    """
    connection = sqlite3.connect(path)
    try:
        guards = list(connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'trigger' AND tbl_name = 'journal_order'"
        ))
        assert len(guards) == 3, f"сторожей заявок {len(guards)}, а не три — вход не тот"
        for name, _body in guards:
            connection.execute(f"DROP TRIGGER {name}")
        connection.execute("DROP TABLE journal_order")
        connection.executescript(_ORDER_TABLE_WITHOUT_PHASE)
        written = order()
        connection.execute(
            "INSERT INTO journal_order (session_id, order_id, at, kind, action, side, "
            "volume, reason, close_price, average) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session, written.order_id, int(written.at.timestamp()), written.kind.value,
                written.action, written.side.value, written.volume, written.reason,
                written.close_price, written.average,
            ),
        )
        for _name, body in guards:
            connection.execute(body)
        connection.commit()
    finally:
        connection.close()


def test_an_order_table_without_the_phase_gets_it_and_keeps_its_row(
    tmp_path: pathlib.Path,
) -> None:
    """Таблица заявок без фазы: колонка дописана при открытии, строка цела и читается пересчётом.

    Стережёт миграцию `journal_order` без подъёма номера схемы. Мутации:
    колонка не дописывается (чтение и запись падают), дописывается без
    `CHECK` (чужая фаза ложится), умолчание — живое (строка неизвестной фазы
    попадает в «как было»), открытие снимает сторожей заявок и не ставит
    их заново либо объявляет их подменёнными.
    """
    path = tmp_path / "debug-build.sqlite3"
    with CandleStore(path) as fresh:
        session = paper_run(fresh)
        fresh.write_paper_rows(session, PaperRows(mark=MARK))
    _order_table_without_phase(path, session)

    with CandleStore(path) as store:
        assert store.replaced_guards == ()
        columns = [
            str(row[1])
            for row in past_the_front_door(store, "PRAGMA table_info(journal_order)")
        ]
        assert columns[-1] == "phase", columns
        (kept,) = store.orders(session_id=session)
        assert kept.record == dataclasses.replace(order(), phase=DecisionPhase.RECOUNT)
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            past_the_front_door(
                store,
                "INSERT INTO journal_order "
                "(session_id, order_id, at, kind, action, side, volume, phase) "
                "VALUES (?, 'x', 0, 'submitted', 'open', 'long', 1, 'someday')",
                session,
            )
        with pytest.raises(sqlite3.IntegrityError, match="переписать нельзя"):
            past_the_front_door(store, "UPDATE journal_order SET phase = 'live'")
        ((version,),) = past_the_front_door(store, "PRAGMA user_version")
        assert version == SCHEMA_VERSION == 7, "номер схемы поднят — откат на 0.2.4 потерян"
        live = dataclasses.replace(order(), phase=DecisionPhase.LIVE)
        store.write_paper_rows(session, PaperRows(orders=(live,)))
        assert [one.phase for one in store.orders(session_id=session)] == [
            DecisionPhase.RECOUNT, DecisionPhase.LIVE,
        ]
    with CandleStore(path) as again:
        assert again.replaced_guards == ()
        assert len(again.orders(session_id=session)) == 2


@pytest.mark.parametrize("built", ["fresh", "migrated"])
def test_the_phase_column_behaves_alike_in_a_new_base_and_a_migrated_one(
    tmp_path: pathlib.Path, built: str
) -> None:
    """Фаза одинакова у новой базы и у дописанной: чужая отвергнута, без фазы — пересчёт.

    Колонку пишут два места — `CREATE TABLE` новой базы и `ALTER TABLE`
    базы отладочной сборки. Разойдись тексты — отказ записи и умолчание
    зависели бы от того, какой сборкой база создана, и молча. Мутации:
    у одного из двух мест снят `CHECK` либо умолчание живое.
    """
    path = tmp_path / f"{built}.sqlite3"
    with CandleStore(path) as first:
        session = paper_run(first)
        first.write_paper_rows(session, PaperRows(mark=MARK))
    if built == "migrated":
        _order_table_without_phase(path, session)
    with CandleStore(path) as store:
        past_the_front_door(
            store,
            "INSERT INTO journal_order (session_id, order_id, at, kind, action, side, volume) "
            "VALUES (?, 'no-phase', 0, 'submitted', 'open', 'long', 1)",
            session,
        )
        unnamed = [
            one for one in store.orders(session_id=session) if one.record.order_id == "no-phase"
        ]
        assert [one.phase for one in unnamed] == [DecisionPhase.RECOUNT], (
            "строка без фазы читается не пересчётом — попадёт в «как было»"
        )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            past_the_front_door(
                store,
                "INSERT INTO journal_order "
                "(session_id, order_id, at, kind, action, side, volume, phase) "
                "VALUES (?, 'x', 0, 'submitted', 'open', 'long', 1, 'someday')",
                session,
            )


# ------------------------------------------------------------- поток данных


def test_the_worker_moves_the_cursor_in_the_store_thread_and_only_on_success(
    tmp_path: pathlib.Path,
) -> None:
    """Колбэк записи зовётся в потоке базы и только после фиксации.

    На этом держится «успех записи не теряется при отмене ожидания»:
    ждущего могут снять, а колбэк к тому моменту уже сдвинул курсоры.
    """

    async def go() -> tuple[list[str], int]:
        threads: list[str] = []
        worker = MarketWorker(tmp_path / "w.sqlite3")
        await worker.open()
        try:
            session, _ = await worker.open_journal_session(SessionRecord(RunOrigin.PAPER))
            def seen(_written: PaperWritten) -> None:
                threads.append(threading.current_thread().name)

            await worker.write_paper_rows(session.id, rows(), on_written=seen)
            with pytest.raises(ValueError, match="без отметки"):
                await worker.write_paper_rows(
                    (await worker.open_journal_session(SessionRecord(RunOrigin.PAPER)))[0].id,
                    rows(mark=None),
                    on_written=seen,
                )
            return threads, len(await worker.orders(session_id=session.id))
        finally:
            await worker.close()

    threads, stored = asyncio.run(go())
    assert len(threads) == 1, "колбэк позван и на отказе — курсор ушёл бы за непринятую пачку"
    assert threads[0] != threading.main_thread().name, "колбэк позван не в потоке базы"
    assert stored == 2
