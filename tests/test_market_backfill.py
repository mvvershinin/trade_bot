"""Политика догрузки: где дыра, что просить, что из ответа писать.

Сети и базы здесь нет ни в каком виде — только списки минут и списки свечей.
Проводка (запрос, запись, журнал) проверяется отдельно, в
`tests/test_app_backfill.py`.

Числа взяты из наблюдения владельца счёта 04.09.2026: дыра 13:08–14:03,
и минуты 14:03…14:45 записаны потоком **после** неё. Именно на такой форме
ряда ломается наивное «последняя минутка + 1»: последняя минутка есть,
а дыра остаётся навсегда.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from market import MSK, Candle
from market.backfill import (
    MATCH_BAND,
    MAX_SPAN,
    RATIO_SAMPLE,
    holes,
    pick_new,
    plan,
    suspicious,
    volume_ratio,
)

MINUTE = timedelta(minutes=1)
DAY = datetime(2026, 9, 4, 13, 0, tzinfo=MSK)  # пятница


def series(*spans: tuple[datetime, int]) -> list[datetime]:
    """Минуты нескольких отрезков одним списком: `(начало, сколько)`."""
    out: list[datetime] = []
    for start, count in spans:
        out.extend(start + MINUTE * step for step in range(count))
    return out


def bar(moment: datetime, *, volume: float = 1.0, close: float = 100.0) -> Candle:
    return Candle(
        time=moment, open=close, high=close, low=close, close=close, volume=volume
    )


# -- где дыра ---------------------------------------------------------------


def test_the_plan_starts_at_the_first_missing_minute_not_at_the_last_present_one() -> None:
    """Дыра в **середине** ряда находится. Хвостовая догрузка её не видит.

    Это и есть `B-006` в чистом виде: 13:00…13:07 записаны, час потерян,
    14:03…14:10 записаны потоком после переподключения. «Последняя минутка
    плюс одна» дала бы 14:11 — то есть «догружать нечего», и пустой час
    остался бы пустым навсегда, ровно как у владельца счёта.
    """
    boundary = DAY + timedelta(minutes=71)  # 14:11
    made = plan(series((DAY, 8), (DAY + timedelta(minutes=63), 8)), boundary=boundary)

    assert made.since == DAY + timedelta(minutes=8), (
        "план начался не с первой отсутствующей минуты — дыра в середине ряда "
        f"осталась бы незакрытой; получено {made.since}"
    )
    assert made.until == boundary
    assert made.missing == 55, f"55 минут пропуска, посчитано {made.missing}"


def test_the_tail_of_the_series_is_a_hole_too() -> None:
    """Хвост — частный случай дыры, а не отдельный механизм.

    Программу закрыли в 13:07, открыли в 14:11: между ними ряда нет вовсе,
    и `find_gaps` такого пропуска не видит по построению — он ищет промежутки
    **между** имеющимися точками.
    """
    boundary = DAY + timedelta(minutes=71)
    made = plan(series((DAY, 8)), boundary=boundary)

    assert made.since == DAY + timedelta(minutes=8)
    assert made.missing == 63, f"хвост в 63 минуты не посчитан: {made.missing}"


def test_two_holes_are_covered_from_the_first_one_not_from_the_last() -> None:
    """Дыр несколько — запрос начинается с **первой**, а не с последней.

    Найдено мутацией 05.09.2026: `found[0]` заменили на `found[-1]`, и весь
    прогон остался зелёным. Причина в том, что все прежние проверки строились
    на ряде **с одной дырой**, где первая и последняя — одно и то же; главное
    утверждение `B-006` («ищется дыра, а не хвост») на них поэтому
    не проверялось вовсе.

    Цена ошибки — не абстрактная: обрыв за обрывом за одну сессию даёт
    ровно такой ряд, и с «последней дырой» ранние пропуски не закрылись бы
    никогда. Отдельно проверяется `missing`: он обязан считать **обе** дыры,
    иначе отчёт скажет, что пропущено меньше, чем на самом деле.
    """
    boundary = DAY + timedelta(minutes=60)
    made = plan(
        series(
            (DAY, 5),                            # 13:00…13:04
            (DAY + timedelta(minutes=15), 5),    # 13:15…13:19 — до неё дыра в 10
            (DAY + timedelta(minutes=40), 20),   # 13:40…13:59 — до неё дыра в 20
        ),
        boundary=boundary,
    )

    assert made.since == DAY + timedelta(minutes=5), (
        "запрос начался не с первой дыры: ранний пропуск не закрылся бы "
        f"никогда, а поверх него лёг бы отчёт об успешной догрузке; {made.since}"
    )
    assert len(made.gaps) == 2, f"найдены не обе дыры: {made.gaps}"
    assert made.missing == 30, f"посчитана одна дыра вместо двух: {made.missing}"


def test_a_continuous_series_asks_for_nothing_and_says_nothing() -> None:
    """Дыр нет — запроса нет и отказа нет: молчание, а не строка в журнал."""
    made = plan(series((DAY, 60)), boundary=DAY + timedelta(minutes=60))

    assert made.since is None, "план собрался просить брокера при полном ряде"
    assert made.refusal == "", (
        "непрерывный ряд объявлен отказом — владелец счёта получал бы строку "
        f"журнала на каждом подключении: {made.refusal!r}"
    )


# -- ночь, выходные и слишком старая база -----------------------------------


def test_an_empty_window_is_a_refusal_with_words_not_a_four_day_request() -> None:
    """Своих данных в окне нет — это не дыра обрыва, а отсутствие истории.

    Ровно этот случай отделяет «мы потеряли час» от «программу месяц
    не запускали». Второе догрузкой у брокера не лечится: потолок 1440 баров
    на запрос и ограничение частоты. Молчаливая попытка «догрузить месяц»
    упёрлась бы в предел запросов уже в торговое время.
    """
    boundary = DAY + MAX_SPAN + timedelta(days=1)
    made = plan(series((DAY, 60)), boundary=boundary)

    assert made.since is None, (
        "догрузка полезла к брокеру за отсутствующей историей: "
        f"собралась просить с {made.since}"
    )
    assert "не запускалась" not in made.refusal
    assert made.refusal, "отказ без причины — владелец счёта не узнает, что делать"
    assert "с биржи" in made.refusal, (
        f"в отказе не сказано, чем это лечится: {made.refusal!r}"
    )


def test_minutes_older_than_the_window_do_not_stretch_the_request() -> None:
    """Ряд старше окна в план не входит — иначе запрос вырос бы на месяц.

    Ночь и выходные **внутри** окна запрашиваются вместе со всем отрезком:
    пустой ответ на них и есть доказательство «торгов не было». А вот
    прошлогодняя минутка окна не растягивает.
    """
    boundary = DAY + timedelta(minutes=60)
    old = DAY - timedelta(days=30)
    made = plan(series((old, 10), (DAY, 8)), boundary=boundary)

    assert made.since == DAY + timedelta(minutes=8), (
        f"план захватил данные вне окна: просит с {made.since}"
    )
    assert made.missing == 52


def test_the_holes_of_a_night_are_marked_as_crossing_the_date() -> None:
    """Ночной пропуск помечен переходом через сутки — журнал им не забивается.

    Пометка не отменяет запроса: отрезок всё равно запрашивается целиком.
    Она нужна отчёту, где ночь и выходные топили бы те несколько пропусков,
    ради которых журнал и ведётся.
    """
    evening = datetime(2026, 9, 3, 23, 45, tzinfo=MSK)
    morning = datetime(2026, 9, 4, 10, 0, tzinfo=MSK)
    found = holes(series((evening, 5), (morning, 5)), boundary=morning + MINUTE * 5)

    assert len(found) == 1
    assert found[0].crosses_date, "ночной пропуск не помечен переходом через сутки"


# -- что из ответа писать ---------------------------------------------------


def test_the_minute_the_stream_leads_is_never_written() -> None:
    """Минута границы и всё правее — не наше дело, и в базу не попадает.

    Эту минуту ведёт живой поток: объём в ней посчитан по приращениям оборота
    и сверен с биржей до контракта. Ответ HTTP, записанный поверх, обрезал бы
    её `close` — то есть сдвинул бы среднюю и дал бы переворот там, где его нет.
    Признака «свеча закрыта» у брокера нет ни в HTTP, ни в потоке, отличить
    растущую минуту по содержимому нельзя.
    """
    edge = DAY + timedelta(minutes=10)
    picked = pick_new(
        [bar(edge - MINUTE), bar(edge), bar(edge + MINUTE)], present=[], boundary=edge
    )

    assert [candle.time for candle in picked.fresh] == [edge - MINUTE], (
        "в запись попала минута, которую ведёт поток: "
        f"{[candle.time for candle in picked.fresh]}"
    )
    assert picked.beyond == 2, f"молча отброшено вместо счёта: beyond={picked.beyond}"


def test_a_minute_that_is_already_in_the_base_is_not_rewritten() -> None:
    """Догрузка закрывает дыры и не переписывает существующее.

    У дыры нет правды, которую можно потерять, у записанной минуты — есть.
    Отсюда же безопасность повтора: второй заход не находит пустых минут.
    """
    edge = DAY + timedelta(minutes=5)
    picked = pick_new(
        [bar(DAY), bar(DAY + MINUTE), bar(DAY + MINUTE * 2)],
        present=[DAY, DAY + MINUTE * 2],
        boundary=edge,
    )

    assert [candle.time for candle in picked.fresh] == [DAY + MINUTE]
    assert picked.had == 2, f"уже имевшиеся минуты не посчитаны: had={picked.had}"


def test_seconds_in_the_answer_do_not_make_a_second_row_for_one_minute() -> None:
    """`10:05:07` и `10:05:00` — одна минутка, и она уже есть в базе.

    Без приведения к началу минуты это два разных ключа: в базе две строки
    на одну минуту, а в собранном баре её объём складывается вдвое.
    """
    edge = DAY + timedelta(minutes=5)
    picked = pick_new(
        [bar(DAY.replace(second=7)), bar(DAY + MINUTE)], present=[DAY], boundary=edge
    )

    assert [candle.time for candle in picked.fresh] == [DAY + MINUTE]
    assert picked.had == 1


def test_a_minute_repeated_inside_the_answer_lands_once_and_the_last_one_wins() -> None:
    """Повтор внутри ответа схлопывается — то же правило, что у хранилища."""
    edge = DAY + timedelta(minutes=5)
    picked = pick_new(
        [bar(DAY, close=100.0), bar(DAY, close=101.0)], present=[], boundary=edge
    )

    assert len(picked.fresh) == 1
    assert picked.fresh[0].close == 101.0, "победил не последний снимок минуты"
    assert picked.had == 0, "дубль ответа посчитан как «уже было в базе»"


def test_a_broker_minute_in_utc_becomes_the_same_instant_in_moscow() -> None:
    """Момент с чужим поясом приводится к МСК, а не обрезается.

    Брокер шлёт UTC, база живёт в МСК. Снятый пояс сдвинул бы свечи на три
    часа молча — торговое окно 10:05–11:00 московское, и список сделок
    стал бы другим, ничего при этом не уронив.
    """
    utc_minute = datetime(2026, 9, 4, 10, 5, tzinfo=timezone.utc)
    picked = pick_new([bar(utc_minute)], present=[], boundary=DAY + timedelta(hours=4))

    written = picked.fresh[0].time
    assert written == utc_minute, "момент изменился, а не сменил представление"
    assert written.utcoffset() == timedelta(hours=3), f"пояс не МСК: {written}"
    assert (written.hour, written.minute) == (13, 5), f"часы не переведены: {written}"


# -- единицы объёма ---------------------------------------------------------


def test_the_volume_ratio_is_silent_when_there_is_almost_nothing_to_compare() -> None:
    """Нахлёста мало — не говорится **ничего**.

    Молчание честнее числа, посчитанного по двум точкам: одна минута
    с крупной сделкой перекашивает отношение на порядок.
    """
    ours = {DAY + MINUTE * i: 10.0 for i in range(RATIO_SAMPLE - 1)}
    arrived = [bar(moment, volume=10.0) for moment in ours]

    assert volume_ratio(arrived, ours) is None


def test_the_volume_ratio_checks_the_conversion_instead_of_guessing_the_units() -> None:
    """Замер проверяет пересчёт: сошёлся ли он с тем, что мы посчитали сами.

    До 05.09.2026 замер выяснял **единицу**, и полоса была широкой — она
    ловила разрыв на пять порядков. Теперь оборот пересчитывается в контракты
    до записи (`market.volume`), и вопрос другой: сошлось ли. Отношение
    обязано быть около единицы.
    """
    ours = {DAY + MINUTE * i: 10.0 for i in range(RATIO_SAMPLE + 5)}
    same = [bar(moment, volume=10.0) for moment in ours]
    turnover = [bar(moment, volume=2_248_500.0) for moment in ours]

    plain = volume_ratio(same, ours)
    other = volume_ratio(turnover, ours)

    assert plain is not None and plain[0] == pytest.approx(1.0)
    assert not suspicious(plain[0]), "сошедшийся пересчёт объявлен подозрительным"
    assert other is not None and other[0] == pytest.approx(224_850.0)
    assert suspicious(other[0]), (
        "разрыв в пять порядков не назван подозрительным — владелец счёта "
        "увидел бы оборот в рублях в колонке контрактов и не узнал бы об этом"
    )


def test_the_narrow_band_catches_the_smallest_mistake_it_is_there_for() -> None:
    """Полоса ловит множитель «рублей за пункт», а не только разрыв в единицах.

    Самая мелкая ошибка, ради которой полоса стоит, — контракт на РТС:
    рубль за пункт там 1,73, и пересчёт, поделивший только на цену, ошибётся
    ровно во столько же (`market.volume`, шапка). Прежняя полоса (1/20 … 20)
    такое пропускала молча — вот её и заменили.
    """
    assert MATCH_BAND == (0.9, 1.1), (
        f"полоса разъехалась с разбором: {MATCH_BAND}"
    )
    assert suspicious(1.7317), "множитель РТС не пойман — полоса снова широка"
    assert suspicious(1 / 1.7317), "тот же множитель в другую сторону не пойман"
    assert suspicious(865.857), "множитель Брента не пойман"
    assert not suspicious(1.0), "точное совпадение объявлено подозрительным"
    assert not suspicious(1.05) and not suspicious(0.95), (
        "шум округления на медиане объявлен расхождением: живой поток считает "
        "контракты по приращениям, и пара минут расходится там всегда"
    )


def test_a_minute_without_our_volume_does_not_enter_the_measurement() -> None:
    """Деления на ноль в замере не бывает, и такая минута просто не считается."""
    ours = {DAY + MINUTE * i: (0.0 if i == 0 else 10.0) for i in range(RATIO_SAMPLE + 1)}
    arrived = [bar(moment, volume=10.0) for moment in ours]

    measured = volume_ratio(arrived, ours)

    assert measured is not None
    assert measured[1] == RATIO_SAMPLE, f"нулевая минута попала в замер: {measured}"
