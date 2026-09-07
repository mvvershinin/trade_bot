"""Стоимость пункта: подбор рынка по тикеру и ответ словами вместо исключения.

Сети здесь нет ни в одном тесте: транспорт подставной, тела ответов заранее
заготовлены (`market_helpers`). Что этот код делает с настоящей биржей,
замерено отдельно и записано числами в `market/point.py` — воспроизводить
замер прогоном тестов нельзя, иначе прогон падает от чужого сбоя и зеленеет
от чужой удачи.

Каждый тест стережёт **одну** вещь, и она названа первой строкой докстринга.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import pytest
from market_helpers import MXU6_ROW, FakeTransport, no_sleep, security_body

from market import (
    FUTURES,
    SHARES,
    CandleStore,
    IssClient,
    IssTransportError,
    Market,
    MarketWorker,
    PointValue,
    ask_point_value,
)

#: Карточка акции, как её отдаёт ISS: `STEPPRICE` нет вовсе, лот — `LOTSIZE`.
#: Проверено живым запросом 05.09.2026: у `SBER` колонки `STEPPRICE` нет.
SHARE_COLUMNS = ("SECID", "BOARDID", "SHORTNAME", "MINSTEP", "LOTSIZE", "PREVPRICE")
SBER_ROW: dict[str, object] = {
    "SECID": "SBER", "BOARDID": "TQBR", "SHORTNAME": "Сбербанк",
    "MINSTEP": 0.01, "LOTSIZE": 1, "PREVPRICE": 280.68,
}

#: Пустая таблица — ровно так ISS отвечает на незнакомый тикер: код 200,
#: `data` без строк.
NOTHING: bytes = security_body()


def client(handler: Callable[[str], bytes]) -> tuple[IssClient, FakeTransport]:
    """Клиент на подставном транспорте и сам транспорт — чтобы считать запросы."""
    transport = FakeTransport(handler)
    return IssClient(transport, pause=0, sleep=no_sleep), transport


def by_market(**bodies: bytes) -> Callable[[str], bytes]:
    """Ответ, выбранный по рынку в адресе: `forts=…`, `shares=…`."""
    def handler(url: str) -> bytes:
        for name, body in bodies.items():
            if f"/markets/{name}/" in url or f"/engines/{name}/" in url:
                return body
        return NOTHING
    return handler


# ------------------------------------------------------------------ число

@pytest.mark.parametrize(
    ("step", "step_price", "expected"),
    [(25.0, 25.0, 1.0), (10.0, 17.37744, 1.737744), (0.01, 8.68872, 868.872)],
)
def test_the_answer_carries_the_step_price_over_the_step(
    step: float, step_price: float, expected: float
) -> None:
    """Стережёт: число ответа — именно стоимость шага, делённая на шаг.

    Замер 05.09.2026 живым запросом: `MXU6` — 1,00, `RIU6` — 1,73774,
    `BRV6` — 868,872. Единица верна для одного контракта из шести, и ошибка
    при ней тихая: сделки те же, деньги другие.
    """
    row = {**MXU6_ROW, "MINSTEP": step, "STEPPRICE": step_price}
    loader, _ = client(lambda _url: security_body(row))
    answer = ask_point_value(loader, "X")
    assert answer.rubles == pytest.approx(expected)
    assert answer.confirmed


def test_the_index_future_still_costs_exactly_one_ruble_a_point() -> None:
    """Стережёт сверку с прототипом: у `MXU6` пункт стоит ровно рубль.

    127 сделок из 127 посчитаны при единице. Подстановка с биржи обязана
    давать ту же единицу — иначе поедут все деньги эталонного прогона,
    а список сделок останется прежним и разницы не покажет.
    """
    loader, _ = client(lambda _url: security_body(MXU6_ROW))
    assert ask_point_value(loader, "MXU6").rubles == 1.0


# ----------------------------------------------------------------- рынок

def test_the_futures_market_is_asked_first_and_answers_in_one_request() -> None:
    """Стережёт: за фьючерс не платится ни одного лишнего запроса."""
    loader, transport = client(by_market(forts=security_body(MXU6_ROW)))
    assert ask_point_value(loader, "MXU6").rubles == 1.0
    assert len(transport.urls) == 1, (
        f"карточка фьючерса стоила {len(transport.urls)} запросов вместо одного"
    )
    assert "/markets/forts/" in transport.urls[0]


def test_a_share_is_found_on_the_second_market_and_not_on_the_first() -> None:
    """Стережёт: не найденный на срочном тикер ищется дальше, а не отвергается.

    Кода класса рядом с тикером в настройках окна нет, и различить фьючерс
    и акцию по строке нельзя. Отказ на первом рынке означал бы, что акцию
    программа не узнает никогда.
    """
    loader, transport = client(
        by_market(shares=security_body(SBER_ROW, columns=SHARE_COLUMNS))
    )
    answer = ask_point_value(loader, "SBER")
    assert [url.split("/markets/")[1].split("/")[0] for url in transport.urls] == [
        "forts", "shares"
    ]
    assert answer.spec is not None and answer.spec.board == "TQBR"


def test_a_found_instrument_stops_the_search_even_without_a_step_price() -> None:
    """Стережёт: найденная карточка не гонит искать «получше» на другом рынке.

    У акции `STEPPRICE` нет вовсе. Пойти за ним на срочный рынок значило бы
    взять чужой контракт с похожим именем — та же беда, от которой карточка
    выбирается по режиму торгов (`B-016`).
    """
    loader, transport = client(
        by_market(
            shares=security_body(SBER_ROW, columns=SHARE_COLUMNS),
            forts=NOTHING,
        )
    )
    answer = ask_point_value(loader, "SBER")
    assert answer.rubles is None
    assert len(transport.urls) == 2, "поиск пошёл дальше найденной карточки"


def test_asking_nowhere_is_answered_in_words_and_not_by_an_exception() -> None:
    """Стережёт: пустой список рынков — ответ словами, а не падение."""
    loader, transport = client(lambda _url: NOTHING)
    answer = ask_point_value(loader, "MXU6", markets=())
    assert answer.rubles is None
    assert not transport.urls
    assert "не назван ни один рынок" in answer.told


def test_the_order_of_the_markets_is_the_one_that_was_asked_for() -> None:
    """Стережёт: рынки перебираются в том порядке, в каком их назвали."""
    loader, transport = client(lambda _url: NOTHING)
    ask_point_value(loader, "MXU6", markets=(SHARES, FUTURES))
    assert [url.split("/markets/")[1].split("/")[0] for url in transport.urls] == [
        "shares", "forts"
    ]


# ------------------------------------------------------- ответ без числа

def test_an_unknown_ticker_is_named_with_every_market_it_was_looked_for_on() -> None:
    """Стережёт: тикера нет нигде — сказано, где искали и что делать.

    У фьючерса тикер меняется с каждой экспирацией, и опечатка здесь штатна.
    """
    loader, _ = client(lambda _url: NOTHING)
    answer = ask_point_value(loader, "MXZ9")
    assert answer.rubles is None
    assert "MXZ9" in answer.told
    for market in (FUTURES, SHARES):
        assert market.title in answer.told
    assert "экспирацией" in answer.told


def test_a_card_without_a_step_price_does_not_become_a_confirmed_one() -> None:
    """Стережёт: нет `STEPPRICE` — не подставляется единица, а называется причина.

    Единица подставленная и единица подтверждённая выглядят одинаково,
    а стоят разного: по `BRV6` разница в 869 раз.
    """
    loader, _ = client(lambda _url: security_body(SBER_ROW, columns=SHARE_COLUMNS))
    answer = ask_point_value(loader, "SBER")
    assert answer.rubles is None
    assert not answer.confirmed
    assert "STEPPRICE" in answer.told


@pytest.mark.parametrize("step_price", [0.0, -1.0])
def test_a_step_price_that_is_not_positive_is_refused(step_price: float) -> None:
    """Стережёт: ноль и минус в стоимость пункта не проходят.

    Движок такое значение отвергает исключением (`EngineSettings`), и порт
    показал бы владельцу счёта отказ настроек вместо внятной причины.
    """
    row = {**MXU6_ROW, "STEPPRICE": step_price}
    loader, _ = client(lambda _url: security_body(row))
    answer = ask_point_value(loader, "MXU6")
    assert answer.rubles is None
    assert "не положительное" in answer.told


def test_a_silent_exchange_is_said_in_words_and_not_raised() -> None:
    """Стережёт: биржа промолчала — ответ словами, вызывающий не ловит исключений.

    Молчаливая единица хуже отказа: владелец счёта увидит в отчёте красивое
    число и не узнает, что оно не про его инструмент.
    """
    def handler(_url: str) -> bytes:
        raise IssTransportError("сеть недоступна")

    loader, _ = client(handler)
    answer = ask_point_value(loader, "MXU6")
    assert answer.rubles is None
    assert "не ответила" in answer.told
    assert "не подтверждена" in answer.told


def test_broken_json_is_said_in_words_too() -> None:
    """Стережёт: испорченный ответ — такой же ответ словами, а не трассировка."""
    loader, _ = client(lambda _url: b"{ not a json")
    assert ask_point_value(loader, "MXU6").rubles is None


# ---------------------------------------------------- строка происхождения

def test_the_source_line_names_the_card_and_both_numbers_it_is_built_from() -> None:
    """Стережёт: по строке видно, из чего получено число, а не только само число.

    Строка показывается в окне под полем «Рублей в пункте цены». Величина
    плавает, и «подсказано биржей» без чисел не даёт проверить ничего.
    """
    loader, _ = client(lambda _url: security_body(MXU6_ROW))
    told = ask_point_value(loader, "MXU6").told
    for part in ("MXU6", "RFUD", "25", "2026-09-04 07:00:01"):
        assert part in told, f"в строке происхождения нет «{part}»: {told}"


def test_the_source_line_does_not_change_between_two_identical_answers() -> None:
    """Стережёт: строка не дрожит на каждом запросе — иначе прогон гоняется зря.

    Порт применяет ответ при изменении числа **или** строки. Момент нашего
    запроса в строке означал бы пересчёт всей истории раз в десять минут
    без единой причины.
    """
    loader, _ = client(lambda _url: security_body(MXU6_ROW))
    first = ask_point_value(loader, "MXU6")
    second = ask_point_value(loader, "MXU6")
    assert first.told == second.told


def test_a_card_without_a_stamp_still_names_its_numbers() -> None:
    """Стережёт: нет `IMTIME` — строка короче, но не пустая и не сломанная."""
    row = {**MXU6_ROW, "IMTIME": None}
    loader, _ = client(lambda _url: security_body(row))
    told = ask_point_value(loader, "MXU6").told
    assert "MXU6" in told and "помечена" not in told


def test_the_numbers_in_the_source_line_are_written_the_way_the_window_writes_them(
) -> None:
    """Стережёт: пять знаков и запятая — как в поле окна, а не `8.68872`.

    У фьючерса на РТС величина 1,73774; два знака округлили бы её до 1,74,
    а это 0,1 % на каждой сделке.
    """
    row = {**MXU6_ROW, "MINSTEP": 0.01, "STEPPRICE": 8.68872}
    loader, _ = client(lambda _url: security_body(row))
    told = ask_point_value(loader, "MXU6").told
    assert "0,01" in told and "8,68872" in told
    assert "8.68872" not in told


# ------------------------------------------------------------ поток данных

def test_the_data_thread_answers_in_words_and_writes_nothing(tmp_path) -> None:
    """Стережёт: карточка идёт через поток данных и **не** попадает в базу.

    Записанная величина стала бы вторым источником правды о том, что плавает:
    у РТС и Брента стоимость шага пересчитывается на клиринге.
    """
    path = tmp_path / "candles.sqlite3"
    loader, _ = client(lambda _url: security_body(MXU6_ROW))

    async def scenario() -> PointValue:
        async with MarketWorker(path, iss=loader) as worker:
            return await worker.point_value("MXU6")

    answer = asyncio.run(scenario())
    assert answer.rubles == 1.0
    with CandleStore(path) as store:
        assert store.symbols() == [], "карточка инструмента попала в базу свечей"


def test_a_closed_data_thread_says_so_instead_of_going_to_the_exchange(
    tmp_path,
) -> None:
    """Стережёт: после закрытия потока данных запрос не уходит, а падает вслух."""
    loader, transport = client(lambda _url: security_body(MXU6_ROW))

    async def scenario() -> None:
        worker = MarketWorker(tmp_path / "candles.sqlite3", iss=loader)
        await worker.open()
        await worker.close()
        with pytest.raises(RuntimeError, match="остановлен"):
            await worker.point_value("MXU6")

    asyncio.run(scenario())
    assert not transport.urls


def test_the_market_list_is_a_pair_of_the_two_markets_the_program_knows() -> None:
    """Стережёт: перебираются ровно те рынки, что заведены в слое, и в порядке.

    Появится третий рынок — эта строка упадёт, и решение «искать ли на нём»
    будет принято словами, а не молчанием.
    """
    from market import MARKETS, MARKETS_PROBED

    assert MARKETS_PROBED == (FUTURES, SHARES)
    assert set(MARKETS_PROBED) == set(MARKETS.values())
    assert all(isinstance(market, Market) for market in MARKETS_PROBED)


def test_the_body_of_an_empty_answer_is_really_empty() -> None:
    """Канарейка: заготовка «биржа не знает такого» действительно пуста.

    Без неё половина тестов выше проверяла бы поведение на теле, которое
    случайно перестало быть пустым, — и зеленела бы, ничего не проверяя.
    """
    assert json.loads(NOTHING)["securities"]["data"] == []
