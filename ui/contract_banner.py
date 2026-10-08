"""Плашка «действующий контракт»: расхождение с настройкой и кнопка перехода.

Решение 0061: действующий контракт — тот, что живёт настоящей жизнью сейчас,
и программа узнаёт его по дневным объёмам биржи. Решение 0016: переход
в бою ручной. Поэтому плашка только **говорит** и даёт кнопку; тикер
меняется обычной правкой настроек с подтверждением, которую проводит окно.

Кнопка заблокирована, пока открыта позиция или робот не выключен — с
объяснением в подсказке, а не отказом после нажатия (бриф окна, §6):
смена тикера посреди позиции оставила бы её на контракте, за которым
робот больше не смотрит.

Торговых правил здесь нет: «какой контракт действующий» решает таблица
контрактов (`market/contracts.py`), сюда приходит готовый ответ.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from ui.models import ContractNotice, RobotState
from ui.models import switch_blocked_reason as blocked_reason
from ui.theme import current as current_theme
from ui.theme import scaled, text_on

__all__ = ["ContractBar", "blocked_reason", "notice_text"]

#: Кегль, как у остальных плашек над графиком (`_Banner.LARGE_POINTS`).
LARGE_POINTS = scaled(14.0)  # 14 пт — порог WCAG, ×1,2 — `FONT_SCALE`


def notice_text(notice: ContractNotice) -> str:
    """Текст плашки. Пусто — плашка не нужна."""
    if notice.mismatch:
        since = (
            f" с {notice.current_since:%d.%m.%Y}" if notice.current_since else ""
        )
        return (
            f"Действующий контракт — {notice.current}{since} (по дневным объёмам "
            f"биржи), а робот настроен на {notice.configured or 'пустой код'}. "
            "Программа сама тикер не меняет: переход — вашим подтверждением."
        )
    if notice.urgent and notice.trouble:
        return (
            f"Действующий контракт не определён. {notice.trouble} Уточнить "
            "таблицу у биржи — «Загрузить историю…» в меню «Программа»."
        )
    return ""


class ContractBar(QWidget):
    """Плашка над графиком и кнопка «Перейти на …»."""

    #: Человек нажал кнопку перехода: код действующего контракта.
    switch_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._notice = ContractNotice()
        self.label = QLabel()
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setContentsMargins(12, 8, 12, 8)
        font = self.label.font()
        font.setPointSizeF(max(font.pointSizeF() + 2.0, LARGE_POINTS))
        font.setBold(True)
        self.label.setFont(font)
        self.button = QPushButton()
        self.button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.button.clicked.connect(self._on_click)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.label, 1)
        row.addWidget(self.button)
        self.setVisible(False)

    @property
    def notice(self) -> ContractNotice:
        """Последние сведения — их берёт окно настроек при открытии."""
        return self._notice

    def show_notice(self, notice: ContractNotice, state: RobotState | None) -> None:
        """Показать сведения и привести кнопку к состоянию робота."""
        self._notice = notice
        text = notice_text(notice)
        if not text:
            self.setVisible(False)
            return
        fill = current_theme().warning
        self.label.setText(text)
        self.label.setStyleSheet(
            f"background: {fill}; color: {text_on(fill)}; border-radius: 4px;"
        )
        self.button.setVisible(notice.mismatch)
        self.button.setText(f"Перейти на {notice.current}…")
        self.update_state(state)
        self.setVisible(True)

    def update_state(self, state: RobotState | None) -> None:
        """Кнопка доступна только при выключенном роботе без позиции."""
        reason = blocked_reason(state)
        self.button.setEnabled(not reason)
        self.button.setToolTip(
            reason
            or f"Поставить {self._notice.current} в поле «Инструмент». Перед "
               "применением будет показано подтверждение с прежним и новым "
               "значением."
        )

    def _on_click(self) -> None:
        if self._notice.mismatch:
            self.switch_requested.emit(self._notice.current)
