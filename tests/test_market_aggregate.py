"""Сборка таймфрейма из минуток — то, что обязано быть покрыто тестами
(QUALITY.md §5), а не «проверено глазами».

Проверяется ровно то, на чём эта сборка ломается: границы бара, пропущенная
минутка внутри интервала, разрыв на границе дня, дубль, неполный последний
интервал, пустой ответ сервера.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from market.aggregate import bar_start, build_bars
from market.candles import M5, MINUTE, Candle, Timeframe

from market_helpers import ALLOWED_TIMEFRAMES, minute, msk


def test_bar_10_05_holds_minutes_05_to_09_and_not_10() -> None:
    """Главное правило: пятиминутка 10:05 включает 10:05…10:09.

    Минутка 10:10 открывает следующую свечу. Смещение на одну минуту здесь
    сдвигает время закрытия бара, а по нему движок считает торговое окно.
    """
    minutes = [minute(msk(2026, 8, 26, 10, m), open=float(m)) for m in range(5, 11)]
    bars = build_bars(minutes, M5)

    assert [b.time.strftime("%H:%M") for b in bars] == ["10:05", "10:10"]
    assert bars[0].filled_minutes == 5
    assert bars[0].close_time == msk(2026, 8, 26, 10, 10)
    assert bars[1].filled_minutes == 1


def test_ohlcv_assembly() -> None:
    """open первой минутки, close последней, экстремумы и сумма объёма."""
    minutes = [
        minute(msk(2026, 8, 26, 10, 5), open=100, high=103, low=99, close=101, volume=10),
        minute(msk(2026, 8, 26, 10, 6), open=101, high=107, low=100, close=105, volume=20),
        minute(msk(2026, 8, 26, 10, 9), open=105, high=106, low=95, close=96, volume=5),
    ]
    (bar,) = build_bars(minutes, M5)

    assert (bar.open, bar.high, bar.low, bar.close, bar.volume) == (100, 107, 95, 96, 35)


def test_missing_minute_inside_bar_is_built_and_marked() -> None:
    """Бар собирается из того, что есть, но факт неполноты фиксируется.

    Биржа не отдаёт минуту, в которую не было сделок, — выбрасывать такой бар
    нельзя, иначе на неликвидном инструменте от ряда ничего не останется.
    """
    minutes = [
        minute(msk(2026, 8, 26, 10, 5), open=100),
        minute(msk(2026, 8, 26, 10, 9), open=104),
    ]
    (bar,) = build_bars(minutes, M5)

    assert bar.filled_minutes == 2
    assert bar.is_partial
    assert not bar.unsettled


def test_gap_at_day_boundary_does_not_merge_days() -> None:
    """Вечер одного дня и утро следующего — разные бары, а не один общий."""
    minutes = [
        minute(msk(2026, 8, 26, 23, 47), open=100),
        minute(msk(2026, 8, 26, 23, 49), open=101),
        minute(msk(2026, 8, 27, 7, 0), open=110),
        minute(msk(2026, 8, 27, 7, 1), open=111),
    ]
    bars = build_bars(minutes, M5)

    assert [b.time.isoformat() for b in bars] == [
        "2026-08-26T23:45:00+03:00",
        "2026-08-27T07:00:00+03:00",
    ]
    assert bars[0].close == 101
    assert bars[1].open == 110


def test_duplicate_minute_is_refused_by_default() -> None:
    """Дубль — отказ, а не молчаливое сложение объёмов.

    Сложенный вдвое объём выглядит правдоподобно и не находится потом ничем.
    """
    one = minute(msk(2026, 8, 26, 10, 5), open=100, volume=7)
    with pytest.raises(ValueError, match="дважды"):
        build_bars([one, one], M5)


def test_duplicate_minute_is_caught_even_when_it_comes_with_seconds() -> None:
    """`10:05:00` и `10:05:07` — одна минутка, а не две.

    Биржа метит минутку ровно, поток брокера может прислать её с секундами.
    Сравнение «как пришло» пропустило бы обе, и объём минуты сложился бы
    в баре вдвое: правдоподобное число, которое потом не по чему найти.
    """
    from_exchange = minute(msk(2026, 8, 26, 10, 5), open=100, volume=7)
    from_stream = from_exchange.replace(
        time=msk(2026, 8, 26, 10, 5).replace(second=7), volume=7
    )
    with pytest.raises(ValueError, match="дважды"):
        build_bars([from_exchange, from_stream], M5)


def test_volume_is_not_doubled_when_the_same_minute_comes_with_seconds() -> None:
    """При явном «пропускать дубли» объём остаётся объёмом одной минуты."""
    from_exchange = minute(msk(2026, 8, 26, 10, 5), open=100, volume=7)
    from_stream = from_exchange.replace(time=msk(2026, 8, 26, 10, 5).replace(second=47))
    (bar,) = build_bars([from_exchange, from_stream], M5, on_duplicate="skip")

    assert bar.volume == 7
    assert bar.filled_minutes == 1
    assert bar.time == msk(2026, 8, 26, 10, 5)


def test_a_minute_marked_with_seconds_lands_in_its_own_minute() -> None:
    """Секунды не переносят минутку в соседнюю: 10:09:47 остаётся в баре 10:05."""
    minutes = [
        minute(msk(2026, 8, 26, 10, 5), open=100),
        minute(msk(2026, 8, 26, 10, 9).replace(second=47), open=104),
    ]
    (bar,) = build_bars(minutes, M5)

    assert bar.time == msk(2026, 8, 26, 10, 5)
    assert bar.filled_minutes == 2
    assert bar.close == 104


def test_duplicate_minute_can_be_skipped_explicitly() -> None:
    first_one = minute(msk(2026, 8, 26, 10, 5), open=100, volume=7)
    second_one = minute(msk(2026, 8, 26, 10, 5), open=200, volume=9)
    (bar,) = build_bars([first_one, second_one], M5, on_duplicate="skip")

    assert bar.open == 100
    assert bar.volume == 7


def test_last_bar_is_unsettled_when_period_ends_inside_it() -> None:
    """Незакрытый бар помечается, а по требованию не отдаётся вовсе."""
    minutes = [minute(msk(2026, 8, 26, 10, m), open=float(m)) for m in (5, 6, 10, 11)]
    known_until = msk(2026, 8, 26, 10, 12)

    bars = build_bars(minutes, M5, known_until=known_until)
    assert [(b.time.strftime("%H:%M"), b.unsettled) for b in bars] == [
        ("10:05", False),
        ("10:10", True),
    ]

    settled_only = build_bars(minutes, M5, known_until=known_until, drop_unsettled=True)
    assert [b.time.strftime("%H:%M") for b in settled_only] == ["10:05"]


def test_bar_ending_exactly_at_known_until_is_settled() -> None:
    """Граница строгая: бар, закрывшийся ровно в известный время, — закрытый."""
    minutes = [minute(msk(2026, 8, 26, 10, m)) for m in range(5, 10)]
    bars = build_bars(minutes, M5, known_until=msk(2026, 8, 26, 10, 10))
    assert not bars[0].unsettled


def test_without_known_until_nothing_is_marked_unsettled() -> None:
    """Пометка по догадке так же вредна, как её отсутствие."""
    minutes = [minute(msk(2026, 8, 26, 10, 5))]
    (bar,) = build_bars(minutes, M5)
    assert not bar.unsettled


def test_empty_input_gives_empty_result() -> None:
    """Пустой ответ сервера — пустой список, а не исключение и не «одна свеча»."""
    assert build_bars([], M5) == []
    assert build_bars([], M5, known_until=msk(2026, 8, 26, 10, 0)) == []


def test_input_order_does_not_matter() -> None:
    """Порядок страниц с сервера не должен влиять на open и close бара."""
    minutes = [
        minute(msk(2026, 8, 26, 10, 5), open=100, close=100),
        minute(msk(2026, 8, 26, 10, 6), open=101, close=101),
        minute(msk(2026, 8, 26, 10, 7), open=102, close=102),
    ]
    forward_order = build_bars(minutes, M5)
    reversed_order = build_bars(list(reversed(minutes)), M5)
    assert forward_order == reversed_order
    assert forward_order[0].open == 100
    assert forward_order[0].close == 102


def test_rebuilding_from_rebuilt_bars_is_refused() -> None:
    """Собираем только из минуток.

    Пересборка из уже пересобранных свечей накапливает ошибку округления
    границ, поэтому запрещена не соглашением, а типом входа.
    """
    five_minute_bar = Candle(
        time=msk(2026, 8, 26, 10, 5),
        open=1, high=1, low=1, close=1, volume=1,
        timeframe=M5, filled_minutes=5,
    )
    with pytest.raises(ValueError, match="только из минуток"):
        build_bars([five_minute_bar], Timeframe(15))


def test_hour_bars_align_to_day_start() -> None:
    minutes = [minute(msk(2026, 8, 26, 10, 30)), minute(msk(2026, 8, 26, 11, 30))]
    bars = build_bars(minutes, Timeframe(120))
    assert [b.time.strftime("%H:%M") for b in bars] == ["10:00"]
    assert bars[0].close_time == msk(2026, 8, 26, 12, 0)


@pytest.mark.parametrize(
    ("moment", "minutes", "expected"),
    [
        (msk(2026, 8, 26, 10, 0), 5, "10:00"),
        (msk(2026, 8, 26, 10, 4), 5, "10:00"),
        (msk(2026, 8, 26, 10, 5), 5, "10:05"),
        (msk(2026, 8, 26, 10, 59), 5, "10:55"),
        (msk(2026, 8, 26, 10, 59), 15, "10:45"),
        (msk(2026, 8, 26, 10, 59), 60, "10:00"),
        (msk(2026, 8, 26, 10, 59), 240, "08:00"),
        (msk(2026, 8, 26, 23, 59), 1440, "00:00"),
    ],
)
def test_bar_start_grid(moment: datetime, minutes: int, expected: str) -> None:
    assert bar_start(moment, Timeframe(minutes)).strftime("%H:%M") == expected


@pytest.mark.parametrize("minutes", ALLOWED_TIMEFRAMES)
def test_no_allowed_timeframe_makes_a_bar_cross_midnight(minutes: int) -> None:
    """Ни один допустимый размер свечи не даёт бар поперёк суток.

    На этом стоит расчёт закрытости бара в хранилище: знание о загруженном
    ведётся по дням, и бар, попавший в два дня сразу, сломал бы его молча.
    Проверяется каждая минута суток, а не выборка.

    Список размеров выведен перебором через сам валидатор
    (`market_helpers.ALLOWED_TIMEFRAMES`), а не написан руками: прежде здесь
    стоял список без 4, 6 и 12 минут, а в соседнем файле — без 180, 360 и 480,
    и «проверено на всех размерах» означало «на тех, что вспомнились».
    """
    timeframe = Timeframe(minutes)
    midnight = msk(2026, 8, 26)
    whole_day = [midnight + timedelta(minutes=i) for i in range(24 * 60)]
    crossing_midnight = [
        moment for moment in whole_day
        if (bar_start(moment, timeframe) + timeframe.delta).date() != midnight.date()
        and bar_start(moment, timeframe) + timeframe.delta != midnight + timedelta(days=1)
    ]
    assert not crossing_midnight, f"{timeframe.name}: бар выходит за сутки, например {crossing_midnight[0]}"


def test_bar_start_rejects_naive_time() -> None:
    with pytest.raises(ValueError, match="без часового пояса"):
        bar_start(datetime(2026, 8, 26, 10, 5), M5)


def test_minute_timeframe_is_identity() -> None:
    minutes = [minute(msk(2026, 8, 26, 10, m)) for m in (5, 6, 8)]
    bars = build_bars(minutes, MINUTE)
    assert [b.time for b in bars] == [c.time for c in minutes]
    assert all(not b.is_partial for b in bars)


def test_utc_input_is_converted_not_truncated() -> None:
    """Свеча, пришедшая в UTC, попадает в московский бар, а не в свой.

    Смешение с UTC даёт сдвиг окна на три часа — на графике это незаметно,
    в списке сделок заметно сразу.
    """
    from datetime import timezone

    utc = datetime(2026, 8, 26, 7, 6, tzinfo=timezone.utc)  # 10:06 МСК
    (bar,) = build_bars([minute(utc)], M5)
    assert bar.time == msk(2026, 8, 26, 10, 5)
