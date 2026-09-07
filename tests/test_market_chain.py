"""Сшивка ближних контрактов: рубеж, разрыв на стыке, отказ торговать рядом.

Пять вещей, каждая из которых при поломке даёт правдоподобный, но неверный ряд:

* **рубеж считается по объёму, а не по календарю.** Календарный рубеж сдвинул бы
  ряд на неделю и подменил бы контракт в самые ликвидные дни;
* **одиночный ранний обгон рубежа не создаёт.** Наивное «первый день, когда
  новый обогнал старого» срабатывает за восемь месяцев до экспирации
  на объёмах в единицы контрактов;
* **разрыв цены на стыке меряется в одну минуту.** Иначе в число попадает
  ночной ход рынка, и о разнице контрактов оно не говорит ничего;
* **повтор сборки не плодит дублей и не оставляет хвоста** прошлой сборки;
* **сшитым рядом нельзя торговать.** Ни через список инструментов окна,
  ни через поток свечей движка.
"""

from __future__ import annotations

import asyncio
import pathlib
from datetime import UTC, date, datetime, timedelta
from typing import cast

import pytest
from market_helpers import minute, msk

from market.candles import M5
from market.chain import (
    Leg,
    Seam,
    daily_volume,
    legs_of_chain,
    measure_seam,
    roll_day,
    stitch,
)
from market.storage import _RANKS, CandleStore, Source
from market.synthetic import SYNTHETIC_PREFIX, is_synthetic
from market.worker import MarketWorker


@pytest.fixture
def store(tmp_path: pathlib.Path):
    """База цепочки. Имя файла не рабочее: собранный ряд туда не пустят."""
    with CandleStore(tmp_path / "chain.sqlite3") as opened:
        yield opened


def day(number: int) -> date:
    """Будний день ряда по порядку — понедельник 01.06.2026 плюс `number`."""
    return date(2026, 6, 1) + timedelta(days=number)


def fill(store: CandleStore, symbol: str, days: dict[date, int], *, price: float) -> None:
    """Положить по одной минутке в день с заданным объёмом.

    Одна минутка на день — этого хватает: рубеж считается по дневному объёму,
    и лишние минутки только замедлили бы прогон.
    """
    candles = [
        minute(datetime.combine(when, datetime.min.time(), msk(2026, 1, 1).tzinfo)
               .replace(hour=10, minute=5), open=price, volume=float(volume))
        for when, volume in days.items()
        if volume
    ]
    store.put_minutes(symbol, candles, Source.ISS)


# -- рубеж ------------------------------------------------------------------


def test_roll_is_the_first_day_of_the_new_contract_after_the_old_stopped_leading() -> None:
    """Рубеж — первый день нового после последнего дня, когда старый был активнее."""
    old = {day(0): 100, day(1): 100, day(2): 100, day(3): 40}
    new = {day(0): 1, day(1): 10, day(2): 90, day(3): 200, day(4): 300}
    assert roll_day(old, new) == day(3)


def test_roll_ignores_an_early_flicker() -> None:
    """Единичный ранний обгон на единицах контрактов рубежа не создаёт.

    Замер 05.09.2026: 14.01.2025 у MXU5 за день прошло 2 контракта, у MXZ5 — 3.
    Правило «первый день, когда новый обогнал» назначило бы рубеж туда —
    за восемь месяцев до того, как MXZ5 стал ближним.
    """
    old = {day(0): 2, day(1): 500, day(2): 500, day(3): 500, day(4): 100}
    new = {day(0): 3, day(1): 1, day(2): 5, day(3): 20, day(4): 400, day(5): 900}
    assert roll_day(old, new) == day(4)


def test_roll_moves_with_the_volume_and_not_with_the_dates() -> None:
    """Те же дни, другие объёмы — другой рубеж. Календарь на него не влияет."""
    old = {day(i): 500 for i in range(5)}
    new = {day(i): 10 for i in range(5)} | {day(5): 10}
    early = dict(new) | {day(2): 900, day(3): 900, day(4): 900}
    assert roll_day(old, new) != roll_day(old, early)
    assert roll_day(old, early) == day(2)


def test_equal_volumes_leave_the_old_contract_in_place() -> None:
    """Ровно тот же объём — это не перевес нового.

    Перевес обязан быть строгим: «не хуже старого» и «активнее старого» —
    разные утверждения, и на равенстве рубеж двигаться не должен.
    """
    old = {day(0): 500, day(1): 100, day(2): 100}
    new = {day(0): 500, day(1): 100, day(2): 400, day(3): 900}
    assert roll_day(old, new) == day(2)


def test_roll_ignores_a_day_written_down_with_zero_volume() -> None:
    """День с записанным нулём в сравнении не участвует.

    Иначе он попал бы в «старый был не хуже» (ноль не меньше нуля) и утащил
    бы рубеж на себя — то есть на день, в который не торговали вовсе.
    """
    old = {day(0): 500, day(1): 100}
    new = {day(0): 10, day(1): 400, day(2): 0, day(3): 900}
    assert roll_day(old, new) == day(1)


def test_roll_skips_days_when_neither_contract_traded() -> None:
    """Выходной посреди перекрытия рубежа не двигает: сравнивать в нём нечего."""
    old = {day(0): 500, day(1): 500, day(2): 0, day(3): 100}
    new = {day(0): 10, day(1): 10, day(2): 0, day(3): 400, day(4): 900}
    assert roll_day(old, new) == day(3)


def test_roll_says_nothing_when_the_new_contract_has_no_days_after() -> None:
    """Недокачанная история нового контракта — это `None`, а не рубеж наугад."""
    old = {day(0): 500, day(1): 500}
    new = {day(0): 10, day(1): 10}
    assert roll_day(old, new) is None


def test_roll_starts_at_the_first_day_when_the_old_never_led() -> None:
    """Старый не был активнее ни разу — ряд начинается с первого дня со сделками."""
    old = {day(1): 1, day(2): 1}
    new = {day(1): 100, day(2): 100}
    assert roll_day(old, new) == day(1)


# -- отрезки ----------------------------------------------------------------


def three_contracts() -> list[tuple[str, dict[date, int]]]:
    """Цепочка из трёх контрактов с двумя чистыми рубежами: day(2) и day(5)."""
    first = {day(0): 500, day(1): 500, day(2): 100}
    second = {day(0): 10, day(1): 50, day(2): 400, day(3): 500, day(4): 500, day(5): 100}
    third = {day(3): 10, day(4): 50, day(5): 400, day(6): 500, day(7): 500}
    return [("AAA", first), ("BBB", second), ("CCC", third)]


def test_legs_are_contiguous_and_never_overlap() -> None:
    """Отрезки идут встык: ни одного общего дня и ни одного пропущенного."""
    legs = legs_of_chain(three_contracts())
    assert [leg.symbol for leg in legs] == ["BBB", "CCC"]
    for earlier, later in zip(legs, legs[1:], strict=False):
        assert later.since == earlier.until + timedelta(days=1)
    assert len(legs) > 1  # иначе цикл выше не проверил бы ничего


def test_the_first_contract_only_dates_the_second_and_is_not_in_the_series() -> None:
    """Первый контракт цепочки в ряд не входит — он датирует начало второго."""
    legs = legs_of_chain(three_contracts())
    assert "AAA" not in {leg.symbol for leg in legs}
    assert legs[0].since == day(2)


def test_legs_refuse_a_chain_out_of_expiry_order() -> None:
    """Рубежи обязаны идти по возрастанию: иначе отрезки поедут назад.

    Цепочка подобрана так, что оба рубежа считаются **успешно** — второй
    просто оказывается раньше первого. Отказ здесь исходит именно от проверки
    порядка, а не от «рубеж не нашёлся».
    """
    first = {day(number): 100 for number in range(5)} | {day(5): 10, day(6): 10}
    second = {day(number): 1 for number in range(5)} | {day(5): 200, day(6): 200}
    third = {day(number): 500 for number in range(8)}
    assert roll_day(first, second) == day(5)
    assert roll_day(second, third) == day(0)
    with pytest.raises(ValueError, match="не позже рубежа"):
        legs_of_chain([("AAA", first), ("BBB", second), ("CCC", third)])


def test_legs_refuse_a_chain_of_one() -> None:
    """Цепочка из одного контракта — отказ: датировать начало нечем."""
    with pytest.raises(ValueError, match="короче двух"):
        legs_of_chain([("AAA", {day(0): 1})])


def test_last_leg_ends_on_its_last_day_with_trades() -> None:
    """Последний отрезок кончается последним днём своих сделок, а не «сегодня»."""
    legs = legs_of_chain(three_contracts())
    assert legs[-1].until == day(7)


def test_leg_refuses_to_end_before_it_starts() -> None:
    """Отрезок с перевёрнутыми границами не строится вовсе."""
    with pytest.raises(ValueError, match="кончается раньше"):
        Leg("BBB", day(5), day(2))


# -- разрыв цены на стыке ---------------------------------------------------


def test_seam_gap_is_measured_at_the_same_minute(store: CandleStore) -> None:
    """Разрыв — разница цен двух контрактов в одну минуту, а не через ночь.

    Здесь старый контракт за ночь ушёл со 100 на 130, а разница между
    контрактами всё это время была ровно 10. Мерка «последняя цена старого
    против первой цены нового» показала бы 40 — то есть ночной ход рынка.
    """
    evening, morning = msk(2026, 6, 1, 23, 45), msk(2026, 6, 2, 10, 0)
    store.put_minutes("OLD", [minute(evening, open=100.0, close=100.0)], Source.ISS)
    store.put_minutes("NEW", [minute(evening, open=110.0, close=110.0),
                              minute(morning, open=140.0, close=140.0)], Source.ISS)
    seam = measure_seam(store, "OLD", "NEW", date(2026, 6, 2))
    assert seam.at == evening
    assert seam.gap == pytest.approx(10.0)
    assert seam.gap_percent == pytest.approx(10.0)


def test_seam_takes_the_last_common_minute_and_not_the_first(store: CandleStore) -> None:
    """Из нескольких общих минут берётся последняя перед рубежом.

    Разница контрактов меняется день ото дня: утренняя минута двухдневной
    давности сказала бы про позавчерашний базис, а не про сегодняшний стык.
    """
    early, late = msk(2026, 6, 1, 10, 0), msk(2026, 6, 1, 23, 45)
    store.put_minutes("OLD", [minute(early, open=100.0, close=100.0),
                              minute(late, open=100.0, close=100.0)], Source.ISS)
    store.put_minutes("NEW", [minute(early, open=150.0, close=150.0),
                              minute(late, open=110.0, close=110.0)], Source.ISS)
    seam = measure_seam(store, "OLD", "NEW", date(2026, 6, 2))
    assert seam.at == late
    assert seam.gap == pytest.approx(10.0)


def test_seam_without_a_common_minute_says_so_instead_of_zero(store: CandleStore) -> None:
    """Общей минуты нет — разрыв `None`. Ноль соврал бы «контракты сошлись»."""
    store.put_minutes("OLD", [minute(msk(2026, 6, 1, 10, 0))], Source.ISS)
    store.put_minutes("NEW", [minute(msk(2026, 6, 1, 10, 1))], Source.ISS)
    seam = measure_seam(store, "OLD", "NEW", date(2026, 6, 2))
    assert seam.at is None
    assert seam.gap is None and seam.gap_percent is None


def test_seam_does_not_look_back_further_than_asked(store: CandleStore) -> None:
    """Общая минута месячной давности о стыке не говорит — она не берётся."""
    store.put_minutes("OLD", [minute(msk(2026, 5, 1, 10, 0))], Source.ISS)
    store.put_minutes("NEW", [minute(msk(2026, 5, 1, 10, 0), open=110.0)], Source.ISS)
    assert measure_seam(store, "OLD", "NEW", date(2026, 6, 2), look_back=7).at is None
    assert measure_seam(store, "OLD", "NEW", date(2026, 6, 2), look_back=60).at is not None


def test_seam_gap_is_none_when_a_price_is_missing() -> None:
    """Незамеренный стык не притворяется нулевым разрывом."""
    assert Seam(day(0), "OLD", "NEW", None, None, None).gap is None
    assert Seam(day(0), "OLD", "NEW", None, 0.0, 10.0).gap_percent is None


# -- сборка ряда ------------------------------------------------------------


def stitched_store(store: CandleStore) -> list[Leg]:
    """Три контракта в базе и отрезки по ним. Цена у каждого своя."""
    for symbol, days in three_contracts():
        fill(store, symbol, days, price={"AAA": 100.0, "BBB": 110.0, "CCC": 120.0}[symbol])
    return legs_of_chain(three_contracts())


def test_every_day_of_the_series_comes_from_exactly_one_contract(store: CandleStore) -> None:
    """Минутка ряда принадлежит одному контракту: объёмы не складываются."""
    legs = stitched_store(store)
    stitch(store, store, "@MX", legs)
    for candle in store.minutes("@MX"):
        owners = [leg for leg in legs if leg.since <= candle.time.date() <= leg.until]
        assert len(owners) == 1
        expected = store.minutes(
            owners[0].symbol, candle.time, candle.time + timedelta(minutes=1)
        )
        assert candle.volume == expected[0].volume


def test_second_stitch_changes_nothing(store: CandleStore) -> None:
    """Повтор сборки даёт тот же ряд: ни одной лишней строки."""
    legs = stitched_store(store)
    first = stitch(store, store, "@MX", legs)
    before = store.minutes("@MX")
    second = stitch(store, store, "@MX", legs)
    assert second.forgotten == first.written
    assert store.minutes("@MX") == before


def test_moving_the_boundary_leaves_no_tail_of_the_previous_series(store: CandleStore) -> None:
    """Сборка с другим рубежом стирает прошлый ряд целиком, а не поверх него.

    Без этого за границей новых отрезков остался бы хвост прошлой сборки,
    и в ряду оказались бы дни двух контрактов сразу — то есть ровно та беда,
    от которой сшивка и защищает.
    """
    legs = stitched_store(store)
    stitch(store, store, "@MX", legs)
    shorter = [Leg("BBB", day(2), day(4)), Leg("CCC", day(5), day(6))]
    stitch(store, store, "@MX", shorter)
    assert {candle.time.date() for candle in store.minutes("@MX")} <= {
        day(number) for number in range(2, 7)
    }


def test_stitch_refuses_overlapping_legs(store: CandleStore) -> None:
    """Один день двух контрактов — отказ, а не удвоенный объём."""
    stitched_store(store)
    with pytest.raises(ValueError, match="налезают"):
        stitch(store, store, "@MX", [Leg("BBB", day(2), day(5)), Leg("CCC", day(5), day(7))])


def test_stitch_refuses_a_real_ticker_as_the_name_of_the_series(store: CandleStore) -> None:
    """Под именем биржевого кода собранный ряд неотличим от настоящих свечей."""
    legs = stitched_store(store)
    with pytest.raises(ValueError, match="код инструмента биржи"):
        stitch(store, store, "MXU6", legs)


def test_stitch_writes_the_seams_into_the_load_log(store: CandleStore) -> None:
    """Отчёт о сборке уходит в тот же журнал, и рубежи в нём видны числом."""
    legs = stitched_store(store)
    stitch(store, store, "@MX", legs)
    (record,) = [row for row in store.load_log("@MX")]
    assert record["source"] == Source.STITCHED.value
    assert "BBB→CCC" in str(record["summary"])


def test_daily_volume_counts_contracts_by_moscow_day(store: CandleStore) -> None:
    """Дневной объём режется по МСК: вечерняя минутка остаётся в своём дне."""
    store.put_minutes(
        "BBB",
        [minute(msk(2026, 6, 1, 23, 45), volume=7.0), minute(msk(2026, 6, 2, 0, 5), volume=3.0)],
        Source.ISS,
    )
    assert daily_volume(store, "BBB") == {date(2026, 6, 1): 7.0, date(2026, 6, 2): 3.0}


class UtcTimes:
    """Источник минуток, отдающий время в UTC. Подпорка на один сторож.

    Хранилище сегодня отдаёт МСК, и на нём эта разница не видна вовсе —
    поэтому она проверяется здесь, а не через базу. Уедет `_from_ts` на UTC —
    рубеж поедет на три часа, а по календарной дате это ещё и другой день.
    """

    def minute_volumes(
        self, symbol: str, since: object = None, until: object = None
    ) -> list[tuple[datetime, float]]:
        """Две минутки одного московского дня, записанные по Гринвичу."""
        return [
            (datetime(2026, 6, 1, 20, 45, tzinfo=UTC), 7.0),   # 01.06 23:45 МСК
            (datetime(2026, 6, 1, 21, 5, tzinfo=UTC), 3.0),    # 02.06 00:05 МСК
        ]


def test_daily_volume_converts_before_it_counts() -> None:
    """Время в UTC режется всё равно по московским суткам, а не по гринвичским."""
    counted = daily_volume(cast(CandleStore, UtcTimes()), "BBB")
    assert counted == {date(2026, 6, 1): 7.0, date(2026, 6, 2): 3.0}


# -- сшитым рядом нельзя торговать ------------------------------------------


def test_synthetic_name_is_told_apart_from_an_exchange_code() -> None:
    """Имя ряда начинается с символа, которого в кодах биржи нет."""
    assert is_synthetic(SYNTHETIC_PREFIX + "MX")
    assert not is_synthetic("MXU6")
    assert not is_synthetic("SBER")


def test_synthetic_never_lands_in_the_working_base(tmp_path: pathlib.Path) -> None:
    """В `candles.sqlite3` собранный ряд не пишется: оттуда его увидит окно."""
    with CandleStore(tmp_path / "candles.sqlite3") as working:
        with pytest.raises(ValueError, match="в рабочую базу"):
            working.put_minutes("@MX", [minute(msk(2026, 6, 1, 10, 0))], Source.STITCHED)
        assert working.minutes("@MX") == []


def test_a_real_instrument_still_goes_into_the_working_base(tmp_path: pathlib.Path) -> None:
    """Сторож не мешает обычной загрузке — иначе он поймал бы всё подряд."""
    with CandleStore(tmp_path / "candles.sqlite3") as working:
        written = working.put_minutes("MXU6", [minute(msk(2026, 6, 1, 10, 0))], Source.ISS)
    assert written.written == 1


def test_the_engine_and_the_window_cannot_ask_for_the_stitched_series(
    tmp_path: pathlib.Path,
) -> None:
    """`MarketWorker` — единственная дверь свечей в окно и движок, и она закрыта."""

    async def ask() -> None:
        async with MarketWorker(tmp_path / "candles.sqlite3") as worker:
            with pytest.raises(ValueError, match="не инструмент биржи"):
                await worker.minutes("@MX")
            with pytest.raises(ValueError, match="не инструмент биржи"):
                await worker.bars("@MX", M5)
            assert await worker.minutes("MXU6") == []

    asyncio.run(ask())


def test_forget_minutes_refuses_a_real_instrument(store: CandleStore) -> None:
    """Настоящие свечи не стираются ничем: вернуть их будет неоткуда."""
    store.put_minutes("MXU6", [minute(msk(2026, 6, 1, 10, 0))], Source.ISS)
    with pytest.raises(ValueError, match="не стираются"):
        store.forget_minutes("MXU6")
    assert len(store.minutes("MXU6")) == 1


def test_every_source_has_a_rank() -> None:
    """Источник без ранга падал бы не при добавлении, а при первой записи."""
    assert set(_RANKS) == set(Source)
    assert _RANKS[Source.STITCHED] >= _RANKS[Source.ISS]
