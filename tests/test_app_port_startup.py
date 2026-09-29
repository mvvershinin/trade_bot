"""Сборка порта на старте и плашка отрезка: `B-050`, `D-091`, `D-067`.

Что здесь стережётся
--------------------
* **B-050.** Файл настроек принёс вариант, которого движок не принимает.
  Программа открывается, вариант подменён умолчанием, остальное прочитано,
  а человеку сказано строкой уровня «ошибка».
* **D-091.** Остановку из базы прочитать не удалось. Робот стоит, а не
  поднимается неостановленным, и человек видит причину.
* **D-067.** Плашка закреплённого отрезка называет число торговых дней —
  то же, что в отчёте, и то, что лежит в базе фикстуры.

⚠️ Изоляция: своя база во временном каталоге на каждую проверку, сети нет.
Цикл событий — общий `loop` из `conftest.py`, поверх Qt, как в `app/main.py`.
"""

from __future__ import annotations

import dataclasses
import enum
import os
import pathlib
from datetime import timedelta
from typing import Any

import pytest

os.environ.setdefault("QT_API", "pyside6")  # до первого импорта qasync

from app import convert
from app.port import HistoryPort
from market import CandleStore, MarketWorker, Source, StoredHalt
from market.journal import HaltKind, redact
from tests.test_ui_backtest import DAY, TRADING_DAYS, _minutes, _request, with_port
from ui.models import (
    AfterTakeProfit,
    DecisionLevel,
    DecisionRow,
    Mode,
    RobotState,
    RunOrigin,
    Settings,
)

SYMBOL = "MXU6"


@pytest.fixture
def database(tmp_path: pathlib.Path) -> pathlib.Path:
    """Три торговых дня с разрывом — тот же набор, что у проверок прогона."""
    path = tmp_path / "candles.sqlite3"
    with CandleStore(path) as store:
        for shift in TRADING_DAYS:
            store.put_minutes(
                SYMBOL, _minutes(300, DAY + timedelta(days=shift)), Source.ISS
            )
    return path


class _Heard:
    """Что порт отправил в окно: снимки, журнал, эхо настроек, графики."""

    def __init__(self, port: HistoryPort) -> None:
        self.states: list[RobotState] = []
        self.journals: list[tuple[DecisionRow, ...]] = []
        self.settings: list[Settings] = []
        self.charts: list[object] = []
        port.state_changed.connect(self.states.append)
        port.decisions_replaced.connect(lambda rows: self.journals.append(tuple(rows)))
        port.settings_applied.connect(self.settings.append)
        port.chart_replaced.connect(self.charts.append)

    def rows(self, event: str) -> list[DecisionRow]:
        """Строки последнего журнала с таким событием."""
        return [row for row in self.journals[-1] if row.event == event]


class _UnreadableHalt(MarketWorker):
    """Поток данных, у которого таблица остановки не читается.

    Вход подставной (правило 14): отказ базы изображён исключением, всё
    остальное — настоящая база фикстуры.
    """

    async def standing_halt(self) -> tuple[StoredHalt, ...]:
        raise OSError("disk I/O error")


# ---------------------------------------------------------------- B-050


def test_a_settings_file_with_an_unfinished_variant_still_opens(loop, database) -> None:
    """B-050: «сразу восстановить позицию» в файле — программа открывается.

    Поведение: порт собирается, вариант подменён умолчанием программы,
    остальные прочитанные настройки в силе, прогон идёт, а в журнале —
    строка уровня «ошибка» с причиной из отказа движка.

    ⚠️ Мутации: вернуть `convert.engine_settings` без обработки
    в конструктор — порт не собирается; убрать `_take_startup_substitution` —
    подмена молчит (правило 13); не подменять `_values` — эхо отдаёт окну
    вариант, который движок отвергнет, и прогон отказывает.
    """
    chosen = Settings(
        instrument=SYMBOL,
        average_period=21,
        after_take_profit=AfterTakeProfit.RESTORE_AT_ONCE,
    )

    async def go() -> _Heard:
        worker = MarketWorker(database)
        await worker.open()
        port = HistoryPort(worker, values=chosen, days=0, sanitize=redact)
        heard = _Heard(port)
        try:
            port.refresh("запуск программы")
            await port.wait()
            port.request_settings()
        finally:
            await port.aclose()
            await worker.close()
        return heard

    heard = loop.run_until_complete(go())

    echoed = heard.settings[-1]
    assert echoed.after_take_profit is Settings().after_take_profit, (
        f"окну отдан вариант, которого движок не принимает: {echoed.after_take_profit}"
    )
    assert (echoed.instrument, echoed.average_period) == (SYMBOL, 21), (
        "вместе с одним негодным полем пропали прочитанные настройки"
    )
    assert heard.charts, "прогон по подменённым настройкам не дошёл до графика"
    said = heard.rows("Настройки не приняты")
    assert said, "подмена настройки из файла прошла молча"
    assert said[0].level is DecisionLevel.ERROR
    assert "Сразу восстановить позицию" in said[0].reason, said[0].reason
    assert AfterTakeProfit.STOP_FOR_THE_DAY.label in said[0].reason, (
        f"не сказано, что поставлено вместо: {said[0].reason!r}"
    )


# ---------------------------------------------------------------- D-091


def _start(loop, database: pathlib.Path, worker_class: type[MarketWorker]) -> _Heard:
    """Старт как в `app/main.py`: остановка спрошена, прогон, потом поток."""

    async def go() -> _Heard:
        worker = worker_class(database, sanitize=redact)
        await worker.open()
        port = HistoryPort(
            worker, values=Settings(instrument=SYMBOL), days=0, sanitize=redact
        )
        port.attach_stream(lambda on: None)  # подключение «удалось»
        heard = _Heard(port)
        try:
            # Порядок `app/main.py`: остановка спрошена до первого прогона,
            # поток включается позже, когда связь с брокером поднялась.
            await port.restore_halt()
            port.refresh("запуск программы")
            await port.wait()
            port.stream(True)
            await port.wait()
        finally:
            await port.aclose()
            await worker.close()
        return heard

    heard: _Heard = loop.run_until_complete(go())
    return heard


def _live(heard: _Heard) -> bool:
    """Шёл ли живой ход движка: его строки помечены «симуляция на потоке»."""
    return any(row.origin is RunOrigin.PAPER for row in heard.journals[-1])


def test_the_live_run_starts_when_the_halt_is_read(loop, database) -> None:
    """Контроль к следующей проверке: при прочитанной базе живой ход идёт.

    Без него признак «живого хода нет» мог бы молчать по построению —
    например, если на этой фикстуре живой ход не собирается вообще.
    """
    heard = _start(loop, database, MarketWorker)
    assert not heard.states[-1].halted
    assert _live(heard), "живой ход не собрался и без всякой остановки"


def test_an_unreadable_halt_stops_the_robot(loop, database) -> None:
    """D-091: остановка не прочиталась — робот стоит, и человек видит почему.

    Поведение: неизвестно, стоял ли запрет, — значит стоял. Окно показывает
    остановку вида «состояние счёта неизвестно», живого хода нет, в журнале
    строка уровня «ошибка» с причиной отказа базы.

    ⚠️ Мутации: вернуть прежний `return` после строки журнала (робот
    поднимается неостановленным) и убрать строку журнала (молчание).
    """
    heard = _start(loop, database, _UnreadableHalt)

    state = heard.states[-1]
    assert state.halted, "база не прочиталась, а робот поднялся неостановленным"
    assert [cause.kind for cause in state.halt_causes] == [HaltKind.ACCOUNT.label], (
        f"вид остановки не тот: {state.halt_causes}"
    )
    assert not _live(heard), "живой ход собран при непрочитанной остановке"
    said = heard.rows("Остановка робота и база")
    assert said, "про непрочитанную остановку не сказано ни строки"
    assert said[0].level is DecisionLevel.ERROR
    assert "disk I/O error" in said[0].reason
    assert "остановлен сейчас" in said[0].reason, said[0].reason


# ---------------------------------------------------------------- D-067


def test_the_pinned_span_names_its_trading_days(loop, database) -> None:
    """D-067: плашка отрезка называет число торговых дней.

    Поведение: число в плашке равно числу дней с торгами в базе фикстуры
    (вход извне: `TRADING_DAYS`, три дня с разрывом, а не семь дней отрезка)
    и совпадает с числом отчёта.

    ⚠️ Мутация: не передавать число в `_span_note` — плашка без числа;
    считать дни календарём отрезка — семь или восемь вместо трёх.
    """

    async def work(port, heard):
        port.run_backtest(_request())
        await port.wait()
        span = heard.states[-1].history_span
        report = heard.reports[-1]
        assert f"торговых дней: {len(TRADING_DAYS)}," in span, (
            f"плашка не называет число торговых дней: {span!r}"
        )
        assert report.trading_days == len(TRADING_DAYS)

    with_port(loop, database, work)


def test_what_the_engine_refuses_is_exactly_what_the_window_does_not_offer() -> None:
    """B-050: движок отвергает ровно те варианты, что окно не даёт выбрать.

    Поведение: для каждого поля-перечисления настроек и каждого его значения
    отказ `convert.engine_settings` случается тогда и только тогда, когда
    у значения непуст `unavailable`. Иначе новый отказ в `app/convert.py`
    без пометки в `ui/models.py` вернул бы `B-050`: окно предложило бы
    вариант, а файл с ним снова не дал бы программе открыться —
    `_startup_settings` не нашёл бы, что подменить.

    ⚠️ Мутация: стереть текст в `AfterTakeProfit.unavailable`.
    """
    mismatched = []
    for item in dataclasses.fields(Settings):
        default = getattr(Settings(), item.name)
        if not isinstance(default, enum.Enum):
            continue
        for value in type(default):
            try:
                changes: dict[str, Any] = {item.name: value}
                chosen = dataclasses.replace(Settings(), **changes)
                convert.engine_settings(chosen, Mode.REVERSE)
                refused = False
            except convert.SettingsRefused:
                refused = True
            if refused != bool(getattr(value, "unavailable", "")):
                mismatched.append(f"{item.name}={value.name}: отказ={refused}")
    assert not mismatched, f"отказ движка и окно расходятся: {mismatched}"


def test_every_offered_variant_gives_the_engine_its_own_behaviour() -> None:
    """B-050: вариант, который окно предлагает, движок выражает по-своему.

    Поведение: у поля-перечисления, которое движок различает (хотя бы два
    значения дают разные настройки движка), каждое **предлагаемое** значение
    (пустой `unavailable`) даёт свои настройки движка, не совпадающие
    ни с одним соседним. Вход извне — сами настройки движка, а не список
    `unavailable`: предыдущая проверка сверяет отказ с пометкой, а отказ
    теперь берётся из пометки, и вдвоём они молчат, если пометку стереть.

    ⚠️ Мутация: стереть текст в `AfterTakeProfit.unavailable`. «Сразу
    восстановить позицию» тогда принимается и молча становится «Ждать
    нового сигнала средней» — другие сделки под чужим названием.
    """
    collided: list[str] = []
    for item in dataclasses.fields(Settings):
        default = getattr(Settings(), item.name)
        if not isinstance(default, enum.Enum):
            continue
        made: dict[str, Any] = {}
        for value in type(default):
            if getattr(value, "unavailable", ""):
                continue
            changes: dict[str, Any] = {item.name: value}
            chosen = dataclasses.replace(Settings(), **changes)
            made[value.name] = convert.engine_settings(chosen, Mode.REVERSE)
        if len(set(map(repr, made.values()))) < 2:
            continue  # поле движку безразлично — различать нечего
        names = list(made)
        for index, one in enumerate(names):
            collided.extend(
                f"{item.name}: {one} = {other}"
                for other in names[index + 1:]
                if made[one] == made[other]
            )
    assert not collided, f"предлагаемые варианты движок не различает: {collided}"
