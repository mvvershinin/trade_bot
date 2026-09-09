"""Прогон по истории записывается: с чем гнали, на чём и что вышло.

Повод, а не гипотеза. Владелец счёта показал итог — 143 сделки, чистая
−6 335 ₽, профит-фактор 0,82 — и спросил, почему убыток. Перебор 270 сочетаний
настроек его цифры не воспроизвёл, а посмотреть, что он запускал, было негде:
таблица `journal_session` существовала и была пуста.

Каждый тест здесь стережёт **одно** поведение, и оно названо первой строкой
его докстринга. Ни один не утверждает, что робот принял верное решение: это
проверяют тесты `engine/` и сверка с прототипом.
"""

from __future__ import annotations

import asyncio
import dataclasses
import io
import math
import os
import pathlib
import subprocess
import sys
from dataclasses import dataclass, fields
from datetime import date, datetime, time, timedelta
from typing import cast

import pytest

os.environ.setdefault("QT_API", "pyside6")  # до первого импорта qasync

from app import convert, runs
from app.port import HistoryPort
from app.runs import (
    _COSTS_TITLES,
    _ENGINE_TITLES,
    RUNS_KEPT,
    RUNS_SHOWN,
    RunConditions,
    RunLog,
    result_note,
    settings_text,
    show_runs,
    snapshot_marks,
)
from backtest import Costs
from engine import DayMarks, EngineSettings, Mode, PartialCandles, Reversal, TradingWindow
from market import (
    BACKTEST_SESSIONS_KEPT,
    MSK,
    Candle,
    CandleStore,
    MarketWorker,
    RunOrigin,
    SessionRecord,
    Source,
    Timeframe,
    build_bars,
)
from market.journal import SECRET_MASK, redact
from strategies import (
    AverageKind,
    EmaReverse,
    EmaReverseSettings,
    OnPriceEqualsAverage,
    registry,
)
from ui.models import RunOrigin as WindowOrigin
from ui.models import Settings

DAY = datetime(2026, 6, 19, 7, 0, tzinfo=MSK)  # пятница, до открытия окна


def _minutes(count: int = 300, start: datetime = DAY) -> list[Candle]:
    """Минутки с колебанием вокруг ровной цены: без движения сделок не будет."""
    out = []
    for index in range(count):
        price = 100000.0 + 300.0 * math.sin(index / 9.0)
        out.append(Candle(
            time=start + timedelta(minutes=index),
            open=price, high=price + 40.0, low=price - 40.0, close=price,
            volume=1.0, timeframe=Timeframe(1), filled_minutes=1,
        ))
    return out


@pytest.fixture
def database(tmp_path: pathlib.Path) -> pathlib.Path:
    path = tmp_path / "candles.sqlite3"
    with CandleStore(path) as store:
        store.put_minutes("MXU6", _minutes(), Source.ISS)
    return path


def _bars(count: int = 300) -> list[Candle]:
    return list(build_bars(_minutes(count), Timeframe(5)))


def _conditions(*, days: int = 30, until: datetime | None = None) -> RunConditions:
    """Условия прогона для проверок записи. Меняются только отбор и правый край."""
    return RunConditions(
        origin=RunOrigin.BACKTEST,
        symbol="MXU6",
        timeframe="5 минут",
        engine=EngineSettings(mode=Mode.REVERSE, commission_per_side=14.0),
        strategy=EmaReverseSettings(),
        algorithm=registry.default_entry(),
        app_version="1.2.3",
        days=days,
        until=until,
    )


def _passes(
    loop, database: pathlib.Path, *runs: Settings, worker_class=MarketWorker, **kwargs
) -> tuple[HistoryPort, list[str]]:
    """Прогнать порт столько раз, сколько дано наборов настроек.

    Первый набор ставится при сборке, остальные — через `apply_settings`,
    то есть тем же путём, каким их меняет владелец счёта в окне.
    """
    journal: list[str] = []

    async def go():
        worker = worker_class(database)
        if database.exists():
            await worker.open()
        first = runs[0] if runs else Settings()
        port = HistoryPort(worker, values=first, days=0, sanitize=redact, **kwargs)
        port.decision_appended.connect(lambda row: journal.append(f"{row.event}: {row.reason}"))
        try:
            port.refresh("тест")
            await port.wait()
            for values in runs[1:]:
                port.apply_settings(values)
                await port.wait()
        finally:
            await port.aclose()
            await worker.close()
        return port

    return loop.run_until_complete(go()), journal


def _sessions(database: pathlib.Path):
    with CandleStore(database) as store:
        return list(store.journal_sessions(limit=999).rows)


# ------------------------------------------------- прогон обязан записаться

def test_a_pass_of_the_engine_leaves_a_finished_row_with_conditions_and_result(
    loop, database
) -> None:
    """Стережёт: прогон, который посчитал числа, не остаётся без записи.

    Ровно этого не было 04.09.2026: `journal_session` в базе владельца счёта
    была пуста при живой программе, и на вопрос «что я запускал» ответить
    было нечем ни ему, ни разбору.
    """
    _passes(loop, database, Settings())

    sessions = _sessions(database)
    assert len(sessions) == 1, "проход движка не записан в журнал прогонов"
    session = sessions[0]
    assert session.origin is RunOrigin.BACKTEST, "происхождение прогона не то"
    assert session.finished_at is not None, "прогон закрылся, но остался незакрытым"
    assert session.symbol == "MXU6"
    assert session.timeframe == "5 минут"
    assert session.strategy == EmaReverse.title
    assert session.app_version, "версия программы не записана"
    assert "Настройки движка" in session.settings, "снимка настроек в записи нет"
    assert "свечей" in session.note, "на чём гнали — не записано"
    assert "Сделок" in session.finish_note, "итог прогона не записан"


def test_each_new_pass_makes_its_own_row(loop, database) -> None:
    """Стережёт: смена настройки даёт СВОЮ запись, а не правит прошлую.

    Прогон гоняется заново на каждое изменение настроек, и записи обязаны
    расходиться: иначе разобрать, какой именно набор дал показанные числа,
    по-прежнему нечем.
    """
    _passes(loop, database, Settings(), Settings(average_period=20))

    sessions = _sessions(database)
    assert len(sessions) == 2, "второй прогон не записался отдельной строкой"
    snapshots = {session.settings for session in sessions}
    assert len(snapshots) == 2, "две разные настройки дали одинаковый снимок"
    assert any("Период средней: 20" in text for text in snapshots)
    assert any("Период средней: 15" in text for text in snapshots)


def test_the_port_writes_no_decisions_and_no_trades_and_that_holds_the_limit(
    loop, database
) -> None:
    """Стережёт опору под `RUNS_KEPT`: порт кладёт строку прогона и больше ничего.

    Двести прогонов помещаются в базу только потому, что прогон стоит около
    двух килобайт. Начни порт писать журнал решений и сделок — та же двести
    станет сотней мегабайт, и число обязано вернуться к порядку
    `BACKTEST_SESSIONS_KEPT`. Тест падает ровно в тот день, когда это случится.
    """
    _passes(loop, database, Settings())

    with CandleStore(database) as store:
        stats = store.journal_stats()
    assert stats.sessions == 1
    assert stats.decisions == 0 and stats.trades == 0, (
        "порт начал писать строки журналов — пересмотрите RUNS_KEPT: "
        f"{RUNS_KEPT} прогонов со строками это уже не мегабайты"
    )
    assert RUNS_KEPT > BACKTEST_SESSIONS_KEPT, (
        "своё число хранения потеряло смысл: оно и заведено ради того, "
        "что строка прогона здесь дешевле полного прогона"
    )


# ------------------------------------------------------------ полнота полей

@pytest.mark.parametrize(
    ("titles", "kind"),
    [
        (_ENGINE_TITLES, EngineSettings),
        # ⚠️ Подписи полей торгового алгоритма живут не в `app/runs.py`,
        # а в записи реестра: свой список в сборке был бы второй правдой
        # и разошёлся бы с алгоритмом молча (`D-100`). Проверка от этого
        # не ослабла — она берёт таблицу оттуда, где та теперь лежит,
        # и точно так же падает на поле, заведённом завтра и не подписанном.
        (registry.default_entry().titles(), EmaReverseSettings),
        (_COSTS_TITLES, Costs),
    ],
    ids=["движок", "торговый модуль", "издержки"],
)
def test_every_field_has_a_human_title(titles, kind) -> None:
    """Стережёт полноту подписей: поле, заведённое завтра, названо по-русски.

    Сломать этот тест просто — добавить поле в `EngineSettings` и не тронуть
    `app/runs.py`. Он и обязан упасть: в саму запись поле попадёт само
    (обходом `dataclasses.fields`), но человек прочитает в снимке
    `close_wait_bars` вместо «Ожидание исполнения выхода».
    """
    named = {field.name for field in fields(kind)}
    assert set(titles) == named, (
        "подписи полей разошлись с самими полями. Лишние: "
        f"{sorted(set(titles) - named)}; без подписи: {sorted(named - set(titles))}"
    )


def test_a_setting_added_tomorrow_lands_in_the_snapshot_by_itself() -> None:
    """Стережёт устройство полноты: снимок снимается ОБХОДОМ, а не списком.

    Поле, о котором в `app/runs.py` не знают, обязано оказаться в записи —
    пусть и своим именем, без человеческой подписи. Тест падает, если обход
    `dataclasses.fields` заменят перечислением полей руками: именно такое
    перечисление молчит, когда настройку добавили, а дописать забыли
    (`CLAUDE.md`, правило 9).
    """

    @dataclass(frozen=True)
    class SettingsOfTomorrow:
        mode: Mode = Mode.LONG_ONLY
        daily_loss_limit_rub: float = 25000.0

    # Подставной класс завтрашних настроек — не `EngineSettings`, и приведение
    # здесь именно про это: снимок обязан сниматься с любого набора полей.
    tomorrow = cast(EngineSettings, SettingsOfTomorrow())
    text = settings_text(
        tomorrow, EmaReverseSettings(), algorithm=registry.default_entry()
    )
    assert "daily_loss_limit_rub: 25000" in text, (
        "поле, у которого нет подписи, выпало из снимка целиком — "
        "значит снимок собирается списком, а не обходом полей"
    )


#: Другое значение на каждое поле `EngineSettings` — законное, а не любое:
#: `EngineSettings.__post_init__` отвергает негодное, и подстановка вслепую
#: проверяла бы отказ вместо записи. Полнота таблицы стережётся тем же
#: сравнением с полями класса, что и подписи.
_ANOTHER_ENGINE: dict[str, object] = {
    "mode": Mode.LONG_ONLY,
    "window": TradingWindow(time(9, 0), time(9, 30)),
    "close_on_time_end": False,
    "trade_in_weekend": True,
    "volume": 2.0,
    "reversal": Reversal.SAME_BAR,
    "take_profit": False,
    "take_profit_percent": 1.5,
    "trailing_take_profit": True,
    "trailing_start_percent": 0.9,
    "trailing_offset_percent": 0.3,
    "trailing_step_percent": 0.07,
    "stop_after_take_profit": False,
    "partial_candles": PartialCandles.SKIP,
    "commission_per_side": 7.0,
    "ruble_per_point": 2.0,
    "close_wait_bars": 5,
    "volume_cap": 9.0,
    "daily_loss_limit_percent": 3.5,
    "free_funds_reserve_percent": 25.0,
    "calendar": DayMarks.of({date(2026, 6, 12): False}),
    "exchange_days": DayMarks.of({date(2026, 6, 12): False}),
}

_ANOTHER_STRATEGY: dict[str, object] = {
    "period": 20,
    "kind": AverageKind.SMA,
    "on_equal": OnPriceEqualsAverage.TREAT_AS_LONG,
    "threshold_percent": 0.2,
    "confirm_bars": 2,
}


def test_the_table_of_other_values_covers_every_field() -> None:
    """Стережёт сам следующий тест: неполная таблица делала бы его слепым."""
    assert set(_ANOTHER_ENGINE) == {field.name for field in fields(EngineSettings)}
    assert set(_ANOTHER_STRATEGY) == {field.name for field in fields(EmaReverseSettings)}


@pytest.mark.parametrize("name", sorted(_ANOTHER_ENGINE))
def test_changing_any_engine_setting_changes_the_snapshot(name: str) -> None:
    """Стережёт мерило полноты: два разных набора настроек дают разные записи.

    Это и есть повторимость, выраженная проверяемо. Поле, выпавшее из снимка,
    делает две разные настройки неотличимыми в базе — ровно тот случай,
    когда числа владельца счёта не воспроизводятся, а объяснить это нечем.
    """
    base = EngineSettings()
    other = base.replace(**{name: _ANOTHER_ENGINE[name]})
    assert settings_text(
        base, EmaReverseSettings(), algorithm=registry.default_entry()
    ) != settings_text(
        other, EmaReverseSettings(), algorithm=registry.default_entry()
    ), f"настройка `{name}` не видна в снимке: два разных прогона запишутся одинаково"


@pytest.mark.parametrize("name", sorted(_ANOTHER_STRATEGY))
def test_changing_any_strategy_setting_changes_the_snapshot(name: str) -> None:
    """Стережёт то же самое для настроек торгового модуля."""
    base = EmaReverseSettings()
    other = base.replace(**{name: _ANOTHER_STRATEGY[name]})
    engine = EngineSettings()
    assert settings_text(
        engine, base, algorithm=registry.default_entry()
    ) != settings_text(
        engine, other, algorithm=registry.default_entry()
    ), f"настройка модуля `{name}` не видна в снимке"


def test_the_snapshot_says_what_the_robot_did_with_those_numbers() -> None:
    """Снимок прогона несёт не только значения полей, но и **правило словами**.

    Список значений отвечает на вопрос «чем гнали» наполовину: «период 15,
    порог 0» не говорит, что робот с ними делал. Разбирают прогон через месяц
    и разбирают именно правило.

    Мутация, обязанная ронять проверку: убрать сборку правила
    из `app/runs.py::settings_text`.
    """
    text = settings_text(
        EngineSettings(), EmaReverseSettings(), algorithm=registry.default_entry()
    )
    assert "Правило робота словами:" in text, "снимок не называет правило вовсе"
    assert "Закрытие выше EMA(15) — робот хочет быть в лонге." in text, (
        "правило записано без утверждений — читать в нём нечего"
    )
    assert "EMA(20)" not in text, "в снимке чужие числа"


def test_the_rule_in_the_snapshot_follows_the_settings() -> None:
    """Правило в снимке пересчитывается, а не написано один раз навсегда.

    Записанное однажды и не следящее за настройками, оно было бы хуже
    отсутствующего: разбор прогона опирался бы на правило чужого прогона.
    """
    engine = EngineSettings()
    twenty = settings_text(
        engine, EmaReverseSettings(period=20), algorithm=registry.default_entry()
    )
    assert "Закрытие выше EMA(20) — робот хочет быть в лонге." in twenty
    assert "EMA(15)" not in twenty


def test_the_rule_does_not_leak_into_the_marks_of_the_snapshot() -> None:
    """Абзацы правила — не поля снимка, и читатель их полем не считает.

    `snapshot_marks` считает подписью поля всё, что начинается с отступа
    и содержит «: ». Отступ в абзаце описания превратил бы предложение
    в поле — и сверка набора с прогоном (`_made_with`) искала бы это «поле»
    в чужих записях, то есть перестала бы узнавать свои прогоны.
    """
    text = settings_text(
        EngineSettings(), EmaReverseSettings(), algorithm=registry.default_entry()
    )
    marks = snapshot_marks(text)
    assert set(marks) == (
        {title for title in marks if not title.startswith("•")}
    ), "строка описания попала в подписи полей снимка"
    assert len(marks) == len(fields(EngineSettings)) + len(
        fields(EmaReverseSettings)
    ), (
        "число подписей снимка изменилось: описание правила перестало быть "
        f"текстом и стало полем. Подписи: {sorted(marks)}"
    )


def test_the_costs_the_money_was_counted_on_are_written_next_to_it() -> None:
    """Стережёт: издержки записаны вместе с итогом, а не подразумеваются.

    Проскальзывание живёт только в `backtest.Costs`, настройками движка его
    не восстановить. Два прогона с разными издержками дают ОДИН И ТОТ ЖЕ
    список сделок и разные деньги — различить их по сделкам нельзя.
    """
    from backtest import HistoryRun

    quiet = result_note(HistoryRun(costs=Costs(commission_per_side=14.0)))
    slipping = result_note(HistoryRun(
        costs=Costs(commission_per_side=14.0, price_step=1.0, slippage_steps=2.0)
    ))
    assert "Издержки прогона" in quiet
    assert quiet != slipping, "проскальзывание не попало в запись итога"


# ------------------------------------------------------ два исхода прогона

def _run_log(worker: MarketWorker, **kwargs) -> tuple[RunLog, list[tuple[str, str]]]:
    said: list[tuple[str, str]] = []
    log = RunLog(
        worker,
        warn=lambda event, reason: said.append((event, reason)),
        note=lambda event, reason: said.append((event, reason)),
        **kwargs,
    )
    return log, said


def test_a_failed_run_is_closed_with_the_reason(loop, database) -> None:
    """Стережёт: отказ посреди прогона оставляет запись с причиной, а не пустую."""

    async def go():
        worker = MarketWorker(database)
        await worker.open()
        log, _ = _run_log(worker)
        try:
            with pytest.raises(ZeroDivisionError):
                async with log.around(SessionRecord(RunOrigin.BACKTEST)):
                    raise ZeroDivisionError("свеча без цены")
        finally:
            await worker.close()

    loop.run_until_complete(go())

    (session,) = _sessions(database)
    assert session.finished_at is not None, "отказавший прогон остался незакрытым"
    assert "Прогон не удался" in session.finish_note
    assert "свеча без цены" in session.finish_note


def test_a_cancelled_run_stays_open_and_reads_as_interrupted(loop, database) -> None:
    """Стережёт: снятый прогон НЕ закрывается — иначе он лгал бы о штатном конце.

    Снимают прогон при выходе из программы, когда хранилище закрывается
    следом. Пустое время конца — честная запись «закрыли на середине»,
    и `--runs` печатает её словами.
    """

    async def go():
        worker = MarketWorker(database)
        await worker.open()
        log, _ = _run_log(worker)
        try:
            with pytest.raises(asyncio.CancelledError):
                async with log.around(SessionRecord(RunOrigin.BACKTEST)):
                    raise asyncio.CancelledError
        finally:
            await worker.close()

    loop.run_until_complete(go())

    (session,) = _sessions(database)
    assert session.finished_at is None, "снятый прогон записан как законченный"
    assert session.finish_note == ""


def test_a_token_in_the_failure_reason_never_reaches_the_record(loop, database) -> None:
    """Стережёт правило 7: текст отказа идёт в базу через чистку, а не как есть.

    Путь настоящий: причину отказа `RunLog` берёт из текста исключения,
    а на боевом ходу туда придёт тело ответа сервера брокера. Строка
    в журнале прогонов — такой же выход наружу, как выгрузка и скриншот.
    """
    leak = (
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.c2lnbmF0dXJlLXZhbHVl"
    )

    async def go():
        worker = MarketWorker(database)
        await worker.open()
        log, _ = _run_log(worker)
        try:
            with pytest.raises(RuntimeError):
                async with log.around(SessionRecord(RunOrigin.BACKTEST)):
                    raise RuntimeError(f"брокер отверг: Bearer {leak}")
        finally:
            await worker.close()

    loop.run_until_complete(go())

    (session,) = _sessions(database)
    assert leak not in session.finish_note, "токен уехал в журнал прогонов"
    assert SECRET_MASK in session.finish_note, "чистка сработала молча и без следа"
    assert "брокер отверг" in session.finish_note, "вместе с токеном стёрлась причина"


class _BrokenJournal(MarketWorker):
    """Хранилище, у которого журнал прогонов не работает, а свечи — работают."""

    async def open_journal_session(self, run, **kwargs):
        raise RuntimeError("база занята другой программой")


def test_a_broken_journal_does_not_break_the_run_and_does_not_keep_quiet(
    loop, database
) -> None:
    """Стережёт: отказ записи стоит записи, а не графика — и говорит вслух.

    Владелец счёта открыл программу ради картинки; ронять её из-за журнала
    нельзя. Молчать про пропавшую запись — тоже: он узнал бы об этом
    в тот день, когда пришёл за ответом «что я запускал».
    """
    port, journal = _passes(loop, database, Settings(), worker_class=_BrokenJournal)

    assert port._run is not None, "прогон не состоялся из-за журнала"  # noqa: SLF001
    assert port._run.summary.trades > 0, "сделок нет — проверять было бы нечего"  # noqa: SLF001
    assert _sessions(database) == [], "журнал был сломан, а запись появилась"
    assert any("Прогон не записан" in line for line in journal), (
        "запись не удалась молча: в журнале решений об этом ни строки"
    )


# ------------------------------------------------------------------ чистка

def test_old_runs_are_pruned_by_the_rule_and_the_removal_is_said_out_loud(
    loop, database
) -> None:
    """Стережёт: хранятся последние N прогонов, и выброшенные названы вслух.

    Молча выброшенная запись читается как никогда не сделанный прогон —
    то есть возвращает ровно ту слепоту, ради которой всё это заводится.
    """

    async def go():
        worker = MarketWorker(database)
        await worker.open()
        log, said = _run_log(worker, keep=3)
        try:
            for number in range(5):
                async with log.around(
                    SessionRecord(RunOrigin.BACKTEST, note=f"прогон {number}")
                ) as entry:
                    entry.note = f"итог {number}"
        finally:
            await worker.close()
        return said

    said = loop.run_until_complete(go())

    sessions = _sessions(database)
    assert len(sessions) == 3, f"осталось {len(sessions)} прогонов вместо трёх"
    assert [session.note for session in sessions] == [
        "прогон 4", "прогон 3", "прогон 2",
    ], "убраны не самые старые прогоны"
    assert any("Старые прогоны убраны" in event for event, _ in said), (
        "прогоны выброшены молча"
    )


def test_the_live_journal_is_never_pruned(loop, database) -> None:
    """Стережёт границу чистки: боевой прогон не удаляется вовсе.

    Он неповторим — повторить его нечем, свечей того рынка в базе может
    и не остаться (`market.RunOrigin.evidence`). Здесь проверяется, что
    прогоны по истории его не вытесняют.
    """

    async def go():
        worker = MarketWorker(database)
        await worker.open()
        log, _ = _run_log(worker, keep=2)
        try:
            await worker.open_journal_session(
                SessionRecord(RunOrigin.LIVE, note="боевой день"),
                keep_backtest_sessions=2,
            )
            for number in range(4):
                async with log.around(
                    SessionRecord(RunOrigin.BACKTEST, note=f"прогон {number}")
                ):
                    pass
        finally:
            await worker.close()

    loop.run_until_complete(go())

    live = [s for s in _sessions(database) if s.origin is RunOrigin.LIVE]
    assert len(live) == 1, "боевой прогон вычистили вместе с прогонами по истории"


# ------------------------------------------------------- что видит человек

def test_the_runs_key_prints_the_conditions_next_to_the_result(loop, database) -> None:
    """Стережёт ответ на вопрос «что я запускал»: условия и итог в одной выдаче.

    Число без условий — ровно то, против чего написано правило проекта
    про подгонку. В выдаче обязаны стоять рядом настройки, отрезок и деньги.
    """
    _passes(loop, database, Settings())

    out = io.StringIO()
    assert show_runs(database, limit=RUNS_SHOWN, out=out) == 0
    text = out.getvalue()
    assert "Прогон 1 — прогон по истории" in text
    assert "Тейк-профит, %: 0,5" in text, "настроек в выдаче нет"
    assert "Период средней: 15" in text, "настроек торгового модуля в выдаче нет"
    assert "MXU6, 5 минут: свечей" in text, "отрезка в выдаче нет"
    assert "Сделок" in text and "чистая" in text, "итога в выдаче нет"


def test_the_runs_key_tells_an_empty_base_from_a_missing_one(tmp_path) -> None:
    """Стережёт разницу двух пустот: «прогонов нет» и «базы нет» лечат по-разному."""
    missing = tmp_path / "нет-такого.sqlite3"
    out = io.StringIO()
    assert show_runs(missing, out=out) == 1
    assert "Базы свечей нет" in out.getvalue()

    empty = tmp_path / "пустая.sqlite3"
    with CandleStore(empty):
        pass
    out = io.StringIO()
    assert show_runs(empty, out=out) == 1
    assert "нет ни одного записанного прогона" in out.getvalue()


def test_an_unfinished_run_is_printed_as_interrupted(loop, database) -> None:
    """Стережёт: незакрытый прогон в выдаче назван словами, а не пустым местом."""

    async def go():
        worker = MarketWorker(database)
        await worker.open()
        try:
            await worker.open_journal_session(SessionRecord(RunOrigin.BACKTEST))
        finally:
            await worker.close()

    loop.run_until_complete(go())

    out = io.StringIO()
    show_runs(database, out=out)
    assert "не закрыт — программу закрыли на середине прогона" in out.getvalue()


def test_the_runs_key_answers_on_a_machine_where_qt_cannot_start(database) -> None:
    """Стережёт: `--runs` отвечает, ни разу не тронув Qt.

    Проверка отдельным процессом и негодным плагином окна: тронь ветка Qt —
    и `QApplication` не поднимется вовсе, процесс кончится чужой ошибкой
    про платформенный плагин. В самом прогоне тестов приложение Qt уже одно
    на сессию, и внутри процесса это не проверяется ничем (тот же довод,
    что в шапке `tests/test_app_main.py`).
    """
    with CandleStore(database) as store:
        session = store.open_journal_session(
            SessionRecord(RunOrigin.BACKTEST, symbol="MXU6", settings="Настройки движка")
        )
        store.finish_journal_session(session.id, note="Сделок 3")

    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(pathlib.Path(__file__).resolve().parent.parent)
    environment["QT_QPA_PLATFORM"] = "такого-плагина-нет"
    done = subprocess.run(
        [sys.executable, "-m", "app.main", "--runs", "5", "--db", str(database)],
        cwd=environment["PYTHONPATH"], env=environment, capture_output=True,
        text=True, timeout=180, check=False,
    )
    assert done.returncode == 0, f"ключ не отработал: {done.stderr}"
    assert "Прогон" in done.stdout and "Настройки движка" in done.stdout
    assert "platform plugin" not in done.stderr, "ветка дошла до Qt"


def test_the_runs_key_refuses_a_zero_count(database) -> None:
    """Стережёт: `--runs 0` — отказ с фразой, а не пустая или бесконечная выдача.

    `LIMIT 0` в SQLite вернул бы пустой список, `LIMIT -1` — весь журнал:
    два разных молчаливых ответа на одну опечатку.
    """
    from app import main as entry

    with pytest.raises(SystemExit):
        entry.main(["--runs", "0", "--db", str(database)])


# ------------------------------------------------- происхождение по дороге

def test_the_window_origin_reaches_the_database_unchanged() -> None:
    """Стережёт перевод происхождения: боевой прогон не запишется как прогон.

    Два вида окна, сведённые к одному виду базы, стёрли бы разницу между
    настоящими деньгами и историей — то, ради чего написано решение 0011.
    """
    seen = {convert.stored_origin(origin) for origin in WindowOrigin}
    assert len(seen) == len(list(WindowOrigin)), "перевод склеил два происхождения"
    for origin in WindowOrigin:
        assert convert.origin_of_stored(convert.stored_origin(origin)) is origin


def test_the_conditions_record_names_the_selection_and_the_resolved_edges() -> None:
    """Стережёт: в записи и разрешённые границы, и ключи, которыми их выбрали.

    `--days 30` считается от последней свечи В БАЗЕ, и завтра тот же ключ
    означает другой период. Повторить прогон позволяют границы; ключи
    объясняют, откуда они взялись.
    """
    bars = _bars()
    record = _conditions(days=30).record(bars)
    assert f"{bars[0].close_time:%d.%m.%Y %H:%M}" in record.note
    assert f"{bars[-1].close_time:%d.%m.%Y %H:%M}" in record.note
    assert "последние дни, штук 30" in record.note

    whole = _conditions(days=0).record(bars)
    assert "вся история в базе" in whole.note

    edge = datetime(2026, 6, 19, 11, 0, tzinfo=MSK)
    limited = _conditions(days=0, until=edge).record(bars)
    assert "19.06.2026 11:00" in limited.note


def test_a_field_of_the_record_is_never_left_empty() -> None:
    """Стережёт: у объявления прогона заполнены все колонки, кроме заметки о конце.

    Пустая колонка в базе выглядит как «не было такого», а не как «забыли
    записать», и отличить одно от другого потом нечем.
    """
    record = _conditions().record(_bars())
    for field in dataclasses.fields(SessionRecord):
        value = getattr(record, field.name)
        assert value not in ("", None), f"колонка `{field.name}` осталась пустой"


# ---------------------------------------------------------------------------
# Выбранный алгоритм в снимке прогона
# ---------------------------------------------------------------------------


def test_the_snapshot_names_the_algorithm_that_was_chosen() -> None:
    """Снимок называет **выбранный** алгоритм, а не тот, который был первым.

    Через месяц разбирают по снимку, чем гнали прогон. Название, вписанное
    константой на месте вызова, осталось бы прежним после смены алгоритма —
    и снимок называл бы не то, чем считали.

    Мутация, обязанная ронять проверку: вернуть в `snapshot_of` константу
    вместо `convert.strategy_title(values)`.
    """
    text = runs.snapshot_of(Settings())
    assert f"Торговый алгоритм: {registry.default_entry().title}" in text, (
        "снимок не называет выбранный алгоритм"
    )


def test_the_snapshot_title_comes_from_the_registry_by_the_chosen_name() -> None:
    """Название берётся у реестра по имени из настроек, а не пишется в `app/`.

    ⚠️ Незнакомое имя показывается как есть — и это не мягкость: снимок пишут
    в базу, и запись, упавшая из-за незнакомого имени, унесла бы с собой
    условия прогона целиком.
    """
    assert convert.strategy_title(Settings()) == registry.default_entry().title
    assert convert.strategy_title(
        Settings().replace(strategy_id="atr_channel")
    ) == "atr_channel"
