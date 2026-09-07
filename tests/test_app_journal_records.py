"""Дорога журнала: `engine/` → `market/` → база → `ui/`.

Проверяется целиком то, ради чего существует перекладка. `market/` не
импортирует ни один слой проекта (ARCHITECTURE.md §2), поэтому строку журнала
и сделку в базу кладёт `app/`, а не движок. Если перекладка теряет поле, это
не падает нигде: журнал просто становится беднее, и заметят это через месяц
на разборе торгового дня.

Отдельно — сверка двух рубежей чистки секретов. Реестр живых значений живёт
в `broker/`, образцы — в `market/`, и разъехаться они могут молча.

⚠️ В файле лежат строки формы токена — иначе чистку проверять нечем. Все они
**подделка-для-теста**: значения нарочно написаны так, что принять их
за настоящий токен нельзя, и объявление стоит здесь ради детектора секретов
из `tests/test_layers.py`. Настоящее значение в этот файл попасть не должно
никогда — детектор его больше не увидит.
"""

from __future__ import annotations

import pathlib
from datetime import datetime, timedelta

import pytest

from app import convert
from backtest import Deal
from broker.redaction import MASK as BROKER_MASK
from broker.redaction import scrub
from engine import ExitReason, JournalEntry, JournalLevel
from engine import Side as EngineSide
from market import MSK, SECRET_MASK, CandleStore, RunOrigin, SessionRecord, redact
from ui.models import DecisionLevel, Side
from ui.models import RunOrigin as ShownRunOrigin


def entry(at: datetime, event: str, level: JournalLevel) -> JournalEntry:
    return JournalEntry(at=at, event=event, reason=f"причина: {event}", level=level)


def deal(at: datetime, *, commission: float | None = None) -> Deal:
    return Deal(
        side=EngineSide.SHORT,
        volume=2.0,
        entry_time=at,
        entry_price=312500.0,
        entry_order_id="вход-7",
        exit_time=at + timedelta(minutes=5),
        exit_price=311000.0,
        exit_order_id="выход-7",
        exit_reason=ExitReason.TAKE_PROFIT,
        commission=commission,
        ruble_per_point=1.0,
    )


# -- журнал решений ----------------------------------------------------------


@pytest.mark.parametrize(
    ("level", "shown"),
    [
        (JournalLevel.INFO, DecisionLevel.INFO),
        (JournalLevel.TRADE, DecisionLevel.TRADE),
        (JournalLevel.WARNING, DecisionLevel.WARNING),
        (JournalLevel.ERROR, DecisionLevel.ERROR),
    ],
)
def test_a_decision_survives_the_whole_road(
    tmp_path: pathlib.Path, level: JournalLevel, shown: DecisionLevel
) -> None:
    """Строка движка доезжает до окна через базу без потерь.

    Все четыре уровня по отдельности: уровень задаёт цвет строки в окне,
    и подмена одного другим не падает, а тихо перекрашивает предупреждение
    в обычную запись.
    """
    at = datetime(2026, 9, 3, 10, 10, tzinfo=MSK)
    written = entry(at, "Вход в лонг", level)

    with CandleStore(tmp_path / "candles.sqlite3") as store:
        session = store.open_journal_session(SessionRecord(RunOrigin.BACKTEST), now=at)
        store.write_decision(session.id, convert.decision_record(written))
        (stored,) = store.decisions()

    row = convert.row_of_stored_decision(stored)
    assert row.time == written.at
    assert row.event == written.event
    assert row.reason == written.reason
    assert row.level is shown
    # Происхождение прогона доезжает вместе со строкой. Прежде оно терялось
    # ровно здесь: у строки окна такого поля не было, и в выгрузке прогон
    # по истории выглядел как боевой день (решение 0011, задача Э1-10б).
    assert row.origin is ShownRunOrigin.BACKTEST
    # Та же строка, что построило бы окно без базы вовсе. Происхождение
    # называет тот, кто затеял прогон: движок про режим не знает.
    assert row == convert.decision_row(written, origin=ShownRunOrigin.BACKTEST)


def test_the_road_through_the_base_keeps_the_order(tmp_path: pathlib.Path) -> None:
    """Десять строк одной свечи возвращаются в том же порядке.

    Время у них одно и то же — порядок держится на номере записи.
    """
    at = datetime(2026, 9, 3, 10, 10, tzinfo=MSK)
    events = [f"шаг {number}" for number in range(10)]

    with CandleStore(tmp_path / "candles.sqlite3") as store:
        session = store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)
        store.write_decisions(
            session.id,
            [convert.decision_record(entry(at, name, JournalLevel.INFO)) for name in events],
        )
        rows = [convert.row_of_stored_decision(one) for one in store.decisions()]

    assert [row.event for row in rows] == events


# -- журнал сделок -----------------------------------------------------------


def test_a_deal_survives_the_whole_road(tmp_path: pathlib.Path) -> None:
    at = datetime(2026, 9, 3, 10, 10, tzinfo=MSK)
    made = deal(at, commission=9.9)

    with CandleStore(tmp_path / "candles.sqlite3") as store:
        session = store.open_journal_session(SessionRecord(RunOrigin.BACKTEST), now=at)
        store.write_trades(session.id, [convert.trade_record(made, "MXU6")])
        (stored,) = store.trades()

    assert stored.symbol == "MXU6"
    row = convert.row_of_stored_trade(stored)
    assert row.origin is ShownRunOrigin.BACKTEST, (
        "сделка доехала до окна без происхождения: в отчёте за день прогон "
        "по истории неотличим от настоящих денег"
    )
    assert row == convert.trade_row(made, origin=ShownRunOrigin.BACKTEST), (
        "сделка, прошедшая через базу, показывается иначе, чем та же сделка "
        "прямо из прогона"
    )
    assert row.side is Side.SHORT
    assert row.commission_rub == 9.9


def test_without_a_tariff_the_stored_deal_keeps_the_unknown(
    tmp_path: pathlib.Path,
) -> None:
    """`None` в комиссии — «тариф не задан», а не ноль.

    Ноль объявил бы любую сделку окупившей комиссию, а реверсная система
    на пятиминутках делает много переворотов (DOMAIN.md §5).
    """
    at = datetime(2026, 9, 3, 10, 10, tzinfo=MSK)
    made = deal(at, commission=None)

    with CandleStore(tmp_path / "candles.sqlite3") as store:
        session = store.open_journal_session(SessionRecord(RunOrigin.BACKTEST), now=at)
        store.write_trades(session.id, [convert.trade_record(made, "MXU6")])
        (stored,) = store.trades()

    assert stored.commission is None
    assert stored.net is None
    row = convert.row_of_stored_trade(stored)
    assert row.commission_rub is None
    assert row.profit_rub == made.gross


def test_the_money_is_stored_as_counted_not_recounted(tmp_path: pathlib.Path) -> None:
    """Числа берутся записанными. Пересчёт менял бы прошлое при смене тарифа."""
    at = datetime(2026, 9, 3, 10, 10, tzinfo=MSK)
    made = deal(at, commission=9.9)

    with CandleStore(tmp_path / "candles.sqlite3") as store:
        session = store.open_journal_session(SessionRecord(RunOrigin.BACKTEST), now=at)
        store.write_trades(session.id, [convert.trade_record(made, "MXU6")])
        (stored,) = store.trades()

    assert stored.gross == made.gross
    assert stored.net == made.net
    assert stored.points == made.points
    assert stored.percent == pytest.approx(made.percent)


# -- происхождение прогона на всей дороге (Э1-10б) ---------------------------


@pytest.mark.parametrize("origin", list(RunOrigin), ids=[one.value for one in RunOrigin])
def test_the_origin_of_the_run_survives_the_whole_road(
    tmp_path: pathlib.Path, origin: RunOrigin
) -> None:
    """Все три происхождения доезжают до окна, и ни одно не подменяется другим.

    По одному значению проверять мало: подмена «симуляции» на «боевой режим»
    не падает нигде, а читается как отчёт о настоящих деньгах.
    """
    at = datetime(2026, 9, 3, 10, 10, tzinfo=MSK)
    made = deal(at, commission=9.9)

    with CandleStore(tmp_path / "candles.sqlite3") as store:
        session = store.open_journal_session(SessionRecord(origin), now=at)
        store.write_decision(
            session.id, convert.decision_record(entry(at, "шаг", JournalLevel.INFO))
        )
        store.write_trades(session.id, [convert.trade_record(made, "MXU6")])
        (line,) = store.decisions()
        (closed,) = store.trades()

    shown_line = convert.row_of_stored_decision(line)
    shown_trade = convert.row_of_stored_trade(closed)
    assert shown_line.origin is not None
    assert shown_line.origin.value == origin.value
    assert shown_trade.origin is not None
    assert shown_trade.origin.value == origin.value
    assert shown_trade.origin.real_money is origin.real_money, (
        "признак «настоящие деньги» разошёлся между базой и окном"
    )


def test_the_two_lists_of_origins_read_the_same() -> None:
    """Слой данных и окно называют одно и то же одинаково.

    Список происхождений записан дважды — в `market` и в `ui.models`, — потому
    что слой окна не импортирует слой данных (ARCHITECTURE.md §2). Разойтись
    два списка могут молча: четвёртое происхождение заведут в базе, а окно
    упадёт `KeyError` уже у владельца счёта.
    """
    assert {one.value for one in RunOrigin} == {one.value for one in ShownRunOrigin}
    for one in RunOrigin:
        shown = convert.origin_of_stored(one)
        assert shown.value == one.value
        assert shown.label == one.label, (
            "одно происхождение называется в журнале и в выгрузке по-разному"
        )
        assert shown.real_money is one.real_money


# -- два рубежа чистки -------------------------------------------------------

#: Приметная подделка формы JWT — тот же приём, что у подделок
#: в `test_broker_no_leak`: символы как у настоящего токена, а прочитать
#: значение можно только как «не настоящий».
FAKE_JWT = "eyJGQUtF-NOT-A-REAL.eyJGQUtF-NOT-A-REAL.FAKE-NOT-A-REAL-TOKEN"

#: Формы, в которых токен приходит от сервера и попадает в текст отказа
#: исполнителя, а оттуда — в причину решения.
LEAKY_TEXTS = (
    f'Отказ брокера: {{"access_token": "{FAKE_JWT}"}}',
    f"Authorization: Bearer {FAKE_JWT}",
    f"refresh_token={FAKE_JWT}&grant_type=x",
    f"в ответе пришло {FAKE_JWT}",
    f'наш файл: {{"токен": "{FAKE_JWT}"}}',
)


@pytest.mark.parametrize("text", LEAKY_TEXTS)
def test_both_defences_mask_the_same_shapes(text: str) -> None:
    """Образцы `market/` не отстают от образцов `broker/`.

    Рубежа два и они в разных слоях: реестр живых значений принадлежит
    `broker/`, а слой данных импортировать его не может. Разъехаться наборы
    образцов могут молча — новый образец добавят в один слой и забудут
    в другом. Тогда журнал начнёт получать то, чего лог уже не получает.
    """
    assert "eyJGQUtF" not in scrub(text), "рубеж broker/ пропустил форму токена"
    assert "eyJGQUtF" not in redact(text), (
        "рубеж market/ пропустил форму токена, которую broker/ вырезает: "
        "наборы образцов разошлись"
    )


def test_the_two_masks_read_the_same() -> None:
    """Разные маски читались бы как два разных события."""
    assert SECRET_MASK == BROKER_MASK


def test_the_broker_cleaner_is_the_one_the_program_installs(
    tmp_path: pathlib.Path,
) -> None:
    """`app/main.py` отдаёт хранилищу чистку `broker/` — проверяем, что она годится.

    Значение, выданное этим сеансом, ни в одну известную форму не попадает:
    ни `Bearer`, ни имени поля, ни точек JWT рядом с ним нет. Вырезать его
    умеет только реестр `broker/`. Ради этого случая хранилище и берёт чистку
    снаружи, а не считает свои образцы достаточными.
    """
    from broker.secret import Secret

    live_value = "PMVdY2xTqLwEnKa8ZbRc7HuGfJi3Ns6O"
    holder = Secret(live_value)
    at = datetime(2026, 9, 3, 10, 10, tzinfo=MSK)
    try:
        with CandleStore(tmp_path / "candles.sqlite3", sanitize=scrub) as store:
            session = store.open_journal_session(SessionRecord(RunOrigin.LIVE), now=at)
            store.write_decision(
                session.id,
                convert.decision_record(
                    JournalEntry(
                        at=at,
                        event="Заявка отклонена",
                        reason=f"сервер ответил: {live_value}",
                        level=JournalLevel.ERROR,
                    )
                ),
            )
            (stored,) = store.decisions()
    finally:
        del holder

    assert live_value not in stored.reason
    assert BROKER_MASK in stored.reason
