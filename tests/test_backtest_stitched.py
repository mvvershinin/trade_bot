"""Прогон по склейке: стык MXU6 → MXZ6 на синтетике.

Стерегомые поведения:

1. Позиция, открытая к стыку, закрывается на стыке по закрытию последнего
   бара старого контракта, с комиссией обеих сторон и причиной «конец
   поданного отрезка»; в журнале — строка «Смена контракта».
2. Скачок цены между контрактами не попадает ни в прибыль (ни одна сделка
   не входит в одном контракте и не выходит в другом), ни в среднюю (средняя
   нового куска считается только по его барам).
3. Прогрев из ровно N баров не даёт сделок, а первый бар куска уже решает.
   Прогрев длиннее N — отказ вслух.
4. Нехватка прогрева не молчит: строка в `problems` и предупреждение
   в журнале. Пустой кусок — тоже.
5. Прогон по одному куску без прогрева — ровно прежний `replay`.
6. Последний кусок не закрывается: позиция остаётся открытой, как у `replay`.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date, datetime, time

import pytest

from backtest import replay
from backtest.execution import Costs
from backtest.stitched import Piece, replay_pieces, stitched_lines
from engine import EngineSettings, ExitReason, JournalLevel, Mode, Side, TradingWindow, close_time
from strategies import EmaReverse, EmaReverseSettings
from tests.engine_helpers import MSK, STEP, FakeCandle, candle

PERIOD = 15
COMMISSION = 17.0
SETTINGS = EngineSettings(
    mode=Mode.REVERSE, window=TradingWindow(start=time(0, 0), end=time(0, 0)),
    close_on_time_end=False,
    take_profit=False, commission_per_side=COMMISSION,
)
COSTS = Costs(commission_per_side=COMMISSION)


def _series(day: int, hour: int, closes: list[float]) -> tuple[FakeCandle, ...]:
    """Бары подряд с `hour:00` дня `day` сентября 2026 с указанными закрытиями."""
    start = datetime(2026, 9, day, hour, 0, tzinfo=MSK)
    return tuple(
        replace(candle(hour, 0, close=close, day=day, month=9), time=start + number * STEP)
        for number, close in enumerate(closes)
    )


def _build() -> EmaReverse:
    return EmaReverse(EmaReverseSettings(period=PERIOD))


def _run(pieces: list[Piece]):
    return asyncio.run(replay_pieces(pieces, _build, SETTINGS, COSTS))


#: MXU6, среда 16.09: ровно, потом рост — лонг открыт и держится до конца.
OLD = _series(16, 10, [100_000.0] * 20 + [100_000.0 + 20 * step for step in range(1, 21)])
#: MXZ6: прогрев 16.09 вечером на 102 000 — на 1 600 выше старого, — потом рост.
WARMUP = _series(16, 18, [102_000.0] * PERIOD)
NEW = _series(17, 10, [102_000.0 + 10 * step for step in range(1, 31)])


def _old(**rest) -> Piece:
    return Piece("MXU6", date(2026, 9, 16), date(2026, 9, 16), (), OLD, 0, **rest)


def _new(warmup: tuple[FakeCandle, ...] = WARMUP) -> Piece:
    return Piece("MXZ6", date(2026, 9, 17), date(2026, 9, 17), warmup, NEW, PERIOD)


def test_position_is_closed_on_the_seam_at_the_last_price_of_the_old_contract() -> None:
    run = _run([_old(), _new()])
    seam = run.pieces[0].seam
    assert seam is not None, "лонг к стыку был открыт, а на стыке не закрыт"
    assert seam.side is Side.LONG
    assert seam.exit_price == OLD[-1].close
    assert seam.exit_time == close_time(OLD[-1])
    assert seam.exit_reason is ExitReason.SERIES_END
    assert seam.commission == COMMISSION * 2 * seam.volume
    assert seam in run.deals
    said = [entry for entry in run.journal if entry.event.startswith("Смена контракта MXU6 → MXZ6")]
    assert len(said) == 1, "закрытие на стыке прошло без строки в журнале"
    assert said[0].at == seam.exit_time


def test_the_seam_jump_is_neither_in_the_profit_nor_in_the_average() -> None:
    run = _run([_old(), _new()])
    new_start = NEW[0].time
    for deal in run.deals:
        crosses = deal.entry_time < new_start <= deal.exit_time
        assert not crosses, f"сделка перешла через стык: {deal}"
    old_top = max(bar.close for bar in OLD)
    new_floor = min(bar.close for bar in (*WARMUP, *NEW))
    new_average = [value for moment, value in run.average if moment >= close_time(NEW[0])]
    assert new_average, "у нового куска нет средней"
    assert min(new_average) >= new_floor > old_top, (
        "средняя нового контракта захватила бары старого"
    )


def test_warmup_of_exactly_n_bars_gives_no_trade_and_the_first_bar_decides() -> None:
    run = _run([_old(), _new()])
    new_run = run.pieces[1].run
    assert all(fill.at >= NEW[0].time for fill in new_run.fills), "прогрев дал сделку"
    assert new_run.plans, "на растущем ряде новый кусок обязан войти"
    assert new_run.plans[0].decided_at == close_time(NEW[0]), (
        "первое решение не на первом баре куска: прогрев подан не ровно N"
    )


def test_warmup_longer_than_the_period_is_refused() -> None:
    longer = (*_series(16, 17, [102_000.0]), *WARMUP)
    with pytest.raises(ValueError, match="баров прогрева"):
        _run([_old(), _new(longer)])


def test_short_warmup_is_said_aloud_and_its_bars_do_not_trade() -> None:
    run = _run([_old(), _new(WARMUP[3:])])
    assert any("MXZ6" in text and "12 закрытых баров" in text for text in run.problems)
    loud = [entry for entry in run.journal if entry.level is JournalLevel.WARNING]
    assert any(entry.event == "Прогрев неполный" for entry in loud)
    assert run.pieces[1].run.plans[0].decided_at == close_time(NEW[3])
    assert any("прогрев 12 из 15" in line for line in stitched_lines(run))


def test_an_empty_piece_is_said_aloud() -> None:
    empty = Piece("MXZ6", date(2026, 9, 17), date(2026, 9, 17), WARMUP, (), PERIOD)
    run = _run([_old(), empty])
    assert any("нет ни одной закрытой свечи" in text for text in run.problems)
    assert any(entry.event == "Кусок склейки пуст" for entry in run.journal)


def test_one_piece_without_warmup_is_the_plain_replay() -> None:
    plain = asyncio.run(replay(OLD, _build(), SETTINGS, costs=COSTS))
    run = _run([_old()])
    assert run.deals == plain.deals
    assert run.summary == plain.summary
    assert run.journal == plain.journal
    assert run.position == plain.position


def test_the_last_piece_is_left_open() -> None:
    run = _run([_old(), _new()])
    assert run.pieces[-1].seam is None
    assert run.position is not None and run.position.side is Side.LONG


def test_the_report_has_the_total_and_each_contract_with_commission_apart() -> None:
    run = _run([_old(), _new()])
    lines = stitched_lines(run)
    assert lines[0].startswith("Итог по склейке")
    assert "комиссия" in lines[0]
    assert lines[1].startswith("MXU6") and "позиция закрыта на стыке" in lines[1]
    assert lines[2].startswith("MXZ6")
    by_piece = sum(one.summary.trades for one in run.pieces)
    assert by_piece == run.summary.trades


def test_stitched_money_reads_like_the_report_window() -> None:
    """Деньги строк склейки — как в окне отчёта: запятая, пробел, без плюса у комиссии.

    Было «комиссия +1 820.00 ₽» рядом с «Комиссия: 1 820,00 ₽» того же окна.
    """
    from backtest.stitched import _money

    assert _money(-9762.0) == "−9 762,00 ₽"
    assert _money(1906.0) == "+1 906,00 ₽"
    assert _money(1820.0, sign=False) == "1 820,00 ₽"


#: MXH7 после пустого MXZ6: прогрев и тело — те же бары, что у MXZ6.
def _after_empty() -> list[Piece]:
    empty = Piece("MXZ6", date(2026, 9, 17), date(2026, 9, 17), WARMUP, (), PERIOD)
    later = Piece("MXH7", date(2026, 9, 17), date(2026, 9, 17), WARMUP, NEW, PERIOD)
    return [_old(), empty, later]


def test_every_stitch_journal_line_is_moscow_time() -> None:
    """Строка «Кусок склейки пуст» без пояса уезжала на машине +7 на 4 часа."""
    run = _run(_after_empty())
    assert any(entry.event == "Кусок склейки пуст" for entry in run.journal)
    naive = [entry.event for entry in run.journal if entry.at.utcoffset() != MSK.utcoffset(None)]
    assert not naive, f"строки журнала склейки не в МСК: {naive}"


def test_the_seam_after_an_empty_piece_names_the_contract_the_run_went_to() -> None:
    """Пустой кусок не прогнан — стык ведёт в следующий непустой."""
    run = _run(_after_empty())
    assert [one.piece.symbol for one in run.pieces] == ["MXU6", "MXH7"]
    said = [entry.event for entry in run.journal if entry.event.startswith("Смена контракта")]
    assert len(said) == 1 and said[0].startswith("Смена контракта MXU6 → MXH7"), said


def test_a_trailing_empty_piece_is_not_a_seam() -> None:
    """Перехода в пустой последний кусок не было — позиция остаётся открытой."""
    empty = Piece("MXZ6", date(2026, 9, 17), date(2026, 9, 17), WARMUP, (), PERIOD)
    run = _run([_old(), empty])
    assert run.pieces[0].seam is None
    assert run.position is not None and run.position.side is Side.LONG
