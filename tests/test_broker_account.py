"""Разбор состояния счёта: деньги, обеспечение, фактические позиции.

Подставные ответы собраны по таблицам официальной документации БКС
(сервисы «Лимиты» и «Портфель», редакция от 30.08.2026). В сеть тесты
не ходят и настоящего токена не видят.

Главное, что здесь проверяется: **недостающее число остаётся пустым.**
Ноль в поле «гарантийное обеспечение» читается как «обеспечение
не требуется» и разрешает войти в позицию, которую нечем держать.
"""

from __future__ import annotations

import asyncio
import pathlib
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from broker.errors import UnexpectedAnswer
from broker.account import (
    SNAPSHOT_INTERVAL,
    SNAPSHOT_MAX_AGE,
    Account,
    AccountSnapshot,
    Money,
    Side,
    parse_equity,
    parse_money,
    parse_positions,
)
from broker.errors import IncompleteAccountData
from broker.instruments import Instruments, parse_instruments
from broker.secret import Secret
from broker.session import (
    AUTH_URL,
    INSTRUMENTS_BY_TICKERS_PATH,
    LIMITS_PATH,
    PORTFOLIO_PATH,
    BrokerSession,
)
from broker.throttle import Throttle
from broker.token_store import TokenStore
from broker.tokens import ACCESS_LIFETIME, TokenScope

FAKE_REFRESH = "FAKE-account-refresh-0000-NOT-A-REAL-TOKEN"
FAKE_ACCESS = "FAKE-account-access-1111-NOT-A-REAL-TOKEN"
START = datetime(2026, 8, 30, 9, 0, tzinfo=timezone.utc)


# --- подставные ответы ---

LIMITS = {
    "moneyLimit": {
        "exchange": "MOEX",
        "currencyCode": "SUR",
        "instrumentType": "money",
        "locked": 1500.0,
        "quantity": {"type": "T0", "value": 250000.0},
        "loadDate": "2026-08-30T09:01:00.000Z",
    },
    "futuresLimit": {
        "currencyCode": "SUR",
        "exchange": "MOEX",
        "accruedInt": 320.0,
        "cbpl": {"init": 180000.0},
        "cbplUsedForOrders": 0.0,
        "cbplUsedForPositions": 41250.0,
        "instrumentType": "money",
        "varMargin": 1180.0,
        "realVarMargin": 0.0,
        "loadDate": "2026-08-30T09:01:00.000Z",
    },
}

PORTFOLIO = {
    "positions": [
        {
            "type": "futuresHolding",
            "subAccountId": "1",
            "agreementId": "9876543",
            "account": "FORTS",
            "exchange": "MOEX",
            "ticker": "MXU6",
            "displayName": "Фьючерс на индекс МосБиржи",
            "baseAssetTicker": "MIX",
            "currency": "SUR",
            "expireDate": "2026-09-17T18:50:00.000Z",
            "instrumentType": "FUTURES",
            "term": "T0",
            "quantity": 3,
            "locked": 0,
            "balancePrice": 285400.0,
            "currentPrice": 286100.0,
            "scale": 0,
            "minimumStep": 25.0,
            "board": "SPBFUT",
            "ratioQuantity": 1,
            "lockedForFutures": 41250.0,
            "isBlocked": False,
        }
    ]
}

def held(**fields: object) -> dict[str, object]:
    """Запись портфеля-позиции: `type` обязателен, и это не формальность.

    Разбор отбирает позиции по `type` (05.09.2026): денежная запись
    `moneyLimit` иначе становится позицией с количеством, равным сумме
    денег на счёте. Образец без типа проверял бы разбор, которого больше нет.
    """
    return {"type": "futuresHolding", "ticker": "MXU6", **fields}


INSTRUMENTS = {
    "instruments": [
        {
            "ticker": "MXU6",
            "boards": [{"classCode": "SPBFUT", "exchange": "MOEX"}],
            "shortName": "MIX-9.26",
            "displayName": "Фьючерс на индекс МосБиржи",
            "instrumentType": "FUTURES",
            "tradingCurrency": "SUR",
            "scale": 0,
            "minimumStep": 25.0,
            "stepPrice": 25.0,
            "lotSize": 1.0,
            "maturityDate": "2026-09-17T18:50:00.000Z",
            "baseAsset": "MIX",
        }
    ]
}


# --- деньги и обеспечение ---


def test_money_is_read_from_the_documented_containers() -> None:
    money = parse_money(LIMITS)
    assert money.money_total == 250000.0
    assert money.money_locked == 1500.0
    assert money.free_rub == 248500.0
    assert money.collateral_positions == 41250.0
    assert money.collateral_orders == 0.0
    assert money.collateral_total == 41250.0
    assert money.futures_limit == 180000.0
    assert money.variation_margin == 1180.0
    assert money.complete is True


def test_zero_collateral_is_a_number_not_a_gap() -> None:
    """Ноль, который прислал брокер, — это ноль. Пустота — это пустота."""
    money = parse_money(LIMITS)
    assert money.collateral_orders == 0.0
    assert "гарантийное обеспечение под заявками" not in money.missing


def test_missing_collateral_stays_empty() -> None:
    """Нет поля — нет числа. Ноль здесь разрешил бы вход без обеспечения."""
    broken = {"moneyLimit": LIMITS["moneyLimit"], "futuresLimit": {"currencyCode": "SUR"}}
    money = parse_money(broken)
    assert money.collateral_positions is None
    assert money.collateral_orders is None
    assert money.collateral_total is None
    assert money.complete is False
    assert "гарантийное обеспечение под позициями" in money.missing


def test_empty_answer_reports_everything_missing() -> None:
    money = parse_money({})
    assert money.free_rub is None
    assert money.complete is False
    assert len(money.missing) == 3


def test_alternative_spelling_of_the_collateral_field() -> None:
    """Документация набрана с опечатками — разбор переживает оба написания."""
    variant = {
        "moneyLimit": LIMITS["moneyLimit"],
        "futuresLimit": {"cbplusedForPositions": 41250.0, "cbplusedForOrders": 0.0},
    }
    money = parse_money(variant)
    assert money.collateral_positions == 41250.0
    assert money.collateral_orders == 0.0


def test_marginal_indicators_take_priority_for_free_cash() -> None:
    """Когда появится поток маржинальных показателей, свободные деньги берутся оттуда."""
    marginal = {"fortsStability": {"freeCash": 199000.0, "ownFunds": 240250.0}}
    money = parse_money(LIMITS, marginal)
    assert money.free_rub == 199000.0
    assert money.money_total == 250000.0, "исходные числа обязаны остаться видны"


def test_require_money_refuses_incomplete_data() -> None:
    """Проверка средств перед заявкой на неполных данных не делается."""
    snapshot = _snapshot(parse_money({}))
    with pytest.raises(IncompleteAccountData) as caught:
        snapshot.require_money()
    error = caught.value
    assert "не полностью" in error.human
    assert "в бой не пойдёт" in error.human
    assert "свободные средства" in error.human


def test_require_money_passes_complete_data() -> None:
    snapshot = _snapshot(parse_money(LIMITS))
    assert snapshot.require_money().free_rub == 248500.0


# --- позиции ---


def test_positions_are_read_with_side_and_average_price() -> None:
    positions, account = parse_positions(PORTFOLIO)
    assert account == "9876543"
    assert len(positions) == 1

    item = positions[0]
    assert item.ticker == "MXU6"
    assert item.side is Side.LONG
    assert item.quantity == 3
    assert item.average_price == 285400.0
    assert item.current_price == 286100.0
    assert item.price_step == 25.0
    assert item.lot == 1
    assert item.class_code == "SPBFUT"
    assert item.locked_for_futures == 41250.0
    assert item.expires_on == datetime(2026, 9, 17, 18, 50, tzinfo=timezone.utc)
    assert item.open is True


@pytest.mark.parametrize(
    ("quantity", "side"),
    [(3, Side.LONG), (-3, Side.SHORT), (0, Side.FLAT)],
)
def test_side_follows_the_sign(quantity: int, side: Side) -> None:
    """Шорт брокер показывает отрицательным количеством."""
    payload = {"positions": [held(quantity=quantity)]}
    positions, _ = parse_positions(payload)
    assert positions[0].side is side
    assert positions[0].open is (side is not Side.FLAT)


def test_flat_position_is_not_counted_as_open() -> None:
    payload = {"positions": [held(quantity=0)]}
    positions, _ = parse_positions(payload)
    snapshot = _snapshot(parse_money(LIMITS), positions)
    assert snapshot.open_positions() == ()
    assert snapshot.position("mxu6") is not None, "поиск по тикеру не зависит от регистра"


def test_empty_portfolio_is_not_an_error() -> None:
    """Позиций нет — это нормальное состояние, а не отказ."""
    positions, account = parse_positions({"positions": []})
    assert positions == ()
    assert account is None


def test_portfolio_of_unknown_shape_is_a_refusal_not_an_empty_list() -> None:
    """Ответ не того вида — отказ. «Позиций нет» здесь означало бы вход поверх.

    Прежде этот тест требовал обратного: «прислали не то — считаем, что
    позиций нет». Это и есть молчаливый ноль. Позиция, которую брокер
    показал, а мы не прочитали, — разрешение войти второй раз: сверка после
    обрыва (`DOMAIN.md` §7, шаг 2) спрашивает у брокера **фактическую**
    позицию и верит ответу.
    """
    for answer in ({"positions": "не массив"}, {}, {"positions": None}, "строка", 42):
        with pytest.raises(UnexpectedAnswer) as caught:
            parse_positions(answer)
        assert "портфель" in str(caught.value)

    # А вот запись **известного вида** без тикера — не отказ: разобрать
    # нельзя одну строку, а не весь ответ. Такое пропускается молча.
    positions, _ = parse_positions(
        {"positions": [{"type": "futuresHolding", "нет тикера": 1}]}
    )
    assert positions == ()


def test_money_in_the_portfolio_is_not_a_position() -> None:
    """Денежная запись позицией не становится — иначе плоский счёт «не пуст».

    ⚠️ Тот самый образец, который до 05.09.2026 не подавался в `parse_positions`
    ни разу: он использовался только для размера счёта. Разбор не смотрел
    на `type`, и `{"type": "moneyLimit", "ticker": "RUB", "quantity": 208750}`
    давал `Position(ticker="RUB", side=LONG, quantity=208750)`.

    Цена ошибки названа в решении 0020: сверка позиции после обрыва увидела бы
    открытую позицию на плоском счёте и остановила бы робота **с попыткой
    закрыть то, чего нет**.
    """
    positions, _ = parse_positions(PORTFOLIO_WITH_MONEY)
    assert [item.ticker for item in positions] == ["MXU6"], (
        "деньги прочитаны как позиция"
    )
    assert [item.quantity for item in positions] == [3.0]


def test_a_flat_account_shows_no_open_positions_at_all() -> None:
    """Счёт без позиций: одни деньги — и `open_positions()` пуст.

    Парная проверка к предыдущей и главная по последствиям: именно пустоту
    этого списка сверка после обрыва читает как «позиции нет».
    """
    flat = {"positions": [PORTFOLIO_WITH_MONEY["positions"][0]]}
    positions, _ = parse_positions(flat)
    snapshot = _snapshot(parse_money(LIMITS), positions)
    assert snapshot.open_positions() == ()


def test_the_account_number_is_read_from_the_money_record_too() -> None:
    """Номер счёта берётся из любой записи: на плоском счёте позиций нет.

    Отбор по `type` не имеет права заодно потерять номер счёта — он нужен
    в панели и в журнале как раз тогда, когда позиций нет.
    """
    flat = {"positions": [
        {"type": "moneyLimit", "ticker": "RUB", "agreementId": "9876543",
         "quantity": 1.0},
    ]}
    positions, account = parse_positions(flat)
    assert positions == ()
    assert account == "9876543"


@pytest.mark.parametrize(
    "kind",
    ["FUTURES_HOLDING", "futures_holding", "futuresholding", " futuresHolding "],
    ids=["ЗАГЛАВНЫЕ", "с подчёркиванием", "слитно", "с пробелами"],
)
def test_the_type_is_matched_by_word_not_by_spelling(kind: str) -> None:
    """Регистр и разделители в `type` не решают, позиция это или деньги.

    Как именно сервер пишет это поле, живьём не проверено ни разу
    (`.docs/broker-api/04-portfolio.md`). Отказывать в работе из-за стиля
    именования значило бы встать в торговое время на пустом месте;
    различать надо слова.
    """
    positions, _ = parse_positions({"positions": [held(type=kind, quantity=3)]})
    assert [item.ticker for item in positions] == ["MXU6"]


@pytest.mark.parametrize(
    "record",
    [
        {"ticker": "MXU6", "quantity": 3},
        {"type": "somethingNew", "ticker": "MXU6", "quantity": 3},
        {"type": "", "ticker": "MXU6", "quantity": 3},
    ],
    ids=["типа нет вовсе", "тип незнакомый", "тип пустой"],
)
def test_an_unclassifiable_record_is_a_refusal_not_a_guess(record: dict) -> None:
    """Не поняли вид записи — отказ. Обе догадки стоят денег, и в разные стороны.

    Счесть позицией — призрак, который сверка после обрыва попытается
    закрыть. Счесть деньгами — пропущенная позиция, поверх которой робот
    войдёт вторым объёмом. Отказ виден сразу и стоит несделанной сделки.
    """
    with pytest.raises(UnexpectedAnswer) as caught:
        parse_positions({"positions": [record]})
    assert "неизвестного вида" in str(caught.value)
    assert "futuresHolding".lower() in caught.value.technical.lower(), (
        "технический текст не называет, что программа считает известным"
    )


def test_a_stock_holding_is_a_position_too() -> None:
    """`depoLimit` — позиция по бумагам, а не деньги.

    Проверяется потому, что белый список из одного значения выглядел бы так же
    и был бы неверен: счёт с акциями показал бы отказ на ровном месте.
    """
    payload = {"positions": [
        {"type": "depoLimit", "ticker": "SBER", "quantity": 100, "board": "TQBR"},
    ]}
    positions, _ = parse_positions(payload)
    assert [item.ticker for item in positions] == ["SBER"]


def test_portfolio_is_read_both_as_a_bare_array_and_as_a_container() -> None:
    """Сайт обещает голый массив, PDF — контейнер. Читаются оба вида.

    У справочника инструментов это же расхождение кончилось багом `B-014`:
    живой сервер прислал массив, разбор ждал контейнер, программа решила,
    что тикера нет. Здесь цена ошибки — незамеченная открытая позиция.
    """
    container, account = parse_positions(PORTFOLIO)
    bare, bare_account = parse_positions(PORTFOLIO["positions"])
    assert bare == container
    assert bare_account == account == "9876543"
    assert bare[0].quantity == 3


def test_technical_text_about_a_broken_portfolio_carries_no_values() -> None:
    """В лог уходят имена полей, а не содержимое: там номера счетов."""
    with pytest.raises(UnexpectedAnswer) as caught:
        parse_positions({"account": "9876543/25", "нетПозиций": True})
    technical = caught.value.technical
    assert "account" in technical, "имена полей нужны для разбора"
    assert "9876543" not in technical, "значения из ответа в лог не идут"


# --- размер счёта, ГО контракта и темп опроса ---


PORTFOLIO_WITH_MONEY = {
    "positions": [
        {"type": "moneyLimit", "ticker": "RUB", "currency": "SUR", "quantity": 208750.0,
         "currentValueRub": 208750.0},
        {"type": "futuresHolding", "ticker": "MXU6", "board": "SPBFUT", "quantity": 3,
         "balancePrice": 285400.0, "currentPrice": 286100.0, "lockedForFutures": 41250.0,
         "currentValueRub": 2100.0},
    ]
}


def test_account_size_is_the_sum_of_rouble_values() -> None:
    """Размер счёта = деньги плюс позиции: от него считается дневной лимит."""
    assert parse_equity(PORTFOLIO_WITH_MONEY) == 210850.0


def test_one_entry_without_value_cancels_the_whole_sum() -> None:
    """Занижённый размер счёта — это занижённый лимит убытка и ранняя остановка."""
    half_known = {"positions": [
        PORTFOLIO_WITH_MONEY["positions"][0],
        {"type": "futuresHolding", "ticker": "MXU6", "quantity": 3},
    ]}
    assert parse_equity(half_known) is None, "сумма собралась по неполным данным"


def test_empty_portfolio_gives_no_account_size_rather_than_zero() -> None:
    """Ноль здесь означал бы дневной лимит ноль — остановку на первом убытке."""
    assert parse_equity({"positions": []}) is None
    assert parse_equity([]) is None


def test_marginal_indicators_are_the_first_source_of_account_size() -> None:
    """Появится поток — размер счёта берётся прямым полем, а не суммой."""
    marginal = {"portfolioCurrentValue": {"currentValueRub": 199999.0}}
    assert parse_equity(PORTFOLIO_WITH_MONEY, marginal) == 199999.0


def test_funds_missing_names_what_the_engine_will_not_get() -> None:
    """Пока чего-то нет — движку деньги не кладут, и видно, чего именно нет."""
    full = _snapshot(parse_money(LIMITS))
    assert full.funds_missing() == ("размер счёта",), "размер счёта не спросили"

    ready = AccountSnapshot(
        taken_at=START, account=None, money=parse_money(LIMITS), equity_rub=210850.0
    )
    assert ready.funds_missing() == ()

    blind = AccountSnapshot(taken_at=START, account=None, money=parse_money({}))
    assert "свободные средства" in blind.funds_missing()
    assert "размер счёта" in blind.funds_missing()


def test_missing_collateral_does_not_stop_the_engine_from_getting_money() -> None:
    """ГО под позициями не прочиталось — деньги движку всё равно кладут.

    Обе величины гарантийного обеспечения движок не считает ни в одном
    предохранителе, а имена их полей выписаны из таблицы документации
    с опечатками и живьём не проверены. Отказ строить `AccountFunds`
    из-за поля, которое никто не читает, стоил бы торгового дня.

    ⚠️ Полнота ответа — **другой** вопрос: `require_money()` на этом же
    снимке обязан отказать.
    """
    partial = {
        "moneyLimit": LIMITS["moneyLimit"],
        "futuresLimit": {"currencyCode": "SUR", "cbpLimit": 180000.0},
    }
    money = parse_money(partial)
    assert money.complete is False, "образец перестал быть неполным"
    snapshot = AccountSnapshot(
        taken_at=START, account=None, money=money, equity_rub=210850.0
    )
    assert snapshot.funds_missing() == ()
    with pytest.raises(IncompleteAccountData):
        snapshot.require_money()


def test_margin_per_contract_is_derived_from_the_open_position() -> None:
    """ГО одного контракта — занятое под позицию, делённое на число контрактов."""
    positions, _ = parse_positions(PORTFOLIO)
    snapshot = _snapshot(parse_money(LIMITS), positions)
    assert snapshot.margin_per_contract("MXU6") == 13750.0
    assert snapshot.margin_per_contract("mxu6") == 13750.0, "регистр тикера не важен"
    assert snapshot.margin_per_contract("SiU6") is None, "чужой тикер"


@pytest.mark.parametrize(
    "position",
    [
        held(quantity=0, lockedForFutures=41250.0),
        held(quantity=3),
        held(quantity=3, lockedForFutures=0.0),
    ],
    ids=["позиции нет", "поле не прислано", "ноль под ГО"],
)
def test_margin_per_contract_stays_unknown_rather_than_zero(position: dict) -> None:
    """Ноль означал бы «свободных средств хватит на любой объём»."""
    positions, _ = parse_positions({"positions": [position]})
    snapshot = _snapshot(parse_money(LIMITS), positions)
    assert snapshot.margin_per_contract("MXU6") is None


def test_snapshot_has_its_own_shelf_life() -> None:
    """Срок годности снимка назван в слое, а не выдуман вызывающим."""
    snapshot = _snapshot(parse_money(LIMITS))
    assert SNAPSHOT_INTERVAL < SNAPSHOT_MAX_AGE, "опрос реже срока годности"
    assert snapshot.fresh(START + SNAPSHOT_MAX_AGE - timedelta(seconds=1))
    assert not snapshot.fresh(START + SNAPSHOT_MAX_AGE + timedelta(seconds=1))


# --- валюта контейнеров «Лимитов» ---


def test_containers_are_read_in_plural_too() -> None:
    """Сайт пишет `moneyLimits` и `futuresLimits`, PDF — в единственном числе."""
    plural = {
        "moneyLimits": [LIMITS["moneyLimit"]],
        "futuresLimits": [LIMITS["futuresLimit"]],
    }
    money = parse_money(plural)
    assert money.free_rub == 248500.0
    assert money.collateral_positions == 41250.0
    assert money.complete is True


def test_dollars_are_not_read_as_roubles() -> None:
    """Контейнер приходит по валютам, и «взять первый» читает доллары рублями."""
    mixed = {
        "moneyLimits": [
            {"currencyCode": "USD", "locked": 0.0, "quantity": {"value": 1000.0}},
            LIMITS["moneyLimit"],
        ],
        "futuresLimits": [LIMITS["futuresLimit"]],
    }
    assert parse_money(mixed).money_total == 250000.0


def test_two_rouble_blocks_are_a_gap_rather_than_a_guess() -> None:
    """Сложить их наугад нельзя: пустое число видно, выдуманное — нет."""
    doubled = {
        "moneyLimits": [LIMITS["moneyLimit"], LIMITS["moneyLimit"]],
        "futuresLimits": [LIMITS["futuresLimit"]],
    }
    money = parse_money(doubled)
    assert money.money_total is None
    assert "свободные средства" in money.missing


def _two_exchanges() -> dict:
    """Рубли фондового рынка рядом с рублями срочного — обычный ответ.

    Документация (`03-limits.md`) даёт каждому элементу и `currencyCode`,
    и `exchange`. До 05.09.2026 правило «рублёвый блок ровно один» объявляло
    такой ответ неоднозначным целиком.
    """
    return {
        "moneyLimits": [
            {**LIMITS["moneyLimit"], "exchange": "MOEX", "quantity": {"value": 90000.0}},
            {**LIMITS["moneyLimit"], "exchange": "FORTS"},
        ],
        "futuresLimits": [
            {**LIMITS["futuresLimit"], "exchange": "MOEX",
             "cbplUsedForPositions": 1.0},
            {**LIMITS["futuresLimit"], "exchange": "FORTS"},
        ],
    }


def test_the_named_exchange_picks_its_rouble_block() -> None:
    """Названа биржа — берётся её блок, а не «первый рублёвый».

    Числа у двух блоков **расходятся** намеренно: на одинаковых тест был бы
    зелёным и у разбора, который берёт любой.
    """
    money = parse_money(_two_exchanges(), exchange="FORTS")
    assert money.money_total == 250000.0, "взят блок чужой биржи"
    assert money.collateral_positions == 41250.0
    assert money.complete is True
    assert money.problems == ()


def test_the_exchange_is_matched_regardless_of_case_and_spaces() -> None:
    """`forts`, `FORTS` и ` FORTS ` — одна биржа: различать надо слова."""
    money = parse_money(_two_exchanges(), exchange=" forts ")
    assert money.money_total == 250000.0


def test_without_a_named_exchange_two_rouble_blocks_say_why() -> None:
    """Биржу не назвали — числа пусты, и причина названа **технически**.

    Ровно та находка ревью: `missing` перечисляет имена полей, и разбор
    уходил искать опечатку в написании, тогда как причина другая. Теперь
    в техническом тексте отказа стоит, сколько рублёвых блоков пришло
    и с каких бирж.
    """
    money = parse_money(_two_exchanges())
    assert money.money_total is None
    assert money.complete is False
    said = " | ".join(money.problems)
    assert "FORTS" in said and "MOEX" in said, said
    assert "рублёвых блоков 2" in said, said

    snapshot = AccountSnapshot(taken_at=START, account=None, money=money)
    with pytest.raises(IncompleteAccountData) as caught:
        snapshot.require_money()
    assert "FORTS" in caught.value.technical, (
        "отказ не называет настоящую причину, и разбор пойдёт по ложному следу"
    )


def test_a_named_exchange_that_is_absent_is_a_gap_not_a_fallback() -> None:
    """Названной биржи в ответе нет — числа пусты, а не «возьмём соседнюю».

    Подстановка соседнего блока положила бы деньги фондового рынка
    в проверку обеспечения срочного, где их нет.
    """
    money = parse_money(_two_exchanges(), exchange="SPBFUT")
    assert money.money_total is None
    assert "SPBFUT" in " | ".join(money.problems)


def test_futures_limit_is_read_by_the_name_from_the_site() -> None:
    """`cbpLimit` — имя со страницы сайта; `cbpl.init` в PDF набрано с опечаткой."""
    variant = {
        "moneyLimit": LIMITS["moneyLimit"],
        "futuresLimit": {"currencyCode": "SUR", "cbpLimit": 180000.0,
                         "cbplUsedForOrders": 0.0, "cbplUsedForPositions": 41250.0},
    }
    assert parse_money(variant).futures_limit == 180000.0


def test_used_collateral_is_not_passed_off_as_the_limit() -> None:
    """`cbplUsed` — «позиции после клиринга», а не лимит. Чужое число хуже пустого."""
    variant = {
        "moneyLimit": LIMITS["moneyLimit"],
        "futuresLimit": {"currencyCode": "SUR", "cbplUsed": 41250.0,
                         "cbplUsedForOrders": 0.0, "cbplUsedForPositions": 41250.0},
    }
    assert parse_money(variant).futures_limit is None


def test_human_summary_reads_like_a_sentence() -> None:
    positions, account = parse_positions(PORTFOLIO)
    snapshot = _snapshot(parse_money(LIMITS), positions, account)
    text = snapshot.human_summary()
    assert "счёт 9876543" in text
    assert "MXU6" in text
    assert "лонг" in text
    assert "248\u00a0500,00 ₽" in text, text


def test_human_summary_admits_missing_money() -> None:
    snapshot = _snapshot(parse_money({}))
    assert "не прислал" in snapshot.human_summary()


def test_snapshot_freshness_is_checkable() -> None:
    """Проверка средств перед заявкой не должна опираться на старый снимок."""
    snapshot = _snapshot(parse_money(LIMITS))
    assert snapshot.fresh(snapshot.taken_at + timedelta(seconds=5), timedelta(seconds=30))
    assert not snapshot.fresh(snapshot.taken_at + timedelta(minutes=31), timedelta(seconds=30))


# --- справочник инструментов ---


def test_instrument_card_is_filled_from_the_reference() -> None:
    """ТЗ §4.2: шаг цены, лот и стоимость шага не вводятся вручную."""
    found = parse_instruments(INSTRUMENTS)
    assert len(found) == 1
    card = found[0]
    assert card.ticker == "MXU6"
    assert card.class_code == "SPBFUT"
    assert card.lot == 1.0
    assert card.price_step == 25.0
    assert card.step_price == 25.0
    assert card.maturity == datetime(2026, 9, 17, 18, 50, tzinfo=timezone.utc)
    assert card.complete_for_trading is True
    assert card.missing() == ()


def test_incomplete_card_names_what_is_missing() -> None:
    """Единица вместо неизвестного лота — ошибка объёма в разы."""
    payload = {"instruments": [{"ticker": "MXU6", "minimumStep": 25.0}]}
    card = parse_instruments(payload)[0]
    assert card.complete_for_trading is False
    assert card.missing() == ("размер лота", "стоимость шага цены")


# --- сквозной проход через сессию ---


def make_session(tmp_path: pathlib.Path, replies: dict[str, Any]) -> tuple[BrokerSession, list[str]]:
    directory = tmp_path / "userdata"
    directory.mkdir()
    keeper = TokenStore(directory)
    keeper.save(Secret(FAKE_REFRESH), TokenScope.TRADE, issued_at=START - timedelta(days=1))
    visited: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        visited.append(url)
        if url == AUTH_URL:
            return httpx.Response(
                200,
                json={
                    "expires_in": int(ACCESS_LIFETIME.total_seconds()),
                    "token_type": "bearer",
                    "scope": "openid",
                    "access_token": FAKE_ACCESS,
                },
            )
        return httpx.Response(200, json=replies[request.url.path])

    client = httpx.AsyncClient(
        base_url="https://example.invalid", transport=httpx.MockTransport(handler)
    )
    session = BrokerSession(
        keeper,
        client=client,
        clock=lambda: START,
        throttle=Throttle(rate=1000.0),
    )
    return session, visited


def test_snapshot_asks_both_services(tmp_path: pathlib.Path) -> None:
    replies = {LIMITS_PATH: LIMITS, PORTFOLIO_PATH: PORTFOLIO}

    async def scenario() -> Any:
        session, visited = make_session(tmp_path, replies)
        async with session:
            snapshot = await Account(session).snapshot()
        return snapshot, visited

    snapshot, visited = asyncio.run(scenario())
    assert any(LIMITS_PATH in url for url in visited)
    assert any(PORTFOLIO_PATH in url for url in visited)
    assert snapshot.money.free_rub == 248500.0
    assert snapshot.account == "9876543"
    assert snapshot.position("MXU6") is not None


def test_instruments_go_through_the_reference_service(tmp_path: pathlib.Path) -> None:
    replies = {INSTRUMENTS_BY_TICKERS_PATH: INSTRUMENTS}

    async def scenario() -> Any:
        session, visited = make_session(tmp_path, replies)
        async with session:
            return await Instruments(session).one("MXU6")

    card = asyncio.run(scenario())
    assert card.ticker == "MXU6"


def _snapshot(money: Money, positions: Any = (), account: str | None = None) -> Any:
    return AccountSnapshot(
        taken_at=START, account=account, money=money, positions=tuple(positions)
    )


# --- отсутствующее поле не становится нулём ---


def test_missing_locked_does_not_inflate_free_money() -> None:
    """Нет «заблокировано в заявках» — свободных денег нет, а не «весь счёт».

    Прежде отсутствующее поле молча считалось нулём, и `free_rub` равнялся
    всему счёту. Расчёт объёма от процента свободных средств завышался ровно
    на сумму в активных заявках: на счёте 250 000 с 200 000 в заявках объём
    получался впятеро больше нужного. Имена полей угаданы по таблице
    документации, где найдены опечатки, — промах по имени обязан быть виден.
    """
    without_locked = {
        "moneyLimit": {"value": 250000.0},
        "futuresLimit": LIMITS["futuresLimit"],
    }
    money = parse_money(without_locked)
    assert money.money_total == 250000.0
    assert money.free_rub is None, "отсутствующее поле подставилось нулём"
    assert "свободные средства" in money.missing
    assert money.complete is False


def test_locked_is_read_under_several_spellings() -> None:
    """Написание имени поля не должно решать, увидим ли мы заблокированное."""
    for spelling in ("locked", "lockedSum", "locked_sum", "blocked"):
        limits = {
            "moneyLimit": {"value": 250000.0, spelling: 200000.0},
            "futuresLimit": LIMITS["futuresLimit"],
        }
        money = parse_money(limits)
        assert money.free_rub == 50000.0, f"написание {spelling} не прочиталось"


def test_position_without_quantity_is_an_error_not_an_empty_portfolio() -> None:
    """Не прочитали количество — это отказ, а не «позиции нет».

    `Side.of()` читает ноль как «позиции нет». Сверка после обрыва по такому
    ответу вошла бы по текущей цене поверх уже открытой позиции: двойной объём,
    двойное обеспечение и путь к принудительному закрытию брокером.
    """
    portfolio = {"positions": [
        {"type": "futuresHolding", "ticker": "MXU6", "board": "SPBFUT",
         "averagePrice": 285000.0}
    ]}
    with pytest.raises(UnexpectedAnswer) as caught:
        parse_positions(portfolio)
    assert "количество" in str(caught.value)
