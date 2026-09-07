"""Биржевой сбор из карточки инструмента: число либо причина, по которой его нет.

Сети здесь нет ни в одном тесте: карточки — заготовленные тела ответов
(`market_helpers`). Что отдаёт настоящая биржа, замерено отдельно и записано
числами в `market/fee.py`; воспроизводить замер прогоном тестов нельзя —
прогон падал бы от чужого сбоя и зеленел от чужой удачи.

Числа в этом файле **настоящие**: 14,68 ₽ — карточка `MXU6` 04.09.2026,
14,92 ₽ — она же 05.09.2026. Разница между ними и есть то, ради чего сбор
читается у биржи, а не вписывается в настройки один раз.

Каждый тест стережёт **одну** вещь, и она названа первой строкой докстринга.
"""

from __future__ import annotations

from market_helpers import MXU6_ROW, SECURITY_COLUMNS, security_body

from market import FUTURES, SHARES, parse_security, read_commission

#: Тариф брокера за контракт на сторону у владельца счёта («Трейдер»).
#: Биржа его не знает: это условие договора, а не свойство инструмента.
BROKER_RUB = 1.2

#: Карточка `MXU6` на следующий день после `MXU6_ROW`: те же поля, другие
#: числа. Замер живым запросом 05.09.2026, 15:00 МСК.
NEXT_DAY: dict[str, object] = {
    **MXU6_ROW,
    "INITIALMARGIN": 23301.38,
    "IMTIME": "2026-09-05 10:00:02",
    "BUYSELLFEE": 14.92,
    "SCALPERFEE": 7.46,
}

#: Карточка акции: колонок сбора у неё нет вовсе (проверено живым запросом
#: 05.09.2026 по `SBER` — ни `BUYSELLFEE`, ни `SCALPERFEE`).
SHARE_COLUMNS = ("SECID", "BOARDID", "SHORTNAME", "MINSTEP", "LOTSIZE")
SBER_ROW: dict[str, object] = {
    "SECID": "SBER", "BOARDID": "TQBR", "SHORTNAME": "Сбербанк", "MINSTEP": 0.01,
    "LOTSIZE": 1,
}


def card(row: dict[str, object], *, columns: tuple[str, ...] = SECURITY_COLUMNS,
         secid: str = "MXU6", market=FUTURES):
    """Карточка инструмента, разобранная рабочим кодом, а не собранная руками."""
    return parse_security(security_body(row, columns=columns), secid=secid, market=market)


def test_the_card_gives_both_fees_of_the_index_future() -> None:
    """Оба сбора читаются из той же строки, в которой лежит стоимость пункта.

    Ради этого сбор и не стоит отдельного запроса: `BUYSELLFEE` и `SCALPERFEE`
    приходят вместе с `MINSTEP` и `STEPPRICE` — одна карточка, два ответа.
    """
    fee = read_commission(card(MXU6_ROW), secid="MXU6")

    assert (fee.exchange_rub, fee.scalper_rub) == (14.68, 7.34)
    assert fee.confirmed


def test_the_fee_of_the_same_contract_is_different_the_next_day() -> None:
    """Сбор плавает — значит, записать его в настройки один раз нельзя.

    Он не число, а доля стоимости контракта (0,0066 % у `MXU6`), и растёт
    вместе с ценой. Две настоящие карточки одного контракта, снятые в сутках
    друг от друга, дают разные сборы: 14,68 ₽ и 14,92 ₽.
    """
    yesterday = read_commission(card(MXU6_ROW), secid="MXU6")
    today = read_commission(card(NEXT_DAY), secid="MXU6")

    assert yesterday.exchange_rub != today.exchange_rub, (
        "сбор объявлен постоянным — тогда и хранить его можно было бы, "
        "и вся затея с чтением у биржи не нужна"
    )
    assert "2026-09-04" in yesterday.told and "2026-09-05" in today.told, (
        f"в строке нет отметки биржи, и вчерашнее число неотличимо "
        f"от сегодняшнего:\n{yesterday.told}\n{today.told}"
    )


def test_the_broker_tariff_is_added_in_one_place() -> None:
    """Сбор биржи и тариф брокера складываются здесь, а не в трёх местах окна.

    Числа настоящие: 14,92 ₽ биржа + 1,2 ₽ тариф «Трейдер» = 16,12 ₽
    за контракт на сторону. Умолчание проекта — 14 ₽, то есть по обычному
    сбору оно занижено.
    """
    fee = read_commission(card(NEXT_DAY), secid="MXU6")

    assert fee.per_side(BROKER_RUB) == 16.12
    assert fee.per_side(0.0) == 14.92, "тариф брокера подмешан там, где его не звали"


def test_the_scalper_tariff_is_a_different_number_not_a_half_of_ours() -> None:
    """Скальперский сбор берётся у биржи, а не делится нами пополам.

    У всех шести замеренных контрактов он ровно вдвое меньше обычного, и
    соблазн поделить на два велик. Делить нельзя: «вдвое» — это сегодняшнее
    наблюдение, а не правило биржи, и в тот день, когда оно перестанет
    выполняться, деление станет тихой ошибкой в деньгах.
    """
    fee = read_commission(card(NEXT_DAY), secid="MXU6")

    assert fee.per_side(BROKER_RUB, scalper=True) == 8.66
    assert fee.scalper_rub == 7.46, "скальперский сбор взят не из карточки"


def test_a_missing_scalper_fee_is_not_replaced_by_the_full_one() -> None:
    """Нет скальперского сбора — отказ, а не обычный сбор вместо него.

    Подстановка удвоила бы комиссию молча: скальперский вдвое меньше.
    """
    without = {**NEXT_DAY, "SCALPERFEE": None}
    fee = read_commission(card(without), secid="MXU6")

    assert fee.scalper_rub is None
    assert fee.per_side(BROKER_RUB, scalper=True) is None
    assert fee.per_side(BROKER_RUB) == 16.12, "обычный сбор пропал заодно"


def test_a_share_without_the_fee_column_is_not_given_a_zero() -> None:
    """У акции колонки сбора нет — это сказано словами, а не нулём.

    Ноль в комиссии выглядит посчитанным: отчёт с нулевой комиссией
    неотличим от отчёта, где её просто не назвали.
    """
    fee = read_commission(
        card(SBER_ROW, columns=SHARE_COLUMNS, secid="SBER", market=SHARES),
        secid="SBER",
    )

    assert fee.exchange_rub is None and fee.scalper_rub is None
    assert not fee.confirmed
    assert fee.per_side(BROKER_RUB) is None
    assert "BUYSELLFEE" in fee.told and "настройках" in fee.told, (
        f"причина не названа человеку: {fee.told}"
    )


def test_a_card_that_the_exchange_did_not_give_says_so() -> None:
    """Биржа не ответила — сбор не подтверждён, и об этом сказано вслух.

    Молчание здесь означало бы, что владелец счёта видит деньги отчёта
    и не знает, на каком сборе они посчитаны.
    """
    fee = read_commission(None, secid="RIU6")

    assert not fee.confirmed
    assert fee.per_side(BROKER_RUB) is None
    assert "RIU6" in fee.told and "не подтверждена" in fee.told


def test_a_fee_that_is_not_a_positive_number_is_refused() -> None:
    """Ноль и минус в карточке — отказ, а не бесплатная торговля.

    Бесплатных сделок на срочном рынке не бывает, и ноль в этой колонке
    означает «биржа промолчала», а не «сбора нет».
    """
    zero = read_commission(card({**NEXT_DAY, "BUYSELLFEE": 0.0}), secid="MXU6")

    assert not zero.confirmed
    assert zero.per_side(BROKER_RUB) is None
    assert "не положительное число" in zero.told


def test_a_negative_broker_tariff_does_not_lower_the_commission() -> None:
    """Отрицательный тариф брокера не уменьшает комиссию, а отменяет ответ.

    Настройка приходит из окна, и опечатка со знаком дала бы комиссию
    меньше биржевого сбора — то есть отчёт, в котором торговля дешевле,
    чем она есть.
    """
    fee = read_commission(card(NEXT_DAY), secid="MXU6")

    assert fee.per_side(-1.2) is None
