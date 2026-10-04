"""Нарезка периода по таблице контрактов: какие бары получает каждый кусок.

Стерегомые поведения:

1. Кусок читается своим тикером и только в своих днях: минуты MXU6 после
   его рубежа в прогон не попадают, хвост MXZ6 до рубежа — тоже.
2. Перед куском, кроме первого, — ровно N последних закрытых баров того же
   контракта до рубежа; N — из запроса (период средней из настроек).
3. Дни периода без контракта не пропадают молча: предупреждение
   в `problems` и в журнале прогона.
4. Нехватка прогрева в базе доходит до `problems`.
"""

from __future__ import annotations

import asyncio
import dataclasses
import pathlib
from collections.abc import Iterator
from datetime import date, datetime, time, timedelta

import pytest

from app import convert, stitched_view
from app.stitched import StitchRequest, load_pieces, run_stitched, uncovered
from backtest.assumptions import assumptions
from backtest.execution import Costs, LimitExits, MinuteOrder
from backtest.stitched import replay_pieces
from engine import EngineSettings, Mode, TradingWindow
from market.candles import M5, MSK, Timeframe
from market.chain import Leg
from market.storage import CandleStore, ContractRow, Source
from strategies import EmaReverse, EmaReverseSettings
from tests.market_helpers import minute
from ui.models import MinutePriceOrder, Settings
from ui.models import Mode as EngineMode

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=MSK)
SEAM = date(2026, 9, 17)
#: Торговые дни с минутами: у MXZ6 хвост с понедельника, у MXU6 — и после рубежа.
DAYS = {
    "MXU6": (date(2026, 9, 16), date(2026, 9, 17)),
    "MXZ6": (date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 16), date(2026, 9, 17)),
}
BARS_A_DAY = 24  # 10:00–12:00, по пять минут


@pytest.fixture
def store(tmp_path: pathlib.Path) -> Iterator[CandleStore]:
    with CandleStore(tmp_path / "work.sqlite3") as opened:
        opened.put_contracts(
            [ContractRow("MXU6", active_from=date(2026, 6, 8), active_to=date(2026, 9, 16)),
             ContractRow("MXZ6", active_from=SEAM)],
            now=NOW,
        )
        for symbol, days in DAYS.items():
            for day in days:
                start = datetime.combine(day, time(10, 0), MSK)
                opened.put_minutes(
                    symbol,
                    [minute(start + timedelta(minutes=i), volume=1.0)
                     for i in range(BARS_A_DAY * 5)],
                    Source.ISS,
                )
            opened.mark_days_requested(symbol, days, now=NOW)
        yield opened


def _request(since: date, warmup: int = 15, look_back: int = 14) -> StitchRequest:
    return StitchRequest(
        since=since, until=SEAM, timeframe=M5, warmup_bars=warmup, look_back_days=look_back,
        asset="MX",
    )


def test_each_piece_reads_its_own_contract_only_in_its_own_days(store: CandleStore) -> None:
    made, notes = load_pieces(store, _request(date(2026, 9, 16)))
    assert [(one.symbol, one.since, one.until) for one in made] == [
        ("MXU6", date(2026, 9, 16), date(2026, 9, 16)),
        ("MXZ6", SEAM, SEAM),
    ]
    old, new = made
    assert len(old.body) == BARS_A_DAY
    assert {bar.time.date() for bar in old.body} == {date(2026, 9, 16)}
    assert {bar.time.date() for bar in new.body} == {SEAM}
    assert notes == []


def test_warmup_is_exactly_the_last_n_bars_of_the_same_contract(store: CandleStore) -> None:
    old, new = load_pieces(store, _request(date(2026, 9, 16)))[0]
    assert old.warmup == () and old.warmup_wanted == 0, "первый кусок стартует холодным"
    assert new.warmup_wanted == 15
    assert len(new.warmup) == 15
    assert all(bar.time < datetime.combine(SEAM, time(0), MSK) for bar in new.warmup)
    assert new.warmup[-1].time == datetime(2026, 9, 16, 11, 55, tzinfo=MSK)
    zulu = store.bars("MXZ6", M5, since=datetime(2026, 9, 16, 10, 45, tzinfo=MSK),
                      until=datetime(2026, 9, 17, tzinfo=MSK), drop_unsettled=True)
    assert list(new.warmup) == zulu


def test_days_without_a_contract_are_said_aloud(store: CandleStore) -> None:
    made, notes = load_pieces(store, _request(date(2026, 6, 1)))
    assert [text for _, text in notes] == [
        "01.06.2026 … 07.06.2026: в таблице контрактов нет ближнего на эти дни — "
        "они в прогон не вошли"
    ]
    settings = EngineSettings(mode=Mode.REVERSE, window=TradingWindow(start=time(0), end=time(0)))
    run = asyncio.run(replay_pieces(
        made, lambda: EmaReverse(EmaReverseSettings(period=15)), settings, Costs(), notes,
    ))
    assert notes[0][1] in run.problems
    assert any(entry.reason == notes[0][1] for entry in run.journal)


def test_missing_warmup_in_the_base_reaches_the_problems(store: CandleStore) -> None:
    made, _ = load_pieces(store, _request(date(2026, 9, 16), warmup=30, look_back=1))
    assert len(made[1].warmup) == BARS_A_DAY
    settings = EngineSettings(mode=Mode.REVERSE, window=TradingWindow(start=time(0), end=time(0)))
    run = asyncio.run(replay_pieces(
        made, lambda: EmaReverse(EmaReverseSettings(period=30)), settings, Costs(),
    ))
    said = "MXZ6: перед 17.09.2026 в базе 24 закрытых"
    assert any(text.startswith(said) for text in run.problems)


def test_holes_are_found_before_between_and_after_the_pieces() -> None:
    legs = [Leg("A", date(2026, 1, 5), date(2026, 1, 9)),
            Leg("B", date(2026, 1, 12), date(2026, 1, 16))]
    assert uncovered(legs, date(2026, 1, 1), date(2026, 1, 20)) == [
        (date(2026, 1, 1), date(2026, 1, 4)),
        (date(2026, 1, 10), date(2026, 1, 11)),
        (date(2026, 1, 17), date(2026, 1, 20)),
    ]
    assert uncovered(legs, date(2026, 1, 5), date(2026, 1, 9)) == []


def test_the_entry_point_runs_the_window_settings_over_both_contracts(
    store: CandleStore,
) -> None:
    """Точка входа для окна: настройки окна → куски → прогон. Проводка, а не половина."""
    values = Settings()
    engine = convert.engine_settings(values, EngineMode.REVERSE)
    run = asyncio.run(run_stitched(store, date(2026, 9, 16), SEAM, values, engine))
    assert [one.piece.symbol for one in run.pieces] == ["MXU6", "MXZ6"]
    assert run.pieces[1].piece.warmup_wanted == values.average_period
    assert len(run.pieces[1].piece.warmup) == values.average_period


class _Asked(Exception):
    """Запрос нарезки перехвачен — дальше прогон тесту не нужен."""


@pytest.mark.parametrize(
    ("trailing", "timeframe", "wanted"),
    [
        (True, "5 минут", MinuteOrder.ADVERSE_FIRST),
        (False, "5 минут", None),
        (True, "30 минут", None),
    ],
    ids=["trailing-5", "fixed-take-5", "trailing-30"],
)
def test_the_tester_stitch_asks_minutes_only_for_a_trailing_level_on_a_small_bar(
    store: CandleStore, monkeypatch: pytest.MonkeyPatch, trailing: bool,
    timeframe: str, wanted: MinuteOrder | None,
) -> None:
    """Стережёт (B-058): точка входа тестера `run_stitched` просит минутки по правилу окна.

    Склейка тестера собирает запрос сама (`app/stitched.py`), а не через
    `stitched_view.request_of`, и сторож `request_of` её не видит. Минутки —
    только скользящему уровню на свече не крупнее порога (5 мин): без
    скользящего уровня и на 30 минутах `minute_order` запроса пуст.
    Первый случай — контроль: скользящий уровень на пятиминутках минутки
    получает, и в порядке из настроек.

    Вход подставной: `load_pieces` подменён перехватом запроса, база временная.
    """
    import app.stitched as stitched_module

    asked: list[StitchRequest] = []

    def caught(store: object, request: StitchRequest) -> object:
        asked.append(request)
        raise _Asked

    monkeypatch.setattr(stitched_module, "load_pieces", caught)
    values = Settings(
        minute_order=MinutePriceOrder.ADVERSE_FIRST,
        trailing_enabled=trailing, timeframe=timeframe,
    )
    engine = convert.engine_settings(values, EngineMode.REVERSE)
    with pytest.raises(_Asked):
        asyncio.run(run_stitched(
            store, date(2026, 9, 16), SEAM, values, engine,
        ))
    assert len(asked) == 1
    assert asked[0].minute_order is wanted, (
        f"склейка тестера: минутки {asked[0].minute_order!r}, по правилу B-058 {wanted!r}"
    )


def test_the_window_names_the_same_seam_neighbour_as_the_journal() -> None:
    """MXU6, пустой MXZ6, MXH7: журнал, таблица сделок и отчёт — все «→ MXH7»."""
    from dataclasses import replace

    from app.stitched_view import lines, seam_reason
    from tests.test_backtest_stitched import _after_empty, _run

    run = _run(_after_empty())
    journal = [e.event for e in run.journal if e.event.startswith("Смена контракта")]
    assert journal and all("MXU6 → MXH7" in event for event in journal), journal
    labels = seam_reason(run)
    assert list(labels.values()) == ["смена контракта MXU6 → MXH7"], labels
    assert not any("→ MXZ6" in line for line in lines(run))
    # Стык без позиции — та же подпись соседа.
    quiet = replace(run, pieces=(replace(run.pieces[0], seam=None), *run.pieces[1:]))
    said = [line for line in lines(quiet) if "к стыку позиции не было" in line]
    assert said == ["Смена контракта MXU6 → MXH7 с 17.09.2026: к стыку позиции не было."]


def test_each_piece_gets_the_minutes_of_its_own_contract_under_warmup_and_body(
    tmp_path: pathlib.Path,
) -> None:
    """Стережёт: кусок склейки получает минутки **своего** контракта и порядок из запроса.

    Под каждым баром прогрева и тела лежат минутки того же тикера — иначе
    прогрев целиком ушёл бы в «бары без минуток», а минутки соседнего
    контракта задевали бы уровень ценами, которых у этого не было. Цены
    у двух тикеров здесь разные (100 и 200) — иначе подмена тикера была бы
    неотличима. Без порядка в запросе минуток нет вовсе — по размаху бара.
    """
    price = {"MXU6": 100.0, "MXZ6": 200.0}
    with CandleStore(tmp_path / "own.sqlite3") as store:
        store.put_contracts(
            [ContractRow("MXU6", active_from=date(2026, 6, 8), active_to=date(2026, 9, 16)),
             ContractRow("MXZ6", active_from=SEAM)],
            now=NOW,
        )
        for symbol, days in DAYS.items():
            for day in days:
                start = datetime.combine(day, time(10, 0), MSK)
                store.put_minutes(
                    symbol,
                    [minute(start + timedelta(minutes=i), open=price[symbol])
                     for i in range(BARS_A_DAY * 5)],
                    Source.ISS,
                )
            store.mark_days_requested(symbol, days, now=NOW)

        plain, _ = load_pieces(store, _request(date(2026, 9, 16)))
        assert [one.minutes for one in plain] == [None, None]

        request = dataclasses.replace(
            _request(date(2026, 9, 16)), minute_order=MinuteOrder.ADVERSE_FIRST
        )
        made, _ = load_pieces(store, request)
    assert [one.symbol for one in made] == ["MXU6", "MXZ6"]
    for piece in made:
        assert piece.minutes is not None, f"{piece.symbol}: кусок без минуток"
        assert piece.minutes.order is MinuteOrder.ADVERSE_FIRST
        assert {one.open for one in piece.minutes.candles} == {price[piece.symbol]}, (
            f"{piece.symbol}: под кусок легли минутки чужого контракта"
        )
        bars = (*piece.warmup, *piece.body)
        times = {one.time for one in piece.minutes.candles}
        assert all(
            bar.time + timedelta(minutes=step) in times for bar in bars for step in range(5)
        ), f"{piece.symbol}: под частью баров прогрева или тела минуток нет"


def test_the_stitched_window_run_says_how_it_walked_the_minutes(
    store: CandleStore,
) -> None:
    """Стережёт: итог склейки окна несёт порядок минуток и счёт баров без них.

    `stitched_view.as_history` собирает один `HistoryRun` из кусков; потерянный
    там порядок или счёт означал бы отчёт склейки, молчащий о том, что часть
    свечей посчитана по размаху. Порядок — из настроек окна (`request_of`).

    Минутки склейке — по тому же правилу, что окну (B-058): только скользящему
    уровню и только на свече не крупнее порога. Без скользящего уровня
    и на 30 минутах `request_of` минуток не просит.
    """
    values = Settings(minute_order=MinutePriceOrder.ADVERSE_FIRST, trailing_enabled=True)
    since = date(2026, 9, 16)
    assert stitched_view.request_of(
        dataclasses.replace(values, trailing_enabled=False), M5, since, SEAM, asset="MX"
    ).minute_order is None, "неподвижному тейку склейка просит минутки"
    assert stitched_view.request_of(
        values, Timeframe(30), since, SEAM, asset="MX"
    ).minute_order is None, "свече крупнее порога склейка просит минутки"
    request = stitched_view.request_of(values, M5, since, SEAM, asset="MX")
    assert request.minute_order is MinuteOrder.ADVERSE_FIRST
    made, notes = load_pieces(store, request)
    # Под последним баром тела MXZ6 минуток нет — их вырезали из куска.
    last = made[-1]
    assert last.minutes is not None
    cut = tuple(one for one in last.minutes.candles if one.time < last.body[-1].time)
    made[-1] = dataclasses.replace(
        last, minutes=dataclasses.replace(last.minutes, candles=cut)
    )
    engine = convert.engine_settings(values, EngineMode.REVERSE)
    stitched = asyncio.run(stitched_view.run(
        stitched_view.Stitch(pieces=tuple(made), notes=tuple(notes)), values, engine,
    ))
    shown = stitched_view.as_history(stitched, convert.run_costs(values))
    assert shown.minute_order is MinuteOrder.ADVERSE_FIRST
    assert shown.bars_without_minutes == 1, shown.bars_without_minutes


def test_the_stitched_run_keeps_the_limit_exits_of_every_piece(
    store: CandleStore,
) -> None:
    """Стережёт: итог склейки несёт счёт выходов с предельной ценой всех кусков.

    `stitched_view.as_history` собирает `HistoryRun` руками; поле, забытое
    там, обнуляет счёт — и неисполненная заявка, остановившая робота,
    пропадает из допущений склеенного прогона (правило 13).

    Вход подставной: счёт кусков вписан в их прогоны, проверяется сборка
    итога, а не модель исполнения (её стережёт `tests/test_backtest_limit_exit.py`).

    ⚠️ Мутация: снять строку `limit_exits=` в `as_history` — допущения
    «Выход по времени с предельной ценой» нет, проверка падает.
    """
    values = Settings()
    request = stitched_view.request_of(values, M5, date(2026, 9, 16), SEAM, asset="MX")
    made, notes = load_pieces(store, request)
    engine = convert.engine_settings(values, EngineMode.REVERSE)
    stitched = asyncio.run(stitched_view.run(
        stitched_view.Stitch(pieces=tuple(made), notes=tuple(notes)), values, engine,
    ))
    assert len(stitched.pieces) == 2, "склейка без двух кусков сумму не проверяет"
    counted = (
        LimitExits(submitted=2, at_open=1, unfilled=1),
        LimitExits(submitted=1, at_touch=1),
    )
    stitched = dataclasses.replace(stitched, pieces=tuple(
        dataclasses.replace(one, run=dataclasses.replace(one.run, limit_exits=count))
        for one, count in zip(stitched.pieces, counted, strict=True)
    ))
    shown = stitched_view.as_history(stitched, convert.run_costs(values))
    assert shown.limit_exits == LimitExits(submitted=3, at_open=1, at_touch=1, unfilled=1)
    named = [one for one in assumptions(shown)
             if one.name == "Выход по времени с предельной ценой"]
    assert named, "неисполненный выход с предельной ценой пропал из допущений склейки"
    assert "не исполнилось 1" in named[0].short, named[0].short
