"""Сборка программы: окно ↔ прогон по истории через `TerminalPort`.

Это тесты **шва**, а не торговой логики. Проверяется ровно то, что делает
`app/`: свечи доехали из базы в окно, сделки движка доехали в журнал, отказ
показан фразой, а не трассировкой, и окно во время прогона остаётся живым.

Ни один тест здесь не утверждает, что робот принял верное решение: это
проверяют тесты `engine/` и сверка с прототипом. Здесь проверяется, что
решение доехало и не подменилось по дороге.
"""

from __future__ import annotations

import asyncio
import dataclasses
import enum
import math
import os
import pathlib
from collections.abc import Callable
from dataclasses import fields, replace
from datetime import date, datetime, time, timedelta, timezone

import pytest
from PySide6.QtCore import Qt

os.environ.setdefault("QT_API", "pyside6")  # до первого импорта qasync

from backtest import BREATHE_EVERY, Deal, HistoryRun
from engine import ExitReason, Side
from market import (
    MSK,
    Candle,
    CandleStore,
    MarketWorker,
    PointValue,
    Source,
    Timeframe,
)
from market.journal import SECRET_MASK, redact
from ui.models import (
    AfterTakeProfit,
    CalendarDay,
    Candle as WindowCandle,
    ChartData,
    Connection,
    DecisionLevel,
    HistoryLoadRequest,
    Layer,
    Mode,
    Settings,
)

from app.port import NO_GUARDS, HistoryPort

DAY = datetime(2026, 6, 19, 7, 0, tzinfo=MSK)  # пятница, до открытия окна


def _minutes(count: int = 300, start: datetime = DAY) -> list[Candle]:
    """Минутки с колебанием вокруг ровной цены.

    Колебание нужно, чтобы средняя пересекалась и робот принимал решения:
    ряд без движения даёт прогон без единой сделки, и проверять было бы нечего.
    Ни одно число здесь не подбиралось под результат — важно только то, что
    сделки в принципе есть.
    """
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


@pytest.fixture
def long_database(tmp_path: pathlib.Path) -> pathlib.Path:
    """База, которой хватает на **две передышки** прогона и больше.

    Размер считается от `BREATHE_EVERY`, а не записан числом: прогон отдаёт
    управление циклу событий раз в столько-то свечей, и выборка из шестидесяти
    свечей не доходит до первой передышки ни разу. Тест живости окна на такой
    выборке зелёный при любом устройстве прогона — в том числе при полностью
    синхронном, который окно и морозит.
    """
    path = tmp_path / "long-candles.sqlite3"
    with CandleStore(path) as store:
        store.put_minutes("MXU6", _minutes((BREATHE_EVERY * 2 + 20) * 5), Source.ISS)
    return path


@pytest.fixture
def two_day_database(tmp_path: pathlib.Path) -> pathlib.Path:
    """Два **соседних** торговых дня: четверг 18.06 и пятница 19.06.

    Соседних намеренно: на днях, разнесённых по календарю, ошибка на единицу
    в счёте дней не проявляется вовсе — между ними всё равно нет свечей.
    """
    path = tmp_path / "two-days.sqlite3"
    with CandleStore(path) as store:
        store.put_minutes("MXU6", _minutes(300, DAY - timedelta(days=1)), Source.ISS)
        store.put_minutes("MXU6", _minutes(300, DAY), Source.ISS)
    return path


@pytest.fixture
def database_with_tails(tmp_path: pathlib.Path) -> pathlib.Path:
    """Два дня, и каждый обрывается **не на границе бара**: 07:00…11:57.

    Так выглядит база, наполненная потоком: минутки кончаются там, где была
    последняя сделка, а не там, где кончился бар. Хвостовой бар каждого дня
    при этом объявляется незакрытым — и без объявленной границы полноты
    молча выпадает из прогона.
    """
    path = tmp_path / "tails.sqlite3"
    with CandleStore(path) as store:
        store.put_minutes("MXU6", _minutes(298, DAY), Source.ISS)
        store.put_minutes("MXU6", _minutes(298, DAY + timedelta(days=3)), Source.ISS)
    return path


class Recorded:
    """Всё, что порт отправил в окно."""

    def __init__(self, port: HistoryPort) -> None:
        self.charts: list[ChartData] = []
        self.states: list[object] = []
        self.trades: list[tuple] = []
        self.decisions: list[tuple] = []
        self.failures: list[str] = []
        self.busy: list[tuple[bool, str]] = []
        self.progress: list[tuple[int, str]] = []
        self.settings: list[Settings] = []
        #: Правило робота словами — каждое, что порт отправил в окно.
        self.rules: list[str] = []
        #: Строки журнала по одной, как они появлялись. Нужны там, где до
        #: полной публикации журнала дело не доходит: `decisions_replaced`
        #: отправляется в конце прохода, а строка об изменении настройки
        #: пишется до него.
        self.appended: list[object] = []
        #: Свечи, доехавшие мимо полного снимка: живая свеча приходит
        #: добавлением, а не прогоном (`HistoryPort.live_candle`), а растущая —
        #: на каждом снимке брокера (`HistoryPort.growing_minute`).
        self.grown: list[WindowCandle] = []
        self.retouched: list[WindowCandle] = []
        port.decision_appended.connect(self.appended.append)
        port.chart_replaced.connect(self.charts.append)
        port.candle_appended.connect(self.grown.append)
        port.candle_updated.connect(self.retouched.append)
        port.state_changed.connect(self.states.append)
        port.trades_replaced.connect(lambda rows, s: self.trades.append((rows, s)))
        port.decisions_replaced.connect(self.decisions.append)
        port.failed.connect(self.failures.append)
        port.busy_changed.connect(lambda on, what: self.busy.append((on, what)))
        port.progress_changed.connect(lambda p, what: self.progress.append((p, what)))
        port.settings_applied.connect(self.settings.append)
        port.strategy_rule_changed.connect(self.rules.append)


async def _run(port: HistoryPort, worker: MarketWorker | None = None) -> None:
    port.announce(port._worker.path)  # noqa: SLF001 — то же делает app/main.py
    port.refresh("тест")
    await port.wait()


def replay_history(loop, database: pathlib.Path, **kwargs):
    """Открыть базу, прогнать историю, вернуть порт и его записи."""

    async def go():
        worker = MarketWorker(database)
        if database.exists():
            await worker.open()
        port = HistoryPort(worker, sanitize=redact, **kwargs)
        recorded = Recorded(port)
        try:
            await _run(port)
        finally:
            await port.aclose()
            await worker.close()
        return port, recorded

    return loop.run_until_complete(go())


# --------------------------------------------------------------------- норма

def test_candles_and_deals_reach_the_window(loop, database) -> None:
    port, recorded = replay_history(loop, database, values=Settings(), days=0)

    chart = recorded.charts[-1]
    assert chart.instrument == "MXU6"
    assert len(chart.candles) > 50, "свечи из базы не доехали"
    assert chart.average, "линия средней пуста — торговый модуль её не отдал"
    assert chart.average_label.startswith("EMA(15)")

    rows, summary = recorded.trades[-1]
    assert rows, "движок не сделал ни одной сделки — проверять нечего"
    assert summary is not None and summary.trades == len(rows)
    assert recorded.decisions[-1], "журнал решений пуст"


def test_candles_are_stamped_with_the_start_of_their_interval(loop, database) -> None:
    """Ось графика размечена **открытиями**, как в терминале брокера.

    B-008, 04.09.2026: в график уходило время закрытия, и вся картинка вместе
    с метками сделок стояла на бар правее, чем у брокера. Прежняя редакция
    этой проверки требовала обратного и держала ошибку зелёной.

    Ещё раньше здесь стояло сравнение свечи самой с собой
    (`closes_at > closes_at - 5 минут`) — утверждение, которое не может быть
    ложным ни при какой ошибке перевода.
    """
    _, recorded = replay_history(loop, database, values=Settings(), days=0)
    chart_candles = recorded.charts[-1].candles
    # Первая пятиминутка ряда занимает 07:00–07:05 — на оси она стоит в 07:00.
    assert chart_candles[0].opens_at == DAY, (
        "первая свеча помечена не началом интервала: весь график и все метки "
        f"сдвинуты на бар ({chart_candles[0].opens_at})"
    )
    assert all(
        b.opens_at - a.opens_at == timedelta(minutes=5)
        for a, b in zip(chart_candles, chart_candles[1:])
    ), "шаг оси не равен размеру свечи"


def test_the_average_on_the_chart_is_the_strategy_average(loop, database) -> None:
    """Линия на экране — **та же**, что считает стратегия. Точка в точку.

    Это единственное место, где требование проверяется числами. Держится оно
    на одной строке в `backtest/history.py` (значение берётся у самого
    торгового модуля), а `app/` обязан его только переложить. Вторая
    реализация средней в `convert.average_line` — хоть циклом на голом
    Python, без единого импорта, — прошла бы все остальные проверки слоя:
    они смотрят лишь на то, что линия непуста, и на её подпись. Расхождение
    после этого выглядело бы как «на экране одна линия, а в решениях другая»,
    и искать его пошли бы в стратегию.
    """
    port, recorded = replay_history(loop, database, values=Settings(), days=0)
    run = port._run  # noqa: SLF001 — результат прогона, из которого построен кадр
    assert run is not None and run.average, "прогон не отдал среднюю"
    line = recorded.charts[-1].average
    assert len(line) == len(run.average), (
        f"на графике {len(line)} точек средней, у торгового модуля "
        f"{len(run.average)} — линия построена не из его значений"
    )
    on_axis = {candle.opens_at for candle in recorded.charts[-1].candles}
    for point, (moment, expected_value) in zip(line, run.average, strict=True):
        # Торговый модуль штампует значение закрытием свечи, ось размечена
        # открытиями. Точка обязана встать на отметку своей свечи, а не между
        # свечами и не на соседнюю: тогда линия средней сползает на бар,
        # и на глаз это выглядит как опережение сигнала.
        assert point.time == moment - timedelta(minutes=5), (
            "точка средней уехала по времени"
        )
        assert point.time in on_axis, (
            f"точка средней {point.time} не стоит ни на одной свече графика"
        )
        # Равенство точное. «Почти совпало» здесь означает вторую реализацию.
        assert point.value == expected_value, (
            f"значение средней на графике {point.value} против {expected_value} "
            "у торгового модуля"
        )


def test_the_markers_come_in_two_layers(loop, database) -> None:
    """Прогноз и факт — оба слоя, и они не совпадают по цене.

    Совпадение цены означало бы, что слой прогноза построен из тех же чисел,
    что и факт, то есть не показывает ничего (DOMAIN.md §8).
    """
    _, recorded = replay_history(loop, database, values=Settings(), days=0)
    chart = recorded.charts[-1]
    fact = [m for m in chart.markers if m.layer is Layer.FACT]
    plan = [m for m in chart.markers if m.layer is Layer.PLAN]
    assert fact and plan
    assert len(fact) == len(plan)
    paired = {m.pair_id for m in fact} & {m.pair_id for m in plan}
    assert len(paired) == len(fact), "метки не связаны именем заявки"
    assert any(
        f.price != p.price
        for f in fact for p in plan if f.pair_id == p.pair_id
    ), "расчёт и факт совпали во всех парах — слой прогноза ничего не показывает"
    assert all("Расчётная цена" in m.tooltip for m in fact)


def test_the_trade_paths_of_both_layers(loop, database) -> None:
    _, recorded = replay_history(loop, database, values=Settings(), days=0)
    paths = recorded.charts[-1].paths
    assert [p for p in paths if p.layer is Layer.FACT]
    assert [p for p in paths if p.layer is Layer.PLAN]


def test_time_outside_the_window_is_shaded(loop, database) -> None:
    _, recorded = replay_history(loop, database, values=Settings(), days=0)
    shades = recorded.charts[-1].shades
    assert shades, "затенения нет — не видно, где робот молчал"
    window_bounds = Settings().window_start, Settings().window_end
    for shade in shades:
        # Затенение не должно накрывать само торговое окно.
        assert not (shade.start.time() < window_bounds[0] < shade.end.time())


def test_the_state_is_always_simulation(loop, database) -> None:
    _, recorded = replay_history(loop, database, values=Settings(), days=0)
    state = recorded.states[-1]
    assert state.simulation is True, "боевой режим не подключён и объявляться не должен"
    assert state.running is False
    assert state.connection is Connection.UNKNOWN
    assert state.instrument == "MXU6"


@pytest.mark.slow
def test_progress_and_busy_are_shown(loop, long_database) -> None:
    """Длинная работа обязана быть видна: иначе окно выглядит зависшим.

    База нужна длинная. На шестидесяти свечах `_report` зовётся ровно один
    раз — в самом конце, — и «прогресс отдавался» означает «отдался сразу
    100 %». Полоса, появляющаяся заполненной в момент окончания работы,
    ничем не отличается от отсутствующей.
    """
    _, recorded = replay_history(loop, long_database, values=Settings(), days=0)
    assert recorded.busy[0][0] is True
    assert recorded.busy[-1][0] is False
    assert recorded.progress, "прогресс прогона не отдавался"
    in_between = [percent for percent, _ in recorded.progress if percent < 100]
    assert in_between, (
        "прогресс отдан один раз, и сразу 100 %: полоса появится заполненной "
        f"в момент окончания прогона. Все отчёты: {recorded.progress}"
    )
    assert recorded.progress[-1][0] == 100


# --------------------------------------------------------------------- отказы

def test_a_missing_database_is_said_in_plain_words(loop, tmp_path) -> None:
    """Отсутствие базы — не трассировка, а фраза, называющая файл."""
    missing = tmp_path / "no-such.sqlite3"
    _, recorded = replay_history(loop, missing, values=Settings(), days=0)
    assert recorded.failures, "программа промолчала о том, что базы нет"
    message = recorded.failures[-1]
    assert "no-such.sqlite3" in message
    assert "Traceback" not in message
    assert recorded.charts[-1].candles == ()


def test_a_missing_instrument_names_what_is_there(loop, database) -> None:
    _, recorded = replay_history(
        loop, database, values=Settings().replace(instrument="RIU6"), days=0
    )
    message = recorded.failures[-1]
    assert "RIU6" in message and "MXU6" in message


def test_start_refuses_with_an_explanation(loop, database) -> None:
    """Кнопка «Старт» не делает вид, что робот пошёл."""
    port, recorded = replay_history(loop, database, values=Settings(), days=0)
    port.start()
    assert recorded.failures, "«Старт» промолчал"
    assert "боевого хода" in recorded.failures[-1]


def test_bad_settings_are_not_applied(loop, database) -> None:
    """Незнакомый размер свечи — отказ, а не тихая подмена умолчанием."""
    port, recorded = replay_history(loop, database, values=Settings(), days=0)
    # Читается приватное поле намеренно: проверяется, что порт НЕ подменил
    # настройки. Публичного способа спросить «что сейчас применено» нет.
    before = port._values  # noqa: SLF001
    port.apply_settings(Settings().replace(timeframe="семь минут"))
    assert recorded.failures
    assert recorded.settings == [], "настройки объявлены применёнными, хотя отвергнуты"
    assert port._values is before  # noqa: SLF001 — та же приватная ссылка


def test_an_empty_instrument_is_not_applied(loop, database) -> None:
    port, recorded = replay_history(loop, database, values=Settings(), days=0)
    port.apply_settings(Settings().replace(instrument=""))
    assert recorded.failures
    assert recorded.settings == []


def test_a_bad_settings_value_does_not_crash_the_window(loop, database) -> None:
    """Проверки настроек движка и модуля бросают не `SettingsRefused`.

    `apply_settings` — публичная команда порта, и зовут её из слота Qt.
    Непойманное `ValueError` там означает трассировку в консоль и молчащее
    окно: нажатие «Применить» просто ничего не делает.
    """
    port, recorded = replay_history(loop, database, values=Settings(), days=0)
    before = port._values  # noqa: SLF001 — см. тест выше: публичного способа нет
    port.apply_settings(Settings().replace(average_period=0))
    assert recorded.failures, "негодное значение принято молча"
    assert "Traceback" not in recorded.failures[-1]
    assert "период средней" in recorded.failures[-1]
    assert port._values is before, "настройки применились вопреки отказу"  # noqa: SLF001
    assert recorded.settings == []


# ------------------------------------------------------------------ пересчёт

def test_changing_the_mode_changes_the_deals(loop, database) -> None:
    """Режим меняет решения робота, а не оформление: прогон гоняется заново."""

    async def go():
        worker = MarketWorker(database)
        await worker.open()
        port = HistoryPort(worker, values=Settings(), days=0, sanitize=redact)
        recorded = Recorded(port)
        try:
            await _run(port)
            before = len(recorded.trades[-1][0])
            port.set_mode(Mode.OFF)
            await port.wait()
            after = len(recorded.trades[-1][0])
        finally:
            await port.aclose()
            await worker.close()
        return before, after

    before, after = loop.run_until_complete(go())
    assert before > 0
    assert after == 0, "в режиме «Выключен» сделок быть не может"


def test_the_new_rule_in_words_reaches_the_journal(loop, database) -> None:
    """Сменили период — в журнале и число, и **правило целиком** новыми словами.

    Строка «Период средней: 15 → 20» говорит, что поменяли, и не говорит,
    каким стало правило. Разбирают через месяц именно правило — «почему
    в тот день робот повёл себя иначе», — а восстанавливать его из чисел
    по памяти некому.

    Мутация, обязанная ронять проверку: убрать `changes.append(...)`
    с `rule_headline_of` в `HistoryPort.apply_settings`.
    """

    async def go():
        worker = MarketWorker(database)
        await worker.open()
        port = HistoryPort(worker, values=Settings(), days=0, sanitize=redact)
        recorded = Recorded(port)
        try:
            await _run(port)
            port.apply_settings(Settings().replace(average_period=20))
            await port.wait()
        finally:
            await port.aclose()
            await worker.close()
        return recorded

    recorded = loop.run_until_complete(go())
    said = [
        row.reason
        for row in recorded.appended
        if getattr(row, "event", "") == "Настройки изменены"
    ]
    assert said, "изменение настроек не попало в журнал"
    told = said[-1]
    assert "Период средней: 15 → 20" in told, "прежнего и нового значения нет"
    assert "Правило теперь читается так" in told, (
        "журнал назвал изменившееся число и не назвал получившееся правило"
    )
    assert "EMA(20)" in told, "правило в журнале осталось с прежним периодом"
    assert "EMA(15)" not in told.split("Правило теперь читается так")[1], (
        "в новом правиле стоит прежняя средняя"
    )


def test_a_change_that_does_not_touch_the_module_says_nothing_about_the_rule(
    loop, database
) -> None:
    """Объём поменяли — правило не изменилось, и строки про него нет.

    Строка про правило на **каждое** изменение настроек превратила бы журнал
    в стену повторов, и читать его перестали бы целиком.
    """

    async def go():
        worker = MarketWorker(database)
        await worker.open()
        port = HistoryPort(worker, values=Settings(), days=0, sanitize=redact)
        recorded = Recorded(port)
        try:
            await _run(port)
            port.apply_settings(Settings().replace(volume=2))
            await port.wait()
        finally:
            await port.aclose()
            await worker.close()
        return recorded

    recorded = loop.run_until_complete(go())
    said = [
        row.reason
        for row in recorded.appended
        if getattr(row, "event", "") == "Настройки изменены"
    ]
    assert said, "изменение настроек не попало в журнал"
    assert "Правило теперь читается так" not in said[-1]


def test_the_rule_in_words_reaches_the_window(loop, database) -> None:
    """Правило словами уезжает в окно вместе с настройками, и с новыми числами.

    Окно посчитать его не может: `ui/` не импортирует торговые слои
    (ARCHITECTURE.md §2). Значит текст обязан приехать готовым — и приехать
    **и по запросу настроек, и после их применения**: окно, спросившее
    настройки при открытии, иначе показывало бы поля без объяснения,
    что робот с ними делает.
    """

    async def go():
        worker = MarketWorker(database)
        await worker.open()
        port = HistoryPort(worker, values=Settings(), days=0, sanitize=redact)
        recorded = Recorded(port)
        try:
            await _run(port)
            port.request_settings()
            asked = list(recorded.rules)
            port.apply_settings(Settings().replace(average_period=20))
            await port.wait()
        finally:
            await port.aclose()
            await worker.close()
        return asked, recorded

    asked, recorded = loop.run_until_complete(go())
    assert asked, "на запрос настроек правило словами не пришло"
    assert "EMA(15)" in asked[-1], "правило пришло без нынешних чисел"
    assert recorded.rules[-1] != asked[-1], "после «Применить» правило не обновилось"
    assert "EMA(20)" in recorded.rules[-1], "в правиле остался прежний период"
    assert "робот хочет быть в лонге" in recorded.rules[-1], (
        "правило приехало без утверждений — показывать человеку нечего"
    )


def test_a_settings_change_writes_a_line_in_the_journal(loop, database) -> None:
    """Список изменений даёт движок, а не окно и не порт."""

    async def go():
        worker = MarketWorker(database)
        await worker.open()
        port = HistoryPort(worker, values=Settings(), days=0, sanitize=redact)
        recorded = Recorded(port)
        try:
            await _run(port)
            port.apply_settings(Settings().replace(volume=2))
            await port.wait()
        finally:
            await port.aclose()
            await worker.close()
        return recorded

    recorded = go and loop.run_until_complete(go())
    journal_rows = [row for row in recorded.decisions[-1] if row.event == "Настройки изменены"]
    assert journal_rows, "изменение настроек не попало в журнал"
    assert "объём" in journal_rows[-1].reason.lower()
    assert recorded.settings, "окно не получило подтверждения настроек"


#: Поля, у которых годное «другое значение» задаётся не типом, а смыслом.
#:
#: ⚠️ Третий вариант поведения после тейка («сразу восстановить позицию»)
#: движком не выражен и отвергается вслух — это отдельная проверка. Здесь
#: нужен выразимый.
_ANOTHER_VALUE = {
    "instrument": "RIU6",
    "timeframe": "15 минут",
    "after_take_profit": AfterTakeProfit.WAIT_FOR_SIGNAL,
}


def _something_else(field: str, expected_value):
    """Другое значение того же поля — чем угодно, лишь бы отличалось.

    Значения не подбираются под ожидаемый текст: тест проверяет, что запись
    об изменении вообще появилась, а не как она сформулирована.
    """
    if field in _ANOTHER_VALUE:
        return _ANOTHER_VALUE[field]
    return _another_of_the_same_type(field, expected_value)


def _another_of_the_same_type(field: str, expected_value):
    """Другое значение по типу: булево наоборот, число больше, час позже."""
    if isinstance(expected_value, bool):
        return not expected_value
    if isinstance(expected_value, str):
        # Строковые поля без своего словаря значений: каталог технического
        # журнала. Пустая строка означает «умолчание», поэтому отличающееся
        # значение — любой путь.
        return f"{expected_value}/иначе".lstrip("/")
    if isinstance(expected_value, enum.Enum):
        return next(member for member in type(expected_value) if member is not expected_value)
    if isinstance(expected_value, time):
        return time((expected_value.hour + 1) % 24, expected_value.minute)
    if isinstance(expected_value, int):
        return expected_value + 1
    if isinstance(expected_value, float):
        return expected_value + 0.25
    if isinstance(expected_value, tuple):
        # Календарь нерабочих дней: пустой набор → один помеченный день.
        # Дата постоянная намеренно: «другое значение» обязано быть одним и тем
        # же от прогона к прогону, иначе упавший тест не воспроизводится.
        return (CalendarDay(day=date(2026, 6, 12), trading=False),)
    if expected_value is None:
        return 14.0  # тариф комиссии: `None` — «не задан», это не ноль
    raise AssertionError(f"тест не знает, как изменить поле {field}")


def _apply(loop, database: pathlib.Path, updated: Settings) -> Recorded:
    """Применить настройки и вернуть записи. Прогон не гоняется.

    Строка журнала пишется до постановки прогона в очередь, поэтому ждать
    прогон незачем: задача снимается `aclose()` не начавшись.
    """

    async def go():
        worker = MarketWorker(database)
        port = HistoryPort(worker, values=Settings(), days=0, sanitize=redact)
        recorded = Recorded(port)
        port.apply_settings(updated)
        await port.aclose()
        await worker.close()
        return recorded

    return loop.run_until_complete(go())


@pytest.mark.parametrize("field", [field.name for field in fields(Settings)])
def test_a_change_of_any_settings_field_reaches_the_journal(loop, database, field) -> None:
    """ТЗ §4.4 А: изменение настройки — строка с прежним и новым значением.

    Проверяются **все** поля окна, а не одно. Список изменений раньше
    собирался только у движка и торгового модуля, а пяти полей окна нет ни
    в одном из них: инструмент, размер свечи и три поля предохранителей.
    Их изменение давало в журнал «Значения совпали с прежними» — после чего
    программа грузила другой инструмент. Тест на объёме этого не ловил:
    у объёма строка есть.

    Новое поле в `Settings` без источника строки тоже уронит этот тест —
    список полей берётся у самого класса, а не записан здесь от руки.
    """
    before = Settings()
    recorded = _apply(loop, database, before.replace(**{field: _something_else(field, getattr(before, field))}))
    reasons = [entry.reason for entry in recorded.appended]
    assert "Значения совпали с прежними" not in reasons, (
        f"поле «{field}» изменено, а в журнале написано, что всё осталось "
        f"как было. Строки журнала: {reasons}"
    )
    assert any("→" in explanation for explanation in reasons), (
        f"изменение поля «{field}» не попало в журнал ни одной строкой "
        f"с прежним и новым значением: {reasons}"
    )


@pytest.mark.parametrize(
    "field", ["volume_cap", "daily_loss_limit_pct", "free_funds_reserve_pct"]
)
def test_changing_a_guard_says_what_it_does_and_what_it_does_not(
    loop, database, field
) -> None:
    """Стережёт: число предохранителя принято не молча, а с оговоркой.

    Тихое «принято» читается как «ограничение поставлено». Само по себе
    число не ограничивает ничего: предохранитель работает от своей галочки,
    и все три галочки сняты умолчанием. Два из трёх вдобавок считаются
    от размера счёта, которого программа не знает.

    ⚠️ Прежняя редакция этого теста утверждала обратное — «в движке нет
    ни одного из трёх предохранителей». Это перестало быть правдой
    05.09.2026, когда их провели из окна (`D-030`), а текст остался
    и врал владельцу счёта при каждом старте (`D-037`).
    """
    before = Settings()
    recorded = _apply(loop, database, before.replace(**{field: _something_else(field, getattr(before, field))}))
    warnings_seen = [
        entry for entry in recorded.appended
        if entry.level is DecisionLevel.WARNING and NO_GUARDS in entry.reason
    ]
    assert warnings_seen, (
        f"поле «{field}» принято молча — владелец счёта вправе решить, что "
        "ограничение поставлено. Строки журнала: "
        f"{[(seen.event, seen.level) for seen in recorded.appended]}"
    )
    assert "→" in warnings_seen[-1].reason


def test_the_journal_opens_with_the_version_line(loop, database) -> None:
    """Версия программы пишется в журнал при каждом старте (ТЗ, DESKTOP §7)."""
    _, recorded = replay_history(loop, database, values=Settings(), days=0)
    journal_rows = recorded.decisions[-1]
    start_lines = [row for row in journal_rows if row.event == "Программа запущена"]
    assert start_lines and "версия" in start_lines[0].reason.lower()
    guard_lines = [row for row in journal_rows if row.event == "Предохранители по деньгам"]
    assert guard_lines and guard_lines[0].level is DecisionLevel.WARNING


# -------------------------------------------------------------- живость окна

def test_the_window_stays_alive_during_a_run(loop, long_database) -> None:
    """Прогон отдаёт управление циклу событий, а не держит его до конца.

    Проверяется числом, и число считается **внутри самого прогона**, а не
    за всё время вызова. Прежний тест был вакуумным дважды:

    * база давала 60 свечей при передышке раз в 250 — то есть передышка
      не выполнялась ни разу, и убрать её из `HistorySource` целиком можно
      было, не уронив тест;
    * тики считались с начала `_run`, а до прогона стоит чтение свечей
      из базы (`await worker.bars`) — оно уходит в поток данных, и цикл
      событий получает управление именно там. Тики набирались до прогона.

    Тики считаются между **двумя отчётами о ходе прогона**: оба приходят
    изнутри `replay`, и всё, что между ними, — это работа самого прогона.
    Считать до и после вызова нельзя: `await port.wait()` в конце сам отдаёт
    управление циклу, и тики набегают там.
    """
    from PySide6.QtCore import QTimer

    ticks: list[int] = []
    ticks_at_report: list[int] = []

    async def go():
        worker = MarketWorker(long_database)
        await worker.open()
        port = HistoryPort(worker, values=Settings(), days=0, sanitize=redact)
        port.progress_changed.connect(lambda *_: ticks_at_report.append(len(ticks)))
        timer = QTimer()
        timer.setInterval(1)
        timer.timeout.connect(lambda: ticks.append(1))
        timer.start()
        try:
            await _run(port)
        finally:
            timer.stop()
            await port.aclose()
            await worker.close()

    loop.run_until_complete(go())
    assert len(ticks_at_report) >= 2, (
        "прогон отчитался о ходе работы один раз — значит управления циклу "
        "он не отдавал вовсе: единственный отчёт приходит в самом конце. "
        f"Отчёты: {ticks_at_report}"
    )
    assert ticks_at_report[-1] > ticks_at_report[0], (
        "между началом и концом прогона таймер Qt не тикнул ни разу: "
        f"{ticks_at_report}. Окно в это время не перерисовывается и не отвечает "
        "на мышь — владелец счёта читает это как «программа зависла»"
    )


def test_a_frame_never_mixes_a_run_with_fresher_settings(loop, long_database) -> None:
    """Настройки, изменённые ПОСРЕДИ прогона, не попадают в его кадр.

    Прогон идёт по старым свечам и старым настройкам, а `apply_settings`
    вызывается из слота Qt и переписывает поля порта немедленно. Пока `_show`
    читал поля порта, доезжающий проход показывал линию EMA(15) с подписью
    «EMA(20)» и затенение по НОВОМУ окну поверх сделок, сделанных в старом.
    Второй проход это исправляет, но кадр между ними — ровно то, против чего
    написан докстринг `app/convert.py`: на экране одна линия, а в решениях
    другая.

    Момент вмешательства выбран не наугад: `apply_settings` ставится
    в очередь цикла событий в момент, когда прогон уже начался, и исполняется
    на первой же его передышке.
    """
    # Отметка на оси у ПОСЛЕДНЕЙ молчавшей свечи перед окном, то есть её
    # открытие. Окно 10:05 — молчит свеча 10:00–10:05 (её закрытие 10:05
    # в окно не входит, неравенства строгие), на оси она стоит в 10:00.
    FIRST_WINDOW_SHADE_END = datetime(2026, 6, 19, 10, 0, tzinfo=MSK)
    SECOND_WINDOW_SHADE_END = datetime(2026, 6, 19, 11, 55, tzinfo=MSK)

    async def go():
        worker = MarketWorker(long_database)
        await worker.open()
        port = HistoryPort(worker, values=Settings(), days=0, sanitize=redact)
        recorded = Recorded(port)
        updated = Settings().replace(
            average_period=20, window_start=time(12, 0), window_end=time(13, 0)
        )
        interfered: list[int] = []

        def while_running(is_busy: bool, with_what: str) -> None:
            if is_busy and "Прогон" in with_what and not interfered:
                interfered.append(1)
                loop.call_soon(port.apply_settings, updated)

        port.busy_changed.connect(while_running)
        try:
            port.refresh("первый")
            await port.wait()
        finally:
            await port.aclose()
            await worker.close()
        return recorded

    recorded = loop.run_until_complete(go())
    assert len(recorded.charts) == 2, (
        f"проходов должно быть два: старый и пересчитанный, а было "
        f"{len(recorded.charts)}"
    )
    first_frame, second_frame = recorded.charts[0], recorded.charts[-1]
    assert first_frame.average_label.startswith("EMA(15)"), (
        f"кадр прогона подписан чужой средней: {first_frame.average_label!r}"
    )
    assert second_frame.average_label.startswith("EMA(20)")
    assert first_frame.shades and second_frame.shades
    assert first_frame.shades[0].end == FIRST_WINDOW_SHADE_END, (
        "затенение первого кадра размечено по новому торговому окну, а сделки "
        f"на нём — из старого: полоса кончается в {first_frame.shades[0].end}"
    )
    assert second_frame.shades[0].end == SECOND_WINDOW_SHADE_END, (
        "пересчитанный кадр не взял новое окно"
    )


def test_the_tail_bar_of_a_day_does_not_vanish_silently(loop, database_with_tails) -> None:
    """Граница полноты минуток объявляется, а отброшенные бары — называются.

    В базе два дня, и каждый обрывается на 11:57 — не на границе бара. Без
    объявленной границы полноты знание останавливается на последней минутке
    КАЖДОГО дня, и бар 11:55–12:00 объявляется незакрытым в обоих. Первый
    день от этого терял свой последний бар молча: данные за него полны
    настолько, насколько вообще будут.

    Хвост последнего дня отбрасывается по-прежнему — и это верно: «данные
    кончились» и «торги кончились» по самому ряду неразличимы. Но число
    отброшенных баров попадает в журнал, а не пропадает.
    """
    _, recorded = replay_history(loop, database_with_tails, values=Settings(), days=0)
    # На оси свеча стоит своим открытием: бар 11:55–12:00 — это отметка 11:55.
    starts = {candle.opens_at for candle in recorded.charts[-1].candles}
    assert datetime(2026, 6, 19, 11, 55, tzinfo=MSK) in starts, (
        "последний бар первого дня выпал из прогона: тестер не объявил "
        "границу полноты, и слой данных счёл бар незакрытым"
    )
    assert datetime(2026, 6, 22, 11, 55, tzinfo=MSK) not in starts, (
        "бар за пределом известного попал в прогон — он ещё изменится"
    )
    journal_rows = [
        entry for entry in recorded.decisions[-1]
        if entry.event == "Незакрытые свечи не в прогоне"
    ]
    assert journal_rows, "отброшенные бары нигде не названы"
    assert "1 шт." in journal_rows[-1].reason, journal_rows[-1].reason


def test_days_counts_the_day_of_the_last_candle_too(loop, two_day_database) -> None:
    """`--days 1` — это день последней свечи, а не он же плюс предыдущий.

    Граница округляется вниз до полуночи намеренно (иначе первый день
    выборки обрезан посередине), и из-за этого `N` дней назад от последней
    свечи давали N+1 календарный день. Подсказка к ключу обещала другое.
    """
    _, recorded = replay_history(loop, two_day_database, values=Settings(), days=1)
    days_shown = {candle.opens_at.date() for candle in recorded.charts[-1].candles}
    assert days_shown == {date(2026, 6, 19)}, f"взят лишний день: {sorted(days_shown)}"

    _, recorded = replay_history(loop, two_day_database, values=Settings(), days=2)
    days_shown = {candle.opens_at.date() for candle in recorded.charts[-1].candles}
    assert days_shown == {date(2026, 6, 18), date(2026, 6, 19)}, sorted(days_shown)


def test_a_request_during_a_run_is_not_lost(loop, database) -> None:
    """Настройки, изменённые во время прогона, применяются сразу после него.

    Потерянная просьба выглядит как «программа меня не услышала»: окно
    показывает новый режим, а график остаётся от старого.
    """

    async def go():
        worker = MarketWorker(database)
        await worker.open()
        port = HistoryPort(worker, values=Settings(), days=0, sanitize=redact)
        recorded = Recorded(port)
        try:
            port.refresh("первый")     # задача поставлена, но ещё не работала
            port.set_mode(Mode.OFF)    # просьба пришла во время прогона
            await port.wait()
        finally:
            await port.aclose()
            await worker.close()
        return recorded

    recorded = loop.run_until_complete(go())
    database_passes = [what for on, what in recorded.busy if on and "базы" in what]
    assert len(database_passes) == 2, "второй прогон не состоялся — просьба потеряна"
    assert len(recorded.trades[-1][0]) == 0, "показан результат старого режима"


def test_a_halted_run_is_visible_in_the_window(loop, database, monkeypatch) -> None:
    """Прогон, остановленный движком, обязан выглядеть остановленным.

    Проверяется **плашка над графиком**, а не поле снимка состояния: поле
    можно заполнить и не показать. Здесь окно собрано целиком и подключено
    к порту так же, как в `app/main.py`.

    Остановку движка на модели исполнения по истории не воспроизвести: она
    наступает на сделке, не сопоставившейся с заявкой в полёте, на застрявшем
    выходе и на отказе исполнителя — а `HistoryExecutor` не отказывает
    и не путается. Поэтому прогон настоящий, и подменена ровно одна вещь:
    причина остановки в его результате.

    Что было: `replay` делал `break`, `HistoryRun.halted` заполнялся, порт
    писал строку ERROR в журнал — и тут же отправлял в окно состояние
    с `halted=""`. Плашки не было, режим в панели не подсвечивался,
    а `Context.of(state)` считал робота управляющим: позиция, брошенная
    на середине прогона, показывалась обычной.
    """
    from dataclasses import replace as _replace

    from ui.main_window import MainWindow

    from app import port as port_module

    HALT_REASON = "Сделка не сопоставилась ни с одной заявкой в полёте"
    the_real_replay = port_module.replay

    async def halted_replay(*args, **kwargs):
        run = await the_real_replay(*args, **kwargs)
        return _replace(run, halted=HALT_REASON)

    monkeypatch.setattr(port_module, "replay", halted_replay)

    async def go():
        worker = MarketWorker(database)
        await worker.open()
        port = HistoryPort(
            worker, values=Settings(), days=0, sanitize=redact,
            until=datetime(2026, 6, 19, 10, 30, tzinfo=MSK),  # оборвать в окне
        )
        window = MainWindow(port=port, settings=Settings(), sanitize=redact)
        try:
            await _run(port)
        finally:
            await port.aclose()
            await worker.close()
        return window

    window = loop.run_until_complete(go())
    try:
        assert window.halt_banner.isVisibleTo(window), (
            "робот остановлен, а красной плашки над графиком нет: окно "
            "показывает график, кончившийся раньше данных, как обычный"
        )
        message = window.halt_banner.text()
        assert HALT_REASON in message, f"причина остановки не названа: {message!r}"
        assert "ОСТАНОВЛЕН" in window.status_panel.mode.value.text(), (
            "в панели состояния остановка неотличима от простоя"
        )
        assert "Позиция при этом осталась открыта" in message, (
            "позиция, брошенная остановленным прогоном, показана управляемой"
        )
    finally:
        window._timer.stop()  # noqa: SLF001 — как в фикстуре тестов окна
        from ui.models import RobotState

        window.apply_state(RobotState())  # снять позицию до закрытия
        window.close()
        window.deleteLater()


def test_the_runs_position_is_shown_with_its_levels(loop, database) -> None:
    """Прогон, оборванный внутри окна, оставляет позицию — и её видно.

    Это позиция **прогона**, а не счёта: `simulation` стоит, и все тексты окна
    про неё говорят «на счёте у брокера её нет» (`ui/notices.py`).
    """
    from ui.models import LevelKind, TakeGuard

    _, recorded = replay_history(
        loop, database, values=Settings(), days=0,
        until=datetime(2026, 6, 19, 10, 30, tzinfo=MSK),
    )
    state = recorded.states[-1]
    assert state.position is not None, "робот вошёл в окне и остался в позиции"
    assert state.simulation is True
    assert state.position.take is TakeGuard.ARMED
    assert state.position.take_level is not None
    kinds = {level.kind for level in recorded.charts[-1].levels}
    assert LevelKind.ENTRY in kinds
    assert LevelKind.TAKE in kinds or LevelKind.TRAILING in kinds


def test_every_row_the_port_builds_names_its_run(loop, database) -> None:
    """У каждой строки, вышедшей из порта, названо происхождение прогона.

    Сторож на дыру, найденную 03.09.2026 при Э1-10б. Механизм происхождения
    был сделан и покрыт тестами в `market/` и `app/convert.py`, а единственный
    вызывающий, наполняющий окно, его не использовал: `origin` оставался
    `None`, и в выгрузке стояло «прогон не назван» вместо «прогон по истории».

    Цена, ради которой это стережётся, названа в решении 0011: владелец счёта
    выгружает отчёт за день и **выбирает объём по числу сделок из этого файла**.
    Отчёт, в котором прогон по истории неотличим от боевого, даёт ему для этого
    выбора неверное число.

    Проверяются строки **обоих** видов: пришедшие из движка и сочинённые самой
    программой (`note`) — дыра была в обоих.
    """
    from ui.models import RunOrigin

    _, recorded = replay_history(loop, database, values=Settings(), days=0)

    rows, _ = recorded.trades[-1]
    assert rows, "прогон не дал ни одной сделки — сторожу нечего проверять"
    nameless = [row for row in rows if row.origin is None]
    assert not nameless, (
        f"{len(nameless)} сделок из {len(rows)} уйдут в выгрузку "
        "с пометкой «прогон не назван»"
    )
    assert {row.origin for row in rows} == {RunOrigin.BACKTEST}, (
        "порт гоняет только прошлое, а помечает строки чем-то ещё"
    )

    decisions = recorded.decisions[-1]
    assert decisions, "журнал решений пуст"
    nameless = [row for row in decisions if row.origin is None]
    assert not nameless, (
        f"{len(nameless)} строк решений из {len(decisions)} без происхождения; "
        f"первая — {nameless[0].event!r}"
    )


# -- день считается в московском времени --------------------------------------


def _utc_deal(exit_at: datetime) -> Deal:
    """Сделка, целиком описанная в UTC. Числа неважны, важен только день."""
    return Deal(
        side=Side.LONG,
        volume=1.0,
        entry_time=exit_at - timedelta(minutes=5),
        entry_price=100_000.0,
        entry_order_id="вход",
        exit_time=exit_at,
        exit_price=100_100.0,
        exit_order_id="выход",
        exit_reason=ExitReason.WINDOW_END,
        commission=28.0,
    )


def test_the_day_of_the_panel_is_a_moscow_day() -> None:
    """«Прибыль за день» считается по московским суткам, а не по суткам источника.

    Источник, отдающий UTC, разводит панель и разбивку внутри той же сводки
    (`backtest.summarise` приводит время к МСК явно) — с 00:00 до 03:00 МСК
    ровно на сутки. Проверка нарочно ставит последнюю свечу в этот промежуток:
    21:30 UTC — это уже 00:30 следующего дня в Москве.
    """
    from app.port import _day_summary

    utc = timezone.utc
    last = Candle(
        time=datetime(2026, 6, 19, 21, 30, tzinfo=utc),
        open=1.0, high=1.0, low=1.0, close=1.0, volume=1.0,
        timeframe=Timeframe(5), filled_minutes=5,
    )
    # Обе сделки закрылись 19 июня по UTC, но в разные московские дни.
    same_moscow_day = _utc_deal(datetime(2026, 6, 19, 21, 40, tzinfo=utc))
    other_moscow_day = _utc_deal(datetime(2026, 6, 19, 5, 0, tzinfo=utc))
    run = HistoryRun(deals=(other_moscow_day, same_moscow_day))

    summary = _day_summary(run, [last])
    assert summary is not None
    assert summary.trades == 1, (
        "в «за день» попала сделка другого московского дня: день взят "
        "по времени источника, а не по МСК"
    )


def test_the_day_of_the_panel_refuses_a_naive_moment() -> None:
    """Наивный момент отвергается, а не толкуется молча.

    Так же поступает `backtest.summarise`: время без пояса сдвинуло бы границу
    суток на разницу поясов, и неверное число в панели ничем бы себя не выдало.
    """
    from app.port import _day_summary

    naive = Candle(
        time=datetime(2026, 6, 19, 21, 30),
        open=1.0, high=1.0, low=1.0, close=1.0, volume=1.0,
        timeframe=Timeframe(5), filled_minutes=5,
    )
    run = HistoryRun(deals=(_utc_deal(datetime(2026, 6, 19, 21, 40, tzinfo=timezone.utc)),))
    with pytest.raises(ValueError):
        _day_summary(run, [naive])


# -- чистка секретов доезжает не только до базы -------------------------------


def test_the_program_gives_the_window_the_same_cleaner_as_the_store() -> None:
    """`app/main.py` отдаёт чистку порту и окну — тем же именем, что и слою данных.

    Проверка по исходнику, а не по поведению: собрать `_run` целиком стоит
    базы, окна и цикла событий, а забыть здесь можно ровно одну строку.
    Два разных имени в двух вызовах означали бы две разных чистки на два
    выхода — они разошлись бы молча, и выгрузка осталась бы без рубежа
    `broker/`, у которого единственного есть реестр живых значений.
    """
    import ast

    tree = ast.parse((pathlib.Path(__file__).resolve().parent.parent / "app" / "main.py")
                     .read_text(encoding="utf-8"))
    cleaners: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in {"MainWindow", "MarketWorker", "HistoryPort"}:
            continue
        for keyword in node.keywords:
            if keyword.arg == "sanitize" and isinstance(keyword.value, ast.Name):
                cleaners[node.func.id] = keyword.value.id

    assert "MarketWorker" in cleaners, "слою данных перестали передавать чистку"
    assert "HistoryPort" in cleaners, (
        "порт собирается без чистки: рубеж на границе app/ → ui/ снят, "
        "и текст отказа поедет в окно как есть"
    )
    assert "MainWindow" in cleaners, (
        "окно собирается без чистки: выгрузка журналов пойдёт мимо неё"
    )
    assert len(set(cleaners.values())) == 1, (
        f"чистки разные: {cleaners}. Три разных на три места разошлись бы молча"
    )


# -- рубеж чистки стоит на границе app/ → ui/ ---------------------------------
#
# У одной строки журнала решений четыре выхода: ячейка таблицы, всплывающая
# подсказка, строка состояния окна и выгрузка в файл. Чистка на выгрузке
# закрывала один из четырёх. Разбор — в докстринге `HistoryPort._send`.

#: Приметная подделка формы JWT. То же значение, что в `test_market_journal`,
#: `test_app_journal_records` и `test_ui_journals`: одно и то же в четырёх
#: местах читается как одно и то же.
FAKE_JWT = "eyJGQUtF-NOT-A-REAL.eyJGQUtF-NOT-A-REAL.FAKE-NOT-A-REAL-TOKEN"


def test_a_token_in_a_failure_reaches_neither_the_window_nor_the_journal(
    loop, database, monkeypatch, qapp
) -> None:
    """Отказ прогона несёт тело ответа сервера — и оно не должно доехать никуда.

    Путь настоящий: `_refresh` ловит `Exception as error` и кладёт `{error}`
    и в причину решения, и в `failed`. На Э1-5 в `{error}` придёт ответ
    брокера. Проверяются **все** выходы окна разом: строка состояния,
    показанный текст ячейки, всплывающая подсказка и снимок состояния.
    """
    from app import port as port_module
    from ui.journals import SORT_ROLE
    from ui.main_window import MainWindow

    async def leaky_replay(*args, **kwargs):
        raise RuntimeError(f'сервер ответил: {{"access_token": "{FAKE_JWT}"}}')

    monkeypatch.setattr(port_module, "replay", leaky_replay)

    async def go():
        worker = MarketWorker(database)
        await worker.open()
        port = HistoryPort(worker, values=Settings(), days=0, sanitize=redact)
        recorded = Recorded(port)
        window = MainWindow(port=port, settings=Settings(), sanitize=redact)
        window._timer.stop()  # noqa: SLF001 — часы в тесте только мешают
        try:
            await _run(port)
        finally:
            await port.aclose()
            await worker.close()
        return window, recorded

    window, recorded = loop.run_until_complete(go())
    try:
        seen: list[str] = list(recorded.failures)
        seen.append(window.statusBar().currentMessage())
        model = window.journals.decisions_model
        assert model.rowCount() > 0, "журнал пуст — проверка вакуумна"
        for row in range(model.rowCount()):
            for column in range(model.columnCount()):
                index = model.index(row, column)
                for role in (
                    Qt.ItemDataRole.DisplayRole,
                    Qt.ItemDataRole.ToolTipRole,
                    SORT_ROLE,
                ):
                    value = model.data(index, role)
                    if isinstance(value, str):
                        seen.append(value)
        seen.extend(
            str(getattr(state, "halted", "")) + str(getattr(state, "note", ""))
            for state in recorded.states
        )
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()

    assert any("eyJGQUtF" not in one for one in seen)
    leaked = [one for one in seen if "eyJGQUtF" in one]
    assert not leaked, f"токен доехал до окна: {leaked}"
    assert any(SECRET_MASK in one for one in seen), (
        "маски нет нигде — чистка не сработала, а проверка вакуумна"
    )


def test_nothing_leaves_the_port_past_the_cleaner() -> None:
    """Прямой `signal.emit` в порту запрещён: он обходит рубеж молча.

    Проверка по исходнику, а не по поведению. Забыть здесь можно ровно одну
    строку — новый сигнал, отправленный напрямую, — и ни один тест поведения
    об этом не скажет: он проверяет то, что доехало, а не то, чем оно чистилось.
    """
    import ast

    source = pathlib.Path(__file__).resolve().parent.parent / "app" / "port.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    inside_send = {
        node
        for holder in ast.walk(tree)
        if isinstance(holder, ast.FunctionDef) and holder.name == "_send"
        for node in ast.walk(holder)
    }
    culprits = [
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "emit"
        and node not in inside_send
    ]
    assert not culprits, (
        "порт отправляет событие мимо `_send`, то есть мимо чистки секретов:\n  "
        + "\n  ".join(culprits)
    )


def test_every_text_field_of_the_window_models_is_cleaned() -> None:
    """Чистка идёт по `dataclasses.fields`, а не по списку полей.

    Новое текстовое поле в `ui.models` обязано попадать под неё само. Список
    «а эти поля чистые» в `market/journal.py` уже оказался неверным сразу
    в семи местах — здесь его нет, и проверка это подтверждает: у **каждого**
    типа окна все строковые поля заполняются токеном, и ни одно не должно
    его сохранить.

    Нестроковые обязательные поля заполняются `None`: датакласс типов
    не проверяет, а проверка здесь не про типы.
    """
    import dataclasses as dc

    from app.port import _scrubbed
    from ui import models

    leaky = f"Bearer {FAKE_JWT}"
    checked: list[str] = []
    for name in dir(models):
        kind = getattr(models, name)
        if not (isinstance(kind, type) and dc.is_dataclass(kind)):
            continue
        text_fields = [one.name for one in dc.fields(kind) if "str" in str(one.type)]
        if not text_fields:
            continue
        filling: dict[str, object] = {}
        for one in dc.fields(kind):
            if one.name in text_fields:
                filling[one.name] = leaky
            elif one.default is dc.MISSING and one.default_factory is dc.MISSING:
                filling[one.name] = None
        cleaned = _scrubbed(kind(**filling), redact)
        for field_name in text_fields:
            assert "eyJGQUtF" not in getattr(cleaned, field_name), (
                f"{name}.{field_name} не чистится"
            )
        checked.append(name)

    assert len(checked) >= 8, (
        f"типов с текстовыми полями найдено {len(checked)} ({checked}) — "
        "проверка перестала находить модели окна"
    )


def test_a_string_enum_is_not_replaced_by_a_plain_string() -> None:
    """`RunOrigin` и `DecisionLevel` объявлены как `str, Enum` — их трогать нельзя.

    `isinstance(value, str)` для них истинно. Подменённое обычной строкой
    значение перечисления сломало бы раскраску журнала и подпись происхождения,
    ничего при этом не уронив: окно сравнивает их через `is`.
    """
    from app.port import _scrubbed
    from ui.models import DecisionRow

    row = DecisionRow(
        time=datetime(2026, 6, 19, 10, 10, tzinfo=MSK),
        event=f"Отказ: {FAKE_JWT}",
        reason="обычная причина",
        level=DecisionLevel.ERROR,
    )
    cleaned = _scrubbed(row, redact)
    assert "eyJGQUtF" not in cleaned.event
    assert cleaned.level is DecisionLevel.ERROR, "значение перечисления подменено строкой"
    assert cleaned.reason is row.reason, "чистая строка пересобрана без нужды"


# ------------------------------------------------------------ поток котировок


def test_stream_without_a_handler_refuses_aloud(loop, database) -> None:
    """Кнопка нажата, сборка связь не провела — отказ вслух, а не тишина."""
    port, recorded = replay_history(loop, database)
    port.stream(True)
    assert recorded.failures[-1] == (
        "Подключение к брокеру не собрано: программа запущена без него."
    )
    row = recorded.appended[-1]
    assert row.level is DecisionLevel.WARNING
    assert row.event == "поток котировок"
    assert port.connection is Connection.UNKNOWN


def test_a_refused_connection_speaks_and_releases_the_button(loop, database) -> None:
    """Отказ сборки: строка журнала, строка состояния и снимок с `OFFLINE` — сразу.

    Без снимка кнопка окна оставалась бы нажатой при связи, которой нет.
    Снимок идёт без прогона: связь меняется на каждой попытке, и прогон
    по всей истории ради одного поля был бы штормом (ворота `/validate`).
    """
    port, recorded = replay_history(loop, database)
    port.attach_stream(lambda on: "Токен брокера ещё не введён. Откройте настройки.")
    states, busy = len(recorded.states), list(recorded.busy)
    port.stream(True)
    assert recorded.failures[-1] == "Токен брокера ещё не введён. Откройте настройки."
    row = recorded.appended[-1]
    assert (row.event, row.level) == ("Подключение к брокеру", DecisionLevel.WARNING)
    assert port.connection is Connection.OFFLINE
    assert len(recorded.states) == states + 1
    assert recorded.states[-1].connection is Connection.OFFLINE
    assert recorded.busy == busy, "ради снимка состояния запущен прогон"


def test_an_accepted_stream_command_does_not_pretend_to_know_the_connection(
    loop, database
) -> None:
    """Принятая команда связи не отказывает и не выдумывает состояние связи.

    ⚠️ Прежде здесь стояло «принятая команда не говорит **ничего**», и это
    перестало быть верным на Э3: включение потока запускает живой ход движка,
    а о том, что робот пошёл по живому ряду и что заявки при этом не подаются,
    владелец счёта обязан узнать из журнала. Молчание там было бы дефектом,
    а не чистотой.

    Что осталось прежним и проверяется здесь: отказа нет; состояние связи
    выставляет **поток**, а не команда, — иначе кнопка светилась бы связью,
    которой ещё нет.
    """
    port, recorded = replay_history(loop, database)
    asked: list[bool] = []
    port.attach_stream(asked.append)
    failures, states = len(recorded.failures), len(recorded.states)
    port.stream(True)
    port.stream(False)
    assert asked == [True, False]
    assert len(recorded.failures) == failures, "принятая команда связи объявлена отказом"
    assert len(recorded.states) == states, "ради команды связи переслан снимок состояния"
    assert port.connection is Connection.UNKNOWN, "состояние связи выставляет поток, не команда"
    said = [row.event for row in recorded.appended[-2:]]
    assert said == ["Наблюдение включено", "Наблюдение выключено"], (
        f"о включении и выключении живого хода не сказано: {said}"
    )
    # Включение ставит прогон в очередь цикла событий, а он тут уже остановлен.
    # Незакрытая задача досталась бы **следующему** тесту: «Task was destroyed
    # but it is pending» падает при разборке чужой обвязки, и искать его идут
    # не туда.
    loop.run_until_complete(port.aclose())


def test_restate_resends_the_last_snapshot_with_the_new_connection_and_no_run(
    loop, database
) -> None:
    port, recorded = replay_history(loop, database)
    last, busy = recorded.states[-1], list(recorded.busy)
    port.connection = Connection.ONLINE
    port.restate()
    assert recorded.states[-1] == replace(last, connection=Connection.ONLINE)
    assert recorded.busy == busy, "ради снимка состояния запущен прогон"


def test_restate_before_the_first_run_sends_nothing(loop, database) -> None:
    """Снимка ещё нет — переотправлять нечего; связь придёт с первым прогоном."""

    async def go() -> list[object]:
        worker = MarketWorker(database)
        port = HistoryPort(worker, sanitize=redact)
        recorded = Recorded(port)
        port.connection = Connection.RECONNECTING
        port.restate()
        await worker.close()
        return recorded.states

    assert loop.run_until_complete(go()) == []

# ------------------------------------------- живая свеча: рисунок, а не прогон

#: Минута, следующая за базой фикстуры `database` (07:00…11:59, 300 минут).
#: Бар [11:55, 12:00) в ней закрыт, а [12:00, 12:05) начинается со следующей.
NEXT_MINUTE = DAY + timedelta(minutes=300)


def _one_minute(moment: datetime) -> Candle:
    """Одна минутка — такая же, как пишет живой поток: одна строка, один срез."""
    price = 100_000.0 + 300.0 * math.sin(moment.minute / 9.0)
    return Candle(
        time=moment, open=price, high=price + 40.0, low=price - 40.0, close=price,
        volume=1.0, timeframe=Timeframe(1), filled_minutes=1,
    )


class LiveRun:
    """Прогон по базе, в которую по ходу дописываются живые минутки.

    Помощник, а не проверка: держит базу открытой между шагами, чтобы живая
    минута попадала туда же, откуда порт читает график, — как в программе
    (`app/live_feed.py` пишет в `minute_candle`, порт перечитывает базу).
    """

    def __init__(self, port: HistoryPort, worker: MarketWorker, recorded: Recorded) -> None:
        self.port = port
        self.worker = worker
        self.recorded = recorded

    def counters(self) -> tuple[int, int, int]:
        """Полные снимки графика, свечи справа, уточнения последней свечи."""
        return (
            len(self.recorded.charts),
            len(self.recorded.grown),
            len(self.recorded.retouched),
        )

    async def minute(self, moment: datetime) -> tuple[int, int, int]:
        """Записать минутку, отдать её порту и вернуть, что тот сделал.

        Возвращает приросты трёх счётчиков: полных снимков графика, свечей,
        добавленных справа, и уточнений последней свечи.
        """
        was = self.counters()
        await self.worker.put_minutes("MXU6", [_one_minute(moment)], Source.BROKER)
        self.port.live_candle("MXU6")
        await self.port.wait()
        return tuple(b - a for a, b in zip(was, self.counters(), strict=True))  # type: ignore[return-value]

    async def growing(self, minute: Candle, symbol: str = "MXU6") -> tuple[int, int, int]:
        """Отдать порту снимок **ещё идущей** минуты — как это делает поток.

        В базу при этом не пишется ничего: формирующаяся минута туда
        не попадает никогда (`app/live_feed.py`, шапка). Именно поэтому
        помощник берёт минутку готовой, а не кладёт её в базу, как `minute`.
        """
        was = self.counters()
        self.port.growing_minute(symbol, minute)
        await self.port.wait()
        return tuple(b - a for a, b in zip(was, self.counters(), strict=True))  # type: ignore[return-value]


def _running_minute(
    moment: datetime,
    *,
    close: float,
    high: float | None = None,
    low: float | None = None,
    volume: float = 1.0,
) -> Candle:
    """Минутка, которая **ещё идёт**: ровно то, что отдаёт поток на снимке.

    `unsettled=True` — не украшение: бар, собранный с такой минуткой, обязан
    остаться незакрытым, иначе по нему пойдёт прогон (`market.build_bars`).
    """
    return Candle(
        time=moment,
        open=close,
        high=close if high is None else high,
        low=close if low is None else low,
        close=close,
        volume=volume,
        timeframe=Timeframe(1),
        filled_minutes=1,
        unsettled=True,
    )


def live_session(loop, database: pathlib.Path, body, **kwargs):
    """Открыть базу, показать историю, дальше — по шагам живой минуты."""

    async def go():
        worker = MarketWorker(database)
        await worker.open()
        port = HistoryPort(worker, sanitize=redact, **kwargs)
        recorded = Recorded(port)
        try:
            await _run(port)
            return await body(LiveRun(port, worker, recorded))
        finally:
            await port.aclose()
            await worker.close()

    return loop.run_until_complete(go())


def test_a_growing_bar_is_drawn_and_does_not_start_a_run(loop, database) -> None:
    """Пока бар набирается, живая минута рисует свечу и НЕ гоняет прогон.

    Это и есть `B-009`: до 04.09.2026 каждая живая минута звала `refresh`,
    то есть прогон стратегии по всей истории, и он заканчивался полной заменой
    набора свечей — приближение, выставленное руками, сбрасывалось раз в минуту.

    Прогон здесь не нужен по существу, а не ради скорости: движок принимает
    решения на **закрытии** бара (`ARCHITECTURE.md` §6, `_trusted`), и по
    незакрытому бару ни сделки, ни точки средней, ни уровня появиться не может.
    """

    async def body(run: LiveRun):
        first = await run.minute(NEXT_MINUTE)                    # 12:00 — новый бар
        second = await run.minute(NEXT_MINUTE + timedelta(minutes=1))  # 12:01 — тот же
        return first, second

    first, second = live_session(loop, database, body, values=Settings(), days=0)

    assert first == (0, 1, 0), (
        "первая минута нового бара обязана прийти добавлением свечи справа "
        f"и без прогона, а пришло (снимков, добавлено, уточнено) = {first}"
    )
    assert second == (0, 0, 1), (
        "вторая минута того же бара обязана уточнить последнюю свечу "
        f"и не гонять прогон, а пришло {second}"
    )


def test_a_closed_bar_still_runs_the_strategy(loop, database) -> None:
    """Бар закрылся — прогон идёт, как и прежде, со всеми метками.

    Парная проверка к предыдущей, и без неё та была бы вредна: «никогда
    не гонять прогон» прошло бы её зелёной, а метки сделок, средняя, уровни
    и затенение перестали бы появляться вовсе. Молчаливая потеря меток хуже
    сброса масштаба.

    Бар [12:00, 12:05) закрывается на минутке 12:04: полнота ряда объявлена
    по последней минутке плюс одна (`CandleStore.bars`), и 12:05 достигается
    ровно тогда.
    """

    async def body(run: LiveRun):
        steps = []
        for shift in range(5):  # 12:00 … 12:04
            steps.append(await run.minute(NEXT_MINUTE + timedelta(minutes=shift)))
        return steps

    steps = live_session(loop, database, body, values=Settings(), days=0)

    charts = [step[0] for step in steps]
    assert charts == [0, 0, 0, 0, 1], (
        "прогон обязан пойти ровно на закрытии бара — на минутке 12:04 "
        f"и ни на одной раньше, а снимки графика пришли по шагам: {charts}"
    )


def test_a_live_minute_of_another_instrument_touches_nothing(loop, database) -> None:
    """Минута чужого инструмента график не трогает и прогон не запускает.

    ⚠️ Считаются все три счётчика, а не только снимок и добавление. Без сверки
    уточнений проверка молчала бы: без разбора, чей это символ, порт перечитал
    бы хвост показанного инструмента и **уточнил бы последнюю свечу** — снимка
    и добавления при этом не было бы ни одного, и проверка осталась бы зелёной
    (замер мутацией, 04.09.2026).
    """

    def counters(run: LiveRun) -> tuple[int, int, int]:
        return (
            len(run.recorded.charts),
            len(run.recorded.grown),
            len(run.recorded.retouched),
        )

    async def body(run: LiveRun):
        was = counters(run)
        await run.worker.put_minutes("SiU6", [_one_minute(NEXT_MINUTE)], Source.BROKER)
        run.port.live_candle("SiU6")
        await run.port.wait()
        return counters(run), was

    now, was = live_session(loop, database, body, values=Settings(), days=0)
    assert now == was, (
        "минута чужого инструмента дошла до показанного графика: "
        f"(снимков, добавлено, уточнено) было {was}, стало {now}"
    )


def test_a_bar_the_run_refused_leaves_the_chart_instead_of_freezing_on_it(
    loop, database
) -> None:
    """Бар, собранный не из всех минут, закрывается — и с графика уходит.

    Случай неприятный и потому проверяется отдельно. Минуты без сделок в живом
    потоке штатны, а догрузки пропущенного в этой версии нет, поэтому неполный
    бар, пришедший в этом сеансе, прогон отбрасывает и **говорит об этом
    в журнал**: «такой бар закрытым не показывается» (`_trusted`). Пока бар
    набирался, его рисовала живая свеча; в момент закрытия признак снимается,
    и дальше про бар отвечает прогон. Оставить бар на графике вопреки строке
    журнала значило бы показать закрытым ровно то, что журнал объявил
    непоказанным.

    Дыра в ряду при этом остаётся дырой — её лечит догрузка (`B-006`),
    а не рисование того, чему прогон не поверил.
    """

    async def body(run: LiveRun):
        await run.minute(NEXT_MINUTE)                            # 12:00
        await run.minute(NEXT_MINUTE + timedelta(minutes=2))     # 12:02, 12:01 пропущена
        await run.minute(NEXT_MINUTE + timedelta(minutes=4))     # 12:04 — бар закрылся
        return run.recorded.charts[-1].candles[-1].opens_at, len(run.recorded.grown)

    last_shown, grown = live_session(loop, database, body, values=Settings(), days=0)

    assert last_shown == NEXT_MINUTE - timedelta(minutes=5), (
        "неполный бар доехал до снимка прогона — движок решает по бару, "
        "собранному не из всех минут"
    )
    assert grown == 1, (
        "неполный бар вернулся на график после прогона, который его отбросил: "
        "на экране закрытая свеча, про которую журнал говорит обратное"
    )


# ------------------------------------- растущая свеча внутри минуты (`B-007`)


def test_a_snapshot_inside_the_minute_redraws_the_candle_without_waiting_for_it_to_end(
    loop, database
) -> None:
    """`B-007`. Снимок идущей минуты меняет свечу сразу, а не в конце минуты.

    Слова владельца счёта: «свечка не колеблется при получении сигнала,
    как я видел на бирже». Раньше свеча появлялась готовой и только после
    конца минуты: программа узнавала о конце минуты 14:07 из первого снимка
    минуты 14:08, писала её в базу и читала обратно — по замеру канала это
    от 63 до 77 секунд после начала минуты.

    Проверяется не «пришёл сигнал», а **что именно пришло**: цена свечи
    обязана стать ценой снимка. Сверка одним счётчиком прошла бы зелёной
    и при отправке прежней свечи.
    """

    async def body(run: LiveRun):
        await run.minute(NEXT_MINUTE)  # 12:00 — бар [12:00, 12:05) начат
        first = await run.growing(_running_minute(NEXT_MINUTE, close=222_222.0))
        second = await run.growing(_running_minute(NEXT_MINUTE, close=333_333.0))
        return first, second, [candle.close for candle in run.recorded.retouched]

    first, second, closes = live_session(
        loop, database, body, values=Settings(), days=0, redraw_gap=0.0
    )

    assert first == (0, 0, 1), (
        "снимок внутри минуты обязан уточнить последнюю свечу — без прогона "
        f"и без второй свечи, а пришло (снимков, добавлено, уточнено) = {first}"
    )
    assert second == (0, 0, 1), f"второй снимок той же минуты не нарисован: {second}"
    assert closes[-2:] == [222_222.0, 333_333.0], (
        "свеча не колеблется: в окно ушла не цена снимка, а что-то другое — "
        f"{closes[-2:]}"
    )


def test_the_growing_bar_is_the_database_plus_the_running_minute(loop, database) -> None:
    """Растущий бар = записанные минутки этого бара плюс идущая минута.

    Пятиминутка 12:00 состоит из минут 12:00…12:04. Когда идёт 12:02, две
    первые уже в базе — и они обязаны остаться в баре: открытие бара берётся
    у 12:00, объём складывается из всех трёх. Сама идущая минута в базу
    не попадает никогда, поэтому её высшая и низшая цены приходят только
    из снимка.

    Проверка ловит **обе** ошибки склейки, и по отдельности каждая
    правдоподобна на вид:

    * забыли базу — бар начнётся с идущей минуты (открытие 199 000, объём 7),
      и пятиминутка на экране будет короче настоящей;
    * забыли снимок — размах останется старым, и свеча снова замрёт.
    """
    spike, pit = 199_000.0, 98_000.0

    async def body(run: LiveRun):
        await run.minute(NEXT_MINUTE)                            # 12:00 в базу
        await run.minute(NEXT_MINUTE + timedelta(minutes=1))     # 12:01 в базу
        await run.growing(_running_minute(
            NEXT_MINUTE + timedelta(minutes=2), close=spike, high=spike, low=pit, volume=7.0,
        ))
        return run.recorded.retouched[-1]

    candle = live_session(loop, database, body, values=Settings(), days=0, redraw_gap=0.0)

    assert candle.opens_at == NEXT_MINUTE, (
        f"растущая минута нарисована отдельной свечой в {candle.opens_at}, "
        "а не внутри своей пятиминутки"
    )
    assert candle.open == _one_minute(NEXT_MINUTE).open, (
        "начало бара взято у идущей минуты: минутки из базы в бар не вошли"
    )
    assert (candle.high, candle.low, candle.close) == (spike, pit, spike), (
        f"размах бара не тронут снимком: {candle.high}/{candle.low}/{candle.close}"
    )
    assert candle.volume == 9.0, (
        f"объём бара {candle.volume} вместо 1 + 1 + 7: слагаемые потерялись"
    )


def test_on_a_one_minute_chart_the_growing_bar_is_the_minute_itself(
    loop, database
) -> None:
    """Минутный график: растущий бар — сама минута, и базы для него не надо.

    Размер свечи в одну минуту — самый вероятный на показе: именно там свеча
    дрожит заметнее всего. У такого бара нет прошлого — он и есть идущая
    минута, — поэтому чтение базы пропускается вовсе, и вся ветка «минутки
    этого бара» остаётся непроверенной, если не спросить её отдельно.

    ⚠️ Ошибка здесь была бы тихой и правдоподобной: бар, собранный
    из **предыдущей** минуты плюс идущей, выглядит обычной свечой и врёт
    вдвое по объёму.
    """

    async def body(run: LiveRun):
        moved = await run.growing(_running_minute(NEXT_MINUTE, close=222_222.0, volume=5.0))
        return moved, run.recorded.grown[-1]

    moved, candle = live_session(
        loop, database, body,
        values=Settings(timeframe="1 минута"), days=0, redraw_gap=0.0,
    )

    assert moved == (0, 1, 0), f"растущая минута на минутном графике не нарисована: {moved}"
    assert (candle.opens_at, candle.close, candle.volume) == (NEXT_MINUTE, 222_222.0, 5.0), (
        f"минутный бар собран не из одной своей минуты: {candle}"
    )


def test_a_finished_minute_is_not_drawn_through_the_growing_door(loop, database) -> None:
    """Через вход растущей свечи законченная минутка не рисуется.

    Вход существует ровно для идущей минуты. Законченная пишется в базу
    и рисуется оттуда — там про неё отвечает прогон, и он вправе бар
    **не показать**: неполный бар, пришедший в этом сеансе, он отбрасывает
    и говорит об этом в журнал (`_trusted`). Нарисованная в обход, она стала бы
    закрытой свечой, про которую журнал в ту же секунду говорит обратное.

    Сторож нужен потому, что подмена выглядит безобидной оптимизацией:
    «минута уже закрыта, нарисуем её сразу, не дожидаясь чтения базы».
    """

    async def body(run: LiveRun):
        finished = replace(_running_minute(NEXT_MINUTE, close=222_222.0), unsettled=False)
        return await run.growing(finished)

    moved = live_session(loop, database, body, values=Settings(), days=0, redraw_gap=0.0)
    assert moved == (0, 0, 0), (
        f"законченная минутка нарисована мимо прогона: {moved}"
    )


def test_a_growing_minute_is_never_written_to_the_base(loop, database) -> None:
    """Растущая минута рисуется, но в базу не попадает ни при каких условиях.

    Записанная формирующаяся минута двигает границу полноты
    (`known_until = последняя минутка + 1`), и бар объявляется закрытым, пока
    его последняя минута ещё набирается: по нему пойдёт прогон, а через минуту
    он окажется другим. На минутном размере свечи это случалось бы **каждую**
    минуту.

    Сторож стоит здесь, а не только у потока: рисующая сторона тоже держит
    минутку в руках, и запись «заодно» отсюда выглядела бы безобидной.
    """

    async def body(run: LiveRun):
        before = await run.worker.minutes("MXU6")
        for shift, price in enumerate((222_000.0, 333_000.0, 444_000.0)):
            moment = NEXT_MINUTE + timedelta(minutes=shift)
            await run.growing(_running_minute(moment, close=price))
        return before, await run.worker.minutes("MXU6"), run.recorded.retouched

    before, after, drawn = live_session(
        loop, database, body, values=Settings(), days=0, redraw_gap=0.0
    )

    assert drawn, "сторож бесполезен: не нарисовано ни одной растущей свечи"
    assert after == before, (
        f"растущая минута дописана в базу: было {len(before)} минуток, "
        f"стало {len(after)}"
    )


def test_a_late_snapshot_does_not_bring_back_a_bar_the_run_threw_away(
    loop, database
) -> None:
    """Опоздавший снимок не воскрешает бар, который прогон уже закрыл.

    Случай гоночный и потому проверяется отдельно. Пока перерисовка читала
    базу, минута закончилась: её записали, бар закрылся, прогон по нему прошёл
    и **отбросил** его как собранный не из всех минут — и сказал об этом
    в журнал (`_trusted`). Снимок, дорисованный после этого, вернул бы такой
    бар на график: на экране закрытая свеча, про которую журнал в ту же
    секунду говорит, что её не показывают.

    Бар [12:00, 12:05) здесь неполон намеренно: 12:01 и 12:03 пропущены —
    так выглядит обрыв связи внутри бара.
    """

    async def body(run: LiveRun):
        await run.minute(NEXT_MINUTE)                            # 12:00
        await run.minute(NEXT_MINUTE + timedelta(minutes=2))     # 12:02
        await run.minute(NEXT_MINUTE + timedelta(minutes=4))     # 12:04 — бар закрылся
        shown = run.recorded.charts[-1].candles[-1].opens_at
        late = await run.growing(_running_minute(
            NEXT_MINUTE + timedelta(minutes=4), close=555_000.0
        ))
        return shown, late, run.counters()

    shown, late, _ = live_session(
        loop, database, body, values=Settings(), days=0, redraw_gap=0.0
    )

    assert shown == NEXT_MINUTE - timedelta(minutes=5), (
        "прогон не отбросил неполный бар — проверка сторожит не тот случай"
    )
    assert late == (0, 0, 0), (
        "опоздавший снимок вернул на график бар, который прогон отбросил: "
        f"(снимков, добавлено, уточнено) = {late}"
    )


def test_a_growing_minute_of_another_instrument_touches_nothing(loop, database) -> None:
    """Растущая минута чужого инструмента показанный график не трогает.

    Считаются все три счётчика: без разбора, чей это символ, порт собрал бы
    бар из минуток **показанного** инструмента и уточнил бы последнюю свечу —
    ни снимка, ни добавления при этом не было бы, и проверка по двум
    счётчикам осталась бы зелёной.
    """

    async def body(run: LiveRun):
        return await run.growing(
            _running_minute(NEXT_MINUTE, close=222_222.0), symbol="SiU6"
        )

    moved = live_session(loop, database, body, values=Settings(), days=0, redraw_gap=0.0)
    assert moved == (0, 0, 0), (
        f"минута чужого инструмента дошла до показанного графика: {moved}"
    )


def test_a_flood_of_snapshots_is_thinned_out_by_the_redraw_gap(loop, database) -> None:
    """Порог перерисовки: снимки чаще порога рисуются не все.

    Порог — предохранитель, а не режим работы: брокер шлёт минуту примерно
    девять раз (`REDRAW_GAP`), и при такой частоте он не срабатывает никогда.
    Проверяются обе половины, и вторая обязательна: без неё «не рисовать
    вообще ничего» прошло бы эту проверку зелёным.
    """

    async def body(run: LiveRun):
        first = await run.growing(_running_minute(NEXT_MINUTE, close=222_222.0))
        second = await run.growing(_running_minute(NEXT_MINUTE, close=333_333.0))
        return first, second

    held = live_session(loop, database, body, values=Settings(), days=0, redraw_gap=60.0)
    free = live_session(loop, database, body, values=Settings(), days=0, redraw_gap=0.0)

    assert held == ((0, 1, 0), (0, 0, 0)), (
        f"порог перерисовки не держит поток снимков: {held}"
    )
    assert free == ((0, 1, 0), (0, 0, 1)), (
        f"без порога рисоваться обязаны оба снимка, а вышло {free}"
    )


def test_the_first_snapshot_of_a_new_bar_puts_it_on_the_chart_right_away(
    loop, database
) -> None:
    """Первый снимок нового бара ставит бар на график сразу, а не через размер свечи.

    На пятиминутках это разница между «свеча появилась в 12:00» и «свеча
    появилась в 12:05»: до `B-007` бар возникал, когда в базу ложилась его
    первая минута, то есть в 12:01 в лучшем случае, а полным становился
    только на закрытии.

    Проверяется и время, и цена: свеча, прибавленная не на своё место или
    с чужой ценой, сдвинула бы весь дальнейший разговор про правый край.
    """

    async def body(run: LiveRun):
        moved = await run.growing(_running_minute(NEXT_MINUTE, close=222_222.0))
        return moved, run.recorded.grown[-1]

    moved, candle = live_session(
        loop, database, body, values=Settings(), days=0, redraw_gap=0.0
    )

    assert moved == (0, 1, 0), (
        "первая минута нового бара обязана прийти новой свечой справа "
        f"и без прогона, а пришло (снимков, добавлено, уточнено) = {moved}"
    )
    assert (candle.opens_at, candle.close) == (NEXT_MINUTE, 222_222.0), (
        f"новая свеча встала не на своё место или не с той ценой: {candle}"
    )


def test_the_bar_still_runs_the_strategy_when_it_closes_between_snapshots(
    loop, database
) -> None:
    """Растущая свеча не отменяет прогон на закрытии бара (`B-009` цел).

    Парная проверка ко всем предыдущим, и без неё они вредны: «рисовать
    растущую свечу и никогда не гонять прогон» прошло бы их зелёными,
    а метки сделок, средняя, уровни и затенение перестали бы появляться.

    Снимки идут между минутами — так и бывает в жизни: девять снимков
    на минуту, запись раз в минуту.
    """

    async def body(run: LiveRun):
        steps = []
        for shift in range(5):  # 12:00 … 12:04
            moment = NEXT_MINUTE + timedelta(minutes=shift)
            await run.growing(_running_minute(moment, close=100_000.0 + shift))
            steps.append(await run.minute(moment))
        return steps

    steps = live_session(loop, database, body, values=Settings(), days=0, redraw_gap=0.0)

    charts = [step[0] for step in steps]
    assert charts == [0, 0, 0, 0, 1], (
        "прогон обязан пойти ровно на закрытии бара — на минутке 12:04 "
        f"и ни на одной раньше, а снимки графика пришли по шагам: {charts}"
    )


def _broker_wire(close: float, turnover: float) -> str:
    """Строка ровно того вида, какой шлёт сокет брокера: плоский JSON, UTC.

    Время — 09:00 UTC, то есть 12:00 МСК: первая минута за краем базы
    фикстуры. Проверять разбор на времени без пояса нельзя — его слой
    брокера отвергает, и это отдельный сторож.
    """
    import json

    return json.dumps({
        "responseType": "CandleStick",
        "ticker": "MXU6",
        "classCode": "SPBFUT",
        "timeFrame": "M1",
        "open": close, "high": close, "low": close, "close": close,
        "volume": turnover,
        "dateTime": "2026-06-19T09:00:00.000Z",
    })


def _painted(surface):
    """Снимок нарисованного графика — то, что увидит владелец счёта."""
    from PySide6.QtGui import QImage

    surface.resize(900, 400)
    image = QImage(900, 400, QImage.Format.Format_RGB32)
    surface.render(image)
    return image


def test_a_hanging_growing_candle_does_not_swallow_the_run_at_the_bar_close(
    loop, database
) -> None:
    """Растущая свеча уступает дорисовке закрытой минуты, а не наоборот.

    Две живые дороги идут разными задачами, и слот у каждой свой. Общий слот
    выглядел бы экономнее и стоил бы дорого: перерисовка растущей свечи,
    затянувшаяся на чтении базы, **вытеснила** бы дорисовку закрытой минуты —
    а решает, гнать ли прогон на закрытии бара, именно она. Цена пропуска
    несимметрична: пропущенный прогон — это молча не появившиеся метки
    сделок, средняя и затенение; пропущенное дрожание свечи — доля секунды
    несвежей картинки.

    Чтение базы здесь задержано нарочно — так выглядит медленный диск или
    занятый поток данных. Опоздавший снимок после этого не рисуется:
    бар уже закрыт (`_already_closed`).
    """

    async def body(run: LiveRun):
        gate = asyncio.Event()
        honest = run.worker.minutes

        async def slow(*args, **kwargs):
            await gate.wait()
            return await honest(*args, **kwargs)

        run.worker.minutes = slow  # type: ignore[method-assign]
        run.port.growing_minute("MXU6", _running_minute(
            NEXT_MINUTE + timedelta(minutes=2), close=222_222.0
        ))
        await asyncio.sleep(0.01)  # дать задаче дойти до чтения базы
        assert run.port._growing is not None and not run.port._growing.done(), (  # noqa: SLF001 — предмет проверки
            "растущая свеча не зависла — проверка сторожит не тот случай"
        )

        was = run.counters()
        for shift in range(5):  # 12:00 … 12:04 — бар [12:00, 12:05) закрылся
            await run.worker.put_minutes(
                "MXU6", [_one_minute(NEXT_MINUTE + timedelta(minutes=shift))], Source.BROKER
            )
        run.port.live_candle("MXU6")
        gate.set()
        await run.port.wait()
        return was, run.counters()

    was, now = live_session(loop, database, body, values=Settings(), days=0, redraw_gap=0.0)

    assert now[0] == was[0] + 1, (
        "прогон на закрытии бара не пошёл: его вытеснила зависшая перерисовка "
        "растущей свечи. Метки сделок, средняя и затенение не появились бы вовсе"
    )


def test_a_broker_snapshot_moves_the_painted_candle_all_the_way_to_the_pixels(
    loop, database, qapp
) -> None:
    """Снимок брокера доезжает от строки сокета до нарисованной свечи.

    Проверка **до пикселей**, а не до сигнала, и написана она по замеру
    04.09.2026: четыре теста за день выглядели проверками и не проверяли
    ничего — заглушка `ChartPanel.append_candle → return` проходила прогон
    из 1902 тестов зелёной. Здесь ломается любое звено цепочки: разбор
    сообщения брокера, счёт контрактов, перекладка в минутку, порт, сигнал,
    окно, панель графика, отрисовщик, кисть.

    Взяты **оба** пути свечи: первый снимок минуты открывает новый бар
    (прибавление справа), второй меняет его (уточнение последней). До
    `B-007` не случалось ни того ни другого: снимки внутри минуты
    выбрасывались.
    """
    from app.live_feed import tally_after, to_minute
    from broker.stream import snapshot_of
    from ui.main_window import MainWindow

    async def go():
        worker = MarketWorker(database)
        await worker.open()
        port = HistoryPort(
            worker, values=Settings(), days=0, sanitize=redact, redraw_gap=0.0
        )
        window = MainWindow(port=port, settings=Settings(), sanitize=redact)
        surface = window.chart._surface  # noqa: SLF001 — предмет проверки и есть холст
        shots = []
        try:
            await _run(port)
            shots.append(_painted(surface))
            tally = None
            for close, turnover in ((101_500.0, 700_000.0), (102_500.0, 1_400_000.0)):
                snapshot = snapshot_of(_broker_wire(close, turnover))
                assert snapshot is not None, "сообщение брокера не разобрано"
                tally = tally_after(tally, snapshot)
                assert tally.refusal is None, tally.refusal
                port.growing_minute("MXU6", to_minute(snapshot, tally.contracts, unsettled=True))
                await port.wait()
                qapp.processEvents()
                shots.append(_painted(surface))
            drawn = list(surface._line.candles)  # noqa: SLF001 — то же
        finally:
            window._timer.stop()  # noqa: SLF001 — как в фикстуре тестов окна
            await port.aclose()
            await worker.close()
            window.deleteLater()
            qapp.processEvents()
        return shots, drawn

    (before, after_first, after_second), drawn = loop.run_until_complete(go())

    assert before != after_first, (
        "первый снимок минуты не изменил картинку: свеча текущей минуты "
        "на графике не появилась"
    )
    assert after_first != after_second, (
        "второй снимок той же минуты не изменил картинку: свеча не колеблется — "
        "ровно то, на что указал владелец счёта"
    )
    assert drawn[-1].opens_at == NEXT_MINUTE, (
        f"растущая свеча нарисована не на своём месте: {drawn[-1].opens_at}"
    )
    assert drawn[-1].close == 102_500.0, (
        f"на холсте не цена последнего снимка, а {drawn[-1].close}"
    )
    assert drawn[-1].volume == 14.0, (
        f"объём растущей свечи {drawn[-1].volume} вместо 7 + 7 контрактов "
        "по приращениям оборота"
    )


def test_a_bar_the_run_refused_comes_back_once_the_exchange_confirmed_the_gap(
    loop, database
) -> None:
    """Догруженное становится доверенным: отброшенный бар возвращается в прогон.

    Половина ответа владельцу счёта на «пустой промежуток меня не устраивает».
    Пока пропуск не проверен, бар, собранный не из всех минут, прогон
    отбрасывает: различить «сделок не было» и «связь оборвалась» по самому
    ряду нельзя. Догрузка спрашивает у брокера ровно этот отрезок и двигает
    границу подтверждения (`app/backfill.py`) — и с этого момента пустая
    минута внутри отрезка означает «сделок не было».

    ⚠️ Третьей категории в `_trusted` при этом не появляется: правило осталось
    прежним и по данным (`is_partial` + граница), а «кто принёс свечу» сюда
    не доехало ничем.
    """

    async def body(run: LiveRun):
        await run.minute(NEXT_MINUTE)                            # 12:00
        await run.minute(NEXT_MINUTE + timedelta(minutes=2))     # 12:02, 12:01 нет
        await run.minute(NEXT_MINUTE + timedelta(minutes=4))     # 12:04 — бар закрылся
        refused = run.recorded.charts[-1].candles[-1].opens_at
        moved = run.port.history_confirmed("MXU6", NEXT_MINUTE + timedelta(minutes=5))
        run.port.refresh("догрузка пропущенного")
        await run.port.wait()
        return refused, moved, run.recorded.charts[-1].candles[-1].opens_at

    refused, moved, after = live_session(loop, database, body, values=Settings(), days=0)

    assert refused == NEXT_MINUTE - timedelta(minutes=5), (
        "неполный бар и без подтверждения дошёл до прогона — проверка вакуумна"
    )
    assert moved is True, "граница подтверждения не сдвинулась"
    assert after == NEXT_MINUTE, (
        "бар, отрезок которого биржа подтвердила, так и не вернулся в прогон: "
        f"последняя свеча снимка {after}. Дыра закрыта в базе и осталась на экране"
    )


def test_the_confirmed_edge_never_moves_back(loop, database) -> None:
    """Подтверждение — это знание, и назад оно не отменяется.

    Сдвиг границы влево молча выбросил бы из прогона бары, которым уже
    поверили: они пропали бы с графика до перезапуска программы, и объяснить
    это владельцу счёта было бы нечем.
    """

    async def body(run: LiveRun):
        await run.minute(NEXT_MINUTE)
        await run.minute(NEXT_MINUTE + timedelta(minutes=2))
        await run.minute(NEXT_MINUTE + timedelta(minutes=4))
        forward = run.port.history_confirmed("MXU6", NEXT_MINUTE + timedelta(minutes=5))
        backward = run.port.history_confirmed("MXU6", NEXT_MINUTE)
        run.port.refresh("после отката")
        await run.port.wait()
        return forward, backward, run.recorded.charts[-1].candles[-1].opens_at

    forward, backward, last = live_session(
        loop, database, body, values=Settings(), days=0
    )

    assert forward is True
    assert backward is False, "граница подтверждения поехала назад"
    assert last == NEXT_MINUTE, (
        "бар, которому уже поверили, снова выпал из прогона после отката границы"
    )


def test_another_instrument_does_not_confirm_the_shown_one(loop, database) -> None:
    """Догрузка чужого инструмента границу показанному не двигает.

    Граница одна на порт, а догрузка идёт по тикеру потока. Подтверждение
    по `SiU6` объявило бы подтверждённым отрезок `MXU6`, о котором никто
    не спрашивал, — и неполный бар показался бы закрытым без единого запроса.
    """

    async def body(run: LiveRun):
        await run.minute(NEXT_MINUTE)
        await run.minute(NEXT_MINUTE + timedelta(minutes=2))
        await run.minute(NEXT_MINUTE + timedelta(minutes=4))
        moved = run.port.history_confirmed("SiU6", NEXT_MINUTE + timedelta(minutes=5))
        run.port.refresh("чужое подтверждение")
        await run.port.wait()
        return moved, run.recorded.charts[-1].candles[-1].opens_at

    moved, last = live_session(loop, database, body, values=Settings(), days=0)

    assert moved is False, "чужой инструмент сдвинул границу подтверждения"
    assert last == NEXT_MINUTE - timedelta(minutes=5), (
        "неполный бар показан закрытым после подтверждения чужого инструмента"
    )


# ------------------------------- граница доверия: своя у каждого инструмента

#: Второй торговый день, на три календарных дня позже `DAY`: понедельник 22.06.
LATER_DAY = DAY + timedelta(days=3)

#: Минутка, которую вынимают из ряда второго дня, — 07:22. Пятиминутка
#: [07:20, 07:25) собирается тогда из четырёх минут и помечается неполной.
HOLE = LATER_DAY + timedelta(minutes=22)

#: Начало того самого неполного бара. На оси свеча стоит своим открытием.
HOLED_BAR = LATER_DAY + timedelta(minutes=20)


def _holed(start: datetime, *, drop: datetime, count: int = 300) -> list[Candle]:
    """Минутки дня, из которых вынута одна: бар с этой минутой заведомо неполон.

    Проверка длины здесь не украшение: ряд без дыры прошёл бы все три теста
    ниже зелёным при любом устройстве границы доверия — отбрасывать было бы
    нечего, и проверки стали бы вакуумными.
    """
    minutes = [candle for candle in _minutes(count, start) if candle.time != drop]
    assert len(minutes) == count - 1, f"минутки {drop} в ряду и не было — дыры нет"
    return minutes


@pytest.fixture
def two_instrument_database(tmp_path: pathlib.Path) -> pathlib.Path:
    """Два инструмента **разной глубины** в одной базе.

    MXU6 кончается 19.06, SiU6 идёт 22.06 и содержит дыру. Разная глубина —
    предмет проверки: граница доверия, снятая с первого инструмента, для
    второго оказывается в прошлом, и весь его ряд выглядит «пришедшим
    в этом сеансе».
    """
    path = tmp_path / "two-instruments.sqlite3"
    with CandleStore(path) as store:
        store.put_minutes("MXU6", _minutes(300, DAY), Source.ISS)
        store.put_minutes("SiU6", _holed(LATER_DAY, drop=HOLE), Source.ISS)
    return path


def test_switching_the_instrument_does_not_take_its_ray_apart(
    loop, two_instrument_database
) -> None:
    """Смена инструмента не портит ряд нового: граница доверия своя у каждого.

    Находка ревью 05.09.2026 и прямой удар по задаче «графики обязаны
    совпадать». Граница была одна на порт: запуск с MXU6 ставил её по 19.06,
    а после переключения на SiU6, чей ряд идёт 22.06, каждый неполный бар
    объявлялся пришедшим в этом сеансе и выбрасывался из прогона. Неполных
    баров в истории большинство — 8 822 из 21 712 пятиминуток MXU6, — так что
    с графика уходило около сорока процентов свечей, и журнал объяснял это
    бедой с данными, а не дефектом программы.
    """

    async def go():
        worker = MarketWorker(two_instrument_database)
        await worker.open()
        port = HistoryPort(worker, values=Settings(), days=0, sanitize=redact)
        recorded = Recorded(port)
        try:
            await _run(port)  # MXU6: граница встаёт по 19.06
            port.apply_settings(replace(Settings(), instrument="SiU6"))
            await port.wait()
            partial = [
                bar
                for bar in await worker.bars("SiU6", Timeframe(5), known_until=None)
                if bar.is_partial and not bar.unsettled
            ]
        finally:
            await port.aclose()
            await worker.close()
        return recorded, partial

    recorded, partial = loop.run_until_complete(go())

    assert [bar.time for bar in partial] == [HOLED_BAR], (
        "в ряду SiU6 не оказалось ровно одного неполного закрытого бара — "
        f"проверять нечего, найдено: {[bar.time for bar in partial]}"
    )
    shown = recorded.charts[-1]
    assert shown.instrument == "SiU6", f"на графике не тот инструмент: {shown.instrument}"
    starts = {candle.opens_at for candle in shown.candles}
    assert HOLED_BAR in starts, (
        "неполный бар исторического ряда SiU6 выброшен из прогона: граница "
        "доверия досталась ему от прежнего инструмента"
    )
    dropped = [
        entry for entry in recorded.decisions[-1]
        if entry.event == "Неполные свечи не в прогоне"
    ]
    assert not dropped, f"история объявлена сомнительной: {[e.reason for e in dropped]}"


def test_a_deeper_instrument_does_not_lend_its_edge_to_the_shallower_one(
    loop, two_instrument_database
) -> None:
    """Обратная сторона той же находки: чужой край не делает свой ряд доверенным.

    Первая проверка ловит потерю свечей, эта — противоположную беду, и она
    дороже. SiU6 идёт на три дня дальше MXU6; общая на порт граница после
    взгляда на SiU6 уезжала в будущее ряда MXU6, и **любой** неполный бар
    MXU6 — включая собранный из минут потока с пропуском — оказывался
    «внутри подтверждённого» и уходил в прогон закрытым. Движок принимал
    по нему решение, а спрашивал ли кто-нибудь про пропущенную минуту,
    не знал никто, и в журнале об этом не было ни строки.
    """

    async def go():
        worker = MarketWorker(two_instrument_database)
        await worker.open()
        port = HistoryPort(worker, values=Settings(), days=0, sanitize=redact)
        recorded = Recorded(port)
        try:
            await _run(port)                                        # MXU6
            port.apply_settings(replace(Settings(), instrument="SiU6"))
            await port.wait()                                       # край уехал на 22.06
            port.apply_settings(replace(Settings(), instrument="MXU6"))
            await port.wait()
            # Живая минута MXU6 с пропуском: бар [12:00, 12:05) из трёх минут.
            for shift in (0, 2, 4):
                await worker.put_minutes(
                    "MXU6", [_one_minute(NEXT_MINUTE + timedelta(minutes=shift))],
                    Source.BROKER,
                )
                port.live_candle("MXU6")
                await port.wait()
        finally:
            await port.aclose()
            await worker.close()
        return recorded

    recorded = loop.run_until_complete(go())

    shown = recorded.charts[-1]
    assert shown.instrument == "MXU6", f"на графике не тот инструмент: {shown.instrument}"
    starts = {candle.opens_at for candle in shown.candles}
    assert NEXT_MINUTE not in starts, (
        "бар из минут потока с пропуском показан закрытым: границу доверия "
        "MXU6 подменил край чужого, более глубокого ряда"
    )
    assert NEXT_MINUTE - timedelta(minutes=5) in starts, (
        "с графика ушёл и последний бар истории — проверка выше прошла бы "
        "и на пустом ряду"
    )
    dropped = [
        entry for entry in recorded.decisions[-1]
        if entry.event == "Неполные свечи не в прогоне"
    ]
    assert dropped, "сомнительный бар выброшен молча: в журнале об этом ни строки"


def test_a_base_that_appeared_later_still_gets_its_first_look(
    loop, tmp_path: pathlib.Path
) -> None:
    """Базы при запуске не было — первый взгляд не потрачен, а отложен.

    Признак «чтение уже было» тратился в самом начале `_candles`, до проверки
    существования файла. Запуск на машине без базы съедал единственную
    попытку: граница не подтверждалась ни разу за сеанс (догрузке на пустой
    базе не от чего плясать), и дальше любой неполный бар — включая всю
    историю, появившуюся потом, — считался пришедшим в этом сеансе.
    """
    path = tmp_path / "appears-later.sqlite3"

    async def go():
        worker = MarketWorker(path)
        port = HistoryPort(worker, values=Settings(), days=0, sanitize=redact)
        recorded = Recorded(port)
        try:
            await _run(port)  # базы ещё нет
            missing = list(recorded.failures)
            with CandleStore(path) as store:
                store.put_minutes("MXU6", _holed(DAY, drop=DAY + timedelta(minutes=22)),
                                  Source.ISS)
            await worker.open()
            port.refresh("база появилась")
            await port.wait()
        finally:
            await port.aclose()
            await worker.close()
        return missing, recorded

    missing, recorded = loop.run_until_complete(go())

    assert missing and "Базы свечей нет" in missing[0], (
        f"первый проход прошёл не по ветке «базы нет»: {missing}"
    )
    starts = {candle.opens_at for candle in recorded.charts[-1].candles}
    assert DAY + timedelta(minutes=20) in starts, (
        "неполный бар истории выброшен из прогона: первый взгляд потрачен "
        "проходом, которому смотреть было некуда"
    )
    dropped = [
        entry for entry in recorded.decisions[-1]
        if entry.event == "Неполные свечи не в прогоне"
    ]
    assert not dropped, f"история объявлена сомнительной: {[e.reason for e in dropped]}"


def test_minutes_the_stream_wrote_itself_are_never_confirmed_as_history(
    loop, tmp_path: pathlib.Path
) -> None:
    """Сторож к предыдущей правке: базу завёл поток — его минуты не история.

    Пара к `test_a_base_that_appeared_later…`, и без неё та правка была бы
    опасна. Отложенный первый взгляд обязан закрываться не только чтением:
    как только поток дописал в базу минуту, `coverage.last` показывает
    на неё, и подтвердить край значило бы выдать непроверенное за
    проверенное. Неполный бар из потока показывается закрытым — движок
    принимает по нему решение, а пропущенную минуту никто не спрашивал.
    """
    path = tmp_path / "stream-made-it.sqlite3"

    async def go():
        worker = MarketWorker(path)
        port = HistoryPort(worker, values=Settings(), days=0, sanitize=redact)
        recorded = Recorded(port)
        try:
            await _run(port)  # базы ещё нет
            await worker.open()  # так делает поток: `LiveFeed._listen`
            await worker.put_minutes(
                "MXU6", _holed(DAY, drop=DAY + timedelta(minutes=22)), Source.BROKER
            )
            port.live_candle("MXU6")  # поток: минута записана
            await port.wait()
            port.refresh("после потока")
            await port.wait()
        finally:
            await port.aclose()
            await worker.close()
        return recorded

    recorded = loop.run_until_complete(go())

    starts = {candle.opens_at for candle in recorded.charts[-1].candles}
    assert DAY + timedelta(minutes=20) not in starts, (
        "бар, собранный не из всех минут потока, показан закрытым: программа "
        "выдала минуты собственного потока за подтверждённую историю"
    )
    assert DAY + timedelta(minutes=25) in starts, (
        "выброшены не только сомнительные бары, а весь ряд — проверка выше "
        "прошла бы и на пустом графике"
    )
    dropped = [
        entry for entry in recorded.decisions[-1]
        if entry.event == "Неполные свечи не в прогоне"
    ]
    assert dropped, "бар выброшен молча: в журнале об этом ни строки"


def test_the_growing_bar_survives_a_run_it_did_not_take_part_in(loop, database) -> None:
    """Настройка изменилась — набирающийся бар возвращается на график.

    Прогон обрывается на последнем **закрытом** баре, поэтому полный снимок
    набирающегося бара не содержит. Без возврата последняя свеча пропадала бы
    с экрана при каждом прогоне и появлялась снова только через минуту.
    """

    async def body(run: LiveRun):
        await run.minute(NEXT_MINUTE)  # 12:00 — бар набирается, свеча нарисована
        was = len(run.recorded.grown)
        run.port.apply_settings(replace(Settings(), average_period=20))
        await run.port.wait()
        return len(run.recorded.grown) - was, run.recorded.charts[-1].candles[-1].opens_at

    returned, last_of_snapshot = live_session(loop, database, body, values=Settings(), days=0)

    assert last_of_snapshot == NEXT_MINUTE - timedelta(minutes=5), (
        "снимок прогона дошёл до набирающегося бара — прогон решает "
        "по незакрытой свече"
    )
    assert returned == 1, (
        "набирающийся бар не вернулся на график после прогона: последняя свеча "
        "пропадает с экрана на каждой смене настроек"
    )


@pytest.mark.parametrize(
    ("edge_at", "note"),
    [
        (NEXT_MINUTE + timedelta(minutes=10), "свеча левее правого края"),
        (NEXT_MINUTE - timedelta(minutes=10), "закрытый бар правее правого края"),
    ],
)
def test_the_drawing_does_not_argue_with_the_run(loop, database, edge_at, note) -> None:
    """Дорисовка рисует только набирающийся бар и только на своём месте.

    Проверка по состоянию порта, а не по сценарию: оба случая приходят гонкой
    — дорисовка читает базу, а прогон за это же время успевает сдвинуть край
    графика, — и воспроизвести их порядком вызовов нельзя.

    Что стережётся. Свеча **левее** края устарела: уточнить ею последнюю
    значило бы переставить чужую свечу задним числом. **Закрытый** бар правее
    края означает, что прогон его не показал, то есть отбросил как неполный
    и сказал об этом в журнал; рисовать его вопреки той строке дорисовке
    нечего.
    """

    async def body(run: LiveRun):
        await run.worker.put_minutes(
            "MXU6",
            [_one_minute(NEXT_MINUTE + timedelta(minutes=shift)) for shift in range(5)],
            Source.BROKER,
        )
        # Бар [12:00, 12:05) закрыт и уже учтён: закрытие прогоном не сработает.
        run.port._settled_seen = NEXT_MINUTE + timedelta(minutes=5)  # noqa: SLF001
        edge = run.port._edge  # noqa: SLF001 — правый край и есть предмет проверки
        assert edge is not None, "прогон не оставил правого края — проверять нечего"
        run.port._edge = replace(edge, last=edge_at, forming=None)  # noqa: SLF001
        was = (len(run.recorded.grown), len(run.recorded.retouched))
        run.port.live_candle("MXU6")
        await run.port.wait()
        return (len(run.recorded.grown), len(run.recorded.retouched)), was

    now, was = live_session(loop, database, body, values=Settings(), days=0)
    assert now == was, f"{note}: дорисовка спорит с прогоном, было {was}, стало {now}"


# ------------------- живая свеча: чего дорисовка не имеет права делать вовсе
#
# Три проверки ниже добавлены проходом сторожей 04.09.2026: три мутации
# в `HistoryPort` прошли полный прогон (1902 теста) зелёными, то есть на этих
# трёх местах сторожа не стояло.


def test_a_live_minute_does_not_glue_itself_to_a_past_day_on_screen(
    loop, two_day_database
) -> None:
    """Показан прошедший день целиком — живая минута к нему не приклеивается.

    `until` означает «показать день 18.06, а не хвост ряда». Минута, пришедшая
    сегодня, в этот день не входит по определению, и делать с ней на таком
    графике нечего: ни дорисовывать справа (получилась бы свеча из другого дня
    встык), ни гонять прогон (он покажет тот же день заново).

    Мутацией проверено: без отсечения по `until` минута 19.06 доезжает
    до графика 18.06 — прогон уходит в работу, и полный снимок приходит
    заново каждую минуту.
    """

    async def body(run: LiveRun):
        return await run.minute(NEXT_MINUTE)

    moved = live_session(
        loop, two_day_database, body, values=Settings(), days=0, until=DAY,
    )

    assert moved == (0, 0, 0), (
        "живая минута следующего дня дошла до графика прошедшего дня: "
        f"(снимков, добавлено, уточнено) = {moved}"
    )


def test_the_growing_bar_does_not_move_over_to_the_chart_of_another_timeframe(
    loop, database
) -> None:
    """Набирающийся бар возвращается только на свой график, а не на любой.

    Бар [12:00, 12:05) нарисован живой свечой на пятиминутках. Владелец счёта
    переключает размер свечи на пятнадцать минут — это **другой** график:
    последний закрытый бар там [11:45, 12:00), и пятиминутному огрызку
    на нём места нет. Вернуть его значило бы показать на пятнадцатиминутном
    графике свечу длиной в минуту.

    Мутацией проверено: без сверки инструмента и размера свечи в `_keep_forming`
    огрызок переезжает на новый график, и полный прогон (1902 теста) остаётся
    зелёным.
    """

    async def body(run: LiveRun):
        drawn = await run.minute(NEXT_MINUTE)  # 12:00 — бар набирается
        was = len(run.recorded.grown)
        run.port.apply_settings(replace(Settings(), timeframe="15 минут"))
        await run.port.wait()
        return drawn, len(run.recorded.grown) - was, run.recorded.charts[-1]

    drawn, returned, chart = live_session(loop, database, body, values=Settings(), days=0)

    assert drawn == (0, 1, 0), f"пятиминутный бар не нарисован живой свечой: {drawn}"
    assert chart.timeframe == "15 минут", "прогон не переключился на другой размер свечи"
    assert chart.candles[-1].opens_at == NEXT_MINUTE - timedelta(minutes=15), (
        "последний бар пятнадцатиминутного графика не тот, что ожидался: "
        f"{chart.candles[-1].opens_at}"
    )
    assert returned == 0, (
        "пятиминутный огрызок переехал на пятнадцатиминутный график: "
        "на экране свеча, которой на этом графике быть не может"
    )


def test_a_bar_that_closed_and_went_into_the_run_is_not_drawn_a_second_time(
    loop, database
) -> None:
    """Бар закрылся и попал в прогон — второй раз его не прибавляют.

    Граница строгая, и проверяется она ровно на равенстве. Бар [12:00, 12:05)
    сначала нарисован живой свечой как набирающийся, потом добирает все свои
    минуты и попадает в прогон последним. Правый край прогона при этом
    **совпадает** с этим баром: прибавить его ещё раз значило бы показать
    последнюю свечу дважды, вторую — недобранной.

    Минуты 12:01…12:04 записываются мимо `live_candle` намеренно: так выглядит
    минута, пришедшая, пока предыдущая дорисовка ещё читает базу, — тогда
    признак «набирается» до прогона снять некому.

    Мутацией проверено: `<=` → `<` в `_keep_forming` проходит полный прогон
    (1902 теста) зелёным, а на экране появляется задвоенная последняя свеча.
    """

    async def body(run: LiveRun):
        drawn = await run.minute(NEXT_MINUTE)  # 12:00 — бар набирается
        await run.worker.put_minutes(
            "MXU6",
            [_one_minute(NEXT_MINUTE + timedelta(minutes=shift)) for shift in (1, 2, 3, 4)],
            Source.BROKER,
        )
        was = len(run.recorded.grown)
        run.port.apply_settings(replace(Settings(), average_period=20))
        await run.port.wait()
        return drawn, len(run.recorded.grown) - was, run.recorded.charts[-1].candles

    drawn, returned, shown = live_session(loop, database, body, values=Settings(), days=0)

    assert drawn == (0, 1, 0), f"бар не нарисован живой свечой: {drawn}"
    assert shown[-1].opens_at == NEXT_MINUTE, (
        "бар не добрал минуты и в прогон не попал — проверять равенство не на чем"
    )
    assert returned == 0, (
        "закрытый бар прибавлен к графику второй раз: последняя свеча "
        "задвоена, и вторая недобрана"
    )


# ============================ смена инструмента и граница потока (`B-020`)


def test_a_change_of_instrument_reaches_the_quote_stream(loop, database) -> None:
    """Инструмент в окне сменили — звено об этом узнало. Это `B-020`.

    Тикер подписки задавался один раз при сборке, `apply_settings` его
    не трогал, а снимки нового инструмента поток отбрасывал бы как чужие.
    После смены инструмента в окне поток продолжал писать **старый**, молча.

    Зовётся и на неизменившемся инструменте, и это часть контракта: решение
    «менять или нет» принимает тот, кто знает, на что подписан сейчас,
    а порт этого не знает.
    """
    port, _ = replay_history(loop, database)
    asked: list[str] = []
    port.attach_stream(lambda on: None, retarget=asked.append)

    port.apply_settings(replace(Settings(), instrument="SiU6"))
    port.apply_settings(replace(Settings(), instrument="SiU6"))
    port.apply_settings(replace(Settings(), instrument="  MXU6  "))
    loop.run_until_complete(port.wait())

    assert asked == ["SiU6", "SiU6", "MXU6"], (
        f"звено узнало о смене инструмента не так: {asked}"
    )


def test_refused_settings_do_not_move_the_stream(loop, database) -> None:
    """Настройки отвергнуты — подписка остаётся там же, где была.

    Пустой инструмент отвергается вслух (`convert.instrument_of`), и увести
    за ним поток значило бы отписаться от котировок в ответ на опечатку.
    """
    port, _ = replay_history(loop, database)
    asked: list[str] = []
    port.attach_stream(lambda on: None, retarget=asked.append)

    port.apply_settings(replace(Settings(), instrument="   "))
    loop.run_until_complete(port.wait())

    assert asked == [], "поток увели за отвергнутыми настройками"


def test_a_port_without_a_retarget_handler_still_applies_settings(loop, database) -> None:
    """Звена нет вовсе — настройки применяются, а не падают в слоте Qt.

    `--snapshot` и проверки собирают порт без подключения к брокеру. Отказ
    здесь означал бы трассировку из слота окна на каждое «Применить».
    """
    port, recorded = replay_history(loop, database)
    port.attach_stream(lambda on: None)
    port.apply_settings(replace(Settings(), instrument="SiU6"))
    loop.run_until_complete(port.wait())
    assert recorded.settings[-1].instrument == "SiU6"


def test_the_boundary_of_a_never_seen_instrument_comes_from_the_first_stream_minute(
    loop, tmp_path: pathlib.Path
) -> None:
    """Поток дописал туда, куда порт не смотрел, — граница берётся из его минуты.

    Случай, которого до `B-020` не было: тикер подписки брался из настроек
    при сборке, и к первой живой минуте чтение по этому инструменту уже было.
    Теперь поток следует за окном и способен опередить первый взгляд. Тогда
    `coverage.last` показывает уже на минуту потока и границей служить
    не может — а минута первого снимка может (`HistoryPort.stream_leads`).

    Без неё вся настоящая история такого инструмента осталась бы
    неподтверждённой: неполных баров в истории большинство, и с графика
    пропало бы около сорока процентов свечей — громко, но напрасно.
    """
    path = tmp_path / "stream-came-first.sqlite3"
    lead = DAY + timedelta(minutes=300)

    async def go():
        worker = MarketWorker(path)
        port = HistoryPort(worker, values=Settings(), days=0, sanitize=redact)
        recorded = Recorded(port)
        try:
            await _run(port)  # базы ещё нет, первый взгляд отложен
            await worker.open()
            await worker.put_minutes(
                "MXU6", _holed(DAY, drop=DAY + timedelta(minutes=22)), Source.ISS
            )
            port.stream_leads("MXU6", lead)  # поток: подписка открыта с этой минуты
            await worker.put_minutes("MXU6", _stream_tail(lead), Source.BROKER)
            port.live_candle("MXU6")
            await port.wait()
            port.refresh("после потока")
            await port.wait()
        finally:
            await port.aclose()
            await worker.close()
        return recorded

    recorded = loop.run_until_complete(go())

    starts = {candle.opens_at for candle in recorded.charts[-1].candles}
    assert DAY + timedelta(minutes=20) in starts, (
        "неполный бар настоящей истории выброшен из прогона: граница доверия "
        "не взята из минуты первого снимка потока"
    )
    assert lead not in starts, (
        "бар, собранный не из всех минут потока, показан закрытым: граница "
        "уехала правее подписки"
    )


def test_only_the_first_connection_of_a_session_sets_the_boundary(
    loop, tmp_path: pathlib.Path
) -> None:
    """Второе подключение границу не двигает: слева от него лежит дыра обрыва.

    Пара к проверке выше, и без неё та была бы опасна. Поток объявляет минуту
    первого снимка на **каждом** подключении; принять её у второго значило бы
    объявить проверенной историей дыру, оставленную обрывом, — и отдать
    движку бар, собранный не из всех минут. Дыру закрывает догрузка,
    и граница двигается её подтверждением (`history_confirmed`).

    ⚠️ Оба подключения случаются **до** первого чтения базы, и это не декорация
    сценария, а единственное место, где правило видно. Стоит порту хоть раз
    прочитать базу — первый взгляд закрыт, и границу больше не двигает ничто
    (`_Trust.seen`). Случай настоящий: `--stream` на машине, где файла базы
    ещё нет, плюс обрыв связи до первой отрисовки графика.
    """
    path = tmp_path / "second-connection.sqlite3"
    lead = DAY + timedelta(minutes=300)
    again = lead + timedelta(minutes=20)

    async def go():
        worker = MarketWorker(path)
        port = HistoryPort(worker, values=Settings(), days=0, sanitize=redact)
        recorded = Recorded(port)
        try:
            await _run(port)  # базы ещё нет, первый взгляд отложен
            await worker.open()
            await worker.put_minutes(
                "MXU6", _holed(DAY, drop=DAY + timedelta(minutes=22)), Source.ISS
            )
            port.stream_leads("MXU6", lead)            # подключение первое
            await worker.put_minutes("MXU6", _stream_tail(lead), Source.BROKER)
            port.stream_leads("MXU6", again)           # обрыв и подключение второе
            port.live_candle("MXU6")
            await port.wait()
            port.refresh("после переподключения")
            await port.wait()
        finally:
            await port.aclose()
            await worker.close()
        return recorded

    recorded = loop.run_until_complete(go())

    starts = {candle.opens_at for candle in recorded.charts[-1].candles}
    assert lead not in starts, (
        "граница уехала на минуту второго подключения: дыра обрыва объявлена "
        "проверенной историей"
    )
    assert DAY + timedelta(minutes=20) in starts, (
        "с графика ушла и история — проверка выше прошла бы и на пустом ряду"
    )


def _stream_tail(lead: datetime) -> list[Candle]:
    """Десять минут потока с дырой: бар `[lead, lead+5)` заведомо неполон."""
    minutes = [
        candle for candle in _minutes(10, lead)
        if candle.time != lead + timedelta(minutes=2)
    ]
    assert len(minutes) == 9, "дыры в хвосте потока нет — проверка стала бы вакуумной"
    return minutes


# ------------------------------------------- стоимость пункта цены (`B-021`)

class _Exchange:
    """Подставная биржа для порта: считает запросы, отдаёт заготовленное.

    Настоящая биржа в проверках не участвует ни разу — тела ответов и разбор
    проверяет `tests/test_market_point.py`. Здесь проверяется только шов:
    когда порт спрашивает и что делает с ответом.
    """

    def __init__(
        self,
        answer: PointValue | Exception | Callable[[str], PointValue],
        *,
        before: Callable[[str], None] | None = None,
    ) -> None:
        self._answer = answer
        self._before = before
        self.asked: list[str] = []

    async def __call__(self, symbol: str) -> PointValue:
        self.asked.append(symbol)
        if self._before is not None:
            self._before(symbol)
        if isinstance(self._answer, Exception):
            raise self._answer
        if isinstance(self._answer, PointValue):
            return self._answer
        return self._answer(symbol)


#: Ответ биржи про фьючерс на РТС: 1,73774 ₽ в пункте (замер 05.09.2026).
#: Взят намеренно не единицей — на единице тест не отличил бы подстановку
#: с биржи от умолчания программы.
RTS = PointValue(
    "MXU6", 1.737744, "биржей — карточка RIU6 (RFUD): шаг цены 10, "
    "стоимость шага 17,37744 ₽, помечена 2026-09-04 07:00:01"
)

#: Ответ биржи про фьючерс на индекс: ровно рубль. На нём стоит сверка
#: с прототипом — 127 сделок из 127.
INDEX = PointValue(
    "MXU6", 1.0, "биржей — карточка MXU6 (RFUD): шаг цены 25, "
    "стоимость шага 25 ₽, помечена 2026-09-04 07:00:01"
)


def replay_with_exchange(loop, database: pathlib.Path, exchange, **kwargs):
    """То же, что `replay_history`, но с проводкой к бирже."""

    async def go():
        worker = MarketWorker(database)
        if database.exists():
            await worker.open()
        port = HistoryPort(worker, sanitize=redact, **kwargs)
        port.attach_exchange(exchange)
        recorded = Recorded(port)
        try:
            await _run(port)
        finally:
            await port.aclose()
            await worker.close()
        return port, recorded

    return loop.run_until_complete(go())


def _money(port: HistoryPort) -> list[float]:
    """Деньги каждой сделки последнего прогона, в рублях."""
    run = port._run  # noqa: SLF001 — снимок прогона наружу порт не отдаёт
    return [] if run is None else [deal.gross for deal in run.deals]


def _shown_point_values(port: HistoryPort) -> set:
    """Стоимость пункта, с которой посчитана каждая сделка последнего прогона."""
    run = port._run  # noqa: SLF001 — снимок прогона наружу порт не отдаёт
    return set() if run is None else {deal.ruble_per_point for deal in run.deals}


def _shape(port: HistoryPort) -> list[tuple]:
    """Сами сделки без денег: сторона, вход, выход. Стоимость пункта их не трогает."""
    run = port._run  # noqa: SLF001 — то же
    return [] if run is None else [
        (deal.side, deal.entry_time, deal.entry_price, deal.exit_time, deal.exit_price)
        for deal in run.deals
    ]


def test_the_cost_of_a_point_is_asked_from_the_exchange_and_reaches_the_engine(
    loop, database
) -> None:
    """Стережёт точку вызова: без неё величину в настройки класть некому.

    До 05.09.2026 читающая половина существовала (`market.ask_point_value`),
    а спросить было некому: в движок уходило умолчание 1 ₽ за пункт. По `RIU6`
    это занижает итог в 1,74 раза, по `BRV6` — в 869 раз, и ошибка тихая:
    список сделок тот же самый.
    """
    exchange = _Exchange(RTS)
    port, recorded = replay_with_exchange(
        loop, database, exchange, values=Settings(), days=0
    )
    assert exchange.asked == ["MXU6"], (
        f"биржу спросили {exchange.asked} раз вместо одного про MXU6"
    )
    assert port._engine_settings.ruble_per_point == pytest.approx(1.737744)  # noqa: SLF001 — настройки движка наружу не отдаются
    assert recorded.settings[-1].ruble_per_point == pytest.approx(1.737744)
    assert recorded.settings[-1].ruble_per_point_source == RTS.told


def test_a_point_worth_one_ruble_does_not_move_a_single_deal(loop, database) -> None:
    """Стережёт сверку с прототипом: подтверждённая единица меняет ровно ничего.

    ⚠️ Проверка не вакуумна: рядом стоит тот же прогон при 1,73774 ₽ за пункт.
    Сделки в нём **те же самые**, а деньги другие — ровно это и делает ошибку
    в стоимости пункта тихой.
    """
    plain, _ = replay_history(loop, database, values=Settings(), days=0)
    same, _ = replay_with_exchange(
        loop, database, _Exchange(INDEX), values=Settings(), days=0
    )
    other, _ = replay_with_exchange(
        loop, database, _Exchange(RTS), values=Settings(), days=0
    )

    assert _money(plain), "прогон без сделок — сверять нечего"
    assert _money(same) == _money(plain), (
        "подтверждённая биржей единица сдвинула деньги эталонного прогона"
    )
    assert _shape(same) == _shape(plain) == _shape(other), (
        "стоимость пункта изменила сами сделки — она не вправе их трогать"
    )
    assert _money(other) != _money(plain), (
        "1,73774 ₽ за пункт дали те же деньги, что и 1 ₽, — величина "
        "до расчёта не доехала, и проверка выше ничего не значит"
    )


def test_a_silent_exchange_is_said_out_loud_and_names_what_is_used_instead(
    loop, database
) -> None:
    """Стережёт: биржа не ответила — сказано вслух, а не оставлено молча.

    Молчаливая единица хуже отказа: владелец счёта увидит красивое число
    и не узнает, что оно не про его инструмент.
    """
    exchange = _Exchange(RuntimeError("сеть недоступна"))
    _, recorded = replay_with_exchange(
        loop, database, exchange, values=Settings(), days=0
    )
    said = [
        row for row in recorded.appended
        if row.event == "Стоимость пункта не подтверждена"
    ]
    assert said, (
        "биржа промолчала, а журнал — тоже: "
        f"{[row.event for row in recorded.appended]}"
    )
    assert said[0].level is DecisionLevel.WARNING
    assert "сеть недоступна" in said[0].reason
    assert "1 ₽ за пункт" in said[0].reason, (
        f"не названо, какая величина осталась в расчёте: {said[0].reason}"
    )


def test_an_answer_without_a_number_is_said_out_loud_too(loop, database) -> None:
    """Стережёт: «биржа не назвала стоимость шага» — тоже событие для журнала."""
    silent = PointValue("MXU6", None, "у акций колонки STEPPRICE нет вовсе")
    _, recorded = replay_with_exchange(
        loop, database, _Exchange(silent), values=Settings(), days=0
    )
    said = [
        row for row in recorded.appended
        if row.event == "Стоимость пункта не подтверждена"
    ]
    assert said and "STEPPRICE" in said[0].reason


def test_the_same_trouble_is_not_repeated_on_every_pass(loop, database) -> None:
    """Стережёт: одна беда — одна строка, а не строка на каждый закрытый бар.

    Живой ход делает проход на каждом баре. Повтор одной и той же строки
    похоронил бы под собой всё остальное в журнале — тот же приём, что
    у причины остановки робота.
    """

    async def go():
        worker = MarketWorker(database)
        await worker.open()
        port = HistoryPort(worker, values=Settings(), days=0, sanitize=redact)
        recorded = Recorded(port)
        port.attach_exchange(_Exchange(RuntimeError("сеть недоступна")))
        try:
            for number in range(3):
                # Передышка обнуляется руками: иначе второй проход
                # не пошёл бы на биржу вовсе и повтор было бы нечем вызвать.
                port._point.asked_at.clear()  # noqa: SLF001 — иначе не воспроизвести
                port.refresh(f"проход {number}")
                await port.wait()
        finally:
            await port.aclose()
            await worker.close()
        return recorded

    recorded = loop.run_until_complete(go())
    said = [
        row for row in recorded.appended
        if row.event == "Стоимость пункта не подтверждена"
    ]
    assert len(said) == 1, f"одна и та же беда сказана {len(said)} раз"


def test_the_exchange_is_not_asked_more_often_than_the_gap(loop, database) -> None:
    """Стережёт передышку: три прохода подряд — один запрос к бирже.

    Без передышки живой ход на минутках спрашивал бы карточку каждую минуту,
    а меняется она на клиринге — дважды за торговый день.
    """

    async def go():
        worker = MarketWorker(database)
        await worker.open()
        port = HistoryPort(worker, values=Settings(), days=0, sanitize=redact)
        exchange = _Exchange(INDEX)
        port.attach_exchange(exchange)
        try:
            for number in range(3):
                port.refresh(f"проход {number}")
                await port.wait()
        finally:
            await port.aclose()
            await worker.close()
        return exchange

    exchange = loop.run_until_complete(go())
    assert exchange.asked == ["MXU6"], (
        f"за три прохода биржу спросили {len(exchange.asked)} раз"
    )


def test_a_port_without_the_wiring_never_goes_to_the_exchange(loop, database) -> None:
    """Стережёт: без проводки порт в сеть не ходит — и проверки остаются offline.

    Сеть внутри порта по умолчанию означала бы, что каждая проверка,
    собирающая порт, уходит в интернет: девять секунд повторов на проверку
    и красный прогон на машине без связи.
    """
    port, _ = replay_history(loop, database, values=Settings(), days=0)
    assert port._point.ask is None  # noqa: SLF001 — состояние наружу не отдаётся
    # ⚠️ Смотрится **отметка о попытке**, а не поле задачи: `aclose` обнуляет
    # задачу, и проверка по нему была бы зелёной при любом устройстве порта.
    # Отметка ставится ровно там, где запрос заводится, и переживает закрытие.
    assert port._point.asked_at == {}, (  # noqa: SLF001 — то же
        "порт собрался спрашивать биржу, хотя спрашивать его не просили"
    )
    assert port._engine_settings.ruble_per_point == 1.0  # noqa: SLF001 — то же


def test_the_wait_does_not_return_before_a_slow_answer_is_applied(
    loop, database
) -> None:
    """Стережёт круги в `wait`: ответ биржи приходит **после** прохода.

    На настоящей сети запрос стоит около секунды, а проход по базе — миллисекунды,
    поэтому ответ почти всегда приходит уже после прохода и заводит следующий.
    `wait`, вернувшаяся между ними, отдала бы снимок экрана и проверку картинке,
    посчитанной по неподтверждённой единице.

    Медленность здесь **не таймером**: ответ ждёт, пока закончится сам проход,
    поэтому порядок событий один и тот же на любой машине.
    """

    async def go():
        worker = MarketWorker(database)
        await worker.open()
        port = HistoryPort(worker, values=Settings(), days=0, sanitize=redact)

        async def slow(_symbol: str) -> PointValue:
            while port._task is not None and not port._task.done():  # noqa: SLF001 — иначе не воспроизвести порядок
                await asyncio.sleep(0)
            return RTS

        port.attach_exchange(slow)
        try:
            port.refresh("проход")
            await port.wait()
            # ⚠️ Смотрится **прогон**, а не поле настроек: настройки движка
            # меняются в тот же миг, когда ответ применён, а деньги отчёта —
            # только следующим проходом. Проверка по полю была бы зелёной
            # и без всякого ожидания.
            return _shown_point_values(port)
        finally:
            await port.aclose()
            await worker.close()

    values = loop.run_until_complete(go())
    assert values, "прогон без сделок — сверять нечего"
    assert sorted(values) == [pytest.approx(1.737744)], (
        "wait вернулась раньше, чем прогон пересчитался с подтверждённой "
        f"биржей величиной: в сделках стоит {values}. Снимок экрана и проверка "
        "поймали бы деньги, посчитанные по умолчанию 1 ₽ за пункт"
    )


def test_an_answer_about_an_instrument_no_longer_shown_is_not_applied(
    loop, database
) -> None:
    """Стережёт: пока шёл запрос, инструмент сменился — чужое число не подставлено.

    Стоимость пункта одного контракта под именем другого — та же беда,
    от которой карточка выбирается по режиму торгов (`B-016`), только
    с другой стороны.
    """
    values = Settings()

    async def go():
        worker = MarketWorker(database)
        await worker.open()
        port = HistoryPort(worker, values=values, days=0, sanitize=redact)
        recorded = Recorded(port)

        def switch(symbol: str) -> None:
            if symbol == "MXU6":
                port.apply_settings(values.replace(instrument="SiU6"))

        # По MXU6 биржа отвечает «Брентом», по SiU6 — единицей. Применённый
        # ответ про MXU6 виден сразу: 868,872 не спутать ни с чем.
        def answer(symbol: str) -> PointValue:
            return PointValue(symbol, 868.872 if symbol == "MXU6" else 1.0, f"биржей {symbol}")

        port.attach_exchange(_Exchange(answer, before=switch))
        try:
            port.refresh("проход")
            await port.wait()
        finally:
            await port.aclose()
            await worker.close()
        return recorded

    recorded = loop.run_until_complete(go())
    stale = [
        seen for seen in recorded.settings
        if seen.ruble_per_point == pytest.approx(868.872)
    ]
    assert not stale, "величина чужого инструмента доехала до настроек"


# ------------------------------------ текст про предохранители (`D-037`)

def test_the_journal_line_about_guards_names_every_guard_that_exists() -> None:
    """Стережёт расхождение текста и состояния: заведут четвёртый — упадёт здесь.

    Текст `NO_GUARDS` уходит в журнал уровнем WARNING при каждом старте
    и после каждой правки предохранителя. Прежняя редакция говорила «в движке
    отсутствуют», и это стало ложью в тот день, когда их провели (`D-037`).
    Связь текста с состоянием держалась комментарием, то есть ничем.
    """
    from app.convert import _GUARDS, _WINDOW_TOLD

    # Регистр не сверяется: в окне подпись стоит с большой буквы, а в строке
    # журнала — внутри фразы. Требовать совпадения регистра значило бы
    # заставлять писать «Дневной лимит убытка» посреди предложения.
    said = NO_GUARDS.lower()
    for guard in _GUARDS.values():
        label = _WINDOW_TOLD[guard.switch].label
        assert label.lower() in said, (
            f"предохранитель «{label}» заведён в app/convert.py, а строка "
            "журнала о нём молчит. Дописать его в NO_GUARDS "
            "(app/port.py) — иначе владелец счёта прочтёт неполный список"
        )


def test_the_journal_line_about_guards_matches_the_defaults() -> None:
    """Стережёт вторую половину текста: «все три галочки сняты по умолчанию».

    Умолчание, поменянное на «включено», сделало бы строку ложью — и заодно
    включило бы остановку торговли, которой владелец счёта не просил.
    """
    from app.convert import _GUARDS

    values = Settings()
    armed = [guard.switch for guard in _GUARDS.values() if getattr(values, guard.switch)]
    assert not armed, (
        f"предохранители {armed} включены умолчанием, а строка журнала обещает "
        "обратное. Либо вернуть умолчание, либо переписать NO_GUARDS "
        "(app/port.py) — расходиться им нельзя"
    )
    assert "по умолчанию сняты" in NO_GUARDS


def test_the_journal_line_about_guards_no_longer_claims_they_are_absent() -> None:
    """Стережёт возврат прежней лжи: «в движке отсутствуют» — это `D-037`.

    Проверка на подстроку, а не на смысл, и это осознанно: вернуть текст
    целиком проще всего копированием, и ловится это ровно так.
    """
    for lie in ("отсутствуют", "ничего не ограничивают", "не проверяются"):
        assert lie not in NO_GUARDS, (
            f"в строке про предохранители снова стоит «{lie}» — они проведены "
            "и работают от галочек (D-030)"
        )


# ================================================================== B-029
# Внутренние фразы не выходят в окно


#: Приметы текста, написанного разработчиком для разработчика. Не «слова,
#: которые нельзя», а **формы**: вызов метода, ожидание корутины, имя класса
#: из кода, трассировка. Ни одна из них не может стоять в фразе, обращённой
#: к владельцу счёта, — он не программист и кода не видел.
#:
#: ⚠️ Список нарочно не про конкретную строку `B-029`. Стережётся класс
#: беды: любой отказ слоя данных, пересказанный окну как есть.
DEVELOPER_SPEAK = (
    "await ",
    "worker.",
    "MarketWorker",
    "CandleStore",
    "RuntimeError",
    "Traceback",
    "sqlite3.",
    "asyncio",
    "()`",
)


def _texts(value: object, out: list[str]) -> None:
    """Все строки внутри доехавшего до окна объекта — рекурсивно."""
    if isinstance(value, str):
        out.append(value)
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            _texts(item, out)
        return
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        for field in fields(value):
            _texts(getattr(value, field.name), out)


def _all_texts(payload: tuple[object, ...], out: list[str]) -> None:
    """Строки из всего, что уехало одним сигналом."""
    for item in payload:
        _texts(item, out)


def _everything_said(port: HistoryPort) -> list[str]:
    """Подписаться на **все** сигналы порта и копить каждую сказанную строку.

    Обходом, а не перечислением: сигнал, добавленный завтра, попадёт под
    проверку сам. Перечисление руками — ровно тот способ, которым в этом
    проекте уже забывали половину выходов.
    """
    from PySide6.QtCore import SignalInstance

    said: list[str] = []
    for name in dir(type(port)):
        if name.startswith("_"):
            continue
        signal = getattr(port, name, None)
        if isinstance(signal, SignalInstance):
            signal.connect(lambda *payload: _all_texts(payload, said))
    return said


def test_no_developer_speak_reaches_the_window(loop, database) -> None:
    """Стережёт `B-029`: внутренняя фраза слоя данных не доезжает до окна.

    Владелец счёта увидел в диалоге загрузки: «поток данных не запущен:
    сначала `await worker.open()`. Соединение с базой создаётся внутри
    этого потока…». Это текст для нас; ему он не говорит ничего, а `await`
    посреди окна читается как вывалившийся кусок кода.

    ⚠️ Ломается **настоящей** бедой, а не подставным исключением: поток
    данных закрывается посреди работы — так бывает при выходе из программы,
    когда окно ещё живо и успевает попросить. Слой данных отвечает своим
    текстом (`market/worker.py::_CLOSED`), и порт обязан пересказать его
    словами.
    """
    async def go() -> list[str]:
        worker = MarketWorker(database)
        await worker.open()
        port = HistoryPort(worker, values=Settings(instrument="MXU6"),
                           days=0, sanitize=redact)
        said = _everything_said(port)
        await worker.close()  # база закрыта, а окно ещё спрашивает
        port.request_history_facts("MXU6")
        port.request_backtest_options()
        port.load_history(HistoryLoadRequest(symbol="MXU6", days=3))
        port.refresh("после закрытия базы")
        for _ in range(200):
            await asyncio.sleep(0.01)
            if len(said) > 3:
                break
        await port.aclose()
        return said

    said = loop.run_until_complete(go())
    assert said, "порт не сказал ничего — проверять нечего, сторож вакуумный"
    for line in said:
        for mark in DEVELOPER_SPEAK:
            assert mark not in line, (
                f"в окно уехал текст для разработчика («{mark}»):\n{line}"
            )
