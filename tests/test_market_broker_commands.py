"""Журнал команд брокеру и таблица исполнений (`market.commands`).

Журнал — граница безопасности денег: настоящая заявка уходит брокеру только
после возврата из записи (план Э4 §2.2 п. 4, С5), и дубль заявки начинается
с потерянной записи. Каждый тест стережёт **одно** поведение, и оно названо
первой строкой его докстринга. Вход — база во временной папке; сеть
и рабочая база владельца счёта не участвуют (правило 14).

«Видит второе соединение» проверяется настоящим вторым соединением
с тем же файлом: ответ своего же соединения фиксации не доказывает — оно
видит и незафиксированное.
"""

from __future__ import annotations

import math
import pathlib
import sqlite3
import uuid
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

import pytest

from market.commands import (
    BrokerCommand,
    BrokerExecution,
    CommandAction,
    CommandSide,
    CommandStatus,
)
from market.journal import SECRET_MASK, RunOrigin, SessionRecord
from market.storage import (
    _COMMAND_CHECKED_COLUMNS,
    _COMMAND_KEY_COLUMNS,
    _COMMAND_TEXT_COLUMNS,
    _EXECUTION_TEXT_COLUMNS,
    _GUARDS,
    SCHEMA_VERSION,
    CandleStore,
    _same_sql,
)
from tests.market_helpers import msk
from tests.test_market_journal import FAKE_JWT, past_the_front_door

AT = msk(2026, 10, 27, 10, 5)
LATER = AT + timedelta(minutes=1)


def key(number: int) -> str:
    """Ключ команды: настоящий UUID в каноническом написании, но предсказуемый."""
    return str(uuid.UUID(int=number))


def create(
    number: int = 1, *, volume: int = 1, order_id: str = "entry:1", symbol: str = "MXZ6"
) -> BrokerCommand:
    return BrokerCommand(
        client_order_id=key(number),
        action=CommandAction.CREATE,
        order_id=order_id,
        symbol=symbol,
        side=CommandSide.BUY,
        volume=volume,
    )


def fill(execution_id: str = "E-1", *, volume: int = 1) -> BrokerExecution:
    return BrokerExecution(
        execution_id=execution_id,
        client_order_id=key(1),
        volume=volume,
        price=230_550.0,
        at=AT,
        commission=None,
    )


def open_run(store: CandleStore, origin: RunOrigin = RunOrigin.LIVE) -> int:
    return store.open_journal_session(SessionRecord(origin, symbol="MXZ6"), now=AT).id


def elsewhere(path: pathlib.Path, sql: str, *args: object) -> list[tuple]:
    """Запрос через **другое** соединение: видно только зафиксированное."""
    connection = sqlite3.connect(path)
    try:
        return list(connection.execute(sql, args))
    finally:
        connection.close()


class _CommitFails:
    """Соединение, у которого не проходит фиксация: диск полон, база занята.

    Всё остальное — как у настоящего, поэтому до `COMMIT` запись идёт
    обычным путём, и падает ровно то место, после которого вернуться можно.
    """

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real

    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> sqlite3.Cursor:
        if sql.strip().upper() == "COMMIT":
            raise sqlite3.OperationalError("disk I/O error")
        return self._real.execute(sql, parameters)

    def __getattr__(self, name: str) -> object:
        return getattr(self._real, name)


# -- запись: возврат только после фиксации ------------------------------------


def test_a_recorded_command_is_seen_by_another_connection(tmp_path: pathlib.Path) -> None:
    """Запись и смена статуса возвращаются только зафиксированными.

    Строку видит второе соединение с тем же файлом сразу после возврата,
    пока хранилище ещё открыто: заявка уходит брокеру по возврату, и строка,
    видимая лишь своему соединению, не переживёт падения программы.
    """
    path = tmp_path / "commands.sqlite3"
    with CandleStore(path) as store:
        stored = store.record_broker_command(open_run(store), create(), now=AT)
        after_record = elsewhere(path, "SELECT client_order_id, status FROM broker_command")
        store.set_broker_command_status(key(1), CommandStatus.ACCEPTED, now=LATER)
        after_status = elsewhere(path, "SELECT status FROM broker_command")

    assert stored.status is CommandStatus.RECORDED
    assert after_record == [(key(1), "recorded")]
    assert after_status == [("accepted",)]


def test_a_failed_commit_raises_and_leaves_no_row(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Не зафиксировалось — исключение наружу, строки нет: заявка не уйдёт.

    Проглоченный сбой фиксации дал бы возврат без строки, то есть отправку
    заявки, о которой после перезапуска журнал не знает.
    """
    path = tmp_path / "commands.sqlite3"
    with CandleStore(path) as store:
        session = open_run(store)
        monkeypatch.setattr(store, "_db", _CommitFails(vars(store)["_db"]))
        with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
            store.record_broker_command(session, create(), now=AT)
        seen = elsewhere(path, "SELECT COUNT(*) FROM broker_command")
        monkeypatch.undo()
        # Соединение не осталось внутри брошенной транзакции: следующая запись идёт.
        store.record_broker_command(session, create(2), now=AT)

    assert seen == [(0,)]


def test_a_command_of_a_run_that_does_not_exist_is_refused(tmp_path: pathlib.Path) -> None:
    """Команда несуществующему прогону — отказ, и строки нет.

    Номер, не выданный базой, ничего не связывает: строка такой команды
    не нашлась бы ни по прогону, ни по ходу, который её подал.
    """
    path = tmp_path / "commands.sqlite3"
    with CandleStore(path) as store, pytest.raises(LookupError, match="прогона 77"):
        store.record_broker_command(77, create(), now=AT)

    assert elsewhere(path, "SELECT COUNT(*) FROM broker_command") == [(0,)]


# -- повтор ключа: отказ, а не замена -----------------------------------------


@pytest.mark.parametrize(
    "repeat",
    [create(1), create(1, volume=2, order_id="entry:2")],
    ids=["same-content", "other-content"],
)
def test_a_repeated_key_is_refused_not_replaced(
    tmp_path: pathlib.Path, repeat: BrokerCommand
) -> None:
    """Повтор UUID — исключение, и лежит первая строка, одна.

    Тихий пропуск повтора (`INSERT OR IGNORE`) и замена (`INSERT OR REPLACE`)
    одинаково плохи: вызывающий считает записанной заявку, которой в журнале
    нет, либо журнал забывает заявку, которая у брокера уже есть.
    """
    path = tmp_path / "commands.sqlite3"
    with CandleStore(path) as store:
        session = open_run(store)
        store.record_broker_command(session, create(1), now=AT)
        with pytest.raises((ValueError, sqlite3.IntegrityError)):
            store.record_broker_command(session, repeat, now=LATER)
        seen = elsewhere(
            path,
            "SELECT order_id, volume, at FROM broker_command WHERE client_order_id = ?",
            key(1),
        )

    assert seen == [("entry:1", 1, int(AT.timestamp()))]


@pytest.mark.parametrize(
    ("sql", "refusal"),
    [
        (
            "INSERT OR REPLACE INTO broker_command (session_id, client_order_id, action, "
            "order_id, symbol, side, volume, at, status, status_at) VALUES "
            f"(1, '{key(1)}', 'create', 'entry:2', 'MXZ6', 'sell', 2, 0, 'recorded', 0)",
            "удалить нельзя",
        ),
        ("DELETE FROM broker_command", "удалить нельзя"),
        ("UPDATE broker_command SET volume = 2", "не меняются после записи"),
        ("UPDATE broker_command SET client_order_id = 'x'", "не меняются после записи"),
        ("DELETE FROM broker_execution", "удалить нельзя"),
        ("UPDATE broker_execution SET price = 1", "переписать нельзя"),
    ],
    ids=["replace", "delete", "volume", "key", "delete-fill", "rewrite-fill"],
)
def test_the_base_refuses_rewriting_past_the_front_door(
    tmp_path: pathlib.Path, sql: str, refusal: str
) -> None:
    """Сторожа в базе: команду и исполнение не заменить, не удалить, не переписать.

    Правка снаружи — из просмотрщика баз или чужого скрипта — обходит
    проверки кода; держит только сторож в самой базе.
    """
    with CandleStore(tmp_path / "commands.sqlite3") as store:
        store.record_broker_command(open_run(store), create(1), now=AT)
        store.record_broker_execution(fill())
        with pytest.raises(sqlite3.IntegrityError, match=refusal):
            past_the_front_door(store, sql)
        stored = store.broker_command(key(1))

    assert stored is not None
    assert (stored.command.order_id, stored.command.volume) == ("entry:1", 1)


# -- исполнения: повтор события — одна строка ---------------------------------


def test_a_repeated_execution_is_one_row(tmp_path: pathlib.Path) -> None:
    """Повтор `executionId` — одна строка, и второй вызов говорит «уже лежало».

    Поток статусов вправе прислать одно исполнение дважды; вторая строка —
    вторая сделка в отчёте и удвоенная позиция в сверке.
    """
    path = tmp_path / "commands.sqlite3"
    with CandleStore(path) as store:
        first = store.record_broker_execution(fill())
        second = store.record_broker_execution(fill())
        seen = elsewhere(
            path, "SELECT COUNT(*) FROM broker_execution WHERE execution_id = 'E-1'"
        )
        read = store.broker_executions(key(1))

    assert (first, second) == (True, False)
    assert seen == [(1,)]
    assert read == (fill(),)


def test_a_failed_commit_of_an_execution_raises_and_leaves_no_row(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Исполнение не зафиксировалось — исключение наружу, строки нет.

    Проглоченный сбой — сделка у брокера есть, в журнале нет, и вызывающий
    об этом не узнал: сверка после перезапуска не досчитается позиции.
    Мутация, выжившая до этого теста (test-expert, 27.10.2026): сбой записи
    исполнения проглочен.
    """
    path = tmp_path / "commands.sqlite3"
    with CandleStore(path) as store:
        monkeypatch.setattr(store, "_db", _CommitFails(vars(store)["_db"]))
        with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
            store.record_broker_execution(fill())
        seen = elsewhere(path, "SELECT COUNT(*) FROM broker_execution")
        monkeypatch.undo()
        # Соединение не осталось внутри брошенной транзакции: повтор ложится.
        retried = store.record_broker_execution(fill())

    assert seen == [(0,)]
    assert retried is True


def test_a_repeated_execution_with_other_content_is_refused(tmp_path: pathlib.Path) -> None:
    """Тот же `executionId` с другим объёмом — отказ, лежит первое.

    Какому из двух верить, из базы не видно; молча оставленное первое
    скрыло бы противоречие, молча записанное второе — подменило бы сделку.
    """
    with CandleStore(tmp_path / "commands.sqlite3") as store:
        store.record_broker_execution(fill(volume=1))
        with pytest.raises(ValueError, match="E-1 уже записано с другим содержимым"):
            store.record_broker_execution(fill(volume=2))
        read = store.broker_executions(key(1))

    assert read == (fill(volume=1),)


# -- строки переживают прогон -------------------------------------------------


def test_commands_and_executions_outlive_their_run(tmp_path: pathlib.Path) -> None:
    """Удаление прогона не уносит ни команды, ни исполнения — и не запрещается ими.

    Живая заявка у брокера остаётся живой, что бы ни стало с прогоном,
    который её подал. Запрет удаления был бы не лучше: чистка прогонов
    по истории идёт внутри открытия каждого прогона.
    """
    path = tmp_path / "commands.sqlite3"
    with CandleStore(path) as store:
        history = open_run(store, RunOrigin.BACKTEST)
        store.record_broker_command(history, create(), now=AT)
        store.record_broker_execution(fill())
        report = store.forget_journal_sessions([history])
        gone = store.journal_session(history)
        still_open = [one.command.client_order_id for one in store.open_broker_commands()]
        executions = store.broker_executions(key(1))

    assert report.sessions == 1
    assert gone is None
    assert still_open == [key(1)]
    assert executions == (fill(),)
    assert elsewhere(path, "SELECT session_id FROM broker_command") == [(history,)]


def test_commands_outlive_the_rebuild_of_the_run_table(tmp_path: pathlib.Path) -> None:
    """Пересборка таблицы прогонов (миграция 3 → 4) возвращает команды и исполнения целыми.

    База доводится до вида схемы 3 снаружи: без второй заметки прогона
    и с номером 3 — ровно то, по чему открытие решает пересобирать таблицу.
    """
    path = tmp_path / "commands.sqlite3"
    with CandleStore(path) as store:
        live = open_run(store)
        store.record_broker_command(live, create(), now=AT)
        store.record_broker_execution(fill())
    connection = sqlite3.connect(path)
    connection.execute("DROP TRIGGER journal_session_final_note_comes_with_the_close")
    connection.execute("ALTER TABLE journal_session DROP COLUMN finish_note")
    connection.execute("PRAGMA user_version = 3")
    connection.commit()
    connection.close()

    with CandleStore(path) as store:
        rebuilt = {
            str(row[1])
            for row in past_the_front_door(store, "PRAGMA table_info(journal_session)")
        }
        stored = store.broker_command(key(1))
        executions = store.broker_executions(key(1))
        store.set_broker_command_status(key(1), CommandStatus.FILLED, now=LATER)

    assert "finish_note" in rebuilt, "пересборка не прошла — тест ничего не проверил"
    assert stored is not None
    assert (stored.session_id, stored.status) == (live, CommandStatus.RECORDED)
    assert executions == (fill(),)


# -- чистка текста -----------------------------------------------------------


def test_texts_of_commands_and_executions_go_through_the_store_cleaner(
    tmp_path: pathlib.Path,
) -> None:
    """Имя заявки, тикер, причина и ключи исполнения проходят чистку хранилища.

    Чистка — та, что передана хранилищу (`app/` передаёт `broker.redaction.
    scrub`), а не своя: иначе живые значения токена прошли бы на диск.
    """
    path = tmp_path / "commands.sqlite3"

    def cleaner(text: str) -> str:
        return text.replace("SECRET", "[clean]")

    with CandleStore(path, sanitize=cleaner) as store:
        session = open_run(store)
        store.record_broker_command(
            session,
            create(order_id="entry:SECRET", symbol="MXSECRET"),
            reason="подана: SECRET",
            now=AT,
        )
        store.record_broker_command(session, create(2), now=AT)
        store.set_broker_command_status(
            key(2), CommandStatus.REFUSED, reason="отказ: SECRET", now=LATER
        )
        store.record_broker_execution(
            BrokerExecution("E-SECRET", "order-SECRET", 1, 230_550.0, AT)
        )
        on_disk = elsewhere(
            path, "SELECT order_id, symbol, reason FROM broker_command ORDER BY id"
        ) + elsewhere(path, "SELECT execution_id, client_order_id FROM broker_execution")

    assert "SECRET" not in repr(on_disk)
    assert on_disk[0] == ("entry:[clean]", "MX[clean]", "подана: [clean]")
    assert on_disk[1][2] == "отказ: [clean]"


def test_a_broker_refusal_with_a_token_is_cleaned_by_default(tmp_path: pathlib.Path) -> None:
    """Причина отказа с токеном внутри ложится на диск без токена и при чистке по умолчанию."""
    path = tmp_path / "commands.sqlite3"
    leaky = f'Брокер ответил: {{"access_token": "{FAKE_JWT}"}}'
    with CandleStore(path) as store:
        store.record_broker_command(open_run(store), create(), now=AT)
        store.set_broker_command_status(key(1), CommandStatus.REFUSED, reason=leaky, now=LATER)
        ((reason,),) = elsewhere(path, "SELECT reason FROM broker_command")

    assert FAKE_JWT not in reason
    assert SECRET_MASK in reason


@pytest.mark.parametrize(
    ("table", "expected"),
    [
        (
            "broker_command",
            set(_COMMAND_TEXT_COLUMNS) | set(_COMMAND_KEY_COLUMNS) | set(_COMMAND_CHECKED_COLUMNS),
        ),
        ("broker_execution", set(_EXECUTION_TEXT_COLUMNS)),
    ],
)
def test_every_text_column_is_cleaned_checked_or_a_key(
    tmp_path: pathlib.Path, table: str, expected: set[str]
) -> None:
    """Каждая текстовая колонка — в списке чистки, проверки по словарю или ключей.

    Колонка, заведённая завтра и не попавшая ни в один список, легла бы
    на диск нечищеной, и ни один тест про причину этого бы не заметил.
    """
    with CandleStore(tmp_path / "commands.sqlite3") as store:
        text = {
            str(row[1])
            for row in past_the_front_door(store, f"PRAGMA table_info({table})")
            if str(row[2]).upper() == "TEXT"
        }

    assert text == expected


# -- статус и незакрытые ------------------------------------------------------


def test_open_commands_are_the_ones_not_closed(tmp_path: pathlib.Path) -> None:
    """Незакрытые — записана, принята, исход неизвестен; остальные закрыты.

    Список ожидаемых написан руками по плану Э4 §2.4–§2.5, а не выведен
    из `CommandStatus.closed`: выведенный, он зеленел бы вместе с ошибкой.
    """
    with CandleStore(tmp_path / "commands.sqlite3") as store:
        session = open_run(store)
        for number, status in enumerate(CommandStatus, start=1):
            store.record_broker_command(session, create(number), now=AT)
            if status is not CommandStatus.RECORDED:
                store.set_broker_command_status(key(number), status, now=LATER)
        opened = [one.status for one in store.open_broker_commands()]

    assert opened == [CommandStatus.RECORDED, CommandStatus.ACCEPTED, CommandStatus.UNKNOWN]


def test_a_status_the_program_does_not_know_is_not_taken_for_closed(
    tmp_path: pathlib.Path,
) -> None:
    """Незнакомый статус в базе роняет выбор незакрытых, а не выпадает из него.

    Выпавшая из выборки команда — живая заявка у брокера, которую сверка
    не увидит; отказ чтения — остановка, которую увидит человек.
    """
    with CandleStore(tmp_path / "commands.sqlite3") as store:
        store.record_broker_command(open_run(store), create(), now=AT)
        past_the_front_door(store, "UPDATE broker_command SET status = 'cancelling'")
        with pytest.raises(ValueError, match="cancelling"):
            store.open_broker_commands()


def test_a_closed_command_stays_closed(tmp_path: pathlib.Path) -> None:
    """Закрытая команда не открывается и не переписывается — ни кодом, ни снаружи.

    Исполненную заявку, объявленную снова живой, сверка поехала бы искать
    у брокера; другой конечный статус поверх — переписанное свидетельство.
    """
    with CandleStore(tmp_path / "commands.sqlite3") as store:
        store.record_broker_command(open_run(store), create(), now=AT)
        filled = store.set_broker_command_status(key(1), CommandStatus.FILLED, now=AT)
        with pytest.raises((ValueError, sqlite3.IntegrityError), match="закрыта"):
            store.set_broker_command_status(key(1), CommandStatus.ACCEPTED, now=LATER)
        again = store.set_broker_command_status(
            key(1), CommandStatus.FILLED, reason="повтор события", now=LATER
        )
        with pytest.raises(sqlite3.IntegrityError, match="закрыта"):
            past_the_front_door(store, "UPDATE broker_command SET status = 'accepted'")

    assert again == filled


def test_a_repeated_open_status_keeps_the_moment_it_began(tmp_path: pathlib.Path) -> None:
    """Повтор того же открытого статуса ничего не пишет: время статуса — когда он наступил."""
    with CandleStore(tmp_path / "commands.sqlite3") as store:
        store.record_broker_command(open_run(store), create(), now=AT)
        first = store.set_broker_command_status(key(1), CommandStatus.UNKNOWN, now=AT)
        again = store.set_broker_command_status(key(1), CommandStatus.UNKNOWN, now=LATER)

    assert again.status_at == first.status_at == AT


@pytest.mark.parametrize(
    ("status", "wanted"),
    [(CommandStatus.RECORDED, ValueError), (CommandStatus.ACCEPTED, LookupError)],
    ids=["recorded-by-update", "unknown-key"],
)
def test_status_change_refusals(
    tmp_path: pathlib.Path, status: CommandStatus, wanted: type[Exception]
) -> None:
    """«Записана» ставит только запись; статус незнакомой команды не ставится."""
    with CandleStore(tmp_path / "commands.sqlite3") as store:
        store.record_broker_command(open_run(store), create(), now=AT)
        target = key(1) if wanted is ValueError else key(9)
        with pytest.raises(wanted):
            store.set_broker_command_status(target, status, now=LATER)


# -- форма команды -----------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"price": math.nan}, id="nan-price"),
        pytest.param({"price": math.inf}, id="infinite-price"),
        pytest.param({"client_order_id": key(0xABCDEF).upper()}, id="uppercase-key"),
        pytest.param({"client_order_id": "entry:1"}, id="not-a-uuid"),
        pytest.param({"side": None}, id="create-without-side"),
        pytest.param({"volume": 0}, id="zero-volume"),
        pytest.param({"volume": True}, id="bool-volume"),
        pytest.param({"order_id": " "}, id="no-engine-name"),
        pytest.param(
            {"action": CommandAction.CANCEL, "target_client_order_id": key(2)},
            id="cancel-with-side-and-volume",
        ),
        pytest.param(
            {
                "action": CommandAction.CANCEL,
                "side": None,
                "volume": None,
                "target_client_order_id": key(1),
            },
            id="cancel-of-itself",
        ),
    ],
)
def test_a_malformed_command_is_refused_before_the_base(change: dict[str, Any]) -> None:
    """Команда неверной формы не создаётся: `NaN` в цене прочитался бы заявкой по рынку."""
    fields: dict[str, Any] = {
        "client_order_id": key(1),
        "action": CommandAction.CREATE,
        "order_id": "entry:1",
        "symbol": "MXZ6",
        "side": CommandSide.BUY,
        "volume": 1,
    } | change
    with pytest.raises(ValueError):
        BrokerCommand(**fields)


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"execution_id": " "}, id="empty-execution-id"),
        pytest.param({"client_order_id": ""}, id="no-order-key"),
        pytest.param({"volume": 0}, id="zero-volume"),
        pytest.param({"volume": True}, id="bool-volume"),
        pytest.param({"price": math.nan}, id="nan-price"),
        pytest.param({"commission": math.inf}, id="infinite-commission"),
        pytest.param({"at": AT.replace(tzinfo=None)}, id="naive-time"),
    ],
)
def test_a_malformed_execution_is_refused_before_the_base(change: dict[str, Any]) -> None:
    """Исполнение неверной формы не создаётся: пустой `executionId` не отсечёт повтор.

    Пустой ключ лёг бы один раз, и все следующие исполнения без ключа стали бы
    «повтором» — потерянными сделками. Мутация, выжившая до этого теста
    (test-expert, 27.10.2026): проверки `BrokerExecution` проглочены.
    """
    fields: dict[str, Any] = {
        "execution_id": "E-1",
        "client_order_id": key(1),
        "volume": 1,
        "price": 230_550.0,
        "at": AT,
        "commission": None,
    } | change
    with pytest.raises(ValueError):
        BrokerExecution(**fields)


def test_a_cancel_command_round_trips(tmp_path: pathlib.Path) -> None:
    """Отмена ложится со своим ключом и ключом исходной, без стороны и объёма."""
    cancel = BrokerCommand(
        client_order_id=key(2),
        action=CommandAction.CANCEL,
        order_id="take:1",
        symbol="MXZ6",
        target_client_order_id=key(1),
    )
    with CandleStore(tmp_path / "commands.sqlite3") as store:
        stored = store.record_broker_command(open_run(store), cancel, now=AT)

    assert stored.command == cancel


# -- откат на прежнюю сборку -------------------------------------------------


def test_a_base_of_the_previous_build_opens_without_alarm_and_keeps_its_number(
    tmp_path: pathlib.Path,
) -> None:
    """База сборки 0.2.5 (без журнала команд) открывается молча, номер схемы прежний.

    Поднятый номер отнял бы откат на 0.2.5 без копии `userdata/`, ничего
    не защитив: настоящих заявок та сборка не подаёт.
    """
    path = tmp_path / "previous.sqlite3"
    with CandleStore(path):
        pass
    connection = sqlite3.connect(path)
    for (name,) in list(connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'broker_%'"
    )):
        connection.execute(f"DROP TRIGGER {name}")
    connection.execute("DROP TABLE broker_command")
    connection.execute("DROP TABLE broker_execution")
    connection.commit()
    connection.close()

    with CandleStore(path) as store:
        found = {
            str(name): str(body)
            for name, body in past_the_front_door(
                store, "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"
            )
        }
        ((version,),) = past_the_front_door(store, "PRAGMA user_version")
        store.record_broker_command(open_run(store), create(), now=AT)

    assert store.replaced_guards == ()
    assert [name for name, body in _GUARDS.items() if not _same_sql(found.get(name), body)] == []
    assert version == SCHEMA_VERSION == 7
