"""Шаг цены берётся у биржи, а не у человека и не из файла настроек.

Решение владельца счёта 05.10.2026 (отменяет 04.10, решение 0062 §3).
Карточка инструмента подставная (правило 14): в сеть проверки не ходят.
Шаг в карточке — **7**: его нет ни в умолчаниях, ни в файлах проверок,
и зашитое где-либо число (25, 10, 1) с ним не совпадёт.

Что стережётся, словами:

1. Шаг из карточки доезжает до движка (предел заявки на выход) и до
   издержек прогона (проскальзывание в шагах).
2. Нет карточки — нет связи или биржа отказала — плашка «биржа ещё не
   сообщила шаг цены» в снимке состояния, а движок идёт «по рынку».
   Мутация молчания: `step_wait` возвращает пусто — проверка падает.
3. Шаг из файла настроек не действует и биржевого не перебивает;
   расхождение — одна строка журнала, а не строка на каждый проход.
4. «Применить» из окна, открытого до ответа биржи, шаг не стирает.
5. Кусок склейки идёт со своим шагом — и в прогоне, и на стыке.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_API", "pyside6")  # до первого импорта qasync

from app import convert, stitched_view
from app import port as port_module
from app.port import HistoryPort
from backtest import Costs
from backtest import stitched as stitched_module
from backtest.stitched import Piece, replay_pieces
from engine import TimeExitOrder
from market import MarketWorker, PointValue
from market.journal import redact
from tests.exchange_card import card, exchange
from tests.test_app_port_startup import SYMBOL, _Heard, database  # noqa: F401 — фикстура
from tests.test_backtest_stitched import NEW, OLD, PERIOD, SETTINGS, WARMUP, _build
from ui.models import STEP_WAIT, Settings, TimeExitKind

#: Шаг в подставной карточке. Не 25 и не 10: см. шапку.
STEP = 7.0

#: Выход с предельной ценой и проскальзывание — оба зависят от шага.
CHOSEN = Settings(
    instrument=SYMBOL, time_exit_order=TimeExitKind.LIMIT, slippage_steps=1.0,
)


def _started(loop, database: pathlib.Path, values: Settings, ask=None, then=None):  # noqa: F811
    """Порт на базе фикстуры: прогон, ответ биржи, если подан, — и `then`."""

    async def go():
        worker = MarketWorker(database)
        await worker.open()
        port = HistoryPort(worker, values=values, days=0, sanitize=redact)
        heard = _Heard(port)
        if ask is not None:
            port.attach_exchange(ask)
        try:
            port.refresh("запуск программы")
            await port.wait()
            if then is not None:
                await then(port)
        finally:
            await port.aclose()
            await worker.close()
        return heard, port

    return loop.run_until_complete(go())


def test_the_exchange_step_reaches_the_engine_and_the_costs(loop, database) -> None:  # noqa: F811
    """Стережёт: шаг из карточки — в движке и в издержках прогона, плашки нет.

    ⚠️ Мутации: не запоминать шаг из карточки (`_learn_step`) или взять
    зашитое число — шаг движка не 7, проверка падает.
    """
    heard, port = _started(loop, database, CHOSEN, exchange(STEP))
    engine = port._engine_settings  # noqa: SLF001 — что получил движок
    assert engine.price_step == STEP, engine
    assert engine.time_exit_order is TimeExitOrder.LIMIT, engine
    run = port._run  # noqa: SLF001 — снимок прогона наружу порт не отдаёт
    assert run is not None and run.costs.price_step == STEP, run
    assert run.costs.slippage_steps == 1.0, run.costs
    assert port.values.price_step == STEP
    assert heard.states[-1].step_wait == "", heard.states[-1].step_wait


def test_without_a_card_the_plaque_says_the_exchange_has_not_told(loop, database) -> None:  # noqa: F811
    """Стережёт: без карточки — плашка в снимке, движок «по рынку», выбор не тронут.

    ⚠️ Мутация молчания: `step_wait` возвращает пусто — плашки нет,
    проверка падает. Мутация подмены выбора: класть «по рынку» в `values` —
    падает третье утверждение.
    """
    heard, port = _started(loop, database, CHOSEN)
    assert heard.states, "порт не отдал ни одного снимка — проверка вакуумна"
    said = heard.states[-1].step_wait
    assert said == (
        f"{STEP_WAIT} — пока выход по концу окна идёт по рынку "
        "и проскальзывание в отчёте не учитывается."
    ), said
    engine = port._engine_settings  # noqa: SLF001 — что получил движок
    assert engine.time_exit_order is TimeExitOrder.MARKET, engine
    assert port.values.time_exit_order is TimeExitKind.LIMIT, "выбор человека подменён"


def test_a_failing_exchange_leaves_the_plaque_up(loop, database) -> None:  # noqa: F811
    """Стережёт: биржа не ответила — плашка стоит, программа не падает."""

    async def broken(_symbol: str) -> PointValue:
        raise ConnectionError("сеть недоступна")

    heard, _port = _started(loop, database, CHOSEN, broken)
    assert heard.states[-1].step_wait.startswith(STEP_WAIT), heard.states[-1]


def test_the_file_step_does_not_override_the_exchange_one(loop, database) -> None:  # noqa: F811
    """Стережёт: шаг 10 из файла не действует; биржевой 7 — да; расхождение — одна строка.

    ⚠️ Мутации: брать шаг из файла при запуске — без карточки движок
    получает 10 (первая часть падает); говорить расхождение на каждом
    ответе — строк две (вторая часть падает).
    """
    filed = CHOSEN.replace(price_step=10.0)
    _heard, alone = _started(loop, database, filed)
    engine = alone._engine_settings  # noqa: SLF001 — что получил движок
    assert engine.price_step == 0.0 and engine.time_exit_order is TimeExitOrder.MARKET, (
        f"шаг из файла настроек действует без биржи: {engine}"
    )

    async def ask_again(port: HistoryPort) -> None:
        port._point.asked_at.clear()  # noqa: SLF001 — снять передышку запроса
        port.refresh("второй проход")
        await port.wait()

    heard, port = _started(loop, database, filed, exchange(STEP), ask_again)
    assert port._engine_settings.price_step == STEP  # noqa: SLF001
    rows = [row for row in heard.journals[-1] if row.event == "Шаг цены взят у биржи"]
    assert len(rows) == 1, [row.reason for row in rows]
    assert "10" in rows[0].reason and "7" in rows[0].reason, rows[0].reason


def test_applying_from_a_dialog_opened_before_the_card_keeps_the_step(loop, database) -> None:  # noqa: F811
    """Стережёт: шаг из пришедших настроек не берётся — ни ноль, ни выдуманный.

    Окно настроек, открытое до ответа биржи, держит в поле ноль и пришлёт
    его с «ОК». ⚠️ Мутация: брать шаг из `apply_settings` как есть — шаг
    движка 0, проверка падает.
    """
    seen: list[float] = []

    async def apply_twice(port: HistoryPort) -> None:
        for step in (0.0, 99.0):
            port.apply_settings(port.values.replace(price_step=step, average_period=21))
            await port.wait()
            seen.append(port._engine_settings.price_step)  # noqa: SLF001

    _started(loop, database, CHOSEN, exchange(STEP), apply_twice)
    assert seen == [STEP, STEP], seen


def test_each_stitched_contract_gets_its_own_step(loop, database, monkeypatch) -> None:  # noqa: F811
    """Стережёт: склейке уходит шаг каждого контракта — из его карточки.

    Второй контракт порт спрашивает сам (`_want_steps`) и пересчитывает
    прогон, когда шаг пришёл. ⚠️ Мутация: не передавать `steps` —
    словарь пуст, проверка падает.
    """
    real_series = port_module.HistoryPort._series  # noqa: SLF001 — подмена в тесте
    given: list[dict[str, float]] = []
    pieces = (SimpleNamespace(symbol="MXH6"), SimpleNamespace(symbol=SYMBOL))

    async def stitched_series(self, frame, symbol, timeframe):
        candles, _ = await real_series(self, frame, symbol, timeframe)
        return candles, SimpleNamespace(
            candles=candles, seams=(), symbols="подставная", pieces=pieces,
        )

    async def stitched_run(stitch, values, engine, *, steps=None):
        given.append(dict(steps or {}))
        return await port_module.replay(
            stitch.candles,
            convert.chosen_algorithm(values).build(convert.strategy_settings(values)),
            engine, costs=convert.run_costs(values),
        )

    monkeypatch.setattr(port_module.HistoryPort, "_series", stitched_series)
    monkeypatch.setattr(port_module.stitched_view, "run", stitched_run)
    monkeypatch.setattr(port_module.stitched_view, "as_history", lambda run, costs: run)

    async def ask(symbol: str) -> PointValue:
        return card(symbol, 11.0 if symbol == "MXH6" else STEP)

    _heard, _port = _started(loop, database, CHOSEN, ask)
    assert given and given[-1] == {"MXH6": 11.0, SYMBOL: STEP}, given


def test_a_stitched_piece_runs_and_closes_on_the_seam_with_its_own_step() -> None:
    """Стережёт: кусок со своим шагом — в издержках прогона и в цене закрытия на стыке.

    Проскальзывание в один шаг против позиции: лонг на стыке закрывается
    на шаг **своего** контракта ниже последней цены. ⚠️ Мутация: отдать
    `_seam_deal` общие издержки — цена закрытия уходит на 5, а не на 7.
    """
    costs = Costs(commission_per_side=17.0, price_step=5.0, slippage_steps=1.0)
    old = Piece("MXU6", date(2026, 9, 16), date(2026, 9, 16), (), OLD, 0, price_step=STEP)
    new = Piece("MXZ6", date(2026, 9, 17), date(2026, 9, 17), WARMUP, NEW, PERIOD)
    run = asyncio.run(replay_pieces([old, new], _build, SETTINGS, costs))
    assert run.pieces[0].run.costs.price_step == STEP
    assert run.pieces[1].run.costs.price_step == 5.0, "кусок без шага взял чужой"
    seam = run.pieces[0].seam
    assert seam is not None, "сценарий: лонг к стыку не открыт"
    assert seam.exit_price == OLD[-1].close - STEP, seam


def test_the_card_is_asked_when_the_history_load_ends(loop, tmp_path) -> None:
    """Стережёт: запрос карточки, отложенный ради загрузки истории, не теряется.

    Путь владельца 05.10.2026: при запуске шла загрузка, запрос откладывался
    «до следующего прохода», а прохода без живого потока не было — плашка
    «биржа не сообщила шаг» стояла при исправной сети. ⚠️ Мутация: убрать
    `add_done_callback` в `_ask_point_value` — запроса нет, проверка падает.
    """
    asked: list[str] = []

    async def ask(symbol: str) -> PointValue:
        asked.append(symbol)
        return card(symbol, STEP)

    async def go() -> None:
        worker = MarketWorker(tmp_path / "candles.sqlite3")
        port = HistoryPort(worker, values=CHOSEN, days=0, sanitize=redact)
        port.attach_exchange(ask)
        gate = asyncio.Event()

        async def loading() -> None:
            await gate.wait()

        load = asyncio.ensure_future(loading())
        port._history.load = load  # noqa: SLF001 — загрузка изображена задачей
        try:
            port._ask_point_value(SYMBOL)  # noqa: SLF001
            await asyncio.sleep(0)
            assert asked == [], "сценарий: запрос не отложен ради загрузки"
            gate.set()
            await load
            for _ in range(5):
                await asyncio.sleep(0)
            task = port._point.task  # noqa: SLF001
            if task is not None:
                await task
        finally:
            port._history.load = None  # noqa: SLF001
            await port.aclose()
            await worker.close()

    loop.run_until_complete(go())
    assert asked == [SYMBOL], f"загрузка кончилась, а карточку так и не спросили: {asked}"


def test_a_stitched_piece_gives_its_own_step_to_the_engine(monkeypatch) -> None:
    """Стережёт: шаг куска доходит до **движка** куска, а не только до издержек.

    Предел заявки на выход по концу окна считает движок в шагах цены;
    кусок, прогнанный с шагом соседа, ставил бы предел не там. ⚠️ Мутация:
    отдать движку общие настройки, а шаг — только издержкам, — проверка падает.
    """
    real = stitched_module.replay
    engines: list[float] = []

    async def spy(bars, module, settings, **kwargs):
        engines.append(settings.price_step)
        return await real(bars, module, settings, **kwargs)

    monkeypatch.setattr(stitched_module, "replay", spy)
    costs = Costs(commission_per_side=17.0, price_step=5.0, slippage_steps=1.0)
    old = Piece("MXU6", date(2026, 9, 16), date(2026, 9, 16), (), OLD, 0, price_step=STEP)
    new = Piece("MXZ6", date(2026, 9, 17), date(2026, 9, 17), WARMUP, NEW, PERIOD)
    asyncio.run(replay_pieces([old, new], _build, replace(SETTINGS, price_step=5.0), costs))
    assert engines == [STEP, 5.0], f"движок куска получил не свой шаг: {engines}"


def test_the_stitched_view_hands_each_contract_its_step() -> None:
    """Стережёт звено порт → склейка: `stitched_view.run` раздаёт шаги по кускам.

    Соседняя проверка (`test_each_stitched_contract_gets_its_own_step`)
    подменяет сам `stitched_view.run` и это звено не видит. ⚠️ Мутация:
    прогнать `stitch.pieces` вместо кусков с шагами — у первого куска шаг
    прогона (5), проверка падает.
    """
    values = Settings(price_step=5.0, slippage_steps=1.0, commission_per_side_rub=17.0)
    old = Piece("MXU6", date(2026, 9, 16), date(2026, 9, 16), (), OLD, 0)
    new = Piece("MXZ6", date(2026, 9, 17), date(2026, 9, 17), WARMUP, NEW, PERIOD)
    stitch = stitched_view.Stitch(pieces=(old, new))
    run = asyncio.run(stitched_view.run(stitch, values, SETTINGS, steps={"MXU6": STEP}))
    assert [piece.run.costs.price_step for piece in run.pieces] == [STEP, 5.0], run.pieces


def test_the_exchange_answer_is_not_journaled_as_a_change_by_the_owner(
    loop, request: pytest.FixtureRequest
) -> None:
    """Стережёт: ответ биржи в журнале — своей строкой, а не «Настройки изменены».

    «Настройки изменены» на каждом запуске читалось бы как правка, которой
    владелец счёта не делал. ⚠️ Две мутации: снять `event=FROM_EXCHANGE`
    в `_point_taken` — появляется «Настройки изменены»; не писать строку
    вовсе (молчание) — нет строки биржи.
    """
    base = request.getfixturevalue("database")
    heard, _port = _started(loop, base, CHOSEN, exchange(STEP))
    events =[row.event for journal in heard.journals for row in journal]
    assert "Настройки изменены" not in events, (
        f"ответ биржи записан как правка человека: {events}"
    )
    assert port_module.FROM_EXCHANGE in events, f"ответ биржи в журнал не попал: {events}"


@pytest.mark.parametrize("bad", [float("nan"), float("inf")], ids=["nan", "inf"])
def test_a_card_step_that_is_not_finite_is_not_taken(loop, database, bad) -> None:  # noqa: F811
    """Стережёт: `NaN` и бесконечность из карточки шагом не становятся.

    `NaN <= 0` — ложь: без `math.isfinite` в `_learn_step` такой шаг доехал
    бы до движка, и предел заявки на выход посчитался бы из `NaN` молча.
    Канарейка — плашка ожидания шага осталась: шаг по-прежнему неизвестен.
    """
    heard, port = _started(loop, database, CHOSEN, exchange(bad))
    assert SYMBOL not in port._point.steps, port._point.steps  # noqa: SLF001 — что запомнил порт
    refused = [row for row in heard.journals[-1] if row.event == "Настройки не приняты"]
    assert not refused, "шаг из карточки дошёл до проверки настроек и отказом в журнал"
    assert port.values.price_step == 0.0, port.values.price_step
    assert port._engine_settings.time_exit_order is TimeExitOrder.MARKET  # noqa: SLF001
    assert heard.states[-1].step_wait, "шаг из NaN принят — плашки ожидания нет"
