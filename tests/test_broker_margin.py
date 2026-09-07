"""ГО под контракт до первого входа за день (`D-046`).

Замкнутый круг: брокер сообщает ГО контракта только через открытую позицию,
а открыть позицию без ГО предохранитель не даёт. Разрывается биржевым
`INITIALMARGIN` с надбавкой (`broker/margin.py`).

Ни один тест не ходит в сеть: модуль принимает числа и отдаёт число.

Что стережёт каждый тест — первой строкой его докстринга.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from broker.margin import (
    BROKER_MARKUP,
    BROKER_MEMORY,
    EXCHANGE_MEMORY,
    MarginBook,
    MarginSource,
)

NOW = datetime(2026, 9, 5, 7, 30, 0, tzinfo=timezone.utc)

#: Замер 05.09.2026: биржа объявила столько по `MXU6`.
EXCHANGE = 23_124.62
#: Столько требовал брокер в окне владельца счёта в тот же день.
BROKER = 38_526.0


def test_the_circle_is_broken_margin_is_known_before_the_first_entry() -> None:
    """Позиции нет ни разу, а ГО известно: круг разорван биржевым числом.

    Это и есть `D-046`. До 05.09.2026 ответ здесь был `None`, предохранитель
    писал «НЕ ПРОВЕРЕНО», и так каждый первый вход каждого дня.
    """
    book = MarginBook()
    book.exchange_said(EXCHANGE, NOW)
    answer = book.best(NOW, open_position=None)
    assert answer is not None, "ГО до первого входа снова неизвестно"
    assert answer.source is MarginSource.EXCHANGE_ESTIMATE
    assert answer.rubles == EXCHANGE * BROKER_MARKUP


def test_nothing_known_is_still_nothing_not_zero() -> None:
    """Ни биржи, ни брокера — `None`, а не ноль: ноль разрешал бы любой объём."""
    assert MarginBook().best(NOW) is None


def test_the_estimate_is_above_the_exchange_number_not_equal_to_it() -> None:
    """Оценка выше биржевой: по биржевому брокер откажет в заявке.

    Замер 05.09.2026: биржа 23 124,62 ₽, брокер 38 526 ₽. Взять биржевое
    как есть — значит посчитать на 66 % больше контрактов, чем позволит брокер.
    """
    book = MarginBook()
    book.exchange_said(EXCHANGE, NOW)
    answer = book.best(NOW)
    assert answer is not None
    assert answer.rubles > BROKER, (
        "оценка ниже того, что брокер требовал на замере: заявку отклонят"
    )


def test_the_estimate_says_it_is_an_estimate() -> None:
    """Оценка помечена оценкой и словами, и признаком: движку уходит просто число."""
    book = MarginBook()
    book.exchange_said(EXCHANGE, NOW)
    answer = book.best(NOW)
    assert answer is not None
    assert answer.estimated is True
    assert "ОЦЕНКА" in answer.told, answer.told
    assert "занижен" in answer.told, "не сказано, чем оценка грозит объёму"


def test_an_open_position_beats_the_estimate() -> None:
    """Открытая позиция сильнее оценки: это ответ брокера здесь и сейчас."""
    book = MarginBook()
    book.exchange_said(EXCHANGE, NOW)
    answer = book.best(NOW, open_position=BROKER)
    assert answer is not None
    assert answer.source is MarginSource.BROKER_POSITION
    assert answer.rubles == BROKER
    assert answer.estimated is False


def test_the_broker_number_outlives_the_position_that_gave_it() -> None:
    """Позиция закрыта — брокерское число ещё живо и сильнее оценки.

    Внутри клирингового отрезка обеспечение не меняется, а брокерское число
    точнее оценки на порядок: это ответ брокера, а не наша прикидка.
    """
    book = MarginBook()
    book.exchange_said(EXCHANGE, NOW)
    book.best(NOW, open_position=BROKER)
    later = book.best(NOW + timedelta(minutes=20), open_position=None)
    assert later is not None
    assert later.source is MarginSource.BROKER_RECENT
    assert later.rubles == BROKER


def test_a_stale_broker_number_gives_way_to_the_estimate() -> None:
    """Брокерское число старше `BROKER_MEMORY` — не «примерно верно», а неизвестно."""
    book = MarginBook()
    book.exchange_said(EXCHANGE, NOW)
    book.best(NOW, open_position=BROKER)
    later = NOW + BROKER_MEMORY + timedelta(seconds=1)
    book.exchange_said(EXCHANGE, later)
    answer = book.best(later)
    assert answer is not None
    assert answer.source is MarginSource.EXCHANGE_ESTIMATE


def test_a_stale_exchange_number_is_not_used_at_all() -> None:
    """Биржевое число старше `EXCHANGE_MEMORY` не годится: ГО меняется на клиринге."""
    book = MarginBook()
    book.exchange_said(EXCHANGE, NOW)
    assert book.best(NOW + EXCHANGE_MEMORY + timedelta(seconds=1)) is None


def test_a_number_from_the_future_is_refused() -> None:
    """Отрицательный возраст — часы разошлись; число неизвестного возраста не берём."""
    book = MarginBook()
    book.exchange_said(EXCHANGE, NOW)
    assert book.best(NOW - timedelta(minutes=5)) is None


def test_no_position_does_not_erase_what_the_broker_said() -> None:
    """`None` значит «позиции нет», а не «ГО нулевое»: память ради этого и есть."""
    book = MarginBook()
    book.broker_said(BROKER, NOW)
    book.broker_said(None, NOW + timedelta(minutes=1))
    answer = book.best(NOW + timedelta(minutes=1))
    assert answer is not None and answer.rubles == BROKER


def test_zero_and_negative_margin_are_refused_from_both_sources() -> None:
    """Ноль ГО — «обеспечения не требуется», то есть любой объём. Не берём."""
    book = MarginBook()
    book.exchange_said(0.0, NOW)
    book.exchange_said(-1.0, NOW)
    book.broker_said(0.0, NOW)
    book.broker_said(-1.0, NOW)
    assert book.best(NOW) is None
    assert book.best(NOW, open_position=0.0) is None


def test_forget_wipes_the_book_when_the_instrument_changes() -> None:
    """Смена тикера стирает обе памяти: у `MXU6` и `GZU6` ГО разное в 15 раз."""
    book = MarginBook()
    book.exchange_said(EXCHANGE, NOW)
    book.broker_said(BROKER, NOW)
    book.forget()
    assert book.best(NOW) is None


def test_the_real_markup_is_measurable_once_both_numbers_are_known() -> None:
    """Настоящая надбавка брокера считается, как только была позиция.

    Ради неё константа `BROKER_MARKUP` и помечена «подлежит замене»:
    сегодня она взята из одного чужого наблюдения.
    """
    book = MarginBook()
    assert book.markup_seen() is None
    book.exchange_said(EXCHANGE, NOW)
    assert book.markup_seen() is None, "одного биржевого числа мало"
    book.broker_said(BROKER, NOW)
    seen = book.markup_seen()
    assert seen is not None
    assert round(seen, 3) == 1.666, seen


def test_the_exchange_age_is_answered_for_the_caller_to_decide() -> None:
    """Возраст биржевого числа виден снаружи: когда идти на биржу — решает сборка."""
    book = MarginBook()
    assert book.exchange_age(NOW) is None
    book.exchange_said(EXCHANGE, NOW)
    assert book.exchange_age(NOW + timedelta(minutes=7)) == timedelta(minutes=7)


def test_the_told_line_names_the_number_in_rubles_for_a_human() -> None:
    """Строка для журнала — с разрядами и запятой, а не «46249.24».

    Пробел разрядов неразрывный (`\u00a0`), как и в остальных денежных
    строках слоя: иначе число разъезжается по двум строкам окна.
    """
    book = MarginBook()
    book.exchange_said(EXCHANGE, NOW)
    answer = book.best(NOW)
    assert answer is not None
    assert "46\u00a0249,24 \u20bd" in answer.told, answer.told
    assert "23\u00a0124,62 \u20bd" in answer.told, "биржевое число не показано"
    assert "46249.24" not in answer.told, "число показано машинной записью"
