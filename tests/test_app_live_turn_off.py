"""Выключение боя: что окно видит и что журнал говорит о позиции у брокера (Н2–Н4, B-082).

Сторожа поведения, которое мутации test-expert 02.11.2026 ломали без падения
хоть одного теста (правило 13 и «сторожа проверяет не тот, кто их написал»):

* Н2 — портфель не прочитан: позиция — по роботу **с неотданным** у брокера,
  с её объёмом, а неотданный выход позицию закрывает; при пустой позиции —
  строка уровня «предупреждение», а не справка;
* Н3 — закрытие боевой стороны с ошибкой не оставляет прогон «факта»
  незакрытым в журнале прогонов; строка «Бой выключен» — одна, и повторное
  закрытие хода её не повторяет;
* Н4 — отмена выключения сразу видна окну снимком боя;
* B-082 — «тейка у неё нет» только там, где программа может поручиться:
  брокер прочитан, позиция совпала, журнал живых лимитных прочитан и пуст.

Стенд — пара движков 2в (`tests/test_app_live_pair.py`): порт, подставной
брокер, своя база во временном каталоге, сети нет.
"""

from __future__ import annotations

import dataclasses
import pathlib
from datetime import timedelta

import pytest
import websockets

from app.fact import LiveStatus
from app.live_trading import LiveDesk
from market.journal import RunOrigin
from tests.fake_broker import FakeBroker
from tests.test_app_live_pair import (
    _SPIKES,
    BASE,
    ENTRY_SPIKE,
    FIXED_TAKE,
    LIVE_FROM,
    NO_TAKE,
    SYMBOL,
    Bench,
    Exchange,
    _bench,
    _close,
    _connect,
    _ride,
    _Unread,
)
from ui.models import DecisionLevel, DecisionRow

#: Закрытие 09:50: лонг со входа 09:45 уже у «факта».
AFTER_ENTRY = LIVE_FROM + timedelta(minutes=50)


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch) -> FakeBroker:
    fake = FakeBroker()
    monkeypatch.setattr(websockets, "connect", fake.connect)
    return fake


async def _unread(_self: object) -> tuple[int, float | None]:
    raise _Unread("проба: портфель не прочитан")


def _rows(bench: Bench) -> list[DecisionRow]:
    """Строки журнала решений с уровнем — `Bench.notes` уровня не хранит."""
    rows: list[DecisionRow] = []
    bench.port.decision_appended.connect(
        lambda row: rows.append(row) if isinstance(row, DecisionRow) else None
    )
    return rows


# ------------------------------------------------------------------ Н2


def test_turning_off_with_an_unread_portfolio_and_no_robot_position_is_a_warning(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Н2: позиции у робота нет, портфель не прочитан — «Проверьте позицию» предупреждением.

    У брокера позиция могла быть: справкой такая строка теряется среди
    сотни строк журнала. Стережёт: одна строка «Бой выключен», уровень
    «предупреждение», слова «Проверьте позицию».
    """
    monkeypatch.delitem(_SPIKES, ENTRY_SPIKE)

    async def go() -> list[DecisionRow]:
        bench, session = await _bench(tmp_path, broker, FIXED_TAKE)
        rows = _rows(bench)
        try:
            await _connect(bench)
            await _ride(bench, LIVE_FROM + timedelta(minutes=30))
            assert bench.port.live_status().position is None, "позиция есть — сценарий не тот"
            monkeypatch.setattr(LiveDesk, "holding", _unread)
            bench.port.set_live(False)
            await bench.port.wait()
            return [row for row in rows if row.event == "Бой выключен"]
        finally:
            await _close(bench, session)

    [off] = loop.run_until_complete(go())
    assert "Проверьте позицию" in off.reason, off.reason
    assert off.level is DecisionLevel.WARNING, off.level


def test_an_unread_portfolio_shows_the_undelivered_entry_with_its_own_volume(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Н2: вход на 2 исполнен у брокера, движку не отдан, портфель не прочитан — окно видит лонг 2.

    Не «лонг 1» и не «позиции нет»: человек, закрывающий позицию руками,
    продаст столько, сколько видит.
    """
    monkeypatch.delitem(_SPIKES, ENTRY_SPIKE)
    monkeypatch.setattr(Exchange, "reveal", lambda _self: None)

    async def go() -> LiveStatus:
        bench, session = await _bench(tmp_path, broker, dataclasses.replace(FIXED_TAKE, volume=2))
        try:
            await _connect(bench)
            await _ride(bench, LIVE_FROM + timedelta(minutes=45))
            monkeypatch.setattr(LiveDesk, "holding", _unread)
            bench.port.set_live(False)
            await bench.port.wait()
            return bench.port.live_status()
        finally:
            await _close(bench, session)

    status = loop.run_until_complete(go())
    assert broker.position(SYMBOL) == 2, "вход не исполнился у брокера — сценарий не тот"
    assert status.position is not None and status.position.signed == 2, status.position
    assert "не прочитана" in status.position_note, status.position_note


def test_an_unread_portfolio_after_an_undelivered_exit_shows_no_robot_position(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Н2: выход по концу окна исполнен у брокера, движку не отдан, портфель не прочитан.

    Движок ещё держит шорт 1, у брокера ноль. Показать «шорт 1» — позвать
    человека закрывать руками позицию, которой нет: покупка откроет лонг
    без робота. Стережёт: позиции по роботу нет, оговорка «не прочитана»
    стоит, строка «Бой выключен» — предупреждение с «Проверьте позицию».
    """
    monkeypatch.delitem(_SPIKES, ENTRY_SPIKE)
    monkeypatch.setattr(Exchange, "reveal", lambda _self: None)

    async def go() -> tuple[object, LiveStatus, str]:
        bench, session = await _bench(tmp_path, broker, NO_TAKE)
        try:
            await _connect(bench)
            await _ride(bench, LIVE_FROM + timedelta(minutes=120))
            engine = bench.port.live_status().position
            monkeypatch.setattr(LiveDesk, "holding", _unread)
            bench.port.set_live(False)
            await bench.port.wait()
            return engine, bench.port.live_status(), bench.said("Бой выключен")
        finally:
            await _close(bench, session)

    engine, status, off = loop.run_until_complete(go())
    assert broker.position(SYMBOL) == 0 and engine is not None, "выход отдан движку — не тот случай"
    assert status.position is None, f"у брокера ноль, а окно показывает {status.position}"
    assert "не прочитана" in status.position_note, status.position_note
    assert "Проверьте позицию" in off, off


# ------------------------------------------------------------------ Н3


def test_a_failed_close_of_the_live_side_still_closes_the_live_run(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Н3: закрытие боевой стороны упало — прогон «факта» в журнале прогонов всё равно закрыт.

    Незакрытый прогон в журнале — «бой ещё идёт» для того, кто через неделю
    разбирает, что робот делал на счёте.
    """
    monkeypatch.delitem(_SPIKES, ENTRY_SPIKE)

    async def broken(_self: object) -> None:
        raise _Unread("проба: закрытие стороны упало")

    async def go() -> list[object]:
        bench, session = await _bench(tmp_path, broker, FIXED_TAKE)
        try:
            await _connect(bench)
            await _ride(bench, AFTER_ENTRY)
            monkeypatch.setattr(LiveDesk, "aclose", broken)
            bench.port.set_live(False)
            await bench.port.wait()
            page = await bench.worker.journal_sessions(origin=RunOrigin.LIVE)
            return [row.finished_at for row in page.rows]
        finally:
            await _close(bench, session)

    finished = loop.run_until_complete(go())
    assert finished, "прогона «факта» в журнале нет — проверять нечего"
    assert all(moment is not None for moment in finished), f"прогон «факта» не закрыт: {finished}"


def test_the_live_off_line_is_said_once_even_when_the_watch_closes_again(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Н3: «Бой выключен» — одна строка; следующее закрытие хода (поток отключён) её не повторяет.

    Повтор через полчаса читается как второе выключение — и как то, что бой
    между ними снова был включён.
    """
    monkeypatch.delitem(_SPIKES, ENTRY_SPIKE)

    async def go() -> list[str]:
        bench, session = await _bench(tmp_path, broker, FIXED_TAKE)
        try:
            await _connect(bench)
            await _ride(bench, AFTER_ENTRY)
            bench.port.set_live(False)
            await bench.port.wait()
            assert "Бой выключен" in bench.events(), "выключение не сказано — проверять нечего"
            bench.port.stream(False)
            await bench.port.wait()
            return bench.events()
        finally:
            await _close(bench, session)

    events = loop.run_until_complete(go())
    assert events.count("Бой выключен") == 1, events


# ------------------------------------------------------------------ Н4


def test_cancelling_an_off_shows_the_live_on_in_the_window_at_once(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Н4: «Выключить» и сразу «Включить» — снимок боя окну «бой просили» тем же нажатием.

    Окно рисует кнопку по снимку (`live_changed`), а не по нажатию: без
    снимка кнопка висит «выключено», пока идёт бой с позицией.
    """
    monkeypatch.delitem(_SPIKES, ENTRY_SPIKE)

    async def go() -> tuple[LiveStatus, LiveStatus]:
        bench, session = await _bench(tmp_path, broker, FIXED_TAKE)
        published: list[LiveStatus] = []
        bench.port.live_changed.connect(published.append)
        try:
            await _connect(bench)
            await _ride(bench, AFTER_ENTRY)
            bench.port.set_live(False)
            bench.port.set_live(True)
            at_once = published[-1]
            await bench.port.wait()
            return at_once, published[-1]
        finally:
            await _close(bench, session)

    at_once, after = loop.run_until_complete(go())
    assert at_once.wanted, "окно после отмены выключения видит «бой не просили»"
    assert after.wanted and after.on, after


# ------------------------------------------------------------------ B-082


async def _journal_unread(_self: object) -> tuple[float, ...]:
    raise _Unread("проба: журнал команд не прочитан")


@pytest.mark.parametrize("how", ["portfolio-unread", "foreign-buy", "journal-unread"])
def test_the_live_off_line_does_not_say_no_take_when_it_cannot_vouch(
    loop, tmp_path: pathlib.Path, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    """B-082: живой лимитной в журнале нет, но поручиться за «тейка нет» нельзя — «неизвестно».

    Тейка у позиции нет и по журналу, но портфель не прочитан, позиция
    у брокера не та, что у робота, или не прочитан сам журнал. «Тейка у неё
    нет» здесь — утверждение, которого программа не проверяла.
    """

    async def go() -> tuple[str, LiveStatus]:
        bench, session = await _bench(tmp_path, broker, NO_TAKE)
        try:
            await _connect(bench)
            await _ride(bench, AFTER_ENTRY)
            if how == "portfolio-unread":
                monkeypatch.setattr(LiveDesk, "holding", _unread)
            elif how == "journal-unread":
                monkeypatch.setattr(LiveDesk, "resting_levels", _journal_unread)
            else:
                broker.muted = True
                broker.foreign_trade("1", 1, BASE, ticker=SYMBOL)
                broker.muted = False
            bench.port.set_live(False)
            await bench.port.wait()
            return bench.said("Бой выключен"), bench.port.live_status()
        finally:
            await _close(bench, session)

    off, status = loop.run_until_complete(go())
    assert "лонг" in off, off
    assert "тейка у неё нет" not in off, off
    assert "неизвестно" in off, off
    assert "тейка у неё нет" not in status.take_note, status.take_note
