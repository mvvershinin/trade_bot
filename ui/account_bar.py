"""Строка «Счёт у брокера» под шапкой окна (решение 0070).

Показывает ровно то, что прислал порт (`TerminalPort.account_changed`,
`ui.models.AccountLine`): позицию по инструменту, живые заявки и рубли —
по ответу брокера, со временем чтения. Ничего не считает и не сверяет.
До первого чтения (связи с брокером ещё не было) строки нет вовсе.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QWidget

from ui.models import AccountLine
from ui.theme import Theme
from ui.theme import current as current_theme

__all__ = ["AccountBar"]


class AccountBar(QLabel):
    """Одна строка (переносится, если не влезает) — цвет тревоги, если чтение не удалось."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(True)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._theme: Theme = current_theme()
        self._line = AccountLine("")
        self.setVisible(False)

    def show_line(self, line: AccountLine) -> None:
        """Новая строка от порта: текст, подсказка, цвет."""
        self._line = line
        self.setText(line.text)
        self.setToolTip(line.tooltip)
        self.setVisible(bool(line.text))
        self._paint()

    def set_theme(self, theme: Theme) -> None:
        """Тема сменилась — перекрасить строку."""
        self._theme = theme
        self._paint()

    def _paint(self) -> None:
        colour = self._theme.warning if self._line.trouble else self._theme.text
        self.setStyleSheet(f"color: {colour}; font-weight: 600; padding: 2px 4px;")
