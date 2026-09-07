"""Сверка часов машины с часами брокера (`D-045`).

Ни один тест не ходит в сеть. Серверное время подставляется заголовком `Date`,
как его прислал бы брокер.

Что стережёт каждый тест — первой строкой его докстринга.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from broker.clock import (
    SKEW_BLOCKING,
    SKEW_NOTICE,
    ClockWatch,
    Verdict,
    reading_of,
    server_moment,
)

NOW = datetime(2026, 9, 5, 7, 30, 0, tzinfo=timezone.utc)


def header_at(moment: datetime) -> str:
    """Момент → заголовок `Date` в том виде, в каком его шлёт сервер."""
    return moment.astimezone(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")


# --- разбор заголовка ---


def test_the_date_header_is_read_as_a_moment_with_a_zone() -> None:
    """Заголовок `Date` разбирается в момент с поясом."""
    assert server_moment(header_at(NOW)) == NOW


@pytest.mark.parametrize(
    "header",
    [None, "", "вчера", "Mon, 32 Foo 2026 99:99:99 GMT", "2026-09-05T07:30:00Z"],
)
def test_an_unreadable_date_header_means_unknown_not_zero(header: str | None) -> None:
    """Заголовка нет или он мусор — ответ «не знаем», а не нулевой скос."""
    assert server_moment(header) is None


def test_a_naive_server_moment_is_refused_rather_than_given_a_zone() -> None:
    """Момент без пояса отвергается: дорисованный пояс — выдумка на два часа.

    `parsedate_to_datetime` для формы `-0000` возвращает наивный момент.
    Вычитание наивного из момента с поясом даёт `TypeError` посреди дня.
    """
    assert server_moment("Sat, 05 Sep 2026 07:30:00 -0000") is None


# --- один замер ---


def test_the_skew_is_measured_from_the_middle_of_the_round_trip() -> None:
    """Скос считается от середины круга, а не от края: край врёт на весь круг."""
    sent = NOW
    received = NOW + timedelta(seconds=4)
    # Сервер пометил ответ ровно серединой — значит часы сходятся.
    measured = reading_of(
        header_at(NOW + timedelta(seconds=2)), sent_at=sent, received_at=received
    )
    assert measured is not None
    assert measured.skew == timedelta(0)
    assert measured.round_trip == timedelta(seconds=4)


def test_the_sign_says_which_way_the_clock_is_off() -> None:
    """Плюс — наши часы отстают, минус — спешат. Знак назван один раз."""
    slow = reading_of(
        header_at(NOW + timedelta(seconds=30)), sent_at=NOW, received_at=NOW
    )
    fast = reading_of(
        header_at(NOW - timedelta(seconds=30)), sent_at=NOW, received_at=NOW
    )
    assert slow is not None and fast is not None
    assert slow.skew == timedelta(seconds=30), "сервер дальше — наши отстают"
    assert fast.skew == timedelta(seconds=-30), "сервер позади — наши спешат"


def test_the_uncertainty_grows_with_the_round_trip() -> None:
    """Погрешность — половина круга плюс секунда огрубления заголовка."""
    quick = reading_of(header_at(NOW), sent_at=NOW, received_at=NOW)
    slow = reading_of(
        header_at(NOW), sent_at=NOW, received_at=NOW + timedelta(seconds=20)
    )
    assert quick is not None and slow is not None
    assert quick.uncertainty == timedelta(seconds=1)
    assert slow.uncertainty == timedelta(seconds=11)


def test_a_clock_that_ran_backwards_gives_no_reading() -> None:
    """Круг отрицательной длины — замер отбрасывается, а не считается серединой.

    Так бывает, когда часы перевела служба синхронизации прямо между
    отправкой и ответом: середина промежутка в таком замере не значит ничего.
    """
    assert (
        reading_of(
            header_at(NOW), sent_at=NOW, received_at=NOW - timedelta(seconds=5)
        )
        is None
    )


# --- вердикт ---


def test_a_matching_clock_is_matched_not_silence() -> None:
    """Расхождение меньше порога — вердикт «совпадают», а не «не сверяли»."""
    watch = ClockWatch()
    watch.observed(
        header_at(NOW + timedelta(seconds=2)), sent_at=NOW, received_at=NOW
    )
    assert watch.verdict() is Verdict.MATCHED


def test_no_reading_at_all_is_unknown_not_matched() -> None:
    """Ни одного замера — «не сверяли». Молчание не выдаётся за подтверждение."""
    watch = ClockWatch()
    assert watch.verdict() is Verdict.UNKNOWN
    watch.observed(None, sent_at=NOW, received_at=NOW)
    assert watch.verdict() is Verdict.UNKNOWN
    assert watch.complaint() is None


def test_a_skew_past_the_notice_threshold_is_seen_and_said() -> None:
    """Расхождение с порога `SKEW_NOTICE` замечено и сказано словами."""
    watch = ClockWatch()
    watch.observed(
        header_at(NOW + SKEW_NOTICE), sent_at=NOW, received_at=NOW
    )
    assert watch.verdict() is Verdict.SKEWED
    told = watch.complaint()
    assert told is not None
    headline, text = told
    assert "отстают" in headline, headline
    assert "10,0 с" in text or "10 с" in text, text


def test_a_skew_past_the_blocking_threshold_says_trading_stops() -> None:
    """С порога `SKEW_BLOCKING` сказано, что торговля уже останавливается."""
    watch = ClockWatch()
    watch.observed(header_at(NOW + SKEW_BLOCKING), sent_at=NOW, received_at=NOW)
    assert watch.verdict() is Verdict.BROKEN
    told = watch.complaint()
    assert told is not None
    assert "уже останавливается" in told[1], told[1]


def test_both_directions_name_their_own_damage() -> None:
    """Отстающие и спешащие часы ломают разное, и в тексте это разное."""
    behind, ahead = ClockWatch(), ClockWatch()
    behind.observed(header_at(NOW + timedelta(seconds=30)), sent_at=NOW, received_at=NOW)
    ahead.observed(header_at(NOW - timedelta(seconds=30)), sent_at=NOW, received_at=NOW)
    slow = behind.complaint()
    fast = ahead.complaint()
    assert slow is not None and fast is not None
    assert "устаревшим" in slow[1], slow[1]
    assert "раньше срока" in fast[1], fast[1]


def test_the_complaint_never_offers_to_fix_the_clock() -> None:
    """Программа не подкручивает время и говорит об этом прямо."""
    watch = ClockWatch()
    watch.observed(header_at(NOW + timedelta(seconds=90)), sent_at=NOW, received_at=NOW)
    told = watch.complaint()
    assert told is not None
    assert "не подкручивает" in told[1], told[1]
    assert "синхронизац" in told[1], "не сказано, что делать человеку"


def test_a_measurement_coarser_than_the_threshold_decides_nothing() -> None:
    """Круг грубее порога не даёт ни «врут», ни «совпадают» — только «не сверяли».

    Найдено собственным тестом: первая редакция сторожила одну сторону
    и на медленном канале объявляла бы исправность часов, которой не проверяла.
    """
    watch = ClockWatch()
    watch.observed(
        header_at(NOW + timedelta(seconds=12)),
        sent_at=NOW,
        received_at=NOW + timedelta(seconds=40),
    )
    assert watch.verdict() is Verdict.UNKNOWN
    assert watch.complaint() is None
    assert watch.matched_again() is None, "исправность объявлена без проверки"


def test_a_huge_skew_is_seen_even_through_a_slow_link() -> None:
    """Пять минут разницы при погрешности в двадцать секунд — факт, а не шум."""
    watch = ClockWatch()
    watch.observed(
        header_at(NOW + timedelta(minutes=5)),
        sent_at=NOW,
        received_at=NOW + timedelta(seconds=40),
    )
    assert watch.verdict() is Verdict.BROKEN


# --- повторы ---


def test_the_same_complaint_is_not_repeated_line_after_line() -> None:
    """Одна и та же жалоба вторично не выдаётся: иначе строка в минуту весь день."""
    watch = ClockWatch()
    for _ in range(5):
        watch.observed(
            header_at(NOW + timedelta(seconds=30)), sent_at=NOW, received_at=NOW
        )
    assert watch.complaint() is not None
    assert watch.complaint() is None, "вторая жалоба на то же расхождение"


def test_a_worsening_skew_is_said_again() -> None:
    """Смена вердикта говорится заново: «встанет» — не то же, что «запас съеден»."""
    watch = ClockWatch()
    watch.observed(header_at(NOW + timedelta(seconds=30)), sent_at=NOW, received_at=NOW)
    assert watch.complaint() is not None
    watch.observed(header_at(NOW + timedelta(seconds=300)), sent_at=NOW, received_at=NOW)
    told = watch.complaint()
    assert told is not None
    assert "5 мин" in told[1], told[1]


def test_the_clock_coming_back_is_announced_once() -> None:
    """Часы снова сошлись — сказано один раз, и это отдельная новость."""
    watch = ClockWatch()
    watch.observed(header_at(NOW + timedelta(seconds=30)), sent_at=NOW, received_at=NOW)
    assert watch.complaint() is not None
    watch.observed(header_at(NOW), sent_at=NOW, received_at=NOW)
    back = watch.matched_again()
    assert back is not None and "снова сходятся" in back, back
    assert watch.matched_again() is None, "второе объявление о том же"


def test_a_clock_that_never_drifted_says_nothing_at_all() -> None:
    """Исправные часы в журнал не пишут: записи «всё как обычно» ему не нужны."""
    watch = ClockWatch()
    watch.observed(header_at(NOW), sent_at=NOW, received_at=NOW)
    assert watch.complaint() is None
    assert watch.matched_again() is None


def test_an_answer_without_a_date_does_not_erase_what_was_found() -> None:
    """Ответ без заголовка времени — отсутствие сведений, а не опровержение."""
    watch = ClockWatch()
    watch.observed(header_at(NOW + timedelta(seconds=90)), sent_at=NOW, received_at=NOW)
    watch.observed(None, sent_at=NOW, received_at=NOW)
    assert watch.verdict() is Verdict.BROKEN


def test_describe_is_never_empty_and_says_when_nothing_was_measured() -> None:
    """Строка для панели непустая всегда и различает «сходятся» и «не сверяли»."""
    watch = ClockWatch()
    assert "не сверялись" in watch.describe()
    watch.observed(header_at(NOW + timedelta(seconds=90)), sent_at=NOW, received_at=NOW)
    assert "отстают на 1 мин 30 с" in watch.describe(), watch.describe()
    assert "±" in watch.describe(), "погрешность замера не показана"
