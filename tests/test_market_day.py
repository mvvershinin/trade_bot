"""День счёта из базы (`market.day`, решение 0066 Р7) и миграция журнала команд (`D-176`).

Каждый тест стережёт **одно** поведение, оно названо первой строкой докстринга.
Вход — база во временной папке; сеть и рабочая база владельца счёта
не участвуют (правило 14). Ожидаемые числа дня посчитаны **руками** из цен
сценария, а не тем же проходом, что проверяется: сторож, сверяющий функцию
с ней же, зеленеет вместе с её ошибкой.

«Перезапуск» — закрытие хранилища и новое открытие того же файла: всё,
что держалось в памяти первого объекта, второму недоступно.
"""

from __future__ import annotations

import pathlib
import sqlite3
import uuid
from dataclasses import replace
from datetime import date, datetime

import pytest

from market.commands import BrokerCommand, BrokerExecution, CommandAction, CommandSide
from market.day import OpeningFunds, TradingDay
from market.journal import RunOrigin, SessionRecord, TradeRecord, TradeSide
from market.storage import _GUARDS, SCHEMA_VERSION, CandleStore, _same_sql
from tests.market_helpers import msk
from tests.test_market_journal import past_the_front_door

ACCOUNT = "A-1"
OTHER = "B-2"
DAY = date(2026, 10, 28)
SYMBOL = "MXZ6"
#: Рублей за пункт — круглое число, чтобы ожидаемое считалось в уме.
WEIGHT = {SYMBOL: 10.0}


def is_take(order_id: str) -> bool:
    """Соглашение движка об имени тейка (`engine.pipeline.take_order_id`)."""
    return order_id.startswith("take:")


def key(number: int) -> str:
    return str(uuid.UUID(int=number))


def open_run(store: CandleStore, origin: RunOrigin = RunOrigin.LIVE) -> int:
    return store.open_journal_session(
        SessionRecord(origin, symbol=SYMBOL), now=msk(2026, 10, 27, 9, 0)
    ).id


def trade(
    store: CandleStore,
    session: int,
    number: int,
    *,
    side: CommandSide,
    order_id: str,
    fills: list[tuple[str, int, float, datetime, float | None]],
    account: str = ACCOUNT,
) -> None:
    """Команда и её исполнения: (executionId, объём, цена, момент, комиссия)."""
    store.record_broker_command(
        session,
        BrokerCommand(
            client_order_id=key(number),
            action=CommandAction.CREATE,
            order_id=order_id,
            symbol=SYMBOL,
            side=side,
            volume=sum(volume for _, volume, _, _, _ in fills),
            account=account,
        ),
        now=fills[0][3],
    )
    for execution_id, volume, price, at, commission in fills:
        store.record_broker_execution(
            BrokerExecution(execution_id, key(number), volume, price, at, commission)
        )


def funds(at: datetime, equity: float, account: str = ACCOUNT) -> OpeningFunds:
    return OpeningFunds(account=account, at=at, equity=equity, free=equity / 2)


def live_day(store: CandleStore, day: date = DAY, account: str = ACCOUNT) -> TradingDay:
    return store.executions_day(account, day, ruble_per_point=WEIGHT, is_take=is_take)


def a_morning(store: CandleStore) -> None:
    """Вчерашний вход, утренний тейк двумя исполнениями, шорт и «закрыть всё».

    * 27.10 18:00 — покупка 2 по 100, комиссия 2 (вход вчера);
    * 28.10 10:00 — тейк лонга: продажа 1 по 103 и 1 по 104, комиссия по 1;
    * 28.10 10:30 — вход в шорт: продажа 1 по 104, комиссия 1;
    * 28.10 11:00 — «закрыть всё» (сверка 0020): покупка 1 по 101, комиссия
      брокером не названа.
    """
    session = open_run(store)
    trade(store, session, 1, side=CommandSide.BUY, order_id="open:long:27",
          fills=[("E-1", 2, 100.0, msk(2026, 10, 27, 18, 0), 2.0)])
    trade(store, session, 2, side=CommandSide.SELL, order_id="take:long:27",
          fills=[("E-2", 1, 103.0, msk(2026, 10, 28, 10, 0), 1.0),
                 ("E-3", 1, 104.0, msk(2026, 10, 28, 10, 0), 1.0)])
    trade(store, session, 3, side=CommandSide.SELL, order_id="open:short:28",
          fills=[("E-4", 1, 104.0, msk(2026, 10, 28, 10, 30), 1.0)])
    trade(store, session, 4, side=CommandSide.BUY, order_id="reconcile:close-all",
          fills=[("E-5", 1, 101.0, msk(2026, 10, 28, 11, 0), None)])


#: День после `a_morning`, посчитанный руками.
#: Лонг: (103 − 100) × 10 + (104 − 100) × 10 = 70; шорт: (104 − 101) × 10 = 30.
#: Комиссия даты — три исполнения по 1; вчерашние 2 — вчера; одно без комиссии.
MORNING = TradingDay(
    account=ACCOUNT,
    day=DAY,
    opening=OpeningFunds(ACCOUNT, msk(2026, 10, 28, 9, 0), 1_000_000.0, 500_000.0),
    gross=100.0,
    commission=3.0,
    unknown_commissions=1,
    take_profit_date=DAY,
    last_exit_date=DAY,
)


# -- день боевого движка по исполнениям --------------------------------------


def test_the_day_after_a_restart_is_the_day_before_it(tmp_path: pathlib.Path) -> None:
    """День, восстановленный после перезапуска, тот же, что был до него, и верен.

    Счёт на утро — первый снимок даты, а не снимок после убытка, сделанный
    позже; результат дня — с тейком, разбитым на два исполнения, и с «закрыть
    всё»; даты тейка и выхода — сегодняшние. Ровно это движок держал в памяти,
    и ровно это теряет перезапуск без восстановления (0066, Р7).
    """
    path = tmp_path / "day.sqlite3"
    with CandleStore(path) as store:
        store.record_opening_funds(funds(msk(2026, 10, 28, 9, 0), 1_000_000.0))
        a_morning(store)
        store.record_opening_funds(funds(msk(2026, 10, 28, 10, 45), 990_000.0))
        before = live_day(store)

    with CandleStore(path) as store:
        after = live_day(store)

    assert before == MORNING
    assert after == MORNING
    assert after.realized == 97.0


def test_a_close_all_written_to_executions_counts_in_the_day(tmp_path: pathlib.Path) -> None:
    """Сделка «закрыть всё», идущая мимо движка, входит в результат и в дату выхода.

    Позицию открыл движок, закрыла сверка 0020 своей командой; у движка
    этой сделки нет вовсе. Без неё день после «Возобновить» начинался бы
    без убытка закрытия и без выхода — «один вход в день» разрешил бы второй.
    Тейком она не считается: имя не движка.
    """
    with CandleStore(tmp_path / "day.sqlite3") as store:
        session = open_run(store)
        trade(store, session, 1, side=CommandSide.BUY, order_id="open:long:28",
              fills=[("E-1", 1, 100.0, msk(2026, 10, 28, 9, 35), 1.0)])
        trade(store, session, 2, side=CommandSide.SELL, order_id="reconcile:close-all",
              fills=[("E-2", 1, 95.0, msk(2026, 10, 28, 9, 50), 1.0)])
        day = live_day(store)

    assert (day.gross, day.commission, day.realized) == (-50.0, 2.0, -52.0)
    assert (day.last_exit_date, day.take_profit_date) == (DAY, None)


def test_a_take_that_opened_a_position_is_not_a_take(tmp_path: pathlib.Path) -> None:
    """Исполнение тейка без позиции — вход в обратную сторону, а не тейк и не выход.

    Позицию уже закрыло «закрыть всё», а тейк у брокера снять не успели,
    и он исполнился (`OrderAction.ARM_TAKE_PROFIT`, «позиция в обратную
    сторону»). Принять это за тейк значило бы запретить входы до конца дня
    по сделке, которая прибыли не зафиксировала.
    """
    with CandleStore(tmp_path / "day.sqlite3") as store:
        session = open_run(store)
        trade(store, session, 1, side=CommandSide.BUY, order_id="open:long:28",
              fills=[("E-1", 1, 100.0, msk(2026, 10, 28, 9, 35), 1.0)])
        trade(store, session, 2, side=CommandSide.SELL, order_id="reconcile:close-all",
              fills=[("E-2", 1, 99.0, msk(2026, 10, 28, 9, 50), 1.0)])
        trade(store, session, 3, side=CommandSide.SELL, order_id="take:long:28",
              fills=[("E-3", 1, 101.0, msk(2026, 10, 28, 9, 55), 1.0)])
        day = live_day(store)

    assert (day.gross, day.commission) == (-10.0, 3.0)
    assert day.take_profit_date is None


def test_a_day_asked_for_yesterday_does_not_know_today(tmp_path: pathlib.Path) -> None:
    """День прошлой даты не видит сегодняшних сделок: только вход и его комиссию."""
    with CandleStore(tmp_path / "day.sqlite3") as store:
        a_morning(store)
        yesterday = live_day(store, date(2026, 10, 27))

    assert (yesterday.gross, yesterday.commission) == (0.0, 2.0)
    assert (yesterday.take_profit_date, yesterday.last_exit_date) == (None, None)


def test_another_account_does_not_leak_into_the_day(tmp_path: pathlib.Path) -> None:
    """Исполнения чужого счёта в день счёта не попадают — и обратно.

    В `userdata/` бывает два токена (находка `/risk`, Э4 шаг 1): день,
    собранный по всем исполнениям базы, приписал бы счёту чужой тейк.
    """
    with CandleStore(tmp_path / "day.sqlite3") as store:
        a_morning(store)
        session = open_run(store)
        trade(store, session, 9, side=CommandSide.BUY, order_id="open:long:28",
              fills=[("E-9", 1, 50.0, msk(2026, 10, 28, 9, 40), 1.0)], account=OTHER)
        trade(store, session, 10, side=CommandSide.SELL, order_id="take:long:28",
              fills=[("E-10", 1, 60.0, msk(2026, 10, 28, 9, 45), 1.0)], account=OTHER)
        mine = live_day(store)
        theirs = live_day(store, account=OTHER)

    assert (mine.gross, mine.commission) == (MORNING.gross, MORNING.commission)
    assert (theirs.gross, theirs.commission, theirs.unknown_commissions) == (100.0, 2.0, 0)


def test_an_execution_without_a_command_is_named_not_counted(tmp_path: pathlib.Path) -> None:
    """Исполнение без команды (заявка руками у брокера) названо поимённо и не учтено.

    Стороны у него нет — вход это или выход, не сказать. Молча выбросить
    нельзя (правило 13): не пустой `foreign` — результат дня неполон.
    """
    with CandleStore(tmp_path / "day.sqlite3") as store:
        a_morning(store)
        store.record_broker_execution(
            BrokerExecution("E-hand", "hand-made", 3, 99.0, msk(2026, 10, 28, 12, 0), 5.0)
        )
        store.record_broker_execution(
            BrokerExecution("E-old", "hand-old", 1, 99.0, msk(2026, 10, 20, 12, 0), 5.0)
        )
        day = live_day(store)

    assert day.foreign == ("E-hand",)
    assert (day.gross, day.commission) == (MORNING.gross, MORNING.commission)


def test_an_exit_without_its_point_weight_is_refused(tmp_path: pathlib.Path) -> None:
    """Выход по тикеру без веса пункта — отказ, а не пункты, выданные за рубли."""
    with CandleStore(tmp_path / "day.sqlite3") as store:
        a_morning(store)
        with pytest.raises(ValueError, match=SYMBOL):
            store.executions_day(ACCOUNT, DAY, ruble_per_point={}, is_take=is_take)


# -- счёт на утро --------------------------------------------------------------


def test_the_morning_snapshot_is_the_first_of_its_date(tmp_path: pathlib.Path) -> None:
    """Первый снимок даты остаётся базой дня; следующая дата начинает свою.

    Ответ записи — лежащий снимок, а не поданный: вызывающий видит, от чего
    считается день, а не то, что прислал опрос.
    """
    path = tmp_path / "day.sqlite3"
    first = funds(msk(2026, 10, 28, 9, 0), 1_000_000.0)
    with CandleStore(path) as store:
        assert store.record_opening_funds(first) == first
        assert store.record_opening_funds(funds(msk(2026, 10, 28, 15, 0), 900_000.0)) == first
        tomorrow = store.record_opening_funds(funds(msk(2026, 10, 29, 0, 5), 900_000.0))
        assert store.record_opening_funds(funds(msk(2026, 10, 28, 9, 0), 1.0, OTHER)).equity == 1.0

    with CandleStore(path) as store:
        assert store.opening_funds(ACCOUNT, DAY) == first
        assert store.opening_funds(ACCOUNT, date(2026, 10, 29)) == tomorrow
        assert store.opening_funds(ACCOUNT, date(2026, 10, 30)) is None


@pytest.mark.parametrize(
    ("sql", "refusal"),
    [
        ("UPDATE account_opening SET equity = 1", "не переписывается"),
        ("DELETE FROM account_opening", "не удаляется"),
        (
            "INSERT OR REPLACE INTO account_opening VALUES ('A-1', '2026-10-28', 0, 1, 1, NULL)",
            "не удаляется",
        ),
    ],
)
def test_the_morning_snapshot_is_not_replaced_past_the_front_door(
    tmp_path: pathlib.Path, sql: str, refusal: str
) -> None:
    """Сторожа в базе: счёт на утро не правится, не удаляется и не заменяется."""
    first = funds(msk(2026, 10, 28, 9, 0), 1_000_000.0)
    with CandleStore(tmp_path / "day.sqlite3") as store:
        store.record_opening_funds(first)
        with pytest.raises(sqlite3.IntegrityError, match=refusal):
            past_the_front_door(store, sql)
        assert store.opening_funds(ACCOUNT, DAY) == first


# -- день симуляции по журналу сделок ----------------------------------------


def test_the_simulation_day_comes_from_its_trade_journal(tmp_path: pathlib.Path) -> None:
    """День симуляции — из журнала сделок **её** прогона, тем же счётом, что бой.

    Вход вчера и выход по тейку сегодня: сегодня валовый результат и половина
    комиссии (сторона выхода). Сделка без тарифа — две стороны без комиссии.
    Сделка другого прогона симуляции в день не входит.
    """
    path = tmp_path / "day.sqlite3"
    taken = TradeRecord(
        symbol=SYMBOL, side=TradeSide.LONG, volume=1,
        entry_time=msk(2026, 10, 27, 15, 0), entry_price=100.0,
        exit_time=msk(2026, 10, 28, 10, 0), exit_price=105.0,
        exit_reason="Тейк-профит", gross=50.0, commission=4.0, net=46.0,
        entry_order_id="open:long:27", exit_order_id="take:long:27", ruble_per_point=10.0,
    )
    untariffed = TradeRecord(
        symbol=SYMBOL, side=TradeSide.SHORT, volume=1,
        entry_time=msk(2026, 10, 28, 10, 30), entry_price=105.0,
        exit_time=msk(2026, 10, 28, 11, 0), exit_price=107.0,
        exit_reason="Обратный сигнал", gross=-20.0,
        entry_order_id="open:short:28", exit_order_id="close:short:28", ruble_per_point=10.0,
    )
    with CandleStore(path) as store:
        store.record_opening_funds(funds(msk(2026, 10, 28, 9, 0), 1_000_000.0))
        paper = open_run(store, RunOrigin.PAPER)
        store.write_trades(paper, [taken, untariffed])
        elsewhere = open_run(store, RunOrigin.PAPER)
        store.write_trades(
            elsewhere, [replace(taken, exit_time=msk(2026, 10, 28, 10, 5), gross=1_000.0)]
        )
        before = store.journal_day(paper, DAY, account=ACCOUNT, is_take=is_take)

    with CandleStore(path) as store:
        after = store.journal_day(paper, DAY, account=ACCOUNT, is_take=is_take)

    expected = TradingDay(
        account=ACCOUNT, day=DAY, opening=MORNING.opening,
        gross=30.0, commission=2.0, unknown_commissions=2,
        take_profit_date=DAY, last_exit_date=DAY,
    )
    assert before == expected
    assert after == expected


def test_a_simulation_day_asked_for_yesterday_does_not_know_today_exit(
    tmp_path: pathlib.Path,
) -> None:
    """День симуляции задним числом не знает выхода, случившегося после него.

    Сделка вошла вчера и вышла по тейку сегодня: в журнале прогона она одна
    строка, и вчерашний день её видит (вход до конца дня). Выход сегодня
    вчерашнему дню чужой: ни даты тейка, ни даты выхода, ни результата —
    только комиссия входа.
    """
    taken = TradeRecord(
        symbol=SYMBOL, side=TradeSide.LONG, volume=1,
        entry_time=msk(2026, 10, 27, 15, 0), entry_price=100.0,
        exit_time=msk(2026, 10, 28, 10, 0), exit_price=105.0,
        exit_reason="Тейк-профит", gross=50.0, commission=4.0, net=46.0,
        entry_order_id="open:long:27", exit_order_id="take:long:27", ruble_per_point=10.0,
    )
    with CandleStore(tmp_path / "day.sqlite3") as store:
        paper = open_run(store, RunOrigin.PAPER)
        store.write_trades(paper, [taken])
        yesterday = store.journal_day(
            paper, date(2026, 10, 27), account=None, is_take=is_take
        )

    assert (yesterday.take_profit_date, yesterday.last_exit_date) == (None, None)
    assert (yesterday.gross, yesterday.commission) == (0.0, 2.0)


def test_a_day_of_a_run_that_does_not_exist_is_refused(tmp_path: pathlib.Path) -> None:
    """Номер несуществующего прогона — отказ, а не пустой день «сделок не было»."""
    with CandleStore(tmp_path / "day.sqlite3") as store:
        with pytest.raises(LookupError):
            store.journal_day(77, DAY, account=ACCOUNT, is_take=is_take)


# -- миграция журнала команд шага 1 (D-176) ----------------------------------

#: Таблица команд и её сторожа — **дословно** из базы, созданной сборкой
#: шага 1 (`547346c`), сняты с копии базы владельца счёта 28.10.2026;
#: перенос строки в списке колонок сравнению сторожей безразличен (`_same_sql`).
#: Не выводятся из нынешнего кода и не должны: это чужая, уже неизменяемая
#: схема. Выведенная, она поехала бы вслед за кодом, и тест перестал бы
#: видеть, что новая сборка на базе шага 1 бьёт тревогу.
STEP_ONE = """
CREATE TABLE broker_command (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id             INTEGER NOT NULL,
    client_order_id        TEXT    NOT NULL UNIQUE,
    action                 TEXT    NOT NULL CHECK (action IN ('create', 'cancel')),
    order_id               TEXT    NOT NULL,
    symbol                 TEXT    NOT NULL,
    side                   TEXT    CHECK (side IN ('buy', 'sell')),
    volume                 INTEGER CHECK (volume >= 1),
    price                  REAL,
    target_client_order_id TEXT,
    at                     INTEGER NOT NULL,
    status                 TEXT    NOT NULL,
    status_at              INTEGER NOT NULL,
    reason                 TEXT    NOT NULL DEFAULT '',
    CHECK (
        (action = 'create' AND side IS NOT NULL AND volume IS NOT NULL
         AND target_client_order_id IS NULL)
        OR (action = 'cancel' AND target_client_order_id IS NOT NULL
            AND side IS NULL AND volume IS NULL AND price IS NULL)
    )
);
CREATE TRIGGER broker_command_closed_is_final
        BEFORE UPDATE OF status, status_at, reason ON broker_command
        WHEN old.status IN ('refused', 'rejected', 'filled', 'cancelled')
        BEGIN
            SELECT RAISE(
                ABORT,
                'команда брокеру закрыта: её статус и причина больше не меняются'
            );
        END;
CREATE TRIGGER broker_command_is_kept
        BEFORE DELETE ON broker_command
        BEGIN
            SELECT RAISE(
                ABORT,
                'команду брокеру удалить нельзя: по ней сверяют заявки у брокера и деньги на счёте'
            );
        END;
CREATE TRIGGER broker_command_terms_are_final
        BEFORE UPDATE OF id, session_id, client_order_id, action, order_id,
                         symbol, side, volume, price, target_client_order_id,
                         at ON broker_command
        BEGIN
            SELECT RAISE(
                ABORT,
                'условия команды брокеру не меняются после записи: только её статус'
            );
        END;
"""


def step_one_base(path: pathlib.Path) -> None:
    """База сборки шага 1: схема 7, журнал команд без счёта, строки в нём.

    Остальное — как у нынешней сборки: шаг 1 ничего другого не менял.
    """
    with CandleStore(path):
        pass
    connection = sqlite3.connect(path)
    for (name,) in list(connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' "
        "AND (name LIKE 'broker_command_%' OR name LIKE 'account_opening_%')"
    )):
        connection.execute(f"DROP TRIGGER {name}")
    connection.execute("DROP TABLE broker_command")
    connection.execute("DROP TABLE account_opening")
    connection.executescript(STEP_ONE)
    at = int(msk(2026, 10, 27, 10, 0).timestamp())
    connection.execute(
        "INSERT INTO journal_session (origin, started_at) VALUES ('live', ?)", (at,)
    )
    connection.execute(
        "INSERT INTO broker_command (session_id, client_order_id, action, order_id, "
        "symbol, side, volume, at, status, status_at) "
        "VALUES (1, ?, 'create', 'open:long:27', 'MXZ6', 'buy', 1, ?, 'filled', ?)",
        (key(1), at, at),
    )
    connection.execute(
        "INSERT INTO broker_execution (execution_id, client_order_id, volume, price, at) "
        "VALUES ('E-1', ?, 1, 100.0, ?)",
        (key(1), at),
    )
    connection.commit()
    connection.close()


def schema_of(store: CandleStore) -> list[tuple]:
    return past_the_front_door(
        store, "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
    )


def counts(store: CandleStore) -> tuple[int, ...]:
    return tuple(
        int(past_the_front_door(store, f"SELECT COUNT(*) FROM {table}")[0][0])
        for table in ("journal_session", "broker_command", "broker_execution")
    )


def test_a_base_of_step_one_migrates_without_alarm_and_keeps_its_rows(
    tmp_path: pathlib.Path,
) -> None:
    """База шага 1 получает колонку счёта без тревоги о подмене и без потери строк.

    Прежнее тело сторожа условий команды — своё, а не подменённое снаружи
    (`_SUPERSEDED_GUARDS`); после открытия стоят нынешние тела. Колонки — те же
    и в том же порядке, что у новой базы. Номер схемы прежний: откат на 0.2.5
    цел (шапка `SCHEMA_VERSION`). Строка шага 1 без счёта лежит, но не читается
    командой: сверка с чужим счётом хуже громкого отказа.
    """
    path = tmp_path / "step-one.sqlite3"
    step_one_base(path)
    with CandleStore(tmp_path / "fresh.sqlite3") as fresh:
        wanted = past_the_front_door(fresh, "PRAGMA table_info(broker_command)")

    with CandleStore(path) as store:
        found = dict(past_the_front_door(
            store, "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"
        ))
        columns = past_the_front_door(store, "PRAGMA table_info(broker_command)")
        ((version,),) = past_the_front_door(store, "PRAGMA user_version")
        rows = counts(store)
        with pytest.raises(ValueError, match="счёт"):
            store.broker_command(key(1))
        session = open_run(store)
        trade(store, session, 2, side=CommandSide.SELL, order_id="close:long:28",
              fills=[("E-2", 1, 101.0, msk(2026, 10, 28, 10, 0), None)])

    assert store.replaced_guards == ()
    assert [name for name, body in _GUARDS.items() if not _same_sql(found.get(name), body)] == []
    assert columns == wanted
    assert version == SCHEMA_VERSION == 7
    assert rows == (1, 1, 1)


def test_an_execution_of_a_step_one_command_is_named_not_counted(
    tmp_path: pathlib.Path,
) -> None:
    """Исполнение по команде шага 1 (счёт не записан) — названо в `foreign`, в день не входит.

    Чей это счёт, не сказать: приписать его счёту дня значило бы взять
    чужую сделку, выбросить молча — скрыть, что результат дня неполон.
    """
    path = tmp_path / "step-one.sqlite3"
    step_one_base(path)
    with CandleStore(path) as store:
        day = live_day(store, date(2026, 10, 27))

    assert day.foreign == ("E-1",)
    assert (day.gross, day.commission, day.unknown_commissions) == (0.0, 0.0, 0)


def test_a_second_open_of_a_migrated_base_changes_nothing(tmp_path: pathlib.Path) -> None:
    """Повтор миграции ничего не меняет: та же схема, те же строки, тревоги нет."""
    path = tmp_path / "step-one.sqlite3"
    step_one_base(path)
    with CandleStore(path) as store:
        first = (schema_of(store), counts(store))
    with CandleStore(path) as store:
        second = (schema_of(store), counts(store))

    assert store.replaced_guards == ()
    assert second == first
