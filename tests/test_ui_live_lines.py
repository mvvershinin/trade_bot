"""Окно боя: строка у кнопки говорит, уходят ли заявки, и вкладка сделок помечена (Э4 2в).

Два обещания окна, которых не стерёг никто (`tests/test_ui_live.py` — соседи):

* прогретый бой — строка «БОЙ: заявки уходят брокеру» цветом опасности;
  прогрев — «заявок ещё нет». Перепутать их — значит сказать человеку, что
  настоящих заявок нет, ровно тогда, когда они уходят;
* вкладка «Сделки» помечена боем, если среди строк есть сделка боевого
  режима, — по `origin` строки, а не по режиму окна (`D-172`).

Снимок — настоящий `app.fact.LiveStatus`; окно читает его через протокол
`ui.models.LiveView`.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

import pytest

from app.fact import LiveStatus
from market import redact
from ui.journals import REAL_MONEY_MARK, JournalTabs
from ui.main_window import MainWindow
from ui.models import RunOrigin, Side, TradeRow


@pytest.fixture
def window(qapp) -> Iterator[MainWindow]:
    shown = MainWindow(sanitize=redact)
    yield shown
    shown.close()
    shown.deleteLater()


def test_an_armed_live_says_orders_go_to_the_broker_and_warming_says_not_yet(window) -> None:
    """Прогрев и бой — разные строки: «заявок ещё нет» не говорится, когда они уходят."""
    window.show_live(LiveStatus(available=False, wanted=True, on=True, armed=True))
    armed = window.live_note.text()
    armed_style = window.live_note.styleSheet()
    window.show_live(LiveStatus(available=False, wanted=True, on=True, armed=False))
    warming = window.live_note.text()

    assert armed.startswith("БОЙ: заявки уходят брокеру"), armed
    assert window.theme.danger in armed_style, armed_style
    assert "заявок ещё нет" in warming and "заявок ещё нет" not in armed, (armed, warming)


def _trade(origin: RunOrigin | None) -> TradeRow:
    return TradeRow(
        entry_time=datetime(2026, 11, 2, 10, 5), exit_time=datetime(2026, 11, 2, 10, 20),
        side=Side.LONG, volume=1.0, entry_price=2850.0, exit_price=2860.0,
        exit_reason="сигнал", profit_rub=10.0, profit_pct=0.35, commission_rub=34.0,
        origin=origin,
    )


def test_the_trades_tab_is_marked_by_a_live_trade_row(qapp) -> None:
    """Сделка боевого режима среди строк — вкладка «Сделки» помечена; без неё — нет."""
    tabs = JournalTabs()
    try:
        tabs.set_trades([_trade(RunOrigin.PAPER)])
        assert tabs.tabs.tabText(0) == "Сделки"
        tabs.set_trades([_trade(RunOrigin.PAPER), _trade(RunOrigin.LIVE)])
        assert tabs.tabs.tabText(0) == "Сделки" + REAL_MONEY_MARK
        assert tabs.tabs.tabText(1) == "Решения робота", "пометка ушла на чужую вкладку"
        tabs.set_trades([_trade(None)])
        assert tabs.tabs.tabText(0) == "Сделки"
    finally:
        tabs.deleteLater()
