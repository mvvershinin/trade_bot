"""Кнопки «Снять заявку…» и «Закрыть позицию…»: проводка окна до порта.

Задача владельца 13.11.2026. Окно ничего не решает: кнопки горят по снимку
боя (`hand_refused`), снимаемую заявку выбирает человек из списка порта,
команда уходит только после вопроса. Строка открытой позиции в выгрузку
не идёт: в файл — сделки, а не позиция, которая ещё не кончилась.
"""

from __future__ import annotations

import asyncio
import pathlib
from collections.abc import Iterator, Sequence
from datetime import datetime

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QDialog

from app.fact import FactPosition, LiveStatus
from engine import MSK
from engine import Side as EngineSide
from market import redact
from ui.main_window import MainWindow
from ui.models import RobotOrder, RobotOrders, RobotState, Side, TradeRow
from ui.ports import TerminalPort

ORDER = RobotOrder(key="k-1", words="продажа 1 MXZ6 по 251375; заявка робота «take:long:1»")


class HandPort(TerminalPort):
    """Порт, который запоминает ручные команды и отвечает списком заявок."""

    def __init__(self) -> None:
        super().__init__()
        self.cancelled: list[str] = []
        self.closed = 0
        self.orders = RobotOrders(orders=(ORDER,))

    def request_robot_orders(self) -> None:
        self.robot_orders_ready.emit(self.orders)

    def cancel_robot_order(self, key: str) -> None:
        self.cancelled.append(key)

    def close_position(self) -> None:
        self.closed += 1


@pytest.fixture
def setup(qapp, monkeypatch) -> Iterator[tuple[MainWindow, HandPort, list[str]]]:
    port = HandPort()
    window = MainWindow(port=port, sanitize=redact)
    asked: list[str] = []

    def ask(_title: str, text: str, _yes: str) -> bool:
        asked.append(text)
        return True

    def choose(orders: Sequence[RobotOrder]) -> RobotOrder | None:
        return orders[0]

    monkeypatch.setattr(window, "ask", ask)
    monkeypatch.setattr(window, "choose_order", choose)
    monkeypatch.setattr(window, "inform", lambda _title, text: asked.append(text))
    yield window, port, asked
    window.apply_state(RobotState())
    window.close()
    window.deleteLater()


def _long() -> LiveStatus:
    return LiveStatus(
        available=False, wanted=True, on=True, armed=True, hand_refused="",
        position=FactPosition(side=EngineSide.LONG, volume=1.0, entry_price=250_100.0),
    )


def test_buttons_are_off_with_the_reason_without_trade_access(setup) -> None:
    """Без связи или на токене для чтения — кнопки погашены, причина в подсказке."""
    window, _, _ = setup
    window.show_live(LiveStatus(available=False, hand_refused="Токен только для чтения."))
    for action in (window.cancel_order_action, window.close_position_action):
        assert not action.isEnabled()
        assert "Токен только для чтения." in action.toolTip()
    window.show_live(_long())
    assert window.cancel_order_action.isEnabled() and window.close_position_action.isEnabled()


def test_cancel_asks_with_the_order_words_and_sends_its_key(setup, qapp) -> None:
    """«Снять заявку…»: список от порта → вопрос словами заявки → команда с её ключом."""
    window, port, asked = setup
    window.show_live(_long())
    window.cancel_order_action.trigger()
    qapp.processEvents()  # выбор и вопрос — следующим оборотом цикла (`_open_modal`)
    assert port.cancelled == [ORDER.key]
    assert ORDER.words in asked[-1]


def test_an_empty_or_unread_list_is_said_and_sends_nothing(setup, qapp) -> None:
    """Пустой список и отказ чтения — словами в окне, команды нет (правило 13)."""
    window, port, asked = setup
    window.show_live(_long())
    port.orders = RobotOrders()
    window.cancel_order_action.trigger()
    qapp.processEvents()
    port.orders = RobotOrders(trouble="база занята")
    window.cancel_order_action.trigger()
    qapp.processEvents()
    assert port.cancelled == []
    assert "снимать нечего" in asked[0] and "база занята" in asked[1], asked


def test_close_asks_with_side_and_volume_then_sends(setup) -> None:
    """«Закрыть позицию…»: вопрос называет сторону и объём закрывающей заявки."""
    window, port, asked = setup
    window.show_live(_long())
    window.close_position_action.trigger()
    assert port.closed == 1
    assert "продажа 1" in asked[-1], asked[-1]


def test_the_open_position_row_is_not_exported(setup, tmp_path: pathlib.Path) -> None:
    """Строка открытой позиции живёт только в окне: в файл сделок она не идёт."""
    window, _, _ = setup
    at = datetime(2026, 11, 13, 9, 45, tzinfo=MSK)
    closed = TradeRow(
        entry_time=at, exit_time=at, side=Side.LONG, volume=1.0, entry_price=1.0,
        exit_price=2.0, exit_reason="тейк", profit_rub=1.0, profit_pct=1.0, commission_rub=0.0,
    )
    still = TradeRow(
        entry_time=at, exit_time=None, side=Side.LONG, volume=1.0, entry_price=1.0,
        exit_price=None, exit_reason="Позиция открыта. Результат плавающий", profit_rub=1.0,
        profit_pct=1.0, commission_rub=None,
    )
    window.journals.set_trades([closed, still])
    paths = window.export_journals_to(tmp_path)
    trades = next(path for path in paths if path.name.startswith("trades_"))
    text = trades.read_text(encoding="utf-8-sig")
    assert "тейк" in text
    assert "Результат плавающий" not in text, text


class _Quiet(HandPort):
    """Порт, который на просьбу не отвечает сразу: ответ испускает тест изнутри задачи."""

    def request_robot_orders(self) -> None:
        return None


def test_the_order_list_opened_by_a_port_signal_does_not_kill_a_running_task(
    loop, qapp
) -> None:
    """Стережёт `B-026` у «Снять заявку…»: модальное по сигналу порта — не внутри шага задачи.

    Порт испускает `robot_orders_ready` изнутри корутины. Настоящий выбор
    заявки (`QInputDialog`), открытый прямо в слоте, поднял бы вложенный цикл
    Qt внутри шага задачи, и соседняя задача получила бы «Cannot enter into
    task». Выбор и вопрос не подменяются: окно закрывает таймер.
    """
    port = _Quiet()
    window = MainWindow(port=port, sanitize=redact)
    window._timer.stop()
    window.show_live(_long())
    trouble: list[dict[str, object]] = []
    loop.set_exception_handler(lambda _loop, context: trouble.append(context))
    closed: list[str] = []

    def close_modal() -> None:
        for modal in QApplication.topLevelWidgets():
            if isinstance(modal, QDialog) and modal.isVisible():
                closed.append(type(modal).__name__)
                modal.reject()

    closer = QTimer()
    closer.setInterval(20)
    closer.timeout.connect(close_modal)

    async def busy() -> str:
        for _ in range(30):
            await asyncio.sleep(0.005)
        return "дошла до конца"

    async def main() -> asyncio.Task[str]:
        neighbour = asyncio.ensure_future(busy())
        await asyncio.sleep(0.01)
        window.request_cancel_order()
        port.robot_orders_ready.emit(RobotOrders(orders=(ORDER,)))
        await asyncio.sleep(0.5)
        return neighbour

    closer.start()
    try:
        neighbour = loop.run_until_complete(main())
        assert closed, "выбор заявки так и не открылся — тест проверяет не то"
        assert not trouble, "; ".join(str(item.get("message")) for item in trouble)
        assert neighbour.done() and neighbour.result() == "дошла до конца"
        assert port.cancelled == [], "отказ в выборе отправил снятие"
    finally:
        loop.set_exception_handler(None)
        # Позиция снимается до закрытия: иначе закрытие спросило бы человека.
        window.show_live(LiveStatus(available=False))
        window.apply_state(RobotState())
        window.close()
        window.deleteLater()
        qapp.processEvents()
        closer.stop()
