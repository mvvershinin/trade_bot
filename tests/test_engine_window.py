"""Границы торгового окна: ошибка на один знак здесь даёт другой список сделок.

Проверяется дословное правило PROTOTYPE.md §4::

    now = час*60 + минута  (от времени ЗАКРЫТИЯ свечи)
    start == end  → торгуем весь день
    start <  end  → now > start && now < end
    start >  end  → now > start || now < end

Все три случая границ, обе границы каждого, свеча ровно на границе, секунды,
часовой пояс и выходные. Ни одна из этих ошибок не падает: она просто меняет
сделки при тех же настройках.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest

from engine import (
    MSK,
    DayMarks,
    DayRule,
    TradingWindow,
    day_verdict,
    in_moscow,
    is_trading_day,
    minutes_of_day,
)

UTC = timezone.utc
DAY = 19  # 19.06.2026 — пятница


def at(hour: int, minute: int, second: int = 0, *, day: int = DAY) -> datetime:
    return datetime(2026, 6, day, hour, minute, second, tzinfo=MSK)


# --------------------------------------------------------------------------
# start < end — обычное дневное окно
# --------------------------------------------------------------------------

def test_the_candle_closing_exactly_at_the_start_is_outside() -> None:
    """Свеча, закрывшаяся ровно в 10:05, в окно 10:05–11:00 НЕ попадает.

    Первая рабочая пятиминутка — закрывшаяся в 10:10, потому что 10:05
    это граница, а неравенство строгое. Это тот самый знак, из-за которого
    расходятся списки сделок (ARCHITECTURE.md §6).
    """
    window = TradingWindow(time(10, 5), time(11, 0))
    assert window.contains(at(10, 5)) is False
    assert window.contains(at(10, 10)) is True


def test_the_candle_closing_exactly_at_the_end_is_outside() -> None:
    """Правая граница тоже строгая: закрытие ровно в 11:00 уже вне окна."""
    window = TradingWindow(time(10, 5), time(11, 0))
    assert window.contains(at(10, 55)) is True
    assert window.contains(at(11, 0)) is False
    assert window.contains(at(11, 5)) is False


def test_a_minute_on_either_side_of_the_boundary() -> None:
    """Минута до границы — снаружи, минута после — внутри. И симметрично."""
    window = TradingWindow(time(10, 5), time(11, 0))
    assert window.contains(at(10, 4)) is False
    assert window.contains(at(10, 6)) is True
    assert window.contains(at(10, 59)) is True
    assert window.contains(at(11, 1)) is False


def test_seconds_are_dropped_from_the_comparison() -> None:
    """Секунды в счёт не идут: 10:05:30 — это по-прежнему граница, а не «после».

    Прототип собирает `now` из часа и минуты. Сравнение объектов времени
    целиком пустило бы 10:05:30 внутрь окна 10:05–11:00 — и дало бы сделку,
    которой у прототипа нет.
    """
    window = TradingWindow(time(10, 5), time(11, 0))
    assert window.contains(at(10, 5, 30)) is False
    assert window.contains(at(11, 0, 59)) is False
    assert minutes_of_day(at(10, 5, 59)) == 10 * 60 + 5


# --------------------------------------------------------------------------
# start == end — окно не задано
# --------------------------------------------------------------------------

@pytest.mark.parametrize("hour,minute", [(0, 0), (3, 30), (10, 5), (23, 55)])
def test_equal_boundaries_mean_the_whole_day(hour: int, minute: int) -> None:
    """`start == end` — окно не задано, торгуем весь день, включая границу."""
    window = TradingWindow(time(0, 0), time(0, 0))
    assert window.whole_day is True
    assert window.contains(at(hour, minute)) is True


def test_equal_boundaries_at_a_non_midnight_time_also_mean_the_whole_day() -> None:
    """Совпадение границ означает «весь день» при любом значении, не только 00:00."""
    window = TradingWindow(time(14, 0), time(14, 0))
    assert window.whole_day is True
    assert window.contains(at(14, 0)) is True
    assert window.contains(at(2, 0)) is True


def test_equal_boundaries_are_compared_by_minutes_not_by_seconds() -> None:
    """Границы, различающиеся только секундами, — это «весь день».

    Секунды не читаются ни у свечи, ни у границ. 10:05:00 и 10:05:30 дают
    одно и то же `now`, значит окно считается незаданным. Сравнение объектов
    времени целиком сочло бы это окном длиной в полсуток без тридцати секунд.
    """
    window = TradingWindow(time(10, 5), time(10, 5, 30))
    assert window.whole_day is True
    assert window.contains(at(3, 0)) is True


# --------------------------------------------------------------------------
# start > end — окно через полночь
# --------------------------------------------------------------------------

def test_window_over_midnight_covers_both_sides_of_the_night() -> None:
    """`start > end` — окно переходит через полночь: `now > start || now < end`."""
    window = TradingWindow(time(23, 0), time(2, 0))
    assert window.over_midnight is True
    assert window.contains(at(23, 30)) is True
    assert window.contains(at(0, 30)) is True
    assert window.contains(at(1, 59)) is True
    assert window.contains(at(12, 0)) is False


def test_window_over_midnight_keeps_both_boundaries_strict() -> None:
    """И у ночного окна обе границы строгие: 23:00 и 02:00 — снаружи."""
    window = TradingWindow(time(23, 0), time(2, 0))
    assert window.contains(at(23, 0)) is False
    assert window.contains(at(2, 0)) is False
    assert window.contains(at(23, 1)) is True
    assert window.contains(at(1, 59)) is True


# --------------------------------------------------------------------------
# Часовой пояс
# --------------------------------------------------------------------------

def test_the_window_is_counted_in_moscow_time_whatever_the_zone() -> None:
    """Момент в UTC приводится к МСК, а не читается как есть.

    07:10 UTC — это 10:10 МСК, то есть внутри окна 10:05–11:00. Чтение
    часа «как есть» выбросило бы свечу из окна и убрало сделку.
    """
    window = TradingWindow(time(10, 5), time(11, 0))
    moment = datetime(2026, 6, DAY, 7, 10, tzinfo=UTC)
    assert window.contains(moment) is True
    assert in_moscow(moment).hour == 10


def test_naive_time_is_refused_loudly() -> None:
    """Время без пояса — отказ, а не догадка «наверное, это уже МСК».

    Догадка сдвинула бы окно на разницу поясов: сделки остались бы,
    просто стали бы другими.
    """
    window = TradingWindow(time(10, 5), time(11, 0))
    with pytest.raises(ValueError, match="без часового пояса"):
        window.contains(datetime(2026, 6, DAY, 10, 10))


def test_window_boundaries_may_not_carry_a_timezone() -> None:
    """Граница окна — час внутри дня, а не момент времени."""
    with pytest.raises(ValueError, match="часовым поясом"):
        TradingWindow(time(10, 5, tzinfo=MSK), time(11, 0))


def test_window_boundaries_must_be_times() -> None:
    with pytest.raises(TypeError, match="datetime.time"):
        TradingWindow("10:05", time(11, 0))  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Выходные
# --------------------------------------------------------------------------

def test_weekends_are_excluded_unless_switched_on() -> None:
    """Суббота и воскресенье выключены, пока торговля в выходные не включена.

    20.06.2026 — суббота, 21.06 — воскресенье, 22.06 — понедельник.
    """
    saturday = at(10, 10, day=20)
    sunday = at(10, 10, day=21)
    monday = at(10, 10, day=22)
    assert is_trading_day(saturday, trade_in_weekend=False) is False
    assert is_trading_day(sunday, trade_in_weekend=False) is False
    assert is_trading_day(monday, trade_in_weekend=False) is True
    assert is_trading_day(saturday, trade_in_weekend=True) is True
    assert is_trading_day(sunday, trade_in_weekend=True) is True


def test_the_trading_day_is_taken_from_the_close_time_in_moscow() -> None:
    """День недели берётся от времени ЗАКРЫТИЯ свечи и в МСК.

    Свеча, закрывшаяся в 00:30 МСК субботы, — это 21:30 UTC пятницы.
    Считать день по UTC значило бы разрешить торговлю в выходной.
    """
    moment = datetime(2026, 6, 19, 21, 30, tzinfo=UTC)  # 20.06 00:30 МСК, суббота
    assert in_moscow(moment).weekday() == 5
    assert is_trading_day(moment, trade_in_weekend=False) is False


def test_the_window_label_reads_like_a_journal_line() -> None:
    assert TradingWindow(time(10, 5), time(11, 0)).label == "10:05–11:00"
    assert TradingWindow(time(0, 0), time(0, 0)).label == "весь день"


def test_default_window_is_the_one_from_the_specification() -> None:
    """Умолчание — 10:05–11:00 из ТЗ, а не 09:30–11:30 из прототипа.

    Цифры различаются, и при сверке окно задаётся явно с обеих сторон
    (PROTOTYPE.md §1). Тест ловит подмену умолчания задним числом.
    """
    assert TradingWindow() == TradingWindow(time(10, 5), time(11, 0))


def test_moscow_offset_is_three_hours_without_a_timezone_database() -> None:
    """МСК задана фиксированным смещением: `zoneinfo` в сборке может не быть."""
    assert MSK.utcoffset(None) == timedelta(hours=3)


# --------------------------------------------------------------------------
# Календарь нерабочих дней (F-002, решение 0038)
#
# Каждая проверка ниже стережёт одно поведение, и оно названо первой строкой.
# Даты: 19.06.2026 — пятница, 20.06 — суббота, 22.06 — понедельник.
# --------------------------------------------------------------------------

def test_by_default_the_calendar_repeats_the_prototype() -> None:
    """Без единой отметки правило дня — прототип, и ничего кроме.

    На этом умолчании стоит сверка 127 сделок из 127. Проверка на **умолчании
    самого довода**, а не на явно переданном пустом наборе: подмена умолчания
    непустым набором иначе прошла бы молча.
    """
    for day, expected in ((19, True), (20, False), (21, False), (22, True)):
        assert is_trading_day(at(10, 10, day=day), trade_in_weekend=False) is expected


def test_a_marked_day_gives_no_trading() -> None:
    """День, помеченный владельцем счёта, перестаёт быть торговым."""
    marks = DayMarks.of({date(2026, 6, 19): False})
    assert is_trading_day(at(10, 10, day=19), trade_in_weekend=False) is True
    assert is_trading_day(
        at(10, 10, day=19), trade_in_weekend=False, calendar=marks
    ) is False


def test_a_working_saturday_is_traded_when_marked() -> None:
    """Рабочая суббота: отметка «торгуем» сильнее календарного выходного.

    Первая половина просьбы владельца счёта: «суббота рабочая». Сегодня без
    отметки робот в субботу не выйдет — и это неверно при переносе.
    """
    marks = DayMarks.of({date(2026, 6, 20): True})
    assert is_trading_day(
        at(10, 10, day=20), trade_in_weekend=False, calendar=marks
    ) is True


def test_a_marked_monday_is_not_traded() -> None:
    """Нерабочий понедельник: вторая половина просьбы владельца счёта.

    Сегодня без отметки робот в праздничный понедельник выйдет торговать.
    """
    marks = DayMarks.of({date(2026, 6, 22): False, date(2026, 6, 23): False})
    for day in (22, 23):
        assert is_trading_day(
            at(10, 10, day=day), trade_in_weekend=False, calendar=marks
        ) is False


def test_the_exchange_beats_the_owner_mark_and_says_so() -> None:
    """Биржа закрыта — отметка «торгуем» не работает, и это сказано словами.

    Решение 0038: побеждает биржа. Молчаливое подчинение читалось бы как
    поломка программы — владелец счёта не понял бы, почему его отметка
    ничего не дала.
    """
    verdict = day_verdict(
        at(10, 10, day=19),
        trade_in_weekend=False,
        calendar=DayMarks.of({date(2026, 6, 19): True}),
        exchange=DayMarks.of({date(2026, 6, 19): False}),
    )
    assert verdict.trading is False
    assert verdict.rule is DayRule.EXCHANGE_CLOSED
    assert "биржа" in verdict.why.lower()
    assert "пометили день торговым" in verdict.why


def test_the_owner_may_refuse_a_day_the_exchange_trades() -> None:
    """Обратный случай: биржа работает, а владелец счёта — нет. Его воля сильнее."""
    verdict = day_verdict(
        at(10, 10, day=19),
        trade_in_weekend=False,
        calendar=DayMarks.of({date(2026, 6, 19): False}),
        exchange=DayMarks.of({date(2026, 6, 19): True}),
    )
    assert verdict.trading is False
    assert verdict.rule is DayRule.OWNER_OFF
    assert "ваше решение сильнее" in verdict.why


def test_a_working_saturday_named_by_the_exchange_is_traded() -> None:
    """Перенос выходного, объявленный биржей, отменяет календарный выходной."""
    verdict = day_verdict(
        at(10, 10, day=20),
        trade_in_weekend=False,
        exchange=DayMarks.of({date(2026, 6, 20): True}),
    )
    assert verdict.trading is True
    assert verdict.rule is DayRule.EXCHANGE_OPEN
    assert "перенос выходного" in verdict.why


def test_an_unknown_day_is_not_a_closed_day() -> None:
    """Пустое расписание — это «не знаем», а не «биржа закрыта».

    `broker/schedule.py`, решение 2: программа не встаёт из-за незнания
    расписания. Пустой набор дней биржи обязан вести себя как его отсутствие.
    """
    assert is_trading_day(
        at(10, 10, day=19), trade_in_weekend=False, exchange=DayMarks()
    ) is True


def test_the_order_of_the_day_rules_is_the_one_from_the_document() -> None:
    """Порядок правил — данные, и он сверяется с документом, а не с памятью.

    Перестановка двух правил не падает и не видна в сделках сразу: она меняет
    ответ только там, где два правила спорят, — то есть ровно в том случае,
    ради которого принято решение 0038.
    """
    from engine.window import _DAY_RULES

    assert [rule for rule, _, _ in _DAY_RULES] == [
        DayRule.EXCHANGE_CLOSED,
        DayRule.OWNER_OFF,
        DayRule.OWNER_ON,
        DayRule.EXCHANGE_OPEN,
        DayRule.WEEKEND,
        DayRule.ORDINARY,
    ]
    assert [trading for _, trading, _ in _DAY_RULES] == [
        False, False, True, True, False, True
    ]


def test_a_moment_of_time_is_not_a_day() -> None:
    """`datetime` в наборе отметок — громкий отказ, а не молчаливая непопадашка.

    `datetime` — подкласс `date`, и момент времени не совпал бы ни с одной
    датой: отметка выглядела бы поставленной и не работала.
    """
    with pytest.raises(TypeError, match="datetime.date"):
        DayMarks.of({datetime(2026, 6, 19, 10, 5, tzinfo=MSK): False})


def test_the_day_of_a_candle_is_taken_in_moscow() -> None:
    """Дата берётся от закрытия свечи в МСК, а не в поясе машины.

    Свеча, закрывшаяся 20.06 в 00:30 МСК, в UTC приходится на 19.06. Отметка
    на 20 июня обязана сработать именно на ней.
    """
    moment = datetime(2026, 6, 19, 21, 30, tzinfo=UTC)  # 20.06 00:30 МСК
    verdict = day_verdict(
        moment,
        trade_in_weekend=True,
        calendar=DayMarks.of({date(2026, 6, 20): False}),
    )
    assert verdict.day == date(2026, 6, 20)
    assert verdict.trading is False


def test_two_different_answers_about_one_day_are_refused() -> None:
    """Один день, помеченный дважды и по-разному, — отказ, а не выбор наугад."""
    with pytest.raises(ValueError, match="дважды"):
        DayMarks(((date(2026, 6, 19), True), (date(2026, 6, 19), False)))


def test_the_same_days_in_a_different_order_are_the_same_marks() -> None:
    """Порядок отметок не меняет набор: иначе журнал писал бы небывшее.

    `changes_from` сравнивает наборы на равенство. Два одинаковых набора,
    собранных в разном порядке, дали бы строку «Календарь изменён» на пустом
    месте — и владелец счёта искал бы, что же поменялось.
    """
    first = DayMarks.of({date(2026, 6, 19): False, date(2026, 6, 22): True})
    second = DayMarks.of({date(2026, 6, 22): True, date(2026, 6, 19): False})
    assert first == second
    assert hash(first) == hash(second)


def test_the_label_of_the_marks_names_the_days_and_counts_the_rest() -> None:
    """Подпись для журнала: дни словами, длинный список — с числом."""
    assert DayMarks().label == "нет"
    assert DayMarks.of({date(2026, 6, 19): False}).label == "19.06.2026 не торгуем"
    many = DayMarks.of({date(2026, 6, day): False for day in range(1, 8)})
    assert "и ещё 3 (всего 7)" in many.label
