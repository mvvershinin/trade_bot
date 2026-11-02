"""Бой после второго раунда `/risk` по 2в: отложенная пересборка и позиция, оставшаяся у брокера.

Решение 0066, строки от 01.11.2026. Сторожа поведения, которое мутации
test-expert 02.11.2026 ломали без падения хоть одного теста:

* N1 — пока пересборка отложена, живая свеча растёт на баре **прежнего**
  размера, а строка окна «Ждёт пересборки боя» называет смену и причину
  и снимается, когда пересборка случилась;
* N2 — позиция окна после закрытия боевой стороны берётся у брокера:
  портфель не прочитан — позиция движка без уровня и словами «не прочитана»;
  брокер говорит ноль — позиции нет, что бы ни думал движок;
* N3 — позиция, которой робот не знал, показана ценой брокера и помечена
  «по брокеру, а не по роботу»; скользящий уровень у запомненной позиции
  не остаётся — его больше никто не сторожит;
* сверка после связи, не прочитавшая портфель, останавливает робота
  до первой заявки, а не проглатывается проходом.

Стенд — пара движков 2в (`tests/test_app_live_pair.py`), переподключение без
пауз (`tests/test_app_live_ride.py`).
"""

from __future__ import annotations

import dataclasses
import pathlib
from datetime import datetime, timedelta

import httpx
import pytest
import websockets

from app.fact import LiveStatus
from broker.session import PORTFOLIO_PATH
from engine import ExitReason
from tests.fake_broker import FakeBroker
from tests.test_app_live_pair import (
    _SPIKES,
    BASE,
    ENTRY_SPIKE,
    FIXED_TAKE,
    LAST_MINUTE_TOUCH,
    LIVE_FROM,
    NO_TAKE,
    OWNER,
    SYMBOL,
    Bench,
    Exchange,
    _bench,
    _close,
    _connect,
    _deals,
    _fills,
    _ride,
    _takes,
)
from tests.test_app_live_ride import _fast_reconnect, _relink

#: Закрытие 09:50: лонг 1 со входа 09:45 уже у «факта».
AFTER_ENTRY = LIVE_FROM + timedelta(minutes=50)


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch) -> FakeBroker:
    fake = FakeBroker()
    monkeypatch.setattr(websockets, "connect", fake.connect)
    return fake


def _portfolio_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """Портфель у брокера перестал читаться: 503 на каждый запрос портфеля, прочее — как было.

    Отказ — на транспорте подделки, а не подменой метода программы: до порта
    он доходит тем же путём, что настоящий (сессия, повторы, `BrokerError`).
    """
    answer = FakeBroker.__call__

    def down(self: FakeBroker, request: httpx.Request) -> httpx.Response:
        if (request.method, request.url.path) == ("GET", PORTFOLIO_PATH):
            return httpx.Response(503, text="Service Unavailable")
        return answer(self, request)

    monkeypatch.setattr(FakeBroker, "__call__", down)


# ------------------------------------------------------------------ N1


def test_while_the_rebuild_is_held_the_live_candle_grows_on_the_held_bar(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """N1 «живая свеча тоже»: свеча на краю графика — 15-минутный бар хода, а не 5-минутный.

    Лонг 1 на 15-минутках, в 09:50 «Размер свечи» — 5 минут; пересборка
    отложена позицией. Стережёт: с 09:51 по 09:58 край графика уточняется,
    и каждое уточнение — бар 09:45 (15-минутный, идёт до 10:00); 5-минутных
    баров 09:50 и 09:55 на краю нет. Свечи настроек вместо свечей хода
    не совпали бы с графиком хода, и край замер бы до пересборки.
    """
    drawn: list[datetime] = []

    async def go() -> str:
        bench, session = await _bench(tmp_path, broker, NO_TAKE)
        try:
            await _connect(bench)
            await _ride(bench, AFTER_ENTRY)
            bench.port.apply_settings(dataclasses.replace(NO_TAKE, timeframe="5 минут"))
            await bench.port.wait()
            bench.port.candle_updated.connect(lambda candle: drawn.append(candle.opens_at))
            bench.port.candle_appended.connect(lambda candle: drawn.append(candle.opens_at))
            await _ride(
                bench, LIVE_FROM + timedelta(minutes=58), since=AFTER_ENTRY + timedelta(minutes=1)
            )
            return bench.port.live_status().held
        finally:
            await _close(bench, session)

    held = loop.run_until_complete(go())
    assert held, "пересборка не отложена — проверять нечего"
    assert drawn, "край графика за 09:51–09:58 не уточнялся ни разу: живая свеча замерла"
    assert set(drawn) == {LIVE_FROM + timedelta(minutes=45)}, [f"{t:%H:%M}" for t in drawn]


def test_the_held_rebuild_line_names_the_change_and_the_reason_and_goes_after_the_rebuild(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """N1: строка окна «Ждёт пересборки боя» — что сменено и почему ждёт; после пересборки пуста.

    Стережёт снимок боя (`LiveStatus.held`), а не журнал: строку журнала
    человек мог пропустить, строка окна стоит, пока ждёт. Пересборка после
    выхода по концу окна — и строки, обещающей пересборку, больше нет.
    """

    async def go() -> tuple[str, str]:
        bench, session = await _bench(tmp_path, broker, NO_TAKE)
        try:
            await _connect(bench)
            await _ride(bench, AFTER_ENTRY)
            bench.port.apply_settings(dataclasses.replace(NO_TAKE, timeframe="5 минут"))
            await bench.port.wait()
            waiting = bench.port.live_status().held
            await _ride(bench, since=AFTER_ENTRY + timedelta(minutes=1))
            assert bench.events().count("Бой включён") == 2, "пересборки не было"
            return waiting, bench.port.live_status().held
        finally:
            await _close(bench, session)

    waiting, after = loop.run_until_complete(go())
    assert "Размер свечи: 15 минут → 5 минут" in waiting, waiting
    assert "лонг 1" in waiting, f"строка не называет, почему ждёт: «{waiting}»"
    assert after == "", f"пересборка случилась, а окно всё ещё ждёт её: «{after}»"


# ------------------------------------------------------------------ N2


def test_an_unreadable_portfolio_at_turn_off_shows_the_engine_position_without_a_take(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N2: «Выключить бой», портфель не прочитался — позиция движка, без уровня, со словами.

    Лонг 1 с неподвижным тейком у брокера. Стережёт: окно не теряет позицию
    («позиции нет» при лонге у брокера — молчание), уровень не обещает
    (жив ли тейк, программа не проверила), а оговорка говорит, что брокер
    не прочитан.
    """
    monkeypatch.delitem(_SPIKES, ENTRY_SPIKE)

    async def go() -> tuple[LiveStatus, LiveStatus]:
        bench, session = await _bench(tmp_path, broker, FIXED_TAKE)
        try:
            await _connect(bench)
            await _ride(bench, AFTER_ENTRY)
            before = bench.port.live_status()
            _portfolio_down(monkeypatch)
            bench.port.set_live(False)
            await bench.port.wait()
            return before, bench.port.live_status()
        finally:
            await _close(bench, session)

    before, after = loop.run_until_complete(go())
    assert before.position is not None and before.position.take_level is not None, (
        "до выключения у «факта» нет лонга с тейком — проверять нечего"
    )
    assert after.position is not None and after.position.signed == 1, after.position
    assert after.position.take_level is None, "окно обещает тейк, которого не проверяло"
    assert "не прочитана" in after.position_note, after.position_note


def test_a_position_closed_by_hand_at_the_broker_is_gone_from_the_window_after_turn_off(
    loop, tmp_path: pathlib.Path, broker: FakeBroker
) -> None:
    """N2: позицию закрыли руками у брокера, робот не заметил; «Выключить бой» — позиции нет.

    Движок считает лонг 1, у брокера ноль (кадр продажи потерян). Стережёт:
    после выключения окно верит свежему чтению брокера, а не движку.
    """

    async def go() -> tuple[LiveStatus, LiveStatus]:
        bench, session = await _bench(tmp_path, broker, NO_TAKE)
        try:
            await _connect(bench)
            await _ride(bench, AFTER_ENTRY)
            broker.muted = True
            broker.foreign_trade("2", 1, BASE, ticker=SYMBOL)
            broker.muted = False
            before = bench.port.live_status()
            bench.port.set_live(False)
            await bench.port.wait()
            return before, bench.port.live_status()
        finally:
            await _close(bench, session)

    before, after = loop.run_until_complete(go())
    assert before.position is not None and broker.position(SYMBOL) == 0, (
        "движок не в лонге или у брокера не ноль — сценарий не тот"
    )
    assert after.position is None, f"окно показывает позицию, которой у брокера нет: {after}"


# ------------------------------------------------------------------ N3


#: Как позиция у брокера разошлась с роботом: что у брокера, что считал робот.
UNKNOWN = {
    # Вход исполнен у брокера, движку ещё не отдан (растущей минутки нет).
    "undelivered": (1, "позиции нет"),
    # За лонгом робота у брокера прошла чужая покупка по другой цене; кадр потерян.
    "foreign-buy": (2, "лонг 1"),
}


@pytest.mark.parametrize("how", list(UNKNOWN))
def test_a_position_the_robot_did_not_know_is_shown_at_the_broker_price_and_marked_so(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    """N3: у брокера не та позиция, что у робота; бой выключен — позиция брокера и пометка.

    Стережёт: объём и цена входа — брокера (средняя цена портфеля), а не
    робота; оговорка и строка «Бой выключен» говорят «по брокеру, а не
    по роботу» и что робот считал: человек видит, что робот этой позиции
    не знал.
    """
    monkeypatch.delitem(_SPIKES, ENTRY_SPIKE)
    if how == "undelivered":
        monkeypatch.setattr(Exchange, "reveal", lambda _self: None)
    signed, believed = UNKNOWN[how]

    async def go() -> tuple[LiveStatus, str]:
        bench, session = await _bench(tmp_path, broker, FIXED_TAKE)
        try:
            await _connect(bench)
            if how == "undelivered":
                await _ride(bench, LIVE_FROM + timedelta(minutes=45))
            else:
                await _ride(bench, AFTER_ENTRY)
                broker.muted = True
                broker.foreign_trade("1", 1, BASE + 1000.0, ticker=SYMBOL)
                broker.muted = False
            bench.port.set_live(False)
            await bench.port.wait()
            return bench.port.live_status(), bench.said("Бой выключен")
        finally:
            await _close(bench, session)

    status, off = loop.run_until_complete(go())
    assert broker.position(SYMBOL) == signed, "у брокера не та позиция — сценарий не тот"
    assert status.position is not None and status.position.signed == signed, status.position
    assert status.position.entry_price == broker.average_price(SYMBOL), status.position
    assert f"по брокеру, а не по роботу: робот считал {believed}" in status.position_note, (
        status.position_note
    )
    assert "по брокеру, а не по роботу" in off, off


def test_a_trailing_level_is_not_kept_on_the_position_left_at_the_broker(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N3: скользящий уровень встал в 10:15, в 10:16 бой выключен — у позиции уровня нет.

    Скользящий уровень сторожит боевая часть программы (Р2); с выключением
    боя его не сторожит никто. Стережёт: окно показывает шорт без уровня —
    уровень на экране читался бы как защита, которой нет.
    """
    monkeypatch.delitem(_SPIKES, LAST_MINUTE_TOUCH)

    async def go() -> tuple[LiveStatus, LiveStatus]:
        bench, session = await _bench(tmp_path, broker, OWNER)
        try:
            await _connect(bench)
            await _ride(bench, LIVE_FROM + timedelta(minutes=76))
            armed = bench.port.live_status()
            bench.port.set_live(False)
            await bench.port.wait()
            return armed, bench.port.live_status()
        finally:
            await _close(bench, session)

    armed, after = loop.run_until_complete(go())
    assert armed.position is not None and armed.position.take_level is not None, (
        "к 10:16 скользящий уровень у шорта не встал — проверять нечего"
    )
    assert _takes(broker) == [], "у брокера лимитная — это не скользящий уровень"
    assert after.position is not None and after.position.signed == -1, after.position
    assert after.position.take_level is None, "окно показывает уровень, который не сторожится"


# ------------------------------------------------------------- честность подставной биржи


def test_a_signal_exit_with_a_live_fixed_take_is_a_market_deal_equal_to_the_forecast(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Тейк лонга жив у брокера, сигнал 10:00 переворачивает — сделки «факта» = сделкам «прогноза».

    Неподвижный тейк — лимитная у брокера (Р1); выход по сигналу при нём —
    рыночная по своей цене (открытие 10:00, 249 800), а не по уровню тейка
    (251 375, выше рынка). Стережёт честность сравнений «факт/прогноз»
    с `FIXED_TAKE`: подставная биржа, исполняющая любой рыночный выход
    по уровню позиции (правило стопа `low <= level` у тейка лонга истинно
    всегда), продала бы «факту» лонг по цене тейка, и сравнение разошлось
    бы — или, хуже, сошлось бы с ошибкой модели. Сценарий, которого нет
    в `test_fact_deals_equal_forecast_deals_on_an_ideal_broker`: там тейк
    исполняется в баре входа, и рыночного выхода при живом тейке нет.
    """
    monkeypatch.delitem(_SPIKES, ENTRY_SPIKE)

    async def go() -> tuple[object, object, bool]:
        bench, session = await _bench(tmp_path, broker, FIXED_TAKE)
        try:
            await _connect(bench)
            await _ride(bench, AFTER_ENTRY)
            lives = bool(_takes(broker))
            await _ride(bench, since=AFTER_ENTRY + timedelta(minutes=1))
            status = bench.port.live_status()
            return status.fact, status.forecast, lives
        finally:
            await _close(bench, session)

    fact, forecast, lives = loop.run_until_complete(go())
    assert lives, "к 09:50 тейка лонга у брокера нет — сценарий не тот"
    assert forecast is not None and forecast.deals, "у прогноза нет сделок"
    first = forecast.deals[0]
    assert first.exit_reason is ExitReason.SIGNAL, _deals(forecast)
    assert _deals(fact) == _deals(forecast)
    assert _fills(fact) == _fills(forecast)


# ------------------------------------------------------------- сверка после связи


def test_an_unreadable_portfolio_after_the_link_returns_halts_before_any_order(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Связь вернулась в 09:50, портфель не читается — робот стоит, переворот 10:00 не подан.

    Сверка после связи не прошла («Позицию у брокера прочитать не удалось»):
    позиция у брокера неизвестна. Стережёт: робот остановлен словами
    сверки, и ни одной новой заявки — проход, проглотивший отказ сверки,
    подал бы переворот на 10:00 при неизвестной позиции.
    """
    monkeypatch.delitem(_SPIKES, ENTRY_SPIKE)

    async def go() -> tuple[str, int]:
        bench, session = await _bench(tmp_path, broker, FIXED_TAKE)
        _fast_reconnect(bench, session)
        try:
            await _connect(bench)
            await _ride(bench, AFTER_ENTRY)
            sent = len(broker.commands)
            _portfolio_down(monkeypatch)
            assert await _relink(broker) >= 1, "поток статусов не переоткрылся"
            await _ride(
                bench, LIVE_FROM + timedelta(minutes=61), since=AFTER_ENTRY + timedelta(minutes=1)
            )
            created = [key for kind, key in broker.commands[sent:] if kind == "create"]
            return bench.port.halted, len(created)
        finally:
            await _close(bench, session)

    halted, created = loop.run_until_complete(go())
    assert "Позицию у брокера прочитать не удалось" in halted, f"робот не встал: «{halted}»"
    assert created == 0, "после несостоявшейся сверки заявка ушла брокеру"


# ------------------------------------------------------------- B-082


async def _off_with_a_live_take(
    bench: Bench, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch, how: str
) -> str:
    """Лонг с живым тейком у брокера; бой выключен так, что уровень у позиции не остаётся."""
    await _connect(bench)
    await _ride(bench, AFTER_ENTRY)
    if how == "unread":
        _portfolio_down(monkeypatch)
    else:
        broker.muted = True
        broker.foreign_trade("1", 1, BASE, ticker=SYMBOL)
        broker.muted = False
    bench.port.set_live(False)
    await bench.port.wait()
    return bench.said("Бой выключен")


@pytest.mark.parametrize("how", ["unread", "foreign-buy"])
def test_the_live_off_line_does_not_deny_a_take_that_is_live_at_the_broker(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    """Тейк лонга жив у брокера, уровня у запомненной позиции нет — строка не говорит «тейка нет».

    Уровень не остаётся у позиции, когда программа не может за него
    поручиться: портфель не прочитан или позиция у брокера не та, что
    у робота. Окно в этом случае честно говорит «рассчитывать на тейк нельзя»
    (`ui/live_control.py::TAKE_NONE`). Строка журнала говорит обратное
    утверждение — «тейка у неё нет», и при `unread` противоречит собственной
    оговорке в скобках («жив ли тейк у брокера, неизвестно»). Цена: человек,
    поверивший строке, закрывает позицию руками и не снимает тейк — живая
    лимитная остаётся у брокера и при касании откроет позицию без робота.
    """
    monkeypatch.delitem(_SPIKES, ENTRY_SPIKE)

    async def go() -> str:
        bench, session = await _bench(tmp_path, broker, FIXED_TAKE)
        try:
            return await _off_with_a_live_take(bench, broker, monkeypatch, how)
        finally:
            await _close(bench, session)

    off = loop.run_until_complete(go())
    assert _takes(broker), "тейка у брокера нет — сценарий не тот"
    assert "лонг" in off, off
    assert "тейка у неё нет" not in off, off
