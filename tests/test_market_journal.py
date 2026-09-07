"""Журнал сделок и журнал решений: запись, чтение, сторожа, чистка, миграция.

Пункт приёмки, который держится на этих проверках, — «Записи не теряются при
перезапуске программы» (ТЗ §4.6). Плюс три требования, которые дороже кода:

* строку журнала нельзя переписать задним числом;
* по прочитанной строке всегда видно, были ли это настоящие деньги;
* боевую запись программа не удаляет никаким путём.

⚠️ В файле лежат строки формы токена — иначе чистку секретов проверять нечем.
Все они **подделка-для-теста**: значения нарочно написаны так, что принять их
за настоящий токен нельзя, и объявление стоит здесь ради детектора секретов
из `tests/test_layers.py`. Настоящее значение в этот файл попасть не должно
никогда — детектор его больше не увидит.
"""

from __future__ import annotations

import contextlib
import dataclasses
import pathlib
import sqlite3
import subprocess
import sys
import textwrap
from collections.abc import Callable, Iterator, Sequence
from datetime import datetime, timedelta
from typing import Literal

import pytest

from market.journal import (
    BACKTEST_SESSIONS_KEPT,
    SECRET_MASK,
    DecisionLevel,
    DecisionRecord,
    JournalSession,
    PruneReport,
    RunOrigin,
    SessionRecord,
    TradeRecord,
    TradeSide,
    redact,
)
from market.reports import LoadReport
from market.storage import (
    SCHEMA_VERSION,
    CandleStore,
    _CLOSING_SESSION_COLUMNS,
    _FROZEN_SESSION_COLUMNS,
    _GUARDS,
    _SESSION_COLUMNS,
    _SESSION_COLUMNS_BEFORE_TWO_NOTES,
    _SESSION_SELECT,
    _SESSION_TEXT_COLUMNS,
    _TRADE_IDENTITY,
    _same_sql,
)

from market_helpers import msk

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture
def store(tmp_path: pathlib.Path):
    with CandleStore(tmp_path / "userdata" / "candles.sqlite3") as opened:
        yield opened


def past_the_front_door(store: CandleStore, sql: str, *args: object) -> list[tuple]:
    """Запрос прямо в базу, мимо публичного входа хранилища.

    Сторожа журнала — триггеры с `RAISE(ABORT)` и `CHECK` на колонках —
    публичным путём проверить невозможно **по построению**: публичного пути
    переписать строку журнала, сменить происхождение прогона или удалить
    боевой прогон нет и быть не должно. Проверять их всё равно надо: сторож,
    который никто не пробовал, однажды окажется выключённым правкой схемы.

    Обращение к приватному соединению собрано здесь одним местом. Иначе
    связанность с внутренностями хранилища расползлась бы на семь строк,
    и каждая ломалась бы отдельно.
    """
    return list(store._db.execute(sql, args))  # noqa: SLF001 — см. докстринг


def stored_session(store: CandleStore, session_id: int) -> JournalSession:
    """Прогон по номеру. Отсутствие прогона — падение теста, а не `None`.

    Чтение отдаёт `JournalSession | None`, и в проверках интересен только
    первый случай: если прогона нет, тест обязан сказать об этом прямо,
    а не упасть на обращении к полю.
    """
    found = store.journal_session(session_id)
    assert found is not None, f"прогон {session_id} не найден в журнале"
    return found


def decision(
    at: datetime, event: str = "Вход в лонг", reason: str = "Закрытие выше EMA(15)"
) -> DecisionRecord:
    return DecisionRecord(at=at, event=event, reason=reason, level=DecisionLevel.TRADE)


def trade(entry: datetime, *, price: float = 100.0, gain: float = 1.0) -> TradeRecord:
    return TradeRecord(
        symbol="MXU6",
        side=TradeSide.LONG,
        volume=1.0,
        entry_time=entry,
        entry_price=price,
        exit_time=entry + timedelta(minutes=5),
        exit_price=price + gain,
        exit_reason="обратный сигнал средней",
        gross=gain,
        entry_order_id="вход-1",
        exit_order_id="выход-1",
    )


# -- запись и чтение ---------------------------------------------------------


def test_a_decision_survives_a_close_and_reopen(tmp_path: pathlib.Path) -> None:
    """Главный пункт приёмки: журнал переживает перезапуск программы."""
    path = tmp_path / "candles.sqlite3"
    at = msk(2026, 9, 3, 10, 10)
    with CandleStore(path) as store:
        session = store.open_journal_session(
            SessionRecord(RunOrigin.LIVE, symbol="MXU6"), now=at
        )
        store.write_decision(session.id, decision(at))
        store.write_trades(session.id, [trade(at)])

    with CandleStore(path) as reopened:
        (row,) = reopened.decisions()
        assert row.event == "Вход в лонг"
        assert row.origin is RunOrigin.LIVE
        (deal,) = reopened.trades()
        assert deal.entry_price == 100.0
        assert deal.origin is RunOrigin.LIVE


def test_the_written_record_comes_back_field_for_field(store: CandleStore) -> None:
    """Запись и чтение — одна и та же строка, а не похожая."""
    at = msk(2026, 9, 3, 10, 10)
    session = store.open_journal_session(SessionRecord(RunOrigin.BACKTEST), now=at)
    written = decision(at, "Тейк выставлен", "Уровень 100,5 = вход + 0,5%")
    store.write_decision(session.id, written)

    (read_back,) = store.decisions()
    assert read_back.record == written


def test_a_trade_comes_back_field_for_field(store: CandleStore) -> None:
    at = msk(2026, 9, 3, 10, 10)
    session = store.open_journal_session(SessionRecord(RunOrigin.BACKTEST), now=at)
    written = trade(at)
    store.write_trades(session.id, [written])

    (read_back,) = store.trades()
    assert read_back.record == written


def test_the_order_of_lines_is_kept_even_at_the_same_second(store: CandleStore) -> None:
    """Порядок держится на `id`, а не на времени.

    У десяти строк одной свечи время закрытия одно и то же: сортировка
    по времени перемешала бы их, и журнал перестал бы читаться как рассказ.
    """
    at = msk(2026, 9, 3, 10, 10)
    session = store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)
    events = ["Сигнал", "Заявка отправлена", "Лонг открыт", "Тейк выставлен"]
    store.write_decisions(session.id, [decision(at, event=name) for name in events])

    assert [row.event for row in store.decisions()] == events


def test_a_limit_cuts_the_old_lines_not_the_fresh_ones(store: CandleStore) -> None:
    """Лимит отсекает старое. Обратное уже было дефектом у `gap_log`."""
    at = msk(2026, 9, 3, 10, 10)
    session = store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)
    store.write_decisions(
        session.id, [decision(at, event=f"строка {number}") for number in range(10)]
    )

    tail = store.decisions(limit=3)
    assert [row.event for row in tail] == ["строка 7", "строка 8", "строка 9"]


def test_a_naive_time_is_refused(store: CandleStore) -> None:
    """Время без пояса — отказ, а не догадка: иначе журнал уедет на три часа."""
    session = store.open_journal_session(
        SessionRecord(RunOrigin.LIVE), now=msk(2026, 9, 3, 10, 0)
    )
    naive = DecisionRecord(at=datetime(2026, 9, 3, 10, 10), event="что-то", reason="")
    with pytest.raises(ValueError, match="без часового пояса"):
        store.write_decision(session.id, naive)


def test_a_line_without_a_session_is_impossible(store: CandleStore) -> None:
    """Строка ссылается на прогон обязательным ключом. Внешний ключ включён."""
    at = msk(2026, 9, 3, 10, 10)
    with pytest.raises(sqlite3.IntegrityError):
        store.write_decision(9999, decision(at))


def test_time_comes_back_in_moscow(store: CandleStore) -> None:
    at = msk(2026, 9, 3, 10, 10)
    session = store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)
    store.write_decision(session.id, decision(at))

    (row,) = store.decisions()
    assert row.at == at
    assert row.at.utcoffset() == timedelta(hours=3)


# -- происхождение -----------------------------------------------------------


def test_every_read_line_says_whether_it_was_real_money(store: CandleStore) -> None:
    """Отчёт, по которому нельзя сказать про настоящие деньги, — не отчёт."""
    at = msk(2026, 9, 3, 10, 10)
    live = store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)
    paper = store.open_journal_session(SessionRecord(RunOrigin.PAPER), now=at)
    history = store.open_journal_session(SessionRecord(RunOrigin.BACKTEST), now=at)
    for session in (live, paper, history):
        store.write_decision(session.id, decision(at))
        store.write_trades(session.id, [trade(at)])

    origins = {row.session_id: row.origin for row in store.decisions(all_runs=True)}
    assert origins == {
        live.id: RunOrigin.LIVE,
        paper.id: RunOrigin.PAPER,
        history.id: RunOrigin.BACKTEST,
    }
    assert [row.real_money for row in store.decisions(all_runs=True)] == [True, False, False]
    assert [row.real_money for row in store.trades(all_runs=True)] == [True, False, False]


def test_reading_can_be_narrowed_to_one_origin(store: CandleStore) -> None:
    at = msk(2026, 9, 3, 10, 10)
    live = store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)
    history = store.open_journal_session(SessionRecord(RunOrigin.BACKTEST), now=at)
    store.write_decision(live.id, decision(at, event="боевая"))
    store.write_decision(history.id, decision(at, event="прогон"))

    only_live = store.decisions(origin=RunOrigin.LIVE)
    assert [row.event for row in only_live] == ["боевая"]


def test_the_origin_of_a_run_can_never_be_changed(store: CandleStore) -> None:
    """Сторож стоит в базе, а не в соглашении между разработчиками."""
    session = store.open_journal_session(
        SessionRecord(RunOrigin.BACKTEST), now=msk(2026, 9, 3, 10, 0)
    )
    with pytest.raises(sqlite3.IntegrityError, match="условия прогона не меняются"):
        past_the_front_door(
            store,
            "UPDATE journal_session SET origin = 'live' WHERE id = ?",
            session.id,
        )
    assert stored_session(store, session.id).origin is RunOrigin.BACKTEST


def test_an_unknown_origin_is_refused_by_the_database(store: CandleStore) -> None:
    """Четвёртого происхождения не бывает: `CHECK` отвергает его на вставке."""
    with pytest.raises(sqlite3.IntegrityError):
        past_the_front_door(
            store,
            "INSERT INTO journal_session (origin, started_at) VALUES ('почти боевой', 0)",
        )


# -- журнал как свидетельство ------------------------------------------------


def test_a_decision_line_cannot_be_rewritten(store: CandleStore) -> None:
    at = msk(2026, 9, 3, 10, 10)
    session = store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)
    written = store.write_decision(session.id, decision(at))

    with pytest.raises(sqlite3.IntegrityError, match="переписать нельзя"):
        past_the_front_door(
            store,
            "UPDATE journal_decision SET reason = 'другая причина' WHERE id = ?",
            written,
        )
    assert store.decisions()[0].reason == "Закрытие выше EMA(15)"


def test_a_trade_line_cannot_be_rewritten(store: CandleStore) -> None:
    at = msk(2026, 9, 3, 10, 10)
    session = store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)
    (written,) = store.write_trades(session.id, [trade(at)])

    with pytest.raises(sqlite3.IntegrityError, match="переписать нельзя"):
        past_the_front_door(
            store, "UPDATE journal_trade SET exit_price = 999 WHERE id = ?", written
        )


def test_a_live_run_cannot_be_deleted_at_all(store: CandleStore) -> None:
    """Ни чисткой, ни прямым `DELETE`, ни просьбой человека."""
    at = msk(2026, 9, 3, 10, 10)
    session = store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)
    store.write_decision(session.id, decision(at))

    with pytest.raises(sqlite3.IntegrityError, match="не удаляется"):
        past_the_front_door(
            store, "DELETE FROM journal_session WHERE id = ?", session.id
        )
    with pytest.raises(sqlite3.IntegrityError, match="не удаляется"):
        store.forget_journal_sessions([session.id])
    assert len(store.decisions()) == 1


def test_finishing_a_run_does_not_touch_its_lines(store: CandleStore) -> None:
    """`finished_at` дописывается прогону, а не строкам: строки неприкосновенны."""
    at = msk(2026, 9, 3, 10, 10)
    session = store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)
    store.write_decision(session.id, decision(at))
    store.finish_journal_session(session.id, now=at + timedelta(hours=1))

    finished = stored_session(store, session.id)
    assert finished.finished_at == at + timedelta(hours=1)
    assert len(store.decisions()) == 1


def test_a_run_left_open_stays_open(store: CandleStore) -> None:
    """Аварийное завершение видно: у прогона нет времени конца.

    Проставить его задним числом при следующем запуске было бы неправдой
    ровно в том месте, где человек ищет, что случилось.
    """
    session = store.open_journal_session(
        SessionRecord(RunOrigin.LIVE), now=msk(2026, 9, 3, 10, 0)
    )
    assert stored_session(store, session.id).finished_at is None


def test_a_killed_process_does_not_lose_written_lines(tmp_path: pathlib.Path) -> None:
    """Строки переживают SIGKILL — то есть аварийное завершение программы.

    Проверяется настоящим убитым процессом, а не закрытым файлом: `close()`
    доводит до диска всё, и на нём этот вопрос не проверяется вовсе.
    Долговечность здесь держится на `synchronous = FULL` — каждая транзакция
    сброшена на диск до возврата из вызова.
    """
    path = tmp_path / "candles.sqlite3"
    script = textwrap.dedent(
        f"""
        import os, sys
        sys.path.insert(0, {str(REPO_ROOT)!r})
        from datetime import datetime, timedelta, timezone
        from market.journal import DecisionRecord, RunOrigin, SessionRecord
        from market.storage import CandleStore

        msk = timezone(timedelta(hours=3), "MSK")
        at = datetime(2026, 9, 3, 10, 10, tzinfo=msk)
        store = CandleStore({str(path)!r})
        session = store.open_journal_session(
            SessionRecord(RunOrigin.LIVE, symbol="MXU6"), now=at
        )
        store.write_decisions(session.id, [
            DecisionRecord(at, "Лонг открыт", "Объём 1 контракт"),
            DecisionRecord(at, "Тейк выставлен", "Уровень 100,5"),
        ])
        os.kill(os.getpid(), 9)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == -9, (
        f"процесс не был убит, проверка вырождается: код {result.returncode}, "
        f"{result.stderr}"
    )

    with CandleStore(path) as reopened:
        rows = reopened.decisions()
        assert [row.event for row in rows] == ["Лонг открыт", "Тейк выставлен"]
        # Прогон остался незакрытым — ровно так и должно выглядеть падение.
        (session,) = reopened.journal_sessions()
        assert session.finished_at is None


# -- токен -------------------------------------------------------------------

#: Приметная подделка формы JWT. Символы те же, что у настоящего токена
#: (иначе проверка шла бы мимо образцов), а прочитать её можно только как
#: «не настоящий». Тот же приём, что у подделок в `test_broker_no_leak`.
FAKE_JWT = "eyJGQUtF-NOT-A-REAL.eyJGQUtF-NOT-A-REAL.FAKE-NOT-A-REAL-TOKEN"

#: Формы, в которых токен приходит от сервера. Ровно они и попадают в текст
#: отказа исполнителя, а оттуда — в строку журнала решений (`engine/`
#: кладёт в неё текст исключения целиком).
LEAKY_TEXTS = (
    f'Брокер ответил: {{"access_token": "{FAKE_JWT}"}}',
    f"Заголовок: Authorization: Bearer {FAKE_JWT}",
    f"Тело запроса: refresh_token={FAKE_JWT}&grant=x",
    f"В ответе пришло {FAKE_JWT}",
    f'Наш файл: {{"token": "{FAKE_JWT}"}}',
    # Имя поля до 06.09.2026: такие файлы лежат в резервных копиях папки данных.
    f'Наш файл прежнего формата: {{"токен": "{FAKE_JWT}"}}',
)


@pytest.mark.parametrize("text", LEAKY_TEXTS)
def test_a_token_shaped_text_never_reaches_the_disk(
    store: CandleStore, text: str
) -> None:
    """Чистка идёт на записи: на чтении токен уже лежал бы в файле."""
    at = msk(2026, 9, 3, 10, 10)
    session = store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)
    store.write_decision(
        session.id, DecisionRecord(at, "Заявка отклонена", text)
    )

    # Смысл проверки — в том, что лежит на диске, а не в том, что отдаёт
    # публичное чтение: чистка на чтении оставила бы токен в файле.
    ((raw,),) = past_the_front_door(
        store, "SELECT reason FROM journal_decision WHERE session_id = ?", session.id
    )
    assert "eyJGQUtF" not in raw, f"токен уехал на диск: {raw}"
    assert SECRET_MASK in raw


def test_the_event_and_the_run_note_are_cleaned_too(store: CandleStore) -> None:
    """Не только причина: чистится весь текст, который человек потом прочтёт."""
    at = msk(2026, 9, 3, 10, 10)
    secret = f"Bearer {FAKE_JWT}"
    session = store.open_journal_session(
        SessionRecord(RunOrigin.LIVE, settings=f"подключение {secret}", note=secret),
        now=at,
    )
    store.write_decision(session.id, DecisionRecord(at, f"Отказ {secret}", "причина"))
    store.write_trades(
        session.id,
        [
            TradeRecord(
                symbol="MXU6",
                side=TradeSide.LONG,
                volume=1.0,
                entry_time=at,
                entry_price=100.0,
                exit_time=at + timedelta(minutes=5),
                exit_price=101.0,
                exit_reason=f"отказ брокера {secret}",
                gross=1.0,
            )
        ],
    )

    read_session = stored_session(store, session.id)
    assert "eyJGQUtF" not in read_session.settings
    assert "eyJGQUtF" not in read_session.note
    assert "eyJGQUtF" not in store.decisions()[0].event
    assert "eyJGQUtF" not in store.trades()[0].exit_reason


def test_the_supplied_cleaner_is_the_one_that_runs(tmp_path: pathlib.Path) -> None:
    """`app/` подставляет чистку `broker/` — у неё есть реестр живых значений.

    Образцы не знают значения токена, выданного этим сеансом: его нет
    ни в одной известной форме. Именно ради этого случая хранилище берёт
    чистку снаружи, а не считает свою достаточной.
    """
    live_value = "секретное-значение-этого-сеанса"

    def with_registry(text: str) -> str:
        return redact(text).replace(live_value, SECRET_MASK)

    at = msk(2026, 9, 3, 10, 10)
    with CandleStore(tmp_path / "candles.sqlite3", sanitize=with_registry) as store:
        session = store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)
        store.write_decision(
            session.id, DecisionRecord(at, "Отказ", f"сервер вернул {live_value}")
        )
        assert live_value not in store.decisions()[0].reason
        assert SECRET_MASK in store.decisions()[0].reason


def test_the_cleaner_is_idempotent() -> None:
    """Текст проходит через чистку дважды — маска не должна ломать сообщение."""
    once = redact(f"Authorization: Bearer {FAKE_JWT}")
    assert redact(once) == once


def test_an_ordinary_phrase_is_not_touched() -> None:
    """Чистка, портящая обычные строки, будет выключена в тот же день."""
    phrase = "Вход в лонг: закрытие 312 500 выше EMA(15) 311 900 → лонг 1 контракт"
    assert redact(phrase) == phrase


# -- объём и чистка ----------------------------------------------------------


def test_old_history_runs_are_dropped_when_a_new_one_opens(store: CandleStore) -> None:
    """Конкретное поведение, а не «когда-нибудь почистим»."""
    at = msk(2026, 9, 3, 10, 10)
    for number in range(BACKTEST_SESSIONS_KEPT + 5):
        session = store.open_journal_session(SessionRecord(RunOrigin.BACKTEST), now=at)
        store.write_decision(session.id, decision(at, event=f"прогон {number}"))

    kept = store.journal_sessions(origin=RunOrigin.BACKTEST, limit=1000)
    assert len(kept) == BACKTEST_SESSIONS_KEPT
    assert store.pruned_journal.sessions == 1
    assert store.pruned_journal.decisions == 1
    events = {row.event for row in store.decisions(limit=1000, all_runs=True)}
    assert "прогон 0" not in events, "старый прогон удалён, а его строки остались"
    assert f"прогон {BACKTEST_SESSIONS_KEPT + 4}" in events


def test_the_cleanup_never_touches_live_or_paper(store: CandleStore) -> None:
    """Чистится только то, что можно повторить командой."""
    at = msk(2026, 9, 3, 10, 10)
    live = store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)
    paper = store.open_journal_session(SessionRecord(RunOrigin.PAPER), now=at)
    store.write_decision(live.id, decision(at, event="боевая"))
    store.write_decision(paper.id, decision(at, event="симуляция"))
    for _ in range(BACKTEST_SESSIONS_KEPT + 5):
        store.open_journal_session(SessionRecord(RunOrigin.BACKTEST), now=at)

    events = {row.event for row in store.decisions(limit=1000, all_runs=True)}
    assert events == {"боевая", "симуляция"}


def test_a_cleanup_that_would_eat_the_new_run_is_refused(store: CandleStore) -> None:
    with pytest.raises(ValueError, match="prune_journal"):
        store.open_journal_session(
            SessionRecord(RunOrigin.BACKTEST),
            now=msk(2026, 9, 3, 10, 0),
            keep_backtest_sessions=0,
        )


def test_prune_zero_clears_history_runs_but_not_the_rest(store: CandleStore) -> None:
    at = msk(2026, 9, 3, 10, 10)
    live = store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)
    store.write_decision(live.id, decision(at, event="боевая"))
    history = store.open_journal_session(SessionRecord(RunOrigin.BACKTEST), now=at)
    store.write_decisions(history.id, [decision(at), decision(at)])

    report = store.prune_journal(keep_backtest_sessions=0)
    assert (report.sessions, report.decisions) == (1, 2)
    assert "прогоны по истории не хранятся вовсе" in report.summary()
    assert [row.event for row in store.decisions(all_runs=True)] == ["боевая"]


def test_an_empty_cleanup_says_so_instead_of_counting_nothing() -> None:
    assert PruneReport().empty
    assert "удалять нечего" in PruneReport().summary()


def test_a_negative_threshold_is_refused(store: CandleStore) -> None:
    with pytest.raises(ValueError, match="отрицательное"):
        store.prune_journal(keep_backtest_sessions=-1)


def test_the_stats_answer_the_question_about_size(store: CandleStore) -> None:
    """Про объём отвечают числом, а не догадкой."""
    at = msk(2026, 9, 3, 10, 10)
    live = store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)
    store.write_decisions(live.id, [decision(at), decision(at), decision(at)])
    store.write_trades(live.id, [trade(at)])
    store.open_journal_session(SessionRecord(RunOrigin.BACKTEST), now=at)

    stats = store.journal_stats()
    assert (stats.sessions, stats.decisions, stats.trades) == (2, 3, 1)
    assert dict(stats.sessions_by_origin) == {RunOrigin.BACKTEST: 1, RunOrigin.LIVE: 1}
    assert stats.database_bytes > 0


def test_forgetting_a_run_takes_its_lines_with_it(store: CandleStore) -> None:
    at = msk(2026, 9, 3, 10, 10)
    history = store.open_journal_session(SessionRecord(RunOrigin.BACKTEST), now=at)
    store.write_decisions(history.id, [decision(at), decision(at)])
    store.write_trades(history.id, [trade(at)])

    report = store.forget_journal_sessions([history.id])
    assert (report.sessions, report.decisions, report.trades) == (1, 2, 1)
    assert report.kept is None
    assert "по просьбе человека" in report.summary()
    assert store.journal_stats().sessions == 0


# -- миграция схемы ----------------------------------------------------------


def test_a_database_of_schema_two_gets_the_journals(tmp_path: pathlib.Path) -> None:
    """База со свечами, но без журналов, дописывается при открытии.

    Проверяется на базе, у которой уже есть данные: пустая база ничего
    не доказывает — таблицы в ней создаются с нуля в любом случае.
    """
    path = tmp_path / "schema-two.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE minute_candle (
            symbol TEXT NOT NULL, ts INTEGER NOT NULL,
            open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
            close REAL NOT NULL, volume REAL NOT NULL,
            source TEXT NOT NULL, source_rank INTEGER NOT NULL,
            PRIMARY KEY (symbol, ts)
        ) WITHOUT ROWID;
        """
    )
    minute = int(msk(2026, 8, 26, 10, 5).timestamp())
    connection.execute(
        "INSERT INTO minute_candle VALUES ('MXU6', ?, 100, 101, 99, 100, 5, 'iss', 2)",
        (minute,),
    )
    connection.execute("PRAGMA user_version = 2")
    connection.commit()
    connection.close()

    at = msk(2026, 9, 3, 10, 10)
    with CandleStore(path) as store:
        ((version,),) = past_the_front_door(store, "PRAGMA user_version")
        assert version == SCHEMA_VERSION
        # Свечи на месте: миграция журналов их не касается.
        assert len(store.minutes("MXU6")) == 1
        session = store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)
        store.write_decision(session.id, decision(at))
        assert len(store.decisions()) == 1


def test_a_database_of_schema_one_still_repairs_its_minute_keys(
    tmp_path: pathlib.Path,
) -> None:
    """Цепочка миграций не потеряла первый шаг при появлении второго."""
    path = tmp_path / "schema-one.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE minute_candle (
            symbol TEXT NOT NULL, ts INTEGER NOT NULL,
            open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
            close REAL NOT NULL, volume REAL NOT NULL,
            source TEXT NOT NULL, source_rank INTEGER NOT NULL,
            PRIMARY KEY (symbol, ts)
        ) WITHOUT ROWID;
        """
    )
    minute = int(msk(2026, 8, 26, 10, 5).timestamp())
    connection.execute(
        "INSERT INTO minute_candle VALUES ('MXU6', ?, 100, 101, 99, 100, 5, 'broker', 1)",
        (minute + 7,),
    )
    connection.execute("PRAGMA user_version = 1")
    connection.commit()
    connection.close()

    with CandleStore(path) as store:
        assert store.migrated_minutes == 1
        (candle,) = store.minutes("MXU6")
        assert candle.time == msk(2026, 8, 26, 10, 5)


def test_a_database_of_the_new_schema_is_not_migrated_again(
    tmp_path: pathlib.Path,
) -> None:
    """Открытие готовой базы не запускает миграцию ключей повторно."""
    path = tmp_path / "candles.sqlite3"
    with CandleStore(path):
        pass
    with CandleStore(path) as store:
        assert store.migrated_minutes == 0


# -- что считается свидетельством --------------------------------------------


@pytest.mark.parametrize(
    ("origin", "real_money", "evidence"),
    [
        (RunOrigin.LIVE, True, True),
        (RunOrigin.PAPER, False, True),
        (RunOrigin.BACKTEST, False, False),
    ],
)
def test_the_three_origins_are_classified_once(
    origin: RunOrigin, real_money: bool, evidence: bool
) -> None:
    """Два разных вопроса, и ответы на них не совпадают.

    «Настоящие ли это деньги» и «можно ли эту запись удалить» — разные
    вопросы: симуляция на боевом потоке денег не двигала, но повторить её
    нельзя. Слить их в один признак значило бы либо чистить неповторимое,
    либо копить воспроизводимое вечно.
    """
    assert origin.real_money is real_money
    assert origin.evidence is evidence


def test_the_cleanup_takes_its_rule_from_that_classification() -> None:
    """Чистка чистит ровно то, что не свидетельство, — и список выводится.

    Правило записано один раз, в `RunOrigin.evidence`; SQL чистки строится
    из него. Иначе четвёртое происхождение однажды заведут, а в список
    чистки добавить забудут — и молча.
    """
    # Импорт внутри теста: проверяется именно то, что список ВЫВЕДЕН
    # из `RunOrigin`, а не переписан строкой в SQL.
    from market.storage import _PRUNABLE_ORIGINS

    assert set(_PRUNABLE_ORIGINS) == {
        origin.value for origin in RunOrigin if not origin.evidence
    }
    assert RunOrigin.LIVE.value not in _PRUNABLE_ORIGINS
    assert RunOrigin.PAPER.value not in _PRUNABLE_ORIGINS


# -- сторожа: что именно они держат и чем это доказано ------------------------
#
# Каждая проверка ниже сделана в две половины:
#   1. со сторожем на месте операция отвергается;
#   2. **после снятия сторожа та же операция проходит.**
# Вторая половина — не украшение. Без неё проверка зеленеет и тогда, когда
# операция не проходит по любой посторонней причине, а сторож давно мёртв.
# Три из этих дыр так и жили: `UPDATE` отвергался, `DELETE` и `INSERT OR
# REPLACE` проходили, и зелёный прогон это скрывал.


def unguard(store: CandleStore, name: str) -> None:
    """Снять сторожа. Только для второй половины проверки (см. выше)."""
    past_the_front_door(store, f"DROP TRIGGER {name}")


def test_a_decision_line_cannot_be_deleted_while_its_run_lives(
    store: CandleStore,
) -> None:
    """`DELETE` по строке журнала — тот же подлог, что и `UPDATE`.

    Прежде он проходил: сторож стоял только на `UPDATE`. Боевой прогон
    удалить было нельзя, а опустошить — можно, и оставалась пустая оболочка,
    неотличимая от аварийно завершённого прогона.
    """
    at = msk(2026, 9, 3, 10, 10)
    session = store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)
    written = store.write_decision(session.id, decision(at))

    with pytest.raises(sqlite3.IntegrityError, match="живёт, пока жив её прогон"):
        past_the_front_door(
            store, "DELETE FROM journal_decision WHERE id = ?", written
        )
    assert len(store.decisions()) == 1

    unguard(store, "journal_decision_dies_with_its_run")
    past_the_front_door(store, "DELETE FROM journal_decision WHERE id = ?", written)
    assert not store.decisions(), (
        "без сторожа удаление тоже не прошло — проверка ничего не доказывает"
    )


def test_a_trade_cannot_be_deleted_while_its_run_lives(store: CandleStore) -> None:
    at = msk(2026, 9, 3, 10, 10)
    session = store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)
    (written,) = store.write_trades(session.id, [trade(at)])

    with pytest.raises(sqlite3.IntegrityError, match="живёт, пока жив её прогон"):
        past_the_front_door(store, "DELETE FROM journal_trade WHERE id = ?", written)
    assert len(store.trades()) == 1

    unguard(store, "journal_trade_dies_with_its_run")
    past_the_front_door(store, "DELETE FROM journal_trade WHERE id = ?", written)
    assert not store.trades(), "без сторожа удаление тоже не прошло — проверка пуста"


def test_insert_or_replace_does_not_slip_past_the_guard(store: CandleStore) -> None:
    """`INSERT OR REPLACE` в SQLite — это DELETE + INSERT, а не UPDATE.

    Сторож `BEFORE UPDATE` на нём не срабатывает вовсе: строка подменяется
    молча. Ловит его сторож на удалении — но только при включённом
    `PRAGMA recursive_triggers`, иначе неявный DELETE проходит мимо триггеров.
    """
    at = msk(2026, 9, 3, 10, 10)
    session = store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)
    written = store.write_decision(session.id, decision(at))
    forgery = (
        "INSERT OR REPLACE INTO journal_decision "
        "(id, session_id, at, event, reason, level) "
        "VALUES (?, ?, 0, 'подделка', 'подделка', 'info')"
    )

    with pytest.raises(sqlite3.IntegrityError, match="живёт, пока жив её прогон"):
        past_the_front_door(store, forgery, written, session.id)
    assert store.decisions()[0].event == "Вход в лонг"

    unguard(store, "journal_decision_dies_with_its_run")
    past_the_front_door(store, forgery, written, session.id)
    assert store.decisions()[0].event == "подделка", (
        "без сторожа подмена тоже не прошла — проверка ничего не доказывает"
    )


def test_the_recursive_triggers_pragma_is_load_bearing(store: CandleStore) -> None:
    """Без `recursive_triggers` сторож на удалении не видит REPLACE.

    Проверяется сам флаг, а не сторож: выключаем — подмена проходит.
    Это единственный способ показать, что строка `PRAGMA recursive_triggers`
    в `CandleStore.__init__` стоит за поведением, а не за компанию.
    """
    at = msk(2026, 9, 3, 10, 10)
    session = store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)
    written = store.write_decision(session.id, decision(at))

    past_the_front_door(store, "PRAGMA recursive_triggers = OFF")
    past_the_front_door(
        store,
        "INSERT OR REPLACE INTO journal_decision "
        "(id, session_id, at, event, reason, level) "
        "VALUES (?, ?, 0, 'подделка', 'подделка', 'info')",
        written,
        session.id,
    )
    assert store.decisions()[0].event == "подделка", (
        "REPLACE не прошёл даже с выключенным флагом — значит его держит "
        "что-то другое, и строка PRAGMA в хранилище ничего не значит"
    )


#: Чем пробуют подменить каждое замороженное условие прогона: колонка → SQL
#: со значением другого типа содержания. Таблица, а не список в `parametrize`:
#: её полноту сверяет с `_FROZEN_SESSION_COLUMNS` отдельная проверка ниже,
#: и девятое замороженное поле не останется непробованным.
_FORGERIES: dict[str, str] = {
    "origin": "'backtest'",
    "started_at": "0",
    "symbol": "'RIU6'",
    "timeframe": "'1 минута'",
    "strategy": "'другой модуль'",
    "settings": "'другие настройки'",
    "app_version": "'0.0'",
    "note": "'ничего такого не было'",
}


def test_every_frozen_condition_of_a_run_gets_a_forgery_tried_on_it() -> None:
    """Подделку пробуют на каждом замороженном поле, а не на семи из восьми.

    Поле, замороженное сторожем и забытое здесь, осталось бы непроверенным:
    сторож на него распространяется, но никто ни разу не убедился, что он
    и правда стоит.
    """
    assert set(_FORGERIES) == set(_FROZEN_SESSION_COLUMNS), (
        "замороженные поля и пробы разошлись: "
        f"без пробы {sorted(set(_FROZEN_SESSION_COLUMNS) - set(_FORGERIES))}, "
        f"проба без поля {sorted(set(_FORGERIES) - set(_FROZEN_SESSION_COLUMNS))}"
    )


@pytest.mark.parametrize(("column", "value"), sorted(_FORGERIES.items()))
def test_no_condition_of_a_run_can_be_edited_afterwards(
    store: CandleStore, column: str, value: str
) -> None:
    """Все замороженные поля проверяются поимённо, а не «в целом».

    Сторож стоял только на двух — `origin` и `started_at`. Остальные шесть
    правились обычным `UPDATE` у боевого прогона: инструмент, размер свечи,
    торговый модуль, снимок настроек, версия программы и заметка об открытии.
    """
    session = store.open_journal_session(
        SessionRecord(RunOrigin.LIVE, symbol="MXU6"), now=msk(2026, 9, 3, 10, 0)
    )
    forgery = f"UPDATE journal_session SET {column} = {value} WHERE id = ?"

    with pytest.raises(sqlite3.IntegrityError, match="условия прогона не меняются"):
        past_the_front_door(store, forgery, session.id)

    unguard(store, "journal_session_conditions_are_final")
    past_the_front_door(store, forgery, session.id)
    assert store.journal_session(session.id) is not None


def test_a_disarmed_guard_does_not_survive_the_next_open(
    tmp_path: pathlib.Path,
) -> None:
    """Тело сторожа принадлежит коду, а не файлу.

    `CREATE TRIGGER IF NOT EXISTS` означал обратное: сторож, подменённый
    снаружи на пустой и оставленный под тем же именем, переживал открытие
    программой, и журнал после этого правился обычным `UPDATE`.
    """
    path = tmp_path / "candles.sqlite3"
    at = msk(2026, 9, 3, 10, 10)
    with CandleStore(path) as store:
        session = store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)
        store.write_decision(session.id, decision(at))

    outside = sqlite3.connect(path)
    outside.execute("DROP TRIGGER journal_decision_is_append_only")
    outside.execute(
        "CREATE TRIGGER journal_decision_is_append_only "
        "BEFORE UPDATE ON journal_decision BEGIN SELECT 1; END"
    )
    outside.commit()
    outside.close()

    with CandleStore(path) as reopened:
        assert reopened.replaced_guards == ("journal_decision_is_append_only",), (
            "подмена сторожа не замечена и не названа"
        )
        with pytest.raises(sqlite3.IntegrityError, match="переписать нельзя"):
            past_the_front_door(
                reopened, "UPDATE journal_decision SET reason = 'подделка'"
            )
        assert reopened.decisions()[0].reason == "Закрытие выше EMA(15)"


#: Тело сторожа заметки о конце, каким его ставила сборка до ревью 03.09.2026.
#: Дословно, а не из `_SUPERSEDED_GUARDS`: иначе проверка сравнивала бы код
#: сам с собой — тот же приём, что у `_SCHEMA_THREE_GUARDS`.
_FINAL_NOTE_GUARD_BEFORE_THE_REVIEW = """
CREATE TRIGGER journal_session_final_note_comes_with_the_close
BEFORE UPDATE OF finish_note ON journal_session
WHEN old.finished_at IS NOT NULL
BEGIN
    SELECT RAISE(
        ABORT,
        'прогон уже закрыт: заметка о завершении пишется один раз, вместе с закрытием'
    );
END
"""


def test_our_own_change_of_a_guard_is_not_called_tampering(
    tmp_path: pathlib.Path,
) -> None:
    """Обновление программы не должно выглядеть как правка базы снаружи.

    `replaced_guards` отвечает на один вопрос: «базу правили мимо программы?».
    Сторож, тело которого сменила новая сборка, попадал бы в это поле при
    первом открытии каждой старой базы — и поле перестало бы значить
    что-либо ровно там, где на него смотрят.

    Прощается тревога, а не сторож: тело всё равно заменяется на нынешнее,
    и это вторая половина проверки.
    """
    path = tmp_path / "candles.sqlite3"
    at = msk(2026, 9, 3, 10, 0)
    with CandleStore(path) as store:
        store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)

    outside = sqlite3.connect(path)
    outside.execute("DROP TRIGGER journal_session_final_note_comes_with_the_close")
    outside.execute(_FINAL_NOTE_GUARD_BEFORE_THE_REVIEW)
    outside.commit()
    outside.close()

    with CandleStore(path) as reopened:
        assert reopened.replaced_guards == (), (
            "смена тела сторожа новой сборкой выдана за подмену базы снаружи"
        )
        with pytest.raises(sqlite3.IntegrityError, match="пишется один раз"):
            past_the_front_door(
                reopened,
                "UPDATE journal_session SET finish_note = 'закрыто штатно' WHERE id = 1",
            )
        assert stored_session(reopened, 1).finish_note == "", (
            "прежнее тело сторожа пережило открытие: прощена не тревога, "
            "а сам сторож"
        )


def test_an_untouched_database_reports_no_replaced_guards(
    tmp_path: pathlib.Path,
) -> None:
    """Пересоздание сторожей не выдаёт себя за находку.

    Иначе поле `replaced_guards` было бы полным при каждом открытии и
    перестало бы что-либо значить.
    """
    path = tmp_path / "candles.sqlite3"
    with CandleStore(path) as first:
        assert first.replaced_guards == ()
    with CandleStore(path) as again:
        assert again.replaced_guards == ()


def test_a_run_still_takes_its_lines_with_it(store: CandleStore) -> None:
    """Сторож на удалении не сломал каскад — иначе чистить журнал было бы нечем.

    Прямое удаление строки отвергается, а удаление вместе с прогоном проходит:
    разделяет их условие `WHEN EXISTS` у сторожа.
    """
    at = msk(2026, 9, 3, 10, 10)
    history = store.open_journal_session(SessionRecord(RunOrigin.BACKTEST), now=at)
    store.write_decisions(history.id, [decision(at), decision(at)])
    store.write_trades(history.id, [trade(at)])

    report = store.forget_journal_sessions([history.id])
    assert (report.sessions, report.decisions, report.trades) == (1, 2, 1)
    stats = store.journal_stats()
    assert (stats.sessions, stats.decisions, stats.trades) == (0, 0, 0)


# -- закрытие прогона --------------------------------------------------------


def test_finishing_a_run_that_does_not_exist_is_refused(store: CandleStore) -> None:
    """Ошибка в номере не должна превращаться в ложный факт о торговом дне.

    Прежде вызов с чужим номером проходил молча: настоящий прогон оставался
    незакрытым навсегда и читался как «программу закрыли аварийно».
    """
    session = store.open_journal_session(
        SessionRecord(RunOrigin.LIVE), now=msk(2026, 9, 3, 10, 0)
    )
    with pytest.raises(LookupError, match="прогона 999 в журнале нет"):
        store.finish_journal_session(999, now=msk(2026, 9, 3, 11, 0))
    assert stored_session(store, session.id).finished_at is None


def test_finishing_twice_does_not_rewrite_the_end(store: CandleStore) -> None:
    """Второе закрытие — не отказ, но и не новая правда.

    Завершение программы бывает вызвано дважды, падать на этом нельзя.
    Время конца при этом остаётся первым.
    """
    at = msk(2026, 9, 3, 10, 0)
    session = store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)
    assert store.finish_journal_session(session.id, now=at + timedelta(hours=1)) is True
    assert store.finish_journal_session(session.id, now=at + timedelta(hours=5)) is False
    assert stored_session(store, session.id).finished_at == at + timedelta(hours=1)


def test_the_end_of_a_run_cannot_be_rewritten_from_outside(store: CandleStore) -> None:
    at = msk(2026, 9, 3, 10, 0)
    session = store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)
    store.finish_journal_session(session.id, now=at + timedelta(hours=1))
    forgery = "UPDATE journal_session SET finished_at = 0 WHERE id = ?"

    with pytest.raises(sqlite3.IntegrityError, match="прогон уже закрыт"):
        past_the_front_door(store, forgery, session.id)

    unguard(store, "journal_session_finishes_once")
    past_the_front_door(store, forgery, session.id)
    assert stored_session(store, session.id).finished_at != at + timedelta(hours=1)


# -- чистка секретов: все текстовые колонки ----------------------------------


#: Текстовые поля прогона и как испачкать каждое — по той же причине,
#: что и `DIRTY_TRADES`.
DIRTY_RUNS: tuple[tuple[str, "Callable[[], SessionRecord]"], ...] = (
    ("symbol", lambda: SessionRecord(RunOrigin.LIVE, symbol=FAKE_JWT)),
    ("timeframe", lambda: SessionRecord(RunOrigin.LIVE, timeframe=FAKE_JWT)),
    ("strategy", lambda: SessionRecord(RunOrigin.LIVE, strategy=FAKE_JWT)),
    ("settings", lambda: SessionRecord(RunOrigin.LIVE, settings=FAKE_JWT)),
    ("app_version", lambda: SessionRecord(RunOrigin.LIVE, app_version=FAKE_JWT)),
    ("note", lambda: SessionRecord(RunOrigin.LIVE, note=FAKE_JWT)),
)


@pytest.mark.parametrize(("field", "spoil"), DIRTY_RUNS, ids=[n for n, _ in DIRTY_RUNS])
def test_no_text_field_of_a_run_reaches_the_disk_dirty(
    store: CandleStore, field: str, spoil: "Callable[[], SessionRecord]"
) -> None:
    """Чистились пять колонок из десяти, и список исключений был неверен.

    Инструмент владелец счёта вводит руками — вставить туда он может что
    угодно; `strategy` и `app_version` завтра начнут собираться из чужих строк.
    Поэтому чистится всё текстовое, а не «то, куда может прийти чужой текст».
    """
    session = store.open_journal_session(spoil(), now=msk(2026, 9, 3, 10, 0))
    ((value,),) = past_the_front_door(
        store, f"SELECT {field} FROM journal_session WHERE id = ?", session.id
    )
    assert "eyJGQUtF" not in str(value), f"{field} уехал на диск с токеном: {value}"
    assert SECRET_MASK in str(value)


#: Текстовые поля сделки и как испачкать каждое. Подделки собраны заранее,
#: а не подставляются по имени поля: `replace(base, **{имя: значение})`
#: для проверяющего типов — словарь неизвестной формы, и он справедливо
#: возражает, что туда может попасть что угодно.
DIRTY_TRADES: tuple[tuple[str, Callable[[TradeRecord], TradeRecord]], ...] = (
    ("symbol", lambda base: dataclasses.replace(base, symbol=FAKE_JWT)),
    ("entry_order_id", lambda base: dataclasses.replace(base, entry_order_id=FAKE_JWT)),
    ("exit_order_id", lambda base: dataclasses.replace(base, exit_order_id=FAKE_JWT)),
)


@pytest.mark.parametrize(("field", "spoil"), DIRTY_TRADES, ids=[n for n, _ in DIRTY_TRADES])
def test_no_text_field_of_a_trade_reaches_the_disk_dirty(
    store: CandleStore, field: str, spoil: "Callable[[TradeRecord], TradeRecord]"
) -> None:
    """`entry_order_id` и `exit_order_id` на Э1-5 придут из ответа брокера."""
    at = msk(2026, 9, 3, 10, 10)
    session = store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)
    store.write_trades(session.id, [spoil(trade(at))])
    ((value,),) = past_the_front_door(store, f"SELECT {field} FROM journal_trade")
    assert "eyJGQUtF" not in str(value), f"{field} уехал на диск с токеном: {value}"


def test_the_load_report_is_cleaned_too(store: CandleStore) -> None:
    """Отчёт о загрузке — не «свой текст из чисел и дат».

    `LoadReport.note` собирается в `market.sync` из текста исключения
    и вклеивается в `summary()`. Сегодня источник один и он без токена,
    но `sync_minutes` уже принимает `source`.
    """
    store.write_load_report(
        LoadReport(symbol="MXU6", source="broker", note=f"отказ: Bearer {FAKE_JWT}"),
        now=msk(2026, 9, 3, 20, 0),
    )
    ((summary,),) = past_the_front_door(store, "SELECT summary FROM data_load")
    assert "eyJGQUtF" not in str(summary), f"токен уехал в журнал загрузок: {summary}"
    assert SECRET_MASK in str(summary)


def test_the_whole_database_file_holds_no_token(tmp_path: pathlib.Path) -> None:
    """Последняя проверка — по файлу целиком, а не по колонкам.

    Проверка по названным колонкам зеленеет ровно до появления одиннадцатой.
    Здесь ищется подделка в байтах файла: любое новое текстовое поле,
    забытое в чистке, всплывёт тут.

    ⚠️ **Проверок две, и вторая — положительный контроль.** «Подделки в файле
    нет» само по себе зеленеет и на пустом файле: тест, который не может
    упасть, — украшение. Поэтому маска в файле ищется тоже: она доказывает,
    что текст до диска дошёл и был вычищен, а не потерялся по дороге.
    """
    path = tmp_path / "candles.sqlite3"
    at = msk(2026, 9, 3, 10, 10)
    with CandleStore(path) as store:
        session = store.open_journal_session(
            SessionRecord(
                RunOrigin.LIVE,
                symbol=FAKE_JWT,
                timeframe=FAKE_JWT,
                strategy=FAKE_JWT,
                settings=FAKE_JWT,
                app_version=FAKE_JWT,
                note=FAKE_JWT,
            ),
            now=at,
        )
        store.write_decision(session.id, DecisionRecord(at, FAKE_JWT, FAKE_JWT))
        store.write_trades(
            session.id,
            [
                TradeRecord(
                    symbol=FAKE_JWT,
                    side=TradeSide.LONG,
                    volume=1.0,
                    entry_time=at,
                    entry_price=100.0,
                    exit_time=at + timedelta(minutes=5),
                    exit_price=101.0,
                    exit_reason=FAKE_JWT,
                    gross=1.0,
                    entry_order_id=FAKE_JWT,
                    exit_order_id=FAKE_JWT,
                )
            ],
        )
        store.write_load_report(
            LoadReport(symbol=FAKE_JWT, source="broker", note=FAKE_JWT), now=at
        )
        store.finish_journal_session(session.id, now=at, note=FAKE_JWT)

    raw = path.read_bytes()
    found = raw.count(b"eyJGQUtF")
    assert found == 0, f"подделка токена нашлась в файле базы {found} раз"

    # Семнадцать вхождений маски — шестнадцать текстовых колонок, которые
    # заполнил этот тест: шесть у прогона плюс заметка о конце, две у строки
    # решения, четыре у сделки, две у отчёта о загрузке и сводка, где токен
    # назван дважды. Новая колонка это число увеличит, поэтому сравнение
    # «не меньше»: пересчитывать тест из-за каждого поля не нужно, а вот
    # поле, переставшее записываться, обязано его уронить.
    masked = raw.count(SECRET_MASK.encode("utf-8"))
    assert masked >= 17, (
        f"маска нашлась в файле {masked} раз вместо семнадцати: проверка выше "
        "зеленеет не потому, что токен вычищен, а потому, что писать было нечего"
    )


# -- бывшие долги: удаление неповторимого прогона и форма выдачи --------------
#
# Всё ниже до конца файла было записано `xfail(strict=True)` — тремя
# известными дырами, оставленными задачей Э1-10. Пометки сняты работой
# Э1-10а и Э1-10б; тела проверок сохранены дословно и дополнены второй
# половиной там, где её не было. Долг, записанный только словами в документе,
# исчезает при первом же обновлении документа, — эти три не исчезли.


def test_a_paper_run_cannot_be_deleted_either(store: CandleStore) -> None:
    """`RunOrigin.evidence` объявляет симуляцию неповторимой — база знает.

    Прежде сторож стоял только на боевом прогоне: симуляцию защищала
    чистка, то есть код, а код переписывают. Вторая половина проверки —
    снятие сторожа — показывает, что удаление держит именно он.
    """
    at = msk(2026, 9, 3, 10, 10)
    session = store.open_journal_session(SessionRecord(RunOrigin.PAPER), now=at)
    store.write_decision(session.id, decision(at))

    with pytest.raises(sqlite3.IntegrityError):
        past_the_front_door(
            store, "DELETE FROM journal_session WHERE id = ?", session.id
        )
    assert store.journal_stats().sessions == 1

    unguard(store, "journal_session_evidence_is_kept")
    past_the_front_door(store, "DELETE FROM journal_session WHERE id = ?", session.id)
    assert store.journal_stats().sessions == 0, (
        "без сторожа удаление тоже не прошло — проверка ничего не доказывает"
    )


@pytest.mark.parametrize("origin", list(RunOrigin), ids=[one.value for one in RunOrigin])
def test_only_a_repeatable_run_can_be_deleted(
    store: CandleStore, origin: RunOrigin
) -> None:
    """Список неудаляемого выведен из `RunOrigin.evidence`, а не написан руками.

    Проверка идёт **по всем** происхождениям сразу: четвёртое, если его
    однажды заведут, попадёт сюда само. Именно так и появилась дыра с `paper` —
    происхождений стало три, а сторож остался про одно.
    """
    at = msk(2026, 9, 3, 10, 10)
    session = store.open_journal_session(SessionRecord(origin), now=at)
    forgery = "DELETE FROM journal_session WHERE id = ?"

    if origin.evidence:
        with pytest.raises(sqlite3.IntegrityError, match="повторить его нельзя"):
            past_the_front_door(store, forgery, session.id)
        assert store.journal_stats().sessions == 1
        return

    past_the_front_door(store, forgery, session.id)
    assert store.journal_stats().sessions == 0, (
        "повторимый прогон не удалился — чистить журнал станет нечем"
    )


def test_the_retired_guard_does_not_stay_in_an_old_database(
    tmp_path: pathlib.Path,
) -> None:
    """Отменённый сторож снимается, а не остаётся в файле навсегда.

    `_install_guards` пересоздаёт только тех, кто перечислен в коде. Сторож,
    снятый с довольствия и оставленный в базе, был бы невидимым для кода,
    непроверяемым — и уже неверным: `journal_session_live_is_kept` стерёг
    одно происхождение из двух неповторимых.
    """
    path = tmp_path / "candles.sqlite3"
    with CandleStore(path) as store:
        past_the_front_door(
            store,
            "CREATE TRIGGER journal_session_live_is_kept "
            "BEFORE DELETE ON journal_session BEGIN SELECT 1; END",
        )

    with CandleStore(path) as reopened:
        names = {
            str(name)
            for (name,) in past_the_front_door(
                reopened, "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
    assert "journal_session_live_is_kept" not in names
    assert "journal_session_evidence_is_kept" in names
    assert reopened.replaced_guards == (), (
        "снятие отменённого сторожа выдано за подмену базы снаружи: "
        "тогда поле replaced_guards перестаёт значить что-либо"
    )


def test_reading_without_a_run_does_not_mix_origins(store: CandleStore) -> None:
    """Гарантия «видно, боевая ли строка» живёт в типе и умирала в отчёте.

    Прочитанная строка происхождение несёт, но выдача **вперемешку** уже
    неверна как отчёт: владелец счёта видит сорок сделок вместо четырёх
    настоящих и от этого числа выбирает объём.
    """
    at = msk(2026, 9, 3, 10, 10)
    live = store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)
    history = store.open_journal_session(SessionRecord(RunOrigin.BACKTEST), now=at)
    store.write_decision(live.id, decision(at, event="боевая"))
    store.write_decision(history.id, decision(at, event="прогон"))

    origins = {row.origin for row in store.decisions()}
    assert len(origins) == 1, (
        "выдача смешала происхождения; читающий обязан назвать прогон "
        "или получить отказ"
    )
    page = store.decisions()
    assert page.session_id == history.id, "без просьбы отдаётся последний прогон"
    assert [row.event for row in page] == ["прогон"]


def test_the_run_of_a_read_can_be_named(store: CandleStore) -> None:
    """Прогон называют номером — тогда отдаётся он, а не последний."""
    at = msk(2026, 9, 3, 10, 10)
    live = store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)
    history = store.open_journal_session(SessionRecord(RunOrigin.BACKTEST), now=at)
    store.write_decision(live.id, decision(at, event="боевая"))
    store.write_decision(history.id, decision(at, event="прогон"))

    page = store.decisions(session_id=live.id)
    assert [row.event for row in page] == ["боевая"]
    assert page.session_id == live.id


def test_mixing_runs_is_possible_but_only_out_loud(store: CandleStore) -> None:
    """Смешать прогоны можно — сказав об этом вслух самим вызовом.

    Запрет насовсем сделал бы невозможным честный вопрос «сколько всего
    в журнале»: на него отвечает `journal_stats`, а на «покажи всё» —
    `all_runs`. Разница с прежним поведением в том, что смешение теперь
    **просят**, а не получают по умолчанию.
    """
    at = msk(2026, 9, 3, 10, 10)
    live = store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)
    history = store.open_journal_session(SessionRecord(RunOrigin.BACKTEST), now=at)
    store.write_decision(live.id, decision(at, event="боевая"))
    store.write_decision(history.id, decision(at, event="прогон"))

    page = store.decisions(all_runs=True)
    assert [row.event for row in page] == ["боевая", "прогон"]
    assert page.session_id is None, "сужения не было — называть прогон нечем"


def test_asking_for_one_run_and_for_all_at_once_is_refused(store: CandleStore) -> None:
    """Противоречивая просьба — отказ вслух, а не догадка о том, что имелось в виду."""
    session = store.open_journal_session(
        SessionRecord(RunOrigin.LIVE), now=msk(2026, 9, 3, 10, 0)
    )
    with pytest.raises(ValueError, match="все прогоны"):
        store.decisions(session_id=session.id, all_runs=True)
    with pytest.raises(ValueError, match="все прогоны"):
        store.trades(session_id=session.id, all_runs=True)


def test_reading_one_origin_still_spans_its_runs(store: CandleStore) -> None:
    """Боевой день, разорванный перезапуском, — по-прежнему один отчёт.

    Происхождения при этом не смешиваются: в выдаче только боевые строки.
    """
    at = msk(2026, 9, 3, 10, 10)
    morning = store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)
    store.write_decision(morning.id, decision(at, event="до перезапуска"))
    store.open_journal_session(SessionRecord(RunOrigin.BACKTEST), now=at)
    evening = store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)
    store.write_decision(evening.id, decision(at, event="после перезапуска"))

    page = store.decisions(origin=RunOrigin.LIVE)
    assert [row.event for row in page] == ["до перезапуска", "после перезапуска"]
    assert {row.origin for row in page} == {RunOrigin.LIVE}


def test_a_read_of_an_empty_journal_says_nothing_extra(store: CandleStore) -> None:
    """Пустая база — пустая выдача без обрезки и без выдуманного прогона."""
    page = store.decisions()
    assert list(page) == []
    assert page.truncated is False
    assert page.session_id is None


def test_a_truncated_read_says_that_it_is_truncated(store: CandleStore) -> None:
    """Молчаливая обрезка — то же, что тихая потеря свечи."""
    at = msk(2026, 9, 3, 10, 10)
    session = store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)
    store.write_decisions(session.id, [decision(at) for _ in range(10)])

    tail = store.decisions(limit=3)
    assert hasattr(tail, "truncated"), (
        "выдача не сообщает, что она обрезана: у списка нет признака"
    )
    assert tail.truncated is True
    assert len(tail) == 3
    assert tail.limit == 3
    assert "остальные не показаны" in tail.summary()


def test_a_whole_read_says_that_it_is_whole(store: CandleStore) -> None:
    """Признак обрезки обязан быть и **отрицательным**.

    Иначе он значил бы «лимит задан», а не «строки отрезаны», и читался бы
    как тревога при каждом обычном чтении.
    """
    at = msk(2026, 9, 3, 10, 10)
    session = store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)
    store.write_decisions(session.id, [decision(at) for _ in range(3)])
    store.write_trades(session.id, [trade(at)])

    exactly = store.decisions(limit=3)
    assert exactly.truncated is False, "лимит совпал с числом строк — это не обрезка"
    assert len(exactly) == 3
    assert "показаны все" in exactly.summary()
    assert store.trades(limit=1).truncated is False


def test_a_truncated_read_of_trades_says_so_too(store: CandleStore) -> None:
    """Сделки обрезаются тем же лимитом — и говорят об этом так же."""
    at = msk(2026, 9, 3, 10, 10)
    session = store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)
    store.write_trades(session.id, [trade(at + timedelta(minutes=n)) for n in range(5)])

    tail = store.trades(limit=2)
    assert tail.truncated is True
    assert len(tail) == 2
    assert [row.entry_time for row in tail] == [
        at + timedelta(minutes=3), at + timedelta(minutes=4),
    ], "лимит отсёк свежие сделки вместо старых"


@pytest.mark.parametrize("limit", [0, -1])
def test_a_limit_below_one_is_refused(store: CandleStore, limit: int) -> None:
    """`LIMIT 0` вернул бы пусто, `LIMIT -1` в SQLite — вообще всё.

    Два разных молчаливых ответа на одну опечатку. Оба заменены отказом.
    """
    with pytest.raises(ValueError, match="меньше единицы"):
        store.decisions(limit=limit)
    with pytest.raises(ValueError, match="меньше единицы"):
        store.trades(limit=limit)
    with pytest.raises(ValueError, match="меньше единицы"):
        store.journal_sessions(limit=limit)


def test_a_failed_cleanup_does_not_leave_an_empty_run(
    store: CandleStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Чистка идёт в той же транзакции, что и вставка прогона.

    Прежде она шла после коммита. Отказ чистки — диск полон, база занята —
    оставлял прогон без строк и без `finished_at`, то есть неотличимый
    от аварийно завершённого. Ложный факт ровно там, где человек ищет,
    что случилось.
    """
    def full_disk(self: CandleStore, keep: int) -> PruneReport:
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(CandleStore, "_prune_journal", full_disk)
    with pytest.raises(sqlite3.OperationalError):
        store.open_journal_session(
            SessionRecord(RunOrigin.LIVE), now=msk(2026, 9, 3, 10, 0)
        )
    assert store.journal_stats().sessions == 0, (
        "прогон остался в базе после отката: чистка идёт вне транзакции вставки"
    )


# -- правки ревью 03.09.2026: заметки, дозапись, повтор сделки, обрезка -------
#
# Четыре находки одного разбора, и три из них про одно: сторожа стерегли
# `UPDATE` и `DELETE`, а `INSERT` не стерёг никто. Строку можно было
# приписать к закрытому прогону, сделку — записать дважды, а заметку
# о завершении писать поверх заметки об открытии.


def test_the_note_of_a_run_survives_its_own_close(store: CandleStore) -> None:
    """Заметка об открытии и заметка о конце — разные факты и разные колонки.

    Прежде закрытие писало `SET note = ?` поверх: прогон, открытый
    с «автозапуск, догрузка 3 дня», после Ctrl+C нёс только «закрыто
    по Ctrl+C». Условие открытия исчезало насовсем — строки журнала
    неизменяемы, а заметка была одна.
    """
    at = msk(2026, 9, 3, 10, 0)
    session = store.open_journal_session(
        SessionRecord(RunOrigin.LIVE, note="автозапуск, догрузка 3 дня"), now=at
    )
    store.finish_journal_session(
        session.id, now=at + timedelta(hours=1), note="закрыто по Ctrl+C"
    )

    closed = stored_session(store, session.id)
    assert closed.note == "автозапуск, догрузка 3 дня", "условие открытия затёрто"
    assert closed.finish_note == "закрыто по Ctrl+C"


def test_a_run_closed_without_a_word_says_nothing_instead_of_guessing(
    store: CandleStore,
) -> None:
    """Молчаливое закрытие записывается пустотой, а не выдумкой про штатный конец."""
    at = msk(2026, 9, 3, 10, 0)
    session = store.open_journal_session(SessionRecord(RunOrigin.LIVE, note="утро"), now=at)
    store.finish_journal_session(session.id, now=at + timedelta(hours=1))

    closed = stored_session(store, session.id)
    assert (closed.note, closed.finish_note) == ("утро", "")


def test_the_note_of_a_run_is_a_frozen_condition(store: CandleStore) -> None:
    """Заметка об открытии переехала к условиям прогона — и сторож это знает.

    Она была единственным текстом прогона, который правился `UPDATE` без
    отказа: щель в семи сторожах шириной ровно в одну колонку.
    """
    session = store.open_journal_session(
        SessionRecord(RunOrigin.LIVE, note="автозапуск"), now=msk(2026, 9, 3, 10, 0)
    )
    forgery = "UPDATE journal_session SET note = 'ничего такого не было' WHERE id = ?"

    with pytest.raises(sqlite3.IntegrityError, match="условия прогона не меняются"):
        past_the_front_door(store, forgery, session.id)
    assert stored_session(store, session.id).note == "автозапуск"

    unguard(store, "journal_session_conditions_are_final")
    past_the_front_door(store, forgery, session.id)
    assert stored_session(store, session.id).note != "автозапуск", (
        "правка прошла и без сторожа — значит, первая половина проверки "
        "зеленела по постороннней причине"
    )


def test_the_final_note_is_written_with_the_close_and_never_after(
    store: CandleStore,
) -> None:
    """Заметка о конце пишется тем же действием, что закрывает прогон.

    Иначе она осталась бы тем, чем была `note`: единственным текстом прогона,
    правимым задним числом сколько угодно раз.
    """
    at = msk(2026, 9, 3, 10, 0)
    session = store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)
    store.finish_journal_session(session.id, now=at + timedelta(hours=1), note="штатно")
    forgery = "UPDATE journal_session SET finish_note = 'авария' WHERE id = ?"

    with pytest.raises(sqlite3.IntegrityError, match="пишется один раз"):
        past_the_front_door(store, forgery, session.id)
    assert stored_session(store, session.id).finish_note == "штатно"

    unguard(store, "journal_session_final_note_comes_with_the_close")
    past_the_front_door(store, forgery, session.id)
    assert stored_session(store, session.id).finish_note == "авария"


def test_an_open_run_gets_no_final_note_either(store: CandleStore) -> None:
    """Заметка о конце **открытого** прогона — тоже подделка, и тоже отвергается.

    Дыра ревью 03.09.2026: сторож стоял `WHEN old.finished_at IS NOT NULL`,
    то есть стерёг только закрытый прогон. У открытого заметка правилась
    обычным `UPDATE` сколько угодно раз. Дальше программу убивают, и в журнале
    остаётся прогон, который «не закончился» (`finished_at IS NULL`)
    и одновременно несёт «закрыто штатно». Поверить придётся одному из двух,
    а какому — по журналу не видно.
    """
    at = msk(2026, 9, 3, 10, 0)
    session = store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)
    forgery = "UPDATE journal_session SET finish_note = 'закрыто штатно' WHERE id = ?"

    with pytest.raises(sqlite3.IntegrityError, match="пишется один раз"):
        past_the_front_door(store, forgery, session.id)
    open_run = stored_session(store, session.id)
    assert (open_run.finished_at, open_run.finish_note) == (None, ""), (
        "у незакрытого прогона появилась заметка о том, чем он кончился"
    )

    unguard(store, "journal_session_final_note_comes_with_the_close")
    past_the_front_door(store, forgery, session.id)
    assert stored_session(store, session.id).finish_note == "закрыто штатно", (
        "правка не прошла и без сторожа — значит, первая половина проверки "
        "зеленела по посторонней причине"
    )


def test_the_close_itself_still_writes_the_final_note(store: CandleStore) -> None:
    """Сторож не должен запретить заодно и законное закрытие.

    Условие у него из двух половин, и вторая (`new.finished_at IS NULL`)
    пропускает ровно тот `UPDATE`, который закрывает прогон. Ошибись она
    в другую сторону — программа не смогла бы закрыть ни один прогон,
    и обнаружилось бы это на завершении боевого дня.
    """
    at = msk(2026, 9, 3, 10, 0)
    session = store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)

    assert store.finish_journal_session(
        session.id, now=at + timedelta(hours=1), note="закрыто по Ctrl+C"
    )
    closed = stored_session(store, session.id)
    assert closed.finished_at == at + timedelta(hours=1)
    assert closed.finish_note == "закрыто по Ctrl+C"


def test_the_final_note_is_cleaned_of_secrets_too(store: CandleStore) -> None:
    """Одиннадцатая текстовая колонка чистится наравне с десятью прежними.

    Текст закрытия собирается из причины остановки, а причиной бывает отказ
    брокера — то есть тело ответа сервера.
    """
    at = msk(2026, 9, 3, 10, 0)
    session = store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)
    store.finish_journal_session(
        session.id, now=at + timedelta(hours=1), note=f"остановка: Bearer {FAKE_JWT}"
    )

    ((value,),) = past_the_front_door(
        store, "SELECT finish_note FROM journal_session WHERE id = ?", session.id
    )
    assert "eyJGQUtF" not in str(value), f"токен уехал на диск: {value}"
    assert SECRET_MASK in str(value)


@pytest.mark.parametrize("table", ["journal_decision", "journal_trade"])
def test_a_closed_run_takes_no_more_lines(store: CandleStore, table: str) -> None:
    """Дозапись в закрытый прогон — тот же подлог, что и правка строки.

    Закрытый прогон — законченное свидетельство. Строка, приписанная к нему
    задним числом, ничем не отличается от переписанной, только следов
    от неё ещё меньше: сторожа на `UPDATE` при этом целы.
    """
    at = msk(2026, 9, 3, 10, 10)
    session = store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)
    store.finish_journal_session(session.id, now=at + timedelta(hours=1))

    def append() -> None:
        if table == "journal_decision":
            store.write_decision(session.id, decision(at, event="приписка"))
        else:
            store.write_trades(session.id, [trade(at)])

    with pytest.raises(sqlite3.IntegrityError, match="прогон закрыт"):
        append()
    assert store.journal_stats().decisions == 0
    assert store.journal_stats().trades == 0

    unguard(store, f"{table}_joins_an_open_run")
    append()
    assert store.journal_stats().decisions + store.journal_stats().trades == 1, (
        "запись не прошла и без сторожа — значит, первая половина проверки "
        "зеленела по посторонней причине"
    )


def test_an_open_run_still_takes_lines(store: CandleStore) -> None:
    """Сторож закрытого прогона не мешает работать открытому.

    Иначе он выключил бы боевой режим целиком, и заметили бы это в бою.
    """
    at = msk(2026, 9, 3, 10, 10)
    session = store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)
    store.write_decision(session.id, decision(at))
    store.write_trades(session.id, [trade(at)])

    stats = store.journal_stats()
    assert (stats.decisions, stats.trades) == (1, 1)


def test_the_same_trade_is_not_written_twice(store: CandleStore) -> None:
    """Повтор `write_trades` после таймаута удваивал прибыль за день.

    Журнал при этом формально не переписан: сторожа на `UPDATE` целы,
    строки на месте — их просто вдвое больше. Владелец счёта видит восемь
    сделок вместо четырёх и от этого числа выбирает объём.
    """
    at = msk(2026, 9, 3, 10, 10)
    session = store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)
    batch = [trade(at + timedelta(minutes=5 * n)) for n in range(4)]
    first = store.write_trades(session.id, batch)

    again = store.write_trades(session.id, batch)
    assert store.journal_stats().trades == 4, "повтор пачки удвоил сделки за день"
    assert again == first, (
        "на повтор ответили не теми номерами строк, в которых сделки лежат"
    )
    assert store.repeated_trades == tuple(first), (
        "повтор пачки прошёл молча: следа в отчёте нет"
    )


def test_a_new_trade_in_a_repeated_batch_is_not_lost(store: CandleStore) -> None:
    """Сделка, попавшая в пачку вместе с уже записанными, обязана записаться.

    Находка `/risk` 03.09.2026, и цена её — боевой день. Прежде повтор пачки
    отвергался сторожем **целиком**: `write_trades` поднимал `IntegrityError`,
    и новая сделка из той же пачки не появлялась в журнале вовсе. Отвергнутый
    дубль виден в отчёте и снимается; пропавшая сделка на настоящие деньги
    не видна никак.
    """
    at = msk(2026, 9, 3, 10, 10)
    session = store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)
    day = [trade(at + timedelta(minutes=5 * n)) for n in range(4)]
    written = store.write_trades(session.id, day)

    # 10:47, сработал тейк. Вызывающий подаёт весь день заново — так бывает
    # после таймаута сети, когда ответ на первую пачку не дошёл.
    take = trade(at + timedelta(minutes=37), price=101.0, gain=0.5)
    answer = store.write_trades(session.id, [*day, take])

    assert store.journal_stats().trades == 5, "новая сделка потерялась вместе с пачкой"
    assert answer[:4] == written, "у повторов сменились номера строк"
    assert store.repeated_trades == tuple(written), "пропущенные строки не названы"
    (fresh,) = [row for row in store.trades() if row.id == answer[4]]
    assert fresh.entry_time == take.entry_time
    assert fresh.gross == take.gross


def test_a_trade_that_only_looks_like_a_repeat_is_refused(store: CandleStore) -> None:
    """Совпал ключ, а содержимое нет — это не повтор, и пропускать его нельзя.

    Ключ грубый по построению: прогон, инструмент, сторона, вход, выход.
    Молчаливый пропуск по одному ключу означал бы, что вторая сделка с теми же
    временами исчезает вместе со своей ценой — и никто об этом не узнает.
    Пропуск позволен только там, где пропускать **нечего**.
    """
    at = msk(2026, 9, 3, 10, 10)
    session = store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)
    store.write_trades(session.id, [trade(at)])

    other = dataclasses.replace(trade(at), entry_price=555.0, gross=42.0)
    with pytest.raises(ValueError, match="entry_price"):
        store.write_trades(session.id, [other])
    assert store.journal_stats().trades == 1
    assert store.trades()[0].entry_price == 100.0, "запись подменена похожей"


def test_a_duplicate_written_from_outside_is_still_refused(store: CandleStore) -> None:
    """Сторож неповторимости остался, и проверка в коде его не подменяет.

    Проверка перед вставкой стоит на **нашем** пути и молча пропускает повтор.
    Сторож стоит на всех путях, включая правку базы снаружи, и там пропускать
    нечего: чужой `INSERT` вставит вторую такую же строку.
    """
    at = msk(2026, 9, 3, 10, 10)
    session = store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)
    store.write_trades(session.id, [trade(at)])
    forgery = (
        "INSERT INTO journal_trade "
        "(session_id, symbol, side, volume, entry_ts, entry_price, "
        " exit_ts, exit_price, gross, written_at) "
        "SELECT session_id, symbol, side, volume, entry_ts, entry_price, "
        "       exit_ts, exit_price, gross, written_at FROM journal_trade"
    )

    with pytest.raises(sqlite3.IntegrityError, match="уже записана"):
        past_the_front_door(store, forgery)
    assert store.journal_stats().trades == 1

    unguard(store, "journal_trade_is_written_once")
    past_the_front_door(store, forgery)
    assert store.journal_stats().trades == 2, (
        "вставка не прошла и без сторожа — значит, первая половина проверки "
        "зеленела по посторонней причине"
    )


def test_a_close_that_did_not_happen_is_not_reported_as_one(
    tmp_path: pathlib.Path,
) -> None:
    """`True` означает «закрыл я», а не «прогон закрыт».

    Между проверкой и записью прогон успевает закрыть кто-то ещё — второе
    подключение к тому же файлу или правка снаружи. `UPDATE` тогда не задевает
    ни одной строки (`AND finished_at IS NULL`), в базе остаётся чужое время
    конца и чужая заметка, а поданная сюда выброшена. Прежде вызывающему
    в этом случае отвечали `True`, то есть говорили, что записана его.

    Гонка воспроизводится не потоками, а точкой между двумя запросами: чистка
    текста (`_sanitize`) зовётся уже после проверки и ещё до записи.
    """
    path = tmp_path / "candles.sqlite3"
    at = msk(2026, 9, 3, 10, 0)
    mine, stranger = "моё закрытие", "чужое закрытие"

    def close_from_another_connection(text: str) -> str:
        """Чистка текста — заодно точка вмешательства. Сам текст не трогает.

        Подаётся через `sanitize=`, то есть обычным входом хранилища:
        лезть в его внутренности для этого не требуется.
        """
        if text == mine:
            outside = sqlite3.connect(path)
            outside.execute(
                "UPDATE journal_session SET finished_at = ?, finish_note = ? "
                "WHERE id = ?",
                (int((at + timedelta(minutes=30)).timestamp()), stranger, 1),
            )
            outside.commit()
            outside.close()
        return text

    with CandleStore(path, sanitize=close_from_another_connection) as store:
        session = store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)

        answered = store.finish_journal_session(
            session.id, now=at + timedelta(hours=1), note=mine
        )

        assert answered is False, (
            "закрытие объявлено состоявшимся, хотя записана чужая заметка"
        )
        closed = stored_session(store, session.id)
        assert closed.finish_note == stranger
        assert closed.finished_at == at + timedelta(minutes=30)


def test_a_repeat_of_a_written_batch_passes_even_into_a_closed_run(
    store: CandleStore,
) -> None:
    """Повтор пачки, целиком уже записанной, не спотыкается о закрытие прогона.

    Записывать нечего, значит и отказывать не за что. Обратное поведение
    делало бы опасным сам повтор: вызывающий, не дождавшийся ответа, получал
    бы отказ там, где всё давно записано.
    """
    at = msk(2026, 9, 3, 10, 10)
    session = store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)
    batch = [trade(at), trade(at + timedelta(minutes=5))]
    written = store.write_trades(session.id, batch)
    store.finish_journal_session(session.id, now=at + timedelta(hours=1))

    assert store.write_trades(session.id, batch) == written
    assert store.journal_stats().trades == 2


def test_two_different_trades_are_not_taken_for_a_repeat(store: CandleStore) -> None:
    """Сторож неповторимости не должен отвергать вторую законную сделку.

    Проверяются оба входа сразу: боевой, где имена заявок есть и совпадают
    (заглушка одного источника), и прогон по истории, где имён нет вовсе.
    Ключ по паре имён заявок именно на этом и ломался: у прогона по истории
    все имена пусты, и он схлопнул бы весь прогон в одну сделку.
    """
    at = msk(2026, 9, 3, 10, 10)
    live = store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)
    store.write_trades(
        live.id, [trade(at), trade(at + timedelta(minutes=5))]
    )

    history = store.open_journal_session(SessionRecord(RunOrigin.BACKTEST), now=at)
    nameless = [
        dataclasses.replace(
            trade(at + timedelta(minutes=5 * n)), entry_order_id="", exit_order_id=""
        )
        for n in range(3)
    ]
    store.write_trades(history.id, nameless)

    assert store.journal_stats().trades == 5


def test_the_same_trade_in_another_run_is_another_trade(store: CandleStore) -> None:
    """Неповторимость считается **внутри прогона**, а не по всей базе.

    Один и тот же день, пройденный дважды по истории, — два прогона
    и две одинаковые сделки. Общий на всю базу ключ объявил бы второй
    прогон повтором первого и оставил бы его без сделок.
    """
    at = msk(2026, 9, 3, 10, 10)
    first = store.open_journal_session(SessionRecord(RunOrigin.BACKTEST), now=at)
    second = store.open_journal_session(SessionRecord(RunOrigin.BACKTEST), now=at)
    store.write_trades(first.id, [trade(at)])
    store.write_trades(second.id, [trade(at)])

    assert store.journal_stats().trades == 2


def test_the_list_of_runs_says_when_it_is_truncated(store: CandleStore) -> None:
    """Соседи научились честному признаку обрезки, а список прогонов — нет.

    Пятьдесят первый прогон исчезал молча, и список выглядел ровно как полный.
    """
    at = msk(2026, 9, 3, 10, 0)
    for number in range(3):
        store.open_journal_session(
            SessionRecord(RunOrigin.BACKTEST, note=f"прогон {number}"), now=at
        )

    cut = store.journal_sessions(limit=2)
    assert cut.truncated is True
    assert len(cut) == 2
    assert [row.note for row in cut] == ["прогон 2", "прогон 1"], (
        "лимит отсёк свежие прогоны вместо старых"
    )

    whole = store.journal_sessions(limit=3)
    assert whole.truncated is False
    assert len(whole) == 3


def test_the_list_of_runs_still_behaves_like_a_list(store: CandleStore) -> None:
    """Читателю, которому полнота не нужна, менять нечего.

    `JournalPage` — последовательность: длина, перебор, распаковка
    и обращение по номеру работают как у списка. Иначе честный признак
    обрезки стоил бы правки каждому читающему.
    """
    at = msk(2026, 9, 3, 10, 0)
    store.open_journal_session(SessionRecord(RunOrigin.LIVE, note="один"), now=at)

    (only,) = store.journal_sessions()
    assert only.note == "один"
    assert store.journal_sessions()[0].note == "один"
    assert [row.note for row in store.journal_sessions()] == ["один"]


@pytest.mark.parametrize(
    ("table", "values"),
    [
        ("journal_session", [origin.value for origin in RunOrigin]),
        ("journal_decision", [level.value for level in DecisionLevel]),
        ("journal_trade", [side.value for side in TradeSide]),
    ],
)
def test_every_check_is_derived_from_its_enum(
    store: CandleStore, table: str, values: list[str]
) -> None:
    """`CHECK` собирается из перечисления, а не переписан в SQL строкой.

    Списки `_EVIDENCE_ORIGINS` и `_PRUNABLE_ORIGINS` выводятся из `RunOrigin`
    ровно затем, чтобы четвёртое происхождение попало в оба следствия сразу.
    В написанный руками `CHECK` оно не попало бы, и база начала бы отвергать
    значение, которое код считает законным.
    """
    from market.storage import _sql_values

    ((sql,),) = past_the_front_door(
        store, "SELECT sql FROM sqlite_master WHERE name = ?", table
    )
    assert _sql_values(values) in str(sql), (
        f"{table}: список значений в CHECK не совпал с перечислением — "
        "значит, он написан отдельно и разойдётся молча"
    )


#: Сторожа прежней сборки — дословно из базы, созданной коммитом `4337561`.
#: Лежат на уровне модуля, а не в теле функции: под отступом вложенности те же
#: строки уходили за сотню знаков. Отступы внутри тел на дело не влияют —
#: SQLite к ним нечувствителен, а сравнение тел (`_same_sql`) нормализует
#: пробелы намеренно.
#:
#: ⚠️ Единственная правка против снятого из базы текста — перенос строки
#: в списке замороженных колонок: в базе он записан одной строкой длиной
#: в 114 знаков. Список колонок и сам перенос ничего не меняют для SQL.
_SCHEMA_THREE_GUARDS = """
CREATE TRIGGER journal_decision_dies_with_its_run
        BEFORE DELETE ON journal_decision
        WHEN EXISTS (SELECT 1 FROM journal_session WHERE id = old.session_id)
        BEGIN
            SELECT RAISE(
                ABORT,
                'строку журнала решений удалить нельзя: она живёт, пока жив её прогон'
            );
        END;

CREATE TRIGGER journal_decision_is_append_only
        BEFORE UPDATE ON journal_decision
        BEGIN
            SELECT RAISE(
                ABORT,
                'строку журнала решений переписать нельзя: журнал — свидетельство'
            );
        END;

CREATE TRIGGER journal_session_conditions_are_final
        BEFORE UPDATE OF origin, started_at, symbol, timeframe, strategy,
                         settings, app_version ON journal_session
        BEGIN
            SELECT RAISE(
                ABORT,
                'условия прогона не меняются после его открытия — ни одно из семи полей'
            );
        END;

CREATE TRIGGER journal_session_evidence_is_kept
        BEFORE DELETE ON journal_session
        WHEN old.origin IN ('live', 'paper')
        BEGIN
            SELECT RAISE(
                ABORT,
                'прогон не удаляется: повторить его нельзя — настоящие деньги или живой рынок'
            );
        END;

CREATE TRIGGER journal_session_finishes_once
        BEFORE UPDATE OF finished_at ON journal_session
        WHEN old.finished_at IS NOT NULL
        BEGIN
            SELECT RAISE(
                ABORT,
                'прогон уже закрыт: время его конца не переписывается'
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

CREATE TRIGGER journal_trade_is_append_only
        BEFORE UPDATE ON journal_trade
        BEGIN
            SELECT RAISE(
                ABORT,
                'строку журнала сделок переписать нельзя: журнал — свидетельство'
            );
        END;
"""


def _schema_three_database(path: pathlib.Path, at: datetime) -> None:
    """База схемы 3 с прогоном, строкой решения и сделкой.

    Текст таблиц переписан сюда **дословно** из той сборки, а не собран
    из `_SCHEMA`: собранный из кода, он менялся бы вместе с кодом, и проверка
    миграции сравнивала бы код сам с собой.

    ⚠️ **Сторожа ставятся тоже, и без них фикстура была базой, которой
    не бывает.** Найдено ревью 03.09.2026. Прежняя сборка ставит сторожей
    при **каждом** открытии базы, то есть база схемы 3 без единого триггера
    в природе не встречается. Пять из них ссылаются на `journal_session`,
    и два — `journal_decision_dies_with_its_run`, `journal_trade_dies_with_its_run` —
    стоят на ДРУГИХ таблицах: `DROP TABLE journal_session` их не уносит,
    а следующий за ним `ALTER TABLE … RENAME` переразбирает всю схему
    и падает на ссылке в несуществующую таблицу.

    Цена промаха была не в тесте: программа переставала открывать свою базу
    вообще, при каждом запуске. Тесты при этом были зелёными — они мигрировали
    базу без сторожей.

    Тела триггеров сняты **из настоящей базы, созданной сборкой `4337561`**,
    а не написаны заново, по тому же правилу, что и таблицы выше.
    """
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE journal_session (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            origin      TEXT    NOT NULL CHECK (origin IN ('live', 'paper', 'backtest')),
            started_at  INTEGER NOT NULL,
            finished_at INTEGER,
            symbol      TEXT    NOT NULL DEFAULT '',
            timeframe   TEXT    NOT NULL DEFAULT '',
            strategy    TEXT    NOT NULL DEFAULT '',
            settings    TEXT    NOT NULL DEFAULT '',
            app_version TEXT    NOT NULL DEFAULT '',
            note        TEXT    NOT NULL DEFAULT ''
        );
        CREATE TABLE journal_decision (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL REFERENCES journal_session (id) ON DELETE CASCADE,
            at         INTEGER NOT NULL,
            event      TEXT    NOT NULL,
            reason     TEXT    NOT NULL,
            level      TEXT    NOT NULL
                CHECK (level IN ('info', 'trade', 'warning', 'error'))
        );
        CREATE TABLE journal_trade (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id      INTEGER NOT NULL REFERENCES journal_session (id) ON DELETE CASCADE,
            symbol          TEXT    NOT NULL,
            side            TEXT    NOT NULL CHECK (side IN ('long', 'short')),
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
        PRAGMA user_version = 3;
        """
    )
    started = int(at.timestamp())
    connection.execute(
        "INSERT INTO journal_session (id, origin, started_at, note) "
        "VALUES (1, 'live', ?, 'боевой день')",
        (started,),
    )
    connection.execute(
        "INSERT INTO journal_decision (session_id, at, event, reason, level) "
        "VALUES (1, ?, 'Лонг открыт', 'Закрытие выше EMA(15)', 'trade')",
        (started,),
    )
    connection.execute(
        "INSERT INTO journal_trade (session_id, symbol, side, volume, entry_ts, "
        "entry_price, exit_ts, exit_price, gross, written_at) "
        "VALUES (1, 'MXU6', 'long', 1, ?, 100, ?, 101, 1, ?)",
        (started, started + 300, started),
    )
    # Сторожа прежней сборки — дословно из базы, созданной коммитом 4337561.
    connection.executescript(_SCHEMA_THREE_GUARDS)
    connection.commit()
    connection.close()


def test_a_database_of_schema_three_keeps_its_journal_through_the_rebuild(
    tmp_path: pathlib.Path,
) -> None:
    """Пересборка таблицы прогонов не уносит с собой журнал.

    Это главное, что может пойти не так: у строк журнала стоит
    `ON DELETE CASCADE`, а `DROP TABLE` при включённых внешних ключах
    выполняет неявный `DELETE FROM`. Забыть выключить ключи — значит
    удалить весь боевой журнал миграцией, молча и необратимо.
    """
    path = tmp_path / "schema-three.sqlite3"
    at = msk(2026, 9, 3, 10, 10)
    _schema_three_database(path, at)

    with CandleStore(path) as store:
        stats = store.journal_stats()
        assert (stats.sessions, stats.decisions, stats.trades) == (1, 1, 1), (
            "миграция унесла журнал: внешние ключи не были выключены"
        )
        run = stored_session(store, 1)
        assert (run.origin, run.note, run.finish_note) == (
            RunOrigin.LIVE, "боевой день", ""
        )
        ((version,),) = past_the_front_door(store, "PRAGMA user_version")
        assert version == SCHEMA_VERSION


def test_the_rebuild_brings_the_derived_check_and_the_guards(
    tmp_path: pathlib.Path,
) -> None:
    """На старой базе правится и то, что `CREATE TABLE IF NOT EXISTS` не правит.

    `CHECK` на происхождение остался бы там написанным вручную списком,
    а сторожа, снесённые вместе со старой таблицей, не вернулись бы вовсе,
    если ставить их до миграции, а не после.
    """
    path = tmp_path / "schema-three.sqlite3"
    at = msk(2026, 9, 3, 10, 10)
    _schema_three_database(path, at)

    with CandleStore(path) as store:
        assert store.replaced_guards == (), (
            "собственная миграция выдана за подмену базы снаружи"
        )
        names = {
            str(name)
            for (name,) in past_the_front_door(
                store, "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
        assert "journal_session_conditions_are_final" in names, (
            "сторожа прогона не пережили пересборку таблицы"
        )
        with pytest.raises(sqlite3.IntegrityError, match="условия прогона не меняются"):
            past_the_front_door(
                store, "UPDATE journal_session SET origin = 'backtest' WHERE id = 1"
            )
        # Продолжать работу с прогоном можно: он остался открытым.
        store.write_decision(1, decision(at, event="после миграции"))
        assert store.journal_stats().decisions == 2


def test_a_database_of_the_new_schema_is_not_rebuilt_again(
    tmp_path: pathlib.Path,
) -> None:
    """Готовая база не пересобирается при каждом открытии.

    Пересборка на месте — самая опасная операция в файле: она сносит таблицу
    прогонов и её сторожей. Делать её на ровном месте нельзя.
    """
    path = tmp_path / "candles.sqlite3"
    at = msk(2026, 9, 3, 10, 10)
    with CandleStore(path) as store:
        session = store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)
        store.write_decision(session.id, decision(at))

    with CandleStore(path) as reopened:
        rebuilt = past_the_front_door(
            reopened,
            "SELECT name FROM sqlite_master WHERE name = 'journal_session_new'",
        )
        assert rebuilt == [], "остался хвост пересборки: таблица-времянка в базе"
        assert reopened.journal_stats().decisions == 1


# -- пересборка: чего она не имеет права сделать ------------------------------
#
# Ветка отказа миграции решает, откроется ли у владельца счёта его база
# и уцелеет ли журнал боевого дня. Исполнить её из публичного входа нельзя
# по построению: пересборка написана так, чтобы строк не терять. Поэтому
# отказ вызывается обёрткой соединения, подменяющей ровно один шаг, — иначе
# ветка существовала бы только в бою (решение 0008, п. 3).

#: Три таблицы журнала — руками, а не из `market.storage`: проверка обязана
#: заметить таблицу, выпавшую из списка в коде.
_JOURNAL_TABLES_BY_HAND = ("journal_session", "journal_decision", "journal_trade")


class _ConnectionProxy:
    """Соединение с одним подменённым шагом — для проб пересборки.

    Хранилище открывает соединение само (`sqlite3.connect`), и между шагами
    миграции другого входа нет. Всё, кроме `execute`, уходит в настоящее
    соединение как есть.
    """

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real

    def execute(self, sql: str, parameters: Sequence[object] = (), /) -> sqlite3.Cursor:
        return self._real.execute(sql, parameters)

    def __getattr__(self, name: str) -> object:
        return getattr(self._real, name)


class _KeysThatStayOn(_ConnectionProxy):
    """`PRAGMA foreign_keys = OFF` не действует — ключи остаются включёнными.

    Ровно тот отказ, ради которого стоит счёт строк. В SQLite так бывает,
    если прагма подана внутри транзакции. `DROP TABLE journal_session`
    тогда выполняет неявный `DELETE FROM`, и `ON DELETE CASCADE` уносит
    журнал целиком. Сирот при этом **нет**: `PRAGMA foreign_key_check` был бы
    пуст, транзакция зафиксирована, журнал уничтожен.
    """

    def execute(self, sql: str, parameters: Sequence[object] = (), /) -> sqlite3.Cursor:
        if " ".join(sql.split()).lower() == "pragma foreign_keys = off":
            return self._real.execute("SELECT 1")
        return super().execute(sql, parameters)


class _LinesVanishAfterTheRename(_ConnectionProxy):
    """Сразу после переименования таблицы одна таблица журнала пустеет."""

    def __init__(self, real: sqlite3.Connection, table: str) -> None:
        super().__init__(real)
        self._table = table

    def execute(self, sql: str, parameters: Sequence[object] = (), /) -> sqlite3.Cursor:
        cursor = super().execute(sql, parameters)
        if sql.lstrip().startswith("ALTER TABLE journal_session_new RENAME"):
            self._real.execute(f"DELETE FROM {self._table}")
        return cursor


@contextlib.contextmanager
def _connections_wrapped_in(
    wrap: Callable[[sqlite3.Connection], _ConnectionProxy],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[list[sqlite3.Connection]]:
    """Открытия базы внутри блока идут через обёртку.

    Отдаёт настоящие соединения по порядку открытия и закрывает их
    на выходе — чтобы проба, в которой закрытие не проверяется, ничего
    не оставляла открытым.
    """
    real_connect = sqlite3.connect
    opened: list[sqlite3.Connection] = []

    def connect(
        database: str,
        *,
        isolation_level: Literal["DEFERRED", "EXCLUSIVE", "IMMEDIATE"] | None,
    ) -> _ConnectionProxy:
        real = real_connect(database, isolation_level=isolation_level)
        opened.append(real)
        return wrap(real)

    with monkeypatch.context() as patched:
        patched.setattr(sqlite3, "connect", connect)
        try:
            yield opened
        finally:
            for real in opened:
                real.close()


def _schema_and_counts(path: pathlib.Path) -> tuple[int, tuple[int, ...]]:
    """Версия схемы и число строк в таблицах журнала — мимо хранилища."""
    raw = sqlite3.connect(path)
    try:
        version = int(raw.execute("PRAGMA user_version").fetchone()[0])
        counts = tuple(
            int(raw.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in _JOURNAL_TABLES_BY_HAND
        )
    finally:
        raw.close()
    return version, counts


def test_a_rebuild_that_would_lose_the_journal_is_refused_and_rolled_back(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Каскад от `DROP TABLE` не проходит молча: отказ, откат, база прежняя.

    `PRAGMA foreign_key_check` здесь не ловил ничего — каскад **удаляет**
    строки, а не оставляет их без прогона. Ловит только счёт строк.
    """
    path = tmp_path / "schema-three.sqlite3"
    _schema_three_database(path, msk(2026, 9, 3, 10, 10))

    with _connections_wrapped_in(_KeysThatStayOn, monkeypatch):
        with pytest.raises(
            RuntimeError, match="journal_decision 1 → 0, journal_trade 1 → 0"
        ):
            CandleStore(path)

    assert _schema_and_counts(path) == (3, (1, 1, 1)), (
        "отказ не откатил пересборку: база не осталась схемы 3 с целым журналом"
    )
    # Обычное открытие после отказа проходит и журнал доносит.
    with CandleStore(path) as store:
        stats = store.journal_stats()
        assert (stats.sessions, stats.decisions, stats.trades) == (1, 1, 1)


@pytest.mark.parametrize("table", _JOURNAL_TABLES_BY_HAND)
def test_a_rebuild_that_empties_any_journal_table_is_refused(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, table: str
) -> None:
    """Считаются все три таблицы, и отказ называет ту, где строк не досчитались."""
    path = tmp_path / "schema-three.sqlite3"
    _schema_three_database(path, msk(2026, 9, 3, 10, 10))

    with _connections_wrapped_in(
        lambda real: _LinesVanishAfterTheRename(real, table), monkeypatch
    ):
        with pytest.raises(RuntimeError, match=f"{table} 1 → 0"):
            CandleStore(path)

    assert _schema_and_counts(path) == (3, (1, 1, 1))


def test_orphans_left_by_an_outside_delete_do_not_lock_the_owner_out(
    tmp_path: pathlib.Path,
) -> None:
    """Чужие сироты в базе — не повод не открывать её никогда.

    Владелец открыл базу сторонним просмотрщиком, где внешние ключи
    по умолчанию выключены, и удалил прогон по истории: его строки остались
    без прогона. База при этом читается — чтение идёт через `JOIN`, сироты
    невидимы. `PRAGMA foreign_key_check` после пересборки находил их
    и отказывал из `__init__` при каждом запуске, без пути починки, с текстом
    про миграцию, которая их не создавала. В портативе, запущенном двойным
    щелчком, это «окно не появилось».

    Сирота остаётся на месте: пересборка не удаляет ничего, это её правило.
    Что с ним делать дальше — отдельный вопрос, и он не про открытие базы.
    """
    path = tmp_path / "schema-three.sqlite3"
    at = msk(2026, 9, 3, 10, 10)
    _schema_three_database(path, at)
    viewer = sqlite3.connect(path)  # внешние ключи выключены, как в просмотрщике
    viewer.execute(
        "INSERT INTO journal_session (id, origin, started_at) VALUES (2, 'backtest', ?)",
        (int(at.timestamp()),),
    )
    viewer.execute(
        "INSERT INTO journal_decision (session_id, at, event, reason, level) "
        "VALUES (2, ?, 'проба', 'по истории', 'info')",
        (int(at.timestamp()),),
    )
    viewer.execute("DELETE FROM journal_session WHERE id = 2")
    viewer.commit()
    viewer.close()

    with CandleStore(path) as store:
        ((version,),) = past_the_front_door(store, "PRAGMA user_version")
        assert version == SCHEMA_VERSION
        stats = store.journal_stats()
        assert (stats.sessions, stats.decisions, stats.trades) == (1, 2, 1), (
            "пересборка либо унесла строки, либо взялась чистить чужих сирот"
        )
        assert stored_session(store, 1).origin is RunOrigin.LIVE
        assert len(past_the_front_door(store, "PRAGMA foreign_key_check")) == 1


def test_the_run_counter_survives_the_rebuild(tmp_path: pathlib.Path) -> None:
    """Номер удалённого прогона после миграции не выдаётся заново.

    `DROP TABLE` уносит строку `sqlite_sequence`, `INSERT … SELECT` её
    не восстанавливает: счётчик падал до наибольшего живого номера, и новый
    прогон получал номер удалённого. Два разных «прогона №3» в журнале-
    свидетельстве — двусмысленность в каждой внешней ссылке на номер.
    Состояние «счётчик выше наибольшего номера» обычное: чистка прогонов
    по истории удаляет старые по замыслу.
    """
    path = tmp_path / "schema-three.sqlite3"
    at = msk(2026, 9, 3, 10, 10)
    _schema_three_database(path, at)
    raw = sqlite3.connect(path)
    for run in (2, 3):
        raw.execute(
            "INSERT INTO journal_session (id, origin, started_at) VALUES (?, 'backtest', ?)",
            (run, int(at.timestamp())),
        )
    raw.execute("DELETE FROM journal_session WHERE id = 3")
    raw.commit()
    raw.close()

    with CandleStore(path) as store:
        fresh = store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)
        assert fresh.id == 4, "номер удалённого прогона выдан заново"


@pytest.mark.parametrize(
    ("version", "wrap", "refusal"),
    [
        pytest.param(3, _KeysThatStayOn, "изменила бы журнал", id="rebuild-refused"),
        pytest.param(99, _ConnectionProxy, "более новой версией", id="newer-schema"),
    ],
)
def test_a_refused_open_closes_its_connection(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    version: int,
    wrap: Callable[[sqlite3.Connection], _ConnectionProxy],
    refusal: str,
) -> None:
    """Отказ из `__init__` не оставляет соединение открытым.

    Объект не создан — `close()` не позовут никогда. На Windows открытый
    дескриптор не даёт переименовать и удалить файл базы, то есть мешает
    ровно той ручной починке, которая после отказа остаётся единственной.
    Проверка пересборки сделала этот путь чаще, чем он был.
    """
    path = tmp_path / "schema-three.sqlite3"
    _schema_three_database(path, msk(2026, 9, 3, 10, 10))
    raw = sqlite3.connect(path)
    raw.execute(f"PRAGMA user_version = {version}")
    raw.commit()
    raw.close()

    with _connections_wrapped_in(wrap, monkeypatch) as opened:
        with pytest.raises(RuntimeError, match=refusal):
            CandleStore(path)
        assert len(opened) == 1, "открытие обошлось не одним соединением"
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            opened[0].execute("SELECT 1")


#: Сторожа схемы 3, чьё тело ссылается на `journal_session`: пересборка
#: снимает их раньше, чем `_install_guards` их прочитает (ревью 03.09.2026).
#: Пять из семи; уцелевают только два `*_is_append_only`.
_GUARDS_TAKEN_DOWN_BY_THE_REBUILD = (
    "journal_decision_dies_with_its_run",
    "journal_trade_dies_with_its_run",
    "journal_session_conditions_are_final",
    "journal_session_finishes_once",
    "journal_session_evidence_is_kept",
)


def _schema_three_guard(name: str) -> str:
    """Текст одного сторожа прежней сборки, без завершающей `;`."""
    for chunk in _SCHEMA_THREE_GUARDS.strip().split("\n\n"):
        if chunk.startswith(f"CREATE TRIGGER {name}\n"):
            return chunk.rstrip().removesuffix(";")
    raise AssertionError(f"среди сторожей схемы 3 нет {name}")


def _disarmed(name: str) -> str:
    """Сторож схемы 3 под тем же именем и на той же таблице, но без `RAISE`.

    Ссылка на `journal_session` в нём остаётся — иначе пересборка его
    не снимет, и проба будет не про то.
    """
    head, _, _ = _schema_three_guard(name).partition("BEGIN")
    return head + "BEGIN SELECT 1; END"


@pytest.mark.parametrize("name", _GUARDS_TAKEN_DOWN_BY_THE_REBUILD)
def test_a_guard_tampered_before_the_update_is_still_named_after_the_rebuild(
    tmp_path: pathlib.Path, name: str
) -> None:
    """Подмена, сделанная до обновления программы, не исчезает вместе с пересборкой.

    `replaced_guards` — единственный след правки базы снаружи. Пересборка
    снимала пять сторожей из семи раньше, чем `_install_guards` читал
    `sqlite_master`, и поле пустело ровно на том открытии, где подмена свежая.
    """
    path = tmp_path / "schema-three.sqlite3"
    _schema_three_database(path, msk(2026, 9, 3, 10, 10))
    outside = sqlite3.connect(path)
    outside.execute(f"DROP TRIGGER {name}")
    outside.execute(_disarmed(name))
    outside.commit()
    outside.close()

    with CandleStore(path) as store:
        assert store.replaced_guards == (name,), (
            "подмена, сделанная до обновления, пропала вместе с пересборкой"
        )
        ((body,),) = past_the_front_door(
            store, "SELECT sql FROM sqlite_master WHERE name = ?", name
        )
        assert _same_sql(str(body), _GUARDS[name]), "сторож не возвращён к телу из кода"


def test_every_guard_is_back_after_the_rebuild(tmp_path: pathlib.Path) -> None:
    """Снятый миграцией сторож пересоздаётся и тогда, когда его тело не менялось.

    Разбор «тело совпало — оставить как есть» верен для сторожа, лежащего
    в базе, и неверен для снятого: его в базе уже нет. Четыре из пяти снятых
    сторожей схемы 3 совпадают с нынешними дословно.
    """
    path = tmp_path / "schema-three.sqlite3"
    _schema_three_database(path, msk(2026, 9, 3, 10, 10))

    with CandleStore(path) as store:
        found = {
            str(name): str(sql)
            for name, sql in past_the_front_door(
                store, "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"
            )
        }
        missing = [
            name for name, body in _GUARDS.items() if not _same_sql(found.get(name), body)
        ]
        assert missing == [], "после пересборки не встали на место сторожа"


# -- списки колонок: один на все места ---------------------------------------
#
# Колонки прогона были перечислены руками в шести местах, и стерёг их
# комментарий. Списки свели в один, а проверки ниже стерегут то, что
# комментарий обещал: полноту списка и попадание колонки в своё поле.
# Цена расхождения названа там же — у колонок прогона одинаковые типы,
# поэтому перепутанные местами `note` и `finish_note` не уронили бы ничего,
# кроме смысла.


@pytest.mark.parametrize(
    ("table", "listed"),
    [
        ("journal_session", _SESSION_COLUMNS),
        ("journal_trade", _TRADE_IDENTITY),
    ],
)
def test_the_listed_columns_are_the_columns_of_the_table(
    store: CandleStore, table: str, listed: tuple[str, ...]
) -> None:
    """Список колонок в коде — про ту самую таблицу, а не про её прошлую жизнь.

    Колонка, переименованная в схеме и забытая в списке, дала бы отказ
    на первом же чтении журнала — то есть в бою, а не здесь.
    """
    actual = [
        str(row[1]) for row in past_the_front_door(store, f"PRAGMA table_info({table})")
    ]
    assert set(listed) <= set(actual), (
        f"{table}: в списке есть колонки, которых в таблице нет: "
        f"{sorted(set(listed) - set(actual))}"
    )
    if table == "journal_session":
        assert list(listed) == actual, (
            "порядок колонок прогона разошёлся с таблицей: чтение собирает "
            "прогон по счёту, а не по имени"
        )


def test_every_column_of_a_run_has_a_field_of_its_own() -> None:
    """Колонок и полей прогона поровну, и зовутся они одинаково.

    Колонка, заведённая в таблице и забытая в `JournalSession`, читалась бы
    из базы и выбрасывалась молча: `_session_of` берёт первые одиннадцать
    значений и на двенадцатое не смотрит.
    """
    fields = tuple(field.name for field in dataclasses.fields(JournalSession))
    assert set(_SESSION_COLUMNS) == set(fields), (
        "колонки прогона и поля JournalSession разошлись: "
        f"лишние колонки {sorted(set(_SESSION_COLUMNS) - set(fields))}, "
        f"поля без колонки {sorted(set(fields) - set(_SESSION_COLUMNS))}"
    )


def test_the_text_columns_of_a_run_are_the_fields_it_is_opened_with() -> None:
    """Через чистку от секретов идёт **каждое** текстовое поле объявления.

    Поле, заведённое в `SessionRecord` и забытое в списке текстовых колонок,
    не записалось бы вовсе и не почистилось бы — а заметили бы это по пустой
    колонке в журнале через полгода.
    """
    declared = tuple(
        field.name
        for field in dataclasses.fields(SessionRecord)
        if field.name != "origin"
    )
    assert _SESSION_TEXT_COLUMNS == declared, (
        "список текстовых колонок разошёлся с полями SessionRecord"
    )


def test_a_new_column_of_a_run_is_frozen_unless_it_closes_it() -> None:
    """Замороженные поля выводятся вычитанием, а не перечисляются.

    Двенадцатая колонка, заведённая завтра, обязана оказаться замороженной
    сама. Перечисленная руками, она попала бы под охрану только если о ней
    вспомнить, — а забытая колонка правилась бы обычным `UPDATE` у боевого
    прогона, и это ровно та дыра, которую нашли у `note`.
    """
    covered = {"id", *_CLOSING_SESSION_COLUMNS, *_FROZEN_SESSION_COLUMNS}
    assert covered == set(_SESSION_COLUMNS), (
        "колонка прогона не отнесена ни к замороженным, ни к закрывающим: "
        f"{sorted(set(_SESSION_COLUMNS) - covered)}"
    )
    assert "note" in _FROZEN_SESSION_COLUMNS
    assert "finish_note" not in _FROZEN_SESSION_COLUMNS


def test_the_columns_of_the_old_schema_all_still_exist() -> None:
    """Перенос со схемы 3 читает колонки, которые в нынешней таблице есть.

    Список схемы 3 намеренно не выводится из нынешнего — он описывает чужую,
    уже неизменяемую схему. Но `INSERT … SELECT` при пересборке подставляет
    его в **новую** таблицу, и колонка, убранная из неё, уронила бы миграцию
    на настоящей старой базе. Тесты бы это пропустили: они мигрируют базу,
    собранную рядом.
    """
    assert set(_SESSION_COLUMNS_BEFORE_TWO_NOTES) <= set(_SESSION_COLUMNS), (
        "перенос со схемы 3 читает колонки, которых в нынешней таблице нет: "
        f"{sorted(set(_SESSION_COLUMNS_BEFORE_TWO_NOTES) - set(_SESSION_COLUMNS))}"
    )


def test_a_run_is_read_into_the_fields_its_columns_name(store: CandleStore) -> None:
    """Каждое значение попадает в поле, названное его колонкой.

    Проверка, которой не было: сборка прогона идёт по счёту колонок, типы
    у них одинаковые, и `note`, поменянный местами с `finish_note`, прошёл бы
    все прежние проверки. Поэтому каждому полю здесь дано своё слово —
    одинаковые значения перестановку не ловят.
    """
    at = msk(2026, 9, 3, 10, 0)
    session = store.open_journal_session(
        SessionRecord(
            RunOrigin.LIVE,
            symbol="инструмент",
            timeframe="размер свечи",
            strategy="торговый модуль",
            settings="снимок настроек",
            app_version="версия программы",
            note="условие открытия",
        ),
        now=at,
    )
    store.finish_journal_session(
        session.id, now=at + timedelta(hours=1), note="чем кончился"
    )

    (row,) = past_the_front_door(
        store,
        f"SELECT {_SESSION_SELECT} FROM journal_session WHERE id = ?",
        session.id,
    )
    in_base = dict(zip(_SESSION_COLUMNS, row, strict=True))
    read = stored_session(store, session.id)

    assert read.id == in_base["id"]
    assert read.origin.value == in_base["origin"]
    assert read.started_at.timestamp() == in_base["started_at"]
    assert read.finished_at is not None
    assert read.finished_at.timestamp() == in_base["finished_at"]
    for name in (*_SESSION_TEXT_COLUMNS, "finish_note"):
        assert getattr(read, name) == in_base[name], (
            f"колонка {name} прочитана не в своё поле: в базе "
            f"{in_base[name]!r}, в прогоне {getattr(read, name)!r}"
        )


def test_a_run_comes_back_from_the_base_it_was_written_into(
    store: CandleStore,
) -> None:
    """Открытие прогона отдаёт то, что записано, а не то, что подано.

    Прогон собирался вторым набором полей рядом с `INSERT`, и разойтись
    они могли молча. Теперь он читается обратно тем же путём, что и всегда, —
    значит, ответ доказывает запись, а не повторяет аргумент.
    """
    at = msk(2026, 9, 3, 10, 0)
    given = SessionRecord(
        RunOrigin.PAPER,
        symbol="MXU6",
        timeframe="5 минут",
        strategy="средняя с тейком",
        settings="EMA(15), тейк 0,5 %, окно 10:05–11:00",
        app_version="1.0",
        note="автозапуск",
    )
    opened = store.open_journal_session(given, now=at)

    assert opened == stored_session(store, opened.id)
    assert opened.record == given
    assert opened.finished_at is None
    assert opened.finish_note == ""
