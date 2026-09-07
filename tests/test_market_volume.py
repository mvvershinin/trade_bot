"""Единица объёма: в базе контракты, у брокера оборот, между ними — пересчёт.

Числа здесь **настоящие**, а не круглые: цена 226 325 и оборот 64 264 575 —
это минута 04.09.2026 20:44 из базы владельца счёта, единственная пара
«оборот брокера рядом с ценой», которая в ней есть. Круглые числа проверяли
бы арифметику, а вопрос был не в ней: колонка `volume` хранила рубли под
именем контрактов, и обнаружилось это отношением 2,199 × 10⁵ на 3858 общих
минутах, а не делением 700 000 на 100 000.
"""

from __future__ import annotations

import pytest

from market.volume import CONTRACT_HALF, bar_price, contracts_from_turnover

#: Минута 04.09.2026 20:44 по базе владельца счёта: цены и оборот из ответа
#: брокера. Середина разброса — 226 262,5, и оборот даёт по ней 284,03
#: контракта; по закрытию вышло бы 283,93. Округление в обоих случаях
#: даёт 284 — на этой минуте выбор цены не виден, и потому он проверяется
#: отдельно, на баре с несимметричным разбросом.
REAL_HIGH = 226_350.0
REAL_LOW = 226_175.0
REAL_CLOSE = 226_325.0
REAL_TURNOVER = 64_264_575.0


def test_the_turnover_of_a_real_minute_becomes_contracts() -> None:
    """Настоящая минута брокера даёт число контрактов, а не рублей."""
    price = bar_price(REAL_HIGH, REAL_LOW)

    assert contracts_from_turnover(REAL_TURNOVER, price=price) == 284.0


def test_the_price_of_a_bar_is_the_middle_of_its_range_not_the_close() -> None:
    """Делим на середину разброса, а не на закрытие, и это выбор по замеру.

    Замер 05.09.2026 на 1000 настоящих минут `SBER` и `GAZP` — там ISS отдаёт
    и оборот, и число бумаг, то есть истина известна: середина разброса
    ошибается на 0,004…0,009 %, закрытие — на 0,009…0,019 %, вдвое с лишним
    больше, и втрое больше по худшему бару.

    Проверка на **несимметричном** баре: на симметричном середина совпадает
    с закрытием, и подмена одного другим прошла бы незамеченной.
    """
    assert bar_price(110.0, 90.0) == 100.0
    assert bar_price(120.0, 90.0) == 105.0, "середина разброса посчитана не так"
    # Бар закрылся на минимуме: середина выше закрытия на 5 %.
    assert contracts_from_turnover(10_500.0, price=bar_price(110.0, 100.0)) == 100.0
    assert contracts_from_turnover(10_500.0, price=100.0) == 105.0, (
        "проверка вакуумна: по закрытию вышло бы то же число"
    )


def test_a_minute_without_trades_stays_a_zero() -> None:
    """Нулевой оборот — это правда «сделок не было», а не отказ.

    Отказать здесь значило бы выбросить минуту, которая у брокера законно
    пустая, и завести дыру на ровном месте.
    """
    assert contracts_from_turnover(0.0, price=REAL_CLOSE) == 0.0


def test_contracts_instead_of_a_turnover_are_refused_instead_of_rounded_to_zero() -> None:
    """Пришли контракты вместо оборота — отказ, а не ноль.

    Сторож против обратной ошибки. Если брокер однажды начнёт слать контракты,
    деление на цену даст 0,0009, округление — ноль, и в базе появится минута
    «сделок не было» там, где сделок было 284. Ноль в этой колонке выглядит
    посчитанным, и отличить его от настоящего нечем.
    """
    assert contracts_from_turnover(284.0, price=REAL_CLOSE) is None


def test_a_price_that_cannot_be_divided_by_is_refused() -> None:
    """Ноль и минус в цене — отказ, а не исключение и не подставленная единица.

    Цена приходит из ответа брокера, а не из нашей арифметики: разбор
    не обязан ронять всю догрузку из-за одной свечи, но и делить на ноль
    ему нечем.
    """
    assert contracts_from_turnover(REAL_TURNOVER, price=0.0) is None
    assert contracts_from_turnover(REAL_TURNOVER, price=-226_325.0) is None


def test_a_negative_turnover_is_refused() -> None:
    """Отрицательного оборота не бывает — значит, пришло не то."""
    assert contracts_from_turnover(-REAL_TURNOVER, price=REAL_CLOSE) is None


def test_the_border_of_the_refusal_is_half_a_contract() -> None:
    """Граница отказа названа и проверена с обеих сторон.

    Ровно половина ещё считается: один контракт даёт оборот в одну цену,
    и чтобы округление увело его ниже половины, цена внутри минуты должна
    была вырасти вдвое. Такое — уже не «мало торговали».
    """
    price = 100.0
    assert contracts_from_turnover(price * CONTRACT_HALF, price=price) == 0.0
    assert contracts_from_turnover(price * CONTRACT_HALF * 0.999, price=price) is None


def test_the_answer_is_a_whole_number_of_contracts() -> None:
    """Контракт неделим: дробное число в колонке объёма выглядело бы измеренным.

    Средняя цена минуты нам неизвестна — брокер шлёт только оборот, — поэтому
    283,93 округляется до 284. Замер на 81 914 настоящих минутах `MXU6`:
    в 81,7 % минут округление даёт заведомо точное число, потому что граница
    ошибки там меньше половины контракта.
    """
    counted = contracts_from_turnover(REAL_TURNOVER, price=REAL_CLOSE)

    assert counted is not None
    assert counted == pytest.approx(round(counted)), f"дробные контракты: {counted}"
    assert counted != pytest.approx(REAL_TURNOVER / REAL_CLOSE), (
        "число не округлено: 283,93 контракта — это не количество, "
        "а результат деления"
    )
